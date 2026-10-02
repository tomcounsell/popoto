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
| `increment_field` | Advisory lock, `SELECT ... FOR UPDATE`, decode / add / re-encode in Python, upsert; reproduces the Redis Lua's `%.14g` return and cmsgpack's integer / float32 / float64 packing, including the `Decimal` envelope and the absent-record case. |
| `purge_orphan` | One CTE statement: an `EXISTS` gate over `popoto_record` and conditional deletes from `popoto_sorted` / `popoto_set`. |
| `begin()` | `PostgresUnitOfWork`: a queue of operations run inside one transaction on `commit()`; leaving the `with` block without committing discards it. |

Tables are created with `CREATE TABLE IF NOT EXISTS` on the first connection a
backend instance opens, in whatever schema the URL's `search_path` names.

**Not implemented in WS3a**: record TTL (`save_record(ttl=...)` /
`expire_at=...` raises `NotImplementedError` rather than silently storing a
record that never expires) and every other protocol family -- side maps, set
and sorted indexes, atomic index/tag swaps, decay ranking, confidence,
validity and supersession -- which still raise `NotImplementedError` naming
the method. `native()` raises on Postgres by design: it is the Redis-only
escape hatch for out-of-scope features.
