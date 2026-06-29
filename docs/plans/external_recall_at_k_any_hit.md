---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-06-29
tracking: https://github.com/tomcounsell/popoto/issues/433
last_comment_id:
---

# External benchmark Recall@k: any-hit hit-rate (not fractional)

## Problem

The external-benchmark harness (`tests/benchmarks/`) scores Popoto Agent Memory
retrieval against published academic datasets. The committed LongMemEval-S
baseline reports an aggregate that is **mathematically impossible** under the
any-hit recall definition the report claims to compute and compare against:

| Metric | Reported | Tell |
|--------|----------|------|
| Recall@1 | 0.5492 | |
| Recall@5 | 0.8883 | |
| Recall@10 | 0.9367 | |
| MRR | **0.8987** | `MRR > R@5` is impossible under any-hit recall |

**Current behavior:** `recall_at_k` returns **fractional** recall — `hits /
len(relevant)` (`tests/benchmarks/metrics/retrieval.py:132`). LongMemEval-S has
**multi-evidence** ground truth (`|relevant|` distribution among rank-1 hits is
`{1:147, 2:222, 3:32, 4:13, 5:11, 6:3}`), so a top-1 hit on a 2-evidence
question scores `recall@1 = 1/2 = 0.5`, never 1.0 — deflating the aggregate.
The report's embedded reference (`agentmemory ... Recall@5: 95.2%`,
`run_external.py:267`) and the `recall_at_k` docstring ("equivalent to a
hit-rate") both assume the **any-hit** definition. `mean_reciprocal_rank` is
correct; only the recall normalization is wrong.

**Desired outcome:** Headline `Recall@k` reports the **any-hit hit-rate**
(`1.0` if ≥1 relevant item in top-k, else `0.0`), restoring the `MRR ≤ R@k`
invariant and matching the external reference. Corrected values:
`R@1=0.856 / R@5=0.952 / R@10=0.978 / MRR=0.899` (R@5 matches the reference
95.2% to the digit). The committed report artifact is regenerated.

## Freshness Check

**Baseline commit:** `272ff30d9b31d69bdfea4e11e931217d0c67c784`
**Issue filed at:** 2026-06-29T08:26:57Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `tests/benchmarks/metrics/retrieval.py:129-133` — `return min(1.0, hits / len(relevant))` — still holds verbatim.
- `tests/benchmarks/metrics/retrieval.py:136-149` — `mean_reciprocal_rank` correct — still holds.
- `tests/benchmarks/run_external.py:133-136` — same `retrieved_ids` feeds both metrics — still holds (now at lines ~133-136; confirmed `r1/r5/r10/mrr` block present).
- `tests/benchmarks/datasets/longmemeval_s.py:166-168` — `relevant_ids` built from a list of `answer_session_ids` — still holds.
- `tests/benchmarks/run_external.py:265-268` — embedded agentmemory reference (`Recall@5: 95.2%`) — still holds.

**Cited sibling issues/PRs re-checked:**
- #436 — OPEN, CLEAN/MERGEABLE — LongMemEval-S adapter real-schema fix; **prerequisite for report regeneration** (regen must run against the corrected adapter). See Prerequisites.
- #395 — referenced in report Notes as the hybrid-retrieval follow-up (now tracked by #437).

**Commits on main since issue was filed (touching referenced files):** none
(`git log --since=2026-06-29T08:26:57Z -- retrieval.py run_external.py` is empty).

**Active plans in `docs/plans/` overlapping this area:** `bm25_first_class_retrieval_mode.md`
touches retrieval scoring but not the metric functions — no overlap with `recall_at_k`.

**Notes:** Code matches the issue exactly; no drift.

## Prior Art

- **PR #250** — "Experimental tuning benchmark harness for agent-memory constants": established the `tests/benchmarks/` harness and `metrics/retrieval.py`. Introduced the fractional `recall_at_k` with the misleading "equivalent to a hit-rate" docstring. This is the origin of the defect.
- **PR #436** (open) — LongMemEval-S adapter real-schema fix. Sibling fix on the same dataset; corrects which sessions are retrieved, not how recall is scored. Prerequisite for regeneration.
- No prior attempt to fix the recall normalization itself.

## Research

No relevant external findings — proceeding with codebase context. The any-hit
vs. fractional recall distinction is standard IR terminology; the issue already
derives and verifies the correct any-hit values against the artifact, and the
external reference (agentmemory) numbers are any-hit hit-rates by construction.

## Data Flow

1. **Entry point**: `python -m tests.benchmarks.run_external --dataset longmemeval-s`.
2. **Adapter** (`datasets/longmemeval_s.py`): builds each `BenchmarkItem` with `relevant_ids` = set of evidence session IDs (often 2-6 members).
3. **Scenario run** (`run_external.py`): produces `retrieved_ids` (ordered, BM25 score-only).
4. **Metrics** (`metrics/retrieval.py`): `recall_at_k(retrieved_ids, relevant_ids, k)` ← **defect here** (fractional, not any-hit); `mean_reciprocal_rank(...)` correct. Both read the same `retrieved_ids`.
5. **Aggregation + report** (`run_external.py`): averages per-question metrics, writes `results/external/longmemeval_s_<date>.json/.md`, embeds the agentmemory any-hit reference.
6. **Output**: committed report artifact with the misleading recall numbers.

The fix is applied at layer 4 (the metric function). Layers 2-3 are correct.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (issue is crisply specified with a verified solution sketch)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test suite needs Redis (DB 15) |
| LongMemEval-S dataset cached locally | `python -c "import pathlib,glob; assert glob.glob('data/**/longmemeval*', recursive=True) or True"` | Report regeneration re-runs the 500-question benchmark |
| PR #436 merged to main | `gh pr view 436 --json state -q .state` → `MERGED` | Regeneration must use the corrected adapter (see No-Gos / ORDERED) |

The metric fix + unit tests do **not** depend on #436. Only the **report
regeneration** step does. If #436 has not merged when this builds, ship the
code+tests and regenerate the report as a follow-up commit once #436 lands.

## Solution

### Key Elements

- **`recall_at_k` → any-hit**: returns `1.0` if any relevant item is in top-k, else `0.0`. Docstring updated to state it is a hit-rate unconditionally.
- **`fractional_recall_at_k` (retained)**: the previous `hits / len(relevant)` behavior moved to a separate, clearly named function (sanctioned by the issue's AC clause). Not wired into the headline report. Preserves the tested multi-evidence math for future per-evidence analysis.
- **Invariant guard test**: `MRR(retrieved, relevant) ≤ recall_at_k(retrieved, relevant, k)` for any single result.
- **Multi-evidence test**: `|relevant| > 1` with a rank-1 hit ⇒ `recall_at_1 == 1.0`.
- **Regenerated artifact**: `longmemeval_s_20260629.json/.md` re-run after the fix; `latest` symlinks repointed.

### Flow

`recall_at_k(retrieved, relevant, k)` → count hits in top-k → return `1.0 if hits > 0 else 0.0`.
Edge cases (`k<=0`, empty `retrieved`, empty `relevant`) → `0.0` (unchanged).

### Technical Approach

- Edit `tests/benchmarks/metrics/retrieval.py:129-133`:
  ```python
  if k <= 0 or not retrieved or not relevant:
      return 0.0
  hits = sum(1 for item in retrieved[:k] if item in relevant)
  return 1.0 if hits > 0 else 0.0
  ```
  Update the docstring to describe an any-hit hit-rate unconditionally (drop the
  "for a single relevant item ... equivalent to a hit-rate" hedge).
- Add `fractional_recall_at_k(retrieved, relevant, k)` with the old body and a
  docstring naming it explicitly as fractional recall.
- No change to `run_external.py` call site — it already calls `recall_at_k`; the
  semantics change underneath it.
- Regenerate the report by re-running the harness (deterministic BM25 score-only
  retrieval), then commit the new `.json`/`.md` and repoint `*_latest` symlinks.

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope — `recall_at_k` is pure arithmetic with explicit edge-case guards (no `try/except`).

### Empty/Invalid Input Handling
- Empty `retrieved`, empty `relevant`, `k<=0`, negative `k` → `0.0`. Already covered by existing tests (`test_empty_retrieved`, `test_empty_relevant`, `test_k_zero`, `test_k_negative`); these remain valid under any-hit and must continue to pass.

### Error State Rendering
- No user-visible rendering path beyond the report artifact. The regenerated `.md` is verified by asserting `MRR ≤ R@5` and `R@5 ≈ 0.952` post-regeneration.

## Test Impact

- [ ] `tests/benchmarks/test_external.py::TestRecallAtK::test_multiple_relevant_partial` — **REPLACE**: under any-hit, `recall_at_k(["a","x","y"], {"a","b"}, k=3)` is `1.0` (one hit), not `0.5`. Rewrite to assert any-hit `== 1.0`, and move the `== 0.5` assertion to a new `fractional_recall_at_k` test.
- [ ] `tests/benchmarks/test_external.py::TestRecallAtK::test_multiple_relevant_all_found` — **UPDATE**: still `1.0` under any-hit (both relevant found). Passes unchanged but re-confirm intent.
- [ ] `tests/benchmarks/test_external.py::TestRecallAtK::test_caps_at_1_for_single_relevant` — **UPDATE**: `recall_at_k(["a","b","c"], {"a"}, k=10) == 1.0` still holds; keep as a sanity check.
- [ ] All other `TestRecallAtK` cases (`test_hit_in_top1`, `test_miss_*`, empty/`k` edge cases) — **no change**: single-relevant and miss cases are identical under both definitions.
- [ ] Add new tests: invariant guard (`MRR ≤ R@k`), multi-evidence rank-1 hit (`recall_at_1 == 1.0`), and `fractional_recall_at_k` partial case.

## Rabbit Holes

- **Re-deriving numbers from the existing artifact instead of re-running.** Tempting (the issue derives any-hit from MRR), but the correct artifact comes from a real harness run, not arithmetic on the old file. Re-run.
- **Reworking `mean_reciprocal_rank`.** It is correct; do not touch it.
- **Wiring `fractional_recall_at_k` into the report as a second column.** Out of scope; the headline is any-hit only. Adding columns is a reporting-format change for a separate issue.

## Risks

### Risk 1: Report regeneration blocked on #436
**Impact:** If #436 hasn't merged, regenerating now uses the old (fictional-schema) adapter, producing wrong retrieved_ids.
**Mitigation:** Gate regeneration on #436 merge (Prerequisites + ORDERED No-Go). Ship code+tests independently; regenerate as a follow-up commit if needed.

### Risk 2: Dataset not cached locally on the build machine
**Impact:** Regeneration can't run without the LongMemEval-S haystack.
**Mitigation:** The 285KB `longmemeval_s_20260629.json` confirms a prior local run; dataset is present. If absent at build time, defer regeneration and flag it, but still land the metric fix + tests.

## Race Conditions

No race conditions identified — `recall_at_k` is a pure, synchronous, single-threaded function.

## No-Gos (Out of Scope)

- [ORDERED] Report regeneration must wait for PR #436 (LongMemEval-S adapter fix) to merge so the regenerated retrieved_ids use the corrected adapter. **Decision:** merge #436 first, then build #433 off updated main and regenerate the report **in this PR** (not a follow-up).
- [SEPARATE-SLUG #434] LoCoMo adapter uses the same metric path and will inherit the any-hit fix automatically, but its adapter is broken (0 items) and is tracked separately. No LoCoMo regeneration here.
- [SEPARATE-SLUG #437] Hybrid BM25+vector retrieval mode (the apples-to-apples comparison against the agentmemory reference) is a separate enhancement.

## Update System

No update system changes required — this is a test-harness-internal metric fix.

## Agent Integration

No agent integration required — `recall_at_k` is a benchmark metric, not an agent-facing capability.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` entry — this is a metric correction, not a user feature.

### External Documentation Site
- [ ] No docs-site pages reference `recall_at_k`. Confirm with `grep -rn "recall_at_k\|Recall@" docs/` — update only if a page cites the old numbers.

### Inline Documentation
- [ ] Update `recall_at_k` docstring to describe any-hit hit-rate unconditionally.
- [ ] Add docstring to `fractional_recall_at_k` naming it explicitly as fractional recall.
- [ ] Update `tests/benchmarks/README.md` if it documents the recall definition.

## Success Criteria

- [ ] `recall_at_k` returns any-hit hit-rate (0.0/1.0); docstring matches.
- [ ] `fractional_recall_at_k` exists as a separate, clearly named function with its own test.
- [ ] Unit test enforces `MRR ≤ recall_at_k` invariant.
- [ ] Unit test covers multi-evidence rank-1 hit ⇒ `recall_at_1 == 1.0`.
- [ ] LongMemEval-S report regenerated (or follow-up committed post-#436); `R@5 ≈ 0.952`, `MRR ≤ R@5` holds in the artifact.
- [ ] All existing `TestRecallAtK` cases pass (with `test_multiple_relevant_partial` replaced).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

Small fix — solo builder + validator.

### Team Members

- **Builder (metric-fix)**
  - Name: metric-builder
  - Role: Edit `recall_at_k`, add `fractional_recall_at_k`, update + add tests, regenerate report if #436 merged.
  - Agent Type: builder
  - Resume: true

- **Validator (metric-fix)**
  - Name: metric-validator
  - Role: Verify any-hit semantics, invariant test, multi-evidence test, and report invariant.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Convert recall_at_k to any-hit + add fractional variant
- **Task ID**: build-metric
- **Depends On**: none
- **Validates**: tests/benchmarks/test_external.py::TestRecallAtK
- **Assigned To**: metric-builder
- **Agent Type**: builder
- **Parallel**: false
- Edit `tests/benchmarks/metrics/retrieval.py` `recall_at_k` to return `1.0 if hits>0 else 0.0`; rewrite docstring.
- Add `fractional_recall_at_k` with the old `min(1.0, hits/len(relevant))` body and an explicit docstring.

### 2. Update and add tests
- **Task ID**: build-tests
- **Depends On**: build-metric
- **Validates**: tests/benchmarks/test_external.py
- **Assigned To**: metric-builder
- **Agent Type**: builder
- **Parallel**: false
- Replace `test_multiple_relevant_partial` to assert any-hit `== 1.0`; move the `0.5` assertion into a new `fractional_recall_at_k` test.
- Add `test_mrr_le_recall_invariant` and `test_multi_evidence_rank1_hit` (`recall_at_k(["a","x"], {"a","b"}, 1) == 1.0`).

### 3. Regenerate LongMemEval-S report (gated on #436)
- **Task ID**: build-report
- **Depends On**: build-tests
- **Assigned To**: metric-builder
- **Agent Type**: builder
- **Parallel**: false
- #436 merges first (decided), so build #433 off updated main: run `python -m tests.benchmarks.run_external --dataset longmemeval-s`, commit the new `.json`/`.md`, repoint `*_latest` symlinks to `longmemeval_s_20260629`.
- Verify the regenerated artifact has `R@5 ≈ 0.952` and `MRR ≤ R@5`.

### 4. Validation
- **Task ID**: validate-all
- **Depends On**: build-metric, build-tests, build-report
- **Assigned To**: metric-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/benchmarks/test_external.py -q`.
- Verify regenerated artifact (if produced) has `R@5 ≈ 0.952` and `MRR ≤ R@5`.
- Confirm success criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Recall metric tests pass | `pytest tests/benchmarks/test_external.py::TestRecallAtK -q` | exit code 0 |
| Any-hit semantics in code | `grep -n "1.0 if hits" tests/benchmarks/metrics/retrieval.py` | exit code 0 |
| Fractional variant exists | `grep -n "def fractional_recall_at_k" tests/benchmarks/metrics/retrieval.py` | exit code 0 |
| Invariant test present | `grep -rn "mrr_le_recall\|MRR.*recall" tests/benchmarks/test_external.py` | exit code 0 |
| No fractional in headline metric | `grep -n "hits / len(relevant)" tests/benchmarks/metrics/retrieval.py` | output > 0 |
| Format clean | `black --check tests/benchmarks/metrics/retrieval.py tests/benchmarks/test_external.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Resolved Decisions

1. **Retain `fractional_recall_at_k`** as a separate, clearly named, tested function (confirmed 2026-06-29). Headline `recall_at_k` is any-hit; fractional math preserved for future per-evidence analysis.
2. **Merge #436 first, regenerate in this PR** (confirmed 2026-06-29). Build #433 off updated main after #436 lands; the corrected report artifact ships within this PR, not a follow-up.
