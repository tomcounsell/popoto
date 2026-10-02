# Postgres Backend (proof of concept)

The storage backend seam ([#631](https://github.com/tomcounsell/popoto/issues/631))
puts a `Backend` protocol behind the agent-memory field layer and ships a
second implementation, `popoto.backends.postgres.PostgresBackend`, on the
`poc/backend-seam` branch. It is **not** a supported way to run popoto: nothing
here reaches `main` or PyPI until the POC's report (WS4) says it should, and a
user with `REDIS_URL` set and no `POSTGRES_URL` sees no change at all.

Selection is lazy: the first `popoto.get_backend()` call reads `POSTGRES_URL`
and, when it is set and `psycopg` is importable (`pip install
'popoto[postgres]'`), binds the Postgres backend; otherwise the Redis backend.
Importing popoto never dials Postgres.

## What is implemented (WS3a)

The record family, the atomic increment, and the orphan purge, all behind the
[conformance harness](../testing.md#backend-conformance-tests-opt-in) with
`RedisBackend` as the oracle:

| Protocol methods | Postgres shape |
|---|---|
| `save_record`, `load_record`, `load_records`, `load_fields`, `record_exists`, `delete_record`, `list_keys`, `count_records` | `popoto_record(key, field bytea, value bytea)`, one row per field, so the msgpack bytes the field layer emits are stored and returned untouched and a save merges fields the way `HSET` does. The class set lives in `popoto_set(idx, member)`. |
| `save_record(numeric=...)` | `popoto_numeric(key, field, value double precision)`, the typed side-map the decay query will `ORDER BY`. |
| `increment_field` | Per-key advisory lock, `SELECT ... FOR UPDATE`, decode / add / re-encode in Python, upsert; reproduces the Redis Lua's `%.14g` return and cmsgpack's integer / float32 / float64 packing, including the `Decimal` envelope and the absent-record case. |
| `purge_orphan` | One CTE statement: an `EXISTS` gate over `popoto_record` and conditional deletes from `popoto_sorted` / `popoto_set`. |
| `begin()` | `PostgresUnitOfWork`: a queue of operations run inside one transaction on `commit()`; leaving the `with` block without committing discards it. |

## What is implemented (WS3b)

The side maps, the set and sorted indexes, and the two remaining maintenance
methods, under the same harness and oracle:

| Protocol methods | Postgres shape |
|---|---|
| `map_get`, `map_set`, `map_delete`, `map_scan` | `popoto_map(idx, member, value bytea)`, one table for composite unique indexes, confidence payloads and supersession chain links. `map_set` is an upsert whose `RETURNING (xmax = 0)` is `HSET`'s new-entry reply; `only_if_absent` is `ON CONFLICT DO NOTHING` (`HSETNX`). `map_scan` pushes the glob down as a regex; its `count` is `HSCAN`'s batch hint and is ignored, there being no cursor. |
| `index_add`, `index_remove`, `index_members`, `index_union`, `index_intersection` | `popoto_set(idx, member)`. `SADD`/`SREM` replies are the statement's row count; union is `idx = ANY(...)`, intersection is `GROUP BY member HAVING count(DISTINCT idx) = n`, so a missing index empties it as `SINTER` does. |
| `scan_index_names`, `scan_record_keys` | The Redis glob translated to an anchored POSIX regex and applied with `~`: over the three index tables' `idx` for the first, over `popoto_record.key` for the second (the table *is* Redis's `TYPE` filter). |
| `sorted_add`, `sorted_remove`, `sorted_score`, `sorted_count`, `sorted_increment` | `popoto_sorted(idx, member, score double precision)` with an `(idx, score)` index. `ZADD`'s 1-for-new / 0-for-update reply is `RETURNING (xmax = 0)`; `ZINCRBY` is one `ON CONFLICT DO UPDATE SET score = score + delta`, which locks the row it updates. |
| `sorted_members`, `sorted_range` | `ORDER BY score, member COLLATE "C"` (see below); `ZRANGE`'s index arithmetic over a `row_number()` window in one statement; bounds compared as `double precision` with the operator chosen by the inclusivity flags. |
| `scan_index_members`, `drop_index` | A lazy `SELECT member` generator over the table `kind` names; `DELETE ... WHERE idx = %s`, replying 1 when any row went, else 0, as `DEL` does. |

Every one of these is a single statement, so none takes an advisory lock; the
per-key lock above is only for the record read-modify-write paths.

```sql
CREATE TABLE IF NOT EXISTS popoto_map (
    idx    text  NOT NULL,
    member text  NOT NULL,
    value  bytea NOT NULL,
    PRIMARY KEY (idx, member)
);
```

**Tie order.** Redis orders a sorted set by score and breaks ties by member,
comparing the member *bytes*, in both directions. Postgres's default collation
is locale-aware and does not agree (`B` sorts between `a` and `b` in
`en_US`), so every ordered read says `member COLLATE "C"` -- byte order for
UTF-8 text -- and the reverse reads flip both sort keys. The conformance
suite's property test seeds few distinct scores over twelve members chosen
for byte-order traps (`a`, `A`, `aa`, `a:1`, `a-1`, `_`, `0`, `é`) so ties are
the common case, and compares the Postgres leg against both a reference
model and the live `RedisBackend`.

**Bound rendering.** The Redis backend renders `sorted_range` bounds in wire
format -- `2.0`, `(2.0` for an exclusive bound, `-inf` / `+inf` -- and Redis
parses them back to doubles. Postgres has no rendering step: the bounds travel
as `double precision` parameters (`±Infinity` is a native value), and
`lo_inclusive` / `hi_inclusive` pick `>=` / `>` and `<=` / `<`. A `NaN` score
raises `ValueError` with Redis's wording (`value is not a valid float` on
`sorted_add`, `resulting score is not a number (NaN)` on an increment of
`inf` by `-inf`) where Redis replies with an error; the increment's
transaction rolls the row back.

**Non-positive `limit`** means unbounded in both backends, because the Redis
backend only passes `LIMIT` for a positive `int`.

Tables are created with `CREATE TABLE IF NOT EXISTS` on the first connection a
backend instance opens, in whatever schema the URL's `search_path` names. The
block runs in one transaction under
`pg_advisory_xact_lock(631, hashtext(current_schema()))`, because `IF NOT
EXISTS` is not race-safe: two sessions that both find a table absent both try
to create it and the loser fails on the catalog's unique index
(`UniqueViolation: pg_type_typname_nsp_index`). Measured on this tree before
the lock, 8 of 12 concurrent first connections against an empty schema failed
that way; with it, none, and `tests/conformance/test_postgres_bootstrap.py`
holds the line. The two-argument lock form is a separate key space from the
one-argument `hashtext(key)` record locks, so no record key can collide with
it. A bootstrap that fails closes its connection before the error propagates.

## Cross-instance serialisation

Redis runs every command and script on one thread, so an `increment_field`
and a `save_record` on the same key can never interleave. Postgres gives each
backend instance its own connection, and a transaction on its own only
serialises rows that already exist (`FOR UPDATE` cannot lock a row that an
upsert is about to create). Every operation that writes a record key --
`save_record`, `delete_record`, `increment_field` and `purge_orphan` --
therefore takes `pg_advisory_xact_lock(hashtext(key))` as its first statement,
held until the transaction ends; on the `uow=` path that is the unit of work's
transaction at `commit()`. A rename (`save_record(obsolete_key=...)`) locks
both keys in one global order (ascending lock id) so two instances renaming
in opposite directions cannot deadlock on each other. Without this, an
increment on an absent field could read "no row", lose to another instance
committing `n = 100`, and store `0 + 1`: a value no serial order produces.

## Known deviations from the Redis oracle

**`2**63` packs differently per platform.** cmsgpack decides whether a Lua
number "fits `int64`" with a C `(int64_t)d` cast, which is undefined
behaviour at exactly `9223372036854775808.0`. On aarch64 the cast saturates,
so an arm64 Redis stores the msgpack `int64` max (`cf 7fffffffffffffff`); an
x86-64 Redis, and the Postgres backend on every platform, store the lossless
float32 (`ca 5f000000`). The value `increment_field` *returns* is identical
either way; only a later decode of the stored bytes differs (`int`
`2**63 - 1` against `float` `2**63`). The Postgres backend documents this
rather than emulating one platform's undefined behaviour.

**Globs match bytes on Redis and characters on Postgres.** `SCAN MATCH` and
`HSCAN MATCH` run Redis's `stringmatchlen` over the key's bytes, so `?` and a
`[...]` class consume one *byte*; the Postgres backend translates the glob to
a regular expression over `text`, where they consume one *character*. The two
disagree only on a non-ASCII name under a single-character glob (`_tag:?`
does not match `_tag:é` on Redis -- two bytes -- and does on Postgres). `*`,
the only glob popoto's own callers use (`prefix*`, `prefix:*`), agrees
everywhere. `tests/conformance/test_indexes.py::TestScanDeviations` pins the
difference rather than hiding it.

**`scan_index_names` sees only indexes.** Redis's `SCAN` returns every key of
every type that matches, so a *record* whose key happens to match an index
glob is returned too (and `rebuild_indexes` would `DEL` it). The Postgres
backend enumerates the three index tables only.

**A finite sum that overflows `double` raises.** `sorted_increment` of
`1e308` by `1e308` stores `inf` on Redis; Postgres's `float8` addition raises
`value out of range: overflow` for a finite-operand overflow, and the backend
does not emulate `inf`. `inf` plus a finite delta is `inf` on both.

**Not implemented**: record TTL (`save_record(ttl=...)` / `expire_at=...`
raises `NotImplementedError` rather than silently storing a record that never
expires), `records_exist` (family B, added in protocol-1 and still a stub),
and the atomic index/tag swaps, decay ranking, confidence, validity and
supersession families, which still raise `NotImplementedError` naming the
method. `native()` raises on Postgres by design: it is the Redis-only escape
hatch for out-of-scope features.
