---
status: Draft
type: feature
appetite: Large
owner: tomcounsell
created: 2026-10-03
revised: 2026-10-04
tracking: https://github.com/tomcounsell/popoto/issues/759
---

# v2: Postgres-native backend behind a model-level protocol (#759)

## Problem

The #631 POC (`poc/backend-seam`, head `efda14a3`) showed that popoto's
agent-memory semantics run on Postgres, with the seam in the wrong place. Its
46-method protocol speaks Redis structures (opaque index names, sets, sorted
sets, maps, msgpack bytes), so its Postgres backend **emulates Redis** on
generic `bytea` tables and decodes msgpack in PL/pgSQL on every ranked row:
3.4x to 6.2x on ranking whenever a payload is read (WS4 report §4), "the right
compatibility layer and the wrong storage layout" (§5).

v2 keeps one public model API with **two native implementations** behind a
model/query-level protocol: Redis keeps today's hooks, index sets and Lua, with
its behaviour frozen; Postgres gets typed tables, native indexes and its
extensions, and is where new capabilities land.

## Direction (maintainer; binding on this plan)

The maintainer ruled on 2026-10-04, settling this plan's former first question:

> "we shouldn't break what was previously working on Redis, but Postgres is the
> new target for new improvements and it's unclear how long we will need to
> continue supporting Redis. That will be a later decision based on community
> support."

1. **Postgres is native, not emulated:** per-model typed tables, B-tree/GIN
   indexes, `tstzrange`, pgvector, SQL BM25, `WITH RECURSIVE` and PostGIS, with
   no emulated sets, sorted sets or Lua.
2. **Redis is regression-frozen: not deprecated, not extended.** Everything
   that passes on Redis today keeps passing, with the same wire behaviour, on
   today's hook, index and Lua machinery. It gains no new capabilities and no
   deprecation warnings. How long Redis stays supported is an open,
   community-driven decision outside this plan; nothing here presumes it.
3. **Postgres is the target for new work.** Capabilities beyond today's
   feature set are Postgres-only, with no Redis implementation (§1.1, and the
   `[PG-only]` rows in §5).
4. **The seam moves up** to a model/query protocol. `poc/backend-seam` and its
   46 methods are not merged; the branch is a frozen reference archive, with
   durable parts salvaged in small PRs (M0).

### Reconciliation with #755 / #756 / #758

#755 (plan PR #758, `docs/plans/postgres_native_memory.md` on `docs/plan-755`)
and #756 (plan PR #757) were filed under an earlier direction. This plan is
proposed as the **single plan**, with #758 folded in as below, pending
confirmation from #758's author.

**Adopted from #755/#758:**

| Item | Source | Where it lands here |
|---|---|---|
| Valor usage survey sets the order: Valor's slice first | #758 spike-1 | M2 is Valor's slice; M3 is the rest of the memory core |
| Field-compiler contract, as the Postgres backend's *internal* shape below the protocol | #758 D2 | §2, §3 |
| Postings-table BM25 with a scope-leading key; `tsvector` and `pg_search` rejected | #758 D4, spike-4 | M2 |
| Exact-vs-HNSW vector path by scope size, plus a recall guard | #758 D5 | M2 |
| Postgres unit of work is atomic: `READ COMMITTED`, PK-ordered row locks, bounded retry on deadlock or serialization failure | #755 Q2, #758 D2 | §2, M1 |
| TTL via `_expires_at` plus an automatic reaper; no cron and no manual job | #755 Q3 | M5 |
| Optional composable scoping by agent, project and tags | #755 scope item 6 | §3 Scoping, M2 |
| "Fully subconscious": DDL on first use, piggybacked embedding backfill, staged reads that expire by comparison | #758 D2, D7 | §3, M2 |
| Integration points in `ContextAssembler` and `ObservationProtocol`, including a Postgres branch ahead of `batch()` in `_post_effects` | #758 D8 | M2 |
| Outage contract and health record; engine and import-provenance columns; NUL refusal; statics keep their return shape | #758 D3, D6; #755 Q4, Q5 | §1.1 |
| Pool per (DSN, pid), `max_size` 4, fork-safe | #758 D6 | M1 |

**#756** stays a companion: a one-off Redis→Postgres copy for Valor and
Yudame, not dual-write, read-through or a runtime feature. It targets the DDL
M2 pins. `transfer/` on Postgres (M5) is same-backend parity, not a cutover.

**Kept from this plan where #758 differs:**

- **"Redis memory is frozen or deprecated."** Frozen, never deprecated: the
  ruling leaves Redis's support horizon to a later, community-driven decision.
- **A separate `popoto.pg.Model` base class.** `Meta.backend = "postgres"` on
  the one `Model` instead: a parallel base class forks the model and query
  surface, so the parity gate could not run the existing tests against it.
- **Valor-unused features "rejected, not deferred".** Ordered later (M3–M5),
  because popoto is a published library and parity covers its feature set.
- **Per-scope BM25 statistics as the only mode.** `BM25Field.search` keeps
  corpus-wide statistics so parity holds; per-scope is a `recall()` option
  (Open Question 1).
- **"No `src/popoto/backends/` on main."** Dropped: that directory holds this
  plan's model-level protocol, not the POC's seam.

## Freshness Check

**Baseline:** `origin/main` @ `57c29ebf`. **Reference archive:** `origin/poc/backend-seam` @ `efda14a3`.

- `src/popoto/backends/` and a `conformance` marker in `pytest_plugin.py`
  exist only on the POC branch.
- #630 is **closed**, so recipes reach storage through field/model methods,
  except `recipes/question_queue.py` (#730, after the POC forked) with its own
  `_DELIVER_LUA`, `_CLAIM_LUA`, `_RELEASE_LUA` (M4).
- #747 is open: in `src/popoto/fields/validity_field.py` a quoting comment at
  :336 precedes the real `-- MUTATION PHASE` marker at :395, and
  `scripts/check_supersede_lua_phases.py` partitions on the first one.
- `main` has 32 named `*_LUA` scripts (26 in `fields/`, 1 in `models/base.py`,
  3 in `recipes/question_queue.py`, 2 in `extraction/decision_log.py`), one
  inline script in `Model.atomic_increment`, and 73 `pipeline if pipeline` /
  `if pipeline:` sites.

## 1. Public surface the protocol must cover

Derived from `src/popoto/__init__.py` `__all__`, `models/base.py`,
`models/query.py`, the field modules and `recipes/`. Tier is the milestone that
delivers the row on Postgres. Every row is an existing capability, so it falls
under both §5 gates (Redis regression, Postgres parity).

| Capability (public entry point) | Redis mechanism today | Protocol v2 method | Postgres-native mapping | Tier |
|---|---|---|---|---|
| `Model.save()` (`update_fields`, `migrate_key`, `ignore_errors`, `skip_auto_now`) | msgpack `HSET` + `$Class:` set + per-field `on_save` hooks + `INDEX_SWAP_LUA` | `save` | `INSERT … ON CONFLICT (_pk) DO UPDATE SET <changed cols>`; `migrate_key` = `UPDATE … SET _pk` in the same txn | M1 |
| `Model.create`, `get_or_create`, `update_or_create` | composed of `load` + `save` | above the seam | composed, inside one `transaction()` | M1 |
| `Model.load`, `Query.get`, `Query.get_many` | `HGETALL` / pipelined `HGETALL` + `decode_popoto_model_hashmap` | `load` | `SELECT … WHERE _pk = ANY($1)` | M1 |
| `Model.load_fields`, `values=` projection, `load_raw_hash` | `HMGET` (`load_raw_hash` returns raw hash bytes) | `load(fields=)` | column projection; `load_raw_hash` stays Redis-only (debug API) | M1 |
| `Model.delete`, `delete_all`, `bulk_delete` | `on_delete` hooks + `DEL` + `SREM` | `delete` | `DELETE … WHERE _pk = ANY`; companions `ON DELETE CASCADE` | M1 |
| `Model.exists` | `EXISTS` | `exists` | `SELECT _pk … = ANY` | M1 |
| `Model.atomic_increment` | inline Lua: decode msgpack, add, re-encode, `HSET` | `increment` | `UPDATE … SET f = f + $d RETURNING f` | M1 |
| `Query.filter` exact / `__in` / `__isnull` on `KeyField`/`IndexedField` | `SINTER` over `$KeyF:` / `$IndexF:` sets | `select` | `WHERE` on a B-tree column | M1 |
| `__startswith` / `__endswith` / `__contains` (`key_field_mixin.py`, `indexed_field_mixin.py`) | set scans / `KEYS` glob | `select` | `LIKE` with `text_pattern_ops`; `pg_trgm` GIN for `__contains` (capability) | M1 |
| `__gt`/`__gte`/`__lt`/`__lte`/`__between` on `SortedField` (`sorted_field_mixin.py:239`), `partition_by` | `ZRANGEBYSCORE` on `$SortF:` (partitioned keys) | `select` | B-tree `(partition cols…, field)` | M1 |
| `Q` objects (`&`, `\|`, `~`), `Expression`/`CombinedExpression` | set algebra in Python over per-field key sets | `select` (`Predicate` tree) | boolean `WHERE` | M1 |
| `order_by`, `Meta.order_by`, `limit`, `first`/`last`, `count`, `keys()` | `ZRANGE`/Python sort, `SCARD` | `select`, `count` | `ORDER BY … LIMIT/OFFSET`, `count(*)` | M1 |
| `computed_sort`, `post_filter`, `to_dict`, `bulk_create`/`bulk_update` | Python over hydrated rows | above the seam | unchanged | M1 |
| `UniqueField`/`UniqueKeyField` conflict → `ModelException` | `INDEX_SWAP_LUA` `POPOTO_UNIQUE_CONFLICT` | `save` | `UNIQUE` index; `23505` → same `ModelException` text | M1.1 |
| `TagField` `__any`/`__all`/`__contains` | `TAG_SWAP_LUA`, tag sets | `select` | `text[]` + GIN (`&&`, `@>`) | M1.1 |
| `Relationship` (lazy key string, reverse lookups, `sample_related_keys`) | stored target key string + `$RelationshipF:` set + `SRANDMEMBER` | `select`, `load` | `text` column holding the target `_pk` + B-tree; `TABLESAMPLE`/`ORDER BY random() LIMIT` | M1.1 |
| `DecayingSortedField` / `top_by_decay` (`base_score_field`, `as_of`) | `DECAY_SCORE_LUA` over a ZSET | `rank_decayed` | `ORDER BY` decay expression over typed columns | M2 |
| `Model.touch` | `ZADD` new timestamp | `touch` | `UPDATE … SET f_at = $at` | M2 |
| `ConfidenceField.update_confidence`/`get_confidence`, partitioned confidence | `CAPPED_BAYESIAN_UPDATE_LUA` on a companion hash | `update_confidence`; reads via `load` | four typed columns, one `UPDATE … RETURNING` | M2 (partitioned: M3) |
| Confidence-modulated decay | `DECAY_SCORE_LUA` reads the confidence hash per member | `rank_decayed(confidence_field=)` | one expression over the confidence column | M2 |
| `ValidityField` `__as_of` / `__current`, `resolve_excluded_keys` | three ZSETs + exclusion rule | `select` (`Predicate` op `valid_at`) | `tstzrange` column, GiST, `@>` | M3 |
| `SupersessionProtocol.supersede`/`invalidate`/`save_and_*` | `SUPERSEDE_LUA` (validate-then-mutate) + open-pointer STRING | `supersede` | one txn: lock, validate, `UPDATE` upper bound, insert; partial unique index on the open identity | M3 |
| `superseded_by`/`supersedes`/`chain` | `HGET` on the chain hashes | `chain` | `WITH RECURSIVE` over `f_supersedes` | M3 |
| `EmbeddingField`, `semantic_search`, `load_embeddings` | `.npy` files + in-process numpy cosine | `vector_search` | pgvector `vector(d)`, `<=>`; HNSW past a size threshold | M2 (`semantic_search`: M3) |
| `BM25Field.search`, `keyword_search` | `BM25_SAVE/DELETE/SEARCH_LUA` postings | `keyword_search` | scope-keyed postings table, BM25 in SQL (#758 D4); corpus-wide statistics, as on Redis | M2 (`keyword_search`: M3) |
| `composite_score` (`co_occurrence_boost`, `similarity_boost`, `temperature`) | temp ZSETs + `ZUNIONSTORE` (POC `native()` sites 6–12) | `rank_composite` | one `SELECT` with a weighted sum over columns and CTE arms | M2, for the decay and confidence arms `assess` uses (`co_occurrence_boost`: M4) |
| `fuse` (RRF), `ContextAssembler` hybrid path | Python RRF over `(redis_key, score)` lists | above the seam (lists of `(RecordId, score)`) | unchanged; SQL push-down is an optimisation | M2 |
| `ExistenceFilter.might_exist`/`_batch`/`definitely_missing`, `FrequencySketch` | `BLOOM_*_LUA`, `CMS_*_LUA` | `membership_add`, `membership_query` | `(field, token)` / `(field, token, count)` tables (exact; §1.1) | M2 |
| `CoOccurrenceField.link`/`strengthen`/`unlink`/`weaken_all` | `LINK_WITH_PRUNE_LUA`, `STRENGTHEN_CLAMP_LUA`, `WEAKEN_ALL_LUA` | `graph_update` | edge table upsert; prune by a window rank | M4 |
| `CoOccurrenceField.propagate`, `recipes/graph_traversal.traverse` | `PROPAGATE_BFS_LUA`, `SRANDMEMBER` | `graph_expand` | `WITH RECURSIVE` with depth/threshold | M4 |
| `AccessTrackerMixin` (`on_read`, `confirm_access`, `discard_staged_access`) | `CONFIRM_ACCESS_LUA`, meta hashes | `field_call` | `_access_count`, `_last_accessed` columns + staged-read columns that expire by comparison (#758 D2) | M2 |
| `WriteFilterMixin`, `ObservationProtocol`, `NeverRecordMixin`, `AppendOnlyMixin` | Python above storage; `$WF:` priority set | above the seam, plus `field_call` for the priority tier | unchanged | M2 (`NeverRecordMixin`, `AppendOnlyMixin`: M4) |
| Recipes: `ContextAssembler`, `AdaptiveAssembler`, `DefaultMemory`, `SubconsciousMemory`, `TrajectoryMemory`, `MemoryLifecycle`, `ProvenanceJournal`, `TelemetryRecorder`, `BeliefSheetResolver`, `reconciliation`, `policy_cache` | field/model methods (#630) | none new | follows from M1–M3 | M4 (`ContextAssembler`: M2) |
| `recipes/question_queue.py` | `_DELIVER/_CLAIM/_RELEASE_LUA` | `field_call` (or a model-level `claim`) | `SELECT … FOR UPDATE SKIP LOCKED` | M4 |
| `Model.idle_seconds` (`memory_lifecycle`) | `OBJECT IDLETIME` | `field_call` | `now() - _last_accessed` | M4 |
| `Meta.ttl`, `save(ttl=/expire_at=)` | `EXPIRE`/`EXPIREAT` | `save(expiry=)` | `_expires_at` column + read filter + automatic reaper on writes (#755 Q3) | M5 |
| `popoto.batch()`, `pipeline=` kwarg everywhere | `GuardedPipeline` (`MULTI`/`EXEC`) | `transaction` | one DB transaction | M5 |
| `async_*` twins, `get_async_redis_db` | `redis.asyncio` | `AsyncBackend` twin | `psycopg.AsyncConnection` pool | M5 |
| `Publisher`/`Subscriber`, `EventStreamMixin`, `StreamConsumer` | `PUBLISH`/`SUBSCRIBE`, `XADD`/`XREADGROUP` | separate `PubSub` protocol (not counted) | `LISTEN`/`NOTIFY` + an events table with `bigserial` | M5 |
| `GeoField` (`_latitude`/`_longitude`/`_radius` filters) | `GEOADD`/`GEOSEARCH` | `select` (op `within`) | PostGIS `geography(Point)` + GiST (capability) | M5 |
| `CyclicDecayField`, `resolve_pressure`/`strengthen_cycle`/`weaken_cycle` | `CYCLIC_DECAY_LUA`, `CYCLES_*_LUA` | `rank_decayed` variant + `field_call` | `jsonb` cycles + SQL expression | M5 |
| `PredictionLedgerMixin`, `TDValueField.td_update` | `RESOLVE_PREDICTION_LUA`, `TD_UPDATE_LUA` | `field_call` | companion table / `UPDATE … RETURNING` | M5 |
| `DataFrameField`, `ContentField` | msgpack bytes / filesystem store | `save`/`load` | `bytea` / `text` (or keep the `ContentStore`) | M5 |
| `check_indexes`/`clean_indexes`/`rebuild_indexes`, `migrations.py` cookbook | Redis set and ZSET scans and repairs | `maintain` | indexes are transactional, so check finds no drift; rebuild = `REINDEX` + `ANALYZE` | M5 |
| `export_records`/`import_records`, `transfer/` | per-field `export_state`/`import_state` | composed of `load`/`save` (+ `field_call`) | same-backend parity; Redis→Postgres copy is #756 | M5 |
| `Query.keys(catchall=True)`, `load_raw_hash`, `extraction/decision_log.py` Lua | raw `KEYS`/`HGETALL`/Lua | none | Redis-only debug and extraction surfaces; raise `BackendCapabilityError` | — |

### 1.1 Postgres-only: new capabilities and documented divergences

**New capabilities `[PG-only]`** go beyond today's feature set. They have
**no Redis implementation**: on a Redis-bound model they raise
`BackendCapabilityError`. Each is a `PostgresBackend` method or a
compiler-registered `field_call`, not a protocol method, tested under
`tests/postgres/` with no Redis leg.

| Capability | Source | Milestone |
|---|---|---|
| `Query.recall(q, scope=, filters=, tags=, weights=, limit=)`: BM25, vector and decay×confidence arms fused by RRF (k=60) in **one** SQL statement, returning `(instance, score)` pairs | #758 Data Flow | M2 |
| Composable optional scoping: `scope=` is the `partition_by` column, `filters=` takes any `KeyField`/`IndexedField`, and `tags=` takes a `TagField` (`&&`/`@>`); all are optional and compose | #755 item 6 | M2 |
| Per-scope BM25 statistics inside `recall` (`bm25_stats="scope"`) | #758 D4 | M2 |
| `Model.top_by_relevance(scope, limit)`, decay×confidence in SQL (replaces Valor's raw `ZREVRANGE`) | #758 D8 | M2 |
| Piggybacked embedding backfill after a successful save (`embedding_model`, `embedded_hash` columns; bounded batch and wall-clock budget) | #758 D7 | M2 |
| `_migrated_from` / `_estimated_fields` engine columns, as #756's import contract | #758 D3 | M2 |
| Engine-owned `_created_at` / `_updated_at` on every table | #758 D7 | M1 |
| Backend health record (`ok`, `last_ok_at`, `consecutive_failures`, `dropped_writes`), logged at ERROR once per outage window | #758 D6 | M1 |

**Documented divergences.** Here Postgres is stricter than Redis. Redis
behaviour does not change to match (Direction 2).

| Behaviour | Redis (unchanged) | Postgres | Source |
|---|---|---|---|
| Unit-of-work failure | `MULTI`/`EXEC` applies queued commands and reports runtime errors without rollback | the whole transaction rolls back | #755 Q2 |
| `\x00` in a text value | stored | `ValueError` naming the field, at `to_db` (TD-12) | #755 Q4 |
| `ExistenceFilter.might_exist` | bloom, about 1% false positives | exact token table; no false positives, forgets on delete | #758 D3 |
| `increment` past `2**63` | Lua goes to float | `NumericValueOutOfRange` (TD-26) | POC |
| `DecayingSortedField.rank_decayed(zset_key, …)`, which takes a ZSET key | works | `BackendCapabilityError` naming `top_by_decay` as the backend-neutral call | TD-40 |

A test asserting a Redis-side property in this table is marked `redis_only`
with that reason, not rewritten. Statics that return Redis keys
(`BM25Field.search`, `EmbeddingField.load_embeddings`,
`CoOccurrenceField.get_linked`) need no divergence: `_pk` *is* the canonical
`ClassName:…` string, so both backends return the same strings (#755 Q5).

## 2. Protocol v2

### Types (in `src/popoto/backends/types.py`)

```python
@dataclass(frozen=True)
class RecordId:            # identity of one record, backend-neutral
    model: str             # ModelSpec.name
    values: tuple[Any, ...]  # KeyField values, in DB_key order
    canonical: str         # today's DB_key string; Model.pk on BOTH backends

@dataclass(frozen=True)
class FieldSpec:  name: str; kind: FieldKind; py_type: type | None; null: bool; options: Mapping[str, Any]
@dataclass(frozen=True)
class ModelSpec:  name: str; key_fields: tuple[str, ...]; fields: Mapping[str, FieldSpec]
                  order_by: str | None; ttl: int | None; indexes: tuple[tuple[str, ...], ...]

Predicate = Cond(field: str, op: Op, value: Any) | And(tuple[Predicate, ...]) | Or(...) | Not(Predicate)
@dataclass(frozen=True)
class QueryPlan:  where: Predicate | None; order_by: tuple[OrderTerm, ...]; limit: int | None
                  offset: int; project: tuple[str, ...] | None; as_of: float | None
Row = Mapping[str, Any]                      # decoded Python values; always carries "_id": RecordId
Scored = list[tuple[RecordId, float]]        # every ranking method returns typed pairs
```

`ModelSpec` is built once by `ModelBase` from `_meta`. `Op` is the closed set
of §1 suffixes plus `valid_at` and `within`. Values cross the protocol
**decoded**: no msgpack, key strings or index names.

### Methods (24)

```python
class Backend(Protocol):
    # A. lifecycle (3)
    def bind(self, spec: ModelSpec) -> Capabilities: ...       # at class creation; refuses unsupported fields (TD-9)
    def transaction(self) -> ContextManager[UnitOfWork]: ...   # popoto.batch() and every pipeline= kwarg
    def close(self) -> None: ...
    # B. records (5)
    def save(self, obj: "Model", *, fields: Sequence[str] | None = None, previous_id: RecordId | None = None,
             expiry: Expiry | None = None, uow: UnitOfWork | None = None) -> SaveOutcome: ...
    def load(self, spec: ModelSpec, ids: Sequence[RecordId], *, fields: Sequence[str] | None = None) -> list[Row | None]: ...
    def delete(self, spec: ModelSpec, ids: Sequence[RecordId], *, uow: UnitOfWork | None = None) -> int: ...
    def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]: ...
    def increment(self, spec: ModelSpec, id: RecordId, field: str, delta: int | float, *, uow=None) -> int | float: ...
    # C. query (2)
    def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]: ...   # project=() returns id-only rows
    def count(self, spec: ModelSpec, plan: QueryPlan) -> int: ...
    # D. memory state (4)
    def touch(self, spec, id: RecordId, field: str, *, at: float, uow=None) -> float: ...
    def update_confidence(self, spec, id: RecordId, field: str, signal: float, *, uow=None) -> ConfidenceState | None: ...
    def supersede(self, spec, field: str, *, successor: RecordId, incumbent: RecordId | None, identity: str | None,
                  mode: SupersedeMode, valid_from: float | None, invalid_at: float | None, now: float,
                  uow=None) -> SupersedeOutcome: ...
    def chain(self, spec, field: str, id: RecordId) -> list[RecordId]: ...   # oldest first
    # E. ranking and retrieval (4)
    def rank_decayed(self, spec, field: str, *, now: float, n: int | None, where: Predicate | None = None,
                     as_of: float | None = None, decay_rate: float | None = None, base_score_field: str | None = None,
                     confidence_field: str | None = None, validity_field: str | None = None) -> Scored: ...
    def rank_composite(self, spec, terms: Sequence[RankTerm], *, limit: int, aggregate: Literal["SUM", "MAX", "MIN"],
                       min_score: float | None, where: Predicate | None, as_of: float | None, temperature: float) -> Scored: ...
    def vector_search(self, spec, field: str, query: Sequence[float], *, limit: int, where: Predicate | None = None,
                      min_score: float | None = None) -> Scored: ...
    def keyword_search(self, spec, field: str, tokens: Sequence[str], *, limit: int, where: Predicate | None = None,
                       allowed: Collection[RecordId] | None = None) -> Scored: ...
    # F. graph (2)
    def graph_update(self, spec, field: str, op: GraphOp, src: RecordId, dst: RecordId | None,
                     amount: float | None, *, uow=None) -> None: ...
    def graph_expand(self, spec, field: str, seeds: Sequence[RecordId], *, depth: int, decay_per_hop: float,
                     threshold: float, fanout: int | None) -> Scored: ...
    # G. membership (2)
    def membership_add(self, spec, field: str, tokens: Sequence[str], *, uow=None) -> None: ...
    def membership_query(self, spec, field: str, tokens: Sequence[str], *, mode: Literal["any", "each", "count"]) -> list[int] | list[bool] | bool: ...
    # H. maintenance and extension (2)
    def maintain(self, spec, op: Literal["check", "clean", "rebuild"], *, batch_size: int = 1000) -> MaintenanceReport: ...
    def field_call(self, spec, field: str, op: str, /, *args: Any, uow=None, **kwargs: Any) -> Any: ...
```

`UnitOfWork` is a wrapper class with `__bool__ = True` and an
`is_redis_pipeline` accessor, not duck-typed `pipeline=` (POC decision 1;
TD-10). `field_call` dispatches to a per-backend **field-adapter registry**
keyed by `(FieldClass via MRO, op)`, so the long tail (PredictionLedger,
CyclicDecay, TDValue, WriteFilter priority, `idle_seconds`, question-queue
claim) needs no protocol method per mixin. An unregistered `(field, op)` raises
`BackendCapabilityError`, and `bind()` refuses a field missing an adapter.

**Not on the protocol, on purpose:** `fuse`/RRF, `computed_sort`,
`post_filter`, `to_dict`, `get_or_create`/`update_or_create`/`bulk_*`
(composed in one `transaction()`), `ObservationProtocol`, the never-record
firewall, and pub/sub (a separate `PubSub` protocol, M5). The async twin
(`AsyncBackend`, M5) mirrors the 24 methods one for one.

### How each backend implements it

The Redis column is a **move, not a rewrite**: field modules keep their own
`get_REDIS_DB()`/`run_lua` calls, reachable only through `RedisBackend`, and
today's `Model.save` body becomes `backends/redis/records.py::save` with hooks
firing from there. `src/popoto/backends/postgres/` never imports `redis`, and
`models/base.py`/`models/query.py` public methods hold no
`get_REDIS_DB`/`run_lua`.

| Method | Redis: delegates to (on `main` today) | Postgres: SQL |
|---|---|---|
| `bind` | no-op; returns the full capability set | compile `ModelSpec` → DDL; check the `popoto_schema` version row; refuse unsupported fields |
| `transaction` | `batch()` → `GuardedPipeline` (`MULTI`/`EXEC`) | pool connection, one `READ COMMITTED` transaction, atomic on failure (§1.1); rows locked in PK order; `DeadlockDetected`/`SerializationFailure` retried up to 3 times with jitter, then re-raised (TD-2, #758 D2) |
| `save` | `Model.save` body: `HSET` msgpack, `$Class:` `SADD`, field `on_save` hooks, `INDEX_SWAP_LUA`/`TAG_SWAP_LUA`, `EXPIRE` | `INSERT … ON CONFLICT (_pk) DO UPDATE SET …` + compiler `write_sql` (postings, edges) in one txn |
| `load` | `Query.get`/`get_many`: `HGETALL`/`HMGET` + `decode_popoto_model_hashmap` | `SELECT <cols> FROM <table> WHERE _pk = ANY($1)` |
| `delete` | `Model.delete` body: `on_delete` hooks, `DEL`, `SREM` | `DELETE … WHERE _pk = ANY($1)`; companions cascade |
| `exists` | pipelined `EXISTS` | `SELECT _pk … WHERE _pk = ANY($1)` |
| `increment` | `Model.atomic_increment` Lua | `UPDATE … SET f = f + $2 WHERE _pk = $1 RETURNING f` |
| `select`/`count` | `Query.filter_for_keys_set` + each field's `filter_query`, then Python order/limit | `WHERE` compiled from `Predicate`; `ORDER BY …, _pk COLLATE "C"`; `LIMIT/OFFSET`; `count(*)` |
| `touch` | `Model.touch` (`ZADD`) | `UPDATE … SET f_at = $at` |
| `update_confidence` | `ConfidenceField.update_confidence` (`CAPPED_BAYESIAN_UPDATE_LUA`) | single `UPDATE … SET (c, n, corr, contra) = (<capped-mean expr>) RETURNING …` |
| `supersede` | `ValidityField.execute_supersede` (`SUPERSEDE_LUA`) | one txn: advisory lock `(model, field)` → `FOR UPDATE` incumbent → validation (same error tokens as POC `popoto_supersede`) → `UPDATE f = tstzrange(lower(f), $at)` → insert/repoint |
| `chain` | `supersession._walk_one` over the chain hashes | `WITH RECURSIVE` over `f_supersedes` / `f_superseded_by` |
| `rank_decayed` | `DecayingSortedField.rank_decayed` (`DECAY_SCORE_LUA`) | `SELECT _pk, <decay expr> s … WHERE <where> AND f_valid @> $as_of ORDER BY s DESC, _pk COLLATE "C" LIMIT $n` |
| `rank_composite` | `QueryBuilder.composite_score` (temp ZSETs, `ZUNIONSTORE`) | one `SELECT` with a weighted sum of column expressions; similarity and co-occurrence arms as CTEs |
| `vector_search` | `EmbeddingField.load_embeddings` + numpy cosine | `ORDER BY v <=> $q LIMIT $n`; exact under the scope threshold, HNSW above it (#758 D5) |
| `keyword_search` | `BM25Field.search` (`BM25_SEARCH_LUA`) | BM25 over `(scope, term, _pk, tf)` postings; corpus-wide N/avgdl as on Redis, per-scope as a `recall` option (#758 D4) |
| `graph_update` | `CoOccurrenceField.link`/`strengthen`/`unlink`/`weaken_all` Lua | upsert into `<model>__<field>_edges`; prune past `max_edges` with `row_number()` |
| `graph_expand` | `CoOccurrenceField.propagate` (`PROPAGATE_BFS_LUA`), `graph_traversal` | `WITH RECURSIVE` bounded by depth, threshold and fan-out |
| `membership_*` | `ExistenceFilter`/`FrequencySketch` Lua | `(token)` / `(token, count)` companion tables (exact; §1.1) |
| `maintain` | `check_indexes`/`clean_indexes`/`rebuild_indexes` bodies | `check` returns zero drift by construction; `rebuild` = `REINDEX TABLE` + `ANALYZE` |
| `field_call` | the existing field statics and Lua | a compiler-registered statement per op |

## 3. Schema strategy on Postgres

### Tables and keys

- **One table per concrete model**, `popoto.<model_snake>` (schema from
  `POPOTO_POSTGRES_SCHEMA`). `_pk text PRIMARY KEY` holds the **canonical key
  string** (today's `DB_key`), so `Model.pk`, `fuse` inputs and `Relationship`
  values are unchanged. Each `KeyField` is also a typed column under one
  `UNIQUE`, so typed `WHERE` never parses `_pk`.
- **Engine-owned columns:** `_created_at`, `_updated_at`; `_expires_at` only
  with `Meta.ttl` or expiry, so TTL-free models keep TTL-free plans;
  `_migrated_from jsonb` and `_estimated_fields text[]` for #756 (every native
  write sets `_migrated_from = NULL`; a delta re-load overwrites only rows
  where it is still set).

### Field → column type mapping

| popoto Field (`fields/shortcuts.py` unless noted) | Column(s) | Index |
|---|---|---|
| `AutoKeyField` / `KeyField(type=T)` / `UniqueKeyField` | column of T (default `text`) | part of the key `UNIQUE`; unique fields get their own `UNIQUE` |
| `IntField` / `FloatField` / `DecimalField` / `BooleanField` | `bigint` / `double precision` / `numeric` / `boolean` | none unless indexed |
| `StringField` / `BytesField` | `text` (NUL refused, TD-12) / `bytea` | none unless indexed |
| `ListField` / `TupleField` / `DictField` / `SetField` | `jsonb` (`ListField(max_length=)` cap enforced in Python as today) | none (GIN on demand) |
| `DateField` / `TimeField` | `date` / `time` | none unless sorted |
| `DatetimeField` (`fields/datetime_field.py`) | `timestamptz` + `<f>__utcoff integer NULL` (offset seconds; NULL = naive) | the offset column preserves #521's round-trip contract, which `timestamptz` alone drops |
| `SortedField` / `SortedKeyField` (`partition_by=`) | typed column | B-tree `(partition cols…, f)` |
| `IndexedField` / `UniqueField` | typed column | B-tree / `UNIQUE` |
| `TagField` | `text[]` | GIN |
| `Relationship` | `text` (target `_pk`) | B-tree; **no FK by default** (Redis enforces none; circular refs) |
| `GeoField` | `geography(Point,4326)` | GiST (needs PostGIS) |
| `DecayingSortedField` | `<f>_at timestamptz` | B-tree `(partition cols…, <f>_at)` |
| `ConfidenceField` | `<f> double precision`, `<f>_n int`, `<f>_corr int`, `<f>_contra int` | B-tree on `<f>` |
| `ValidityField` | `<f> tstzrange`, `<f>_ingested_at timestamptz`, `<f>_identity text`, `<f>_supersedes text`, `<f>_superseded_by text` | GiST on `<f>`; partial `UNIQUE (<f>_identity) WHERE upper_inf(<f>)` replaces the open-pointer STRING |
| `EmbeddingField` | `vector(d)` (`d` from provider) | HNSW `vector_cosine_ops` past the threshold (needs pgvector) |
| `BM25Field` | `<f>_len int` + companion `<model>__<f>_postings(scope, term, _pk, tf)` | PK `(scope, term, _pk)` |
| `CoOccurrenceField` | companion `<model>__<f>_edges(src, dst, weight)` | PK `(src, dst)`, B-tree `(dst)` |
| `ExistenceFilter` / `FrequencySketch` | companion `(token)` / `(token, count)` | PK |
| `ContentField` | `text` (or a `ContentStore` reference, as today) | none |
| `CyclicDecayField` / `TDValueField` | `jsonb` cycles + pressure / `double precision` | none |
| `AccessTrackerMixin` | `_access_count int`, `_last_accessed timestamptz`, `_staged_reads int`, `_staged_at timestamptz` (#758 D2) | B-tree `(_last_accessed)` |
| `DataFrameField` | `bytea` | none |
| custom `Field` subclass with no compiler | if it overrides no hook: column from `type=`; otherwise `bind()` refuses | Open Question 2 |

### Migrations

DDL ownership follows TD-8 and #758 D2, with no manual step in normal use.
Popoto keeps a deterministic fingerprint per model in `popoto_schema(model,
fingerprint, ddl, applied_at)`. On first use per **process**, under
`pg_advisory_xact_lock(hashtext('popoto:ddl:'||table))`, a **missing table** is
created and an **additive** change (nullable column, index, companion) is
applied. A **destructive or ambiguous** change (drop, retype, vector dimension,
key-field change) raises `SchemaDriftError` with the diff; the operator runs
`python -m popoto.schema migrate <dotted.Model>` (`--dry-run` prints the DDL).
`POPOTO_SCHEMA_AUTO=0` disables even create/additive. The `migrations.py`
cookbook stays Redis-specific.

### Multi-tenancy and scoping (`docs/multi-tenancy.md`, #755 item 6)

- **Namespaces.** The documented `KeyField` namespace plus `partition_by` maps
  directly: a typed key column that leads every partitioned index.
  Partitioned `ConfidenceField` becomes a `WHERE` on it, and the "query must
  include filter(s) for: project" refusal stays a field-layer check.
- **Composable scoping, all optional, no new `Meta` option.** The scope is the
  `partition_by` column (Valor: `project_key`); it leads the decay and BM25
  postings keys and filters the vector arm. Without `partition_by` the scope is
  `''`, matched as `coalesce(col, '') = $s` so NULL is never dropped. Other
  `KeyField`s (Valor: `agent_id`) are B-tree filters and a `TagField` is a GIN
  filter; all three compose in `recall(scope=, filters=, tags=)` [PG-only].
- **Declarative partitioning and RLS** are operator options; popoto emits
  neither.

### Extensions and capabilities

`bind()` checks `pg_extension` once per process: an `EmbeddingField` without
`vector`, or a `GeoField` without `postgis`, raises at declaration. Bootstrap
refuses `server_encoding <> 'UTF8'` (TD-13); every tie-break uses
`COLLATE "C"`, matching Redis's bytewise member order.

## 4. Selection

- **`POPOTO_BACKEND=redis|postgres`**, default `redis`, read lazily at the
  first `get_backend()` (TD-7). No auto-selection from `POSTGRES_URL`,
  `DATABASE_URL` or any generic variable. **`POPOTO_POSTGRES_URL`** gives the
  DSN; selecting Postgres without it, or without `psycopg`, is a hard error
  naming the variable.
- **Per-model override.** `Meta.backend = "redis" | "postgres"`, because Valor
  keeps Redis counters and a Postgres `Memory` in one process (#758). It
  replaces #758's `popoto.pg.Model`: Valor's cutover is one `Meta` line.
- **Unchanged.** `get_redis()`, `POPOTO_REDIS_DB` and `REDIS_URL` keep their
  meaning. Importing popoto never dials Postgres or needs `psycopg` (M1).
- **Tests.** The pytest plugin pins `redis` for the session whenever it opted
  in. The `backend` fixture binds Postgres per test and restores the
  *previous* binding (salvaged POC rule, `docs/testing.md`).

## 5. Milestones

Each milestone is one or more PRs against `main`, each independently
shippable.

### Gates

| Gate | Applies to | Check | Passes when |
|---|---|---|---|
| **(a) Redis regression** | every PR, M0–M5 | full suite on the Redis and Valkey jobs in `tests.yml`; M1 wire-trace diff wherever a Redis path moved | every test that passes on Redis at the PR's base commit still passes, with none deleted, skipped, loosened or re-marked to get there. Redis behaviour is frozen: no PR deprecates or extends it |
| **(b) Postgres parity** | existing-feature rows, M1–M5 | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres pytest -m conformance` in the `postgres` job | the milestone's listed existing test files pass on the Postgres leg from the **same test code**; a Redis-key or §1.1-divergence assertion is `redis_only` with a reason, never deleted |
| `[PG-only]` tests | §1.1 new capabilities | `pytest tests/postgres/` in the `postgres` job | the capability's own tests pass; there is no Redis leg and no Redis code path is added for one |

Per-leg counts state their attribution rule (TD-33). Gate (a) permits the §2
*move* of Redis code, which changes neither what Redis does nor what it is
sent. M2 and M3 follow Valor's usage (#758 spike-1).

| Milestone | Existing features: gates (a) + (b) | `[PG-only]` additions: gate (a) + own tests |
|---|---|---|
| M0 | salvage harness, markers, CI job, #747 fix (gate (a) only; no Postgres code) | — |
| M1 | plain models: records, query, `Q`, ordering, count, increment; pool, schema compiler, atomic transaction | engine `_created_at`/`_updated_at`; health record + `BackendUnavailableError` |
| M1.1 | indexed/unique/tag/relationship/collection fields, `Meta.indexes` | — |
| M2 | **Valor's slice:** decay + `touch`, confidence (+ modulation), BM25, embeddings, `ExistenceFilter`/`FrequencySketch`, `AccessTrackerMixin`, `WriteFilterMixin`, `ObservationProtocol`, `fuse`, `composite_score` (decay/confidence arms), `ContextAssembler` hybrid + `assess` + `_post_effects` | `recall()`; composable scoping; per-scope BM25 stats; `top_by_relevance`; embedding backfill; `_migrated_from`/`_estimated_fields`; Valor `Memory` DDL pinned for #756 |
| M3 | memory core Valor does not use: `ValidityField` + supersession + `chain`, partitioned confidence, `semantic_search`/`keyword_search` | — |
| M4 | graph (`CoOccurrenceField`, `graph_traversal`), remaining recipes and mixins, question queue, `idle_seconds` | — |
| M5 | TTL (`_expires_at` + automatic reaper), `batch()`, async, pub/sub, geo, long-tail fields, `maintain`, `transfer/` | — |

### M0: salvage to `main`, zero behaviour change

- **Scope.** Four PRs, each cherry-picked or ported from `efda14a3`, not
  rebased. **(a)** The #747 fix: partition on the last or column-0 marker,
  plus a negative test that plants a write in the validation phase. **(b)**
  `pytest_plugin.py`: `conformance`/`redis_only` markers, with `reason=`
  *required* (13 POC marks lacked one); `popoto_conformance_backends` ini/env;
  Postgres schema isolation (`popoto_test_<hex>`, refusing `public`, `popoto`
  and a db-less URL); the Redis session pin. **(c)** The `postgres` CI job in
  `tests.yml`: a `pgvector/pgvector` service plus Redis, `REDIS_URL` pinned to
  DB 15 per #639. **(d)** The `postgres` extra (`psycopg[binary,pool]`,
  `pgvector`), `check_lock_imports.py` and `uv.lock`.
- **Not salvaged.** `backends/` and the 46-method protocol; the
  `tests/conformance/test_{records,indexes,swaps,decay,validity}.py` files test
  that protocol and stay in the archive.
- **Exit criteria.** Gate (a). Harness self-tests in the style of the archive's
  `test_harness.py`/`test_postgres_bootstrap.py` pass in the `postgres` job,
  and the Postgres model leg skips with a named reason. The phase checker fails
  on the planted write; the ratchet is not above its ceiling.
- **Tests.** `tests/test_pytest_plugin.py`, `tests/test_validity_field.py::TestSupersedeLuaPhaseSplit`, `tests/test_ci_workflow_redis_url.py`, `tests/test_check_lock_imports.py`.

### M1: a plain-model vertical slice on typed tables

- **Scope.** `backends/{__init__,types}.py` (protocol, selection, `ModelSpec`)
  and `RedisBackend` (the §2 move). `PostgresBackend` groups A–C for
  `KeyField`/`AutoKeyField`, `IntField`, `FloatField`, `StringField`,
  `DatetimeField` and `SortedField`: save (with `update_fields=` and
  `migrate_key=`), get, delete, exists, increment, filter, `Q`, order_by,
  limit, count. The pool (one `psycopg_pool.ConnectionPool` per (DSN, pid),
  lazy, `max_size` 4, TD-3), the schema compiler with `popoto_schema`, and
  `bind()` refusal. `Model`/`Query` public bodies dispatch to `get_backend()`.
- **Exit criteria.**
  - Gates (a) and (b) on the files below, and a Redis command trace of them
    that matches `main` byte for byte (the POC's #751 serializer-hook method).
  - `[PG-only]`: the engine columns; the outage contract (Postgres unreachable
    raises `BackendUnavailableError`, the health record counts dropped writes,
    ERROR is logged once per window); `import popoto` with `psycopg` blocked.
  - `Model.save()` p50 ≤ 2x Redis and `filter+hydrate` p50 ≤ 1x after
    `ANALYZE`, via the archive's `scripts/bench_backend_seam.py` ported to v2
    (environment stated).
- **Files (gate b).** `test_model_exists.py`,
  `test_get_many.py`, `test_get_or_create.py`, `test_bulk_operations.py`,
  `test_model_equality.py`, `test_auto_timestamps.py`, `test_q_objects.py`,
  `test_meta_order_by.py`, `test_sorted_field_ordering.py`,
  `test_sorted_time_field.py`, `test_atomic_increment.py`, `test_to_dict.py`,
  `test_datetime_tzinfo_round_trip.py`, `test_client_side_filter.py`,
  `test_delete_all.py`, `test_model_partial_load.py`, `test_key_fields.py`.
  `test_queries.py`, `test_sortedfield.py`, `test_field_types.py` and
  `test_query_results.py` have zero `def test_`; each is first converted to
  test functions in its own no-logic-change PR.

**M1.1: plain-field breadth.** Out of strict order because it follows the M1
compiler cheaply, and M2 (Valor's `DictField`, `tags=` scoping) and M4 need it. Scope:
`IndexedField`/`UniqueField`, `TagField`, `Relationship`, the collection
fields, `DateField`/`TimeField`, `Meta.indexes`, the unique-conflict text.
**Files (gate b):** `test_indexed_fields.py`,
`test_issue_534_indexed_field_encoders.py`, `test_tag_field.py`,
`test_relationship_edge_cases.py`, `test_relationship_sample.py`,
`test_meta_indexes.py`, `test_field_defaults_roundtrip.py`,
`test_list_field_capped.py`, `test_immutable_keys.py`.

### M2: Valor's memory slice (#758 spike-1 order)

- **Scope: existing features.**
  - `DecayingSortedField` (`partition_by`, `base_score_field`), `touch`,
    `top_by_decay`, and confidence-modulated `rank_decayed` over typed columns;
    `ConfidenceField` as one capped-mean `UPDATE … RETURNING`; `BM25Field` on
    `(scope, term, _pk, tf)` postings with corpus-wide statistics (#758 D4).
  - `EmbeddingField` on `vector(d)`: exact at or below
    `Defaults.PG_VECTOR_EXACT_MAX` (ordering by `(v <=> $q) + 0`), HNSW with
    `iterative_scan = relaxed_order` above it, and a recall guard that re-runs
    the arm exactly when HNSW returns short (#758 D5).
  - `membership_*` for `ExistenceFilter` and `FrequencySketch`;
    `AccessTrackerMixin` staged-read columns; `WriteFilterMixin` (priority tier
    a no-op); all five `ObservationProtocol` outcomes in one PK-ordered
    transaction (#758 D8); `rank_composite` for the arms `assess` probes;
    `fuse` over `RecordId` lists.
  - `ContextAssembler` at #758 D8's integration points: `_pull_path_hybrid`
    (`recipes/context_assembler.py:2406`), weighted by `_fusion_weights`
    (`:271`), with its zero-signal fallback to `_pull_path_composite`
    (`:2307`); `assess` (`:2767`); and `_post_effects` (`:2634`), whose
    Postgres branch runs **before** `pipeline = batch()` (`:2641`) as bulk
    stage and suppress calls in one transaction.
- **Scope: `[PG-only]`** (§1.1): `recall()`, composable scoping, per-scope
  BM25 statistics, `top_by_relevance`, the embedding backfill,
  `_migrated_from`/`_estimated_fields`, and a compiler test pinning the DDL for
  Valor's `Memory`, which #756 targets.
- **Exit criteria.**
  - Gates (a) and (b) on the files below.
    `ContextAssembler(retrieval_mode="auto")` returns the same ranked keys on
    both legs for the retrieval-quality fixtures; BM25 scores and confidence
    after an identical signal sequence match the Redis oracle to 1e-9.
  - `rank_decayed` with base + confidence at N=2000: Postgres p50 ≤ Redis p50
    (the POC measured 5.4x on bytea; WS4 §4 predicts "the 0.2x case").
  - `[PG-only]`: with the Redis client patched to raise, a Postgres-bound
    `assemble()` plus `on_context_used()` completes; a first save on a fresh
    database creates the schema with no CLI step; a sleeping provider cannot
    push `save()` past the backfill budget; `recall` on a 20k-row, 1536-d
    corpus is ≤ 15 ms p95 at 5% scope and ≤ 60 ms p95 at 60% (#758,
    environment stated).
- **Files (gate b).** `test_decaying_sorted_field.py`,
  `test_top_by_decay_autodetect.py`, `test_decay_rank_seam.py`,
  `test_confidence_field.py`, `test_confidence_modulated_decay.py`,
  `test_bm25_field.py`, `test_embedding_field.py`, `test_embedding_field_gc.py`,
  `test_existence_filter.py`, `test_access_tracker.py`, `test_write_filter.py`,
  `test_observation_protocol.py`, `test_composite_score_query.py`,
  `test_rrf_fusion.py`, `test_fusion_weights.py`, `test_hybrid_retrieval.py`,
  `test_context_assembler.py`, `test_context_assembler_hybrid.py`,
  `test_context_assembler_token_budget.py`,
  `test_retrieval_quality_regression.py`.

### M3: the memory core Valor does not use

- **Scope.** `ValidityField` on `tstzrange` and `SupersessionProtocol` in all
  modes, with the four typed errors and `chain`, porting the POC's 17-row
  validity exclusion rule and `popoto_supersede` phase order and error tokens.
  Lock order: one `(model, field)` advisory lock, then `FOR UPDATE` in `_pk`
  order. Partitioned confidence; the `semantic_search`/`keyword_search` APIs.
- **Exit criteria.** Gates (a) and (b) on the files below. The POC's
  crossing-chains interleaving (`tests/conformance/test_validity.py::TestConcurrency`,
  archive), re-expressed at model level, passes ten times. `rank_decayed` with
  a validity gate at N=2000: Postgres p50 ≤ Redis p50.
- **Files (gate b).** `test_validity_field.py` (non-Lua tests),
  `test_partitioned_confidence.py`, `test_semantic_search.py`.

### M4: graph, the remaining recipes and mixins

- **Scope.** Groups F and H: `graph_*`, `CoOccurrenceField`, `graph_traversal`,
  and `rank_composite`'s `co_occurrence_boost` arm. `field_call` adapters for
  `idle_seconds` and question-queue claim/deliver/release (`FOR UPDATE SKIP
  LOCKED`); `NeverRecordMixin`, `AppendOnlyMixin`. Every remaining recipe runs
  unchanged on a Postgres-bound model.
- **Files (gates a and b).** `test_co_occurrence_field.py`, `test_graph_traversal.py`,
  `test_adaptive_assembler.py`, `test_default_memory_eviction.py`,
  `test_subconscious_memory.py`, `test_trajectory_memory.py`,
  `test_memory_lifecycle.py`, `test_provenance_journal.py`,
  `test_question_queue.py`, `test_memory_telemetry.py`, `test_view_resolver.py`,
  `test_reconciliation_m5.py`, `test_recipes_field_layer.py`.

### M5: the remainder

- **Scope.** TTL: `_expires_at`, a read filter, and an automatic reaper that
  deletes a bounded batch of expired rows after a write commits, like M2's
  backfill, so there is no cron and no manual job (#755 Q3). `popoto.batch()`
  → `transaction()` (TD-5), atomic on Postgres. `AsyncBackend` on
  `psycopg.AsyncConnection` (TD-6). `PubSub` over `LISTEN`/`NOTIFY` plus an
  events table for `EventStreamMixin`/`StreamConsumer`, or declared out of
  scope. `GeoField`/PostGIS, `CyclicDecayField`, `PredictionLedgerMixin`,
  `TDValueField`, `DataFrameField`, `ContentField`. `maintain`, and
  same-backend `transfer/`.
- **Exit criteria.** Each item is its own PR passing gates (a) and (b) on its
  files, or a documented `bind()` refusal. A TTL test asserts an expired row is
  invisible to reads before the reaper runs and gone after a later write.
- **Files (gate b).** `test_meta_ttl.py`, `test_batch.py`, `test_atomic_save.py`,
  `test_async.py`, `test_event_stream_mixin.py`, `test_stream_consumer.py`,
  `test_geo_with_distances.py`, `test_cyclic_decay_field.py`,
  `test_prediction_ledger.py`, `test_td_value_field.py`,
  `test_content_field.py`, `test_check_indexes.py`, `test_clean_indexes.py`,
  `test_transfer_roundtrip.py`, `test_transfer_key_regeneration.py`.

## 6. Carried forward from the POC

| Ref (WS4 §7 / feature doc) | Lesson | v2 disposition |
|---|---|---|
| TD-1, #747 | the phase checker is vacuous | M0(a) |
| TD-12, TD-15, TD-26, TD-40 | NUL in `text`; UoW rollback; `2**63`; `rank_decayed` raw reply | documented divergences, §1.1 |
| TD-2, #750 B1 | cross-operation deadlock is detected, not prevented | lock ordering at commit, `(model, field)` lock before row locks, `_pk`-ordered `FOR UPDATE`, typed retryable error (M2) |
| TD-3 | one connection per instance; ten threads hung the harness | `psycopg_pool.ConnectionPool`, a connection per transaction, pid check after fork (M1) |
| TD-8, #750 TD4 | `CREATE OR REPLACE FUNCTION` ×10 on every connection | `popoto_schema` fingerprint, once per process; no PL/pgSQL required by M1 (§3) |
| TD-10 | 73 `pipeline if pipeline` sites return `None` on an empty Postgres UoW | `UnitOfWork.__bool__ = True`, so the sites are inert; Postgres never hands a UoW to a Redis hook. The sweep is optional hygiene |
| TD-13 | tie order assumes `UTF8` | bootstrap refuses non-UTF8; `COLLATE "C"` on every tie-break |
| TD-16, TD-35 | stale and inert `redis_only` marks; marks without reasons | `reason=` required (M0); per-file mark audit in each milestone PR |
| TD-11, TD-14, TD-21 | `popoto_numeric` fed by nobody; pointer table conflates index and tag spaces; Query layer keeps `bytes` members | gone: typed columns, no pointer tables, `RecordId` at the seam; Redis internals unchanged |
| TD-23 | residual Redis wire changes from routing | avoided by construction (move, not rewrite); M1 trace-diff exit criterion |
| feature doc "Known deviations" | Valkey 8 and Redis 7/8 cmsgpack emit the confidence map in different key orders | Postgres stores typed columns, so the point is moot; `transfer/` and #756 must decode by key and never byte-compare payloads |
| feature doc | `power()` and finite `float8` over/underflow raise where Lua returns `inf`/`0` | clamp the exponent in the decay SQL so both legs agree; add the POC's boundary rows as conformance cases; document for `increment` |
| WS4 §4 | two benchmark rows bimodal (no `ANALYZE`) | `ANALYZE` after seeding, prepared statements, ≥3 runs per row, range reported |

## 7. Risks

| Risk | Mitigation |
|---|---|
| The Redis move is not wire-identical (the POC found #735's `SADD` and `EXPIRE`-order divergences only by tracing) | M1 trace-diff gate, #751 method |
| Ranking math drifts when decay, capped Bayesian and BM25 are reimplemented in SQL | oracle conformance with per-test numeric tolerances, plus the POC's boundary cases |
| The protocol grows past 25 | new features use a `field_call` adapter; any protocol change is its own small PR (the POC freeze rule) |
| #758 proceeds as a parallel `popoto.pg` | the single-plan proposal is posted on #755 and #758; M1 starts after #758's author confirms or the maintainer rules |
| Gate (a) erodes: Redis tests get re-marked or skipped to make a PR green | gate (a) compares against the PR's base commit; a new `redis_only`/`skip` on a previously passing test fails review; per-PR mark audit (TD-16) |
| A `[PG-only]` capability grows a Redis path by accident | each one has a test asserting `BackendCapabilityError` on a Redis-bound model |

## 8. No-Gos

Generic `bytea` tables, msgpack inside Postgres, PL/pgSQL decoders, or any
Redis-structure emulation; merging or rebasing `poc/backend-seam`; changing
Redis key layout or wire behaviour; deprecating Redis, or adding a deprecation
warning; adding new capabilities to Redis; dual-write or read-through between
backends (#756 is a one-off copy); a separate `popoto.pg.Model` base class;
reading `POSTGRES_URL`/`DATABASE_URL`; popoto-emitted declarative partitioning
or RLS; `py.typed` (unchanged policy, CLAUDE.md); a Postgres port of
`extraction/decision_log.py`, `Query.keys(catchall=True)` or `load_raw_hash`.

## 9. Questions for the architect

The ruling and #755's decisions settled the earlier questions on
reconciliation, unit-of-work failure, TTL, NUL bytes, migration tooling,
Redis-shaped statics and `ExistenceFilter` (§Direction, §1.1, M5). A custom
field that overrides hooks is refused by `bind()` on Postgres: emulating hooks
is a No-Go, and deprecating them would break Redis. Still open:

1. **`recall()`'s default BM25 statistics.** Parity keeps `BM25Field.search`
   corpus-wide; #758 D4 argues per-scope IDF is the correct statistic for a
   scoped search, and spike-4 shows it is faster. Should `recall()` default to
   per-scope once M2's parity gate is met, or stay corpus-wide with per-scope
   opt-in?
2. **A public field-compiler API.** Should custom-field authors get a
   documented Postgres compiler registration (`docs/field-authoring.md`) in
   v2, or does the contract stay internal until a second adopter asks?
3. **One central Postgres, or one per machine?** (#758 OQ1, #756.) This sets
   pool-sizing guidance, and whether `agent_id` or a machine id joins the scope.
4. **Minimum Postgres version: 16 or 18?** Unscoped BM25 is fast only with
   18's B-tree skip scan (#758 OQ3).
