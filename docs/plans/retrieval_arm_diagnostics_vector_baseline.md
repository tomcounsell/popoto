---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-10
tracking: https://github.com/tomcounsell/popoto/issues/455
last_comment_id: 4934211339
---

# Retrieval-arm diagnostics — vector-only baseline + chunk-granularity parity

## Problem

The full LoCoMo run (#447) found **hybrid** retrieval (BM25 + vector, unweighted
RRF k=60) *underperforming* pure **lexical** on every metric
(R@1 0.1667 vs 0.2986, MRR 0.2835 vs 0.4124) — the exact inverse of
LongMemEval-S, where hybrid wins on every metric (R@1 0.894 vs 0.856). For a
live agent this means the current fusion cannot be trusted across conversation
shapes. Issue #457 (weighted / query-adaptive fusion) is the intended fix, but
it is **blocked on this issue**: before touching fusion we must isolate *where*
the LoCoMo regression comes from.

Two competing hypotheses:

1. **Weak-arm / RRF hypothesis** — on coreference-heavy multi-session dialogue
   the dense arm injects topically-similar-but-wrong turns, and unweighted RRF
   gives that weak-but-confident arm equal say with BM25.
2. **Granularity-mismatch hypothesis** (the 2026-07-10 strategy review's
   *leading* suspect) — if BM25 tokenizes at one unit and embeddings embed at
   another, RRF fuses non-comparable ranked lists. A mismatch would invalidate
   any interpretation of the vector numbers, so it must be checked *first*.

The single missing measurement that discriminates between "the arm is weak" and
"the fusion is broken" is **vector-only recall on both datasets** — a baseline
the harness cannot currently produce.

**Current behavior:**
- The external harness (`tests/benchmarks/run_external.py`) supports only
  `--retrieval-mode {lexical, hybrid}`. `ContextAssembler._VALID_MODES` is
  `{"auto", "lexical", "hybrid", "composite"}` — there is **no vector-only
  mode**. An EmbeddingField-only model routed through `retrieval_mode="auto"`
  resolves to `composite` (query-blind — the #409 defect), *not* cosine
  retrieval. So there is no way to measure the dense arm in isolation.
- No artifact records a granularity-parity audit; the leading suspect is
  unverified.

**Desired outcome:**
- A read-only **granularity-parity audit** committed as durable evidence,
  stating whether BM25 and the embedding arm rank the same units.
- A first-class **vector-only** retrieval mode the harness can run, producing
  the full R@1 / R@5 / R@10 / MRR vector on both datasets under the existing
  mode-suffix artifact convention (`{slug}_{date}_vector.*` + `*_latest_vector.*`).
- `docs/benchmarks.md` updated with the vector-only numbers alongside
  lexical/hybrid, so #457 can proceed from a decisive diagnostic.

## Freshness Check

**Baseline commit:** `3b63a7ac7a7e8aa9eca21bcdf7c4edf84541091f`
**Issue filed at:** 2026-07-10T09:50:42Z
**Disposition:** Unchanged (with an Overlap note)

**File:line references re-verified (all still hold):**
- `tests/benchmarks/run_external.py:496-505` — `--retrieval-mode` argparse arg
  with `choices=("lexical","hybrid")`. Confirmed present; this is the CLI
  surface to extend.
- `tests/benchmarks/scenarios/external_base.py:123-159`
  (`_build_external_model_class`) — builds the per-item model; `with_embedding`
  toggles the EmbeddingField. Both `content_index = BM25Field(source="content")`
  and `embedding = EmbeddingField(source="content")` source the **same**
  `content` field on the **same** per-turn record. Confirmed.
- `src/popoto/recipes/context_assembler.py:937`
  (`_VALID_MODES = {"auto","lexical","hybrid","composite"}`) — no `vector`.
  Confirmed.
- `src/popoto/recipes/context_assembler.py:946-954` — auto-resolution: BM25 +
  Embedding → hybrid; BM25 only → lexical; **neither → composite** (so
  Embedding-only → composite). Confirmed.
- `src/popoto/models/query.py:995-1067` (`_get_vector_scores`) — loads *all*
  per-turn embeddings via `EmbeddingField.load_embeddings(model_class)`, cosine
  over the full matrix, returns sorted `(redis_key, score)`. Confirmed: this is
  the pure-cosine primitive a vector mode reuses.

**Cited sibling issues/PRs re-checked:**
- #457 — still OPEN. Its "Gates and criteria" explicitly say **"Blocked on
  #455 … if a granularity mismatch is found, fix that first and re-measure
  hybrid before touching fusion."** This plan is that gate.
- #447 — closed 2026-07-10T09:20 (PR #452 merged). Produced the lexical +
  hybrid LoCoMo artifacts this issue reacts to.
- #442 / #437 — the EmbeddingField listener connection-leak fix and the hybrid
  mode. Confirmed already handled in `external_base.py:411` via
  `stop_invalidation_listeners()` in `teardown()`.

**Commits on main since issue was filed (touching referenced files):** none.
`git log --since=2026-07-10T09:50Z` is empty; HEAD is the #452 merge.

**Active plans in `docs/plans/` overlapping this area:**
- `locomo_full_benchmark_run.md` (#447, just merged) — produced the artifacts
  this issue diagnoses. **Overlap is upstream, not concurrent** — that work
  shipped; this plan consumes its results. No coordination conflict.
- Issue #457 references `docs/plans/benchmarking_strategy_2026-07.md §3.1`,
  which is **not committed** on this branch/main. The strategy doc is context,
  not a dependency; this plan stands alone.

**Notes:** No drift. All file:line pointers are current as of the baseline SHA.

## Prior Art

- **#437 / PR #441** — "hybrid BM25+vector retrieval mode for external
  benchmark." Established the `--retrieval-mode hybrid` path, the shared
  `_SHARED_PROVIDER`, and `assemble()` as the primary retrieval path. The vector
  mode extends exactly this pattern. **Succeeded.**
- **#447 / PR #452** — full LoCoMo lexical + hybrid run; established the
  mode-suffix artifact convention (`save_reports` suffix logic,
  `run_external.py:419-452`). The vector artifacts reuse this convention
  verbatim. **Succeeded.**
- **#442 / PR #443** — full LongMemEval-S hybrid run + harness fixes; fixed the
  EmbeddingField invalidation-listener connection leak
  (`stop_invalidation_listeners()`). The vector run inherits this fix (same
  teardown path). **Succeeded.**
- **#409 / PR #426** — "make BM25 a first-class retrieval mode"; the reason
  `composite` is query-blind. Directly relevant: an Embedding-only model under
  `auto` mode falls into that same query-blind `composite` bucket, which is
  *why* the harness ranks by cosine directly rather than routing an
  embedding-only model through the assembler (which would resolve to the
  query-blind `composite` path, not vector search). **Succeeded.**
- **#457** — the downstream consumer (weighted/query-adaptive fusion). This plan
  is its unblocking gate. **Open.**

No prior *failed* attempts at a vector-only mode exist — this is additive
greenfield within an established harness.

## Research

**Queries used:**
- "reciprocal rank fusion weak dense retrieval arm hurts lexical BM25 hybrid
  weighted RRF"
- "dense retrieval underperforms BM25 conversational multi-session dialogue
  coreference embeddings"

**Key findings:**
- **RRF operates on ranks, not scores** — a weak arm still casts full
  rank-based votes, so an unweighted blend structurally gives a weak-but-confident
  dense arm equal say. Dynamic Weight Adaptation (DAT) / per-query interpolation
  are the established remedies. This validates #457's "weighted RRF" direction
  and confirms the vector-only baseline is the right discriminating measurement.
  ([digitalapplied hybrid-search reference 2026](https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026))
- **Dense underperforms BM25 on surface-matching / adversarial / domain-specific
  queries**, and "which signal answers which queries depends on session length."
  This is precisely the LongMemEval-S (hybrid wins) vs LoCoMo (lexical wins)
  split, and supports interpreting a *weak vector-only LoCoMo number* as the
  weak-arm hypothesis rather than a fusion bug.
  ([arxiv 2503.17507 — Dense Passage Retrieval in Conversational Search](https://arxiv.org/abs/2503.17507))
- A directly on-point paper exists: **Training-Free Lexical–Dense Fusion for
  Conversational-Memory Retrieval** ([arxiv 2606.04194](https://arxiv.org/html/2606.04194)),
  reinforcing that per-query lexical/dense weighting (not a fixed blend) is the
  live-agent-correct answer — i.e., #457's scope, not this plan's.

**How this informs the approach:** the external literature says the vector-only
number is genuinely diagnostic (dense weakness is query-shape-dependent, not a
constant), and that the fix is *weighting*, not re-plumbing the arms. That keeps
this plan strictly measurement + one additive mode, and pushes all tuning to
#457.

## Spike Results

The three assumptions this plan rests on were resolved by first-hand code
reading during planning (code-read spikes, high confidence), not deferred to
build:

### spike-1: Granularity parity — do BM25 and the embedding arm rank the same units?
- **Assumption**: "BM25 and embeddings may index at different granularities
  (the leading suspect for the LoCoMo regression)."
- **Method**: code-read (`external_base.py`, `context_assembler.py`, `query.py`).
- **Finding**: **Parity holds at both layers.** (a) *Model layer*: in
  `_build_external_model_class` both `content_index = BM25Field(source="content")`
  and `embedding = EmbeddingField(source="content")` source the identical
  `content` field of the identical per-turn `ExternalBenchmarkMemory` record
  (`external_base.py:136-140`) — one turn = one BM25 document = one embedding
  vector. (b) *Retrieval layer*: the BM25 arm (`BM25Field.search(model_class,
  …)`, `context_assembler.py:1287`) and the vector arm (`_get_vector_scores` →
  `EmbeddingField.load_embeddings(model_class)`, `query.py:1048`) both enumerate
  the **same full set of per-turn records** of the same model class. There is no
  chunking, windowing, or session-vs-turn discrepancy between the arms.
- **Confidence**: high.
- **Impact on plan**: The granularity-mismatch hypothesis is **refuted for the
  external harness**. Therefore #457's "fix granularity and re-measure hybrid
  first" branch does **not** trigger — we proceed straight to the vector-only
  runs, and the audit deliverable is a written confirmation (with these
  citations) rather than a fix. This *elevates* the weak-arm/RRF hypothesis as
  the remaining explanation to test.

### spike-2: Can a pure-cosine vector-only path reuse existing primitives?
- **Assumption**: "A vector-only ranking can be produced without new numpy /
  server-side vector code."
- **Method**: code-read (`query.py:995-1067`).
- **Finding**: `QueryBuilder._get_vector_scores(query_text, limit)` already does
  exactly this — embeds the query, in-process numpy cosine over
  `load_embeddings(model_class)`, returns sorted `(redis_key, score)`. It is
  Valkey-safe (no `FT.*`), already used by the hybrid arm, and needs no RRF.
- **Confidence**: high.
- **Impact on plan**: the harness calls `QueryBuilder._get_vector_scores`
  directly in `ExternalScenario.run()` — the returned `(redis_key, score)` pairs
  are already sorted by cosine descending, so the redis keys ARE the ranked
  retrieval. No new similarity code and no assembler involvement.

### spike-3: Does auto-mode already give us vector-only for free?
- **Assumption**: "An EmbeddingField-only model under `retrieval_mode='auto'`
  will rank by cosine."
- **Method**: code-read (`context_assembler.py:946-954`).
- **Finding**: **No.** Auto-resolution has no vector branch: BM25+Embedding →
  hybrid; BM25-only → lexical; **neither → composite**. An Embedding-only model
  matches *neither* and resolves to `composite` (query-blind — wrong). So vector
  must be an **explicit** first-class mode.
- **Confidence**: high.
- **Impact on plan**: the CLI exposes `--retrieval-mode vector` and the harness
  drives it entirely — `ExternalScenario` builds an EmbeddingField-only model and
  `run()` ranks by pure cosine, bypassing the assembler. `ContextAssembler` is
  **not** touched (no `_VALID_MODES` entry, no `auto` change), so no production
  default behavior changes (see No-Gos).

## Data Flow

1. **Entry point**: `run_external.py --dataset {locomo|longmemeval-s}
   --retrieval-mode vector` (new choice).
2. **Adapter**: `iter_locomo` / `iter_longmemeval` yield `BenchmarkItem`s
   (unchanged).
3. **Scenario setup** (`external_base.py`): `_build_external_model_class(prefix,
   with_bm25=False, with_embedding=True)` builds a model with **EmbeddingField
   only, no BM25Field**; `setup()` sets `self._assembler = None` (no assembler is
   constructed for vector mode).
4. **Ingest**: one record per non-empty turn (unchanged); embeddings written to
   the per-class `.npy` dir.
5. **Retrieve** (`ExternalScenario.run()`, assembler bypassed):
   `QueryBuilder(model_class.query)._get_vector_scores(query_text, limit)` →
   cosine ranking → the returned `(redis_key, score)` pairs are already sorted by
   similarity descending, so the redis keys ARE the ranked retrieval. No BM25, no
   RRF, no graph, no assembler.
6. **Score** (`run_item`): `recall_at_k` (1/5/10) + `mean_reciprocal_rank`
   (unchanged; optionally + `ndcg_at_k` — see Open Questions).
7. **Aggregate + report** (`compute_aggregate`, `build_markdown_report`,
   `save_reports`): `retrieval_mode="vector"` → mode-notes branch + `_vector`
   artifact suffix.
8. **Output**: `tests/benchmarks/results/external/{slug}_{date}_vector.{json,md}`
   + `{slug}_latest_vector.*`; numbers folded into `docs/benchmarks.md`;
   teardown runs `stop_invalidation_listeners()` (inherited leak fix).

## Architectural Impact

- **New dependencies**: none. Reuses `SentenceTransformersProvider`
  (all-MiniLM-L6-v2), existing numpy cosine, existing artifact plumbing.
- **Interface changes** (harness-local — Open Question 1): `ContextAssembler` is
  **not** modified — no `_VALID_MODES` entry, no `_pull_path_vector`, no `auto`
  change. Only the benchmark harness changes: `run_external.py` CLI choice grows
  by one, and `ExternalScenario` gains an embedding-only model variant + a
  cosine-ranking branch in `run()`. All harness-local and additive.
- **Coupling**: unchanged. Vector mode reuses `_get_vector_scores`, already a
  dependency of the hybrid path — but now the harness calls it directly rather
  than through the assembler.
- **Data ownership**: unchanged.
- **Reversibility**: high — the change is confined to the benchmark harness;
  removing it deletes one CLI choice + the `run()`/`setup()` vector branch and
  the embedding-only model variant. No production surface to unwind.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (confirm the vector-mode implementation approach and the
  nDCG / re-run scope decisions in Open Questions)
- Review rounds: 1 (additive change to production `ContextAssembler` warrants a
  read; the long runs are measurement)

The coding is small (one additive mode + harness plumbing). The bulk of the
appetite is the two long detached benchmark runs and their interpretation.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on :6379 | `redis-cli ping` | Ingest + retrieval backend |
| `[benchmark]` extra installed | `python -c "import sentence_transformers, huggingface_hub"` | Embedding provider |
| all-MiniLM-L6-v2 loadable | `python -c "from src.popoto.embeddings.sentence_transformers import SentenceTransformersProvider as P; P().embed(['ping'])"` | ~90MB one-time model download |
| Datasets cached | `ls ~/.cache/popoto_benchmarks/ 2>/dev/null` | LongMemEval-S (~264MB) + LoCoMo; downloaded on first run |
| numpy available | `python -c "import numpy"` | In-process cosine |

## Solution

### Key Elements

- **Granularity-parity audit (read-only deliverable)**: a written confirmation,
  with the spike-1 citations, that BM25 and the embedding arm rank identical
  per-turn units at both the model and retrieval layers. Recorded in the plan
  (above) and summarized in `docs/benchmarks.md` / the PR. No code.
- **Harness-local vector ranking** (Open Question 1 → harness-local):
  `ContextAssembler` is **NOT** modified. `ExternalScenario.run()` ranks by pure
  cosine directly via `QueryBuilder(model_class.query)._get_vector_scores(
  query_text, limit)` (no BM25 / RRF / graph, no `_pull_path_vector`, no
  `_VALID_MODES` change). The assembler is bypassed entirely for vector mode
  because auto-mode would resolve an embedding-only model to the query-blind
  `composite` path.
- **Harness plumbing**: `--retrieval-mode vector` CLI choice; an
  embedding-only model variant in `_build_external_model_class`
  (`with_bm25=False, with_embedding=True` → EmbeddingField only, no BM25Field);
  `setup()` sets `self._assembler = None` for vector mode; vector-mode notes in
  `compute_aggregate` / `build_markdown_report`; `_vector` artifact suffix
  (already generic in `save_reports`).
- **Two diagnostic runs**: vector-only on LongMemEval-S (500 q) and LoCoMo
  (1986 q), launched detached, artifacts committed.
- **Docs update**: vector-only R@1/5/10/MRR folded into `docs/benchmarks.md`
  next to lexical/hybrid, with the interpretation guide.

### Flow

`run_external.py --retrieval-mode vector` → ExternalScenario builds
EmbeddingField-only model (no BM25Field) → `setup()` sets `_assembler = None`
(assembler bypassed) → `run()` calls
`QueryBuilder(model_class.query)._get_vector_scores(query_text, limit)` (cosine)
→ maps `(redis_key, score)` pairs to ground-truth IDs → R@1/5/10/MRR →
`{slug}_{date}_vector.*` artifacts → `docs/benchmarks.md` table row.

### Technical Approach (harness-local — Open Question 1 resolved)

- **No `context_assembler.py` change.** `_VALID_MODES`, `_pull_path`, and the
  assembler's `auto`/lexical/hybrid resolution are **untouched**. The briefly
  committed first-class `_pull_path_vector` (65b36ea) is reverted so
  `git diff main -- src/popoto/recipes/context_assembler.py` is empty.
- **Harness — `_build_external_model_class`** (`external_base.py`): a
  `with_bm25` / `with_embedding` pair. Vector mode passes
  `with_bm25=False, with_embedding=True` → EmbeddingField only, **no**
  `content_index = BM25Field(...)`.
- **Harness — `setup()`**: for `retrieval_mode == "vector"` sets
  `self._assembler = None` (no assembler is constructed); lexical/hybrid still
  build `ContextAssembler(retrieval_mode="auto")` as before.
- **Harness — `run()`**: for vector mode, build a `QueryBuilder` from
  `self._model_class.query` and call
  `_get_vector_scores(self.item.query, limit=MAX_ITEMS)` (`query.py:995-1067`).
  The returned `(redis_key, score)` pairs are already sorted by cosine
  descending, so the redis keys ARE the ranked retrieval — no hydration and no
  `fuse()`/RRF are needed. Empty vector signal → empty retrieved_ids (R@k = 0),
  never a composite fallback (a vector-only baseline must not silently become
  query-blind — that would corrupt the diagnostic).
- **`run_external.py`**: add `"vector"` to the `--retrieval-mode` choices
  and its help text; thread through `run_item` / `compute_aggregate` /
  `save_reports` (all already parameterized on `retrieval_mode`); add a `vector`
  branch to the `mode_notes` and the `build_markdown_report` mode blurb. The
  `_vector` artifact suffix already falls out of the generic
  `suffix = "" if retrieval_mode == "lexical" else f"_{retrieval_mode}"`.
- **Runs**: `nohup python -m tests.benchmarks.run_external --dataset <d>
  --retrieval-mode vector … & disown` per the #442/#447 ops lesson; the
  inherited `stop_invalidation_listeners()` teardown prevents the connection
  leak.
- **nDCG (conditional)**: `ndcg_at_k` already exists (`metrics/retrieval.py:29`)
  but is unused by `run_external`. Wiring it means binary-gain nDCG (map each
  relevant_id → 1.0) at k=5,10 across **all three** modes for comparability,
  which implies re-running lexical + hybrid. Held as an Open Question (issue says
  "consider").

## Failure Path Test Strategy

### Exception Handling Coverage
- The existing hybrid/lexical assembler paths wrap failures in
  `logger.warning(...)` + fallback (e.g. `context_assembler.py:1293,1311,1362`).
  The harness-local vector path must **not** copy that composite fallback — a
  vector-only baseline that silently degrades to query-blind composite would
  produce a misleading number. Because the vector path bypasses the assembler
  entirely and ranks by raw cosine in `ExternalScenario.run()`, when
  `_get_vector_scores` returns `[]` the run yields empty retrieved_ids (R@k = 0),
  **never** a composite result.
- `_get_vector_scores` already returns `[]` (not raise) on missing
  provider/embeddings/numpy (`query.py:1025-1046`); assert the vector mode
  surfaces that as an empty ScenarioResult, not an error crash.

### Empty/Invalid Input Handling
- Empty `query_text` → `_get_vector_scores` returns `[]` (`query.py:1012`);
  assert vector mode yields zero retrieved_ids (R@k = 0), not an exception.
- A model with no saved embeddings → empty result; covered by the empty-signal
  test above.

### Error State Rendering
- Assert `ContextAssembler(retrieval_mode="vector")` on a model with **no
  EmbeddingField** raises `QueryException` with a message naming the missing
  field (mirrors the `lexical`/`hybrid` validation), rather than silently
  resolving to composite.

## Test Impact

- `tests/benchmarks/test_external.py` — **UPDATE/ADD**: add cases for
  `save_reports(retrieval_mode="vector")` producing `_vector`-suffixed names
  (the suffix logic is already generic, so this is a guard), and
  `compute_aggregate(retrieval_mode="vector")` emitting vector mode-notes.
  Existing lexical/hybrid naming tests (659-804) are **unaffected** (additive).
- `tests/benchmarks/test_csr.py:237` — asserts a `"bogus-mode"` is rejected;
  adding `"vector"` to valid modes does **not** affect it (bogus stays invalid).
  No change.
- **NEW** `tests/` (ContextAssembler unit): vector-mode resolution
  (Embedding-only + explicit `"vector"` → `_effective_mode == "vector"`),
  missing-EmbeddingField rejection, and the empty-signal-no-composite-fallback
  assertion. Likely `tests/recipes/test_context_assembler*.py` (locate the
  existing mode-resolution tests and extend them).

No existing test asserts `_VALID_MODES` has exactly four entries (verified by
grep during planning), so adding `"vector"` breaks nothing.

## Rabbit Holes

- **Fixing `auto`-resolution for Embedding-only models (composite → vector).**
  Tempting ("shouldn't an embedding-only model just do cosine?"), but it changes
  production default behavior and belongs to the #457/#409 conversation. The
  harness uses **explicit** `retrieval_mode="vector"`, so it needs nothing from
  `auto`. Leave `auto` alone.
- **nDCG everywhere + re-running all three modes.** Adding graded-relevance
  nDCG and re-running lexical + hybrid to keep the metric vector comparable can
  balloon into a full re-benchmark. Gate it behind the Open Question; the
  headline diagnostic (R@1/5/10/MRR vector-only) does not need it.
- **Weighted / query-adaptive RRF tuning.** That is #457. This plan must produce
  the *evidence*, not the fix. No fusion-weight constants here.
- **Re-running hybrid "to be safe."** Parity holds (spike-1), so the #457
  "fix-granularity-then-re-measure-hybrid" branch does not trigger. Don't
  re-run hybrid as part of this issue.

## Risks

### Risk 1: Vector-only run is slow / leaks connections on the full LoCoMo set (1986 q)
**Impact:** A multi-hour run that exhausts the 128-connection pool at ~item 120
(the #442 failure mode) wastes a long detached run.
**Mitigation:** The leak is already fixed — `teardown()` calls
`stop_invalidation_listeners()` (`external_base.py:411`) per item. Smoke-test
with `--limit 20 --dry-run` first; launch full runs detached (`nohup … &
disown`) and watch the progress log.

### Risk 2: Empty vector signal silently falls back to composite, corrupting the baseline
**Impact:** A "vector-only" number that is really query-blind composite would
mislead the #457 decision.
**Mitigation:** The harness-local vector path (`ExternalScenario.run()`) ranks by
raw cosine and returns empty retrieved_ids on no signal — the assembler is
bypassed, so there is no composite fallback to fall into. An explicit test
asserts it. Documented in Failure Path Strategy.

### Risk 3: nDCG scope creep forces a full three-mode re-benchmark
**Impact:** The cheap diagnostic turns into re-running lexical + hybrid on both
datasets.
**Mitigation:** nDCG is an Open Question, defaulted OFF. Ship the headline
metric vector first; nDCG can be a follow-up if the maintainer wants it.

## Race Conditions

No race conditions identified. The harness runs items **sequentially**
(`run_external.py:581`), each item uses a uniquely-named model class for Redis
key isolation, and the embedding invalidation listener is started and stopped
within a single item's lifecycle. `_get_vector_scores` is a read-only numpy
computation over a materialized matrix. No shared mutable state across items
beyond the read-only `_SHARED_PROVIDER` (documented safe at
`external_base.py:78-82`).

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG #457]` **Weighted / query-adaptive RRF fusion** — the actual
  fix for the LoCoMo hybrid regression. This plan produces the diagnostic that
  unblocks it; it does not tune fusion. Confirmed open via `gh issue view 457`.
- `[SEPARATE-SLUG #457]` **Changing `auto`-mode resolution for Embedding-only
  models** (composite → vector) — a production default-behavior change that
  belongs with the fusion/mode-semantics work in #457, not this measurement
  task. The harness uses explicit `retrieval_mode="vector"` and needs nothing
  from `auto`.

Nothing else deferred — the audit, the vector mode, both runs, and the docs
update are all in scope for this plan. (nDCG is not a No-Go; it is an explicit
Open Question with a default.)

## Update System

No update-system changes required — this is a benchmark-harness + internal
`ContextAssembler` change with no deploy/propagation surface.

## Agent Integration

No agent integration required — the vector mode is reachable only through the
benchmark CLI (`run_external.py`) and the internal `ContextAssembler` API; there
is no MCP/tool surface to wire.

## Documentation

### Feature Documentation
- [ ] Update `docs/benchmarks.md`: add a `--retrieval-mode vector` row to the
  "Running the Benchmark" section and a vector-only column/table to the results
  discussion, with the interpretation guide (vector weak on LoCoMo + competitive
  on LongMemEval-S → weak-arm/RRF hypothesis → feeds #457).
- [ ] Record the granularity-parity audit conclusion (parity holds, with
  citations) in `docs/benchmarks.md` and the PR body.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (the docs deploy gate).

### Inline Documentation
- [ ] ~~Docstring for `_pull_path_vector` in `ContextAssembler`~~ — VOID
  (harness-local; the assembler is not modified).
- [ ] Update `external_base.py` module docstring (the "Retrieval mode" section)
  and `ExternalScenario.run()` docstring to describe the harness-local vector
  mode (pure cosine, assembler bypassed).

## Success Criteria

- [ ] Granularity-parity audit is written with citations and its conclusion
  (parity holds → weak-arm hypothesis elevated) recorded in `docs/benchmarks.md`
  + PR. **The audit isolates only the dense arm** — not the graph /
  co-occurrence arm that also lives inside hybrid's `_pull_path_hybrid` — so the
  conclusion is scoped to the vector signal's standalone strength.
- [ ] **Harness-local (Open Question 1):** `git diff main -- src/popoto/recipes/context_assembler.py`
  is **empty** — no first-class `vector` mode, no `_pull_path_vector`, no
  `_VALID_MODES` change ship to production.
- [ ] `ExternalScenario.run()` ranks vector mode by pure cosine via
  `QueryBuilder._get_vector_scores` (no BM25/RRF/graph, assembler bypassed) and
  yields empty retrieved_ids (R@k = 0), not a composite fallback, on empty
  vector signal.
- [ ] **Decision rule for #457 (the diagnostic's core deliverable):**
  vector-only LoCoMo R@1 **substantially below lexical** R@1 (mirroring the
  hybrid<lexical LoCoMo regression) confirms the **weak-arm / RRF-dilution**
  hypothesis and greenlights #457's weighted/query-adaptive fusion; vector-only
  **competitive with hybrid** instead reopens the **fusion-mechanism**
  hypothesis (the fix is in how the arms combine, not in the dense arm). This
  rule is recorded in `docs/benchmarks.md` so #457 proceeds without a fresh
  interpretation debate.
- [ ] `run_external.py --retrieval-mode vector` runs end-to-end on a smoke
  subset (`--limit 20 --dry-run` / fixture).
- [ ] Full vector-only runs committed for **both** datasets as
  `{slug}_{date}_vector.{json,md}` + `{slug}_latest_vector.*`, never
  overwriting lexical/hybrid artifacts.
- [ ] Full R@1 / R@5 / R@10 / MRR vector reported for each (matching
  lexical/hybrid), folded into `docs/benchmarks.md`.
- [ ] Existing lexical + hybrid artifacts and the hybrid/lexical ranking code
  are byte-for-byte unchanged (additive-only change).
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`), `mkdocs build --strict` green.

## Team Orchestration

When this plan is executed, the lead agent orchestrates via Task tools and never
builds directly.

### Team Members

- **Builder (assembler-vector-mode)** — **VOID** (Open Question 1 → harness-local;
  see Task 1). No `ContextAssembler` changes ship; the vector ranking lives in
  the harness (`harness-builder`, below).
  - Name: `vector-mode-builder`
  - Role: ~~Add `"vector"` to `ContextAssembler` (`_VALID_MODES`, validation,
    `_pull_path_vector`, dispatch) + unit tests.~~ VOID.
  - Agent Type: builder
  - Domain: Redis/Popoto data + retrieval
  - Resume: true

- **Builder (harness-plumbing)**
  - Name: `harness-builder`
  - Role: `run_external.py` CLI choice + notes; `_build_external_model_class`
    embedding-only variant; `external_base.py` setup wiring; harness tests.
  - Agent Type: builder
  - Resume: true

- **Validator (vector-mode)**
  - Name: `vector-validator`
  - Role: Verify additive-only (lexical/hybrid untouched), empty-signal
    no-fallback, rejection-on-missing-field.
  - Agent Type: validator
  - Resume: true

- **Runner (benchmark-runs)**
  - Name: `benchmark-runner`
  - Role: Smoke test, then launch + collect both detached vector-only runs;
    commit artifacts.
  - Agent Type: builder
  - Resume: true

- **Documentarian**
  - Name: `benchmarks-doc`
  - Role: `docs/benchmarks.md` update + audit writeup; `mkdocs build --strict`.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. ~~Add first-class `vector` mode to ContextAssembler~~ — VOID (Open Question 1 → harness-local)
- **Task ID**: build-vector-mode
- **Status**: **VOID.** Open Question 1 resolved harness-local, so `ContextAssembler`
  is not modified. The briefly-committed `_VALID_MODES` entry + `_pull_path_vector`
  (65b36ea) are reverted; `git diff main -- src/popoto/recipes/context_assembler.py`
  must be empty. The pure-cosine ranking lives in the harness (Task 2), calling
  `QueryBuilder._get_vector_scores` directly.

### 2. Harness-local vector ranking + plumbing for `--retrieval-mode vector`
- **Task ID**: build-harness
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py`
- **Informed By**: spike-2 (reuse `_get_vector_scores`), spike-3 (auto-mode
  gives composite, not vector — so bypass the assembler)
- **Assigned To**: harness-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `"vector"` to the `--retrieval-mode` choices + help (`run_external.py`).
- Add an embedding-only model variant in `_build_external_model_class`
  (`with_bm25=False, with_embedding=True` → EmbeddingField only, no BM25Field).
- Wire `setup()` to set `self._assembler = None` for vector mode (no assembler).
- Wire `run()` to rank by pure cosine via
  `QueryBuilder(self._model_class.query)._get_vector_scores(query_text, MAX_ITEMS)`
  and map `(redis_key, score)` pairs to ground-truth IDs; empty signal → empty
  retrieved_ids (NO composite fallback).
- Add `vector` branch to `mode_notes` + `build_markdown_report` blurb.
- Tests: `save_reports`/`compute_aggregate` vector suffix + notes; cosine-path
  smoke via fixture.

### 3. Validate additive-only + failure paths
- **Task ID**: validate-vector
- **Depends On**: build-vector-mode, build-harness
- **Assigned To**: vector-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm lexical/hybrid ranking code + existing artifacts unchanged.
- Confirm empty-signal returns empty (not composite) and missing-field rejects.
- Run the full test suite.

### 4. Smoke test + full detached runs
- **Task ID**: run-benchmarks
- **Depends On**: validate-vector
- **Assigned To**: benchmark-runner
- **Agent Type**: builder
- **Parallel**: false
- `--limit 20 --dry-run` smoke on both datasets.
- `nohup … --retrieval-mode vector & disown` full runs (LongMemEval-S 500,
  LoCoMo 1986); watch progress log; verify no connection-leak stall.
- Commit `{slug}_{date}_vector.*` + `_latest_vector.*` artifacts.

### 5. Documentation + audit writeup
- **Task ID**: document-benchmarks
- **Depends On**: run-benchmarks
- **Assigned To**: benchmarks-doc
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/benchmarks.md` (vector row, results, interpretation guide,
  granularity-parity conclusion).
- `mkdocs build --strict`.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: document-benchmarks
- **Assigned To**: vector-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all Verification checks; confirm every Success Criterion (incl. docs).
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -q` | exit code 0 |
| Assembler untouched (harness-local, anti-criterion) | `git diff main -- src/popoto/recipes/context_assembler.py` | empty diff |
| No first-class vector mode leaked to production | `grep -c '_pull_path_vector' src/popoto/recipes/context_assembler.py` | output == 0 |
| Harness ranks vector by cosine | `grep -c '_get_vector_scores' tests/benchmarks/scenarios/external_base.py` | output > 0 |
| CLI exposes vector | `grep -c 'vector' tests/benchmarks/run_external.py` | output > 0 |
| Vector artifacts committed (LoCoMo) | `ls tests/benchmarks/results/external/locomo_latest_vector.json` | exit code 0 |
| Vector artifacts committed (LongMemEval-S) | `ls tests/benchmarks/results/external/longmemeval_s_latest_vector.json` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

**Verdict: NEEDS REVISION** — 1 blocker must be resolved before build. Critics:
Risk & Robustness, Scope & Value, History & Consistency (FULL depth). 4 findings
(1 blocker, 3 concerns, 0 nits). All three critics independently flagged Open
Question 1 as unresolved-yet-committed; cross-validation + live working-tree
divergence elevated it to BLOCKER.

| Severity | Critic(s) | Finding | Addressed By | Implementation Note |
|----------|-----------|---------|--------------|---------------------|
| **BLOCKER** | All 3 | Open Question 1 (first-class production `vector` mode vs harness-local) is unresolved, but Solution (287-290), Technical Approach (310-323), Tasks 1-2 (552-584), Success Criteria (490-493) and Verification (631-632) all commit to first-class. The in-progress branch has already diverged: harness went harness-local (`external_base.py` cosine bypass), assembler got first-class `_pull_path_vector`. Build is split across both approaches. | Resolve Q1 (PM check-in) and reconcile with divergent in-progress code before dispatching Tasks 1-2. | Add `Depends On: open-question-1-resolved` to `build-vector-mode` and `build-harness`. If harness-local chosen, Task 1, `_VALID_MODES`/`_pull_path` edits, and the two `context_assembler.py` verification greps (631-632) become void — `ExternalScenario.run()` calls `QueryBuilder(model_class)._get_vector_scores(query_text, limit)` (query.py:995-1067) and hydrates itself, no `context_assembler.py` diff. |
| CONCERN | Risk & Robustness, History & Consistency | `_pull_path_vector` "reuse the hydration `fuse()` path" contradicts the no-RRF vector-only guarantee. `_get_vector_scores` returns raw `(redis_key, score)` tuples, not records; hydration in hybrid is done by `fuse()`, which IS the RRF fusion function. Routing vector-only candidates back through `fuse()` re-invokes RRF — contradicting Data Flow's "No BM25, no RRF, no graph" (228-230) and Risk 2 (416-421). | Name the exact hydration primitive to reuse, or confirm one must be factored out of `fuse()`, before Task 1. | Either call `fuse()` with only `vector=` populated and verify single-arm input preserves cosine order, or extract the hydrate-by-redis_key sub-step and reuse directly. Add a unit assertion that `_pull_path_vector` never invokes `RRF_K`-weighted scoring. |
| CONCERN | Risk & Robustness | "Weak-arm vs broken-fusion" is a false binary — vector-only cannot isolate the graph arm inside hybrid. `_pull_path_hybrid` fuses a THIRD graph/co-occurrence arm seeded from BM25 top-5. A weak vector-only number cannot distinguish "vector arm weak" from "graph arm is the confound." Concluding it "elevates the weak-arm/RRF hypothesis" (spike-1, 183-188) overstates what a vector-vs-everything comparison proves. | Scope the audit's conclusion language, or add a graph-arm-off ablation as a follow-up candidate in Open Questions before #457 commits to a fix. | In `docs/benchmarks.md` interpretation guide, state that the vector-only run isolates only the dense arm — not the graph/co-occurrence arm in `_pull_path_hybrid`. |
| CONCERN | Scope & Value | Success Criteria (485-506) are all mechanical — none define the "decisive" result that unblocks #457. Every criterion is a mechanical check; none state the decision rule (threshold/pattern) that lets #457 proceed without a fresh interpretation debate. The plan's purpose is to unblock #457, so the deliverable's core value is undefined. | Add a Success Criterion stating the decision rule. | Docs-only change to Success Criteria + `docs/benchmarks.md` interpretation guide. E.g. "vector-only LoCoMo R@1 substantially below lexical R@1 (mirroring hybrid<lexical) confirms weak-arm/RRF; competitive with hybrid reopens the fusion-mechanism hypothesis." |

---

## Open Questions

1. **Vector-mode implementation surface.** ✅ **RESOLVED (2026-07-13):
   harness-local.** The maintainer chose the **harness-local** alternative: the
   vector-only baseline lives entirely in the benchmark harness and
   `ContextAssembler` is **NOT** modified. `ExternalScenario.run()` calls
   `QueryBuilder(model_class.query)._get_vector_scores(query_text, limit)`
   (`query.py:995-1067`) directly and maps the returned `(redis_key, score)`
   pairs back to ground-truth IDs — no `context_assembler.py` diff, no
   `_pull_path_vector`, no new `_VALID_MODES` entry, no `retrieval_mode="vector"`
   branch in the assembler. The first-class production `vector` mode (the
   originally-recommended path, briefly committed in 65b36ea) is **rejected and
   reverted**: shipping a production `retrieval_mode="vector"` was deemed
   unwarranted for a diagnostic, and auto-mode resolving an embedding-only model
   to the query-blind `composite` path stays a #457/#409 concern, not this
   measurement task. Rationale: the vector baseline is a one-off diagnostic to
   unblock #457; it does not need to be a durable `assemble()` retrieval path.
   The superseded first-class plan is preserved at
   `docs/plans/vector_retrieval_mode.md` (status: Superseded) for history.
2. **nDCG.** The issue says "consider adding nDCG across all three modes."
   Default here is **OFF** (headline R@1/5/10/MRR vector is the diagnostic).
   Wiring nDCG means binary-gain nDCG@{5,10} and, for a comparable three-mode
   vector, **re-running lexical + hybrid** on both datasets. **Add nDCG now
   (accept the re-runs), or defer to a follow-up?**
3. **Run scope.** Full LoCoMo is 1986 questions (a multi-hour detached run).
   Confirm both **full** datasets are wanted now, versus a representative
   `--limit`/`--sample stratified` first pass to get a fast read before
   committing the full runs.
