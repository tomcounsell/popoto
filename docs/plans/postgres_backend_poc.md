---
status: Draft
type: report
appetite: Large
owner: tomcounsell
created: 2026-10-03
tracking: https://github.com/tomcounsell/popoto/issues/631
---

# Postgres backend POC report (#631, WS4)

The #631 proof of concept asked whether popoto's agent-memory semantics --
decay ranking, supersession, atomic index swaps, confidence updates -- can run
on Postgres behind a `Backend` protocol without changing the public API. This
report answers the five things the issue's WS4 section asks for (what passed,
what was stubbed, the protocol diff, a latency comparison, the schema verdict)
and adds the selection footgun, a consolidated tech-debt register, and the
merge recommendation. It is measured at the head of PR #752 (WS3d), the last
family, so every number includes family H.

**Short answer.** The seam works: every protocol method is implemented on
Postgres, 644 Postgres-leg conformance ids pass from the same test code as
770 Redis-leg ids, and the Redis path is wire-identical to `main` on every
family (proven by command recording in six reviews, with two real divergences
fixed rather than ratified). The generic schema does *not* survive into
production as-is: it is the right compatibility layer and the wrong storage
layout, and the one piece of it the architect pre-approved -- the `numeric`
side-map -- is fed by nobody and should be dropped.

## Environment (binding on every number below)

| Item | Value |
|---|---|
| Head measured | `4111db58` (`feature/backend-seam-ws3d`, #752 head = `poc/backend-seam` @ `d538c5a1` + WS3d) |
| Python / redis-py / psycopg / pytest / mypy / msgpack | 3.12.14 / 8.1.0 / 3.3.6 / 9.1.1 / 2.3.1 (pinned) / 1.2.3 |
| Redis | 8.10.2, Darwin 27.0.0 arm64, local, `POPOTO_TEST_DB=10` for every pytest run |
| Postgres | PostgreSQL 18.6 (Homebrew) aarch64-apple-darwin27.0.0, `server_encoding=UTF8`, `postgresql://localhost:5432/postgres` |
| Machine | Apple M1 Max, 64 GB, macOS 27.0.1 (`macOS-27.0.1-arm64-arm-64bit`) |
| Install | fresh `uv venv --python 3.12`, `.[dev,embeddings,benchmark,mcp,postgres,docs]`, editable install resolving to this worktree (`popoto.__file__` checked) |

One foreign `popoto_test_12543ddc…` schema pre-existed on the server (also
seen by the #751 reviewer) and was left alone; the harness schemas this
session created were all dropped (count unchanged at 1 before and after).

## 1. What passed

### 1.1 The two-leg conformance run

`POSTGRES_URL=postgresql://localhost:5432/postgres POPOTO_CONFORMANCE_BACKENDS=redis,postgres POPOTO_TEST_DB=10 pytest -m conformance -q -rs -p no:cacheprovider` (the CI job's command, `slow` included):

| Leg | Passed | Skipped | Failed | XFAIL |
|---|---:|---:|---:|---:|
| Redis | 770 | 16 | 0 | 0 |
| Postgres | 644 | 142 | 0 | 0 |
| leg-less (packer / bootstrap-list pins) | 2 | 2 (collection-time pandas/hermes) | 0 | 0 |
| **Total** | **1416** | **160** | **0** | **0** |

46.5 s. Per-leg counts come from a `-v` run of the same command, attributing
each id to the leg named in its parameter list (`[redis]`, `[redis-case]`,
`[case-postgres]` and so on); the 2 leg-less skips are module-level
`importorskip`s. This is the method the #751 reviewer asked for after the
per-leg split differed between the PR body and the review (TD-33 below): the
totals here are not comparable to #751's because #752 added `test_decay.py`
(150 tests × 2 legs) since.

Every Postgres-leg skip is a named `redis_only` reason or a harness
"Postgres-leg assertion on the Redis leg" skip; none is a failure disguised
as a skip. **Zero Postgres-backend bugs were found by the slice files** --
#751's reading of every mark (19 sampled by its reviewer with the mark
stripped) stands, and #752 adds the family-H methods the marks were waiting
for.

### 1.2 The Redis suite, downstream shape

`POPOTO_TEST_DB=10 pytest -m "not slow" -q --tb=short -p no:cacheprovider`
with no `POSTGRES_URL` and no `POPOTO_CONFORMANCE_BACKENDS`: **4900 passed,
45 skipped, 32 deselected, 0 failed** in 192 s. (#752's body reports 4899 + 1
environmental failure on DB 12; on DB 10 that test passes.)

### 1.3 Every slice test file, and why its Postgres leg skips what it skips

`redis_only` marks are counted from the file at head; the reason categories
are the mark's own `reason=` text (an unstated mark is classified from the
PR that added it).

| File | `conformance` | Redis leg | Postgres leg | `redis_only` marks by reason |
|---|---|---|---|---|
| `tests/conformance/test_records.py` (WS3a) | yes | 48 / 3 skip | 51 / 0 | -- (3 Redis-leg skips are Postgres-only probes) |
| `tests/conformance/test_indexes.py` (WS3b) | yes | 87 / 0 | 87 / 0 | -- (18 leg-less `stringmatchlen` pins) |
| `tests/conformance/test_swaps.py` (WS3c) | yes | 54 / 3 | 55 / 2 | -- (deterministic interleavings are per-leg) |
| `tests/conformance/test_validity.py` (WS3e) | yes | 48 / 2 | 50 / 0 | -- |
| `tests/conformance/test_decay.py` (WS3d) | yes | 147 / 2 | 149 / 0 | -- |
| `tests/conformance/test_harness.py`, `test_postgres_bootstrap.py` (WS2) | yes | 3 / 6 | 8 / 1 | -- |
| `tests/test_indexed_fields.py` | yes (#751) | 26 / 0 | 26 / 0 | none |
| `tests/test_retrieval_quality_regression.py` | yes (#752) | 11 / 0 | 11 / 0 | none |
| `tests/test_issue_534_indexed_field_encoders.py` | yes (#751) | 13 / 0 | 8 / 5 | raw Redis read 2 (hash bytes via `hget`; 5 ids) |
| `tests/test_key_fields.py` | yes (#751) | 24 / 0 | 15 / 9 | raw Redis read 9 (`SMEMBERS` on `$KeyF:`/`$Class:`, #746) |
| `tests/test_atomic_save.py` | yes (#751) | 9 / 0 | 4 / 5 | raw Redis read 3; real `Pipeline` 1; async 1 |
| `tests/test_tag_field.py` | yes (#751) | 33 / 0 | 22 / 11 | raw Redis read/key types 2; **family H 1 class (8 ids)** |
| `tests/test_decaying_sorted_field.py` | yes (#751) | 46 / 0 | 25 / 21 | **family H 16** (2 classes + 11 per-test) |
| `tests/test_confidence_field.py` | yes (#751) | 60 / 0 | 35 / 25 | **family H 12** (2 classes + 10 per-test) |
| `tests/test_validity_field.py` | yes (#751) | 139 / 0 | 76 / 63 | raw Redis read/plant 23; **family H 10 + plant-and-H 4**; spy 4; real `Pipeline` 3; TTL 1; non-goal (cyclic 2, transfer 1) |
| `tests/test_concurrent_index_integrity.py` | no -- module `redis_only` (#751) | 33 | n/a | Redis-level throughout (raw `$IdxPtr:` reads, forced mid-EVAL errors, multiprocess children) |
| `tests/test_lua_decay_scoring.py`, `test_sorted_range_pushdown.py`, `test_sorted_datetime_score_purity.py`, `test_issue_476_*`, `test_issue_540_*` | no -- module `redis_only` | run as before | n/a | Lua-level / raw ZSET reads / pointer side keys; the marks are inert without `conformance` |
| `tests/test_sorted_field_reads.py`, `test_sorted_field_score.py` | no | run as before | n/a | 1 each: client spy / `POPOTO_REDIS_DB` rebind |
| `tests/test_sortedfield.py`, `test_queries.py`, `test_chainable_queries.py`, `test_expression_queries.py` | no | run as before | n/a | module-scope scripts with zero `def test`; nothing to parametrise (#746) |
| `tests/test_*_routes_through_backend.py` (6 files) | no | run as before | n/a | by construction Redis-bound: each installs a `RecordingBackend(RedisBackend())`; a Postgres leg would record against Redis (#748, #752). `test_validity_routes_through_backend.py` carries 7 inert marks (#745 TD3) |

**Family H marks are now strippable.** 43 marks across four files (16 + 12 +
1 class + 14) carry the text `family H: decayed_rank / confidence_update are
WS3d stubs on Postgres`. #752 implements both methods, and its own trial of
the two files found exactly five tests that still fail on Postgres for other
reasons (three seed the companion payload through `POPOTO_REDIS_DB.hset`,
one backdates ZSET scores through the raw client, and
`test_concurrent_updates_within_window_match_oracle` shares one
`PostgresBackend` connection across ten threads and hangs the leg). Stripping
the marks is a grep plus those five re-marks under their true reasons; it is
listed under WS2/WS3 work in §8.

## 2. What was stubbed or not implemented on Postgres

Every one of the 46 protocol methods is implemented at this head; `_todo` is
gone from `backends/postgres.py`. What remains is the escape hatch, the
documented refusals, and the plan's non-goals.

### 2.1 The `native()` ledger -- 17 sites, consolidated from every WS1 PR body

Each site carries a `# native(): <feature>` comment (asserted by the shape
tests in each routing file). `grep -rn "native()" src/popoto | grep -v
backends/` at head finds exactly these 17 call sites (plus two prose
mentions in a module comment and a docstring).

| # | File:line | Function | Feature served (out of the slice) | Redis commands |
|---|---|---|---|---|
| 1 | `models/base.py:2163` | `idle_seconds` | `recipes/memory_lifecycle.py` idleness (object-header read) | `OBJECT IDLETIME` |
| 2 | `models/base.py:2633` | `resolve_pressure` | `CyclicDecayField` pressure hash | `HSET` |
| 3 | `models/base.py:2746` | `_adjust_cycle_amplitudes` | `CyclicDecayField` (`CYCLES_ADJUST_LUA`; the one `run_lua(` left outside `backends/`) | `EVALSHA`/`EVAL` |
| 4 | `models/base.py:3599` | `rebuild_indexes` step 1 | `GeoField` index drop | `DEL` |
| 5 | `models/base.py:4362` | `raw_update` | migrations' hook-free `HSET` bypass (expressible as `save_record(class_set=None)` since protocol-2; left ledgered) | pipelined `HSET` |
| 6 | `models/query.py:789` | `composite_score` | ranking path: temp ZSETs, `ZUNIONSTORE`, top-K | `ZADD`, `EXPIRE`, `ZUNIONSTORE`, `ZREVRANGE*` |
| 7 | `models/query.py:1038` | `_similarity_only_search` | `semantic_search` without indexes | `ZADD`, `EXPIRE`, `ZREVRANGE`, `DEL` |
| 8 | `models/query.py:1589` | `_materialize_decay_field` | decay scores into a temp ZSET (the `rank_decayed` call itself is routed) | `ZADD`, `EXPIRE` |
| 9 | `models/query.py:1645` | `_materialize_confidence_field` | confidence hash into a temp ZSET | `HGETALL`, `ZADD`, `EXPIRE` |
| 10 | `models/query.py:1701` | `_materialize_access_tracker` | `AccessTrackerMixin` meta reads | `SMEMBERS`, `HGET`, `ZADD`, `EXPIRE` |
| 11 | `models/query.py:1809` | `_apply_validity_mask` | composite validity mask (#580) | `ZRANGESTORE`, `ZUNIONSTORE`, `ZDIFFSTORE`, `EXPIRE` |
| 12 | `models/query.py:1850` | `_cleanup_temp_keys` | temp-key cleanup for 6-11 | `DEL` |
| 13 | `models/query.py:2397` | `Query.keys(clean=True)` | deprecated `KEYS` orphan sweep (`Model.clean_indexes()` is the routed replacement) | `SMEMBERS`, `HGETALL`, `KEYS`, `SREM` |
| 14 | `models/query.py:2433` | `Query.keys(catchall=True)` | debug glob over every key type incl. `$SortF:` ZSETs (`tests/test_timeseries.py` counts them) | `KEYS` |
| 15 | `fields/validity_field.py:371` | `find_open_pointers_for_member` | transfer export (`{prefix}:open:*` scan) | `SCAN`, `GET` |
| 16 | `fields/validity_field.py:445` | `export_state` | transfer export | `ZSCORE` ×3, `HGET` ×2 |
| 17 | `fields/validity_field.py:534` | `import_state` | transfer import (unguarded `ZADD`/`HSET`/`SET`) | `ZADD`, `HSET`, `SET` |

WS1c (`indexed_field_mixin.py`, `tag_field.py`), WS1d
(`decaying_sorted_field.py`, `confidence_field.py`) and WS1b's three mixins
hold **zero** sites; `supersession.py` holds zero. On Postgres every site
raises `NotImplementedError("<feature> is Redis-only in the backend-seam
POC")` at the call, which means a model declaring any out-of-scope field
fails at *first use*, not at declaration (TD-9).

### 2.2 Not implemented, by design, with the mapping the issue names

| Item | State at head | Where it bites | Known Postgres mapping |
|---|---|---|---|
| Record TTL (`Meta.ttl`, `save_record(ttl=/expire_at=)`, `set_expiry`) | `NotImplementedError("record TTL …: Postgres has no key expiry")`; a record is never silently stored as "forever" (#735 B2, #737) | any `Meta.ttl` model; `tests/test_validity_field.py::test_ttl_model_warns_once` | not named in the issue; candidates in §9 Q3 (expiry column + read filter + reaper, or `pg_cron`) |
| `popoto.batch()` | Redis-bound (`batch.py:40`, #630 "not a seam"); on Postgres fails closed at the first queued op with `TypeError … not GuardedPipeline`, nothing written (#750 TD1) | every `with popoto.batch() as pipe:` caller | `get_backend().begin()` -- the seam already exists |
| Async client (`async_get`, `async_filter`, `get_async_redis_db()`) | untouched; the shape test exempts `async def` bodies (#746) | `tests/test_atomic_save.py` async test, MCP server | `psycopg.AsyncConnection` behind an async twin of the protocol |
| Pub/sub | untouched (`pubsub/publisher.py` also holds a `pipeline if pipeline` site) | `pubsub/` | not named in the issue; `LISTEN`/`NOTIFY` is the natural candidate (this report's suggestion, not the issue's) |
| `GeoField` | `native()` site 4; field hooks untouched | `rebuild_indexes` on a geo model | PostGIS |
| `EmbeddingField` | out of scope; `_similarity_only_search` ledgered | `semantic_search` | pgvector |
| `BM25Field` | out of scope; hydration routed (defensive `as_key_strs`) | `keyword_search`, `fuse` | `pg_search` / `pg_trgm` |
| `ExistenceFilter` / `FrequencySketch` | out of scope | `batch_might_exist` | `bloom` |
| `CoOccurrenceField` | out of scope (`composite_score` arm) | ranking | `WITH RECURSIVE` |
| `CyclicDecayField` | `native()` sites 2-3; its `rank_decayed` override keeps `CYCLIC_DECAY_LUA` | any cyclic model | not named; an `ORDER BY` expression like 3d's |
| `PredictionLedgerMixin`, `AccessTrackerMixin`, `EventStreamMixin` | out of scope; `native()` site 10; `_tag_priority` / `_xadd_mutation` untouched | mixins | not named (plain tables; `XADD` has no equivalent, a sequence does) |
| `DataFrameField` | out of scope | -- | not named |
| MCP server, all recipes, `migrations.py`, `transfer/` | out of scope; sites 5, 15-17 | -- | -- |
| Out-of-slice `pipeline if pipeline else` / `if pipeline:` truthiness sites (WS1f's ledger names nine modules; a grep at head counts ~50 sites across `embedding_field` 10, `relationship` 8, `content_field` 5, `co_occurrence_field` 5, `existence_filter` 4, `event_stream` 4, `bm25_field` 4, `geo_field` 3, `access_tracker` 3, `write_filter` 2, `shortcuts` 1, `pubsub/publisher` 1, `recipes/provenance_journal` 1) | untouched; a `PostgresUnitOfWork` is falsy while empty, so each returns `None` into the hook chain | every out-of-scope field on a Postgres unit of work | one-line `is not None` each (#751's nine in-slice sites are the pattern) |
| `popoto_numeric` side-map | table exists, written by `increment_field` only; **no `save_record` call in `models/base.py` passes `numeric=`** (#752) | nothing reads it | drop (§5) |

## 3. Protocol surface diff

The plan predicted 42 methods in ten groups (A 2, B 8, C 1, D 4, E 7, F 7,
G 4, H 2, I 5, J 2). WS0 shipped exactly 42. Three freeze-rule PRs added
four and widened two, for **46** at head (`tests/test_backend_selection.py`
pins the count; `vars(Backend)` at head enumerates 46 public callables).

### 3.1 Additions

| PR | Method | Group | Why WS1 needed it (from the protocol PR body) |
|---|---|---|---|
| protocol-1 (#734) | `records_exist(keys) -> list[bool]` | B (8 → 9) | `check_indexes`/`clean_indexes` batch 1000 `EXISTS` per pipeline; a `record_exists` loop is 1000 round trips on a production maintenance command. Stubbed on Postgres by WS3a, implemented in #743's patch (`SELECT DISTINCT key … ANY(%s)`) |
| protocol-1 (#734) | `drop_index(idx, kind, *, uow=None)` | J (2 → 3) | `rebuild_indexes` step 1 `DEL`s whole indexes; no WS0 method removed one wholesale. `kind` names the Postgres table; Redis ignores it. Also consumed by WS1d's `migrate_to_partitioned` |
| protocol-2 (#740) | `save_record(class_set: str \| None = None)` | B (widened) | #735 review **B1**: the pre-seam partial save only `SADD`ed on key migration; WS0's unconditional `SADD` surfaced a never-fully-saved hash through `query.count()`/`all()` -- a key-layout change |
| protocol-2 (#740) | `set_expiry(key, *, ttl, expire_at, uow)` | B (9 → 10) | #735 review **B2**: the partial path must queue `EXPIRE` *after* the field hooks so it lands on the hash the `INDEX_SWAP` EVAL creates; inside `save_record` it ran against a missing key and `Meta.ttl` became "forever" |
| protocol-2 (#740) | `map_scan(idx, pattern="*", count=100)` | D (widened) | #735 TD1: step-5 side-map scans issued `HSCAN … COUNT 1000`; the backend default was 100 (what `ConfidenceField`'s loop uses) |
| protocol-3 (#741) | `load_fields_many(keys, names)` | B (10 → 11) | the `values=` projection issues one pipelined `HMGET` per key; a `load_fields` loop would be a round trip per key *and* change the command (`load_fields` issues `HGET` for one name). `tests/test_query_hydration_count.py` counts `Pipeline.hmget` |

No WS1 family other than 1a and 1b needed a protocol change: 1c, 1d and 1e
each found WS0's groups sufficient. The plan's "protocol drift" risk cost
three tiny PRs, each merged within the hour, which is the coordination cost
the issue priced in.

### 3.2 The 13 WS0 deviations and how WS1 consumed each

| # | Deviation (#732) | Consumed by |
|---|---|---|
| 1 | Hash field names are `bytes` on the wire (`fields: Mapping[Any, bytes]`, `load_record -> dict[Any, bytes]`) | WS1a/1b: not decoded at the boundary; replies go straight to `decode_popoto_model_hashmap`; `_classify_class_set_orphans` encodes the auto-key name. WS3a stores `field bytea` for the same reason (`{name}\x00idxset` legacy pointer round-trips) |
| 2 | `supersede` gains `now: float` | WS1e passes `clock`; backend renders `repr(float(now))`, byte-identical to `repr(clock)`. WS3e fills every defaulted instant from it and never reads `clock_timestamp()` |
| 3 | `drop_validity` drops `pointer_digest`; backend scans `{prefix}:open:*` itself | WS1e: `on_delete` is one call. WS3e: `DELETE … WHERE prefix = %s AND member = %s` on `popoto_open_ptr`'s index |
| 4 | `purge_orphan` gains `uow=None` | WS1a keeps one unit of work for all orphans and its `try/except` warning around `commit()` |
| 5 | Every `uow=` method returns `None` when queued | WS1a (four chained-return sites), 1b, 1c, 1d, 1e all hand the caller's `pipeline` object back; WS1f then fixed the truthiness test that this exposed on Postgres (`PostgresUnitOfWork.__len__`) |
| 6 | `swap_index`'s `ModelException` cannot name `Model.field` and the raw value | WS1c re-wraps through `IndexedFieldMixin._unique_conflict_message`; user text byte-identical. WS3c raises the backend's text identically on both backends. Cost: a three-deep chain on the internal path (TD-29) |
| 7 | `confidence_update` direct path returns `None` for an absent `require_record` | WS1d raises `TypeError("update_confidence() requires a saved model instance")` on the `None`; wire unchanged (`EXISTS` then `EVAL`). #751 TD5: the backend is reached before the rejection (TD-27) |
| 8 | `decayed_rank` renders the confidence triple with `str()` | WS1d made it a no-op: the literal strings `("", "0", "0.5")` cross through a local `Any`; EVAL argv byte-identical (#742 review confirmed on the wire). WS3d accepts the typed triple |
| 9 | `delete_record` returns `bool(DEL)` when executed now | WS1a keeps `record_exists` *before* the hooks (#476's ordering) and ignores the reply; pinned by `test_delete_checks_existence_first_then_delete_record` |
| 10 | Validity `model_prefix` is the opaque `$ValidityF:<Model>`; backend appends `:{field}:{suffix}` | WS1e: `ValidityField._model_prefix` via `get_special_use_field_db_key`; `_validity_keys(prefix, "validity") == get_all_keys(...)` asserted. WS3e imports `_validity_keys` from `backends/redis.py` rather than copying it |
| 11 | Set/sorted/scan returns decode to `str`; `filter_query` intersected raw `bytes` | WS1b/1c/1e re-encode at each field boundary (`_members_as_bytes`, now defined twice -- TD-20); the Query layer stays `bytes`. WS1f (#751) added `as_key_str`/`as_key_strs` at the nine call sites where `bytes` *reached* a backend, after #748/#750 measured 88-112 `text = bytea` failures |
| 12 | `sorted_range` renders bounds as `f"{float}"`, `±inf` as `"+inf"`/`"-inf"` | WS1b passes the numeric value `convert_to_numeric` produced plus flags; an int stays an int so the wire is `f"{numeric_value}"` as before. Residual: a user-supplied `float("inf")` bound renders `+inf` where base rendered `inf` (#746 TD1; same parse) |
| 13 | Two files outside `backends/` changed (`GuardedPipeline.commit`, `check_supersede_lua_phases.py` `SOURCE`) | `commit()` is what WS1e's `_save_and_close` and WS1b's `_fire_on_read` call; the phase checker's repoint is what #747 found vacuous |

Architect decision 4 (`rank_decayed` keeps the raw flat reply) was taken as
option (a) by WS1d with zero recipe edits, and WS3d therefore renders every
Postgres score through Lua's `%.14g` solely to match the string shape. That
is a parity tax with no consumer that wants strings; §9 Q5 reopens it.

## 4. Latency comparison

Measured with `scripts/bench_backend_seam.py` (committed; `ruff` and `black`
clean), same process, same machine, both servers local and warm, N = 2000
records each with a decay score, a confidence payload and an open validity
interval, 200 timed iterations per operation after 20 warm-up calls (the
`supersede` mode run takes no warm-up so each pair is fresh). Protocol-level
rows call `RedisBackend` / `PostgresBackend` directly; the model-level rows
bind each backend with `set_backend` and drive `BenchMemory` (KeyField +
FloatField + `DecayingSortedField(base_score_field=)` + `ConfidenceField` +
`ValidityField`). Microseconds; the ratio is Postgres p50 over Redis p50.
Environment as in the table at the top; `REDIS_URL=redis://localhost:6379/10`;
head `4111db58`; the script's own schema `popoto_test_<hex>` created and
dropped.

| Operation | Redis p50 | p95 | p99 | Postgres p50 | p95 | p99 | PG / Redis |
|---|---:|---:|---:|---:|---:|---:|---:|
| `save_record` (insert, 2 fields + class set) | 87 | 127 | 170 | 429 | 641 | 713 | 4.9x |
| `load_record` | 69 | 114 | 140 | 63 | 94 | 112 | 0.9x |
| `decayed_rank` plain (N=2000, limit 50) | 2929 | 3195 | 3322 | 512 | 587 | 663 | **0.2x** |
| `decayed_rank` + `base_score_field` | 4366 | 4876 | 5001 | 14816 | 15759 | 16104 | 3.4x |
| `decayed_rank` + confidence modulation | 5843 | 6159 | 6361 | 31696 | 33307 | 36335 | 5.4x |
| `decayed_rank` + validity gate | 2904 | 3153 | 3277 | 966 | 1497 | 2114 | **0.3x** |
| `decayed_rank` + base + confidence + gate | 7491 | 8739 | 12577 | 46529 | 48537 | 49580 | **6.2x** |
| `supersede` (open, with pointer) | 126 | 217 | 265 | 288 | 492 | 614 | 2.3x |
| `supersede` (supersede mode, explicit incumbent) | 86 | 122 | 137 | 314 | 500 | 579 | 3.7x |
| `swap_index` (new value, non-unique) | 73 | 99 | 123 | 512 | 655 | 819 | **7.0x** |
| `sorted_range` (30-day window, reverse, limit 50) | 99 | 130 | 149 | 80 | 92 | 103 | 0.8x |
| `Model.save()` (key + decay + confidence + validity) | 503 | 1542 | 2484 | 1487 | 1975 | 2209 | 3.0x |
| `list(filter(relevance__gte=cutoff, limit=50))` + hydrate | 1788 | 2871 | 5726 | 1586 | 1795 | 2069 | 0.9x |
| `top_by_decay("relevance", n=50)` through `Query` | 11243 | 13734 | 18133 | 61957 | 68357 | 75060 | 5.5x |

Mean of the p50 ratios: 3.2x. A first run before the `filter()` fix (it
returned a lazy `QueryBuilder` in 1 µs) gave the same picture within noise
(3.4x mean; `swap_index` 9.9x, `decayed_rank` all-arms 6.3x), so the second
run is reported.

**Read this as a POC number on a laptop with the generic schema and no
tuning**: one autocommit connection per backend instance, no pool, no
`ANALYZE`, every write a transaction with an advisory lock, every Postgres
row round-tripping through psycopg's adaptation layer, and a Redis server on
the same CPU. It says where the generic schema is slow
and by roughly how much; it does not say what a tuned deployment would do.

Where Postgres is slowest, and why:

1. **`decayed_rank` with a base score and/or confidence (3.4x-6.2x, 15-47
   ms).** The plain and gated rankings are *faster* on Postgres (0.2x-0.3x):
   the Lua interprets 2000 members, formats 2000 scores with `tostring` and
   sorts in Lua, where the SQL sorts 2000 doubles in C off the `(idx, score)`
   index. The moment the ranking has to read a msgpack payload per member the
   per-row cost jumps to ~7 µs (`popoto_base_score`) and ~15 µs
   (`popoto_confidence`, a map walk) -- a PL/pgSQL interpreter stepping through
   bytes. That is the cost of plan finding 2 (payloads are msgpack *inside*
   the store) paid on every query rather than once on write. A typed column
   (`importance double precision`, `confidence double precision`) would make
   these rows the 0.2x case.
2. **`swap_index` (7.0x, 0.5 ms).** One transaction, an advisory lock, three
   reads (pointer, legacy in-hash field, idempotency) and four writes, each a
   separate round trip through psycopg, where the Lua does the same seven
   steps in one server-side script. Batching the statements into one PL/pgSQL
   function (as `popoto_supersede` already is) would collapse it to ~2x.
3. **`save_record` (4.9x) and `Model.save()` (3.0x).** The record write is
   `pg_advisory_xact_lock` + `unnest` upsert + class-set insert in a
   transaction; the model-level save adds `sorted_add`, `map_set`, the
   `popoto_supersede` call and `commit()`, each its own statement. Per-field
   rows (§5) also mean a 10-field record is 10 upserts' worth of index
   maintenance.

What this comparison does not include: concurrency (every number is one
client), cold cache, large records, Redis pipelining of unrelated commands,
network, or the `n=None` full-scan `ZCARD` path.

## 5. Schema verdict

**Does the generic schema survive into production? No -- not as the storage
layout. Yes -- as the compatibility layer for fields the typed layout does not
know.** Argued item by item:

| Question | Evidence from the POC | Verdict |
|---|---|---|
| `popoto_record(key, field bytea, value bytea)` vs typed per-model tables | It was forced by finding 2 (`jsonb` cannot hold msgpack bytes) and it works: `HSET` merge semantics for free, byte-identical round trips incl. `\x00` field names, one statement per hot path. But it makes every typed read a PL/pgSQL decode (§4 rows 4-7, 3-6x), one row per field per record, and `ORDER BY` on a user value impossible without the decoder. The field layer already knows every field's type (`_meta.fields`); the backend is the only layer that does not | **Production: per-model typed tables, backend-owned encoding** (the plan's question 2 alternative). The generic table stays for fields without a typed mapping. This widens the protocol into the encoding layer -- `save_record` would take decoded values -- which is the WS0 scope the architect declined for the POC and should accept for production |
| `popoto_numeric` side-map | Pre-approved (decision 2) so 3d could `ORDER BY` a typed column. WS3d found **nobody feeds it**: every `save_record` in `models/base.py` omits `numeric=`; only `increment_field` writes it; `decayed_rank` reads the record bytes instead and would have ranked every member at base `1.0` had it trusted the map. Its invariant ("kept current by every writer") has no owner | **Drop it.** A side-map that must be maintained by every writer in parallel with the bytes is the double write the architect asked WS4 to judge; the answer is that it is a stale-read hazard, not an optimisation. The typed-table design above makes it unnecessary; until then the PL/pgSQL decoder is the honest shape |
| Validity intervals as `popoto_sorted` rows under the `_validity_keys` names vs `tstzrange` | 3e kept them in `popoto_sorted` because the *callers* read them through the generic families (`filter(validity__current=False)` is `sorted_members` on two index names; `chain` is `map_get`) and because `interval_of`/`interval_members` receive index *names*, which the reading convention forbids parsing. A `tstzrange` table would be invisible to those three call sites. `'infinity'::float8` behaves exactly as `math.huge` in every comparison (17-row exclusion table, `as_of = ±inf`, `1e308`) | **POC: keep. Production: `tstzrange` (or `valid_from timestamptz, invalid_at timestamptz NULL`) on the per-model table, with a GiST `@>` index for `as_of` queries** -- but only once the protocol passes `(model, field)` for the validity group instead of three pre-rendered names, and once `filter(validity__…)` reads through `interval_members` rather than `sorted_members`. The float sentinel can stay at the *protocol* boundary (`float("inf")` is what Python callers see either way); it should not survive as the *storage* representation |
| The `+inf` float sentinel | Native in `double precision` and in `timestamptz` (`'infinity'`), so no sentinel translation is needed in either design | Keep at the boundary; `timestamptz 'infinity'` or `NULL` upper bound in storage |
| `idx text` vs `(model text, field text)` | The one deliberate deviation from the issue's sketch held: not one backend method parses an index name, `_validity_keys` is imported rather than re-derived, and the WS3 families needed no model/field knowledge. Its cost is that a typed per-model schema cannot be addressed from an opaque string | **Keep `idx text` for the generic tables and for protocol v1.** A typed schema needs the *protocol* to carry the model and field names it already has in `DB_key` -- a v2 signature change, not a parse |
| `popoto_pointer` needs a family column (#748 TD1) | `(key, field)` conflates `$IdxPtr:` and `$TagPtr:`; `swap_tags(k,"f",…)` then `swap_index(k,"f",…)` reads the lexically-first tag row as the old index. Unreachable through a `Model` (one attribute, one type) | Add `family text` to the key (or prefix `field`) and drop the `LIMIT 1`; cheap, do it before the schema is anyone's data |
| `COLLATE "C"` and server encoding (#743 TD5) | Tie order parity (0 divergences over 3 seeds × 200 ops incl. non-ASCII) holds *because* the server is `UTF8`, and nothing asserts it | `SHOW server_encoding` in the bootstrap, refuse anything but `UTF8`; in the typed design consider `bytea` members, which compare bytewise natively |
| NUL bytes in `text` (#737 TD3, #748 TD3) | `key`, `idx`, `member` are `text`; Redis accepts `\x00` anywhere, Postgres `text` raises `DataError`. Keys are user-derived (`KeyField` values) | Refuse NUL at the field layer (`KeyField` / index value validation) so both backends agree, rather than widening every column to `bytea`; document as a limit |
| Per-connection `CREATE OR REPLACE FUNCTION` bootstrap (#750 TD4) | `SCHEMA_DDL` runs on every first connection under an advisory lock; the ten `CREATE OR REPLACE FUNCTION`s (`popoto_supersede` plus the nine msgpack/decoder helpers) re-install on each, so a hand-patched function silently reverts and every new connection pays the DDL | Operator-owned DDL: an `install`/migration entry point that writes a schema-version row, and a bootstrap that only *checks* the version. Required before any pooled or multi-process deployment |

Three findings the plan recorded, closed: finding 1 (the unit of work is
public API) held and the duck-typed `pipeline=` survived every family with
one one-line consequence per out-of-slice hook (TD-10); finding 2 (`jsonb`
does not fit) is the whole of the first row above; finding 3 (hand-maintained
tooling trips) cost `backends` in the ratchet allowlist, `("psycopg",
"postgres")` in `check_lock_imports.py`, and the `postgres` CI job carrying a
Redis service -- all three landed in WS0/WS2 without incident.

## 6. Selection and the pytest plugin

`get_backend()` selects lazily from the environment: `POSTGRES_URL` set and
`psycopg` importable binds `PostgresBackend`. #738/#739 found the footgun
the moment WS1a routed `models/base.py`: the `pytest (Postgres)` job exports
`POSTGRES_URL` for the conformance legs, so every module-scope `Model.save()`
in the tree hit the WS0 stub at *collection* -- 14 errors in a job that had
deselected those tests. The fix is a design rule, now in `docs/testing.md`:
**the test-process default backend is Redis.** An opted-in session
(`popoto_test_db` / `POPOTO_TEST_DB`) pins `RedisBackend` in
`pytest_configure` regardless of `POSTGRES_URL`; the Postgres leg is
fixture-scoped and restores the *previous* binding (never resets the cache);
a session that never opted in is not pinned (`test_session_that_never_opted_in_is_not_pinned`).

That rule is right for tests and does not fix production. An environment
variable set for an unrelated reason -- a shared `.env`, a sibling service,
the harness itself -- silently redirects every model operation, and the
failure surfaces far from the cause (#577's shape). It gets *worse* when the
backend is real rather than a stub: the process quietly writes to Postgres.

**Recommendation (#739 option a):** selection by an explicit popoto-specific
variable, `POPOTO_BACKEND=redis|postgres`, defaulting to `redis`;
`POSTGRES_URL` only says *where* the Postgres backend connects.
`POPOTO_BACKEND=postgres` without `psycopg` or without `POSTGRES_URL` is a
hard error at selection time naming the variable, never a
`NotImplementedError` at first use. The pytest pin stays as the second line
of defence. This is a one-file change in `backends/__init__.py` plus
`tests/test_backend_selection.py`, and it should land *before* `backends/`
reaches `main` (§8), because the lazy `POSTGRES_URL` rule is the one part of
WS0 that is observable to a downstream user who has that variable set for
another program.

## 7. Consolidated tech-debt register

Severity: **P** = blocks production (`popoto[postgres]` GA); **M** = fix
before WS0+WS1 are re-proposed against `main`; **N** = note (documented
deviation, cosmetic, or test-side). Items a later PR already closed are
listed only where the closure matters to a reader of the review threads.

| # | Item | Ref | Sev | Remedy |
|---|---|---|---|---|
| 1 | `scripts/check_supersede_lua_phases.py` partitions on the *first* `-- MUTATION PHASE`, which is a quoting comment (`redis.py:834`; real marker `:893`): a `ZADD` in the real validation phase prints `OK`. Pre-existing on `main` | #745 TD1, #750 TD3, **#747** (open) | M | partition on the last marker or a column-0 marker; add the negative test from #747 |
| 2 | Cross-*operation* deadlock is detected, not prevented: a uow that queued `save_record(K)` ahead of a `supersede`, against a supersede holding the prefix and naming `K`, raises raw `DeadlockDetected` on one side (not a `ValidityError`); `ObservationProtocol`'s `(TypeError, ValueError)` degrade does not catch it | #737 TD4, #750 B1 fix + new TD1 | P | the unit of work acquires its locks up front in the global order (prefix before keys) at `commit()`; map `DeadlockDetected` to a typed retryable error |
| 3 | `PostgresBackend` is one autocommit connection per instance, no pool, not thread-safe: ten threads sharing the field layer's one instance left a transaction `idle in transaction` and hung the harness's `TRUNCATE` | #737 body, #752 integration proof | P | a pool (psycopg `ConnectionPool`) with a connection per transaction; or document "one instance per thread" and have `get_backend()` hand out thread-local instances |
| 4 | Record TTL unimplemented; `Meta.ttl` models refuse on Postgres | #737, #740 | P | §9 Q3 |
| 5 | `popoto.batch()` is Redis-bound; fails closed on Postgres | #750 TD1 | P | route through `get_backend().begin()` |
| 6 | Async client untouched; async twins read Redis regardless of backend | #746 | P | async protocol twin on `psycopg.AsyncConnection` |
| 7 | `POSTGRES_URL` auto-selection binds Postgres process-wide for any process that has the variable | #738, #739 | P (M for the rule) | `POPOTO_BACKEND` (§6) |
| 8 | Per-connection DDL bootstrap with `CREATE OR REPLACE FUNCTION` ×10 | #737 review, #750 TD4 | P | operator-owned install + version check (§5) |
| 9 | A model declaring any out-of-scope field fails at first `native()` use, not at declaration | WS1 ledgers | P | validate `_meta.fields` against the bound backend's capabilities at selection or first use, naming the field |
| 10 | Out-of-slice `pipeline if pipeline else` / `if pipeline:` truthiness sites (~50 across 12 field modules, `pubsub/publisher.py`, `recipes/provenance_journal.py`; §2.2) return `None` into the hook chain on a falsy empty `PostgresUnitOfWork` | #751 ledger | P | `is not None` sweep, one line each, with the `TestUnitOfWorkIsTestedForPresence` proxy pattern |
| 11 | `popoto_numeric` side-map fed by `increment_field` alone; `save_record(numeric=)` has no caller | #752 | P | drop the table and the kwarg (§5) |
| 12 | NUL byte in `key`/`idx`/`member` (`text`) raises `DataError` where Redis accepts it | #737 TD3, #748 TD3 | P | refuse at the field layer (§5) |
| 13 | `COLLATE "C"` tie order assumes `server_encoding = UTF8`; nothing checks | #743 TD5 | P | `SHOW server_encoding` at bootstrap |
| 14 | `popoto_pointer(key, field, idx)` conflates the index and tag pointer spaces | #748 TD1 | P | `family` column |
| 15 | uow failure semantics differ: Postgres rolls back the whole queue, a Redis pipeline commits the rest; NaN raises at queue time on Postgres, at execute on Redis | #743 TD2 (documented) | P (decision) | §9 Q2 |
| 16 | Family-H `redis_only` marks (43) are stale since #752; five of the tests they cover need re-marking under their true reason (raw `hset`/`zadd` plants ×4, shared-connection threads ×1) | #751, #752 | M | strip by grep, re-mark five |
| 17 | `as_key_str` falls through to `str()`, so a `DB_key` or any object passed as `redis_key` to `get`/`get_many`/`exists`/`load_raw_hash`/`load_fields` renders instead of raising redis-py's `DataError` (error → works, unobservable in-tree) | #751 TD2 | M | accept `str \| bytes` only |
| 18 | `rank_decayed(validity=(k1, k2, ""))` raises `ValueError` before any command where base issued the EVAL with `ARGV[7]=""` and returned the ungated ranking | #742 TD1 | M | map the `ValueError` to `None` (the script's own "unparseable = disabled" rule) or disclose |
| 19 | `map_lua_error` and the three `Validity*Error` classes live in `validity_field.py`, so both backends import them function-locally (module scope is an import cycle through the `*_LUA` re-exports); the "WS1e moves the map" comment at `redis.py` is stale | #732 TD2, #745 TD4 | M | move the classes and the map to `popoto/exceptions.py`, re-export from `validity_field` |
| 20 | `_members_as_bytes` defined twice by the same name (`indexed_field_mixin.py:98`, `key_field_mixin.py:81`) | #744 TD5, #746 TD4 | M | one helper in `backends/` or `fields/` |
| 21 | Query layer keeps `bytes` members (`Query.keys()`, `filter_for_keys_set`, `_members_as_bytes`, `SortedFieldMixin.filter_query`, `ValidityField._members_valid_at`) with WS1f decoding at each backend call; the flip to `str` is one later PR across four mixins and `Query.keys` | #744, #746, #748 TD5 | M (for `popoto[postgres]`), N for `main` | the flip, after the re-proposal (it changes `Query.keys()`'s public return type) |
| 22 | `_save_and_close` reads `pipe.command_stack` for `close_index`; `PostgresUnitOfWork` grew a `command_stack` property under redis-py's name to satisfy it; `_validate_caller_pipeline` is duck-typed on it. `close_index` is a per-leg queue position by contract (4 on Redis, 3 on Postgres) | #745 body, #750 e | N | a `UnitOfWork.position()` protocol method |
| 23 | Residual Redis wire changes to disclose in the release notes: one extra `ZSCORE invalid_at` per single-score read and per chain hop (#745); `HGETALL` → `HSCAN COUNT 1000` and report ordering in `migrate_to_partitioned` (#742); `SCARD`+`SISMEMBER` → `SMEMBERS` in `pre_save`, `TYPE` filter in `rebuild_indexes` turning `WRONGTYPE` into a silent skip, `DEL`+`SREM` on absent class-set orphans (#735 TD3/TD4); `inf` → `+inf` bound rendering (#746 TD1); `MATCH *` token on `HSCAN` (#735) | as cited | N | release-notes paragraph; none is Python-visible except 18 above |
| 24 | Internal-path unique conflict is a three-deep exception chain (user `ModelException` → backend `ModelException` → `ResponseError`); `str()` byte-identical | #744 TD3 | N | `raise … from None` or accept |
| 25 | `_lua_tonumber` and the SQL `::float8` cast reject `0x` hex that Lua's `tonumber` accepts (Decimal envelope `as_encodable`, `s`/`c0`) | #737 TD2, #752 | N | note; absurd input |
| 26 | `2**63` packs as saturated `int64` on arm64 Redis and as float32 on Postgres/x86-64 (cmsgpack UB) | #737 TD1 (documented) | N | documented, not emulated |
| 27 | `update_confidence` reaches the backend (`EXISTS` on Redis, a transaction on Postgres) before the field layer rejects an unsaved instance | #751 TD5 | N | pre-check in the field; wire-neutral on Redis only if the `EXISTS` is kept |
| 28 | `drop_index` with a mismatched `kind` replies 0; `scan_index_members` yields nothing where Redis raises `WRONGTYPE`; `scan_index_names` sees only the three index tables; globs match characters not bytes; finite `float8` overflow raises where Redis stores `inf`; `-inf` timestamp underflows `power()` | #743 TD3 + body, #752 (all documented) | N | documented deviations |
| 29 | Lazy `fallback_idxs` raises at call time on Redis and at `commit()` on Postgres on the queued no-pointer path | #748 TD2 | N | docs sentence |
| 30 | `commit()` entry counts differ (`save_record` 2 vs 1, `drop_validity` 6 vs 1, supersede raw bytes vs decoded member, `confidence_update` raw strings vs tuple) | #737, #750 TD2, #752 | N | only truthiness is read today; a `UnitOfWork` result contract would pin it |
| 31 | No machine check for the PL/pgSQL phase split in `popoto_supersede` (the transaction is the real guard) | #750 TD3 | N | reviewability only |
| 32 | `clean_indexes` widens the delete window for a concurrently re-created record (`DEL`+`SREM` where base did `SREM`) | #735 TD3 | N | accept |
| 33 | Per-leg conformance counts are method-dependent (body 626/468 vs review 612/484 on the same head) | #751 TD1 | N | state the attribution rule with every count, as §1.1 does |
| 34 | `tests/test_concurrent_index_integrity.py` is Redis-level as a whole file (raw pointer reads, forced mid-EVAL errors, multiprocess children); its module `redis_only` is inert without `conformance`; the forced-error atomicity tests have no Postgres twin | #744 TD1, #748, #751 | N | leave; write Postgres fault-injection twins in `test_swaps.py` if the family ships |
| 35 | Inert `redis_only` marks on `test_validity_routes_through_backend.py` (7) and the recording files; `-m "not redis_only"` would deselect three whole files | #745 TD3, #742 review | N | docstring sentence; no workflow uses that expression |
| 36 | `TestSourceShape.REDIS_COMMANDS` omits `get`/`keys`/`type` (false positives); the recorder tests are the real guard | #735 TD2, #744 TD2, #746 TD3 | N | docstrings already say so |
| 37 | `StrictStrBackend._checked` iterates a `Sequence[str]` argument and would consume a generator before delegating | #751 TD3 | N | materialise or assert `list \| tuple` |
| 38 | Plan E-table row and the WS0 docstring named `Query.keys(catchall=True)` a `scan_record_keys` consumer; it is ledger row 14 on `native()`. The protocol docstring was corrected in #746's patch; the plan row is still stale | #746 TD2 | N | docs cascade on `sdlc-631.md` |
| 39 | Stale comment tail at `postgres.py` ("covered by the pointer lock and by the `FOR UPDATE`" -- it is the prefix lock now) | #750 nit | N | one line |
| 40 | `rank_decayed` returns the raw flat `[member, score, …]` bytes list; WS3d renders `%.14g` strings only to match it | decision 4, #742, #752 | N (decision) | §9 Q5 |
| 41 | `test_decay_confidence_route_through_backend.py` would be a vacuous Postgres leg (it rebinds a `RecordingBackend(RedisBackend())`) and is correctly unmarked; the same is true of the other five routing files | #752 | N | keep unmarked |

Closed during the POC and worth knowing about: #732 B1 (vacuous staleness
probe -- the lazy-cache mutant now fails 2 tests); #733 TD1-4 (`TRUNCATE
CASCADE`, schema-name regex, order-dependent reset test, leaked admin
connection); #735 B1/B2 (via protocol-2); #737 B1 (lost update on an absent
field -- advisory locks on every record writer); #737 bootstrap race (via
#743's DDL lock, 8-of-12 → 0); #743 TD1/TD4 (`-0.0`, `records_exist`); #744
B1 (eager tag normalisation -- `_LazyFallbackIdxs`); #745 B1 and #746 B1
(mypy regressions hidden by ratchet slack -- banked by #749, slack now zero);
#750 B1 (crossing pointer chains deadlock -- prefix lock).

## 8. Recommendation on the issue's merge mechanics

### 8.1 WS0 + WS1 (+ protocol-1/2/3 + WS1f) against `main` as a zero-behaviour-change minor release: **ready, with five prerequisites**

The wire-parity evidence is stronger than "the suite passes": every WS1
family was recorded against its base with a command tracer and the diff
read line by line.

| PR | Method | Result |
|---|---|---|
| #735 WS1a | `redis-cli monitor`, 24 scenarios | only the four admitted MULTI reorders + `SMEMBERS`/`TYPE`/`HSCAN` changes; B1/B2 found and fixed |
| #742 WS1d | monitor, 24 scenarios, 163 vs 161 commands | `DECAY_SCORE_LUA` and `CAPPED_BAYESIAN_UPDATE_LUA` argv byte-identical; one disclosed admin-path change |
| #744 WS1c | serializer hook, 39 scenarios | 39/39 identical in sequence, argv bytes, result and post-state; B1 found and fixed |
| #745 WS1e | serializer hook, 14 sections | 27 extra `ZSCORE invalid_at` and nothing else; `SUPERSEDE_LUA` argv identical incl. `now` |
| #746 WS1b | serializer hook, 95 scenarios, 669 → 669 commands | 92/95 identical; 3 = `inf`/`+inf` rendering |
| #751 WS1f | serializer hook, 10-14 sections, 926/949 commands | byte-identical JSON, `cmp` exit 0, base-vs-base re-run proves the trace is deterministic |

Two real divergences were caught by that method and **fixed rather than
ratified**: #735's partial-save `SADD` (a never-fully-saved hash became a
visible row -- key layout) and `EXPIRE`-before-`EVAL` (a `Meta.ttl` record
never expired -- data retention), both closed by protocol-2; and #744's
eager tag normalisation (`delete()` rejecting a value `save()` accepts),
closed by the lazy sequence. Two further blockers were test-side (#732's
vacuous probe; #745/#746's mypy regressions under ratchet slack, now banked
at 988 with zero slack).

Prerequisites before the re-proposal:

1. TD-1 (#747), because the phase checker is cited as a gate in the PR bodies
   and is vacuous.
2. TD-18 and TD-17 -- the only two Python-visible changes on Redis.
3. TD-19 (exceptions module) and TD-20 (one `_members_as_bytes`).
4. TD-7's rule for the lazy selector (§6), because it is the one part of
   WS0 a downstream user with a stray `POSTGRES_URL` can observe.
5. A release-notes paragraph listing TD-23's wire-only changes.

What ships: `backends/__init__.py` (the protocol, `get_backend`,
`set_backend`, `as_key_str`), `backends/redis.py`, the routed field and
model modules, the `pytest_plugin` pin and conformance harness, the markers.
`backends/postgres.py` and the `postgres` extra should ship **only** if the
selector is explicit (prerequisite 4); otherwise the stub is reachable from
the environment and the minor release is not zero-change.

### 8.2 WS2 + WS3 before `popoto[postgres]` ships

In dependency order: TD-16 (strip the family-H marks; now a grep); TD-21
(the Query-layer `bytes` → `str` flip, so the WS1f decode-at-call sites and
both `_members_as_bytes` go away); TD-11 and TD-14 (drop `popoto_numeric`,
add the pointer family column -- schema changes are cheapest before anyone
has data); TD-13 and TD-12 (encoding check, NUL rule); TD-3 (pool / thread
safety) and TD-2 (lock acquisition order at `commit()`); TD-8
(operator-owned DDL); TD-5 (`batch()` through `begin()`); TD-9 (capability
validation per model); TD-4 (the TTL story, §9 Q3); TD-6 (async). Then the
production schema decision (§5, §9 Q1), which is the one item that is a
redesign rather than a fix, and which the latency table says is worth
making before anyone benchmarks `popoto[postgres]` against Redis in anger.

## 9. Questions for the architect

Only the genuinely open ones; everything else above is a recommendation.

1. **Production schema route.** Ship `popoto[postgres]` v1 on the generic
   `bytea` tables with the measured 3-6x ranking penalty and the PL/pgSQL
   decoder, or hold it for per-model typed tables with backend-owned
   encoding (a protocol v2 where `save_record` takes decoded values and the
   validity/sorted groups take `(model, field)`)? The first is weeks; the
   second reopens WS0's scope. This report recommends the second as the
   target and the first only as an explicitly labelled preview.
2. **Unit-of-work failure semantics.** Postgres rolls back the whole queue;
   a Redis pipeline commits the other commands. Document "atomic on
   Postgres, best-effort on Redis", or change the Redis `commit()` to
   `MULTI`/`EXEC` with `WATCH`-style abort (it cannot roll back a partial
   `EXEC`)? The POC pinned the difference leg-aware; production needs one
   contract.
3. **TTL on Postgres.** (a) an `expires_at` column on the record, a read-time
   `WHERE` in every record read, and a periodic reaper; (b) `pg_cron`; (c)
   refuse `Meta.ttl` models on Postgres permanently. (a) changes every read;
   (b) adds an extension dependency; (c) excludes the memory-lifecycle
   recipes.
4. **NUL bytes.** Refuse at the field layer on both backends (a behaviour
   change on Redis for a key no one should have) or widen the Postgres
   columns to `bytea` (bytewise comparison for free, loses `text` tooling)?
5. **`rank_decayed`'s return type (decision 4, reopened).** With both
   backends now producing typed `(member, score)` internally and rendering
   `%.14g` strings only for the flat reply, is the production shape typed
   pairs (touching `recipes/context_assembler.py` and the two `query.py`
   decode loops) or the raw list forever?
6. **Does `backends/` ship in the minor release with the explicit selector
   and no `PostgresBackend`, or with the backend behind the extra?** §8.1
   recommends the former unless prerequisite 4 lands first.
