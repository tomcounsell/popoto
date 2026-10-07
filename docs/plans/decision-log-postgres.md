---
status: Planning
type: feature
appetite: Medium
owner: Solo dev
created: 2026-10-07
tracking: https://github.com/tomcounsell/popoto/issues/811
last_comment_id:
---

# Decision Log on Postgres

Implementation branch: `feature/decision-log-postgres`.

## Problem

An agent host running on the Postgres backend cannot turn on auditable extraction. `SubconsciousMemory(auditable_extraction=AuditableExtractionConfig(...))` on a Postgres-bound model raises `BackendCapabilityError` at construction (`src/popoto/recipes/subconscious_memory.py:284-298`). The host therefore loses the per-candidate decision log, which is the only way to measure extraction precision and recall offline.

Everything else on the auditable path already runs on Postgres: candidates, verdicts, the provenance journal and the M4 resolution sidecar. The decision log is the one piece written directly against a Redis client.

**Current behavior:**
- Construction refuses. The refusal is pinned by `tests/postgres/test_postgres_recipes.py::test_the_auditable_extraction_path_is_refused_on_postgres` (line 593) and documented in `docs/features/postgres-backend.md` ("Not on Postgres yet", lines 843-848).
- `DecisionLog` (`src/popoto/extraction/decision_log.py`) holds `self._redis = redis_client or get_REDIS_DB()`. It relies on Redis in three places:
  - `TERMINAL_WRITE_LUA` does the guarded terminal write, the summary `HINCRBY`, and maintains the class set and index sets by hand.
  - `SET NX PX` plus a Lua compare-and-delete implement the assembly claim.
  - `EXISTS`, `SCAN` and `HGETALL` back the readers.

**Desired outcome:**
- On a Postgres-bound process, `SubconsciousMemory(auditable_extraction=...)` constructs.
- `extract_memories` runs the full flow (candidates, verdicts, decision rows, journal, `ExtractedFact`s) with **zero Redis commands**.
- Observable semantics match the Redis path, apart from one documented `turn_summary` divergence (see Solution).
- The Redis path stays byte-identical: same Lua, same keys, same command sequence.
- `DecisionLog`'s public signatures are unchanged.

## Freshness Check

**Baseline commit:** `c199b788` (origin/main at plan time)
**Issue filed at:** 2026-10-07T16:31:19Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/subconscious_memory.py:284-302` still holds. The `non_redis_backend(model_class)` check raises `BackendCapabilityError` (the raise is at lines 284-298), then `self._decision_log = DecisionLog()` is built.
- `src/popoto/extraction/decision_log.py` still holds:
  - `TERMINAL_WRITE_LUA` is at lines 237-282.
  - `acquire_claim` (line 552) uses `SET NX PX`.
  - `get`, `list_for_agent`, `list_pending` and `turn_summary` are at lines 955-1036.
- `docs/features/postgres-backend.md` ~840 still holds. The "Not on Postgres yet" paragraph is at lines 843-848.
- `tests/postgres/test_postgres_events.py:722` holds `_RedisRecorder`. A twin is in `tests/postgres/test_postgres_recipes.py` (~line 88).

**Cited sibling issues/PRs re-checked:**
- #562 (M3) is closed, and its PR #591 is merged. It defines the decision-log contract.
- #563 (M4) is closed, and its PR #622 is merged. It added `ResolutionLog`.
- #759 (Postgres umbrella) is open. Its M4b PR #782 added the refusal and ported the journal, leases and question queue.
- #807 / #806 are merged. They provide the DB-isolation harness this plan reuses.
- #568 (M9 retention) is open and out of scope.

**Commits on main since issue was filed (touching referenced files):** none. The issue was filed at `c199b788`.

**Active plans in `docs/plans/` overlapping this area:** none. `docs/plans/auditable_extraction_m3.md` is the shipped M3 plan, kept for history.

**Notes:** The same `postgres-backend.md` paragraph also says MemoryTelemetry's `Meta.ttl` is not on Postgres. That looks stale, since `Meta.ttl` has been supported since #783, but it is tangential (see Rabbit Holes).

## Prior Art

- **#562 / PR #591** (M3, auditable extraction) introduced `DecisionLog`, `TERMINAL_WRITE_LUA` and the claim. This is the Redis implementation that must stay byte-identical.
- **#563 / PR #622** (M4, reference resolution) added `ResolutionLog`, a plain model whose write never raises. `assemble` calls it. It already works on Postgres (spike-3).
- **PR #782** (#759 M4b: recipes, mixins and the question queue on Postgres) is the most relevant precedent:
  - It established the `field_call` pseudo-field adapter pattern.
  - It added the `popoto_lease` engine table (`_qq_lock` / `_qq_release`).
  - It added the refusal this plan removes. That was deliberate, so a Postgres model would not split its audit trail across two stores.
- **PR #793** gave Postgres one connection per unit of work and DB-enforced uniqueness. The guarded write relies on its `_record_locked` and per-statement atomicity.
- **PR #638** routed the journal's annotate-and-close through `SupersessionProtocol`. That is why the journal needs no Redis on Postgres.

No prior attempt to port the decision log exists, so the "Why Previous Fixes Failed" section is omitted.

## Research

**Queries used:**
- PostgreSQL INSERT ON CONFLICT DO UPDATE WHERE condition false row locked not returned; data-modifying CTE touching the same row

**Key findings:**
- [PostgreSQL INSERT docs](https://www.postgresql.org/docs/current/sql-insert.html):
  - When the `ON CONFLICT DO UPDATE ... WHERE` condition is false, the row is **locked but not updated**.
  - That row is **not returned** by `RETURNING`.
  - This is exactly the "refused" signal the guarded write needs.
  - The lock also serializes concurrent terminal writers on one candidate.
- PostgreSQL WITH-query semantics (see the [pgsql-hackers thread](https://www.postgresql.org/message-id/CAKFQuwYHf4%3D4tGOOasUb62kVhBkgm3SgJGOB2UdHnBkMt1QaoA%40mail.gmail.com)):
  - Sibling data-modifying CTEs share one snapshot.
  - Modifying the **same row twice in one statement** is unsupported, and which change survives is unpredictable.
  - The spike-2 single-statement shape is safe only because its two branches are mutually exclusive. The refusal `UPDATE` runs only when the upsert returned nothing, so the row is modified at most once.
  - The builder must keep that exclusivity, or use the two-statement fallback in Technical Approach.

## Spike Results

All spikes ran against local Postgres (`postgresql://localhost:5432/postgres`) through the `tests/postgres` `pg` fixture, with `POPOTO_TEST_DB=9` and a `_RedisRecorder` refusing all Redis traffic. Scratch files were deleted afterwards.

### spike-1: DecisionRecord and the journal already work on Postgres through the ORM
- **Assumption**: "`DecisionRecord` save, keyed get and field filters work on Postgres unchanged."
- **Method**: prototype
- **Finding**: Confirmed, with zero Redis calls:
  - `DecisionRecord.save()` works.
  - `query.get(agent_id=, turn_id=, candidate_id=)` works, and returns `None` on a miss.
  - `query.filter(agent_id=...)` works, and so does a filter on the plain field `state`.
  - The `decision_record` table has a unique `_pk`, a unique `(agent_id, candidate_id, turn_id)` index, and indexes on `turn_id` and `candidate_id`.
  - `ProvenanceJournal.append` works.
  - `JournalEntry.query.filter(turn_id=, subjects__all=["cand:..."])` (the `_reconcile_pending` probe) works.
- **Confidence**: high
- **Impact on plan**: `write_pending`, `get`, `list_for_agent` and the reconcile probe need no new SQL. Only the guarded write, the claim and `turn_summary` need Postgres-specific code.

### spike-2: Guarded terminal write as one SQL statement
- **Assumption**: "An `INSERT ... ON CONFLICT DO UPDATE ... WHERE NOT (accept with entry_id)` plus a refusal branch reproduces `TERMINAL_WRITE_LUA`'s boolean contract."
- **Method**: prototype (raw SQL under `_record_locked`)
- **Finding**: Confirmed with this statement:
  ```sql
  WITH up AS (
    INSERT INTO <t> AS t (...) VALUES (...)
    ON CONFLICT ("_pk") DO UPDATE SET <non-key cols> = EXCLUDED.<col>, ..., "_updated_at" = now()
    WHERE NOT (t."state" = 'accept' AND coalesce(t."entry_id", '') <> '')
    RETURNING 1),
  refuse AS (
    UPDATE <t> SET "detail_code" = 'terminal_conflict_refused'
    WHERE "_pk" = %s AND NOT EXISTS (SELECT 1 FROM up) RETURNING 1)
  SELECT (SELECT count(*) FROM up), (SELECT count(*) FROM refuse)
  ```
  Results:
  - A fresh candidate returns `(1,0)`.
  - `accept` over `reject` returns `(1,0)`.
  - `reject` over an accept-with-entry returns `(0,1)`. The row stays `accept`/`E1` with `detail_code='terminal_conflict_refused'`.
- **Confidence**: high
- **Impact on plan**: The guarded write is one statement under the record advisory lock. No class-set or index-set bookkeeping is needed, because the table's own indexes cover it.

### spike-3: ResolutionLog on Postgres
- **Assumption**: "`ResolutionLog.write()` works on Postgres and is not silently dropping rows."
- **Method**: prototype
- **Finding**:
  - `ResolutionLog.write` with a real `Resolution` returns `True`.
  - `get` reads the row back, with zero Redis.
  - A malformed input returns `False` without raising, so it stays fail-open.
- **Confidence**: high
- **Impact on plan**: `ResolutionLog` needs no change. It only needs coverage in the zero-Redis flow test.

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

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

TBD
