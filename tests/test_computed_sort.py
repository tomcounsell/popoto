"""
Tests for computed_sort() on QueryBuilder (Issue #182).

Verifies that QueryBuilder.computed_sort(fn, reverse=False) applies a
caller-provided key function to sort results in Python after fetching,
before applying limit().
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import QueryBuilder


# Test Model
class ComputedSortModel(popoto.Model):
    name = popoto.KeyField()
    status = popoto.KeyField()
    priority = popoto.Field(type=int, default=0)
    score = popoto.Field(type=float, default=0.0)


# Clean up before tests
for item in ComputedSortModel.query.all():
    item.delete()


# Create test data
item_a = ComputedSortModel.create(name="alpha", status="active", priority=1, score=4.5)
item_b = ComputedSortModel.create(name="beta", status="active", priority=3, score=2.0)
item_c = ComputedSortModel.create(
    name="gamma", status="inactive", priority=2, score=3.5
)
item_d = ComputedSortModel.create(name="delta", status="active", priority=4, score=1.0)


# =============================================================================
# Test 1: Basic computed_sort - ascending by priority
# =============================================================================
print("Test 1: Basic computed_sort ascending")

results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority)
    .all()
)
priorities = [r.priority for r in results]
assert priorities == [1, 3, 4], f"Expected [1, 3, 4], got {priorities}"

print("  PASSED")


# =============================================================================
# Test 2: computed_sort with reverse=True
# =============================================================================
print("Test 2: computed_sort with reverse=True")

results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority, reverse=True)
    .all()
)
priorities = [r.priority for r in results]
assert priorities == [4, 3, 1], f"Expected [4, 3, 1], got {priorities}"

print("  PASSED")


# =============================================================================
# Test 3: computed_sort with limit - sort applied before limit
# =============================================================================
print("Test 3: computed_sort with limit (sort before limit)")

results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority)
    .limit(2)
    .all()
)
priorities = [r.priority for r in results]
assert priorities == [1, 3], f"Expected [1, 3], got {priorities}"

print("  PASSED")


# =============================================================================
# Test 4: computed_sort with reverse and limit
# =============================================================================
print("Test 4: computed_sort with reverse=True and limit")

results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority, reverse=True)
    .limit(2)
    .all()
)
priorities = [r.priority for r in results]
assert priorities == [4, 3], f"Expected [4, 3], got {priorities}"

print("  PASSED")


# =============================================================================
# Test 5: computed_sort on empty result set
# =============================================================================
print("Test 5: computed_sort on empty result set")

results = (
    ComputedSortModel.query.filter(status="nonexistent")
    .computed_sort(lambda x: x.priority)
    .all()
)
assert results == [], f"Expected [], got {results}"

print("  PASSED")


# =============================================================================
# Test 6: computed_sort takes precedence over order_by
# =============================================================================
print("Test 6: computed_sort takes precedence over order_by")

results = (
    ComputedSortModel.query.filter(status="active")
    .order_by("name")
    .computed_sort(lambda x: x.priority, reverse=True)
    .all()
)
priorities = [r.priority for r in results]
assert priorities == [
    4,
    3,
    1,
], f"Expected [4, 3, 1] (computed_sort wins over order_by), got {priorities}"

print("  PASSED")


# =============================================================================
# Test 7: computed_sort is chainable (returns self)
# =============================================================================
print("Test 7: computed_sort is chainable")

builder = ComputedSortModel.query.filter(status="active")
result = builder.computed_sort(lambda x: x.priority)
assert isinstance(result, QueryBuilder), f"Expected QueryBuilder, got {type(result)}"

print("  PASSED")


# =============================================================================
# Test 8: computed_sort propagates through filter() chaining
# =============================================================================
print("Test 8: computed_sort propagates through filter()")

query1 = ComputedSortModel.query.filter(status="active").computed_sort(
    lambda x: x.priority
)
query2 = query1.filter(name="beta")
results = query2.all()
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].name == "beta", f"Expected beta, got {results[0].name}"

print("  PASSED")


# =============================================================================
# Test 9: computed_sort with values() projection
# =============================================================================
print("Test 9: computed_sort with values() projection")

results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x["priority"], reverse=True)
    .values("name", "priority")
    .all()
)
assert isinstance(results[0], dict), f"Expected dict, got {type(results[0])}"
priorities = [r["priority"] for r in results]
assert priorities == [4, 3, 1], f"Expected [4, 3, 1], got {priorities}"

print("  PASSED")


# =============================================================================
# Test 10: computed_sort with complex key function
# =============================================================================
print("Test 10: computed_sort with complex key function")


def activation_score(item):
    """Compute a score combining priority and score fields."""
    return item.priority * 0.5 + item.score * 0.5


results = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(activation_score, reverse=True)
    .all()
)
scores = [activation_score(r) for r in results]
assert scores == sorted(
    scores, reverse=True
), f"Expected descending scores, got {scores}"

print("  PASSED")


# =============================================================================
# Test 11: computed_sort with first() and last()
# =============================================================================
print("Test 11: computed_sort with first() and last()")

first = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority)
    .first()
)
assert first.priority == 1, f"Expected priority 1, got {first.priority}"

last = (
    ComputedSortModel.query.filter(status="active")
    .computed_sort(lambda x: x.priority)
    .last()
)
assert last.priority == 4, f"Expected priority 4, got {last.priority}"

print("  PASSED")


# =============================================================================
# Test 12: computed_sort with None function raises TypeError
# =============================================================================
print("Test 12: computed_sort with None function raises TypeError")

try:
    ComputedSortModel.query.filter(status="active").computed_sort(None).all()
    assert False, "Expected TypeError"
except TypeError:
    pass

print("  PASSED")


# =============================================================================
# Test 13: computed_sort with function that raises propagates error
# =============================================================================
print("Test 13: computed_sort with raising function propagates error")


def bad_sort_fn(item):
    raise ValueError("intentional error")


try:
    (ComputedSortModel.query.filter(status="active").computed_sort(bad_sort_fn).all())
    assert False, "Expected ValueError"
except ValueError as e:
    assert "intentional error" in str(e)

print("  PASSED")


# =============================================================================
# Cleanup
# =============================================================================
print("\nCleaning up test data...")
for item in ComputedSortModel.query.all():
    item.delete()

assert ComputedSortModel.query.count() == 0, "Cleanup failed"

print("\n" + "=" * 60)
print("ALL COMPUTED_SORT TESTS PASSED!")
print("=" * 60)
