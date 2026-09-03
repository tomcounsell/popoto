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
from ..redis_db import POPOTO_REDIS_DB, scan_keys, run_lua
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


#: Token -> exception dispatch, consulted in order by :func:`_map_lua_error`.
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


def _map_lua_error(e: BaseException) -> BaseException:
    """Return the typed exception for a :data:`SUPERSEDE_LUA` error reply.

    **Returns** the mapped exception instance, or ``e`` itself when no token
    matches. It never raises: every call site is spelled
    ``raise _map_lua_error(e) from e``, so a helper that raised internally would
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
#   ARGV[8] = '1' when ARGV[3] is a caller *assertion* about the new member's
#             valid-time; '' or '0' (or absent) otherwise. Only an assertion can
#             conflict (#588 plan D3).
#
# Phase rule (#588, plan D2) — LOAD-BEARING, do not "tidy" it away:
#   The script is split by the `-- MUTATION PHASE` marker. **No redis.call that
#   writes may appear above that marker.** Redis Lua has no rollback, so a script
#   that half-applied before hitting an error_reply would leave torn state
#   permanently. All-or-nothing is achieved by ordering, not by transactions:
#   every check that can fail runs first, reading only.
#
# Logic:
#   VALIDATION PHASE (reads and error_reply only)
#   1. Resolve the incumbent: ARGV[7] if given, else GET KEYS[4]. Skipped entirely
#      in mode 'open' (a plain save never closes anything).
#   2. Membership, at the instant of the write (#588). A caller-named successor
#      that does not exist -> error_reply(POPOTO_VALIDITY_MEMBER_ABSENT successor).
#      This is the whole of the fix: in a pipeline the record's HSET has already
#      applied by the time this body runs inside MULTI, so a same-transaction
#      successor is visible here even though a client-side EXISTS ahead of the
#      queue was not. The guards run ONLY in modes 'supersede'/'invalidate',
#      never in 'open' (plan Risk 1): mode 'open' is co-transactional with the
#      record's own hash write by construction, so there is nothing to verify.
#   3. Asserted vs hinted incumbent (plan Risk 3). An incumbent named explicitly
#      in ARGV[7] is a caller assertion: if it does not exist, error_reply(
#      POPOTO_VALIDITY_MEMBER_ABSENT incumbent). An incumbent resolved from the
#      open pointer is a hint — a pointer left naming a hard-deleted record reads
#      as "no incumbent", the same way chain() reads a dangling link.
#   4. Idempotency guard: read ZSCORE invalid_at <incumbent> and refuse to re-close
#      anything whose score is not +inf. Under retry, or when two writers race the
#      same identity, the second close is a no-op — the script is idempotent and
#      chains rather than forks (plan Race 1). Records the decision in will_close.
#   5. Input validation: if the incumbent's own valid_from exceeds the close-at,
#      return error_reply(POPOTO_VALIDITY_CLOSE_BEFORE_START). A zero-or-negative
#      length interval is a caller bug, not a state to store silently.
#   6. Valid-time single writer: when ARGV[8] == '1' and a valid_from score is
#      already stored for the new member, a disagreeing ARGV[3] returns
#      error_reply(POPOTO_VALIDITY_VALID_FROM_CONFLICT <stored> <requested>)
#      rather than losing silently to the NX below (#588 secondary defect,
#      measured by the reporter at 30 days of divergence).
#
#   MUTATION PHASE (every check above has passed)
#   7. Close the incumbent (ZADD invalid_at <close_at>) and write BOTH chain links
#      (HSET fwd old->new, HSET rev new->old) — same EVAL, so a half-linked chain
#      is unobservable.
#   8. Open the newcomer with NX semantics: valid_from / ingested_at / invalid_at
#      are only written when absent, so a re-save never shifts an existing interval
#      and — critically — can never resurrect an already-closed record (plan Race 2,
#      the reason ValidityField.on_save routes through this script in mode 'open'
#      rather than issuing a bare ZADD).
#   9. Repoint the open pointer at the newcomer, but only when the newcomer is
#      actually open. Returns the closed member key, or '' if nothing was closed.
#
# Replies:
#   bulk string <old_member>   the incumbent was closed by this call
#   bulk string ''             nothing closed: no incumbent, or already closed
#   error POPOTO_VALIDITY_MEMBER_ABSENT <role> <key>
#   error POPOTO_VALIDITY_CLOSE_BEFORE_START
#   error POPOTO_VALIDITY_VALID_FROM_CONFLICT <stored> <requested>
#
# The success reply shapes are UNCHANGED and that is load-bearing:
# ProvenanceJournal._write reads bool(results[close_index]).
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
local vf_assert = (ARGV[8] or '') == '1'
-- ARGV[7] was supplied by the caller: an assertion, not a pointer hint.
local asserted_old = old_member ~= ''

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
local will_close = false

-- VALIDATION PHASE -- reads and error_reply only. No write command may appear
-- above the `-- MUTATION PHASE` marker below: Redis Lua has no rollback, so
-- all-or-nothing is achieved by ordering.

if mode ~= 'open' then
  if old_member == '' and ptr_key ~= '' then
    local pointed = redis.call('GET', ptr_key)
    if pointed and pointed ~= false then old_member = pointed end
  end

  -- A caller-named successor must exist at the instant of the write. This is
  -- the whole of #588: in a pipeline the HSET has already applied by the time
  -- this script body runs inside MULTI, so a same-transaction successor is
  -- visible here even though a client-side EXISTS ahead of the queue was not.
  if new_member ~= '' and redis.call('EXISTS', new_member) == 0 then
    return redis.error_reply('POPOTO_VALIDITY_MEMBER_ABSENT successor ' .. new_member)
  end

  if old_member ~= '' then
    if redis.call('EXISTS', old_member) == 0 then
      if asserted_old then
        -- The caller named this record. A missing record is a caller error.
        return redis.error_reply('POPOTO_VALIDITY_MEMBER_ABSENT incumbent ' .. old_member)
      end
      -- Resolved from the open pointer, which is a hint and not an assertion.
      -- A pointer left naming a hard-deleted record means "no incumbent", the
      -- same reading chain() already gives a dangling link.
      old_member = ''
    end
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
      will_close = true
    end
  end
end

if new_member ~= '' and vf_assert then
  -- Valid-time has one writer. The ZADD NX below would drop a disagreeing start
  -- on the floor and leave the hash and the index answering differently
  -- (#588 secondary observation, measured at 30 days).
  local stored_vf = redis.call('ZSCORE', vf_key, new_member)
  if stored_vf and stored_vf ~= false then
    local s = tonumber(stored_vf)
    if s ~= nil and s ~= valid_from then
      return redis.error_reply(
        'POPOTO_VALIDITY_VALID_FROM_CONFLICT ' .. tostring(s) .. ' ' .. tostring(valid_from))
    end
  end
end

-- MUTATION PHASE -- every check above has passed.

if will_close then
  redis.call('ZADD', ia_key, close_at, old_member)
  closed = old_member
  if new_member ~= '' then
    if fwd_key ~= '' then redis.call('HSET', fwd_key, old_member, new_member) end
    if rev_key ~= '' then redis.call('HSET', rev_key, new_member, old_member) end
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
        """
        prefix = cls.get_prefix_db_key(model, field_name).redis_key
        matched = []
        for pointer_key in scan_keys(f"{prefix}:open:*"):
            pointer_key = _as_str(pointer_key)
            current = POPOTO_REDIS_DB.get(pointer_key)
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

        valid_from = cast(
            "Optional[float]", POPOTO_REDIS_DB.zscore(keys["valid_from"], member_key)
        )
        invalid_at = cast(
            "Optional[float]", POPOTO_REDIS_DB.zscore(keys["invalid_at"], member_key)
        )
        ingested_at = cast(
            "Optional[float]", POPOTO_REDIS_DB.zscore(keys["ingested_at"], member_key)
        )
        if valid_from is None and invalid_at is None and ingested_at is None:
            return None

        fwd = POPOTO_REDIS_DB.hget(keys["chain_fwd"], member_key)
        rev = POPOTO_REDIS_DB.hget(keys["chain_rev"], member_key)

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

        valid_from = state.get("valid_from")
        if valid_from is not None:
            POPOTO_REDIS_DB.zadd(keys["valid_from"], {member_key: float(valid_from)})

        invalid_at = state.get("invalid_at")
        if invalid_at is not None:
            score = (
                Defaults.VALIDITY_OPEN_SENTINEL
                if invalid_at == cls.OPEN_SENTINEL_TOKEN
                else float(invalid_at)
            )
            POPOTO_REDIS_DB.zadd(keys["invalid_at"], {member_key: score})

        ingested_at = state.get("ingested_at")
        if ingested_at is not None:
            POPOTO_REDIS_DB.zadd(keys["ingested_at"], {member_key: float(ingested_at)})

        chain_fwd = state.get("chain_fwd")
        if chain_fwd:
            POPOTO_REDIS_DB.hset(keys["chain_fwd"], member_key, chain_fwd)

        chain_rev = state.get("chain_rev")
        if chain_rev:
            POPOTO_REDIS_DB.hset(keys["chain_rev"], member_key, chain_rev)

        for digest in state.get("open_pointers") or []:
            POPOTO_REDIS_DB.set(
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
        assert_valid_from: bool = False,
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
            assert_valid_from: When ``True``, ``valid_from`` is a caller
                *assertion* about ``new_member``'s start rather than a default,
                and a disagreement with the already-stored start raises
                :class:`ValidityValidFromConflictError` instead of losing
                silently to the script's ``ZADD NX`` (plan D3). ``False`` — the
                default, and what ``SupersessionProtocol`` and
                ``ProvenanceJournal`` pass — preserves today's behavior for every
                existing caller: their ``at=`` is a *close-time* assertion about
                the incumbent, not a start-time assertion about the successor.
            pipeline: Optional external pipeline. When given, the EVAL is queued
                (following ``tag_field.py``'s threading shape) and the pipeline
                is returned; the closed-member result is only available at
                ``execute()`` time.

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
            The ``ResponseError`` -> typed-exception remap lives on the
            **non-pipeline** branch only. On a caller-supplied pipeline redis-py
            raises during ``pipe.execute()`` result parsing, long after this
            method returned, so the caller sees a raw
            ``redis.exceptions.ResponseError``. Use
            :meth:`SupersessionProtocol.save_and_supersede`, which owns its
            ``execute()``, to get a typed error in pipeline shape.
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
            "1" if assert_valid_from else "",  # ARGV[8]
        ]

        if isinstance(pipeline, redis.client.Pipeline):
            run_lua(pipeline, SUPERSEDE_LUA, 6, *args)
            return pipeline
        try:
            result = run_lua(POPOTO_REDIS_DB, SUPERSEDE_LUA, 6, *args)
        except redis.exceptions.ResponseError as e:
            raise _map_lua_error(e) from e
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
        stored = POPOTO_REDIS_DB.zscore(
            cls.get_valid_from_key(model_instance, field_name), member_key
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
        score = POPOTO_REDIS_DB.zscore(
            cls.get_valid_from_key(model, field_name), member_key
        )
        return None if score is None else float(score)

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

        stale_pointers = cls.find_open_pointers_for_member(
            model_instance, field_name, member
        )

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
