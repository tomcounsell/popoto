---
status: Planning
type: bug
appetite: Medium
owner: Solo dev
created: 2026-10-08
tracking: https://github.com/tomcounsell/popoto/issues/814
last_comment_id:
---

# Harness Integration on Postgres

## Problem

The harness integration (`popoto-memory` hook, `MemoryService`, MCP server,
`doctor`, `demo`) is the path a coding agent actually runs. On a process bound
to Postgres (`POPOTO_BACKEND=postgres`) the memory model itself is
Postgres-native (spike-1: zero Redis commands for save / extract / assemble /
observe / count / get_many / delete), but the layer around it is not. Under
#759's rule that a Postgres process sends Redis **zero** commands, three
defects remain:

1. **Service side state is raw Redis.** `MemoryService` keeps all of its own
   bookkeeping through the `redis` property (`src/popoto/integrations/service.py:228-232`,
   function-local `from ..redis_db import POPOTO_REDIS_DB`):
   - operation counters `$popoto_memory:counter:{agent}:{op}`: `INCR` in
     `_record_failure` and `_touch`, `SCAN`+`GET` in `_read_counters`;
   - last-success stamps `$popoto_memory:last:{agent}:{op}`: `SET` in `_touch`,
     `SCAN`+`GET` in `_read_last_events`;
   - the per-session pending FIFO `$popoto_memory:pending:{agent}:{session}`
     (`RPUSH`/`LTRIM -32`/`EXPIRE 3600` in `_push_pending`, `LRANGE` in
     `_has_pending_turn`, `LRANGE`+`LREM`/`LPOP` in `_pop_pending`);
   - the per-session injected set `$popoto_memory:injected:{agent}:{session}`
     (`SADD`+`EXPIRE` in `_mark_injected`, `SMEMBERS` in `_injected_keys`);
   - the one-shot heuristic notice (`SETNX` in `_warn_heuristic_cost`);
   - reachability in `status()` (`self.redis.info("server")`).

   With Redis absent, every one of these fails. The failures are swallowed
   (fail-open), so the hook does not crash, but it is **wrong**: the pending
   push fails, so `Stop` finds no turn and `feedback` records 0 outcomes
   (spike-2); the injected set fails, so cross-turn suppression is lost and
   the same memories are re-injected every turn; counters and stamps never
   persist, so `doctor` reports nothing; `status()` reports
   `redis_reachable: False` and `record_count: None` on a healthy Postgres
   install.

2. **Construction refuses on a Postgres-only host.** `MemoryConfig` defaults
   `url` to `redis://localhost:6379/0` and `bind_connection`
   (`src/popoto/integrations/config.py:441`) rebinds the Redis pool and applies
   the DB-0 guard regardless of the memory backend. On a Postgres process with
   neither `REDIS_URL` nor `POPOTO_MEMORY_URL` set, `MemoryService()` raises
   `Db0RefusedError` after attempting one Redis command (`INFO keyspace` from
   `suggest_free_db`) (spike-3). So even with defect 1 fixed, a Postgres-only
   host's hook injects nothing: the hook's fail-open handler logs the refusal
   and exits 0.

3. **The operator surfaces speak only Redis.** `doctor` (`cli.py` `_cmd_doctor`)
   keys entirely on `redis_reachable` / `redis_url`, prints
   `redis UNREACHABLE` and exits 1; the MCP server's `_status` tool and its
   server instructions say "backed by Redis or Valkey"; `demo` prints
   `redis {url}`, calls `service.redis.ping()` and tells the user records were
   "left in Redis". On Postgres an operator has no way to see server version,
   pgvector or schema state, which are exactly the three things that break a
   Postgres install.

**Desired outcome (issue #814):** with `POPOTO_BACKEND=postgres`, the full
harness path (hooks → service → memory, MCP server, doctor, demo) works and
sends Redis zero commands; `doctor` reports Postgres health (server version,
pgvector, schema); the Redis path has no regression (wire byte-identical);
the integration tests run on both legs, plus a zero-Redis-commands test that
covers a full hook turn. Release blocker for 1.10.0. `integrations/` stays at
zero mypy errors (pinned by the `clean` allowlist in
`scripts/mypy_baseline.json`).

## Freshness Check

**Baseline:** `e8552e69` (origin/main, "Decision log on Postgres (#811) (#813)",
committed 2026-10-08T05:03Z). Issue #814 was filed 2026-10-08T05:13:47Z, after
the baseline.

- `git log --since=2026-10-08T05:13:47Z -- src/popoto/integrations src/popoto/backends src/popoto/redis_db.py`
  returns nothing: no commit has touched the cited area since filing.
- File:line references re-read and still exact: `service.py:230` (function-local
  `POPOTO_REDIS_DB` import inside the `redis` property), `config.py:288`
  (`effective_db`), `config.py:441` (`bind_connection` pool rebind + DB-0 guard).
- The bug was reproduced against the baseline rather than inferred: spikes 2
  and 3 below record the exact Redis commands attempted and the
  `Db0RefusedError`.
- Sibling issues: #759 (Postgres umbrella) OPEN; #755 OPEN; #811 CLOSED with
  PR #813 merged (the decision-log zero-Redis precedent this plan copies);
  #800 CLOSED.
- **Overlap:** #816 (OPEN, filed 05:22Z, in flight on another lane) widens
  `OUTAGE_ERRORS` to cover `BackendUnavailableError` (which includes the
  service's `_redis_down` circuit breaker at `service.py:44` / `:808`) and
  fixes `set_backend(instance)` being ignored for a model with
  `Meta.backend="postgres"`. This plan depends on the first for the breaker
  and touches the same lines; see Prerequisites and No-Gos for the
  coordination rule.
- Active plans: `docs/plans/harness_integration.md` is the shipped original
  (#515), not active; `docs/plans/decision-log-postgres.md` is shipped. No
  active plan covers `integrations/` on Postgres.

**Disposition: Unchanged** (with the #816 overlap surfaced above).

## Prior Art

- **PR #546** (feat #515): the original harness integration: hook, service,
  MCP server, doctor, demo. Defines every key shape this plan ports. Redis-only
  by design at the time.
- **PR #628** (fix #574): turn-keyed pending entries (`{"t": turn, "k": keys}`
  JSON vs the legacy bare list) and the claim-by-turn logic in `_pop_pending`.
  The Postgres store must preserve both entry shapes and the claim semantics
  unchanged, so `_decode_pending_entry` stays the single decoder.
- **PR #592**: `exclude_keys` cross-turn suppression via the injected set. The
  Postgres injected set must keep the 1 h TTL semantics.
- **PR #706** (fix #704, Hermes plugin) and **PR #696** (OpenClaw): the plugins.
  Hermes uses `MemoryService()` in-process; the others shell out to the hook.
  Neither needs code changes if the service is backend-neutral.
- **PR #644** (#630): field-layer routing, the `field_call` adapter pattern on
  `PostgresBackend` this plan extends.
- **PR #813** (#811): decision log on Postgres. Same shape of problem (a
  Redis-native side structure next to a Postgres model) solved with an engine
  table plus a `_RedisRecorder` zero-Redis test. This plan follows it.
- **PR #728** (research doc) and **PR #613** (#550, install size doc) matched
  the search but are docs-only and not relevant.

No previous attempt at this problem exists, so there is no "Why Previous
Fixes Failed" section.

## Research

Query: "PostgreSQL pg_available_extensions vector check installed version pgvector health check query".

- pgvector is installed **per database**, not per server: `pg_extension`
  (`SELECT extversion, extnamespace::regnamespace FROM pg_extension WHERE extname = 'vector'`)
  answers "is it installed here, and in which schema"; `pg_available_extensions`
  (`default_version`, `installed_version`) answers "could it be installed on
  this server". Doctor must report both, because "available but not created"
  and "not available at all" have different fixes (`CREATE EXTENSION` vs
  installing the package). Sources:
  https://github.com/pgvector/pgvector,
  https://dbakevlar.com/2025/12/extension-management-in-postgresql-for-new-dbas/
- The extension's schema must be on the `search_path` for the `vector` type
  to resolve unqualified; managed providers commonly install it into
  `public` or `extensions`. Doctor reports the extension schema and whether
  it is on the effective `search_path`. Sources:
  https://neon.com/docs/extensions/pgvector,
  https://devcenter.heroku.com/articles/pgvector-heroku-postgres
- How this informs the approach: the doctor check reads only catalog views
  (no DDL, no `CREATE EXTENSION`), so it is safe to run against any database,
  including a read-only role.

## Spike Results

Environment: Postgres 18.6 (Homebrew) at `postgresql://localhost:5432/postgres`,
pgvector 0.8.7, Redis refused at the connection layer
(`Connection.send_packed_command`, `send_command`, `ConnectionPool.get_connection`
monkeypatched to record and raise), `PYTHONPATH=<worktree>/src`. Spike schemas
were dropped afterwards.

### spike-1: the memory layer is already Postgres-native
- **Assumption**: "Only the service wrapper talks to Redis; the memory model
  and assembler do not."
- **Method**: prototype
- **Result**: confirmed. `save`, `extract_memories`, `assembler.assemble`
  (lexical mode), `ObservationProtocol.on_context_used` (used / acted /
  contradicted), `count`, `get_many`, `delete`: **0 Redis commands**.
  `observation._apply_outcome` builds a `get_REDIS_DB().pipeline()` object but
  sends nothing on Postgres.
- **Confidence**: high
- **Impact if false**: scope would grow into `fields/` and `recipes/`.

### spike-2: the service sends 13 Redis commands per turn
- **Assumption**: "The defect is MemoryService side state plus status."
- **Method**: prototype (`assemble` → `capture` → `feedback` → `search` →
  `status` on a Postgres-bound service)
- **Result**: confirmed. 13 refused commands: `SMEMBERS` (injected set), the
  `_touch` pipeline (`INCR` + `SET`), the pending and injected pipelines,
  failure-counter `INCR`s, `INFO server`. `feedback` returned 0 because the
  pending push had failed; `status` reported `redis_reachable False`,
  `record_count None`. `search` sent 0.
- **Confidence**: high
- **Impact if false**: n/a

### spike-3: construction refuses with no Redis URL
- **Assumption**: "A Postgres-only host can construct `MemoryService()`."
- **Method**: prototype (no `REDIS_URL`, no `POPOTO_MEMORY_URL`)
- **Result**: **false**. `Db0RefusedError`, after 1 attempted Redis command
  (`INFO keyspace` from `suggest_free_db`). This is defect 2 in Problem.
- **Confidence**: high
- **Impact if false**: n/a (it is false; the plan adds the `bind_connection` fix).

### spike-4: Postgres cold start fits the hook budget
- **Assumption**: "A cold hook process on Postgres stays inside the 400 ms p95
  hook budget (`tests/test_integrations_latency.py`)."
- **Method**: prototype (fresh process: import, first assemble, warm assemble)
- **Result**: import ~100 ms; first assemble 55-106 ms (connect + server-version
  check + table check); warm assemble ~2 ms. Redis cold first assemble ~3-4 ms.
  Inside budget locally, with the margin consumed mostly by connection setup.
  The service side state adds a handful of statements per turn on the same
  connection.
- **Confidence**: medium (local socket; a remote managed Postgres adds RTT and
  TLS, see Risks)
- **Impact if false**: the hook would need a connect timeout below
  `PG_CONNECT_TIMEOUT_SECONDS` (Open Question 1).

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

## Open Questions
TBD
