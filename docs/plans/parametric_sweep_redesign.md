---
status: Shipped
type: enhancement
appetite: Large
owner: Valor Engels
created: 2026-03-26
tracking: https://github.com/tomcounsell/popoto/issues/293
---

# Parametric Scenario Generation with Train/Validation Splits and Ratchet Loop

## Problem

The sweep system evaluates ~31 behavioral constants by running scenarios and measuring retrieval quality (nDCG@5). Currently, Tiers 1-3 (21 constants) produce a flat nDCG@5 of 0.770 regardless of value -- the existing 3 field-layer scenarios (`factual_recall`, `multi_step_reasoning`, `temporal_scheduling`) don't stress those constants. Each scenario has hardcoded record counts (8-13), fixed importance distributions, and a single access pattern. 16 of 19 Tier 1-3 constants show zero sensitivity.

Issue #279 Phase 2 proposed hand-crafting 5 new stress-test scenarios, but manually designing scenarios per constant risks overfitting to those specific test cases -- the same problem with more steps.

**Current behavior:** Most constants show no measurable sensitivity across the 3 hand-crafted scenarios. Sweep results are uninformative for tuning.

**Desired outcome:** A `ScenarioFactory` that generates diverse scenarios from parameterized seeds, with train/validation splits to guard against overfitting and a ratchet loop to automate keep/discard decisions. At least 3 Tier 1-3 constants show measurable sensitivity (nDCG variance > 0.05) across generated scenarios.

## Prior Art

- **PR #250 / Issue #234**: Built the initial sweep infrastructure -- `SweepRunner`, `ResultsAggregator`, `Scenario` base class, `ParameterGrid`, override injection, metric computation. This is the foundation we extend.
- **PR #253**: Centralized constants into `fields/constants.py` with the `Defaults` class. Made override injection via `apply_overrides()` possible.
- **PR #292 / Issue #279**: Expanded sweep coverage for phases 1/3/4 -- added pairwise interaction sweeps, Tier 4 recipe-layer experiments, degenerate value detection, results persistence. Phases 2 and 5 are replaced by this issue.
- **PR #276 / Issue #273**: Added Tier 4 SubconsciousMemory recipe-layer scenarios (`SupportAgentScenario`, `CodingAssistantScenario`, `ResearchAgentScenario`) with `RecipeScenario` base class, fixture-driven multi-turn simulation, and extraction/token metrics.
- **karpathy/autoresearch**: Inspiration for the immutable-harness + diverse-data + ratchet pattern. Avoids overfitting by using a large fixed diverse dataset with an immutable evaluation harness.

## Spike Results

### spike-1: What axes produce meaningful nDCG variance for Tier 1-3 constants?

- **Assumption**: "Varying record count and importance distribution will expose sensitivity in decay and confidence constants"
- **Method**: Code analysis of the 3 existing scenarios and how they interact with `DecayingSortedField`, `ConfidenceField`, `ObservationProtocol`, and `composite_score`.
- **Finding**: The existing scenarios all use similar importance distributions (linear spread from ~0.05 to ~0.9) and small record counts (8-13). The `composite_score` query returns results ranked by a weighted sum of sorted-set scores. With so few records, the ranking is trivially correct regardless of decay_rate or confidence_signal values. To create differentiation, scenarios need: (a) larger record counts (30-100) so ranking actually matters, (b) clustered importance distributions where many records have similar scores (forcing decay/confidence to break ties), (c) varied access patterns (some records accessed recently, others stale) so temporal decay creates real score differences, and (d) varied outcome frequencies so confidence signals have something to amplify/suppress.
- **Confidence**: High -- the math is straightforward. With 13 records spread across the 0.05-0.9 range, composite_score ranking is determined by importance alone. Constants only matter when scores are close enough that the constant's contribution changes the ordering.
- **Impact on plan**: Confirms the 7 parameterizable axes listed in the issue. Record count and importance distribution shape are the highest-leverage axes.

### spike-2: Can generated scenarios reuse the existing Scenario base class without modification?

- **Assumption**: "ScenarioFactory output must conform to the existing Scenario interface"
- **Method**: Code-read of `Scenario` base class, `SweepRunner.run_single_sweep()`, and `FactualRecallScenario`.
- **Finding**: `SweepRunner` expects `scenario_classes` -- a list of classes, each with a `name` attribute and an `__init__(overrides)` constructor. It instantiates via `sc_class(overrides=overrides)` and calls `.execute()`. A generated scenario class can satisfy this interface by subclassing `Scenario` and implementing `setup()`/`run()`. The factory produces class objects (not instances), each with a unique `name`. No changes to `SweepRunner` or `Scenario` needed.
- **Confidence**: High -- direct code inspection.
- **Impact on plan**: ScenarioFactory returns `Type[Scenario]` objects. The existing runner handles them like any other scenario class.

### spike-3: Performance feasibility of 50+ scenarios in <30 seconds

- **Assumption**: "Each scenario takes ~500ms based on existing harness timings"
- **Method**: Inference from existing sweep results and code structure.
- **Finding**: Each scenario involves: Redis writes (N records), an `ObservationProtocol.on_context_used()` call, a `composite_score` query, and Redis cleanup. With N=10, this takes ~200-400ms. With N=50-100 (larger generated scenarios), expect ~400-800ms per scenario execution. Running 50 scenarios sequentially: 50 * 600ms = 30s. This is tight but feasible. Optimization: scenarios with smaller record counts (5-20) run faster; mixing sizes keeps total time down. Parallelization is not needed for the initial implementation but could be added later.
- **Confidence**: Medium -- depends on actual Redis performance. The 30s target is achievable with a mix of small and medium scenarios.
- **Impact on plan**: Default scenario generation should produce a mix of sizes (5-100 records). Add a `max_records` parameter to the factory for tuning the speed/coverage tradeoff.

## Solution

Three connected components that extend the existing harness without modifying the immutable evaluation infrastructure (metrics, `apply_overrides`, `Scenario` base class):

### Component 1: ScenarioFactory

A new module `tests/benchmarks/scenarios/factory.py` that generates `Scenario` subclasses from parameterized seed configurations.

**Parameterizable axes** (each is a seed parameter):

| Axis | Type | Range | Effect |
|------|------|-------|--------|
| `record_count` | int | 5-100 | Number of Memory records created in setup |
| `importance_shape` | enum | uniform, clustered, bimodal, exponential, flat | Distribution shape for importance values |
| `access_pattern` | enum | all_recent, half_stale, mostly_stale, interleaved | Which records get recent access (affects decay) |
| `outcome_frequency` | float | 0.0-1.0 | Fraction of records that receive "acted" outcomes |
| `noise_ratio` | float | 0.0-0.5 | Fraction of records that are pure noise (importance < 0.15) |
| `link_density` | float | 0.0-1.0 | Fraction of record pairs with co-occurrence links |
| `age_spread_days` | int | 1-365 | Range of record ages (affects temporal decay separation) |

**Seed-to-scenario mapping:**

```python
@dataclass
class ScenarioSeed:
    """Deterministic configuration for generating a Scenario class."""
    seed_id: int
    record_count: int
    importance_shape: str  # "uniform", "clustered", "bimodal", "exponential", "flat"
    access_pattern: str    # "all_recent", "half_stale", "mostly_stale", "interleaved"
    outcome_frequency: float
    noise_ratio: float
    link_density: float
    age_spread_days: int
```

`ScenarioFactory.create(seed: ScenarioSeed) -> Type[Scenario]` builds a concrete `Scenario` subclass whose `setup()` creates records according to the seed parameters and whose `run()` queries via `composite_score` and returns `ScenarioResult` with proper ground truth.

**Determinism**: Given the same seed, the factory produces the same scenario with the same records. Uses `random.Random(seed_id)` for all stochastic decisions within a scenario.

**Default seed bank**: `ScenarioFactory.default_seeds(n=50) -> List[ScenarioSeed]` produces a deterministic list of 50 diverse seeds by iterating over axis combinations. The seeds are designed to cover the cross-product of axes with emphasis on high-leverage combinations (large record count + clustered importance + mixed staleness).

### Component 2: Train/Validation Split

A new module `tests/benchmarks/split.py` that partitions generated scenarios into disjoint train and validation sets.

**Split strategy:**
- `ScenarioFactory.default_seeds(n=50)` produces 50 seeds with IDs 0-49.
- Seeds 0-34 (70%) form the **train set** -- used for sweeping and finding optimal values.
- Seeds 35-49 (30%) form the **validation set** -- used to verify that optimal values generalize.
- The split is deterministic and fixed. No random shuffling -- seed IDs determine the partition.

**Overfitting guard:**
After sweeping on the train set, the ratchet loop re-evaluates each constant's proposed optimal value on the validation set. If a constant's best value on train does NOT improve nDCG@5 on validation (within a tolerance of 0.01), the proposed change is rejected as spurious. This directly addresses the overfitting risk of hand-crafted scenarios.

**Implementation:**
```python
class SplitRunner:
    """Run sweeps on train scenarios, validate on held-out scenarios."""

    def __init__(self, train_scenarios, validation_scenarios):
        self.train_runner = SweepRunner(train_scenarios)
        self.validation_runner = SweepRunner(validation_scenarios)

    def sweep_and_validate(self, constant_name, values):
        """Sweep on train, validate best value on held-out set."""
        train_results = self.train_runner.run_single_sweep(constant_name, values)
        # Find best on train
        train_agg = ResultsAggregator()
        train_agg.add_sweep(constant_name, train_results)
        best_value, train_score = train_agg.get_optimal_value(constant_name)
        # Validate on held-out set
        val_results = self.validation_runner.run_single_sweep(constant_name, [best_value, current_default])
        # Compare best vs default on validation
        ...
```

### Component 3: Ratchet Loop

A new module `tests/benchmarks/ratchet.py` that automates the keep/discard decision for each constant's proposed optimal value.

**Ratchet algorithm:**
1. For each constant in Tiers 1-3:
   a. Sweep on train scenarios, find optimal value.
   b. Evaluate optimal value AND current default on validation scenarios.
   c. Compute `delta = validation_ndcg(optimal) - validation_ndcg(default)`.
   d. If `delta > 0.01` (improvement threshold): ACCEPT the proposed change.
   e. If the optimal value is within 10% of a detected cliff: REJECT with "too close to cliff" warning.
   f. Otherwise: REJECT as "no significant improvement on validation".
2. Produce a summary report: proposed changes, accepted/rejected, safety margins, cliff warnings.
3. The ratchet NEVER writes to `constants.py` directly. It outputs a human-readable diff proposal.

**Cliff safety margin:**
Using `ResultsAggregator.detect_cliff_effects()`, the ratchet checks whether the proposed value is within 10% of any cliff boundary. If so, it shifts the proposal away from the cliff by the safety margin, or rejects if no safe value exists.

**Output format:**
```
RATCHET SUMMARY
===============
Proposed changes (validated on held-out scenarios):
  decay_rate:              0.5 -> 0.3  (train +0.08, validation +0.05) ACCEPT
  ACTED_CONFIDENCE_SIGNAL: 0.9 -> 0.7  (train +0.03, validation -0.01) REJECT (no validation improvement)
  decay_factor:            0.95 -> 0.85 (train +0.06, validation +0.04, cliff at 0.80) ACCEPT (margin: 0.05)

Rejected (no sensitivity):
  TD_ALPHA, TD_GAMMA, WILSON_CI_THRESHOLD, ...
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
                                                     Proposed defaults diff
                                                     (human-readable report)
```

## Tasks

### Phase 1: ScenarioFactory (core generation)

- [ ] Create `tests/benchmarks/scenarios/factory.py` with `ScenarioSeed` dataclass
- [ ] Implement `ScenarioFactory.create(seed) -> Type[Scenario]` -- builds a `Scenario` subclass that creates `record_count` records with the specified importance distribution, access pattern, outcome frequency, noise ratio, and age spread
- [ ] Implement importance distribution generators: `uniform`, `clustered`, `bimodal`, `exponential`, `flat`
- [ ] Implement access pattern simulators: `all_recent`, `half_stale`, `mostly_stale`, `interleaved` (manipulate DecayingSortedField scores via time offsets)
- [ ] Implement `ScenarioFactory.default_seeds(n=50)` -- deterministic diverse seed bank
- [ ] Add co-occurrence link generation based on `link_density` parameter
- [ ] Verify generated scenarios pass the `Scenario` interface (execute returns valid `ScenarioResult`)

### Phase 2: Train/Validation Split

- [ ] Create `tests/benchmarks/split.py` with `SplitRunner` class
- [ ] Implement deterministic 70/30 split by seed ID
- [ ] `sweep_and_validate()` method: sweep on train, evaluate best + default on validation
- [ ] Return structured result: train_score, validation_score, delta, accept/reject recommendation

### Phase 3: Ratchet Loop

- [ ] Create `tests/benchmarks/ratchet.py` with `RatchetLoop` class
- [ ] Implement ratchet algorithm: iterate Tiers 1-3, sweep + validate, accept/reject with cliff safety margins
- [ ] Implement cliff safety margin check (10% buffer from detected cliffs)
- [ ] Human-readable summary report output
- [ ] Integration with `run_sweeps.py`: add `--ratchet` flag that runs the full ratchet pipeline

### Phase 4: Integration and CLI

- [ ] Add `--parametric` flag to `run_sweeps.py` that uses generated scenarios instead of (or alongside) hand-crafted ones
- [ ] Add `--ratchet` flag to `run_sweeps.py` that runs the full ratchet pipeline after sweeping
- [ ] Ensure backward compatibility: existing `--tier 1|2|3|4|all` flags continue to use hand-crafted scenarios by default
- [ ] Persist ratchet results alongside sweep results in `results/` directory

### Phase 5: Validation and Tuning

- [ ] Run full sweep with 50 generated scenarios and verify at least 3 Tier 1-3 constants show nDCG variance > 0.05
- [ ] Verify train/validation split catches at least 1 spurious "optimal" value
- [ ] Verify full sweep (all tiers + generated scenarios + ratchet) completes in under 60 seconds
- [ ] If sensitivity target not met, tune seed parameters (increase record counts, add more extreme distributions)

## No-Gos

- **Do not modify `tests/benchmarks/metrics/`** -- the metrics system is the immutable evaluation harness
- **Do not modify `tests/benchmarks/overrides.py`** -- override injection is stable and sufficient
- **Do not modify the `Scenario` or `RecipeScenario` base classes** -- generated scenarios must conform to the existing interface
- **Do not auto-write to `constants.py`** -- the ratchet proposes changes, humans approve them
- **Do not introduce parallelism in Phase 1** -- sequential execution is simpler to debug and the 30s target is achievable without it
- **Do not replace hand-crafted scenarios** -- they remain the default; generated scenarios are opt-in via `--parametric`
- **Do not sweep InteractionWeight constants** -- these are team-preference values, not tunable by retrieval metrics

## Update System

No update system changes required. This feature is entirely within the test/benchmark harness and does not affect the deployed library, CLI tools, or any update scripts. No new dependencies are introduced.

## Agent Integration

No agent integration required. The benchmark harness is a developer-facing test tool, not an agent-accessible capability. No MCP server changes, no bridge changes, no `.mcp.json` modifications needed.

## Failure Path Test Strategy

Failure modes for the parametric generation system:

1. **Degenerate scenarios**: A generated scenario might produce 0 relevant records (e.g., all noise) or trivial rankings (1 record). The factory must validate seeds and skip degenerate configurations. Test: generate 100 seeds, assert all produce at least 3 relevant records.

2. **Non-determinism**: If a scenario produces different results on repeated runs with the same seed, the sweep results become unreliable. Test: run the same generated scenario 3 times, assert identical `ScenarioResult.retrieved_ids`.

3. **Redis key collisions**: Generated scenarios create many records. If prefixes collide, results are corrupted. Test: run 10 generated scenarios concurrently (sequentially with overlapping teardown) and verify no key cross-contamination.

4. **Ratchet false accepts**: The ratchet might accept a change that happens to pass validation by chance. Mitigation: the 0.01 improvement threshold and cliff safety margin reduce this risk. Test: create a synthetic scenario where the optimal value on train doesn't generalize, verify the ratchet rejects it.

5. **Timeout**: 50 scenarios with large record counts might exceed the 30s budget. Test: time the full factory run and assert < 30s. If it fails, reduce default record counts.

## Test Impact

- [ ] `tests/benchmarks/test_sweep.py::TestSweepRunner` -- UPDATE: add tests for generated scenario classes passed to SweepRunner (verify they work identically to hand-crafted ones)
- [ ] `tests/benchmarks/test_harness.py::TestFactualRecallScenario` -- no change needed, hand-crafted scenarios are untouched
- [ ] `tests/benchmarks/test_sweep.py::TestSweepIntegration::test_end_to_end_sweep` -- UPDATE: add a parametric variant that uses generated scenarios
- [ ] `tests/benchmarks/test_tier4.py` -- no change needed, Tier 4 recipe scenarios are independent

New test files to create:
- `tests/benchmarks/test_factory.py` -- unit tests for ScenarioFactory, ScenarioSeed, importance distributions, access patterns, determinism, degenerate detection
- `tests/benchmarks/test_split.py` -- unit tests for SplitRunner, train/validation partitioning, overfitting guard
- `tests/benchmarks/test_ratchet.py` -- unit tests for RatchetLoop, cliff safety margin, accept/reject logic, summary report format

## Rabbit Holes

- **Optimizing seed selection with meta-learning**: It's tempting to use optimization algorithms to find "the best" set of seeds that maximizes constant sensitivity. This is a meta-overfitting trap -- the point is diverse coverage, not optimized coverage. Stick with deterministic combinatorial seed generation.

- **Adding semantic/embedding-based scenarios to the factory**: The factory generates field-level scenarios for Tiers 1-3. Recipe-layer scenarios (Tier 4) already have good coverage via the existing `RecipeScenario` fixtures. Don't try to parametrically generate recipe-layer scenarios -- the multi-turn LLM fixture format is too complex for parametric generation.

- **Parallelizing scenario execution**: Tempting for performance, but Redis operations within scenarios are not thread-safe (shared key prefixes, scan operations). Sequential execution is correct and fast enough. Only consider parallelism if the 60s full-sweep target is missed after tuning record counts.

## Documentation

- [ ] Create `docs/features/parametric-sweep.md` describing the ScenarioFactory, train/validation split, and ratchet loop
- [ ] Update `tests/benchmarks/README.md` (if it exists) or add inline module docstrings explaining `--parametric` and `--ratchet` CLI flags
- [ ] Add entry to `docs/plans/` index if one exists
