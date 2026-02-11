"""
Tests for expression-based query syntax.

This module tests the ability to use Python comparison operators on Field
class attributes to build queries, as an alternative to keyword arguments.
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.expressions import Expression, CombinedExpression


# Test model with various field types
class Restaurant(popoto.Model):
    name = popoto.KeyField()
    rating = popoto.SortedField(type=float)
    price_level = popoto.SortedField(type=int)
    cuisine = popoto.KeyField()
    is_open = popoto.Field(type=bool, default=True)


# Clean up any existing test data
for r in Restaurant.query.all():
    r.delete()


# Create test data
restaurant1 = Restaurant.create(
    name="Burger Palace",
    rating=4.5,
    price_level=2,
    cuisine="American",
    is_open=True,
)

restaurant2 = Restaurant.create(
    name="Sushi Master",
    rating=4.8,
    price_level=3,
    cuisine="Japanese",
    is_open=True,
)

restaurant3 = Restaurant.create(
    name="Pizza Corner",
    rating=3.5,
    price_level=1,
    cuisine="Italian",
    is_open=False,
)

restaurant4 = Restaurant.create(
    name="Taco Town",
    rating=4.0,
    price_level=1,
    cuisine="Mexican",
    is_open=True,
)


# Test 1: Basic equality expression
print("Test 1: Basic equality expression")
expr = Restaurant.name == "Burger Palace"
assert isinstance(expr, Expression)
assert expr.field_name == "name"
assert expr.operator == "__eq"
assert expr.value == "Burger Palace"
assert expr.to_kwargs() == {"name": "Burger Palace"}
print("  PASSED: Expression creation and to_kwargs()")


# Test 2: Greater than expression
print("Test 2: Greater than expression")
expr = Restaurant.rating > 4.0
assert isinstance(expr, Expression)
assert expr.field_name == "rating"
assert expr.operator == "__gt"
assert expr.value == 4.0
assert expr.to_kwargs() == {"rating__gt": 4.0}
print("  PASSED: Greater than expression")


# Test 3: Less than expression
print("Test 3: Less than expression")
expr = Restaurant.rating < 4.0
assert expr.operator == "__lt"
assert expr.to_kwargs() == {"rating__lt": 4.0}
print("  PASSED: Less than expression")


# Test 4: Greater than or equal expression
print("Test 4: Greater than or equal expression")
expr = Restaurant.rating >= 4.0
assert expr.operator == "__gte"
assert expr.to_kwargs() == {"rating__gte": 4.0}
print("  PASSED: Greater than or equal expression")


# Test 5: Less than or equal expression
print("Test 5: Less than or equal expression")
expr = Restaurant.rating <= 4.0
assert expr.operator == "__lte"
assert expr.to_kwargs() == {"rating__lte": 4.0}
print("  PASSED: Less than or equal expression")


# Test 6: Not equal expression
print("Test 6: Not equal expression")
expr = Restaurant.cuisine != "American"
assert expr.operator == "__ne"
assert expr.to_kwargs() == {"cuisine__ne": "American"}
print("  PASSED: Not equal expression")


# Test 7: Filter with single expression (equality)
print("Test 7: Filter with single expression (equality)")
results = Restaurant.query.filter(Restaurant.name == "Burger Palace")
assert len(results) == 1
assert results[0].name == "Burger Palace"
print("  PASSED: Filter with equality expression")


# Test 8: Filter with single expression (greater than)
print("Test 8: Filter with single expression (greater than)")
results = Restaurant.query.filter(Restaurant.rating > 4.0)
assert len(results) == 2  # Sushi Master (4.8) and Burger Palace (4.5)
assert all(r.rating > 4.0 for r in results)
print("  PASSED: Filter with greater than expression")


# Test 9: Filter with single expression (less than or equal)
print("Test 9: Filter with single expression (less than or equal)")
results = Restaurant.query.filter(Restaurant.rating <= 4.0)
assert len(results) == 2  # Pizza Corner (3.5) and Taco Town (4.0)
assert all(r.rating <= 4.0 for r in results)
print("  PASSED: Filter with less than or equal expression")


# Test 10: Combined expressions with AND
print("Test 10: Combined expressions with AND")
combined = (Restaurant.rating > 4.0) & (Restaurant.price_level <= 2)
assert isinstance(combined, CombinedExpression)
assert combined.connector == "AND"
results = Restaurant.query.filter(combined)
assert len(results) == 1  # Only Burger Palace (rating 4.5, price_level 2)
assert results[0].name == "Burger Palace"
print("  PASSED: Combined expressions with AND")


# Test 11: Combined expressions with OR
print("Test 11: Combined expressions with OR")
combined = (Restaurant.cuisine == "American") | (Restaurant.cuisine == "Japanese")
assert isinstance(combined, CombinedExpression)
assert combined.connector == "OR"
results = Restaurant.query.filter(combined)
assert len(results) == 2  # Burger Palace and Sushi Master
cuisines = {r.cuisine for r in results}
assert cuisines == {"American", "Japanese"}
print("  PASSED: Combined expressions with OR")


# Test 12: Mixed expressions and kwargs
print("Test 12: Mixed expressions and kwargs")
results = Restaurant.query.filter(Restaurant.rating > 3.5, cuisine="Italian")
# Italian with rating > 3.5 - but Pizza Corner has rating 3.5, not > 3.5
assert len(results) == 0
print("  PASSED: Mixed expressions and kwargs (no results)")

results = Restaurant.query.filter(Restaurant.rating >= 3.5, cuisine="Italian")
assert len(results) == 1
assert results[0].name == "Pizza Corner"
print("  PASSED: Mixed expressions and kwargs (with results)")


# Test 13: Multiple simple expressions (implicit AND)
print("Test 13: Multiple simple expressions (implicit AND)")
results = Restaurant.query.filter(Restaurant.rating >= 4.0, Restaurant.price_level <= 2)
# rating >= 4.0 AND price_level <= 2
# Burger Palace (4.5, 2), Taco Town (4.0, 1)
assert len(results) == 2
names = {r.name for r in results}
assert names == {"Burger Palace", "Taco Town"}
print("  PASSED: Multiple simple expressions")


# Test 14: Complex nested expressions
print("Test 14: Complex nested expressions")
# (rating > 4.0 AND price_level == 3) OR (rating <= 4.0 AND price_level == 1)
combined = ((Restaurant.rating > 4.0) & (Restaurant.price_level == 3)) | (
    (Restaurant.rating <= 4.0) & (Restaurant.price_level == 1)
)
results = Restaurant.query.filter(combined)
# Sushi Master (4.8, 3) OR (Pizza Corner (3.5, 1) and Taco Town (4.0, 1))
assert len(results) == 3
names = {r.name for r in results}
assert names == {"Sushi Master", "Pizza Corner", "Taco Town"}
print("  PASSED: Complex nested expressions")


# Test 15: Expression with order_by and limit
print("Test 15: Expression with order_by and limit")
results = Restaurant.query.filter(Restaurant.rating >= 3.5, order_by="-rating", limit=2)
assert len(results) == 2
assert results[0].rating >= results[1].rating  # Descending order
print("  PASSED: Expression with order_by and limit")


# Test 16: Expression on integer field
print("Test 16: Expression on integer field")
results = Restaurant.query.filter(Restaurant.price_level == 1)
assert len(results) == 2  # Pizza Corner and Taco Town
assert all(r.price_level == 1 for r in results)
print("  PASSED: Expression on integer field")


# Test 17: Field equality comparison (should return bool, not Expression)
print("Test 17: Field equality comparison returns bool")
field1 = Restaurant.name
field2 = Restaurant.cuisine
field3 = Restaurant.name
# Comparing two different fields should return False
assert (field1 == field2) == False
# Comparing same field reference should return True
assert (field1 == field3) == True
print("  PASSED: Field equality comparison")


# Test 18: Expression repr
print("Test 18: Expression repr")
expr = Restaurant.rating > 4.0
repr_str = repr(expr)
assert "rating" in repr_str
assert "__gt" in repr_str
assert "4.0" in repr_str
print(f"  Expression repr: {repr_str}")
print("  PASSED: Expression repr")


# Test 19: CombinedExpression repr
print("Test 19: CombinedExpression repr")
combined = (Restaurant.rating > 4.0) & (Restaurant.cuisine == "American")
repr_str = repr(combined)
assert "AND" in repr_str
print(f"  CombinedExpression repr: {repr_str}")
print("  PASSED: CombinedExpression repr")


# Test 20: Empty filter with expression returns all (when combined with no other filters)
print("Test 20: Filter with tautology-like expression")
# This tests that expressions work alongside the normal filter behavior
results = Restaurant.query.filter(
    Restaurant.rating >= 0
)  # All restaurants have rating >= 0
assert len(results) == 4
print("  PASSED: Filter returns all matching records")


# Cleanup
print("\nCleaning up test data...")
for r in Restaurant.query.all():
    r.delete()

assert Restaurant.query.count() == 0
print("Cleanup complete.")

print("\n" + "=" * 50)
print("ALL EXPRESSION QUERY TESTS PASSED!")
print("=" * 50)
