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

import logging
from asyncio import to_thread
from typing import TYPE_CHECKING, Optional

from .db_key import DB_key

if TYPE_CHECKING:
    from .base import Model
from ..redis_db import POPOTO_REDIS_DB, get_async_redis_db

logger = logging.getLogger("POPOTO.Query")


class QueryException(Exception):
    """Raised when a query is malformed or produces an unexpected result.

    Common causes include:
    - Using unknown filter parameters not supported by any field
    - Using `get()` when multiple objects match the criteria
    - Specifying `order_by` without including that field in `values`
    - Missing required partition fields for sorted field queries
    """

    pass


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

    def top_by_decay(self, field_name, n=10, decay_rate=None, base_score_field=None):
        """Return top-N instances ranked by time-decayed score.

        Executes a Lua script server-side that computes:
            decayed_score = base_score * elapsed_days ^ (-decay_rate)

        Args:
            field_name: Name of a DecayingSortedField on the model.
            n: Maximum number of results to return. Default 10.
            decay_rate: Override the field's decay_rate for this query.
            base_score_field: Override the field's base_score_field for this query.

        Returns:
            List of model instances in decayed-score order.

        Raises:
            QueryException: If field is not a DecayingSortedField or
                required partition_by filter is missing.
        """
        from ..fields.decaying_sorted_field import DecayingSortedField, DECAY_SCORE_LUA
        from .encoding import decode_popoto_model_hashmap

        model_class = self._query.model_class
        if field_name not in model_class._meta.fields:
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
            partition_values = [str(self._filters[pf]) for pf in field.partition_by]
        except KeyError:
            missing = [pf for pf in field.partition_by if pf not in self._filters]
            raise QueryException(
                f"top_by_decay() on '{field_name}' requires partition filter(s): "
                f"{', '.join(missing)}"
            )

        # Use actual field class for key generation (CyclicDecayField has
        # its own field_class_key prefix, distinct from DecayingSortedField)
        field_cls = type(field)
        sortedset_db_key = field_cls.get_sortedset_db_key(
            model_class, field_name, *partition_values
        )

        import time

        now = time.time()

        # Use extended Lua script for CyclicDecayField, plain script otherwise
        from ..fields.cyclic_decay_field import CyclicDecayField, CYCLIC_DECAY_LUA

        if isinstance(field, CyclicDecayField):
            # Build companion hash keys from partition values
            cycles_hash_key = CyclicDecayField._get_cycles_hash_key_from_parts(
                model_class, field_name, *partition_values
            )
            pressure_hash_key = CyclicDecayField._get_pressure_hash_key_from_parts(
                model_class, field_name, *partition_values
            )

            result = POPOTO_REDIS_DB.eval(
                CYCLIC_DECAY_LUA,
                3,  # number of KEYS
                sortedset_db_key.redis_key,
                cycles_hash_key,
                pressure_hash_key,
                str(now),
                str(effective_decay_rate),
                str(n),
                effective_base_score_field,
            )
        else:
            result = POPOTO_REDIS_DB.eval(
                DECAY_SCORE_LUA,
                1,  # number of KEYS
                sortedset_db_key.redis_key,
                str(now),
                str(effective_decay_rate),
                str(n),
                effective_base_score_field,
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
                instance = decode_popoto_model_hashmap(model_class, data)
                instances.append(instance)

        return instances

    def all(self) -> list:
        """Execute the query and return all matching results.

        Combines all accumulated filters, ordering, and limits into a single
        query execution. When computed_sort() is set, it takes precedence over
        order_by() and applies after fetch but before limit.

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
            results = self._query._execute_filter(q_objects=self._q_objects, **kwargs)
            # Apply computed sort (O(N log N) on full result set)
            results = sorted(
                results,
                key=self._computed_sort_fn,
                reverse=self._computed_sort_reverse,
            )
            # Apply limit after sorting
            if self._limit_value is not None:
                results = results[: self._limit_value]
            return results

        # Standard path: no computed_sort
        if self._limit_value is not None:
            kwargs["limit"] = self._limit_value
        if self._order_by_value is not None:
            kwargs["order_by"] = self._order_by_value
        if self._values_tuple is not None:
            kwargs["values"] = self._values_tuple
        return self._query._execute_filter(q_objects=self._q_objects, **kwargs)

    def count(self) -> int:
        """Count matching results without loading objects.

        Returns:
            Integer count of matching instances
        """
        # For Q objects, we need to execute the full query and count
        if self._q_objects:
            return len(self.all())
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

            # Fallback to filter when using non-key fields
            user = User.query.get(email="alice@example.com")  # May be slower
        """
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
            instance = decode_popoto_model_hashmap(self.model_class, hashmap)

        else:
            instances = self.filter(**kwargs)
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        # or not hasattr(instance, 'db_key')
        return instance or None

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
                "{clean} is for debugging purposes only. Not for use in production environment"
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
            result = field.__class__.filter_query(
                self.model_class, field_name, **kwargs
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

    def top_by_decay(self, field_name, n=10, decay_rate=None, base_score_field=None):
        """Return top-N instances ranked by time-decayed score.

        Convenience method that creates a QueryBuilder and delegates.
        For partitioned fields, use query.filter(partition=value).top_by_decay().

        Args:
            field_name: Name of a DecayingSortedField on the model.
            n: Maximum number of results to return. Default 10.
            decay_rate: Override the field's decay_rate for this query.
            base_score_field: Override the field's base_score_field for this query.

        Returns:
            List of model instances in decayed-score order.
        """
        builder = QueryBuilder(self)
        return builder.top_by_decay(
            field_name, n=n, decay_rate=decay_rate, base_score_field=base_score_field
        )

    def _execute_filter(self, q_objects: list = None, **kwargs) -> list:
        """Internal method to execute filter logic and return results.

        This is the actual filter execution, called by QueryBuilder.all() and
        the backward-compatible list operations on QueryBuilder.

        Args:
            q_objects: List of Q objects for complex query expressions
            **kwargs: Filter parameters and result modifiers

        Returns:
            List of Model instances or dicts
        """
        # Reset geo distances for this query
        self._geo_distances = {}
        self._geo_distance_unit = None

        # Use _evaluate_filter_args if Q objects present, otherwise filter_for_keys_set
        if q_objects:
            db_keys_set = self._evaluate_filter_args(q_objects, kwargs)
            # Q objects combine results from multiple filter_for_keys_set calls,
            # so _sorted_field_order is unreliable — clear it
            self._sorted_field_order = None
            self._sorted_field_name = None
        else:
            db_keys_set = self.filter_for_keys_set(**kwargs)
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

        objects = Query.get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=kwargs.get("limit", None),
            values=kwargs.get("values", None),
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

        return self.prepare_results(objects, **kwargs)

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
            logger.error(
                "one or more redis keys points to missing objects. Debug with Model.query.keys(clean=True)"
            )

        return [
            decode_popoto_model_hashmap(
                model, redis_hash, fields_only=bool(values), lazy=lazy and not values
            )
            for redis_hash in hashes_list
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
            instance = decode_popoto_model_hashmap(self.model_class, hashmap)
        else:
            instances = await self.async_filter(**kwargs)
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        return instance or None

    async def async_filter(self, **kwargs) -> list:
        """Async version of filter() using native async Redis.

        Filters model instances based on field values using non-blocking I/O.
        Currently uses to_thread() for complex filter operations that involve
        field-specific query logic, but uses native async for result fetching.

        Preserves sorted field ordering from ZRANGEBYSCORE when no explicit
        order_by is provided, matching the sync _execute_filter behavior.

        Precedence: explicit order_by > sorted field order > Meta.order_by

        Args:
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

        # Get keys using sync method in thread pool (field query implementations are sync)
        db_keys_set = await to_thread(self.filter_for_keys_set, **kwargs)
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided,
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

        # Use native async for bulk object loading
        objects = await self._async_get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=kwargs.get("limit", None),
            values=kwargs.get("values", None),
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

        return [
            decode_popoto_model_hashmap(
                model, redis_hash, fields_only=bool(values), lazy=lazy and not values
            )
            for redis_hash in hashes_list
            if redis_hash
        ]
