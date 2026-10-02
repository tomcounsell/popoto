"""
Validity Field - Bitemporal Validity Intervals and Supersession Index State
===========================================================================

This module provides :class:`ValidityField`, the *validity axis* for popoto
models (issue #580). Where ``DecayingSortedField`` answers "how important is
this memory right now?", ``ValidityField`` answers the prior question: "is this
memory still true at all?"

Motivation
----------
An agent learns "user is on the free plan". Two weeks later it learns "user
upgraded to enterprise". Without a validity axis the stale fact keeps its place
in every index and only loses ground gradually, through decay — so it can still
be packed into the agent's context ahead of the correction. ``ValidityField``
makes that first fact *stop being a member* of default retrieval the moment it
is superseded, while keeping it fully queryable in historical mode.

Design Philosophy
-----------------
- **Validity decides membership, decay decides ordering among the valid.** The
  two axes compose and neither needs to know the other's constants.
- **Not a** :class:`~popoto.fields.sorted_field_mixin.SortedFieldMixin`. This is
  load-bearing, not an oversight — see the class docstring. Validity must never
  win a query's ordering field.
- **Closed, never deleted.** Superseding a record closes its interval; the
  record and its chain links survive for provenance and ``as_of`` replay.
- **No model-hash mutation for chain links** (plan D3). Chain links live in two
  derived HASHes so an append-only journal (#560) can adopt this field unchanged.
- **Valkey-safe.** Core commands only (``ZADD``/``ZSCORE``/``ZREM``/
  ``ZRANGEBYSCORE``/``HSET``/``HDEL``/``GET``/``SET``/``DEL``/``EXISTS``) plus
  Lua 5.1. No Redis-module commands (``BF.``/``CMS.``/``TS.``/``TOPK.``)
  anywhere.

Index Structure (plan D1) — six Redis keys per model/field::

    $ValidityF:Model:field:valid_from      ZSET   member -> valid-from epoch
    $ValidityF:Model:field:invalid_at      ZSET   member -> close epoch, +inf = open
    $ValidityF:Model:field:ingested_at     ZSET   member -> ingest epoch
    $ValidityF:Model:field:chain:fwd       HASH   old redis_key -> superseding key
    $ValidityF:Model:field:chain:rev       HASH   new redis_key -> superseded key
    $ValidityF:Model:field:open:{digest}   STRING identity digest -> open record

(The ``$ValidityF`` prefix is auto-derived by the ``FieldBase`` metaclass from
the class name ``ValidityField``.)

Since #631 (WS1e) every read and write of those keys goes through the storage
backend -- ``popoto.backends.get_backend()`` -- rather than the Redis client:
``supersede`` / ``interval_of`` / ``interval_members`` / ``drop_validity`` /
``open_pointer`` (the validity group) plus ``sorted_members`` for the
``__current=False`` complement. The backend receives the opaque
``$ValidityF:<Model>`` namespace and the field name and derives the six keys
itself, byte-equal to the helpers below (WS0 deviation 10). The three
transfer-path methods (``export_state`` / ``import_state`` /
``find_open_pointers_for_member``) are out of the slice and use the
``native()`` escape hatch; each site is ledgered in the WS1e PR body.

An as-of-``t`` membership test is ``valid_from <= t AND invalid_at > t``: two
``ZRANGEBYSCORE``s intersected, or two ``ZSCORE``s inside Lua. ``+inf`` as the
open-interval sentinel is native to both Redis and Valkey sorted sets.

Example:
    from popoto import Model, KeyField, ValidityField

    class Fact(Model):
        fact_id = KeyField()
        validity = ValidityField()

    old = Fact(fact_id="plan-1").save()   # interval opens at now, closes at +inf
    new = Fact(fact_id="plan-2").save()

    # The explicit close below is NOT optional (issue #693). Saving alone only
    # ever OPENS intervals, so a save-only model's exclusion set stays empty,
    # unless a save declares a future `valid_from`.
    # Close `old` and chain it to `new` in one atomic EVAL:
    ValidityField.execute_supersede(
        Fact, "validity", new_member=new.db_key.redis_key,
        mode="supersede", old_member=old.db_key.redis_key,
    )

    Fact.query.filter(validity__current=True)      # -> only `new`
    Fact.query.filter(validity__as_of=earlier)     # -> `old`
"""

import logging
import time
from typing import TYPE_CHECKING, Any, Optional, Union, cast

from ..backends import UnitOfWork, as_key_str, get_backend
from ..models.db_key import DB_key
from ..redis_db import ENCODING
from .constants import Defaults
from .field import Field

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..models.base import Model

    #: Every key helper here reads only class-level metadata, so callers pass
    #: either the model class (``SupersessionProtocol``) or a live instance
    #: (``on_save`` / ``on_delete``). Both are accepted deliberately.
    ModelLike = Union[Model, type[Model]]

logger = logging.getLogger("POPOTO.ValidityField")

#: Valid ``mode`` values for :data:`SUPERSEDE_LUA` / :meth:`ValidityField.execute_supersede`.
VALID_MODES = frozenset({"open", "supersede", "invalidate"})

#: Lua error token returned when a close-at timestamp precedes the record's own
#: ``valid_from``. Mapped to :class:`ValueError` by :meth:`ValidityField.execute_supersede`,
#: mirroring the ``POPOTO_UNIQUE_CONFLICT`` mapping in ``indexed_field_mixin.py``.
CLOSE_BEFORE_START_ERROR = "POPOTO_VALIDITY_CLOSE_BEFORE_START"

#: Lua error token returned when a member named by the caller (the successor, or
#: an explicitly-named incumbent) does not exist at the instant of the write
#: (#588). The reply carries two diagnostic tokens after this one: the role
#: (``successor`` / ``incumbent``) and the member key.
MEMBER_ABSENT_ERROR = "POPOTO_VALIDITY_MEMBER_ABSENT"

#: Lua error token returned when a caller *asserts* a ``valid_from`` (ARGV[8] ==
#: ``'1'``) that disagrees with the start already stored in the ``valid_from``
#: ZSET. Without this the assertion would lose silently to ``ZADD NX`` and leave
#: the model hash and the index answering differently (#588 secondary defect).
VALID_FROM_CONFLICT_ERROR = "POPOTO_VALIDITY_VALID_FROM_CONFLICT"

#: Models already warned about the TTL/validity interaction (plan D9). Keyed by
#: ``(model name, field name)`` so the warning fires exactly once per pair.
_TTL_WARNED: "set[tuple[str, str]]" = set()


class ValidityError(ValueError):
    """Base for every typed failure raised out of :data:`SUPERSEDE_LUA`.

    Subclasses :class:`ValueError` deliberately (plan D4). Two shipped contracts
    depend on it: ``ObservationProtocol._apply_supersession`` degrades on
    ``except (TypeError, ValueError)``, and the V0 test suite asserts
    ``pytest.raises(ValueError)`` for close-before-start. Widening those to a new
    base class would be a silent behavior change on a signal path.
    """


class ValidityMemberAbsentError(ValidityError):
    """A member the caller named does not exist at the instant of the write.

    Raised for a successor that was never saved (including the ``"Model:None"``
    key an unsaved instance yields) and for an incumbent named explicitly via
    ``old_member``. An incumbent merely *resolved* from the open-claim pointer is
    a hint, not an assertion, and its absence means "no incumbent" instead.
    """


class ValidityCloseBeforeStartError(ValidityError):
    """A close instant precedes the record's own stored ``valid_from``."""


class ValidityValidFromConflictError(ValidityError):
    """An asserted ``valid_from`` disagrees with the already-stored start.

    Valid-time has exactly one writer: the field value at construction. Any other
    writer that *asserts* a disagreeing start gets this rather than losing
    silently to ``ZADD NX``.
    """


#: Token -> exception dispatch, consulted in order by :func:`map_lua_error`.
#:
#: An ordered tuple rather than a dict on purpose: matching is by *substring* of
#: ``str(ResponseError)`` (Redis versions differ on whether ``error_reply``
#: output is prefixed), so the tokens must be tested in a declared order and a
#: future token that is a prefix of another cannot shadow it.
_LUA_ERROR_MAP = (
    (MEMBER_ABSENT_ERROR, ValidityMemberAbsentError),
    (CLOSE_BEFORE_START_ERROR, ValidityCloseBeforeStartError),
    (VALID_FROM_CONFLICT_ERROR, ValidityValidFromConflictError),
)


def map_lua_error(e: BaseException) -> BaseException:
    """Return the typed exception for a :data:`SUPERSEDE_LUA` error reply.

    Package-internal, deliberately without a leading underscore: three call
    sites across three packages import it (``backends.redis.RedisBackend.
    supersede``, which raises the typed exception on the backend's direct
    path; ``supersession._save_and_close`` and
    ``recipes.provenance_journal``, which own a ``commit()``/``execute()`` and
    remap the reply it raises), which is more reach than a private name
    honestly describes. It stays out of ``popoto.__all__`` -- internal to the
    package, not to the module.

    The map lives here, not in the Redis backend, because the typed
    exceptions it instantiates live here and the backend already imports this
    module's sibling for ``SUPERSEDE_LUA``: a module-scope import in the other
    direction is a cycle (the four field modules that re-import a Lua
    constant from ``backends.redis`` would load it half-initialised), so the
    backend reaches it with a function-local import instead. Moving the three
    exception classes to a backend-neutral module is the fix, and is a
    published-API move outside #631's WS1e file set.

    **Returns** the mapped exception instance, or ``e`` itself when no token
    matches. It never raises: every call site is spelled
    ``raise map_lua_error(e) from e``, so a helper that raised internally would
    leave that expression unfinished, and one that returned ``None`` would turn
    the call site into a ``TypeError``.

    The reply is a space-separated string whose first token is a stable
    ``POPOTO_VALIDITY_*`` constant; the remaining tokens are diagnostic detail
    for humans and are never parsed.
    """
    text = str(e)
    for token, exc_type in _LUA_ERROR_MAP:
        if token in text:
            return exc_type(_LUA_ERROR_MESSAGES[token].format(detail=text.strip()))
    return e


#: Human-facing message templates, one per token. ``{detail}`` is the raw reply.
_LUA_ERROR_MESSAGES = {
    MEMBER_ABSENT_ERROR: (
        "ValidityField: a member named by this call does not exist at write "
        "time, so no interval, chain link, or pointer was written ({detail})"
    ),
    CLOSE_BEFORE_START_ERROR: (
        "ValidityField: close-at precedes the record's own valid_from " "({detail})"
    ),
    VALID_FROM_CONFLICT_ERROR: (
        "ValidityField: the asserted valid_from disagrees with the start "
        "already stored for this record; valid-time has one writer, the field "
        "value at construction ({detail})"
    ),
}


# ``SUPERSEDE_LUA`` moved to ``popoto.backends.redis`` (#631 WS0).
# It is re-imported here under its existing name so every current
# reader -- this module's own ``run_lua`` sites and the tests that
# import it from here -- keeps finding it. The script text itself is
# byte-identical.
from ..backends.redis import SUPERSEDE_LUA  # noqa: E402,F401


def _as_str(value: Any) -> str:
    """Decode a Redis reply element to ``str`` (bytes or str in, str out)."""
    return value.decode() if isinstance(value, bytes) else str(value)


class ValidityField(Field):
    """Bitemporal validity interval for each record of a model.

    Declared as a plain field::

        class Fact(Model):
            fact_id = KeyField()
            validity = ValidityField()

    The stored field value is the record's valid-from epoch (a ``float``); the
    interval state itself lives in the six derived Redis keys documented in the
    module docstring, maintained by :data:`SUPERSEDE_LUA`.

    Declaring the field is NOT enough to exclude anything (issue #693)
    -----------------------------------------------------------------
    A plain ``.save()`` routes through :meth:`on_save` in mode ``'open'``: it
    writes ``valid_from = save time`` (or a caller-declared epoch, see below)
    and ``invalid_at = +inf``, and nothing else. ``+inf`` never satisfies the
    ``invalid_at <= as_of`` test in :meth:`resolve_excluded_keys`, so **with
    respect to that branch, a save that does not declare a future
    ``valid_from`` gets a fully populated pair of interval ZSETs and an
    exclusion set that stays permanently empty.** That is only half of
    ``resolve_excluded_keys``, though: its other, independent branch excludes
    on ``valid_from > as_of``, and :meth:`on_save` uses ``field_value`` as the
    valid-from epoch whenever it is numeric. So a plain ``.save()`` that
    declares a future ``valid_from`` *is* excluded, until that moment arrives
    — the one thing a save without a producer can exclude. A save also never
    registers a record as the incumbent for any identity — the
    ``{prefix}:open:{digest}`` pointer is written by :data:`SUPERSEDE_LUA`
    itself, so a later
    ``SupersessionProtocol.supersede(successor, identity_key=...)`` finds no
    incumbent, closes nothing, and returns ``None``.

    Closing an interval requires an explicit producer. Today there are four:

    - :meth:`SupersessionProtocol.supersede` /
      :meth:`~popoto.fields.supersession.SupersessionProtocol.save_and_supersede`
      — use one of these for *every* identity-bearing write, including the
      first claim about an identity, or the second claim will close nothing.
    - :meth:`~popoto.fields.supersession.SupersessionProtocol.invalidate` /
      :meth:`~popoto.fields.supersession.SupersessionProtocol.save_and_invalidate`
      — the identity-free counterpart of the pair above.
    - ``ProvenanceJournal``.
    - ``ObservationProtocol.on_context_used`` with the ``"contradicted"``
      outcome *and* ``instance._superseded_by`` set — see
      ``_apply_supersession``. This is still an explicit application call, not
      an inference.

    The empty-set-versus-``None`` distinction is real but invisible to an
    adopter: gating did run, and excluded nothing. Whether this should stay
    imperative is the open question on issue #693.

    Why this is NOT a ``SortedFieldMixin`` (plan D2)
    ------------------------------------------------
    This is load-bearing and must not be "improved". ``ModelOptions.add_field``
    classifies any ``SortedFieldMixin`` into ``_meta.sorted_field_names``, which
    puts the field in ``filter_for_keys_set``'s *first* loop — where a returned
    **list** becomes ``Query._sorted_field_order`` and the field can win the
    query's ordering. Validity is membership, not priority: it must never order
    results. As a plain ``Field`` it lands in the second loop and returns a
    ``set`` (hence :meth:`filter_query`'s ``set`` return type — a list there
    would silently reintroduce the ordering bug). It also stays out of the
    reindex/migration loops that iterate ``sorted_field_names``.

    Query params (see :meth:`get_filter_query_params`):
        - ``{field}__current=True``  — records valid right now
        - ``{field}__current=False`` — the complement (closed / not-yet-started)
        - ``{field}__as_of=t``       — records valid at epoch ``t``

    These are *deliberate* queries: they consume a filter param and therefore
    disable sorted-range limit pushdown. That is exactly why the default
    retrieval path gates on validity server-side instead of via a filter kwarg.
    """

    # Export/import (issue #580 review blocker, PR #582): no validity byte
    # lives in the model hash -- the interval and chain state live entirely
    # in the six derived keys documented in the module docstring. A plain
    # re-save's ``on_save`` opens a *fresh* interval in mode="open" and has
    # no way to know about a prior close time or supersession chain, so the
    # ``Field`` default of ``roundtrip_policy = "rebuild"`` is a false claim
    # here: a transfer export/import round trip would silently reopen every
    # superseded record, because gating is subtractive (see the warning on
    # :meth:`resolve_valid_keys`) and a record with no interval entry is
    # fully retrievable. "carry" restores all six derived keys explicitly --
    # interval scores, chain links, and the identity-scoped open-claim
    # pointers -- after ``save()`` has already run.
    roundtrip_policy: str = "carry"

    #: Sentinel written into exported ``invalid_at`` in place of Python's
    #: ``float('inf')``. ``to_jsonable`` (transfer/format.py) passes floats
    #: through unchanged, and ``json.dumps`` would then emit the bare literal
    #: ``Infinity`` -- a token Python's own ``json.loads`` accepts back
    #: (so it *would* round-trip end-to-end) but which is not valid JSON per
    #: spec and would break any non-Python consumer of the export. A plain
    #: string sentinel keeps every exported line spec-valid JSON, consistent
    #: with the rest of the transfer format's convention of tagging
    #: non-primitive values explicitly rather than relying on interpreter
    #: leniency.
    OPEN_SENTINEL_TOKEN = "+inf"

    @classmethod
    def find_open_pointers_for_member(
        cls, model: "ModelLike", field_name: str, member_key: str
    ) -> "list[str]":
        """Return the pointer keys under ``{prefix}:open:*`` that name ``member_key``.

        There is no record -> identity-digest reverse lookup by design (plan D1
        fixes the key count at six, and the digest is opaque: identity is
        caller-defined and hashed by ``SupersessionProtocol.identity_key``), so
        the only way to answer "which identities currently claim this record as
        their open one?" is to ``SCAN`` the pointer keyspace and compare values.
        Both callers are admin/rare paths — :meth:`on_delete` and
        :meth:`export_state` — never save or read.

        A record may legitimately match zero pointers: it was never superseded
        on an identity, or it has already been closed and the pointer moved on.

        Since #631 WS1e the one in-slice caller, :meth:`on_delete`, no longer
        comes here: the backend's ``drop_validity`` performs this scan itself
        (WS0 deviation 3). What remains is the transfer export path.
        """
        prefix = cls.get_prefix_db_key(model, field_name).redis_key
        # native(): transfer export (plan non-goal ``transfer/``). The SCAN
        # loop below is ``redis_db.scan_keys`` inlined -- cursor scan, COUNT
        # 1000, all keys collected before the first GET -- so the wire shape
        # is unchanged.
        client = get_backend().native()
        pointer_keys: "list[Any]" = []
        cursor = 0
        while True:
            cursor, batch = client.scan(
                cursor=cursor, match=f"{prefix}:open:*", count=1000
            )
            pointer_keys.extend(batch)
            if cursor == 0:
                break
        matched = []
        for pointer_key in pointer_keys:
            pointer_key = _as_str(pointer_key)
            current = client.get(pointer_key)
            if current is not None and _as_str(current) == member_key:
                matched.append(pointer_key)
        return matched

    @classmethod
    def export_state(  # type: ignore[override]
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: Any,
        **kwargs: Any,
    ) -> "Optional[dict[str, Any]]":
        """Export this record's interval scores, chain links, and open claims.

        Reads the member's score from each of the three interval ZSETs, its
        own forward/reverse chain-link entries, and every identity-scoped
        open-claim pointer (``{prefix}:open:{digest}``, see
        :meth:`get_open_pointer_key`) that currently names this record.

        All six derived keys are therefore carried. The pointer matters more
        than its "per-identity, not per-record" shape suggests:
        ``SupersessionProtocol.supersede(new, identity_key=...)`` resolves the
        incumbent *solely* through it (``old_member=''``, and
        :data:`SUPERSEDE_LUA` ``GET``s ``KEYS[4]``). A round trip that dropped
        the pointer would leave the next supersession on that identity closing
        nothing and writing no chain link, while still repointing the pointer
        at the newcomer -- orphaning the incumbent open forever. Gating is
        subtractive (see :meth:`resolve_valid_keys`), so that orphan stays
        fully retrievable: the same silent resurrection ``roundtrip_policy =
        "carry"`` exists to prevent, deferred by one supersession.

        Capturing the pointers costs a ``SCAN`` of ``{prefix}:open:*`` per
        record (see :meth:`find_open_pointers_for_member`) because the digest
        is opaque and there is no record -> digest reverse lookup. Export is
        an admin-path operation and that cost is accepted deliberately, on
        the same reasoning as :meth:`on_delete`'s scan.

        Returns:
            ``{"valid_from": float, "invalid_at": float | "+inf",
            "ingested_at": float, "chain_fwd": str | None,
            "chain_rev": str | None, "open_pointers": list[str]}``, or
            ``None`` when this instance has no interval entry at all (never
            saved through this field, or already deleted). ``open_pointers``
            holds identity digests, not full Redis keys, so the destination
            rebuilds them under its own prefix; it is commonly empty -- a
            record that was never superseded on an identity, or one already
            closed, legitimately owns no pointer, and its interval and chain
            state are still exported.
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ValidityField):
            return None

        member_key = model_instance.db_key.redis_key
        keys = cls.get_all_keys(model_instance, field_name)

        # native(): transfer export (plan non-goal ``transfer/``). The three
        # interval scores and both chain links are read off the raw client,
        # exactly as before #631; the protocol's ``interval_of`` could serve
        # two of the five reads but the path is out of the slice as a whole.
        client = get_backend().native()
        valid_from: Optional[float] = client.zscore(keys["valid_from"], member_key)
        invalid_at: Optional[float] = client.zscore(keys["invalid_at"], member_key)
        ingested_at: Optional[float] = client.zscore(keys["ingested_at"], member_key)
        if valid_from is None and invalid_at is None and ingested_at is None:
            return None

        fwd = client.hget(keys["chain_fwd"], member_key)
        rev = client.hget(keys["chain_rev"], member_key)

        invalid_at_out: Union[float, str]
        if invalid_at is None or float(invalid_at) == Defaults.VALIDITY_OPEN_SENTINEL:
            invalid_at_out = cls.OPEN_SENTINEL_TOKEN
        else:
            invalid_at_out = float(invalid_at)

        # Pointer keys are ``{prefix}:open:{digest}``; the digest is the last
        # segment (16 hex chars from blake2b, never contains a separator), so
        # a single rsplit recovers it without re-deriving the prefix.
        open_pointers = [
            pointer_key.rsplit(":", 1)[-1]
            for pointer_key in cls.find_open_pointers_for_member(
                model_instance, field_name, member_key
            )
        ]

        return {
            "valid_from": float(valid_from) if valid_from is not None else None,
            "invalid_at": invalid_at_out,
            "ingested_at": float(ingested_at) if ingested_at is not None else None,
            "chain_fwd": _as_str(fwd) if fwd is not None else None,
            "chain_rev": _as_str(rev) if rev is not None else None,
            # Plain list of strings: spec-valid JSON with no special tokens
            # needed, unlike ``invalid_at``'s :data:`OPEN_SENTINEL_TOKEN`.
            "open_pointers": open_pointers,
        }

    @classmethod
    def import_state(
        cls,
        model_instance: "Model",
        field_name: str,
        state: Any,
        **kwargs: Any,
    ) -> None:
        """Restore this record's interval scores and chain links after import.

        Written with plain ``ZADD``/``HSET`` -- deliberately NOT through
        :data:`SUPERSEDE_LUA` and NOT with the ``NX`` guards ``on_save``
        uses. The transfer driver calls ``import_state`` *after* ``save()``,
        so ``on_save`` has already run: it seeds ``valid_from`` /
        ``ingested_at`` / ``invalid_at`` via ``ZADD NX`` in mode="open",
        i.e. a fresh, open interval with no close time and no chain. An
        ``NX`` write here would be a silent no-op against those
        already-present scores, discarding the carried close time and chain
        -- exactly the resurrection bug this field exists to prevent.
        Plain ``ZADD``/``HSET`` overwrite instead.

        Carried open-claim pointers are restored with a plain ``SET``, for
        the same reason: ``on_save`` cannot write them at all on this path
        (it calls :meth:`execute_supersede` with no ``identity_digest``, so
        ``KEYS[4]`` is ``''`` and :data:`SUPERSEDE_LUA` skips the ``SET``),
        and an unconditional ``SET`` is what makes the identity's next
        supersession resolve this record as the incumbent (see
        :meth:`export_state` for why dropping it resurrects records). Only
        the digests actually captured at export are written, so a record
        that owned no open claim still writes none.

        Chain links are restored independently of import order: ``HSET``
        does not require the counterpart record to already exist, and
        ``_walk_links``-style chain traversal already treats a link to a
        record with no interval entry as a chain end (see
        :meth:`on_delete`'s "Known limitation" note), so a partially
        imported chain converges once both sides have landed.
        """
        if not state:
            return None

        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ValidityField):
            return None

        member_key = model_instance.db_key.redis_key
        keys = cls.get_all_keys(model_instance, field_name)

        # native(): transfer import (plan non-goal ``transfer/``). Plain
        # ``ZADD``/``HSET``/``SET`` with no ``NX`` guard, for the reasons in
        # the docstring; the protocol has no unguarded open-pointer write
        # (``supersede`` owns that key), so the whole path stays raw.
        client = get_backend().native()

        valid_from = state.get("valid_from")
        if valid_from is not None:
            client.zadd(keys["valid_from"], {member_key: float(valid_from)})

        invalid_at = state.get("invalid_at")
        if invalid_at is not None:
            score = (
                Defaults.VALIDITY_OPEN_SENTINEL
                if invalid_at == cls.OPEN_SENTINEL_TOKEN
                else float(invalid_at)
            )
            client.zadd(keys["invalid_at"], {member_key: score})

        ingested_at = state.get("ingested_at")
        if ingested_at is not None:
            client.zadd(keys["ingested_at"], {member_key: float(ingested_at)})

        chain_fwd = state.get("chain_fwd")
        if chain_fwd:
            client.hset(keys["chain_fwd"], member_key, chain_fwd)

        chain_rev = state.get("chain_rev")
        if chain_rev:
            client.hset(keys["chain_rev"], member_key, chain_rev)

        for digest in state.get("open_pointers") or []:
            client.set(
                cls.get_open_pointer_key(model_instance, field_name, str(digest)),
                member_key,
            )

        return None

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the field.

        Args:
            **kwargs: Standard :class:`~popoto.fields.field.Field` options.
                ``type`` defaults to ``float`` (the stored valid-from epoch) and
                ``null`` to ``True`` — a record may be saved without an explicit
                valid-from, in which case save time is used.
        """
        kwargs.setdefault("type", float)
        kwargs.setdefault("null", True)
        super().__init__(**kwargs)

    # ------------------------------------------------------------------
    # Key helpers (plan D1)
    # ------------------------------------------------------------------

    @classmethod
    def get_prefix_db_key(cls, model: "ModelLike", field_name: str) -> DB_key:
        """Return the ``$ValidityF:{Model}:{field}`` prefix all six keys extend."""
        # ``Field.get_special_use_field_db_key`` annotates an instance but reads
        # only ``_meta``, which a model class carries too — hence ``ModelLike``
        # here and the narrowing cast at the boundary.
        return cls.get_special_use_field_db_key(cast("Model", model), field_name)

    @classmethod
    def _model_prefix(cls, model: "ModelLike") -> str:
        """Return the opaque ``$ValidityF:{Model}`` namespace the backend takes.

        The backend protocol's validity group (``supersede``, ``drop_validity``,
        ``open_pointer``) takes this and the field name separately and appends
        ``:{field}:{suffix}`` itself (WS0 deviation 10). It is
        :meth:`get_prefix_db_key` minus its last segment -- built by the same
        helper with zero field names (``DB_key(field_class_key,
        db_class_key)``) rather than by string surgery, so the two stay
        byte-equal under ``DB_key.clean``: a field name is a Python identifier
        and the five suffixes contain nothing ``clean`` escapes, so
        ``f"{prefix}:{field}:{suffix}"`` is exactly
        ``DB_key(get_prefix_db_key(...), suffix).redis_key``.
        ``tests/test_validity_routes_through_backend.py`` asserts that equality
        for all six keys.
        """
        return cls.get_special_use_field_db_key(cast("Model", model)).redis_key

    @classmethod
    def get_valid_from_key(cls, model: "ModelLike", field_name: str) -> str:
        """Redis key of the ``valid_from`` ZSET (member -> valid-from epoch)."""
        return DB_key(cls.get_prefix_db_key(model, field_name), "valid_from").redis_key

    @classmethod
    def get_invalid_at_key(cls, model: "ModelLike", field_name: str) -> str:
        """Redis key of the ``invalid_at`` ZSET (member -> close epoch, ``+inf`` = open)."""
        return DB_key(cls.get_prefix_db_key(model, field_name), "invalid_at").redis_key

    @classmethod
    def get_ingested_at_key(cls, model: "ModelLike", field_name: str) -> str:
        """Redis key of the ``ingested_at`` ZSET (member -> transaction-time epoch)."""
        return DB_key(cls.get_prefix_db_key(model, field_name), "ingested_at").redis_key

    @classmethod
    def get_chain_fwd_key(cls, model: "ModelLike", field_name: str) -> str:
        """Redis key of the forward chain HASH (old redis_key -> superseding key)."""
        return DB_key(
            cls.get_prefix_db_key(model, field_name), "chain", "fwd"
        ).redis_key

    @classmethod
    def get_chain_rev_key(cls, model: "ModelLike", field_name: str) -> str:
        """Redis key of the reverse chain HASH (new redis_key -> superseded key)."""
        return DB_key(
            cls.get_prefix_db_key(model, field_name), "chain", "rev"
        ).redis_key

    @classmethod
    def get_open_pointer_key(
        cls, model: "ModelLike", field_name: str, identity_digest: str
    ) -> str:
        """Redis key of the open-claim pointer STRING for one identity digest.

        Args:
            model: The model class (or instance) owning the field.
            field_name: Name of the ``ValidityField`` on that model.
            identity_digest: Opaque, already-normalized identity token — see
                ``SupersessionProtocol.identity_key`` (plan D7), which hashes the
                caller's ``(subject, predicate)`` into 16 hex characters so raw
                user text can never reach the keyspace.
        """
        return DB_key(
            cls.get_prefix_db_key(model, field_name), "open", identity_digest
        ).redis_key

    @classmethod
    def get_interval_keys(
        cls, model: "ModelLike", field_name: str
    ) -> "tuple[str, str]":
        """Return ``(valid_from_key, invalid_at_key)`` for a model/field pair.

        This is the stable seam every other validity consumer builds on: the
        decay-Lua gate passes these two as extra ``KEYS``, the composite-score
        mask ``ZRANGESTORE``s from them, and the assembler's
        ``_resolve_excluded_keys`` reads them directly. Returned in the order
        ``(valid_from, invalid_at)``; an as-of-``t`` member satisfies
        ``valid_from <= t AND invalid_at > t``.

        Args:
            model: The model class (or instance) owning the field.
            field_name: Name of the ``ValidityField`` on that model.

        Returns:
            A 2-tuple of Redis key strings.
        """
        return (
            cls.get_valid_from_key(model, field_name),
            cls.get_invalid_at_key(model, field_name),
        )

    @classmethod
    def get_all_keys(cls, model: "ModelLike", field_name: str) -> "dict[str, str]":
        """Return the five non-identity keys as a name -> Redis key mapping.

        Keys: ``valid_from``, ``invalid_at``, ``ingested_at``, ``chain_fwd``,
        ``chain_rev``. The sixth key (the per-identity open pointer) is excluded
        because it is parameterized by identity digest — use
        :meth:`get_open_pointer_key` for that one.
        """
        return {
            "valid_from": cls.get_valid_from_key(model, field_name),
            "invalid_at": cls.get_invalid_at_key(model, field_name),
            "ingested_at": cls.get_ingested_at_key(model, field_name),
            "chain_fwd": cls.get_chain_fwd_key(model, field_name),
            "chain_rev": cls.get_chain_rev_key(model, field_name),
        }

    # ------------------------------------------------------------------
    # Membership resolution
    # ------------------------------------------------------------------

    @classmethod
    def resolve_valid_keys(
        cls,
        model: "ModelLike",
        field_name: str,
        as_of: Optional[float] = None,
    ) -> "set[str]":
        """Return the record keys whose interval covers ``as_of``.

        Two read-only ``ZRANGEBYSCORE``s intersected:
        ``valid_from <= as_of`` AND ``invalid_at > as_of``.

        .. warning::

           This is a **whitelist**: a record with no entry in either ZSET is
           absent from the result. That makes it wrong for retrieval gating,
           and it is deliberately **not** used by any retrieval path. All three
           gating layers are *subtractive* — they exclude records that are
           positively known to be closed or not-yet-started, and leave
           unmanaged records (no interval at all) fully visible. Using this
           method to gate retrieval would silently hide every record that
           predates the field's adoption on a model. The assembler uses
           ``ContextAssembler._resolve_excluded_keys`` instead, and the query
           layer uses ``QueryBuilder._apply_validity_mask``.

        Retained as the public, deliberate-query helper for callers that
        genuinely want "which records positively claim validity at ``t``" —
        e.g. audit and provenance tooling, not context assembly.

        Args:
            model: The model class (or instance) owning the field.
            field_name: Name of the ``ValidityField`` on that model.
            as_of: Epoch seconds to evaluate at. ``None`` means "now".

        Returns:
            ``set[str]`` of decoded Redis keys. Decoded — not bytes — because
            every consumer compares against ``record.db_key.redis_key``, which is
            a ``str``; the ``bytes`` form is confined to :meth:`filter_query`,
            where the query layer intersects raw index replies.

        Note:
            This is a point-in-time snapshot of a live store. A supersession
            landing after the read is not reflected until the next call — the
            same accepted property tag scoping already has (plan Race 3).
        """
        t = time.time() if as_of is None else float(as_of)
        valid_from_key, invalid_at_key = cls.get_interval_keys(model, field_name)
        return get_backend().interval_members(
            valid_from_key, invalid_at_key, t, select="valid"
        )

    @classmethod
    def resolve_excluded_keys(
        cls,
        model: "ModelLike",
        field_name: str,
        as_of: Optional[float] = None,
    ) -> "set[str]":
        """Return the record keys to DROP as of ``as_of`` (#648).

        The subtractive counterpart to :meth:`resolve_valid_keys`, and the one
        every retrieval path actually wants. Two read-only ``ZRANGEBYSCORE``s,
        unioned: ``invalid_at <= as_of`` (already closed) and
        ``valid_from > as_of`` (not yet started) — the exact mirror of the two
        ranges :meth:`resolve_valid_keys` intersects.

        .. warning::

           **This returns an exclusion set, not a whitelist, and that is
           deliberate.** A record with no entry in either interval ZSET is
           *unmanaged* and stays fully retrievable. Resolving the valid set
           instead — via :meth:`resolve_valid_keys`, whose own warning is the
           other half of this one — would silently hide every record that
           predates the day a ``ValidityField`` was added to an existing model,
           since none of those has an interval until it is next saved. That is a
           data-visibility regression, not a stricter gate. Do not "simplify"
           this into a valid-key intersection.

           This method lives beside ``resolve_valid_keys`` precisely so the
           warning and the trap it warns about are read together. It used to
           live in ``ContextAssembler._resolve_excluded_keys``, a different file
           from the method it forbids.

        The exclusive lower bound on ``valid_from`` is spelled ``f"({t}"``,
        while the ``invalid_at`` upper bound is inclusive. Redis renders the
        ``+inf`` open sentinel such that an open record never matches
        ``<= as_of``. Both bound conventions are this method's business, not a
        caller's.

        Callers own the *policy* questions this does not answer: whether the
        model declares a ``ValidityField`` at all, and whether the
        ``Defaults.VALIDITY_GATING_ENABLED`` kill switch is on (which must be
        read at call time, never captured at import, so a deploy-level switch
        takes effect for adopters who cannot edit model code).

        Args:
            model: The model class (or instance) owning the field.
            field_name: Name of the ``ValidityField`` on that model.
            as_of: Epoch seconds to evaluate membership at. ``None`` = now.

        Returns:
            ``set[str]`` of decoded record keys to drop. An empty set means
            gating ran and excluded nothing — which is **not** the same as a
            caller's ``None`` for "gating did not run at all".

        Note:
            A point-in-time snapshot of a live store. A supersession landing
            after the read is not reflected until the next call.
        """
        t = time.time() if as_of is None else float(as_of)
        valid_from_key, invalid_at_key = cls.get_interval_keys(model, field_name)
        # The backend owns both bound conventions: ``invalid_at <= t`` (already
        # closed; the +inf open sentinel never matches) read before
        # ``valid_from > t`` (not yet started), the order the assembler
        # established. Absence from either index means *included*.
        return get_backend().interval_members(
            valid_from_key, invalid_at_key, t, select="excluded"
        )

    @classmethod
    def is_valid_at(
        cls,
        model: "ModelLike",
        field_name: str,
        member_key: str,
        as_of: Optional[float] = None,
    ) -> bool:
        """Return whether one record's interval covers ``as_of``.

        Two ``ZSCORE``s rather than two range reads — the single-member form of
        :meth:`resolve_valid_keys`. A member absent from either ZSET is not valid
        (it has no interval).
        """
        t = time.time() if as_of is None else float(as_of)
        valid_from_key, invalid_at_key = cls.get_interval_keys(model, field_name)
        # The member may be a ``bytes`` key from ``Query.keys()``; the backend
        # takes ``str`` (WS1f).
        start, close = get_backend().interval_of(
            valid_from_key, invalid_at_key, as_key_str(member_key)
        )
        if start is None or close is None:
            return False
        if float(close) == Defaults.VALIDITY_OPEN_SENTINEL:
            return float(start) <= t
        return float(start) <= t < float(close)

    # ------------------------------------------------------------------
    # Script execution
    # ------------------------------------------------------------------

    @classmethod
    def execute_supersede(
        cls,
        model: "ModelLike",
        field_name: str,
        *,
        new_member: str = "",
        mode: str = "open",
        now: Optional[float] = None,
        valid_from: Optional[float] = None,
        ingested_at: Optional[float] = None,
        close_at: Optional[float] = None,
        old_member: str = "",
        identity_digest: str = "",
        assert_valid_from: bool = False,
        pipeline: Optional[UnitOfWork] = None,
    ) -> Any:
        """Run one atomic supersede against this model/field's six keys.

        The single seam through which every validity mutation flows —
        ``ValidityField.on_save`` and ``SupersessionProtocol`` both call it, so
        there is exactly one place that turns the field layer's vocabulary into
        the backend's ``supersede`` call. On Redis that is :data:`SUPERSEDE_LUA`;
        the KEYS/ARGV order is the Redis backend's business now (#631 WS1e).

        Args:
            model: The model class (or instance) owning the field.
            field_name: Name of the ``ValidityField`` on that model.
            new_member: Redis key of the record whose interval opens. Empty for
                a pure ``invalidate``.
            mode: ``'open'`` (a save: open the newcomer, close nothing),
                ``'supersede'`` (close the incumbent and chain it to the
                newcomer), or ``'invalidate'`` (close only).
            now: Epoch seconds to use as the script's clock. Defaults to
                ``time.time()``. Passing one clock for a batch keeps intervals
                consistent across a multi-record write.
            valid_from: Valid-from epoch for ``new_member``. Defaults to ``now``.
            ingested_at: Transaction-time epoch for ``new_member``. Defaults to
                ``now``.
            close_at: Explicit close epoch for the incumbent. Defaults to ``now``.
            old_member: Explicit incumbent key, bypassing identity resolution.
            identity_digest: Identity digest naming the open-claim pointer. When
                empty, ``KEYS[4]`` is passed as ``''`` and the script neither
                reads nor repoints a pointer.
            assert_valid_from: When ``True``, ``valid_from`` is a caller
                *assertion* about ``new_member``'s start rather than a default,
                and a disagreement with the already-stored start raises
                :class:`ValidityValidFromConflictError` instead of losing
                silently to the script's ``ZADD NX`` (plan D3). ``False`` — the
                default, and what ``SupersessionProtocol`` and
                ``ProvenanceJournal`` pass — preserves today's behavior for every
                existing caller: their ``at=`` is a *close-time* assertion about
                the incumbent, not a start-time assertion about the successor.
            pipeline: Optional external unit of work (on Redis, a pipeline).
                When given, the supersede is queued onto it (following
                ``tag_field.py``'s threading shape) and the pipeline is
                returned; the closed-member result is only available at
                ``commit()``/``execute()`` time.

        Returns:
            The closed member key as ``str``, or ``None`` if nothing was closed —
            or the ``pipeline`` when one was supplied.

        Raises:
            ValueError: If ``mode`` is not one of :data:`VALID_MODES`, or if the
                client-side pre-check finds ``close_at`` before ``valid_from``.
            ValidityMemberAbsentError: If ``new_member``, or an explicitly-named
                ``old_member``, does not exist at the instant of the write.
            ValidityCloseBeforeStartError: If ``close_at`` precedes the
                incumbent's stored ``valid_from``.
            ValidityValidFromConflictError: If ``assert_valid_from`` is set and
                ``valid_from`` disagrees with the stored start.

        Note:
            The typed-exception remap is the backend's, on its **direct**
            branch only. On a caller-supplied pipeline redis-py raises during
            ``pipe.execute()`` result parsing, long after this method returned,
            so the caller sees a raw ``redis.exceptions.ResponseError``. Use
            :meth:`SupersessionProtocol.save_and_supersede`, which owns its
            ``commit()``, to get a typed error in pipeline shape.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"ValidityField mode must be one of {sorted(VALID_MODES)}, got {mode!r}"
            )
        clock = time.time() if now is None else float(now)
        if close_at is not None and valid_from is not None and mode != "open":
            # Cheap client-side pre-check for the direct-invalidation form, where
            # the caller already knows both ends. The authoritative check lives in
            # the backend (the stored valid_from is the one that matters).
            if float(close_at) < float(valid_from):
                raise ValueError(
                    "ValidityField: close-at "
                    f"({close_at}) precedes valid_from ({valid_from})"
                )

        # WS0 deviation 2: ``now`` is the script's ARGV[2] clock and travels
        # explicitly. Deviation 10: the backend takes the ``$ValidityF:<Model>``
        # namespace and the field name and derives the six keys itself. An empty
        # ``identity_digest`` is ``pointer_digest=None`` -- no pointer read, no
        # repoint. Architect decision 1: ``pipeline`` is the unit of work and
        # is tested for presence, never for type.
        result = get_backend().supersede(
            cls._model_prefix(model),
            field_name,
            mode=mode,
            new_member=new_member or "",
            old_member=old_member or "",
            now=clock,
            valid_from=None if valid_from is None else float(valid_from),
            ingested_at=None if ingested_at is None else float(ingested_at),
            close_at=None if close_at is None else float(close_at),
            assert_valid_from=assert_valid_from,
            pointer_digest=identity_digest or None,
            uow=pipeline,
        )
        if pipeline is not None:
            return pipeline
        return result

    # ------------------------------------------------------------------
    # TTL interaction (plan D9)
    # ------------------------------------------------------------------

    @classmethod
    def warn_if_ttl(cls, model: "ModelLike", field_name: str) -> bool:
        """Warn once when a ``ValidityField`` model also declares a TTL.

        A TTL truncates supersession chains and silently breaks ``as_of``
        correctness: the expired record vanishes while its chain links and
        interval entries remain. This warns rather than raises — refusing would
        break adopters who legitimately want bounded history (plan D9).

        Returns:
            ``True`` if a warning was emitted on this call.
        """
        meta = getattr(model, "_meta", None)
        if meta is None or getattr(meta, "ttl", None) is None:
            return False
        marker = (getattr(meta, "model_name", str(model)), field_name)
        if marker in _TTL_WARNED:
            return False
        _TTL_WARNED.add(marker)
        logger.warning(
            "%s.%s is a ValidityField on a model with Meta.ttl=%s. TTL expiry "
            "truncates supersession chains and breaks as_of reconstruction: the "
            "record disappears while its interval and chain links remain. Drop "
            "the TTL, or accept bounded history.",
            marker[0],
            field_name,
            meta.ttl,
        )
        return True

    # ------------------------------------------------------------------
    # Model hooks
    # ------------------------------------------------------------------

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: Any,
        # ``Field.on_save`` (field.py, outside the WS1e file set) still spells
        # the redis ``Pipeline``; ``Any`` keeps this override LSP-compatible
        # while the runtime shape is the backend's ``UnitOfWork``.
        pipeline: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """Open (or re-affirm) this record's validity interval.

        Routes through :meth:`execute_supersede` -- the backend's ``supersede``
        in mode ``'open'`` (:data:`SUPERSEDE_LUA` on Redis) -- rather than
        issuing a bare ``ZADD``, which is what makes the save path safe against
        plan Race 2: the script's ``ZSCORE``/``NX`` guards mean a save that
        interleaves with a concurrent supersession can never resurrect an
        already-closed record, and a re-save never shifts an existing interval's
        start or ingest time.

        ``field_value`` is used as the valid-from epoch when it is numeric;
        otherwise save time is used. The write is idempotent, so repeated saves
        of an open record are no-ops on the index.

        Args:
            model_instance: The instance being saved.
            field_name: Name of this field on the model.
            field_value: The declared valid-from epoch, or ``None``.
            pipeline: Optional external pipeline; the EVAL is queued onto it.
            **kwargs: Accepted for forward compatibility.

        Returns:
            The ``pipeline`` if one was provided, else the script result.
        """
        cls.warn_if_ttl(model_instance, field_name)
        now = time.time()
        declared = False
        try:
            if field_value is not None:
                valid_from = float(field_value)
                declared = True
            else:
                valid_from = now
        except (TypeError, ValueError):
            valid_from = now
        return cls.execute_supersede(
            model_instance,
            field_name,
            new_member=model_instance.db_key.redis_key,
            mode="open",
            now=now,
            valid_from=valid_from,
            ingested_at=now,
            # Plan D3: a declared field value IS the single authoritative writer
            # of valid-time, so a re-save that declares a different start is the
            # reporter's bug and must be loud. A defaulted save asserts nothing
            # and keeps NX idempotence.
            assert_valid_from=declared,
            pipeline=pipeline,
        )

    @classmethod
    def pre_save_validate(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: Any,
        **kwargs: Any,
    ) -> None:
        """Refuse a save that declares a ``valid_from`` the index disagrees with.

        Runs from the single pre-split dispatch site in ``Model.save()``, before
        *any* write is issued or queued — which is the point. Raising from
        :meth:`on_save` would be too late on a model that also declares
        ``IndexedFieldMixin`` fields: those commit their hash values and index
        entries eagerly, against live Redis, before ``ValidityField.on_save``
        ever runs (plan D5 half 1, the same treatment #476 gave the
        unique-conflict window).

        A cheap client-side pre-check for a good error message; the authoritative
        comparison is in :data:`SUPERSEDE_LUA` under ``ARGV[8]``, evaluated
        atomically (plan Race 4). A racing pre-check can only produce a false
        negative, which the script then catches.
        """
        if field_value is None:
            return  # defaulted: no assertion, nothing to conflict with
        try:
            declared = float(field_value)
        except (TypeError, ValueError):
            return  # on_save falls back to the save clock; not an assertion
        try:
            member_key = model_instance.db_key.redis_key
        except (TypeError, ValueError):
            return
        if not member_key:
            return
        stored, _ = get_backend().interval_of(
            *cls.get_interval_keys(model_instance, field_name), member_key
        )
        if stored is not None and float(stored) != declared:
            raise ValidityValidFromConflictError(
                f"ValidityField: {model_instance.__class__.__name__}.{field_name} "
                f"declares valid_from={declared!r} for {member_key}, but the "
                f"index already holds {float(stored)!r}. Valid-time has one "
                "writer -- the field value at construction. Adopt the stored "
                "value with ValidityField.get_valid_from(...), or overwrite the "
                "index with a plain ZADD (no NX), then save again."
            )

    @classmethod
    def get_valid_from(
        cls,
        model: "ModelLike",
        field_name: str,
        member_key: Optional[str] = None,
    ) -> Optional[float]:
        """Return the record's *effective* valid-from — the index score.

        ``instance.validity`` is the **declared** value: ``None`` there means
        "not declared, defaulted to the save clock". This returns what the
        ``valid_from`` index actually holds, which is what every ``as_of`` query
        answers against (plan D5 half 2). The two differ legitimately, and
        reading this is how an operator reconciles a record whose hash and index
        already disagree.

        Args:
            model: The model class, or a live instance.
            field_name: Name of the ``ValidityField``.
            member_key: The record's Redis key. Defaults to ``model``'s own key
                when an instance was passed.

        Returns:
            The stored valid-from epoch, or ``None`` when the member has no
            entry in the index.
        """
        if member_key is None:
            try:
                member_key = model.db_key.redis_key  # type: ignore[union-attr]
            except (AttributeError, TypeError, ValueError) as e:
                raise ValueError(
                    "ValidityField.get_valid_from: pass member_key when the "
                    "first argument is a model class rather than an instance"
                ) from e
        score, _ = get_backend().interval_of(
            *cls.get_interval_keys(model, field_name), as_key_str(member_key)
        )
        return None if score is None else float(score)

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: Any,
        pipeline: Optional[UnitOfWork] = None,
        **kwargs: Any,
    ) -> Any:
        """Remove every trace of this record from the validity keyspace.

        One backend call, ``drop_validity``: on Redis, ``ZREM`` from the three
        interval ZSETs, ``HDEL`` from both chain HASHes, and ``DEL`` of any
        open-claim pointer still aimed at the record. Records are normally
        *closed*, not deleted — this hook exists so an explicit ``delete()``
        (or a key migration, which calls it with ``saved_redis_key``) does not
        leave orphaned index members behind.

        Pointer cleanup scans ``{prefix}:open:*`` because the pointer keyspace is
        keyed by identity digest, not by member — the reverse lookup does not
        exist by design (plan D1 fixes the key count at six), which is why the
        backend takes the member rather than a digest (WS0 deviation 3) and
        does the scan itself. This is a delete-time-only cost on a path that
        is rare relative to save and read; adding a seventh per-record
        back-pointer key to avoid it was rejected as the worse trade.

        Known limitation: the deleted key is removed from both chain HASHes as a
        *field*, but a neighbor's link may still name it as a *value* (``fwd``
        holds ``old -> deleted``). Scrubbing the value side would mean an
        ``HGETALL`` of the whole chain on every delete. Chain traversal treats a
        link to a record with no interval as a chain end, which is the correct
        reading of a hard-deleted link.

        Returns:
            The ``pipeline`` if one was provided, else the pointer-cleanup result.
        """
        member = kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
        result = get_backend().drop_validity(
            cls._model_prefix(model_instance), field_name, member, uow=pipeline
        )
        if pipeline is not None:
            return pipeline
        return result

    # ------------------------------------------------------------------
    # Query integration
    # ------------------------------------------------------------------

    def get_filter_query_params(self, field_name: str) -> "set[str]":
        """Declare the two deliberate validity lookups.

        ``{field}__as_of=t`` (records valid at epoch ``t``) and
        ``{field}__current=True|False`` (valid now / the complement).
        Unioned with ``super()``'s set per the base-class contract.
        """
        return super().get_filter_query_params(field_name) | {
            f"{field_name}__as_of",
            f"{field_name}__current",
        }

    @classmethod
    def filter_query(
        cls,
        model: "Model",
        field_name: str,
        **query_params: Any,
    ) -> "set[Any]":
        """Resolve validity lookups to a ``set`` of matching Redis keys.

        Two ``ZRANGEBYSCORE`` reads intersected — ``valid_from <= t`` AND
        ``invalid_at > t``. ``__current=True`` evaluates at now;
        ``__current=False`` returns the complement (every member with an
        interval that does *not* cover now: closed, or not yet started). Multiple
        params AND-intersect, consistent with the rest of ``filter_for_keys_set``.

        Returns a ``set`` and never a ``list``: the query layer turns a list
        return into ``Query._sorted_field_order``, and validity must never order
        results (plan D2).

        Args:
            model: The model class being queried.
            field_name: Name of this field on the model.
            **query_params: ``{field}__as_of`` and/or ``{field}__current``.

        Returns:
            ``set`` of Redis keys (``bytes``, matching the other index fields'
            reply type) for records matching every supplied param.

        Raises:
            ValueError: If ``__current`` is not a bool or ``__as_of`` is not a
                finite number.
        """
        valid_from_key, invalid_at_key = cls.get_interval_keys(model, field_name)
        results = []

        for query_param, query_value in query_params.items():
            if query_param == f"{field_name}__current":
                if not isinstance(query_value, bool):
                    raise ValueError(
                        f"{query_param} filter must be True or False, "
                        f"got {query_value!r}"
                    )
                t = time.time()
                valid = cls._members_valid_at(valid_from_key, invalid_at_key, t)
                if query_value:
                    results.append(valid)
                else:
                    # Every member with an interval, as raw ``bytes`` to match
                    # ``valid`` (see :meth:`_members_valid_at`).
                    backend = get_backend()
                    everything = {
                        m.encode(ENCODING)
                        for m in backend.sorted_members(invalid_at_key)
                        + backend.sorted_members(valid_from_key)
                    }
                    results.append(everything - valid)

            elif query_param == f"{field_name}__as_of":
                try:
                    t = float(query_value)
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{query_param} filter must be a number of epoch seconds, "
                        f"got {query_value!r}"
                    ) from e
                results.append(cls._members_valid_at(valid_from_key, invalid_at_key, t))

        if not results:
            return set()
        matched = results[0]
        for other in results[1:]:
            matched &= other
        return matched

    @staticmethod
    def _members_valid_at(
        valid_from_key: str, invalid_at_key: str, t: float
    ) -> "set[Any]":
        """Raw (``bytes``) member set whose interval covers ``t``.

        The primitive behind :meth:`filter_query`: the backend's
        ``interval_members(select="valid")`` -- on Redis the same two
        ``ZRANGEBYSCORE`` reads as before -- re-encoded to ``bytes``. Kept
        separate from :meth:`resolve_valid_keys` because the query layer
        intersects this with the other index fields' *raw* replies
        (``set.intersection(*db_keys_sets)`` in ``filter_for_keys_set``),
        which are still ``bytes`` on this branch; a ``str`` set here would
        intersect to nothing against a ``KeyField`` filter. The protocol
        decodes to ``str`` (WS0 deviation 11), so this is the one place the
        encoding is reversed. When WS1b moves ``filter_for_keys_set`` to
        ``str``, this ``encode`` goes with it.
        """
        return {
            m.encode(ENCODING)
            for m in get_backend().interval_members(
                valid_from_key, invalid_at_key, t, select="valid"
            )
        }
