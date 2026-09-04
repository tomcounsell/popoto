"""
Query Layer for Popoto Redis ORM.

This module provides the Query class, which serves as the primary interface for
retrieving Model instances from Redis. It follows a Django-inspired query API,
allowing developers familiar with Django's ORM to quickly adapt to Popoto.

Design Philosophy:
-----------------
Popoto treats Redis not as a simple key-value cache, but as a first-class
database with rich querying capabilities. The Query class abstracts away the
complexity of Redis data structures (sets, sorted sets, hash maps) and presents
a unified, intuitive interface.

Key architectural decisions:
1. **Set Intersection Strategy**: Filter operations return sets of Redis keys,
   which are then intersected to combine multiple filters. This leverages Redis's
   O(N*M) SINTER performance for efficient multi-criteria queries.

2. **Field-Delegated Filtering**: Each field type (KeyField, SortedField, etc.)
   implements its own `filter_query()` method. The Query class orchestrates these
   but delegates the actual Redis commands to the field implementations.

3. **Sorted Fields First**: When processing filters, sorted fields are evaluated
   before key fields. Sorted fields often produce smaller result sets (range queries)
   and may satisfy multiple filter parameters at once due to partitioning.

4. **Pipeline Optimization**: Bulk retrieval uses Redis pipelines to batch
   HGETALL commands, dramatically reducing round-trip latency when fetching
   multiple objects.

Usage Examples:
--------------
    # Get a single object by key fields
    user = User.query.get(username="alice")

    # Filter with multiple criteria
    products = Product.query.filter(category="electronics", price__lte=100.0)

    # Count without loading objects
    count = Order.query.count(status="pending")

    # Retrieve specific fields only (projection)
    names = User.query.filter(active=True, values=("name", "email"))

See Also:
---------
- `popoto.models.base.Model` - The base class that exposes `Model.query`
- `popoto.fields.key_field_mixin.KeyFieldMixin` - Key field filtering logic
- `popoto.fields.sorted_field_mixin.SortedFieldMixin` - Range query logic
"""

import asyncio
import logging
import threading
import weakref
from asyncio import to_thread
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any, Callable, Optional

from .canonical_key import canonical_key_str
from .db_key import DB_key

if TYPE_CHECKING:
    from .base import Model, ModelOptions
    from ..fields.sorted_field_mixin import SortedFieldMixin

from ..redis_db import (
    POPOTO_REDIS_DB,
    get_async_redis_db,
    normalize_redis_keys,
    run_lua,
)
from ..fields.constants import Defaults

logger = logging.getLogger("POPOTO.Query")


@dataclass
class _PushdownState:
    """Per-call snapshot of the bookkeeping ``filter_for_keys_set`` writes.

    ``Query`` is instantiated once per model class (``models/base.py``), so
    every ``async_filter`` call on a model shares one ``self``. The sync path
    reads these attributes off ``self`` with no yield point between write and
    read; the async path has two awaits in between, so a second coroutine can
    overwrite all seven fields mid-flight. The async path therefore snapshots
    them into one of these inside the single ``to_thread`` hop and never reads
    ``self._pushdown_*`` again.
    """

    sorted_field_order: "Optional[list[Any]]" = None
    sorted_field_name: "Optional[str]" = None
    pending_client_filters: "dict[str, Any]" = dataclass_field(default_factory=dict)
    pushdown_limit: "Optional[int]" = None
    pushdown_requested: int = 0
    pushdown_fetched: int = 0
    pushdown_partition: "dict[str, Any]" = dataclass_field(default_factory=dict)


# One lock per running event loop, built lazily. A module-level asyncio.Lock()
# constructed at import binds the first loop that awaits it and raises on every
# other one (redis_db._async_redis_lock has that shape, and tests/test_async.py
# carries an autouse fixture solely to reassign it per test). The
# WeakKeyDictionary self-cleans as loops are collected, so a per-test loop needs
# no fixture. The threading.Lock guards the dict only; no I/O happens under it.
_PUSHDOWN_LOCKS: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]"
) = weakref.WeakKeyDictionary()
_PUSHDOWN_LOCKS_GUARD = threading.Lock()


def _pushdown_lock_for_running_loop() -> "asyncio.Lock":
    """Return the pushdown lock belonging to the currently running loop."""
    loop = asyncio.get_running_loop()
    with _PUSHDOWN_LOCKS_GUARD:
        lock = _PUSHDOWN_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _PUSHDOWN_LOCKS[loop] = lock
        return lock


class QueryException(Exception):
    """Raised when a query is malformed or produces an unexpected result.

    Common causes include:
    - Using unknown filter parameters not supported by any field
    - Using `get()` when multiple objects match the criteria
    - Specifying `order_by` without including that field in `values`
    - Missing required partition fields for sorted field queries
    """

    pass


def _fire_on_read(model_class, instances):
    """Fire on_read() for AccessTrackerMixin models after hydration.

    Batches all RPUSH commands into a single pipeline for efficiency.
    Only fires when the model class uses AccessTrackerMixin and
    _track_reads is True.

    Args:
        model_class: The Model class being queried
        instances: List of hydrated model instances
    """
    from ..fields.access_tracker import AccessTrackerMixin

    if not issubclass(model_class, AccessTrackerMixin):
        return
    if not getattr(model_class, "_track_reads", True):
        return
    valid = [
        inst
        for inst in instances
        if hasattr(inst, "_redis_key") or hasattr(inst, "db_key")
    ]
    if not valid:
        return
    pipe = POPOTO_REDIS_DB.pipeline()
    for inst in valid:
        inst.on_read(pipeline=pipe)
    pipe.execute()


class QueryBuilder:
    """Chainable query builder that accumulates query state.

    This class provides a fluent interface for building queries incrementally.
    Each method returns self (or a new QueryBuilder) to enable method chaining.

    The QueryBuilder is returned by Query.filter() and accumulates filter
    parameters, ordering, and limits until execution methods like all() are called.

    Example:
        # Chainable query construction
        results = Model.query.filter(status="active").order_by("name").limit(10).all()

        # Multiple filter chaining
        results = Model.query.filter(status="active").filter(type="premium").all()

    Note:
        QueryBuilder also acts as a list-like object for backward compatibility.
        Iterating over a QueryBuilder or accessing len() will execute the query.
    """

    def __init__(self, query: "Query", filters: dict = None, q_objects: list = None):
        """Initialize a QueryBuilder with a reference to the parent Query.

        Args:
            query: The Query instance this builder operates on
            filters: Initial filter parameters (optional)
            q_objects: List of Q objects for complex query logic (optional)
        """
        self._query = query
        self._filters = filters.copy() if filters else {}
        self._q_objects = list(q_objects) if q_objects else []
        self._limit_value = None
        self._order_by_value = None
        self._values_tuple = None
        self._computed_sort_fn = None
        self._computed_sort_reverse = False
        self._no_track = False
        # Point-in-time epoch for validity gating (#580). None = "now". Set for
        # the duration of one composite_score() / top_by_decay() call so the
        # decay-Lua gate (layer 1) and the composite validity mask (layer 2)
        # evaluate membership at the SAME instant. Without it an as-of query
        # could only ever narrow the now-valid set, never reconstruct history.
        self._validity_as_of: Optional[float] = None

    def filter(self, *args, **kwargs) -> "QueryBuilder":
        """Add filter criteria and return a new QueryBuilder.

        Creates a new QueryBuilder with merged filter parameters, allowing
        multiple filter() calls to be chained. Supports Q objects and Expression
        objects for complex query logic with OR/AND/NOT operators.

        Args:
            *args: Q objects or Expression objects for complex query expressions
            **kwargs: Filter parameters to add to the query

        Returns:
            A new QueryBuilder with the combined filters

        Example:
            query = Model.query.filter(status="active").filter(type="premium")
            query = Model.query.filter(Q(status="active") | Q(type="premium"))
            query = Model.query.filter(Model.rating > 4.0)
        """
        from .q import Q
        from .expressions import Expression, CombinedExpression

        # Create a new QueryBuilder with merged filters and Q objects
        new_builder = QueryBuilder(self._query, self._filters, self._q_objects)
        new_builder._filters.update(kwargs)

        # Process args - can be Q objects or Expression objects
        for arg in args:
            if isinstance(arg, Q):
                new_builder._q_objects.append(arg)
            elif isinstance(arg, (Expression, CombinedExpression)):
                # Convert Expression to Q object
                new_builder._q_objects.append(arg.to_q())

        new_builder._limit_value = self._limit_value
        new_builder._order_by_value = self._order_by_value
        new_builder._values_tuple = self._values_tuple
        new_builder._computed_sort_fn = self._computed_sort_fn
        new_builder._computed_sort_reverse = self._computed_sort_reverse
        new_builder._no_track = self._no_track
        return new_builder

    def limit(self, n: int) -> "QueryBuilder":
        """Set the maximum number of results to return.

        Args:
            n: Maximum number of results

        Returns:
            Self for method chaining

        Example:
            results = Model.query.filter(status="active").limit(10).all()
        """
        self._limit_value = n
        return self

    def order_by(self, field: str) -> "QueryBuilder":
        """Set the field to order results by.

        Args:
            field: Field name to sort by. Prefix with "-" for descending order.

        Returns:
            Self for method chaining

        Example:
            results = Model.query.filter(status="active").order_by("-created_at").all()
        """
        self._order_by_value = field
        return self

    def values(self, *fields) -> "QueryBuilder":
        """Specify fields to return as dicts instead of model instances.

        Args:
            *fields: Field names to include in the result dicts

        Returns:
            Self for method chaining

        Example:
            results = Model.query.filter(status="active").values("name", "email").all()
        """
        self._values_tuple = fields
        return self

    def computed_sort(self, fn, reverse: bool = False) -> "QueryBuilder":
        """Sort results using a caller-provided key function.

        Applies a Python-side sort after fetching results from Redis, before
        applying limit(). This enables sorting by computed/derived values that
        are not stored as indexed fields.

        When both computed_sort() and order_by() are set, computed_sort() takes
        precedence and order_by() is ignored.

        Performance note: This is O(N log N) on the full result set before
        limiting. For large result sets (>10K records), consider using
        SortedField indexes instead.

        Args:
            fn: A callable that takes a model instance (or dict if values()
                is used) and returns a sort key. Must not be None.
            reverse: If True, sort in descending order. Default is False.

        Returns:
            Self for method chaining.

        Raises:
            TypeError: If fn is None.

        Example:
            # Sort by a computed activation score
            results = (
                Model.query.filter(status="active")
                .computed_sort(lambda x: x.priority * 0.5 + x.score * 0.5,
                               reverse=True)
                .limit(10)
                .all()
            )
        """
        if fn is None:
            raise TypeError("computed_sort() requires a callable, got None")
        self._computed_sort_fn = fn
        self._computed_sort_reverse = reverse
        return self

    def no_track(self) -> "QueryBuilder":
        """Suppress on_read() tracking for this query.

        Use for internal operations (reindex, migration) that shouldn't
        count as reads for AccessTrackerMixin models.

        Returns:
            Self for method chaining

        Example:
            results = Model.query.filter(status="active").no_track().all()
        """
        self._no_track = True
        return self

    def top_by_decay(
        self,
        field_name=None,
        n=10,
        decay_rate=None,
        base_score_field=None,
        *,
        as_of=None,
    ):
        """Return top-N instances ranked by time-decayed score.

        Executes a Lua script server-side that computes:
            decayed_score = base_score * elapsed_days ^ (-decay_rate)

        Args:
            field_name: Name of a DecayingSortedField on the model. Optional
                when the model has exactly one DecayingSortedField (or subclass).
            n: Maximum number of results to return. Default 10.
            decay_rate: Override the field's decay_rate for this query.
            base_score_field: Override the field's base_score_field for this query.
            as_of: Keyword-only. Epoch seconds at which validity membership is
                evaluated (issue #580). ``None`` means "now". Only meaningful
                for models declaring a ``ValidityField``; ignored otherwise.

        Returns:
            List of model instances in decayed-score order. Ordering is
            deterministic: ties are broken inside the Lua script by member
            key (redis_key) ascending, byte-wise, before the ``n``
            truncation, so identical calls always return identical
            orderings -- including which members survive the cutoff.

        Raises:
            QueryException: If field is not a DecayingSortedField or
                required partition_by filter is missing.
        """
        from ..fields.decaying_sorted_field import DecayingSortedField, DECAY_SCORE_LUA
        from .encoding import decode_popoto_model_hashmap

        model_class = self._query.model_class

        if field_name is None:
            dsf_names = [
                name
                for name, f in model_class._meta.fields.items()
                if isinstance(f, DecayingSortedField)
            ]
            if len(dsf_names) == 1:
                field_name = dsf_names[0]
            elif len(dsf_names) == 0:
                raise QueryException(
                    f"'{model_class.__name__}' has no DecayingSortedField"
                )
            else:
                raise QueryException(
                    f"Multiple DecayingSortedFields on '{model_class.__name__}': "
                    f"{dsf_names}. Specify field_name explicitly."
                )
        elif field_name not in model_class._meta.fields:
            raise QueryException(
                f"'{model_class.__name__}' has no field '{field_name}'"
            )

        field = model_class._meta.fields[field_name]
        if not isinstance(field, DecayingSortedField):
            raise QueryException(
                f"top_by_decay() requires a DecayingSortedField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        # Use field defaults unless overridden
        effective_decay_rate = (
            decay_rate if decay_rate is not None else field.decay_rate
        )
        effective_base_score_field = (
            base_score_field
            if base_score_field is not None
            else (field.base_score_field or "")
        )

        if n <= 0:
            return []

        # Build the sorted set key respecting partition_by
        try:
            partition_values = [
                canonical_key_str(self._filters[pf]) for pf in field.partition_by
            ]
        except KeyError:
            missing = [pf for pf in field.partition_by if pf not in self._filters]
            raise QueryException(
                f"top_by_decay() on '{field_name}' requires partition filter(s): "
                f"{', '.join(missing)}"
            )

        # Use actual field class for key generation (CyclicDecayField has
        # its own field_class_key prefix, distinct from DecayingSortedField)
        sortedset_db_key = field.__class__.get_sortedset_db_key(
            model_class, field_name, *partition_values
        )

        import time

        now = time.time()

        # Use extended Lua script for CyclicDecayField, plain script otherwise
        from ..fields.cyclic_decay_field import CyclicDecayField, CYCLIC_DECAY_LUA
        from ..fields.decaying_sorted_field import (
            confidence_modulation_args,
            validity_gate_args,
        )

        # Confidence-modulated decay (#491). Resolves to ("", "0", ...) whenever
        # modulation is off, which makes the Lua guard skip the extra HGET and
        # produce byte-identical scores.
        conf_hash_key, conf_s, conf_c0 = confidence_modulation_args(
            model_class, field, field_name, filters=self._filters
        )

        # Validity gating (#580, plan D5). Resolves to ("", "", "") whenever the
        # model has no ValidityField or the kill switch is off, which disables
        # the gate inside the script. On a plain DecayingSortedField,
        # top_by_decay is the one path where this layer is *authoritative*: its
        # result is the member list itself, with no ZUNIONSTORE afterwards to
        # reintroduce a skipped member.
        #
        # NOT so for a CyclicDecayField: the branch below dispatches to
        # CYCLIC_DECAY_LUA, which is deliberately left ungated (an explicit plan
        # No-Go — its KEYS 1-4 are taken and its header comment forbids
        # renumbering). So a *direct* top_by_decay() call on a CyclicDecayField
        # returns superseded records. Assembler paths are unaffected: they never
        # call top_by_decay, and layers 2 and 3 cover them. Pinned by
        # tests/test_validity_field.py::TestCyclicDecayGatingGap and documented
        # under "Known limitations" in docs/features/validity-and-supersession.md
        # — if you gate the cyclic script, update all three.
        gate_invalid_key, gate_valid_key, gate_as_of = validity_gate_args(
            model_class, as_of=as_of
        )

        if isinstance(field, CyclicDecayField):
            # Build companion hash keys from partition values
            cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
                model_class, field_name, *partition_values
            )
            pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
                model_class, field_name, *partition_values
            )

            result = run_lua(
                POPOTO_REDIS_DB,
                CYCLIC_DECAY_LUA,
                # numkeys: zset + cycles + pressure + confidence (KEYS[4]).
                # Passing the confidence key without bumping this would shunt
                # it into ARGV and silently disable modulation.
                4,
                sortedset_db_key.redis_key,
                cycles_hash_key,
                pressure_hash_key,
                conf_hash_key,
                str(now),
                str(effective_decay_rate),
                str(n),
                effective_base_score_field,
                conf_s,
                conf_c0,
            )
        else:
            result = run_lua(
                POPOTO_REDIS_DB,
                DECAY_SCORE_LUA,
                # numkeys: zset + confidence (KEYS[2]) + invalid_at (KEYS[3]) +
                # valid_from (KEYS[4]). Passing the validity keys without
                # bumping this would shunt them into ARGV and silently corrupt
                # base_score_field / the confidence params (plan Risk 1).
                4,
                sortedset_db_key.redis_key,
                conf_hash_key,
                gate_invalid_key,
                gate_valid_key,
                str(now),
                str(effective_decay_rate),
                str(n),
                effective_base_score_field,
                conf_s,
                conf_c0,
                gate_as_of,  # ARGV[7]
            )

        if not result:
            return []

        # Parse result: [key1, score1, key2, score2, ...]
        redis_keys = []
        for i in range(0, len(result), 2):
            key = result[i]
            if isinstance(key, bytes):
                key = key.decode()
            redis_keys.append(key)

        if not redis_keys:
            return []

        # Fetch model instances via pipeline
        pipe = POPOTO_REDIS_DB.pipeline()
        for key in redis_keys:
            pipe.hgetall(key)
        raw_results = pipe.execute()

        instances = []
        for key, data in zip(redis_keys, raw_results):
            if data:
                instance = decode_popoto_model_hashmap(
                    model_class, data, source_redis_key=key
                )
                instances.append(instance)

        if not self._no_track:
            _fire_on_read(model_class, instances)

        return instances

    def composite_score(
        self,
        indexes: dict,
        limit: int = 10,
        aggregate: str = "SUM",
        min_score: float = None,
        post_filter: Optional[Callable[[str, float], bool]] = None,
        co_occurrence_boost: dict = None,
        similarity_boost: dict = None,
        temperature: float = 1.0,
        *,
        as_of: Optional[float] = None,
    ) -> list:
        """Return top-K instances ranked by a weighted composite of multiple indexes.

        Combines N sorted set indexes with configurable weights via Redis
        ZUNIONSTORE and returns model instances ranked by composite score.

        Each index name maps to a field on the model. Supported field types:
            - DecayingSortedField / CyclicDecayField: Materializes decay-computed
              scores into a temp ZSET via the existing Lua decay script.
            - SortedFieldMixin fields: Uses the sorted set directly.
            - WriteFilter priority: Resolves ``$WF:{Class}:priority`` directly.
            - ConfidenceField: Materializes confidence values from companion hash.
            - AccessTracker: Materializes access_count from meta hashes.

        Args:
            indexes: Mapping of field names to weights, e.g.
                ``{"relevance": 0.4, "confidence": 0.3}``. Weights are arbitrary
                positive floats; relative ratios matter, not absolute values.
            limit: Maximum results to return. Default 10.
            aggregate: Aggregation mode for ZUNIONSTORE: "SUM", "MIN", or "MAX".
                Default "SUM".
            min_score: Optional minimum composite score threshold. Results below
                this score are excluded. Note: ``min_score`` is applied to raw
                composite scores **before** temperature scaling, while
                ``post_filter`` receives temperature-scaled scores.
            post_filter: Optional callable ``(redis_key, score) -> bool``. Applied
                after ZREVRANGE but before hydration. Return True to keep.
            co_occurrence_boost: Optional dict ``{redis_key: weight}`` from
                ``CoOccurrenceField.propagate()``. Injected as an additional
                index in the composite.
            similarity_boost: Optional dict ``{redis_key: score}`` from
                ``semantic_search()``. Injected as an additional index
                in the composite, identical mechanism to co_occurrence_boost.
            temperature: Scales composite scores by dividing each score by this
                value. Low temperature (0.02-0.1) sharpens discrimination so top
                scores dominate. Default 1.0 preserves current behavior. High
                temperature (2.0+) flattens scores toward uniform. Must be > 0.
            as_of: Keyword-only. Epoch seconds at which validity membership is
                evaluated (issue #580); ``None`` means "now". Both the decay-arm
                pre-trim and the post-union validity mask read this same instant,
                so ``as_of`` genuinely reconstructs the past rather than merely
                narrowing the currently-valid set. Ignored for models that
                declare no ``ValidityField``, which is every model that has not
                opted in.

        Returns:
            List of model instances ranked by composite score (descending).

        Raises:
            QueryException: If indexes is empty, contains invalid field names,
                references fields without sorted set indexes, or temperature <= 0.

        Example:
            results = Memory.query.filter(agent_id="agent-1").composite_score(
                indexes={"relevance": 0.4, "confidence": 0.3, "access_score": 0.2},
                limit=10,
                temperature=0.1,  # sharp retrieval -- top result dominates
            )
        """
        import uuid

        from .encoding import decode_popoto_model_hashmap

        model_class = self._query.model_class

        # Park the as-of on the builder for the duration of this call so
        # _materialize_decay_field (reached via _resolve_index) and
        # _apply_validity_mask agree on the instant, without threading a
        # parameter through _resolve_index's field-type dispatch.
        self._validity_as_of = as_of

        # --- Validate inputs ---
        if not indexes:
            raise QueryException("composite_score() requires a non-empty indexes dict")

        if limit <= 0:
            return []

        aggregate = aggregate.upper()
        if aggregate not in ("SUM", "MIN", "MAX"):
            raise QueryException(
                f"aggregate must be 'SUM', 'MIN', or 'MAX' (got '{aggregate}')"
            )

        if temperature <= 0:
            raise QueryException(f"temperature must be > 0 (got {temperature})")

        # --- Resolve each index to a Redis sorted set key ---
        resolved_keys = {}  # {redis_zset_key: weight}
        temp_keys = []  # keys to clean up after
        uid = uuid.uuid4().hex[:8]
        model_name = model_class.__name__

        for field_name, weight in indexes.items():
            resolved_key = self._resolve_index(
                model_class, field_name, weight, uid, temp_keys
            )
            if resolved_key:
                resolved_keys[resolved_key] = weight

        # --- Handle co_occurrence_boost ---
        if co_occurrence_boost:
            co_key = f"$CSQ:{model_name}:co_occurrence:{uid}"
            POPOTO_REDIS_DB.zadd(
                co_key,
                {str(k): float(v) for k, v in co_occurrence_boost.items()},
            )
            POPOTO_REDIS_DB.expire(co_key, 5)
            temp_keys.append(co_key)
            resolved_keys[co_key] = 1.0  # weight already in the scores

        # --- Handle similarity_boost ---
        if similarity_boost:
            sim_key = f"$CSQ:{model_name}:similarity:{uid}"
            POPOTO_REDIS_DB.zadd(
                sim_key,
                {str(k): float(v) for k, v in similarity_boost.items()},
            )
            POPOTO_REDIS_DB.expire(sim_key, 5)
            temp_keys.append(sim_key)
            resolved_keys[sim_key] = 1.0  # weight already in the scores

        if not resolved_keys:
            self._cleanup_temp_keys(temp_keys)
            return []

        # --- ZUNIONSTORE ---
        composite_key = f"$CSQ:{model_name}:{uid}"
        temp_keys.append(composite_key)

        try:
            POPOTO_REDIS_DB.zunionstore(
                composite_key,
                resolved_keys,
                aggregate=aggregate,
            )
            POPOTO_REDIS_DB.expire(composite_key, 5)

            # --- Validity mask (#580, plan D5b) ---
            # Must run BEFORE the top-K read: the decay-Lua gate only makes a
            # closed member *absent from the decay arm*, and under AGGREGATE SUM
            # an absent member scores 0 rather than being removed -- so it still
            # surfaces on whatever a ConfidenceField / co-occurrence /
            # similarity arm gives it. Skipping is not excluding; this is.
            # Subtracts only demonstrably-invalid members: a record with no
            # interval entry is unmanaged and stays visible (see the method).
            self._apply_validity_mask(
                composite_key, model_name, uid, temp_keys, as_of=as_of
            )

            # --- ZREVRANGE top-K ---
            if min_score is not None:
                raw_results = POPOTO_REDIS_DB.zrevrangebyscore(
                    composite_key,
                    "+inf",
                    str(min_score),
                    start=0,
                    num=limit,
                    withscores=True,
                )
            else:
                raw_results = POPOTO_REDIS_DB.zrevrange(
                    composite_key, 0, limit - 1, withscores=True
                )

            if not raw_results:
                return []

            # --- Temperature scaling ---
            if temperature != 1.0:
                raw_results = [
                    (member, score / temperature) for member, score in raw_results
                ]

            # --- Post-filter ---
            pks = []
            for member, score in raw_results:
                if isinstance(member, bytes):
                    member = member.decode()
                if post_filter is not None and not post_filter(member, score):
                    continue
                pks.append(member)

            if not pks:
                return []

            # --- Hydrate models ---
            pipe = POPOTO_REDIS_DB.pipeline()
            for key in pks:
                pipe.hgetall(key)
            hashes = pipe.execute()

            instances = []
            for key, data in zip(pks, hashes):
                if data:
                    instance = decode_popoto_model_hashmap(
                        model_class, data, source_redis_key=key
                    )
                    instances.append(instance)

            if not self._no_track:
                _fire_on_read(model_class, instances)

            return instances

        finally:
            self._cleanup_temp_keys(temp_keys)
            self._validity_as_of = None

    def semantic_search(
        self,
        query_text: str,
        indexes: dict = None,
        limit: int = 10,
        aggregate: str = "SUM",
        min_score: float = None,
        post_filter: Optional[Callable[[str, float], bool]] = None,
        co_occurrence_boost: dict = None,
        temperature: float = 1.0,
    ) -> list:
        """Return top-K instances ranked by semantic similarity combined with memory signals.

        Embeds the query text via the configured provider, computes cosine
        similarity against all stored embeddings, and injects similarity
        scores into composite_score() via the similarity_boost parameter.

        Args:
            query_text: The text to search for semantically.
            indexes: Optional mapping of field names to weights for
                composite scoring alongside similarity. If None, returns
                results ranked by similarity alone.
            limit: Maximum results to return. Default 10.
            aggregate: Aggregation mode for ZUNIONSTORE. Default "SUM".
            min_score: Optional minimum composite score threshold.
            post_filter: Optional callable (redis_key, score) -> bool.
            co_occurrence_boost: Optional {redis_key: weight} dict.
            temperature: Score scaling factor. Default 1.0.

        Returns:
            List of model instances ranked by combined score (descending).
            Returns empty list if query_text is empty, no provider is
            configured, or no embeddings exist.

        Example:
            results = Memory.query.semantic_search(
                "revenue trends",
                indexes={"relevance": 0.4, "confidence": 0.3},
                limit=10,
            )
        """
        if not query_text or not query_text.strip():
            return []

        model_class = self._query.model_class

        # Find the EmbeddingField on the model
        from ..fields.embedding_field import EmbeddingField

        embedding_field = None
        for fname, field in model_class._meta.fields.items():
            if isinstance(field, EmbeddingField):
                embedding_field = field
                break

        if embedding_field is None:
            raise QueryException(
                f"{model_class.__name__} has no EmbeddingField for semantic_search()"
            )

        provider = embedding_field.provider
        if provider is None:
            return []

        # Embed the query text
        try:
            query_vectors = provider.embed([query_text], input_type="query")
            if not query_vectors or not query_vectors[0]:
                return []
        except Exception as e:
            logger.error(f"semantic_search embedding failed: {e}")
            return []

        # Load cached embeddings for this model class
        try:
            import numpy as np
        except ImportError:
            raise QueryException(
                "numpy is required for semantic_search(). "
                "Install with: pip install popoto[embeddings]"
            )

        matrix, keys = EmbeddingField.load_embeddings(model_class)
        if matrix is None or len(keys) == 0:
            return []

        # Compute cosine similarity (matrix is pre-normalized)
        query_vec = np.array(query_vectors[0], dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []
        query_vec = query_vec / query_norm

        similarities = matrix @ query_vec  # dot product on unit vectors

        # Build similarity_boost dict: {redis_key: similarity_score}
        similarity_boost = {}
        for i, score in enumerate(similarities):
            if score > 0:  # Only include positive similarities
                similarity_boost[keys[i]] = float(score)

        if not similarity_boost:
            return []

        # If no additional indexes provided, use similarity-only mode
        if not indexes:
            # Direct similarity ranking without composite_score overhead
            return self._similarity_only_search(
                model_class, similarity_boost, limit, temperature, post_filter
            )

        # Delegate to composite_score with similarity_boost
        return self.composite_score(
            indexes=indexes,
            limit=limit,
            aggregate=aggregate,
            min_score=min_score,
            post_filter=post_filter,
            co_occurrence_boost=co_occurrence_boost,
            similarity_boost=similarity_boost,
            temperature=temperature,
        )

    def _similarity_only_search(
        self, model_class, similarity_boost, limit, temperature, post_filter
    ):
        """Rank and return instances using only similarity scores.

        Used when semantic_search() is called without additional indexes.
        Creates a temp ZSET from similarity scores and hydrates top-K.
        """
        import uuid
        from .encoding import decode_popoto_model_hashmap

        uid = uuid.uuid4().hex[:8]
        model_name = model_class.__name__
        sim_key = f"$CSQ:{model_name}:sim_only:{uid}"

        try:
            POPOTO_REDIS_DB.zadd(
                sim_key,
                {str(k): float(v) for k, v in similarity_boost.items()},
            )
            POPOTO_REDIS_DB.expire(sim_key, 5)

            raw_results = POPOTO_REDIS_DB.zrevrange(
                sim_key, 0, limit - 1, withscores=True
            )

            if not raw_results:
                return []

            # Temperature scaling
            if temperature != 1.0:
                raw_results = [
                    (member, score / temperature) for member, score in raw_results
                ]

            # Post-filter
            pks = []
            for member, score in raw_results:
                if isinstance(member, bytes):
                    member = member.decode()
                if post_filter is not None and not post_filter(member, score):
                    continue
                pks.append(member)

            if not pks:
                return []

            # Hydrate
            pipe = POPOTO_REDIS_DB.pipeline()
            for key in pks:
                pipe.hgetall(key)
            hashes = pipe.execute()

            instances = []
            for key, data in zip(pks, hashes):
                if data:
                    instance = decode_popoto_model_hashmap(
                        model_class, data, source_redis_key=key
                    )
                    instances.append(instance)

            return instances
        finally:
            POPOTO_REDIS_DB.delete(sim_key)

    def keyword_search(
        self,
        query_text: str,
        field: str = None,
        limit: int = 10,
    ) -> list:
        """Return instances ranked by BM25 keyword relevance.

        Delegates to BM25Field.search() and hydrates model instances.
        The BM25 score is attached to each instance as ``_bm25_score``.

        Args:
            query_text: The search query string.
            field: Name of the BM25Field to search. Optional when the model
                has exactly one BM25Field.
            limit: Maximum results to return. Default 10.

        Returns:
            List of model instances ranked by BM25 score (descending).

        Raises:
            QueryException: If the model has no BM25Field or field name
                is invalid.
        """
        from .encoding import decode_popoto_model_hashmap
        from ..fields.bm25_field import BM25Field

        model_class = self._query.model_class

        # Auto-detect BM25Field if not specified
        if field is None:
            bm25_fields = [
                name
                for name, f in model_class._meta.fields.items()
                if isinstance(f, BM25Field)
            ]
            if len(bm25_fields) == 1:
                field = bm25_fields[0]
            elif len(bm25_fields) == 0:
                raise QueryException(
                    f"'{model_class.__name__}' has no BM25Field for keyword_search()"
                )
            else:
                raise QueryException(
                    f"Multiple BM25Fields on '{model_class.__name__}': "
                    f"{bm25_fields}. Specify field= explicitly."
                )

        # Get raw scored results
        scored = BM25Field.search(model_class, field, query_text, limit=limit)
        if not scored:
            return []

        # Hydrate model instances
        pipe = POPOTO_REDIS_DB.pipeline()
        for key, _score in scored:
            pipe.hgetall(key)
        hashes = pipe.execute()

        instances = []
        for (key, score), data in zip(scored, hashes):
            if data:
                instance = decode_popoto_model_hashmap(
                    model_class, data, source_redis_key=key
                )
                instance._bm25_score = score
                instances.append(instance)

        if not self._no_track:
            _fire_on_read(model_class, instances)

        return instances

    def fuse(
        self,
        k: int = 60,
        limit: int = 10,
        post_filter: Optional[Callable] = None,
        weights: Optional[dict] = None,
        **ranked_lists,
    ) -> list:
        """Reciprocal Rank Fusion across heterogeneous ranked lists.

        Combines multiple ranked lists from different retrieval signals
        (keyword search, semantic search, graph propagation, etc.) using
        the RRF formula: ``score(d) = sum(w_i / (k + rank_i(d)))``

        Each ranked list is a sequence of ``(redis_key, score)`` tuples.
        The scores are used only for ordering within each list -- RRF uses
        ranks, not raw scores.

        Args:
            k: RRF constant (default 60). Higher values reduce the influence
                of high-ranking items. Standard value from Cormack et al.
            limit: Maximum results to return. Default 10.
            post_filter: Optional ``(redis_key, rrf_score) -> bool`` callback.
                Return True to keep the result.
            weights: Optional ``{list_name: multiplier}`` mapping applying a
                per-list weight to the RRF sum. Lists not present in the
                mapping default to a weight of ``1.0``. ``None`` (the
                default) is byte-for-byte equivalent to unweighted RRF --
                fully backward compatible. Must be passed as an explicit
                keyword argument (never inside ``**ranked_lists``).
            **ranked_lists: Named ranked lists. Each value is a list of
                ``(redis_key, score)`` tuples sorted by score descending.

        Scope (#576): the ranked lists are supplied by the caller and carry no
        scope of their own -- ``BM25Field.search()`` and graph propagation both
        return every matching key in the database. So this builder's own
        keyword filters are applied to the fused set, before the top-K slice,
        which means ``Model.query.filter(agent_id=...).fuse(...)`` is scoped as
        it reads and the ``limit`` still backfills with in-scope candidates.
        Filters on unindexed plain ``Field``s are honored too, at the cost of
        hydrating the surviving candidates to evaluate them.

        Returns:
            List of model instances ranked by RRF score (descending).

        Raises:
            QueryException: If no ranked lists are provided, or if the builder
                carries Q-object filters. Q objects cannot be resolved to a key
                set here, and fusing an unscoped set under a query that reads
                as filtered is the leak this scoping exists to prevent, so
                fuse() refuses rather than silently widening. Use keyword
                filters or a ``post_filter`` callback instead.

        Example:
            results = Memory.query.fuse(
                keyword=BM25Field.search(Memory, "content", "redis", limit=50),
                semantic=[(key, sim) for key, sim in similarity_results],
                weights={"keyword": 1.0, "semantic": 0.4},
                k=60,
                limit=10,
            )
        """
        from .encoding import decode_popoto_model_hashmap

        model_class = self._query.model_class

        if not ranked_lists:
            raise QueryException(
                "fuse() requires at least one ranked list as a keyword argument"
            )

        # Compute RRF scores
        rrf_scores = {}  # doc_key -> accumulated RRF score

        for list_name, ranked_list in ranked_lists.items():
            if not ranked_list:
                continue
            list_weight = 1.0 if weights is None else weights.get(list_name, 1.0)
            for rank_idx, (doc_key, _score) in enumerate(ranked_list):
                doc_key = str(doc_key)
                rrf_scores[doc_key] = rrf_scores.get(doc_key, 0.0) + (
                    list_weight * (1.0 / (k + rank_idx + 1))
                )

        if not rrf_scores:
            return []

        # Sort by RRF score descending
        sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        # Apply this builder's own filters (#576). The ranked lists are supplied
        # by the caller and carry no scope of their own -- BM25Field.search() and
        # graph propagation both return every matching key in the database. So
        # `Model.query.filter(agent_id=...).fuse(...)` read as scoped while
        # fusing across every agent's records, leaking one agent's memories into
        # another's retrieval. Filtering happens BEFORE the top-K slice so the
        # limit backfills with the next-best in-scope candidates instead of
        # returning short.
        # Instances already decoded by the client-side filter pass below, reused
        # by hydration so a plain-field filter costs one HGETALL per candidate.
        prefetched: dict[str, Any] = {}
        if self._filters or self._q_objects:
            if self._q_objects:
                # filter_for_keys_set() resolves kwargs only. Rather than fuse an
                # unfiltered set under a query that reads as filtered, refuse.
                raise QueryException(
                    "fuse() cannot honor Q-object filters; the ranked lists are "
                    "unscoped and would fuse across the whole keyspace. Use "
                    "keyword filters, or pass a post_filter callback."
                )
            # filter_for_keys_set() returns bytes; the ranked lists carry str.
            allowed_keys = normalize_redis_keys(
                self._query.filter_for_keys_set(**self._filters)
            )
            sorted_results = [
                (key, score) for key, score in sorted_results if key in allowed_keys
            ]
            # A filter on a plain (unindexed) Field has no index to resolve, so
            # filter_for_keys_set() stashes it in _pending_client_filters and
            # hands back the whole keyspace -- making the intersection above a
            # no-op and reopening the exact cross-scope leak (#576) for that
            # filter class. Every other read path applies the stash client-side;
            # do the same here, before the top-K slice so the limit still
            # backfills with in-scope candidates.
            client_filters = getattr(self._query, "_pending_client_filters", None) or {}
            if client_filters and sorted_results:
                pipe = POPOTO_REDIS_DB.pipeline()
                for key, _score in sorted_results:
                    pipe.hgetall(key)
                in_scope = []
                for (key, score), data in zip(sorted_results, pipe.execute()):
                    if not data:
                        continue
                    instance = decode_popoto_model_hashmap(
                        model_class, data, source_redis_key=key
                    )
                    if all(
                        getattr(instance, fname, None) == expected
                        for fname, expected in client_filters.items()
                    ):
                        prefetched[key] = instance
                        in_scope.append((key, score))
                sorted_results = in_scope
            if not sorted_results:
                return []

        # Apply post_filter
        if post_filter is not None:
            sorted_results = [
                (key, score) for key, score in sorted_results if post_filter(key, score)
            ]

        # Take top-K
        sorted_results = sorted_results[:limit]

        if not sorted_results:
            return []

        # Hydrate model instances
        missing = [(key, s) for key, s in sorted_results if key not in prefetched]
        if missing:
            pipe = POPOTO_REDIS_DB.pipeline()
            for key, _score in missing:
                pipe.hgetall(key)
            for (key, _score), data in zip(missing, pipe.execute()):
                if data:
                    prefetched[key] = decode_popoto_model_hashmap(
                        model_class, data, source_redis_key=key
                    )

        instances = []
        for key, score in sorted_results:
            hydrated = prefetched.get(key)
            if hydrated is not None:
                hydrated._rrf_score = score
                instances.append(hydrated)

        if not self._no_track:
            _fire_on_read(model_class, instances)

        return instances

    def _get_vector_scores(self, query_text: str, limit: int = 10) -> list:
        """Return (redis_key, cosine_similarity) tuples for hybrid RRF fusion.

        Mirrors the internals of semantic_search() but returns raw scored
        pairs instead of hydrated model instances. Used by
        ContextAssembler._pull_path_hybrid() to supply the vector signal to
        fuse().

        Returns:
            list[(redis_key, float)] sorted by similarity score descending.
            Empty list if:
            - ``query_text`` is empty or whitespace
            - no EmbeddingField found on the model
            - embedding provider is not configured
            - no stored embeddings exist for this model class
            - embedding call raises an exception (logs warning)
        """
        if not query_text or not query_text.strip():
            return []

        model_class = self._query.model_class

        from ..fields.embedding_field import EmbeddingField

        embedding_field = None
        for fname, field in model_class._meta.fields.items():
            if isinstance(field, EmbeddingField):
                embedding_field = field
                break

        if embedding_field is None:
            return []

        provider = embedding_field.provider
        if provider is None:
            return []

        try:
            query_vectors = provider.embed([query_text], input_type="query")
            if not query_vectors or not query_vectors[0]:
                return []
        except Exception as e:
            logger.warning("_get_vector_scores embedding failed: %s", e)
            return []

        try:
            import numpy as np
        except ImportError:
            logger.warning(
                "_get_vector_scores: numpy not available; skipping vector signal"
            )
            return []

        matrix, keys = EmbeddingField.load_embeddings(model_class)
        if matrix is None or len(keys) == 0:
            return []

        query_vec = np.array(query_vectors[0], dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []
        query_vec = query_vec / query_norm

        similarities = matrix @ query_vec

        # Build sorted (redis_key, score) pairs, positive similarities only
        scored_pairs = [
            (keys[i], float(similarities[i]))
            for i in range(len(keys))
            if similarities[i] > 0
        ]
        scored_pairs.sort(key=lambda x: x[1], reverse=True)
        return scored_pairs[:limit]

    def _resolve_index(self, model_class, field_name, weight, uid, temp_keys):
        """Resolve a field name to a Redis sorted set key for composite scoring.

        Handles different field types by either returning existing ZSET keys
        directly or materializing data into temporary ZSETs.

        Args:
            model_class: The Model class.
            field_name: Name of the field or special index.
            weight: The weight for this index (used for naming temp keys).
            uid: Unique identifier for temp key namespacing.
            temp_keys: List to append any created temp keys to.

        Returns:
            str: Redis key of the sorted set to use, or None if unresolvable.

        Raises:
            QueryException: If field_name is invalid or unsupported.
        """
        from ..fields.access_tracker import AccessTrackerMixin
        from ..fields.confidence_field import ConfidenceField
        from ..fields.decaying_sorted_field import DecayingSortedField
        from ..fields.sorted_field_mixin import SortedFieldMixin
        from ..fields.write_filter import WriteFilterMixin

        model_name = model_class.__name__

        # --- Special case: "priority" for WriteFilter ---
        if field_name == "priority":
            if not issubclass(model_class, WriteFilterMixin):
                raise QueryException(
                    f"'{model_name}' does not use WriteFilterMixin; "
                    f"cannot resolve 'priority' index"
                )
            return f"$WF:{model_name}:priority"

        # --- Special case: "access_count" / "access_score" for AccessTracker ---
        if field_name in ("access_count", "access_score"):
            if not issubclass(model_class, AccessTrackerMixin):
                raise QueryException(
                    f"'{model_name}' does not use AccessTrackerMixin; "
                    f"cannot resolve '{field_name}' index"
                )
            return self._materialize_access_tracker(model_class, uid, temp_keys)

        # --- Look up the field on the model ---
        if field_name not in model_class._meta.fields:
            raise QueryException(
                f"'{model_name}' has no field '{field_name}'. "
                f"Valid fields: {list(model_class._meta.fields.keys())}"
            )

        field = model_class._meta.fields[field_name]

        # --- DecayingSortedField: materialize decay scores ---
        if isinstance(field, DecayingSortedField):
            return self._materialize_decay_field(
                model_class, field, field_name, uid, temp_keys
            )

        # --- ConfidenceField: materialize from companion hash ---
        if isinstance(field, ConfidenceField):
            return self._materialize_confidence_field(
                model_class, field, field_name, uid, temp_keys
            )

        # --- SortedFieldMixin: use existing sorted set directly ---
        if isinstance(field, SortedFieldMixin):
            try:
                partition_values = [
                    canonical_key_str(self._filters[pf]) for pf in field.partition_by
                ]
            except KeyError:
                missing = [pf for pf in field.partition_by if pf not in self._filters]
                raise QueryException(
                    f"composite_score() on '{field_name}' requires "
                    f"partition filter(s): {', '.join(missing)}"
                )
            sortedset_db_key = field.__class__.get_sortedset_db_key(
                model_class, field_name, *partition_values
            )
            return sortedset_db_key.redis_key

        # --- Unsupported field type ---
        raise QueryException(
            f"Field '{field_name}' ({type(field).__name__}) does not have "
            f"a sorted set index and cannot be used in composite_score()"
        )

    def _materialize_decay_field(self, model_class, field, field_name, uid, temp_keys):
        """Materialize a DecayingSortedField's decay-computed scores into a temp ZSET.

        Uses the existing Lua decay script to compute scores, then writes
        them to a temporary sorted set.

        Args:
            model_class: The Model class.
            field: The DecayingSortedField instance.
            field_name: Name of the field.
            uid: Unique identifier for temp key.
            temp_keys: List to append temp key to.

        Returns:
            str: Redis key of the temp sorted set.
        """
        import time

        from ..fields.decaying_sorted_field import DECAY_SCORE_LUA
        from ..fields.cyclic_decay_field import CyclicDecayField, CYCLIC_DECAY_LUA

        model_name = model_class.__name__

        try:
            partition_values = [
                canonical_key_str(self._filters[pf]) for pf in field.partition_by
            ]
        except KeyError:
            missing = [pf for pf in field.partition_by if pf not in self._filters]
            raise QueryException(
                f"composite_score() on '{field_name}' requires "
                f"partition filter(s): {', '.join(missing)}"
            )

        sortedset_db_key = field.__class__.get_sortedset_db_key(
            model_class, field_name, *partition_values
        )

        now = time.time()
        base_score_field = field.base_score_field or ""

        # Confidence-modulated decay (#491) — same resolution as top_by_decay,
        # so composite_score and top_by_decay never disagree on a score.
        from ..fields.decaying_sorted_field import (
            confidence_modulation_args,
            validity_gate_args,
        )

        conf_hash_key, conf_s, conf_c0 = confidence_modulation_args(
            model_class, field, field_name, filters=self._filters
        )

        # Validity gating (#580, plan D5). Here the gate is a *pre-trim*, not the
        # membership guarantee: the decay arm's contribution is what gets
        # dropped, and ZUNIONSTORE/SUM would still surface a closed member that
        # another arm scores. `_apply_validity_mask` (plan D5b) is what actually
        # excludes it after the union. Both layers must read the SAME as-of --
        # composite_score parks it on self._validity_as_of for the duration of
        # the call rather than threading it through _resolve_index's signature.
        gate_invalid_key, gate_valid_key, gate_as_of = validity_gate_args(
            model_class, as_of=self._validity_as_of
        )

        # Get all decay scores via Lua
        if isinstance(field, CyclicDecayField):
            cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
                model_class, field_name, *partition_values
            )
            pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
                model_class, field_name, *partition_values
            )
            result = run_lua(
                POPOTO_REDIS_DB,
                CYCLIC_DECAY_LUA,
                # numkeys: zset + cycles + pressure + confidence (KEYS[4]).
                4,
                sortedset_db_key.redis_key,
                cycles_hash_key,
                pressure_hash_key,
                conf_hash_key,
                str(now),
                str(field.decay_rate),
                str(999999),  # get all members
                base_score_field,
                conf_s,
                conf_c0,
            )
        else:
            result = run_lua(
                POPOTO_REDIS_DB,
                DECAY_SCORE_LUA,
                # numkeys: zset + confidence (KEYS[2]) + invalid_at (KEYS[3]) +
                # valid_from (KEYS[4]). See the Risk 1 note in top_by_decay.
                4,
                sortedset_db_key.redis_key,
                conf_hash_key,
                gate_invalid_key,
                gate_valid_key,
                str(now),
                str(field.decay_rate),
                str(999999),  # get all members
                base_score_field,
                conf_s,
                conf_c0,
                gate_as_of,  # ARGV[7]
            )

        temp_key = f"$CSQ:{model_name}:decay:{field_name}:{uid}"
        temp_keys.append(temp_key)

        if result:
            # Parse [key1, score1, key2, score2, ...] and write to temp ZSET
            zadd_mapping = {}
            for i in range(0, len(result), 2):
                member = result[i]
                if isinstance(member, bytes):
                    member = member.decode()
                score = float(result[i + 1])
                zadd_mapping[member] = score

            if zadd_mapping:
                POPOTO_REDIS_DB.zadd(temp_key, zadd_mapping)
                POPOTO_REDIS_DB.expire(temp_key, 5)

        return temp_key

    def _materialize_confidence_field(
        self, model_class, field, field_name, uid, temp_keys
    ):
        """Materialize a ConfidenceField's confidence values into a temp ZSET.

        Reads the companion hash and extracts confidence values for each member.
        When the field has partition_by and the query includes partition field
        values, reads only the partition-scoped hash. When partition fields are
        missing from a partitioned query, raises QueryException.

        Args:
            model_class: The Model class.
            field: The ConfidenceField instance.
            field_name: Name of the field.
            uid: Unique identifier for temp key.
            temp_keys: List to append temp key to.

        Returns:
            str: Redis key of the temp sorted set.

        Raises:
            QueryException: If a partitioned field is queried without providing
                values for all partition fields.
        """
        import msgpack

        model_name = model_class.__name__

        # Build companion hash key — partition-aware
        if field.partition_by:
            # Extract partition values from query filters
            partition_values = {}
            for pf in field.partition_by:
                if pf not in self._filters:
                    missing = [p for p in field.partition_by if p not in self._filters]
                    raise QueryException(
                        f"ConfidenceField '{field_name}' is partitioned by "
                        f"{', '.join(field.partition_by)}. "
                        f"Query must include filter(s) for: {', '.join(missing)}"
                    )
                partition_values[pf] = self._filters[pf]
            data_hash_key = field.get_data_hash_key_from_values(
                model_class, field_name, **partition_values
            )
        else:
            # Unpartitioned: single global hash
            data_hash_key = field.get_data_hash_key_from_values(model_class, field_name)

        # Read all entries from companion hash
        all_data = POPOTO_REDIS_DB.hgetall(data_hash_key)

        temp_key = f"$CSQ:{model_name}:confidence:{field_name}:{uid}"
        temp_keys.append(temp_key)

        if all_data:
            zadd_mapping = {}
            for member_key, raw_value in all_data.items():
                if isinstance(member_key, bytes):
                    member_key = member_key.decode()
                try:
                    data = msgpack.unpackb(raw_value, raw=False)
                    confidence = data.get("confidence", field.initial_confidence)
                except Exception:
                    logger.warning(
                        "Failed to unpack confidence data for %s", member_key
                    )
                    confidence = field.initial_confidence
                zadd_mapping[member_key] = float(confidence)

            if zadd_mapping:
                POPOTO_REDIS_DB.zadd(temp_key, zadd_mapping)
                POPOTO_REDIS_DB.expire(temp_key, 5)

        return temp_key

    def _materialize_access_tracker(self, model_class, uid, temp_keys):
        """Materialize AccessTracker access_count values into a temp ZSET.

        Iterates over all model instances and reads access_count from each
        instance's meta hash.

        .. note::
            This method uses ``SMEMBERS`` on the class set to discover all
            instances.  For models with very large instance counts (100K+),
            this scan can be expensive.  Consider using ``post_filter`` or
            partitioned queries to narrow the result set when working at
            that scale.

        Args:
            model_class: The Model class.
            uid: Unique identifier for temp key.
            temp_keys: List to append temp key to.

        Returns:
            str: Redis key of the temp sorted set.
        """
        model_name = model_class.__name__
        temp_key = f"$CSQ:{model_name}:access:{uid}"
        temp_keys.append(temp_key)

        # Get all instance keys
        all_keys = POPOTO_REDIS_DB.smembers(
            model_class._meta.db_class_set_key.redis_key
        )

        if all_keys:
            zadd_mapping = {}
            pipe = POPOTO_REDIS_DB.pipeline()
            decoded_keys = []
            for key in all_keys:
                if isinstance(key, bytes):
                    key = key.decode()
                decoded_keys.append(key)
                meta_key = f"$AT:{model_name}:meta:{key}"
                pipe.hget(meta_key, "access_count")

            results = pipe.execute()

            for key, count_raw in zip(decoded_keys, results):
                count = int(count_raw) if count_raw else 0
                if count > 0:
                    zadd_mapping[key] = float(count)

            if zadd_mapping:
                POPOTO_REDIS_DB.zadd(temp_key, zadd_mapping)
                POPOTO_REDIS_DB.expire(temp_key, 5)

        return temp_key

    def _apply_validity_mask(
        self,
        composite_key: str,
        model_name: str,
        uid: str,
        temp_keys: list[str],
        as_of: Optional[float] = None,
    ) -> None:
        """Remove *demonstrably invalid* members from a composite result (D5b).

        This is layer 2 of validity gating, and the only layer that enforces
        *membership* on the ``composite_score`` path. The decay-Lua gate (layer
        1) merely leaves a closed member out of the decay arm; ``ZUNIONSTORE
        ... AGGREGATE SUM`` then reads that absence as a 0 contribution, not as
        an exclusion, so the member still surfaces on the strength of any other
        weighted arm (a ``ConfidenceField`` index, ``co_occurrence_boost``,
        ``similarity_boost``). Subtracting an exclusion set removes it.

        THIS IS A SET DIFFERENCE, NOT AN INTERSECTION -- do not "simplify" it
        back to a ``ZINTERSTORE`` against a valid-member mask. The rule is
        *exclusion*, matching ``DECAY_SCORE_LUA``'s gate exactly: a member is
        dropped only when it is provably closed or provably not-yet-started. A
        record with **no entry in either interval ZSET** is an *unmanaged*
        record and stays fully retrievable. An intersect-with-whitelist mask
        would instead delete every such record -- i.e. every row that predates
        the day a ``ValidityField`` was added to an existing model, none of
        which has an interval until it is next saved. That is a silent
        data-visibility regression, not a stricter gate.

        Four core commands, no Lua and no Redis modules, so this is Valkey-safe:

        - ``ZRANGESTORE m_closed invalid_at -inf t  BYSCORE`` -> closed at ``t``
        - ``ZRANGESTORE m_future valid_from (t  +inf BYSCORE`` -> not yet started
        - ``ZUNIONSTORE m_excl 2 m_closed m_future WEIGHTS 0 0`` -> the excluded
        - ``ZDIFFSTORE composite_key 2 composite_key m_excl``

        ``ZDIFFSTORE`` keeps the left-hand scores, so composite scores pass
        through unchanged and only membership is affected. Both it and
        ``ZRANGESTORE`` require Redis >= 6.2 / Valkey — the same floor
        ``bm25_field.py`` already asserts for ``ZMSCORE``, so this adds no new
        compatibility surface.

        No-op (leaving the composite byte-identical to pre-#580) when the kill
        switch is off or the model declares no ``ValidityField``.

        Args:
            composite_key: The post-``ZUNIONSTORE`` key, filtered in place.
            model_name: Model class name, for the ``$CSQ:`` temp-key namespace.
            uid: The per-query unique suffix shared by every temp key.
            temp_keys: The caller's cleanup list; all three temps are appended.
            as_of: Epoch seconds to evaluate membership at. ``None`` = now.
        """
        from ..fields.decaying_sorted_field import validity_gate_args

        model_class = self._query.model_class
        invalid_at_key, valid_from_key, as_of_repr = validity_gate_args(
            model_class, as_of=as_of
        )
        if not invalid_at_key or not valid_from_key:
            return

        t = float(as_of_repr)
        closed_key = f"$CSQ:{model_name}:validclosed:{uid}"
        future_key = f"$CSQ:{model_name}:validfuture:{uid}"
        excluded_key = f"$CSQ:{model_name}:validexcl:{uid}"
        temp_keys.extend([closed_key, future_key, excluded_key])

        # invalid_at <= t -- already closed at t. Inclusive upper bound: a record
        # closed exactly at t is closed at t. The +inf open sentinel can never
        # fall in this range, so open records are never collected here.
        #
        # The per-argument ignores below are load-bearing: redis-py annotates
        # zrangestore's start/end as `int`, which is only correct for the
        # default index-range form. Under BYSCORE they are score bounds and must
        # be str/float -- "(", "-inf" and "+inf" have no int spelling -- so the
        # upstream annotation is simply too narrow.
        POPOTO_REDIS_DB.zrangestore(
            closed_key,
            invalid_at_key,
            "-inf",  # type: ignore[arg-type]
            t,  # type: ignore[arg-type]
            byscore=True,
        )
        # valid_from > t -- not yet started at t (exclusive lower bound).
        POPOTO_REDIS_DB.zrangestore(
            future_key,
            valid_from_key,
            f"({t}",  # type: ignore[arg-type]
            "+inf",  # type: ignore[arg-type]
            byscore=True,
        )
        POPOTO_REDIS_DB.expire(closed_key, 5)
        POPOTO_REDIS_DB.expire(future_key, 5)

        # Union, not intersect: a member is excluded if it fails EITHER end of
        # the interval test. Weights are 0 only for tidiness -- ZDIFFSTORE
        # ignores the right-hand scores entirely.
        POPOTO_REDIS_DB.zunionstore(excluded_key, {closed_key: 0, future_key: 0})
        POPOTO_REDIS_DB.expire(excluded_key, 5)

        # Set difference. Members with no interval entry appear in neither
        # exclusion set, so they survive -- see the docstring for why that is
        # the required behavior and not an oversight.
        POPOTO_REDIS_DB.zdiffstore(composite_key, [composite_key, excluded_key])
        POPOTO_REDIS_DB.expire(composite_key, 5)

    @staticmethod
    def _cleanup_temp_keys(temp_keys):
        """Delete temporary Redis keys created during composite scoring.

        Args:
            temp_keys: List of Redis key strings to delete.
        """
        if temp_keys:
            POPOTO_REDIS_DB.delete(*temp_keys)

    def all(self) -> list:
        """Execute the query and return all matching results.

        Combines all accumulated filters, ordering, and limits into a single
        query execution. When computed_sort() is set, it takes precedence over
        order_by() and applies after fetch but before limit.

        Returns:
            List of Model instances, or list of dicts if values() was called.
        """
        return self._execute(no_track=self._no_track)

    def _execute(
        self, *, apply_limit: bool = True, no_track: bool = False
    ) -> "list[Any]":
        """Execute the accumulated filters, ordering, and limit.

        This is the shared execution seam behind `all()` and the `Q`-object
        branch of `count()`. `all()` calls it with the builder's limit applied
        (`apply_limit=True`, the default) so it returns a bounded page.
        `count()` calls it with `apply_limit=False` so it returns the full
        matching population regardless of any `limit()` on the builder — a
        limit bounds the rows a caller receives, it must never bound a tally.

        Args:
            apply_limit: When True (default), bound the results to
                `self._limit_value` as `all()` does. When False (used by
                `count()`), no limit is applied and the full match set is
                returned/hydrated.
            no_track: Forwarded to `Query._execute_filter` as `_no_track`.
                `all()` passes the builder's own `self._no_track` here, so
                read tracking behaves exactly as before for every ORM read.
                `count()` passes `no_track=True` unconditionally: a tally is
                not a read and must record no accesses. Without this, an
                unbounded `Q` + `limit` count would fire a population-scale
                `RPUSH`+`EXPIRE` write per instance via `_fire_on_read`
                instead of the bounded page's worth.

        Returns:
            List of Model instances, or list of dicts if values() was called.
        """
        kwargs = self._filters.copy()

        if self._computed_sort_fn is not None:
            # When computed_sort is active:
            # 1. Remove order_by (computed_sort takes precedence)
            # 2. Remove limit (we need all results to sort, then slice)
            if self._order_by_value is not None:
                logger.warning(
                    "Both computed_sort() and order_by() are set; "
                    "computed_sort() takes precedence, order_by() is ignored."
                )
            if self._values_tuple is not None:
                kwargs["values"] = self._values_tuple
            results = self._query._execute_filter(
                q_objects=self._q_objects, _no_track=no_track, **kwargs
            )
            # Apply computed sort (O(N log N) on full result set)
            results = sorted(
                results,
                key=self._computed_sort_fn,
                reverse=self._computed_sort_reverse,
            )
            # Apply limit after sorting
            if apply_limit and self._limit_value is not None:
                results = results[: self._limit_value]
            return results

        # Standard path: no computed_sort
        if apply_limit and self._limit_value is not None:
            kwargs["limit"] = self._limit_value
        if self._order_by_value is not None:
            kwargs["order_by"] = self._order_by_value
        if self._values_tuple is not None:
            kwargs["values"] = self._values_tuple
        return self._query._execute_filter(
            q_objects=self._q_objects, _no_track=no_track, **kwargs
        )

    def count(self) -> int:
        """Count matching results without loading objects.

        Invariant: a `limit()` on the builder bounds the rows returned by
        `all()`; it never bounds the tally returned by `count()`. This holds
        on both branches below. The plain-field branch delegates to
        `Query.count(**self._filters)`, which never receives `limit` at all,
        so it complies implicitly. The `Q`-object branch must comply
        explicitly: it calls the shared `_execute()` seam with
        `apply_limit=False` so the full match set is counted, and with
        `no_track=True` because a tally is not a read and must not trigger
        read-tracking writes for the (potentially whole-population) rows it
        hydrates to count them.

        Returns:
            Integer count of matching instances
        """
        # For Q objects, we need to execute the full query and count
        if self._q_objects:
            return len(self._execute(apply_limit=False, no_track=True))
        return self._query.count(**self._filters)

    def first(self) -> "Model":
        """Return the first matching result, or None if no matches.

        Returns:
            First Model instance or None
        """
        results = self.limit(1).all()
        return results[0] if results else None

    def last(self) -> "Model":
        """Return the last matching result, or None if no matches.

        Note: For efficient access to the last item in sorted order, use
        order_by("-field").first() instead. This method fetches all results.

        Returns:
            Last Model instance or None
        """
        results = self.all()
        return results[-1] if results else None

    # List-like behavior for backward compatibility
    def __iter__(self):
        """Iterate over query results (executes query)."""
        return iter(self.all())

    def __len__(self):
        """Return the number of results (executes query)."""
        return len(self.all())

    def __getitem__(self, index):
        """Access results by index (executes query)."""
        return self.all()[index]

    def __bool__(self):
        """Check if any results exist (executes query)."""
        return len(self.all()) > 0

    def __eq__(self, other):
        """Compare with another object (executes query for comparison).

        Supports comparison with lists and other QueryBuilders for backward
        compatibility with code like: `assert Model.query.filter(...) == []`
        """
        if isinstance(other, list):
            return self.all() == other
        if isinstance(other, QueryBuilder):
            return self.all() == other.all()
        return NotImplemented

    def __contains__(self, item):
        """Check if item is in query results (executes query)."""
        return item in self.all()

    def __repr__(self):
        return f"<QueryBuilder for {self._query.model_class.__name__} filters={self._filters}>"


class Query:
    """Query interface for a Popoto Model.

    Accessed via ``Model.query``. Provides ``get``, ``filter``, ``all``,
    ``count``, and ``keys`` methods, plus async variants of each.

    Every Model class automatically receives a Query instance as both `Model.query`
    and `Model.objects` (for Django compatibility). This class coordinates filtering,
    retrieval, and result preparation across different field types.

    Architecture:
    ------------
    Query acts as an orchestrator rather than implementing filter logic directly.
    Each field type knows how to filter itself via its `filter_query()` class method.
    Query's job is to:

    1. Route filter parameters to the appropriate fields
    2. Combine results from multiple field filters via set intersection
    3. Batch-load matching objects using Redis pipelines
    4. Apply post-query sorting and limiting

    This delegation pattern allows new field types to add query capabilities
    without modifying the Query class.

    Chainable Query API:
    -------------------
    In addition to the original kwargs-based API, Query now supports a fluent
    chainable interface via QueryBuilder:

        # Original API (still fully supported)
        results = Model.query.filter(status="active", limit=10, order_by="name")

        # Chainable API
        results = Model.query.filter(status="active").order_by("name").limit(10).all()

        # Chain multiple filters
        results = Model.query.filter(status="active").filter(type="premium").all()

    The filter() method returns a QueryBuilder when no limit/order_by/values kwargs
    are provided, enabling chaining. The QueryBuilder is also iterable for backward
    compatibility with code that iterates over filter() results directly.

    Attributes:
        model_class: The Model subclass this Query operates on
        options: The ModelOptions metadata for the model (fields, key names, etc.)

    Example:
        class Product(Model):
            sku = KeyField(type=str)
            price = SortedField(type=float)
            category = KeyField(type=str)

        # Query is automatically available
        cheap_electronics = Product.query.filter(
            category="electronics",
            price__lte=50.0,
            order_by="price",
            limit=10
        )
    """

    model_class: "Model"
    options: "ModelOptions"

    # Sorted-range bound bookkeeping, reset per query by filter_for_keys_set.
    _pushdown_limit: Optional[int]
    _pushdown_requested: int
    _pushdown_fetched: int
    _pushdown_partition: "dict[str, Any]"

    def __init__(self, model_class: "Model"):
        """
        Initialize a Query instance bound to a specific Model class.

        This is called automatically by the ModelBase metaclass when a Model
        subclass is defined. Users should not need to instantiate Query directly.

        Args:
            model_class: The Model subclass this Query will operate on
        """
        self.model_class = model_class
        self.options = model_class._meta
        self._geo_distances = {}  # {redis_key: distance}
        self._geo_distance_unit = None  # unit for distance values

    def get(
        self, db_key: DB_key = None, redis_key: str = None, **kwargs
    ) -> Optional["Model"]:
        """Retrieve a single model instance.

        Look up by *db_key*, *redis_key*, or keyword field values. Raises
        :class:`QueryException` if more than one match is found. Returns
        ``None`` when no match exists.

        This method provides multiple retrieval strategies, optimized for different
        use cases:

        1. **Direct key lookup** (fastest): If all KeyField values are provided,
           the Redis key can be computed directly without any search.

        2. **Raw Redis key**: If you already have the Redis key string (e.g., from
           a previous query or external source), pass it directly.

        3. **Filter fallback**: If non-key fields are provided, falls back to
           `filter()` but raises an exception if multiple objects match.

        Args:
            db_key: A DB_key instance pointing to the object
            redis_key: The raw Redis key string (e.g., "User:alice:123")
            **kwargs: Field values to identify the object. If all KeyFields are
                      provided, enables direct lookup.

        Returns:
            The matching Model instance, or None if not found.

        Raises:
            QueryException: If the filter matches more than one object. This
                           indicates get() was used incorrectly; use filter() instead.

        Example:
            # Direct lookup when all keys are known (single Redis command)
            user = User.query.get(username="alice", tenant_id="acme")

            # Positional redis_key string (e.g. from a previous query or external source)
            user = User.query.get("User:alice:acme")

            # Fallback to filter when using non-key fields
            user = User.query.get(email="alice@example.com")  # May be slower
        """
        if isinstance(db_key, str) and not redis_key:
            redis_key = db_key
            db_key = None

        if (
            not db_key
            and not redis_key
            and all([key in kwargs for key in self.options.key_field_names])
        ):
            db_key = self.model_class(**kwargs).db_key

        if db_key and not redis_key:
            redis_key = db_key.redis_key

        if redis_key:
            from ..models.encoding import decode_popoto_model_hashmap

            hashmap = POPOTO_REDIS_DB.hgetall(redis_key)
            if not hashmap:
                return None
            instance = decode_popoto_model_hashmap(
                self.model_class, hashmap, source_redis_key=redis_key
            )
            _fire_on_read(self.model_class, [instance])

        else:
            # Materialize once: a QueryBuilder re-executes on every len()
            # and index, so the previous three-step check ran the query
            # three times. Two rows are enough to prove non-uniqueness.
            # Safe with plain-field (client-side) filters too: _execute_filter
            # suppresses the pre-hydration truncation whenever
            # _pending_client_filters is non-empty, so this limit is applied
            # only after those filters have run.
            kwargs.setdefault("limit", 2)
            instances = list(self.filter(**kwargs))
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        # or not hasattr(instance, 'db_key')
        return instance or None

    def get_many(self, redis_keys: list, skip_none: bool = False) -> list:
        """Retrieve multiple model instances by their Redis keys in a single pipeline.

        Uses a Redis pipeline to batch HGETALL calls, reducing N sequential
        round-trips to a single pipelined round-trip. Input order is preserved:
        each position in the returned list corresponds to the same position in
        ``redis_keys``.

        Unlike the internal ``get_many_objects()`` static method (which takes a
        set of bytes keys and silently drops missing entries), this public method
        takes a list of string keys, preserves order, and returns ``None`` for
        missing keys.

        Args:
            redis_keys: List of Redis key strings to look up (e.g.
                ``["User:alice:acme", "User:bob:acme"]``).
            skip_none: If True, filter out ``None`` entries from the result so
                that only successfully hydrated instances are returned. When
                False (default), the returned list has the same length as
                ``redis_keys`` with ``None`` at positions where the key was
                missing.

        Returns:
            List of Model instances (and/or ``None`` when *skip_none* is False).

        Example:
            # Bulk hydration after a set-based query
            keys = ["Product:widget:001", "Product:widget:002", "Product:widget:003"]
            products = Product.query.get_many(redis_keys=keys)
            # [<Product>, None, <Product>]  -- second key was missing

            # Skip missing entries
            products = Product.query.get_many(redis_keys=keys, skip_none=True)
            # [<Product>, <Product>]
        """
        if not redis_keys:
            return []

        from ..models.encoding import decode_popoto_model_hashmap

        pipeline = POPOTO_REDIS_DB.pipeline()
        for key in redis_keys:
            pipeline.hgetall(key)
        hashes_list = pipeline.execute()

        results = []
        live_instances = []
        for key, hashmap in zip(redis_keys, hashes_list):
            if hashmap:
                instance = decode_popoto_model_hashmap(
                    self.model_class, hashmap, source_redis_key=key
                )
                results.append(instance)
                live_instances.append(instance)
            else:
                results.append(None)

        if live_instances:
            _fire_on_read(self.model_class, live_instances)

        if skip_none:
            return [r for r in results if r is not None]
        return results

    def keys(self, catchall=False, clean=False, **kwargs) -> list:
        """Return a list of Redis key bytes for all instances of this model.

        By default, returns keys from the Model's class set (a Redis SET that
        tracks all instances). This is O(N) where N is the number of instances.

        Args:
            catchall: Debug flag. If True, uses Redis KEYS command with wildcard
                     pattern matching. This scans ALL keys in Redis and should
                     NEVER be used in production. Useful for finding orphaned keys
                     that aren't in the class set.
            clean: Debug flag. If True, removes dangling references from index sets.
                  This repairs inconsistencies where the class set or field indexes
                  reference objects that no longer exist. Run this if you see
                  "missing objects" errors in query results.
            **kwargs: Reserved for future filtering capabilities.

        Returns:
            List of Redis key strings (bytes) for all Model instances.

        Warning:
            Both `catchall` and `clean` use Redis KEYS command, which blocks the
            server and scans all keys. Never use these in production environments.

        Example:
            # Normal usage - get all keys from class set
            all_keys = Product.query.keys()

            # Debug - find any keys matching the model name
            orphaned = Product.query.keys(catchall=True)

            # Repair - clean up dangling references
            Product.query.keys(clean=True)
        """
        if clean:
            logger.warning(
                "Query.keys(clean=True) is deprecated. Use Model.clean_indexes() for production-safe orphan cleanup."
            )
            pipeline = POPOTO_REDIS_DB.pipeline()
            from ..fields.key_field_mixin import KeyFieldMixin
            from ..fields.relationship import Relationship

            for db_key in list(
                POPOTO_REDIS_DB.smembers(
                    self.model_class._meta.db_class_set_key.redis_key
                )
            ):
                hash = POPOTO_REDIS_DB.hgetall(db_key)
                if not len(hash):
                    pipeline = pipeline.srem(
                        self.model_class._meta.db_class_set_key.redis_key, db_key
                    )

            # find
            for field_name, field in self.model_class._meta.fields.items():  # 3
                if not isinstance(field, (KeyFieldMixin, Relationship)):
                    continue
                field_key_prefix = field.get_special_use_field_db_key(
                    self.model_class, field_name
                )
                for field_key in POPOTO_REDIS_DB.keys(f"{field_key_prefix}:*"):
                    for object_key in POPOTO_REDIS_DB.smembers(field_key):
                        hash = POPOTO_REDIS_DB.hgetall(object_key)
                        if not len(hash):
                            pipeline = pipeline.srem(field_key, object_key)

            pipeline.execute()

        if catchall:
            logger.warning(
                "{catchall} is for debugging purposes only. Not for use in production environment"
            )
            return list(POPOTO_REDIS_DB.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(
                POPOTO_REDIS_DB.smembers(
                    self.model_class._meta.db_class_set_key.redis_key
                )
            )

    def all(self, **kwargs) -> list:
        """Return all instances, with optional ``order_by``, ``limit``, and ``values``.

        Non-tracking by design: Query.all() does not fire _fire_on_read(). This asymmetry
        vs QueryBuilder.filter().all() is intentional — see QueryBuilder.no_track() for the
        explicit opt-out.

        Fetches every object tracked in the Model's class set. For large datasets,
        consider using `filter()` with appropriate constraints or `count()` to
        first assess the result size.

        Args:
            **kwargs: Supports the same result modifiers as `filter()`:
                - values: tuple of field names to return as dicts instead of objects
                - order_by: field name to sort by (prefix with "-" for descending)
                - limit: maximum number of results to return

        Returns:
            List of Model instances, or list of dicts if `values` is specified.

        Example:
            # Get all users
            users = User.query.all()

            # Get all users, sorted by creation date, newest first
            users = User.query.all(order_by="-created_at", limit=100)

            # Get only names and emails (more efficient for large objects)
            user_data = User.query.all(values=("name", "email"))
        """
        redis_db_keys_list = self.keys()

        # Apply default order_by from Meta if not explicitly provided
        if "order_by" not in kwargs and self.model_class._meta.order_by:
            kwargs["order_by"] = self.model_class._meta.order_by

        return self.prepare_results(
            Query.get_many_objects(
                self.model_class,
                set(redis_db_keys_list),
                order_by_attr_name=kwargs.get("order_by", None),
                values=kwargs.get("values", None),
            ),
            **kwargs,
        )

    def _snapshot_pushdown_state(self) -> "_PushdownState":
        """Copy the bookkeeping ``filter_for_keys_set`` left on ``self``.

        Mutable members are copied, not aliased, so a later query on the same
        shared ``Query`` cannot mutate a snapshot already handed out.
        """
        return _PushdownState(
            sorted_field_order=getattr(self, "_sorted_field_order", None),
            sorted_field_name=getattr(self, "_sorted_field_name", None),
            pending_client_filters=dict(
                getattr(self, "_pending_client_filters", None) or {}
            ),
            pushdown_limit=getattr(self, "_pushdown_limit", None),
            pushdown_requested=getattr(self, "_pushdown_requested", 0),
            pushdown_fetched=getattr(self, "_pushdown_fetched", 0),
            pushdown_partition=dict(getattr(self, "_pushdown_partition", None) or {}),
        )

    def _filter_keys_with_pushdown(
        self, allow_pushdown: bool, kwargs: "dict[str, Any]"
    ) -> "tuple[Any, _PushdownState]":
        """Arm the pushdown, run the key query, and snapshot — no await inside.

        The async path calls this through a single ``to_thread`` hop so that the
        arm, the query, and the snapshot cannot be interleaved with another
        coroutine's use of the same shared ``Query`` instance. Callers must hold
        the per-loop pushdown lock: two ``to_thread`` calls run in different
        worker threads, and ``filter_for_keys_set`` releases the GIL on Redis
        I/O, so its reset can otherwise land between another call's populate and
        its snapshot.
        """
        self._pushdown_allowed = allow_pushdown
        try:
            db_keys = self.filter_for_keys_set(**kwargs)
        finally:
            self._pushdown_allowed = False
        return db_keys, self._snapshot_pushdown_state()

    def _bound_keys_before_hydration(
        self,
        db_keys: Any,
        q_objects: "Optional[list[Any]]",
        allow_pushdown: bool,
        kwargs: "dict[str, Any]",
        *,
        state: "Optional[_PushdownState]" = None,
    ) -> Any:
        """Slice the sorted key list to ``limit`` before anything is loaded.

        Returns ``db_keys`` untouched unless the query qualifies. The guard
        mirrors ``_sorted_pushdown_args``: no Q objects, no pending client-side
        filter, ordering supplied by the sorted field, and a positive int limit.

        Kept separate from the range-read bound because it applies in a strictly
        wider set of cases. A second indexed predicate blocks the Redis-side
        bound, since the sorted set alone cannot honor the other index, but not
        this one: the intersection is already reflected in _sorted_field_order.

        ``state`` is the async path's per-call snapshot: when supplied the guard
        reads and writes it instead of ``self``, which is what keeps concurrent
        ``async_filter`` calls from clobbering each other's bound. ``None`` keeps
        the sync path on shared instance state exactly as before.
        """
        if state is None:
            state = self._snapshot_pushdown_state()
            write_back = True
        else:
            write_back = False

        if q_objects or not allow_pushdown:
            return db_keys
        if state.pending_client_filters:
            return db_keys
        if state.pushdown_limit:
            return db_keys  # the range read was already bounded, and in order
        ordered = state.sorted_field_order
        if not ordered:
            return db_keys

        limit = kwargs.get("limit", None)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return db_keys

        order_by = kwargs.get("order_by", None) or self.model_class._meta.order_by
        desc = False
        if order_by:
            if not isinstance(order_by, str):
                return db_keys
            desc = order_by.startswith("-")
            if (order_by[1:] if desc else order_by) != state.sorted_field_name:
                return db_keys

        # _sorted_field_order is ascending by score; a descending query wants
        # the tail, so reverse before slicing rather than after.
        fetch = limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN
        bounded = list(reversed(ordered))[:fetch] if desc else list(ordered)[:fetch]
        state.pushdown_limit = limit
        state.pushdown_requested = fetch
        state.pushdown_fetched = len(bounded)
        if write_back:
            self._pushdown_limit = limit
            self._pushdown_requested = fetch
            self._pushdown_fetched = len(bounded)
        return bounded

    def _short_result_action(
        self,
        n_objects: int,
        allow_pushdown: bool,
        state: "_PushdownState",
    ) -> bool:
        """Warn about a short bounded read; return True if it must be re-read.

        ``get_many_objects`` silently drops keys whose hash is gone, so a
        bounded read can spend its budget on members that hydrate to nothing.
        The over-fetch margin absorbs the ordinary case in the same round trip.
        Coming up short despite a full page is the signal that it did not; a
        partial page means the range was exhausted and the count is honest.
        A short bounded result is a wrong answer rather than a slow one, so
        neither branch below is allowed to pass silently.

        Decision and logging only: the caller owns the retry, because the sync
        and async paths recurse into different methods.
        """
        pushdown_limit: int = state.pushdown_limit or 0
        short = pushdown_limit > 0 and n_objects < pushdown_limit
        exhausted = state.pushdown_fetched < state.pushdown_requested
        orphans = state.pushdown_fetched - n_objects
        partition = (
            f", partition {state.pushdown_partition}"
            if state.pushdown_partition
            else ""
        )
        if allow_pushdown and short and not exhausted:
            logger.warning(
                f"{self.model_class.__name__}: bounded sorted read on "
                f"{state.sorted_field_name} returned {n_objects} of "
                f"{pushdown_limit} requested ({state.pushdown_fetched} index "
                f"members read, {orphans} hydrated to nothing{partition}). "
                f"Re-reading the full range so the answer is correct. Orphaned "
                f"index members are the cause, and re-reading only tolerates "
                f"them: clear them with "
                f"{self.model_class.__name__}.clean_indexes(), or inspect "
                f"with {self.model_class.__name__}.query.keys(clean=True)."
            )
            return True
        if short and orphans > 0:
            # The range ran out, so this is as complete as the index allows.
            # Still short, and still worth saying out loud.
            logger.warning(
                f"{self.model_class.__name__}: sorted read on "
                f"{state.sorted_field_name} returned {n_objects} rows of "
                f"{pushdown_limit} requested; {orphans} index members hydrated "
                f"to nothing{partition}. The range is exhausted, so the result "
                f"is short rather than wrong. Clear the orphans with "
                f"{self.model_class.__name__}.clean_indexes()."
            )
        return False

    def _sorted_pushdown_args(
        self,
        field_name: str,
        field: "SortedFieldMixin",
        unemployed_params: "set[str]",
        kwargs: "dict[str, Any]",
    ) -> "tuple[Optional[int], bool]":
        """Decide whether ``limit`` may be pushed into this field's range read.

        Returns ``(limit, desc)``, with ``limit`` None when the query does not
        qualify and the caller must read the full range as before.

        A bound applied inside the sorted-set read lands before hydration and
        before every later stage of the pipeline, so it is only sound when
        nothing downstream can drop a row. Each condition below removes one way
        that could happen:

        1. ``_pushdown_allowed`` — the caller is ``_execute_filter``'s plain
           path. Q objects union results from several ``filter_for_keys_set``
           calls, which is why they already null ``_sorted_field_order``.
        2. This field supplies the ordering (it is the first sorted field).
        3. ``limit`` is a positive int.
        4. ``order_by`` is absent or names this same field. Ordering by anything
           else means score order is not result order, so the top N by score is
           not the answer.
        5. No filter param survives this field and its partitions. A leftover
           param becomes either another index intersection or a client-side
           filter on an unindexed field, and both can eliminate rows after the
           bound has already been spent. This is the condition that would
           silently return short results if it were dropped.
        """
        if not getattr(self, "_pushdown_allowed", False):
            return None, False
        if self._sorted_field_order is not None:
            return None, False

        limit = kwargs.get("limit", None)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return None, False

        order_by = kwargs.get("order_by", None) or self.model_class._meta.order_by
        desc = False
        if order_by:
            if not isinstance(order_by, str):
                return None, False
            desc = order_by.startswith("-")
            if (order_by[1:] if desc else order_by) != field_name:
                return None, False

        remaining = (
            set(unemployed_params)
            - set(self.options.filter_query_params_by_field[field_name])
            - set(field.partition_by)
        )
        if remaining:
            return None, False

        return limit, desc

    def filter_for_keys_set(self, **kwargs) -> set:
        """
        Execute filter logic and return matching Redis keys (without loading objects).

        This is the core filtering engine. It routes filter parameters to the
        appropriate field types and combines their results via set intersection.
        Separated from `filter()` to support `count()` without the overhead of
        object instantiation.

        Processing Order:
        ----------------
        1. **Sorted fields first**: SortedFields use Redis sorted sets with range
           queries (ZRANGEBYSCORE), which are often more selective than key lookups.
           Additionally, sorted fields may have partition dependencies (`partition_by`)
           that consume other filter parameters.

        2. **Remaining fields**: KeyFields and other field types are processed
           after sorted fields, using whatever parameters remain unconsumed.

        3. **Set intersection**: Each field's filter returns a set of matching keys.
           The final result is the intersection of all sets, implementing AND logic.

        Args:
            **kwargs: Filter parameters. Each parameter is matched to a field that
                     supports it. Reserved params (limit, order_by, values) are
                     excluded from field matching.

        Returns:
            Set of Redis key strings (bytes) matching ALL filter criteria.
            Returns empty set if no filters provided or no matches found.

        Raises:
            QueryException: If any kwargs don't match a known filter parameter.
                          This prevents silent failures from typos.

        Note:
            This method does not apply ordering or limits - it only identifies
            matching keys. Use `filter()` for the complete query pipeline.
        """
        db_keys_sets = []
        self._sorted_field_order = None
        self._sorted_field_name = None
        self._pending_client_filters = {}
        self._pushdown_limit = None
        self._pushdown_requested = 0
        self._pushdown_fetched = 0
        self._pushdown_partition = {}
        yet_employed_kwargs_set = set(kwargs.keys()).difference(
            {"limit", "order_by", "values"}
        )
        if not len(yet_employed_kwargs_set):
            # No filter criteria - return all keys (same as all())
            return set(self.keys())

        # todo: use redis.SINTER for keyfield exact match filters

        # do sorted_fields first - because they can obviate some keyfield filters
        for field_name in self.options.sorted_field_names:
            field = self.options.fields[field_name]
            if not len(
                yet_employed_kwargs_set
                & self.options.filter_query_params_by_field[field_name]
            ):
                continue  # this field cannot use any of the available filter params
            logger.debug(
                f"query on {field_name} with {self.options.filter_query_params_by_field[field_name]}"
            )
            logger.debug(
                {
                    k: kwargs[k]
                    for k in self.options.filter_query_params_by_field[field_name]
                    if k in kwargs
                }
            )
            push_limit, push_desc = self._sorted_pushdown_args(
                field_name, field, yet_employed_kwargs_set, kwargs
            )
            # Ask for a margin beyond `limit`: orphaned index members hydrate to
            # nothing and would otherwise come off the result count, costing a
            # second round trip to discover. Only sorted-set fields accept the
            # bound; GeoField and friends share this loop and take plain query
            # params only.
            push_fetch = (
                push_limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN
                if push_limit
                else None
            )
            push_kwargs = (
                {"_limit": push_fetch, "_desc": push_desc} if push_limit else {}
            )
            result = field.__class__.filter_query(
                self.model_class, field_name, **push_kwargs, **kwargs
            )
            # Handle tuple return from GeoField with distances
            if isinstance(result, tuple) and len(result) == 3:
                keys_set, distances, unit = result
                self._geo_distances.update(distances)
                self._geo_distance_unit = unit
                db_keys_sets.append(keys_set)
            else:
                # result is now a list (preserving ZRANGEBYSCORE order)
                if self._sorted_field_order is None:
                    self._sorted_field_order = result
                    self._sorted_field_name = field_name
                if push_limit:
                    self._pushdown_limit = push_limit
                    self._pushdown_requested = int(push_fetch or 0)
                    self._pushdown_fetched = len(result)
                    self._pushdown_partition = {
                        name: kwargs.get(name) for name in field.partition_by
                    }
                db_keys_sets.append(set(result))  # convert to set for intersection
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                self.options.filter_query_params_by_field[field_name]
            ).difference(
                set(field.partition_by)
            )  # also remove the required partition_by field names

        for field_name in self.options.filter_query_params_by_field:
            if field_name in self.options.sorted_field_names:
                continue  # already handled
            params_for_field = yet_employed_kwargs_set & set(
                self.options.filter_query_params_by_field[field_name]
            )
            if not params_for_field:
                continue  # this field cannot use any of the available filter params

            field = self.options.fields[field_name]
            logger.debug(f"query on {field_name} with {params_for_field}")
            logger.debug({k: kwargs[k] for k in params_for_field})
            result = field.__class__.filter_query(
                self.model_class, field_name, **{k: kwargs[k] for k in params_for_field}
            )
            # Handle tuple return from GeoField with distances
            if isinstance(result, tuple) and len(result) == 3:
                keys_set, distances, unit = result
                self._geo_distances.update(distances)
                self._geo_distance_unit = unit
                db_keys_sets.append(keys_set)
            else:
                db_keys_sets.append(result)
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                params_for_field
            )

        # Separate plain field params (client-side filter) from truly unknown params
        if yet_employed_kwargs_set:
            plain_field_filters = {}
            unknown_params = set()
            for param in yet_employed_kwargs_set:
                if param in self.options.fields:
                    plain_field_filters[param] = kwargs[param]
                    logger.debug(
                        f"Client-side filter on unindexed field '{param}' "
                        f"— consider using SortedField for better performance"
                    )
                else:
                    unknown_params.add(param)
            if unknown_params:
                raise QueryException(
                    f"Invalid filter parameters: {','.join(unknown_params)}"
                )
            self._pending_client_filters = plain_field_filters

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            if self._pending_client_filters:
                # Only plain field filters — load all keys for client-side filtering
                return set(self.keys())
            return set()
        # return intersection of all the db keys sets, effectively &&-ing all filters
        intersection = set.intersection(*db_keys_sets)
        if self._sorted_field_order is not None:
            matched_keys = intersection
            self._sorted_field_order = [
                k for k in self._sorted_field_order if k in matched_keys
            ]
        return intersection

    def _evaluate_filter_args(self, q_objects: list, kwargs: dict) -> set:
        """Evaluate filter arguments including Q objects and return matching keys.

        This method handles both traditional kwargs filtering and Q object
        expressions. When Q objects are present, they are combined with any
        kwargs using AND logic.

        Args:
            q_objects: List of Q objects for complex query expressions
            kwargs: Dict of filter parameters and result modifiers

        Returns:
            Set of Redis keys matching all filter criteria.

        Processing Logic:
        ----------------
        1. If no Q objects, delegate to filter_for_keys_set()
        2. If Q objects present:
           a. Evaluate each Q object to get its result set
           b. If kwargs filters exist, evaluate them too
           c. Intersect all result sets (AND logic between args)
        """
        from .q import evaluate_q

        # Extract result modifiers from kwargs
        filter_kwargs = {
            k: v for k, v in kwargs.items() if k not in {"limit", "order_by", "values"}
        }

        if not q_objects:
            # No Q objects - use traditional filtering
            return self.filter_for_keys_set(**kwargs)

        # Evaluate Q objects
        result_sets = []
        all_keys = None  # Lazy-loaded for negation operations

        for q_obj in q_objects:
            result_sets.append(evaluate_q(self, q_obj, all_keys))

        # If there are also kwargs filters, include them
        if filter_kwargs:
            kwargs_result = self.filter_for_keys_set(**kwargs)
            result_sets.append(kwargs_result)

        # Intersect all result sets (AND logic between multiple Q args and kwargs)
        if not result_sets:
            return set()
        return set.intersection(*result_sets) if result_sets else set()

    def filter(self, *args, **kwargs) -> "QueryBuilder":
        """
        Query for Model instances matching the specified criteria.

        This is the primary query method for Popoto, providing Django-like filtering
        syntax with Redis-optimized execution. All filter parameters are AND-ed
        together by default. Use Q objects for OR logic and complex combinations.

        Returns a QueryBuilder that supports method chaining. The QueryBuilder also
        behaves like a list for backward compatibility - you can iterate over it or
        pass it to len() and it will execute the query automatically.

        Filter Parameters:
        -----------------
        Available filters depend on the field types in your Model:

        **KeyField filters:**
        - `field=value` - Exact match
        - `field__in=[v1, v2]` - Match any value in list
        - `field__contains="x"` - Substring match (uses Redis KEYS, slow)
        - `field__startswith="x"` - Prefix match
        - `field__endswith="x"` - Suffix match
        - `field__isnull=True/False` - Null check

        **SortedField filters:**
        - `field=value` - Exact match
        - `field__gt=value` - Greater than
        - `field__gte=value` - Greater than or equal
        - `field__lt=value` - Less than
        - `field__lte=value` - Less than or equal

        **Q Objects (for complex logic):**
        - `Q(field=value)` - Basic Q object (equivalent to kwargs)
        - `Q(...) | Q(...)` - OR logic (union of results)
        - `Q(...) & Q(...)` - AND logic (intersection of results)
        - `~Q(...)` - NOT logic (exclusion)

        Result Modifiers (kwargs API):
        -----------------------------
        - `order_by="field"` - Sort ascending by field
        - `order_by="-field"` - Sort descending by field
        - `limit=N` - Return at most N results
        - `values=("field1", "field2")` - Return dicts with only specified fields
          instead of full Model instances (projection query, more efficient)

        Chainable Methods:
        -----------------
        - `.filter(**kwargs)` - Add more filter criteria
        - `.order_by("field")` - Sort results (prefix with "-" for descending)
        - `.limit(n)` - Limit number of results
        - `.values("field1", "field2")` - Return dicts instead of objects
        - `.all()` - Execute and return results
        - `.first()` - Execute and return first result or None
        - `.count()` - Count matching results without loading objects

        Args:
            *args: Q objects for complex query expressions
            **kwargs: Filter parameters and result modifiers as described above.

        Returns:
            QueryBuilder that can be chained or iterated directly.

        Raises:
            QueryException: If unknown filter parameters are provided.

        Example:
            # Original kwargs API (still fully supported)
            users = User.query.filter(
                status="active",
                tier="premium",
                created_at__gte=datetime(2024, 1, 1),
                order_by="-created_at",
                limit=50
            )

            # Chainable API
            users = User.query.filter(status="active").order_by("-created_at").limit(50).all()

            # Chain multiple filters
            users = User.query.filter(status="active").filter(tier="premium").all()

            # Efficient projection - only load specific fields
            emails = User.query.filter(status="active").values("email", "name").all()

            # OR logic with Q objects
            users = User.query.filter(Q(status="active") | Q(type="premium"))

            # Complex combinations
            users = User.query.filter(
                (Q(status="active") | Q(type="premium")) & Q(rating__gt=3.0)
            )
        """
        from .q import Q
        from .expressions import Expression, CombinedExpression

        # Process args - can be Q objects or Expression objects
        q_objects = []
        for arg in args:
            if isinstance(arg, Q):
                q_objects.append(arg)
            elif isinstance(arg, (Expression, CombinedExpression)):
                # Convert Expression to Q object
                q_objects.append(arg.to_q())

        # Extract result modifiers from kwargs for the QueryBuilder
        filters = {
            k: v for k, v in kwargs.items() if k not in {"limit", "order_by", "values"}
        }
        builder = QueryBuilder(self, filters, q_objects)

        # Apply result modifiers if provided in kwargs (for backward compatibility)
        if "limit" in kwargs:
            builder._limit_value = kwargs["limit"]
        if "order_by" in kwargs:
            builder._order_by_value = kwargs["order_by"]
        if "values" in kwargs:
            builder._values_tuple = kwargs["values"]

        return builder

    def top_by_decay(
        self,
        field_name=None,
        n=10,
        decay_rate=None,
        base_score_field=None,
        *,
        as_of=None,
    ):
        """Return top-N instances ranked by time-decayed score.

        Convenience method that creates a QueryBuilder and delegates.
        For partitioned fields, use query.filter(partition=value).top_by_decay().

        Args:
            field_name: Name of a DecayingSortedField on the model. Optional
                when the model has exactly one DecayingSortedField (or subclass).
            n: Maximum number of results to return. Default 10.
            decay_rate: Override the field's decay_rate for this query.
            base_score_field: Override the field's base_score_field for this query.

        Returns:
            List of model instances in decayed-score order.
        """
        builder = QueryBuilder(self)
        return builder.top_by_decay(
            field_name,
            n=n,
            decay_rate=decay_rate,
            base_score_field=base_score_field,
            as_of=as_of,
        )

    def composite_score(
        self,
        indexes: dict,
        limit: int = 10,
        aggregate: str = "SUM",
        min_score: float = None,
        post_filter: Optional[Callable[[str, float], bool]] = None,
        co_occurrence_boost: dict = None,
        similarity_boost: dict = None,
        temperature: float = 1.0,
        *,
        as_of: Optional[float] = None,
    ) -> list:
        """Return top-K instances ranked by weighted composite score.

        Convenience method that creates a QueryBuilder and delegates.
        For partitioned fields, use
        ``query.filter(partition=value).composite_score(...)``.

        Args:
            indexes: Mapping of field names to weights.
            limit: Maximum results to return. Default 10.
            aggregate: Aggregation mode: "SUM", "MIN", or "MAX".
            min_score: Optional minimum composite score threshold.
            post_filter: Optional callable (redis_key, score) -> bool.
            co_occurrence_boost: Optional {redis_key: weight} dict.
            similarity_boost: Optional {redis_key: score} dict from
                semantic_search().
            temperature: Score scaling factor. Default 1.0 (no scaling).
                Must be > 0.

        Returns:
            List of model instances ranked by composite score.
        """
        builder = QueryBuilder(self)
        return builder.composite_score(
            indexes=indexes,
            limit=limit,
            aggregate=aggregate,
            min_score=min_score,
            post_filter=post_filter,
            co_occurrence_boost=co_occurrence_boost,
            similarity_boost=similarity_boost,
            temperature=temperature,
            as_of=as_of,
        )

    def semantic_search(
        self,
        query_text: str,
        indexes: dict = None,
        limit: int = 10,
        aggregate: str = "SUM",
        min_score: float = None,
        post_filter: Optional[Callable[[str, float], bool]] = None,
        co_occurrence_boost: dict = None,
        temperature: float = 1.0,
    ) -> list:
        """Return top-K instances ranked by semantic similarity.

        Convenience method that creates a QueryBuilder and delegates.

        Args:
            query_text: The text to search for semantically.
            indexes: Optional field-weight mapping for composite scoring.
            limit: Maximum results to return. Default 10.
            aggregate: Aggregation mode. Default "SUM".
            min_score: Optional minimum score threshold.
            post_filter: Optional callable (redis_key, score) -> bool.
            co_occurrence_boost: Optional {redis_key: weight} dict.
            temperature: Score scaling factor. Default 1.0.

        Returns:
            List of model instances ranked by semantic similarity.
        """
        builder = QueryBuilder(self)
        return builder.semantic_search(
            query_text=query_text,
            indexes=indexes,
            limit=limit,
            aggregate=aggregate,
            min_score=min_score,
            post_filter=post_filter,
            co_occurrence_boost=co_occurrence_boost,
            temperature=temperature,
        )

    def keyword_search(
        self,
        query_text: str,
        field: str = None,
        limit: int = 10,
    ) -> list:
        """Return instances ranked by BM25 keyword relevance.

        Convenience method that creates a QueryBuilder and delegates.

        Args:
            query_text: The search query string.
            field: Name of the BM25Field to search. Optional when the model
                has exactly one BM25Field.
            limit: Maximum results to return. Default 10.

        Returns:
            List of model instances ranked by BM25 score (descending).
        """
        builder = QueryBuilder(self)
        return builder.keyword_search(
            query_text=query_text,
            field=field,
            limit=limit,
        )

    def fuse(
        self,
        k: int = 60,
        limit: int = 10,
        post_filter: Optional[Callable] = None,
        weights: Optional[dict] = None,
        **ranked_lists,
    ) -> list:
        """Reciprocal Rank Fusion across heterogeneous ranked lists.

        Convenience method that creates a QueryBuilder and delegates.

        Args:
            k: RRF constant (default 60).
            limit: Maximum results to return. Default 10.
            post_filter: Optional (redis_key, rrf_score) -> bool callback.
            weights: Optional ``{list_name: multiplier}`` mapping for
                per-list RRF weighting. ``None`` == unweighted (default).
                Must be an explicit keyword argument -- never swept into
                ``**ranked_lists``.
            **ranked_lists: Named ranked lists of (redis_key, score) tuples.

        Reached from ``Model.query``, this entry point carries no filters, so
        the fused set is unscoped -- the ranked lists are used exactly as
        given. To scope a fusion, filter first:
        ``Model.query.filter(agent_id=...).fuse(...)``. See
        :meth:`QueryBuilder.fuse` for what that applies (#576).

        Returns:
            List of model instances ranked by RRF score (descending).

        Raises:
            QueryException: If no ranked lists are provided. The Q-object
                refusal documented on :meth:`QueryBuilder.fuse` is not
                reachable here, since this path has no filters to carry.
        """
        builder = QueryBuilder(self)
        return builder.fuse(
            k=k,
            limit=limit,
            post_filter=post_filter,
            weights=weights,
            **ranked_lists,
        )

    def _execute_filter(
        self,
        q_objects: list = None,
        _no_track: bool = False,
        _allow_pushdown: bool = True,
        **kwargs,
    ) -> list:
        """Internal method to execute filter logic and return results.

        This is the actual filter execution, called by QueryBuilder.all() and
        the backward-compatible list operations on QueryBuilder.

        Args:
            q_objects: List of Q objects for complex query expressions
            _no_track: If True, suppress on_read() for AccessTrackerMixin models
            _allow_pushdown: If False, read the full sorted range even when the
                query qualifies for a bounded read. Set on the one retry that
                stale index members can force.
            **kwargs: Filter parameters and result modifiers

        Returns:
            List of Model instances or dicts
        """
        # Reset geo distances for this query
        self._geo_distances = {}
        self._geo_distance_unit = None

        # Use _evaluate_filter_args if Q objects present, otherwise filter_for_keys_set
        if q_objects:
            self._pushdown_allowed = False
            db_keys_set = self._evaluate_filter_args(q_objects, kwargs)
            # Q objects combine results from multiple filter_for_keys_set calls,
            # so _sorted_field_order is unreliable — clear it
            self._sorted_field_order = None
            self._sorted_field_name = None
        else:
            self._pushdown_allowed = _allow_pushdown
            try:
                db_keys_set = self.filter_for_keys_set(**kwargs)
            finally:
                self._pushdown_allowed = False
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided
        # but not when sorted field ordering is active (it's a smarter default)
        if (
            "order_by" not in kwargs
            and self.model_class._meta.order_by
            and not getattr(self, "_sorted_field_order", None)
        ):
            kwargs["order_by"] = self.model_class._meta.order_by

        # Use sorted field order if available and no explicit order_by
        sorted_field_order = getattr(self, "_sorted_field_order", None)
        explicit_order_by = kwargs.get("order_by", None)
        # Meta.order_by is a default - sorted field order takes precedence over it
        if sorted_field_order and not explicit_order_by:
            db_keys_set = sorted_field_order  # Use ordered list instead of set

        # Bound the key list before hydration when the range read could not be
        # bounded itself. filter_for_keys_set has already intersected
        # _sorted_field_order down to the keys every other index agreed on, so
        # the AND happened without loading anything and slicing here is sound
        # even with other indexed filters in play. This is the cut that matters:
        # it takes hydration from every key in the partition to `limit` HGETALLs.
        # The Redis-side bound saves transferring the key list, a smaller win.
        db_keys_set = self._bound_keys_before_hydration(
            db_keys_set, q_objects, _allow_pushdown, kwargs
        )

        # A pending client-side filter must see every candidate before any
        # truncation: get_many_objects slices KeyField-ordered keys to `limit`
        # BEFORE hydration, which would cut rows the plain-field filter would
        # have kept -- filter(kind="x", note="hit", limit=2) on a model whose
        # Meta.order_by is a KeyField returned the first two keys, filtered
        # them all away, and answered [] although matches existed further down.
        # prepare_results re-applies the limit after the client filters run.
        client_filters_pending = bool(getattr(self, "_pending_client_filters", None))
        objects = Query.get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=None if client_filters_pending else kwargs.get("limit", None),
            values=kwargs.get("values", None),
        )

        if self._short_result_action(
            len(objects), _allow_pushdown, self._snapshot_pushdown_state()
        ):
            return self._execute_filter(
                q_objects=q_objects,
                _no_track=_no_track,
                _allow_pushdown=False,
                **kwargs,
            )

        # Apply client-side filters for plain (unindexed) fields
        client_filters = getattr(self, "_pending_client_filters", {})
        if client_filters:
            filtered = []
            for obj in objects:
                match = True
                for field_name, expected_value in client_filters.items():
                    if isinstance(obj, dict):
                        actual = obj.get(field_name)
                    else:
                        actual = getattr(obj, field_name, None)
                    if actual != expected_value:
                        match = False
                        break
                if match:
                    filtered.append(obj)
            objects = filtered

        # Attach geo distances to objects if available
        if self._geo_distances:
            # Normalize distance dict keys to strings for consistent lookup
            normalized_distances = {}
            for key, dist in self._geo_distances.items():
                if isinstance(key, bytes):
                    normalized_distances[key.decode()] = dist
                else:
                    normalized_distances[key] = dist

            for obj in objects:
                if isinstance(obj, dict):
                    # When values= is used, obj is a dict - skip distance attachment
                    continue
                redis_key = obj.db_key.redis_key
                if isinstance(redis_key, bytes):
                    redis_key = redis_key.decode()
                distance = normalized_distances.get(redis_key)
                if distance is not None:
                    obj._geo_distance = distance
                    obj._geo_distance_unit = self._geo_distance_unit

            # Sort by distance (ascending) to preserve geo-sorted order
            # Only sort model objects, not dicts
            model_objects = [o for o in objects if not isinstance(o, dict)]
            dict_objects = [o for o in objects if isinstance(o, dict)]
            model_objects.sort(key=lambda o: getattr(o, "_geo_distance", float("inf")))
            objects = model_objects + dict_objects

        results = self.prepare_results(objects, **kwargs)

        # Fire on_read for AccessTrackerMixin models (skip for value projections)
        if not _no_track and not kwargs.get("values"):
            model_results = [r for r in results if not isinstance(r, dict)]
            if model_results:
                _fire_on_read(self.model_class, model_results)

        return results

    def prepare_results(
        self,
        objects,
        order_by: str = "",
        values: tuple = (),
        limit: int = None,
        **kwargs,
    ):
        """Apply sorting and limiting to query results.

        This is a post-processing step that operates on already-loaded objects.
        For large result sets, sorting happens in Python rather than Redis,
        which may have performance implications.

        Design Note:
        -----------
        Sorting is applied after fetching because Redis doesn't support cross-key
        sorting natively. SortedFields maintain their own sorted sets for range
        queries, but general-purpose sorting across arbitrary fields requires
        loading objects first.

        For KeyFields, `get_many_objects()` can optimize sorting when the sort
        field is part of the key (extracting sort values from key strings without
        loading full objects). This method handles the remaining cases.

        Args:
            objects: List of Model instances or dicts (if values projection was used)
            order_by: Field name to sort by. Prefix with "-" for descending order.
            values: Tuple of field names if projection was used (affects sort behavior)
            limit: Maximum number of results to return after sorting
            **kwargs: Unused, accepts extra params for forward compatibility

        Returns:
            Sorted and limited list of objects or dicts.

        Raises:
            QueryException: If order_by field doesn't exist or isn't included in
                           values tuple when using projection.

        Note:
            Null values in the sort field are handled by substituting the field's
            default type value (e.g., "" for str, 0 for int), ensuring consistent
            ordering.
        """
        # Apply default order_by from Meta if not explicitly provided
        if not order_by and self.model_class._meta.order_by:
            order_by = self.model_class._meta.order_by

        reverse_order = False
        if order_by and order_by.startswith("-"):
            reverse_order = True
            order_by = order_by[1:]
        if order_by:
            order_by_attr_name = order_by
            if (
                not isinstance(order_by_attr_name, str)
            ) or order_by_attr_name not in self.model_class._meta.fields:
                raise QueryException(
                    f"order_by={order_by_attr_name} must be a field name (str)"
                )
            attr_type = self.model_class._meta.fields[order_by_attr_name].type
            if values and order_by_attr_name not in values:
                raise QueryException(
                    "field must be included in values=(fieldnames) in order to use order_by"
                )
            elif values:
                objects.sort(key=lambda item: item.get(order_by_attr_name))
            else:
                objects.sort(
                    key=lambda item: getattr(item, order_by_attr_name) or attr_type()
                )
            objects = (
                list(reversed(objects))[:limit] if reverse_order else objects[:limit]
            )

        if limit and len(objects) > limit:
            objects = objects[:limit]

        return objects

    def count(self, **kwargs) -> int:
        """Count instances matching the given filters (or all if no filters).

        More efficient than `len(filter(...))` because it avoids object
        instantiation. For unfiltered counts, uses Redis SCARD which is O(1).

        Args:
            **kwargs: Same filter parameters as `filter()`. If empty, counts
                     all instances of the Model.

        Returns:
            Integer count of matching instances.

        Performance:
        -----------
        - No filters: O(1) using Redis SCARD on the class set
        - With filters: O(N) where N is the result of filter intersection,
          but still avoids the overhead of HGETALL and object instantiation

        Example:
            # O(1) - count all products
            total = Product.query.count()

            # Filtered count - still more efficient than len(filter(...))
            active = Product.query.count(status="active", category="electronics")

        Note:
            Future optimization: Could use Redis SINTERCARD (Redis 7.0+) for
            filtered counts to avoid materializing the full key set.
        """
        if not len(kwargs):
            return int(
                POPOTO_REDIS_DB.scard(self.model_class._meta.db_class_set_key.redis_key)
                or 0
            )
        db_keys = self.filter_for_keys_set(**kwargs)
        client_filters = getattr(self, "_pending_client_filters", {})
        if client_filters:
            # Must load objects to apply client-side filters
            objects = Query.get_many_objects(self.model_class, db_keys)
            return sum(
                1
                for obj in objects
                if all(
                    getattr(obj, fname, None) == fval
                    for fname, fval in client_filters.items()
                )
            )
        return len(db_keys)

    @classmethod
    def get_many_objects(
        cls,
        model: "Model",
        db_keys: set,
        order_by_attr_name: str = None,
        limit: int = None,
        values: tuple = None,
        lazy: bool = True,
    ) -> list:
        """
        Batch-load multiple Model instances from Redis using pipelined commands.

        This is the core bulk retrieval method, optimized for performance through
        several strategies:

        1. **Redis Pipelines**: All HGETALL/HMGET commands are batched into a single
           pipeline, reducing network round trips from O(N) to O(1).

        2. **KeyField Sorting Optimization**: If sorting by a KeyField, sort values
           can be extracted directly from key strings without loading objects,
           and limit can be applied BEFORE loading (reducing Redis commands).

        3. **Projection Optimization**: With `values` tuple:
           - If ALL requested fields are KeyFields, data is extracted from key
             strings without ANY Redis commands.
           - Otherwise, uses HMGET instead of HGETALL for partial field retrieval.

        Args:
            model: The Model class to instantiate
            db_keys: Set of Redis keys to load
            order_by_attr_name: Field to sort by (prefix with "-" for descending).
                               If this is a KeyField, sorting is optimized.
            limit: Maximum objects to load. Applied early if sorting by KeyField.
            values: Tuple of field names for projection. If specified, returns
                   dicts instead of Model instances.

        Returns:
            List of Model instances, or list of dicts if values is specified.
            Objects for missing/deleted keys are silently excluded (with a
            warning logged).

        Raises:
            QueryException: If values is not a tuple.

        Performance Notes:
        -----------------
        - N objects without projection: 1 pipeline with N HGETALL commands
        - N objects with projection: 1 pipeline with N HMGET commands
        - Projection with only KeyFields: 0 Redis commands (parsed from keys)
        - Sorting by KeyField with limit: Only `limit` objects loaded

        Example:
            # Internal usage - typically called by filter() or all()
            objects = Query.get_many_objects(
                User,
                {b"User:alice", b"User:bob"},
                order_by_attr_name="username",
                limit=10
            )
        """
        from .encoding import decode_popoto_model_hashmap

        pipeline = POPOTO_REDIS_DB.pipeline()
        reverse_order = False
        # order the hashes list or objects before applying limit
        if order_by_attr_name and order_by_attr_name.startswith("-"):
            order_by_attr_name = order_by_attr_name[1:]
            reverse_order = True

        if order_by_attr_name and order_by_attr_name in model._meta.key_field_names:
            field_position = model._meta.get_db_key_index_position(order_by_attr_name)
            db_keys = list(db_keys)
            db_keys.sort(key=lambda key: key.split(b":")[field_position])
            db_keys = (
                list(reversed(db_keys))[:limit] if reverse_order else db_keys[:limit]
            )

        if values:
            if not isinstance(values, tuple):
                raise QueryException(
                    "values takes a tuple. eg. query.filter(values=('name',))"
                )
            elif set(values).issubset(model._meta.key_field_names):
                db_keys = [DB_key.from_redis_key(db_key) for db_key in db_keys]
                return [
                    {
                        field_name: (
                            model._meta.fields[field_name].type(
                                db_key[
                                    model._meta.get_db_key_index_position(field_name)
                                ]
                            )
                            if db_key[model._meta.get_db_key_index_position(field_name)]
                            else None
                        )
                        for field_name in values
                    }
                    for db_key in db_keys
                ]
            else:
                [pipeline.hmget(db_key, values) for db_key in db_keys]
                value_lists = pipeline.execute()
                hashes_list = [
                    {field_name: result[i] for i, field_name in enumerate(values)}
                    for result in value_lists
                ]

        else:
            [pipeline.hgetall(db_key) for db_key in db_keys]
            hashes_list = pipeline.execute()

        if {} in hashes_list:
            # A member whose hash is gone (Meta.ttl expiry, or an external
            # DEL). Repair what the key alone can repair so the next read
            # is clean; clean_indexes() covers the rest.
            missing = [db_key for db_key, data in zip(db_keys, hashes_list) if not data]
            purged = model._purge_orphan_keys(missing)
            logger.info(
                "%s: purged %d expired index member(s); run "
                "%s.clean_indexes() for partitions this read cannot derive.",
                model.__name__,
                purged,
                model.__name__,
            )

        return [
            decode_popoto_model_hashmap(
                model,
                redis_hash,
                fields_only=bool(values),
                lazy=lazy and not values,
                source_redis_key=source_key,
            )
            for source_key, redis_hash in zip(db_keys, hashes_list)
            if redis_hash
        ]

    # Async methods using native redis.asyncio

    async def async_get(
        self, db_key: DB_key = None, redis_key: str = None, **kwargs
    ) -> "Model":
        """Async version of get() using native async Redis.

        Retrieves a single model instance from Redis using non-blocking I/O.
        Uses redis.asyncio for true async operations without thread pool overhead.

        Args:
            db_key: Optional DB_key object
            redis_key: Optional Redis key string
            **kwargs: Field values to construct query

        Returns:
            Model instance or None if not found

        Raises:
            QueryException: If the filter matches more than one object.
        """
        from ..models.encoding import decode_popoto_model_hashmap

        if (
            not db_key
            and not redis_key
            and all([key in kwargs for key in self.options.key_field_names])
        ):
            db_key = self.model_class(**kwargs).db_key

        if db_key and not redis_key:
            redis_key = db_key.redis_key

        if redis_key:
            async_redis = await get_async_redis_db()
            hashmap = await async_redis.hgetall(redis_key)
            if not hashmap:
                return None
            instance = decode_popoto_model_hashmap(
                self.model_class, hashmap, source_redis_key=redis_key
            )
            await to_thread(_fire_on_read, self.model_class, [instance])
        else:
            instances = await self.async_filter(**kwargs)
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        return instance or None

    async def async_get_many(self, redis_keys: list, skip_none: bool = False) -> list:
        """Async version of get_many() using native async Redis.

        Retrieves multiple model instances by their Redis keys in a single
        async pipeline. See :meth:`Query.get_many` for full documentation.

        Args:
            redis_keys: List of Redis key strings to look up.
            skip_none: If True, filter out ``None`` entries from the result.

        Returns:
            List of Model instances (and/or ``None`` when *skip_none* is False).

        Example:
            keys = ["Product:widget:001", "Product:widget:002"]
            products = await Product.query.async_get_many(redis_keys=keys)
        """
        if not redis_keys:
            return []

        from ..models.encoding import decode_popoto_model_hashmap

        async_redis = await get_async_redis_db()
        pipeline = async_redis.pipeline()
        for key in redis_keys:
            pipeline.hgetall(key)
        hashes_list = await pipeline.execute()

        results = []
        live_instances = []
        for key, hashmap in zip(redis_keys, hashes_list):
            if hashmap:
                instance = decode_popoto_model_hashmap(
                    self.model_class, hashmap, source_redis_key=key
                )
                results.append(instance)
                live_instances.append(instance)
            else:
                results.append(None)

        if live_instances:
            await to_thread(_fire_on_read, self.model_class, live_instances)

        if skip_none:
            return [r for r in results if r is not None]
        return results

    async def async_filter(self, *, _allow_pushdown: bool = True, **kwargs) -> list:
        """Async version of filter() using native async Redis.

        Filters model instances based on field values using non-blocking I/O.
        Currently uses to_thread() for complex filter operations that involve
        field-specific query logic, but uses native async for result fetching.

        Preserves sorted field ordering from ZRANGEBYSCORE when no explicit
        order_by is provided, matching the sync _execute_filter behavior.

        Precedence: explicit order_by > sorted field order > Meta.order_by

        Args:
            _allow_pushdown: If False, read the full sorted range even when the
                query qualifies for a bounded read. Set on the one retry that
                stale index members can force. Keyword-only so it stays out of
                **kwargs, which go straight to filter_for_keys_set.
            **kwargs: Filter parameters (field values, limit, order_by, values)

        Returns:
            List of model instances or dicts (if values= specified)

        Note:
            The filter_for_keys_set() operation currently uses sync Redis due to
            the complexity of field-specific filter_query() implementations.
            Object loading uses native async Redis for better performance on
            bulk data retrieval.
        """
        # Reset geo distances for this query
        self._geo_distances = {}
        self._geo_distance_unit = None

        # Get keys using sync method in thread pool (field query implementations
        # are sync). Arming the pushdown, running the query and snapshotting its
        # bookkeeping all happen inside that one hop, under a per-loop lock:
        # `self` is shared by every caller on this model class, so anything read
        # off it after an await may belong to another coroutine. Everything
        # below reads `state`, never self._pushdown_*. The lock covers only this
        # hop; hydration below stays fully concurrent.
        async with _pushdown_lock_for_running_loop():
            db_keys_set, state = await to_thread(
                self._filter_keys_with_pushdown, _allow_pushdown, kwargs
            )
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided,
        # but not when sorted field ordering is active (it's a smarter default)
        if (
            "order_by" not in kwargs
            and self.model_class._meta.order_by
            and not state.sorted_field_order
        ):
            kwargs["order_by"] = self.model_class._meta.order_by

        # Use sorted field order if available and no explicit order_by
        sorted_field_order = state.sorted_field_order
        explicit_order_by = kwargs.get("order_by", None)
        # Meta.order_by is a default - sorted field order takes precedence over it
        if sorted_field_order and not explicit_order_by:
            db_keys_set = sorted_field_order  # Use ordered list instead of set

        # Bound the key list before hydration when the range read could not be
        # bounded itself, exactly as _execute_filter does. There is no Q-object
        # path here, so q_objects is always None.
        db_keys_set = self._bound_keys_before_hydration(
            db_keys_set, None, _allow_pushdown, kwargs, state=state
        )

        # Use native async for bulk object loading. As in _execute_filter, a
        # pending client-side filter suppresses the pre-hydration limit so the
        # filter sees every candidate; prepare_results re-applies the limit.
        client_filters_pending = bool(state.pending_client_filters)
        objects = await self._async_get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=None if client_filters_pending else kwargs.get("limit", None),
            values=kwargs.get("values", None),
        )

        if self._short_result_action(len(objects), _allow_pushdown, state):
            return await self.async_filter(_allow_pushdown=False, **kwargs)

        # Apply client-side filters for plain (unindexed) fields
        client_filters = state.pending_client_filters
        if client_filters:
            filtered = []
            for obj in objects:
                match = True
                for field_name, expected_value in client_filters.items():
                    if isinstance(obj, dict):
                        actual = obj.get(field_name)
                    else:
                        actual = getattr(obj, field_name, None)
                    if actual != expected_value:
                        match = False
                        break
                if match:
                    filtered.append(obj)
            objects = filtered

        # Attach geo distances to objects if available
        if self._geo_distances:
            normalized_distances = {}
            for key, dist in self._geo_distances.items():
                if isinstance(key, bytes):
                    normalized_distances[key.decode()] = dist
                else:
                    normalized_distances[key] = dist

            for obj in objects:
                if isinstance(obj, dict):
                    continue
                redis_key = obj.db_key.redis_key
                if isinstance(redis_key, bytes):
                    redis_key = redis_key.decode()
                distance = normalized_distances.get(redis_key)
                if distance is not None:
                    obj._geo_distance = distance
                    obj._geo_distance_unit = self._geo_distance_unit

            model_objects = [o for o in objects if not isinstance(o, dict)]
            dict_objects = [o for o in objects if isinstance(o, dict)]
            model_objects.sort(key=lambda o: getattr(o, "_geo_distance", float("inf")))
            objects = model_objects + dict_objects

        return self.prepare_results(objects, **kwargs)

    async def async_all(self, **kwargs) -> list:
        """Async version of all() using native async Redis.

        Retrieves all model instances using non-blocking I/O.

        Args:
            **kwargs: Optional order_by and values parameters

        Returns:
            List of all model instances or dicts (if values= specified)
        """
        async_redis = await get_async_redis_db()
        redis_db_keys_list = list(
            await async_redis.smembers(
                self.model_class._meta.db_class_set_key.redis_key
            )
        )

        # Apply default order_by from Meta if not explicitly provided
        if "order_by" not in kwargs and self.model_class._meta.order_by:
            kwargs["order_by"] = self.model_class._meta.order_by

        objects = await self._async_get_many_objects(
            self.model_class,
            set(redis_db_keys_list),
            order_by_attr_name=kwargs.get("order_by", None),
            values=kwargs.get("values", None),
        )

        return self.prepare_results(objects, **kwargs)

    async def async_count(self, **kwargs) -> int:
        """Async version of count() using native async Redis.

        Counts model instances matching filter criteria using non-blocking I/O.

        Args:
            **kwargs: Optional filter parameters

        Returns:
            Count of matching instances
        """
        async_redis = await get_async_redis_db()

        if not len(kwargs):
            count = await async_redis.scard(
                self.model_class._meta.db_class_set_key.redis_key
            )
            return int(count or 0)

        # Use sync filter_for_keys_set in thread pool for complex filter logic
        db_keys = await to_thread(self.filter_for_keys_set, **kwargs)
        client_filters = getattr(self, "_pending_client_filters", {})
        if client_filters:
            # Must load objects to apply client-side filters
            objects = await self._async_get_many_objects(self.model_class, db_keys)
            return sum(
                1
                for obj in objects
                if all(
                    getattr(obj, fname, None) == fval
                    for fname, fval in client_filters.items()
                )
            )
        return len(db_keys)

    async def async_keys(self, catchall=False, clean=False, **kwargs) -> list:
        """Async version of keys() using native async Redis.

        Retrieves Redis keys for model instances using non-blocking I/O.

        Args:
            catchall: If True, use KEYS pattern (debug only, not for production)
            clean: If True, clean up orphaned keys (debug only, not for production)
            **kwargs: Additional parameters

        Returns:
            List of Redis keys

        Note:
            The clean operation uses to_thread() as it involves complex pipeline
            operations. Regular key retrieval uses native async.
        """
        if clean:
            # Clean operation is complex with pipelines, use thread pool
            return await to_thread(self.keys, catchall=catchall, clean=clean, **kwargs)

        async_redis = await get_async_redis_db()

        if catchall:
            logger.warning(
                "{catchall} is for debugging purposes only. Not for use in production environment"
            )
            return list(await async_redis.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(
                await async_redis.smembers(
                    self.model_class._meta.db_class_set_key.redis_key
                )
            )

    @classmethod
    async def _async_get_many_objects(
        cls,
        model: "Model",
        db_keys: set,
        order_by_attr_name: str = None,
        limit: int = None,
        values: tuple = None,
        lazy: bool = True,
    ) -> list:
        """Async version of get_many_objects using native async Redis.

        Batch-loads multiple Model instances from Redis using async pipelined
        commands for true non-blocking I/O.

        Args:
            model: The Model class to instantiate
            db_keys: Set of Redis keys to load
            order_by_attr_name: Field to sort by (prefix with "-" for descending)
            limit: Maximum objects to load
            values: Tuple of field names for projection

        Returns:
            List of Model instances, or list of dicts if values is specified.
        """
        from .encoding import decode_popoto_model_hashmap
        from .db_key import DB_key

        async_redis = await get_async_redis_db()
        pipeline = async_redis.pipeline()

        reverse_order = False
        if order_by_attr_name and order_by_attr_name.startswith("-"):
            order_by_attr_name = order_by_attr_name[1:]
            reverse_order = True

        if order_by_attr_name and order_by_attr_name in model._meta.key_field_names:
            field_position = model._meta.get_db_key_index_position(order_by_attr_name)
            db_keys = list(db_keys)
            db_keys.sort(key=lambda key: key.split(b":")[field_position])
            db_keys = (
                list(reversed(db_keys))[:limit] if reverse_order else db_keys[:limit]
            )

        if values:
            if not isinstance(values, tuple):
                raise QueryException(
                    "values takes a tuple. eg. query.filter(values=('name',))"
                )
            elif set(values).issubset(model._meta.key_field_names):
                db_keys = [DB_key.from_redis_key(db_key) for db_key in db_keys]
                return [
                    {
                        field_name: (
                            model._meta.fields[field_name].type(
                                db_key[
                                    model._meta.get_db_key_index_position(field_name)
                                ]
                            )
                            if db_key[model._meta.get_db_key_index_position(field_name)]
                            else None
                        )
                        for field_name in values
                    }
                    for db_key in db_keys
                ]
            else:
                for db_key in db_keys:
                    pipeline.hmget(db_key, values)
                value_lists = await pipeline.execute()
                hashes_list = [
                    {field_name: result[i] for i, field_name in enumerate(values)}
                    for result in value_lists
                ]
        else:
            for db_key in db_keys:
                pipeline.hgetall(db_key)
            hashes_list = await pipeline.execute()

        if {} in hashes_list:
            logger.error(
                "one or more redis keys points to missing objects. Debug with Model.query.keys(clean=True)"
            )

        objects = [
            decode_popoto_model_hashmap(
                model,
                redis_hash,
                fields_only=bool(values),
                lazy=lazy and not values,
                source_redis_key=source_key,
            )
            for source_key, redis_hash in zip(db_keys, hashes_list)
            if redis_hash
        ]
        if not values:
            await to_thread(_fire_on_read, model, objects)
        return objects
