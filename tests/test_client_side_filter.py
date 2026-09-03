"""
Tests for client-side filtering on plain (unindexed) Fields (Issue #117).

Plain Fields don't have Redis indexes, so filter() loads objects server-side
using any indexed filters, then post-filters on plain field values in Python.
"""

import sys
import os
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import QueryException


# Test Model with a mix of indexed and plain fields
class Order(popoto.Model):
    order_id = popoto.AutoKeyField(strategy="uuid4")
    total = popoto.SortedField(type=float)
    status = popoto.Field(type=str, default="pending")
    category = popoto.Field(type=str, default="general")
    priority = popoto.Field(type=int, default=0)


# Clean up before tests
for item in Order.query.all():
    item.delete()

# Create test data
o1 = Order.create(total=10.0, status="pending", category="food", priority=1)
o2 = Order.create(total=25.0, status="delivered", category="food", priority=2)
o3 = Order.create(total=50.0, status="delivered", category="electronics", priority=3)
o4 = Order.create(total=75.0, status="cancelled", category="electronics", priority=1)
o5 = Order.create(total=100.0, status="pending", category="food", priority=2)

assert Order.query.count() == 5


# =============================================================================
# Test 1: Filter on a single plain field
# =============================================================================
print("Test 1: Filter on single plain field (status)")

results = Order.query.filter(status="delivered")
assert len(results) == 2, f"Expected 2 delivered orders, got {len(results)}"
assert all(o.status == "delivered" for o in results)

results = Order.query.filter(status="cancelled")
assert len(results) == 1, f"Expected 1 cancelled order, got {len(results)}"
assert results[0].status == "cancelled"

results = Order.query.filter(status="nonexistent")
assert len(results) == 0, f"Expected 0 results, got {len(results)}"

print("  PASSED")


# =============================================================================
# Test 2: Hybrid filter — indexed + plain field
# =============================================================================
print("Test 2: Hybrid filter (SortedField + plain field)")

# total >= 50 AND status == "delivered"
results = Order.query.filter(total__gte=50.0, status="delivered")
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].total == 50.0
assert results[0].status == "delivered"

# total >= 10 AND status == "pending"
results = Order.query.filter(total__gte=10.0, status="pending")
assert len(results) == 2, f"Expected 2 results, got {len(results)}"
assert all(o.status == "pending" for o in results)

print("  PASSED")


# =============================================================================
# Test 3: Multiple plain field filters
# =============================================================================
print("Test 3: Multiple plain field filters")

results = Order.query.filter(status="delivered", category="food")
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].total == 25.0

results = Order.query.filter(category="electronics", priority=3)
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].total == 50.0

print("  PASSED")


# =============================================================================
# Test 4: Hybrid with multiple plain fields
# =============================================================================
print("Test 4: Hybrid with multiple plain fields")

results = Order.query.filter(total__gte=20.0, status="delivered", category="food")
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].total == 25.0

print("  PASSED")


# =============================================================================
# Test 5: Unknown params still raise QueryException
# =============================================================================
print("Test 5: Unknown params still raise QueryException")

try:
    Order.query.filter(nonexistent_field="value").all()
    assert False, "Should have raised QueryException"
except QueryException as e:
    assert "nonexistent_field" in str(e)

# Mix of valid plain field + unknown param
try:
    Order.query.filter(status="delivered", bogus="value").all()
    assert False, "Should have raised QueryException"
except QueryException as e:
    assert "bogus" in str(e)

print("  PASSED")


# =============================================================================
# Test 6: QueryBuilder chaining with plain fields
# =============================================================================
print("Test 6: QueryBuilder chaining with plain fields")

results = Order.query.filter(status="delivered").all()
assert len(results) == 2

results = Order.query.filter(total__gte=20.0).filter(status="delivered").all()
assert len(results) == 2

results = Order.query.filter(status="pending").limit(1).all()
assert len(results) == 1

print("  PASSED")


# =============================================================================
# Test 7: count() with plain field params
# =============================================================================
print("Test 7: count() with plain field params")

assert Order.query.count(status="delivered") == 2
assert Order.query.count(status="pending") == 2
assert Order.query.count(status="cancelled") == 1
assert Order.query.count(category="food") == 3
assert Order.query.count(category="electronics") == 2

print("  PASSED")


# =============================================================================
# Test 8: count() with hybrid params
# =============================================================================
print("Test 8: count() with hybrid params (indexed + plain)")

assert Order.query.count(total__gte=50.0, status="delivered") == 1
assert Order.query.count(total__gte=10.0, status="pending") == 2

print("  PASSED")


# =============================================================================
# Test 9: Debug logging emitted for client-side filters
# =============================================================================
print("Test 9: Debug log emitted for client-side filtering")

log_messages = []
handler = logging.Handler()
handler.emit = lambda record: log_messages.append(record.getMessage())
query_logger = logging.getLogger("POPOTO.Query")
query_logger.addHandler(handler)
original_level = query_logger.level
query_logger.setLevel(logging.DEBUG)

Order.query.filter(status="delivered").all()

query_logger.setLevel(original_level)
query_logger.removeHandler(handler)

assert any(
    "Client-side filter" in msg and "status" in msg for msg in log_messages
), f"Expected debug log about client-side filter, got: {log_messages}"

print("  PASSED")


# =============================================================================
# Test 10: Plain field filter with order_by and limit
# =============================================================================
print("Test 10: Plain field filter with order_by and limit")

results = Order.query.filter(status="pending", order_by="total", limit=1)
assert len(results) == 1
assert results[0].total == 10.0

results = Order.query.filter(status="pending", order_by="-total", limit=1)
assert len(results) == 1
assert results[0].total == 100.0

print("  PASSED")


# =============================================================================
# Cleanup
# =============================================================================
for item in Order.query.all():
    item.delete()

assert Order.query.count() == 0
print("\nAll client-side filter tests passed!")


# =============================================================================
# PR #594 blocker: limit must not truncate keys before client-side filters
# =============================================================================


class TruncationRepro(popoto.Model):
    """Meta.order_by on a KeyField triggers get_many_objects' pre-hydration
    key slice, which used to run BEFORE _pending_client_filters."""

    name = popoto.KeyField()
    kind = popoto.IndexedField(type=str)
    note = popoto.Field(type=str)

    class Meta:
        order_by = "name"


def _seed_truncation_repro():
    for item in TruncationRepro.query.all():
        item.delete()
    TruncationRepro.create(name="aaa", kind="x", note="miss")
    TruncationRepro.create(name="bbb", kind="x", note="miss")
    TruncationRepro.create(name="ccc", kind="x", note="hit")


def test_get_finds_a_match_beyond_the_uniqueness_limit():
    """get()'s limit=2 optimization must not hide a unique match that sorts
    after the first two keys when a plain-field filter is in play."""
    _seed_truncation_repro()
    try:
        found = TruncationRepro.query.get(kind="x", note="hit")
        assert found is not None, (
            "get(kind='x', note='hit') returned None although a unique "
            "match exists -- limit truncated keys before the client filter"
        )
        assert found.name == "ccc"
    finally:
        for item in TruncationRepro.query.all():
            item.delete()


def test_get_still_detects_non_uniqueness_beyond_the_limit():
    """The truncation also masked the more-than-one check: both 'miss'
    rows sort first here, but the guard must hold regardless of order."""
    import pytest

    _seed_truncation_repro()
    try:
        with pytest.raises(QueryException, match="more than one"):
            TruncationRepro.query.get(kind="x", note="miss")
    finally:
        for item in TruncationRepro.query.all():
            item.delete()


def test_explicit_filter_limit_applies_after_client_side_filters():
    """filter(..., limit=N) with a plain-field filter must filter first,
    then limit -- the same latent wrongness as get(), fixed by the same
    suppression of pre-hydration truncation."""
    _seed_truncation_repro()
    try:
        rows = TruncationRepro.query.filter(kind="x", note="hit", limit=2)
        assert [o.name for o in rows] == ["ccc"]
        rows = TruncationRepro.query.filter(kind="x", note="miss", limit=1)
        assert len(rows) == 1
        assert rows[0].note == "miss"
    finally:
        for item in TruncationRepro.query.all():
            item.delete()
