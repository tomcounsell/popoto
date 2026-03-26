---
status: Ready
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-03-26
tracking: https://github.com/tomcounsell/popoto/issues/296
last_comment_id:
---

# Scenario Code Path Coverage for Constant Sensitivity

## Problem

The parametric sweep system (PR #295) generates diverse data but every scenario uses the same behavioral pathway: create records, optionally call `on_context_used("acted")` once, then run `composite_score(indexes={"relevance": 0.6, "certainty": 0.4})`. This means 28 of 30 constants show zero nDCG sensitivity because the scenarios never exercise the code paths those constants control.

**Current behavior:** Sweep results show flat nDCG@5 of 0.7702 for 28/30 constants. Only `ACTED_CYCLE_STRENGTHEN_FACTOR` (range 0.2269) and `default_importance` (range 1.0) show any variance. The ratchet loop produces all "no_sensitivity" decisions.

**Desired outcome:** At least 5 Tier 1-3 constants show nDCG variance > 0.05. The ratchet loop produces at least 2 accept/reject decisions. Each constant family has a dedicated scenario variant that exercises its specific code path deeply enough that changing the constant produces measurably different retrieval rankings.

## Prior Art

- **PR #295 / Issue #293**: Shipped the `ScenarioFactory`, `ScenarioSeed`, train/validation splits, and `RatchetLoop`. The factory interface and `Scenario` base class are the extension points for this work.
- **PR #292 / Issue #279**: Expanded sweep coverage for tiers 1/3/4. Confirmed that the hand-crafted scenarios (`factual_recall`, `temporal_scheduling`, `multi_step_reasoning`) don't stress Tier 1-3 constants.
- **PR #250 / Issue #234**: Built the original sweep infrastructure (`SweepRunner`, `apply_overrides`, metrics).

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| PR #295 | Generated diverse **data** (varied record counts, importance distributions, access patterns, co-occurrence links) | Scenarios all follow the same **behavioral pathway**: one `on_context_used` call, no time passage for decay, ground truth defined post-filter, co-occurrence links created but never queried through propagation. Data diversity alone cannot expose constants that gate different code paths. |

**Root cause pattern:** The factory varies data parameters but hardcodes the behavioral sequence. Constants control code paths (decay computation, multi-round confidence updates, write filter gating, graph propagation), so sensitivity requires scenarios that exercise those specific paths with enough depth to produce ranking differences.

## Data Flow

The sweep system flows as follows:

1. **Entry point**: `SweepRunner.run_single_sweep(constant_name, values)` iterates over parameter values
2. **Override injection**: `apply_overrides(overrides)` patches `Defaults` and module-level aliases
3. **Scenario execution**: `scenario.execute()` calls `setup()` then `run()` then `teardown()`
4. **Setup phase**: Creates model instances, applies behavioral effects (this is where code paths must diverge per family)
5. **Run phase**: Queries via `composite_score()` and builds ground truth
6. **Metrics**: `ResultsAggregator` computes nDCG@5, calibration error, etc.
7. **Output**: JSON results with per-constant sensitivity curves

The key intervention point is step 4: the setup phase must exercise family-specific code paths so that the constant under test actually influences the scores that `composite_score` reads.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (scope alignment on family prioritization)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites -- this work extends existing benchmark infrastructure with no external dependencies.

## Solution

### Key Elements

- **FamilyScenarioFactory**: A new factory class (or extension of the existing `ScenarioFactory`) that generates family-specific scenario variants. Each family variant inherits the data-generation logic from `ScenarioSeed` but overrides the behavioral sequence in `setup()` and the query/ground-truth logic in `run()`.
- **Family-aware seed**: A new `family` field on `ScenarioSeed` (or a separate `FamilySeed` dataclass) that selects which behavioral pathway to exercise. Values: `"decay"`, `"confidence"`, `"write_filter"`, `"co_occurrence"`.
- **Integration with ratchet**: The ratchet loop's `SplitRunner` uses the family-aware factory to generate scenarios that target the constants being swept, so each constant is evaluated against scenarios that actually exercise its code path.

### Flow

**SweepRunner** -> selects constant -> **FamilyScenarioFactory** generates scenarios for that constant's family -> **setup()** exercises family-specific code path -> **run()** queries with family-appropriate method -> **metrics** measure nDCG variance -> **ratchet** makes accept/reject decision

### Technical Approach

#### Family 1: Decay (`DECAY_RATE`)

**Problem**: Records are queried immediately after creation. The Lua decay formula `base_score * elapsed_days^(-decay_rate)` produces nearly identical scores for all records when elapsed time is < 0.01 days, because `0.01^(-0.1)` and `0.01^(-0.9)` differ by only ~5x vs the ~50x range of base scores.

**Solution**: Manipulate the sorted set timestamps directly so that records have real age spread that the Lua decay script reads at query time. The current factory already does `ZADD` with old timestamps for stale records, but the age spread is relative to `time.time()` and `composite_score` calls the Lua script with `now` as the current time -- so the decay computation does see real age. The issue is that the Lua script reads `base_score` from the model hash, and importance differences dominate.

**Specific changes**:
- Create scenarios with **clustered** importance (many records at similar base scores, e.g., 0.45-0.55) so that decay is the tiebreaker
- Use large age spreads (90-365 days) with **bimodal** staleness: half accessed today, half accessed months ago
- Ground truth: rank records by `importance * elapsed_days^(-decay_rate)` using the current `decay_rate` override value, so the "correct" ranking actually depends on the constant

**Constants exercised**: `DECAY_RATE`
**Expected sensitivity**: At `decay_rate=0.1`, old records barely lose score. At `decay_rate=1.0`, old records are heavily penalized. With clustered importance, the ranking order should flip between these extremes.

#### Family 2: Confidence Lifecycle (`ACTED_CONFIDENCE_SIGNAL`, `CONTRADICTED_CONFIDENCE_SIGNAL`, `ACTED_CYCLE_STRENGTHEN_FACTOR`, `DISMISSED_CYCLE_WEAKEN_FACTOR`, `CONTRADICTED_CYCLE_WEAKEN_FACTOR`, `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD`, `INITIAL_CONFIDENCE`)

**Problem**: The current factory calls `on_context_used()` once with only `"acted"` outcomes. The Bayesian confidence update formula is `confidence + (signal - confidence) / sqrt(evidence_count + 1)`. With one update and `signal=0.9`, the confidence goes from 0.5 to 0.9 for acted records and stays at 0.5 for others. The certainty axis only contributes 40% of the composite score, and the first update has the same magnitude regardless of the signal value (because `evidence_count=0` makes `sqrt(1)=1`, so the update is `signal - 0.5`).

**Solution**: Run **multiple observation cycles** with **mixed outcomes** per record. Some records get repeated "acted" (corroborated), some get "contradicted" (suppressed), some get mixed sequences. After N cycles, the confidence values diverge significantly, and the signal constants control how fast they diverge.

**Specific changes**:
- Run 3-5 rounds of `on_context_used()` per scenario
- Each round assigns outcomes based on record "ground truth quality": high-importance records get "acted", low-importance get "contradicted", mid-range get mixed "acted"/"dismissed"/"contradicted" sequences
- Use higher certainty weight in composite_score: `{"relevance": 0.3, "certainty": 0.7}` for confidence-family scenarios so confidence differences dominate
- Ground truth: records that received mostly "acted" outcomes should rank highest

**Constants exercised**: `ACTED_CONFIDENCE_SIGNAL`, `CONTRADICTED_CONFIDENCE_SIGNAL`, `INITIAL_CONFIDENCE`, `ACTED_CYCLE_STRENGTHEN_FACTOR`, `DISMISSED_CYCLE_WEAKEN_FACTOR`, `CONTRADICTED_CYCLE_WEAKEN_FACTOR`, `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD`
**Expected sensitivity**: High signal values (0.9) push confidence toward 1.0 faster than low values (0.5). After 5 rounds, the gap between corroborated and contradicted records is much wider with extreme signals than with moderate ones. This changes the ranking.

#### Family 3: Write Filter (`WF_MIN_THRESHOLD`, `WF_PRIORITY_THRESHOLD`)

**Problem**: The current factory defines ground truth as "top 30% by importance of surviving records." When `_wf_min_threshold` increases, low-importance records are filtered out, but since those are the least relevant anyway, the ground truth set barely changes and nDCG stays flat. The metric does not capture the filter's effect.

**Solution**: Define ground truth **before** filtering against the **full intended record set**. Create all records with a pre-filter ground truth (the intended top-K based on importance of ALL planned records, including those that would be filtered). Then apply the write filter. Measure whether the query retrieves the correct records from the full intended set, not just from survivors.

**Specific changes**:
- Generate the full set of `record_count` records with known importance values
- Compute ground truth relevance set from the full set (top 30% by importance)
- Create instances via `save()` -- some will be filtered by `WriteFilterMixin`
- Query the surviving records and measure nDCG against the pre-filter ground truth
- When `_wf_min_threshold` is high, many relevant records are filtered out, dropping nDCG
- When `_wf_min_threshold` is low, noise records survive and push relevant records lower

**Constants exercised**: `WF_MIN_THRESHOLD`, `WF_PRIORITY_THRESHOLD`
**Expected sensitivity**: At threshold 0.05, nearly all records survive (including noise), so retrieval must separate signal from noise. At threshold 0.5, many mid-importance relevant records are filtered out, so nDCG drops because the ground truth expects them. The optimal threshold balances these effects and depends on the importance distribution.

#### Family 4: Co-occurrence (`CO_OCCURRENCE_INITIAL_WEIGHT`, `CO_OCCURRENCE_DECAY_PER_HOP`, `CO_OCCURRENCE_DECAY_FACTOR`)

**Problem**: The current factory creates co-occurrence links as raw Redis keys (`POPOTO_REDIS_DB.set(link_key, "1")`), not through the `CoOccurrenceField` API. The links are never queried through `propagate()` or injected into `composite_score()` via the `co_occurrence_boost` parameter.

**Solution**: Use `CoOccurrenceField.link()` and `CoOccurrenceField.propagate()` to create real graph edges and query them. Inject propagation scores into `composite_score()` via `co_occurrence_boost`.

**Specific changes**:
- Define a model class with a `CoOccurrenceField` named `associations`
- Create link networks: seed records (known relevant) are linked to target records
- Call `associations_field.link(model_class, seed_pk, target_pk, initial_weight=...)` using the override value
- Call `associations_field.propagate(model_class, seed_pks, depth=2, decay_per_hop=...)` to get propagation scores
- Pass propagation scores as `co_occurrence_boost` to `composite_score()`
- Ground truth: records reachable from seeds via links should rank higher

**Constants exercised**: `CO_OCCURRENCE_INITIAL_WEIGHT`, `CO_OCCURRENCE_DECAY_PER_HOP`, `CO_OCCURRENCE_DECAY_FACTOR`
**Expected sensitivity**: At `decay_per_hop=0.9`, propagation reaches many hops with high weight, boosting distant records. At `decay_per_hop=0.1`, only direct neighbors get meaningful boost. This changes which records appear in the top-K.

### Constant-to-Family Mapping

| Family | Constants | Scenario Variant |
|--------|-----------|-----------------|
| Decay | `DECAY_RATE` | Clustered importance, bimodal age spread, decay-aware ground truth |
| Confidence | `ACTED_CONFIDENCE_SIGNAL`, `CONTRADICTED_CONFIDENCE_SIGNAL`, `INITIAL_CONFIDENCE`, `ACTED_CYCLE_STRENGTHEN_FACTOR`, `DISMISSED_CYCLE_WEAKEN_FACTOR`, `CONTRADICTED_CYCLE_WEAKEN_FACTOR`, `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` | Multi-round mixed outcomes, high certainty weight |
| Write Filter | `WF_MIN_THRESHOLD`, `WF_PRIORITY_THRESHOLD` | Pre-filter ground truth, full-set relevance |
| Co-occurrence | `CO_OCCURRENCE_INITIAL_WEIGHT`, `CO_OCCURRENCE_DECAY_PER_HOP`, `CO_OCCURRENCE_DECAY_FACTOR` | Real CoOccurrenceField edges, propagate + co_occurrence_boost |

### Out-of-scope Families (deferred)

| Family | Constants | Why Deferred |
|--------|-----------|-------------|
| PolicyCache | `MIN_EVENTS_FOR_CRYSTALLIZATION`, `TD_ALPHA`, `TD_GAMMA`, `WILSON_CI_THRESHOLD`, `CHI_SQUARED_P_THRESHOLD`, `INITIAL_CYCLE_AMPLITUDE` | Requires recipe-level scenarios with crystallization cycles. Large scope. |
| ContextAssembler | `COMPETITIVE_SUPPRESSION_SIGNAL`, `DEFAULT_SURFACING_THRESHOLD` | Requires full ContextAssembler integration. Separate issue. |
| PredictionLedger | `PL_*` constants | Requires prediction/resolution cycles. Separate issue. |

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The existing factory has `except Exception: pass` blocks around `save()`, `ZADD`, and `on_context_used()`. New family scenarios must NOT swallow exceptions silently -- if a code path fails, the scenario should return `status="error"` so the sweep detects it.
- [ ] Each family scenario must handle the case where zero instances survive setup (return `ScenarioResult(status="skipped-degenerate")`).

### Empty/Invalid Input Handling
- [ ] `CoOccurrenceField.propagate()` with empty `seed_pks` returns `{}` -- scenario must handle this.
- [ ] `composite_score()` with `co_occurrence_boost={}` is a no-op -- scenario must ensure boost dict is non-empty.

### Error State Rendering
- Not applicable -- benchmark scenarios produce JSON results, not user-visible output.

## Test Impact

No existing tests affected -- this work adds new scenario generation code in the benchmarks directory. The existing `ScenarioFactory` and its `GeneratedScenario` class remain unchanged. New family-specific scenarios are additive.

## Rabbit Holes

- **Simulating real time passage**: Do not use `time.sleep()` to create age spread. Manipulate sorted set scores directly (the factory already does this). The Lua decay script reads timestamps from the sorted set, not from a clock.
- **Recipe-layer scenario families**: PolicyCache, ContextAssembler, and PredictionLedger require recipe-level integration far beyond the field layer. Defer to a separate issue.
- **Optimizing sweep speed**: The 120-second budget is generous. Do not prematurely optimize scenario count or record count. Focus on code path coverage first.
- **Modifying the metrics system**: The nDCG@5 metric is correct. The problem is that scenarios produce identical rankings regardless of constant value. Do not change how nDCG is computed.

## Risks

### Risk 1: Decay sensitivity may still be low due to Lua script behavior
**Impact:** Decay family scenarios show less variance than expected because the Lua script's power-law formula has diminishing returns at extreme rates.
**Mitigation:** Use clustered importance (0.45-0.55 range) so even small decay differences change rankings. Verify with a unit-level calculation before building the full scenario.

### Risk 2: Co-occurrence scenarios may be slow due to propagate() Lua evaluation
**Impact:** BFS propagation on large graphs could slow individual scenario execution, pushing total sweep time past 120 seconds.
**Mitigation:** Limit co-occurrence scenarios to 20-30 records with sparse link density (0.1-0.3). Propagation depth capped at 2 hops.

## Race Conditions

No race conditions identified -- all benchmark operations are synchronous and single-threaded. Each scenario runs in isolation with unique key prefixes.

## No-Gos (Out of Scope)

- PolicyCache, ContextAssembler, and PredictionLedger scenario families (separate issue)
- Modifying the metrics system (`tests/benchmarks/metrics/`)
- Modifying `apply_overrides` in `overrides.py`
- Changing the `Scenario` or `ScenarioFactory` public interfaces
- Modifying hand-crafted scenarios (`factual_recall`, `temporal_scheduling`, `multi_step_reasoning`)

## Update System

No update system changes required -- this is benchmark infrastructure only.

## Agent Integration

No agent integration required -- benchmark infrastructure is developer-facing tooling.

## Documentation

### Inline Documentation
- [ ] Docstrings on `FamilyScenarioFactory` and each family scenario class
- [ ] Comments explaining why each family scenario exercises specific constants

No external documentation changes needed.

## Success Criteria

- [ ] At least 4 constant families have dedicated scenario variants (decay, confidence, write filter, co-occurrence)
- [ ] Each family scenario exercises the actual code path for its constants (not just data setup)
- [ ] At least 5 Tier 1-3 constants show nDCG variance > 0.05 across generated scenarios
- [ ] The ratchet loop produces at least 2 accept/reject decisions (not all "no_sensitivity")
- [ ] Existing hand-crafted scenarios and factory interface unchanged
- [ ] Full sweep completes in under 120 seconds
- [ ] Tests pass (`pytest tests/ -x -q`)

## Team Orchestration

### Team Members

- **Builder (family-scenarios)**
  - Name: scenario-builder
  - Role: Implement family-specific scenario variants
  - Agent Type: builder
  - Resume: true

- **Validator (sweep-validation)**
  - Name: sweep-validator
  - Role: Run sweeps and verify sensitivity metrics
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create Family Scenario Infrastructure
- **Task ID**: build-family-infra
- **Depends On**: none
- **Validates**: `tests/benchmarks/scenarios/` imports work, factory generates family scenarios
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/scenarios/family_factory.py` with `FamilyScenarioFactory` class
- Add `family` parameter to seed generation (or create `FamilySeed` subclass)
- Implement constant-to-family mapping so the sweep runner can generate appropriate scenarios per constant
- Wire into `run_sweeps.py` parametric mode so `--parametric` uses family-aware scenarios

### 2. Implement Decay Family Scenarios
- **Task ID**: build-decay-family
- **Depends On**: build-family-infra
- **Validates**: `DECAY_RATE` shows nDCG variance > 0.05
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Create decay-family scenario: clustered importance (0.45-0.55), bimodal age spread (half today, half 90-365 days old)
- Ground truth ranks records by `importance * elapsed_days^(-current_decay_rate)`
- Query with `composite_score(indexes={"relevance": 1.0})` (relevance only, no certainty noise)
- Verify that sweeping `decay_rate` from 0.1 to 1.0 produces different top-5 rankings

### 3. Implement Confidence Family Scenarios
- **Task ID**: build-confidence-family
- **Depends On**: build-family-infra
- **Validates**: `ACTED_CONFIDENCE_SIGNAL`, `CONTRADICTED_CONFIDENCE_SIGNAL`, `INITIAL_CONFIDENCE` show nDCG variance > 0.05
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Create confidence-family scenario: 3-5 rounds of `on_context_used()` with mixed outcomes
- High-importance records get "acted", low-importance get "contradicted", mid get mixed
- Query with `composite_score(indexes={"relevance": 0.3, "certainty": 0.7})` (certainty-dominated)
- Ground truth: records with mostly "acted" outcomes should rank highest

### 4. Implement Write Filter Family Scenarios
- **Task ID**: build-wf-family
- **Depends On**: build-family-infra
- **Validates**: `WF_MIN_THRESHOLD` shows nDCG variance > 0.05
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Create write-filter-family scenario: pre-filter ground truth from full record set
- Generate all record importance values, compute ground truth, then save (some filtered)
- Measure retrieval quality against the full intended set, not just survivors
- Verify that varying threshold changes nDCG

### 5. Implement Co-occurrence Family Scenarios
- **Task ID**: build-cooc-family
- **Depends On**: build-family-infra
- **Validates**: `CO_OCCURRENCE_DECAY_PER_HOP` or `CO_OCCURRENCE_INITIAL_WEIGHT` show nDCG variance > 0.05
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Create co-occurrence-family scenario with real `CoOccurrenceField` on the model
- Use `link()` to create edges between seed records and target records
- Use `propagate()` to get boost scores, pass as `co_occurrence_boost` to `composite_score()`
- Ground truth: linked records should rank higher than unlinked

### 6. Integration and Sweep Validation
- **Task ID**: validate-sweep
- **Depends On**: build-decay-family, build-confidence-family, build-wf-family, build-cooc-family
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full parametric sweep: `python -m tests.benchmarks.run_sweeps --parametric --tier all`
- Verify at least 5 constants show nDCG variance > 0.05
- Run ratchet loop: `python -m tests.benchmarks.run_sweeps --ratchet`
- Verify at least 2 accept/reject decisions
- Verify total sweep time < 120 seconds
- Run `pytest tests/ -x -q` to confirm no regressions

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-sweep
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| Sweep completes | `timeout 120 python -m tests.benchmarks.run_sweeps --parametric --tier 1` | exit code 0 |
| Existing scenarios unchanged | `git diff HEAD -- tests/benchmarks/scenarios/factual_recall.py tests/benchmarks/scenarios/multi_step_reasoning.py tests/benchmarks/scenarios/temporal_scheduling.py` | empty output |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. Should the family-aware factory replace the existing `ScenarioFactory.default_seeds()` output, or should it be a separate entry point (e.g., `FamilyScenarioFactory.create_all()`) that the parametric mode selects based on the constant being swept? Leaning toward separate entry point to preserve backward compatibility.

2. For the confidence family, should the certainty weight in `composite_score` be configurable per seed, or fixed at `{"relevance": 0.3, "certainty": 0.7}`? Fixed is simpler but less flexible.
