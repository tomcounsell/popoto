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

import sys
import os
import time
import threading

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
    _default_should_forget,
    _default_should_promote,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db():
    """Flush all test models before and after each test."""
    for model in [TrackedMemory, UntrackedMemory, BadImportanceModel, MissingTierModel]:
        model.delete_all()
    yield
    for model in [TrackedMemory, UntrackedMemory, BadImportanceModel, MissingTierModel]:
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
    """Records below importance floor and idle too long get deleted."""
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
    remaining = TrackedMemory.query.filter(tier="episodic").all()
    keys = [r.key for r in remaining]
    assert record_key not in keys


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
# Batch pagination tests
# ---------------------------------------------------------------------------


def test_tick_batch_pagination():
    """200 records paginate correctly across two TICK_BATCH_SIZE=100 batches."""
    lifecycle = MemoryLifecycle(
        model_class=TrackedMemory,
        importance_field="relevance",
    )
    lifecycle.TICK_BATCH_SIZE = 100
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
