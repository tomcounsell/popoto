"""Parameter sweep engine for systematic constant tuning.

Provides ParameterGrid for generating override combinations,
SweepRunner for executing scenarios with overrides, and
ResultsAggregator for collecting and summarizing results.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from .metrics.retrieval import (
    calibration_error,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
)
from .overrides import apply_overrides, is_degenerate
from .scenarios.base import Scenario, ScenarioResult

logger = logging.getLogger("POPOTO.Benchmark.Sweep")

RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class SweepPoint:
    """A single point in the parameter sweep.

    Attributes:
        constant_name: Name of the constant being swept.
        value: The override value for this point.
        scenario_name: Name of the scenario used.
        precision_at_5: Precision@5 metric.
        precision_at_10: Precision@10 metric.
        ndcg_at_5: nDCG@5 metric.
        ndcg_at_10: nDCG@10 metric.
        calibration_error: Expected calibration error.
        mrr: Mean reciprocal rank.
        duration_ms: Execution time in milliseconds.
        status: "ok", "skipped-degenerate", or "error".
        error_message: Description if status is not "ok".
    """

    constant_name: str = ""
    value: Any = None
    scenario_name: str = ""
    precision_at_5: float = 0.0
    precision_at_10: float = 0.0
    ndcg_at_5: float = 0.0
    ndcg_at_10: float = 0.0
    calibration_error: float = 0.0
    mrr: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"
    error_message: str = ""


@dataclass
class PairwiseSweepPoint:
    """A single point in a pairwise interaction sweep."""

    constant_a: str = ""
    value_a: Any = None
    constant_b: str = ""
    value_b: Any = None
    scenario_name: str = ""
    precision_at_5: float = 0.0
    ndcg_at_5: float = 0.0
    calibration_error: float = 0.0
    mrr: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"
    error_message: str = ""


class ParameterGrid:
    """Generate parameter override combinations for sweeps."""

    @staticmethod
    def single_sweep(constant_name: str, values: list) -> List[Dict[str, Any]]:
        """Generate override dicts for a single-constant sweep.

        Args:
            constant_name: Name of the constant to sweep.
            values: List of values to try.

        Returns:
            List of override dicts, one per value.
        """
        return [{constant_name: v} for v in values]

    @staticmethod
    def pairwise_sweep(
        constant_a: str,
        values_a: list,
        constant_b: str,
        values_b: list,
    ) -> List[Dict[str, Any]]:
        """Generate override dicts for a pairwise interaction sweep.

        Args:
            constant_a: First constant name.
            values_a: Values for first constant.
            constant_b: Second constant name.
            values_b: Values for second constant.

        Returns:
            List of override dicts with all combinations.
        """
        return [
            {constant_a: va, constant_b: vb} for va, vb in product(values_a, values_b)
        ]


class SweepRunner:
    """Execute scenarios with parameter overrides and collect results."""

    def __init__(self, scenario_classes: List[Type[Scenario]]):
        """Initialize with scenario classes to use for evaluation.

        Args:
            scenario_classes: List of Scenario subclasses to run per sweep point.
        """
        self.scenario_classes = scenario_classes

    def run_single_sweep(
        self,
        constant_name: str,
        values: list,
    ) -> List[SweepPoint]:
        """Run a single-constant sweep across all scenarios.

        Args:
            constant_name: Name of the constant to sweep.
            values: List of values to try.

        Returns:
            List of SweepPoint results.
        """
        results = []
        overrides_list = ParameterGrid.single_sweep(constant_name, values)

        for overrides in overrides_list:
            value = overrides[constant_name]

            # Check for degenerate values
            if is_degenerate(constant_name, value):
                for sc_class in self.scenario_classes:
                    results.append(
                        SweepPoint(
                            constant_name=constant_name,
                            value=value,
                            scenario_name=sc_class.name,
                            status="skipped-degenerate",
                            error_message=f"{constant_name}={value} is degenerate",
                        )
                    )
                logger.warning(f"Skipping degenerate value: {constant_name}={value}")
                continue

            for sc_class in self.scenario_classes:
                scenario = sc_class(overrides=overrides)
                result = scenario.execute()

                point = SweepPoint(
                    constant_name=constant_name,
                    value=value,
                    scenario_name=result.scenario_name,
                    duration_ms=result.duration_ms,
                    status=result.status,
                    error_message=result.error_message,
                )

                if result.status == "ok":
                    point.precision_at_5 = precision_at_k(
                        result.retrieved_ids, result.relevant_ids, 5
                    )
                    point.precision_at_10 = precision_at_k(
                        result.retrieved_ids, result.relevant_ids, 10
                    )
                    point.ndcg_at_5 = ndcg_at_k(
                        result.retrieved_ids, result.relevance_scores, 5
                    )
                    point.ndcg_at_10 = ndcg_at_k(
                        result.retrieved_ids, result.relevance_scores, 10
                    )
                    point.mrr = mean_reciprocal_rank(
                        result.retrieved_ids, result.relevant_ids
                    )
                    if result.predictions and result.outcomes:
                        point.calibration_error = calibration_error(
                            result.predictions, result.outcomes
                        )

                results.append(point)

        return results

    def run_pairwise_sweep(
        self,
        constant_a: str,
        values_a: list,
        constant_b: str,
        values_b: list,
    ) -> List[PairwiseSweepPoint]:
        """Run a pairwise interaction sweep across all scenarios.

        Args:
            constant_a: First constant name.
            values_a: Values for first constant.
            constant_b: Second constant name.
            values_b: Values for second constant.

        Returns:
            List of PairwiseSweepPoint results.
        """
        results = []
        overrides_list = ParameterGrid.pairwise_sweep(
            constant_a, values_a, constant_b, values_b
        )

        for overrides in overrides_list:
            va = overrides[constant_a]
            vb = overrides[constant_b]

            # Check for degenerate values
            if is_degenerate(constant_a, va) or is_degenerate(constant_b, vb):
                for sc_class in self.scenario_classes:
                    results.append(
                        PairwiseSweepPoint(
                            constant_a=constant_a,
                            value_a=va,
                            constant_b=constant_b,
                            value_b=vb,
                            scenario_name=sc_class.name,
                            status="skipped-degenerate",
                        )
                    )
                continue

            for sc_class in self.scenario_classes:
                scenario = sc_class(overrides=overrides)
                result = scenario.execute()

                point = PairwiseSweepPoint(
                    constant_a=constant_a,
                    value_a=va,
                    constant_b=constant_b,
                    value_b=vb,
                    scenario_name=result.scenario_name,
                    duration_ms=result.duration_ms,
                    status=result.status,
                    error_message=result.error_message,
                )

                if result.status == "ok":
                    point.precision_at_5 = precision_at_k(
                        result.retrieved_ids, result.relevant_ids, 5
                    )
                    point.ndcg_at_5 = ndcg_at_k(
                        result.retrieved_ids, result.relevance_scores, 5
                    )
                    point.mrr = mean_reciprocal_rank(
                        result.retrieved_ids, result.relevant_ids
                    )
                    if result.predictions and result.outcomes:
                        point.calibration_error = calibration_error(
                            result.predictions, result.outcomes
                        )

                results.append(point)

        return results


class ResultsAggregator:
    """Collect sweep results and produce summary statistics."""

    def __init__(self):
        self.sweep_results: Dict[str, List[SweepPoint]] = {}
        self.pairwise_results: Dict[str, List[PairwiseSweepPoint]] = {}

    def add_sweep(self, constant_name: str, points: List[SweepPoint]):
        """Add results from a single-constant sweep."""
        self.sweep_results[constant_name] = points

    def add_pairwise(self, pair_name: str, points: List[PairwiseSweepPoint]):
        """Add results from a pairwise interaction sweep."""
        self.pairwise_results[pair_name] = points

    def get_optimal_value(
        self,
        constant_name: str,
        metric: str = "ndcg_at_5",
    ) -> Tuple[Any, float]:
        """Find the value that maximizes a metric (or minimizes calibration_error).

        Args:
            constant_name: Name of the constant.
            metric: Metric to optimize. Default "ndcg_at_5".

        Returns:
            Tuple of (best_value, best_metric_score).
        """
        points = self.sweep_results.get(constant_name, [])
        ok_points = [p for p in points if p.status == "ok"]
        if not ok_points:
            return None, 0.0

        # Group by value, average across scenarios
        value_scores: Dict[Any, List[float]] = {}
        for p in ok_points:
            score = getattr(p, metric, 0.0)
            value_scores.setdefault(p.value, []).append(score)

        avg_scores = {v: sum(s) / len(s) for v, s in value_scores.items()}

        # For calibration_error, lower is better
        if metric == "calibration_error":
            best_value = min(avg_scores, key=avg_scores.get)
        else:
            best_value = max(avg_scores, key=avg_scores.get)

        return best_value, avg_scores[best_value]

    def get_sensitivity_curve(
        self,
        constant_name: str,
        metric: str = "ndcg_at_5",
        scenario_name: Optional[str] = None,
    ) -> List[Tuple[Any, float]]:
        """Get (value, metric) pairs for plotting sensitivity curves.

        Args:
            constant_name: Name of the constant.
            metric: Metric to track.
            scenario_name: If set, filter to a specific scenario.

        Returns:
            Sorted list of (value, metric_score) tuples.
        """
        points = self.sweep_results.get(constant_name, [])
        ok_points = [p for p in points if p.status == "ok"]
        if scenario_name:
            ok_points = [p for p in ok_points if p.scenario_name == scenario_name]

        value_scores: Dict[Any, List[float]] = {}
        for p in ok_points:
            score = getattr(p, metric, 0.0)
            value_scores.setdefault(p.value, []).append(score)

        curve = [(v, sum(s) / len(s)) for v, s in value_scores.items()]
        curve.sort(key=lambda x: x[0])
        return curve

    def detect_cliff_effects(
        self,
        constant_name: str,
        metric: str = "ndcg_at_5",
        threshold: float = 0.2,
    ) -> List[Tuple[Any, Any, float]]:
        """Detect sharp metric drops between adjacent sweep points.

        Args:
            constant_name: Name of the constant.
            metric: Metric to analyze.
            threshold: Minimum drop magnitude to flag as a cliff.

        Returns:
            List of (value_before, value_after, drop_magnitude) tuples.
        """
        curve = self.get_sensitivity_curve(constant_name, metric)
        cliffs = []
        for i in range(len(curve) - 1):
            v1, s1 = curve[i]
            v2, s2 = curve[i + 1]
            drop = abs(s1 - s2)
            if drop >= threshold:
                cliffs.append((v1, v2, drop))
        return cliffs

    def to_summary_dict(self) -> dict:
        """Export results as a summary dictionary for JSON serialization."""
        constants = {}
        for name, points in self.sweep_results.items():
            ok_points = [p for p in points if p.status == "ok"]
            best_val, best_score = self.get_optimal_value(name)
            curve = self.get_sensitivity_curve(name)
            cliffs = self.detect_cliff_effects(name)

            constants[name] = {
                "best_value": best_val,
                "best_ndcg_at_5": best_score,
                "sensitivity_curve": curve,
                "cliff_effects": cliffs,
                "n_points_ok": len(ok_points),
                "n_points_total": len(points),
                "scenarios": list(set(p.scenario_name for p in ok_points)),
            }

        pairwise = {}
        for pair_name, points in self.pairwise_results.items():
            ok_points = [p for p in points if p.status == "ok"]
            pairwise[pair_name] = {
                "n_points_ok": len(ok_points),
                "n_points_total": len(points),
                "points": [
                    {
                        "value_a": p.value_a,
                        "value_b": p.value_b,
                        "ndcg_at_5": p.ndcg_at_5,
                        "precision_at_5": p.precision_at_5,
                    }
                    for p in ok_points
                ],
            }

        return {
            "constants": constants,
            "pairwise": pairwise,
            "timestamp": time.time(),
        }

    def save_results(self, filename: str = "summary.json"):
        """Save summary results to JSON file in results directory."""
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = RESULTS_DIR / filename
        summary = self.to_summary_dict()
        with open(filepath, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        return filepath
