"""ObservationProtocol + RecallProposal — Outcome-driven memory effects.

Provides lifecycle hooks for passive behavioral inference on memory models.
The application layer reports how the agent used retrieved memories; the ORM
applies effects atomically.

Three hooks:
    - ``on_read(instance)``: Fire when query hydrates an instance.
      Delegates to AccessTrackerMixin staging.
    - ``on_surfaced(instances, reason)``: Fire when proactive system pushes
      memories into agent context. Creates RecallProposal entries.
    - ``on_context_used(instances, outcome_map)``: Fire when application
      reports how the agent responded. Applies effects based on outcome.

Five outcomes:
    - ``acted``: Memory content appeared in agent's response. Strengthen.
    - ``dismissed``: Agent explicitly ignored/rejected. Weaken.
    - ``deferred``: Agent didn't address it. No effects, pressure builds.
    - ``contradicted``: Agent explicitly contradicted. Aggressively weaken.
    - ``used``: Agent consumed the memory (read + reasoned) but did not
      act on it in the response. Confirms the staged read and auto-resolves
      predictions with a moderate error, but does NOT touch ConfidenceField,
      CyclicDecayField, or DecayingSortedField. Observable distinction from
      ``deferred``: ``used`` records a confirmed-read trace, ``deferred``
      discards staged reads.

See Also:
    For the effects-per-field matrix (what each outcome does to ConfidenceField,
    CyclicDecayField, DecayingSortedField, AccessTracker, PredictionLedger), see
    the "Effects Matrix" section of docs/features/observation-protocol.md.

RecallProposal:
    Internal ORM infrastructure for tracking proactively surfaced memories.
    Redis ZSET keyed by model class and partition, scored by surfaced_at.
    TTL-based expiration (default 1 hour).

Example:
    from popoto import ObservationProtocol, RecallProposal

    # After agent processes memories:
    outcome_map = {
        memory1.db_key.redis_key: "acted",
        memory2.db_key.redis_key: "dismissed",
    }
    ObservationProtocol.on_context_used(memories, outcome_map)
"""

import logging
import time

from ..redis_db import POPOTO_REDIS_DB
from .constants import Defaults

logger = logging.getLogger("POPOTO.ObservationProtocol")

VALID_OUTCOMES = {"acted", "dismissed", "deferred", "contradicted", "used"}

# ---------------------------------------------------------------------------
# Tuning Constants — initialized from Defaults (validated via sweep #234)
# Functions reference these bare names; the benchmark harness patches both
# Defaults and these module-level aliases.
# ---------------------------------------------------------------------------

ACTED_CONFIDENCE_SIGNAL = Defaults.ACTED_CONFIDENCE_SIGNAL
"""Confidence signal sent to ConfidenceField on 'acted' outcome.
Higher values corroborate the memory more strongly.
Optimal range: [0.5, 1.0]. Insensitive within this range (nDCG stable)."""

CONTRADICTED_CONFIDENCE_SIGNAL = Defaults.CONTRADICTED_CONFIDENCE_SIGNAL
"""Confidence signal sent to ConfidenceField on 'contradicted' outcome.
Lower values contradict the memory more aggressively.
Optimal range: [0.05, 0.3]. Insensitive within this range (nDCG stable)."""

ACTED_CYCLE_STRENGTHEN_FACTOR = Defaults.ACTED_CYCLE_STRENGTHEN_FACTOR
"""CyclicDecayField amplification factor on 'acted' outcome.
CLIFF EFFECT: values < 1.0 cause a 23% nDCG drop in temporal scheduling.
Must be >= 1.0. Optimal range: [1.0, 2.0]. Default 1.2 is safe."""

DISMISSED_CYCLE_WEAKEN_FACTOR = Defaults.DISMISSED_CYCLE_WEAKEN_FACTOR
"""CyclicDecayField damping factor on 'dismissed' outcome.
Values < 1.0 weaken the cycle amplitude.
Optimal range: [0.3, 1.0]. Insensitive within this range."""

CONTRADICTED_CYCLE_WEAKEN_FACTOR = Defaults.CONTRADICTED_CYCLE_WEAKEN_FACTOR
"""CyclicDecayField aggressive damping factor on 'contradicted' outcome.
Values < 1.0 weaken the cycle amplitude; lower = more aggressive.
Optimal range: [0.3, 0.8]. Insensitive within this range."""

AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD
"""Below this confidence level, pressure is auto-resolved on contradicted
outcome. Very low confidence records should not continue building pressure.
Optimal range: [0.05, 0.3]. Insensitive within this range."""

CONFIDENCE_EPSILON = Defaults.CONFIDENCE_EPSILON
"""Internal float-boundary tolerance for the auto-discharge comparison.
Confidence values within this epsilon of the threshold are NOT below it.
Not user config."""


class ObservationProtocol:
    """Lifecycle hooks for passive behavioral inference on memory models.

    All methods are static — the protocol is a stateless coordinator that
    dispatches effects based on outcome type.
    """

    @staticmethod
    def on_read(instance, pipeline=None):
        """Fire when query hydrates an instance. Delegates to AccessTrackerMixin staging.

        If the instance's model uses AccessTrackerMixin, this calls
        ``instance.on_read()``. Otherwise it's a no-op.

        Args:
            instance: A Model instance that was just read from Redis.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if hasattr(instance, "on_read") and callable(instance.on_read):
            instance.on_read(pipeline=pipeline)

    @staticmethod
    def on_surfaced(instances, reason="proactive", partition=None, pipeline=None):
        """Fire when proactive system pushes memories into agent context.

        Creates RecallProposal entries for tracking. Side-effect-free on
        the memories themselves.

        Args:
            instances: List of Model instances being surfaced.
            reason: Why the memories were surfaced. Default "proactive".
            partition: Optional partition key for multi-agent setups.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if not instances:
            return
        RecallProposal.create_batch(
            instances, reason=reason, partition=partition, pipeline=pipeline
        )

    @staticmethod
    def on_context_used(instances, outcome_map, pipeline=None):
        """Fire when application reports how agent responded to surfaced memories.

        For each instance, looks up its outcome in outcome_map and applies
        the corresponding effects atomically.

        Args:
            instances: List of Model instances that were in the agent's context.
            outcome_map: Dict mapping instance Redis keys (str) to outcome
                strings: "acted", "used", "dismissed", "deferred",
                "contradicted". Instances not in the map default to "deferred".
            pipeline: Optional Redis pipeline for batch operations.

        Note:
            This method validates ``outcome_map`` strictly against
            ``VALID_OUTCOMES``. Application-specific outcomes (e.g. a custom
            ``"echoed"`` label) must be coerced to one of the five valid
            values before calling, otherwise a ``ValueError`` is raised.
            See ``docs/features/observation-protocol.md`` (where the
            protocol lives) for guidance on mapping bespoke outcomes into
            the canonical vocabulary.

        Raises:
            ValueError: If any outcome string is not a valid outcome.
        """
        if not instances:
            return

        # Validate all outcomes upfront
        for pk, outcome in outcome_map.items():
            if outcome not in VALID_OUTCOMES:
                raise ValueError(
                    f"Invalid outcome '{outcome}' for key '{pk}'. "
                    f"Valid outcomes: {sorted(VALID_OUTCOMES)}"
                )

        for instance in instances:
            pk = _get_instance_key(instance)
            outcome = outcome_map.get(pk, "deferred")
            _apply_outcome(instance, outcome, pipeline=pipeline)

            # Resolve any pending proposal for this instance
            RecallProposal.resolve(instance, outcome, pipeline=pipeline)


def _get_instance_key(instance):
    """Get the Redis key for a model instance.

    Args:
        instance: A Model instance.

    Returns:
        str: The Redis key string.
    """
    if hasattr(instance, "_redis_key") and instance._redis_key:
        return instance._redis_key
    return instance.db_key.redis_key


def _apply_outcome(instance, outcome, pipeline=None):
    """Apply effects for a single outcome on a single instance.

    Creates an internal pipeline for atomicity when no pipeline is provided.

    Args:
        instance: A Model instance.
        outcome: One of "acted", "used", "dismissed", "deferred",
            "contradicted".
        pipeline: Optional Redis pipeline for batch operations.
    """
    # Use internal pipeline for atomicity if none provided
    use_internal_pipeline = pipeline is None
    if use_internal_pipeline:
        pipeline = POPOTO_REDIS_DB.pipeline()

    if outcome == "acted":
        _apply_acted(instance, pipeline)
    elif outcome == "dismissed":
        _apply_dismissed(instance, pipeline)
    elif outcome == "deferred":
        _apply_deferred(instance, pipeline)
    elif outcome == "contradicted":
        _apply_contradicted(instance, pipeline)
    elif outcome == "used":
        _apply_used(instance, pipeline)

    if use_internal_pipeline:
        pipeline.execute()


def _apply_acted(instance, pipeline):
    """Acted: touch decay clock, confirm reads, strengthen cycles, discharge pressure.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    from .decaying_sorted_field import DecayingSortedField
    from .cyclic_decay_field import CyclicDecayField

    # Touch all DecayingSortedFields (refreshes decay clock)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, DecayingSortedField):
            instance.touch(field_name, pipeline=pipeline)

    # Confirm staged reads (AccessTrackerMixin)
    if hasattr(instance, "confirm_access") and callable(instance.confirm_access):
        instance.confirm_access(pipeline=pipeline)

    # Strengthen cycles and resolve pressure (CyclicDecayField)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.strengthen_cycle(
                field_name, factor=ACTED_CYCLE_STRENGTHEN_FACTOR, pipeline=pipeline
            )
            if field.pressure_rate > 0:
                instance.resolve_pressure(field_name, pipeline=pipeline)

    # Corroborate confidence (ConfidenceField)
    from .confidence_field import ConfidenceField

    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                ConfidenceField.update_confidence(
                    instance, field_name, signal=ACTED_CONFIDENCE_SIGNAL
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "acted", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


def _apply_dismissed(instance, pipeline):
    """Dismissed: discard staged reads, weaken cycles.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    from .cyclic_decay_field import CyclicDecayField

    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)

    # Weaken cycles
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.weaken_cycle(
                field_name, factor=DISMISSED_CYCLE_WEAKEN_FACTOR, pipeline=pipeline
            )

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "dismissed", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


def _apply_deferred(instance, pipeline):
    """Deferred: discard staged reads, no other effects. Pressure keeps building.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)


def _apply_contradicted(instance, pipeline):
    """Contradicted: discard staged reads, aggressively weaken cycles.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    from .cyclic_decay_field import CyclicDecayField

    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)

    # Aggressively weaken cycles (factor=0.5 vs 0.8 for dismissed)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            instance.weaken_cycle(
                field_name, factor=CONTRADICTED_CYCLE_WEAKEN_FACTOR, pipeline=pipeline
            )

    # Contradict confidence (ConfidenceField)
    from .confidence_field import ConfidenceField

    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                ConfidenceField.update_confidence(
                    instance, field_name, signal=CONTRADICTED_CONFIDENCE_SIGNAL
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(
                instance, "contradicted", pipeline=pipeline
            )
        except (TypeError, ValueError):
            pass  # Graceful degradation

    # Auto-discharge: when confidence is clearly below the threshold, resolve
    # pressure on CyclicDecayFields. Values within float epsilon of the
    # threshold are NOT below it (fixes the 0.09999999999999998 artifact
    # that auto-discharged on the first contradiction).
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                conf = ConfidenceField.get_confidence(instance, field_name)
                if conf < AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - CONFIDENCE_EPSILON:
                    for cdf_name, cdf_field in instance._meta.fields.items():
                        if (
                            isinstance(cdf_field, CyclicDecayField)
                            and cdf_field.pressure_rate > 0
                        ):
                            instance.resolve_pressure(cdf_name, pipeline=pipeline)
            except (TypeError, ValueError, AttributeError):
                pass


def _apply_used(instance, pipeline):
    """Used: confirm staged reads, auto-resolve predictions. No confidence,
    no cycle, no decay effects.

    Semantic: the agent consumed the memory (read + reasoned) but did not
    act on it in the response. Observably stronger than ``deferred``
    (which discards staged reads) because it commits the AccessTracker
    staged-read to a confirmed trace. Observably weaker than ``acted``
    (which corroborates confidence and strengthens cycles).

    Effects:
        * ``confirm_access(pipeline=pipeline)`` when AccessTrackerMixin is
          present — commits the staged read as a confirmed read.
        * ``PredictionLedgerMixin.auto_resolve(instance, "used")`` — maps
          to ``Defaults.PL_AUTO_RESOLVE_USED`` (0.3, moderate error).
        * Does NOT touch ConfidenceField, CyclicDecayField, or
          DecayingSortedField.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    # Confirm staged reads (AccessTrackerMixin) — this is the key behavioral
    # distinction from `_apply_deferred`, which discards staged reads.
    if hasattr(instance, "confirm_access") and callable(instance.confirm_access):
        instance.confirm_access(pipeline=pipeline)

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "used", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


class RecallProposal:
    """Internal tracking for proactively surfaced memories.

    Key pattern: $RP:{ClassName}:pending:{partition} -> ZSET scored by surfaced_at
    Statuses: pending -> acted | used | dismissed | deferred | contradicted | expired
    TTL: default 3600s (1 hour). Unresolved proposals treated as deferred.

    This is internal ORM infrastructure, not a user-facing Model.
    """

    DEFAULT_TTL = 3600

    @classmethod
    def _pending_key(cls, model_class, partition=None):
        """Build the Redis key for pending proposals.

        Args:
            model_class: The Model class (or instance's class).
            partition: Optional partition key.

        Returns:
            str: Redis key like '$RP:ClassName:pending:partition'
        """
        class_name = (
            model_class.__name__
            if isinstance(model_class, type)
            else type(model_class).__name__
        )
        part = partition or "default"
        return f"$RP:{class_name}:pending:{part}"

    @classmethod
    def create_batch(cls, instances, reason="proactive", partition=None, pipeline=None):
        """Create pending proposals for a batch of instances.

        Args:
            instances: List of Model instances being surfaced.
            reason: Why the memories were surfaced.
            partition: Optional partition key.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if not instances:
            return

        now = time.time()
        model_class = type(instances[0])
        key = cls._pending_key(model_class, partition)

        db = pipeline if pipeline is not None else POPOTO_REDIS_DB

        # ZADD each instance with score=now
        members = {}
        for instance in instances:
            member_key = _get_instance_key(instance)
            # Store member key with reason as the value scored by time
            members[member_key] = now

        if members:
            db.zadd(key, members)

    @classmethod
    def resolve(cls, instance, outcome, partition=None, pipeline=None):
        """Remove a resolved proposal from the pending set.

        Idempotent — returns 0 if already removed (e.g., by expiration).

        Args:
            instance: The Model instance whose proposal to resolve.
            outcome: The outcome string (for logging).
            partition: Optional partition key.
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            int: Number of members removed (0 or 1).
        """
        model_class = type(instance)
        key = cls._pending_key(model_class, partition)
        member_key = _get_instance_key(instance)

        if pipeline is not None:
            pipeline.zrem(key, member_key)
            return pipeline
        else:
            return POPOTO_REDIS_DB.zrem(key, member_key)

    @classmethod
    def expire_stale(cls, model_class, partition=None, ttl=None, pipeline=None):
        """Remove proposals older than TTL. Returns expired member keys.

        Args:
            model_class: The Model class to check.
            partition: Optional partition key.
            ttl: TTL in seconds. Default DEFAULT_TTL (3600).
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            list: List of expired member key strings.
        """
        if ttl is None:
            ttl = cls.DEFAULT_TTL

        key = cls._pending_key(model_class, partition)
        cutoff = time.time() - ttl

        # Get members with scores below cutoff (older than TTL)
        expired = POPOTO_REDIS_DB.zrangebyscore(key, "-inf", cutoff)

        if expired:
            if pipeline is not None:
                pipeline.zremrangebyscore(key, "-inf", cutoff)
            else:
                POPOTO_REDIS_DB.zremrangebyscore(key, "-inf", cutoff)

        # Decode bytes to strings
        return [m.decode() if isinstance(m, bytes) else m for m in expired]

    @classmethod
    def get_pending(cls, model_class, partition=None):
        """Return all pending proposals as (member_key, surfaced_at) pairs.

        Args:
            model_class: The Model class to check.
            partition: Optional partition key.

        Returns:
            list: List of (member_key_str, surfaced_at_float) tuples.
        """
        key = cls._pending_key(model_class, partition)
        results = POPOTO_REDIS_DB.zrange(key, 0, -1, withscores=True)
        return [
            (m.decode() if isinstance(m, bytes) else m, score) for m, score in results
        ]
