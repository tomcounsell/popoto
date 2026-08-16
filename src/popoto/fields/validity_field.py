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
  ``ZRANGEBYSCORE``/``HSET``/``HDEL``/``GET``/``SET``/``DEL``) plus Lua 5.1.
  No Redis-module commands (``BF.``/``CMS.``/``TS.``/``TOPK.``) anywhere.

Index Structure (plan D1) — six Redis keys per model/field::

    $ValidityF:Model:field:valid_from      ZSET   member -> valid-from epoch
    $ValidityF:Model:field:invalid_at      ZSET   member -> close epoch, +inf = open
    $ValidityF:Model:field:ingested_at     ZSET   member -> ingest epoch
    $ValidityF:Model:field:chain:fwd       HASH   old redis_key -> superseding key
    $ValidityF:Model:field:chain:rev       HASH   new redis_key -> superseded key
    $ValidityF:Model:field:open:{digest}   STRING identity digest -> open record

(The ``$ValidityF`` prefix is auto-derived by the ``FieldBase`` metaclass from
the class name ``ValidityField``.)

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

import redis.client

from ..models.db_key import DB_key
from ..redis_db import POPOTO_REDIS_DB, scan_keys
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

#: Models already warned about the TTL/validity interaction (plan D9). Keyed by
#: ``(model name, field name)`` so the warning fires exactly once per pair.
_TTL_WARNED: "set[tuple[str, str]]" = set()


# SUPERSEDE_LUA — atomic interval closure + chain linking + open-pointer repoint.
#
# One EVAL owns every byte of state for one logical supersession (#580, plan D4).
# There is no code path that closes an interval without also updating the gating
# index, because the `invalid_at` ZSET *is* the gating index: closing the interval
# and removing the record from retrieval are literally the same ZADD. No lock, no
# second step, therefore no window.
#
# Contract:
#   KEYS[1] = valid_from ZSET   (member = record redis_key, score = valid-from epoch)
#   KEYS[2] = invalid_at ZSET   (score = close epoch; +inf means "still open")
#   KEYS[3] = ingested_at ZSET  (score = transaction-time epoch)
#   KEYS[4] = open-identity pointer STRING (identity digest -> open record key).
#             May be '' for identity-free direct invalidation.
#   KEYS[5] = chain:fwd HASH    (old redis_key -> superseding redis_key)
#   KEYS[6] = chain:rev HASH    (new redis_key -> superseded redis_key)
#
#   ARGV[1] = new member (record redis_key). '' for a pure `invalidate`.
#   ARGV[2] = now (epoch seconds, caller-supplied so a whole batch shares one clock)
#   ARGV[3] = valid_from for the new member ('' -> use now)
#   ARGV[4] = ingested_at for the new member ('' -> use now)
#   ARGV[5] = mode: 'open' | 'supersede' | 'invalidate'
#   ARGV[6] = explicit close-at for the incumbent ('' -> use now)
#   ARGV[7] = explicit old member, bypassing the pointer ('' -> resolve via KEYS[4])
#
# Logic:
#   1. Resolve the incumbent: ARGV[7] if given, else GET KEYS[4]. Skipped entirely
#      in mode 'open' (a plain save never closes anything).
#   2. Idempotency guard: read ZSCORE invalid_at <incumbent> and refuse to re-close
#      anything whose score is not +inf. Under retry, or when two writers race the
#      same identity, the second close is a no-op — the script is idempotent and
#      chains rather than forks (plan Race 1).
#   3. Input validation: if the incumbent's own valid_from exceeds the close-at,
#      return error_reply(POPOTO_VALIDITY_CLOSE_BEFORE_START). A zero-or-negative
#      length interval is a caller bug, not a state to store silently.
#   4. Close the incumbent (ZADD invalid_at <close_at>) and write BOTH chain links
#      (HSET fwd old->new, HSET rev new->old) — same EVAL, so a half-linked chain
#      is unobservable.
#   5. Open the newcomer with NX semantics: valid_from / ingested_at / invalid_at
#      are only written when absent, so a re-save never shifts an existing interval
#      and — critically — can never resurrect an already-closed record (plan Race 2,
#      the reason ValidityField.on_save routes through this script in mode 'open'
#      rather than issuing a bare ZADD).
#   6. Repoint the open pointer at the newcomer, but only when the newcomer is
#      actually open. Returns the closed member key, or '' if nothing was closed.
#
# Nil-safety: every KEYS/ARGV read is guarded `KEYS[n] or ''` (Lua 5.1 gives nil,
# not '', for indices past numkeys), mirroring `decaying_sorted_field.py:67`. A
# caller that passes fewer keys degrades to the narrower operation instead of
# erroring on a nil concat.
#
# Valkey-safety: core sorted-set / hash / string commands only. Nothing here is a
# Redis module command, so the script runs byte-identically on Redis and Valkey.
SUPERSEDE_LUA = """
local vf_key, ia_key, ig_key = KEYS[1], KEYS[2], KEYS[3]
local ptr_key = KEYS[4] or ''
local fwd_key = KEYS[5] or ''
local rev_key = KEYS[6] or ''

local new_member = ARGV[1] or ''
local now = tonumber(ARGV[2] or '') or 0
local mode = ARGV[5] or 'open'
local old_member = ARGV[7] or ''

local valid_from = tonumber(ARGV[3] or '') or now
local ingested_at = tonumber(ARGV[4] or '') or now
local close_at = tonumber(ARGV[6] or '') or now

-- Redis returns the +inf score as the string 'inf'. Lua 5.1's tonumber() goes
-- through strtod and parses it, but the literal comparison is kept as a belt so
-- the open/closed decision never depends on that detail.
local function is_open(score)
  if score == false or score == nil then return false end
  if score == 'inf' or score == '+inf' then return true end
  local n = tonumber(score)
  return n ~= nil and n == math.huge
end

local closed = ''

if mode ~= 'open' then
  if old_member == '' and ptr_key ~= '' then
    local pointed = redis.call('GET', ptr_key)
    if pointed and pointed ~= false then old_member = pointed end
  end

  if old_member ~= '' and old_member ~= new_member then
    local old_score = redis.call('ZSCORE', ia_key, old_member)
    if is_open(old_score) then
      local old_start = redis.call('ZSCORE', vf_key, old_member)
      if old_start and old_start ~= false then
        local start_num = tonumber(old_start)
        if start_num ~= nil and close_at < start_num then
          return redis.error_reply('POPOTO_VALIDITY_CLOSE_BEFORE_START')
        end
      end
      redis.call('ZADD', ia_key, close_at, old_member)
      closed = old_member
      if new_member ~= '' then
        if fwd_key ~= '' then redis.call('HSET', fwd_key, old_member, new_member) end
        if rev_key ~= '' then redis.call('HSET', rev_key, new_member, old_member) end
      end
    end
  end
end

if new_member ~= '' then
  local new_score = redis.call('ZSCORE', ia_key, new_member)
  if new_score == false or is_open(new_score) then
    redis.call('ZADD', vf_key, 'NX', valid_from, new_member)
    redis.call('ZADD', ig_key, 'NX', ingested_at, new_member)
    redis.call('ZADD', ia_key, 'NX', '+inf', new_member)
    if ptr_key ~= '' then redis.call('SET', ptr_key, new_member) end
  end
end

return closed
"""


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
        # redis-py types every command ``Awaitable[T] | T`` for both the sync and
        # async clients, so mypy cannot see that these are concrete lists on the
        # sync client we actually use. Same narrowing cast as
        # ``ContextAssembler._resolve_excluded_keys``; CLAUDE.md notes this error
        # family is redis-py-version-dependent.
        started = cast(
            "list[Any]", POPOTO_REDIS_DB.zrangebyscore(valid_from_key, "-inf", t)
        )
        still_open = cast(
            "list[Any]", POPOTO_REDIS_DB.zrangebyscore(invalid_at_key, f"({t}", "+inf")
        )
        return {_as_str(m) for m in started} & {_as_str(m) for m in still_open}

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
        # redis-py types ZSCORE ``Awaitable[float | None] | float | None`` to cover
        # the async client; narrow to the sync reply (see the cast note in
        # :meth:`resolve_valid_keys`).
        start = cast(
            "Optional[float]", POPOTO_REDIS_DB.zscore(valid_from_key, member_key)
        )
        close = cast(
            "Optional[float]", POPOTO_REDIS_DB.zscore(invalid_at_key, member_key)
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
        pipeline: Optional[redis.client.Pipeline] = None,
    ) -> Any:
        """Run :data:`SUPERSEDE_LUA` once against this model/field's six keys.

        The single seam through which every validity mutation flows —
        ``ValidityField.on_save`` and ``SupersessionProtocol`` both call it, so
        there is exactly one place that knows the script's KEYS/ARGV order.

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
            pipeline: Optional external pipeline. When given, the EVAL is queued
                (following ``tag_field.py``'s threading shape) and the pipeline
                is returned; the closed-member result is only available at
                ``execute()`` time.

        Returns:
            The closed member key as ``str``, or ``None`` if nothing was closed —
            or the ``pipeline`` when one was supplied.

        Raises:
            ValueError: If ``mode`` is not one of :data:`VALID_MODES`, or if
                ``close_at`` precedes the incumbent's own ``valid_from`` (the
                script's :data:`CLOSE_BEFORE_START_ERROR` reply, mapped here).
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"ValidityField mode must be one of {sorted(VALID_MODES)}, got {mode!r}"
            )
        clock = time.time() if now is None else float(now)
        if close_at is not None and valid_from is not None and mode != "open":
            # Cheap client-side pre-check for the direct-invalidation form, where
            # the caller already knows both ends. The authoritative check lives in
            # the script (the stored valid_from is the one that matters).
            if float(close_at) < float(valid_from):
                raise ValueError(
                    "ValidityField: close-at "
                    f"({close_at}) precedes valid_from ({valid_from})"
                )

        keys = cls.get_all_keys(model, field_name)
        pointer_key = (
            cls.get_open_pointer_key(model, field_name, identity_digest)
            if identity_digest
            else ""
        )
        args = [
            keys["valid_from"],  # KEYS[1]
            keys["invalid_at"],  # KEYS[2]
            keys["ingested_at"],  # KEYS[3]
            pointer_key,  # KEYS[4] (may be '')
            keys["chain_fwd"],  # KEYS[5]
            keys["chain_rev"],  # KEYS[6]
            new_member or "",  # ARGV[1]
            repr(clock),  # ARGV[2]
            "" if valid_from is None else repr(float(valid_from)),  # ARGV[3]
            "" if ingested_at is None else repr(float(ingested_at)),  # ARGV[4]
            mode,  # ARGV[5]
            "" if close_at is None else repr(float(close_at)),  # ARGV[6]
            old_member or "",  # ARGV[7]
        ]

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline.eval(SUPERSEDE_LUA, 6, *args)
            return pipeline
        try:
            result = POPOTO_REDIS_DB.eval(SUPERSEDE_LUA, 6, *args)
        except redis.exceptions.ResponseError as e:
            if CLOSE_BEFORE_START_ERROR in str(e):
                raise ValueError(
                    "ValidityField: close-at precedes the record's own valid_from "
                    f"(member={old_member or '<pointer>'}, close_at={close_at})"
                ) from e
            raise
        closed = _as_str(result) if result else ""
        return closed or None

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
        pipeline: Optional[redis.client.Pipeline] = None,
        **kwargs: Any,
    ) -> Any:
        """Open (or re-affirm) this record's validity interval.

        Routes through :data:`SUPERSEDE_LUA` in mode ``'open'`` rather than
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
        try:
            valid_from = float(field_value) if field_value is not None else now
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
            pipeline=pipeline,
        )

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: Any,
        pipeline: Optional[redis.client.Pipeline] = None,
        **kwargs: Any,
    ) -> Any:
        """Remove every trace of this record from the validity keyspace.

        ``ZREM`` from the three interval ZSETs, ``HDEL`` from both chain HASHes,
        and clear any open-claim pointer still aimed at the record. Records are
        normally *closed*, not deleted — this hook exists so an explicit
        ``delete()`` (or a key migration, which calls it with ``saved_redis_key``)
        does not leave orphaned index members behind.

        Pointer cleanup scans ``{prefix}:open:*`` because the pointer keyspace is
        keyed by identity digest, not by member — the reverse lookup does not
        exist by design (plan D1 fixes the key count at six). This is a
        delete-time-only cost on a path that is rare relative to save and read;
        adding a seventh per-record back-pointer key to avoid it was rejected as
        the worse trade.

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
        keys = cls.get_all_keys(model_instance, field_name)

        prefix = cls.get_prefix_db_key(model_instance, field_name).redis_key
        stale_pointers = []
        for pointer_key in scan_keys(f"{prefix}:open:*"):
            pointer_key = _as_str(pointer_key)
            current = POPOTO_REDIS_DB.get(pointer_key)
            if current is not None and _as_str(current) == member:
                stale_pointers.append(pointer_key)

        if pipeline is not None:
            pipeline.zrem(keys["valid_from"], member)
            pipeline.zrem(keys["invalid_at"], member)
            pipeline.zrem(keys["ingested_at"], member)
            pipeline.hdel(keys["chain_fwd"], member)
            pipeline.hdel(keys["chain_rev"], member)
            for pointer_key in stale_pointers:
                pipeline.delete(pointer_key)
            return pipeline

        POPOTO_REDIS_DB.zrem(keys["valid_from"], member)
        POPOTO_REDIS_DB.zrem(keys["invalid_at"], member)
        POPOTO_REDIS_DB.zrem(keys["ingested_at"], member)
        POPOTO_REDIS_DB.hdel(keys["chain_fwd"], member)
        POPOTO_REDIS_DB.hdel(keys["chain_rev"], member)
        result: Any = 0
        for pointer_key in stale_pointers:
            result = POPOTO_REDIS_DB.delete(pointer_key)
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
                    # Narrow the sync ZRANGE replies (see the cast note in
                    # :meth:`resolve_valid_keys`).
                    everything = set(
                        cast("list[Any]", POPOTO_REDIS_DB.zrange(invalid_at_key, 0, -1))
                    ) | set(
                        cast("list[Any]", POPOTO_REDIS_DB.zrange(valid_from_key, 0, -1))
                    )
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

        The two-``ZRANGEBYSCORE`` primitive behind :meth:`filter_query`. Kept
        separate from :meth:`resolve_valid_keys` because the query layer
        intersects the raw index replies while assembler-side consumers want
        decoded ``str`` keys.
        """
        # Narrow the sync ZRANGEBYSCORE replies (see the cast note in
        # :meth:`resolve_valid_keys`).
        started = cast(
            "list[Any]", POPOTO_REDIS_DB.zrangebyscore(valid_from_key, "-inf", t)
        )
        still_open = cast(
            "list[Any]", POPOTO_REDIS_DB.zrangebyscore(invalid_at_key, f"({t}", "+inf")
        )
        return set(started) & set(still_open)
