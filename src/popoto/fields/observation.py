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

Four outcomes:
    - ``acted``: Memory content appeared in agent's response. Strengthen.
    - ``dismissed``: Agent explicitly ignored/rejected. Weaken.
    - ``deferred``: Agent didn't address it. No effects, pressure builds.
    - ``contradicted``: Agent explicitly contradicted. Aggressively weaken.

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

logger = logging.getLogger("POPOTO.ObservationProtocol")

VALID_OUTCOMES = {"acted", "dismissed", "deferred", "contradicted"}


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
                strings: "acted", "dismissed", "deferred", "contradicted".
                Instances not in the map default to "deferred".
            pipeline: Optional Redis pipeline for batch operations.

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
        outcome: One of "acted", "dismissed", "deferred", "contradicted".
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
            instance.strengthen_cycle(field_name, factor=1.2, pipeline=pipeline)
            if field.pressure_rate > 0:
                instance.resolve_pressure(field_name, pipeline=pipeline)


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
            instance.weaken_cycle(field_name, factor=0.8, pipeline=pipeline)


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
            instance.weaken_cycle(field_name, factor=0.5, pipeline=pipeline)


class RecallProposal:
    """Internal tracking for proactively surfaced memories.

    Key pattern: $RP:{ClassName}:pending:{partition} -> ZSET scored by surfaced_at
    Statuses: pending -> acted | dismissed | deferred | contradicted | expired
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
