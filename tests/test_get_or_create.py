"""Tests for get_or_create and update_or_create methods."""

import pytest

from popoto import Model, KeyField, Field, SortedField


class SimpleModel(Model):
    key = KeyField(type=str)
    name = Field(type=str, null=True)
    count = Field(type=int, default=0)


class SortedModel(Model):
    key = KeyField(type=str)
    timestamp = SortedField(type=float)
    value = Field(type=str, null=True)


class TestGetOrCreate:
    """Test get_or_create method."""

    def setup_method(self):
        SimpleModel.delete_all()
        SortedModel.delete_all()

    def teardown_method(self):
        SimpleModel.delete_all()
        SortedModel.delete_all()

    def test_creates_when_not_exists(self):
        """Should create new instance when lookup finds nothing."""
        instance, created = SimpleModel.get_or_create(key="new_key")

        assert created is True
        assert instance.key == "new_key"
        assert SimpleModel.query.count() == 1

    def test_returns_existing_when_exists(self):
        """Should return existing instance without creating."""
        original = SimpleModel.create(key="existing", name="Original")

        instance, created = SimpleModel.get_or_create(key="existing")

        assert created is False
        assert instance.key == "existing"
        assert instance.name == "Original"
        assert SimpleModel.query.count() == 1

    def test_defaults_used_only_on_create(self):
        """Defaults should be used when creating, not for lookup."""
        instance, created = SimpleModel.get_or_create(
            key="with_defaults", defaults={"name": "Default Name", "count": 42}
        )

        assert created is True
        assert instance.name == "Default Name"
        assert instance.count == 42

    def test_defaults_not_applied_to_existing(self):
        """Defaults should not overwrite existing instance values."""
        original = SimpleModel.create(key="existing", name="Original", count=10)

        instance, created = SimpleModel.get_or_create(
            key="existing", defaults={"name": "New Name", "count": 99}
        )

        assert created is False
        assert instance.name == "Original"
        assert instance.count == 10

    def test_with_sorted_field(self):
        """Should work with models containing SortedField."""
        instance, created = SortedModel.get_or_create(
            key="sorted_key", defaults={"timestamp": 123.456, "value": "test"}
        )

        assert created is True
        assert instance.timestamp == 123.456

        # Verify SortedField index was updated
        results = list(SortedModel.query.filter(timestamp__gte=100.0))
        assert len(results) == 1

    def test_lookup_with_key_field(self):
        """Should support lookup by key field."""
        SimpleModel.create(key="multi", name="Target", count=5)
        SimpleModel.create(key="other", name="Other", count=5)

        instance, created = SimpleModel.get_or_create(
            key="multi", defaults={"count": 99}
        )

        assert created is False
        assert instance.name == "Target"


class TestUpdateOrCreate:
    """Test update_or_create method."""

    def setup_method(self):
        SimpleModel.delete_all()
        SortedModel.delete_all()

    def teardown_method(self):
        SimpleModel.delete_all()
        SortedModel.delete_all()

    def test_creates_when_not_exists(self):
        """Should create new instance when lookup finds nothing."""
        instance, created = SimpleModel.update_or_create(
            key="new_key", defaults={"name": "Created", "count": 1}
        )

        assert created is True
        assert instance.key == "new_key"
        assert instance.name == "Created"
        assert instance.count == 1

    def test_updates_when_exists(self):
        """Should update existing instance with defaults."""
        original = SimpleModel.create(key="existing", name="Original", count=10)

        instance, created = SimpleModel.update_or_create(
            key="existing", defaults={"name": "Updated", "count": 20}
        )

        assert created is False
        assert instance.key == "existing"
        assert instance.name == "Updated"
        assert instance.count == 20

        # Verify persisted
        reloaded = SimpleModel.query.get(key="existing")
        assert reloaded.name == "Updated"
        assert reloaded.count == 20

    def test_partial_update(self):
        """Should only update fields specified in defaults."""
        original = SimpleModel.create(key="partial", name="Keep This", count=5)

        instance, created = SimpleModel.update_or_create(
            key="partial", defaults={"count": 100}
        )

        assert created is False
        assert instance.name == "Keep This"
        assert instance.count == 100

    def test_with_sorted_field_update(self):
        """Should properly update SortedField and its index."""
        original = SortedModel.create(key="sorted", timestamp=100.0, value="old")

        instance, created = SortedModel.update_or_create(
            key="sorted", defaults={"timestamp": 200.0, "value": "new"}
        )

        assert created is False
        assert instance.timestamp == 200.0
        assert instance.value == "new"

        # Verify SortedField index was updated
        old_results = list(SortedModel.query.filter(timestamp__lte=150.0))
        new_results = list(SortedModel.query.filter(timestamp__gte=150.0))
        assert len(old_results) == 0
        assert len(new_results) == 1

    def test_empty_defaults_on_update(self):
        """Should handle empty defaults gracefully."""
        original = SimpleModel.create(key="no_change", name="Original", count=5)

        instance, created = SimpleModel.update_or_create(key="no_change")

        assert created is False
        assert instance.name == "Original"
        assert instance.count == 5


class TestAsyncMethods:
    """Test async versions of get_or_create and update_or_create."""

    def setup_method(self):
        SimpleModel.delete_all()

    def teardown_method(self):
        SimpleModel.delete_all()

    @pytest.mark.asyncio
    async def test_async_get_or_create_new(self):
        """Async get_or_create should create when not exists."""
        instance, created = await SimpleModel.async_get_or_create(
            key="async_new", defaults={"name": "Async Created"}
        )

        assert created is True
        assert instance.key == "async_new"
        assert instance.name == "Async Created"

    @pytest.mark.asyncio
    async def test_async_get_or_create_existing(self):
        """Async get_or_create should return existing."""
        SimpleModel.create(key="async_existing", name="Original")

        instance, created = await SimpleModel.async_get_or_create(key="async_existing")

        assert created is False
        assert instance.name == "Original"

    @pytest.mark.asyncio
    async def test_async_update_or_create_new(self):
        """Async update_or_create should create when not exists."""
        instance, created = await SimpleModel.async_update_or_create(
            key="async_update_new", defaults={"name": "Created", "count": 1}
        )

        assert created is True
        assert instance.name == "Created"

    @pytest.mark.asyncio
    async def test_async_update_or_create_existing(self):
        """Async update_or_create should update when exists."""
        SimpleModel.create(key="async_update_existing", name="Original", count=1)

        instance, created = await SimpleModel.async_update_or_create(
            key="async_update_existing", defaults={"name": "Updated", "count": 99}
        )

        assert created is False
        assert instance.name == "Updated"
        assert instance.count == 99
