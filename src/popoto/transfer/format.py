"""JSON Lines export format for :mod:`popoto.transfer`.

The on-disk format is a single manifest object on the first line followed by
one JSON object per exported record::

    {"popoto_export": 1, "model": "Memory", "exported_at": "...", ...}
    {"key": "Memory:abc", "values": {...}, "state": {...}, "model_state": {...}}
    {"key": "Memory:def", "values": {...}, "state": {}, "model_state": {}}

Only the standard library is used. Values that JSON cannot represent natively
are coerced through Popoto's existing per-type encoder registry
(:data:`popoto.models.encoding.TYPE_ENCODER_DECODERS`), whose encoders already
emit JSON-primitive tagged dicts such as
``{"__Decimal__": True, "as_encodable": "1.23"}``. The inverse uses
:data:`popoto.models.encoding.DECODERS_BY_KEYSTRING`.

Two tags are local to this module because the shared registry has no entry for
them:

``__bytes__``
    Binary payloads (e.g. a serialized embedding vector) carried as base64.
``__dictpairs__``
    A mapping with non-string keys, which JSON objects cannot express.
"""

from __future__ import annotations

import base64
import datetime
import json
from importlib.metadata import PackageNotFoundError, version as _get_version
from typing import Any, Iterator, TextIO

from ..models.encoding import DECODERS_BY_KEYSTRING, TYPE_ENCODER_DECODERS

FORMAT_VERSION = 1
"""Version of the ``popoto_export`` JSON Lines format written by this module.

Bumped whenever the record or manifest shape changes incompatibly. Import
refuses a file whose version it does not recognise; cross-version
compatibility is explicitly not promised.
"""

MANIFEST_KEY = "popoto_export"
"""Manifest field carrying :data:`FORMAT_VERSION`; also the manifest marker."""

BYTES_TAG = "__bytes__"
DICT_PAIRS_TAG = "__dictpairs__"

_JSON_PRIMITIVES = (str, int, float, bool)


def popoto_version() -> str:
    """Return the installed popoto version, or a sentinel when unknown."""
    try:
        return _get_version("popoto")
    except PackageNotFoundError:  # pragma: no cover - source-tree import
        return "0.0.0+unknown"


def _encoder_for(value: Any):
    """Return the registry encoder for ``value``'s type, or ``None``.

    Exact-type lookup first (the common case and the cheap one), then a
    subclass scan so that, say, a ``pandas`` subclass still round-trips.
    """
    encoder_decoder = TYPE_ENCODER_DECODERS.get(type(value))
    if encoder_decoder is not None:
        return encoder_decoder.encoder
    for registered_type, encoder_decoder in TYPE_ENCODER_DECODERS.items():
        try:
            matches = isinstance(value, registered_type)
        except TypeError:  # pragma: no cover - defensive
            continue
        if matches:
            return encoder_decoder.encoder
    return None


def to_jsonable(value: Any) -> Any:
    """Coerce ``value`` into something :func:`json.dumps` accepts.

    Args:
        value: Any Python value taken from a model field or from a field's
            ``export_state`` return.

    Returns:
        A JSON-representable structure. Non-primitive types are wrapped in a
        tagged dict that :func:`from_jsonable` reverses.

    Raises:
        TypeError: If the value has no encoder and is not a JSON primitive or
            container. Raising here is deliberate: silently stringifying an
            unknown type would produce an export that imports to the wrong
            value with no diagnostic.
    """
    if value is None or isinstance(value, _JSON_PRIMITIVES):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            BYTES_TAG: True,
            "as_encodable": base64.b64encode(bytes(value)).decode("ascii"),
        }

    encoder = _encoder_for(value)
    if encoder is not None:
        encoded = encoder(value)
        # The registry encoders emit {"__tag__": True, "as_encodable": ...}
        # where as_encodable may itself hold arbitrary values (a tuple's
        # contents, for example), so recurse into it.
        return {
            key: (to_jsonable(item) if key == "as_encodable" else item)
            for key, item in encoded.items()
        }

    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: to_jsonable(item) for key, item in value.items()}
        return {
            DICT_PAIRS_TAG: True,
            "as_encodable": [
                [to_jsonable(key), to_jsonable(item)] for key, item in value.items()
            ],
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        # tuple/set have registry encoders and never reach here; list and any
        # other sequence type land as a plain JSON array.
        return [to_jsonable(item) for item in value]

    raise TypeError(
        f"cannot represent {type(value).__name__} in the popoto export format; "
        f"register an encoder in TYPE_ENCODER_DECODERS or carry it as bytes"
    )


def from_jsonable(value: Any) -> Any:
    """Reverse :func:`to_jsonable`.

    Args:
        value: A structure parsed from a JSON Lines record.

    Returns:
        The original Python value, with tagged dicts restored to their types.
    """
    if isinstance(value, list):
        return [from_jsonable(item) for item in value]

    if not isinstance(value, dict):
        return value

    if BYTES_TAG in value and "as_encodable" in value:
        return base64.b64decode(value["as_encodable"].encode("ascii"))

    if DICT_PAIRS_TAG in value and "as_encodable" in value:
        return {
            from_jsonable(pair[0]): from_jsonable(pair[1])
            for pair in value["as_encodable"]
        }

    for tag, decoder in DECODERS_BY_KEYSTRING.items():
        if tag in value and "as_encodable" in value:
            restored = dict(value)
            restored["as_encodable"] = from_jsonable(value["as_encodable"])
            return decoder(restored)

    return {key: from_jsonable(item) for key, item in value.items()}


def build_manifest(
    model_name: str,
    filter_repr: "str | None",
    filter_kwargs: dict,
    matched_count: int,
    fields: dict,
    mixins: dict,
    embedding_provenance: dict,
) -> dict:
    """Assemble the manifest object written as the export's first line.

    ``filter`` and ``matched_count`` are deliberately separate members: a
    filter that matched nothing is ``{"filter": "Q(x='y')", "matched_count":
    0}`` while an empty model is ``{"filter": null, "matched_count": 0}``.

    Args:
        model_name: ``Model.__name__`` of the exported model.
        filter_repr: Rendered provenance of the applied filter, or ``None``
            when the export was unfiltered.
        filter_kwargs: The plain keyword filters, JSON-coerced.
        matched_count: Number of keys the filter resolved to, recorded at
            resolution time (export is not a point-in-time snapshot).
        fields: Per-field ``{"class", "policy", "note"}`` roll-up.
        mixins: Per-model-level-mixin ``{"policy", "note"}`` roll-up.
        embedding_provenance: Per-field ``{provider, model, dimensions}``.

    Returns:
        A JSON-serializable manifest dict.
    """
    return {
        MANIFEST_KEY: FORMAT_VERSION,
        "model": model_name,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "popoto_version": popoto_version(),
        "filter": filter_repr,
        "filter_kwargs": filter_kwargs,
        "matched_count": matched_count,
        "fields": fields,
        "mixins": mixins,
        "embedding_provenance": embedding_provenance,
        "consistent_snapshot": False,
    }


def dump_line(obj: dict) -> str:
    """Serialize one manifest or record object as a newline-terminated line."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=False) + "\n"


def iter_lines(stream: TextIO) -> Iterator["tuple[int, str]"]:
    """Yield ``(line_number, raw_line)`` for every non-blank line.

    Line numbers are 1-based and count blank lines, so a reported number
    matches what an operator sees in an editor.
    """
    for line_number, raw in enumerate(stream, start=1):
        if not raw.strip():
            continue
        yield line_number, raw


def parse_line(raw: str) -> dict:
    """Parse one JSON Lines line into a dict.

    Raises:
        ValueError: If the line is not valid JSON or is not a JSON object.
            The caller counts this as an ``errored`` record with its line
            number and continues, so a truncated file still imports the rest.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
