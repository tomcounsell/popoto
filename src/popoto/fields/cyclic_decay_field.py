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
from ..redis_db import POPOTO_REDIS_DB, get_REDIS_DB, run_lua
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

        cycles_raw = POPOTO_REDIS_DB.hget(
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
                state["cycles"] = [list(cycle) for cycle in cycles]

        pressure_raw = POPOTO_REDIS_DB.hget(
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
            POPOTO_REDIS_DB.hset(
                field.get_cycles_hash_key(model_instance, field_name),
                member_key,
                msgpack.packb(normalized),
            )

        pressure = state.get("pressure")
        if pressure:
            POPOTO_REDIS_DB.hset(
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
            n = int(POPOTO_REDIS_DB.zcard(zset_key))
            if not n:
                return []
        effective_rate = self.decay_rate if decay_rate is None else decay_rate
        if base_score_field is None:
            base_score_field = self.base_score_field or ""

        return run_lua(
            POPOTO_REDIS_DB,
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

        Note that an amplitude adjustment queued on the *same* pipeline as this
        save is still lost, because ``_adjust_cycle_amplitudes`` writes through
        the pipeline while both it and this method read directly. Queue
        ``save()`` first when sharing one pipeline.
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

        # Cycle periods and phases are declarative; amplitudes are LEARNED
        # (mutated by strengthen_cycle / weaken_cycle) and must survive an
        # ordinary save. Mirror the pressure branch below: refresh the declared
        # parameters from the field, preserve the learned one from storage.
        # Read directly from Redis (not the pipeline) because the result is
        # needed immediately to build the value being written.
        #
        # Gated on field.cycles so a pressure-only CyclicDecayField pays no
        # extra round trip — it falls straight through to the hdel branch.
        # Each bucket entry is an (amplitude, baseline) pair, popped together
        # as one decision (never two independent .pop(0) calls that could
        # drift under a malformed payload). baseline is None when slot 3 is
        # absent or non-numeric — "baseline unknown".
        learned: dict[Any, list[tuple[Any, Optional[float]]]] = {}
        if field.cycles:
            stored_raw = get_REDIS_DB().hget(cycles_hash_key, member_key)
            if stored_raw:
                # The guarded region covers the decode AND the shape
                # normalization: a payload can decode cleanly and still be
                # malformed (an entry whose period slot is itself a list is
                # unhashable, and would raise TypeError out of save()). Both
                # failures mean the same thing — the stored state is not
                # usable — so both take the same fallback. This also covers
                # baseline extraction (#698): a half-read payload must not
                # contribute a baseline to some cycles and not others, so the
                # bucket is cleared on the same fallback path as the learned
                # amplitudes.
                try:
                    stored = msgpack.unpackb(stored_raw, raw=False)
                    if isinstance(stored, (list, tuple)):
                        for entry in stored:
                            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                                baseline = None
                                if (
                                    len(entry) >= 4
                                    and isinstance(entry[3], (int, float))
                                    and not isinstance(entry[3], bool)
                                ):
                                    baseline = float(entry[3])
                                learned.setdefault(entry[0], []).append(
                                    (entry[1], baseline)
                                )
                except Exception:
                    # Mirror export_state's handler: #679 exists because this
                    # state was destroyed silently. Do not add a second mute
                    # path — fall back to declared defaults, but say so.
                    logger.warning(
                        f"Could not decode cycles data for {member_key}; "
                        f"falling back to declared amplitudes for {field_name}"
                    )
                    # Discard any partial merge — a half-read payload must not
                    # contribute amplitudes/baselines to some cycles and not
                    # others.
                    learned = {}

        # Normalize cycles to 4-tuples for storage (#698): a stored entry may
        # carry an optional declared-baseline slot. Match stored to declared
        # by period, FIFO within duplicates. The truth test for "has a
        # learned amplitude" is on the bucket list, not the amplitude value,
        # so a learned 0.0 from weaken_cycle is preserved rather than reset to
        # the default.
        normalized_cycles = []
        for cycle in field.cycles:
            period, declared_amplitude = cycle[0], cycle[1]
            phase = cycle[2] if len(cycle) > 2 else 0
            amplitude = declared_amplitude
            new_baseline = declared_amplitude

            bucket = learned.get(period)
            if bucket:
                learned_amplitude, old_baseline = bucket.pop(0)
                if old_baseline is None:
                    # Baseline unknown (legacy entry, or corrupt slot 3):
                    # preserve today's (#679) behavior and acquire a baseline
                    # from this save.
                    amplitude = learned_amplitude
                elif old_baseline == declared_amplitude:
                    # Declaration unchanged — learning wins, as #679 intends.
                    amplitude = learned_amplitude
                else:
                    # The developer edited the declared amplitude — the
                    # declaration wins. The learned amplitude is discarded by
                    # design (#698); this destroys real state, so it is
                    # logged loudly.
                    amplitude = declared_amplitude
                    logger.info(
                        f"CyclicDecayField declared amplitude changed for "
                        f"{model_instance.__class__.__name__}.{field_name} "
                        f"member={member_key} period={period!r}: "
                        f"declared baseline {old_baseline!r} -> "
                        f"{declared_amplitude!r}; discarded learned "
                        f"amplitude {learned_amplitude!r}"
                    )

            normalized_cycles.append([period, amplitude, phase, new_baseline])

        db = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )

        # Store cycles data (declared periods/phases, learned amplitudes)
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
