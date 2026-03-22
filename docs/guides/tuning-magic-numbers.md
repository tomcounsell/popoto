# Tuning Magic Numbers: Agent-Memory Constants

This guide documents the ~25 behavioral constants that control Popoto's agent-memory primitives. Each constant has been validated through systematic parameter sweeps across three benchmark scenarios.

## Overview

Popoto's agent-memory stack uses constants that control scoring, decay, strengthening, weakening, filtering, and learning. These were initially set to reasonable guesses and have now been validated through a benchmark harness that measures retrieval quality (precision@k, nDCG) and calibration error across factual recall, multi-step reasoning, and temporal scheduling scenarios.

**Key finding**: The initial defaults are all within their safe operating ranges. The only constant with a cliff effect is `ACTED_CYCLE_STRENGTHEN_FACTOR`, which must be >= 1.0.

## Constant Catalog

### ObservationProtocol Constants

Source: `src/popoto/fields/observation.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `ACTED_CONFIDENCE_SIGNAL` | 0.9 | [0.5, 1.0] | Low |
| `CONTRADICTED_CONFIDENCE_SIGNAL` | 0.1 | [0.05, 0.3] | Low |
| `ACTED_CYCLE_STRENGTHEN_FACTOR` | 1.2 | [1.0, 2.0] | **HIGH** — cliff at <1.0 |
| `DISMISSED_CYCLE_WEAKEN_FACTOR` | 0.8 | [0.3, 1.0] | Low |
| `CONTRADICTED_CYCLE_WEAKEN_FACTOR` | 0.5 | [0.3, 0.8] | Low |
| `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` | 0.1 | [0.05, 0.3] | Low |

### ConfidenceField

Source: `src/popoto/fields/confidence_field.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `initial_confidence` | 0.5 | [0.1, 0.9] | Low |

### WriteFilterMixin

Source: `src/popoto/fields/write_filter.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `_wf_min_threshold` | 0.2 | [0.05, 0.5] | Low |
| `_wf_priority_threshold` | 0.7 | [0.5, 0.9] | Low |

### DecayingSortedField / CyclicDecayField

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `decay_rate` | 0.5 | [0.1, 1.0] | Low |

### CoOccurrenceField

Source: `src/popoto/fields/co_occurrence_field.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `decay_factor` | 0.95 | [0.5, 0.99] | Low |
| `initial_weight` | 0.1 | [0.01, 0.5] | Low |
| `delta` | 0.05 | [0.01, 0.2] | Low |
| `decay_per_hop` | 0.5 | [0.1, 0.9] | Low |

### PredictionLedgerMixin

Source: `src/popoto/fields/prediction_ledger.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `_pl_confidence_error_threshold` | 0.7 | — | Not swept (Tier 2) |
| `_pl_confidence_low_signal` | 0.2 | — | Not swept (Tier 2) |
| `_pl_auto_resolve_errors` | {acted:0.1, dismissed:0.5, contradicted:0.9} | — | Not swept |

### PolicyCache

Source: `src/popoto/recipes/policy_cache.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | 3 | [1, 10] | Low |
| `WILSON_CI_THRESHOLD` | 0.6 | [0.3, 0.8] | Low |
| `TD_ALPHA` | 0.1 | [0.01, 0.5] | Low |
| `TD_GAMMA` | 0.95 | [0.8, 0.99) | Low |
| `CHI_SQUARED_P_THRESHOLD` | 0.05 | — | Not swept |
| `INITIAL_CYCLE_AMPLITUDE` | 0.5 | — | Not swept |

### ContextAssembler

Source: `src/popoto/recipes/context_assembler.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `COMPETITIVE_SUPPRESSION_SIGNAL` | 0.3 | [0.1, 0.7] | Low |
| `DEFAULT_SURFACING_THRESHOLD` | 0.5 | [0.1, 0.9] | Low |

## Cliff Effects

Only one constant showed a cliff effect in the sweep:

**`ACTED_CYCLE_STRENGTHEN_FACTOR`**: Values below 1.0 cause a 23% drop in nDCG@5 for the temporal scheduling scenario. When the strengthen factor is < 1.0, acted outcomes actually weaken cycle amplitude instead of strengthening it, causing the system to suppress recurring tasks that should be reinforced.

**Recommendation**: Keep this constant at >= 1.0. The default of 1.2 is well within the safe zone.

## Interaction Effects

Five pairwise interactions were tested:

1. **`decay_rate` x `initial_confidence`**: No interaction. Both constants are insensitive independently and together.

2. **`_wf_min_threshold` x `initial_weight`**: No interaction. Write filter threshold and co-occurrence initial weight operate independently.

3. **`ACTED_CONFIDENCE_SIGNAL` x `ACTED_CYCLE_STRENGTHEN_FACTOR`**: **Strong interaction**. When strengthen factor < 1.0, nDCG drops to 0.31 regardless of the confidence signal value. Above 1.0, both constants are insensitive.

4. **`TD_ALPHA` x `TD_GAMMA`**: No interaction. These RL constants do not affect retrieval quality in the benchmark scenarios.

5. **`_wf_min_threshold` x `_wf_priority_threshold`**: No interaction. Both operate independently.

## Methodology

### Benchmark Harness

The benchmark harness (`tests/benchmarks/`) includes:

- **Factual Recall**: 13 facts with varying importance, queried via `composite_score`. Measures whether high-importance facts rank first.
- **Multi-Step Reasoning**: 4-item reasoning chain + 5 distractors, linked via `CoOccurrenceField`. Measures whether chain items are retrieved together.
- **Temporal Scheduling**: 8 recurring tasks with `CyclicDecayField`, some recently acted on. Measures whether un-acted tasks surface above recently-acted ones.

### Metrics

- **Precision@k**: Fraction of top-k results that are relevant
- **nDCG@k**: Normalized discounted cumulative gain (rank-sensitive)
- **Calibration Error**: ECE between predicted confidence and actual outcomes
- **MRR**: Mean reciprocal rank of first relevant result

### Sweep Design

Each constant was swept independently while holding others at defaults. Grid sizes ranged from 4 to 7 values per constant. All three scenarios were evaluated per grid point. Total: 19 constants swept, 5 pairwise interactions, ~390 scenario evaluations.

## Running the Benchmarks

```bash
# Run all sweeps
python -m tests.benchmarks.run_sweeps --tier all --interactions

# Run a specific tier
python -m tests.benchmarks.run_sweeps --tier 1

# Run just the harness tests
pytest tests/benchmarks/test_harness.py -x -q

# Run sweep engine tests
pytest tests/benchmarks/test_sweep.py -x -q
```

Results are saved to `tests/benchmarks/results/summary.json`.
