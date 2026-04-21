"""Tests for PredictionLedgerMixin — outcome tracking with auto-resolution.

Tests cover:
- Basic predict/resolve cycle
- Auto-resolve from each outcome type (acted, dismissed, contradicted)
- Idempotent resolution (resolve twice, second is no-op)
- Error computation for numeric, string, and mixed-type dicts
- ConfidenceField synergy: high error reduces confidence
- EventStreamMixin synergy: resolution produces stream entry
- ObservationProtocol synergy: on_context_used triggers auto-resolve
- Pipeline support: all operations work with pipeline parameter
- Error cases: unsaved instance, None actual, already-resolved, invalid outcome
- get_highest_errors query
- get_prediction_data read
- Empty predicted dict edge case
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from src.popoto.fields.observation import ObservationProtocol  # noqa: E402
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# --- Test Models ---


class PredictionItem(PredictionLedgerMixin, popoto.Model):
    """Basic model with PredictionLedgerMixin."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class PredictionWithConfidence(PredictionLedgerMixin, popoto.Model):
    """Model with PredictionLedger + ConfidenceField for synergy tests."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    certainty = ConfidenceField(initial_confidence=0.8)


class PredictionWithStream(PredictionLedgerMixin, EventStreamMixin, popoto.Model):
    """Model with PredictionLedger + EventStreamMixin for synergy tests."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


class PredictionFull(PredictionLedgerMixin, EventStreamMixin, popoto.Model):
    """Model with PredictionLedger + EventStream + ConfidenceField."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    certainty = ConfidenceField(initial_confidence=0.8)

    _pl_partition = "test_partition"


class PredictionCustomErrors(PredictionLedgerMixin, popoto.Model):
    """Model with custom auto-resolve error values."""

    name = popoto.UniqueKeyField()
    _pl_auto_resolve_errors = {
        "acted": 0.0,
        "dismissed": 0.3,
        "contradicted": 1.0,
    }
    _pl_confidence_error_threshold = 0.5


class PlainModel(popoto.Model):
    """Model without PredictionLedgerMixin for negative tests."""

    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")


# --- Helpers ---


def _cleanup_keys(pattern):
    """Delete all Redis keys matching pattern."""
    for key in POPOTO_REDIS_DB.keys(pattern):
        POPOTO_REDIS_DB.delete(key)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys before and after each test."""
    patterns = [
        "$PL:*",
        "PredictionItem:*",
        "PredictionWithConfidence:*",
        "PredictionWithStream:*",
        "PredictionFull:*",
        "PredictionCustomErrors:*",
        "PlainModel:*",
    ]
    for p in patterns:
        _cleanup_keys(p)
    yield
    for p in patterns:
        _cleanup_keys(p)


# =========================================================================
# Basic predict/resolve cycle
# =========================================================================


class TestBasicPredictResolve:
    def test_record_and_resolve_prediction(self):
        """Basic predict → resolve cycle returns error."""
        item = PredictionItem(name="basic-1", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"relevance": 0.9})
        error = PredictionLedgerMixin.resolve_prediction(
            item, actual={"relevance": 0.3}
        )

        assert error is not None
        # |0.9 - 0.3| / max(0.9, 0.3, 1) = 0.6 / 1.0 = 0.6
        assert abs(error - 0.6) < 0.001

    def test_resolve_marks_resolved(self):
        """After resolution, prediction data shows resolved=True."""
        item = PredictionItem(name="basic-2", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"score": 0.5})
        PredictionLedgerMixin.resolve_prediction(item, actual={"score": 0.5})

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["resolved"] is True
        assert data["resolution_mode"] == "explicit"
        assert data["prediction_error"] is not None

    def test_resolve_records_timestamp(self):
        """Resolution should record resolved_at timestamp."""
        item = PredictionItem(name="basic-ts", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        before = time.time()
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.5})

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        resolved_at = float(data["resolved_at"])
        assert resolved_at >= before - 1.0

    def test_prediction_data_before_resolve(self):
        """get_prediction_data returns unresolved prediction data."""
        item = PredictionItem(name="basic-unresolved", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"val": 42})

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["resolved"] is False
        assert data["predicted"] == {"val": 42}
        assert data["prediction_error"] is None

    def test_no_prediction_data_returns_none(self):
        """get_prediction_data returns None when no prediction exists."""
        item = PredictionItem(name="basic-none", content="test")
        item.save()

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is None


# =========================================================================
# Idempotent resolution
# =========================================================================


class TestIdempotentResolution:
    def test_resolve_twice_is_noop(self):
        """Second resolve call returns None (idempotent)."""
        item = PredictionItem(name="idempotent-1", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        error1 = PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.5})
        error2 = PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.0})

        assert error1 is not None
        assert error2 is None

    def test_auto_resolve_after_explicit_is_noop(self):
        """Auto-resolve after explicit resolve returns None."""
        item = PredictionItem(name="idempotent-2", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.5})

        error = PredictionLedgerMixin.auto_resolve(item, "acted")
        assert error is None


# =========================================================================
# Error computation
# =========================================================================


class TestErrorComputation:
    def test_numeric_error(self):
        """Numeric values use normalized absolute error."""
        error = PredictionLedgerMixin.compute_prediction_error({"x": 0.9}, {"x": 0.3})
        # |0.9 - 0.3| / max(0.9, 0.3, 1) = 0.6
        assert abs(error - 0.6) < 0.001

    def test_numeric_zero_values(self):
        """Zero predicted and actual gives zero error."""
        error = PredictionLedgerMixin.compute_prediction_error({"x": 0.0}, {"x": 0.0})
        assert error == 0.0

    def test_numeric_large_values(self):
        """Large values: denominator is max of abs values."""
        error = PredictionLedgerMixin.compute_prediction_error({"x": 100}, {"x": 50})
        # |100 - 50| / max(100, 50, 1) = 50/100 = 0.5
        assert abs(error - 0.5) < 0.001

    def test_string_match(self):
        """String values: exact match = 0.0."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"s": "hello"}, {"s": "hello"}
        )
        assert error == 0.0

    def test_string_mismatch(self):
        """String values: mismatch = 1.0."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"s": "hello"}, {"s": "world"}
        )
        assert error == 1.0

    def test_missing_key_in_actual(self):
        """Missing key in actual counts as 1.0 error."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"a": 0.5, "b": 0.5}, {"a": 0.5}
        )
        # a: 0.0, b: 1.0 → mean = 0.5
        assert abs(error - 0.5) < 0.001

    def test_missing_key_in_predicted(self):
        """Missing key in predicted counts as 1.0 error."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"a": 0.5}, {"a": 0.5, "b": 0.5}
        )
        assert abs(error - 0.5) < 0.001

    def test_mixed_types(self):
        """Mixed numeric and string values in same dict."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"num": 1.0, "str": "yes"},
            {"num": 0.5, "str": "yes"},
        )
        # num: |1.0-0.5|/max(1.0,0.5,1) = 0.5, str: 0.0 → mean = 0.25
        assert abs(error - 0.25) < 0.001

    def test_empty_dicts(self):
        """Empty dicts produce 0.0 error."""
        error = PredictionLedgerMixin.compute_prediction_error({}, {})
        assert error == 0.0

    def test_perfect_prediction(self):
        """Perfect prediction produces 0.0 error."""
        error = PredictionLedgerMixin.compute_prediction_error(
            {"a": 1.0, "b": "yes"}, {"a": 1.0, "b": "yes"}
        )
        assert error == 0.0


# =========================================================================
# Auto-resolve
# =========================================================================


class TestAutoResolve:
    def test_auto_resolve_acted(self):
        """Auto-resolve with 'acted' outcome uses 0.1 error."""
        item = PredictionItem(name="auto-acted", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        error = PredictionLedgerMixin.auto_resolve(item, "acted")

        assert error == 0.1

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data["resolution_mode"] == "observed"
        assert data["resolved"] is True

    def test_auto_resolve_dismissed(self):
        """Auto-resolve with 'dismissed' outcome uses 0.5 error."""
        item = PredictionItem(name="auto-dismissed", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        error = PredictionLedgerMixin.auto_resolve(item, "dismissed")

        assert error == 0.5

    def test_auto_resolve_contradicted(self):
        """Auto-resolve with 'contradicted' outcome uses 0.9 error."""
        item = PredictionItem(name="auto-contradicted", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        error = PredictionLedgerMixin.auto_resolve(item, "contradicted")

        assert error == 0.9

    def test_auto_resolve_custom_errors(self):
        """Custom _pl_auto_resolve_errors are used."""
        item = PredictionCustomErrors(
            name="auto-custom",
        )
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        error = PredictionLedgerMixin.auto_resolve(item, "acted")

        assert error == 0.0

    def test_auto_resolve_invalid_outcome(self):
        """Invalid outcome raises ValueError."""
        item = PredictionItem(name="auto-invalid", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        with pytest.raises(ValueError, match="Invalid outcome"):
            PredictionLedgerMixin.auto_resolve(item, "unknown")

    def test_auto_resolve_no_prediction(self):
        """Auto-resolve with no prediction returns None."""
        item = PredictionItem(name="auto-nopred", content="test")
        item.save()

        error = PredictionLedgerMixin.auto_resolve(item, "acted")
        assert error is None


# =========================================================================
# ConfidenceField synergy
# =========================================================================


class TestConfidenceSynergy:
    def test_high_error_reduces_confidence(self):
        """Error above threshold triggers confidence reduction."""
        item = PredictionWithConfidence(name="conf-high-err", content="test")
        item.save()

        # Read initial confidence
        initial = ConfidenceField.get_confidence(item, "certainty")

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        # Resolve with high error (threshold default is 0.7)
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.0})

        updated = ConfidenceField.get_confidence(item, "certainty")
        assert updated < initial

    def test_low_error_no_confidence_change(self):
        """Error below threshold does not reduce confidence."""
        item = PredictionWithConfidence(name="conf-low-err", content="test")
        item.save()

        initial = ConfidenceField.get_confidence(item, "certainty")

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 0.5})
        # Resolve with low error (0.1 < threshold of 0.7)
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.4})

        updated = ConfidenceField.get_confidence(item, "certainty")
        assert updated == initial

    def test_auto_resolve_contradicted_reduces_confidence(self):
        """Auto-resolve 'contradicted' (0.9 error) reduces confidence."""
        item = PredictionWithConfidence(name="conf-contradicted", content="test")
        item.save()

        initial = ConfidenceField.get_confidence(item, "certainty")

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        PredictionLedgerMixin.auto_resolve(item, "contradicted")

        updated = ConfidenceField.get_confidence(item, "certainty")
        assert updated < initial

    def test_no_confidence_field_graceful(self):
        """Model without ConfidenceField: high error doesn't crash."""
        item = PredictionItem(name="conf-none", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        # Should not raise even with high error
        error = PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.0})
        assert error is not None


# =========================================================================
# EventStreamMixin synergy
# =========================================================================


class TestEventStreamSynergy:
    def test_resolution_produces_stream_entry(self):
        """Explicit resolve produces a prediction_resolved stream entry."""
        item = PredictionWithStream(name="stream-1", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 0.9})
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.3})

        # Read stream entries
        stream_key = item._get_stream_key()
        entries = POPOTO_REDIS_DB.xrange(stream_key)

        # Find prediction_resolved entry (skip create entry from save)
        resolved_entries = [
            e
            for e in entries
            if e[1].get(b"op") == b"prediction_resolved"
            or e[1].get("op") == "prediction_resolved"
        ]
        assert len(resolved_entries) >= 1

    def test_auto_resolve_produces_stream_entry(self):
        """Auto-resolve produces a prediction_resolved stream entry."""
        item = PredictionWithStream(name="stream-2", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 0.5})
        PredictionLedgerMixin.auto_resolve(item, "dismissed")

        stream_key = item._get_stream_key()
        entries = POPOTO_REDIS_DB.xrange(stream_key)

        resolved_entries = [
            e
            for e in entries
            if e[1].get(b"op") == b"prediction_resolved"
            or e[1].get("op") == "prediction_resolved"
        ]
        assert len(resolved_entries) >= 1

    def test_no_event_stream_graceful(self):
        """Model without EventStreamMixin: resolution doesn't crash."""
        item = PredictionItem(name="stream-none", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        # Should not raise
        error = PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.0})
        assert error is not None


# =========================================================================
# ObservationProtocol synergy
# =========================================================================


class TestObservationProtocolSynergy:
    def test_acted_triggers_auto_resolve(self):
        """ObservationProtocol 'acted' outcome triggers auto-resolve."""
        item = PredictionItem(name="obs-acted", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        outcome_map = {item.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["resolved"] is True
        assert data["resolution_mode"] == "observed"

    def test_dismissed_triggers_auto_resolve(self):
        """ObservationProtocol 'dismissed' outcome triggers auto-resolve."""
        item = PredictionItem(name="obs-dismissed", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        outcome_map = {item.db_key.redis_key: "dismissed"}
        ObservationProtocol.on_context_used([item], outcome_map)

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["resolved"] is True

    def test_contradicted_triggers_auto_resolve(self):
        """ObservationProtocol 'contradicted' outcome triggers auto-resolve."""
        item = PredictionItem(name="obs-contradicted", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        outcome_map = {item.db_key.redis_key: "contradicted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["resolved"] is True

    def test_deferred_does_not_trigger_auto_resolve(self):
        """ObservationProtocol 'deferred' does NOT trigger auto-resolve."""
        item = PredictionItem(name="obs-deferred", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        outcome_map = {item.db_key.redis_key: "deferred"}
        ObservationProtocol.on_context_used([item], outcome_map)

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        # Deferred does not call auto_resolve, so prediction stays unresolved
        assert data["resolved"] is False

    def test_no_prediction_observation_graceful(self):
        """ObservationProtocol outcome with no recorded prediction is fine."""
        item = PredictionItem(name="obs-nopred", content="test")
        item.save()

        outcome_map = {item.db_key.redis_key: "acted"}
        # Should not raise
        ObservationProtocol.on_context_used([item], outcome_map)

    def test_non_prediction_model_unaffected(self):
        """Model without PredictionLedgerMixin is unaffected by observation."""
        item = PlainModel(name="obs-plain", content="test")
        item.save()

        outcome_map = {item.db_key.redis_key: "acted"}
        # Should not raise
        ObservationProtocol.on_context_used([item], outcome_map)


# =========================================================================
# get_highest_errors
# =========================================================================


class TestGetHighestErrors:
    def test_query_highest_errors(self):
        """get_highest_errors returns instances sorted by descending error."""
        items = []
        for i, err_val in enumerate([0.2, 0.8, 0.5]):
            item = PredictionItem(name=f"errors-{i}", content="test")
            item.save()
            items.append(item)

            PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        # Resolve with different actual values to get different errors
        PredictionLedgerMixin.resolve_prediction(
            items[0], actual={"x": 0.8}
        )  # low error
        PredictionLedgerMixin.resolve_prediction(
            items[1], actual={"x": 0.0}
        )  # high error
        PredictionLedgerMixin.resolve_prediction(
            items[2], actual={"x": 0.5}
        )  # medium error

        results = PredictionLedgerMixin.get_highest_errors(PredictionItem)
        assert len(results) == 3

        # Should be sorted by descending error
        errors = [score for _, score in results]
        assert errors == sorted(errors, reverse=True)

    def test_query_with_limit(self):
        """get_highest_errors respects limit parameter."""
        for i in range(5):
            item = PredictionItem(name=f"limit-{i}", content="test")
            item.save()
            PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
            PredictionLedgerMixin.resolve_prediction(item, actual={"x": float(i) / 5})

        results = PredictionLedgerMixin.get_highest_errors(PredictionItem, limit=3)
        assert len(results) == 3

    def test_empty_error_set(self):
        """get_highest_errors returns empty list when no errors exist."""
        results = PredictionLedgerMixin.get_highest_errors(PredictionItem)
        assert results == []

    def test_custom_partition(self):
        """get_highest_errors with custom partition."""
        item = PredictionFull(name="part-1", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
        PredictionLedgerMixin.resolve_prediction(item, actual={"x": 0.0})

        # Query the custom partition
        results = PredictionLedgerMixin.get_highest_errors(
            PredictionFull, partition="test_partition"
        )
        assert len(results) == 1

        # Default partition should be empty
        results_default = PredictionLedgerMixin.get_highest_errors(
            PredictionFull, partition="default"
        )
        assert len(results_default) == 0


# =========================================================================
# Pipeline support
# =========================================================================


class TestPipelineSupport:
    def test_record_prediction_with_pipeline(self):
        """record_prediction works with a Redis pipeline."""
        item = PredictionItem(name="pipe-record", content="test")
        item.save()

        pipe = POPOTO_REDIS_DB.pipeline()
        PredictionLedgerMixin.record_prediction(
            item, predicted={"x": 0.5}, pipeline=pipe
        )
        pipe.execute()

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["predicted"] == {"x": 0.5}


# =========================================================================
# Error cases
# =========================================================================


class TestErrorCases:
    def test_record_unsaved_instance_raises(self):
        """record_prediction on unsaved instance raises TypeError."""
        item = PredictionItem(name="unsaved-record")

        with pytest.raises(TypeError, match="saved model instance"):
            PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

    def test_resolve_unsaved_instance_raises(self):
        """resolve_prediction on unsaved instance raises TypeError."""
        item = PredictionItem(name="unsaved-resolve")

        with pytest.raises(TypeError, match="saved model instance"):
            PredictionLedgerMixin.resolve_prediction(item, actual={"x": 1.0})

    def test_record_none_predicted_raises(self):
        """record_prediction with None predicted raises ValueError."""
        item = PredictionItem(name="none-pred", content="test")
        item.save()

        with pytest.raises(ValueError, match="predicted must not be None"):
            PredictionLedgerMixin.record_prediction(item, predicted=None)

    def test_resolve_none_actual_raises(self):
        """resolve_prediction with None actual raises ValueError."""
        item = PredictionItem(name="none-actual", content="test")
        item.save()

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        with pytest.raises(ValueError, match="actual must not be None"):
            PredictionLedgerMixin.resolve_prediction(item, actual=None)

    def test_resolve_no_prediction_returns_none(self):
        """resolve_prediction with no recorded prediction returns None."""
        item = PredictionItem(name="no-pred-resolve", content="test")
        item.save()

        error = PredictionLedgerMixin.resolve_prediction(item, actual={"x": 1.0})
        assert error is None

    def test_record_empty_predicted_allowed(self):
        """record_prediction with empty dict is allowed."""
        item = PredictionItem(name="empty-pred", content="test")
        item.save()

        # Should not raise
        PredictionLedgerMixin.record_prediction(item, predicted={})

        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is not None
        assert data["predicted"] == {}

    def test_get_prediction_data_unsaved(self):
        """get_prediction_data on unsaved instance returns None."""
        item = PredictionItem(name="unsaved-data")
        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data is None


# =========================================================================
# Redis key patterns
# =========================================================================


class TestRedisKeyPatterns:
    def test_meta_key_format(self):
        """Meta key follows $PL:{ClassName}:meta:{pk} pattern."""
        item = PredictionItem(name="key-test", content="test")
        item.save()

        meta_key = PredictionLedgerMixin._meta_key(item)
        assert meta_key.startswith("$PL:PredictionItem:meta:")

    def test_error_key_format(self):
        """Error key follows $PL:{ClassName}:errors:{partition} pattern."""
        error_key = PredictionLedgerMixin._error_key(PredictionItem, "default")
        assert error_key == "$PL:PredictionItem:errors:default"

    def test_error_key_from_instance(self):
        """Error key works with instance (not just class)."""
        item = PredictionItem(name="key-inst", content="test")
        item.save()

        error_key = PredictionLedgerMixin._error_key(item, "custom")
        assert error_key == "$PL:PredictionItem:errors:custom"


# =========================================================================
# Full integration: PredictionLedger + Confidence + EventStream
# =========================================================================


class TestFullIntegration:
    def test_full_predict_resolve_cycle(self):
        """Full cycle with ConfidenceField + EventStream + error tracking."""
        item = PredictionFull(name="full-1", content="test")
        item.save()

        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        # Record prediction
        PredictionLedgerMixin.record_prediction(
            item, predicted={"score": 0.9, "category": "positive"}
        )

        # Resolve with high error (contradicting prediction)
        error = PredictionLedgerMixin.resolve_prediction(
            item, actual={"score": 0.1, "category": "negative"}
        )

        assert error is not None
        assert error > 0.7  # High error

        # Confidence should decrease
        updated_conf = ConfidenceField.get_confidence(item, "certainty")
        assert updated_conf < initial_conf

        # EventStream should have entry
        stream_key = item._get_stream_key()
        entries = POPOTO_REDIS_DB.xrange(stream_key)
        resolved_entries = [
            e
            for e in entries
            if e[1].get(b"op") == b"prediction_resolved"
            or e[1].get("op") == "prediction_resolved"
        ]
        assert len(resolved_entries) >= 1

        # Error sorted set should have entry
        results = PredictionLedgerMixin.get_highest_errors(
            PredictionFull, partition="test_partition"
        )
        assert len(results) == 1

    def test_observation_protocol_full_synergy(self):
        """ObservationProtocol → auto-resolve → confidence feedback → event stream."""
        item = PredictionFull(name="full-obs", content="test")
        item.save()

        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})

        # Contradicted via ObservationProtocol triggers auto-resolve
        outcome_map = {item.db_key.redis_key: "contradicted"}
        ObservationProtocol.on_context_used([item], outcome_map)

        # Should be resolved
        data = PredictionLedgerMixin.get_prediction_data(item)
        assert data["resolved"] is True
        assert data["resolution_mode"] == "observed"

        # Confidence should decrease (0.9 error > 0.7 threshold)
        updated_conf = ConfidenceField.get_confidence(item, "certainty")
        assert updated_conf < initial_conf


# =========================================================================
# error_summary — grouped prediction-error aggregation (#352 Tier 2)
# =========================================================================


class TestErrorSummary:
    """Tests for PredictionLedgerMixin.error_summary."""

    def _seed_errors(self, n=6):
        """Create N resolved predictions with varying errors."""
        items = []
        for i in range(n):
            item = PredictionItem(name=f"err-{i}", content=f"c-{i}")
            item.save()
            PredictionLedgerMixin.record_prediction(item, predicted={"x": 1.0})
            # Resolve with varying actual values to get varying errors.
            # Use "dismissed" outcome (error=0.5) for odd indices, "contradicted"
            # (error=0.9) for even — to get a mix of error values.
            outcome = "contradicted" if i % 2 == 0 else "dismissed"
            PredictionLedgerMixin.auto_resolve(item, outcome)
            items.append(item)
        return items

    def test_error_summary_empty_set_returns_zero_count(self):
        """error_summary on a model with zero errors returns count=0."""
        result = PredictionLedgerMixin.error_summary(PredictionItem)
        assert "__all__" in result
        assert result["__all__"]["count"] == 0
        assert result["__all__"]["mean"] == 0.0

    def test_error_summary_ungrouped_stats(self):
        """Without group_by, returns overall stats under __all__."""
        self._seed_errors(n=6)
        result = PredictionLedgerMixin.error_summary(PredictionItem)
        assert "__all__" in result
        stats = result["__all__"]
        assert stats["count"] == 6
        assert 0.0 <= stats["mean"] <= 1.0
        assert stats["max"] >= stats["mean"]
        # We alternated 0.9 / 0.5; mean should be ~0.7
        assert abs(stats["mean"] - 0.7) < 0.01

    def test_error_summary_callable_group_by(self):
        """Callable group_by produces per-group stats keyed by the label."""
        self._seed_errors(n=6)

        def _high_low(member_key, error):
            return "high" if error >= 0.7 else "low"

        result = PredictionLedgerMixin.error_summary(
            PredictionItem,
            group_by=_high_low,
        )
        assert "high" in result
        assert "low" in result
        assert result["high"]["count"] == 3
        assert result["low"]["count"] == 3
        assert abs(result["high"]["mean"] - 0.9) < 1e-6
        assert abs(result["low"]["mean"] - 0.5) < 1e-6

    def test_error_summary_builtin_hour(self):
        """Built-in 'hour' bucketer groups by resolved_at hour."""
        self._seed_errors(n=4)
        result = PredictionLedgerMixin.error_summary(
            PredictionItem, group_by="hour"
        )
        # Every entry was resolved "now", so a single hour bucket is expected.
        # The exact hour depends on the test runner's clock; we just assert
        # there's at least one bucket with count == 4.
        assert result
        totals = sum(g["count"] for g in result.values())
        assert totals == 4

    def test_error_summary_builtin_weekday(self):
        """Built-in 'weekday' bucketer groups by resolved_at weekday 0..6."""
        self._seed_errors(n=3)
        result = PredictionLedgerMixin.error_summary(
            PredictionItem, group_by="weekday"
        )
        # Single weekday bucket (all resolved "now") with all 3 items.
        assert result
        totals = sum(g["count"] for g in result.values())
        assert totals == 3
        # Keys should be ints 0..6
        for label in result.keys():
            assert isinstance(label, int)
            assert 0 <= label <= 6

    def test_error_summary_builtin_day(self):
        """Built-in 'day' bucketer groups by resolved_at ISO date."""
        self._seed_errors(n=3)
        result = PredictionLedgerMixin.error_summary(
            PredictionItem, group_by="day"
        )
        assert result
        for label in result.keys():
            # ISO date format: YYYY-MM-DD
            assert isinstance(label, str)
            assert len(label) == 10
            assert label[4] == "-" and label[7] == "-"

    def test_error_summary_unknown_builtin_raises_value_error(self):
        """Unknown group_by string raises ValueError listing known bucketers."""
        with pytest.raises(ValueError) as exc:
            PredictionLedgerMixin.error_summary(PredictionItem, group_by="unknown")
        msg = str(exc.value)
        assert "hour" in msg
        assert "weekday" in msg
        assert "day" in msg

    def test_error_summary_limit_caps_sample_size(self):
        """limit parameter caps the number of members sampled."""
        self._seed_errors(n=6)
        result = PredictionLedgerMixin.error_summary(PredictionItem, limit=3)
        assert result["__all__"]["count"] == 3
