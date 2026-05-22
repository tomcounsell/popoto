---
status: Draft
type: feature
appetite: Medium
owner: Valor
created: 2026-05-19
tracking: https://github.com/tomcounsell/popoto/issues/395
last_comment_id:
---

# ContextAssembler: Default to Hybrid Retrieval (BM25 + Vector + Graph via RRF)

## Problem

`ContextAssembler._pull_path()` currently runs a single `CompositeScoreQuery` weighted by
`score_weights` (e.g., `{"relevance": 0.6, "confidence": 0.3}`). It has no concept of
lexical (BM25) or semantic (vector) retrieval signals — users wanting hybrid retrieval must
wire it manually from lower-level primitives. Since most users won't, the hybrid path is
effectively absent despite `BM25Field` and `CompositeScoreQuery.fuse()` both existing since
PR #306.

The agentmemory repo demonstrates ~50 lines of RRF fusion is the single biggest contributor
to its 95.2% R@5 on LongMemEval-S. Our defaults shape what users actually get.

**Desired outcome:** `ContextAssembler` defaults to hybrid retrieval — BM25 + vector + graph
signals fused with RRF — whenever the model has the required field capabilities. Users opting
out can pass `retrieval_mode="composite"` to preserve the current weighted-sum behavior.
Existing `score_weights`-only constructions continue to work unchanged.

## Freshness Check

Verified 2026-05-22 against commit `273b491` (HEAD on main).

| Reference | Status | Notes |
|-----------|--------|-------|
| `src/popoto/fields/bm25_field.py` — `BM25Field.search()` at line 487 | **Unchanged** | Returns `list[tuple[str, float]]` as expected |
| `src/popoto/models/query.py:894` — `QueryBuilder.fuse()` | **Unchanged** | Accepts `**ranked_lists` of `(redis_key, score)` tuples |
| `src/popoto/models/query.py:2220` — `Query.fuse()` convenience wrapper | **Unchanged** | Delegates to `QueryBuilder.fuse()` |
| `src/popoto/fields/embedding_field.py` — `EmbeddingField` | **Updated** | No `encode()` or `similarity_search()` method exists. Vector retrieval goes through `QueryBuilder.semantic_search()` internally (lines 637–760) which calls `provider.embed()` + `EmbeddingField.load_embeddings()`. The hybrid path must use this internal path, not a non-existent `EmbeddingField.encode()`. |
| `src/popoto/recipes/context_assembler.py:691` — `ContextAssembler.__init__` | **Unchanged** | No `retrieval_mode` param yet. Field-walk loop detects `ExistenceFilter`, `CoOccurrenceField`, `CyclicDecayField`, `ConfidenceField`, `DecayingSortedField`. |
| Issue #394 (benchmark harness) | **Closed — merged** | Landed in commit `273b491`. `tests/benchmarks/` exists with LongMemEval-S + LoCoMo adapters. |
| Issue #304 (BM25 + RRF primitives) | **Closed — merged** | PR #306 merged 2026-03-30. `BM25Field` and `CompositeScoreQuery.fuse()` are live. |

**Disposition: Minor drift.** The prior plan draft referenced `self._embedding_field.encode()` and
`self._embedding_field.similarity_search()` — neither method exists on `EmbeddingField`. The correct
interface is `QueryBuilder._get_vector_scores()` (a new private helper to add) that reuses the
`provider.embed()` + `EmbeddingField.load_embeddings()` logic from `semantic_search()` but returns
`[(redis_key, score)]` tuples instead of hydrated instances.

## Research

**Search queries used:**
1. "Reciprocal Rank Fusion hybrid retrieval BM25 vector RRF k=60 best practices 2025"
2. "agentmemory LongMemEval hybrid retrieval RRF implementation pattern 2025"

**Key findings:**

- **k=60 is validated**: Cormack et al. 2009 tuned k=60 on TREC data; it generalizes across
  corpora. Production guidance (BigData Boutique, OpenSearch) ships k=60 as default. No reason
  to deviate. ([BigData Boutique](https://bigdataboutique.com/blog/reciprocal-rank-fusion-how-it-works-and-when-to-use-it),
  [OpenSearch](https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/))

- **agentmemory implementation pattern**: 3 parallel streams (BM25 with stemming, vector cosine
  similarity, graph spreading activation) fused via RRF k=60. Six signals total including confidence
  and temporal proximity. The hybrid BM25+vector achieves 95.2% R@5 vs 86.2% BM25-only.
  ([agentmemory GitHub](https://github.com/JordanMcCann/agentmemory))

- **RRF sidesteps score normalization**: RRF uses rank position, not raw scores — BM25 scores
  and cosine similarities are incommensurable, but ranks combine naturally. No calibration needed.
  ([avchauzov hybrid retrieval blog](https://avchauzov.github.io/blog/2025/hybrid-retrieval-rrf-rank-fusion/))

**How findings inform the plan**: k=60 is confirmed as the right default. The 3-signal pattern
(BM25 + vector + graph) maps directly to what Popoto has available. The plan's goal of wiring these
as the ContextAssembler default is validated as the single highest-ROI retrieval improvement.

## Prior Art

| Source | Relevance |
|--------|-----------|
| Issue #304 / PR #306 (merged 2026-03-30) | Built the BM25Field + fuse() primitives this plan wires as the default |
| Issue #394 (merged 2026-05-22) | Benchmark harness used as measurement gate for this PR |
| `docs/plans/hybrid_retrieval.md` | Construction plan for BM25Field + fuse() — this plan is the consumer |
| `docs/plans/qmd_retrieval_investigation.md` | Investigation doc that explicitly defers position-aware blending to #397, after #395 lands |

## Data Flow

### Current pull path

```
query_cues → ExistenceFilter pre-check
           → composite_score(score_weights, limit=max_items*2)
           → CoOccurrenceField propagation (seeds from top-N candidates)
           → re-run composite_score with co_occurrence_boost
           → (records, all_candidates)
```

### New hybrid pull path (`retrieval_mode="hybrid"` or auto-detected)

```
query_cues → ExistenceFilter pre-check
           → BM25Field.search(query_text, limit=candidate_limit)    → keyword_results [(key, score)]
           → QueryBuilder._get_vector_scores(query_text, limit=...)  → vector_results  [(key, score)]
           → CoOccurrenceField.propagate(bm25_seed_pks)              → graph_results   [(key, weight)]
           → query.fuse(keyword=..., vector=..., graph=..., k=RRF_K, limit=max_items*2)
           → (hydrated_records, all_candidates)
```

### Composite pull path (fallback / explicit `retrieval_mode="composite"`)

Unchanged — identical to current behavior.

### Key interface clarification

`fuse()` accepts `**ranked_lists` where each value is `list[(redis_key, score)]`. The sources map as:
- `keyword_results`: `BM25Field.search()` already returns this format
- `vector_results`: new `QueryBuilder._get_vector_scores()` returns this format
- `graph_results`: `CoOccurrenceField.propagate()` returns `{pk: weight}` — convert with `list(propagated.items())`

## Architectural Impact

### 1. New parameter on `ContextAssembler.__init__`

`retrieval_mode: str = "auto"` (new, keyword-only after existing params for backwards compat)

- `"auto"` (default): detect `BM25Field` + `EmbeddingField`; if both present → `"hybrid"`; otherwise → `"composite"`
- `"hybrid"`: always use BM25 + vector + graph RRF. Raises `QueryException` if required fields absent at init time.
- `"composite"`: current behavior, unchanged.

After field-walk loop, compute `_effective_mode`:
```python
if retrieval_mode == "auto":
    self._effective_mode = (
        "hybrid" if (self._bm25_field and self._embedding_field) else "composite"
    )
elif retrieval_mode == "hybrid":
    if self._bm25_field is None or self._embedding_field is None:
        from ..exceptions import QueryException
        raise QueryException(
            "retrieval_mode='hybrid' requires BM25Field and EmbeddingField on the model"
        )
    self._effective_mode = "hybrid"
else:
    self._effective_mode = "composite"
```

### 2. New capability detection in field-walk loop

Add to existing `for name, f in model_class._meta.fields.items()` loop in `__init__`:
```python
from ..fields.bm25_field import BM25Field
from ..fields.embedding_field import EmbeddingField

if isinstance(f, BM25Field) and self._bm25_field is None:
    self._bm25_field = f
    self._bm25_field_name = name
if isinstance(f, EmbeddingField) and self._embedding_field is None:
    self._embedding_field = f
    self._embedding_field_name = name
```

Initialize `self._bm25_field = None`, `self._bm25_field_name = None`, `self._embedding_field = None`, `self._embedding_field_name = None` before the loop.

### 3. New `QueryBuilder._get_vector_scores()` method

A private method on `QueryBuilder` that returns `[(redis_key, score)]` tuples (not hydrated instances).
This reuses the `provider.embed()` + `EmbeddingField.load_embeddings()` logic from `semantic_search()`
without the hydration step:

```python
def _get_vector_scores(self, query_text: str, limit: int = 10) -> list[tuple[str, float]]:
    """Return (redis_key, cosine_similarity) tuples for hybrid RRF fusion.

    Identical to semantic_search() internals but returns raw scored pairs
    instead of hydrated instances. Used by ContextAssembler._pull_path_hybrid().

    Returns:
        list[(redis_key, score)] sorted by score descending. Empty if no
        provider configured or no embeddings exist.
    """
```

Avoids duplicating numpy / cosine-similarity logic — this is the single authoritative
raw-vector-scores computation. All existing `semantic_search()` internals remain untouched.

### 4. New `ContextAssembler._pull_path_hybrid()` method

```python
def _pull_path_hybrid(self, query_cues, filters):
    """Hybrid pull path: BM25 + vector + graph via RRF."""
    query_text = " ".join(str(v) for v in query_cues.values())

    # ExistenceFilter pre-check (same as composite path)
    if self._existence_filter is not None:
        all_missing = all(
            self._existence_filter.definitely_missing(self.model_class, str(v))
            for v in query_cues.values()
        )
        if all_missing:
            return [], []

    candidate_limit = self.max_items * HYBRID_CANDIDATE_MULTIPLIER  # = max_items * 5

    keyword_results = []
    vector_results = []
    graph_results = []

    # BM25 lexical retrieval
    try:
        keyword_results = BM25Field.search(
            self.model_class, self._bm25_field_name, query_text, limit=candidate_limit,
        )
    except Exception as e:
        logger.warning("BM25 search failed in hybrid path: %s", e)

    # Vector semantic retrieval (raw scored tuples, not hydrated instances)
    try:
        query = self.model_class.query
        if filters:
            query = query.filter(**filters)
        builder = query._get_builder()  # or direct QueryBuilder instantiation
        vector_results = builder._get_vector_scores(query_text, limit=candidate_limit)
    except Exception as e:
        logger.warning("Vector search failed in hybrid path: %s", e)

    # Graph propagation (optional — seeds from BM25 top results)
    if self._co_occurrence_field is not None and keyword_results:
        seed_pks = [k for k, _ in keyword_results[:5]]
        try:
            propagated = self._co_occurrence_field.propagate(
                self.model_class, seed_pks,
                depth=self.propagation_depth, decay_per_hop=0.5, threshold=0.01,
            )
            graph_results = list(propagated.items())
        except Exception as e:
            logger.warning("Graph propagation failed in hybrid path: %s", e)

    if not keyword_results and not vector_results:
        # Nothing to fuse — fall back to composite
        logger.debug("Hybrid path: no signals collected, falling back to composite")
        return self._pull_path_composite(query_cues, filters)

    # Build fuse kwargs — only include non-empty lists
    fuse_kwargs = {}
    if keyword_results:
        fuse_kwargs["keyword"] = keyword_results
    if vector_results:
        fuse_kwargs["vector"] = vector_results
    if graph_results:
        fuse_kwargs["graph"] = graph_results

    try:
        query = self.model_class.query
        if filters:
            query = query.filter(**filters)
        candidates = query.fuse(k=RRF_K, limit=self.max_items * 2, **fuse_kwargs)
    except Exception as e:
        logger.warning("RRF fusion failed, falling back to composite: %s", e)
        return self._pull_path_composite(query_cues, filters)

    return candidates, list(candidates)
```

### 5. `_pull_path` dispatch

Rename current `_pull_path` body → `_pull_path_composite`, then:
```python
def _pull_path(self, query_cues, filters):
    if self._effective_mode == "hybrid":
        return self._pull_path_hybrid(query_cues, filters)
    return self._pull_path_composite(query_cues, filters)
```

### 6. Module-level constants

Add to `context_assembler.py` (alongside existing `DEFAULT_MAX_ITEMS` etc.):
```python
RRF_K = 60                       # Cormack et al. 2009 standard — do not expose as user config
HYBRID_CANDIDATE_MULTIPLIER = 5  # candidate_limit = max_items * this
```

### No-changes zone

- Push path (`_push_path`) — untouched
- Merge, budget selection, post-effects, formatters — untouched
- `score_weights` parameter — still valid and used by composite path unchanged
- `CompositeScoreQuery.composite_score()` — untouched
- `QueryBuilder.fuse()` — untouched (consumed, not modified)

## Appetite

**Size:** Medium (one new method on `QueryBuilder`, one new method on `ContextAssembler`, one new
`__init__` param, capability detection extension, unit tests)

**Team:** Solo dev

**Review rounds:** 1

## Prerequisites

| Requirement | Check | Status |
|-------------|-------|--------|
| BM25Field ships (PR #306) | `python -c "from popoto.fields.bm25_field import BM25Field"` | Done |
| `CompositeScoreQuery.fuse()` ships (PR #306) | `python -c "from popoto.models.query import QueryBuilder; assert hasattr(QueryBuilder, 'fuse')"` | Done |
| `EmbeddingField` available | `python -c "from popoto.fields.embedding_field import EmbeddingField"` | Done |
| Benchmark harness (#394) | `pytest tests/benchmarks/ -v` | Done — merged |

**All prerequisites satisfied.** This plan is unblocked.

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] `retrieval_mode="hybrid"` on model without `BM25Field` → `QueryException` at init
- [ ] `retrieval_mode="hybrid"` on model without `EmbeddingField` → `QueryException` at init
- [ ] `retrieval_mode="auto"` on model without `BM25Field` → silently uses `"composite"`, no error
- [ ] `retrieval_mode="auto"` on model without `EmbeddingField` → silently uses `"composite"`, no error
- [ ] BM25 search throws at runtime → logs warning, continues with vector-only RRF
- [ ] Vector search throws at runtime → logs warning, continues with keyword-only RRF
- [ ] Both signals produce empty lists → falls back to composite path, logs debug
- [ ] `fuse()` raises → falls back to composite path, logs warning

### Backwards Compatibility

- [ ] `ContextAssembler(score_weights={"relevance": 0.6})` on model without BM25/EmbeddingField → composite path, no error
- [ ] `retrieval_mode="composite"` → always uses composite path regardless of field presence
- [ ] Existing `assemble()` callers get identical behavior when model lacks BM25/EmbeddingField

## Test Impact

**New test file: `tests/test_context_assembler_hybrid.py`**

Tests (all require Redis on localhost:6379 — standard popoto test pattern):

- `test_auto_mode_selects_hybrid_when_fields_present` — model with BM25 + EmbeddingField → `_effective_mode == "hybrid"`
- `test_auto_mode_selects_composite_when_bm25_absent` — model without BM25Field → `_effective_mode == "composite"`
- `test_auto_mode_selects_composite_when_embedding_absent` — model without EmbeddingField → `_effective_mode == "composite"`
- `test_hybrid_mode_raises_without_bm25` — `QueryException` at `__init__`
- `test_hybrid_mode_raises_without_embedding` — `QueryException` at `__init__`
- `test_hybrid_pull_path_combines_signals` — seed corpus, run hybrid assemble, verify results returned
- `test_hybrid_fallback_when_both_signals_empty` — mock BM25+vector to return empty → composite fallback used, no crash
- `test_hybrid_fallback_on_bm25_exception` — mock BM25.search to raise → warning logged, vector-only RRF or composite fallback
- `test_composite_mode_unchanged` — `retrieval_mode="composite"` → only `_pull_path_composite` called
- `test_score_weights_still_honored_in_composite_mode` — `score_weights` param drives composite path
- `test_get_vector_scores_returns_tuples` — `QueryBuilder._get_vector_scores()` returns `[(redis_key, float)]`

**Existing tests:** All `tests/test_context_assembler.py` tests must pass unchanged.
Regression run: `pytest tests/test_context_assembler.py -v`

## Measurement Gate

Every PR touching this change must include a benchmark report from the #394 harness showing
R@5 and MRR delta vs. composite baseline on LongMemEval-S and LoCoMo.

**Decision rule:**
- If hybrid beats composite: commit report, open PR, change default
- If hybrid does not beat composite: document finding in PR, surface as option only, do NOT change default

## Rabbit Holes

- **Tuning `RRF_K` per-corpus:** Do not expose as user-facing init param in this PR. The constant
  is documented and subclassable. A tuning follow-up can add it once #394 harness gives data.
- **Position-aware blend weights (QMD-style 75/25 lexical at rank 1, 40/60 at tail):** Out of
  scope. Standard equal-weight RRF first; #397 investigates whether position-aware weights are
  worth the complexity.
- **`SubconsciousMemory` update:** Defer. It has its own retrieval logic; routing it through
  `ContextAssembler._pull_path_hybrid` is a separate change.
- **Provider None guard:** If `embedding_field.provider` is None (not configured), `_get_vector_scores()`
  returns `[]`. Auto-mode with provider=None silently falls back to composite. This is correct
  behavior — do not add a warning at assemble-time, only at init if `retrieval_mode="hybrid"` was
  explicitly requested.

## Risks

### Risk 1: Latency regression
**Impact:** Two retrieval calls (BM25 + vector) instead of one (composite) adds per-query latency.
**Mitigation:** BM25 is Valkey-side Lua (sub-ms for small corpora). Vector encode is the likely
bottleneck. Benchmark p95 before committing the default. Gate hybrid-as-default on p95 < 100ms.

### Risk 2: `_get_vector_scores` API surface
**Impact:** Adding a private method to `QueryBuilder` exposes internals. Future refactors to
`semantic_search()` must also update `_get_vector_scores()`.
**Mitigation:** Document clearly as "for hybrid path only." Single function, small scope.
Long-term the two can be unified into a shared `_compute_similarity_scores()`.

### Risk 3: Empty fuse when provider not configured
**Impact:** Auto-mode model has EmbeddingField but no provider → `_get_vector_scores()` returns `[]`
→ only BM25 signal goes to `fuse()` → single-signal RRF (mathematically identical to BM25-only ranking).
**Mitigation:** Single-signal `fuse()` is valid. `fuse()` already guards against empty `ranked_lists`.
Log a debug message noting the provider is absent. No user action required.

## No-Gos (Out of Scope)

- Adding a new embedding model or vector index implementation
- Changes to `composite_score()` method
- LLM reranking (tracked in #397)
- Memory consolidation / decay lifecycle (tracked in #396)
- Defining new benchmark metrics — uses #394 harness
- Exposing `RRF_K` as a user-facing init parameter

## Step by Step Tasks

### 1. Read and document current interfaces
- **Task ID**: read-interfaces
- **Depends On**: none
- **Parallel**: true
- Read `QueryBuilder.semantic_search()` (lines 637–760 in `models/query.py`) — confirm `provider.embed()` + `EmbeddingField.load_embeddings()` sequence
- Read `QueryBuilder.fuse()` (lines 894–980) — confirm `**ranked_lists` accepts `[(redis_key, score)]` tuples
- Read `BM25Field.search()` (line 487 in `fields/bm25_field.py`) — confirm return type
- Read `CoOccurrenceField.propagate()` — confirm return type `{pk: weight}`
- Note the correct import path for `QueryBuilder` (for `_get_vector_scores` placement)

### 2. Add `QueryBuilder._get_vector_scores()`
- **Task ID**: add-vector-scores-helper
- **Depends On**: read-interfaces
- **Parallel**: false
- In `src/popoto/models/query.py`, add private method `_get_vector_scores(self, query_text, limit)` to `QueryBuilder`
- Implementation: mirror `semantic_search()` internals — embed query, load embeddings, compute cosine similarity, return `[(redis_key, score)]` sorted descending
- Return `[]` if no provider, no embeddings, or `query_text` is empty
- Add docstring noting "for hybrid RRF path only — returns tuples, not instances"

### 3. Extend `ContextAssembler.__init__` capability detection
- **Task ID**: extend-capability-detection
- **Depends On**: read-interfaces
- **Parallel**: false
- Add `retrieval_mode: str = "auto"` parameter to `__init__`
- Initialize `self._bm25_field`, `self._bm25_field_name`, `self._embedding_field`, `self._embedding_field_name` to `None` before field-walk
- Add `BM25Field` + `EmbeddingField` detection in field-walk loop (import at top of `context_assembler.py`)
- Compute `self._effective_mode` after loop
- Raise `QueryException` for `retrieval_mode="hybrid"` when required fields absent
- Update class docstring to document `retrieval_mode` parameter

### 4. Implement `_pull_path_hybrid()`
- **Task ID**: build-hybrid-pull
- **Depends On**: extend-capability-detection, add-vector-scores-helper
- **Parallel**: false
- Add `RRF_K = 60` and `HYBRID_CANDIDATE_MULTIPLIER = 5` module-level constants
- Implement `_pull_path_hybrid(self, query_cues, filters)` per design above
- Add imports: `from ..fields.bm25_field import BM25Field` in `context_assembler.py`

### 5. Refactor `_pull_path` → `_pull_path_composite` + dispatch
- **Task ID**: rename-composite-path
- **Depends On**: build-hybrid-pull
- **Parallel**: false
- Rename existing `_pull_path` to `_pull_path_composite`
- Add new `_pull_path` that dispatches based on `self._effective_mode`
- Run `pytest tests/test_context_assembler.py -v` — all tests must pass

### 6. Write tests
- **Task ID**: write-tests
- **Depends On**: rename-composite-path
- **Parallel**: false
- Create `tests/test_context_assembler_hybrid.py`
- Implement all 11 test cases listed above
- Run `pytest tests/test_context_assembler.py tests/test_context_assembler_hybrid.py -v`

### 7. Benchmark and measure
- **Task ID**: benchmark
- **Depends On**: write-tests
- **Parallel**: false
- Run `pytest tests/benchmarks/ -v -k "longmemeval or locomo"` with hybrid vs. composite
- Compare R@5 and MRR delta
- If hybrid wins: commit report to `docs/benchmarks/context_assembler_hybrid_vs_composite.md`, proceed
- If hybrid does not win: document finding, keep feature as option only, update PR description

### 8. Documentation
- **Task ID**: docs
- **Depends On**: benchmark
- **Parallel**: false
- Update `docs/features/context-assembler.md` with `retrieval_mode` parameter documentation
- Add mode comparison table and guidance ("when to override")
- Update `docs/features/hybrid-retrieval.md` to note ContextAssembler integration (link to context-assembler.md)
- Add `CHANGELOG` entry under next version

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit 0 |
| Hybrid tests | `pytest tests/test_context_assembler_hybrid.py -v` | exit 0 |
| Existing CA tests | `pytest tests/test_context_assembler.py -v` | exit 0 |
| No regressions | `pytest tests/ -x` | exit 0 |
| Format | `black --check src/ tests/` | exit 0 |
| Docs build | `mkdocs build` | exit 0 |
| Benchmark report | `ls docs/benchmarks/context_assembler_hybrid_vs_composite.md` | file present |

## Success Criteria

- [ ] `ContextAssembler(model_class=M, score_weights=..., retrieval_mode="hybrid")` works on M with BM25Field + EmbeddingField
- [ ] `retrieval_mode="auto"` selects `"hybrid"` when both fields present, `"composite"` otherwise
- [ ] Existing `ContextAssembler` callers with composite/score_weights keep working without migration
- [ ] `QueryBuilder._get_vector_scores()` returns `[(redis_key, float)]` tuples for fuse() input
- [ ] Benchmark report (from #394) showing R@5/MRR delta committed with PR
- [ ] No Redis-module dependencies (Valkey-compat maintained)
- [ ] `docs/features/context-assembler.md` updated with `retrieval_mode` parameter
- [ ] `CHANGELOG` entry added

## Open Questions

1. **`score_weights` when in hybrid mode:** Should passing `score_weights` with `retrieval_mode="auto"`
   emit a debug log message if auto selects hybrid (since weights are ignored for the pull path)?
   Lean: yes, log at DEBUG level — silent ignore is confusing in production.

2. **Graph seeds from BM25 vs. vector results:** The design uses BM25 top-5 as graph seeds.
   Should we use the union of BM25+vector top-5 as seeds? Lean: BM25 top-5 only for now — simpler,
   matches agentmemory pattern, can tune later.

3. **`_get_vector_scores()` filter handling:** Should vector scoring respect `filters` (partition
   filters)? Currently `similarity_boost` in `semantic_search()` is computed across all keys then
   filtered during composite_score. For `fuse()`, filtering happens on the query object. Confirm
   `query.filter(**filters).fuse(...)` correctly applies filters — this needs a unit test.
