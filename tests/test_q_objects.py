"""
Tests for Q objects and complex query expressions.

Tests cover:
- Basic Q object (equivalent to kwargs)
- OR with two Q objects
- AND with two Q objects
- Complex nested expressions
- Negation (~Q)
- Mixed Q objects and kwargs
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto import Model, KeyField, SortedField, Q
from src.popoto.redis_db import POPOTO_REDIS_DB


class Product(Model):
    """Test model for Q object queries."""

    sku = KeyField(type=str)
    category = KeyField(type=str)
    status = KeyField(type=str)
    price = SortedField(type=float)
    rating = SortedField(type=float)


@pytest.fixture(autouse=True)
def setup_and_teardown():
    """Set up test data and clean up after each test."""
    # Clean up any existing test data
    for product in Product.query.all():
        product.delete()

    # Create test products
    Product.create(
        sku="LAPTOP001",
        category="electronics",
        status="active",
        price=999.99,
        rating=4.5,
    )
    Product.create(
        sku="PHONE001",
        category="electronics",
        status="active",
        price=599.99,
        rating=4.8,
    )
    Product.create(
        sku="BOOK001",
        category="books",
        status="active",
        price=29.99,
        rating=4.2,
    )
    Product.create(
        sku="BOOK002",
        category="books",
        status="discontinued",
        price=19.99,
        rating=3.5,
    )
    Product.create(
        sku="SHIRT001",
        category="clothing",
        status="active",
        price=49.99,
        rating=4.0,
    )
    Product.create(
        sku="PANTS001",
        category="clothing",
        status="discontinued",
        price=79.99,
        rating=2.5,
    )

    yield

    # Clean up after test
    for product in Product.query.all():
        product.delete()


class TestBasicQObject:
    """Tests for basic Q object functionality."""

    def test_basic_q_object_single_filter(self):
        """Q object with single filter should work like kwargs."""
        # Using Q object
        results_q = Product.query.filter(Q(category="electronics"))
        # Using kwargs
        results_kwargs = Product.query.filter(category="electronics")

        assert len(results_q) == len(results_kwargs) == 2
        assert set(p.sku for p in results_q) == set(p.sku for p in results_kwargs)

    def test_basic_q_object_multiple_filters(self):
        """Q object with multiple filters should AND them together."""
        results = Product.query.filter(Q(category="electronics", status="active"))
        assert len(results) == 2
        for product in results:
            assert product.category == "electronics"
            assert product.status == "active"

    def test_empty_q_object_returns_all(self):
        """Empty Q() should return all objects."""
        results = Product.query.filter(Q())
        assert len(results) == 6


class TestOrLogic:
    """Tests for OR logic using Q objects."""

    def test_or_two_q_objects(self):
        """OR of two Q objects should return union of results."""
        results = Product.query.filter(Q(category="electronics") | Q(category="books"))
        assert len(results) == 4

        categories = {p.category for p in results}
        assert categories == {"electronics", "books"}

    def test_or_with_different_fields(self):
        """OR should work with different fields."""
        results = Product.query.filter(
            Q(category="electronics") | Q(status="discontinued")
        )

        # Should include: electronics (2) + discontinued (2) - overlap (0) = 4
        assert len(results) == 4

    def test_or_with_range_filter(self):
        """OR should work with SortedField range filters."""
        # High rating OR low price
        results = Product.query.filter(Q(rating__gte=4.5) | Q(price__lte=30.0))

        # rating >= 4.5: LAPTOP001 (4.5), PHONE001 (4.8) = 2
        # price <= 30.0: BOOK001 (29.99), BOOK002 (19.99) = 2
        # No overlap
        assert len(results) == 4

    def test_multiple_or_chain(self):
        """Multiple OR operations should chain correctly."""
        results = Product.query.filter(
            Q(category="electronics") | Q(category="books") | Q(category="clothing")
        )
        assert len(results) == 6


class TestAndLogic:
    """Tests for explicit AND logic using Q objects."""

    def test_and_two_q_objects(self):
        """AND of two Q objects should return intersection."""
        results = Product.query.filter(Q(category="books") & Q(status="active"))
        assert len(results) == 1
        assert results[0].sku == "BOOK001"

    def test_and_with_range_filters(self):
        """AND should work with SortedField range filters."""
        results = Product.query.filter(Q(price__gte=50.0) & Q(rating__gte=4.0))

        # price >= 50: LAPTOP001, PHONE001, PANTS001, SHIRT001 (no, price=49.99)
        # Actually SHIRT001 is 49.99, so: LAPTOP001, PHONE001, PANTS001
        # rating >= 4.0: LAPTOP001, PHONE001, BOOK001, SHIRT001
        # Intersection: LAPTOP001, PHONE001
        assert len(results) == 2
        skus = {p.sku for p in results}
        assert skus == {"LAPTOP001", "PHONE001"}


class TestComplexExpressions:
    """Tests for complex nested Q expressions."""

    def test_or_then_and(self):
        """(A | B) & C should work correctly."""
        # (electronics OR books) AND active
        results = Product.query.filter(
            (Q(category="electronics") | Q(category="books")) & Q(status="active")
        )

        # electronics active: LAPTOP001, PHONE001
        # books active: BOOK001
        assert len(results) == 3

    def test_and_then_or(self):
        """(A & B) | C should work correctly."""
        # (books AND active) OR discontinued
        results = Product.query.filter(
            (Q(category="books") & Q(status="active")) | Q(status="discontinued")
        )

        # books AND active: BOOK001
        # discontinued: BOOK002, PANTS001
        # Union: BOOK001, BOOK002, PANTS001
        assert len(results) == 3

    def test_deeply_nested(self):
        """Deeply nested expressions should work."""
        # ((electronics OR books) AND active) OR (clothing AND discontinued)
        results = Product.query.filter(
            ((Q(category="electronics") | Q(category="books")) & Q(status="active"))
            | (Q(category="clothing") & Q(status="discontinued"))
        )

        # (electronics OR books) AND active: LAPTOP001, PHONE001, BOOK001
        # clothing AND discontinued: PANTS001
        assert len(results) == 4


class TestNegation:
    """Tests for NOT logic using ~Q."""

    def test_simple_negation(self):
        """~Q should return everything except matches."""
        results = Product.query.filter(~Q(status="discontinued"))

        # All active products
        assert len(results) == 4
        for product in results:
            assert product.status != "discontinued"

    def test_negation_with_or(self):
        """~Q combined with OR should work."""
        # NOT electronics OR books
        results = Product.query.filter(~Q(category="electronics") | Q(category="books"))

        # NOT electronics: books (2), clothing (2) = 4
        # books: 2 (already included above)
        # Union: all non-electronics = 4
        assert len(results) == 4

    def test_negation_with_and(self):
        """~Q combined with AND should work."""
        # NOT discontinued AND high rating
        results = Product.query.filter(~Q(status="discontinued") & Q(rating__gte=4.0))

        # NOT discontinued: LAPTOP001, PHONE001, BOOK001, SHIRT001
        # rating >= 4.0: LAPTOP001, PHONE001, BOOK001, SHIRT001
        # Intersection: LAPTOP001, PHONE001, BOOK001, SHIRT001
        assert len(results) == 4

    def test_double_negation(self):
        """~~Q should be equivalent to Q."""
        results_double_neg = Product.query.filter(~~Q(category="electronics"))
        results_positive = Product.query.filter(Q(category="electronics"))

        assert len(results_double_neg) == len(results_positive)
        assert set(p.sku for p in results_double_neg) == set(
            p.sku for p in results_positive
        )


class TestMixedQAndKwargs:
    """Tests for mixing Q objects with kwargs."""

    def test_q_and_kwargs(self):
        """Q object combined with kwargs should AND them."""
        results = Product.query.filter(Q(category="electronics"), status="active")

        assert len(results) == 2
        for product in results:
            assert product.category == "electronics"
            assert product.status == "active"

    def test_multiple_q_and_kwargs(self):
        """Multiple Q objects with kwargs should all be ANDed."""
        results = Product.query.filter(
            Q(category="books") | Q(category="electronics"), status="active"
        )

        # (books OR electronics) AND active
        assert len(results) == 3

    def test_q_with_result_modifiers(self):
        """Q objects should work with limit and order_by."""
        results = Product.query.filter(Q(status="active"), order_by="-price", limit=2)

        assert len(results) == 2
        assert results[0].price >= results[1].price

    def test_q_with_values(self):
        """Q objects should work with values projection."""
        results = Product.query.filter(
            Q(category="electronics"), values=("sku", "price")
        )

        assert len(results) == 2
        assert all(isinstance(r, dict) for r in results)
        assert all("sku" in r and "price" in r for r in results)


class TestQObjectRepr:
    """Tests for Q object string representation."""

    def test_simple_repr(self):
        """Simple Q object should have readable repr."""
        q = Q(status="active")
        assert "status" in repr(q)
        assert "active" in repr(q)

    def test_or_repr(self):
        """OR expression should show OR in repr."""
        q = Q(status="active") | Q(category="books")
        assert "OR" in repr(q)

    def test_and_repr(self):
        """AND expression should show AND in repr."""
        q = Q(status="active") & Q(category="books")
        assert "AND" in repr(q)

    def test_negated_repr(self):
        """Negated Q should show ~ in repr."""
        q = ~Q(status="discontinued")
        assert "~" in repr(q)


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_q_with_no_results(self):
        """Q that matches nothing should return empty list."""
        results = Product.query.filter(Q(category="nonexistent"))
        assert results == []

    def test_complex_q_with_no_results(self):
        """Complex Q that matches nothing should return empty list."""
        results = Product.query.filter(Q(category="nonexistent") & Q(status="active"))
        assert results == []

    def test_q_type_error(self):
        """Q combined with non-Q should raise TypeError."""
        with pytest.raises(TypeError):
            Q(status="active") | "not a Q object"

        with pytest.raises(TypeError):
            Q(status="active") & 123
