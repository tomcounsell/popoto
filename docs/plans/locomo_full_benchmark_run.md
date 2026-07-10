---
status: Ready
type: chore
appetite: Small
owner: valor
created: 2026-07-07
tracking: https://github.com/tomcounsell/popoto/issues/447
last_comment_id: 4875437883
revision_applied: false
---

# Full LoCoMo Benchmark Run: Replace the 6-Question Fixture Baseline with Complete Lexical + Hybrid Artifacts

## Problem

LongMemEval-S has full-dataset committed artifacts in both retrieval modes: a
500-question lexical baseline (`longmemeval_s_20260630.{md,json}`) and a
500-question hybrid run (`longmemeval_s_20260703_hybrid.{md,json}`, R@1 0.894 /
R@5 0.986 / R@10 0.992 / MRR 0.932, shipped via #442/#443). LoCoMo only has a
**6-question fixture run** committed (`locomo_20260522.{md,json}`) — enough to
prove the adapter works, not a credible baseline.

**Current behavior:** `python -m tests.benchmarks.run_external --dataset locomo`
works, but the committed baseline covers 6 of ~350 QA pairs (one dialogue's
fixture). Recall/MRR at that sample size is statistically meaningless. The
`locomo_latest.*` symlinks point at that 6-question fixture
(`tests/benchmarks/results/external/locomo_latest.{json,md}` → `locomo_20260522.*`),
and there are **no** `locomo_latest_hybrid.*` artifacts at all.

**Why this is painful now:** The benchmark sequence (#394 → default hybrid
retrieval → consolidation/decay lifecycle) requires every retrieval change to
show measurable delta. For LongMemEval-S that story is complete; for LoCoMo any
claimed delta would be against a 6-question fixture.

**Desired outcome:** Full-dataset LoCoMo artifacts committed for **both**
retrieval modes, `locomo_latest.*` repointed and `locomo_latest_hybrid.*`
created, `docs/benchmarks.md` baseline tables filled with the real numbers, and
zero errored items (or errors documented). This mirrors the LongMemEval-S story.
**Measurement only — no retrieval-quality/tuning changes.**

## Freshness Check

**Baseline commit:** `893c1e0` (`fix(#446): deterministic tie-break for
equal-scored BM25 search results (#450)`) — the last commit touching
`tests/benchmarks/`.
**Issue filed at:** 2026-07-03T10:22:34Z
**Disposition:** Unchanged

**File:line references re-verified against current main:**
- `tests/benchmarks/scenarios/external_base.py:82-96` — module-level
  `_SHARED_PROVIDER` + `_get_shared_provider()` (the #443 shared-embedding fix)
  — **confirmed present**. Used at line 139 for the `EmbeddingField` provider.
- `tests/benchmarks/scenarios/external_base.py:411` — `teardown()` calls
  `stop_invalidation_listeners()` (the #443 pool-exhaustion fix) — **confirmed
  present**. The LoCoMo path runs through the same `ExternalScenario`, so it
  inherits both fixes with no code change.
- `tests/benchmarks/run_external.py:421-452` — `save_reports` suffixes any
  non-lexical mode into both dated and `_latest` filenames (`_hybrid`), lexical
  stays unsuffixed — **confirmed**. This is exactly the naming the acceptance
  criteria require; no change needed.
- `tests/benchmarks/datasets/locomo.py:44-45` — downloads
  `snap-research/locomo` / `locomo10.json` into
  `~/.cache/popoto_benchmarks/locomo.json` — **confirmed**. The dataset is
  **not currently cached** locally (only `longmemeval_s_cleaned.json` is), so
  the run will trigger a one-time HuggingFace download.
- `tests/benchmarks/metrics/retrieval.py:137` — `recall_at_k` returns `0.0`
  when `relevant` is empty — **confirmed**. Direct consequence: LoCoMo
  **adversarial** QAs (category 5, no `evidence`) score 0 on recall/MRR **by
  construction** (see Risks / Open Questions).
- `docs/benchmarks.md:187-194` — LoCoMo baseline table is the v1.6.3 all-zeros
  fixture floor; `docs/benchmarks.md:159-165` — LongMemEval-S Retrieval Modes
  comparison table (LoCoMo has no equivalent yet) — **confirmed**.

**Cited sibling issues/PRs re-checked:**
- #445 (default-recipe BM25Field wiring) — **MERGED** (commit `9886d0f`,
  `test(#445)...(#449)`). On main.
- #446 (BM25 stable-sort tie-break) — **MERGED** (commit `893c1e0`, `(#450)`).
  On main. The issue explicitly says to run *after* these land so the baseline
  reflects current main — both have landed.
- #442/#443 (LongMemEval-S hybrid + harness fixes) — **MERGED** (commit
  `be3b038`). The shared-provider + listener-teardown fixes this run depends on
  are on main.
- #394 (external harness) — closed; harness is the substrate this consumes.

**Commits on main since issue filed** touching `tests/benchmarks/` or
`docs/benchmarks.md`: only `893c1e0` (#446), which is the tie-break fix the
issue says to wait for. No other drift.

**Active plans in `docs/plans/` overlapping this area:**
- `benchmark_hybrid_full_run.md` (#442) — **status: Complete**. Direct sibling;
  this plan mirrors its structure for LoCoMo. No code overlap.
- `locomo_adapter_real_schema.md` (#434) — shipped; built the adapter this
  consumes. No open overlap.

**Notes:** No drift. Premise intact: the harness supports full LoCoMo runs in
both modes with the #443 fixes already present, and only a 6-question fixture is
committed.

## Research

No external research required. The work is internal + measurement: run an
existing CLI twice, commit artifacts, edit a docs table. The only external
dependency is the LoCoMo dataset itself (`snap-research/locomo`, `locomo10.json`
on HuggingFace), already wired into `locomo.py`'s downloader and gated behind the
`[benchmark]` extra (`huggingface_hub`). Both prerequisites are already
satisfied locally (`huggingface_hub`, `sentence_transformers`, `numpy` import
cleanly; Redis answers `PONG`).

## Prior Art

- **#442/#443 (`benchmark_hybrid_full_run.md`, shipped)**: ran and committed the
  full 500-question LongMemEval-S hybrid artifact and added the shared-provider
  + listener-teardown harness fixes. This plan is the LoCoMo twin of that work,
  consuming the identical harness with **zero code changes** — the fixes that
  made a full run practical already apply to the LoCoMo path.
- **#434 (`locomo_adapter_real_schema.md`, shipped)**: rewrote the LoCoMo
  adapter for the real `snap-research/locomo` dict schema (dia_id evidence,
  blip_caption image ingestion). This plan runs that adapter at full scale.
- **#438 (`external_recall_at_k_any_hit.md`, merged)**: the any-hit Recall@k
  metric used for both LongMemEval-S and LoCoMo — the LoCoMo numbers will be
  measured with the identical metric for apples-to-apples comparison.
- **#445 / #446 (merged)**: BM25Field default wiring + deterministic tie-break.
  The issue explicitly sequenced this run after them so the committed baseline
  reflects current-main BM25 behavior.

No prior attempt to run and commit the **full** LoCoMo number exists — only the
6-question fixture. This is the first.

## Data Flow

1. **Entry point (lexical):** `python -m tests.benchmarks.run_external --dataset
   locomo`. `main()` selects the LoCoMo adapter; `iter_locomo(...)` downloads
   `locomo10.json` (first run), parses **all** dialogues into a flat item list
   (one `BenchmarkItem` per QA pair, ~350 total), and samples (`--limit` unset ⇒
   full dataset).
2. **Per item:** `run_item(item, retrieval_mode="lexical")` builds
   `ExternalScenario`; `setup()` ingests each turn (keyed by `dia_id`), `run()`
   drives `ContextAssembler.assemble()` (auto ⇒ lexical BM25 with the model
   carrying only `BM25Field`), `teardown()` cleans up and
   `stop_invalidation_listeners()` releases pool connections.
3. **Adversarial QAs:** category-5 items have no `evidence` ⇒ empty
   `relevant_ids` ⇒ `recall_at_k`/MRR = 0 by construction. Status is still `ok`
   (turns ingested, retrieval ran), so they count in `n_ok` and **deflate the
   headline aggregate**. The `_by_question_type` breakdown isolates them under
   category `5`.
4. **Aggregation & output:** `compute_aggregate(...)` rolls up R@1/5/10 + MRR +
   p50/p95 + `by_question_type`; `save_reports(aggregate, "locomo",
   retrieval_mode="lexical")` writes `locomo_{date}.{json,md}` and **repoints**
   `locomo_latest.{json,md}` (unsuffixed — replacing the fixture symlinks).
5. **Entry point (hybrid):** same, with `--retrieval-mode hybrid`. The per-item
   model additionally carries `EmbeddingField(provider=_get_shared_provider())`
   (MiniLM loads once for the whole run); assemble resolves to hybrid (BM25 +
   numpy-cosine vector via RRF k=60). `save_reports(..., retrieval_mode="hybrid")`
   writes `locomo_{date}_hybrid.{json,md}` and **creates**
   `locomo_latest_hybrid.{json,md}` — never touching the lexical artifacts.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0-1 (only if the hybrid run wall-clock or the adversarial-item
  interpretation warrants a heads-up).
- Review rounds: 1 (no production code changes; the substance is the two
  committed artifacts + the docs table).

There is **no implementation code** in this plan — the harness already supports
full LoCoMo runs in both modes. The work is: run twice, verify, commit
artifacts, edit `docs/benchmarks.md`. The long pole is the **hybrid run
wall-clock** (CPU MiniLM embedding of all LoCoMo turns — ~50 dialogues ×
~200-600 turns each, on the order of ~26k turns, materially smaller than
LongMemEval-S's ~275k, so faster than the #442 run). Small appetite reflects
zero code surface; the care is in preserving the lexical artifacts from the
hybrid run and interpreting the adversarial-item effect.

## Prerequisites

| Requirement | Check Command | Purpose | Status |
|-------------|---------------|---------|--------|
| `[benchmark]` extra installed | `python -c "import huggingface_hub, sentence_transformers, numpy"` | HF download + MiniLM + numpy cosine | ✅ verified |
| Redis/Valkey on :6379 | `redis-cli ping` | Benchmark ingestion target | ✅ `PONG` |
| LoCoMo dataset reachable | (adapter downloads `snap-research/locomo`/`locomo10.json`) | ~350 QA over 50 dialogues | ⚠️ **not cached** — first run downloads |
| MiniLM cached (one-time ~90MB) | `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` | hybrid vector signal | already present from #442 run |

## Solution

### Key Elements

- **No code changes.** The harness (`run_external.py`, `external_base.py`,
  `locomo.py`) already supports full-dataset LoCoMo runs in both modes, and
  already carries the #443 shared-provider + listener-teardown fixes on the
  shared `ExternalScenario` path. `save_reports` already suffixes non-lexical
  runs correctly. Do not modify these files.
- **Run 1 — lexical:** `python -m tests.benchmarks.run_external --dataset locomo`.
  Produces `locomo_{date}.{json,md}` and repoints `locomo_latest.*` (replacing
  the 6-question fixture symlinks). Commit both dated files + the repointed
  symlinks.
- **Run 2 — hybrid:** `python -m tests.benchmarks.run_external --dataset locomo
  --retrieval-mode hybrid`. Produces `locomo_{date}_hybrid.{json,md}` and creates
  `locomo_latest_hybrid.*`. Commit these; confirm the lexical artifacts are
  byte-untouched.
- **Verify zero errors** in each run (`summary.n_errors == 0`) or document any
  errored items in the report notes / plan. Confirm `n_total ≈ 350`.
- **Interpret adversarial items:** surface the `by_question_type` breakdown so
  the category-5 (adversarial, 0-by-construction) contribution to the headline
  aggregate is legible. **Do not change the metric or scoring** (out of scope).
- **Docs:** add a LoCoMo **Retrieval Modes comparison table**
  (`docs/benchmarks.md`, mirroring the LongMemEval-S table at lines 159-165) with
  the measured lexical vs hybrid R@1/R@5/R@10/MRR, and replace the v1.6.3
  all-zeros LoCoMo fixture-floor table (lines 187-194) with the real full-dataset
  numbers. Note the adversarial-item effect on the headline aggregate.

### Flow

`--dataset locomo` (lexical) → download dataset → ~350 items ingest + assemble
(BM25) → aggregate → `save_reports(..., "lexical")` repoints `locomo_latest.*` →
commit → re-run with `--retrieval-mode hybrid` → `save_reports(..., "hybrid")`
creates `locomo_latest_hybrid.*` (lexical artifacts untouched) → commit → fill
the LoCoMo docs tables → `mkdocs build --strict`.

### Technical Approach

- **Running:** run both modes against the default output directory (the
  `save_reports` suffix guarantees the hybrid run cannot clobber the lexical
  artifacts). Pre-download the dataset by letting the first (lexical) run fetch
  it, or verify the cache exists before the hybrid run. For the hybrid run,
  prefer background execution + progress monitoring so an interactive timeout
  doesn't kill a multi-minute run; the harness prints progress every 10 items.
- **Commit hygiene:** stage **only** `tests/benchmarks/results/external/locomo_*`
  artifacts (dated + `_latest*` pointers) and `docs/benchmarks.md`. Do **not**
  commit run byproducts: `data/` (untracked embedding scratch),
  `.embeddings/` `.npy` files, or the `~/.cache` dataset. `git status` review
  before each commit.
- **Docs edits:** add the LoCoMo Retrieval Modes table near the LongMemEval-S one
  (after line ~174), and rewrite the LoCoMo baseline table (lines 187-194) with
  the real numbers plus a one-line note on the adversarial-item deflation and a
  pointer to the committed `locomo_latest*.json` for per-category detail. Keep
  the Valkey-safety statement. `scripts/ci-local.sh docs` must pass.

## Rabbit Holes

- **Tuning retrieval to improve LoCoMo numbers.** Out of scope — measurement
  only. Report whatever default BM25 / RRF(k=60) produces. No field, fusion, or
  weight changes.
- **"Fixing" the adversarial-item zeros by changing the metric or excluding
  category 5 from the aggregate.** That is a scoring-semantics change, out of
  scope. Report the raw aggregate + the `by_question_type` breakdown and
  **document** the effect; do not alter `recall_at_k` or the scenario.
- **Refactoring `save_reports` / artifact layout.** It already does exactly what
  the acceptance criteria need. Touch nothing.
- **Re-running or re-recording LongMemEval-S.** Complete as of #443; leave it.
- **Speeding up CPU embedding with batching/threading.** If the hybrid run is
  slow, run it in the background — do not build a parallel-embedding pipeline.
- **Committing run byproducts** (`data/doc_embeddings.json`, `.embeddings/`
  `.npy`, the cached dataset). Artifacts + docs only.

## Risks

### Risk 1: Adversarial QAs deflate the headline aggregate (interpretation trap)
**Impact:** LoCoMo category-5 (adversarial) questions have no evidence, so they
score recall=0/MRR=0 by construction while still counting in `n_ok`. The blended
headline R@k will look worse than the retrievable-question performance, and a
naive reader could misread it as a retrieval regression.
**Mitigation:** Report the `by_question_type` breakdown alongside the headline
number and add an explicit note (report + `docs/benchmarks.md`) that category-5
items are 0-by-construction. This is documentation, not a scoring change. Flag
the interpretation choice as an Open Question (headline = raw aggregate, with the
breakdown for context).

### Risk 2: Hybrid run clobbers the lexical artifacts
**Impact:** Overwriting `locomo_{date}.*` or repointing `locomo_latest.*` from
the hybrid run would corrupt the lexical baseline.
**Mitigation:** `save_reports`'s mode suffix is the structural guard — hybrid
writes `_hybrid` names only (verified at `run_external.py:421`). Run lexical
first, commit, then run hybrid; `git status` before the hybrid commit must show
only `locomo_*_hybrid.*` + `locomo_latest_hybrid.*` changed.

### Risk 3: Dataset download fails / schema surprise at full scale
**Impact:** The adapter has only ever run against the 6-question fixture at
commit time; a full download could hit an HF availability issue or a dialogue
whose schema trips `_parse_dialogue` (it `logger.warning`s and skips malformed
dialogues rather than erroring).
**Mitigation:** The first lexical run downloads and parses all 50 dialogues; if
any are skipped, the log surfaces it and `n_total` will be < ~350 — document any
shortfall in the report notes and the plan. If download fails, the adapter
raises `RuntimeError` with a manual-placement path; retry or place the file
manually. No partial/`--limit` run may be committed as the headline number.

### Risk 4: Valkey-compatibility regression
**Impact:** Any Redis-module dependency (`FT.*`/`BF.*`/`CMS.*`) breaks Valkey.
**Mitigation:** No code changes here; the hybrid path uses the same in-process
numpy-cosine substrate shipped in #443. A verification grep over the touched
docs/artifacts confirms no module commands are introduced.

### Risk 5: Non-determinism across the two runs
**Impact:** BM25 ties previously produced unstable orderings; #446 fixed the
tie-break, but a run predating the local editable-install refresh could still
show drift.
**Mitigation:** #446 is on main (`893c1e0`); confirm the editable install is
current (`scripts/ci-local.sh` auto-refreshes on version drift) before running.
The committed numbers are a point-in-time baseline, not a CI assertion.

## Race Conditions

None. Ingestion and retrieval are synchronous and single-threaded per item;
items run sequentially. The shared MiniLM provider is read-only after its
one-time load. Each turn is ingested in `setup()` before `run()` queries.
`teardown()`'s `stop_invalidation_listeners()` runs per item, keeping the
connection pool from exhausting across the full run (the #443 fix).

## No-Gos (Out of Scope)

- Any retrieval-quality change: tuning, new fields, fusion weights, RRF_K,
  metric semantics, or excluding adversarial items from the aggregate.
- LongMemEval-S re-runs (complete as of #443).
- Modifying `run_external.py`, `external_base.py`, `locomo.py`, or
  `metrics/retrieval.py`. This is a run-and-record chore.
- Committing run byproducts or the cached dataset.
- CI-gating a LoCoMo run behind a pre-cached HF step (file separately if wanted).

## Update System

No update/deploy-system changes. Internal benchmark run + docs only; the hybrid
path lives behind the already-declared `[benchmark]` extra.

## Agent Integration

None. The benchmark harness is a developer CLI; no agent/MCP tool surface is
touched.

## Documentation

### Feature Documentation
- [x] `docs/benchmarks.md` — add a **LoCoMo Retrieval Modes** comparison table
  (lexical vs hybrid R@1/R@5/R@10/MRR), mirroring the LongMemEval-S table at
  lines 159-165, placed near it.
- [x] `docs/benchmarks.md:187-194` — replace the v1.6.3 all-zeros LoCoMo fixture
  table with the real full-dataset numbers; add a note on the category-5
  (adversarial) 0-by-construction effect and a pointer to `locomo_latest*.json`
  for per-category detail.

### External Documentation Site
- [x] `mkdocs build --strict` passes after the edits (`scripts/ci-local.sh docs`).

### Inline Documentation
- [x] None — no code changes. (The committed report `md` files are
  self-describing via `build_markdown_report`.)

## Success Criteria

- [x] Full-dataset (~350 QA) **lexical** LoCoMo artifact committed
  (`locomo_{date}.{json,md}`); `locomo_latest.{json,md}` repointed off the
  6-question fixture onto it.
- [x] Full-dataset **hybrid** LoCoMo artifact committed with `_hybrid` suffix
  (`locomo_{date}_hybrid.{json,md}`); `locomo_latest_hybrid.{json,md}` created.
- [x] Each run has `summary.n_errors == 0`, or errored/skipped items are
  documented in the report notes and this plan. `n_total ≈ 350`.
- [x] The hybrid run leaves the lexical `locomo_{date}.*` / `locomo_latest.*`
  artifacts byte-unchanged (`git status` confirms only `_hybrid` names in the
  hybrid commit).
- [x] `docs/benchmarks.md` has a LoCoMo Retrieval Modes table with the measured
  lexical + hybrid numbers, and the fixture-floor table is replaced with real
  numbers + the adversarial-item note.
- [x] No Redis/Valkey module commands introduced (verification grep passes).
- [x] `mkdocs build --strict` passes.

## Step by Step Tasks

### 1. Confirm prerequisites and refresh install
- **Task ID**: prereqs
- **Depends On**: none
- Verify `[benchmark]` extra imports, Redis `PONG`, MiniLM cached, and the
  editable install is current (so #445/#446 behavior is in effect).

### 2. Run the full lexical LoCoMo benchmark
- **Task ID**: run-lexical
- **Depends On**: prereqs
- **Validates**: committed `locomo_{date}.{json,md}` with ~350 questions, 0 errors
- `python -m tests.benchmarks.run_external --dataset locomo` (first run downloads
  the dataset).
- Confirm `summary.retrieval_mode == "lexical"`, `n_total ≈ 350`,
  `n_errors == 0` (or document). Inspect `by_question_type` for the category-5
  adversarial group.
- Commit `locomo_{date}.{json,md}` + the repointed `locomo_latest.{json,md}` only.

### 3. Run the full hybrid LoCoMo benchmark
- **Task ID**: run-hybrid
- **Depends On**: run-lexical
- **Validates**: committed `locomo_{date}_hybrid.{json,md}`; lexical artifacts untouched
- `python -m tests.benchmarks.run_external --dataset locomo --retrieval-mode
  hybrid` (background + monitor for the CPU embedding run).
- Confirm `summary.retrieval_mode == "hybrid"`, `n_total ≈ 350`,
  `n_errors == 0` (or document).
- `git status` must show only `locomo_*_hybrid.*` + `locomo_latest_hybrid.*`
  changed. Commit those only.

### 4. Update the LoCoMo docs tables
- **Task ID**: docs
- **Depends On**: run-hybrid
- **Validates**: `mkdocs build --strict`
- Add the LoCoMo Retrieval Modes comparison table (lexical vs hybrid) near the
  LongMemEval-S one.
- Replace the v1.6.3 all-zeros LoCoMo fixture table (lines 187-194) with the real
  numbers + adversarial-item note + artifact pointer.
- `scripts/ci-local.sh docs`.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: docs
- Valkey-safety grep on the touched docs/artifacts.
- Confirm `git status` stages only `locomo_*` artifacts + `docs/benchmarks.md`
  (no `data/`, no `.embeddings/`, no cached dataset).
- Confirm all Success Criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Lexical artifact committed | `ls tests/benchmarks/results/external/locomo_*.json \| grep -v hybrid \| grep -v latest` | one dated file (post-2026-07-07) |
| Lexical is the full run | `python -c "import json,glob; d=json.load(open(sorted(g for g in glob.glob('tests/benchmarks/results/external/locomo_*.json') if 'hybrid' not in g and 'latest' not in g)[-1])); print(d['summary']['retrieval_mode'], d['summary']['n_total'], d['summary']['n_errors'])"` | `lexical` ~350 `0` |
| `locomo_latest` repointed off fixture | `readlink tests/benchmarks/results/external/locomo_latest.json` | new dated file (not `locomo_20260522.json`) |
| Hybrid artifact committed | `ls tests/benchmarks/results/external/locomo_*_hybrid.json` | one dated `_hybrid` file |
| Hybrid is the full run | `python -c "import json,glob; d=json.load(open(sorted(glob.glob('tests/benchmarks/results/external/locomo_*_hybrid.json'))[-1])); print(d['summary']['retrieval_mode'], d['summary']['n_total'], d['summary']['n_errors'])"` | `hybrid` ~350 `0` |
| `locomo_latest_hybrid` exists | `ls tests/benchmarks/results/external/locomo_latest_hybrid.{json,md}` | both present |
| Docs LoCoMo modes table present | `grep -c "LoCoMo.*Recall\|locomo.*hybrid" docs/benchmarks.md` | ≥ 1 |
| Fixture-floor table replaced (anti-criterion) | `grep -c "fixture sample, 6 QA pairs" docs/benchmarks.md` | `0` |
| No Redis vector-module commands | `grep -rEn "FT\.\|BF\.\|CMS\." docs/benchmarks.md tests/benchmarks/results/external/locomo_*` | `0` matches |
| No harness code changed | `git status --porcelain tests/benchmarks/run_external.py tests/benchmarks/scenarios/external_base.py tests/benchmarks/datasets/locomo.py` | empty |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Decisions (formerly Open Questions)

_Coordinator approved all recommended defaults on 2026-07-07 — no PM loop-back needed:_

1. **Adversarial (category-5) items in the headline aggregate.** DECIDED:
   report the **raw** aggregate (including the zeros) as the headline — it's
   what the harness computes and stays comparable to how other tools report
   LoCoMo — but always surface the `by_question_type` breakdown and add an
   explicit note that category-5 is 0-by-construction. The metric and category
   inclusion are unchanged (out of scope).
2. **Hybrid run wall-clock.** DECIDED: run it in the background and monitor; do
   not commit a `--limit` partial as the headline. Escalate only if it can't
   finish in a single session.
3. **Docs table placement.** DECIDED: add the LoCoMo Retrieval Modes table
   immediately after the LongMemEval-S one (after line ~174) and replace the
   fixture-floor table in place (lines 187-194), rather than restructuring the
   Baseline Numbers section.
