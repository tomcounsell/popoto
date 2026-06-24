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

Auto-forget criteria (non-semantic records, ALL must hold):
    importance_score < FORGET_IMPORTANCE_FLOOR
    last_accessed_seconds_ago > FORGET_IDLE_SECONDS

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

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.MemoryLifecycle")


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
    """Return True if the record should be hard-deleted.

    Semantic memories are protected by default.
    Both importance floor AND idle threshold must be exceeded.
    """
    tier = _get_tier(record, lifecycle.tier_field)
    if tier == "semantic":
        return False
    importance = _get_importance_score(record, lifecycle.importance_field)
    idle = _get_idle_seconds(record)
    return (
        importance < lifecycle.FORGET_IMPORTANCE_FLOOR
        and idle > lifecycle.FORGET_IDLE_SECONDS
    )


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

        Safe to run concurrently — promotion and deletion are idempotent at the
        record level. Worst case: two concurrent ticks both promote the same
        record (second write is a no-op) or both delete the same record
        (second delete is a no-op because the record no longer exists).

        Returns:
            dict with keys:
                promoted (int): Number of records promoted this tick.
                forgotten (int): Number of records deleted this tick.
                duration_ms (float): Wall time for this tick in milliseconds.
        """
        start = time.time()

        # Single-pass: load all non-semantic records once, promote then forget
        promoted, forgotten = self._tick_pass()

        duration_ms = (time.time() - start) * 1000
        summary = {
            "promoted": promoted,
            "forgotten": forgotten,
            "duration_ms": round(duration_ms, 2),
        }
        logger.info(
            "tick() complete: promoted=%d forgotten=%d duration_ms=%.1f",
            promoted,
            forgotten,
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

    # -------------------------------------------------------------------
    # Internal passes
    # -------------------------------------------------------------------

    def _tick_pass(self) -> Tuple[int, int]:
        """Single-pass hydration: promote then forget, loading all non-semantic records once.

        Loads all non-semantic records using a single non-tracking query.
        Evaluates promotion eligibility on the episodic subset, tracking which
        records were promoted.  Then evaluates forget eligibility on records
        that were NOT promoted this pass, using a re-check-tier guard
        immediately before deletion to prevent concurrent promotion races.

        Returns:
            Tuple (promoted_count, forgotten_count).
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

                from ..models.encoding import decode_lazy_field

                try:
                    live_tier = decode_lazy_field(raw_tier)
                except Exception:
                    live_tier = None

                if live_tier == "semantic":
                    logger.debug(
                        "forget guard: tier is now semantic, skipping %s", live_key
                    )
                    continue

                try:
                    record.delete()
                    forgotten += 1
                    logger.debug("forgotten (deleted) %s", live_key)
                except Exception as exc:
                    logger.warning(
                        "tick() delete failed for %s: %s — skipping",
                        live_key,
                        exc,
                    )

        return promoted, forgotten

    # -------------------------------------------------------------------
    # Convenience: all() fallback for models without partition filters
    # -------------------------------------------------------------------

    def _get_all_records(self) -> list:
        """Return all records for this model class."""
        return self.model_class.query.all()
