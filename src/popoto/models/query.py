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
import asyncio
import sys
import functools

from .db_key import DB_key
from ..redis_db import POPOTO_REDIS_DB, ENCODING

logger = logging.getLogger("POPOTO.Query")

# Python 3.8 compatibility for asyncio.to_thread()
if sys.version_info >= (3, 9):
    to_thread = asyncio.to_thread
else:
    # Backport for Python 3.8
    async def to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )


class QueryException(Exception):
    """Raised when a query is malformed or produces an unexpected result.

    Common causes include:
    - Using unknown filter parameters not supported by any field
    - Using `get()` when multiple objects match the criteria
    - Specifying `order_by` without including that field in `values`
    - Missing required partition fields for sorted field queries
    """

    pass


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

    def get(self, db_key: DB_key = None, redis_key: str = None, **kwargs) -> "Model":
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
           Additionally, sorted fields may have partition dependencies (`sort_by`)
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
        yet_employed_kwargs_set = set(kwargs.keys()).difference(
            {"limit", "order_by", "values"}
        )
        if not len(yet_employed_kwargs_set):
            return set()

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
            result = field.__class__.filter_query(self.model_class, field_name, **kwargs)
            # Handle tuple return from GeoField with distances
            if isinstance(result, tuple) and len(result) == 3:
                keys_set, distances, unit = result
                self._geo_distances.update(distances)
                self._geo_distance_unit = unit
                db_keys_sets.append(keys_set)
            else:
                db_keys_sets.append(result)
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                self.options.filter_query_params_by_field[field_name]
            ).difference(
                set(field.sort_by)
            )  # also remove the required sort_by field names

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

        # raise error on additional unknown query parameters
        if yet_employed_kwargs_set:
            raise QueryException(
                f"Invalid filter parameters: {','.join(yet_employed_kwargs_set)}"
            )

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            return set()
        # return intersection of all the db keys sets, effectively &&-ing all filters
        return set.intersection(*db_keys_sets)

    def filter(self, **kwargs) -> list:
        """
        Query for Model instances matching the specified criteria.

        This is the primary query method for Popoto, providing Django-like filtering
        syntax with Redis-optimized execution. All filter parameters are AND-ed
        together; OR queries require multiple calls combined in application code.

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

        Result Modifiers:
        ----------------
        - `order_by="field"` - Sort ascending by field
        - `order_by="-field"` - Sort descending by field
        - `limit=N` - Return at most N results
        - `values=("field1", "field2")` - Return dicts with only specified fields
          instead of full Model instances (projection query, more efficient)

        Args:
            **kwargs: Filter parameters and result modifiers as described above.

        Returns:
            List of Model instances matching all criteria, or list of dicts if
            `values` is specified. Empty list if no matches.

        Raises:
            QueryException: If unknown filter parameters are provided.

        Example:
            # Find active premium users created this year
            users = User.query.filter(
                status="active",
                tier="premium",
                created_at__gte=datetime(2024, 1, 1),
                order_by="-created_at",
                limit=50
            )

            # Efficient projection - only load specific fields
            emails = User.query.filter(
                status="active",
                values=("email", "name")
            )  # Returns [{"email": "...", "name": "..."}, ...]
        """
        # Reset geo distances for this query
        self._geo_distances = {}
        self._geo_distance_unit = None

        db_keys_set = self.filter_for_keys_set(**kwargs)
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided
        if "order_by" not in kwargs and self.model_class._meta.order_by:
            kwargs["order_by"] = self.model_class._meta.order_by

        objects = Query.get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=kwargs.get("limit", None),
            values=kwargs.get("values", None),
        )

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
            model_objects.sort(key=lambda o: getattr(o, '_geo_distance', float('inf')))
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
        return len(
            self.filter_for_keys_set(**kwargs)
        )  # maybe possible to refactor to use redis.SINTERCARD

    @classmethod
    def get_many_objects(
        cls,
        model: "Model",
        db_keys: set,
        order_by_attr_name: str = None,
        limit: int = None,
        values: tuple = None,
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
            decode_popoto_model_hashmap(model, redis_hash, fields_only=bool(values))
            for redis_hash in hashes_list
            if redis_hash
        ]

    # Async methods

    async def async_get(
        self, db_key: DB_key = None, redis_key: str = None, **kwargs
    ) -> "Model":
        """Async version of get().

        Retrieves a single model instance from Redis in a thread pool to avoid
        blocking the event loop.

        Args:
            db_key: Optional DB_key object
            redis_key: Optional Redis key string
            **kwargs: Field values to construct query

        Returns:
            Model instance or None if not found
        """
        return await to_thread(self.get, db_key=db_key, redis_key=redis_key, **kwargs)

    async def async_filter(self, **kwargs) -> list:
        """Async version of filter().

        Filters model instances based on field values in a thread pool to avoid
        blocking the event loop.

        Args:
            **kwargs: Filter parameters (field values, limit, order_by, values)

        Returns:
            List of model instances or dicts (if values= specified)
        """
        return await to_thread(self.filter, **kwargs)

    async def async_all(self, **kwargs) -> list:
        """Async version of all().

        Retrieves all model instances in a thread pool to avoid blocking
        the event loop.

        Args:
            **kwargs: Optional order_by and values parameters

        Returns:
            List of all model instances or dicts (if values= specified)
        """
        return await to_thread(self.all, **kwargs)

    async def async_count(self, **kwargs) -> int:
        """Async version of count().

        Counts model instances matching filter criteria in a thread pool to avoid
        blocking the event loop.

        Args:
            **kwargs: Optional filter parameters

        Returns:
            Count of matching instances
        """
        return await to_thread(self.count, **kwargs)

    async def async_keys(self, catchall=False, clean=False, **kwargs) -> list:
        """Async version of keys().

        Retrieves Redis keys for model instances in a thread pool to avoid
        blocking the event loop.

        Args:
            catchall: If True, use KEYS pattern (debug only, not for production)
            clean: If True, clean up orphaned keys (debug only, not for production)
            **kwargs: Additional parameters

        Returns:
            List of Redis keys
        """
        return await to_thread(self.keys, catchall=catchall, clean=clean, **kwargs)
