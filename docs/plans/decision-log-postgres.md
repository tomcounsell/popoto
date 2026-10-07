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

1. **Entry point**: the host calls `SubconsciousMemory.extract_memories(...)` with `auditable_extraction=` configured (`recipes/subconscious_memory.py`, `_extract_memories_auditable`, around line 732).
2. **Candidate generation and verdicts**: pure Python, with no storage. When the verdict provider raises, the candidate gets `reject(llm_unavailable)` (`_verdict_for`).
3. **Non-accept verdicts** (`firewall_drop`, `reject`, `withhold`) go to `DecisionLog.write_terminal(record)`.
   - On Redis this is `TERMINAL_WRITE_LUA`.
   - On Postgres it becomes `backend.field_call(DecisionRecord spec, "_m3", "terminal_write", ...)`, which runs the guarded upsert from spike-2.
4. **Accept verdicts** go to `DecisionLog.assemble(...)`:
   1. `acquire_claim` takes the lease. On Redis that is `SET NX PX`. On Postgres it is a `popoto_lease` row through the shared lease adapter.
   2. `get` short-circuits if the row is already terminal.
   3. `_reconcile_pending` runs the journal probe (ORM `filter`).
   4. `write_pending` saves through the ORM.
   5. `_append_and_transition` calls `ProvenanceJournal.append()`, which is already Postgres-routed. It then writes the terminal row through `write_terminal`, and the `ResolutionLog().write()` sidecar through the ORM, which never raises.
   6. `release_claim` releases the claim with a token check.
5. **Turn summary**: at the end of the turn, `turn_summary(agent_id, turn_id)` sets `_last_extraction_privacy_dropped`.
   - On Redis this is `HGETALL` of the summary hash.
   - On Postgres it is derived from the detail rows through `DecisionRecord.query.filter(agent_id=, turn_id=)`.
6. **Output**: the host receives the `ExtractedFact` list. Offline, `compute_metrics` reads through `list_for_agent`, which is SCAN on Redis and an ORM `filter(agent_id=)` on Postgres.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**:
  - None public. `DecisionLog(redis_client=None)` and every method signature are unchanged.
  - Internally there are two new Postgres pseudo-field adapters: `_m3` / `terminal_write`, and `_lease` / `lock` + `release`, which generalises `_qq_lock` / `_qq_release`. The `_qq` `lock` / `release` ops stay registered and delegate to the shared functions, so the question queue is unchanged.
- **Coupling**: lower. `SubconsciousMemory` no longer special-cases the backend for this feature, and `DecisionLog` follows the same `non_redis_backend(Model)` dispatch as `ProvenanceJournal` and `question_queue`.
- **Data ownership**: unchanged. `DecisionRecord` follows the process-default backend, the same rule `JournalEntry` and `ResolutionRecord` already follow, so the decision log, the journal and the resolution sidecar always land in the same store.
- **Reversibility**: easy. Reverting restores the refusal. There is no data migration: Postgres rows live in an ordinary typed table (`decision_record`) plus lease rows that expire.

## Appetite

**Size:** Medium

**Team:** Solo dev (builder), validator, documentarian

**Interactions:**
- PM check-ins: 1, to confirm the `turn_summary` divergence and the `Meta.backend`-only gap in Open Questions.
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli -n 15 ping` | Redis leg of the suite (DB 15 via `popoto_test_db`) |
| Postgres reachable | `pg_isready -h localhost -p 5432` | Postgres conformance leg and `tests/postgres` |
| `POSTGRES_URL` exported for test runs | `test -n "$POSTGRES_URL"` (e.g. `postgresql://localhost:5432/postgres`) | Enables the Postgres leg; without it the leg skips |
| Postgres driver installed | `python -c "import psycopg"` | Postgres backend import |

## Solution

### Key Elements

- **Backend dispatch in `DecisionLog`**: `DecisionLog` chooses its store once, at construction, from `DecisionRecord`'s bound backend.
  - When `redis_client` is passed explicitly, the Redis path is used. That keeps the public parameter's meaning.
  - When `non_redis_backend(DecisionRecord)` is not `None`, the Postgres path is used.
  - Otherwise the Redis path is used, exactly as today.
- **Postgres guarded terminal write**: one SQL statement in a new `_m3` / `terminal_write` adapter. It returns `True` when the row was written and `False` when it was refused, with the same warning log on refusal.
- **Postgres assembly claim**: the existing `popoto_lease` engine table, reached through a shared `_lease` adapter with `lock` / `release` ops. The claim keys keep the `popoto:m3:claim:{agent}:{turn}:{cand}` format, so the key helpers are unchanged.
- **Postgres readers**: ORM queries, with no new SQL.
  - `get` uses `DecisionRecord.query.get(...)`.
  - `list_for_agent` uses `query.filter(agent_id=...)`.
  - `list_pending` keeps its existing Python state filter, `older_than` filter and `written_at`-ascending sort over `list_for_agent`.
  - `compute_metrics` is untouched.
- **Postgres `turn_summary`**: derived from the detail rows. `filter(agent_id=, turn_id=)` selects the rows, then each terminal row adds one to `state:<state>` and, when `reason_code` is set, one to `reason:<reason_code>`. Pending rows are not counted. The result is returned as the same `dict[str, int]` shape.
- **Refusal removed**: the `BackendCapabilityError` branch in `SubconsciousMemory.__init__` is deleted, and `self._decision_log = DecisionLog()` is built unconditionally.

### Flow

Host on Postgres → `SubconsciousMemory(auditable_extraction=cfg)` constructs (it used to raise) → `extract_memories(turn)` → decision rows in `decision_record`, journal entries in `journal_entry`, sidecar rows in `resolution_record`, and leases in `popoto_lease` → `ExtractedFact`s returned → offline `compute_metrics(agent_id)` produces precision and recall. No Redis connection is checked out at any point.

### Technical Approach

- **Dispatch shape**: keep `DecisionLog` as one class. In `__init__`:
  - If `redis_client is None`, resolve `self._backend = non_redis_backend(DecisionRecord)`.
  - Only when `self._backend` is `None`, assign `self._redis = redis_client or get_REDIS_DB()`, exactly as today.
  - On the Postgres path, set `self._redis = None` and never call `get_REDIS_DB()`.

  Each Redis-touching method (`write_terminal`, `acquire_claim`, `release_claim`, `get`, `list_for_agent`, `turn_summary`) gets a single early branch, `if self._backend is not None: return self._pg_<name>(...)`. The Redis bodies below the branch stay textually unchanged, so the Redis command sequence is identical. Private `_pg_*` helpers live in `decision_log.py` and call `self._backend.field_call(DecisionRecord._meta.spec, ...)` for SQL work, or the ORM for reads. That matches how `question_queue` and `provenance_journal` route.
- **Why `DecisionRecord`'s backend and not the memory model's**: this answers the issue's open question.
  - `DecisionRecord`, `ResolutionRecord` and `JournalEntry` have no `Meta.backend`, so all three follow the process default (`set_backend()` → `POPOTO_BACKEND` → `"redis"`).
  - Keying the decision log on that same rule guarantees the audit trail and the journal it reconciles against are always in one store. That was the refusal's whole purpose.
  - Keying it on the memory model would split them whenever the memory model sets `Meta.backend="postgres"` under a Redis process default.
  - That configuration keeps working as it does today: decision log and journal both stay in Redis. The plan documents it rather than refusing it, because a new raise would change Redis-default behavior. See Open Questions.
- **Guarded write SQL**: implement spike-2's statement in `backends/postgres/recipes.py`:
  - Add an `M3_FIELD = "_m3"` handler `{"terminal_write": self._m3_terminal_write}`.
  - Build the columns from `DecisionRecord`'s `TableSpec` with `to_column_value`.
  - Wrap the statement with `_record_locked` on the row's `_pk`, so it serializes with ORM saves of the same row such as `write_pending`.
  - `written_at` is taken from the record exactly as the Redis path sets it before the Lua call, so both legs store the same value.
  - The two CTE branches must stay mutually exclusive (see Research). If `_record_locked` and `uow` allow it, an acceptable fallback is two statements in one unit of work under the same advisory lock: the upsert with `RETURNING`, then the `detail_code` `UPDATE` only when the upsert returned nothing.
  - The adapter returns `bool`.
- **Lease generalisation**:
  - Rename `_qq_lock` / `_qq_release` to `_lease_lock` / `_lease_release`. Their bodies are unchanged.
  - Register them under both `QQ_FIELD` (`lock` / `release`), so the question queue keeps working, and a new `LEASE_FIELD = "_lease"`.
  - Check the record-lock membership tuple at the tail of `_recipe_field_call` and add `_m3` and `_lease` only where the existing `_qq` precedent does.
  - `acquire_claim` on Postgres passes `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`. The magic number stays in `Defaults`.
- **`turn_summary` divergence, documented and pinned by a Postgres-only test**:
  - The Redis summary counts a candidate's **first** terminal write only. A later `accept`-over-`reject` transition does not re-count, and the docstring calls the summary a convenience index while the detail rows are the source of truth.
  - Postgres derives the summary from the detail rows, so it always equals the current terminal states.
  - The two legs agree in every case the existing suite exercises: `test_summary_counts_terminal_states_only` and `test_summary_counts_a_transitioned_candidate_once` both pass under derivation. They differ only for a candidate whose terminal state changed after its first terminal write, and Postgres reports the truer number there.
  - The rejected alternative is a `popoto_*` counter engine table incremented in the guarded statement. It reproduces a Redis convenience structure the doctrine asks us not to emulate, and it imports Redis's summary-versus-detail drift.
- **Docstrings**: `list_for_agent` and `list_pending` docstrings say the key-pattern `SCAN` and its "ORM bypass" rationale apply to Redis only, and that Postgres uses an indexed `WHERE agent_id = ...` query.
- **Test conversion**: `tests/test_auditable_extraction.py`'s storage classes join the conformance harness. See Test Impact.
- **Stale snapshot**: the file's module-level `from popoto.redis_db import POPOTO_REDIS_DB` (line 41) is converted to `get_REDIS_DB()` at call sites, per CLAUDE.md.

## Failure Path Test Strategy

### Exception Handling Coverage
- `ResolutionLog.write` catches `Exception`, logs a warning and returns `False`. A Postgres-leg test monkeypatches the sidecar save to raise. It asserts that `assemble` still returns the accept outcome, that the decision row is `accept`, and that a warning is logged.
- `_append_and_transition` has two mappings:
  - `JournalBlockedError` becomes `firewall_drop(post_accept_journal_block)`.
  - Any other exception becomes `reject(assembly_failed)`, with the exception class name in the detail.

  Both mappings get conformance-leg tests (on both backends) that assert the terminal row's state, reason and detail.
- `_verdict_for` maps a raising verdict provider to `reject(llm_unavailable)`. A conformance-leg test checks it on both backends.
- On a refused terminal write, `write_terminal` returns `False` and logs a warning. A conformance-leg test asserts the return value, the unchanged state and `entry_id`, `detail_code == "terminal_conflict_refused"`, and the log record.
- Outage parity:
  - With the Postgres backend's connection forced down, `DecisionLog.write_terminal` raises `BackendUnavailableError`, a `ConnectionError` subclass. That matches the Redis path's propagating `redis.ConnectionError`. No new swallow is added.
  - A Postgres-only test asserts that `extract_memories` propagates a `ConnectionError` subclass, exactly as the Redis path does today.

### Empty/Invalid Input Handling
- An empty turn produces no candidates and no rows. `turn_summary` returns `{}` on both legs, and `list_for_agent` returns `[]` for an unknown agent on both legs.
- `write_terminal` with a non-terminal state raises `ValueError` before any backend call, on both legs. That check is not backend-specific.
- `get` on a missing key returns `None` on both legs.

### Error State Rendering
- There is no user-visible UI. Errors surface as log warnings, refused-write `False` values or terminal reason codes, and the tests above assert each of those.

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
