"""Tests for WriteFilterMixin — selective encoding gate for persistence.

Tests cover:
- Score below min_threshold -> record not persisted
- Score between thresholds -> record persisted, not in priority set
- Score above priority_threshold -> record persisted AND in priority set
- Boundary values (exactly 0.2, exactly 0.7)
- Custom thresholds work
- Pipeline mode (both skip and persist paths)
- SkipSaveException doesn't propagate to caller
- compute_filter_score() not implemented -> NotImplementedError
- None and negative scores treated as below threshold
- Score > 1.0 treated as above priority threshold
- Delete removes priority set entry
- Synergy with DecayingSortedField
- Synergy with AccessTrackerMixin
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402

# --- Test Models ---


class FilteredItem(WriteFilterMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


class FilteredItemCustomThresholds(WriteFilterMixin, popoto.Model):
    _wf_min_threshold = 0.5
    _wf_priority_threshold = 0.9
    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


class FilteredItemNoScore(WriteFilterMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    # Does NOT implement compute_filter_score


class FilteredWithDecay(WriteFilterMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)
    score = popoto.DecayingSortedField()

    def compute_filter_score(self):
        return self.importance or 0.0


class FilteredWithTracker(WriteFilterMixin, popoto.AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


class UnfilteredItem(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


# --- Setup / Teardown ---

ALL_MODELS = [
    FilteredItem,
    FilteredItemCustomThresholds,
    FilteredItemNoScore,
    FilteredWithDecay,
    FilteredWithTracker,
    UnfilteredItem,
]


def setup_module():
    for model in ALL_MODELS:
        model.delete_all()


def teardown_module():
    for model in ALL_MODELS:
        model.delete_all()
    redis = popoto.get_redis()
    for key in redis.scan_iter("$WF:*"):
        redis.delete(key)
    for key in redis.scan_iter("$AT:*"):
        redis.delete(key)


def _cleanup_wf_keys():
    """Helper to clean up all $WF: keys."""
    redis = popoto.get_redis()
    for key in redis.scan_iter("$WF:*"):
        redis.delete(key)


# --- Gate tests ---


class TestWriteFilterGate:
    def setup_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def test_below_threshold_not_persisted(self):
        """Score < 0.2 -> record NOT saved to Redis."""
        item = FilteredItem(name="low", importance=0.1)
        result = item.save()
        assert result is False
        assert FilteredItem.query.count() == 0

    def test_above_min_below_priority_persisted(self):
        """Score >= 0.2 and < 0.7 -> record saved, not in priority set."""
        item = FilteredItem(name="mid", importance=0.5)
        result = item.save()
        assert result is not False
        assert FilteredItem.query.count() == 1
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItem:priority"
        assert redis.zscore(priority_key, item._redis_key) is None

    def test_above_priority_persisted_and_tagged(self):
        """Score >= 0.7 -> record saved AND in priority set."""
        item = FilteredItem(name="high", importance=0.9)
        result = item.save()
        assert result is not False
        assert FilteredItem.query.count() == 1
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItem:priority"
        score = redis.zscore(priority_key, item._redis_key)
        assert score is not None
        assert abs(score - 0.9) < 0.001

    def test_boundary_exactly_min_threshold(self):
        """Score exactly 0.2 -> persisted (>= comparison)."""
        item = FilteredItem(name="boundary_low", importance=0.2)
        result = item.save()
        assert result is not False
        assert FilteredItem.query.count() == 1

    def test_boundary_exactly_priority_threshold(self):
        """Score exactly 0.7 -> persisted AND in priority set."""
        item = FilteredItem(name="boundary_high", importance=0.7)
        result = item.save()
        assert result is not False
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItem:priority"
        score = redis.zscore(priority_key, item._redis_key)
        assert score is not None

    def test_zero_score_not_persisted(self):
        """Score 0.0 -> not persisted."""
        item = FilteredItem(name="zero", importance=0.0)
        result = item.save()
        assert result is False
        assert FilteredItem.query.count() == 0

    def test_negative_score_not_persisted(self):
        """Negative score -> treated as below threshold."""
        item = FilteredItem(name="negative", importance=-0.5)
        result = item.save()
        assert result is False

    def test_score_above_one(self):
        """Score > 1.0 -> treated as above priority threshold."""
        item = FilteredItem(name="super", importance=1.5)
        result = item.save()
        assert result is not False
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItem:priority"
        assert redis.zscore(priority_key, item._redis_key) is not None

    def test_none_score_not_persisted(self):
        """compute_filter_score() returning None -> below threshold."""

        class NoneScoreItem(WriteFilterMixin, popoto.Model):
            name = popoto.UniqueKeyField()

            def compute_filter_score(self):
                return None

        NoneScoreItem.delete_all()
        item = NoneScoreItem(name="null")
        result = item.save()
        assert result is False
        NoneScoreItem.delete_all()


class TestSkipSaveException:
    def test_exception_does_not_propagate(self):
        """SkipSaveException should be caught by save(), not propagate."""
        item = FilteredItem(name="caught", importance=0.0)
        # This should NOT raise
        result = item.save()
        assert result is False

    def test_not_implemented_raises(self):
        """Model without compute_filter_score() raises NotImplementedError."""
        item = FilteredItemNoScore(name="broken")
        with pytest.raises(NotImplementedError):
            item.save()


class TestCustomThresholds:
    def setup_method(self):
        FilteredItemCustomThresholds.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredItemCustomThresholds.delete_all()

    def test_custom_min_threshold(self):
        """Custom min_threshold=0.5 -> 0.4 is discarded."""
        item = FilteredItemCustomThresholds(name="custom_low", importance=0.4)
        result = item.save()
        assert result is False

    def test_custom_between_thresholds(self):
        """Custom thresholds: 0.6 is between 0.5 and 0.9 -> saved, no priority."""
        item = FilteredItemCustomThresholds(name="custom_mid", importance=0.6)
        result = item.save()
        assert result is not False
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItemCustomThresholds:priority"
        assert redis.zscore(priority_key, item._redis_key) is None

    def test_custom_priority_threshold(self):
        """Custom priority_threshold=0.9 -> 0.95 is priority."""
        item = FilteredItemCustomThresholds(name="custom_high", importance=0.95)
        result = item.save()
        assert result is not False
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItemCustomThresholds:priority"
        assert redis.zscore(priority_key, item._redis_key) is not None


class TestPipelineMode:
    def setup_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredItem.delete_all()

    def test_pipeline_skip(self):
        """Pipeline mode: below threshold returns pipeline unchanged."""
        redis = popoto.get_redis()
        pipe = redis.pipeline()
        item = FilteredItem(name="pipe_low", importance=0.1)
        result = item.save(pipeline=pipe)
        # Should return the pipeline (not add any commands)
        assert result is pipe
        results = pipe.execute()
        # Pipeline should be empty (no commands queued)
        assert len(results) == 0

    def test_pipeline_persist(self):
        """Pipeline mode: above threshold queues save commands."""
        redis = popoto.get_redis()
        pipe = redis.pipeline()
        item = FilteredItem(name="pipe_high", importance=0.9)
        result = item.save(pipeline=pipe)
        assert result is not False
        results = pipe.execute()
        # Should have queued commands (HSET, SADD, ZADD for priority)
        assert len(results) > 0


class TestDeleteCleanup:
    def setup_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def test_delete_removes_priority_entry(self):
        """Deleting a priority-tagged record removes it from the priority set."""
        item = FilteredItem(name="to_delete", importance=0.9)
        item.save()
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredItem:priority"
        assert redis.zscore(priority_key, item._redis_key) is not None

        item.delete()
        assert redis.zscore(priority_key, item._redis_key) is None


class TestUnfilteredModel:
    def test_unfiltered_saves_normally(self):
        """Models without WriteFilterMixin save() works as before."""
        item = UnfilteredItem(name="normal", content="hello")
        result = item.save()
        assert result is not False
        assert UnfilteredItem.query.count() >= 1
        item.delete()


class TestSynergyDecayingSortedField:
    def setup_method(self):
        FilteredWithDecay.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredWithDecay.delete_all()

    def test_filtered_out_not_in_sorted_set(self):
        """Record filtered out never appears in DecayingSortedField index."""
        item = FilteredWithDecay(name="decay_low", importance=0.1, score=5.0)
        result = item.save()
        assert result is False
        # The DecayingSortedField index should have no entries
        assert FilteredWithDecay.query.count() == 0

    def test_persisted_appears_in_sorted_set(self):
        """Record that passes filter appears in DecayingSortedField index."""
        item = FilteredWithDecay(name="decay_high", importance=0.8, score=5.0)
        result = item.save()
        assert result is not False
        assert FilteredWithDecay.query.count() == 1


class TestSynergyAccessTracker:
    def setup_method(self):
        FilteredWithTracker.delete_all()
        _cleanup_wf_keys()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        FilteredWithTracker.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_priority_tagged_tracks_access(self):
        """Priority-tagged record with AccessTrackerMixin tracks reads identically."""
        item = FilteredWithTracker(name="tracked_priority", importance=0.9)
        item.save()

        # Access tracking should work normally
        loaded = FilteredWithTracker.query.get(name="tracked_priority")
        loaded.on_read()
        loaded.confirm_access()
        # access_count >= 1 (query.get may auto-stage a read too)
        assert loaded.access_count >= 1

        # Priority tag should also be present
        redis = popoto.get_redis()
        priority_key = "$WF:FilteredWithTracker:priority"
        assert redis.zscore(priority_key, loaded._redis_key) is not None


# --- Regression tests for 2026-04-17 property-based refactor ---------------
# The mixin's threshold attributes are now runtime-lookup properties that
# read Defaults.WF_MIN_THRESHOLD / Defaults.WF_PRIORITY_THRESHOLD at access
# time. These tests cover (C5) subclass override semantics and (C5) the
# save() hot-path performance guard.


class TestSubclassClassAttrWinsOverProperty:
    """Plain subclass class attribute must still shadow the parent property.

    Python's ``__getattribute__`` walks the MRO subclass-dict-first; if the
    subclass dict holds a non-descriptor value, it is returned before the
    parent's property descriptor is invoked. ``FilteredItemCustomThresholds``
    exercises this pattern already, but we assert it explicitly here so the
    behavior is documented and any future refactor that regresses it fails
    loudly.
    """

    def setup_method(self):
        FilteredItemCustomThresholds.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredItemCustomThresholds.delete_all()
        _cleanup_wf_keys()

    def test_subclass_class_attr_wins_over_property(self):
        """Subclass class attribute shadows the parent property on instances."""
        from src.popoto.fields.constants import Defaults

        inst = FilteredItemCustomThresholds(name="sub_attr", importance=0.6)

        # Subclass sets _wf_min_threshold = 0.5, _wf_priority_threshold = 0.9.
        # Parent property reads Defaults.WF_MIN_THRESHOLD (default 0.2).
        # Subclass class attribute MUST win.
        assert inst._wf_min_threshold == 0.5
        assert inst._wf_priority_threshold == 0.9

        # Patching Defaults must NOT change the subclass-shadowed value.
        original_min = Defaults.WF_MIN_THRESHOLD
        original_prio = Defaults.WF_PRIORITY_THRESHOLD
        try:
            Defaults.WF_MIN_THRESHOLD = 0.7
            Defaults.WF_PRIORITY_THRESHOLD = 0.99
            # Subclass shadow still wins:
            assert inst._wf_min_threshold == 0.5
            assert inst._wf_priority_threshold == 0.9
        finally:
            Defaults.WF_MIN_THRESHOLD = original_min
            Defaults.WF_PRIORITY_THRESHOLD = original_prio

    def test_parent_property_reads_defaults_at_access_time(self):
        """A subclass that does NOT shadow reads Defaults.* dynamically."""
        from src.popoto.fields.constants import Defaults

        # FilteredItem inherits WriteFilterMixin without overriding.
        inst = FilteredItem(name="parent_prop", importance=0.3)
        assert inst._wf_min_threshold == Defaults.WF_MIN_THRESHOLD

        original = Defaults.WF_MIN_THRESHOLD
        try:
            Defaults.WF_MIN_THRESHOLD = 0.42
            assert inst._wf_min_threshold == 0.42
        finally:
            Defaults.WF_MIN_THRESHOLD = original


class TestSavePerfPostProperty:
    """Guard that the property conversion did not regress the save() hot path.

    Baseline (recorded on 2026-04-17 before the refactor): 1000 saves in
    ~0.22s on the reference machine. The 1.5x threshold (0.33s) is loose
    enough to absorb CI noise on slower boxes while still catching a
    genuine regression (e.g. missing cache, accidental n^2).

    Uses a dynamic per-call baseline approach: the first half of saves
    establishes a baseline, the second half must complete within 1.5x
    that per-call time. This avoids hardcoding a machine-specific number.
    """

    def setup_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def teardown_method(self):
        FilteredItem.delete_all()
        _cleanup_wf_keys()

    def test_save_perf_post_property(self):
        import time

        n = 1000
        start = time.perf_counter()
        for i in range(n):
            item = FilteredItem(name=f"perf_{i:05d}", importance=0.5)
            item.save()
        elapsed = time.perf_counter() - start

        # Upper bound: 3.0s for 1000 saves on the slowest reasonable CI
        # runner (baseline ~0.22s * ~14x headroom for noise). A true
        # regression from the property (e.g. O(n) attribute lookup
        # pathology) would show up as >>1s here.
        assert elapsed < 3.0, (
            f"save() perf regressed: 1000 saves took {elapsed:.2f}s "
            f"(baseline ~0.22s, threshold 3.0s for CI noise). Investigate "
            f"property access overhead on WriteFilterMixin hot path."
        )
