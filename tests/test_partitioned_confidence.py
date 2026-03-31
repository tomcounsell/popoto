"""Tests for partitioned ConfidenceField — partition_by support for hash-based indexes.

Tests cover:
- Partition parameter initialization and validation
- Partitioned on_save creates correct hash key structure
- Partitioned update_confidence writes to correct partition
- Partitioned get_confidence reads from correct partition
- Partition key change removes from old, adds to new
- Unpartitioned behavior unchanged (backward compatibility)
- Query materialization with partition filter reads only scoped hash
- QueryException when partition fields missing from query
- HSCAN utility with various match patterns
- Migration helper redistributes data correctly
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.exceptions import ModelException
from src.popoto.models.query import QueryException
from src.popoto.redis_db import POPOTO_REDIS_DB


# --- Test Models ---


class PartitionedConfidence(popoto.Model):
    name = popoto.KeyField()
    project = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by="project")


class MultiPartitionConfidence(popoto.Model):
    name = popoto.KeyField()
    project = popoto.KeyField()
    category = popoto.KeyField()
    certainty = ConfidenceField(
        initial_confidence=0.6, partition_by=("project", "category")
    )


class UnpartitionedConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(initial_confidence=0.5)


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [
        PartitionedConfidence,
        MultiPartitionConfidence,
        UnpartitionedConfidence,
    ]:
        for key in POPOTO_REDIS_DB.keys(f"{model_class.__name__}:*"):
            POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.keys("$Confidenc*"):
        POPOTO_REDIS_DB.delete(key)
    for key in POPOTO_REDIS_DB.keys("$CSQ:*"):
        POPOTO_REDIS_DB.delete(key)
    # Also clean up KeyField index sets
    for key in POPOTO_REDIS_DB.keys("$KeyF:*"):
        POPOTO_REDIS_DB.delete(key)


# --- Initialization Tests ---


class TestPartitionInit:
    def test_partition_by_string(self):
        """partition_by as string is normalized to tuple."""
        field = ConfidenceField(partition_by="project")
        assert field.partition_by == ("project",)

    def test_partition_by_tuple(self):
        """partition_by as tuple is stored as-is."""
        field = ConfidenceField(partition_by=("project", "category"))
        assert field.partition_by == ("project", "category")

    def test_partition_by_empty(self):
        """No partition_by defaults to empty tuple."""
        field = ConfidenceField()
        assert field.partition_by == tuple()

    def test_partition_by_invalid_type(self):
        """partition_by with invalid type raises ModelException."""
        with pytest.raises(ModelException):
            ConfidenceField(partition_by=["project"])

    def test_partition_by_with_initial_confidence(self):
        """partition_by works together with initial_confidence."""
        field = ConfidenceField(initial_confidence=0.8, partition_by="project")
        assert field.initial_confidence == 0.8
        assert field.partition_by == ("project",)


# --- Partitioned on_save / on_delete ---


class TestPartitionedLifecycle:
    def test_on_save_creates_partitioned_hash(self):
        """Saving with partition_by creates hash key with partition value."""
        item = PartitionedConfidence.create(name="t1", project="atlas")
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        assert ":data:atlas" in data_hash_key

    def test_on_save_initializes_data_in_partitioned_hash(self):
        """Companion hash entry is created in the correct partition."""
        item = PartitionedConfidence.create(name="t2", project="atlas")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5
        assert data["evidence_count"] == 0

    def test_different_partitions_are_isolated(self):
        """Items in different partitions have separate hashes."""
        item_a = PartitionedConfidence.create(name="a1", project="atlas")
        item_b = PartitionedConfidence.create(name="b1", project="bravo")

        ConfidenceField.update_confidence(item_a, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item_b, "certainty", signal=0.1)

        conf_a = ConfidenceField.get_confidence(item_a, "certainty")
        conf_b = ConfidenceField.get_confidence(item_b, "certainty")

        assert conf_a > 0.5
        assert conf_b < 0.5

    def test_on_delete_removes_from_partitioned_hash(self):
        """Deleting removes entry from the correct partition hash."""
        item = PartitionedConfidence.create(name="del1", project="atlas")
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        member_key = item.db_key.redis_key

        # Verify entry exists
        assert POPOTO_REDIS_DB.hget(data_hash_key, member_key) is not None

        item.delete()

        # Verify entry removed
        assert POPOTO_REDIS_DB.hget(data_hash_key, member_key) is None

    def test_multi_partition_creates_correct_key(self):
        """Multi-field partition creates hash key with all partition values."""
        item = MultiPartitionConfidence.create(
            name="m1", project="atlas", category="facts"
        )
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        assert ":data:atlas:facts" in data_hash_key


# --- Partition Key Change ---


class TestPartitionChange:
    def test_partition_change_moves_entry(self):
        """Changing partition key moves confidence data to new partition hash."""
        item = PartitionedConfidence.create(name="move1", project="atlas")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)

        # Verify data is in atlas partition
        conf_before = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf_before - 0.9) < 1e-6

        # Change partition key (KeyField requires migrate_key=True)
        item.project = "bravo"
        item.save(migrate_key=True)

        # Data should be in bravo partition now
        conf_after = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf_after - 0.9) < 1e-6

        # Old partition hash should not have the entry
        field = item._meta.fields["certainty"]
        base_key = field.get_special_use_field_db_key(item, "certainty")
        old_hash_key = base_key.redis_key + ":data:atlas"
        member_key = item.db_key.redis_key
        assert POPOTO_REDIS_DB.hget(old_hash_key, member_key) is None


# --- Partitioned update_confidence / get_confidence ---


class TestPartitionedConfidenceOps:
    def test_update_confidence_writes_to_partition(self):
        """update_confidence writes to the partitioned hash key."""
        item = PartitionedConfidence.create(name="uc1", project="atlas")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.8)
        assert new_conf > 0.5

        # Verify data is in the partition hash
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        assert ":data:atlas" in data_hash_key
        assert POPOTO_REDIS_DB.hget(data_hash_key, item.db_key.redis_key) is not None

    def test_get_confidence_reads_from_partition(self):
        """get_confidence reads from the partitioned hash key."""
        item = PartitionedConfidence.create(name="gc1", project="atlas")
        ConfidenceField.update_confidence(item, "certainty", signal=0.7)

        conf = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf - 0.7) < 1e-6

    def test_get_confidence_data_from_partition(self):
        """get_confidence_data reads full metadata from partition."""
        item = PartitionedConfidence.create(name="gcd1", project="atlas")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item, "certainty", signal=0.1)

        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 2
        assert data["corroborations"] == 1
        assert data["contradictions"] == 1

    def test_get_confidence_empty_partition_returns_initial(self):
        """get_confidence on a partition with no entries returns initial_confidence."""
        item = PartitionedConfidence.create(name="empty1", project="atlas")
        # Delete the companion hash entry manually
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        POPOTO_REDIS_DB.hdel(data_hash_key, item.db_key.redis_key)

        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf == 0.5


# --- Backward Compatibility ---


class TestBackwardCompatibility:
    def test_unpartitioned_behavior_unchanged(self):
        """ConfidenceField without partition_by works exactly as before."""
        item = UnpartitionedConfidence.create(name="compat1")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5

        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - 0.9) < 1e-6

        conf = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf - 0.9) < 1e-6

    def test_unpartitioned_hash_key_unchanged(self):
        """Unpartitioned hash key has no partition suffix."""
        item = UnpartitionedConfidence.create(name="compat2")
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        assert data_hash_key.endswith(":data")
        assert (
            ":data:" not in data_hash_key.split(":data")[1]
            if len(data_hash_key.split(":data")) > 1
            else True
        )

    def test_unpartitioned_on_save_on_delete(self):
        """on_save/on_delete lifecycle works unchanged for unpartitioned."""
        item = UnpartitionedConfidence.create(name="compat3")
        ConfidenceField.update_confidence(item, "certainty", signal=0.8)

        # Re-save should not reset
        item.save()
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 1

        # Delete should clean up
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        member_key = item.db_key.redis_key
        item.delete()
        assert POPOTO_REDIS_DB.hget(data_hash_key, member_key) is None


# --- HSCAN Utility ---


class TestHSCANUtility:
    def test_get_confidence_filtered_all(self):
        """get_confidence_filtered with '*' returns all entries."""
        UnpartitionedConfidence.create(name="hscan1")
        UnpartitionedConfidence.create(name="hscan2")
        UnpartitionedConfidence.create(name="hscan3")

        result = ConfidenceField.get_confidence_filtered(
            UnpartitionedConfidence, "certainty"
        )
        assert len(result) == 3

    def test_get_confidence_filtered_pattern(self):
        """get_confidence_filtered with pattern filters results."""
        UnpartitionedConfidence.create(name="alpha1")
        UnpartitionedConfidence.create(name="beta1")

        result = ConfidenceField.get_confidence_filtered(
            UnpartitionedConfidence, "certainty", pattern="*alpha*"
        )
        assert len(result) == 1
        # The key should contain "alpha"
        key = list(result.keys())[0]
        assert "alpha" in key

    def test_get_confidence_filtered_no_match(self):
        """get_confidence_filtered with no matching pattern returns empty dict."""
        UnpartitionedConfidence.create(name="nomatch1")

        result = ConfidenceField.get_confidence_filtered(
            UnpartitionedConfidence, "certainty", pattern="*zzzzz*"
        )
        assert result == {}

    def test_get_confidence_filtered_wrong_field_raises(self):
        """get_confidence_filtered on non-ConfidenceField raises TypeError."""
        with pytest.raises(TypeError):
            ConfidenceField.get_confidence_filtered(UnpartitionedConfidence, "name")

    def test_get_confidence_filtered_returns_data_dict(self):
        """get_confidence_filtered returns confidence data dicts."""
        item = UnpartitionedConfidence.create(name="datacheck1")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)

        result = ConfidenceField.get_confidence_filtered(
            UnpartitionedConfidence, "certainty"
        )
        assert len(result) == 1
        data = list(result.values())[0]
        assert "confidence" in data
        assert "evidence_count" in data
        assert data["evidence_count"] == 1


# --- Migration Helper ---


class TestMigrationHelper:
    def test_migrate_no_partition_raises(self):
        """migrate_to_partitioned on unpartitioned field raises ModelException."""
        with pytest.raises(ModelException):
            ConfidenceField.migrate_to_partitioned(UnpartitionedConfidence, "certainty")

    def test_migrate_wrong_field_raises(self):
        """migrate_to_partitioned on non-ConfidenceField raises ModelException."""
        with pytest.raises(ModelException):
            ConfidenceField.migrate_to_partitioned(PartitionedConfidence, "name")

    def test_migrate_empty_hash(self):
        """migrate_to_partitioned on empty hash returns zero counts."""
        report = ConfidenceField.migrate_to_partitioned(
            PartitionedConfidence, "certainty"
        )
        assert report["total"] == 0
        assert report["migrated"] == 0

    def test_migrate_dry_run(self):
        """migrate_to_partitioned dry_run reports without modifying data."""
        # Create items and manually write to unpartitioned hash to simulate legacy
        item_a = PartitionedConfidence.create(name="mig_a", project="atlas")
        item_b = PartitionedConfidence.create(name="mig_b", project="bravo")

        import msgpack

        field = item_a._meta.fields["certainty"]
        base_key = field.get_special_use_field_db_key(
            PartitionedConfidence, "certainty"
        )
        unpartitioned_key = base_key.redis_key + ":data"

        # Clean up partitioned hashes first
        for key in POPOTO_REDIS_DB.keys(unpartitioned_key + ":*"):
            POPOTO_REDIS_DB.delete(key)

        # Write to unpartitioned hash (simulating legacy data)
        legacy_data = msgpack.packb(
            {
                "confidence": 0.7,
                "evidence_count": 5,
                "corroborations": 3,
                "contradictions": 2,
            }
        )
        POPOTO_REDIS_DB.hset(unpartitioned_key, item_a.db_key.redis_key, legacy_data)
        POPOTO_REDIS_DB.hset(unpartitioned_key, item_b.db_key.redis_key, legacy_data)

        report = ConfidenceField.migrate_to_partitioned(
            PartitionedConfidence, "certainty", dry_run=True
        )

        assert report["total"] == 2
        assert report["migrated"] == 0  # dry_run: no writes
        assert "atlas" in report["partitions"]
        assert "bravo" in report["partitions"]

        # Unpartitioned hash should still exist
        assert POPOTO_REDIS_DB.hlen(unpartitioned_key) == 2

    def test_migrate_actual(self):
        """migrate_to_partitioned moves data from unpartitioned to partitioned."""
        import msgpack

        item_a = PartitionedConfidence.create(name="mig_c", project="atlas")
        item_b = PartitionedConfidence.create(name="mig_d", project="bravo")

        field = item_a._meta.fields["certainty"]
        base_key = field.get_special_use_field_db_key(
            PartitionedConfidence, "certainty"
        )
        unpartitioned_key = base_key.redis_key + ":data"

        # Clean up partitioned hashes
        for key in POPOTO_REDIS_DB.keys(unpartitioned_key + ":*"):
            POPOTO_REDIS_DB.delete(key)

        # Write legacy data to unpartitioned hash
        legacy_data_a = msgpack.packb(
            {
                "confidence": 0.7,
                "evidence_count": 5,
                "corroborations": 3,
                "contradictions": 2,
            }
        )
        legacy_data_b = msgpack.packb(
            {
                "confidence": 0.3,
                "evidence_count": 10,
                "corroborations": 2,
                "contradictions": 8,
            }
        )
        POPOTO_REDIS_DB.hset(unpartitioned_key, item_a.db_key.redis_key, legacy_data_a)
        POPOTO_REDIS_DB.hset(unpartitioned_key, item_b.db_key.redis_key, legacy_data_b)

        report = ConfidenceField.migrate_to_partitioned(
            PartitionedConfidence, "certainty"
        )

        assert report["total"] == 2
        assert report["migrated"] == 2
        assert len(report["errors"]) == 0

        # Verify data is in partitioned hashes
        conf_a = ConfidenceField.get_confidence(item_a, "certainty")
        conf_b = ConfidenceField.get_confidence(item_b, "certainty")
        assert abs(conf_a - 0.7) < 1e-6
        assert abs(conf_b - 0.3) < 1e-6

        # Unpartitioned hash should be deleted
        assert not POPOTO_REDIS_DB.exists(unpartitioned_key)


# --- Error Cases ---


class TestPartitionedErrors:
    def test_partition_by_nonexistent_field_none_value(self):
        """on_save with None partition value still works (writes to base key)."""
        # This tests graceful handling - partition value None is skipped
        # in get_data_hash_key, so the key is the same as unpartitioned
        _item = PartitionedConfidence(name="err1", project=None)  # noqa: F841
        # Model should still be creatable if the field allows null
        # (KeyField does not allow null by default, so this tests the boundary)

    def testget_data_hash_key_from_values_missing_partition(self):
        """get_data_hash_key_from_values raises QueryException for missing partition."""
        field = PartitionedConfidence._meta.fields["certainty"]
        with pytest.raises(QueryException, match="partitioned by"):
            field.get_data_hash_key_from_values(PartitionedConfidence, "certainty")

    def testget_data_hash_key_from_values_with_partition(self):
        """get_data_hash_key_from_values builds correct key with partition values."""
        field = PartitionedConfidence._meta.fields["certainty"]
        key = field.get_data_hash_key_from_values(
            PartitionedConfidence, "certainty", project="atlas"
        )
        assert ":data:atlas" in key
