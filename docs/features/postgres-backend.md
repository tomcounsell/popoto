# Postgres Backend (v2)

Popoto v2 keeps one model API with two native storage backends behind it
([#759](https://github.com/tomcounsell/popoto/issues/759)). Redis keeps
today's hashes, index sets and Lua. Postgres stores each model in a typed
table with native indexes, and it is where new capabilities land.

This page covers the first Postgres milestones: **plain models** (M1),
**plain-field breadth** (M1.1), the **ranking and memory-state half of
Valor's slice** (M2a), **search** (M2b), **`ContextAssembler`** (M2c), the
**validity axis** (M3), the **co-occurrence graph and the remaining
recipes** (M4), and M5: **record expiry** (`Meta.ttl`), **`popoto.batch()`**
and the **long-tail memory fields** (`CyclicDecayField`, `TDValueField` and
`PredictionLedgerMixin`).
That means records, queries, `Q` objects, ordering, counting and atomic
increments for the field types listed below, including indexed, unique, tag,
relationship and collection fields, plus decay ranking, confidence
(partitioned too), read tracking, the write filter, `ObservationProtocol` and
`composite_score`, BM25 keyword search, pgvector embeddings, exact membership
filters, fusion and `recall()`, the assembler over all of them,
`ValidityField` with `SupersessionProtocol`, `CoOccurrenceField` with its
graph expansion, the remaining recipes and mixins on top, records that
expire, batches that commit as one transaction, and cyclic decay, TD values
and the prediction ledger. Models that use other fields stay on Redis until
their milestone. Popoto refuses them when you declare them, so they never
fail halfway through.

## Selecting the backend

```bash
pip install 'popoto[postgres]'      # psycopg[binary,pool] + pgvector
```

A backend is chosen per model, or once for the whole process:

```python
import popoto

class Note(popoto.Model):
    owner = popoto.KeyField()
    slug = popoto.KeyField()
    hits = popoto.IntField(default=0)
    score = popoto.SortedField(type=float, partition_by="owner")

    class Meta:
        backend = "postgres"        # this model only
```

```bash
export POPOTO_BACKEND=postgres                                  # process default
export POPOTO_POSTGRES_URL=postgresql://db.internal:5432/agents  # the DSN
export POPOTO_POSTGRES_SCHEMA=popoto                             # optional; default "popoto"
```

**Which variable each component reads:**

| Component | Reads | Never reads |
|---|---|---|
| the library (`popoto`) | `POPOTO_BACKEND`, `POPOTO_POSTGRES_URL`, `POPOTO_POSTGRES_SCHEMA`, `POPOTO_SCHEMA_AUTO` | `POSTGRES_URL`, `DATABASE_URL` |
| the pytest conformance harness | `POSTGRES_URL`, for its throwaway `popoto_test_<hex>` schema (see [Testing](../testing.md)) | — |

Popoto never picks Postgres up from a generic variable. Selecting Postgres
without `POPOTO_POSTGRES_URL`, or without the `postgres` extra installed,
raises `BackendUnavailableError` naming what is missing. You can also pass
the DSN directly:
`popoto.backends.set_backend(PostgresBackend(dsn=..., schema=...))`.

**Laziness.** `import popoto` never imports `psycopg`. Defining a
`Meta.backend = "postgres"` model never touches the network either: class
creation only checks the model's fields against a static capability table.
The connection, the version check, the DDL and the schema check all run inside
the model's **first** query or save. `bind()` itself only compiles the table
spec, so an outage at that first call is charged to the call that hit it: a
first save against an unreachable server counts as a dropped write, and a
first query does not.

## Supported fields (M1, M1.1, M2a, M2b, M3, M4, M5)

| popoto field | Column | Index |
|---|---|---|
| `KeyField` / `UniqueKeyField` / `AutoKeyField` (`type=T`) | `T` (default `text`) | all key fields under one `UNIQUE`; a B-tree for each non-leading key field; `UniqueKeyField` also gets its own `UNIQUE` |
| `SortedField(type=T, partition_by=…)` / `SortedKeyField` | `T`: `bigint`, `double precision`, `numeric`, `date`, `time` or `timestamptz` | B-tree `(partition cols…, f, _pk COLLATE "C")` |
| `IntField` / `FloatField` / `DecimalField` / `BooleanField` | `bigint` / `double precision` / `numeric` / `boolean` | — |
| `StringField` | `text` | — |
| `DatetimeField` | `timestamptz` plus `<f>__utcoff integer` (offset in seconds; `NULL` = naive) | — |
| `Field(type=int/float/str/bool/Decimal/datetime)` | as above | — |
| `IndexedField(type=T)` / `UniqueField(type=T)` (M1.1) | `T` (a scalar type) | B-tree on the column; `UNIQUE` for a unique field |
| `TagField` (M1.1) | `text[]`, normalised (sorted, unique, `str(tag)`; untagged is `{}`) | GIN: `__contains` and `__all` are `@>`, `__any` is `&&` |
| `Relationship(model=M)` (M1.1) | `text` holding the target's `_pk` | B-tree (no foreign key: Redis enforces none, and references may be circular) |
| `ListField` / `DictField` / `SetField` / `TupleField`, `Field(type=list/dict/set/tuple)` (M1.1) | `jsonb` | — |
| `ListField(max_length=N)` (M1.1) | `jsonb`, each element type-tagged as `push()` writes it on Redis | — |
| `BytesField` / `DateField` / `TimeField` (M1.1) | `bytea` / `date` / `time` | — |
| `Meta.indexes` (M1.1) | — | a composite B-tree per entry; `UNIQUE` when `is_unique` |
| `DecayingSortedField(partition_by=…, base_score_field=…)` (M2a) | `double precision`: the decay clock, in epoch seconds | B-tree `(partition cols…, f, _pk COLLATE "C")` |
| `ConfidenceField` (M2a; `partition_by=` M3) | `double precision` for the attribute, plus the state `<f>__conf` (`double precision`), `<f>__n`, `<f>__corr`, `<f>__contra` (`bigint`) | — |
| `BM25Field(source=…)` (M2b) | no column of its own: postings `<table>__<f>__post (scope, term, _pk, tf)` and lengths `<table>__<f>__dl (_pk, scope, len)` | postings `PRIMARY KEY (scope, term, _pk)` plus a B-tree on `_pk`; lengths `(scope) INCLUDE (len)` |
| `EmbeddingField(source=…)` (M2b) | `<f> bigint` (the dimension count Redis stores), `<f>__vec vector(d)`, `<f>__model text`, `<f>__hash text`, plus the narrow vector table `<table>__<f>__vec (_pk, scope, v vector(d))`, `v` stored `PLAIN` | HNSW `vector_cosine_ops` on `<f>__vec`; a partial B-tree on rows with no vector, and a B-tree on `<f>__model` (the backfill's probe); `(scope)` on the narrow table |
| `ExistenceFilter` / `FrequencySketch` (M2b) | no column: `<table>__<f>__tok (token, _pk)` / `<table>__<f>__cnt (token, count)` | `PRIMARY KEY (token, _pk)` / `PRIMARY KEY (token)` |
| `ContentField` (M2b, pulled forward from M5) | `text` holding the content itself (no `$CF:` reference, no file) | — |
| `ValidityField` (M3) | `double precision` for the declared value, plus `<f>__valid_from`, `<f>__invalid_at`, `<f>__ingested_at` (`double precision`; `'Infinity'` = open) and `<f>__supersedes`, `<f>__superseded_by` (`text`); companion `<table>__<f>__open (digest, member)` | B-tree on `<f>__valid_from` and on `<f>__invalid_at`; the companion's `member` references `_pk` `ON DELETE CASCADE` |
| `CoOccurrenceField(symmetric=…, max_edges=…)` (M4) | no column: the edge table `<table>__<f>__edge (src, dst, weight)` | `PRIMARY KEY (src, dst)` |
| `CyclicDecayField(cycles=…, pressure_rate=…)` (M5) | `double precision`: the decay clock, as for `DecayingSortedField`; the cycles as four parallel `double precision[]` columns `<f>__cycle_period`, `<f>__cycle_amp`, `<f>__cycle_phase`, `<f>__cycle_base` (the #698 declared baseline, a `NULL` element = unknown); the pressure as `<f>__pressure_rate` and `<f>__pressure_at` (`last_resolved`). `NULL` = no companion-hash entry | B-tree `(partition cols…, f, _pk COLLATE "C")` |
| `TDValueField` (M5) | `numeric`, as `DecimalField` | — |
| `GeoField` (M5) | `jsonb` for the coordinates as given, plus `<f>__geohash bigint` (the score `GEOADD` stores; `NULL` = not in the geo index) and `<f>__geolon` / `<f>__geolat` (`double precision`, the position decoded from that score) | partial B-tree on `<f>__geohash` (no PostGIS, no GiST) |

`PredictionLedgerMixin` (M5) adds no column: its ledger is two engine tables
(see [Long-tail fields](#long-tail-fields-m5)). `DataFrameField` is refused
at declaration with its reason: a pandas `DataFrame` is not stored on Postgres
in v2 (the field needs the optional `dataframe` extra, which no CI job
installs, so a column for it would ship untested); store the frame's JSON in a
`DictField` or a `BytesField`.

Every table also has:

- `_pk text PRIMARY KEY`, which holds the same `ClassName:key:…` string that
  Redis uses as the hash key. `Model.pk` and `redis_key` are therefore
  identical on both backends.
- `_created_at` and `_updated_at` (`timestamptz`), which the engine maintains.
  They are Postgres-only.
- `_migrated_from jsonb` and `_estimated_fields text[]` (M2b), the import
  contract for the one-off Redis-to-Postgres copy (#756). An importer records
  where a row came from and which values it inferred. Every native write
  (`save`, `atomic_increment`, `push`, and the embedding backfill) sets
  `_migrated_from` to `NULL`, so a re-load can guard with
  `WHERE _migrated_from IS NOT NULL` and never overwrite a row popoto has
  written since. The only engine write to `_estimated_fields` is the
  backfill's: it removes the embedding field's name, because the vector it
  writes is derived natively rather than imported.

`DatetimeField` round-trips the way it does on Redis (#521). An aware value
comes back with the same UTC offset as a fixed-offset `timezone`. A naive
value comes back naive. A naive value is stored and compared as UTC, which is
the same instant `SortedField` scores it as on Redis.

**Collections are JSON, never msgpack.** A collection field comes back
exactly as it comes back from Redis: the field's own type is restored at the
top level (a `TupleField` is a tuple, a `SetField` a set), nested values
behave as msgpack does (a nested tuple comes back a list; a tagged
`Decimal`/`date`/`tuple` element of a capped list comes back typed), and a
value msgpack cannot pack (a nested `set` or `Decimal`) raises `TypeError` on
both backends. What JSON lacks and msgpack has is tagged so it survives:
`bytes`, a non-finite `float`, and a `dict` with non-`str` keys.

**Relationships stay lazy.** The column holds the related record's key
string. `filter()`/`all()` return it as that string; `get()`/`get_many()`
resolve it to the related instance, exactly as the Redis read paths do.
`Relationship.sample_related_keys` is an id-only `SELECT … WHERE f = $key
ORDER BY random() LIMIT n`, with `SRANDMEMBER`'s count contract (negative
counts repeat).

**Uniqueness.** A `UniqueField`, `UniqueKeyField` or unique `Meta.indexes`
tuple is checked in `pre_save` by a read through the backend, with the same
`ModelException` text as Redis and before any write. The `UNIQUE` index is the
authority behind it: a write that races past the read, or two conflicting
saves inside one `transaction()`, gets the same text from the index.

**Memory state (M2a).** A `ConfidenceField` keeps two things, as it does on
Redis: the model attribute (the hash value there, its own column here) and
the evidence state (the companion hash entry there, the four `__` columns
here). State that is `NULL` is the seed Redis writes on save with `HSETNX`
(`initial_confidence` and three zeros), so a save never writes it and a
re-save never resets it. A model with `AccessTrackerMixin` gains
`_access_count`, `_last_accessed`, `_staged_reads` and `_staged_at`; staged
reads count only while `_staged_at` is within `_staged_ttl_seconds`, which is
what the Redis list's refreshed `EXPIRE` means. `WriteFilterMixin`'s gate runs
above the seam as before; its priority tier is not stored (plan §5 M2).
`RecallProposal` keeps its pending proposals in one engine table per schema,
`popoto_recall_proposal`, created on first use like `popoto_schema`. No state
column is indexed: each index would make every `update_confidence` or
`confirm_access` a non-HOT update, and nothing filters or orders by one alone.
A partitioned `ConfidenceField` (M3) needs nothing more: its state is the
record's own columns, so its partition is the row's partition columns, and a
partition change that keeps the key keeps the state.
`PredictionLedgerMixin` keeps its ledger in Redis until M5 and is refused on a
Postgres model too, rather than issuing Redis commands for a record Redis does
not hold. `EventStreamMixin` writes its stream to the backend's events tables
(M5, [Event streams and pub/sub](#event-streams-and-pubsub-m5)), in the
save's own transaction.

`Meta.ttl` (M5) adds an engine column, `_expires_at`; see
[Record expiry](#record-expiry-m5).

`GeoField` (M5) is stored without PostGIS; see [Geo](#geo-m5).

An `IndexedField` on a collection type is refused, as is a custom field that
overrides a storage hook. A model that uses one of them raises
`BackendCapabilityError` when you declare it with `Meta.backend =
"postgres"`, or on first use when it takes the process default.

## What the schema looks like

The model above compiles to:

```sql
CREATE TABLE "popoto"."note" (
  "_pk" text PRIMARY KEY,
  "hits" bigint, "owner" text, "score" double precision, "slug" text,
  "_created_at" timestamptz NOT NULL DEFAULT now(),
  "_updated_at" timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX "note__keys__idx" ON "popoto"."note" ("owner", "slug");
CREATE INDEX "note__slug__idx" ON "popoto"."note" ("slug");
CREATE INDEX "note__sort__score__idx" ON "popoto"."note" ("owner", "score", "_pk" COLLATE "C");
```

**Migrations.** Popoto records each table's fingerprint in
`popoto.popoto_schema`, along with the DDL, the popoto version that wrote it
and a schema-format version. The first use in each process creates a missing
table or applies an **additive** change (a new nullable column, a new index)
under `pg_advisory_xact_lock`: the schema's lock first, then the table's.
Every first use takes the schema lock before `CREATE SCHEMA`, a model table's
and an engine table's alike, so any number of processes can start on a schema
that does not exist yet without racing each other into `pg_namespace`
(`test_engine_table_first_use_is_race_free_on_a_fresh_schema`). No manual
step is needed. Anything else raises
`SchemaDriftError` with the diff instead of writing: a dropped or retyped
column, a key change, a schema written by a newer popoto, or a table popoto
did not create. Set `POPOTO_SCHEMA_AUTO=0` to turn off even the automatic
create and additive changes.

## Search (M2b)

A model's search fields keep their state in the same database as its
records, written by the record's own `INSERT … ON CONFLICT` statement as
data-modifying CTEs: one round trip, one transaction. A delete cascades to
the postings and the token rows. Nothing touches Redis or the filesystem.

```python
class Memory(popoto.Model):
    memory_id = popoto.AutoKeyField()
    project_key = popoto.Field(type=str)
    relevance = popoto.SortedField(type=float, partition_by="project_key")
    content = popoto.StringField(default="")
    lexical = BM25Field(source="content")
    embedding = EmbeddingField(source="content")     # provider: 1536 dimensions
    bloom = ExistenceFilter(fingerprint_fn=lambda m: m.content)
    seen = FrequencySketch(fingerprint_fn=lambda m: m.content)

    class Meta:
        backend = "postgres"
```

compiles to (beside the record table's own columns and indexes):

```sql
"embedding__vec" vector(1536), "embedding__model" text, "embedding__hash" text
CREATE INDEX "memory__hnsw__embedding__idx" ON "popoto"."memory"
    USING hnsw ("embedding__vec" vector_cosine_ops);
CREATE TABLE "popoto"."memory__lexical__post" (
  "scope" text NOT NULL, "term" text NOT NULL,
  "_pk" text NOT NULL REFERENCES "popoto"."memory" ("_pk") ON DELETE CASCADE,
  "tf" integer NOT NULL, PRIMARY KEY ("scope", "term", "_pk"));
CREATE TABLE "popoto"."memory__lexical__dl" (
  "_pk" text PRIMARY KEY REFERENCES "popoto"."memory" ("_pk") ON DELETE CASCADE,
  "scope" text NOT NULL, "len" integer NOT NULL);
CREATE TABLE "popoto"."memory__bloom__tok" (
  "token" text NOT NULL,
  "_pk" text NOT NULL REFERENCES "popoto"."memory" ("_pk") ON DELETE CASCADE,
  PRIMARY KEY ("token", "_pk"));
CREATE TABLE "popoto"."memory__seen__cnt" (
  "token" text PRIMARY KEY, "count" bigint NOT NULL);
CREATE TABLE "popoto"."memory__embedding__vec" (
  "_pk" text PRIMARY KEY REFERENCES "popoto"."memory" ("_pk") ON DELETE CASCADE,
  "scope" text NOT NULL, "v" vector(1536) NOT NULL);
ALTER TABLE "popoto"."memory__embedding__vec" ALTER COLUMN "v" SET STORAGE PLAIN;
```

`tests/postgres/test_postgres_search.py::test_compiled_search_ddl_is_pinned`
pins the full statement list.

**The scope.** A model's scope is the `partition_by` of its
`DecayingSortedField` (Valor's `project_key`), else of its first partitioned
`SortedField`. With neither, every record is in scope `''`. A `NULL` scope
column is scope `''` too, so it is never dropped. The scope leads the
postings key. A save that changes a scope column, including
`save(update_fields=["project_key"])`, moves the record's postings and its
narrow vector row in the same statement.

The postings, document length and narrow vector row always carry the scope of
the row that is *stored* after the save. A scope column the save writes (every
column on a full save, the listed ones with `update_fields`) takes the
instance's value; one it does not write keeps the stored value, read from the
record row inside the save's own statement, after the record lock. So
`save(update_fields=["text"])` on an instance whose `project_key` was changed
but never saved, or was changed by another writer after this instance was
loaded, re-indexes the text in the scope the row actually holds. A scope part
whose type has no exact SQL spelling of `str(value)` (anything but `str`,
`int` and `bool`) is taken from the record's existing side rows, which already
hold the stored scope. Pinned: `tests/postgres/test_postgres_search.py::test_a_partial_save_keeps_the_side_tables_in_the_stored_scope`
and `::test_a_partial_save_takes_each_unwritten_scope_column_from_the_store`.

**Concurrent saves of one record.** The data-modifying CTEs of a save share
one snapshot, so under `READ COMMITTED` a second save of the same record
whose statement started before the first one committed could not see the
rows the first one wrote, and would leave them behind. A save therefore
takes the record's advisory lock first, as its own statement in the same
message:
`SELECT pg_advisory_xact_lock(hashtextextended('popoto:rec:<table>:<_pk>', 0))`.
The rewrite then takes its snapshot after any earlier writer of that record
has committed, which also covers two racing first inserts. The backfill
takes the same locks, sorted by `_pk`. Pinned by
`test_concurrent_saves_of_one_record_leave_no_stale_rows`, which interleaves
two saves behind a held row lock in 8 runs, and by its control, which shows
the same interleaving goes stale without the lock.

**One lock order for every writer.** Every statement that writes a record
row takes that record's advisory lock first, not only a save: `delete`,
`atomic_increment`, a capped-list push, `touch`, `update_confidence`, the
access tracker's writes, `on_context_used`'s `FOR UPDATE` and an
`ExistenceFilter` row. The order is any `(model, field)` lock (a
`ValidityField`'s, M3), then the record-key locks sorted by `_pk`, then the row locks in `_pk`
order. A record's key lock is always the first lock taken on it, so a
transaction that runs `update_confidence(x, pipeline=tx)` and then
`x.save(pipeline=tx)` queues a concurrent `x.save()` behind it instead of
deadlocking with it. Transactions that each take several records in
different orders can still deadlock; that surfaces as
`BackendRetryableError`. Pinned by
`test_a_confidence_update_then_save_cannot_deadlock_a_save` and its control.
A record delete on a model with a symmetric `CoOccurrenceField` (M4) writes
rows in its *partners'* edge sets too (the reverse edges), so it takes the
partners' record-key locks with the deleted keys', in one sorted sequence,
before it touches a row. The lock statement runs twice: the first takes the
locks in order with the partners its snapshot saw, the second picks up a
partner linked while the first waited (none, normally; once the deleted keys
are held no new partner can appear, since a `link` takes both keys' locks).
Pinned by `tests/postgres/test_postgres_graph.py::test_a_delete_cannot_deadlock_a_transaction_holding_a_partner_set`
and its control, where a transaction holding a partner's set and then linking
into the deleted record deadlocks a delete that skips the partners' locks.

### BM25

The tokens come from popoto's own tokenizer, the one Redis uses; Postgres
text search is never involved. `BM25Field.search` computes BM25_SEARCH_LUA's
formula operation for operation, in double precision:
`idf = ln((N - df + 0.5) / (df + 0.5) + 1)` and
`tf · (k1 + 1) / (tf + k1 · (1 - b + b · dl / avgdl))`, summed over the query
terms in query order, with `k1 = 1.2` and `b = 0.75`. `N`, `avgdl` and `df`
are **corpus-wide**, as on Redis. Results come back in the same order (ties
by key, bytewise) and with Lua's `%.14g` score representation. The
statistics are counted live from the document-length table, so there is no
running `avgdl` to drift and `recompute_stats` has nothing to do.
`get_idf`, `filter_selective_tokens` and `Query.keyword_search` follow.
`BM25Field.search(allowed_keys=)` keeps only the best
`max(limit, SCOPED_SEARCH_FETCH_CAP)` records overall, which is the window
Redis's widening loop reaches before it stops.

### Embeddings

`EmbeddingField` needs the **pgvector** extension. popoto never creates or
drops an extension, so run `CREATE EXTENSION vector` in the database (as a
role allowed to). Its schema must be on the connection's `search_path`; the
default, `public`, is. A model with an `EmbeddingField` raises
`BackendCapabilityError` naming the fix on its first use when either is
missing. The `pgvector/pgvector:pg18` image ships the extension.

The dimension `d` is read from the field's provider at the model's first
use. Configure the provider (`popoto.configure`) before then. With no
provider the column is an unconstrained `vector` with no HNSW index, and
every search is exact.

A save embeds the source text exactly as `on_save` does on Redis: a provider
failure raises `RuntimeError` and nothing is written. The vector is stored
as float32, the precision of Redis's `.npy` files. Reads that hydrate records
never fetch it. A save that names the source in `update_fields` re-embeds,
as it re-indexes BM25.

**The narrow vector table.** Each vector is also kept in
`<table>__<f>__vec (_pk, scope, v)`, written in the save statement and
cascaded on delete. In the record table a 1536-d vector (6 kB) is TOASTed,
so an exact scan reads two heaps and de-TOASTs every row in scope. The
narrow table stores `v` inline (`STORAGE PLAIN`, up to
1,900 dimensions, so the row fits an 8 kB page), and a scope's vectors are
one index range on `scope` plus their heap pages. The exact path reads it;
the HNSW index stays on the record table's column. The cost is a second
copy of every vector, about 6 kB a row at 1536-d.

**The vector arm** (`_get_vector_scores`, behind `ContextAssembler`'s hybrid
path) returns cosine similarity `1 - (v <=> q)`, positive only, as Redis's
numpy path does. While at most `Defaults.PG_VECTOR_EXACT_MAX` (5,000) rows in
scope hold a vector, it scans them exactly, ordering by `(v <=> q) + 0`,
which no index can serve. Above that it uses the HNSW index with
`SET LOCAL hnsw.ef_search = Defaults.PG_HNSW_EF_SEARCH` and
`hnsw.iterative_scan = relaxed_order`. When HNSW returns fewer rows than
`min(limit, rows with a vector)`, the **recall guard** re-runs the arm
exactly (on the narrow table), so a pathological query costs latency rather
than coming back empty.
`EmbeddingField.load_embeddings` reads the column (normalized as before, in
`_pk` order). `garbage_collect` and `sweep_stale_tempfiles` return `0` and
touch no file: the vector goes with its row.

**Backfill.** After a save whose own embedding succeeded, the engine embeds
up to `Defaults.PG_BACKFILL_BATCH` (4) rows of the same scope that have no
vector, or whose vector another model made. It runs after the commit,
outside any `transaction()`, and stops at
`Defaults.PG_BACKFILL_BUDGET_SECONDS` (1 s) of wall clock. A provider call
that outlives the budget is abandoned, so a slow provider cannot hold
`save()` past it. A row is written only while its source still hashes to the
text that was embedded and the row is still in the scope, so a concurrent
edit is never given a stale vector. It writes the narrow row in the same
statement, under the same record locks a save takes. Errors are logged and
swallowed. The busy flag is process-wide: while one backfill's provider
call is still out (abandoned at the budget but not yet returned), every
other model's backfill skips.

### Membership

`ExistenceFilter` and `FrequencySketch` are **exact** on Postgres (a
documented strictness): `might_exist` has no false positives, and
`get_frequency` returns the true count. The token table holds each live
record's tokens and forgets a record when it is deleted. The count table
counts saves and never decrements, as the sketch never does.
`fill_ratio` reports the fill a bloom of the field's parameters would have
after the distinct tokens stored, `1 - e^(-k·n/m)`.

### Fusion and `recall()`

`Query.fuse` is unchanged Python RRF. On Postgres the builder's filters
(including plain fields) scope the fused set through one `SELECT`, and the
survivors hydrate through one `load`.

`Query.recall()` is new and Postgres-only (`[PG-only]`; on a Redis-bound
model it raises `BackendCapabilityError`):

```python
hits = Memory.query.recall(
    "kubernetes upgrade",
    scope="valor",                       # the partition_by value
    filters={"agent_id": "a1"},          # KeyField / IndexedField equality
    tags=["ops"], tags_mode="any",       # the TagField: && ("any") or @> ("all")
    weights={"bm25": 1.0, "vector": 0.4},
    limit=10,
)
for memory, score in hits: ...
```

The BM25 and vector arms rank `max(limit, Defaults.PG_RECALL_ARM_DEPTH)`
deep and are fused by weighted RRF, `Σ w / (60 + rank)`. A first, index-only
statement counts the scope's rows with a vector, which picks the vector
path. On the exact path one SQL statement then runs both arms and the
fusion, and returns the fused records' rows. On the HNSW path the vector
arm runs as its own statement, with the recall guard, first. That arm needs
statement-wide planner settings to keep the planner on the HNSW index, and
in a combined statement those settings would push the BM25 arm onto a full
index scan. Its ranking then joins the fused statement as a ranked list.
A record that leaves the scope between the HNSW statement and the fused one
is filtered out again in the fused statement, like every ranked list that
joins it.
Every scoping argument is optional, and they compose. BM25's statistics are
**per scope** by default (`bm25_stats="scope"`): on a shared database, one
agent's corpus must not skew another's IDF. `bm25_stats="corpus"` uses the
corpus-wide statistics `BM25Field.search` uses.

A model with one `DecayingSortedField` adds a third arm, `weights["decay"]`
(default 1.0): `top_by_decay`'s ranking (`DECAY_SCORE_LUA` over the M2a
columns: the field's clock, its `base_score_field`, and `<f>__conf`
modulation when the field resolves a `ConfidenceField`), computed in the
fused statement itself over the same scope and filters. It adds no round
trip.

**`Query.top_by_relevance(scope=None, limit=10, *, as_of=None)`** (`[PG-only]`, #758 D8)
returns `[(instance, score)]` ranked by decay × confidence in SQL, the score
`top_by_decay` ranks by. It replaces reading a decay sorted set with a raw
`ZREVRANGE`. `scope` is the decay field's `partition_by` value; `None` ranks
every partition together. On a Redis-bound model it raises
`BackendCapabilityError`; use `top_by_decay` there.

**Both read through the validity gate (M3).** On a model with a
`ValidityField`, `top_by_relevance` and `recall` leave out a record closed at
or before `as_of` and one that starts after it, as `top_by_decay`,
`composite_score` and the assembler do. `as_of` is epoch seconds and defaults
to now; `Query.recall(..., as_of=)` and `top_by_relevance(..., as_of=)` take
it, so `as_of` in the past returns the records that were valid then. In
`recall` the gate is ANDed onto the domain of **every** arm (BM25, vector,
decay), so a superseded record cannot enter through the lexical arm either.
`Defaults.VALIDITY_GATING_ENABLED = False` turns it off, and a model without a
`ValidityField` is unaffected.

**`composite_score(similarity_boost=…)`** works on Postgres too (M2b). The
mapping is one more arm, weight 1.0 and last, as its temporary set is in the
Redis `ZUNIONSTORE`. `semantic_search(indexes=…)` uses it, and on a model
with a `ValidityField` the validity mask applies to it as to every arm (M3;
pinned two-leg by `tests/test_semantic_search.py::TestSemanticSearchWithIndexes`). The order is the
same on both backends; with three or more summed arms a score can differ by
up to 2 ulp, because `ZUNIONSTORE` adds the smallest input set first while
Postgres adds the arms in order. `co_occurrence_boost=` is an arm too (M4):
weight 1.0, after the indexes and before `similarity_boost`, the position its
temporary set takes in the `ZUNIONSTORE`.

## ContextAssembler (M2c)

`ContextAssembler` runs on a Postgres-bound model with no option of its own,
and returns the same ranked records as on Redis. It issues **no Redis
command**: `tests/postgres/test_postgres_assembler.py` patches the Redis
connection layer to record and refuse every command, then runs a Valor-shaped
model through `assemble()` in every mode with scopes, tags, a budget, the
gate, `assess_quality` and `emit_trace`, plus `assess()` and
`on_context_used()`, and asserts the record is empty.

What each stage runs on Postgres:

| Stage | Postgres |
|---|---|
| ExistenceFilter short-circuit | `might_exist` on the exact token table (M2b) |
| Hybrid / lexical pull | the same body as on Redis: `BM25Field.search` (`keyword_search` with **corpus-wide** statistics), the vector arm (`vector_search`), and `fuse` with `_fusion_weights`. The BM25 window is narrowed by the scope's indexed filters from one id-only `SELECT`, as `filter_for_keys_set` narrows it on Redis |
| Zero-signal fallback, composite pull, `assess` probe | `composite_score`: `rank_composite`, one `SELECT` (M2a) |
| Tag scoping | one id-only `SELECT` with `&&` (any) / `@>` (all) |
| Score proxy (`assess_quality`, `emit_trace`, `assess`) | the partition's `rank_decayed` for a decay field, with the model's validity gate at now (M3: a record closed now or not yet started scores `None` and counts as stale, as on Redis, which runs `DECAY_SCORE_LUA` with the gate; pinned two-leg by `test_the_score_proxy_gates_on_validity_at_now`), the column for a plain sorted field |
| Post-effects | one transaction: the rows of the selected and the suppressed records locked in `_pk` order behind their record-key locks, one staged-read `UPDATE`, one confidence `UPDATE` for every suppressed candidate |

**Why the hybrid path does not call `recall()`.** `recall()` ranks each arm
inside the scope; the assembler ranks the vector arm across the whole model
and the BM25 arm inside the `SCOPED_SEARCH_FETCH_CAP` window, and `fuse` then
keeps the in-scope records. RRF sums those ranks, so the same keys in the same
order on both backends needs the same arms. The BM25 statistics are
corpus-wide on that path, never `recall()`'s per-scope default (architect
decision 3).

**The parity evidence.** The retrieval-quality fixture (200 records, 20
queries, `retrieval_mode="auto"`) returns the same keys in the same order,
the same formatted output and the same token count on both legs, through the
lexical path, and through the hybrid path partitioned by `agent_id` with each
record's text suffixed `(memory N)` (`test_postgres_assembler.py`): with the
fixture's texts as written, several records share a text and so a vector, and
the hybrid path's parity then holds only up to the vector-arm tie row below. `scripts/probe_assembler_parity.py` builds
random corpora on both legs and compares `assemble()` (keys, formatted output
and token count, metadata, trace, quality, and the post-effects' confidence and
staged-read state), `on_context_used()` and `assess()`; its classes are in the
PR that introduced M2c, and `test_postgres_assembler.py` runs a 12-shape slice
of it in CI.

```bash
REDIS_URL=redis://localhost:6379/13 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_assembler_parity.py --seeds 1 2 3 --shapes 100
```

The differences the probe classifies are rows elsewhere on this page, seen
through the assembler: the exact `ExistenceFilter` (a cue whose only record was
deleted or rewritten short-circuits on Postgres, not on Redis), a reload after
`touch()` (after an `acted` outcome the formatted `relevance` of that record
differs, and a token budget can then admit a different record), and the vector
arm's tie order (records with the same text have the same vector, which Redis
returns in file-listing order and Postgres by key).

An outage raises `BackendUnavailableError` from `assemble()`, as a Redis
outage raises `ConnectionError`; it is in
`popoto.recipes.context_assembler.OUTAGE_ERRORS`. That is the retrieval
path's rule, not every helper's: the quality helpers behind
`assess_quality` and `assess()` (score spread, feeling-of-knowing,
staleness) catch every exception and degrade, on both backends (unchanged
from Redis), so `assess()` during a `rank_decayed` outage returns a degraded
quality rather than raising.

**Post-effects fail differently.** On Postgres the post-effects are one
transaction (above), so they are all-or-nothing: a statement that fails drops
the staged reads and every suppression signal of the call, with one warning.
On Redis a `TypeError` or `ValueError` is swallowed per candidate and the
pipeline carries on with the rest. And Postgres stages reads only for a model
with `AccessTrackerMixin` (the `_at_member` check), where Redis calls each
record's `on_read`: a user-defined `on_read` on a model without the mixin runs
on Redis but not on Postgres. Neither is reachable with the shipped models.

## Co-occurrence graph (M4)

`CoOccurrenceField` keeps, on Redis, one sorted set per source key; on
Postgres each field has one edge table, a row per directed edge:

```sql
CREATE TABLE "popoto"."memory__associations__edge" (
  "src" text NOT NULL, "dst" text NOT NULL,
  "weight" double precision NOT NULL, PRIMARY KEY ("src", "dst"));
```

A symmetric field writes both directions, as the Lua writes both sets, so the
two weights of a pair can differ exactly as they can on Redis (a prune or a
`weaken_all` touches one side). There is no foreign key: Redis links any two
key strings, records or not. Deleting a record removes its own edges and,
for a symmetric field, the reverse edge in every set it linked to, as CTEs of
the record's `DELETE`, behind those partners' record-key locks as well as its own (see "One lock order for every writer"). Every tie-break is `COLLATE "C"`, the sorted set's
member order. Pinned: `tests/postgres/test_postgres_graph.py::test_the_edge_table_ddl_is_pinned`.

**Writes.** Each one is a statement behind the record-key advisory locks of
the sets it writes (`src`, and `dst` when symmetric), sorted: the backend's
one lock order. That lock is what makes a link's count-then-prune atomic, as
the script is: no committed state of a set ever holds more than `max_edges`
edges (`test_concurrent_links_to_one_set_never_exceed_max_edges`, and its
deterministic interleaving with a control that overflows without the lock).

| Call | Lua | Postgres |
|---|---|---|
| `link` | `LINK_WITH_PRUNE_LUA` per direction: an existing edge keeps its weight; a new one is added and, past `max_edges`, the lowest `count - max_edges` by `(weight, member)` are removed | the same, in one statement: the prune is decided over the set as it would be after the insert, so a new edge that ranks lowest is never inserted |
| `strengthen` | `min(old + delta, cap)`, a missing edge as `0`, no prune | `INSERT … ON CONFLICT DO UPDATE` with the same `CASE` |
| `unlink` | `ZREM` | `DELETE` |
| `weaken_all` | every edge times `factor`; below `0.001`, removed | one statement: `DELETE` the pruned, `UPDATE` the rest |

The arithmetic is the Lua's, in `double precision`, and the replies are the
Redis ones: `link` returns the script's number reply, which Redis sends as an
integer (the weight truncated toward zero, so `link(…, 0.5)` returns `0.0` on
both); `strengthen` returns `tostring(new)` (`%.14g`). Where Postgres would
raise on a float overflow or underflow and C saturates, the statement guards
the one product that can (an underflow in `weaken_all`, whose edge is removed
either way), and an input past `1e300`, infinite or NaN takes an exact path:
the same steps in Python floats, which are C doubles, in one transaction.

**`propagate`** is `PROPAGATE_BFS_LUA`. The Lua runs a FIFO queue with a
visited map; an entry expands through the node's top `max_edges` neighbours
by `(weight, member)` descending unless an earlier entry for the node was at
least as heavy, and a neighbour is reached with `w * decay * min(edge, cap)`,
kept when that is at least the threshold. With a positive threshold every
weight is positive and the step is monotone, so a skipped entry is dominated
by an earlier, no-deeper one: each result is the heaviest walk of at most
`depth` hops whose weights stay at or above the threshold. That is one
`WITH RECURSIVE` statement, a layer per iteration:

```sql
WITH RECURSIVE
  "_seed" AS (SELECT DISTINCT u AS pk FROM unnest($seeds::text[]) AS u),
  "_walk" (pk, w, d) AS (
    SELECT pk, 1::float8, 0 FROM "_seed"
    UNION ALL
    SELECT pk, w, d FROM (                     -- each node's heaviest arrival
      SELECT pk, w, d, row_number() OVER (PARTITION BY pk ORDER BY w DESC) AS rk
      FROM (SELECT n.dst AS pk, safe_mul(k.w * $decay, least(n.weight, $cap)) AS w,
                   k.d + 1 AS d
            FROM "_walk" AS k
            CROSS JOIN LATERAL (               -- the top max_edges neighbours
              SELECT dst, weight FROM <edges> WHERE src = k.pk
              ORDER BY weight DESC, dst COLLATE "C" DESC LIMIT $max_edges) AS n
            WHERE k.d < $depth) AS h
      WHERE w >= $threshold AND w <> 'NaN') AS r
    WHERE rk = 1)
SELECT pk, max(w) FROM "_walk"
 WHERE d >= 1 AND pk NOT IN (SELECT pk FROM "_seed")
 GROUP BY pk ORDER BY max(w) DESC, pk COLLATE "C"
```

`safe_mul` is the saturating product of the decay SQL (M2a).

That statement only runs while it expands at most
`Defaults.PG_GRAPH_RECURSIVE_MAX_LAYERS` (2) layers, i.e. `ceil(depth) <= 2`
(the default `depth=2`). A recursive CTE sees only the previous layer, so it
cannot apply the visited map: it re-expands every reached node on every layer
until `depth` runs out or the weights drop under the threshold. Up to two
layers that is exactly the pruned work (layer one expands the seeds, layer two
every first arrival); past that it grows with depth × fan-out where the Lua
stops. The #781 review measured a 400-node clique at `depth=50, decay=0.99` at
24.9 s against Redis's 0.10 s, and at larger depths (or a decay near 1) the
statement ran into `statement_timeout`, which the outage contract reports as
`BackendUnavailableError`. A deeper call therefore runs one statement per
layer instead: each frontier node's top `max_edges` neighbours, the same
product and threshold, each node's heaviest arrival (`GROUP BY`), and
between layers the Lua's visited rule -- a node goes on only when it arrived
strictly heavier than at any earlier layer. A pruned arrival is dominated by
the earlier one, so the answer is the recursive statement's; the work is at
most the Lua's, and the loop stops after the last layer that improved a node
whatever `depth` is (the contraction guard keeps `decay * cap < 1`, so an
improving walk is a simple path: at most one layer per node reached). A
statement timeout is never what bounds a graph read. Measured on the review's
graphs (macOS arm64, PostgreSQL 18.6, Redis 8.10, load average 4.2-4.9,
median of 5 runs; 7 for the probe shapes):

| graph, call | Redis | Postgres |
|---|---|---|
| 400-node clique, `depth=50, decay=0.99, threshold=0.01` | 77 ms | 300 ms |
| same, `depth=1000, threshold=1e-6` | 86 ms | 295 ms |
| same, `depth=1e6, decay=0.999` | 81 ms | 290 ms |
| same, `depth=1e6, decay=0.5, threshold=1e-280` | 84 ms | 288 ms |
| same, `depth=inf, decay=0.99` | 82 ms | 289 ms |
| the review probe's five ≤12-node shapes at `depth=1e9, decay=0.999999` | 0.21-0.32 ms | 1.15-1.55 ms |
| 50k edges, fan-out 10, `depth=2` / `3` / `5`, `threshold=0.01` | 2.7 / 11 / 35 ms | 1.8 / 10 / 61 ms |

The deep calls run their layers on one pooled connection in one read
transaction, so a layer costs one round trip. Every answer is identical to Redis's. Pinned:
`test_a_dense_clique_at_any_depth_answers_inside_the_statement_timeout`
(a 40-node clique at `depth=1e9` with the timeout lowered to 3 s; the health
record sees no failure), `test_a_deep_propagate_prunes_as_the_visited_map_does`
and `test_the_three_bfs_paths_agree`.

Outside that domain -- a threshold at or below `0` (or under `1e-290`), a negative or
infinite `decay_per_hop` -- the step is not monotone and the visited map's
order decides the answer, so the backend replays the Lua's queue exactly in
Python, over the same neighbour lists fetched one layer per statement.
Scores come back through `%.14g`, as the script's `tostring` sends them.
`get_linked` is `ZREVRANGEBYSCORE +inf <min> LIMIT 0 <limit>`: one indexed
read of the source's rows, the stored weights unclamped (`graph_expand` at
depth 1), with `(` for an exclusive minimum, `limit=0` empty and a negative
limit everything.

`recipes/graph_traversal.traverse` runs unchanged on top: `propagate`, the
relationship walk (M1.1's `sample_related_keys`), and the confidence and
decay modulation (M2a/M2c). `ContextAssembler`'s graph arm and
`composite_score(co_occurrence_boost=)` work on a Postgres model as on Redis.

**The seeded probe.** `scripts/probe_graph_parity.py` replays the same random
sequence of writes on both legs -- weights from typical to `-inf`, ties and the
cap; deltas from `1e-12` past the cap; factors `0`, tiny and `1`; record
deletes and `import_state` (NaN weights included) -- comparing every return,
then each key's full edge set, `get_linked` (`limit=None` and a NaN bound
included), `propagate` (depths up to `1e9` and `inf`, decays up to `0.999999`)
on all three Postgres paths, `export_state`, `composite_score` with the boost
and `traverse()`. Its classes are in its docstring and the PR that introduced
M4; the documented ones are the M4 rows of the table above.
`tests/postgres/test_postgres_graph.py` runs a 25-shape slice in CI.

```bash
REDIS_URL=redis://localhost:6379/13 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_graph_parity.py --seeds 1 2 3 --shapes 200
```

## Recipes, mixins and the queue (M4)

The recipes reach storage through field and model methods (#630), so a
Postgres-bound model runs them unchanged. The few methods that keep state
outside a model's own hash on Redis are `field_call` adapters on Postgres
(`popoto/backends/postgres/recipes.py`), several of them backed by engine
tables created on first use, like `popoto_recall_proposal`:

| Method | Redis | Postgres |
|---|---|---|
| `Model.idle_seconds` | `OBJECT IDLETIME`: whole seconds since the key was last read or written | whole seconds since the later of the row's last write (`_updated_at`) and, with `AccessTrackerMixin`, its last *confirmed* read (`_last_accessed`); `None` with no row, or an expired one (M5) |
| `SortedFieldMixin.count` / `members` / `score` | `ZCARD` / `ZRANGE` / `ZSCORE` on the partition's sorted set | the partition's rows, in `(value, _pk COLLATE "C")` order, with `ZRANGE`'s inclusive and negative ranks; `score` converts the stored value as the sorted set scores it |
| `counters.increment` / `read` (`model=`) | `INCRBY` / `GET` on a string | `popoto_counter (key, value)`: `INSERT … ON CONFLICT DO UPDATE … RETURNING` |
| `TombstoneStore` (`MemoryLifecycle`'s archive) | `$TOMB:{Model}:data` hash + `:index` sorted set | `popoto_tombstone (model, member, entry, ts)` |
| `TombstonePriorStore` (the negative prior) | `$TOMBPRIOR:{Model}:*` | `popoto_tombstone_prior (model, digest, burials, ts)` + `popoto_tombstone_stats` |
| `NeverRecordMixin`'s audit log | `$NR:{Model}:counts` hash + `:drops` capped list | `popoto_never_record_count (model, reason, count)` + `popoto_never_record_log (model, seq, entry)`, capped at `NR_TOMBSTONE_LOG_MAX` |
| The question queue's token bucket and propose lock | `$QuestionBucket:{agent}` (with a TTL) and `SET NX PX` | `popoto_question_bucket (agent, last_turn, expires_at)` and `popoto_lease (key, token, expires_at)`: an expired row reads as absent |

`DefaultMemory`'s eviction counts and pages its partition through the sorted
reads above and records the eviction in `popoto_counter`, so a
Postgres-bound `DefaultMemory` issues no Redis command; `MemoryService`
(the Redis-only integration) does not see that counter.
`MemoryLifecycle.tombstone()` archives the record as `restore()` will decode
it: on Redis the raw hash bytes, on Postgres the stored row encoded the way a
Redis save would write it, so `decode_popoto_model_hashmap` restores it
unchanged.

**The question queue.** Delivery is `_DELIVER_LUA` as one statement behind
the agent's bucket advisory lock, so two deliveries for one agent serialize
and the budget is exact under any concurrency (the 8-thread hammer passes on
both legs):

```sql
SELECT pg_advisory_xact_lock(hashtextextended('popoto:qq:<schema>:<agent>', 0));
WITH b AS (SELECT last_turn FROM popoto_question_bucket
            WHERE agent = $agent AND expires_at > <now>),
     pick AS (SELECT t._pk, coalesce(t.ask_count, 0) + 1 AS n, k.ord
                FROM unnest($candidates::text[]) WITH ORDINALITY AS k(pk, ord)
                JOIN question_candidate AS t ON t._pk = k.pk
               WHERE NOT EXISTS (SELECT 1 FROM b WHERE $turn < b.last_turn + $K)
                 AND t.status = ANY($deliverable)
                 AND (t.cooldown_until IS NULL OR NOT ($turn < t.cooldown_until))
               ORDER BY k.ord LIMIT 1
                 FOR UPDATE OF t SKIP LOCKED),
     upd AS (UPDATE question_candidate AS t
                SET status = 'delivered', ask_count = pick.n, delivered_turn = $turn
               FROM pick WHERE t._pk = pick._pk RETURNING pick.ord, pick.n),
     bk AS (INSERT INTO popoto_question_bucket (agent, last_turn, expires_at)
            SELECT $agent, $turn, <now> + $ttl FROM upd
            ON CONFLICT (agent) DO UPDATE
               SET last_turn = EXCLUDED.last_turn, expires_at = EXCLUDED.expires_at)
SELECT ord, n FROM upd;
```

The bucket is written only when a candidate was claimed, as the script
writes it, so a refused or empty delivery never spends the budget. The
statement waits on nothing but the bucket lock, which no record writer takes,
so it cannot join a deadlock cycle. The claim (`_CLAIM_LUA`) is an
`UPDATE … WHERE status = ANY($allowed) AND <guard> IS NOT DISTINCT FROM … RETURNING`
behind the candidate's record lock. Pinned by
`tests/postgres/test_postgres_question_queue.py` (a deterministic
interleaving of two deliveries in one turn, and its control without the
bucket lock, which delivers twice).

**`NeverRecordMixin`** refuses on Postgres exactly where it refuses on
Redis, in `Model.save()` before the backend is asked to write: a blocked save
returns `False`, no row is written, and the content-free audit entry goes to
the backend's tables. `tests/test_never_record_firewall.py` runs on both
legs; on the Postgres leg its "no trace anywhere" sweep covers every table of
the schema and Redis.

**`AppendOnlyMixin`** checks for the record through the backend. Inside a
Postgres `transaction()` the check runs on the transaction's connection, so
two saves of one key in one transaction refuse the second (on Redis the
"intra-pipeline" shape stays open, as its module docstring records).

**`ProvenanceJournal`** runs its pre-flight unchanged and then appends --
and, for a closing kind, closes the target's interval -- in **one
transaction** (`SupersessionProtocol.save_and_invalidate`, M3): its own, or
the caller's when the backend's unit of work is passed as `pipeline=`. The
close's outcome is known at the call, so `AnnotationResult.target_closed` is
the truth (on Redis a caller pipeline reports `None`, "unknown until you
execute", with a `close_index` to read after `EXEC`), and a close that fails
rolls the annotation back with it (on Redis the queued annotation is kept:
the M3 row below). Any other `pipeline=` object, a Redis pipeline included, is
refused with `ValueError` before anything is written.
`AppendOnlyMixin.hard_delete` removes the row (its open-claim pointer
cascades) and clears the `<f>__supersedes` / `<f>__superseded_by` columns of
the records that named it, the value side of the Redis chain hashes. The
reconciler's statement-vector cache is `popoto_embedding_cache (model,
member, vector)`, beside the entries, and `erase_entry` drops it there.
`JournalEntry` composes `EventStreamMixin`: on Postgres its mutation log is
the backend's events table (M5), appended in the journal write's own
transaction (a caller's `transaction()` or `popoto.batch()` included), and the reconciler's `StreamConsumer` trigger
(`reconciliation_consumer`, which names `JournalEntry` as its model) reads it
there. A rolled-back write leaves no entry, and a Postgres-bound journal and
its reconciler send Redis no command at all. Pinned by
`tests/postgres/test_postgres_journal.py` and
`tests/postgres/test_postgres_events.py::test_a_postgres_journal_and_its_reconciler_send_redis_nothing`.

**Not on Postgres yet.** `MemoryTelemetry`'s `AssemblyEvent` declares
`Meta.ttl`, refused until M5, so a Postgres-bound telemetry recorder fails
open (it records nothing). `SubconsciousMemory(auditable_extraction=…)` keeps
its decision log in Redis (`extraction/decision_log.py` is Redis-only, plan
§1), so on a Postgres-bound model it raises `BackendCapabilityError` at
construction.

The seeded probe, `scripts/probe_queue_parity.py`, replays random queue
sessions on both legs (proposals with dedup, use, delivery with and without
cues, answers, expiry, prune, regressed turns) and compares every return,
the stored candidates, the targets' confidence and the bucket;
`test_postgres_question_queue.py` runs a 20-shape slice in CI.

```bash
REDIS_URL=redis://localhost:6379/13 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_queue_parity.py --seeds 1 2 3 --shapes 200
```

## Event streams and pub/sub (M5)

`EventStreamMixin`, `StreamConsumer`, `Publisher` and `Subscriber` run on
Postgres with no Redis command at all. The stream lives where the model that
writes it lives: a Redis stream for a Redis-bound model, the backend's events
tables for a Postgres-bound one.

**The entry is part of the write.** A save (create, update, partial update)
or delete of a Postgres-bound `EventStreamMixin` model appends its mutation
entry **in the same transaction** as the record: the record and its entry
commit together or not at all. On Redis the `XADD` rides the save's
`MULTI`/`EXEC`, which applies queued commands without rollback, so this is
strictly stronger. Without a caller transaction the save takes one of its own
(the write, then the append); inside a caller's `transaction()` or a
`popoto.batch()` the entry is appended just before that transaction's
`COMMIT` (`uow.defer_stream_append`), so a batch that is `reset()` appends
nothing. `ConfidenceField.update_confidence` and `CoOccurrenceField.strengthen`
log their events to the same stream, as on Redis, and a custom
`_xadd_event(..., pipeline=batch)` joins the batch the same way.

**Stream locks are taken in one global order.** Each append locks its
stream's `popoto_stream` row until the transaction ends (that lock is what
makes ids commit in id order, below). A unit's appends therefore run at
`COMMIT`, after every record lock the unit takes, **sorted by stream key**
(stably, so one stream's entries keep their order); a write that appends in
its own transaction (a multi-record delete) sorts the same way. Two
transactions that write two `EventStreamMixin` models, or two partitions of
one, in opposite orders then queue on the first stream rather than
deadlocking. Measured with 4 threads × 40 `transaction()`s, each saving one
record of two stream models in random order: 82-86 of 160 failed with
`40P01 deadlock detected` on `popoto_stream` before the sort, 0 after; the
partitioned variant went from 35/160 to 0 (pool size 16, so that the pool
did not run out first). Pinned by
`test_postgres_events.py::test_opposite_order_transactions_never_deadlock_on_stream_locks`
and `::test_a_units_stream_appends_run_in_stream_key_order`. The cost is the owned
transaction: a save of an `EventStreamMixin` model measured p50 0.83-0.99 ms
against 0.32-0.38 ms for a plain model (3 runs × 400 saves, PostgreSQL 18.6
on localhost, Apple M1 Max, load 4.7-4.9).

**Tables** (one set per schema, created on first use):

| Table | Holds |
|---|---|
| `popoto_stream (stream, last_ms, last_seq, length, entries_added, max_del_ms, max_del_seq, created_at)` | one row per stream key: its last generated id and counters. The row is the key: an empty stream keeps it, as Redis keeps an empty stream key. Its row lock is the append lock |
| `popoto_stream_entry (stream, ms, seq, fields bytea[], created_at)` | one row per entry; `fields` is the flat `k1, v1, k2, v2` array, in order, of the bytes `XADD` would send |
| `popoto_stream_group`, `popoto_stream_consumer`, `popoto_stream_pending` | each consumer group's cursor and logical read counter, its consumers, and its pending entries list (owner, last delivery time, delivery count) |
| `popoto_stream_attempt` | `StreamConsumer`'s handler-attempt counter (the `_hattempts:` hash on Redis) |
| `popoto_pubsub_listener` | live `Subscriber` registrations, for `publish`'s count |

**Ids** are Redis's `<ms>-<seq>`: the server's millisecond clock, with `seq`
restarting at 0 each new millisecond, strictly increasing per stream (a clock
that steps back keeps the last `ms` and bumps `seq`; the last id survives
deleting the entry that holds it). Because the stream row stays locked until
the appending transaction ends, ids **commit in id order**: every snapshot
sees an id prefix of the stream, so a group cursor never skips an entry that
commits later. Explicit ids (`XADD key 5-1 …`, `5-*`, `7`) are validated with
Redis's rules and error text.

**Reading.** `Model.stream_range()`, `Model.stream_revrange()` and
`Model.stream_len()` read a model's stream on either backend, and
`popoto.streams.stream_client(model)` returns the client that holds it: the
Redis client on Redis, the backend's stream store on Postgres, with the same
redis-py methods and replies (`xadd`, `xrange`, `xrevrange`, `xlen`, `xdel`,
`xtrim`, `xgroup_*`, `xreadgroup`, `xack`, `xpending`, `xpending_range`,
`xclaim`, `xautoclaim`, `xinfo_groups`, `xinfo_consumers`, `delete`,
`exists`). Errors keep Redis's message text (`BUSYGROUP …`, `NOGROUP …`;
`XINFO CONSUMERS` answers `no such key` for a missing stream and `NOGROUP No
such consumer group 'g' for key name 'k'` for a missing group, as Redis does)
as `StreamCommandError`, and redis-py's client-side checks as
`StreamDataError`.

**`StreamConsumer`** finds its backend from `backend=` or `model=`, else from
the `EventStreamMixin` models that write its `stream_key` (and their `dead:`
streams), else the process default; `reconciliation_consumer` names
`JournalEntry`. On Postgres:

- `XREADGROUP >` advances the group's cursor under the group row's lock, so
  two consumers never receive one entry; the entries join the pending list in
  the same transaction. At-least-once delivery, retries, reclaim and
  dead-lettering are the Redis consumer's own logic, unchanged.
- `XCLAIM`/`XAUTOCLAIM` take pending rows `FOR UPDATE SKIP LOCKED`: a row a
  concurrent claimer holds is passed over rather than waited on or claimed
  twice. `XAUTOCLAIM` scans as Redis does (`count × 10` entries looked at),
  and reports entries deleted from the stream in its third list.
- **Blocking reads** (`block_ms`) wait on `LISTEN`: each append sends
  `pg_notify` on the schema's events channel inside its transaction, so the
  notification arrives when the entry is visible. `LISTEN` needs a session,
  so the wait runs on a **dedicated connection per consumer, never a pooled
  one** -- PgBouncer in transaction mode would hand a pooled connection to
  another client between statements (§3 Topology). Its DSN is
  `POPOTO_POSTGRES_LISTEN_URL` when set (point it past a transaction-mode
  pooler at the server), else the backend's own. The session `LISTEN`s
  before the read it backs up, so an append that commits in between is not
  missed. A wait is capped at one second (`STREAM_WAIT_POLL_SECONDS`), after
  which the read runs again anyway -- the fallback poll that bounds a lost
  notification -- and a dropped `LISTEN` connection is reopened on the next
  wait. `consumer.close()` (or the end of `run()`) closes the session.

**Pub/sub.** `Publisher.publish` is one `pg_notify` and `Subscriber` polls a
`PostgresPubSub`, a redis-py-shaped `PubSub` (`subscribe`, `psubscribe`,
`unsubscribe`, `punsubscribe`, `get_message`, `listen`, `close`, the same
message dicts) on its own dedicated `LISTEN` session. A publisher uses its
model's backend, `backend=`, or the process default; a subscriber `backend=`
or the default.

- One notification channel per schema carries every popoto channel: the
  payload names the channel, so channel names are not bound by Postgres's
  63-byte identifier limit. **Pattern subscriptions are matched
  client-side**, with Redis's glob rules translated to a regular expression
  and matched **byte-wise**, as Redis's `stringmatchlen` matches: `?` and
  `[^…]` consume one byte of a non-ASCII channel, a range compares bytes as
  C `char` (signed on the x86-64 and Apple-silicon builds, so bytes 0x80-0xFF
  sort below 0x00), and `[x-]` is a range ending at `]`
  (`popoto.backends.postgres.pubsub.glob_match`). Every subscriber of the
  schema receives every message and keeps what its subscriptions match.
- `NOTIFY` is transactional: a message published with the backend's unit of
  work, or a `popoto.batch()`, as `pipeline=` is delivered when that
  transaction commits (the batch's `execute()`), and never if it rolls back
  (`reset()`); a Postgres `Publisher` never queues a Redis `PUBLISH` on a
  batch. A batch an `async_*` call opened (whose connection belongs to the
  event loop) takes the same publish: the `NOTIFY` is sent from inside the
  transaction `await pipe.async_execute()` commits, as are its stream
  appends (pinned by `tests/postgres/test_postgres_async.py::test_an_async_opened_batch_carries_its_events_and_notifies_to_commit`).
  Postgres folds identical notifications sent in one transaction into
  one, so each payload carries an 8-character per-publish nonce, stripped
  before delivery: `dup, dup, other, dup` published in one transaction
  arrives as all four, in order, as a Redis `MULTI` delivers them (pinned on
  both legs by
  `tests/test_pubsub.py::test_identical_messages_in_one_unit_are_each_delivered`).
- **Payload limit.** A `NOTIFY` payload must be shorter than 8000 bytes, and
  the message travels base64-encoded beside the channel name and the nonce,
  so about 5.9 KB of message fits. A larger one is **refused** with
  `PubSubPayloadTooLarge` (a `PublisherException`) before anything is sent,
  not split: a split message could be half-delivered to a subscriber that
  joins between its parts. Publish a key and keep the body in a record.
- `publish` returns the number of live subscriptions reached, counted from
  `popoto_pubsub_listener` rows whose session pid is live
  (`pg_stat_activity`); a subscriber deletes its rows on `close()`, and rows
  of a session that died stop counting and are swept by the next subscriber
  to connect. The `LISTEN` URL must reach the same server.

**Connection cost.** `LISTEN` belongs to a session, so each blocking
`StreamConsumer` (`block_ms`) and each `Subscriber` holds **one dedicated
Postgres session** for as long as it is open, outside the pool and outside
any transaction-mode pooler; it cannot be pooled or shared. Measured: 4
blocking consumers hold 4 `LISTEN` sessions plus the pool's connections. In
the central-database topology every agent process counts against the one
server's `max_connections`: N processes × (blocking consumers + subscribers)
sessions, on top of N × `Defaults.PG_POOL_MAX_SIZE` pooled ones. Size
`max_connections` (or a session-mode pool behind `POPOTO_POSTGRES_LISTEN_URL`)
for that sum, and prefer a non-blocking consumer (`block_ms=None`, polled)
where a wake-up latency of a poll interval is acceptable. Each `publish` also
reads `pg_stat_activity` for its count, so its cost grows with the server's
session count. One multiplexed listener per process and schema, shared by
all consumers and subscribers, is a planned follow-up
(`docs/plans/sdlc-631-v2.md`, "M5 events as shipped").

**Divergences** (stream and pub/sub; Redis behaviour unchanged):

| Behaviour | Redis | Postgres |
|---|---|---|
| `MAXLEN ~ N` (the mixin's `_stream_max_length`, and `XTRIM ~`) | trims whole radix-tree nodes, so keeps at least `N`, often more | keeps exactly `N`, the newest. The probe's documented `approx_trim` class; pinned by `test_postgres_events.py::test_maxlen_trims_exactly_a_documented_divergence` |
| An append that fails at the server after an immediate save | the `XADD` failure is logged and the record stays | the save raises and nothing is kept (one transaction). An entry that cannot be *built* (an empty `_stream_name`) is logged and skipped on both, or raises with a `pipeline=` on both. Pinned: `test_a_failing_append_fails_the_save_and_keeps_no_record` |
| Error types | `redis.exceptions.ResponseError` / `DataError` | `StreamCommandError` / `StreamDataError`, same text (the Postgres backend never imports `redis`) |
| `XADD … MINID`/`LIMIT`, `XTRIM … LIMIT`, `XCLAIM … FORCE`, `XREADGROUP … CLAIM` | supported | `StreamDataError` |
| Stream keys, groups, consumers, channels | binary-safe | UTF-8 text without NUL |
| A pending row another transaction holds during `XCLAIM`/`XAUTOCLAIM` | single-threaded: never contended | skipped (`SKIP LOCKED`) |
| A message published inside a transaction | `MULTI`/`EXEC` sends it at `EXEC` | delivered at `COMMIT`, never after a rollback |
| A pub/sub message whose payload encodes to 8000 bytes or more | delivered | `PubSubPayloadTooLarge` |
| Pattern subscriptions | matched by the server | matched by each subscriber (byte-wise, the same result); every subscriber of the schema receives every message |
| Stream id parts in `(2**63 - 1, 2**64 - 1]` | valid ids | refused by every stream command with `StreamIdOutOfRange` (a `StreamCommandError`), since each part is a `bigint`; no clock reaches them. Past `2**64 - 1` both refuse with `Invalid stream ID`. `XCLAIM` with such an id raises the refusal, where Redis returns `[]`. Pinned: `test_ids_past_bigint_are_refused_a_documented_divergence` |
| A message matching several pattern subscriptions | one `pmessage` per pattern, in the order of the server's pattern table (unspecified) | the `message` (if the channel is subscribed), then one `pmessage` per pattern in subscription order. The payloads are the same as a multiset. Pinned: `test_delivery_and_confirmation_order_a_documented_divergence` |
| `unsubscribe()` / `punsubscribe()` with no arguments | one confirmation per subscription in the server's table order, count falling to 0 | confirmations in subscription order, count falling to 0 (same pin) |
| A confirmation while nothing is subscribed (an unsubscribe-all of nothing, or one reaching 0) | redis-py's `get_message` does not read while unsubscribed, so it surfaces only after the client subscribes again | returned by the next `get_message` at once (same pin) |

The seeded probe, `scripts/probe_events_parity.py`, replays random sessions
on both legs -- appends with auto and explicit ids, ranges with every bound
form, deletes, exact trims, group create/destroy, reads (new and history,
`NOACK`), acks, pending summaries and ranges, claims and autoclaims, `XINFO
GROUPS` (including `entries-read` and `lag`), key deletes, a model's saves,
partial saves, deletes and rolled-back writes, and `StreamConsumer` batches
with a failing handler (retry, reclaim, dead-letter) -- renames each leg's
ids `e1, e2, …` in creation order, and compares every reply and error.
`test_postgres_events.py` runs a 20-shape slice in CI.

```bash
REDIS_URL=redis://localhost:6379/13 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_events_parity.py --seeds 1 2 3 4 5 6 7 8 --shapes 100
```

## Long-tail fields (M5)

Three Redis features that each owned a Lua script are stored natively
(`popoto/backends/postgres/longtail.py`). Every number a script computes is
computed in SQL operation for operation, in `double precision` and in the
script's order of evaluation; `cos` and `power` are the platform `libm` on
both servers, as for M2a's decay expression. Numbers a script *replies* went
through Lua's `tostring` (`%.14g`) and numbers it *stores* through
`cmsgpack`, which packs an integral number as an integer; Postgres applies the
same rules on the way out. The seeded probe compares both legs bit for bit:

```bash
REDIS_URL=redis://localhost:6379/7 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_longtail_parity.py --seeds 1 2 3 --shapes 500
```

**`CyclicDecayField`.** The clock is the field's column; the cycles and the
pressure are columns beside it (the table above). Why parallel arrays and not
the `jsonb` the plan first proposed: an amplitude is a `double` the score
multiplies, it can be `NaN` or `±inf` (a declared `float("inf")` passes the
field's validation), and a `jsonb` number is `numeric`, which holds neither
and adds a text round trip to every row a ranking scans.

- `CYCLIC_DECAY_LUA` is an expression the rankings share -- `top_by_decay`
  (`rank_decayed`), `composite_score`'s arm for the field (and so the
  assembler's push path), and the assembler's score proxy:

  ```sql
  (SELECT y.dc + y.p FROM (SELECT z.d + z.c AS dc, z.p FROM (SELECT
      <DECAY_SCORE_LUA's expression, M2a> AS d,
      (SELECT 0 + coalesce(sum(u.a * cos(6.283185307179586 * (<now> - coalesce(u.h, 0)) / u.p)
                              ORDER BY u.o), 0)
         FROM unnest(t.f__cycle_period, t.f__cycle_amp, t.f__cycle_phase)
              WITH ORDINALITY AS u(p, a, h, o)
        WHERE u.p > 0 AND u.p <> 'NaN') AS c,
      CASE WHEN t.f__pressure_rate > 0 AND t.f__pressure_rate <> 'NaN'
           THEN t.f__pressure_rate
                * greatest((<now> - coalesce(t.f__pressure_at, <now>)) / 86400, 0)
           ELSE 0 END AS p
    OFFSET 0) AS z OFFSET 0) AS y)
  ```

  shown in its plain form. A row whose cycles or pressure lie outside the box
  where no step can leave the double range takes a clamped form instead:
  each step through M2a's saturating helpers (plus a saturating divide, and
  `cos(±inf)` = `NaN` where Postgres would raise "input is out of range"),
  and the terms folded left to right by a recursive CTE with a saturating
  add. The script's unsplit `base * pow` equals `DECAY_SCORE_LUA`'s
  `sign * |base| * pow` once `+ cyclic` (at least `0`) has turned a `-0`
  into `+0`, so the decay half is M2a's expression unchanged. The script has
  no validity gate (its `KEYS` are all taken), so a cyclic ranking ignores
  the gate on Postgres too (`TestCyclicDecayGatingGap`, and the "Known
  limitations" of [validity and supersession](validity-and-supersession.md)).
- `CYCLES_MERGE_LUA` is part of the save's upsert: a new row takes the
  declared cycles (baseline = the declared amplitude) and `last_resolved =
  now`; on conflict the period, phase and baseline columns take the
  declaration and the amplitudes merge in one sub-select --

  ```sql
  f__cycle_amp = (SELECT array_agg(CASE WHEN s.o IS NULL THEN d.a
                    WHEN s.b IS NULL OR (s.b = d.a AND s.b <> 'NaN') THEN s.a
                    ELSE d.a END ORDER BY d.o)
    FROM (SELECT x.p, x.a, x.o, row_number() OVER (PARTITION BY float8send(x.p) ORDER BY x.o) AS r
            FROM unnest(<declared periods>, <declared amps>) WITH ORDINALITY AS x(p, a, o)) d
    LEFT JOIN (SELECT y.a, y.b, y.o, float8send(y.p) AS k,
                      row_number() OVER (PARTITION BY float8send(y.p) ORDER BY y.o) AS r
                 FROM unnest(t.f__cycle_period, t.f__cycle_amp, t.f__cycle_base)
                      WITH ORDINALITY AS y(p, a, b, o)) s
      ON s.k = float8send(d.p) AND s.r = d.r)
  ```

  -- the script's period-keyed FIFO pairing and #698's three-way rule, with
  the period's bits as its `%.17g` match key. The pressure rate is
  refreshed and `last_resolved` kept (`now` for a new entry); no declared
  cycles, or a rate `<= 0`, clears that half. The upsert returns the
  previous cycles (`RETURNING old.…`), from which the save logs #698's reset
  line, word for word as on Redis. No second statement: the merge is atomic
  with the row it belongs to.
- `CYCLES_ADJUST_LUA` (`strengthen_cycle` / `weaken_cycle`) is one `UPDATE …
  SET f__cycle_amp = coalesce((SELECT array_agg(<clamp(a * factor)> ORDER BY o) …), '{}')
  WHERE f__cycle_period IS NOT NULL RETURNING …` behind the record's key lock; a
  `NaN` amplitude passes every comparison untouched, as in Lua. The
  `coalesce` is load-bearing: `array_agg` over no rows is `NULL`, and an
  empty cycles entry must stay empty, as the script's re-pack of an empty
  array does (`test_adjusting_an_empty_cycles_entry_keeps_it_empty`).
  `resolve_pressure` is one `UPDATE` of the two pressure columns.
- The factor is read as the script reads it, with Lua's `tonumber`: C
  `strtod` with only trailing space after it, so `"0x10"` is 16 and
  `"0x1p-1"` is 0.5 on both legs, while `"1_0"` and non-ASCII digits are not
  numbers (`lua_tonumber`, checked case by case against the server's own in
  `test_lua_tonumber_matches_the_servers`). A factor that reads as `nil`
  (`None`, `"abc"`, `True`) fails the script's first multiplication, so
  Redis writes nothing and raises `ResponseError`; Postgres raises
  `ValueError` with the same text, before any write -- never a `NaN`
  amplitude. As on Redis, it is reached only when there is a cycle to
  multiply: no entry still returns `[]`, and an empty entry stays empty.

**`TDValueField`.** `TD_UPDATE_LUA` is one statement behind the record's key
lock: the row locked `FOR UPDATE`, `td = target - q` and `q' = q + alpha * td`
(with `target = reward + gamma * max_future_q` evaluated first, as the
script does), and `q'` stored as the `numeric` of the script's
`tostring(q')` -- `to_char(q', '9.9999999999999EEEE')` is C's `%.13e`, the
same 14 significant digits as `%.14g`, with its trailing zeros trimmed. The
reply is `tostring(td)`. The constants enter through a `MATERIALIZED` CTE, so
the planner cannot fold a subnormal one into a plan-time "underflow".
The stored `numeric` is read as the script's `tonumber` of its text: a
`Decimal` past the double range is `±inf` and one below half the smallest
subnormal (`2**-1075`) is `±0`, at `strtod`'s exact rounding boundaries,
where a plain `::float8` cast raises "value out of range"
(`test_td_update_reads_an_out_of_range_value_as_tonumber_does`).
`recipes/policy_cache.py` runs unchanged on a Postgres-bound `PolicyEntry`.

**`PredictionLedgerMixin`.** The `$PL:{Class}:meta:{pk}` entry is a row of
`popoto_prediction_ledger (model, member, entry jsonb, lua_packed)` and the
`$PL:{Class}:errors:{part}` sorted set is `popoto_prediction_error (model,
part, member, error)`, engine tables created on first use like the M4 ones.
`RESOLVE_PREDICTION_LUA` is one statement:

```sql
WITH r AS (UPDATE popoto_prediction_ledger
              SET entry = entry || <{resolved, prediction_error, resolution_mode, resolved_at}>,
                  lua_packed = true
            WHERE model = $m AND member = $k AND jsonb_typeof(entry) = 'object'
              AND (entry->'resolved' IS NULL OR entry->'resolved' IN ('null', 'false'))
            RETURNING 1)
INSERT INTO popoto_prediction_error (model, part, member, error)
SELECT $m, $part, $k, abs($error) FROM r
ON CONFLICT (model, part, member) DO UPDATE SET error = EXCLUDED.error
RETURNING 1;
```

-- Lua truthiness for `resolved`, and `ZADD` as an upsert. The script
re-packs the whole entry with `cmsgpack`, so `lua_packed` makes a read apply
the same transformation: an integral number becomes an `int`, an empty map a
list, a `nil` field is dropped, an array with a hole a map, a table 16 deep
`nil`. A prediction holding `bytes` cannot be resolved -- Redis's cmsgpack
reads no msgpack `bin` ("Bad data format"), so the script's `pcall` fails --
and that refusal is reproduced. `get_highest_errors` is `ZREVRANGE`'s order
(error descending, ties by member bytes descending) and rank arithmetic
(`limit <= 0` counts from the end). Like the Redis keys, the ledger is not
removed when the record is deleted. The entry is `jsonb` with this module's
tags for what JSON lacks (`bytes`, a non-finite or exponent-form float, a
`-0.0`, a non-`str` key), never msgpack (plan §8).

**`ObservationProtocol`** applies the whole effects matrix on Postgres now,
in the order the Redis functions apply it and inside the batch's one
transaction: `strengthen_cycle` and `resolve_pressure` on `acted`,
`weaken_cycle` on `dismissed` and `contradicted`, `auto_resolve` on every
outcome but `deferred` (with its confidence feedback on the batch's
connection, so it never waits on the batch's own row lock), and the
pressure auto-discharge on `contradicted`, reading the confidence the batch
just updated. Every ledger and cycle write takes the record-key lock the
batch already holds, so the plan's one lock order is kept; a rolled-back
batch resolves nothing.

**Expiry and `popoto.batch()`.** On a `Meta.ttl` model every long-tail
statement that addresses the record row -- the ledger's `EXISTS` guard, the
cycle adjustment, the pressure write, the cycle export and import, and the
TD update -- carries #783's live-row filter, so an expired record is no
record to it. A `popoto.batch()` handed to `strengthen_cycle`,
`weaken_cycle`, `resolve_pressure`, `td_update` or the ledger's writers
joins the batch's transaction, as a save does: nothing lands before
`execute()`, a `reset()` writes nothing, and the record locks it takes are
the batch's (`test_long_tail_writes_join_a_batch`).

## Geo (M5)

A `GeoField` needs no extension: plain columns reproduce what Redis does
with a geo set, from its own C (`geohash.c`, `geohash_helper.c`, `geo.c` at
8.10.2), in `popoto/backends/postgres/geo.py`. The radius filters
(`<f>=(lat, lon)` or `<f>_latitude`/`<f>_longitude`, `<f>_member`,
`<f>_radius`, `<f>_radius_unit`, `<f>_with_distances`) behave as on Redis,
inside `Q` objects too, through `filter()`, `count()` and their `async_`
twins.

**What is stored.** Redis keeps a member's position as a 52-bit geohash, the
zset score, and measures from the cell's center, never from the coordinates
you saved. So beside the coordinates as given (`jsonb`, which is what a read
returns on both backends), a save writes:

- `<f>__geohash`: the score `GEOADD` stores. It is `geohashEncodeWGS84` at
  step 26, converted to a `double` as the zset holds it. That conversion
  matters at the edges. A point on the latitude limit (±85.05112878) sets bit
  52, and one on longitude 180 sets bit 53, where the `double` rounds to a
  neighbouring cell. Redis then stores that neighbour, and so does Postgres.
- `<f>__geolon` / `<f>__geolat`: the position decoded from the score, i.e.
  what `GEOPOS` returns.

A value with a falsy latitude or longitude (`None`, `0`) is not indexed
(`NULL` score), as `GeoField.on_save` `ZREM`s it. A delete removes the row,
and with it the point. A partial save writes these columns only when it
writes the field.

**How a search runs.** The nine geohash boxes Redis scans are computed in
Python: its step estimate, its "decrease the step" check, and its pruning of
useless neighbours. One statement fetches the rows whose score falls in those
`[min, max)` ranges. That is the bounding-box prefilter, served by the
partial B-tree; on a `Meta.ttl` model it fetches live rows only, and it is
narrowed by the query's other top-level filters. Each candidate's distance
is then `geohashGetDistance`, Redis's haversine on its 6372797.560856 m
earth radius with its equal-longitude shortcut. A point is in when
`distance <= radius * conversion`. The matched keys scope the query's own
statement (`_pk = ANY(…)`), so `limit`, `order_by`, `values=` and the
other filters apply in SQL. With `with_distances`, each row carries
`_geo_distance` as `WITHDIST` replies it (the meters divided by the unit's
factor, printed with four decimals, ties to even), plus
`_geo_distance_unit`. Rows sort by that distance after any `order_by` term
and before the key tie-break, which is how the Redis path re-sorts its
hydrated objects. This is `QueryPlan.compute` (`ComputedCol("_geo_distance",
…)`).

**Arithmetic: plain IEEE doubles, never fused.** A Postgres deployment has
no Redis to mirror, so the port never depends on how a compiler built
Redis: each `a*b + c` in Redis's C is evaluated as two roundings, as CPython
evaluates it. The geohash encode and decode use no libm, so the stored
score and decoded position are the same on every platform and equal to an
x86-64 Redis's (`GEOPOS`) to the bit. A Redis built by clang on arm64 (Homebrew 8.10.2, for
one) contracts two expressions into fused multiply-adds: the decode's
`min + (i / 2**step) * scale` and the haversine's
`u*u + cos(lat1)*cos(lat2)*v*v`. Against that build, a decoded position
(`GEOPOS`) or a distance is one fused rounding away from this one in places.
For a position that is at most an ulp of the decode's product (about
1.4e-14 degrees), which is thousands of ulps of a latitude near zero, where
the decode's subtraction cancels. For a distance it is usually an ulp or
two, but near the antipode `asin`'s slope amplifies one rounding of the
haversine's `a`: the seeded probe saw up to about 0.19 m on a
2×10^7 m distance. The consequence that reaches a query is at the radius:
a member whose distance is that close to the radius can be in on one build
and out on the other, and a `WITHDIST` value can differ in its fourth
decimal when the gap straddles a rounding boundary.

The distance also calls `sin`, `cos` and `asin`, from the host Python's
`libm`, as Redis calls its own. Mainstream libms (glibc, musl, Apple's) are
accurate to under an ulp but do not agree on the last bit, so a distance
can differ by those ulps (propagated through the formula; again up to tens
of centimetres near the antipode) between the port and a Redis linked
against another libm, and between ports on different hosts. CI shows it:
`redis:7-alpine` links musl and the runner's Python glibc, and with
positions identical, a handful of radius tests in the seeded probe come out
on opposite sides of a radius that close. `sqrt` is IEEE, correctly
rounded everywhere.
The exact test runs in Python rather than SQL so the save's decode and the
search's test share one implementation.

**Quirks reproduced, not repaired.** A point whose score is 2^52 or more
(the latitude limit, longitude 180) lies past every search box, so no radius
search finds it; it is stored all the same. A search by member of a field
with nothing indexed replies empty without looking at the member or the
radius, as `GEORADIUSBYMEMBER` on a missing key does. An `inf` radius is
accepted and covers the whole indexed set. redis-py refuses a `bool` or a
`Decimal` center or radius before anything is sent, so its message comes
before any server-side check.

**PostGIS** (a `geography` column and a GiST index) could replace the box
scan later as an optimisation. It would not change the arithmetic above, and
it is not a dependency.

The seeded probe compares both legs against the *running* Redis, including
the stored score and decoded position against `ZSCORE`/`GEOPOS`, and each
leg's exact distance read off its own radius test (in at `d`, out one ulp
below). Every comparison is exact, except four classes that count apart.
`fma_position_ulp` (a position or distance one fused rounding away) and
`fma_boundary` (a search or count differing only by members the contraction
moves across the radius) exist only against a contracting build, and are
not a tolerance: a mismatch joins one only when a model of the same C with
the two contractions reproduces the running Redis to the bit. `libm_ulp`
and `libm_boundary` exist only against a Redis on another libm: a distance
(never a position) within the envelope the formula gives when each of its
five libm results is off by at most one ulp, or a search differing only by
members whose envelope straddles the radius. Against a contracting Redis
sharing the probe's libm (arm64 macOS) the `libm_*` classes are 0; against
x86-64 Redis the `fma_*` classes are 0.

```bash
REDIS_URL=redis://localhost:6379/7 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_geo_parity.py --seeds 1 2 3 --shapes 500
```

**Divergences** (each counted apart by the probe):

| | Redis | Postgres |
|---|---|---|
| Saving a point `GEOADD` refuses (`lat` past ±85.05112878, `lon` past ±180) | the hash is written, then `GEOADD` fails: the record exists, unindexed, and the save raises `ResponseError` | refused before writing: `ValueError` with the same text (`invalid longitude,latitude pair …`) |
| A search the server refuses (bad center, bad or negative radius, a member that is not indexed) | `ResponseError` | `QueryException`, same text |
| A record that has expired (`Meta.ttl`) | its geo-set member stays (no read purges it): `filter()` drops it at hydration, but `count()` keeps counting it, and a search by its member still runs around it | invisible to every search, `count()` and member lookup at once |
| A geo leaf scoping a ranking or a search (`rank_decayed(where=…)`, `recall`) | the leaf's key set | `BackendCapabilityError`: a geo filter scopes `filter()` and `count()` only, for now |
| A Redis built with floating-point contraction (clang, arm64) | the decode and the haversine round `a*b + c` once: positions and distances one fused rounding away (up to ~0.19 m near the antipode), so a member that close to the radius can flip | plain IEEE arithmetic: positions equal to x86-64 Redis's to the bit, on every platform |
| A Redis linked against another libm (musl in `redis:7-alpine`, against glibc or Apple's) | `sin`/`cos`/`asin` from its libm | from the host Python's: a distance can differ in its last bits, so a member that close to the radius can flip |

Two leaves asking for distances in one query (two geo fields, or a geo leaf
in each branch of a `Q`) merge their distances in predicate order, and the
last leaf's unit wins. On Redis the merge order follows its own evaluation
order, which this does not promise to match.

## Topology and the outage contract

The deployment model is one central Postgres for every agent and machine.
Each process opens one `psycopg_pool.ConnectionPool` per DSN, and opens it
again after a fork. Its `max_size` is `Defaults.PG_POOL_MAX_SIZE` (4).
Popoto holds no session state. Each operation is a single message,
`SET LOCAL statement_timeout = …; <statement>`, which runs as one implicit
transaction. That makes it atomic and costs one round trip. It is also safe
behind **PgBouncer in transaction mode**, which you should put in front once
you have more than a few dozen clients. Advisory locks are only ever
`pg_advisory_xact_lock`.

**Stale connections.** A server restart, a failover or an idle reaper kills
the pool's backends while the server stays up. The pool checks each connection
when it hands it out (`ConnectionPool.check_connection`, one empty-query round
trip), so a dead one is replaced rather than used. If a connection dies while a
statement is running, the statement is retried once on a fresh connection only
when it cannot have committed: always for a read, and for a write only when the
server reported an error for it (such as `AdminShutdown`), because the server
rolls that statement's transaction back. A write whose reply was lost may have
committed, so it is not retried. It raises `BackendUnavailableError` and counts
as a dropped write. The count means "no confirmed reply", not "did not land":
a write that committed but lost its reply (for example the connection dying
while the server waits on a synchronous standby) is counted as dropped even
though the row is there. Check the row before replaying such a write. Statements inside a `transaction()` are never retried.

When Postgres is unreachable, or a connect or statement timeout fires
(`Defaults.PG_CONNECT_TIMEOUT_SECONDS`, `Defaults.PG_STATEMENT_TIMEOUT_MS`),
the call raises **`BackendUnavailableError`**. The backend's health record
tracks the outage:

```python
backend = popoto.backends.get_backend(Note)
backend.health.as_dict()
# {'ok': False, 'last_ok_at': ..., 'consecutive_failures': 3,
#  'dropped_writes': 2, 'last_error': 'PoolTimeout: ...'}
```

The outage is logged at ERROR once per `Defaults.PG_OUTAGE_LOG_WINDOW_SECONDS`
(60 s), however many calls fail within that window. After a successful call
the record resets, and a recovery is logged at WARNING.

**A busy pool is not an outage.** When every connection the pool may hold
(`Defaults.PG_POOL_MAX_SIZE`) is checked out by other callers -- more
concurrent `async with transaction()` blocks or `async_*` calls on one event
loop than that, or more threads -- a caller waits up to
`Defaults.PG_CONNECT_TIMEOUT_SECONDS` for one to come back, and then raises
**`popoto.backends.BackendBusyError`**, a subclass of
`BackendRetryableError`. Every connection is in use, so this is contention:
the health record is not touched and no dropped write is counted; the call
sent nothing, so running it again is safe:

```python
from popoto.backends import BackendBusyError

try:
    await note.async_save()
except BackendBusyError:
    ...  # back off and retry, or bound your concurrency below PG_POOL_MAX_SIZE
```

A connection that cannot be *opened* is still `BackendUnavailableError` and
an outage. The sync `psycopg_pool` reports both cases as one `PoolTimeout`,
so on the sync path popoto calls a timeout busy only when every connection
of the pool is checked out to a caller at that moment. The async pool opens
connections inline, so it can tell more: a wait that times out is busy when
every slot is checked out, and otherwise it is decided by what the server
did. Some slot is then held by a caller still validating an idle
connection (the checkout check's round trip) or *connecting*, and the
waiter waits -- at most one connect timeout more -- for that round trip's
outcome: one that reaches the server makes the wait contention
(`BackendBusyError`), a connect that *fails* (refused, timed out at the
socket) makes it an outage, and none answering within the connect timeout
is an outage too. So a slow but reachable server under contention -- a
loaded CI runner, where a round trip can straddle a 1 s checkout timeout --
is busy and leaves health untouched, while during a partition (an
unresponsive server, a black-holed port) callers sit inside the connect
holding slots, the waits queued behind them end as outages, health flips and
each write is counted dropped. Classifying by slot state alone counted the
first case as an outage, which is what turned `main` red after #784. The
"every slot checked out" rule still says nothing about the server's state,
only the pool's -- with every connection checked out and the server then
going down, a new caller is still told the pool is busy, and the holders
report the outage when their statements fail. Pinned by
`tests/postgres/test_postgres_async.py::test_a_busy_async_pool_is_not_an_outage`,
`test_a_partitioned_async_pool_is_an_outage_not_busy`,
`test_slow_connects_to_a_reachable_server_are_contention_not_an_outage`,
`test_a_wait_behind_a_slow_checkout_check_is_contention_not_an_outage` and
`test_a_busy_sync_pool_is_not_an_outage`.

## Transactions

```python
backend = popoto.backends.get_backend(Note)
with backend.transaction() as uow:      # one READ COMMITTED transaction
    Note(owner="a", slug="1").save(pipeline=uow)
    Note(owner="a", slug="2").save(pipeline=uow)
# both committed, or (on an exception) neither
```

`bulk_create`, `bulk_update` and `bulk_delete` on a Postgres model each run
inside one `transaction()`. A unique conflict anywhere in the batch rolls back
the whole batch. Deadlock and serialization failures are retried
automatically, up to `Defaults.PG_TRANSACTION_RETRIES` times, for single
statements, which then raise `popoto.backends.BackendRetryableError`. Inside a
`transaction()` -- including at its commit -- the whole unit rolls back and
`BackendRetryableError` is raised at once, chained from the driver error;
popoto does not retry a block it does not own:

```python
from popoto.backends import BackendRetryableError

for attempt in range(3):
    try:
        with backend.transaction() as uow:
            ...
        break
    except BackendRetryableError:
        continue  # rolled back by a concurrent transaction; safe to rerun
```

**After-commit hook.** `uow.after_commit(fn)` registers a no-argument
callable to run once this transaction has committed. Callbacks run in
registration order after `COMMIT` returns. A rolled-back transaction drops
them, and so does a failure at `COMMIT`. A callback that raises is logged at
WARNING; the remaining callbacks still run, and the exception is not raised,
because the write has already committed. This hook is how a Postgres-bound
save runs the `WriteFilterMixin` priority tag (a no-op off Redis). Without a
caller transaction, the save has already committed by the time it runs, so
it runs immediately. Pinned by
`test_after_commit_callbacks_run_only_after_commit` and
`tests/postgres/test_postgres_recipes.py::test_post_save_redis_side_effects_wait_for_commit`.

**Before-commit hook (M5).** `uow.before_commit(fn)` runs `fn` *inside* the
transaction, just before its `COMMIT`, in registration order; an exception
from it rolls the whole unit back and propagates. A save's or delete's
`EventStreamMixin` entry is queued beside them with
`uow.defer_stream_append(stream, fn)` inside a caller's `transaction()` or a
`popoto.batch()`; those run after the `before_commit` callbacks, sorted by
stream key, so the stream row locks are the last locks the transaction takes
(after every record-key and row lock, the one lock order of §6), always in
the same order, and the entry commits or rolls back with the record. Pinned by
`tests/postgres/test_postgres_journal.py::test_the_mutation_stream_is_written_only_after_commit`.

Before #759 M2a's patch these reached the caller as raw psycopg
`DeadlockDetected` / `SerializationFailure`. A statement whose completion is
unknown (SQLSTATE 40003) is not retryable -- it may have committed -- and is
reported as `BackendUnavailableError`.

### `popoto.batch()` (M5)

The same code opens a batch on either backend:

```python
pipe = popoto.batch()
Note(owner="a", slug="1").save(pipeline=pipe)   # returns pipe, as on Redis
Note(owner="a", slug="2").save(pipeline=pipe)
pipe.execute()                                  # both, or neither
```

`batch()` still returns a real `redis.client.Pipeline` (a subclass, so every
`isinstance` gate and every Redis byte is what it was). The first write from
a Postgres-bound model that is handed the batch opens one `transaction()` on
that model's backend, and that write and every later one runs inside it.
`execute()` commits it and returns `[]`. `reset()`, leaving a `with` block,
or dropping the batch without `execute()` rolls it back, as an unexecuted
Redis pipeline sends nothing. Saves, deletes, increments, `touch`, the
confidence, read-tracking and `ObservationProtocol` writes, the
`CoOccurrenceField` edge writes (`link`, `strengthen`, `unlink`,
`weaken_all`), the `AppendOnlyMixin` guard and `ProvenanceJournal`'s
`append`/`supersede`/`retract` all join it. Until `execute()`, no other
connection sees the writes, as with queued commands. A graph write handed
the batch runs in the batch's transaction, so it reuses the lock the batch
already holds on a record it saved; a second save of an append-only key in
one batch sees the first and is refused, as in a `transaction()`.

**Stream entries and side effects follow the batch.** A save's
`EventStreamMixin` entry, a custom `_xadd_event` and a `Publisher`'s message
join the batch's transaction (M5, [Event streams and pub/sub](#event-streams-and-pubsub-m5)):
the entries are appended just before `execute()` commits, one per save, in
save order, and the message is delivered at that `COMMIT`; a batch that
`reset()`, a `with` block or a failed `execute()` rolled back appends and
delivers nothing. `WriteFilterMixin`'s priority tag (a no-op off Redis) is
registered on the after-commit hook ([Transactions](#transactions)). On
Redis the same effects queue on the pipeline, so both backends apply them
exactly when the writes land, and a Postgres batch sends Redis nothing.
Pinned on both legs by `tests/test_batch.py` and `tests/test_pubsub.py`.

**Atomic, where Redis is not.** If a statement fails *inside the batch's
transaction* (two saves in the batch that claim one unique value, say), the
call that issued it raises as usual, the transaction is aborted, and
`execute()` then raises `popoto.backends.BackendError` and writes nothing. A
Redis `MULTI`/`EXEC` applies the other queued commands. A save that is
refused *before* it sends anything -- `pre_save` finding a unique value
already held by a committed record, a field validation error -- raises at
the call and leaves the batch healthy, so if you catch it, `execute()`
commits the rest, as on Redis. `batch(transaction=False)` is still one
transaction on Postgres. A deadlock inside the batch raises
`BackendRetryableError`, as inside any `transaction()`.

**A record lock this thread or task already holds.** Until it commits, a
batch holds the record-key lock of each record it wrote. A write from the
same thread that would wait for one of those locks outside the batch -- a
second, nested `batch()` or `transaction()` that writes the same record, or
a plain `save()` of it -- could never be granted, because the only thread
that can release it is the one waiting. Popoto refuses that write with
`BackendCapabilityError` before sending it, instead of hanging until
`PG_STATEMENT_TIMEOUT_MS`; the batch that holds the record is unharmed.
Write through that batch, or `execute()` it first. Another thread's write
simply waits for the commit. Nested batches that write different records
work as on Redis.

Under asyncio the unit is the **task**, not the thread: every task's
Postgres I/O runs on the event-loop thread ([Async](#async-m5)). Three
rules decide whether a task's write that would wait on another unit's
record lock waits or is refused:

- **Its own transaction or batch:** refused at once, as for a thread.
- **An ancestor's:** refused at once. An ancestor is a task that had a
  `transaction()` or batch open on this backend when it created this task
  (`asyncio.gather`, `create_task`, `TaskGroup`), directly or through
  intermediate tasks. The parent typically awaits the child inside its
  block, so it cannot commit until the child finishes and the wait would
  hang until `PG_STATEMENT_TIMEOUT_MS`. The error says so: pass
  `pipeline=uow` (the parent's unit or batch) to the child to join the
  parent's transaction, or write the record after it ends. This applies even
  to a child the parent never awaits (fire-and-forget): popoto cannot tell
  the two shapes apart, so the child is refused rather than allowed to wait.
- **Anyone else's** -- a sibling task, a task created before the
  transaction opened, an unrelated task, another thread: an ordinary wait.
  That unit can commit while this one waits, so it is never refused. Two
  children of one parent, each in its own `transaction()`, that write
  overlapping records serialize on the locks; neither is refused.

The ancestry is carried in a context variable that the parent's task sets
when an `async_*` call leaves it with a unit open, and that a task inherits
when it is created. So a child created *before* the parent opened its
transaction does not count as a descendant. Neither does the reverse case:
a child that opened a batch and returned, with the parent then writing
the same record outside that batch. Both still wait until
`PG_STATEMENT_TIMEOUT_MS`.

The check covers **record-key locks only** (the locks `save`, `delete`,
`increment` and the other record writes take first). A nested write that
would wait on any other lock an outer unit holds -- the `(model, field)`
lock a `ValidityField` write takes, a `UNIQUE` value the outer unit wrote
and has not committed -- is not detected, and waits until
`PG_STATEMENT_TIMEOUT_MS`.

**One batch, one backend.** A batch holding a Postgres transaction refuses a
Redis command, and a batch with Redis commands queued refuses a Postgres
write, with `BackendCapabilityError`, before anything is sent: a Redis
`MULTI`/`EXEC` and a Postgres transaction cannot commit atomically together,
and a batch that only looked atomic would be worse than a refusal. A batch is
also bound to the one Postgres backend it opened on. Use one batch per
backend. `SupersessionProtocol`'s mutators refuse a batch on a Postgres
model, as they refuse any Redis pipeline; pass `backend.transaction()`'s
unit of work there. (`ProvenanceJournal`, which calls them, accepts a batch:
it hands them the batch's unit of work.)

## Record expiry (M5)

```python
class Session(popoto.Model):
    token = popoto.KeyField()
    user = popoto.StringField()

    class Meta:
        backend = "postgres"
        ttl = 3600            # every save sets the record to expire in an hour

s = Session(token="t", user="u")
s._ttl = 60                   # or an instance override, as on Redis
s.save()
```

A `Meta.ttl` model's table has one more engine column, `_expires_at double
precision` (epoch seconds; `NULL` never expires), with a partial B-tree on
the rows that set it. A model without `Meta.ttl` gets neither, and none of
the SQL below: its tables and plans are exactly what they were.

**Writes.** A save sets `_expires_at` from the instance, as the Redis save
issues `EXPIRE`/`EXPIREAT`: `now + _ttl` (the server's clock), else
`int(_expire_at.timestamp())`, else it leaves the stored value alone, as
`HSET` leaves a key's TTL alone. A partial (`update_fields`) save sets it
too. A zero or negative `_ttl`, or an `_expire_at` in the past, expires the
record at once. A save over an expired key that the reaper has not yet
deleted drops the expired row first, so the save writes a fresh record (new
side rows, the confidence seed) and returns the new-record count, as `HSET`
on a key Redis has expired creates a new hash.

**Reads.** Every read of the model sees a row only while `_expires_at` is
`NULL` or later than *now*: `get`, `get_many`, `filter`, `all`, `count`,
`keys`, `exists`, `values=`, `load_fields`, `top_by_decay`,
`composite_score`, `top_by_relevance`, `BM25Field.search`,
`keyword_search`, `semantic_search`, `load_embeddings`, `recall`,
`might_exist`, the BM25 corpus statistics, confidence and read-tracking
state, the validity reads and `chain`, `idle_seconds`, the `AppendOnlyMixin`
guard, and `ContextAssembler` through all of them. An expired row is gone to every reader the instant it expires, deleted
or not. *Now* is the server's `statement_timestamp()`, one clock for every
agent on the central database. A `_ttl` is added to that clock, so the app
machine's clock never moves it; only an `_expire_at`, an absolute instant
from the app, is exposed to skew between the app and the database server,
exactly as `EXPIREAT` is judged by the Redis server's clock. (Decay and the
validity `as_of` stay on the app's clock, as on Redis.) `tests/postgres/test_postgres_ttl.py`
exercises each of those reads against one expired record. A write addressed
to one record (`atomic_increment`, a capped `push`, `touch`,
`update_confidence`) treats an expired row as missing.

**The reaper.** No cron, no CLI, no manual job (#755 Q3). After a record
write on a `Meta.ttl` model commits (a save, delete, increment or capped
push, or a `transaction()`/`batch()` that wrote one), the backend deletes up
to `Defaults.PG_REAPER_BATCH` (20) expired rows of that table in a
transaction of its own; their postings, vectors, membership and validity
rows cascade. It runs at most once per `Defaults.PG_REAPER_INTERVAL_SECONDS`
(1 s) per table and process, except that a run which found a full batch
leaves the next write due at once, so a backlog drains a batch per write. It
takes the backend's lock order without ever waiting: each candidate's
record-key lock is a try-lock (a record a writer holds is skipped), rows are
locked `SKIP LOCKED` in `_pk` order and re-checked (a save that refreshed the
TTL keeps its row), and `lock_timeout` is
`Defaults.PG_REAPER_LOCK_TIMEOUT_MS` (50 ms), below `deadlock_timeout`, so
the reaper gives up before a caller could be picked as a deadlock victim. A
busy pool, a lock it would wait on, or an outage skips the run; it never
fails the write that triggered it and does not touch the health record.

**Reads never reap, by decision.** A read stays a single read-only
statement: it takes no row locks, works on a read-only role or a replica,
and its plan does not change with the backlog's size. Correctness does not
need the reaper, since every read filters expired rows out. The cost is that
a workload that stops writing to a `Meta.ttl` table stops draining it: the
expired rows stay until the next write, and the reads that filter them with
an anti-join (the BM25 corpus statistics, the membership reads) pay for each
one. The backlog is therefore bounded by write traffic: at most the rows
that expired since the table's last write, and each later write drains a
batch of 20 at once. A table that is read for long stretches without writes
and holds many short-lived rows can be drained by any write to it, such as
saving and deleting one record.

Cost, measured as the p50 of 300 saves (Apple M1 Max, PostgreSQL 18.6 on
localhost, load 5-7, three runs each): a model without `Meta.ttl`
0.33-0.35 ms; a `Meta.ttl` model with the reaper idle 0.37-0.39 ms; the
same with the reaper forced on every write and nothing due 0.66-0.79 ms
(what the 1 s interval saves); a save that reaps a full batch of 20 rows
1.07-1.09 ms (100 rows: 2.2-3.5 ms, which is why the batch is 20).

**Reading a remaining TTL.** `backend.ttl_remaining(spec, ids)` answers what
`redis.ttl(key)` answers: `-2` for no live record, `-1` for no expiry, else
the seconds left, rounded as Redis rounds its milliseconds.

**The seeded probe.** `scripts/probe_ttl_parity.py` runs random sequences of
saves (every kind of TTL above, full and partial), deletes and batches on
both backends, compares every outcome and every read (`get`, `exists`,
`filter`, a range filter, `count`, the remaining TTL), sleeps past the short
expiries and compares again, then writes to the expired records. On the
Postgres side alone it also freezes the clock at, just before and just after
each expiry instant and checks visibility against an exact model of the
sequence. Its documented classes are the TTL rows in the divergence table
below; `tests/postgres/test_postgres_ttl.py` runs a 40-shape slice.

```bash
REDIS_URL=redis://localhost:6379/10 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_ttl_parity.py --seeds 1 2 3 --shapes 120
```

## Async (M5)

On a Postgres-bound model every `async_*` method -- `async_save`,
`async_create`, `async_delete`, `async_load`, `async_get_or_create`,
`async_update_or_create`, the bulk twins, `async_delete_all`, and
`Query.async_get`/`async_get_many`/`async_filter`/`async_all`/`async_count`/
`async_keys` -- runs on the model's **async backend**, with its I/O on
`psycopg.AsyncConnection` on the running event loop. Before M5 they ran the
sync call in a worker thread. A Redis-bound model's `async_*` methods are
unchanged, command for command (`scripts/trace_async_redis_wire.py`).

```python
from popoto.backends.postgres.aio import get_async_backend

note = await Note.async_create(owner="a", slug="1")
rows = await Note.query.async_filter(owner="a")

backend = get_async_backend(Note)          # Postgres-bound models only
async with backend.transaction() as uow:   # one READ COMMITTED transaction
    await Note(owner="a", slug="2").async_save(pipeline=uow)
    await Note(owner="a", slug="3").async_save(pipeline=uow)
# both committed, or (on an exception) neither
```

**One implementation.** The async backend does not reimplement the sync one.
It runs the sync backend's own code in a greenlet on the loop thread (the
technique SQLAlchemy's asyncio extension uses) and swaps the one thing that
does I/O: inside that greenlet, every statement the sync code issues is
awaited on an `AsyncConnection` from the loop's pool. So the SQL, the lock
order, the deadlock retry and `BackendRetryableError`, the #769 no-blind-retry
rule, the `BackendUnavailableError` outage record (shared: `get_backend(M).health`
counts async failures and dropped writes too), the savepoint a guarded save
takes inside a caller's transaction, and the first-use version, encoding and
DDL checks are the sync backend's, by construction. `greenlet` comes with the
`postgres` extra; without it, the `async_*` methods fall back to the worker
thread and log one warning. A unit of work from the async `transaction()` is
for the `async_*` methods: handing it to a sync `save(pipeline=uow)` raises
`BridgeMisuseError`.

**`popoto.batch()` from async code.** An `async_*` write handed a batch
joins it as a sync write does ([`popoto.batch()`](#popotobatch-m5)), but
the batch's transaction is then opened on the loop's async pool, so commit
it from the coroutine:

```python
pipe = popoto.batch()
await Note(owner="a", slug="1").async_save(pipeline=pipe)
await Note(owner="a", slug="2").async_save(pipeline=pipe)
await pipe.async_execute()     # COMMIT on the loop; then the XADDs, in order
```

`pipe.execute()` on such a batch raises `BridgeMisuseError` naming
`async_execute()`; `await pipe.async_reset()` rolls it back. A plain
`reset()` or leaving a `with` block schedules the rollback on the running
loop as a task. If that rollback fails (its connection broke), the failure
is logged once at WARNING; the server rolls back a transaction whose
connection is gone. The Redis side effects wait for the commit exactly as in a
sync batch. `async_execute()` on any other batch (Redis commands, or a
transaction a sync call opened) runs `execute()` in a worker thread.
Pinned by `test_postgres_async.py::test_an_async_save_joins_a_batch_on_the_loop`.

**A batch is driven by the side that opened it.** If an `async_*` write
opened the batch, a sync write to it raises `BridgeMisuseError`. If a sync
write opened it, an `async_*` write raises `BridgeMisuseError` too: the
batch's connection is a blocking one, and driving it from the loop would
stall every other task for each statement. Both are refused before anything
is sent, and the batch stays usable from the side that opened it. A batch
that outlives its loop (`asyncio.run()` returned before `async_execute()`)
is rolled back as the loop shuts down, and its connection is closed at the
socket by the next pool lookup or `close_pools()`. A later `reset()` of it
is a no-op.

**Concurrent transactions are per task.** Two tasks' `async with
transaction()` blocks that write the same records wait on each other's
record locks like two threads do; the refusal of a write that waits on a
lock its *own* open transaction holds ([`popoto.batch()`](#popotobatch-m5))
is per task. More concurrent transactions than `PG_POOL_MAX_SIZE` queue for
a connection, and one that waits longer than `PG_CONNECT_TIMEOUT_SECONDS`
raises `BackendBusyError`, not an outage ([Topology](#topology-and-the-outage-contract)).

**What still leaves the loop.** An embedding provider is a sync API: its call,
and the backfill's wait on it, run in a worker thread so they never block the
loop. Redis I/O that a Postgres model's sync path makes (an
`EventStreamMixin`'s `XADD`) is a blocking call on the loop thread.
`async_check_indexes`/`async_clean_indexes`/`async_rebuild_indexes` stay on a
worker thread on every backend: their bodies scan Redis index keys.

**Model hooks run on the event-loop thread.** The bridge runs the whole sync
method, so a model's overrides of `save()`, `pre_save()` and `delete()`, and
its fields' `pre_save_validate`/`on_save`/`on_delete` hooks, run inside it, on
the loop thread, with the loop running. Before M5 they ran in a worker thread.
Two consequences:

- A hook that calls `asyncio.run()` (or `loop.run_until_complete()`) now
  raises `RuntimeError: asyncio.run() cannot be called from a running event
  loop`. It worked under the thread shim. `asyncio.get_running_loop()` now
  succeeds inside a hook, so a hook that branches on it sees a loop.
- A hook that blocks (`time.sleep`, `requests`, a sync client of another
  service) blocks the whole loop for that long, not one worker thread.

Keep hooks to in-memory work on the instance. Do blocking or async work
outside the save: `await` it before or after `async_save()`, or hand it to
`asyncio.to_thread()` / a task from the calling coroutine. A hook that must
stay blocking can be reached through the sync `save()` in
`asyncio.to_thread(instance.save)`.

**First use of a model from concurrent tasks.** The backend's first-use table
check holds a `threading.RLock`, which separates threads, not tasks: every
bridge greenlet runs on the loop thread, so for them the lock is re-entrant
and concurrent first uses all pass it. What serialises them is the server:
`ensure_table` takes a transaction-scoped advisory lock
(`pg_advisory_xact_lock` on `popoto:ddl:<schema>`), so one task creates or
checks the table and the others wait, then find it made.

**Event loops.** Pools are per (DSN, process, event loop), lazy, each at most
`Defaults.PG_POOL_MAX_SIZE` connections, validated on checkout like the sync
pool's. A model used from two loops -- `asyncio.run()` twice, pytest-asyncio's
function-scoped loops -- gets a pool per loop. A loop's pool closes with the
loop: `loop.shutdown_asyncgens()`, which `asyncio.run`, `asyncio.Runner` and
pytest-asyncio call, finalizes it. A loop closed without that call is swept,
at the socket, by the next pool lookup from any loop, so `pg_stat_activity`
does not grow with the number of loops a process has used
(`tests/postgres/test_postgres_async.py`). A task cancelled at any point --
`task.cancel()`, an `asyncio.wait_for` timeout -- including while its
connection's checkout check is in flight, closes that connection rather than
leaving it open and unowned, so the server's session count for a pool stays
at or below `PG_POOL_MAX_SIZE` however often callers cancel. The pool is popoto's own, not
`psycopg_pool.AsyncConnectionPool`: that pool's maintenance workers catch
`CancelledError`, so one still open when its loop shuts down hangs
`asyncio.run()`'s task cancellation whenever a worker is mid-task.

## Ranking and memory state (M2a)

Each operation is one statement. `top_by_decay` is `rank_decayed` (below)
followed by one `load` of the ranked rows, in rank order; a model with
`AccessTrackerMixin` then stages the reads in one more `UPDATE`.

**`rank_decayed`** is `DECAY_SCORE_LUA` as a `SELECT`, operation for
operation in `double precision`, so the platform's `pow` behind `power()`
gives the Lua's bits:

```sql
SELECT t."_pk",
       ((CASE WHEN b < 0 THEN -1 ELSE 1 END) * abs(b)
        * power(greatest(($now - t.f) / 86400, 0.01), -$rate))
       -- with confidence modulation:
       * power(greatest(greatest(($now - t.f) / 86400, 0.01), 1),
               -($rate * power(2, $s2 * ($c0 - greatest(0, least(1, coalesce(t.c__conf, $c0)))))
                 - $rate)) AS "_score"
  FROM popoto.<model> AS t
 WHERE t.f IS NOT NULL AND <partition filters>
 ORDER BY ("_score" = 'NaN'), "_score" DESC, "_pk" COLLATE "C"
 LIMIT $n
```

`b` is the base-score column (`1.0` when there is none, or it is `NULL`, a
string or a boolean, as the script's `HGET` + `cmsgpack` rule gives; a
`numeric` converts through the same `strtod` the script's `tonumber` uses),
`$s2` is twice `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH`, and `$c0` the
confidence field's `initial_confidence`. Only the partition filters scope the
scan, as on Redis, where they pick the sorted set.

Postgres raises `value out of range` where C's `pow` and `*` overflow to
`inf` or underflow to `0` (`power(0.01, -155)`, `1e-300 * 1e-300`; the #631
POC's boundary rows). Rows whose inputs sit in a box where no step can leave
the `double` range -- every realistic one -- take the expression above; any
other row, and every row when the rate or strength is extreme, takes a
correlated subquery that computes the same steps with each `power`, `*` and
`-` clamped, so the result is `inf` or `0`, with its sign, where the Lua's is.
Inside a band of 0.003 in log space at each end of the `double` range (a
result within 0.25% of `DBL_MAX`, or between `2.47e-324` and `2.48e-324`) the
clamp saturates where the exact value is still finite; that is the only place
the two legs can differ. Scores come back rounded through `%.14g`, which is
what the Lua's `tostring` replies with.

**`update_confidence`** is `CAPPED_BAYESIAN_UPDATE_LUA` as one `UPDATE`:

```sql
UPDATE popoto.<model> SET
  c__conf   = greatest(0, least(1, coalesce(c__conf, $c0)
              + ($signal - coalesce(c__conf, $c0)) / (least(coalesce(c__n, 0) + 1, $cap) + 1))),
  c__n      = coalesce(c__n, 0) + 1,
  c__corr   = coalesce(c__corr, 0) + <1 if $signal >= 0.5>,
  c__contra = coalesce(c__contra, 0) + <1 if $signal < 0.5>
 WHERE "_pk" = $pk
RETURNING c__conf, c__n, c__corr, c__contra
```

The arithmetic is the script's, so the stored confidence is bit-identical to
the Redis one; the returned value is rounded through `%.14g` as the script's
reply is. No row means the record does not exist: the field layer raises the
same `TypeError` as on Redis (with a pipeline, the update is skipped, as the
queued script skips it).

**`composite_score`** is one `SELECT` over the model's rows: each index is an
arm scoring the records its Redis sorted set would hold (the decay arm the
partition's clocks, the confidence arm every record, the access arm every
record read at least once), a record is ranked when any arm holds it, and the
aggregate runs over the arms that do -- `SUM` in the indexes' order (a NaN
step, `inf + -inf`, becomes 0 as `ZUNIONSTORE` makes it), `MAX`/`MIN`
ignoring the rest. Ties come back in descending key order, as `ZREVRANGE`
gives; `min_score` applies before `temperature`, and `post_filter` after it,
as on Redis. Two differences stay below the 1e-9 tolerance: Redis's decay arm
holds the script's `%.14g`-rounded scores where Postgres uses the full
`double`, and Redis adds three or more arms in its own set order rather than
the indexes', which can move a sum by an ulp.

**The seeded probe.** `scripts/probe_memory_parity.py` builds the same random
corpus on both backends -- ages from fresh to centuries, the future, shared
timestamps and pathological clocks; base scores of every type the script reads,
from `5e-324` to `1e305`; signal sequences; partitions; staged and confirmed
reads -- and compares `rank_decayed`, `top_by_decay`, confidence,
read tracking and `composite_score` between the legs. Its classes and the
two documented NaN rows are in the PR that introduced M2a;
`tests/postgres/test_memory_probe.py` runs a 40-shape slice of it in CI.

```bash
REDIS_URL=redis://localhost:6379/10 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_memory_parity.py --seeds 1 2 3 --shapes 200
```

**`ObservationProtocol.on_context_used`** applies all five outcomes for a
batch in one transaction: the batch's rows are locked `FOR UPDATE` in `_pk`
order first (the plan's §6 lock order), then each instance's effects run in
that order -- `touch` for `acted`, staged reads confirmed (`acted`, `used`) or
discarded (the rest), the confidence signal (`acted`, `contradicted`), and its
proposal resolved. A deadlock or serialization failure retries the whole
batch up to `Defaults.PG_TRANSACTION_RETRIES` times and then raises
`BackendRetryableError`; inside a `transaction()` you pass as `pipeline` it
raises that error at once, for you to retry.

## Documented divergences

Redis behaviour does not change to match Postgres in v2 M1 (plan gate (a)).
Where a test pins one of these, it pins both behaviours explicitly or carries a
`redis_only(reason=...)` mark.

### Query results

Each row is pinned on both conformance legs: rows (i)–(xi) by a test in
`tests/test_backend_parity_edges.py`, rows (xii)–(xx) (M1.1) in
`tests/test_backend_parity_fields.py`. In rows (i)–(iv), (vi), (vii), (xii),
(xiv), (xv) and (xviii)–(xx) Redis's result is a bug in its query layer,
tracked in #771. The examples use `code`/`group` (`KeyField`), `rank`
(`SortedField`), `note`/`hits` (unindexed) and `at` (an unindexed
`DatetimeField`); the M1.1 rows use `IndexedField`s named by type (`s` str,
`i` int, `f` float, `dec` Decimal, `dte` date), a `DateField` `day`, a
`ListField` `lst`, a capped `ListField(max_length=3)` `cap`, a
`TupleField` `pair` and a `Relationship` `author`/`owner`.

| | Example | Redis (unchanged) | Postgres | Correct |
|---|---|---|---|---|
| (i) An unindexed field inside a composed `Q`, or a `Q` beside a kwarg | `filter(Q(hits=5) & Q(group="g1"))` | the plain leaf is dropped: every `g1` row | rows with `hits = 5` in `g1` | Postgres |
| (i′) The same, negated | `filter(~Q(note="x"))` | always empty: the leaf returns every key and its equality filter runs after the set algebra | `NOT`, with `NULL` counted as "not equal" | Postgres |
| (ii) `values=` with an unindexed-field filter outside the projection | `filter(group="g1", note="x", values=("code",))` | `[]`: the filter runs on dicts that lack `note` | the matching rows | Postgres |
| (iii) Two lookups bounding the same side of one `SortedField` | `filter(rank__gt=10, rank__gte=1)` | only the last lookup for that side applies: every row | both apply (`AND`): `[]` | Postgres |
| (iv) `values=` + `order_by` over a column holding `None` | `filter(rank__gte=0, values=("code", "hits"), order_by="hits")` | `TypeError: '<' not supported…` | rows, `NULL` sorted as the type's zero (as instances sort on both) | Postgres |
| (v) A bad `SortedField` range bound | `filter(rank__gte=None)`, `filter(rank__lte="abc")`, `filter(rank__lte="nan")` | `None`: `ResponseError: min or max is not a float`. A string the server's `strtod` refuses (`"nan"`, `"1_0"`, `"3 "`, `"abc"`, a non-ASCII digit) raises the same `ResponseError` (on a `Decimal` field Python's own `ValueError`, which converts first) | a refused string raises `QueryException("min or max is not a float")`: the same text, a different class. `None` returns `[]`. The strings Redis accepts parse identically (`"+3"`, `"3."`, `"1e1"`, `"inf"`; Redis also takes a leading space, hex and `""` as 0, which Valkey refuses, and Postgres follows Redis). The `ZRANGEBYSCORE` exclusive prefix (`"(3"`) is not supported: it raises. | Redis (failing loudly is the better answer) |
| (vi) `KeyField` `__contains` | `filter(code__contains="0")` | matches nothing (the lookup is accepted but not implemented) | `LIKE '%0%'` | Postgres |
| (vii) `KeyField` `__isnull=False` | `filter(group__isnull=False)` | only some non-null records match, depending on the key's position and value (a second key matches none; values such as `"10"`, or containing `_` or `%`, are missed) | `IS NOT NULL` | Postgres |
| (viii) Equality on a `DatetimeField` with a naive value against a stored aware one | `filter(at=datetime(2024, 1, 5, 12))` | compared in Python: naive never equals aware | compared as instants, naive taken as UTC (the rule sorted fields use, #519) | Postgres |
| (ix) Equality on `KeyField(type=Decimal)` | `filter(code=Decimal("1.50"))` against a stored `1.5`; `filter(code=2)` against `2.0` | key strings are compared, so both match nothing | `numeric` values are compared, so both match | Postgres |
| (x) Saving `Decimal("1.5")` and then `Decimal("1.50")` as `KeyField(type=Decimal)` | `create(code=Decimal("1.50"))` after `1.5` | two records (`"1.5"` and `"1.50"`) | unique violation: the values are equal | Postgres |
| (xi) `KeyField(type=float)` `-0.0` against a stored `0.0` | `filter(code=-0.0)` | `[]`: `"-0.0" != "0.0"` | `[0.0]` | Postgres |
| (xii) Chained relationship lookup | `filter(author__country="uk")` | `AttributeError`: `filter_query` gets `bytes` keys back and calls `.db_key` on them | resolves the related model's query and matches its keys | Postgres |
| (xiii) Equality on a collection field with another collection type | `filter(pair=[1, "x"])` on a `TupleField` holding `(1, "x")` | Python equality after hydration: a tuple never equals a list, so `[]` | compares the stored JSON documents, so the list matches (a `SetField` compares as a set) | Postgres is the more useful; neither is wrong by contract |
| (xiv) `IndexedField` numeric equality across value forms | `filter(i=1.0)` on `1`; `filter(f=1)` or `f="1"` on `1.0`; `filter(dec=Decimal("1.5"))` on `1.50`; `filter(i=True)` on `1`; `i__in=[1, 2.0]` | compares the filter value's key string (`"1.0"` ≠ `"1"`), so `[]`. Save coerces `i=1.0` to `1`, so a record saved with `i=1.0` cannot be found by `filter(i=1.0)` | compares numbers, as Python equality does (`1 == 1.0`, `Decimal("1.5") == Decimal("1.50")`) | Postgres |
| (xv) `__startswith`/`__endswith` on a row holding `None` | `filter(s__startswith="No")`, `s__endswith="ne"`, `s__startswith=""`, `f__startswith="N"` | the `None` set is named `…:None`, so the glob matches rows holding `None` | `LIKE` never matches `NULL` | Postgres |
| (xvi) Pattern lookup on a float in `1e15 <= abs(x) < 1e16`, or a `Decimal` Python writes in exponent form | `filter(f__startswith="1000")` on `1e15` | matches the key string `1000000000000000.0` | no match: Postgres renders `1e+15` (and `numeric` never uses exponent form) | Redis (the key string is the lookup's contract; outside this band Postgres renders it exactly) |
| (xvii) `order_by` on a collection field | `filter(…, order_by="lst")` | sorts as Python does (`[] < [1, 2] < [2]`); `TypeError` for mixed or `dict` elements | `BackendCapabilityError`: `jsonb` orders by length first (`[2] < [1, 2]`), so Postgres refuses rather than return a different order. Sort in Python. | Redis, where it does not raise |
| (xviii) `order_by` on a `date` column holding `None` (`IndexedField(type=date)`, `DateField`) | `filter(…, order_by="day")` | `TypeError: function missing required argument 'year'`: `None` sorts as the type's zero, `date()` | `NULL` first (last for `-day`) | Postgres |
| (xix) `order_by` on a `Relationship` | `filter(…, order_by="owner")` | `AttributeError: … '_meta'` | ordered by the stored key string, `NULL` first | Postgres |
| (xx) A capped `ListField` on a lazy read | `filter(…)`, `all()`; `filter(…, values=("name", "cap"))` | `cap` is `None` (only `get` loads the separate list key); `values=` omits `cap` and logs `quarantined field 'cap'` per row | the stored list, as `get` returns on both | Postgres |

Numeric key fields otherwise follow Redis's key-string rules on both backends
(`KeyField(type=int)` matches `1` and `"1"` but not `1.0`; an integer beyond
`bigint` matches nothing), and a `SortedKeyField` matches by score (`2.0`,
`"01"` and `" 1"` all find `2` and `1`). Redis's `SortedKeyField` `__in`
ignores the filter and returns every record (#771); Postgres filters, by
score, with mixed numeric types allowed (pinned:
`test_sorted_int_key_in_with_mixed_types`). `SortedKeyField` equality with
`True`, `False` or `None` raises `ResponseError: min or max is not a float` on
Redis; Postgres matches the score `1`/`0`, and `None` matches nothing (pinned:
`test_sorted_key_equality_with_bool_or_none_is_a_documented_divergence`).

A lone unindexed-field `Q` (`filter(Q(hits=5))`), one lower plus one upper
bound on a sorted field, and `values=` that projects the filtered field all
agree on both backends.

M1.1 cases that agree on both backends, and are pinned as such: a string
filter on an `IndexedField` of type `bool`, `date` or `datetime` that is the
stored value's key string (`b="True"`, `dte="2026-01-01"`,
`dtm="2026-01-01T12:00:00.000000Z"`; `"true"` matches neither); an aware
`time` filter on a `TimeField`, which matches nothing; `__startswith` /
`__endswith` on a non-text `IndexedField`, matched against the key string
(`1.0`, `True`, `2026-01-01`, `1.50`, `…T12:00:00.000000Z`) except row (xvi);
and `__in` with mixed numeric types (`i__in=[1, Decimal("2")]`), each element
cast to the column's type.

### Records and other behaviour

| Behaviour | Redis (unchanged) | Postgres |
|---|---|---|
| `save(migrate_key=True)` that changes a key field | rewrites the key and its index entries | raises `BackendCapabilityError` (v2). Create the new record and delete the old one. |
| Unit-of-work / bulk failure | `MULTI`/`EXEC` applies the other queued commands | the whole transaction rolls back |
| `\x00` in a text value | stored | `ValueError` naming the field |
| `atomic_increment` past `2**63` on an `IntField` | Lua goes to float | `NumericValueOutOfRange` |
| A Redis `pipeline=` handed to a Postgres model | queued | the write runs immediately, and the pipeline comes back untouched for the caller to execute. A `popoto.batch()` is the exception since M5: the write joins the batch's Postgres transaction (see [`popoto.batch()`](#popotobatch-m5)) |
| `atomic_increment` of an `IntField` by a non-integral delta (`hits=5`, `+1.5`) | stores the float sum `6.5` and returns it truncated, `6` | stores and returns the sum cast to `bigint`, rounded half away from zero: `7`. Neither is well defined; pass an `int` delta. |
| Order of results with no `order_by`, no `Meta.order_by` and no sorted-field filter | set order (arbitrary) | `_pk` in bytewise (`COLLATE "C"`) order |
| An invalid `order_by=` / `values=` on a query that matches nothing | returns `[]` before validating | raises the same `QueryException` either way |
| `save(update_fields=…)` on a record that does not exist yet | writes a partial hash that stays out of the class set, so queries do not see it | inserts the row (unlisted columns `NULL`), so queries see it |
| `UniqueKeyField` / `UniqueField` / unique `Meta.indexes` conflict | checked by a read in `pre_save` before the write | the same read, through the backend, plus a `UNIQUE` index inside the write as the authority (it also catches two conflicting saves in one `transaction()`); same `ModelException` text either way (`tests/postgres/test_postgres_fields.py`) |
| An aware `time` in a `TimeField` / `SortedField(type=time)` (M1.1) | stored with its offset (`isoformat()`) | `ValueError` naming the field: a `time` column holds wall-clock time only. Use a `DatetimeField` when the offset matters. Pinned: `tests/postgres/test_postgres_fields.py::test_an_aware_time_is_refused` |
| `push()` on a capped `ListField` whose record was deleted (M1.1) | `LPUSH` recreates an orphan list key | raises `ModelException` (`UPDATE` finds no row). After a successful `push()` the in-memory list is the stored list, not a local prepend. Pinned: `test_push_on_a_record_that_no_longer_exists_raises` |
| `load_raw_hash`, `Query.keys(catchall=/clean=)` | Redis debug and inspection APIs | raise `BackendCapabilityError` |
| `Model.idle_seconds` (M4) | `OBJECT IDLETIME`: any read or write resets it (an `HGETALL` included) | whole seconds since the row's last write or, with `AccessTrackerMixin`, its last confirmed read; an unconfirmed read does not reset it. Pinned: `tests/postgres/test_postgres_recipes.py::test_idle_seconds_counts_a_confirmed_read` |
| `MemoryLifecycle` with a `KeyField` tier (M4) | promotion is a key migration (`save(migrate_key=True)`) | refused when you build it: `MemoryLifecycle(...)` raises `BackendCapabilityError` naming `IndexedField`, because every promotion would be a key migration, which v2 refuses. Declare the tier as an `IndexedField` on Postgres; promotion is then a plain save. Pinned: `test_a_key_tier_lifecycle_is_refused_at_construction_on_postgres`, `test_lifecycle_promotes_a_non_key_tier`, and `tests/test_memory_lifecycle.py`, whose Postgres leg runs every test against `IndexedField`-tier twins of its models |
| A question-queue delivery when another transaction holds a candidate's row (M4) | the script runs after the other write and sees it | `FOR UPDATE SKIP LOCKED`: that candidate is passed over for the next, as one another worker claimed would be. Pinned: `test_postgres_question_queue.py::test_a_candidate_another_writer_holds_is_skipped` |
| A proposal that duplicates two or more open candidates (M4) | folds into the first in `QuestionCandidate.query.filter(agent_id=…)`'s order: set order | the first in `_pk` order (the "order of results" row above, seen through dedup). Pinned on both legs by `tests/test_question_queue.py::TestPropose::test_a_proposal_duplicating_two_candidates_folds_into_the_first`, and counted as the queue probe's `dedup_order` class |
| `DefaultMemory`'s eviction counter (M4) | a Redis string `MemoryService.status()` reads | a `popoto_counter` row: a Postgres-bound `DefaultMemory` needs no Redis, and the Redis-only `MemoryService` does not report it |
| `ProvenanceJournal` with a caller `pipeline=` (M4) | a Redis pipeline: the annotation and close are queued, `target_closed` is `None` and `close_index` names the close in `execute()`'s results | the backend's unit of work only (anything else raises `ValueError`): the annotation and close run inside it, `target_closed` is known at the call, `close_index` is `None`. Pinned: `test_postgres_journal.py::test_a_caller_unit_of_work_carries_the_annotation_and_the_close` |
| `AppendOnlyMixin`: two saves of one key in one unit of work (M4) | both pass the guard (the documented intra-pipeline shape) | the second is refused (the guard reads inside the transaction). Pinned: `test_postgres_recipes.py::test_append_only_sees_its_own_transaction` |
| `async_get`/`async_filter`/`async_count`/… | native `redis.asyncio` (reads); a worker thread (writes) | the async backend: the sync call's Postgres I/O on `psycopg.AsyncConnection`, on the running loop, no thread (M5, [Async](#async-m5)) |
| `async_load(db_key=<str>)` (the type its signature names) | raises `AttributeError: 'str' object has no attribute 'redis_key'`: the native path reads `db_key.redis_key` (pre-existing, before M5). The sync `load(db_key=<str>)` works | runs the sync `load`: the record, or `None` when there is none. Pinned on both legs: `tests/test_async_parity.py::test_async_load_with_a_string_db_key_is_a_documented_divergence` |
| `async_all` on an `AccessTrackerMixin` model | stages a read per record (its native path hydrates through `_async_get_many_objects`), although `all()` is non-tracking by design | does not stage, as `all()` does not (before M5 too) |
| `ExistenceFilter.might_exist` (M2b) | a bloom filter: false positives are possible, and a deleted record stays "seen" | exact: no false positives, and a deleted record is forgotten (plan §1.1). Pinned on both legs: `test_existence_filter.py::TestMembershipExactness` |
| `FrequencySketch.get_frequency` (M2b) | a count-min sketch: never under, may be over | the exact count of saves (never decremented, like the sketch). Pinned: same class |
| `ExistenceFilter.fill_ratio` (M2b) | the fraction of set bits | an estimate, `1 - e^(-k·n/m)` for the `n` distinct tokens stored |
| `save(update_fields=[…])` naming a `BM25Field`'s or `EmbeddingField`'s **source**, or only a **scope** column (M2b) | only the listed fields' hooks run, so the BM25 index keeps the old text or scope, and the vector (and its hash) the old text | re-indexes and re-embeds on the source; moves the postings and the narrow vector row on a scope change. Pinned: `test_update_fields_naming_the_source_reindexes`, `test_update_fields_naming_the_source_re_embeds` |
| `save(update_fields=[…])` naming a partitioned sorted field (`DecayingSortedField`, `SortedField(partition_by=…)`) but not its partition column, after an unsaved change to that column (M2b, #774 review) | the field's hook reads the partition from the instance, so the member moves to the instance's partition sorted set while the hash keeps the old value: `filter(agent="B").top_by_decay()` finds a record whose `agent` is `"A"` (#771) | the partition is the stored column, so the record stays where the row says; the BM25 postings, document length and narrow vector row follow the stored row too. Pinned: `test_backend_parity_memory.py::test_a_partial_save_with_an_unsaved_partition_is_a_documented_divergence` |
| `EmbeddingField` storage (M2b) | a `.npy` file per record plus `_index.json`, and an in-process matrix cache | the `vector(d)` column. No file, no cache, and `garbage_collect` / `sweep_stale_tempfiles` return `0`. The tests that assert files are `redis_only` |
| Vector-arm ties and precision (M2b) | equal similarities come back in directory-listing order; numpy float32 dot products | ties by key, bytewise; pgvector's `<=>`. Both are float32 accumulations in a different order, so similarities differ by an amount that grows with the dimension. Measured maximum absolute difference over 10,000 vector-query pairs per dimension (clustered Gaussian vectors, PostgreSQL 18.6, pgvector 0.8.7): 2.5e-7 at 2-d, 2.3e-7 at 8-d, 5.3e-7 at 64-d, 8.9e-7 at 256-d, 1.2e-6 at 768-d, 1.4e-6 at 1024-d, 1.7e-6 at 1536-d (the #774 review measured 1.56e-6) and 2.3e-6 at 3072-d. So 1e-6 holds only up to about 256 dimensions; above that, near-ties can order differently |
| `ContentField` (M2b) | a `$CF:` reference in the hash, with the content in a file store | the content itself in a `text` column |
| `BM25Field.recompute_stats` (M2b) | corrects the running `avgdl`'s floating drift | a no-op: `N` and `avgdl` are counted live |
| The `$BM25:` / `$EF:` / `$FS:` keys (M2b) | the index, readable through the raw client | not used: postings, length and token tables. The tests that read the keys are `redis_only` |
| A reload after `touch()` (M2a) | `touch` moves only the sorted-set score, so the hash, and a reload, keep the save-time value | the clock is the field's column, so a reload sees the touched time. Pinned: `test_a_reload_after_touch_is_a_documented_divergence` |
| `DecayingSortedField.rank_decayed(zset_key, …)` (M2a, TD-40) | ranks that sorted set | raises `BackendCapabilityError` naming `top_by_decay`, the backend-neutral call |
| `composite_score({"priority": …})` (M2a) | ranks by the WriteFilter priority set | raises `BackendCapabilityError`: the priority tier is a no-op on Postgres (plan §5 M2) |
| `composite_score(similarity_boost=…, co_occurrence_boost=…)` (M2b, M4) | injects each boost as an arm | an arm on both. A key with no record cannot take a top-K slot on Postgres, where on Redis it takes one and is then dropped at hydration, so Redis can return fewer records (never in another order). Pinned: `test_backend_parity_memory.py::test_composite_co_occurrence_boost_is_an_arm_on_both_backends`; the graph probe's `composite_orphan_slot` class |
| A NaN edge weight (`link(…, initial_weight=nan)` on a new edge, a NaN `strengthen` or `weaken_all` result) (M4) | `ZADD` refuses it: `ResponseError: value is not a valid float`. A symmetric write whose *second* script fails keeps the first script's write | `ValueError` with the same text, and the whole write rolls back. Pinned: `test_postgres_graph.py::test_a_nan_weight_raises_value_error` |
| A NaN weight in `CoOccurrenceField.import_state` (one that survives the `max_edges` truncation) (M4) | `DELETE` runs, then `ZADD` refuses the NaN: `ResponseError: value is not a valid float`, and the record's edge set is left **empty** | refused before any write: `ValueError` with the same text, and the edge set is unchanged. No NaN edge is stored on either (stored, Postgres would rank it above every weight and `least(NaN, cap)` is the cap). Pinned on both legs: `test_co_occurrence_field.py::TestImportStateAndErrorParity::test_a_nan_import_is_refused_a_documented_divergence`; the graph probe's `nan_import_keeps_set` class |
| `CoOccurrenceField.get_linked(…, limit=None)` (M4) | redis-py refuses it client-side: `DataError: ``start`` and ``num`` must both be specified` (before any NaN-bound check) | `ValueError` with the same text, in the same order. Pinned on both legs: `test_get_linked_limit_none_a_documented_divergence`; the graph probe's `limit_none_error_type` class |
| A NaN `min_weight` in `CoOccurrenceField.get_linked` (M4) | `ResponseError: min or max is not a float` | `ValueError` with the same text. Pinned on both legs: `test_get_linked_nan_min_weight_a_documented_divergence`; the probe's `nan_error_type` class |
| The order of `propagate()`'s dict, and so of equal weights in `graph_traversal.traverse()` (M4) | Lua table iteration order | weight descending, then key bytewise. The dict and its weights are equal on both; `traverse()` sorts by weight, so only ties can be listed in another order (the graph probe's `traverse_tie_order` class) |
| `link()`'s reply for a weight at or past `2**63` in magnitude (M4) | the server's C `(long long)` cast of the Lua number: `-inf` and anything under `-2**63` reply `-2**63` on arm64 and x86-64; above `2**63` (only with a cap past it) arm64 saturates to `2**63 - 1`, x86-64 replies `-2**63` | the arm64 values |
| Where a NaN decay score ranks (M2a) | NaN (`0 * inf`: a `-inf` clock with above-prior confidence) makes the script's comparator inconsistent (`x > nan` is always false), so `table.sort` places it arbitrarily and can misorder real scores around it | real scores sorted, NaN last; every member's score is the same on both. Pinned: `test_where_a_nan_score_ranks_is_a_documented_divergence` |
| A NaN decay score in `composite_score` (M2a) | `rank_decayed` replies `nan` (`0 * inf`: a `-inf` clock with above-prior confidence) and the composite's `ZADD` refuses it: `ResponseError: value is not a valid float` | that arm scores 0 for the record, the value `ZUNIONSTORE` gives a NaN product. Pinned: `test_a_nan_decay_score_in_composite_is_a_documented_divergence` |
| The confirmed access log (M2a) | a capped list of read timestamps (`$AT:…:access_log`) | not kept: `access_count` and `last_accessed` are. It is read only by `export_state`, which arrives with `transfer/` in M5 |
| `update_confidence(…, pipeline=uow)` with a Postgres `transaction()` (M2a) | (a Redis pipeline queues the update and returns `None`) | the update runs inside the transaction, so its value is returned and the attribute synced |
| `CyclicDecayField.rank_decayed(zset_key, …)` (M5) | ranks that sorted set with `CYCLIC_DECAY_LUA` | raises `BackendCapabilityError` naming `top_by_decay`, as `DecayingSortedField.rank_decayed` does |
| A cycles entry written raw (`import_state`) (M5) | Python msgpack keeps the importer's `float` for an integral period or baseline until the next save or adjustment re-packs it through cmsgpack | cmsgpack's `int` at once. Values identical (probe class `cyclic_merge_raw_types`) |
| A non-numeric cycle slot (a string period, a malformed entry) (M5) | storable; the merge falls back to the declaration with a warning, and the ranking may raise | unrepresentable: `import_state` refuses a non-numeric slot with `ValueError` |
| `strengthen_cycle` / `weaken_cycle` / `resolve_pressure` / `td_update` on a record that no longer exists, or has expired (M5) | `resolve_pressure` and `td_update` write orphan companion or hash entries (`HSET`) | nothing is written; `td_update` replies what the script replies from `Q = 0` (on an expired record that is the same reply: Redis's `HGET` of the expired key is `nil`) |
| A non-numeric `strengthen_cycle` / `weaken_cycle` factor (`None`, `"abc"`, `True`) (M5) | the script's multiplication fails: `ResponseError: user_script:15: attempt to perform arithmetic on local 'factor' (a nil value) …`, nothing written | `ValueError` with the same text, before anything is written. Pinned on both legs: `test_backend_parity_longtail.py::test_a_non_numeric_factor_is_refused_before_writing` |
| `td_update` on a stored `Decimal('-0')` (M5) | `tonumber("-0")` is `-0.0`, so with a target of `-0.0` the TD error is `-0.0 - -0.0` = `0.0` | a `numeric` has no `-0`: the value is stored as `0`, and the reply is `-0.0 - 0.0` = `-0.0`. The stored values compare equal. Pinned on both legs: `test_backend_parity_longtail.py::test_td_update_from_a_negative_zero_is_a_documented_divergence` |
| A NaN `CyclicDecayField` score (M5) | the script's comparator is inconsistent around it (`x > nan` is false): its sort may misplace real scores, or raise "invalid order function for sorting" | NaN ranks last and every other score keeps its place (M2a's rule; probe class `cyclic_rank_nan`) |
| `td_update(…, pipeline=uow)` with a Postgres `transaction()` (M5) | (a Redis pipeline queues the script and returns `None`) | the update runs inside the transaction, so the TD error is returned, as `update_confidence` does |
| A NaN `td_update` value (M5) | stored as `tostring(nan)`, `"nan"` or `"-nan"` by platform | `numeric` `NaN`, unsigned |
| The key order of a resolved ledger entry (M5) | cmsgpack's Lua-table iteration order | the entry's own order. Dicts compare equal |
| A NaN prediction error (M5) (`inf` against a number, `inf - inf`) | the script marks the entry resolved, then its `ZADD` refuses the score (`ResponseError: value is not a valid float`), and Redis does not roll the `HSET` back: the entry reads resolved with a NaN error and no error-set member | refused before anything is written: `ValueError` with the same text, and the entry stays unresolved. Pinned on both legs: `test_backend_parity_longtail.py::test_a_nan_prediction_error_is_a_documented_divergence`; the probe's `ledger_nan_error` class |
| An integer in a ledger entry that rounds to `2**63` or more as a double (M5) | cmsgpack's conversion is undefined behaviour in C: the re-packed value differs by platform (`-2**63`, `-1`) | the double |
| `execute_supersede(mode="open")` naming a member with no record (M3) | `ZADD NX` indexes the member anyway | writes nothing: the interval is the record's row. Only a direct `execute_supersede` call can ask for it. Pinned: `tests/postgres/test_postgres_validity.py::test_mode_open_on_a_member_with_no_record_writes_nothing` |
| An open-claim pointer naming a record that does not exist (M3) | storable (a manual `SET`, or a partial `import_state`); `supersede` reads it as "no incumbent" | unrepresentable: the pointer table's foreign key refuses it, and deleting a record cascades to its pointers. `import_state` for a record that is not stored raises `ValidityMemberAbsentError` (a `ValidityError`, so a `ValueError`) chained from the driver's `ForeignKeyViolation`; Redis's `import_state` never raises there. Pinned: `test_a_pointer_cannot_name_a_record_that_does_not_exist` |
| `save_and_supersede` / `save_and_invalidate` whose close fails (M3) | `MULTI`/`EXEC` keeps the successor's save, and the typed error's text carries redis-py's `Command # N (...) of pipeline caused error:` prefix | the whole unit rolls back, so the successor is not saved either; same exception type, and the text is the bare reply line |
| A NaN `valid_from` on save (M3) | the script's `ZADD` refuses it: `ResponseError: … value is not a valid float` from the pipelined `EVALSHA`, after `MULTI`/`EXEC` has written the record's hash, so the record exists with no interval | refused before anything is written: `ModelException("value is not a valid float")` -- the same text, popoto's save error (as row (v) of the query table). Stored, a NaN start would sort above every float and hide the record from every gate. Pinned: `tests/test_validity_parity.py::TestNanInstants::test_a_nan_valid_from_on_save_is_refused` |
| A NaN instant in `supersede` / `invalidate` / `execute_supersede` (M3) | `ResponseError: value is not a valid float script: …`. With only `valid_from` NaN (a real close instant), `SUPERSEDE_LUA`'s validation phase lets it through and the successor's `ZADD` fails in the mutation phase, after the incumbent was closed and chained, with the pointer still naming it: half-written state, issue #778 | `ValueError("value is not a valid float (<instant> is NaN)")`: the same text, a different class, raised before the first write, so nothing is written. Pinned: `TestNanInstants::test_a_nan_at_is_refused_and_writes_nothing` and `::test_a_nan_valid_from_alone_in_execute_supersede` |
| A NaN `as_of` / `validity__as_of` (M3) | the range reads (`filter`, `resolve_*_keys`, the composite mask) raise `ResponseError: min or max is not a float`; the decay ranking's gate excludes nothing | `QueryException` with the same text (as row (v) of the query table); the decay ranking excludes nothing |
| `SupersessionProtocol.supersede`/`invalidate` with the backend's `transaction()` as `pipeline` (M3) | (a Redis pipeline queues the script; the closed key is unknown until `execute()`) | runs inside the transaction: the closed key is returned and a typed error raised at the call. A Redis pipeline is refused with `ValueError` by `supersede`, `invalidate` and `save_and_*` alike: it cannot carry a Postgres write. Pinned: `test_a_redis_pipeline_is_refused_by_supersede_and_invalidate` |
| `_ttl` / `_expire_at` on a model without `Meta.ttl` (M5) | `EXPIRE`/`EXPIREAT` on the hash | `BackendCapabilityError` before anything is written: only a `Meta.ttl` model has `_expires_at` and the read filter, so a model that never expires keeps plans that never check. Declare `Meta.ttl`; an instance can opt out with `_ttl = None`. Pinned on both legs: `test_backend_parity_ttl.py::test_an_instance_ttl_without_meta_ttl_is_a_documented_divergence`, and `tests/postgres/test_postgres_ttl.py::test_an_instance_ttl_needs_meta_ttl` |
| A `_ttl` that is not a whole number (`1.5`) (M5) | `MULTI`/`EXEC` writes the hash, then `EXPIRE` fails: `ResponseError: value is not an integer or out of range`, and the record stays with its old TTL, or none | `ModelException` with the same text, before anything is written. Pinned: `test_backend_parity_ttl.py::test_a_fractional_ttl_is_a_documented_divergence` |
| `count()` / `keys()` after a record expires (M5) | count the class or index set, which keeps the expired member until a hydrating read (`get`, `filter`, `all`) purges it or `clean_indexes` runs | count live rows only. Probe class `count_orphans`. Pinned: `test_backend_parity_ttl.py::test_count_after_expiry_is_a_documented_divergence` |
| Rankings, search and membership after a record expires (M5) | the sorted sets, BM25 postings, vector file and bloom keep the member: `top_by_decay(n=…)` can give a slot to it and return fewer than `n` after hydration drops it, BM25's `N`/`avgdl`/`df` count it, and `might_exist` stays `True` | the record is in none of them from the instant it expires: `n` live records, statistics over live documents, `might_exist` `False` once no live record holds the token. Pinned: `test_backend_parity_ttl.py::test_ranking_after_expiry_is_a_documented_divergence`, and each read in `tests/postgres/test_postgres_ttl.py::test_every_public_read_misses_an_expired_record` |
| State keyed by an expired record (M5): its confidence entry, validity intervals and open-claim pointers, staged reads, `CyclicDecayField` cycles and pressure | kept in their own keys until something cleans them; `ConfidenceField.update_confidence` refuses (its script checks the hash); `strengthen_cycle` / `weaken_cycle` still adjust the cycles entry, `export_state` returns it, and a save over the key merges with it | gone with the row: reads see the seed / no interval / no pointer, and `chain` stops at it as at a hard delete; an adjustment finds no entry (`[]`), `export_state` returns `None`, and a save starts from the declaration. `update_confidence` refuses on both. Pinned on both legs: `test_backend_parity_ttl.py::test_state_keyed_by_an_expired_record_is_a_documented_divergence` and `test_backend_parity_longtail.py::test_cycle_state_of_an_expired_record_is_a_documented_divergence`; the validity half in `tests/postgres/test_postgres_ttl.py::test_validity_reads_miss_an_expired_record`. The prediction ledger is **not** in this row: its `EXISTS` guard misses an expired record on both legs (`record`/`resolve` raise `TypeError`, `auto_resolve` returns `None`), and its entries outlive the record on both, as they outlive a delete (`test_the_ledger_treats_an_expired_record_as_absent`) |
| A save over an expired key (M5) | `HSET` creates a new hash, but the expired record's companion state (confidence entry, BM25 postings, interval) is still there for the new one to inherit | the expired row and its side rows are deleted first: a fresh record, confidence at the seed. Pinned on both legs: `test_backend_parity_ttl.py::test_a_save_over_an_expired_key_is_a_documented_divergence`; the postings in `tests/postgres/test_postgres_ttl.py::test_a_save_over_an_expired_key_writes_a_fresh_record` |
| `atomic_increment` through an instance whose record expired (or was deleted) under it (M5) | the script `HSET`s the field onto a fresh key: a one-field hash with no TTL, outside the class set | `ModelException` (`… no longer exists`), as for any missing row. Probe class `increment_after_expiry`. Pinned: `test_backend_parity_ttl.py::test_increment_after_expiry_is_a_documented_divergence` |
| The instant of expiry (M5) | a key is expired once the server's millisecond clock is *past* its expiry, so it is still there at that exact millisecond | a row is expired once `_expires_at <= now` (microseconds), so `ttl=0` is gone even under a frozen clock. Observable only with the frozen test clock: two real clocks never land on one instant |
| Removing `Meta.ttl` from a model whose table has `_expires_at` (M5) | the next save simply stops issuing `EXPIRE`; the key keeps the TTL it had | `SchemaDriftError` (a column the model no longer declares): drop the column by hand, or keep `Meta.ttl` and set `_ttl = None` per instance. Pinned: `test_backend_parity_ttl.py::test_removing_meta_ttl_is_a_documented_divergence` |
| A failed statement inside `popoto.batch()` (M5) | `MULTI`/`EXEC` applies the other queued commands | the whole batch rolls back; `execute()` raises `BackendError`. Pinned: `test_backend_parity_ttl.py::test_a_failed_batch_is_a_documented_divergence` |
| A `popoto.batch()` used for both Redis and Postgres writes (M5) | one store: a raw command and a model save share one `MULTI`/`EXEC` | refused with `BackendCapabilityError` before the second backend's first command, in either order. Pinned on both legs: `test_backend_parity_ttl.py::test_a_batch_of_raw_commands_and_model_writes_is_a_documented_divergence`; the reverse order in `tests/postgres/test_postgres_ttl.py::test_a_batch_refuses_to_mix_backends` |
| A write that waits on a record an open batch or `transaction()` on the same thread holds (M5): a nested batch writing the same record, or a plain save of a record saved in an open batch | a queued command holds no lock: both apply, in execution order | `BackendCapabilityError` before anything is sent (it could never be granted); the holding batch is unharmed. Another thread waits for the commit. Pinned: `tests/postgres/test_postgres_ttl.py::test_nested_batches_writing_one_record_are_refused_at_once`, `::test_a_write_outside_the_batch_to_a_record_in_it_is_refused` |

## Validity and supersession (M3)

A `ValidityField` keeps the six Redis keys' state on the record's row and in
one companion table:

| Redis key | Postgres |
|---|---|
| `…:valid_from` / `…:invalid_at` / `…:ingested_at` sorted sets | `<f>__valid_from` / `<f>__invalid_at` / `<f>__ingested_at`, `double precision` (`NULL` = absent from that index; `'Infinity'` = open) |
| `…:chain:fwd` / `…:chain:rev` hashes | `<f>__superseded_by` / `<f>__supersedes` on the record |
| `…:open:{digest}` strings | `<table>__<f>__open (digest PRIMARY KEY, member)`, `member` referencing `_pk` `ON DELETE CASCADE` |

**Why not `tstzrange`.** The plan proposed one `tstzrange` column. A
`timestamptz` keeps microseconds, so the Redis score `1700000000.1234567`
comes back `1700000000.123457`, two scores one ulp apart become one instant,
and the gate's `invalid_at <= as_of` flips for a close one ulp after `as_of`
(pinned: `tests/postgres/test_postgres_validity.py::test_timestamptz_would_not_hold_the_redis_score`,
`tests/test_validity_parity.py::TestExclusionRule::test_the_as_of_bound_is_bit_exact`).
The second reason is a close at the record's own start, which the script
allows (its check is `close < start`): `tstzrange(t, t)` is the canonical
`empty` range and keeps neither bound, so the record's start and its recorded
close are both lost (and `lower > upper` raises). A range *can* tell "no
`invalid_at` recorded" from "`invalid_at` is `+inf`" (`upper_inf('[t,)')` is
true, `upper_inf('[t,infinity)')` false), so that is not a reason; the columns
spell it `NULL` vs `'Infinity'`. So the interval follows M2a's clock decision:
`double precision` epoch seconds, bit-identical to the score.

**The exclusion rule** every gate applies -- `top_by_decay`'s ranking,
`composite_score`'s mask, `ValidityField.resolve_excluded_keys`, the
assembler -- is `invalid_at <= as_of OR valid_from > as_of`, with an absent end
never excluding, as one `WHERE` term on the row. `filter(validity__as_of=t)`
and `resolve_valid_keys` are the whitelist, `valid_from <= t AND invalid_at >
t`, both ends present; `__current=False` is the members of either index that
are not valid now. The #631 POC's 17-row table (`tests/test_validity_parity.py`)
holds on both backends.

**`supersede`** is `SUPERSEDE_LUA` phase for phase, in one transaction:

1. `pg_advisory_xact_lock(hashtext('popoto:validity:<schema>.<table>.<f>'))`
   -- one lock per model and field, the Redis single thread;
2. resolve the incumbent: the named one, else the identity's pointer;
3. the record-key locks of the successor and the incumbent, in `_pk` byte
   order, then `SELECT … ORDER BY _pk COLLATE "C" FOR UPDATE` on both rows;
4. validate, raising the typed error built from the script's reply line (and
   so with the same text) and writing nothing: a successor that does not
   exist, an *asserted* incumbent that does not exist (a pointer-resolved one
   is "no incumbent"), a close before the incumbent's start, an asserted
   `valid_from` that disagrees with the stored one;
5. mutate: close the incumbent if it is open (idempotent), both chain links,
   open the successor `NX` (an absent end filled, a closed record never
   reopened), repoint the pointer.

| Reply line | Exception (a `ValueError`) |
|---|---|
| `POPOTO_VALIDITY_MEMBER_ABSENT successor <key>` | `ValidityMemberAbsentError` |
| `POPOTO_VALIDITY_MEMBER_ABSENT incumbent <key>` | `ValidityMemberAbsentError` |
| `POPOTO_VALIDITY_CLOSE_BEFORE_START` | `ValidityCloseBeforeStartError` |
| `POPOTO_VALIDITY_VALID_FROM_CONFLICT <stored> <requested>` (numbers as Lua's `%.14g`) | `ValidityValidFromConflictError` |

A save opens the interval inside its upsert, the script's mode `'open'`, and a
declared `valid_from` that disagrees with the stored start fails the upsert's
guard, so nothing is written (behind the same `pre_save_validate` check as on
Redis). `chain` is one `WITH RECURSIVE` with the Redis walk's stop rules: a
missing link, a record already visited, a link naming a record with no
`valid_from`.

**Lock order.** Validity writers follow the backend's one lock order (see
"One lock order for every writer" above): the `(model, field)` lock, then the
record-key locks sorted by `_pk`, then the row locks. `supersede` takes them
in that order; `save_and_supersede` / `save_and_invalidate` take all of the
supersede's locks *before* the save (otherwise the save would hold the
successor's key and row while a concurrent supersede naming that record holds
the field lock and waits for them -- pinned:
`test_save_and_supersede_of_an_existing_record_takes_the_supersede_lock_order`,
which deadlocks every round with the pre-lock removed); `import_state` takes
the field lock as a pointer writer; and `ObservationProtocol.on_context_used`
takes the field lock before locking its batch, with a contradicted record's
successor locked as part of the batch. So two supersedes never interleave --
including the crossing chains that deadlocked the #631 POC (`d1 → X`
superseded by `Y` while `d2 → Y` is superseded by `X`; `TestCrossingChains`
forces the overlap ten times). A plain save does not take the field lock: it
meets a supersede on the record's key lock, and its upsert re-reads the row
it waited on. The residual is a caller's own `transaction()` that locks a
record and then supersedes while another supersede waits for that record:
Postgres detects the cycle and aborts one side. Usually the other, earlier
waiter is the victim and its owned transaction retries, so the caller
completes; when the caller is the victim it gets `BackendRetryableError`
(pinned: `test_a_cross_operation_deadlock_is_a_retryable_error`).

**The seeded probe.** `scripts/probe_validity_parity.py` runs the same random
sequences of saves (declaring and re-declaring starts), supersedes,
invalidations, direct `execute_supersede` calls in every mode, `save_and_*`
and deletes on both backends, comparing every return value and exception
(type and text), the intervals, links, pointers and chains, and every gated
read at the interval ends, `±inf`, `1e308` and NaN. Its documented classes are
the M3 rows of the table above; `tests/postgres/test_validity_probe.py` runs a
25-shape slice in CI.

```bash
REDIS_URL=redis://localhost:6379/10 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/probe_validity_parity.py --seeds 1 2 3 --shapes 200
```

## Performance (M1, M2a and M3 exit criteria)

`scripts/bench_backend_seam.py` measures the public API on both backends. It
seeds 2,000 records, runs `ANALYZE`, and then makes three runs of 300
iterations per operation, with the two backends interleaved within each run.
The M1 targets are `Model.save()` p50 at most 2x Redis and `filter` +
hydration p50 at most 1x Redis. M2a adds `rank_decayed` with a base score and
confidence modulation over all 2,000 records (top 10): Postgres p50 at most
1x Redis, `DECAY_SCORE_LUA` against one `SELECT`; `top_by_decay` with
hydration is measured beside it. M3 adds the same ranking with a validity gate
(a tenth of the records superseded, a twentieth not yet started): Postgres
p50 at most 1x Redis. The PRs that introduced each milestone
record the measured numbers and the environment they were taken on.

```bash
REDIS_URL=redis://localhost:6379/14 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/bench_backend_seam.py
```

### `recall()` (M2b exit criterion)

`scripts/bench_recall.py` seeds a 20,000-row, 1536-dimension corpus through
`Model.save()`: five projects (60 / 20 / 10 / 5 / 5 %), documents of 40-80
tokens from a 30,000-term Zipf vocabulary, and clustered synthetic vectors.
It runs `VACUUM ANALYZE`, then makes three runs of 200
`recall(q, scope=p, limit=10)` calls at a 5% and a 60% scope, and times each
arm alone. The targets are at most 15 ms p95 at 5% scope and at most 60 ms
p95 at 60% scope. The 5% scope (1,000 vectors) takes the exact path, over
the narrow vector table; before that table, reading 1,000 TOASTed 6 kB
vectors from the record table was most of the time, and the 5% p95 sat
at 13-25 ms. The 60% scope (12,000 vectors) takes HNSW. Each run prints the
load average.

Measured for #774 (Apple M1 Max, PostgreSQL 18.6 with `shared_buffers`
128 MB, pgvector 0.8.7, one 20k corpus, six invocations of three runs each;
the machine was shared, load average 4.7-7.5 throughout):

| Scope | Path | p50 (typical) | p95 per run | Runs within the bar |
|---|---|---|---|---|
| 5% | exact, narrow table | 5.6-5.9 ms (11.5-13.4 ms in the slow windows) | 6.5-10.8 ms in 13 runs; 16.4-20.2 ms in 5 | 13 of 18 (bar 15 ms) |
| 60% | HNSW | 9.9-17.8 ms | 16.8-46.3 ms | 18 of 18 (bar 60 ms) |

The five slow 5% runs fell in windows where the whole machine slowed: the
same index-only count statement went from 0.4 ms to 1.0 ms p50 and every
arm doubled together. Run alone in one window, the exact arm on the record
table (TOAST) took p50 7.9 ms, p95 9.1 ms, and on the narrow table p50
2.9 ms, p95 3.6 ms.

```bash
POPOTO_POSTGRES_URL=postgresql://localhost:5432/popoto_bench \
    python scripts/bench_recall.py [--n 20000] [--dim 1536] [--keep | --reuse SCHEMA]
```
