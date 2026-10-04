"""Compile a public query call into a protocol :class:`QueryPlan` (#759 M1b).

``Query.filter``/``count`` hand a backend a :class:`QueryPlan`. The Redis
backend replays :attr:`QueryPlan.source` through its moved filter body, so for
Redis the query layer sends ``source`` alone, exactly as in M1a. Every other
backend compiles ``WHERE``/``ORDER BY`` from the plan's own terms, so for
those the query layer calls :func:`plan_from_call`, which populates
``where``, ``order_by``, ``limit`` and ``project`` from the call -- and keeps
``source`` for reference.

It reproduces, from the model's own filter vocabulary
(``_meta.filter_query_params_by_field``), what the Redis path decides:

* **validation**, with the same ``QueryException`` text -- an unknown
  parameter or an unknown ``__operator`` (``name__bogus``) is
  ``Invalid filter parameters: …``, never an exact match on a field of that
  name; a non-boolean ``__isnull``; a malformed ``__between``; a partitioned
  ``SortedField`` queried without its partition; a non-tuple ``values=``; an
  ``order_by`` that is not a field, or is missing from ``values=``.
* **ordering**: explicit ``order_by`` > ``Meta.order_by`` > the first
  filtered ``SortedField``'s score order (``ZRANGEBYSCORE`` order, which the
  Redis path uses when nothing else orders the result, and which survives as
  ``Meta.order_by``'s tie-break because Python's sort is stable). A
  descending primary term reverses the tie-breaks too, as
  ``list(reversed(...))`` does.
* **plain fields**: an unindexed field compared by equality, which Redis does
  client-side after hydration, is an ordinary ``Cond``.

Pure: no network, no backend driver.
"""

from __future__ import annotations

from typing import Any, Optional

from .types import And, Cond, Not, Op, Or, OrderTerm, Predicate, QueryCall, QueryPlan

__all__ = ["RESULT_MODIFIERS", "TRUE", "plan_from_call", "has_filters"]

RESULT_MODIFIERS = ("limit", "order_by", "values")
_SUFFIX_OPS = {
    "in": Op.IN,
    "isnull": Op.ISNULL,
    "startswith": Op.STARTSWITH,
    "endswith": Op.ENDSWITH,
    "contains": Op.CONTAINS,
    "gt": Op.GT,
    "gte": Op.GTE,
    "lt": Op.LT,
    "lte": Op.LTE,
    "between": Op.BETWEEN,
}

TRUE: Predicate = And(())
"""Matches everything (an empty ``Q()``)."""


def _query_exception(message: str) -> Exception:
    from ..models.query import QueryException

    return QueryException(message)


class _Params:
    """The model's filter vocabulary, as ``Query.filter_for_keys_set`` sees it."""

    def __init__(self, meta: Any) -> None:
        self.meta = meta
        self.owner: dict[str, str] = {}
        for field_name, params in meta.filter_query_params_by_field.items():
            for param in params:
                self.owner.setdefault(param, field_name)

    def cond(self, param: str, value: Any) -> Cond:
        field_name = self.owner.get(param)
        if field_name is None:
            if param in self.meta.fields:
                # A plain (unindexed) field: Redis filters it client-side by
                # equality; here it is an ordinary WHERE.
                return Cond(param, Op.EXACT, value)
            raise _query_exception(f"Invalid filter parameters: {param}")
        if param == field_name:
            return Cond(field_name, Op.EXACT, value)
        suffix = param[len(field_name) + 2 :]
        op = _SUFFIX_OPS.get(suffix)
        if op is None:  # pragma: no cover - a vocabulary we do not know
            raise _query_exception(f"Invalid filter parameters: {param}")
        if op is Op.ISNULL and value is not True and value is not False:
            raise _query_exception(f"{param} filter must be True or False")
        if op is Op.BETWEEN and (
            not isinstance(value, (tuple, list)) or len(value) != 2
        ):
            raise _query_exception(
                f"{field_name}__between requires a tuple or list of exactly "
                f"2 elements (low, high), got {value!r}"
            )
        return Cond(field_name, op, value)

    def leaf(self, filters: dict[str, Any]) -> list[Predicate]:
        params = [k for k in filters if k not in RESULT_MODIFIERS]
        unknown = [
            p for p in params if p not in self.owner and p not in self.meta.fields
        ]
        if unknown:
            raise _query_exception(f"Invalid filter parameters: {','.join(unknown)}")
        self._check_partitions(params, filters)
        return [self.cond(p, filters[p]) for p in params]

    def _check_partitions(self, params: list[str], filters: dict[str, Any]) -> None:
        for field_name in self.meta.sorted_field_names:
            field = self.meta.fields[field_name]
            partition = tuple(getattr(field, "partition_by", ()) or ())
            if not partition:
                continue
            if not set(params) & set(
                self.meta.filter_query_params_by_field[field_name]
            ):
                continue
            if any(name not in filters for name in partition):
                raise _query_exception(
                    f"{field_name} field is sorted on {', '.join(partition)}. "
                    f"Query filter must also specify a value for "
                    f"{', '.join(partition)}"
                )

    def sorted_default(self, filters: dict[str, Any]) -> Optional[str]:
        """The first ``SortedField`` the filters use: Redis returns its
        ``ZRANGEBYSCORE`` order when nothing else orders the result."""
        used = set(filters)
        for field_name in self.meta.sorted_field_names:
            if used & set(self.meta.filter_query_params_by_field[field_name]):
                return field_name
        return None


def _q_predicate(params: _Params, q: Any) -> Predicate:
    if q.is_empty():
        node: Predicate = TRUE
    elif q.is_leaf():
        leaves = params.leaf(dict(q.filters))
        node = leaves[0] if len(leaves) == 1 else And(tuple(leaves))
    elif q.children:
        children = tuple(_q_predicate(params, child) for child in q.children)
        node = Or(children) if q.connector == "OR" else And(children)
    else:
        node = TRUE
    return Not(node) if q.negated else node


def plan_from_call(call: QueryCall) -> QueryPlan:
    """Validate ``call`` the way the query layer does and compile it."""
    meta = call.query.model_class._meta
    params = _Params(meta)
    kwargs = dict(call.kwargs)
    parts: list[Predicate] = params.leaf(kwargs)
    parts.extend(_q_predicate(params, q) for q in (call.q_objects or ()))
    where: Optional[Predicate]
    if not parts:
        where = None
    elif len(parts) == 1:
        where = parts[0]
    else:
        where = And(tuple(parts))
    if call.kind == "count":
        return QueryPlan(where=where, source=call)

    values = kwargs.get("values")
    if values is not None and not isinstance(values, tuple):
        raise _query_exception(
            "values takes a tuple. eg. query.filter(values=('name',))"
        )
    order_by = kwargs.get("order_by") or meta.order_by or ""
    terms: list[OrderTerm] = []
    if order_by:
        descending = isinstance(order_by, str) and order_by.startswith("-")
        name = order_by[1:] if descending else order_by
        if not isinstance(name, str) or name not in meta.fields:
            raise _query_exception(f"order_by={name} must be a field name (str)")
        if values and name not in values:
            raise _query_exception(
                "field must be included in values=(fieldnames) in order to use "
                "order_by"
            )
        terms.append(OrderTerm(name, descending))
    if not kwargs.get("order_by") and not call.q_objects:
        # The sorted field's score order is the base order; Meta.order_by
        # re-sorts it stably, so it survives as the tie-break.
        sorted_name = params.sorted_default(kwargs)
        if sorted_name and not (terms and terms[0].field == sorted_name):
            terms.append(
                OrderTerm(sorted_name, terms[0].descending if terms else False)
            )
    limit = kwargs.get("limit")
    return QueryPlan(
        where=where,
        order_by=tuple(terms),
        limit=limit if isinstance(limit, int) and limit > 0 else None,
        project=tuple(values) if values else None,
        source=call,
    )


def has_filters(call: QueryCall) -> bool:
    """Whether ``call`` filters at all (anything beyond the result
    modifiers, or a ``Q`` object)."""
    return bool(call.q_objects) or any(k not in RESULT_MODIFIERS for k in call.kwargs)
