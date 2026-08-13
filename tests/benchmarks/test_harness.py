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
    leaderboard_parity_slice,
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


class TestLeaderboardParitySlice:
    """Pure re-aggregation of a by_question_type breakdown (issue #454)."""

    # A minimal 3-category breakdown; metric values chosen so the n-weighted
    # means are exact and hand-checkable.
    BREAKDOWN = {
        1: {
            "n": 100,
            "recall_at_1": 0.4,
            "recall_at_5": 0.6,
            "recall_at_10": 0.7,
            "mrr": 0.5,
        },
        4: {
            "n": 300,
            "recall_at_1": 0.2,
            "recall_at_5": 0.4,
            "recall_at_10": 0.5,
            "mrr": 0.3,
        },
        5: {
            "n": 100,
            "recall_at_1": 0.9,
            "recall_at_5": 0.9,
            "recall_at_10": 0.9,
            "mrr": 0.9,
        },
    }

    def test_excludes_default_cat5_and_reweights(self):
        # Retained cats 1 (n=100) + 4 (n=300): R@1 = (100*0.4 + 300*0.2)/400 = 0.25
        out = leaderboard_parity_slice(self.BREAKDOWN)
        assert out["excluded"] == ["5"]
        assert out["n"] == 400
        assert out["recall_at_1"] == pytest.approx(0.25)
        assert out["recall_at_5"] == pytest.approx(0.45)
        assert out["recall_at_10"] == pytest.approx(0.55)
        assert out["mrr"] == pytest.approx(0.35)

    def test_string_keys_match_int_keys(self):
        # A JSON round-trip stringifies category keys; the slice must be stable.
        str_breakdown = {str(k): v for k, v in self.BREAKDOWN.items()}
        assert leaderboard_parity_slice(str_breakdown) == leaderboard_parity_slice(
            self.BREAKDOWN
        )

    def test_no_excluded_category_present_is_full_aggregate(self):
        # Excluding a category that is not present retains everyone.
        no_cat5 = {k: v for k, v in self.BREAKDOWN.items() if k != 5}
        out = leaderboard_parity_slice(no_cat5)
        assert out["excluded"] == []
        assert out["n"] == 400

    def test_multiple_exclusions(self):
        out = leaderboard_parity_slice(self.BREAKDOWN, exclude_categories=("4", "5"))
        assert out["excluded"] == ["4", "5"]
        assert out["n"] == 100
        assert out["recall_at_1"] == pytest.approx(0.4)

    def test_all_excluded_yields_zero(self):
        out = leaderboard_parity_slice(
            self.BREAKDOWN, exclude_categories=("1", "4", "5")
        )
        assert out["n"] == 0
        assert out["recall_at_1"] == 0.0
        assert out["mrr"] == 0.0


class TestLoCoMoParityRegression:
    """Pin the published LoCoMo leaderboard-parity numbers (issue #454).

    The 4-category slice is the standing published artifact; these assertions
    are the regression gate so the docs numbers cannot silently drift from the
    committed benchmark artifacts.
    """

    RESULTS_DIR = os.path.join(SCRIPT_DIR, "results", "external")

    def _slice(self, filename):
        import json

        path = os.path.join(self.RESULTS_DIR, filename)
        if not os.path.exists(path):
            pytest.skip(f"committed artifact missing: {filename}")
        with open(path) as fh:
            data = json.load(fh)
        return leaderboard_parity_slice(data["by_question_type"])

    def test_lexical_parity_slice_is_1540_qa(self):
        """Corrected (gold-blind) lexical parity numbers, issue #514.

        Re-pinned from 0.2883 / 0.5390 / 0.6260 / 0.3991 — those came from
        ``locomo_20260708.json``, scored by the gold-aware ID selection that
        #514 removed. ``locomo_latest`` now resolves to ``locomo_20260807``.
        """
        out = self._slice("locomo_latest.json")
        assert out["excluded"] == ["5"]
        assert out["n"] == 1540  # 1986 full − 446 cat-5 = exact leaderboard variant
        assert out["recall_at_1"] == pytest.approx(0.2877, abs=1e-4)
        assert out["recall_at_5"] == pytest.approx(0.5130, abs=1e-4)
        assert out["recall_at_10"] == pytest.approx(0.5877, abs=1e-4)
        assert out["mrr"] == pytest.approx(0.3875, abs=1e-4)

    def test_hybrid_parity_slice_is_a_labelled_sample(self):
        """Hybrid parity numbers, gold-blind scoring on a 250-question sample.

        ``locomo_latest_hybrid`` is NOT a full-1986 run. A full hybrid pass
        re-embeds ~1.19M records and measured ~5.2 h on the reference machine,
        so #530 re-ran it as a 250-question stratified sample (seed 0) under
        gold-blind scoring rather than leaving the pre-#514 full run standing.
        The parity slice is therefore 194 questions, not 1540; asserting the
        sampled n is deliberate, so a later full re-run trips this test instead
        of silently swapping a sample for a full run (or vice versa).

        Re-pinned from 0.1552 / 0.4065 / 0.5181 / 0.2686 (``locomo_20260708``
        = full 1986, pre-#514 scoring, unweighted RRF).
        """
        out = self._slice("locomo_latest_hybrid.json")
        assert out["excluded"] == ["5"]
        assert out["n"] == 194  # 250 sampled − 56 cat-5
        assert out["recall_at_1"] == pytest.approx(0.3041, abs=1e-4)
        assert out["recall_at_5"] == pytest.approx(0.4846, abs=1e-4)
        assert out["recall_at_10"] == pytest.approx(0.5619, abs=1e-4)
        assert out["mrr"] == pytest.approx(0.3836, abs=1e-4)


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
