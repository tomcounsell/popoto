"""SupersessionProtocol — Identity-scoped belief revision over ValidityField.

Where :class:`~popoto.fields.validity_field.ValidityField` owns the *storage* of
validity intervals and supersession chains, ``SupersessionProtocol`` owns the
*vocabulary*: "this new claim replaces whatever was previously believed about
``(subject, predicate)``". It is a stateless coordinator of ``@staticmethod``s
mirroring :class:`~popoto.fields.observation.ObservationProtocol` — never
inherited by a model, never instantiated.

Four operations:
    - ``identity_key(subject, predicate)``: Normalize a claim identity into a
      16-hex digest naming that identity's open-claim pointer (plan D7).
    - ``supersede(new_instance, identity_key=...)``: Close whichever record is
      currently open for that identity, chain it to ``new_instance``, and
      repoint the pointer — one atomic ``EVAL``.
    - ``invalidate(instance, at=..., superseded_by=...)``: The direct,
      identity-free form. Close one specific record, optionally chaining it to
      the record that replaced it.
    - ``superseded_by`` / ``supersedes`` / ``chain``: Bidirectional provenance
      traversal over the two derived chain HASHes.

Design notes:
    - **Chain links are derived state, never model-hash bytes** (plan D3). The
      forward/reverse links live in two HASHes owned by ``ValidityField``, so an
      append-only journal (#560) can adopt this protocol unchanged. There is no
      ``Relationship`` field and no ``HSET`` onto a record's own hash here.
    - **All mutations route through** ``ValidityField.execute_supersede``. This
      module never issues a ``ZADD``/``HSET`` of its own, so there is exactly one
      place that knows ``SUPERSEDE_LUA``'s KEYS/ARGV order.
    - **Membership is decided inside the script, and an absent member raises**
      (#588). ``_member_key`` resolves a key string and issues no Redis command;
      ``SUPERSEDE_LUA`` runs the ``EXISTS`` check at the instant of the write, so
      pipeline mode and immediate mode behave identically. An unsaved instance
      therefore raises :class:`~popoto.fields.validity_field.\
ValidityMemberAbsentError` — a ``ValueError`` subclass — instead of returning
      ``None``, which was byte-identical to the normal pipeline-mode return and
      gave the caller no signal at all. Nothing is written on that path: the
      script errors before its first write command.
    - **The one exception is the observation signal path.**
      ``ObservationProtocol._apply_supersession`` keeps a client-side ``EXISTS``
      probe of its own, because telemetry must not raise and, by construction,
      never has a same-transaction successor.
    - **Cycle-safe traversal.** ``chain()`` carries a ``seen`` set and treats a
      link naming a record with no interval entry as a chain end, so a corrupt
      or hard-deleted chain terminates instead of hanging.

Example:
    from popoto import Model, KeyField, ValidityField, SupersessionProtocol

    class Fact(Model):
        fact_id = KeyField()
        validity = ValidityField()

    identity = SupersessionProtocol.identity_key("user_42", "subscription_plan")

    old = Fact(fact_id="free").save()
    SupersessionProtocol.supersede(old, identity_key=identity)  # -> None

    new = Fact(fact_id="enterprise").save()
    SupersessionProtocol.supersede(new, identity_key=identity)  # -> old's key

    SupersessionProtocol.superseded_by(old)   # -> new
    SupersessionProtocol.supersedes(new)      # -> old
    SupersessionProtocol.chain(new)           # -> [old, new]
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

import redis.client
import redis.exceptions

from ..redis_db import POPOTO_REDIS_DB
from .validity_field import (
    ValidityField,
    ValidityMemberAbsentError,
    map_lua_error,
)

logger = logging.getLogger("POPOTO.SupersessionProtocol")

# ---------------------------------------------------------------------------
# Identity normalization constants (plan D7)
#
# Magic numbers pinned in-repo, not constructor kwargs: changing either would
# silently repartition every already-written open-claim pointer.
# ---------------------------------------------------------------------------

IDENTITY_SEPARATOR = "\x00"
"""Join byte between normalized identity components.

LOAD-BEARING: ``\\x00`` cannot occur in normalized component text (it is
rejected outright), so ``("ab", "c")`` and ``("a", "bc")`` can never hash to the
same digest. Any printable delimiter would reintroduce that false-merge."""

IDENTITY_DIGEST_BYTES = 8
"""``blake2b`` digest size, giving a 16-hex-character key segment.

Hashing (rather than embedding the caller's text) is what keeps arbitrary user
strings out of the Redis keyspace."""


class SupersedeDeclinedError(RuntimeError):
    """Raised when the successor's ``save()`` was declined, so nothing closed.

    ``Model.save()`` has several early-return gates -- the never-record firewall
    and the write filter among them -- that *return* instead of raising. In
    pipeline mode the value they return is the pipeline itself, which is
    byte-identical to success, so a caller that queued a close on top of it
    would commit a membership change with no record backing it.
    :func:`_save_and_close` checks for both shapes (a falsy return and a
    ``_never_record_verdict`` stamped on the instance) and raises this instead.

    A ``RuntimeError`` subclass, not a ``ValueError``: it reports a broken
    invariant inside the write path rather than a malformed argument, and
    ``RuntimeError`` is what this path raised before the error was given a type,
    so existing handlers keep working.

    Attributes:
        verdict: The blocking ``NeverRecordVerdict``, or ``None`` when the save
            was declined by some other early-return gate. Content-free, under
            the same no-quoting rule as
            :class:`~popoto.exceptions.NeverRecordException`.
    """

    def __init__(self, message: str, verdict: Any = None) -> None:
        super().__init__(message)
        self.verdict = verdict


@dataclass(frozen=True)
class SupersedeResult:
    """Outcome of :meth:`SupersessionProtocol.save_and_supersede` (plan D6).

    Attributes:
        instance: The successor, saved (or queued for saving).
        closed_key: The superseded record's Redis key, or ``None``. On a
            caller-supplied pipeline ``None`` means *unknown until you execute*,
            not "nothing was closed" — read :attr:`close_index` out of your own
            ``execute()`` results for the truth. This is the same
            honest-unknown contract as ``AnnotationResult.target_closed=None``.
        pipeline: The caller's pipeline, unexecuted; ``None`` when this call
            owned and executed its own.
        close_index: Index of the queued supersede command in the caller's
            pipeline, or ``None`` when the pipeline was owned here.
    """

    instance: Any
    closed_key: Optional[str] = None
    pipeline: Optional[redis.client.Pipeline] = None
    close_index: Optional[int] = None


class SupersessionProtocol:
    """Identity-scoped belief revision and provenance traversal.

    All methods are static — the protocol is a stateless coordinator over a
    model's :class:`~popoto.fields.validity_field.ValidityField`. It is NOT a
    mixin and must not be inherited by a model.

    Every method takes an optional ``field_name`` (which ``ValidityField`` to
    act on, auto-detected when the model declares exactly one) and an optional
    ``pipeline`` (threaded straight through to the underlying ``EVAL``).
    """

    @staticmethod
    def identity_key(subject: str, predicate: str) -> str:
        """Normalize a claim identity into a 16-hex digest (plan D7).

        Casefolds, strips, and collapses internal whitespace in each component,
        joins them with :data:`IDENTITY_SEPARATOR`, and hashes the result with
        ``blake2b(digest_size=8)``. Deterministic and LLM-free: semantic
        normalization ("is ``plan`` the same predicate as ``subscription_tier``?")
        is a downstream opt-in, not core.

        Args:
            subject: What the claim is about, e.g. ``"user_42"``.
            predicate: Which property of the subject, e.g. ``"plan"``.

        Returns:
            16 lowercase hex characters, safe to embed in a Redis key.

        Raises:
            ValueError: If either component is empty, whitespace-only, or
                contains a literal ``\\x00`` (which would let a caller forge a
                collision across the component boundary).
        """
        parts = []
        for label, raw in (("subject", subject), ("predicate", predicate)):
            text = "" if raw is None else str(raw)
            if IDENTITY_SEPARATOR in text:
                raise ValueError(
                    f"SupersessionProtocol.identity_key: {label} must not contain a "
                    "NUL byte (it is the component separator)"
                )
            normalized = " ".join(text.split()).casefold()
            if not normalized:
                raise ValueError(
                    f"SupersessionProtocol.identity_key: {label} must be a "
                    f"non-empty, non-whitespace string, got {raw!r}"
                )
            parts.append(normalized)
        joined = IDENTITY_SEPARATOR.join(parts).encode("utf-8")
        return hashlib.blake2b(joined, digest_size=IDENTITY_DIGEST_BYTES).hexdigest()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @staticmethod
    def supersede(
        new_instance: Any,
        *,
        identity_key: Union[str, Sequence[str]],
        at: Optional[float] = None,
        field_name: Optional[str] = None,
        pipeline: Optional[redis.client.Pipeline] = None,
    ) -> Optional[str]:
        """Replace whichever record is currently open for ``identity_key``.

        One ``EVAL``: closes the incumbent's interval, writes both chain links,
        opens ``new_instance``'s interval, and repoints the identity's open
        pointer. There is no observable state in which the incumbent is closed
        but unchained, or chained but still index-visible.

        Args:
            new_instance: The saved model instance carrying the new claim.
            identity_key: Either a digest from :meth:`identity_key`, or a
                ``(subject, predicate)`` pair to normalize on the caller's
                behalf.
            at: Valid-time instant of the transition (epoch seconds). Defaults
                to now. Transaction time (``ingested_at``) is always real now.
            field_name: Name of the ``ValidityField`` to act on. Auto-detected
                when omitted.
            pipeline: Optional Redis pipeline; the ``EVAL`` is queued onto it and
                the closed-member result is only available at ``execute()`` time.

        Returns:
            The closed (superseded) record's Redis key, or ``None`` when there
            was no incumbent for this identity — the first claim about an
            identity simply opens, writing no chain link. Also ``None`` when a
            ``pipeline`` was supplied (the script has not run yet).

        Raises:
            ValueError: If ``identity_key`` is malformed.
            ValidityMemberAbsentError: If ``new_instance`` does not exist at the
                instant of the write — an unsaved instance, or one hard-deleted
                since. **Changed in 1.9.0**: this used to return ``None``, which
                was indistinguishable from the pipeline-mode return. Nothing is
                written on this path. In pipeline mode the error surfaces from
                ``pipe.execute()``; use :meth:`save_and_supersede` to get it
                typed.
            ValidityCloseBeforeStartError: If ``at`` precedes the incumbent's own
                ``valid_from`` (a zero-or-negative-length interval is a caller
                bug, not a state to store silently).

        Note:
            ``at`` is a *close-time* assertion about the incumbent, never a
            start-time assertion about ``new_instance``. To set a successor's
            valid-time, construct it with ``validity=t`` (plan D3).
        """
        digest = _coerce_identity(identity_key)
        resolved = _resolve_field_name(new_instance, field_name)
        if resolved is None:
            return None
        new_member = _member_key(new_instance)
        if new_member is None:
            # Key resolution itself failed, so there is nothing to name in the
            # script. The membership decision is the script's (#588); this is
            # only the unresolvable case.
            raise ValidityMemberAbsentError(
                "SupersessionProtocol.supersede: could not resolve a Redis key "
                f"for {new_instance!r}"
            )

        clock = time.time()
        instant = clock if at is None else float(at)
        return _apply_supersede(
            new_instance,
            resolved,
            new_member=new_member,
            identity_digest=digest,
            clock=clock,
            instant=instant,
            pipeline=pipeline,
        )

    @staticmethod
    def invalidate(
        instance: Any,
        at: Optional[float] = None,
        superseded_by: Any = None,
        field_name: Optional[str] = None,
        pipeline: Optional[redis.client.Pipeline] = None,
    ) -> Optional[str]:
        """Close one specific record's interval — the identity-free direct form.

        Args:
            instance: The saved record to close.
            at: Close instant (epoch seconds). Defaults to now.
            superseded_by: Optional saved instance that replaced ``instance``.
                When given, both chain links are written and its interval is
                opened in the same ``EVAL``.
            field_name: Name of the ``ValidityField``. Auto-detected when
                omitted.
            pipeline: Optional Redis pipeline; the ``EVAL`` is queued onto it.

        Returns:
            The closed record's Redis key, or ``None`` if it was already closed
            or a ``pipeline`` was supplied.

        Raises:
            ValidityMemberAbsentError: If ``instance`` or ``superseded_by`` does
                not exist at the instant of the write. **Changed in 1.9.0**:
                this used to return ``None``. Nothing is written on this path.
            ValidityCloseBeforeStartError: If ``at`` precedes the record's own
                ``valid_from``.

        Note:
            A same-pipeline successor now works and is the recommended spelling::

                pipe = popoto.get_redis().pipeline()
                new.save(pipeline=pipe)
                SupersessionProtocol.invalidate(
                    old, superseded_by=new, pipeline=pipe
                )
                pipe.execute()
        """
        resolved = _resolve_field_name(instance, field_name)
        if resolved is None:
            return None
        old_member = _member_key(instance)
        if old_member is None:
            raise ValidityMemberAbsentError(
                "SupersessionProtocol.invalidate: could not resolve a Redis key "
                f"for {instance!r}"
            )
        new_member = ""
        if superseded_by is not None:
            new_member = _member_key(superseded_by) or ""
            if not new_member:
                raise ValidityMemberAbsentError(
                    "SupersessionProtocol.invalidate: could not resolve a Redis "
                    f"key for the successor {superseded_by!r}"
                )

        clock = time.time()
        instant = clock if at is None else float(at)
        return _apply_invalidate(
            instance,
            resolved,
            old_member=old_member,
            new_member=new_member,
            clock=clock,
            instant=instant,
            pipeline=pipeline,
        )

    @staticmethod
    def save_and_supersede(
        new_instance: Any,
        *,
        identity_key: Union[str, Sequence[str]],
        at: Optional[float] = None,
        field_name: Optional[str] = None,
        pipeline: Optional[redis.client.Pipeline] = None,
    ) -> SupersedeResult:
        """Save ``new_instance`` and close the identity's incumbent, atomically.

        The append-and-close shape a bitemporal store wants, as one supported
        call instead of a pipeline the caller assembles by hand: the successor's
        hash, its indexes, its open interval, the incumbent's close, both chain
        links, and the pointer repoint all apply in a single MULTI/EXEC, so no
        reader ever sees both records open or neither present.

        This is also the only place a caller gets a *typed* error in pipeline
        shape (plan D4): ``execute_supersede``'s remap cannot live on its own
        pipeline branch, because redis-py raises during ``execute()`` result
        parsing long after that method returned. This method owns the
        ``execute()``, so it can remap.

        Args:
            new_instance: The unsaved model instance carrying the new claim.
            identity_key: A digest from :meth:`identity_key`, or a
                ``(subject, predicate)`` pair to normalize here.
            at: Valid-time instant of the transition. Defaults to now.
            field_name: Name of the ``ValidityField``. Auto-detected when
                omitted.
            pipeline: Optional caller pipeline to compose onto. When given,
                nothing is executed here — see :class:`SupersedeResult`.

        Returns:
            A :class:`SupersedeResult`.

        Raises:
            ValueError: If the model declares no ``ValidityField``, if
                ``identity_key`` is malformed, or if ``pipeline`` is not a
                transactional Redis pipeline in a queueing state.
            SupersedeDeclinedError: If ``new_instance.save()`` was declined —
                either a falsy return or a ``_never_record_verdict`` stamped on
                the instance. The never-record firewall and the write filter
                decline a save by returning rather than raising, and queuing a
                close behind a record that was never written is exactly the
                failure this guards. A ``RuntimeError`` subclass, so callers
                that caught the untyped error keep working.
            ValidityMemberAbsentError: If the incumbent named by the identity
                pointer was hard-deleted, or the successor's save was declined.
            ValidityCloseBeforeStartError: If ``at`` precedes the incumbent's
                stored ``valid_from``.
        """
        digest = _coerce_identity(identity_key)
        return _save_and_close(
            new_instance,
            field_name=field_name,
            at=at,
            pipeline=pipeline,
            mode="supersede",
            identity_digest=digest,
            old_member="",
            entry_point="save_and_supersede",
        )

    @staticmethod
    def save_and_invalidate(
        new_instance: Any,
        *,
        closes: Any,
        at: Optional[float] = None,
        field_name: Optional[str] = None,
        pipeline: Optional[redis.client.Pipeline] = None,
    ) -> SupersedeResult:
        """Save ``new_instance`` and close ``closes``, atomically.

        The identity-free form of :meth:`save_and_supersede`: the incumbent is
        named explicitly rather than resolved through an open-claim pointer, and
        being named makes it a caller *assertion* — a ``closes`` that does not
        exist at EXEC time raises rather than being read as "no incumbent".

        Args:
            new_instance: The unsaved successor.
            closes: The saved record this successor replaces.
            at: Valid-time instant of the transition. Defaults to now.
            field_name: Name of the ``ValidityField``. Auto-detected when
                omitted.
            pipeline: Optional caller pipeline to compose onto.

        Returns:
            A :class:`SupersedeResult` whose ``closed_key`` is ``closes``'s
            Redis key when this call closed it.

        Raises:
            The same set as :meth:`save_and_supersede`.
        """
        old_member = _member_key(closes)
        if not old_member:
            raise ValidityMemberAbsentError(
                "SupersessionProtocol.save_and_invalidate: could not resolve a "
                f"Redis key for closes={closes!r}"
            )
        return _save_and_close(
            new_instance,
            field_name=field_name,
            at=at,
            pipeline=pipeline,
            mode="invalidate",
            identity_digest="",
            old_member=old_member,
            entry_point="save_and_invalidate",
        )

    # ------------------------------------------------------------------
    # Provenance traversal (plan D3)
    # ------------------------------------------------------------------

    @staticmethod
    def superseded_by(instance: Any, field_name: Optional[str] = None) -> Any:
        """Return the instance that superseded ``instance``, or ``None``.

        One ``HGET`` against the forward chain HASH, then a hydrate by Redis key.
        Returns ``None`` at the head of a chain, for an unsaved instance, for a
        model without a ``ValidityField``, or when the link names a record that
        no longer exists.
        """
        return _walk_one(instance, field_name, forward=True)

    @staticmethod
    def supersedes(instance: Any, field_name: Optional[str] = None) -> Any:
        """Return the instance that ``instance`` superseded, or ``None``.

        The mirror of :meth:`superseded_by`: one ``HGET`` against the reverse
        chain HASH. Returns ``None`` at the tail of a chain.
        """
        return _walk_one(instance, field_name, forward=False)

    @staticmethod
    def chain(instance: Any, field_name: Optional[str] = None) -> "list[Any]":
        """Return the full supersession chain, oldest first, including ``instance``.

        Walks the reverse links back to the oldest ancestor, then the forward
        links out to the newest descendant, so the chain is recoverable from any
        member — not just its head or tail.

        Termination is guaranteed two ways, because a chain read from a live
        store cannot be assumed well-formed:

        1. A ``seen`` set stops a cycle. A corrupt chain terminates; it never
           hangs.
        2. A link naming a record with no ``valid_from`` entry is treated as a
           chain end. ``ValidityField.on_delete`` removes a hard-deleted record's
           interval entries and its own chain *fields*, but a neighbor's link may
           still name it as a *value*; that dangling value ends the walk rather
           than yielding a phantom.

        Args:
            instance: Any member of the chain.
            field_name: Name of the ``ValidityField``. Auto-detected when
                omitted.

        Returns:
            ``list`` of instances ordered oldest -> newest. ``[instance]`` when
            the record has no chain links, and ``[]`` when the model has no
            ``ValidityField`` or the instance is unsaved.
        """
        resolved = _resolve_field_name(instance, field_name)
        if resolved is None:
            return []
        model = type(instance)
        valid_from_key, _ = ValidityField.get_interval_keys(model, resolved)

        anchor = _member_key(instance)
        if anchor is None:
            return []
        # Membership, not resolvability. ``_member_key`` no longer probes
        # (#588 D1), so the "unsaved instance -> []" contract this method
        # documents has to live here. ``ZSCORE`` rather than ``EXISTS`` on
        # purpose: it is the same rule ``_walk_links`` already applies to a
        # dangling link, so an anchor and a link are judged by one criterion.
        # Read-only path; ``_member_key`` still issues zero commands.
        if POPOTO_REDIS_DB.zscore(valid_from_key, anchor) is None:
            return []

        fwd_key = ValidityField.get_chain_fwd_key(model, resolved)
        rev_key = ValidityField.get_chain_rev_key(model, resolved)

        seen = {anchor}
        older = _walk_links(rev_key, anchor, valid_from_key, seen)
        newer = _walk_links(fwd_key, anchor, valid_from_key, seen)

        chain: "list[Any]" = []
        for member in reversed(older):
            hydrated = _hydrate(model, member)
            if hydrated is not None:
                chain.append(hydrated)
        chain.append(instance)
        for member in newer:
            hydrated = _hydrate(model, member)
            if hydrated is not None:
                chain.append(hydrated)
        return chain


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _coerce_identity(identity_key: Union[str, Sequence[str]]) -> str:
    """Accept either a digest or a ``(subject, predicate)`` pair.

    A ``str`` is taken as an already-normalized digest from
    :meth:`SupersessionProtocol.identity_key`; a 2-sequence is normalized here.
    """
    if isinstance(identity_key, str):
        if not identity_key.strip():
            raise ValueError(
                "SupersessionProtocol.supersede: identity_key must be a non-empty "
                "digest or a (subject, predicate) pair"
            )
        return identity_key
    if isinstance(identity_key, Sequence) and len(identity_key) == 2:
        subject, predicate = identity_key
        return SupersessionProtocol.identity_key(subject, predicate)
    raise ValueError(
        "SupersessionProtocol.supersede: identity_key must be a digest string or a "
        f"(subject, predicate) pair, got {identity_key!r}"
    )


def _member_key(instance: Any) -> Optional[str]:
    """Return an instance's Redis key string, or ``None`` if it cannot be resolved.

    Resolution only. This function issues **no Redis command** — membership is
    decided inside ``SUPERSEDE_LUA`` at the instant of the write (#588). The
    ``EXISTS`` probe that used to live here answered the right question at a
    moment when the answer could not stay true: the write it guarded happens
    later, inside the script, at EXEC time. In pipeline mode "later" is
    unbounded, which is how a same-transaction successor came back as ``0`` and
    turned an ``invalidate`` into a silent no-op.

    ``"Model:None"`` (a model with an unset ``KeyField``) is returned from here
    like any other string and is rejected by the script's ``EXISTS`` check,
    because it genuinely does not exist.
    """
    try:
        member = instance.db_key.redis_key
    except (TypeError, ValueError):
        return None
    return member or None


def _validate_caller_pipeline(pipeline: Any, entry_point: str) -> None:
    """Refuse a pipeline that cannot deliver the atomicity this API promises.

    The same three checks, and the same reasoning, as
    ``ProvenanceJournal._write``'s pre-flight: a non-transactional pipeline
    voids the guarantee outright, and a ``WATCH``ing pipeline that has not been
    put into ``MULTI`` executes each command immediately instead of queueing, so
    ``Model.save()`` would apply part-way and the close would land against a
    half-written record.

    ``watching and not explicit_transaction`` is the precise condition, NOT
    ``watching`` alone: ``watch()`` + ``multi()`` is redis-py's standard
    optimistic-locking pattern and queues normally.
    """
    if not isinstance(pipeline, redis.client.Pipeline):
        raise ValueError(
            f"SupersessionProtocol.{entry_point}: pipeline must be a redis "
            f"Pipeline, got {type(pipeline).__name__}"
        )
    if pipeline.transaction is not True:
        raise ValueError(
            f"SupersessionProtocol.{entry_point}: pipeline(transaction=False) "
            "voids the save-and-close atomicity guarantee. Use "
            "popoto.get_redis().pipeline() (transactional by default)."
        )
    if getattr(pipeline, "watching", False) and not getattr(
        pipeline, "explicit_transaction", False
    ):
        raise ValueError(
            f"SupersessionProtocol.{entry_point}: a WATCHing pipeline that is "
            "not yet in MULTI executes commands immediately instead of "
            "queueing, so the save would apply while the close did not. Call "
            "pipeline.multi() to open the transaction -- that keeps your "
            "optimistic lock and makes the save-and-close atomic -- or use a "
            "fresh pipeline. Do NOT call UNWATCH: it would discard the lock "
            "you took."
        )


def _save_and_close(
    new_instance: Any,
    *,
    field_name: Optional[str],
    at: Optional[float],
    pipeline: Optional[redis.client.Pipeline],
    mode: str,
    identity_digest: str,
    old_member: str,
    entry_point: str,
) -> SupersedeResult:
    """Shared body of ``save_and_supersede`` / ``save_and_invalidate`` (plan D6)."""
    resolved = _resolve_field_name(new_instance, field_name)
    if resolved is None:
        # This entry point is explicit, so silence would be wrong -- unlike
        # ``supersede``, which is also reached from the observation signal path.
        raise ValueError(
            f"SupersessionProtocol.{entry_point}: "
            f"{type(new_instance).__name__} declares no ValidityField"
        )

    owns_pipeline = pipeline is None
    if pipeline is not None:
        _validate_caller_pipeline(pipeline, entry_point)
        pipe = pipeline
    else:
        pipe = POPOTO_REDIS_DB.pipeline()

    saved = new_instance.save(pipeline=pipe)

    # Defence in depth against the whole class of bug the close path belongs
    # to. ``Model.save()``'s early-return gates (the never-record firewall, the
    # write filter) *return* instead of raising, and in pipeline mode what they
    # return is the pipeline -- indistinguishable from success. Each one is a
    # way for the successor to not be written while the close below is queued
    # anyway, closing the incumbent's interval with nothing behind it.
    #
    # Two shapes are checked, not one. ``not saved`` catches the immediate-mode
    # falsy return; ``_never_record_verdict`` catches the pipeline-mode return,
    # which is truthy. Raised, not asserted -- ``python -O`` strips asserts, and
    # this must hold in production above all.
    #
    # ``ProvenanceJournal._write`` keeps its own copy of this guard for the
    # branch that does not close a target and so never reaches here; see the
    # comment there.
    blocked = getattr(new_instance, "_never_record_verdict", None)
    if not saved or blocked is not None:
        cause = (
            f"the never-record firewall ({blocked.reason})"
            if blocked is not None
            else "never-record firewall or write filter"
        )
        raise SupersedeDeclinedError(
            f"SupersessionProtocol.{entry_point}: save() of "
            f"{type(new_instance).__name__} was declined ({cause}), so the "
            "close would have been queued behind a record that was never "
            "written.",
            verdict=blocked,
        )

    new_member = _member_key(new_instance)
    if not new_member:
        raise ValidityMemberAbsentError(
            f"SupersessionProtocol.{entry_point}: could not resolve a Redis key "
            f"for {new_instance!r} after save()"
        )

    clock = time.time()
    instant = clock if at is None else float(at)
    close_index = len(pipe.command_stack)
    ValidityField.execute_supersede(
        new_instance,
        resolved,
        new_member=new_member,
        mode=mode,
        now=clock,
        valid_from=instant,
        ingested_at=clock,
        close_at=instant,
        old_member=old_member,
        identity_digest=identity_digest,
        # Plan D3: ``at`` is a close-time assertion about the incumbent, never a
        # start-time assertion about the successor.
        assert_valid_from=False,
        pipeline=pipe,
    )

    if not owns_pipeline:
        # The caller owns execution, so nothing has run yet and there is no
        # truthful answer to "what was closed". ``closed_key=None`` here means
        # *unknown until you execute*; ``close_index`` is how the caller learns
        # the truth from their own results.
        return SupersedeResult(
            instance=new_instance,
            closed_key=None,
            pipeline=pipe,
            close_index=close_index,
        )

    try:
        results = pipe.execute()
    except redis.exceptions.ResponseError as e:
        raise map_lua_error(e) from e
    closed = results[close_index] if close_index < len(results) else None
    if isinstance(closed, bytes):
        closed = closed.decode()
    return SupersedeResult(
        instance=new_instance,
        closed_key=str(closed) if closed else None,
        pipeline=None,
        close_index=None,
    )


def _resolve_field_name(instance: Any, field_name: Optional[str]) -> Optional[str]:
    """Return the name of the ``ValidityField`` to act on, or ``None``.

    ``None`` means "this model has no validity axis", which every shipped model
    is today — every protocol entry point degrades to a no-op on it.
    """
    meta = getattr(instance, "_meta", None)
    fields = getattr(meta, "fields", None) if meta is not None else None
    if not fields:
        return None
    if field_name is not None:
        field = fields.get(field_name)
        return field_name if isinstance(field, ValidityField) else None
    for name, field in fields.items():
        if isinstance(field, ValidityField):
            return name
    return None


def _apply_supersede(
    instance: Any,
    field_name: str,
    *,
    new_member: str,
    identity_digest: str,
    clock: float,
    instant: float,
    pipeline: Optional[redis.client.Pipeline],
) -> Optional[str]:
    """Run the script in mode ``'supersede'``, resolving the incumbent by pointer.

    Bitemporal split: ``instant`` is valid time (both the incumbent's close and
    the newcomer's start), ``clock`` is transaction time.
    """
    result = ValidityField.execute_supersede(
        instance,
        field_name,
        new_member=new_member,
        mode="supersede",
        now=clock,
        valid_from=instant,
        ingested_at=clock,
        close_at=instant,
        identity_digest=identity_digest,
        pipeline=pipeline,
    )
    return _closed_key(result, pipeline)


def _apply_invalidate(
    instance: Any,
    field_name: str,
    *,
    old_member: str,
    new_member: str,
    clock: float,
    instant: float,
    pipeline: Optional[redis.client.Pipeline],
) -> Optional[str]:
    """Run the script in mode ``'invalidate'`` against one explicit member."""
    result = ValidityField.execute_supersede(
        instance,
        field_name,
        new_member=new_member,
        mode="invalidate",
        now=clock,
        valid_from=instant if new_member else None,
        ingested_at=clock,
        close_at=instant,
        old_member=old_member,
        pipeline=pipeline,
    )
    return _closed_key(result, pipeline)


def _closed_key(
    result: Any, pipeline: Optional[redis.client.Pipeline]
) -> Optional[str]:
    """Normalize ``execute_supersede``'s return into ``str | None``.

    On the pipeline branch the script has not run yet and the return value is
    the pipeline itself, so there is no closed member to report.
    """
    if pipeline is not None or result is None:
        return None
    return result.decode() if isinstance(result, bytes) else str(result)


def _walk_one(instance: Any, field_name: Optional[str], forward: bool) -> Any:
    """One hop along the chain in either direction; ``None`` at either end."""
    resolved = _resolve_field_name(instance, field_name)
    if resolved is None:
        return None
    member = _member_key(instance)
    if member is None:
        return None
    model = type(instance)
    link_key = (
        ValidityField.get_chain_fwd_key(model, resolved)
        if forward
        else ValidityField.get_chain_rev_key(model, resolved)
    )
    linked = POPOTO_REDIS_DB.hget(link_key, member)
    if not linked:
        return None
    member_key = linked.decode() if isinstance(linked, bytes) else str(linked)
    return _hydrate(model, member_key)


def _walk_links(
    link_key: str, start: str, valid_from_key: str, seen: "set[str]"
) -> "list[str]":
    """Follow ``link_key`` from ``start``, returning members in hop order.

    Stops on a missing link, on a member already in ``seen`` (cycle), or on a
    member with no ``valid_from`` entry (hard-deleted / dangling link). ``seen``
    is mutated so both directions share one visited set.
    """
    members: "list[str]" = []
    current = start
    while True:
        linked = POPOTO_REDIS_DB.hget(link_key, current)
        if not linked:
            return members
        member = linked.decode() if isinstance(linked, bytes) else str(linked)
        if member in seen:
            logger.debug("chain: cycle detected at %s, terminating walk", member)
            return members
        if POPOTO_REDIS_DB.zscore(valid_from_key, member) is None:
            logger.debug("chain: dangling link to %s, terminating walk", member)
            return members
        seen.add(member)
        members.append(member)
        current = member


def _hydrate(model: Any, redis_key: str) -> Any:
    """Load a chain member by Redis key, or ``None`` if it no longer exists."""
    try:
        return model.query.get(redis_key=redis_key)
    except (TypeError, ValueError, AttributeError, KeyError):
        # A record whose hash is unreadable must not break provenance traversal
        # for the rest of the chain. Deliberately narrow — a genuine Redis error
        # stays loud rather than being reported as "no such ancestor".
        logger.debug("chain: failed to hydrate %s", redis_key, exc_info=True)
        return None
