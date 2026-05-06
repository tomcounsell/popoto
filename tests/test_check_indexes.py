"""Tests for Model.check_indexes() read-only health check.

Verifies orphan detection across all five index types (class set, key fields,
sorted fields, geo fields, composite indexes), read-only guarantee, and async
counterpart.

Related: Issue #322
"""

import asyncio

import popoto
from popoto.redis_db import POPOTO_REDIS_DB
from popoto.fields.geo_field import GeoField

# ---------------------------------------------------------------------------
# Test model definitions
# ---------------------------------------------------------------------------


class CheckUser(popoto.Model):
    """Simple model with a key field and sorted field."""

    email = popoto.KeyField()
    score = popoto.SortedField(type=float)
    name = popoto.Field(type=str, null=True)


class CheckGeoPlace(popoto.Model):
    """Model with a geo field."""

    name = popoto.KeyField()
    location = GeoField()


class CheckComposite(popoto.Model):
    """Model with a composite index."""

    item_id = popoto.KeyField()
    category = popoto.Field(type=str)
    brand = popoto.Field(type=str)

    class Meta:
        indexes = ((("category", "brand"), False),)


class CheckMinimal(popoto.Model):
    """Model with no sorted, geo, or composite indexes."""

    uuid = popoto.AutoKeyField()
    data = popoto.Field(type=str, null=True)


class CheckPartitioned(popoto.Model):
    """Model with a partitioned sorted field."""

    uuid = popoto.AutoKeyField()
    category = popoto.KeyField()
    price = popoto.SortedField(type=float, partition_by="category")


# ---------------------------------------------------------------------------
# Tests: No orphans (healthy state)
# ---------------------------------------------------------------------------


class TestCheckIndexesHealthy:
    """Tests that check_indexes returns zeros when there are no orphans."""

    def test_no_instances_returns_zeros(self):
        """Model with no instances returns all-zero counts."""
        result = CheckUser.check_indexes()
        assert result["class_set"] == 0
        assert result["key_fields"]["email"] == 0
        assert result["sorted_fields"]["score"] == 0
        assert result["total"] == 0

    def test_healthy_instances_return_zeros(self):
        """Model with valid instances returns zero orphans."""
        CheckUser.create(email="alice@test.com", score=10.0, name="Alice")
        CheckUser.create(email="bob@test.com", score=20.0, name="Bob")

        result = CheckUser.check_indexes()
        assert result["class_set"] == 0
        assert result["key_fields"]["email"] == 0
        assert result["sorted_fields"]["score"] == 0
        assert result["total"] == 0

    def test_healthy_geo_returns_zero(self):
        """Geo field with valid instances returns zero orphans."""
        CheckGeoPlace.create(name="office", location=(40.7128, -74.0060))

        result = CheckGeoPlace.check_indexes()
        assert result["class_set"] == 0
        assert result["geo_fields"]["location"] == 0
        assert result["total"] == 0

    def test_healthy_composite_returns_zero(self):
        """Composite index with valid instances returns zero orphans."""
        CheckComposite.create(item_id="item1", category="electronics", brand="acme")

        result = CheckComposite.check_indexes()
        assert result["class_set"] == 0
        assert result["composite_indexes"] is not None
        assert result["total"] == 0

    def test_minimal_model_returns_zeros(self):
        """Model with no sorted/geo/composite indexes returns empty sub-dicts."""
        CheckMinimal.create(data="test")

        result = CheckMinimal.check_indexes()
        assert result["class_set"] == 0
        assert result["sorted_fields"] == {}
        assert result["geo_fields"] == {}
        assert result["composite_indexes"] == {}
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Tests: Orphan detection
# ---------------------------------------------------------------------------


class TestCheckIndexesOrphanDetection:
    """Tests that orphaned index entries are correctly detected."""

    def test_class_set_orphan(self):
        """Detects orphan in class set when instance hash is deleted."""
        user = CheckUser.create(email="orphan@test.com", score=5.0)
        redis_key = user.db_key.redis_key

        # Delete the instance hash directly, leaving stale index entries
        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckUser.check_indexes()
        assert result["class_set"] >= 1
        assert result["total"] >= 1

    def test_key_field_orphan(self):
        """Detects orphan in key field index when instance hash is deleted."""
        user = CheckUser.create(email="keyorphan@test.com", score=5.0)
        redis_key = user.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckUser.check_indexes()
        assert result["key_fields"]["email"] >= 1

    def test_sorted_field_orphan(self):
        """Detects orphan in sorted field index when instance hash is deleted."""
        user = CheckUser.create(email="sortorphan@test.com", score=15.0)
        redis_key = user.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckUser.check_indexes()
        assert result["sorted_fields"]["score"] >= 1

    def test_geo_field_orphan(self):
        """Detects orphan in geo field index when instance hash is deleted."""
        place = CheckGeoPlace.create(name="ghost", location=(51.5074, -0.1278))
        redis_key = place.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckGeoPlace.check_indexes()
        assert result["geo_fields"]["location"] >= 1
        assert result["total"] >= 1

    def test_composite_index_orphan(self):
        """Detects orphan in composite index when instance hash is deleted."""
        item = CheckComposite.create(
            item_id="ghost_item", category="tools", brand="wrench_co"
        )
        redis_key = item.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckComposite.check_indexes()
        # Composite index should detect the orphan
        assert result["total"] >= 1
        total_composite = sum(result["composite_indexes"].values())
        assert total_composite >= 1

    def test_partitioned_sorted_field_orphan(self):
        """Detects orphan in partitioned sorted field index."""
        item = CheckPartitioned.create(category="electronics", price=99.99)
        redis_key = item.db_key.redis_key

        POPOTO_REDIS_DB.delete(redis_key)

        result = CheckPartitioned.check_indexes()
        assert result["sorted_fields"]["price"] >= 1

    def test_multiple_orphans_counted(self):
        """Multiple orphaned entries are counted correctly."""
        users = []
        for i in range(5):
            u = CheckUser.create(email=f"multi{i}@test.com", score=float(i))
            users.append(u)

        # Delete 3 of 5 instance hashes
        for u in users[:3]:
            POPOTO_REDIS_DB.delete(u.db_key.redis_key)

        result = CheckUser.check_indexes()
        assert result["class_set"] == 3
        assert result["key_fields"]["email"] == 3
        assert result["sorted_fields"]["score"] == 3

    def test_total_is_sum_of_all_types(self):
        """Total field equals the sum of all orphan counts across types."""
        user = CheckUser.create(email="totalcheck@test.com", score=42.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        result = CheckUser.check_indexes()
        expected_total = (
            result["class_set"]
            + result["partial_writes"]
            + sum(result["key_fields"].values())
            + sum(result["sorted_fields"].values())
            + sum(result["geo_fields"].values())
            + sum(result["composite_indexes"].values())
        )
        assert result["total"] == expected_total


# ---------------------------------------------------------------------------
# Tests: Read-only guarantee
# ---------------------------------------------------------------------------


class TestCheckIndexesReadOnly:
    """Verify check_indexes makes zero writes to Redis."""

    def test_read_only_no_orphans(self):
        """check_indexes does not modify Redis state with healthy data."""
        CheckUser.create(email="readonly1@test.com", score=1.0)
        CheckUser.create(email="readonly2@test.com", score=2.0)

        # Snapshot all keys and their values
        all_keys = set(POPOTO_REDIS_DB.keys("*"))
        snapshot = {}
        for key in all_keys:
            key_type = POPOTO_REDIS_DB.type(key)
            if isinstance(key_type, bytes):
                key_type = key_type.decode("utf-8")
            if key_type == "string":
                snapshot[key] = POPOTO_REDIS_DB.get(key)
            elif key_type == "hash":
                snapshot[key] = POPOTO_REDIS_DB.hgetall(key)
            elif key_type == "set":
                snapshot[key] = POPOTO_REDIS_DB.smembers(key)
            elif key_type == "zset":
                snapshot[key] = POPOTO_REDIS_DB.zrange(key, 0, -1, withscores=True)

        # Run check
        CheckUser.check_indexes()

        # Verify no keys were added or removed
        all_keys_after = set(POPOTO_REDIS_DB.keys("*"))
        assert all_keys == all_keys_after

        # Verify no values changed
        for key in all_keys:
            key_type = POPOTO_REDIS_DB.type(key)
            if isinstance(key_type, bytes):
                key_type = key_type.decode("utf-8")
            if key_type == "string":
                assert snapshot[key] == POPOTO_REDIS_DB.get(key)
            elif key_type == "hash":
                assert snapshot[key] == POPOTO_REDIS_DB.hgetall(key)
            elif key_type == "set":
                assert snapshot[key] == POPOTO_REDIS_DB.smembers(key)
            elif key_type == "zset":
                assert snapshot[key] == POPOTO_REDIS_DB.zrange(
                    key, 0, -1, withscores=True
                )

    def test_read_only_with_orphans(self):
        """check_indexes does not modify Redis state even when orphans exist."""
        user = CheckUser.create(email="readonly_orphan@test.com", score=5.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        # Snapshot
        all_keys = set(POPOTO_REDIS_DB.keys("*"))

        # Run check
        result = CheckUser.check_indexes()
        assert result["total"] > 0

        # Verify no keys were added or removed
        all_keys_after = set(POPOTO_REDIS_DB.keys("*"))
        assert all_keys == all_keys_after


# ---------------------------------------------------------------------------
# Tests: Return structure
# ---------------------------------------------------------------------------


class TestCheckIndexesReturnStructure:
    """Verify the return dict structure."""

    def test_return_keys_present(self):
        """Return dict contains all expected keys."""
        result = CheckUser.check_indexes()
        assert "class_set" in result
        assert "partial_writes" in result
        assert "key_fields" in result
        assert "sorted_fields" in result
        assert "geo_fields" in result
        assert "composite_indexes" in result
        assert "total" in result

    def test_return_types(self):
        """Return dict values have correct types."""
        result = CheckUser.check_indexes()
        assert isinstance(result["class_set"], int)
        assert isinstance(result["partial_writes"], int)
        assert isinstance(result["key_fields"], dict)
        assert isinstance(result["sorted_fields"], dict)
        assert isinstance(result["geo_fields"], dict)
        assert isinstance(result["composite_indexes"], dict)
        assert isinstance(result["total"], int)

    def test_partial_writes_zero_for_composite_keyfield_model(self):
        """Models with composite KeyField (no AutoKeyField) always report 0."""
        result = CheckComposite.check_indexes()
        assert result["partial_writes"] == 0

    def test_partial_writes_present_for_minimal_automodel(self):
        """AutoKeyField-eligible model exposes the partial_writes key."""
        result = CheckMinimal.check_indexes()
        assert result["partial_writes"] == 0
        assert isinstance(result["partial_writes"], int)

    def test_batch_size_parameter(self):
        """batch_size parameter is accepted and produces same results."""
        CheckUser.create(email="batch@test.com", score=1.0)

        result_default = CheckUser.check_indexes()
        result_small = CheckUser.check_indexes(batch_size=1)
        result_large = CheckUser.check_indexes(batch_size=10000)

        assert result_default == result_small == result_large


# ---------------------------------------------------------------------------
# Tests: Async version
# ---------------------------------------------------------------------------


class TestAsyncCheckIndexes:
    """Verify async_check_indexes matches sync behavior."""

    def test_async_returns_same_as_sync(self):
        """async_check_indexes returns the same result as check_indexes."""
        CheckUser.create(email="async@test.com", score=7.0)

        sync_result = CheckUser.check_indexes()
        async_result = asyncio.run(CheckUser.async_check_indexes())
        assert sync_result == async_result

    def test_async_detects_orphans(self):
        """async_check_indexes correctly detects orphans."""
        user = CheckUser.create(email="async_orphan@test.com", score=3.0)
        POPOTO_REDIS_DB.delete(user.db_key.redis_key)

        result = asyncio.run(CheckUser.async_check_indexes())
        assert result["total"] >= 1
        assert result["class_set"] >= 1


# ---------------------------------------------------------------------------
# Tests: Partial-write orphan detection
# ---------------------------------------------------------------------------


def _inject_partial_write_orphan(model_cls, missing_field_name: str) -> str:
    """Manually create a partial-write orphan: a hash that exists in Redis
    but is missing the given primary-key field, and is registered in the
    class set.

    Returns the orphan's redis_key (str).
    """
    class_set_key = model_cls._meta.db_class_set_key.redis_key
    # Build a fake redis key with the right shape: ClassName:<auto_id_value>
    # Use a placeholder value matching AutoKeyField's default uuid4 length (32).
    fake_value = "deadbeef" * 4  # 32 chars
    orphan_redis_key = f"{model_cls.__name__}:{fake_value}"

    # Inject a hash with EVERY field EXCEPT the auto-key. This simulates
    # a save that completed pipeline.hset for non-key fields but the
    # auto-key field was either never set or was HDEL'd by a migration.
    POPOTO_REDIS_DB.hset(orphan_redis_key, mapping={"data": "ghost"})
    POPOTO_REDIS_DB.sadd(class_set_key, orphan_redis_key)
    return orphan_redis_key


def _inject_partial_write_orphan_with_value(
    model_cls, missing_field_name: str, raw_value
) -> str:
    """Like _inject_partial_write_orphan but writes the auto-key field with
    a specific raw value (e.g., empty bytes/string) to verify normalization.
    """
    class_set_key = model_cls._meta.db_class_set_key.redis_key
    fake_value = "feedbeef" * 4
    orphan_redis_key = f"{model_cls.__name__}:{fake_value}"
    POPOTO_REDIS_DB.hset(
        orphan_redis_key,
        mapping={"data": "ghost", missing_field_name: raw_value},
    )
    POPOTO_REDIS_DB.sadd(class_set_key, orphan_redis_key)
    return orphan_redis_key


class TestCheckIndexesPartialWriteOrphans:
    """Verify partial-write orphan detection in check_indexes()."""

    def test_partial_write_orphan_counted(self):
        """A class-set member whose hash lacks the auto-key is counted."""
        # Create one healthy instance to make sure healthy rows aren't
        # mis-counted as partial-writes.
        CheckMinimal.create(data="healthy")

        _inject_partial_write_orphan(CheckMinimal, "uuid")

        result = CheckMinimal.check_indexes()
        assert result["partial_writes"] == 1
        # The hash exists, so the absent-orphan count should NOT include it.
        assert result["class_set"] == 0

    def test_partial_write_increments_total(self):
        """The total field includes partial_writes."""
        _inject_partial_write_orphan(CheckMinimal, "uuid")

        result = CheckMinimal.check_indexes()
        assert result["total"] >= result["partial_writes"]
        assert result["total"] >= 1

    def test_empty_bytes_value_counts_as_missing(self):
        """An auto-key field set to b"" is treated as a partial-write orphan."""
        _inject_partial_write_orphan_with_value(CheckMinimal, "uuid", b"")
        result = CheckMinimal.check_indexes()
        assert result["partial_writes"] == 1

    def test_empty_str_value_counts_as_missing(self):
        """An auto-key field set to "" is treated as a partial-write orphan."""
        _inject_partial_write_orphan_with_value(CheckMinimal, "uuid", "")
        result = CheckMinimal.check_indexes()
        assert result["partial_writes"] == 1

    def test_whitespace_value_counts_as_healthy(self):
        """An auto-key field set to " " is NOT treated as missing.

        Whitespace stripping is intentionally out of scope (see plan
        Rabbit Holes). Only None / b"" / "" count as missing.
        """
        _inject_partial_write_orphan_with_value(CheckMinimal, "uuid", " ")
        result = CheckMinimal.check_indexes()
        assert result["partial_writes"] == 0

    def test_composite_keyfield_model_skips_partial_write_check(self):
        """Composite-KeyField models never report partial_writes > 0.

        Even if we inject a hash that lacks every field, the model has no
        single AutoKeyField so the eligibility gate must short-circuit.
        """
        # Inject a class-set member with a hash that exists but is empty
        # of the composite key fields.
        class_set_key = CheckComposite._meta.db_class_set_key.redis_key
        fake_redis_key = "CheckComposite:item_x"
        POPOTO_REDIS_DB.hset(fake_redis_key, mapping={"category": "tools"})
        POPOTO_REDIS_DB.sadd(class_set_key, fake_redis_key)

        result = CheckComposite.check_indexes()
        # The composite-KeyField model must not classify this as a partial-write.
        assert result["partial_writes"] == 0

    def test_async_reports_partial_writes(self):
        """async_check_indexes returns the new dict shape with partial_writes."""
        _inject_partial_write_orphan(CheckMinimal, "uuid")

        result = asyncio.run(CheckMinimal.async_check_indexes())
        assert "partial_writes" in result
        assert result["partial_writes"] == 1
        assert result["total"] >= 1

    def test_round_trip_with_clean(self):
        """clean_indexes removes partial-writes; subsequent check returns 0."""
        _inject_partial_write_orphan(CheckMinimal, "uuid")
        assert CheckMinimal.check_indexes()["partial_writes"] == 1

        CheckMinimal.clean_indexes()

        assert CheckMinimal.check_indexes()["partial_writes"] == 0
