"""Tests for MemoryLifecycle — policy layer for episodic/semantic tier transitions.

Coverage:
- tag_new sets tier
- tick promotes eligible episodic records
- tick does not promote ineligible records
- tick forgets low-importance idle records
- tick does not forget semantic records
- tick is idempotent
- custom should_promote overrides default
- custom should_forget overrides default
- assess returns correct LifecycleState
- empty corpus tick (no-op)
- tick batch pagination (200 records across two batches)
- ConfigurationError guards (missing fields, wrong type)
- AccessTrackerMixin absence degrades gracefully
"""

import pytest

# Test models use AccessTrackerMixin for realistic lifecycle behavior
from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.fields.shortcuts import KeyField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.exceptions import ModelException
from src.popoto.recipes.memory_lifecycle import (
    LifecycleState,
    MemoryLifecycle,
)

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class TrackedMemory(AccessTrackerMixin, popoto.Model):
    """Memory model with full AccessTrackerMixin support."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)
    confidence = ConfidenceField(initial_confidence=0.5)


class UntrackedMemory(popoto.Model):
    """Memory model without AccessTrackerMixin (degrades gracefully)."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)


class BadImportanceModel(popoto.Model):
    """Model with a non-SortedField importance field (for error testing)."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    relevance = popoto.StringField(default="1.0")  # Wrong type


class MissingTierModel(popoto.Model):
    """Model missing tier field (for error testing)."""

    key = popoto.AutoKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)


class LinkedMemory(popoto.Model):
    """Memory with a self-referential Relationship (graph-traversal retrieval)."""

    key = popoto.AutoKeyField()
    tier = KeyField(type=str, default="episodic")
    relevance = DecayingSortedField(decay_rate=0.5)
    confidence = ConfidenceField(initial_confidence=0.5)


# Self-referential Relationship must be registered post-hoc (the class does not
# exist yet inside its own body) — mirrors tests/test_graph_traversal.py.
LinkedMemory.related = popoto.Relationship(model=LinkedMemory, null=True)
LinkedMemory._meta.add_field("related", LinkedMemory.related)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db():
    """Flush all test models before and after each test."""
    models = [
        TrackedMemory,
        UntrackedMemory,
        BadImportanceModel,
        MissingTierModel,
        LinkedMemory,
    ]
    for model in models:
        model.delete_all()
    yield
    for model in models:
        model.delete_all()


@pytest.fixture
def lifecycle():
    """Standard lifecycle for TrackedMemory."""
    return MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )


def _make_record(tier="episodic", confirm_accesses=0):
    """Create a TrackedMemory record and optionally confirm reads."""
    record = TrackedMemory(tier=tier)
    record.save()
    for _ in range(confirm_accesses):
        record.on_read()
        record.confirm_access()
    return record


def _make_old_record(age_seconds, tier="episodic"):
    """Create a record and simulate age by manipulating idle time.

    Since we can't easily backdate Redis TTL, we use a lifecycle with
    low PROMOTION_MIN_AGE_SECONDS to test age-based logic.
    """
    record = TrackedMemory(tier=tier)
    record.save()
    return record


# ---------------------------------------------------------------------------
# Init / validation tests
# ---------------------------------------------------------------------------


def test_init_raises_if_importance_field_missing():
    """ConfigurationError if importance_field not on model."""
    with pytest.raises(ModelException, match="importance_field 'nonexistent'"):
        MemoryLifecycle(
            model_class=TrackedMemory,
            importance_field="nonexistent",
        )


def test_init_raises_if_importance_field_wrong_type():
    """ConfigurationError if importance_field is not a SortedFieldMixin subclass."""
    with pytest.raises(ModelException, match="SortedFieldMixin"):
        MemoryLifecycle(
            model_class=BadImportanceModel,
            importance_field="relevance",
        )


def test_init_raises_if_tier_field_missing():
    """ConfigurationError if tier_field not on model."""
    with pytest.raises(ModelException, match="tier_field 'tier'"):
        MemoryLifecycle(
            model_class=MissingTierModel,
            importance_field="relevance",
        )


def test_init_warns_without_access_tracker(caplog):
    """MemoryLifecycle logs a warning when model lacks AccessTrackerMixin."""
    import logging

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        lifecycle = MemoryLifecycle(
            model_class=UntrackedMemory,
            importance_field="relevance",
        )
    assert lifecycle is not None
    assert "AccessTrackerMixin" in caplog.text


# ---------------------------------------------------------------------------
# tag_new tests
# ---------------------------------------------------------------------------


def test_tag_new_sets_tier(lifecycle):
    """tag_new assigns the correct tier to a new record."""
    record = TrackedMemory()
    record.save()
    lifecycle.tag_new(record, tier="episodic")

    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "episodic"


def test_tag_new_sets_semantic_tier(lifecycle):
    """tag_new can set tier to 'semantic'."""
    record = TrackedMemory()
    record.save()
    lifecycle.tag_new(record, tier="semantic")

    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "semantic"


def test_tag_new_is_idempotent(lifecycle):
    """Calling tag_new multiple times overwrites — no conflict."""
    record = TrackedMemory()
    record.save()
    lifecycle.tag_new(record, tier="episodic")
    lifecycle.tag_new(record, tier="semantic")

    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "semantic"


def test_tag_new_defaults_to_episodic(lifecycle):
    """Default tier for tag_new is 'episodic'."""
    record = TrackedMemory()
    record.save()
    lifecycle.tag_new(record)

    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "episodic"


# ---------------------------------------------------------------------------
# tick() — promotion tests
# ---------------------------------------------------------------------------


def test_tick_promotes_eligible_episodic():
    """Records meeting all promotion criteria get tier='semantic'."""
    # Use a lifecycle with low thresholds so the test record qualifies
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 1
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0

    record = _make_record(tier="episodic", confirm_accesses=2)

    summary = lifecycle.tick()

    assert summary["promoted"] >= 1
    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "semantic"


def test_tick_does_not_promote_ineligible():
    """Records not meeting promotion criteria stay episodic."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    # Require high access count — test record has 0 accesses
    lifecycle.PROMOTION_ACCESS_COUNT = 100
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0

    record = _make_record(tier="episodic", confirm_accesses=0)

    summary = lifecycle.tick()

    assert summary["promoted"] == 0
    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "episodic"


# ---------------------------------------------------------------------------
# tick() — forget tests
# ---------------------------------------------------------------------------


def test_tick_forgets_low_importance_idle():
    """Records below importance floor and idle too long are tombstoned.

    Pre-#491 this asserted a hard delete. Forgetting now tombstones: the
    record still leaves the live corpus, but it is recoverable.
    """
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    # Set thresholds so record qualifies for forget immediately.
    # FORGET_IDLE_SECONDS = -1.0 ensures any idle time (including 0) qualifies
    # because the condition is idle_seconds > FORGET_IDLE_SECONDS.
    lifecycle.FORGET_IMPORTANCE_FLOOR = (
        1.1  # Impossibly high floor (no record can score >= 1.1)
    )
    lifecycle.FORGET_IDLE_SECONDS = -1.0  # Any idle time satisfies idle > -1.0

    record = _make_record(tier="episodic", confirm_accesses=0)
    record_key = record.key

    summary = lifecycle.tick()

    assert summary["forgotten"] >= 1
    assert summary["tombstoned"] == summary["forgotten"]
    remaining = TrackedMemory.query.filter(tier="episodic").all()
    keys = [r.key for r in remaining]
    assert record_key not in keys

    # Tombstoned, not deleted: the death is recorded and reversible.
    assert lifecycle.tombstone_count() == summary["forgotten"]
    assert record._redis_key in [ts.redis_key for ts in lifecycle.list_tombstones()]
    assert lifecycle.restore(record._redis_key) is not None
    assert TrackedMemory.query.get(key=record_key) is not None


def test_tick_does_not_forget_semantic():
    """Semantic records are never deleted by default policy."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    # Set thresholds so any non-semantic record would be forgotten
    lifecycle.FORGET_IMPORTANCE_FLOOR = 1.1
    lifecycle.FORGET_IDLE_SECONDS = 0.0

    record = _make_record(tier="semantic", confirm_accesses=0)
    record_key = record.key

    summary = lifecycle.tick()

    assert summary["forgotten"] == 0
    reloaded = TrackedMemory.query.get(key=record_key)
    assert reloaded is not None
    assert reloaded.tier == "semantic"


# ---------------------------------------------------------------------------
# tick() — idempotency tests
# ---------------------------------------------------------------------------


def test_tick_is_idempotent():
    """Running tick twice produces the same result as once."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 1
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget in this test

    _make_record(tier="episodic", confirm_accesses=2)

    summary1 = lifecycle.tick()
    summary2 = lifecycle.tick()

    # Second tick should promote 0 (already semantic) and forget 0
    assert summary2["promoted"] == 0
    assert summary1["promoted"] >= 1


# ---------------------------------------------------------------------------
# Empty corpus tests
# ---------------------------------------------------------------------------


def test_empty_corpus_tick(lifecycle):
    """tick() on an empty corpus returns zero promoted, zero forgotten."""
    summary = lifecycle.tick()
    assert summary["promoted"] == 0
    assert summary["forgotten"] == 0
    assert "duration_ms" in summary


# ---------------------------------------------------------------------------
# Large corpus single-pass tests
# ---------------------------------------------------------------------------


def test_tick_large_corpus():
    """200 records are all promoted in a single tick() pass."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 1
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget

    # Create 200 episodic records with sufficient accesses
    for _ in range(200):
        r = TrackedMemory(tier="episodic")
        r.save()
        r.on_read()
        r.confirm_access()

    summary = lifecycle.tick()
    assert summary["promoted"] == 200

    # Verify all records moved to semantic
    remaining_episodic = TrackedMemory.query.filter(tier="episodic").all()
    assert len(remaining_episodic) == 0

    semantic_records = TrackedMemory.query.filter(tier="semantic").all()
    assert len(semantic_records) == 200


# ---------------------------------------------------------------------------
# Custom policy callables
# ---------------------------------------------------------------------------


def test_custom_should_promote():
    """Custom should_promote callable overrides default logic."""

    def always_promote(record, lifecycle):
        return "semantic"

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_promote=always_promote,
    )

    record = _make_record(tier="episodic", confirm_accesses=0)

    summary = lifecycle.tick()

    assert summary["promoted"] >= 1
    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "semantic"


def test_custom_should_forget():
    """Custom should_forget callable overrides default logic."""

    def never_forget(record, lifecycle):
        return False

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_forget=never_forget,
    )
    # Would normally forget under extreme thresholds
    lifecycle.FORGET_IMPORTANCE_FLOOR = 1.1
    lifecycle.FORGET_IDLE_SECONDS = 0.0

    record = _make_record(tier="episodic", confirm_accesses=0)

    summary = lifecycle.tick()

    assert summary["forgotten"] == 0
    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded is not None


def test_custom_should_promote_returns_none():
    """Custom should_promote returning None never promotes."""

    def never_promote(record, lifecycle):
        return None

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_promote=never_promote,
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget

    record = _make_record(tier="episodic", confirm_accesses=10)

    summary = lifecycle.tick()

    assert summary["promoted"] == 0
    reloaded = TrackedMemory.query.get(key=record.key)
    assert reloaded.tier == "episodic"


# ---------------------------------------------------------------------------
# assess() tests
# ---------------------------------------------------------------------------


def test_assess_returns_correct_state(lifecycle):
    """assess() returns LifecycleState with expected field values."""
    record = _make_record(tier="episodic", confirm_accesses=0)

    state = lifecycle.assess(record)

    assert isinstance(state, LifecycleState)
    assert state.tier == "episodic"
    assert isinstance(state.access_count, int)
    assert isinstance(state.importance_score, float)
    assert isinstance(state.promotion_eligible, bool)
    assert isinstance(state.forget_eligible, bool)


def test_assess_semantic_not_forget_eligible():
    """assess() on a semantic record shows forget_eligible=False by default."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 1.1
    lifecycle.FORGET_IDLE_SECONDS = 0.0

    record = _make_record(tier="semantic", confirm_accesses=0)
    state = lifecycle.assess(record)

    assert state.tier == "semantic"
    assert state.forget_eligible is False


def test_assess_returns_lifecycle_state_type(lifecycle):
    """assess() return value is LifecycleState dataclass."""
    record = _make_record(tier="episodic")
    state = lifecycle.assess(record)
    assert type(state).__name__ == "LifecycleState"


# ---------------------------------------------------------------------------
# Without AccessTrackerMixin
# ---------------------------------------------------------------------------


def test_untracked_model_tick_works():
    """tick() works correctly on models without AccessTrackerMixin."""
    lifecycle = MemoryLifecycle(
        model_class=UntrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 0  # No access count needed
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget

    record = UntrackedMemory(tier="episodic")
    record.save()

    summary = lifecycle.tick()

    assert summary["promoted"] >= 1


# ---------------------------------------------------------------------------
# Custom should_forget raises — logs warning, continues
# ---------------------------------------------------------------------------


def test_should_forget_raises_logs_warning(caplog):
    """If should_forget raises, tick logs warning and continues."""
    import logging

    call_count = {"n": 0}

    def raises_sometimes(record, lifecycle):
        call_count["n"] += 1
        raise RuntimeError("synthetic error")

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_forget=raises_sometimes,
    )

    _make_record(tier="episodic", confirm_accesses=0)

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        summary = lifecycle.tick()

    assert summary["forgotten"] == 0
    assert call_count["n"] >= 1
    assert "should_forget raised" in caplog.text


def test_should_promote_raises_logs_warning(caplog):
    """If should_promote raises, tick logs warning and continues."""
    import logging

    def raises_promote(record, lifecycle):
        raise RuntimeError("promote error")

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_promote=raises_promote,
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget

    _make_record(tier="episodic", confirm_accesses=0)

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        summary = lifecycle.tick()

    assert summary["promoted"] == 0
    assert "should_promote raised" in caplog.text


# ---------------------------------------------------------------------------
# tick() return type and keys
# ---------------------------------------------------------------------------


def test_tick_returns_dict_with_expected_keys(lifecycle):
    """tick() return value includes promoted, forgotten, duration_ms."""
    summary = lifecycle.tick()
    assert "promoted" in summary
    assert "forgotten" in summary
    assert "duration_ms" in summary
    assert isinstance(summary["promoted"], int)
    assert isinstance(summary["forgotten"], int)
    assert isinstance(summary["duration_ms"], float)


# ---------------------------------------------------------------------------
# Non-tracking reads: tick() must produce 0 new staged AccessTracker entries
# ---------------------------------------------------------------------------


def test_tick_produces_zero_staged_entries():
    """tick() over an AccessTrackerMixin corpus stages 0 new entries.

    This is the primary acceptance criterion for #413: a tick() pass must
    leave the AccessTracker staged-key count and total staged-list length
    identical before and after.
    """
    import popoto as popoto_pkg

    redis = popoto_pkg.get_redis()

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 999  # nothing promotes
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # nothing forgets

    # Seed 10 episodic records with confirmed accesses
    for _ in range(10):
        r = TrackedMemory(tier="episodic")
        r.save()
        r.on_read()
        r.confirm_access()

    # Flush all staged keys that were produced by the seeding above
    for key in redis.scan_iter("$AT:TrackedMemory:staged:*"):
        redis.delete(key)

    # Capture staged-key count and total staged-list length before tick
    staged_before = list(redis.scan_iter("$AT:TrackedMemory:staged:*"))
    total_len_before = sum(redis.llen(k) for k in staged_before)

    lifecycle.tick()

    staged_after = list(redis.scan_iter("$AT:TrackedMemory:staged:*"))
    total_len_after = sum(redis.llen(k) for k in staged_after)

    assert len(staged_after) == len(
        staged_before
    ), f"tick() created {len(staged_after) - len(staged_before)} new staged keys"
    assert (
        total_len_after == total_len_before
    ), f"tick() appended {total_len_after - total_len_before} new staged entries"


def test_tick_produces_zero_staged_entries_with_partition_filters():
    """tick() with partition_filters exercises the .filter(...).no_track().all() path.

    This test is the non-vacuous companion to test_tick_produces_zero_staged_entries.
    The existing test uses no partition_filters, so _tick_pass takes the bare
    query.all() branch — which was already non-tracking before PR #429.

    THIS test supplies partition_filters={"tier": "episodic"} so _tick_pass
    takes the `.filter(**filters).no_track().all()` branch — the actual code
    added by this PR.  It MUST FAIL if .no_track() is removed from that branch
    (that is the whole point: it pins the fix).

    Acceptance criterion: tick() over a partition-filtered AccessTrackerMixin
    corpus stages 0 new entries.
    """
    import popoto as popoto_pkg

    redis = popoto_pkg.get_redis()

    # partition_filters forces _tick_pass into the .filter(**filters).no_track().all()
    # branch — the new code path added by this PR.
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        partition_filters={"tier": "episodic"},
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 999  # nothing promotes
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # nothing forgets

    # Seed 10 episodic records with confirmed accesses
    for _ in range(10):
        r = TrackedMemory(tier="episodic")
        r.save()
        r.on_read()
        r.confirm_access()

    # Flush all staged keys produced by the seeding above
    for key in redis.scan_iter("$AT:TrackedMemory:staged:*"):
        redis.delete(key)

    # Capture baseline before tick
    staged_before = list(redis.scan_iter("$AT:TrackedMemory:staged:*"))
    total_len_before = sum(redis.llen(k) for k in staged_before)

    lifecycle.tick()

    staged_after = list(redis.scan_iter("$AT:TrackedMemory:staged:*"))
    total_len_after = sum(redis.llen(k) for k in staged_after)

    assert len(staged_after) == len(staged_before), (
        f"tick() with partition_filters created "
        f"{len(staged_after) - len(staged_before)} new staged keys "
        f"(expected 0) — remove .no_track() from the 'if filters' branch "
        f"in _tick_pass() to reproduce"
    )
    assert total_len_after == total_len_before, (
        f"tick() with partition_filters appended "
        f"{total_len_after - total_len_before} new staged entries "
        f"(expected 0) — .no_track() is missing from the filtered branch"
    )


# ---------------------------------------------------------------------------
# Single-pass hydration: corpus loaded at most once per tick
# ---------------------------------------------------------------------------


def test_tick_single_pass_hydration():
    """200 records are all promoted in a single-pass tick() (no batch slicing).

    Formerly test_tick_batch_pagination — now verifies that the single-pass
    refactor handles a 200-record corpus correctly, replacing the removed
    TICK_BATCH_SIZE pagination.
    """
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.PROMOTION_ACCESS_COUNT = 1
    lifecycle.PROMOTION_CONFIDENCE_THRESHOLD = 0.0
    lifecycle.PROMOTION_MIN_AGE_SECONDS = 0.0
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # Never forget

    for _ in range(200):
        r = TrackedMemory(tier="episodic")
        r.save()
        r.on_read()
        r.confirm_access()

    summary = lifecycle.tick()
    assert summary["promoted"] == 200

    remaining_episodic = TrackedMemory.query.filter(tier="episodic").all()
    assert len(remaining_episodic) == 0

    semantic_records = TrackedMemory.query.filter(tier="semantic").all()
    assert len(semantic_records) == 200


# ---------------------------------------------------------------------------
# Concurrent-tick re-check-tier-before-delete guard
# ---------------------------------------------------------------------------


def test_forget_guard_skips_record_promoted_to_semantic():
    """Re-check-tier guard: a record promoted-to-semantic between snapshot and
    delete is NOT forgotten.

    Simulates a concurrent promotion by flipping the tier directly in Redis
    (monkeypatching the stale in-memory snapshot), then asserting that the
    forget pass skips the delete.
    """
    import msgpack
    import popoto as popoto_pkg

    redis = popoto_pkg.get_redis()

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    # Tune so record would normally be forgotten (zero importance, zero idle)
    lifecycle.FORGET_IMPORTANCE_FLOOR = 1.1  # floor above max importance
    lifecycle.FORGET_IDLE_SECONDS = 0.0

    record = TrackedMemory(tier="episodic")
    record.save()
    live_key = record._redis_key

    # Simulate a concurrent promotion: directly set tier to "semantic" in Redis
    # before tick() runs its forget evaluation.  The stale in-memory snapshot
    # still says "episodic", but the guard re-reads from Redis and should skip.
    encoded_semantic = msgpack.packb("semantic")
    redis.hset(live_key, "tier", encoded_semantic)

    # Run tick — should NOT delete the record (guard detects tier == semantic)
    lifecycle.tick()

    # Record must still exist (not deleted)
    assert redis.exists(
        live_key
    ), "Re-check-tier guard failed: record was deleted despite tier being semantic in Redis"


def test_forget_guard_skips_absent_key():
    """Re-check-tier guard: a record already deleted (key absent) is skipped
    without raising an exception.
    """
    import popoto as popoto_pkg

    redis = popoto_pkg.get_redis()

    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 1.1  # everything would be forgotten
    lifecycle.FORGET_IDLE_SECONDS = 0.0

    record = TrackedMemory(tier="episodic")
    record.save()
    live_key = record._redis_key

    # Simulate concurrent deletion: remove the key before tick() runs forget
    redis.delete(live_key)

    # tick() should complete without raising and forgotten count should be 0
    # (skip-delete because key is absent — NOT a double-delete error)
    summary = lifecycle.tick()
    assert summary["forgotten"] == 0


# ---------------------------------------------------------------------------
# Confidence-driven forgetting (#491) + tombstoning
# ---------------------------------------------------------------------------


def _set_confidence(record, confidence, evidence_count, field_name="confidence"):
    """Write a confidence payload directly into the ConfidenceField :data hash.

    Bypasses the Bayesian update so policy tests can pin an exact
    (confidence, evidence_count) pair instead of depending on how many
    signals it takes to cross a threshold.
    """
    import msgpack
    import popoto as popoto_pkg

    field = type(record)._meta.fields[field_name]
    data_key = field.get_data_hash_key(record, field_name)
    popoto_pkg.get_redis().hset(
        data_key,
        record.db_key.redis_key,
        msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": evidence_count,
                "corroborations": 0,
                "contradictions": evidence_count,
            },
            use_bin_type=True,
        ),
    )


def _forget_ready_lifecycle(**kwargs):
    """Lifecycle whose importance path can never fire, so only confidence can."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        **kwargs,
    )
    # Floor of 0.0 => importance < floor is impossible (scores are >= 0.0),
    # so the importance branch is disabled and only confidence can forget.
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0
    lifecycle.FORGET_IDLE_SECONDS = -1.0  # any idle time qualifies
    return lifecycle


def test_low_confidence_with_evidence_is_forget_eligible():
    """Confidence below the ceiling with enough evidence forgets on its own."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.05, lifecycle.FORGET_MIN_EVIDENCE)

    assert lifecycle.assess(record).forget_eligible is True


def test_min_evidence_floor_blocks_single_dismissal_burial():
    """One unlucky dismissal must not bury a memory (LIFECYCLE_FORGET_MIN_EVIDENCE)."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.01, 1)

    assert lifecycle.assess(record).forget_eligible is False

    summary = lifecycle.tick()
    assert summary["forgotten"] == 0
    assert TrackedMemory.query.get(key=record.key) is not None


def test_confidence_above_ceiling_is_not_forget_eligible():
    """Plenty of evidence but healthy confidence stays."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.9, 20)

    assert lifecycle.assess(record).forget_eligible is False


def test_semantic_tier_still_protected_from_confidence_forget():
    """Semantic protection outranks the confidence path."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="semantic")
    _set_confidence(record, 0.01, 20)

    assert lifecycle.assess(record).forget_eligible is False
    summary = lifecycle.tick()
    assert summary["forgotten"] == 0
    assert summary["tombstoned"] == 0


def test_model_without_confidence_field_unchanged():
    """A model with no ConfidenceField behaves exactly as before."""
    lifecycle = MemoryLifecycle(
        model_class=UntrackedMemory,
        importance_field="relevance",
    )
    lifecycle.FORGET_IMPORTANCE_FLOOR = 0.0  # importance branch disabled
    lifecycle.FORGET_IDLE_SECONDS = -1.0

    record = UntrackedMemory(tier="episodic")
    record.save()

    assert lifecycle.assess(record).forget_eligible is False
    assert lifecycle.tick()["forgotten"] == 0


def test_custom_should_forget_override_bypasses_confidence():
    """A custom should_forget still wins outright."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
        should_forget=lambda record, lc: False,
    )
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.0, 20)

    assert lifecycle.assess(record).forget_eligible is False
    assert lifecycle.tick()["forgotten"] == 0


def test_real_dismissal_signals_drive_forget_eligibility():
    """End-to-end: repeated dismissals via update_confidence make a record forgettable."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")

    for _ in range(12):
        ConfidenceField.update_confidence(record, "confidence", 0.0)

    data = ConfidenceField.get_confidence_data(record, "confidence")
    assert data["confidence"] < lifecycle.FORGET_CONFIDENCE_CEILING
    assert data["evidence_count"] >= lifecycle.FORGET_MIN_EVIDENCE
    assert lifecycle.assess(record).forget_eligible is True


# --- Tombstoning ----------------------------------------------------------


def test_tick_tombstones_instead_of_deleting():
    """A confidence-forgotten record leaves the corpus but leaves a tombstone."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.05, 10)
    redis_key = record._redis_key

    summary = lifecycle.tick()

    assert summary["tombstoned"] == 1
    assert summary["forgotten"] == 1
    # Gone from the live corpus
    assert TrackedMemory.query.get(key=record.key) is None
    assert record.key not in [r.key for r in TrackedMemory.query.all()]
    # But remembered
    tombstones = lifecycle.list_tombstones()
    assert len(tombstones) == 1
    ts = tombstones[0]
    assert ts.redis_key == redis_key
    assert ts.fingerprint
    assert ts.tier == "episodic"
    assert ts.evidence_count == 10
    assert ts.dismissal_count == 10
    assert ts.confidence_at_death == pytest.approx(0.05)
    assert ts.importance_at_death >= 0.0
    assert ts.tombstoned_at > 0


def test_tombstoned_record_excluded_from_all_retrieval_modes():
    """Every read path must stop returning a tombstoned record."""
    lifecycle = _forget_ready_lifecycle()
    keep = _make_record(tier="episodic")
    _set_confidence(keep, 0.95, 10)
    doomed = _make_record(tier="episodic")
    _set_confidence(doomed, 0.05, 10)
    lifecycle.tick()

    def _keys(records):
        return [r.key for r in records]

    # Each path must still return `keep` — otherwise "doomed is absent" would
    # pass vacuously on an empty result.
    for label, records in [
        ("query.filter", TrackedMemory.query.filter(tier="episodic").all()),
        ("query.all", TrackedMemory.query.all()),
        ("top_by_decay", TrackedMemory.query.top_by_decay("relevance", n=10)),
        (
            "composite_score",
            TrackedMemory.query.composite_score({"relevance": 1.0}, limit=10),
        ),
    ]:
        keys = _keys(records)
        assert keep.key in keys, f"{label} lost the surviving record"
        assert doomed.key not in keys, f"{label} still returns a tombstoned record"

    assert TrackedMemory.query.get(key=doomed.key) is None
    assert TrackedMemory.query.get(key=keep.key) is not None

    # ContextAssembler (push path, no cues)
    from src.popoto.recipes.context_assembler import ContextAssembler

    assembler = ContextAssembler(
        model_class=TrackedMemory,
        score_weights={"relevance": 1.0},
        surfacing_threshold=0.0,
    )
    assembled = _keys(assembler.assemble(query_cues={"tier": "episodic"}).records)
    assert keep.key in assembled
    assert doomed.key not in assembled

    # Lifecycle re-evaluation: a second tick must not see it again
    assert lifecycle.tick()["tombstoned"] == 0
    assert lifecycle.tombstone_count() == 1


def test_tombstoned_record_is_recoverable():
    """restore() brings a tombstoned record back into the live corpus."""
    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.05, 10)
    key = record.key
    redis_key = record._redis_key

    lifecycle.tick()
    assert TrackedMemory.query.get(key=key) is None

    restored = lifecycle.restore(redis_key)

    assert restored is not None
    assert restored.key == key
    reloaded = TrackedMemory.query.get(key=key)
    assert reloaded is not None
    assert reloaded.tier == "episodic"
    # Restoring consumes the tombstone
    assert lifecycle.tombstone_count() == 0
    # And the record is retrievable by decay rank again
    ranked = TrackedMemory.query.top_by_decay("relevance", n=10)
    assert key in [r.key for r in ranked]


def test_restore_unknown_key_returns_none(lifecycle):
    assert lifecycle.restore("TrackedMemory:nope") is None


def test_tombstone_retention_is_bounded():
    """Oldest tombstones age out at LIFECYCLE_TOMBSTONE_RETENTION_LIMIT."""
    lifecycle = _forget_ready_lifecycle()
    lifecycle.TOMBSTONE_RETENTION_LIMIT = 3

    keys = []
    for _ in range(5):
        record = _make_record(tier="episodic")
        _set_confidence(record, 0.05, 10)
        keys.append(record._redis_key)
        lifecycle.tick()

    assert lifecycle.tombstone_count() == 3
    retained = {ts.redis_key for ts in lifecycle.list_tombstones()}
    # Newest three survive; the two oldest aged out
    assert retained == set(keys[2:])
    assert lifecycle.restore(keys[0]) is None


def test_forget_hard_deletes_without_tombstone(lifecycle):
    """Hard deletion remains available as an explicit separate operation."""
    record = _make_record(tier="episodic")
    key = record.key

    assert lifecycle.forget_hard(record) is True

    assert TrackedMemory.query.get(key=key) is None
    assert lifecycle.tombstone_count() == 0


def test_tick_summary_reports_tombstoned_key(lifecycle):
    summary = lifecycle.tick()
    assert "tombstoned" in summary
    assert isinstance(summary["tombstoned"], int)


def test_tombstoned_record_excluded_from_graph_traversal():
    """A tombstoned record must not be reachable by relationship expansion."""
    from src.popoto.recipes import graph_traversal

    target = LinkedMemory(tier="episodic")
    target.save()
    seed = LinkedMemory(tier="episodic")
    seed.related = target
    seed.save()
    seed_key = seed.db_key.redis_key
    target_key = target.db_key.redis_key

    reachable = graph_traversal.traverse(
        LinkedMemory, [seed_key], relationship_field_names=["related"]
    )
    assert target_key in [pk for pk, _ in reachable], "traversal precondition failed"

    lifecycle = MemoryLifecycle(
        model_class=LinkedMemory,
        importance_field="relevance",
    )
    assert lifecycle.tombstone(target) is not None

    reachable = graph_traversal.traverse(
        LinkedMemory, [seed_key], relationship_field_names=["related"]
    )
    assert target_key not in [pk for pk, _ in reachable]
    assert lifecycle.tombstone_count() == 1


def test_partial_tombstone_entry_is_dropped_not_inflated(caplog):
    """A partial msgpack entry is skipped, never inflated into a None-filled Tombstone.

    `_unpack_tombstone_entry` used to validate only `isinstance(entry, dict)`, so a
    foreign or truncated payload under `$TOMB:{Model}:data` produced a Tombstone whose
    non-Optional fields (redis_key, tier, importance_at_death, ...) were all None.
    """
    import logging

    import msgpack
    import popoto as popoto_pkg

    lifecycle = _forget_ready_lifecycle()
    good = _make_record(tier="episodic")
    _set_confidence(good, 0.05, 10)
    lifecycle.tick()
    good_key = good._redis_key
    assert lifecycle.tombstone_count() == 1

    # Inject a partial entry alongside the good one.
    data_key, index_key = lifecycle._tombstone_keys()
    partial_key = "TrackedMemory:partial"
    redis = popoto_pkg.get_redis()
    redis.hset(
        data_key,
        partial_key,
        msgpack.packb(
            {"redis_key": partial_key, "reason": "policy"}, use_bin_type=True
        ),
    )
    redis.zadd(index_key, {partial_key: 1.0})
    assert lifecycle.tombstone_count() == 2

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        tombstones = lifecycle.list_tombstones()
        assert lifecycle.get_tombstone(partial_key) is None
        assert lifecycle.restore(partial_key) is None

    assert [ts.redis_key for ts in tombstones] == [good_key]
    assert any("missing required keys" in r.message for r in caplog.records)
    # And no None-filled shell escaped into the result set.
    for ts in tombstones:
        assert ts.tier is not None
        assert ts.importance_at_death is not None
        assert ts.tombstoned_at is not None


def test_kill_switch_suppresses_confidence_forgetting_after_construction():
    """The deploy-level kill switch stops confidence-driven tombstoning at runtime.

    The switch is re-read on every call, so flipping it AFTER a MemoryLifecycle
    exists must immediately disarm the data-mutating half of #491 — not just decay
    ranking.
    """
    from src.popoto.fields.constants import Defaults

    lifecycle = _forget_ready_lifecycle()
    record = _make_record(tier="episodic")
    _set_confidence(record, 0.01, 20)

    # Precondition: with the switch on, this record is doomed.
    assert lifecycle.confidence_forget_eligible(record) is True
    assert lifecycle.assess(record).forget_eligible is True

    original = Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED
    try:
        Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False

        assert lifecycle.confidence_forget_eligible(record) is False
        assert lifecycle.assess(record).forget_eligible is False

        summary = lifecycle.tick()
        assert summary["forgotten"] == 0
        assert summary["tombstoned"] == 0
        assert TrackedMemory.query.get(key=record.key) is not None
        assert lifecycle.tombstone_count() == 0
    finally:
        Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = original

    # Restoring the switch re-arms it.
    assert lifecycle.confidence_forget_eligible(record) is True
