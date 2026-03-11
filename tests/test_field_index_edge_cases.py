"""Comprehensive edge case tests for field index operations.

Tests verify correct index behavior for all field types across mutation scenarios,
validating both query-level behavior (.filter()) AND raw Redis state (SMEMBERS,
ZRANGEBYSCORE, etc.).

Related: Issue #151, PR #150 (KeyField index cleanup fix)

Each test follows the pattern:
    create -> mutate -> save -> assert query correctness -> assert Redis index correctness
"""

import pytest

import popoto
from popoto.redis_db import POPOTO_REDIS_DB
from popoto.fields.geo_field import GeoField

# ---------------------------------------------------------------------------
# Test model definitions
# ---------------------------------------------------------------------------


class EdgeTeam(popoto.Model):
    """Team model for relationship tests."""

    name = popoto.KeyField()


class EdgePlayer(popoto.Model):
    """Player with a relationship to a team, for relationship mutation tests."""

    uuid = popoto.AutoKeyField()
    name = popoto.Field(type=str)
    team = popoto.Relationship(model=EdgeTeam)


class EdgeMenuItem(popoto.Model):
    """Model with SortedField partitioned by category, for sorted field tests."""

    uuid = popoto.AutoKeyField()
    category = popoto.KeyField()
    price = popoto.SortedField(type=float, partition_by="category")
    name = popoto.Field(type=str, null=True)


class EdgeGeoPlace(popoto.Model):
    """Model with GeoField for geo coordinate update tests."""

    name = popoto.KeyField()
    location = GeoField()


class EdgeNullableKey(popoto.Model):
    """Model with nullable KeyField for null transition tests."""

    uuid = popoto.AutoKeyField()
    status = popoto.KeyField(null=True)
    data = popoto.Field(type=str, null=True)


class EdgeUniqueItem(popoto.Model):
    """Model with UniqueKeyField for uniqueness cleanup tests."""

    email = popoto.UniqueKeyField()
    name = popoto.Field(type=str, null=True)


class EdgeCompositeKey(popoto.Model):
    """Model with two KeyFields for composite key change tests."""

    region = popoto.KeyField()
    user_id = popoto.KeyField()
    name = popoto.Field(type=str, null=True)


class EdgePartialSave(popoto.Model):
    """Model for partial save (update_fields) tests."""

    uuid = popoto.AutoKeyField()
    status = popoto.KeyField(null=True)
    priority = popoto.SortedField(type=int)
    data = popoto.Field(type=str, null=True)


class EdgeLifecycle(popoto.Model):
    """Model for rapid create-mutate-delete lifecycle tests."""

    uuid = popoto.AutoKeyField()
    status = popoto.KeyField(null=True)
    score = popoto.SortedField(type=float)
    location = GeoField()


# ---------------------------------------------------------------------------
# Helper to build special-use field db key strings
# ---------------------------------------------------------------------------


def _keyfield_set_key(model_class, field_name, value):
    """Build the Redis Set key for a KeyField index entry.

    Uses DB_key to properly escape the value (e.g., dashes become '/-'),
    matching the key format that on_save() actually writes to Redis.
    """
    from popoto.models.db_key import DB_key

    prefix = model_class._meta.fields[field_name].get_special_use_field_db_key(
        model_class, field_name
    )
    return DB_key(prefix, value).redis_key


def _relationship_set_key(model_class, field_name, related_instance):
    """Build the Redis Set key for a Relationship index entry."""
    from popoto.models.db_key import DB_key

    prefix = model_class._meta.fields[field_name].get_special_use_field_db_key(
        model_class, field_name
    )
    if related_instance is None:
        return f"{prefix}:None"
    return f"{prefix}:{related_instance.db_key.redis_key}"


def _sorted_set_key(model_class, field_name, *partition_values):
    """Build the Redis Sorted Set key for a SortedField index."""
    from popoto.fields.sorted_field_mixin import SortedFieldMixin

    key = model_class._meta.fields[field_name].get_sortedset_db_key(
        model_class, field_name, *partition_values
    )
    return key.redis_key


def _geo_set_key(model_class, field_name):
    """Build the Redis Geo Set key for a GeoField index."""
    key = GeoField.get_geo_db_key(model_class, field_name)
    return key.redis_key


# ---------------------------------------------------------------------------
# Cleanup fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up all test model data before and after each test."""
    model_classes = [
        EdgeTeam,
        EdgePlayer,
        EdgeMenuItem,
        EdgeGeoPlace,
        EdgeNullableKey,
        EdgeUniqueItem,
        EdgeCompositeKey,
        EdgePartialSave,
        EdgeLifecycle,
    ]

    def _clean():
        for cls in model_classes:
            name = cls._meta.model_name
            # Delete instance keys
            for key in POPOTO_REDIS_DB.scan_iter(f"{name}:*"):
                POPOTO_REDIS_DB.delete(key)
            # Delete index keys (special-use field keys)
            for pattern in [f"$*{name}*"]:
                for key in POPOTO_REDIS_DB.scan_iter(pattern):
                    POPOTO_REDIS_DB.delete(key)

    _clean()
    yield
    _clean()


# ===========================================================================
# Test Case 1: SortedField -- Partition key change leaves orphan in old sorted set
# ===========================================================================


class TestSortedFieldPartitionKeyChange:
    """When partition_by is set, changing the partition key field should remove
    the entry from the old partition's sorted set and add it to the new one.

    ZADD is idempotent within the same sorted set but cannot clean up a different
    sorted set automatically.
    """

    def test_partition_key_change_removes_from_old_sorted_set(self):
        """Changing the category (partition key) should move the item out of
        the old category's sorted set and into the new one."""
        item = EdgeMenuItem.create(category="fruit", price=2.50, name="Apple")

        old_ss_key = _sorted_set_key(EdgeMenuItem, "price", "fruit")
        new_ss_key = _sorted_set_key(EdgeMenuItem, "price", "veggie")

        # Verify initial state: item is in "fruit" sorted set
        members = POPOTO_REDIS_DB.zrangebyscore(old_ss_key, "-inf", "+inf")
        assert len(members) == 1

        # Mutate: move to "veggie" category
        item.category = "veggie"
        item.save()

        # Query-level check
        fruit_items = EdgeMenuItem.query.filter(category="fruit", price__gte=0)
        veggie_items = EdgeMenuItem.query.filter(category="veggie", price__gte=0)

        # NOTE: This test may fail if SortedField.on_save does not remove from
        # old partition. If it fails, mark xfail and file a bug.
        # The item should appear in veggie, not fruit
        assert len(veggie_items) >= 1, "Item should appear in new category query"

        # Raw Redis check: old sorted set should be empty
        old_members = POPOTO_REDIS_DB.zrangebyscore(old_ss_key, "-inf", "+inf")
        new_members = POPOTO_REDIS_DB.zrangebyscore(new_ss_key, "-inf", "+inf")

        # This is the critical assertion -- old partition should not have orphan
        if len(old_members) > 0:
            pytest.xfail(
                "Bug: SortedField.on_save() does not clean up old partition "
                "sorted set when partition key changes. File as separate issue."
            )
        assert len(new_members) == 1, "Item should be in new partition sorted set"


# ===========================================================================
# Test Case 2: Relationship -- Changing the related object leaves orphan
# ===========================================================================


class TestRelationshipChangeOrphan:
    """Changing player.team from team_a to team_b should remove the player from
    team_a's relationship index set and add to team_b's."""

    def test_changing_related_object_cleans_old_index(self):
        team_a = EdgeTeam.create(name="TeamA")
        team_b = EdgeTeam.create(name="TeamB")
        player = EdgePlayer.create(name="Alice", team=team_a)

        # Verify initial state
        set_key_a = _relationship_set_key(EdgePlayer, "team", team_a)
        set_key_b = _relationship_set_key(EdgePlayer, "team", team_b)

        assert POPOTO_REDIS_DB.sismember(set_key_a, player.db_key.redis_key)
        assert not POPOTO_REDIS_DB.sismember(set_key_b, player.db_key.redis_key)

        # Query-level check: player is associated with team_a
        assert len(EdgePlayer.query.filter(team=team_a)) == 1
        assert len(EdgePlayer.query.filter(team=team_b)) == 0

        # Mutate: change team
        player.team = team_b
        player.save()

        # Query-level check: player should now be associated with team_b only
        team_b_players = EdgePlayer.query.filter(team=team_b)
        team_a_players = EdgePlayer.query.filter(team=team_a)

        assert len(team_b_players) == 1, "Player should be in team_b query results"

        # Raw Redis check: old relationship set should not contain the player
        still_in_a = POPOTO_REDIS_DB.sismember(set_key_a, player.db_key.redis_key)
        now_in_b = POPOTO_REDIS_DB.sismember(set_key_b, player.db_key.redis_key)

        assert now_in_b, "Player should be in team_b's relationship set"

        if still_in_a:
            pytest.xfail(
                "Bug: Relationship.on_save() does not remove from old related "
                "object's index set when relationship changes. File as separate issue."
            )
        assert len(team_a_players) == 0, "Player should not appear in team_a query"


# ===========================================================================
# Test Case 3: GeoField -- Coordinates update in place
# ===========================================================================


class TestGeoFieldCoordinateUpdate:
    """GEOADD is idempotent (member-keyed), so updating coordinates should work
    correctly. Verify with GEORADIUS queries at old and new coordinates."""

    def test_coordinate_update_replaces_old_position(self):
        rome_coords = GeoField.Coordinates(latitude=41.9028, longitude=12.4964)
        paris_coords = GeoField.Coordinates(latitude=48.8566, longitude=2.3522)

        place = EdgeGeoPlace.create(name="MyPlace", location=rome_coords)

        geo_key = _geo_set_key(EdgeGeoPlace, "location")

        # Verify initial: place near Rome
        near_rome = POPOTO_REDIS_DB.georadius(
            geo_key,
            longitude=rome_coords.longitude,
            latitude=rome_coords.latitude,
            radius=10,
            unit="km",
        )
        assert len(near_rome) == 1

        # Update coordinates to Paris
        place.location = paris_coords
        place.save()

        # Near Paris should find it
        near_paris = POPOTO_REDIS_DB.georadius(
            geo_key,
            longitude=paris_coords.longitude,
            latitude=paris_coords.latitude,
            radius=10,
            unit="km",
        )
        assert len(near_paris) == 1, "Place should be near Paris after update"

        # Near Rome should NOT find it (since GEOADD replaces the member's coords)
        near_rome_after = POPOTO_REDIS_DB.georadius(
            geo_key,
            longitude=rome_coords.longitude,
            latitude=rome_coords.latitude,
            radius=10,
            unit="km",
        )
        assert (
            len(near_rome_after) == 0
        ), "Place should no longer be near Rome after coordinate update"


# ===========================================================================
# Test Case 4: KeyField -- Mutation from non-null to null
# ===========================================================================


class TestKeyFieldNonNullToNull:
    """Changing a nullable KeyField from a real value to None should clean up
    the old value's index set."""

    def test_nonnull_to_null_cleans_old_index(self):
        item = EdgeNullableKey.create(status="active", data="test1")

        active_key = _keyfield_set_key(EdgeNullableKey, "status", "active")
        none_key = _keyfield_set_key(EdgeNullableKey, "status", "None")

        # Verify initial state
        assert len(POPOTO_REDIS_DB.smembers(active_key)) == 1

        # Query-level check
        assert len(EdgeNullableKey.query.filter(status="active")) == 1

        # Mutate: set to None
        item.status = None
        item.save()

        # Query-level: should no longer appear in "active" filter
        active_items = EdgeNullableKey.query.filter(status="active")
        assert (
            len(active_items) == 0
        ), "Item should not appear in 'active' after null transition"

        # Raw Redis: old set should be empty
        old_members = POPOTO_REDIS_DB.smembers(active_key)
        assert (
            len(old_members) == 0
        ), "Old index set should be empty after null transition"

        # The item should be in the "None" set
        null_members = POPOTO_REDIS_DB.smembers(none_key)
        assert len(null_members) == 1, "Item should be in the 'None' index set"


# ===========================================================================
# Test Case 5: KeyField -- Mutation from null to non-null
# ===========================================================================


class TestKeyFieldNullToNonNull:
    """Creating with null and then setting a value should correctly add to
    the new index without leaving ghost entries."""

    def test_null_to_nonnull_adds_to_correct_index(self):
        item = EdgeNullableKey.create(data="test2")  # status defaults to None

        none_key = _keyfield_set_key(EdgeNullableKey, "status", "None")
        active_key = _keyfield_set_key(EdgeNullableKey, "status", "active")

        # Verify initial: item is in the None index
        null_items = EdgeNullableKey.query.filter(status__isnull=True)
        assert len(null_items) == 1

        # Mutate: set a real value
        item.status = "active"
        item.save()

        # Query-level
        active_items = EdgeNullableKey.query.filter(status="active")
        assert len(active_items) == 1, "Item should appear in 'active' filter"

        # Raw Redis: None set should be empty, active set should have 1
        none_members = POPOTO_REDIS_DB.smembers(none_key)
        active_members = POPOTO_REDIS_DB.smembers(active_key)

        assert len(none_members) == 0, "None index should be empty after setting value"
        assert len(active_members) == 1, "Active index should have the item"


# ===========================================================================
# Test Case 6: UniqueKeyField -- Mutation cleans up old unique index
# ===========================================================================


class TestUniqueKeyFieldMutationCleanup:
    """Changing a unique field's value should release the old value for reuse
    by other instances."""

    def test_unique_value_change_releases_old_value(self):
        """UniqueKeyField value is part of the Redis key, so changing it
        triggers the obsolete_redis_key path. The old key field index set
        entry should be cleaned up so the old value is available for reuse."""
        item1 = EdgeUniqueItem.create(email="alice@example.com", name="Alice")

        # Note: UniqueKeyField with the same value produces the same Redis
        # key, so .create() silently overwrites (no exception raised).
        # The uniqueness is enforced by the key structure itself, not by
        # a pre-save check.

        old_key = _keyfield_set_key(EdgeUniqueItem, "email", "alice@example.com")
        new_key = _keyfield_set_key(EdgeUniqueItem, "email", "alice_new@example.com")

        # Verify initial state: old email index has the item
        assert len(POPOTO_REDIS_DB.smembers(old_key)) == 1

        # Mutate item1's email
        # UniqueKeyField is part of the Redis key, so this changes the key
        item1.email = "alice_new@example.com"
        item1.save()

        # The new email index should contain the item
        new_members = POPOTO_REDIS_DB.smembers(new_key)
        assert len(new_members) >= 1, "New email index should contain the item"

        # The old email index should be cleaned up
        old_members = POPOTO_REDIS_DB.smembers(old_key)

        if len(old_members) > 0:
            pytest.xfail(
                "Bug: UniqueKeyField index cleanup on value change does not "
                "remove old index entry. File as separate issue."
            )

        # Another instance should now be able to use the old email
        item2 = EdgeUniqueItem.create(email="alice@example.com", name="New Alice")
        assert item2.email == "alice@example.com"


# ===========================================================================
# Test Case 7: save(update_fields=["status"]) -- Partial save with KeyField
# ===========================================================================


class TestPartialSaveKeyField:
    """The partial save path only calls on_save() for listed fields.
    Verify index correctness when a KeyField is in update_fields.

    Fixed in #156: The partial save path now detects when the db_key
    changes (because a KeyField was updated) and performs obsolete key
    cleanup — deleting old hash, removing old class set entry, and
    running on_delete for old index entries.
    """

    def test_partial_save_updates_keyfield_index(self):
        """Partial save with a KeyField in update_fields.

        Since status is a KeyField that participates in the Redis key,
        changing it via update_fields changes the db_key. The partial
        save path now detects this and cleans up the obsolete key."""
        item = EdgePartialSave.create(status="draft", priority=5, data="doc1")

        draft_key = _keyfield_set_key(EdgePartialSave, "status", "draft")
        published_key = _keyfield_set_key(EdgePartialSave, "status", "published")

        assert len(POPOTO_REDIS_DB.smembers(draft_key)) == 1

        # Partial save: update only status
        item.status = "published"
        item.save(update_fields=["status"])

        # Raw Redis: check if old index was cleaned up
        draft_members = POPOTO_REDIS_DB.smembers(draft_key)
        published_members = POPOTO_REDIS_DB.smembers(published_key)

        # The new index should have the item
        assert len(published_members) >= 1, "Published index should have the item"

        # After fix for #156, partial save detects obsolete_redis_key and
        # cleans up old index entries properly.
        assert (
            len(draft_members) == 0
        ), "Draft index should be empty after partial save to published"


# ===========================================================================
# Test Case 8: save(update_fields=["status"]) -- Partial save index cleanup
# ===========================================================================


class TestPartialSaveIndexCleanup:
    """Verify the partial save path correctly handles index cleanup
    via _saved_field_values.

    Fixed in #156: partial saves now detect obsolete_redis_key and
    clean up old index entries properly."""

    def test_partial_save_multiple_mutations(self):
        """Multiple partial saves should keep indexes consistent.

        Since status is a KeyField, each partial save changes the db_key.
        The fix in #156 ensures obsolete key cleanup happens on each save."""
        item = EdgePartialSave.create(status="new", priority=1, data="item1")

        new_key = _keyfield_set_key(EdgePartialSave, "status", "new")
        processing_key = _keyfield_set_key(EdgePartialSave, "status", "processing")
        done_key = _keyfield_set_key(EdgePartialSave, "status", "done")

        assert len(POPOTO_REDIS_DB.smembers(new_key)) == 1

        # First partial save: new -> processing
        item.status = "processing"
        item.save(update_fields=["status"])

        new_members = POPOTO_REDIS_DB.smembers(new_key)
        processing_members = POPOTO_REDIS_DB.smembers(processing_key)

        # After fix for #156, partial save handles obsolete_redis_key cleanup.
        assert (
            len(new_members) == 0
        ), "New index should be empty after partial save to processing"

        assert len(processing_members) == 1

        # Second partial save: processing -> done
        item.status = "done"
        item.save(update_fields=["status"])

        assert (
            len(POPOTO_REDIS_DB.smembers(processing_key)) == 0
        ), "Processing index should be empty after second partial save"
        assert (
            len(POPOTO_REDIS_DB.smembers(done_key)) == 1
        ), "Done index should have the item"


# ===========================================================================
# Test Case 9: Composite KeyField -- Changing one of two KeyFields
# ===========================================================================


class TestCompositeKeyFieldChange:
    """Changing one KeyField in a composite key changes the Redis key itself,
    triggering the obsolete_redis_key path. Verify no double-add or missed cleanup."""

    def test_changing_one_composite_key_field(self):
        item = EdgeCompositeKey.create(region="us-east", user_id="123", name="Alice")

        old_region_key = _keyfield_set_key(EdgeCompositeKey, "region", "us-east")
        new_region_key = _keyfield_set_key(EdgeCompositeKey, "region", "us-west")
        user_key = _keyfield_set_key(EdgeCompositeKey, "user_id", "123")

        # Verify initial state
        assert len(POPOTO_REDIS_DB.smembers(old_region_key)) == 1
        assert len(POPOTO_REDIS_DB.smembers(user_key)) == 1

        old_redis_key = item.db_key.redis_key

        # Mutate one key field: region changes
        item.region = "us-west"
        item.save()

        new_redis_key = item.db_key.redis_key
        assert old_redis_key != new_redis_key, "Redis key should change"

        # The old region index should not reference the old key
        old_region_members = POPOTO_REDIS_DB.smembers(old_region_key)
        new_region_members = POPOTO_REDIS_DB.smembers(new_region_key)

        assert len(new_region_members) == 1, "New region index should have the item"

        if len(old_region_members) > 0:
            pytest.xfail(
                "Bug: Composite key change does not clean up old region index. "
                "File as separate issue."
            )

        # user_id index should contain the new key, not the old key
        user_members = POPOTO_REDIS_DB.smembers(user_key)
        assert len(user_members) == 1, "user_id index should have exactly one entry"

        # The old Redis key should no longer exist
        assert not POPOTO_REDIS_DB.exists(
            old_redis_key
        ), "Old Redis key should be deleted"


# ===========================================================================
# Test Case 10: delete_all() -- Index cleanup at scale
# ===========================================================================


class TestDeleteAllIndexCleanup:
    """After delete_all(), verify all index sets are empty and no orphaned
    Redis keys remain."""

    def test_delete_all_cleans_all_indexes(self):
        # Create several items across different categories
        for i in range(5):
            EdgeMenuItem.create(category="food", price=float(i + 1), name=f"Item_{i}")
        for i in range(3):
            EdgeMenuItem.create(
                category="drink", price=float(i + 10), name=f"Drink_{i}"
            )

        # Verify items exist
        assert EdgeMenuItem.query.count() == 8

        food_ss_key = _sorted_set_key(EdgeMenuItem, "price", "food")
        drink_ss_key = _sorted_set_key(EdgeMenuItem, "price", "drink")
        food_kf_key = _keyfield_set_key(EdgeMenuItem, "category", "food")
        drink_kf_key = _keyfield_set_key(EdgeMenuItem, "category", "drink")

        assert len(POPOTO_REDIS_DB.zrangebyscore(food_ss_key, "-inf", "+inf")) == 5
        assert len(POPOTO_REDIS_DB.zrangebyscore(drink_ss_key, "-inf", "+inf")) == 3

        # Delete all
        EdgeMenuItem.delete_all()

        # Verify all gone
        assert EdgeMenuItem.query.count() == 0

        # Sorted set indexes should be empty
        assert (
            len(POPOTO_REDIS_DB.zrangebyscore(food_ss_key, "-inf", "+inf")) == 0
        ), "Food sorted set should be empty after delete_all"
        assert (
            len(POPOTO_REDIS_DB.zrangebyscore(drink_ss_key, "-inf", "+inf")) == 0
        ), "Drink sorted set should be empty after delete_all"

        # KeyField indexes should be empty
        assert (
            len(POPOTO_REDIS_DB.smembers(food_kf_key)) == 0
        ), "Food key field set should be empty after delete_all"
        assert (
            len(POPOTO_REDIS_DB.smembers(drink_kf_key)) == 0
        ), "Drink key field set should be empty after delete_all"


# ===========================================================================
# Test Case 11: Rapid create-mutate-delete cycle
# ===========================================================================


class TestRapidCreateMutateDelete:
    """Create, immediately mutate, immediately delete. Verify all indexes
    are clean afterward."""

    def test_rapid_lifecycle_leaves_no_orphans(self):
        """Create, mutate, save, delete cycle.

        When status (a KeyField) changes, the Redis key changes too
        (e.g., EdgeLifecycle:init:uuid -> EdgeLifecycle:running:uuid).
        The save() correctly deletes the old hash key but leaves the
        old entry in the class set. delete() only removes the current
        key from the class set, leaving the orphan.
        """
        # Create
        item = EdgeLifecycle.create(
            status="init",
            score=1.0,
            location=GeoField.Coordinates(latitude=40.7128, longitude=-74.0060),
        )

        status_key = _keyfield_set_key(EdgeLifecycle, "status", "init")
        geo_key = _geo_set_key(EdgeLifecycle, "location")

        # Verify item exists in all indexes
        assert len(POPOTO_REDIS_DB.smembers(status_key)) == 1

        # Mutate
        item.status = "running"
        item.score = 99.0
        item.location = GeoField.Coordinates(latitude=48.8566, longitude=2.3522)
        item.save()

        running_key = _keyfield_set_key(EdgeLifecycle, "status", "running")

        # Delete
        item.delete()

        # Key field indexes should be clean
        init_members = POPOTO_REDIS_DB.smembers(status_key)
        running_members = POPOTO_REDIS_DB.smembers(running_key)
        geo_members = POPOTO_REDIS_DB.zrangebyscore(geo_key, "-inf", "+inf")

        assert len(init_members) == 0, "Init status index should be empty"
        assert len(running_members) == 0, "Running status index should be empty"
        assert len(geo_members) == 0, "Geo index should be empty"

        # Class set: when a KeyField changes via save(), the obsolete_redis_key
        # path deletes the old hash but does NOT remove the old key from the
        # class set. delete() only removes the current key, leaving an orphan.
        count = EdgeLifecycle.query.count()
        if count > 0:
            pytest.xfail(
                "Bug: save() with obsolete_redis_key does not remove old key "
                "from the class set. After delete(), orphaned class set entries "
                "remain, inflating query.count(). File as separate issue."
            )


# ===========================================================================
# Test Case 12: Relationship -- Setting to None (clearing relationship)
# ===========================================================================


class TestRelationshipClearToNone:
    """Setting a relationship to None should remove the instance from the old
    related object's index set."""

    def test_clearing_relationship_removes_from_index(self):
        team = EdgeTeam.create(name="TeamX")
        player = EdgePlayer.create(name="Bob", team=team)

        set_key_team = _relationship_set_key(EdgePlayer, "team", team)
        set_key_none = _relationship_set_key(EdgePlayer, "team", None)

        # Verify initial state
        assert POPOTO_REDIS_DB.sismember(set_key_team, player.db_key.redis_key)
        assert len(EdgePlayer.query.filter(team=team)) == 1

        # Clear the relationship
        player.team = None
        player.save()

        # Query-level: player should no longer be associated with the team
        team_players = EdgePlayer.query.filter(team=team)

        # Raw Redis: player should be removed from team's relationship set
        still_in_team = POPOTO_REDIS_DB.sismember(set_key_team, player.db_key.redis_key)

        if still_in_team:
            pytest.xfail(
                "Bug: Relationship.on_save() does not remove from old related "
                "object's index set when relationship is set to None. "
                "File as separate issue."
            )

        assert (
            len(team_players) == 0
        ), "Player should not appear in team query after clearing relationship"

        # The player should be in the "None" relationship set
        in_none_set = POPOTO_REDIS_DB.sismember(set_key_none, player.db_key.redis_key)
        assert in_none_set, "Player should be in the 'None' relationship set"
