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

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class MigrationModel(popoto.Model):
    category = popoto.KeyField()
    slug = popoto.KeyField()
    title = popoto.Field(null=True)


def _cleanup():
    for item in MigrationModel.query.all():
        item.delete()


# ── Setup ──────────────────────────────────────────────────────────────
_cleanup()


# ── Test 1: filter()-loaded instance has correct _redis_key ────────────
item = MigrationModel.create(category="books", slug="old-slug", title="Test Book")
original_key = item._redis_key
assert original_key is not None, "_redis_key should be set after create"

loaded = MigrationModel.query.filter(category="books")[0]
assert loaded._redis_key is not None, "_redis_key must not be None on filter()-loaded instance"
assert loaded._redis_key == original_key, (
    f"filter()-loaded _redis_key mismatch: {loaded._redis_key} != {original_key}"
)

_cleanup()


# ── Test 2: filter()-loaded instance has _saved_field_values populated ─
item = MigrationModel.create(category="books", slug="my-slug", title="Title")

loaded = MigrationModel.query.filter(category="books")[0]
assert "category" in loaded._saved_field_values, "_saved_field_values must include KeyField 'category'"
assert "slug" in loaded._saved_field_values, "_saved_field_values must include KeyField 'slug'"
assert loaded._saved_field_values["category"] == "books"
assert loaded._saved_field_values["slug"] == "my-slug"

_cleanup()


# ── Test 3: change KeyField on filter()-loaded instance → no duplicates
item = MigrationModel.create(category="books", slug="orig", title="A Book")
old_key = item._redis_key

loaded_items = MigrationModel.query.filter(category="books")
assert len(loaded_items) == 1
loaded = loaded_items[0]

# Change a KeyField value and save
loaded.slug = "renamed"
loaded.save()

# The old Redis hash key must be gone
assert not POPOTO_REDIS_DB.exists(old_key), f"Old key {old_key} should be deleted after migration"

# Only one instance should exist
all_items = MigrationModel.query.all()
assert len(all_items) == 1, f"Expected 1 item after migration, got {len(all_items)}"
assert all_items[0].slug == "renamed"

_cleanup()


# ── Test 4: get()-loaded instance also works (already correct path) ────
item = MigrationModel.create(category="music", slug="orig", title="Song")
old_key = item._redis_key

got = MigrationModel.query.get(category="music", slug="orig")
assert got._redis_key is not None
got.slug = "new-slug"
got.save()

assert not POPOTO_REDIS_DB.exists(old_key), "Old key should be deleted after get()-loaded migration"
all_items = MigrationModel.query.all()
assert len(all_items) == 1, f"Expected 1 item after get() migration, got {len(all_items)}"

_cleanup()


# ── Test 5: bulk filter, change KeyField on multiple, save all ─────────
MigrationModel.create(category="games", slug="a", title="Game A")
MigrationModel.create(category="games", slug="b", title="Game B")
MigrationModel.create(category="games", slug="c", title="Game C")

loaded_items = MigrationModel.query.filter(category="games")
assert len(loaded_items) == 3

for i, obj in enumerate(loaded_items):
    obj.slug = f"renamed-{i}"
    obj.save()

all_items = MigrationModel.query.filter(category="games")
assert len(all_items) == 3, f"Expected 3 items after bulk migration, got {len(all_items)}"
slugs = sorted([item.slug for item in all_items])
assert slugs == ["renamed-0", "renamed-1", "renamed-2"], f"Unexpected slugs: {slugs}"

_cleanup()


# ── Test 6: chained filter().filter() with KeyField change ─────────────
MigrationModel.create(category="tools", slug="hammer", title="Hammer")
MigrationModel.create(category="tools", slug="wrench", title="Wrench")

# Chained filter
loaded_items = MigrationModel.query.filter(category="tools").filter(slug="hammer")
assert len(loaded_items) == 1
loaded = loaded_items[0]
old_key = loaded._redis_key

loaded.slug = "mallet"
loaded.save()

assert not POPOTO_REDIS_DB.exists(old_key)
results = MigrationModel.query.filter(category="tools")
assert len(results) == 2
slugs = sorted([r.slug for r in results])
assert slugs == ["mallet", "wrench"], f"Unexpected slugs after chained filter migration: {slugs}"

_cleanup()


# ── Test 7: update_fields partial save with non-key field ──────────────
# Partial save (update_fields) with a KeyField change triggers key migration
# in save(), but only writes the listed fields to the new hash. Use full save
# for KeyField changes. This test verifies partial save works correctly when
# only non-key fields are updated on a filter()-loaded instance.
item = MigrationModel.create(category="food", slug="apple", title="Apple")
original_key = item._redis_key

loaded = MigrationModel.query.filter(category="food")[0]
loaded.title = "Green Apple"
loaded.save(update_fields=["title"])

assert POPOTO_REDIS_DB.exists(original_key), "Key should still exist for non-key partial save"
reloaded = MigrationModel.query.get(category="food", slug="apple")
assert reloaded.title == "Green Apple"

_cleanup()


# ── Test 8: delete after KeyField change cleans up properly ────────────
item = MigrationModel.create(category="pets", slug="cat", title="Cat")

loaded = MigrationModel.query.filter(category="pets")[0]
loaded.slug = "dog"
loaded.save()

# Now delete the migrated instance
migrated = MigrationModel.query.filter(category="pets")[0]
migrated.delete()

all_items = MigrationModel.query.all()
assert len(all_items) == 0, f"Expected 0 items after delete, got {len(all_items)}"

_cleanup()


# ── Test 9: unchanged KeyField does NOT trigger unnecessary migration ──
item = MigrationModel.create(category="cars", slug="sedan", title="Sedan")
original_key = item._redis_key

loaded = MigrationModel.query.filter(category="cars")[0]
# Change only a non-key field
loaded.title = "Updated Sedan"
loaded.save()

# Key should remain the same
assert POPOTO_REDIS_DB.exists(original_key), "Key should still exist when KeyField unchanged"
reloaded = MigrationModel.query.get(category="cars", slug="sedan")
assert reloaded.title == "Updated Sedan"

_cleanup()


# ── Test 10: new unsaved instance has _redis_key=None (no regression) ──
new_item = MigrationModel(category="test", slug="new")
# _redis_key is computed in __init__ when all KeyFields are present
# This is correct existing behavior
assert new_item._redis_key is not None, "New instance with all KeyFields should have _redis_key"
assert new_item._is_persisted is False, "New instance should not be marked as persisted"

_cleanup()

print("All KeyField migration tests passed!")
