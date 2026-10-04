"""Query edge cases where SQL and Redis set algebra could disagree (#759 M1b).

Runs on both conformance legs. Each case is one the Postgres renderer has to
handle on purpose: negation over ``NULL`` (SQL's ``NOT NULL`` is ``NULL``;
Redis's ``all_keys - matches`` keeps the row), ``NULL`` in an ordered column
(``prepare_results`` sorts it as the type's zero), ``±inf`` range bounds,
``__in`` containing ``None``, and a key filter given a non-string value.
"""

import datetime
import decimal
import math

import pytest

import popoto
from popoto import Q

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class EdgeItem(popoto.Model):
    code = popoto.KeyField()
    group = popoto.KeyField(null=True)
    rank = popoto.SortedField(type=int)
    note = popoto.StringField(null=True)
    hits = popoto.IntField(null=True)


@pytest.fixture(autouse=True)
def items():
    EdgeItem.create(code="a", group="g1", rank=1, note="x", hits=5)
    EdgeItem.create(code="b", group="g1", rank=2, note=None, hits=None)
    EdgeItem.create(code="c", group=None, rank=3, note="y", hits=2)
    EdgeItem.create(code="10", group="g2", rank=4, note="x", hits=0)


def codes(results):
    return sorted(r.code for r in results)


def test_negation_keeps_rows_whose_field_is_null():
    assert codes(EdgeItem.query.filter(~Q(group="g1"))) == ["10", "c"]


def test_negating_a_plain_field_q_is_a_documented_divergence(backend_is_redis):
    """On Redis a ``Q`` leaf over an unindexed field returns *every* key and
    defers the equality to a client-side filter applied after the set
    algebra, so ``~Q(note="x")`` is ``all - all`` and then filtered: always
    empty. Postgres evaluates the predicate (docs/features/postgres-backend.md,
    "Documented divergences"). Both are pinned so neither changes silently."""
    result = codes(EdgeItem.query.filter(~Q(note="x")))
    if backend_is_redis:
        assert result == []
    else:
        assert result == ["b", "c"]


def test_null_sorts_as_the_types_zero():
    ordered = [r.code for r in EdgeItem.query.filter(rank__gte=0, order_by="hits")]
    assert ordered[-2:] == ["c", "a"]
    assert set(ordered[:2]) == {"b", "10"}


def test_infinite_bounds():
    assert codes(EdgeItem.query.filter(rank__lte=math.inf)) == ["10", "a", "b", "c"]
    assert codes(EdgeItem.query.filter(rank__gt=-math.inf, rank__lt=math.inf)) == [
        "10",
        "a",
        "b",
        "c",
    ]
    assert codes(EdgeItem.query.filter(rank__gte=math.inf)) == []


def test_in_with_none_matches_null_keys():
    assert codes(EdgeItem.query.filter(group__in=["g2", None])) == ["10", "c"]
    assert codes(EdgeItem.query.filter(group__isnull=True)) == ["c"]
    assert codes(EdgeItem.query.filter(group__in=[])) == []


def test_key_filter_matches_by_key_string():
    assert codes(EdgeItem.query.filter(code=10)) == ["10"]


def test_sorted_range_order_and_projection():
    assert EdgeItem.query.filter(rank__gt=1, values=("code", "rank")) == [
        {"code": "b", "rank": 2},
        {"code": "c", "rank": 3},
        {"code": "10", "rank": 4},
    ]
    assert EdgeItem.query.count(rank__between=(2, 3)) == 2
    assert EdgeItem.query.count(group="g1", note="x") == 1


# -- documented divergences (#769 review, blocker 4) ---------------------------
#
# Each test below pins *both* legs of one row of the "Documented divergences"
# table in docs/features/postgres-backend.md. Redis behaviour is deliberately
# unchanged in M1b (plan gate (a)); the Redis-side bugs among these are tracked
# in #771. If either leg changes, the test fails and the table must be
# updated with it.


def test_plain_field_inside_a_composed_q_is_a_documented_divergence(
    backend_is_redis,
):
    """(i) A ``Q`` leaf over an unindexed field returns every key on Redis and
    its equality is applied afterwards only for a *lone* leaf, so inside
    ``&``/``|`` (or beside a kwarg) Redis drops the plain condition. Postgres
    evaluates it. Postgres is correct."""
    both = codes(EdgeItem.query.filter(Q(hits=5) & Q(group="g1")))
    either = codes(EdgeItem.query.filter(Q(hits=5) | Q(group="g2")))
    beside = codes(EdgeItem.query.filter(Q(note=None), group="g1"))
    if backend_is_redis:
        assert (both, either, beside) == (
            ["a", "b"],
            ["10", "a", "b", "c"],
            ["a", "b"],
        )
    else:
        assert (both, either, beside) == (["a"], ["10", "a"], ["b"])
    # A lone plain-field Q agrees on both legs.
    assert codes(EdgeItem.query.filter(Q(hits=5))) == ["a"]


def test_values_with_a_filter_outside_the_projection_is_a_documented_divergence(
    backend_is_redis,
):
    """(ii) ``values=`` with a plain-field filter whose field is not
    projected: Redis applies the filter to the projected dicts, which lack the
    field, so nothing matches. Postgres filters the rows. Postgres is
    correct."""
    result = EdgeItem.query.filter(group="g1", note="x", values=("code",))
    assert list(result) == ([] if backend_is_redis else [{"code": "a"}])
    # With the filtered field projected, both agree.
    assert list(
        EdgeItem.query.filter(group="g1", note="x", values=("code", "note"))
    ) == [{"code": "a", "note": "x"}]


def test_two_lookups_on_one_sorted_field_is_a_documented_divergence(
    backend_is_redis,
):
    """(iii) Two lookups bounding the same side of one ``SortedField``: Redis
    keeps only the last one given for that side; Postgres applies both
    (``AND``). Postgres is correct. One lower and one upper bound agree."""
    gt_then_gte = codes(EdgeItem.query.filter(rank__gt=10, rank__gte=1))
    lt_then_between = codes(EdgeItem.query.filter(rank__lt=2, rank__between=(2, 3)))
    counted = EdgeItem.query.count(rank__gt=10, rank__gte=1)
    if backend_is_redis:
        assert gt_then_gte == ["10", "a", "b", "c"]
        assert lt_then_between == ["b", "c"]
        assert counted == 4
    else:
        assert gt_then_gte == lt_then_between == []
        assert counted == 0
    assert codes(EdgeItem.query.filter(rank__gt=1, rank__lt=3)) == ["b"]


def test_values_ordered_over_null_is_a_documented_divergence(backend_is_redis):
    """(iv) ``values=`` + ``order_by`` over a column holding ``None``: Redis
    sorts the projected dicts without the zero substitution the instance path
    uses, and raises ``TypeError``. Postgres sorts ``NULL`` as the type's
    zero, as the instance path does on both. Postgres is correct."""

    def call():
        return list(
            EdgeItem.query.filter(rank__gte=0, values=("code", "hits"), order_by="hits")
        )

    if backend_is_redis:
        with pytest.raises(TypeError, match="not supported between instances"):
            call()
    else:
        rows = call()
        assert [r["code"] for r in rows][2:] == ["c", "a"]
        assert {r["code"] for r in rows[:2]} == {"b", "10"}


def test_none_or_non_numeric_sorted_bound_is_a_documented_divergence(
    backend_is_redis,
):
    """(v) ``None`` or a non-numeric string as a ``SortedField`` range bound:
    Redis passes it to ``ZRANGEBYSCORE`` and the server refuses it
    (``ResponseError``); Postgres matches nothing. Neither is a documented
    contract. A numeric string is parsed on both."""
    for bound in ({"rank__gte": None}, {"rank__lte": "abc"}):
        if backend_is_redis:
            with pytest.raises(Exception, match="min or max is not a float"):
                list(EdgeItem.query.filter(**bound))
        else:
            assert list(EdgeItem.query.filter(**bound)) == []
    assert codes(EdgeItem.query.filter(rank__lte="2.5")) == ["a", "b"]
    assert codes(EdgeItem.query.filter(rank__lte="3")) == ["a", "b", "c"]


def test_key_field_contains_is_a_documented_divergence(backend_is_redis):
    """(vi) ``KeyField`` ``__contains`` is accepted but not implemented on
    Redis (matches nothing); Postgres renders ``LIKE '%value%'``. Postgres is
    correct."""
    group = codes(EdgeItem.query.filter(group__contains="1"))
    code = codes(EdgeItem.query.filter(code__contains="0"))
    if backend_is_redis:
        assert group == code == []
    else:
        assert (group, code) == (["a", "b"], ["10"])


def test_int_field_incremented_by_a_float_is_a_documented_divergence(
    backend_is_redis,
):
    """An ``IntField`` incremented by a non-integral delta. Redis's Lua stores
    the float sum and ``atomic_increment`` returns it truncated, so the
    returned and the stored value disagree (6 vs 6.5). Postgres casts the sum
    to ``bigint``, rounding half away from zero, and stores what it returns.
    Neither is well defined; pass an ``int`` delta to an ``IntField``."""
    item = EdgeItem.query.get(code="a")  # hits=5
    returned = item.atomic_increment("hits", 1.5)
    stored = EdgeItem.query.get(code="a").hits
    if backend_is_redis:
        assert (returned, stored) == (6, 6.5)
    else:
        assert (returned, stored) == (7, 7)
    # An integral delta agrees on both legs.
    item = EdgeItem.query.get(code="c")  # hits=2
    assert item.atomic_increment("hits", 3) == 5
    assert EdgeItem.query.get(code="c").hits == 5


def test_key_isnull_false_skips_glob_characters_is_a_documented_divergence(
    backend_is_redis,
):
    """(vii) ``KeyField`` ``__isnull=False`` on Redis matches only some of the
    non-null records: which ones depends on the key field's position and on
    the value (here the second key matches nothing, and the first misses
    ``"10"``; the oracle probe also saw values containing ``_`` or ``%``
    dropped). Postgres renders ``IS NOT NULL``. Postgres is correct."""
    first = codes(EdgeItem.query.filter(code__isnull=False))
    second = codes(EdgeItem.query.filter(group__isnull=False))
    if backend_is_redis:
        assert (first, second) == (["a", "b", "c"], [])
    else:
        assert (first, second) == (["10", "a", "b", "c"], ["10", "a", "b"])


class EdgeStamp(popoto.Model):
    code = popoto.KeyField()
    at = popoto.DatetimeField(null=True)


def test_naive_datetime_equality_is_a_documented_divergence(backend_is_redis):
    """(viii) Equality on a ``DatetimeField`` with a *naive* value against a
    stored aware one: Redis compares in Python, where naive never equals
    aware; Postgres compares instants, taking naive as UTC (the rule sorted
    fields already use, #519). Postgres is consistent with that rule."""
    utc = datetime.timezone.utc
    EdgeStamp.create(code="aware", at=datetime.datetime(2024, 1, 5, 12, tzinfo=utc))
    naive = datetime.datetime(2024, 1, 5, 12)
    result = [s.code for s in EdgeStamp.query.filter(at=naive)]
    assert result == ([] if backend_is_redis else ["aware"])
    aware = naive.replace(tzinfo=utc)
    assert [s.code for s in EdgeStamp.query.filter(at=aware)] == ["aware"]


# -- numeric key fields: Redis's key-string semantics (#770 review) -------------


class IntKeyed(popoto.Model):
    code = popoto.KeyField(type=int)


class FloatKeyed(popoto.Model):
    code = popoto.KeyField(type=float)


def _numeric_codes(model, **lookup):
    return sorted(r.code for r in model.query.filter(**lookup))


@pytest.mark.parametrize(
    "values, expected",
    [
        ([1, 1.5], [1]),
        ([1.5, 2], [2]),
        ([1, decimal.Decimal("1.5")], [1]),
        ([decimal.Decimal("1"), 2.0], [1]),
        ([1.0, 3], [3]),
        ([2.0], []),
        (["1", 2], [1, 2]),
        ([1, 1.5, None], [1]),
        ([], []),
    ],
    ids=lambda v: repr(v),
)
def test_int_key_in_with_mixed_numeric_types(values, expected):
    """A key field matches by its key string on Redis, so on
    ``KeyField(type=int)`` a value matches only when ``str(value)`` is an int's
    string: ``1`` and ``"1"`` match, ``1.0`` and ``1.5`` do not. Postgres
    binds the mixture as one array of the column's type and agrees. (It used
    to raise psycopg ``DataError: cannot dump lists of mixed types``.)"""
    for code in (1, 2, 3):
        IntKeyed.create(code=code)
    assert _numeric_codes(IntKeyed, code__in=values) == expected


@pytest.mark.parametrize(
    "values, expected",
    [
        ([1, 1.5], [1.5]),
        ([1.0], [1.0]),
        ([1], []),
        ([decimal.Decimal("1.5"), 3], [1.5]),
        ([], []),
    ],
    ids=lambda v: repr(v),
)
def test_float_key_in_with_mixed_numeric_types(values, expected):
    """On ``KeyField(type=float)`` the stored key string is ``"1.0"``, so
    ``1`` (``"1"``) matches nothing on Redis, and on Postgres too."""
    for code in (1.0, 1.5):
        FloatKeyed.create(code=code)
    assert _numeric_codes(FloatKeyed, code__in=values) == expected


def test_numeric_key_equality_uses_the_key_string():
    for code in (1, 2):
        IntKeyed.create(code=code)
    FloatKeyed.create(code=1.0)
    assert _numeric_codes(IntKeyed, code=2) == [2]
    assert _numeric_codes(IntKeyed, code="2") == [2]
    assert _numeric_codes(IntKeyed, code=2.0) == []
    assert _numeric_codes(IntKeyed, code=1.5) == []
    assert _numeric_codes(FloatKeyed, code=1.0) == [1.0]
    assert _numeric_codes(FloatKeyed, code=1) == []
