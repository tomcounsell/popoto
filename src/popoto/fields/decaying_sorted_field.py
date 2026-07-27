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
from typing import Any, Optional

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
        confidence_modulation_field: Controls confidence-modulated decay
            (issue #491). ``None`` (default) auto-detects a single
            ``ConfidenceField`` on the model; a ``str`` names one explicitly;
            ``False`` disables modulation for this field. Modulation is also
            globally disabled by ``Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED
            = False``, which needs no model-code edit (deploy-level kill
            switch). Every disabled path is a byte-identical no-op.
        partition_by: Partition the sorted set by key field values.
            Inherited from SortedFieldMixin.
    """

    def __init__(self, **kwargs):
        decay_rate = kwargs.pop("decay_rate", None)
        self.decay_rate = decay_rate if decay_rate is not None else Defaults.DECAY_RATE
        self.base_score_field = kwargs.pop("base_score_field", None)
        self.confidence_modulation_field = kwargs.pop(
            "confidence_modulation_field", None
        )
        if not (
            self.confidence_modulation_field is None
            or self.confidence_modulation_field is False
            or isinstance(self.confidence_modulation_field, str)
        ):
            raise ModelException(
                "confidence_modulation_field must be None (auto-detect), False "
                "(disabled), or the str name of a ConfidenceField (got "
                f"{self.confidence_modulation_field!r})"
            )
        # Per-model-class resolution cache. Keyed by model class, NOT by the
        # kill switch -- the switch is re-read on every call so toggling it at
        # runtime takes effect immediately.
        self._confidence_modulation_cache: dict[Any, Any] = {}

        if self.decay_rate <= 0:
            raise ModelException(
                "decay_rate must be > 0 (got {})".format(self.decay_rate)
            )

        # Force type=float and auto_now=True behavior
        kwargs["type"] = float
        kwargs["auto_now"] = True
        kwargs["sorted"] = True
        super().__init__(**kwargs)


# --- Confidence-modulated decay wiring (issue #491) -------------------------
#
# The Lua scripts take the confidence ":data" hash as a KEY (index 2 in
# DECAY_SCORE_LUA, index 4 in CYCLIC_DECAY_LUA), plus ARGV[5] = strength s and
# ARGV[6] = c0. Every "off" path resolves to key="" / s="0", which makes the
# Lua guard (`confidence_hash_key ~= '' and s ~= 0`) short-circuit: no extra
# HGET is issued and scores are byte-identical to the pre-#491 formula.

#: (confidence_hash_key, s, c0) for every disabled path. c0 is irrelevant when
#: s == 0 but must still be a parseable number for ``tonumber``.
MODULATION_DISABLED: tuple[str, str, str] = ("", "0", "0.5")


def resolve_confidence_modulation_field(
    model_class: Any, field: Any, field_name: str
) -> tuple[Any, Any]:
    """Resolve which ``ConfidenceField`` modulates ``field``'s decay.

    Resolution order (first match wins):

    1. ``Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED is False`` -> off.
       This is the deploy-level kill switch: it disables modulation without
       any model-code edit, for adopters who cannot edit model definitions.
    2. ``confidence_modulation_field=False`` -> off (per-field opt-out).
    3. ``confidence_modulation_field="name"`` -> that field, or ``ModelException``
       if it is missing or is not a ``ConfidenceField``.
    4. ``confidence_modulation_field=None`` (default) -> auto-detect over
       ``model_class._meta.fields``. Exactly one ``ConfidenceField`` is used;
       zero means off; two or more means off plus a warning naming the
       candidates. Guessing between two confidence signals would silently pick
       a ranking policy the adopter never chose.

    Returns:
        tuple: ``(confidence_field_name, confidence_field)``, or
        ``(None, None)`` when modulation is off.
    """
    from .confidence_field import ConfidenceField

    if not Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED:
        return None, None

    spec = getattr(field, "confidence_modulation_field", None)
    if spec is False:
        return None, None

    cache = getattr(field, "_confidence_modulation_cache", None)
    if cache is not None and model_class in cache:
        return cache[model_class]

    fields = getattr(getattr(model_class, "_meta", None), "fields", {}) or {}

    if isinstance(spec, str):
        target = fields.get(spec)
        if target is None:
            raise ModelException(
                f"confidence_modulation_field='{spec}' on "
                f"{model_class.__name__}.{field_name} names a field that does "
                f"not exist on {model_class.__name__}. Available fields: "
                f"{', '.join(sorted(fields)) or '(none)'}"
            )
        if not isinstance(target, ConfidenceField):
            raise ModelException(
                f"confidence_modulation_field='{spec}' on "
                f"{model_class.__name__}.{field_name} must name a "
                f"ConfidenceField, but '{spec}' is a "
                f"{type(target).__name__}"
            )
        resolved: tuple[Any, Any] = (spec, target)
    else:
        candidates = [
            (name, f) for name, f in fields.items() if isinstance(f, ConfidenceField)
        ]
        if len(candidates) == 1:
            resolved = candidates[0]
        elif not candidates:
            resolved = (None, None)
        else:
            logger.warning(
                "%s.%s: confidence-modulated decay disabled -- %d ConfidenceFields "
                "found (%s). Pass confidence_modulation_field='<name>' to choose "
                "one, or False to silence this.",
                model_class.__name__,
                field_name,
                len(candidates),
                ", ".join(name for name, _ in candidates),
            )
            resolved = (None, None)

    if cache is not None:
        cache[model_class] = resolved
    return resolved


def confidence_modulation_args(
    model_class: Any,
    field: Any,
    field_name: str,
    *,
    filters: Optional[dict[Any, Any]] = None,
    model_instance: Any = None,
) -> tuple[str, str, str]:
    """Build ``(confidence_hash_key, s, c0)`` for a decay ``EVAL``.

    ``c0`` is the resolved field's own ``initial_confidence`` -- never a
    hard-coded ``0.5``. It is both the absent-value default and the centering
    constant, so an adopter running ``initial_confidence=0.3`` still gets a
    bit-exactly neutral score for a record with no evidence.

    Args:
        model_class: The Model class being queried.
        field: The DecayingSortedField / CyclicDecayField instance.
        field_name: Name of that field.
        filters: Query filter mapping, used to satisfy the ConfidenceField's
            ``partition_by``. Missing partition filters raise QueryException.
        model_instance: A saved instance to read partition values off of, for
            callers (the metacognitive proxy) that have records but no filters.

    Returns:
        tuple[str, str, str]: ``(key, s, c0)``; ``MODULATION_DISABLED`` when off.

    Raises:
        QueryException: The ConfidenceField is partitioned by fields the query
            does not filter on, so no single ``:data`` hash covers the result
            set. Silently disabling modulation here would make the ranking
            quietly wrong instead of loudly unsupported.
    """
    # models.query.QueryException, NOT exceptions.QueryException: the two are
    # distinct classes, and this must match what ConfidenceField's own partition
    # guard (confidence_field.py:259) and top_by_decay already raise.
    from ..models.query import QueryException

    conf_name, conf_field = resolve_confidence_modulation_field(
        model_class, field, field_name
    )
    if conf_field is None:
        return MODULATION_DISABLED

    strength = Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH
    if not strength:
        return MODULATION_DISABLED

    if model_instance is not None:
        data_hash_key = conf_field.get_data_hash_key(model_instance, conf_name)
    else:
        filters = filters or {}
        missing = [pf for pf in conf_field.partition_by if pf not in filters]
        if missing:
            raise QueryException(
                f"Confidence-modulated decay on "
                f"{model_class.__name__}.{field_name} reads ConfidenceField "
                f"'{conf_name}', which is partitioned by "
                f"{', '.join(conf_field.partition_by)}. "
                f"Query must include filter(s) for: {', '.join(missing)}. "
                f"Alternatively set confidence_modulation_field=False on "
                f"'{field_name}' to disable modulation."
            )
        partition_values = {pf: filters[pf] for pf in conf_field.partition_by}
        data_hash_key = conf_field.get_data_hash_key_from_values(
            model_class, conf_name, **partition_values
        )

    return (data_hash_key, str(strength), str(conf_field.initial_confidence))
