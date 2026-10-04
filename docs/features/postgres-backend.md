# Postgres Backend (v2)

Popoto v2 keeps one model API with two native storage backends behind it
([#759](https://github.com/tomcounsell/popoto/issues/759)). Redis keeps
today's hashes, index sets and Lua. Postgres stores each model in a typed
table with native indexes, and it is where new capabilities land.

This page covers the first Postgres milestones: **plain models** (M1),
**plain-field breadth** (M1.1), the **ranking and memory-state half of
Valor's slice** (M2a), **search** (M2b), **`ContextAssembler`** (M2c), and
the **co-occurrence graph and the remaining recipes** (M4).
That means records, queries, `Q` objects, ordering, counting and atomic
increments for the field types listed below, including indexed, unique, tag,
relationship and collection fields, plus decay ranking, confidence, read
tracking, the write filter, `ObservationProtocol` and `composite_score`, BM25
keyword search, pgvector embeddings, exact membership filters, fusion and
`recall()`, the assembler over all of them, and `CoOccurrenceField` with its
graph expansion. Models that use
other fields stay on Redis until their milestone. Popoto refuses them when you
declare them, so they never fail halfway through.

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

## Supported fields (M1, M1.1, M2a, M2b, M4)

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
| `ConfidenceField` (M2a) | `double precision` for the attribute, plus the state `<f>__conf` (`double precision`), `<f>__n`, `<f>__corr`, `<f>__contra` (`bigint`) | — |
| `BM25Field(source=…)` (M2b) | no column of its own: postings `<table>__<f>__post (scope, term, _pk, tf)` and lengths `<table>__<f>__dl (_pk, scope, len)` | postings `PRIMARY KEY (scope, term, _pk)` plus a B-tree on `_pk`; lengths `(scope) INCLUDE (len)` |
| `EmbeddingField(source=…)` (M2b) | `<f> bigint` (the dimension count Redis stores), `<f>__vec vector(d)`, `<f>__model text`, `<f>__hash text`, plus the narrow vector table `<table>__<f>__vec (_pk, scope, v vector(d))`, `v` stored `PLAIN` | HNSW `vector_cosine_ops` on `<f>__vec`; a partial B-tree on rows with no vector, and a B-tree on `<f>__model` (the backfill's probe); `(scope)` on the narrow table |
| `ExistenceFilter` / `FrequencySketch` (M2b) | no column: `<table>__<f>__tok (token, _pk)` / `<table>__<f>__cnt (token, count)` | `PRIMARY KEY (token, _pk)` / `PRIMARY KEY (token)` |
| `ContentField` (M2b, pulled forward from M5) | `text` holding the content itself (no `$CF:` reference, no file) | — |
| `CoOccurrenceField(symmetric=…, max_edges=…)` (M4) | no column: the edge table `<table>__<f>__edge (src, dst, weight)` | `PRIMARY KEY (src, dst)` |

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
A partitioned `ConfidenceField` arrives in M3 and is refused until then.
`PredictionLedgerMixin` keeps its ledger in Redis until M5 and is refused on a
Postgres model too, rather than issuing Redis commands for a record Redis does
not hold.

Fields that arrive later: the remaining memory field (validity; M3);
`GeoField`, `Meta.ttl` and the rest (M5); an `IndexedField` on a collection type is refused. A model
that uses one of them raises
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
under `pg_advisory_xact_lock`. No manual step is needed. Anything else raises
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
`ExistenceFilter` row. The order is any `(model, field)` lock (none before
M3), then the record-key locks sorted by `_pk`, then the row locks in `_pk`
order. A record's key lock is always the first lock taken on it, so a
transaction that runs `update_confidence(x, pipeline=tx)` and then
`x.save(pipeline=tx)` queues a concurrent `x.save()` behind it instead of
deadlocking with it. Transactions that each take several records in
different orders can still deadlock; that surfaces as
`BackendRetryableError`. Pinned by
`test_a_confidence_update_then_save_cannot_deadlock_a_save` and its control.

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

**`Query.top_by_relevance(scope=None, limit=10)`** (`[PG-only]`, #758 D8)
returns `[(instance, score)]` ranked by decay × confidence in SQL, the score
`top_by_decay` ranks by. It replaces reading a decay sorted set with a raw
`ZREVRANGE`. `scope` is the decay field's `partition_by` value; `None` ranks
every partition together. On a Redis-bound model it raises
`BackendCapabilityError`; use `top_by_decay` there.

**`composite_score(similarity_boost=…)`** works on Postgres too (M2b). The
mapping is one more arm, weight 1.0 and last, as its temporary set is in the
Redis `ZUNIONSTORE`. `semantic_search(indexes=…)` uses it. The order is the
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
| Score proxy (`assess_quality`, `emit_trace`, `assess`) | the partition's `rank_decayed` for a decay field, the column for a plain sorted field |
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
the record's `DELETE`. Every tie-break is `COLLATE "C"`, the sorted set's
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

`safe_mul` is the saturating product of the decay SQL (M2a). Outside that
domain -- a threshold at or below `0` (or under `1e-290`), a negative or
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
deletes and `import_state` -- comparing every return, then each key's full
edge set, `get_linked`, `propagate` on both Postgres paths, `export_state`,
`composite_score` with the boost and `traverse()`. Its classes are in the PR
that introduced M4; `tests/postgres/test_postgres_graph.py` runs a 25-shape
slice in CI.

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
| `Model.idle_seconds` | `OBJECT IDLETIME`: whole seconds since the key was last read or written | whole seconds since the later of the row's last write (`_updated_at`) and, with `AccessTrackerMixin`, its last *confirmed* read (`_last_accessed`); `None` with no row |
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

Before #759 M2a's patch these reached the caller as raw psycopg
`DeadlockDetected` / `SerializationFailure`. A statement whose completion is
unknown (SQLSTATE 40003) is not retryable -- it may have committed -- and is
reported as `BackendUnavailableError`.

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
| A Redis `pipeline=` handed to a Postgres model | queued | the write runs immediately, and the pipeline comes back untouched for the caller to execute |
| `atomic_increment` of an `IntField` by a non-integral delta (`hits=5`, `+1.5`) | stores the float sum `6.5` and returns it truncated, `6` | stores and returns the sum cast to `bigint`, rounded half away from zero: `7`. Neither is well defined; pass an `int` delta. |
| Order of results with no `order_by`, no `Meta.order_by` and no sorted-field filter | set order (arbitrary) | `_pk` in bytewise (`COLLATE "C"`) order |
| An invalid `order_by=` / `values=` on a query that matches nothing | returns `[]` before validating | raises the same `QueryException` either way |
| `save(update_fields=…)` on a record that does not exist yet | writes a partial hash that stays out of the class set, so queries do not see it | inserts the row (unlisted columns `NULL`), so queries see it |
| `UniqueKeyField` / `UniqueField` / unique `Meta.indexes` conflict | checked by a read in `pre_save` before the write | the same read, through the backend, plus a `UNIQUE` index inside the write as the authority (it also catches two conflicting saves in one `transaction()`); same `ModelException` text either way (`tests/postgres/test_postgres_fields.py`) |
| An aware `time` in a `TimeField` / `SortedField(type=time)` (M1.1) | stored with its offset (`isoformat()`) | `ValueError` naming the field: a `time` column holds wall-clock time only. Use a `DatetimeField` when the offset matters. Pinned: `tests/postgres/test_postgres_fields.py::test_an_aware_time_is_refused` |
| `push()` on a capped `ListField` whose record was deleted (M1.1) | `LPUSH` recreates an orphan list key | raises `ModelException` (`UPDATE` finds no row). After a successful `push()` the in-memory list is the stored list, not a local prepend. Pinned: `test_push_on_a_record_that_no_longer_exists_raises` |
| `load_raw_hash`, `Query.keys(catchall=/clean=)` | Redis debug and inspection APIs | raise `BackendCapabilityError` |
| `Model.idle_seconds` (M4) | `OBJECT IDLETIME`: any read or write resets it (an `HGETALL` included) | whole seconds since the row's last write or, with `AccessTrackerMixin`, its last confirmed read; an unconfirmed read does not reset it. Pinned: `tests/postgres/test_postgres_recipes.py::test_idle_seconds_counts_a_confirmed_read` |
| `MemoryLifecycle` promotion of a `KeyField` tier (`tick()`, `tag_new`) (M4) | a key migration (`save(migrate_key=True)`) | refused, as every key migration is in v2: `tick()` logs and skips the record, `tag_new` raises `BackendCapabilityError`. Declare the tier as a non-key field (an `IndexedField`) on Postgres. Pinned: `test_a_key_tier_promotion_is_refused_on_postgres`, `test_lifecycle_promotes_a_non_key_tier` |
| A question-queue delivery when another transaction holds a candidate's row (M4) | the script runs after the other write and sees it | `FOR UPDATE SKIP LOCKED`: that candidate is passed over for the next, as one another worker claimed would be. Pinned: `test_postgres_question_queue.py::test_a_candidate_another_writer_holds_is_skipped` |
| A proposal that duplicates two or more open candidates (M4) | folds into the first in `QuestionCandidate.query.filter(agent_id=…)`'s order: set order | the first in `_pk` order (the "order of results" row above, seen through dedup); the queue probe's `dedup_order` class |
| `DefaultMemory`'s eviction counter (M4) | a Redis string `MemoryService.status()` reads | a `popoto_counter` row: a Postgres-bound `DefaultMemory` needs no Redis, and the Redis-only `MemoryService` does not report it |
| `AppendOnlyMixin`: two saves of one key in one unit of work (M4) | both pass the guard (the documented intra-pipeline shape) | the second is refused (the guard reads inside the transaction). Pinned: `test_postgres_recipes.py::test_append_only_sees_its_own_transaction` |
| `async_get`/`async_filter`/`async_count`/… | native `redis.asyncio` | run the sync call in a worker thread (the async driver arrives in M5) |
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
| The order of `propagate()`'s dict, and so of equal weights in `graph_traversal.traverse()` (M4) | Lua table iteration order | weight descending, then key bytewise. The dict and its weights are equal on both; `traverse()` sorts by weight, so only ties can be listed in another order (the graph probe's `traverse_tie_order` class) |
| `link()`'s reply for a weight at or past `2**63` in magnitude (M4) | the server's C `(long long)` cast of the Lua number: `-inf` and anything under `-2**63` reply `-2**63` on arm64 and x86-64; above `2**63` (only with a cap past it) arm64 saturates to `2**63 - 1`, x86-64 replies `-2**63` | the arm64 values |
| `CoOccurrenceField.strengthen()` on a model with `EventStreamMixin` (M4) | appends a `strengthen` entry to the model's Redis stream | no entry: the event stream is Redis-only until M5 |
| Where a NaN decay score ranks (M2a) | NaN (`0 * inf`: a `-inf` clock with above-prior confidence) makes the script's comparator inconsistent (`x > nan` is always false), so `table.sort` places it arbitrarily and can misorder real scores around it | real scores sorted, NaN last; every member's score is the same on both. Pinned: `test_where_a_nan_score_ranks_is_a_documented_divergence` |
| A NaN decay score in `composite_score` (M2a) | `rank_decayed` replies `nan` (`0 * inf`: a `-inf` clock with above-prior confidence) and the composite's `ZADD` refuses it: `ResponseError: value is not a valid float` | that arm scores 0 for the record, the value `ZUNIONSTORE` gives a NaN product. Pinned: `test_a_nan_decay_score_in_composite_is_a_documented_divergence` |
| The confirmed access log (M2a) | a capped list of read timestamps (`$AT:…:access_log`) | not kept: `access_count` and `last_accessed` are. It is read only by `export_state`, which arrives with `transfer/` in M5 |
| `update_confidence(…, pipeline=uow)` with a Postgres `transaction()` (M2a) | (a Redis pipeline queues the update and returns `None`) | the update runs inside the transaction, so its value is returned and the attribute synced |
| A model with a `CyclicDecayField`, `ValidityField`, partitioned `ConfidenceField` or `PredictionLedgerMixin` (M2a) | supported | refused at declaration until M5, M3, M3 and M5 respectively, so `ObservationProtocol`'s cycle, supersession, auto-discharge and ledger-resolution effects have no Postgres model to act on yet (`CoOccurrenceField` is stored from M4) |

## Performance (M1 and M2a exit criteria)

`scripts/bench_backend_seam.py` measures the public API on both backends. It
seeds 2,000 records, runs `ANALYZE`, and then makes three runs of 300
iterations per operation, with the two backends interleaved within each run.
The M1 targets are `Model.save()` p50 at most 2x Redis and `filter` +
hydration p50 at most 1x Redis. M2a adds `rank_decayed` with a base score and
confidence modulation over all 2,000 records (top 10): Postgres p50 at most
1x Redis, `DECAY_SCORE_LUA` against one `SELECT`; `top_by_decay` with
hydration is measured beside it. The PRs that introduced each milestone
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
