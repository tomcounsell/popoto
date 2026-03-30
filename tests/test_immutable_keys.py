"""Tests for KeyField immutability and migrate_key parameter."""

import pytest

from popoto import Model, KeyField, Field, AutoKeyField
from popoto.exceptions import KeyMutationError


class ImmutableKeyModel(Model):
    """Model with KeyFields for testing immutability."""

    name = KeyField(type=str)
    category = KeyField(type=str)
    description = Field(type=str, null=True)


class AutoKeyModel(Model):
    """Model with AutoKeyField (should be exempt from immutability check)."""

    key = AutoKeyField()
    name = Field(type=str, null=True)


class SingleKeyModel(Model):
    """Model with a single KeyField."""

    slug = KeyField(type=str)
    title = Field(type=str, null=True)


class TestKeyFieldImmutability:
    """Test that KeyField values cannot be changed without migrate_key=True."""

    def setup_method(self):
        ImmutableKeyModel.delete_all()

    def teardown_method(self):
        ImmutableKeyModel.delete_all()

    def test_mutation_raises_error(self):
        """Changing a KeyField and saving should raise KeyMutationError."""
        instance = ImmutableKeyModel.create(
            name="original", category="test", description="desc"
        )
        instance.name = "changed"
        with pytest.raises(KeyMutationError):
            instance.save()

    def test_mutation_error_message_quality(self):
        """KeyMutationError message should include field name and values."""
        instance = ImmutableKeyModel.create(
            name="original", category="test", description="desc"
        )
        instance.name = "changed"
        with pytest.raises(KeyMutationError, match="name"):
            instance.save()

    def test_mutation_error_includes_old_and_new_values(self):
        """Error message should mention both old and new values."""
        instance = ImmutableKeyModel.create(name="old_name", category="test")
        instance.name = "new_name"
        with pytest.raises(KeyMutationError, match="old_name"):
            instance.save()

    def test_migrate_key_true_succeeds(self):
        """save(migrate_key=True) should allow KeyField mutation."""
        instance = ImmutableKeyModel.create(
            name="original", category="test", description="desc"
        )
        instance.name = "migrated"
        instance.save(migrate_key=True)  # Should not raise

        # Verify migration happened
        results = ImmutableKeyModel.query.filter(name="migrated")
        assert len(results) == 1
        assert results[0].description == "desc"

        # Old key should be gone
        assert len(ImmutableKeyModel.query.filter(name="original")) == 0

    def test_changing_any_key_field_raises(self):
        """Changing any one of multiple KeyFields should raise."""
        instance = ImmutableKeyModel.create(name="test", category="original")
        instance.category = "changed"
        with pytest.raises(KeyMutationError, match="category"):
            instance.save()

    def test_non_key_field_change_allowed(self):
        """Changing non-key fields should not trigger immutability check."""
        instance = ImmutableKeyModel.create(
            name="test", category="cat", description="old"
        )
        instance.description = "new"
        instance.save()  # Should not raise
        assert instance.description == "new"

    def test_resave_same_values_allowed(self):
        """Re-saving with identical KeyField values should not raise."""
        instance = ImmutableKeyModel.create(
            name="test", category="cat", description="desc"
        )
        instance.description = "updated desc"
        instance.save()  # Should not raise -- key fields unchanged


class TestFreshInstanceExemption:
    """Test that fresh instances (not loaded from DB) do not false-positive."""

    def setup_method(self):
        SingleKeyModel.delete_all()

    def teardown_method(self):
        SingleKeyModel.delete_all()

    def test_fresh_instance_first_save(self):
        """A brand new instance (never saved) should save without error."""
        instance = SingleKeyModel(slug="new_item", title="Test")
        instance.save()  # Should not raise

    def test_constructor_created_instance_no_false_positive(self):
        """Instance created via constructor (no query.get) should save fine."""
        instance = SingleKeyModel(slug="test_slug", title="Test")
        instance.save()  # Should not raise -- _saved_field_values is empty


class TestAutoKeyFieldExemption:
    """Test that AutoKeyField is exempt from immutability check."""

    def setup_method(self):
        AutoKeyModel.delete_all()

    def teardown_method(self):
        AutoKeyModel.delete_all()

    def test_auto_key_field_exempt(self):
        """AutoKeyField should not trigger immutability check."""
        instance = AutoKeyModel.create(name="test")
        instance.name = "updated"
        instance.save()  # Should not raise -- auto key field is exempt

    def test_auto_key_model_normal_save(self):
        """AutoKeyField models should save and load normally."""
        instance = AutoKeyModel.create(name="hello")
        loaded = AutoKeyModel.query.get(
            **{
                field_name: getattr(instance, field_name)
                for field_name in instance._meta.key_field_names
            }
        )
        assert loaded.name == "hello"


class TestMigrateKeyWithQuery:
    """Test migrate_key=True with instances loaded via query."""

    def setup_method(self):
        SingleKeyModel.delete_all()

    def teardown_method(self):
        SingleKeyModel.delete_all()

    def test_query_loaded_instance_mutation_blocked(self):
        """Instances loaded via query should be protected by immutability."""
        SingleKeyModel.create(slug="original", title="Test")
        loaded = SingleKeyModel.query.get(slug="original")
        loaded.slug = "changed"
        with pytest.raises(KeyMutationError):
            loaded.save()

    def test_query_loaded_instance_migrate_key_works(self):
        """migrate_key=True should work on query-loaded instances."""
        SingleKeyModel.create(slug="original", title="Test")
        loaded = SingleKeyModel.query.get(slug="original")
        loaded.slug = "migrated"
        loaded.save(migrate_key=True)

        assert len(SingleKeyModel.query.filter(slug="migrated")) == 1
        assert len(SingleKeyModel.query.filter(slug="original")) == 0

    def test_migrate_key_preserves_non_key_fields(self):
        """Key migration should preserve all non-key field values."""
        SingleKeyModel.create(slug="original", title="My Title")
        loaded = SingleKeyModel.query.get(slug="original")
        loaded.slug = "migrated"
        loaded.save(migrate_key=True)

        migrated = SingleKeyModel.query.get(slug="migrated")
        assert migrated.title == "My Title"
