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
TBD

## Prior Art
TBD

## Research
TBD

## Spike Results
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
