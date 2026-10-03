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

The **logical** model is the set of facts that must survive. The **physical** columns come from #755's concrete schema: PR #758, `docs/plans/postgres_native_memory.md` §D3, table `popoto.memory`, schema `popoto`, DSN from `POPOTO_POSTGRES_URL`. They were aligned on 2026-10-03, while #758 was still open. Task `map-physical` re-checks this column against the DDL as merged, and the DDL pin test in #755 is the source of truth if the two differ.

| # | Logical fact | Source (Redis) | Transformation | Lossy? | `popoto.memory` column (#758 §D3) |
|---|---|---|---|---|---|
| L1 | Identity | `memory_id` (uuid4 hex) | Verbatim. Must match `CHECK ^[0-9a-f]{32}$`; a violation is a reject. A PK collision against another machine's run aborts the load. | no | `memory_id text PK` |
| L2 | Author/agent scope | `agent_id` | verbatim | no | `agent_id` |
| L3 | Project scope | `project_key` | Verbatim, including legacy `dm`/`default` values, which the report lists. This is #758's scope column (`partition_by`). | no | `project_key` |
| L4 | Content, title | `content`, `title` | Text. A NUL byte rejects the record; Postgres `text` refuses NUL (POC Q4). | Records with NUL bytes are rejected and reported. The default `--max-rejects 0` aborts the load. | `content`, `title` |
| L5 | Importance | `importance` | float8 | no | `importance` |
| L6 | Source kind | `source` | Text. Any value outside `{human, agent, system, knowledge}` is reported. | no | `source` |
| L7 | Reference pointer | `reference` (JSON string or `""`) | Verbatim text, because #758 keeps it `text NOT NULL DEFAULT ''` | no | `reference` |
| L8 | Free metadata | `metadata` dict | jsonb, verbatim. Tagged non-JSON values from the transfer format are converted, or the record is rejected; see Failure Path. | no | `metadata` |
| L9 | Tags and category | `metadata.tags`, `metadata.category` | Stay in jsonb; #758 promotes neither | no | `metadata` |
| L10 | Outcome state | `metadata.dismissal_count`, `metadata.last_outcome` | Stay in jsonb | no | `metadata` |
| L11 | Outcome history | `metadata.outcome_history[]` (≤10 entries) | Stays in jsonb | History beyond the 10-entry cap was already gone in Redis | `metadata` |
| L12 | Distillation status | `metadata.distill_*` | Stays in jsonb | no | `metadata` |
| L13 | Decay anchor | hash `relevance` (last-save Unix ts) | `to_timestamp()`. The zset score is used only as a cross-check. | no | `relevance timestamptz` |
| L14 | Creation time | **does not exist.** uuid4 ids carry no time and the model has no `created_at`. | Estimate as `min(access_log[*], outcome_history[*].ts, relevance)`, and add `created_at` to `estimated_fields` | **yes, flagged** | `created_at` + `estimated_fields` |
| L15 | Confidence | companion hash `confidence` | float8, within `CHECK 0..1` | no | `confidence` |
| L16 | Confidence evidence | companion `evidence_count`, `corroborations`, `contradictions` | ints | no | `confidence_evidence`, `confidence_corroborations`, `confidence_contradictions` |
| L17 | Access stats | `$AT meta` `access_count`, `last_accessed` | int, timestamptz | no | `access_count`, `last_accessed_at` |
| L18 | Access log | `$AT access_log` | **Not loaded.** #758 drops it because nothing in Valor reads it. It is used only as an input to the L14 estimate, and stays in the run-directory archive. | **yes, by schema decision** | none |
| L19 | Staged reads | `$AT staged` list | `LLEN` → `staged_reads`, the latest timestamp → `staged_at`. Read by the raw inventory, because transfer export drops staged reads. | no | `staged_reads`, `staged_at` |
| L20 | Embedding | `.npy` file (1536-d current; 768-d legacy) | If 1536-d: `vector(1536)`, `embedding_model = 'openai:text-embedding-3-small'` (the only 1536-d provider Valor has used; added to `estimated_fields`), and `embedded_hash = md5(content)`. Any other dimension → NULL, which #758's D7 backfill re-embeds. | Re-embed cost only | `embedding`, `embedding_model`, `embedded_hash` |
| L21 | Supersession: replacement | `superseded_by` when it is a 32-hex id, whether or not the replacement still exists (#758 has no FK, by design) | verbatim | no | `superseded_by` |
| L22 | Retirement reason | `superseded_by` when it is a sentinel: `dismissal-prune` (`ai/agent/memory_extraction.py:1513`), `decay-prune-tier2` (`ai/reflections/memory/memory_decay_prune.py:146`), `cleanup-junk-extraction` (`ai/reflections/memory/memory_quality_audit.py:60`) | Move to `retired_reason`, set `superseded_by` NULL. Any other non-empty value is a reject that names the value. | no | `retired_reason` |
| L23 | Supersession rationale | `superseded_by_rationale` | text | no | `superseded_by_rationale` |
| L24 | Supersession time | **does not exist** | For superseded or retired records: estimate as `relevance` (the timestamp of the last save, which is at or after the supersession save), and add `superseded_at` to `estimated_fields`. NULL for active records. | **yes, flagged** | `superseded_at` + `estimated_fields` |
| L25 | Machine provenance | none (implicit) | `{machine, snapshot, run_id}`. This is also the delta guard. | n/a | `migrated_from jsonb` |
| L26 | Derived lexical/bloom state | BM25 postings, bloom bits | **Not loaded.** Rebuilt by `python -m popoto.pg reindex` after the load. | no | `bm25_len`, `popoto.memory__bm25`, `popoto.memory__bloom` |
| L27 | Gate/distill counters | raw `{pk}:memory-gate:*`, `{pk}:memory-distill:*` | **Not migrated.** #758 classes them as Valor app state that stays in Redis. Counted in the inventory for the record. | no (they stay put) | none |
| — | Write-filter priority | `$WF:Memory:priority` | **Not loaded.** #758: no reader, and the priority tier is a no-op on pg. | no | none |

Lossy cases, all named in the run report and accepted by design:
- **L4**: content with NUL bytes is rejected, not silently stripped.
- **L14, L24**: creation time and supersession time are estimated and flagged in `estimated_fields`.
- **L18**: the access log is dropped, by #758's schema decision.
- **L20**: wrong-dimension vectors are re-embedded.
- **Bloom bits** are not carried; they are rebuilt from content.

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

### Key Elements

- **Snapshot isolation (the safety model)**: the tool cannot reach the live store. It connects only to a throwaway `redis-server` it started itself from a copied RDB, identified by `run_id` in `source.json`. It has no code path that accepts a URL, port or host for the source. That makes "read-only against Redis, never flush" a structural property rather than a discipline. It also neutralizes the popoto read paths that write (spike-2), because those writes land in a disposable process.
- **Allowlisted client**: every Redis call goes through a wrapper that permits only the read commands listed in Data Flow step 4. Any other command raises `ForbiddenCommand` before it reaches the socket. This is defense in depth on top of isolation, and it sits outside popoto's own DB-0 flush guard. It applies to the inventory and to counter extraction. Transfer export uses popoto's global client, bound by `REDIS_URL` set before `import popoto` (CLAUDE.md, #577) to the throwaway URL. The tool asserts that binding by comparing `run_id` before export starts.
- **Reused extractor**: `popoto.transfer.export`, run against a mirror `Memory` model. The mirror has the same class name, field names and field types as Valor's model, with `GracefulEmbeddingField` replaced by plain `EmbeddingField`, whose storage is identical (`ai/models/graceful_embedding_field.py` overrides only `on_save`). Using a mirror means the tool never imports Valor's runtime: no `apply_defaults()` and no OpenAI provider configuration. A preflight compares the mirror against Valor's model by AST, read-only.
- **Pure transform**: JSONL to logical tuples (L1–L25) plus a lossy-case report. It is deterministic, so the same input always yields byte-identical `rows.jsonl`.
- **Idempotent loader**: one transaction per run.
  - The PK is `memory_id`.
  - Every migrated row carries `migrated_from` (hostname, snapshot id, run id).
  - `ON CONFLICT DO UPDATE` fires only where `target.migrated_from IS NOT NULL`, so a row Valor has since created or edited natively on Postgres is never overwritten. Those rows are reported as `conflict_native`.
  - A run ledger (`migration_756.runs`) stores the artifact sha256 values. Re-running the same artifact is a verified no-op. Re-running a newer snapshot is the delta mechanism.
  - `--dry-run` runs the full load and verification inside the transaction, then rolls back.
- **Three-level verification**:
  1. Counts.
  2. A logical checksum over **every** record (not a sample). At a 20k-record scale this costs seconds.
  3. Retrieval parity on a query sample.

  The tool exits non-zero on any mismatch in levels 1–2. Level 3 is a go/no-go input with pinned thresholds.
- **Retrieval parity**: there is no real-query log in Valor; analytics records only hit counts (`ai/agent/memory_retrieval.py:443,469`). The sample has two parts:
  - (a) the 100 most recent `source="human"` memory contents. These are literally the user prompts that `memory_bridge.prefetch` recalls against.
  - (b) 100 known-item queries from Valor's own `tools/memory_eval/query_set.py:143`.

  The same queries run through Valor's `retrieve_memories` twice: against a *second* throwaway server loaded from the same snapshot, and against the Postgres store, back to back. Metrics:
  - known-item hit@10, which must not drop by more than `PARITY_HIT_DROP_MAX`;
  - mean overlap@10, which must be at least `PARITY_OVERLAP_MIN`.

  Exact equality is not expected, because #755 replaces popoto BM25 with a Postgres text-search ranking. The thresholds are pinned constants (magic numbers per CLAUDE.md), starting at 0.02 and 0.6.

### Flow

Operator freezes writers → `BGSAVE` and copy the RDB plus embeddings → `serve-snapshot` → `inventory` (stop if an ASSERT fails) → `extract` → `transform` (report lossy cases) → `load --dry-run` (review the report) → `load` → `verify` (counts plus checksum: must pass) → `parity` (go/no-go) → operator repoints Valor → resume writers → late-write check → optional delta re-run.

### Technical Approach

- **CLI shape**: `python -m tools.memory_migration <stage> --run-dir <dir>`, with stages `serve-snapshot | inventory | extract | transform | load | verify | parity | stop-snapshot`. Each stage reads its predecessor's artifacts from the run directory and refuses to run if they are missing or their sha256 does not match `artifact.json`. That makes stages individually re-runnable, and makes the run directory the complete audit record.
- **Run directory**: lives in the operator's chosen archive location, never in the repo. It holds `dump.rdb`, `embeddings/`, `source.json`, `inventory.json`, `memory.jsonl`, `counters.jsonl`, `rows.jsonl`, `report.json`, `load.json` and `verify.json`. It is retained after cutover as the cold archive.
- **Transfer reuse**: call `popoto.transfer.export.export_records` (the library API, not the CLI) for the mirror model, with chunked hydration. Carry the `.npy` bytes through the existing embedding carry. If a gap needs a popoto change (for example, the export path reads `POPOTO_CONTENT_PATH` at import time instead of per call), it is an additive popoto PR with its own tests. The tool sets the environment before importing popoto either way.
- **Orphan hashes**: `Memory:*` hashes that `SCAN` finds but `$Class:Memory` lacks are decoded by the inventory through `popoto.models.encoding` (a pure function) and appended to `memory.jsonl` with `orphan: true`. They load like any other record, because they are real memories that popoto's index simply lost. Their count is reported.
- **Loader**:
  1. `psycopg` 3 binary `COPY` into `migration_756.stage_*`, which is unindexed.
  2. One `INSERT … SELECT … ON CONFLICT` per target table.
  3. HNSW and text-search index build or `REINDEX` after the load, with `maintenance_work_mem` raised for the session.

  The staging schema is dropped at the end of a successful non-dry run. It is created and destroyed inside the transaction, so a crash leaves nothing behind.
- **Encoding compatibility**: Valor writes with popoto 1.9.0 and the tool reads with main. A fixture test encodes records with the 1.9.0 encoder semantics (vendored msgpack fixtures captured on DB 15) and decodes them with main.
- **What can start before #755** (tasks 1–5): `serve-snapshot`, the allowlisted client, `inventory`, `extract`, `transform`, the report, and the mirror drift preflight. **What waits for #755** (tasks 6–9): `map-physical`, `load`, `verify`, `parity` and the runbook rehearsal.

## Cutover Runbook

This runs once per machine (each machine has its own Redis). The operator is the maintainer. Each step names its rollback.

**T-1 day: rehearsal.** Run the whole pipeline against a fresh snapshot without freezing anything, loading into a scratch Postgres database. Review `report.json` and the parity result. This sizes the downtime window, which is expected to be minutes at 20k records, dominated by index build and parity. Nothing goes live.

**T-0:**
1. **Drain.** Let `side-effect-drain` empty the `SideEffectJob(kind=memory_extraction)` queue, and let `memory-outcome-resolve` consume the session sidecars. Check the queue is empty from the Valor dashboard (:8500).
2. **Freeze.** Close every Claude Code session on the machine; the hooks are memory writers. Then stop the bridge, the worker (which runs all memory reflections plus the title daemon's host process) and the memory MCP server, using Valor's service manager. *Rollback: restart them.*
3. **Snapshot.** Run `redis-cli BGSAVE` and poll `LASTSAVE` until it advances. Record `INFO persistence` → `rdb_changes_since_last_save` (it should read 0 immediately after). Copy `dump.rdb` and `~/.popoto/content/.embeddings/Memory/` (or `$POPOTO_CONTENT_PATH`) into the run directory. These are the only commands issued to the live server, and none of them mutates the dataset. *Rollback: none needed.*
4. **Migrate.** `serve-snapshot`, then `inventory`, `extract`, `transform`, `load --dry-run`, review, `load`.
5. **Verify.** `verify` must exit 0. Then run `parity` and make a go/no-go decision against the thresholds. *No-go rollback: restart the writers on Redis unchanged. Postgres rows can be left (they are inert) or truncated by `migrated_from` run id.*
6. **Repoint.** Switch Valor's memory backend configuration to Postgres. This is a Valor-side change delivered by Valor's adoption of #755; see No-Gos [ORDERED].
7. **Resume.** Restart the writers. Run `stop-snapshot`.
8. **Late-write check.** Compare `rdb_changes_since_last_save` on the live server with the value recorded at step 3. It counts *all* DB writes, including non-memory Valor models, so a non-zero value is a prompt, not a verdict. If it is non-zero, take a second snapshot, re-run inventory/extract/transform, and diff `rows.jsonl` against run 1. Any memory record that changed or appeared after the snapshot is loaded by a delta `load`, which the `migrated_from` guard keeps from touching Postgres-native rows. Expected result: zero memory deltas.
9. **Archive.** Keep the run directory (RDB, embeddings, JSONL, reports). Leave the Redis memory keys in place. Their removal is a separate, later decision.

**After step 7, rollback costs data.** Memories written on Postgres after resume would not exist in Redis. The window between steps 5 and 7 is where the go/no-go belongs, and the runbook does not let the operator skip it.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The allowlisted client raises `ForbiddenCommand` for every write and admin command: `SET`, `HSET`, `DEL`, `SREM`, `ZREM`, `FLUSHDB`, `FLUSHALL`, `EXPIRE`, `RPUSH`, `CONFIG`, `SHUTDOWN`, `EVAL`/`EVALSHA`. A parametrized test covers each one, and asserts the command never reached the server by checking `INFO commandstats` on the DB-15 fixture server.
- [ ] Each stage refuses a missing or checksum-mismatched predecessor artifact, with a named error and non-zero exit. No stage swallows an exception. The tool has no `except Exception: pass`; the build's review checks this.
- [ ] `extract` aborts when the throwaway server's `run_id` differs from `source.json`. It also aborts when popoto's resolved client is not the throwaway server, which guards against a `REDIS_URL` that was not set before import.
- [ ] `load` aborts the whole transaction on a PK collision against a row from another machine's run. The report names the colliding `memory_id`.

### Empty/Invalid Input Handling
- [ ] An empty snapshot (zero Memory records) produces a valid empty artifact. `load` then makes a no-op run ledger entry, and `verify` passes on zero, but only with `--allow-empty`. Without the flag, zero records is an error, because an empty source on a production machine almost certainly means the wrong RDB.
- [ ] Records with: `content` containing NUL; non-JSON `reference`; `metadata` holding a non-JSON type (the transfer format's tagged bytes or datetime); a missing confidence companion entry; a missing `.npy` file for a record whose hash says it has a vector; a wrong-dimension vector; dangling or sentinel `superseded_by`; and an orphan hash. Each case has a fixture record and an asserted line in the report.
- [ ] An ASSERT-row violation (a non-empty `$TOMBPRIOR`, or an idxset pointer present) stops `inventory` with a non-zero exit.

### Error State Rendering
- [ ] `report.json` and the stage's stdout summary both name every non-zero lossy counter. A test asserts the summary text for a fixture snapshot that triggers every lossy case.
- [ ] `verify` prints the first 20 mismatching `memory_id`s together with the differing logical fields, not just a count.

## Test Impact

No existing tests affected. The tool is additive and lives outside `src/`: it imports `popoto.transfer` and `popoto.models.encoding` read-only and modifies neither. New tests:
- `tests/test_memory_migration_safety.py`: allowlist, `run_id` binding, the anti-live guards.
- `tests/test_memory_migration_extract.py`: an inventory and extract round-trip on fixture snapshots built on DB 15, then saved to an RDB and served by `serve-snapshot`.
- `tests/test_memory_migration_transform.py`: pure tests over L1–L25 and the lossy cases.
- `tests/test_memory_migration_load.py`: loader idempotency, dry-run rollback, the `migrated_from` guard, verify. Skipped unless `POSTGRES_TEST_URL` is set.

If a transfer gap forces an additive popoto change, that PR carries its own tests in `tests/test_transfer_*.py`.

## Rabbit Holes

- **Making the tool generic** ("migrate any popoto model to any backend"). The maintainer scoped it one-off for Valor's `Memory`. Transfer is already the generic layer, and the mirror model is the only Valor-specific part.
- **Live-source mode** (reading the live server directly, "just once, carefully"). Snapshot isolation is the safety model. A live mode would reopen every risk spike-2 found.
- **Dual-write, read-through or reverse migration.** These are ruled out by the 2026-10-03 decision. The pre-resume go/no-go window makes a reverse path unnecessary.
- **Reconstructing true creation and supersession times** from Valor logs or transcripts. The estimates are flagged and good enough for decay. Log mining is the #493 class of problem (blocked).
- **Re-embedding inside the tool.** Valor's embedding backfill reflection already regenerates NULL vectors after cutover. The tool only routes and reports them.
- **Byte-exact retrieval parity.** The ranking implementation changes on purpose. Parity is a regression check with thresholds, not an equality.
- **Carrying the bloom filter bits.** They include deleted records and are rebuilt or retired by #755.

## Risks

### Risk 1: #755's schema shape forces mapping rework
**Impact:** the loader and parts of the transform are rewritten.
**Mitigation:** the transform emits *logical* tuples (L1–L25). Only `map-physical` and `load` touch column names, and both are sequenced after #755's schema PR merges. If #755 changes after this plan, only the right-hand column of the mapping table and one module change.

### Risk 2: a write lands after the snapshot and is lost
**Impact:** memories written between the snapshot and the repoint never reach Postgres.
**Mitigation:** the freeze stops every known writer class (spike-3). Runbook step 8 detects late writes, and a delta re-run recovers them through the idempotent loader.

### Risk 3: the extractor accidentally binds the live store
**Impact:** writes to a live agent store. This is the #577 failure class.
**Mitigation:** there is no source-URL parameter. `REDIS_URL` is set by the tool from `source.json` before `import popoto`, and the `run_id` is asserted before any read. The allowlist covers raw access. An anti-criterion grep forbids `6379` and `DEFAULT_URL` in the tool source.

### Risk 4: the embedding directory does not match the RDB
**Impact:** vectors are missing, or come from another time.
**Mitigation:** copy both while frozen, in the same runbook step. The inventory checks that every hash-declared vector has its `.npy` and that `_index.json` maps to an existing `R`, and reports both directions of mismatch.

### Risk 5: encoding drift between popoto 1.9.0 (Valor) and main (tool)
**Impact:** values are misdecoded without any error.
**Mitigation:** the fixture test decodes 1.9.0-encoded records with main. The full-record logical checksum compares JSONL against Postgres, and the transform cross-checks the decoded `relevance` against the zset score, which is an independent encoding path.

## Race Conditions

### Race 1: writers active during snapshot
**Location:** runbook steps 2–3
**Trigger:** a Claude Code hook or reflection writes Memory after `BGSAVE` forks.
**Data prerequisite:** all writers stopped before `BGSAVE`.
**State prerequisite:** the side-effect queue and sidecars are drained.
**Mitigation:** the freeze ordering in the runbook, plus the step-8 late-write detection and delta re-run. `BGSAVE` itself is point-in-time (fork copy-on-write), so the snapshot is internally consistent even if the freeze leaks.

### Race 2: RDB and embeddings copied at different moments
**Location:** runbook step 3
**Trigger:** the embedding backfill writes `.npy` files after `BGSAVE`.
**Mitigation:** the worker is stopped first (step 2). The inventory's two-way vector reconciliation (Risk 4) catches any residue.

### Race 3: delta load against rows Valor already modified on Postgres
**Location:** the `load` stage, delta run
**Trigger:** Valor updates a migrated row on Postgres (for example, a new outcome), and then a delta load writes the older Redis version over it.
**Mitigation:** Valor's native writes clear `migrated_from` (or set it to NULL) on update. That is a requirement placed on #755's adoption code and listed in Open Questions. Until #755 confirms it, the loader also compares the row's `updated_at` against `migrated_at` and refuses to overwrite a newer row, reporting it as `conflict_native`.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #755] The Postgres schema, typed-table DDL, pgvector/text-search retrieval, and the Postgres-side embedding backfill and reflections. This plan consumes them.
- [ORDERED] Repointing Valor (runbook step 6) waits on Valor's adoption of #755 shipping in the ai repo. The tool can run and verify before that, but cannot finish a cutover.
- [EXTERNAL] Running the cutover on each real machine (freeze, `BGSAVE`, file copy, repoint). It needs the maintainer, on machines and live services the build agent must not touch.
- [DESTRUCTIVE] Deleting Valor's Redis memory keys or the run-directory archive after cutover. This is a separate, reviewed decision once Postgres has run cleanly.
- [ORDERED] Re-indexing `KnowledgeDocument`/`DocumentChunk` on Postgres. This is Valor's indexer, after the repoint.

## Update System

No update-system changes. The tool is run by hand from a popoto checkout, in a venv with the tool's optional dependencies, once per machine. It is not shipped through PyPI, not installed by `/update`, and not part of Valor's deploy.

## Agent Integration

No agent integration. This is an operator-run, one-off CLI. It deliberately has no MCP surface: an agent-callable migration against a live store is exactly the exposure the safety model rules out.

## Documentation

### Feature Documentation
- [ ] `tools/memory_migration/README.md`: the runbook (copied from this plan and kept current), the stage reference, artifact formats, and the lossy-case glossary.
- [ ] A short "Migrating Valor memory to Postgres" pointer page in `docs/features/`, linking to the README and #755's feature doc. Add it to the docs index.

### External Documentation Site
- [ ] `mkdocs build --strict` passes with the new page.

### Inline Documentation
- [ ] Module docstrings on the allowlisted client and `serve-snapshot` state the safety model and why there is no source-URL parameter.

## Success Criteria

- [ ] The tool's source has no code path that connects to a caller-supplied Redis URL, host or port. The anti-criteria greps pass.
- [ ] The allowlist test proves each forbidden command never reaches the server.
- [ ] On a fixture snapshot that exercises every Source Inventory row and every lossy case: inventory, extract, transform, load, verify complete end to end; `verify` reports 100% logical-checksum equality; and `report.json` matches the expected lossy counts exactly.
- [ ] Re-running `load` with the same artifact changes zero rows. `--dry-run` leaves Postgres byte-identical (same row counts and the same table checksums before and after).
- [ ] A delta run over a second snapshot updates only the changed records and never a Postgres-native row.
- [ ] Rehearsal on a real snapshot of the maintainer's machine, loaded into scratch Postgres: verify passes, and the parity result is recorded in the PR.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (source side)**
  - Name: source-builder
  - Role: `serve-snapshot`, the allowlisted client, `inventory`, `extract`, the mirror model and its drift preflight
  - Agent Type: builder (Domain: Redis/Popoto data, and security/untrusted input)
  - Resume: true
- **Builder (transform)**
  - Name: transform-builder
  - Role: the pure L1–L25 transform and the lossy report
  - Agent Type: builder
  - Resume: true
- **Builder (sink side)**
  - Name: sink-builder
  - Role: `map-physical`, `load`, `verify`, `parity` (after #755)
  - Agent Type: builder
  - Resume: true
- **Validator**
  - Name: migration-validator
  - Role: safety anti-criteria, fixture end-to-end runs, idempotency
  - Agent Type: validator
  - Resume: true
- **Documentarian**
  - Name: migration-docs
  - Role: the README runbook and the feature pointer page
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Safety core
- **Task ID**: build-safety
- **Depends On**: none
- **Validates**: tests/test_memory_migration_safety.py (create)
- **Informed By**: spike-2 (popoto reads can write), Research (an RDB is the only point-in-time copy)
- **Assigned To**: source-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `tools/memory_migration/` with `snapshot.py`. `serve-snapshot` spawns `redis-server` on an ephemeral 127.0.0.1 port with `--save "" --appendonly no` from `<rundir>/dump.rdb`, writes `source.json` (port, pid, run_id), and `stop-snapshot` stops it.
- Create `client.py`: the command-allowlisted wrapper that raises `ForbiddenCommand` pre-send. Its only constructor is `from_source_json(rundir)`.
- Create `bind.py`: sets `REDIS_URL` from `source.json` before any popoto import, then asserts `popoto.get_redis()` reports the same `run_id`.

### 2. Inventory and mirror model
- **Task ID**: build-inventory
- **Depends On**: build-safety
- **Validates**: tests/test_memory_migration_extract.py (create)
- **Informed By**: spike-1 (key inventory)
- **Assigned To**: source-builder
- **Agent Type**: builder
- **Parallel**: false
- Write `mirror.py`: a `Memory` with Valor's field list, with plain `EmbeddingField` in place of `GracefulEmbeddingField`.
- Write `preflight.py`: an AST comparison against a given `models/memory.py` path, read-only.
- Write `inventory.py`: per-family counts for every Source Inventory row; the class-set vs `SCAN Memory:*` reconciliation; zset score vs hash `relevance`; two-way `.npy` reconciliation; the ASSERT rows; counter keys.
- Build fixture snapshots on DB 15: populate, `SAVE` to a temp dir via a dedicated fixture server rather than the shared DB-15 server, and serve. Cover every inventory row, including an orphan hash and a missing companion entry.

### 3. Extract
- **Task ID**: build-extract
- **Depends On**: build-inventory
- **Validates**: tests/test_memory_migration_extract.py
- **Assigned To**: source-builder
- **Agent Type**: builder
- **Parallel**: false
- Call `popoto.transfer.export_records` for the mirror against the bound throwaway server, with `POPOTO_CONTENT_PATH` set to `<rundir>/embeddings`. Append orphan records (decoded with `popoto.models.encoding`) and `counters.jsonl`. Write `artifact.json` sha256 values. Use `.part` then `os.replace`.
- Add the encoding-compatibility fixture (1.9.0-encoded hashes decoded by main).
- If a transfer gap requires a popoto change, split it into its own additive PR with tests. Do not patch transfer from inside the tool.

### 4. Transform and report
- **Task ID**: build-transform
- **Depends On**: build-extract (format only; can start from the JSONL spec in parallel)
- **Validates**: tests/test_memory_migration_transform.py (create)
- **Assigned To**: transform-builder
- **Agent Type**: builder
- **Parallel**: true
- Write `transform.py`: L1–L25 as pure functions to deterministic `rows.jsonl`. Includes the supersession split (id / sentinel / dangling), the created-at estimate with its flag, the validity estimate with its flag, dimension routing, NUL rejection, and reference parsing.
- Write `report.json` with every lossy counter, and add the stdout summary.

### 5. Validate pre-#755 half
- **Task ID**: validate-source
- **Depends On**: build-safety, build-inventory, build-extract, build-transform
- **Assigned To**: migration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification anti-criteria rows and the fixture end-to-end through `transform`. Confirm no test or tool code touches DB 0 or port 6379.
- This half is mergeable on its own, before #755.

### 6. Physical mapping (after #755 schema PR merges)
- **Task ID**: map-physical
- **Depends On**: validate-source, plus the merged #755 schema PR
- **Assigned To**: sink-builder
- **Agent Type**: builder
- **Parallel**: false
- Fill the "#755 column" side of the Logical Field Mapping table in this plan, and the matching `columns.py` in the tool. Confirm with #755 that native writes clear `migrated_from` (Race 3), or add the `updated_at` guard.

### 7. Load and verify
- **Task ID**: build-load
- **Depends On**: map-physical
- **Validates**: tests/test_memory_migration_load.py (create; skipped without `POSTGRES_TEST_URL`)
- **Informed By**: Research (binary COPY, index after load)
- **Assigned To**: sink-builder
- **Agent Type**: builder
- **Parallel**: false
- `load.py`: binary `COPY` into staging, then guarded upsert, run ledger, `--dry-run` rollback, index build afterwards.
- `verify.py`: counts, the full logical checksum, and side-by-side samples.

### 8. Parity harness
- **Task ID**: build-parity
- **Depends On**: build-load, and Valor's #755 adoption being available for the Postgres arm
- **Assigned To**: sink-builder
- **Agent Type**: builder
- **Parallel**: false
- `parity.py`: builds the query sample (recent human memories plus Valor's known-item generator). A driver runs Valor's `retrieve_memories` in Valor's venv against a second throwaway server and against Postgres. The tool compares hit@10 and overlap@10 against the pinned `PARITY_HIT_DROP_MAX` and `PARITY_OVERLAP_MIN`.

### 9. Validate sink half and rehearse
- **Task ID**: validate-sink
- **Depends On**: build-load, build-parity
- **Assigned To**: migration-validator
- **Agent Type**: validator
- **Parallel**: false
- Fixture end-to-end through `verify`. Idempotent re-run (zero rows changed). Dry-run leaves Postgres unchanged. Delta run touches only changed records and never native ones.
- The maintainer's rehearsal on a real snapshot into scratch Postgres is the [EXTERNAL] operator step. Its report is attached to the PR.

### 10. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-source (README skeleton), validate-sink (final)
- **Assigned To**: migration-docs
- **Agent Type**: documentarian
- **Parallel**: false
- `tools/memory_migration/README.md` (runbook plus stage reference) and the `docs/features/` pointer page with its index entry.

### 11. Final Validation
- **Task ID**: validate-all
- **Depends On**: all of the above
- **Assigned To**: migration-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all Verification rows and confirm the Success Criteria.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Migration tests pass | `pytest tests/test_memory_migration_safety.py tests/test_memory_migration_extract.py tests/test_memory_migration_transform.py -q` | exit code 0 |
| Loader tests pass (with Postgres) | `pytest tests/test_memory_migration_load.py -q` | exit code 0 |
| Format clean | `black --check tools/memory_migration tests/` | exit code 0 |
| Lint clean | `ruff check src/ tools/memory_migration` | exit code 0 |
| No live port in tool | `grep -rn "6379" tools/memory_migration --include=*.py \| wc -l` | match count == 0 |
| No default URL in tool | `grep -rn "DEFAULT_URL\|redis://localhost" tools/memory_migration --include=*.py \| wc -l` | match count == 0 |
| No source-URL CLI option | `grep -rnE "add_argument\(.*(url\|host\|port)" tools/memory_migration --include=*.py \| wc -l` | match count == 0 |
| No flush in tool | `grep -rniE "flushdb\|flushall" tools/memory_migration --include=*.py \| grep -v "FORBIDDEN\|ForbiddenCommand" \| wc -l` | match count == 0 |
| Tool not in wheel | `python -m build --wheel -o /tmp/w756 >/dev/null 2>&1 && unzip -l /tmp/w756/*.whl \| grep -c memory_migration` | match count == 0 |
| Sdist check still clean | `python -m build --sdist -o /tmp/s756 >/dev/null 2>&1 && python scripts/check_sdist_contents.py /tmp/s756/*.tar.gz` | exit code 0 |

## Critique Results

## Open Questions

1. **Target topology across machines.** Each machine has its own Redis today. Do all machines' memories go into **one central Postgres**, which matches the "central shared store with optional agent/project scoping" direction, or does each machine get its own? The plan supports both: `memory_id` is globally unique and every row carries `migrated_from`. But a central target makes the loader's cross-machine PK-collision check and the `project_key` namespace merge real concerns, so I'd like the answer before `map-physical`.
2. **Yudame.** Valor's repo has no Yudame memory model; "yudame" appears there only as a bot account name and in unrelated tooling. Does Yudame have a Redis memory store somewhere else that this tool must also cover, or is "Valor/Yudame" a single store in practice?
3. **Telemetry counters (L25).** Should the content-gate and distill-gate counters carry over so the :8500 dashboard stays continuous? They are cheap to carry, but they need a home in #755's schema. If #755 has none, the plan drops them and the dashboard restarts from zero.
4. **Lossy-estimate policy (L14, L23).** Is a flagged estimate acceptable for `created_at` and for the supersession end time? The alternative is to leave them NULL and let #755's decay and validity logic treat NULL explicitly. Estimates keep decay behaviour closest to today's, because decay uses the last-save timestamp either way.
5. **Write-freeze switch.** The runbook freezes by stopping processes and closing Claude Code sessions, because Valor has no global memory-write kill switch. Is that acceptable for a minutes-long window, or should a Valor-side `MEMORY_WRITES=off` flag land in the ai repo first? That would be an ai-repo change, outside this plan.
