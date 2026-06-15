"""Tests for auto_now_add and auto_now on SortedField."""

import time
from datetime import datetime, timezone

import pytest

from popoto import Model, KeyField, Field, SortedField, DatetimeField


class AutoNowAddModel(Model):
    """Model with auto_now_add timestamp."""

    key = KeyField(type=str)
    created_at = SortedField(type=float, auto_now_add=True)
    name = Field(type=str, null=True)


class AutoNowModel(Model):
    """Model with auto_now timestamp."""

    key = KeyField(type=str)
    updated_at = SortedField(type=float, auto_now=True)
    name = Field(type=str, null=True)


class AutoNowIntModel(Model):
    """Model with int timestamp."""

    key = KeyField(type=str)
    created_at = SortedField(type=int, auto_now_add=True)
    updated_at = SortedField(type=int, auto_now=True)


class BothTimestampsModel(Model):
    """Model with both auto_now_add and auto_now fields."""

    key = KeyField(type=str)
    created_at = SortedField(type=float, auto_now_add=True)
    updated_at = SortedField(type=float, auto_now=True)
    name = Field(type=str, null=True)


class TestAutoNowAdd:
    """Test auto_now_add=True behavior."""

    def setup_method(self):
        AutoNowAddModel.delete_all()
        AutoNowIntModel.delete_all()

    def teardown_method(self):
        AutoNowAddModel.delete_all()
        AutoNowIntModel.delete_all()

    def test_sets_timestamp_on_create(self):
        """auto_now_add should set timestamp on first save."""
        before = time.time()
        instance = AutoNowAddModel.create(key="test1")
        after = time.time()

        assert instance.created_at is not None
        assert before <= instance.created_at <= after

    def test_does_not_overwrite_on_update(self):
        """auto_now_add should NOT change timestamp on subsequent saves."""
        instance = AutoNowAddModel.create(key="test2", name="original")
        original_timestamp = instance.created_at

        time.sleep(0.01)  # Small delay to ensure time difference

        instance.name = "updated"
        instance.save()

        assert instance.created_at == original_timestamp

    def test_allows_explicit_value(self):
        """auto_now_add should allow explicit value override."""
        explicit_time = 1234567890.123
        instance = AutoNowAddModel.create(key="test3", created_at=explicit_time)

        assert instance.created_at == explicit_time

    def test_preserves_explicit_value_on_update(self):
        """Explicit value should be preserved on update."""
        explicit_time = 1234567890.123
        instance = AutoNowAddModel.create(key="test4", created_at=explicit_time)

        instance.name = "updated"
        instance.save()

        assert instance.created_at == explicit_time

    def test_int_type_truncates(self):
        """auto_now_add with int type should truncate (not round)."""
        before = int(time.time())
        instance = AutoNowIntModel.create(key="int_test")
        after = int(time.time())

        assert isinstance(instance.created_at, int)
        assert before <= instance.created_at <= after


class TestAutoNow:
    """Test auto_now=True behavior."""

    def setup_method(self):
        AutoNowModel.delete_all()
        AutoNowIntModel.delete_all()

    def teardown_method(self):
        AutoNowModel.delete_all()
        AutoNowIntModel.delete_all()

    def test_sets_timestamp_on_create(self):
        """auto_now should set timestamp on first save."""
        before = time.time()
        instance = AutoNowModel.create(key="test1")
        after = time.time()

        assert instance.updated_at is not None
        assert before <= instance.updated_at <= after

    def test_updates_timestamp_on_save(self):
        """auto_now should update timestamp on every save."""
        instance = AutoNowModel.create(key="test2", name="original")
        original_timestamp = instance.updated_at

        time.sleep(0.01)  # Small delay to ensure time difference

        instance.name = "updated"
        instance.save()

        assert instance.updated_at > original_timestamp

    def test_overwrites_explicit_value(self):
        """auto_now should overwrite explicit values on save."""
        explicit_time = 1234567890.123
        instance = AutoNowModel(key="test3", updated_at=explicit_time)

        before = time.time()
        instance.save()
        after = time.time()

        # Should be current time, not explicit_time
        assert before <= instance.updated_at <= after
        assert instance.updated_at != explicit_time

    def test_int_type_truncates(self):
        """auto_now with int type should truncate (not round)."""
        before = int(time.time())
        instance = AutoNowIntModel.create(key="int_test")
        after = int(time.time())

        assert isinstance(instance.updated_at, int)
        assert before <= instance.updated_at <= after


class TestBothTimestamps:
    """Test model with both auto_now_add and auto_now."""

    def setup_method(self):
        BothTimestampsModel.delete_all()

    def teardown_method(self):
        BothTimestampsModel.delete_all()

    def test_both_set_on_create(self):
        """Both timestamps should be set on create."""
        before = time.time()
        instance = BothTimestampsModel.create(key="test1")
        after = time.time()

        assert before <= instance.created_at <= after
        assert before <= instance.updated_at <= after

    def test_only_updated_at_changes_on_save(self):
        """Only auto_now field should change on subsequent saves."""
        instance = BothTimestampsModel.create(key="test2", name="original")
        original_created = instance.created_at
        original_updated = instance.updated_at

        time.sleep(0.01)

        instance.name = "updated"
        instance.save()

        assert instance.created_at == original_created  # Unchanged
        assert instance.updated_at > original_updated  # Updated


class TestSortedFieldIndex:
    """Test that auto timestamps work with SortedField indexing."""

    def setup_method(self):
        AutoNowAddModel.delete_all()

    def teardown_method(self):
        AutoNowAddModel.delete_all()

    def test_index_query_works_with_auto_timestamp(self):
        """Should be able to query by auto-generated timestamp."""
        before = time.time()
        instance1 = AutoNowAddModel.create(key="first")
        time.sleep(0.01)
        instance2 = AutoNowAddModel.create(key="second")
        after = time.time()

        # Query using the auto-generated timestamps
        results = list(
            AutoNowAddModel.query.filter(created_at__gte=before, created_at__lte=after)
        )

        assert len(results) == 2
        keys = {r.key for r in results}
        assert keys == {"first", "second"}

    def test_range_query_with_auto_timestamp(self):
        """Should be able to do range queries on auto timestamps."""
        instance1 = AutoNowAddModel.create(key="old")
        time.sleep(0.01)
        cutoff = time.time()
        time.sleep(0.01)
        instance2 = AutoNowAddModel.create(key="new")

        old_results = list(AutoNowAddModel.query.filter(created_at__lt=cutoff))
        new_results = list(AutoNowAddModel.query.filter(created_at__gte=cutoff))

        assert len(old_results) == 1
        assert old_results[0].key == "old"
        assert len(new_results) == 1
        assert new_results[0].key == "new"


class TestAsyncAutoTimestamps:
    """Test async methods with auto timestamps."""

    def setup_method(self):
        AutoNowAddModel.delete_all()
        AutoNowModel.delete_all()

    def teardown_method(self):
        AutoNowAddModel.delete_all()
        AutoNowModel.delete_all()

    @pytest.mark.asyncio
    async def test_async_create_with_auto_now_add(self):
        """async_create should work with auto_now_add."""
        before = time.time()
        instance = await AutoNowAddModel.async_create(key="async_test")
        after = time.time()

        assert before <= instance.created_at <= after

    @pytest.mark.asyncio
    async def test_async_save_with_auto_now(self):
        """async_save should update auto_now field."""
        instance = AutoNowModel.create(key="async_test", name="original")
        original_timestamp = instance.updated_at

        time.sleep(0.01)

        instance.name = "updated"
        await instance.async_save()

        assert instance.updated_at > original_timestamp


class UTCAutoNowAddModel(Model):
    """DatetimeField model with auto_now_add timestamp."""

    key = KeyField(type=str)
    created_at = DatetimeField(auto_now_add=True)
    name = Field(type=str, null=True)


class UTCAutoNowModel(Model):
    """DatetimeField model with auto_now timestamp."""

    key = KeyField(type=str)
    updated_at = DatetimeField(auto_now=True)
    name = Field(type=str, null=True)


def _as_naive(dt):
    """Drop tzinfo for naive-to-naive comparison.

    The encoder stores wall-clock without tzinfo, so a round-tripped value is
    naive. A freshly-stamped in-memory value is tz-aware UTC. Comparing the two
    directly raises TypeError, so we normalize both to naive before asserting.
    """
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class TestDatetimeFieldUTC:
    """DatetimeField auto_now/auto_now_add stamp UTC wall-clock (tomcounsell/ai#1653).

    Run under a non-UTC TZ (e.g. ``TZ=America/New_York pytest``) to make a naive
    ``datetime.now()`` regression visibly diverge: the stamped wall-clock would
    fall outside the UTC ``before``/``after`` bounds by the host's UTC offset.
    """

    def setup_method(self):
        UTCAutoNowAddModel.delete_all()
        UTCAutoNowModel.delete_all()

    def teardown_method(self):
        UTCAutoNowAddModel.delete_all()
        UTCAutoNowModel.delete_all()

    def test_auto_now_add_stamps_utc_wallclock(self):
        """auto_now_add stamps the current UTC wall-clock, not host-local time."""
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        instance = UTCAutoNowAddModel.create(key="dt-add-1")
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        assert instance.created_at is not None
        # in-memory fresh stamp (tz-aware UTC) normalized to naive UTC wall-clock
        assert before <= _as_naive(instance.created_at) <= after

        # round-trip from Redis: decoder returns a naive datetime carrying the
        # stored UTC wall-clock, which must still fall within the UTC bounds.
        reloaded = UTCAutoNowAddModel.query.get(key="dt-add-1")
        assert reloaded.created_at.tzinfo is None
        assert before <= reloaded.created_at <= after

    def test_auto_now_stamps_utc_wallclock(self):
        """auto_now stamps the current UTC wall-clock on every save."""
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        instance = UTCAutoNowModel.create(key="dt-now-1")
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        assert before <= _as_naive(instance.updated_at) <= after

        reloaded = UTCAutoNowModel.query.get(key="dt-now-1")
        assert reloaded.updated_at.tzinfo is None
        assert before <= reloaded.updated_at <= after

    def test_skip_auto_now_preserves_value(self):
        """skip_auto_now=True must not re-stamp an existing auto_now value."""
        instance = UTCAutoNowModel.create(key="dt-skip-1", name="original")
        original = instance.updated_at

        time.sleep(0.01)
        instance.name = "updated"
        instance.save(skip_auto_now=True)

        assert instance.updated_at == original
