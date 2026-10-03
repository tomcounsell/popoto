---
status: Draft
type: feature
appetite: Large
owner: tomcounsell
created: 2026-10-03
tracking: https://github.com/tomcounsell/popoto/issues/759
---

# v2: Postgres-native backend behind a model-level protocol (#759)

## Problem

The #631 POC (`poc/backend-seam`, head `efda14a3`) showed that popoto's
agent-memory semantics can run on Postgres. It also showed that it put the seam
in the wrong place. Its 46-method `Backend` protocol speaks in Redis
structures: opaque index names, sets, sorted sets, maps, index swaps, and msgpack
bytes in and out. As a result the Postgres backend **emulates Redis**. It uses
generic `bytea` tables and decodes msgpack inside PL/pgSQL on every ranked row.
That costs 3.4x to 6.2x on ranking whenever a payload must be read (WS4 report
§4). The report's own verdict (§5) is that the generic layout is "the right
compatibility layer and the wrong storage layout".

v2 keeps the single public model API and puts **two native implementations**
behind it. Redis keeps today's hooks, index sets and Lua. Postgres gets typed
tables, native indexes and its extensions. The protocol sits at the
model/query level, where both backends can be native.

## Direction (maintainer, 2026-10-03; binding on this plan)

1. **Postgres is native, not emulated.** Postgres gets per-model typed tables,
   B-tree/GIN indexes, `tstzrange` validity, pgvector for `EmbeddingField`, a
   SQL BM25 implementation for `BM25Field`, `WITH RECURSIVE` for graphs, and
   PostGIS for geo. It has no emulated sets, sorted sets, or Lua.
2. **Redis stays first-class.** The Redis backend's implementation is today's
   field-hook, index-maintenance and Lua machinery.
3. **The seam moves up** to a model/query protocol. The POC's 46 methods are not
   merged to `main`.
4. **`poc/backend-seam` is frozen as a reference archive.** Only the durable
   parts are salvaged, in small PRs (M0). The POC's SQL and tests are reference
   material for M2.

### Overlap with #755 / #756 (must be reconciled; see Open Question 1)

#755 (plan PR #758) and #756 (plan PR #757) were filed the same day under a
different direction. #755 freezes or deprecates memory on Redis. It ships a
separate `popoto.pg.Model` base class with per-class selection and scopes the
work to Valor's one `Memory` model. That conflicts with points 2 and 3 above
(one model API, Redis first-class). This plan adopts four pieces of #758
rather than re-deriving them: the **field-compiler contract** (#758 D2), as the
Postgres backend's *internal* shape below the protocol; the **postings-table
BM25** and the spike-4 measurement that ruled out `tsvector` (D4);
**exact-vs-HNSW vector selection** (D5); and the **Valor usage survey**
(spike-1), which sets priority within M2 and M3.

## Freshness Check

**Baseline:** `origin/main` @ `57c29ebf`. **Reference archive:** `origin/poc/backend-seam` @ `efda14a3`.

- `src/popoto/backends/` does not exist on `main`. `pytest_plugin.py` on
  `main` has no `conformance` marker. Both live only on the POC branch.
- #630 (recipes bypass the field layer) is **closed**, so recipes reach storage
  through field/model methods. The exception is `recipes/question_queue.py`
  (#730, after the POC forked), which carries three Lua scripts of its own
  (`_DELIVER_LUA`, `_CLAIM_LUA`, `_RELEASE_LUA`). M4 must cover it.
- #747 is still open on `main`. `SUPERSEDE_LUA` sits in
  `src/popoto/fields/validity_field.py`. Its quoting comment at :336 precedes the
  real `-- MUTATION PHASE` marker at :395, and
  `scripts/check_supersede_lua_phases.py` partitions on the first marker.
- `main` has 32 named `*_LUA` scripts: 26 in `fields/`, 1 in
  `models/base.py`, 3 in `recipes/question_queue.py` and 2 in
  `extraction/decision_log.py`. It also has one inline script in
  `Model.atomic_increment`. It has 73 `pipeline if pipeline` /
  `if pipeline:` sites (`grep -rn` over `src/popoto`).

## 1. Public surface the protocol must cover

The table is derived from `src/popoto/__init__.py` `__all__`,
`models/base.py`, `models/query.py`, the field modules and `recipes/`. The tier
is the milestone that delivers the row on Postgres. Methods named in the
protocol column are defined in §2.

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
| `ConfidenceField.update_confidence`/`get_confidence`, partitioned confidence | `CAPPED_BAYESIAN_UPDATE_LUA` on a companion hash | `update_confidence`; reads via `load` | four typed columns, one `UPDATE … RETURNING` | M2 |
| Confidence-modulated decay | `DECAY_SCORE_LUA` reads the confidence hash per member | `rank_decayed(confidence_field=)` | one expression over the confidence column | M2 |
| `ValidityField` `__as_of` / `__current`, `resolve_excluded_keys` | three ZSETs + exclusion rule | `select` (`Predicate` op `valid_at`) | `tstzrange` column, GiST, `@>` | M2 |
| `SupersessionProtocol.supersede`/`invalidate`/`save_and_*` | `SUPERSEDE_LUA` (validate-then-mutate) + open-pointer STRING | `supersede` | one txn: lock, validate, `UPDATE` upper bound, insert; partial unique index on the open identity | M2 |
| `superseded_by`/`supersedes`/`chain` | `HGET` on the chain hashes | `chain` | `WITH RECURSIVE` over `f_supersedes` | M2 |
| `EmbeddingField`, `semantic_search`, `load_embeddings` | `.npy` files + in-process numpy cosine | `vector_search` | pgvector `vector(d)`, `<=>`; HNSW past a size threshold | M3 |
| `BM25Field.search`, `keyword_search` | `BM25_SAVE/DELETE/SEARCH_LUA` postings | `keyword_search` | scope-keyed postings table, BM25 in SQL (#758 D4) | M3 |
| `composite_score` (`co_occurrence_boost`, `similarity_boost`, `temperature`) | temp ZSETs + `ZUNIONSTORE` (POC `native()` sites 6–12) | `rank_composite` | one `SELECT` with a weighted sum over columns and CTE arms | M3 |
| `fuse` (RRF), `ContextAssembler` hybrid path | Python RRF over `(redis_key, score)` lists | above the seam (lists of `(RecordId, score)`) | unchanged; SQL push-down is an optimisation | M3 |
| `ExistenceFilter.might_exist`/`_batch`/`definitely_missing`, `FrequencySketch` | `BLOOM_*_LUA`, `CMS_*_LUA` | `membership_add`, `membership_query` | `(field, token)` / `(field, token, count)` tables (exact) | M3 |
| `CoOccurrenceField.link`/`strengthen`/`unlink`/`weaken_all` | `LINK_WITH_PRUNE_LUA`, `STRENGTHEN_CLAMP_LUA`, `WEAKEN_ALL_LUA` | `graph_update` | edge table upsert; prune by a window rank | M4 |
| `CoOccurrenceField.propagate`, `recipes/graph_traversal.traverse` | `PROPAGATE_BFS_LUA`, `SRANDMEMBER` | `graph_expand` | `WITH RECURSIVE` with depth/threshold | M4 |
| `AccessTrackerMixin` (`on_read`, `confirm_access`, `discard_staged_access`) | `CONFIRM_ACCESS_LUA`, meta hashes | `field_call` | `_access_count`, `_last_accessed` columns + a staged-reads table | M4 |
| `WriteFilterMixin`, `ObservationProtocol`, `NeverRecordMixin`, `AppendOnlyMixin` | Python above storage; `$WF:` priority set | above the seam, plus `field_call` for the priority tier | unchanged | M4 |
| Recipes: `ContextAssembler`, `AdaptiveAssembler`, `DefaultMemory`, `SubconsciousMemory`, `TrajectoryMemory`, `MemoryLifecycle`, `ProvenanceJournal`, `TelemetryRecorder`, `BeliefSheetResolver`, `reconciliation`, `policy_cache` | field/model methods (#630) | none new | follows from M1–M3 | M4 |
| `recipes/question_queue.py` | `_DELIVER/_CLAIM/_RELEASE_LUA` | `field_call` (or a model-level `claim`) | `SELECT … FOR UPDATE SKIP LOCKED` | M4 |
| `Model.idle_seconds` (`memory_lifecycle`) | `OBJECT IDLETIME` | `field_call` | `now() - _last_accessed` | M4 |
| `Meta.ttl`, `save(ttl=/expire_at=)` | `EXPIRE`/`EXPIREAT` | `save(expiry=)` | `_expires_at` column + read filter + reaper (Open Question 4) | M5 |
| `popoto.batch()`, `pipeline=` kwarg everywhere | `GuardedPipeline` (`MULTI`/`EXEC`) | `transaction` | one DB transaction | M5 |
| `async_*` twins, `get_async_redis_db` | `redis.asyncio` | `AsyncBackend` twin | `psycopg.AsyncConnection` pool | M5 |
| `Publisher`/`Subscriber`, `EventStreamMixin`, `StreamConsumer` | `PUBLISH`/`SUBSCRIBE`, `XADD`/`XREADGROUP` | separate `PubSub` protocol (not counted) | `LISTEN`/`NOTIFY` + an events table with `bigserial` | M5 |
| `GeoField` (`_latitude`/`_longitude`/`_radius` filters) | `GEOADD`/`GEOSEARCH` | `select` (op `within`) | PostGIS `geography(Point)` + GiST (capability) | M5 |
| `CyclicDecayField`, `resolve_pressure`/`strengthen_cycle`/`weaken_cycle` | `CYCLIC_DECAY_LUA`, `CYCLES_*_LUA` | `rank_decayed` variant + `field_call` | `jsonb` cycles + SQL expression | M5 |
| `PredictionLedgerMixin`, `TDValueField.td_update` | `RESOLVE_PREDICTION_LUA`, `TD_UPDATE_LUA` | `field_call` | companion table / `UPDATE … RETURNING` | M5 |
| `DataFrameField`, `ContentField` | msgpack bytes / filesystem store | `save`/`load` | `bytea` / `text` (or keep the `ContentStore`) | M5 |
| `check_indexes`/`clean_indexes`/`rebuild_indexes`, `migrations.py` cookbook | Redis set and ZSET scans and repairs | `maintain` | indexes are transactional, so check finds no drift; rebuild = `REINDEX` + `ANALYZE` | M5 |
| `export_records`/`import_records`, `transfer/` | per-field `export_state`/`import_state` | composed of `load`/`save` (+ `field_call`) | backend-neutral; Open Question 6 | M5 |
| `Query.keys(catchall=True)`, `load_raw_hash`, `extraction/decision_log.py` Lua | raw `KEYS`/`HGETALL`/Lua | none | Redis-only debug and extraction surfaces; raise `BackendCapabilityError` | — |

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

`ModelSpec` is built once by `ModelBase` from `_meta`, so it is the same
metadata the field layer already holds. `Op` is the closed set of suffixes in §1
plus `valid_at` and `within`. Values cross the protocol **decoded**: no msgpack,
no key strings, no index names.

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

`UnitOfWork` is a wrapper class: it defines `__bool__ = True` and an
`is_redis_pipeline` accessor. It is not duck-typed `pipeline=` (POC decision 1;
see TD-10 and TD-22 below). `field_call` dispatches to a per-backend
**field-adapter registry** keyed by `(FieldClass via MRO, op)`. It is how the
long tail (AccessTracker, PredictionLedger, CyclicDecay, TDValue, WriteFilter
priority, `idle_seconds`, question-queue claim) reaches storage without adding
a protocol method per mixin. An unregistered `(field, op)` raises
`BackendCapabilityError` naming both. `bind()` refuses at declaration any
field whose adapter is missing for an op the field declares.

**Not on the protocol, on purpose:** `fuse`/RRF, `computed_sort`,
`post_filter`, `to_dict`, `get_or_create`/`update_or_create`/`bulk_*`
(composed in one `transaction()`), `ObservationProtocol`, the never-record
firewall, and pub/sub (a separate `PubSub` protocol, M5). The async twin
(`AsyncBackend`, M5) mirrors the 24 methods one for one.

### How each backend implements it

The Redis column is a **move, not a rewrite**. Unlike the POC, field modules
keep their own `get_REDIS_DB()`/`run_lua` calls. Those modules become reachable
only through `RedisBackend`. Today's `Model.save` body (after `pre_save`)
becomes `backends/redis/records.py::save`, and hooks still fire from there. The
grep criterion inverts: `src/popoto/backends/postgres/` never imports `redis`,
and `models/base.py`/`models/query.py` public methods hold no
`get_REDIS_DB`/`run_lua`.

| Method | Redis: delegates to (on `main` today) | Postgres: SQL |
|---|---|---|
| `bind` | no-op; returns the full capability set | compile `ModelSpec` → DDL; check the `popoto_schema` version row; refuse unsupported fields |
| `transaction` | `batch()` → `GuardedPipeline` (`MULTI`/`EXEC`) | pool connection, `BEGIN … COMMIT`; locks taken in global order at commit (TD-2) |
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
| `keyword_search` | `BM25Field.search` (`BM25_SEARCH_LUA`) | BM25 over `(scope, term, _pk, tf)` postings with live N/avgdl (#758 D4) |
| `graph_update` | `CoOccurrenceField.link`/`strengthen`/`unlink`/`weaken_all` Lua | upsert into `<model>__<field>_edges`; prune past `max_edges` with `row_number()` |
| `graph_expand` | `CoOccurrenceField.propagate` (`PROPAGATE_BFS_LUA`), `graph_traversal` | `WITH RECURSIVE` bounded by depth, threshold and fan-out |
| `membership_*` | `ExistenceFilter`/`FrequencySketch` Lua | `(token)` / `(token, count)` companion tables (exact; Open Question 8) |
| `maintain` | `check_indexes`/`clean_indexes`/`rebuild_indexes` bodies | `check` returns zero drift by construction; `rebuild` = `REINDEX TABLE` + `ANALYZE` |
| `field_call` | the existing field statics and Lua | a compiler-registered statement per op |

## 3. Schema strategy on Postgres

### Tables and keys

- **One table per concrete model**, `popoto.<model_snake>` (schema from
  `POPOTO_POSTGRES_SCHEMA`, default `popoto`).
- `_pk text PRIMARY KEY` holds the **canonical key string** (`RecordId.canonical`,
  today's `DB_key`). `Model.pk`, `fuse` inputs and `Relationship` values are
  therefore unchanged on both backends.
- Each `KeyField` is also a typed column. The key-field tuple carries a
  `UNIQUE` constraint, so typed `WHERE` uses B-trees and never parses `_pk`.
- Engine-owned columns: `_created_at`, `_updated_at`, and `_expires_at` (only
  when `Meta.ttl` or expiry is used).

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
| `AccessTrackerMixin` | `_access_count int`, `_last_accessed timestamptz` + staged-reads companion | B-tree `(_last_accessed)` |
| `DataFrameField` | `bytea` | none |
| custom `Field` subclass with no compiler | if it overrides no hook: column from `type=`; otherwise `bind()` refuses | Open Question 3 |

### Migrations

DDL ownership follows TD-8 and #758 D2. Popoto compiles a deterministic schema
fingerprint per model and keeps `popoto_schema(model, fingerprint, ddl,
applied_at)`. On first use per **process** (never per connection), under
`pg_advisory_xact_lock(hashtext('popoto:ddl:'||table))`:

a **missing table** is created; an **additive** change (nullable column,
index, companion) is applied; a **destructive or ambiguous** change (drop,
retype, vector dimension, key-field change) raises `SchemaDriftError` with the
diff, and the operator runs `python -m popoto.schema migrate <dotted.Model>`
(`--dry-run` prints the DDL).

`POPOTO_SCHEMA_AUTO=0` turns off even create/additive, for operators who
own all DDL. The `migrations.py` cookbook stays Redis-specific. Postgres
documents `ALTER` plus the CLI instead.

### Multi-tenancy (`docs/multi-tenancy.md`)

- **Namespaces.** The documented pattern is a `KeyField` namespace plus
  `partition_by`. It maps directly: the namespace is a typed key column, and
  every partitioned index leads with it.
- **Partitioned `ConfidenceField` hashes.** These become a `WHERE` on that
  column, so the "query must include filter(s) for: project" refusal is kept as
  a field-layer check on both backends.
- **Declarative partitioning and RLS.** `PARTITION BY LIST` and row-level
  security are operator options. Popoto does not emit them.

### Extensions and capabilities

`bind()` checks `pg_extension` once per process. Declaring an `EmbeddingField`
without `vector`, or a `GeoField` without `postgis`, raises at declaration.
Neither is a lazy `NotImplementedError`. Bootstrap refuses `server_encoding <>
'UTF8'` (TD-13). Every tie-break uses `COLLATE "C"`, matching Redis's bytewise
member order.

## 4. Selection

- **`POPOTO_BACKEND=redis|postgres`**, default `redis`, read lazily at the
  first `get_backend()` (WS4 §6, TD-7). **No auto-selection** from
  `POSTGRES_URL`, `DATABASE_URL` or any generic variable.
- **`POPOTO_POSTGRES_URL`** gives the DSN. `POPOTO_BACKEND=postgres` without it,
  or without `psycopg`, is a hard error at selection that names the variable.
- **Per-model override.** `Meta.backend = "redis" | "postgres"` overrides the
  process default per model. #758 found Valor keeps Redis counters and a
  Postgres `Memory` in one process. This is an explicit choice, not
  auto-selection.
- **Unchanged.** `get_redis()`, `POPOTO_REDIS_DB` (PEP 562 hook) and
  `REDIS_URL` keep their current meaning. Importing popoto never dials
  Postgres.
- **Tests.** The pytest plugin pins `redis` for the session whenever it opted
  in. The `backend` fixture binds Postgres per test and restores the
  *previous* binding (salvaged POC rule, `docs/testing.md`).

## 5. Milestones

Each milestone is one or more PRs against `main`, independently shippable, and
gated by the conformance harness. "Both legs" means
`POPOTO_CONFORMANCE_BACKENDS=redis,postgres pytest -m conformance` passes in
the `postgres` CI job, and the full Redis suite stays green on the Redis and
Valkey jobs. Each per-leg count states its attribution rule (TD-33).

### M0: salvage to `main`, zero behaviour change

- **Scope.** Four PRs, each cherry-picked or ported from `efda14a3`, not
  rebased:
  - **(a)** #747 fix: partition on the last or column-0 marker, plus a negative
    test that plants a write in the validation phase.
  - **(b)** `pytest_plugin.py`: the `conformance`/`redis_only` markers. Make
    `redis_only` *require* `reason=`, since 13 POC marks lacked one. Add
    `popoto_conformance_backends` ini/env, Postgres schema isolation
    (`popoto_test_<hex>`, refuse `public` and a db-less URL), and the Redis
    session pin.
  - **(c)** The `postgres` CI job in `tests.yml`. It runs Postgres and Redis
    services and installs `.[…,postgres]`.
  - **(d)** The `postgres` extra (`psycopg[binary]`, as on the POC; M1 adds
    `psycopg-pool`), plus `check_lock_imports.py` and `uv.lock`.
- **Not salvaged.** `backends/` and the 46-method protocol. The
  `tests/conformance/test_{records,indexes,swaps,decay,validity}.py` files
  test that protocol, so they stay in the archive as reference.
- **Exit criteria.** The full Redis suite is green; harness self-tests in the
  style of the archive's `test_harness.py`/`test_postgres_bootstrap.py`
  (schema create, refusal, teardown) pass in the `postgres` job, and with no
  backend yet the Postgres model leg skips with a named reason;
  `scripts/check_supersede_lua_phases.py` fails on the planted write; the
  ratchet is not above its ceiling.
- **Tests.** `tests/test_pytest_plugin.py`, `tests/test_validity_field.py::TestSupersedeLuaPhaseSplit`, `tests/test_ci_workflow_redis_url.py`, `tests/test_check_lock_imports.py`.

### M1: a plain-model vertical slice on typed tables

- **Scope.**
  - `backends/{__init__,types}.py` (protocol, selection, `ModelSpec`) and
    `RedisBackend` (the move described in §2).
  - `PostgresBackend` with groups A–C. Models use `KeyField`/`AutoKeyField`,
    `IntField`, `FloatField`, `StringField`, `DatetimeField` and `SortedField`:
    save/get/delete/exists/increment/filter/`Q`/order_by/limit/count.
  - The pool (psycopg `ConnectionPool`, fork-safe pid check, TD-3), the
    schema compiler plus `popoto_schema`, and `bind()` capability refusal.
  - `Model`/`Query` public bodies dispatch to `get_backend()`.
- **Exit criteria.**
  - Every file below runs on both legs from the same test code. Redis-key
    assertions are marked `redis_only` with a reason; none are deleted.
  - Wire parity on Redis: a command trace of the files below matches the
    trace on `main` byte for byte, using the POC's #751 serializer-hook method.
  - `Model.save()` p50 is ≤ 2x Redis and `filter+hydrate` p50 is ≤ 1x, after
    `ANALYZE`, using the archive's `scripts/bench_backend_seam.py` ported to
    the v2 API (environment stated).
- **Files that must pass on both legs.** `test_model_exists.py`,
  `test_get_many.py`, `test_get_or_create.py`, `test_bulk_operations.py`,
  `test_model_equality.py`, `test_auto_timestamps.py`, `test_q_objects.py`,
  `test_meta_order_by.py`, `test_sorted_field_ordering.py`,
  `test_sorted_time_field.py`, `test_atomic_increment.py`, `test_to_dict.py`,
  `test_datetime_tzinfo_round_trip.py`, `test_client_side_filter.py`,
  `test_delete_all.py`, `test_model_partial_load.py`, `test_key_fields.py`.
- **Module-scope scripts.** `test_queries.py`, `test_sortedfield.py`,
  `test_field_types.py` and `test_query_results.py` have zero `def test_`, so
  they are converted to test functions first. Each conversion is its own PR
  with no logic change.

**M1.1: plain-field breadth.** This is a deviation from a strict M1→M2 order.
It follows the M1 compiler cheaply and M4 needs it.

- **Scope.** `IndexedField`/`UniqueField`, `TagField`, `Relationship`, the
  collection fields, `DateField`/`TimeField`, `Meta.indexes` and the
  unique-conflict text.
- **Files.** `test_indexed_fields.py`, `test_issue_534_indexed_field_encoders.py`,
  `test_tag_field.py`, `test_relationship_edge_cases.py`,
  `test_relationship_sample.py`, `test_meta_indexes.py`,
  `test_field_defaults_roundtrip.py`, `test_list_field_capped.py`,
  `test_immutable_keys.py`.

### M2: agent-memory core

- **Scope.** Group D plus `rank_decayed`:
  - `DecayingSortedField`, `touch`, `top_by_decay`;
  - `ConfidenceField`, including partitioned confidence and modulation;
  - `ValidityField` on `tstzrange`, and `SupersessionProtocol` in all modes
    with the four typed errors.
  - Ported from the POC: the validity exclusion rule (17-row table), the
    `popoto_supersede` phase order and its error tokens, and the decay SQL
    expression. The POC read msgpack per row; M2 reads typed columns.
  - Lock ordering: one `(model, field)` advisory lock first, then
    `FOR UPDATE` in `_pk` order. `DeadlockDetected`/`SerializationFailure`
    map to a typed retryable error with bounded retry (TD-2).
- **Exit criteria.**
  - Both legs pass on the files below.
  - The POC's crossing-chains interleaving
    (`tests/conformance/test_validity.py::TestConcurrency`, archive) is
    re-expressed at model level and passes ten times.
  - `rank_decayed` with base + confidence + validity gate at N=2000: Postgres
    p50 ≤ Redis p50. The POC measured 6.2x on bytea; WS4 §4 predicts typed
    columns make it "the 0.2x case".
- **Files.** `test_decaying_sorted_field.py`, `test_top_by_decay_autodetect.py`,
  `test_decay_rank_seam.py`, `test_confidence_field.py`,
  `test_confidence_modulated_decay.py`, `test_partitioned_confidence.py`,
  `test_validity_field.py` (non-Lua tests), `test_retrieval_quality_regression.py`.

### M3: vector and keyword search, composite ranking, fusion

- **Scope.**
  - `vector_search` on pgvector, with exact or HNSW chosen by scope size and a
    recall guard (#758 D5).
  - `keyword_search` on a postings table with tokenizer parity
    (`fields/_tokenizer.py`) and a stable `_pk` tie-break.
  - `rank_composite`, and `fuse` over `RecordId` lists.
  - `membership_*` for `ExistenceFilter`, which `ContextAssembler` calls
    through `definitely_missing`.
- **Exit criteria.**
  - Both legs pass.
  - BM25 scores match the Redis oracle to 1e-9 on the fixture corpus.
  - Hybrid recall@10 on `test_hybrid_retrieval.py` fixtures is identical.
- **Files.** `test_embedding_field.py`, `test_semantic_search.py`,
  `test_bm25_field.py`, `test_composite_score_query.py`,
  `test_hybrid_retrieval.py`, `test_rrf_fusion.py`, `test_fusion_weights.py`,
  `test_existence_filter.py`.

### M4: recipes and the memory mixins

- **Scope.**
  - Groups F and H: `graph_*`, `CoOccurrenceField`, `graph_traversal`.
  - `field_call` adapters for `AccessTrackerMixin`, the `WriteFilterMixin`
    priority tier, `idle_seconds`, and the question-queue claim/deliver/release
    (`FOR UPDATE SKIP LOCKED`).
  - Every recipe running unchanged on a Postgres-bound model.
- **Exit criteria.**
  - Both legs pass on the files below.
  - `ContextAssembler(retrieval_mode="auto")` returns the same ranked keys on
    both legs for the retrieval-quality fixtures.
- **Files.** `test_co_occurrence_field.py`, `test_graph_traversal.py`,
  `test_access_tracker.py`, `test_write_filter.py`,
  `test_observation_protocol.py`, `test_context_assembler.py`,
  `test_context_assembler_hybrid.py`, `test_context_assembler_token_budget.py`,
  `test_adaptive_assembler.py`, `test_default_memory_eviction.py`,
  `test_subconscious_memory.py`, `test_trajectory_memory.py`,
  `test_memory_lifecycle.py`, `test_provenance_journal.py`,
  `test_question_queue.py`, `test_memory_telemetry.py`, `test_view_resolver.py`,
  `test_reconciliation_m5.py`, `test_recipes_field_layer.py`.

### M5: the remainder

- **Scope.**
  - TTL (Open Question 4).
  - `popoto.batch()` → `transaction()` (TD-5).
  - `AsyncBackend` on `psycopg.AsyncConnection` (TD-6).
  - `PubSub` over `LISTEN`/`NOTIFY` plus an events table for
    `EventStreamMixin`/`StreamConsumer`, or declared out of scope.
  - `GeoField`/PostGIS, `CyclicDecayField`, `PredictionLedgerMixin`,
    `TDValueField`, `DataFrameField`, `ContentField`.
  - `maintain`, and backend-neutral `transfer/`.
- **Exit criteria.** Each item lands as its own PR with both legs green on its
  files, or as a documented `bind()` refusal.
- **Files.** `test_meta_ttl.py`, `test_batch.py`, `test_atomic_save.py`,
  `test_async.py`, `test_event_stream_mixin.py`, `test_stream_consumer.py`,
  `test_geo_with_distances.py`, `test_cyclic_decay_field.py`,
  `test_prediction_ledger.py`, `test_td_value_field.py`,
  `test_content_field.py`, `test_check_indexes.py`, `test_clean_indexes.py`,
  `test_transfer_roundtrip.py`, `test_transfer_key_regeneration.py`.

## 6. Carried forward from the POC

| Ref (WS4 §7 / feature doc) | Lesson | v2 disposition |
|---|---|---|
| TD-1, #747 | the phase checker is vacuous | M0(a) |
| TD-2, #750 B1 | cross-operation deadlock is detected, not prevented | lock ordering at commit, `(model, field)` lock before row locks, `_pk`-ordered `FOR UPDATE`, typed retryable error (M2) |
| TD-3 | one connection per instance; ten threads hung the harness | `psycopg_pool.ConnectionPool`, a connection per transaction, pid check after fork (M1) |
| TD-7, WS4 §6 | `POSTGRES_URL` auto-selects | `POPOTO_BACKEND` + `POPOTO_POSTGRES_URL` (§4) |
| TD-8, #750 TD4 | `CREATE OR REPLACE FUNCTION` ×10 on every connection | `popoto_schema` fingerprint, once per process; no PL/pgSQL required by M1 (§3) |
| TD-9 | out-of-scope fields fail at first use | `bind()` refuses at declaration |
| TD-10 | 73 `pipeline if pipeline` sites return `None` on an empty Postgres UoW | `UnitOfWork.__bool__ = True`, so the sites are inert; Postgres never hands a UoW to a Redis hook. The sweep is optional hygiene |
| TD-11 | `popoto_numeric` side-map fed by nobody | gone: typed columns are the value |
| TD-12 | NUL in `text` raises `DataError` | refuse `\x00` in text fields at `to_db` with a `ValueError` naming the field (Open Question 5) |
| TD-13 | tie order assumes `UTF8` | bootstrap refuses non-UTF8; `COLLATE "C"` on every tie-break |
| TD-14 | pointer table conflates index and tag spaces | gone: no pointer tables |
| TD-15 | UoW rollback differs per backend | Open Question 2 |
| TD-16, TD-35 | stale and inert `redis_only` marks; marks without reasons | `reason=` required (M0); per-file mark audit in each milestone PR |
| TD-21 | Query layer keeps `bytes` members | gone at the seam: `RecordId` everywhere; Redis internals unchanged |
| TD-23 | residual Redis wire changes from routing | avoided by construction (move, not rewrite); M1 trace-diff exit criterion |
| TD-26 | `2**63` packs per platform | moot on `bigint`; `increment` overflow raises `NumericValueOutOfRange` where Redis Lua goes to float (documented deviation) |
| TD-40, WS4 §9 Q5 | `rank_decayed` raw flat reply | the protocol returns `Scored`; the public `DecayingSortedField.rank_decayed(zset_key, …)` stays Redis-shaped (Open Question 7) |
| feature doc "Known deviations" | Valkey 8 and Redis 7/8 cmsgpack emit the confidence map in different key orders | Postgres stores typed columns, so the point is moot; `transfer/` and #756 must decode by key and never byte-compare payloads |
| feature doc | `power()` raises on finite over/underflow where Lua returns `inf`/`0` | clamp the exponent in the decay SQL so both legs agree; add the POC's boundary rows as conformance cases |
| feature doc | finite `float8` overflow raises | same clamp; document for `increment` |
| feature doc | globs match bytes vs chars | moot: v2 has no glob method; `Query.keys(catchall=True)` is Redis-only |
| WS4 §4 | two benchmark rows bimodal (no `ANALYZE`) | `ANALYZE` after seeding, prepared statements, ≥3 runs per row, range reported |
| WS4 §1.1, TD-33 | per-leg counts depend on the counting method | state the attribution rule with every count |

## 7. Risks

| Risk | Mitigation |
|---|---|
| The Redis move is not wire-identical (the POC found #735's `SADD` and `EXPIRE`-order divergences only by tracing) | M1 trace-diff gate, #751 method |
| Ranking math drifts when decay, capped Bayesian and BM25 are reimplemented in SQL | oracle conformance with per-test numeric tolerances, plus the POC's boundary cases |
| The protocol grows past 25 | new features use a `field_call` adapter; any protocol change is its own small PR (the POC freeze rule) |
| #755 builds a parallel `popoto.pg` | settle Open Question 1 before M1 starts |

## 8. No-Gos

Generic `bytea` tables, msgpack inside Postgres, PL/pgSQL decoders, or any
Redis-structure emulation; merging or rebasing `poc/backend-seam`; changing
Redis key layout or wire behaviour; reading `POSTGRES_URL`/`DATABASE_URL`;
popoto-emitted declarative partitioning or RLS; `py.typed` (unchanged policy,
CLAUDE.md); a Postgres port of `extraction/decision_log.py`,
`Query.keys(catchall=True)` or `load_raw_hash`.

## 9. Questions for the architect

1. **Reconcile with #755/#758.** This plan follows the binding direction: one
   `Model` API, Redis first-class, memory fields on both backends. #755 says
   memory on Redis is frozen or deprecated and ships `popoto.pg.Model` for
   Valor only. Proposed resolution: this plan is the umbrella; #758's field
   compilers become the Postgres backend's internals; Valor's surface (#758
   spike-1) is the first slice of M2 and M3; `popoto.pg.Model` becomes
   `Meta.backend = "postgres"`. Does #758 merge as-is, get revised to target
   this protocol, or close? Must memory primitives keep passing on Redis (this
   plan's M2–M4 "both legs" gate assumes yes)?
2. **Unit-of-work failure semantics.** Postgres rolls back the whole
   transaction; Redis `MULTI`/`EXEC` (`batch(transaction=True)`) applies every
   queued command and reports a runtime failure without rollback. (a) Document
   "atomic on Postgres, applied-with-errors on Redis"; (b) pre-validate on the
   Redis UoW before `EXEC` (narrows, does not close, the gap); or (c) promise
   no partial-failure guarantee on either.
3. **Field-hook API when Postgres runs no hooks.** `on_save`/`on_delete`/
   `pre_save_validate` and the `docs/field-authoring.md` workflow are Redis
   mechanics. (a) Hooks become Redis-backend-only API, and a custom field needs
   a Postgres compiler or `bind()` refuses it; (b) Postgres also calls Python
   hooks in the transaction for custom fields only (emulation at the edge); or
   (c) deprecate public hooks for a backend-neutral `FieldAdapter`
   registration. Which is public in 2.0?
4. **TTL on Postgres.** (a) `_expires_at` + a filter on every read + a reaper
   piggy-backed on writes (no cron); (b) `pg_cron`; or (c) `bind()` refuses
   `Meta.ttl`. (a) changes every query plan; (c) excludes TTL'd recipes.
5. **NUL bytes.** Refuse on both backends (a behaviour change on Redis) or on
   Postgres only (a documented divergence)?
6. **Migration tooling for existing Redis users.** (a) Generalise `transfer/`
   (`export_records`/`import_records`, per-field `export_state`/`import_state`)
   into a backend-neutral copy on `load`/`save` + `field_call`, giving every
   user `popoto transfer --from redis --to postgres`, which needs a mapping
   for companion state (decay timestamps, confidence hashes, validity chains,
   co-occurrence edges); or (b) keep #756's one-off Valor tool only.
7. **`DecayingSortedField.rank_decayed(zset_key, …)` and other Redis-shaped
   public statics.** They take a ZSET key or return Redis keys:
   `BM25Field.search`, `EmbeddingField.load_embeddings` (returns
   `(matrix, redis_keys)`), `CoOccurrenceField.get_linked`. Options: keep
   them Redis-only and add backend-neutral twins, or change their signatures
   in a major release.
8. **ExistenceFilter on Postgres.** An exact token table has no false
   positives, so `might_exist` becomes stricter than on Redis. Is that
   acceptable, or must the `bloom` extension reproduce the probabilistic
   behaviour?
