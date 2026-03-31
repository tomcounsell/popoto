"""Tests for Model.clean_indexes() orphan removal.

Verifies orphan cleanup across all five index types (class set, key fields,
sorted fields, geo fields, composite indexes), async counterpart, batch_size
parameter, round-trip with check_indexes(), and deprecation warning update.

Related: Issue #320
"""

import asyncio
import logging

import popoto
from popoto.redis_db import POPOTO_REDIS_DB
from popoto.fields.geo_field import GeoField


# ---------------------------------------------------------------------------
# Test model definitions (reuse same patterns as test_check_indexes.py)
# ---------------------------------------------------------------------------


class CleanUser(popoto.Model):
    """Simple model with a key field and sorted field."""

    email = popoto.KeyField()
    score = popoto.SortedField(type=float)
    name = popoto.Field(type=str, null=True)


class CleanGeoPlace(popoto.Model):
    """Model with a geo field."""

    name = popoto.KeyField()
    location = GeoField()


class CleanComposite(popoto.Model):
    """Model with a composite index."""

    item_id = popoto.KeyField()
    category = popoto.Field(type=str)
    brand = popoto.Field(type=str)

    class Meta:
        indexes = ((("category", "brand"), False),)


class CleanMinimal(popoto.Model):
    """Model with no sorted, geo, or composite indexes."""

    uuid = popoto.AutoKeyField()
    data = popoto.Field(type=str, null=True)


class CleanPartitioned(popoto.Model):
    """Model with a partitioned sorted field."""

    uuid = popoto.AutoKeyField()
    category = popoto.KeyField()
    price = popoto.SortedField(type=float, partition_by="category")


# ---------------------------------------------------------------------------
# Tests: No orphans (healthy state)
# ---------------------------------------------------------------------------


class TestCleanIndexesHealthy:
    """Tests that clean_indexes returns 0 when there are no orphans."""

    def test_no_instances_returns_zero(self):
        """Model with no instances returns 0."""
        result = CleanUser.clean_indexes()
        assert result == 0

    def test_healthy_instances_return_zero(self):
        """Model with valid instances returns 0 and performs no writes."""
        CleanUser.create(email="healthy1@test.com", score=10.0, name="Alice")
        CleanUser.create(email="healthy2@test.com", score=20.0, name="Bob")

        result = CleanUser.clean_indexes()
        assert result == 0

    def test_minimal_model_returns_zero(self):
        """Model with no sorted/geo/composite indexes returns 0."""
        CleanMinimal.create(data="test")

        result = CleanMinimal.clean_indexes()
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: Orphan removal per index type
# ---------------------------------------------------------------------------


class TestCleanIndexesOrphanRemoval:
    """Tests that orphaned index entries are removed for each index type."""

    def test_class_set_orphan_removal(self):
        """Removes orphan from class set when instance hash is deleted."""
        user = CleanUser.create(email="csorphan@test.com", score=5.0)
        redis_key = user.db_key.redis_key

        # Delete the instance hash directly, leaving stale index entries
        POPOTO_REDIS_DB.delete(redis_key)

        # Verify orphan exists
        check_before = CleanUser.check_indexes()
        assert check_before["class_set"] >= 1

        removed = CleanUser.clean_indexes()
        assert removed >= 1

    def test_key_field_orphan_removal(self):
        """Removes orphan from key field index."""
        user = CleanUser.create(email="kforphan@test.com", score=5.0)
        redis_key = user.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        removed = CleanUser.clean_indexes()
        assert removed >= 1

    def test_sorted_field_orphan_removal(self):
        """Removes orphan from sorted field index."""
        user = CleanUser.create(email="sforphan@test.com", score=15.0)
        redis_key = user.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        removed = CleanUser.clean_indexes()
        assert removed >= 1

    def test_geo_field_orphan_removal(self):
        """Removes orphan from geo field index."""
        place = CleanGeoPlace.create(name="ghost", location=(51.5074, -0.1278))
        redis_key = place.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        removed = CleanGeoPlace.clean_indexes()
        assert removed >= 1

    def test_composite_index_orphan_removal(self):
        """Removes orphan from composite index."""
        item = CleanComposite.create(
            item_id="ghost_item", category="tools", brand="wrench_co"
        )
        redis_key = item.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        removed = CleanComposite.clean_indexes()
        assert removed >= 1

    def test_partitioned_sorted_field_orphan_removal(self):
        """Removes orphan from partitioned sorted field index."""
        item = CleanPartitioned.create(category="electronics", price=99.99)
        redis_key = item.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        removed = CleanPartitioned.clean_indexes()
        assert removed >= 1

    def test_multiple_orphans_counted(self):
        """Multiple orphaned entries are counted correctly."""
        users = []
        for i in range(5):
            u = CleanUser.create(email=f"multi_clean{i}@test.com", score=float(i))
            users.append(u)

        # Delete 3 of 5 instance hashes
        for u in users[:3]:
            POPOTO_REDIS_DB.delete(u.db_key.redis_key)

        removed = CleanUser.clean_indexes()
        # Each orphan appears in class set + key field + sorted field = 3 entries each
        # 3 orphans * 3 index types = 9 minimum
        assert removed >= 9


# ---------------------------------------------------------------------------
# Tests: Post-cleanup verification via check_indexes round-trip
# ---------------------------------------------------------------------------


class TestCleanIndexesRoundTrip:
    """Verify that after clean_indexes, check_indexes reports zero orphans."""

    def test_class_set_clean_then_check(self):
        """After cleaning class set orphans, check_indexes reports 0."""
        user = CleanUser.create(email="rtcs@test.com", score=1.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        assert CleanUser.check_indexes()["total"] > 0
        CleanUser.clean_indexes()
        assert CleanUser.check_indexes()["total"] == 0

    def test_geo_clean_then_check(self):
        """After cleaning geo orphans, check_indexes reports 0."""
        place = CleanGeoPlace.create(name="rtgeo", location=(40.7128, -74.006))
        POPOTO_REDIS_DB.delete(place.db_key.redis_key)

        assert CleanGeoPlace.check_indexes()["total"] > 0
        CleanGeoPlace.clean_indexes()
        assert CleanGeoPlace.check_indexes()["total"] == 0

    def test_composite_clean_then_check(self):
        """After cleaning composite orphans, check_indexes reports 0."""
        item = CleanComposite.create(
            item_id="rtcomp", category="food", brand="organic_co"
        )
        POPOTO_REDIS_DB.delete(item.db_key.redis_key)

        assert CleanComposite.check_indexes()["total"] > 0
        CleanComposite.clean_indexes()
        assert CleanComposite.check_indexes()["total"] == 0

    def test_partitioned_clean_then_check(self):
        """After cleaning partitioned orphans, check_indexes reports 0."""
        item = CleanPartitioned.create(category="books", price=19.99)
        POPOTO_REDIS_DB.delete(item.db_key.redis_key)

        assert CleanPartitioned.check_indexes()["total"] > 0
        CleanPartitioned.clean_indexes()
        assert CleanPartitioned.check_indexes()["total"] == 0

    def test_full_round_trip_multiple_types(self):
        """Create orphans across multiple index types, clean, verify all zero."""
        user = CleanUser.create(email="rtfull@test.com", score=42.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        check = CleanUser.check_indexes()
        assert check["class_set"] > 0
        assert check["key_fields"]["email"] > 0
        assert check["sorted_fields"]["score"] > 0

        removed = CleanUser.clean_indexes()
        assert removed > 0

        check_after = CleanUser.check_indexes()
        assert check_after["class_set"] == 0
        assert check_after["key_fields"]["email"] == 0
        assert check_after["sorted_fields"]["score"] == 0
        assert check_after["total"] == 0


# ---------------------------------------------------------------------------
# Tests: Async version
# ---------------------------------------------------------------------------


class TestAsyncCleanIndexes:
    """Verify async_clean_indexes matches sync behavior."""

    def test_async_returns_same_as_sync_healthy(self):
        """async_clean_indexes returns 0 on healthy data like sync."""
        CleanUser.create(email="asynchealthy@test.com", score=7.0)

        sync_result = CleanUser.clean_indexes()
        async_result = asyncio.run(CleanUser.async_clean_indexes())
        assert sync_result == async_result == 0

    def test_async_removes_orphans(self):
        """async_clean_indexes correctly removes orphans."""
        user = CleanUser.create(email="asyncorphan@test.com", score=3.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        removed = asyncio.run(CleanUser.async_clean_indexes())
        assert removed >= 1

        # Verify cleanup was effective
        assert CleanUser.check_indexes()["total"] == 0


# ---------------------------------------------------------------------------
# Tests: batch_size parameter
# ---------------------------------------------------------------------------


class TestCleanIndexesBatchSize:
    """Verify batch_size parameter works correctly."""

    def test_batch_size_accepted(self):
        """batch_size parameter is accepted and produces same results."""
        user = CleanUser.create(email="batchtest@test.com", score=1.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        # Create fresh orphans for each batch size test
        user1 = CleanUser.create(email="batch1@test.com", score=1.0)
        user2 = CleanUser.create(email="batch2@test.com", score=2.0)
        user3 = CleanUser.create(email="batch3@test.com", score=3.0)
        POPOTO_REDIS_DB.delete(user1.db_key.redis_key)
        POPOTO_REDIS_DB.delete(user2.db_key.redis_key)
        POPOTO_REDIS_DB.delete(user3.db_key.redis_key)

        # Small batch size should still clean everything
        removed = CleanUser.clean_indexes(batch_size=1)
        assert removed > 0
        assert CleanUser.check_indexes()["total"] == 0

    def test_large_batch_size(self):
        """Large batch_size works without error."""
        user = CleanUser.create(email="largebatch@test.com", score=1.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        removed = CleanUser.clean_indexes(batch_size=10000)
        assert removed >= 1


# ---------------------------------------------------------------------------
# Tests: Deprecation warning
# ---------------------------------------------------------------------------


class TestDeprecationWarning:
    """Verify Query.keys(clean=True) warning is updated."""

    def test_warning_references_clean_indexes(self, caplog):
        """Warning message references Model.clean_indexes()."""
        CleanUser.create(email="warntest@test.com", score=1.0)

        with caplog.at_level(logging.WARNING):
            CleanUser.query.keys(clean=True)

        assert any("clean_indexes()" in record.message for record in caplog.records), (
            f"Expected warning referencing clean_indexes(), got: {[r.message for r in caplog.records]}"
        )

    def test_warning_says_deprecated(self, caplog):
        """Warning message says deprecated."""
        CleanUser.create(email="warntest2@test.com", score=1.0)

        with caplog.at_level(logging.WARNING):
            CleanUser.query.keys(clean=True)

        assert any(
            "deprecated" in record.message.lower() for record in caplog.records
        ), f"Expected deprecation warning, got: {[r.message for r in caplog.records]}"
