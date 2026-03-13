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
from .field import Field
from .sorted_field_mixin import SortedFieldMixin

logger = logging.getLogger("POPOTO.DecayingSortedField")

# Lua script: compute decayed scores for all members of a sorted set.
# The sorted set stores members with their last_updated timestamp as score.
# Base scores are read from each member's model hash via cmsgpack.
# Returns top-N member keys ranked by decayed score.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
# ARGV[4] = base_score_field name (empty string = default 1.0)
DECAY_SCORE_LUA = """
local zset_key = KEYS[1]
local now = tonumber(ARGV[1])
local decay_rate = tonumber(ARGV[2])
local max_results = tonumber(ARGV[3])
local base_score_field = ARGV[4]

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
    local decayed = base_score * math.pow(elapsed_days, -decay_rate)

    table.insert(scored, {member, decayed})
end

-- Sort by decayed score descending
table.sort(scored, function(a, b) return a[2] > b[2] end)

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

    Args:
        decay_rate: Controls how fast scores drop. Higher = faster decay.
            Default 0.5. Must be > 0.
        base_score_field: Name of a companion field whose value multiplies
            the decay curve. When None, base score is 1.0.
        partition_by: Partition the sorted set by key field values.
            Inherited from SortedFieldMixin.
    """

    def __init__(self, **kwargs):
        self.decay_rate = kwargs.pop("decay_rate", 0.5)
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
