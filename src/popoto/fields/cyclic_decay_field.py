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
    - ``$CyclicDecayF:{Model}:{field}:{partitions}:cycles`` — msgpack cycle tuples
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

import msgpack
import redis

from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB
from .decaying_sorted_field import DecayingSortedField

logger = logging.getLogger("POPOTO.CyclicDecayField")

# Extended Lua script: computes decay + cyclic resonance + pressure atomically.
#
# KEYS[1] = sorted set key (member -> last_updated_timestamp)
# KEYS[2] = cycles companion hash key (member -> msgpack [[period, amp, phase], ...])
# KEYS[3] = pressure companion hash key (member -> msgpack {rate, last_resolved})
# ARGV[1] = current timestamp (seconds)
# ARGV[2] = decay rate (e.g. 0.5)
# ARGV[3] = max results to return
# ARGV[4] = base_score_field name (empty string = default 1.0)
CYCLIC_DECAY_LUA = """
local zset_key = KEYS[1]
local cycles_hash_key = KEYS[2]
local pressure_hash_key = KEYS[3]
local now = tonumber(ARGV[1])
local decay_rate = tonumber(ARGV[2])
local max_results = tonumber(ARGV[3])
local base_score_field = ARGV[4]

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

-- Sort by effective score descending
table.sort(scored, function(a, b) return a[2] > b[2] end)

-- Return top-N as flat array: [member1, score1, member2, score2, ...]
local result = {}
for i = 1, math.min(max_results, #scored) do
    table.insert(result, scored[i][1])
    table.insert(result, tostring(scored[i][2]))
end
return result
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

        Pattern: $CyclicDecayF:{Model}:{field}:{partitions}:cycles
        """
        ss_key = self.get_partitioned_sortedset_db_key(model_instance, field_name)
        return ss_key.redis_key + ":cycles"

    def get_pressure_hash_key(self, model_instance, field_name):
        """Build the Redis key for the pressure companion hash.

        Pattern: $CyclicDecayF:{Model}:{field}:{partitions}:pressure
        """
        ss_key = self.get_partitioned_sortedset_db_key(model_instance, field_name)
        return ss_key.redis_key + ":pressure"

    @classmethod
    def get_cycles_hash_key_from_parts(
        cls, model_class, field_name, *partition_values
    ):
        """Build cycles hash key from model class and partition values."""
        ss_key = cls.get_sortedset_db_key(model_class, field_name, *partition_values)
        return ss_key.redis_key + ":cycles"

    @classmethod
    def get_pressure_hash_key_from_parts(
        cls, model_class, field_name, *partition_values
    ):
        """Build pressure hash key from model class and partition values."""
        ss_key = cls.get_sortedset_db_key(model_class, field_name, *partition_values)
        return ss_key.redis_key + ":pressure"

    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        """Store timestamp (parent) then store cycle/pressure companion data.

        On first save (no existing entry in pressure hash), writes the full
        pressure dict with last_resolved=now. On subsequent saves, only
        updates the rate — never overwrites last_resolved.
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

        # Normalize cycles to 3-tuples for storage
        normalized_cycles = []
        for cycle in field.cycles:
            period, amplitude = cycle[0], cycle[1]
            phase = cycle[2] if len(cycle) > 2 else 0
            normalized_cycles.append([period, amplitude, phase])

        db = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )

        # Store cycles data (always write field-level defaults)
        if normalized_cycles:
            db.hset(cycles_hash_key, member_key, msgpack.packb(normalized_cycles))
        else:
            # Remove any stale cycles data if field now has no cycles
            db.hdel(cycles_hash_key, member_key)

        # Store pressure data — preserve existing last_resolved
        if field.pressure_rate > 0:
            # Read directly from Redis (not the pipeline) because we need
            # the result immediately to decide whether to preserve last_resolved.
            existing_raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
            if existing_raw:
                # Only update rate, preserve last_resolved
                existing = msgpack.unpackb(existing_raw, raw=False)
                existing["rate"] = field.pressure_rate
                db.hset(pressure_hash_key, member_key, msgpack.packb(existing))
            else:
                # First save: set last_resolved to now
                pressure_data = {
                    "rate": field.pressure_rate,
                    "last_resolved": time.time(),
                }
                db.hset(pressure_hash_key, member_key, msgpack.packb(pressure_data))
        else:
            # Remove any stale pressure data
            db.hdel(pressure_hash_key, member_key)

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
                else POPOTO_REDIS_DB
            )
            db.hdel(cycles_hash_key, member_key)
            db.hdel(pressure_hash_key, member_key)

        # Delegate to parent for sorted set cleanup
        return super().on_delete(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )
