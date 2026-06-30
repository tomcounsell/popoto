---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-06-30
tracking: https://github.com/tomcounsell/popoto/issues/435
last_comment_id:
---

# External benchmark: representative `--limit` sampling + `question_type` in per-question reports

## Problem

The external-benchmark harness (`tests/benchmarks/`) scores Popoto Agent Memory
retrieval against LongMemEval-S (500 QA items, grouped on disk into 6
`question_type` blocks of differing difficulty) and LoCoMo. Two harness-quality
defects make limited runs misleading and the artifacts un-sliceable. They live
in the same files, both require a benchmark re-run, and are tracked together.

**Current behavior:**

- **Problem A — `--limit N` benchmarks only the easiest category.**
  `datasets/longmemeval_s.py` `iter_items` (lines 220-230) enumerates records in
  **file order** and `break`s once `count >= limit` — a pure contiguous prefix.
  LongMemEval-S is grouped by `question_type` on disk: the **first 70 records are
  all `single-session-user`** (the easiest single-evidence category); hard
  categories (`temporal-reasoning`, `knowledge-update`) are not reached until
  index ~295+. So `--limit 20` gave **Recall@1 = 1.0** while the full 500-question
  run gave **R@1 = 0.856** (post-#433). Any `--limit < ~70` benchmarks exclusively
  one easy category — a "quick smoke run" reports near-perfect numbers that do not
  reflect real performance. There is no `--shuffle`/`--seed`/`--stratified` flag
  and no `random` import anywhere in the harness (verified). LoCoMo
  (`datasets/locomo.py` `iter_items`, lines 264-277) shares the flaw — also a
  contiguous prefix.

- **Problem B — `question_type` is dropped before the per-question report.** The
  adapters correctly place `question_type` into `BenchmarkItem.metadata`
  (`longmemeval_s.py:181`; `locomo.py:220`). The **drop point** is
  `scenarios/external_base.py:286-295`: `ScenarioResult.metadata` is rebuilt as a
  fresh dict that copies `item_id`, `query`, `n_history_turns`, `n_saved_records`,
  `n_retrieved`, `retrieval_ms`, `dataset`, `retrieval_method` — but **never
  `question_type`**. `run_external.py` faithfully serializes whatever the scenario
  put in, so the field is already gone. Verified: 0 of 500 per-question records in
  the committed `longmemeval_s_20260629.json` carry `question_type`. A per-category
  breakdown — **the** key diagnostic dimension (e.g. a BM25 baseline scores
  single-session-assistant R@1≈1.00 vs multi-session R@1≈0.35) — can only be
  produced by re-joining `item_id` to the raw dataset. The artifact is un-sliceable.

**Desired outcome:**

- Limited runs are **representative of the whole dataset by default**, seeded for
  reproducibility, and self-describing (the chosen sample mode + seed are recorded
  in the report). Both datasets are covered by one mechanism.
- Every per-question result record (both datasets) carries its `question_type`, so
  reports can be sliced by category without re-joining the raw dataset, and the
  summary gains a per-category breakdown.
- Committed artifacts are regenerated to reflect the new sampling and schema.

## Freshness Check

**Baseline commit:** `13d21d04691d8611efcd890c9c2efe8807b4b6e0`
**Issue filed at:** 2026-06-29T08:27:18Z
**Disposition:** Unchanged

**File:line references re-verified (against current main):**
- `tests/benchmarks/datasets/longmemeval_s.py:220-230` — `iter_items` enumerates `data` in file order, `if limit is not None and count >= limit: break` — still holds verbatim (contiguous-prefix cut).
- `tests/benchmarks/datasets/longmemeval_s.py:181` — `"question_type": record.get("question_type", "")` placed into `BenchmarkItem.metadata` — still holds.
- `tests/benchmarks/datasets/locomo.py:264-277` — `iter_items` is a contiguous prefix over dialogues/QA pairs — still holds.
- `tests/benchmarks/datasets/locomo.py:220` — `"question_type": qa.get("type", "")` in metadata — still holds.
- `tests/benchmarks/scenarios/external_base.py:286-295` — `ScenarioResult.metadata` rebuilt without `question_type` (copies `dataset` via `item.metadata.get("dataset", ...)` but not `question_type`/`answer`) — still holds verbatim.
- `tests/benchmarks/run_external.py:343-348` — `--limit` arg definition — still holds. `run_external.py:381,384` — `limit=args.limit` pass-through to both adapters — still holds. No `--shuffle`/`--seed`/`--stratified` flag; no `import random` in `datasets/` or `run_external.py` (confirmed).

**Cited sibling issues/PRs re-checked:**
- #436 — **MERGED** (LongMemEval-S adapter real-schema fix). The longmemeval adapter on current main is the corrected real-schema version; line numbers above are post-#436.
- #434 — **OPEN** (LoCoMo adapter written against fictional schema, yields 0 items, discards ground truth). Rewrites `datasets/locomo.py` substantially. **Ordering prerequisite** — see No-Gos / Risks.
- #437 — **OPEN** (hybrid BM25+vector mode). Adds flags to `run_external.py` and may touch `compute_aggregate`/scenario metadata. **Merge-conflict surface** — see Risks.

**Commits on main since issue was filed (touching referenced files):** none on the four affected files beyond the already-merged #436 (which the line refs above already reflect).

**Active plans in `docs/plans/` overlapping this area:**
- `external_recall_at_k_any_hit.md` (#433, Complete) — same harness, fixed `recall_at_k` semantics; no overlap with sampling or report schema. Its No-Gos already point `--limit`/category concerns here.
- `external_benchmark_harness.md` (#394) — origin harness plan; this is a follow-on quality fix.

**Notes:** Code matches the issue exactly; no drift. All four cited line ranges are accurate against `13d21d0`.

## Prior Art

- **#394 / `external_benchmark_harness.md`** — built the harness, adapters, scenario layer, and `run_external.py` CLI. Introduced the contiguous-prefix `--limit` and the metadata-rebuild drop site that this plan fixes.
- **#433 / `external_recall_at_k_any_hit.md`** (Complete) — corrected `recall_at_k` to any-hit; regenerated `longmemeval_s_20260629.*`. Establishes the "fix + regenerate the committed artifact in the same PR" pattern this plan follows.
- **#436** (Merged) — real-schema LongMemEval-S adapter; the `question_type` metadata key this plan threads is a product of that fix.
- No prior attempt to add representative sampling or to thread `question_type` into the report.

## Data Flow

1. **Entry point:** `python -m tests.benchmarks.run_external --dataset longmemeval-s --limit N [--sample MODE --seed S]`.
2. **CLI** (`run_external.py`): parses `--limit` and (new) `--sample`/`--seed`, passes them to the adapter's `iter_items`.
3. **Adapter** (`datasets/longmemeval_s.py` / `locomo.py`): loads the full `data`, parses records into `BenchmarkItem`s, then (new) applies a **shared sampler** that selects the subset *before* returning. `BenchmarkItem.metadata` carries `question_type`. ← **Problem A fix here.**
4. **Scenario run** (`scenarios/external_base.py`): builds `ScenarioResult.metadata`. ← **Problem B fix here** (add `question_type` to the rebuilt dict).
5. **Per-question result** (`run_external.py` `QuestionResult`): copies `scenario_result.metadata` verbatim into the JSON record — `question_type` now rides along.
6. **Aggregation + report** (`run_external.py` `compute_aggregate` / `build_markdown_report`): (new) per-`question_type` breakdown block + markdown table; records sample mode + seed in the artifact.
7. **Output:** regenerated `results/external/{dataset}_{date}.{json,md}` + `*_latest` symlinks.

The fixes are isolated to layers 3 (sampling) and 4 (one metadata line); layers 5-6 gain additive reporting.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (two Open Questions need a default-policy decision: sample-default and stride-vs-stratified)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test suite needs Redis (DB 15) |
| #434 merged to main | `gh issue view 434 --json state -q .state` → `CLOSED` | LoCoMo sampling must layer on the corrected adapter (see No-Gos / ORDERED) |
| LongMemEval-S dataset cached | `python -c "import pathlib; assert (pathlib.Path.home()/'.cache'/'popoto_benchmarks'/'longmemeval_s_cleaned.json').exists()"` | LongMemEval-S report regeneration re-runs the 500-question benchmark |

The sampling logic + unit tests (driven by fixtures / synthetic item lists) do
**not** depend on #434. Only the **LoCoMo report regeneration** and the
LoCoMo-adapter sampling wiring depend on #434's corrected adapter shape.

## Solution

### Key Elements

- **Shared sampler** (`datasets/sampling.py`, new): one function
  `sample_items(items, limit, mode, seed)` operating on an already-parsed
  `list[BenchmarkItem]`, returning the selected subset. Both adapters call it, so
  LongMemEval-S and LoCoMo are covered by one mechanism. Modes:
  - `head` — current contiguous prefix (`items[:limit]`); retained only for
    reproducing legacy runs. Emits a one-line WARNING when combined with `--limit`.
  - `stride` — deterministic even sampling across the full list:
    indices `round(i * N / limit)` for `i in range(limit)`. Spans the whole
    dataset, needs no RNG, fully reproducible. **Proposed default when `--limit`
    is set.**
  - `shuffle` — `random.Random(seed).sample(range(N), limit)`; seeded.
  - `stratified` — group by `metadata["question_type"]`, apportion `limit`
    across groups proportionally (largest-remainder so counts sum to exactly
    `limit`), pick within each group by seeded shuffle. Guarantees every category
    is represented in small runs.
- **One-line Problem-B fix** in `external_base.py` metadata dict:
  `"question_type": self.item.metadata.get("question_type", "")`. Same drop site
  serves both datasets.
- **Per-category breakdown:** `compute_aggregate` groups `ok` results by
  `question_type` and emits per-category R@1/R@5/R@10/MRR/count; `build_markdown_report`
  renders a "By question_type" table.
- **Self-describing artifacts:** the chosen `sample_mode`, `seed`, and `limit` are
  recorded in `aggregate` (and the markdown header) so a run is reproducible from
  its own report.
- **Regenerated artifacts:** `longmemeval_s_*` regenerated unconditionally;
  `locomo_*` regenerated after #434 lands.

### Flow

`run_external --limit 12 --sample stratified --seed 0`
→ adapter parses all 500 items → `sample_items(items, 12, "stratified", 0)`
selects ~2 per category across all 6 → scenario tags each result with
`question_type` → report shows headline metrics **and** a per-category table,
with `sample_mode=stratified seed=0 limit=12` in the header.

### Technical Approach

- **New `tests/benchmarks/datasets/sampling.py`** with `sample_items(items, limit, mode, seed) -> list[BenchmarkItem]` and the four modes above. Pure function over a list — no I/O, trivially unit-testable with synthetic items carrying varied `question_type`. `limit is None` or `limit >= len(items)` returns all items unchanged. Modes that need RNG seed a local `random.Random(seed)` (never the global RNG).
- **Adapters materialize then sample.** In each `iter_items`, parse the full `data` into a list (the longmemeval loop already parses one record at a time; collect into a list instead of streaming), then `return sample_items(parsed, limit, mode, seed)`. The `limit` cut moves out of the per-record loop into the sampler. Callers already do `for item in items`, so returning a list is compatible. Add `sample: str = "head"`, `seed: int = 0` params to both `iter_items` signatures (default `head` preserves byte-for-byte legacy behavior for existing callers/tests that pass neither).
- **LoCoMo nesting:** LoCoMo yields multiple QA `BenchmarkItem`s per dialogue. Parse **all** dialogues into the full flat item list first, then sample — this makes stride/stratified span the whole corpus rather than a prefix of dialogues. (Wire this against #434's corrected `_parse_dialogue`/`iter_items`, not current main's 0-item version.)
- **CLI flags** in `run_external.py`: `--sample {head,stride,shuffle,stratified}` (default per Open Question 1) and `--seed INT` (default 0). Thread both into the `iter_*` calls at lines ~381/384. Log the resolved mode/seed at run start.
- **Problem B:** add the single `question_type` line to the `external_base.py:286-295` metadata dict. `QuestionResult.to_dict()` already passes `metadata` through unchanged, so JSON carries it with no further edit.
- **Per-category aggregation** in `compute_aggregate`: build `by_question_type` = `{qtype: {n, recall_at_1, recall_at_5, recall_at_10, mrr}}` from `ok` results (skip empty/`""` qtype into an `"(unlabeled)"` bucket). Render in `build_markdown_report` as a table; include `sample_mode`/`seed`/`limit` in the aggregate dict and markdown header.

## Failure Path Test Strategy

### Exception Handling Coverage
- The adapters already wrap per-record parsing in `try/except ValueError: logger.warning(...); continue`. Moving the limit cut out of that loop must not swallow those warnings — a test feeds a fixture/synthetic list with one malformed record and asserts it is skipped (warning) while sampling still returns `limit` good items. No new `except Exception: pass` blocks are introduced; `sample_items` is pure arithmetic + RNG with explicit guards.

### Empty/Invalid Input Handling
- `sample_items` guards: `limit is None` → all; `limit <= 0` → empty list; `limit >= len(items)` → all; empty `items` → empty list; `mode` unknown → raise `ValueError` (fail loud, not silent). `stratified` with all-empty `question_type` → falls back to stride over the unlabeled bucket. Each guard gets a unit test.
- `--seed` with non-int → argparse rejects (type=int). Determinism test: same `(mode, seed, limit, items)` → identical selection across two calls.

### Error State Rendering
- The per-category table must render when `question_type` is `""` for every item (legacy artifacts / LoCoMo with sparse types) — assert the report still builds and routes those to `"(unlabeled)"` rather than crashing on an empty group.

## Test Impact

- [ ] `tests/benchmarks/test_external.py::TestLongMemEvalAdapter::test_limit_respected` — UPDATE: still asserts `len <= limit`; add that the default sample mode is applied (or pass `sample="head"` explicitly to pin legacy behavior). Keep green.
- [ ] `tests/benchmarks/test_external.py::TestLoCoMoAdapter::test_limit_respected` — UPDATE: same as above for LoCoMo (against #434's adapter).
- [ ] `tests/benchmarks/test_external.py::TestLongMemEvalAdapter::test_all_fixture_items_loaded` / `TestLoCoMoAdapter::test_multiple_qa_per_dialogue` — UPDATE only if the no-limit path changes return type expectations; returning a list of the same items keeps these green (assert counts unchanged).
- [ ] Add `TestSampling` class: stride spans full range, stratified hits every category proportionally, shuffle is seed-deterministic, head is a prefix, edge cases (`limit None / <=0 / >=N`, empty list, unknown mode raises), malformed-record-skipped-then-sampled.
- [ ] Add a Problem-B assertion: run one fixture item through `ExternalScenario` (or assert at the metadata-construction boundary) and confirm `question_type` is present in `ScenarioResult.metadata`.
- [ ] No existing test asserts the *absence* of `question_type`, so adding it breaks nothing.

## Rabbit Holes

- **Streaming-preserving sampling.** Reservoir-sampling to keep `iter_items` a true generator is unnecessary — datasets are ≤500 / ~350 items and already fully loaded into `data`. Materialize a list and sample it.
- **Perfect stratified apportionment math.** Largest-remainder is enough; do not pull in numpy or build an exact integer-programming apportioner.
- **Changing `recall_at_k` / MRR.** Out of scope — #433 already fixed the metric. This plan only *slices* existing metrics by category.
- **Retroactively re-slicing the old `longmemeval_s_20260629.json`.** It lacks `question_type`; regenerate rather than back-fill by re-joining to raw data.
- **Touching `run_external.py`'s arg block or `compute_aggregate` more than needed** while #437 is in flight — keep edits minimal and localized to reduce conflict surface (see Risks).

## Risks

### Risk 1: Merge conflicts with #434 and #437 on the shared files
**Impact:** `run_external.py` and `test_external.py` are edited by all three issues; `datasets/locomo.py` is *rewritten* by #434; `scenarios/external_base.py` metadata dict may be touched by #437.
- `run_external.py`: #437 adds hybrid-mode flags to the same argparse block this plan adds `--sample`/`--seed` to; #437 may also alter `compute_aggregate`/`build_markdown_report` where this plan adds the per-category block → **adjacent-line conflicts**.
- `datasets/locomo.py`: #434 replaces `iter_items`/`_parse_dialogue` wholesale; this plan's LoCoMo sampling wiring must target #434's final shape → **high conflict if built first**.
- `test_external.py`: all three append test classes → mechanical conflicts.
**Mitigation:** Build and merge this **after #434** (ORDERED No-Go + Prerequisite); rebase onto #434's LoCoMo rewrite and wire sampling into its adapter, not current main's. Keep `run_external.py`/`external_base.py` edits minimal and localized. If #437 lands first, rebase and re-apply the (small, additive) flag + aggregate edits. The shared `sampling.py` is a **new file** — zero conflict there — which is why most logic lives in it.

### Risk 2: LoCoMo sampling cannot be validated until #434 lands
**Impact:** On current main the LoCoMo adapter yields 0 items (#434), so the LoCoMo sampling path and report regeneration can't be exercised against real data.
**Mitigation:** Unit-test `sample_items` independently of LoCoMo (synthetic item lists). Gate LoCoMo regeneration on #434 (Prerequisite). LongMemEval-S sampling + regeneration proceed independently.

### Risk 3: Default sample-mode change alters what existing `--limit` invocations measure
**Impact:** Scripts/CI that rely on `--limit N` silently switch from "first N" to a representative sample, changing reported numbers.
**Mitigation:** Document the default in `--help` and the report header (records `sample_mode`); `head` remains available via `--sample head` for exact legacy reproduction. Surface the default choice as Open Question 1 for an explicit decision.

## Race Conditions

No race conditions identified — sampling, scenario execution, and aggregation are
synchronous and single-threaded. `sample_items` uses a local `random.Random(seed)`
instance, never the global RNG, so concurrent test runs cannot perturb each other's
selection.

## No-Gos (Out of Scope)

- [ORDERED] LoCoMo adapter sampling wiring and LoCoMo report regeneration wait for **#434** (LoCoMo real-schema adapter) to merge. Event: #434 closed/merged to main. Build this off updated main and wire LoCoMo sampling into #434's corrected `_parse_dialogue`/`iter_items`; regenerate `locomo_*` in this PR once #434 has landed.
- [SEPARATE-SLUG #437] Hybrid BM25+vector retrieval mode — independent enhancement on the same CLI. This plan only adds sampling + per-category reporting and must avoid colliding with #437's flag/aggregate changes (see Risks).
- LongMemEval-S report regeneration **is** in scope here (dataset is cached locally; no external blocker) — not deferred.

## Update System

No update system changes required — this is a test-harness-internal change.

## Agent Integration

No agent integration required — the benchmark harness is a developer/CI tool, not an agent-facing capability.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` entry — harness-internal quality fix.

### External Documentation Site
- [ ] No docs-site pages reference `--limit` sampling or per-question schema. Confirm with `grep -rn "run_external\|--limit\|question_type" docs/`; update only if a page documents the smoke-run command.

### Inline Documentation
- [ ] Docstring `sample_items` (modes, seeding, apportionment).
- [ ] Update `iter_items` docstrings in both adapters to document `sample`/`seed`.
- [ ] Update `run_external.py` module docstring's "Smoke test" usage block to show `--sample`/`--seed` and note that `--limit` is now representative by default.
- [ ] Update `tests/benchmarks/README.md` if it documents `--limit` behavior or the report schema.

## Success Criteria

- [ ] A seeded representative sampling mode (`stride` and `stratified`, plus `shuffle`) exists and is applied for limited runs; `head` is opt-in and emits a WARNING when used with `--limit`.
- [ ] `run_external.py` exposes `--sample {head,stride,shuffle,stratified}` and `--seed`; both thread into LongMemEval-S and LoCoMo adapters via the shared `sample_items`.
- [ ] LoCoMo's limited-run path uses the same `sample_items` mechanism (wired onto #434's adapter).
- [ ] Sampling is reproducible: same `(mode, seed, limit)` → identical selection (unit test).
- [ ] Every per-question result record (both datasets) includes `question_type` (one-line `external_base.py` fix; verified in regenerated JSON).
- [ ] Report summary includes a per-`question_type` breakdown (JSON block + markdown table); `sample_mode`/`seed`/`limit` recorded in the artifact.
- [ ] Committed artifacts regenerated: `longmemeval_s_*` now; `locomo_*` after #434 lands; `*_latest` symlinks repointed.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

Small fix — solo builder + validator. Most logic is a new, isolated file.

### Team Members

- **Builder (sampling)**
  - Name: sampling-builder
  - Role: Add `datasets/sampling.py`, wire adapters + CLI, add `question_type` to scenario metadata, add per-category aggregation, regenerate artifacts.
  - Agent Type: builder
  - Resume: true

- **Validator (sampling)**
  - Name: sampling-validator
  - Role: Verify representative + seeded sampling, `question_type` threading, per-category report, and regenerated-artifact schema.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Shared sampler
- **Task ID**: build-sampler
- **Depends On**: none
- **Validates**: tests/benchmarks/test_external.py::TestSampling (create)
- **Assigned To**: sampling-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tests/benchmarks/datasets/sampling.py` with `sample_items(items, limit, mode, seed)` implementing `head`/`stride`/`shuffle`/`stratified` (local `random.Random(seed)`; largest-remainder apportionment; explicit guards for `None`/`<=0`/`>=N`/empty/unknown-mode).
- Add `TestSampling` unit tests (stride spans range, stratified covers every category, shuffle seed-deterministic, head prefix, all edge cases, unknown mode raises).

### 2. Thread question_type through the scenario (Problem B)
- **Task ID**: build-qtype
- **Depends On**: none
- **Validates**: tests/benchmarks/test_external.py (Problem-B assertion)
- **Assigned To**: sampling-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `"question_type": self.item.metadata.get("question_type", "")` to the `ScenarioResult.metadata` dict at `scenarios/external_base.py:286-295`.
- Add a test asserting `question_type` survives into `ScenarioResult.metadata`.

### 3. Wire adapters + CLI
- **Task ID**: build-wiring
- **Depends On**: build-sampler
- **Validates**: tests/benchmarks/test_external.py::TestLongMemEvalAdapter, ::TestLoCoMoAdapter
- **Assigned To**: sampling-builder
- **Agent Type**: builder
- **Parallel**: false
- Materialize-then-sample in both `iter_items`; add `sample`/`seed` params (default `head`). LoCoMo wiring targets #434's adapter.
- Add `--sample`/`--seed` to `run_external.py`; thread into both `iter_*` calls; log resolved mode/seed; emit WARNING for `head`+`--limit`.

### 4. Per-category aggregation + self-describing report
- **Task ID**: build-report
- **Depends On**: build-qtype, build-wiring
- **Validates**: tests/benchmarks/test_external.py
- **Assigned To**: sampling-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `by_question_type` block to `compute_aggregate` and a markdown table to `build_markdown_report`; record `sample_mode`/`seed`/`limit` in the aggregate + header. Handle empty/`""` qtype as `"(unlabeled)"`.

### 5. Regenerate artifacts
- **Task ID**: build-regen
- **Depends On**: build-report
- **Assigned To**: sampling-builder
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_external --dataset longmemeval-s`; commit new `.json`/`.md`; repoint `longmemeval_s_latest` symlinks. Confirm `question_type` present in every record and the per-category table renders.
- LoCoMo regeneration gated on #434 (ORDERED) — run after it lands.

### 6. Validation
- **Task ID**: validate-all
- **Depends On**: build-sampler, build-qtype, build-wiring, build-report, build-regen
- **Assigned To**: sampling-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/benchmarks/test_external.py -q`. Verify reproducibility, `question_type` in artifact, per-category table, success criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark tests pass | `pytest tests/benchmarks/test_external.py -q` | exit code 0 |
| Sampler module exists | `grep -n "def sample_items" tests/benchmarks/datasets/sampling.py` | exit code 0 |
| Stratified + stride modes present | `grep -En "stratified\|stride" tests/benchmarks/datasets/sampling.py` | output > 0 |
| Local seeded RNG (not global) | `grep -n "random.Random(" tests/benchmarks/datasets/sampling.py` | exit code 0 |
| CLI exposes --sample/--seed | `grep -En "\"--sample\"\|'--sample'\|--seed" tests/benchmarks/run_external.py` | output > 0 |
| question_type threaded in scenario | `grep -n "question_type" tests/benchmarks/scenarios/external_base.py` | exit code 0 |
| Per-category breakdown in aggregate | `grep -n "by_question_type" tests/benchmarks/run_external.py` | exit code 0 |
| Regenerated artifact carries question_type | `grep -c "question_type" tests/benchmarks/results/external/longmemeval_s_latest.json` | output > 0 |
| Determinism test present | `grep -rn "seed\|determinist" tests/benchmarks/test_external.py` | exit code 0 |
| Format clean | `black --check tests/benchmarks/datasets/sampling.py tests/benchmarks/run_external.py tests/benchmarks/scenarios/external_base.py tests/benchmarks/test_external.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

_Resolved 2026-06-30 (orchestrator, recommended defaults accepted):_

1. **Default sample mode — RESOLVED: `stride`** is the default when `--limit` is set
   (deterministic, spans the whole category-grouped file). `head` is opt-in via
   `--sample head` and emits a WARNING. This intentionally changes what bare
   `--limit N` measures (Risk 3); the `sample_mode` is recorded in every report so
   runs stay self-describing and legacy behavior is reproducible with `--sample head`.
2. **Stratified vs. stride — RESOLVED: ship both**, `stride` default, `stratified`
   opt-in.
3. **Per-category breakdown — RESOLVED: in scope** for this PR.
4. **Copy `answer` too? — RESOLVED: no.** Only `question_type` is threaded.
