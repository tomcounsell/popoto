# Changelog

All notable changes to Popoto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`ContextAssembler(retrieval_mode=...)`** — new `retrieval_mode` parameter controls pull-path strategy ([#395](https://github.com/tomcounsell/popoto/issues/395)):
  - `"auto"` *(default)* — detects `BM25Field` + `EmbeddingField` on the model; uses hybrid RRF path if both present, composite otherwise
  - `"hybrid"` — BM25 lexical + vector semantic signals fused via Reciprocal Rank Fusion (k=60), optional CoOccurrence graph expansion; raises `QueryException` at init if required fields are absent
  - `"composite"` — original `CompositeScoreQuery` weighted-sum path (pre-v1.7 behaviour)
  - Existing callers without `retrieval_mode` keep working unchanged: auto-mode falls back to composite on models without `BM25Field`/`EmbeddingField`
- **`QueryBuilder._get_vector_scores(query_text, limit)`** — private helper that returns `[(redis_key, cosine_similarity)]` tuples for RRF fusion input; mirrors `semantic_search()` internals without hydration
- **Benchmark R@K improvement** — external harness ([#394](https://github.com/tomcounsell/popoto/issues/394)) now shows measurable signal with BM25 retrieval:
  - LongMemEval-S (fixture): R@5 0.0 → 1.0, MRR 0.0 → 0.667
  - LoCoMo (fixture): R@5 0.0 → 0.667, MRR 0.0 → 0.375
- **Full-dataset LoCoMo baseline** ([#447](https://github.com/tomcounsell/popoto/issues/447)) — the 6-question fixture baseline is replaced by complete 1986-question runs (10 dialogues) in both retrieval modes:
  - Lexical (BM25): R@1 0.2986, R@5 0.5534, R@10 0.6400, MRR 0.4124
  - Hybrid (BM25 + vector RRF): R@1 0.1667, R@5 0.4235, R@10 0.5403, MRR 0.2835 — hybrid underperforms lexical on LoCoMo
  - Category-5 (adversarial) questions score normally in this dataset snapshot (`evidence` is populated), correcting the earlier zero-by-construction assumption
  - Artifacts: `tests/benchmarks/results/external/locomo_latest{,_hybrid}.{json,md}`; analysis in [docs/benchmarks.md](https://popoto.io/benchmarks/)
- **`MemoryLifecycle`** recipe (`src/popoto/recipes/memory_lifecycle.py`) — policy layer orchestrating memory tier transitions and auto-forget. Composes `DecayingSortedField`, `ConfidenceField`, and `AccessTrackerMixin` into a two-tier episodic → semantic lifecycle without replacing any existing primitive. ([#396](https://github.com/tomcounsell/popoto/issues/396))
  - `MemoryLifecycle(model_class, importance_field)` — init with capability detection and `ModelException` guards
  - `tag_new(record, tier="episodic")` — assign starting tier; handles `KeyField` migration automatically
  - `tick()` → `{"promoted": N, "forgotten": N, "duration_ms": F}` — idempotent periodic lifecycle pass with paginated batch scanning
  - `assess(record)` → `LifecycleState` — snapshot of tier, access count, importance score, and promotion/forget eligibility
  - `LifecycleState` dataclass — return type for `assess()`
  - Custom `should_promote` and `should_forget` callables — injectable for application-specific policies
  - `partition_filters` — scope each lifecycle instance to a sub-partition (e.g. per-agent)
  - Five tuning constants (`PROMOTION_ACCESS_COUNT`, `PROMOTION_CONFIDENCE_THRESHOLD`, `PROMOTION_MIN_AGE_SECONDS`, `FORGET_IMPORTANCE_FLOOR`, `FORGET_IDLE_SECONDS`) registered in `Defaults` and the Tier 5 benchmark sweep grid
- **`LifecycleState`** exported from `popoto.recipes`
- **Tier 5 benchmark sweep grid** (`TIER5_SWEEPS` in `tests/benchmarks/run_sweeps.py`) — five lifecycle constants with sweep ranges for tuning against LoCoMo + LongMemEval-S
- **`docs/benchmarks/memory_lifecycle_baseline.md`** — pre-lifecycle retrieval baseline and sweep grid documentation

### Fixed

- **`BM25Field.search()` now returns deterministic ordering for equal scores** ([#446](https://github.com/tomcounsell/popoto/issues/446)): Lua 5.1 `table.sort` is unstable and candidates were collected in hash order, so equal-scored documents came back in undefined order — across runs and across the `limit` truncation boundary. Ties are now broken inside the scoring Lua script by member `redis_key` ascending (byte-wise), so identical searches return identical orderings on both Redis and Valkey. `keyword_search()`, RRF `fuse()`, and hybrid retrieval inherit the determinism.
- **`DecayingSortedField` / `CyclicDecayField` `top_by_decay()` now returns deterministic ordering for equal scores** ([#448](https://github.com/tomcounsell/popoto/issues/448)): the same unstable-`table.sort` defect as #446 in both decay-field Lua scripts. When members share a base score and timestamp (e.g. batch-inserted memories) their equal decayed/effective scores left the tied run — and which members survived the `n` truncation — in undefined order. Both comparators now apply a two-level total order (score descending, then member `redis_key` ascending byte-wise) before truncation, entirely inside the Lua script, so identical queries return identical orderings on both Redis and Valkey.
- **`ContextAssembler` token budget now enforced for real** ([#408](https://github.com/tomcounsell/popoto/issues/408)):
  - The default `token_counter` previously counted characters in the Redis key (`str(record)`, typically 12–14 "tokens" per record regardless of content size), so `max_tokens` never engaged for realistic budgets. The counter now receives the serialized per-record string the formatter emits, and `metadata["token_count"]` reflects actual serialized content.
  - **Breaking change for users who set `max_tokens`**: assemblies will now admit fewer records. Audit and raise your `max_tokens` values if needed. See [ContextAssembler — Token Budget Semantics](features/context-assembler.md#token-budget-semantics).
  - New `token_counter` contract: `callable(serialized_text: str) -> int`. Old-contract `callable(record)` counters trigger `DeprecationWarning` at construction and fall back to the stdlib heuristic per call.
  - New default heuristic (`_estimate_tokens`): escape-aware character-class estimator accurate to within ±25% vs tiktoken cl100k_base across English, code, CJK, and emoji content (worst case −15% underestimate on URL/hash-heavy records; all other content types err in the safe overestimate direction).
  - Packing now uses skip-not-break semantics: a record that does not fit is skipped; later smaller records may still be admitted. The first record is always admitted (never-zero-records guarantee).
  - Wrapper framing (JSON array brackets, `<records>` envelope) is excluded from counting — fixed residual under 20 tokens per assembly.
- **`EmbeddingField` cross-process cache invalidation** — in multi-worker deployments (gunicorn, multiple containers/pods) a write on one worker no longer leaves peers serving a stale embedding matrix ([#403](https://github.com/tomcounsell/popoto/issues/403)):
  - New `POPOTO_EMBEDDING_INVALIDATION` environment variable selects the strategy: `pubsub` *(default)* uses a Valkey pub/sub bus to notify peers within ~100 ms; `mtime` uses an on-disk `_version` counter checked on the next `semantic_search()`; `none` restores the original zero-overhead single-process behavior.
  - The default `pubsub` mode degrades to the on-disk `_version` check (never back to the stale-cache bug) if the subscriber thread cannot start, and the listener self-heals via lazy respawn after a connection drop.
  - Single-process **results** are unchanged in all modes; the default adds one daemon listener thread, one Valkey connection, and one loopback `PUBLISH` per write per model class. Set `POPOTO_EMBEDDING_INVALIDATION=none` for zero overhead. See [EmbeddingField → Multi-Worker Deployments](https://popoto.io/fields/#embeddingfield).

### Removed

- **`MemoryLifecycle.TICK_BATCH_SIZE`** — removed in #413 (single-pass refactor eliminated batch scanning; the constant was never registered in `Defaults` and was always an internal implementation detail). Public-API break acceptable under beta.

## [1.7.1] - 2026-06-15

### Fixed

- **`DatetimeField` `auto_now` / `auto_now_add` now stamp UTC** ([tomcounsell/ai#1653](https://github.com/tomcounsell/ai/issues/1653)) — `format_value_pre_save` previously returned naive `datetime.now()` (host **local** wall-clock). Since the encoder serializes wall-clock without tzinfo, every `auto_now`/`auto_now_add` timestamp on a non-UTC host was skewed by the host's UTC offset, breaking downstream "age since update" math. It now returns `datetime.now(timezone.utc)`, so timestamps are correct regardless of host timezone. Uses `timezone.utc` (valid on the `requires-python = ">=3.10"` floor), not the 3.11+ `datetime.UTC`. Write-only and non-migrating: existing rows are unchanged; UTC hosts already stamped UTC.

## [1.5.0](https://github.com/tomcounsell/popoto/compare/v1.0.3...v1.5.0) (2026-04-21)

### Popoto Agent Memory — Now in Beta

After shipping 14 independent primitives across the 1.1–1.4 series, the Popoto Agent Memory system exits alpha with this release. The system gives AI agents non-cortical memory capabilities — episodic recall, salience gating, temporal decay, confidence tracking, prediction-error learning, and now retrieval self-assessment — all built on plain Redis/Valkey with no module dependencies.

**Metacognitive layer** (the final piece — [#352](https://github.com/tomcounsell/popoto/issues/352)):

Agents using `ContextAssembler` can now ask "how much should I trust this context?" before reasoning over it, and automatically tune their retrieval strategy over time without any ML training pipeline.

#### Added

- **`RetrievalQuality`** dataclass — four-signal retrieval self-assessment: `fok_score` (feeling-of-knowing via `ExistenceFilter`), `avg_confidence` (mean Bayesian certainty of returned memories), `score_spread` (retrieval confidence interval), `staleness_ratio` (fraction of memories past their decay threshold)
- **`ContextAssembler.assess(query_cues)`** — standalone quality probe; returns `RetrievalQuality` without executing a full retrieval
- **`ContextAssembler.assemble(query_cues, assess_quality=True)`** — attaches `RetrievalQuality` to `AssemblyResult.metadata["quality"]`; off by default, zero overhead when not used
- **`PredictionLedgerMixin.error_summary(group_by=...)`** — aggregate prediction errors across instances, grouped by hour of day, day of week, error band, or any callable bucketer; uses pipelined Redis reads, Valkey-compatible
- **`ObservationProtocol` `"used"` outcome** — agent consumed the memory but hasn't acted yet; confirms the staged `AccessTracker` read and auto-resolves the pending prediction with a configurable error signal (default 0.3); distinct from `"deferred"` which discards the staged read
- **`AdaptiveAssembler`** recipe (`src/popoto/recipes/adaptive_assembler.py`) — wraps any `ContextAssembler` with an autoresearch-style keep/revert loop: proposes symmetric `score_weights` perturbations, measures quality over a rolling window, keeps improvements, reverts regressions; single-threaded by design, purely opt-in, no Redis writes for the adaptation state

#### Also in this cycle (1.1–1.4 series highlights)

- `OllamaProvider` — local embedding generation via Ollama, no API key required
- `ExistenceFilter.batch_might_exist()` + `BM25Field.get_idf()` IDF selectivity signal
- `Model.check_indexes()` — read-only index health check for production
- `Model.clean_indexes()` — production-safe orphan index removal
- `Query.get_many()` — bulk key hydration in a single pipeline
- Companion hash key public API
- `ConfidenceField` partition support
- Adaptive constant optimizer sweep (closes constant-sensitivity variance gap from benchmarks)

#### Fixed

- `PredictionLedgerMixin.error_summary(group_by=...)` returned `{"__all__": ...}` instead of `{}` when called on a model with zero recorded predictions and a non-`None` `group_by`

#### Migration

If you were using a custom `"echoed"` outcome (or any application-specific
label semantically between `"used"` and `"dismissed"`):

- Map it to `"used"` if the agent reasoned over the memory (staged read
  should be confirmed; prediction auto-resolves with moderate error).
- Map it to `"dismissed"` if the overlap was purely coincidental keyword
  match (staged read discarded; confidence/cycle weakened).

`on_context_used()` raises `ValueError` on unknown outcome labels — coerce
to a valid value before calling.

#### Notes

- All metacognitive features are **opt-in** and additive — existing `ContextAssembler` API is unchanged
- No Redis module commands anywhere in the stack — works on Redis ≥ 6 and Valkey ≥ 7
- Cross-restart persistence for `AdaptiveAssembler` is deferred to v1.6; adaptation state is per-process

---

## [1.0.3](https://github.com/tomcounsell/popoto/compare/v1.0.2...v1.0.3) (2026-03-22)


### Documentation

* add temperature parameter to composite_score references ([3600fbd](https://github.com/tomcounsell/popoto/commit/3600fbd856d2e4229661efc4106beebbb72d67e9))

## [1.0.2](https://github.com/tomcounsell/popoto/compare/v1.0.1...v1.0.2) (2026-03-20)


### Documentation

* AccessTrackerMixin — update agent-memory, api-reference, query docs ([5bc29e5](https://github.com/tomcounsell/popoto/commit/5bc29e5d27b1b6f9af7152791422ff6a6215f13b))
* add composite_score() to query and API reference ([#223](https://github.com/tomcounsell/popoto/issues/223)) ([53b6318](https://github.com/tomcounsell/popoto/commit/53b63185456b4084a8b9e649eb9c3ed2fbd57974))
* audit cleanup + allow docs pushes to main ([#227](https://github.com/tomcounsell/popoto/issues/227)) ([0ef0baf](https://github.com/tomcounsell/popoto/commit/0ef0baf1fa8289c7c82399c189c267093d8137c1))
* cascade updates for PredictionLedgerMixin (PR [#231](https://github.com/tomcounsell/popoto/issues/231)) ([#237](https://github.com/tomcounsell/popoto/issues/237)) ([a65255a](https://github.com/tomcounsell/popoto/commit/a65255a79ee4387e9af1831b0e8385033d57155a))

## [1.0.1](https://github.com/tomcounsell/popoto/compare/popoto-v1.0.0...popoto-v1.0.1) (2026-03-12)


### Bug Fixes

* update README community section ([8627346](https://github.com/tomcounsell/popoto/commit/8627346dbcf93b952cb33778405678fb773172c3))

## [1.0.0] - 2026-03-11

Popoto 1.0.0 is the first General Availability release. It marks the project's graduation from beta to a stable, production-ready Redis/Valkey ORM with Django-like model syntax. This release consolidates all features and fixes from the beta series (1.0.0b1, 1.0.0b2) plus additional hardening work.

### Highlights

- **Full async/await support** with native `redis.asyncio` — no more `asyncio.to_thread()` wrappers
- **Chainable query builder** with Q objects and expression-based filtering
- **Bulk operations** (create, update, delete) via Redis pipelines
- **Atomic saves** via internal pipeline for data integrity
- **Migration utilities** for production schema changes
- **Comprehensive index integrity** — all known ghost-entry and corruption bugs resolved
- **Valkey compatibility** — works identically with Redis and Valkey

### Added

#### Query System
- **Chainable Query Builder** (#91): Fluent interface for building queries incrementally
  ```python
  results = Model.query.filter(status="active").order_by("name").limit(10).all()
  ```
  `QueryBuilder` supports `filter()`, `limit()`, `order_by()`, `values()`, `all()`, `first()`, `last()`, `count()`

- **Q Objects for OR Queries** (#92): Django-style Q objects for complex query logic
  ```python
  from popoto import Q
  Model.query.filter(Q(status="active") | Q(type="premium"))
  Model.query.filter(~Q(status="inactive"))
  ```

- **Expression-Based Queries** (#96): Python comparison operators on Field attributes
  ```python
  Model.query.filter(Model.rating > 4.0)
  Model.query.filter((Model.rating > 4.0) & (Model.status == "active"))
  ```

- **`__between` range query operator** (#131): Filter SortedField by range
  ```python
  Model.query.filter(score__between=(50, 100))
  ```

- **Plain Field filtering** (#122): Filter on non-indexed fields with client-side fallback

- **`last()` query method** (#137): Retrieve the last result from a query

- **Sorted field ordering preservation** (#139): Queries filtering on SortedField return results in sorted order by default

#### Model Methods
- **`get_or_create()` and `update_or_create()`** (#132): Django-style convenience methods
  ```python
  obj, created = Model.query.get_or_create(name="test", defaults={"score": 100})
  obj, created = Model.query.update_or_create(name="test", defaults={"score": 200})
  ```
  Async variants: `async_get_or_create()`, `async_update_or_create()`

- **`to_dict()` method** (#129): Dictionary serialization with relationship expansion
  ```python
  obj.to_dict()                          # All fields
  obj.to_dict(include=["name", "score"]) # Specific fields
  obj.to_dict(expand=True, max_depth=2)  # Expand relationships
  ```

- **`delete_all()` classmethod** (#115): Delete all instances of a model with index cleanup

- **`Model.pk` property** (#121): Clean primary key access

- **`Model.objects` alias** (#94): Django-style query manager — `Model.objects.filter()` works identically to `Model.query.filter()`

#### Bulk Operations
- **Bulk Create/Update/Delete** (#93): Efficient batch operations using Redis pipelines
  ```python
  Model.bulk_create([obj1, obj2, obj3])
  Model.bulk_update(Model.query.filter(status="pending"), status="active")
  Model.bulk_delete(Model.query.filter(status="inactive"))
  ```
  All support `batch_size` parameter and async variants

#### Migration Utilities
- **`save(skip_auto_now, update_fields)`** (#144): Fine-grained save control for migrations
- **`rebuild_indexes()`** (#146): Rebuild all secondary indexes from stored data
- **`raw_update()`** (#146): Low-level field updates bypassing hooks
- **Comprehensive migration cookbook** (#143): Step-by-step guide for common migration scenarios

#### Field Enhancements
- **Sortable ID Strategies** (#95): ULID and KSUID support for `AutoKeyField`
  ```python
  id = AutoKeyField(strategy="ulid")   # Time-sortable (requires ulid-py)
  id = AutoKeyField(strategy="ksuid")  # Time-sortable (requires cyksuid)
  ```

- **`auto_now_add` and `auto_now` on SortedField** (#133): Automatic timestamps
  ```python
  created_at = SortedField(type=float, auto_now_add=True)
  updated_at = SortedField(type=float, auto_now=True)
  ```

- **Renamed `sort_by` to `partition_by`** (#138): Better reflects the parameter's purpose. Deprecation shim maintains backward compatibility.

#### Async Support
- **Native `redis.asyncio` support** (#130): True async Redis operations — significant performance improvement over the `asyncio.to_thread()` wrapper used in beta 1
- **Full async API**: `async_save()`, `async_delete()`, `async_create()`, `async_load()`, `async_get()`, `async_filter()`, `async_all()`, `async_count()`, `async_get_or_create()`, `async_update_or_create()`, `async_delete_all()`, `async_bulk_create()`, `async_bulk_update()`, `async_bulk_delete()`

#### Developer Experience
- **`get_redis()` helper** (#137): Direct access to the Redis connection
- **`popoto.testing` module** (#137): `use_test_db()` and `flush_test_db()` helpers
- **Popoto Kitchen TUI** (#112): Interactive terminal example app for exploring features

#### Infrastructure
- **Valkey Support** (#55): Full compatibility with Valkey (Redis fork) via `REDIS_URL` or `VALKEY_URL`
- **Optional Pandas** (#63): Core library no longer requires pandas — install with `pip install popoto[dataframe]`
- **Comprehensive stress tests** (#65): Bulk ops, concurrent access, memory efficiency, geo queries, TTL

### Changed

- **Atomic saves** (#148): `save()` now executes via internal Redis pipeline for atomicity
- **SCAN vs KEYS** (#77): Pattern queries use SCAN instead of KEYS to prevent blocking
- **Msgpack deserialization** (#78): 60% faster query times via lazy field deserialization
- **Validation logic** (#72, #73): Optimized field validation with merged iteration loops
- **`filter()` with no arguments** (#81): Now correctly returns all objects
- **Pre-release polish** (#171): Exception hierarchy cleanup, connection hardening, logging improvements, type hints on core CRUD methods

### Fixed

- **KeyField index corruption on value mutation** (#150): `on_save()` now removes instance from old index Set when field value changes
- **SortedField ghost entries on partition key change** (#159): `on_save()` and `on_delete()` clean up old partition's sorted set
- **Obsolete key in class set after key change** (#161): `save()` removes old redis_key from class tracking set
- **Partial save obsolete key cleanup** (#156): `update_fields` saves properly clean up obsolete redis keys
- **Relationship index cleanup on value change** (#155): `Relationship.on_save()` removes old relationship indexes
- **Relationship validation on re-save** (#113): Lazy-loaded `redis_key` strings accepted during validation
- **Exact match queries on SortedField**: Now work correctly
- **Field.name attribute**: Properly set for expression-based queries
- **Relationship on_delete edge cases**: Lazy-loaded relationships handled correctly during deletion

### Known Considerations

- **`~Q` Negation Performance**: Negating Q objects requires scanning all keys — use with caution on large datasets
- **Bulk Operations Memory**: `bulk_update` and `bulk_delete` materialize the full queryset before processing. For 100K+ items, consider batching.

### Migration Guide from 0.x

No breaking changes. All new features are additive with full backward compatibility.

**Recommended upgrades:**

1. Switch to chainable queries: `Model.query.filter(status="active").order_by("-created").limit(10).all()`
2. Use Q objects for OR logic: `Model.query.filter(Q(status="active") | Q(type="premium"))`
3. Use bulk operations for batch processing: `Model.bulk_create(items)`
4. Rename `sort_by` to `partition_by` on SortedField (old name still works via deprecation shim)

---

## [1.0.0b2] - 2026-02-12

Beta 2 release. See [1.0.0] above for consolidated changelog.

## [1.0.0b1] - 2026-02-03

Beta 1 release. See [1.0.0] above for consolidated changelog.

## [0.9.0] - 2025-12-15

See [commit history](https://github.com/tomcounsell/popoto/compare/v0.8.3...v0.9.0) for changes.

## [0.8.3] and earlier

See [commit history](https://github.com/tomcounsell/popoto/commits/v0.8.3) for changes.
