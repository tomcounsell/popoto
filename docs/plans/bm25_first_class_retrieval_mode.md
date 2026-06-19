---
status: Planning
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-19
tracking: https://github.com/tomcounsell/popoto/issues/409
last_comment_id: 4689746491
---

# Make BM25 a First-Class Retrieval Mode and Recipe Default

## Problem

The agent-memory "subconscious memory" recipe tells users to build a `Memory` model
(copied verbatim from the recipe Quick Start) and wraps every LLM turn with
`SubconsciousMemory.inject_context()`. The recipe promises it "assembl[es] relevant
context before each call" using "the last user message as a query cue."

**Current behavior:** For the model the recipe documents, retrieval is provably
**query-independent**. The June 2026 adversarial audit (finding RETR-1) measured, on a
200-memory corpus with 20 topical queries through `inject_context()`:

- Mean P@10 = **0.10** — exactly the random baseline (20 relevant / 200).
- **1** distinct injected set across 20 different queries (mean pairwise Jaccard = 1.0).
- Literal gibberish (`"zzz qqq flurble..."`) injects a **bit-identical 10-memory set**.
- Ranking is driven by importance (mean 0.912 injected vs 0.588 not injected), not the query.
- Direct BM25 `keyword_search()` on the same corpus: **P@10 = 0.62**, zero extra deps.

Root cause: the recipe's default model has no search field, so the default
`retrieval_mode="auto"` resolves to `"composite"`, which ranks by a weighted sum over
per-record indexes (decayed importance + confidence) and takes **no query-text input**.
The `query_cues` extracted from the user message are only consumed by an `ExistenceFilter`
pre-check, which the documented model does not have — so the cue is dead code.

Two compounding defects:
1. **`BM25Field` alone is silently ignored.** A model with `BM25Field` but no
   `EmbeddingField` resolves `"auto"` → `"composite"` (BM25 index built but never
   consulted), and explicit `retrieval_mode="hybrid"` raises `QueryException`. There is no
   way to get query-sensitive retrieval through `ContextAssembler`/`SubconsciousMemory`
   without also adding `EmbeddingField` (numpy + an embedding provider).
2. **The docs overpromise.** The recipe guide claims query-relevance the default
   configuration cannot deliver.

**Desired outcome:** BM25 becomes a first-class retrieval mode usable **without**
`EmbeddingField`; the recipe's default Quick Start model includes a `BM25Field` so the
documented copy-paste path is query-sensitive (P@10 ≈ 0.6 instead of 0.1); and the docs
stop claiming query-relevance for query-blind configurations.

## Freshness Check

**Baseline commit:** `5c33ca990048fe9b23e732a40a08f18c7549a6fc`
**Issue filed at:** 2026-06-11T05:20:26Z
**Disposition:** Minor drift

The issue body's line citations were written pre-#419. The upstream-change-notice comment
(2026-06-12) flagged that #419 rewrote the token-budget region and shifted the cited lines.
A full re-read of current `main` produced corrected, verified references:

**File:line references re-verified (post-#419, all confirmed present):**
- `src/popoto/recipes/context_assembler.py:926-932` — `"auto"` resolution: `"hybrid"` iff
  both `_bm25_field` and `_embedding_field` non-null, else `"composite"`. (issue cited `:782-787`)
- `context_assembler.py:933-945` — explicit `"hybrid"` without both fields raises
  `QueryException(f"retrieval_mode='hybrid' requires {...} on {model}")`. (cited `:788-800`)
- `context_assembler.py:1134-1142` — `_pull_path()` dispatcher: `"hybrid"` →
  `_pull_path_hybrid`, else `_pull_path_composite`.
- `context_assembler.py:1144-1212` — `_pull_path_composite`: `ExistenceFilter` cue check
  at `:1151-1164`; `composite_score(indexes=...)` ranking (no query text) at `:1175-1179`,
  `:1203-1207`. (cited `:967-980`, `:991-995`)
- `context_assembler.py:1214-1313` — `_pull_path_hybrid`: builds `query_text` from cues
  (`:1224`), BM25 via `BM25Field.search()` (`:1246-1251`), vector via `_get_vector_scores()`
  (`:1264`), graph propagation (`:1276-1288`), RRF `query.fuse()` (`:1303-1307`). **Crucially,
  it already fuses whichever signals exist and only requires `keyword_results OR vector_results`
  (`:1268-1273`); empty vector results are tolerated.**
- `context_assembler.py:1022-1050` — post-#419 budget packing (greedy first-fit, skip-not-break,
  first record always admitted). New mode flows through this unchanged.
- `models/query.py:446` — `composite_score(self, indexes, ...)` — confirmed **no** query-text param.
- `models/query.py:823-892` — `keyword_search(query_text, field=None, limit=10)` delegates to
  `BM25Field.search()`, returns hydrated instances.
- `models/query.py:894-989` — `fuse(k=60, limit=10, **ranked_lists)` RRF over `(redis_key, score)` lists.
- `fields/bm25_field.py` — no numpy import (grep-verified); `BM25Field.search(model_class,
  field_name, query_text, limit)` → `[(redis_key, score), ...]` via Lua (`BM25_SEARCH_LUA`).
- `recipes/subconscious_memory.py:132-137` — builds `ContextAssembler` **without** passing
  `retrieval_mode` (defaults to `"auto"`). `:158-163` extracts last user message → `query_cues["topic"]`.
- `docs/guides/subconscious-memory-recipe.md:39-54` — Quick Start `Memory` model (no search field).
  Overpromise claims at `:5`, `:13`, `:80`, `:101`, `:106`.
- `docs/features/context-assembler.md:17-23` — retrieval mode table.

**Cited sibling issues/PRs re-checked:**
- #419 — MERGED 2026-06-12. Its token-budget rewrite is the only source of line drift; it did
  **not** change the root cause (mode resolution or composite query-blindness). The new packing
  stage is relevant to the P@10 test design (pin `max_tokens`, see below).
- #395 / PR #400 — MERGED 2026-05-22. Shipped hybrid mode + `"auto"` resolution. This issue is
  the defaults/gating follow-up; the machinery from #395 is reused, not rebuilt.
- #394 — OPEN (benchmark harness). Not a blocker; a self-contained pytest using the PoC corpus suffices.

**Commits on main since issue was filed (touching referenced files):**
- `11cf23c` (PR #419) — token budgets. Caused line drift only; root cause intact. (corrected above)

**Active plans in `docs/plans/` overlapping this area:** `context_assembler_hybrid_default.md`
(#395, already shipped — historical), `hybrid_retrieval.md`, `qmd_retrieval_investigation.md`
(#397, investigation). None is an active competing plan for the defaults/gating change. No overlap blocker.

**Notes:** The single most important freshness finding is architectural, not a line number:
`_pull_path_hybrid` is **already signal-tolerant**. It collects BM25, vector, and graph signals
independently and fuses whichever are non-empty, bailing to composite only when *both* BM25 and
vector are empty. A BM25-only model would run through this path correctly today — `vector_results`
would simply be `[]`. **The fix is therefore primarily in the mode-resolution gate (`__init__`),
not the pull path.** This substantially reduces the implementation surface.

## Prior Art

- **#395 / PR #400** (MERGED 2026-05-22): "Default ContextAssembler to hybrid retrieval
  (BM25 + vector + graph via RRF)." Built the hybrid pull path, RRF fusion, and the `"auto"`
  resolution this issue revises. Reused, not redone. **Why it didn't cover this case:** it
  defined "auto → hybrid" as requiring *both* search fields, treating BM25-only as a degenerate
  case that falls back to composite. The gap is the gating decision, not the retrieval engine.
- **#304** (CLOSED 2026-03-29): "Hybrid retrieval: BM25 + RRF fusion." Built `keyword_search`
  and `fuse()`. These public APIs are the building blocks the new mode reuses.
- **#397** (CLOSED 2026-05-26): QMD-style expand/RRF/rerank investigation. Background; out of scope.
- **#403** (CLOSED 2026-06-03): EmbeddingField stale-cache fix. Tangential — the BM25 path
  deliberately avoids EmbeddingField entirely.

No prior attempt addressed BM25-only gating or the recipe-default model. This is the first fix.

## Research

**Queries used:**
- "BM25 lexical retrieval as default RAG memory when no embeddings available 2026"

**Key findings:**
- BM25 is a strong standalone baseline that often matches or beats dense embeddings on
  keyword-heavy / domain-specific corpora (precise terminology — names, labels, IDs — gives
  strong lexical signal that embeddings can dilute), and requires **no embeddings**, making it
  the practical choice when an embedding provider is unavailable
  (https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026).
  This directly supports making BM25 a no-dependency first-class mode and recipe default —
  agent-memory facts ("Valor prefers...", "Production deploys Tuesday 14:00 UTC...") are exactly
  the keyword-heavy content BM25 excels at, consistent with the audit's measured P@10 = 0.62.
- Hybrid (BM25 + dense) is the recommended *ceiling* when embeddings are available, which the
  plan preserves: callers who add `EmbeddingField` still get hybrid. BM25-only is the new floor,
  not a replacement for hybrid.

## Data Flow

1. **Entry point**: `SubconsciousMemory.inject_context(messages)` extracts the last user
   message into `query_cues={"topic": <user text>}` (`subconscious_memory.py:158-163`).
2. **Assembler dispatch**: `ContextAssembler.assemble(query_cues=...)` calls `_pull_path()`
   (`context_assembler.py:996`), which dispatches on `self._effective_mode`
   (`:1134-1142`). `_effective_mode` was resolved in `__init__` (`:926-948`).
   - **Today (bug):** default model has no search field → `_effective_mode = "composite"` →
     `_pull_path_composite` ranks by `composite_score(indexes=...)`, ignoring `query_cues`
     entirely (only an `ExistenceFilter`, absent here, would read them).
   - **After fix:** BM25-present model → `_effective_mode = "lexical"` (or `"hybrid"` if also
     embedding-capable) → `_pull_path_hybrid`/lexical path builds `query_text` from cues,
     runs `BM25Field.search()`, fuses via `query.fuse()`.
3. **Budget packing**: selected candidates flow through the post-#419 packing stage
   (`:1022-1050`) — serialize per record, count tokens, greedy first-fit skip-not-break.
4. **Output**: `AssemblyResult.records` / `.formatted` injected into the system message;
   different queries now yield different memory sets.

## Architectural Impact

- **New dependencies**: None. BM25 is pure Lua + core Redis commands (Redis + Valkey safe).
  No numpy, no embedding provider on the BM25-only path.
- **Interface changes** (beta substrate, breaking acceptable):
  - `retrieval_mode` gains a `"lexical"` value and `"auto"` resolution rules change (BM25-present
    → lexical/hybrid instead of composite).
  - `SubconsciousMemory` should pass `retrieval_mode` through (or default to `"auto"` and rely on
    the improved resolution — see Open Questions).
- **Coupling**: Unchanged. Reuses existing `_pull_path_hybrid`, `BM25Field.search`, `query.fuse`.
- **Data ownership**: Unchanged.
- **Reversibility**: High. Mode resolution is a localized `__init__` branch; the recipe model and
  doc edits are additive/textual.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (confirm the `"auto"` resolution semantics + whether SubconsciousMemory forces lexical)
- Review rounds: 1 (correctness of mode gating + the P@10 regression test design)

The retrieval engine already exists and works (P@10 = 0.595 hybrid-through-recipe was measured).
This is a gating, defaults, and docs change plus a regression test — communication/review bound,
not coding-bound.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Tests run against a live server (DB 15 via plugin) |
| Editable install current | `python -c "import popoto; print(popoto.__version__)"` | Plugin isolation + BM25 Lua available |

Run all checks: `python scripts/check_prerequisites.py docs/plans/bm25_first_class_retrieval_mode.md`

## Solution

### Key Elements

- **`"lexical"` retrieval mode**: A first-class mode that uses BM25 only — no `EmbeddingField`
  required. Resolves the pull path through the existing (already signal-tolerant) hybrid fusion
  machinery with only the keyword (and optional graph) signal.
- **Revised `"auto"` resolution**: BM25-present → query-sensitive (lexical if no embedding,
  hybrid if embedding too); no BM25 and no embedding → composite (unchanged). The silent
  "BM25 built but ignored" trap is eliminated.
- **Loud failure preserved**: Explicit `retrieval_mode="lexical"` on a model with no `BM25Field`
  raises `QueryException` (mirrors the existing `"hybrid"` guard). Explicit `"hybrid"` still
  requires both fields.
- **Recipe default model gains `BM25Field`**: The Quick Start `Memory` model and relevant
  quickstart levels include a `content_bm25 = BM25Field(source="content")` so the documented
  copy-paste path is query-sensitive out of the box.
- **Docs honesty pass**: Recipe guide states plainly that composite ignores the query; the
  context-assembler mode table documents the new resolution and the `"lexical"` mode.
- **P@10 regression test**: A self-contained pytest using the audit PoC corpus, asserting the
  documented-default model achieves mean P@10 ≥ 0.5 and ≥ 19/20 distinct injected sets.

### Flow

Recipe Quick Start (with `BM25Field`) → `SubconsciousMemory.inject_context(messages)` →
`ContextAssembler` resolves `"auto"` → `"lexical"` (BM25 present, no embedding) →
`_pull_path` (lexical) runs `BM25Field.search(query_text)` → `query.fuse()` → budget pack →
**query-specific** memories injected → different queries yield different sets.

### Technical Approach

- **Mode resolution (`context_assembler.py:926-948`)**: Add a `"lexical"` branch and revise `"auto"`:
  - `"auto"`: `"hybrid"` if (BM25 and embedding); elif `"lexical"` if BM25; elif `"hybrid"`-via-embedding
    decision (preserve any embedding-only behavior that exists today — verify); else `"composite"`.
    (Confirm current embedding-only behavior during build; the issue is silent about an
    embedding-only-no-BM25 model. Default that to composite unless a hybrid-capable path exists.)
  - `"lexical"`: require `_bm25_field is not None`, else raise `QueryException` with a message
    mirroring the `"hybrid"` guard (`"retrieval_mode='lexical' requires BM25Field on {model}"`).
  - `"hybrid"`: unchanged (still requires both fields).
  - `"composite"`: unchanged.
- **Pull-path dispatch (`:1134-1142`)**: Route `"lexical"` to `_pull_path_hybrid` (which already
  tolerates empty vector results) **or** add a thin `_pull_path_lexical` that calls only the BM25
  + fuse + optional-graph subset. Prefer routing through `_pull_path_hybrid` to avoid duplication —
  but verify the `if not keyword_results and not vector_results: fall back to composite` branch
  (`:1268-1273`) behaves acceptably for lexical (a truly empty BM25 result legitimately falling to
  composite is acceptable; document it). If reuse is awkward, extract a shared helper.
- **`_get_vector_scores` guard**: In lexical mode, do **not** call `_get_vector_scores` (it may
  import numpy / require an embedding provider). Gate the vector branch on `self._embedding_field
  is not None` so a numpy-free environment never touches vector code. (Verify the current hybrid
  path doesn't already short-circuit when `_embedding_field is None`; if it does, lexical reuse is free.)
- **SubconsciousMemory (`subconscious_memory.py:132-137`)**: Either (a) keep building with
  `retrieval_mode="auto"` and rely on improved resolution (preferred — zero new surface), or
  (b) accept/forward a `retrieval_mode` kwarg. Decide via Open Question 1.
- **Recipe model + quickstart**: Add `content_bm25 = BM25Field(source="content")` to the Quick
  Start model (`subconscious-memory-recipe.md:39-54`) and the quickstart guide levels that the
  recipe references (Level 2+ in `agent-memory-quickstart.md`). Keep `EmbeddingField` as the
  Level 6 upgrade path, unchanged.
- **Docs honesty**: Reword `subconscious-memory-recipe.md:5,13,80,101,106` so query-relevance is
  only claimed for query-sensitive modes; correct the "returned unchanged" claim (in composite it
  is effectively unreachable). Update the `docs/features/context-assembler.md:17-23` mode table
  with the `"lexical"` row and revised `"auto"` rules.

## Failure Path Test Strategy

### Exception Handling Coverage
- The hybrid/lexical pull path has `except Exception` blocks that `logger.warning` and fall back
  to composite (`context_assembler.py:1252-1253`, `:1265-1266`, `:1308-1310`). Add a test that a
  BM25 search failure in lexical mode logs a warning and degrades gracefully (assert on log/observable
  behavior, not silent swallow). The `token_counter` contract probe (`:874-886`) is out of scope.
- The new `QueryException` for `"lexical"` without `BM25Field` must be tested as a *loud* failure
  (assert it raises, with the field-name message).

### Empty/Invalid Input Handling
- Test gibberish query through the new default path: it must return a query-appropriate set (likely
  few/zero on-topic), and critically must **not** return the same bit-identical set as an unrelated
  query (the RETR-1 signature). Document expected behavior when BM25 returns zero hits (falls to
  composite per `:1268-1273` — acceptable, but assert it's not query-blind across *different* real queries).
- Test empty `query_cues` / empty user message: pull path is skipped (`:995`), no crash.

### Error State Rendering
- `inject_context()` output is consumed by the caller's LLM prompt, not a UI. Assert that on
  retrieval failure the messages are returned (degraded, not crashed) and the failure is logged.

## Test Impact

- `tests/test_context_assembler_hybrid.py` — UPDATE: add cases for `_effective_mode` resolution
  with BM25-only models (`"auto"` → `"lexical"`) and the `"lexical"` explicit mode + its loud-failure
  guard. Existing hybrid (both-fields) assertions must still pass unchanged.
- `tests/test_subconscious_memory_integration.py` — UPDATE: the documented-default model now has a
  `BM25Field`; any assertion pinned to composite behavior for the default model must be revised. Add
  query-sensitivity assertions (distinct sets per query).
- `tests/test_bm25_field.py` — likely no change (BM25 engine unchanged); verify still green.
- `tests/test_hybrid_retrieval.py` — verify still green (RRF fusion unchanged); add a lexical-only
  fusion case if not covered.
- NEW `tests/test_retrieval_quality_regression.py` (or similar) — REPLACE/ADD: the PoC-corpus P@10
  regression test. **Pin `max_tokens` ample/unset** so post-#419 budget packing does not truncate
  the top-10 and masquerade as a retrieval regression (per the #419 upstream notice).

## Rabbit Holes

- **Reranking / QMD-style expand pipelines** (#397): tempting quality gains, separate scope. Do not.
- **Tuning BM25 k1/b or the tokenizer/stopwords** to chase P@10 beyond the 0.5 floor: the floor
  already leaves headroom (measured 0.62); do not tune for marginal gains.
- **Fixing the `ExistenceFilter` per-cue EVAL inefficiency** (audit PERF-7): out of scope, separate issue.
- **`extract_memories()` filler-pollution** (audit extraction finding): out of scope, separate issue.
- **Reworking the embedding-only (no-BM25) resolution**: only touch it if the current behavior is
  demonstrably wrong; the issue is about BM25-only. Keep the diff focused on the documented defect.
- **Building on #394's harness**: nice-to-have home for the numbers; do not block on it — ship a
  self-contained pytest.

## Risks

### Risk 1: Lexical reuse of `_pull_path_hybrid` accidentally invokes numpy/embedding code
**Impact:** A BM25-only deployment without numpy crashes or imports a missing provider, violating
the "no new required deps" acceptance criterion.
**Mitigation:** Gate every vector branch on `self._embedding_field is not None`. Add a test that
runs the lexical path with the vector branch asserted unreached (monkeypatch `_get_vector_scores`
to raise, expect it never called). Verify in build whether the current hybrid path already short-circuits.

### Risk 2: P@10 regression test is flaky or environment-sensitive
**Impact:** CI noise; false regressions from budget truncation (#419) or tokenizer changes.
**Mitigation:** Use a fixed-seed corpus (the PoC uses `random.Random(42)`), pin `max_tokens` ample/unset,
set the floor at 0.5 (below the measured 0.62), and assert distinct-set count (≥19/20) which is robust
to small score perturbations.

### Risk 3: Changing the recipe default model breaks existing user models on upgrade
**Impact:** Users who copied the old model and upgrade get no `BM25Field` automatically (their stored
data has no BM25 index). This is a docs/onboarding change, not a runtime migration — existing models
keep working in composite. Acceptable for beta substrate.
**Mitigation:** Document that adding `BM25Field` to an existing model requires reindexing existing
records (or that it indexes on next save). State this in the docs honesty pass.

## Race Conditions

No race conditions identified. Mode resolution is a synchronous `__init__` branch; the pull path is a
synchronous sequence of Redis calls. BM25 indexing already happens in field `on_save` hooks
(unchanged). No new concurrent access patterns or shared mutable state introduced.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #409] The `ExistenceFilter` per-cue/per-token EVAL inefficiency (audit PERF-7) —
  noted in the issue as a separate concern; file independently. (Tracked for filing; not addressed here.)
- [SEPARATE-SLUG #409] `extract_memories()` filler-pollution (audit extraction finding) — separate
  concern per the issue's "Dropped" bucket; file independently.
- [SEPARATE-SLUG #394] Wiring P@10 into the LongMemEval-S / LoCoMo benchmark harness — #394 is the
  proper home for harness-based regression tracking; this plan ships a self-contained pytest instead.

<!-- NOTE: PERF-7 and the extraction finding are flagged in issue #409 itself as out-of-scope and
     "should be filed independently." They are not yet separate issues; the build/merge step should
     file them. Tagged to #409 as the source of record per the issue's explicit deferral. -->

## Update System

No update system changes required — this is a library-internal change (retrieval mode resolution +
recipe docs). No new dependencies, config files, or migration scripts to propagate.

## Agent Integration

No agent integration required — this is a Popoto library change. There is no MCP server or Telegram
bridge in scope; `ContextAssembler`/`SubconsciousMemory` are imported directly by library consumers.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/context-assembler.md` mode table (`:17-23`): add the `"lexical"` row,
      revise `"auto"` resolution rules, update the `"hybrid"` exception note.
- [ ] Update `docs/guides/subconscious-memory-recipe.md`: add `BM25Field` to the Quick Start model;
      reword the overpromising claims at `:5,13,80,101,106`; note reindexing for existing models.
- [ ] Update `docs/guides/agent-memory-quickstart.md`: add `BM25Field` to the recipe-feeding levels
      (Level 2+) so the progressive path is query-sensitive; keep Level 6 EmbeddingField as the
      semantic upgrade.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (run via `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstring for the `"lexical"` mode on `ContextAssembler.__init__` / `retrieval_mode`.
- [ ] Comment on the revised `"auto"` resolution explaining BM25-only → lexical.

## Success Criteria

- [ ] A model with `BM25Field` and no `EmbeddingField` passed to
      `ContextAssembler(retrieval_mode="auto")` resolves to a lexical-capable mode (assert
      `_effective_mode`) and returns query-dependent results; no numpy required on that path.
- [ ] The post-update recipe Quick Start model returns different memory sets for different queries:
      on the PoC corpus (200 / 10 topics / 20 queries), ≥ 19/20 distinct injected sets, mean
      pairwise Jaccard well below 1.0.
- [ ] Regression test: documented-default model through `SubconsciousMemory.inject_context()`
      achieves mean P@10 ≥ 0.5 (with `max_tokens` pinned ample/unset).
- [ ] Explicit `retrieval_mode="lexical"` on a model without `BM25Field` raises `QueryException`
      (loud, no silent fallback); explicit `"hybrid"` still requires both fields.
- [ ] `"composite"` unchanged for opt-in callers; existing hybrid (BM25+embedding) behavior and
      P@10 do not regress.
- [ ] Docs updated: recipe guide no longer claims query-relevance for query-blind configs;
      context-assembler mode table reflects new resolution; "returned unchanged" corrected.
- [ ] Full suite green on Redis and Valkey (`scripts/ci-local.sh`); no Redis-module commands introduced.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (retrieval-mode)**
  - Name: `retrieval-builder`
  - Role: Mode resolution + lexical pull path in `context_assembler.py`; SubconsciousMemory wiring.
  - Agent Type: builder
  - Resume: true

- **Builder (regression-test)**
  - Name: `test-builder`
  - Role: PoC-corpus P@10 regression test + lexical-mode unit tests.
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian (docs)**
  - Name: `docs-writer`
  - Role: Recipe model + claims, quickstart levels, mode table.
  - Agent Type: documentarian
  - Resume: true

- **Validator (final)**
  - Name: `final-validator`
  - Role: Verify all success criteria, run full local CI.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement lexical mode + revised auto resolution
- **Task ID**: build-retrieval-mode
- **Depends On**: none
- **Validates**: tests/test_context_assembler_hybrid.py
- **Assigned To**: retrieval-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `"lexical"` to `retrieval_mode` resolution in `context_assembler.py:926-948`; revise `"auto"`
  so BM25-present (no embedding) → `"lexical"`, both → `"hybrid"`, neither → `"composite"`.
- Add loud `QueryException` guard for explicit `"lexical"` without `BM25Field`.
- Route `"lexical"` through `_pull_path_hybrid` (verify vector branch is gated on
  `_embedding_field is not None`; add the gate if missing) or extract a shared helper.
- Decide SubconsciousMemory wiring per Open Question 1 (default: rely on improved `"auto"`).

### 2. Update recipe default model + quickstart levels
- **Task ID**: build-recipe-model
- **Depends On**: none
- **Validates**: docs build (`scripts/ci-local.sh docs`)
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: true
- Add `content_bm25 = BM25Field(source="content")` to the Quick Start model and the recipe-feeding
  quickstart levels; note reindexing for existing models.

### 3. Docs honesty pass + mode table
- **Task ID**: build-docs-honesty
- **Depends On**: build-retrieval-mode
- **Validates**: docs build
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Reword overpromise claims; add `"lexical"` row and revised `"auto"` rules to the mode table.

### 4. P@10 regression + lexical unit tests
- **Task ID**: build-tests
- **Depends On**: build-retrieval-mode, build-recipe-model
- **Validates**: new regression test file + tests/test_context_assembler_hybrid.py
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Add the PoC-corpus P@10 regression test (pin `max_tokens`); add lexical mode resolution,
  loud-failure, query-sensitivity, and numpy-free gating tests.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: build-retrieval-mode, build-recipe-model, build-docs-honesty, build-tests
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `scripts/ci-local.sh`; verify every success criterion; confirm no Redis-module commands and
  no new required deps on the lexical path.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Hybrid/lexical tests | `pytest tests/test_context_assembler_hybrid.py -q` | exit code 0 |
| Regression test | `pytest -k retrieval_quality -q` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| No Redis modules introduced | `git diff main -- src/ \| grep -E '^\+.*(BF\.\|CMS\.\|FT\.\|JSON\.\|TS\.)' ` | exit code 1 |
| Lexical mode resolves | `python -c "from popoto.recipes.context_assembler import ContextAssembler"` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **SubconsciousMemory wiring**: Should `SubconsciousMemory` keep building the assembler with
   `retrieval_mode="auto"` and rely on the improved resolution (zero new surface, recommended), or
   should it expose/forward a `retrieval_mode` kwarg so callers can force `"lexical"`/`"composite"`?
   Recommendation: rely on improved `"auto"`; add the kwarg only if a forcing use case is needed.
2. **`"lexical"` vs relaxing `"hybrid"`**: The issue offers two shapes — (a) a new `"lexical"` mode,
   or (b) relax `"hybrid"` to "fuse whichever signals exist." This plan picks (a) `"lexical"` for an
   explicit, discoverable name and to keep `"hybrid"`'s both-fields contract loud. Confirm this is the
   preferred shape.
3. **Embedding-only (no-BM25) model under `"auto"`**: The issue is silent on a model with
   `EmbeddingField` but no `BM25Field`. Current behavior resolves such a model to `"composite"`
   (query-blind). Should `"auto"` also route embedding-only models to a vector-sensitive path, or is
   that out of scope (BM25-only is the stated target)? Recommendation: out of scope unless trivial.
