"""Tests for ObservationProtocol + RecallProposal — outcome-driven memory effects.

Tests cover:
- ObservationProtocol.on_read() delegates to AccessTrackerMixin
- ObservationProtocol.on_surfaced() creates RecallProposal entries
- ObservationProtocol.on_context_used() dispatches correct effects per outcome
- Outcome: acted → touch(), confirm_access(), strengthen_cycle(), resolve_pressure()
- Outcome: dismissed → discard_staged_access(), weaken_cycle(0.8)
- Outcome: deferred → discard_staged_access() only
- Outcome: contradicted → discard_staged_access(), weaken_cycle(0.5)
- RecallProposal: create_batch, resolve, expire_stale, get_pending
- Graceful degradation: DecayingSortedField-only model (no CyclicDecayField)
- Pipeline atomicity (internal pipeline when none provided)
- Synergy: AccessTrackerMixin staging + confirmation/discard
- Synergy: CyclicDecayField amplitude strengthen/weaken
- Edge cases: empty inputs, invalid outcomes, unsaved instances
- Model.strengthen_cycle / weaken_cycle methods directly
"""

import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol, RecallProposal
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class FullMemory(AccessTrackerMixin, popoto.Model):
    """Model with all three features: AccessTracker + CyclicDecayField."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
        pressure_rate=0.1,
    )


class DecayOnlyMemory(AccessTrackerMixin, popoto.Model):
    """Model with DecayingSortedField but NOT CyclicDecayField."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(decay_rate=0.5)


class PlainModel(popoto.Model):
    """Model with no AccessTrackerMixin or decay fields."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class NoPressureMemory(AccessTrackerMixin, popoto.Model):
    """Model with CyclicDecayField but pressure_rate=0."""

    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 3.0, 0)],
        pressure_rate=0.0,
    )


# --- Setup / Teardown ---


def setup_module():
    """Clean up test data before running tests."""
    for model in [FullMemory, DecayOnlyMemory, PlainModel, NoPressureMemory]:
        model.delete_all()
    # Clean up any RecallProposal keys
    for key in POPOTO_REDIS_DB.keys("$RP:*"):
        POPOTO_REDIS_DB.delete(key)


def teardown_module():
    """Clean up test data after running tests."""
    for model in [FullMemory, DecayOnlyMemory, PlainModel, NoPressureMemory]:
        model.delete_all()
    for key in POPOTO_REDIS_DB.keys("$RP:*"):
        POPOTO_REDIS_DB.delete(key)


def _cleanup_proposals():
    """Clean up RecallProposal keys between tests."""
    for key in POPOTO_REDIS_DB.keys("$RP:*"):
        POPOTO_REDIS_DB.delete(key)


# --- Helper Functions ---


def _get_cycles(instance, field_name="relevance"):
    """Read current cycle amplitudes from Redis."""
    field = instance._meta.fields[field_name]
    cycles_hash_key = field.get_cycles_hash_key(instance, field_name)
    member_key = instance._redis_key or instance.db_key.redis_key
    raw = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)
    if not raw:
        return []
    return msgpack.unpackb(raw, raw=False)


def _get_staged_count(instance):
    """Get the number of staged reads for an instance."""
    staged_key = instance._at_key("staged")
    return POPOTO_REDIS_DB.llen(staged_key)


# =========================================================================
# ObservationProtocol.on_read()
# =========================================================================


class TestOnRead:
    def test_delegates_to_access_tracker(self):
        """on_read() should stage a read via AccessTrackerMixin."""
        item = FullMemory(name="read-test-1", content="hello")
        item.save()

        ObservationProtocol.on_read(item)
        assert _get_staged_count(item) == 1

        ObservationProtocol.on_read(item)
        assert _get_staged_count(item) == 2

    def test_noop_without_access_tracker(self):
        """on_read() should be a no-op for models without AccessTrackerMixin."""
        item = PlainModel(name="read-test-plain", content="hello")
        item.save()
        # Should not raise
        ObservationProtocol.on_read(item)


# =========================================================================
# ObservationProtocol.on_surfaced()
# =========================================================================


class TestOnSurfaced:
    def setup_method(self):
        _cleanup_proposals()

    def test_creates_proposals(self):
        """on_surfaced() should create pending RecallProposal entries."""
        m1 = FullMemory(name="surf-1", content="a")
        m1.save()
        m2 = FullMemory(name="surf-2", content="b")
        m2.save()

        ObservationProtocol.on_surfaced([m1, m2], reason="proactive")

        pending = RecallProposal.get_pending(FullMemory)
        assert len(pending) == 2
        keys = {p[0] for p in pending}
        assert m1.db_key.redis_key in keys
        assert m2.db_key.redis_key in keys

    def test_empty_instances_noop(self):
        """on_surfaced() with empty list should be a no-op."""
        ObservationProtocol.on_surfaced([], reason="test")
        pending = RecallProposal.get_pending(FullMemory)
        assert len(pending) == 0

    def test_partition_isolation(self):
        """Proposals with different partitions should be isolated."""
        m1 = FullMemory(name="surf-part-1", content="a")
        m1.save()

        ObservationProtocol.on_surfaced([m1], partition="agent-1")
        ObservationProtocol.on_surfaced([m1], partition="agent-2")

        p1 = RecallProposal.get_pending(FullMemory, partition="agent-1")
        p2 = RecallProposal.get_pending(FullMemory, partition="agent-2")
        assert len(p1) == 1
        assert len(p2) == 1


# =========================================================================
# ObservationProtocol.on_context_used() — Outcome: acted
# =========================================================================


class TestActedOutcome:
    def test_touch_called(self):
        """Acted outcome should refresh the decay timestamp via touch()."""
        item = FullMemory(name="acted-touch", content="important")
        item.save()
        original_score = item.relevance

        time.sleep(0.05)
        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # The sorted set score should be updated (newer timestamp)
        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            item, "relevance"
        ).redis_key
        new_score = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)
        assert new_score > original_score

    def test_confirm_access(self):
        """Acted outcome should confirm staged reads."""
        item = FullMemory(name="acted-confirm", content="data")
        item.save()

        # Stage some reads
        item.on_read()
        item.on_read()
        assert _get_staged_count(item) == 2

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # Staged reads should be confirmed (staging cleared)
        assert _get_staged_count(item) == 0
        assert item.access_count == 2

    def test_strengthen_cycles(self):
        """Acted outcome should strengthen cycle amplitudes by 1.2x."""
        item = FullMemory(name="acted-cycles", content="cyclical")
        item.save()

        original_cycles = _get_cycles(item)
        original_amp = original_cycles[0][1]

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        new_cycles = _get_cycles(item)
        expected_amp = original_amp * 1.2
        assert abs(new_cycles[0][1] - expected_amp) < 0.001

    def test_resolve_pressure(self):
        """Acted outcome should discharge pressure."""
        item = FullMemory(name="acted-pressure", content="urgent")
        item.save()

        # Manually backdate pressure to simulate buildup
        field = item._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(item, "relevance")
        member_key = item.db_key.redis_key
        old_pressure = {"rate": 0.1, "last_resolved": time.time() - 86400}
        POPOTO_REDIS_DB.hset(pressure_hash_key, member_key, msgpack.packb(old_pressure))

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # Pressure should be resolved (last_resolved updated to ~now)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        pressure = msgpack.unpackb(raw, raw=False)
        assert abs(pressure["last_resolved"] - time.time()) < 2.0


# =========================================================================
# ObservationProtocol.on_context_used() — Outcome: dismissed
# =========================================================================


class TestDismissedOutcome:
    def test_discard_staged_reads(self):
        """Dismissed outcome should discard staged reads."""
        item = FullMemory(name="dismissed-reads", content="nah")
        item.save()

        item.on_read()
        item.on_read()
        assert _get_staged_count(item) == 2

        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # Staged reads should be discarded
        assert _get_staged_count(item) == 0
        # But access_count should NOT increase (reads were discarded, not confirmed)
        assert item.access_count == 0

    def test_weaken_cycles(self):
        """Dismissed outcome should weaken cycle amplitudes by 0.8x."""
        item = FullMemory(name="dismissed-cycles", content="rejected")
        item.save()

        original_cycles = _get_cycles(item)
        original_amp = original_cycles[0][1]

        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        new_cycles = _get_cycles(item)
        expected_amp = original_amp * 0.8
        assert abs(new_cycles[0][1] - expected_amp) < 0.001

    def test_no_touch(self):
        """Dismissed outcome should NOT touch the decay timestamp."""
        item = FullMemory(name="dismissed-notouch", content="skip")
        item.save()
        original_score = item.relevance

        time.sleep(0.05)
        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            item, "relevance"
        ).redis_key
        new_score = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)
        assert new_score == original_score


# =========================================================================
# ObservationProtocol.on_context_used() — Outcome: deferred
# =========================================================================


class TestDeferredOutcome:
    def test_discard_staged_reads(self):
        """Deferred outcome should discard staged reads."""
        item = FullMemory(name="deferred-reads", content="later")
        item.save()

        item.on_read()
        assert _get_staged_count(item) == 1

        outcome_map = {item.db_key.redis_key: "deferred"}
        ObservationProtocol.on_context_used([item], outcome_map)

        assert _get_staged_count(item) == 0
        assert item.access_count == 0

    def test_no_cycle_changes(self):
        """Deferred outcome should NOT change cycle amplitudes."""
        item = FullMemory(name="deferred-cycles", content="meh")
        item.save()

        original_cycles = _get_cycles(item)
        original_amp = original_cycles[0][1]

        outcome_map = {item.db_key.redis_key: "deferred"}
        ObservationProtocol.on_context_used([item], outcome_map)

        new_cycles = _get_cycles(item)
        assert abs(new_cycles[0][1] - original_amp) < 0.001

    def test_no_touch(self):
        """Deferred outcome should NOT touch the decay timestamp."""
        item = FullMemory(name="deferred-notouch", content="skip")
        item.save()
        original_score = item.relevance

        time.sleep(0.05)
        outcome_map = {item.db_key.redis_key: "deferred"}
        ObservationProtocol.on_context_used([item], outcome_map)

        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            item, "relevance"
        ).redis_key
        new_score = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)
        assert new_score == original_score

    def test_default_outcome(self):
        """Instances not in outcome_map should default to deferred."""
        item = FullMemory(name="deferred-default", content="forgotten")
        item.save()

        original_cycles = _get_cycles(item)
        original_amp = original_cycles[0][1]

        item.on_read()
        # Empty outcome map → all default to deferred
        ObservationProtocol.on_context_used([item], {})

        assert _get_staged_count(item) == 0
        new_cycles = _get_cycles(item)
        assert abs(new_cycles[0][1] - original_amp) < 0.001


# =========================================================================
# ObservationProtocol.on_context_used() — Outcome: contradicted
# =========================================================================


class TestContradictedOutcome:
    def test_discard_staged_reads(self):
        """Contradicted outcome should discard staged reads."""
        item = FullMemory(name="contradicted-reads", content="wrong")
        item.save()

        item.on_read()
        assert _get_staged_count(item) == 1

        outcome_map = {item.db_key.redis_key: "contradicted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        assert _get_staged_count(item) == 0
        assert item.access_count == 0

    def test_aggressive_weaken_cycles(self):
        """Contradicted outcome should weaken cycle amplitudes by 0.5x (aggressive)."""
        item = FullMemory(name="contradicted-cycles", content="false")
        item.save()

        original_cycles = _get_cycles(item)
        original_amp = original_cycles[0][1]

        outcome_map = {item.db_key.redis_key: "contradicted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        new_cycles = _get_cycles(item)
        expected_amp = original_amp * 0.5
        assert abs(new_cycles[0][1] - expected_amp) < 0.001

    def test_more_aggressive_than_dismissed(self):
        """Contradicted should weaken more than dismissed (0.5 vs 0.8)."""
        m1 = FullMemory(name="compare-dismissed", content="a")
        m1.save()
        m2 = FullMemory(name="compare-contradicted", content="b")
        m2.save()

        orig1 = _get_cycles(m1)[0][1]
        orig2 = _get_cycles(m2)[0][1]
        assert orig1 == orig2  # Same starting amplitude

        ObservationProtocol.on_context_used([m1], {m1.db_key.redis_key: "dismissed"})
        ObservationProtocol.on_context_used([m2], {m2.db_key.redis_key: "contradicted"})

        amp1 = _get_cycles(m1)[0][1]
        amp2 = _get_cycles(m2)[0][1]
        assert amp2 < amp1  # contradicted weakens more


# =========================================================================
# Graceful Degradation
# =========================================================================


class TestGracefulDegradation:
    def test_decay_only_model_acted(self):
        """Model with DecayingSortedField but no CyclicDecayField still gets touch() on acted."""
        item = DecayOnlyMemory(name="decay-only-acted", content="simple")
        item.save()
        original_score = item.relevance

        item.on_read()
        time.sleep(0.05)

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # Should touch (refresh timestamp)
        ss_key = DecayingSortedField.get_partitioned_sortedset_db_key(
            item, "relevance"
        ).redis_key
        new_score = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)
        assert new_score > original_score

        # Should confirm access
        assert item.access_count == 1

    def test_decay_only_model_dismissed(self):
        """DecayingSortedField-only model: dismissed should discard reads, skip cycle ops."""
        item = DecayOnlyMemory(name="decay-only-dismissed", content="nope")
        item.save()

        item.on_read()
        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        assert _get_staged_count(item) == 0
        assert item.access_count == 0

    def test_plain_model_noop(self):
        """Model with no tracking features: all outcomes should be no-ops."""
        item = PlainModel(name="plain-acted", content="nothing")
        item.save()

        # Should not raise for any outcome
        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

    def test_no_pressure_model_acted(self):
        """CyclicDecayField with pressure_rate=0: acted should skip resolve_pressure()."""
        item = NoPressureMemory(name="nopressure-acted")
        item.save()

        outcome_map = {item.db_key.redis_key: "acted"}
        # Should not raise (resolve_pressure is skipped when pressure_rate=0)
        ObservationProtocol.on_context_used([item], outcome_map)


# =========================================================================
# RecallProposal
# =========================================================================


class TestRecallProposal:
    def setup_method(self):
        _cleanup_proposals()

    def test_create_and_get_pending(self):
        """create_batch should add proposals; get_pending should return them."""
        m1 = FullMemory(name="rp-create-1", content="a")
        m1.save()
        m2 = FullMemory(name="rp-create-2", content="b")
        m2.save()

        before = time.time()
        RecallProposal.create_batch([m1, m2])
        after = time.time()

        pending = RecallProposal.get_pending(FullMemory)
        assert len(pending) == 2

        for member_key, score in pending:
            assert before <= score <= after

    def test_resolve_removes_proposal(self):
        """resolve should remove the proposal from pending."""
        m1 = FullMemory(name="rp-resolve-1", content="a")
        m1.save()

        RecallProposal.create_batch([m1])
        assert len(RecallProposal.get_pending(FullMemory)) == 1

        RecallProposal.resolve(m1, "acted")
        assert len(RecallProposal.get_pending(FullMemory)) == 0

    def test_resolve_idempotent(self):
        """resolve on already-resolved proposal should return 0."""
        m1 = FullMemory(name="rp-resolve-idem", content="a")
        m1.save()

        RecallProposal.create_batch([m1])
        RecallProposal.resolve(m1, "acted")
        result = RecallProposal.resolve(m1, "acted")
        assert result == 0

    def test_expire_stale(self):
        """expire_stale should remove proposals older than TTL."""
        m1 = FullMemory(name="rp-expire-1", content="a")
        m1.save()
        m2 = FullMemory(name="rp-expire-2", content="b")
        m2.save()

        # Create proposals
        RecallProposal.create_batch([m1, m2])

        # With a very short TTL, they should expire immediately
        time.sleep(0.05)
        expired = RecallProposal.expire_stale(FullMemory, ttl=0.01)
        assert len(expired) == 2
        assert len(RecallProposal.get_pending(FullMemory)) == 0

    def test_expire_stale_empty(self):
        """expire_stale with no pending proposals should return empty list."""
        expired = RecallProposal.expire_stale(FullMemory)
        assert expired == []

    def test_partition_pending_key(self):
        """Pending keys should be partitioned correctly."""
        key = RecallProposal._pending_key(FullMemory, partition="agent-x")
        assert "$RP:FullMemory:pending:agent-x" == key

    def test_default_partition(self):
        """Default partition should be 'default'."""
        key = RecallProposal._pending_key(FullMemory)
        assert "$RP:FullMemory:pending:default" == key


# =========================================================================
# Model.strengthen_cycle / weaken_cycle
# =========================================================================


class TestCycleMethods:
    def test_strengthen_cycle(self):
        """strengthen_cycle should multiply amplitudes by factor."""
        item = FullMemory(name="strengthen-1", content="boost")
        item.save()

        original_amp = _get_cycles(item)[0][1]
        item.strengthen_cycle("relevance", factor=1.5)
        new_amp = _get_cycles(item)[0][1]
        assert abs(new_amp - original_amp * 1.5) < 0.001

    def test_weaken_cycle(self):
        """weaken_cycle should multiply amplitudes by factor."""
        item = FullMemory(name="weaken-1", content="fade")
        item.save()

        original_amp = _get_cycles(item)[0][1]
        item.weaken_cycle("relevance", factor=0.6)
        new_amp = _get_cycles(item)[0][1]
        assert abs(new_amp - original_amp * 0.6) < 0.001

    def test_amplitude_clamping_max(self):
        """Amplitudes should be clamped at 100.0."""
        item = FullMemory(name="clamp-max", content="huge")
        item.save()

        # Strengthen many times to exceed 100.0
        for _ in range(20):
            item.strengthen_cycle("relevance", factor=2.0)

        cycles = _get_cycles(item)
        assert cycles[0][1] <= 100.0

    def test_amplitude_clamping_min(self):
        """Amplitudes below 0.01 should snap to 0.0."""
        item = FullMemory(name="clamp-min", content="tiny")
        item.save()

        # Weaken many times to approach zero
        for _ in range(50):
            item.weaken_cycle("relevance", factor=0.5)

        cycles = _get_cycles(item)
        assert cycles[0][1] == 0.0

    def test_factor_1_0_noop(self):
        """strengthen_cycle with factor=1.0 should not change amplitudes."""
        item = FullMemory(name="factor-noop", content="same")
        item.save()

        original_amp = _get_cycles(item)[0][1]
        item.strengthen_cycle("relevance", factor=1.0)
        new_amp = _get_cycles(item)[0][1]
        assert abs(new_amp - original_amp) < 0.001

    def test_wrong_field_type_raises(self):
        """strengthen_cycle on non-CyclicDecayField should raise TypeError."""
        item = DecayOnlyMemory(name="wrong-field", content="error")
        item.save()

        with pytest.raises(TypeError, match="CyclicDecayField"):
            item.strengthen_cycle("relevance")

    def test_unsaved_instance_raises(self):
        """strengthen_cycle on unsaved instance should raise TypeError."""
        item = FullMemory(name="unsaved-cycle")

        with pytest.raises(TypeError, match="unsaved"):
            item.strengthen_cycle("relevance")

    def test_nonexistent_field_raises(self):
        """strengthen_cycle on nonexistent field should raise AttributeError."""
        item = FullMemory(name="nofield-cycle", content="x")
        item.save()

        with pytest.raises(AttributeError, match="no field"):
            item.strengthen_cycle("nonexistent")

    def test_no_cycles_stored(self):
        """strengthen_cycle when no cycles stored should return empty list."""
        item = NoPressureMemory(name="nocycles-stored")
        item.save()

        # NoPressureMemory has cycles in its field def, so cycles ARE stored.
        # Let's test with a model that has cycles stored and then delete them.
        field = item._meta.fields["relevance"]
        cycles_hash_key = field.get_cycles_hash_key(item, "relevance")
        member_key = item.db_key.redis_key
        POPOTO_REDIS_DB.hdel(cycles_hash_key, member_key)

        result = item.strengthen_cycle("relevance", factor=1.5)
        assert result == []

    def test_pipeline_support(self):
        """strengthen_cycle with pipeline should return pipeline."""
        item = FullMemory(name="pipe-cycle", content="piped")
        item.save()

        pipe = POPOTO_REDIS_DB.pipeline()
        result = item.strengthen_cycle("relevance", factor=1.2, pipeline=pipe)
        assert result is pipe
        pipe.execute()


# =========================================================================
# Edge Cases and Error Handling
# =========================================================================


class TestEdgeCases:
    def test_invalid_outcome_raises(self):
        """on_context_used with invalid outcome string should raise ValueError."""
        item = FullMemory(name="invalid-outcome", content="bad")
        item.save()

        with pytest.raises(ValueError, match="Invalid outcome"):
            ObservationProtocol.on_context_used(
                [item], {item.db_key.redis_key: "unknown"}
            )

    def test_empty_instances_noop(self):
        """on_context_used with empty instances should be a no-op."""
        ObservationProtocol.on_context_used([], {"key": "acted"})

    def test_empty_outcome_map_defaults_deferred(self):
        """on_context_used with empty outcome_map: all instances get deferred."""
        item = FullMemory(name="empty-map", content="default")
        item.save()

        item.on_read()
        ObservationProtocol.on_context_used([item], {})

        # Deferred: staged reads discarded
        assert _get_staged_count(item) == 0

    def test_multiple_instances_mixed_outcomes(self):
        """on_context_used with multiple instances and mixed outcomes."""
        m1 = FullMemory(name="mix-acted", content="a")
        m1.save()
        m2 = FullMemory(name="mix-dismissed", content="b")
        m2.save()
        m3 = FullMemory(name="mix-deferred", content="c")
        m3.save()

        # Stage reads for all
        m1.on_read()
        m2.on_read()
        m3.on_read()

        outcome_map = {
            m1.db_key.redis_key: "acted",
            m2.db_key.redis_key: "dismissed",
            # m3 not in map → defaults to deferred
        }
        ObservationProtocol.on_context_used([m1, m2, m3], outcome_map)

        # m1 (acted): confirmed
        assert m1.access_count == 1
        # m2 (dismissed): discarded
        assert m2.access_count == 0
        # m3 (deferred): discarded
        assert m3.access_count == 0


# =========================================================================
# Synergy Tests (from acceptance criteria)
# =========================================================================


class TestSynergy:
    def test_decaying_sorted_field_plus_access_tracker(self):
        """Retrieve 5 memories, report 2 acted and 3 deferred.

        Verify only the 2 acted memories get touch() and read confirmation.
        Verify the 3 deferred memories retain original timestamps.
        """
        memories = []
        for i in range(5):
            m = FullMemory(name=f"synergy-dat-{i}", content=f"mem-{i}")
            m.save()
            m.on_read()
            memories.append(m)

        original_scores = {}
        for m in memories:
            ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
                m, "relevance"
            ).redis_key
            original_scores[m.db_key.redis_key] = POPOTO_REDIS_DB.zscore(
                ss_key, m.db_key.redis_key
            )

        time.sleep(0.05)

        # 2 acted, 3 deferred
        outcome_map = {
            memories[0].db_key.redis_key: "acted",
            memories[1].db_key.redis_key: "acted",
            # memories[2], [3], [4] default to deferred
        }
        ObservationProtocol.on_context_used(memories, outcome_map)

        # Acted: touch called (score updated), reads confirmed
        for m in memories[:2]:
            ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
                m, "relevance"
            ).redis_key
            new_score = POPOTO_REDIS_DB.zscore(ss_key, m.db_key.redis_key)
            assert new_score > original_scores[m.db_key.redis_key]
            assert m.access_count == 1

        # Deferred: score unchanged, reads discarded (not confirmed)
        for m in memories[2:]:
            ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
                m, "relevance"
            ).redis_key
            new_score = POPOTO_REDIS_DB.zscore(ss_key, m.db_key.redis_key)
            assert new_score == original_scores[m.db_key.redis_key]
            assert m.access_count == 0
            assert _get_staged_count(m) == 0

    def test_cyclic_decay_field_synergy(self):
        """Record surfaces → acted → cycles strengthened.
        Same record surfaces → dismissed → cycles weakened, no touch."""
        item = FullMemory(name="synergy-cyclic", content="recurring")
        item.save()

        original_amp = _get_cycles(item)[0][1]

        # First: acted → strengthen
        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)
        amp_after_acted = _get_cycles(item)[0][1]
        assert amp_after_acted > original_amp  # strengthened

        # Get score after acted
        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            item, "relevance"
        ).redis_key
        score_after_acted = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)

        time.sleep(0.05)

        # Second: dismissed → weaken, no touch
        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)
        amp_after_dismissed = _get_cycles(item)[0][1]
        assert amp_after_dismissed < amp_after_acted  # weakened

        # Score should NOT have changed (no touch on dismissed)
        score_after_dismissed = POPOTO_REDIS_DB.zscore(ss_key, item.db_key.redis_key)
        assert score_after_dismissed == score_after_acted

    def test_proposal_resolved_on_context_used(self):
        """on_context_used should resolve pending proposals."""
        m1 = FullMemory(name="synergy-proposal", content="tracked")
        m1.save()

        # Surface creates proposal
        ObservationProtocol.on_surfaced([m1])
        assert len(RecallProposal.get_pending(FullMemory)) == 1

        # Context used resolves proposal
        outcome_map = {m1.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([m1], outcome_map)
        assert len(RecallProposal.get_pending(FullMemory)) == 0


# =========================================================================
# "used" outcome — confirms staged reads, auto-resolves, no confidence/cycle
# (#352 Tier 2)
# =========================================================================

from src.popoto.fields.observation import VALID_OUTCOMES  # noqa: E402
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402


class UsedOutcomeMemory(AccessTrackerMixin, PredictionLedgerMixin, popoto.Model):
    """Model with AccessTracker + PredictionLedger + ConfidenceField + CyclicDecay.

    Lets us assert that "used" confirms the staged read (AccessTracker),
    auto-resolves predictions (PredictionLedger), but does NOT touch
    ConfidenceField or CyclicDecayField amplitudes.
    """

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
        pressure_rate=0.0,
    )
    certainty = ConfidenceField(initial_confidence=0.5)


class TestUsedOutcome:
    """Tests for the 'used' outcome in ObservationProtocol (#352)."""

    def _cleanup(self):
        UsedOutcomeMemory.delete_all()
        for pattern in ("$PL:UsedOutcomeMemory:*", "$ConfidencF:UsedOutcomeMemory:*"):
            for key in POPOTO_REDIS_DB.keys(pattern):
                POPOTO_REDIS_DB.delete(key)

    def test_used_in_valid_outcomes(self):
        """'used' is a recognized outcome alongside the original four."""
        assert "used" in VALID_OUTCOMES
        # The original four must still be present — no breaking change.
        for orig in ("acted", "dismissed", "deferred", "contradicted"):
            assert orig in VALID_OUTCOMES

    def test_used_confirms_staged_read(self):
        """'used' calls confirm_access -> access_count increments."""
        self._cleanup()
        try:
            m = UsedOutcomeMemory(name="used-confirm", content="c")
            m.save()
            # Stage a read (on_read stages to AccessTracker)
            ObservationProtocol.on_read(m)
            assert m.access_count == 0  # not yet confirmed

            # "used" should confirm the staged read
            outcome_map = {m.db_key.redis_key: "used"}
            ObservationProtocol.on_context_used([m], outcome_map)

            assert (
                m.access_count >= 1
            ), "'used' must confirm staged reads (distinction from 'deferred')"
        finally:
            self._cleanup()

    def test_deferred_does_not_confirm_staged_read(self):
        """Contrast: 'deferred' discards staged reads; access_count stays 0."""
        self._cleanup()
        try:
            m = UsedOutcomeMemory(name="deferred-discard", content="c")
            m.save()
            ObservationProtocol.on_read(m)
            assert m.access_count == 0

            outcome_map = {m.db_key.redis_key: "deferred"}
            ObservationProtocol.on_context_used([m], outcome_map)

            assert (
                m.access_count == 0
            ), "'deferred' must discard staged reads (not confirm)"
        finally:
            self._cleanup()

    def test_used_auto_resolves_prediction(self):
        """'used' routes through PredictionLedger.auto_resolve with PL_AUTO_RESOLVE_USED."""
        self._cleanup()
        try:
            m = UsedOutcomeMemory(name="used-prediction", content="c")
            m.save()
            PredictionLedgerMixin.record_prediction(m, predicted={"x": 1.0})

            outcome_map = {m.db_key.redis_key: "used"}
            ObservationProtocol.on_context_used([m], outcome_map)

            data = PredictionLedgerMixin.get_prediction_data(m)
            assert data is not None
            assert data["resolved"] is True
            assert data["resolution_mode"] == "observed"
            # Error value should be the PL_AUTO_RESOLVE_USED default (0.3)
            from src.popoto.fields.constants import Defaults

            assert abs(data["prediction_error"] - Defaults.PL_AUTO_RESOLVE_USED) < 1e-9
        finally:
            self._cleanup()

    def test_used_does_not_update_confidence(self):
        """'used' MUST NOT touch ConfidenceField evidence counts.

        Distinguishes 'used' from 'acted' / 'contradicted' which do.
        """
        self._cleanup()
        try:
            m = UsedOutcomeMemory(name="used-no-conf", content="c")
            m.save()

            initial_data = ConfidenceField.get_confidence_data(m, "certainty")
            initial_count = initial_data["evidence_count"]

            outcome_map = {m.db_key.redis_key: "used"}
            ObservationProtocol.on_context_used([m], outcome_map)

            after_data = ConfidenceField.get_confidence_data(m, "certainty")
            # Evidence count should NOT have incremented — no confidence update.
            # Note: auto_resolve with error=0.3 is BELOW the default
            # PL_CONFIDENCE_ERROR_THRESHOLD=0.7, so _apply_confidence_feedback
            # does not fire — this is the correct behavior.
            assert after_data["evidence_count"] == initial_count
        finally:
            self._cleanup()

    def test_used_does_not_strengthen_cycle(self):
        """'used' MUST NOT call strengthen_cycle (distinguishes from 'acted')."""
        self._cleanup()
        try:
            m = UsedOutcomeMemory(name="used-no-cycle", content="c")
            m.save()

            # Read the cycle amplitude before the "used" outcome
            cycles_before = POPOTO_REDIS_DB.hget(
                m.db_key.redis_key, "_cycles_relevance"
            )
            # Issue a "used" outcome — should NOT change cycle state
            outcome_map = {m.db_key.redis_key: "used"}
            ObservationProtocol.on_context_used([m], outcome_map)

            cycles_after = POPOTO_REDIS_DB.hget(m.db_key.redis_key, "_cycles_relevance")
            # Cycle state should be unchanged.
            assert cycles_before == cycles_after
        finally:
            self._cleanup()

    def test_used_empty_instances_noop(self):
        """on_context_used with empty instance list is a no-op even for 'used'."""
        ObservationProtocol.on_context_used([], {"rk1": "used"})  # no raise


# ---------------------------------------------------------------------------
# Docstring / feature-doc integration tests (issue #370, item 1 + item 4)
# ---------------------------------------------------------------------------


class TestFeatureDocAndDocstrings:
    """Assert integrator-facing docstrings and the feature doc stay in sync."""

    _REPO_ROOT = os.path.dirname(SCRIPT_DIR)
    _FEATURE_DOC = os.path.join(
        _REPO_ROOT, "docs", "features", "observation-protocol.md"
    )

    def test_observation_protocol_doc_contains_effects_table(self):
        """The feature doc's Effects Matrix section includes all five outcomes
        and all five row labels (ConfidenceField, CyclicDecayField,
        DecayingSortedField, AccessTracker, PredictionLedger).
        """
        with open(self._FEATURE_DOC, encoding="utf-8") as f:
            text = f.read()

        # All five outcome column headers must appear.
        for outcome in ("acted", "used", "dismissed", "deferred", "contradicted"):
            assert (
                outcome in text
            ), f"Expected outcome {outcome!r} in {self._FEATURE_DOC}"

        # All five row labels must appear.
        for row_label in (
            "ConfidenceField",
            "CyclicDecayField",
            "DecayingSortedField",
            "AccessTracker",
            "PredictionLedger",
        ):
            assert (
                row_label in text
            ), f"Expected row label {row_label!r} in {self._FEATURE_DOC}"

        # And there must be a table (pipe-separated row with the columns).
        assert (
            "| acted" in text or "|acted" in text
        ), "Expected a Markdown table with an 'acted' column in the Effects Matrix"

    def test_observation_module_docstring_points_to_feature_doc(self):
        """The module docstring of popoto.fields.observation references the
        feature doc so integrators using help() find the full matrix.
        """
        from popoto.fields import observation

        assert observation.__doc__ is not None
        assert "docs/features/observation-protocol.md" in observation.__doc__, (
            "Expected module docstring to reference "
            "docs/features/observation-protocol.md as a See Also pointer"
        )

    def test_on_context_used_docstring_mentions_coerce(self):
        """on_context_used() docstring must warn integrators about strict
        VALID_OUTCOMES validation, mention 'coerce', 'ValueError', and
        cross-reference observation-protocol.md (NOT metacognitive-layer.md).
        """
        doc = ObservationProtocol.on_context_used.__doc__
        assert doc is not None
        for needle in ("coerce", "ValueError", "observation-protocol.md"):
            assert (
                needle in doc
            ), f"Expected {needle!r} in on_context_used docstring, got:\n{doc}"

    def test_changelog_has_used_migration_note(self):
        """CHANGELOG.md v1.5.0 section includes a Migration note mentioning
        the bespoke 'echoed' outcome integrators need to map into the
        canonical vocabulary.
        """
        changelog = os.path.join(self._REPO_ROOT, "CHANGELOG.md")
        with open(changelog, encoding="utf-8") as f:
            text = f.read()

        # Locate the v1.5.0 section (up to the next H2).
        start = text.find("## [1.5.0")
        assert start != -1, "Could not find v1.5.0 section in CHANGELOG"
        end = text.find("\n## [", start + 1)
        section = text[start:end] if end != -1 else text[start:]

        assert (
            "Migration" in section
        ), "Expected 'Migration' sub-section in v1.5.0 CHANGELOG entry"
        assert (
            "echoed" in section
        ), "Expected 'echoed' migration guidance in v1.5.0 CHANGELOG entry"


# =========================================================================
# Auto-discharge epsilon boundary (issue #407)
# =========================================================================


class EpsilonBoundaryMemory(popoto.Model):
    """Model with ConfidenceField + pressured CyclicDecayField for the
    auto-discharge epsilon-boundary tests."""

    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(initial_confidence=0.5)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
        pressure_rate=0.1,
    )


class TestAutoDischargeEpsilonBoundary:
    """Auto-discharge fires only for conf < threshold - CONFIDENCE_EPSILON.

    Values within float epsilon of the 0.1 threshold (e.g. the IEEE-754
    predecessor 0.09999999999999998) are NOT below it — fixes the float
    artifact that auto-discharged on the first contradiction.
    """

    def _cleanup(self):
        EpsilonBoundaryMemory.delete_all()
        for pattern in (
            "$ConfidencF:EpsilonBoundaryMemory:*",
            "$CyclicDecayF:EpsilonBoundaryMemory:*",
        ):
            for key in POPOTO_REDIS_DB.keys(pattern):
                POPOTO_REDIS_DB.delete(key)

    def _seed(self, m, confidence, last_resolved):
        """Seed companion-hash confidence and pressure state directly.

        Companion hashes are field metadata (not Popoto model hashes), so
        direct writes are the established seeding pattern in these tests.
        """
        member_key = m.db_key.redis_key

        conf_field = m._meta.fields["certainty"]
        data_hash_key = conf_field.get_data_hash_key(m, "certainty")
        POPOTO_REDIS_DB.hset(
            data_hash_key,
            member_key,
            msgpack.packb(
                {
                    "confidence": confidence,
                    "evidence_count": 20,
                    "corroborations": 0,
                    "contradictions": 20,
                }
            ),
        )

        rel_field = m._meta.fields["relevance"]
        pressure_hash_key = rel_field.get_pressure_hash_key(m, "relevance")
        POPOTO_REDIS_DB.hset(
            pressure_hash_key,
            member_key,
            msgpack.packb({"rate": 0.1, "last_resolved": last_resolved}),
        )
        return pressure_hash_key, member_key

    def test_float_predecessor_of_threshold_does_not_discharge(self):
        """conf seeded at the float predecessor of 0.1 must NOT discharge.

        After the contradicted update (signal 0.1, at the evidence cap):
        c' = c + (0.1 - c)/21, which keeps c within CONFIDENCE_EPSILON
        (1e-9) of the 0.1 threshold — so pressure stays unresolved.
        """
        self._cleanup()
        try:
            m = EpsilonBoundaryMemory(name="eps-predecessor")
            m.save()
            thirty_days_ago = time.time() - 86400 * 30
            pressure_hash_key, member_key = self._seed(
                m, confidence=0.09999999999999998, last_resolved=thirty_days_ago
            )

            ObservationProtocol.on_context_used(
                [m], {m.db_key.redis_key: "contradicted"}
            )

            raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
            assert raw is not None
            pdata = msgpack.unpackb(raw, raw=False)
            assert abs(pdata["last_resolved"] - thirty_days_ago) < 1.0, (
                "pressure was resolved — a value within float epsilon of "
                "the threshold must NOT auto-discharge"
            )
        finally:
            self._cleanup()

    def test_clearly_below_threshold_discharges(self):
        """conf clearly below threshold - epsilon DOES auto-discharge.

        Seeded at 0.05; the contradicted update gives
        c' = 0.05 + (0.1 - 0.05)/21 = 0.0524 < 0.1 - 1e-9 -> discharge.
        """
        self._cleanup()
        try:
            m = EpsilonBoundaryMemory(name="eps-below")
            m.save()
            thirty_days_ago = time.time() - 86400 * 30
            pressure_hash_key, member_key = self._seed(
                m, confidence=0.05, last_resolved=thirty_days_ago
            )

            ObservationProtocol.on_context_used(
                [m], {m.db_key.redis_key: "contradicted"}
            )

            raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
            assert raw is not None
            pdata = msgpack.unpackb(raw, raw=False)
            assert (
                time.time() - pdata["last_resolved"] < 60
            ), "pressure should have been auto-discharged"
        finally:
            self._cleanup()
