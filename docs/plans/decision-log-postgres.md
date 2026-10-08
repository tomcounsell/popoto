---
status: Planning
type: feature
appetite: Medium
owner: Solo dev
created: 2026-10-07
tracking: https://github.com/tomcounsell/popoto/issues/811
last_comment_id:
revision_applied: true
revision_applied_at: 2026-10-08T00:00:00Z
---

# Decision Log on Postgres

Implementation branch: `feature/decision-log-postgres`.

**Test environment (binding for build, validation and review):**
- Redis/Valkey on `localhost:6379`, **DB 15 only**. The suite binds DB 15 through `popoto_test_db`; any ad-hoc repro script exports `REDIS_URL=redis://localhost:6379/15` *before* `import popoto`. Never DB 0 (a live agent store on this machine, see CLAUDE.md and #577).
- Local Postgres 18 at `POSTGRES_URL=postgresql://localhost:5432/postgres` (measured at plan time: `18.6 (Homebrew)`).
- A worktree venv with `pip install -e ".[dev,embeddings,benchmark,mcp,postgres]"`, whose editable install resolves to the worktree (CLAUDE.md "Verifying in a worktree", trap 1).
- **The Postgres tests must actually run.** A leg that skips because `POSTGRES_URL` is unset or `psycopg` is missing is a failed validation, not a pass. Every Postgres command in Verification is read with `-rs` and must report zero Postgres skips; the collect-count rows guard against a vacuous leg.

## Problem

An agent host running on the Postgres backend cannot turn on auditable extraction. `SubconsciousMemory(auditable_extraction=AuditableExtractionConfig(...))` on a Postgres-bound model raises `BackendCapabilityError` at construction (`src/popoto/recipes/subconscious_memory.py:284-298`). The host therefore loses the per-candidate decision log, which is the only way to measure extraction precision and recall offline.

Everything else on the auditable path already runs on Postgres: candidates, verdicts, the provenance journal and the M4 resolution sidecar. The decision log is the one piece written directly against a Redis client.

**Current behavior:**
- Construction refuses. The refusal is pinned by `tests/postgres/test_postgres_recipes.py::test_the_auditable_extraction_path_is_refused_on_postgres` (line 593) and documented in `docs/features/postgres-backend.md` ("Not on Postgres yet", lines 843-848).
- `DecisionLog` (`src/popoto/extraction/decision_log.py`) holds `self._redis = redis_client or get_REDIS_DB()`. It relies on Redis in three places:
  - `TERMINAL_WRITE_LUA` does the guarded terminal write, the summary `HINCRBY`, and maintains the class set and index sets by hand.
  - `SET NX PX` plus a Lua compare-and-delete implement the assembly claim.
  - `EXISTS`, `SCAN` and `HGETALL` back the readers.

**turn_summary drift on Redis (found while resolving Open Question 2):**
- **Contract.** M3 defines the per-turn summary as "a convenience index" over the detail rows "holding terminal-state counts and reason-code distribution", with "the detail rows ... the sole source of truth" (`docs/plans/auditable_extraction_m3.md:713-722`; also `:257`, `:1342-1344`). The shipped docstring repeats it: "A convenience index over the detail rows, aggregating terminal states only ... if it ever disagrees with the detail rows, the detail rows are right" (`src/popoto/extraction/decision_log.py:1019-1024`), as does `docs/features/auditable-extraction.md:187-190`. M3's Race 1 states the prerequisite outright: "The summary reflects every candidate written for the turn" (`auditable_extraction_m3.md:962-963`). So the summary is meant to equal a count over the rows' current states, never a log of first decisions.
- **Implementation.** `TERMINAL_WRITE_LUA` bumps the summary only "when the row is new or still non-terminal 'pending'" (`decision_log.py:267-274`). A terminal-to-terminal write that the guard lets through (anything except overwriting an `accept` with an `entry_id`) rewrites the row and leaves the summary alone.
- **Reproduced on Redis DB 15 at `44afd0f7`:** `write_terminal(reject, not_a_fact)`, then `write_terminal(reject, not_memorable)`, then `write_terminal(accept, accepted, entry_id="E1")` on one candidate leaves the detail row at `accept`/`accepted`, while `turn_summary` returns `{'state:reject': 1, 'reason:not_a_fact': 1}`. The summary disagrees with the row on both keys.
- **Consumers.** The one in-tree consumer is `SubconsciousMemory._extract_memories_auditable`, which reads `state:firewall_drop` to set `_last_extraction_privacy_dropped` (`recipes/subconscious_memory.py:822-825`). It asks "did this turn drop anything for privacy", a question about current row states. `compute_metrics` reads detail rows only. No consumer needs first-decision counts, and none could tell them apart from current counts in the existing tests, because `test_summary_counts_terminal_states_only` and `test_summary_counts_a_transitioned_candidate_once` (`tests/test_auditable_extraction.py:836-879`) never move a row between two terminal states.
- **Conclusion.** First-write-only counting is drift from the contract, not the contract. The plan fixes it on Redis and implements the same semantics on Postgres.

**Desired outcome:**
- On a Postgres-bound process, `SubconsciousMemory(auditable_extraction=...)` constructs.
- `extract_memories` runs the full flow (candidates, verdicts, decision rows, journal, `ExtractedFact`s) with **zero Redis commands**.
- Observable semantics are **identical** on both backends, `turn_summary` included. There is no documented divergence.
- `turn_summary` honours its M3 contract on both backends: it is a rollup of the turn's detail rows' *current* terminal states. The Redis implementation does not do that today when a candidate's terminal state or reason changes after its first terminal write (see "turn_summary drift on Redis" below), so this plan fixes Redis too.
- Apart from that fix, the Redis path stays byte-identical: same keys, same command sequence, and the same Lua except for the summary-maintenance block in `TERMINAL_WRITE_LUA`.
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
   - On Redis this is `HGETALL` of the summary hash, which `TERMINAL_WRITE_LUA` now keeps equal to the rollup of current terminal states (decrement-old / increment-new on a terminal-to-terminal write).
   - On Postgres it is one aggregate statement over the detail rows: `GROUP BY state, reason_code` for `(agent_id, turn_id)`, terminal states only.
   - Both legs return the same `dict[str, int]` for the same sequence of writes.
6. **Output**: the host receives the `ExtractedFact` list. Offline, `compute_metrics` reads through `list_for_agent`, which is SCAN on Redis and an ORM `filter(agent_id=)` on Postgres.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**:
  - None public. `DecisionLog(redis_client=None)` and every method signature are unchanged.
  - Internally there are two new Postgres pseudo-field adapters: `_m3` / `terminal_write`, and `_lease` / `lock` + `release`, which generalises `_qq_lock` / `_qq_release`. The `_qq` `lock` / `release` ops stay registered and delegate to the shared functions, so the question queue is unchanged.
- **Coupling**: lower. `SubconsciousMemory`'s backend check shrinks to the split-trail case only, and `DecisionLog` follows the same `non_redis_backend(Model)` dispatch as `ProvenanceJournal` and `question_queue`.
- **Data ownership**: unchanged. `DecisionRecord` follows the process-default backend. The default `JournalEntry` and `ResolutionRecord` follow the same rule, so with the default journal the decision log, the journal and the resolution sidecar always land in the same store. A journal configured with a custom `entry_model` (`ProvenanceJournal.entry_model`, `recipes/provenance_journal.py:596`) can set its own `Meta.backend`. The narrowed refusal guards that case explicitly (see Technical Approach and Risk 4).
- **Reversibility**: easy. Reverting restores the refusal. There is no data migration: Postgres rows live in an ordinary typed table (`decision_record`) plus lease rows that expire.

## Appetite

**Size:** Medium

**Team:** Solo dev (builder), validator, documentarian

**Interactions:**
- PM check-ins: done. All three Open Questions were answered by the maintainer on 2026-10-08 (see Open Questions).
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli -n 15 ping` | Redis leg of the suite (DB 15 via `popoto_test_db`) |
| Postgres 18 reachable | `psql postgresql://localhost:5432/postgres -Atc 'show server_version'` | Postgres conformance leg and `tests/postgres` (expect `18.x`) |
| `POSTGRES_URL` exported for test runs | `test "$POSTGRES_URL" = postgresql://localhost:5432/postgres` | Enables the Postgres leg. Without it the leg skips, which counts as a failed run |
| Worktree venv with all extras | `pip install -e ".[dev,embeddings,benchmark,mcp,postgres]"` then `python -c "import popoto, psycopg; print(popoto.__file__)"` | Postgres driver present; `popoto.__file__` must resolve inside the worktree |
| Redis bound to DB 15 for ad-hoc scripts | `export REDIS_URL=redis://localhost:6379/15` before any `python -c "import popoto ..."` | Keeps repro scripts off DB 0 |

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
- **One `turn_summary` contract, both backends.** `turn_summary(agent_id, turn_id)` returns, for the turn's detail rows whose **current** state is terminal (`accept`, `reject`, `withhold`, `firewall_drop`):
  - `state:<s>` = the number of those rows whose state is `s`;
  - `reason:<r>` = the number of those rows whose `reason_code` is `r`, counted **unconditionally**, so an empty reason yields the key `reason:` (matching `summary_fields`, `decision_log.py:514-517`);
  - no key with a zero count, and never a `state:pending` key.

  Each candidate contributes exactly one `state:` and one `reason:` count, reflecting its row as it stands now. A refused write (`terminal_conflict_refused`) changes only `detail_code`, so it changes no count.
- **Redis fix: keep the summary hash equal to the rollup, atomically.** `TERMINAL_WRITE_LUA` already reads the prior `state`. When the write proceeds and the prior state is terminal, the script also reads the prior `reason_code`, decodes both with `cmsgpack.unpack`, decrements `state:<old>` and `reason:<old>` (removing a field that reaches zero or below with `HDEL`), and increments the new fields. A rewrite with the same state and reason is a net no-op. Everything stays inside the one `EVAL`, so the row and the summary still change together, as M3's Race 1 requires. See Technical Approach.
- **Redis repair for already-drifted hashes.** Summary hashes written before the fix may disagree with their rows. A new `DecisionLog.rebuild_turn_summary(agent_id, turn_id) -> Dict[str, int]` recomputes the hash from the detail rows in one Lua script and returns the result. On Postgres the summary is derived on read and cannot drift, so the method returns `turn_summary(...)` unchanged.
- **Postgres `turn_summary`**: one aggregate statement through a new `_m3` / `turn_summary` op:
  `SELECT state, reason_code, count(*) FROM decision_record WHERE agent_id = %s AND turn_id = %s AND state IN (<terminal states>) GROUP BY state, reason_code`.
  Python folds the rows into the same `state:` / `reason:` dict (a `NULL` reason folds to `reason:`). It is a single snapshot-consistent read, as `HGETALL` is on Redis.
- **Refusal narrowed to the split-trail case**:
  - Today the `BackendCapabilityError` branch in `SubconsciousMemory.__init__` fires for any non-Redis memory model.
  - After this change it fires only inside the existing `non_redis_backend(model_class) is not None` branch, and only when the decision log would land in Redis or would land in a different store from the journal. Concretely: the memory model is non-Redis **and** either `non_redis_backend(DecisionRecord)` is `None`, or `DecisionRecord`'s store and the configured journal entry model's store disagree (see Technical Approach, "Refusal guard").
  - The error message changes to name that mixed configuration and the fix: bind the process default with `set_backend("postgres")` / `POPOTO_BACKEND=postgres`.
  - An all-Postgres process constructs normally. See Risk 4 for the full configuration table.

### Flow

Host on Postgres → `SubconsciousMemory(auditable_extraction=cfg)` constructs (it used to raise) → `extract_memories(turn)` → decision rows in `decision_record`, journal entries in `journal_entry`, sidecar rows in `resolution_record`, and leases in `popoto_lease` → `ExtractedFact`s returned → offline `compute_metrics(agent_id)` produces precision and recall. No Redis connection is checked out at any point.

### Technical Approach

- **Dispatch shape**: keep `DecisionLog` as one class. In `__init__`:
  - If `redis_client is None`, resolve `self._backend = non_redis_backend(DecisionRecord)`.
  - Only when `self._backend` is `None`, assign `self._redis = redis_client or get_REDIS_DB()`, exactly as today.
  - On the Postgres path, set `self._redis = None` and never call `get_REDIS_DB()`.

  Each Redis-touching method (`write_terminal`, `acquire_claim`, `release_claim`, `get`, `list_for_agent`, `turn_summary`, and the new `rebuild_turn_summary`) gets a single early branch, `if self._backend is not None: return self._pg_<name>(...)`. The Python Redis bodies below the branch stay textually unchanged, so the Redis command sequence is identical. The one deliberate Redis change is inside the `TERMINAL_WRITE_LUA` string (next bullets); `write_terminal`'s `run_lua` call, its `numkeys` of 6, its KEYS and its ARGV layout do not change. Private `_pg_*` helpers live in `decision_log.py` and call `self._backend.field_call(DecisionRecord._meta.spec, ...)` for SQL work, or the ORM for reads. That matches how `question_queue` and `provenance_journal` route.
- **Why `DecisionRecord`'s backend and not the memory model's**: this answers the issue's open question.
  - `DecisionRecord`, `ResolutionRecord` and `JournalEntry` have no `Meta.backend`, so all three follow the process default (`set_backend()` → `POPOTO_BACKEND` → `"redis"`).
  - Keying the decision log on that same rule guarantees the audit trail and the journal it reconciles against are always in one store. That was the refusal's whole purpose.
  - Keying it on the memory model would split them whenever the memory model sets `Meta.backend="postgres"` under a Redis process default.
  - That mixed configuration (memory model on Postgres, process default on Redis) stays refused through a narrowed guard. See the next bullet and Risk 4.
  - The same-store guarantee holds for the **default** `JournalEntry` only. The journal's store is set by `journal.entry_model` (read at `decision_log.py:733`; overridable at `recipes/provenance_journal.py:596`), and a custom entry model may carry its own `Meta.backend`. `_reconcile_pending` works across stores, because it queries through the entry model's own ORM, but a split trail is exactly what the guard exists to prevent, so the guard refuses it explicitly.
- **Refusal guard (exact shape)**: in `SubconsciousMemory.__init__`, the new check sits **inside** the existing `if non_redis_backend(model_class) is not None:` branch, so the new refusal is a strict subset of today's:
  ```python
  jb = non_redis_backend(
      getattr(cfg.journal, "entry_model", None) or JournalEntry
  )
  db = non_redis_backend(DecisionRecord)
  if db is None or (db is None) != (jb is None):
      raise BackendCapabilityError(...)  # names the mixed configuration and the fix
  ```
  `db is None` covers the row where the memory model is on Postgres and the process default is on Redis. The second clause covers a custom `entry_model` whose store differs from `DecisionRecord`'s. The builder confirms the attribute path to the journal on `AuditableExtractionConfig` (`cfg.journal` above), falling back to `JournalEntry` when it is absent.
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
  - The tuple at the tail of `_recipe_field_call` (`recipes.py:228-234`: `COUNTER_FIELD`, `TOMB_FIELD`, ...) is **not** a record-lock list. It decides whether `field` is passed to the handler: members are model-level stores called as `handler(spec, *args, ...)`, and everything else is called as `handler(spec, field, *args, ...)`. `_qq` is not in it, so `_m3` and `_lease` stay out too and receive `(spec, field, ...)` exactly as `_qq_lock` does.
  - The `_lease` alias is optional. The builder may instead have `DecisionLog` call the existing `_qq` `lock` / `release` ops directly and skip the rename. If the alias is added, `_qq` keeps delegating to the same functions.
  - `acquire_claim` on Postgres passes `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`. The magic number stays in `Defaults`.
- **`turn_summary` semantics: current terminal states, identical on both legs.** The contract and the evidence that Redis drifts from it are in Problem ("turn_summary drift on Redis"). Two alternatives were considered and rejected:
  - *First-terminal-write counts on both legs.* That would mean building a counter engine table on Postgres to reproduce a number that no consumer wants and that contradicts M3's "detail rows are right" rule. It would make the two backends agree by making both wrong.
  - *Derive on read on Redis too* (SINTER the `agent_id` and `turn_id` index sets, then read each row). It is correct, but it turns an O(1) `HGETALL` into a per-turn row scan, and avoiding that scan is the only reason M3 kept the summary (`auditable_extraction_m3.md:713-720`).

  The chosen fix keeps Redis O(1) by maintaining the hash correctly at write time, and Postgres derives on read, where an indexed `GROUP BY` is the natural shape and cannot drift. The two implementations satisfy one stated contract, and one conformance test checks it on both legs (Test Impact).
- **Redis: `TERMINAL_WRITE_LUA` summary block (exact shape).** Only the block after the refusal branch changes (today `decision_log.py:267-274`). The refusal branch, the `SADD`s, the row `HSET` and the return values stay as they are.
  ```lua
  -- The summary is a rollup of the rows' CURRENT terminal states: each
  -- candidate contributes one state:<s> and one reason:<r>. A write that
  -- moves a row off a terminal state takes back that state's counts first.
  local n = tonumber(ARGV[5])
  local is_new = (prev_state == false or prev_state == nil or prev_state == ARGV[4])
  if not is_new then
      local prev_reason = redis.call('HGET', KEYS[1], 'reason_code')
      local old_fields = {
          'state:' .. cmsgpack.unpack(prev_state),
          'reason:' .. (prev_reason and cmsgpack.unpack(prev_reason) or ''),
      }
      -- ARGV[6] / ARGV[7] are the new state:/reason: fields, in that order.
      if old_fields[1] ~= ARGV[6] or old_fields[2] ~= ARGV[7] then
          for _, f in ipairs(old_fields) do
              if redis.call('HINCRBY', KEYS[2], f, -1) <= 0 then
                  redis.call('HDEL', KEYS[2], f)
              end
          end
          is_new = true
      end
  end
  if is_new then
      for i = 1, n do
          redis.call('HINCRBY', KEYS[2], ARGV[5 + i], 1)
      end
  end
  ```
  Notes for the builder:
  - The decrement compares the two fields as a pair. When only the reason changes, decrementing and re-incrementing the unchanged `state:` field nets to zero, which is correct. Comparing both avoids two wasted round-trips inside the script when nothing changed (a same-verdict retry, M3 Race 2).
  - `cmsgpack.unpack` is already used by in-repo Lua (`backends/redis.py:840`). It is a Lua library bundled with both Redis and Valkey, not a module, so it is Valkey-safe. Popoto packs `str` values as msgpack `str` (`_packb` → `msgpack.packb`), which `cmsgpack` decodes; it cannot read the `bin` type, which never occurs for these two fields.
  - `HDEL` at `<= 0` keeps the stored hash equal to the rollup, so `"state:reject" not in summary` keeps holding after a row leaves `reject`. The `<= 0` (not `== 0`) also clamps a hash that drifted before this fix: decrementing a field that was never counted removes it rather than storing `-1`.
  - The `state:` / `reason:` prefixes are now spelled in two places, Python (`summary_fields`) and Lua. Add a comment at each pointing to the other, and a test that pins the agreement (the conformance test does).
  - `ARGV` order is unchanged: Python already passes `state:` first and `reason:` second (`decision_log.py:514-517`). The script relies on that order; add a comment saying so.
  - A prior state that decodes to something outside the terminal vocabulary other than `pending` (in practice only `""`, from a row created by a bare ORM `save()` with defaults, which `DecisionLog` never does) is treated like `pending`: it was never counted, so nothing is decremented. Implement this by comparing the decoded prior state against `''` alongside the existing `ARGV[4]` check, so the ARGV layout stays unchanged.
- **Redis: `rebuild_turn_summary` (repair).** A second script, `TURN_SUMMARY_REBUILD_LUA`, runs as one `EVAL`, so concurrent `TERMINAL_WRITE_LUA` calls serialize around it:
  - `KEYS[1]` = the summary hash, `KEYS[2]` / `KEYS[3]` = the `agent_id` and `turn_id` KeyField index Sets (from `key_field_index_keys`).
  - `SINTER KEYS[2] KEYS[3]` gives the turn's row keys. For each row it `HGET`s `state` and `reason_code`, decodes them with `cmsgpack.unpack`, and counts terminal ones. The terminal-state names come in as `ARGV`, so the script does not hard-code the vocabulary.
  - It then `DEL`s the hash, writes the counts with `HINCRBY` (or `HSET`), and returns the flat field/count list.

  The row keys are read from a set rather than declared in `KEYS`. That is fine on standalone Redis and Valkey, and there is in-repo precedent for computed key access inside a script (`fields/bm25_field.py:291`). It is not Redis Cluster-safe, and neither is the existing decision log, whose summary and row keys already share no hash tag. `rebuild_turn_summary` is an operator repair, called by nothing on the hot path.
- **Postgres: `_m3` / `turn_summary` op.** It runs the aggregate from Key Elements in its own unit of work, with no advisory lock: a plain `SELECT` reads one snapshot, and the guarded write's `_record_locked` already serializes writers per row. `rebuild_turn_summary` on Postgres returns `self.turn_summary(...)`, because there is no stored summary to repair.
- **Docstrings**: `list_for_agent` and `list_pending` docstrings say the key-pattern `SCAN` and its "ORM bypass" rationale apply to Redis only, and that Postgres uses an indexed `WHERE agent_id = ...` query.
- **Test conversion**: `tests/test_auditable_extraction.py`'s storage classes join the conformance harness. See Test Impact.
- **Stale snapshot (tests only)**: the module-level `from popoto.redis_db import POPOTO_REDIS_DB` is in `tests/test_auditable_extraction.py:41`, not in `decision_log.py`, which already imports `get_REDIS_DB` (line 92). The test file's import is converted to `get_REDIS_DB()` at its call sites (lines 533, 924 and 1202-1203), per CLAUDE.md.

## Failure Path Test Strategy

### Exception Handling Coverage
- `ResolutionLog.write` catches `Exception`, logs a warning and returns `False`. A Postgres-leg test monkeypatches the sidecar save to raise. It asserts that `assemble` still returns the accept outcome, that the decision row is `accept`, and that a warning is logged.
- `_append_and_transition` has two mappings:
  - `JournalBlockedError` becomes `firewall_drop(post_accept_journal_block)`.
  - Any other exception becomes `reject(assembly_failed)`, with the exception class name in the detail.

  Both mappings get conformance-leg tests (on both backends) that assert the terminal row's state, reason and detail.
- `_verdict_for` maps a raising verdict provider to `reject(llm_unavailable)`. A conformance-leg test checks it on both backends.
- On a refused terminal write, `write_terminal` returns `False` and logs a warning. A conformance-leg test asserts the return value, the unchanged state and `entry_id`, `detail_code == "terminal_conflict_refused"`, and the log record. It also asserts that `turn_summary` is unchanged by the refusal on both legs.
- A drifted Redis summary hash (seeded directly with `HSET`, as a pre-fix deployment would have left it) is clamped, never driven negative, by a later transition, and `rebuild_turn_summary` restores it to the rollup. Redis-only, because Postgres has no stored hash; its Postgres twin asserts `rebuild_turn_summary == turn_summary`.
- Outage parity:
  - With the Postgres backend's connection forced down, `DecisionLog.write_terminal` raises `BackendUnavailableError`, a `ConnectionError` subclass. That matches the Redis path's propagating `redis.ConnectionError`. No new swallow is added.
  - A Postgres-only test asserts that `extract_memories` propagates a `ConnectionError` subclass, exactly as the Redis path does today.

### Empty/Invalid Input Handling
- An empty or whitespace-only turn writes exactly one `reject(empty_turn)` row on both legs. This is already pinned by `test_auditable_empty_turn_logs_a_reject_row`, which joins the conformance harness.
- An unknown agent or turn gives the same result on both legs: `turn_summary` returns `{}` and `list_for_agent` returns `[]`.
- `write_terminal` with a non-terminal state raises `ValueError` before any backend call, on both legs. That check is not backend-specific.
- `get` on a missing key returns `None` on both legs.

### Error State Rendering
- There is no user-visible UI. Errors surface as log warnings, refused-write `False` values or terminal reason codes, and the tests above assert each of those.

## Test Impact

- [ ] `tests/postgres/test_postgres_recipes.py::test_the_auditable_extraction_path_is_refused_on_postgres` — REPLACE. It becomes a positive test, e.g. `test_the_auditable_extraction_path_runs_on_postgres`. The test checks that construction succeeds, that `decision_log` is not `None`, and that `extract_memories` returns `ExtractedFact`s.
- [ ] `tests/postgres/test_postgres_recipes.py::test_a_key_tier_lifecycle_is_refused_at_construction_on_postgres` — UPDATE, docstring only. Drop the "as SubconsciousMemory(auditable_extraction=) is" comparison, since that refusal no longer exists.
- [ ] `tests/test_auditable_extraction.py` — UPDATE. The storage-touching classes join the conformance harness: `TestDecisionLogCore`, `TestAssemblyWiring`, `TestSubconsciousMemoryWiring`, `TestAssemblyAgainstTheRealJournal` and `TestExtractedFactSpanInvariant`. Each gets `pytest.mark.conformance` plus `pytest.mark.usefixtures("backend")`, following `tests/test_provenance_journal.py`. The pure classes (`TestVerdictVocabulary`, `TestVerdictStage`, `TestCandidateGeneration`) stay unmarked.
- [ ] `tests/test_auditable_extraction.py:41` (module-level `from popoto.redis_db import POPOTO_REDIS_DB`) — UPDATE. Remove it and resolve `get_REDIS_DB()` at each Redis-only call site.
- [ ] `TestDecisionLogCore._rows_for` (raw `KEYS`), used by `test_single_row_per_candidate_transitions_in_place`, `test_fresh_candidate_terminal_write_succeeds`, `test_lua_created_row_is_visible_to_the_orm_query_api` and `test_terminal_write_is_refused_against_an_assembled_accept_row` — UPDATE. Make `_rows_for` backend-agnostic. On Redis it keeps the `KEYS` count. On Postgres it counts `DecisionRecord.query.filter(agent_id=, turn_id=)` rows matching the `candidate_id`. One row per candidate is the invariant on both backends, so these four stay on both legs and need no `redis_only`.
- [ ] `TestDecisionLogCore::test_redis_key_joins_key_fields_alphabetically` — UPDATE. Add `@pytest.mark.redis_only(reason=...)`, because the key format only exists on Redis. Its Postgres twin asserts the composite unique index on `decision_record`.
- [ ] `TestAssemblyWiring::test_claim_carries_a_finite_ttl` (`PTTL`) — UPDATE. Add `redis_only`. The Postgres twin asserts that the `popoto_lease` row's `expires_at` is about `time.time() + M3_ASSEMBLY_CLAIM_TTL_MS / 1000`, within a tolerance. `expires_at` is epoch **seconds** (`recipes.py:109-111`). The twin then asserts that a second `acquire_claim` succeeds once the lease has expired. **Do not sleep 30s**: `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS` is 30_000 (`constants.py:588`). `acquire_claim` reads the constant at call time (`decision_log.py:583`), so either monkeypatch it to about 50ms and sleep 0.1s, or set the row's `expires_at = extract(epoch from clock_timestamp()) - 1` directly.
- [ ] `TestAssemblyWiring::test_metrics_are_identical_with_the_journal_keyspace_absent` (`SCAN`/`DEL` of `JournalEntry*`) — UPDATE. Add `redis_only`. The Postgres twin deletes the `journal_entry` rows through the ORM, or truncates the test table, and asserts that `compute_metrics` is unchanged.
- [ ] `tests/test_auditable_extraction.py::TestDecisionLogCore::test_summary_counts_terminal_states_only` and `::test_summary_counts_a_transitioned_candidate_once` — no assertion change. Both hold under the current-state contract, and they now run on both legs.
- [ ] New conformance tests in `TestDecisionLogCore` (both legs), the `turn_summary` contract pin:
  - `test_summary_follows_a_terminal_to_terminal_transition`: `reject(not_a_fact)`, then `accept(accepted, entry_id="E1")` on one candidate. The summary is exactly `{"state:accept": 1, "reason:accepted": 1}`, with no `state:reject` and no `reason:not_a_fact` key.
  - `test_summary_follows_a_reason_change_within_one_state`: `reject(not_a_fact)`, then `reject(not_memorable)`. The summary is exactly `{"state:reject": 1, "reason:not_memorable": 1}`.
  - `test_summary_same_verdict_retry_is_a_no_op`: the same `reject(not_a_fact)` written twice gives counts of 1, not 2.
  - `test_summary_transition_among_several_candidates`: three candidates, two of which move between terminal states (including `withhold` → `reject` and `accept` without an `entry_id` → `firewall_drop`). The summary equals a rollup computed in the test from `list_for_agent` rows. That equality is the contract itself.
  - `test_summary_counts_an_empty_reason_under_reason_colon`: a terminal write with `reason_code=""` produces `reason:` on both legs.
  - `test_refused_write_leaves_the_summary_unchanged`.
- [ ] New Redis-only test (`redis_only`, reason "the stored summary hash exists only on Redis; Postgres derives on read"): `test_rebuild_turn_summary_repairs_a_drifted_hash`. Seed a drifted hash with `HSET` through `get_REDIS_DB()`, transition a row, assert no field is negative, call `rebuild_turn_summary`, and assert it equals the rollup. Postgres twin: `rebuild_turn_summary(...) == turn_summary(...)`.
- [ ] New in `tests/postgres/test_postgres_decision_log.py`, all Postgres-only twins:
  - The zero-Redis full-flow test. It monkeypatches `Defaults.M4_RESOLUTION_ENABLED = True`, because the env-derived default may be off. It then asserts that a `resolution_record` row exists after the accept, which shows the sidecar was exercised rather than skipped.
  - The guard and claim concurrency tests, using two threads and separate units of work.
  - The `_m3` / `turn_summary` aggregate test against seeded rows, including a `NULL` `reason_code` folding to `reason:`.
  - The outage-propagation test.
  - The three twins named above.
- [ ] `tests/test_reference_resolution.py` — UPDATE. The file has no conformance marker today, so it never runs on Postgres. Opt in `TestResolvedStatus`, `TestAssumedStatus`, `TestEvidenceGapStatus`, `TestIndeterminateStatus`, `TestValidFromMatrix`, `TestResolutionLogWriteFailure`, `TestEmptyOrInvalidInput`, `TestDegradedTagLiteral`, `TestKillSwitchParity` and `TestContextReachesJournalEntry` with `pytest.mark.conformance` plus `pytest.mark.usefixtures("backend")`. A plan-time grep found no direct Redis access in the file (no `keys`, `scan_iter`, `get_REDIS_DB` or `_redis`). The builder re-checks each class before marking it. Any class that turns out to need Redis gets `redis_only` with a reason.
- [ ] `tests/test_query_thread_safety.py` references these names. Re-run it on both legs. No change is expected.

## Rabbit Holes

- **A Postgres counter table for `turn_summary`.** A `popoto_*` counter engine table bumped inside the guarded statement could mirror the Redis hash, but it adds a second structure that can disagree with the rows. The indexed `GROUP BY` answers the same question from the rows themselves. Do not build the table.
- **Auto-repairing drifted Redis hashes on read.** Calling `rebuild_turn_summary` from `turn_summary`, or on a schedule, turns an O(1) read into a scan and puts an undeclared-key script on the hot path. Repair stays an explicit operator call, and the CHANGELOG tells operators when to run it.
- **Generalising the summary into a reusable "maintained rollup" primitive.** One consumer does not justify it.
- **Making `DecisionLog` follow the memory model's `Meta.backend`.** Threading a model or backend through `DecisionLog()` or `AuditableExtractionConfig` changes public signatures and splits the trail from the journal, which follows the process default. Defer it to the open question.
- **Unifying `_RedisRecorder` into a shared fixture.** It already exists twice, and a third copy, or an import from `test_postgres_recipes.py`, is fine. A refactor is a separate chore.
- **The stale MemoryTelemetry `Meta.ttl` sentence** in the same `postgres-backend.md` paragraph. When the builder deletes the SubconsciousMemory sentence and the remaining text is clearly stale, they should fix it in the same edit. They should not go auditing other doc paragraphs.
- **Retention, TTL or a stale-`pending` sweeper for decision rows.** That belongs to M9 (#568).

## Risks

### Risk 1: The Redis path drifts beyond the intended summary fix
**Impact:** Apart from the summary block, the Redis path must stay byte-identical. A refactor that, say, moves `get_REDIS_DB()` to lazy resolution changes when the client is captured.
**Mitigation:**
- Each method gets one early `if self._backend is not None` return, and the Python Redis bodies stay textually unchanged. Review the diff hunk by hunk.
- The whole existing suite stays green on the Redis leg.
- `CLAIM_RELEASE_LUA` is untouched. In `TERMINAL_WRITE_LUA` only the summary block changes. A Verification row confirms that the refusal branch, the four `SADD`s and the row `HSET` are unchanged.

### Risk 1b: The Redis summary fix itself
**Impact:** The new Lua could mis-decrement, for example by decoding the prior reason wrongly, double-counting when nothing changed, or driving a field negative. That would make `_last_extraction_privacy_dropped` wrong, or make the summary disagree with the rows in a new way.
**Mitigation:**
- The conformance tests in Test Impact pin every transition shape on both legs against a rollup computed from the rows.
- `HDEL` at `<= 0` means no field is ever negative, and a Redis-only test seeds a drifted hash to prove it.
- The change is visible to Redis users, so it gets a CHANGELOG **Fixed** entry naming the old behaviour, the new behaviour and the `rebuild_turn_summary` repair.
- Rolling upgrade: a pre-fix process writing to the same hash still increments only on first terminal write. The two scripts touch disjoint cases and neither can drive a field negative, so mixed fleets cannot corrupt the hash beyond the old drift, and `rebuild_turn_summary` repairs it once every process is upgraded. The CHANGELOG says to upgrade every process first.

### Risk 2: The `_qq` lease rename regresses the question queue
**Impact:** The question queue's propose lock stops working on Postgres.
**Mitigation:** `_qq` `lock`/`release` stay registered and point at the renamed functions. Run `tests/postgres/test_postgres_recipes.py` and the question-queue tests on the Postgres leg.

### Risk 3: The guarded upsert's column list diverges from what `save()` writes
**Impact:** A row written by `write_terminal` reads back differently through the ORM. Examples: a wrong `null` encoding for `span_start`, or a missing `_updated_at` or `_migrated_from` reset.
**Mitigation:**
- Build the column list from `TableSpec.field_types` with `to_column_value`, the same converter `save()` uses, and mirror save's `_updated_at` / `_migrated_from = NULL` updates.
- `test_lua_created_row_is_visible_to_the_orm_query_api`, which runs on both legs, checks the read-back.

### Risk 4: Mixed-backend configurations
**Impact:** Today the refusal fires whenever the *memory model* is non-Redis. After this change the decision log follows `DecisionRecord`'s backend, which is the process default. That changes the outcome for three configurations:

| Memory model | Process default | Today | After |
|---|---|---|---|
| postgres | postgres | refused | runs, all-Postgres (the goal) |
| postgres (`Meta.backend`) | redis | refused | **still refused** (narrow guard, below) |
| redis (`Meta.backend`) | postgres | decision log in Redis via `get_REDIS_DB()` | decision log in Postgres, co-located with the default journal and `ResolutionRecord`, which already followed the default |
| postgres | postgres, but the journal uses a custom `entry_model` on Redis (`Meta.backend="redis"`) | refused | **still refused** (explicit `entry_model` clause in the guard) |

**Mitigation:**
- Keep a narrow refusal in `SubconsciousMemory.__init__`, inside the existing `non_redis_backend(model_class) is not None` branch. It raises when `non_redis_backend(DecisionRecord)` is `None`, or when `DecisionRecord`'s store and the journal entry model's store disagree. That case is exactly the mixed shape the old guard existed to prevent, and it is a strict subset of today's raise, so no configuration that constructs today starts raising.
- The third row is a data-location change for an unusual configuration, and it is a fix: the decision log now sits with the journal it reconciles against. Call it out in the CHANGELOG. The maintainer confirmed that a CHANGELOG callout is enough (Open Question 3, resolved).
- The narrowed refusal was approved by the maintainer as planned (Open Question 1, resolved).

## Race Conditions

### Race 1: Two terminal writers on one candidate
**Location:** `DecisionLog.write_terminal` → `_m3_terminal_write` (`backends/postgres/recipes.py`)
**Trigger:** An assembly runner writes `accept` with an `entry_id` while a retried `reject` for the same candidate is in flight.
**Data prerequisite:** The row's `_pk` is deterministic from the three KeyFields.
**State prerequisite:** The guard's read of the existing `state`/`entry_id` and the write must be atomic.
**Mitigation:**
- The guard is a single statement under `_record_locked`.
- `ON CONFLICT DO UPDATE` locks the conflicting row even when the `WHERE` is false, so the second writer waits and then sees the committed accept.
- A two-thread test asserts that the accept survives and that exactly one writer gets `False`.

### Race 2: Two assembly runners claim the same candidate
**Location:** `DecisionLog.acquire_claim` → `_lease_lock`
**Trigger:** Two processes run `assemble` for the same accepted candidate concurrently.
**Data prerequisite:** none
**State prerequisite:** At most one unexpired lease per claim key.
**Mitigation:**
- `INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expires_at <= now RETURNING 1` makes the primary key the arbiter.
- The release is token-checked.
- `test_concurrent_claim_racing_runners_produce_exactly_one_entry` runs on both legs.

### Race 3: `write_pending` racing a terminal write
**Location:** `assemble` → `write_pending` (ORM `save`) vs `write_terminal`
**Trigger:** A crashed runner's lease expires while a new runner writes `pending` over a row another runner just wrote terminal.
**Data prerequisite:** none
**State prerequisite:** The same as Redis today. `assemble` re-reads with `get` after claiming and short-circuits on a terminal row.
**Mitigation:**
- The behavior is unchanged from Redis. `save()` and the guarded write share the record advisory lock, so they serialize.
- The claim TTL bound (`M3_ASSEMBLY_CLAIM_TTL_MS`) is the same.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #568] Retention or TTL for decision rows, a stale-`pending` sweeper, and decision-log dashboards. These are M9 work, and rows stay unbounded on Postgres just as on Redis.
- [ORDERED] Making the decision log follow a memory model's own `Meta.backend` instead of the process default. The maintainer approved the process-default rule plus the narrow mixed-shape refusal (Open Question 1), so this stays out unless a host asks for it.
- [DESTRUCTIVE] Migrating existing Redis decision rows into Postgres. The one-off Redis→Postgres transfer path owns that, and it should be reviewed separately before it runs. Nothing in this plan copies rows across stores.

## Update System

No update system changes are required.
- popoto is a library, so nothing needs deploying.
- Postgres creates the `decision_record` table through the existing lazy model-table path.
- The `popoto_lease` table already exists as an engine table.
- No new dependencies, configuration or environment variables are added.
- Existing Redis installs see no change.

## Agent Integration

No agent integration is required. `SubconsciousMemory` is already the host-facing entry point. Harness integrations (`popoto.integrations`) construct it the same way, and they gain Postgres support for `auditable_extraction=` without any wiring change. No MCP or tool surface changes.

## Documentation

### Feature Documentation
- [ ] `docs/features/postgres-backend.md`:
  - Remove the `SubconsciousMemory(auditable_extraction=…)` sentence from "Not on Postgres yet" (lines 843-848).
  - Add a "Decision log" subsection. It should cover:
    - the guarded upsert;
    - the claim on `popoto_lease`;
    - ORM readers;
    - `turn_summary` as a `GROUP BY` over the detail rows, under the same contract as Redis;
    - the process-default rule and the narrowed mixed-shape refusal;
    - the zero-Redis test name.
  - If the MemoryTelemetry `Meta.ttl` sentence in the same paragraph is stale since #783, fix it in the same edit. Otherwise leave it.
- [ ] `docs/features/auditable-extraction.md`:
  - Add a "Backends" section stating that the decision log runs on Redis and on Postgres, and naming the store-selection rule (process default, same as the journal).
  - Rewrite the summary paragraph (lines 187-190) to state the contract exactly: counts of the turn's rows by current terminal state and by reason, one of each per candidate, with a candidate that moves between terminal states counted under its new state only. Say how each backend meets it (a hash maintained atomically in the terminal-write script on Redis, an aggregate query on Postgres), and document `rebuild_turn_summary` as the repair for hashes written before the fix.
  - In "The terminal-write conflict guard" section, mention that the same script maintains the summary.
- [ ] `docs/guides/subconscious-memory-recipe.md`: the auditable section (around line 251) does not mention the Redis-only limit, so add one sentence noting Postgres support.
- [ ] `CHANGELOG.md` `[Unreleased]` → `### Added`: add an entry that covers:
  - the auditable extraction decision log on Postgres (#811);
  - the narrowed refusal;
  - the data-location change for a Redis-`Meta.backend` memory model under a Postgres process default (Risk 4, approved by the maintainer as a CHANGELOG-only callout).
- [ ] `CHANGELOG.md` `[Unreleased]` → `### Fixed`: a separate entry for the Redis `turn_summary` fix (#811). It should state:
  - the old behaviour: a candidate whose terminal state or reason changed after its first terminal write stayed counted under the first one, so the summary could disagree with the detail rows (for example `state:reject: 1` for a row that is now `accept`);
  - the new behaviour: the summary always equals the rollup of current terminal states, on Redis and on Postgres;
  - who is affected: only rows written through `write_terminal` more than once with a different outcome. `_last_extraction_privacy_dropped` could previously be set from a stale `firewall_drop` count;
  - the repair: call `DecisionLog().rebuild_turn_summary(agent_id, turn_id)` for turns written before the upgrade, after every process runs the new version.

### External Documentation Site
- [ ] Run `mkdocs build --strict` (or `scripts/ci-local.sh docs`) to confirm the docs still build.

### Inline Documentation
- [ ] Update the `DecisionLog` class docstring and the `list_for_agent` / `list_pending` / `turn_summary` docstrings to describe both backends. The `turn_summary` docstring states the contract (current terminal states, one count per candidate, zero keys absent).
- [ ] Rewrite the comment above the summary block in `TERMINAL_WRITE_LUA` (today "counts each candidate once: bump only when the row is new or still non-terminal 'pending'") to describe decrement-old / increment-new. Add a docstring for `rebuild_turn_summary` and a header comment for `TURN_SUMMARY_REBUILD_LUA`, including the undeclared-key note.
- [ ] Add docstrings for `_m3_terminal_write` and `_lease_lock` / `_lease_release` that name the Redis structure each one replaces, in the same style as the existing `_qq_lock` docstring.

## Success Criteria

- [ ] On an all-Postgres process, `SubconsciousMemory(auditable_extraction=...)` constructs, `decision_log` is not `None`, and `extract_memories` returns `ExtractedFact`s.
- [ ] Both mixed shapes still raise `BackendCapabilityError`, and each has a test:
  - the memory model has `Meta.backend="postgres"` and the process default is Redis;
  - the journal's custom `entry_model` is in a different store from `DecisionRecord`.
- [ ] A zero-Redis test (`_RedisRecorder`) runs this whole sequence on Postgres with no Redis command and no connection checkout:
  - empty turn;
  - firewall drop;
  - reject;
  - withhold;
  - accept with a real journal append;
  - duplicate assembly;
  - claim contention;
  - `list_pending`;
  - `turn_summary`;
  - `compute_metrics`.
- [ ] Guard parity:
  - A terminal write over an accept-with-entry row returns `False`, sets only `detail_code="terminal_conflict_refused"`, and does not raise.
  - `pending` → terminal transitions in place, so each candidate has one row.
- [ ] Claim parity:
  - Exactly one of two concurrent claimers wins.
  - Release is token-checked.
  - The claim expires after `Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`.
- [ ] `get`, `list_for_agent`, `list_pending`, `turn_summary` and `compute_metrics` agree across the legs on the shared suite.
- [ ] `turn_summary` has one contract on both backends: after any sequence of writes it equals the rollup of the rows' current terminal states. The terminal-to-terminal, reason-change, same-verdict-retry, multi-candidate, empty-reason and refused-write conformance tests pass on **both** legs.
- [ ] On Redis, no summary field is ever negative, and `rebuild_turn_summary` restores a seeded drifted hash to the rollup. On Postgres, `rebuild_turn_summary == turn_summary`.
- [ ] No backend divergence is documented anywhere: `grep -rn "divergen" docs/features/ CHANGELOG.md src/popoto/extraction/decision_log.py` finds nothing about `turn_summary`.
- [ ] The CHANGELOG has a **Fixed** entry for the Redis `turn_summary` change, naming `rebuild_turn_summary`.
- [ ] Fail-open parity tests exist for each of these:
  - verdict-provider failure;
  - `ResolutionLog.write` failure;
  - journal-blocked assembly;
  - other append failure;
  - a refused terminal write.

  Outage propagation is tested on Postgres.
- [ ] `tests/test_auditable_extraction.py` runs on both legs. Every `redis_only` test names its reason and its Postgres twin. The Redis leg is green and unchanged in behavior.
- [ ] Apart from the summary block in `TERMINAL_WRITE_LUA` and the new `TURN_SUMMARY_REBUILD_LUA`, the Redis implementation is byte-identical: `CLAIM_RELEASE_LUA`, the refusal branch, the `SADD`s, the row `HSET`, the key helpers, and the `write_terminal` call's KEYS/ARGV layout are unchanged.
- [ ] The Postgres leg ran rather than skipped: every Postgres pytest command reports zero skips for Postgres reasons under `-rs`, with `POSTGRES_URL=postgresql://localhost:5432/postgres` against Postgres 18.
- [ ] The question-queue tests stay green on both legs after the lease generalisation.
- [ ] `ruff check src/`, `black --check src/ tests/` and `scripts/mypy_ratchet.py` pass. No new `POPOTO_REDIS_DB` snapshot imports are added, and the one at `tests/test_auditable_extraction.py:41` is removed.
- [ ] Tests pass (`/do-test`).
- [ ] Documentation is updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (postgres-adapters)**
  - Name: pg-adapter-builder
  - Role: Add the `_m3` / `terminal_write` and `_lease` adapters in `backends/postgres/recipes.py`, keeping the `_qq` aliases.
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Builder (decision-log dispatch)**
  - Name: decision-log-builder
  - Role: Add the `DecisionLog` backend dispatch and the `_pg_*` helpers, fix the Redis summary block in `TERMINAL_WRITE_LUA`, add `rebuild_turn_summary` (both backends), and narrow the refusal in `SubconsciousMemory`.
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Test engineer (parity)**
  - Name: parity-test-engineer
  - Role: Convert `tests/test_auditable_extraction.py` to conformance, write the `turn_summary` contract tests that run on both legs, and write the Postgres twins plus the zero-Redis, concurrency and outage tests.
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: decision-log-validator
  - Role: Run both legs, lint, black and the mypy ratchet, and diff-review that the Redis path is byte-identical.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: decision-log-docs
  - Role: Update the docs and the CHANGELOG.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Postgres adapters
- **Task ID**: build-pg-adapters
- **Depends On**: none
- **Validates**: tests/postgres/test_postgres_recipes.py (question-queue lease regression). Task 3 creates `tests/postgres/test_postgres_decision_log.py`, which validates the new adapters end to end.
- **Informed By**: spike-2 (the guarded upsert returns `(1,0)` when written and `(0,1)` when refused)
- **Assigned To**: pg-adapter-builder
- **Agent Type**: builder
- **Parallel**: true
- Optionally rename `_qq_lock` / `_qq_release` to `_lease_lock` / `_lease_release`, registering them under `LEASE_FIELD = "_lease"` and keeping them under `QQ_FIELD`. Reusing the `_qq` `lock` / `release` ops directly is equally acceptable.
- Add `M3_FIELD = "_m3"` with two ops, `terminal_write` and `turn_summary`. `turn_summary` runs the `GROUP BY` aggregate from Key Elements and returns the `(state, reason_code, count)` rows. `terminal_write` builds the spike-2 statement from `DecisionRecord`'s `TableSpec` through `to_column_value`, wraps it in `_record_locked`, mirrors `save()`'s `_updated_at` / `_migrated_from` handling, and returns `bool`.
- Leave the model-level-store tuple at the tail of `_recipe_field_call` (`recipes.py:228-234`) unchanged. `_m3` and `_lease` are not model-level stores, so like `_qq` they receive `(spec, field, ...)`.

### 2. DecisionLog dispatch and refusal narrowing
- **Task ID**: build-decision-log
- **Depends On**: build-pg-adapters
- **Validates**: tests/test_auditable_extraction.py (both legs), tests/postgres/test_postgres_recipes.py
- **Informed By**: spike-1 (the ORM reads work unchanged), spike-3 (`ResolutionLog` works)
- **Assigned To**: decision-log-builder
- **Agent Type**: builder
- **Parallel**: false
- Change `DecisionLog.__init__` to resolve `self._backend` and assign `self._redis` only on the Redis path.
- Give `write_terminal`, `acquire_claim`, `release_claim`, `get`, `list_for_agent` and `turn_summary` an early `_pg_*` branch. Leave the Redis bodies textually unchanged.
- Fix the summary block in `TERMINAL_WRITE_LUA` exactly as in Technical Approach (decrement-old / increment-new, `HDEL` at `<= 0`, prior `''` treated as new). Leave the rest of the script and the `run_lua` call unchanged.
- Add `TURN_SUMMARY_REBUILD_LUA` and `DecisionLog.rebuild_turn_summary(agent_id, turn_id) -> Dict[str, int]`, with a Postgres branch returning `turn_summary(...)`.
- Implement `_pg_turn_summary` over the `_m3` / `turn_summary` op. Fold the rows into `state:<s>` and `reason:<reason_code or "">`, the reason counted unconditionally, with no zero keys.
- Before touching the tests, re-run the Problem section's three-write repro on Redis DB 15 (`REDIS_URL=redis://localhost:6379/15` set before import). It must print `{'state:accept': 1, 'reason:accepted': 1}` after the fix.
- Narrow the `SubconsciousMemory.__init__` refusal to the split-trail case and update its message. Use the exact guard shape in Technical Approach ("Refusal guard"): the check stays inside the existing `non_redis_backend(model_class)` branch, and it also refuses a custom `entry_model` whose store differs from `DecisionRecord`'s.
- Update the docstrings.

### 3. Parity and Postgres tests
- **Task ID**: build-tests
- **Depends On**: build-decision-log
- **Validates**: tests/test_auditable_extraction.py, tests/postgres/test_postgres_decision_log.py, tests/postgres/test_postgres_recipes.py
- **Assigned To**: parity-test-engineer
- **Agent Type**: test-engineer
- **Parallel**: false
- Add the conformance markers to the five storage classes, and to the ten `tests/test_reference_resolution.py` classes listed in Test Impact.
- Add the six `turn_summary` contract tests to `TestDecisionLogCore` (both legs), and the Redis-only `test_rebuild_turn_summary_repairs_a_drifted_hash` with its Postgres twin.
- Remove the `POPOTO_REDIS_DB` snapshot import and make `_rows_for` backend-agnostic.
- Mark the three Redis-structure tests `redis_only`, each with a reason that names its twin.
- Create `tests/postgres/test_postgres_decision_log.py` with:
  - the three twins;
  - the zero-Redis full-flow test;
  - the two-thread guard and claim tests;
  - the `_m3` / `turn_summary` aggregate test (`NULL` reason folds to `reason:`);
  - the outage-propagation test;
  - the `ResolutionLog`-failure fail-open test.
- Replace the refusal test with a positive test. Add two mixed-shape refusal tests, one for each shape:
  - the memory model on Postgres under a Redis default;
  - a custom `entry_model` whose `Meta.backend` differs from `DecisionRecord`'s.
- Fix the docstring of `test_a_key_tier_lifecycle_is_refused_at_construction_on_postgres`.

### 4. Validate
- **Task ID**: validate-decision-log
- **Depends On**: build-tests
- **Assigned To**: decision-log-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table in the worktree venv (`.[dev,embeddings,benchmark,mcp,postgres]`), with Redis on DB 15 and `POSTGRES_URL=postgresql://localhost:5432/postgres` (Postgres 18). Confirm that no Postgres test skipped.
- Diff-review `decision_log.py`: `CLAIM_RELEASE_LUA` and the key helpers are unchanged; in `TERMINAL_WRITE_LUA` only the summary block changed; the Python Redis method bodies are unchanged below the new branch.
- State the environment (redis-py, mypy, Postgres versions) alongside every count.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-decision-log
- **Assigned To**: decision-log-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Apply every item in the Documentation section, then run the docs build.

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: decision-log-validator
- **Agent Type**: validator
- **Parallel**: false
- Re-run the Verification table and confirm every Success Criterion.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Auditable suite, both legs | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/test_auditable_extraction.py tests/test_reference_resolution.py -q -rs` | exit code 0, no Postgres skips |
| `turn_summary` contract runs on the Postgres leg | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/test_auditable_extraction.py -q -k "summary and postgres" --co` | output > 0 collected |
| Reference-resolution suite actually collects a Postgres leg | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/test_reference_resolution.py -q -k postgres --co` | output > 0 collected |
| Postgres decision-log + recipes | `POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/postgres/test_postgres_decision_log.py tests/postgres/test_postgres_recipes.py -q -rs` | exit code 0, 0 skipped |
| Full suite (Redis leg) | `pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet | `scripts/mypy_ratchet.py` | exit code 0 |
| No stale snapshot import in the auditable tests | `grep -c "from popoto.redis_db import POPOTO_REDIS_DB" tests/test_auditable_extraction.py` | match count == 0 |
| Refusal test inverted | `grep -c "def test_the_auditable_extraction_path_is_refused_on_postgres" tests/postgres/test_postgres_recipes.py` | match count == 0 |
| No `redis.call` removed outside the summary block | `git diff origin/main -- src/popoto/extraction/decision_log.py \| grep '^-' \| grep 'redis\.call' \| grep -vc HINCRBY` | match count == 0 |
| No backend divergence documented | `grep -rn "divergen" docs/features/auditable-extraction.md docs/features/postgres-backend.md CHANGELOG.md src/popoto/extraction/decision_log.py` | match count == 0 for `turn_summary` |
| CHANGELOG Fixed entry | `grep -c "rebuild_turn_summary" CHANGELOG.md` | output > 0 |
| Doc sentence removed | `grep -c "decision log in Redis" docs/features/postgres-backend.md` | match count == 0 |
| Zero-Redis test exists | `grep -c "_RedisRecorder" tests/postgres/test_postgres_decision_log.py` | output > 0 |
| CHANGELOG entry | `grep -c "#811" CHANGELOG.md` | output > 0 |

## Critique Results

Verdict: **READY TO BUILD (with concerns)**. Revision pass applied 2026-10-07T17:06:10Z.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | war room | The same-store guarantee holds only for the default `JournalEntry`. The journal's store comes from `journal.entry_model` (`decision_log.py:733`), which `provenance_journal.py:596` lets a caller override. | Architectural Impact; Technical Approach ("Refusal guard"); Risk 4 (new row); Open Question 1; Task 2; Task 3; Success Criteria | Guard explicitly, inside the existing `non_redis_backend(model_class) is not None` branch: `jb = non_redis_backend(getattr(cfg.journal, "entry_model", None) or JournalEntry)`. Refuse when `non_redis_backend(DecisionRecord) is None` or `(db is None) != (jb is None)`. `_reconcile_pending` works across stores through the entry model's ORM. |
| CONCERN | war room | The claim-TTL twin would sleep 30s (`M3_ASSEMBLY_CLAIM_TTL_MS=30_000`, `constants.py:588`). | Test Impact (`test_claim_carries_a_finite_ttl`) | `acquire_claim` reads the constant at call time (`decision_log.py:583`). Monkeypatch it to about 50ms and sleep 0.1s, or set `expires_at = extract(epoch from clock_timestamp()) - 1`. `expires_at` is in epoch seconds (`recipes.py:109-111`), so compare it with `time.time() + TTL_MS/1000` within a tolerance. |
| CONCERN | war room | `tests/test_reference_resolution.py` has no conformance marker, so it never ran on Postgres. | Test Impact; Task 3; Verification (new collect row); zero-Redis flow test | Opt in the ten listed classes with `conformance` plus `usefixtures("backend")`. The plan-time grep found no direct Redis access. The zero-Redis flow test monkeypatches `Defaults.M4_RESOLUTION_ENABLED=True` and asserts that a `resolution_record` row exists. |
| NIT | war room | The Redis Lua always increments `reason:{reason}`, even when the reason is empty (`decision_log.py:513-516`). | Key Elements (`turn_summary`); Task 2 | Count `reason:<reason_code or "">` unconditionally. |
| NIT | war room | The "Stale snapshot" bullet placed the line-41 import in `decision_log.py`. | Technical Approach ("Stale snapshot (tests only)") | The import is in `tests/test_auditable_extraction.py:41`. `decision_log.py:92` already imports `get_REDIS_DB`. |
| NIT | war room | Task 1 called the `recipes.py:228-234` tuple a record-lock list. | Technical Approach (lease bullet); Task 1 | The tuple decides whether `field` is passed to the handler. `_qq` is not in it, so `_m3` and `_lease` stay out and receive `(spec, field, ...)`. |
| NIT | war room | Task 1's "Validates" named a file that Task 3 creates, and the `_lease` alias is optional. | Task 1 | "Validates" now names only `test_postgres_recipes.py`. The rename is optional; reusing the `_qq` `lock` / `release` ops directly is acceptable. |

---

## Open Questions

1. **Store-selection rule.** The plan keys the decision log on `DecisionRecord`'s backend, which is the process default and the same rule the journal and `ResolutionRecord` follow. It keeps a narrowed `BackendCapabilityError` for two cases. The first is a memory model on Postgres via `Meta.backend` while the process default is Redis. The second is a journal whose custom `entry_model` sits in a different store from `DecisionRecord`. Outside those cases, the same-store guarantee holds only for the default `JournalEntry`. Is that the right cut, or should the mixed shape be allowed and documented instead?
2. **`turn_summary` divergence.** On Postgres the summary is derived from the detail rows, so it always reflects current terminal states. Redis counts each candidate's first terminal write only. Is that divergence acceptable for a "convenience index"? The alternative is a counter engine table that reproduces Redis's first-write-only counting, which the #759 doctrine discourages.
3. **Data-location change.** A memory model with `Meta.backend="redis"` under a Postgres process default would move its decision log from Redis to Postgres, co-locating it with the journal. Is a CHANGELOG callout enough, or should that shape keep its decision log in Redis?
