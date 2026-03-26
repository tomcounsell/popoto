# Parametric Sweep: Generated Scenarios, Train/Validation Splits, and Ratchet Loop

The parametric sweep system generates diverse stress-test scenarios from parameterized seeds, splits them into train and validation sets to guard against overfitting, and provides a ratchet loop that automates keep/discard decisions for proposed constant changes.

This extends the existing [benchmark harness](../guides/tuning-magic-numbers.md) without modifying the immutable evaluation infrastructure (metrics, `apply_overrides`, `Scenario` base class).

## Motivation

The 3 hand-crafted field-layer scenarios (`factual_recall`, `multi_step_reasoning`, `temporal_scheduling`) use small record counts (8-13) and similar importance distributions, making Tier 1-3 constant sweeps uninformative -- most constants show zero sensitivity. Manually designing more scenarios risks overfitting to specific test cases.

The parametric system solves both problems: `ScenarioFactory` generates 50 scenarios with varied axes that expose real constant sensitivity, while the train/validation split catches spurious "improvements" that do not generalize.

## Components

### ScenarioFactory

**Module**: `tests/benchmarks/scenarios/factory.py`

Generates `Scenario` subclasses from `ScenarioSeed` dataclasses. Each seed deterministically produces a scenario with specific characteristics via `random.Random(seed_id)`.

**ScenarioSeed axes** (7 parameterizable dimensions):

| Axis | Type | Range | Effect |
|------|------|-------|--------|
| `record_count` | int | 5-100 | Number of Memory records created in setup |
| `importance_shape` | enum | uniform, clustered, bimodal, exponential, flat | Distribution shape for importance values |
| `access_pattern` | enum | all_recent, half_stale, mostly_stale, interleaved | Which records get recent access (affects decay) |
| `outcome_frequency` | float | 0.0-1.0 | Fraction of records that receive "acted" outcomes |
| `noise_ratio` | float | 0.0-0.5 | Fraction of records that are pure noise (importance < 0.15) |
| `link_density` | float | 0.0-1.0 | Fraction of record pairs with co-occurrence links |
| `age_spread_days` | int | 1-365 | Range of record ages (affects temporal decay separation) |

**Key APIs**:

```python
from tests.benchmarks.scenarios.factory import ScenarioFactory, ScenarioSeed

# Generate 50 diverse seeds (deterministic)
seeds = ScenarioFactory.default_seeds(n=50)

# Create a single scenario class from a seed
scenario_class = ScenarioFactory.create(seeds[0])

# Create all scenario classes at once
scenario_classes = ScenarioFactory.create_all(n=50)
```

Generated scenario classes conform to the standard `Scenario` interface -- they have a `name` attribute, accept `overrides` in the constructor, and return a valid `ScenarioResult` from `execute()`.

### Train/Validation Split

**Module**: `tests/benchmarks/split.py`

Partitions seeds into disjoint train (70%) and validation (30%) sets by seed ID. The split is deterministic and fixed -- seed IDs 0-34 go to train, 35-49 go to validation.

`SplitRunner` wraps two `SweepRunner` instances and provides a `sweep_and_validate()` method that:

1. Sweeps a constant on train scenarios to find the optimal value.
2. Evaluates the optimal value AND the current default on validation scenarios.
3. Compares and produces an accept/reject `ValidationResult`.

A proposed change is accepted only if the validation delta exceeds the improvement threshold (default 0.01). This guards against overfitting to the train set.

```python
from tests.benchmarks.split import SplitRunner, make_split
from tests.benchmarks.scenarios.factory import ScenarioFactory

seeds = ScenarioFactory.default_seeds(50)
train_seeds, val_seeds = make_split(seeds)

train_scenarios = ScenarioFactory.create_all(train_seeds)
val_scenarios = ScenarioFactory.create_all(val_seeds)

runner = SplitRunner(train_scenarios, val_scenarios)
result = runner.sweep_and_validate("decay_rate", [0.1, 0.3, 0.5, 0.7])
# result.recommendation is "accept" or "reject"
# result.delta is validation_score - validation_default_score
```

### Ratchet Loop

**Module**: `tests/benchmarks/ratchet.py`

Automates the full keep/discard pipeline for all Tier 1-3 constants:

1. Generate 50 parametric scenarios and split into train/validation.
2. For each constant: sweep on train, validate on held-out set.
3. Check cliff safety margins (10% buffer from detected cliff boundaries).
4. Produce a `RatchetSummary` with accept/reject/no-sensitivity decisions.

The ratchet **never writes to `constants.py`** -- it outputs a human-readable report.

```python
from tests.benchmarks.ratchet import RatchetLoop

loop = RatchetLoop(n_seeds=50)
summary = loop.run()
print(summary.format_report())
```

**Example output**:

```
RATCHET SUMMARY
======================================================================
Total constants evaluated: 21
Accepted: 2
Rejected: 5
No sensitivity: 14
Duration: 45.3s

Proposed changes (validated on held-out scenarios):
  decay_rate                                        0.5 ->  0.3     (train +0.080, validation +0.050) ACCEPT
  decay_factor                                     0.95 -> 0.85     (train +0.060, validation +0.040) ACCEPT

Rejected (insufficient validation improvement):
  ACTED_CONFIDENCE_SIGNAL                           0.9 ->  0.7     (train +0.030, validation -0.010) REJECT: No significant ...
```

Each `RatchetDecision` includes: current value, proposed value, train/validation deltas, action (accept/reject/no_sensitivity), reason, and cliff warning details.

## CLI Flags

Added to `tests/benchmarks/run_sweeps.py`:

| Flag | Effect |
|------|--------|
| `--parametric` | Use 50 generated scenarios instead of hand-crafted ones for Tiers 1-3. Existing `--tier` flags select which tiers to sweep. Tier 4 (recipe-layer) is unaffected. |
| `--ratchet` | Run the full ratchet pipeline: generate scenarios, sweep all Tier 1-3 constants, validate on held-out set, print summary report. |

**Backward compatibility**: The default behavior (no flags) continues to use hand-crafted scenarios. The `--tier` and `--interactions` flags work as before.

```bash
# Parametric sweep of Tier 1 constants
python -m tests.benchmarks.run_sweeps --parametric --tier 1

# Full ratchet pipeline
python -m tests.benchmarks.run_sweeps --ratchet
```

## Data Flow

```
ScenarioFactory.default_seeds(50)
    |
    v
[ScenarioSeed x 50] --split--> Train seeds (0-34)  |  Validation seeds (35-49)
    |                               |                       |
    v                               v                       v
ScenarioFactory.create()     SweepRunner(train)      SweepRunner(validation)
    |                               |                       |
    v                               v                       v
Type[Scenario] x 50          SweepPoint results       SweepPoint results
                                    |                       |
                                    v                       v
                             ResultsAggregator -----> RatchetLoop
                                                          |
                                                          v
                                                     RatchetSummary
                                                     (human-readable report)
```

## Design Decisions

- **Deterministic seeds**: `ScenarioFactory.default_seeds()` uses `random.Random(42)` for reproducible seed generation. Each scenario uses `random.Random(seed_id)` internally.
- **No parallelism**: Sequential execution avoids Redis key conflicts and is fast enough (target: under 60 seconds for a full ratchet run).
- **Immutable harness**: The factory generates standard `Scenario` subclasses. No changes to `SweepRunner`, `ResultsAggregator`, `Scenario` base, metrics, or `apply_overrides`.
- **No auto-writes**: The ratchet proposes changes for human review. It never modifies `constants.py`.
- **Tier 4 excluded**: Recipe-layer scenarios use fixture-driven multi-turn simulation, which is too complex for parametric generation. Tier 4 continues to use hand-crafted scenarios exclusively.

## Related

- [Tuning Magic Numbers](../guides/tuning-magic-numbers.md) -- constant catalog and benchmark methodology
- [Plan: Parametric Sweep Redesign](../plans/parametric_sweep_redesign.md) -- design document
- [Issue #293](https://github.com/tomcounsell/popoto/issues/293) -- tracking issue
