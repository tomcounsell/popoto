---
status: Planning
type: bug
appetite: Small
owner: dev
created: 2026-07-17
tracking: https://github.com/tomcounsell/popoto/issues/474
last_comment_id:
---

# RetrievalQuality score proxy ignores partition_by (0.0 scores for partitioned models)

## Problem

The shared metacognitive score-proxy helper
`_score_proxy_for_records` in `src/popoto/recipes/context_assembler.py`
reads each record's composite score from the **non-partitioned** sorted-set
index key. Agent-memory models almost always declare `partition_by` on their
sorted fields (e.g. `DecayingSortedField(partition_by="agent_id")`), so the
real scores live in a **partition-specific** ZSET (`$SortF:Model:field:<agent>`),
not the base key (`$SortF:Model:field`). The base key has zero members, every
`ZSCORE` returns `None`, and the helper reports `0.0` for every record.

This silently degrades every metacognitive signal that reads the proxy:
`RetrievalQuality.score_spread`, the FOK subthreshold-activation component, and
`staleness_ratio` — all collapse toward their degenerate value for partitioned
models. `AdaptiveContextAssembler`'s default quality metric consumes
`score_spread`, so retrieval-mode selection is fed a flatlined signal.

**Current behavior:** For a model with `partition_by` on its scored sorted
field, `_score_proxy_for_records(...)` returns `{key: 0.0, ...}` regardless of
the real per-record scores, and `RetrievalQuality.score_spread` is `0.0`.

**Desired outcome:** The shared helper reads the partition-specific ZSET (the
same source `ContextAssembler._injection_scores` already uses for the telemetry
trace), so partitioned models get correct per-record proxy scores and
`score_spread` reflects real score dispersion.

## Freshness Check

**Baseline commit:** `774d274be8a2be3bb9d242b2fc6f6fd7d4438843`
**Issue filed at:** 2026-07-14T06:22:15Z
**Disposition:** Unchanged (one unrelated commit landed since filing)

**File:line references re-verified (against baseline HEAD):**
- `src/popoto/recipes/context_assembler.py:390` `_score_proxy_for_records` — still present; line 419 uses `get_special_use_field_db_key` (the non-partitioned key). Confirmed defect.
- `src/popoto/recipes/context_assembler.py:603` `_staleness_ratio` inline ZSCORE — **also** uses `get_special_use_field_db_key`; same defect, second call site.
- `src/popoto/recipes/context_assembler.py:1174` `ContextAssembler._injection_scores` — the partition-aware reference scorer from PR #473; uses `get_partitioned_sortedset_db_key` (line 1216). Issue's provenance points at `memory_telemetry.py`, but the actual partition-aware scorer lives in `context_assembler.py::_injection_scores` — noted for the builder.

**Cited sibling issues/PRs re-checked:**
- #464 / PR #473 — merged (`7247a41 feat(#464): live-agent memory telemetry`). Introduced `_injection_scores` and deliberately left the shared helper untouched. Landscape unchanged.

**Commits on main since issue was filed (touching referenced files):**
- `774d274 feat(#458): judged-answer harness (Tier 5)` — unrelated; does not touch `context_assembler.py`, the scoring path, or `memory_telemetry.py`.

**Active plans in `docs/plans/` overlapping this area:** none.

**Reproduction (spike, DB 15):** A `Model` with `SortedField(partition_by="agent_id")` and three records at scores `0.2/0.6/0.9` yields:
- `_score_proxy_for_records(...)` → `{'Mem:x:a': 0.0, 'Mem:x:b': 0.0, 'Mem:x:c': 0.0}` (buggy).
- Base key `$SortF:Mem:relevance` → `ZSCORE` `None` for every record.
- Partition key `$SortF:Mem:relevance:x` → `0.2 / 0.6 / 0.9` (correct).
Bug confirmed empirically; it was previously "not yet independently reproduced".

## Prior Art

- **PR #473 (issue #464)** — Live-agent memory telemetry. Added the self-contained partition-aware scorer `ContextAssembler._injection_scores`, used **only** for the telemetry trace, and explicitly scoped around fixing the shared helper because doing so changes `score_spread` behavior and existing `== 0.0` test contracts. This plan is the deferred broad fix.
- No other closed issue/PR attempts to fix `_score_proxy_for_records`. This is a first fix, not a repeat — so no "Why Previous Fixes Failed" section.

## Research

No relevant external findings — this is an internal Popoto/Redis ZSET-keying bug. Proceeding with codebase context. Valkey-safety constraint applies: the fix is read-only pipelined `ZSCORE` only (no Redis modules, no `ZUNIONSTORE`, no temp keys), identical to the existing `_injection_scores` approach.

## Data Flow

1. **Entry point:** `ContextAssembler.assess(...)` (or `AdaptiveContextAssembler` via its quality metric) builds a `RetrievalQuality`.
2. **`_compute_score_spread(records, model_class, score_weights)`** calls **`_score_proxy_for_records`** → currently reads `get_special_use_field_db_key(record, field).redis_key` (non-partitioned) → `ZSCORE` returns `None` for partitioned models → `0.0` scores → `score_spread == 0.0`.
3. Two sibling consumers of the same non-partitioned key:
   - **`_compute_fok(...)`** → `_score_proxy_for_records` for subthreshold-activation fraction.
   - **`_staleness_ratio(...)`** → its own inline `ZSCORE` loop over `get_special_use_field_db_key` (line 603).
4. **Output:** `RetrievalQuality.score_spread` / `.fok_score` / `.staleness_ratio` feed the metacognitive layer and `AdaptiveContextAssembler`'s mode selection.

The fix swaps the key builder at the two non-partitioned call sites (steps 2/3) from `get_special_use_field_db_key` to `get_partitioned_sortedset_db_key`, matching `_injection_scores`.

## Appetite

**Size:** Small

**Team:** Solo dev + validator

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

This is a localized keying fix in one file plus test-contract updates. The only judgment call is which `== 0.0` assertions are legitimate-zero vs. bug-masking (resolved below).

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 15 ping` | Test suite needs a live server on DB 15 |

## Solution

### Key Elements

- **Partition-aware shared proxy**: `_score_proxy_for_records` reads each record's score from the partition-specific ZSET via `get_partitioned_sortedset_db_key(record, field_name)` instead of `get_special_use_field_db_key(record, field_name)`. For non-partitioned fields `partition_by` is an empty tuple, so `get_partitioned_sortedset_db_key` returns the identical base key — non-partitioned models are unaffected.
- **Partition-aware staleness**: `_staleness_ratio`'s inline `ZSCORE` loop (line ~603) switches to the same partitioned key builder.
- **De-duplication (preferred)**: Fold `ContextAssembler._injection_scores` onto the now-partition-aware module helper so the trace scorer and metacognitive scorer share one implementation, eliminating the drift the issue was born from. Keep this refactor conservative — only if it is a clean delegation; otherwise leave `_injection_scores` as-is (it is already correct) and just note the duplication.
- **Test-contract update**: Replace the `score_spread == 0.0` assertions that were masking the bug with assertions reflecting correct partitioned scoring; preserve the assertions where `0.0` is legitimately correct (empty retrieval, no-sorted-field models, fresh `DecayingSortedField` records whose score genuinely decays to 0).

### Flow

`assess()` → `_compute_score_spread` → `_score_proxy_for_records` (now reads partition ZSET) → non-zero per-record scores for partitioned models → `score_spread` reflects real dispersion.

### Technical Approach

- Single-line-of-intent change at each of two call sites: `f.get_special_use_field_db_key(record, field_name)` → `f.get_partitioned_sortedset_db_key(record, field_name)`. Keep the existing `try/except` that queues a `zscore("", "")` placeholder to keep the pipeline plan aligned — `get_partitioned_sortedset_db_key` raises `QueryException` when a partition field value is missing, and the placeholder path already handles that.
- The change is safe for non-partitioned models: `get_partitioned_sortedset_db_key` appends nothing when `partition_by` is empty, yielding the same key as today.
- Numeric constants: none added. The scoring weights already flow through `score_weights`; no new config surface.

### Distinguishing legitimate-zero from bug-masked-zero (critical)

Some existing `== 0.0` assertions are correct and MUST stay; others mask the bug and MUST change. The builder classifies each by asking: *would the partition-specific ZSET hold a non-zero score here?*
- `tests/test_context_assembler.py:802` (`test_...` with `DecayingSortedField(partition_by="agent_id")` on freshly-saved records) — the existing comment (lines 815–819) claims the three proxy scores are "zero-valued ... decayed to 0 because DecayingSortedField starts at 0 score on a freshly saved record." Verify this against a partition-aware proxy: if the fresh decaying score is genuinely 0 in the partition ZSET, `score_spread == 0.0` stays correct **for the right reason**; if the fixture actually has non-zero partition scores, this assertion was bug-masked and must be updated. The new dedicated TDD test (below) removes ambiguity by using non-decaying, non-zero scores.
- `:655`, `:936`, `:954` — models with **no sorted field in `score_weights`** → proxy legitimately `0.0`; keep.
- `:721` — empty retrieval → `0.0`; keep.
- `tests/test_adaptive_assembler.py:87` — a hand-constructed `RetrievalQuality(score_spread=0.0)` literal, not computed; keep.

## Failure Path Test Strategy

### Exception Handling Coverage
- The touched code has `except Exception:` blocks that queue a `zscore("", "")` placeholder (proxy) or count a record as stale (`_staleness_ratio`). These are pipeline-alignment guards, exercised when `get_partitioned_sortedset_db_key` raises `QueryException` for a missing partition value. Add/confirm a test that a record missing its partition field value does not crash the proxy and does not corrupt other records' scores (placeholder keeps the plan aligned).

### Empty/Invalid Input Handling
- `_score_proxy_for_records([], ...)` returns `{}` (existing early return) — keep a test.
- Model with no sorted-field-backed weight returns all-`0.0` — keep existing tests (`:655`, `:936`, `:954`).

### Error State Rendering
- Not user-facing UI. `score_spread` degradation is the "error state"; the new TDD test is the regression guard that it is no longer silently zeroed.

## Test Impact

- [ ] `tests/test_context_assembler.py::<partitioned score_spread test at :802>` — UPDATE (conditionally): re-verify the fixture's partition ZSET scores; update the `score_spread`/`score_distribution` assertions if they were masked zeros, or annotate why they stay 0 if legitimately decayed-to-zero.
- [ ] `tests/test_context_assembler.py` — ADD: new failing-first TDD test with `SortedField(partition_by="agent_id")` and distinct non-zero scores asserting `score_spread > 0` (and that the module helper returns the true per-record scores).
- [ ] `tests/test_context_assembler.py:655,:721,:936,:954` — KEEP: legitimate zeros (empty retrieval / no scored sorted field); do not weaken.
- [ ] `tests/test_adaptive_assembler.py:87` — KEEP: literal `RetrievalQuality`, not affected.
- [ ] `tests/test_adaptive_assembler.py` — REVIEW: confirm `_default_quality_metric` behavior tests still hold; partitioned fixtures (if any) may now produce non-zero `score_spread`.

## Rabbit Holes

- **Rewriting `_injection_scores` scoring semantics.** Only unify the *key-building*; do not change what fields contribute or how RRF/hybrid fused scores are (not) persisted. Per-arm score decomposition remains the documented fast-follow, out of scope.
- **Introducing a new config knob for partitioning.** There is none — `partition_by` is already model metadata read off the field; do not add config surface.
- **Chasing every `== 0.0` in the test suite.** Only the proxy-fed assertions on partitioned models are in scope; the legitimate zeros must be left intact.
- **ZUNIONSTORE / temp-key aggregation for multi-field weights.** Stay read-only pipelined `ZSCORE`, matching the existing helper (Valkey-safe).

## Risks

### Risk 1: Over-correcting a legitimately-zero assertion
**Impact:** Weakening `:655/:721/:936/:954` (true zeros) would delete real coverage.
**Mitigation:** The classification rule above; the builder must justify each changed assertion in the PR. The new TDD test isolates the partitioned-non-zero case so untouched legitimate-zero tests remain the guard for the zero case.

### Risk 2: Silent behavior change for downstream `AdaptiveContextAssembler`
**Impact:** `score_spread` becomes non-zero for partitioned models, which changes `_default_quality_metric` outputs and possibly mode selection — acceptable per issue (beta substrate), but must not surprise callers.
**Mitigation:** Grep all `score_spread` / `_score_proxy_for_records` / `RetrievalQuality` consumers (done: `adaptive_assembler.py`, both test files) and update expectations. Call out the behavior change in the PR body. No non-test production caller asserts `score_spread == 0.0`.

### Risk 3: `QueryException` on records missing a partition value
**Impact:** A record without its partition field set would raise inside the key builder.
**Mitigation:** Existing `try/except` → `zscore("", "")` placeholder already handles it and keeps the pipeline plan aligned; add a test.

## Race Conditions

No race conditions identified — the proxy is a synchronous, read-only pipelined `ZSCORE` batch; no shared mutable state, no cross-process writes.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #464] Per-arm score decomposition for hybrid/lexical fused RRF scores (the trace already documents this as a fast-follow; unchanged here).
- Nothing else deferred — the shared-helper fix, the `_staleness_ratio` fix, the test-contract updates, and consumer coordination are all in scope for this plan.

## Update System

No update-system changes required — this is a purely internal library fix; no deploy/propagation surface.

## Agent Integration

No agent integration required — the change is internal to the `recipes` scoring path; no new tool/MCP surface.

## Documentation

### Inline Documentation
- [ ] Update the `_score_proxy_for_records` docstring to state it reads the partition-specific ZSET (drop the "non-partitioned" framing).
- [ ] Update the `_injection_scores` docstring note (lines 1177–1183) that currently says the metacognitive proxy "would return 0.0" for partitioned models — no longer true once fixed.

### Feature / External Docs
- [ ] No `docs/features/` or MkDocs page documents `_score_proxy_for_records` behavior specifically; a scan confirms no `score_spread == 0.0`-for-partitioned claim to correct. If `/do-docs` finds a metacognitive-signals page, add a one-line note that proxy scores are partition-aware.

## Success Criteria

- [ ] New TDD test: a partitioned `SortedField` model with distinct non-zero scores yields `score_spread > 0` and correct per-record proxy scores — **written first and observed failing** on baseline HEAD.
- [ ] `_score_proxy_for_records` and `_staleness_ratio` read the partition-specific ZSET; non-partitioned models produce identical results to before.
- [ ] All masked `score_spread == 0.0` assertions on partitioned models updated; all legitimate-zero assertions preserved with justification.
- [ ] No new config surface or numeric-constant knobs added.
- [ ] Read-only pipelined `ZSCORE` only — no `ZUNIONSTORE`, temp keys, or Redis modules (Valkey-safe).
- [ ] Fix implemented on a feature branch off `main` (e.g. `fix/474-partition-aware-score-proxy`); no direct push to `main`.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (score-proxy)**
  - Name: `proxy-builder`
  - Role: Write failing TDD test, then make `_score_proxy_for_records` + `_staleness_ratio` partition-aware; update masked test assertions.
  - Agent Type: builder
  - Domain: redis-popoto
  - Resume: true

- **Validator (score-proxy)**
  - Name: `proxy-validator`
  - Role: Verify partitioned scoring correct, legitimate zeros preserved, non-partitioned unchanged, Valkey-safe.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Write failing reproduction test (TDD red)
- **Task ID**: build-tdd-test
- **Depends On**: none
- **Validates**: new test in `tests/test_context_assembler.py`
- **Informed By**: Freshness Check reproduction (shared helper returns 0.0 for partition ZSET at 0.2/0.6/0.9)
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Parallel**: false
- Add a test with a model declaring `SortedField(type=float, partition_by="agent_id")`, three records with distinct non-zero scores under one partition.
- Assert `_score_proxy_for_records(...)` returns the true per-record scores and `assess(...).score_spread > 0`.
- Run it and confirm it **fails** on current HEAD (proxy returns all 0.0). Capture the failure output.

### 2. Make the shared proxy partition-aware (TDD green)
- **Task ID**: build-fix
- **Depends On**: build-tdd-test
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Domain**: redis-popoto
- **Parallel**: false
- In `_score_proxy_for_records` (line ~419) swap `get_special_use_field_db_key(record, field_name)` → `get_partitioned_sortedset_db_key(record, field_name)`, keeping the existing placeholder `try/except`.
- In `_staleness_ratio` (line ~603) apply the same swap.
- Optionally delegate `ContextAssembler._injection_scores` to the now-partition-aware module helper (only if a clean, behavior-preserving delegation).
- Update the two docstrings noted in the Documentation section.
- Run the new test → green.

### 3. Reconcile existing test contracts
- **Task ID**: build-test-contracts
- **Depends On**: build-fix
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Parallel**: false
- Run full `tests/test_context_assembler.py` and `tests/test_adaptive_assembler.py`.
- For each newly-failing `score_spread`/`score_distribution` assertion, apply the classification rule: update if bug-masked, keep + annotate if legitimate zero. Re-verify the `:802` fixture's partition ZSET scores explicitly.
- Grep for any other `score_spread == 0.0` / `_score_proxy_for_records` consumers and update expectations.

### 4. Validation
- **Task ID**: validate-all
- **Depends On**: build-test-contracts
- **Assigned To**: proxy-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm: partitioned scoring correct; non-partitioned identical to before (add/confirm a non-partitioned control test); legitimate zeros preserved; no `ZUNIONSTORE`/temp keys/Redis modules; no new config surface.
- Run the full suite (`scripts/ci-local.sh --fast` or `pytest`) and report pass/fail.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Shared helper no longer uses the non-partitioned key builder | `grep -n "get_special_use_field_db_key" src/popoto/recipes/context_assembler.py \| grep -c .` | `output contains 0` (or only unrelated occurrences — reviewer confirms none in `_score_proxy_for_records`/`_staleness_ratio`) |
| Partition-aware key builder present in scoring path | `grep -c "get_partitioned_sortedset_db_key" src/popoto/recipes/context_assembler.py` | `output > 1` |
| No Redis-module / temp-key aggregation introduced | `grep -rn "ZUNIONSTORE\|zunionstore\|BF\.\|CMS\." src/popoto/recipes/context_assembler.py` | `match count == 0` |
| context_assembler tests pass | `pytest tests/test_context_assembler.py -q` | `exit code 0` |
| adaptive_assembler tests pass | `pytest tests/test_adaptive_assembler.py -q` | `exit code 0` |

## Open Questions

1. **De-duplication scope:** Should `_injection_scores` be folded onto the shared helper now (removes the drift that caused this bug), or left independent (smaller diff, but the two partition-aware scorers can drift again)? Plan currently says "only if a clean delegation." Preference?
2. **The `:802` legitimate-zero fixture:** If re-verification shows the fresh `DecayingSortedField` fixture genuinely has zero partition scores, `score_spread == 0.0` stays there. Acceptable to keep that assertion (documented) rather than force the fixture to non-zero?
