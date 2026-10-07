# Redis to Postgres Migration (#756)

A one-off tool that copies agent memory from a Redis store into the
[Postgres backend](postgres-backend.md). It is built for Valor's `Memory`,
but works on any popoto model the Postgres backend accepts. Run it once per
machine: each machine's Redis store merges into the one central Postgres
database.

```bash
python -m popoto.migrate_redis_to_postgres \
    --rdb /archive/laptop-1/dump.rdb \
    --content-dir /archive/laptop-1/content \
    --run-dir /archive/laptop-1/migration \
    --source-id laptop-1 \
    --mapping myapp.memory_migration:MAPPINGS
```

The target is the library's own setting: `POPOTO_POSTGRES_URL` and
`POPOTO_POSTGRES_SCHEMA`. The source is the RDB file. The tool has no Redis
URL, host or port option.

If `POPOTO_POSTGRES_URL` points at PgBouncer in transaction mode, also set
`POPOTO_POSTGRES_MAINTENANCE_URL` to a direct (or session-mode) DSN to the
same database. When calling `run_migration()`, pass
`postgres_maintenance_dsn=`; the variable is read only when the main URL
also comes from the environment. The tool opens one connection to that DSN,
outside any pool, and holds it for the whole run. On it, the tool:

- takes the run lock, a session advisory lock that refuses a second run into
  the same schema;
- creates its run and ledger tables.

The model tables' first-use DDL also goes to that DSN, through the backend.
A session lock needs a real session. Through a transaction-mode pooler it
would stay with the pooled server connection after the run ended or the
process died, and every later run would be refused. Without a maintenance
DSN the lock and the tool's DDL use the main DSN, which is right for a
direct connection. Before taking the lock, the tool checks that the
maintenance DSN reaches the same database as the main one. If it does not,
the run is refused before anything is written. When the two DSNs run as
different roles, the main role is granted read and write on the tool's
tables, as on the model tables (see [two
roles](postgres-backend.md#two-roles-an-application-role-and-an-owner-role)).

## Safety model

**The tool never connects to a live Redis.** You freeze the writers, take a
`BGSAVE`, and copy the RDB file and the content directory. The tool copies
that RDB into a private temporary directory (mode `0700`) and starts its own
`redis-server` with persistence switched off (`save ""`, `appendonly no`).
It reads only from that process, identified by its `run_id`.

**The copy is never reachable from outside the run.** The throwaway server
listens on no TCP port at all (`port 0`). It listens only on a unix socket
inside the private directory (`unixsocketperm 700`). It also requires a
random password (`requirepass`) and runs with `protected-mode yes`. The
password is written to a `0600` config file, never to the command line,
where `ps` would show it. A port collision with another server cannot
happen, so the tool never probes a server it did not start.

**Nothing outlives the run, however it ends.** Before any copy of the store
exists, the tool starts a small watchdog process in a session of its own.
The watchdog uses only the standard library and cannot connect to anything.
It spawns the throwaway server and is told every private directory: the RDB
copy, the content copy and the socket. When the tool exits, the watchdog
stops the server and deletes those directories. That holds whether the run
succeeded, raised, or was killed:

- `SIGTERM`, `SIGHUP` (what a dropped SSH session sends) and `SIGINT`
  (Ctrl-C) unwind the run normally.
- `SIGKILL` cannot be caught. The kernel closes the tool's end of the
  watchdog's pipe, and the watchdog cleans up within a fraction of a second.

The watchdog runs in its own session, so a terminal's `SIGHUP` or `SIGINT`
never reaches it or the server directly. The tests kill the tool with each
of the four signals mid-run. Each time they check that no server, watchdog
or directory is left, and that the server never had a TCP socket.

One case is not covered: `SIGKILL` sent to both the tool and the watchdog,
for example `kill -9` on every process you own. If the watchdog alone dies,
the tool stops the server itself when the run ends. If both die, the server
keeps running and the copy stays on disk. Even so, it is reachable only through its unix socket,
which sits in a `0700` directory and needs the password. After such a kill,
look for a leftover with `ps -ax -o pid,command | grep 'redis-server
unixsocket:'`. Stop it with `kill <pid>`, then remove the
`popoto-migrate-work-*` and `pmig-*` directories under `$TMPDIR`.

Two read paths reach the throwaway server, and both are checked:

- **The inventory** uses `ReadOnlyRedis`. It can only be built from the
  throwaway server, it re-checks that server's `run_id`, and it refuses any
  command outside a read-only allowlist (`SCAN`, `TYPE`, `HGETALL`,
  `ZRANGE`, `INFO` and similar) before the command is sent. `SET`, `DEL`,
  `FLUSHDB`, `FLUSHALL`, `CONFIG`, `SHUTDOWN` and `EVAL` raise
  `ForbiddenCommand`.
- **Record export** reuses `popoto.transfer.export_records`, which reads
  through popoto's global client. The tool rebinds that client to the
  throwaway server's socket and checks the binding. It compares the
  connection parameters first. A client with a host and port, such as
  popoto's default database 0 on a live server, is refused with
  `LiveRedisRefused` before any command reaches it. So is any other socket.
  It then compares the `run_id`.

Some popoto read paths write: hydration purges orphan index entries, and
reads stage access-tracker entries. Those writes land in the disposable
process.

On Postgres, records go in only through popoto's save path
(`import_records`). The engine therefore builds every derived table it will
later maintain: BM25 postings, narrow vector rows, membership tokens and
indexes. The tool's only other writes are its provenance columns, its ledger
and its resume marker. A batch's saves, the carried state restored onto
them, their provenance, their ledger rows and the resume marker commit in
**one Postgres transaction** (see *Resume and idempotency*).

## Runbook

Each machine has its own Redis, so run this once per machine. The operator
is the maintainer, on the machine itself.

**T-1 day: rehearsal.** Run the whole procedure against a fresh snapshot
into a scratch Postgres database, without freezing anything. Read the report
(below). The report's `duration_seconds` sizes the downtime window.

**T-0:**

1. **Drain.** Let pending memory-extraction jobs and session sidecars drain,
   so nothing writes memory after the snapshot.
2. **Freeze.** Stop every memory writer on the machine: close the Claude
   Code sessions (their hooks write memory), then stop the bridge, the
   worker (all memory reflections) and the memory MCP server.
   *Rollback: restart them.*
3. **Snapshot.** On the live server, run `BGSAVE` and poll `LASTSAVE` until
   it advances. Record `INFO persistence`'s `rdb_changes_since_last_save`.
   Copy `dump.rdb` and `$POPOTO_CONTENT_PATH` (default `~/.popoto/content`,
   which holds `.embeddings/<Model>/*.npy` and any `ContentField` files) into
   an archive directory. These are the only commands you send the live
   server, and none of them changes data.
4. **Dry run.** Run the tool with `--dry-run` and a run directory of its
   own, say `--run-dir /archive/laptop-1/dry-run`. It reads the snapshot,
   writes `inventory.json`, `export/`, `transform/` and the report, and
   writes nothing to Postgres. Read the lossy counts. A dry run *claims* its
   run directory (it writes `run.json`). If step 5 used the same directory,
   it would be refused unless you passed `--resume`, so give the real run a
   fresh one.
5. **Migrate.** Run it without `--dry-run`, with `--run-dir
   /archive/laptop-1/migration` and, for tamper evidence, `--report-key`
   (see step 6). Its exit code is `0` only when verification is clean.
6. **Verify.** Read `report.txt`. Every check must be `ok` and the verdict
   must be `clean`. The report is sealed two ways:
   - `sign_off.checksum_sha256` covers the whole JSON, including the
     operator and time. It catches an accidental edit or a truncated copy.
     It is **not** tamper evidence, because anyone who edits the report can
     recompute it.
   - `sign_off.hmac_sha256` is present only when you pass `--report-key
     FILE`. The file holds at least 16 secret bytes, for example from
     `openssl rand -hex 32`. Without the key, nobody can recompute the HMAC
     after an edit. Keep the key file somewhere other than the archive.
     Check a report with
     `popoto.migrate_redis_to_postgres.verify_report(data, key)`.

   *No-go rollback: restart the writers on Redis unchanged. The migrated
   rows can stay, because nothing reads them yet, or be deleted by their
   `_migrated_from->>'run_id'`.*
7. **Repoint.** Set `Meta.backend = "postgres"` on the model (or
   `POPOTO_BACKEND=postgres`) and deploy. Restart the writers.
8. **Late-write check.** Compare `rdb_changes_since_last_save` with the
   value you recorded at step 3. A non-zero value counts every write, not
   only memory writes, so treat it as a prompt to check. If memory changed,
   take a second snapshot and run again with the same `--source-id`, a new
   `--run-dir` and `--merge`. The merge rule replaces this source's own
   changed rows and never touches a row popoto has written natively since.
9. **Archive.** Copy `report.json` and `report.txt` out of the run
   directory, then delete the run directory. Its `export/` and `transform/`
   hold a plaintext copy of the memory. The tool creates them `0700`, with
   `0600` files, but they have no use after sign-off. A resume needs the
   directory only until the run is signed off, and a later `--merge` uses a
   new one. The RDB and the content copy are the same memory, so keep them
   only as long as the rollback needs them. Leave the Redis keys in place.
   Deleting them is a separate decision.

Rollback costs nothing until step 7. After that, memories written on
Postgres do not exist in Redis.

## Merging several machines

Each machine's store is one run, with its own `--source-id` (the machine
name, say). The first run loads into an empty schema. Every later run passes
`--merge`. Run the stores one after another. Each run holds a Postgres
advisory lock on the target schema from preflight to the end. A second run
started meanwhile is refused (exit `2`) before it reads the snapshot or
writes to Postgres. By then it has already hashed the RDB and claimed its
`--run-dir` (it wrote `run.json`), so retry it with a **new** `--run-dir`.
Reusing the claimed one is refused without `--resume`, and a `--resume`
would continue a run that never started. Per key:

| Situation | Decision | What happens |
|---|---|---|
| No row yet | `inserted` | The record lands. |
| Equal payload from another source | `deduplicated` | Kept as is. This source is added to `_migrated_from.duplicates`. |
| Equal payload from this source (a re-run) | `unchanged` | Nothing is written. |
| Differing payload from this source (a newer snapshot) | `updated_delta` | Replaced. This is the delta mechanism. |
| Differing payload from another source | `won_merge` / `lost_merge` | The later `_updated_at` wins. A tie goes to the greater source id. The loser is logged in the winner's `_migrated_from.losers`. |
| A row popoto wrote natively (`_migrated_from IS NULL`) | `conflict_native` | Never overwritten. |
| A row another run left half-written (it crashed between import and provenance; only a run from before batches were atomic can leave one) | `conflict_native` | Treated as native. Only the run that wrote it can adopt it (see *Resume*). |
| A record Postgres cannot store | `rejected` | Not written. Counted, with the reason. |

The payload is the record's normalized export: field values, confidence
evidence, access counters, validity, edges, cycles and the float32 vector
digest. The source id appears only in `_migrated_from`. It never becomes part
of a key, a scope or a partition.

`_migrated_from.duplicates` and `.losers` record each source once. Re-running
the same snapshot leaves them exactly as they were. A newer snapshot from the
same source replaces that source's entry. A source that wins drops out of
`losers`.

Rule order matters only for ties. The final row is the same whichever
machine loads first, except that the first source of an equal payload is
recorded as the owner and later ones as duplicates.

## Mapping

A model crosses field for field. The target is the same popoto class bound
to Postgres, so its table is the one the backend compiles, with the column
names and types M2a and M2b shipped. A mapping (`ModelMapping`) adds only
the evidence Redis never stored:

```python
from popoto.migrate_redis_to_postgres import ModelMapping

MAPPINGS = [
    ModelMapping(
        model=Memory,
        # Earliest evidence of a memory besides its access log (#757 L14).
        created_at_paths=("metadata.outcome_history[].ts",),
        # Carried verbatim, counted in the report.
        sentinel_values={
            "superseded_by": (
                "dismissal-prune", "decay-prune-tier2", "cleanup-junk-extraction",
            ),
        },
        # Valor's ids are uuid4 hex; anything else is rejected and counted.
        id_patterns={"memory_id": r"^[0-9a-f]{32}$"},
    ),
]
```

`--model module:Class` is the same with no extra evidence. A model whose
post-cutover declaration already says `Meta.backend = "postgres"` works too:
the tool binds it to Redis to export and to its own Postgres backend to
import.

For Valor's `Memory`, as surveyed by #757 and #758 and mirrored by the test
fixture `tests/postgres/migrate_fixtures.py::MigMemory`:

| Redis | Postgres (`<schema>.memory`) | Notes |
|---|---|---|
| hash key `Memory:{agent_id}:{memory_id}:{project_key}` | `_pk` | The same string, so `Model.pk` and every reference are unchanged. |
| `agent_id`, `memory_id`, `project_key` | `text` columns under one `UNIQUE` | `project_key` is the scope (`partition_by`). |
| `content`, `title`, `source`, `reference`, `superseded_by`, `superseded_by_rationale` | `text` | Retirement sentinels stay in `superseded_by` (there is no `retired_reason` column) and are counted. |
| `importance` | `double precision` | The decay base score. |
| `metadata` (tags, category, outcome telemetry) | `jsonb` | Verbatim. |
| `relevance` (decay clock; the `$DecayingSortF` zset score) | `relevance double precision`, epoch seconds | The zset is rebuilt as a B-tree. The inventory cross-checks each score against the hash. |
| `$ConfidencF:Memory:confidence:data` entry | `confidence__conf`, `__n`, `__corr`, `__contra` | Authoritative. The attribute mirror lands in `confidence`. |
| `$AT:Memory:meta:{key}` | `_access_count`, `_last_accessed` | Carried. |
| `$AT:Memory:staged:{key}` | `_staged_reads`, `_staged_at` | Carried by the tool. `popoto.transfer` alone drops staged reads. |
| `$AT:Memory:access_log:{key}` | none | Dropped and counted. Used as `_created_at` evidence. |
| `.embeddings/Memory/{sha256(key)}.npy` | `embedding` (dims), `embedding__vec`, `embedding__model`, `embedding__hash`, and the narrow vector table | Carried without calling the provider. Missing files and wrong-dimension vectors are left `NULL` for the backfill to re-embed, and counted. |
| `$BM25:Memory:bm25:*` | `memory__bm25__post`, `memory__bm25__dl` | Rebuilt by the import's save. |
| `$EF:Memory:bloom` | `memory__bloom__tok` | Rebuilt, exact. Bloom bits of deleted records are not carried. |
| `$WF:Memory:priority` | none | The priority tier is not stored on Postgres. Counted. |
| `$Class:Memory`, `$KeyF:Memory:*` | the table and its B-trees | Rebuilt. |
| (none) | `_created_at`, `_updated_at` | Estimated, and named in `_estimated_fields`. |
| (none) | `_migrated_from` | Source, run, snapshot hash, payload hash, duplicates and losers. |

The rest of the gap list in
[Cross-backend migration notes](postgres-backend.md#cross-backend-migration-notes-for-756)
is handled as follows:

- `ContentField` text is resolved from the copied content directory and
  carried inline. An unreadable file rejects the record.
- A per-record TTL is not carried. The record gets `Meta.ttl` from its
  import time. A record of a `Meta.ttl` model whose remaining TTL is no
  longer than `Meta.ttl` is counted as `meta_ttl_restarted`, not as a
  per-record TTL: its expiry clock restarts at the import. A per-record TTL
  *shorter* than `Meta.ttl` looks the same in a snapshot and is counted
  there too.
- `FrequencySketch` restarts at one count per record.
- A cycle's declared baseline is dropped (#698).
- Co-occurrence edges are truncated to the destination's `max_edges`, keeping
  the highest weights, and weights above the cap are clamped. Both are
  counted.
- Geo coordinates are re-saved and rebuilt.
- An `EventStreamMixin` stream is not carried. The import's saves append one
  event per record to the Postgres stream, so start consumers after the
  migration. A resume does not append a second one for a row it adopts
  (see *Resume and idempotency*), and verification checks the count.

## What the report counts

Every lossy or estimated case has a counter. A counter that stays at zero is
omitted.

| Counter | Meaning |
|---|---|
| `created_at_estimated`, `updated_at_estimated` | Every record. Redis stores neither time. `_created_at` is the earliest of the decay clocks, access log, staged reads and the mapping's paths. `_updated_at` is the latest decay clock or `auto_now` datetime. |
| `created_at_from_snapshot_time`, `updated_at_from_snapshot_time` | No evidence at all, so the RDB file's time is used. |
| `access_log_entries_dropped` | Confirmed access-log timestamps. The counters cross; the log does not. |
| `staged_reads_carried` | Unconfirmed reads carried to `_staged_reads`. |
| `embedding_file_missing_reembed`, `embedding_dimension_mismatch_reembed` | Vectors left `NULL` for the embedding backfill. |
| `embedding_files_without_record` | `.npy` files no record names. |
| `rejected_nul_bytes`, `rejected_id_pattern`, `rejected_content_file_missing` | Records not written. Postgres `text` refuses NUL. |
| `meta_ttl_restarted` | Records of a `Meta.ttl` model with no more than `Meta.ttl` left. Postgres applies `Meta.ttl` again from the import. |
| `confidence_companion_missing` | Records with no `ConfidenceField` companion-hash entry. Information only: they read as the field's seed on Redis and land with it (initial confidence, no evidence), and verification compares exactly that. |
| `per_record_ttl_not_carried`, `frequency_sketch_keys_reset`, `cycle_baselines_dropped`, `co_occurrence_edges_truncated`, `co_occurrence_weights_clamped`, `event_stream_entries_not_carried`, `write_filter_priority_keys_not_stored` | The gap list above. |
| `orphan_hashes_recovered` | Record hashes missing from the class set. `export_records` alone cannot see them. They are migrated. |
| `class_members_without_hash` | Class-set entries with no record. There is nothing to migrate. |
| `decay_index_score_mismatches` | Decay zset scores that differ from the record's clock. The record's value is the one carried. |
| `export_errors` | Records the export could not serialize. Each is listed. |

## Verification

With the throwaway server still running, the tool reads everything back
through popoto on Postgres and compares it with the snapshot:

- **records**: the normalized Postgres re-export of every key this source
  owns equals the transformed source record. That covers values, confidence
  evidence, access counters, validity, graph edges and cycles. Vectors are
  compared bit for bit as float32. The first 20 mismatches are listed with
  the differing parts (`values.importance`, `state.confidence`, ...).
- **decay order**: `top_by_decay` (with `no_track()`) per sampled partition,
  on both sides, restricted to this source's keys.
- **BM25**: `BM25Field.search` on sample queries taken from the records.
  When the two corpora hold the same documents, the ranked top 10 must match
  exactly. When they differ, because a record was rejected or other stores
  share the table, scores differ by corpus statistics. Which of this
  source's records match a query does not differ, because every IDF is
  positive. The check then compares that set in full on both sides, so it
  still fails when a record lost its postings. The top-10 overlap is also
  reported, as information.
- **tool columns**: the columns the tool writes itself, outside popoto's
  save path. These are `_created_at`, `_updated_at`, `_estimated_fields`
  and each embedding's `<f>__hash` (`md5` of the source text wherever a
  vector landed). They are compared for every row this run wrote.
- **event stream**: on an `EventStreamMixin` model, every key this run
  wrote and still owns holds exactly one save event (`create` or `update`)
  appended since the run first queued its write. Two events for one key is
  a mismatch: that is what a resume used to leave when it saved an adopted
  row again. No event is a mismatch too, unless the stream was trimmed to
  its `MAXLEN` (the default is 10000 entries) or had entries deleted.
- **carried state**: every key this run queued a save for, read from the
  ledger rather than from `_migrated_from`, holds the snapshot's carried
  state: confidence evidence, access counters, the prediction ledger,
  edges, cycles, validity, and the vector's dimensions and float32 hash.
  That includes a key the run ended up reporting as `conflict_native`. A
  row whose save committed without its carried state therefore fails the
  run instead of hiding behind a native-looking row. A row the run wrote
  and the application then changed before a resume fails it too; inspect
  it. A row another source has won since is left out here and checked by
  **superseded by merge** instead.
- **superseded by merge** (only when there is such a row): a row this run
  wrote that another source then won under the merge rule. This happens
  when a run is resumed after another store merged into the schema (see
  *Resume*). The row no longer holds this run's snapshot, and it should
  not. It is checked against the winner instead. A row is a candidate when
  its `_migrated_from` names another source and lists this source's
  snapshot of exactly this payload in `losers`, which is what the winning
  run writes. `_migrated_from` cannot vouch for itself, so the candidate
  counts as superseded only when the tool's own tables back the claimed
  winner: its run is in `popoto_migration_run` with the same source and
  snapshot, that run's ledger row for the key is `done` with a write
  decision and the same `payload_sha`, the `updated_at` the ledger
  recorded is the claimed one, and the row's `_updated_at` column holds it.
  A superseded row must then hold exactly the winner's payload (values and
  carried state), and the winner must beat this run's copy under the merge
  rule, decided on the ledger's recorded `updated_at` and the run's source
  id. Its `_created_at`, `_estimated_fields`, embedding hashes and staged
  reads must be what the winner's ledger says it wrote. A candidate that
  fails any of these is listed as `unconfirmed` and is not treated as
  superseded: the `records` and `carried state` checks compare it with this
  run's snapshot, so the run is a mismatch. So is a row popoto saved
  natively (`_migrated_from` is `NULL`), which is never a candidate. A
  ledger written before the `wrote` column existed has no recorded values,
  so a row won by such a run is `unconfirmed`; inspect it.
- **staged reads** match the inventory.
- **`check_indexes()`** reports no drift.

Example `report.txt`, from the test fixture (`report.json` holds the same
data and more detail). This run had no `--report-key`:

```text
popoto Redis -> Postgres migration (#756): CLEAN
run b0d49d3e461c4fbdbdad54543dc4f96e  source valor-laptop  snapshot sha256 07301e66eb8e8038
target popoto  mode load

Records:
  MigMemory: exported 11, inserted 11
  MigLongTail: exported 2, inserted 2
  MigTtl: exported 2, inserted 2

Lossy and estimated (every non-zero count):
  access_log_entries_dropped: 3
  class_members_without_hash: 1
  created_at_estimated: 15
  created_at_from_snapshot_time: 2
  cycle_baselines_dropped: 2
  embedding_dimension_mismatch_reembed: 1
  embedding_file_missing_reembed: 1
  event_stream_entries_not_carried: 3
  frequency_sketch_keys_reset: 1
  orphan_hashes_recovered: 1
  meta_ttl_restarted: 1
  per_record_ttl_not_carried: 1
  staged_reads_carried: 2
  updated_at_estimated: 15
  updated_at_from_snapshot_time: 2
  write_filter_priority_keys_not_stored: 1

Verification:
  MigMemory records: ok {"compared": 11, "expected": 11, "mismatched": 0}
  MigMemory check_indexes: ok {"total": 0}
  MigMemory tool_columns: ok {"compared": 11, "mismatched": 0}
  MigMemory staged_reads: ok {"mismatched": 0}
  MigMemory decay_order:relevance: ok {"partitions": 2, "mismatched": 0}
  MigMemory bm25:bm25: ok {"queries": 11, "mode": "strict", "mismatched": 0, "mean_top_k_overlap": null}
  MigLongTail records: ok {"compared": 2, "expected": 2, "mismatched": 0}
  MigLongTail check_indexes: ok {"total": 0}
  MigLongTail tool_columns: ok {"compared": 2, "mismatched": 0}
  MigLongTail event_stream: ok {"written": 2, "save_events": 2, "keys_with_more_than_one": 0, "keys_without_one": 0, "stream_trimmed": false}
  MigLongTail decay_order:rhythm: ok {"partitions": 1, "mismatched": 0}
  MigTtl records: ok {"compared": 2, "expected": 2, "mismatched": 0}
  MigTtl check_indexes: ok {"total": 0}
  MigTtl tool_columns: ok {"compared": 2, "mismatched": 0}

Signed off by maintainer at 2026-10-06T00:18:39+00:00.
Checksum sha256 f81df632d6180c15... (catches accidental change only: anyone can recompute it).
No HMAC: the run had no --report-key.
```

## Inventory and stop conditions

Before anything is exported, the inventory accounts for every key in the
snapshot. Each key family of a migrated model has a disposition, recorded in
`inventory.json`:

- **irreplaceable**: carried. This covers record hashes, confidence
  evidence, access counters and staged reads, edges, validity, cycles and
  pressure, and the prediction ledger.
- **rebuildable**: rebuilt by the save. This covers class sets, key and
  sorted indexes, BM25, existence filters and geo.
- **not carried**: dropped by design and counted. This covers the access
  log, the frequency sketch, write-filter priority and the event stream.
- **expected empty**: `$TOMBPRIOR`, legacy `$IdxPtr` pointers, and NUL-byte
  index-pointer fields. Any key here **stops the run** (exit code 3) before
  anything is written.

A key family the tool does not recognize also stops the run, unless you pass
`--accept-unclassified` after reading `inventory.json`. Keys of other models
are counted under `out_of_scope` and left alone. That includes Valor's
memory-gate counters, which stay in Redis.

## Resume and idempotency

Records load in batches (`--batch-size`, default 200). Each batch is **one
Postgres transaction**:

1. It takes its locks, in the backend's one lock order: any `(model,
   field)` validity lock, then the record-key locks of every key in the
   batch, in `_pk` byte order. It reads the existing rows only after that,
   so a native save cannot slip in between the merge decision and the
   write. The stream rows are locked last, at commit.
2. It writes `pending` ledger rows.
3. It lands its records through `import_records(uow=...)`. Each record's
   save and the restore of its carried state (confidence evidence, access
   counters, prediction ledger, edges, cycles, validity, vector) run in
   that transaction, under a savepoint per record. A record that fails is
   rolled back whole and reported as a load error. Each save sets
   `_migrated_from = NULL`, like any native write.
4. It writes the provenance columns, marks the ledger `done` and advances
   the resume marker. Then it commits.

A crash at any point, `kill -9` included, therefore leaves either none of
the batch or all of it. There is no row without its carried state or its
provenance. Re-run with `--resume` and the same `--run-dir`: it skips the
committed batches and loads the rest. Without `--resume`, the committed
batches make the target non-empty, and the run is refused.

A plain `--resume` continues only into a table that holds nothing but this
run's rows. If it holds rows the run did not write, the resume is refused
too, and the message says so and names `--resume --merge`. Those rows can
have been there before the run started, when the run itself began with
`--merge` into a table another store had already loaded. Or they were
written while the run was down: by popoto saving one of the run's
committed rows natively, which clears its `_migrated_from`, or by another
store's `--merge`. Either way, re-run with `--resume --merge` and the same
`--run-dir`. The batches still to load
then go through the merge rule, which never overwrites a natively saved
row. A row the run wrote and the application then changed is reported by
`records` and `carried state` as a mismatch, so inspect it before you sign
off. A deadlock or
serialization failure rolls the batch back and runs it again, up to three
times.

Holding a batch's locks to its commit costs little. On a 1500-record store
with a stream, confidence and access tracking, the load phase took 2.6 to
3.0 s at `--batch-size` 100 or 200. With a commit per statement, as before,
it took 3.8 to 4.2 s. Each batch held its locks for at most 0.22 s at 100
and 0.48 s at 200. A batch blocks only native writes to its own keys, and
only while it runs.

A schema that a run from **before batches were atomic** crashed into can
still hold rows that look native, with `pending` ledger rows that show the
tool wrote them. The resume rules for those are unchanged: the same run
adopts them (`resumed`).

An adopted row is **not saved again**. Its save committed together with
everything the save derives: the `EventStreamMixin` event and its
notification, BM25 postings, vector rows, membership tokens. Only its
provenance is written. A second save would append a second `create` event
per adopted row, and that is what earlier versions did: 1600 events for
1500 records after a `kill -9` and a resume. Every table popoto keeps is
left as a clean load leaves it, and the `event_stream` check fails if it is
not.

Adoption is narrow on purpose:

- **Only the same run adopts its pending rows.** The match is on `run_id`.
  To any other run, including a `--merge` from another machine, a row
  another run left pending is indistinguishable from a native one, and
  might have become one. It is treated as native (`conflict_native`) and
  left alone. Its ledger row stays `pending` for the run that owns it.
- **Only rows that still hold what the run imported.** If popoto saved a
  pending row natively after the crash, its re-export no longer hashes to
  the ledger's `payload_sha`. The resume then reports it as
  `conflict_native` and leaves it alone.

**Another source merged while this run was down.** Say run A crashed after
some of its batches committed, and source B then ran with `--merge`. B sees
A's committed rows as ordinary migrated rows, and the merge rule decides
each shared key. Where B's copy is newer, B wins the row (`won_merge`) and
lists A's snapshot in the row's `_migrated_from.losers`. Resume A with
`--resume --merge` and the same `--run-dir`. Its verification checks the
rows B won against B's provenance, under **superseded by merge**, rather
than against A's snapshot, so A reaches `clean` when the data is what the
merge rule says. Earlier versions compared those rows with A's snapshot and
reported a mismatch for every row B had won, although the data was correct.

One situation, which only a run from before batches were atomic can cause,
needs a manual step after a resume:

- **Half-written rows of an old run.** An atomic batch leaves no
  half-written rows. If an older run A crashed between import and
  provenance, and source B then ran with `--merge` before A resumed, A's
  half-written rows looked native to B. B reported those keys as
  `conflict_native` and did not write them, yet its verdict is still
  `clean`. After A's resume finishes, run B again: the same snapshot,
  `--merge` and a **new** `--run-dir`. Its rows then go through the merge
  rule (`won_merge`, `lost_merge` or `deduplicated`). Do this for every
  source that ran in between and reported `conflict_native`, in the order
  they first ran.

The tool keeps two tables in the target schema: `popoto_migration_run` (one
row per run, with its status, progress and final report) and
`popoto_migration_ledger` (one row per key per run, with its decision).

## Exit codes and refusals

| Code | Meaning |
|---|---|
| `0` | `clean`, or a finished `--dry-run`. |
| `1` | The load finished but verification found a mismatch. Read `report.txt`. |
| `2` | Refused before reading. The causes are: a bad RDB; a missing `redis-server`; a model Postgres cannot store (`DataFrameField`); a target schema that already holds rows (pass `--merge` or `--resume`, or `--resume --merge` when a resumed run's table also holds rows it did not write); a target schema another run is loading (the advisory lock); a run directory that belongs to another run, or one a dry run already claimed; a short or missing `--report-key`; or an empty snapshot (`--allow-empty`). |
| `3` | The inventory stopped the run. Nothing was written. |
| `130` | Interrupted by `SIGTERM`, `SIGHUP` or `SIGINT`. The throwaway server is stopped and its copies are removed. Continue with `--resume` and the same `--run-dir`. |

`redis-server` must be on `PATH` (or passed with `--redis-server`), at the
same major version as the server that wrote the snapshot.

## Where this departs from the #757 plan

The #757 plan was written before the schema existed. The build follows its
safety model and adapts to what shipped:

- **Target.** Each model gets its own typed table (v2), not a hand-written
  `popoto.memory` table. Records land through `import_records`, not through
  binary `COPY` into staging. The engine owns the DDL and builds every
  derived table, so no separate `reindex` step is needed.
- **Columns.** Clocks are `double precision` epoch seconds. Confidence uses
  the `<f>__conf/n/corr/contra` columns. Access state uses
  `_access_count/_last_accessed/_staged_reads/_staged_at`. Provenance uses
  `_migrated_from`/`_estimated_fields`, with `_created_at` standing in for
  #757's `created_at`.
- **Supersession.** Retirement sentinels stay in `superseded_by`, because
  the model has no `retired_reason` column; they are counted. There is no
  `superseded_at` column, so it is not estimated.
- **Machines.** Several machines merge into one database under the v2 rule
  above, instead of #757's "a primary-key collision aborts".
- **Packaging.** The tool ships as the package
  `popoto.migrate_redis_to_postgres`, run with `python -m`, instead of a
  `tools/` directory outside the package. Its `__main__.py` only imports
  `main` from the package. That way the `ModelMapping` your mapping module
  imports is the same class the CLI checks. It sits beside `popoto.transfer`,
  not inside it, because `transfer/` is the model-generic driver and is
  pinned never to name a concrete field type
  (`tests/test_transfer_roundtrip.py::TestGenericDriver`). This tool knows
  the memory fields by design.
- **Not built here.** The retrieval-parity harness against Valor's
  `retrieve_memories` runs in Valor's repo. This tool's decay-order and
  BM25 checks are the library-level equivalent.
