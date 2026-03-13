"""Tests for bulk operations (bulk_create, bulk_update, bulk_delete)."""

import sys
import os
import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


# Test model for bulk operations
class Restaurant(popoto.Model):
    name = popoto.KeyField()
    rating = popoto.SortedField(type=float, default=0.0)
    status = popoto.KeyField(type=str)  # KeyField for filtering
    cuisine = popoto.Field(type=str, default="")


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up all Restaurant instances before and after each test."""
    for item in Restaurant.query.all():
        item.delete()
    yield
    for item in Restaurant.query.all():
        item.delete()


# === Test bulk_create ===


def test_bulk_create_basic():
    """Test basic bulk_create with a list of instances."""
    restaurants = [
        Restaurant(
            name="Restaurant A", rating=4.0, status="pending", cuisine="Italian"
        ),
        Restaurant(
            name="Restaurant B", rating=4.5, status="pending", cuisine="Japanese"
        ),
        Restaurant(
            name="Restaurant C", rating=3.8, status="pending", cuisine="Mexican"
        ),
    ]

    created = Restaurant.bulk_create(restaurants)

    assert len(created) == 3
    assert Restaurant.query.count() == 3

    # Verify all instances are retrievable
    assert Restaurant.query.get(name="Restaurant A", status="pending") is not None
    assert Restaurant.query.get(name="Restaurant B", status="pending") is not None
    assert Restaurant.query.get(name="Restaurant C", status="pending") is not None

    # Verify field values
    a = Restaurant.query.get(name="Restaurant A", status="pending")
    assert a.rating == 4.0
    assert a.status == "pending"
    assert a.cuisine == "Italian"


def test_bulk_create_empty_list():
    """Test bulk_create with empty list returns empty list."""
    result = Restaurant.bulk_create([])
    assert result == []
    assert Restaurant.query.count() == 0


def test_bulk_create_with_batching():
    """Test bulk_create with small batch_size to verify batching works."""
    restaurants = [
        Restaurant(name=f"Batch Restaurant {i}", rating=float(i), status="active")
        for i in range(10)
    ]

    # Use batch_size of 3 to ensure multiple batches
    created = Restaurant.bulk_create(restaurants, batch_size=3)

    assert len(created) == 10
    assert Restaurant.query.count() == 10

    # Verify all were created
    for i in range(10):
        r = Restaurant.query.get(name=f"Batch Restaurant {i}", status="active")
        assert r is not None
        assert r.rating == float(i)


# === Test bulk_update ===


def test_bulk_update_from_all():
    """Test bulk_update on all instances from query.all()."""
    # Setup
    Restaurant.bulk_create(
        [
            Restaurant(name="Update Test 1", status="pending"),
            Restaurant(name="Update Test 2", status="pending"),
            Restaurant(name="Update Test 3", status="pending"),
        ]
    )

    # Update all restaurants using query.all()
    all_restaurants = Restaurant.query.all()
    count = Restaurant.bulk_update(all_restaurants, rating=5.0)

    assert count == 3

    # Verify all ratings updated
    for r in Restaurant.query.all():
        assert r.rating == 5.0


def test_bulk_update_from_list():
    """Test bulk_update on a plain list of instances."""
    # Setup
    r1 = Restaurant.create(
        name="List Update 1", rating=3.0, status="active", cuisine="Italian"
    )
    r2 = Restaurant.create(
        name="List Update 2", rating=3.0, status="active", cuisine="Italian"
    )
    r3 = Restaurant.create(
        name="List Update 3", rating=3.0, status="active", cuisine="Mexican"
    )

    # Update the list (just r1 and r2)
    count = Restaurant.bulk_update([r1, r2], rating=5.0, cuisine="French")

    assert count == 2

    # Verify updates
    r1_reloaded = Restaurant.query.get(name="List Update 1", status="active")
    assert r1_reloaded.rating == 5.0
    assert r1_reloaded.cuisine == "French"

    r2_reloaded = Restaurant.query.get(name="List Update 2", status="active")
    assert r2_reloaded.rating == 5.0
    assert r2_reloaded.cuisine == "French"

    # r3 should be unchanged
    r3_reloaded = Restaurant.query.get(name="List Update 3", status="active")
    assert r3_reloaded.rating == 3.0
    assert r3_reloaded.cuisine == "Mexican"


def test_bulk_update_empty_list():
    """Test bulk_update with empty list returns 0."""
    result = Restaurant.bulk_update([], rating=5.0)
    assert result == 0


def test_bulk_update_no_updates():
    """Test bulk_update with no update fields returns 0."""
    Restaurant.create(name="No Update Test", status="pending")

    result = Restaurant.bulk_update(Restaurant.query.all())
    assert result == 0


def test_bulk_update_with_batching():
    """Test bulk_update with small batch_size."""
    # Setup 10 restaurants
    Restaurant.bulk_create(
        [Restaurant(name=f"Batch Update {i}", status="pending") for i in range(10)]
    )

    # Update with small batch size
    all_restaurants = Restaurant.query.all()
    count = Restaurant.bulk_update(all_restaurants, rating=9.9, batch_size=3)

    assert count == 10

    # Verify all updated
    for r in Restaurant.query.all():
        assert r.rating == 9.9


def test_bulk_update_sorted_field():
    """Test bulk_update properly updates sorted field indexes."""
    # Setup
    Restaurant.bulk_create(
        [
            Restaurant(name="Sorted Update 1", rating=1.0, status="active"),
            Restaurant(name="Sorted Update 2", rating=2.0, status="active"),
            Restaurant(name="Sorted Update 3", rating=3.0, status="active"),
        ]
    )

    # Update ratings
    all_restaurants = Restaurant.query.all()
    Restaurant.bulk_update(all_restaurants, rating=10.0)

    # Verify sorted field queries work correctly with new values
    high_rated = Restaurant.query.filter(rating__gte=9.0)
    assert len(high_rated) == 3


# === Test bulk_delete ===


def test_bulk_delete_from_all():
    """Test bulk_delete on all instances from query.all()."""
    # Setup
    Restaurant.bulk_create(
        [
            Restaurant(name="Delete All 1", status="active"),
            Restaurant(name="Delete All 2", status="active"),
            Restaurant(name="Delete All 3", status="active"),
        ]
    )

    assert Restaurant.query.count() == 3

    # Delete all
    count = Restaurant.bulk_delete(Restaurant.query.all())

    assert count == 3
    assert Restaurant.query.count() == 0


def test_bulk_delete_from_list():
    """Test bulk_delete on a plain list of instances."""
    # Setup
    r1 = Restaurant.create(name="List Delete 1", status="active")
    r2 = Restaurant.create(name="List Delete 2", status="active")
    r3 = Restaurant.create(name="List Delete 3", status="active")

    assert Restaurant.query.count() == 3

    # Delete only r1 and r2
    count = Restaurant.bulk_delete([r1, r2])

    assert count == 2
    assert Restaurant.query.count() == 1

    # r3 should still exist
    assert Restaurant.query.get(name="List Delete 3", status="active") is not None


def test_bulk_delete_empty_list():
    """Test bulk_delete with empty list returns 0."""
    result = Restaurant.bulk_delete([])
    assert result == 0


def test_bulk_delete_with_batching():
    """Test bulk_delete with small batch_size."""
    # Setup 10 restaurants
    Restaurant.bulk_create(
        [Restaurant(name=f"Batch Delete {i}", status="active") for i in range(10)]
    )

    assert Restaurant.query.count() == 10

    # Delete with small batch size
    count = Restaurant.bulk_delete(Restaurant.query.all(), batch_size=3)

    assert count == 10
    assert Restaurant.query.count() == 0


def test_bulk_delete_partial():
    """Test bulk_delete on a subset of instances."""
    # Setup
    Restaurant.bulk_create(
        [Restaurant(name=f"Partial Delete {i}", status="active") for i in range(5)]
    )

    assert Restaurant.query.count() == 5

    # Get only 3 of them and delete
    to_delete = Restaurant.query.all()[:3]
    count = Restaurant.bulk_delete(to_delete)

    assert count == 3
    assert Restaurant.query.count() == 2


# === Test sorted field index cleanup ===


class ProductWithSortedField(popoto.Model):
    """Model with sorted field to test index cleanup."""

    sku = popoto.KeyField()
    price = popoto.SortedField(type=float, default=0.0)


@pytest.fixture
def cleanup_products():
    """Clean up ProductWithSortedField instances."""
    for p in ProductWithSortedField.query.all():
        p.delete()
    yield
    for p in ProductWithSortedField.query.all():
        p.delete()


def test_bulk_delete_cleans_sorted_indexes(cleanup_products):
    """Test that bulk_delete properly cleans up sorted field indexes."""
    # Setup
    products = [
        ProductWithSortedField(sku=f"SKU-{i}", price=float(10 * i)) for i in range(5)
    ]
    ProductWithSortedField.bulk_create(products)

    # Verify sorted field queries work
    expensive = ProductWithSortedField.query.filter(price__gte=30.0)
    assert len(expensive) == 2  # SKU-3 (30.0) and SKU-4 (40.0)

    # Bulk delete expensive products
    ProductWithSortedField.bulk_delete(expensive)

    # Verify count
    assert ProductWithSortedField.query.count() == 3

    # Verify sorted index was cleaned up - no results above 30
    remaining_expensive = ProductWithSortedField.query.filter(price__gte=30.0)
    assert len(remaining_expensive) == 0


def test_bulk_create_returns_same_instances():
    """Test that bulk_create returns the same instances that were passed in."""
    restaurants = [
        Restaurant(name="Same Instance 1", status="active"),
        Restaurant(name="Same Instance 2", status="active"),
    ]

    created = Restaurant.bulk_create(restaurants)

    # Should be the same objects
    assert created[0] is restaurants[0]
    assert created[1] is restaurants[1]

    # And they should have their _redis_key set
    assert created[0]._redis_key is not None
    assert created[1]._redis_key is not None


def test_bulk_update_modifies_instances_in_place():
    """Test that bulk_update modifies the instances passed to it."""
    r1 = Restaurant.create(name="In Place 1", rating=1.0, status="active")
    r2 = Restaurant.create(name="In Place 2", rating=2.0, status="active")

    Restaurant.bulk_update([r1, r2], rating=9.0)

    # Instances should be modified in place
    assert r1.rating == 9.0
    assert r2.rating == 9.0


# === Async bulk operations tests ===


@pytest.fixture(autouse=True)
def reset_async_connection():
    """Reset async Redis connection for each test."""
    import src.popoto.redis_db as redis_db_module
    import asyncio

    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()


class TestAsyncBulkOperations:
    """Gap 1: Tests for async bulk operations."""

    @pytest.mark.asyncio
    async def test_async_bulk_create_basic(self):
        """Gap 1: async_bulk_create basic functionality."""
        restaurants = [
            Restaurant(
                name="Async Restaurant A",
                rating=4.0,
                status="pending",
                cuisine="Italian",
            ),
            Restaurant(
                name="Async Restaurant B",
                rating=4.5,
                status="pending",
                cuisine="Japanese",
            ),
            Restaurant(
                name="Async Restaurant C",
                rating=3.8,
                status="pending",
                cuisine="Mexican",
            ),
        ]

        result = await Restaurant.async_bulk_create(restaurants)

        assert len(result) == 3
        assert Restaurant.query.count() == 3

        # Verify one can be retrieved by name
        retrieved = Restaurant.query.get(name="Async Restaurant A", status="pending")
        assert retrieved is not None
        assert retrieved.cuisine == "Italian"

    @pytest.mark.asyncio
    async def test_async_bulk_update_from_all(self):
        """Gap 1: async_bulk_update on all instances."""
        # Create 3 restaurants via sync bulk_create
        Restaurant.bulk_create(
            [
                Restaurant(name="Async Update 1", status="pending"),
                Restaurant(name="Async Update 2", status="pending"),
                Restaurant(name="Async Update 3", status="pending"),
            ]
        )

        # Get all via sync query.all()
        all_restaurants = Restaurant.query.all()

        # Call async_bulk_update
        count = await Restaurant.async_bulk_update(all_restaurants, rating=9.0)

        assert count == 3

        # Verify all ratings are 9.0 via sync query
        for r in Restaurant.query.all():
            assert r.rating == 9.0

    @pytest.mark.asyncio
    async def test_async_bulk_delete_from_all(self):
        """Gap 1: async_bulk_delete on all instances."""
        # Create 3 restaurants via sync bulk_create
        Restaurant.bulk_create(
            [
                Restaurant(name="Async Delete 1", status="active"),
                Restaurant(name="Async Delete 2", status="active"),
                Restaurant(name="Async Delete 3", status="active"),
            ]
        )

        # Get all via sync query.all()
        all_restaurants = Restaurant.query.all()

        # Call async_bulk_delete
        count = await Restaurant.async_bulk_delete(all_restaurants)

        assert count == 3

        # Verify count is 0
        assert Restaurant.query.count() == 0
