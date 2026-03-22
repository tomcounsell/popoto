"""Run all parameter sweeps and save results.

Usage:
    python -m tests.benchmarks.run_sweeps [--tier 1|2|3|all] [--interactions]

Results are saved to tests/benchmarks/results/summary.json
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.benchmarks.scenarios.factual_recall import FactualRecallScenario
from tests.benchmarks.scenarios.multi_step_reasoning import (
    MultiStepReasoningScenario,
)
from tests.benchmarks.scenarios.temporal_scheduling import (
    TemporalSchedulingScenario,
)
from tests.benchmarks.sweep import ResultsAggregator, SweepRunner

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("Sweep")

ALL_SCENARIOS = [
    FactualRecallScenario,
    MultiStepReasoningScenario,
    TemporalSchedulingScenario,
]

# ---------------------------------------------------------------------------
# Tier 1: High sensitivity constants
# ---------------------------------------------------------------------------

TIER1_SWEEPS = {
    "decay_rate": [0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0],
    "initial_confidence": [0.1, 0.3, 0.5, 0.7, 0.9],
    "_wf_min_threshold": [0.05, 0.1, 0.2, 0.3, 0.5],
    "ACTED_CONFIDENCE_SIGNAL": [0.1, 0.3, 0.5, 0.7, 0.9],
    "CONTRADICTED_CONFIDENCE_SIGNAL": [0.1, 0.3, 0.5, 0.7, 0.9],
}

# ---------------------------------------------------------------------------
# Tier 2: Medium sensitivity constants
# ---------------------------------------------------------------------------

TIER2_SWEEPS = {
    "ACTED_CYCLE_STRENGTHEN_FACTOR": [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0],
    "DISMISSED_CYCLE_WEAKEN_FACTOR": [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0],
    "CONTRADICTED_CYCLE_WEAKEN_FACTOR": [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0],
    "decay_factor": [0.5, 0.7, 0.85, 0.9, 0.95, 0.99],
    "initial_weight": [0.01, 0.05, 0.1, 0.2, 0.5],
    "delta": [0.01, 0.02, 0.05, 0.1, 0.2],
    "decay_per_hop": [0.1, 0.3, 0.5, 0.7, 0.9],
    "COMPETITIVE_SUPPRESSION_SIGNAL": [0.1, 0.2, 0.3, 0.5, 0.7],
    "DEFAULT_SURFACING_THRESHOLD": [0.1, 0.3, 0.5, 0.7, 0.9],
}

# ---------------------------------------------------------------------------
# Tier 3: PolicyCache constants
# ---------------------------------------------------------------------------

TIER3_SWEEPS = {
    "MIN_EVENTS_FOR_CRYSTALLIZATION": [1, 2, 3, 5, 10],
    "TD_ALPHA": [0.01, 0.05, 0.1, 0.2, 0.5],
    "TD_GAMMA": [0.8, 0.9, 0.95, 0.99],
    "WILSON_CI_THRESHOLD": [0.3, 0.5, 0.6, 0.7, 0.8],
    "AUTO_DISCHARGE_CONFIDENCE_THRESHOLD": [0.05, 0.1, 0.2, 0.3],
}

# ---------------------------------------------------------------------------
# Interaction effect pairs
# ---------------------------------------------------------------------------

INTERACTION_PAIRS = [
    ("decay_rate", [0.3, 0.5, 0.7], "initial_confidence", [0.3, 0.5, 0.7]),
    ("_wf_min_threshold", [0.1, 0.2, 0.3], "initial_weight", [0.05, 0.1, 0.2]),
    (
        "ACTED_CONFIDENCE_SIGNAL",
        [0.5, 0.7, 0.9],
        "ACTED_CYCLE_STRENGTHEN_FACTOR",
        [0.8, 1.2, 1.5],
    ),
    ("TD_ALPHA", [0.05, 0.1, 0.2], "TD_GAMMA", [0.9, 0.95, 0.99]),
    (
        "_wf_min_threshold",
        [0.1, 0.2, 0.3],
        "_wf_priority_threshold",
        [0.5, 0.7, 0.9],
    ),
]


def run_tier(tier_sweeps, tier_name, runner, aggregator):
    """Run all sweeps for a tier."""
    logger.info("Starting %s sweeps (%d constants)...", tier_name, len(tier_sweeps))
    tier_start = time.monotonic()

    for constant_name, values in tier_sweeps.items():
        logger.info("  Sweeping %s over %s", constant_name, values)
        results = runner.run_single_sweep(constant_name, values)
        aggregator.add_sweep(constant_name, results)

        ok_count = sum(1 for r in results if r.status == "ok")
        logger.info("    %d/%d points OK", ok_count, len(results))

    elapsed = time.monotonic() - tier_start
    logger.info("%s complete in %.1fs", tier_name, elapsed)


def run_interactions(runner, aggregator):
    """Run pairwise interaction sweeps."""
    logger.info("Starting interaction sweeps (%d pairs)...", len(INTERACTION_PAIRS))

    for const_a, vals_a, const_b, vals_b in INTERACTION_PAIRS:
        pair_name = f"{const_a}_x_{const_b}"
        logger.info("  Pairwise: %s x %s", const_a, const_b)
        results = runner.run_pairwise_sweep(const_a, vals_a, const_b, vals_b)
        aggregator.add_pairwise(pair_name, results)

        ok_count = sum(1 for r in results if r.status == "ok")
        logger.info("    %d/%d points OK", ok_count, len(results))


def main():
    parser = argparse.ArgumentParser(description="Run parameter sweeps")
    parser.add_argument(
        "--tier",
        choices=["1", "2", "3", "all"],
        default="all",
        help="Which tier to run",
    )
    parser.add_argument(
        "--interactions",
        action="store_true",
        help="Run interaction effect sweeps",
    )
    parser.add_argument(
        "--output",
        default="summary.json",
        help="Output filename (default: summary.json)",
    )
    args = parser.parse_args()

    runner = SweepRunner(ALL_SCENARIOS)
    aggregator = ResultsAggregator()

    total_start = time.monotonic()

    if args.tier in ("1", "all"):
        run_tier(TIER1_SWEEPS, "Tier 1", runner, aggregator)

    if args.tier in ("2", "all"):
        run_tier(TIER2_SWEEPS, "Tier 2", runner, aggregator)

    if args.tier in ("3", "all"):
        run_tier(TIER3_SWEEPS, "Tier 3", runner, aggregator)

    if args.interactions:
        run_interactions(runner, aggregator)

    filepath = aggregator.save_results(args.output)
    total_elapsed = time.monotonic() - total_start

    # Print summary
    summary = aggregator.to_summary_dict()
    n_constants = len(summary["constants"])
    logger.info("\nSweep complete: %d constants in %.1fs", n_constants, total_elapsed)
    logger.info("Results saved to: %s", filepath)

    # Print optimal values
    print("\n" + "=" * 70)
    print("OPTIMAL VALUES (by nDCG@5 averaged across scenarios)")
    print("=" * 70)
    for name, info in sorted(summary["constants"].items()):
        best = info["best_value"]
        score = info["best_ndcg_at_5"]
        cliffs = info["cliff_effects"]
        cliff_note = f" [CLIFF at {cliffs[0]}]" if cliffs else ""
        print(f"  {name:45s} = {best!s:8s} (nDCG@5={score:.3f}){cliff_note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
