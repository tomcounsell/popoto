"""CoOccurrenceField — weighted association edges with graph propagation.

Maintains weighted bidirectional (or unidirectional) edges between model
instances using Redis sorted sets. Weights strengthen via co-retrieval
and decay when not reinforced. BFS graph propagation with exponential
weight decay per hop enables multi-hop associative retrieval.

Each CoOccurrenceField instance owns per-PK sorted sets:
    $CoOcF:{ClassName}:{field_name}:{pk} -> ZSET { target_pk: weight, ... }

When symmetric=True (default), link/strengthen/unlink operations mirror
on both source and target sorted sets.

Example:
    class Memory(Model):
        key = UniqueKeyField()
        content = StringField()
        associations = CoOccurrenceField(symmetric=True, max_edges=100)

    # Create edges
    CoOccurrenceField.link(Memory, "pk_a", "pk_b", initial_weight=0.1)
    CoOccurrenceField.strengthen(Memory, "pk_a", "pk_b", delta=0.05)

    # Query associations
    linked = CoOccurrenceField.get_linked(Memory, "pk_a")
    # => [("pk_b", 0.15)]

    # Multi-hop propagation
    scores = CoOccurrenceField.propagate(Memory, ["pk_a"], depth=2)
    # => {"pk_b": 0.5, "pk_c": 0.25}
"""

import json
import logging

from ..redis_db import POPOTO_REDIS_DB
from .constants import Defaults
from .field import Field

logger = logging.getLogger("POPOTO.CoOccurrenceField")

_UNSET = object()  # Sentinel for method params where None has meaning

# Lua script: atomic link with pruning.
# ZADD + ZCARD + conditional ZREMRANGEBYRANK in one atomic operation.
# KEYS[1] = sorted set key for source
# ARGV[1] = target_pk member
# ARGV[2] = initial_weight
# ARGV[3] = max_edges
LINK_WITH_PRUNE_LUA = """
local zset_key = KEYS[1]
local target = ARGV[1]
local weight = tonumber(ARGV[2])
local max_edges = tonumber(ARGV[3])

-- Only set if not already exists (NX behavior)
local existing = redis.call('ZSCORE', zset_key, target)
if existing == false then
    redis.call('ZADD', zset_key, weight, target)
else
    -- Already linked, keep existing weight
    return tonumber(existing)
end

-- Prune if over max_edges
local count = redis.call('ZCARD', zset_key)
if count > max_edges then
    -- Remove lowest-weight edges (rank 0 is lowest score)
    redis.call('ZREMRANGEBYRANK', zset_key, 0, count - max_edges - 1)
end

return weight
"""

# Lua script: weaken all edges by multiplying scores by a factor.
# Edges below threshold after weakening are removed.
# KEYS[1] = sorted set key
# ARGV[1] = decay factor (0 < factor < 1)
# ARGV[2] = threshold for pruning
WEAKEN_ALL_LUA = """
local zset_key = KEYS[1]
local factor = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])

local members = redis.call('ZRANGE', zset_key, 0, -1, 'WITHSCORES')
local removed = 0

for i = 1, #members, 2 do
    local member = members[i]
    local score = tonumber(members[i + 1])
    local new_score = score * factor

    if new_score < threshold then
        redis.call('ZREM', zset_key, member)
        removed = removed + 1
    else
        redis.call('ZADD', zset_key, new_score, member)
    end
end

return removed
"""

# Lua script: BFS graph propagation.
# Traverses edges across hops with exponential weight decay per hop.
# When same PK reached via multiple paths: use max(weight).
# KEYS[1] = key pattern prefix (e.g. "$CoOcF:Memory:associations:")
# ARGV[1] = JSON array of seed PKs
# ARGV[2] = max depth
# ARGV[3] = decay per hop
# ARGV[4] = threshold (minimum weight to continue propagation)
# ARGV[5] = max edges per node to consider
PROPAGATE_BFS_LUA = """
local key_prefix = KEYS[1]
local seeds = cjson.decode(ARGV[1])
local max_depth = tonumber(ARGV[2])
local decay_per_hop = tonumber(ARGV[3])
local threshold = tonumber(ARGV[4])
local max_per_node = tonumber(ARGV[5])

-- Result table: pk -> max_weight
local results = {}
-- BFS queue: {pk, weight, depth}
local queue = {}
-- Visited set to avoid re-expanding at same or lower weight
local visited = {}

-- Initialize with seeds
for _, seed in ipairs(seeds) do
    table.insert(queue, {seed, 1.0, 0})
    -- Seeds get weight 1.0 in results if depth=0
    if max_depth == 0 then
        results[seed] = 1.0
    end
end

if max_depth == 0 then
    local result = {}
    for pk, w in pairs(results) do
        table.insert(result, pk)
        table.insert(result, tostring(w))
    end
    return result
end

local head = 1
while head <= #queue do
    local current = queue[head]
    head = head + 1

    local pk = current[1]
    local weight = current[2]
    local depth = current[3]

    -- Mark visited
    if visited[pk] and visited[pk] >= weight then
        -- Already visited with equal or higher weight, skip
    else
        visited[pk] = weight

        if depth < max_depth then
            -- Get neighbors
            local zset_key = key_prefix .. pk
            local neighbors = redis.call('ZREVRANGE', zset_key, 0, max_per_node - 1, 'WITHSCORES')

            for i = 1, #neighbors, 2 do
                local neighbor_pk = neighbors[i]
                local edge_weight = tonumber(neighbors[i + 1])
                local propagated_weight = weight * decay_per_hop * edge_weight

                if propagated_weight >= threshold then
                    -- Update result with max weight
                    if not results[neighbor_pk] or propagated_weight > results[neighbor_pk] then
                        results[neighbor_pk] = propagated_weight
                    end
                    -- Add to queue for further exploration
                    table.insert(queue, {neighbor_pk, propagated_weight, depth + 1})
                end
            end
        end
    end
end

-- Remove seeds from results (they are starting points, not discoveries)
for _, seed in ipairs(seeds) do
    results[seed] = nil
end

-- Return flat array [pk1, weight1, pk2, weight2, ...]
local result = {}
for pk, w in pairs(results) do
    table.insert(result, pk)
    table.insert(result, tostring(w))
end
return result
"""

# Lua script: atomic strengthen with upper-bound clamp.
# Reads the current edge weight, adds delta, clamps at cap, writes back.
# Atomic read-then-write prevents concurrent strengthen() races on the
# same edge (Redis/Valkey single-threaded EVAL).
# KEYS[1] = source ZSET key
# ARGV[1] = target_pk
# ARGV[2] = delta
# ARGV[3] = cap
STRENGTHEN_CLAMP_LUA = """
local zset_key = KEYS[1]
local target = ARGV[1]
local existing = redis.call('ZSCORE', zset_key, target)
local old = tonumber(existing) or 0
local delta = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local new_weight = math.min(old + delta, cap)
redis.call('ZADD', zset_key, new_weight, target)
return tostring(new_weight)
"""


class CoOccurrenceField(Field):
    """A field that maintains weighted association edges between model instances.

    Uses per-PK Redis sorted sets to store weighted edges to other PKs.
    Supports symmetric (bidirectional) and asymmetric (unidirectional) modes.

    Args:
        symmetric: If True, edges are bidirectional. Default True.
        max_edges: Maximum edges per PK. Lowest-weight edges pruned when exceeded.
            Default 500.
        decay_factor: Multiplicative decay factor for weaken_all(). Default 0.95.

    Example:
        class Memory(Model):
            key = UniqueKeyField()
            content = StringField()
            associations = CoOccurrenceField(symmetric=True, max_edges=100)
    """

    def __init__(self, **kwargs):
        self.symmetric = kwargs.pop("symmetric", True)
        self.max_edges = kwargs.pop("max_edges", 500)
        decay_factor = kwargs.pop("decay_factor", None)
        self.decay_factor = (
            decay_factor
            if decay_factor is not None
            else Defaults.CO_OCCURRENCE_DECAY_FACTOR
        )

        if self.max_edges < 1:
            from ..exceptions import ModelException

            raise ModelException(f"max_edges must be >= 1 (got {self.max_edges})")
        if not (0 <= self.decay_factor < 1):
            from ..exceptions import ModelException

            raise ModelException(
                f"decay_factor must be >= 0 and < 1 (got {self.decay_factor})"
            )

        # CoOccurrenceField stores no value on the model instance itself
        kwargs.setdefault("type", str)
        kwargs.setdefault("null", True)
        kwargs.setdefault("default", None)
        super().__init__(**kwargs)

    def get_edge_key(self, model_class, pk):
        """Build the Redis key for a PK's edge sorted set.

        Public API for external callers that need direct Redis access to
        a PK's edge set (e.g., bulk edge inspection, custom graph queries,
        monitoring). Prefer the higher-level methods (link, get_linked,
        propagate) for normal operations.

        Pattern: $CoOcF:{ClassName}:{field_name}:{pk}

        Args:
            model_class: The Model class (or instance).
            pk: The primary key string.

        Returns:
            str: The Redis key for this PK's edge sorted set.
        """
        base_key = self.get_special_use_field_db_key(model_class, self.name)
        return base_key.redis_key + ":" + str(pk)

    def get_edge_key_prefix(self, model_class):
        """Build the Redis key prefix for BFS propagation.

        Public API for external callers that need to scan or iterate over
        all edge sorted sets for a field (e.g., graph analytics, bulk cleanup).

        Pattern: $CoOcF:{ClassName}:{field_name}:

        Args:
            model_class: The Model class.

        Returns:
            str: The key prefix (ending with colon).
        """
        base_key = self.get_special_use_field_db_key(model_class, self.name)
        return base_key.redis_key + ":"

    def link(
        self,
        model_class,
        source_pk,
        target_pk,
        initial_weight=_UNSET,
        pipeline=None,
    ):
        """Create a weighted edge between two PKs.

        If symmetric=True, creates edges in both directions. Uses an atomic
        Lua script to handle ZADD + ZCARD + conditional pruning.

        Args:
            model_class: The Model class.
            source_pk: Source primary key string.
            target_pk: Target primary key string.
            initial_weight: Weight for the new edge. Default from
                ``Defaults.CO_OCCURRENCE_INITIAL_WEIGHT``.
            pipeline: Optional Redis pipeline (unused for Lua eval).

        Returns:
            float: The weight of the edge (existing weight if already linked).

        Raises:
            ValueError: If source_pk == target_pk (no self-loops).
        """
        if initial_weight is _UNSET:
            initial_weight = Defaults.CO_OCCURRENCE_INITIAL_WEIGHT
        source_pk = str(source_pk)
        target_pk = str(target_pk)

        if source_pk == target_pk:
            raise ValueError("Cannot link a PK to itself (no self-loops)")

        source_key = self.get_edge_key(model_class, source_pk)
        result = POPOTO_REDIS_DB.eval(
            LINK_WITH_PRUNE_LUA,
            1,
            source_key,
            target_pk,
            str(initial_weight),
            str(self.max_edges),
        )

        if self.symmetric:
            target_key = self.get_edge_key(model_class, target_pk)
            POPOTO_REDIS_DB.eval(
                LINK_WITH_PRUNE_LUA,
                1,
                target_key,
                source_pk,
                str(initial_weight),
                str(self.max_edges),
            )

        return float(result)

    def strengthen(
        self,
        model_class,
        source_pk,
        target_pk,
        delta=0.05,
        pipeline=None,
    ):
        """Increase the weight of an existing edge.

        Uses an atomic Lua script (STRENGTHEN_CLAMP_LUA) to read the
        current weight, add delta, and clamp at
        ``Defaults.CO_OCCURRENCE_WEIGHT_CAP`` so stored weights can never
        exceed the cap. This guarantees the per-hop contraction invariant
        used by ``propagate()`` (``decay_per_hop * effective_edge_weight``
        stays <= ``decay_per_hop * cap`` < 1 at default config).

        Args:
            model_class: The Model class.
            source_pk: Source primary key string.
            target_pk: Target primary key string.
            delta: Amount to increase weight by. Must be > 0. Default 0.05.
            pipeline: Optional Redis pipeline.

        Returns:
            float: The new (clamped) weight after increment, or None when
                called with a pipeline (execution deferred).

        Raises:
            ValueError: If delta <= 0.
        """
        if delta <= 0:
            raise ValueError(f"delta must be > 0 (got {delta})")

        source_pk = str(source_pk)
        target_pk = str(target_pk)

        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
        source_key = self.get_edge_key(model_class, source_pk)
        db = pipeline if pipeline else POPOTO_REDIS_DB
        new_weight = db.eval(
            STRENGTHEN_CLAMP_LUA,
            1,
            source_key,
            target_pk,
            str(delta),
            str(cap),
        )

        if self.symmetric:
            target_key = self.get_edge_key(model_class, target_pk)
            db.eval(
                STRENGTHEN_CLAMP_LUA,
                1,
                target_key,
                source_pk,
                str(delta),
                str(cap),
            )

        # EventStreamMixin: log strengthen event
        from .event_stream import EventStreamMixin

        if not pipeline and issubclass(model_class, EventStreamMixin):
            try:
                import time

                stream_name = getattr(
                    model_class, "_stream_name", EventStreamMixin._stream_name
                )
                max_length = getattr(
                    model_class,
                    "_stream_max_length",
                    EventStreamMixin._stream_max_length,
                )
                stream_key = f"stream:{stream_name}"
                partition_field = getattr(model_class, "_stream_partition_field", None)
                if partition_field:
                    stream_key = f"{stream_key}:{source_pk}"
                entry = {
                    "model": model_class.__name__,
                    "pk": str(source_pk),
                    "op": "strengthen",
                    "ts": str(time.time()),
                    "changed_fields": "",
                    "source_pk": str(source_pk),
                    "target_pk": str(target_pk),
                    "delta": str(delta),
                }
                POPOTO_REDIS_DB.xadd(
                    stream_key, entry, maxlen=max_length, approximate=True
                )
            except Exception:
                pass  # Best-effort, don't block strengthen

        if pipeline:
            return None  # Pipeline defers execution
        return float(new_weight)

    def unlink(self, model_class, source_pk, target_pk, pipeline=None):
        """Remove an edge between two PKs.

        If symmetric=True, removes edges in both directions.

        Args:
            model_class: The Model class.
            source_pk: Source primary key string.
            target_pk: Target primary key string.
            pipeline: Optional Redis pipeline.
        """
        source_pk = str(source_pk)
        target_pk = str(target_pk)

        source_key = self.get_edge_key(model_class, source_pk)
        db = pipeline if pipeline else POPOTO_REDIS_DB
        db.zrem(source_key, target_pk)

        if self.symmetric:
            target_key = self.get_edge_key(model_class, target_pk)
            db.zrem(target_key, source_pk)

    def weaken_all(self, model_class, pk, factor=None, pipeline=None):
        """Multiplicatively decay all edge weights for a PK.

        Edges that fall below a threshold (factor * 0.01) after weakening
        are automatically pruned.

        Args:
            model_class: The Model class.
            pk: Primary key whose edges to weaken.
            factor: Decay factor (0 < factor < 1). Defaults to self.decay_factor.
                factor=0 removes all edges.
            pipeline: Optional Redis pipeline (unused for Lua eval).

        Returns:
            int: Number of edges removed by pruning.

        Raises:
            ValueError: If factor > 1 or factor < 0.
        """
        if factor is None:
            factor = self.decay_factor

        if factor < 0 or factor > 1:
            raise ValueError(f"factor must be between 0 and 1 inclusive (got {factor})")

        pk = str(pk)
        edge_key = self.get_edge_key(model_class, pk)

        if factor == 0:
            # Special case: remove all edges
            count = POPOTO_REDIS_DB.zcard(edge_key)
            POPOTO_REDIS_DB.delete(edge_key)
            return int(count)

        # Use threshold of 0.001 for pruning
        threshold = 0.001
        result = POPOTO_REDIS_DB.eval(
            WEAKEN_ALL_LUA,
            1,
            edge_key,
            str(factor),
            str(threshold),
        )
        return int(result)

    def get_linked(self, model_class, pk, min_weight=0.01, limit=20):
        """Get linked PKs sorted by weight descending.

        Args:
            model_class: The Model class.
            pk: Primary key to get links for.
            min_weight: Minimum weight threshold. Default 0.01.
            limit: Maximum number of results. Default 20.

        Returns:
            list[tuple[str, float]]: List of (pk, weight) tuples,
                sorted by weight descending.
        """
        pk = str(pk)
        edge_key = self.get_edge_key(model_class, pk)

        # ZREVRANGEBYSCORE: highest to lowest, with score filter
        results = POPOTO_REDIS_DB.zrevrangebyscore(
            edge_key,
            "+inf",
            str(min_weight),
            start=0,
            num=limit,
            withscores=True,
        )

        return [
            (
                member.decode("utf-8") if isinstance(member, bytes) else member,
                float(score),
            )
            for member, score in results
        ]

    def propagate(
        self,
        model_class,
        seed_pks,
        depth=2,
        decay_per_hop=_UNSET,
        threshold=0.01,
    ):
        """BFS graph propagation with exponential weight decay per hop.

        Traverses edges starting from seed PKs, applying multiplicative
        decay at each hop. When the same PK is reached via multiple paths,
        the maximum weight is kept.

        Args:
            model_class: The Model class.
            seed_pks: List of starting primary keys.
            depth: Maximum BFS depth. Default 2. depth=0 returns seeds only.
            decay_per_hop: Weight multiplier per hop. Default from
                ``Defaults.CO_OCCURRENCE_DECAY_PER_HOP``.
            threshold: Minimum propagated weight to continue. Default 0.01.

        Returns:
            dict[str, float]: Mapping of discovered PKs to their propagated
                weights. Seeds are not included in results (except for depth=0).
        """
        if decay_per_hop is _UNSET:
            decay_per_hop = Defaults.CO_OCCURRENCE_DECAY_PER_HOP
        if not seed_pks:
            return {}

        seed_pks = [str(pk) for pk in seed_pks]

        if depth == 0:
            return {pk: 1.0 for pk in seed_pks}

        key_prefix = self.get_edge_key_prefix(model_class)

        result = POPOTO_REDIS_DB.eval(
            PROPAGATE_BFS_LUA,
            1,
            key_prefix,
            json.dumps(seed_pks),
            str(depth),
            str(decay_per_hop),
            str(threshold),
            str(self.max_edges),
        )

        # Parse flat array [pk1, weight1, pk2, weight2, ...]
        scores = {}
        if result:
            for i in range(0, len(result), 2):
                pk = result[i]
                if isinstance(pk, bytes):
                    pk = pk.decode("utf-8")
                weight = float(result[i + 1])
                scores[pk] = weight

        return scores

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Clean up edges when a model instance is deleted.

        Removes the instance's own edge sorted set AND removes it from
        all other instances' edge sorted sets via SCAN + ZREM.

        Args:
            model_instance: The Model instance being deleted.
            field_name: Name of this CoOccurrenceField.
            field_value: Current field value (unused).
            pipeline: Optional Redis pipeline.
            **kwargs: Additional context.
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, CoOccurrenceField):
            return super().on_delete(
                model_instance, field_name, field_value, pipeline=pipeline, **kwargs
            )

        # Get this instance's PK from its redis key
        member_key = kwargs.get("saved_redis_key") or model_instance.db_key.redis_key

        # Get the edge key for this instance
        edge_key = field.get_edge_key(model_instance, member_key)

        if field.symmetric:
            # Get all linked PKs so we can remove reverse edges
            linked = POPOTO_REDIS_DB.zrange(edge_key, 0, -1)
            for target_pk in linked:
                if isinstance(target_pk, bytes):
                    target_pk = target_pk.decode("utf-8")
                target_edge_key = field.get_edge_key(model_instance, target_pk)
                if pipeline:
                    pipeline.zrem(target_edge_key, member_key)
                else:
                    POPOTO_REDIS_DB.zrem(target_edge_key, member_key)

        # Delete this instance's edge sorted set
        if pipeline:
            pipeline.delete(edge_key)
        else:
            POPOTO_REDIS_DB.delete(edge_key)

        return super().on_delete(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )
