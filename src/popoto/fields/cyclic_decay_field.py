"""CyclicDecayField — Temporal Rhythms + Homeostatic Pressure.

Extends DecayingSortedField with two additional temporal forces computed
atomically in the same Lua script:

1. **Cyclical resonance**: Periodic boosts following cosine curves.
   A record about Q1 renewals resurfaces every January.

2. **Homeostatic pressure**: Urgency that builds linearly over time
   when an item goes unresolved. Discharged by ``resolve_pressure()``.

The effective score is: ``decay + cyclic_resonance + pressure``

When ``cycles=[]`` and ``pressure_rate=0.0``, behavior is identical to
DecayingSortedField (the Lua script short-circuits on nil HGET lookups).

Companion Redis hashes store per-member cycle and pressure data:
    - ``$CyclicDecayF:{Model}:{field}:{partitions}:cycles`` — msgpack cycle
      tuples, ``[period, amplitude, phase]`` or, once a member has saved under
      this field (#698), ``[period, amplitude, phase, declared_baseline]``.
      The optional 4th slot records the declared amplitude in force when the
      entry was last written, letting ``on_save`` tell "the developer edited
      the declaration" from "learning diverged from the declaration" — see
      ``CyclicDecayField.on_save``.
    - ``$CyclicDecayF:{Model}:{field}:{partitions}:pressure`` — msgpack pressure dict

Example:
    class Directive(Model):
        agent_id = KeyField()
        content = Field(type=str)
        relevance = CyclicDecayField(
            decay_rate=0.5,
            cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
            pressure_rate=0.1,
        )

    top = Directive.query.filter(agent_id="agent-1").top_by_decay("relevance", n=10)
    directive.resolve_pressure("relevance")
"""

import logging
import time
from typing import Any, Optional

import msgpack
import redis

from ..exceptions import ModelException
from ..redis_db import get_REDIS_DB, run_lua
from .decaying_sorted_field import MODULATION_DISABLED, DecayingSortedField

logger = logging.getLogger("POPOTO.CyclicDecayField")

# Extended Lua script: computes decay + cyclic resonance + pressure atomically.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# KEYS[2] = cycles companion hash key (member -> msgpack [[period, amp, phase], ...])
# KEYS[3] = pressure companion hash key (member -> msgpack {rate, last_resolved})
# KEYS[4] = ConfidenceField ":data" companion hash (member -> msgpack payload).
#           Empty string / absent = confidence modulation disabled.
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
# ARGV[4] = base_score_field name (empty string = default 1.0)
# ARGV[5] = confidence modulation strength s (0 / absent = disabled)
# ARGV[6] = c0, the confidence field's initial_confidence (default + centering
#           constant). Same ARGV indices as DECAY_SCORE_LUA; only the KEYS
#           index differs.
CYCLIC_DECAY_LUA = """
local zset_key = KEYS[1]
local cycles_hash_key = KEYS[2]
local pressure_hash_key = KEYS[3]
-- Confidence hash is KEYS[4] HERE, but KEYS[2] in DECAY_SCORE_LUA. The indices
-- differ because this fork already binds KEYS[2] = cycles and KEYS[3] =
-- pressure; the confidence hash is appended after them. Do NOT "unify" the two
-- scripts on KEYS[2]: reusing it here would cmsgpack.unpack the cycles array as
-- a confidence dict -- a silent corrupt read, not a clean crash.
local confidence_hash_key = KEYS[4] or ''
local now = tonumber(ARGV[1])
local decay_rate = tonumber(ARGV[2])
local max_results = tonumber(ARGV[3])
local base_score_field = ARGV[4]
local s = tonumber(ARGV[5]) or 0
local c0 = tonumber(ARGV[6]) or 0.5

-- When modulation is off, never pay for the extra HGET per member.
local modulate = confidence_hash_key ~= '' and s ~= 0

-- Get all members with their last_updated timestamps
local members = redis.call('ZRANGE', zset_key, 0, -1, 'WITHSCORES')

local scored = {}
local two_pi = 2 * math.pi

for i = 1, #members, 2 do
    local member = members[i]
    local last_updated = tonumber(members[i + 1])

    -- Base score from model hash (same as DecayingSortedField)
    local base_score = 1.0
    if base_score_field ~= '' then
        local raw = redis.call('HGET', member, base_score_field)
        if raw then
            local ok, decoded = pcall(cmsgpack.unpack, raw)
            if ok and type(decoded) == 'number' then
                base_score = decoded
            elseif ok and type(decoded) == 'table' and decoded['as_encodable'] then
                base_score = tonumber(decoded['as_encodable']) or 1.0
            end
        end
    end

    -- Power-law decay: base_score * elapsed_days^(-decay_rate)
    local elapsed_days = math.max((now - last_updated) / 86400, 0.01)
    local decayed = base_score * math.pow(elapsed_days, -decay_rate)

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

        -- Correction factor applied on top of the unmodulated decay so that
        -- neutrality is bit-exact: c == c0 gives an exponent of exactly 0 and
        -- math.pow(x, -0) is exactly 1.0.
        --
        -- The math.max(elapsed_days, 1.0) guard is load-bearing, NOT redundant.
        -- elapsed_days is floored at 0.01, and for t < 1 the term t^(-rate) is a
        -- multiplier > 1 that a LARGER rate amplifies MORE (at t=0.01, rate 0.66
        -- gives x21.9 vs x5.0 for rate 0.35). Without the guard, modulation runs
        -- backwards for the first 24 hours and boosts exactly the low-confidence
        -- junk it is meant to bury. Clamping the correction's base to >= 1.0
        -- makes the term exactly 1.0 for fresh records.
        decayed = decayed
            * math.pow(math.max(elapsed_days, 1.0), -(eff - decay_rate))
    end

    -- Cyclical resonance: sum of cosine curves
    local cyclic = 0
    local cycles_raw = redis.call('HGET', cycles_hash_key, member)
    if cycles_raw then
        local ok, cycles = pcall(cmsgpack.unpack, cycles_raw)
        if ok and type(cycles) == 'table' then
            for _, c in ipairs(cycles) do
                -- c = {period, amplitude, phase}
                local period = c[1]
                local amplitude = c[2]
                local phase = c[3] or 0
                if period > 0 then
                    cyclic = cyclic + amplitude * math.cos(two_pi * (now - phase) / period)
                end
            end
        end
    end

    -- Homeostatic pressure: linear urgency buildup
    local pressure = 0
    local pressure_raw = redis.call('HGET', pressure_hash_key, member)
    if pressure_raw then
        local ok, pdata = pcall(cmsgpack.unpack, pressure_raw)
        if ok and type(pdata) == 'table' then
            local rate = pdata['rate'] or pdata[1] or 0
            local last_resolved = pdata['last_resolved'] or pdata[2] or now
            if rate > 0 then
                local unresolved_days = math.max((now - last_resolved) / 86400, 0)
                pressure = rate * unresolved_days
            end
        end
    end

    -- Three-force superposition
    local effective_score = decayed + cyclic + pressure
    table.insert(scored, {member, effective_score})
end

-- Two-level total-order comparator. Lua 5.1 table.sort is unstable and
-- members are collected from ZRANGE (index order), so a score-only comparator
-- leaves equal-scored members in undefined order -- including across the
-- max_results truncation boundary below. Tie-break on a[1] (the member's full
-- redis_key): sorted-set members are unique by definition, so distinct entries
-- always have unequal key strings, giving a strict weak ordering. The
-- three-force effective_score is a finite sum (decay + cyclic + pressure), so
-- a[2] ~= b[2] behaves as a normal total order (no NaN).
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

# Atomic read-modify-write of both companion hashes for one on_save() call
# (#699). Replaces two client-side HGET-then-HSET round trips (one per hash)
# with one server-side script, closing the lost-update race between save()
# and strengthen_cycle()/weaken_cycle() (cycles) and between save() and
# resolve_pressure() (pressure).
#
# KEYS[1] = cycles companion hash key
# KEYS[2] = pressure companion hash key
# ARGV[1] = member key
# ARGV[2] = declared cycle count N (0 => HDEL the cycles entry)
# ARGV[3 .. 3N+2] = N triples: period, declared_amplitude, phase
# ARGV[3N+3] = pressure_rate (<= 0 => HDEL the pressure entry)
# ARGV[3N+4] = now (only used on a pressure first-save)
#
# Returns cmsgpack.pack({decode_failed, resets}) where resets is an array of
# {period, old_baseline, declared_amplitude, discarded_amplitude} tuples --
# one per period where an edited declaration won the #698 merge and a
# learned amplitude was discarded. Python turns decode_failed into the
# existing "Could not decode cycles" warning and each reset into the
# existing #698 logger.info reset line -- both signals move server-side but
# stay observable exactly as before.
#
# Every ARGV-sourced numeric slot (period, amplitude, phase,
# declared_baseline) is a Lua STRING and must be converted before it is
# written back. Redis also converts a bare Lua number to an integer over the
# wire, so the return value is always cmsgpack-packed, never a raw table.
CYCLES_MERGE_LUA = """
local cycles_hash_key = KEYS[1]
local pressure_hash_key = KEYS[2]
local member = ARGV[1]
local n = tonumber(ARGV[2])

-- Type-safe match key: a period may legitimately be a string (a named
-- TemporalPeriod alias), and Lua's default tostring() on a float does not
-- agree with Python's repr(), so numeric and string periods are normalized
-- through one function on both the stored and declared sides.
local function match_key(v)
    local num = tonumber(v)
    if num then
        return 'n:' .. string.format('%.17g', num)
    end
    return 's:' .. tostring(v)
end

-- ARGV is always a Lua string. A numeric-looking period becomes a number
-- (matching what Python originally stored); a genuinely non-numeric period
-- stays a string. Never store the raw ARGV string or the match key itself.
local function coerce_period(raw)
    local num = tonumber(raw)
    if num then
        return num
    end
    return raw
end

local decode_failed = 0
local resets = {}
local output = {}

if n > 0 then
    -- learned[match_key] = FIFO queue of {amp, baseline} pairs, one per
    -- stored entry for that period -- mirrors Python's dict-of-lists bucket.
    local learned = {}
    local raw = redis.call('HGET', cycles_hash_key, member)
    if raw then
        local ok, stored = pcall(cmsgpack.unpack, raw)
        if ok and type(stored) == 'table' then
            for _, entry in ipairs(stored) do
                if type(entry) == 'table' and entry[1] ~= nil and entry[2] ~= nil then
                    local p = entry[1]
                    -- A table-valued (unhashable) period has no Lua
                    -- analogue of Python's TypeError -- nothing raises, so
                    -- this must be an explicit type guard, not a pcall. On
                    -- trip, discard the WHOLE bucket (not just this entry):
                    -- that is what makes one malformed entry take an
                    -- earlier well-formed entry down with it, matching the
                    -- Python except-clause's all-or-nothing fallback.
                    if type(p) ~= 'number' and type(p) ~= 'string' then
                        decode_failed = 1
                        learned = {}
                        break
                    end
                    local key = match_key(p)
                    learned[key] = learned[key] or {}
                    -- Booleans decode from msgpack as Lua booleans, so
                    -- type(entry[4]) == 'number' already excludes them --
                    -- same effect as Python's isinstance(x, bool) guard,
                    -- different mechanism.
                    local baseline = nil
                    if type(entry[4]) == 'number' then
                        baseline = entry[4]
                    end
                    table.insert(learned[key], {amp = entry[2], baseline = baseline})
                end
            end
        else
            decode_failed = 1
        end
    end

    for i = 0, n - 1 do
        local idx = 3 + i * 3
        local period_raw = ARGV[idx]
        local declared_amp = tonumber(ARGV[idx + 1])
        local phase = tonumber(ARGV[idx + 2]) or 0
        local key = match_key(period_raw)
        local amplitude = declared_amp
        local new_baseline = declared_amp

        local bucket = learned[key]
        if bucket and #bucket > 0 then
            -- FIFO within duplicate periods: pop the front.
            local picked = table.remove(bucket, 1)
            if picked.baseline == nil then
                -- Baseline unknown (legacy entry, or a bucket cleared by
                -- the guard above) -- preserve the learned amplitude.
                amplitude = picked.amp
            elseif picked.baseline == declared_amp then
                -- Declaration unchanged -- learning wins.
                amplitude = picked.amp
            else
                -- The developer edited the declared amplitude -- the
                -- declaration wins and the learned amplitude is discarded,
                -- reported so Python can log it loudly.
                amplitude = declared_amp
                table.insert(resets, {
                    coerce_period(period_raw), picked.baseline, declared_amp, picked.amp
                })
            end
        end

        table.insert(output, {
            coerce_period(period_raw),
            tonumber(amplitude),
            phase,
            tonumber(new_baseline),
        })
    end

    redis.call('HSET', cycles_hash_key, member, cmsgpack.pack(output))
else
    redis.call('HDEL', cycles_hash_key, member)
end

-- Pressure: rate is declarative, last_resolved is learned. This is the
-- read-modify-write being fixed -- resolve_pressure() is a blind HSET and
-- stays untouched (spike-4).
local pressure_rate = tonumber(ARGV[3 * n + 3])
local now = tonumber(ARGV[3 * n + 4])

if pressure_rate and pressure_rate > 0 then
    local praw = redis.call('HGET', pressure_hash_key, member)
    if praw then
        local pdata = cmsgpack.unpack(praw)
        pdata['rate'] = pressure_rate
        redis.call('HSET', pressure_hash_key, member, cmsgpack.pack(pdata))
    else
        redis.call('HSET', pressure_hash_key, member, cmsgpack.pack({
            rate = pressure_rate,
            last_resolved = now,
        }))
    end
else
    redis.call('HDEL', pressure_hash_key, member)
end

return cmsgpack.pack({decode_failed, resets})
"""

# Atomic read-modify-write of the cycles companion hash for
# strengthen_cycle()/weaken_cycle() (#699). Slot numbering here is 1-based
# Lua throughout (matching CYCLES_MERGE_LUA above), NOT the 0-based Python
# indexing used in Model._adjust_cycle_amplitudes's comments (critique C5):
# this script mutates only c[2] (amplitude) of each entry and re-packs the
# same tables, so an optional c[4] (declared_baseline, #698) survives
# untouched -- see Model._adjust_cycle_amplitudes.
#
# KEYS[1] = cycles companion hash key
# ARGV[1] = member key
# ARGV[2] = factor
# ARGV[3] = max_amplitude
# ARGV[4] = min_threshold
#
# Returns cmsgpack.pack(cycles) after the write, or Lua nil (-> Python None)
# when the member has no stored entry -- the existence check this replaces
# (`if not raw: return []`) moves into the script, so the no-entry case must
# be distinguishable from an empty msgpack payload. nil is the only shape
# that carries no numbers to truncate over the protocol; every other return
# is cmsgpack-packed.
CYCLES_ADJUST_LUA = """
local cycles_hash_key = KEYS[1]
local member = ARGV[1]
local factor = tonumber(ARGV[2])
local max_amplitude = tonumber(ARGV[3])
local min_threshold = tonumber(ARGV[4])

local raw = redis.call('HGET', cycles_hash_key, member)
if not raw then
    return nil
end

local cycles = cmsgpack.unpack(raw)
for _, cycle in ipairs(cycles) do
    local new_amp = cycle[2] * factor
    if new_amp < 0 then
        new_amp = 0
    elseif new_amp > max_amplitude then
        new_amp = max_amplitude
    end
    if new_amp < min_threshold then
        new_amp = 0
    end
    cycle[2] = new_amp
end

local packed = cmsgpack.pack(cycles)
redis.call('HSET', cycles_hash_key, member, packed)
return packed
"""


class CyclicDecayField(DecayingSortedField):
    """A DecayingSortedField with cyclical resonance and homeostatic pressure.

    Extends the parent's power-law decay with two additional forces:

    1. **Cyclical resonance** via ``cycles`` parameter: each cycle is a
       ``(period, amplitude, phase)`` tuple defining a cosine curve.
       The resonance contribution is ``amplitude * cos(2*pi*(now-phase)/period)``.

    2. **Homeostatic pressure** via ``pressure_rate``: linearly increasing
       urgency. Pressure = ``pressure_rate * unresolved_days``.
       Reset by calling ``model.resolve_pressure(field_name)``.

    When ``cycles=[]`` and ``pressure_rate=0.0``, behavior is identical
    to ``DecayingSortedField``.

    Ranking is deterministic: equal effective-scored members are ordered
    by member key (redis_key) ascending, byte-wise, broken inside the Lua
    script before top-N truncation.

    Args:
        decay_rate: Controls how fast scores drop. Higher = faster decay.
            Default 0.5. Must be > 0. (Inherited from DecayingSortedField.)
        base_score_field: Name of a companion field whose value multiplies
            the decay curve. When None, base score is 1.0. (Inherited.)
        cycles: List of ``(period, amplitude, phase)`` tuples defining
            cyclical resonance curves. ``period`` is in seconds (use
            ``TemporalPeriod`` constants). ``amplitude`` is the peak boost.
            ``phase`` is a time offset in seconds. Default ``[]``.
        pressure_rate: Rate at which urgency builds per unresolved day.
            Default ``0.0`` (no pressure). Must be >= 0.
        partition_by: Partition the sorted set by key field values.
            Inherited from SortedFieldMixin.

    Example:
        from popoto.fields.constants import TemporalPeriod

        class Directive(Model):
            agent_id = KeyField()
            content = Field(type=str)
            relevance = CyclicDecayField(
                decay_rate=0.5,
                cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
                pressure_rate=0.1,
            )
    """

    # Export/import: two companion hashes hold state a plain re-save cannot
    # reconstruct. Per-member cycle amplitudes are LEARNED (mutated by
    # strengthen_cycle / weaken_cycle) and diverge from the class-level
    # ``cycles`` defaults; ``pressure.last_resolved`` is genuine independent
    # state whose age is the whole point of homeostatic pressure.
    roundtrip_policy: str = "carry"

    @classmethod
    def export_state(cls, model_instance, field_name, field_value, **kwargs):
        """Export the per-member cycles and pressure companion data.

        Returns:
            ``{"cycles": [[period, amplitude, phase], ...],
                "pressure": {"rate": float, "last_resolved": float}}``
            with either key omitted when that companion hash has no entry for
            this instance, or ``None`` when neither does.
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, CyclicDecayField):
            return None

        member_key = model_instance.db_key.redis_key
        state = {}

        cycles_raw = get_REDIS_DB().hget(
            field.get_cycles_hash_key(model_instance, field_name), member_key
        )
        if cycles_raw:
            try:
                cycles = msgpack.unpackb(cycles_raw, raw=False)
            except Exception:
                logger.warning(
                    f"Could not decode cycles data for {member_key}; "
                    f"skipping cycles export of {field_name}"
                )
                cycles = None
            if isinstance(cycles, (list, tuple)):
                normalized = []
                for cycle in cycles:
                    cycle = list(cycle)
                    # #699 Risk 1: integral amplitudes/phases round-trip from
                    # the Lua scripts as msgpack integers (Lua 5.1 has one
                    # number type). Coerce whole-number slots back to float
                    # here so exporters see the same types as pre-#699;
                    # period (slot 0) is left alone since it may
                    # legitimately be a string. Only int values are
                    # touched — floats, strings and bools pass through, so
                    # a malformed entry can never raise out of this read
                    # path.
                    if (
                        len(cycle) > 1
                        and isinstance(cycle[1], int)
                        and not isinstance(cycle[1], bool)
                    ):
                        cycle[1] = float(cycle[1])
                    if (
                        len(cycle) > 2
                        and isinstance(cycle[2], int)
                        and not isinstance(cycle[2], bool)
                    ):
                        cycle[2] = float(cycle[2])
                    normalized.append(cycle)
                state["cycles"] = normalized

        pressure_raw = get_REDIS_DB().hget(
            field.get_pressure_hash_key(model_instance, field_name), member_key
        )
        if pressure_raw:
            try:
                pressure = msgpack.unpackb(pressure_raw, raw=False)
            except Exception:
                logger.warning(
                    f"Could not decode pressure data for {member_key}; "
                    f"skipping pressure export of {field_name}"
                )
                pressure = None
            if isinstance(pressure, dict):
                state["pressure"] = {
                    "rate": float(pressure.get("rate", 0.0) or 0.0),
                    "last_resolved": float(pressure.get("last_resolved", 0.0) or 0.0),
                }

        return state or None

    @classmethod
    def import_state(cls, model_instance, field_name, state, **kwargs):
        """Restore per-member cycles and pressure companion data after import.

        Ordering note -- the transfer driver calls ``import_state`` *after*
        ``save()``, and that is still required, though since #679 the reason
        has narrowed.

        ``on_save`` no longer clobbers learned amplitudes; it preserves
        whatever is already stored for the member. But on an import the record
        is *new*, so there is nothing stored yet and ``on_save`` legitimately
        writes the class-level defaults -- and it seeds
        ``pressure.last_resolved`` to ``now`` whenever the entry is fresh,
        which is exactly the value the import must replace. So these writes
        still have to land on top of ``on_save``'s, and inverting the order
        would still silently discard both the imported amplitudes and the
        accumulated pressure age.

        **Cycle entries deliberately do not carry the declared-baseline slot
        (#698).** A stored cycle entry may have a 4th element,
        ``declared_baseline`` — the declared amplitude in force in the
        deployment that *wrote* the entry (see ``on_save``). That is
        deployment-local by definition, so this method rebuilds every cycle as
        a 3-element ``[period, amplitude, phase]`` and never carries slot 3
        through, even when the exported state has it. Carrying the exporter's
        baseline into a target whose ``field.cycles`` declares a different
        amplitude for that period would make the first post-import ``save()``
        see ``baseline != declared`` and fire a reset — destroying exactly the
        learned amplitude ``roundtrip_policy = "carry"`` exists to preserve,
        with no import-time signal. Dropping it instead means the imported
        entry is "baseline unknown": it preserves the learned amplitude
        unconditionally and acquires a fresh baseline, from the *importing*
        deployment's declaration, on its next ordinary save. The cost is one
        missed detection window if the declaration was also edited between
        export and that first post-import save — the same trade already
        accepted for a pre-upgrade record (see ``on_save``'s Risk 2 in
        docs/plans/sdlc-698.md) and self-healing the same way.
        """
        if not state:
            return None

        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, CyclicDecayField):
            return None

        member_key = model_instance.db_key.redis_key

        cycles = state.get("cycles")
        if cycles:
            normalized = []
            for cycle in cycles:
                cycle = list(cycle)
                period, amplitude = cycle[0], cycle[1]
                phase = cycle[2] if len(cycle) > 2 else 0
                # Deliberately 3-element, not widened to carry a 4th slot
                # (#698 / see the docstring above): the declared baseline is
                # deployment-local and must never come from the exporter.
                normalized.append([period, amplitude, phase])
            get_REDIS_DB().hset(
                field.get_cycles_hash_key(model_instance, field_name),
                member_key,
                msgpack.packb(normalized),
            )

        pressure = state.get("pressure")
        if pressure:
            get_REDIS_DB().hset(
                field.get_pressure_hash_key(model_instance, field_name),
                member_key,
                msgpack.packb(
                    {
                        "rate": float(pressure.get("rate", 0.0) or 0.0),
                        "last_resolved": float(
                            pressure.get("last_resolved", 0.0) or 0.0
                        ),
                    }
                ),
            )
        return None

    def __init__(self, **kwargs):
        self.cycles = kwargs.pop("cycles", [])
        self.pressure_rate = kwargs.pop("pressure_rate", 0.0)

        # Validate cycles
        for cycle in self.cycles:
            if len(cycle) < 2 or len(cycle) > 3:
                raise ModelException(
                    f"Each cycle must be (period, amplitude) or "
                    f"(period, amplitude, phase), got {cycle}"
                )
            period, amplitude = cycle[0], cycle[1]
            if period <= 0:
                raise ModelException(f"Cycle period must be > 0 (got {period})")
            if amplitude < 0:
                raise ModelException(f"Cycle amplitude must be >= 0 (got {amplitude})")

        # Validate pressure_rate
        if self.pressure_rate < 0:
            raise ModelException(
                f"pressure_rate must be >= 0 (got {self.pressure_rate})"
            )

        super().__init__(**kwargs)

    def get_cycles_hash_key(self, model_instance, field_name):
        """Build the Redis key for the cycles companion hash.

        Public API for external callers that need direct Redis access to
        cycle data (e.g., bulk inspection, custom cycle updates, monitoring).

        Pattern: $CyclicDecayF:{Model}:{field}:{partitions}:cycles
        """
        ss_key = self.get_partitioned_sortedset_db_key(model_instance, field_name)
        return ss_key.redis_key + ":cycles"

    def get_pressure_hash_key(self, model_instance, field_name):
        """Build the Redis key for the pressure companion hash.

        Public API for external callers that need direct Redis access to
        pressure data (e.g., bulk pressure resets, monitoring dashboards).

        Pattern: $CyclicDecayF:{Model}:{field}:{partitions}:pressure
        """
        ss_key = self.get_partitioned_sortedset_db_key(model_instance, field_name)
        return ss_key.redis_key + ":pressure"

    def rank_decayed(
        self,
        zset_key: str,
        *,
        now: float,
        n: Optional[int] = None,
        confidence: Optional[tuple[str, str, str]] = None,
        validity: Optional[tuple[str, str, str]] = None,
        decay_rate: Optional[float] = None,
        base_score_field: Optional[str] = None,
    ) -> "list[Any]":
        """Evaluate the cyclic decay script over one sorted set (#648).

        Overrides :meth:`DecayingSortedField.rank_decayed` because this fork of
        the decay math uses an **incompatible KEYS layout**: cycles at
        ``KEYS[2]`` and pressure at ``KEYS[3]``, which pushes the confidence
        hash to ``KEYS[4]``. Both scripts carry a comment forbidding a "unify"
        on ``KEYS[2]`` -- reusing index 2 here would ``cmsgpack.unpack`` the
        cycles array as a confidence dict, a silent corrupt read rather than a
        clean crash. Keeping the two layouts in two class bodies, rather than
        behind a flag in one, is what makes that mistake unavailable instead of
        merely discouraged.

        The companion hash keys are the partition ZSET key plus a suffix (the
        same derivation as :meth:`get_cycles_hash_key` /
        :meth:`get_cycles_hash_key_from_parts`), so they follow from
        ``zset_key`` alone. The confidence hash does not -- it lives under its
        own ``$ConfidencF:`` prefix -- so it arrives resolved in ``confidence``.

        ``validity`` is accepted and **deliberately ignored**: ``KEYS`` 1-4 are
        taken here and the script's header forbids renumbering, so this script
        has no validity gate. That gap is an explicit No-Go, pinned by
        ``tests/test_validity_field.py::TestCyclicDecayGatingGap`` and recorded
        under "Known limitations" in
        ``docs/features/validity-and-supersession.md``. The parameter is kept in
        the signature so callers stay polymorphic; if you ever gate this script,
        update all three places.

        Args and return value are otherwise as
        :meth:`DecayingSortedField.rank_decayed`.
        """
        conf_hash_key, conf_s, conf_c0 = (
            MODULATION_DISABLED if confidence is None else confidence
        )
        if n is None:
            n = int(get_REDIS_DB().zcard(zset_key))
            if not n:
                return []
        effective_rate = self.decay_rate if decay_rate is None else decay_rate
        if base_score_field is None:
            base_score_field = self.base_score_field or ""

        return run_lua(
            get_REDIS_DB(),
            CYCLIC_DECAY_LUA,
            # numkeys: zset + cycles + pressure + confidence (KEYS[4]).
            # Passing the confidence key without bumping this would shunt it
            # into ARGV and silently disable modulation.
            4,
            zset_key,
            zset_key + ":cycles",
            zset_key + ":pressure",
            conf_hash_key,
            str(now),
            str(effective_rate),
            str(n),
            base_score_field,
            conf_s,
            conf_c0,
        )

    @classmethod
    def get_cycles_hash_key_from_parts(cls, model_class, field_name, *partition_values):
        """Build cycles hash key from model class and explicit partition values.

        Public API for query paths and external callers that have partition
        values but not a model instance.
        """
        ss_key = cls.get_sortedset_db_key(model_class, field_name, *partition_values)
        return ss_key.redis_key + ":cycles"

    @classmethod
    def get_pressure_hash_key_from_parts(
        cls, model_class, field_name, *partition_values
    ):
        """Build pressure hash key from model class and explicit partition values.

        Public API for query paths and external callers that have partition
        values but not a model instance.
        """
        ss_key = cls.get_sortedset_db_key(model_class, field_name, *partition_values)
        return ss_key.redis_key + ":pressure"

    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        """Store timestamp (parent) then store cycle/pressure companion data.

        Both companion hashes follow the same rule: **declared parameters are
        refreshed from the field; learned state is preserved.**

        For cycles, ``period`` and ``phase`` are declarative and re-read from
        ``field.cycles`` on every save. ``amplitude`` is learned (mutated by
        ``strengthen_cycle`` / ``weaken_cycle``) but is now merged with a
        **three-way rule** rather than a two-way one (#698): each stored entry
        may carry an optional 4th slot, ``declared_baseline`` — the declared
        amplitude that was in force the last time ``on_save`` wrote this entry.
        Comparing the *incoming* declared amplitude against that baseline (not
        against the learned value) distinguishes "the developer edited the
        declaration" from "learning diverged from the declaration":

        - baseline absent (a legacy 3-element entry, or a non-numeric slot 3) →
          "baseline unknown" → preserve the learned amplitude exactly as before
          #698, and record the declared amplitude as the new baseline.
        - baseline equals the incoming declared amplitude → the declaration has
          not moved → preserve the learned amplitude, re-record the same
          baseline.
        - baseline differs from the incoming declared amplitude → the developer
          edited the declaration → **discard the learned amplitude**, adopt the
          declared value, record it as the new baseline, and emit one
          ``logger.info`` naming the model, field, member key, period, old
          baseline, new declared value and the discarded learned amplitude.

        The comparison is exact float equality, never a tolerance — both sides
        are the same Python float, round-tripped through msgpack, which
        preserves IEEE doubles exactly. Stored cycles are matched to declared
        cycles by period, FIFO within duplicate periods, with the amplitude and
        its baseline popped together as one decision; a declared period with
        nothing stored takes the declared amplitude (baseline unknown), and a
        stored period no longer declared is dropped. Before #679 this branch
        overwrote the whole entry with the declared defaults, silently erasing
        everything the strengthen/weaken calls had accumulated; #679 then made
        the merge unconditional the other way, so an edited declaration could
        never win. #698 adds the missing third input (the baseline) so the
        merge can tell the two cases apart. A record with no recorded baseline
        needs two saves to honor an edit made after this change ships: the
        first save records the baseline, and only an edit made after that save
        is detected — see the field's module docs / docs/features/cyclic-decay-field.md.

        For pressure, ``rate`` is declarative and ``last_resolved`` is learned:
        on first save (no existing entry) the full dict is written with
        ``last_resolved=now``; on subsequent saves only the rate is updated.

        Both companion hashes are read-modified-written by one Lua script,
        ``CYCLES_MERGE_LUA`` (#699), run eagerly against the live connection
        regardless of a caller-supplied ``pipeline`` — the merge decision
        (which amplitude/rate wins) has to be known synchronously to emit the
        #698 reset log, and a pipeline-queued script's return value is not
        available until ``execute()``, which ``save()`` does not surface.
        Running it eagerly is also what closes the race this exists for:
        without it, two concurrent ``save()`` calls — or a ``save()`` racing
        ``strengthen_cycle()``/``weaken_cycle()``/``resolve_pressure()`` —
        each read-then-write the hash independently and the last writer
        clobbers the other's update; the script makes the whole
        read-decide-write sequence one atomic server-side step.
        """
        # Call parent to store timestamp in sorted set
        result = super().on_save(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )

        field = model_instance._meta.fields[field_name]
        if not isinstance(field, CyclicDecayField):
            return result

        member_key = model_instance.db_key.redis_key
        cycles_hash_key = field.get_cycles_hash_key(model_instance, field_name)
        pressure_hash_key = field.get_pressure_hash_key(model_instance, field_name)

        # Build ARGV: member, N, then N (period, amplitude, phase) triples,
        # then pressure_rate, then now. Periods may be TemporalPeriod
        # aliases (strings) or raw numbers — EVAL only accepts string/number
        # ARGV, so every slot is stringified; the script's coerce_period()
        # converts numeric-looking strings back before storage (B1).
        argv: list[Any] = [member_key, str(len(field.cycles))]
        for cycle in field.cycles:
            period, declared_amplitude = cycle[0], cycle[1]
            phase = cycle[2] if len(cycle) > 2 else 0
            argv.extend([str(period), str(declared_amplitude), str(phase)])
        argv.append(str(field.pressure_rate))
        argv.append(str(time.time()))

        packed_report = run_lua(
            get_REDIS_DB(),
            CYCLES_MERGE_LUA,
            2,
            cycles_hash_key,
            pressure_hash_key,
            *argv,
        )
        decode_failed, resets = msgpack.unpackb(packed_report, raw=False)

        if decode_failed:
            # Mirror export_state's handler: #679 exists because this state
            # was destroyed silently. Do not add a second mute path — fall
            # back to declared defaults, but say so.
            logger.warning(
                f"Could not decode cycles data for {member_key}; "
                f"falling back to declared amplitudes for {field_name}"
            )

        for period, old_baseline, declared_amplitude, learned_amplitude in resets:
            # cmsgpack packs an integral Lua number as a msgpack integer
            # (Lua 5.1 has one number type), so a whole-number amplitude or
            # baseline round-trips as int rather than float. Coerce back to
            # float here so the log line matches the pre-#699 Python-float
            # formatting; period is left alone since it may legitimately be
            # a non-numeric string. Coercion is display-only and defensive:
            # a hand-written/migrated payload can carry a non-numeric value
            # that still decodes as valid msgpack, and the pre-#699 code
            # logged such values fine — never raise out of save() here.
            try:
                old_baseline = float(old_baseline)
                declared_amplitude = float(declared_amplitude)
                learned_amplitude = float(learned_amplitude)
            except (TypeError, ValueError):
                pass
            # The developer edited the declared amplitude — the declaration
            # wins. The learned amplitude is discarded by design (#698); this
            # destroys real state, so it is logged loudly.
            logger.info(
                f"CyclicDecayField declared amplitude changed for "
                f"{model_instance.__class__.__name__}.{field_name} "
                f"member={member_key} period={period!r}: "
                f"declared baseline {old_baseline!r} -> "
                f"{declared_amplitude!r}; discarded learned "
                f"amplitude {learned_amplitude!r}"
            )

        return result

    @classmethod
    def on_delete(
        cls, model_instance, field_name, field_value, pipeline=None, **kwargs
    ):
        """Remove companion hash entries then delegate to parent."""
        field = model_instance._meta.fields[field_name]

        if isinstance(field, CyclicDecayField):
            member_key = (
                kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
            )
            cycles_hash_key = field.get_cycles_hash_key(model_instance, field_name)
            pressure_hash_key = field.get_pressure_hash_key(model_instance, field_name)

            db = (
                pipeline
                if isinstance(pipeline, redis.client.Pipeline)
                else get_REDIS_DB()
            )
            db.hdel(cycles_hash_key, member_key)
            db.hdel(pressure_hash_key, member_key)

        # Delegate to parent for sorted set cleanup
        return super().on_delete(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )
