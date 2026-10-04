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
import re
from typing import Any, Optional

from ..types import And, Cond, Not, Op, Or, OrderTerm, Predicate
from .schema import KEY_KINDS, SORTED_KINDS, TableSpec, quote_ident

__all__ = ["render_order", "render_where", "to_db_value"]


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


_NO_MATCH = object()


def _coerce(ts: TableSpec, kind: str, field_name: str, value: Any) -> Any:
    """Coerce a filter value to the column type, or :data:`_NO_MATCH`.

    A ``KeyField`` matches by its key string on Redis (``filter(code=5)``
    finds ``code="5"``), so key values are converted; a plain field is
    compared by Python equality on Redis, so a value of another type simply
    matches nothing rather than raising a SQL type error.
    """
    py_type = ts.field_types[field_name]
    if value is None or isinstance(value, bool) and py_type is bool:
        return value
    if py_type is str:
        if isinstance(value, str):
            return value
        return str(value) if kind in KEY_KINDS else _NO_MATCH
    if py_type in (int, float, decimal.Decimal):
        if isinstance(value, (int, float, decimal.Decimal)) and not isinstance(
            value, bool
        ):
            return value
        if isinstance(value, bool):
            return int(value)
        if kind in KEY_KINDS and isinstance(value, str):
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
    if py_type is datetime.date and isinstance(value, datetime.date):
        return value
    if py_type is datetime.time and isinstance(value, datetime.time):
        return value.replace(tzinfo=None)
    if py_type is bool:
        return _NO_MATCH
    return value


_NUMERIC_TYPES = (int, float, decimal.Decimal)


def _key_value_unbounded(ts: TableSpec, kind: str, field_name: str, value: Any) -> Any:
    """An equality / ``__in`` value for a key field, with Redis's semantics.

    On Redis a key field matches by its *key string*: ``str(value)`` against
    the ``str()`` of the stored value. For a numeric key column that means a
    value matches only when its string is the canonical string of a value of
    the column's type: on ``KeyField(type=int)``, ``1`` and ``"1"`` match 1,
    but ``1.0`` and ``1.5`` match nothing; on ``KeyField(type=float)``, ``1``
    matches nothing (``"1" != "1.0"``). Every value that survives is of the
    column's type, so an ``__in`` list binds as one homogeneous array.

    A ``SortedKeyField`` is the exception: Redis matches it by *score*, a
    numeric comparison (``code=2.0`` finds 2, ``"01"`` finds 1), so it takes
    the plain coercion.
    """
    py_type = ts.field_types[field_name]
    if kind in SORTED_KINDS and isinstance(value, str) and py_type in _NUMERIC_TYPES:
        # Matched by score: Redis parses the string as a bound would be.
        return _score_bound(value, py_type is decimal.Decimal)
    if (
        value is None
        or kind not in KEY_KINDS
        or kind in SORTED_KINDS  # matched by score, not by key string
        or py_type not in _NUMERIC_TYPES
    ):
        return _coerce(ts, kind, field_name, value)
    text = str(value)
    try:
        parsed = py_type(text)
    except (ValueError, decimal.InvalidOperation):
        return _NO_MATCH
    return parsed if str(parsed) == text else _NO_MATCH


def _key_value(ts: TableSpec, kind: str, field_name: str, value: Any) -> Any:
    """:func:`_key_value_unbounded`, with an out-of-range integer matching
    nothing (Redis finds no such key; ``bigint`` would raise
    ``NumericValueOutOfRange``)."""
    result = _key_value_unbounded(ts, kind, field_name, value)
    if (
        ts.field_types[field_name] is int
        and isinstance(result, int)
        and not isinstance(result, bool)
        and not -(2**63) <= result < 2**63
    ):
        return _NO_MATCH
    return result


_DECIMAL_BOUND = re.compile(
    r"[ \t\n\v\f\r]*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII
)
_SPECIAL_BOUND = re.compile(
    r"[ \t\n\v\f\r]*[+-]?(?:inf(?:inity)?|0x[0-9a-f]+(?:\.[0-9a-f]*)?(?:p[+-]?\d+)?)",
    re.ASCII | re.IGNORECASE,
)


def _score_bound(text: str, as_decimal: bool) -> Any:
    """Parse a string score bound as Redis does, or raise.

    For an ``int``/``float`` field ``ZRANGEBYSCORE`` takes the bound as text
    and the server parses it with ``strtod``, refusing what is left over or
    ``nan``: ``"nan"``, ``"1_0"``, ``"3 "`` and ``"abc"`` fail with
    ``min or max is not a float``, while leading whitespace, a sign, an
    exponent, ``inf``/``infinity`` and hex floats parse. Python's ``float()``
    is looser (underscores, trailing whitespace, ``nan``, non-ASCII digits),
    so the shape is checked first.

    A ``Decimal`` field converts the bound with Python's ``float()`` before
    it reaches the server, so ``"1_0"`` and ``"3 "`` parse and junk raises
    Python's own ``ValueError``; only ``nan`` reaches the server's error."""
    if as_decimal:
        number = float(text)
        if number != number:
            raise _query_exception("min or max is not a float")
        return decimal.Decimal(repr(number))
    if text == "":
        return 0.0
    if _DECIMAL_BOUND.fullmatch(text):
        return float(text)
    if _SPECIAL_BOUND.fullmatch(text):
        stripped = text.strip().lower()
        if "0x" in stripped.lstrip("+-"):
            return float.fromhex(stripped)
        return float(stripped)
    raise _query_exception("min or max is not a float")


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cond_sql(ts: TableSpec, kinds: dict[str, str], c: Cond, params: list[Any]) -> str:
    if c.field not in ts.field_types:
        raise _query_exception(f"Invalid filter parameters: {c.field}")
    col = quote_ident(c.field)
    kind = kinds.get(c.field, "Field")
    sql_type = ts.column_map()[c.field]
    op = c.op
    if op is Op.ISNULL:
        return f"{col} IS NULL" if c.value else f"{col} IS NOT NULL"
    if op is Op.EXACT:
        value = _key_value(ts, kind, c.field, c.value)
        if value is _NO_MATCH:
            return "FALSE"
        if value is None:
            return f"{col} IS NULL"
        params.append(value)
        return f"{col} = %s"
    if op is Op.IN:
        items = list(c.value or ())
        has_none = any(v is None for v in items)
        coerced = [_key_value(ts, kind, c.field, v) for v in items if v is not None]
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
    (``score__lte="3"`` matches on both), as strictly as the server does: a
    string it refuses (``"nan"``, ``"1_0"``, ``"3 "``, ``"abc"``) raises
    ``QueryException("min or max is not a float")`` here, where Redis raises
    ``ResponseError`` with that text (same message, different class). ``None``
    matches nothing here, where Redis raises (a documented divergence)."""
    if value is None:
        return _NO_MATCH
    py_type = ts.field_types[field_name]
    if (
        kind in SORTED_KINDS
        and isinstance(value, str)
        and py_type in (int, float, decimal.Decimal)
    ):
        return _score_bound(value, py_type is decimal.Decimal)
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        if ts.field_types[field_name] in (int, float, decimal.Decimal):
            return value
    return _coerce(ts, kind, field_name, value)


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
