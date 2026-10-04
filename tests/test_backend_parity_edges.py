"""Query edge cases where SQL and Redis set algebra could disagree (#759 M1b).

Runs on both conformance legs. Each case is one the Postgres renderer has to
handle on purpose: negation over ``NULL`` (SQL's ``NOT NULL`` is ``NULL``;
Redis's ``all_keys - matches`` keeps the row), ``NULL`` in an ordered column
(``prepare_results`` sorts it as the type's zero), ``±inf`` range bounds,
``__in`` containing ``None``, and a key filter given a non-string value.
"""

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
