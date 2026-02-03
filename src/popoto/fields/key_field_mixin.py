"""
Key Field Mixin - Identity and Indexing for Popoto Models
==========================================================

This module provides the KeyFieldMixin class, which is the foundation for all
identity-related field behavior in Popoto. Key fields serve two critical purposes:

1. **Identity**: Key field values are concatenated to form the Redis key where
   the model instance is stored. Together, all key fields enforce a unique_together
   constraint - no two instances can have identical values across all key fields.

2. **Indexing**: Key fields maintain Redis Sets that enable efficient queries.
   For each unique key field value, a Set tracks all model instances with that value,
   enabling O(1) lookups by key field value.

Design Philosophy
-----------------
Unlike traditional ORMs where primary keys are typically auto-incremented integers,
Popoto embraces Redis's key-value nature by making the primary key a composite of
meaningful field values. This allows direct Redis key construction from known values,
bypassing index lookups entirely for common access patterns.

For example, a User model with `email` as a KeyField stores instances at keys like
`User:alice@example.com`. Retrieving a user by email is a single Redis HGETALL
operation rather than an index lookup followed by a fetch.

The trade-off is that key field values become part of the storage key itself,
so they cannot be changed after creation without deleting and recreating the instance.

Usage Examples
--------------
    from popoto import Model
    from popoto.fields import KeyField, UniqueKeyField

    class Product(Model):
        # Category enables filtering: Product.query.filter(category="electronics")
        category = KeyField(type=str)
        # SKU is unique within category, together they form the Redis key
        sku = KeyField(type=str)
        name = Field(type=str)

    # Stored at Redis key: "Product:electronics:ABC123"
    product = Product(category="electronics", sku="ABC123", name="Widget")
    product.save()

    # Efficient queries using key field indexes
    electronics = Product.query.filter(category="electronics")
    product = Product.query.get(category="electronics", sku="ABC123")

See Also
--------
- AutoFieldMixin: For auto-generated unique identifiers
- UniqueFieldMixin: For enforcing uniqueness on a single field
- DB_key: The class that manages Redis key construction
"""

from decimal import Decimal
from datetime import date, datetime, time
import redis.client
import logging
from ..models.db_key import DB_key

logger = logging.getLogger("POPOTO.KeyFieldMixin")

from ..exceptions import ModelException
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

# Key fields must be serializable to strings for Redis key construction.
# Complex types like dict, list, and set are excluded because they don't
# have a canonical string representation suitable for key construction.
VALID_KEYFIELD_TYPES = [
    int,
    float,
    Decimal,
    str,
    bool,
    date,
    datetime,
    time,
]


class KeyFieldMixin:
    """
    Mixin that transforms a Field into a key field for model identity and indexing.

    KeyFieldMixin is the core abstraction that enables Popoto's Django-like query
    interface over Redis. It provides two interconnected capabilities:

    **Identity (Primary Key Construction)**

    Key field values are concatenated with the model class name to form the Redis
    key where the instance is stored. For a model with key fields `region` and `user_id`,
    an instance might be stored at `Account:us-west:12345`.

    All key fields together enforce a unique_together constraint - attempting to save
    a second instance with identical key field values will overwrite the first.

    **Secondary Indexing**

    For each key field value, KeyFieldMixin maintains a Redis Set containing the
    Redis keys of all instances with that value. This enables efficient filtering:

        # Behind the scenes, this queries the Set at "$KeyF:Account:region:us-west"
        # which contains all Account redis keys where region="us-west"
        Account.query.filter(region="us-west")

    The Set-based indexing enables query patterns like exact match, __in (OR queries),
    __startswith, __endswith, and __isnull using Redis's native Set intersection.

    **Mixin Architecture**

    This class is designed to be mixed with the base Field class. It uses cooperative
    multiple inheritance (super().__init__) to work alongside other mixins like
    AutoFieldMixin and UniqueFieldMixin. The Method Resolution Order (MRO) ensures
    proper initialization of all mixin defaults.

    Attributes:
        key (bool): Always True for key fields. Used by ModelOptions to identify
            which fields participate in primary key construction.
        max_length (int): Maximum string length for key field values. Defaults to 128.
            Shorter than regular Field's 1024 because key field values become part
            of Redis keys, where extremely long keys impact performance.

    See Also:
        AutoFieldMixin: Generates UUID values for automatic unique identification.
        UniqueFieldMixin: Enforces single-field uniqueness constraints.
        shortcuts.KeyField: The concrete class combining this mixin with Field.
    """

    key: bool = True
    max_length: int = None

    def __init__(self, **kwargs):
        """
        Initialize KeyFieldMixin defaults and validate the field type.

        Calls super().__init__() to support cooperative multiple inheritance with
        other mixins. Updates field_defaults dict so that field introspection
        can distinguish key field defaults from overridden values.

        Raises:
            ModelException: If the field type is not in VALID_KEYFIELD_TYPES.
                Complex types like dict and list cannot be key fields because
                they don't have canonical string representations for Redis keys.
        """
        super().__init__(**kwargs)
        keyfield_defaults = {
            "key": True,
            "max_length": None,
        }
        self.field_defaults.update(keyfield_defaults)
        # set field options, let kwargs override
        for k, v in keyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
        if self.key and self.type not in VALID_KEYFIELD_TYPES:
            raise ModelException(f"{self.type} is not a valid KeyField type")

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """
        Validate that a value is acceptable for this key field.

        Delegates to the parent Field's validation (type checking, null handling,
        max_length). Key fields don't add additional validation constraints beyond
        the type restriction enforced at __init__ time.

        This method is called during model.save() to ensure data integrity before
        persisting to Redis.

        Args:
            field: The Field instance being validated.
            value: The value to validate.
            null_check: If False, allows None values regardless of field.null setting.
                Used internally during partial updates.

        Returns:
            bool: True if the value is valid for this field, False otherwise.
        """
        if not super().is_valid(field, value, null_check):
            return False
        return True

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Maintain the secondary index Set when a model instance is saved.

        This hook is called by Model.save() for each field. For key fields,
        it adds the model instance's Redis key to the index Set for this
        field's value, enabling queries like `Model.query.filter(field=value)`.

        **Index Structure**

        The index Set key follows the pattern: `$KeyF:ModelName:field_name:value`
        For example, saving a Product with category="electronics" adds the
        Product's Redis key to the Set at `$KeyF:Product:category:electronics`.

        **AutoField Exception**

        Auto-generated key fields (AutoKeyField) skip index maintenance because:
        1. Each auto value is unique by design, so the Set would contain exactly one member
        2. Queries on auto fields use direct key construction instead of Set lookups

        **Pipeline Support**

        Accepts an optional Redis pipeline for batching multiple operations.
        When saving a model with multiple key fields, all SADD operations are
        batched into a single round-trip to Redis.

        Args:
            model_instance: The Model instance being saved.
            field_name: The name of this field on the model.
            field_value: The value being saved for this field.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The pipeline (if provided) or the SADD result.
        """
        if model_instance._meta.fields[field_name].auto:
            return pipeline if pipeline else None

        unique_set_key = DB_key(
            cls.get_special_use_field_db_key(model_instance, field_name), field_value
        )
        if pipeline:
            return pipeline.sadd(
                unique_set_key.redis_key, model_instance.db_key.redis_key
            )
        else:
            return POPOTO_REDIS_DB.sadd(
                unique_set_key.redis_key, model_instance.db_key.redis_key
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
        Clean up the secondary index Set when a model instance is deleted.

        The counterpart to on_save(), this removes the model instance's Redis key
        from the index Set for this field's value. This ensures that queries don't
        return stale references to deleted instances.

        **Index Cleanup**

        For a Product with category="electronics" being deleted, this removes the
        Product's Redis key from the Set at `$KeyF:Product:category:electronics`.

        Note: This only removes the instance from the Set - it does not delete
        the Set itself even if it becomes empty. Empty Sets are cleaned up lazily
        by Query.keys(clean=True) during maintenance operations.

        **AutoField Exception**

        Like on_save(), auto-generated key fields skip index cleanup because
        they don't maintain index Sets.

        Args:
            model_instance: The Model instance being deleted.
            field_name: The name of this field on the model.
            field_value: The value stored for this field.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The pipeline (if provided) or the SREM result.
        """
        if model_instance._meta.fields[field_name].auto:
            return pipeline if pipeline else None

        unique_set_key = DB_key(
            cls.get_special_use_field_db_key(model_instance, field_name), field_value
        )
        # Use saved_redis_key if provided, otherwise fall back to current db_key
        member_key = kwargs.get("saved_redis_key", model_instance.db_key.redis_key)
        if pipeline:
            return pipeline.srem(unique_set_key.redis_key, member_key)
        else:
            return POPOTO_REDIS_DB.srem(unique_set_key.redis_key, member_key)

    def get_filter_query_params(self, field_name: str) -> set:
        """
        Return the set of valid query parameter names for filtering on this field.

        This method enables Django-style query syntax like `Model.query.filter(name="X")`
        and `Model.query.filter(name__startswith="A")`. The Query class uses this to
        validate filter parameters and route them to the appropriate filter_query method.

        **Supported Query Lookups**

        - `field_name`: Exact match using Set lookup (O(1) via SMEMBERS)
        - `field_name__in`: OR query across multiple values (Set UNION)
        - `field_name__isnull`: Match instances where field is/isn't None
        - `field_name__startswith`: Pattern match using Redis KEYS (O(N), use sparingly)
        - `field_name__endswith`: Pattern match using Redis KEYS (O(N), use sparingly)
        - `field_name__contains`: Pattern match - currently not implemented in filter_query

        **Performance Note**

        Exact match and __in queries use the Set-based index for O(1) lookups.
        Pattern queries (__startswith, __endswith) fall back to Redis KEYS command,
        which scans all keys and should be avoided in production with large datasets.

        Args:
            field_name: The name of this field on the model.

        Returns:
            set: Valid query parameter strings that filter_query can process.
        """
        return (
            super()
            .get_filter_query_params(field_name)
            .union(
                {
                    f"{field_name}",  # takes a str, exact match :x:
                    f"{field_name}__isnull",  # Takes boolean, to match for [^None]
                    f"{field_name}__contains",  # takes a str, matches :*x*:
                    f"{field_name}__startswith",  # takes a str, matches :x*:
                    f"{field_name}__endswith",  # takes a str, matches :*x:
                    f"{field_name}__in",  # takes a list, returns any matches
                }
            )
        )

    @classmethod
    def filter_query(cls, model: "Model", field_name: str, **query_params) -> set:
        """
        Execute a filter query on this key field and return matching Redis keys.

        This is the workhorse method that translates Django-style query parameters
        into Redis operations. It's called by Query.filter_for_keys_set() after
        validating that the query parameters belong to this field.

        **Query Strategy**

        The method uses two complementary strategies:

        1. **Set-based lookups** (exact match, __in, __isnull=True): O(1) operations
           using the secondary index Sets maintained by on_save(). These are efficient
           and preferred.

        2. **Pattern-based lookups** (__startswith, __endswith, __isnull=False): Falls
           back to Redis KEYS command with glob patterns. This scans all keys matching
           the pattern and should be avoided with large datasets.

        **Multiple Filters**

        When multiple query parameters are provided (e.g., filter(status="active", type="premium")),
        each parameter produces a set of matching keys. The final result is the intersection
        of all sets, implementing AND semantics.

        **Key Position Calculation**

        For pattern queries, the method must construct a Redis key pattern like
        `ModelName:*:value:*` where the value appears at the correct position.
        It calculates this position from the field's order in the model's key fields.

        **__in Query Optimization**

        The __in lookup (e.g., filter(status__in=["active", "pending"])) uses a nested
        pipeline to fetch all relevant Sets in one round-trip, then performs a Python
        set union to implement OR semantics.

        Args:
            model: The Model class being queried.
            field_name: The name of this field on the model.
            **query_params: Dict mapping query parameter names to values.
                Example: {"status": "active"} or {"status__in": ["active", "pending"]}

        Returns:
            set: Redis keys (as bytes) of matching model instances. These keys can be
                passed to Query.get_many_objects() to fetch the actual instances.

        Raises:
            QueryException: If __isnull receives a non-boolean value.
        """

        keys_lists_to_intersect = list()
        (
            db_key_length,
            field_key_position,
        ) = model._meta.db_key_length, model._meta.get_db_key_index_position(field_name)
        num_keys_before = field_key_position
        num_keys_after = db_key_length - field_key_position - 1

        pipeline = POPOTO_REDIS_DB.pipeline()

        def get_key_pattern(query_value_pattern):
            """Build a Redis KEYS pattern with the value at the correct position."""
            key_pattern = model._meta.db_class_key.redis_key + ":"
            key_pattern += "*:" * (num_keys_before - 1)
            key_pattern += query_value_pattern
            key_pattern += ":*" * num_keys_after
            return key_pattern

        redis_set_key_prefix = model._meta.fields[
            field_name
        ].get_special_use_field_db_key(model, field_name)

        for query_param, query_value in query_params.items():

            if query_param.endswith("__in"):
                pipeline_2 = POPOTO_REDIS_DB.pipeline()
                for query_value_elem in query_value:
                    pipeline_2 = pipeline_2.smembers(
                        DB_key(redis_set_key_prefix, query_value_elem).redis_key
                    )
                keys_lists_to_union = pipeline_2.execute()
                keys_lists_to_intersect.append(
                    set.union(*[set(key_list) for key_list in keys_lists_to_union])
                )

            else:
                if query_param == f"{field_name}":
                    if model._meta.fields[field_name].auto:
                        # todo: refactor or show warning or depricate ability
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.keys(get_key_pattern(query_value))
                        )
                    else:
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.smembers(
                                DB_key(redis_set_key_prefix, query_value).redis_key
                            )
                        )

                elif query_param.endswith("__isnull"):
                    if query_value is True:
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.smembers(
                                DB_key(redis_set_key_prefix, None).redis_key
                            )
                        )
                    elif query_value is False:
                        pipeline = pipeline.keys(
                            get_key_pattern(f"[^None]")
                        )  # todo: refactor
                    else:
                        raise QueryException(
                            f"{query_param} filter must be True or False"
                        )

                elif query_param.endswith("__startswith"):
                    pipeline = pipeline.keys(
                        get_key_pattern(f"{DB_key.clean(query_value)}*")
                    )  # todo: refactor
                elif query_param.endswith("__endswith"):
                    pipeline = pipeline.keys(
                        get_key_pattern(f"*{DB_key.clean(query_value)}")
                    )  # todo: refactor

            # todo: refactor to use HSCAN or sets, https://redis.io/commands/keys
            # https://redis-py.readthedocs.io/en/stable/index.html#redis.Redis.hscan_iter

        keys_lists_to_intersect += pipeline.execute()
        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(
                *[set(key_list) for key_list in keys_lists_to_intersect]
            )
        return set()
