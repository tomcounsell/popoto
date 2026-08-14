"""Train/Validation split for parametric scenario sweeps.

Partitions generated scenarios into disjoint train (70%) and validation (30%)
sets. The SplitRunner sweeps on train scenarios and validates proposed optimal
values on held-out scenarios to guard against overfitting.

Usage:
    from tests.benchmarks.scenarios.factory import ScenarioFactory
    from tests.benchmarks.split import SplitRunner

    seeds = ScenarioFactory.default_seeds(50)
    train_seeds = [s for s in seeds if s.seed_id < 35]
    val_seeds = [s for s in seeds if s.seed_id >= 35]

    train_scenarios = [ScenarioFactory.create(s) for s in train_seeds]
    val_scenarios = [ScenarioFactory.create(s) for s in val_seeds]

    split_runner = SplitRunner(train_scenarios, val_scenarios)
    result = split_runner.sweep_and_validate("decay_rate", [0.1, 0.3, 0.5, 0.7])
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Type

from .scenarios.base import Scenario
from .sweep import ResultsAggregator, SweepRunner

# Default split boundary: seeds 0-34 train, 35-49 validation
TRAIN_SPLIT_BOUNDARY = 35


@dataclass
class ValidationResult:
    """Result of a sweep-and-validate cycle for one constant.

    Attributes:
        constant_name: The constant that was swept.
        best_value: The value that performed best on the train set.
        current_default: The current default value (baseline).
        train_score: Average nDCG@5 of best_value on train scenarios.
        train_default_score: Average nDCG@5 of current default on train scenarios.
        validation_score: Average nDCG@5 of best_value on validation scenarios.
        validation_default_score: Average nDCG@5 of default on validation scenarios.
        delta: validation_score - validation_default_score.
        recommendation: "accept" or "reject".
        reject_reason: Why the change was rejected, if applicable.
    """

    constant_name: str
    best_value: Any
    current_default: Any
    train_score: float
    train_default_score: float
    validation_score: float
    validation_default_score: float
    delta: float
    recommendation: str  # "accept" or "reject"
    reject_reason: str = ""


class SplitRunner:
    """Run sweeps on train scenarios, validate on held-out scenarios.

    The split is deterministic: seed IDs below TRAIN_SPLIT_BOUNDARY go to
    train, the rest to validation.
    """

    def __init__(
        self,
        train_scenarios: List[Type[Scenario]],
        validation_scenarios: List[Type[Scenario]],
    ):
        """Initialize with pre-split scenario class lists.

        Args:
            train_scenarios: Scenario classes for the training set (70%).
            validation_scenarios: Scenario classes for the validation set (30%).
        """
        self.train_runner = SweepRunner(train_scenarios)
        self.validation_runner = SweepRunner(validation_scenarios)

    def sweep_and_validate(
        self,
        constant_name: str,
        values: list,
        current_default: Any = None,
        improvement_threshold: float = 0.01,
    ) -> ValidationResult:
        """Sweep on train, validate best value on held-out set.

        1. Run full sweep on train scenarios to find the optimal value.
        2. Evaluate that optimal value AND the current default on validation.
        3. Compare and produce an accept/reject recommendation.

        Args:
            constant_name: Name of the constant to sweep.
            values: List of values to try.
            current_default: The current default value. If None, uses the
                first value in the list as baseline.
            improvement_threshold: Minimum validation delta to accept (default 0.01).

        Returns:
            ValidationResult with train/validation scores and recommendation.
        """
        if current_default is None:
            # Use the middle value as a proxy for "current default"
            current_default = values[len(values) // 2]

        # Step 1: Sweep on train
        train_results = self.train_runner.run_single_sweep(constant_name, values)
        train_agg = ResultsAggregator()
        train_agg.add_sweep(constant_name, train_results)
        best_value, train_score = train_agg.get_optimal_value(constant_name)

        if best_value is None:
            return ValidationResult(
                constant_name=constant_name,
                best_value=current_default,
                current_default=current_default,
                train_score=0.0,
                train_default_score=0.0,
                validation_score=0.0,
                validation_default_score=0.0,
                delta=0.0,
                recommendation="reject",
                reject_reason="No valid train results",
            )

        # Get train score for the default value
        train_default_score = 0.0
        default_points = [
            p for p in train_results if p.value == current_default and p.status == "ok"
        ]
        if default_points:
            train_default_score = sum(p.ndcg_at_5 for p in default_points) / len(
                default_points
            )

        # Step 2: Validate on held-out set
        # Only sweep the best value and the default
        val_values = list({best_value, current_default})
        val_results = self.validation_runner.run_single_sweep(constant_name, val_values)
        val_agg = ResultsAggregator()
        val_agg.add_sweep(constant_name, val_results)

        # Get validation scores
        validation_score = 0.0
        validation_default_score = 0.0

        best_val_points = [
            p for p in val_results if p.value == best_value and p.status == "ok"
        ]
        if best_val_points:
            validation_score = sum(p.ndcg_at_5 for p in best_val_points) / len(
                best_val_points
            )

        default_val_points = [
            p for p in val_results if p.value == current_default and p.status == "ok"
        ]
        if default_val_points:
            validation_default_score = sum(
                p.ndcg_at_5 for p in default_val_points
            ) / len(default_val_points)

        delta = validation_score - validation_default_score

        # Step 3: Accept/reject decision
        if delta > improvement_threshold:
            recommendation = "accept"
            reject_reason = ""
        elif best_value == current_default:
            recommendation = "reject"
            reject_reason = "Best value is already the default"
        else:
            recommendation = "reject"
            reject_reason = (
                f"No significant validation improvement "
                f"(delta={delta:.4f}, threshold={improvement_threshold})"
            )

        return ValidationResult(
            constant_name=constant_name,
            best_value=best_value,
            current_default=current_default,
            train_score=train_score,
            train_default_score=train_default_score,
            validation_score=validation_score,
            validation_default_score=validation_default_score,
            delta=delta,
            recommendation=recommendation,
            reject_reason=reject_reason,
        )


def make_split(
    seeds: list,
    boundary: int = TRAIN_SPLIT_BOUNDARY,
) -> Tuple[list, list]:
    """Split seeds into train and validation sets by seed_id.

    Args:
        seeds: List of ScenarioSeed objects.
        boundary: Seed IDs below this go to train.

    Returns:
        (train_seeds, validation_seeds) tuple.
    """
    train = [s for s in seeds if s.seed_id < boundary]
    val = [s for s in seeds if s.seed_id >= boundary]
    return train, val
