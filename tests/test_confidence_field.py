"""Tests for ConfidenceField — Bayesian confidence tracking.

Tests cover:
- Field initialization and defaults
- Bayesian update: corroboration increases confidence
- Bayesian update: contradiction decreases confidence
- Precision growth: later updates have smaller effect
- Entrainment via ObservationProtocol (acted, dismissed, contradicted)
- Auto-discharge: confidence below 0.1 triggers pressure resolve
- on_save/on_delete lifecycle
- Edge cases: unsaved model, wrong field type
- Pipeline support
- Synergy with DecayingSortedField (composite scoring)
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.redis_db import POPOTO_REDIS_DB


# --- Test Models ---


class ConfidenceMemory(popoto.Model):
    key = popoto.AutoKeyField()
    content = popoto.Field(type=str)
    confidence = ConfidenceField(initial_confidence=0.5)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(86400 * 90, 5.0, 0)],
        pressure_rate=0.1,
    )


class SimpleConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    score = ConfidenceField(initial_confidence=0.7)


# --- Fixtures ---


@pytest.fixture(autouse=True)
def clean_redis():
    """Flush test keys before each test."""
    yield
    # Clean up all keys for test models
    for pattern in [
        "ConfidenceMemory:*",
        "SimpleConfidence:*",
        "$ConfidencF:*",
        "$CyclicDecayF:*",
        "$DecayingSortedF:*",
        "$SortedF:*",
        "$RP:*",
    ]:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


# --- Basic Tests ---


class TestConfidenceFieldInit:
    """Test ConfidenceField initialization and defaults."""

    def test_default_initial_confidence(self):
        """ConfidenceField with no args defaults to 0.5."""
        field = ConfidenceField()
        assert field.initial_confidence == 0.5

    def test_custom_initial_confidence(self):
        """ConfidenceField respects custom initial_confidence."""
        field = ConfidenceField(initial_confidence=0.8)
        assert field.initial_confidence == 0.8

    def test_field_type_is_float(self):
        """ConfidenceField type defaults to float."""
        field = ConfidenceField()
        assert field.type is float

    def test_field_null_default(self):
        """ConfidenceField allows null by default."""
        field = ConfidenceField()
        assert field.null is True


class TestConfidenceFieldLifecycle:
    """Test on_save and on_delete lifecycle hooks."""

    def test_save_creates_companion_data(self):
        """Saving a model with ConfidenceField creates companion hash data."""
        item = SimpleConfidence.create(name="test-save")
        data = item.get_confidence_data("score")
        assert data is not None
        assert data["confidence"] == 0.7
        assert data["evidence_count"] == 0
        assert data["corroborations"] == 0
        assert data["contradictions"] == 0

    def test_delete_cleans_companion_data(self):
        """Deleting a model removes companion hash entries."""
        item = SimpleConfidence.create(name="test-delete")
        # Verify data exists
        data = item.get_confidence_data("score")
        assert data is not None

        # Delete and verify cleanup
        item.delete()
        data = ConfidenceField.get_confidence_data(item, "score")
        assert data is None

    def test_resave_preserves_confidence(self):
        """Re-saving does not reset confidence data if it already exists."""
        item = SimpleConfidence.create(name="test-resave")
        item.update_confidence("score", signal=0.9)
        data_before = item.get_confidence_data("score")

        # Re-save
        item.save()
        data_after = item.get_confidence_data("score")

        # Confidence should not be reset
        assert data_after["confidence"] == data_before["confidence"]
        assert data_after["evidence_count"] == data_before["evidence_count"]


class TestBayesianUpdate:
    """Test Bayesian confidence updates."""

    def test_corroboration_increases_confidence(self):
        """Signal > 0.5 (corroboration) increases confidence from 0.5."""
        item = SimpleConfidence.create(name="test-corr")
        initial = item.get_confidence("score")
        assert initial == 0.7

        item.update_confidence("score", signal=0.9)
        updated = item.get_confidence("score")
        assert updated > initial

    def test_contradiction_decreases_confidence(self):
        """Signal < 0.5 (contradiction) decreases confidence."""
        item = SimpleConfidence.create(name="test-contra")
        initial = item.get_confidence("score")

        item.update_confidence("score", signal=0.1)
        updated = item.get_confidence("score")
        assert updated < initial

    def test_evidence_count_increments(self):
        """Each update increments evidence_count."""
        item = SimpleConfidence.create(name="test-evidence")
        item.update_confidence("score", signal=0.9)
        item.update_confidence("score", signal=0.8)
        item.update_confidence("score", signal=0.7)

        data = item.get_confidence_data("score")
        assert data["evidence_count"] == 3

    def test_corroborations_counter(self):
        """Signal >= 0.5 increments corroborations."""
        item = SimpleConfidence.create(name="test-corr-cnt")
        item.update_confidence("score", signal=0.9)
        item.update_confidence("score", signal=0.8)
        item.update_confidence("score", signal=0.1)  # contradiction

        data = item.get_confidence_data("score")
        assert data["corroborations"] == 2
        assert data["contradictions"] == 1

    def test_precision_growth(self):
        """10th update moves score less than 1st update."""
        item = SimpleConfidence.create(name="test-precision")

        # First update
        before_first = item.get_confidence("score")
        item.update_confidence("score", signal=0.9)
        after_first = item.get_confidence("score")
        first_delta = abs(after_first - before_first)

        # Do 9 more updates to build evidence
        for _ in range(9):
            item.update_confidence("score", signal=0.9)

        # 11th update
        before_11th = item.get_confidence("score")
        item.update_confidence("score", signal=0.9)
        after_11th = item.get_confidence("score")
        eleventh_delta = abs(after_11th - before_11th)

        assert eleventh_delta < first_delta

    def test_bayesian_formula(self):
        """Verify the exact Bayesian update formula."""
        item = SimpleConfidence.create(name="test-formula")
        # initial_confidence = 0.7, signal = 0.9
        # evidence_count starts at 0
        # new = 0.7 + (0.9 - 0.7) / sqrt(0 + 1) = 0.7 + 0.2 = 0.9
        item.update_confidence("score", signal=0.9)
        data = item.get_confidence_data("score")
        assert abs(data["confidence"] - 0.9) < 1e-6

        # Second update: prior=0.9, signal=0.9, evidence_count=1
        # new = 0.9 + (0.9 - 0.9) / sqrt(1+1) = 0.9
        item.update_confidence("score", signal=0.9)
        data = item.get_confidence_data("score")
        assert abs(data["confidence"] - 0.9) < 1e-6


class TestEntrainment:
    """Test ObservationProtocol entrainment with ConfidenceField."""

    def test_acted_increases_confidence(self):
        """'acted' outcome corroborates ConfidenceField (signal=0.9)."""
        item = ConfidenceMemory.create(content="test acted")
        initial = item.get_confidence("confidence")

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        updated = item.get_confidence("confidence")
        assert updated > initial

    def test_contradicted_decreases_confidence(self):
        """'contradicted' outcome reduces ConfidenceField (signal=0.1)."""
        item = ConfidenceMemory.create(content="test contradicted")
        initial = item.get_confidence("confidence")

        outcome_map = {item.db_key.redis_key: "contradicted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        updated = item.get_confidence("confidence")
        assert updated < initial

    def test_dismissed_no_confidence_change(self):
        """'dismissed' outcome does not change ConfidenceField."""
        item = ConfidenceMemory.create(content="test dismissed")
        initial = item.get_confidence("confidence")

        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        updated = item.get_confidence("confidence")
        assert updated == initial

    def test_dismiss_3x_weakens_cycle_amplitude(self):
        """Dismissing 3 times weakens CyclicDecayField amplitude."""
        item = ConfidenceMemory.create(content="test dismiss cycles")

        # Get initial cycle amplitude
        field = item._meta.fields["relevance"]
        member_key = item.db_key.redis_key
        cycles_hash_key = field._get_cycles_hash_key(item, "relevance")
        raw = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)
        initial_cycles = msgpack.unpackb(raw, raw=False)
        initial_amp = initial_cycles[0][1]

        # Dismiss 3 times
        for _ in range(3):
            outcome_map = {item.db_key.redis_key: "dismissed"}
            ObservationProtocol.on_context_used([item], outcome_map)

        raw = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)
        updated_cycles = msgpack.unpackb(raw, raw=False)
        updated_amp = updated_cycles[0][1]

        assert updated_amp < initial_amp


class TestAutoDischarge:
    """Test auto-discharge when confidence drops below 0.1."""

    def test_low_confidence_resolves_pressure(self):
        """When confidence drops below 0.1, pressure is resolved."""
        item = ConfidenceMemory.create(content="test auto-discharge")

        # Let pressure build by manipulating the pressure hash
        field = item._meta.fields["relevance"]
        member_key = item.db_key.redis_key
        pressure_hash_key = field._get_pressure_hash_key(item, "relevance")
        # Set last_resolved far in the past so pressure has built up
        pressure_data = {"rate": 0.1, "last_resolved": time.time() - 86400 * 30}
        POPOTO_REDIS_DB.hset(
            pressure_hash_key, member_key, msgpack.packb(pressure_data)
        )

        # Hammer confidence down with contradictions
        for _ in range(20):
            item.update_confidence("confidence", signal=0.0)

        conf = item.get_confidence("confidence")
        assert conf < 0.1

        # Verify pressure was resolved (last_resolved should be recent)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        pdata = msgpack.unpackb(raw, raw=False)
        # last_resolved should have been updated to ~now
        assert pdata["last_resolved"] > time.time() - 5


class TestEdgeCases:
    """Test error handling and edge cases."""

    def test_unsaved_model_raises_typeerror(self):
        """update_confidence on unsaved model raises TypeError."""
        item = SimpleConfidence(name="unsaved")
        with pytest.raises(TypeError, match="unsaved"):
            item.update_confidence("score", signal=0.9)

    def test_wrong_field_type_raises_typeerror(self):
        """update_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceMemory.create(content="test wrong type")
        with pytest.raises(TypeError, match="ConfidenceField"):
            item.update_confidence("content", signal=0.9)

    def test_nonexistent_field_raises_attributeerror(self):
        """update_confidence on nonexistent field raises AttributeError."""
        item = SimpleConfidence.create(name="test-nofield")
        with pytest.raises(AttributeError, match="has no field"):
            item.update_confidence("nonexistent", signal=0.9)

    def test_get_confidence_wrong_field_type(self):
        """get_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceMemory.create(content="test wrong type")
        with pytest.raises(TypeError, match="ConfidenceField"):
            item.get_confidence("content")

    def test_get_confidence_data_wrong_field_type(self):
        """get_confidence_data on non-ConfidenceField raises TypeError."""
        item = ConfidenceMemory.create(content="test wrong type")
        with pytest.raises(TypeError, match="ConfidenceField"):
            item.get_confidence_data("content")


class TestPipelineSupport:
    """Test ConfidenceField operations with Redis pipelines."""

    def test_update_with_pipeline(self):
        """update_confidence works within a pipeline."""
        item = SimpleConfidence.create(name="test-pipeline")
        pipe = POPOTO_REDIS_DB.pipeline()

        item.update_confidence("score", signal=0.9, pipeline=pipe)
        pipe.execute()

        # Verify the update was applied
        data = item.get_confidence_data("score")
        assert data["evidence_count"] == 1
        assert data["confidence"] > 0.7


class TestSynergyWithDecay:
    """Test composite scoring: decay_score * confidence."""

    def test_confidence_accessible_alongside_decay(self):
        """ConfidenceField and CyclicDecayField coexist on same model."""
        item = ConfidenceMemory.create(content="synergy test")

        # Both fields should have data
        conf = item.get_confidence("confidence")
        assert conf == 0.5

        # Decay field should work normally
        results = ConfidenceMemory.query.top_by_decay("relevance", n=10)
        assert len(results) >= 1

    def test_composite_scoring_pattern(self):
        """Application can compute decay_score * confidence for ranking."""
        item1 = ConfidenceMemory.create(content="high confidence")
        item2 = ConfidenceMemory.create(content="low confidence")

        # Boost item1 confidence
        for _ in range(5):
            item1.update_confidence("confidence", signal=0.9)

        # Lower item2 confidence
        for _ in range(5):
            item2.update_confidence("confidence", signal=0.1)

        conf1 = item1.get_confidence("confidence")
        conf2 = item2.get_confidence("confidence")

        assert conf1 > conf2
