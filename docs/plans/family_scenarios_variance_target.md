---
status: Ready
type: chore
appetite: Medium
owner: valorengels
created: 2026-04-20
tracking: https://github.com/tomcounsell/popoto/issues/362
last_comment_id: None
---

# Family Scenarios — Close the 5-Constant Variance Target

## Problem

Issue #351's Success Criterion requires a `--parametric` sweep to produce
nDCG@5 variance > 0.05 for **at least 5 constants**. PR #361 shipped the
override-reach fix, ground-truth decoupling, and evidence-driven defaults,
but the reference sweep
(`tests/benchmarks/results/sweep_20260417_141047.json`) only reached 4/5:

| Constant | Variance | Status |
|----------|----------|--------|
| `decay_rate` | 0.067 | sensitive |
| `_wf_min_threshold` | 0.068 | sensitive |
| `CO_OCCURRENCE_DECAY_PER_HOP` | 0.112 | sensitive |
| `CO_OCCURRENCE_INITIAL_WEIGHT` | 0.144 | sensitive |
| `ACTED_CONFIDENCE_SIGNAL` | 0.020 | borderline |
| `CONTRADICTED_CONFIDENCE_SIGNAL` | 0.021 | borderline |

**Current behavior:** `FamilyScenarioFactory` ships four scenarios covering
four code-path families (decay, confidence, write_filter, co_occurrence).
Twenty-two constants in `src/popoto/fields/constants.py` are annotated
`# empirically inert (sweep 2026-04-17, variance=0.0)` because their code
paths — `PredictionLedgerMixin` auto-resolution, `PolicyCache`
crystallization / Q-value updates, `ContextAssembler` competitive
suppression / surfacing, and cyclic cycle factors — are not exercised by
any scenario.

**Desired outcome:** A fresh `--parametric` sweep produces nDCG@5
variance > 0.05 for **≥5 constants**, fulfilling the #351 Success
Criterion. At least one new family scenario targets one of
PredictionLedger / PolicyCache / ContextAssembler, each with a
`TestFamilyGroundTruthDecoupling`-style sanity test.

## Freshness Check

**Baseline commit:** `7117c8b` (`Merge remote-tracking branch
'origin/main' into session/apply_experiment_learnings`).
**Issue filed at:** 2026-04-17T15:07:17Z.
**Disposition:** Unchanged.

**File:line references re-verified:**
- `tests/benchmarks/scenarios/family_factory.py` — 4 family scenarios
  (`DecayFamilyScenario`, `ConfidenceFamilyScenario`,
  `WriteFilterFamilyScenario`, `CoOccurrenceFamilyScenario`) plus
  `FamilyScenarioFactory.{for_constant, create_all, create_varied}`.
  Still matches issue claims.
- `src/popoto/fields/constants.py` — 22 constants carry
  `# empirically inert (sweep 2026-04-17, variance=0.0)` annotations.
  `DECAY_RATE=0.1`, `WF_MIN_THRESHOLD=0.1`, and
  `CO_OCCURRENCE_INITIAL_WEIGHT=0.1` reflect post-sweep best values.
  Matches issue claims.
- `tests/benchmarks/results/sweep_20260417_141047.json` — reference
  sweep still present. An untracked newer file
  `tests/benchmarks/results/sweep_20260330_160911.json` exists (Tier 1
  only, 5 constants) but is not the reference sweep for this issue.

**Cited sibling issues/PRs re-checked:**
- #351 — closed 2026-04-17T15:32:53Z by PR #361 (merged). Closure
  landed the override-reach + ground-truth fixes that define the
  baseline for this follow-up.
- #361 — merged 2026-04-17T15:32:53Z. Merge confirms the reference
  sweep file shipped with the PR.
- #296 / #312 — parent work that introduced family scenarios in the
  first place; closed.

**Commits on main since issue was filed (touching referenced files):**
- None. `git log --since="2026-04-17T15:07:17Z"` on
  `tests/benchmarks/`, `src/popoto/recipes/`, and
  `src/popoto/fields/` returns empty.

**Active plans in `docs/plans/` overlapping this area:**
- `docs/plans/apply_experiment_learnings.md` (parent plan, #351).
  Acceptance bullet 3 explicitly hands this gap off to "follow-up
  issue #362" — this plan is its designated closeout. No overlap with
  any other active plan.

**Notes:** Issue premises hold unchanged. The issue comment count is 0,
so there is no separate comment context to sync.

## Prior Art

- **Issue #296 / PR #312**: "Experiment iteration: scenarios must
  exercise constant-specific code paths" / "Add family-aware parametric
  scenarios for code path coverage". Introduced the four-scenario
  `FamilyScenarioFactory`. Succeeded for decay/write_filter/
  co_occurrence; confidence family had only borderline signal.
- **Issue #351 / PR #361**: "Apply experiment learnings". Fixed override
  reach (triple-patch strategy), decoupled ground truth from retrieval
  (critiques B1/B2/C1/C4), updated constants. Succeeded for 4 constants;
  confidence signal stayed at ~0.02; PredictionLedger/PolicyCache/
  ContextAssembler constants remained 0.0 because no family scenario
  touches those code paths. This follow-up closes that gap.
- **Issue #279 / PR #312** (reference): expanded interaction pairs and
  introduced parametric sweeps. Not directly overlapping — Tier 2/3
  sweep definitions remain stable.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| PR #312 | Added 4 family scenarios exercising decay / confidence / write_filter / co_occurrence | Confidence scenario used outcome-derived ground truth that was too correlated with the confidence signal itself, producing borderline variance even with perfect retrieval. PolicyCache and PredictionLedger had no scenario at all. |
| PR #361 | Fixed ground-truth decoupling (held-out future acted-rate for confidence, orthogonal urgency for write_filter, noise-link hop2 for co_occurrence) | Fixes moved decay/write_filter/co_occurrence variance above 0.05 but `ACTED_CONFIDENCE_SIGNAL`/`CONTRADICTED_CONFIDENCE_SIGNAL` stayed at ~0.02 because the held-out acted-rate is only weakly influenced by the signal weight constants (the confidence field updates are dwarfed by importance). And 22 other constants remained untouched — no scenario exercises their code path. |

**Root cause pattern:** The family-scenario set is defined by which
Popoto *fields* exist, not by which *mixins and recipes* consume those
fields. Every inert constant lives inside a code path
(`PredictionLedgerMixin.auto_resolve`, `policy_cache.crystallization_handler`,
`policy_cache.update_q_value`, `ContextAssembler.assemble`'s
suppression/surfacing branches) that is never executed by any scenario.
The fix is additive: write scenarios whose retrieval path routes
through those specific mixins/recipes.

## Research

No external research — the work is purely internal (Popoto scenarios
exercising Popoto primitives, measured with Popoto's own sweep
harness). No new libraries, APIs, or ecosystem patterns.

## Data Flow

For the new scenarios, the data flow extends the existing
family-scenario pipeline:

1. **Entry point**: `SweepRunner.run_single_sweep(constant_name, values)`
   invokes `run_parametric()` in `tests/benchmarks/run_sweeps.py`,
   which consumes family scenarios from
   `FamilyScenarioFactory.create_varied()`.
2. **Scenario setup**: `scenario.setup()` creates Model instances
   (PolicyEntry-like / prediction-bearing / suppression-prone),
   populates Redis state, and (for PolicyCache/PredictionLedger)
   invokes the crystallization / resolution paths to set up the
   state the retrieval query will rank over.
3. **Override injection**: `with apply_overrides(self.overrides):`
   wraps the retrieval query. This patches `Defaults`, module-level
   aliases (`policy_cache_mod.MIN_EVENTS_FOR_CRYSTALLIZATION`,
   `context_assembler_mod.COMPETITIVE_SUPPRESSION_SIGNAL`, etc.), and
   the `PredictionLedgerMixin` runtime-property reads.
4. **Retrieval + ground truth**: The scenario's `run()` method issues
   `composite_score` (or a PolicyCache-specific query) and compares
   against a held-out / orthogonal oracle.
5. **Output**: `ScenarioResult(retrieved_ids, relevant_ids,
   relevance_scores)` feeds `ndcg_at_k`, which produces one
   sensitivity-curve point per override value.
6. **Aggregation**: `ResultsAggregator` stores the sensitivity curve;
   a fresh `sweep_*.json` record carries per-constant variance derived
   from `max(curve) - min(curve)`.

For `PredictionLedgerFamilyScenario` specifically:
- Entry: `scenario.setup()` creates records with a
  `ConfidenceField` + `PredictionLedgerMixin`. Calls
  `PredictionLedgerMixin.record_prediction(inst, predicted=...)`
  followed by `PredictionLedgerMixin.auto_resolve(inst, outcome=...)`
  over a multi-round loop. The `outcome` per round is drawn from a
  per-record tier distribution (high / medium / low
  acted-likelihood) analogous to the confidence scenario.
- Query: `composite_score(indexes={"relevance": 0.3, "certainty": 0.7})`
  after resolution. The `PL_AUTO_RESOLVE_ACTED`/`DISMISSED`/
  `CONTRADICTED` error values directly drive
  `_apply_confidence_feedback`, shifting the certainty ranking.
- Oracle: a held-out future acted-rate (like the confidence family)
  — so the scenario tests whether a well-tuned PL_AUTO_RESOLVE_* triad
  produces certainty scores that predict future acted-rate.

For `PolicyCacheFamilyScenario`:
- Entry: `scenario.setup()` emits a stream of synthetic events with a
  mixture of `(state_fingerprint, action_type)` groups — some groups
  cross the `MIN_EVENTS_FOR_CRYSTALLIZATION` threshold with
  high-success rates (should crystallize), some with low counts but
  high-success rates (should NOT crystallize at
  `MIN_EVENTS=3` but WILL at `MIN_EVENTS=1`), some with high counts
  but low success (should NOT crystallize because
  `wilson_ci_lower <= WILSON_CI_THRESHOLD`). Calls
  `crystallization_handler` directly (or a synchronous helper).
- Query: rank-by-expected-value over `PolicyEntry` instances.
- Oracle: a held-out ground-truth "which (state, action) pairs were
  actually optimal" signal, generated from a separate RNG. `MIN_EVENTS`
  and `WILSON_CI_THRESHOLD` gate which pairs get crystallized, so their
  variation changes the set of retrievable PolicyEntry records and
  therefore nDCG@5.

For `ContextAssemblerFamilyScenario`:
- Entry: `scenario.setup()` creates a pool of records with a
  `ConfidenceField`, a `CyclicDecayField`, and a `ExistenceFilter`.
  Several "competing" records share similar cues.
- Query: `ContextAssembler.assemble(query_cues=...)`. The assembler's
  `_post_effects` invokes
  `ConfidenceField.update_confidence(candidate, signal=COMPETITIVE_SUPPRESSION_SIGNAL)`
  on non-selected pull candidates, mutating their certainty and
  affecting subsequent runs.
- To make the signal measurable: the scenario runs `assemble()` TWICE
  in sequence. The first call establishes selection (and suppresses
  losers); the second call's retrieval ordering is what nDCG measures.
  At low `COMPETITIVE_SUPPRESSION_SIGNAL`, loser records barely move
  and keep a similar ranking; at high signal, losers drop significantly
  and winners dominate the second-call retrieval. The oracle ranks by
  a pre-suppression ground-truth relevance, so high suppression
  over-penalizes originally-relevant records.

## Architectural Impact

- **New dependencies**: None. New scenarios compose existing Popoto
  primitives (no new library or service).
- **Interface changes**: `CONSTANT_FAMILY_MAP` gains entries mapping
  `PL_*` → `prediction_ledger`, `MIN_EVENTS_FOR_CRYSTALLIZATION` /
  `WILSON_CI_THRESHOLD` / `TD_ALPHA` / `TD_GAMMA` →
  `policy_cache`, and `COMPETITIVE_SUPPRESSION_SIGNAL` /
  `DEFAULT_SURFACING_THRESHOLD` → `context_assembler`.
  `FAMILY_NAMES` and `FAMILY_SCENARIO_CLASSES` gain entries for the
  new families. `FamilyScenarioFactory.create_varied` extends to
  seed/record-count tables for the new families. No public Popoto API
  changes.
- **Coupling**: Increases test-to-primitive coupling (tests now reach
  into `crystallization_handler`, `ContextAssembler.assemble`) — this
  is intentional; that coupling IS the sensitivity signal.
- **Data ownership**: Unchanged. Redis DB 15 remains the test target;
  scenarios are teardown-clean.
- **Reversibility**: High. The changes are purely additive — removing
  a new scenario restores the prior 4-family behavior with no impact
  on other tests.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (mid-build scope alignment after Tier B passes)
- Review rounds: 1 (one critique round, one PR review round)

Solo dev, straightforward code pattern (new scenarios follow the
existing family-scenario template), but the correctness bar is
evidence-driven — the fresh sweep must produce the variance. That
moves this above Small.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Scenarios exercise real Redis state |
| Python venv with `[dev]` extras | `python -c "import msgpack, pytest"` | Sweep harness + tests |
| Existing Tier 1/3 sweep definitions in `run_sweeps.py` | `grep -q "TIER3_SWEEPS" tests/benchmarks/run_sweeps.py` | PL/PolicyCache/ContextAssembler constants are already in the sweep matrix |

## Solution

### Key Elements

- **Tier B (minimum viable)**: Tighten `ConfidenceFamilyScenario`
  held-out distribution so `ACTED_CONFIDENCE_SIGNAL` /
  `CONTRADICTED_CONFIDENCE_SIGNAL` variance crosses 0.05.
  This alone satisfies the #351 Success Criterion.
- **Tier A.1 — PredictionLedgerFamilyScenario**: A new scenario that
  records and resolves predictions via `PredictionLedgerMixin`, ranks
  by composite certainty, and measures nDCG against held-out acted-rate.
- **Tier A.2 — ContextAssemblerFamilyScenario**: A new scenario that
  runs `ContextAssembler.assemble()` twice and measures nDCG on the
  second call's ordering against a pre-suppression oracle.
- **Tier A.3 — PolicyCacheFamilyScenario**: A new scenario that
  drives `crystallization_handler` with synthetic event streams and
  measures nDCG of the crystallized PolicyEntry retrieval against a
  held-out optimality oracle. Built unconditionally as part of the
  full-closeout scope (decision 2026-04-20).
- **CONSTANT_FAMILY_MAP extension**: Map each PL_*, policy-cache, and
  context-assembler constant to its owning family so
  `run_parametric` picks the right scenario.
- **Sanity tests**: One `TestFamilyGroundTruthDecoupling`-class test
  per new scenario, enforcing non-circular ground truth.

### Flow

Sweep invocation → `run_parametric` picks family scenarios for
`PL_CONFIDENCE_ERROR_THRESHOLD` → `PredictionLedgerFamilyScenario`
runs with override values → variance curve emerges → fresh
`sweep_YYYY*.json` shows ≥5 constants above 0.05 → constants.py
updated with best values and annotations.

### Technical Approach

1. **Start with Tier B** (lowest risk, fastest signal):
   - In `ConfidenceFamilyScenario`, widen the gap between tier
     distributions or extend the held-out window from 3 to 4-5 rounds.
     Empirically validate with a local Tier-1 sweep of
     `ACTED_CONFIDENCE_SIGNAL` that variance crosses 0.05.
   - If Tier B alone pushes the total above 5, Tier A becomes optional
     for the #351 criterion (but still desirable for closing the 22
     inert constants).
2. **Tier A.1 PredictionLedgerFamilyScenario**:
   - Model with `PredictionLedgerMixin` + `ConfidenceField`.
   - `setup()`: create records with spread importance, generate 8-round
     outcome sequences (like confidence scenario). Record a prediction
     per round, then `auto_resolve(outcome)` with `outcome = seq[r]`.
     Observed rounds: `seq[0:5]`; held-out: `seq[5:8]`.
   - `run()`: composite-score query on `{relevance: 0.3, certainty: 0.7}`.
     Ground truth: top-30% by held-out acted-rate.
   - Override sensitivity: `PL_AUTO_RESOLVE_ACTED` (0.1 default) being
     higher than `PL_AUTO_RESOLVE_DISMISSED` (0.5) reshapes the
     confidence updates because errors above `PL_CONFIDENCE_ERROR_THRESHOLD`
     trigger `_apply_confidence_feedback` with `PL_CONFIDENCE_LOW_SIGNAL`.
     Varying any of these five constants should shift certainty ranking.
3. **Tier A.2 ContextAssemblerFamilyScenario**:
   - Model with `ConfidenceField` + `CyclicDecayField` +
     `ExistenceFilter` + `WriteFilterMixin` (minimal valid
     ContextAssembler target).
   - `setup()`: create a pool of records with clusters of cue-sharing
     records (overlapping in `query_cues`-matchable fields). Some
     records are high-importance-pre-suppression, some are low.
   - `run()`: instantiate `ContextAssembler(model_class, score_weights,
     surfacing_threshold=DEFAULT_SURFACING_THRESHOLD, ...)`. Call
     `assemble(query_cues=...)` twice. Ground truth is the pre-
     suppression importance ranking over the full pool.
   - Override sensitivity: `COMPETITIVE_SUPPRESSION_SIGNAL` changes how
     aggressively losing candidates' confidence is reduced between
     calls; `DEFAULT_SURFACING_THRESHOLD` changes which push-path records
     make the second-call retrieval.
4. **Tier A.3 PolicyCacheFamilyScenario**:
   - Use the existing `PolicyEntry` model from
     `src/popoto/recipes/policy_cache.py`.
   - `setup()`: emit a synthetic stream of (state_fingerprint,
     action_type, outcome) events. Include groups that straddle the
     `MIN_EVENTS_FOR_CRYSTALLIZATION` and `WILSON_CI_THRESHOLD`
     boundaries. Call `crystallization_handler` directly with the
     in-memory batch (or synchronously via `asyncio.run` if needed).
   - `run()`: query `PolicyEntry` by `expected_value` ordering,
     composite-scored against `agent_id` partition.
   - Oracle: a held-out "which (state, action) is actually optimal"
     signal generated by a separate RNG. Higher `MIN_EVENTS` filters
     out small-sample pairs (good for precision, bad for recall at
     the edges); higher `WILSON_CI_THRESHOLD` raises the evidentiary
     bar similarly. `TD_ALPHA`/`TD_GAMMA` are exercised by a single
     `update_q_value` call per resolved prediction (optional — the
     initial Q-values from crystallization may be enough signal).
5. **Sanity tests**: add three new methods to
   `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling`,
   one per new scenario. Each test asserts:
   - Scenario executes successfully with default overrides.
   - `result.metadata["oracle"]` describes a decoupled ground-truth
     signal (not circular).
   - nDCG@5 differs by > 0.03 between two extreme override values for
     the scenario's primary constant. Matches the loose floor used
     for the existing four sanity tests.
6. **Constants update**: after the fresh sweep, update
   `src/popoto/fields/constants.py` for any newly-sensitive constant:
   set `Defaults.X` to the sweep's best value; replace the
   `# empirically inert (sweep 2026-04-17, variance=0.0)` annotation
   with a new-sweep annotation citing the new variance and best value.

### Non-circular ground-truth design per new scenario

- **PredictionLedger**: retrieval = `certainty` (trained on seq[0:5]);
  oracle = acted-rate over seq[5:8]. Identical decoupling pattern to
  `ConfidenceFamilyScenario` — held-out future is structurally
  independent of training past modulo the per-record tier distribution
  that both are drawn from. Tier distributions partially overlap so
  borderline records genuinely move.
- **ContextAssembler**: retrieval = second-call `assemble()` result
  (post-suppression); oracle = pre-suppression importance ordering.
  Suppression signal strength changes the second call's ranking without
  touching the oracle.
- **PolicyCache**: retrieval = crystallized PolicyEntries ranked by
  expected_value; oracle = held-out optimality label generated
  independently of the success/failure labels that drove
  crystallization. Even with perfect crystallization, retrieval
  rank-order need not match the independent optimality label — so
  override values that let noisy groups crystallize (low `MIN_EVENTS`)
  drop nDCG; values that crystallize only well-attested groups raise it.

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] `ContextAssembler._post_effects` uses pipeline and a broad
  `except Exception` around `pipeline.execute()`. The scenario does
  not need to test this branch directly, but the sanity test must not
  silently pass when the pipeline fails — assert the scenario status
  is `"ok"`, not simply that `retrieved_ids` is non-empty.
- [ ] `PredictionLedgerMixin.auto_resolve` returns `None` silently
  when no prediction is recorded. The scenario must call
  `record_prediction` before `auto_resolve` for every record; the
  sanity test must assert at least one resolution happens (via
  metadata: `n_resolutions > 0`).
- [ ] `crystallization_handler` silently skips groups that miss
  thresholds. The sanity test must assert at least one
  crystallization occurs at default settings
  (`n_crystallized_at_default > 0`).

### Empty/Invalid Input Handling

- [ ] Each new scenario's `run()` checks
  `len(self._instances) < 3 → return skipped-degenerate`, matching
  the existing four scenarios.
- [ ] Sanity tests that run the scenario at extreme override values
  must tolerate `ndcg_at_k` returning `None` for skipped-degenerate
  runs (skip the test with `pytest.skip`).

### Error State Rendering

- [ ] Scenario `ScenarioResult.error_message` must be populated with
  a specific string (not empty) whenever status is `"error"`. The
  sanity tests do not assert error strings but the scenario code must
  not swallow an exception into an empty error_message.

## Test Impact

- [ ] `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling`
  — UPDATE: add three new test methods (one per new scenario),
  mirroring the existing four tests' shape.
- [ ] `tests/benchmarks/test_overrides_reach.py` — UPDATE if new family
  scenario overrides surface names not in the current registry (e.g.
  if PL/PolicyCache/ContextAssembler sanity tests register new
  short-form override keys). Verify
  `CLASS_ATTR_CONSTANTS`/`MODULE_CONSTANTS` already cover every
  override the new scenarios will use (Defaults.PL_*,
  Defaults.MIN_EVENTS_FOR_CRYSTALLIZATION, Defaults.WILSON_CI_THRESHOLD,
  Defaults.COMPETITIVE_SUPPRESSION_SIGNAL, Defaults.DEFAULT_SURFACING_THRESHOLD
  — they are all present).
- [ ] `tests/benchmarks/scenarios/family_factory.py` — UPDATE: add new
  scenario classes, extend `CONSTANT_FAMILY_MAP`, `FAMILY_NAMES`,
  `FAMILY_SCENARIO_CLASSES`, and the `base_seed`/`record_count` dicts
  in `create_varied`.
- [ ] `tests/benchmarks/run_sweeps.py` — NO CHANGE required. The
  Tier 1/2/3 sweep definitions already include all the target
  constants; `run_parametric` picks family scenarios automatically
  via `CONSTANT_FAMILY_MAP`.
- [ ] `src/popoto/fields/constants.py` — UPDATE per-constant comments
  after the fresh sweep. Any constant whose fresh variance exceeds
  0.05 has its `# empirically inert` annotation replaced with a
  `# best from sweep YYYY-MM-DD, variance=X.XXX, prior=...`
  annotation, and its value updated to the sweep's best value.

## Rabbit Holes

- Building a full async StreamConsumer loop for PolicyCache. The
  crystallization_handler is async, but calling it directly with an
  in-memory batch via `asyncio.run` avoids the StreamConsumer
  machinery entirely. Don't wire Redis Streams just for a sensitivity
  scenario.
- Exercising every PL_* constant in a single scenario. Each
  `PL_AUTO_RESOLVE_*` constant independently shifts the error value
  per outcome; one scenario that drives all three outcomes is enough.
  Don't build three separate PL scenarios.
- Chasing PolicyCache `TD_ALPHA`/`TD_GAMMA` sensitivity. Q-value TD
  updates require a sequential reward/future-q feedback loop; that's
  a lot of machinery for two constants whose effect is bounded.
  Include a single `update_q_value` call per PolicyEntry if easy,
  otherwise accept that TD_ALPHA/TD_GAMMA remain inert and note it
  in the constants.py annotation.
- Trying to exercise `CHI_SQUARED_P_THRESHOLD` /
  `INITIAL_CYCLE_AMPLITUDE`. These belong to
  `temporal_discovery_handler`, which clusters event timestamps over
  a time range. Simulating enough temporal structure to make a
  chi-squared test non-trivial is a separate scenario class — defer
  to a future follow-up.
- Trying to cover all 22 inert constants. The acceptance criterion
  says "≥5 constants" total. Targeting one new family that lifts 2-3
  constants is sufficient. The rest of the inert constants can stay
  annotated.

## Risks

### Risk 1: Tier B alone fails to cross 0.05
**Impact:** Confidence variance stays borderline even after tightening
distributions; Tier A work becomes mandatory.
**Mitigation:** Start with Tier B because it's the cheapest. If after
a fresh Tier 1 sweep the confidence variance is still < 0.05, proceed
directly to Tier A.1 (PredictionLedger is the highest-leverage Tier A
target — five PL_* constants share one scenario).

### Risk 2: Fresh sweep produces < 5 sensitive constants even with new scenarios
**Impact:** Acceptance criterion unmet; further scenario iterations
needed.
**Mitigation:** The acceptance bar is 5 out of all swept constants,
not 5 newly-introduced. The 4 existing sensitive constants count.
Adding one new family scenario that lifts even ONE constant above
0.05 closes the gap. PredictionLedger holds 5 candidate constants;
at least one should move.

### Risk 3: ContextAssembler scenario is too slow for the sweep loop
**Impact:** Two `assemble()` calls per override value × 5 override
values × 3 scenario variants × 4 families → sweep duration balloons.
**Mitigation:** Cap the ContextAssembler scenario record pool at
15-20 records (vs. 30 for write_filter). If per-scenario runtime
exceeds 500ms, drop to 1 assemble() call per override by pre-seeding
suppressed confidence state directly via
`ConfidenceField.update_confidence` in setup(), skipping the first
assemble() call entirely.

### Risk 4: PolicyCache scenario requires `WriteFilterMixin` or a
composition conflict surfaces
**Impact:** `PolicyEntry` already composes `EventStreamMixin`,
`AccessTrackerMixin`, `PredictionLedgerMixin` — instantiating and
persisting instances in a test scenario may need extra stream setup.
**Mitigation:** The scenario can define its own minimal Model subclass
(not reuse `PolicyEntry`) that mimics the crystallization output
— a plain Model with DecayingSortedField `expected_value`. The
crystallization logic operates on (state_fingerprint, action_type)
groups and produces a model; reproducing the post-crystallization
state with a simpler model keeps the scenario self-contained.

### Risk 5: Scenario teardown leaves Redis state
**Impact:** Cross-scenario contamination in sweeps; subsequent runs
see stale keys.
**Mitigation:** Every existing family scenario iterates `instances` in
`teardown()` calling `inst.delete()`. New scenarios MUST do the same;
sanity tests should assert `scenario.teardown()` completes without
error. Redis DB 15 is flushed by conftest per test, but sweep-harness
runs don't flush between scenarios — so the per-instance delete path
is authoritative.

## Race Conditions

No race conditions identified — all scenario operations are
synchronous (even `crystallization_handler`, wrapped with
`asyncio.run` when used). Each scenario owns a unique
UUID-prefixed key namespace (`self._prefix`). The sweep harness runs
scenarios serially.

## No-Gos (Out of Scope)

- Exercising every one of the 22 inert constants. Target: ≥5 total
  sensitive. Stop once that bar is met.
- Building a full async StreamConsumer integration for PolicyCache.
  Use direct `crystallization_handler` invocation with an in-memory
  batch.
- Changes to the override-injection machinery (overrides.py is stable
  post-PR #361).
- Tuning any already-sensitive constant's default away from its PR
  #361 value unless the fresh sweep explicitly finds a better one.
- Temporal discovery (`temporal_discovery_handler`, chi-squared)
  scenarios — deferred to a future follow-up.
- Changing sweep-harness metric semantics (nDCG@5 > 0.05 stays the
  bar; don't change to variance@10 or other metric).

## Update System

No update system changes — purely test/benchmark code plus a
constants-file comment/value update. No new dependencies or configs.

## Agent Integration

No agent integration — benchmark sweep is a developer tool, invoked
via `python -m tests.benchmarks.run_sweeps`. No MCP wrapping.

## Documentation

### Feature Documentation

- [ ] Update `docs/plans/apply_experiment_learnings.md` to mark the
  `#362` acceptance bullet as checked once the fresh sweep confirms
  ≥5 sensitive constants. The plan file is already closed but the
  parent issue's tracker should reflect the follow-up completion.
- [ ] Update any docs under `docs/guides/` that reference the
  "4 family scenarios" count (spot-check: `agent-memory`,
  `policy-cache-recipe`, `context-assembler-recipe`). Bump to
  whatever the new count is (5, 6, or 7) and cite the new sweep
  file.

### External Documentation Site

- [ ] Inspect `docs/` MkDocs build — no tests expected to break, but
  the family-scenario count may appear in one of the primitive
  documentation pages.

### Inline Documentation

- [ ] Each new scenario class needs a docstring matching the existing
  four scenarios' format: purpose, ground-truth decoupling note,
  expected override sensitivity.
- [ ] `CONSTANT_FAMILY_MAP` docstring / comment should be updated
  with the new family entries.
- [ ] `constants.py` annotations updated per the constants-update
  step above.

## Success Criteria

- [ ] `FamilyScenarioFactory` registers at least one new scenario
  class targeting PredictionLedger OR PolicyCache OR ContextAssembler.
- [ ] `CONSTANT_FAMILY_MAP` gains entries mapping at least 2 previously-
  inert constants to the new scenario's family.
- [ ] Running
  `python -m tests.benchmarks.run_sweeps --parametric --tier all`
  produces a fresh `tests/benchmarks/results/sweep_YYYYMMDD_HHMMSS.json`
  in which nDCG@5 `max(curve) - min(curve) > 0.05` holds for **≥5
  constants**.
- [ ] At least one new constant (previously annotated as "empirically
  inert") shows variance > 0.05 in the fresh sweep.
- [ ] `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling`
  has one new method per new scenario, each asserting non-circular
  ground truth (loose floor: ndcg-between-extremes diff > 0.03).
- [ ] `src/popoto/fields/constants.py`: every constant whose fresh
  variance crosses 0.05 has its default updated to the best value
  AND its annotation replaced with a dated new-sweep comment.
- [ ] `pytest tests/benchmarks/test_factory.py -q` passes.
- [ ] `pytest tests/benchmarks/ -q` passes (full benchmark test
  suite, not just the factory file).
- [ ] `black src/ tests/` and `mypy src/` clean.
- [ ] Sweep file committed alongside the constants update so the
  evidence trail is intact.
- [ ] Documentation cascades updated (plan acceptance bullet,
  agent-memory guides, docs site build passes).

## Team Orchestration

Solo dev. No parallel team members needed — sequential task
execution fits the scope.

### Team Members

- **Builder (family-scenarios)**
  - Name: family-scenarios-builder
  - Role: Implement Tier B tightening, Tier A scenarios, and factory
    registry extensions.
  - Agent Type: builder
  - Resume: true

- **Validator (family-scenarios)**
  - Name: family-scenarios-validator
  - Role: Run the fresh sweep, verify ≥5 sensitive constants,
    validate sanity tests pass, check constants.py annotations.
  - Agent Type: validator
  - Resume: true

### Available Agent Types

See agent-type registry. Only `builder` and `validator` are needed
here.

## Step by Step Tasks

### 1. Tighten ConfidenceFamilyScenario (Tier B)
- **Task ID**: build-confidence-tighten
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_factory.py::test_confidence_family_held_out_split`
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: true
- In `tests/benchmarks/scenarios/family_factory.py`, widen the gap
  between tier distributions in
  `ConfidenceFamilyScenario.setup()._sample_tier`. Options to try:
  (a) extend sequence length from 8 to 10, held-out from 3 to 5;
  (b) sharpen the high-vs-low tier gap (e.g., 0.70/0.20/0.10 vs
  0.20/0.35/0.45); (c) add a small noise round at index 5 that
  breaks any residual correlation.
- Run a local Tier-1 parametric sweep of `ACTED_CONFIDENCE_SIGNAL` only
  (single constant, 5 values, all 8 family-confidence variants +
  10 generic scenarios) and confirm max-min nDCG@5 variance > 0.05.
  This is a focused local check, not a full sweep.
- Adjust distribution parameters iteratively until the local variance
  crosses 0.05 OR two iterations fail (time cap). If two iterations
  fail, tier-up to task 2 without reverting — Tier A will still
  satisfy the acceptance bar.

### 2. Build PredictionLedgerFamilyScenario (Tier A.1)
- **Task ID**: build-pl-scenario
- **Depends On**: none (can run parallel with build-confidence-tighten
  — both land in `family_factory.py` but touch disjoint classes)
- **Validates**: `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling::test_prediction_ledger_family_produces_variance` (new)
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `PredictionLedgerFamilyScenario` to `family_factory.py`
  modeled on `ConfidenceFamilyScenario`.
- Model: `popoto.Model` composing
  `PredictionLedgerMixin`, `ConfidenceField`, `DecayingSortedField`,
  `UniqueKeyField`, `StringField`, `FloatField`.
- In `setup()`: create 15-20 records with spread importance. Generate
  8-round outcome sequences per record (high-importance tier →
  high acted-rate, low-importance → low). For each record, for each
  of 5 observed rounds: call `record_prediction(inst, {"outcome": "acted"})`
  then `auto_resolve(inst, outcome=seq[round_idx])`. Held-out:
  `seq[5:8]`.
- In `run()`: apply overrides, query `composite_score(indexes={"relevance": 0.3, "certainty": 0.7})`.
  Compute relevance_scores = held-out acted-rate per record; top 30%
  are relevant.
- Add family registry entries: `CONSTANT_FAMILY_MAP` gains
  `"PL_CONFIDENCE_ERROR_THRESHOLD"`, `"PL_CONFIDENCE_LOW_SIGNAL"`,
  `"PL_AUTO_RESOLVE_ACTED"`, `"PL_AUTO_RESOLVE_DISMISSED"`,
  `"PL_AUTO_RESOLVE_CONTRADICTED"` → `"prediction_ledger"`.
  `FAMILY_NAMES`, `FAMILY_SCENARIO_CLASSES`, `create_varied`
  base_seed/record_count dicts each gain a `"prediction_ledger"`
  entry (e.g., `base_seed=5000, record_count=18`).
- Scenario name: `"family_prediction_ledger"`.

### 3. Add PredictionLedger sanity test
- **Task ID**: build-pl-test
- **Depends On**: build-pl-scenario
- **Validates**: itself
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `test_prediction_ledger_family_produces_variance` to
  `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling`.
  Two extreme values: `PL_AUTO_RESOLVE_ACTED=0.05` vs
  `PL_AUTO_RESOLVE_ACTED=0.5` (or whichever PL_* constant the scenario
  most strongly exercises).
- Assert ndcg-between-extremes diff > 0.03. Skip with pytest.skip if
  scenario returns skipped-degenerate at either extreme.

### 4. Build ContextAssemblerFamilyScenario (Tier A.2)
- **Task ID**: build-ca-scenario
- **Depends On**: build-pl-scenario (same file; serialize to avoid
  merge conflicts)
- **Validates**: `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling::test_context_assembler_family_produces_variance` (new)
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `ContextAssemblerFamilyScenario` to `family_factory.py`.
- Model: `popoto.Model` composing `ConfidenceField`,
  `CyclicDecayField(cycles=[(DAILY, 1.0, 0)])`, `ExistenceFilter`,
  `UniqueKeyField`, `FloatField`. (Skip `WriteFilterMixin` to keep
  setup simple — the assembler composes fine without it.)
- In `setup()`: create 15 records; importance spread 0.1-0.9; add each
  to the `ExistenceFilter`. Query cues defined by a keyword field
  (e.g., `topic`) with 3 distinct topics × 5 records each.
- In `run()`: apply overrides, instantiate `ContextAssembler(model_class,
  score_weights={"relevance": 0.7, "certainty": 0.3},
  surfacing_threshold=overrides.get("DEFAULT_SURFACING_THRESHOLD", 0.5))`.
  Call `assembler.assemble(query_cues={"topic": "topic_0"})` twice.
- Retrieved = second-call `.records` order.
- Oracle: the record's pre-suppression importance × topic-match
  indicator. Top 30% = relevant.
- `CONSTANT_FAMILY_MAP`: `"COMPETITIVE_SUPPRESSION_SIGNAL"` and
  `"DEFAULT_SURFACING_THRESHOLD"` → `"context_assembler"`.
  `FAMILY_NAMES`/`FAMILY_SCENARIO_CLASSES`/`create_varied` dicts
  each gain a `"context_assembler"` entry (`base_seed=6000,
  record_count=15`).
- Scenario name: `"family_context_assembler"`.

### 5. Add ContextAssembler sanity test
- **Task ID**: build-ca-test
- **Depends On**: build-ca-scenario
- **Validates**: itself
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `test_context_assembler_family_produces_variance` to
  `TestFamilyGroundTruthDecoupling`. Two extreme values:
  `COMPETITIVE_SUPPRESSION_SIGNAL=0.1` vs
  `COMPETITIVE_SUPPRESSION_SIGNAL=0.7`. Assert diff > 0.03.

### 6. Build PolicyCacheFamilyScenario (Tier A.3)
- **Task ID**: build-pc-scenario
- **Depends On**: build-ca-scenario (same file)
- **Validates**: `tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling::test_policy_cache_family_produces_variance` (new)
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- Built unconditionally as part of the full-closeout framing
  (decision recorded 2026-04-20 — prefer full buildout over
  minimum-viable gating).
- Add `PolicyCacheFamilyScenario` targeting
  `MIN_EVENTS_FOR_CRYSTALLIZATION` and `WILSON_CI_THRESHOLD`.
- Minimal model (NOT `PolicyEntry` — avoid the multi-mixin composition):
  a plain `popoto.Model` with `DecayingSortedField` `expected_value`,
  `UniqueKeyField`, `StringField action_type`.
- In `setup()`: synthesize event batch — 8 distinct (fingerprint,
  action) groups with varying `(successes, total)`:
  `(1, 1), (2, 3), (3, 5), (5, 5), (3, 10), (8, 10), (10, 15), (2, 20)`.
  Directly insert crystallized model instances for groups where
  `total >= MIN_EVENTS_FOR_CRYSTALLIZATION AND wilson_ci_lower(successes, total) > WILSON_CI_THRESHOLD`.
  Use `wilson_ci_lower` from `src/popoto/recipes/policy_cache.py`.
- In `run()`: composite score query on `expected_value`. Oracle:
  independently-generated "truth ratio" per group, with top 30% as
  relevant. Retrieval misses groups that failed to crystallize and
  are therefore absent from the model — nDCG drops when thresholds
  are too strict.
- `CONSTANT_FAMILY_MAP`: `"MIN_EVENTS_FOR_CRYSTALLIZATION"`,
  `"WILSON_CI_THRESHOLD"` → `"policy_cache"`.
- Scenario name: `"family_policy_cache"`.

### 7. Run fresh parametric sweep
- **Task ID**: run-sweep
- **Depends On**: build-pc-scenario (Tier B + A.1 + A.2 + A.3) and
  build-confidence-tighten
- **Validates**: Variance-threshold acceptance criterion
- **Assigned To**: family-scenarios-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --parametric --tier all`
  from the repo root with a warm Redis on DB 15 (the sweep harness
  uses the default DB; scenarios self-prefix with UUIDs).
- Parse the resulting `tests/benchmarks/results/sweep_YYYYMMDD_HHMMSS.json`
  and compute per-constant variance as `max(sensitivity_curve_y) -
  min(sensitivity_curve_y)`.
- Count constants with variance > 0.05. If the count is ≥ 5,
  proceed to task 8. If < 5, return to task 1 to re-tune Tier B
  distributions more aggressively (all four new scenarios are
  already built by this point).
- Expected total sweep runtime: 20-30 seconds on warm Redis.
  `n_per_family=8` stays fixed for all new families
  (decision recorded 2026-04-20).

### 8. Update constants.py with fresh sweep findings
- **Task ID**: build-constants-update
- **Depends On**: run-sweep
- **Validates**: `pytest tests/benchmarks/test_defaults_sync.py -q`
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- For each constant whose fresh variance > 0.05:
  - If it was previously annotated "empirically inert", replace the
    annotation with `# best from sweep YYYY-MM-DD, variance=X.XXX, prior=OLD_VALUE`.
  - Update `Defaults.X` to the sweep's best value
    (the value that produced the highest nDCG@5 averaged across
    family+generic scenarios).
- For constants that remain < 0.05, update the sweep-date in their
  `empirically inert` annotation to the new sweep's date
  (bookkeeping — shows the annotation is still current).
- Do NOT change the value of any constant whose variance is below
  0.05; the prior annotations stay.

### 9. Update documentation
- **Task ID**: document-family-scenarios
- **Depends On**: build-constants-update
- **Assigned To**: family-scenarios-builder
- **Agent Type**: builder
- **Parallel**: false
- Update any `docs/guides/*` files that mention the "4 family
  scenarios" count (grep
  `-rn "4 family\|four family\|4 scenarios" docs/`). Bump to the new
  count.
- Update `docs/plans/apply_experiment_learnings.md` to mark the
  "#362 follow-up" note next to the variance acceptance bullet as
  complete.
- Update the module docstring of
  `tests/benchmarks/scenarios/family_factory.py` to list the new
  family names (decay, confidence, write_filter, co_occurrence,
  prediction_ledger, context_assembler, [policy_cache]).

### 10. Final validation
- **Task ID**: validate-all
- **Depends On**: document-family-scenarios
- **Assigned To**: family-scenarios-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/benchmarks/ -q` and confirm full pass.
- Re-run `pytest tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling -v`
  and verify every sanity test (existing 4 + 2 or 3 new) passes.
- Run `black src/ tests/` and `mypy src/` — both must be clean.
- Confirm fresh sweep file is committed alongside the constants update
  so a PR reviewer can cross-reference the variance evidence.
- Generate a final report: count of sensitive constants in the fresh
  sweep, list of newly-sensitive constants (and their variance +
  best value), list of constants whose value changed.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full test suite | `pytest tests/benchmarks/ -q` | exit code 0 |
| Sanity tests | `pytest tests/benchmarks/test_factory.py::TestFamilyGroundTruthDecoupling -v` | exit code 0 |
| Defaults sync | `pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Overrides reach | `pytest tests/benchmarks/test_overrides_reach.py -q` | exit code 0 |
| Lint | `black --check src/ tests/` | exit code 0 |
| Type check | `mypy src/` | exit code 0 |
| Variance ≥ 5 constants | `python -c "import json,sys; d=json.load(open(sys.argv[1])); n=sum(1 for c in d['constants'].values() if (max(p[1] for p in c['sensitivity_curve']) - min(p[1] for p in c['sensitivity_curve'])) > 0.05); sys.exit(0 if n>=5 else 1)" tests/benchmarks/results/sweep_LATEST.json` | exit code 0 |
| Family factory has ≥ 5 registered classes | `python -c "from tests.benchmarks.scenarios.family_factory import FAMILY_SCENARIO_CLASSES as f; assert len(f) >= 5, len(f)"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique. Leave empty until critique is run. -->

---

## Decisions (resolved 2026-04-20)

1. **Tier A.3 (PolicyCache) inclusion**: Built unconditionally.
   Full-closeout framing preferred over minimum-viable.
2. **Sweep runtime tolerance**: 20-30 seconds acceptable.
   `n_per_family=8` kept fixed for all new families.
3. **Default value changes**: Continue the PR #361 policy — keep
   defaults near their semantic-meaning value when the sweep best
   is within noise. Accept material changes only when the variance
   clearly favors a different value.
