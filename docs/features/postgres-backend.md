# Postgres Backend (v2)

Popoto v2 keeps one model API with two native storage backends behind it
([#759](https://github.com/tomcounsell/popoto/issues/759)). Redis keeps
today's hashes, index sets and Lua. Postgres stores each model in a typed
table with native indexes, and it is where new capabilities land.

This page covers the first Postgres milestones: **plain models** (M1),
**plain-field breadth** (M1.1), the **ranking and memory-state half of
Valor's slice** (M2a) and the **validity axis** (M3). That means records,
queries, `Q` objects, ordering, counting and atomic increments for the field
types listed below, including indexed, unique, tag, relationship and
collection fields, plus decay ranking, confidence (partitioned too), read
tracking, the write filter, `ObservationProtocol`, `composite_score`, and
`ValidityField` with `SupersessionProtocol`. Models that use other fields stay on Redis until their
milestone. Popoto refuses them when you declare them, so they never fail
halfway through.

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

## Supported fields (M1, M1.1, M2a, M3)

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
| `ValidityField` (M3) | `double precision` for the declared value, plus `<f>__valid_from`, `<f>__invalid_at`, `<f>__ingested_at` (`double precision`; `'Infinity'` = open) and `<f>__supersedes`, `<f>__superseded_by` (`text`); companion `<table>__<f>__open (digest, member)` | B-tree on `<f>__valid_from` and on `<f>__invalid_at`; the companion's `member` references `_pk` `ON DELETE CASCADE` |

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
not hold.

Fields that arrive later: the remaining memory fields (M2b–M4); `GeoField`, `Meta.ttl`
and the rest (M5); an `IndexedField` on a collection type is refused. A model
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
| `load_raw_hash`, `idle_seconds`, `Query.keys(catchall=/clean=)` | Redis debug and inspection APIs | raise `BackendCapabilityError` (`idle_seconds` arrives in M4) |
| `async_get`/`async_filter`/`async_count`/… | native `redis.asyncio` | run the sync call in a worker thread (the async driver arrives in M5) |
| A reload after `touch()` (M2a) | `touch` moves only the sorted-set score, so the hash, and a reload, keep the save-time value | the clock is the field's column, so a reload sees the touched time. Pinned: `test_a_reload_after_touch_is_a_documented_divergence` |
| `DecayingSortedField.rank_decayed(zset_key, …)` (M2a, TD-40) | ranks that sorted set | raises `BackendCapabilityError` naming `top_by_decay`, the backend-neutral call |
| `composite_score({"priority": …})` (M2a) | ranks by the WriteFilter priority set | raises `BackendCapabilityError`: the priority tier is a no-op on Postgres (plan §5 M2) |
| `composite_score(similarity_boost=/co_occurrence_boost=)` (M2a) | injects the boost as an arm | raises `BackendCapabilityError` until the vector arm (M2) and `CoOccurrenceField` (M4) arrive |
| Where a NaN decay score ranks (M2a) | NaN (`0 * inf`: a `-inf` clock with above-prior confidence) makes the script's comparator inconsistent (`x > nan` is always false), so `table.sort` places it arbitrarily and can misorder real scores around it | real scores sorted, NaN last; every member's score is the same on both. Pinned: `test_where_a_nan_score_ranks_is_a_documented_divergence` |
| A NaN decay score in `composite_score` (M2a) | `rank_decayed` replies `nan` (`0 * inf`: a `-inf` clock with above-prior confidence) and the composite's `ZADD` refuses it: `ResponseError: value is not a valid float` | that arm scores 0 for the record, the value `ZUNIONSTORE` gives a NaN product. Pinned: `test_a_nan_decay_score_in_composite_is_a_documented_divergence` |
| The confirmed access log (M2a) | a capped list of read timestamps (`$AT:…:access_log`) | not kept: `access_count` and `last_accessed` are. It is read only by `export_state`, which arrives with `transfer/` in M5 |
| `update_confidence(…, pipeline=uow)` with a Postgres `transaction()` (M2a) | (a Redis pipeline queues the update and returns `None`) | the update runs inside the transaction, so its value is returned and the attribute synced |
| A model with a `CyclicDecayField`, `CoOccurrenceField` or `PredictionLedgerMixin` (M2a) | supported | refused at declaration until M5, M4 and M5 respectively, so `ObservationProtocol`'s cycle, auto-discharge and ledger-resolution effects have no Postgres model to act on yet (its supersession effect runs from M3) |
| `execute_supersede(mode="open")` naming a member with no record (M3) | `ZADD NX` indexes the member anyway | writes nothing: the interval is the record's row. Only a direct `execute_supersede` call can ask for it. Pinned: `tests/postgres/test_postgres_validity.py::test_mode_open_on_a_member_with_no_record_writes_nothing` |
| An open-claim pointer naming a record that does not exist (M3) | storable (a manual `SET`, or a partial `import_state`); `supersede` reads it as "no incumbent" | unrepresentable: the pointer table's foreign key refuses it (`ForeignKeyViolation`), and deleting a record cascades to its pointers. Pinned: `test_a_pointer_cannot_name_a_record_that_does_not_exist` |
| `save_and_supersede` / `save_and_invalidate` whose close fails (M3) | `MULTI`/`EXEC` keeps the successor's save, and the typed error's text carries redis-py's `Command # N (...) of pipeline caused error:` prefix | the whole unit rolls back, so the successor is not saved either; same exception type, and the text is the bare reply line |
| A NaN `as_of` / `validity__as_of` (M3) | the range reads (`filter`, `resolve_*_keys`, the composite mask) raise `ResponseError: min or max is not a float`; the decay ranking's gate excludes nothing | `QueryException` with the same text (as row (v) of the query table); the decay ranking excludes nothing |
| `SupersessionProtocol.supersede`/`invalidate` with the backend's `transaction()` as `pipeline` (M3) | (a Redis pipeline queues the script; the closed key is unknown until `execute()`) | runs inside the transaction: the closed key is returned and a typed error raised at the call. `save_and_*` with a Redis pipeline is refused with `ValueError` |

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
A range also has no way to say "no `invalid_at` recorded" beside "`invalid_at`
is `+inf`", which differ at `as_of = +inf`. So the interval follows M2a's
clock decision: `double precision` epoch seconds, bit-identical to the score.

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
3. `SELECT … ORDER BY _pk COLLATE "C" FOR UPDATE` on the successor and the
   incumbent;
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

**Lock order.** Every supersede on a model and field takes the advisory lock
before any row lock, and `ObservationProtocol.on_context_used` takes it
before locking its batch, so two supersedes never interleave -- including
the crossing chains that deadlocked the #631 POC (`d1 → X` superseded by `Y`
while `d2 → Y` is superseded by `X`; `TestCrossingChains` forces the overlap
ten times). A save does not take it: it meets a supersede on the row lock,
and its upsert re-reads the row it waited on. The residual is a caller's own
`transaction()` that locks a row and then supersedes while another supersede
waits for that row: Postgres detects the cycle, and the caller gets
`BackendRetryableError` (pinned: `test_a_cross_operation_deadlock_is_a_retryable_error`).

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
