---
status: docs_complete
type: chore
appetite: Medium
owner: Valor Engels
created: 2026-04-17
tracking: https://github.com/tomcounsell/popoto/issues/351
last_comment_id:
revision_applied: true
revision_date: 2026-04-17
docs_complete_date: 2026-04-17
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
- **Decoupled family ground truth**: Each of the 4 family scenarios gets its ground truth rewritten so that relevance is determined by a signal *orthogonal to* the one the retriever consumes, not a scaled copy of it (per critique B1/B2/C1):
  - `DecayFamilyScenario` (B1 fix — oracle must not be a power-law of the same inputs): ground truth = oracle rank computed as `importance * log1p(age_days)` (logarithmic, not a power law). Retrieval continues to use `importance * age^(-decay_rate)` via `DecayingSortedField`. Because log-growth and power-law-decay are structurally different, an override of `decay_rate=0.1` vs `0.9` will actually diverge from the oracle ordering instead of producing symmetric permutations that score identically. Alternative (if log still correlates too strongly in testing): `rng.shuffle(list(range(n)))` seeded with a fixed salt independent of the constant being swept.
  - `ConfidenceFamilyScenario` (C4 fix — held-out split must be concretely specified): generate **8-round** outcome sequences per record using the same per-tier distribution rule currently used for 5-round sequences. Feed `seq[:5]` to `ObservationProtocol` in the 5-round observation loop (unchanged). In `run()`, compute `relevance_scores[key] = mean(seq[5:8] == "acted")`. Ground truth `relevant_ids` = top 30% by this held-out acted-rate. The retriever's confidence state (based on observed rounds 0–4) is evaluated on its ability to predict future (rounds 5–7) acted-rate.
  - `WriteFilterFamilyScenario` (C1 fix — NOT already decoupled, contrary to prior plan wording): in `setup()`, add an orthogonal urgency field `self._gt_urgency = {idx: rng.random() for idx in range(n)}`. Ground truth = top-K by urgency **for records not filtered by `wf_min`**. Retrieval still filters by importance. As the override threshold rises, high-urgency-low-importance records get filtered out, dropping nDCG. This isolates "filter removes valuable-by-orthogonal-signal records" from "retrieval favors what survived."
  - `CoOccurrenceFamilyScenario` (B2 fix — NOT already decoupled; hop-depth strictly orders retrieval regardless of `decay_per_hop`): add a small number of "noise" hop2 records with direct seed↔hop2 links weighted at `initial_weight * 0.3` (the standard seed↔hop1 weight is `initial_weight`). Retrieval ordering of hop1 vs hop2-noise now depends on whether `decay_per_hop^1 * initial_weight` exceeds `decay_per_hop^0 * (initial_weight * 0.3)` — i.e., depends on `decay_per_hop` itself. Ground truth continues to rank hop1 (topological cluster membership) above hop2-noise, independent of the propagation math.
- **Family-weighted sweep**: Run `run_sweeps.py --parametric --tier all` with a reduced generic-scenario count so family scenarios are not drowned. Alternatively add a `--family-only` flag.
- **Evidence-driven defaults**: For each constant with nDCG variance > 0.05, update `Defaults.*` to the best-scoring value. For constants with variance ≤ 0.05 after decoupled ground truth, add a one-line comment `# empirically inert (sweep YYYY-MM-DD)` next to the default. **Commit the final sweep JSON into `tests/benchmarks/results/` alongside the `constants.py` update, per existing precedent** (prior sweeps are already checked in).

### Flow

Current `constants.py` defaults → `/do-build` lands override fixes + decoupled ground truth → fresh `--parametric` sweep run → analyst (PM) reviews best values → `/do-build` commits updated defaults with sweep JSON as evidence → test suite still green → merge.

### Technical Approach

- **Fix 1 — Make mixin reads override-reachable.** Two viable options, pick one per mixin:
  - *Option A (preferred for WriteFilterMixin):* Change `_wf_min_threshold` from a class attribute to a `@property` that returns `Defaults.WF_MIN_THRESHOLD`, with a per-instance override stored as `self._wf_min_threshold_override`. Subclasses can still override by shadowing the property with a class attribute.
  - *Option B (preferred for PredictionLedgerMixin):* Keep class attributes but make them lazy — use a `classmethod` accessor `_get_pl_setting(name)` that reads from `Defaults` at call time. The existing `_pl_auto_resolve_errors` dict is rebuilt per-call. The mixin's private callers already use `getattr(instance, '_pl_confidence_error_threshold', 0.7)`, so they adapt naturally.
  - **Validation:** Write `tests/benchmarks/test_overrides_reach.py::test_all_constants_patchable` that iterates all 19 constant names, enters `apply_overrides({name: sentinel_value})`, reads the effective value via the production code path, and asserts it equals `sentinel_value`.
- **Fix 2 — Extend `overrides.py` registry.** Add `CLASS_ATTR_CONSTANTS` parallel to `MODULE_CONSTANTS` that maps constants to the mixin classes needing class-attr patches. `apply_overrides()` saves and restores class attributes for these. After Fix 1, this may be a no-op for the refactored mixin, but it is a belt-and-suspenders guard and makes the registry authoritative.
- **Fix 3 — Decouple ground truth.** In `family_factory.py`, each scenario's `run()` computes `relevance_scores` from a signal *structurally orthogonal* to the one the retriever consumes (not a scaled copy). The critique verified that the prior "reference constant" approach produces symmetric permutations that score identically — the oracle must not be a power-law of the same inputs:
  - `DecayFamilyScenario` (replaces B1-circular design): In `setup()`, after age assignment, compute `self._oracle_ranks` using `importance * log1p(age_days)` — a logarithmic growth function of age, structurally different from the power-law decay `age^(-decay_rate)` the retrieval uses. Ground truth = top-K by oracle rank. Retrieval continues using `DecayingSortedField` with the overridden `decay_rate`. An override of `0.1` vs `0.9` now diverges from the oracle rather than producing symmetric inversions. Fallback if `log1p(age)` still correlates too strongly with `age^(-decay)` in sanity tests: use `rng.shuffle(list(range(n)))` seeded with a fixed salt independent of the swept constant.
  - `ConfidenceFamilyScenario` (replaces C4-underspecified split): Modify the `seq = ["acted"] * 5` lines in `ConfidenceFamilyScenario.setup()` to produce **8-length sequences** using the same per-tier distribution rule (same RNG, same distribution, just longer). Feed `seq[:5]` to `ObservationProtocol` during the 5-round observation loop — the retriever only sees observed rounds. In `run()`, compute `relevance_scores[key] = mean(seq[5:8] == "acted")` — this is what a well-calibrated confidence state *should* predict. Ground truth `relevant_ids` = top 30% by this held-out acted-rate. Add a code comment explaining: retriever trains on rounds 0–4, ground truth is rounds 5–7.
  - `WriteFilterFamilyScenario` (fixes C1 — was NOT decoupled, contrary to prior plan text): In `setup()`, add `self._gt_urgency = {idx: rng.random() for idx in range(n)}` — an orthogonal scalar uncorrelated with `importance`. Ground truth = top-K by urgency **restricted to records not filtered by `wf_min`** (i.e., whose importance ≥ the threshold at sweep time). Retrieval continues to filter by importance and score by composite. As the override threshold rises, high-urgency-low-importance records get filtered out, dropping nDCG. This isolates "filter removes valuable-by-orthogonal-signal records" from "retrieval favors what survived."
  - `CoOccurrenceFamilyScenario` (fixes B2 — was NOT decoupled; hop-depth strictly orders retrieval): Add a small number of "noise" hop2 records with **direct seed↔hop2 links** weighted at `initial_weight * 0.3` (where the standard seed↔hop1 weight is `initial_weight`). After this change, retrieval ordering of hop1 vs hop2-noise depends on whether `decay_per_hop^1 * initial_weight` exceeds `decay_per_hop^0 * (initial_weight * 0.3)` — i.e., depends on `decay_per_hop` itself. At low `decay_per_hop` the hop2-noise outscores hop1 (direct link wins); at high `decay_per_hop` hop1 wins. Ground truth continues to rank hop1 (topological cluster membership) above hop2-noise, independent of the propagation math. Fixes the B2 circularity.
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

- `tests/benchmarks/test_overrides_reach.py` — CREATE: new test file covering:
  - `test_all_constants_patchable` — parameterized over all 19 constants; asserts effective value inside `apply_overrides()` equals the sentinel via the production code path.
  - `test_unknown_name_raises_keyerror` (C3 transition test) — asserts `apply_overrides({'DOES_NOT_EXIST': 0.5})` raises `KeyError` (or logs a deprecation warning if the migration period is still active — see task 2).
  - `test_all_registered_names_dont_raise` — asserts every name in `MODULE_CONSTANTS ∪ CLASS_ATTR_CONSTANTS ∪ Defaults.*` registry does NOT trigger the unknown-name path.
- `tests/benchmarks/test_factory.py` — INSPECT + UPDATE: ground-truth decoupling changes scenario internals. Add `test_decay_family_produces_variance` (B1 sanity test): asserts nDCG at `decay_rate=0.1` vs `decay_rate=0.9` in `DecayFamilyScenario` differs by > 0.05. This test is designed to FAIL under the prior `age^(-0.5)` oracle design and to PASS under the `log1p(age)` oracle.
- `tests/benchmarks/test_sweep.py` — INSPECT: may need UPDATE if new `CLASS_ATTR_CONSTANTS` branch is added to `apply_overrides`.
- `tests/test_write_filter.py` (C5 perf + subclass guard tests):
  - `test_subclass_class_attr_wins_over_property` — covers existing subclass override pattern (`tests/test_write_filter.py:41-42` sets `_wf_min_threshold = 0.5` on a subclass). Property in parent; class attribute in subclass must still win via `__getattribute__` subclass-dict-first lookup.
  - `test_save_perf_post_property` — saves 1000 records and asserts total wall-clock time < 1.5x the baseline (record baseline with `git stash` + `pytest --benchmark-only` before the refactor). If regression exceeds 5%, cache resolved value on the instance (`self._wf_min_threshold_resolved`) and have the property return the cached value, with a classmethod to invalidate on `Defaults` patch.
- `tests/test_prediction_ledger.py` (if exists) — INSPECT: same subclass-wins guard for PL_* constants if Option B (classmethod accessor) is used; the classmethod itself is not a descriptor, so the subclass override pattern is different — document in the test.

No deletions expected. All changes are additive or refactor-safe.

## Rabbit Holes

- **Redesigning the override system from scratch.** Tempting given how fragile the dual-patch mechanism is, but out of scope. The minimal Fix 2 addition (class-attr registry) is enough.
- **Benchmarking retrieval latency as part of this work.** The sweep already records `duration_ms`, but tuning for latency is a separate optimization effort.
- **Adding a `--family-only` flag + CI wiring.** Nice-to-have but not required to unblock the issue. Default to running `--parametric` with reduced generic-scenario count.
- **Revising the `Defaults` class structure (e.g., to frozen dataclass, Pydantic model).** Defer. The current plain class works once reads are done at runtime.
- **Adjusting the metrics in `tests/benchmarks/metrics/`.** Explicitly forbidden by issue constraints and by this plan — metrics are the ruler, not the thing being measured.

## Risks

### Risk 1: Ground-truth decoupling makes scenarios pathologically easy or hard
**Impact:** An oracle signal that still correlates strongly with the retrieval signal (even if not identical to it) could produce nDCG values clustered at 1.0 or 0.0 regardless of the override, defeating the sensitivity signal a second time. The critique specifically flagged the prior `age^(-reference)` oracle as failing this way — symmetric power-law swings around the reference produce identical nDCG at override extremes.
**Mitigation:** Fix 3 now uses structurally orthogonal oracles (log-growth vs power-decay, urgency vs importance, held-out future vs observed past, cross-cluster noise links). Before running the full sweep, run the task-4 sanity tests (`test_decay_family_produces_variance`, `test_cooccurrence_noise_links_break_circularity`, etc.) to confirm each family produces variance > 0.05 between override extremes. If any sanity test fails, iterate on that family's oracle before spending compute on the full sweep. If `log1p(age)` still correlates too strongly in `DecayFamilyScenario`, fall back to the `rng.shuffle` oracle noted in task 4.

### Risk 2: Option A (property) subclass override + per-call performance overhead on `save()`
**Impact:** Two sub-risks identified by critique C5:
- (a) Subclass override pattern: existing code at `tests/test_write_filter.py:41-42` sets `_wf_min_threshold = 0.5` on a subclass. A plain class attribute in the subclass **does** correctly shadow a parent property — Python's attribute lookup uses subclass-dict-first in `__getattribute__`, so `subclass._wf_min_threshold` resolves to the plain value before the parent property is consulted. (The prior plan text incorrectly described MRO descriptor evaluation order — corrected here.) The subclass-wins path is safe but requires an explicit regression test since relying on it was implicit before.
- (b) Performance: converting `_wf_min_threshold` from a cached class attribute to a property introduces a per-call `Defaults.WF_MIN_THRESHOLD` lookup on the `save()` hot path (`write_filter.py:115` reads `self._wf_min_threshold`). Uncached property access on a hot loop can regress throughput.

**Mitigation:**
- Add `tests/test_write_filter.py::test_subclass_class_attr_wins_over_property` that extends `WriteFilterMixin` with a class-level `_wf_min_threshold = 0.3` and asserts reads return 0.3 even when `Defaults.WF_MIN_THRESHOLD` is patched to 0.7.
- Add `tests/test_write_filter.py::test_save_perf_post_property` that saves 1000 records and asserts total time < 1.5x the baseline. Record baseline with `git stash` + `pytest --benchmark-only` (or a manual `time.perf_counter()` loop) **before** the refactor. If the guard fails, cache the resolved value on the instance (`self._wf_min_threshold_resolved = Defaults.WF_MIN_THRESHOLD` at `__init__`); have the property return the cached value; expose a classmethod `invalidate_wf_cache()` to be called from `apply_overrides()` when `Defaults` changes.

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
- [x] Update `docs/plans/experimental_tuning_magic_numbers.md` with a trailing "Results applied" section linking to this plan and the sweep JSON. *(Done in PR #361 — see "Results Applied (2026-04-17)" section citing `sweep_20260417_141047.json`.)*
- [x] Update `docs/plans/scenario_code_path_coverage.md` with a trailing note on which scenarios had ground-truth decoupling applied. *(Done in PR #361 — see "Ground-Truth Decoupling Applied (2026-04-17)" section covering the B1/B2/C1/C4 fixes.)*

### External Documentation Site
- [x] If `docs/guides/` has a file on constants or tuning, add a brief note that defaults are now empirically tuned (source: `sweep_YYYYMMDD_HHMMSS.json`). *(Done in PR #361 — `docs/guides/tuning-magic-numbers.md` now shows the new `DECAY_RATE=0.1` and `_wf_min_threshold=0.1` defaults with "(sweep 2026-04-17)" annotations.)*
- [x] Cascade new defaults to user-facing reference docs. *(Added in the docs stage: `docs/features/decaying-sorted-field.md`, `docs/features/cyclic-decay-field.md`, `docs/features/agent-memory.md`, `docs/fields.md`, `docs/api-reference.md`, `docs/guides/agent-memory-quickstart.md`, `docs/guides/subconscious-memory-recipe.md`, and the `popoto-memory-roadmap.md` catalog all now reflect the new defaults. Code examples that explicitly pass `decay_rate=0.5` carry an inline comment noting the override.)*

### Inline Documentation
- [x] Code comment on each updated `Defaults.*` line recording the sweep date and variance for auditability. *(Done in PR #361 — see `src/popoto/fields/constants.py`: `DECAY_RATE = 0.1  # best from sweep 2026-04-17, variance=0.067, prior=0.5` and `WF_MIN_THRESHOLD = 0.1  # best from sweep 2026-04-17, variance=0.068, prior=0.2`.)*
- [x] Code comment on each `# empirically inert` constant with the sweep date. *(Done in PR #361 — 22 inert constants annotated `# empirically inert (sweep 2026-04-17, variance=0.0)` in `constants.py`.)*
- [x] Docstring on `apply_overrides` noting the `CLASS_ATTR_CONSTANTS` mechanism (if added in Fix 2). *(Done in PR #361 — see `tests/benchmarks/overrides.py` module docstring describing the triple-patch strategy and `apply_overrides` docstring explaining the `MODULE_CONSTANTS` + `CLASS_ATTR_CONSTANTS` + kwargs channels, including the unknown-name warning path.)*

## Success Criteria

- [ ] `tests/benchmarks/test_overrides_reach.py::test_all_constants_patchable` passes for all 19 constants.
- [ ] `WriteFilterMixin` and `PredictionLedgerMixin` reads of `Defaults.*` happen at method-call / attribute-access time, not class-definition time.
- [ ] Family-factory scenarios define ground truth from a signal distinct from the retrieval signal. Each scenario has a code comment explaining which signal drives ground truth and which drives retrieval.
- [ ] A `--parametric` sweep produces nDCG@5 variance > 0.05 for at least 5 constants.
- [ ] `src/popoto/fields/constants.py` has best-value updates for every sensitive constant, each annotated with the sweep date.
- [ ] Inert constants have inline `# empirically inert (sweep YYYY-MM-DD)` comments.
- [ ] `latest.json` symlink points to the newest sweep JSON (should already; verify).
- [ ] `pytest` passes with no regressions after constant changes.
- [x] Tests pass (`/do-test`). *(Full suite 1510 passed, 14 skipped; override-reach 30 passed; family ground-truth decoupling 4 passed — verified on PR #361.)*
- [x] Documentation updated (`/do-docs`). *(Plan doc cross-links, inline constant comments, `apply_overrides` docstring, and user-facing reference doc cascade all shipped in PR #361 docs stage.)*

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
- **Baseline the hot path FIRST** (before any refactor): record `save()` throughput for `WriteFilterMixin`-using models by running `pytest --benchmark-only` or a manual 1000-record timing loop on current main. Note the wall-clock number; this is the pass/fail threshold for the C5 perf guard test.
- Convert `WriteFilterMixin._wf_min_threshold` and `_wf_priority_threshold` to properties that read `Defaults.WF_MIN_THRESHOLD`/`WF_PRIORITY_THRESHOLD` at access time. Preserve subclass-override semantics — a plain subclass class attribute (e.g. `_wf_min_threshold = 0.3`) must still shadow the property via `__getattribute__` subclass-dict-first lookup.
- Convert `PredictionLedgerMixin._pl_*` attributes to a classmethod `_get_pl_setting(name)` reading `Defaults.*` at call time; rebuild `_pl_auto_resolve_errors` per-call if needed.
- Update internal callers (`_check_write_filter`, `_tag_priority`, `_apply_confidence_feedback`) to use the new accessors.
- Add `tests/test_write_filter.py::test_subclass_class_attr_wins_over_property` — extends `WriteFilterMixin` with a class-level `_wf_min_threshold = 0.3`; asserts reads return 0.3 even when `Defaults.WF_MIN_THRESHOLD` is patched to 0.7.
- Add `tests/test_write_filter.py::test_save_perf_post_property` — saves 1000 records and asserts total time < 1.5x the recorded baseline. If the guard fails: cache resolved value on the instance (`self._wf_min_threshold_resolved` at `__init__`), have the property return it, and add a classmethod `WriteFilterMixin.invalidate_wf_cache()` called from `apply_overrides()` on patch/restore.

### 2. Extend `apply_overrides` registry for class attrs
- **Task ID**: build-overrides-registry
- **Depends On**: build-mixin-refactor
- **Validates**: `tests/benchmarks/test_overrides_reach.py`
- **Assigned To**: overrides-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `CLASS_ATTR_CONSTANTS` dict mapping WF_* and PL_* names to `(MixinClass, attr_name)` pairs. (No-op after Fix 1 but authoritative and future-proof.)
- In `apply_overrides`, after the existing `Defaults`/module patching, also patch the mixin class attribute if the name is in `CLASS_ATTR_CONSTANTS`. Save original for restore.
- **Handle unknown override names safely (C3 migration path):** FIRST grep `tests/benchmarks/ratchet.py`, `tests/benchmarks/sweep.py`, and `tests/benchmarks/split.py` for every `apply_overrides({...})` call site. Confirm none of them construct override names dynamically (from data, config files, or computed strings) that could miss the registry. If all call sites pass statically-known names, raise `KeyError` on unknown names directly. If ANY call site builds names dynamically, issue a `logger.warning("unknown override name %r — will raise in next release", name)` for this release and defer the hard error to a follow-up ticket. Document the decision in a code comment on the raise/warn branch.
- Add tests to `tests/benchmarks/test_overrides_reach.py`:
  - `test_unknown_name_raises_keyerror` — asserts the chosen behavior (raise OR log-warn) matches the decision above.
  - `test_all_registered_names_dont_raise` — iterates every name in `MODULE_CONSTANTS ∪ CLASS_ATTR_CONSTANTS ∪ Defaults.*`-derived names and confirms none fall into the unknown-name branch.

### 3. Write `test_overrides_reach.py`
- **Task ID**: build-test-overrides-reach
- **Depends On**: build-overrides-registry
- **Assigned To**: overrides-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- New file `tests/benchmarks/test_overrides_reach.py` with three tests:
  - `test_all_constants_patchable` — parameterized over all 19 constant names from `TIER1_SWEEPS | TIER2_SWEEPS | TIER3_SWEEPS`. For each: enter `apply_overrides({name: sentinel})` where `sentinel` is a valid-range value != default; read the effective value via the production code path (instantiate a minimal model or call the accessor); assert equal to `sentinel`.
  - `test_unknown_name_raises_keyerror` — asserts the behavior chosen in task 2 (raise `KeyError` OR emit `logger.warning` during the migration window). Document the expected behavior in the test docstring so the migration period is auditable.
  - `test_all_registered_names_dont_raise` — iterates every name in `MODULE_CONSTANTS`, `CLASS_ATTR_CONSTANTS`, and every uppercase attribute on `Defaults`; confirms none fall into the unknown-name branch. Guards against future registry drift.

### 4. Decouple family-scenario ground truth
- **Task ID**: build-decouple-ground-truth
- **Depends On**: none (can parallelize with task 1)
- **Validates**: `tests/benchmarks/test_factory.py`
- **Informed By**: Research finding on nDCG requiring external ground truth; critique findings B1, B2, C1, C4
- **Assigned To**: scenarios-builder
- **Agent Type**: builder
- **Parallel**: true
- **`DecayFamilyScenario` (B1 fix):** In `setup()`, after age assignment, compute `self._oracle_ranks` using `importance * log1p(age_days)` — a logarithmic growth function, structurally different from the power-law `age^(-decay_rate)` retrieval uses. Ground truth top-K = top-K records by `_oracle_ranks`. Retrieval continues via `DecayingSortedField` with the overridden `decay_rate`. Add a code comment: "Oracle uses log-growth of age; retrieval uses power-law decay — the two are not symmetric permutations of each other, so decay_rate variation changes nDCG." If the sanity test (below) fails under the log oracle, fall back to `self._oracle_ranks = rng.shuffle(list(range(n)))` seeded with a fixed salt independent of any swept constant.
- **`ConfidenceFamilyScenario` (C4 fix):** Modify the `seq = ["acted"] * 5` lines in `setup()` to produce **8-length sequences** drawn from the same per-tier distribution. Feed `seq[:5]` to `ObservationProtocol` in the observation loop (rounds 0–4). In `run()`, compute `relevance_scores[key] = mean(seq[5:8] == "acted")`. Ground truth `relevant_ids` = top 30% by this held-out acted-rate. Add a code comment: "Retriever sees observed rounds 0–4 via ObservationProtocol; ground truth is held-out future behavior rounds 5–7. A well-calibrated confidence state predicts the future acted-rate."
- **`WriteFilterFamilyScenario` (C1 fix — was NOT decoupled, contrary to prior plan wording):** In `setup()`, add `self._gt_urgency = {idx: rng.random() for idx in range(n)}`. Ground truth = top-K by urgency **restricted to records whose importance ≥ the current `wf_min` threshold** (i.e., records that survived the write filter). Retrieval continues via composite score driven by importance. Add a code comment: "Urgency is orthogonal to importance; filter removes high-urgency-low-importance records as threshold rises, which drops nDCG."
- **`CoOccurrenceFamilyScenario` (B2 fix — was NOT decoupled):** Add a small number of noise hop2 records with direct seed↔hop2 links weighted at `initial_weight * 0.3` (vs the standard `initial_weight` for seed↔hop1). Retrieval ordering of hop1 vs hop2-noise now depends on `decay_per_hop` (compares `decay_per_hop^1 * initial_weight` vs `decay_per_hop^0 * initial_weight * 0.3`). Ground truth continues to rank hop1 (cluster membership) above hop2-noise. Add a code comment: "Cross-cluster noise links make retrieval ordering depend on decay_per_hop value, not just topology."
- **Sanity tests in `tests/benchmarks/test_factory.py`:**
  - `test_decay_family_produces_variance` (B1 guard) — asserts `DecayFamilyScenario` nDCG at `decay_rate=0.1` vs `decay_rate=0.9` differs by > 0.05. This test is designed to FAIL under the prior `age^(-0.5)` oracle and to PASS under the `log1p(age)` oracle.
  - `test_confidence_family_held_out_split` — asserts `_outcome_sequences` have length 8; `ObservationProtocol` only sees `seq[:5]`; `relevance_scores` derived from `seq[5:8]`.
  - `test_write_filter_family_urgency_orthogonal` — asserts `_gt_urgency` has near-zero Pearson correlation (|r| < 0.2) with `importance` across 100 records.
  - `test_cooccurrence_noise_links_break_circularity` — asserts at least some hop2-noise records exist in the scenario graph and that `decay_per_hop=0.1` vs `0.9` yield nDCG difference > 0.05.

### 5. Run Tier 1 sweep and verify variance target
- **Task ID**: run-tier1-sweep
- **Depends On**: build-overrides-registry, build-test-overrides-reach, build-decouple-ground-truth
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --parametric --tier 1`.
- Open the resulting JSON; compute variance per constant.
- **Variance gate (C2 fix):** If ≥4 of 5 Tier-1 constants show variance > 0.05, proceed to task 6. Otherwise return to task 4, cap at **2 iterations total** before filing a follow-up issue to escalate. This gate is tightened from the prior "3 of 5" to keep Tier 1 in-line with the global "≥5 total constants" acceptance criterion.

### 6. Run full-tier sweep
- **Task ID**: run-full-sweep
- **Depends On**: run-tier1-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_sweeps --parametric --tier all`.
- Verify at least 5 constants across all tiers show variance > 0.05 (acceptance criterion).
- **Rollback gate (C2 fix):** If fewer than 5 constants total (across all tiers) show variance > 0.05, STOP and return to task 4. Escalate to PM after **2 iterations total** of the task 4 → task 5 → task 6 loop (combined count — iterations at task 5 count toward the same budget). Filing a follow-up issue for remaining inert constants is acceptable once the budget is exhausted.

### 7. Apply sweep results to `constants.py`
- **Task ID**: build-apply-defaults
- **Depends On**: run-full-sweep
- **Assigned To**: sweep-runner
- **Agent Type**: builder
- **Parallel**: false
- For each constant with variance > 0.05: update `Defaults.X = best_value` and add `# best from sweep YYYY-MM-DD, variance=V` comment.
- For each constant with variance ≤ 0.05 after decoupling: add `# empirically inert (sweep YYYY-MM-DD)` comment. Do not remove.
- **Update docstrings in place (N1 fix):** For every `Defaults.X` value change, also update any docstring that cites that default in `src/popoto/fields/write_filter.py` (module docstring at `write_filter.py:10-11` cites WF_MIN_THRESHOLD/WF_PRIORITY_THRESHOLD) and `src/popoto/fields/prediction_ledger.py` (cites PL_* defaults). Grep `src/popoto/fields/*.py` for literal old values before committing; any match in a docstring or comment must be updated to the new value.
- **Commit the final sweep JSON (N2-aligned):** Move/copy the final sweep result file into `tests/benchmarks/results/` alongside the `constants.py` update, per existing precedent. Include the sweep JSON filename in the commit message.

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

**Critics:** Skeptic, Operator, Archaeologist, Adversary, Simplifier, User
**Findings:** 9 total (2 blockers, 5 concerns, 2 nits)
**Date:** 2026-04-17

### Blockers

#### B1. `DecayFamilyScenario` ground-truth decoupling is still circular
- **Critics:** Skeptic, Adversary
- **Location:** Technical Approach / Fix 3 bullet 1; Solution bullet on DecayFamilyScenario
- **Finding:** The plan proposes ground truth = `importance * age^(-reference_decay_rate=0.5)` while retrieval uses overridden `decay_rate`. But `importance` values are clustered at 0.45-0.55 (per `family_factory.py:122-123`), so the ordering is dominated by `age^(-decay_rate)`. When the overridden `decay_rate` equals the reference (0.5 is a swept value in `TIER1_SWEEPS`), nDCG must equal 1.0 by construction, and at 0.1 vs 0.9 the ordering of a bimodal (recent/old) age distribution is *exactly inverted but symmetric*, producing identical nDCG against both extremes. The "decoupling" only adds a scalar constant; it does not decouple the signal.
- **Suggestion:** Ground truth should NOT be derived from any power-law of the same inputs. Options: (a) pre-score with an orthogonal function (e.g., a learned permutation fixed per-seed); (b) use an external ranking from a held-out "oracle" decay rate chosen to be OUTSIDE the swept range so that no override matches; (c) score against "recent half vs old half" as binary relevance independent of exact decay math.
- **Implementation Note:** In `DecayFamilyScenario.setup()`, after age assignment, compute `self._oracle_ranks = [...]` using `rng.shuffle(list(range(n)))` seeded once at class-load with a fixed salt independent of the constant being swept, OR use `importance * log1p(age_days)` (not a power law) as the oracle. Retrieval continues to use `importance * age^(-decay_rate)` via `DecayingSortedField`. For the sanity test in task 4, assert that nDCG at `decay_rate=0.1` vs `decay_rate=0.9` differs by > 0.05 — this test will FAIL with the currently-proposed `age^(-0.5)` oracle because a symmetric swing around the reference produces the same nDCG at 0.1 and 0.9.

#### B2. `CoOccurrenceFamilyScenario` ground truth is already circular; plan says "verify and document"
- **Critics:** Skeptic, Simplifier
- **Location:** Technical Approach / Fix 3 bullet 4; Solution Key Elements bullet 3
- **Finding:** Per `family_factory.py:700-767`, retrieval calls `self._assoc_field.propagate(..., decay_per_hop=decay_per_hop)` and uses the resulting scores as `co_occurrence_boost` in `composite_score`. Ground truth assigns `hop1=1.0, hop2=0.5`. But the propagate() result is strictly hop-depth ordered: hop1 records will always score higher than hop2 records regardless of `decay_per_hop` value (0.1 vs 0.9 just changes the magnitude of the gap). So retrieval will ALWAYS rank hop1 before hop2 before unlinked — matching ground truth — giving nDCG ≈ 1.0 for any `decay_per_hop`. Plan claims this is already decoupled; it is not.
- **Suggestion:** Introduce at least one cross-cluster link (e.g., 1-2 seed↔hop2 direct links) or vary initial_weight per link so that only certain `decay_per_hop` values produce hop1-dominant ordering. OR: define ground truth from an independent seed cluster assignment that does not match the link topology.
- **Implementation Note:** Add a few "noise" hop2 records with direct seed links weighted at `initial_weight * 0.3`. Now retrieval ordering of hop1 vs hop2 depends on whether `decay_per_hop^1 * initial_weight` (standard hop1) exceeds `decay_per_hop^0 * (initial_weight * 0.3)` (noise hop2) — i.e., depends on `decay_per_hop` itself. Ground truth continues to rank hop1 (topological) above hop2-noise. Fixes the circularity.

### Concerns

#### C1. `WriteFilterFamilyScenario` ground truth is NOT decoupled and plan mis-classifies it
- **Critics:** Skeptic, Adversary
- **Location:** Solution Key Elements / WriteFilterFamilyScenario bullet; Fix 3 bullet 3
- **Finding:** Per `family_factory.py:527-545`, ground truth is "top 30% by importance from the full intended set" while retrieval uses `composite_score(relevance=0.4, certainty=0.6)`. `relevance` is `DecayingSortedField(base_score_field="importance")` — so retrieval is ALSO driven by importance (plus confidence boost). The "decoupling" the plan claims is just "retrieve from surviving set, compare against planned set" — but since WF_MIN_THRESHOLD filters by importance and ground truth ranks by importance, the filter monotonically removes the low end of both. Variance comes from survivors + confidence boost interaction, not from a truly independent signal.
- **Suggestion:** Define ground truth as a signal that does NOT correlate with importance. Options: (a) an explicit `self._ground_truth_ids` set chosen by `rng.sample()` independent of importance; (b) use a separate "urgency" field distinct from "importance" and rank ground truth by urgency while retrieval filters by importance.
- **Implementation Note:** In `WriteFilterFamilyScenario.setup()`, add `self._gt_urgency = {idx: rng.random() for idx in range(n)}`. Ground truth: top-K by urgency for records not filtered by `wf_min`. As threshold rises, high-urgency-low-importance records get filtered, dropping nDCG. This isolates "filter removes valuable-by-orthogonal-signal records" from "retrieval favors what survived."

#### C2. Success criterion "variance > 0.05 for ≥5 constants" vs task 5 gate "≥3 of 5 Tier-1"
- **Critics:** Skeptic
- **Location:** Step-by-step task 5 bullet 3 ("If ≥3 of 5 Tier-1 constants show variance > 0.05, proceed") vs Success Criteria line 4 ("≥5 constants")
- **Finding:** Tier 1 has exactly 5 constants. Task 5 gate says 3/5 is enough to proceed to Tier 2; Success Criteria requires 5 total across all tiers. The gate is weaker than the acceptance criterion, creating a path where Tier 1 shows 3 variant constants, Tier 2/3 show 0-1 each, total = 3-4 < 5, and task 6 "verify at least 5 constants" fails AFTER significant compute has been spent. No rollback to "iterate on ground truth" is specified at task 6 — only task 5 mentions iteration back to task 4.
- **Suggestion:** Either (a) tighten task 5 gate to "all 5 Tier-1 constants show variance > 0.05", or (b) explicitly allow task 6 to loop back to task 4 if the cumulative variance-passing count is below 5, with a bounded iteration count (e.g., max 2 re-design cycles before opening a follow-up issue).
- **Implementation Note:** Replace task 5 bullet 3 with: "If ≥4 of 5 Tier-1 constants show variance > 0.05, proceed. Otherwise return to task 4, cap at 2 iterations total before filing a follow-up issue." Add to task 6: "If fewer than 5 constants total (across all tiers) show variance > 0.05, STOP and return to task 4. Escalate to PM after 2 iterations."

#### C3. Task 2 raises `KeyError` on unknown overrides — breaking change with no migration
- **Critics:** Operator, Adversary
- **Location:** Step-by-step task 2 bullet 3
- **Finding:** Current behavior (`overrides.py:152`) silently no-ops on unknown overrides via `elif hasattr(Defaults, name.upper())`. Task 2 changes this to raise KeyError. This is a breaking behavior change that will crash any existing caller passing an unexpected name (including typos in future sweep configs). The ratchet loop (`ratchet.py`) and parametric sweeps may construct override names from data. No test is specified to catch callers that currently depend on the silent-ignore behavior.
- **Suggestion:** Before raising, log the current silent-no-op behavior with `logger.warning` for one release cycle. Alternatively, issue a deprecation warning now and convert to error later. Add a test that covers a known-misspelled name to document the transition behavior.
- **Implementation Note:** Task 2 step should be "Raise `KeyError` — but FIRST grep `tests/benchmarks/ratchet.py`, `tests/benchmarks/sweep.py`, `tests/benchmarks/split.py` for any `apply_overrides({...})` call site to confirm none pass dynamically-named keys that could miss the registry. If any dynamic callers exist, log-warn for one release and defer the hard error." Add to Test Impact: `tests/benchmarks/test_overrides_reach.py::test_unknown_name_raises_keyerror` and `test_overrides_reach.py::test_all_registered_names_dont_raise`.

#### C4. `ConfidenceFamilyScenario` split into observed/held-out is underspecified
- **Critics:** Adversary, User
- **Location:** Technical Approach / Fix 3 bullet 2
- **Finding:** Plan says "split `_outcome_sequences` into observed (5 rounds) and held-out (next 3 rounds)." But current `_outcome_sequences` (family_factory.py:278-295) is hard-coded to 5 entries per record. No guidance on (a) how to generate the 3 held-out rounds (same deterministic schedule? fresh RNG?), (b) whether held-out outcomes should come from a different distribution than observed ones (otherwise the "future" is just more of the same and doesn't test confidence calibration), (c) how to score relevance from the held-out fraction ("acted"-dominant — but using what threshold?).
- **Suggestion:** Be concrete: generate 8-round sequences with the same distribution-per-tier rule; fed rounds 0-4 to ObservationProtocol; compute ground truth as `sum(rounds[5:8].count("acted")) / 3 ≥ 0.5 → relevant`. OR: keep observed rounds from the same distribution but draw held-out rounds with added noise so "calibrated confidence" is what actually predicts future acted-rate.
- **Implementation Note:** Modify the `seq = ["acted"] * 5` lines in `ConfidenceFamilyScenario.setup()` to produce 8-length sequences. Feed `seq[:5]` to ObservationProtocol in the 5-round loop. In `run()`, compute `relevance_scores[key] = mean(seq[5:8] == "acted")` — this is what the retriever's confidence state should predict. Ground truth `relevant_ids` = top 30% by this held-out acted-rate. Cite this in the code comment per Fix 3 bullet.

#### C5. Option A (property) subtly breaks existing subclass override pattern
- **Critics:** Adversary, Skeptic
- **Location:** Technical Approach / Fix 1 Option A; Risk 2 mitigation
- **Finding:** Risk 2 claims "Python's MRO evaluates descriptors first, so a subclass class attribute does shadow a parent property." This is backwards — a property in a parent class that is NOT shadowed with another property will correctly be overridden by a subclass CLASS ATTRIBUTE (because the subclass dict wins in `__getattribute__` lookup). However, the existing test pattern (`tests/test_write_filter.py:41-42`) sets `_wf_min_threshold = 0.5` on a subclass — which WILL work. But if the subclass INHERITS without overriding, reads go through the property — and the property returns `Defaults.WF_MIN_THRESHOLD`. Existing code at `write_filter.py:115` reads `self._wf_min_threshold`. After Fix 1, reads become a method call, introducing a per-call lookup overhead on the hot `save()` path. This should be measured.
- **Suggestion:** Add a micro-benchmark: time `save()` with and without the property conversion across 10k calls. If regression > 5%, cache the resolved value on the instance (`self._wf_min_threshold_resolved = Defaults.WF_MIN_THRESHOLD` at `__init__`) and have the property return that, with a classmethod to invalidate caches when `Defaults` changes.
- **Implementation Note:** Task 1 should explicitly add a performance guard test: `tests/test_write_filter.py::test_save_perf_post_property` that saves 1000 records and asserts total time < 1.5x the current baseline (record baseline with `git stash` + `pytest --benchmark-only` before refactor). Also add `tests/test_write_filter.py::test_subclass_class_attr_wins_over_property` covering the existing subclass override pattern.

### Nits

#### N1. "Defaults of 0.5, 0.2, 0.7" in Solution don't match `constants.py`
- **Location:** Solution Key Elements
- **Finding:** The plan repeatedly cites `WF_MIN_THRESHOLD = 0.2` and `WF_PRIORITY_THRESHOLD = 0.7`. Verified correct. But the docstring in `write_filter.py:10-11` references the same defaults. When applying sweep results, make sure the docstring in `write_filter.py` is also updated, not just `constants.py`.
- **Suggestion:** Add to task 7 bullet: "Also update docstrings in `write_filter.py` and `prediction_ledger.py` that cite default values."

#### N2. Open Question #4 should be resolved, not left open
- **Location:** Open Questions (bottom)
- **Finding:** Precedent is clearly established (sweeps ARE committed under `tests/benchmarks/results/`). The question "commit the sweep JSON or reference by filename" wastes a PM cycle to re-confirm established behavior.
- **Suggestion:** Remove Open Question 4 and move to Solution: "Commit final sweep JSON alongside the constants update, per existing precedent."

### Structural Check Results

| Check | Status | Detail |
|-------|--------|--------|
| Required sections (Documentation, Update System, Agent Integration, Test Impact) | PASS | All four present, non-empty |
| Task numbering | PASS | 1-10 contiguous |
| Dependencies valid | PASS | All Depends On references point to valid task IDs (build-mixin-refactor, build-overrides-registry, etc.) |
| File paths exist | PASS | All referenced paths exist: `write_filter.py`, `prediction_ledger.py`, `constants.py`, `overrides.py`, `family_factory.py`, `test_write_filter.py`, `test_prediction_ledger.py`. Only `test_overrides_reach.py` is intentionally new. |
| Prerequisites met | PASS | Redis on localhost:6379 is the only prereq |
| Cross-references | PASS | All Success Criteria map to at least one task; no No-Gos appear in Solution as planned work |

### Verdict

**READY TO BUILD (with concerns)** — No catastrophic blockers that prevent build start, but B1 and B2 need the Implementation Notes embedded before coding or the fresh sweep will repeat the "flat sensitivity" failure mode (the same failure this plan is meant to fix). Recommend running a `/do-plan` revision pass to embed the Implementation Notes for B1, B2, C1, C3, C4, C5 directly into the Technical Approach section before `/do-build`. After revision, proceed to build.

---

## Open Questions

1. **Option A vs Option B for mixin refactor:** Property (Option A) gives clean subclass shadowing but changes attribute semantics subtly; classmethod accessor (Option B) is more explicit but requires more caller updates. Preference?
2. **Constants that remain inert after decoupling:** Document in place with `# empirically inert` (plan default), or open a follow-up issue to evaluate removal? The issue text says "documented OR removed" — my read is "document now, remove in a later targeted cleanup." Confirm.
3. **If Tier 2/3 constants cannot be swept in reasonable time:** Acceptable to land defaults updates for Tier 1 only and defer Tiers 2/3 to a follow-up issue, as long as the override-reach test covers all 19?

*(Q4 about sweep JSON commit was removed during the 2026-04-17 revision pass — resolved inline in Solution / Key Elements and task 7: commit the final sweep JSON alongside the `constants.py` update per existing precedent.)*
