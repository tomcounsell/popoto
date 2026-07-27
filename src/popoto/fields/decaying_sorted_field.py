"""DecayingSortedField — time-weighted scoring via Lua decay computation.

This module provides a SortedField subclass where records lose relevance
over time following power-law decay: base_score * elapsed_days^(-decay_rate).

The sorted set stores timestamps as scores. A Lua script computes
decay-ranked results at query time, reading base scores from each
member's model hash via cmsgpack.

Design:
    - Timestamps are always stored as scores (auto_now=True behavior)
    - Decay computation happens server-side in Lua (no round trips)
    - Base scores come from a companion field on the same model hash
    - When base_score_field is None, all items have equal base score (1.0)

Example:
    class Memory(Model):
        key = UniqueKeyField()
        content = StringField()
        strength = FloatField(default=1.0)
        last_accessed = DecayingSortedField(
            decay_rate=0.5,
            base_score_field="strength",
        )

    # Query top-10 memories by decayed relevance
    top = Memory.query.top_by_decay("last_accessed", n=10)

    # Refresh a memory's decay clock without full save
    memory.touch("last_accessed")
"""

import logging

from ..exceptions import ModelException
from .constants import Defaults
from .field import Field
from .sorted_field_mixin import SortedFieldMixin

logger = logging.getLogger("POPOTO.DecayingSortedField")

# Lua script: compute decayed scores for all members of a sorted set.
# The sorted set stores members with their last_updated timestamp as score.
# Base scores are read from each member's model hash via cmsgpack.
# Returns top-N member keys ranked by decayed score.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# KEYS[2] = ConfidenceField ":data" companion hash (member -> msgpack payload).
#           Empty string / absent = confidence modulation disabled.
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
# ARGV[4] = base_score_field name (empty string = default 1.0)
# ARGV[5] = confidence modulation strength s (0 / absent = disabled)
# ARGV[6] = c0, the confidence field's initial_confidence. Serves as BOTH the
#           default for members with no confidence data AND the centering
#           constant, so a zero-evidence record is bit-exactly neutral for any
#           configured initial_confidence (not just 0.5).
DECAY_SCORE_LUA = """
local zset_key = KEYS[1]
-- Confidence hash is KEYS[2] *in this script only*. The CyclicDecayField fork
-- of this math binds KEYS[2] = cycles and KEYS[3] = pressure, so its confidence
-- hash is KEYS[4]. The indices are deliberately different -- do not "unify"
-- them: reusing KEYS[2] there would cmsgpack.unpack the cycles array as a
-- confidence dict, which corrupts silently instead of erroring.
local confidence_hash_key = KEYS[2] or ''
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
for i = 1, #members, 2 do
    local member = members[i]
    local last_updated = tonumber(members[i + 1])

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

        -- Correction factor, applied on top of TODAY'S formula so neutrality is
        -- bit-exact: when c == c0 the exponent is exactly 0 and math.pow(x, -0)
        -- is exactly 1.0.
        --
        -- The math.max(elapsed_days, 1.0) guard is load-bearing, NOT redundant.
        -- elapsed_days is floored at 0.01, and for t < 1 the term t^(-rate) is a
        -- multiplier > 1 that a LARGER rate amplifies MORE (at t=0.01, rate 0.66
        -- gives x21.9 vs x5.0 for rate 0.35). Without the guard, modulation runs
        -- backwards for the first 24 hours and boosts exactly the low-confidence
        -- junk it is meant to bury -- and since agent memory is touched
        -- constantly, most of the working set lives in that region. Clamping the
        -- correction's base to >= 1.0 makes the term exactly 1.0 for fresh
        -- records, so modulation only ever applies in the region where a higher
        -- rate means a lower score.
        decayed = decayed
            * math.pow(math.max(elapsed_days, 1.0), -(eff - decay_rate))
    end

    table.insert(scored, {member, decayed})
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


class DecayingSortedField(SortedFieldMixin, Field):
    """A SortedField subclass where records lose relevance over time.

    Stores timestamps as sorted set scores. A Lua script computes
    decay-ranked results at query time using power-law decay:
        decayed_score = base_score * elapsed_days ^ (-decay_rate)

    With decay_rate=0.5, a record scores 1.0 after 1 day, 0.5 after
    4 days, and 0.1 after 100 days.

    Ranking is deterministic: equal-scored members are ordered by member
    key (redis_key) ascending, byte-wise, broken inside the Lua script
    before top-N truncation.

    Args:
        decay_rate: Controls how fast scores drop. Higher = faster decay.
            Defaults to ``Defaults.DECAY_RATE`` (0.1). Must be > 0.
        base_score_field: Name of a companion field whose value multiplies
            the decay curve. When None, base score is 1.0.
        partition_by: Partition the sorted set by key field values.
            Inherited from SortedFieldMixin.
    """

    def __init__(self, **kwargs):
        decay_rate = kwargs.pop("decay_rate", None)
        self.decay_rate = decay_rate if decay_rate is not None else Defaults.DECAY_RATE
        self.base_score_field = kwargs.pop("base_score_field", None)

        if self.decay_rate <= 0:
            raise ModelException(
                "decay_rate must be > 0 (got {})".format(self.decay_rate)
            )

        # Force type=float and auto_now=True behavior
        kwargs["type"] = float
        kwargs["auto_now"] = True
        kwargs["sorted"] = True
        super().__init__(**kwargs)
