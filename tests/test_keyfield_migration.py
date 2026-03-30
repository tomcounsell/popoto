"""
Regression tests for KeyField migration on filter()-loaded instances.

Issue: https://github.com/tomcounsell/popoto/issues/298

When a model instance is loaded via query.filter() and a KeyField value is
changed, calling .save() must remove the old Redis entry. Before the fix,
_create_lazy_model set _redis_key=None and _saved_field_values={}, so save()
could not detect or clean up the old key, resulting in duplicate entries.
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class MigrationModel(popoto.Model):
    category = popoto.KeyField()
    slug = popoto.KeyField()
    title = popoto.Field(null=True)


@pytest.fixture(autouse=True)
def cleanup():
    """Remove all MigrationModel instances before and after each test."""
    for item in MigrationModel.query.all():
        item.delete()
    yield
    for item in MigrationModel.query.all():
        item.delete()


def test_filter_loaded_instance_has_correct_redis_key():
    """filter()-loaded instance must have the same _redis_key as the original."""
    item = MigrationModel.create(category="books", slug="old-slug", title="Test Book")
    original_key = item._redis_key
    assert original_key is not None, "_redis_key should be set after create"

    loaded = MigrationModel.query.filter(category="books")[0]
    assert loaded._redis_key is not None, (
        "_redis_key must not be None on filter()-loaded instance"
    )
    assert loaded._redis_key == original_key, (
        f"filter()-loaded _redis_key mismatch: {loaded._redis_key} != {original_key}"
    )


def test_filter_loaded_instance_has_saved_field_values():
    """filter()-loaded instance must populate _saved_field_values with KeyField values."""
    MigrationModel.create(category="books", slug="my-slug", title="Title")

    loaded = MigrationModel.query.filter(category="books")[0]
    assert "category" in loaded._saved_field_values, (
        "_saved_field_values must include KeyField 'category'"
    )
    assert "slug" in loaded._saved_field_values, (
        "_saved_field_values must include KeyField 'slug'"
    )
    assert loaded._saved_field_values["category"] == "books"
    assert loaded._saved_field_values["slug"] == "my-slug"


def test_keyfield_change_on_filter_loaded_no_duplicates():
    """Changing a KeyField on a filter()-loaded instance must not create duplicates."""
    item = MigrationModel.create(category="books", slug="orig", title="A Book")
    old_key = item._redis_key

    loaded_items = MigrationModel.query.filter(category="books")
    assert len(loaded_items) == 1
    loaded = loaded_items[0]

    loaded.slug = "renamed"
    loaded.save(migrate_key=True)

    assert not POPOTO_REDIS_DB.exists(old_key), (
        f"Old key {old_key} should be deleted after migration"
    )

    all_items = MigrationModel.query.all()
    assert len(all_items) == 1, f"Expected 1 item after migration, got {len(all_items)}"
    assert all_items[0].slug == "renamed"


def test_get_loaded_instance_keyfield_migration():
    """get()-loaded instance KeyField change should also clean up old key."""
    item = MigrationModel.create(category="music", slug="orig", title="Song")
    old_key = item._redis_key

    got = MigrationModel.query.get(category="music", slug="orig")
    assert got._redis_key is not None
    got.slug = "new-slug"
    got.save(migrate_key=True)

    assert not POPOTO_REDIS_DB.exists(old_key), (
        "Old key should be deleted after get()-loaded migration"
    )
    all_items = MigrationModel.query.all()
    assert len(all_items) == 1, (
        f"Expected 1 item after get() migration, got {len(all_items)}"
    )


def test_bulk_filter_keyfield_change_on_multiple():
    """Changing KeyField on multiple filter()-loaded instances should not lose any."""
    MigrationModel.create(category="games", slug="a", title="Game A")
    MigrationModel.create(category="games", slug="b", title="Game B")
    MigrationModel.create(category="games", slug="c", title="Game C")

    loaded_items = MigrationModel.query.filter(category="games")
    assert len(loaded_items) == 3

    for i, obj in enumerate(loaded_items):
        obj.slug = f"renamed-{i}"
        obj.save(migrate_key=True)

    all_items = MigrationModel.query.filter(category="games")
    assert len(all_items) == 3, (
        f"Expected 3 items after bulk migration, got {len(all_items)}"
    )
    slugs = sorted([item.slug for item in all_items])
    assert slugs == ["renamed-0", "renamed-1", "renamed-2"], (
        f"Unexpected slugs: {slugs}"
    )


def test_chained_filter_with_keyfield_change():
    """Chained filter().filter() with KeyField change should clean up old key."""
    MigrationModel.create(category="tools", slug="hammer", title="Hammer")
    MigrationModel.create(category="tools", slug="wrench", title="Wrench")

    loaded_items = MigrationModel.query.filter(category="tools").filter(slug="hammer")
    assert len(loaded_items) == 1
    loaded = loaded_items[0]
    old_key = loaded._redis_key

    loaded.slug = "mallet"
    loaded.save(migrate_key=True)

    assert not POPOTO_REDIS_DB.exists(old_key)
    results = MigrationModel.query.filter(category="tools")
    assert len(results) == 2
    slugs = sorted([r.slug for r in results])
    assert slugs == ["mallet", "wrench"], (
        f"Unexpected slugs after chained filter migration: {slugs}"
    )


def test_partial_save_non_key_field_preserves_key():
    """Partial save (update_fields) on non-key field should not trigger key migration."""
    item = MigrationModel.create(category="food", slug="apple", title="Apple")
    original_key = item._redis_key

    loaded = MigrationModel.query.filter(category="food")[0]
    loaded.title = "Green Apple"
    loaded.save(update_fields=["title"])

    assert POPOTO_REDIS_DB.exists(original_key), (
        "Key should still exist for non-key partial save"
    )
    reloaded = MigrationModel.query.get(category="food", slug="apple")
    assert reloaded.title == "Green Apple"


def test_delete_after_keyfield_change():
    """Deleting an instance after KeyField migration should clean up properly."""
    MigrationModel.create(category="pets", slug="cat", title="Cat")

    loaded = MigrationModel.query.filter(category="pets")[0]
    loaded.slug = "dog"
    loaded.save(migrate_key=True)

    migrated = MigrationModel.query.filter(category="pets")[0]
    migrated.delete()

    all_items = MigrationModel.query.all()
    assert len(all_items) == 0, f"Expected 0 items after delete, got {len(all_items)}"


def test_unchanged_keyfield_no_unnecessary_migration():
    """Saving with unchanged KeyField should not trigger migration."""
    item = MigrationModel.create(category="cars", slug="sedan", title="Sedan")
    original_key = item._redis_key

    loaded = MigrationModel.query.filter(category="cars")[0]
    loaded.title = "Updated Sedan"
    loaded.save()

    assert POPOTO_REDIS_DB.exists(original_key), (
        "Key should still exist when KeyField unchanged"
    )
    reloaded = MigrationModel.query.get(category="cars", slug="sedan")
    assert reloaded.title == "Updated Sedan"


def test_new_unsaved_instance_has_redis_key():
    """New instance with all KeyFields present should have _redis_key computed."""
    new_item = MigrationModel(category="test", slug="new")
    assert new_item._redis_key is not None, (
        "New instance with all KeyFields should have _redis_key"
    )
    assert new_item._is_persisted is False, (
        "New instance should not be marked as persisted"
    )
