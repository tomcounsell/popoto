"""Cross-feature integration tests for v1.4.4 APIs.

Tests exercise feature combinations reflecting real-world usage patterns:
- Scenario 1: get_many + check_indexes + clean_indexes round-trip
- Scenario 2: partition_by (ConfidenceField) + clean_indexes
- Scenario 3: TTL expiry + check_indexes
- Async variant of Scenario 1

Related: Issue #337
"""

import asyncio

import popoto
from popoto.fields.confidence_field import ConfidenceField
from popoto.redis_db import POPOTO_REDIS_DB


# ---------------------------------------------------------------------------
# Model definitions
# ---------------------------------------------------------------------------


class IntegrationItem(popoto.Model):
    """Model for Scenario 1: get_many + check/clean indexes."""

    name = popoto.KeyField()
    score = popoto.SortedField(type=float)
    label = popoto.Field(type=str, null=True)


class IntegrationConfidence(popoto.Model):
    """Model for Scenario 2: partition_by + clean_indexes."""

    name = popoto.KeyField()
    category = popoto.KeyField()
    certainty = ConfidenceField(initial_confidence=0.5, partition_by="category")


class IntegrationTTL(popoto.Model):
    """Model for Scenario 3: simulated TTL expiry + check_indexes."""

    name = popoto.KeyField()
    score = popoto.SortedField(type=float)


# ---------------------------------------------------------------------------
# Scenario 1: get_many + check_indexes + clean_indexes round-trip
# ---------------------------------------------------------------------------


class TestGetManyCheckClean:
    """Tests the full diagnostic-and-repair workflow on bulk-fetched data."""

    def test_bulk_fetch_then_corrupt_then_diagnose_and_repair(self):
        """Full round-trip: create, bulk-fetch, corrupt, diagnose, clean, verify."""
        # 1. Create 5 instances
        items = []
        for i in range(5):
            item = IntegrationItem.create(
                name=f"item_{i}", score=float(i * 10), label=f"label_{i}"
            )
            items.append(item)

        # 2. Bulk-fetch all with get_many -- verify all returned correctly
        keys = [item.db_key.redis_key for item in items]
        results = IntegrationItem.query.get_many(redis_keys=keys)
        assert len(results) == 5
        for i, result in enumerate(results):
            assert result is not None
            assert result.name == f"item_{i}"
            assert result.score == float(i * 10)

        # 3. Delete 2 instance hashes directly (bypassing ORM)
        POPOTO_REDIS_DB.delete(items[1].db_key.redis_key)
        POPOTO_REDIS_DB.delete(items[3].db_key.redis_key)

        # 4. get_many again -- deleted instances return as None at correct positions
        results = IntegrationItem.query.get_many(redis_keys=keys)
        assert len(results) == 5
        assert results[0] is not None
        assert results[1] is None
        assert results[2] is not None
        assert results[3] is None
        assert results[4] is not None

        # 5. check_indexes -- verify orphans detected
        check_result = IntegrationItem.check_indexes()
        assert check_result["class_set"] >= 2
        assert check_result["sorted_fields"]["score"] >= 2
        assert check_result["total"] >= 2

        # 6. clean_indexes -- verify it returns count > 0
        removed = IntegrationItem.clean_indexes()
        assert removed >= 2

        # 7. check_indexes again -- verify total == 0
        check_after = IntegrationItem.check_indexes()
        assert check_after["total"] == 0

        # 8. get_many with skip_none=True -- only surviving instances
        surviving_keys = [items[0].db_key.redis_key, items[2].db_key.redis_key,
                          items[4].db_key.redis_key]
        results = IntegrationItem.query.get_many(
            redis_keys=surviving_keys, skip_none=True
        )
        assert len(results) == 3

        # 9. Verify surviving instances have correct field values
        assert results[0].name == "item_0"
        assert results[0].score == 0.0
        assert results[0].label == "label_0"
        assert results[1].name == "item_2"
        assert results[1].score == 20.0
        assert results[2].name == "item_4"
        assert results[2].score == 40.0

    def test_get_many_skip_none_after_clean(self):
        """After cleaning orphans, skip_none filters stale keys correctly."""
        a = IntegrationItem.create(name="alive", score=1.0, label="ok")
        b = IntegrationItem.create(name="dead", score=2.0, label="gone")

        all_keys = [a.db_key.redis_key, b.db_key.redis_key]

        # Delete one instance directly
        POPOTO_REDIS_DB.delete(b.db_key.redis_key)

        # Before clean: get_many returns None for deleted
        results = IntegrationItem.query.get_many(redis_keys=all_keys)
        assert results[0] is not None
        assert results[1] is None

        # Clean up orphans
        IntegrationItem.clean_indexes()

        # After clean: skip_none returns only the surviving instance
        results = IntegrationItem.query.get_many(
            redis_keys=all_keys, skip_none=True
        )
        assert len(results) == 1
        assert results[0].name == "alive"


# ---------------------------------------------------------------------------
# Scenario 2: partition_by (ConfidenceField) + clean_indexes
# ---------------------------------------------------------------------------


class TestPartitionByCleanIndexes:
    """Tests that index health tools work with partitioned ConfidenceField models."""

    def test_partitioned_model_corrupt_then_clean(self):
        """Create partitioned instances, corrupt one, diagnose, clean, verify."""
        # 1. Create instances with different categories
        inst_a = IntegrationConfidence.create(name="alpha", category="science")
        inst_b = IntegrationConfidence.create(name="bravo", category="science")
        inst_c = IntegrationConfidence.create(name="charlie", category="art")

        # 2. Update confidence values to ensure companion hash entries exist
        ConfidenceField.update_confidence(inst_a, "certainty", signal=0.9)
        ConfidenceField.update_confidence(inst_b, "certainty", signal=0.8)
        ConfidenceField.update_confidence(inst_c, "certainty", signal=0.7)

        # 3. Verify healthy state first
        check_healthy = IntegrationConfidence.check_indexes()
        assert check_healthy["total"] == 0

        # 4. Delete one instance hash directly
        POPOTO_REDIS_DB.delete(inst_b.db_key.redis_key)

        # 5. check_indexes -- verify orphans detected
        check_result = IntegrationConfidence.check_indexes()
        assert check_result["class_set"] >= 1
        assert check_result["total"] >= 1

        # 6. clean_indexes -- verify orphans removed
        removed = IntegrationConfidence.clean_indexes()
        assert removed >= 1

        # 7. check_indexes -- verify total == 0
        check_after = IntegrationConfidence.check_indexes()
        assert check_after["total"] == 0

        # 8. Verify surviving instances still have correct confidence data
        conf_a = ConfidenceField.get_confidence(inst_a, "certainty")
        assert isinstance(conf_a, float)
        assert conf_a > 0.5  # Signal of 0.9 should push confidence above initial

        data_a = ConfidenceField.get_confidence_data(inst_a, "certainty")
        assert data_a["evidence_count"] >= 1

        conf_c = ConfidenceField.get_confidence(inst_c, "certainty")
        assert isinstance(conf_c, float)

        data_c = ConfidenceField.get_confidence_data(inst_c, "certainty")
        assert data_c["evidence_count"] >= 1


# ---------------------------------------------------------------------------
# Scenario 3: TTL expiry + check_indexes
# ---------------------------------------------------------------------------


class TestTTLExpiryCheckIndexes:
    """Tests that expired keys are correctly detected as orphans."""

    def test_ttl_expiry_creates_orphan_indexes(self):
        """Simulated TTL expiry: manually delete the main key, leaving orphan indexes."""
        # 1. Create an instance
        inst = IntegrationTTL.create(name="ephemeral", score=42.0)

        # 2. Verify no orphans immediately
        check_result = IntegrationTTL.check_indexes()
        assert check_result["total"] == 0

        # 3. Simulate TTL expiry by deleting the main key directly
        POPOTO_REDIS_DB.delete(inst.db_key.redis_key)

        # 4. Verify instance key no longer exists
        fetched = IntegrationTTL.query.get(name="ephemeral")
        assert fetched is None

        # 5. check_indexes -- orphans detected (sorted field and class set survive deletion)
        check_result = IntegrationTTL.check_indexes()
        assert check_result["total"] >= 1

        # 6. clean_indexes -- remove orphans
        removed = IntegrationTTL.clean_indexes()
        assert removed >= 1

        # 7. check_indexes -- verify total == 0
        check_final = IntegrationTTL.check_indexes()
        assert check_final["total"] == 0


# ---------------------------------------------------------------------------
# Async variant of Scenario 1
# ---------------------------------------------------------------------------


class TestAsyncIntegration:
    """Async variant of the get_many + check_indexes + clean_indexes round-trip."""

    def test_async_round_trip(self):
        """Async version of the full diagnostic-and-repair workflow."""
        asyncio.run(self._async_round_trip())

    @staticmethod
    async def _async_round_trip():
        # 1. Create instances (sync -- async create is not the focus)
        items = []
        for i in range(5):
            item = IntegrationItem.create(
                name=f"async_item_{i}", score=float(i * 10), label=f"label_{i}"
            )
            items.append(item)

        # 2. Bulk-fetch all with async_get_many
        keys = [item.db_key.redis_key for item in items]
        results = await IntegrationItem.query.async_get_many(redis_keys=keys)
        assert len(results) == 5
        for i, result in enumerate(results):
            assert result is not None
            assert result.name == f"async_item_{i}"

        # 3. Delete 2 instance hashes directly
        POPOTO_REDIS_DB.delete(items[1].db_key.redis_key)
        POPOTO_REDIS_DB.delete(items[3].db_key.redis_key)

        # 4. async_get_many -- deleted instances return as None
        results = await IntegrationItem.query.async_get_many(redis_keys=keys)
        assert len(results) == 5
        assert results[1] is None
        assert results[3] is None

        # 5. async_check_indexes -- orphans detected
        check_result = await IntegrationItem.async_check_indexes()
        assert check_result["class_set"] >= 2
        assert check_result["total"] >= 2

        # 6. async_clean_indexes -- remove orphans
        removed = await IntegrationItem.async_clean_indexes()
        assert removed >= 2

        # 7. async_check_indexes -- verify total == 0
        check_after = await IntegrationItem.async_check_indexes()
        assert check_after["total"] == 0

        # 8. Verify surviving instances via async_get_many with skip_none
        results = await IntegrationItem.query.async_get_many(
            redis_keys=keys, skip_none=True
        )
        assert len(results) == 3
        assert results[0].name == "async_item_0"
        assert results[1].name == "async_item_2"
        assert results[2].name == "async_item_4"
