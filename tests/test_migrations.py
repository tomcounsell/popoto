"""Tests for migration-related save flags (skip_auto_now, update_fields)."""

import time

import pytest

from popoto import Model, KeyField, Field, SortedField
from popoto.redis_db import POPOTO_REDIS_DB


class MigrationModel(Model):
    """Model for testing migration save flags."""

    key = KeyField(type=str)
    name = Field(type=str, null=True)
    score = SortedField(type=float, null=False, default=0.0)
    created_at = SortedField(type=float, auto_now_add=True)
    updated_at = SortedField(type=float, auto_now=True)


class TestSkipAutoNow:
    """Test save(skip_auto_now=True) behavior."""

    def setup_method(self):
        MigrationModel.delete_all()

    def teardown_method(self):
        MigrationModel.delete_all()

    def test_skip_auto_now_preserves_timestamp(self):
        """save(skip_auto_now=True) should preserve existing auto_now timestamp."""
        instance = MigrationModel.create(key="test1", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "migrated"
        instance.save(skip_auto_now=True)

        assert instance.updated_at == original_updated

    def test_default_save_still_updates_timestamp(self):
        """save() without skip_auto_now should still update auto_now."""
        instance = MigrationModel.create(key="test2", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "updated"
        instance.save()

        assert instance.updated_at > original_updated

    def test_skip_auto_now_does_not_affect_auto_now_add(self):
        """skip_auto_now should not affect auto_now_add behavior."""
        instance = MigrationModel.create(key="test3")
        original_created = instance.created_at

        time.sleep(0.01)

        instance.name = "updated"
        instance.save(skip_auto_now=True)

        # created_at (auto_now_add) shouldn't change regardless
        assert instance.created_at == original_created

    def test_skip_auto_now_with_pipeline(self):
        """skip_auto_now should work with pipeline saves."""
        instance = MigrationModel.create(key="test4", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        pipeline = POPOTO_REDIS_DB.pipeline()
        instance.name = "migrated"
        instance.save(pipeline=pipeline, skip_auto_now=True)
        pipeline.execute()

        assert instance.updated_at == original_updated

    @pytest.mark.asyncio
    async def test_skip_auto_now_async(self):
        """async_save should pass through skip_auto_now."""
        instance = MigrationModel.create(key="test5", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "migrated"
        await instance.async_save(skip_auto_now=True)

        assert instance.updated_at == original_updated


class TestUpdateFields:
    """Test save(update_fields=[...]) behavior."""

    def setup_method(self):
        MigrationModel.delete_all()

    def teardown_method(self):
        MigrationModel.delete_all()

    def test_partial_save_only_updates_listed_fields(self):
        """save(update_fields=['name']) should only write name to Redis."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)

        instance.name = "updated"
        instance.score = 999.0  # Change this but don't include in update_fields
        instance.save(update_fields=["name"])

        # Reload from Redis
        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "updated"
        assert reloaded.score == 1.0  # Should NOT have changed in Redis

    def test_partial_save_only_triggers_on_save_for_listed(self):
        """save(update_fields=['score']) should only update score's sorted set."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)
        original_updated = instance.updated_at

        time.sleep(0.01)

        # Only update score, not updated_at
        instance.score = 5.0
        instance.save(update_fields=["score"])

        # Reload and verify
        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.score == 5.0
        # updated_at should NOT have changed since it's not in update_fields
        assert reloaded.updated_at == original_updated

    def test_empty_update_fields_is_noop(self):
        """save(update_fields=[]) should be a no-op."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)

        time.sleep(0.01)

        instance.name = "should_not_save"
        instance.score = 999.0
        instance.save(update_fields=[])

        # Reload and verify nothing changed
        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "original"
        assert reloaded.score == 1.0

    def test_auto_now_not_updated_when_not_in_update_fields(self):
        """auto_now field should NOT update when not in update_fields list."""
        instance = MigrationModel.create(key="test1", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "changed"
        instance.save(update_fields=["name"])

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.updated_at == original_updated

    def test_auto_now_updated_when_explicitly_in_update_fields(self):
        """auto_now field SHOULD update when explicitly listed in update_fields."""
        instance = MigrationModel.create(key="test1", name="original")
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "changed"
        instance.save(update_fields=["name", "updated_at"])

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.updated_at > original_updated

    def test_default_behavior_unchanged(self):
        """save() with no update_fields should behave exactly as before."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "updated"
        instance.score = 5.0
        instance.save()

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "updated"
        assert reloaded.score == 5.0
        assert reloaded.updated_at > original_updated  # auto_now fires normally

    def test_update_fields_with_pipeline(self):
        """update_fields should work with pipeline saves."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)

        pipeline = POPOTO_REDIS_DB.pipeline()
        instance.name = "pipelined"
        instance.save(pipeline=pipeline, update_fields=["name"])
        pipeline.execute()

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "pipelined"
        assert reloaded.score == 1.0

    @pytest.mark.asyncio
    async def test_update_fields_async(self):
        """async_save should pass through update_fields."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)

        instance.name = "async_updated"
        await instance.async_save(update_fields=["name"])

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "async_updated"
        assert reloaded.score == 1.0

    def test_update_fields_with_sorted_field_updates_index(self):
        """Partial save with a sorted field should update its sorted set index."""
        instance = MigrationModel.create(key="test1", score=1.0)

        instance.score = 50.0
        instance.save(update_fields=["score"])

        # Query by the new score range should find it
        results = list(MigrationModel.query.filter(score__gte=40.0, score__lte=60.0))
        assert len(results) == 1
        assert results[0].key == "test1"

        # Old score range should NOT find it
        old_results = list(MigrationModel.query.filter(score__gte=0.5, score__lte=1.5))
        assert len(old_results) == 0

    def test_update_fields_unknown_field_raises(self):
        """update_fields with unknown field name should raise ModelException."""
        from popoto.exceptions import ModelException

        instance = MigrationModel.create(key="test1", name="original")

        with pytest.raises(ModelException, match="Unknown field"):
            instance.save(update_fields=["nonexistent_field"])


class TestRebuildIndexes:
    """Test Model.rebuild_indexes() behavior."""

    def setup_method(self):
        MigrationModel.delete_all()

    def teardown_method(self):
        MigrationModel.delete_all()

    def test_rebuild_sorted_field_indexes(self):
        """rebuild_indexes() should reconstruct sorted set indexes."""
        # Create instances with sorted fields
        MigrationModel.create(key="a", score=1.0)
        MigrationModel.create(key="b", score=2.0)
        MigrationModel.create(key="c", score=3.0)

        # Manually delete the sorted set index using the field's own key API
        score_field = MigrationModel._meta.fields["score"]
        sorted_key = score_field.get_special_use_field_db_key(MigrationModel, "score")
        POPOTO_REDIS_DB.delete(sorted_key.redis_key)

        # Verify queries are broken
        results = list(MigrationModel.query.filter(score__gte=1.0, score__lte=3.0))
        assert len(results) == 0  # Index is gone

        # Rebuild indexes
        count = MigrationModel.rebuild_indexes()
        assert count == 3

        # Verify queries work again
        results = list(MigrationModel.query.filter(score__gte=1.0, score__lte=3.0))
        assert len(results) == 3

    def test_rebuild_class_set(self):
        """rebuild_indexes() should reconstruct the class set."""
        MigrationModel.create(key="a", score=1.0)
        MigrationModel.create(key="b", score=2.0)

        # Delete the class set
        POPOTO_REDIS_DB.delete(MigrationModel._meta.db_class_set_key.redis_key)

        # Verify .all() is broken
        assert len(list(MigrationModel.query.all())) == 0

        # Rebuild
        count = MigrationModel.rebuild_indexes()
        assert count == 2

        # Verify .all() works again
        assert len(list(MigrationModel.query.all())) == 2

    def test_rebuild_with_batch_size(self):
        """rebuild_indexes() should respect batch_size parameter."""
        for i in range(5):
            MigrationModel.create(key=f"item_{i}", score=float(i))

        # Delete all sorted indexes
        score_field = MigrationModel._meta.fields["score"]
        sorted_key = score_field.get_special_use_field_db_key(MigrationModel, "score")
        POPOTO_REDIS_DB.delete(sorted_key.redis_key)

        # Rebuild with small batch size
        count = MigrationModel.rebuild_indexes(batch_size=2)
        assert count == 5

        # Verify all indexes restored
        results = list(MigrationModel.query.filter(score__gte=0.0, score__lte=10.0))
        assert len(results) == 5

    def test_rebuild_returns_zero_for_empty_model(self):
        """rebuild_indexes() on model with no instances should return 0."""
        count = MigrationModel.rebuild_indexes()
        assert count == 0

    @pytest.mark.asyncio
    async def test_async_rebuild_indexes(self):
        """async_rebuild_indexes() should work via to_thread."""
        MigrationModel.create(key="a", score=1.0)
        MigrationModel.create(key="b", score=2.0)

        score_field = MigrationModel._meta.fields["score"]
        sorted_key = score_field.get_special_use_field_db_key(MigrationModel, "score")
        POPOTO_REDIS_DB.delete(sorted_key.redis_key)

        count = await MigrationModel.async_rebuild_indexes()
        assert count == 2

        results = list(MigrationModel.query.filter(score__gte=0.0, score__lte=3.0))
        assert len(results) == 2


class TestRawUpdate:
    """Test Model.raw_update() behavior."""

    def setup_method(self):
        MigrationModel.delete_all()

    def teardown_method(self):
        MigrationModel.delete_all()

    def test_raw_update_changes_field_values(self):
        """raw_update should change field values readable by normal get()."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)
        redis_key = instance.db_key.redis_key

        MigrationModel.raw_update([redis_key], name="updated_via_raw")

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.name == "updated_via_raw"

    def test_raw_update_does_not_trigger_hooks(self):
        """raw_update should NOT update sorted set indexes."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)
        redis_key = instance.db_key.redis_key

        # raw_update changes score but doesn't update sorted set
        MigrationModel.raw_update([redis_key], score=99.0)

        # Normal load should show new value
        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.score == 99.0

        # But sorted set still has old score - query by old range finds it
        results = list(MigrationModel.query.filter(score__gte=0.5, score__lte=1.5))
        assert len(results) == 1  # Still indexed at old score

        # Query by new range does NOT find it (stale index)
        results = list(MigrationModel.query.filter(score__gte=98.0, score__lte=100.0))
        assert len(results) == 0

    def test_raw_update_plus_rebuild_restores_queries(self):
        """raw_update + rebuild_indexes should restore full query functionality."""
        instance = MigrationModel.create(key="test1", name="original", score=1.0)
        redis_key = instance.db_key.redis_key

        MigrationModel.raw_update([redis_key], score=99.0)
        MigrationModel.rebuild_indexes()

        # Now query by new range should work
        results = list(MigrationModel.query.filter(score__gte=98.0, score__lte=100.0))
        assert len(results) == 1
        assert results[0].key == "test1"

    def test_raw_update_multiple_keys(self):
        """raw_update should work with multiple redis keys."""
        inst1 = MigrationModel.create(key="a", name="name_a", score=1.0)
        inst2 = MigrationModel.create(key="b", name="name_b", score=2.0)
        inst3 = MigrationModel.create(key="c", name="name_c", score=3.0)

        keys = [
            inst1.db_key.redis_key,
            inst2.db_key.redis_key,
            inst3.db_key.redis_key,
        ]
        count = MigrationModel.raw_update(keys, name="bulk_updated")
        assert count == 3

        for k in ["a", "b", "c"]:
            assert MigrationModel.query.get(key=k).name == "bulk_updated"

    def test_raw_update_with_batch_size(self):
        """raw_update should work with batch_size parameter."""
        instances = [
            MigrationModel.create(key=f"item_{i}", score=float(i)) for i in range(5)
        ]
        keys = [inst.db_key.redis_key for inst in instances]

        count = MigrationModel.raw_update(keys, name="batched", batch_size=2)
        assert count == 5

        for i in range(5):
            assert MigrationModel.query.get(key=f"item_{i}").name == "batched"

    def test_raw_update_does_not_update_auto_now(self):
        """raw_update should NOT trigger auto_now fields."""
        instance = MigrationModel.create(key="test1", name="original")
        original_updated = instance.updated_at
        redis_key = instance.db_key.redis_key

        time.sleep(0.01)

        MigrationModel.raw_update([redis_key], name="raw_changed")

        reloaded = MigrationModel.query.get(key="test1")
        assert reloaded.updated_at == original_updated  # NOT updated
        assert reloaded.name == "raw_changed"

    def test_raw_update_returns_zero_for_empty_list(self):
        """raw_update with empty key list should return 0."""
        count = MigrationModel.raw_update([], name="nothing")
        assert count == 0
