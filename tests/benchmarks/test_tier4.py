"""Tests for Tier 4 SubconsciousMemory recipe-layer benchmarks.

Validates that fixture data loads correctly, new metrics produce
correct values, and recipe-layer scenarios run without errors.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from tests.benchmarks.fixtures import *  # noqa: ensure fixtures dir is importable
from tests.benchmarks.metrics.extraction import extraction_f1
from tests.benchmarks.metrics.token_efficiency import (
    importance_distribution_health,
    token_utilization_ratio,
)
from tests.benchmarks.scenarios.recipe_base import load_fixture, FIXTURES_DIR

# ---------------------------------------------------------------------------
# Fixture loading tests
# ---------------------------------------------------------------------------


class TestFixtureLoading:
    """Verify fixture files load and validate correctly."""

    @pytest.mark.parametrize(
        "filename",
        ["support_agent.json", "coding_assistant.json", "research_agent.json"],
    )
    def test_fixture_loads(self, filename):
        data = load_fixture(filename)
        assert "scenario" in data
        assert "turns" in data
        assert "ground_truth" in data
        assert "sentences" in data["ground_truth"]
        assert "queries" in data["ground_truth"]

    @pytest.mark.parametrize(
        "filename",
        ["support_agent.json", "coding_assistant.json", "research_agent.json"],
    )
    def test_fixture_has_turns(self, filename):
        data = load_fixture(filename)
        assert len(data["turns"]) >= 4
        for turn in data["turns"]:
            assert "role" in turn
            assert "content" in turn
            assert turn["role"] in ("user", "assistant")

    @pytest.mark.parametrize(
        "filename",
        ["support_agent.json", "coding_assistant.json", "research_agent.json"],
    )
    def test_fixture_has_labeled_sentences(self, filename):
        data = load_fixture(filename)
        sentences = data["ground_truth"]["sentences"]
        assert len(sentences) >= 5
        for s in sentences:
            assert "text" in s
            assert "label" in s
            assert s["label"] in ("meaningful", "noise")

    @pytest.mark.parametrize(
        "filename",
        ["support_agent.json", "coding_assistant.json", "research_agent.json"],
    )
    def test_fixture_has_queries(self, filename):
        data = load_fixture(filename)
        queries = data["ground_truth"]["queries"]
        assert len(queries) >= 2
        for q_name, q_data in queries.items():
            assert "text" in q_data
            assert "relevant_sentences" in q_data
            assert len(q_data["relevant_sentences"]) >= 1

    def test_fixture_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            load_fixture("nonexistent.json")

    def test_three_fixtures_exist(self):
        """Verify all 3 scenario fixtures exist in the fixtures directory."""
        expected = {
            "support_agent.json",
            "coding_assistant.json",
            "research_agent.json",
        }
        actual = {f.name for f in FIXTURES_DIR.iterdir() if f.suffix == ".json"}
        assert expected.issubset(actual), f"Missing fixtures: {expected - actual}"


# ---------------------------------------------------------------------------
# Extraction metrics tests
# ---------------------------------------------------------------------------


class TestExtractionF1:
    """Verify extraction F1 metric correctness."""

    def test_perfect_extraction(self):
        ground_truth = [
            {"text": "The capital of France is Paris", "label": "meaningful"},
            {"text": "Hello there", "label": "noise"},
        ]
        extracted = ["The capital of France is Paris"]
        result = extraction_f1(extracted, ground_truth)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0

    def test_no_extraction(self):
        ground_truth = [
            {"text": "The capital of France is Paris", "label": "meaningful"},
        ]
        result = extraction_f1([], ground_truth)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0
        assert result["false_negatives"] == 1

    def test_noise_only_extraction(self):
        ground_truth = [
            {"text": "The capital of France is Paris", "label": "meaningful"},
            {"text": "Hello there", "label": "noise"},
        ]
        extracted = ["Hello there"]
        result = extraction_f1(extracted, ground_truth)
        assert result["precision"] == 0.0
        assert result["false_positives"] == 1

    def test_partial_match(self):
        ground_truth = [
            {"text": "The capital of France is Paris", "label": "meaningful"},
            {"text": "Water boils at 100 degrees", "label": "meaningful"},
            {"text": "Hello there", "label": "noise"},
        ]
        extracted = ["The capital of France is Paris"]
        result = extraction_f1(extracted, ground_truth)
        assert result["true_positives"] == 1
        assert result["false_negatives"] == 1
        assert result["precision"] == 1.0
        assert result["recall"] == 0.5


# ---------------------------------------------------------------------------
# Token efficiency metrics tests
# ---------------------------------------------------------------------------


class TestTokenUtilization:
    """Verify token utilization metric correctness."""

    def test_empty_records(self):
        result = token_utilization_ratio([], {})
        assert result["utilization_ratio"] == 0.0

    def test_all_high_relevance(self):
        records = ["record_a", "record_b", "record_c"]
        relevance = {"record_a": 0.9, "record_b": 0.8, "record_c": 0.7}
        result = token_utilization_ratio(records, relevance, key_fn=lambda r: r)
        # Median is 0.8, so record_a (0.9) and record_b (0.8) are >= median
        assert result["utilization_ratio"] > 0.5

    def test_mixed_relevance(self):
        records = ["high_relevance_record", "low_relevance_record"]
        relevance = {"high_relevance_record": 0.9, "low_relevance_record": 0.1}
        # Use explicit token counter so both records have equal weight
        result = token_utilization_ratio(
            records,
            relevance,
            key_fn=lambda r: r,
            token_counter=lambda r: 100,
        )
        # Median is 0.5, "high" is above, "low" is below
        assert result["utilization_ratio"] == 0.5
        assert result["total_tokens"] == 200


class TestImportanceDistribution:
    """Verify importance distribution health metric."""

    def test_empty_scores(self):
        result = importance_distribution_health([])
        assert result["std_dev"] == 0.0
        assert result["distinct_ranks"] == 0

    def test_uniform_scores(self):
        result = importance_distribution_health([0.5, 0.5, 0.5, 0.5])
        assert result["std_dev"] == 0.0
        assert result["distinct_ranks"] == 1
        assert result["rank_ratio"] == 0.25

    def test_varied_scores(self):
        result = importance_distribution_health([0.1, 0.3, 0.5, 0.7, 0.9])
        assert result["std_dev"] > 0.2
        assert result["distinct_ranks"] == 5
        assert result["rank_ratio"] == 1.0

    def test_single_score(self):
        result = importance_distribution_health([0.5])
        assert result["std_dev"] == 0.0
        assert result["n_scores"] == 1


# ---------------------------------------------------------------------------
# Recipe scenario lifecycle tests
# ---------------------------------------------------------------------------


class TestRecipeScenarioLifecycle:
    """Test that recipe-layer scenarios run through the full lifecycle."""

    @pytest.mark.parametrize(
        "scenario_class_path",
        [
            "tests.benchmarks.scenarios.support_agent.SupportAgentScenario",
            "tests.benchmarks.scenarios.coding_assistant.CodingAssistantScenario",
            "tests.benchmarks.scenarios.research_agent.ResearchAgentScenario",
        ],
    )
    def test_scenario_executes(self, scenario_class_path):
        """Each scenario should execute() without error."""
        # Import dynamically to isolate failures
        module_path, class_name = scenario_class_path.rsplit(".", 1)
        import importlib

        mod = importlib.import_module(module_path)
        scenario_class = getattr(mod, class_name)

        scenario = scenario_class()
        result = scenario.execute()

        assert (
            result.status == "ok"
        ), f"Scenario {class_name} failed: {result.error_message}"
        assert result.metadata.get("n_memories_saved", 0) > 0
        assert result.metadata.get("n_extraction_rounds", 0) > 0

    def test_scenario_with_overrides(self):
        """Scenario should accept and use overrides."""
        from tests.benchmarks.scenarios.support_agent import SupportAgentScenario

        scenario = SupportAgentScenario(
            overrides={"extraction_min_length": 20, "max_items": 5}
        )
        result = scenario.execute()
        assert result.status == "ok", f"Failed: {result.error_message}"

    def test_scenario_degenerate_max_items_zero(self):
        """max_items=0 should produce a valid result (possibly empty)."""
        from tests.benchmarks.scenarios.support_agent import SupportAgentScenario

        scenario = SupportAgentScenario(overrides={"max_items": 0})
        result = scenario.execute()
        # Should not crash -- either ok or skipped-degenerate
        assert result.status in ("ok", "skipped-degenerate")


# ---------------------------------------------------------------------------
# Tier 4 sweep definition tests
# ---------------------------------------------------------------------------


class TestTier4SweepDefinitions:
    """Verify Tier 4 sweep configurations are well-formed."""

    def test_tier4_sweeps_defined(self):
        from tests.benchmarks.run_sweeps import TIER4_SWEEPS

        assert len(TIER4_SWEEPS) >= 5
        for name, values in TIER4_SWEEPS.items():
            assert len(values) >= 3, f"{name} has too few sweep values"

    def test_tier4_score_weights_defined(self):
        from tests.benchmarks.run_sweeps import TIER4_SCORE_WEIGHTS

        assert len(TIER4_SCORE_WEIGHTS) >= 4
        for config in TIER4_SCORE_WEIGHTS:
            assert isinstance(config, dict)
            assert len(config) >= 1

    def test_tier4_interaction_pairs_defined(self):
        from tests.benchmarks.run_sweeps import TIER4_INTERACTION_PAIRS

        assert len(TIER4_INTERACTION_PAIRS) >= 3
        for pair in TIER4_INTERACTION_PAIRS:
            assert len(pair) == 4
            const_a, vals_a, const_b, vals_b = pair
            assert isinstance(const_a, str)
            assert isinstance(const_b, str)
            assert len(vals_a) >= 2
            assert len(vals_b) >= 2

    def test_tier4_scenarios_importable(self):
        from tests.benchmarks.run_sweeps import TIER4_SCENARIOS

        assert len(TIER4_SCENARIOS) == 3
        for sc_class in TIER4_SCENARIOS:
            assert hasattr(sc_class, "name")
            assert hasattr(sc_class, "execute")

    def test_small_sweep_completes(self):
        """Run a minimal sweep (1 constant, 2 values, 1 scenario) to verify plumbing."""
        from tests.benchmarks.scenarios.support_agent import SupportAgentScenario
        from tests.benchmarks.sweep import SweepRunner

        runner = SweepRunner([SupportAgentScenario])
        results = runner.run_single_sweep("extraction_min_length", [10, 20])

        assert len(results) >= 2
        for point in results:
            assert point.status in ("ok", "skipped-degenerate", "error")
        ok_count = sum(1 for r in results if r.status == "ok")
        assert ok_count >= 1, "At least one sweep point should succeed"
