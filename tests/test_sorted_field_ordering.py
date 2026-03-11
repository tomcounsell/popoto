"""
Tests for sorted field ordering preservation in query results.

When filtering on a SortedField and no explicit order_by() is provided,
results should come back in ascending score order (the natural Redis
ZRANGEBYSCORE sorted set order).

Precedence: explicit order_by() > implicit sorted field order > Meta.order_by
"""

import asyncio
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module

# ---------------------------------------------------------------------------
# Test model: deliberately simple with one KeyField and one SortedField,
# plus an extra KeyField (category) for intersection tests.
# ---------------------------------------------------------------------------


class OrderTestModel(popoto.Model):
    name = popoto.KeyField(type=str)
    score = popoto.SortedField(type=float)
    category = popoto.KeyField(type=str, default="default")


class OrderTestModelWithMeta(popoto.Model):
    """Model with Meta.order_by to verify precedence rules."""

    name = popoto.KeyField(type=str)
    score = popoto.SortedField(type=float)
    label = popoto.Field(type=str, default="")

    class Meta:
        order_by = "name"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def flush_redis():
    """Clean Redis and reset async connection before each test."""
    POPOTO_REDIS_DB.flushdb()
    # Reset the cached async connection and lock so they get recreated in the
    # new event loop (pytest-asyncio creates a fresh loop per test)
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()


@pytest.fixture()
def create_ordered_data():
    """Create model instances saved in deliberately non-sequential order.

    Saves score=30 first, then score=10, then score=20, then score=50,
    then score=40 to prove that result ordering comes from Redis sorted
    set scores rather than insertion order.
    """
    items = []
    items.append(OrderTestModel.create(name="charlie", score=30.0, category="A"))
    items.append(OrderTestModel.create(name="alice", score=10.0, category="A"))
    items.append(OrderTestModel.create(name="bob", score=20.0, category="B"))
    items.append(OrderTestModel.create(name="eve", score=50.0, category="A"))
    items.append(OrderTestModel.create(name="dave", score=40.0, category="B"))
    return items


@pytest.fixture()
def create_meta_ordered_data():
    """Create instances of the Meta-ordered model in non-sequential order."""
    items = []
    items.append(OrderTestModelWithMeta.create(name="charlie", score=30.0))
    items.append(OrderTestModelWithMeta.create(name="alice", score=10.0))
    items.append(OrderTestModelWithMeta.create(name="bob", score=20.0))
    items.append(OrderTestModelWithMeta.create(name="eve", score=50.0))
    items.append(OrderTestModelWithMeta.create(name="dave", score=40.0))
    return items


# ---------------------------------------------------------------------------
# Test 1: __gte filter returns ascending order
# ---------------------------------------------------------------------------


def test_filter_gte_returns_ascending_order(create_ordered_data):
    """Filter with __gte on SortedField returns results in ascending score order."""
    results = OrderTestModel.query.filter(score__gte=15.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), f"Expected ascending order by score, got {scores}"
    # Verify the exact expected order: 20, 30, 40, 50
    assert scores == [20.0, 30.0, 40.0, 50.0]


# ---------------------------------------------------------------------------
# Test 2: __lte filter returns ascending order
# ---------------------------------------------------------------------------


def test_filter_lte_returns_ascending_order(create_ordered_data):
    """Filter with __lte on SortedField returns results in ascending score order."""
    results = OrderTestModel.query.filter(score__lte=40.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), f"Expected ascending order by score, got {scores}"
    # Verify exact expected order: 10, 20, 30, 40
    assert scores == [10.0, 20.0, 30.0, 40.0]


# ---------------------------------------------------------------------------
# Test 3: __between filter returns ascending order
# ---------------------------------------------------------------------------


def test_filter_between_returns_ascending_order(create_ordered_data):
    """Filter with __between on SortedField returns results in ascending score order."""
    results = OrderTestModel.query.filter(score__between=(15.0, 45.0))

    scores = [r.score for r in results]
    assert scores == sorted(scores), f"Expected ascending order by score, got {scores}"
    # Verify exact expected order: 20, 30, 40
    assert scores == [20.0, 30.0, 40.0]


# ---------------------------------------------------------------------------
# Test 4: Exact match on sorted field still works
# ---------------------------------------------------------------------------


def test_filter_exact_match_returns_result(create_ordered_data):
    """Exact match on a SortedField returns the correct single result."""
    results = OrderTestModel.query.filter(score=30.0)

    assert len(results) == 1
    assert results[0].name == "charlie"
    assert results[0].score == 30.0


# ---------------------------------------------------------------------------
# Test 5: Sorted field order preserved with key field intersection
# ---------------------------------------------------------------------------


def test_order_preserved_with_key_field_intersection(create_ordered_data):
    """Filter on both SortedField and KeyField preserves sorted field order.

    When filtering on score__gte and category, the intersection of results
    should still be ordered by score ascending.
    """
    # Category "A" items: alice(10), charlie(30), eve(50)
    # Filter: score >= 20 AND category == "A"
    # Expected: charlie(30), eve(50) in ascending score order
    results = OrderTestModel.query.filter(score__gte=20.0, category="A")

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score after intersection, got {scores}"
    )
    assert scores == [30.0, 50.0]
    names = [r.name for r in results]
    assert names == ["charlie", "eve"]


# ---------------------------------------------------------------------------
# Test 6: Explicit order_by overrides implicit sorted field order
# ---------------------------------------------------------------------------


def test_explicit_order_by_overrides_implicit(create_ordered_data):
    """Explicit order_by overrides the implicit sorted field ordering.

    Even though we are filtering on a SortedField, providing an explicit
    order_by("name") should sort results alphabetically by name, not by score.
    """
    results = OrderTestModel.query.filter(score__gte=15.0, order_by="name")

    names = [r.name for r in results]
    assert names == sorted(names), f"Expected alphabetical order by name, got {names}"
    assert names == ["bob", "charlie", "dave", "eve"]


# ---------------------------------------------------------------------------
# Test 7: Explicit order_by descending on sorted field
# ---------------------------------------------------------------------------


def test_explicit_order_by_descending_sorted_field(create_ordered_data):
    """Explicit order_by('-score') reverses the default ascending sorted order."""
    results = OrderTestModel.query.filter(score__gte=15.0, order_by="-score")

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), (
        f"Expected descending order by score, got {scores}"
    )
    assert scores == [50.0, 40.0, 30.0, 20.0]


# ---------------------------------------------------------------------------
# Test 8: Count is unaffected by ordering
# ---------------------------------------------------------------------------


def test_count_unaffected(create_ordered_data):
    """Verify count() still returns the correct number regardless of ordering."""
    count = OrderTestModel.query.filter(score__gte=15.0).count()
    assert count == 4, f"Expected count 4, got {count}"

    count_lte = OrderTestModel.query.filter(score__lte=30.0).count()
    assert count_lte == 3, f"Expected count 3, got {count_lte}"

    count_between = OrderTestModel.query.filter(score__between=(15.0, 45.0)).count()
    assert count_between == 3, f"Expected count 3, got {count_between}"


# ---------------------------------------------------------------------------
# Test 9: Async filter preserves sorted field ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_preserves_ordering(create_ordered_data):
    """Async variant: async_filter with __gte preserves ascending score order."""
    results = await OrderTestModel.query.async_filter(score__gte=15.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score (async), got {scores}"
    )
    assert scores == [20.0, 30.0, 40.0, 50.0]


# ---------------------------------------------------------------------------
# Test 10: Async filter with explicit order_by overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_with_explicit_order_by(create_ordered_data):
    """Async variant: explicit order_by overrides sorted field ordering."""
    results = await OrderTestModel.query.async_filter(score__gte=15.0, order_by="name")

    names = [r.name for r in results]
    assert names == sorted(names), (
        f"Expected alphabetical order by name (async), got {names}"
    )
    assert names == ["bob", "charlie", "dave", "eve"]


# ---------------------------------------------------------------------------
# Test 11: Sorted field order takes precedence over Meta.order_by
# ---------------------------------------------------------------------------


def test_sorted_field_order_overrides_meta_order_by(create_meta_ordered_data):
    """Sorted field implicit order should take precedence over Meta.order_by.

    OrderTestModelWithMeta has Meta.order_by = 'name' (alphabetical).
    When filtering on the SortedField (score), the results should be
    ordered by score ascending, not alphabetically by name.
    """
    results = OrderTestModelWithMeta.query.filter(score__gte=15.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending score order (overriding Meta.order_by), got {scores}"
    )
    assert scores == [20.0, 30.0, 40.0, 50.0]


# ---------------------------------------------------------------------------
# Test 12: Chainable API preserves sorted field ordering
# ---------------------------------------------------------------------------


def test_chainable_filter_preserves_ordering(create_ordered_data):
    """Chainable query API also preserves sorted field ordering."""
    results = OrderTestModel.query.filter(score__gte=15.0).all()

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order via chainable API, got {scores}"
    )
    assert scores == [20.0, 30.0, 40.0, 50.0]


# ---------------------------------------------------------------------------
# Test 13: Chainable order_by overrides implicit sorted field order
# ---------------------------------------------------------------------------


def test_chainable_order_by_overrides_implicit(create_ordered_data):
    """Chainable order_by() method overrides implicit sorted field ordering."""
    results = OrderTestModel.query.filter(score__gte=15.0).order_by("name").all()

    names = [r.name for r in results]
    assert names == sorted(names), (
        f"Expected alphabetical order via chainable order_by, got {names}"
    )
    assert names == ["bob", "charlie", "dave", "eve"]


# ---------------------------------------------------------------------------
# Test 14: __lt filter returns ascending order
# ---------------------------------------------------------------------------


def test_filter_lt_returns_ascending_order(create_ordered_data):
    """Filter with __lt on SortedField returns results in ascending score order."""
    results = OrderTestModel.query.filter(score__lt=40.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score with __lt, got {scores}"
    )
    # score=10, 20, 30 are all < 40
    assert scores == [10.0, 20.0, 30.0]


# ---------------------------------------------------------------------------
# Test 15: __gt filter returns ascending order
# ---------------------------------------------------------------------------


def test_filter_gt_returns_ascending_order(create_ordered_data):
    """Filter with __gt on SortedField returns results in ascending score order."""
    results = OrderTestModel.query.filter(score__gt=20.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score with __gt, got {scores}"
    )
    # score=30, 40, 50 are all > 20
    assert scores == [30.0, 40.0, 50.0]


# ---------------------------------------------------------------------------
# Test 16: Async filter with __lte preserves ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_lte_preserves_ordering(create_ordered_data):
    """Async variant: async_filter with __lte preserves ascending score order."""
    results = await OrderTestModel.query.async_filter(score__lte=40.0)

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score (async __lte), got {scores}"
    )
    assert scores == [10.0, 20.0, 30.0, 40.0]


# ---------------------------------------------------------------------------
# Test 17: Async filter with __between preserves ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_between_preserves_ordering(create_ordered_data):
    """Async variant: async_filter with __between preserves ascending score order."""
    results = await OrderTestModel.query.async_filter(score__between=(15.0, 45.0))

    scores = [r.score for r in results]
    assert scores == sorted(scores), (
        f"Expected ascending order by score (async __between), got {scores}"
    )
    assert scores == [20.0, 30.0, 40.0]


# ---------------------------------------------------------------------------
# Test 18: Async descending order_by on sorted field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_explicit_order_by_descending(create_ordered_data):
    """Async variant: explicit order_by('-score') returns descending order."""
    results = await OrderTestModel.query.async_filter(
        score__gte=15.0, order_by="-score"
    )

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), (
        f"Expected descending order by score (async), got {scores}"
    )
    assert scores == [50.0, 40.0, 30.0, 20.0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
