"""
Tests for Model Meta.order_by feature
"""

import pytest
from src.popoto import Model, KeyField, Field
from src.popoto.fields.shortcuts import SortedField
from src.popoto.redis_db import POPOTO_REDIS_DB


# Flush Redis before tests
@pytest.fixture(autouse=True)
def flush_redis():
    POPOTO_REDIS_DB.flushdb()


class Product(Model):
    name = KeyField()
    price = SortedField()  # Use SortedField for filtering
    stock = Field()

    class Meta:
        order_by = "price"


class ProductDescending(Model):
    name = KeyField()
    price = Field()

    class Meta:
        order_by = "-price"


class ProductNoDefault(Model):
    name = KeyField()
    price = Field()


def test_meta_order_by_ascending():
    """Test that Meta.order_by applies ascending order by default."""
    Product.create(name="Widget", price=50, stock=10)
    Product.create(name="Gadget", price=30, stock=5)
    Product.create(name="Doodad", price=40, stock=15)

    results = Product.query.all()

    assert len(results) == 3
    assert results[0].name == "Gadget"  # price=30
    assert results[1].name == "Doodad"  # price=40
    assert results[2].name == "Widget"  # price=50


def test_meta_order_by_descending():
    """Test that Meta.order_by with '-' prefix applies descending order."""
    ProductDescending.create(name="Widget", price=50)
    ProductDescending.create(name="Gadget", price=30)
    ProductDescending.create(name="Doodad", price=40)

    results = ProductDescending.query.all()

    assert len(results) == 3
    assert results[0].name == "Widget"  # price=50
    assert results[1].name == "Doodad"  # price=40
    assert results[2].name == "Gadget"  # price=30


def test_explicit_order_by_overrides_meta():
    """Test that explicit order_by parameter overrides Meta.order_by."""
    Product.create(name="Alpha", price=50, stock=5)
    Product.create(name="Bravo", price=30, stock=10)
    Product.create(name="Charlie", price=40, stock=15)

    # Default Meta.order_by is "price" (ascending), but we override with "-price" (descending)
    results = Product.query.all(order_by="-price")

    assert len(results) == 3
    assert results[0].name == "Alpha"  # price=50
    assert results[1].name == "Charlie"  # price=40
    assert results[2].name == "Bravo"  # price=30


def test_filter_with_meta_order_by():
    """Test that filter() applies Meta.order_by."""
    Product.create(name="Widget", price=50, stock=10)
    Product.create(name="Gadget", price=30, stock=10)
    Product.create(name="Doodad", price=40, stock=10)

    # Filter by price (SortedField), should still apply Meta.order_by
    results = Product.query.filter(price__gte=25)

    assert len(results) == 3
    assert results[0].name == "Gadget"  # price=30
    assert results[1].name == "Doodad"  # price=40
    assert results[2].name == "Widget"  # price=50


def test_no_default_order_by():
    """Test that models without Meta.order_by work normally."""
    ProductNoDefault.create(name="Widget", price=50)
    ProductNoDefault.create(name="Gadget", price=30)
    ProductNoDefault.create(name="Doodad", price=40)

    results = ProductNoDefault.query.all()

    # Results should not be ordered by price (insertion order or undefined)
    assert len(results) == 3
    # We can't assert specific order without default ordering


def test_meta_order_by_with_limit():
    """Test that Meta.order_by works with limit parameter."""
    Product.create(name="Widget", price=50, stock=10)
    Product.create(name="Gadget", price=30, stock=5)
    Product.create(name="Doodad", price=40, stock=15)

    results = Product.query.all(limit=2)

    assert len(results) == 2
    assert results[0].name == "Gadget"  # price=30
    assert results[1].name == "Doodad"  # price=40


def test_invalid_order_by_field_raises():
    """Test that invalid field in Meta.order_by raises ModelException."""
    from src.popoto.models.base import ModelException

    with pytest.raises(ModelException, match="does not exist"):

        class InvalidProduct(Model):
            name = KeyField()

            class Meta:
                order_by = "nonexistent_field"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
