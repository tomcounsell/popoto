---
status: Planning
type: chore
appetite: Medium
owner: Valor Engels
created: 2026-04-17
tracking: https://github.com/tomcounsell/popoto/issues/351
last_comment_id:
---

# Apply experiment learnings: fix overrides, ground truth, and update constants

## Problem

Popoto's agent-memory subsystem has 19 tunable numeric constants in `src/popoto/fields/constants.py`. Four issues (#268, #279, #293, #296) built a sweep harness to find empirically optimal defaults. The infrastructure shipped but the payoff did not — saved sweep results still show flat sensitivity (nDCG@5 variance < 0.05 for every swept constant), 7 constants are not reachable by the runtime override system at all, and no defaults have been updated from experimental data. The system works as a science project; it does not yet work as an evidence-based tuning loop.

**Current behavior (verified on commit `b298922`):**
- Latest saved sweep (`sweep_20260330_160911.json`, 2026-03-30) runs in `--parametric` mode and *does* invoke `FamilyScenarioFactory`, but family scenarios are drowned ~5:88 by generic `gen_*` scenarios. All 5 Tier-1 constants show nDCG variance < 0.023.
- Family-factory scenarios define ground truth using the same signal that drives retrieval (importance = relevance in `DecayFamilyScenario` and `WriteFilterFamilyScenario`; outcome sequence = retrieval ranking in `ConfidenceFamilyScenario`). Perfect ranking is trivial regardless of the constant value.
- `WriteFilterMixin._wf_min_threshold` and `_wf_priority_threshold` read from `Defaults` at **class body time** (`write_filter.py:60-61`). `PredictionLedgerMixin._pl_confidence_error_threshold`, `_pl_confidence_low_signal`, `_pl_auto_resolve_*` do the same (`prediction_ledger.py:110-116`). Patching `Defaults.WF_MIN_THRESHOLD` after import has no effect on these cached attributes.
- `tests/benchmarks/overrides.py` `MODULE_CONSTANTS` does not contain any of the 7 WF/PL constants. The `hasattr(Defaults, name.upper())` fallback at line 152 patches `Defaults` but not the mixin class attributes already bound at import, so the overrides silently do nothing.
- No constant in `constants.py` has been updated from sweep data. `TIER1_SWEEPS` has best-value results sitting in `sweep_20260330_160911.json` that never propagated back.

**Desired outcome:**
- Every one of the 19 swept constants is verifiably patchable inside `apply_overrides()`. A dedicated test asserts the effective value changes.
- Family-factory ground truth is decoupled from the retrieval signal. Each family scenario defines relevance based on the *outcome* the constant controls, not the input signal.
- A fresh `--parametric` or `--ratchet` sweep produces nDCG@5 variance > 0.05 for at least 5 constants.
- Constants with demonstrated sensitivity get their `Defaults.*` values updated to the sweep's best value; inert constants are documented in place with a brief comment.

## Freshness Check

**Baseline commit:** `b298922` (as of 2026-04-17)
**Issue filed at:** 2026-04-05T06:56:48Z (12 days old)
**Disposition:** Minor drift — two surface claims in the issue are stale but the core problems remain valid.

**File:line references re-verified:**
- `src/popoto/fields/write_filter.py:60-61` (`_wf_min_threshold = Defaults.WF_MIN_THRESHOLD`) — still holds. Read at class body time.
- `src/popoto/fields/prediction_ledger.py:110-116` (`_pl_*` attributes read from `Defaults`) — still holds.
- `tests/benchmarks/overrides.py:23-65` (`MODULE_CONSTANTS` registry) — still holds. Missing WF_* and PL_* entries confirmed.
- `tests/benchmarks/scenarios/family_factory.py:180-200` (circular ground truth in `DecayFamilyScenario`) — still holds. `importance * effective_age^(-decay_rate)` uses the same signal as the retrieval path. Same pattern in `WriteFilterFamilyScenario:527-545`.

**Cited sibling issues/PRs re-checked:**
- #268 — CLOSED 2026-03-25. Defined the experiment harness.
- #279 — CLOSED (sweep expansion, merged via PR #292 on 2026-03-26).
- #293 — CLOSED (parametric + ratchet infra).
- #296 — CLOSED 2026-03-30. Shipped `FamilyScenarioFactory` via PR #312.

**Commits on main since issue was filed (touching referenced files):**
- `b298922` OllamaProvider — unrelated to benchmarks or constants.
- `db6a317` Plan revision — unrelated.
- **No commits touched `tests/benchmarks/`, `src/popoto/fields/constants.py`, `src/popoto/fields/write_filter.py`, or `src/popoto/fields/prediction_ledger.py` since the issue was filed.**

**Active plans in `docs/plans/` overlapping this area:** None active. `experimental_tuning_magic_numbers.md` (#268), `parametric_sweep_redesign.md` (#293), and `scenario_code_path_coverage.md` (#296) are all shipped.

**Stale claims in the issue (corrections absorbed into this plan):**
- Issue says "All saved sweep results were generated using only 3 hand-crafted scenarios." → A newer sweep (`sweep_20260330_160911.json`, dated 2026-03-30) exists that DOES invoke `FamilyScenarioFactory`. But family scenarios are outnumbered ~5:88 by generic scenarios, so their signal is heavily diluted. The problem remains; the framing is just slightly outdated.
- Issue says "The `latest.json` symlink in results/ is broken." → The symlink correctly points to `sweep_20260330_160911.json`. No fix needed.
- Issue says "18 of 19 constants show **zero sensitivity** — nDCG@5 is flat at 0.9971." → The latest sweep shows variance 0.0225 for decay_rate and 0.0000–0.0111 for the other 4 Tier-1 constants. Still under the 0.05 target. Only Tier 1 was swept; Tiers 2–3 were not re-run after the family factory landed.

**Notes:** All three core problems (circular ground truth, override gap, flat sensitivity) are reproducible today. The plan proceeds on a corrected premise: the fix is not "run the family factory for the first time" but "give the family factory meaningful decoupled ground truth, fix the 7 override gaps, and rerun with family scenarios weighted appropriately."

## Prior Art

- **#268** *(CLOSED 2026-03-25)* — Designed constant tuning experiments. Shipped the sweep harness.
- **PR #250** *(MERGED 2026-03-22)* — Experimental tuning benchmark harness for agent-memory constants. Foundational infra.
- **#279** *(CLOSED)* / **PR #292** *(MERGED 2026-03-26)* — Expanded sweep coverage across Tiers 1–4. Flat sensitivity persisted.
- **#293** *(CLOSED)* — Parametric scenario generation with ratchet loop. Infrastructure improvement, no default changes.
- **#296** *(CLOSED 2026-03-30)* / **PR #312** *(MERGED 2026-03-30)* — `FamilyScenarioFactory` with 4 families. Merged but the follow-up sweep to actually *use* it effectively and apply results never ran.

## Research

**Queries used:**
- `hyperparameter sensitivity sweep nDCG retrieval ground truth decoupling benchmarking 2026`
- `Python class-level constant patching test override monkeypatch default factory runtime`

**Key findings:**
- Pytest's monkeypatch (and equivalent manual patching) must target *where the code is used*, not where it's defined. Class-body reads like `_wf_min_threshold = Defaults.WF_MIN_THRESHOLD` bind at class-definition time; patching `Defaults.WF_MIN_THRESHOLD` later cannot reach them. Source: [pytest-with-eric.com](https://pytest-with-eric.com/mocking/pytest-monkeypatch/), [pythonforthelab.com](https://pythonforthelab.com/blog/monkey-patching-and-its-consequences/). This directly informs the Technical Approach: patch the mixin class attributes OR rewrite reads to runtime lookups — nothing else will work.
- For retrieval benchmarks, nDCG@K requires graded relevance derived from an external ground-truth source, not from the same signal the retriever consumes. Using a shared signal produces perfect ranking by construction — a known trap. Source: [weaviate.io/blog/retrieval-evaluation-metrics](https://weaviate.io/blog/retrieval-evaluation-metrics). This confirms that decoupling ground truth is a correctness fix, not a stylistic one.

## Data Flow

The sensitivity signal pipeline, as it fails today:

1. **Entry**: `run_sweeps.py --parametric` invokes `SweepRunner` with a scenario list.
2. **Scenario construction**: `FamilyScenarioFactory.for_constant(name)` returns family-specific scenarios; the runner also appends 20 generic `ScenarioFactory` scenarios. Family scenarios become ~5% of the workload.
3. **Override application**: `apply_overrides({name: value})` patches `Defaults.X` and (for constants in `MODULE_CONSTANTS`) the module-level alias. For WF_* and PL_*, neither patch reaches the cached class attribute on `WriteFilterMixin`/`PredictionLedgerMixin`. The model instantiated inside the scenario reads the unchanged cached value.
4. **Scenario setup + run**: In `DecayFamilyScenario`, records are saved with `importance` drawn from a clustered range; composite_score uses `relevance` which is `DecayingSortedField(base_score_field=importance)`. Ground truth is computed as `importance * age^(-decay_rate)`. Both retrieval and ground truth read from `importance` → perfect ordering for any decay_rate.
5. **Metric computation**: `ndcg_at_k` compares retrieved order to ideal order. Because ideal and actual are driven by the same signal, nDCG ≈ 1.0 regardless of the override.
6. **Output**: Flat sensitivity curves get aggregated; best value is whichever one happens to score highest in noise. No learning signal propagates back to `Defaults.*`.

The fix has to intervene at **steps 3 and 4** together: the override system must actually change behavior, AND the scenario must measure behavior downstream of the changed constant — not the input that was already decided before the override took effect.

## Architectural Impact

- **New dependencies**: None.
- **Interface changes**: `WriteFilterMixin` and `PredictionLedgerMixin` move from caching `Defaults.*` at class body to reading via property or method at runtime. Public API surface (class attribute names like `_wf_min_threshold`) is preserved — subclasses that set their own values still win.
- **Coupling**: Slightly decreases. Reads move from import-time snapshots to lookup-time reads, which is closer to how the sweep harness already expects overrides to flow.
- **Data ownership**: Unchanged. `Defaults` remains the single source of truth.
- **Reversibility**: High. All changes are scoped to mixin implementation and to benchmark scenarios. A revert is a three-file diff.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1 (after override fix and ground-truth fix, before running the final sweep — decide if best values are accept/hold)
- Review rounds: 1

This has three phases that depend on each other but each phase is small. The medium size comes from needing to interpret a fresh sweep and defend changes to `constants.py`, not from coding volume.

## Prerequisites

No prerequisites — Redis (localhost:6379) is already required for the standard test suite; the sweep uses the same connection. No external API keys.

## Solution

### Key Elements

- **Override-reachable mixin reads**: `WriteFilterMixin` and `PredictionLedgerMixin` stop caching `Defaults.*` at class body. Reads become runtime lookups (property or `getattr(self, ...)` with fallback to `Defaults.*`), so `apply_overrides()` patching `Defaults` is actually observed.
- **Override registry coverage**: `tests/benchmarks/overrides.py` gains an explicit `CLASS_ATTR_CONSTANTS` entry for WF_MIN_THRESHOLD, WF_PRIORITY_THRESHOLD, and the 5 PL_* constants. Each override patches `Defaults` AND any class-body fallbacks still present on relevant mixins. A new test `tests/benchmarks/test_overrides_reach.py` asserts, for all 19 constants, that the effective value inside the context manager differs from the default.
- **Decoupled family ground truth**: Each of the 4 family scenarios gets its ground truth rewritten so that relevance is determined by the *outcome of the constant's behavior* rather than the input signal:
  - `DecayFamilyScenario`: ground truth = records the retrieval system *would rank in the top-K if decay_rate were correct for this data's age distribution*. Use a held-out reference decay_rate (e.g., 0.5 as the "truth") and score nDCG against its ranking. Overrides that deviate should drop nDCG.
  - `ConfidenceFamilyScenario`: ground truth = records whose *future* outcome sequence (a second, unseen set of interactions) is "acted"-dominant. The retriever only sees past outcomes; ground truth uses held-out ones.
  - `WriteFilterFamilyScenario`: ground truth = top-K by importance from the *full intended set* (kept as-is, this one was already decoupled) — verify and document.
  - `CoOccurrenceFamilyScenario`: ground truth already uses cluster membership (hop1, hop2), not the raw propagation score — verify and document.
- **Family-weighted sweep**: Run `run_sweeps.py --parametric --tier all` with a reduced generic-scenario count so family scenarios are not drowned. Alternatively add a `--family-only` flag.
- **Evidence-driven defaults**: For each constant with nDCG variance > 0.05, update `Defaults.*` to the best-scoring value. For constants with variance ≤ 0.05 after decoupled ground truth, add a one-line comment `# empirically inert (sweep YYYY-MM-DD)` next to the default. Commit the sweep result alongside the `constants.py` update.

### Flow

Current `constants.py` defaults → `/do-build` lands override fixes + decoupled ground truth → fresh `--parametric` sweep run → analyst (PM) reviews best values → `/do-build` commits updated defaults with sweep JSON as evidence → test suite still green → merge.

### Technical Approach

- **Fix 1 — Make mixin reads override-reachable.** Two viable options, pick one per mixin:
  - *Option A (preferred for WriteFilterMixin):* Change `_wf_min_threshold` from a class attribute to a `@property` that returns `Defaults.WF_MIN_THRESHOLD`, with a per-instance override stored as `self._wf_min_threshold_override`. Subclasses can still override by shadowing the property with a class attribute.
  - *Option B (preferred for PredictionLedgerMixin):* Keep class attributes but make them lazy — use a `classmethod` accessor `_get_pl_setting(name)` that reads from `Defaults` at call time. The existing `_pl_auto_resolve_errors` dict is rebuilt per-call. The mixin's private callers already use `getattr(instance, '_pl_confidence_error_threshold', 0.7)`, so they adapt naturally.
  - **Validation:** Write `tests/benchmarks/test_overrides_reach.py::test_all_constants_patchable` that iterates all 19 constant names, enters `apply_overrides({name: sentinel_value})`, reads the effective value via the production code path, and asserts it equals `sentinel_value`.
- **Fix 2 — Extend `overrides.py` registry.** Add `CLASS_ATTR_CONSTANTS` parallel to `MODULE_CONSTANTS` that maps constants to the mixin classes needing class-attr patches. `apply_overrides()` saves and restores class attributes for these. After Fix 1, this may be a no-op for the refactored mixin, but it is a belt-and-suspenders guard and makes the registry authoritative.
- **Fix 3 — Decouple ground truth.** In `family_factory.py`, each scenario's `run()` computes `relevance_scores` from a signal *different* from the one the retriever consumes:
  - `DecayFamilyScenario`: add `self._reference_decay_rate = 0.5` during `setup()`. Ground truth uses `importance * age^(-reference_decay_rate)`. Retrieval uses the overridden `decay_rate`. Now an override of 0.1 or 0.9 will diverge from the reference ordering, and nDCG will drop.
  - `ConfidenceFamilyScenario`: split `_outcome_sequences` into `_observed_outcomes` (fed to ObservationProtocol) and `_held_out_outcomes` (used to compute ground truth). The retriever only sees past outcomes; ground truth is future behavior.
  - `WriteFilterFamilyScenario` and `CoOccurrenceFamilyScenario`: audit each — if ground truth is already decoupled (top-K by planned importance; cluster hop membership), just add a comment noting the decoupling and skip further changes.
- **Fix 4 — Run sweep, apply, commit.**
  1. `python -m tests.benchmarks.run_sweeps --parametric --tier 1` (verify sensitivity with fixed scaffolding).
  2. If ≥5 constants show variance > 0.05, extend to `--tier 2` and `--tier 3`. Otherwise, investigate and iterate on ground-truth decoupling.
  3. Update `src/popoto/fields/constants.py` with best values. Inert constants get a comment. Include the sweep JSON path in the commit message.

Two existing plan docs (`experimental_tuning_magic_numbers.md`, `scenario_code_path_coverage.md`) remain the authoritative references for *why* each constant exists and *how* the family factory was designed. This plan does not re-derive either; it completes the loop.

## Failure Path Test Strategy

### Exception Handling Coverage
- `tests/benchmarks/overrides.py` has a `try / finally` in `apply_overrides` but no `except` that swallows errors — every unpatched constant currently raises AttributeError on restore attempts if the patch mutated a non-existent attribute. Before Fix 2, add a test that `apply_overrides({'DOES_NOT_EXIST': 0.5})` leaves `Defaults` unchanged (currently raises silently). The fixed behavior: it should raise `KeyError` or log a warning — decide in Fix 2.
- `scenarios/family_factory.py` has `try / except Exception as e:` blocks wrapping save/link/propagate calls (write_filter.py:275+, family_factory.py:135+, etc.) that return `ScenarioResult(status="error")`. Each such path already asserts via the sweep's error counting — no additional test needed, but confirm the error counts in the fresh sweep are ≤ 5% of total scenarios.

### Empty/Invalid Input Handling
- New `test_overrides_reach.py` covers empty override dict, dict with unknown keys, and dict with known keys set to valid-range boundary values.
- Family scenarios already `return ScenarioResult(status="skipped-degenerate")` for n < 3 instances. No change.

### Error State Rendering
- Sweep output is machine-readable JSON; errors surface as `status: "error"` entries in the result file. No user-facing rendering path.

## Test Impact

- `tests/benchmarks/test_overrides_reach.py` — CREATE: new test asserting all 19 constants are patchable end-to-end.
- `tests/benchmarks/test_factory.py` — INSPECT: confirms family scenario construction. May need UPDATE if ground-truth decoupling changes scenario signatures.
- `tests/benchmarks/test_sweep.py` — INSPECT: may need UPDATE if new `CLASS_ATTR_CONSTANTS` branch is added to `apply_overrides`.
- `tests/test_write_filter.py` (if exists) — INSPECT: if Option A is chosen for Fix 1, tests that directly set `_wf_min_threshold` as a class attribute still need to work. The property should yield to any class-level override in a subclass.
- `tests/test_prediction_ledger.py` (if exists) — INSPECT: same as above for PL_* constants.

No deletions expected. All changes are additive or refactor-safe.

## Rabbit Holes

- **Redesigning the override system from scratch.** Tempting given how fragile the dual-patch mechanism is, but out of scope. The minimal Fix 2 addition (class-attr registry) is enough.
- **Benchmarking retrieval latency as part of this work.** The sweep already records `duration_ms`, but tuning for latency is a separate optimization effort.
- **Adding a `--family-only` flag + CI wiring.** Nice-to-have but not required to unblock the issue. Default to running `--parametric` with reduced generic-scenario count.
- **Revising the `Defaults` class structure (e.g., to frozen dataclass, Pydantic model).** Defer. The current plain class works once reads are done at runtime.
- **Adjusting the metrics in `tests/benchmarks/metrics/`.** Explicitly forbidden by issue constraints and by this plan — metrics are the ruler, not the thing being measured.

## Risks

### Risk 1: Ground-truth decoupling makes scenarios pathologically easy or hard
**Impact:** A reference-ordering ground truth could produce nDCG values clustered at 1.0 or 0.0 regardless of the override, defeating the sensitivity signal a second time.
**Mitigation:** After Fix 3, before running the full sweep, run one targeted `--tier 1` sweep and inspect variance. If variance is still < 0.05, iterate on the scenario before spending compute on full sweeps. Include a quick `test_family_factory_produces_variance` check that asserts `decay_rate=0.1` and `decay_rate=0.9` produce nDCG values differing by > 0.05 in `DecayFamilyScenario`.

### Risk 2: Option A (property) breaks subclasses that set `_wf_min_threshold` as a class attribute
**Impact:** Models that extend `WriteFilterMixin` and set `_wf_min_threshold = 0.3` at class body might silently hit the property instead of their override.
**Mitigation:** Python's MRO evaluates descriptors first, so a subclass class attribute *does* shadow a parent property — but only if the subclass attribute is a plain value, not a property itself. Add a test `test_subclass_override_wins` that extends the mixin with a class-level 0.3 and asserts reads return 0.3 even when `Defaults.WF_MIN_THRESHOLD` is patched to 0.7.

### Risk 3: Updating `Defaults.*` values breaks downstream tests that depend on the old defaults
**Impact:** Tests that were tuned around `DECAY_RATE=0.5` (for example) might fail if we set it to 0.1.
**Mitigation:** Run the full pytest suite (`pytest`) after each `Defaults.*` change. Revert individual changes that break tests and document the conflict in a code comment (e.g., `DECAY_RATE = 0.5  # best sweep value 0.1, but test_X depends on current behavior — see issue N`). Don't silently loosen test assertions.

### Risk 4: Sweep takes long enough that iteration becomes painful
**Impact:** Previous sweep reports wall_clock ~15s for Tier 1, but full-tier sweep with family variants × parametric scenarios could scale to minutes.
**Mitigation:** Start with Tier 1 only. Use `--ratchet` (has train/validation split and early-termination logic) if Tier 2/3 take too long.

## Race Conditions

No race conditions identified — the sweep runs serially scenario by scenario, and `apply_overrides` is a single-threaded context manager with strict save/restore semantics. All operations on Redis go through the connection pool but each scenario `setup()`/`run()`/`teardown()` runs sequentially. Confidence updates inside `ConfidenceFamilyScenario` use the existing `ConfidenceField.update_confidence` pipeline which is atomic per-call.

## No-Gos (Out of Scope)

- Redesigning `Defaults` as anything other than a plain class.
- Modifying anything in `tests/benchmarks/metrics/` (explicitly forbidden by issue constraints).
- Changing the `Scenario` or `ScenarioFactory` public interfaces.
- Breaking the existing hand-crafted scenarios in `run_sweeps.py` (`FactualRecallScenario`, `MultiStepReasoningScenario`, etc.).
- Removing inert constants — document them in place with a comment. Removal is a separate decision.
- Benchmarking the metacognitive layer (#352). This plan unblocks #352 by delivering correctly-tuned mechanical primitives; it does not implement #352's features.

## Update System

No update system changes required — this is purely a test-harness + benchmark + defaults change. No new dependencies, no new config.

## Agent Integration

No agent integration required — Popoto is a library consumed by other projects. The sweep harness runs locally via `python -m tests.benchmarks.run_sweeps`. No MCP server or bridge involvement.

## Documentation

### Feature Documentation
- [ ] Update `docs/plans/experimental_tuning_magic_numbers.md` with a trailing "Results applied" section linking to this plan and the sweep JSON.
- [ ] Update `docs/plans/scenario_code_path_coverage.md` with a trailing note on which scenarios had ground-truth decoupling applied.

### External Documentation Site
- [ ] If `docs/guides/` has a file on constants or tuning, add a brief note that defaults are now empirically tuned (source: `sweep_YYYYMMDD_HHMMSS.json`).

### Inline Documentation
- [ ] Code comment on each updated `Defaults.*` line recording the sweep date and variance for auditability.
- [ ] Code comment on each `# empirically inert` constant with the sweep date.
- [ ] Docstring on `apply_overrides` noting the `CLASS_ATTR_CONSTANTS` mechanism (if added in Fix 2).

## Success Criteria

- [ ] `tests/benchmarks/test_overrides_reach.py::test_all_constants_patchable` passes for all 19 constants.
- [ ] `WriteFilterMixin` and `PredictionLedgerMixin` reads of `Defaults.*` happen at method-call / attribute-access time, not class-definition time.
- [ ] Family-factory scenarios define ground truth from a signal distinct from the retrieval signal. Each scenario has a code comment explaining which signal drives ground truth and which drives retrieval.
- [ ] A `--parametric` sweep produces nDCG@5 variance > 0.05 for at least 5 constants.
- [ ] `src/popoto/fields/constants.py` has best-value updates for every sensitive constant, each annotated with the sweep date.
- [ ] Inert constants have inline `# empirically inert (sweep YYYY-MM-DD)` comments.
- [ ] `latest.json` symlink points to the newest sweep JSON (should already; verify).
- [ ] `pytest` passes with no regressions after constant changes.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (overrides)**
  - Name: `overrides-builder`
  - Role: Refactor `WriteFilterMixin` and `PredictionLedgerMixin` for runtime reads; extend `tests/benchmarks/overrides.py` registry; write `test_overrides_reach.py`.
  - Agent Type: builder
  - Resume: true

- **Builder (scenarios)**
  - Name: `scenarios-builder`
  - Role: Decouple ground truth in `family_factory.py` (decay, confidence scenarios primarily; verify + comment write_filter and co_occurrence).
  - Agent Type: builder
  - Resume: true

- **Sweep operator**
  - Name: `sweep-runner`
  - Role: Run `run_sweeps.py --parametric`, interpret results, update `Defaults.*` in `constants.py` with best values or inert-comments.
  - Agent Type: builder
  - Resume: true

- **Validator**
  - Name: `sweep-validator`
  - Role: Verify variance targets, run full test suite after constants changes, confirm no regressions.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Refactor mixins for runtime `Defaults` reads
- **Task ID**: build-mixin-refactor
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_overrides_reach.py` (create), `tests/test_write_filter*.py` (if present), `tests/test_prediction_ledger*.py` (if present)
- **Informed By**: Research finding on monkeypatching where code is used
- **Assigned To**: overrides-builder
- **Agent Type**: builder
- **Parallel**: true
- Convert `WriteFilterMixin._wf_min_threshold` and `_wf_priority_threshold` to properties that read `Defaults.WF_MIN_THRESHOLD`/`WF_PRIORITY_THRESHOLD` at access time. Preserve subclass-override semantics.
- Convert `PredictionLedgerMixin._pl_*` attributes to a classmethod `_get_pl_setting(name)` reading `Defaults.*` at call time; rebuild `_pl_auto_resolve_errors` per-call if needed.
- Update internal callers (`_check_write_filter`, `_tag_priority`, `_apply_confidence_feedback`) to use the new accessors.

### 2. Extend `apply_overrides` registry for class attrs
- **Task ID**: build-overrides-registry
- **Depends On**: build-mixin-refactor
- **Validates**: `tests/benchmarks/test_overrides_reach.py`
- **Assigned To**: overrides-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `CLASS_ATTR_CONSTANTS` dict mapping WF_* and PL_* names to `(MixinClass, attr_name)` pairs. (No-op after Fix 1 but authoritative and future-proof.)
- In `apply_overrides`, after the existing `Defaults`/module patching, also patch the mixin class attribute if the name is in `CLASS_ATTR_CONSTANTS`. Save original for restore.
- Raise `KeyError` on unknown override names instead of silently ignoring (currently `elif hasattr(Defaults, name.upper())` silently no-ops).

### 3. Write `test_overrides_reach.py`
- **Task ID**: build-test-overrides-reach
- **Depends On**: build-overrides-registry
- **Assigned To**: overrides-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- New file `tests/benchmarks/test_overrides_reach.py`.
- Single parameterized test over all 19 constant names from `TIER1_SWEEPS | TIER2_SWEEPS | TIER3_SWEEPS`.
- For each: enter `apply_overrides({name: sentinel})` where `sentinel` is a valid-range value != default; read the effective value via the production code path (instantiate a minimal model or call the accessor); assert equal to `sentinel`.

### 4. Decouple family-scenario ground truth
- **Task ID**: build-decouple-ground-truth
- **Depends On**: none (can parallelize with task 1)
- **Validates**: `tests/benchmarks/test_factory.py`
- **Informed By**: Research finding on nDCG requiring external ground truth
- **Assigned To**: scenarios-builder
- **Agent Type**: builder
- **Parallel**: true
- In `DecayFamilyScenario`: introduce `self._reference_decay_rate = 0.5` in setup. Compute ground truth with reference, not override. Retrieval continues using override.
- In `ConfidenceFamilyScenario`: split `_outcome_sequences` into observed (5 rounds → ObservationProtocol) and held-out (next 3 rounds → ground truth). Relevance = held-out "acted" fraction.
- Add code comments explaining the decoupling in both scenarios.
- Audit `WriteFilterFamilyScenario` and `CoOccurrenceFamilyScenario`: confirm ground truth is already decoupled. Add explanatory comment.
- Add quick sanity test `tests/benchmarks/test_factory.py::test_decay_family_produces_variance` asserting `decay_rate=0.1` vs `decay_rate=0.9` in `DecayFamilyScenario` yields nDCG difference > 0.05.

### 5. Run Tier 1 sweep and verify variance target
- **Task ID**: run-tier1-sweep
- **Depends On**: build-overrides-registry, build-test-overrides-reach, build-decouple-ground-truth
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --parametric --tier 1`.
- Open the resulting JSON; compute variance per constant.
- If ≥3 of 5 Tier-1 constants show variance > 0.05, proceed. Otherwise iterate on ground-truth decoupling (return to task 4).

### 6. Run full-tier sweep
- **Task ID**: run-full-sweep
- **Depends On**: run-tier1-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --parametric --tier all`.
- Verify at least 5 constants across all tiers show variance > 0.05 (acceptance criterion).
- If not, investigate and extend scenario coverage or return to task 4.

### 7. Apply sweep results to `constants.py`
- **Task ID**: build-apply-defaults
- **Depends On**: run-full-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- For each constant with variance > 0.05: update `Defaults.X = best_value` and add `# best from sweep YYYY-MM-DD, variance=V` comment.
- For each constant with variance ≤ 0.05 after decoupling: add `# empirically inert (sweep YYYY-MM-DD)` comment. Do not remove.
- Commit with message referencing the sweep JSON filename.

### 8. Run full test suite and resolve regressions
- **Task ID**: validate-tests
- **Depends On**: build-apply-defaults
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest` and verify zero regressions.
- If a test breaks due to a changed default, choose: revert that default (document in a comment) OR update the test IF the test was implicitly depending on the old value. Never silently loosen assertions.

### 9. Documentation
- **Task ID**: document-feature
- **Depends On**: build-apply-defaults
- **Assigned To**: scenarios-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Append results sections to `docs/plans/experimental_tuning_magic_numbers.md` and `docs/plans/scenario_code_path_coverage.md`.
- Verify `mkdocs build` passes if guide docs were touched.

### 10. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-tests, document-feature
- **Assigned To**: sweep-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all Verification checks below.
- Confirm all Success Criteria boxes check off.
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest` | exit code 0 |
| Override reach test passes | `pytest tests/benchmarks/test_overrides_reach.py -v` | exit code 0 |
| Family scenarios show variance | `pytest tests/benchmarks/test_factory.py::test_decay_family_produces_variance` | exit code 0 |
| Sweep produces variance > 0.05 | `python -c "import json; d=json.load(open('tests/benchmarks/results/latest.json')); v=[max(vv for _,vv in c['sensitivity_curve'])-min(vv for _,vv in c['sensitivity_curve']) for c in d['constants'].values() if c.get('sensitivity_curve')]; print(sum(1 for x in v if x > 0.05))"` | output >= 5 |
| latest.json valid | `test -e tests/benchmarks/results/latest.json && python -c "import json; json.load(open('tests/benchmarks/results/latest.json'))"` | exit code 0 |
| Lint clean | `black --check src/ tests/` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **Option A vs Option B for mixin refactor:** Property (Option A) gives clean subclass shadowing but changes attribute semantics subtly; classmethod accessor (Option B) is more explicit but requires more caller updates. Preference?
2. **Constants that remain inert after decoupling:** Document in place with `# empirically inert` (plan default), or open a follow-up issue to evaluate removal? The issue text says "documented OR removed" — my read is "document now, remove in a later targeted cleanup." Confirm.
3. **If Tier 2/3 constants cannot be swept in reasonable time:** Acceptable to land defaults updates for Tier 1 only and defer Tiers 2/3 to a follow-up issue, as long as the override-reach test covers all 19?
4. **Sweep reproducibility:** Should we commit the final sweep JSON into the repo alongside the `constants.py` update, or reference it by filename only? Latest sweep JSONs are already checked in under `tests/benchmarks/results/` — precedent suggests yes.
