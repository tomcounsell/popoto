"""
Tests for the chainable query builder interface (Issue #91).

This tests the fluent API for building queries:
    Model.query.filter(status="active").order_by("name").limit(10).all()

And ensures backward compatibility with the existing kwargs API:
    Model.query.filter(status="active", order_by="name", limit=10)
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import QueryBuilder


# Test Model
class ChainTestModel(popoto.Model):
    name = popoto.KeyField()
    status = popoto.KeyField()
    priority = popoto.Field(type=int, default=0)


# Clean up before tests
for item in ChainTestModel.query.all():
    item.delete()


# Create test data
item1 = ChainTestModel.create(name="alpha", status="active", priority=1)
item2 = ChainTestModel.create(name="beta", status="active", priority=2)
item3 = ChainTestModel.create(name="gamma", status="inactive", priority=3)
item4 = ChainTestModel.create(name="delta", status="active", priority=4)


# =============================================================================
# Test 1: Basic backward compatibility - kwargs API still works
# =============================================================================
print("Test 1: Backward compatibility with kwargs API")

# Original kwargs API should still work
results = ChainTestModel.query.filter(status="active", limit=2)
# QueryBuilder behaves like a list - can iterate, get length, etc.
assert len(results) == 2, f"Expected 2 results, got {len(results)}"

# Filter with order_by as kwarg
results = ChainTestModel.query.filter(status="active", order_by="name", limit=10)
names = [r.name for r in results]
assert names == sorted(names), f"Expected sorted names, got {names}"

print("  PASSED: kwargs API works correctly")


# =============================================================================
# Test 2: QueryBuilder is returned from filter()
# =============================================================================
print("Test 2: filter() returns QueryBuilder")

result = ChainTestModel.query.filter(status="active")
assert isinstance(result, QueryBuilder), f"Expected QueryBuilder, got {type(result)}"

print("  PASSED: filter() returns QueryBuilder")


# =============================================================================
# Test 3: Basic chaining - filter().limit().all()
# =============================================================================
print("Test 3: Basic chaining - filter().limit().all()")

results = ChainTestModel.query.filter(status="active").limit(2).all()
assert isinstance(results, list), f"Expected list, got {type(results)}"
assert len(results) == 2, f"Expected 2 results, got {len(results)}"

print("  PASSED: filter().limit().all() works")


# =============================================================================
# Test 4: Order by chaining
# =============================================================================
print("Test 4: order_by chaining")

results = ChainTestModel.query.filter(status="active").order_by("name").all()
names = [r.name for r in results]
assert names == sorted(names), f"Expected ascending sort, got {names}"

# Descending order
results = ChainTestModel.query.filter(status="active").order_by("-name").all()
names = [r.name for r in results]
assert names == sorted(names, reverse=True), f"Expected descending sort, got {names}"

print("  PASSED: order_by chaining works")


# =============================================================================
# Test 5: Chain multiple filters
# =============================================================================
print("Test 5: Chain multiple filter() calls")

# This should return only items that are both active AND have name="alpha"
results = ChainTestModel.query.filter(status="active").filter(name="alpha").all()
assert len(results) == 1, f"Expected 1 result, got {len(results)}"
assert results[0].name == "alpha", f"Expected alpha, got {results[0].name}"

print("  PASSED: Multiple filter() calls chain correctly")


# =============================================================================
# Test 6: Full chain - filter().filter().order_by().limit().all()
# =============================================================================
print("Test 6: Full chaining - filter().filter().order_by().limit().all()")

results = ChainTestModel.query.filter(status="active").order_by("name").limit(2).all()
assert len(results) == 2, f"Expected 2 results, got {len(results)}"
names = [r.name for r in results]
assert names == sorted(names), f"Expected sorted names, got {names}"

print("  PASSED: Full chaining works")


# =============================================================================
# Test 7: QueryBuilder is iterable (backward compatibility)
# =============================================================================
print("Test 7: QueryBuilder iteration (backward compatibility)")

query = ChainTestModel.query.filter(status="active")
items_from_iteration = []
for item in query:
    items_from_iteration.append(item)

assert len(items_from_iteration) == 3, (
    f"Expected 3 items, got {len(items_from_iteration)}"
)

print("  PASSED: QueryBuilder iteration works")


# =============================================================================
# Test 8: QueryBuilder indexing (backward compatibility)
# =============================================================================
print("Test 8: QueryBuilder indexing (backward compatibility)")

query = ChainTestModel.query.filter(status="active").order_by("name")
first_item = query[0]
assert first_item.name == "alpha", f"Expected alpha as first, got {first_item.name}"

print("  PASSED: QueryBuilder indexing works")


# =============================================================================
# Test 9: values() chaining
# =============================================================================
print("Test 9: values() chaining")

results = ChainTestModel.query.filter(status="active").values("name", "status").all()
assert isinstance(results[0], dict), f"Expected dict, got {type(results[0])}"
assert "name" in results[0], "Expected 'name' in result dict"
assert "status" in results[0], "Expected 'status' in result dict"

print("  PASSED: values() chaining works")


# =============================================================================
# Test 10: count() on QueryBuilder
# =============================================================================
print("Test 10: count() on QueryBuilder")

count = ChainTestModel.query.filter(status="active").count()
assert count == 3, f"Expected 3, got {count}"

print("  PASSED: count() on QueryBuilder works")


# =============================================================================
# Test 11: first() method
# =============================================================================
print("Test 11: first() method")

result = ChainTestModel.query.filter(status="active").order_by("name").first()
assert result is not None, "Expected a result, got None"
assert result.name == "alpha", f"Expected alpha, got {result.name}"

# first() on empty result
empty_result = ChainTestModel.query.filter(status="nonexistent").first()
assert empty_result is None, f"Expected None, got {empty_result}"

print("  PASSED: first() method works")


# =============================================================================
# Test 12: Boolean evaluation of QueryBuilder
# =============================================================================
print("Test 12: Boolean evaluation of QueryBuilder")

query_with_results = ChainTestModel.query.filter(status="active")
query_without_results = ChainTestModel.query.filter(status="nonexistent")

assert bool(query_with_results) is True, "Expected True for query with results"
assert bool(query_without_results) is False, "Expected False for query without results"

print("  PASSED: Boolean evaluation works")


# =============================================================================
# Test 13: Immutability - filter() returns new QueryBuilder
# =============================================================================
print("Test 13: Immutability - filter() returns new QueryBuilder")

query1 = ChainTestModel.query.filter(status="active")
query2 = query1.filter(name="alpha")

# query1 should not be affected by query2's additional filter
assert len(query1.all()) == 3, (
    f"Expected 3 results from query1, got {len(query1.all())}"
)
assert len(query2.all()) == 1, f"Expected 1 result from query2, got {len(query2.all())}"

print("  PASSED: filter() returns new QueryBuilder (immutable)")


# =============================================================================
# Test 14: QueryBuilder repr
# =============================================================================
print("Test 14: QueryBuilder __repr__")

query = ChainTestModel.query.filter(status="active")
repr_str = repr(query)
assert "QueryBuilder" in repr_str, f"Expected 'QueryBuilder' in repr, got {repr_str}"
assert "ChainTestModel" in repr_str, (
    f"Expected 'ChainTestModel' in repr, got {repr_str}"
)

print("  PASSED: QueryBuilder __repr__ works")


# =============================================================================
# Cleanup
# =============================================================================
print("\nCleaning up test data...")
for item in ChainTestModel.query.all():
    item.delete()

assert ChainTestModel.query.count() == 0, "Cleanup failed"

print("\n" + "=" * 60)
print("ALL CHAINABLE QUERY TESTS PASSED!")
print("=" * 60)
