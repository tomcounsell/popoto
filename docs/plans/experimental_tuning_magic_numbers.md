---
status: Planning
type: chore
appetite: Large
owner: Tom Counsell
created: 2026-03-22
tracking: https://github.com/tomcounsell/popoto/issues/234
last_comment_id: IC_kwDOExCOnM70NIl8
---

# Experimental Tuning for Agent-Memory Magic Numbers

## Problem

Popoto's agent-memory primitives ship with ~25 behavioral constants that were set to reasonable initial guesses. These constants control scoring, decay, strengthening, weakening, filtering, and learning -- small changes to any of them produce measurable differences in retrieval quality and learning speed. Without systematic experimentation, there is no evidence that the current defaults are optimal or even close.

**Current behavior:**
All magic numbers use hard-coded best-guess defaults. No benchmark harness exists to measure how changes to these constants affect agent performance. Developers tuning constants do so by intuition.

**Desired outcome:**
A benchmark harness that measures retrieval relevance and calibration error across task types, parameter sweep results for each Category 1 constant, interaction effect analysis for correlated pairs, and updated defaults backed by experimental evidence.

## Prior Art

No prior issues found related to experimental tuning or benchmarking of these constants. This is greenfield work. The constants themselves were introduced across 14 separate PRs as part of the agent-memory primitives rollout.

## Data Flow

The tuning harness sits outside the core ORM -- it exercises the primitives through their public APIs:

1. **Entry point**: Benchmark script loads test scenarios (factual recall, multi-step reasoning, temporal scheduling)
2. **Model setup**: Creates Memory model instances with known characteristics using Popoto's ORM layer
3. **Lifecycle simulation**: Exercises ObservationProtocol outcomes (acted/dismissed/deferred/contradicted), triggering ConfidenceField updates, CyclicDecayField strengthen/weaken, PredictionLedger resolution, and CoOccurrence edge management
4. **Query evaluation**: Runs `composite_score`, `decayed_top`, and confidence-filtered queries against the test data
5. **Metric collection**: Measures retrieval relevance (precision@k, nDCG) and calibration error (predicted vs actual outcome match rates)
6. **Output**: Parameter sweep results as structured data (CSV/JSON) plus summary report

## Appetite

**Size:** Large

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 2-3 (scope alignment on which constants matter most, review of initial findings)
- Review rounds: 2+ (methodology review, results review)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| All 14 agent-memory primitives shipped | `python -c "from popoto.fields import DecayingSortedField, CyclicDecayField, ConfidenceField, CoOccurrenceField; from popoto.fields.observation import ObservationProtocol; from popoto.fields.prediction_ledger import PredictionLedgerMixin; from popoto.fields.write_filter import WriteFilterMixin; from popoto.fields.access_tracker import AccessTrackerMixin; from popoto.recipes.policy_cache import PolicyCache"` | All primitives importable |
| Redis running | `redis-cli ping` | Required for integration tests |

## Solution

### Key Elements

- **Benchmark harness**: A test framework that runs reproducible scenarios against Popoto's agent-memory primitives, measuring retrieval quality metrics
- **Parameter sweep engine**: Systematic variation of each constant independently while holding others fixed, producing sensitivity curves
- **Interaction effect analysis**: Targeted pairwise experiments for constants that are theoretically coupled
- **Results aggregator**: Collects metrics across runs and produces summary reports with optimal ranges

### Flow

**Define scenarios** -> Configure parameter grid -> **Run sweep** (per-constant, fixed others) -> Collect metrics -> **Analyze sensitivity** -> Identify interactions -> **Run pairwise sweeps** -> **Report optimal ranges** -> Update defaults

### Technical Approach

- Benchmark harness lives in `tests/benchmarks/` (not in `src/`) -- it is a development tool, not a library feature
- Each scenario is a self-contained function that creates models, simulates a lifecycle, and returns metrics
- Parameter overrides are injected via model class attributes and field constructor kwargs (no monkey-patching)
- Results stored as JSON files in `tests/benchmarks/results/` (gitignored) with a summary committed
- Constants are organized into three tiers based on expected sensitivity:
  - **Tier 1 (High sensitivity)**: `decay_rate`, `initial_confidence`, `_wf_min_threshold`, confidence signals (0.9/0.1)
  - **Tier 2 (Medium sensitivity)**: cycle factors (1.2/0.8/0.5), `decay_factor`, `initial_weight`, `delta`
  - **Tier 3 (PolicyCache)**: `MIN_EVENTS_FOR_CRYSTALLIZATION`, `TD_ALPHA`, `TD_GAMMA`, `WILSON_CI_THRESHOLD`

## Complete Catalog of Constants

### From ObservationProtocol (`src/popoto/fields/observation.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| Acted confidence signal | `0.9` | 198 | Signal sent to ConfidenceField on "acted" outcome |
| Contradicted confidence signal | `0.1` | 284 | Signal sent to ConfidenceField on "contradicted" outcome |
| Acted cycle strengthen factor | `1.2` | 188 | CyclicDecayField amplification on "acted" |
| Dismissed cycle weaken factor | `0.8` | 230 | CyclicDecayField damping on "dismissed" |
| Contradicted cycle weaken factor | `0.5` | 276 | CyclicDecayField aggressive damping on "contradicted" |
| Auto-discharge confidence threshold | `0.1` | 304 | Below this confidence, pressure is auto-resolved |

### From WriteFilterMixin (`src/popoto/fields/write_filter.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| `_wf_min_threshold` | `0.2` | 59 | Minimum filter score to persist a record |
| `_wf_priority_threshold` | `0.7` | 60 | Score above which record is tagged as priority |

### From ConfidenceField (`src/popoto/fields/confidence_field.py`)

| Constant | Default | Effect |
|----------|---------|--------|
| `initial_confidence` | `0.5` | Starting confidence for new records |

### From DecayingSortedField / CyclicDecayField

| Constant | Default | Effect |
|----------|---------|--------|
| `decay_rate` | `0.5` | Power-law decay exponent for time-based relevance |

### From CoOccurrenceField (`src/popoto/fields/co_occurrence_field.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| `decay_factor` | `0.95` | 218 | Multiplicative decay for `weaken_all()` |
| `initial_weight` | `0.1` | 271 | Default edge weight for new co-occurrence links |
| `delta` (strengthen) | `0.05` | implicit | Weight increment per co-occurrence |
| `decay_per_hop` | `0.5` | 505 | Weight multiplier per hop in spreading activation |

### From PredictionLedgerMixin (`src/popoto/fields/prediction_ledger.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| `_pl_confidence_error_threshold` | `0.7` | 109 | Error above which confidence is reduced |
| `_pl_confidence_low_signal` | `0.2` | 110 | Signal sent to ConfidenceField on high error |
| Acted prediction error | `0.1` | 112 | Error value for auto-resolved "acted" |
| Dismissed prediction error | `0.5` | 113 | Error value for auto-resolved "dismissed" |
| Contradicted prediction error | `0.9` | 114 | Error value for auto-resolved "contradicted" |

### From PolicyCache (`src/popoto/recipes/policy_cache.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | `3` | 80 | Minimum observations before pattern crystallizes |
| `WILSON_CI_THRESHOLD` | `0.6` | 85 | Confidence interval threshold for crystallization |
| `TD_ALPHA` | `0.1` | 89 | Q-value learning rate |
| `TD_GAMMA` | `0.95` | 93 | Q-value discount factor |
| `CHI_SQUARED_P_THRESHOLD` | `0.05` | 97 | Statistical significance threshold |
| `INITIAL_CYCLE_AMPLITUDE` | `0.5` | 101 | Starting amplitude for discovered cycles |

### From InteractionWeight (`src/popoto/fields/constants.py`)

| Constant | Default | Line | Effect |
|----------|---------|------|--------|
| `HUMAN` | `6.0` | 44 | Source weight for human interactions |
| `AGENT` | `1.0` | 45 | Source weight for agent interactions |
| `SYSTEM` | `0.2` | 46 | Source weight for system interactions |
| `EXECUTIVE` | `44.0` | 48 | Role weight for executive authority |
| `MANAGER` | `16.0` | 49 | Role weight for manager authority |
| `PEER` | `6.0` | 50 | Role weight for peer authority |
| `SUBORDINATE` | `1.0` | 51 | Role weight for subordinate authority |

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Benchmark harness must handle Redis connection failures gracefully (report "skipped" not crash)
- [ ] Parameter values outside valid ranges (negative decay_rate, threshold > 1.0) must raise clear errors, not produce silent garbage

### Empty/Invalid Input Handling
- [ ] Scenarios with zero memories, single memory, and large memory sets must all produce valid metrics
- [ ] Parameter sweep with boundary values (0.0, 1.0) must not cause division-by-zero or infinite loops

### Error State Rendering
- [ ] Results report must flag invalid runs (e.g., all records filtered by write filter) rather than reporting misleading 0% metrics

## Test Impact

No existing tests affected -- this is a greenfield benchmarking harness. The harness itself lives in `tests/benchmarks/` and does not modify any existing test files. If defaults are updated based on findings, existing unit tests for each field will need their expected values adjusted in a follow-up PR.

## Rabbit Holes

- **Bayesian optimization / AutoML**: Tempting to use fancy hyperparameter optimization libraries, but simple grid search with a few hundred runs per constant is sufficient for 25 constants. Save Bayesian optimization for when the search space proves too large.
- **Realistic agent workloads**: Building a full agent simulation to generate "real" workloads would take weeks and still be artificial. Use synthetic but controlled scenarios instead.
- **Category 2 (structural capacity) constants**: Application-dependent, not tunable generically. Out of scope per the issue.
- **Continuous integration benchmarks**: Running parameter sweeps in CI is expensive and slow. The harness is a local development tool.

## Risks

### Risk 1: Dependencies not shipped
**Impact:** Benchmark harness cannot exercise all constants if primitives like StreamConsumer or ContextAssembler are still in progress.
**Mitigation:** Design harness to be modular -- each primitive's benchmarks are independent. Run what is available, add the rest incrementally.

### Risk 2: Metric design is wrong
**Impact:** Optimizing for the wrong metric produces constants that perform well on benchmarks but poorly in real use.
**Mitigation:** Use multiple metrics (precision@k, nDCG, calibration error) and multiple scenario types. Look for constants that are robust across metrics, not optimal for one.

### Risk 3: Interaction effects are combinatorial
**Impact:** 25 constants means 300 pairwise interactions -- too many to test exhaustively.
**Mitigation:** Only test theoretically coupled pairs (e.g., decay_rate x initial_confidence, _wf_min_threshold x initial_weight). The issue identifies these already.

## Race Conditions

No race conditions identified -- the benchmark harness is a sequential, single-process tool. Each run creates fresh Redis state and cleans up after itself.

## No-Gos (Out of Scope)

- Category 2 (structural capacity) constants -- application-dependent
- Category 3 (edge pruning thresholds) -- low impact, spot-check only
- Category 4 (domain constants) -- fixed by design
- Changing the ORM's public API or field signatures
- Building a persistent benchmark database or dashboard
- Running benchmarks in CI

## Update System

No update system changes required -- the benchmark harness is a development-only tool in `tests/benchmarks/`.

## Agent Integration

No agent integration required -- this is a development-only benchmarking tool.

## Documentation

### Feature Documentation
- [ ] Create `docs/guides/tuning-magic-numbers.md` with the catalog of constants, their ranges, and sensitivity findings
- [ ] Update `docs/guides/popoto-memory-roadmap.md` to mark this step as complete

### Inline Documentation
- [ ] Each constant in the catalog should have a docstring explaining its role and optimal range
- [ ] Benchmark harness README in `tests/benchmarks/README.md`

## Success Criteria

- [ ] Benchmark harness runs 3+ scenario types (factual recall, multi-step reasoning, temporal scheduling)
- [ ] Parameter sweep results exist for all 25 Category 1 constants
- [ ] Sensitivity curves identify cliff effects and plateaus for each constant
- [ ] Interaction effect matrix covers at least 5 theoretically coupled pairs
- [ ] Updated defaults are proposed with supporting evidence
- [ ] Documentation of optimal ranges and sensitivity analysis published
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (harness)**
  - Name: harness-builder
  - Role: Build the benchmark harness framework and scenario definitions
  - Agent Type: builder
  - Resume: true

- **Builder (sweeps)**
  - Name: sweep-runner
  - Role: Implement parameter sweep engine and results aggregation
  - Agent Type: builder
  - Resume: true

- **Validator (results)**
  - Name: results-validator
  - Role: Verify sweep results are statistically sound and reproducible
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: tuning-documentarian
  - Role: Write the tuning guide with findings
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Build benchmark harness framework
- **Task ID**: build-harness
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_harness.py` (create)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/` directory structure with `conftest.py`, `scenarios/`, and `metrics/`
- Implement base `Scenario` class with setup/run/teardown lifecycle and Redis cleanup
- Implement metric collectors: precision@k, nDCG, calibration error
- Build 3 scenarios: factual_recall, multi_step_reasoning, temporal_scheduling

### 2. Build parameter sweep engine
- **Task ID**: build-sweep
- **Depends On**: build-harness
- **Validates**: `tests/benchmarks/test_sweep.py` (create)
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Implement `ParameterGrid` that generates constant override combinations
- Implement `SweepRunner` that executes scenarios with overrides and collects results
- Implement `ResultsAggregator` that produces JSON output and summary statistics
- Support single-constant sweeps and pairwise interaction sweeps

### 3. Run Tier 1 sweeps (high sensitivity)
- **Task ID**: sweep-tier1
- **Depends On**: build-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: true
- Sweep `decay_rate` over [0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0]
- Sweep `initial_confidence` over [0.1, 0.3, 0.5, 0.7, 0.9]
- Sweep `_wf_min_threshold` over [0.05, 0.1, 0.2, 0.3, 0.5]
- Sweep confidence signals (acted/contradicted) over [0.1, 0.3, 0.5, 0.7, 0.9]

### 4. Run Tier 2 sweeps (medium sensitivity)
- **Task ID**: sweep-tier2
- **Depends On**: build-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: true
- Sweep cycle factors (strengthen/weaken) over [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0]
- Sweep CoOccurrence constants: `decay_factor`, `initial_weight`, `delta`, `decay_per_hop`

### 5. Run Tier 3 sweeps (PolicyCache)
- **Task ID**: sweep-tier3
- **Depends On**: build-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: true
- Sweep `MIN_EVENTS_FOR_CRYSTALLIZATION` over [1, 2, 3, 5, 10]
- Sweep `TD_ALPHA` over [0.01, 0.05, 0.1, 0.2, 0.5]
- Sweep `TD_GAMMA` over [0.8, 0.9, 0.95, 0.99]

### 6. Run interaction effect sweeps
- **Task ID**: sweep-interactions
- **Depends On**: sweep-tier1, sweep-tier2
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Pairwise: `decay_rate` x `initial_confidence`
- Pairwise: `_wf_min_threshold` x `initial_weight`
- Pairwise: confidence signal (acted) x cycle strengthen factor
- Pairwise: `TD_ALPHA` x `TD_GAMMA`
- Pairwise: `_wf_min_threshold` x `_wf_priority_threshold`

### 7. Validate results
- **Task ID**: validate-results
- **Depends On**: sweep-tier1, sweep-tier2, sweep-tier3, sweep-interactions
- **Assigned To**: results-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify reproducibility: re-run 3 random sweeps, confirm results within 5% tolerance
- Verify cliff effects and plateaus are consistent across scenario types
- Flag any constants where optimal value differs significantly between scenarios

### 8. Update defaults
- **Task ID**: update-defaults
- **Depends On**: validate-results
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Update default values in source files based on sweep findings
- Add inline comments documenting the optimal range and sensitivity
- Ensure all existing tests pass with new defaults (update expected values if needed)

### 9. Documentation
- **Task ID**: document-findings
- **Depends On**: update-defaults
- **Assigned To**: tuning-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/guides/tuning-magic-numbers.md` with full catalog, ranges, and recommendations
- Update roadmap to mark tuning as complete
- Add README to `tests/benchmarks/`

### 10. Final Validation
- **Task ID**: validate-all
- **Depends On**: update-defaults, document-findings
- **Assigned To**: results-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite with updated defaults
- Verify documentation is accurate and complete
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Harness runs | `pytest tests/benchmarks/test_harness.py -x -q` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| All constants documented | `python -c "import json; data=json.load(open('tests/benchmarks/results/summary.json')); assert len(data['constants']) >= 19"` | exit code 0 |

---

## Open Questions

1. **Dependency readiness**: The issue lists #228 (PredictionLedger) and #229 (StreamConsumer) as dependencies, plus PolicyCache and ContextAssembler (TBD). Should the plan proceed with whatever primitives are currently shipped, or block until all 14 are available?

2. **InteractionWeight constants**: The `InteractionWeight` class in `constants.py` has 7 constants (HUMAN, AGENT, SYSTEM, EXECUTIVE, MANAGER, PEER, SUBORDINATE). The issue did not list these but they are Category 1 behavioral constants. Should they be included in the sweep?

3. **Default update policy**: When sweep results suggest a different default, should the change be applied immediately (potentially breaking existing applications relying on current defaults) or should new defaults be opt-in via a version flag?

4. **Scenario design**: The issue mentions "progressive stack activation" from the roadmap Step 12 table. Should the benchmark scenarios mirror that exact activation sequence, or is it acceptable to design independent scenarios per primitive?
