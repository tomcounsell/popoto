"""The populated query plan agrees with the call it was compiled from (#759 M1b).

For every non-Redis backend the query layer compiles ``QueryPlan.where`` /
``order_by`` / ``limit`` / ``project`` from the public call
(:func:`popoto.backends.planning.plan_from_call`); Redis keeps replaying
``QueryPlan.source``. These tests hold the two together on Redis, with no
Postgres involved: for each query shape the M1 gate-(b) files use, the Redis
result (the source replay) must equal a pure-Python evaluation of the compiled
plan over every stored record. A disagreement here is a planning bug that the
Postgres leg would otherwise only show as a parity failure.

Also: unknown fields and ``__operators`` are refused with Redis's own error
text (#768 review), and ``bind()`` is memoised per class, not per name.
"""

import math

import pytest

import popoto
from popoto import Q
from popoto.backends import (
    And,
    Cond,
    Not,
    Op,
    Or,
    QueryCall,
    compile_where,
    get_backend,
    set_backend,
)
from popoto.backends.planning import plan_from_call
from popoto.backends.redis import RedisBackend
from popoto.models.query import QueryException


class PlanItem(popoto.Model):
    cat = popoto.KeyField()
    sku = popoto.KeyField()
    price = popoto.SortedField(type=float)
    qty = popoto.SortedField(type=int, partition_by="cat")
    note = popoto.StringField(null=True)
    hits = popoto.IntField(default=0)


class PlanOrdered(popoto.Model):
    name = popoto.KeyField()
    rank = popoto.SortedField(type=int)
    label = popoto.StringField(null=True)

    class Meta:
        order_by = "-label"


ROWS = [
    ("a", "a1", 10.0, 1, "x", 3),
    ("a", "a2", 20.0, 2, None, 0),
    ("a", "a3", 30.5, 3, "y", 3),
    ("b", "b1", 5.0, 9, "x", 1),
    ("b", "b2", 25.0, 4, "z", 7),
    ("c", "c1", 99.0, 5, None, 2),
]


@pytest.fixture
def items():
    for cat, sku, price, qty, note, hits in ROWS:
        PlanItem.create(cat=cat, sku=sku, price=price, qty=qty, note=note, hits=hits)
    for i, (rank, label) in enumerate([(3, "m"), (1, "m"), (2, "a"), (5, None)]):
        PlanOrdered.create(name=f"n{i}", rank=rank, label=label)


# -- a reference evaluator for the protocol's plan terms -----------------------


def _match(obj, p):
    if isinstance(p, Not):
        return not _match(obj, p.item)
    if isinstance(p, And):
        return all(_match(obj, i) for i in p.items)
    if isinstance(p, Or):
        return any(_match(obj, i) for i in p.items)
    v = getattr(obj, p.field)
    op, want = p.op, p.value
    if op is Op.EXACT:
        return v == want if want is not None else v is None
    if op is Op.IN:
        return v in want
    if op is Op.ISNULL:
        return (v is None) is want
    if op is Op.STARTSWITH:
        return v is not None and str(v).startswith(want)
    if op is Op.ENDSWITH:
        return v is not None and str(v).endswith(want)
    if v is None:
        return False
    if op is Op.GT:
        return v > want
    if op is Op.GTE:
        return v >= want
    if op is Op.LT:
        return v < want
    if op is Op.LTE:
        return v <= want
    if op is Op.BETWEEN:
        return want[0] <= v <= want[1]
    raise AssertionError(op)


def _evaluate(model, plan):
    rows = [o for o in model.query.all() if plan.where is None or _match(o, plan.where)]
    rows.sort(key=lambda o: o.pk)
    terms = plan.order_by
    for term in reversed(terms):
        zero = model._meta.fields[term.field].type()
        rows.sort(key=lambda o, f=term.field, z=zero: getattr(o, f) or z)
    if terms and terms[0].descending:
        rows.reverse()
    if plan.limit:
        rows = rows[: plan.limit]
    if plan.project:
        return [{f: getattr(o, f) for f in plan.project} for o in rows]
    return [o.pk for o in rows]


def _redis(model, *q, ordered, **kwargs):
    result = model.query.filter(*q, **kwargs) if (q or kwargs) else model.query.all()
    result = list(result)
    out = [r if isinstance(r, dict) else r.pk for r in result]
    return out if ordered else sorted(out, key=repr)


SHAPES = [
    # (model, q-objects, kwargs, ordered?)
    ("PlanItem", (), {"cat": "a"}, False),
    ("PlanItem", (), {"cat__in": ["a", "c"]}, False),
    ("PlanItem", (), {"sku__startswith": "b"}, False),
    ("PlanItem", (), {"sku__endswith": "1"}, False),
    ("PlanItem", (), {"price__gte": 20.0}, True),
    ("PlanItem", (), {"price__gt": 20.0, "price__lt": 30.5}, True),
    ("PlanItem", (), {"price__lte": math.inf}, True),
    ("PlanItem", (), {"price__gte": -math.inf, "price__lt": math.inf}, True),
    ("PlanItem", (), {"price__between": (5.0, 25.0)}, True),
    ("PlanItem", (), {"qty__gte": 2, "cat": "a"}, True),
    ("PlanItem", (), {"price__gte": 0, "order_by": "-price", "limit": 3}, True),
    ("PlanItem", (), {"price__gte": 0, "values": ("sku", "price")}, True),
    ("PlanItem", (), {"note": "x"}, False),
    ("PlanItem", (), {"cat": "a", "order_by": "hits"}, False),
    ("PlanItem", (Q(cat="a") | Q(cat="b"),), {}, False),
    ("PlanItem", (Q(cat="a") & Q(price__gt=15.0),), {}, False),
    ("PlanItem", (~Q(cat="a"),), {}, False),
    ("PlanItem", (Q(cat="b") | ~Q(price__lt=25.0),), {"sku__startswith": "b"}, False),
    ("PlanItem", (Q(),), {}, False),
    ("PlanOrdered", (), {"rank__gte": 1}, True),
    ("PlanOrdered", (), {"name__in": ["n0", "n2"]}, True),
]


@pytest.mark.parametrize(
    "model_name,q,kwargs,ordered",
    SHAPES,
    ids=[f"{m}-{i}" for i, (m, *_rest) in enumerate(SHAPES)],
)
def test_populated_plan_agrees_with_the_source_replay(
    items, model_name, q, kwargs, ordered
):
    model = {"PlanItem": PlanItem, "PlanOrdered": PlanOrdered}[model_name]
    plan = plan_from_call(
        QueryCall(query=model.query, kind="filter", kwargs=kwargs, q_objects=q)
    )
    assert plan.source is not None and plan.source.kwargs == kwargs
    expected = _redis(model, *q, ordered=ordered, **kwargs)
    got = _evaluate(model, plan)
    if not ordered:
        got = sorted(got, key=repr)
    assert got == expected
    count = model.query.count(**{k: v for k, v in kwargs.items() if k != "values"})
    if not q and "limit" not in kwargs:
        count_plan = plan_from_call(
            QueryCall(query=model.query, kind="count", kwargs=kwargs)
        )
        assert count == len(_evaluate(model, count_plan))


def test_meta_order_by_keeps_the_sorted_order_as_tie_break(items):
    plan = plan_from_call(
        QueryCall(query=PlanOrdered.query, kind="filter", kwargs={"rank__gte": 0})
    )
    assert [(t.field, t.descending) for t in plan.order_by] == [
        ("label", True),
        ("rank", True),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [{"sku__bogus": "x"}, {"nope": 1}, {"price__contains": 1}],
    ids=["unknown-operator", "unknown-field", "operator-not-on-this-field"],
)
def test_unknown_lookups_raise_the_redis_error_on_both_paths(kwargs):
    """#768 review: an unknown ``__operator`` is refused, not read as an exact
    match on a field of that name -- and Redis's own behaviour is unchanged."""
    with pytest.raises(QueryException, match="Invalid filter parameters"):
        list(PlanItem.query.filter(**kwargs))  # Redis: the source replay
    call = QueryCall(query=PlanItem.query, kind="filter", kwargs=kwargs)
    with pytest.raises(QueryException, match="Invalid filter parameters"):
        plan_from_call(call)
    with pytest.raises(QueryException, match="Invalid filter parameters"):
        compile_where(call)


def test_compile_where_with_a_model_is_the_validated_compile():
    call = QueryCall(
        query=PlanItem.query,
        kind="filter",
        kwargs={"price__gte": 1.0},
        q_objects=(~Q(cat="a"),),
    )
    assert compile_where(call) == And(
        (Cond("price", Op.GTE, 1.0), Not(Cond("cat", Op.EXACT, "a")))
    )


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"cat__isnull": "yes"}, "must be True or False"),
        ({"price__between": (1,)}, "exactly 2 elements"),
        ({"qty__gte": 1}, "Query filter must also specify a value for cat"),
        ({"values": ["sku"]}, "values takes a tuple"),
        ({"order_by": "nope"}, "must be a field name"),
        ({"values": ("sku",), "order_by": "price"}, "must be included in values"),
    ],
)
def test_validation_matches_the_query_layer(kwargs, message):
    call = QueryCall(query=PlanItem.query, kind="filter", kwargs=kwargs)
    with pytest.raises(QueryException, match=message):
        plan_from_call(call)


def test_bind_is_memoised_per_class_not_per_name():
    """#768 review: two classes sharing a name (two modules) each bind."""
    bound = []

    class Recording(RedisBackend):
        def bind(self, spec):
            bound.append(spec)
            return super().bind(spec)

    def declare(module):
        return type(
            "PlanTwin",
            (popoto.Model,),
            {"__module__": module, "key": popoto.KeyField()},
        )

    one, two = declare("pkg.one"), declare("pkg.two")
    previous = set_backend(Recording())
    try:
        get_backend(one)
        get_backend(one)
        get_backend(two)
    finally:
        set_backend(previous)
    assert len(bound) == 2
    assert bound[0] is one._meta.spec and bound[1] is two._meta.spec
