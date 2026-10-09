---
status: Planning
type: bug
appetite: Small
owner: valorengels
created: 2026-10-09
tracking: https://github.com/tomcounsell/popoto/issues/832
last_comment_id:
---

# Post-1.10.0 polish: short-password redaction, migration exit code, hook stderr

## Problem

Three non-blocking notes from the 1.10.0 release review (PR #821). Nothing
leaks; each is a readability or contract-accuracy defect, and each was
reproduced on `main` at plan time (commands and output in Spike Results).

**Current behavior:**

1. **Short passwords garble error messages.** `_redact()` replaces *every*
   occurrence of the parsed password text. With the password `a`, a pool
   timeout reads `PoolTimeout: couldn't get *** connection ***fter 5.00 sec`;
   the keyword form garbles the keyword itself (`host=h p***ssword=*** b`);
   and the hook/migration path prints `Oper***tion***lError: connection
   f***iled`.
2. **The migration tool exits 1 with a traceback when it cannot reach
   Postgres.** The documented exit-code table
   (`docs/features/redis-to-postgres-migration.md`, "Exit codes and
   refusals") reserves `1` for "the load finished but verification found a
   mismatch". An operator script that branches on `1` reads an unreachable
   server as a data mismatch. The same traceback-exit-1 happens for a DSN
   libpq cannot parse.
3. **A hook on an unreachable Postgres writes 4+ stderr lines.** The harness
   doc (`docs/features/harness-integration.md`, "On Postgres" and "Failure
   behavior") promises "exit 0, no output, one log line, and a stderr
   warning". The actual stderr is a `psycopg.pool` retry warning (one *or
   more*: the count depends on how many retries fit in the 1 s budget), the
   backend's throttled outage ERROR, an unthrottled duplicate of the same
   text from `PopotoException`'s auto-log, and finally the one documented
   warning. Exit code is 0, as documented.

   The third line is not hook-specific: every `BackendUnavailableError` the
   backend raises from `_fail` logs its message at ERROR through
   `PopotoException.__init__` (logger `POPOTO-REDIS_DB`), on every call,
   which makes the documented "the outage is logged at ERROR once per
   `Defaults.PG_OUTAGE_LOG_WINDOW_SECONDS`, however many calls fail"
   (`docs/features/postgres-backend.md`) false for any host application with
   logging configured. `test_error_is_logged_once_per_window` does not catch
   this because it filters to `r.name == "POPOTO.postgres"`.

**Desired outcome:**

1. Redaction touches only DSN-shaped text (URL userinfo, `password=` values,
   and the password's spellings inside those spans). Ordinary prose is never
   rewritten, so a short password cannot garble a message. Every existing
   leak case stays green.
2. A migration run that cannot connect to Postgres exits with a new,
   documented code `4` and one `UNREACHABLE: ...` stderr line (password
   redacted), no traceback. An unparseable DSN is a `REFUSED` (exit `2`).
3. A hook on an unreachable Postgres writes exactly one stderr line (the
   documented `popoto memory ... failed (backend: postgres): ...` warning),
   exit 0, with the full detail still in `POPOTO_MEMORY_LOG`. The backend's
   outage ERROR is logged once per window library-wide, as documented.


## Freshness Check

**Baseline commit:** `fe971a44` (plan skeleton committed on top as `f8f7e32f`)
**Issue filed at:** 2026-10-08T12:12:17Z
**Disposition:** Unchanged

The issue carries no `## Recon Summary` and no file:line references, so the
recon was done at plan time; every claim below was re-verified by reading the
code and by running it (Spike Results).

**File:line references verified at plan time (baseline `fe971a44`):**
- `src/popoto/backends/postgres/__init__.py:391-392` -- `_PASSWORD_KEYWORD`, `_URL_USERINFO` regexes.
- `src/popoto/backends/postgres/__init__.py:394-415` -- `_redact()`; lines 410-412 are the every-occurrence `message.replace(spelling, "***")` loop.
- `src/popoto/backends/postgres/__init__.py:431, 967, 1280` -- the three `_redact` call sites (connect recorder, `_fail`, maintenance-DSN error).
- `src/popoto/backends/postgres/__init__.py:956-983` -- `_fail()`: throttled `logger.error` (logger `POPOTO.postgres`, line 975) then `return BackendUnavailableError(...)` (line 983).
- `src/popoto/backends/types.py:73-82` -- `BackendError.__init__` calls `PopotoException.__init__`, which logs at ERROR (`src/popoto/redis_db.py:174-176`, logger `POPOTO-REDIS_DB`).
- `src/popoto/migrate_redis_to_postgres/__init__.py:3807-3858` -- `main()`: catches `InventoryStop` (3), `MigrationRefused` (2), `KeyboardInterrupt` (130); anything else escapes as a traceback, exit 1.
- `src/popoto/migrate_redis_to_postgres/__init__.py:1528-1535` `_connect`, `1693-1699` `_connect_autocommit` -- raw `psycopg.connect`, no exception mapping.
- `src/popoto/migrate_redis_to_postgres/__init__.py:1568-1587` -- `_check_maintenance_dsn` maps only `MaintenanceDsnMismatchError`/`MaintenanceConnectionError`; a dead *main* DSN escapes as `BackendUnavailableError`.
- `src/popoto/migrate_redis_to_postgres/__init__.py:3330-3341` -- the run directory is claimed (`run.json`) *before* the first Postgres connect (`_lock_target_schema`).
- `src/popoto/integrations/cli.py:149-182` -- `_cmd_hook`; `183-216` `_bound_postgres_waits` (the precedent for hook-process-only tuning).
- `src/popoto/integrations/service.py:1144-1160` -- `_record_failure`: the one documented `logger.warning` plus the `POPOTO_MEMORY_LOG` line.

**Cited sibling issues/PRs re-checked:**
- PR #821 (1.10.0 release review) -- merged; the source of these notes. Introduced `_redact`, `_last_connect_error`, and the leak tests.

**Commits on main since the issue was filed, touching referenced files:**
- `ff8d15d2` fix(#833) -- removed a dead helper in `backends/postgres/__init__.py`; irrelevant (none of the cited functions changed).
- `3923eb62` fix(#830) -- Redis pending-write Lua; irrelevant.

**Active plans in `docs/plans/` overlapping this area:** none (`transfer_db0_fence_rebind.md` is `popoto-transfer`, a different CLI).


## Prior Art

- **PR #821** (1.10.0 release): added `_redact`, `_require_parseable_dsn`, `_last_connect_error` and `tests/postgres/test_postgres_outage.py`'s `_LEAK_CASES`. This plan narrows `_redact`'s scope without weakening any of those cases.
- **#814** (harness on Postgres): added `(backend: postgres)` to the hook warning and `_bound_postgres_waits()`, the hook-process-only tuning this plan extends.
- **#816**: `OUTAGE_ERRORS` / `BackendUnavailableError` short-circuit in the hook.
- **#756 / #829**: the migration tool and its exit-code table (`3` for an inventory stop).

No prior attempt at any of the three fixes; no "Why Previous Fixes Failed" section.


## Research

No relevant external findings needed: the work is internal. The one
third-party fact the plan depends on -- that `psycopg_pool` emits its
connect-retry warnings on the logger named `psycopg.pool` -- was verified
empirically (Spike Results, spike-3), not from docs.


## Spike Results

All spikes were run directly at plan time against baseline `fe971a44`, with
`REDIS_URL=redis://localhost:6379/7` (never DB 0) and Postgres on refused port
`127.0.0.1:1`.

### spike-1: Short-password garbling is real and has three shapes
- **Method**: prototype (`python -c` calling `_redact`)
- **Finding**:
  - `_redact("PoolTimeout: couldn't get a connection after 5.00 sec", "postgresql://u:a@h:1/d")` -> `PoolTimeout: couldn't get *** connection ***fter 5.00 sec`
  - `_redact("host=h password=a b", "postgresql://u:a@h:1/d")` -> `host=h p***ssword=*** b`
  - `_redact("at postgresql://u:p@ss@h/d", "postgresql://u:p%40ss@h/d")` -> `at postgresql://u:***@h/d` -- correct **only because** the bare-replace loop runs first; `_URL_USERINFO` alone stops at the first `@` and would leave `ss@h/d`. So dropping the bare loop outright regresses this case; the spellings must still be replaced *inside DSN-shaped spans*.
- **Confidence**: high
- **Impact on plan**: scoped replacement (Technical Approach 1), not regex-only.

### spike-2: The migration tool exits 1 with a traceback on a dead Postgres
- **Method**: prototype (fake RDB = file starting `REDIS0011`, a one-field model, `POPOTO_POSTGRES_URL=postgresql://app:a@127.0.0.1:1/agents`)
- **Finding**:
  - `python -m popoto.migrate_redis_to_postgres --rdb dump.rdb --run-dir run1 --source-id t --model mymodels:M832Note` -> traceback ending `psycopg.OperationalError: connection failed: connection to server at "127.0.0.1", port 1 failed: could not receive data from server: Connection refused`, **exit 1**. Raised from `_lock_target_schema` -> `_connect_autocommit`.
  - Re-running the same command -> `REFUSED: run1 already holds run <id>; pass --resume to continue it, or use a new --run-dir`, exit 2: the failed run had already claimed the run directory. The new message must therefore tell the operator to rerun with `--resume` and the same `--run-dir`.
  - With `POPOTO_POSTGRES_MAINTENANCE_URL` also set (port 2): traceback ending `popoto.backends.types.BackendUnavailableError: Postgres is unavailable: Oper***tion***lError: connection f***iled: ...`, exit 1 -- raised through `_check_maintenance_dsn` (main DSN probed via `PostgresBackend._open_dedicated` -> `_fail`), and also showing spike-1's garbling.
  - Unparseable DSN `postgresql://app:Pa%zz@127.0.0.1:1/agents` -> traceback ending `psycopg.OperationalError: invalid connection string: ...` (the `INVALID_DSN_MESSAGE`), exit 1.
- **Confidence**: high
- **Impact on plan**: three entry paths to map (Technical Approach 2).

### spike-3: Hook stderr on a dead Postgres is 4+ lines from 4 loggers
- **Method**: prototype (`echo '{"hook_event_name":"UserPromptSubmit",...}' | env POPOTO_BACKEND=postgres POPOTO_POSTGRES_URL=postgresql://127.0.0.1:1/nowhere ... python -m popoto.integrations.cli hook`), then again under `logging.basicConfig` with `%(name)s` to attribute each line.
- **Finding**: exit 0, stdout empty, stderr:
  1. `psycopg.pool` WARNING `error connecting in 'popoto-NNN': ...` -- **1 line in the first run, 2 in the second**: the pool retries inside the 1 s budget, so the count is timing-dependent.
  2. `POPOTO.postgres` ERROR `popoto Postgres backend unavailable (...); 1 consecutive failure(s) ... Logged once per 60s.` (`backends/postgres/__init__.py:975`)
  3. `POPOTO-REDIS_DB` ERROR `Postgres is unavailable: ...` (`redis_db.py:176`, `PopotoException.__init__` auto-log of the `BackendUnavailableError`)
  4. `POPOTO.integrations` WARNING `popoto memory injected_read failed (backend: postgres): ...` -- the documented line.

  `POPOTO_MEMORY_LOG` got exactly one line carrying the full error (including `last connection attempt: ...`). The same hook on a dead **Redis** prints exactly one stderr line (the documented warning), confirming the doc describes the intended contract and the code drifted.
- Library-wide check: three `PostgresBackend._run("SELECT 1")` calls on a dead DSN under `basicConfig(level=ERROR)` produced **one** `POPOTO.postgres` record and **three** `POPOTO-REDIS_DB` records -- the throttle in `_fail` is defeated by the exception's own auto-log.
- **Confidence**: high
- **Impact on plan**: fix line 3 in the library (it is a real throttle bug), quiet lines 1-2 only in the hook process (Technical Approach 3). Fixing the code, not the doc, is the right side: the Redis leg already meets the doc.


## Data Flow

1. **Connect failure** (pool worker thread or `psycopg.connect`) -> `_RecordingConnection.connect` stores `_redact(...)` in `_last_connect_error`; `psycopg.pool` logs its own retry WARNING.
2. **`_fail`** builds `last_error` (+ last connection attempt), runs `_redact` per DSN, logs throttled ERROR on `POPOTO.postgres`, returns `BackendUnavailableError` -> `PopotoException.__init__` logs again on `POPOTO-REDIS_DB`.
3. **Hook**: `MemoryService._record_failure` logs the documented WARNING and appends the `POPOTO_MEMORY_LOG` line; `_cmd_hook` exits 0. With no handler configured, every WARNING+ record reaches stderr via `logging.lastResort`.
4. **Migration tool**: `run_migration` -> `_check_maintenance_dsn` / `_lock_target_schema` / `preflight_target` / `_main_role` -> `_connect[_autocommit]` -> `psycopg.connect`; an unmapped exception escapes `main()`.


## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: new `MigrationError` subclass `PostgresUnreachable` and exit code `4` (additive, documented). `BackendError.__init__` gains a keyword-only `log: bool = True` (default preserves behavior for every other raise site). `_redact`'s contract narrows from "every occurrence" to "DSN-shaped text" -- private function.
- **Coupling**: unchanged. The hook-only logging adjustment lives beside `_bound_postgres_waits` in `integrations/cli.py`, the existing home for hook-process-only tuning.
- **Data ownership**: unchanged.
- **Reversibility**: each of the three fixes is an independent, small revert.


## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0
- Review rounds: 1


## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| psycopg installed | `python -c "import psycopg, psycopg_pool"` | Every new test lives under `tests/postgres/` and imports psycopg |
| Local Postgres 18 for `pg`-fixture tests | `pg_isready` | The hook end-to-end test uses `pg_schema`; set `POSTGRES_URL` (e.g. `postgresql://localhost:5432/postgres`) or it skips |
| `redis-server` binary | `which redis-server` | `tests/postgres/test_migrate_redis_to_postgres.py` is `pytestmark = needs_redis_server` |


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
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions
TBD
