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
    - **Graceful degradation, narrowly scoped.** Only *key resolution* is
      wrapped in ``except (TypeError, ValueError)`` — an unsaved instance
      degrades to a no-op before any Redis write is issued, so no partial index
      state is possible. Errors raised by the script itself (notably a close-at
      that precedes the record's own ``valid_from``) propagate as ``ValueError``.
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
from typing import Any, Optional, Sequence, Union

import redis.client

from ..redis_db import POPOTO_REDIS_DB
from .validity_field import ValidityField

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
            ``pipeline`` was supplied, or when ``new_instance`` is unsaved.

        Raises:
            ValueError: If ``identity_key`` is malformed, or if ``at`` precedes
                the incumbent's own ``valid_from`` (a zero-or-negative-length
                interval is a caller bug, not a state to store silently).
        """
        digest = _coerce_identity(identity_key)
        resolved = _resolve_field_name(new_instance, field_name)
        if resolved is None:
            return None
        new_member = _member_key(new_instance)
        if new_member is None:
            # Unsaved instance. Degrade before any write is issued, so no
            # partial index state (no valid_from entry, no chain link, no
            # pointer) can exist.
            logger.debug("supersede: unsaved instance, no-op")
            return None

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
            The closed record's Redis key, or ``None`` if it was already closed,
            unsaved, or a ``pipeline`` was supplied.

        Raises:
            ValueError: If ``at`` precedes the record's own ``valid_from``.
        """
        resolved = _resolve_field_name(instance, field_name)
        if resolved is None:
            return None
        old_member = _member_key(instance)
        if old_member is None:
            logger.debug("invalidate: unsaved instance, no-op")
            return None
        new_member = ""
        if superseded_by is not None:
            new_member = _member_key(superseded_by) or ""
            if not new_member:
                # An unsaved successor would produce a chain link to a record
                # that does not exist. Degrade the whole call rather than close
                # the incumbent into a dangling chain.
                logger.debug("invalidate: unsaved successor, no-op")
                return None

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
        anchor = _member_key(instance)
        if anchor is None:
            return []

        model = type(instance)
        fwd_key = ValidityField.get_chain_fwd_key(model, resolved)
        rev_key = ValidityField.get_chain_rev_key(model, resolved)
        valid_from_key, _ = ValidityField.get_interval_keys(model, resolved)

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
    """Return a persisted instance's Redis key, or ``None`` if it is not persisted.

    An unsaved instance does *not* raise on ``db_key.redis_key`` — a model with
    an unset ``KeyField`` cheerfully yields ``"Model:None"``, which would open a
    validity interval for a record that does not exist and leave permanent
    orphan index state. So membership is established by an ``EXISTS`` against
    the record hash, and every mutation resolves its keys through here *before*
    issuing any write. That ordering is what makes the unsaved-instance path a
    true no-op rather than a partial one.
    """
    try:
        member = instance.db_key.redis_key
    except (TypeError, ValueError):
        return None
    if not member or not POPOTO_REDIS_DB.exists(member):
        return None
    return member


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
