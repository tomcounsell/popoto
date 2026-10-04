# Postgres Backend (v2)

Popoto v2 keeps one model API with two native storage backends behind it
([#759](https://github.com/tomcounsell/popoto/issues/759)). Redis keeps
today's hashes, index sets and Lua. Postgres stores each model in a typed
table with native indexes, and it is where new capabilities land.

This page covers the first Postgres milestone (M1): **plain models**. That
means records, queries, `Q` objects, ordering, counting and atomic increments
for the field types listed below. Models that use other fields stay on Redis
until their milestone. Popoto refuses them when you declare them, so they
never fail halfway through.

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
The connection, the version check, the DDL and the schema check all run on the
model's **first** query or save (`bind()`).

## Supported fields (M1)

| popoto field | Column | Index |
|---|---|---|
| `KeyField` / `UniqueKeyField` / `AutoKeyField` (`type=T`) | `T` (default `text`) | all key fields under one `UNIQUE`; a B-tree for each non-leading key field; `UniqueKeyField` also gets its own `UNIQUE` |
| `SortedField(type=T, partition_by=…)` / `SortedKeyField` | `T`: `bigint`, `double precision`, `numeric`, `date`, `time` or `timestamptz` | B-tree `(partition cols…, f, _pk COLLATE "C")` |
| `IntField` / `FloatField` / `DecimalField` / `BooleanField` | `bigint` / `double precision` / `numeric` / `boolean` | — |
| `StringField` | `text` | — |
| `DatetimeField` | `timestamptz` plus `<f>__utcoff integer` (offset in seconds; `NULL` = naive) | — |
| `Field(type=int/float/str/bool/Decimal/datetime)` | as above | — |

Every table also has:

- `_pk text PRIMARY KEY`, which holds the same `ClassName:key:…` string that
  Redis uses as the hash key. `Model.pk` and `redis_key` are therefore
  identical on both backends.
- `_created_at` and `_updated_at` (`timestamptz`), which the engine maintains.
  They are Postgres-only.

`DatetimeField` round-trips the way it does on Redis (#521). An aware value
comes back with the same UTC offset as a fixed-offset `timezone`. A naive
value comes back naive. A naive value is stored and compared as UTC, which is
the same instant `SortedField` scores it as on Redis.

Fields that arrive later: `IndexedField`, `UniqueField`, `TagField`,
`Relationship`, the collection fields, `BytesField`, `DateField`/`TimeField`,
and `Meta.indexes` (M1.1); the memory fields (M2–M4); `GeoField`, `Meta.ttl`
and the rest (M5). A model that uses one of them raises
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
statements. Inside a `transaction()` they propagate to the caller.

## Documented divergences

In each case below Postgres is stricter or more literal than Redis. Redis
behaviour does not change to match. Where a test pins one of these, it pins
both behaviours explicitly or carries a `redis_only(reason=...)` mark.

| Behaviour | Redis (unchanged) | Postgres |
|---|---|---|
| `save(migrate_key=True)` that changes a key field | rewrites the key and its index entries | raises `BackendCapabilityError` (v2). Create the new record and delete the old one. |
| Unit-of-work / bulk failure | `MULTI`/`EXEC` applies the other queued commands | the whole transaction rolls back |
| `\x00` in a text value | stored | `ValueError` naming the field |
| `atomic_increment` past `2**63` on an `IntField` | Lua goes to float | `NumericValueOutOfRange` |
| A Redis `pipeline=` handed to a Postgres model | queued | the write runs immediately, and the pipeline comes back untouched for the caller to execute |
| `~Q(field=…)` on an **unindexed** field | always empty: the leaf returns every key and its equality filter runs after the set algebra | the predicate is evaluated (`NOT`, with `NULL` counted as "not equal") |
| `KeyField` `__contains` | matches nothing (the lookup is accepted but not implemented) | `LIKE '%value%'` |
| Order of results with no `order_by`, no `Meta.order_by` and no sorted-field filter | set order (arbitrary) | `_pk` in bytewise (`COLLATE "C"`) order |
| An invalid `order_by=` / `values=` on a query that matches nothing | returns `[]` before validating | raises the same `QueryException` either way |
| `save(update_fields=…)` on a record that does not exist yet | writes a partial hash that stays out of the class set, so queries do not see it | inserts the row (unlisted columns `NULL`), so queries see it |
| `UniqueKeyField` conflict | checked by a read in `pre_save` before the write | enforced by a `UNIQUE` index inside the write; same `ModelException` text |
| `load_raw_hash`, `idle_seconds`, `Query.keys(catchall=/clean=)` | Redis debug and inspection APIs | raise `BackendCapabilityError` (`idle_seconds` arrives in M4) |
| `async_get`/`async_filter`/`async_count`/… | native `redis.asyncio` | run the sync call in a worker thread (the async driver arrives in M5) |

## Performance (M1 exit criteria)

`scripts/bench_backend_seam.py` measures the public API on both backends. It
seeds 2,000 records, runs `ANALYZE`, and then makes three runs of 300
iterations per operation, with the two backends interleaved within each run.
The M1 targets are `Model.save()` p50 at most 2x Redis and `filter` +
hydration p50 at most 1x Redis. The PR that introduced this page records the
measured numbers and the environment they were taken on.

```bash
REDIS_URL=redis://localhost:6379/14 \
POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \
    python scripts/bench_backend_seam.py
```
