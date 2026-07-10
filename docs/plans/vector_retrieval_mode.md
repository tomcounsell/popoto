---
status: Ready
type: feature
appetite: Small
owner: valor
created: 2026-07-10
tracking: https://github.com/tomcounsell/popoto/issues/455
last_comment_id: 0
revision_applied: false
---

# Retrieval-arm diagnostics: vector-only baseline (`--retrieval-mode vector`)

## Problem

The full LoCoMo run (#447) found hybrid (BM25 + vector, RRF k=60)
**underperforming** lexical on every metric (R@1 0.1667 vs 0.2986, MRR 0.2835 vs
0.4124) — the inverse of LongMemEval-S, where hybrid wins on every metric. The
working hypothesis is that on coreference-heavy multi-session dialogue the vector
arm injects topically-similar-but-wrong candidates, and unweighted RRF gives that
weak-but-confident arm equal say with BM25.

**Current behavior:** `run_external.py` supports `--retrieval-mode lexical`
(BM25 only) and `--retrieval-mode hybrid` (BM25 + vector via RRF). There is **no
way to measure the vector arm in isolation.** Without a vector-only baseline we
cannot tell whether the LoCoMo regression comes from the *arm* (vector recall is
genuinely weak on dialogue) or from the *fusion* (RRF blends two comparable arms
badly).

**Desired outcome:** a third `--retrieval-mode vector` that ranks by pure cosine
over the `EmbeddingField` `.npy` embeddings — no BM25, no RRF — with artifacts
suffixed `_vector` so they never touch the committed lexical or hybrid baselines.
Running it on both datasets isolates the vector arm and confirms or refutes the
weak-arm hypothesis. **Measurement only — no retrieval tuning, no changes to the
hybrid/lexical paths.**

## Freshness Check

**Baseline commit:** `3855053` (`docs(plans): adopt 2026-07-10 benchmarking
strategy as roadmap of record for epic #456`) — current `origin/main` HEAD.
**Issue filed at:** 2026-07-03 (reshaped under epic #456, 2026-07-10 review).
**Disposition:** Unchanged — premise intact.

**File:line references re-verified against `origin/main` (`3855053`):**
- `tests/benchmarks/run_external.py:113` — `run_item(item, retrieval_mode=...)`
  threads the mode into `ExternalScenario`. **Confirmed.**
- `tests/benchmarks/run_external.py:240-253` — `compute_aggregate` branches
  `mode_notes` on `"hybrid"` vs else(lexical). Needs a `"vector"` branch.
  **Confirmed.**
- `tests/benchmarks/run_external.py:373-384` — `build_markdown_report` closing
  paragraph branches `hybrid` vs else. Needs a `"vector"` branch. **Confirmed.**
- `tests/benchmarks/run_external.py:419-452` — `save_reports` suffix logic:
  `suffix = "" if retrieval_mode == "lexical" else f"_{retrieval_mode}"`. Already
  generic — `vector` → `_vector` with **no change**. **Confirmed.**
- `tests/benchmarks/run_external.py:495-505` — `--retrieval-mode` `choices=
  ("lexical", "hybrid")`. Needs `"vector"` added. **Confirmed.**
- `tests/benchmarks/scenarios/external_base.py:100-159` —
  `_build_external_model_class(safe_prefix, with_embedding=...)` builds BM25-only
  (lexical) or BM25+Embedding (hybrid). Needs an **Embedding-only** shape for
  vector. **Confirmed.**
- `tests/benchmarks/scenarios/external_base.py:195-217` — `setup()` builds the
  model and a `ContextAssembler(retrieval_mode="auto")`. For vector we skip the
  assembler (see Data Flow / Solution). **Confirmed.**
- `tests/benchmarks/scenarios/external_base.py:257-356` — `run()` drives
  `assemble()`. Needs a vector branch that ranks by pure cosine. **Confirmed.**
- `src/popoto/models/query.py:995-1067` — `QueryBuilder._get_vector_scores(
  query_text, limit)` returns `[(redis_key, cosine), ...]` sorted descending,
  positive similarities only, via `matrix @ query_vec` over
  `EmbeddingField.load_embeddings()`. This is the **exact pure-cosine primitive**
  the hybrid vector arm uses (`context_assembler.py:1308`). Reuse it verbatim so
  vector-only numbers are directly comparable to the hybrid vector arm.
  **Confirmed.**
- `src/popoto/recipes/context_assembler.py:946-954` — `auto` mode resolution:
  BM25+Embedding→hybrid, BM25-only→lexical, **neither** (incl. embedding-only)→
  `composite` (query-blind). So an EmbeddingField-only model routed through the
  assembler's `auto` mode would resolve to **composite (query-blind)**, NOT
  vector search. This is why vector mode **bypasses the assembler** and calls the
  cosine primitive directly. **Confirmed — load-bearing design constraint.**
- `docs/benchmarks.md:99, 136-147, 176-194` — CLI table, Retrieval Modes section,
  and the LoCoMo tables. Need a `vector` row/paragraph and result slots.
  **Confirmed.**

**Cited sibling issues/PRs re-checked:**
- #442/#443 — shared MiniLM provider (`_get_shared_provider`) + listener teardown
  (`stop_invalidation_listeners()`). **On main.** Vector mode declares an
  `EmbeddingField` and so relies on both fixes; both are already present on the
  shared `ExternalScenario` path.
- #447/#452 — full LoCoMo lexical + hybrid baselines committed; this run adds the
  missing vector arm to the same tables. **On main.**
- #457 — weighted/query-adaptive fusion. This diagnostic **unblocks** it; it is
  not implemented here.

**Notes:** No drift. MiniLM (`all-MiniLM-L6-v2`) is already in the local HF cache,
so no download on the smoke tests or the runs.

## Research

No external research required. The cosine primitive, the shared provider, and the
mode-suffix artifact convention already exist. The only external dependency is the
datasets themselves (LongMemEval-S cleaned JSON already cached; LoCoMo downloads
`snap-research/locomo`/`locomo10.json` on first run — already exercised by #447).

## Prior Art

- **#447/#452 (`locomo_full_benchmark_run.md`, shipped):** committed the full
  lexical + hybrid LoCoMo baselines and the `_hybrid` mode-suffix artifact
  convention. This plan is the vector twin — same harness, same artifact naming,
  one new mode.
- **#442/#443 (`benchmark_hybrid_full_run.md`, shipped):** added the shared MiniLM
  provider and the listener-teardown fix that make a full embedding run practical.
  Vector mode reuses both with zero new plumbing.
- **#437/#395 (ContextAssembler `auto` mode / effective-mode reporting):** defines
  the assembler's field-presence→mode resolution. This plan works **with** that
  design by recognizing that embedding-only resolves to composite (query-blind)
  under the assembler, and therefore routes vector retrieval around the assembler
  to the raw cosine primitive instead.

No prior vector-only run exists — this is the first.

## Data Flow

1. **Entry point:** `python -m tests.benchmarks.run_external --dataset {locomo,
   longmemeval-s} --retrieval-mode vector`. `main()` threads
   `retrieval_mode="vector"` into `run_item` → `ExternalScenario`.
2. **setup():** `_build_external_model_class(prefix, with_bm25=False,
   with_embedding=True)` builds an **EmbeddingField-only** per-item model (no
   `BM25Field`). Each non-empty turn is saved as one record; `EmbeddingField`
   writes one `.npy` per turn (same per-turn granularity as BM25 — see Risks §1).
   The `ContextAssembler` is **not** constructed in vector mode.
3. **run() (vector branch):** build `QueryBuilder(model_class.query)` and call
   `_get_vector_scores(item.query, limit=max_items)` → `[(redis_key, cosine)]`
   sorted descending. Take the redis_keys in rank order as `retrieved_keys`. This
   is pure cosine — no BM25, no RRF, no graph. `retrieval_method="vector"` is
   recorded in metadata.
4. **Scoring:** the existing `redis_key → session_id/turn_id` reverse map
   (unchanged) converts `retrieved_keys` to ground-truth ids; `run_item` computes
   R@1/5/10 + MRR exactly as for the other modes.
5. **teardown():** unchanged — deletes records, scans by class/agent prefix,
   removes the per-class embedding dir, and calls `stop_invalidation_listeners()`
   (the #442 connection-leak fix). Runs for vector mode because an EmbeddingField
   is present.
6. **Artifacts:** `save_reports(..., retrieval_mode="vector")` writes
   `{slug}_{date}_vector.{json,md}` and `{slug}_latest_vector.{json,md}` via the
   already-generic suffix logic — never touches lexical/hybrid artifacts.

## Appetite

**Size:** Small. **Team:** Solo dev + code reviewer. **Interactions:** 1 review
round; 0-1 PM check-ins (only for run wall-clock).

Code surface is small and confined to `tests/benchmarks/`: one new mode string
threaded through the CLI + aggregate + report, one new model-builder branch
(embedding-only), one new `run()` retrieval branch (reuse the existing cosine
primitive), plus tests and a docs table. The long pole is the **LoCoMo vector run
wall-clock** (CPU MiniLM embedding of ~26k turns) — launched detached, not on the
PR critical path.

## Prerequisites

| Requirement | Check Command | Purpose | Status |
|-------------|---------------|---------|--------|
| `[benchmark]` extra installed | `python -c "import huggingface_hub, sentence_transformers, numpy"` | embedding + cosine | expected ✅ |
| Redis/Valkey on :6379 | `redis-cli ping` | ingestion target | expected ✅ |
| MiniLM cached (~90MB) | `try_to_load_from_cache('sentence-transformers/all-MiniLM-L6-v2', 'config.json')` | vector signal | ✅ present locally |
| LoCoMo dataset | adapter downloads on first run | ~1986 QA over 10 dialogues | ✅ exercised by #447 |

## Solution

### Key Elements

- **CLI:** add `"vector"` to `--retrieval-mode` `choices`; extend the help text.
- **Model builder:** generalize `_build_external_model_class(safe_prefix,
  with_bm25=True, with_embedding=False)` so vector mode gets `with_bm25=False,
  with_embedding=True` → an **EmbeddingField-only** model. Lexical
  (`with_bm25=True, with_embedding=False`) and hybrid (`True, True`) are
  byte-behavior-unchanged.
- **Scenario:** in `setup()`, choose field flags from `retrieval_mode` and skip
  the `ContextAssembler` when mode is `vector`. In `run()`, add a `vector` branch
  that ranks via `QueryBuilder._get_vector_scores(query, limit=max_items)` (pure
  cosine) and records `retrieval_method="vector"`. Lexical/hybrid paths untouched.
- **Aggregate + report:** add a `"vector"` branch to `compute_aggregate`'s
  `mode_notes` and to `build_markdown_report`'s closing paragraph. R@1/5/10 + MRR
  are already computed uniformly, so the full metric vector is reported for vector
  mode with no extra work (satisfies the addendum's "full metric-vector
  reporting").
- **Artifacts:** none — `save_reports`' `suffix = "" if lexical else f"_{mode}"`
  already yields `_vector`.
- **Runs:** launch both datasets detached (`nohup … & disown`); commit artifacts
  and fill `docs/benchmarks.md` when they land. The PR merges on the code +
  methodology; numbers slot in after.

### Granularity-parity audit (addendum, read-only)

The 2026-07-10 review flagged BM25/embedding **granularity mismatch** as the
leading suspect for the LoCoMo hybrid regression. Read-only finding from the
harness: in `_build_external_model_class`, both `BM25Field(source="content")` and
`EmbeddingField(source="content")` are declared on the **same** per-turn
`ExternalBenchmarkMemory`, and `setup()` saves **one instance per non-empty
turn**. So both arms index exactly one unit per turn, from the identical
`content` string — **parity confirmed, no mismatch.** The hybrid regression is
therefore not a granularity artifact; the vector-only run tests the remaining
(weak-arm / fusion) hypotheses. This finding is recorded in the plan, the issue,
and `docs/benchmarks.md` — it is documentation, not a code change.

### Technical Approach

- Reuse `_get_vector_scores` (the hybrid vector arm's own primitive) rather than
  `semantic_search()` — the latter blends memory signals (composite), which would
  contaminate a "pure cosine" baseline. Using the identical primitive keeps the
  vector-only numbers directly comparable to the vector contribution inside
  hybrid.
- Retrieve `limit=max_items` (20, matching the assembler's `max_items`) so R@10 is
  well-defined and the candidate depth matches the other modes.
- Detached runs: `nohup python -m tests.benchmarks.run_external --dataset X
  --retrieval-mode vector > logs/… 2>&1 & disown`. Monitor progress (the harness
  logs every 10 items). Do not commit a `--limit` partial as the headline.
- Commit hygiene: stage only `tests/benchmarks/results/external/*_vector.*` and
  `docs/benchmarks.md`; never `data/`, `.embeddings/`, or the cached dataset.

### Flow

`--retrieval-mode vector` → embedding-only model per item → per-turn `.npy` →
`_get_vector_scores(query)` pure cosine → ranked redis_keys → map to session/turn
ids → R@1/5/10 + MRR → `save_reports(..., "vector")` writes `_vector` artifacts →
fill `docs/benchmarks.md` → `mkdocs build --strict`.

## Rabbit Holes

- **Adding a `vector` effective mode to `ContextAssembler`.** Out of scope and
  higher-risk (core library API, its own test surface). Vector-only retrieval is a
  measurement concern; keep it in the benchmark harness and reuse the existing
  cosine primitive.
- **Tuning to improve the vector numbers** (weighted fusion, RRF_K, different
  model, normalization changes). Explicitly out of scope — #457 owns fusion.
- **Adding nDCG.** The addendum says "consider"; deferred to keep the metric set
  identical to the already-committed lexical/hybrid artifacts (which have no
  nDCG). Adding it would require recomputing all three modes for parity — a
  separate change.
- **Refactoring `save_reports` / artifact layout.** Already generic; touch
  nothing.
- **Re-running or altering lexical/hybrid.** Leave the committed baselines byte-
  identical.
- **Committing run byproducts** (`data/`, `.embeddings/` `.npy`, cached dataset).

## Risks

### Risk 1: Granularity mismatch invalidates the comparison
**Impact:** If BM25 and embedding indexed different units, the vector-only numbers
would not be comparable to lexical/hybrid.
**Mitigation:** Audited (see Granularity-parity audit) — both arms index one
per-turn record from the same `content` source. Parity confirmed; documented.

### Risk 2: Embedding-only model routed through the assembler silently degrades to composite
**Impact:** `ContextAssembler(auto)` resolves embedding-only → `composite`
(query-blind), which would produce meaningless "vector" numbers.
**Mitigation:** Vector mode **bypasses the assembler** and calls
`_get_vector_scores` directly. A test asserts `retrieval_method == "vector"` and
that the returned records actually rank the query-relevant turn first, guarding
against an accidental composite fallback.

### Risk 3: Vector run wall-clock (LoCoMo ~26k turns, CPU embedding)
**Impact:** An interactive timeout could kill a multi-hour run.
**Mitigation:** Launch detached (`nohup … & disown`), monitor via the per-10-item
progress log. The PR does not block on the run; numbers land afterward.

### Risk 4: Connection-pool exhaustion from per-item embedding listeners
**Impact:** Each `EmbeddingField` starts a PubSubWorkerThread holding a pool
connection; a fresh class per item would exhaust the pool (~item 120) as it did
pre-#442.
**Mitigation:** `teardown()` already calls `stop_invalidation_listeners()` per
item (the #442 fix); vector mode runs the same teardown. No new plumbing.

### Risk 5: Valkey-compatibility regression
**Impact:** Any `FT.*`/`BF.*`/vector-module dependency breaks Valkey.
**Mitigation:** `_get_vector_scores` is in-process numpy cosine over `.npy`
files — the same substrate hybrid uses. A verification grep confirms no module
commands are introduced.

## Race Conditions

None. Ingestion and retrieval are synchronous, single-threaded, one item at a
time. The shared MiniLM provider is read-only after its one-time load. Each turn
is ingested in `setup()` before `run()` queries; `teardown()` releases the
listener connection per item.

## No-Gos (Out of Scope)

- Any retrieval-quality change to lexical or hybrid (tuning, fields, fusion
  weights, RRF_K, metric semantics).
- Adding a `vector` mode to `ContextAssembler` or any `src/popoto/` change.
- nDCG (deferred).
- Committing run byproducts or the cached dataset.
- Implementing #457 (weighted/query-adaptive fusion) — this only unblocks it.

## Update System

No update/deploy-system changes. Internal benchmark harness + docs only; the
vector path lives behind the already-declared `[benchmark]` extra, exactly like
hybrid.

## Agent Integration

None. The benchmark harness is a developer CLI; no agent/MCP tool surface is
touched.

## Documentation

### Feature Documentation
- [ ] `docs/benchmarks.md` — add `vector` to the CLI `--retrieval-mode` row
  (line ~99), add a `vector` row to the Retrieval Modes field-presence table
  (lines ~142-143) with the EmbeddingField-only / pure-cosine / no-RRF
  description, and add a run example.
- [ ] `docs/benchmarks.md` — add a vector column/row to the LongMemEval-S and
  LoCoMo Retrieval Modes comparison tables; record the granularity-parity finding
  as a note. Populate numbers when the detached runs finish (placeholder + "run
  pending" note at merge time is acceptable per the ops lesson).

### External Documentation Site
- [ ] `mkdocs build --strict` passes (`scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstrings on `_build_external_model_class`, `ExternalScenario`, and
  `run_item` updated to describe the three modes including vector (pure cosine,
  EmbeddingField-only, assembler-bypassed).

## Success Criteria

- [ ] `--retrieval-mode vector` accepted by the CLI; help text describes it.
- [ ] Vector mode builds an **EmbeddingField-only** per-item model (no BM25Field)
  and ranks by pure cosine via `_get_vector_scores`; `retrieval_method ==
  "vector"` in metadata.
- [ ] `save_reports(..., "vector")` writes `{slug}_{date}_vector.{json,md}` and
  `{slug}_latest_vector.{json,md}`; a vector save leaves any pre-existing
  lexical/hybrid artifact byte-identical.
- [ ] Lexical and hybrid paths are behavior-unchanged (existing contract tests
  still pass).
- [ ] Granularity-parity finding documented (both arms = one per-turn record from
  `content`).
- [ ] New narrow tests pass: vector-mode smoke contract (skipif MiniLM absent) +
  `_vector` artifact naming + no-clobber.
- [ ] Full-dataset vector runs launched detached on both datasets; artifacts +
  `docs/benchmarks.md` numbers land when they finish (may post-date the merge).
- [ ] No Redis/Valkey module commands introduced (verification grep passes).
- [ ] `mkdocs build --strict` passes.

## Step by Step Tasks

### 1. Model builder: embedding-only shape
- **Task ID**: model-builder
- Generalize `_build_external_model_class` to `with_bm25`/`with_embedding` flags;
  add the BM25-absent + Embedding-present branch. Keep lexical/hybrid identical.

### 2. Scenario: vector retrieval branch
- **Task ID**: scenario-vector
- **Depends On**: model-builder
- In `setup()`, pick field flags from `retrieval_mode`; skip the assembler for
  vector. In `run()`, add the vector branch (pure cosine via `_get_vector_scores`,
  `retrieval_method="vector"`). Update docstrings.

### 3. CLI + aggregate + report
- **Task ID**: cli-report
- **Depends On**: scenario-vector
- Add `"vector"` to `--retrieval-mode` choices + help; add `vector` `mode_notes`
  and report closing paragraph. Update `run_item` docstring.

### 4. Tests
- **Task ID**: tests
- **Depends On**: cli-report
- Add `test_vector_effective_mode_and_records` (skipif MiniLM absent) asserting
  `retrieval_method == "vector"`, embedding-only model, relevant turn retrieved.
  Add `test_vector_names_are_suffixed` + `test_vector_save_does_not_clobber_
  lexical_baseline` to `TestSaveReportsArtifactNaming`. Run the benchmark test
  module only.

### 5. Docs
- **Task ID**: docs
- **Depends On**: tests
- Update `docs/benchmarks.md` (CLI row, Retrieval Modes table, comparison tables,
  granularity-parity note). `scripts/ci-local.sh docs`.

### 6. Detached full runs
- **Task ID**: runs
- **Depends On**: tests
- Launch `--retrieval-mode vector` on both datasets detached. Do not block the PR.
  Commit `_vector` artifacts + fill the docs numbers when they finish.

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: docs
- Valkey-safety grep; `git status` stages only harness code + tests + docs (+
  `_vector` artifacts when present); confirm Success Criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| CLI accepts vector | `python -m tests.benchmarks.run_external --dataset longmemeval-s --retrieval-mode vector --help` | exit 0, lists `vector` |
| Vector smoke contract | `pytest tests/benchmarks/test_external.py -k vector -q` | pass (or skip if MiniLM absent) |
| Artifact suffix | `pytest tests/benchmarks/test_external.py -k "vector and (suffix or clobber)" -q` | pass |
| Lexical/hybrid contract intact | `pytest tests/benchmarks/test_external.py -k "Contract or ArtifactNaming" -q` | pass |
| No harness src change | `git status --porcelain src/popoto/` | empty |
| No Redis module commands | `grep -rEn "FT\.\|BF\.\|CMS\." docs/benchmarks.md tests/benchmarks/scenarios/external_base.py tests/benchmarks/run_external.py` | 0 matches |
| Docs build | `mkdocs build --strict` | exit 0 |

## Critique Results

<!-- Populated by critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Decisions

1. **Retrieval primitive.** DECIDED: reuse `QueryBuilder._get_vector_scores`
   (pure cosine, the hybrid vector arm's own primitive) rather than
   `semantic_search` (blends memory signals). Keeps vector-only comparable to the
   hybrid vector arm.
2. **Assembler vs bypass.** DECIDED: bypass `ContextAssembler` for vector mode —
   its `auto` resolution sends embedding-only to composite (query-blind). Vector
   retrieval calls the cosine primitive directly.
3. **nDCG.** DECIDED: deferred (parity with existing artifacts; separate change).
4. **Candidate depth.** DECIDED: `limit=max_items` (20), matching the assembler's
   `max_items`, so R@10 is well-defined and depth matches other modes.
