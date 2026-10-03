---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-10-03
tracking: https://github.com/tomcounsell/popoto/issues/756
last_comment_id:
depends_on: https://github.com/tomcounsell/popoto/issues/755
---

# One-off migration: Valor agent memory, Redis to Postgres

## Problem

Valor's subconscious memory lives in Redis DB 0 on each machine that runs it. Per the 2026-10-03 maintainer decisions on #755, agent memory moves to a Postgres-native store with per-model typed tables, pgvector and `tstzrange` validity. The schema is designed clean, without dual-write or read-through. If nothing carries the existing records across, Valor wakes up on Postgres with an empty memory. That loses months of human instructions (`source="human"`, importance 6.0), the act/dismiss outcome history that tunes recall, Bayesian confidence evidence, and access history.

**Current behavior:**
No path from Redis memory to the new store exists. The nearest tool, `popoto.transfer`, exports JSONL and imports only back into a popoto `Model` on Redis. Read naively, the Redis store is also dangerous to copy:
- Part of the state that matters exists only in side structures: confidence evidence in a companion hash, and access history under `$AT:` keys.
- The vectors are not in Redis at all. They are `.npy` files on local disk.
- Several popoto read paths write to Redis: `get_many_objects` purges orphans, and `get`/`get_many`/`.all()` stage access-tracker reads.
- The source is the live agent store.

**Desired outcome:**
A one-off, re-runnable utility that never writes to the live Redis. It:
1. Takes a frozen snapshot of one machine's memory.
2. Produces an auditable, checksummed intermediate artifact.
3. Loads that artifact into the #755 schema idempotently, with a dry-run.
4. Proves the result at three levels: counts, a per-record logical checksum covering every record, and retrieval parity on a query sample.

It ships with a cutover runbook (freeze → snapshot → migrate → verify → repoint → resume) and a rollback that costs nothing until writers resume. It is not part of popoto's runtime path and is not shipped in the wheel.

## Freshness Check

**Baseline commit:** `57c29ebf8ef6faca93412ec99ccdf90b4f433583` (origin/main at plan time)
**Issue filed at:** 2026-10-03T01:26:25Z
**Disposition:** Unchanged

**File:line references re-verified:** The issue cites no line numbers. It names `/Users/valorengels/src/ai/models/memory.py`, which was read in full at plan time. The field list matches the issue, with two refinements:
- The model has no `ValidityField`. Supersession is two plain `StringField`s, `superseded_by` and `superseded_by_rationale`.
- Outcome telemetry is stored inside the `metadata` `DictField`, not in a separate structure.

**Cited sibling issues/PRs re-checked:**
- #755: open, with no plan PR yet (`gh pr list --state all --search 755` matched only the unrelated #591). Its schema is not fixed, so this plan maps against the logical model and declares the physical mapping a dependency.
- #557 / PR #721: merged 2026-09-16. Key-regenerating import. Not needed here, because the migration preserves `memory_id` (see Solution).
- #554 / PR #558, #555 / PR #626, #556 / PR #675: merged. Generic export/import, the CLI, and history-shaped round-trip carry. These are what the extract stage reuses.
- #631 POC: complete on `origin/poc/backend-seam` and not on main. Its report states there is "no data-migration or import story" (§2.1–2.2), and it lists `transfer/` as out of scope.

**Commits on main since issue was filed:** none apart from this plan's own.

**Active plans in `docs/plans/` overlapping this area:** `sdlc-631.md` (backend seam) and the #755 plan being written in parallel. These are coordination points, not overlap: #755 owns the schema, this plan owns the data movement.

**Notes:**
- Valor runs popoto **1.9.0 from PyPI** (`ai/pyproject.toml:21`, pinned in `ai/uv.lock`). `transfer/export.py` is in 1.9.0. The `popoto-transfer` CLI (#626) and `preserve_keys=False` (#721) are not.
- The extractor therefore runs from popoto main in its own venv, not from Valor's venv. That is safe because the on-disk encoding (msgpack hash values) did not change between 1.9.0 and main. A build task asserts this with a decode fixture.

## Prior Art

- **#554 / PR #558**: generic export/import with per-field round-trip fidelity. Introduced the JSONL format and the per-field `roundtrip_policy` (`carry` / `partial` / `rebuild`). The extract stage reuses this.
- **#556 / PR #675**: round-trip carry for history-shaped state. Added confidence, access-tracker, validity and embedding carry. Without it the extract would have to hand-read companion structures.
- **#555 / PR #626**: the `popoto-transfer` CLI. It refuses DB 0 without `--allow-db0`, and it writes to `<out>.part` then calls `os.replace`. Both are reused as patterns.
- **#557 / PR #721**: key-regenerating import with reference remapping. **Not used.** The migration keeps `memory_id` as the identity, so nothing needs remapping. #721's two-pass spool is the model for how `superseded_by` would be remapped if identity ever changed.
- **#631 / PR #731 and the `poc/backend-seam` branch**: `PostgresBackend` on generic `bytea` tables. Its report records the decisions that bind #755 (typed tables, Postgres unit-of-work semantics). It also measured `save_record` at 4.9× Redis cost with an advisory lock per call. That is the reason the loader writes rows directly in one transaction rather than through `Model.save()`.
- **Valor `scripts/migrate_memory_project_key.py`** (in the ai repo): an earlier in-place Redis rewrite of `project_key`. It is the reason `project_key` values like `dm` and `default` can still appear in old records (see `_warn_if_legacy_namespace`). The loader carries them verbatim and the inventory reports them.

## Research

**Queries used:**
- "pgvector COPY binary bulk load vectors psycopg3 best practice create index after load"
- "redis SCAN consistency guarantees keys modified during iteration DUMP read-only replica snapshot"

**Key findings:**
- **Bulk-load vectors with binary `COPY` and build the HNSW index after the load** ([pgvector README](https://github.com/pgvector/pgvector), [pgvector-python loading example](https://github.com/pgvector/pgvector-python/blob/master/examples/loading/example.py)). Binary `COPY` needs `register_vector(conn)` and `copy.set_types([...,'vector'])`. The vector dimension is fixed in the column type.

  Effect on the plan: the loader `COPY`s into unindexed staging tables, then `INSERT … SELECT … ON CONFLICT` into the #755 tables. Vectors whose dimension differs from the column are routed to NULL plus a re-embed queue rather than failing the load.
- **`SCAN` gives no point-in-time snapshot, and a replica keeps applying writes.** Only an RDB file is a true point-in-time copy ([Redis SCAN docs](https://redis-doc-test.readthedocs.io/en/latest/commands/scan/), [Redis replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/), [DUMP](https://redis.io/docs/latest/commands/dump/)).

  Effect on the plan: extraction never runs against the live server. It runs against a throwaway `redis-server` loaded from a copied RDB file. That makes the source both consistent and disposable, so popoto read paths that write (orphan purge, access-tracker staging) cannot harm the live store.

## Spike Results

Each of these was a time-boxed code-read by an Explore agent. All read code only; nothing touched Redis.

### spike-1: Which Redis structures hold irreplaceable state?
- **Assumption**: "Decay scores, confidence and access tracking are all derivable from the model hash."
- **Method**: code-read (`src/popoto/fields/`, `models/`)
- **Finding**: False for two of the three.
  - `ConfidenceField` evidence (`confidence`, `evidence_count`, `corroborations`, `contradictions`) lives only in `$ConfidencF:Memory:confidence:data` (`fields/confidence_field.py:309-316`). The `confidence` attribute in the record hash is a mirror that can be stale.
  - AccessTracker `meta` and `access_log` exist only under `$AT:Memory:*` (`fields/access_tracker.py:199`).
  - The decay zset is derived: its score is the `relevance` float that is also stored in the hash, because `DecayingSortedField` forces `auto_now=True` (`fields/decaying_sorted_field.py:361-364`).
  - Vectors are `.npy` files under `$POPOTO_CONTENT_PATH/.embeddings/Memory/{sha256(R)}.npy` with an `_index.json` (`fields/embedding_field.py:224-236`). Redis stores only the dimension.
- **Confidence**: high
- **Impact on plan**: the inventory and mapping tables below. Extraction must carry the companion hash, the `$AT` keys and the embedding directory, not only `HGETALL` the records.

### spike-2: Is `popoto.transfer` export safe and sufficient as the extract stage?
- **Assumption**: "Transfer export is strictly read-only."
- **Method**: code-read (`src/popoto/transfer/`, `models/query.py`)
- **Finding**: Mostly, but not strictly.
  - Export hydrates through `Query.get_many_objects`. When any class-set member's hash is empty, that function calls `model._purge_orphan_keys(missing)`, which issues SREM/ZREM writes (`models/query.py:3736-3741`, `models/base.py:4026`).
  - It does not fire `_fire_on_read`, so it stages no access-tracker reads.
  - It binds only the global connection (no `client=`).
  - It enumerates with `SMEMBERS $Class:Memory`, so it misses orphan hashes that are absent from the class set.
  - It does carry confidence, access tracker (dropping staged reads) and embeddings (`.npy` bytes plus provenance).
- **Confidence**: high
- **Impact on plan**: reuse transfer export unchanged as the record extractor, but only against the throwaway snapshot server, where its orphan purge is harmless. Pair it with a separate raw inventory pass over a command-allowlisted client. That pass catches what `SMEMBERS` misses and accounts for every key family.

### spike-3: What writes Valor memory, and can it be frozen?
- **Assumption**: "Stopping the bridge freezes memory writes."
- **Method**: code-read (`/Users/valorengels/src/ai`)
- **Finding**: False. Writers include:
  - the bridge;
  - the worker, which runs the memory reflections: dedup/consolidation (daily), decay-prune, quality-audit, distill-backfill (every 300s), embedding-backfill and side-effect-drain (every 60s);
  - the memory MCP server;
  - the CLI tools;
  - a title-generator daemon thread;
  - every local Claude Code session through `.claude/hooks/hook_utils/memory_bridge.py`, which handles `ingest` and `confirm_access`.

  There is no global memory-write kill switch. The only one is `tools/improvement_eval/writer_guard.py`, an in-process eval monkeypatch. Each machine has its own Redis (`ai/docs/features/redis-durability.md:167`).
- **Confidence**: high
- **Impact on plan**: the runbook freezes by stopping processes and closing sessions, then detects late writes rather than trusting the freeze. Re-running the idempotent loader against a newer snapshot is the delta mechanism. The migration runs once per machine.

### spike-4: Does a #755 schema exist to map against?
- **Method**: `gh pr list --state all --search 755`, plus the POC report §5 and §9.
- **Finding**: No. The only design guidance is POC §5: typed tables, a typed `importance`/`confidence` double, `tstzrange` or `valid_from`/`invalid_at` with a GiST index, NUL bytes refused, UTF-8 required.
- **Confidence**: high
- **Impact on plan**: the mapping is written against the logical model. The physical column list is a declared dependency, and every task that touches it is sequenced after #755's schema PR.

## Source Inventory

This inventory was built from code (popoto main at the baseline plus Valor `models/memory.py`), not from live data. `R` means a record key: `Memory:{agent_id}:{memory_id}:{project_key}`. KeyFields are ordered alphabetically, with `:` inside a value escaped as `{&#58;}` (`models/base.py:765`, `models/db_key.py:43`).

Disposition codes:
- **CARRY**: authoritative state that exists nowhere else, so it must be migrated.
- **REBUILD**: derived from carried state; the #755 schema or Valor recomputes it.
- **DROP**: transient, or meaningless outside Redis.
- **ASSERT**: expected to be empty for Valor. The inventory stops the run if it is not.

### Memory model (popoto-managed, Redis DB 0)

| Structure | Key pattern | Type | Holds | Disposition |
|---|---|---|---|---|
| Record hash | `R` | hash, msgpack values | `memory_id`, `agent_id`, `project_key`, `content`, `title`, `importance`, `source`, `reference`, `metadata`, `superseded_by`, `superseded_by_rationale`, `relevance` (last-save Unix ts), `confidence` (mirror), `embedding` (dimension int), `bm25`/`bloom` (placeholders) | **CARRY** (except the mirror, dimension and placeholder attributes) |
| Confidence companion | `$ConfidencF:Memory:confidence:data`, hash field `R` | hash → msgpack `{confidence, evidence_count, corroborations, contradictions}` | Bayesian evidence | **CARRY.** Authoritative. The hash mirror is ignored. A missing entry maps to `initial_confidence=0.5`, `evidence_count=0`, and is counted in the report. |
| Access meta | `$AT:Memory:meta:{R}` | hash | `access_count`, `last_accessed` | **CARRY** |
| Access log | `$AT:Memory:access_log:{R}` | list (capped) | confirmed access timestamps | **CARRY** |
| Staged reads | `$AT:Memory:staged:{R}` | list, 24h TTL | unconfirmed reads | **DROP.** Lossy by contract, the same as transfer's `partial` policy, and bounded at 24h. Counted in the report. |
| Embedding vectors | **disk:** `$POPOTO_CONTENT_PATH` (default `~/.popoto/content`) `/.embeddings/Memory/{sha256(R)}.npy` plus `_index.json` | float32 `.npy` | vectors (current provider: OpenAI `text-embedding-3-small`, 1536-d, per `ai/agent/embedding_provider.py`; older records may be 768-d `nomic-embed-text`) | **CARRY** if the dimension matches the #755 column. Otherwise NULL plus re-embed. No provider name is stored, so the dimension is the only provenance. |
| Class set | `$Class:Memory` | set | every `R` | **REBUILD** (becomes the PK). Cross-checked against a `SCAN Memory:*` in the inventory. |
| KeyField indexes | `$KeyF:Memory:agent_id:{v}`, `$KeyF:Memory:project_key:{v}` | set | `R` by value | **REBUILD** (SQL indexes) |
| Decay index | `$DecayingSortF:Memory:relevance:{project_key}` | zset, score = `relevance` ts | ranking | **REBUILD.** Cross-checked: each zset score equals the hash's `relevance`. |
| BM25 postings | `$BM25:Memory:bm25:{tf:R, inv:term, df, dl, n, avgdl}` | zsets/strings | inverted index over `content` | **REBUILD** (#755's text-search choice) |
| Bloom | `$EF:Memory:bloom` | string bitmap | content-token bits, never cleared on delete | **REBUILD** or retire, per #755. Not carried: its bits include deleted records. |
| Write-filter priority | `$WF:Memory:priority` | zset | `R` → filter score at save time | **REBUILD** (a function of `importance`) |
| Tombstone prior | `$TOMBPRIOR:Memory:{burials,index,stats}` | hash/zset/hash | burial counts | **ASSERT empty.** Only popoto's `recipes/memory_lifecycle.py:868` records burials, and Valor does not use that recipe (grep of `ai/` found no caller). A non-empty value stops the run for a maintainer decision. |
| Legacy idxset pointers | hash fields containing `\x00`, `$IdxPtr:*` | — | `IndexedField`/`UniqueField` only | **ASSERT absent / DROP.** Memory has no such fields. The decoder already skips `\x00` fields (`models/encoding.py:612`). |
| Embedding invalidation | pubsub `popoto:embedding:invalidate:Memory` | channel | — | **DROP** (not state) |

### Valor-side Redis state outside the Memory model

| Structure | Key / model | Disposition |
|---|---|---|
| Content-gate counters | raw `INCR {project_key}:memory-gate:{reason}`, reasons `ack`/`fragment`/`short`/`fallback_dropped` (`ai/models/memory_gate.py:25-35`) | **CARRY (telemetry)** into a small counters table so the :8500 dashboard stays continuous. Optional; see Open Questions. |
| Distill-gate counters | raw `INCR {project_key}:memory-distill:{reason}` (`ai/models/memory_distill_gate.py:32-42`) | **CARRY (telemetry)**, same as the row above |
| `CorpusSizeBaseline` | popoto model (`ai/models/memory_corpus_baseline.py`) | **REBUILD.** A single row that the quality audit regenerates. |
| `KnowledgeDocument`, `DocumentChunk` | popoto models with their own embeddings | **Out of scope: re-index.** `tools/knowledge/indexer.py` derives them from files. The `source="knowledge"` *Memory* rows are carried as Memory rows. |
| `SideEffectJob(kind="memory_extraction")` | popoto model | **DRAIN before freeze.** Pending jobs would write Memory after the snapshot. |
| Session sidecars `data/sessions/{id}/memory_buffer.json` | filesystem | **DRAIN** (the outcome-resolve reflection consumes them) |

## Logical Field Mapping

The **logical** model is the set of facts that must survive. The **physical** column names are set by #755's schema PR and fill the right-hand column at build time (task `map-physical`). Every row below is stated as a fact plus a transformation, so the mapping holds whatever table shape #755 chooses.

| # | Logical fact | Source (Redis) | Transformation | Lossy? | #755 column |
|---|---|---|---|---|---|
| L1 | Identity | `memory_id` (uuid4 hex) | Verbatim. It is the PK, unique across machines with overwhelming probability. A collision aborts the load. | no | TBD |
| L2 | Author/agent scope | `agent_id` | verbatim | no | TBD |
| L3 | Project scope | `project_key` | Verbatim, including legacy `dm`/`default` values, which the report lists | no | TBD |
| L4 | Content, title | `content`, `title` | Text. A NUL byte rejects the record; Postgres `text` refuses NUL (POC Q4). | Records with NUL bytes are rejected and reported. The default `--max-rejects 0` aborts the load. | TBD |
| L5 | Importance | `importance` | float8 | no | TBD |
| L6 | Source kind | `source` | Text, or an enum if #755 picks one. Any value outside `{human, agent, system, knowledge}` is reported. | no | TBD |
| L7 | Reference pointer | `reference` (JSON string or `""`) | Becomes jsonb if it parses, NULL if `""`, otherwise kept as raw text (counted) | no | TBD |
| L8 | Free metadata | `metadata` dict | jsonb, verbatim. Typed promotion of known keys is optional and #755's call (rows L9–L11). | no | TBD |
| L9 | Tags and category (scoping) | `metadata.tags`, `metadata.category` | Promote to #755's optional tag-scoping column if one exists; otherwise leave in jsonb | no | TBD |
| L10 | Outcome state | `metadata.dismissal_count`, `metadata.last_outcome` | Typed columns, or kept in jsonb | no | TBD |
| L11 | Outcome history | `metadata.outcome_history[]` (≤10 entries: `outcome`, `reasoning`, `ts`) | One row per entry if #755 has an outcomes table, otherwise jsonb | History beyond the 10-entry cap was already gone in Redis | TBD |
| L12 | Distillation status | `metadata.distill_*` (status, attempts, last_attempt_at, model, prompt_version, failed/refused/abandoned) | Kept in jsonb unless #755 promotes it | no | TBD |
| L13 | Decay anchor | hash `relevance` (last-save ts) | `timestamptz`. The zset score is used only as a cross-check. | no | TBD |
| L14 | Creation time | **does not exist.** uuid4 ids carry no time and the model has no `created_at`. | Estimate as `min(access_log[0], outcome_history[*].ts, relevance)` and set an `created_at_estimated` provenance flag | **yes, documented** | TBD |
| L15 | Confidence | companion hash `confidence` | float8 | no | TBD |
| L16 | Confidence evidence | companion `evidence_count`, `corroborations`, `contradictions` | ints | no | TBD |
| L17 | Access stats | `$AT meta` `access_count`, `last_accessed` | int, timestamptz | no | TBD |
| L18 | Access log | `$AT access_log` | Rows, or a `timestamptz[]` array | staged (unconfirmed, <24h) reads are dropped | TBD |
| L19 | Embedding | `.npy` file | `vector(N)` if the dimension equals N. Otherwise NULL, reported per dimension, and left for Valor's embedding backfill to regenerate. | Re-embed cost only. Wrong-dimension vectors cannot be loaded at all. | TBD |
| L20 | Supersession: replacement | `superseded_by` when it equals an existing `memory_id` | FK to the replacement row | no | TBD |
| L21 | Retirement reason | `superseded_by` when it is a sentinel: `dismissal-prune` (`ai/agent/memory_extraction.py:1513`), `decay-prune-tier2` (`ai/reflections/memory/memory_decay_prune.py:146`), `cleanup-junk-extraction` (`ai/reflections/memory/memory_quality_audit.py:60`) | Retirement-reason column or enum. Any other non-id, non-sentinel value (for example a dangling id whose replacement was hard-deleted) is carried as raw text and counted. | no | TBD |
| L22 | Supersession rationale | `superseded_by_rationale` | text | no | TBD |
| L23 | Validity interval | **does not exist as a timestamp.** Supersession time was never recorded. | `tstzrange(created_at_est, NULL)` for active records. For superseded records the upper bound is estimated as `relevance` (the timestamp of the last save, which is at or after the supersession save) and flagged. | **yes, documented**: the upper bound is approximate | TBD |
| L24 | Machine provenance | none (implicit) | `migrated_from` = hostname + snapshot id + run id on every row. This is also the idempotency guard (see Solution). | n/a | TBD |
| L25 | Gate/distill counters | raw counter keys | `(project_key, gate, reason, count, migrated_at)` | no | TBD |

Lossy cases, all named in the run report and accepted by design:
- **L4**: content containing NUL bytes is rejected, not silently stripped.
- **L14, L23**: creation time and supersession time are estimated, and each estimate is flagged.
- **L18**: staged reads are dropped.
- **L19**: wrong-dimension vectors are re-embedded.
- **Bloom bits** are not carried.

The run report exists so these cases are counted, never discovered later.

## Data Flow

1. **Freeze (operator)**: stop Valor's writers on the machine; see the Cutover Runbook.
2. **Snapshot (operator, EXTERNAL)**: the operator runs `BGSAVE` on the live Redis and waits for `LASTSAVE` to advance, then copies `dump.rdb` and `$POPOTO_CONTENT_PATH/.embeddings/Memory/` into a run directory. This is the only operation ever performed against the live server. It does not mutate the dataset, and it is the operator's command, not the tool's.
3. **`serve-snapshot` (tool)**: starts a throwaway `redis-server` on an ephemeral port bound to 127.0.0.1, with `--dir <rundir> --dbfilename dump.rdb --save "" --appendonly no`. It records the server's `run_id` and port in `<rundir>/source.json`. Every later stage connects only through this record.
4. **`inventory` (tool)**: connects through an allowlisted read-only client (`SCAN`, `TYPE`, `SMEMBERS`, `SCARD`, `HGETALL`, `HGET`, `HLEN`, `LRANGE`, `LLEN`, `ZRANGE`, `ZSCORE`, `ZCARD`, `GET`, `STRLEN`, `PTTL`, `INFO`, `DBSIZE`). It counts every key family in the Source Inventory, cross-checks the class set against `SCAN Memory:*` and the decay-zset scores against the hash `relevance` values, and asserts the ASSERT rows. Output: `inventory.json`.
5. **`extract` (tool)**: runs `popoto.transfer.export` with a mirror `Memory` model against the throwaway server, with `POPOTO_CONTENT_PATH` set to the copied embeddings directory. It also collects any orphan hashes the inventory found, and the counter keys. Output: `memory.jsonl` (manifest plus records) and `counters.jsonl`, each with a sha256 recorded in `artifact.json`. After this the Redis side is finished and the throwaway server is stopped.
6. **`transform` (tool, pure)**: reads JSONL and applies L1–L25: supersession split, created-at estimate, dimension routing, NUL rejection. Output: `rows.jsonl` (the logical tuples) plus `report.json` (every lossy count). It needs no network or database and can run before #755 lands.
7. **`load` (tool, after #755)**: in one Postgres transaction, binary-`COPY`s rows into a `migration_756` staging schema, then runs `INSERT … ON CONFLICT (id) DO UPDATE … WHERE target.migrated_from IS NOT NULL` into the #755 tables, records the run in `migration_756.runs`, and commits. In `--dry-run` mode it rolls back instead. Indexes (HNSW, text search) are built or `REINDEX`ed after the load.
8. **`verify` (tool)**:
   - (a) row counts per `(project_key, source, active/superseded, vector dimension)` against `report.json`.
   - (b) a per-record logical checksum over every row: sha256 of the canonical tuple from `rows.jsonl` against the same tuple re-read from Postgres. It must be 100% equal.
   - (c) N random records printed side by side for human review.
   - (d) retrieval parity (see Solution).
9. **Repoint and resume (operator, Valor-side)**.

## Architectural Impact

- **New dependencies**: `psycopg[binary]>=3` and `pgvector` (the Python package) for the loader, in a tool-local optional group. They are not added to popoto's runtime dependencies or published extras. The source side needs a `redis-server` binary, the same one the machine already runs.
- **Interface changes**: none to popoto's public API. `popoto.transfer.export` is reused unmodified. If the build finds an additive change is needed, such as an explicit `content_path=`, it becomes its own PR with tests and no behaviour change.
- **Coupling**: the tool depends on the #755 schema (loader only) and on a mirror of Valor's `Memory` field list. A preflight compares the mirror against `ai/models/memory.py` by AST, read-only, so the two cannot drift silently.
- **Data ownership**: after cutover, Postgres is authoritative for Valor memory on that machine. Redis memory keys are left untouched as a cold archive. Deleting them is a separate decision (No-Gos).
- **Reversibility**: full until writers resume, because Redis was never written. After resume, rolling back loses the memories written on Postgres since then. A reverse migration is deliberately not built (No-Gos).
- **Location**: `tools/memory_migration/` at the repo root, outside `src/`. It is never in the wheel. It is also not in the sdist: setuptools' default sdist membership does not pick up an arbitrary top-level directory, and the build verifies this with `scripts/check_sdist_contents.py`. That placement makes the "one-off, not a runtime feature" decision structural.

## Appetite

**Size:** Medium

**Team:** Solo dev (builder plus validator pairs), with the maintainer as operator for the cutover

**Interactions:**
- PM check-ins: 1–2 (the Open Questions; the go/no-go on the parity threshold)
- Review rounds: 1 (the loader against #755's schema)

The pre-#755 half (snapshot, inventory, extract, transform) is about 60% of the work and can ship first. The loader, verification and the rehearsal follow once #755's schema PR merges.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `redis-server` binary present (same major as the live server) | `redis-server --version` | Throwaway snapshot server |
| Test Redis on DB 15 for unit fixtures | `redis-cli -n 15 ping` | Fixture snapshots (tests never touch DB 0) |
| #755 schema merged (loader, verify, rehearsal only) | `gh pr list --state merged --search "755 schema" --json number -q 'length'` | Physical mapping target |
| Postgres with pgvector available for loader tests | `python -c "import os,psycopg; psycopg.connect(os.environ['POSTGRES_TEST_URL']).execute('select extversion from pg_extension where extname=%s',('vector',)).fetchone()[0]"` | Loader and verify tests (skipped when unset) |

## Solution
TBD

## Cutover Runbook
TBD

## Failure Path Test Strategy
TBD

## Test Impact
TBD

## Rabbit Holes
TBD

## Risks
TBD

## Race Conditions
TBD

## No-Gos (Out of Scope)
TBD

## Update System
TBD

## Agent Integration
TBD

## Documentation
TBD

## Success Criteria
TBD

## Team Orchestration
TBD

## Step by Step Tasks
TBD

## Verification
TBD

## Critique Results

## Open Questions
TBD
