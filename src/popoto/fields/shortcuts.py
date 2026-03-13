"""
Shortcut Field Classes for Popoto Redis ORM.

This module provides convenient, type-safe field classes that simplify model
definitions by pre-configuring the base Field class with common Python types.
Rather than writing `Field(type=int)`, users can write `IntField()`.

Design Philosophy:
    Popoto follows Django's ORM conventions to provide a familiar API for
    Python developers. These shortcut fields serve three purposes:

    1. Readability: `price = FloatField()` clearly communicates intent
    2. Type Safety: Prevents accidental type mismatches at field definition
    3. IDE Support: Better autocomplete and type hints than generic Field

Architecture:
    The shortcuts are organized into three categories:

    - Type Fields (IntField, StringField, etc.): Simple type wrappers that
      pre-set the `type` parameter. These are thin convenience layers.

    - Key Fields (KeyField, UniqueKeyField, AutoKeyField): Control how model
      instances are identified and indexed in Redis. These determine the
      Redis key structure and enable efficient lookups.

    - Sorted Fields (SortedField, SortedKeyField): Enable range queries by
      maintaining Redis Sorted Sets alongside model data.

Redis Storage Considerations:
    All Python types are serialized to bytes via msgpack before storage.
    The type parameter primarily affects validation and deserialization,
    not storage format. Complex types (list, dict, set) serialize correctly
    but cannot be used as KeyFields since Redis keys must be simple strings.

Example:
    class Product(Model):
        sku = UniqueKeyField()           # Primary identifier
        name = StringField()             # Basic string storage
        price = FloatField()             # Numeric, could use SortedField for range queries
        tags = ListField(default=[])     # Complex type storage
        created = DateField()            # Temporal data

See Also:
    - field.py: Base Field class implementation
    - key_field_mixin.py: KeyField indexing behavior
    - sorted_field_mixin.py: Range query support via Sorted Sets
"""

import msgpack

from .field import Field
from .key_field_mixin import KeyFieldMixin
from .auto_field_mixin import AutoFieldMixin
from .sorted_field_mixin import SortedFieldMixin
from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB


class IntField(Field):
    """A Field that stores ``int`` values.

    Use IntField when you need whole number storage with automatic type
    validation. For range queries (e.g., "find products with quantity > 10"),
    consider SortedField(type=int) instead.

    Example:
        class Inventory(Model):
            product_id = UniqueKeyField()
            quantity = IntField(default=0)
            reorder_threshold = IntField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize an IntField with integer type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value for new instances
        """
        kwargs["type"] = int
        super().__init__(**kwargs)


class FloatField(Field):
    """A Field that stores ``float`` values.

    Use FloatField for decimal values where exact precision is not critical.
    For financial data or when precision matters, prefer DecimalField.
    For range queries, consider SortedField(type=float).

    Example:
        class Sensor(Model):
            sensor_id = UniqueKeyField()
            temperature = FloatField()
            humidity = FloatField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a FloatField with float type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value for new instances
        """
        kwargs["type"] = float
        super().__init__(**kwargs)


class DecimalField(Field):
    """A Field that stores ``Decimal`` values.

    Use DecimalField for financial data, currency, or any values where
    floating-point precision errors are unacceptable. The Decimal type
    preserves exact decimal representation through serialization.

    Note: DecimalField can be used with SortedField for range queries,
    though the underlying Redis score uses float conversion.

    Example:
        class Transaction(Model):
            tx_id = UniqueKeyField()
            amount = DecimalField(null=False)
            tax_rate = DecimalField(default=Decimal("0.0825"))
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a DecimalField with Decimal type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use Decimal objects)
        """
        from decimal import Decimal

        kwargs["type"] = Decimal
        super().__init__(**kwargs)


class StringField(Field):
    """A Field that stores ``str`` values (same as the base ``Field`` default).

    StringField is functionally equivalent to Field() since str is the
    default type. It exists for explicitness and consistency with other
    typed fields.

    For strings that identify model instances, use KeyField or UniqueKeyField
    instead, which enable efficient lookups and query filtering.

    Example:
        class Article(Model):
            slug = UniqueKeyField()           # Identifier - use KeyField
            title = StringField(max_length=200)
            content = StringField()           # No practical limit
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a StringField with string type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - max_length (int): Maximum string length. Default: 1024
                - default: Default value for new instances
        """
        kwargs["type"] = str
        super().__init__(**kwargs)


class BooleanField(Field):
    """A Field that stores ``bool`` values.

    BooleanField stores Python booleans with full type validation.
    Note that by default null=True, so the field can hold three states:
    True, False, or None. Set null=False for strict two-state booleans.

    Example:
        class Feature(Model):
            name = UniqueKeyField()
            enabled = BooleanField(default=False, null=False)
            deprecated = BooleanField(default=False)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a BooleanField with boolean type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (True or False)
        """
        kwargs["type"] = bool
        super().__init__(**kwargs)


class BytesField(Field):
    """A Field that stores ``bytes`` values.

    Use BytesField for binary content like images, encoded data, or any
    content that should not be interpreted as text. The bytes are stored
    as-is via msgpack serialization.

    Example:
        class BinaryCache(Model):
            key = UniqueKeyField()
            data = BytesField()
            checksum = BytesField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a BytesField with bytes type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (bytes object)
        """
        kwargs["type"] = bytes
        super().__init__(**kwargs)


class CappedListProxy:
    """A list-like wrapper for capped ListField values.

    Provides a push() method for direct Redis LPUSH + LTRIM operations
    without requiring a full read/write cycle. Behaves like a regular
    list for all other operations.

    The proxy holds a reference to the model instance, field name, and
    max_length to compute the correct Redis list key and enforce the cap.

    Attributes:
        _data: The underlying Python list.
        _model_instance: The Model instance this proxy belongs to.
        _field_name: The name of the ListField on the model.
        _max_length: Maximum number of items to keep in the list.
    """

    def __init__(
        self, data=None, model_instance=None, field_name=None, max_length=None
    ):
        self._data = list(data) if data else []
        self._model_instance = model_instance
        self._field_name = field_name
        self._max_length = max_length

    def push(self, value):
        """Prepend a value to the capped list using LPUSH + LTRIM.

        This method writes directly to Redis without requiring a full
        model save. The value is serialized with msgpack individually.

        The Redis write and local state update are kept in sync: if the
        Redis pipeline succeeds but the local update fails, the local
        state is rolled back to match what Redis held before the push,
        and the exception is re-raised so the caller knows something
        went wrong.

        Args:
            value: The value to prepend. Can be any msgpack-serializable type.

        Raises:
            ModelException: If the model instance has not been saved yet
                (no _redis_key available).
        """
        if self._model_instance is None or self._model_instance._redis_key is None:
            raise ModelException(
                "Cannot push() on an unsaved model instance. Call save() first."
            )

        list_key = f"{self._model_instance._redis_key}::{self._field_name}"
        encoded_value = _encode_list_element(value)

        # Snapshot local state before mutation so we can roll back
        previous_data = list(self._data)

        pipe = POPOTO_REDIS_DB.pipeline()
        pipe.lpush(list_key, encoded_value)
        pipe.ltrim(list_key, 0, self._max_length - 1)
        pipe.execute()

        # Update local data; roll back if anything goes wrong so that
        # local state does not silently diverge from Redis.
        try:
            self._data.insert(0, value)
            if len(self._data) > self._max_length:
                self._data = self._data[: self._max_length]
        except Exception:
            self._data = previous_data
            raise

    # List-like interface methods
    def __len__(self):
        return len(self._data)

    def __getitem__(self, index):
        return self._data[index]

    def __setitem__(self, index, value):
        self._data[index] = value

    def __iter__(self):
        return iter(self._data)

    def __eq__(self, other):
        if isinstance(other, CappedListProxy):
            return self._data == other._data
        if isinstance(other, list):
            return self._data == other
        return NotImplemented

    def __repr__(self):
        return f"CappedListProxy({self._data!r})"

    def __bool__(self):
        return bool(self._data)

    def append(self, value):
        self._data.append(value)

    def extend(self, values):
        self._data.extend(values)


def _encode_list_element(value):
    """Encode a single list element for Redis storage using msgpack.

    Handles custom types (Decimal, tuple, set, datetime, etc.) using
    the same TYPE_ENCODER_DECODERS registry as the model encoding system.

    Args:
        value: Any msgpack-serializable value, including custom Popoto types.

    Returns:
        bytes: The msgpack-encoded value.
    """
    from ..models.encoding import TYPE_ENCODER_DECODERS

    if value is not None and type(value) in TYPE_ENCODER_DECODERS:
        encoded = TYPE_ENCODER_DECODERS[type(value)].encoder(value)
        return msgpack.packb(encoded)
    return msgpack.packb(value)


def _decode_list_element(raw_bytes):
    """Decode a single msgpack-encoded list element from Redis.

    Handles custom types via decode_custom_types.

    Args:
        raw_bytes: msgpack-encoded bytes from Redis.

    Returns:
        The decoded Python value.
    """
    from ..models.encoding import decode_custom_types

    return decode_custom_types(msgpack.unpackb(raw_bytes, strict_map_key=False))


def _wrap_capped_field(model_instance, field_name, data, max_length):
    """Wrap a capped list field value in a CappedListProxy on the model instance.

    Args:
        model_instance: The model instance.
        field_name: The field name.
        data: The list data.
        max_length: The max_length setting.
    """
    proxy = CappedListProxy(
        data=data,
        model_instance=model_instance,
        field_name=field_name,
        max_length=max_length,
    )
    setattr(model_instance, field_name, proxy)


class ListField(Field):
    """A Field that stores ``list`` values.

    ListField stores ordered collections that serialize via msgpack. The list
    contents can be any msgpack-serializable types.

    When ``max_length`` is set, the list is stored in a separate Redis list
    key (``{model_db_key}::field_name``) instead of in the model hash. This
    enables efficient ``push()`` operations using LPUSH + LTRIM without
    reading the full list.

    When ``max_length`` is not set, the list is stored atomically in the
    model's Redis hash as before (backward compatible).

    Note: ListField cannot be used as a KeyField since lists cannot be
    converted to Redis key strings.

    Example:
        class ShoppingCart(Model):
            user_id = UniqueKeyField()
            items = ListField(default=[])
            recently_viewed = ListField(null=True)

        class EventLog(Model):
            session_id = UniqueKeyField()
            events = ListField(max_length=100)  # Capped at 100 items
    """

    # Track whether this is a capped list field
    _capped = False

    def __init__(self, *args, **kwargs):
        """
        Initialize a ListField with list type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use list, e.g., default=[])
                - max_length (int): Maximum number of items. When set,
                    stores data in a separate Redis list key and enables
                    the push() method for atomic append + trim.
        """
        self._capped = kwargs.get("max_length") is not None
        kwargs["type"] = list
        super().__init__(**kwargs)

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """Validate list field values, accepting CappedListProxy as valid."""
        # CappedListProxy should pass validation as if it were a list
        if isinstance(value, CappedListProxy):
            return True
        return super().is_valid(field, value, null_check, **kwargs)

    def format_value_pre_save(self, field_value, **kwargs):
        """For capped lists, return the value unchanged.

        The actual data is written to a separate Redis list key in on_save().
        The value is excluded from the hash in encode_popoto_model_obj().
        """
        return field_value

    @classmethod
    def on_save(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Write capped list data to a separate Redis list key.

        For capped lists (max_length is set), this replaces the list at the
        Redis list key using DEL + RPUSH in a pipeline. For uncapped lists,
        delegates to the base Field.on_save() (no-op).
        """
        field = model_instance._meta.fields[field_name]
        if not field._capped:
            return super(ListField, cls).on_save(
                model_instance, field_name, field_value, pipeline, **kwargs
            )

        list_key = f"{model_instance.db_key.redis_key}::{field_name}"

        # Get the actual list data (may be a CappedListProxy or plain list)
        if isinstance(field_value, CappedListProxy):
            data = field_value._data
        elif isinstance(field_value, list):
            data = field_value
        else:
            data = []

        # Encode each element individually
        encoded_values = [_encode_list_element(v) for v in data]

        if hasattr(pipeline, "execute"):
            pipeline.delete(list_key)
            if encoded_values:
                pipeline.rpush(list_key, *encoded_values)
            # Wrap the field value in a CappedListProxy on the instance
            _wrap_capped_field(model_instance, field_name, data, field.max_length)
            return pipeline
        else:
            internal_pipe = POPOTO_REDIS_DB.pipeline()
            internal_pipe.delete(list_key)
            if encoded_values:
                internal_pipe.rpush(list_key, *encoded_values)
            internal_pipe.execute()
            # Wrap the field value in a CappedListProxy on the instance
            _wrap_capped_field(model_instance, field_name, data, field.max_length)
            return None

    @classmethod
    def on_delete(
        cls,
        model_instance,
        field_name,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """Delete the Redis list key when the model is deleted.

        For capped lists, removes the separate Redis list key. For uncapped
        lists, delegates to the base Field.on_delete() (no-op).
        """
        field = model_instance._meta.fields[field_name]
        if not field._capped:
            return super(ListField, cls).on_delete(
                model_instance, field_name, field_value, pipeline, **kwargs
            )

        # Use saved_redis_key if available (for key migration scenarios)
        redis_key = kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
        list_key = f"{redis_key}::{field_name}"

        if pipeline:
            return pipeline.delete(list_key)
        else:
            POPOTO_REDIS_DB.delete(list_key)
            return None


class DictField(Field):
    """A Field that stores ``dict`` values.

    DictField stores key-value mappings via msgpack serialization. Useful for
    flexible schema-less data, metadata, or configuration. The entire dict is
    stored atomically with the model.

    Note: DictField cannot be used as a KeyField. For Redis Hash operations,
    consider using the Redis client directly or separate fields.

    Example:
        class UserPreferences(Model):
            user_id = UniqueKeyField()
            settings = DictField(default={})
            metadata = DictField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a DictField with dict type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use dict, e.g., default={})
        """
        kwargs["type"] = dict
        super().__init__(**kwargs)


class SetField(Field):
    """A Field that stores ``set`` values.

    SetField stores unordered collections of unique values. The set is
    serialized via msgpack and stored atomically with the model instance.
    This is distinct from Redis Sets - the data is embedded in the model.

    Note: SetField cannot be used as a KeyField. For Redis Set operations
    (unions, intersections), use the Redis client directly.

    Example:
        class Article(Model):
            slug = UniqueKeyField()
            tags = SetField(default=set())
            categories = SetField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a SetField with set type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use set, e.g., default=set())
        """
        kwargs["type"] = set
        super().__init__(**kwargs)


class TupleField(Field):
    """A Field that stores ``tuple`` values.

    TupleField stores immutable ordered sequences. The tuple is serialized
    via msgpack and stored atomically. Useful for fixed-structure data like
    coordinates (x, y) or version numbers (major, minor, patch).

    Note: TupleField cannot be used as a KeyField. For geographic coordinates,
    consider GeoField which provides spatial queries.

    Example:
        class Release(Model):
            name = UniqueKeyField()
            version = TupleField()  # (major, minor, patch)
            coordinates = TupleField(null=True)  # (lat, lng)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a TupleField with tuple type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use tuple)
        """
        kwargs["type"] = tuple
        super().__init__(**kwargs)


class DateField(Field):
    """A Field that stores ``datetime.date`` values.

    DateField stores Python date objects. Dates are serialized and can be
    filtered in queries. For date range queries like "find all events after
    2024-01-01", use SortedField(type=date) instead.

    For datetime values with time components, use DatetimeField (defined in
    datetime_field.py with timezone handling).

    Example:
        class Event(Model):
            event_id = UniqueKeyField()
            event_date = DateField(null=False)
            registration_deadline = DateField(null=True)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a DateField with date type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use date object)
        """
        from datetime import date

        kwargs["type"] = date
        super().__init__(**kwargs)


class TimeField(Field):
    """A Field that stores ``datetime.time`` values.

    TimeField stores Python time objects representing time of day. Useful for
    schedules, recurring events, or time-only data. For full datetime values,
    use DatetimeField instead.

    Example:
        class Store(Model):
            store_id = UniqueKeyField()
            opening_time = TimeField(null=False)
            closing_time = TimeField(null=False)
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a TimeField with time type constraint.

        Args:
            **kwargs: Passed to Field.__init__. Common options:
                - null (bool): Allow None values. Default: True
                - default: Default value (use time object)
        """
        from datetime import time

        kwargs["type"] = time
        super().__init__(**kwargs)


class KeyField(KeyFieldMixin, Field):
    """A field that forms part of the model's Redis key and enables query filtering.

    KeyField is the foundation of Popoto's identity and query system. When you
    define KeyFields on a model, their values become part of the Redis key used
    to store that instance. This enables:

    1. Direct lookups: Model.query.get(field=value) uses the key directly
    2. Pattern matching: Queries can use startswith, endswith, contains
    3. Set-based filtering: Each unique value maintains a Redis Set of instance keys

    All KeyField values together enforce a unique-together constraint.
    Supports filter lookups: exact, ``__isnull``, ``__contains``,
    ``__startswith``, ``__endswith``, ``__in``.

    Design Decision:
        KeyField allows null values and duplicate single-field values by default.
        Use UniqueKeyField for strict uniqueness, or AutoKeyField for auto-generated IDs.

    Example:
        class BandMember(Model):
            band = KeyField()     # Part of composite key
            role = KeyField()     # Part of composite key
            name = StringField()

        # Redis key: "BandMember:BLACKPINK:vocalist"
        lisa = BandMember(band="BLACKPINK", role="vocalist", name="Lisa")

        # Query filtering uses the KeyField's Redis Set index
        BandMember.query.filter(band="BLACKPINK")  # Returns all BLACKPINK members
        BandMember.query.filter(band__startswith="BLACK")  # Pattern matching

    See Also:
        - UniqueKeyField: Enforces single-field uniqueness
        - AutoKeyField: Auto-generates unique identifiers
        - KeyFieldMixin: Implementation of indexing behavior
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a KeyField that participates in model identity.

        Args:
            **kwargs: Passed through mixin chain. Common options:
                - unique (bool): Enforce uniqueness for this field alone. Default: False
                - null (bool): Allow None values. Default: True
                - max_length (int): Maximum string length. Default: 128
                - type: Field type. Must be a simple type (int, str, etc.), not list/dict
        """
        kwargs["key"] = True
        super().__init__(**kwargs)


class UniqueKeyField(KeyField):
    """A KeyField with a per-value uniqueness constraint. Cannot be null.

    UniqueKeyField guarantees that no two model instances can have the same
    value for this field. Unlike KeyField (which only enforces unique_together
    across all key fields), UniqueKeyField enforces uniqueness on this single
    field alone.

    Constraints:
        - Cannot be null (unique=True + null=True is logically inconsistent)
        - Cannot set unique=False (use KeyField instead)

    Use Cases:
        - Primary identifiers: usernames, SKUs, email addresses
        - Slugs or permalinks
        - Any field that must be globally unique

    Example:
        class User(Model):
            username = UniqueKeyField()  # Unique across all users
            email = UniqueKeyField()     # Also unique across all users
            display_name = StringField()

        # Direct lookup by unique field
        user = User.query.get(username="johndoe")

    See Also:
        - KeyField: For non-unique key fields
        - AutoKeyField: For auto-generated unique identifiers
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a UniqueKeyField with strict uniqueness and non-null constraints.

        Args:
            **kwargs: Passed through to KeyField. Note these restrictions:
                - unique: Must be True (or omitted). Setting False raises ModelException.
                - null: Must be False (or omitted). Setting True raises ModelException.

        Raises:
            ModelException: If unique=False or null=True is specified.
        """
        if kwargs.get("unique") is False:
            raise ModelException("you may not set unique=False on this field type")
        kwargs["unique"] = True
        if kwargs.get("null") is True:
            raise ModelException("you may not set null=True on this field type")
        kwargs["null"] = False
        super().__init__(**kwargs)


class AutoKeyField(AutoFieldMixin, UniqueKeyField):
    """A UniqueKeyField whose value is auto-generated (UUID-based).

    AutoKeyField automatically generates a unique identifier (UUID hex) when
    a model instance is created. This is Popoto's equivalent of Django's
    AutoField or an auto-incrementing primary key.

    Automatically added to models that define no KeyField of their own.

    Key Behavior:
        - Generates a 32-character UUID hex string by default
        - Value is set at instance creation, before first save
        - Cannot be null, cannot be non-unique
        - If no KeyFields are defined on a model, Popoto automatically adds
          an AutoKeyField named "_auto_key"

    Design Decision:
        Uses UUID4 (random) rather than auto-increment for two reasons:
        1. Distributed safety: No coordination needed across Redis instances
        2. Privacy: IDs don't reveal creation order or count

    Example:
        class LogEntry(Model):
            # Explicit AutoKeyField
            entry_id = AutoKeyField()
            message = StringField()

        class SimpleModel(Model):
            # No KeyFields defined - Popoto adds "_auto_key" automatically
            data = StringField()

        entry = LogEntry(message="Hello")
        entry.save()
        print(entry.entry_id)  # e.g., "a1b2c3d4e5f6..."

    See Also:
        - AutoFieldMixin: UUID generation implementation
        - UniqueKeyField: Base class providing uniqueness
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize an AutoKeyField with auto-generation enabled.

        Args:
            **kwargs: Passed through to UniqueKeyField. Restrictions:
                - unique: Must be True (inherited from UniqueKeyField)
                - null: Must be False (inherited from UniqueKeyField)
                - auto_uuid_length (int): Length of UUID hex. Default: 32

        Raises:
            ModelException: If unique=False or null=True is specified.
        """
        if kwargs.get("unique") is False:
            raise ModelException("you may not set unique=False on this field type")
        if kwargs.get("null") is True:
            raise ModelException("you may not set null=True on this field type")
        kwargs["auto"] = True
        super().__init__(**kwargs)


class SortedField(SortedFieldMixin, Field):
    """A field backed by a Redis sorted set for fast range queries.

    SortedField maintains a parallel Redis Sorted Set index, enabling O(log N)
    range queries on numeric and temporal values. Without SortedField, range
    filtering would require scanning all instances.

    Supports filter lookups: ``__gt``, ``__gte``, ``__lt``, ``__lte``.
    Must be a numeric type (int, float, Decimal, date, or datetime).

    Supported Types:
        - int, float, Decimal (used directly as Redis scores)
        - date, datetime, time (converted to numeric via ordinal/timestamp)

    Constraints:
        - Cannot be null (Sorted Sets require a score for every member)
        - Must be a numeric or temporal type

    Partitioning (partition_by):
        For large datasets, you can partition the Sorted Set by another field's
        value using partition_by. This improves performance by reducing set size,
        but requires the partition field in all queries.

    Example:
        class Product(Model):
            sku = UniqueKeyField()
            price = SortedField(type=float)

        # Range query - O(log N) using ZRANGEBYSCORE
        cheap_products = Product.query.filter(price__lte=10.0)
        mid_range = Product.query.filter(price__gte=10.0, price__lte=50.0)

        # With partitioning for better scalability
        class Product(Model):
            sku = UniqueKeyField()
            category = KeyField()
            price = SortedField(type=float, partition_by='category')

        # Must include partition field
        Product.query.filter(category='electronics', price__lte=100.0)

    See Also:
        - SortedFieldMixin: Range query implementation details
        - SortedKeyField: Combines SortedField with KeyField behavior
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a SortedField with Sorted Set indexing.

        Args:
            **kwargs: Common options:
                - type: Field type. Default: float. Must be numeric or temporal.
                - null: Must be False (Sorted Sets require scores). Raises if True.
                - default: Default value for new instances.
                - partition_by (str or tuple): Field name(s) to partition the Sorted Set.
                    Improves performance on large datasets but requires partition
                    field(s) in all queries.
        """
        kwargs["sorted"] = True
        super().__init__(**kwargs)


class SortedKeyField(SortedFieldMixin, KeyFieldMixin, Field):
    """A field that is both a KeyField and a SortedField.

    SortedKeyField participates in the model's Redis key (like KeyField) while
    also maintaining a Sorted Set index for range queries (like SortedField).
    This enables both direct lookups and efficient range filtering on the same field.

    Use Case:
        When you need to both identify instances by a numeric value AND perform
        range queries on it. For example, a timestamp-based ID where you need
        both direct access and "find all after time X" queries.

    Inheritance Order:
        The MRO is [SortedFieldMixin, KeyFieldMixin, Field]. Both mixin behaviors
        are active: KeyField's Set-based lookups and SortedField's range queries.

    Example:
        class TimestampedEvent(Model):
            # Timestamp serves as both key and enables range queries
            timestamp = SortedKeyField(type=float)
            event_type = KeyField()
            data = DictField()

        # Direct lookup via key
        event = TimestampedEvent.query.get(timestamp=1609459200.0, event_type="login")

        # Range query via sorted index
        recent = TimestampedEvent.query.filter(timestamp__gte=1609459200.0)

    See Also:
        - KeyField: For key-only fields without range queries
        - SortedField: For range queries without key participation
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a SortedKeyField with both key and sorted behaviors.

        Args:
            **kwargs: Combines options from both KeyField and SortedField:
                - type: Field type. Default: float (from SortedFieldMixin).
                - key: Automatically True (participates in Redis key).
                - sorted: Automatically True (maintains Sorted Set index).
                - null: Must be False (Sorted Sets require scores).
                - unique (bool): Enforce single-field uniqueness. Default: False.
                - partition_by (str or tuple): Partition field(s) for the Sorted Set.
        """
        super().__init__(**kwargs)
