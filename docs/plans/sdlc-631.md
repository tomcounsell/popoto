---
status: Ready
type: feature
appetite: Large
owner: tomcounsell
created: 2026-10-02
tracking: https://github.com/tomcounsell/popoto/issues/631
---

# Storage backend seam and Postgres POC (#631)

## Problem

Every persistence operation in popoto is written directly against redis-py:
`get_REDIS_DB().hset(...)`, `run_lua(client, SCRIPT, numkeys, ...)`, and
`pipeline.zadd(...)`. Issue #631 asks whether the *semantics* of the agent-memory
fields (decay ranking, supersession, atomic index swaps) can run on Postgres
without changing the public API, by putting a `Backend` protocol behind the
field layer and shipping a second implementation as a proof of concept on a
dedicated branch.

Measured on `main` at `7b82c4b2` (2026-10-02):

- 25 `*_LUA` constants across `src/popoto` (the issue says 23; two landed since).
  Six are in the slice — `PURGE_ORPHAN_LUA`, `INDEX_SWAP_LUA`, `DECAY_SCORE_LUA`,
  `CAPPED_BAYESIAN_UPDATE_LUA`, `SUPERSEDE_LUA`, `TAG_SWAP_LUA` — plus the inline
  script in `Model.atomic_increment`.
- The eleven slice files hold ~16.6k lines; heaviest are `models/base.py` (61
  accessor/Lua sites) and `models/query.py` (49).
- `redis.client.Pipeline` is named 54 times across 19 files in `src/popoto`;
  17 are `isinstance(pipeline, redis.client.Pipeline)` branches inside the slice
  (6 `base.py`, 2 `sorted_field_mixin.py`, 2 `confidence_field.py`, 1 each in
  `indexed_field_mixin.py`, `tag_field.py`, `validity_field.py`,
  `supersession.py`). The issue does not mention this; see group A below.

The #630 prerequisite series (#644, #646, #647, #648, #653, #654, #656) is
merged. `poc/backend-seam` exists on origin at `7b82c4b2`.

## Approach

### Decisions already taken (recorded, not reopened)

1. WS0 is one serial PR.
2. WS1a (`models/base.py` CRUD, atomic increment, `PURGE_ORPHAN_LUA`) also runs
   serially after WS0, before 1b–1e fan out: every other family depends on
   record load/save.
3. WS2 adds a third `postgres` job to `tests.yml` using a `postgres:16` service,
   running only tests marked `conformance`. The Redis and Valkey jobs are untouched.
4. Postgres test isolation is one schema per run, dropped on teardown; running
   against schema `public` is refused (mirror of the DB-0 refusal).
5. The generic-tables schema and the float / `inf` validity sentinel from the
   issue stay as-is for the POC. The WS4 report must explicitly answer whether
   `tstzrange` replaces the sentinel in production.
6. WS0 adopts the `count` / `members` names from #634 for the sorted family.

### The WS0 protocol surface, enumerated from the code

Reading convention: *index* means an opaque string the field layer already
computes (`DB_key(...).redis_key`, e.g. `Memory:_score:acme`). The protocol never
parses it. On Redis it is the key verbatim; on Postgres it is a single `idx text`
column. This collapses the issue's `(model text, field text)` column pairs into
one column and is the one deliberate deviation from its schema sketch. *uow* is
the unit-of-work handle below; `None` means "execute now".

**A. Unit of work (2).** `Model.save()`, `Model.delete()`, every field
`on_save`/`on_delete`, `execute_supersede`, `update_confidence` and
`_save_and_close` all take a `pipeline=` kwarg and branch on
`isinstance(pipeline, redis.client.Pipeline)`. The seam needs an object that
plays that role on both backends.

| Method | Signature | Replaces |
|---|---|---|
| `begin` | `begin() -> UnitOfWork` (context manager; `commit()` returns the per-op results list) | `get_REDIS_DB().pipeline()` + `.execute()` |
| `native` | `native() -> Any` | the raw client, for out-of-scope mixin code only (see "escape hatch") |

On Redis, `UnitOfWork` *is* the `GuardedPipeline`, so an external caller passing
a real pipeline into `save(pipeline=...)` (as `tests/test_atomic_save.py` does)
keeps working unchanged. The 17 `isinstance` checks become `uow is not None`.

**B. Records (8) — `models/base.py`, `models/query.py`, owner 1a.**

| Method | Signature | Replaces |
|---|---|---|
| `save_record` | `(key, fields: dict[str, bytes], *, class_set, obsolete_key=None, ttl=None, expire_at=None, numeric: dict[str, float] \| None = None, uow=None)` | `HSET mapping` + `EXPIRE`/`EXPIREAT` + `SADD class_set` + the obsolete-key `DEL`/`SREM` on key migration (`Model.save`, 34 sites) |
| `load_record` | `(key) -> dict[str, bytes] \| None` | `HGETALL` (`Query.get`, `load_raw_hash`, `_hydrate`) |
| `load_records` | `(keys) -> list[dict \| None]` | pipelined `HGETALL` (`get_many_objects`, `async_get_many`, `top_by_decay`) |
| `load_fields` | `(key, names) -> list[bytes \| None]` | `HGET`/`HMGET` (`Model.load_fields`, `values=` path) |
| `record_exists` | `(key) -> bool` | `EXISTS` (`Model.exists`, `delete`, `update_confidence`, orphan checks) |
| `delete_record` | `(key, *, class_set, uow=None)` | `DEL` + `SREM class_set` (`Model.delete`) |
| `list_keys` | `(class_set) -> set[str]` | `SMEMBERS` (`Query.keys`, `Query.all`) |
| `count_records` | `(class_set) -> int` | `SCARD` (`Query.count`) |

`fields` are the per-field msgpack bytes `encode_popoto_model_obj` already
produces; the backend treats them as opaque. `numeric` is new: the decoded float
value of every `IntField`/`FloatField`/`DecimalField`, so Postgres can keep a
typed column that `decayed_rank` and `increment_field` can address in SQL. Redis
ignores it. See finding 2 below.

**C. Atomic increment (1) — owner 1a.** `increment_field(key, field, delta, *,
kind: Literal["int","float","decimal"], uow=None) -> int | float | Decimal`.
Replaces the inline Lua in `atomic_increment` (read, msgpack-decode, add,
re-encode, `HSET`). The `Decimal` tagged-dict envelope stays inside the Redis
backend. The companion `ZINCRBY` on the field's sorted index is `sorted_increment`
below, called by `base.py` after the increment.

**D. Side maps (4) — shared by 1a, 1d, 1e.** Three features keep a member ->
payload map: composite unique indexes (`Meta.indexes`, 4 sites in `base.py`),
confidence payloads (`ConfidenceField`), and supersession chain links
(`chain_fwd`/`chain_rev`). One family serves all three; on Postgres it is one
table `popoto_map(idx, member, value bytea)`.

| Method | Signature |
|---|---|
| `map_get` | `(idx, member) -> bytes \| None` |
| `map_set` | `(idx, member, value: bytes, *, only_if_absent=False, uow=None) -> bool` |
| `map_delete` | `(idx, member, *, uow=None) -> int` |
| `map_scan` | `(idx, pattern="*") -> dict[str, bytes]` |

**E. Set indexes (7) — `key_field_mixin.py`, `indexed_field_mixin.py`
(filter path), `tag_field.py` (filter path), owner 1b/1c.** `KeyFieldMixin`
is in the slice but absent from the issue's WS1 table; it is assigned to 1b.
`UniqueFieldMixin` performs **no storage operation at all** — its only Redis
contact is `kwargs.get` in `__init__`; uniqueness is the `is_unique` flag on
`swap_index` and the per-value sets `KeyFieldMixin` maintains.

| Method | Signature | Replaces |
|---|---|---|
| `index_add` | `(idx, member, *, uow=None)` | `SADD` |
| `index_remove` | `(idx, member, *, uow=None)` | `SREM` |
| `index_members` | `(idx) -> set[str]` | `SMEMBERS` |
| `index_union` | `(idxs) -> set[str]` | `SUNION` (`__in` filters) |
| `index_intersection` | `(idxs) -> set[str]` | `SINTER` (`TagField __all`) |
| `scan_index_names` | `(pattern) -> list[str]` | `scan_keys` on index prefixes (`__startswith`, `__endswith`, `__isnull=False`) |
| `scan_record_keys` | `(pattern) -> list[str]` | `scan_keys` + `TYPE` filter (`_scan_hash_keys`, `Query.keys(catchall=True)`, `check_indexes`) |

**F. Sorted indexes (7) — `sorted_field_mixin.py`, `base.py:touch`, owner 1b.**

| Method | Signature | Replaces |
|---|---|---|
| `sorted_add` | `(idx, member, score: float, *, uow=None)` | `ZADD` |
| `sorted_remove` | `(idx, member, *, uow=None)` | `ZREM` |
| `sorted_score` | `(idx, member) -> float \| None` | `ZSCORE` (`SortedFieldMixin.score`) |
| `sorted_count` | `(idx) -> int` | `ZCARD` (#634 `count`) |
| `sorted_members` | `(idx, start=0, stop=-1, *, reverse=False) -> list[str]` | `ZRANGE`/`ZREVRANGE` (#634 `members`) |
| `sorted_range` | `(idx, lo: float, hi: float, *, lo_inclusive=True, hi_inclusive=True, reverse=False, limit=None) -> list[str]` | `ZRANGEBYSCORE`/`ZREVRANGEBYSCORE` incl. the `(` exclusive-bound strings and `-inf`/`+inf` |
| `sorted_increment` | `(idx, member, delta: float, *, uow=None) -> float` | `ZINCRBY` |

The `"(5.0"` / `"-inf"` bound strings in `filter_query` are Redis wire format;
the protocol takes floats plus inclusivity flags (`float("inf")` is legal).

**G. Atomic swaps (4) — `indexed_field_mixin.py`, `tag_field.py`, owner 1c.**

| Method | Signature | Replaces |
|---|---|---|
| `swap_index` | `(record_key, field, new_idx, value: bytes, *, unique: bool, legacy_old_idx="", uow=None)` raises `ModelException` (today's type) | `INDEX_SWAP_LUA` (the `POPOTO_UNIQUE_CONFLICT` error reply) |
| `drop_index_entry` | `(record_key, field, *, fallback_idx, uow=None)` | `IndexedFieldMixin.on_delete`: `GET ptr` -> `SREM` -> `DEL ptr, old_ptr` |
| `swap_tags` | `(record_key, field, new_idxs: list[str], value: bytes, *, uow=None)` | `TAG_SWAP_LUA` |
| `drop_tag_entries` | `(record_key, field, *, fallback_idxs, uow=None)` | `TagFieldMixin.on_delete` |

The `$IdxPtr:` / `$TagPtr:` side keys, their pre-#540 variants and the pre-#476
in-hash pointer are Redis key-layout migration state; the Redis backend derives
all three from `(record_key, field)` and Postgres needs none, since the index
row *is* the pointer. The external-pipeline uniqueness pre-check stays in the
mixin as `index_members`.

**H. Decay and confidence (2) — `decaying_sorted_field.py`,
`confidence_field.py`, owner 1d.**

| Method | Signature | Replaces |
|---|---|---|
| `decayed_rank` | `(idx, *, now, decay_rate, limit, base_score_field="", confidence: tuple[str, float, float] \| None, validity: tuple[str, str, float] \| None, pretrim_max_ratio) -> list[tuple[str, float]]` | `DECAY_SCORE_LUA` (and the preceding `ZCARD` when `n is None`) |
| `confidence_update` | `(idx, member, signal, *, initial, cap, require_record: str \| None = None, uow=None) -> tuple[float, int, int, int] \| None` | `CAPPED_BAYESIAN_UPDATE_LUA` |

Leaks hidden here: the Lua returns a flat `[member, score, ...]` string list and
`as_of` travels as `repr(float)` so the range bound is bit-exact. The protocol
returns typed pairs and takes `as_of: float`; the Redis backend does the `repr`
and the flat-reply decode. `DecayingSortedField.rank_decayed` keeps its signature
but its documented "raw, undecoded" return becomes typed pairs; its three callers
(`query.top_by_decay`, `query.composite_score`, `recipes/context_assembler.py`)
are adjusted by 1d. `confidence_get`/`set`/`delete`/`scan` are `map_*`.

**I. Validity (5) — `validity_field.py`, `supersession.py`, owner 1e.**

| Method | Signature | Replaces |
|---|---|---|
| `supersede` | `(model_prefix, field, *, mode, new_member, old_member="", valid_from, ingested_at, close_at, assert_valid_from: bool, pointer_digest: str \| None, uow=None) -> str \| None` raises `ValidityMemberAbsentError` / `ValidityCloseBeforeStartError` / `ValidityValidFromConflictError` | `SUPERSEDE_LUA` + `_LUA_ERROR_MAP` |
| `interval_of` | `(valid_idx, invalid_idx, member) -> tuple[float \| None, float \| None]` | `ZSCORE` pair (`is_valid_at`, `get_valid_from`, `pre_save_validate`, `chain`, `_walk_links`) |
| `interval_members` | `(valid_idx, invalid_idx, as_of: float, *, select: Literal["valid","excluded"]) -> set[str]` | the two `ZRANGEBYSCORE` calls in `resolve_valid_keys` (intersection) and `resolve_excluded_keys` (union) |
| `drop_validity` | `(model_prefix, field, member, *, pointer_digest, uow=None)` | `ValidityField.on_delete`: 3x`ZREM` + 2x`HDEL` + `DEL ptr` |
| `open_pointer` | `(model_prefix, field, digest) -> str \| None` | `GET {prefix}:open:{digest}` (`find_open_pointers_for_member`) |

`_LUA_ERROR_MAP` moves into the Redis backend; the typed exceptions stay in
`validity_field.py` and the protocol raises them directly. `+inf` is passed and
returned as `float("inf")`, per decision 5. `filter_query(__current=...)`'s
`ZRANGE 0 -1` is `sorted_members`.

**J. Orphan purge and maintenance (2) — owner 1a.**

| Method | Signature | Replaces |
|---|---|---|
| `purge_orphan` | `(record_key, refs: list[tuple[str, Literal["sorted","set"]]]) -> int` | `PURGE_ORPHAN_LUA` (`'z'`/`'s'` kind flags become the literal) |
| `scan_index_members` | `(idx, kind) -> Iterator[str]` | `SSCAN`/`ZSCAN` in `check_indexes`, `clean_indexes`, `rebuild_indexes` |

**Count: 42 methods** (2 + 8 + 1 + 4 + 7 + 7 + 4 + 2 + 5 + 2). The issue
predicted 30–40. The overshoot is entirely the three groups it did not foresee:
the unit of work (A), the side-map family (D, which also absorbs composite
unique indexes), and the maintenance scans (E's two scans and J). Without those
the count is 33.

### The escape hatch, and what the grep criterion can honestly mean

`models/base.py` and `models/query.py` contain Redis code for fields the issue
puts *out of scope*: `_adjust_cycle_amplitudes` / `resolve_pressure`
(CyclicDecayField, `CYCLES_ADJUST_LUA`), `_tag_priority` / `_xadd_mutation`
(AccessTracker, EventStream), and the whole `composite_score` /
`_similarity_only_search` / `fuse` / `keyword_search` / `_materialize_*` /
`_apply_validity_mask` ranking path (~45 of `query.py`'s 49 sites, fusing
embeddings, BM25, access-tracker and co-occurrence arms with `ZUNIONSTORE` /
`ZRANGESTORE` / `ZDIFFSTORE` into 5-second temp keys), plus the admin paths
`migrate_to_partitioned`, both `export_state`/`import_state` pairs, and
`Query.keys(clean=True)`. They route through `get_backend().native()`: the Redis
client on Redis, `NotImplementedError("<feature> is Redis-only in the
backend-seam POC")` on Postgres. The issue's grep criterion then holds
**literally**, but every `native()` call is a known hole, so the criterion is
restated as "zero accessor/Lua sites outside `backends/`, plus an enumerated
`native()` ledger in each PR body and in WS4".

### Backend selection and the stale-client trap

`src/popoto/backends/__init__.py` holds a module global `_BACKEND: Backend |
None = None`, `get_backend()`, and `set_backend(b)`. Rules, in order of how
badly things break when violated:

1. **`RedisBackend` stores no client.** Every method body calls
   `get_REDIS_DB()` at call time (`run_lua(get_REDIS_DB(), ...)` for scripts);
   its `client` property is `return get_REDIS_DB()`, never an attribute set in
   `__init__`. This is CLAUDE.md's #655 rule one layer up: `set_REDIS_DB_settings()`
   *rebinds* `redis_db.POPOTO_REDIS_DB`, so a backend that captured the client
   would be a fresh copy of the bug #655 removed from 29 modules. With nothing
   stored there is nothing to invalidate on rebind, and the pytest plugin's
   `_swap_db()` (pool mutated in place) is equally invisible to it.
2. **`backends/__init__.py` never imports `POPOTO_REDIS_DB` by name**, only
   `get_REDIS_DB` and `run_lua`; `src/popoto/__init__.py` exports `get_backend`
   and `set_backend` as functions, never `_BACKEND`.
3. **Selection is lazy, cached on first `get_backend()` call**, not at import:
   read `POSTGRES_URL`, try `import psycopg`, bind `PostgresBackend(url)` if
   both hold, else `RedisBackend()`. Deliberately unlike `REDIS_URL` (read at
   import): `backends` is imported by `models/base.py`, and an import-time
   Postgres connection would make `import popoto` dial a second database. A
   downstream user with `REDIS_URL` and no `POSTGRES_URL` sees no change.
4. **`PostgresBackend` may hold a pool** (no rebind protocol to honour);
   `set_backend(None)` resets the cache and the WS2 fixture uses it per session.
5. **Guard tests.** `tests/test_redis_db_rebind_staleness.py` already proves #655
   with a `RecordingClient` spy; WS0 adds `backends.redis` as a probe, and
   `tests/test_backend_selection.py` asserts `"_BACKEND" not in vars(popoto)`,
   mirroring `test_popoto_redis_db_rebind.py`.

`run_lua` on a pipeline reads `POPOTO_REDIS_DB.script_load` inside `redis_db.py`
itself — the module's own global — and stays correct.

### Postgres test isolation and the CI job

The pytest plugin binds Redis in `pytest_configure` and the autouse
`_popoto_flush_db` fixture issues `FLUSHDB` before every test. Neither can be
switched off per backend without touching the plugin's contract, and
`tests/test_ci_workflow_redis_url.py::test_every_tests_workflow_job_sets_redis_url`
fails for any `tests.yml` job that drops `REDIS_URL`. Consequence: **the
`postgres` job runs both services** (`redis:7-alpine` and `postgres:16`), sets
`REDIS_URL: redis://localhost:6379/15` like its siblings, and adds
`POSTGRES_URL`. The conformance fixture then does per-run `CREATE SCHEMA
popoto_test_<uuid>` / `DROP SCHEMA ... CASCADE` and per-test `TRUNCATE` of the
backend's tables, mirroring flush. The schema name is checked against `public`
before any statement runs, mirroring `Db0FlushRefusedError`.

The `conformance` marker is registered in `[tool.pytest.ini_options].markers`.
The `backend` fixture is parameterised over installed backends and is
*opt-in per test file* via the marker; unmarked tests never see Postgres.

### Redis-specific assertions in the slice's tests

Counted on `main` with a coarse grep (`get_REDIS_DB()`/`get_redis()` command
calls, `run_lua`, `*_LUA`, `$IdxPtr`/`$TagPtr`/`$ValidityF`, `:open:`, msgpack,
raw `zscore`/`zrange`/`smembers`/`hgetall`/`hget`/`exists` calls). It
over-counts slightly (it also hits `Model.exists`), so read as upper bounds.

| File (tests / Redis-specific lines) |
|---|
| `test_validity_field.py` (130 / 147), `test_datetime_key_migration.py` (39 / 51), `test_lua_decay_scoring.py` (31 / 38, Lua-level throughout: Redis-only as a whole file) |
| `test_decaying_sorted_field.py` (46 / 35), `test_sorted_range_pushdown.py` (42 / 31), `test_partitioned_confidence.py` (31 / 29), `test_confidence_modulated_decay.py` (37 / 26), `test_confidence_field.py` (55 / 23) |
| `test_delete_all.py` (10 / 16), `test_atomic_save.py` (9 / 15, passes real `redis.client.Pipeline` objects), `test_query_get_no_track.py` (10 / 15), `test_key_fields.py` (23 / 14), `test_datetime_key_identity.py` (18 / 14), `test_sorted_datetime_score_purity.py` (7 / 12), `test_model_exists.py` (8 / 11), `test_decay_rank_seam.py` (12 / 10), `test_query_hydration_count.py` (11 / 8) |
| Under 8: `test_issue_534_indexed_field_encoders.py`, `test_model_partial_load.py`, `test_tag_field.py`, `test_get_or_create.py`, `test_datetime_tzinfo_round_trip.py`, `test_sorted_time_field.py`, `test_atomic_increment.py`, `test_sorted_field_ordering.py` |
| Zero: `test_indexed_fields.py` (26), `test_get_many.py` (12), `test_sorted_field_reads.py` (14), `test_sorted_field_score.py` (7), `test_model_equality.py` (23), `test_query_get_positional_string.py` (7) |

Roughly **34 files, ~830 tests, ~550 Redis-specific lines**. The zero-line files
are the first `conformance` candidates. The others are *split or skipped under
Postgres, not deleted*: WS2 adds a `redis_only` marker and a `backend_is_redis`
fixture, and each WS1 agent marks the assertions in its own family's files.

### Three findings the issue did not anticipate

1. **The unit of work is public API.** `pipeline=` is a documented kwarg on
   `save`, `delete`, `atomic_increment`, `update_confidence`,
   `execute_supersede`, `save_and_supersede` and every field hook, and the slice
   branches on its *type* 17 times. The protocol cannot be "one method per Lua
   script plus CRUD"; it needs a transaction handle the existing pipeline
   satisfies on Redis. On Postgres that handle is also where every script's
   "validation phase then mutation phase" ordering becomes a real rollback,
   not only 3c's.
2. **`data jsonb` does not fit what the field layer emits.** `save_record`
   receives per-field *msgpack bytes* (`encode_popoto_model_obj`), and three
   scripts decode msgpack *inside the store* (`DECAY_SCORE_LUA`'s base score,
   `CAPPED_BAYESIAN_UPDATE_LUA`'s payload, `atomic_increment`). `jsonb` cannot
   hold bytes, and a base64 string inside it cannot be an `ORDER BY` expression.
   POC resolution: the `numeric` side-map (typed `popoto_record.numeric jsonb`)
   plus opaque `bytea` payloads; the production answer is deferred to WS4.
3. **Hand-maintained tooling lists the new package trips.**
   `scripts/mypy_baseline.json` derives its ceiling from per-package counts and
   `backends/` has none, so *any* mypy error there breaks the ratchet — WS0
   ships it clean and allowlisted. `scripts/check_lock_imports.py` lists extras
   by hand, so WS2 adds `("psycopg", "postgres")`. And
   `test_every_tests_workflow_job_sets_redis_url` plus the plugin's autouse
   `FLUSHDB` force the `postgres` job to carry `REDIS_URL` and a Redis service.

## Tasks

All branches cut from `poc/backend-seam`; all PRs target `poc/backend-seam`,
never `main`. Each PR description names its workstream and owned file set.

### WS0 — protocol and Redis backend skeleton (`feature/backend-seam-ws0`, serial)

- [ ] `src/popoto/backends/__init__.py`: `Backend` as `typing.Protocol` with all
      42 methods, docstrings naming the Lua script or commands each replaces;
      `UnitOfWork` protocol; `get_backend()` / `set_backend()` per the rules above.
- [ ] `src/popoto/backends/redis.py`: `RedisBackend` with **no stored client**;
      every method body is existing code moved verbatim; every Lua constant
      moves here under its existing name and is re-exported from its old module
      (`test_validity_field.py`'s `SUPERSEDE_LUA` numkeys guard and
      `test_transfer_roundtrip.py`'s source scan must keep finding them).
- [ ] `src/popoto/backends/postgres.py`: stub, every method raises
      `NotImplementedError` naming itself; no psycopg import at module scope.
- [ ] `backends` added to `scripts/mypy_baseline.json` `clean`, measuring zero.
- [ ] `tests/test_redis_db_rebind_staleness.py` gains `backends.redis` as a
      probe; new `tests/test_backend_selection.py` (lazy selection, `_BACKEND`
      not in `vars(popoto)`, `REDIS_URL`-only environment selects Redis).
- [ ] Freeze: later protocol changes are their own tiny PRs
      (`feature/backend-seam-protocol-<n>`), merged first, never bundled.

### WS1a — records, increment, purge (`feature/backend-seam-ws1a`, serial after WS0)

- [ ] `models/base.py`: `save`, `delete`, `exists`, `load_fields`,
      `load_raw_hash`, `touch`, `atomic_increment`, `_purge_orphan_keys`,
      composite `Meta.indexes` (via `map_*`), `pre_save`'s unique check, and the
      maintenance scans through B/C/D/J.
- [ ] `models/query.py`: `get`, `get_many`, `get_many_objects`, `keys`, `all`,
      `count`, the async twins, through B.
- [ ] `_adjust_cycle_amplitudes`, `resolve_pressure`, `_tag_priority`,
      `_xadd_mutation`, `Query.keys(clean=True)` and the composite ranking path
      through `native()`; ledger of every `native()` site in the PR body.
- [ ] Mark Redis-only assertions in `test_atomic_save.py`, `test_delete_all.py`,
      `test_model_exists.py`, `test_query_get_no_track.py`,
      `test_datetime_key_*.py`, `test_atomic_increment.py`.

### WS1b — sorted, key, unique (`feature/backend-seam-ws1b`)

- [ ] `sorted_field_mixin.py` (`count`, `members`, `score`, `on_save`,
      `on_delete`, `filter_query`) through F; bound strings become floats + flags.
- [ ] `key_field_mixin.py` (`on_save`, `on_delete`, `filter_query`,
      `_scan_hash_keys`) through E; `unique_field_mixin.py` untouched (no sites).
- [ ] `query.py` sorted pushdown wiring unchanged in shape; assertions marked in
      `test_sorted_range_pushdown.py`, `test_sorted_datetime_score_purity.py`,
      `test_key_fields.py`.

### WS1c — index swap, tag swap (`feature/backend-seam-ws1c`)

- [ ] `indexed_field_mixin.py` through G and E; pointer-key derivation moves into
      `RedisBackend`, `POPOTO_UNIQUE_CONFLICT` mapping moves with it.
- [ ] `tag_field.py` through G and E.
- [ ] Assertions marked in `test_issue_534_indexed_field_encoders.py`,
      `test_tag_field.py`.

### WS1d — decay, confidence (`feature/backend-seam-ws1d`)

- [ ] `decaying_sorted_field.py` `rank_decayed` through H; typed-pair return;
      the three callers adjusted (including `recipes/context_assembler.py`).
- [ ] `confidence_field.py` through H and D; `migrate_to_partitioned`,
      `export_state`, `import_state` through `native()`.
- [ ] `test_lua_decay_scoring.py` marked Redis-only as a file; assertions marked
      in `test_decaying_sorted_field.py`, `test_confidence_*.py`,
      `test_partitioned_confidence.py`, `test_decay_rank_seam.py`.

### WS1e — validity, supersession (`feature/backend-seam-ws1e`)

- [ ] `validity_field.py` through I and F; `_LUA_ERROR_MAP` and `map_lua_error`
      move into `RedisBackend`, exceptions stay; `export_state`/`import_state`
      through `native()`.
- [ ] `supersession.py` `chain`, `_walk_links`, `_save_and_close` through I, D, A.
- [ ] Assertions marked in `test_validity_field.py` (the largest single job in
      WS1; budget it as such).

### WS2 — conformance harness (`feature/backend-seam-ws2`, after WS0, parallel with WS1)

- [ ] `pyproject.toml`: `postgres = ["psycopg[binary]>=3.1"]` extra; `uv lock`;
      `conformance` and `redis_only` markers registered.
- [ ] `scripts/check_lock_imports.py`: add `("psycopg", "postgres")`.
- [ ] `backend` fixture in `popoto.pytest_plugin`, opt-in via `conformance`
      marker; `backend_is_redis` fixture; schema-per-run + truncate-per-test;
      `public` refused before any statement.
- [ ] `tests.yml`: third job `postgres` with both services, `REDIS_URL` pinned to
      `/15`, `POSTGRES_URL`, `pytest -m conformance`. Redis/Valkey jobs untouched;
      `tests/test_ci_workflow_redis_url.py` still green.
- [ ] `tests/conformance/test_harness.py`: fixture isolation and `public`
      refusal proven on both backends.

### WS3 — Postgres backend (`feature/backend-seam-ws3{a..e}`, after WS2 and the matching WS1)

- [ ] 3a records/increment/purge: `INSERT ... ON CONFLICT DO UPDATE`; `numeric`
      column; increment as `SELECT ... FOR UPDATE` + decode/encode in Python
      inside the transaction (the `Decimal` envelope is opaque bytes).
- [ ] 3b sorted/set/map: `SELECT ... WHERE score BETWEEN ... ORDER BY LIMIT`;
      `inf` passes through `double precision`.
- [ ] 3c swaps: one transaction, `SELECT ... FOR UPDATE` on the record row,
      `ModelException` raised before any write.
- [ ] 3d decay: one `SELECT`, power-law in `ORDER BY`, confidence via `LEFT JOIN
      popoto_map`, gate as `WHERE NOT (invalid_at <= as_of OR valid_from >
      as_of)` with absence meaning included; parity with `DECAY_SCORE_LUA`'s
      exclusion rule asserted by the conformance tests.
- [ ] 3e supersede: PL/pgSQL `RAISE EXCEPTION` carrying the same
      `POPOTO_VALIDITY_*` tokens so one mapper serves both backends.
- [ ] Each 3x PR flips its family's test files to `conformance` and turns the
      `postgres` job green for them.

### WS4 — report (`feature/backend-seam-ws4`)

- [ ] `docs/plans/postgres_backend_poc.md`: passed/stubbed matrix; protocol
      diff (42 predicted vs. actual); full `native()` ledger; decay and
      supersede latency on both backends, environment stated per CLAUDE.md;
      explicit answers to `tstzrange` vs. sentinel, `idx text` vs. `(model,
      field)`, and whether `numeric` survives into production.

## Success Criteria

Per-PR gates into `poc/backend-seam` (CI runs on PRs to any branch; neither
`tests.yml` nor `lint.yml` filters on target branch):

- [ ] `pytest -m "not slow"` green on the Redis **and** Valkey jobs; any local
      run states `POPOTO_TEST_DB`, redis-py and Python versions.
- [ ] `black --check src/ tests/` and `ruff check src/` clean.
- [ ] `scripts/mypy_ratchet.py --strict-env` not above the ceiling; `backends`
      allowlisted at zero from WS0 onward.
- [ ] `grep -rn "POPOTO_REDIS_DB\|run_lua" src/popoto/fields src/popoto/models`
      returns nothing for the PR's owned files; `native()` sites in the PR body.
- [ ] `test_redis_db_rebind_staleness.py` and `test_backend_selection.py` green.

POC-level:

- [ ] Every test in the `conformance`-marked files passes on both backends from
      the same test code; Redis-only assertions marked, none deleted.
- [ ] `REDIS_URL` set and no `POSTGRES_URL`: zero change in imports, behaviour
      or key layout (`test_import_surfaces_are_unchanged` extended with
      `get_backend`).
- [ ] WS4 report published with environment-stated numbers and the three
      explicit schema answers.

## Non-goals

- Nothing merges to `main`; a successful POC re-proposes WS0+WS1 against `main`
  as a no-behaviour-change minor release.
- No Postgres path for the issue's out-of-scope list (`GeoField`,
  `EmbeddingField`, `BM25Field`, `ExistenceFilter`/`FrequencySketch`,
  `CoOccurrenceField`, `CyclicDecayField`, `PredictionLedgerMixin`,
  `AccessTrackerMixin`, `EventStreamMixin`, `DataFrameField`, pub/sub, async,
  MCP, recipes) nor for `migrations.py` or `transfer/`.
- No per-model typed schema, no `tstzrange`, no decoding of msgpack at the
  boundary beyond the `numeric` side-map, no `py.typed`, no rename of the
  public `pipeline=` kwarg.
- No Postgres TTL: `ttl`/`expire_at` are recorded in a column, not enforced.

## Questions for the architect

1. **Duck-typed `pipeline=`.** The POC keeps the kwarg name and, on Redis, the
   `GuardedPipeline` object, but changes the field-layer checks from
   `isinstance(pipeline, redis.client.Pipeline)` to `uow is not None`. A caller
   passing a pipeline from a *different* redis-py client still works on Redis.
   Is that acceptable for the POC, or must `UnitOfWork` be a wrapper class so
   the type check survives?
2. **`numeric` side-map vs. decoding at the boundary.** The side-map keeps
   `save_record`'s payload opaque and makes 3d a single SQL statement, at the
   cost of writing every numeric field twice on Postgres. The alternative — the
   backend receiving decoded Python values and owning encoding — is the
   production shape but widens WS0 into the encoding layer. Confirm the
   side-map for the POC.
3. **`native()` as a sanctioned hatch.** It makes the literal grep criterion
   true while leaving ~50 sites Redis-bound by construction. Is the restated
   criterion ("zero accessor/Lua sites outside `backends/` plus an enumerated
   ledger") the one the POC is judged against?
4. **`rank_decayed`'s return type.** Changing it from the raw flat reply to typed
   pairs touches `recipes/context_assembler.py`, which is otherwise out of
   scope. Accept that one recipe edit, or have the Redis backend keep a
   `decayed_rank_raw` twin for the POC's duration?

## Architect decisions (2026-10-02)

Answers to the four questions above, taken by the maintainer so WS0 can start.
They bind the POC only; WS4 may reopen any of them for the production shape.

1. **Duck-typed `pipeline=` — accepted for the POC.** The field layer checks
   `uow is not None`; on Redis the `UnitOfWork` *is* the `GuardedPipeline`
   object and the kwarg name does not change. A wrapper class is deferred to
   production. WS0 must keep `tests/test_atomic_save.py` passing unchanged.
2. **`numeric` side-map — confirmed for the POC.** `save_record`'s payload stays
   opaque msgpack bytes; the backend receives the decoded numeric values it
   needs for ordering as a side-map. WS4 reports whether the double write is
   acceptable or whether the backend should own encoding.
3. **`native()` is sanctioned, with a ledger.** The POC is judged against the
   restated criterion: zero accessor/Lua sites outside `backends/` in the
   slice's files, plus an enumerated `native()` ledger in each WS1 PR body and
   in the WS4 report. Every `native()` call site carries a one-line comment
   naming the out-of-scope feature it serves.
4. **`rank_decayed` keeps the raw reply in WS0.** WS0 is a verbatim move, so
   the Redis backend returns exactly what `DECAY_SCORE_LUA` returns today.
   WS1d may change it to typed pairs and is allowed the one edit to
   `recipes/context_assembler.py` that follows, stated in its PR body.
