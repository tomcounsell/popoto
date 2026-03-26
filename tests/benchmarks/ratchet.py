"""Ratchet loop for automated keep/discard decisions on constant tuning.

The RatchetLoop iterates over Tiers 1-3 constants, sweeps each on the train
set, validates on the held-out set, and produces accept/reject recommendations
with cliff safety margins. It NEVER writes to constants.py -- it outputs a
human-readable diff proposal.

Usage:
    from tests.benchmarks.ratchet import RatchetLoop
    loop = RatchetLoop()
    summary = loop.run()
    print(summary)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

from .scenarios.base import Scenario
from .scenarios.factory import ScenarioFactory, ScenarioSeed
from .split import SplitRunner, ValidationResult, make_split
from .sweep import ResultsAggregator, SweepRunner

logger = logging.getLogger("POPOTO.Benchmark.Ratchet")


# Import tier definitions from run_sweeps
from .run_sweeps import TIER1_SWEEPS, TIER2_SWEEPS, TIER3_SWEEPS


@dataclass
class RatchetDecision:
    """A single ratchet decision for one constant.

    Attributes:
        constant_name: The constant evaluated.
        current_value: Current default value.
        proposed_value: Best value found on train set.
        train_delta: Improvement on train set vs default.
        validation_delta: Improvement on validation set vs default.
        action: "accept", "reject", or "no_sensitivity".
        reason: Human-readable explanation.
        cliff_warning: True if proposed value is near a cliff boundary.
        cliff_margin: Distance from nearest cliff, if applicable.
    """

    constant_name: str
    current_value: Any
    proposed_value: Any
    train_delta: float
    validation_delta: float
    action: str  # "accept", "reject", "no_sensitivity"
    reason: str
    cliff_warning: bool = False
    cliff_margin: float = 0.0


@dataclass
class RatchetSummary:
    """Complete ratchet loop results.

    Attributes:
        decisions: List of RatchetDecision for each constant.
        accepted: Constants whose changes were accepted.
        rejected: Constants whose changes were rejected.
        no_sensitivity: Constants that showed no measurable sensitivity.
        duration_seconds: Total wall-clock time for the ratchet loop.
    """

    decisions: List[RatchetDecision] = field(default_factory=list)
    accepted: List[RatchetDecision] = field(default_factory=list)
    rejected: List[RatchetDecision] = field(default_factory=list)
    no_sensitivity: List[RatchetDecision] = field(default_factory=list)
    duration_seconds: float = 0.0

    def format_report(self) -> str:
        """Produce a human-readable summary report."""
        lines = []
        lines.append("RATCHET SUMMARY")
        lines.append("=" * 70)
        lines.append(f"Total constants evaluated: {len(self.decisions)}")
        lines.append(f"Accepted: {len(self.accepted)}")
        lines.append(f"Rejected: {len(self.rejected)}")
        lines.append(f"No sensitivity: {len(self.no_sensitivity)}")
        lines.append(f"Duration: {self.duration_seconds:.1f}s")
        lines.append("")

        if self.accepted:
            lines.append("Proposed changes (validated on held-out scenarios):")
            for d in self.accepted:
                cliff_note = ""
                if d.cliff_warning:
                    cliff_note = f", cliff margin: {d.cliff_margin:.3f}"
                lines.append(
                    f"  {d.constant_name:45s} "
                    f"{d.current_value!s:>8s} -> {d.proposed_value!s:<8s} "
                    f"(train +{d.train_delta:.3f}, "
                    f"validation +{d.validation_delta:.3f}"
                    f"{cliff_note}) ACCEPT"
                )
            lines.append("")

        if self.rejected:
            lines.append("Rejected (insufficient validation improvement):")
            for d in self.rejected:
                lines.append(
                    f"  {d.constant_name:45s} "
                    f"{d.current_value!s:>8s} -> {d.proposed_value!s:<8s} "
                    f"(train +{d.train_delta:.3f}, "
                    f"validation {d.validation_delta:+.3f}) "
                    f"REJECT: {d.reason}"
                )
            lines.append("")

        if self.no_sensitivity:
            lines.append("No sensitivity detected:")
            names = [d.constant_name for d in self.no_sensitivity]
            lines.append(f"  {', '.join(names)}")
            lines.append("")

        return "\n".join(lines)


class RatchetLoop:
    """Automate keep/discard decisions for constant tuning.

    For each constant in Tiers 1-3:
    1. Sweep on train scenarios, find optimal value.
    2. Validate on held-out scenarios.
    3. Check cliff safety margins.
    4. Accept or reject with explanation.
    """

    def __init__(
        self,
        n_seeds: int = 50,
        improvement_threshold: float = 0.01,
        cliff_safety_margin: float = 0.10,
        sensitivity_threshold: float = 0.005,
        seeds: Optional[List[ScenarioSeed]] = None,
    ):
        """Initialize the ratchet loop.

        Args:
            n_seeds: Number of parametric seeds to generate.
            improvement_threshold: Minimum validation delta to accept.
            cliff_safety_margin: Reject if proposed value is within this
                fraction of a detected cliff boundary.
            sensitivity_threshold: Minimum nDCG variance to consider a
                constant as having sensitivity.
            seeds: Explicit seed list. If None, uses default_seeds(n_seeds).
        """
        self.n_seeds = n_seeds
        self.improvement_threshold = improvement_threshold
        self.cliff_safety_margin = cliff_safety_margin
        self.sensitivity_threshold = sensitivity_threshold

        # Generate seeds and split
        if seeds is None:
            seeds = ScenarioFactory.default_seeds(n_seeds)
        self.seeds = seeds
        train_seeds, val_seeds = make_split(seeds)

        # Create scenario classes
        self.train_scenarios = ScenarioFactory.create_all(train_seeds)
        self.val_scenarios = ScenarioFactory.create_all(val_seeds)

        # Build runners
        self.split_runner = SplitRunner(self.train_scenarios, self.val_scenarios)

        # For cliff detection, we need a separate aggregator with train data
        self._train_runner = SweepRunner(self.train_scenarios)

    def _get_current_default(self, constant_name: str) -> Any:
        """Look up the current default value for a constant.

        Uses the Defaults class from constants.py.
        """
        from src.popoto.fields.constants import Defaults

        # Check direct attribute
        if hasattr(Defaults, constant_name):
            return getattr(Defaults, constant_name)

        # Check uppercase variant
        upper = constant_name.upper()
        if hasattr(Defaults, upper):
            return getattr(Defaults, upper)

        return None

    def _check_cliff_proximity(
        self,
        constant_name: str,
        proposed_value: Any,
        values: list,
    ) -> Tuple[bool, float]:
        """Check if proposed value is near a cliff boundary.

        Args:
            constant_name: The constant name.
            proposed_value: The proposed optimal value.
            values: The sweep values used.

        Returns:
            (is_near_cliff, margin) tuple.
        """
        # Run a quick sweep on train to get cliff data
        train_results = self._train_runner.run_single_sweep(constant_name, values)
        agg = ResultsAggregator()
        agg.add_sweep(constant_name, train_results)
        cliffs = agg.detect_cliff_effects(constant_name, threshold=0.15)

        if not cliffs or not isinstance(proposed_value, (int, float)):
            return False, 1.0

        for v_before, v_after, drop in cliffs:
            if not isinstance(v_before, (int, float)) or not isinstance(
                v_after, (int, float)
            ):
                continue
            cliff_center = (v_before + v_after) / 2
            cliff_range = abs(v_after - v_before)
            if cliff_range == 0:
                continue
            distance = abs(proposed_value - cliff_center)
            margin = distance / cliff_range
            if margin < self.cliff_safety_margin / 0.10:  # Normalize
                return True, margin

        return False, 1.0

    def run(
        self,
        tiers: Optional[Dict[str, dict]] = None,
    ) -> RatchetSummary:
        """Execute the full ratchet loop across all tiers.

        Args:
            tiers: Optional dict of {constant_name: values_list}.
                If None, uses TIER1-3 from run_sweeps.

        Returns:
            RatchetSummary with all decisions.
        """
        start = time.monotonic()
        summary = RatchetSummary()

        if tiers is None:
            tiers = {}
            tiers.update(TIER1_SWEEPS)
            tiers.update(TIER2_SWEEPS)
            tiers.update(TIER3_SWEEPS)

        for constant_name, values in tiers.items():
            logger.info("Ratchet: evaluating %s", constant_name)

            current_default = self._get_current_default(constant_name)

            # Run sweep and validate
            result = self.split_runner.sweep_and_validate(
                constant_name,
                values,
                current_default=current_default,
                improvement_threshold=self.improvement_threshold,
            )

            train_delta = result.train_score - result.train_default_score
            val_delta = result.delta

            # Check sensitivity: if train variance is too low, mark as no_sensitivity
            if abs(train_delta) < self.sensitivity_threshold and abs(val_delta) < self.sensitivity_threshold:
                decision = RatchetDecision(
                    constant_name=constant_name,
                    current_value=current_default,
                    proposed_value=result.best_value,
                    train_delta=train_delta,
                    validation_delta=val_delta,
                    action="no_sensitivity",
                    reason="No measurable sensitivity across generated scenarios",
                )
                summary.decisions.append(decision)
                summary.no_sensitivity.append(decision)
                continue

            # Check cliff proximity (only if we have a numeric proposed value)
            cliff_warning = False
            cliff_margin = 1.0
            if isinstance(result.best_value, (int, float)):
                cliff_warning, cliff_margin = self._check_cliff_proximity(
                    constant_name, result.best_value, values
                )

            if cliff_warning and result.recommendation == "accept":
                # Reject if too close to a cliff
                decision = RatchetDecision(
                    constant_name=constant_name,
                    current_value=current_default,
                    proposed_value=result.best_value,
                    train_delta=train_delta,
                    validation_delta=val_delta,
                    action="reject",
                    reason=f"Too close to cliff (margin={cliff_margin:.3f})",
                    cliff_warning=True,
                    cliff_margin=cliff_margin,
                )
                summary.decisions.append(decision)
                summary.rejected.append(decision)
            elif result.recommendation == "accept":
                decision = RatchetDecision(
                    constant_name=constant_name,
                    current_value=current_default,
                    proposed_value=result.best_value,
                    train_delta=train_delta,
                    validation_delta=val_delta,
                    action="accept",
                    reason="Validated improvement on held-out scenarios",
                    cliff_warning=cliff_warning,
                    cliff_margin=cliff_margin,
                )
                summary.decisions.append(decision)
                summary.accepted.append(decision)
            else:
                decision = RatchetDecision(
                    constant_name=constant_name,
                    current_value=current_default,
                    proposed_value=result.best_value,
                    train_delta=train_delta,
                    validation_delta=val_delta,
                    action="reject",
                    reason=result.reject_reason,
                )
                summary.decisions.append(decision)
                summary.rejected.append(decision)

        summary.duration_seconds = time.monotonic() - start
        return summary
