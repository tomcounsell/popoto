"""
Sorted Field Mixin for Redis Sorted Set-backed range queries.

This module provides the SortedFieldMixin class, which enables efficient range-based
filtering on numeric and temporal fields by leveraging Redis Sorted Sets (ZSET).

Design Philosophy:
    Redis stores all data as strings, making range queries on numeric values
    impossible without additional data structures. The SortedFieldMixin solves
    this by maintaining a parallel Sorted Set index alongside each model instance.
    When a model is saved, its Redis key is added to the Sorted Set with the field
    value as the score, enabling O(log(N)) range queries via ZRANGEBYSCORE.

Trade-offs:
    - Storage: Each sorted field creates an additional Redis key per unique
      partition (or one global key if no partitioning). This is a deliberate
      trade-off of storage space for query performance.
    - Consistency: The Sorted Set index must be updated on every save/delete.
      This is handled automatically through the on_save() and on_delete() hooks.
    - Null values: Currently not supported. A sorted field cannot be null because
      Redis Sorted Sets require a numeric score. Future versions may support
      nulls via a separate index set.

Integration with Query System:
    The Query class (models/query.py) prioritizes sorted field filters because
    they typically return smaller result sets than key field filters. The
    filter_query() method returns a set of Redis keys that can be intersected
    with results from other field filters.

Example:
    class Product(Model):
        name = KeyField()
        price = SortedField(type=float)
        category = KeyField()

    # Range query - uses ZRANGEBYSCORE under the hood
    Product.query.filter(price__gte=10.0, price__lte=50.0)

    # With partitioning for better performance on large datasets
    class Product(Model):
        name = KeyField()
        price = SortedField(type=float, partition_by='category')
        category = KeyField()

    # Query must include partition field
    Product.query.filter(category='electronics', price__lte=100.0)
"""

import logging
import warnings
from decimal import Decimal
import datetime
import typing
import redis

from ..models.canonical_key import canonical_key_str
from ..models.db_key import DB_key
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

if typing.TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..models.base import Model

logger = logging.getLogger("POPOTO.SortedFieldMixin")


class SortedFieldMixin:
    """
    Mixin that adds Redis Sorted Set indexing to enable range-based queries.

    This mixin transforms a regular Field into one that supports Django-style
    range lookups (__gt, __gte, __lt, __lte) by maintaining a Redis Sorted Set
    index. The design follows the mixin pattern so it can be combined with
    other field behaviors (e.g., SortedKeyField combines sorting with key field
    identity).

    Why a Mixin:
        Popoto uses composition over inheritance for field capabilities. This
        allows creating field types like SortedKeyField that combines range
        query support with key field identity, without complex inheritance
        hierarchies.

    Partitioning via partition_by:
        For large datasets, a single global Sorted Set becomes a bottleneck.
        The partition_by parameter partitions the index by one or more key fields,
        creating separate Sorted Sets per partition. For example, sorting
        products by price within each category creates one Sorted Set per
        category rather than one global set.

        .. deprecated::
            The ``sort_by`` parameter is deprecated in favor of ``partition_by``.
            ``sort_by`` still works but emits a DeprecationWarning.

    Supported Types:
        - int, float: Used directly as Redis scores
        - Decimal: Converted to float (some precision loss possible)
        - datetime: Converted to Unix timestamp
        - date: Converted to ordinal (days since year 1)
        - time: Converted to timestamp

    Attributes:
        type: The Python type for this field (must be numeric or temporal).
        null: Must be False; sorted fields cannot be null.
        default: Default value when none provided.
        partition_by: Tuple of field names to partition the sorted index by.

    Example:
        # Basic sorted field
        price = SortedField(type=float)

        # Partitioned by category for better scaling
        price = SortedField(type=float, partition_by='category')

        # Partitioned by multiple fields
        timestamp = SortedField(type=datetime.datetime, partition_by=('exchange', 'symbol'))

        # Deprecated but still supported
        price = SortedField(type=float, sort_by='category')  # emits DeprecationWarning
    """

    type: type = float
    null: bool = False
    default = ""
    partition_by = tuple()

    # Export/import: the sorted-set index is fully rebuilt from the field
    # value by on_save(). auto_now fields need the driver's
    # save(skip_auto_now=True) to preserve the exported timestamp rather than
    # restamping time.time() -- that is a generic driver-level flag, not
    # field-specific carried state.
    roundtrip_policy: str = "rebuild"

    def __init__(self, **kwargs):
        """
        Initialize the sorted field mixin with sorting configuration.

        This constructor follows Popoto's field initialization pattern where
        each mixin updates the shared field_defaults dict and then applies
        kwargs overrides. The super().__init__() call ensures proper MRO
        (Method Resolution Order) traversal through all mixins.

        The partition_by parameter accepts either a single field name string or a
        tuple of field names for multi-field partitioning. Single strings are
        automatically converted to tuples for consistent internal handling.

        The deprecated ``sort_by`` parameter is still accepted for backward
        compatibility. If provided, it is used as the value for ``partition_by``
        and a DeprecationWarning is emitted.

        Args:
            **kwargs: Field configuration options including:
                - type: Python type (int, float, Decimal, datetime, date, time)
                - default: Default value (None by default; cannot default datetime)
                - partition_by: Field name(s) to partition the sorted index
                - sort_by: Deprecated alias for partition_by. Emits DeprecationWarning.
                - auto_now_add: If True, set to time.time() on first save (for
                    float/int types only). Value is only set if field is None/falsy.
                - auto_now: If True, set to time.time() on every save (for
                    float/int types only). Always overwrites the current value.

        Raises:
            ModelException: If both sort_by and partition_by are provided, if
                partition_by is not a string or tuple, or if null=True
                is attempted (not yet supported).
        """
        # Handle sort_by -> partition_by deprecation
        has_sort_by = "sort_by" in kwargs
        has_partition_by = "partition_by" in kwargs

        if has_sort_by and has_partition_by:
            from ..exceptions import ModelException

            raise ModelException(
                "Cannot specify both 'sort_by' and 'partition_by'. "
                "Use 'partition_by' (sort_by is deprecated)."
            )

        if has_sort_by:
            warnings.warn(
                "sort_by is deprecated, use partition_by instead",
                DeprecationWarning,
                stacklevel=2,
            )
            kwargs["partition_by"] = kwargs.pop("sort_by")

        # Extract auto_now params before super().__init__() to avoid Field validation issues
        self.auto_now_add = kwargs.pop("auto_now_add", False)
        self.auto_now = kwargs.pop("auto_now", False)

        super().__init__()
        sortedfield_defaults = {
            "type": float,
            "null": False,
            "default": None,  # cannot set a default for datetime, so no type gets a default
            "partition_by": tuple(),
        }
        self.field_defaults.update(sortedfield_defaults)

        # set field_options, let kwargs override
        for k, v in sortedfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

        if isinstance(self.partition_by, str):
            self.partition_by = tuple((self.partition_by,))

        elif self.partition_by and not isinstance(self.partition_by, tuple):
            from ..exceptions import ModelException

            raise ModelException("partition_by must be str or tuple of str field names")

        # todo: move this to field init validation
        if self.null is not False:
            from ..exceptions import ModelException

            raise ModelException("SortedField cannot be null")
            # todo: when allow null in SortedField. null removes instance from SortedSet
            # todo: how to filter by null value? use extra index set just for nulls?

    @property
    def sort_by(self):
        """Deprecated: use partition_by instead."""
        warnings.warn(
            "sort_by is deprecated, use partition_by instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.partition_by

    @sort_by.setter
    def sort_by(self, value):
        """Deprecated: use partition_by instead."""
        warnings.warn(
            "sort_by is deprecated, use partition_by instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self.partition_by = value

    def get_filter_query_params(self, field_name) -> set:
        """
        Register the Django-style lookup parameters this field supports.

        This method is called during model class construction to build a
        registry of valid query parameters. The Query class uses this registry
        to validate filter() arguments and route them to the appropriate
        field's filter_query() method.

        The sorted field mixin adds range comparison operators following
        Django's double-underscore convention. The exact match parameter
        (field_name without suffix) is also included for equality filtering.

        Args:
            field_name: The attribute name of this field on the model.

        Returns:
            A set of valid query parameter strings that can be passed to
            Model.query.filter(). Includes parameters from parent classes
            via super() to support mixin composition.
        """
        return (
            super()
            .get_filter_query_params(field_name)
            .union(
                {
                    f"{field_name}",
                    f"{field_name}__gt",
                    f"{field_name}__gte",
                    f"{field_name}__lt",
                    f"{field_name}__lte",
                    f"{field_name}__between",
                    # f'{field_name}__isnull',  # todo: see todo in __init__
                }
            )
        )

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """
        Validate that the value is compatible with sorted set storage.

        Extends the base Field validation to ensure the value matches the
        declared type. This is important because the value will be converted
        to a numeric score for Redis storage, and type mismatches could
        cause conversion errors or incorrect sorting behavior.

        For fields with auto_now or auto_now_add=True, null values are allowed
        since they will be automatically populated in format_value_pre_save().

        Args:
            field: The Field instance being validated.
            value: The value to validate.
            null_check: Whether to enforce null constraints.
            **kwargs: Additional validation context.

        Returns:
            True if the value is valid for this sorted field, False otherwise.
        """
        # Skip null check for auto_now/auto_now_add fields - they'll be populated later
        if not value and (
            getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
        ):
            null_check = False

        if not super().is_valid(field, value, null_check):
            return False
        if value and not isinstance(value, field.type):
            return False
        return True

    def format_value_pre_save(self, field_value, skip_auto_now=False):
        """
        Normalize the field value before saving to the model's Redis hash.

        This method handles the value stored in the model's primary hash
        (the actual model data), not the sorted set index. Native numeric
        and temporal types are preserved as-is for msgpack serialization,
        while other types (like Decimal) are converted to float.

        For numeric types (int, float), this method also applies auto_now_add
        and auto_now logic to automatically set Unix timestamps.

        Note that this is separate from convert_to_numeric(), which handles
        the score conversion for the sorted set index.

        Args:
            field_value: The raw value to be saved.
            skip_auto_now: If True, suppress auto_now timestamp updates.
                Useful for data migrations where existing timestamps should
                be preserved. Does not affect auto_now_add behavior.

        Returns:
            The normalized value suitable for msgpack serialization.
        """
        import time

        # Apply auto_now/auto_now_add for numeric types (Unix timestamps)
        if self.type in (int, float):
            if self.auto_now and not skip_auto_now:
                # Always set current timestamp on every save (unless skipped)
                field_value = int(time.time()) if self.type is int else time.time()
            elif self.auto_now_add and not field_value:
                # Set timestamp only on first save (when value is None/falsy)
                field_value = int(time.time()) if self.type is int else time.time()

        if self.type in [int, float, datetime.datetime, datetime.date, datetime.time]:
            return field_value
        else:
            return float(field_value)

    @classmethod
    def convert_to_numeric(cls, field, field_value):
        """
        Convert a field value to a numeric score for Redis Sorted Set storage.

        Redis Sorted Sets require numeric scores for ordering. This method
        provides the conversion strategy for each supported type, ensuring
        that the natural ordering of the original type is preserved in the
        numeric representation.

        Conversion strategies:
            - int/float: Used directly (no conversion needed)
            - Decimal: Cast to float (may lose precision for very large values)
            - date: Converted to ordinal (days since year 1), preserving day-level ordering
            - datetime: Converted to Unix timestamp, preserving second-level ordering
            - time: Converted to seconds since midnight, preserving time-of-day
              ordering. tzinfo does not affect the score (see the branch below).

        This method is used both when saving (to compute the score) and when
        filtering (to convert query values to comparable scores).

        Args:
            field: The Field instance containing type information.
            field_value: The value to convert.

        Returns:
            A numeric value suitable for use as a Redis Sorted Set score.

        Raises:
            ValueError: If the value cannot be converted to a numeric score.
        """
        if field.type in [int, float]:
            return field_value
        elif field.type is Decimal:
            return float(field_value)
        elif field.type is datetime.date:
            return field_value.toordinal()
        elif field.type is datetime.datetime:
            # A score has to be a pure function of the stored value. The hash
            # encoding carries no offset (see #521), so a value that arrives
            # fresh is aware while the same value read back is naive, and
            # `.timestamp()` reads a naive datetime as LOCAL time. Left alone,
            # saving a reloaded row re-scores it by the machine's UTC offset
            # and the index disagrees with the data it indexes (#519).
            if field_value.tzinfo is None:
                return field_value.replace(tzinfo=datetime.timezone.utc).timestamp()
            return field_value.timestamp()
        elif field.type is datetime.time:
            # `datetime.time` has no `.timestamp()` -- the previous call raised
            # AttributeError on every save, so `SortedField(type=datetime.time)`
            # never worked despite being on the allowed-type list above.
            #
            # Seconds since midnight is the natural score for a time of day and
            # preserves ordering. tzinfo is deliberately not folded in: a bare
            # time has no date, so normalizing by the offset would wrap past
            # midnight and put 01:00+07:00 *before* 23:00+00:00 on the same
            # clock face. Two times that differ only in tzinfo therefore score
            # the same. Use `datetime.datetime` if the offset must order.
            return (
                field_value.hour * 3600
                + field_value.minute * 60
                + field_value.second
                + field_value.microsecond / 1_000_000
            )
        else:
            raise ValueError(
                f"SortedField {field} received non-numeric value {field_value} type {type(field_value)}."
            )

    @classmethod
    def get_sortedset_db_key(cls, model, field_name, *partition_field_names) -> DB_key:
        """
        Generate the Redis key for this field's Sorted Set index.

        Each sorted field maintains a Redis Sorted Set that maps model instance
        keys to their field values (as scores). This method constructs the key
        for that Sorted Set, incorporating any partition field values.

        Key structure:
            $SortedF:{ModelName}:{field_name}:{partition_value1}:{partition_value2}:...

        The $SortedF prefix identifies this as a sorted field index (following
        Popoto's convention of prefixing special-purpose keys). Partition values
        are appended to create separate sorted sets per partition.

        Args:
            model: The Model class or instance.
            field_name: The name of the sorted field.
            *partition_field_names: Values for partition fields (from partition_by).

        Returns:
            A DB_key instance representing the Redis key for the Sorted Set.
        """
        return cls.get_special_use_field_db_key(
            model, field_name, *partition_field_names
        )

    @classmethod
    def get_partitioned_sortedset_db_key(cls, model_instance, field_name) -> DB_key:
        """
        Build the fully-qualified Sorted Set key including partition values.

        This method reads the actual partition field values from a model instance
        to construct the complete Sorted Set key. It is used during save/delete
        operations where we have a concrete instance with all field values.

        For queries (where we have filter parameters but not an instance), use
        get_sortedset_db_key() with explicit partition values instead.

        Args:
            model_instance: A Model instance with populated field values.
            field_name: The name of the sorted field.

        Returns:
            A DB_key for the partition-specific Sorted Set.

        Raises:
            QueryException: If a required partition field value is missing.
        """
        sortedset_db_key = cls.get_sortedset_db_key(model_instance, field_name)
        # use field names and query values partition_by fields to extend sortedset_db_key
        for partition_field_name in model_instance._meta.fields[
            field_name
        ].partition_by:
            try:
                sortedset_db_key.append(
                    canonical_key_str(getattr(model_instance, partition_field_name))
                )
            except KeyError:
                raise QueryException(
                    f"{field_name} field is partitioned. "
                    f"Queries must also contain a filter for the partitioned fields"
                )
        return sortedset_db_key

    @classmethod
    def count(cls, model_instance: "Model", field_name: str) -> int:
        """Number of members in this field's Sorted Set for one partition.

        The partition is read from ``model_instance`` the same way
        :meth:`get_partitioned_sortedset_db_key` reads it during save and
        delete, so the instance only needs its partition fields populated.
        One ``ZCARD`` round trip; a missing index counts as ``0``.

        Args:
            model_instance: A Model instance carrying the partition values.
            field_name: The name of the sorted field.

        Returns:
            The cardinality of that partition's index.

        Raises:
            QueryException: Propagated from the partition key builder.
        """
        key = cls.get_partitioned_sortedset_db_key(model_instance, field_name).redis_key
        return int(POPOTO_REDIS_DB.zcard(key))

    @classmethod
    def members(
        cls,
        model_instance: "Model",
        field_name: str,
        start: int = 0,
        stop: int = -1,
        reverse: bool = False,
    ) -> list[str]:
        """Members of this field's Sorted Set for one partition, in score order.

        Members are the redis keys of the indexed records, decoded to ``str``.
        ``start``/``stop`` follow Redis ``ZRANGE`` semantics: inclusive rank
        bounds, negative values count from the end, and ``stop < start``
        yields ``[]``. ``reverse=True`` walks from the highest score down
        (``ZREVRANGE``), so ``members(inst, "f", 0, n - 1)`` is the ``n``
        lowest-scored members and the same call with ``reverse=True`` the
        ``n`` highest.

        Args:
            model_instance: A Model instance carrying the partition values.
            field_name: The name of the sorted field.
            start: First rank to include (default ``0``).
            stop: Last rank to include (default ``-1``, the final member).
            reverse: Walk from the highest score when ``True``.

        Returns:
            Redis keys of the matching members as ``str``.

        Raises:
            QueryException: Propagated from the partition key builder.
        """
        key = cls.get_partitioned_sortedset_db_key(model_instance, field_name).redis_key
        if reverse:
            raw_members = POPOTO_REDIS_DB.zrevrange(key, start, stop)
        else:
            raw_members = POPOTO_REDIS_DB.zrange(key, start, stop)
        return [
            raw.decode() if isinstance(raw, bytes) else str(raw) for raw in raw_members
        ]

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: typing.Union[int, float],
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Update the Sorted Set index when a model instance is saved.

        This hook is called by the Model.save() method for each field. For
        sorted fields, it adds or updates the model's entry in the Sorted Set
        index using ZADD. The model's Redis key becomes the member, and the
        field value (converted to numeric) becomes the score.

        Redis ZADD is idempotent for updates: if the member already exists,
        its score is simply updated. This handles both create and update
        operations without needing to check existence first.

        When a partition key changes (e.g., moving an item from category A to
        category B), the sorted set key changes entirely. This method detects
        the change by comparing saved partition field values to current ones
        and removes the member from the old partition's sorted set.

        Pipeline Support:
            When a pipeline is provided, the ZADD command is queued for batch
            execution. This is crucial for atomic saves where the model hash
            and all indexes must be updated together.

        Args:
            model_instance: The Model instance being saved.
            field_name: The name of this sorted field.
            field_value: The value being saved (will be converted to score).
            pipeline: Optional Redis pipeline for batch execution.
            **kwargs: Additional context (unused but accepted for compatibility).

        Returns:
            The pipeline (if provided) or the ZADD result.
        """
        field = model_instance._meta.fields[field_name]
        sortedset_db_key = cls.get_partitioned_sortedset_db_key(
            model_instance, field_name
        )

        # Clean up old partition sorted set if partition key changed
        if field.partition_by and hasattr(model_instance, "_saved_field_values"):
            saved = model_instance._saved_field_values
            if saved:
                # Check if any partition field value changed
                partition_changed = any(
                    saved.get(pf) is not None
                    and saved.get(pf) != getattr(model_instance, pf, None)
                    for pf in field.partition_by
                )
                if partition_changed:
                    # Build the OLD sorted set key using saved partition values
                    old_ss_key = cls.get_sortedset_db_key(model_instance, field_name)
                    for pf in field.partition_by:
                        old_val = saved.get(pf)
                        if old_val is not None:
                            old_ss_key.append(canonical_key_str(old_val))
                    # Use saved_redis_key if available (key may have changed too)
                    old_member = kwargs.get(
                        "saved_redis_key",
                        model_instance.obsolete_redis_key
                        or model_instance.db_key.redis_key,
                    )
                    if isinstance(pipeline, redis.client.Pipeline):
                        pipeline.zrem(old_ss_key.redis_key, old_member)
                    else:
                        POPOTO_REDIS_DB.zrem(old_ss_key.redis_key, old_member)

        sortedset_member = model_instance.db_key.redis_key
        sortedset_score = cls.convert_to_numeric(field, field_value)

        if isinstance(pipeline, redis.client.Pipeline):
            return pipeline.zadd(
                sortedset_db_key.redis_key, {sortedset_member: sortedset_score}
            )
        else:
            return POPOTO_REDIS_DB.zadd(
                sortedset_db_key.redis_key, {sortedset_member: sortedset_score}
            )

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Remove the model instance from the Sorted Set index when deleted.

        This hook ensures index consistency when a model is deleted. It removes
        the model's Redis key from the Sorted Set using ZREM. Without this
        cleanup, deleted models would remain in the index as orphaned entries,
        causing ghost results in range queries.

        When called during key migration (saved_redis_key is provided), the
        partition key values may have changed. In that case, the old sorted set
        key is computed from _saved_field_values rather than the current
        (mutated) instance attributes.

        ZREM is safe to call even if the member doesn't exist (returns 0),
        so this works correctly even if the index is somehow out of sync.

        Args:
            model_instance: The Model instance being deleted.
            field_name: The name of this sorted field.
            field_value: The current field value (used to find the right partition).
            pipeline: Optional Redis pipeline for batch execution.
            **kwargs: Additional context including saved_redis_key for key migrations.

        Returns:
            The pipeline (if provided) or the ZREM result.
        """
        field = model_instance._meta.fields[field_name]
        saved_redis_key = kwargs.get("saved_redis_key")

        # When cleaning up an obsolete key (key migration), use saved partition
        # values to compute the old sorted set key, since the instance's current
        # attribute values may have already been mutated.
        if (
            saved_redis_key
            and field.partition_by
            and hasattr(model_instance, "_saved_field_values")
            and model_instance._saved_field_values
        ):
            saved = model_instance._saved_field_values
            partition_changed = any(
                saved.get(pf) is not None
                and saved.get(pf) != getattr(model_instance, pf, None)
                for pf in field.partition_by
            )
            if partition_changed:
                # Build old sorted set key from saved partition values
                sortedset_db_key = cls.get_sortedset_db_key(model_instance, field_name)
                for pf in field.partition_by:
                    old_val = saved.get(pf)
                    if old_val is not None:
                        sortedset_db_key.append(canonical_key_str(old_val))
                if pipeline:
                    return pipeline.zrem(sortedset_db_key.redis_key, saved_redis_key)
                else:
                    return POPOTO_REDIS_DB.zrem(
                        sortedset_db_key.redis_key, saved_redis_key
                    )

        sortedset_db_key = cls.get_partitioned_sortedset_db_key(
            model_instance, field_name
        )
        # Use saved_redis_key if provided, otherwise fall back to current db_key
        sortedset_member = saved_redis_key or model_instance.db_key.redis_key
        if pipeline:
            return pipeline.zrem(sortedset_db_key.redis_key, sortedset_member)
        else:
            return POPOTO_REDIS_DB.zrem(sortedset_db_key.redis_key, sortedset_member)

    @classmethod
    def filter_query(
        cls,
        model_class: "Model",
        field_name: str,
        *,
        _limit: "int | None" = None,
        _desc: bool = False,
        **query_params,
    ) -> set:
        """
        Execute a range query against the Sorted Set index.

        This is the core query method that translates Django-style filter
        parameters into a Redis ZRANGEBYSCORE command. It returns a set of
        Redis keys for model instances whose field values fall within the
        specified range.

        Query Parameter Parsing:
            The method parses __gt, __gte, __lt, __lte suffixes to determine
            the range bounds. Redis uses special syntax for exclusive bounds:
            a leading '(' makes the bound exclusive. For example:
            - price__gte=10 becomes min="10" (inclusive)
            - price__gt=10 becomes min="(10" (exclusive)

        Partition Handling:
            For partitioned sorted fields (those with partition_by), the query
            parameters MUST include values for all partition fields. This is
            required because each partition has its own Sorted Set, and we
            need to know which one to query.

        Performance:
            ZRANGEBYSCORE is O(log(N)+M) where N is the set size and M is the
            number of results. For large datasets, partitioning via partition_by
            reduces N significantly, improving query performance.

        Args:
            model_class: The Model class to query.
            field_name: The name of the sorted field being filtered.
            _limit: When set, ask Redis for at most this many members instead of
                the whole range. Callers must establish that no predicate outside
                this field can eliminate rows later, because a bound applied here
                lands before hydration and before any client-side filtering.
                ``Query.filter_for_keys_set`` owns that decision.
            _desc: Read the range highest-score-first. Redis swaps the bound
                order for a reverse read, which this method handles.
            **query_params: Filter parameters (e.g., price__gte=10, price__lt=100).

        Returns:
            A list of Redis keys (as bytes) for matching model instances, in
            score order. The caller intersects it with results from other field
            filters.

        Raises:
            QueryException: If a partitioned field is queried without providing
                values for all partition fields.

        Example:
            # Direct call (usually called by Query.filter_for_keys_set)
            keys = SortedFieldMixin.filter_query(
                Product, 'price',
                price__gte=10.0, price__lt=50.0, category='electronics'
            )
        """
        value_range = {"min": "-inf", "max": "+inf"}

        for query_param, query_value in query_params.items():
            if field_name not in query_param:
                continue

            if "__between" in query_param:
                if not isinstance(query_value, (tuple, list)) or len(query_value) != 2:
                    raise QueryException(
                        f"{field_name}__between requires a tuple or list of exactly "
                        f"2 elements (low, high), got {query_value!r}"
                    )
                low, high = query_value
                field_obj = model_class._meta.fields[field_name]
                numeric_low = cls.convert_to_numeric(field_obj, low)
                numeric_high = cls.convert_to_numeric(field_obj, high)
                value_range["min"] = f"{numeric_low}"
                value_range["max"] = f"{numeric_high}"
                continue

            numeric_value = cls.convert_to_numeric(
                model_class._meta.fields[field_name], query_value
            )
            if "__gt" in query_param:
                inclusive = query_param.split("__gt")[1]
                value_range["min"] = f"{'' if inclusive == 'e' else '('}{numeric_value}"
            elif "__lt" in query_param:
                inclusive = query_param.split("__lt")[1]
                value_range["max"] = f"{'' if inclusive == 'e' else '('}{numeric_value}"
            elif query_param == field_name:
                # Exact match: set both min and max to the same value (inclusive)
                value_range["min"] = f"{numeric_value}"
                value_range["max"] = f"{numeric_value}"
            else:
                pass  # this is just a mixin, another subclass may have valid query params

        try:
            # use field names and query values partition_by fields to extend sortedset_db_key
            sortedset_db_key = cls.get_sortedset_db_key(
                model_class,
                field_name,
                *[
                    canonical_key_str(query_params[partition_field_name])
                    for partition_field_name in model_class._meta.fields[
                        field_name
                    ].partition_by
                ],
            )
        except KeyError:
            raise QueryException(
                f"{field_name} field is sorted on "
                f"{', '.join(model_class._meta.fields[field_name].partition_by)}. "
                f"Query filter must also specify a value for "
                f"{', '.join(model_class._meta.fields[field_name].partition_by)}"
            )

        bounded = isinstance(_limit, int) and _limit > 0
        if bounded and _desc:
            # ZREVRANGEBYSCORE takes the bounds high-then-low.
            redis_db_keys_list = POPOTO_REDIS_DB.zrevrangebyscore(
                sortedset_db_key.redis_key,
                value_range["max"],
                value_range["min"],
                start=0,
                num=_limit,
            )
        elif bounded:
            redis_db_keys_list = POPOTO_REDIS_DB.zrangebyscore(
                sortedset_db_key.redis_key,
                value_range["min"],
                value_range["max"],
                start=0,
                num=_limit,
            )
        elif _desc:
            redis_db_keys_list = POPOTO_REDIS_DB.zrevrangebyscore(
                sortedset_db_key.redis_key, value_range["max"], value_range["min"]
            )
        else:
            redis_db_keys_list = POPOTO_REDIS_DB.zrangebyscore(
                sortedset_db_key.redis_key, value_range["min"], value_range["max"]
            )
        return list(redis_db_keys_list)
