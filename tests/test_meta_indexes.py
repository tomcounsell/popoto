"""
Tests for Model Meta.indexes feature (Peewee-style)
"""

import pytest
from src.popoto import Model, KeyField, Field
from src.popoto.fields.shortcuts import IntField
from src.popoto.models.base import ModelException
from src.popoto.redis_db import POPOTO_REDIS_DB


# Flush Redis before tests
@pytest.fixture(autouse=True)
def flush_redis():
    POPOTO_REDIS_DB.flushdb()


class Transaction(Model):
    transaction_id = KeyField()
    from_account = Field()
    to_account = Field()
    amount = IntField()

    class Meta:
        indexes = (
            # (field_names, is_unique)
            (("from_account", "to_account"), True),  # Unique composite index
        )


class Product(Model):
    sku = KeyField()
    name = Field()
    category = Field()

    class Meta:
        indexes = (
            (("name",), False),  # Non-unique single-column index
            (("name", "category"), True),  # Unique composite index
        )


class NoIndexModel(Model):
    id = KeyField()
    value = Field()


def test_meta_indexes_structure_validation():
    """Test that Meta.indexes validates structure at class definition."""

    # Not a tuple/list
    with pytest.raises(ModelException, match="must be a tuple or list"):

        class Bad1(Model):
            id = KeyField()

            class Meta:
                indexes = "invalid"

    # Index not a 2-tuple
    with pytest.raises(ModelException, match="must be a 2-tuple"):

        class Bad2(Model):
            id = KeyField()

            class Meta:
                indexes = (("field",),)  # Missing is_unique flag

    # Field names not a tuple
    with pytest.raises(ModelException, match="must be a tuple/list"):

        class Bad3(Model):
            id = KeyField()

            class Meta:
                indexes = (("field", True),)  # Field names should be tuple

    # is_unique not boolean
    with pytest.raises(ModelException, match="must be boolean"):

        class Bad4(Model):
            id = KeyField()

            class Meta:
                indexes = ((("field",), "yes"),)  # Should be True/False

    # Unknown field
    with pytest.raises(ModelException, match="Unknown field"):

        class Bad5(Model):
            id = KeyField()

            class Meta:
                indexes = ((("nonexistent",), True),)


def test_unique_index_prevents_duplicates():
    """Test that unique indexes prevent duplicate combinations."""
    # Create first transaction
    tx1 = Transaction.create(
        transaction_id="tx1", from_account="A", to_account="B", amount=100
    )

    # Try to create duplicate combination - should fail
    with pytest.raises(ModelException, match="Unique index violation"):
        Transaction.create(
            transaction_id="tx2", from_account="A", to_account="B", amount=200
        )


def test_unique_index_allows_different_combinations():
    """Test that unique indexes allow different combinations."""
    tx1 = Transaction.create(
        transaction_id="tx1", from_account="A", to_account="B", amount=100
    )
    tx2 = Transaction.create(
        transaction_id="tx2", from_account="A", to_account="C", amount=200
    )
    tx3 = Transaction.create(
        transaction_id="tx3", from_account="B", to_account="C", amount=300
    )

    assert tx1 is not None
    assert tx2 is not None
    assert tx3 is not None


def test_unique_index_update_same_instance():
    """Test that updating an instance doesn't violate its own unique index."""
    tx = Transaction.create(
        transaction_id="tx1", from_account="A", to_account="B", amount=100
    )

    # Update non-indexed field - should succeed
    tx.amount = 150
    tx.save()

    # Verify update worked
    reloaded = Transaction.query.get(transaction_id="tx1")
    assert reloaded.amount == 150


def test_unique_index_update_to_duplicate_fails():
    """Test that updating to a duplicate combination fails."""
    tx1 = Transaction.create(
        transaction_id="tx1", from_account="A", to_account="B", amount=100
    )
    tx2 = Transaction.create(
        transaction_id="tx2", from_account="C", to_account="D", amount=200
    )

    # Try to update tx2 to match tx1's indexed fields
    tx2.from_account = "A"
    tx2.to_account = "B"

    with pytest.raises(ModelException, match="Unique index violation"):
        tx2.save()


def test_non_unique_index_allows_duplicates():
    """Test that non-unique indexes allow duplicate values."""
    p1 = Product.create(sku="SKU1", name="Widget", category="Tools")
    p2 = Product.create(sku="SKU2", name="Widget", category="Hardware")

    # Both have same name - should be allowed (name index is non-unique)
    assert p1 is not None
    assert p2 is not None


def test_unique_composite_on_non_unique_field():
    """Test unique composite index with a field that has non-unique single index."""
    p1 = Product.create(sku="SKU1", name="Widget", category="Tools")
    p2 = Product.create(sku="SKU2", name="Widget", category="Hardware")

    # name+category combo is unique even though name alone isn't
    with pytest.raises(ModelException, match="Unique index violation"):
        Product.create(sku="SKU3", name="Widget", category="Tools")


def test_index_cleanup_on_delete():
    """Test that deleting removes instance from index sets."""
    tx = Transaction.create(
        transaction_id="tx1", from_account="A", to_account="B", amount=100
    )

    # Delete the transaction
    tx.delete()

    # Should be able to create new transaction with same indexed fields
    tx2 = Transaction.create(
        transaction_id="tx2", from_account="A", to_account="B", amount=200
    )
    assert tx2 is not None


def test_no_indexes_model():
    """Test that models without indexes work normally."""
    m1 = NoIndexModel.create(id="1", value="a")
    m2 = NoIndexModel.create(id="2", value="a")

    # No unique constraints, so duplicates are fine
    assert m1 is not None
    assert m2 is not None


def test_null_values_in_unique_index():
    """Test that NULL values in indexed fields are handled."""
    tx1 = Transaction.create(transaction_id="tx1", from_account=None, to_account="B", amount=100)
    tx2 = Transaction.create(transaction_id="tx2", from_account=None, to_account="B", amount=200)

    # Multiple NULLs should be allowed (standard SQL behavior)
    # Two instances with (NULL, "B") should not conflict
    assert tx1 is not None
    assert tx2 is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
