---
status: In Progress
type: feature
appetite: Large
owner: Valor
created: 2026-03-29
tracking: https://github.com/tomcounsell/popoto/issues/304
last_comment_id:
---

# Hybrid Retrieval: BM25 + RRF Fusion for Multi-Signal Ranked Search

## Problem

Popoto's retrieval layer lacks ranked keyword search. `ExistenceFilter` answers "does this term appear?" (boolean) but not "how relevant is this document to these terms?" (scored). Without keyword ranking, the fusion layer (`CompositeScoreQuery`) has nothing to fuse with vector scores — it operates on weighted sums of pre-computed sorted set indexes, not on heterogeneous ranked lists from different retrieval signals.

**Current behavior:**
- `ExistenceFilter.might_exist("kubernetes")` returns True/False — no ranking.
- `EmbeddingField` + `semantic_search()` returns cosine similarity scores — good for meaning, bad for exact terms like "error code 4012".
- `CompositeScoreQuery.composite_score()` fuses via weighted ZUNIONSTORE — requires all signals to be Redis sorted sets with comparable score semantics. Cannot combine ranked lists from different retrieval modalities.

**Desired outcome:**
1. A `BM25Field` that maintains TF-IDF statistics in Redis sorted sets and computes BM25 scores at query time. Pure Redis structures, no modules.
2. An RRF fusion mode in `CompositeScoreQuery` that combines heterogeneous ranked lists (keyword, vector, graph, temporal) using Reciprocal Rank Fusion.
3. A documented hybrid retrieval recipe showing how to wire all signals together.

## Prior Art

- **ExistenceFilter** (`fields/existence_filter.py`): Bloom filter for boolean term existence. Uses Lua + Redis strings. The tokenization logic (lowercasing, stop words, min-length filtering) is directly reusable for BM25Field.
- **CompositeScoreQuery** (`models/query.py:composite_score()`): Weighted ZUNIONSTORE fusion. Already supports `similarity_boost` and `co_occurrence_boost` dict injection. The RRF fusion mode follows the same injection pattern but replaces ZUNIONSTORE with rank-based scoring.
- **EmbeddingField** (`fields/embedding_field.py`): Vector similarity via cosine on cached numpy matrices. Returns `{redis_key: score}` dicts that feed into `similarity_boost`.
- **CoOccurrenceField** (`fields/co_occurrence_field.py`): Graph propagation returns `{pk: weight}` dicts that feed into `co_occurrence_boost`.
- **ContentField** (`fields/content_field.py`): Filesystem-backed large content storage. BM25Field will often pair with ContentField — the content is stored on disk, the BM25 statistics are in Redis.
- **ContextAssembler** (`recipes/context_assembler.py`): Retrieval orchestrator that calls composite_score(). Will be the primary consumer of hybrid retrieval.
- **SubconsciousMemory** (`recipes/subconscious_memory.py`): End-to-end memory recipe. The hybrid retrieval recipe builds on the same pattern.

No prior issues or PRs related to BM25 or RRF in this repository. This is greenfield work.

## Data Flow

### BM25Field — Save Path

1. Model instance saved via `Model.save()`.
2. `BM25Field.on_save()` hook fires. Reads the source field value (e.g., `content`).
3. Tokenizes text using shared tokenization logic (reuse from ExistenceFilter: lowercase, split on non-word chars, filter short tokens, remove stop words).
4. Executes a Lua script that atomically:
   - Removes old term frequencies for this document key (if updating).
   - Computes term frequencies for the new content.
   - Updates four Redis structures:
     - `$BM25:{Class}:{field}:tf:{doc_key}` — sorted set: term -> term_frequency
     - `$BM25:{Class}:{field}:df` — sorted set: term -> document_frequency
     - `$BM25:{Class}:{field}:dl` — sorted set: doc_key -> document_length (token count)
     - `$BM25:{Class}:{field}:n` — string: total document count
5. On delete: `on_delete()` reverses — decrements df for each term, removes tf sorted set, removes dl entry, decrements n.

### BM25Field — Query Path

1. Caller invokes `Model.query.keyword_search("deployment redis timeout", field="content", limit=20)`.
2. Query tokenizes the input using the same tokenizer.
3. Executes a Lua script that:
   - Reads N (total docs) from the counter string.
   - Reads avgdl by computing mean of all dl entries (or caches it in a separate key).
   - For each query term: reads df, then iterates documents that contain the term (via tf sorted set members) and computes BM25 per-term score.
   - Accumulates per-document BM25 scores across all query terms.
   - Returns top-K documents sorted by score.
4. Returns list of `(model_instance, bm25_score)` tuples after hydration.

### RRF Fusion Path

1. Caller invokes `CompositeScoreQuery.fuse()` with named ranked lists.
2. Each ranked list is a sequence of `(redis_key, score)` tuples — the scores are used only for ordering within each list, not cross-list comparison.
3. For each document appearing in any list, compute RRF score: `score(d) = sum(1 / (k + rank_i(d)))` where `rank_i(d)` is the 1-based rank in list i, and k is the RRF constant (default 60).
4. Sort by RRF score descending, take top-K.
5. Hydrate model instances.
6. Apply optional post_filter.

## Architectural Impact

- **New dependencies**: None. Uses only core Redis commands (ZADD, ZINCRBY, ZRANGEBYSCORE, ZCARD, GET, SET, EVAL) and Lua scripts. No modules, no numpy (BM25 is pure math).
- **New files**:
  - `src/popoto/fields/bm25_field.py` — BM25Field class
  - `tests/test_bm25_field.py` — BM25Field tests
  - `tests/test_rrf_fusion.py` — RRF fusion tests
  - `tests/test_hybrid_retrieval.py` — End-to-end hybrid retrieval tests
  - `docs/features/hybrid-retrieval.md` — Feature documentation
- **Interface changes**:
  - New `BM25Field` field type with `keyword_search()` class method.
  - New `keyword_search()` method on `QueryBuilder` / `Query` (delegates to BM25Field).
  - New `fuse()` method on `QueryBuilder` / `Query` for RRF fusion.
  - Existing `composite_score()` unchanged — RRF is a separate method, not a mode flag.
- **Coupling**: Low. BM25Field is a standalone field type following the same patterns as ExistenceFilter. RRF fusion is a new method on QueryBuilder, parallel to composite_score(). Neither modifies existing fields or methods.
- **Data ownership**: BM25Field owns its `$BM25:` prefixed keys. RRF fusion creates no persistent state — it operates on in-memory ranked lists.
- **Reversibility**: Easy — purely additive. Removing BM25Field removes its Redis keys. Removing fuse() removes the method. No schema changes to existing data.

## Appetite

**Size:** Large (three deliverables: field, fusion method, recipe)

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (validate BM25 storage design before implementation)
- Review rounds: 1-2

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis available | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Redis connection + Lua scripting |
| CompositeScoreQuery exists | `python -c "from popoto.models.query import QueryBuilder; assert hasattr(QueryBuilder, 'composite_score')"` | Dependency for fusion |
| ExistenceFilter exists | `python -c "from popoto.fields.existence_filter import ExistenceFilter"` | Tokenizer reuse |

## Solution

### Key Elements

#### 1. BM25Field

A new Field subclass that maintains term frequency / document frequency statistics in Redis sorted sets. Computes BM25(k1=1.2, b=0.75) scores at query time via Lua.

**Class constants (tunable):**
```python
class BM25Field(Field):
    BM25_K1 = 1.2   # Term frequency saturation
    BM25_B = 0.75    # Document length normalization
```

These are class-level constants, not per-instance configuration. They can be overridden by subclassing for experimental tuning.

**Redis key patterns:**
```
$BM25:{ClassName}:{field_name}:tf:{doc_key}  — ZSET {term: tf}  (per document)
$BM25:{ClassName}:{field_name}:df            — ZSET {term: df}  (corpus-wide)
$BM25:{ClassName}:{field_name}:dl            — ZSET {doc_key: doc_length}
$BM25:{ClassName}:{field_name}:n             — STRING doc_count
$BM25:{ClassName}:{field_name}:avgdl         — STRING avg_doc_length (cached, updated on save)
```

All native Redis sorted sets and strings. No modules. Works on Redis and Valkey.

**Tokenization:** Reuse the tokenization logic from ExistenceFilter (`_tokenize()` function). Extract it into a shared utility in `fields/` or import directly. Same behavior: lowercase, split on `\W+`, filter tokens < 3 chars, remove stop words.

**on_save() Lua script:**
- KEYS: tf:{doc_key}, df, dl, n, avgdl
- Reads old tf set for this doc (if exists) and decrements df for old terms
- Tokenizes new content, computes tf for each term
- Writes new tf set, increments df for new terms
- Updates dl with new doc length
- Increments n if new document (checks dl membership)
- Recomputes avgdl as running average

**on_delete() Lua script:**
- Reads tf set for this doc, decrements df for each term
- Removes tf set, dl entry
- Decrements n
- Recomputes avgdl

**keyword_search() Lua script:**
- KEYS: df, dl, n, avgdl
- ARGV: query terms, limit, k1, b
- For each query term: get df, then scan tf sets of candidate documents
- Candidate selection: union of all documents that contain at least one query term (read from df members, then check tf sets). This is O(df * terms) which is acceptable for typical memory corpus sizes (< 100K docs).
- Compute BM25 score per document: `sum over terms of: idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl/avgdl))`
- Where `idf = log((N - df + 0.5) / (df + 0.5) + 1)`
- Return top-K by score

**API:**
```python
class Memory(Model):
    key = AutoKeyField()
    content = BM25Field(source="raw_content")  # source field to index
    embedding = EmbeddingField(source="raw_content")
    raw_content = ContentField()

# Ranked keyword search
results = Memory.query.keyword_search("deployment redis timeout", field="content", limit=20)
# Returns list of model instances (scores available via _bm25_score attr)

# Or via class method for raw scores
scored = BM25Field.search(Memory, "content", "deployment redis timeout", limit=20)
# Returns [(redis_key, bm25_score), ...]
```

#### 2. RRF Fusion in CompositeScoreQuery

A new `fuse()` method on QueryBuilder that accepts named ranked lists and combines them using Reciprocal Rank Fusion.

**RRF formula:** `score(d) = sum(1 / (k + rank_i(d)))` for each list i where document d appears.

**RRF constant k=60** (default). This is the standard value from the original Cormack et al. paper. Configurable via parameter.

**Implementation:** Pure Python — no Lua needed. RRF operates on ranked lists already materialized in Python memory. Each input list is a sequence of `(redis_key, score)` tuples.

```python
class QueryBuilder:
    def fuse(
        self,
        k: int = 60,
        limit: int = 10,
        post_filter: Optional[Callable] = None,
        **ranked_lists,  # keyword=[(key, score), ...], semantic=[(key, score), ...]
    ) -> list:
        """Reciprocal Rank Fusion across heterogeneous ranked lists.

        Args:
            k: RRF constant (default 60). Higher values reduce the influence
               of high-ranking items. The standard value from Cormack et al.
            limit: Maximum results to return.
            post_filter: Optional (redis_key, rrf_score) -> bool callback.
            **ranked_lists: Named ranked lists. Each value is a list of
                (redis_key, score) tuples sorted by score descending. The
                scores are used only for ordering; RRF uses ranks, not scores.

        Returns:
            List of model instances ranked by RRF score (descending).
        """
```

**Why a separate method, not a mode on composite_score():**
- `composite_score()` operates on Redis sorted set indexes via ZUNIONSTORE. It's server-side.
- `fuse()` operates on Python-side ranked lists from heterogeneous sources (BM25 search results, embedding similarity results, co-occurrence propagation results). It's client-side.
- Mixing them would muddy both APIs. They serve different use cases and compose well: you can feed `composite_score()` output as one of the ranked lists into `fuse()`.

#### 3. Hybrid Retrieval Recipe

A documented recipe (like PolicyCache, ContextAssembler, SubconsciousMemory) showing the recommended pattern for combining BM25 + vector + graph + temporal signals.

**Location:** Not a new recipe class — a section in the hybrid retrieval feature doc with copy-paste code. The existing SubconsciousMemory recipe will be updated to optionally use hybrid retrieval when BM25Field is present on the model.

**Pattern:**
```python
# 1. Keyword search
keyword_results = BM25Field.search(Memory, "content", query_text, limit=50)

# 2. Semantic search
query_vector = get_embedding(query_text)
semantic_results = Memory.query.filter(agent_id=agent_id).semantic_search(
    "embedding", query_vector, limit=50
)

# 3. Graph propagation (optional)
seed_pks = [key for key, _ in keyword_results[:5]]
graph_results = CoOccurrenceField.propagate(Memory, seed_pks, depth=2)

# 4. RRF fusion
results = Memory.query.filter(agent_id=agent_id).fuse(
    keyword=keyword_results,
    semantic=semantic_results,
    graph=list(graph_results.items()),
    k=60,
    limit=10,
)
```

### Flow

**Save path:** Model.save() -> BM25Field.on_save() -> Lua script updates tf/df/dl/n/avgdl sorted sets

**Query path:** keyword_search() -> tokenize query -> Lua script computes BM25 scores -> return ranked list -> optional RRF fusion with other signals -> hydrate models

### Technical Approach

#### Shared Tokenizer

Extract the tokenization logic from `ExistenceFilter` into a shared module. Both ExistenceFilter and BM25Field need the same preprocessing: lowercase, split on `\W+`, filter short tokens, remove stop words.

**Location:** `src/popoto/fields/_tokenizer.py` (private module, underscore prefix)

The ExistenceFilter currently has `_tokenize()` as a module-level function. Move it to the shared module and import from both places. This is a refactor, not a behavior change.

#### BM25 Lua Scripts

Three Lua scripts, stored as module-level string constants in `bm25_field.py`:

1. **BM25_SAVE_LUA** — Atomic save: remove old terms, add new terms, update stats.
2. **BM25_DELETE_LUA** — Atomic delete: remove terms, update stats.
3. **BM25_SEARCH_LUA** — Query: compute BM25 scores across candidate documents, return top-K.

The search Lua script is the most complex. Key design decision: iterate candidate documents via the df sorted set (which terms match the query) rather than scanning all documents. For each query term, get the set of documents containing that term by scanning the df-keyed tf sets. This avoids a full corpus scan.

**Candidate document discovery in Lua:**
- For each query term, use `ZRANGEBYSCORE $BM25:...:df term term` to confirm the term exists in the corpus.
- For each matching term, we need to find which documents contain it. Since tf is stored per-document (`tf:{doc_key}`), we cannot directly look up "all docs containing term X" without an inverted index.
- **Solution: Add an inverted index sorted set** — `$BM25:{Class}:{field}:inv:{term}` mapping `doc_key -> tf` for each term. This is the standard inverted index structure and enables efficient per-term document lookup.
- Updated key patterns:
  - `$BM25:{Class}:{field}:inv:{term}` — ZSET {doc_key: tf} (inverted index, per term)
  - `$BM25:{Class}:{field}:df` — ZSET {term: df}
  - `$BM25:{Class}:{field}:dl` — ZSET {doc_key: doc_length}
  - `$BM25:{Class}:{field}:n` — STRING doc_count
  - `$BM25:{Class}:{field}:avgdl` — STRING avg_doc_length
- The per-document tf sorted set (`tf:{doc_key}`) is still needed for on_save/on_delete to know which terms to update. But query-time lookup uses the inverted index.

This trades storage (one extra sorted set per unique term) for query performance (O(sum of df for query terms) instead of O(N * terms)).

#### Average Document Length Caching

Recomputing avgdl on every query would require scanning the entire dl sorted set. Instead:
- Cache avgdl as a Redis string, updated incrementally on save/delete.
- Formula: `new_avgdl = ((old_avgdl * (n-1)) + new_dl) / n` on save, reverse on delete.
- This is computed inside the save/delete Lua scripts for atomicity.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `keyword_search()` with empty query string -> return empty list
- [ ] `keyword_search()` with all stop words -> return empty list (all tokens filtered)
- [ ] `keyword_search()` on a field that is not a BM25Field -> raise QueryException
- [ ] `fuse()` with no ranked lists -> raise QueryException
- [ ] `fuse()` with empty ranked lists -> return empty list
- [ ] BM25Field on a model with no source field -> raise configuration error on class creation

### Empty/Invalid Input Handling
- [ ] Corpus with zero documents -> keyword_search returns empty list
- [ ] Document with empty content -> on_save is a no-op (no terms to index)
- [ ] Query term not in corpus -> contributes 0 to BM25 score (idf is well-defined for df=0)
- [ ] Single-term query -> works correctly (degenerate case)
- [ ] Very long document -> tokenization handles gracefully, dl is large, BM25 normalizes

### Error State Rendering
- [ ] Not applicable — this is an ORM field and query method, not user-facing UI

## Test Impact

No existing tests affected — this is a greenfield feature adding new field type (`BM25Field`), new query method (`fuse()`), and new test files. Existing `test_composite_score_query.py` and `test_existence_filter.py` are not modified.

The shared tokenizer refactor (extracting `_tokenize()` from ExistenceFilter) must preserve identical behavior. `test_existence_filter.py` serves as the regression suite for this — run it after the refactor to confirm no behavior change.

## Rabbit Holes

- **Full-text search engine in Redis**: BM25Field is not a general-purpose search engine. It targets agent memory corpora (typically < 100K documents). Do not optimize for millions of documents — that is external search engine territory.
- **TF normalization variants**: Stick with standard BM25 (Okapi). Do not implement BM25+, BM25L, or other variants. The standard formula is well-validated and the k1/b constants are tunable enough.
- **Async BM25 search**: Defer async variant to a follow-up, consistent with how other query methods handle async (via `to_thread` wrapper).
- **Automatic hybrid mode on composite_score()**: Do not automatically detect BM25Field and switch to RRF. Keep the APIs explicit — the caller chooses `composite_score()` for weighted-sum or `fuse()` for RRF.
- **BM25 score caching**: Do not cache BM25 scores in sorted sets. The scores depend on corpus statistics (N, avgdl, df) which change with every save/delete. Always compute fresh.
- **EverMemBench integration in this PR**: The benchmark evaluation is valuable but is a separate concern. This plan ships the primitives. A follow-up can wire up EverMemBench as a test harness.

## Risks

### Risk 1: Lua Script Complexity for BM25 Search
**Impact:** The search Lua script must iterate candidate documents across multiple query terms, compute BM25 per document, and sort. For large corpora this could block Redis.
**Mitigation:** The inverted index design bounds work to O(sum of df for query terms), not O(N). For typical agent memory workloads (< 100K docs, < 10 query terms), this is well within Redis Lua time limits. Document the recommended corpus size ceiling.

### Risk 2: Inverted Index Storage Overhead
**Impact:** One sorted set per unique term in the corpus. For a corpus with 50K unique terms, that is 50K sorted sets.
**Mitigation:** Each sorted set is small (contains only doc_keys that have that term). Redis handles millions of small sorted sets efficiently. The total memory overhead is proportional to the total number of (term, document) pairs, which is the same as storing the forward index — just organized differently.

### Risk 3: avgdl Drift on Concurrent Updates
**Impact:** The incremental avgdl formula can accumulate floating-point drift over many save/delete cycles.
**Mitigation:** Periodically recompute avgdl from scratch (ZCARD + sum of dl). Add a `recompute_stats()` class method for maintenance. In practice, small drift in avgdl has negligible effect on BM25 ranking quality.

## Race Conditions

### Race 1: Concurrent saves updating df/n/avgdl
**Location:** BM25_SAVE_LUA script
**Trigger:** Two processes saving documents simultaneously
**Mitigation:** The entire save operation is a single Lua script execution, which is atomic in Redis. No race condition possible within a single Lua call. Concurrent Lua calls are serialized by Redis.

### Race 2: Query during save
**Location:** BM25_SEARCH_LUA reading while BM25_SAVE_LUA writes
**Trigger:** A search running while a document is being saved
**Mitigation:** Acceptable — Lua scripts are atomic and serialized. The search will see either the pre-save or post-save state, never a partial update. This is eventually consistent, same as all other Popoto query operations.

## No-Gos (Out of Scope)

- Changes to existing ExistenceFilter behavior (refactor only extracts shared tokenizer)
- Changes to existing composite_score() method
- LLM-driven memory segmentation
- External search engine integration (Elasticsearch, Meilisearch, etc.)
- Changes to existing field types
- Async variants (follow-up work)
- EverMemBench benchmark integration (follow-up work)
- Score normalization or calibration across BM25 and cosine similarity scales (RRF handles this by using ranks, not scores)

## Update System

No update system changes required — this is a library feature in the Popoto ORM package. Users get the new field type and query method by upgrading the package version.

## Agent Integration

No agent integration required — this is an ORM field type and query method consumed by application code (e.g., the SubconsciousMemory recipe in the AI system). The AI system's memory search tool (`tools/memory_search.py`) will be updated separately to use hybrid retrieval once this ships.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/hybrid-retrieval.md` covering BM25Field, RRF fusion, and the hybrid retrieval recipe
- [ ] Update `docs/features/agent-memory.md` to reference hybrid retrieval as the recommended search pattern
- [ ] Add BM25Field to `docs/fields.md` field type listing

### External Documentation Site
- [ ] Add hybrid retrieval page to mkdocs nav
- [ ] Verify docs build passes with `mkdocs build`

### Inline Documentation
- [ ] Docstrings on BM25Field class and all public methods
- [ ] Docstrings on `fuse()` method with usage examples
- [ ] Docstrings on `keyword_search()` query method

## Success Criteria

- [ ] `BM25Field` class in `src/popoto/fields/bm25_field.py`
- [ ] BM25 parameters k1=1.2, b=0.75 as class constants
- [ ] Redis storage uses only sorted sets and strings (no modules)
- [ ] `on_save()` atomically updates tf/df/dl/n/avgdl via Lua
- [ ] `on_delete()` atomically reverses via Lua
- [ ] `keyword_search()` computes BM25 scores via Lua, returns ranked results
- [ ] Shared tokenizer extracted from ExistenceFilter, used by both
- [ ] `fuse()` method on QueryBuilder implementing RRF with k=60 default
- [ ] RRF correctly combines heterogeneous ranked lists by rank position
- [ ] Hybrid retrieval recipe documented with BM25 + vector + graph example
- [ ] Test: BM25 ranks exact keyword matches above vague matches
- [ ] Test: BM25 handles document updates (re-save with new content)
- [ ] Test: BM25 handles document deletion (stats updated correctly)
- [ ] Test: RRF produces better top-1 than any single signal alone
- [ ] Test: RRF with single list degenerates to that list's ranking
- [ ] Test: Empty corpus / empty query edge cases
- [ ] Test: ExistenceFilter behavior unchanged after tokenizer refactor
- [ ] Tests pass (`pytest tests/ -x -q`)
- [ ] Format clean (`black --check src/ tests/`)
- [ ] Documentation created and builds

## Team Orchestration

### Team Members

- **Builder (BM25Field)**
  - Name: bm25-builder
  - Role: Implement BM25Field, tokenizer extraction, Lua scripts, on_save/on_delete/keyword_search
  - Agent Type: builder
  - Resume: true

- **Builder (RRF fusion)**
  - Name: rrf-builder
  - Role: Implement fuse() method on QueryBuilder, RRF scoring logic
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Implement BM25, RRF, and hybrid retrieval tests
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create hybrid-retrieval.md, update agent-memory.md
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

**Tier 1 -- Core (default choices):**
- `builder` - General implementation
- `validator` - Read-only verification
- `test-engineer` - Test implementation

## Step by Step Tasks

### 1. Extract shared tokenizer from ExistenceFilter
- **Task ID**: extract-tokenizer
- **Depends On**: none
- **Assigned To**: bm25-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/fields/_tokenizer.py` with the `tokenize()` function
- Move `_tokenize()` logic from `existence_filter.py` to the shared module
- Update ExistenceFilter imports to use the shared tokenizer
- Run `test_existence_filter.py` to confirm no behavior change

### 2. Implement BM25Field with Lua scripts
- **Task ID**: build-bm25-field
- **Depends On**: extract-tokenizer
- **Assigned To**: bm25-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/fields/bm25_field.py`
- Implement BM25Field class extending Field
- Add `source` parameter (field name to index, same pattern as EmbeddingField)
- Define BM25_K1=1.2 and BM25_B=0.75 as class constants
- Implement BM25_SAVE_LUA script for atomic tf/df/dl/n/avgdl updates with inverted index
- Implement BM25_DELETE_LUA script for atomic reversal
- Implement BM25_SEARCH_LUA script for query-time BM25 scoring
- Implement `on_save()` hook calling the save Lua script
- Implement `on_delete()` hook calling the delete Lua script
- Implement `search()` class method returning `[(redis_key, bm25_score), ...]`
- Add `recompute_stats()` class method for avgdl recalculation
- Export from `fields/__init__.py`

### 3. Add keyword_search() to QueryBuilder
- **Task ID**: build-keyword-search-query
- **Depends On**: build-bm25-field
- **Assigned To**: bm25-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `keyword_search()` method to `QueryBuilder` class in `models/query.py`
- Add convenience `keyword_search()` to `Query` class
- Method delegates to `BM25Field.search()` with partition filter support
- Returns list of model instances (with `_bm25_score` attribute)

### 4. Implement RRF fusion method
- **Task ID**: build-rrf-fusion
- **Depends On**: none
- **Assigned To**: rrf-builder
- **Agent Type**: builder
- **Parallel**: true (independent of BM25 implementation)
- Add `fuse()` method to `QueryBuilder` class
- Add convenience `fuse()` to `Query` class
- Implement RRF scoring: `score(d) = sum(1 / (k + rank_i(d)))`
- Accept **kwargs of named ranked lists, each a list of (redis_key, score) tuples
- k parameter with default 60
- limit parameter with default 10
- post_filter callback support
- Hydrate model instances from winning redis_keys

### 5. Implement BM25Field tests
- **Task ID**: build-bm25-tests
- **Depends On**: build-keyword-search-query
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_bm25_field.py`
- Test: save document, keyword_search returns it ranked
- Test: multiple documents with varying relevance, correct ranking order
- Test: exact term match ranks above partial relevance
- Test: document update (re-save) updates BM25 stats correctly
- Test: document delete removes from index, stats updated
- Test: empty corpus returns empty results
- Test: query with all stop words returns empty results
- Test: single-term query works
- Test: multi-term query combines scores
- Test: BM25 parameters (k1, b) affect scoring as expected
- Test: tokenizer shared with ExistenceFilter (same tokenization output)

### 6. Implement RRF fusion tests
- **Task ID**: build-rrf-tests
- **Depends On**: build-rrf-fusion
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_rrf_fusion.py`
- Test: two lists with overlapping documents, RRF produces correct merged ranking
- Test: document in all lists outranks document in one list
- Test: single list degenerates to that list's ranking
- Test: empty lists return empty results
- Test: k parameter affects score distribution
- Test: post_filter callback works
- Test: no ranked lists raises QueryException

### 7. Implement hybrid retrieval end-to-end test
- **Task ID**: build-hybrid-test
- **Depends On**: build-bm25-tests, build-rrf-tests
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/test_hybrid_retrieval.py`
- Define a Memory model with BM25Field + EmbeddingField + CoOccurrenceField + DecayingSortedField
- Test: hybrid retrieval (BM25 + vector + RRF) finds documents that single signals miss
- Test: exact keyword query surfaces correct document even when embedding similarity is low
- Test: semantically similar query surfaces correct document even when keywords don't match

### 8. Documentation
- **Task ID**: document-feature
- **Depends On**: build-hybrid-test
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/hybrid-retrieval.md`
- Update `docs/features/agent-memory.md` with hybrid retrieval reference
- Add BM25Field to field type listings
- Add docstrings to all public methods
- Verify mkdocs build

### 9. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: bm25-builder
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Run format check: `black --check src/ tests/`
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| BM25 tests pass | `pytest tests/test_bm25_field.py -v` | exit code 0 |
| RRF tests pass | `pytest tests/test_rrf_fusion.py -v` | exit code 0 |
| Hybrid tests pass | `pytest tests/test_hybrid_retrieval.py -v` | exit code 0 |
| ExistenceFilter unchanged | `pytest tests/test_existence_filter.py -v` | exit code 0 |
| Import BM25Field | `python -c "from popoto.fields.bm25_field import BM25Field"` | exit code 0 |
| Import fuse | `python -c "from popoto.models.query import QueryBuilder; assert hasattr(QueryBuilder, 'fuse')"` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Docs build | `mkdocs build` | exit code 0 |

---

## Open Questions

1. **Inverted index vs. forward-only storage**: The plan adds per-term inverted index sorted sets (`inv:{term}`) for efficient query-time lookup. This trades storage for query speed. For very large vocabularies (> 100K unique terms), this could be significant. Is the trade-off acceptable, or should we start with forward-only and add inverted indexes only if query performance is insufficient?

2. **Tokenizer extraction scope**: Should the shared tokenizer also be used by ContentField or other text-processing fields, or keep it scoped to ExistenceFilter + BM25Field only?

3. **SubconsciousMemory recipe update**: Should this PR also update SubconsciousMemory to auto-detect BM25Field and use hybrid retrieval, or defer that to a follow-up? The plan currently defers it, but it would be a natural integration point.
