"""Tests for ConfidenceField — Bayesian certainty tracking with entrainment.

Tests cover:
- Field initialization (initial_confidence, validation)
- on_save() initializes companion hash with initial_confidence
- on_delete() cleans up companion hash entries
- update_confidence() with Bayesian formula
- Precision growth: 10th update moves score less than 1st
- Corroboration increases confidence, contradiction decreases
- get_confidence() and get_confidence_data() read current values
- Error cases: unsaved model, invalid signal, wrong field type
- Entrainment: ObservationProtocol outcome handlers update confidence
- Auto-discharge: confidence < 0.1 resolves pressure on CyclicDecayFields
"""

import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class ConfidenceItem(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class ConfidenceCustom(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(initial_confidence=0.8)


class ConfidenceWithCyclic(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    certainty = ConfidenceField(initial_confidence=0.5)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        pressure_rate=0.1,
    )


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [ConfidenceItem, ConfidenceCustom, ConfidenceWithCyclic]:
        for key in POPOTO_REDIS_DB.keys(f"{model_class.__name__}:*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$Confidenc*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$CyclicDecay*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$RP:*"):
            POPOTO_REDIS_DB.delete(key)


# --- Initialization Tests ---


class TestConfidenceFieldInit:
    def test_default_initial_confidence(self):
        """Default initial_confidence is 0.5."""
        field = ConfidenceField()
        assert field.initial_confidence == 0.5

    def test_custom_initial_confidence(self):
        """Custom initial_confidence is stored."""
        field = ConfidenceField(initial_confidence=0.8)
        assert field.initial_confidence == 0.8

    def test_invalid_initial_confidence_below_zero(self):
        """initial_confidence < 0 raises ModelException."""
        with pytest.raises(popoto.ModelException):
            ConfidenceField(initial_confidence=-0.1)

    def test_invalid_initial_confidence_above_one(self):
        """initial_confidence > 1 raises ModelException."""
        with pytest.raises(popoto.ModelException):
            ConfidenceField(initial_confidence=1.5)

    def test_boundary_initial_confidence_zero(self):
        """initial_confidence=0 is valid."""
        field = ConfidenceField(initial_confidence=0)
        assert field.initial_confidence == 0

    def test_boundary_initial_confidence_one(self):
        """initial_confidence=1 is valid."""
        field = ConfidenceField(initial_confidence=1)
        assert field.initial_confidence == 1

    def test_field_type_is_float(self):
        """ConfidenceField defaults to float type."""
        field = ConfidenceField()
        assert field.type == float

    def test_field_is_nullable(self):
        """ConfidenceField is nullable by default."""
        field = ConfidenceField()
        assert field.null is True


# --- on_save / on_delete Lifecycle ---


class TestConfidenceFieldLifecycle:
    def test_on_save_initializes_companion_hash(self):
        """Saving a model with ConfidenceField creates companion hash entry."""
        item = ConfidenceItem.create(name="test1")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5
        assert data["evidence_count"] == 0
        assert data["corroborations"] == 0
        assert data["contradictions"] == 0

    def test_on_save_custom_initial(self):
        """Custom initial_confidence is used in companion hash."""
        item = ConfidenceCustom.create(name="test1")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.8

    def test_on_save_does_not_overwrite(self):
        """Re-saving does not overwrite existing confidence data."""
        item = ConfidenceItem.create(name="test1")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)

        # Re-save should NOT reset confidence
        item.save()
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 1  # still has the update

    def test_on_delete_removes_companion_hash(self):
        """Deleting model removes companion hash entry."""
        item = ConfidenceItem.create(name="test_del")
        # Verify data exists
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5

        item.delete()
        # After delete, get_confidence_data should return defaults
        # (but we can't call it on deleted instance easily, so check Redis directly)
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        member_key = f"ConfidenceItem:{item.name}"
        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        assert raw is None


# --- update_confidence Tests ---


class TestUpdateConfidence:
    def test_corroboration_increases_confidence(self):
        """Signal > 0.5 increases confidence from 0.5 initial."""
        item = ConfidenceItem.create(name="corr1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert new_conf > 0.5

    def test_contradiction_decreases_confidence(self):
        """Signal < 0.5 decreases confidence from 0.5 initial."""
        item = ConfidenceItem.create(name="contr1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.1)
        assert new_conf < 0.5

    def test_bayesian_formula_first_update(self):
        """First update: new = 0.5 + (signal - 0.5) / sqrt(0 + 1) = signal."""
        item = ConfidenceItem.create(name="formula1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        # sqrt(1) = 1, so new = 0.5 + (0.9 - 0.5) / 1 = 0.9
        assert abs(new_conf - 0.9) < 1e-6

    def test_bayesian_formula_second_update(self):
        """Second update uses sqrt(2) denominator."""
        item = ConfidenceItem.create(name="formula2")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        # After first: confidence = 0.9, evidence_count = 1
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        # new = 0.9 + (0.9 - 0.9) / sqrt(2) = 0.9
        assert abs(new_conf - 0.9) < 1e-6

    def test_precision_growth(self):
        """10th update moves score less than 1st update."""
        item = ConfidenceItem.create(name="precision1")

        # First update: delta = (0.8 - 0.5) / sqrt(1) = 0.3
        first_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.8)
        first_delta = abs(first_conf - 0.5)

        # Apply 8 more updates to build evidence
        for _ in range(8):
            ConfidenceField.update_confidence(item, "certainty", signal=0.8)

        # Record confidence before 10th update
        data_before = ConfidenceField.get_confidence_data(item, "certainty")
        conf_before = data_before["confidence"]

        # 10th update
        tenth_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.8)
        tenth_delta = abs(tenth_conf - conf_before)

        assert tenth_delta < first_delta

    def test_counters_increment(self):
        """Corroborations and contradictions counters track correctly."""
        item = ConfidenceItem.create(name="counters1")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item, "certainty", signal=0.1)

        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 3
        assert data["corroborations"] == 2
        assert data["contradictions"] == 1

    def test_confidence_clamped_to_zero_one(self):
        """Confidence stays within [0, 1] bounds."""
        item = ConfidenceItem.create(name="clamp1")
        # Many corroborations
        for _ in range(20):
            conf = ConfidenceField.update_confidence(item, "certainty", signal=1.0)
        assert 0 <= conf <= 1

        item2 = ConfidenceItem.create(name="clamp2")
        # Many contradictions
        for _ in range(20):
            conf = ConfidenceField.update_confidence(item2, "certainty", signal=0.0)
        assert 0 <= conf <= 1


# --- get_confidence / get_confidence_data Tests ---


class TestGetConfidence:
    def test_get_confidence_returns_initial(self):
        """get_confidence returns initial_confidence for new items."""
        item = ConfidenceItem.create(name="get1")
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf == 0.5

    def test_get_confidence_after_update(self):
        """get_confidence reflects updates."""
        item = ConfidenceItem.create(name="get2")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf - 0.9) < 1e-6

    def test_get_confidence_data_structure(self):
        """get_confidence_data returns complete metadata dict."""
        item = ConfidenceItem.create(name="get3")
        ConfidenceField.update_confidence(item, "certainty", signal=0.7)
        data = ConfidenceField.get_confidence_data(item, "certainty")

        assert "confidence" in data
        assert "evidence_count" in data
        assert "corroborations" in data
        assert "contradictions" in data
        assert data["evidence_count"] == 1
        assert data["corroborations"] == 1
        assert data["contradictions"] == 0


# --- Attribute Access Tests (issue #281) ---


class TestAttributeAccess:
    def test_attribute_returns_initial_confidence_after_create(self):
        """instance.certainty returns initial_confidence, not None (issue #281)."""
        item = ConfidenceItem.create(name="attr1")
        assert item.certainty == 0.5

    def test_attribute_returns_custom_initial_confidence(self):
        """instance.certainty returns custom initial_confidence after create."""
        item = ConfidenceCustom.create(name="attr2")
        assert item.certainty == 0.8

    def test_attribute_returns_initial_before_save(self):
        """instance.certainty returns initial_confidence even before save."""
        item = ConfidenceItem(name="attr3")
        assert item.certainty == 0.5

    def test_attribute_returns_updated_value_after_update(self):
        """instance.certainty reflects updated confidence after update_confidence."""
        item = ConfidenceItem.create(name="attr4")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(item.certainty - 0.9) < 1e-6

    def test_attribute_not_none(self):
        """instance.certainty is never None with default config (issue #281)."""
        item = ConfidenceItem.create(name="attr5")
        assert item.certainty is not None

    def test_confidence_reload_from_redis(self):
        """Reloading from Redis preserves initial_confidence (issue #289)."""
        ConfidenceItem.create(name="reload1")
        loaded = ConfidenceItem.query.get(name="reload1")
        assert loaded.certainty is not None
        assert loaded.certainty == 0.5


# --- Error Cases ---


class TestConfidenceFieldErrors:
    def test_update_unsaved_model_raises_type_error(self):
        """update_confidence on unsaved model raises TypeError."""
        item = ConfidenceItem(name="unsaved1")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "certainty", signal=0.9)

    def test_update_invalid_signal_above_one(self):
        """Signal > 1 raises ValueError."""
        item = ConfidenceItem.create(name="invalid1")
        with pytest.raises(ValueError):
            ConfidenceField.update_confidence(item, "certainty", signal=1.5)

    def test_update_invalid_signal_below_zero(self):
        """Signal < 0 raises ValueError."""
        item = ConfidenceItem.create(name="invalid2")
        with pytest.raises(ValueError):
            ConfidenceField.update_confidence(item, "certainty", signal=-0.1)

    def test_update_signal_none_raises_type_error(self):
        """Signal=None raises TypeError."""
        item = ConfidenceItem.create(name="invalid3")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "certainty", signal=None)

    def test_update_wrong_field_type_raises_type_error(self):
        """update_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceItem.create(name="wrongtype1")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "name", signal=0.9)

    def test_get_confidence_wrong_field_raises_type_error(self):
        """get_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceItem.create(name="wrongtype2")
        with pytest.raises(TypeError):
            ConfidenceField.get_confidence(item, "name")


# --- Entrainment Tests ---


class TestEntrainment:
    def test_acted_corroborates_confidence(self):
        """ObservationProtocol 'acted' outcome increases confidence."""
        item = ConfidenceWithCyclic.create(name="ent1", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "acted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf > initial_conf

    def test_contradicted_decreases_confidence(self):
        """ObservationProtocol 'contradicted' outcome decreases confidence."""
        item = ConfidenceWithCyclic.create(name="ent2", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "contradicted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf < initial_conf

    def test_dismissed_does_not_change_confidence(self):
        """ObservationProtocol 'dismissed' outcome does NOT change confidence."""
        item = ConfidenceWithCyclic.create(name="ent3", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "dismissed"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf == initial_conf

    def test_deferred_does_not_change_confidence(self):
        """ObservationProtocol 'deferred' outcome does NOT change confidence."""
        item = ConfidenceWithCyclic.create(name="ent4", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "deferred"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf == initial_conf

    def test_auto_discharge_on_low_confidence(self):
        """When confidence drops below 0.1, pressure is auto-discharged."""
        import msgpack
        import time

        item = ConfidenceWithCyclic.create(name="ent5", content="test")

        # Build up some pressure by setting last_resolved to far in the past
        rel_field = item._meta.fields["relevance"]
        pressure_hash_key = rel_field.get_pressure_hash_key(item, "relevance")
        member_key = item.db_key.redis_key
        pressure_data = {
            "rate": 0.1,
            "last_resolved": time.time() - 86400 * 30,  # 30 days ago
        }
        POPOTO_REDIS_DB.hset(
            pressure_hash_key, member_key, msgpack.packb(pressure_data)
        )

        # Manually set confidence below 0.1 to trigger auto-discharge
        conf_field = item._meta.fields["certainty"]
        data_hash_key = conf_field.get_data_hash_key(item, "certainty")
        low_conf_data = {
            "confidence": 0.05,
            "evidence_count": 20,
            "corroborations": 0,
            "contradictions": 20,
        }
        POPOTO_REDIS_DB.hset(data_hash_key, member_key, msgpack.packb(low_conf_data))

        # Now trigger contradicted outcome — auto-discharge should fire
        outcome_map = {item.db_key.redis_key: "contradicted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        # Verify pressure was resolved (last_resolved should be recent)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        assert raw is not None
        pdata = msgpack.unpackb(raw, raw=False)
        # last_resolved should be within the last minute (was 30 days ago)
        assert time.time() - pdata["last_resolved"] < 60

    def test_entrainment_without_cyclic_field_is_safe(self):
        """Entrainment on model without CyclicDecayField is a no-op for cycles."""
        item = ConfidenceItem.create(name="ent6")
        # This should not raise
        outcome_map = {item.db_key.redis_key: "acted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)
        # Confidence should still increase
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf > 0.5


# --- Export Test ---


class TestExport:
    def test_confidence_field_exported_from_popoto(self):
        """ConfidenceField is importable from popoto."""
        assert hasattr(popoto, "ConfidenceField")
        assert popoto.ConfidenceField is ConfidenceField
