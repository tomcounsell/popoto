---
status: Ready
type: feature
appetite: Medium
owner: valor
created: 2026-06-30
tracking: https://github.com/tomcounsell/popoto/issues/437
last_comment_id:
---

# Run External Benchmark in Hybrid (BM25 + Vector) Mode

## Problem

Popoto ships an agent-memory layer whose retrieval entry point, `ContextAssembler`,
already implements a `hybrid` mode that fuses BM25 (lexical) and vector (semantic)
signals via Reciprocal Rank Fusion. We validate retrieval against the outside world
with the LongMemEval-S external benchmark. But the benchmark **cannot currently run
in hybrid mode**, so the only external number we can report is lexical-only.

The published `agentmemory` reference runs BM25 + vector hybrid with
`all-MiniLM-L6-v2` embeddings and scores **R@5 0.952 / R@10 0.986** on LongMemEval-S.
Popoto's first real LongMemEval-S baseline is **BM25-only: R@5 0.952 / R@10 0.978**
(any-hit, measured after the recall-metric fix in #438). We have no apples-to-apples
hybrid number because the harness never exercises the hybrid path.

**Current behavior:**
- `ExternalScenario.run()` (`tests/benchmarks/scenarios/external_base.py:219-235`)
  calls `BM25Field.search(...)` **directly** as the primary retrieval, and only
  falls back to `assemble()` when BM25 returns zero hits. The hybrid RRF path in
  `ContextAssembler._pull_path_hybrid()` therefore never executes.
- The benchmark model declares a `BM25Field` but **no** `EmbeddingField`, so even
  if `assemble()` were the primary path, `retrieval_mode="auto"` would resolve to
  `"lexical"`, not `"hybrid"`.
- No local (no-API-key) embedding provider exists. The three providers today
  (`VoyageProvider`, `OpenAIProvider`, `OllamaProvider`) all need either an API key
  or a running Ollama daemon — none wraps `all-MiniLM-L6-v2`. `grep -rn
  "SentenceTransformer" src/ tests/` returns nothing, yet the `[benchmark]` extra
  already installs `sentence-transformers>=2.7` and `huggingface_hub>=0.23`
  (`pyproject.toml:49-52`) — a dangling, installed-but-unused dependency.
- The run report text is stale: `run_external.py` notes still say "relevance-only
  scoring (DecayingSortedField)" and "No vector/embedding retrieval wired in"
  (`run_external.py:210-214, 270-273`), which describes neither the actual current
  behavior (BM25-only) nor the desired behavior (hybrid).

**Desired outcome:**
- A local `SentenceTransformersProvider` wrapping `all-MiniLM-L6-v2`, no API key
  required, usable under the `[benchmark]` extra.
- The benchmark scenario drives retrieval through `ContextAssembler.assemble()` as
  the **primary** path so the hybrid RRF fusion actually runs.
- A CLI flag (`--retrieval-mode`) selects the mode, so we can produce both the
  lexical baseline and a validated hybrid R@5 / R@10 from the same harness and
  compare them against the `agentmemory` reference.
- All of this is **Valkey-safe**: vector similarity is computed in pure numpy
  in-process — no RediSearch, no vector-search modules, no `FT.*` / `BF.*` commands.

## Freshness Check

**Baseline commit:** `13d21d04691d8611efcd890c9c2efe8807b4b6e0` (2026-06-29 16:13 +0700)
**Issue filed at:** 2026-06-29T08:27:45Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:946-954` — auto-mode resolution
  (BM25+Embedding → `hybrid`; BM25-only → `lexical`; else `composite`) — **still
  holds** (confirmed in the `if retrieval_mode == "auto":` block).
- `src/popoto/recipes/context_assembler.py:127` — `RRF_K = 60` — **still holds**.
- `src/popoto/models/query.py:991` — `_get_vector_scores()` computes cosine via
  numpy dot product — **still holds** (numpy-only, degrades gracefully if numpy
  absent at line 1040).
- `src/popoto/fields/embedding_field.py:332` — `class EmbeddingField` stores per-
  record `.npy` under `POPOTO_CONTENT_PATH/.embeddings/` (`_get_embeddings_dir()`
  at line 223-229) — **still holds**.
- `src/popoto/embeddings/__init__.py` — `AbstractEmbeddingProvider` interface
  (`embed()`, `dimensions`, `max_batch_size`) — **still holds**.
- `pyproject.toml:49-52` — `[benchmark]` extra installs `sentence-transformers>=2.7`
  + `huggingface_hub>=0.23` — **still holds**.
- `tests/benchmarks/scenarios/external_base.py:219-249` — BM25-direct primary with
  `assemble()` fallback — **still holds** (the bypass is real).
- `grep -rn "SentenceTransformer" src/ tests/` — **returns nothing** (no provider
  exists), as the issue claims.

**Drift noted (Minor):** The orchestrator brief described the current baseline as
"score-only (DecayingSortedField)". The actual current code path is **BM25-only**
(`run()` calls `BM25Field.search` directly). The DecayingSortedField/relevance-only
path is only reachable via the dead `composite` fallback. The stale `run_external.py`
report notes (`:210-214`, `:270-273`) are the source of that "score-only" framing.
This plan treats the de-facto baseline as **lexical (BM25)** and includes fixing the
stale notes/docstrings as in-scope cleanup.

**Cited sibling issues/PRs re-checked:**
- #395 (Default ContextAssembler to hybrid retrieval) — **CLOSED**; substrate landed
  on main. This issue depends on that substrate, which is present.
- #409 (BM25 as first-class retrieval mode) — **CLOSED**; `lexical` mode landed.
- #438 (external Recall@k any-hit fix) — **MERGED** (commits `a72de90`, `13d21d0`);
  the cited 0.952 / 0.978 baseline already reflects this fix.

**Commits on main since issue was filed (touching referenced files):**
- `git log --since=2026-06-29 -- tests/benchmarks/scenarios/external_base.py
  src/popoto/embeddings/` — **none**. The referenced files are unchanged since the
  issue was filed.

**Active plans in `docs/plans/` overlapping this area:**
- `external_recall_at_k_any_hit.md` — shipped via #438 (the recall-metric fix this
  plan's baseline depends on). No code overlap remaining.
- `external_benchmark_harness.md` — the original harness (#394). This plan extends it.
- `context_assembler_hybrid_default.md` / `hybrid_retrieval.md` /
  `content_and_embedding_fields.md` — the hybrid substrate (#395, #304). **Already
  shipped**; this plan consumes them, does not re-touch them.
- **Live coordination:** #434 and #435 are open sibling issues that also modify
  `run_external.py` and `test_external.py` (see No-Gos / Risk 4). This plan builds
  and merges **LAST**.

**Notes:** No major drift. The premise — "the harness never executes the hybrid
path, and no MiniLM provider exists" — is intact on current main.

## Prior Art

- **#395 (CLOSED)**: Default ContextAssembler to hybrid retrieval (BM25 + vector +
  graph via RRF). Landed the `_pull_path_hybrid()` fusion, `RRF_K = 60`, and the
  `auto → hybrid` resolution rule this plan relies on. Succeeded — substrate is on main.
- **#304 (CLOSED, hybrid_retrieval.md)**: Hybrid retrieval: BM25 + RRF fusion. The
  fusion algorithm. Succeeded.
- **#409 (CLOSED)**: BM25 as first-class retrieval mode / recipe default. Added the
  `lexical` mode that BM25-only models resolve to. Succeeded.
- **#394 (external_benchmark_harness.md)**: LongMemEval-S + LoCoMo adapters and the
  `ExternalScenario` / `run_external.py` harness this plan extends. Succeeded.
- **#438 (external_recall_at_k_any_hit.md, MERGED)**: Recall@k any-hit fix that
  produced the 0.952/0.978 baseline. The baseline this plan compares against.
- **OllamaProvider (`src/popoto/embeddings/ollama.py`)**: The closest structural
  template for a local, no-API-key provider. The new `SentenceTransformersProvider`
  mirrors its shape (lazy detection of dims, `max_batch_size` for local inference).

No prior attempt to wire embeddings into the *benchmark* exists — this is the first.

## Data Flow

1. **Entry point**: `python -m tests.benchmarks.run_external --dataset longmemeval-s
   --retrieval-mode hybrid`. `main()` parses the new `--retrieval-mode` arg and
   threads it into `run_item(item, retrieval_mode)`.
2. **Scenario construction**: `run_item` constructs
   `ExternalScenario(item, retrieval_mode=mode)`. `setup()` builds the per-item model
   class. When `mode == "hybrid"`, `_build_external_model_class` declares **both**
   `content_index = BM25Field(source="content")` and `embedding =
   EmbeddingField(source="content", provider=SentenceTransformersProvider())`. When
   `mode == "lexical"` (default), only `BM25Field` is declared (no model download).
3. **Ingestion**: each turn's `content` is saved. On save, `EmbeddingField.on_save`
   calls `provider.embed([...])` → `all-MiniLM-L6-v2` produces a 384-dim vector →
   stored as a `.npy` under `POPOTO_CONTENT_PATH/.embeddings/{ClassName}/`.
4. **Retrieval (the fix)**: `run()` calls
   `self._assembler.assemble(query_cues={"topic": query}, agent_id=...)` as the
   **primary** path. The assembler is constructed with `retrieval_mode="auto"`;
   with both fields present it resolves to `hybrid` and runs `_pull_path_hybrid()`.
5. **Hybrid fusion (existing substrate, in-process)**: BM25 produces a ranked list;
   `QueryBuilder._get_vector_scores()` (`query.py:991`) embeds the query, computes
   **cosine similarity in numpy** against the cached embedding matrix, producing a
   second ranked list. The two lists (plus any graph signal) are fused via **RRF,
   k=60**. No Redis/Valkey module is touched.
6. **Output**: `assemble()` returns `result.records`; `run()` maps each to its
   `db_key.redis_key`, then maps those back to ground-truth session/turn IDs via
   `_session_key_map`, and reports Recall@1/5/10 + MRR + latency.

## Architectural Impact

- **New dependencies**: none added to install graph — `sentence-transformers` and
  `huggingface_hub` are already declared under the `[benchmark]` extra. The new
  provider imports them **lazily** (inside `embed()` / `__init__`), so importing
  `popoto.embeddings` without the extra installed does not break.
- **Interface changes**: additive only. New `SentenceTransformersProvider` class +
  export in `src/popoto/embeddings/__init__.py`. New `--retrieval-mode` CLI flag
  (defaulted) and a new `retrieval_mode` kwarg on `ExternalScenario.__init__`
  (defaulted) — existing callers unchanged.
- **Coupling**: the benchmark scenario gains a dependency on `EmbeddingField` +
  provider, but only in hybrid mode. Lexical mode keeps its current dependency set.
- **Data ownership**: unchanged. Embeddings are owned by `EmbeddingField` on disk,
  not by the benchmark.
- **Reversibility**: high. The provider is new isolated code; the scenario change is
  behind a defaulted flag. Reverting the flag default restores current behavior.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (confirm default mode + fusion method — see Open Questions; confirm merge ordering vs #434/#435)
- Review rounds: 1 (code review; the merge-conflict surface with siblings warrants a careful diff)

Coding is ~0.5-1 day per the issue. The Medium appetite reflects coordination
overhead (merge ordering with #434/#435) and the one decision that needs sign-off
(default mode), not implementation complexity.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `sentence-transformers` importable | `python -c "import sentence_transformers"` | Local MiniLM embeddings (provided by `[benchmark]` extra) |
| `numpy` importable | `python -c "import numpy"` | In-process cosine similarity for the vector signal |
| Redis/Valkey on :6379 | `redis-cli ping` | Benchmark ingestion target |
| HF model reachable / pre-cached | `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` | One-time ~90MB `all-MiniLM-L6-v2` download, then cached |

Install the extra with `uv pip install -e ".[benchmark]"` (or `pip install -e
".[benchmark]"`) before running hybrid mode.

## Solution

### Key Elements

- **`SentenceTransformersProvider`**: a ~40-line `AbstractEmbeddingProvider`
  implementation wrapping `SentenceTransformer("all-MiniLM-L6-v2")`. `embed()`
  returns `model.encode(texts).tolist()`; `dimensions = 384`; `max_batch_size`
  conservative for CPU (e.g. 64). No API key. Lazy import so it costs nothing when
  unused.
- **Hybrid-aware benchmark model**: `_build_external_model_class` conditionally adds
  an `EmbeddingField(source="content", provider=...)` when hybrid mode is requested,
  so `retrieval_mode="auto"` resolves to `hybrid`.
- **Assemble-as-primary `run()`**: rewrite `ExternalScenario.run()` to call
  `assemble()` first and map `result.records → redis_keys`, deleting the
  BM25-direct-primary path (keeping the key-mapping logic, which is correct).
- **`--retrieval-mode` CLI flag**: `run_external.py` gains
  `--retrieval-mode {lexical,hybrid}` (default proposed: `lexical`), threaded into
  the scenario.
- **Fusion = existing RRF (no new fusion code)**: hybrid fusion is performed by the
  already-shipped `_pull_path_hybrid()` using RRF k=60. This plan does **not**
  introduce a new weighted-sum fusion — it drives the substrate that already fuses.
- **Cleanup**: `teardown()` removes the `.embeddings/{ClassName}/` directory; stale
  `external_base.py` docstrings (`composite` → `lexical`) and stale
  `run_external.py` report notes are corrected.

### Flow

CLI `--retrieval-mode hybrid` → `ExternalScenario(retrieval_mode="hybrid")` →
`setup()` declares BM25Field + EmbeddingField, ingests turns (embeddings written to
`.npy`) → `run()` calls `assemble()` → assembler auto-resolves to `hybrid` →
`_pull_path_hybrid()` fuses BM25 + numpy-cosine vector via RRF → `run()` maps records
to session IDs → Recall@5 / Recall@10 reported and compared to the lexical baseline
and the `agentmemory` reference.

### Technical Approach

- **Provider** (`src/popoto/embeddings/sentence_transformers.py`, new): mirror
  `OllamaProvider`'s structure. Import `SentenceTransformer` lazily inside the
  constructor (or first `embed()`); raise a clear `RuntimeError`/`ImportError` with
  the `pip install popoto[benchmark]` hint if it's missing. Cache the loaded model on
  the instance. `dimensions = 384` (known a priori for MiniLM; no need to defer like
  Ollama). `embed(texts, input_type=None)` ignores `input_type` (MiniLM is
  symmetric). Add to `__all__` in `src/popoto/embeddings/__init__.py` and update the
  module docstring's "Available providers" list.
- **Valkey-safety is structural, not added work**: the vector signal flows through
  `EmbeddingField` (`.npy` on disk) and `QueryBuilder._get_vector_scores()` (numpy
  cosine). **No** RediSearch / vector index / `FT.*` / `BF.*` / `CMS.*` commands are
  introduced. A Verification grep guards against any such command sneaking in.
- **Scenario wiring**: add `retrieval_mode: str = "lexical"` to
  `ExternalScenario.__init__`; pass it to `_build_external_model_class(safe_prefix,
  with_embedding=mode == "hybrid")`. Construct the assembler with
  `retrieval_mode="auto"` (field presence drives resolution) — or pass the explicit
  mode through; either is acceptable, `auto` is simpler and matches #395's design.
- **`run()` rewrite**: make `assemble(query_cues={"topic": self.item.query},
  agent_id=self._agent_id)` the primary call; map `result.records` to
  `db_key.redis_key`; preserve the existing `_session_key_map` reverse-mapping block
  (it is mode-agnostic and correct). Record `retrieval_method` = the assembler's
  effective mode in metadata.
- **CLI**: add `--retrieval-mode` with `choices=("lexical", "hybrid")`,
  `default="lexical"`; thread through `run_item(item, retrieval_mode)`; surface the
  mode in the report header and `notes`.
- **teardown**: after Redis cleanup, `shutil.rmtree(os.path.join(
  _get_embeddings_dir(), self._model_class.__name__), ignore_errors=True)`.
- **Report honesty**: replace the stale "relevance-only / no vector retrieval" notes
  in `compute_aggregate()` and `build_markdown_report()` with mode-aware text, and
  emit both the lexical baseline and hybrid numbers against the reference.

## Failure Path Test Strategy

### Exception Handling Coverage
- `external_base.py` `run()` currently wraps `assemble()` in `try/except` and returns
  a `status="error"` ScenarioResult — keep this and assert it: a test injecting an
  `assemble()` that raises must produce `status == "error"` with a non-empty
  `error_message` (observable, not swallowed).
- `teardown()`'s `except Exception: pass` blocks are best-effort cleanup. The new
  `.embeddings/` rmtree uses `ignore_errors=True`; add a test asserting the directory
  is gone after teardown on the happy path (so cleanup isn't silently skipped).
- The provider's missing-dependency branch must raise a clear error, not pass — test
  that constructing/using it without `sentence_transformers` raises with an
  actionable message (simulate via import monkeypatch).

### Empty/Invalid Input Handling
- `SentenceTransformersProvider.embed([])` must return `[]` (mirror Ollama) — unit
  test.
- Whitespace-only / empty turns are already skipped in `setup()`; add an assertion
  that a hybrid run over an all-empty history yields `status="skipped-empty"` and
  does **not** attempt embedding.
- A hybrid query that matches nothing must return an empty `retrieved_ids` (R@k = 0),
  not error — test.

### Error State Rendering
- The benchmark's user-visible output is the report. Test that when a run produces
  errors above `--error-threshold`, `main()` returns exit code 1 (existing behavior;
  assert it still holds under the new mode).
- Assert the report `notes` no longer contain the stale "No vector/embedding
  retrieval wired in" string after a hybrid run.

## Test Impact

- [ ] `tests/benchmarks/test_external.py` — UPDATE: existing assertions assume
  BM25-direct primary and `retrieval_method` of `"bm25"`/`"composite_fallback"`.
  Update to the assemble-primary contract and the new `retrieval_method` values
  (effective mode). **Coordinate with #434/#435 which also touch this file.**
- [ ] `tests/benchmarks/test_external.py` — ADD: a hybrid-mode smoke test (small
  fixture, may be `@pytest.mark.benchmark` / skipped if the model isn't cached) that
  asserts the assembler's effective mode is `hybrid` and records are returned.
- [ ] `tests/embeddings/test_sentence_transformers_provider.py` (or alongside
  existing embedding-provider tests) — ADD: unit tests for the new provider
  (dimensions == 384, `embed([])` == `[]`, missing-dep error, a real `encode` round
  trip behind a cache/skip guard).
- [ ] `tests/benchmarks/test_factory.py` / `test_harness.py` — REVIEW: confirm no
  assertion hard-codes the BM25-only model field set; update if the conditional
  `EmbeddingField` changes the model shape they introspect.

No other existing tests are affected — the provider is greenfield and the scenario
change is behind a defaulted flag (lexical default preserves current behavior).

## Rabbit Holes

- **Re-implementing fusion.** RRF (k=60) is already shipped in `_pull_path_hybrid()`.
  Do **not** write a new weighted-sum or a new RRF. Drive `assemble()`.
- **Tuning the fusion / weights for benchmark score.** `RRF_K = 60` is intentionally
  not user-configurable (comment at `context_assembler.py:127-128`). Tuning belongs
  in a separate experiment, not this plan. Report whatever the default substrate
  produces.
- **Speeding up embedding with batching/threading gymnastics.** CPU latency is a
  known cost; accept it. A 500-question MiniLM run on CPU is minutes, not hours.
- **A general-purpose embedding-cache / warm-start layer for CI.** Pre-caching the
  model is a CI config concern (Risk 2), not a code feature. Don't build a caching
  abstraction.
- **Making lexical mode also route through a unified path "for symmetry".** Keep the
  change minimal: hybrid drives `assemble()`; don't refactor lexical's proven path
  beyond making `assemble()` its primary too if trivial.

## Risks

### Risk 1: Valkey-compatibility regression (vector search via a Redis module)
**Impact:** If any part of the vector path used RediSearch / a vector index / `FT.*`
/ `BF.*` commands, it would break on Valkey and on module-less Redis — a hard project
constraint (CLAUDE.md / project memory: "Never use Redis modules; all features must
work on both Redis and Valkey").
**Mitigation:** Reuse the **existing** in-process path only: `EmbeddingField` stores
`.npy` on disk; `QueryBuilder._get_vector_scores()` computes cosine in numpy. Add no
new Redis commands. A Verification grep asserts no `FT.`/`BF.`/`CMS.`/`vector` Redis
command strings appear in the changed files. The new provider touches only
`sentence_transformers` + numpy, never Redis.

### Risk 2: One-time ~90MB model download / CI network
**Impact:** First hybrid run downloads `all-MiniLM-L6-v2` (~90MB) from Hugging Face.
In CI this can fail (no network / firewall) or slow the job.
**Mitigation:** Default `--retrieval-mode lexical` so the standard benchmark and CI
path needs **no** download. Hybrid is opt-in. For CI hybrid runs, pre-cache the model
(HF cache restore step) or gate the hybrid test with a skip-if-not-cached guard. The
lazy import means the dependency is only resolved when hybrid is actually invoked.

### Risk 3: CPU embedding latency inflates benchmark wall-clock
**Impact:** Embedding every turn on save, plus the query at retrieval, adds CPU time;
a full 500-question hybrid run is materially slower than lexical.
**Mitigation:** Latency is measured for retrieval only (not ingestion) so reported
p50/p95 stay comparable. Use a sensible `max_batch_size`. Document the wall-clock
expectation. Keep hybrid opt-in so routine CI stays fast.

### Risk 4: Merge-conflict surface with #434 and #435 (shared files)
**Impact:** This work edits `tests/benchmarks/run_external.py` and
`tests/benchmarks/test_external.py`, which are **also** edited by #434 (LoCoMo
adapter schema fix) and #435 (`--limit` prefix + `question_type` reporting). Parallel
merges would conflict.
**Mitigation:** This plan is **explicitly sequenced LAST** — build and merge #434 and
#435 first, then rebase this work onto the merged result and resolve against their
final shape. Recorded in No-Gos as an `[ORDERED]` constraint. Keep this plan's edits
to those two files as localized as possible (new CLI arg block; assemble-primary
`run()`; mode-aware notes) to minimize the conflict footprint.

## Race Conditions

No concurrency-induced races in the benchmark path itself — ingestion and retrieval
are synchronous and single-threaded per item, and items run sequentially.

### Race N: Embedding-cache invalidation across processes
**Location:** `src/popoto/fields/embedding_field.py` (cache invalidation modes).
**Trigger:** Only relevant in multi-worker deployments; the benchmark is a single
process.
**Data prerequisite:** Each turn's `.npy` must be written before the query embeds and
runs cosine — guaranteed here because `setup()` (all saves) completes before `run()`.
**State prerequisite:** The in-process embedding matrix must include all ingested
turns at query time — guaranteed by single-process, setup-before-run ordering.
**Mitigation:** None needed for the benchmark. The existing
`POPOTO_EMBEDDING_INVALIDATION` machinery is out of scope; the benchmark relies on the
single-process default where the cache is always consistent within the run.

## No-Gos (Out of Scope)

- [ORDERED] Merging this work before #434 and #435 land. This plan shares
  `run_external.py` and `test_external.py` with both; it must be built and merged
  **last**, after those two are merged, to avoid conflicts. Gating event: #434 and
  #435 merged to main.
- [SEPARATE-SLUG #434] Fixing the LoCoMo adapter schema / `dia_id` ground-truth
  handling. Tracked in #434; not touched here.
- [SEPARATE-SLUG #435] Fixing `--limit` prefix sampling and `question_type` reporting.
  Tracked in #435; not touched here.
- Tuning `RRF_K` or introducing configurable fusion weights — intentionally fixed in
  the substrate (`context_assembler.py:127-128`); a tuning experiment is a separate
  effort. (Rabbit hole, not deferred work — nothing to file.)
- Wiring `SentenceTransformersProvider` as a production default for non-benchmark
  models — out of scope; it ships as a benchmark-extra provider only.

## Update System

No update/deploy-system changes required — this is internal benchmark + library code.
The new provider lives behind the existing `[benchmark]` optional extra, which is
already declared in `pyproject.toml`. No new top-level install dependency, config
file, or migration is introduced.

## Agent Integration

No agent/MCP integration required — `SentenceTransformersProvider` is a library
embedding provider and the benchmark harness is a developer CLI. Neither is exposed
through an agent tool surface. The provider follows the existing pluggable
`AbstractEmbeddingProvider` contract, so any agent-facing model that opts into
`EmbeddingField` could use it, but that wiring is out of scope here.

## Documentation

### Feature Documentation
- [ ] Update `docs/benchmarks.md` (the benchmark prerequisites already mention
  `sentence-transformers`): document `--retrieval-mode`, the hybrid prerequisites
  (`[benchmark]` extra + one-time MiniLM download), and the expected hybrid vs
  lexical vs `agentmemory`-reference comparison.
- [ ] Add `SentenceTransformersProvider` to the embedding-providers documentation
  (wherever Voyage/OpenAI/Ollama providers are listed), noting it is local,
  no-API-key, 384-dim, `all-MiniLM-L6-v2`.

### External Documentation Site
- [ ] Verify `mkdocs build --strict` passes after doc edits (mirrors
  `deploy-docs.yml`; run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstring on the new provider (mirror Ollama's: what it wraps, no API key,
  one-time download).
- [ ] Fix stale `external_base.py` module/class docstrings (`composite` → `lexical`)
  and the stale `run_external.py` report notes.

## Success Criteria

- [ ] `SentenceTransformersProvider` exists, implements `AbstractEmbeddingProvider`,
  wraps `all-MiniLM-L6-v2` (384 dims), needs no API key, lazily imports its deps, and
  is exported from `src/popoto/embeddings/__init__.py`.
- [ ] A unit test covers the provider (dimensions == 384, `embed([])` == `[]`,
  missing-dependency error path, one real `encode` round trip behind a skip/cache
  guard).
- [ ] `external_base.py` declares an `EmbeddingField` on the benchmark model in
  hybrid mode, and `run()` drives retrieval through `ContextAssembler.assemble()` as
  the **primary** path (not BM25-direct with assemble as fallback).
- [ ] With both fields present and `retrieval_mode="auto"`, the assembler's effective
  mode is `hybrid` and `_pull_path_hybrid()` (RRF) executes during the benchmark
  (asserted by a test).
- [ ] `run_external.py` exposes `--retrieval-mode {lexical,hybrid}` (default
  `lexical`), threaded into the scenario; the report header/notes reflect the mode
  used.
- [ ] No Redis/Valkey module commands are introduced (Verification grep passes); the
  vector signal is numpy-only.
- [ ] `teardown()` removes the `.embeddings/{ClassName}/` artifacts; stale
  `composite`→`lexical` docstrings and stale report notes are corrected.
- [ ] A LongMemEval-S hybrid re-run produces a validated R@5 / R@10, reported and
  compared against the BM25-only baseline (0.952 / 0.978) and the `agentmemory`
  reference (0.952 / 0.986).
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).
- [ ] Built and merged **after** #434 and #435 (ORDERED constraint honored).

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using Task tools. The
lead NEVER builds directly — they deploy team members and coordinate.

### Team Members

- **Builder (embedding-provider)**
  - Name: provider-builder
  - Role: Implement `SentenceTransformersProvider` + export + unit test.
  - Agent Type: builder
  - Resume: true

- **Builder (benchmark-scenario)**
  - Name: scenario-builder
  - Role: Hybrid-aware model, assemble-primary `run()`, teardown cleanup, docstrings.
  - Agent Type: builder
  - Resume: true

- **Builder (cli-and-report)**
  - Name: cli-builder
  - Role: `--retrieval-mode` flag, threading, mode-aware report notes/comparison.
  - Agent Type: builder
  - Resume: true

- **Validator (hybrid-path)**
  - Name: hybrid-validator
  - Role: Verify effective mode == hybrid, RRF executes, Valkey-safety grep, recall
    numbers produced and compared.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update `docs/benchmarks.md` + provider docs; `mkdocs build --strict`.
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

(Standard roster: builder, validator, code-reviewer, test-engineer, documentarian,
performance-optimizer, etc.)

## Step by Step Tasks

### 1. Implement SentenceTransformersProvider
- **Task ID**: build-provider
- **Depends On**: none
- **Validates**: `tests/embeddings/test_sentence_transformers_provider.py` (create)
- **Assigned To**: provider-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/embeddings/sentence_transformers.py` mirroring
  `ollama.py`'s structure; lazy-import `sentence_transformers`.
- `embed(texts, input_type=None)` → `model.encode(texts).tolist()`; `embed([])` →
  `[]`; `dimensions` == 384; `max_batch_size` conservative (e.g. 64).
- Raise an actionable error (`pip install popoto[benchmark]`) if the dep is missing.
- Export from `src/popoto/embeddings/__init__.py` (`__all__` + docstring list).
- Add the unit test.

### 2. Make the benchmark scenario hybrid-capable
- **Task ID**: build-scenario
- **Depends On**: build-provider
- **Validates**: `tests/benchmarks/test_external.py` (update + add hybrid smoke test)
- **Assigned To**: scenario-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `retrieval_mode: str = "lexical"` to `ExternalScenario.__init__`.
- Conditionally declare `EmbeddingField(source="content",
  provider=SentenceTransformersProvider())` in `_build_external_model_class` when
  hybrid is requested.
- Rewrite `run()` to call `assemble()` as the primary path; map
  `result.records → db_key.redis_key`; keep the `_session_key_map` reverse-mapping.
- Extend `teardown()` to `rmtree` the `.embeddings/{ClassName}/` dir.
- Fix stale `composite` → `lexical` docstrings.

### 3. Add the CLI flag and mode-aware reporting
- **Task ID**: build-cli
- **Depends On**: build-scenario
- **Validates**: `tests/benchmarks/test_external.py`, manual `--retrieval-mode` run
- **Assigned To**: cli-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `--retrieval-mode {lexical,hybrid}` (default `lexical`) to `run_external.py`;
  thread through `run_item` → `ExternalScenario`.
- Replace stale "relevance-only / no vector retrieval" notes with mode-aware text;
  emit lexical vs hybrid vs `agentmemory` comparison.

### 4. Validate hybrid path + Valkey-safety
- **Task ID**: validate-hybrid
- **Depends On**: build-cli
- **Assigned To**: hybrid-validator
- **Agent Type**: validator
- **Parallel**: false
- Assert the assembler's effective mode is `hybrid` and RRF runs.
- Run the Valkey-safety grep (no `FT.`/`BF.`/`CMS.`/vector-index commands).
- Run a small LongMemEval-S fixture in both modes; confirm numbers are produced and
  compared against baseline + reference.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-hybrid
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/benchmarks.md` (`--retrieval-mode`, hybrid prerequisites, comparison).
- Add `SentenceTransformersProvider` to provider docs.
- `mkdocs build --strict` (via `scripts/ci-local.sh docs`).

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: hybrid-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite + lint/format.
- Confirm all Success Criteria, including the ORDERED merge constraint (#434/#435
  merged first).
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Provider tests pass | `pytest tests/embeddings/test_sentence_transformers_provider.py -q` | exit code 0 |
| Benchmark tests pass | `pytest tests/benchmarks/test_external.py -q` | exit code 0 |
| Provider exported | `python -c "from popoto.embeddings import SentenceTransformersProvider"` | exit code 0 |
| Provider dims == 384 | `python -c "from popoto.embeddings.sentence_transformers import SentenceTransformersProvider as P; print(P().dimensions)"` | output contains 384 |
| CLI flag present | `python -m tests.benchmarks.run_external --help` | output contains retrieval-mode |
| assemble() is primary in run() | `grep -n "self._assembler.assemble" tests/benchmarks/scenarios/external_base.py` | output > 0 |
| No Redis vector-module commands (anti-criterion) | `grep -rEn "FT\.|BF\.|CMS\.|FT_SEARCH|vector_index" src/popoto/embeddings/sentence_transformers.py tests/benchmarks/scenarios/external_base.py tests/benchmarks/run_external.py` | match count == 0 |
| Stale "no vector retrieval" note removed (anti-criterion) | `grep -c "No vector/embedding retrieval wired in" tests/benchmarks/run_external.py` | match count == 0 |
| Stale composite docstring fixed (anti-criterion) | `grep -c "auto-mode resolves to \`\`\"composite\"\`\`" tests/benchmarks/scenarios/external_base.py` | match count == 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

_Resolved 2026-06-30 (orchestrator, recommended defaults accepted):_

1. **Default `--retrieval-mode` — RESOLVED: `lexical`.** Standard/CI path needs no
   model download; hybrid is opt-in.
2. **Fusion method — RESOLVED: reuse existing RRF (k=60).** No new/weighted-sum fusion
   in this plan.
3. **Merge ordering — RESOLVED: this plan merges LAST**, after #434 and #435. The
   orchestrator gates it.
4. **CLI mode names — RESOLVED: `{lexical, hybrid}` only.** No `score`/`composite`
   choice exposed (the de-facto floor is lexical/BM25).
