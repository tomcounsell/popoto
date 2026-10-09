---
status: Planning
type: bug
appetite: Small
owner: valorengels
created: 2026-10-09
tracking: https://github.com/tomcounsell/popoto/issues/832
last_comment_id:
revision_applied: true
revision_applied_at: 2026-10-09T09:41:42Z
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
- `src/popoto/migrate_redis_to_postgres/__init__.py:1528-1535` `_connect` (callers: `preflight_target` 1714, `verify` 2803, the load at 3530, the final run-record update at 3594), `1693-1699` `_connect_autocommit` (callers `_main_role` 1561, `_lock_target_schema` 1659) -- raw `psycopg.connect`, no exception mapping.
- `src/popoto/migrate_redis_to_postgres/__init__.py:1568-1587` -- `_check_maintenance_dsn` maps only `MaintenanceDsnMismatchError`/`MaintenanceConnectionError`; a dead *main* DSN escapes as `BackendUnavailableError`.
- `src/popoto/migrate_redis_to_postgres/__init__.py:3330-3341` -- the run directory is claimed (`run.json`) *before* the first Postgres connect (`_lock_target_schema`).
- `src/popoto/integrations/cli.py:149-184` -- `_cmd_hook`; `187-214` `_bound_postgres_waits` (the precedent for hook-process-only tuning).
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

### Key Elements

- **Scoped redaction**: `_redact` rewrites only DSN-shaped text. A message with no DSN shape in it passes through byte-for-byte, whatever the password is.
- **Exit code `4` for an unreachable Postgres**: the migration tool maps a failed connect, and any `BackendUnavailableError`, to `PostgresUnreachable` -> one `UNREACHABLE: ...` stderr line, exit 4. An unparseable DSN becomes `REFUSED` (exit 2).
- **One stderr line from a hook**: the backend stops double-logging its outage library-wide; the hook process additionally quiets the two outage records that duplicate its own warning.

### Flow

Hook on dead Postgres -> backend `_fail` (throttled log, quiet exception) -> service `_record_failure` (one WARNING + one `POPOTO_MEMORY_LOG` line) -> exit 0, stderr = 1 line.

Migration on dead Postgres -> `_connect*` raises `PostgresUnreachable` (or the backend raises `BackendUnavailableError`) -> `main()` prints `UNREACHABLE: ...` -> exit 4 -> operator reruns with `--resume` and the same `--run-dir`.

### Technical Approach

**1. `_redact` (`src/popoto/backends/postgres/__init__.py:394`).**
- Remove the whole-message `message.replace(spelling, "***")` loop (lines 410-412).
- Add a URL-span pattern (`\b[a-z][a-z0-9+.\-]*://\S+`, same scheme shape as `_URL_USERINFO`). For each URL span in the message, replace the parsed password's spellings (`password`, `quote(password, safe="")`, `quote_plus(password)`) with `***` *inside the span's password part only*, via `re.sub` with a function. This keeps spike-1's `postgresql://u:p@ss@h/d` case correct, which `_URL_USERINFO` alone gets wrong (it stops at the first `@`).
- **The password part is the text between the first `:` after `://` and the *last* `@` of the span** (second critique, concern 2). In code: `head, at, tail = span.rpartition("@")`; if there is no `@`, return the span unchanged; otherwise `scheme, sep, rest = head.partition("://")`, `user, colon, pw = rest.partition(":")`, replace the spellings in `pw` only, and reassemble. Replacing across the whole span is not enough: with password `a`, `postgresql://u:a@data:5432/app` becomes `postgresql://u:***@d***t***:5432/***pp`, garbling the host and database of a quoted DSN. Replacing in everything before the last `@` (the critique's suggested fix) fixes the host and database, but still garbles a username that contains the password: `postgresql://app:a@data:5432/app` -> `postgresql://***pp:***@data:5432/app`. Prototyped at revision time against both variants: the password-part version gives `x postgresql://app:***@data:5432/app y` and `x postgresql://u:***@data.example/a y`, and it still passes spike-1's multi-`@` case (`a postgresql://app:p@ss:w/rd@db/agents b postgresql://app:p%40ss%3Aw%2Frd@db/agents` -> `a postgresql://app:***@db/agents b postgresql://app:***@db/agents`) and the `s3cr3t-hunter2` case.
- Then apply `_PASSWORD_KEYWORD` and `_URL_USERINFO` to the whole message, unchanged. `_PASSWORD_KEYWORD` already replaces the whole `password=` value (quoted or bare), so keyword spans need no spelling pass.
- The unparseable-DSN branch (`return INVALID_DSN_MESSAGE`) is unchanged.
- Update the docstring: "anything DSN-shaped" replaces "the parsed password itself ... anywhere", and say why (a short password otherwise rewrites prose).
- Safety argument, verified at plan time: with the bare loop deleted, all 10 runnable `_LEAK_CASES` tests (sync and async, every case) stay green; only the unit test that asserted bare-prose replacement fails (spike in Test Impact). libpq does not echo the password of a DSN it parsed, and `_require_parseable_dsn` withholds the text of one it could not parse.

**2. Migration exit code (`src/popoto/migrate_redis_to_postgres/__init__.py`).**
- Add `class PostgresUnreachable(MigrationError)` beside `InventoryStop` (~line 169), with a docstring that names exit code 4.
- `_connect` and `_connect_autocommit`: keep `_require_parseable_dsn(dsn)`, but catch its `psycopg.OperationalError` and raise `MigrationRefused(INVALID_DSN_MESSAGE) from None`. The message is already password-free. Wrap `psycopg.connect(...)` so that `psycopg.OperationalError` raises `PostgresUnreachable(f"could not connect to Postgres ({_describe_dsn(dsn)}): {_redact(...)}")` from the original exception.
- **Collapse whitespace in these two wraps too** (second critique, concern 1). `_connect` (`migrate_redis_to_postgres/__init__.py:1528`) and `_connect_autocommit` (:1693) call `psycopg.connect` directly, so they never reach `_fail` and its whitespace collapse. A refused TCP connect on Linux libpq ends with `\n\tIs the server running on that host and accepting TCP/IP connections?`, which would make SC4's one-line assertion fail on Linux CI (macOS, where the spikes ran, printed one line). Build the text as `text = " ".join(str(exc).split())`, then `_redact(f"{type(exc).__name__}: {text}", dsn)`. This is the same shape as `_RecordingConnection.connect` (`backends/postgres/__init__.py:430-431`). Put it in one small helper used by both wraps, so the two cannot drift.
- **Use the backend's `_import_psycopg()` in both wraps instead of a bare `import psycopg`** (second critique, nit c). Without this, a missing driver gives two different exits. With a maintenance DSN set, `_check_maintenance_dsn` reaches `_require_parseable_dsn` -> `_import_psycopg()` first, raises `BackendUnavailableError("the Postgres backend needs psycopg and psycopg_pool: ...")` (`backends/postgres/__init__.py:439-447`), and exits 4. Without one, `_connect_autocommit`'s bare `import psycopg` raises a raw `ImportError`: traceback, exit 1. After the change, a missing driver is exit 4 on both paths, with the install hint in the `UNREACHABLE:` line.
- `_check_maintenance_dsn` (~line 1568): immediately after the `maintenance_dsn is None` early return, call `_require_parseable_dsn` on **both** `dsn` and `maintenance_dsn`, mapping `psycopg.OperationalError` to `MigrationRefused(INVALID_DSN_MESSAGE) from None`. Without this, an unparseable main DSN with `POPOTO_POSTGRES_MAINTENANCE_URL` set exits 4, not 2: the parse check inside `PostgresBackend._open_dedicated` (`backends/postgres/__init__.py:1267`) sits inside the `try` whose `except psycopg.OperationalError` (:1272) routes to `_fail` -> `BackendUnavailableError` -> `UNREACHABLE`/4 (critique concern).
- `main()` (~line 3840): add `except (PostgresUnreachable, BackendUnavailableError) as exc:`, then `print(f"UNREACHABLE: Postgres could not be used: {exc}. Any rows already loaded are kept; fix the connection and rerun with --resume and the same --run-dir.", file=sys.stderr)` and `return 4`. The wording deliberately does not say "could not be reached" or "nothing more was written": the same exit also covers auth failure, a missing database, and an outage mid-load (critique nit 1). Import `BackendUnavailableError` lazily inside `main` or at module top, following the module's existing import style. This catch also covers the maintenance-DSN probe (spike-2's `_check_maintenance_dsn` traceback) and a backend outage during the load, where `--resume` is already the documented continuation.
- The `--resume` hint is load-bearing: spike-2 showed the failed run has already claimed `run.json`, so a plain rerun is `REFUSED`.
- Update `run_migration`'s docstring "Raises" list.

**3. Hook stderr.**
- *Library, `src/popoto/backends/types.py:80`*: `BackendError.__init__(self, message, *, log: bool = True)`. When `log` is false, set `self.message` and skip `PopotoException.__init__`'s `logger.error`; always set `self.args = (message,)`. *`backends/postgres/__init__.py:983`*: `_fail` returns `BackendUnavailableError(..., log=False)` and a comment explaining that the outage was just logged, throttled, three lines above. `_fail` also collapses whitespace in the embedded `{exc}` text (`" ".join(str(exc).split())`) before building the message, so a multi-line libpq error still yields the one-line `UNREACHABLE:` and hook-warning outputs the tests assert (critique nit 2). This makes the documented "logged at ERROR once per window, however many calls fail" true. No other raise site changes.
- *Hook process only, `src/popoto/integrations/cli.py`*: a new `_quiet_outage_duplicates()`, called in `_cmd_hook` right after `_bound_postgres_waits()`, with the same `try/except Exception: pass` shape and a docstring in the same style. It does two things:
  - (a) `logging.getLogger("psycopg.pool").setLevel(logging.ERROR)`. The pool's connect-retry WARNINGs vary from 1 to N lines with timing, and the last attempt's text is already inside the hook's warning.
  - (b) It adds a `logging.Filter` to `POPOTO.postgres` that drops only the outage record, matched by a prefix on the rendered text: `record.getMessage().startswith(OUTAGE_LOG_PREFIX)` (critique nit 3: robust to a future change in how `_fail` passes args, unlike `record.msg is ...`). Hoist the fixed leading text of `_fail`'s format string (`"popoto Postgres backend unavailable ("`) to a module constant `OUTAGE_LOG_PREFIX` in `backends/postgres/__init__.py` and build the format from it. Every other `POPOTO.postgres` record still reaches stderr.
  - **Decision: keep the filter; do not raise `POPOTO.postgres` to CRITICAL** (second critique, nit b). Raising the logger level would be simpler, but it also drops the backend's other hook-relevant records, such as the `recovered after N failure(s)` WARNING. So keep the prefix filter, and keep the prefix next to the format string so they cannot drift. Define `OUTAGE_LOG_PREFIX` directly above `_fail`. Make `_fail`'s `logger.error` call pass `OUTAGE_LOG_PREFIX + "%s); %d consecutive ..."` as its format, so the format string itself starts with the constant. Add a one-line comment on the constant: the hook's `_quiet_outage_duplicates` matches on it.
- This is the same "only the hook subcommand changes it" rule `_bound_postgres_waits` documents. The MCP server, the Hermes plugin and host applications are untouched by (a) and (b).
- Result: the hook's stderr is the one `POPOTO.integrations` warning. The full error, including `last connection attempt: ...`, is still in that warning and in the `POPOTO_MEMORY_LOG` line.


## Failure Path Test Strategy

### Exception Handling Coverage
- `_cmd_hook`'s `except Exception: output = None` and `_quiet_outage_duplicates`'s own `except Exception: pass` are unchanged in kind. The observable behavior is pinned by the stderr line-count assertions (exactly one line, and it is the documented warning) plus the existing `POPOTO_MEMORY_LOG` checks.
- The migration tool's new `except` in `main()` is pinned by exit-code and stderr assertions (below).

### Empty/Invalid Input Handling
- `_redact` with no password in the DSN, or with a message that has no DSN shape: it must return the message unchanged (asserted).
- Unparseable DSN for the migration tool: `REFUSED`, exit 2, message is `INVALID_DSN_MESSAGE`, no traceback (asserted), both with and without `POPOTO_POSTGRES_MAINTENANCE_URL` set.
- Multi-line connect error: `_fail` collapses whitespace, and so do the migration tool's `_connect`/`_connect_autocommit` wraps. So the hook warning and the `UNREACHABLE:` line stay one line. That is asserted in SC4 via a single-line check on stderr, and, independently of the platform's libpq wording, by SC4b's unit test.

### Error State Rendering
- Every new and changed message is asserted verbatim or by prefix: the `UNREACHABLE:` line, the `--resume` hint, the absence of `Traceback`, and the absence of the garbled `***` inside ordinary words.


## Test Impact

- [ ] `tests/postgres/test_postgres_outage.py::test_redact_removes_the_dsn_password_from_a_message`: UPDATE. Two assertions pin the old every-occurrence contract and must move to DSN-shaped inputs. Spike: deleting the bare loop turned exactly this test red, and nothing else among the 12 selected.
  - `_redact("auth failed for s3cr3t-hunter2", dsn) == "auth failed for ***"` becomes `_redact("dsn postgresql://app:s3cr3t-hunter2@db.internal:5432/agents", dsn) == "dsn postgresql://app:***@db.internal:5432/agents"`.
  - `_redact("bad p%40ss%3Aw%2Frd and p@ss:w/rd", url_form) == "bad *** and ***"` becomes `_redact("a postgresql://app:p@ss:w/rd@db/agents b postgresql://app:p%40ss%3Aw%2Frd@db/agents", url_form) == "a postgresql://app:***@db/agents b postgresql://app:***@db/agents"`. This is the spike-1 multi-`@` case.
  - The remaining assertions are unchanged.
- [ ] `tests/postgres/test_postgres_outage.py::test_error_is_logged_once_per_window`: UPDATE. Today it filters `r.name == "POPOTO.postgres"`, which hides the `POPOTO-REDIS_DB` duplicate (spike-3). Widen it to `caplog.at_level(logging.ERROR)` with no logger argument, and count records at any logger whose `getMessage()` contains `"unavailable"`. Expect 1 after three failing calls, and 2 after one more with the window at 0.
- [ ] `tests/postgres/test_postgres_harness.py::test_the_hook_survives_postgres_down_and_says_which_backend`: UPDATE. Add `lines = run.stderr.splitlines()`, `assert len(lines) == 1, run.stderr`, and `assert lines[0].startswith("popoto memory injected_read failed (backend: postgres): ")`. Also assert that `h.memory_log` has exactly one line, containing `last connection attempt`. Before adding the memory-log assertion, read `harness_e2e.Harness` to confirm the attribute name.
- [ ] `tests/postgres/test_postgres_harness.py::test_the_hook_fails_open_within_its_budget_on_a_silent_postgres` and `::test_the_hook_fails_open_within_its_budget_on_a_locked_table`: UPDATE. Add the same one-line-stderr assertion. The locked-table case goes through `_fail` with a statement timeout and no pool retry, so it pins the library half (`log=False`) independently of the psycopg.pool half.
  - **Before adding the assertion, list each case's actual stderr** (second critique, nit a). The spikes only measured the refused-port case. For the silent server (connect timeout and `PoolTimeout`) and the locked table (statement timeout), the stderr records have not been observed. Run each test once on `main` with `print(run.stderr)` and once with the fix. Record every line and its logger in the PR description, in the shape of spike-3's table. If a line other than the documented warning survives the fix (for example a `psycopg.pool` `couldn't stop thread` record at pool close, which is a WARNING and so is covered by (a)), decide explicitly: either the fix covers it, or the test asserts the documented warning is the *last* line and the PR says why. Never weaken the assertion silently.
  - The validator runs SC8's three reverts against these two tests as well as the refused-port one. It records which reverts turn each test red. A revert that leaves one of them green is expected only where the PR's stderr listing explains why (for example, the locked-table case has no pool retry, so the `psycopg.pool` revert may leave it green).
- All other `_LEAK_CASES` tests: unchanged, and they must stay green. That is the "keep the leak tests green" acceptance item.


## Rabbit Holes

- **Reordering `run_migration` so the run directory is claimed only after the first successful connect.** It would remove the need for `--resume`, but it reshuffles the refusal order that the docs and existing tests pin. The `--resume` hint is enough.
- **Mapping every `psycopg.OperationalError` raised mid-statement during the load to exit 4.** Only connect failures and `BackendUnavailableError` are mapped. A statement that fails on a live connection is a different failure and keeps today's documented "underlying error of a failed load" behavior.
- **Making `PopotoException` stop auto-logging in general.** Many call sites rely on it. Only `_fail`'s raise opts out, because it has just logged the same text.
- **A minimum-length threshold for bare-text redaction.** That would be a magic number, and it still garbles a short password that happens to be a whole word. Scoping by DSN shape avoids the question entirely.


## Risks

### Risk 1: A message echoes a parsed password outside any DSN shape
**Impact:** after the change, that password would print unredacted.
**Mitigation:** libpq does not echo the password of a DSN it parsed, and an unparseable DSN's text is withheld whole (`INVALID_DSN_MESSAGE`). Every `_LEAK_CASES` surface (str, repr, traceback, health, logs; sync and async) stays green with the bare loop removed, as measured at plan time. The URL-span spelling pass still catches the password wherever a DSN is quoted.

### Risk 2: Quieting `psycopg.pool` in the hook hides a non-outage pool warning
**Impact:** an operator reading only hook stderr misses pool chatter.
**Mitigation:** this applies only to the hook subprocess, whose documented stderr contract is a single warning. Any failure that matters still reaches `_record_failure` (the warning plus `POPOTO_MEMORY_LOG`). The MCP server and in-process callers keep full logging.


## Race Conditions

No race conditions identified. The logging changes run once per hook process before any backend use, and the exit-code mapping is synchronous in `main()`.


## No-Gos (Out of Scope)

Nothing deferred -- every relevant item is in scope for this plan. (The items listed under Rabbit Holes are deliberate non-changes to existing documented behavior, not deferred work.)


## Update System

No update system changes required. This is library code shipped in the next release, with no new dependencies, config or migration steps.


## Agent Integration

No agent integration required. The hook and the migration CLI are existing entry points, and their behavior changes in place.


## Documentation

- [ ] `docs/features/redis-to-postgres-migration.md`, "Exit codes and refusals": add a row `4`, "Postgres could not be used (connect, auth or missing-database failure, an outage mid-load, or the Postgres driver is not installed). One `UNREACHABLE:` line. Any rows already loaded are kept; fix the connection and rerun with `--resume` and the same `--run-dir`." Add "a Postgres DSN that cannot be parsed" to the row `2` cause list, saying to fix the DSN and rerun with `--resume` and the same `--run-dir` (the failed run already claimed it; critique nit 5). "Backend not configured" belongs in row `2`, not row `4`, and the doc should not list it under `4`: the tool reads `POPOTO_POSTGRES_URL` itself and refuses an unset one with `MigrationRefused` (`migrate_redis_to_postgres/__init__.py:3316-3317`), and it never calls `backend_from_env()`, so the backend's "not set" `BackendUnavailableError` (`backends/postgres/__init__.py:251`) cannot reach exit 4 (verified at revision time; second critique, nit c).
- [ ] `docs/features/postgres-backend.md` (~line 2080, "The outage is logged at ERROR once per ..."): add that the raised `BackendUnavailableError` does not log again, so a host app's logs carry one outage line per window.
- [ ] `docs/features/harness-integration.md` (~line 338): the contract text is already right. Add one sentence saying the hook process does not print `psycopg.pool`'s retry warnings or the backend's own outage line, because the hook's warning carries the same error.
- [ ] `CHANGELOG.md` `[Unreleased]`: under `### Fixed`, redaction scope and one-line hook stderr plus the once-per-window outage log. Under `### Changed` (critique nit 4), the exit-code contract change naming old and new codes: unreachable/unusable Postgres 1 (traceback) -> 4 (`UNREACHABLE:`), and unparseable DSN 1 (traceback) -> 2 (`REFUSED:`). Each entry links #832.
- [ ] Docstrings: `_redact`, `BackendError.__init__`, `PostgresUnreachable`, `_quiet_outage_duplicates`, `run_migration`.


## Success Criteria

Each criterion names the test that proves it and the revert that turns that test red.

- [ ] **SC1, prose is never rewritten.** New `tests/postgres/test_postgres_outage.py::test_redact_leaves_prose_alone_for_a_short_password` (DSN `postgresql://u:a@h:1/d`) asserts three things:
  - `"PoolTimeout: couldn't get a connection after 5.00 sec"` comes back unchanged.
  - `"OperationalError: connection failed"` comes back unchanged.
  - `"host=h password=a b"` becomes `"host=h password=*** b"`.

  *Ablation:* restoring the bare `message.replace` loop turns it red (spike-1 outputs).
- [ ] **SC2, DSN-shaped spellings are still redacted, including multi-`@`.** The updated `test_redact_removes_the_dsn_password_from_a_message`. *Ablation:* dropping the URL-span spelling pass (leaving only `_URL_USERINFO`) leaves `ss:w/rd@db/agents` in the output, and the test goes red.
- [ ] **SC2b, a quoted DSN keeps its host, database and username with a short password.** In `test_redact_leaves_prose_alone_for_a_short_password` (or a sibling test), assert:
  - `_redact("x postgresql://u:a@data.example/a y", "postgresql://u:a@h/d") == "x postgresql://u:***@data.example/a y"`
  - `_redact("x postgresql://app:a@data:5432/app y", "postgresql://app:a@h/d") == "x postgresql://app:***@data:5432/app y"`

  *Ablations:* replacing the spellings across the whole span (no `rpartition("@")`) gives `x postgresql://u:***@d***t***.ex***mple/*** y`, and the test goes red. Replacing in everything before the last `@` (no `partition(":")` of the userinfo) gives `x postgresql://***pp:***@data:5432/app y`, and the second assertion goes red. Both outputs were prototyped at revision time.
- [ ] **SC3, no leak regresses.** All `test_a_connection_error_never_contains_the_password[*]` and `test_an_async_connection_error_never_contains_the_password[*]` cases are green.
- [ ] **SC4, exit 4 on an unreachable Postgres.** New `tests/postgres/test_migrate_redis_to_postgres.py::test_an_unreachable_postgres_exits_4_with_one_line`. It uses a fake RDB (a file starting `REDIS0011`), a fixture model, and `POPOTO_POSTGRES_URL=postgresql://app:a@127.0.0.1:1/agents`, then calls `mig.main([...])`. It asserts:
  - the return value is 4;
  - stderr is exactly one line, it starts `UNREACHABLE: Postgres could not be used: ` and contains `--resume`;
  - `Traceback` does not appear;
  - `Oper***` does not appear.

  It then reruns with `--resume` and the same `--run-dir`, and asserts the return value is 4 again (not 2), which proves the hint is actionable. *Ablation:* deleting the `except (PostgresUnreachable, BackendUnavailableError)` clause makes `main` raise, and the test goes red. Deleting only the `_connect_autocommit` wrap lets a raw `psycopg.OperationalError` escape, and the test also goes red.
- [ ] **SC4b, a multi-line connect error becomes one line, on any platform.** New unit test `tests/postgres/test_migrate_redis_to_postgres.py::test_a_multi_line_connect_error_is_one_line`. It monkeypatches `psycopg.connect` to raise `psycopg.OperationalError("a\n\tb")` and calls `_connect` and `_connect_autocommit` with a parseable DSN. For each one it asserts that `PostgresUnreachable` is raised, its `str()` contains no `\n` or `\t`, and it ends with `OperationalError: a b`. This pins concern 1 without depending on which libpq wording the CI host prints. *Ablation:* building the message from `str(exc)` instead of the collapsed text turns it red.
- [ ] **SC4c, a missing driver exits 4, with or without a maintenance DSN.** New unit test `test_a_missing_driver_is_unreachable`. It does two things, and both are needed:
  - It hides the driver with `monkeypatch.setitem(sys.modules, "psycopg", None)`.
  - It monkeypatches `popoto.backends.postgres._import_psycopg` to raise the real `BackendUnavailableError` install-hint message.

  With only the patch, a bare `import psycopg` still succeeds and `_require_parseable_dsn` then raises the patched error, so the test cannot tell the two import styles apart. The wraps must look `_import_psycopg` up at call time (function-local import, as `_require_parseable_dsn` is today), so the patch reaches them. Call `mig.main([...])` without a maintenance DSN. Assert the return value is 4, and that stderr starts `UNREACHABLE:` and contains `pip install`. *Ablation:* restoring the bare `import psycopg` in `_connect_autocommit` gives an `ImportError` traceback, and the test goes red.
- [ ] **SC5, the maintenance-DSN path also exits 4.** Same test file, `test_an_unreachable_main_dsn_with_a_maintenance_dsn_exits_4`, with `POPOTO_POSTGRES_MAINTENANCE_URL` on port 2. *Ablation:* removing `BackendUnavailableError` from the `except` tuple turns it red (spike-2's traceback).
- [ ] **SC6, an unparseable DSN is `REFUSED`.** `test_an_unparseable_postgres_dsn_is_refused` (`postgresql://app:Pa%zz@127.0.0.1:1/agents`): returns 2, stderr has `REFUSED: invalid connection string`, and `Pa%zz` does not appear. *Ablation:* removing the `_require_parseable_dsn` -> `MigrationRefused` mapping turns it red.
- [ ] **SC6b, an unparseable main DSN is `REFUSED` even with a maintenance DSN set.** `test_an_unparseable_postgres_dsn_with_a_maintenance_dsn_is_refused`: same `Pa%zz` main DSN plus `POPOTO_POSTGRES_MAINTENANCE_URL=postgresql://app:b@127.0.0.1:2/agents`. Asserts return 2 (not 4), stderr starts `REFUSED: invalid connection string`, and `Pa%zz` does not appear. *Ablation:* removing the new `_require_parseable_dsn` calls at the top of `_check_maintenance_dsn` routes the parse failure through `_open_dedicated` -> `_fail`, returns 4, and the test goes red.
- [ ] **SC7, the outage is logged once per window library-wide.** The updated `test_error_is_logged_once_per_window`. *Ablation:* dropping `log=False` at `_fail` gives 4 records instead of 1, and the test goes red.
- [ ] **SC8, the hook writes exactly one stderr line.** The three updated hook tests in `tests/postgres/test_postgres_harness.py`. *Ablations:* each one alone turns the refused-port test red:
  - dropping the `psycopg.pool` level change (>= 1 pool warning: a refused port fails every attempt);
  - dropping the `POPOTO.postgres` filter (the first outage in a fresh process always logs);
  - dropping `log=False`.
- [ ] Tests pass (`/do-test`), including `tests/postgres` with `POSTGRES_URL` set.
- [ ] Documentation updated (`/do-docs`).
- [ ] `ruff check src/`, `black --check src/ tests/`, and `scripts/mypy_ratchet.py` all pass.


## Team Orchestration

### Team Members

- **Builder (polish)**
  - Name: polish-builder
  - Role: implement all three fixes and their tests
  - Agent Type: builder
  - Domain: security/untrusted-input (redaction)
  - Resume: true

- **Validator (polish)**
  - Name: polish-validator
  - Role: run the ablations in Success Criteria and confirm each one turns its named test red, then restore
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: polish-docs
  - Role: Documentation section items
  - Agent Type: documentarian
  - Resume: true


## Step by Step Tasks

### 1. Scoped redaction
- **Task ID**: build-redact
- **Depends On**: none
- **Validates**: `tests/postgres/test_postgres_outage.py`
- **Informed By**: spike-1 (multi-`@` case needs the in-span spelling pass)
- **Assigned To**: polish-builder
- **Agent Type**: builder
- **Parallel**: true
- Implement Technical Approach 1 (spelling pass limited to the userinfo password part: last `@`, first `:` after `://`). Update `test_redact_removes_the_dsn_password_from_a_message`, and add `test_redact_leaves_prose_alone_for_a_short_password` with the SC1 and SC2b assertions.

### 2. Migration exit code 4
- **Task ID**: build-migrate-exit
- **Depends On**: none
- **Validates**: `tests/postgres/test_migrate_redis_to_postgres.py` (SC4-SC6 tests)
- **Informed By**: spike-2 (three entry paths; run dir is claimed first)
- **Assigned To**: polish-builder
- **Agent Type**: builder
- **Parallel**: true
- Implement Technical Approach 2, including the parse check at the top of `_check_maintenance_dsn`, the shared whitespace-collapsing helper, and `_import_psycopg()` in both connect wraps. Add the SC4-SC6b tests, including SC4b and SC4c. Pick a model from `tests/postgres/migrate_fixtures.py` and put each `--run-dir` under `tmp_path`.

### 3. Hook stderr and the once-per-window log
- **Task ID**: build-hook-stderr
- **Depends On**: none
- **Validates**: `tests/postgres/test_postgres_outage.py::test_error_is_logged_once_per_window`, `tests/postgres/test_postgres_harness.py`
- **Informed By**: spike-3 (four loggers; the pool warning count varies)
- **Assigned To**: polish-builder
- **Agent Type**: builder
- **Parallel**: true
- Implement Technical Approach 3 (`BackendError(log=)`, whitespace collapse in `_fail`, `OUTAGE_LOG_PREFIX` defined directly above `_fail` with a `getMessage()` prefix filter, `_quiet_outage_duplicates`) and the test updates. Before asserting one line in the silent-server and locked-table tests, list their observed stderr records (Test Impact).

### 4. Ablation validation
- **Task ID**: validate-ablations
- **Depends On**: build-redact, build-migrate-exit, build-hook-stderr
- **Assigned To**: polish-validator
- **Agent Type**: validator
- **Parallel**: false
- For each ablation in SC1, SC2, SC2b, SC4-SC6b (including SC4b and SC4c) and SC7-SC8: apply the revert, run the named test, confirm it is red, then restore. Run with `POSTGRES_URL` set so the `pg`-fixture hook tests do not skip. A skip is not a red.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-ablations
- **Assigned To**: polish-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Every item in the Documentation section.

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: polish-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table and confirm every Success Criterion.


## Verification

| Check | Command | Expected |
|-------|---------|----------|
| PG-only tests pass | `POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/postgres/test_postgres_outage.py tests/postgres/test_postgres_harness.py tests/postgres/test_migrate_redis_to_postgres.py -q` | exit code 0 |
| Full suite | `pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet | `scripts/mypy_ratchet.py` | exit code 0 |
| Bare-replace loop gone | `grep -c 'message = message.replace(spelling' src/popoto/backends/postgres/__init__.py` | match count == 0 |
| Maintenance path parse-checks first | `pytest tests/postgres/test_migrate_redis_to_postgres.py -k maintenance_dsn_is_refused -q` | exit code 0 |
| Exit code 4 documented | `grep -c '^| .4. |' docs/features/redis-to-postgres-migration.md` | output > 0 |


## Critique Results
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | critique | With `POPOTO_POSTGRES_MAINTENANCE_URL` set, an unparseable main DSN exits 4, not 2: `_require_parseable_dsn` inside `_open_dedicated`'s `try` (:1267) is caught at :1272 -> `_fail` -> `BackendUnavailableError` -> `UNREACHABLE`/4. | Technical Approach 2 (`_check_maintenance_dsn` bullet); SC6b; Task 2; Verification row | Parse-check both DSNs right after the `maintenance_dsn is None` return; map `OperationalError` to `MigrationRefused(INVALID_DSN_MESSAGE) from None`. Ablation: drop the calls -> exit 4. |
| NIT | critique | `UNREACHABLE` wording ("could not be reached", "nothing more was written") is wrong for auth/missing-db/mid-load. | Technical Approach 2 (`main()` bullet); SC4; Documentation row 4 | "Postgres could not be used: ... Any rows already loaded are kept; fix the connection and rerun with --resume and the same --run-dir." |
| NIT | critique | `_fail` embeds `{exc}` verbatim; a multi-line libpq message breaks the one-line assertions. | Technical Approach 3 (library bullet); Failure Path; Task 3 | Collapse whitespace in `_fail`. |
| NIT | critique | `record.msg is OUTAGE_LOG_FORMAT` identity filter is brittle. | Technical Approach 3 (b); Task 3 | Prefix match on `record.getMessage()` against `OUTAGE_LOG_PREFIX`. |
| NIT | critique | CHANGELOG should list the exit-code changes under Changed. | Documentation (CHANGELOG item) | `### Changed`: 1 -> 4 for unreachable, 1 -> 2 for unparseable DSN. |
| NIT | critique | Exit-2 docs row should say how to continue after fixing the DSN. | Documentation (migration doc item) | Rerun with `--resume` and the same `--run-dir`. |
| CONCERN | critique (2nd) | SC4's one-line `UNREACHABLE` assertion likely fails on Linux CI: `_connect`/`_connect_autocommit` call `psycopg.connect` directly, never reach `_fail`'s whitespace collapse, and Linux libpq appends `\n\tIs the server running on that host and accepting TCP/IP connections?`. | Technical Approach 2 (collapse bullet); SC4b; Failure Path; Task 2 | Verified: both wraps are a bare `psycopg.connect` (:1528, :1693). One shared helper builds `" ".join(str(exc).split())` before `_redact`, as at `backends/postgres/__init__.py:430`. SC4b patches `psycopg.connect` to raise `OperationalError("a\n\tb")`. Revert: use `str(exc)` -> red. |
| CONCERN | critique (2nd) | A short password still garbles a quoted DSN: password `a` turns `postgresql://u:a@data:5432/app` into `postgresql://u:***@d***t***:5432/***pp`. | Technical Approach 1 (password-part bullet); SC2b; Task 1 | Prototyped. The suggested `rpartition("@")` fix still garbles a username containing the password (`***pp:***@`), so the spelling pass is limited to the userinfo password part (first `:` after `://` up to the last `@`). Spans with no `@` are untouched. The multi-`@` spike-1 case still passes. Two assertions, two reverts. |
| NIT | critique (2nd) | The builder must list stderr records for the silent-server and locked-table hook cases before asserting one line, and the validator must run SC8's reverts on them. | Test Impact (silent/locked bullet); Task 3 | List each logger and line in the PR. Any surviving extra line needs an explicit decision, never a silently weakened assertion. Record which reverts turn each test red. |
| NIT | critique (2nd) | If the log filter stays, keep `OUTAGE_LOG_PREFIX` next to the format string, or pick the simpler CRITICAL-level alternative. | Technical Approach 3 ((b) decision bullet); Task 3 | Keep the filter, because CRITICAL would also hide the `recovered after` WARNING. Define the constant directly above `_fail`, and have the `logger.error` format start with it. |
| NIT | critique (2nd) | Exit-4 docs row: either list "backend not installed or configured", or state that the migration tool can't reach that case. Verify which. | Technical Approach 2 (`_import_psycopg` bullet); Documentation (migration doc item); SC4c | Verified. "Not installed" reaches exit 4 only with a maintenance DSN set (through `_import_psycopg`); without one it is a raw `ImportError`. Both wraps now use `_import_psycopg()`, so it is exit 4 either way and listed in row 4. "Not configured" cannot reach 4 (the tool refuses an unset URL itself, exit 2), so it is not listed. |

---

## Open Questions

None blocking. One judgment call is recorded for the critic: the third
stderr line is fixed in the library (`_fail` stops double-logging) rather
than only in the hook process. That changes what a host application's logs
show during an outage (one ERROR per window instead of one per failed call).
This matches the documented contract, but it is the one change here visible
outside the hook and the migration CLI.

