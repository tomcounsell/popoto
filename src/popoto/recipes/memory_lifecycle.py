"""MemoryLifecycle — policy layer orchestrating memory tier transitions and auto-forget.

Composes existing Popoto decay primitives (DecayingSortedField, CyclicDecayField,
ConfidenceField, AccessTrackerMixin) into a working → episodic → semantic lifecycle.
Does not replace any existing primitive — purely a composition layer.

Architecture::

    New memory created
        |
        v
    [lifecycle.tag_new(record)]  -- sets tier="episodic"
        |
        v
    [lifecycle.tick()]  -- periodic pass
        ├── Scan episodic tier (paginated)
        ├── Promote eligible records to "semantic"
        ├── Forget low-importance idle records
        └── Log summary

Two-tier design:
    "episodic"  -- default tier for new memories; specific events with temporal context
    "semantic"  -- consolidated facts; decontextualized; protected from auto-forget

Working memory is approximated by CyclicDecayField rapid decay — no separate tier in v1.

Promotion criteria (episodic → semantic, ALL must hold):
    access_count >= PROMOTION_ACCESS_COUNT
    confidence >= PROMOTION_CONFIDENCE_THRESHOLD
    age_seconds >= PROMOTION_MIN_AGE_SECONDS

Auto-forget criteria (non-semantic records)::

    (importance_score < FORGET_IMPORTANCE_FLOOR
     OR (confidence < FORGET_CONFIDENCE_CEILING
         AND evidence_count >= FORGET_MIN_EVIDENCE))
    AND last_accessed_seconds_ago > FORGET_IDLE_SECONDS

Forgetting **tombstones** rather than deletes (#491). A forgotten record is
removed from the live corpus — so it is excluded from every retrieval mode by
construction, not by a filter each read path must remember — while its
fingerprint, death metadata, and archived payload are retained under
``$TOMB:{Model}:*``. ``restore()`` reverses the decision, retention is capped
at ``LIFECYCLE_TOMBSTONE_RETENTION_LIMIT``, and ``forget_hard()`` remains
available for irreversible deletion.

Example::

    from popoto.recipes.memory_lifecycle import MemoryLifecycle

    lifecycle = MemoryLifecycle(
        model_class=Memory,
        importance_field="relevance",   # DecayingSortedField name
    )

    # Tag a newly created memory
    record = Memory.create(tier="episodic", content="...")
    lifecycle.tag_new(record)

    # Periodic lifecycle pass
    lifecycle.tick()

    # Inspect a record's lifecycle state
    state = lifecycle.assess(record)
    print(state.tier, state.promotion_eligible, state.forget_eligible)
"""

import dataclasses
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union, cast

import msgpack

from ..exceptions import ModelException
from ..models.encoding import decode_lazy_field, decode_popoto_model_hashmap
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.MemoryLifecycle")

# Namespace for tombstone structures, kept out of the model's own keyspace so
# no query, index scan, or key-set walk can ever surface a tombstoned record.
TOMBSTONE_KEY_PREFIX = "$TOMB"


# ---------------------------------------------------------------------------
# LifecycleState — return type for assess()
# ---------------------------------------------------------------------------


@dataclass
class LifecycleState:
    """Snapshot of a record's lifecycle status.

    Attributes:
        tier: Current tier string ("episodic", "semantic", etc.).
        access_count: Total confirmed read accesses (0 if no AccessTrackerMixin).
        last_accessed: Unix timestamp of most recent confirmed access, or None.
        importance_score: Current importance score from the importance_field.
        promotion_eligible: Whether this record meets all promotion criteria.
        forget_eligible: Whether this record meets all auto-forget criteria.
    """

    tier: str
    access_count: int
    last_accessed: Optional[float]
    importance_score: float
    promotion_eligible: bool
    forget_eligible: bool


# ---------------------------------------------------------------------------
# Tombstone — the record of a forgetting (#491)
# ---------------------------------------------------------------------------


@dataclass
class Tombstone:
    """Durable record that a memory was forgotten, and what it looked like.

    Forgetting tombstones rather than deletes so an aggressive low-confidence
    forget policy stays reversible (Risk 6) and so each death becomes negative
    evidence a future write path can consult (#494).

    Attributes:
        redis_key: The forgotten record's Redis key. Restore handle.
        fingerprint: ExistenceFilter fingerprint of the dead record — the
            identity token #494 matches new writes against.
        tier: Tier the record held at death.
        importance_at_death: Importance score at the moment of forgetting.
        confidence_at_death: ConfidenceField value at death, or None if the
            model carries no confidence signal.
        evidence_count: Observations backing that confidence.
        dismissal_count: Contradiction/dismissal count at death.
        tombstoned_at: Unix timestamp of the forgetting.
        reason: Free-form marker for what triggered it.
    """

    redis_key: str
    fingerprint: str
    tier: str
    importance_at_death: float
    confidence_at_death: Optional[float]
    evidence_count: int
    dismissal_count: int
    tombstoned_at: float
    reason: str = "policy"


_TOMBSTONE_FIELDS: Tuple[str, ...] = tuple(Tombstone.__dataclass_fields__)

# Fields with no dataclass default must be present in a stored entry — a
# Tombstone missing one of them is not a Tombstone, it is a None-filled shell.
_TOMBSTONE_REQUIRED_FIELDS: Tuple[str, ...] = tuple(
    name
    for name, f in Tombstone.__dataclass_fields__.items()
    if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
)


def _sync(reply: Any) -> Any:
    """Narrow a redis-py reply out of its ``Awaitable[T] | T`` union.

    redis-py shares one set of command stubs between its sync and asyncio
    clients, so every command is typed as possibly-awaitable. ``POPOTO_REDIS_DB``
    is always the sync client, so these replies are never awaitable. Which
    call sites this affects varies by redis-py version (7.x flags several that
    8.x does not), so the narrowing is centralized here rather than sprinkled
    as per-site ``type: ignore`` comments that would go stale in either
    direction.
    """
    return reply


def _decoded_members(reply: Any) -> List[str]:
    """Decode a Redis ZSET member reply into ``str`` keys.

    redis-py types ``zrange``/``zrevrange`` as a union covering their
    ``withscores`` overloads (member, or ``(member, score)`` pairs). These
    call sites never pass ``withscores``, so the reply is always a flat list
    of members — the cast records that, rather than blanket-ignoring the
    resulting type error at each call site.
    """
    return [
        m.decode() if isinstance(m, bytes) else m for m in cast(Iterable[Any], reply)
    ]


def _unpack_tombstone_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode a stored tombstone entry, or None if absent/corrupt/incomplete.

    A partial or foreign msgpack dict under ``$TOMB:{Model}:data`` is dropped
    on the same log-and-skip path as undecodable bytes, rather than being
    inflated into a Tombstone whose non-Optional fields are all None.
    """
    if raw is None:
        return None
    try:
        entry = msgpack.unpackb(raw, raw=False)
    except Exception as exc:
        logger.warning("tombstone entry is undecodable, skipping: %s", exc)
        return None
    if not isinstance(entry, dict):
        logger.warning(
            "tombstone entry is not a mapping (%s), skipping", type(entry).__name__
        )
        return None
    missing = [k for k in _TOMBSTONE_REQUIRED_FIELDS if k not in entry]
    if missing:
        logger.warning("tombstone entry is missing required keys %s, skipping", missing)
        return None
    return entry


def _tombstone_from_entry(entry: Dict[str, Any]) -> Tombstone:
    """Build a Tombstone from a stored entry, ignoring the archived payload.

    Callers must pass an entry that has already cleared
    ``_unpack_tombstone_entry``, which guarantees every required key is present.
    """
    return Tombstone(**{k: entry[k] for k in _TOMBSTONE_FIELDS if k in entry})


# ---------------------------------------------------------------------------
# Module-level helper functions (also used by default policy callables)
# ---------------------------------------------------------------------------


def _get_tier(record, tier_field: str) -> str:
    """Return the tier value for a record."""
    return getattr(record, tier_field, "episodic") or "episodic"


def _get_access_count(record) -> int:
    """Return access_count from AccessTrackerMixin, or 0 if not present."""
    return getattr(record, "access_count", 0) or 0


def _get_last_accessed(record) -> Optional[float]:
    """Return last_accessed timestamp from AccessTrackerMixin, or None."""
    return getattr(record, "last_accessed", None)


def _get_confidence(record, confidence_field: Optional[str]) -> float:
    """Return confidence score from ConfidenceField, or 0.5 as default."""
    if confidence_field is None:
        return 0.5
    from ..fields.confidence_field import ConfidenceField

    field = record._meta.fields.get(confidence_field)
    if isinstance(field, ConfidenceField):
        try:
            return ConfidenceField.get_confidence(record, confidence_field)
        except Exception:
            return field.initial_confidence
    # Fall back to direct attribute if not a ConfidenceField
    val = getattr(record, confidence_field, 0.5)
    return float(val) if val is not None else 0.5


def _get_confidence_data(
    record: Any, confidence_field: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Return the full ConfidenceField payload for a record, or None.

    One round trip yields both ``confidence`` and ``evidence_count``, which
    the confidence-driven forget rule needs together (issue #491). Returns
    None when the model has no usable ConfidenceField or the read fails —
    callers treat that as "no evidence", never as "forget it".
    """
    if confidence_field is None:
        return None
    from ..fields.confidence_field import ConfidenceField

    field = record._meta.fields.get(confidence_field)
    if not isinstance(field, ConfidenceField):
        return None
    try:
        return ConfidenceField.get_confidence_data(record, confidence_field)
    except Exception:
        return None


def _get_age_seconds(record) -> float:
    """Return age in seconds since the record was created / last decayed.

    Uses the redis_key creation time as a fallback if no created_at field.
    Approximated via sorted set score (timestamp stored by DecayingSortedField)
    or falls back to OBJECT IDLETIME of the record key.
    """
    # Try created_at / created field
    for attr in ("created_at", "created"):
        val = getattr(record, attr, None)
        if val is not None:
            if isinstance(val, datetime):
                return (
                    datetime.now(timezone.utc) - val.replace(tzinfo=timezone.utc)
                ).total_seconds()
            elif isinstance(val, (int, float)):
                return time.time() - float(val)

    # Fall back: object idle time from Redis
    try:
        redis_key = record._redis_key or record.db_key.redis_key
        idle = POPOTO_REDIS_DB.object("idletime", redis_key)
        if idle is not None:
            return float(idle)
    except Exception:
        pass
    return 0.0


def _get_importance_score(record, importance_field: str) -> float:
    """Return current importance score from the importance_field sorted set.

    For DecayingSortedField / SortedFieldMixin: reads the raw timestamp score
    from the sorted set. This is a proxy — the actual decayed score requires
    Lua computation, but for threshold-based forget decisions the raw score
    (which decays through inactivity) is sufficient.

    Falls back to a direct attribute read if the field is not a sorted field.
    """
    from ..fields.sorted_field_mixin import SortedFieldMixin

    field = record._meta.fields.get(importance_field)
    if isinstance(field, SortedFieldMixin):
        try:
            sorted_key = field.get_special_use_field_db_key(
                type(record), importance_field
            )
            redis_key = record._redis_key or record.db_key.redis_key
            raw_score = POPOTO_REDIS_DB.zscore(sorted_key.redis_key, redis_key)
            if raw_score is not None:
                # Normalize: score is a timestamp; use recency as proxy for importance.
                # A score older than FORGET_IDLE_SECONDS has low importance.
                elapsed = time.time() - float(raw_score)
                # Simple linear decay: 1.0 at touch, 0.0 at 2× idle window
                normalized = max(0.0, 1.0 - elapsed / (2 * 86400))
                return normalized
        except Exception:
            pass

    # Fall back to direct attribute
    val = getattr(record, importance_field, None)
    if val is not None:
        return float(val)
    return 0.0


def _get_idle_seconds(record) -> float:
    """Return seconds since last confirmed access, or time since creation."""
    last = _get_last_accessed(record)
    if last is not None:
        return time.time() - last
    # Fall back to age
    return _get_age_seconds(record)


# ---------------------------------------------------------------------------
# Default policy callables
# ---------------------------------------------------------------------------


def _default_should_promote(record, lifecycle: "MemoryLifecycle") -> Optional[str]:
    """Return new tier string or None if not eligible for promotion.

    Checks tier, access_count, confidence, and age simultaneously.
    All three criteria must hold.
    """
    tier = _get_tier(record, lifecycle.tier_field)
    if tier != "episodic":
        return None

    access_count = _get_access_count(record)
    confidence = _get_confidence(record, lifecycle._confidence_field)
    age = _get_age_seconds(record)

    if (
        access_count >= lifecycle.PROMOTION_ACCESS_COUNT
        and confidence >= lifecycle.PROMOTION_CONFIDENCE_THRESHOLD
        and age >= lifecycle.PROMOTION_MIN_AGE_SECONDS
    ):
        return "semantic"
    return None


def _default_should_forget(record, lifecycle: "MemoryLifecycle") -> bool:
    """Return True if the record should be forgotten (tombstoned).

    Criteria (issue #491)::

        (importance < FORGET_IMPORTANCE_FLOOR
         OR (confidence < FORGET_CONFIDENCE_CEILING
             AND evidence_count >= FORGET_MIN_EVIDENCE))
        AND idle > FORGET_IDLE_SECONDS

    Semantic memories are protected by default.

    The confidence disjunct closes the promote/forget asymmetry: confidence
    already gated promotion (PROMOTION_CONFIDENCE_THRESHOLD) but was blind to
    forgetting, so a memory dismissed ten times decayed exactly as fast as one
    acted on ten times. The min-evidence conjunct is load-bearing —
    ConfidenceField starts at ``initial_confidence`` and moves on every signal,
    so without a minimum track record one unlucky dismissal could bury a
    memory (Risk 6).

    Models with no ConfidenceField take the importance-only path, which is
    byte-identical to the pre-#491 behavior.
    """
    tier = _get_tier(record, lifecycle.tier_field)
    if tier == "semantic":
        return False

    idle = _get_idle_seconds(record)
    if idle <= lifecycle.FORGET_IDLE_SECONDS:
        return False

    importance = _get_importance_score(record, lifecycle.importance_field)
    if importance < lifecycle.FORGET_IMPORTANCE_FLOOR:
        return True

    return lifecycle.confidence_forget_eligible(record)


# ---------------------------------------------------------------------------
# MemoryLifecycle
# ---------------------------------------------------------------------------


class MemoryLifecycle:
    """Policy layer orchestrating memory tier transitions and auto-forget.

    Composes existing Popoto decay primitives — does not replace them.

    The two tiers are "episodic" (default for new memories) and "semantic"
    (consolidated, protected from auto-forget). A "working" tier can be added
    in v2 if benchmarks show benefit.

    Class-level constants are tuning parameters for the benchmark sweep grid
    (see feedback_magic_numbers.md). They are NOT user-configurable init params.

    Args:
        model_class: The Popoto Model class whose records to manage.
        importance_field: Name of a SortedFieldMixin field used as importance
            signal. Must be present on the model. Validated at init time.
        tier_field: Name of the field carrying the tier partition value.
            Defaults to "tier". Must be a KeyField to enable filter queries.
        should_promote: Optional callable(record, lifecycle) → Optional[str].
            Returns the new tier string, or None to skip. Defaults to
            _default_should_promote.
        should_forget: Optional callable(record, lifecycle) → bool.
            Returns True to hard-delete the record. Defaults to
            _default_should_forget.
        partition_filters: Optional dict of extra filter kwargs passed to
            all query.filter() calls. Useful for multi-agent setups where
            each lifecycle instance manages a sub-partition (e.g. agent_id).

    Raises:
        ModelException: If importance_field or tier_field is not found on
            model_class, or if importance_field is not a SortedFieldMixin.

    Example::

        lifecycle = MemoryLifecycle(
            model_class=Memory,
            importance_field="relevance",
        )
        lifecycle.tag_new(record)
        lifecycle.tick()
        state = lifecycle.assess(record)
    """

    # --- Magic-number tuning constants (benchmark sweep grid parameters) ---
    # These are resolved via __getattr__ at runtime so that:
    # (a) apply_overrides() in tests/benchmarks/overrides.py can patch
    #     Defaults.LIFECYCLE_* and have those patches observed by instances
    #     during the sweep (the bug fixed here — class-body assignment
    #     froze values at import time).
    # (b) Tests that need custom per-instance values can set instance
    #     attributes directly (e.g. ``lifecycle.PROMOTION_ACCESS_COUNT = 1``);
    #     __getattr__ is only called when the attribute is NOT found in the
    #     instance __dict__, so instance-dict assignments take priority.
    #
    # Attribute names and their Defaults.LIFECYCLE_* counterparts:
    _LIFECYCLE_ATTRS = {
        "PROMOTION_ACCESS_COUNT": "LIFECYCLE_PROMOTION_ACCESS_COUNT",
        "PROMOTION_CONFIDENCE_THRESHOLD": "LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD",
        "PROMOTION_MIN_AGE_SECONDS": "LIFECYCLE_PROMOTION_MIN_AGE_SECONDS",
        "FORGET_IMPORTANCE_FLOOR": "LIFECYCLE_FORGET_IMPORTANCE_FLOOR",
        "FORGET_IDLE_SECONDS": "LIFECYCLE_FORGET_IDLE_SECONDS",
        # Confidence-driven forgetting + tombstone retention (issue #491).
        "FORGET_CONFIDENCE_CEILING": "LIFECYCLE_FORGET_CONFIDENCE_CEILING",
        "FORGET_MIN_EVIDENCE": "LIFECYCLE_FORGET_MIN_EVIDENCE",
        "TOMBSTONE_RETENTION_LIMIT": "LIFECYCLE_TOMBSTONE_RETENTION_LIMIT",
    }

    def __getattr__(self, name: str):
        """Resolve LIFECYCLE_* threshold attributes from Defaults at access time.

        Called only when ``name`` is not found in the instance __dict__ or
        the class __dict__ (i.e. not set as an instance attribute and not a
        regular class attribute). This ensures:

        - apply_overrides(Defaults.LIFECYCLE_*) is observed by all lifecycle
          instances that have not set a local override.
        - Per-instance overrides (``lifecycle.PROMOTION_ACCESS_COUNT = N``)
          still work because instance-dict lookup precedes __getattr__.
        """
        defaults_key = self._LIFECYCLE_ATTRS.get(name)
        if defaults_key is not None:
            from ..fields.constants import Defaults

            return getattr(Defaults, defaults_key)
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    # -------------------------------------------------------------------

    def __init__(
        self,
        model_class,
        importance_field: str,
        tier_field: str = "tier",
        should_promote: Optional[Callable] = None,
        should_forget: Optional[Callable] = None,
        partition_filters: Optional[dict] = None,
    ):
        self.model_class = model_class
        self.importance_field = importance_field
        self.tier_field = tier_field
        self._should_promote = should_promote or _default_should_promote
        self._should_forget = should_forget or _default_should_forget
        self.partition_filters = partition_filters or {}

        # --- Capability detection ---
        self._validate_fields()

        # Detect AccessTrackerMixin (soft dependency — degrades gracefully)
        from ..fields.access_tracker import AccessTrackerMixin

        self._has_access_tracker = issubclass(model_class, AccessTrackerMixin)
        if not self._has_access_tracker:
            logger.warning(
                "MemoryLifecycle: model %s does not use AccessTrackerMixin. "
                "access_count will default to 0 and last_accessed to creation time. "
                "Add AccessTrackerMixin for best lifecycle results.",
                model_class.__name__,
            )

        # Detect ConfidenceField (soft dependency)
        from ..fields.confidence_field import ConfidenceField

        self._confidence_field: Optional[str] = None
        for fname, field in model_class._meta.fields.items():
            if isinstance(field, ConfidenceField):
                self._confidence_field = fname
                break

        # Confidence-driven forgetting (#491) resolves its field through the
        # same helper the decay path uses, so forgetting and ranking always
        # listen to the same evidence signal — including the deploy-level
        # kill switch (DECAY_CONFIDENCE_MODULATION_ENABLED) and the
        # ambiguity rule (2+ ConfidenceFields => off rather than a guess).
        # This eager call exists only to fail fast on a misconfigured
        # confidence_modulation_field spec at construction time; the result is
        # deliberately discarded. Consumers re-resolve per call (see
        # _resolve_forget_confidence_field) so the kill switch is honoured the
        # moment it is flipped, exactly as on the decay path.
        self._resolve_forget_confidence_field()

    def _resolve_forget_confidence_field(self) -> Optional[str]:
        """Return the ConfidenceField name driving confidence-based forgetting.

        Re-resolved on every call rather than frozen at construction: the
        deploy-level kill switch (``Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED``)
        must be able to suppress confidence-driven *tombstoning* — the
        data-mutating half of #491 — the instant it is toggled, matching the
        decay path's behaviour. ``resolve_confidence_modulation_field`` caches
        per model class, so the steady-state cost is a dict lookup.

        Returns:
            The ConfidenceField name, or None when modulation is off.
        """
        from ..fields.decaying_sorted_field import (
            resolve_confidence_modulation_field,
        )

        field_name, _ = resolve_confidence_modulation_field(
            self.model_class,
            self.model_class._meta.fields[self.importance_field],
            self.importance_field,
        )
        return field_name

    def _validate_fields(self) -> None:
        """Validate that required fields exist on the model class.

        Raises:
            ModelException: If importance_field or tier_field is missing or wrong type.
        """
        from ..fields.sorted_field_mixin import SortedFieldMixin

        fields = self.model_class._meta.fields

        if self.importance_field not in fields:
            raise ModelException(
                f"MemoryLifecycle: importance_field '{self.importance_field}' "
                f"not found on {self.model_class.__name__}. "
                f"Available fields: {list(fields.keys())}"
            )

        importance_f = fields[self.importance_field]
        if not isinstance(importance_f, SortedFieldMixin):
            raise ModelException(
                f"MemoryLifecycle: importance_field '{self.importance_field}' "
                f"must be a SortedFieldMixin subclass (e.g. DecayingSortedField). "
                f"Got {type(importance_f).__name__}."
            )

        if self.tier_field not in fields:
            raise ModelException(
                f"MemoryLifecycle: tier_field '{self.tier_field}' "
                f"not found on {self.model_class.__name__}. "
                f"Available fields: {list(fields.keys())}"
            )

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def tag_new(self, record, tier: str = "episodic") -> None:
        """Set the tier field on a newly created memory record.

        Call this after record.save() to assign the starting tier.
        Idempotent — safe to call on already-tiered records (overwrites).

        When the tier_field is a KeyField, the tier value is part of the Redis
        key identity. Changing it on an already-saved record requires
        migrate_key=True (key migration). tag_new() handles this automatically.

        Args:
            record: A saved Popoto model instance.
            tier: Tier string to assign. Defaults to "episodic".
        """
        from ..fields.key_field_mixin import KeyFieldMixin

        setattr(record, self.tier_field, tier)

        # Determine if tier_field is a KeyField (requires migrate_key=True when changing)
        field = type(record)._meta.fields.get(self.tier_field)
        is_key_field = isinstance(field, KeyFieldMixin)

        saved_values = getattr(record, "_saved_field_values", {})
        tier_changed = (
            is_key_field and saved_values and saved_values.get(self.tier_field) != tier
        )

        if tier_changed:
            record.save(migrate_key=True)
        else:
            record.save()

        logger.debug(
            "tag_new: %s.%s = %r",
            type(record).__name__,
            self.tier_field,
            tier,
        )

    def tick(self) -> dict:
        """Run one lifecycle pass: promote eligible records and forget stale ones.

        Loads all non-semantic records in a single non-tracking pass, evaluates
        promotion eligibility on the episodic subset, then evaluates forget
        eligibility on records that were not promoted this pass.  The
        re-check-tier guard re-reads the authoritative tier from Redis
        immediately before deletion to prevent concurrent promotion races.

        Forgetting **tombstones** rather than deletes (#491): the record leaves
        the live corpus (and therefore every retrieval path) but its full
        payload, fingerprint, and death metadata are archived, which is what
        lets ``restore()`` undo the decision. Use ``forget_hard()`` for
        irreversible deletion.

        Safe to run concurrently — promotion and forgetting are idempotent at
        the record level. Worst case: two concurrent ticks both promote the
        same record (second write is a no-op) or both forget the same record
        (the second finds the hash gone and skips).

        Returns:
            dict with keys:
                promoted (int): Number of records promoted this tick.
                forgotten (int): Number of records forgotten this tick.
                tombstoned (int): Number of tombstones written this tick.
                    Equals ``forgotten`` under the default policy; reported
                    separately so a runaway forget policy is visible in
                    telemetry.
                duration_ms (float): Wall time for this tick in milliseconds.
        """
        start = time.time()

        # Single-pass: load all non-semantic records once, promote then forget
        promoted, forgotten, tombstoned = self._tick_pass()

        duration_ms = (time.time() - start) * 1000
        summary = {
            "promoted": promoted,
            "forgotten": forgotten,
            "tombstoned": tombstoned,
            "duration_ms": round(duration_ms, 2),
        }
        logger.info(
            "tick() complete: promoted=%d forgotten=%d tombstoned=%d duration_ms=%.1f",
            promoted,
            forgotten,
            tombstoned,
            duration_ms,
        )
        return summary

    def assess(self, record) -> LifecycleState:
        """Return the current lifecycle state of a record.

        Args:
            record: A saved Popoto model instance.

        Returns:
            LifecycleState with tier, access_count, last_accessed,
            importance_score, promotion_eligible, and forget_eligible.
        """
        tier = _get_tier(record, self.tier_field)
        access_count = _get_access_count(record)
        last_accessed = _get_last_accessed(record)
        importance_score = _get_importance_score(record, self.importance_field)

        try:
            promotion_eligible = self._should_promote(record, self) is not None
        except Exception as exc:
            logger.warning(
                "assess(): should_promote raised %s — defaulting to False", exc
            )
            promotion_eligible = False

        try:
            forget_eligible = self._should_forget(record, self)
        except Exception as exc:
            logger.warning(
                "assess(): should_forget raised %s — defaulting to False", exc
            )
            forget_eligible = False

        return LifecycleState(
            tier=tier,
            access_count=access_count,
            last_accessed=last_accessed,
            importance_score=importance_score,
            promotion_eligible=promotion_eligible,
            forget_eligible=forget_eligible,
        )

    def confidence_forget_eligible(self, record: Any) -> bool:
        """Return True if accumulated outcome evidence alone justifies forgetting.

        Requires BOTH a confidence below ``FORGET_CONFIDENCE_CEILING`` and at
        least ``FORGET_MIN_EVIDENCE`` observations. Returns False whenever the
        evidence cannot be read at all — absence of evidence is never evidence
        for forgetting, and the kill switch (re-read on every call) forces
        this path off entirely.
        """
        data = _get_confidence_data(record, self._resolve_forget_confidence_field())
        if not data:
            return False
        try:
            evidence = int(data.get("evidence_count", 0) or 0)
            confidence = float(data["confidence"])
        except (KeyError, TypeError, ValueError):
            return False
        if evidence < self.FORGET_MIN_EVIDENCE:
            return False
        return confidence < self.FORGET_CONFIDENCE_CEILING

    # -------------------------------------------------------------------
    # Tombstones (#491)
    # -------------------------------------------------------------------

    def _tombstone_keys(self) -> Tuple[str, str]:
        """Return the (data hash, recency index) Redis keys for tombstones."""
        name = self.model_class.__name__
        return (
            f"{TOMBSTONE_KEY_PREFIX}:{name}:data",
            f"{TOMBSTONE_KEY_PREFIX}:{name}:index",
        )

    def _fingerprint(self, record: Any) -> str:
        """Return the record's ExistenceFilter fingerprint.

        Uses the model's ExistenceFilter fingerprint_fn when one exists so the
        tombstone carries the same identity token #494 will match new writes
        against. Falls back to the redis_key, which is exactly what
        ExistenceFilter itself falls back to when no fingerprint_fn is set.
        """
        from ..fields.existence_filter import (
            ExistenceFilter,
            _compute_fingerprint_impl,
        )

        for field in type(record)._meta.fields.values():
            if isinstance(field, ExistenceFilter):
                try:
                    return _compute_fingerprint_impl(field, record)
                except Exception:
                    break
        return record.db_key.redis_key

    def tombstone(self, record: Any, reason: str = "policy") -> Optional[Tombstone]:
        """Forget a record by tombstoning it: remove from retrieval, keep the death.

        The record is archived (its raw Redis hash, so ``restore()`` can bring
        it back byte-for-byte) together with death metadata, then removed from
        the live corpus. Removal — rather than an in-place "hidden" flag — is
        what makes exclusion from *every* retrieval mode structural instead of
        a filter each read path must remember to apply.

        Args:
            record: A saved Popoto model instance.
            reason: Free-form marker for why it died (e.g. "policy").

        Returns:
            The Tombstone, or None if the record could not be archived.
        """
        live_key = getattr(record, "_redis_key", None) or record.db_key.redis_key

        try:
            raw_hash = POPOTO_REDIS_DB.hgetall(live_key)
        except Exception as exc:
            logger.warning("tombstone: HGETALL failed for %s: %s", live_key, exc)
            return None
        if not raw_hash:
            logger.debug("tombstone: key absent, skipping %s", live_key)
            return None

        data = (
            _get_confidence_data(record, self._resolve_forget_confidence_field()) or {}
        )
        tomb = Tombstone(
            redis_key=live_key,
            fingerprint=self._fingerprint(record),
            tier=_get_tier(record, self.tier_field),
            importance_at_death=_get_importance_score(record, self.importance_field),
            confidence_at_death=(
                float(data["confidence"]) if "confidence" in data else None
            ),
            evidence_count=int(data.get("evidence_count", 0) or 0),
            dismissal_count=int(data.get("contradictions", 0) or 0),
            tombstoned_at=time.time(),
            reason=reason,
        )

        payload = {
            (k.decode() if isinstance(k, bytes) else str(k)): v
            for k, v in _sync(raw_hash).items()
        }
        entry = dict(tomb.__dict__)
        entry["payload"] = payload

        data_key, index_key = self._tombstone_keys()
        try:
            pipeline = POPOTO_REDIS_DB.pipeline()
            pipeline.hset(data_key, live_key, msgpack.packb(entry, use_bin_type=True))
            pipeline.zadd(index_key, {live_key: tomb.tombstoned_at})
            pipeline.execute()
        except Exception as exc:
            logger.warning("tombstone: write failed for %s: %s", live_key, exc)
            return None

        # Only now leave the live corpus — archive first so a crash between the
        # two steps loses nothing.
        try:
            record.delete()
        except Exception as exc:
            logger.warning(
                "tombstone: removal failed for %s: %s — rolling back tombstone",
                live_key,
                exc,
            )
            self.purge_tombstone(live_key)
            return None

        self._enforce_tombstone_retention()
        logger.debug("tombstoned %s (reason=%s)", live_key, reason)
        return tomb

    def _enforce_tombstone_retention(self) -> int:
        """Age out the oldest tombstones beyond TOMBSTONE_RETENTION_LIMIT.

        Bounded retention (Risk 7): tombstones must not outgrow the records
        they replaced. Returns the number evicted.
        """
        limit = int(self.TOMBSTONE_RETENTION_LIMIT)
        data_key, index_key = self._tombstone_keys()
        keys: List[str] = []
        try:
            excess = int(_sync(POPOTO_REDIS_DB.zcard(index_key))) - limit
            if excess <= 0:
                return 0
            oldest = POPOTO_REDIS_DB.zrange(index_key, 0, excess - 1)
            if not oldest:
                return 0
            keys = _decoded_members(oldest)
            pipeline = POPOTO_REDIS_DB.pipeline()
            pipeline.hdel(data_key, *keys)
            pipeline.zrem(index_key, *keys)
            pipeline.execute()
        except Exception as exc:
            logger.warning("tombstone retention sweep failed: %s", exc)
            return 0
        logger.debug("aged out %d tombstone(s) past limit %d", len(keys), limit)
        return len(keys)

    def tombstone_count(self) -> int:
        """Return the number of retained tombstones for this model class."""
        _, index_key = self._tombstone_keys()
        try:
            return int(_sync(POPOTO_REDIS_DB.zcard(index_key)))
        except Exception:
            return 0

    def list_tombstones(self, limit: Optional[int] = None) -> List[Tombstone]:
        """Return retained Tombstones, newest death first."""
        data_key, index_key = self._tombstone_keys()
        stop = -1 if limit is None else max(0, limit - 1)
        try:
            raw_keys = POPOTO_REDIS_DB.zrevrange(index_key, 0, stop)
        except Exception as exc:
            logger.warning("list_tombstones: index read failed: %s", exc)
            return []
        if not raw_keys:
            return []
        keys = _decoded_members(raw_keys)
        raws = _sync(POPOTO_REDIS_DB.hmget(data_key, keys))
        tombstones: List[Tombstone] = []
        for raw in raws:
            entry = _unpack_tombstone_entry(raw)
            if entry is not None:
                tombstones.append(_tombstone_from_entry(entry))
        return tombstones

    def get_tombstone(self, redis_key: str) -> Optional[Tombstone]:
        """Return the Tombstone for a redis_key, or None if not retained."""
        data_key, _ = self._tombstone_keys()
        entry = _unpack_tombstone_entry(POPOTO_REDIS_DB.hget(data_key, redis_key))
        return None if entry is None else _tombstone_from_entry(entry)

    def restore(self, redis_key: Union[str, Tombstone]) -> Optional[Any]:
        """Bring a tombstoned record back into the live corpus.

        Args:
            redis_key: The tombstoned record's Redis key (``Tombstone.redis_key``).

        Returns:
            The restored model instance, or None if no tombstone is retained
            for that key (it may have aged out).
        """
        if isinstance(redis_key, Tombstone):
            redis_key = redis_key.redis_key
        data_key, _ = self._tombstone_keys()
        entry = _unpack_tombstone_entry(POPOTO_REDIS_DB.hget(data_key, redis_key))
        if entry is None:
            return None

        redis_hash = {
            (k.encode() if isinstance(k, str) else k): v
            for k, v in (entry.get("payload") or {}).items()
        }
        instance = decode_popoto_model_hashmap(
            self.model_class, redis_hash, source_redis_key=redis_key
        )
        if instance is None:
            logger.warning("restore: empty payload for %s", redis_key)
            return None
        # save() re-runs every on_save hook, so all secondary indexes
        # (sorted sets, key sets, geo, unique) are rebuilt from scratch.
        instance.save()
        self.purge_tombstone(redis_key)
        logger.debug("restored %s", redis_key)
        return instance

    def purge_tombstone(self, redis_key: Union[str, Tombstone]) -> bool:
        """Drop a tombstone permanently. Returns True if one was removed."""
        if isinstance(redis_key, Tombstone):
            redis_key = redis_key.redis_key
        data_key, index_key = self._tombstone_keys()
        pipeline = POPOTO_REDIS_DB.pipeline()
        pipeline.hdel(data_key, redis_key)
        pipeline.zrem(index_key, redis_key)
        removed = pipeline.execute()
        return bool(removed and removed[0])

    def purge_all_tombstones(self) -> int:
        """Drop every retained tombstone. Returns the number removed."""
        count = self.tombstone_count()
        POPOTO_REDIS_DB.delete(*self._tombstone_keys())
        return count

    def forget_hard(self, record: Any) -> bool:
        """Delete a record outright, leaving no tombstone.

        The explicit, irreversible counterpart to ``tombstone()`` — kept
        available so an adopter can still purge a record entirely (e.g. a
        deletion request) rather than merely retiring it from retrieval.
        """
        try:
            record.delete()
        except Exception as exc:
            logger.warning(
                "forget_hard failed for %s: %s",
                getattr(record, "_redis_key", "?"),
                exc,
            )
            return False
        return True

    # -------------------------------------------------------------------
    # Internal passes
    # -------------------------------------------------------------------

    def _tick_pass(self) -> Tuple[int, int, int]:
        """Single-pass hydration: promote then forget, loading all non-semantic records once.

        Loads all non-semantic records using a single non-tracking query.
        Evaluates promotion eligibility on the episodic subset, tracking which
        records were promoted.  Then evaluates forget eligibility on records
        that were NOT promoted this pass, using a re-check-tier guard
        immediately before deletion to prevent concurrent promotion races.

        Returns:
            Tuple (promoted_count, forgotten_count, tombstoned_count).
        """
        # Load all non-semantic records once (non-tracking — no on_read() side-effects)
        filters = {**self.partition_filters}
        all_non_semantic = (
            self.model_class.query.filter(**filters).no_track().all()
            if filters
            else self.model_class.query.all()
        )

        # Filter to non-semantic in-process
        non_semantic_records = [
            r for r in all_non_semantic if _get_tier(r, self.tier_field) != "semantic"
        ]

        promoted = 0
        promoted_this_pass: set = set()

        # --- Phase 1: Promote episodic → semantic ---
        for record in non_semantic_records:
            if _get_tier(record, self.tier_field) != "episodic":
                continue
            try:
                new_tier = self._should_promote(record, self)
            except Exception as exc:
                logger.warning(
                    "tick() should_promote raised for %s: %s — skipping",
                    getattr(record, "_redis_key", "?"),
                    exc,
                )
                continue

            if new_tier is not None:
                try:
                    old_key = getattr(record, "_redis_key", "?")
                    setattr(record, self.tier_field, new_tier)
                    # migrate_key=True is required when tier_field is a KeyField,
                    # because the tier value is part of the Redis key identity.
                    record.save(migrate_key=True)
                    promoted_this_pass.add(id(record))
                    promoted += 1
                    logger.debug(
                        "promoted %s → %s (new key: %s)",
                        old_key,
                        new_tier,
                        getattr(record, "_redis_key", "?"),
                    )
                except Exception as exc:
                    logger.warning(
                        "tick() promotion save failed for %s: %s — skipping",
                        getattr(record, "_redis_key", "?"),
                        exc,
                    )

        # --- Phase 2: Forget low-importance idle records (not promoted this pass) ---
        forgotten = 0
        tombstoned = 0
        for record in non_semantic_records:
            if id(record) in promoted_this_pass:
                continue
            try:
                should = self._should_forget(record, self)
            except Exception as exc:
                logger.warning(
                    "tick() should_forget raised for %s: %s — skipping",
                    getattr(record, "_redis_key", "?"),
                    exc,
                )
                continue

            if should:
                # Re-check-tier guard: re-read the authoritative tier from Redis
                # before deleting to avoid racing with a concurrent promotion.
                # Use the same key identity that was resolved at hydration time.
                live_key = getattr(record, "_redis_key", None)
                if live_key is None:
                    continue
                try:
                    raw_tier = POPOTO_REDIS_DB.hget(live_key, self.tier_field)
                except Exception:
                    raw_tier = None

                if raw_tier is None:
                    # Key no longer exists — skip delete
                    logger.debug("forget guard: key absent, skipping %s", live_key)
                    continue

                try:
                    live_tier = decode_lazy_field(raw_tier)
                except Exception:
                    live_tier = None

                if live_tier == "semantic":
                    logger.debug(
                        "forget guard: tier is now semantic, skipping %s", live_key
                    )
                    continue

                # Forgetting tombstones rather than deletes (#491): the record
                # leaves every retrieval path but its full payload, fingerprint,
                # and death metadata are archived, so restore() can undo the
                # decision.
                if self.tombstone(record, reason="lifecycle") is not None:
                    forgotten += 1
                    tombstoned += 1

        return promoted, forgotten, tombstoned

    # -------------------------------------------------------------------
    # Convenience: all() fallback for models without partition filters
    # -------------------------------------------------------------------

    def _get_all_records(self) -> list:
        """Return all records for this model class."""
        return self.model_class.query.all()
