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

from ..backends import get_backend
from ..exceptions import ModelException
from .constants import Defaults
from .field import Field
from .sorted_field_mixin import SortedFieldMixin

logger = logging.getLogger("POPOTO.DecayingSortedField")

# ``DECAY_SCORE_LUA`` moved to ``popoto.backends.redis`` (#631 WS0) and the
# ``EVAL`` of it moved into ``RedisBackend.decayed_rank`` (#631 WS1d); this
# module no longer evaluates the script itself. The constant is still
# re-exported here under its existing name because the tests that import it
# from here (``test_lua_decay_scoring.py``, ``test_confidence_modulated_decay.py``)
# keep finding it that way. The script text itself is byte-identical.
from ..backends.redis import DECAY_SCORE_LUA  # noqa: E402,F401


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
        """Evaluate this field's decay script over one sorted set (#648).

        **This method owns the KEYS array so that callers do not.** Before it
        existed, every ``EVAL`` of a decay script was assembled by a caller --
        ``models/query.py`` and ``recipes/context_assembler.py`` each held their
        own copy of both layouts, four copies in total of a mapping whose
        misapplication corrupts silently rather than erroring (see the ``do not
        "unify" them`` note inside :data:`DECAY_SCORE_LUA`).

        The two layouts are deliberately **not** unified into a single body with
        a flag. :class:`CyclicDecayField` overrides this method with its own,
        so neither implementation contains an index it must not use: this one
        knows confidence is ``KEYS[2]`` and knows nothing about cycles; the
        override knows cycles/pressure are ``KEYS[2]``/``KEYS[3]`` and knows
        nothing about the validity gate. The rule the script comments ask
        readers to respect is enforced by the class boundary instead.

        Since #631 WS1d the ``EVAL`` itself lives in the storage backend
        (``Backend.decayed_rank``; on Redis ``RedisBackend.decayed_rank`` is
        this method's former body, KEYS array and numkeys included). What this
        method still owns is the *policy*: which companion triples feed the
        script, the effective rate and base-score field, and the call-time
        read of the pre-trim budget.

        Args:
            zset_key: The (already partition-resolved) sorted set to rank.
            now: Current epoch seconds, passed as ``ARGV[1]``.
            n: Max members to return. ``None`` means *every* member, which
                costs one extra ``ZCARD`` -- issued by the backend, before the
                ``EVAL``, so the wire order is ``ZCARD`` then ``EVAL``. An
                empty set short-circuits to ``[]`` with **no** ``EVAL`` issued
                at all.
            confidence: ``(hash_key, s, c0)`` from
                :func:`confidence_modulation_args`. Defaults to
                :data:`MODULATION_DISABLED`.
            validity: ``(invalid_at_key, valid_from_key, as_of)`` from
                :func:`validity_gate_args`. Defaults to
                :data:`VALIDITY_GATE_DISABLED`.
            decay_rate: Override ``self.decay_rate`` (the query path resolves
                an effective rate per call).
            base_score_field: Override ``self.base_score_field``.

        Returns:
            The script's raw flat reply, ``[member, score, member, score, ...]``,
            **undecoded** (architect decision 4, kept by WS1d). Callers already
            differ in how they decode, the ``CyclicDecayField`` override
            returns the same flat shape, and normalizing it here would change
            what their parsing loops receive.
        """
        conf_hash_key, conf_s, conf_c0 = (
            MODULATION_DISABLED if confidence is None else confidence
        )
        gate_invalid_key, gate_valid_key, gate_as_of = (
            VALIDITY_GATE_DISABLED if validity is None else validity
        )
        effective_rate = self.decay_rate if decay_rate is None else decay_rate
        if base_score_field is None:
            base_score_field = self.base_score_field or ""

        # Protocol deviation 8 (#732): the backend renders the confidence
        # triple with ``str()``. The strings this layer already carries --
        # ``("", "0", "0.5")`` on every off path, ``str(strength)`` /
        # ``str(initial_confidence)`` from ``confidence_modulation_args`` --
        # are the identity under ``str()``, so passing them through keeps the
        # EVAL argv byte-identical to the pre-seam call. Converting to float
        # first would render ``0.0`` where today's wire carries ``0``: harmless
        # to the script's ``tonumber(...) or 0``, but a change for nothing.
        # ``Any`` is the honest annotation for that pass-through.
        conf_triple: Any = (conf_hash_key, conf_s, conf_c0)

        # The validity triple travels typed. ``as_of`` arrives as ``repr(t)``
        # from ``validity_gate_args``; ``repr(float(repr(t))) == repr(t)``, so
        # the backend's rendering reproduces the same bytes. The all-empty
        # triple is the gate-off signal and maps to ``None``, which the backend
        # renders as the same three empty strings the script already treats as
        # "gate disabled".
        validity_triple: Optional[tuple[str, str, float]] = (
            None
            if (gate_invalid_key, gate_valid_key, gate_as_of) == VALIDITY_GATE_DISABLED
            else (gate_invalid_key, gate_valid_key, float(gate_as_of))
        )

        return get_backend().decayed_rank(
            zset_key,
            now=now,
            decay_rate=effective_rate,
            limit=n,
            base_score_field=base_score_field,
            confidence=conf_triple,
            validity=validity_triple,
            # ARGV[8] (#585): pre-trim budget. Read from Defaults HERE, at call
            # time, never captured at import -- same rule as every other switch
            # on this path, so an adopter who cannot edit model code can still
            # turn it off at deploy time. <= 0 restores the pre-#585 per-member
            # ZSCORE path byte-for-byte.
            pretrim_max_ratio=Defaults.VALIDITY_GATE_PRETRIM_MAX_RATIO,
        )


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


#: The gate-off triple for :data:`DECAY_SCORE_LUA`'s ``KEYS[3]``/``KEYS[4]``/
#: ``ARGV[7]``. Mirrors :data:`MODULATION_DISABLED`: passing it produces
#: byte-identical scores to the pre-#580 script, so every call site can pass the
#: extra key slots unconditionally instead of branching on numkeys.
VALIDITY_GATE_DISABLED: tuple[str, str, str] = ("", "", "")


def resolve_validity_field_name(model_class: Any) -> Optional[str]:
    """Return the name of the model's ``ValidityField``, or ``None``.

    First declared field wins, matching the auto-detection style used for the
    ConfidenceField / BM25Field / TagField seams elsewhere. Returns ``None``
    for models with no validity axis, which is every model that has not opted
    in — the overwhelmingly common case, and the one that must stay free.
    """
    from .validity_field import ValidityField

    fields = getattr(getattr(model_class, "_meta", None), "fields", {}) or {}
    for name, f in fields.items():
        if isinstance(f, ValidityField):
            return name
    return None


def validity_gate_args(
    model_class: Any, as_of: Optional[float] = None
) -> tuple[str, str, str]:
    """Build ``(invalid_at_key, valid_from_key, as_of)`` for a decay ``EVAL``.

    The single resolver behind all three production ``DECAY_SCORE_LUA`` call
    sites, so there is exactly one place that knows the gate's KEYS/ARGV order
    (``KEYS[3]`` = ``invalid_at``, ``KEYS[4]`` = ``valid_from``, ``ARGV[7]`` =
    as-of).

    ``Defaults.VALIDITY_GATING_ENABLED`` is read **here, at call time**, never
    captured at import time: the kill switch has to take effect at runtime for
    adopters who cannot edit model code (issue #580, plan D6).

    Args:
        model_class: The Model class being queried. ``None`` is tolerated and
            resolves to the disabled triple, for callers whose model context is
            optional.
        as_of: Epoch seconds to evaluate membership at. ``None`` means "now".

    Returns:
        tuple[str, str, str]: ``VALIDITY_GATE_DISABLED`` when gating is off or
        the model declares no ``ValidityField``.
    """
    import time

    if not Defaults.VALIDITY_GATING_ENABLED or model_class is None:
        return VALIDITY_GATE_DISABLED

    field_name = resolve_validity_field_name(model_class)
    if field_name is None:
        return VALIDITY_GATE_DISABLED

    from .validity_field import ValidityField

    valid_from_key, invalid_at_key = ValidityField.get_interval_keys(
        model_class, field_name
    )
    t = time.time() if as_of is None else float(as_of)
    return (invalid_at_key, valid_from_key, repr(t))
