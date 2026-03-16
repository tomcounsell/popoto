"""
Tests for atomic save() behavior.

Verifies that save() without an explicit pipeline uses an internal pipeline
to execute all Redis commands atomically, preventing race conditions where
records exist but indexes haven't been updated yet.

Related: https://github.com/tomcounsell/popoto/issues/147
"""

import pytest

import popoto
from popoto.redis_db import POPOTO_REDIS_DB


class AtomicModel(popoto.Model):
    """Model with KeyField for testing atomic index creation."""

    name = popoto.KeyField()
    status = popoto.KeyField(default="active")
    value = popoto.Field(type=str, default="")


class SortedModel(popoto.Model):
    """Model with SortedField for testing atomic index creation."""

    item_id = popoto.KeyField()
    score = popoto.SortedField(type=float)
    label = popoto.Field(type=str, default="")


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data after each test."""
    yield
    for key in POPOTO_REDIS_DB.keys("AtomicModel:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.keys("SortedModel:*"):
        POPOTO_REDIS_DB.delete(key)
    POPOTO_REDIS_DB.delete("$Class:AtomicModel")
    POPOTO_REDIS_DB.delete("$Class:SortedModel")
    # Clean up KeyField index sets
    for key in POPOTO_REDIS_DB.keys("$KeyF:AtomicModel:*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.keys("$KeyF:SortedModel:*"):
        POPOTO_REDIS_DB.delete(key)


class TestAtomicSave:
    """Tests verifying save() executes atomically via internal pipeline."""

    def test_save_indexes_exist_immediately(self):
        """After save(), KeyField index must contain the record."""
        obj = AtomicModel(name="test1", status="pending", value="data")
        obj.save()

        # KeyField index should contain this record immediately
        index_key = "$KeyF:AtomicModel:status:pending"
        members = POPOTO_REDIS_DB.smembers(index_key)
        member_strs = {
            m.decode("utf-8") if isinstance(m, bytes) else m for m in members
        }
        assert obj.db_key.redis_key in member_strs

    def test_save_class_set_exists_immediately(self):
        """After save(), class set must contain the record."""
        obj = AtomicModel(name="test2", status="active", value="data")
        obj.save()

        class_set_key = "$Class:AtomicModel"
        members = POPOTO_REDIS_DB.smembers(class_set_key)
        member_strs = {
            m.decode("utf-8") if isinstance(m, bytes) else m for m in members
        }
        assert obj.db_key.redis_key in member_strs

    def test_filter_finds_record_immediately_after_create(self):
        """create() + query.filter() must find the record (no race window)."""
        obj = AtomicModel.create(name="findme", status="pending", value="important")

        # This is the exact pattern that failed before atomic save
        results = AtomicModel.query.filter(status="pending")
        found_keys = [r.db_key.redis_key for r in results]
        assert obj.db_key.redis_key in found_keys

    @pytest.mark.asyncio
    async def test_async_create_then_async_filter(self):
        """async_create() + async_filter() must find the record."""
        obj = await AtomicModel.async_create(
            name="async_test", status="pending", value="async_data"
        )

        results = await AtomicModel.query.async_filter(status="pending")
        found_keys = [r.db_key.redis_key for r in results]
        assert obj.db_key.redis_key in found_keys

    def test_save_return_value_is_int(self):
        """save() return value must still be an int (backward compat)."""
        obj = AtomicModel(name="retval", status="active", value="test")
        result = obj.save()
        assert isinstance(result, int)

    def test_save_with_sorted_field_atomic(self):
        """SortedField index must exist immediately after save()."""
        obj = SortedModel(item_id="s1", score=42.5, label="test")
        obj.save()

        # Sorted set index should contain this record
        sorted_key = "$SortF:SortedModel:score"
        members = POPOTO_REDIS_DB.zrange(sorted_key, 0, -1)
        member_strs = {
            m.decode("utf-8") if isinstance(m, bytes) else m for m in members
        }
        assert obj.db_key.redis_key in member_strs

    def test_partial_save_atomic(self):
        """update_fields save path must also be atomic."""
        obj = AtomicModel.create(name="partial", status="active", value="original")

        # Update just the value field
        obj.value = "updated"
        result = obj.save(update_fields=["value"])

        # Should still return int
        assert isinstance(result, int)

        # Reload and verify
        reloaded = AtomicModel.query.get(name="partial")
        assert reloaded.value == "updated"

    def test_save_with_explicit_pipeline_unchanged(self):
        """Explicit pipeline path must still work as before."""
        pipeline = POPOTO_REDIS_DB.pipeline()

        obj = AtomicModel(name="piped", status="active", value="piped_data")
        returned_pipeline = obj.save(pipeline=pipeline)

        # Should return the pipeline, not execute yet
        assert returned_pipeline is not None

        # Now execute
        pipeline.execute()

        # Verify record exists
        reloaded = AtomicModel.query.get(name="piped")
        assert reloaded is not None
        assert reloaded.value == "piped_data"

    def test_key_migration_atomic(self):
        """Changing a KeyField value must atomically clean old + create new."""
        obj = AtomicModel.create(name="migrate_me", status="old_status", value="data")
        old_key = obj.db_key.redis_key

        # Change the status (a KeyField), which changes the redis key
        obj.status = "new_status"
        obj.save()

        new_key = obj.db_key.redis_key
        assert old_key != new_key

        # Old key should not exist
        assert not POPOTO_REDIS_DB.exists(old_key)

        # New key should exist with correct data
        reloaded = AtomicModel.query.get(name="migrate_me")
        assert reloaded is not None
        assert reloaded.status == "new_status"
