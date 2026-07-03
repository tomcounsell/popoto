---
status: Complete
type: chore
appetite: Medium
owner: valor
created: 2026-06-30
tracking: https://github.com/tomcounsell/popoto/issues/442
last_comment_id:
revision_applied: false
---

# Run the Full 500-Question LongMemEval-S Hybrid Benchmark and Commit the Artifact

## Problem

The hybrid retrieval **path** shipped in #437 (merged via #441) and is validated by
the harness tests — effective mode resolves to `hybrid`, RRF fusion executes, vector
cosine is numpy-only. But the committed 500-question hybrid **comparison number** was
deferred: a full hybrid run embeds ~275k turns on CPU (`all-MiniLM-L6-v2`), which is
far slower than the lexical pass, and long local runs were getting interrupted.

So today the only external number Popoto can report on LongMemEval-S is **lexical-only
(BM25): R@1 0.856 / R@5 0.952 / R@10 0.978 / MRR 0.899**. The `hybrid` row in the
Retrieval Modes comparison table in `docs/benchmarks.md` (line 155) reads
`— see note`, and a pending-run note (lines 158-167) stands in for the real numbers.
We cannot yet make the apples-to-apples claim — hybrid vs lexical vs the `agentmemory`
reference (R@5 0.952 / R@10 0.986 / MRR 0.882) — that the benchmark exists to support.

**Current behavior:**
- `tests/benchmarks/scenarios/external_base.py:105-108`: in **hybrid** mode,
  `_build_external_model_class` declares `embedding = EmbeddingField(source="content",
  provider=SentenceTransformersProvider())`. `_build_external_model_class` is called
  **once per benchmark item** from `setup()` (`external_base.py:174-176`), so a brand
  **new** `SentenceTransformersProvider()` is constructed for every one of the 500
  items. Each fresh instance reloads `all-MiniLM-L6-v2` on its first `embed()`
  (`sentence_transformers.py:88-93` lazy-loads and caches the model **on the
  instance**, so the cache does not survive across items) — repeated HF
  cache-validation + model-into-memory cost, 500×. This is the inefficiency the issue
  flags as making the full run impractical.
- `tests/benchmarks/run_external.py:408-413`: `save_reports` names artifacts
  `{dataset_slug}_{YYYYMMDD}.{json,md}` with **no retrieval-mode suffix**, and repoints
  `{dataset_slug}_latest.{json,md}` to them (lines 424-437). A hybrid run on 2026-06-30
  with the default output directory would therefore **overwrite the committed lexical
  baseline** `longmemeval_s_20260630.{json,md}` and clobber the lexical `_latest`
  symlinks. (`--output DIR` is honored — `main()` reassigns the `RESULTS_DIR` global at
  `run_external.py:516-518` — so a separate directory is a workaround, but the filename
  collision inside the canonical results dir is the real preservation hazard.)
- `docs/benchmarks.md:155`: hybrid row is `— see note`; lines 158-167 hold the
  pending-run note.

**Desired outcome:**
- One shared `SentenceTransformersProvider` instance is reused across all items so the
  MiniLM model loads **once**, making the full 500-question hybrid run practical.
- The hybrid run writes to a **distinct artifact** (`longmemeval_s_{date}_hybrid.*`)
  that does **not** overwrite the committed lexical baseline (`longmemeval_s_20260630.*`)
  or its `_latest` symlinks.
- The committed hybrid artifact's real R@1 / R@5 / R@10 / MRR fill the `hybrid` row of
  the Retrieval Modes table, the pending-run note is replaced with a real comparison
  against the lexical BM25 baseline and the `agentmemory` reference, and the artifact
  is committed for reproducibility.

## Freshness Check

**Baseline commit:** `b77e59b` (Plan complete benchmark_hybrid_retrieval, shipped via #441)
**Issue filed at:** 2026-06-30T07:38:35Z
**Disposition:** Unchanged

**File:line references re-verified against current main:**
- `tests/benchmarks/scenarios/external_base.py:105-108` — per-item
  `SentenceTransformersProvider()` construction inside `_build_external_model_class` —
  **confirmed** (the reload-per-item inefficiency is real).
- `tests/benchmarks/scenarios/external_base.py:174-176` — `setup()` calls
  `_build_external_model_class(safe_prefix, with_embedding=...)` per item — **confirmed**.
- `src/popoto/embeddings/sentence_transformers.py:88-93` — model cached **on the
  instance**, lazily on first `embed()` — **confirmed** (so a per-item instance reloads).
- `tests/benchmarks/run_external.py:408-413,424-437` — `save_reports` names files
  `{slug}_{date}` with no mode suffix and repoints `_latest` — **confirmed** (clobber
  hazard real).
- `tests/benchmarks/run_external.py:516-518` — `--output` reassigns the `RESULTS_DIR`
  global — **confirmed** (`--output` works; filename collision is the residual risk).
- `docs/benchmarks.md:154-167` — lexical row `0.856 / 0.952 / 0.978 / 0.899`, hybrid
  row `— see note`, agentmemory reference `— / 0.952 / 0.986 / 0.882`, pending-run note —
  **confirmed**.

**Cited sibling issues/PRs re-checked:**
- #437 — **CLOSED**; the hybrid path it tracks is on main.
- #441 — **MERGED** 2026-06-30T07:38:01Z (commit `e407801`); the hybrid capability
  (`SentenceTransformersProvider`, `--retrieval-mode`, assemble-primary `run()`) landed.
- #434 / #435 — **MERGED** (PRs #439/#440); the merge-ordering constraint that gated
  the #437 work is fully resolved. No open siblings touch the shared files.

**Commits on main since issue was filed:** `git log --since=2026-06-30T07:38:35Z --
tests/benchmarks/ docs/benchmarks.md src/popoto/embeddings/` returns **none**. The
referenced files are unchanged since the issue was filed (~minutes after #441 merged).

**Active plans in `docs/plans/` overlapping this area:**
- `benchmark_hybrid_retrieval.md` — **status: Complete** (shipped via #441). It built
  the hybrid *capability*; this plan *runs* it and records the number. No code overlap.
- `external_recall_at_k_any_hit.md` (#438), `benchmark_representative_sampling.md`
  (#435), `locomo_adapter_real_schema.md` (#434) — all shipped; no open overlap.

**Notes:** No drift. Premise intact: capability is merged, the number is unrecorded,
the provider reloads per item, and `save_reports` would clobber the lexical baseline.

## Research

No external research required. The work is internal: a shared-instance refactor of an
already-shipped provider, a filename-suffix change in an internal CLI, running an
existing benchmark, and a docs-table edit. `all-MiniLM-L6-v2` / `sentence-transformers`
behavior is already understood and validated by the shipped harness tests. No new
libraries, APIs, or ecosystem patterns are introduced.

## Prior Art

- **#437 / #441 (`benchmark_hybrid_retrieval.md`, shipped)**: built
  `SentenceTransformersProvider`, the `--retrieval-mode {lexical,hybrid}` flag, the
  hybrid-aware benchmark model, and the assemble-primary `run()`. This plan consumes
  that substrate unchanged and adds only the shared-provider efficiency fix + the
  artifact-naming fix + the actual run.
- **#438 (`external_recall_at_k_any_hit.md`, merged)**: the any-hit Recall@k metric
  that produced the committed lexical baseline (0.856/0.952/0.978/0.899). The hybrid
  number will be measured with the identical metric for an apples-to-apples comparison.
- **#394 (`external_benchmark_harness.md`)**: the `ExternalScenario` / `run_external.py`
  harness and the `save_reports` naming convention this plan extends.

No prior attempt to *run and commit* the hybrid number exists — it was explicitly
deferred. This is the first.

## Data Flow

1. **Entry point**: `python -m tests.benchmarks.run_external --dataset longmemeval-s
   --retrieval-mode hybrid`. `main()` parses `--retrieval-mode hybrid`, threads it into
   `run_item(item, retrieval_mode="hybrid")` for each of the 500 items.
2. **Scenario construction**: `run_item` builds `ExternalScenario(item,
   retrieval_mode="hybrid")`; `setup()` calls `_build_external_model_class(safe_prefix,
   with_embedding=True)`.
3. **Provider (the efficiency fix)**: `_build_external_model_class` obtains the embedding
   provider from a **module-level shared accessor** (`_get_shared_provider()`) instead of
   constructing `SentenceTransformersProvider()` inline. The first item loads MiniLM
   once; every subsequent item reuses the same loaded model.
4. **Ingestion**: each turn's `content` is saved; `EmbeddingField.on_save` →
   `provider.embed([...])` → 384-dim vector → `.npy` under
   `POPOTO_CONTENT_PATH/.embeddings/{ClassName}/`.
5. **Retrieval**: `run()` calls `assemble()` (primary path); with BM25 + Embedding
   fields present and `retrieval_mode="auto"`, the assembler resolves to `hybrid` and
   runs `_pull_path_hybrid()` (RRF k=60, numpy cosine, no Redis module).
6. **Aggregation & output**: `compute_aggregate(...)` rolls up Recall@1/5/10 + MRR + p50/p95;
   `save_reports(aggregate, dataset_slug, retrieval_mode="hybrid")` writes
   `longmemeval_s_{date}_hybrid.{json,md}` and `longmemeval_s_latest_hybrid.{json,md}` —
   **never** touching the lexical `longmemeval_s_{date}.*` / `_latest.*` artifacts.

## Architectural Impact

- **New dependencies**: none. Reuses the shipped `[benchmark]` extra.
- **Interface changes**: additive/internal only. `save_reports` gains a
  `retrieval_mode: str = "lexical"` kwarg (default preserves current lexical naming and
  `_latest` behavior exactly — existing lexical callers/output unchanged). New
  module-level `_get_shared_provider()` in `external_base.py`.
- **Coupling**: a single shared provider instance is reused across the per-item model
  classes. The provider is stateless apart from the loaded model (encode text → vector),
  so sharing across `ExtMem{prefix}` classes is safe — no cross-item state leaks.
- **Reversibility**: high. The shared-provider accessor and the `save_reports` suffix
  are localized; reverting them restores per-item construction and unsuffixed names.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0-1 (only if the hybrid run wall-clock or an unexpected number warrants
  a heads-up — see Open Questions).
- Review rounds: 1 (the code surface is tiny; the run + recorded number is the substance).

Implementation code is ~1-2 hours; the **500-question hybrid run is the long pole**
(CPU MiniLM embedding of ~275k turns — expect tens of minutes to a few hours of
wall-clock). The Medium appetite reflects the run duration and the care needed to
preserve the committed lexical baseline, not implementation complexity.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `[benchmark]` extra installed | `python -c "import sentence_transformers, numpy"` | Local MiniLM + numpy cosine |
| Redis/Valkey on :6379 | `redis-cli ping` | Benchmark ingestion target |
| MiniLM cached (one-time ~90MB) | `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` | Pre-download so the run isn't blocked on HF |
| Full LongMemEval-S dataset reachable | (the adapter downloads, or `--fixture PATH`) | 500 questions / ~275k turns |

## Solution

### Key Elements

- **Shared provider instance (the efficiency fix)**: add a module-level cached accessor
  in `external_base.py` (e.g. `_SHARED_PROVIDER = None; def _get_shared_provider(): ...`
  returning a single `SentenceTransformersProvider`). `_build_external_model_class` uses
  it for the `EmbeddingField` provider in hybrid mode. MiniLM loads once for the whole
  run, not 500×.
- **Mode-suffixed artifacts (the preservation fix)**: extend `save_reports` to accept
  `retrieval_mode` and, for any non-lexical mode, suffix it into both the dated filename
  and the `_latest` name: `longmemeval_s_{date}_hybrid.{json,md}` and
  `longmemeval_s_latest_hybrid.{json,md}`. Lexical mode keeps the exact current names
  (no suffix), so the committed `longmemeval_s_20260630.*` baseline and the lexical
  `_latest` symlinks are never overwritten. Thread `args.retrieval_mode` into the
  `save_reports(...)` call at `run_external.py:628`.
- **Run + commit the artifact**: execute the full 500-question hybrid run, commit
  `longmemeval_s_{date}_hybrid.{json,md}` (and the `_latest_hybrid` pointers if not
  symlinks).
- **Fill the docs table + note**: replace `docs/benchmarks.md:155` `— see note` with the
  measured R@1/R@5/R@10/MRR, and replace the pending-run note (lines 158-167) with a
  real three-way comparison (hybrid vs lexical BM25 vs agentmemory reference).

### Flow

`--retrieval-mode hybrid` → shared provider loaded once → 500 items embed + assemble
(hybrid RRF) → aggregate → `save_reports(..., retrieval_mode="hybrid")` writes
`*_hybrid` artifacts (lexical baseline untouched) → commit artifact → fill the docs
Retrieval Modes table + rewrite the note with the real comparison.

### Technical Approach

- **`external_base.py`**: add module-level `_SHARED_PROVIDER` + `_get_shared_provider()`
  (lazy singleton). In `_build_external_model_class`, replace
  `provider=SentenceTransformersProvider()` (line 107) with
  `provider=_get_shared_provider()`. No other scenario logic changes — `setup()`,
  `run()`, `teardown()` are untouched. (Note: `teardown()` already `rmtree`s the
  `.embeddings/{ClassName}/` dir; the shared provider holds no per-item disk state, so
  cleanup is unaffected.)
- **`run_external.py`**: change `save_reports(aggregate, dataset_slug, dry_run=False)`
  to `save_reports(aggregate, dataset_slug, retrieval_mode="lexical", dry_run=False)`.
  Build a `suffix = "" if retrieval_mode == "lexical" else f"_{retrieval_mode}"` and
  weave it into `json_name`, `md_name`, and the two `_latest` names. Pass
  `retrieval_mode=args.retrieval_mode` at the call site (line 628). The aggregate
  already carries `retrieval_mode` (set at line 258), so the function could also read it
  from there — an explicit kwarg is clearer and keeps `save_reports` self-describing.
- **Running**: prefer the default output dir now that names don't collide. Pre-cache the
  model first (Prerequisites). If the run must be resumable/observable, run in the
  background and monitor; the harness already prints progress.
- **Docs**: fill `docs/benchmarks.md:155` with the measured numbers; rewrite lines
  158-167 into a real comparison paragraph. Keep the Valkey-safety statement
  (line 136+). Update the "Report Artifacts" listing (lines 105-115) to mention the
  `*_hybrid` naming if helpful.

## Failure Path Test Strategy

### Exception Handling Coverage
- `save_reports` with `retrieval_mode="hybrid"` must not raise and must not write to any
  lexical filename — assert via a unit test that the returned paths contain `_hybrid`
  and that a pre-existing `longmemeval_s_{date}.json` is left byte-identical.
- The shared-provider accessor must return the **same object** on repeated calls
  (identity assertion) — guards against an accidental per-call construction regression.

### Empty/Invalid Input Handling
- `save_reports(retrieval_mode="lexical")` must produce the **exact** current filenames
  (no suffix) — a regression test pinning backward compatibility.
- A hybrid run over an all-empty history still yields `status="skipped-empty"` (existing
  behavior, unaffected by the shared provider) — assert it still holds.

### Error State Rendering
- After a hybrid run, the report `notes`/header reflect mode `hybrid` (existing
  mode-aware reporting); assert the `*_hybrid` JSON's `summary.retrieval_mode == "hybrid"`.

## Test Impact

- [ ] `tests/benchmarks/test_external.py` — ADD: `save_reports` naming tests
  (lexical → unsuffixed, hybrid → `_hybrid`, lexical baseline not clobbered when a
  hybrid run follows). Use a `tmp_path` / monkeypatched `RESULTS_DIR`.
- [ ] `tests/benchmarks/test_external.py` — ADD: `_get_shared_provider()` returns a
  cached singleton (identity across calls).
- [ ] Existing hybrid smoke test(s) from #441 — REVIEW: confirm they still pass with the
  shared provider (they should; behavior is unchanged, only instance lifetime differs).

No existing test asserts per-item provider construction, so the singleton change breaks
nothing. The `save_reports` signature gains a defaulted kwarg — existing callers
unaffected.

## Rabbit Holes

- **Tuning RRF / weights to beat the reference.** `RRF_K = 60` is intentionally fixed
  in the substrate. Report whatever the default produces. Tuning is a separate effort.
- **Building a general embedding-cache / warm-start abstraction.** The fix is a single
  module-level instance, not a caching framework. Don't over-engineer.
- **Speeding up CPU embedding with batching/threading gymnastics.** The shared-instance
  fix is the agreed scope. If wall-clock is still painful, run in the background — do
  not build a parallel-embedding pipeline.
- **Refactoring `save_reports` output layout broadly** (per-mode subdirs, run manifests,
  etc.). Minimal change: a mode suffix on the filename. Nothing more.
- **Re-running or re-recording the lexical baseline.** It is committed and correct;
  leave `longmemeval_s_20260630.*` and its row untouched.

## Risks

### Risk 1: Hybrid run is slow / gets interrupted (the original deferral reason)
**Impact:** A multi-hour CPU run that dies partway leaves no committed number.
**Mitigation:** The shared-provider fix removes the dominant repeated cost (500× model
reload). Pre-cache the model before starting. Run in the background and monitor so an
interactive timeout doesn't kill it. If still impractical, surface to PM (Open
Questions) rather than committing a partial/`--limit` number as if it were the full run.

### Risk 2: Accidentally clobbering the committed lexical baseline
**Impact:** Overwriting `longmemeval_s_20260630.{json,md}` or repointing lexical
`_latest` would corrupt the baseline the comparison depends on.
**Mitigation:** The `save_reports` mode-suffix is the structural guard (hybrid writes
`*_hybrid` only). A unit test asserts a hybrid save leaves a pre-existing lexical file
untouched. As defense-in-depth, the run can also use `--output` to a scratch dir, then
copy the artifact in under its `_hybrid` name. `git status` before commit confirms only
`*_hybrid` artifacts + `docs/benchmarks.md` are staged.

### Risk 3: Valkey-compatibility regression
**Impact:** Any vector path using a Redis module (`FT.*`/`BF.*`/`CMS.*`) breaks Valkey.
**Mitigation:** No code path changes here touch retrieval mechanics — the shared
provider returns the same vectors through the same numpy-cosine substrate shipped in
#441. A Verification grep asserts no module commands in the changed files.

### Risk 4: Stray large artifacts committed by accident
**Impact:** `data/doc_embeddings.json` (~78MB, untracked) and `.embeddings/` per-run
`.npy` files are run byproducts; committing them would bloat the repo.
**Mitigation:** Only `tests/benchmarks/results/external/longmemeval_s_*_hybrid.{json,md}`
and `docs/benchmarks.md` are committed. Confirm `data/` and `.embeddings/` are
gitignored (or excluded from the commit). `git status` review before commit.

## Race Conditions

None. Ingestion and retrieval are synchronous and single-threaded per item; items run
sequentially. The shared provider is read-only after its one-time model load (each
`embed()` is an independent forward pass), so reuse across sequential items introduces
no shared-mutable-state hazard. Each turn's `.npy` is written in `setup()` before
`run()` queries — the setup-before-run ordering already guarantees the embedding matrix
is complete at query time.

## No-Gos (Out of Scope)

- Re-running or altering the committed **lexical** baseline (`longmemeval_s_20260630.*`,
  the lexical Retrieval Modes row). It stays exactly as-is.
- Tuning `RRF_K` or introducing configurable fusion weights — fixed in the substrate; a
  separate experiment if ever pursued.
- Gating a hybrid run in CI behind a pre-cached HF model step. The issue lists this as
  **Optional**; it is deferred. If desired later, file a separate issue — do not expand
  this plan's scope into CI workflow changes.
- Committing run byproducts (`data/doc_embeddings.json`, `.embeddings/` `.npy` files).
- Any change to `SentenceTransformersProvider`'s own implementation beyond sharing one
  instance (it already lazy-loads and caches on the instance).

## Update System

No update/deploy-system changes. Internal benchmark + docs only; the provider lives
behind the already-declared `[benchmark]` extra.

## Agent Integration

None. The benchmark harness is a developer CLI; no agent/MCP tool surface is touched.

## Documentation

### Feature Documentation
- [ ] `docs/benchmarks.md:155` — replace the hybrid row `— see note` with the measured
  `R@1 / R@5 / R@10 / MRR`.
- [ ] `docs/benchmarks.md:158-167` — replace the pending-run note with a real comparison
  paragraph: hybrid vs lexical BM25 (0.856/0.952/0.978/0.899) vs agentmemory reference
  (—/0.952/0.986/0.882), including a one-line read on whether hybrid moved R@5/R@10
  given the lexical baseline already matches the reference R@5.
- [ ] `docs/benchmarks.md:105-115` (Report Artifacts) — optionally note the
  `*_hybrid` artifact naming so the committed file is discoverable.

### External Documentation Site
- [ ] `mkdocs build --strict` passes after the edits (`scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] `save_reports` docstring documents the new `retrieval_mode` kwarg and the
  suffix/`_latest` behavior.
- [ ] A short comment on `_get_shared_provider()` explaining why the instance is shared
  (avoid 500× model reload).

## Success Criteria

- [x] `external_base.py` reuses a single shared `SentenceTransformersProvider` across all
  items (module-level accessor); MiniLM loads once per run, not per item (asserted by a
  singleton-identity test).
- [x] `save_reports` writes hybrid artifacts as `longmemeval_s_{date}_hybrid.{json,md}`
  + `longmemeval_s_latest_hybrid.{json,md}`, and lexical runs keep the exact current
  unsuffixed names (backward-compat test passes).
- [x] A hybrid save does **not** modify any pre-existing lexical `longmemeval_s_{date}.*`
  / `_latest.*` artifact (asserted by a test).
- [x] The full 500-question LongMemEval-S hybrid run completes and its artifact is
  committed under the `*_hybrid` name.
- [x] `docs/benchmarks.md` hybrid row is filled with the real R@1/R@5/R@10/MRR, and the
  pending-run note is replaced with a real hybrid-vs-lexical-vs-reference comparison.
- [x] No Redis/Valkey module commands introduced (Verification grep passes).
- [x] Tests pass (`/do-test`); `mkdocs build --strict` passes (`/do-docs`).

## Step by Step Tasks

### 1. Share one provider instance across items
- **Task ID**: share-provider
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py` (add singleton-identity test)
- Add module-level `_SHARED_PROVIDER`/`_get_shared_provider()` to `external_base.py`.
- Replace the inline `SentenceTransformersProvider()` (line 107) with
  `_get_shared_provider()`.
- Add a unit test asserting `_get_shared_provider()` returns the same object across calls.

### 2. Mode-suffix benchmark artifacts (preserve the lexical baseline)
- **Task ID**: suffix-artifacts
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py` (naming + non-clobber tests)
- Add `retrieval_mode: str = "lexical"` to `save_reports`; suffix `_hybrid` (any
  non-lexical mode) into the dated and `_latest` filenames; lexical stays unsuffixed.
- Thread `retrieval_mode=args.retrieval_mode` into the `save_reports(...)` call.
- Add tests: lexical → unsuffixed; hybrid → `_hybrid`; pre-existing lexical file
  untouched after a hybrid save.
- Update the `save_reports` docstring.

### 3. Pre-cache the model and run the full hybrid benchmark
- **Task ID**: run-hybrid
- **Depends On**: share-provider, suffix-artifacts
- **Validates**: a committed `longmemeval_s_{date}_hybrid.{json,md}` with 500 questions
- Verify Prerequisites (extra installed, Redis up, MiniLM cached, dataset reachable).
- Run `python -m tests.benchmarks.run_external --dataset longmemeval-s
  --retrieval-mode hybrid` (background + monitor for the long run).
- Confirm the artifact summary has `retrieval_mode == "hybrid"`, `n_total == 500`, and
  an acceptable error rate; commit the `*_hybrid` artifact only.

### 4. Fill the docs table and comparison note
- **Task ID**: docs-compare
- **Depends On**: run-hybrid
- **Validates**: `mkdocs build --strict`
- Fill `docs/benchmarks.md:155` hybrid row with the measured numbers.
- Replace the pending-run note (158-167) with the real three-way comparison.
- `scripts/ci-local.sh docs`.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: docs-compare
- Run the benchmark test subset + lint/format.
- Valkey-safety grep on changed files.
- Confirm `git status` stages only `*_hybrid` artifacts + `docs/benchmarks.md` (no
  `data/`, no `.embeddings/`).
- Confirm all Success Criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark tests pass | `pytest tests/benchmarks/test_external.py -q` | exit code 0 |
| Shared provider is a singleton | `python -c "from tests.benchmarks.scenarios.external_base import _get_shared_provider as g; print(g() is g())"` | `True` |
| Provider no longer constructed inline | `grep -c "provider=SentenceTransformersProvider()" tests/benchmarks/scenarios/external_base.py` | `0` |
| Hybrid artifact committed | `ls tests/benchmarks/results/external/longmemeval_s_*_hybrid.json` | one match |
| Hybrid artifact is the full run | `python -c "import json,glob; d=json.load(open(sorted(glob.glob('tests/benchmarks/results/external/longmemeval_s_*_hybrid.json'))[-1])); print(d['summary']['retrieval_mode'], d['summary']['n_total'])"` | `hybrid 500` |
| Lexical baseline untouched | `git status --porcelain tests/benchmarks/results/external/longmemeval_s_20260630.json` | empty (no change) |
| Docs hybrid row filled (anti-criterion) | `grep -c "— see note" docs/benchmarks.md` | `0` |
| No Redis vector-module commands | `grep -rEn "FT\.|BF\.|CMS\." tests/benchmarks/scenarios/external_base.py tests/benchmarks/run_external.py` | match count `0` |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

_Recommended defaults proposed; proceed unless PM overrides:_

1. **Artifact naming — RECOMMENDED: suffix the mode in `save_reports`** (`*_hybrid`),
   keeping lexical unsuffixed for backward compatibility. (The issue offered this or
   `--output` + manual rename; the suffix is durable and self-documenting.)
2. **Does the hybrid `_latest` symlink set get its own `_latest_hybrid` pointers?**
   RECOMMENDED: yes — a parallel `_latest_hybrid` so neither mode's "latest" clobbers
   the other.
3. **If the full CPU run proves impractical even after the shared-provider fix** (still
   many hours), do we (a) accept the wall-clock and run it, or (b) escalate before
   committing? RECOMMENDED: accept and run in the background; escalate only if it can't
   complete in a single session. Do **not** commit a `--limit` partial as the headline
   number.
