"""Redis implementation of the storage backend seam (#631 WS0).

Everything here is today's code, moved: each method body is the Redis call site
it replaces, lifted out of ``models/base.py``, ``models/query.py`` and the
slice's field modules without being rewritten. The six Lua scripts in the slice
live here under their existing names and are re-exported from their old modules
so every current reader keeps finding them.

**This class stores no client.** ``set_REDIS_DB_settings()`` *rebinds*
``redis_db.POPOTO_REDIS_DB``, and Python does not propagate a rebind to a
reference captured elsewhere, so a backend that held the client in
``__init__`` would be a fresh copy of the bug #655 removed from 29 modules.
Every method calls :func:`~popoto.redis_db.get_REDIS_DB` at call time
(``run_lua(get_REDIS_DB(), ...)`` for scripts) and :attr:`RedisBackend.client`
is a property that does the same. With nothing stored there is nothing to
invalidate on rebind, and the pytest plugin's ``_swap_db()`` (pool mutated in
place) is equally invisible to it. ``tests/test_redis_db_rebind_staleness.py``
probes this with a ``RecordingClient`` spy.
"""

from __future__ import annotations

from decimal import Decimal as _Decimal
from typing import Any, Iterator, Literal, Mapping, Sequence

import redis
import redis.exceptions

from ..exceptions import ModelException
from ..redis_db import ENCODING, get_REDIS_DB, run_lua, scan_keys
from . import UnitOfWork, as_key_str

__all__ = [
    "RedisBackend",
    "ATOMIC_INCREMENT_LUA",
    "PURGE_ORPHAN_LUA",
    "INDEX_SWAP_LUA",
    "TAG_SWAP_LUA",
    "DECAY_SCORE_LUA",
    "CAPPED_BAYESIAN_UPDATE_LUA",
    "SUPERSEDE_LUA",
]


# Reply decoding shares the boundary helper the field layer uses for its
# arguments (WS1f): one definition of "bytes or str in, str out".
_as_str = as_key_str


def _bound(value: float, inclusive: bool) -> str:
    """Render one ``ZRANGEBYSCORE`` bound in Redis wire format.

    ``filter_query`` spells these as ``f"{v}"`` / ``f"({v}"``; the validity
    paths pass ``"-inf"``/``"+inf"`` literally. Both shapes come from here.
    """
    if value == float("inf"):
        text = "+inf"
    elif value == float("-inf"):
        text = "-inf"
    else:
        text = f"{value}"
    return text if inclusive else f"({text}"


# Pointer-key derivation for the atomic swaps. These are the one-line builders
# ``IndexedFieldMixin._pointer_side_key`` / ``_pre_540_pointer_side_key`` /
# ``_legacy_pointer_field`` and ``TagFieldMixin._tag_pointer_side_key`` /
# ``_pre_540_tag_pointer_side_key`` hold today; WS1c retires the mixin copies.
# They are Redis key-layout migration state (#476, #540) and Postgres needs
# none of them.


def _idx_ptr_key(record_key: str, field: str) -> str:
    return f"$IdxPtr:{record_key}:{field}"


def _idx_pre_540_ptr_key(record_key: str, field: str) -> str:
    return f"{record_key}\x00idxptr\x00{field}"


def _idx_legacy_ptr_field(field: str) -> str:
    return f"{field}\x00idxset"


def _tag_ptr_key(record_key: str, field: str) -> str:
    return f"$TagPtr:{record_key}:{field}"


def _tag_pre_540_ptr_key(record_key: str, field: str) -> str:
    return f"{record_key}\x00tagptr\x00{field}"


def _validity_keys(model_prefix: str, field: str) -> dict[str, str]:
    """The five validity keys for one model/field, byte-equal to
    ``ValidityField.get_all_keys`` (``DB_key(prefix, "valid_from").redis_key``
    and friends; the suffixes contain nothing ``DB_key.clean`` would escape).
    The sixth key, the open pointer, is digest-specific and is built by
    ``supersede``/``open_pointer`` at the call site."""
    base = f"{model_prefix}:{field}"
    return {
        "valid_from": f"{base}:valid_from",
        "invalid_at": f"{base}:invalid_at",
        "ingested_at": f"{base}:ingested_at",
        "chain_fwd": f"{base}:chain:fwd",
        "chain_rev": f"{base}:chain:rev",
    }


# Lua script that atomically reads, decodes msgpack, increments, re-encodes,
# and writes back. Uses cmsgpack which is built into Redis since version 2.6.
# Moved from the inline ``lua_script`` in ``Model.atomic_increment``.
#
# KEYS[1] = redis hash key
# ARGV[1] = field name (bytes)
# ARGV[2] = delta value (string representation)
# ARGV[3] = 1 if field is Decimal type (uses tagged dict encoding), 0 otherwise
#
# Returns the new numeric value as a string.
ATOMIC_INCREMENT_LUA = """
        local current_packed = redis.call('HGET', KEYS[1], ARGV[1])
        local current_val = 0
        local is_decimal = tonumber(ARGV[3])

        if current_packed then
            local decoded = cmsgpack.unpack(current_packed)
            if is_decimal == 1 and type(decoded) == 'table' and decoded['as_encodable'] then
                current_val = tonumber(decoded['as_encodable'])
            elseif type(decoded) == 'number' then
                current_val = decoded
            end
        end

        local delta = tonumber(ARGV[2])
        local new_val = current_val + delta

        if is_decimal == 1 then
            local encoded = cmsgpack.pack({['__Decimal__'] = true, ['as_encodable'] = tostring(new_val)})
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        else
            local encoded = cmsgpack.pack(new_val)
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        end

        return tostring(new_val)
        """


#: Remove one orphan's index memberships, only if its hash is still gone.
#: KEYS[1] is the hash, KEYS[2..] the index keys; ARGV[i] is "s" (set) or
#: "z" (sorted set) for KEYS[i+1]. The EXISTS check inside the script is
#: what makes the purge safe against a concurrent re-create.
PURGE_ORPHAN_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
local member = KEYS[1]
local removed = 0
for i = 2, #KEYS do
    local kind = ARGV[i - 1]
    if kind == 'z' then
        removed = removed + redis.call('ZREM', KEYS[i], member)
    else
        removed = removed + redis.call('SREM', KEYS[i], member)
    end
end
return removed
"""


# INDEX_SWAP_LUA — atomic check-and-swap for indexed/unique field secondary index.
#
# Contract:
#   KEYS[1] = model hash key (the record's Redis hash where field values live)
#   KEYS[2] = new value Set key (the index Set for the new field value)
#   KEYS[3] = pointer side key (a standalone STRING key — NOT a field inside
#             the model hash) recording which Set this record currently
#             belongs to for this field, so we can atomically remove from the
#             old Set without relying on a stale client-side snapshot.
#             Namespaced under "$IdxPtr:" — see _pointer_side_key (#540).
#   KEYS[4] = pre-#540 pointer side key ({model_hash_key}\x00idxptr\x00{field}),
#             read-only migration fallback for records written by 1.8.1/1.8.2.
#             DEL'd unconditionally on every save (not just when its value is
#             adopted), so records self-heal off the colliding key space even
#             if an old 1.8.1/1.8.2 node re-writes it after a new node has
#             already migrated the record onto the KEYS[3] namespaced pointer.
#
#   ARGV[1] = field name (hash field name, for reading/writing field value in hash)
#   ARGV[2] = member key (the record's redis_key — the member stored in the Set)
#   ARGV[3] = new value, msgpack-packed by Python (written to model hash)
#   ARGV[4] = unique flag: "1" to enforce uniqueness, "0" to skip check
#   ARGV[5] = legacy-old-set hint: pre-cleaned old value-Set key from
#             _saved_field_values (empty string "" if no prior known value)
#             Used only on the first save after upgrading to the Lua-backed path,
#             for records that predate the pointer (whether the legacy in-hash
#             pointer scheme or the current side-key scheme).
#   ARGV[6] = legacy in-hash pointer field name ({field_name}\x00idxset), kept
#             ONLY for backward-compatible migration off the pre-fix (#476)
#             hash-embedded pointer scheme. Read as a migration fallback when
#             the side key (KEYS[3]) has never been written, and opportunistically
#             HDEL'd here so records self-heal off the polluted schema on next
#             write without requiring an offline migration.
#
# Logic:
#   1. Read the pointer from the side key (KEYS[3]). If absent, fall back to
#      the pre-#540 side key (KEYS[4]) and then to the legacy in-hash pointer
#      field (ARGV[6]) for records written by the pre-#476 code path. KEYS[4]
#      is DEL'd unconditionally (whether or not it had a value to adopt) so it
#      never surfaces to a key glob again, even under interleaving with an old
#      node; the legacy in-hash field (ARGV[6]) is HDEL'd only when adopted.
#   2. Idempotent re-save: if pointer already points to the new Set AND member
#      is already in it, just re-write the field bytes and return 1.
#   3. Uniqueness check (if ARGV[4]=="1"): scan the new Set for any member
#      other than self. Return error_reply on conflict — caller maps this to
#      ModelException.
#   4. Remove from old Set: use the resolved pointer if present and different
#      from new Set; else fall back to the legacy-old-set hint.
#   5. SADD member to new Set, update the pointer side key, write field bytes.
#
# Forward-compat rationale (#476): the pointer lives in a side key (a distinct
# Redis key, SET/GET, plain string) instead of a hash field, so it never
# appears in redis_hash.items() at all — no decoder-skip logic needed, and
# pre-1.8.0 (or any future) decoders that iterate the model hash and
# msgpack.unpackb() every value can never trip over it.
#
# Key-space rationale (#540): that side key must also live OUTSIDE the model's
# own key space, hence the "$IdxPtr:" prefix. See _pointer_side_key.
#
# Type-parity rationale: ARGV[3] is msgpack-packed by Python using the same
# encoder as encode_popoto_model_obj(), so the hash field value is byte-for-byte
# identical to what a plain HSET would have written.
INDEX_SWAP_LUA = """
local model_key, new_set, ptr_key, old_ptr_key =
  KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local field, member, new_bytes, is_unique, legacy_old_set, legacy_ptr_field =
  ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6]

local old_set = redis.call('GET', ptr_key)
if not old_set or old_set == false then
  -- Migration fallback 1: 1.8.1/1.8.2 wrote the pointer to a side key derived
  -- by suffixing the model hash key, which collides with the model's own key
  -- glob (#540). Adopt its value, then remove the colliding key.
  local prev_ptr = redis.call('GET', old_ptr_key)
  if prev_ptr and prev_ptr ~= false then
    old_set = prev_ptr
  end
end
-- Unconditional: reclaim the colliding legacy key even when the namespaced
-- ptr_key already exists (e.g. an old 1.8.1/1.8.2 node wrote old_ptr_key
-- again after a new node had already migrated to ptr_key). Otherwise the
-- legacy key survives inside the model key glob for the life of the record.
redis.call('DEL', old_ptr_key)
if not old_set or old_set == false then
  -- Migration fallback 2: pre-#476 records may still carry the pointer as a
  -- polluting field inside the model hash. Read it once, then scrub it.
  local legacy_ptr = redis.call('HGET', model_key, legacy_ptr_field)
  if legacy_ptr and legacy_ptr ~= false then
    old_set = legacy_ptr
    redis.call('HDEL', model_key, legacy_ptr_field)
  end
end

-- idempotent re-save: same set already recorded AND member already present
if old_set == new_set and redis.call('SISMEMBER', new_set, member) == 1 then
  redis.call('HSET', model_key, field, new_bytes)
  return 1
end

if is_unique == '1' then
  local members = redis.call('SMEMBERS', new_set)
  for _, m in ipairs(members) do
    if m ~= member then return redis.error_reply('POPOTO_UNIQUE_CONFLICT') end
  end
end

if old_set and old_set ~= false and old_set ~= '' then
  if old_set ~= new_set then
    redis.call('SREM', old_set, member)
  end
elseif legacy_old_set ~= '' and legacy_old_set ~= new_set then
  redis.call('SREM', legacy_old_set, member)
end
redis.call('SADD', new_set, member)
redis.call('SET', ptr_key, new_set)
redis.call('HSET', model_key, field, new_bytes)
return 1
"""


# TAG_SWAP_LUA — atomic multi-value tag-index diff.
#
# Contract (all Redis keys the script touches are declared as KEYS, per the
# scripting convention IndexedFieldMixin's INDEX_SWAP_LUA follows — the per-tag
# Set keys are KEYS, never ARGV):
#   KEYS[1]   = model hash key (the record's Redis hash)
#   KEYS[2]   = pointer side key — a standalone Redis SET holding the full
#               index-Set keys this record currently belongs to for this field.
#               Server-authoritative source of truth for the previous membership,
#               so the diff never relies on a stale client snapshot (#476).
#               Namespaced under "$TagPtr:" — see _tag_pointer_side_key (#540).
#   KEYS[3]   = pre-#540 pointer side key ({model_hash_key}\x00tagptr\x00{field}),
#               read-only migration fallback for records written by 1.8.1/1.8.2.
#               Read only when KEYS[2] is empty, but DEL'd unconditionally on
#               every save so it stops colliding with the model key glob.
#   KEYS[4..] = the new per-tag index-Set keys (already DB_key-built + colon-safe;
#               zero of them means the record is untagged / shared pool).
#
#   ARGV[1] = field name (hash field for the packed tag list)
#   ARGV[2] = member key (the record's redis_key — the Set member)
#   ARGV[3] = new value bytes, msgpack-packed by Python (the normalized tag list,
#             written to the model hash — byte-identical to a plain HSET)
#
# Logic (single atomic EVAL):
#   1. Read previous membership from the pointer side key (SMEMBERS).
#   2. SREM the member from every Set present before but absent now.
#   3. SADD the member to every Set present now but absent before.
#   4. Reset the pointer side key to exactly the new Set keys (DEL then SADD).
#   5. HSET the packed tag list into the model hash.
#
# Idempotent re-save is a natural no-op: empty diffs, and the HSET rewrites
# identical bytes. Untagged save (no KEYS[3..]) removes the member from all prior
# Sets, clears the pointer, and stores an empty list.
#
# Cluster note: like INDEX_SWAP_LUA, the model key, pointer key, and value-Set
# keys hash to different slots, so this script targets a single-node or
# proxy-fronted Redis/Valkey (popoto's index model is inherently non-cluster).
# Declaring the value-Set keys as KEYS (not ARGV) keeps the script honest under
# the scripting contract regardless.
TAG_SWAP_LUA = """
local model_key, ptr_key, old_ptr_key = KEYS[1], KEYS[2], KEYS[3]
local field, member, new_bytes = ARGV[1], ARGV[2], ARGV[3]

local new_sets = {}
for i = 4, #KEYS do
  new_sets[KEYS[i]] = true
end

local old_members = redis.call('SMEMBERS', ptr_key)
if #old_members == 0 then
  -- Migration fallback: 1.8.1/1.8.2 kept this pointer at a key derived by
  -- suffixing the model hash key, which the model's own glob matches (#540).
  old_members = redis.call('SMEMBERS', old_ptr_key)
end
redis.call('DEL', old_ptr_key)
local old_sets = {}
for _, s in ipairs(old_members) do
  old_sets[s] = true
end

-- remove member from Sets no longer present
for _, s in ipairs(old_members) do
  if not new_sets[s] then
    redis.call('SREM', s, member)
  end
end

-- add member to newly-present Sets
for i = 4, #KEYS do
  local s = KEYS[i]
  if not old_sets[s] then
    redis.call('SADD', s, member)
  end
end

-- reset the pointer side key to the new membership
redis.call('DEL', ptr_key)
for i = 4, #KEYS do
  redis.call('SADD', ptr_key, KEYS[i])
end

redis.call('HSET', model_key, field, new_bytes)
return 1
"""


# Lua script: compute decayed scores for all members of a sorted set.
# The sorted set stores members with their last_updated timestamp as score.
# Base scores are read from each member's model hash via cmsgpack.
# Returns top-N member keys ranked by decayed score.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# KEYS[2] = ConfidenceField ":data" companion hash (member -> msgpack payload).
#           Empty string / absent = confidence modulation disabled.
# KEYS[3] = ValidityField "invalid_at" ZSET (member -> close epoch, +inf = open).
#           Empty string / absent = validity gating disabled (#580, plan D5).
# KEYS[4] = ValidityField "valid_from" ZSET (member -> valid-from epoch).
#           Empty string / absent = validity gating disabled.
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
# ARGV[4] = base_score_field name (empty string = default 1.0)
# ARGV[5] = confidence modulation strength s (0 / absent = disabled)
# ARGV[6] = c0, the confidence field's initial_confidence. Serves as BOTH the
#           default for members with no confidence data AND the centering
#           constant, so a zero-evidence record is bit-exactly neutral for any
#           configured initial_confidence (not just 0.5).
# ARGV[7] = as-of epoch seconds for the validity gate. Absent / unparseable =
#           gate disabled, exactly like an empty KEYS[3]/KEYS[4].
# ARGV[8] = pre-trim budget, as a multiple of the scanned partition's
#           cardinality (#585). Absent / unparseable / <= 0 = never pre-trim,
#           which is the pre-#585 per-member-ZSCORE path byte-for-byte.
#
# Validity gate convention (#580): KEYS[3]/KEYS[4]/ARGV[7] are APPENDED, never
# renumbered -- see the KEYS[2] note inside the script. All three must be
# present and non-empty for the gate to engage; any of them empty or absent
# leaves this script byte-for-byte equivalent to the pre-#580 version, which is
# what lets existing callers pass numkeys of 1 or 2 unmodified and serve as the
# score-parity oracle. Membership is decided server-side inside the existing
# range read: no extra round trip and, crucially, no filter kwarg (a surviving
# filter param would kill sorted-range limit pushdown).
DECAY_SCORE_LUA = """
local zset_key = KEYS[1]
-- Confidence hash is KEYS[2] *in this script only*. The CyclicDecayField fork
-- of this math binds KEYS[2] = cycles and KEYS[3] = pressure, so its confidence
-- hash is KEYS[4]. The indices are deliberately different -- do not "unify"
-- them: reusing KEYS[2] there would cmsgpack.unpack the cycles array as a
-- confidence dict, which corrupts silently instead of erroring.
local confidence_hash_key = KEYS[2] or ''
-- Validity gate keys (#580). Appended, never renumbered. `KEYS[n] or ''`
-- mirrors the KEYS[2] guard above: Lua 5.1 hands out nil (not '') for indices
-- past numkeys, so callers passing numkeys 1 or 2 get the gate disabled rather
-- than an error, and their scores stay byte-identical to pre-#580.
local invalid_key = KEYS[3] or ''
local valid_key = KEYS[4] or ''
local now = tonumber(ARGV[1])
local decay_rate = tonumber(ARGV[2])
local max_results = tonumber(ARGV[3])
local base_score_field = ARGV[4]
local s = tonumber(ARGV[5]) or 0
local c0 = tonumber(ARGV[6]) or 0.5
local as_of = tonumber(ARGV[7] or '')
-- The as-of RANGE BOUND is the raw ARGV string, never a reformatted `as_of`.
-- Python builds it with repr(float) in validity_gate_args; round-tripping it
-- through Lua 5.1's %.14g tostring (or string.format %.17g) perturbs the last
-- digits and would misclassify a member sitting exactly on the boundary.
local as_of_raw = ARGV[7] or ''
local pretrim_max_ratio = tonumber(ARGV[8] or '') or 0

-- When modulation is off, never pay for the extra HGET per member.
local modulate = confidence_hash_key ~= '' and s ~= 0

-- Validity gating engages only with both interval ZSETs AND an as-of. Any one
-- missing means "gate disabled" -- the same empty-string-is-off convention the
-- confidence modulation guard uses.
local gate = invalid_key ~= '' and valid_key ~= '' and as_of ~= nil

-- Pre-trim (#585). The gate's rule is an EXCLUSION -- skip a member whose
-- invalid_at <= as_of, or whose valid_from > as_of; a member absent from either
-- ZSET is unmanaged and stays visible. Those two clauses are exactly two score
-- ranges, so the whole exclusion set can be fetched in TWO calls and looked up
-- in O(1) per member, instead of paying a ZSCORE pair per scanned member.
-- Semantics are identical, not merely similar: '-inf'..as_of is the first
-- clause with its inclusive bound, '(as_of'..'+inf' the second with its
-- exclusive one, and a member in neither range is in neither result.
--
-- It is CONDITIONAL because invalid_at/valid_from are model+field scoped while
-- this scan is ONE partition. A small hot partition beside a large archive of
-- closed records makes the range read pull far more than the scan touches --
-- measured at 16x ungated before this guard existed. So: count first (ZCOUNT is
-- O(log N)), and only pre-trim while the exclusion set is within
-- pretrim_max_ratio times the partition we are about to scan. Otherwise fall
-- through to the per-member path below, unchanged.
--
-- The two ZCOUNTs are SUMMED, which double-counts a member present in both
-- ranges (a malformed interval, valid_from > invalid_at). That is deliberate:
-- the `excluded` table de-duplicates, so it never affects correctness, and
-- over-counting only makes the guard fall back sooner -- the safe direction.
-- Do not "fix" it into a union count; the 4.0 default is calibrated against
-- this sum.
local excluded = nil
if gate and pretrim_max_ratio > 0 then
    local partition_n = redis.call('ZCARD', zset_key)
    -- pcall: as_of_raw satisfied tonumber() above, but tonumber accepts strings
    -- Redis rejects as a range bound (hex, for one). Falling back beats erroring
    -- on a call shape that worked before this optimization existed.
    local ok, closed_n = pcall(redis.call, 'ZCOUNT', invalid_key, '-inf', as_of_raw)
    local ok2, future_n = pcall(redis.call, 'ZCOUNT', valid_key, '(' .. as_of_raw, '+inf')
    if ok and ok2 and (closed_n + future_n) <= (partition_n * pretrim_max_ratio) then
        local ok3, closed = pcall(
            redis.call, 'ZRANGEBYSCORE', invalid_key, '-inf', as_of_raw)
        local ok4, future = pcall(
            redis.call, 'ZRANGEBYSCORE', valid_key, '(' .. as_of_raw, '+inf')
        if ok3 and ok4 then
            excluded = {}
            for i = 1, #closed do excluded[closed[i]] = true end
            for i = 1, #future do excluded[future[i]] = true end
        end
    end
end

-- Get all members with their last_updated timestamps
local members = redis.call('ZRANGE', zset_key, 0, -1, 'WITHSCORES')

local scored = {}
for i = 1, #members, 2 do
    local member = members[i]
    local last_updated = tonumber(members[i + 1])

    -- Validity gate (#580, plan D5). Placed here deliberately: it is the
    -- cheapest possible position, before the base-score HGET and before all
    -- decay math, so an excluded member costs at most one table lookup (or,
    -- on the un-pretrimmed path, two ZSCOREs). Lua 5.1 has no `goto`, hence
    -- the `if include then` wrapper around the body rather than a `continue`.
    --
    -- The two branches below MUST stay semantically identical (#585): the
    -- per-member one is the parity oracle for the pre-trimmed one, and
    -- tests/test_validity_field.py::TestValidityPretrim asserts byte-identical
    -- replies between them. The `elseif` fires whenever pre-trim declined --
    -- exclusion set too large for the budget, or a range read that errored.
    --
    -- A member is skipped when its interval does not cover as_of:
    --   invalid_at <= as_of  (already closed)  or  valid_from > as_of (not yet
    --   started). Redis renders the +inf open sentinel as 'inf', which Lua
    --   5.1's tonumber parses via strtod, so an open record's `n <= as_of` is
    --   false. A member absent from either ZSET has no interval and is left
    --   alone -- the gate is an exclusion rule, not a whitelist.
    local include = true
    if gate and excluded ~= nil then
        -- Pre-trimmed: the membership question was already answered in two
        -- range reads above. O(1) table lookup, no round trip per member.
        if excluded[member] then
            include = false
        end
    elseif gate then
        local closed_at = redis.call('ZSCORE', invalid_key, member)
        if closed_at then
            local cn = tonumber(closed_at)
            if cn ~= nil and cn <= as_of then
                include = false
            end
        end
        if include then
            local started_at = redis.call('ZSCORE', valid_key, member)
            if started_at then
                local sn = tonumber(started_at)
                if sn ~= nil and sn > as_of then
                    include = false
                end
            end
        end
    end

    if include then
        local base_score = 1.0
        if base_score_field ~= '' then
            -- Read base score from the model's own hash
            local raw = redis.call('HGET', member, base_score_field)
            if raw then
                local ok, decoded = pcall(cmsgpack.unpack, raw)
                if ok and type(decoded) == 'number' then
                    base_score = decoded
                elseif ok and type(decoded) == 'table' and decoded['as_encodable'] then
                    -- Handle Decimal type (tagged dict encoding)
                    base_score = tonumber(decoded['as_encodable']) or 1.0
                end
            end
        end

        -- Compute elapsed time in days (minimum 0.01 to avoid division by zero)
        local elapsed_days = math.max((now - last_updated) / 86400, 0.01)

        -- Power-law decay: base_score * elapsed^(-decay_rate)
        -- Sign-preserving: math.pow only takes non-negative base, so split sign
        -- from magnitude. Positive-base output is bitwise unchanged.
        local sign = base_score < 0 and -1 or 1
        local mag = math.abs(base_score)
        local decayed = sign * mag * math.pow(elapsed_days, -decay_rate)

        if modulate then
            -- Per-member effective decay rate from accumulated outcome evidence.
            -- Payload shape matches CAPPED_BAYESIAN_UPDATE_LUA's writer; anything
            -- missing / undecodable / non-numeric falls back to c0 (neutral).
            local c = c0
            local craw = redis.call('HGET', confidence_hash_key, member)
            if craw then
                local ok, data = pcall(cmsgpack.unpack, craw)
                if ok and type(data) == 'table' then
                    local v = data['confidence'] or data[1]
                    if type(v) == 'number' then
                        c = v
                    end
                end
            end
            -- Defensive clamp: the hash could hold anything.
            c = math.max(0, math.min(1, c))

            local eff = decay_rate * math.pow(2, s * 2 * (c0 - c))

            -- Correction factor, applied on top of TODAY'S formula so neutrality
            -- is bit-exact: when c == c0 the exponent is exactly 0 and
            -- math.pow(x, -0) is exactly 1.0.
            --
            -- The math.max(elapsed_days, 1.0) guard is load-bearing, NOT
            -- redundant. elapsed_days is floored at 0.01, and for t < 1 the term
            -- t^(-rate) is a multiplier > 1 that a LARGER rate amplifies MORE (at
            -- t=0.01, rate 0.66 gives x21.9 vs x5.0 for rate 0.35). Without the
            -- guard, modulation runs backwards for the first 24 hours and boosts
            -- exactly the low-confidence junk it is meant to bury -- and since
            -- agent memory is touched constantly, most of the working set lives
            -- in that region. Clamping the correction's base to >= 1.0 makes the
            -- term exactly 1.0 for fresh records, so modulation only ever applies
            -- in the region where a higher rate means a lower score.
            decayed = decayed
                * math.pow(math.max(elapsed_days, 1.0), -(eff - decay_rate))
        end

        table.insert(scored, {member, decayed})
    end
end

-- Two-level total-order comparator. Lua 5.1 table.sort is unstable and
-- members are collected from ZRANGE (index order), so a score-only comparator
-- leaves equal-scored members in undefined order -- including across the
-- max_results truncation boundary below. Tie-break on a[1] (the member's full
-- redis_key): sorted-set members are unique by definition, so distinct entries
-- always have unequal key strings, giving a strict weak ordering. Decayed
-- scores are finite (sign * finite magnitude * math.pow(elapsed>=0.01, ...)),
-- so a[2] ~= b[2] behaves as a normal total order (no NaN).
table.sort(scored, function(a, b)
    if a[2] ~= b[2] then
        return a[2] > b[2]
    end
    return a[1] < b[1]
end)

-- Return top-N as flat array: [member1, score1, member2, score2, ...]
local result = {}
for i = 1, math.min(max_results, #scored) do
    table.insert(result, scored[i][1])
    table.insert(result, tostring(scored[i][2]))
end
return result
"""


# Lua script: atomic capped-evidence update of the confidence companion hash.
# While n_eff = min(evidence_count + prior_weight, cap) is below the cap, the
# update is an exact running mean over {prior, signals...} (order-invariant).
# At the cap, the gain freezes at 1/(cap+1): fixed-gain exponential
# forgetting with an effective memory window of cap+1 observations.
# KEYS[1] = companion hash key
# ARGV[1] = member key (redis_key of the model instance)
# ARGV[2] = signal (float 0-1)
# ARGV[3] = initial_confidence (default for missing data)
# ARGV[4] = evidence_cap
CAPPED_BAYESIAN_UPDATE_LUA = """
local hash_key = KEYS[1]
local member = ARGV[1]
local signal = tonumber(ARGV[2])
local initial_confidence = tonumber(ARGV[3])

-- KEYS[2] (optional): the member's own hash. When given, an update for a
-- record that no longer exists is a no-op, which lets callers batch many
-- updates into one pipeline without a preceding EXISTS round trip each.
if KEYS[2] and redis.call('EXISTS', KEYS[2]) == 0 then
    return nil
end

-- Read existing data
local raw = redis.call('HGET', hash_key, member)
local confidence = initial_confidence
local evidence_count = 0
local corroborations = 0
local contradictions = 0

if raw then
    local ok, data = pcall(cmsgpack.unpack, raw)
    if ok and type(data) == 'table' then
        confidence = data['confidence'] or data[1] or initial_confidence
        evidence_count = data['evidence_count'] or data[2] or 0
        corroborations = data['corroborations'] or data[3] or 0
        contradictions = data['contradictions'] or data[4] or 0
    end
end

-- Capped-evidence update: running mean while effective evidence <= cap,
-- fixed-gain exponential forgetting (window cap+1) beyond it.
-- ARGV[4] = evidence_cap (the only new ARGV)
local prior_weight = 1  -- internal constant; not user config (issue #407 decision Q2)
local cap = tonumber(ARGV[4])
local n_eff = math.min(evidence_count + prior_weight, cap)
local new_confidence = confidence + (signal - confidence) / (n_eff + 1)

-- Clamp to [0, 1]
new_confidence = math.max(0, math.min(1, new_confidence))

-- Update counters
evidence_count = evidence_count + 1
if signal >= 0.5 then
    corroborations = corroborations + 1
else
    contradictions = contradictions + 1
end

-- Pack and store
local updated = {
    confidence = new_confidence,
    evidence_count = evidence_count,
    corroborations = corroborations,
    contradictions = contradictions
}
redis.call('HSET', hash_key, member, cmsgpack.pack(updated))

-- Return new values
return {tostring(new_confidence), tostring(evidence_count), tostring(corroborations), tostring(contradictions)}
"""


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


class RedisBackend:
    """The Redis :class:`~popoto.backends.Backend`. Stores no client (see module
    docstring); ``client`` resolves ``get_REDIS_DB()`` on every access."""

    @property
    def client(self) -> Any:
        return get_REDIS_DB()

    # -- A. Unit of work ---------------------------------------------------

    def begin(self) -> UnitOfWork:
        return get_REDIS_DB().pipeline()

    def native(self) -> Any:
        return get_REDIS_DB()

    # -- B. Records ---------------------------------------------------------

    def save_record(
        self,
        key: str,
        fields: Mapping[Any, bytes],
        *,
        class_set: str | None = None,
        obsolete_key: str | None = None,
        ttl: int | None = None,
        expire_at: float | None = None,
        numeric: Mapping[str, float] | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        # ``numeric`` is for Postgres's typed column; Redis keeps the msgpack
        # bytes only.
        db: Any = get_REDIS_DB().pipeline() if uow is None else uow
        if fields:
            db.hset(key, mapping=dict(fields))  # 1
        # else: EVAL-only path — indexed field EVALs write the hash fields
        if ttl is not None:
            db.expire(key, ttl)  # 2
        elif expire_at is not None:
            db.expireat(key, int(expire_at))  # 2
        # protocol-2: ``class_set=None`` leaves the class set untouched (3, 4a).
        if class_set is not None:
            db.sadd(class_set, key)  # 3
        if obsolete_key and obsolete_key != key:  # 4
            if class_set is not None:
                db.srem(class_set, obsolete_key)  # 4a - remove old key from set
            db.delete(obsolete_key)  # 4b
        if uow is not None:
            return None
        results = db.execute()
        # When fields is non-empty, results[0] is the HSET count. When
        # EVAL-only (all fields are indexed), fields is empty so results[0] is
        # the first queued op result (expire/sadd), still an int.
        return results[0] if results else 0

    def set_expiry(
        self,
        key: str,
        *,
        ttl: int | None = None,
        expire_at: float | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        # protocol-2: the partial save's trailing EXPIRE/EXPIREAT, issued after
        # the field hooks so it lands on a hash the INDEX_SWAP EVAL created.
        db: Any = get_REDIS_DB() if uow is None else uow
        if ttl is not None:
            reply = db.expire(key, ttl)
        elif expire_at is not None:
            reply = db.expireat(key, int(expire_at))
        else:
            return None
        return None if uow is not None else bool(reply)

    def load_record(self, key: str) -> dict[Any, bytes] | None:
        hashmap = get_REDIS_DB().hgetall(key)
        if not hashmap:
            return None
        return hashmap

    def load_records(self, keys: Sequence[str]) -> list[dict[Any, bytes] | None]:
        if not keys:
            return []
        pipeline = get_REDIS_DB().pipeline()
        for key in keys:
            pipeline.hgetall(key)
        hashes_list = pipeline.execute()
        return [hashmap if hashmap else None for hashmap in hashes_list]

    def load_fields(self, key: str, names: Sequence[str]) -> list[bytes | None]:
        if not names:
            raise ValueError("load_fields() requires at least one field name")
        # Command choice is a parity requirement, not an optimization: the
        # recipe call site this replaces issues HGET for a single field.
        # Always emitting HMGET here -- even for one name -- would change the
        # wire trace and break the byte-identical-behavior contract this PR
        # is gated on. Do not "simplify" this to always-HMGET.
        if len(names) == 1:
            return [get_REDIS_DB().hget(key, names[0])]
        return list(get_REDIS_DB().hmget(key, list(names)))

    def load_fields_many(
        self, keys: Sequence[str], names: Sequence[str]
    ) -> list[list[bytes | None]]:
        # protocol-3: ``get_many_objects``'s projection batch, moved. Always
        # HMGET -- the path never special-cased one name, and the pipelined
        # ``Pipeline.hmget`` count is what ``test_query_hydration_count`` pins.
        if not names:
            raise ValueError("load_fields_many() requires at least one field name")
        if not keys:
            return []
        pipeline = get_REDIS_DB().pipeline()
        for key in keys:
            pipeline.hmget(key, list(names))
        return [list(reply) for reply in pipeline.execute()]

    def record_exists(self, key: str) -> bool:
        return bool(get_REDIS_DB().exists(key))

    def records_exist(self, keys: Sequence[str]) -> list[bool]:
        # Moved from ``check_indexes._count_orphans`` /
        # ``clean_indexes._collect_orphans``: pipeline EXISTS in one batch.
        if not keys:
            return []
        pipe = get_REDIS_DB().pipeline()
        for key in keys:
            pipe.exists(key)
        return [bool(reply) for reply in pipe.execute()]

    def delete_record(
        self, key: str, *, class_set: str, uow: UnitOfWork | None = None
    ) -> Any:
        db: Any = get_REDIS_DB().pipeline() if uow is None else uow
        db.delete(key)  # 1
        db.srem(class_set, key)  # 2
        if uow is not None:
            return None
        results = db.execute()
        return bool(results[0]) if results else False

    def list_keys(self, class_set: str) -> set[str]:
        return {_as_str(k) for k in get_REDIS_DB().smembers(class_set)}

    def count_records(self, class_set: str) -> int:
        return int(get_REDIS_DB().scard(class_set) or 0)

    # -- C. Atomic increment -------------------------------------------------

    def increment_field(
        self,
        key: str,
        field: str,
        delta: int | float | _Decimal,
        *,
        kind: Literal["int", "float", "decimal"],
        uow: UnitOfWork | None = None,
    ) -> int | float | _Decimal | None:
        field_name_bytes = field.encode(ENCODING)
        is_decimal = 1 if kind == "decimal" else 0
        delta_str = str(float(delta) if isinstance(delta, _Decimal) else delta)

        if uow is not None:
            # When using a pipeline, register the script and call it
            script = get_REDIS_DB().register_script(ATOMIC_INCREMENT_LUA)
            script(
                keys=[key],
                args=[field_name_bytes, delta_str, is_decimal],
                client=uow,
            )
            return None

        # Execute the Lua script directly
        result_str = run_lua(
            get_REDIS_DB(),
            ATOMIC_INCREMENT_LUA,
            1,
            key,
            field_name_bytes,
            delta_str,
            is_decimal,
        )

        # Parse result and convert to field type
        if isinstance(result_str, bytes):
            result_str = result_str.decode(ENCODING)

        if kind == "int":
            # Lua may return "15.0" for integer arithmetic; parse via float then int
            return int(float(result_str))
        if kind == "decimal":
            return _Decimal(result_str)
        return float(result_str)

    # -- D. Side maps --------------------------------------------------------

    def map_get(self, idx: str, member: str) -> bytes | None:
        return get_REDIS_DB().hget(idx, member)

    def map_set(
        self,
        idx: str,
        member: str,
        value: bytes,
        *,
        only_if_absent: bool = False,
        uow: UnitOfWork | None = None,
    ) -> bool | None:
        db: Any = get_REDIS_DB() if uow is None else uow
        if only_if_absent:
            # HSETNX: atomic set-if-not-exists, no race condition
            reply = db.hsetnx(idx, member, value)
        else:
            reply = db.hset(idx, member, value)
        if uow is not None:
            return None
        return bool(reply)

    def map_delete(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> int | None:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.hdel(idx, member)
        if uow is not None:
            return None
        return int(reply)

    def map_scan(
        self, idx: str, pattern: str = "*", count: int = 100
    ) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        cursor = 0
        while True:
            cursor, data = get_REDIS_DB().hscan(
                idx, cursor=cursor, match=pattern, count=count
            )
            for member_key, raw_value in data.items():
                result[_as_str(member_key)] = raw_value
            if cursor == 0:
                break
        return result

    # -- E. Set indexes ------------------------------------------------------

    def index_add(self, idx: str, member: str, *, uow: UnitOfWork | None = None) -> Any:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.sadd(idx, member)
        return None if uow is not None else reply

    def index_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.srem(idx, member)
        return None if uow is not None else reply

    def index_members(self, idx: str) -> set[str]:
        return {_as_str(m) for m in get_REDIS_DB().smembers(idx)}

    def index_union(self, idxs: Sequence[str]) -> set[str]:
        # Use SUNION for efficient server-side set union (single command vs N
        # SMEMBERS); an empty key list is an empty match, never a crash.
        if not idxs:
            return set()
        return {_as_str(m) for m in get_REDIS_DB().sunion(list(idxs))}

    def index_intersection(self, idxs: Sequence[str]) -> set[str]:
        if not idxs:
            return set()
        return {_as_str(m) for m in get_REDIS_DB().sinter(list(idxs))}

    def scan_index_names(self, pattern: str) -> list[str]:
        return [_as_str(k) for k in scan_keys(pattern)]

    def scan_record_keys(self, pattern: str) -> list[str]:
        # Moved from ``key_field_mixin._scan_hash_keys``: classify scanned keys
        # by their actual Redis TYPE rather than by any byte pattern in the key
        # name, so a non-hash companion key sharing the glob (a legacy pointer,
        # a capped-list companion) is skipped rather than crashing a downstream
        # HGETALL with WRONGTYPE (#540). ``transaction=False``: one
        # non-transactional round trip of TYPE calls, no MULTI/EXEC.
        keys = scan_keys(pattern)
        if not keys:
            return []
        pipeline = get_REDIS_DB().pipeline(transaction=False)
        for key in keys:
            pipeline.type(key)
        key_types = pipeline.execute()
        return [
            _as_str(key)
            for key, key_type in zip(keys, key_types)
            if key_type in (b"hash", "hash")
        ]

    # -- F. Sorted indexes ---------------------------------------------------

    def sorted_add(
        self, idx: str, member: str, score: float, *, uow: UnitOfWork | None = None
    ) -> Any:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.zadd(idx, {member: score})
        return None if uow is not None else reply

    def sorted_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.zrem(idx, member)
        return None if uow is not None else reply

    def sorted_score(self, idx: str, member: str) -> float | None:
        reply = get_REDIS_DB().zscore(idx, member)
        return None if reply is None else float(reply)

    def sorted_count(self, idx: str) -> int:
        return int(get_REDIS_DB().zcard(idx))

    def sorted_members(
        self, idx: str, start: int = 0, stop: int = -1, *, reverse: bool = False
    ) -> list[str]:
        # Resolve the client attribute at call time so test spies and fault
        # injectors patched onto POPOTO_REDIS_DB keep intercepting the read.
        return [
            _as_str(raw)
            for raw in (
                get_REDIS_DB().zrevrange(idx, start, stop)
                if reverse
                else get_REDIS_DB().zrange(idx, start, stop)
            )
        ]

    def sorted_range(
        self,
        idx: str,
        lo: float,
        hi: float,
        *,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        lo_bound = _bound(lo, lo_inclusive)
        hi_bound = _bound(hi, hi_inclusive)
        bounded = isinstance(limit, int) and limit > 0
        if bounded and reverse:
            # ZREVRANGEBYSCORE takes the bounds high-then-low.
            redis_db_keys_list = get_REDIS_DB().zrevrangebyscore(
                idx, hi_bound, lo_bound, start=0, num=limit
            )
        elif bounded:
            redis_db_keys_list = get_REDIS_DB().zrangebyscore(
                idx, lo_bound, hi_bound, start=0, num=limit
            )
        elif reverse:
            redis_db_keys_list = get_REDIS_DB().zrevrangebyscore(
                idx, hi_bound, lo_bound
            )
        else:
            redis_db_keys_list = get_REDIS_DB().zrangebyscore(idx, lo_bound, hi_bound)
        return [_as_str(k) for k in redis_db_keys_list]

    def sorted_increment(
        self, idx: str, member: str, delta: float, *, uow: UnitOfWork | None = None
    ) -> float | None:
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.zincrby(idx, delta, member)
        if uow is not None:
            return None
        return float(reply)

    # -- G. Atomic swaps -----------------------------------------------------

    def swap_index(
        self,
        record_key: str,
        field: str,
        new_idx: str,
        value: bytes,
        *,
        unique: bool,
        legacy_old_idx: str = "",
        uow: UnitOfWork | None = None,
    ) -> Any:
        # Server-authoritative pointer: a standalone side key (#476), never a
        # field inside the model hash.
        ptr_key = _idx_ptr_key(record_key, field)
        # Pre-#540 side key (1.8.1/1.8.2), read-only migration fallback —
        # DEL'd by the Lua once read, since it collides with the model key glob.
        old_ptr_key = _idx_pre_540_ptr_key(record_key, field)
        # Legacy in-hash pointer field name, read-only migration fallback for
        # records written before #476 shipped (scrubbed via HDEL when found).
        legacy_ptr_field = _idx_legacy_ptr_field(field)
        is_unique = "1" if unique else "0"
        args = (
            record_key,  # KEYS[1]: the model hash key (same as record redis_key)
            new_idx,  # KEYS[2]: new value Set key
            ptr_key,  # KEYS[3]: pointer side key
            old_ptr_key,  # KEYS[4]: pre-#540 pointer side key (migration)
            field,
            record_key,
            value,
            is_unique,
            legacy_old_idx,
            legacy_ptr_field,
        )
        if uow is not None:
            # Queue EVAL into caller's pipeline — authoritative check at execute()
            run_lua(uow, INDEX_SWAP_LUA, 4, *args)
            return None
        # Internal path: execute EVAL via the client directly. This runs
        # atomically on the Redis server; no client-side race.
        try:
            return run_lua(get_REDIS_DB(), INDEX_SWAP_LUA, 4, *args)
        except Exception as e:
            if "POPOTO_UNIQUE_CONFLICT" in str(e):
                raise ModelException(
                    f"Uniqueness violation on {record_key}.{field}: the value "
                    f"indexed at {new_idx!r} is already taken by another instance"
                )
            raise

    def drop_index_entry(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idx: str,
        uow: UnitOfWork | None = None,
    ) -> Any:
        # Reads live Redis state (GET/HGET), so it must run BEFORE the model
        # hash itself is deleted — see Model.delete() (#476).
        model_hash_key = record_key  # model hash key = redis_key for this record

        # 1. Server-authoritative pointer side key (current scheme, #476/#540).
        ptr_key = _idx_ptr_key(model_hash_key, field)
        old_ptr_key = _idx_pre_540_ptr_key(model_hash_key, field)
        ptr_value = get_REDIS_DB().get(ptr_key)

        if not ptr_value:
            # 2. Migration fallback: pre-#540 side key written by 1.8.1/1.8.2.
            ptr_value = get_REDIS_DB().get(old_ptr_key)

        if not ptr_value:
            # 3. Migration fallback: legacy in-hash pointer field written by
            # pre-#476 code. Only useful if this HGET runs before the model
            # hash is deleted.
            ptr_value = get_REDIS_DB().hget(
                model_hash_key, _idx_legacy_ptr_field(field)
            )

        if ptr_value:
            # Pointer present: SREM from the set it names
            index_set_key = _as_str(ptr_value)
        else:
            # No pointer found anywhere: fall back to field_value-derived key
            index_set_key = fallback_idx

        if uow is not None:
            uow_any: Any = uow
            uow_any.srem(index_set_key, record_key)
            uow_any.delete(ptr_key, old_ptr_key)
            return None
        result = get_REDIS_DB().srem(index_set_key, record_key)
        get_REDIS_DB().delete(ptr_key, old_ptr_key)
        return result

    def swap_tags(
        self,
        record_key: str,
        field: str,
        new_idxs: Sequence[str],
        value: bytes,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        ptr_key = _tag_ptr_key(record_key, field)
        old_ptr_key = _tag_pre_540_ptr_key(record_key, field)
        # numkeys = model hash key + 2 pointer side keys + N per-tag Set keys.
        numkeys = 3 + len(new_idxs)
        args = [
            record_key,  # KEYS[1] model hash key
            ptr_key,  # KEYS[2] pointer side key
            old_ptr_key,  # KEYS[3] pre-#540 pointer side key (migration)
            *new_idxs,  # KEYS[4..] new per-tag Set keys
            field,  # ARGV[1]
            record_key,  # ARGV[2] member
            value,  # ARGV[3] packed tag list
        ]
        if uow is not None:
            run_lua(uow, TAG_SWAP_LUA, numkeys, *args)
            return None
        return run_lua(get_REDIS_DB(), TAG_SWAP_LUA, numkeys, *args)

    def drop_tag_entries(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idxs: Sequence[str],
        uow: UnitOfWork | None = None,
    ) -> Any:
        ptr_key = _tag_ptr_key(record_key, field)
        old_ptr_key = _tag_pre_540_ptr_key(record_key, field)

        raw_sets = get_REDIS_DB().smembers(ptr_key)
        if not raw_sets:
            # Migration fallback: pre-#540 pointer written by 1.8.1/1.8.2.
            raw_sets = get_REDIS_DB().smembers(old_ptr_key)
        set_keys = [_as_str(s) for s in raw_sets]

        if not set_keys and fallback_idxs:
            # Fallback: derive Set keys from the field value if no pointer exists.
            set_keys = list(fallback_idxs)

        if uow is not None:
            uow_any: Any = uow
            for set_key in set_keys:
                uow_any.srem(set_key, record_key)
            uow_any.delete(ptr_key, old_ptr_key)
            return None
        for set_key in set_keys:
            get_REDIS_DB().srem(set_key, record_key)
        return get_REDIS_DB().delete(ptr_key, old_ptr_key)

    # -- H. Decay and confidence ---------------------------------------------

    def decayed_rank(
        self,
        idx: str,
        *,
        now: float,
        decay_rate: float,
        limit: int | None,
        base_score_field: str = "",
        confidence: tuple[str, float, float] | None = None,
        validity: tuple[str, str, float] | None = None,
        pretrim_max_ratio: float,
    ) -> list[Any]:
        # The gate-off triples: an empty key disables each feature inside the
        # script (``MODULATION_DISABLED`` / ``VALIDITY_GATE_DISABLED``).
        if confidence is None:
            conf_hash_key, conf_s, conf_c0 = "", "0", "0.5"
        else:
            conf_hash_key, conf_s, conf_c0 = (
                confidence[0],
                str(confidence[1]),
                str(confidence[2]),
            )
        if validity is None:
            gate_invalid_key, gate_valid_key, gate_as_of = "", "", ""
        else:
            # ``as_of`` travels as ``repr(float)`` so the range bound is
            # bit-exact (see the ``as_of_raw`` note inside DECAY_SCORE_LUA).
            gate_invalid_key, gate_valid_key = validity[0], validity[1]
            gate_as_of = repr(float(validity[2]))
        n = limit
        if n is None:
            n = int(get_REDIS_DB().zcard(idx))
            if not n:
                return []

        return run_lua(
            get_REDIS_DB(),
            DECAY_SCORE_LUA,
            # numkeys: zset + confidence (KEYS[2]) + invalid_at (KEYS[3]) +
            # valid_from (KEYS[4]). Passing the validity keys without bumping
            # this would shunt them into ARGV and silently corrupt
            # base_score_field / the confidence params.
            4,
            idx,
            conf_hash_key,
            gate_invalid_key,
            gate_valid_key,
            str(now),
            str(decay_rate),
            str(n),
            base_score_field,
            conf_s,
            conf_c0,
            gate_as_of,  # ARGV[7]
            str(pretrim_max_ratio),  # ARGV[8] (#585): pre-trim budget
        )

    def confidence_update(
        self,
        idx: str,
        member: str,
        signal: float,
        *,
        initial: float,
        cap: int,
        require_record: str | None = None,
        uow: UnitOfWork | None = None,
    ) -> tuple[float, int, int, int] | None:
        if uow is not None:
            # Batched: the Lua EXISTS guard on KEYS[2] replaces the
            # round-trip check below, so N suppressions cost one execute().
            keys = [idx] if require_record is None else [idx, require_record]
            run_lua(
                uow,
                CAPPED_BAYESIAN_UPDATE_LUA,
                len(keys),
                *keys,
                member,
                str(signal),
                str(initial),
                str(cap),
            )
            return None

        if require_record is not None and not get_REDIS_DB().exists(require_record):
            return None

        result = run_lua(
            get_REDIS_DB(),
            CAPPED_BAYESIAN_UPDATE_LUA,
            1,  # number of KEYS
            idx,
            member,
            str(signal),
            str(initial),
            str(cap),
        )
        return (
            float(result[0]),
            int(float(result[1])),
            int(float(result[2])),
            int(float(result[3])),
        )

    # -- I. Validity ---------------------------------------------------------

    def supersede(
        self,
        model_prefix: str,
        field: str,
        *,
        mode: str,
        new_member: str,
        old_member: str = "",
        now: float,
        valid_from: float | None,
        ingested_at: float | None,
        close_at: float | None,
        assert_valid_from: bool,
        pointer_digest: str | None,
        uow: UnitOfWork | None = None,
    ) -> str | None:
        keys = _validity_keys(model_prefix, field)
        pointer_key = (
            f"{model_prefix}:{field}:open:{pointer_digest}" if pointer_digest else ""
        )
        args = [
            keys["valid_from"],  # KEYS[1]
            keys["invalid_at"],  # KEYS[2]
            keys["ingested_at"],  # KEYS[3]
            pointer_key,  # KEYS[4] (may be '')
            keys["chain_fwd"],  # KEYS[5]
            keys["chain_rev"],  # KEYS[6]
            new_member or "",  # ARGV[1]
            repr(float(now)),  # ARGV[2]
            "" if valid_from is None else repr(float(valid_from)),  # ARGV[3]
            "" if ingested_at is None else repr(float(ingested_at)),  # ARGV[4]
            mode,  # ARGV[5]
            "" if close_at is None else repr(float(close_at)),  # ARGV[6]
            old_member or "",  # ARGV[7]
            "1" if assert_valid_from else "",  # ARGV[8]
        ]

        if uow is not None:
            run_lua(uow, SUPERSEDE_LUA, 6, *args)
            return None
        try:
            result = run_lua(get_REDIS_DB(), SUPERSEDE_LUA, 6, *args)
        except redis.exceptions.ResponseError as e:
            # Function-local: ``validity_field`` imports this module for the
            # script text, so a module-scope import here would be a cycle. The
            # typed exceptions stay in ``validity_field``; WS1e moves the map.
            from ..fields.validity_field import map_lua_error

            raise map_lua_error(e) from e
        closed = _as_str(result) if result else ""
        return closed or None

    def interval_of(
        self, valid_idx: str, invalid_idx: str, member: str
    ) -> tuple[float | None, float | None]:
        start = get_REDIS_DB().zscore(valid_idx, member)
        close = get_REDIS_DB().zscore(invalid_idx, member)
        return (
            None if start is None else float(start),
            None if close is None else float(close),
        )

    def interval_members(
        self,
        valid_idx: str,
        invalid_idx: str,
        as_of: float,
        *,
        select: Literal["valid", "excluded"],
    ) -> set[str]:
        t = float(as_of)
        if select == "valid":
            started = get_REDIS_DB().zrangebyscore(valid_idx, "-inf", t)
            still_open = get_REDIS_DB().zrangebyscore(invalid_idx, f"({t}", "+inf")
            return {_as_str(m) for m in started} & {_as_str(m) for m in still_open}
        # invalid_at <= t: already closed. The +inf open sentinel never matches.
        # Read before valid_from, the order the assembler established.
        closed = get_REDIS_DB().zrangebyscore(invalid_idx, "-inf", t)
        # valid_from > t: not yet started.
        future = get_REDIS_DB().zrangebyscore(valid_idx, f"({t}", "+inf")
        return {_as_str(m) for m in closed + future}

    def drop_validity(
        self,
        model_prefix: str,
        field: str,
        member: str,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        keys = _validity_keys(model_prefix, field)

        # Moved from ``ValidityField.find_open_pointers_for_member``: SCAN the
        # pointer keyspace and compare values, because the digest is opaque and
        # there is no record -> digest reverse lookup (plan D1).
        prefix = f"{model_prefix}:{field}"
        stale_pointers = []
        for pointer_key in scan_keys(f"{prefix}:open:*"):
            pointer_key = _as_str(pointer_key)
            current = get_REDIS_DB().get(pointer_key)
            if current is not None and _as_str(current) == member:
                stale_pointers.append(pointer_key)

        if uow is not None:
            uow_any: Any = uow
            uow_any.zrem(keys["valid_from"], member)
            uow_any.zrem(keys["invalid_at"], member)
            uow_any.zrem(keys["ingested_at"], member)
            uow_any.hdel(keys["chain_fwd"], member)
            uow_any.hdel(keys["chain_rev"], member)
            for pointer_key in stale_pointers:
                uow_any.delete(pointer_key)
            return None

        get_REDIS_DB().zrem(keys["valid_from"], member)
        get_REDIS_DB().zrem(keys["invalid_at"], member)
        get_REDIS_DB().zrem(keys["ingested_at"], member)
        get_REDIS_DB().hdel(keys["chain_fwd"], member)
        get_REDIS_DB().hdel(keys["chain_rev"], member)
        result: Any = 0
        for pointer_key in stale_pointers:
            result = get_REDIS_DB().delete(pointer_key)
        return result

    def open_pointer(self, model_prefix: str, field: str, digest: str) -> str | None:
        current = get_REDIS_DB().get(f"{model_prefix}:{field}:open:{digest}")
        return None if current is None else _as_str(current)

    # -- J. Orphan purge and maintenance ------------------------------------

    def purge_orphan(
        self,
        record_key: str,
        refs: Sequence[tuple[str, Literal["sorted", "set"]]],
        *,
        uow: UnitOfWork | None = None,
    ) -> int | None:
        index_keys: list[str] = []
        kinds: list[str] = []
        for idx, kind in refs:
            index_keys.append(idx)
            kinds.append("z" if kind == "sorted" else "s")
        if uow is not None:
            run_lua(
                uow,
                PURGE_ORPHAN_LUA,
                1 + len(index_keys),
                record_key,
                *index_keys,
                *kinds,
            )
            return None
        removed = run_lua(
            get_REDIS_DB(),
            PURGE_ORPHAN_LUA,
            1 + len(index_keys),
            record_key,
            *index_keys,
            *kinds,
        )
        return int(removed)

    def scan_index_members(
        self, idx: str, kind: Literal["sorted", "set"]
    ) -> Iterator[str]:
        cursor = 0
        if kind == "sorted":
            # ZSCAN all members of a Redis sorted set.
            while True:
                cursor, batch = get_REDIS_DB().zscan(idx, cursor, count=1000)
                for member, _score in batch:
                    yield _as_str(member)
                if cursor == 0:
                    break
            return
        # SSCAN all members of a Redis set.
        while True:
            cursor, batch = get_REDIS_DB().sscan(idx, cursor, count=1000)
            for member in batch:
                yield _as_str(member)
            if cursor == 0:
                break

    def drop_index(
        self,
        idx: str,
        kind: Literal["sorted", "set", "map"],
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        # ``kind`` is for Postgres's per-table layout; on Redis the key is the
        # index whatever its type, so one DEL covers all three.
        db: Any = get_REDIS_DB() if uow is None else uow
        reply = db.delete(idx)
        return None if uow is not None else reply
