"""Export a model's records to JSON Lines.

The driver holds no knowledge of any concrete field or mixin type. Auxiliary
state is collected through two duck-typed passes:

* **Field level** -- iterate ``_meta.fields`` and call ``export_state`` on each
  field. State is keyed by field name.
* **Model level** -- walk ``type(instance).__mro__`` and call ``export_state``
  on any class that defines it *as its own attribute*. State is keyed by the
  class name.

Both passes test for the presence of the protocol member, never for membership
of a named class, so a field or model mixin Popoto does not yet have
participates with no change here.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any, Protocol, TextIO, cast

from .format import build_manifest, dump_line, to_jsonable
from .results import ExportResult

if TYPE_CHECKING:  # pragma: no cover - types only, never imported at runtime
    from ..models.base import Model

logger = logging.getLogger("popoto")


class _ExportsState(Protocol):
    """The duck-typed model-level contract this module's docstring describes.

    Naming it lets the ``klass.export_state(instance)`` call below be *checked*
    rather than suppressed. The ``"export_state" not in klass.__dict__``
    guard immediately above the cast is what makes the cast sound.
    """

    __name__: str

    @staticmethod
    def export_state(instance: Any) -> Any: ...

DEFAULT_CHUNK_SIZE = 500
"""Keys hydrated per round trip.

``Query.keys()`` is a single unbatched ``SMEMBERS`` and ``get_many_objects``
pipelines every ``HGETALL`` at once, so without chunking a large model would
hydrate entirely before a byte is written. Chunking bounds peak memory to one
chunk of instances. The key set itself is still resolved in one shot -- a known
ceiling, not solved here.
"""


def _as_str(key: Any) -> str:
    """Normalize a redis key to ``str`` (SMEMBERS returns bytes)."""
    return key.decode() if isinstance(key, (bytes, bytearray)) else str(key)


def _render_filter(
    q_objects: "list[Any]", filters: "dict[str, Any]"
) -> "str | None":
    """Render filter provenance from Q objects plus plain kwargs.

    ``Q.__repr__`` already produces ``(Q(a='b') OR ~Q(c__lt=2))``, so it is
    reused verbatim. ``QueryBuilder.__repr__`` is deliberately NOT used: it
    prints only ``_filters`` and silently drops every Q object.
    """
    parts = [repr(q) for q in q_objects]
    if filters:
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(filters.items()))
        parts.append(f"Q({rendered})")
    if not parts:
        return None
    return " AND ".join(parts)


def field_provenance(model_field: Any) -> "dict[str, Any] | None":
    """Return ``{provider, model, dimensions}`` for a field that has a provider.

    Duck-typed on the presence of a ``provider`` attribute that exposes
    ``dimensions``; no concrete field class is named. Popoto stores no
    provider or model identity alongside an embedding today, so this
    fingerprint exists only in the export file. Import compares it against the
    destination's provider and refuses a mismatch by default -- the difference
    between a wrong vector space that errors and one that lands silently.

    Returns:
        The provenance dict, or ``None`` when the field has no provider at all.
        A field whose provider cannot be resolved yields ``"unknown"`` values
        rather than nothing, so the ambiguity is carried rather than hidden.
    """
    if not hasattr(type(model_field), "provider"):
        return None
    try:
        provider = model_field.provider
    except Exception as exc:
        logger.debug("provider lookup failed: %s", exc)
        return {"provider": "unknown", "model": None, "dimensions": None}
    if provider is None:
        return {"provider": "unknown", "model": None, "dimensions": None}

    model_name = None
    for attribute in ("model_name", "_model_name", "model", "_model"):
        candidate = getattr(provider, attribute, None)
        if isinstance(candidate, str):
            model_name = candidate
            break
    dimensions = getattr(provider, "dimensions", None)
    return {
        "provider": type(provider).__name__,
        "model": model_name,
        "dimensions": dimensions if isinstance(dimensions, int) else None,
    }


def collect_embedding_provenance(
    model_class: "type[Model]",
) -> "dict[str, dict[str, Any]]":
    """Return per-field provider fingerprints for every provider-backed field."""
    provenance: "dict[str, dict[str, Any]]" = {}
    for field_name, model_field in model_class._meta.fields.items():
        found = field_provenance(model_field)
        if found is not None:
            provenance[field_name] = found
    return provenance


def collect_field_policies(model_class: "type[Model]") -> "dict[str, dict[str, Any]]":
    """Return the per-field ``roundtrip_policy`` roll-up for the manifest."""
    policies: "dict[str, dict[str, Any]]" = {}
    for field_name, model_field in model_class._meta.fields.items():
        policies[field_name] = {
            "class": type(model_field).__name__,
            "policy": getattr(model_field, "roundtrip_policy", "rebuild"),
            "note": getattr(model_field, "roundtrip_note", None),
        }
    return policies


def collect_mixin_policies(model_class: "type[Model]") -> "dict[str, dict[str, Any]]":
    """Return the per-model-level-mixin ``roundtrip_policy`` roll-up.

    Duck-typed on a class declaring ``roundtrip_policy`` or ``export_state``
    in its own ``__dict__``, so a third-party model mixin with independent
    Redis state appears in the report with no change here. ``Model`` itself
    declares neither, so a model with no mixins yields ``{}``.
    """
    policies: "dict[str, dict[str, Any]]" = {}
    for klass in model_class.__mro__:
        own = klass.__dict__
        if "roundtrip_policy" not in own and "export_state" not in own:
            continue
        policies[klass.__name__] = {
            "policy": own.get("roundtrip_policy", "rebuild"),
            "note": own.get("roundtrip_note", None),
        }
    return policies


def _record_values(model_class: "type[Model]", instance: "Model") -> "dict[str, Any]":
    """Return the record's field values, including implicit key fields.

    ``to_dict()`` iterates ``_meta.explicit_fields``, so a model relying on the
    auto-generated ``_auto_key`` produces a dict that cannot reconstruct its
    own key. Every key field present in ``_meta.fields`` but missing from the
    dict is added here.
    """
    values = instance.to_dict()

    key_names = set(getattr(model_class._meta, "key_field_names", ()) or ())
    auto_key_name = model_class._get_auto_key_field_name()
    if auto_key_name:
        key_names.add(auto_key_name)

    for field_name, model_field in model_class._meta.fields.items():
        if field_name in values:
            continue
        is_key = field_name in key_names or getattr(model_field, "key", False)
        if is_key or getattr(model_field, "auto", False):
            values[field_name] = getattr(instance, field_name, None)
    return values


def _field_state(
    model_class: "type[Model]", instance: "Model", result: ExportResult
) -> "dict[str, Any]":
    """Collect field-level carried state, keyed by field name."""
    state: "dict[str, Any]" = {}
    for field_name, model_field in model_class._meta.fields.items():
        exporter = getattr(model_field, "export_state", None)
        if exporter is None:
            continue
        try:
            carried = exporter(
                instance, field_name, getattr(instance, field_name, None)
            )
        except Exception as exc:
            result.warnings.append(
                f"{field_name}.export_state raised on "
                f"{instance.db_key.redis_key}: {type(exc).__name__}: {exc}"
            )
            continue
        if carried is not None:
            state[field_name] = to_jsonable(carried)
    return state


def _model_state(instance: "Model", result: ExportResult) -> "dict[str, Any]":
    """Collect model-level carried state, keyed by declaring class name."""
    state: "dict[str, Any]" = {}
    for klass in type(instance).__mro__:
        if "export_state" not in klass.__dict__:
            continue
        # The guard above is what makes this cast sound: the class declares
        # export_state as its own attribute. _ExportsState names that
        # duck-typed contract so the call is checked, not suppressed.
        exporting_class = cast("_ExportsState", klass)
        try:
            carried = exporting_class.export_state(instance)
        except Exception as exc:
            result.warnings.append(
                f"{klass.__name__}.export_state raised on "
                f"{instance.db_key.redis_key}: {type(exc).__name__}: {exc}"
            )
            continue
        if carried is not None:
            state[klass.__name__] = to_jsonable(carried)
    return state


def _matches_client_filters(
    instance: "Model", client_filters: "dict[str, Any]"
) -> bool:
    """Re-apply the query layer's post-hydration equality filters."""
    for field_name, expected in client_filters.items():
        if getattr(instance, field_name, None) != expected:
            return False
    return True


def export_records(
    model_class: "type[Model]",
    *q_objects: Any,
    stream: "TextIO | None" = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    **filters: Any,
) -> ExportResult:
    """Export a model's records as JSON Lines.

    Writes a manifest line followed by one line per record. Filter arguments
    are forwarded verbatim to ``model_class.query.filter(...)``; with no
    arguments the model's full key set is exported. An unknown filter
    parameter raises ``QueryException`` from the query layer rather than being
    quietly ignored.

    Export is deliberately not a point-in-time snapshot. The key set is
    resolved once and hydrated in chunks, so a record deleted in between is
    counted as :attr:`ExportResult.vanished` and omitted, and a record created
    afterwards is simply absent. ``matched_count`` is recorded at resolution
    time so the gap is visible.

    Args:
        model_class: The Model class to export.
        *q_objects: ``Q`` / ``Expression`` objects forwarded to ``filter()``.
        stream: A text file-like object to write to. When ``None``, the JSONL
            text is returned on :attr:`ExportResult.data` instead.
        chunk_size: Keys hydrated per round trip. Bounds peak memory.
        **filters: Plain keyword filters forwarded to ``filter()``.

    Returns:
        An :class:`ExportResult` with counts, filter provenance, and warnings.

    Raises:
        QueryException: If a filter parameter matches no known field.

    Example:
        with open("memories.jsonl", "w") as fh:
            result = export_records(Memory, project_key="ai", stream=fh)
        print(result.summary())
    """
    from ..models.query import Query

    own_buffer = None
    if stream is None:
        own_buffer = io.StringIO()
        stream = own_buffer

    query = model_class.query
    result = ExportResult(model=model_class.__name__)

    if q_objects or filters:
        builder = query.filter(*q_objects, **filters)
        # Never QueryBuilder.__repr__ -- it prints _filters only and drops
        # every Q object, which would render `(Q(a=1) OR Q(b=2))` as `{}`.
        builder_q_objects = list(builder._q_objects)
        builder_filters = dict(builder._filters)
        result.filter = _render_filter(builder_q_objects, builder_filters)
        result.filter_kwargs = {
            key: to_jsonable(value) for key, value in builder_filters.items()
        }
        # Evaluation happens after provenance capture, so a zero-match filter
        # is still fully reported. Unknown params raise from here.
        resolved = query._evaluate_filter_args(builder_q_objects, builder_filters)
        client_filters = dict(getattr(query, "_pending_client_filters", None) or {})
        if client_filters:
            result.warnings.append(
                f"client-side (unindexed) equality filter applied after "
                f"hydration for: {', '.join(sorted(client_filters))}; "
                f"matched_count counts keys before this filter"
            )
    else:
        resolved = set(query.keys())
        client_filters = {}

    keys = sorted(_as_str(key) for key in resolved)
    result.matched_count = len(keys)

    stream.write(
        dump_line(
            build_manifest(
                model_name=model_class.__name__,
                filter_repr=result.filter,
                filter_kwargs=result.filter_kwargs,
                matched_count=result.matched_count,
                fields=collect_field_policies(model_class),
                mixins=collect_mixin_policies(model_class),
                embedding_provenance=collect_embedding_provenance(model_class),
            )
        )
    )

    chunk_size = max(1, int(chunk_size))
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start : start + chunk_size]
        instances = Query.get_many_objects(model_class, set(chunk))
        # get_many_objects silently drops keys whose hash is gone.
        result.vanished += len(chunk) - len(instances)

        for instance in instances:
            if client_filters and not _matches_client_filters(instance, client_filters):
                result.filtered_out += 1
                continue
            redis_key = instance.db_key.redis_key
            try:
                record = {
                    "key": redis_key,
                    "values": to_jsonable(_record_values(model_class, instance)),
                    "state": _field_state(model_class, instance, result),
                    "model_state": _model_state(instance, result),
                }
            except Exception as exc:
                result.errors.append(
                    f"{redis_key}: {type(exc).__name__}: {exc}",
                )
                continue
            stream.write(dump_line(record))
            result.record_count += 1

    if own_buffer is not None:
        result.data = own_buffer.getvalue()

    return result
