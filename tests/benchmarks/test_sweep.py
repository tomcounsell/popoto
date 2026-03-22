"""Tests for the parameter sweep engine.

Validates ParameterGrid, SweepRunner, and ResultsAggregator.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from tests.benchmarks.scenarios.factual_recall import FactualRecallScenario
from tests.benchmarks.scenarios.multi_step_reasoning import (
    MultiStepReasoningScenario,
)
from tests.benchmarks.scenarios.temporal_scheduling import (
    TemporalSchedulingScenario,
)
from tests.benchmarks.sweep import (
    ParameterGrid,
    PairwiseSweepPoint,
    ResultsAggregator,
    SweepPoint,
    SweepRunner,
)

# ---------------------------------------------------------------------------
# ParameterGrid tests
# ---------------------------------------------------------------------------


class TestParameterGrid:
    def test_single_sweep(self):
        grid = ParameterGrid.single_sweep("decay_rate", [0.1, 0.5, 0.9])
        assert len(grid) == 3
        assert grid[0] == {"decay_rate": 0.1}
        assert grid[2] == {"decay_rate": 0.9}

    def test_pairwise_sweep(self):
        grid = ParameterGrid.pairwise_sweep(
            "decay_rate", [0.1, 0.5], "initial_confidence", [0.3, 0.7]
        )
        assert len(grid) == 4
        assert {"decay_rate": 0.1, "initial_confidence": 0.3} in grid
        assert {"decay_rate": 0.5, "initial_confidence": 0.7} in grid

    def test_single_value(self):
        grid = ParameterGrid.single_sweep("TD_ALPHA", [0.1])
        assert len(grid) == 1

    def test_empty_values(self):
        grid = ParameterGrid.single_sweep("TD_ALPHA", [])
        assert len(grid) == 0


# ---------------------------------------------------------------------------
# SweepRunner tests (use single scenario for speed)
# ---------------------------------------------------------------------------


class TestSweepRunner:
    def test_single_sweep_runs(self):
        runner = SweepRunner([FactualRecallScenario])
        results = runner.run_single_sweep("decay_rate", [0.3, 0.5])
        assert len(results) == 2
        for r in results:
            assert r.status in ("ok", "error", "skipped-degenerate")
            assert r.constant_name == "decay_rate"

    def test_degenerate_value_skipped(self):
        runner = SweepRunner([FactualRecallScenario])
        results = runner.run_single_sweep("decay_rate", [0.0, 0.5])
        degenerate = [r for r in results if r.status == "skipped-degenerate"]
        assert len(degenerate) == 1
        assert degenerate[0].value == 0.0

    def test_multiple_scenarios(self):
        runner = SweepRunner([FactualRecallScenario, MultiStepReasoningScenario])
        results = runner.run_single_sweep("initial_confidence", [0.5])
        assert len(results) == 2
        scenarios = {r.scenario_name for r in results}
        assert "factual_recall" in scenarios
        assert "multi_step_reasoning" in scenarios

    def test_pairwise_sweep_runs(self):
        runner = SweepRunner([FactualRecallScenario])
        results = runner.run_pairwise_sweep(
            "decay_rate",
            [0.3, 0.5],
            "initial_confidence",
            [0.3, 0.7],
        )
        assert len(results) == 4
        for r in results:
            assert r.status in ("ok", "error", "skipped-degenerate")

    def test_sweep_metrics_in_range(self):
        runner = SweepRunner([FactualRecallScenario])
        results = runner.run_single_sweep("decay_rate", [0.5])
        ok_results = [r for r in results if r.status == "ok"]
        for r in ok_results:
            assert 0.0 <= r.precision_at_5 <= 1.0
            assert 0.0 <= r.ndcg_at_5 <= 1.0
            assert 0.0 <= r.calibration_error <= 1.0
            assert 0.0 <= r.mrr <= 1.0


# ---------------------------------------------------------------------------
# ResultsAggregator tests
# ---------------------------------------------------------------------------


class TestResultsAggregator:
    def _make_points(self) -> list:
        """Create sample sweep points for testing."""
        return [
            SweepPoint(
                constant_name="decay_rate",
                value=0.3,
                scenario_name="factual_recall",
                ndcg_at_5=0.6,
                precision_at_5=0.4,
                status="ok",
            ),
            SweepPoint(
                constant_name="decay_rate",
                value=0.5,
                scenario_name="factual_recall",
                ndcg_at_5=0.8,
                precision_at_5=0.6,
                status="ok",
            ),
            SweepPoint(
                constant_name="decay_rate",
                value=0.9,
                scenario_name="factual_recall",
                ndcg_at_5=0.3,
                precision_at_5=0.2,
                status="ok",
            ),
        ]

    def test_optimal_value(self):
        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", self._make_points())
        best_val, best_score = agg.get_optimal_value("decay_rate")
        assert best_val == 0.5
        assert best_score == 0.8

    def test_sensitivity_curve(self):
        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", self._make_points())
        curve = agg.get_sensitivity_curve("decay_rate")
        assert len(curve) == 3
        # Should be sorted by value
        values = [v for v, s in curve]
        assert values == sorted(values)

    def test_cliff_detection(self):
        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", self._make_points())
        # Between 0.5 (0.8) and 0.9 (0.3) there's a 0.5 drop
        cliffs = agg.detect_cliff_effects("decay_rate", threshold=0.3)
        assert len(cliffs) >= 1
        # The cliff should be between 0.5 and 0.9
        assert any(v1 == 0.5 and v2 == 0.9 for v1, v2, _ in cliffs)

    def test_summary_dict(self):
        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", self._make_points())
        summary = agg.to_summary_dict()
        assert "constants" in summary
        assert "decay_rate" in summary["constants"]
        assert summary["constants"]["decay_rate"]["best_value"] == 0.5

    def test_save_results(self, tmp_path, monkeypatch):
        import tests.benchmarks.sweep as sweep_mod

        monkeypatch.setattr(sweep_mod, "RESULTS_DIR", tmp_path)

        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", self._make_points())
        filepath = agg.save_results("test_summary.json")
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert "constants" in data

    def test_pairwise_results(self):
        agg = ResultsAggregator()
        points = [
            PairwiseSweepPoint(
                constant_a="decay_rate",
                value_a=0.3,
                constant_b="initial_confidence",
                value_b=0.5,
                scenario_name="factual_recall",
                ndcg_at_5=0.7,
                status="ok",
            ),
        ]
        agg.add_pairwise("decay_rate_x_initial_confidence", points)
        summary = agg.to_summary_dict()
        assert "pairwise" in summary
        assert "decay_rate_x_initial_confidence" in summary["pairwise"]

    def test_no_results(self):
        agg = ResultsAggregator()
        best_val, best_score = agg.get_optimal_value("nonexistent")
        assert best_val is None
        assert best_score == 0.0


# ---------------------------------------------------------------------------
# Integration test: full sweep pipeline
# ---------------------------------------------------------------------------


class TestSweepIntegration:
    def test_end_to_end_sweep(self, tmp_path, monkeypatch):
        """Run a small sweep end-to-end and verify results are saved."""
        import tests.benchmarks.sweep as sweep_mod

        monkeypatch.setattr(sweep_mod, "RESULTS_DIR", tmp_path)

        runner = SweepRunner([FactualRecallScenario])
        results = runner.run_single_sweep("decay_rate", [0.3, 0.5, 0.9])

        agg = ResultsAggregator()
        agg.add_sweep("decay_rate", results)

        filepath = agg.save_results()
        assert filepath.exists()

        data = json.loads(filepath.read_text())
        assert len(data["constants"]["decay_rate"]["sensitivity_curve"]) == 3
