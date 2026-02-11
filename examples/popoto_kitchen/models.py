"""Food delivery domain models for Popoto Kitchen demo.

These models demonstrate all major Popoto features:
- KeyField, AutoKeyField, UniqueKeyField
- SortedField for range queries
- GeoField for location-based queries
- Relationship for model associations
- DatetimeField with auto timestamps
- Model Meta options (ttl, order_by)
"""

from datetime import datetime
from typing import Optional

from popoto import Field, KeyField, Model, Relationship, SortedField
from popoto.fields.geo_field import GeoField
from popoto.fields.shortcuts import AutoKeyField, UniqueKeyField


class Restaurant(Model):
    """A restaurant in the food delivery system.

    Demonstrates:
    - KeyField for unique identifier
    - SortedField for rating-based queries
    - GeoField for location-based searches
    """

    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.cuisine})"


class MenuItem(Model):
    """A menu item belonging to a restaurant.

    Demonstrates:
    - AutoKeyField with uuid4 strategy
    - SortedField for price range queries
    - Relationship to parent model
    """

    item_id = AutoKeyField(strategy="uuid4")
    name = Field(type=str)
    description = Field(type=str, default="")
    price = SortedField(type=float)
    restaurant = Relationship(model=Restaurant)
    available = Field(type=bool, default=True)
    category = Field(type=str, default="Main")

    def __str__(self) -> str:
        return f"{self.name} (${self.price:.2f})"


class Customer(Model):
    """A customer who can place orders.

    Demonstrates:
    - KeyField for username lookup
    - UniqueKeyField for email uniqueness
    - GeoField for delivery address
    """

    username = KeyField()
    email = UniqueKeyField()
    name = Field(type=str)
    phone = Field(type=str, default="")
    address = GeoField()

    def __str__(self) -> str:
        return f"{self.name} (@{self.username})"


class Driver(Model):
    """A delivery driver.

    Demonstrates:
    - AutoKeyField for auto-generated ID
    - UniqueKeyField for phone uniqueness
    - SortedField for rating queries
    - GeoField for real-time location tracking
    """

    driver_id = AutoKeyField(strategy="uuid4")
    name = Field(type=str)
    phone = UniqueKeyField()
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)
    vehicle = Field(type=str, default="car")

    def __str__(self) -> str:
        status = "Available" if self.active else "Offline"
        return f"{self.name} ({status})"


class Order(Model):
    """An order in the food delivery system.

    Demonstrates:
    - AutoKeyField for order ID
    - Multiple Relationships (customer, restaurant, driver)
    - SortedField for total amount
    - Field for status tracking
    - Meta options for TTL and default ordering
    """

    order_id = AutoKeyField(strategy="uuid4")
    customer = Relationship(model=Customer)
    restaurant = Relationship(model=Restaurant)
    driver = Relationship(model=Driver, null=True)
    items = Field(type=list, default=list)  # List of item names
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    notes = Field(type=str, default="")
    created_at = Field(type=str)  # ISO format datetime string
    updated_at = Field(type=str)  # ISO format datetime string

    # Valid status values (private to avoid Popoto field detection)
    _STATUSES = ["pending", "confirmed", "preparing", "ready", "delivering", "delivered", "cancelled"]

    class Meta:
        order_by = "-created_at"  # Newest first

    def __str__(self) -> str:
        return f"Order {self.order_id[:8]}... ({self.status})"

    def save(self) -> "Order":
        """Override save to update timestamps."""
        now = datetime.utcnow().isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return super().save()

    def advance_status(self) -> bool:
        """Move order to next status in workflow."""
        try:
            current_idx = self._STATUSES.index(self.status)
            if current_idx < len(self._STATUSES) - 2:  # Don't go past 'delivered'
                self.status = self._STATUSES[current_idx + 1]
                self.save()
                return True
        except ValueError:
            pass
        return False


# Cuisine types for seeding
CUISINES = [
    "Italian",
    "Chinese",
    "Japanese",
    "Mexican",
    "Indian",
    "Thai",
    "American",
    "Mediterranean",
    "Korean",
    "Vietnamese",
]

# Menu categories
CATEGORIES = ["Appetizer", "Main", "Side", "Dessert", "Drink"]
