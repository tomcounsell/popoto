"""Tests for migration-related save flags (skip_auto_now)."""

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
