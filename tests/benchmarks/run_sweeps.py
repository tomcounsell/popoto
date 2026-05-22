"""Run all parameter sweeps and save results.

Usage:
    python -m tests.benchmarks.run_sweeps [--tier 1|2|3|4|all] [--interactions]
    python -m tests.benchmarks.run_sweeps --parametric [--tier 1|2|3]
    python -m tests.benchmarks.run_sweeps --ratchet

Results are saved to tests/benchmarks/results/sweep_YYYYMMDD_HHMMSS.json
with a latest.json symlink pointing to the most recent run.
Previous results are never overwritten.
"""

import argparse
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
from tests.benchmarks.scenarios.support_agent import SupportAgentScenario
from tests.benchmarks.scenarios.coding_assistant import CodingAssistantScenario
from tests.benchmarks.scenarios.research_agent import ResearchAgentScenario
from tests.benchmarks.sweep import ResultsAggregator, SweepRunner

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("Sweep")

ALL_SCENARIOS = [
    FactualRecallScenario,
    MultiStepReasoningScenario,
    TemporalSchedulingScenario,
]

TIER4_SCENARIOS = [
    SupportAgentScenario,
    CodingAssistantScenario,
    ResearchAgentScenario,
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
    "ACTED_CYCLE_STRENGTHEN_FACTOR": [
        0.3,
        0.5,
        0.8,
        0.9,
        0.95,
        1.0,
        1.02,
        1.05,
        1.08,
        1.1,
        1.12,
        1.15,
        1.2,
        1.5,
        2.0,
    ],
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
    "CHI_SQUARED_P_THRESHOLD": [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2],
    "INITIAL_CYCLE_AMPLITUDE": [0.1, 0.25, 0.5, 0.75, 1.0, 2.0],
    # PredictionLedger constants
    "PL_CONFIDENCE_ERROR_THRESHOLD": [0.3, 0.5, 0.7, 0.85, 0.95],
    "PL_CONFIDENCE_LOW_SIGNAL": [0.05, 0.1, 0.2, 0.3, 0.5],
    "PL_AUTO_RESOLVE_ACTED": [0.05, 0.1, 0.2, 0.3, 0.5],
    "PL_AUTO_RESOLVE_DISMISSED": [0.2, 0.3, 0.5, 0.7, 0.9],
    "PL_AUTO_RESOLVE_CONTRADICTED": [0.5, 0.7, 0.8, 0.9, 0.95],
}

# ---------------------------------------------------------------------------
# Interaction effect pairs
# ---------------------------------------------------------------------------

INTERACTION_PAIRS = [
    # Expanded existing pairs to 5×5 grids (25 combinations each)
    (
        "decay_rate",
        [0.1, 0.3, 0.5, 0.7, 0.9],
        "initial_confidence",
        [0.1, 0.3, 0.5, 0.7, 0.9],
    ),
    (
        "_wf_min_threshold",
        [0.05, 0.1, 0.2, 0.3, 0.5],
        "initial_weight",
        [0.01, 0.05, 0.1, 0.2, 0.5],
    ),
    (
        "ACTED_CONFIDENCE_SIGNAL",
        [0.3, 0.5, 0.7, 0.8, 0.9],
        "ACTED_CYCLE_STRENGTHEN_FACTOR",
        [0.5, 0.8, 1.0, 1.2, 1.5],
    ),
    ("TD_ALPHA", [0.01, 0.05, 0.1, 0.2, 0.5], "TD_GAMMA", [0.8, 0.9, 0.95, 0.97, 0.99]),
    (
        "_wf_min_threshold",
        [0.05, 0.1, 0.2, 0.3, 0.5],
        "_wf_priority_threshold",
        [0.3, 0.5, 0.7, 0.8, 0.9],
    ),
    # New interaction pairs from issue #279
    (
        "ACTED_CYCLE_STRENGTHEN_FACTOR",
        [0.5, 0.8, 1.0, 1.2, 1.5],
        "DISMISSED_CYCLE_WEAKEN_FACTOR",
        [0.3, 0.5, 0.8, 1.0, 1.2],
    ),
    (
        "DEFAULT_SURFACING_THRESHOLD",
        [0.1, 0.3, 0.5, 0.7, 0.9],
        "COMPETITIVE_SUPPRESSION_SIGNAL",
        [0.1, 0.2, 0.3, 0.5, 0.7],
    ),
    (
        "decay_rate",
        [0.1, 0.3, 0.5, 0.7, 0.9],
        "_wf_min_threshold",
        [0.05, 0.1, 0.2, 0.3, 0.5],
    ),
]

# ---------------------------------------------------------------------------
# Tier 4: SubconsciousMemory recipe-layer constants
# ---------------------------------------------------------------------------

# Experiment 1: Extraction min length sweep
# Experiment 2: Max items sweep
# Experiment 3: Max tokens sweep
# Experiment 4: Default importance sweep
# Experiment 5: Score weights grid (dict-valued)
# Experiment 6: Write filter thresholds at recipe layer
# Experiment 7: Initial confidence at recipe layer

TIER4_SWEEPS = {
    "extraction_min_length": [5, 8, 10, 15, 20, 30, 50, 80],
    "max_items": [3, 5, 7, 10, 15, 20, 30],
    "max_tokens": [500, 1000, 2000, 4000, 6000, 8000, 12000],
    "default_importance": [
        0.05,
        0.1,
        0.12,
        0.15,
        0.18,
        0.2,
        0.22,
        0.25,
        0.28,
        0.3,
        0.35,
        0.5,
        0.7,
        0.9,
    ],
    "_wf_min_threshold": [0.05, 0.1, 0.2, 0.3, 0.5],
    "initial_confidence": [0.1, 0.3, 0.5, 0.7, 0.9],
}

# Score weights are dict-valued and handled separately
TIER4_SCORE_WEIGHTS = [
    {"relevance": 1.0},
    {"certainty": 1.0},
    {"relevance": 0.6, "certainty": 0.4},
    {"relevance": 0.5, "certainty": 0.5},
    {"relevance": 0.7, "certainty": 0.3},
    {"relevance": 0.4, "certainty": 0.6},
]

# ---------------------------------------------------------------------------
# Tier 5: MemoryLifecycle policy constants
# ---------------------------------------------------------------------------

TIER5_SWEEPS = {
    # Promotion policy thresholds (episodic → semantic)
    "LIFECYCLE_PROMOTION_ACCESS_COUNT": [1, 2, 3, 5, 10],
    "LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD": [0.3, 0.5, 0.6, 0.7, 0.9],
    "LIFECYCLE_PROMOTION_MIN_AGE_SECONDS": [0.0, 60.0, 300.0, 1800.0, 7200.0],
    # Forget policy thresholds
    "LIFECYCLE_FORGET_IMPORTANCE_FLOOR": [0.01, 0.05, 0.1, 0.2, 0.5],
    "LIFECYCLE_FORGET_IDLE_SECONDS": [3600.0, 21600.0, 86400.0, 259200.0, 604800.0],
}

# Experiment 8: Interaction effects at recipe layer
TIER4_INTERACTION_PAIRS = [
    # Expanded to 5×5 grids
    ("max_items", [3, 5, 10, 20, 30], "max_tokens", [500, 1000, 4000, 8000, 12000]),
    (
        "extraction_min_length",
        [5, 10, 20, 30, 50],
        "default_importance",
        [0.1, 0.2, 0.3, 0.5, 0.7],
    ),
    (
        "_wf_min_threshold",
        [0.05, 0.1, 0.2, 0.3, 0.5],
        "initial_confidence",
        [0.1, 0.3, 0.5, 0.7, 0.9],
    ),
    ("max_items", [3, 5, 10, 20, 30], "default_importance", [0.1, 0.2, 0.3, 0.5, 0.7]),
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


def run_tier4(runner, aggregator):
    """Run Tier 4 recipe-layer sweeps.

    Handles both standard single-constant sweeps and the special
    dict-valued score_weights sweep.
    """
    logger.info(
        "Starting Tier 4 sweeps (%d constants + score_weights)...",
        len(TIER4_SWEEPS),
    )
    tier_start = time.monotonic()

    # Standard single-constant sweeps
    for constant_name, values in TIER4_SWEEPS.items():
        logger.info("  Sweeping %s over %s", constant_name, values)
        results = runner.run_single_sweep(constant_name, values)
        aggregator.add_sweep(constant_name, results)

        ok_count = sum(1 for r in results if r.status == "ok")
        logger.info("    %d/%d points OK", ok_count, len(results))

    # Score weights sweep (dict-valued)
    logger.info(
        "  Sweeping score_weights over %d configurations", len(TIER4_SCORE_WEIGHTS)
    )
    results = runner.run_single_sweep("score_weights", TIER4_SCORE_WEIGHTS)
    aggregator.add_sweep("score_weights", results)
    ok_count = sum(1 for r in results if r.status == "ok")
    logger.info("    %d/%d points OK", ok_count, len(results))

    elapsed = time.monotonic() - tier_start
    logger.info("Tier 4 complete in %.1fs", elapsed)


def run_tier4_interactions(runner, aggregator):
    """Run Tier 4 pairwise interaction sweeps."""
    logger.info(
        "Starting Tier 4 interaction sweeps (%d pairs)...",
        len(TIER4_INTERACTION_PAIRS),
    )

    for const_a, vals_a, const_b, vals_b in TIER4_INTERACTION_PAIRS:
        pair_name = f"{const_a}_x_{const_b}"
        logger.info("  Pairwise: %s x %s", const_a, const_b)
        results = runner.run_pairwise_sweep(const_a, vals_a, const_b, vals_b)
        aggregator.add_pairwise(pair_name, results)

        ok_count = sum(1 for r in results if r.status == "ok")
        logger.info("    %d/%d points OK", ok_count, len(results))


def run_parametric(tier_sweeps, tier_name, aggregator, family_weighted=True):
    """Run sweeps using family-aware and parametrically generated scenarios.

    For each constant, uses family-specific scenarios that exercise the
    constant's actual code path, combined with a sample of generic parametric
    scenarios for baseline coverage.

    Args:
        tier_sweeps: dict mapping constant name -> list of values.
        tier_name: human-readable tier label.
        aggregator: ResultsAggregator instance.
        family_weighted: If True (default, 2026-04-17 onward), run multiple
            family-scenario variants per constant and reduce generic-scenario
            count so family scenarios dominate the averaged signal. This is
            the fix for issue #351 — without family weighting, ~5% family
            vs ~95% generic drowned the constant-sensitivity signal.
    """
    from tests.benchmarks.scenarios.factory import ScenarioFactory
    from tests.benchmarks.scenarios.family_factory import (
        CONSTANT_FAMILY_MAP,
        FamilyScenarioFactory,
    )

    logger.info("Generating parametric + family-aware scenarios...")

    if family_weighted:
        # 8 varied family scenarios per family × 4 families = 32 family
        # classes; pick the 8 for the relevant family per constant inside
        # the loop. Generic scenarios are only used as a fallback for
        # constants that have NO matching family (via CONSTANT_FAMILY_MAP).
        # For mapped constants, family scenarios carry 100% of the signal
        # — the prior "4 family + 5 generic" blend diluted decay_rate
        # variance from 0.18 (family-only) to 0.04 (blended) because
        # generic scenarios are insensitive to most Tier-1 constants by
        # construction (their ground truth doesn't exercise the swept code
        # path).
        varied_family = FamilyScenarioFactory.create_varied(n_per_family=8)
        generic_classes = ScenarioFactory.create_all(n=10)
    else:
        varied_family = None
        generic_classes = ScenarioFactory.create_all(n=20)

    logger.info(
        "Starting %s parametric sweeps (%d constants, %d generic scenarios, family_weighted=%s)...",
        tier_name,
        len(tier_sweeps),
        len(generic_classes),
        family_weighted,
    )
    tier_start = time.monotonic()

    for constant_name, values in tier_sweeps.items():
        if family_weighted:
            # Pick the varied family scenarios that match this constant's family.
            target_family = CONSTANT_FAMILY_MAP.get(constant_name)
            if target_family:
                family_for_constant = [
                    c
                    for c in varied_family
                    if c.name.startswith(f"family_{target_family}_")
                ]
                # 8 family + 5 generic: family majority but include some
                # generic coverage for constants that also affect common
                # retrieval paths (e.g. WF_MIN_THRESHOLD filters records
                # in *any* generic scenario that uses WriteFilterMixin).
                combined = family_for_constant + generic_classes
            else:
                # Unmapped constant -> use all families + generics as a
                # safety net.
                combined = varied_family + generic_classes
        else:
            family_for_constant = FamilyScenarioFactory.for_constant(constant_name)
            # Legacy path: include generics
            combined = family_for_constant + generic_classes

        runner = SweepRunner(combined)

        # Compute per-sweep family/generic breakdown for the log line.
        n_family = (
            len(combined)
            if family_weighted and CONSTANT_FAMILY_MAP.get(constant_name)
            else (
                len(combined) - len(generic_classes)
                if generic_classes
                else len(combined)
            )
        )
        n_generic = len(combined) - n_family
        logger.info(
            "  Sweeping %s over %s (%d scenarios: %d family + %d generic)",
            constant_name,
            values,
            len(combined),
            n_family,
            n_generic,
        )
        results = runner.run_single_sweep(constant_name, values)
        aggregator.add_sweep(constant_name, results)

        ok_count = sum(1 for r in results if r.status == "ok")
        logger.info("    %d/%d points OK", ok_count, len(results))

    elapsed = time.monotonic() - tier_start
    logger.info("%s parametric complete in %.1fs", tier_name, elapsed)


def run_ratchet():
    """Run the full ratchet pipeline and print results."""
    from tests.benchmarks.ratchet import RatchetLoop

    logger.info("Starting ratchet loop...")
    loop = RatchetLoop(n_seeds=50)
    summary = loop.run()
    print(summary.format_report())
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run parameter sweeps")
    parser.add_argument(
        "--tier",
        choices=["1", "2", "3", "4", "all"],
        default="all",
        help="Which tier to run",
    )
    parser.add_argument(
        "--interactions",
        action="store_true",
        help="Run interaction effect sweeps",
    )
    parser.add_argument(
        "--parametric",
        action="store_true",
        help="Use parametrically generated scenarios instead of hand-crafted ones",
    )
    parser.add_argument(
        "--ratchet",
        action="store_true",
        help="Run the full ratchet pipeline (generate, sweep, validate, propose)",
    )
    parser.add_argument(
        "--output",
        default="summary.json",
        help="Output filename (default: summary.json)",
    )
    args = parser.parse_args()

    # Ratchet mode: run the full pipeline and exit
    if args.ratchet:
        total_start = time.monotonic()
        run_ratchet()
        total_elapsed = time.monotonic() - total_start
        logger.info("Ratchet complete in %.1fs", total_elapsed)
        return 0

    # Select scenarios based on tier
    if args.tier == "4":
        runner = SweepRunner(TIER4_SCENARIOS)
    else:
        runner = SweepRunner(ALL_SCENARIOS)

    aggregator = ResultsAggregator()

    total_start = time.monotonic()

    if args.parametric:
        # Parametric mode: use generated scenarios for Tiers 1-3
        sweeps = {}
        if args.tier in ("1", "all"):
            sweeps.update(TIER1_SWEEPS)
        if args.tier in ("2", "all"):
            sweeps.update(TIER2_SWEEPS)
        if args.tier in ("3", "all"):
            sweeps.update(TIER3_SWEEPS)
        if sweeps:
            run_parametric(sweeps, f"Tier {args.tier}", aggregator)
    else:
        # Standard mode: use hand-crafted scenarios
        if args.tier in ("1", "all"):
            run_tier(TIER1_SWEEPS, "Tier 1", runner, aggregator)

        if args.tier in ("2", "all"):
            run_tier(TIER2_SWEEPS, "Tier 2", runner, aggregator)

        if args.tier in ("3", "all"):
            run_tier(TIER3_SWEEPS, "Tier 3", runner, aggregator)

    if args.tier in ("4", "all") and not args.parametric:
        # Tier 4 uses recipe-layer scenarios (not parametric)
        tier4_runner = SweepRunner(TIER4_SCENARIOS)
        run_tier4(tier4_runner, aggregator)

    if args.interactions:
        run_interactions(runner, aggregator)
        if args.tier in ("4", "all"):
            tier4_runner = SweepRunner(TIER4_SCENARIOS)
            run_tier4_interactions(tier4_runner, aggregator)

    total_elapsed = time.monotonic() - total_start

    filepath = aggregator.save_results(
        args.output,
        extra_metadata={
            "wall_clock_seconds": round(total_elapsed, 2),
            "tiers_run": args.tier,
            "interactions_run": args.interactions,
            "parametric": args.parametric,
        },
    )

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
