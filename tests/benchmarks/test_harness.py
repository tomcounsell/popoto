"""Tests for the benchmark harness framework.

Validates that scenarios run, produce valid metrics, and handle edge cases.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import math

import pytest

from tests.benchmarks.metrics.retrieval import (
    calibration_error,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
)
from tests.benchmarks.overrides import apply_overrides, is_degenerate
from tests.benchmarks.scenarios.base import Scenario, ScenarioResult
from tests.benchmarks.scenarios.factual_recall import FactualRecallScenario
from tests.benchmarks.scenarios.multi_step_reasoning import (
    MultiStepReasoningScenario,
)
from tests.benchmarks.scenarios.temporal_scheduling import (
    TemporalSchedulingScenario,
)

# ---------------------------------------------------------------------------
# Metric unit tests
# ---------------------------------------------------------------------------


class TestPrecisionAtK:
    def test_perfect_precision(self):
        assert precision_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == 1.0

    def test_zero_precision(self):
        assert precision_at_k(["x", "y", "z"], {"a", "b", "c"}, 3) == 0.0

    def test_partial_precision(self):
        assert precision_at_k(["a", "x", "b"], {"a", "b", "c"}, 3) == pytest.approx(
            2 / 3
        )

    def test_k_larger_than_retrieved(self):
        assert precision_at_k(["a"], {"a", "b"}, 5) == pytest.approx(1 / 5)

    def test_empty_retrieved(self):
        assert precision_at_k([], {"a"}, 5) == 0.0

    def test_k_zero(self):
        assert precision_at_k(["a"], {"a"}, 0) == 0.0


class TestNDCG:
    def test_perfect_ranking(self):
        scores = {"a": 3.0, "b": 2.0, "c": 1.0}
        assert ndcg_at_k(["a", "b", "c"], scores, 3) == pytest.approx(1.0)

    def test_reversed_ranking(self):
        scores = {"a": 3.0, "b": 2.0, "c": 1.0}
        result = ndcg_at_k(["c", "b", "a"], scores, 3)
        assert result < 1.0
        assert result > 0.0

    def test_empty(self):
        assert ndcg_at_k([], {"a": 1.0}, 3) == 0.0

    def test_no_scores(self):
        assert ndcg_at_k(["a", "b"], {}, 3) == 0.0


class TestCalibrationError:
    def test_perfect_calibration(self):
        # All predictions = 1.0, all outcomes True
        preds = [0.95] * 10
        outcomes = [True] * 10
        assert calibration_error(preds, outcomes) < 0.1

    def test_poor_calibration(self):
        # High predictions, no positive outcomes
        preds = [0.9] * 10
        outcomes = [False] * 10
        assert calibration_error(preds, outcomes) > 0.5

    def test_empty(self):
        assert calibration_error([], []) == 0.0

    def test_mismatched_lengths(self):
        with pytest.raises(ValueError):
            calibration_error([0.5], [True, False])


class TestMRR:
    def test_first_hit(self):
        assert mean_reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0

    def test_second_hit(self):
        assert mean_reciprocal_rank(["x", "a", "c"], {"a"}) == 0.5

    def test_no_hit(self):
        assert mean_reciprocal_rank(["x", "y", "z"], {"a"}) == 0.0


# ---------------------------------------------------------------------------
# Override injection tests
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_module_constant_override(self):
        import src.popoto.recipes.policy_cache as pc

        original = pc.TD_ALPHA
        with apply_overrides({"TD_ALPHA": 0.99}):
            assert pc.TD_ALPHA == 0.99
        assert pc.TD_ALPHA == original

    def test_observation_constant_override(self):
        import src.popoto.fields.observation as obs

        original = obs.ACTED_CONFIDENCE_SIGNAL
        with apply_overrides({"ACTED_CONFIDENCE_SIGNAL": 0.5}):
            assert obs.ACTED_CONFIDENCE_SIGNAL == 0.5
        assert obs.ACTED_CONFIDENCE_SIGNAL == original

    def test_degenerate_detection(self):
        assert is_degenerate("decay_rate", 0.0) is True
        assert is_degenerate("decay_rate", 0.5) is False
        assert is_degenerate("TD_GAMMA", 1.0) is True
        assert is_degenerate("TD_GAMMA", 0.95) is False

    def test_unknown_constant_not_degenerate(self):
        assert is_degenerate("unknown_constant", 0.0) is False


# ---------------------------------------------------------------------------
# Scenario execution tests
# ---------------------------------------------------------------------------


class TestFactualRecallScenario:
    def test_default_parameters(self):
        scenario = FactualRecallScenario()
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"
        assert len(result.retrieved_ids) > 0
        assert len(result.relevant_ids) > 0
        assert result.duration_ms > 0

    def test_with_overrides(self):
        scenario = FactualRecallScenario(
            overrides={"decay_rate": 0.3, "initial_confidence": 0.7}
        )
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"

    def test_metrics_computable(self):
        scenario = FactualRecallScenario()
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"

        p5 = precision_at_k(result.retrieved_ids, result.relevant_ids, 5)
        assert 0.0 <= p5 <= 1.0

        ndcg = ndcg_at_k(result.retrieved_ids, result.relevance_scores, 5)
        assert 0.0 <= ndcg <= 1.0

        if result.predictions and result.outcomes:
            ece = calibration_error(result.predictions, result.outcomes)
            assert 0.0 <= ece <= 1.0


class TestMultiStepReasoningScenario:
    def test_default_parameters(self):
        scenario = MultiStepReasoningScenario()
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"
        assert len(result.retrieved_ids) > 0

    def test_with_co_occurrence_overrides(self):
        scenario = MultiStepReasoningScenario(
            overrides={"initial_weight": 0.2, "delta": 0.1, "decay_per_hop": 0.7}
        )
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"

    def test_propagation_finds_chain(self):
        scenario = MultiStepReasoningScenario()
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"
        # Co-occurrence propagation should find at least some chain items
        assert result.metadata.get("n_propagated", 0) > 0


class TestTemporalSchedulingScenario:
    def test_default_parameters(self):
        scenario = TemporalSchedulingScenario()
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"
        assert len(result.retrieved_ids) > 0

    def test_with_decay_override(self):
        scenario = TemporalSchedulingScenario(overrides={"decay_rate": 0.3})
        result = scenario.execute()
        assert result.status == "ok", f"Scenario failed: {result.error_message}"


# ---------------------------------------------------------------------------
# ScenarioResult tests
# ---------------------------------------------------------------------------


class TestScenarioResult:
    def test_default_values(self):
        r = ScenarioResult()
        assert r.status == "ok"
        assert r.retrieved_ids == []
        assert r.relevant_ids == set()
        assert r.duration_ms == 0.0

    def test_error_result(self):
        r = ScenarioResult(status="error", error_message="test error")
        assert r.status == "error"
        assert r.error_message == "test error"
