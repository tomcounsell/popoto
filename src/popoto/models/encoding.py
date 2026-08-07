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
import re
from collections import namedtuple
from decimal import Decimal
from typing import Any
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

_LEGACY_DATETIME_FORMAT = "%Y%m%dT%H:%M:%S.%f"
"""Offset-free datetime format written before #521. Read-only from here on."""

_LEGACY_DATETIME_RE = re.compile(r"^\d{8}T\d{2}:\d{2}:\d{2}\.\d{1,6}$")
"""Anchored shape test for :data:`_LEGACY_DATETIME_FORMAT`.

The legacy branch is selected by matching this, **not** by letting
``fromisoformat`` raise. On 3.11+ ``fromisoformat`` parses
``20260807T12:00:00.123456`` and on the 3.10 ``requires-python`` floor it does
not, so the moment the two branches stop agreeing about awareness -- which is
exactly what #537 does -- a try-order fallback turns into an interpreter-visible
behaviour switch: the same stored bytes would decode aware on 3.10 and naive on
3.12. Matching the shape explicitly makes the branch a property of the data.

A post-#521 deliberately naive value can never match: it is written by
``isoformat()`` as ``2026-08-07T12:00:00.123456``, with date separators.
"""

_LEGACY_TIME_FORMAT = "%H:%M:%S.%f"
"""Offset-free time format written before #521. Read-only from here on."""


def _decode_datetime(obj: dict[str, Any]) -> datetime.datetime:
    """Decode a stored datetime, tolerating the pre-#521 offset-free form.

    Values written from #521 onward are `isoformat()`, which carries the UTC
    offset when there is one. Values written before it used
    `%Y%m%dT%H:%M:%S.%f`, which has no offset directive, so an aware value was
    stored as its own wall clock with the offset discarded (#521).

    A legacy string is decoded as **UTC** (#537). The offset was never written,
    so nothing is being recovered -- UTC is being *assumed*, and the assumption
    is the same one the rest of the library already makes about an offset-free
    datetime: `SortedFieldMixin.convert_to_numeric` has treated a naive value
    as UTC when deriving a score since #519, and `auto_now` has stamped aware
    UTC since #421. A legacy row therefore scored identically before and after
    this change; what changes is that it now *compares* as the instant it was
    always scored as.

    This assumption was written once before (commit `0342550`) and reverted,
    and the reason it is safe now is worth stating precisely. `datetime` is a
    valid `KeyField` type, and `DB_key` used to build both the hash key and the
    `$KeyF:` index key from `str(value)`. Stamping an offset shifts `str()`, so
    it shifted the derived key away from the key the row was stored under, and
    loading and re-saving a legacy row wrote a second hash and orphaned the
    original -- silently, because the recomputed `_redis_key` blinded `save()`'s
    obsolete-key branch at the same time the unchanged attribute blinded its
    KeyMutationError guard. Keys are now derived through `canonical_key_str`
    (#537/#538), which renders the *instant* and not the representation, so an
    offset-free legacy value and the same value stamped UTC produce identical
    key bytes. The assumption moves no key.

    Callers that compared a legacy value against a naive `datetime.now()` will
    now raise `TypeError` on the naive/aware comparison. That is the intended,
    visible consequence of the value becoming honest about what it denotes, and
    it is why this ships in a minor rather than a patch.

    A `ZoneInfo` tzinfo survives as the fixed offset in effect at that instant
    rather than as the zone itself. The instant is preserved and `fold` is
    resolved at encode time, but later arithmetic across a DST boundary on a
    reloaded value differs from the same arithmetic on the original.
    """
    as_encodable = obj["as_encodable"]
    if _LEGACY_DATETIME_RE.match(as_encodable):
        legacy = datetime.datetime.strptime(as_encodable, _LEGACY_DATETIME_FORMAT)
        return legacy.replace(tzinfo=datetime.timezone.utc)
    # Not the legacy shape: parse as isoformat and let a genuinely malformed
    # value raise rather than be coerced into a plausible datetime.
    return datetime.datetime.fromisoformat(as_encodable)


def _decode_time(obj: dict[str, Any]) -> datetime.time:
    """Decode a stored time, tolerating the pre-#521 offset-free form.

    Same split as :func:`_decode_datetime`: `isoformat()` going forward,
    `%H:%M:%S.%f` accepted for anything already on disk.

    The `strptime` fallback is defensive rather than load-bearing. Unlike the
    datetime case, `time.fromisoformat` already accepts the legacy shape on
    every supported interpreter (verified on 3.10 and 3.12), because
    `%H:%M:%S.%f` output is itself valid isoformat. It stays as a guard against
    a value written by some other path.
    """
    as_encodable = obj["as_encodable"]
    try:
        return datetime.time.fromisoformat(as_encodable)
    except ValueError:
        return datetime.datetime.strptime(as_encodable, _LEGACY_TIME_FORMAT).time()


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
            # isoformat() carries the UTC offset when the value is aware and
            # omits it when the value is naive, so awareness survives the round
            # trip in both directions (#521).
            "as_encodable": obj.isoformat(),
        },
        decoder=_decode_datetime,
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
            # A time may carry tzinfo too, and strftime("%H:%M:%S.%f") dropped
            # it for the same reason the datetime format did (#521).
            "as_encodable": obj.isoformat(),
        },
        decoder=_decode_time,
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


def _as_key_str(redis_key) -> str:
    """Normalize a Redis key to ``str``; SCAN and friends hand back bytes."""
    return redis_key.decode(ENCODING) if isinstance(redis_key, bytes) else redis_key


def decode_popoto_model_hashmap(
    model_class: "Model",
    redis_hash: dict,
    fields_only=False,
    lazy=False,
    source_redis_key=None,
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
        source_redis_key: The key this hash was actually read from. When given
              it becomes the instance's ``_redis_key``, so the instance knows
              where it came from rather than inferring it from the decoded
              values. Callers that have the key should always pass it; the
              fallback recomputation is for callers that genuinely do not
              (see the identity-provenance note below).

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

    Identity provenance (#537/#538):
        Without *source_redis_key* an instance's ``_redis_key`` is recomputed
        from its decoded KeyField values, so the instance's idea of where it
        came from follows the decode. That is the precise mechanism by which
        row duplication was *silent*: when a decode change shifts a KeyField's
        rendering, ``_saved_field_values`` and the attribute still agree (both
        hold the decoded value) so ``save()``'s KeyMutationError guard sees no
        change, and the recomputed ``_redis_key`` already matches the new
        derivation so ``save()``'s obsolete-key branch never fires either.
        Two independent safety nets, both blinded by the same recomputation;
        the row is written to a second hash and the original is orphaned with
        no exception and no log line.

        Passing the real source key converts that silent duplication into
        ``save()``'s existing *rename* path. It also makes the datetime-key
        migration lazily self-healing: a pre-migration row that happens to be
        re-saved before the operator runs the migration moves itself.
    """
    if len(redis_hash):
        if fields_only:
            model_attrs = {
                key_b: decode_custom_types(
                    msgpack.unpackb(value_b, strict_map_key=False)
                )
                for key_b, value_b in redis_hash.items()
                # Skip internal pointer fields. #476: current code no longer
                # writes these into the hash; kept for pre-#476 legacy
                # records until they self-heal on next write.
                if not (
                    b"\x00" in key_b if isinstance(key_b, bytes) else "\x00" in key_b
                )
            }
            return model_attrs

        if lazy:
            # Lazy loading: store raw bytes, decode on access
            return _create_lazy_model(
                model_class, redis_hash, source_redis_key=source_redis_key
            )

        model_attrs = {
            key_b.decode(ENCODING): decode_custom_types(
                msgpack.unpackb(value_b, strict_map_key=False)
            )
            for key_b, value_b in redis_hash.items()
            if b"\x00" not in key_b  # skip internal pointer fields (e.g. \x00idxset)
        }

        # Create the model instance
        model_instance = model_class(**model_attrs)

        # Identity provenance: prefer the key this hash was actually read from
        # over __init__'s recomputation from the decoded values. Set before
        # _load_capped_list_fields, which reads _redis_key to find the list keys.
        if source_redis_key is not None:
            model_instance._redis_key = _as_key_str(source_redis_key)

        # Load capped ListField data from separate Redis list keys
        _load_capped_list_fields(model_class, model_instance)

        # Store the loaded field values for proper cleanup on delete
        # This ensures that if the model is modified and then deleted,
        # the on_delete hooks use the original saved values
        model_instance._saved_field_values = {
            field_name: getattr(model_instance, field_name)
            for field_name in model_instance._meta.fields.keys()
        }
        model_instance._is_persisted = True

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


def _create_lazy_model(
    model_class: "Model", redis_hash: dict, source_redis_key=None
) -> "Model":
    """Create a model instance with deferred field deserialization.

    Uses object.__new__() to bypass the full __init__ process, then sets up
    the instance for lazy field loading. Fields are decoded from msgpack
    on first access via Model.__getattribute__.

    KeyField values are eagerly decoded so that ``_redis_key`` and
    ``_saved_field_values`` are populated immediately. This is required for
    correct key-migration behaviour in ``save()`` — without it, changing a
    KeyField on a filter()-loaded instance would create a duplicate Redis
    entry instead of replacing the old one.  Only KeyField values are
    decoded eagerly; all other fields remain lazy.

    Fields absent from ``redis_hash`` are initialized to their declared
    default (or ``None``), matching ``Model.__init__``. This prevents the
    class-level ``Field`` descriptor object from leaking through
    ``object.__getattribute__`` for partial-hash rows (e.g., a process
    crashed mid-write, or a field was added after the row was saved).
    See issue #380.

    Args:
        model_class: The Model subclass to instantiate.
        redis_hash: Raw Redis hash with msgpack-encoded values.
        source_redis_key: The key this hash was read from, used as
            ``_redis_key`` in preference to recomputing it from the decoded
            KeyField values. See decode_popoto_model_hashmap's docstring for
            why the recomputation made row duplication silent (#537/#538).

    Returns:
        A model instance with _lazy_fields containing raw bytes.
    """
    # Create instance without calling __init__
    instance = object.__new__(model_class)

    # Store raw msgpack bytes for lazy decoding.
    # Skip internal pointer fields (names containing \x00, e.g. field\x00idxset)
    # so they never surface as model attributes.
    instance._lazy_fields = {
        key_b.decode(ENCODING): value_b
        for key_b, value_b in redis_hash.items()
        if b"\x00" not in key_b
    }
    instance._decoded_fields = {}

    # Initialize essential Model attributes that would be set in __init__
    instance._redis_key = None
    instance._db_content = dict()
    instance._saved_field_values = dict()
    instance.obsolete_redis_key = None
    instance._ttl = model_class._meta.ttl
    instance._expire_at = None
    instance._is_persisted = True

    # Eagerly decode KeyField values so _redis_key and _saved_field_values
    # are correct.  This mirrors what __init__ does at base.py L604-608 for
    # non-lazy instances.  Only KeyFields are decoded here — all other fields
    # stay in _lazy_fields for on-demand decoding.
    for key_field_name in model_class._meta.key_field_names:
        if key_field_name in instance._lazy_fields:
            decoded_value = decode_lazy_field(instance._lazy_fields[key_field_name])
            instance._decoded_fields[key_field_name] = decoded_value
            instance._saved_field_values[key_field_name] = decoded_value

    # Initialize defaults for fields absent from the hash.  Mirrors the
    # defaults loop in Model.__init__ (base.py L532-536), ensuring that
    # __getattribute__ finds the value in instance.__dict__ before falling
    # through to the class-level Field descriptor.  Without this, partial
    # hashes (e.g., crashed mid-write rows) would leak the descriptor
    # object on access for any field absent from the hash.  See #380.
    #
    # We use object.__setattr__ to bypass Model.__setattr__'s lazy-cache
    # branch, landing the value directly in instance.__dict__.  Fields
    # eagerly decoded as KeyFields above are skipped via the
    # _decoded_fields check.
    for field_name, field in model_class._meta.fields.items():
        if field_name in instance._lazy_fields:
            continue
        if field_name in instance._decoded_fields:
            continue
        default_value = field.default() if callable(field.default) else field.default
        object.__setattr__(instance, field_name, default_value)

    # Identity provenance (#537/#538): the key this hash was read from beats
    # any key derived from its decoded values. Recomputing is the fallback for
    # callers with no key to give, and is what made duplication silent -- see
    # decode_popoto_model_hashmap's docstring.
    if source_redis_key is not None:
        instance._redis_key = _as_key_str(source_redis_key)
    elif None not in [
        instance._decoded_fields.get(kf) for kf in model_class._meta.key_field_names
    ]:
        instance._redis_key = instance.db_key.redis_key

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
