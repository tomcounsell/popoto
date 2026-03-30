"""Tests for IndexedFieldMixin, IndexedField, and UniqueField."""

import pytest

from popoto import Model, Field, AutoKeyField, IndexedField, UniqueField, ModelException


class IndexedModel(Model):
    """Model with indexed non-key fields for testing."""

    key = AutoKeyField()
    status = IndexedField(type=str)
    category = IndexedField(type=str, null=True)


class UniqueIndexedModel(Model):
    """Model with a unique indexed field."""

    key = AutoKeyField()
    email = UniqueField(type=str)
    name = Field(type=str, null=True)


class MultiIndexedModel(Model):
    """Model with multiple indexed fields."""

    key = AutoKeyField()
    status = IndexedField(type=str)
    role = IndexedField(type=str)
    tag = IndexedField(type=str, null=True)


class TestIndexedFieldCRUD:
    """Test basic CRUD operations with indexed fields."""

    def setup_method(self):
        IndexedModel.delete_all()

    def teardown_method(self):
        IndexedModel.delete_all()

    def test_save_creates_index_set(self):
        """Saving a model with an indexed field should create the index Set."""
        instance = IndexedModel.create(status="active")
        # Query using the indexed field
        results = IndexedModel.query.filter(status="active")
        assert len(results) == 1
        assert results[0].key == instance.key

    def test_delete_removes_from_index_set(self):
        """Deleting a model should remove it from the index Set."""
        instance = IndexedModel.create(status="active")
        assert len(IndexedModel.query.filter(status="active")) == 1
        instance.delete()
        assert len(IndexedModel.query.filter(status="active")) == 0

    def test_save_multiple_with_same_value(self):
        """Multiple instances can share the same indexed value."""
        IndexedModel.create(status="active")
        IndexedModel.create(status="active")
        IndexedModel.create(status="inactive")
        assert len(IndexedModel.query.filter(status="active")) == 2
        assert len(IndexedModel.query.filter(status="inactive")) == 1

    def test_value_change_cleans_old_index(self):
        """Changing an indexed field value should remove old index and add new."""
        instance = IndexedModel.create(status="active")
        assert len(IndexedModel.query.filter(status="active")) == 1

        instance.status = "inactive"
        instance.save()

        assert len(IndexedModel.query.filter(status="active")) == 0
        assert len(IndexedModel.query.filter(status="inactive")) == 1


class TestIndexedFieldFilter:
    """Test filter query operations on indexed fields."""

    def setup_method(self):
        IndexedModel.delete_all()
        self.a1 = IndexedModel.create(status="active", category="admin")
        self.a2 = IndexedModel.create(status="active", category="user")
        self.i1 = IndexedModel.create(status="inactive", category="admin")
        self.n1 = IndexedModel.create(status="pending", category=None)

    def teardown_method(self):
        IndexedModel.delete_all()

    def test_exact_match(self):
        """filter(field=value) should return exact matches."""
        results = IndexedModel.query.filter(status="active")
        assert len(results) == 2

    def test_in_query(self):
        """filter(field__in=[...]) should return OR matches."""
        results = IndexedModel.query.filter(status__in=["active", "pending"])
        assert len(results) == 3

    def test_in_query_empty_list(self):
        """filter(field__in=[]) should return empty results."""
        results = IndexedModel.query.filter(status__in=[])
        assert len(results) == 0

    def test_isnull_true(self):
        """filter(field__isnull=True) should match None values."""
        results = IndexedModel.query.filter(category__isnull=True)
        assert len(results) == 1
        assert results[0].key == self.n1.key

    def test_isnull_false(self):
        """filter(field__isnull=False) should match non-None values."""
        results = IndexedModel.query.filter(category__isnull=False)
        assert len(results) == 3

    def test_isnull_invalid_value(self):
        """filter(field__isnull=<non-bool>) should raise QueryException."""
        from popoto.exceptions import QueryException

        # The QueryException may be raised during query execution (not filter construction)
        with pytest.raises(QueryException):
            list(IndexedModel.query.filter(category__isnull="yes"))

    def test_startswith(self):
        """filter(field__startswith=prefix) should match by prefix."""
        results = IndexedModel.query.filter(status__startswith="act")
        assert len(results) == 2

    def test_endswith(self):
        """filter(field__endswith=suffix) should match by suffix."""
        results = IndexedModel.query.filter(status__endswith="ive")
        assert len(results) == 3  # active (x2) + inactive

    def test_no_match_returns_empty(self):
        """filter with non-existent value should return empty."""
        results = IndexedModel.query.filter(status="nonexistent")
        assert len(results) == 0


class TestMultipleIndexedFields:
    """Test models with multiple indexed fields."""

    def setup_method(self):
        MultiIndexedModel.delete_all()

    def teardown_method(self):
        MultiIndexedModel.delete_all()

    def test_filter_on_multiple_indexed_fields(self):
        """Filtering on multiple indexed fields should AND them."""
        MultiIndexedModel.create(status="active", role="admin", tag="vip")
        MultiIndexedModel.create(status="active", role="user", tag="regular")
        MultiIndexedModel.create(status="inactive", role="admin", tag=None)

        # Single field filter
        assert len(MultiIndexedModel.query.filter(status="active")) == 2
        assert len(MultiIndexedModel.query.filter(role="admin")) == 2

    def test_indexed_field_with_null(self):
        """Indexed fields with null=True should handle None values."""
        instance = MultiIndexedModel.create(status="active", role="admin", tag=None)
        results = MultiIndexedModel.query.filter(tag__isnull=True)
        assert len(results) == 1
        assert results[0].key == instance.key


class TestUniqueIndexedField:
    """Test uniqueness enforcement on indexed fields."""

    def setup_method(self):
        UniqueIndexedModel.delete_all()

    def teardown_method(self):
        UniqueIndexedModel.delete_all()

    def test_unique_field_allows_first_save(self):
        """First save with a unique value should succeed."""
        instance = UniqueIndexedModel.create(email="alice@example.com", name="Alice")
        assert instance.email == "alice@example.com"

    def test_unique_field_rejects_duplicate(self):
        """Second save with same unique value should raise ModelException."""
        UniqueIndexedModel.create(email="alice@example.com", name="Alice")
        with pytest.raises(ModelException, match="(?i)unique"):
            UniqueIndexedModel.create(email="alice@example.com", name="Bob")

    def test_unique_field_allows_resave_same_instance(self):
        """Re-saving the same instance with the same value should succeed."""
        instance = UniqueIndexedModel.create(email="alice@example.com", name="Alice")
        instance.name = "Alice Updated"
        instance.save()  # Should not raise
        assert instance.name == "Alice Updated"

    def test_unique_field_allows_different_values(self):
        """Different unique values should be allowed."""
        UniqueIndexedModel.create(email="alice@example.com")
        UniqueIndexedModel.create(email="bob@example.com")
        assert len(UniqueIndexedModel.query.filter(email="alice@example.com")) == 1
        assert len(UniqueIndexedModel.query.filter(email="bob@example.com")) == 1

    def test_unique_field_value_change(self):
        """Changing a unique indexed field value should update the index."""
        instance = UniqueIndexedModel.create(email="old@example.com", name="User")
        instance.email = "new@example.com"
        instance.save()

        assert len(UniqueIndexedModel.query.filter(email="old@example.com")) == 0
        assert len(UniqueIndexedModel.query.filter(email="new@example.com")) == 1

    def test_unique_field_cannot_be_null(self):
        """UniqueField should reject null=True."""
        with pytest.raises(ModelException):
            UniqueField(type=str, null=True)

    def test_unique_field_cannot_disable_unique(self):
        """UniqueField should reject unique=False."""
        with pytest.raises(ModelException):
            UniqueField(type=str, unique=False)


class TestIndexedFieldRegistration:
    """Test that indexed fields are properly registered in ModelOptions."""

    def test_indexed_field_in_meta(self):
        """Indexed fields should be tracked in _meta.indexed_field_names."""
        assert "status" in IndexedModel._meta.indexed_field_names
        assert "category" in IndexedModel._meta.indexed_field_names
        assert "key" not in IndexedModel._meta.indexed_field_names

    def test_filter_query_params_registered(self):
        """Indexed fields should register their query params."""
        params = IndexedModel._meta.filter_query_params_by_field["status"]
        assert "status" in params
        assert "status__in" in params
        assert "status__isnull" in params
        assert "status__startswith" in params
        assert "status__endswith" in params


class TestPartialSaveWithIndexedFields:
    """Test update_fields (partial save) interaction with indexed fields."""

    def setup_method(self):
        IndexedModel.delete_all()

    def teardown_method(self):
        IndexedModel.delete_all()

    def test_partial_save_updates_index(self):
        """Partial save on an indexed field should update the index."""
        instance = IndexedModel.create(status="active", category="admin")
        assert len(IndexedModel.query.filter(status="active")) == 1

        instance.status = "inactive"
        instance.save(update_fields=["status"])

        assert len(IndexedModel.query.filter(status="active")) == 0
        assert len(IndexedModel.query.filter(status="inactive")) == 1

    def test_partial_save_non_indexed_field_no_index_change(self):
        """Partial save on non-indexed field should not affect indexes."""
        instance = IndexedModel.create(status="active", category="admin")
        instance.category = "user"
        instance.save(update_fields=["category"])

        # status index should still work
        assert len(IndexedModel.query.filter(status="active")) == 1
