"""Tests for Model.delete_all() and async_delete_all() methods."""

import asyncio
import pytest

import popoto
from popoto.redis_db import POPOTO_REDIS_DB
from popoto.fields.geo_field import GeoField
from popoto.fields.shortcuts import AutoKeyField, UniqueKeyField


class Restaurant(popoto.Model):
    """Test model with SortedField and GeoField."""

    name = popoto.KeyField()
    cuisine = popoto.Field(type=str)
    rating = popoto.SortedField(type=float)
    location = GeoField()


class Customer(popoto.Model):
    """Test model with UniqueKeyField."""

    username = popoto.KeyField()
    email = UniqueKeyField()
    name = popoto.Field(type=str)


class MenuItem(popoto.Model):
    """Test model with Relationship."""

    item_id = AutoKeyField(strategy="uuid4")
    name = popoto.Field(type=str)
    price = popoto.SortedField(type=float)
    restaurant = popoto.Relationship(model=Restaurant)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data before and after each test."""
    # Clean before
    for key in POPOTO_REDIS_DB.scan_iter("Restaurant:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.scan_iter("Customer:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.scan_iter("MenuItem:*"):
        POPOTO_REDIS_DB.delete(key)
    # Clean index keys too
    for pattern in ["$*Restaurant*", "$*Customer*", "$*MenuItem*"]:
        for key in POPOTO_REDIS_DB.scan_iter(pattern):
            POPOTO_REDIS_DB.delete(key)

    yield

    # Clean after
    for key in POPOTO_REDIS_DB.scan_iter("Restaurant:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.scan_iter("Customer:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.scan_iter("MenuItem:*"):
        POPOTO_REDIS_DB.delete(key)
    for pattern in ["$*Restaurant*", "$*Customer*", "$*MenuItem*"]:
        for key in POPOTO_REDIS_DB.scan_iter(pattern):
            POPOTO_REDIS_DB.delete(key)


class TestDeleteAll:
    """Tests for Model.delete_all() method."""

    def test_delete_all_removes_all_instances(self):
        """delete_all() should remove all instances of the model."""
        # Create multiple restaurants
        for i in range(10):
            Restaurant(
                name=f"Restaurant_{i}",
                cuisine="Test",
                rating=4.0,
                location=(40.7128 + i * 0.01, -74.0060),
            ).save()

        assert Restaurant.query.count() == 10

        # Delete all
        deleted = Restaurant.delete_all()

        assert deleted == 10
        assert Restaurant.query.count() == 0

    def test_delete_all_returns_zero_when_empty(self):
        """delete_all() should return 0 when no instances exist."""
        assert Restaurant.query.count() == 0
        deleted = Restaurant.delete_all()
        assert deleted == 0

    def test_delete_all_cleans_sorted_index(self):
        """delete_all() should clean up SortedField indexes."""
        # Create restaurants with ratings
        Restaurant(name="A", cuisine="Test", rating=4.5, location=(40.7, -74.0)).save()
        Restaurant(name="B", cuisine="Test", rating=3.5, location=(40.8, -74.1)).save()
        Restaurant(name="C", cuisine="Test", rating=5.0, location=(40.9, -74.2)).save()

        # Verify sorted index works
        high_rated = list(Restaurant.query.filter(rating__gte=4.0))
        assert len(high_rated) == 2

        # Delete all
        Restaurant.delete_all()

        # Verify sorted index is empty
        assert list(Restaurant.query.filter(rating__gte=0)) == []

    def test_delete_all_cleans_unique_index(self):
        """delete_all() should clean up UniqueKeyField indexes."""
        # Create customer with unique email
        Customer(username="user1", email="test@example.com", name="Test User").save()

        # Verify unique constraint
        with pytest.raises(popoto.ModelException):
            Customer(username="user2", email="test@example.com", name="Other").save()

        # Delete all
        Customer.delete_all()

        # Should be able to reuse the email now
        Customer(username="user3", email="test@example.com", name="New User").save()
        assert Customer.query.count() == 1

    def test_delete_all_cleans_geo_index(self):
        """delete_all() should clean up GeoField indexes."""
        # Create restaurants with locations
        Restaurant(name="NYC", cuisine="Test", rating=4.0, location=(40.7128, -74.0060)).save()
        Restaurant(name="LA", cuisine="Test", rating=4.0, location=(34.0522, -118.2437)).save()

        # Verify geo query works
        nearby = list(
            Restaurant.query.filter(
                location=(40.7128, -74.0060),
                location_radius=100,
                location_radius_unit="km",
            )
        )
        assert len(nearby) == 1
        assert nearby[0].name == "NYC"

        # Delete all
        Restaurant.delete_all()

        # Verify geo index is empty
        nearby = list(
            Restaurant.query.filter(
                location=(40.7128, -74.0060),
                location_radius=10000,
                location_radius_unit="km",
            )
        )
        assert len(nearby) == 0

    def test_delete_all_with_relationships(self):
        """delete_all() should work with models that have Relationships."""
        # Create restaurant and menu items
        restaurant = Restaurant(
            name="Test Restaurant",
            cuisine="Test",
            rating=4.0,
            location=(40.7128, -74.0060),
        )
        restaurant.save()

        for i in range(5):
            MenuItem(
                name=f"Item_{i}",
                price=10.0 + i,
                restaurant=restaurant,
            ).save()

        assert MenuItem.query.count() == 5

        # Delete menu items first (referencing model)
        deleted = MenuItem.delete_all()
        assert deleted == 5
        assert MenuItem.query.count() == 0

        # Restaurant should still exist
        assert Restaurant.query.count() == 1

        # Now delete restaurant
        Restaurant.delete_all()
        assert Restaurant.query.count() == 0

    def test_delete_all_with_batch_size(self):
        """delete_all() should respect batch_size parameter."""
        # Create many restaurants
        for i in range(25):
            Restaurant(
                name=f"Restaurant_{i}",
                cuisine="Test",
                rating=4.0,
                location=(40.7 + i * 0.001, -74.0),
            ).save()

        assert Restaurant.query.count() == 25

        # Delete with small batch size
        deleted = Restaurant.delete_all(batch_size=10)

        assert deleted == 25
        assert Restaurant.query.count() == 0


class TestAsyncDeleteAll:
    """Tests for Model.async_delete_all() method."""

    @pytest.mark.asyncio
    async def test_async_delete_all_removes_all_instances(self):
        """async_delete_all() should remove all instances."""
        # Create restaurants
        for i in range(5):
            Restaurant(
                name=f"Restaurant_{i}",
                cuisine="Test",
                rating=4.0,
                location=(40.7 + i * 0.01, -74.0),
            ).save()

        assert Restaurant.query.count() == 5

        # Async delete all
        deleted = await Restaurant.async_delete_all()

        assert deleted == 5
        assert Restaurant.query.count() == 0

    @pytest.mark.asyncio
    async def test_async_delete_all_returns_zero_when_empty(self):
        """async_delete_all() should return 0 when no instances exist."""
        deleted = await Restaurant.async_delete_all()
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_async_delete_all_cleans_indexes(self):
        """async_delete_all() should clean up all indexes."""
        # Create customer with unique email
        Customer(username="async_user", email="async@example.com", name="Async").save()

        # Delete via async
        await Customer.async_delete_all()

        # Unique constraint should be released
        Customer(username="new_user", email="async@example.com", name="New").save()
        assert Customer.query.count() == 1
