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

import msgpack
import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import (
    DecayingSortedField,
    DECAY_SCORE_LUA,
    confidence_modulation_args,
)
from src.popoto.models.query import QueryException

# --- Test Models ---


class DecayItem(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()
    importance = popoto.FloatField(default=1.0)


class DecayConfItem(popoto.Model):
    """Auto-detects ``certainty`` for confidence-modulated decay (#491)."""

    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()


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
    for model in [
        DecayItem,
        DecayConfItem,
        DecayWithBase,
        PartitionedDecay,
        NonDecayModel,
    ]:
        model.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    for model in [
        DecayItem,
        DecayConfItem,
        DecayWithBase,
        PartitionedDecay,
        NonDecayModel,
    ]:
        model.delete_all()


# --- Field initialization tests ---


class TestDecayingSortedFieldInit:
    """Test field construction and validation."""

    def test_default_decay_rate(self):
        """Default decay_rate is ``Defaults.DECAY_RATE`` (0.1 after the
        2026-04-17 empirical tuning; see sweep_20260417_141047.json)."""
        from src.popoto.fields.constants import Defaults

        field = DecayItem._meta.fields["relevance"]
        assert field.decay_rate == Defaults.DECAY_RATE

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


class TestDecayBenchmarksWithModulation:
    """Same budgets as ``TestDecayBenchmarks``, with modulation ENABLED.

    Risk 3: modulation adds one HGET per member inside an O(N) loop. The
    existing ``base_score_field`` HGET is guarded by ``base_score_field ~= ''``
    and ``base_score_field`` defaults to ``None``, so for the common config
    this is 0 -> 1 HGET per member, not 1 -> 2. Both cases below therefore run
    with ``base_score_field=""`` -- the true worst case, not the flattering one.
    """

    CONF_KEY = "_bench:decay:confidence"
    ZSET_KEY = "_bench:decay:timestamps"

    def setup_method(self):
        popoto.POPOTO_REDIS_DB.delete(self.ZSET_KEY, self.CONF_KEY)

    def teardown_method(self):
        popoto.POPOTO_REDIS_DB.delete(self.ZSET_KEY, self.CONF_KEY)

    def _load(self, count, spacing_seconds):
        """Plant `count` members plus a confidence payload for every one."""
        now = time.time()
        members = {}
        confidences = {}
        for i in range(count):
            redis_key = f"DecayItem:bench_{i}"
            members[redis_key] = now - (i * spacing_seconds)
            # Distinct confidences so no member short-circuits to neutral.
            confidences[redis_key] = msgpack.packb(
                {
                    "confidence": (i % 100) / 100.0,
                    "evidence_count": 10,
                    "corroborations": 5,
                    "contradictions": 5,
                },
                use_bin_type=True,
            )
        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        pipe.zadd(self.ZSET_KEY, members)
        pipe.hset(self.CONF_KEY, mapping=confidences)
        pipe.execute()
        return now

    def _timed_eval(self, now):
        start = time.time()
        result = popoto.POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            2,
            self.ZSET_KEY,
            self.CONF_KEY,
            str(now),
            "0.5",
            "10",
            "",  # base_score_field unset: the 0 -> 1 HGET worst case
            "0.5",  # strength s
            "0.5",  # c0
        )
        return result, time.time() - start

    def test_1k_members_modulated(self):
        now = self._load(1000, 3600)
        result, elapsed = self._timed_eval(now)

        assert len(result) == 20  # 10 items * 2 (key + score)
        assert elapsed < 1.0, f"1K modulated took {elapsed:.3f}s (expected < 1s)"

    def test_10k_members_modulated_without_base_score_field(self):
        now = self._load(10000, 360)
        result, elapsed = self._timed_eval(now)

        assert len(result) == 20
        assert elapsed < 5.0, f"10K modulated took {elapsed:.3f}s (expected < 5s)"


# --- Deterministic tie-ordering (issue #448) ---


class TestDecayTieOrdering:
    """Regression tests for deterministic tie-breaking (issue #448).

    Equal-scored members must return in member key (redis_key) ascending
    order, byte-wise, broken inside the Lua script -- independent of
    insertion order, repeatable across runs, and stable across the ``n``
    truncation boundary. Mirrors ``TestBM25TieOrdering`` (#446).
    """

    NAMES = ["tie_a", "tie_b", "tie_c", "tie_d", "tie_e"]

    def setup_method(self):
        DecayItem.delete_all()

    def _plant_tied(self, names=None):
        """Create identical-base members, then plant one shared timestamp.

        Members are created in reversed (non-ascending) key order so a lucky
        insertion order cannot masquerade as a correct tie-break. Base score
        is 1.0 for every member (DecayItem.relevance has no base_score_field),
        so an identical timestamp yields an identical decayed score.
        """
        names = names or self.NAMES
        keys = {}
        for name in reversed(names):
            keys[name] = DecayItem.create(name=name).db_key.redis_key
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        shared_ts = time.time() - 86400  # 1 day ago, identical for every member
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key, {keys[name]: shared_ts for name in names}
        )
        return sorted(keys.values())  # byte-wise ascending == expected order

    def _scores_via_lua(self, n=10):
        """Return the raw decayed score strings the Lua script emits."""
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayItem, "relevance")
        raw = popoto.POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA, 1, ss_key.redis_key, str(time.time()), "0.5", str(n), ""
        )
        decoded = [x.decode() if isinstance(x, bytes) else x for x in raw]
        return [decoded[i + 1] for i in range(0, len(decoded), 2)]

    def test_scores_are_actually_tied(self):
        """All planted members share exactly one score (tie path exercised)."""
        self._plant_tied()
        scores = self._scores_via_lua()
        assert len(scores) == len(self.NAMES)
        assert len(set(scores)) == 1

    def test_tie_order_key_ascending_insertion_independent(self):
        """Tied members return key-ascending regardless of insertion order."""
        expected = self._plant_tied()
        results = DecayItem.query.top_by_decay("relevance", n=10)
        assert [r.db_key.redis_key for r in results] == expected

    def test_repeated_calls_identical(self):
        """The same query returns the identical ordered list every run."""
        self._plant_tied()
        first = [
            r.db_key.redis_key for r in DecayItem.query.top_by_decay("relevance", n=10)
        ]
        assert len(first) == len(self.NAMES)
        for _ in range(9):
            again = [
                r.db_key.redis_key
                for r in DecayItem.query.top_by_decay("relevance", n=10)
            ]
            assert again == first

    def test_deterministic_truncation_at_n(self):
        """With 5 tied members and n=3, exactly the 3 lowest keys return."""
        expected = self._plant_tied()
        results = DecayItem.query.top_by_decay("relevance", n=3)
        assert [r.db_key.redis_key for r in results] == expected[:3]


class TestDecayTieOrderingWithConfidence:
    """Tie-ordering under confidence modulation (#491), alongside #448.

    The class above is the regression oracle and is deliberately left
    untouched. These cases add the two claims modulation introduces:

    - members with NO recorded evidence stay bit-exactly tied, so #448's
      key-ascending tie-break still decides their order;
    - members with DIFFERENT confidence correctly STOP tying.

    Records are aged 30 days: past the ``max(t, 1.0)`` guard, so modulation is
    actually live rather than clamped to 1.0.
    """

    NAMES = ["tie_a", "tie_b", "tie_c", "tie_d", "tie_e"]
    AGED_DAYS = 30

    def setup_method(self):
        DecayConfItem.delete_all()
        DecayConfItem._meta.fields["relevance"]._confidence_modulation_cache.clear()

    def teardown_method(self):
        DecayConfItem.delete_all()

    def _plant_tied(self, names=None):
        """Create members in reversed key order, then plant one shared age."""
        names = names or self.NAMES
        records = {}
        for name in reversed(names):
            records[name] = DecayConfItem.create(name=name)
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayConfItem, "relevance")
        shared_ts = time.time() - 86400 * self.AGED_DAYS
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {records[name].db_key.redis_key: shared_ts for name in names},
        )
        return records

    @staticmethod
    def _set_confidence(record, confidence):
        field = DecayConfItem._meta.fields["certainty"]
        popoto.POPOTO_REDIS_DB.hset(
            field.get_data_hash_key(record, "certainty"),
            record.db_key.redis_key,
            msgpack.packb(
                {
                    "confidence": confidence,
                    "evidence_count": 10,
                    "corroborations": 10,
                    "contradictions": 0,
                },
                use_bin_type=True,
            ),
        )

    @staticmethod
    def _scores_via_lua(n=10):
        """Raw score strings from the modulated EVAL (mirrors query.py:410)."""
        field = DecayConfItem._meta.fields["relevance"]
        ss_key = DecayingSortedField.get_sortedset_db_key(DecayConfItem, "relevance")
        conf_key, s, c0 = confidence_modulation_args(
            DecayConfItem, field, "relevance", filters={}
        )
        assert conf_key != "" and float(s) > 0, "modulation must be live"
        raw = popoto.POPOTO_REDIS_DB.eval(
            DECAY_SCORE_LUA,
            2,
            ss_key.redis_key,
            conf_key,
            str(time.time()),
            str(field.decay_rate),
            str(n),
            "",
            s,
            c0,
        )
        decoded = [x.decode() if isinstance(x, bytes) else x for x in raw]
        return [decoded[i + 1] for i in range(0, len(decoded), 2)]

    def test_no_confidence_members_remain_bit_exactly_tied(self):
        """Zero evidence == c0 for every member, so the tie must survive."""
        self._plant_tied()
        scores = self._scores_via_lua()
        assert len(scores) == len(self.NAMES)
        assert len(set(scores)) == 1

    def test_untouched_tie_order_is_still_key_ascending(self):
        """#448's contract holds unchanged when modulation is on but inert."""
        records = self._plant_tied()
        expected = sorted(r.db_key.redis_key for r in records.values())
        results = DecayConfItem.query.top_by_decay("relevance", n=10)
        assert [r.db_key.redis_key for r in results] == expected

    def test_differing_confidence_members_stop_tying(self):
        """Distinct evidence must produce distinct scores -- no tie left."""
        records = self._plant_tied()
        for name, confidence in zip(self.NAMES, [0.05, 0.25, 0.5, 0.75, 0.95]):
            self._set_confidence(records[name], confidence)

        scores = self._scores_via_lua()
        assert len(set(scores)) == len(self.NAMES), "modulation failed to break ties"
        # Highest confidence ranks first, lowest last.
        results = DecayConfItem.query.top_by_decay("relevance", n=10)
        assert [r.name for r in results] == list(reversed(self.NAMES))

    def test_equal_confidence_members_still_tie_and_break_by_key(self):
        """Same evidence => same score => #448 key-ascending decides."""
        records = self._plant_tied()
        for name in self.NAMES:
            self._set_confidence(records[name], 0.05)

        scores = self._scores_via_lua()
        assert len(set(scores)) == 1
        expected = sorted(r.db_key.redis_key for r in records.values())
        results = DecayConfItem.query.top_by_decay("relevance", n=10)
        assert [r.db_key.redis_key for r in results] == expected

    def test_deterministic_truncation_at_n_with_modulation(self):
        """Truncation stays deterministic across the n boundary."""
        records = self._plant_tied()
        for name in self.NAMES:
            self._set_confidence(records[name], 0.05)
        expected = sorted(r.db_key.redis_key for r in records.values())

        results = DecayConfItem.query.top_by_decay("relevance", n=3)
        assert [r.db_key.redis_key for r in results] == expected[:3]


# --- Export tests ---


class TestExport:
    """Test that DecayingSortedField is accessible from popoto package."""

    def test_importable(self):
        """DecayingSortedField can be imported from popoto."""
        from src.popoto import DecayingSortedField as DSF

        assert DSF is DecayingSortedField
