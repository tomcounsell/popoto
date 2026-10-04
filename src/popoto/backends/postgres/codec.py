"""``jsonb`` encoding for the collection fields on Postgres (#759 M1.1).

``ListField``, ``DictField``, ``SetField`` and ``TupleField`` are stored as
``jsonb`` -- never msgpack (plan §8). The contract is that a value comes back
exactly as it comes back from Redis, so the encoding reproduces what msgpack
plus popoto's tagged-dict registry (``TYPE_ENCODER_DECODERS``) round-trip, and
nothing more:

* **The top level** is typed by the column: a ``list``/``tuple``/``set``
  field is a JSON array, re-typed on the way out by the field's own type (on
  Redis the registry tags a top-level tuple or set for the same reason).
* **Nested values** behave as msgpack does: a nested ``tuple`` comes back a
  ``list``; a tagged dict (``{"__Decimal__": True, "as_encodable": ...}``,
  written by ``CappedListProxy.push`` and capped-list saves per element)
  decodes to its type at any depth, as ``decode_custom_types`` does as
  msgpack's object hook; and a value msgpack cannot pack (a nested ``set``,
  ``Decimal``, ``datetime``...) raises ``TypeError`` here too.
* **What JSON lacks and msgpack has** is tagged so it survives: ``bytes``
  (base64), a non-finite ``float`` and a ``dict`` with a non-``str`` key.
  These three tags are this module's own and never reach Redis.

Pure: no network, no ``psycopg``.
"""

from __future__ import annotations

import base64
import math
from typing import Any

__all__ = ["decode_json", "encode_json", "encode_json_element"]

_BYTES = "__pg_bytes__"
_FLOAT = "__pg_float__"
_MAP = "__pg_map__"
_OWN_TAGS = (_BYTES, _FLOAT, _MAP)


def _tag(key: str, payload: Any) -> dict[str, Any]:
    return {key: True, "as_encodable": payload}


def _encode_nested(value: Any) -> Any:
    """``value`` as msgpack would carry it, made JSON-safe."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return _tag(_FLOAT, repr(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _tag(_BYTES, base64.b64encode(bytes(value)).decode("ascii"))
    if isinstance(value, (list, tuple)):
        return [_encode_nested(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: _encode_nested(v) for k, v in value.items()}
        return _tag(
            _MAP, [[_encode_nested(k), _encode_nested(v)] for k, v in value.items()]
        )
    raise TypeError(f"can not serialize {type(value).__name__!r} object")


def encode_json_element(value: Any) -> Any:
    """One element of a capped ``ListField``: tagged through popoto's type
    registry first, exactly as ``_encode_list_element`` does for Redis's
    per-element ``RPUSH``/``LPUSH``."""
    from ...models.encoding import TYPE_ENCODER_DECODERS

    if value is not None and type(value) in TYPE_ENCODER_DECODERS:
        value = TYPE_ENCODER_DECODERS[type(value)].encoder(value)
    return _encode_nested(value)


def encode_json(py_type: type, value: Any, *, capped: bool = False) -> Any:
    """The JSON document stored for a collection field's ``value``."""
    if value is None:
        return None
    data = getattr(value, "_data", value)  # a CappedListProxy holds a list
    if capped:
        return [encode_json_element(v) for v in data]
    if py_type in (list, tuple, set) and isinstance(data, (list, tuple, set)):
        return [_encode_nested(v) for v in data]
    return _encode_nested(data)


def _decode_nested(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_nested(v) for v in value]
    if isinstance(value, dict):
        if "as_encodable" in value:
            if value.get(_BYTES) is True:
                return base64.b64decode(value["as_encodable"])
            if value.get(_FLOAT) is True:
                return float(value["as_encodable"])
            if value.get(_MAP) is True:
                return {
                    _hashable(_decode_nested(k)): _decode_nested(v)
                    for k, v in value["as_encodable"]
                }
        decoded = {k: _decode_nested(v) for k, v in value.items()}
        from ...models.encoding import decode_custom_types

        return decode_custom_types(decoded)
    return value


def _hashable(key: Any) -> Any:
    # msgpack decodes an array key as a tuple when it must be hashable.
    return tuple(_hashable(k) for k in key) if isinstance(key, list) else key


def decode_json(py_type: type, value: Any) -> Any:
    """A stored JSON document back as the field's Python value."""
    if value is None:
        return None
    decoded = _decode_nested(value)
    if py_type is tuple and isinstance(decoded, list):
        return tuple(decoded)
    if py_type is set and isinstance(decoded, list):
        return set(decoded)
    return decoded
