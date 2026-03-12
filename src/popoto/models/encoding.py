"""
Serialization and deserialization of Popoto model instances using msgpack.

Custom types (Decimal, tuple, set, datetime, date, time, DataFrame) are
encoded with tagged dicts so they round-trip through msgpack faithfully.

This module bridges Python's rich type system with Redis's binary storage using
MessagePack as the serialization format. MessagePack was chosen over JSON for
its compactness and speed, and over pickle for safety and cross-language
compatibility.

Design Philosophy:
    Redis stores all values as binary strings, but Popoto models use rich Python
    types (Decimal, datetime, pandas DataFrames, etc.). This module provides a
    type-preserving serialization system that encodes these types into a format
    MessagePack can handle, then decodes them back to their original Python types.

    The encoding strategy uses sentinel keys (e.g., "__Decimal__", "__datetime__")
    embedded in dictionaries to identify special types during decoding. This allows
    the decoder to distinguish between a regular dict and an encoded Decimal without
    requiring schema information.

Architecture:
    - TYPE_ENCODER_DECODERS: Registry mapping Python types to their encode/decode
      functions. Extensible for new types.
    - encode_popoto_model_obj(): Entry point for serializing a Model instance to
      a Redis hash (dict of field_name -> packed_value).
    - decode_popoto_model_hashmap(): Entry point for deserializing a Redis hash
      back into a Model instance.

Integration:
    Called by Model.save() to persist objects and by Query/DB_key to reconstruct
    objects from Redis. The encoding is transparent to model users.

Example:
    # Automatic during save
    person = Person(name="Alice", birthday=datetime.date(1990, 1, 15))
    person.save()  # Internally calls encode_popoto_model_obj

    # Automatic during query
    person = Person.query.get(name="Alice")  # Internally calls decode_popoto_model_hashmap
"""

import datetime
from collections import namedtuple
from decimal import Decimal
import msgpack
from ..exceptions import ModelException
from ..redis_db import ENCODING

try:
    import pandas as pd

    _pandas_available = True
except ImportError:
    _pandas_available = False

EncoderDecoder = namedtuple("EncoderDecoder", "key, encoder, decoder")
"""
A named tuple defining how to serialize and deserialize a specific Python type.

Attributes:
    key: A unique sentinel string (e.g., "__Decimal__") used to identify this
         type in serialized data. Appears as a key in the encoded dict.
    encoder: A callable that transforms a Python object into a dict with the
             sentinel key and an "as_encodable" key containing the serializable form.
    decoder: A callable that reconstructs the original Python object from the
             encoded dict.
"""

TYPE_ENCODER_DECODERS = {
    Decimal: EncoderDecoder(
        key="__Decimal__",
        encoder=lambda obj: {"__Decimal__": True, "as_encodable": str(obj)},
        decoder=lambda obj: Decimal(obj["as_encodable"]),
    ),
    tuple: EncoderDecoder(
        key="__tuple__",
        encoder=lambda obj: {"__tuple__": True, "as_encodable": list(obj)},
        decoder=lambda obj: tuple(obj["as_encodable"]),
    ),
    set: EncoderDecoder(
        key="__set__",
        encoder=lambda obj: {"__set__": True, "as_encodable": list(obj)},
        decoder=lambda obj: set(obj["as_encodable"]),
    ),
    datetime.datetime: EncoderDecoder(
        key="__datetime__",
        encoder=lambda obj: {
            "__datetime__": True,
            "as_encodable": obj.strftime("%Y%m%dT%H:%M:%S.%f"),
        },
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%Y%m%dT%H:%M:%S.%f"
        ),
    ),
    datetime.date: EncoderDecoder(
        key="__date__",
        encoder=lambda obj: {"__date__": True, "as_encodable": obj.strftime("%Y%m%d")},
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%Y%m%d"
        ).date(),
    ),
    datetime.time: EncoderDecoder(
        key="__time__",
        encoder=lambda obj: {
            "__time__": True,
            "as_encodable": obj.strftime("%H:%M:%S.%f"),
        },
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%H:%M:%S.%f"
        ).time(),
    ),
}

if _pandas_available:
    TYPE_ENCODER_DECODERS[pd.DataFrame] = EncoderDecoder(
        key="__dataframe__",
        encoder=lambda obj: {
            "__dataframe__": True,
            "as_encodable": obj.to_json(),
        },
        decoder=lambda obj: pd.read_json(obj["as_encodable"]),
    )
"""
Registry of type-specific serialization handlers.

Maps Python types to their EncoderDecoder definitions. When encoding a field
value, the system checks if the field's type is in this registry; if so, it
uses the custom encoder instead of default MessagePack serialization.

Supported types and their encoding strategies:
    - Decimal: String representation preserves arbitrary precision
    - tuple/set: Converted to lists (MessagePack's native sequence type)
    - datetime/date/time: ISO-ish string formats for human readability
    - DataFrame: JSON serialization (enables complex data analysis in models)

Trade-offs:
    - String encoding for Decimal/datetime prioritizes precision and readability
      over storage efficiency
    - DataFrame JSON encoding is verbose but preserves column types and index

To add a new type, add an entry mapping the type to an EncoderDecoder instance.
"""

DECODERS_BY_KEYSTRING = {
    encoder_decoder.key: encoder_decoder.decoder
    for encoder_decoder in TYPE_ENCODER_DECODERS.values()
}
"""
Lookup table for fast decoder resolution during deserialization.

Maps sentinel key strings (e.g., "__Decimal__") directly to decoder functions,
avoiding the need to iterate through TYPE_ENCODER_DECODERS during decode.
This is a performance optimization for the hot path of object reconstruction.
"""


def decode_custom_types(obj):
    """Msgpack object-hook that restores tagged dicts to their Python types.

    This is the counterpart to the type-specific encoders in TYPE_ENCODER_DECODERS.
    When MessagePack deserializes data, custom types come back as plain dicts
    with sentinel keys. This function detects those sentinel keys and applies
    the appropriate decoder to reconstruct the original Python type.

    Args:
        obj: Any value returned from msgpack.unpackb(). If it's a dict with
             "as_encodable" and a recognized sentinel key, it will be decoded.
             Otherwise, returned unchanged.

    Returns:
        The decoded Python object (Decimal, datetime, etc.) if obj was an
        encoded custom type, otherwise obj unchanged.

    Design Note:
        The "as_encodable" check is a fast-path optimization. Most dicts in
        user data won't have this key, so we can skip the sentinel key scan
        for the common case.
    """
    if isinstance(obj, dict) and "as_encodable" in obj:
        for keystring in DECODERS_BY_KEYSTRING.keys():
            if keystring in obj:
                return DECODERS_BY_KEYSTRING[keystring](obj)
    return obj


def encode_popoto_model_obj(obj: "Model") -> dict:
    """Encode a model instance into a dict of ``{field_name_bytes: msgpack_bytes}``.

    Transforms all field values on a model into a dictionary suitable for
    Redis HSET operations. Each field name becomes a UTF-8 encoded key,
    and each value is MessagePack-serialized (with custom type handling).

    Relationship fields are stored as the related instance's ``redis_key``.
    Custom types (Decimal, datetime, etc.) use tagged-dict encoding.

    Args:
        obj: A Popoto Model instance to serialize. Must have _meta.fields
             populated by the metaclass.

    Returns:
        A dict mapping bytes (field names) to bytes (packed values), ready
        for direct use with Redis HSET/HMSET commands.

    Raises:
        ModelException: If a Relationship field contains a value that isn't
                        an instance of the expected related model.

    Encoding Strategy:
        1. Relationship fields: Store the related object's db_key (Redis key
           string), not the full object. This enables lazy loading and avoids
           circular serialization.
        2. Custom types (Decimal, datetime, etc.): Use TYPE_ENCODER_DECODERS
           to wrap the value with a sentinel key for later type reconstruction.
        3. All other types: Direct MessagePack serialization (handles None,
           str, int, float, bool, list, dict natively).

    Integration:
        Called by Model.save() as part of the persistence pipeline. The returned
        dict is passed directly to Redis via HSET.

    Note:
        NumPy array support is enabled via msgpack_numpy patching, allowing
        fields to store numpy arrays efficiently.
    """
    try:
        import msgpack_numpy as m

        m.patch()
    except ImportError:
        pass

    encoded_hashmap = dict()
    for field_name, field in obj._meta.fields.items():
        # Skip capped ListField values -- they are stored in a separate Redis list key
        from ..fields.shortcuts import ListField

        if isinstance(field, ListField) and field._capped:
            continue

        value = getattr(obj, field_name)

        # use db_key string for relationships
        from ..fields.relationship import Relationship

        if value is not None and isinstance(field, Relationship):
            if isinstance(value, str):
                # Lazy-loaded redis_key string — already in storage format
                encoded_value = msgpack.packb(value)
            elif not isinstance(value, field.model):
                raise ModelException(
                    f"Relationship field requires {field.model} model instance. got {value} instead"
                )
            else:
                encoded_value = msgpack.packb(value.db_key.redis_key)
                # todo: refactor to store db_key list, not redis_key

        elif value is not None and field.type in TYPE_ENCODER_DECODERS.keys():
            encoded_value = msgpack.packb(
                TYPE_ENCODER_DECODERS[field.type].encoder(value)
            )
        else:
            encoded_value = msgpack.packb(value)

        encoded_hashmap[str(field_name).encode(ENCODING)] = encoded_value

    return encoded_hashmap


def decode_popoto_model_hashmap(
    model_class: "Model", redis_hash: dict, fields_only=False, lazy=False
) -> "Model":
    """Decode a Redis hash into a model instance (or a raw fields dict).

    The inverse of encode_popoto_model_obj(). Takes raw Redis hash data
    (bytes keys and MessagePack-encoded values) and reconstructs either
    a fully-instantiated Model object or a plain dictionary of field values.

    Args:
        model_class: The Model subclass to instantiate.
        redis_hash: Mapping of ``{field_name_bytes: msgpack_bytes}`` from Redis.
        fields_only: If ``True``, return a plain dict instead of a model instance.
                     Keys remain as bytes (not decoded to strings). Useful for
                     bulk operations where Model instantiation overhead is
                     unwanted, such as Query.values() projections.
        lazy: If ``True``, defer field deserialization until access. Fields are
              decoded on-demand when first accessed, reducing overhead for bulk
              queries where only a subset of fields are used.

    Returns:
        A model instance, a dict (when *fields_only*), or ``None`` if the hash
        is empty.

    Decoding Process:
        1. Each value is unpacked via msgpack.unpackb()
        2. decode_custom_types() checks for sentinel keys and reconstructs
           special types (Decimal, datetime, etc.)
        3. Field names are decoded from bytes to strings (unless fields_only)
        4. The resulting dict is passed to model_class() to create the instance

    Integration:
        Called by:
        - DB_key.get() for single-object retrieval
        - Query iteration for bulk object loading
        - Query.values() for projection queries (with fields_only=True)

    Note:
        Relationship fields are stored as Redis key strings, not full objects.
        The Model's __getattribute__ handles lazy loading of related objects
        when accessed.
    """
    if len(redis_hash):
        if fields_only:
            model_attrs = {
                key_b: decode_custom_types(
                    msgpack.unpackb(value_b, strict_map_key=False)
                )
                for key_b, value_b in redis_hash.items()
            }
            return model_attrs

        if lazy:
            # Lazy loading: store raw bytes, decode on access
            return _create_lazy_model(model_class, redis_hash)

        model_attrs = {
            key_b.decode(ENCODING): decode_custom_types(
                msgpack.unpackb(value_b, strict_map_key=False)
            )
            for key_b, value_b in redis_hash.items()
        }

        # Create the model instance
        model_instance = model_class(**model_attrs)

        # Load capped ListField data from separate Redis list keys
        _load_capped_list_fields(model_class, model_instance)

        # Store the loaded field values for proper cleanup on delete
        # This ensures that if the model is modified and then deleted,
        # the on_delete hooks use the original saved values
        model_instance._saved_field_values = {
            field_name: getattr(model_instance, field_name)
            for field_name in model_instance._meta.fields.keys()
        }

        return model_instance

    return None


def _load_capped_list_fields(model_class, model_instance):
    """Load capped ListField data from separate Redis list keys.

    For each ListField with max_length set (capped), fetches the list data
    from the Redis list key ``{model_redis_key}::field_name`` and wraps it
    in a CappedListProxy.

    Args:
        model_class: The Model class.
        model_instance: The model instance to populate.
    """
    from ..fields.shortcuts import ListField, CappedListProxy, _decode_list_element
    from ..redis_db import POPOTO_REDIS_DB

    for field_name, field in model_class._meta.fields.items():
        if isinstance(field, ListField) and field._capped:
            redis_key = model_instance._redis_key or model_instance.db_key.redis_key
            list_key = f"{redis_key}::{field_name}"
            raw_values = POPOTO_REDIS_DB.lrange(list_key, 0, -1)
            data = [_decode_list_element(v) for v in raw_values]
            proxy = CappedListProxy(
                data=data,
                model_instance=model_instance,
                field_name=field_name,
                max_length=field.max_length,
            )
            setattr(model_instance, field_name, proxy)


def _create_lazy_model(model_class: "Model", redis_hash: dict) -> "Model":
    """Create a model instance with deferred field deserialization.

    Uses object.__new__() to bypass the full __init__ process, then sets up
    the instance for lazy field loading. Fields are decoded from msgpack
    on first access via Model.__getattribute__.

    Args:
        model_class: The Model subclass to instantiate.
        redis_hash: Raw Redis hash with msgpack-encoded values.

    Returns:
        A model instance with _lazy_fields containing raw bytes.
    """
    # Create instance without calling __init__
    instance = object.__new__(model_class)

    # Store raw msgpack bytes for lazy decoding
    instance._lazy_fields = {
        key_b.decode(ENCODING): value_b for key_b, value_b in redis_hash.items()
    }
    instance._decoded_fields = {}

    # Initialize essential Model attributes that would be set in __init__
    instance._redis_key = None
    instance._db_content = dict()
    instance._saved_field_values = dict()
    instance.obsolete_redis_key = None
    instance._ttl = model_class._meta.ttl
    instance._expire_at = None

    return instance


def decode_lazy_field(value_bytes: bytes):
    """Decode a single msgpack-encoded field value.

    Called by Model.__getattribute__ when accessing a lazily-loaded field
    for the first time.

    Args:
        value_bytes: Raw msgpack bytes from Redis.

    Returns:
        The decoded Python value with custom types restored.
    """
    return decode_custom_types(msgpack.unpackb(value_bytes, strict_map_key=False))
