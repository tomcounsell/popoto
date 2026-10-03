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
TBD

## Logical Field Mapping
TBD

## Data Flow
TBD

## Architectural Impact
TBD

## Appetite
TBD

## Prerequisites
TBD

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
