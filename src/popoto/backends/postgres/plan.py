"""SQL rendering of query plans for the Postgres backend (#759 M1b).

Two steps, kept apart on purpose:

1. :func:`popoto.backends.planning.plan_from_call` (backend-neutral, run by
   the query layer for every non-Redis backend) turns the public call (:class:`QueryCall`, the call
   exactly as ``Query.filter``/``count`` received it) into a protocol
   :class:`QueryPlan` -- ``where``, ``order_by``, ``limit``, ``project`` --
   while reproducing the query layer's *validation* (the same
   ``QueryException`` text for an unknown parameter, a non-boolean
   ``__isnull``, a malformed ``__between``, a partitioned ``SortedField``
   queried without its partition) and its *ordering rules* (explicit
   ``order_by`` > ``Meta.order_by`` > the first filtered ``SortedField``'s
   score order). On Redis those rules live in the moved filter body; here they
   become plan terms.
2. :func:`render_where` / :func:`render_order` (this module) turn plan terms
   into SQL over a :class:`~popoto.backends.postgres.schema.TableSpec`.

Null handling mirrors Redis's set algebra, not SQL's three-valued logic: a
negated subtree renders as ``NOT coalesce(…, false)``, so ``~Q(x=1)``
includes rows whose ``x`` is ``NULL`` exactly as ``all_keys - matches`` does.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any, Optional

from ..types import And, Cond, Not, Op, Or, OrderTerm, Predicate
from .codec import encode_json
from .schema import (
    INDEXED_KINDS,
    KEY_KINDS,
    RELATIONSHIP_KINDS,
    SORTED_KINDS,
    TAG_KINDS,
    TableSpec,
    quote_ident,
)

__all__ = ["render_order", "render_where", "to_db_value"]

#: Kinds whose Redis lookup is by the value's key string (``DB_key``), so
#: ``filter(code=5)`` finds ``code="5"``: key fields, indexed fields and the
#: related key a ``Relationship`` stores.
_KEY_STRING_KINDS = KEY_KINDS | INDEXED_KINDS | RELATIONSHIP_KINDS


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _query_exception(message: str) -> Exception:
    from ...models.query import QueryException

    return QueryException(message)


# -- step 2: plan terms -> SQL ------------------------------------------------


def to_db_value(py_type: type, value: Any) -> Any:
    """A Python value as the column wants it. A naive ``datetime`` is UTC --
    the same instant ``SortedFieldMixin.convert_to_numeric`` scores it as
    (#519) -- so comparisons and ordering agree with Redis."""
    if value is None:
        return None
    if py_type is datetime.datetime and isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value
    return value


def to_column_value(ts: TableSpec, name: str, value: Any) -> Any:
    """``value`` of field ``name`` as its column stores it (M1.1): the
    collections as ``jsonb`` (:mod:`.codec`), a ``TagField`` as its
    normalised ``text[]``, a ``Relationship`` as the target's key string."""
    py_type = ts.field_types[name]
    kind = ts.kind(name)
    if kind in TAG_KINDS:
        from ...fields.tag_field import TagFieldMixin

        return TagFieldMixin._normalize(getattr(value, "_data", value))
    if value is None:
        return None
    if kind in RELATIONSHIP_KINDS:
        return value if isinstance(value, str) else value.db_key.redis_key
    if ts.is_json(name):
        return _jsonb(encode_json(py_type, value, capped=name in ts.capped_fields))
    if py_type is bytes and isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return to_db_value(py_type, value)


_NO_MATCH = object()


def _key_string_value(py_type: type, value: str) -> Any:
    """The typed value whose key string is ``value``, or :data:`_NO_MATCH`.

    An ``IndexedField`` lookup on Redis is ``SMEMBERS`` of the set named by
    the filter value's key string (``canonical_key_str``), so a string filter
    finds the rows whose stored value renders as exactly that string:
    ``b="True"`` finds ``True`` and ``dte="2026-01-01"`` finds that date. The
    parse is accepted only when it renders back byte for byte, so ``"true"``
    or ``"2026-1-1"`` match nothing here as they match no set on Redis.
    """
    from ...models.canonical_key import canonical_key_str

    parsed: Any
    try:
        if py_type is bool:
            parsed = {"True": True, "False": False}[value]
        elif py_type is datetime.date:
            parsed = datetime.date.fromisoformat(value)
        elif py_type is datetime.datetime:
            text = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = to_db_value(py_type, datetime.datetime.fromisoformat(text))
        else:
            return _NO_MATCH
    except (KeyError, ValueError):
        return _NO_MATCH
    return parsed if canonical_key_str(parsed) == value else _NO_MATCH


def _coerce(
    ts: TableSpec, kind: str, field_name: str, value: Any, exact: bool = True
) -> Any:
    """Coerce a filter value to the column type, or :data:`_NO_MATCH`.

    A ``KeyField`` matches by its key string on Redis (``filter(code=5)``
    finds ``code="5"``), so key values are converted; a plain field is
    compared by Python equality on Redis, so a value of another type simply
    matches nothing rather than raising a SQL type error.

    An ``IndexedField`` string filter on a ``bool``, ``date`` or ``datetime``
    column is parsed when it is the stored value's key string
    (:func:`_key_string_value`). Numeric columns compare by value, as the
    save path's ``field.type(value)`` coercion does (``i=1.0`` finds ``1``);
    Redis compares key strings there, a documented divergence.

    ``exact`` is false for a range bound. An aware ``time`` never equals a
    stored one -- a ``time`` column holds wall-clock time only, and Python
    equality between an aware and a naive ``time`` is ``False`` -- so an
    exact match on one matches nothing.
    """
    py_type = ts.field_types[field_name]
    if value is None or isinstance(value, bool) and py_type is bool:
        return value
    if (
        kind in INDEXED_KINDS
        and isinstance(value, str)
        and py_type in (bool, datetime.date, datetime.datetime)
    ):
        return _key_string_value(py_type, value)
    if exact and isinstance(value, datetime.time) and value.tzinfo is not None:
        return _NO_MATCH
    if py_type is str:
        if isinstance(value, str):
            return value
        return str(value) if kind in _KEY_STRING_KINDS else _NO_MATCH
    if py_type in (int, float, decimal.Decimal):
        if isinstance(value, (int, float, decimal.Decimal)) and not isinstance(
            value, bool
        ):
            return value
        if isinstance(value, bool):
            return int(value)
        if kind in _KEY_STRING_KINDS and isinstance(value, str):
            try:
                return py_type(value)
            except (ValueError, decimal.InvalidOperation):
                return _NO_MATCH
        return _NO_MATCH
    if py_type is datetime.datetime:
        if isinstance(value, datetime.datetime):
            return to_db_value(py_type, value)
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
        return _NO_MATCH
    if py_type is datetime.date:
        if isinstance(value, datetime.date) and not isinstance(
            value, datetime.datetime
        ):
            return value
        return _NO_MATCH
    if py_type is datetime.time:
        if isinstance(value, datetime.time):
            return value.replace(tzinfo=None)
        return _NO_MATCH
    if py_type is bytes:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return _NO_MATCH
    if py_type is bool:
        return _NO_MATCH
    return value


def _tag_values(value: Any) -> list[str]:
    """Tag lookup values as the strings ``TagFieldMixin._normalize`` stores
    (``str(tag)``; on Redis the lookup is ``DB_key(prefix, tag)``, the same
    rendering)."""
    return [str(v) for v in value]


def _tag_sql(field_name: str, col: str, op: Op, value: Any, params: list[Any]) -> str:
    """``TagField`` lookups on its ``text[]`` column, each GIN-served:
    ``__contains`` is ``@> ARRAY[v]``, ``__all`` is ``@>``, ``__any`` is
    ``&&``. An empty ``__any``/``__all`` matches nothing, as the empty
    ``SUNION``/``SINTER`` short-circuit does on Redis."""
    if op is Op.CONTAINS:
        params.append(_tag_values([value]))
        return f"{col} @> %s::text[]"
    if op in (Op.ANY, Op.ALL):
        items = _tag_values(value or ())
        if not items:
            return "FALSE"
        params.append(items)
        return f"{col} {'&&' if op is Op.ANY else '@>'} %s::text[]"
    if op is Op.EXACT:
        # TagFieldMixin raises popoto.exceptions.QueryException for this.
        from ...exceptions import QueryException as FieldQueryException

        raise FieldQueryException(
            f"Exact-match filter on TagField '{field_name}' is not "
            f"supported; use {field_name}__contains (membership), "
            f"{field_name}__any (any-of) or {field_name}__all (all-of)."
        )
    raise _query_exception(f"Invalid filter parameters: {field_name}__{op.value}")


def _json_sql(ts: TableSpec, c: Cond, col: str, params: list[Any]) -> str:
    """Equality on a collection (``jsonb``) column -- what Redis does by
    Python equality after hydration. A set compares as a set (containment
    both ways), every other collection by its encoded document."""
    py_type = ts.field_types[c.field]

    def one(value: Any) -> str:
        if value is None:
            return f"{col} IS NULL"
        if not isinstance(value, (list, tuple, set, dict)):
            return "FALSE"
        if py_type is set and not isinstance(value, set):
            return "FALSE"
        doc = _jsonb(encode_json(py_type, value, capped=c.field in ts.capped_fields))
        if py_type is set:
            params.extend((doc, doc))
            return f"({col} @> %s AND {col} <@ %s)"
        params.append(doc)
        return f"{col} = %s"

    if c.op is Op.EXACT:
        return one(c.value)
    if c.op is Op.IN:
        items = list(c.value or ())
        return "(" + " OR ".join(one(v) for v in items) + ")" if items else "FALSE"
    raise _query_exception(
        f"lookup __{c.op.value} is not supported on a collection field"
    )


def _bind(py_type: type, value: Any) -> Any:
    """One ``__in`` element as the column's Python type, so the list binds as
    one array (psycopg refuses a list of mixed types). An integer column
    drops a non-integral number, which equals no ``bigint``."""
    if value is _NO_MATCH or isinstance(value, bool):
        return value
    if py_type is int and isinstance(value, (float, decimal.Decimal)):
        return int(value) if value == int(value) else _NO_MATCH
    if py_type is float and isinstance(value, (int, decimal.Decimal)):
        return float(value)
    if py_type is decimal.Decimal and isinstance(value, (int, float)):
        return decimal.Decimal(str(value))
    return value


def _key_string_sql(py_type: type, col: str) -> str:
    """``col`` rendered as the key string Redis stores for it
    (``canonical_key_str``, then glob-matched by ``__startswith`` /
    ``__endswith``): ``True``, ``1.0``, ``2026-01-01T12:00:00.000000Z``.

    Exact for ``str``, ``int``, ``bool``, ``date`` and ``time``. A float in
    ``1e15 <= |x| < 1e16`` and a ``Decimal`` Python renders in exponent form
    (``1E+2``) differ, a documented divergence: Postgres switches to exponent
    form at ``1e15`` and never uses it for ``numeric``."""
    from ...fields.constants import Defaults

    if py_type is bool:
        return f"(CASE {col} WHEN true THEN 'True' WHEN false THEN 'False' END)"
    if py_type is float:
        return (
            f"(CASE WHEN {col} = 'Infinity' THEN 'inf'"
            f" WHEN {col} = '-Infinity' THEN '-inf'"
            f" WHEN {col} = 'NaN' THEN 'nan'"
            f" WHEN {col}::text ~ '^-?[0-9]+$' THEN {col}::text || '.0'"
            f" ELSE {col}::text END)"
        )
    if py_type is datetime.date:
        return f"to_char({col}, 'YYYY-MM-DD')"
    if py_type is datetime.datetime and not Defaults.DATETIME_KEY_LEGACY:
        return (
            f"to_char({col} AT TIME ZONE 'UTC'," f' \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
        )
    if py_type is str:
        return col
    return f"{col}::text"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cond_sql(ts: TableSpec, kinds: dict[str, str], c: Cond, params: list[Any]) -> str:
    if c.field not in ts.field_types:
        raise _query_exception(f"Invalid filter parameters: {c.field}")
    col = quote_ident(c.field)
    kind = kinds.get(c.field, "Field")
    sql_type = ts.column_map()[c.field]
    op = c.op
    if ts.is_tag(c.field):
        return _tag_sql(c.field, col, op, c.value, params)
    if op is Op.ISNULL:
        return f"{col} IS NULL" if c.value else f"{col} IS NOT NULL"
    if ts.is_json(c.field):
        return _json_sql(ts, c, col, params)
    if op is Op.EXACT:
        value = _coerce(ts, kind, c.field, c.value)
        if value is _NO_MATCH:
            return "FALSE"
        if value is None:
            return f"{col} IS NULL"
        params.append(value)
        return f"{col} = %s"
    if op is Op.IN:
        items = list(c.value or ())
        has_none = any(v is None for v in items)
        py_type = ts.field_types[c.field]
        coerced = [
            _bind(py_type, _coerce(ts, kind, c.field, v))
            for v in items
            if v is not None
        ]
        coerced = [v for v in coerced if v is not _NO_MATCH]
        clauses = []
        if coerced:
            params.append(coerced)
            clauses.append(f"{col} = ANY(%s::{sql_type}[])")
        if has_none:
            clauses.append(f"{col} IS NULL")
        return "(" + " OR ".join(clauses) + ")" if clauses else "FALSE"
    if op in (Op.STARTSWITH, Op.ENDSWITH, Op.CONTAINS):
        text = _like_escape(str(c.value))
        pattern = {
            Op.STARTSWITH: text + "%",
            Op.ENDSWITH: "%" + text,
            Op.CONTAINS: "%" + text + "%",
        }[op]
        params.append(pattern)
        if kind in _KEY_STRING_KINDS:
            target = _key_string_sql(ts.field_types[c.field], col)
        else:
            target = col if sql_type == "text" else f"{col}::text"
        return f"{target} LIKE %s"
    if op in (Op.GT, Op.GTE, Op.LT, Op.LTE):
        value = _range_value(ts, kind, c.field, c.value)
        if value is _NO_MATCH:
            return "FALSE"
        params.append(value)
        symbol = {Op.GT: ">", Op.GTE: ">=", Op.LT: "<", Op.LTE: "<="}[op]
        return f"{col} {symbol} %s"
    if op is Op.BETWEEN:
        low, high = c.value
        low = _range_value(ts, kind, c.field, low)
        high = _range_value(ts, kind, c.field, high)
        if low is _NO_MATCH or high is _NO_MATCH:
            return "FALSE"
        params.extend((low, high))
        return f"{col} BETWEEN %s AND %s"
    raise _query_exception(f"lookup __{op.value} is not supported on Postgres yet")


def _range_value(ts: TableSpec, kind: str, field_name: str, value: Any) -> Any:
    """A range bound. ``±inf`` passes through for numeric columns (Postgres
    compares ``bigint``/``numeric`` against ``'Infinity'::float8``), which is
    what makes ``score__lte=inf`` behave as it does on Redis.

    On a ``SortedField`` a numeric *string* bound is parsed, because Redis
    sends the bound to ``ZRANGEBYSCORE`` as text and the server parses it
    (``score__lte="3"`` matches on both). ``None`` or a non-numeric string
    matches nothing here, where Redis raises ``ResponseError: min or max is
    not a float`` (a documented divergence)."""
    if value is None:
        return _NO_MATCH
    py_type = ts.field_types[field_name]
    if (
        kind in SORTED_KINDS
        and isinstance(value, str)
        and py_type in (int, float, decimal.Decimal)
    ):
        try:
            return (
                decimal.Decimal(value) if py_type is decimal.Decimal else float(value)
            )
        except (ValueError, decimal.InvalidOperation):
            return _NO_MATCH
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        if ts.field_types[field_name] in (int, float, decimal.Decimal):
            return value
    return _coerce(ts, kind, field_name, value, exact=False)


def _pred_sql(
    ts: TableSpec, kinds: dict[str, str], p: Predicate, params: list[Any]
) -> str:
    """Render ``p``. Outside a ``NOT`` plain SQL is already right: a ``NULL``
    leaf leaves the row out, as a set Redis never put the key in does, and
    ``NULL OR TRUE`` is ``TRUE``. Only negation needs care -- ``NOT NULL`` is
    ``NULL``, where Redis's ``all_keys - matches`` keeps the row -- so a
    negated subtree is ``NOT coalesce(…, false)``. Keeping positive leaves
    bare is what lets their B-trees serve the query."""
    if isinstance(p, Cond):
        return _cond_sql(ts, kinds, p, params)
    if isinstance(p, Not):
        return f"NOT coalesce(({_pred_sql(ts, kinds, p.item, params)}), false)"
    if isinstance(p, And):
        if not p.items:
            return "TRUE"
        return (
            "(" + " AND ".join(_pred_sql(ts, kinds, i, params) for i in p.items) + ")"
        )
    if isinstance(p, Or):
        if not p.items:
            return "FALSE"
        return "(" + " OR ".join(_pred_sql(ts, kinds, i, params) for i in p.items) + ")"
    raise TypeError(f"not a predicate: {p!r}")  # pragma: no cover


def render_where(
    ts: TableSpec, kinds: dict[str, str], where: Optional[Predicate]
) -> tuple[str, list[Any]]:
    """``(" WHERE …", params)``, or ``("", [])`` for no predicate."""
    if where is None:
        return "", []
    params: list[Any] = []
    return " WHERE " + _pred_sql(ts, kinds, where, params), params


def _collection_order_error(field_name: str) -> Exception:
    """``order_by`` on a collection (``jsonb``) column is refused. Redis sorts
    the hydrated values as Python does (``[1, 2] < [2]``) and raises
    ``TypeError`` for mixed or ``dict`` elements; ``jsonb`` orders by length
    first (``[2] < [1, 2]``), so a silently different order is the only
    alternative. Sort the results in Python if you need it."""
    from ..types import BackendCapabilityError

    return BackendCapabilityError(
        f"order_by={field_name!r}: ordering by a collection field is not "
        "supported on Postgres (jsonb order differs from Python's); sort the "
        "results in Python"
    )


_ZERO = {
    str: "''",
    int: "0",
    float: "0",
    decimal.Decimal: "0",
    bool: "false",
}


_NON_NULL_OPS = frozenset({Op.GT, Op.GTE, Op.LT, Op.LTE, Op.BETWEEN})


def non_null_fields(where: Optional[Predicate]) -> frozenset[str]:
    """Fields the predicate guarantees are not ``NULL``: a range lookup (or a
    non-``None`` exact match) in the top-level conjunction."""
    items = where.items if isinstance(where, And) else (where,) if where else ()
    out = set()
    for item in items:
        if isinstance(item, Cond) and (
            item.op in _NON_NULL_OPS or (item.op is Op.EXACT and item.value is not None)
        ):
            out.add(item.field)
    return frozenset(out)


def render_order(
    ts: TableSpec,
    terms: tuple[OrderTerm, ...],
    not_null: frozenset[str] = frozenset(),
) -> str:
    """``ORDER BY`` for ``terms`` plus the deterministic tie-break
    ``_pk COLLATE "C"`` (Redis's bytewise member order).

    A ``NULL`` sorts as the type's zero value, as ``prepare_results``'s
    ``getattr(obj, f) or type()`` does; a descending primary term reverses
    the whole order, as ``list(reversed(…))`` does on Redis. For a field in
    ``not_null`` (the WHERE already excludes its ``NULL`` rows) the bare
    column is ordered, so its B-tree can serve the ``ORDER BY … LIMIT``.
    """
    parts = []
    descending = bool(terms) and terms[0].descending
    for term in terms:
        if term.random:
            parts.append("random()")
            continue
        assert term.field is not None
        if ts.is_json(term.field):
            raise _collection_order_error(term.field)
        col = quote_ident(term.field)
        py_type = ts.field_types[term.field]
        zero = _ZERO.get(py_type)
        known_not_null = term.field in not_null
        if known_not_null:
            zero = None
        expr = f"coalesce({col}, {zero})" if zero is not None else col
        if py_type is str:
            expr += ' COLLATE "C"'
        direction = "DESC" if term.descending else "ASC"
        nulls = ""
        if zero is None and not known_not_null:
            nulls = " NULLS LAST" if term.descending else " NULLS FIRST"
        parts.append(f"{expr} {direction}{nulls}")
    parts.append(f'"_pk" COLLATE "C" {"DESC" if descending else "ASC"}')
    return " ORDER BY " + ", ".join(parts)
