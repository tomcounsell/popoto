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
regression-gated; Postgres gets typed tables, native indexes and its
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
2. **Redis is regression-gated: not deprecated, and no new Redis features are
   required.** Everything that passes on Redis today keeps passing, with the
   same wire behaviour, on today's hook, index and Lua machinery. Bug fixes are
   welcome under gate (a); it gets no deprecation warnings and no new
   capability work. How long Redis stays supported is an open,
   community-driven decision outside this plan; nothing here presumes it.
3. **Postgres is the target for new work.** Capabilities beyond today's
   feature set are Postgres-only, with no Redis implementation (§1.1, and the
   `[PG-only]` rows in §5).
4. **The seam moves up** to a model/query protocol. `poc/backend-seam` and its
   46 methods are not merged; the branch is a frozen reference archive, with
   durable parts salvaged in small PRs (M0).

### Architect decisions (2026-10-04)

The maintainer answered the four open questions; decisions 3 and 4 were made
by the coordinator under delegated authority and are reversible.

1. **One central Postgres.** All agents and machines share one database,
   scoped by agent, project and tags; **no machine id** joins the scope (§3
   Topology, #756 below, M1 outage contract).
2. **Minimum Postgres 18.** `bind()` checks `server_version_num >= 180000`; M0
   moves CI to `pgvector/pgvector:pg18`; unscoped BM25 relies on 18's B-tree skip scan.
   No 16/17 fallback.
3. **`recall()` BM25 is per-scope by default.** It is new and Postgres-only, so
   parity does not bind it; `BM25Field.search` stays corpus-wide for Redis
   parity, and corpus-wide is a `recall()` option. Direct `recall()` callers get
   the per-scope default; the Postgres hybrid path behind `ContextAssembler`
   (`_pull_path_hybrid`) passes `bm25_stats="corpus"` so it ranks identically to
   Redis, which is what M2's parity gate compares. On a shared database
   per-scope is correct: one agent's corpus must not skew another's IDF.
4. **The field-compiler API stays internal in v2**, public only when a second
   adopter asks; `docs/field-authoring.md` says hook-overriding custom fields
   are Redis-only for now.
5. **`bind()` is lazy.** Defining a model never touches the network: class
   creation runs only a pure static check of the model's field set against the
   backend's capability tables (e.g. refusing a hook-overriding custom field on
   a `Meta.backend = "postgres"` model). `bind()`, with the connect, version
   check, extension check, DDL and schema-version check, runs on the first
   backend use for that model (first query or save). Importing a model module
   therefore never dials the database, consistent with the M1 outage contract.

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
| Pool per (DSN, pid), fork-safe; default `max_size` 4, sized for one central database (§3 Topology) | #758 D6 | M1 |

**#756** stays a companion: a one-off Redis→Postgres copy for Valor and
Yudame, not dual-write, read-through or a runtime feature. It targets the DDL
M2 pins, with the column names and types M2a shipped (§3 table, and the
M2a departures under §5 M2): `double precision` epoch-second clocks named
after the field, `<f>__conf`/`__n`/`__corr`/`__contra`, and
`_access_count`/`_last_accessed`/`_staged_reads`/`_staged_at`. Under decision 1 it **merges several per-machine Redis stores into
one database**. Proposed rule, to confirm on #756: within a scope, an equal
`_pk` with an equal payload is deduplicated; a differing payload keeps the
later `_updated_at` (tie: the greater source id) and logs the loser to
`_migrated_from`. The source id lives there, never in the scope. `transfer/` on Postgres (M5) is same-backend parity, not a cutover.

**Kept from this plan where #758 differs:**

- **"Redis memory is frozen or deprecated."** Regression-gated, never deprecated: the
  ruling leaves Redis's support horizon to a later, community-driven decision.
- **Postgres >= 16 and `popoto.pg.UnavailableError`.** Overridden: the floor is
  Postgres 18 (decision 2) and the outage error is `BackendUnavailableError`
  (M1), a popoto-level type shared by both backends.
- **`migrate_key` as `UPDATE ... SET _pk` with a cascade.** On Postgres
  `migrate_key=` **raises** (as #758 proposed) in v2, rather than cascading the
  key change through postings, edges and membership companions. A change to a
  scoped model's scope column is supported: it moves that row's BM25 posting
  rows in the same transaction.
- **A separate `popoto.pg.Model` base class.** `Meta.backend = "postgres"` on
  the one `Model` instead: a parallel base class forks the model and query
  surface, so the parity gate could not run the existing tests against it.
- **Valor-unused features "rejected, not deferred".** Ordered later (M3–M5),
  because popoto is a published library and parity covers its feature set.
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
- `main` @ `57c29ebf` has 31 named `*_LUA` scripts (25 in `fields/`, 1 in `models/base.py`,
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
| `Model.save()` (`update_fields`, `migrate_key`, `ignore_errors`, `skip_auto_now`) | msgpack `HSET` + `$Class:` set + per-field `on_save` hooks + `INDEX_SWAP_LUA` | `save` | `INSERT … ON CONFLICT (_pk) DO UPDATE SET <changed cols>`; `migrate_key` raises `BackendCapabilityError` on Postgres in v2; a scope-column change moves the row's BM25 postings in the same txn | M1 |
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
| `Relationship` (lazy key string, reverse lookups, `sample_related_keys`) | stored target key string + `$RelationshipF:` set + `SRANDMEMBER` | `select`, `load` | `text` column holding the target `_pk` + B-tree; `ORDER BY random() LIMIT n` via an `OrderTerm` `Random` in `QueryPlan` (`sample_related_keys`) | M1.1 |
| `DecayingSortedField` / `top_by_decay` (`base_score_field`, `as_of`) | `DECAY_SCORE_LUA` over a ZSET | `rank_decayed` | `ORDER BY` decay expression over typed columns | M2 |
| `Model.touch` | `ZADD` new timestamp | `touch` | `UPDATE … SET f_at = $at` | M2 |
| `ConfidenceField.update_confidence`/`get_confidence`, partitioned confidence | `CAPPED_BAYESIAN_UPDATE_LUA` on a companion hash | `update_confidence`; reads via `load` | four typed columns, one `UPDATE … RETURNING` | M2 (partitioned: M3) |
| Confidence-modulated decay | `DECAY_SCORE_LUA` reads the confidence hash per member | `rank_decayed(confidence_field=)` | one expression over the confidence column | M2 |
| `ValidityField` `__as_of` / `__current`, `resolve_excluded_keys` | three ZSETs + exclusion rule | `select` (`Predicate` op `valid_at`) | `tstzrange` column, GiST, `@>` | M3 |
| `SupersessionProtocol.supersede`/`invalidate`/`save_and_*` | `SUPERSEDE_LUA` (validate-then-mutate) + open-pointer STRING | `supersede` | one txn: lock, validate, `UPDATE` upper bound, insert; partial unique index on the open identity | M3 |
| `superseded_by`/`supersedes`/`chain` | `HGET` on the chain hashes | `chain` | `WITH RECURSIVE` over `f_supersedes` | M3 |
| `EmbeddingField`, `semantic_search`, `load_embeddings` | `.npy` files + in-process numpy cosine | `vector_search` | pgvector `vector(d)`, `<=>`; HNSW past a size threshold | M2 (`semantic_search`: M3) |
| `BM25Field.search`, `keyword_search` | `BM25_SAVE/DELETE/SEARCH_LUA` postings | `keyword_search` | scope-keyed postings table, BM25 in SQL (#758 D4); corpus-wide statistics, as on Redis (`recall()` defaults to per-scope) | M2 (`keyword_search`: M3) |
| `composite_score` (`co_occurrence_boost`, `similarity_boost`, `temperature`) | temp ZSETs + `ZUNIONSTORE` (POC `native()` sites 6–12) | `rank_composite` | one `SELECT` with a weighted sum over columns and CTE arms | M2, for the decay and confidence arms `assess` uses (`co_occurrence_boost`: M4) |
| `fuse` (RRF), `ContextAssembler` hybrid path | Python RRF over `(redis_key, score)` lists | above the seam (lists of `(RecordId, score)`) | unchanged; SQL push-down is an optimisation | M2 |
| `ExistenceFilter.might_exist`/`_batch`/`definitely_missing`, `FrequencySketch` | `BLOOM_*_LUA`, `CMS_*_LUA` | `membership_add`, `membership_query` | `(field, token)` / `(field, token, count)` tables (exact; §1.1) | M2 |
| `CoOccurrenceField.link`/`strengthen`/`unlink`/`weaken_all` | `LINK_WITH_PRUNE_LUA`, `STRENGTHEN_CLAMP_LUA`, `WEAKEN_ALL_LUA` | `graph_update` | edge table upsert; prune by a window rank | M4 |
| `CoOccurrenceField.propagate`, `recipes/graph_traversal.traverse` | `PROPAGATE_BFS_LUA`, `SRANDMEMBER` | `graph_expand` | `WITH RECURSIVE` with depth/threshold | M4 |
| `CoOccurrenceField.get_linked` | edge-set read | `graph_expand` (`depth=1`, no threshold; weights come back as scores) | one indexed read of `<model>__<f>_edges` by `src` | M4 |
| `AccessTrackerMixin` (`on_read`, `confirm_access`, `discard_staged_access`) | `CONFIRM_ACCESS_LUA`, meta hashes | `field_call` | `_access_count bigint`, `_last_accessed double precision` + `_staged_reads bigint`, `_staged_at double precision` that expire by comparison (#758 D2; M2a) | M2 |
| `WriteFilterMixin`, `ObservationProtocol`, `NeverRecordMixin`, `AppendOnlyMixin` | Python above storage; `$WF:` priority set | above the seam, plus `field_call` for the priority tier | unchanged | M2 (`NeverRecordMixin`, `AppendOnlyMixin`: M4) |
| Recipes: `ContextAssembler`, `AdaptiveAssembler`, `DefaultMemory`, `SubconsciousMemory`, `TrajectoryMemory`, `MemoryLifecycle`, `ProvenanceJournal`, `TelemetryRecorder`, `BeliefSheetResolver`, `reconciliation`, `policy_cache` | field/model methods (#630) | none new | follows from M1–M3 | M4 (`ContextAssembler`: M2) |
| `recipes/question_queue.py` | `_DELIVER/_CLAIM/_RELEASE_LUA` | `field_call` (or a model-level `claim`) | `SELECT … FOR UPDATE SKIP LOCKED` | M4 |
| `Model.idle_seconds` (`memory_lifecycle`) | `OBJECT IDLETIME` | `field_call` | `extract(epoch from now()) - _last_accessed` (epoch seconds since M2a) | M4 |
| `Meta.ttl`, `save(ttl=/expire_at=)` | `EXPIRE`/`EXPIREAT` | `save(expiry=)` | `_expires_at` column + read filter + automatic reaper on writes (#755 Q3) | M5 |
| `popoto.batch()`, `pipeline=` kwarg everywhere | `GuardedPipeline` (`MULTI`/`EXEC`) | `transaction` | one DB transaction | M5 |
| `async_*` twins, `get_async_redis_db` | `redis.asyncio` | `AsyncBackend` twin | `psycopg.AsyncConnection` pool | M5 |
| `Publisher`/`Subscriber`, `EventStreamMixin`, `StreamConsumer` | `PUBLISH`/`SUBSCRIBE`, `XADD`/`XREADGROUP` | separate `PubSub` protocol (not counted) | `LISTEN`/`NOTIFY` + an events table with `bigserial` | M5 |
| `GeoField` (`_latitude`/`_longitude`/`_radius` filters, with distances) | `GEOADD`/`GEOSEARCH` (`WITHDIST`) | `select` (op `within`, `QueryPlan.compute`) | PostGIS `geography(Point)` + GiST; `ST_Distance` returned as a computed column on `Row` (capability) | M5 |
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
| Per-scope BM25 statistics as `recall`'s default (`bm25_stats="scope"`; `"corpus"` opts out) | #758 D4, decision 3 | M2 |
| `Model.top_by_relevance(scope, limit)`, decay×confidence in SQL (replaces Valor's raw `ZREVRANGE`) | #758 D8 | M2 |
| Piggybacked embedding backfill after a successful save (`embedding_model`, `embedded_hash` columns; bounded batch and wall-clock budget) | #758 D7 | M2 |
| `_migrated_from` / `_estimated_fields` engine columns, as #756's import contract | #758 D3 | M2 |
| Engine-owned `_created_at` / `_updated_at` on every table | #758 D7 | M1 |
| Backend health record (`ok`, `last_ok_at`, `consecutive_failures`, `dropped_writes`), logged at ERROR once per outage window | #758 D6 | M1 |

**Documented divergences.** Here Postgres is stricter than Redis. Redis
behaviour does not change to match (Direction 2).

| Behaviour | Redis (unchanged) | Postgres | Source |
|---|---|---|---|
| `save(migrate_key=)` | rewrites the key and its index entries | raises `BackendCapabilityError` in v2 | #758 |
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
                  compute: tuple[ComputedCol, ...] = ()   # e.g. geo distance, surfaced on Row
Row = Mapping[str, Any]                      # decoded Python values; always carries "_id": RecordId
Scored = list[tuple[RecordId, float]]        # every ranking method returns typed pairs
```

`ModelSpec` is built once by `ModelBase` from `_meta`. `Op` is the closed set
of §1 suffixes plus `valid_at` and `within`. `OrderTerm` is a field with a
direction, or `Random` (for `sample_related_keys`); `ComputedCol` is a named
backend-computed value (geo distance) that `Row` carries beside the fields. Values cross the protocol
**decoded**: no msgpack, key strings or index names.

### Methods (24)

```python
class Backend(Protocol):
    # A. lifecycle (3)
    def bind(self, spec: ModelSpec) -> Capabilities: ...       # LAZY: first backend use per model; connect, version, DDL, schema check
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
`BackendCapabilityError`, and a field missing an adapter is refused by the
static spec check at class creation (below), not at `bind()`.

**Declaration versus bind.** Two steps, never conflated. (1) Class creation
runs `validate_spec(spec, backend_name)`, a pure function over the backend's
static capability tables and field-adapter registry: it refuses unsupported
fields (TD-9) and needs no network, so a model module imports with the database
down. (2) `bind()` is lazy: it runs on the first backend use of a model (first
query or save), memoised per (backend, model), and does everything that needs a
server: connect, version, `pg_extension`, DDL and the schema-version check.

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
| `bind` (lazy, first use) | no-op; returns the full capability set | require `server_version_num >= 180000` (else a clear error naming the version found); check `pg_extension`; compile `ModelSpec` → DDL; check the `popoto_schema` fingerprint and version record (§3). Field refusal is the static `validate_spec`, earlier |
| `transaction` | `batch()` → `GuardedPipeline` (`MULTI`/`EXEC`) | pool connection, one `READ COMMITTED` transaction, atomic on failure (§1.1); rows locked in PK order; a single statement outside a unit of work retries `DeadlockDetected`/`SerializationFailure` up to `Defaults.PG_TRANSACTION_RETRIES` times with jitter, then raises `BackendRetryableError` chained from the driver error; inside a caller-owned `transaction()` (a statement or its `COMMIT`) the unit rolls back and `BackendRetryableError` is raised at once, never retried, because only the caller can rerun its block; 40003 (completion unknown) is not retryable and follows the #769 dead-connection rule (TD-2, #758 D2; M2a, #773) |
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
| `keyword_search` | `BM25Field.search` (`BM25_SEARCH_LUA`) | BM25 over `(scope, term, _pk, tf)` postings; corpus-wide N/avgdl as on Redis; `recall` uses per-scope N/avgdl by default (#758 D4, decision 3) |
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
| `DecayingSortedField` | `<f> double precision` (epoch seconds, the Redis sorted-set score; M2a departure, §5 M2) | B-tree `(partition cols…, <f>, _pk COLLATE "C")` |
| `ConfidenceField` | `<f> double precision` (the model attribute, as the Redis hash keeps it) + state `<f>__conf double precision`, `<f>__n bigint`, `<f>__corr bigint`, `<f>__contra bigint` (`NULL` = the seed; M2a) | none (M2a departure, §5 M2) |
| `ValidityField` | `<f>` (the declared value) + `<f>__valid_from`, `<f>__invalid_at`, `<f>__ingested_at` `double precision` (`'Infinity'` = open), `<f>__supersedes`, `<f>__superseded_by` `text`; companion `<table>__<f>__open (digest, member)` (M3 departure, §5 M3) | B-tree on `<f>__valid_from` and `<f>__invalid_at`; the companion's `member` references `_pk` `ON DELETE CASCADE` |
| `EmbeddingField` | `<f> bigint` (dimensions) + `<f>__vec vector(d)` (`d` from provider), `<f>__model`, `<f>__hash`; companion `<model>__<f>__vec (_pk, scope, v vector(d))`, `v` `STORAGE PLAIN` (M2b departure, §5 M2) | HNSW `vector_cosine_ops` on `<f>__vec` (needs pgvector); `(scope)` on the companion |
| `BM25Field` | `<f>_len int` + companion `<model>__<f>_postings(scope, term, _pk, tf)` | PK `(scope, term, _pk)` |
| `CoOccurrenceField` | companion `<model>__<f>_edges(src, dst, weight)` | PK `(src, dst)`, B-tree `(dst)` |
| `ExistenceFilter` / `FrequencySketch` | companion `(token)` / `(token, count)` | PK |
| `ContentField` | `text` (or a `ContentStore` reference, as today) | none |
| `CyclicDecayField` / `TDValueField` | `jsonb` cycles + pressure / `double precision` | none |
| `AccessTrackerMixin` | `_access_count bigint`, `_last_accessed double precision`, `_staged_reads bigint`, `_staged_at double precision` (#758 D2; epoch seconds, M2a) | none (M2a departure, §5 M2) |
| `DataFrameField` | `bytea` | none |
| custom `Field` subclass with no compiler | if it overrides no hook: column from `type=`; otherwise `bind()` refuses | decision 4 |

### Migrations

DDL ownership follows TD-8 and #758 D2, with no manual step in normal use.
Popoto keeps a deterministic fingerprint per model in `popoto_schema(model,
fingerprint, ddl, applied_at)`. On first use per **process** (that is, at the lazy `bind()`), under
`pg_advisory_xact_lock(hashtext('popoto:ddl:'||table))`, a **missing table** is
created and an **additive** change (nullable column, index, companion) is
applied. A **destructive or ambiguous** change (drop, retype, vector dimension,
key-field change) raises `SchemaDriftError` with the diff; the operator runs
`python -m popoto.schema migrate <dotted.Model>` (`--dry-run` prints the DDL).
`POPOTO_SCHEMA_AUTO=0` disables even create/additive. **Version skew on a
shared schema:** `popoto_schema` also records the writing popoto version and
schema-format version. An older client that finds a fingerprint it did not
produce, or a newer format version, raises `SchemaDriftError` rather than
writing; it never downgrades or reconciles. Destructive migrations are
operator-run (`python -m popoto.schema migrate`), never automatic, so one
client cannot alter the schema under the rest of the fleet. The `migrations.py`
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
- **No machine id** (decision 1): scope is agent, project and tags only.
- **Declarative partitioning and RLS** are operator options; popoto emits
  neither.

### Topology: one central Postgres

The pool is mandatory: one `psycopg_pool.ConnectionPool` per (DSN, pid),
`max_size` 4 by default. Demand is `processes x max_size` and must stay under
`max_connections`; beyond a few dozen clients, **run a server-side pooler such
as PgBouncer in transaction mode** and point the DSN at it. That constrains
popoto:

- **Advisory locks are `pg_advisory_xact_lock` only** (DDL, `supersede`, and
  the record-key lock every record writer takes, M2b).
- **No session state:** `prepare_threshold=None`, `SET LOCAL`, no temp tables.
- **`LISTEN`/`NOTIFY` needs a session connection:** M5's `PubSub` bypasses the
  transaction-pooled DSN.
- **Network failure is routine:** timeouts set; `BackendUnavailableError`.
- **Ownership.** HA, failover and backups of the central database belong to the
  operator, not popoto; popoto's part is the outage contract (M1) and the risk
  mitigations in §7.

### Extensions and capabilities

The lazy `bind()` checks `pg_extension` once per process: an `EmbeddingField`
without `vector`, or a `GeoField` without `postgis`, raises there, on the
model's first backend use (declaration cannot know, because it never connects). Bootstrap
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
  meaning. Importing popoto, or defining a model, never dials Postgres or needs
  `psycopg` (M1; decision 5).
- **Tests.** From M1, the pytest plugin pins `redis` for the session whenever
  it opted in (the pin needs `get_backend()`, which arrives in M1). The
  `backend` fixture selects Postgres per test, discards the memoised `bind()`
  state of the models under test so the next first use rebinds them lazily
  against that test's schema, and on teardown restores the *previous* binding
  and discards it again (salvaged POC rule, `docs/testing.md`). Module-level
  test models therefore need no re-declaration.

## 5. Milestones

Each milestone is one or more PRs against `main`, each independently
shippable.

### Gates

| Gate | Applies to | Check | Passes when |
|---|---|---|---|
| **(a) Redis regression** | every PR, M0–M5 | full suite on the Redis and Valkey jobs in `tests.yml`; M1 wire-trace diff wherever a Redis path moved | every test that passes on Redis at the PR's base commit still passes, with none deleted, skipped, loosened or re-marked to get there. Redis is regression-gated: bug fixes are welcome, no new Redis features are required, and no PR deprecates it |
| **(b) Postgres parity** | existing-feature rows, M1–M5 | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres pytest -m conformance` in the `postgres` job | the milestone's listed existing test files pass on the Postgres leg from the **same test code**; a Redis-key or §1.1-divergence assertion is `redis_only` with a reason, never deleted |
| `[PG-only]` tests | §1.1 new capabilities | `pytest tests/postgres/` in the `postgres` job | the capability's own tests pass; there is no Redis leg and no Redis code path is added for one |

Per-leg counts state their attribution rule (TD-33). Gate (a) permits the §2
*move* of Redis code, which changes neither what Redis does nor what it is
sent. M2 and M3 follow Valor's usage (#758 spike-1).

| Milestone | Existing features: gates (a) + (b) | `[PG-only]` additions: gate (a) + own tests |
|---|---|---|
| M0 | salvage harness, markers, CI job, #747 fix (gate (a) only; no Postgres code; no change for users who do not opt in) | — |
| M1 | plain models: records, query, `Q`, ordering, count, increment; pool, schema compiler, lazy `bind()`, `get_backend()` and the Redis session pin, the `postgres` extra, atomic transaction | engine `_created_at`/`_updated_at`; health record + `BackendUnavailableError` |
| M1.1 | indexed/unique/tag/relationship/collection fields, `Meta.indexes` | — |
| M2 | **Valor's slice:** decay + `touch`, confidence (+ modulation), BM25, embeddings, `ExistenceFilter`/`FrequencySketch`, `AccessTrackerMixin`, `WriteFilterMixin`, `ObservationProtocol`, `fuse`, `composite_score` (decay/confidence arms), `ContextAssembler` hybrid + `assess` + `_post_effects` | `recall()`; composable scoping; per-scope BM25 stats; `top_by_relevance`; embedding backfill; `_migrated_from`/`_estimated_fields`; Valor `Memory` DDL pinned for #756 |
| M3 | memory core Valor does not use: `ValidityField` + supersession + `chain`, partitioned confidence, `semantic_search`/`keyword_search` | — |
| M4 | graph (`CoOccurrenceField`, `graph_traversal`), remaining recipes and mixins, question queue, `idle_seconds` | — |
| M5 | TTL (`_expires_at` + automatic reaper), `batch()`, async, pub/sub, geo, long-tail fields, `maintain`, `transfer/` | — |

### M0: salvage to `main`, zero behaviour change for users

- **Scope.** Three PRs, each cherry-picked or ported from `efda14a3`, not
  rebased. **(a)** The #747 fix: partition on the last or column-0 marker,
  plus a negative test that plants a write in the validation phase. **(b)**
  `pytest_plugin.py`: `conformance`/`redis_only` markers, with `reason=`
  *required* (13 POC marks lacked one); `popoto_conformance_backends` ini/env;
  Postgres schema isolation (`popoto_test_<hex>`, refusing `public`, `popoto`
  and a db-less URL). This edits the shipped `src/popoto/pytest_plugin.py`, but
  the markers and ini keys are inert unless a project uses them, so users who
  do not opt in see no change. The Redis session pin is **deferred to M1**: it
  presupposes `get_backend()`, which does not exist until then. **(c)** The `postgres` CI job in
  `tests.yml`: a `pgvector/pgvector` service plus Redis, `REDIS_URL` pinned to
  DB 15 per #639. The POC's `pytest (Postgres)` job uses `postgres:16`
  (`poc/backend-seam`, `.github/workflows/tests.yml`); the salvage bumps it to
  the Postgres 18 image (`pgvector/pgvector:pg18`), the minimum version. The
  `postgres` extra is **not** added here (it moves to M1, where code uses it).
- **Not salvaged.** `backends/` and the 46-method protocol; the
  `tests/conformance/test_{records,indexes,swaps,decay,validity}.py` files test
  that protocol and stay in the archive.
- **Exit criteria.** Gate (a). Harness self-tests in the style of the archive's
  `test_harness.py`/`test_postgres_bootstrap.py` pass in the `postgres` job,
  and the Postgres model leg skips with a named reason. The phase checker fails
  on the planted write; the ratchet is not above its ceiling.
- **Tests.** `tests/test_pytest_plugin.py`, `tests/test_validity_field.py::TestSupersedeLuaPhaseSplit`, `tests/test_ci_workflow_redis_url.py`.

### M1: a plain-model vertical slice on typed tables

- **Scope.** `backends/{__init__,types}.py` (protocol, selection, `ModelSpec`)
  and `RedisBackend` (the §2 move). `PostgresBackend` groups A–C for
  `KeyField`/`AutoKeyField`, `IntField`, `FloatField`, `StringField`,
  `DatetimeField` and `SortedField`: save (with `update_fields=` and
  `migrate_key=`), get, delete, exists, increment, filter, `Q`, order_by,
  limit, count. The pool (one `psycopg_pool.ConnectionPool` per (DSN, pid),
  lazy, `max_size` 4, TD-3, sized for one central database), the schema
  compiler with `popoto_schema`, and the lazy `bind()` (first backend use,
  including the `server_version_num >= 180000` check) and the static
  `validate_spec` refusal at class creation; `get_backend()` and the pytest
  plugin's Redis session pin deferred from M0; and the `postgres` extra
  (`psycopg[binary,pool]`, `pgvector`) with `check_lock_imports.py` and
  `uv.lock`. `migrate_key=` on a Postgres model raises
  `BackendCapabilityError`. `Model`/`Query` public bodies dispatch to `get_backend()`.
- **Exit criteria.**
  - Gates (a) and (b) on the files below, and a Redis command trace of them
    that matches `main` byte for byte (the POC's #751 serializer-hook method).
  - `[PG-only]`: the engine columns; the outage contract (Postgres unreachable
    raises `BackendUnavailableError`, the health record counts dropped writes,
    ERROR is logged once per window; connect and statement timeouts tested);
    first use of a model against Postgres below 18 raises a clear error from
    `bind()`; `import popoto` with `psycopg` blocked; defining a
    `Meta.backend = "postgres"` model with the database unreachable succeeds,
    and the first query or save raises `BackendUnavailableError`;
    `tests/test_check_lock_imports.py` covers the extra.
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
    `(scope, term, _pk, tf)` postings with corpus-wide statistics for
    `BM25Field.search` (#758 D4); a scope change on a scoped model moves its
    posting rows in the same transaction.
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
  Valor's `Memory`, which #756 targets. That pin uses the column names and
  types M2a shipped (below), not the ones this plan first proposed.
- **M2a as shipped (#773): departures from this plan, recorded.** M2a is
  decay + `touch`, confidence (+ modulation), `AccessTrackerMixin`,
  `WriteFilterMixin`, `ObservationProtocol`, `RecallProposal` and
  `composite_score`; search (BM25, embeddings) is M2b.
  - **Clocks are `double precision` epoch seconds, not `timestamptz`.**
    `timestamptz` keeps microseconds only, so a clock round-tripped through
    it is not the Redis sorted-set score, and the decay score computed from
    it is not bit-identical to `DECAY_SCORE_LUA`'s. The decay clock is the
    field's own column `<f>` (not `<f>_at`); `_last_accessed` and
    `_staged_at` are `double precision` too. It does not constrain M3:
    `tstzrange` lives on `ValidityField`'s own column, and the gate only
    needs `to_timestamp(as_of)`.
  - **Confidence state columns are `<f>__conf`, `<f>__n`, `<f>__corr`,
    `<f>__contra`** (double underscore, the M1 `<f>__utcoff` convention;
    counts `bigint`), beside `<f>`, which keeps the model attribute as the
    Redis hash does. `NULL` state is the seed, so a save never writes it and
    a re-save cannot reset it.
  - **No state-column indexes** (none on `<f>`, `<f>__conf`,
    `_last_accessed`). Each would make every `update_confidence` and
    `confirm_access` a non-HOT update, and no M2a query filters or orders by
    one alone. Measured in the #773 review (`EXPLAIN ANALYZE`, N=20k, base +
    confidence, n=10, PostgreSQL 18.6, M1 Max): unpartitioned, a seq scan
    plus top-N heapsort, 13.7 ms execution, p50 14.9 ms, the clamped
    subplans never executed; a 5% partition, a bitmap scan on the decay
    B-tree, 0.86 ms, p50 2.4 ms. With the `OFFSET 0` fence the patch added
    (the score is evaluated once per row instead of once per sort key), the
    same shapes measured p50 10.1 ms and 1.6 ms in the patch's run (14.7 ms
    and 2.6 ms unfenced, same run and machine). Revisit if a later query
    filters on a state column.
  - **No `(model, field)` advisory lock for single-row updates.** A
    confidence update is one `UPDATE … RETURNING`, a `touch` one `UPDATE`,
    and `on_context_used` locks its batch `FOR UPDATE` in `_pk` order before
    any effect: row locks alone serialize them. Review measurement: 6
    processes × 300 mixed calls on 5 shared rows, 0 lost updates. The
    advisory lock (TD-2) is deferred to M3's `supersede`, the one operation
    that validates across rows. (M2b then put a *record-key* advisory lock
    in front of every one of these row locks: see the M2b departures.) A
    cross-transaction deadlock is still possible (a caller's own
    `transaction()` taking rows in another order); it surfaces as `BackendRetryableError` (§2 `transaction`).
  - **`RecallProposal` is one engine table per schema**,
    `popoto_recall_proposal (model, part, member, surfaced_at)` keyed
    `(model, part, member)`, created on first use under
    `pg_advisory_xact_lock` like `popoto_schema`, not a companion per model.
    It carries the model tables' model-name-collision caveat on a shared
    schema.
- **M2b as shipped (#774): departures from this plan, recorded.** M2b is
  BM25, embeddings, `ExistenceFilter`/`FrequencySketch`, `fuse`, `recall()`,
  `top_by_relevance`, the backfill and the #756 engine columns; it merged
  after M2a, so `recall()`'s decay arm, `top_by_relevance` and
  `composite_score(similarity_boost=)` use M2a's columns (`<f>` clock,
  base-score column, `<f>__conf`).
  - **`recall()` on the HNSW path is three statements, not one:** an
    index-only count (which picks the path), the HNSW arm, then the fused
    statement. The HNSW arm needs statement-wide `SET LOCAL` planner
    settings (`enable_seqscan`/`enable_bitmapscan`/`enable_sort` off): at 20k
    1536-d rows and a 60% scope the planner otherwise chose a bitmap heap
    scan plus top-N sort (77 ms) over the index (~5 ms), and the same
    settings in a combined statement pushed the BM25 arm onto a full scan of
    the postings' `_pk` index (300 ms). Its ranking joins the fused
    statement as a key list that is re-filtered by the scope and filters
    there, so a record that leaves the scope in between is not returned
    (#774 review). The exact path is two statements (the count, then
    everything else), and the decay arm adds none.
  - **A narrow vector companion, `<model>__<f>__vec (_pk, scope, v)`,
    `STORAGE PLAIN`** (when `d` ≤ 1,900, so a row fits a page), written in
    the save statement and by the backfill, cascaded on delete, read by the
    exact path and the recall guard. The record table keeps `<f>__vec` for
    the HNSW index. Reason: in the record table a 1536-d vector is TOASTed,
    and de-TOASTing a 5% scope's 1,000 vectors put the 5% `recall` p95 at
    13-25 ms against the 15 ms bar (#774 review). Cost: a second copy of
    each vector. `vector_search(where=<exactly the scope>)` filters the
    narrow table on its own `scope` column too, as `recall()` does (a
    non-empty `str` `EXACT` on every scope column; anything else keeps the
    `_pk IN` record-table filter).
  - **The recall benchmark after the narrow table** (`scripts/bench_recall.py`,
    20k × 1536, Apple M1 Max, PostgreSQL 18.6, pgvector 0.8.7, a shared
    machine; bars: 15 ms p95 at 5%, 60 ms p95 at 60%).
    - #774 patch, six invocations × 3 runs × 200 at load 4.7-7.5: 5% within
      the bar in **13 of 18** runs (p95 6.5-10.8 ms; the five misses, 16.4-20.2
      ms, fell in windows where every statement, the index-only count
      included, doubled); 60% in 18 of 18 (p95 16.8-46.3 ms).
    - #774 follow-up review, plain bench model, 3 invocations × 3 runs at
      load 5.6-6.6: **9 of 9** at 5% (p95 6.4-6.7 ms, p50 5.6-5.8 ms) and 9 of
      9 at 60% (p95 16.9-22.1 ms).
    - The decay arm (a memory-shaped model with `DecayingSortedField` +
      `ConfidenceField`, so `recall()` fuses the arm): about **+9 ms p50 at
      60%** (18 → 28 ms; p95 31-44 ms, under the 60 ms bar); at 5% plain and
      decay measured the same p50 in one window (~12-13 ms, a slow one), so
      no cost there was separable from contention. The fused statement runs
      in 6-11 ms server-side.
    - The second #774 follow-up, base `2d95561a` and the patch interleaved on
      one kept corpus (3 invocations each, load 2.2-2.7, a slow window
      against the review's 5.6-5.8 ms p50 on its own corpus): `recall()` 5% p50
      12.9-13.5 ms base, 12.1-13.4 ms patch, p95 17.4-18.4 ms on both, so
      over the bar in this window on both and unchanged by the patch; 60%
      p95 29-34 ms on both. `vector_search(where=scope)` at 5%: p50
      11.2-12.5 ms (`_pk IN`, base) → 6.8-9.4 ms (narrow scope), below
      `recall()`'s 12.1-13.4 ms in the same window (the review measured
      9.6 ms against `recall()`'s 5.7 ms).
  - **A record-key advisory lock in front of every record writer.**
    `pg_advisory_xact_lock(hashtextextended('popoto:rec:<table>:<_pk>', 0))`
    runs as its own statement, in the same message, before any statement
    that writes (and so row-locks) a record: `save` (every model, not only
    those with companion rows), `delete`, `increment`, a capped-list push,
    `touch`, `update_confidence`, the access-tracker writes (stage, confirm,
    discard), `on_context_used`'s `FOR UPDATE`, an `ExistenceFilter`
    membership row, and the backfill. Why: a save that rewrites companion
    rows needs its snapshot to follow any earlier writer of the record (#758
    Race 1: two interleaved saves left the first one's postings and token
    rows behind, 8 of 8 runs, #774 review), and once saves take the key lock
    every other writer must take it too, before the row. With the lock on
    saves only, a transaction that ran `update_confidence(x)` (row lock)
    and then `x.save()` (key lock) crossed a concurrent save of `x` (key
    lock, then row): 40P01 in 10 of 10 runs each for a plain and a
    `transaction()` save (#774 review, blocker 2; 0 of 20 with every writer
    locked).
    **The lock order, one for the whole backend:** any `(model, field)`
    advisory lock (M3's `supersede`; none before it), then the record-key
    advisory locks of the records the statement writes, sorted by `_pk`
    byte order, then the row locks in `_pk` order (`COLLATE "C"`). A
    record's key lock is therefore always the first lock taken on it, so two
    transactions that each touch one record cannot deadlock; transactions
    that take several records in different orders still can, and surface
    `BackendRetryableError` (TD-2). Cost: `update_confidence` p50 0.28-0.29
    ms before and after (2,000 sequential calls × 3, M1 Max, load 4-5).
  - **`save(update_fields=[source])` re-indexes BM25 and re-embeds** (a
    divergence beyond §1.1: Redis runs only the listed field's hook, so its
    index and vector go stale); a scope-only `update_fields` save moves the
    postings and the narrow vector row.
  - **The side rows follow the stored scope, not the instance's.** A scope
    column an `update_fields` save does not write is read from the record
    row inside the save's statement (after the key lock), so
    `save(update_fields=["text"])` after an unsaved, or concurrently
    overwritten, change to the scope column re-indexes in the scope the row
    holds. Before this, the 4-process load (seeds 1-4) ended with r0's
    postings, document length and narrow row in `p1` under a `p2` row in 2
    of 4 seeds; after, 0 of 8 runs. Redis has the variant for its
    partitioned sorted fields: a save listing the field but not the
    partition column moves the member to the instance's partition (#771,
    divergence row in `docs/features/postgres-backend.md`).
  - **`top_by_relevance` lives on `Model.query`**, beside `recall()`, and
    returns `[(instance, score)]`.
  - **`ContentField` is a `text` column**, pulled forward from M5 because
    the M2b gate files' models use it.
- **M2c as shipped: departures from this plan, recorded.** M2c is
  `ContextAssembler` at #758 D8's integration points.
  - **`_pull_path_hybrid` does not call `recall()`.** It runs the same body
    on both backends, and every arm dispatches: `BM25Field.search` is the
    backend's `keyword_search` with `stats="corpus"` (the corpus-statistics
    primitive `recall(bm25_stats="corpus")` uses), the vector arm is
    `vector_search`, and `fuse` is the same Python RRF weighted by
    `_fusion_weights`. Reason: `recall()` ranks every arm inside the scope,
    while the Redis path ranks the vector arm corpus-wide and the BM25 arm
    inside its `SCOPED_SEARCH_FETCH_CAP` window before `fuse` filters to the
    scope, and RRF sums those ranks; the same ranked keys on both legs needs
    the same arms, not the same statistics alone. Only the scope resolution
    has its own branch: the BM25 window is narrowed by the scope's *indexed*
    filters, from one id-only `SELECT`, exactly as `filter_for_keys_set`
    narrows it on Redis (an all-plain scope narrows nothing; a mixed one by
    its indexed part).
  - **The zero-signal fallback is `_pull_path_composite`**, which on
    Postgres is `rank_composite` in SQL, not #758's `top_by_relevance`:
    the composite ranking is what Redis falls back to.
  - **`assess` has no branch of its own.** Its probe is `composite_score`,
    already one SQL statement on Postgres since M2a; what it lacked was the
    metacognitive score proxy, which now reads the partition's scores
    through the backend (`rank_decayed` for a decay field, the column for a
    plain sorted field) instead of `ZSCORE`/`DECAY_SCORE_LUA`.
  - **`_post_effects` on Postgres** is one transaction (`_observe`
    `atomically`, retried on deadlock): the rows of the selected and the
    suppressed records locked `FOR UPDATE` in `_pk` order behind their
    record-key locks, one stage `UPDATE`, then one confidence `UPDATE` for
    every suppressed candidate (a `ConfidenceField` `signal_many`
    `field_call`, the `update_confidence` `SET` list over `_pk = ANY`). It
    runs before `batch()`, so no Redis pipeline is opened.
  - **`OUTAGE_ERRORS` gains `BackendUnavailableError`** in the assembler,
    so a Postgres outage re-raises as a Redis one does instead of reading as
    a failed arm and degrading to the query-blind fallback.
- **Exit criteria.**
  - Gates (a) and (b) on the files below.
    `ContextAssembler(retrieval_mode="auto")` returns the same ranked keys on
    both legs for the retrieval-quality fixtures, because the Postgres hybrid
    path (`_pull_path_hybrid`, used by `test_context_assembler_hybrid.py`, which
    partitions by `agent_id`) passes `bm25_stats="corpus"`; the per-scope
    default applies only to direct `recall()` callers, which are `[PG-only]`
    and outside the parity comparison; BM25 scores and confidence
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
- **M3 as shipped: departures from this plan, recorded.**
  - **The interval is three `double precision` columns, not `tstzrange`.**
    `timestamptz` keeps microseconds: the Redis score `1700000000.1234567`
    reads back `1700000000.123457`, two scores one ulp apart become one
    instant, and the gate's `invalid_at <= as_of` flips for a close one ulp
    after `as_of` (measured on PostgreSQL 18.6; pinned by
    `test_timestamptz_would_not_hold_the_redis_score` and the bit-exact bound
    test). Second, a close at the record's own start (the script allows
    `close == start`) would be `tstzrange(t, t)`, which collapses to `empty`
    and keeps neither bound; `lower > upper` raises. (A range *can* tell "no
    `invalid_at` recorded" from "`+inf`" -- `upper_inf('[t,)')` vs
    `upper_inf('[t,infinity)')` -- so that was never a reason; the #777 review
    corrected it.) This is M2a's
    clock decision applied to the interval: `<f>__valid_from`,
    `<f>__invalid_at`, `<f>__ingested_at`, `NULL` = absent from that index,
    `'Infinity'` = open; the declared value keeps `<f>`, as the Redis hash
    does. B-trees on the two gate columns, not GiST.
  - **The open pointer is a companion table, not a partial `UNIQUE` on an
    identity column.** One record can be the open claim of several
    identities, and an `invalidate` leaves the pointer naming the record it
    closed; a per-row identity column can say neither.
    `<table>__<f>__open (digest PRIMARY KEY, member)` with `member`
    referencing `_pk` `ON DELETE CASCADE` is `on_delete`'s pointer cleanup by
    exact key (no `a`/`ab` over-match). Created with the model's table at
    first use. Consequence: a pointer naming a record that does not exist
    cannot be stored (documented divergence).
  - **Chain links are `<f>__supersedes` / `<f>__superseded_by` on the row**,
    the double-underscore convention of M1/M2a, and `chain` is one `WITH
    RECURSIVE` over them.
  - **Every validity writer follows M2b's one lock order** (§6, TD-2): the
    `(model, field)` advisory lock, then the record-key locks in `_pk` byte
    order (`_record_locked`), then `FOR UPDATE` in `_pk` order. `supersede`
    takes them in that order; `save_and_*` takes all of the supersede's locks
    before its save (the save used to lock the successor first and deadlock
    against a concurrent supersede naming it, #777 review: 10/10 forced
    rounds deadlocked without the pre-lock, 0/10 with it); `import_state`
    takes the field lock as a pointer writer; `ObservationProtocol`'s batch
    takes the field lock before its row locks and locks a contradicted
    record's successor with the batch. Plain saves do not take the field
    lock.
  - **NaN instants are refused before any write** with Redis's text, `value
    is not a valid float`: a save declaring a NaN `valid_from`
    (`ModelException`; Redis's `MULTI`/`EXEC` keeps the hash) and a NaN
    instant in `supersede` (`ValueError`; Redis can tear the state, #778).
  - **`composite_score`'s similarity arm is M2b's** (one `jsonb` parameter,
    `_scores_arm`); #777 had built the same arm as an `unnest` CTE and
    dropped it at the merge, keeping its validity mask on the arm.
    `semantic_search` (with and without `indexes=`) and `keyword_search` run on
    M2b's columns; `test_semantic_search.py` runs on both legs.
  - **The gate reaches every Postgres ranking, not only `top_by_decay`.** The
    assembler's score proxy (`_backend_partition_scores`) passes the model's
    validity field to `rank_decayed`, so `assess_quality` counts a closed or
    not-yet-started record as stale exactly as Redis's `DECAY_SCORE_LUA` does
    (#777 review B1: 0.0/0.0 against Redis's 0.5/1.0 before). The `[PG-only]`
    `top_by_relevance` and `recall` gate at `as_of` (default now) too; `recall`
    ANDs it onto every arm's domain. `import_state` for a record that is not
    stored raises `ValidityMemberAbsentError` (Redis stores the pointer and
    never raises), not a raw `ForeignKeyViolation`.
  - **Cost of the owned transaction.** A save that declares `valid_from` on a
    model with a search field takes its own `BEGIN`/`COMMIT` when no caller
    transaction is open (inside one it takes a savepoint, and a plain save
    takes neither). Measured as the p50 of 300 saves, `BM25Field` + decay +
    `ValidityField` model, PostgreSQL 18.6 on an M1 Max, Postgres on
    localhost, quiet-machine runs: plain save 0.55-0.62 ms; declared
    `valid_from` outside a transaction 0.70-0.79 ms (+0.15-0.2 ms, about +25%);
    declared inside a caller transaction 0.83-1.14 ms (the savepoint).
    `8e5f22b7` and the fix commit measure the same, as the fix touches no
    save path (5 interleaved before/after runs); runs while other jobs shared
    the machine were 2-5x slower on the transactional rows in both. Reproduce
    with a model of that shape and `time.perf_counter()` around `save()`.
- **Files (gate b).** `test_validity_field.py` (non-Lua tests),
  `test_partitioned_confidence.py`, `test_semantic_search.py`.

### M4: graph, the remaining recipes and mixins

- **Scope.** Groups F and H: `graph_*`, `CoOccurrenceField`, `graph_traversal`,
  `CoOccurrenceField.get_linked` (as `graph_expand` at depth 1), and
  `rank_composite`'s `co_occurrence_boost` arm. `field_call` adapters for
  `idle_seconds` and question-queue claim/deliver/release (`FOR UPDATE SKIP
  LOCKED`); `NeverRecordMixin`, `AppendOnlyMixin`. Every remaining recipe runs
  unchanged on a Postgres-bound model.
- **Files (gates a and b).** `test_co_occurrence_field.py`, `test_graph_traversal.py`,
  `test_adaptive_assembler.py`, `test_default_memory_eviction.py`,
  `test_subconscious_memory.py`, `test_trajectory_memory.py`,
  `test_memory_lifecycle.py`, `test_provenance_journal.py`,
  `test_question_queue.py`, `test_memory_telemetry.py`, `test_view_resolver.py`,
  `test_reconciliation_m5.py`, `test_recipes_field_layer.py`.
- **Split.** M4 ships as two PRs: **M4a** (group F -- the graph,
  `CoOccurrenceField`, `graph_traversal`, the `co_occurrence_boost` arm) and
  **M4b** (group H's adapters, the mixins and the remaining recipes). M4a
  has no dependency on M3; several M4b recipes (`provenance_journal`,
  `reconciliation`, `view_resolver`) declare a `ValidityField` and need M3.
- **M4a as shipped: departures from this plan, recorded.**
  - **The edge table is `<table>__<f>__edge (src, dst, weight)`**, `PRIMARY
    KEY (src, dst)`, the M2b companion naming, with no `(dst)` index: the
    only read by `dst` is a delete's reverse-edge cleanup, which joins on the
    deleted rows' `(dst, src)` and so uses the primary key. No foreign key,
    because Redis links any two key strings, records or not.
  - **Edge writes take record-key locks, not a `(model, field)` lock.** A
    write locks the edge sets it writes (`src`, and `dst` when symmetric),
    sorted by `_pk`, with the same `popoto:rec:` key a record writer takes:
    the edge set is that key's state, so it slots into the one lock order.
    That lock is what makes `link`'s count-then-prune atomic (pinned with a
    deterministic interleaving and its control).
  - **`graph_expand` has two paths.** `WITH RECURSIVE` (one statement, a
    layer per iteration, each node's heaviest arrival kept) is exact only
    where the BFS step is monotone (a threshold of at least `1e-290`, a
    finite non-negative decay); elsewhere the visited map's order decides the
    Lua's answer, and the backend replays the Lua queue in Python over
    neighbour lists fetched one layer per statement. The probe compares both
    paths with Redis on every shape.
  - **`graph_update` and `graph_expand` return the field methods' values**
    (the Lua integer reply of `link`, the `%.14g` reply of `strengthen`, the
    pruned count of `weaken_all`; `(RecordId, score)` pairs), and take a
    `mode` (`bfs`, `linked`, `edges`) and `cap`, so `get_linked` and
    `export_state` are `graph_expand` reads.
- **M4b as shipped: departures from this plan, recorded.**
  - **The recipe-layer state outside a model's hash is engine tables**, one
    per schema, created on first use like `popoto_recall_proposal`:
    `popoto_counter`, `popoto_tombstone`, `popoto_tombstone_prior` /
    `_stats`, `popoto_never_record_count` / `_log`,
    `popoto_question_bucket`, `popoto_lease` and `popoto_embedding_cache`.
    Each is reached through a `field_call` adapter keyed by a pseudo-field
    (`_counter`, `_tomb`, `_tombprior`, `_never_record`, `_qq`,
    `_embed_cache`, `_idle`); a sorted field's `count`/`members`/`score` are
    adapters on the field itself. `counters.increment`/`read` gain `model=`
    to pick the backend.
  - **The question queue's delivery waits only on the agent's bucket
    advisory lock** and takes the candidate with `FOR UPDATE SKIP LOCKED`,
    not behind its record-key lock: it never waits on a record, so it cannot
    join a deadlock cycle, and a candidate a concurrent writer holds is
    skipped (a documented divergence). The claim CAS is an ordinary record
    writer (record-key lock, then the row).
  - **`idle_seconds` is the row's write or confirmed-read clock**, not every
    read's (no per-read write on Postgres): whole seconds since the later of
    `_updated_at` and `_last_accessed`.
  - **`MemoryLifecycle` cannot promote a `KeyField` tier on Postgres**: that
    is a key migration, which v2 refuses (§1.1); the tier must be a non-key
    field there. Documented, not worked around in the recipe.
  - **`EventStreamMixin` is still Redis** (M5): a Postgres-bound
    `JournalEntry` `XADD`s to Redis after its write.
    `SubconsciousMemory(auditable_extraction=…)` is refused on a
    Postgres-bound model, because the decision log is Redis-only (§1).
  - **`AppendOnlyMixin`'s guard reads inside a Postgres unit of work**, so
    two saves of one key in one transaction refuse the second -- the
    intra-pipeline gap stays open on Redis only.

### M5: the remainder

- **Scope.** TTL: `_expires_at`, a read filter, and an automatic reaper that
  deletes a bounded batch of expired rows after a write commits, like M2's
  backfill, so there is no cron and no manual job (#755 Q3). `popoto.batch()`
  → `transaction()` (TD-5), atomic on Postgres. `AsyncBackend` on
  `psycopg.AsyncConnection` (TD-6). `PubSub` over `LISTEN`/`NOTIFY` plus an
  events table for `EventStreamMixin`/`StreamConsumer` on a session
  connection (§3 Topology), or declared out of scope. `GeoField`/PostGIS (with `QueryPlan.compute` distances for `test_geo_with_distances.py`), `CyclicDecayField`, `PredictionLedgerMixin`,
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
- **M5 TTL and `batch()` as shipped: departures, recorded.** The first M5
  PR is record expiry and `popoto.batch()`; the rest of M5 follows.
  - **The surface is `Meta.ttl` and the instance's `_ttl`/`_expire_at`.**
    `main` has no `save(ttl=)`, `save(expire_at=)` or `set_expiry`; the
    protocol's `save(expiry=)` is implemented on Postgres (Redis still
    refuses it: no new Redis feature). `_expires_at` is `double precision`
    epoch seconds, M2a's clock decision, and *now* is the server's
    `statement_timestamp()` (one clock for the central database).
  - **Only a `Meta.ttl` model has the column**, so TTL-free models keep
    TTL-free plans (§3). An instance TTL on a model without `Meta.ttl`
    raises `BackendCapabilityError` before writing -- the one place Postgres
    asks more of the model than Redis does.
  - **The read filter is one predicate, applied where the record table is
    scoped** (`render_where`, which every select, count, ranking, search and
    `recall` statement already went through), plus `load`/`exists`, the
    single-record state reads, an anti-join on the side tables (postings,
    lengths, vectors, tokens) for statistics and membership, and the
    validity reads. The narrow-vector-table scope shortcut is off on a TTL
    model, since it would bypass the record table.
  - **The reaper is throttled, not per write.** Running it after every
    write cost ~0.3 ms p50 with nothing to reap (a pool checkout and a
    statement); once per second per table and process, plus immediately
    after a run that found a full batch, keeps an idle TTL model within
    ~0.04 ms of a TTL-free one. Batch 20, not 100: a save that reaps 20 rows
    costs ~1.1 ms p50, 100 rows 2.2-3.5 ms. It never waits: try-locks on the
    record keys, `SKIP LOCKED` rows, and `lock_timeout` below
    `deadlock_timeout`.
  - **A save over an expired key deletes the row first** (same statement
    list, after the record-key lock), so it writes a fresh record as `HSET`
    on an expired key does, rather than reviving the expired row's side rows
    and confidence state. `delete` reports an expired record as not
    existing; `increment`, a capped push, `touch` and `update_confidence`
    treat it as missing.
  - **`batch()` is one object for both backends**: it still returns a
    `redis.client.Pipeline` (a subclass assigned like `GuardedRedis`'s), and
    a Postgres-bound model's write joins a `transaction()` it opens on first
    use; `execute()` commits. Mixed batches are refused, not split: two
    stores cannot commit atomically together.
  - **Exit criterion:** `tests/postgres/test_postgres_ttl.py::
    test_an_expired_row_is_invisible_before_the_reaper_and_gone_after_a_write`.
- **M5 long tail as shipped: departures from this plan, recorded.** The
  `CyclicDecayField` / `PredictionLedgerMixin` / `TDValueField` PR
  (`backends/postgres/longtail.py`).
  - **The cycles are four parallel `double precision[]` columns, not
    `jsonb`** (`<f>__cycle_period`, `<f>__cycle_amp`, `<f>__cycle_phase`,
    `<f>__cycle_base`), the pressure two `double precision` columns
    (`<f>__pressure_rate`, `<f>__pressure_at`), `NULL` = no companion-hash
    entry. An amplitude can be `NaN` or `±inf` and the ranking multiplies
    it; a `jsonb` number is `numeric`, which holds neither, and would add a
    text round trip per scanned row. M2a's clock rule, applied to the cycles.
  - **`CYCLES_MERGE_LUA` is part of the save's upsert, not a second
    statement:** a new row takes the declaration, and `ON CONFLICT` merges
    the amplitudes in one sub-select (period-keyed FIFO pairing by the
    period's bits, #698's three-way rule); `RETURNING old.…` hands back the
    replaced cycles, from which the reset line is logged as on Redis. On
    Redis the merge is a separate `EVAL` after the `HSET`.
  - **The ledger is two engine tables**, `popoto_prediction_ledger (model,
    member, entry jsonb, lua_packed)` and `popoto_prediction_error (model,
    part, member, error)`, not a per-model companion: the `$PL:` keys are
    keyed by class name and are not removed with the record, and engine
    tables (M4b's shape) keep both properties. `lua_packed` marks an entry
    the resolution re-packed, so a read applies cmsgpack's transformation
    (an integral number an `int`, an empty map a list, a `nil` field
    dropped) and a resolved entry reads exactly as on Redis; a `bytes`
    prediction is unresolvable on both legs (Redis's cmsgpack reads no
    `bin`).
  - **`TD_UPDATE_LUA` stores `tostring(q')` through `to_char(…, 'EEEE')`**
    (C's `%.13e`, the same 14 significant digits as `%.14g`), trailing zeros
    trimmed, in one statement; its constants enter through a
    `MATERIALIZED` CTE because the planner folds a constant subexpression
    inside an unreached `CASE` arm, and the clamped helpers' `x / 2` of a
    subnormal constant then raised "underflow" at plan time.
  - **`ObservationProtocol`'s Postgres batch applies the whole effects
    matrix** in the Redis functions' order, inside its one transaction; the
    ledger's confidence feedback and the auto-discharge's confidence read
    run on the batch's connection (`_apply_confidence_feedback(pipeline=)`,
    the confidence `state` adapter with `uow=`), so neither waits on the
    batch's own row lock. Ledger and cycle writes take the record-key lock
    the batch already holds: the one lock order is unchanged.
  - **`DataFrameField` is a documented `validate_spec` refusal**, with its
    reason (the `dataframe` extra is installed by no CI job), not a `bytea`
    column.
  - **The probe** (`scripts/probe_longtail_parity.py`, seeds 1–6 × 500
    shapes on PostgreSQL 18.6 and Redis 8.10.2, macOS arm64): 11,691 cyclic
    ranking scores bit-identical (max deviation 0 ulp), 0 undocumented
    mismatches across the ranking, merge, adjustment, query, TD, ledger and
    observation classes. Two classes surfaced only past seed 1–3's first
    run and were settled before merge: an adjustment of an *empty* cycles
    entry (`array_agg` over no rows is `NULL`, which left a row with periods
    and no amplitudes and failed the next save) -- **fixed**, `coalesce(…,
    '{}')`; and a NaN prediction error (seed 6) -- **documented and pinned**
    (`ledger_nan_error`; Redis's `HSET` survives its refused `ZADD`,
    Postgres refuses first, both with "value is not a valid float").
  - **After the merge with #783 (review of PR #786):** every long-tail
    statement that addresses the record row carries the live-row filter
    (the ledger's `EXISTS` guard, cycle adjust / pressure / export /
    import, the TD update); the ledger tables do not, since the `$PL:` keys
    outlive the record on Redis too. A non-numeric cycle factor raises
    `ValueError` with the script's error text before writing (it persisted
    `NaN` amplitudes), and `lua_tonumber` is C `strtod` as Lua reads it
    (`"0x10"` = 16). The TD read of a stored `numeric` is clamped at
    `strtod`'s rounding boundaries (`±inf` / `±0`, exact), where the cast
    raised "out of range"; `Decimal('-0')` is a documented divergence. The
    long-tail writers join a `popoto.batch()`.

- **M5 events as shipped: departures from this plan, recorded.** Event
  streams, consumer groups and pub/sub on Postgres (`backends/postgres/events.py`,
  `pubsub.py`); gate (b) on `test_event_stream_mixin.py`,
  `test_stream_consumer.py` and `test_pubsub.py`.
  - **Not `bigserial`: Redis's `<ms>-<seq>` ids**, minted from the server
    clock under the stream row's lock (`popoto_stream`), which is held to the
    end of the appending transaction -- so ids commit in id order and a group
    cursor never skips an entry that commits later. The entry is written **in
    the record write's own transaction** (a caller's or a `popoto.batch()`'s:
    just before `COMMIT`, `PostgresUnitOfWork.defer_stream_append`, so the
    stream locks are the last locks taken, after §6's record-key and row
    locks, and **in stream-key order** -- #787 review: registration order let
    two transactions writing two streams in opposite orders deadlock, 82-86
    of 160 under 4 threads), replacing M4b's after-commit Redis `XADD`. Cost:
    a save takes its own transaction (p50 +0.5 ms).
  - **The stream commands keep redis-py's surface** (`stream_client()`
    returns the Redis client or the backend's `StreamStore`), so
    `StreamConsumer` keeps one body for both backends and the gate-(b) files
    run unchanged against either. `MAXLEN ~` trims exactly (a documented
    divergence); `XINFO GROUPS` `entries-read`/`lag` follow Redis 8's rules.
  - **One notification channel per schema** for pub/sub, patterns matched
    client-side, payloads over 8000 bytes refused (not chunked); a separate
    per-schema events channel wakes blocking `XREADGROUP`s. Both `LISTEN` on
    dedicated sessions (`POPOTO_POSTGRES_LISTEN_URL`), with a 1 s fallback
    poll and reconnection. Each payload carries a per-publish nonce, because
    Postgres folds identical notifications within one transaction; globs
    match byte-wise, as Redis's `stringmatchlen` does.
  - **Follow-up (not in M5): one shared listener per process and schema.**
    Each blocking `StreamConsumer` and each `Subscriber` holds its own
    `LISTEN` session, unpoolable, so a central database pays N processes ×
    (consumers + subscribers) sessions against `max_connections` on top of
    the pools (documented in `docs/features/postgres-backend.md`, "Connection
    cost"). A per-process multiplexer -- one `LISTEN` session per schema
    fanning notifications out to in-process waiters -- would make that one
    session per process. Related: `publish()` reads `pg_stat_activity` for
    its count on every call.

## 6. Carried forward from the POC

| Ref (WS4 §7 / feature doc) | Lesson | v2 disposition |
|---|---|---|
| TD-1, #747 | the phase checker is vacuous | M0(a) |
| TD-12, TD-15, TD-26, TD-40 | NUL in `text`; UoW rollback; `2**63`; `rank_decayed` raw reply | documented divergences, §1.1 |
| TD-2, #750 B1 | cross-operation deadlock is detected, not prevented | lock ordering at commit, `_pk`-ordered `FOR UPDATE`, typed retryable error `BackendRetryableError` (M2a, #773: single statements and `on_context_used` retry then raise it; a caller-owned `transaction()` raises it at once); the `(model, field)` lock before row locks is deferred to M3's `supersede` (§5 M2, M2a departures); M2b puts a record-key advisory lock in front of every record writer, so the order is `(model, field)` lock → record-key locks in `_pk` byte order → row locks in `_pk` order, and two single-record transactions cannot deadlock (§5 M2, M2b departures) |
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
| #758 proceeds as a parallel `popoto.pg` | single-plan proposal posted on #755 and #758; M1 waits for #758's author or the maintainer |
| Gate (a) erodes: Redis tests get re-marked or skipped to make a PR green | gate (a) compares against the PR's base commit; a new `redis_only`/`skip` on a previously passing test fails review; per-PR mark audit (TD-16) |
| A `[PG-only]` capability grows a Redis path by accident | each one has a test asserting `BackendCapabilityError` on a Redis-bound model |
| Central Postgres is a shared dependency (decision 1): one database serves every agent and machine | **Outage:** one outage now hits every agent, where Redis stores were per machine; mitigated by the `BackendUnavailableError` contract (health record, counted dropped writes, once-per-window ERROR) and connect/statement timeouts (M1). **HA and backups are owned by the operator**, not by popoto. **Noisy neighbours:** one agent's backfill, reaper or `recall()` load lands on all; mitigated by pool `max_size` limits, statement timeouts, bounded backfill and reaper batches, and optional server-side pooling (§3 Topology). **Mixed popoto versions on one schema:** the `popoto_schema` version record means an older client raises `SchemaDriftError` on a newer schema instead of writing; destructive migrations are operator-run, never automatic (§3 Migrations) |

## 8. No-Gos

Generic `bytea` tables, msgpack inside Postgres, PL/pgSQL decoders, or any
Redis-structure emulation; merging or rebasing `poc/backend-seam`; changing
Redis key layout or wire behaviour; deprecating Redis, or adding a deprecation
warning; requiring new capabilities of Redis (bug fixes are welcome); dual-write or read-through between
backends (#756 is a one-off copy); a separate `popoto.pg.Model` base class;
reading `POSTGRES_URL`/`DATABASE_URL`; popoto-emitted declarative partitioning
or RLS; `py.typed` (unchanged policy, CLAUDE.md); a Postgres port of
`extraction/decision_log.py`, `Query.keys(catchall=True)` or `load_raw_hash`.

## 9. Questions for the architect

No open questions; see Architect decisions.
