"""Tests for AccessTrackerMixin — read pattern tracking with staged vs confirmed reads.

Tests cover:
- on_read() stages timestamps (single and batch)
- confirm_access() promotes staged reads, increments count, updates last_accessed
- discard_staged_access() clears staging without affecting confirmed
- access_log capping (stage 150 reads, confirm, verify 100 in log)
- access_count and last_accessed properties (zero-state and after confirm)
- no_track() suppresses on_read
- delete cleanup removes all 3 keys
- models without AccessTrackerMixin are unaffected
- concurrent on_read + confirm (threading-based)
- confirm_access() on unsaved model raises TypeError
- confirm_access() when staging is empty returns 0
"""

import sys
import os
import time
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin

# --- Test Models ---


class TrackedItem(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class TrackedItemSmallCap(AccessTrackerMixin, popoto.Model):
    _max_access_log = 10
    name = popoto.UniqueKeyField()


class UntrackedItem(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


# --- Setup / Teardown ---


def setup_module():
    """Clean up test data before running tests."""
    for model in [TrackedItem, TrackedItemSmallCap, UntrackedItem]:
        model.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    for model in [TrackedItem, TrackedItemSmallCap, UntrackedItem]:
        model.delete_all()
    # Clean up any leftover access tracker keys
    redis = popoto.get_redis()
    for key in redis.scan_iter("$AT:*"):
        redis.delete(key)


# --- on_read() tests ---


class TestOnRead:
    """Test that on_read() stages timestamps."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_on_read_stages_timestamp(self):
        """on_read() pushes a timestamp to the staging list."""
        item = TrackedItem.create(name="read_test")
        before = time.time()
        item.on_read()
        after = time.time()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        staged = redis.lrange(staged_key, 0, -1)
        assert len(staged) == 1
        ts = float(staged[0])
        assert before <= ts <= after

    def test_on_read_multiple_stages(self):
        """Multiple on_read() calls accumulate in staging."""
        item = TrackedItem.create(name="multi_read")
        for _ in range(5):
            item.on_read()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        staged = redis.lrange(staged_key, 0, -1)
        assert len(staged) == 5

    def test_on_read_with_pipeline(self):
        """on_read() works with an existing pipeline."""
        item = TrackedItem.create(name="pipe_read")
        pipe = popoto.get_redis().pipeline()
        item.on_read(pipeline=pipe)
        pipe.execute()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        staged = redis.lrange(staged_key, 0, -1)
        assert len(staged) == 1


# --- confirm_access() tests ---


class TestConfirmAccess:
    """Test that confirm_access() promotes staged reads."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_confirm_promotes_staged(self):
        """confirm_access() moves staged timestamps to access_log."""
        item = TrackedItem.create(name="confirm_test")
        item.on_read()
        item.on_read()
        count = item.confirm_access()

        assert count == 2

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        log_key = f"$AT:TrackedItem:access_log:{item.db_key.redis_key}"

        # Staging should be empty after confirm
        assert redis.llen(staged_key) == 0
        # Log should have the timestamps
        assert redis.llen(log_key) == 2

    def test_confirm_increments_access_count(self):
        """confirm_access() increments access_count in meta hash."""
        item = TrackedItem.create(name="count_test")
        item.on_read()
        item.on_read()
        item.on_read()
        item.confirm_access()

        assert item.access_count == 3

    def test_confirm_updates_last_accessed(self):
        """confirm_access() updates last_accessed to latest timestamp."""
        item = TrackedItem.create(name="last_test")
        before = time.time()
        item.on_read()
        item.confirm_access()
        after = time.time()

        assert item.last_accessed is not None
        assert before <= item.last_accessed <= after

    def test_confirm_empty_staging_returns_zero(self):
        """confirm_access() when staging is empty returns 0."""
        item = TrackedItem.create(name="empty_confirm")
        count = item.confirm_access()
        assert count == 0

    def test_confirm_on_unsaved_raises(self):
        """confirm_access() on unsaved model raises TypeError."""
        item = TrackedItem(name="unsaved")
        with pytest.raises(TypeError):
            item.confirm_access()

    def test_confirm_multiple_rounds(self):
        """Multiple confirm rounds accumulate access_count."""
        item = TrackedItem.create(name="multi_confirm")
        item.on_read()
        item.on_read()
        item.confirm_access()

        item.on_read()
        item.confirm_access()

        assert item.access_count == 3


# --- discard_staged_access() tests ---


class TestDiscardStagedAccess:
    """Test that discard_staged_access() clears staging without affecting confirmed."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_discard_clears_staging(self):
        """discard_staged_access() removes staging list."""
        item = TrackedItem.create(name="discard_test")
        item.on_read()
        item.on_read()
        item.discard_staged_access()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        assert redis.llen(staged_key) == 0

    def test_discard_preserves_confirmed(self):
        """discard_staged_access() does not affect already confirmed data."""
        item = TrackedItem.create(name="discard_preserve")
        item.on_read()
        item.on_read()
        item.confirm_access()

        # Stage more, then discard
        item.on_read()
        item.on_read()
        item.on_read()
        item.discard_staged_access()

        # Original confirmed data should remain
        assert item.access_count == 2

        redis = popoto.get_redis()
        log_key = f"$AT:TrackedItem:access_log:{item.db_key.redis_key}"
        assert redis.llen(log_key) == 2


# --- Access log capping tests ---


class TestAccessLogCapping:
    """Test that access_log is capped at max_access_log."""

    def setup_method(self):
        TrackedItemSmallCap.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItemSmallCap.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_capping_at_max_access_log(self):
        """Stage 20 reads with max_access_log=10, confirm, verify 10 in log."""
        item = TrackedItemSmallCap.create(name="cap_test")
        for _ in range(20):
            item.on_read()
        item.confirm_access()

        redis = popoto.get_redis()
        log_key = f"$AT:TrackedItemSmallCap:access_log:{item.db_key.redis_key}"
        log_len = redis.llen(log_key)
        assert log_len == 10

        # But access_count should reflect all 20
        assert item.access_count == 20

    def test_default_cap_100(self):
        """Default max_access_log is 100."""
        item = TrackedItem.create(name="default_cap")
        for _ in range(150):
            item.on_read()
        item.confirm_access()

        redis = popoto.get_redis()
        log_key = f"$AT:TrackedItem:access_log:{item.db_key.redis_key}"
        log_len = redis.llen(log_key)
        assert log_len == 100

        assert item.access_count == 150


# --- Properties tests ---


class TestProperties:
    """Test access_count and last_accessed properties."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_access_count_default_zero(self):
        """access_count returns 0 for fresh model."""
        item = TrackedItem.create(name="zero_count")
        assert item.access_count == 0

    def test_last_accessed_default_none(self):
        """last_accessed returns None for fresh model."""
        item = TrackedItem.create(name="no_access")
        assert item.last_accessed is None


# --- no_track() tests ---


class TestNoTrack:
    """Test that no_track() suppresses on_read firing from queries."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_no_track_suppresses_on_read(self):
        """no_track() prevents on_read from being called during query."""
        TrackedItem.create(name="no_track_test")

        # Normal get should fire on_read
        TrackedItem.query.get(name="no_track_test")
        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{TrackedItem(name='no_track_test').db_key.redis_key}"
        normal_count = redis.llen(staged_key)

        # no_track get should NOT fire on_read
        TrackedItem.query.filter(name="no_track_test").no_track().all()
        after_no_track_count = redis.llen(staged_key)

        # Count should have increased from normal get but not from no_track
        assert normal_count == 1
        assert after_no_track_count == 1


# --- Delete cleanup tests ---


class TestDeleteCleanup:
    """Test that delete() cleans up all 3 access tracker keys."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_delete_removes_all_keys(self):
        """delete() removes staged, access_log, and meta keys."""
        item = TrackedItem.create(name="delete_test")
        item.on_read()
        item.on_read()
        item.confirm_access()
        item.on_read()  # leave some in staging too

        redis_key = item.db_key.redis_key
        item.delete()

        redis = popoto.get_redis()
        assert redis.exists(f"$AT:TrackedItem:staged:{redis_key}") == 0
        assert redis.exists(f"$AT:TrackedItem:access_log:{redis_key}") == 0
        assert redis.exists(f"$AT:TrackedItem:meta:{redis_key}") == 0


# --- Untracked model tests ---


class TestUntrackedModel:
    """Test that models without AccessTrackerMixin are unaffected."""

    def setup_method(self):
        UntrackedItem.delete_all()

    def teardown_method(self):
        UntrackedItem.delete_all()

    def test_untracked_model_no_on_read(self):
        """Models without AccessTrackerMixin have no on_read method."""
        item = UntrackedItem.create(name="untracked")
        assert not hasattr(item, "on_read")

    def test_untracked_get_no_side_effects(self):
        """Getting an untracked model creates no $AT keys."""
        UntrackedItem.create(name="untracked_get")
        UntrackedItem.query.get(name="untracked_get")

        redis = popoto.get_redis()
        at_keys = redis.keys("$AT:UntrackedItem:*")
        assert len(at_keys) == 0


# --- Concurrent access tests ---


class TestConcurrentAccess:
    """Test concurrent on_read + confirm scenarios."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_concurrent_on_read(self):
        """Multiple threads calling on_read() concurrently."""
        item = TrackedItem.create(name="concurrent_test")
        errors = []

        def do_reads():
            try:
                for _ in range(10):
                    item.on_read()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_reads) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        staged = redis.lrange(staged_key, 0, -1)
        assert len(staged) == 50  # 5 threads * 10 reads

    def test_concurrent_read_and_confirm(self):
        """on_read and confirm_access can run concurrently safely."""
        item = TrackedItem.create(name="concurrent_confirm")
        errors = []

        def do_reads():
            try:
                for _ in range(20):
                    item.on_read()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def do_confirms():
            try:
                for _ in range(5):
                    item.confirm_access()
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=do_reads)
        t2 = threading.Thread(target=do_confirms)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0
        # Final confirm to capture any remaining staged
        item.confirm_access()
        # All 20 reads should be accounted for
        assert item.access_count == 20


# --- Query integration tests ---


class TestQueryIntegration:
    """Test that queries fire on_read for AccessTrackerMixin models."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_get_fires_on_read(self):
        """Query.get() fires on_read for tracked models."""
        TrackedItem.create(name="query_get")
        TrackedItem.query.get(name="query_get")

        redis = popoto.get_redis()
        staged_key = (
            f"$AT:TrackedItem:staged:{TrackedItem(name='query_get').db_key.redis_key}"
        )
        assert redis.llen(staged_key) == 1

    def test_filter_fires_on_read(self):
        """Query.filter().all() fires on_read for tracked models."""
        TrackedItem.create(name="query_filter")
        TrackedItem.query.filter(name="query_filter").all()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{TrackedItem(name='query_filter').db_key.redis_key}"
        assert redis.llen(staged_key) == 1


# --- Export tests ---


class TestSpacingEffect:
    """Test that spaced reads produce different access logs than massed reads."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_spaced_vs_massed_reads(self):
        """Spaced reads over 3 days produce different timestamps than massed reads."""

        spaced = TrackedItem.create(name="spaced")
        massed = TrackedItem.create(name="massed")

        redis = popoto.get_redis()

        # Simulate spaced reads: 3 reads spread across 3 days
        day_seconds = 86400
        base_time = time.time() - 3 * day_seconds
        for day in range(3):
            ts = str(base_time + day * day_seconds)
            redis.rpush(spaced._at_key("staged"), ts)

        # Simulate massed reads: 3 reads all at once
        now = str(time.time())
        for _ in range(3):
            redis.rpush(massed._at_key("staged"), now)

        spaced.confirm_access()
        massed.confirm_access()

        # Both have same access_count
        assert spaced.access_count == 3
        assert massed.access_count == 3

        # But their access logs tell different stories
        spaced_log = redis.lrange(spaced._at_key("access_log"), 0, -1)
        massed_log = redis.lrange(massed._at_key("access_log"), 0, -1)

        spaced_ts = [float(t) for t in spaced_log]
        massed_ts = [float(t) for t in massed_log]

        # Spaced reads have significant time spread
        spaced_spread = max(spaced_ts) - min(spaced_ts)
        massed_spread = max(massed_ts) - min(massed_ts)

        assert spaced_spread > day_seconds  # spread over days
        assert massed_spread < 1.0  # all within 1 second


# --- Synergy test: priority score from access log ---


class TestSynergyWithDecay:
    """Test that confirmed access log enables priority score computation."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_priority_score_from_access_log(self):
        """Confirmed access log timestamps enable B = ln(sum(t_j^(-d))) scoring."""
        import math

        item = TrackedItem.create(name="synergy")
        redis = popoto.get_redis()

        # Stage timestamps at known intervals (1, 2, 3 days ago)
        now = time.time()
        for days_ago in [1, 2, 3]:
            ts = str(now - days_ago * 86400)
            redis.rpush(item._at_key("staged"), ts)

        item.confirm_access()

        # Read back access log and compute spacing-effect priority score
        log = redis.lrange(item._at_key("access_log"), 0, -1)
        timestamps = [float(t) for t in log]

        # B = ln(sum(t_j^(-d))) where t_j = elapsed days, d = decay rate
        d = 0.5
        score_sum = 0.0
        for ts_val in timestamps:
            elapsed_days = max((now - ts_val) / 86400, 0.001)
            score_sum += elapsed_days ** (-d)

        B = math.log(score_sum)
        # Score should be computable and finite
        assert math.isfinite(B)
        # With 3 recent accesses, score should be positive
        assert B > 0


# --- Partition interaction test ---


class TrackedPartitioned(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = popoto.DecayingSortedField(partition_by="category")


class TestPartitionInteraction:
    """Test AccessTracker works correctly with partition_by on DecayingSortedField."""

    def setup_method(self):
        TrackedPartitioned.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedPartitioned.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_partition_by_with_access_tracker(self):
        """AccessTracker keys are per-instance, independent of partition_by."""
        a1 = TrackedPartitioned.create(name="item_a1", category="A")
        a2 = TrackedPartitioned.create(name="item_a2", category="A")
        b1 = TrackedPartitioned.create(name="item_b1", category="B")

        # Query within partition A — should fire on_read for a1, a2 only
        results = TrackedPartitioned.query.filter(category="A").top_by_decay(
            "relevance", n=10
        )
        assert len(results) == 2

        redis = popoto.get_redis()

        # a1 and a2 should have staged reads
        assert redis.llen(a1._at_key("staged")) >= 1
        assert redis.llen(a2._at_key("staged")) >= 1
        # b1 should have no staged reads (not in partition A query)
        assert redis.llen(b1._at_key("staged")) == 0

        # Confirm a1 and verify its AT keys don't interfere with partition index
        a1.confirm_access()
        assert a1.access_count >= 1

        # Partition query still works correctly after confirm
        results_after = TrackedPartitioned.query.filter(category="A").top_by_decay(
            "relevance", n=10
        )
        assert len(results_after) == 2


# --- TTL / staged-key expiry tests ---


class TestStagedTTL:
    """Test that on_read() sets a TTL on the staged key."""

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_on_read_sets_ttl(self):
        """on_read() applies a TTL to the staged key."""
        item = TrackedItem.create(name="ttl_test")
        item.on_read()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        ttl = redis.ttl(staged_key)
        # TTL should be positive and roughly equal to _staged_ttl_seconds (86400)
        assert ttl > 0
        assert ttl <= TrackedItem._staged_ttl_seconds

    def test_on_read_pipeline_sets_ttl(self):
        """on_read() with a pipeline also sets the TTL on the staged key."""
        item = TrackedItem.create(name="ttl_pipe_test")
        pipe = popoto.get_redis().pipeline()
        item.on_read(pipeline=pipe)
        pipe.execute()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        ttl = redis.ttl(staged_key)
        assert ttl > 0
        assert ttl <= TrackedItem._staged_ttl_seconds

    def test_multiple_on_reads_refresh_ttl(self):
        """Repeated on_read() calls keep refreshing the TTL."""
        item = TrackedItem.create(name="ttl_refresh")
        item.on_read()
        item.on_read()
        item.on_read()

        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        ttl = redis.ttl(staged_key)
        # TTL should be near-full after the last on_read refresh
        assert ttl > 0
        assert ttl <= TrackedItem._staged_ttl_seconds

    def test_confirm_on_empty_staging_emits_debug(self, caplog):
        """confirm_access() on empty/expired staging emits a DEBUG log."""
        import logging

        item = TrackedItem.create(name="ttl_debug_log")
        # Do NOT call on_read() — staged key is empty
        with caplog.at_level(logging.DEBUG, logger="POPOTO.AccessTracker"):
            count = item.confirm_access()

        assert count == 0
        assert any(
            "staged key empty/expired" in r.message and "TTL contract" in r.message
            for r in caplog.records
        )

    def test_staged_ttl_default_value(self):
        """_staged_ttl_seconds defaults to 86400 (24h)."""
        assert TrackedItem._staged_ttl_seconds == 86400

    def test_staged_ttl_custom_value(self):
        """Subclasses can override _staged_ttl_seconds."""

        class ShortTTLItem(AccessTrackerMixin, popoto.Model):
            _staged_ttl_seconds = 300  # 5 minutes
            name = popoto.UniqueKeyField()

        ShortTTLItem.delete_all()
        item = ShortTTLItem.create(name="short_ttl")
        item.on_read()

        redis = popoto.get_redis()
        staged_key = f"$AT:ShortTTLItem:staged:{item.db_key.redis_key}"
        ttl = redis.ttl(staged_key)
        assert 0 < ttl <= 300
        ShortTTLItem.delete_all()


# --- Cross-TTL-boundary contract tests ---


class TestCrossTTLBoundary:
    """Verify the staged-read TTL contract: reads confirmed after the staged key
    expires are dropped from access_count / last_accessed.  This is deliberate
    contracted behavior, not a bug (Decision 1 / IN-4).
    """

    def setup_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def teardown_method(self):
        TrackedItem.delete_all()
        redis = popoto.get_redis()
        for key in redis.scan_iter("$AT:*"):
            redis.delete(key)

    def test_confirm_after_staged_key_expires_drops_read(self):
        """A read confirmed after _staged_ttl_seconds is silently dropped.

        BLOCKER per plan (cross-TTL-boundary contract test, IN-4):
        on_read() once, forcibly expire the staged key (simulating elapsed TTL
        without a real sleep), then confirm_access() and assert the read was
        dropped — access_count unchanged, last_accessed not updated.
        """
        item = TrackedItem.create(name="ttl_boundary_test")
        initial_count = item.access_count
        assert initial_count == 0

        # Stage a read
        item.on_read()

        # Force-expire the staged key (simulates TTL elapsing — deterministic)
        redis = popoto.get_redis()
        staged_key = f"$AT:TrackedItem:staged:{item.db_key.redis_key}"
        redis.delete(staged_key)

        # Confirm — staged key is gone, Lua #staged == 0 branch returns 0
        count = item.confirm_access()
        assert count == 0, "Expected 0 from confirm_access() when staged key expired"

        # access_count and last_accessed must be unchanged (read was dropped)
        assert item.access_count == initial_count, (
            f"access_count changed from {initial_count} to {item.access_count} "
            "— dropped read must not increment the count"
        )
        assert (
            item.last_accessed is None
        ), "last_accessed should remain None when the staged read was dropped"

    def test_confirm_within_ttl_window_counts_normally(self):
        """A read confirmed within the TTL window is counted normally.

        Counterpart test: within the TTL window, confirm_access() works
        correctly — no corruption from the TTL mechanism.
        """
        item = TrackedItem.create(name="ttl_within_window")
        initial_count = item.access_count

        item.on_read()
        # Staged key is live — confirm immediately (well within TTL)
        count = item.confirm_access()
        assert count == 1

        assert item.access_count == initial_count + 1
        assert item.last_accessed is not None

    def test_count_parity_with_and_without_forced_ttl(self):
        """access_count is identical for two items when both confirm within
        the TTL window, proving the TTL mechanism does not corrupt the count.

        Regression for Concern 1: TTL-only bound must not corrupt access_count
        for records that confirm within the window.
        """
        item_a = TrackedItem.create(name="parity_a")
        item_b = TrackedItem.create(name="parity_b")

        # Stage 5 reads on each and confirm
        for _ in range(5):
            item_a.on_read()
            item_b.on_read()
        item_a.confirm_access()
        item_b.confirm_access()

        assert item_a.access_count == 5
        assert item_b.access_count == 5
        assert item_a.last_accessed is not None
        assert item_b.last_accessed is not None


# --- Export tests ---


class TestExport:
    """Test that AccessTrackerMixin is accessible from popoto package."""

    def test_importable(self):
        """AccessTrackerMixin can be imported from popoto."""
        from src.popoto import AccessTrackerMixin as ATM

        assert ATM is AccessTrackerMixin
