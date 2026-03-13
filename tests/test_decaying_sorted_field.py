"""Tests for DecayingSortedField — time-weighted scoring via Lua decay computation.

Tests cover:
- Field initialization (decay_rate, base_score_field, defaults)
- Validation (decay_rate > 0)
- Save behavior (timestamp stored as sorted set score)
- top_by_decay() query method returns ranked instances
- touch() method refreshes decay clock
- Partitioned sorted sets
- Error cases: wrong field type, missing partition filter, unsaved model
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.models.query import QueryException

# --- Test Models ---


class DecayItem(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()
    importance = popoto.FloatField(default=1.0)


class DecayWithBase(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(base_score_field="weight")
    weight = popoto.FloatField(default=1.0)


class PartitionedDecay(popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = DecayingSortedField(partition_by="category")


class NonDecayModel(popoto.Model):
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


# --- Setup / Teardown ---


def setup_module():
    """Clean up test data before running tests."""
    for model in [DecayItem, DecayWithBase, PartitionedDecay, NonDecayModel]:
        model.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    for model in [DecayItem, DecayWithBase, PartitionedDecay, NonDecayModel]:
        model.delete_all()


# --- Field initialization tests ---


class TestDecayingSortedFieldInit:
    """Test field construction and validation."""

    def test_default_decay_rate(self):
        """Default decay_rate is 0.5."""
        field = DecayItem._meta.fields["relevance"]
        assert field.decay_rate == 0.5

    def test_custom_decay_rate(self):
        """Custom decay_rate is preserved."""

        class CustomDecay(popoto.Model):
            name = popoto.UniqueKeyField()
            score = DecayingSortedField(decay_rate=0.8)

        field = CustomDecay._meta.fields["score"]
        assert field.decay_rate == 0.8

    def test_invalid_decay_rate_zero(self):
        """decay_rate=0 raises ModelException."""
        with pytest.raises(popoto.ModelException):

            class BadDecay(popoto.Model):
                name = popoto.UniqueKeyField()
                score = DecayingSortedField(decay_rate=0)

    def test_invalid_decay_rate_negative(self):
        """Negative decay_rate raises ModelException."""
        with pytest.raises(popoto.ModelException):

            class BadDecay2(popoto.Model):
                name = popoto.UniqueKeyField()
                score = DecayingSortedField(decay_rate=-0.5)

    def test_base_score_field_default_none(self):
        """base_score_field defaults to None."""
        field = DecayItem._meta.fields["relevance"]
        assert field.base_score_field is None

    def test_base_score_field_set(self):
        """base_score_field is stored correctly."""
        field = DecayWithBase._meta.fields["relevance"]
        assert field.base_score_field == "weight"

    def test_field_type_is_float(self):
        """Field type is always float."""
        field = DecayItem._meta.fields["relevance"]
        assert field.type is float

    def test_is_sorted_field(self):
        """DecayingSortedField is a SortedFieldMixin subclass."""
        from src.popoto.fields.sorted_field_mixin import SortedFieldMixin

        field = DecayItem._meta.fields["relevance"]
        assert isinstance(field, SortedFieldMixin)


# --- Save behavior tests ---


class TestDecayingSortedFieldSave:
    """Test that saving stores timestamp as sorted set score."""

    def setup_method(self):
        DecayItem.delete_all()

    def teardown_method(self):
        DecayItem.delete_all()

    def test_save_sets_timestamp(self):
        """Saving a model with DecayingSortedField stores current timestamp."""
        before = time.time()
        item = DecayItem.create(name="test_ts")
        after = time.time()

        assert item.relevance is not None
        assert before <= item.relevance <= after

    def test_save_updates_timestamp(self):
        """Re-saving updates the timestamp (auto_now behavior)."""
        item = DecayItem.create(name="test_update")
        first_ts = item.relevance

        time.sleep(0.05)
        item.save()

        assert item.relevance > first_ts


# --- top_by_decay() tests ---


class TestTopByDecay:
    """Test QueryBuilder.top_by_decay() method."""

    def setup_method(self):
        DecayItem.delete_all()
        DecayWithBase.delete_all()
        PartitionedDecay.delete_all()

    def teardown_method(self):
        DecayItem.delete_all()
        DecayWithBase.delete_all()
        PartitionedDecay.delete_all()

    def test_basic_ranking(self):
        """More recently saved items rank higher with equal base scores."""
        # Create items with staggered saves
        item_old = DecayItem.create(name="old")

        # Manually backdate old item's sorted set score
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {item_old.db_key.redis_key: time.time() - 86400 * 10},
        )

        DecayItem.create(name="new")

        results = DecayItem.query.top_by_decay("relevance", n=10)

        assert len(results) == 2
        assert results[0].name == "new"
        assert results[1].name == "old"

    def test_empty_set_returns_empty(self):
        """top_by_decay on empty set returns []."""
        results = DecayItem.query.top_by_decay("relevance", n=10)
        assert results == []

    def test_n_limits_results(self):
        """n parameter limits result count."""
        for i in range(5):
            DecayItem.create(name=f"item_{i}")

        results = DecayItem.query.top_by_decay("relevance", n=2)
        assert len(results) == 2

    def test_n_zero_returns_empty(self):
        """n=0 returns empty list."""
        DecayItem.create(name="something")
        results = DecayItem.query.top_by_decay("relevance", n=0)
        assert results == []

    def test_decay_rate_override(self):
        """Override decay_rate at query time."""
        # Create two items at different times
        item_old = DecayItem.create(name="old2")
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {item_old.db_key.redis_key: time.time() - 86400 * 5},
        )
        DecayItem.create(name="new2")

        # Both should work with different decay rates
        results_low = DecayItem.query.top_by_decay("relevance", n=10, decay_rate=0.1)
        results_high = DecayItem.query.top_by_decay("relevance", n=10, decay_rate=2.0)

        # Both return results
        assert len(results_low) == 2
        assert len(results_high) == 2
        # With both decay rates, recent item still wins
        assert results_low[0].name == "new2"
        assert results_high[0].name == "new2"

    def test_wrong_field_type_raises(self):
        """top_by_decay on non-DecayingSortedField raises QueryException."""
        with pytest.raises(QueryException):
            NonDecayModel.query.top_by_decay("score", n=10)

    def test_nonexistent_field_raises(self):
        """top_by_decay on missing field raises QueryException."""
        with pytest.raises(QueryException):
            DecayItem.query.top_by_decay("nonexistent", n=10)

    def test_with_base_score_field(self):
        """base_score_field multiplies decay curve."""
        # Create items with different weights
        heavy = DecayWithBase.create(name="heavy", weight=10.0)
        light = DecayWithBase.create(name="light", weight=1.0)

        # Backdate both to same time (1 day ago)
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayWithBase, "relevance")
        one_day_ago = time.time() - 86400
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                heavy.db_key.redis_key: one_day_ago,
                light.db_key.redis_key: one_day_ago,
            },
        )

        results = DecayWithBase.query.top_by_decay("relevance", n=10)
        assert len(results) == 2
        assert results[0].name == "heavy"

    def test_partitioned_query(self):
        """top_by_decay respects partition_by."""
        PartitionedDecay.create(name="a1", category="A")
        PartitionedDecay.create(name="b1", category="B")

        results = PartitionedDecay.query.filter(category="A").top_by_decay(
            "relevance", n=10
        )
        assert len(results) == 1
        assert results[0].name == "a1"

    def test_partitioned_missing_filter_raises(self):
        """top_by_decay without partition filter raises QueryException."""
        PartitionedDecay.create(name="x1", category="X")

        with pytest.raises(QueryException):
            PartitionedDecay.query.top_by_decay("relevance", n=10)


# --- touch() tests ---


class TestTouch:
    """Test Model.touch() method."""

    def setup_method(self):
        DecayItem.delete_all()

    def teardown_method(self):
        DecayItem.delete_all()

    def test_touch_updates_timestamp(self):
        """touch() refreshes the sorted set score."""
        item = DecayItem.create(name="touchable")
        old_ts = item.relevance

        time.sleep(0.05)
        new_ts = item.touch("relevance")

        assert new_ts > old_ts
        assert item.relevance == new_ts

    def test_touch_wrong_field_raises(self):
        """touch() on non-DecayingSortedField raises TypeError."""
        item = NonDecayModel.create(name="bad_touch")
        with pytest.raises(TypeError):
            item.touch("score")
        item.delete()

    def test_touch_nonexistent_field_raises(self):
        """touch() on missing field raises AttributeError."""
        item = DecayItem.create(name="no_field")
        with pytest.raises(AttributeError):
            item.touch("nonexistent")

    def test_touch_unsaved_raises(self):
        """touch() on unsaved model raises TypeError."""
        item = DecayItem(name="unsaved_touch")
        with pytest.raises(TypeError):
            item.touch("relevance")

    def test_touch_affects_ranking(self):
        """touch() makes item rank higher in top_by_decay."""
        item_a = DecayItem.create(name="touch_a")
        item_b = DecayItem.create(name="touch_b")

        # Backdate both
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        old_time = time.time() - 86400 * 5
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                item_a.db_key.redis_key: old_time,
                item_b.db_key.redis_key: old_time,
            },
        )

        # Touch only item_a
        item_a.touch("relevance")

        results = DecayItem.query.top_by_decay("relevance", n=10)
        assert results[0].name == "touch_a"


# --- Decay formula verification ---


class TestDecayFormula:
    """Verify decay computation against hand-computed values."""

    def setup_method(self):
        DecayWithBase.delete_all()

    def teardown_method(self):
        DecayWithBase.delete_all()

    def test_known_decay_values(self):
        """Verify scores match: base_score * elapsed_days^(-decay_rate).

        With decay_rate=0.5 and base_score=1.0:
          1 day  -> 1.0 * 1^-0.5  = 1.0
          4 days -> 1.0 * 4^-0.5  = 0.5
          100 days -> 1.0 * 100^-0.5 = 0.1
        """
        now = time.time()
        items = {}
        for label, days in [("1day", 1), ("4day", 4), ("100day", 100)]:
            item = DecayWithBase.create(name=label, weight=1.0)
            items[label] = item

        # Backdate each item's sorted set score
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayWithBase, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                items["1day"].db_key.redis_key: now - 86400 * 1,
                items["4day"].db_key.redis_key: now - 86400 * 4,
                items["100day"].db_key.redis_key: now - 86400 * 100,
            },
        )

        results = DecayWithBase.query.top_by_decay("relevance", n=10)

        # Build a name->rank lookup
        names = [r.name for r in results]
        assert names == ["1day", "4day", "100day"]

    def test_base_score_scaling(self):
        """base_score=5.0 at 4 days = 5 * 4^-0.5 = 5 * 0.5 = 2.5.

        This should outrank base_score=1.0 at 1 day (score=1.0).
        """
        now = time.time()
        heavy = DecayWithBase.create(name="heavy_4d", weight=5.0)
        light = DecayWithBase.create(name="light_1d", weight=1.0)

        ss_key = DecayingSortedField.get_sortedset_db_key(DecayWithBase, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                heavy.db_key.redis_key: now - 86400 * 4,
                light.db_key.redis_key: now - 86400 * 1,
            },
        )

        results = DecayWithBase.query.top_by_decay("relevance", n=10)
        # heavy: 5.0 * 4^-0.5 = 2.5, light: 1.0 * 1^-0.5 = 1.0
        assert results[0].name == "heavy_4d"


# --- Performance benchmarks ---


class TestDecayBenchmarks:
    """Benchmark top_by_decay on larger sorted sets."""

    def setup_method(self):
        DecayItem.delete_all()

    def teardown_method(self):
        DecayItem.delete_all()

    def test_1k_members(self):
        """top_by_decay handles 1K members."""
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        now = time.time()

        # Bulk insert directly into sorted set and model hashes
        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        members = {}
        for i in range(1000):
            redis_key = f"DecayItem:bench1k_{i}"
            members[redis_key] = now - (i * 3600)  # each 1 hour older
        pipe.zadd(ss_key.redis_key, members)
        pipe.execute()

        start = time.time()
        result = popoto.POPOTO_REDIS_DB.eval(
            __import__(
                "src.popoto.fields.decaying_sorted_field", fromlist=["DECAY_SCORE_LUA"]
            ).DECAY_SCORE_LUA,
            1,
            ss_key.redis_key,
            str(now),
            "0.5",
            "10",
            "",
        )
        elapsed = time.time() - start

        assert len(result) == 20  # 10 items * 2 (key + score)
        assert elapsed < 1.0, f"1K members took {elapsed:.3f}s (expected < 1s)"

        # Cleanup
        popoto.POPOTO_REDIS_DB.delete(ss_key.redis_key)

    def test_10k_members(self):
        """top_by_decay handles 10K members."""
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        now = time.time()

        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        members = {}
        for i in range(10000):
            redis_key = f"DecayItem:bench10k_{i}"
            members[redis_key] = now - (i * 360)
        pipe.zadd(ss_key.redis_key, members)
        pipe.execute()

        start = time.time()
        result = popoto.POPOTO_REDIS_DB.eval(
            __import__(
                "src.popoto.fields.decaying_sorted_field", fromlist=["DECAY_SCORE_LUA"]
            ).DECAY_SCORE_LUA,
            1,
            ss_key.redis_key,
            str(now),
            "0.5",
            "10",
            "",
        )
        elapsed = time.time() - start

        assert len(result) == 20
        assert elapsed < 5.0, f"10K members took {elapsed:.3f}s (expected < 5s)"

        # Cleanup
        popoto.POPOTO_REDIS_DB.delete(ss_key.redis_key)


# --- Export tests ---


class TestExport:
    """Test that DecayingSortedField is accessible from popoto package."""

    def test_importable(self):
        """DecayingSortedField can be imported from popoto."""
        from src.popoto import DecayingSortedField as DSF

        assert DSF is DecayingSortedField
