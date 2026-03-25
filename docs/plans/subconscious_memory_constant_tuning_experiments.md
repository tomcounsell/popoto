---
status: Planning
type: chore
appetite: Medium
owner: Valor Engels
created: 2026-03-25
tracking: https://github.com/tomcounsell/popoto/issues/273
---

# SubconsciousMemory: Implement Constant Tuning Experiments

## Problem

The experiment design doc (`docs/plans/subconscious_memory_constant_tuning.md`, issue #268) defines 8 experiments across 3 scenarios for tuning SubconsciousMemory's magic-number constants. The design is complete but no implementation exists. The existing benchmark harness (PR #250) covers field-level constants (Tiers 1-3) but cannot run recipe-layer experiments because:

1. The override system (`overrides.py`) only patches module-level constants and `Defaults` class attributes -- it cannot inject constructor arguments into `SubconsciousMemory` or `ContextAssembler`.
2. The `Scenario` base class operates at the field/query level (save records, run `composite_score`, compare retrieved IDs). SubconsciousMemory experiments need multi-turn simulation (inject_context -> LLM response -> extract_memories -> report_outcomes).
3. No ground-truth fixture data exists for extraction quality (M1) or token efficiency (M3) metrics.
4. The metrics module only has retrieval metrics (precision@K, nDCG@K, calibration error, MRR). New metrics are needed: extraction F1, token utilization ratio, importance distribution health.

**Current behavior:** Running `python -m tests.benchmarks.run_sweeps` executes Tiers 1-3 for field-level constants only. SubconsciousMemory-layer constants have no experimental coverage.

**Desired outcome:** A Tier 4 sweep section in the harness that runs all 8 experiments from the design doc, produces comparable output in `results/summary.json`, and identifies optimal defaults with data.

## Prior Art

- **PR #250**: "Experimental tuning benchmark harness for agent-memory constants" -- shipped the SweepRunner/ResultsAggregator/Scenario framework. This is the foundation we extend.
- **PR #253**: "Consolidate constants, graduate docs, prepare for experiments" -- centralized constants into `fields/constants.py` with the `Defaults` class. Made override injection possible.
- **PR #272 / Issue #268**: The experiment design doc itself. Defines metrics M1-M6, experiments 1-8, and 3 scenarios. Marked complete as a design deliverable.
- **Issue #234**: Original issue for the field-level benchmark harness. Closed when PR #250 merged.

## Spike Results

### spike-1: Can SubconsciousMemory be instantiated without Redis for extraction tests?
- **Assumption**: "extract_memories needs a live Redis connection for every call"
- **Method**: code-read
- **Finding**: `extract_memories()` calls `instance.save()` which requires Redis. However, extraction quality (M1) can be measured by calling `_split_sentences()` directly (it is a static method) and comparing against ground truth without saving. For full pipeline tests, Redis is required.
- **Confidence**: high
- **Impact on plan**: M1 extraction experiments can be split into a fast path (static method, no Redis) and full path (with save). The fast path can run without Redis infrastructure.

### spike-2: Does the override system need architectural changes for constructor args?
- **Assumption**: "Constructor arguments require a fundamentally different override mechanism"
- **Method**: code-read
- **Finding**: The existing `apply_overrides()` context manager patches module-level constants. SubconsciousMemory constructor args (`max_items`, `max_tokens`, `extraction_min_length`) are passed at instantiation time, not read from module globals at runtime. The scenarios already receive `overrides` in their constructor and use them to build model classes (see `factual_recall.py:_build_model_class`). The same pattern works: recipe-layer scenarios read constructor args from `self.overrides` when instantiating `SubconsciousMemory`. No changes to `overrides.py` needed -- just scenario-level constructor arg forwarding.
- **Confidence**: high
- **Impact on plan**: Eliminates the "extend override system" task from the design doc. Scenarios handle it directly.

### spike-3: Can score_weights sweep be automated without score_weights having a module-level default?
- **Assumption**: "score_weights has no documented default, making sweeping difficult"
- **Method**: code-read
- **Finding**: `score_weights` is a required argument to `SubconsciousMemory.__init__`. There is no default. The sweep defines candidate configurations directly in the sweep definition (Experiment 5 in the design doc lists 6 configurations). The scenario passes each configuration as a constructor arg. This is straightforward.
- **Confidence**: high
- **Impact on plan**: score_weights sweep is a standard parametric sweep, just using dict values instead of floats.

## Data Flow

The experiment pipeline extends the existing sweep architecture:

1. **Entry point**: `run_sweeps.py --tier 4` triggers Tier 4 sweep definitions
2. **SweepRunner**: Iterates over constant values, instantiates recipe-layer scenarios with overrides
3. **Recipe-layer Scenario**: Creates a `SubconsciousMemory` instance with override-derived constructor args, runs multi-turn simulation using fixture data
4. **SubconsciousMemory pipeline**: `inject_context()` -> simulated LLM response (from fixture) -> `extract_memories()` -> `report_outcomes()`
5. **Metrics**: Scenario collects retrieval IDs, extraction counts, token usage, importance distributions and returns them in `ScenarioResult.metadata`
6. **ResultsAggregator**: Computes optimal values, sensitivity curves, cliff effects -- same as Tiers 1-3
7. **Output**: Results appended to `results/summary.json`

## Critical Review of the Design Doc

The design doc is well-structured but has three feasibility issues:

### Issue 1: M4 (Importance Distribution Health) measures caller behavior, not a tunable constant

The `default importance` argument (constant 4) is a fixed value passed to `extract_memories()`. All memories extracted in a single call get the same importance. The design doc proposes measuring std deviation of importance scores after 20 turns -- but this only varies if the *caller* varies the importance argument per turn. The constant itself (0.5) just sets the default when the caller does not specify.

**Resolution**: Reframe Experiment 4 to test two things: (a) the sensitivity of retrieval quality to the fixed default value (does 0.5 produce better nDCG than 0.3 or 0.7?), and (b) whether a "varied" importance strategy (different values per turn) produces better ranking than a fixed default. This keeps the experiment useful without pretending the constant controls distribution shape.

### Issue 2: M5 (Write Filter Pass Rate) requires labeled "genuinely low-value" judgments

The design doc proposes human-labeling blocked memories as "genuinely low-value" to compute filter precision. For an automated benchmark, we replace this with a proxy: memories extracted from designated "noise" sentences in the fixture (sentences pre-labeled as noise) that get blocked are true positives. Memories extracted from "meaningful" sentences that get blocked are false positives.

**Resolution**: Use fixture labels as proxy for human judgment. The fixture already needs meaningful/noise labels for M1, so this piggybacks on the same data.

### Issue 3: Experiment 8 interaction matrix is large but manageable

4 pairs x 9 combos x 3 scenarios = 108 sweep points. Each point runs a 20-turn simulation. At ~100ms per turn (Redis operations, no LLM), this is ~200 seconds total. Acceptable for a benchmark suite.

**Resolution**: No change needed. Include a `--skip-interactions` flag for fast runs.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope confirmation after fixture design)
- Review rounds: 1 (code review of new scenarios and metrics)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | All scenarios need Redis for model operations |
| Dev dependencies | `python -c "import pytest; import popoto"` | Test framework and library |

## Solution

### Key Elements

- **Fixture data**: JSON files in `tests/benchmarks/fixtures/` with pre-labeled conversation corpora for each scenario (support agent, coding assistant, research agent)
- **Recipe-layer scenario base class**: Extends `Scenario` with multi-turn simulation helpers and SubconsciousMemory instantiation
- **New metrics**: Extraction F1, token utilization ratio, importance distribution std dev -- added to `tests/benchmarks/metrics/`
- **Tier 4 sweep definitions**: Added to `run_sweeps.py` covering all 8 experiments
- **3 recipe-layer scenarios**: One per design doc scenario, implementing the multi-turn simulation

### Technical Approach

- Recipe-layer scenarios instantiate `SubconsciousMemory` with constructor args read from `self.overrides`, bypassing the need to modify `overrides.py`
- Fixture data provides deterministic "LLM responses" for extraction, avoiding real LLM calls
- Each fixture includes sentence-level labels (`meaningful`/`noise`) and relevance tags per query
- The `ScenarioResult.metadata` dict carries recipe-specific metrics (extraction counts, token usage, distribution stats) alongside standard retrieval metrics
- `score_weights` sweeps use dict-valued sweep points instead of float-valued ones; the `SweepRunner` already supports `Any` values

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `SubconsciousMemory.extract_memories()` with empty string -- must return empty list (existing behavior, add test)
- [ ] `SubconsciousMemory.inject_context()` with zero stored memories -- must return unmodified messages (existing behavior, add test)
- [ ] Scenario teardown must clean up all Redis keys even if the run step raises (base class handles this via try/finally)

### Empty/Invalid Input Handling
- [ ] Fixture files must be validated on load (schema check for required fields)
- [ ] Sweep with `max_items=0` should produce a `skipped-degenerate` result, not crash
- [ ] Sweep with `extraction_min_length=0` should run (valid edge case, extracts all sentences)

### Error State Rendering
- [ ] Sweep summary must report degenerate/error points distinctly from OK points (existing behavior in `ResultsAggregator`)

## Test Impact

No existing tests affected -- this is purely additive:
- New scenario classes in `tests/benchmarks/scenarios/` (no modifications to existing scenarios)
- New fixture files in `tests/benchmarks/fixtures/` (new directory)
- New metrics in `tests/benchmarks/metrics/` (additive, no changes to `retrieval.py`)
- New Tier 4 section in `run_sweeps.py` (additive, no changes to Tiers 1-3)
- New test file `tests/benchmarks/test_tier4.py` for validating new scenarios run correctly

## Rabbit Holes

- **Building a fancy reporting UI**: The existing JSON output and console summary are sufficient. Matplotlib charts or HTML reports add complexity without improving the tuning decisions. If needed later, it is a separate issue.
- **Using real LLM calls for extraction quality**: Deterministic fixture data is the right choice for reproducible benchmarks. Real LLM calls introduce non-determinism, latency, and cost.
- **Optimizing score_weights with scipy.optimize**: The weight space is 2-3 dimensions with 6 candidate configurations. Grid search is exhaustive at this scale. Optimization frameworks add a dependency for no benefit.
- **Per-scenario optimal defaults**: If constants differ wildly between scenarios, document the sensitivity. Do not overfit defaults to one scenario.

## Risks

### Risk 1: Fixture data quality determines experiment validity
**Impact:** If fixture conversations do not realistically represent the scenarios (support agent, coding assistant, research agent), the optimal constants will be tuned to artificial data.
**Mitigation:** Each fixture includes 20-30 turns of realistic dialogue with diverse sentence lengths and information density. Review fixtures as part of PR review.

### Risk 2: Token counting heuristic varies with serialization
**Impact:** M3 (token budget efficiency) depends on `len(str(r)) // 4` which is a rough approximation. Results may not transfer to deployments with custom tokenizers.
**Mitigation:** Document the heuristic. Use consistent serialization across all experiments. Note that absolute token counts are approximate but relative comparisons between sweep points are valid.

### Risk 3: Redis state leakage between sweep points
**Impact:** If one sweep point's data leaks into the next, metrics are contaminated.
**Mitigation:** Each scenario uses a UUID-prefixed key namespace (existing pattern in `Scenario.__init__`) and `teardown()` cleans up via SCAN. Recipe-layer scenarios follow the same pattern.

## Race Conditions

No race conditions -- all experiments are sequential, single-process benchmarks using isolated Redis key prefixes.

## No-Gos (Out of Scope)

- Changing any default constant values -- that happens after experiments produce data, in a separate PR
- LLM-based extraction quality scoring (Tier 3 in the design doc, marked as optional)
- Visualization or dashboard tooling for results
- Modifying existing Tier 1-3 sweep infrastructure or existing scenario classes
- Running the experiments as part of CI (benchmarks are manual dev-time tools)

## Update System

No update system changes required -- benchmark harness is development-only tooling.

## Agent Integration

No agent integration required -- benchmarks are development-only tools that do not need MCP exposure.

## Documentation

### Inline Documentation
- [ ] Docstrings on all new scenario classes and metric functions
- [ ] Comments explaining fixture data format and labeling conventions

### Feature Documentation
- [ ] After experiments run and defaults are adjusted (separate follow-up), update `docs/guides/subconscious-memory-tuning.md` with findings
- [ ] Add suggested default `score_weights` to `SubconsciousMemory` docstring (after experiments conclude)

## Success Criteria

- [ ] `tests/benchmarks/fixtures/` contains 3 scenario fixture files (support_agent.json, coding_assistant.json, research_agent.json)
- [ ] `tests/benchmarks/scenarios/` contains a recipe-layer base class and 3 scenario implementations
- [ ] `tests/benchmarks/metrics/` contains extraction F1 and token utilization metrics
- [ ] `run_sweeps.py --tier 4` runs all 8 experiments from the design doc to completion
- [ ] `results/summary.json` includes Tier 4 results with optimal values per constant
- [ ] Interaction effect sweeps run via `--tier 4 --interactions`
- [ ] Tests pass (`pytest tests/benchmarks/test_tier4.py`)
- [ ] All sweep points produce `ok` or `skipped-degenerate` status (no `error` status)

## Team Orchestration

### Team Members

- **Builder (fixtures)**
  - Name: fixture-builder
  - Role: Create ground-truth fixture data for 3 scenarios
  - Agent Type: builder
  - Resume: true

- **Builder (harness)**
  - Name: harness-builder
  - Role: Implement recipe-layer scenario base, 3 scenarios, new metrics, Tier 4 sweep defs
  - Agent Type: builder
  - Resume: true

- **Validator (sweep)**
  - Name: sweep-validator
  - Role: Run Tier 4 sweeps end-to-end and verify results
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create fixture data for 3 scenarios
- **Task ID**: build-fixtures
- **Depends On**: none
- **Validates**: fixture files parse correctly, contain required fields
- **Informed By**: spike-1 (extract_memories uses _split_sentences statically)
- **Assigned To**: fixture-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/fixtures/` directory
- Create `support_agent.json`: 25-turn support conversation with sentence-level labels (meaningful/noise) and per-query relevance tags. 15 essential, 10 nice-to-have, 25+ noise sentences.
- Create `coding_assistant.json`: 30-turn design discussion with decisions, rejections, and cross-references. 20 essential, 5 superseded, 15 contextual sentences.
- Create `research_agent.json`: 5 source documents with corroborated (12), contradicted (5), unique-valid (8), and noise sentences.
- Each fixture follows schema: `{"turns": [{"role": "...", "content": "..."}], "ground_truth": {"sentences": [{"text": "...", "label": "meaningful|noise", "relevance_per_query": {"q1": 0.9}}]}}`

### 2. Implement new metrics
- **Task ID**: build-metrics
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_tier4.py::test_extraction_f1`, `test_token_utilization`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `tests/benchmarks/metrics/extraction.py` with `extraction_f1(extracted, ground_truth_labels)` function
- Add `tests/benchmarks/metrics/token_efficiency.py` with `token_utilization_ratio(records, relevance_scores, token_counter)` function
- Add `importance_distribution_health(scores)` returning std dev and distinct rank count

### 3. Implement recipe-layer scenario base class
- **Task ID**: build-scenario-base
- **Depends On**: build-fixtures, build-metrics
- **Validates**: `tests/benchmarks/test_tier4.py::test_recipe_scenario_lifecycle`
- **Informed By**: spike-2 (no override system changes needed)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/scenarios/recipe_base.py` extending `Scenario`
- Add multi-turn simulation helper: loop over fixture turns, call `inject_context()`, use fixture "response" as simulated LLM output, call `extract_memories()`, call `report_outcomes()`
- SubconsciousMemory instantiation reads `max_items`, `max_tokens`, `extraction_min_length`, `score_weights` from `self.overrides` with sensible defaults
- Build a Memory model class dynamically (same pattern as `factual_recall.py:_build_model_class`) with all required fields

### 4. Implement 3 recipe-layer scenarios
- **Task ID**: build-scenarios
- **Depends On**: build-scenario-base
- **Validates**: `tests/benchmarks/test_tier4.py::test_support_agent_scenario`, `test_coding_assistant_scenario`, `test_research_agent_scenario`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/scenarios/support_agent.py` loading `support_agent.json` fixture
- Create `tests/benchmarks/scenarios/coding_assistant.py` loading `coding_assistant.json` fixture
- Create `tests/benchmarks/scenarios/research_agent.py` loading `research_agent.json` fixture
- Each scenario defines ground truth (relevant IDs, relevance scores) from fixture labels
- Each scenario returns `ScenarioResult` with standard retrieval metrics plus recipe-specific metrics in metadata

### 5. Add Tier 4 sweep definitions to run_sweeps.py
- **Task ID**: build-sweeps
- **Depends On**: build-scenarios
- **Validates**: `tests/benchmarks/test_tier4.py::test_tier4_sweep_definitions`
- **Informed By**: spike-3 (score_weights sweep uses dict values)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `TIER4_SWEEPS` dict to `run_sweeps.py` with all single-constant sweeps from experiments 1-7
- Add `TIER4_INTERACTION_PAIRS` for experiment 8 pairwise sweeps
- Add `TIER4_SCENARIOS` list referencing the 3 new scenario classes
- Extend `main()` to accept `--tier 4` and `--tier all` to include Tier 4
- Handle dict-valued sweep points (score_weights) in the sweep runner -- if SweepRunner cannot handle dicts natively, add score_weights as a separate sweep method

### 6. Write test file for Tier 4
- **Task ID**: build-tests
- **Depends On**: build-sweeps
- **Validates**: `pytest tests/benchmarks/test_tier4.py -v`
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/benchmarks/test_tier4.py` with tests that each scenario runs without error
- Test that fixture files load and validate
- Test that new metrics produce correct values on known inputs
- Test that a small sweep (1 constant, 2 values, 1 scenario) completes with OK status

### 7. Validate end-to-end sweep
- **Task ID**: validate-sweep
- **Depends On**: build-tests
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --tier 4` and verify it completes
- Verify `results/summary.json` contains Tier 4 constant entries
- Verify no sweep points have `error` status
- Run `pytest tests/benchmarks/test_tier4.py -v` and verify all pass

### 8. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-sweep
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify no regressions in existing Tier 1-3 tests
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/benchmarks/test_tier4.py -v` | exit code 0 |
| Fixtures exist | `ls tests/benchmarks/fixtures/*.json \| wc -l` | output contains 3 |
| Tier 4 runs | `python -m tests.benchmarks.run_sweeps --tier 4 2>&1 \| tail -1` | output contains "Results saved" |
| No import errors | `python -c "from tests.benchmarks.scenarios.support_agent import SupportAgentScenario"` | exit code 0 |
| Full test suite | `pytest tests/ -x -q` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| CONCERN | [agent-type] | [The concern raised] | [How/whether it was addressed] |

---

## Open Questions

1. **Fixture data depth**: Should fixtures contain full realistic conversations (20-30 turns with natural language) or minimal synthetic data (short, artificial sentences designed to test edge cases)? Full realism is better for validity but takes longer to craft and review. Recommendation: start with minimal synthetic data (5-10 turns) for initial harness validation, then expand to full realism in a follow-up if the harness works.

2. **score_weights sweep format**: The design doc proposes 6 dict configurations for score_weights. The current `SweepRunner.run_single_sweep` expects a flat `{constant_name: value}` dict. Should we (a) add a special case in `SweepRunner` for dict-valued constants, or (b) create a dedicated `run_score_weights_sweep` method? Option (a) is simpler if the runner already handles `Any` types.

3. **Result storage for recipe-specific metrics**: Standard `SweepPoint` stores precision@K and nDCG@K. Recipe-layer experiments also produce extraction F1, token utilization, and importance distribution stats. Should these go in (a) additional fields on `SweepPoint`, or (b) the existing `metadata` pattern? Option (b) avoids changing the dataclass but makes aggregation harder.
