---
status: Ready
type: feature
appetite: Large
owner: Valor
created: 2026-03-23
tracking: https://github.com/tomcounsell/popoto/issues/259
last_comment_id:
---

# ContentField and EmbeddingField: Large Content Storage and Semantic Search

## Problem

Popoto's agent memory primitives answer "what's recent and confident?" but not "what's *about* revenue trends?" All field values live in Redis via msgpack — fine for scores and short strings, but wrong for documents (KB-MB range) and vector embeddings (1024+ floats).

**Current behavior:**
Storing large content in Redis wastes expensive in-memory storage on rarely-accessed bulk data. No semantic retrieval exists — queries require exact field matches, not meaning-based search.

**Desired outcome:**
A developer declares `ContentField` and `EmbeddingField` on a model and the library automatically routes large content to filesystem storage, manages embeddings via a pluggable provider, and combines semantic similarity with existing memory signals in a single ranked query.

```python
class AgentMemory(popoto.Model):
    topic = KeyField()
    content = ContentField(store="filesystem")
    embedding = EmbeddingField(source="content")
    confidence = ConfidenceField()
    relevance = DecayingSortedField()

# Save — content goes to filesystem, embedding generated automatically
AgentMemory(topic="revenue", content="<large text>", confidence=0.9).save()

# Query — semantic + structured signals combined
results = AgentMemory.query.semantic_search("revenue trends", limit=10)
```

## Prior Art

No prior issues or PRs related to vector/embedding/content storage found. This is greenfield work.

Key foundational PRs this builds on:
- **PR #222**: CompositeScoreQuery — the `co_occurrence_boost` dict injection pattern that similarity scores will use
- **PR #199**: DecayingSortedField — established Lua-at-query-time scoring
- **PR #225**: ExistenceFilter — demonstrated optional-dependency field patterns

## Spike Results

### spike-1: NumPy cosine similarity performance at 100K scale
- **Assumption**: "Brute-force cosine similarity in Python is fast enough for agent memory scale"
- **Method**: prototype (benchmark on Apple Silicon)
- **Finding**: 100K x 1024 float32 vectors: **6.23ms** median for dot product + top-10 retrieval. Memory: **391 MB**. Scaling is linear (1K=0.02ms, 10K=0.65ms, 50K=3.43ms). Pre-normalization is a one-time 121ms cost.
- **Confidence**: high
- **Impact on plan**: Confirms query-time similarity in Python is viable. No Lua, no Redis modules, no ANN libraries needed.

### spike-2: Codebase patterns for field hooks and optional imports
- **Assumption**: "ContentField can access the full model instance in on_save() to read other field values"
- **Method**: code-read
- **Finding**: `on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs)` — model instance is the first positional arg. DataFrameField uses module-level `try/except ImportError` with a `_pandas_available` flag and raises at instantiation time. A string file path serializes fine via msgpack — ContentField can store a reference in Redis.
- **Confidence**: high
- **Impact on plan**: ContentField and EmbeddingField can follow the exact same patterns. No encoding changes needed.

### spike-3: Embedding provider API interfaces
- **Assumption**: "Voyage AI and OpenAI have compatible enough APIs for a single abstract interface"
- **Method**: web-research
- **Finding**: Both APIs share identical response shapes (`List[List[float]]`). Key differences: Voyage AI has `input_type` (document/query) distinction for retrieval quality; OpenAI has configurable `dimensions`. Batch limits: 128 (Voyage) / 2048 (OpenAI). Latency: 50-150ms single text, 200-500ms batch of 100. Standard dimensions: 1024 (Voyage-3), 1536 (OpenAI text-embedding-3-small).
- **Confidence**: high
- **Impact on plan**: Abstract interface needs `input_type` parameter (Voyage uses it, OpenAI ignores it). `dimensions` property is per-provider. Authentication in each provider's `__init__`, not the abstract interface.

## Data Flow

### Save path
1. **Entry point**: `model.save()` calls field hooks in order
2. **ContentField.on_save()**: Validates field value is content (not already a reference) → computes SHA-256 hash of raw content bytes → if live file exists, archives current to `.versions/{old_hash}.ext` → writes new content to `{base_path}/{ClassName}/{key_value}.ext` (atomic: temp file + `os.rename()`) → replaces field value with `$CF:{hash}:{relative_path}` reference string on the model instance (msgpack-serialized into Redis)
3. **EmbeddingField.on_save()**: Reads content from model instance via `source` field name (supports both ContentField values and file paths via `source_type="file"`) → calls `provider.embed([content], input_type="document")` → stores embedding as numpy `.npy` file at `~/.popoto/content/.embeddings/{ClassName}/{hash}.npy` → writes embedding dimension count to Redis field (for validation)
4. **Other fields**: `ConfidenceField`, `DecayingSortedField` etc. proceed as normal

### Query path (semantic_search)
1. **Entry point**: `Model.query.filter(...).semantic_search("query text", limit=10)`
2. **Embed query**: Calls `provider.embed(["query text"], input_type="query")` → single vector (~100ms)
3. **Load candidate embeddings**: Reads all `.emb.npy` files for the model class into a numpy matrix (cached in-memory after first load, ~391MB for 100K vectors)
4. **Compute similarity**: Pre-normalized dot product → all similarity scores in ~6ms → top-K via `np.argpartition`
5. **Inject into composite_score()**: Creates `{redis_key: similarity_score}` dict → passes as `similarity_boost` (same mechanism as `co_occurrence_boost`) alongside any other indexes
6. **Hydrate + return**: Standard `composite_score()` pipeline: ZUNIONSTORE → ZREVRANGE → hydration → lazy-load content from filesystem on access

### Content access path (lazy-load)
1. **Entry point**: `instance.content` attribute access on a queried model
2. **Descriptor intercept**: ContentField descriptor detects the stored value is a `$CF:{hash}:{relative_path}` reference string
3. **Filesystem read**: Loads content from `{base_path}/{relative_path}` (the human-readable live file, e.g., `~/vault/AgentMemory/revenue.md`)
4. **Cache on instance**: Stores loaded content on the instance dict so subsequent accesses don't re-read

## Architectural Impact

- **New dependencies**: `numpy` (optional extra `popoto[embeddings]`), `voyageai` (optional `popoto[voyage]`), `openai` (optional `popoto[openai]`). Core popoto gains zero new dependencies.
- **Interface changes**: New `ContentField`, `EmbeddingField` field types. New `semantic_search()` method on QueryBuilder. New `AbstractContentStore` and `AbstractEmbeddingProvider` ABCs. New `popoto.configure()` global configuration function.
- **Coupling**: Low. ContentField is independent — works without EmbeddingField. EmbeddingField depends on a content source field but not specifically ContentField (any string field works). `semantic_search()` uses the existing `co_occurrence_boost` injection pattern on `composite_score()`.
- **Data ownership**: ContentField owns filesystem content. EmbeddingField owns embedding files. Redis holds only references and precomputed sorted set scores. Each field manages its own lifecycle via `on_save()`/`on_delete()`.
- **Reversibility**: Moderate. New field types are additive. Filesystem data can be cleaned up by deleting the content directory. No changes to existing field behavior.

## Appetite

**Size:** Large

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (scope alignment on provider interface, content store path)
- Review rounds: 2+ (abstract interfaces, query integration, documentation)

This is a Large appetite feature: two new field types, two abstract interfaces, two optional provider implementations, a new query method, filesystem storage, numpy integration, and documentation. However, it follows well-established patterns from 14 shipped primitives.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| numpy available | `python -c "import numpy; print(numpy.__version__)"` | Embedding computation |
| At least one provider key (for integration tests) | `python -c "import os; assert os.environ.get('VOYAGE_API_KEY') or os.environ.get('OPENAI_API_KEY')"` | Embedding generation |

Note: numpy and provider SDKs are optional extras. Unit tests will mock the provider; integration tests require a real key.

## Solution

### Key Elements

- **ContentField**: Routes large values to filesystem via content-addressable storage. Redis stores only a `$CF:{sha256}` reference. Lazy-loads on attribute access.
- **EmbeddingField**: Declarative embedding lifecycle. Generates embeddings on save via a pluggable provider, stores as `.npy` files. Supports two source modes: `source_type="field"` (default, reads from a ContentField or StringField) and `source_type="file"` (reads content from a file path stored in the source field — for indexing existing documents like an Obsidian vault).
- **AbstractContentStore / AbstractEmbeddingProvider**: Pluggable interfaces for extensibility. Ship with `FilesystemStore`, `VoyageProvider`, and `OpenAIProvider`.
- **`semantic_search()` query method**: Embeds query text, computes numpy cosine similarity against all stored embeddings, injects scores into `composite_score()` via the existing boost dict pattern.
- **`popoto.configure()`**: Global configuration for default provider and store.

### Flow

**Save path:** `model.save()` → ContentField writes content to filesystem → EmbeddingField calls provider API → embedding written to filesystem → reference strings stored in Redis

**Query path:** `semantic_search("query")` → embed query via provider → numpy dot product against cached embeddings → similarity scores injected into `composite_score()` → ranked results with lazy-loaded content

### Technical Approach

- **Query-time similarity, not save-time precomputation.** Similarity is computed fresh at query time via numpy (~6ms for 100K vectors). This avoids O(n) API calls per save and prevents stale scores. The `co_occurrence_boost` injection pattern on `composite_score()` already supports this — similarity scores are injected as a temp ZADD, same as CoOccurrenceField propagation.
- **Two-path filesystem storage.** ContentField uses two directories:
  - **Live path** (configurable): human-readable files named from key field values. Browseable in tools like Obsidian. Default `~/.popoto/content/`. Override via `popoto.configure(content_path="...")` or `POPOTO_CONTENT_PATH` env var.
  - **Archive path** (internal): `~/.popoto/content/.versions/` stores previous versions by content hash. Invisible to the developer — versioning is automatic and internal.
  - On save: if file already exists at the live path, its current content is moved to `.versions/{sha256_hash}.ext`. New content is written to the live path. Redis stores the live path and current content hash.
  - File naming: `{base_path}/{ClassName}/{key_value}.ext` (e.g., `~/vault/AgentMemory/revenue.md`). Extension configurable via `ContentField(extension=".md")`, default `.txt`.
- **Write ordering: filesystem first, then Redis.** ContentField.on_save() writes content to filesystem before the Redis pipeline executes. If filesystem write fails, on_save() raises and the entire model save aborts (no partial state). If Redis write fails after filesystem write, the orphaned file is harmless and can be garbage-collected later. This ordering ensures Redis never references a file that doesn't exist.
- **No deduplication-aware deletion.** ContentField.on_delete() removes the live file but leaves archived versions. Provide a `ContentField.garbage_collect(ModelClass)` classmethod that removes orphaned archive files. Document that `.versions/` is append-only by default.
- **Embedding cache with bounds.** On first `semantic_search()`, all embeddings for the model class are loaded into a pre-normalized numpy matrix and cached in a class-level dict. Cache is invalidated on save/delete within the same process. Cache has a configurable `max_cache_size` (default 100K vectors) and optional TTL (`cache_ttl` in seconds, default None = no expiry). When cache exceeds max size, oldest entries are evicted. When cache is disabled (`EmbeddingField(cache=False)`), embeddings are loaded from disk per query. Multi-process cache staleness is accepted as a known limitation — document that `semantic_search()` results may lag saves from other processes.
- **`semantic_search()` not `.search()`.** Avoids collision with potential future RediSearch/FT.SEARCH integration. More precise naming.
- **Optional numpy dependency.** Follow the DataFrameField pattern: module-level `try/except ImportError`, raise at field instantiation with helpful install message (`pip install popoto[embeddings]`).
- **`similarity_boost` parameter on `composite_score()`.** Add a new parameter alongside `co_occurrence_boost` for semantic similarity scores. Identical injection mechanism — temp ZADD with `{redis_key: score}` dict.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] ContentField: filesystem write failure (permissions, disk full) — must raise, not silently lose content
- [ ] EmbeddingField: provider API failure (network, auth, rate limit) — must raise on save, not store model without embedding
- [ ] EmbeddingField: provider returns wrong dimensions — must validate and raise
- [ ] `semantic_search()`: no embeddings cached yet — return empty list, not crash
- [ ] ContentField: referenced file missing on read (deleted externally) — raise clear error with the hash

### Empty/Invalid Input Handling
- [ ] ContentField with empty string — store in Redis directly (below size threshold), don't write empty file
- [ ] ContentField with None — treat as null field, no filesystem write
- [ ] EmbeddingField with source field empty/None — skip embedding generation, don't call provider
- [ ] `semantic_search("")` with empty query — return empty list

### Error State Rendering
- [ ] Not applicable — no user-visible UI. Errors propagate as Python exceptions.

## Test Impact

No existing tests affected — this is a greenfield feature with no prior test coverage. All new test files:
- `tests/test_content_field.py` — ContentField save/load/delete lifecycle
- `tests/test_embedding_field.py` — EmbeddingField generation and storage
- `tests/test_semantic_search.py` — query integration with composite_score
- `tests/test_content_store.py` — AbstractContentStore and FilesystemStore
- `tests/test_embedding_provider.py` — AbstractEmbeddingProvider and mock provider

## Rabbit Holes

- **ANN indexes (HNSW, IVF).** Requires Redis modules, violating Valkey compatibility. Brute-force numpy is 6ms at 100K — fast enough. Defer to a future issue if scale exceeds 500K.
- **Reranking as a built-in feature.** Neural reranking (Cohere, Voyage reranker) is a second-stage pass that belongs in the application layer, not the ORM.
- **Chunking strategy for large documents.** The issue mentions configurable chunk size/overlap. This is complex (how to aggregate chunk-level similarity to document-level?) and should be a follow-up. V1 embeds the full content string.
- **S3 / cloud storage backends.** The `AbstractContentStore` interface enables this, but implementing S3Store is out of scope. V1 ships FilesystemStore only.
- **Async embedding providers.** Both Voyage and OpenAI have async SDKs, but Popoto's save path is synchronous. Adding async would touch the core save pipeline. Defer.
- **Embedding migration / re-embedding.** When a provider or model changes, existing embeddings become incompatible. This needs a migration tool, but it's a separate concern from the core field implementation.

## Risks

### Risk 1: Memory footprint of embedding cache
**Impact:** 391 MB for 100K x 1024 vectors. Server processes with limited memory may struggle. Multi-worker deployments multiply this by N workers.
**Mitigation:** Cache is bounded by `max_cache_size` (default 100K vectors) with optional TTL. Disable entirely via `EmbeddingField(cache=False)` — loads from disk per query (slower but memory-safe). Cache is built lazily on first `semantic_search()`, not on class load. Incremental updates: on save, append a single row to the cached matrix rather than rebuilding. Document memory tradeoffs prominently.

### Risk 2: Filesystem store reliability
**Impact:** Content referenced in Redis but missing on filesystem = data loss. External processes could delete files. No ACID guarantees.
**Mitigation:** Content-addressable storage makes files immutable (same hash = same content). Add a `verify_content()` classmethod that checks all references resolve. Document that the content directory should be backed up alongside Redis.

### Risk 3: Embedding provider API costs and latency
**Impact:** Every save with an EmbeddingField triggers an API call (~100ms, ~$0.0001/call). Bulk imports become expensive and slow.
**Mitigation:** Add `EmbeddingField(auto_embed=True)` flag. When False, embeddings are only generated on explicit `.embed()` call. Provide a bulk `Model.embed_all()` classmethod that batches texts to the provider (128 per call for Voyage, 2048 for OpenAI).

### Risk 4: Provider SDK version conflicts
**Impact:** Users may have different versions of `voyageai` or `openai` SDKs installed.
**Mitigation:** Pin minimum versions in optional extras. The abstract interface uses raw `List[List[float]]` returns, not SDK-specific types. Provider implementations import the SDK lazily.

## Race Conditions

### Race 1: Concurrent saves writing the same content hash
**Location:** ContentField.on_save() filesystem write
**Trigger:** Two processes save a model with identical content simultaneously
**Data prerequisite:** Content-addressable file `{hash}.bin` being written
**State prerequisite:** File may be partially written
**Mitigation:** Write to a temp file first, then `os.rename()` (atomic on POSIX). Content-addressable storage means the final content is identical regardless of which write "wins".

### Race 2: Embedding cache stale after concurrent save
**Location:** Class-level embedding cache dict
**Trigger:** Process A queries (loads cache), Process B saves (new embedding), Process A queries again (stale cache)
**Data prerequisite:** Embedding cache populated in Process A
**State prerequisite:** New embedding exists on filesystem but not in cache
**Mitigation:** Accept eventual consistency for multi-process. Within a single process, `on_save()` invalidates the cache. For multi-process, add optional cache TTL. Document that `semantic_search()` results may not include records saved by other processes until cache refresh.

## No-Gos (Out of Scope)

- **Chunking / splitting large documents** — V1 embeds the full content string. Chunking is a follow-up.
- **S3 / cloud storage backends** — V1 ships FilesystemStore only. AbstractContentStore enables future backends.
- **Async providers** — Popoto's save path is synchronous. Async is a separate concern.
- **ANN indexes** — Brute-force numpy is sufficient for <=100K vectors.
- **Reranking** — Application layer concern, not ORM.
- **Embedding migration tools** — Separate follow-up for provider/model changes.
- **Configurable content size threshold** — V1 routes all ContentField values to filesystem. A size-based threshold (e.g., only >1KB) adds complexity for little benefit at agent memory scale.

## Update System

No update system changes required — this is a library feature, not a deployed service.

## Agent Integration

No agent integration required — this is a popoto library feature. Downstream consumers (ContextAssembler, application code) will use the new fields via the standard Popoto model API.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/content-and-embedding-fields.md` describing ContentField, EmbeddingField, and semantic_search()
- [ ] Update `docs/features/agent-memory.md` to add ContentField and EmbeddingField to the primitives table
- [ ] Update `docs/guides/agent-memory-quickstart.md` with a "Level 6: Semantic Search" section

### External Documentation Site
- [ ] Update MkDocs config (`mkdocs.yml`) to include new feature page
- [ ] Update API reference with new field types and query method
- [ ] Verify docs build passes

### Inline Documentation
- [ ] Docstrings for all public classes: ContentField, EmbeddingField, AbstractContentStore, AbstractEmbeddingProvider, FilesystemStore, VoyageProvider, OpenAIProvider
- [ ] Docstring for `semantic_search()` on QueryBuilder

## Success Criteria

- [ ] `ContentField(store="filesystem")` stores values on filesystem, keeps only `$CF:{hash}` reference in Redis
- [ ] Content is lazy-loaded from store when field is accessed on a queried model instance
- [ ] `EmbeddingField(source="content_field_name")` generates and stores embeddings on save via configured provider
- [ ] VoyageProvider and OpenAIProvider ship as optional extras (`popoto[voyage]`, `popoto[openai]`)
- [ ] `AbstractContentStore` and `AbstractEmbeddingProvider` interfaces allow third-party implementations
- [ ] `semantic_search("query text")` returns results ranked by combination of semantic similarity and existing memory signals
- [ ] Similarity scores integrate with `composite_score()` via `similarity_boost` parameter
- [ ] All features work on both Redis and Valkey (no module dependencies)
- [ ] Large content never stored in Redis — only references and computed scores
- [ ] Existing models without ContentField or EmbeddingField are unaffected (backward compatible)
- [ ] numpy is optional — ImportError with helpful message if not installed
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (stores)**
  - Name: stores-builder
  - Role: Implement AbstractContentStore, FilesystemStore, and ContentField
  - Agent Type: builder
  - Resume: true

- **Builder (embeddings)**
  - Name: embeddings-builder
  - Role: Implement AbstractEmbeddingProvider, VoyageProvider, OpenAIProvider, and EmbeddingField
  - Agent Type: builder
  - Resume: true

- **Builder (query)**
  - Name: query-builder
  - Role: Implement semantic_search() on QueryBuilder with similarity_boost injection
  - Agent Type: builder
  - Resume: true

- **Builder (integration)**
  - Name: integration-builder
  - Role: Wire up __init__.py exports, pyproject.toml extras, popoto.configure()
  - Agent Type: builder
  - Resume: true

- **Validator (all)**
  - Name: feature-validator
  - Role: Verify all success criteria, run tests, check backward compatibility
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Create feature docs, update agent-memory docs, update quickstart
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. AbstractContentStore and FilesystemStore
- **Task ID**: build-stores
- **Depends On**: none
- **Validates**: tests/test_content_store.py (create)
- **Informed By**: spike-2 (confirmed: on_save receives model instance, string paths serialize via msgpack)
- **Assigned To**: stores-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/stores/__init__.py` with `AbstractContentStore` ABC: `save(content: bytes, key: str) -> str`, `load(reference: str) -> bytes`, `delete(reference: str)`, `exists(reference: str) -> bool`
- Create `src/popoto/stores/filesystem.py` with `FilesystemStore`: SHA-256 CAS, two-level directory sharding, atomic writes via temp file + `os.rename()`
- Create `tests/test_content_store.py` with unit tests for FilesystemStore lifecycle

### 2. ContentField
- **Task ID**: build-content-field
- **Depends On**: build-stores
- **Validates**: tests/test_content_field.py (create)
- **Informed By**: spike-2 (confirmed: DataFrameField optional import pattern)
- **Assigned To**: stores-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/fields/content_field.py` with `ContentField(Field)`: `on_save()` writes content to store (filesystem first, then reference to Redis), validates that raw content is never accidentally persisted to Redis; descriptor `__get__` detects `$CF:{hash}` reference and lazy-loads from store; `on_delete()` is a no-op (append-only storage — orphaned files cleaned by `garbage_collect()` classmethod)
- Implement `ContentField.garbage_collect(ModelClass)` classmethod: scans all live Redis references, removes orphaned filesystem files
- Configure via `store` parameter (default "filesystem") or store instance
- Create `tests/test_content_field.py` with save/load/delete/lazy-load tests

### 3. AbstractEmbeddingProvider and implementations
- **Task ID**: build-providers
- **Depends On**: none
- **Validates**: tests/test_embedding_provider.py (create)
- **Informed By**: spike-3 (confirmed: both APIs share List[List[float]] response shape, Voyage needs input_type)
- **Assigned To**: embeddings-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/embeddings/__init__.py` with `AbstractEmbeddingProvider` ABC: `embed(texts: List[str], input_type: Optional[str]) -> List[List[float]]`, `dimensions` property, `max_batch_size` property
- Create `src/popoto/embeddings/voyage.py` with `VoyageProvider` (optional `voyageai` import)
- Create `src/popoto/embeddings/openai.py` with `OpenAIProvider` (optional `openai` import)
- Create `tests/test_embedding_provider.py` with mock provider tests and interface compliance tests

### 4. EmbeddingField
- **Task ID**: build-embedding-field
- **Depends On**: build-providers, build-content-field
- **Validates**: tests/test_embedding_field.py (create)
- **Informed By**: spike-1 (confirmed: numpy brute-force is 6ms at 100K), spike-2 (confirmed: on_save signature)
- **Assigned To**: embeddings-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/fields/embedding_field.py` with `EmbeddingField(Field)`: `source` parameter pointing to content field; `on_save()` calls provider, saves `.npy` file to filesystem; `on_delete()` removes embedding file; optional numpy import following DataFrameField pattern
- Implement embedding cache: class-level dict mapping `{ClassName}` to pre-normalized numpy matrix + redis_key index; invalidated on save/delete
- Implement `EmbeddingField.garbage_collect(ModelClass)` classmethod mirroring ContentField's — removes orphaned `.npy` files not referenced by any live model
- Create `tests/test_embedding_field.py` with mock provider, save/delete/cache tests

### 5. semantic_search() query method
- **Task ID**: build-query
- **Depends On**: build-embedding-field
- **Validates**: tests/test_semantic_search.py (create)
- **Informed By**: spike-1 (confirmed: dot product + argpartition in 6ms)
- **Assigned To**: query-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `semantic_search(query_text, indexes=None, limit=10, ...)` to `QueryBuilder` class
- Add `similarity_boost` parameter to `composite_score()` method signature (new parameter, identical injection mechanism to `co_occurrence_boost` at query.py:539-548 — temp ZADD + weight 1.0)
- `semantic_search` embeds query text → loads cached embeddings → computes cosine similarity → injects as `similarity_boost` dict → delegates to `composite_score()`
- Create `tests/test_semantic_search.py` with end-to-end tests using mock provider

### 6. Configuration and packaging
- **Task ID**: build-integration
- **Depends On**: build-query
- **Validates**: tests/test_content_field.py, tests/test_embedding_field.py, tests/test_semantic_search.py
- **Assigned To**: integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `popoto.configure(embedding_provider=..., content_store=...)` to `__init__.py`
- Add ContentField, EmbeddingField to `__init__.py` exports and `__all__`
- Add optional extras to `pyproject.toml`: `embeddings = ["numpy>=1.23.1"]`, `voyage = ["voyageai>=0.3.0", "numpy>=1.23.1"]`, `openai = ["openai>=1.0.0", "numpy>=1.23.1"]`
- Verify all existing tests still pass (backward compatibility)

### 7. Validation
- **Task ID**: validate-feature
- **Depends On**: build-integration
- **Assigned To**: feature-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify no existing tests broken
- Verify ContentField lazy-loading works on queried instances
- Verify semantic_search returns ranked results with mock provider
- Verify backward compatibility: existing models without new fields work unchanged

### 8. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-feature
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/content-and-embedding-fields.md`
- Update `docs/features/agent-memory.md` primitives table
- Update `docs/guides/agent-memory-quickstart.md` with Level 6
- Update MkDocs config
- Verify docs build: `mkdocs build`

### 9. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: feature-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met (including documentation)
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `ruff check src/popoto/` | exit code 0 |
| Format clean | `black --check src/popoto/` | exit code 0 |
| ContentField importable | `python -c "from popoto import ContentField"` | exit code 0 |
| EmbeddingField importable | `python -c "from popoto import EmbeddingField"` | exit code 0 |
| Docs build | `mkdocs build` | exit code 0 |
| No new core deps | `python -c "import popoto"` | exit code 0 (without numpy) |
| Backward compat | `pytest tests/test_queries.py tests/test_model_save.py -x -q` | exit code 0 |

## RFC Feedback

Reviewed by: code-reviewer, data-architect. BLOCKERs from data-architect were incorporated into the plan (write ordering, no dedup-aware deletion, cache bounds). Code-reviewer's 3 BLOCKERs were invalid (based on stale code reads — actual field system uses instances with `__init__`, `on_save()` receives model instance, and the plan uses lazy-loading descriptors not `from_redis`).

| Severity | Critic | Feedback | Plan Response |
|----------|--------|----------|---------------|
| CONCERN | data-architect | No fsync/durability guarantee | Accept — fsync is OS-level concern. Document that content files rely on OS flush behavior, same as any application writing files. |
| CONCERN | data-architect | Filesystem I/O errors in on_save need defined error handling | Addressed — on_save raises on filesystem failure, aborting the entire model save. |
| CONCERN | data-architect | Need validation to prevent raw content from being stored in Redis | Addressed — on_save replaces field value with `$CF:{hash}` reference before Redis write. Add `__set__` validation that rejects values starting with `$CF:` prefix. |
| CONCERN | data-architect | Testing strategy for filesystem ops | Use `tmp_path` pytest fixture for all filesystem tests. Add to task descriptions. |
| CONCERN | code-reviewer | Filesystem storage makes library non-portable across machines | Acknowledged in No-Gos. ContentField is opt-in — users who need multi-server sharing should use standard StringField or wait for S3 backend. |
| CONCERN | code-reviewer | `to_dict()` would return full content (potentially MB) | Add `to_dict()` override on ContentField that returns the `$CF:{hash}` reference, not loaded content. Add `to_dict(include_content=True)` option. |
| QUESTION | data-architect | Backup/restore documentation needed | Add to Documentation section — document that both Redis and content directory must be backed up. |
| QUESTION | code-reviewer | What happens if auto_embed raises during on_save? | Already addressed — on_save raises on provider failure, aborting model save. Document that callers should handle EmbeddingProviderError. |

---

## Resolved Questions

1. **Content store base path**: Default `~/.popoto/content/`, configurable via `popoto.configure(content_path="...")` or `POPOTO_CONTENT_PATH` env var. Live files use human-readable names (key-derived), archived versions use content hashes in `.versions/`.

2. **Method name**: `semantic_search()` — precise, avoids collision with potential RediSearch.

3. **ContentField scope**: ContentField is for files — user-uploaded docs, agent-generated docs, PDFs, images, JSON, etc. The distinction is whether a file (`.md`, `.pdf`, `.png`, `.json`, etc.) is involved. Short structured data (scores, timestamps, short strings) stays in Redis via normal fields. ContentField always writes to filesystem.

4. **EmbeddingField auto_embed**: On by default (`auto_embed=True`), configurable to False for bulk import. Developer provides an API key and selects a provider during setup. Local LLM embeddings are a future feature (out of scope for v1).

5. **Multi-process cache**: Eventual consistency is acceptable. No pub/sub invalidation needed.
