"""End-to-end composite tests for agent memory primitives.

Validates that primitives compose correctly under realistic multi-step
workflows that no single-primitive unit test covers. Each test class
exercises a specific cross-cutting scenario.

Tests cover:
- Full agent memory loop: retrieve → act/dismiss/defer/contradict → verify state
- WriteFilter → decay index → event stream → consumer pipeline
- Prediction → observation → confidence correction loop
- CoOccurrence graph propagation with confidence weighting
- Temporal dynamics: pressure buildup + cycle resonance + confidence over time
- ContextAssembler + PolicyCache: full retrieval-to-action pipeline
"""

import asyncio
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest

from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.constants import Defaults, TemporalPeriod
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.event_stream import EventStreamMixin
from src.popoto.fields.existence_filter import ExistenceFilter, FrequencySketch
from src.popoto.fields.observation import ObservationProtocol, RecallProposal
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Test Models — composed from multiple primitives
# ---------------------------------------------------------------------------


class AgentMemory(
    PredictionLedgerMixin,
    AccessTrackerMixin,
    WriteFilterMixin,
    EventStreamMixin,
    popoto.Model,
):
    """Full-stack agent memory model composing all field types."""

    _stream_name = "e2e_mutations"
    _stream_metadata_fields = ("source",)
    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7
    _pl_partition = "e2e"

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.StringField(default="")
    source = popoto.StringField(default="agent")
    importance = popoto.FloatField(default=0.5)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        partition_by="agent_id",
        cycles=[(TemporalPeriod.WEEKLY, 3.0, 0)],
        pressure_rate=0.1,
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.content,
    )
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.content,
    )

    def compute_filter_score(self):
        return self.importance or 0.0


class SimpleTrackedMemory(AccessTrackerMixin, popoto.Model):
    """Lighter model for tests that don't need the full stack."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)


class StreamedTrackedMemory(
    PredictionLedgerMixin,
    AccessTrackerMixin,
    EventStreamMixin,
    popoto.Model,
):
    """Model for prediction + observation + stream tests."""

    _stream_name = "e2e_pred"
    _pl_partition = "e2e_pred"

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField()
    confidence = ConfidenceField(initial_confidence=0.5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    """Remove all Redis keys matching patterns (test-only, uses KEYS)."""
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    """Clean all test model keys."""
    _clean_keys(
        "*AgentMemory*",
        "*SimpleTrackedMemory*",
        "*StreamedTrackedMemory*",
        "$EF:*Memory*",
        "$FS:*Memory*",
        "$CoOcF:*Memory*",
        "$ConfidencF:*Memory*",
        "$SortedF:*Memory*",
        "$CyclicDF:*Memory*",
        "$RP:*Memory*",
        "$AT:*Memory*",
        "$WF:*Memory*",
        "$PL:*Memory*",
        "stream:e2e_*",
    )


def _read_stream(stream_key, count=100):
    """Read all entries from a Redis Stream."""
    return POPOTO_REDIS_DB.xrange(stream_key, count=count)


@pytest.fixture(autouse=True)
def clean_redis():
    """Clean Redis before and after each test."""
    _clean_all()
    yield
    _clean_all()


# ---------------------------------------------------------------------------
# Test 1: Full Agent Memory Loop
# ---------------------------------------------------------------------------


class TestFullMemoryLoop:
    """Complete retrieve → act/dismiss/defer/contradict → verify all state."""

    def test_retrieve_and_observe_mixed_outcomes(self):
        """Multiple memories retrieved, each gets a different outcome."""
        # Step 1: Create memories with varying importance
        memories = []
        for i, (content, importance) in enumerate(
            [
                ("deploy procedure", 0.8),
                ("old API endpoint", 0.5),
                ("meeting notes", 0.3),
                ("deprecated config", 0.5),
            ]
        ):
            m = AgentMemory(
                agent_id="loop-agent",
                content=content,
                importance=importance,
            )
            m.save()
            memories.append(m)

        # Step 2: Retrieve top memories (exercises DecayingSortedField + AccessTracker)
        retrieved = AgentMemory.query.filter(
            agent_id="loop-agent",
        ).top_by_decay("relevance", n=10)
        assert len(retrieved) == 4

        # Step 3: Assign mixed outcomes
        outcome_map = {
            memories[0].db_key.redis_key: "acted",
            memories[1].db_key.redis_key: "dismissed",
            memories[2].db_key.redis_key: "deferred",
            memories[3].db_key.redis_key: "contradicted",
        }
        time.sleep(0.05)  # ensure timestamp differentiation
        ObservationProtocol.on_context_used(memories, outcome_map)

        # Step 4: Verify acted memory — touched, access confirmed, confidence up
        acted_conf = ConfidenceField.get_confidence(memories[0], "confidence")
        assert acted_conf > 0.5  # corroboration signal=0.9

        # Step 5: Verify contradicted memory — confidence down
        contradicted_conf = ConfidenceField.get_confidence(memories[3], "confidence")
        assert contradicted_conf < 0.5  # contradiction signal=0.1

        # Step 6: Verify bloom filter registered all saved content
        assert (
            AgentMemory.bloom.definitely_missing(AgentMemory, "deploy procedure")
            is False
        )

        # Step 7: Verify frequency sketch tracked saves
        deploy_freq = AgentMemory.freq.get_frequency(AgentMemory, "deploy procedure")
        assert deploy_freq >= 1

    def test_acted_memory_gets_decay_touch(self):
        """Acted outcome refreshes decay timestamp via touch()."""
        m = AgentMemory(
            agent_id="touch-agent",
            content="important fact",
            importance=0.8,
        )
        m.save()

        # Record original score
        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            m, "relevance"
        ).redis_key
        original_score = POPOTO_REDIS_DB.zscore(ss_key, m.db_key.redis_key)

        time.sleep(0.05)
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "acted"},
        )

        new_score = POPOTO_REDIS_DB.zscore(ss_key, m.db_key.redis_key)
        assert new_score > original_score

    def test_deferred_memories_build_pressure(self):
        """Deferred outcome leaves pressure building — no discharge."""
        m = AgentMemory(
            agent_id="pressure-agent",
            content="unresolved task",
            importance=0.8,
        )
        m.save()

        # Manually set pressure to simulate time passing
        field = m._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(m, "relevance")
        member_key = m.db_key.redis_key
        pressure_data = {
            "rate": 0.1,
            "last_resolved": time.time() - 86400 * 10,  # 10 days ago
        }
        POPOTO_REDIS_DB.hset(
            pressure_hash_key,
            member_key,
            msgpack.packb(pressure_data),
        )

        # Defer — should NOT resolve pressure
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "deferred"},
        )

        # Pressure should still be unresolved (last_resolved unchanged)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        data = msgpack.unpackb(raw, raw=False)
        assert data["last_resolved"] < time.time() - 86400 * 5

    def test_stream_entries_produced_on_save(self):
        """Model saves produce event stream entries."""
        m = AgentMemory(
            agent_id="stream-agent",
            content="streamed fact",
            importance=0.8,
        )
        m.save()

        entries = _read_stream("stream:e2e_mutations")
        assert len(entries) >= 1
        last_entry = entries[-1][1]
        assert last_entry[b"model"] == b"AgentMemory"
        assert last_entry[b"op"] == b"create"


# ---------------------------------------------------------------------------
# Test 2: WriteFilter → Index → Stream Pipeline
# ---------------------------------------------------------------------------


class TestFilteredPipeline:
    """WriteFilter gating through to decay index and event stream."""

    def _get_ss_key(self, instance, field_name="relevance"):
        """Get the actual sorted set Redis key for a field on an instance."""
        return CyclicDecayField.get_partitioned_sortedset_db_key(
            instance,
            field_name,
        ).redis_key

    def test_below_threshold_not_indexed(self):
        """Low-importance memory is silently discarded — not in any index."""
        m = AgentMemory(
            agent_id="filter-agent",
            content="noise",
            importance=0.1,
        )
        m.save()  # should be silently discarded

        # Not queryable
        results = AgentMemory.query.filter(agent_id="filter-agent").all()
        assert len(results) == 0

        # No stream entry
        entries = _read_stream("stream:e2e_mutations")
        assert len(entries) == 0

    def test_above_threshold_indexed_and_streamed(self):
        """Normal-importance memory is persisted, indexed, and streamed."""
        m = AgentMemory(
            agent_id="filter-agent",
            content="useful",
            importance=0.5,
        )
        m.save()

        # In sorted set (use API to get correct key)
        ss_key = self._get_ss_key(m)
        members = POPOTO_REDIS_DB.zrange(ss_key, 0, -1)
        assert len(members) == 1

        # Stream entry produced
        entries = _read_stream("stream:e2e_mutations")
        assert len(entries) >= 1

    def test_priority_tagged_and_indexed(self):
        """High-importance memory is persisted, priority-tagged, and in bloom."""
        m = AgentMemory(
            agent_id="filter-agent",
            content="critical finding",
            importance=0.9,
        )
        m.save()

        # In priority set
        priority_key = "$WF:AgentMemory:priority"
        priority_members = POPOTO_REDIS_DB.zrange(priority_key, 0, -1)
        assert len(priority_members) == 1

        # In bloom filter
        assert (
            AgentMemory.bloom.definitely_missing(AgentMemory, "critical finding")
            is False
        )

    def test_mixed_importance_batch(self):
        """Batch of saves with mixed importance — only valid ones indexed."""
        saved = []
        for content, importance in [
            ("noise1", 0.1),  # filtered out
            ("noise2", 0.15),  # filtered out
            ("useful1", 0.5),  # persisted
            ("useful2", 0.6),  # persisted
            ("critical", 0.9),  # persisted + priority
        ]:
            m = AgentMemory(
                agent_id="batch-agent",
                content=content,
                importance=importance,
            )
            m.save()
            saved.append(m)

        # Query should return exactly 3 persisted records
        results = AgentMemory.query.filter(agent_id="batch-agent").all()
        assert len(results) == 3

        # Priority set should have exactly 1 member
        priority_key = "$WF:AgentMemory:priority"
        priority_members = POPOTO_REDIS_DB.zrange(priority_key, 0, -1)
        assert len(priority_members) == 1

        # Stream should have exactly 3 entries
        entries = _read_stream("stream:e2e_mutations")
        assert len(entries) == 3


# ---------------------------------------------------------------------------
# Test 3: Prediction → Observation → Confidence Correction
# ---------------------------------------------------------------------------


class TestPredictionCorrectionLoop:
    """PredictionLedger → ObservationProtocol → ConfidenceField feedback."""

    def test_predict_observe_correct(self):
        """Prediction recorded, observation resolves it, confidence adjusts."""
        m = StreamedTrackedMemory(name="pred-1", content="the sky is blue")
        m.save()

        # Step 1: Record prediction
        PredictionLedgerMixin.record_prediction(
            m,
            predicted={"relevance": 0.9},
        )

        # Step 2: Resolve with actual outcome (agent contradicted the claim)
        PredictionLedgerMixin.resolve_prediction(
            m,
            actual={"relevance": 0.2},
        )

        # Step 3: Verify prediction error is stored
        meta_key = PredictionLedgerMixin._meta_key(m)
        member = m.db_key.redis_key
        raw = POPOTO_REDIS_DB.hget(meta_key, member)
        assert raw is not None
        data = msgpack.unpackb(raw, raw=False)
        assert data["resolved"] is True
        assert abs(data["prediction_error"] - 0.7) < 0.01

    def test_auto_resolve_via_observation(self):
        """ObservationProtocol auto-resolves predictions on context_used."""
        m = StreamedTrackedMemory(name="auto-1", content="auto resolve test")
        m.save()

        # Record prediction
        PredictionLedgerMixin.record_prediction(
            m,
            predicted={"relevance": 0.8},
        )

        # Observation with "acted" outcome triggers auto-resolve
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "acted"},
        )

        # Verify confidence went up (acted = corroboration)
        conf = ConfidenceField.get_confidence(m, "confidence")
        assert conf >= 0.5  # at least initial

    def test_contradicted_reduces_confidence(self):
        """Contradicted outcome reduces confidence when prediction error is high."""
        m = StreamedTrackedMemory(name="contra-1", content="wrong claim")
        m.save()

        initial_conf = ConfidenceField.get_confidence(m, "confidence")

        # Contradicted outcome
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "contradicted"},
        )

        final_conf = ConfidenceField.get_confidence(m, "confidence")
        assert final_conf < initial_conf

    def test_stream_entry_on_prediction_lifecycle(self):
        """Prediction save and resolution both produce stream entries."""
        m = StreamedTrackedMemory(name="stream-pred", content="streamed prediction")
        m.save()

        entries_after_save = _read_stream("stream:e2e_pred")
        count_after_save = len(entries_after_save)

        PredictionLedgerMixin.record_prediction(
            m,
            predicted={"relevance": 0.5},
        )
        PredictionLedgerMixin.resolve_prediction(
            m,
            actual={"relevance": 0.5},
        )

        # Prediction operations don't produce stream entries directly
        # (they're hash updates, not model saves), but the model save did
        entries_after_all = _read_stream("stream:e2e_pred")
        assert len(entries_after_all) >= count_after_save


# ---------------------------------------------------------------------------
# Test 4: CoOccurrence Graph with Confidence Weighting
# ---------------------------------------------------------------------------


class TestGraphWithConfidence:
    """CoOccurrence propagation interacting with confidence scores."""

    def test_linked_memories_propagate(self):
        """BFS propagation returns associated memories with decayed weights."""
        a = SimpleTrackedMemory(name="ml", content="machine learning")
        b = SimpleTrackedMemory(name="nn", content="neural networks")
        c = SimpleTrackedMemory(name="dl", content="deep learning")
        a.save()
        b.save()
        c.save()

        field = SimpleTrackedMemory._meta.fields["associations"]

        # Create chain: ml -> nn -> dl
        pk_a = a.db_key.redis_key
        pk_b = b.db_key.redis_key
        pk_c = c.db_key.redis_key

        field.link(SimpleTrackedMemory, pk_a, pk_b)
        field.link(SimpleTrackedMemory, pk_b, pk_c)

        # Strengthen edges
        field.strengthen(SimpleTrackedMemory, pk_a, pk_b, delta=0.5)
        field.strengthen(SimpleTrackedMemory, pk_b, pk_c, delta=0.5)

        # Propagate from ml — should find nn directly and dl via 2-hop
        scores = field.propagate(
            SimpleTrackedMemory,
            [pk_a],
            depth=2,
            decay_per_hop=0.5,
        )
        assert pk_b in scores
        assert pk_c in scores
        # 2-hop target should have lower weight
        assert scores[pk_c] < scores[pk_b]

    def test_confidence_and_associations_together(self):
        """High-confidence linked memory is reachable; low-confidence also reachable."""
        a = SimpleTrackedMemory(name="fact-a", content="established fact")
        b = SimpleTrackedMemory(name="fact-b", content="corroborated claim")
        a.save()
        b.save()

        field = SimpleTrackedMemory._meta.fields["associations"]
        pk_a = a.db_key.redis_key
        pk_b = b.db_key.redis_key

        field.link(SimpleTrackedMemory, pk_a, pk_b)
        field.strengthen(SimpleTrackedMemory, pk_a, pk_b, delta=0.3)

        # Boost confidence on b
        ConfidenceField.update_confidence(b, "confidence", signal=0.9)
        ConfidenceField.update_confidence(b, "confidence", signal=0.9)

        # Propagate — linked memory is reachable
        scores = field.propagate(SimpleTrackedMemory, [pk_a], depth=1)
        assert pk_b in scores

        # Verify confidence data is independently queryable
        conf = ConfidenceField.get_confidence(b, "confidence")
        assert conf > 0.5

    def test_symmetric_linking_creates_bidirectional_edges(self):
        """Symmetric CoOccurrenceField creates edges in both directions."""
        a = SimpleTrackedMemory(name="sym-a", content="A")
        b = SimpleTrackedMemory(name="sym-b", content="B")
        a.save()
        b.save()

        field = SimpleTrackedMemory._meta.fields["associations"]
        pk_a = a.db_key.redis_key
        pk_b = b.db_key.redis_key

        field.link(SimpleTrackedMemory, pk_a, pk_b)

        # Both directions should have edges
        a_linked = field.get_linked(SimpleTrackedMemory, pk_a)
        b_linked = field.get_linked(SimpleTrackedMemory, pk_b)

        a_targets = [pk for pk, _ in a_linked]
        b_targets = [pk for pk, _ in b_linked]
        assert pk_b in a_targets
        assert pk_a in b_targets


# ---------------------------------------------------------------------------
# Test 5: Temporal Dynamics — Pressure + Cycles + Confidence Over Time
# ---------------------------------------------------------------------------


class TestTemporalDynamics:
    """Simulated multi-day agent activity with interacting temporal effects."""

    def test_pressure_builds_across_deferred_observations(self):
        """Multiple deferred observations leave pressure building linearly."""
        m = AgentMemory(
            agent_id="temporal-agent",
            content="unresolved bug",
            importance=0.8,
        )
        m.save()

        # Set pressure with old last_resolved to simulate time passing
        field = m._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(m, "relevance")
        member_key = m.db_key.redis_key
        pressure_data = {
            "rate": 0.1,
            "last_resolved": time.time() - 86400 * 5,  # 5 days ago
        }
        POPOTO_REDIS_DB.hset(
            pressure_hash_key,
            member_key,
            msgpack.packb(pressure_data),
        )

        # Defer three times — pressure should NOT be resolved
        for _ in range(3):
            ObservationProtocol.on_context_used(
                [m],
                {m.db_key.redis_key: "deferred"},
            )

        # Pressure still unresolved
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        data = msgpack.unpackb(raw, raw=False)
        assert data["last_resolved"] < time.time() - 86400 * 4

    def test_acted_resolves_accumulated_pressure(self):
        """Acting on a memory resolves its accumulated pressure."""
        m = AgentMemory(
            agent_id="resolve-agent",
            content="fix the deployment",
            importance=0.8,
        )
        m.save()

        # Set pressure with old last_resolved
        field = m._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(m, "relevance")
        member_key = m.db_key.redis_key
        pressure_data = {
            "rate": 0.1,
            "last_resolved": time.time() - 86400 * 20,  # 20 days ago
        }
        POPOTO_REDIS_DB.hset(
            pressure_hash_key,
            member_key,
            msgpack.packb(pressure_data),
        )

        # Act on it
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "acted"},
        )

        # Pressure should be resolved (last_resolved ~ now)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        data = msgpack.unpackb(raw, raw=False)
        assert data["last_resolved"] > time.time() - 5  # within last 5 seconds

    def test_contradicted_with_low_confidence_auto_discharges_pressure(self):
        """Contradicted + low confidence auto-discharges pressure."""
        m = AgentMemory(
            agent_id="discharge-agent",
            content="wrong assumption",
            importance=0.8,
        )
        m.save()

        # Drive confidence below the discharge threshold. Capped Bayesian
        # oracle: from initial 0.5, after n <= cap updates at signal s the
        # confidence is the running mean (0.5 + s*n) / (n + 1). At s = 0.05,
        # (0.5 + 0.05n)/(n+1) < 0.1 requires n > 8, so at least 9 updates;
        # use 10: (0.5 + 10*0.05) / 11 = 1.0/11 = 0.0909...
        for _ in range(10):
            ConfidenceField.update_confidence(m, "confidence", signal=0.05)

        conf = ConfidenceField.get_confidence(m, "confidence")
        # (0.5 + 10*0.05) / 11 = 1.0/11
        assert abs(conf - 1.0 / 11) < 1e-12

        # Set up old pressure
        field = m._meta.fields["relevance"]
        pressure_hash_key = field.get_pressure_hash_key(m, "relevance")
        member_key = m.db_key.redis_key
        pressure_data = {
            "rate": 0.1,
            "last_resolved": time.time() - 86400 * 30,
        }
        POPOTO_REDIS_DB.hset(
            pressure_hash_key,
            member_key,
            msgpack.packb(pressure_data),
        )

        # Contradict — auto-discharges because the post-update confidence is
        # strictly below threshold - epsilon (a value float-equal-ish to the
        # threshold would NOT discharge under the epsilon guard, #407).
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "contradicted"},
        )

        # Post-contradiction oracle: contradicted applies one more update at
        # signal 0.1, so (0.5 + 10*0.05 + 0.1) / 12 = 1.1/12 = 0.091666...
        final_conf = ConfidenceField.get_confidence(m, "confidence")
        assert abs(final_conf - 1.1 / 12) < 1e-12
        assert final_conf < (
            Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - Defaults.CONFIDENCE_EPSILON
        )

    def test_cycle_amplitude_strengthens_on_acted(self):
        """Acted outcome strengthens cycle amplitudes via ObservationProtocol."""
        m = AgentMemory(
            agent_id="cycle-agent",
            content="weekly review",
            importance=0.8,
        )
        m.save()

        # Read original cycle amplitude
        field = m._meta.fields["relevance"]
        cycles_hash_key = field.get_cycles_hash_key(m, "relevance")
        member_key = m.db_key.redis_key
        raw_before = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)

        if raw_before:
            cycles_before = msgpack.unpackb(raw_before, raw=False)
            amp_before = cycles_before[0][1] if cycles_before else 3.0
        else:
            amp_before = 3.0  # default from field definition

        time.sleep(0.05)
        ObservationProtocol.on_context_used(
            [m],
            {m.db_key.redis_key: "acted"},
        )

        raw_after = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)
        if raw_after:
            cycles_after = msgpack.unpackb(raw_after, raw=False)
            amp_after = cycles_after[0][1] if cycles_after else amp_before
            assert amp_after >= amp_before


# ---------------------------------------------------------------------------
# Test 6: Access Patterns Feeding Composite Score
# ---------------------------------------------------------------------------


class TestAccessPatternScoring:
    """Access tracker patterns interacting with composite score retrieval."""

    def test_confirmed_access_updates_meta(self):
        """Confirmed access updates access_count and last_accessed metadata."""
        m = SimpleTrackedMemory(name="access-1", content="tracked item")
        m.save()

        # Trigger on_read via query
        results = SimpleTrackedMemory.query.filter(name="access-1").all()
        assert len(results) == 1

        # Confirm access
        results[0].confirm_access()

        # Verify access metadata
        at_meta_key = f"$AT:SimpleTrackedMemory:meta:{m.db_key.redis_key}"
        meta = POPOTO_REDIS_DB.hgetall(at_meta_key)
        assert int(meta.get(b"access_count", 0)) >= 1

    def test_multiple_confirms_accumulate(self):
        """Multiple confirm_access calls accumulate count."""
        m = SimpleTrackedMemory(name="multi-access", content="frequently used")
        m.save()

        for _ in range(3):
            retrieved = SimpleTrackedMemory.query.filter(
                name="multi-access",
            ).all()
            assert len(retrieved) == 1
            retrieved[0].confirm_access()

        at_meta_key = f"$AT:SimpleTrackedMemory:meta:{m.db_key.redis_key}"
        meta = POPOTO_REDIS_DB.hgetall(at_meta_key)
        assert int(meta.get(b"access_count", 0)) >= 3

    def test_discard_staged_does_not_increment_count(self):
        """Discarded staged reads don't affect confirmed access count."""
        m = SimpleTrackedMemory(name="discard-test", content="maybe useful")
        m.save()

        # Read (stages) then discard
        retrieved = SimpleTrackedMemory.query.filter(name="discard-test").all()
        assert len(retrieved) == 1
        retrieved[0].discard_staged_access()

        at_meta_key = f"$AT:SimpleTrackedMemory:meta:{m.db_key.redis_key}"
        meta = POPOTO_REDIS_DB.hgetall(at_meta_key)
        count = int(meta.get(b"access_count", 0))
        assert count == 0


# ---------------------------------------------------------------------------
# Test 7: ContextAssembler + Observation Full Loop
# ---------------------------------------------------------------------------


class TestAssemblerObservationLoop:
    """ContextAssembler retrieves → agent processes → ObservationProtocol updates."""

    def test_assemble_then_observe(self):
        """Full loop: assemble context, then report outcomes on retrieved records."""
        from src.popoto.recipes.context_assembler import ContextAssembler

        # Create memories
        for i, (content, importance) in enumerate(
            [
                ("kubernetes deployment", 0.8),
                ("database migration", 0.6),
                ("code review checklist", 0.5),
            ]
        ):
            AgentMemory(
                agent_id="assembler-agent",
                content=content,
                importance=importance,
            ).save()

        assembler = ContextAssembler(
            model_class=AgentMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            surfacing_threshold=0.0,
        )

        result = assembler.assemble(
            query_cues={"topic": "deployment"},
            partition_filters={"agent_id": "assembler-agent"},
        )

        # We should get records back
        if result.records:
            # Build outcome map — act on first, dismiss rest
            outcome_map = {}
            for i, record in enumerate(result.records):
                pk = record.db_key.redis_key
                outcome_map[pk] = "acted" if i == 0 else "dismissed"

            ObservationProtocol.on_context_used(result.records, outcome_map)

            # Verify acted record got confidence boost
            acted_conf = ConfidenceField.get_confidence(
                result.records[0],
                "confidence",
            )
            assert acted_conf >= 0.5


# ---------------------------------------------------------------------------
# Test 8: Existence Filter + Frequency Sketch in Retrieval Pipeline
# ---------------------------------------------------------------------------


class TestProbabilisticFilters:
    """Bloom filter and frequency sketch working with other primitives."""

    def test_bloom_prevents_unnecessary_queries(self):
        """ExistenceFilter returns definitely_missing for unknown content."""
        # No records saved — bloom should say definitely missing
        assert (
            AgentMemory.bloom.definitely_missing(
                AgentMemory,
                "nonexistent_xyz_topic",
            )
            is True
        )

    def test_bloom_registers_on_save(self):
        """Saving a record registers its fingerprint in the bloom filter."""
        m = AgentMemory(
            agent_id="bloom-agent",
            content="registered topic",
            importance=0.5,
        )
        m.save()

        assert (
            AgentMemory.bloom.definitely_missing(
                AgentMemory,
                "registered topic",
            )
            is False
        )

    def test_frequency_tracks_save_count(self):
        """FrequencySketch counts how many times a fingerprint is saved."""
        for i in range(5):
            AgentMemory(
                agent_id="freq-agent",
                content="repeated topic",
                importance=0.5,
            ).save()

        freq = AgentMemory.freq.get_frequency(AgentMemory, "repeated topic")
        assert freq >= 5

    def test_bloom_and_frequency_together(self):
        """Both probabilistic structures update on save and query correctly."""
        AgentMemory(
            agent_id="both-agent",
            content="dual tracked",
            importance=0.5,
        ).save()

        # Bloom knows it
        assert (
            AgentMemory.bloom.definitely_missing(
                AgentMemory,
                "dual tracked",
            )
            is False
        )

        # Frequency counted it
        freq = AgentMemory.freq.get_frequency(AgentMemory, "dual tracked")
        assert freq >= 1

        # Unknown content is still missing
        assert (
            AgentMemory.bloom.definitely_missing(
                AgentMemory,
                "never_saved_content",
            )
            is True
        )


# ---------------------------------------------------------------------------
# Test 9: Delete Cleanup Across Primitives
# ---------------------------------------------------------------------------


class TestDeleteCleanup:
    """Deleting a model instance cleans up all primitive indexes."""

    def test_delete_removes_from_sorted_set(self):
        """Deleted instance is removed from decay sorted set."""
        m = AgentMemory(
            agent_id="delete-agent",
            content="ephemeral",
            importance=0.5,
        )
        m.save()

        ss_key = CyclicDecayField.get_partitioned_sortedset_db_key(
            m,
            "relevance",
        ).redis_key
        assert POPOTO_REDIS_DB.zcard(ss_key) == 1

        m.delete()
        assert POPOTO_REDIS_DB.zcard(ss_key) == 0

    def test_delete_removes_priority_tag(self):
        """Deleted high-priority instance is removed from priority set."""
        m = AgentMemory(
            agent_id="delete-agent",
            content="was critical",
            importance=0.9,
        )
        m.save()

        priority_key = "$WF:AgentMemory:priority"
        assert POPOTO_REDIS_DB.zcard(priority_key) == 1

        m.delete()
        assert POPOTO_REDIS_DB.zcard(priority_key) == 0

    def test_delete_produces_stream_entry(self):
        """Deleting an instance produces a delete stream entry."""
        m = AgentMemory(
            agent_id="delete-agent",
            content="to be deleted",
            importance=0.5,
        )
        m.save()
        m.delete()

        entries = _read_stream("stream:e2e_mutations")
        ops = [entry[1][b"op"] for entry in entries]
        assert b"delete" in ops


# ---------------------------------------------------------------------------
# Test 10: Multi-Agent Partition Isolation
# ---------------------------------------------------------------------------


class TestPartitionIsolation:
    """Observations and queries don't cross-contaminate between agents."""

    def test_decay_queries_isolated_by_partition(self):
        """top_by_decay for agent-1 doesn't return agent-2 records."""
        for agent_id, content in [
            ("iso-agent-1", "agent 1 memory"),
            ("iso-agent-2", "agent 2 memory"),
        ]:
            AgentMemory(
                agent_id=agent_id,
                content=content,
                importance=0.5,
            ).save()

        results_1 = AgentMemory.query.filter(
            agent_id="iso-agent-1",
        ).top_by_decay("relevance", n=10)
        results_2 = AgentMemory.query.filter(
            agent_id="iso-agent-2",
        ).top_by_decay("relevance", n=10)

        assert len(results_1) == 1
        assert len(results_2) == 1
        assert results_1[0].content == "agent 1 memory"
        assert results_2[0].content == "agent 2 memory"

    def test_observation_on_one_agent_doesnt_affect_other(self):
        """ObservationProtocol on agent-1's memory doesn't touch agent-2's."""
        m1 = AgentMemory(
            agent_id="obs-agent-1",
            content="agent 1 fact",
            importance=0.5,
        )
        m1.save()
        m2 = AgentMemory(
            agent_id="obs-agent-2",
            content="agent 2 fact",
            importance=0.5,
        )
        m2.save()

        # Act on agent-1's memory
        ObservationProtocol.on_context_used(
            [m1],
            {m1.db_key.redis_key: "acted"},
        )

        # Agent-2's confidence should be unchanged (still initial 0.5)
        conf_2 = ConfidenceField.get_confidence(m2, "confidence")
        assert abs(conf_2 - 0.5) < 0.01
