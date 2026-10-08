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

One harness turn, as the Claude Code plugin drives it:

1. **`UserPromptSubmit`**: the plugin runs `popoto-memory hook` and pipes in
   the event JSON. `hooks.handle_payload` builds `MemoryService(MemoryConfig.from_env())`.
   - `MemoryConfig.from_env` resolves `url` / `agent_id` / limits.
     `bind_connection` runs here. **Today** it rebinds the Redis pool and
     applies the DB-0 guard. **After** it is a no-op when the memory model's
     backend is not Redis.
   - `service.assemble(prompt, session_id, turn_id)`:
     1. `_injected_keys(session)` reads the injected set. Its members become
        `exclude_keys`. **Side state.**
     2. `memory.assembler.assemble(...)` runs on the model's backend.
        Already Postgres-native (spike-1).
     3. `_mark_injected(session, keys)` adds to the set and refreshes its
        1 h TTL. **Side state.**
     4. `_push_pending(session, turn, keys)` appends `{"t": turn, "k": keys}`,
        trims to 32 and refreshes the TTL. **Side state.**
     5. `_touch("assemble")` increments the `assemble_ok` counter and stamps
        the time. **Side state.**
   - The hook prints the `additionalContext` JSON and exits 0. On any
     exception it logs and exits 0 (fail-open).
2. **`Stop`**: the hook runs `service.feedback(session, outcome, turn_id)`.
   - `_pop_pending(session, turn)` claims the matching entry. **Side state.**
   - `ObservationProtocol.on_context_used(records, outcomes)` runs on the
     model's backend and is already Postgres-native.
   - `_touch("feedback")` runs. **Side state.**
   - Optionally `capture` / `extract_memories`, which run on the model's
     backend.
3. **Operator surfaces**:
   - `doctor` reads `service.status()`: reachability, record count, counters,
     last success, errors, and the log tail.
   - The MCP `status` tool and `demo` read the same `status()`.

The fix sits at a single layer: every "Side state" step goes through one
`HarnessState` object whose backend is chosen from the memory model's
backend. Nothing in steps 1.2 / 2.2 changes.

## Architectural Impact

- **New interface:** `popoto.integrations.state`, containing
  `HarnessState` (a protocol) plus `RedisHarnessState` and
  `BackendHarnessState`. `MemoryService` gains a `state` attribute and stops
  calling `self.redis.*` for side state.
  - The `redis` property stays. Tests and the Hermes plugin read it. On a
    Postgres-bound service it must not be used on any path, and it is
    reached only lazily.
- **New backend adapters:** a `_harness` pseudo-field on
  `PostgresBackend.field_call` (in `backends/postgres/recipes.py`, next to
  `_counter` / `_lease` / `_m3`). It is backed by three engine tables created
  on first use: `popoto_harness_list`, `popoto_harness_set` and
  `popoto_harness_stamp`.
  - Counters reuse `popoto_counter` and gain two ops: prefix read and
    set-if-absent.
  - All SQL lives in `backends/postgres/`, so `integrations/` holds no SQL and
    no psycopg import. That keeps its mypy count at the pinned zero.
- **New public method:** `PostgresBackend.diagnose() -> dict`. It never
  raises, and doctor uses it for the Postgres health block.
- **`status()` contract:**
  - Three backend-neutral keys are added: `backend`, `reachable`, `server`.
  - On Postgres a `postgres` block is added.
  - The `redis_*` keys keep their meaning on the Redis leg, so existing
    `doctor --json` consumers do not break.
- **Coupling:** `integrations/` now depends on `backends.routing.non_redis_backend`
  and on `PostgresBackend.field_call`. Both are already used by `counters.py`
  and `extraction/decision_log.py`, so no new direction of dependency is
  added.
- **Reversibility:** high. The Redis implementation is the current code moved
  verbatim, and the Postgres tables are new and transient (1 h TTL).

## Appetite

**Size:** Medium

**Team:** Solo dev, PM review

**Interactions:**
- PM check-ins: 1 (the Open Questions below)
- Review rounds: 1-2

The design is a port of a known pattern (#811 did the same thing for the
decision log), so the work is in faithfulness rather than invention. The
pieces are:
- five side-state structures with exact Redis semantics;
- a doctor health block;
- a test sweep across eight integration test files, both legs.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379, test DB 15 | `redis-cli -n 15 ping` | Redis leg of the suite |
| Postgres >= 18 with pgvector reachable via `POSTGRES_URL` | `psql "$POSTGRES_URL" -c "select extversion from pg_available_extensions where name='vector'"` | Postgres leg (`backend` fixture, `tests/postgres/`) |
| Extras `.[dev,embeddings,benchmark,mcp,postgres]` installed | `python -c "import mcp, psycopg, numpy"` | MCP and Postgres tests are skipped, not failed, without them |
| #816's `OUTAGE_ERRORS` widening merged, or rebased onto | `python -c "from popoto.redis_db import OUTAGE_ERRORS; from popoto.backends import BackendUnavailableError as B; assert any(issubclass(B, e) for e in OUTAGE_ERRORS)"` | Service circuit breaker trips on a Postgres outage (see Solution step 6) |

The last row is soft. If #816 has not merged when the build starts, the build
widens the tuple locally in `service.py`, the same way
`context_assembler.py:97` does. It then drops the local widening when it
rebases onto #816.

**Test environment** (the build and review must state theirs):
- Run the Redis leg with `pytest` (plugin on DB 15).
- Run the Postgres leg with
  `POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres pytest -m conformance tests/test_integration*.py tests/test_hermes_plugin_contract.py`, then `pytest tests/postgres/test_postgres_harness.py tests/postgres/test_postgres_recipes.py` (CI's `postgres` job runs exactly these two commands).
- Run ad-hoc scripts with `REDIS_URL=redis://localhost:6379/15` set before
  `import popoto`.

## Solution

### Key Elements

- **`HarnessState`** is one object that owns every piece of side state the
  service keeps. It has one Redis implementation (today's code) and one
  backend implementation (Postgres adapters).
- **Backend selection follows the memory model.** The rule is
  `non_redis_backend(self.model)`: None means Redis, anything else is the
  backend's own store.
  - There is no separate setting, so the harness can never split its state
    from its records.
- **`bind_connection`** never touches Redis when the memory model is not on
  Redis.
- **Doctor, MCP and demo** report the bound backend's health. On Postgres that
  means server version, pgvector and schema.

### Technical Approach

1. **`integrations/state.py`** (new) defines the `HarnessState` protocol, with
   these methods:
   - `incr(op)`, `counters()`
   - `stamp(op)`, `stamps()`
   - `set_once(name)`
   - `pending_push(session, entry_raw)`, `pending_list(session)`,
     `pending_remove(session, raw)`, `pending_pop(session)`
   - `injected_add(session, keys)`, `injected_members(session)`
   - `ping() -> (ok, server, ping_ms)`

   Keys are built once from the existing prefixes (`COUNTER_KEY_PREFIX`,
   `LAST_EVENT_PREFIX`, the pending and injected prefixes) so both stores use
   **identical key strings**.

   `RedisHarnessState` is the current method bodies moved without change:
   the same pipelines, `SCAN` match patterns, `MAX_PENDING_TURNS` trim and
   `PENDING_TTL_SECONDS` expire. The Redis wire must stay byte-identical, and
   a recorded-command test pins it (Test Impact).

2. **`BackendHarnessState`** calls
   `backend.field_call(DefaultMemory-model spec, "_harness", op, ...)`.
   It imports nothing from psycopg.

3. **Postgres adapters** (`backends/postgres/recipes.py`, `_harness`
   pseudo-field, `HARNESS_FIELD = "_harness"`). Engine tables:

   - `popoto_harness_list (key text, seq bigint GENERATED ALWAYS AS IDENTITY, payload text NOT NULL, expires_at double precision NOT NULL, PRIMARY KEY (key, seq))`
   - `popoto_harness_set (key text, member text, expires_at double precision NOT NULL, PRIMARY KEY (key, member))`
   - `popoto_harness_stamp (key text PRIMARY KEY, value text NOT NULL)`

   Ops:
   - **`list_push(key, payload, cap, ttl)`** runs as one statement batch in
     one transaction. It:
     1. deletes the key's rows if they have expired, so an expired list
        restarts empty, as `EXPIRE` would;
     2. inserts the payload;
     3. deletes every row except the newest `cap` (`LTRIM -cap -1`);
     4. sets `expires_at = now + ttl` for all of the key's rows (`EXPIRE`
        refresh).
   - **`list_range(key)`** returns the payloads in seq order where they have
     not expired (`LRANGE 0 -1`).
   - **`list_remove_first(key, payload)`** deletes the lowest-seq row with
     that exact payload, using `DELETE ... WHERE ctid = (SELECT ... LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING 1`.
     It returns the count (`LREM key 1 payload`).
   - **`list_pop(key)`** deletes the lowest-seq live row with
     `RETURNING payload` (`LPOP`).
   - **`set_add(key, members, ttl)`** inserts with `ON CONFLICT DO NOTHING`,
     then refreshes `expires_at` for the whole key (`SADD` + `EXPIRE`).
   - **`set_members(key)`** returns the live members (`SMEMBERS`).
   - **`stamp_set(key, value)`** is an upsert and **`stamp_scan(prefix)`** a
     prefix read.
   - **Counters:** `_counter_increment` already exists. This step adds
     `counter_scan(prefix)` and `counter_set_once(key)`.
     - `counter_scan` uses `WHERE key LIKE %s ESCAPE '\'` with the prefix's
       `%`/`_`/`\` escaped. Agent ids are free text.
     - `counter_set_once` is `INSERT ... ON CONFLICT DO NOTHING RETURNING 1`,
       which is the `SETNX` equivalent.

   Expired rows read as absent everywhere: every read filters on
   `expires_at > _NOW`. Writes to a key sweep that key's expired rows, and
   `list_push` / `set_add` also delete up to a fixed batch of expired rows
   for other keys. Without that bounded sweep, abandoned sessions would grow
   the table without limit.
   - The batch size is a pinned constant in `Defaults`, not configuration.
   - The tables are small: each live session holds at most 32 list rows and
     one set's worth of members.

   Because the counter keys equal `EVICTION_COUNTER_PREFIX`
   (`default_memory.py:102`), doctor on Postgres now **does** see the
   `evicted` counter that `DefaultMemory` already writes to `popoto_counter`.
   The "does not see that counter" sentence in `postgres-backend.md` is
   updated.

4. **`MemoryService`** changes:
   - `self.state = make_harness_state(self.model, self.config)` builds the
     state once, in `__init__`.
   - Every `self.redis.<cmd>` call on a side-state path becomes a
     `self.state.<op>` call.
   - `_decode_pending_entry` and the claim-by-turn logic stay in the service
     and are shared by both stores, so the turn semantics from #574 cannot
     diverge between them.

5. **`config.bind_connection`** returns early, with
   `url_source="backend:<name>"`, when the memory model's backend is not
   Redis. It then skips the pool rebind, `effective_db`, `suggest_free_db`
   and the DB-0 guard.
   - The DB-0 guard protects a *Redis* store. A Postgres-bound harness writes
     no Redis keys, so refusing it is a false positive.
   - If `POPOTO_MEMORY_URL` is set on a Postgres process, `status()` adds a
     warning that it is ignored. Doctor prints the warning but does not fail.

6. **Outage handling:** `_record_failure` sets the `_redis_down` short-circuit
   for `OUTAGE_ERRORS`. On Postgres the matching error is
   `BackendUnavailableError`.
   - With #816 merged, the shared tuple covers it.
   - Without #816, the service checks a local
     `OUTAGE_ERRORS + (BackendUnavailableError,)`.
   - The attribute is renamed to `_store_down`, keeping a `_redis_down` alias
     for tests and plugins.

7. **`status()`**:
   - Always returns `backend` (`"redis"` or `"postgres"`), `reachable`,
     `server` and `ping_ms`.
   - On Redis it keeps `redis_url`, `redis_reachable` and the rest unchanged.
   - On Postgres it adds `postgres_dsn` (redacted) and
     `postgres: backend.diagnose()`.
   - `record_count` uses the model's `query.count()` on both legs.

8. **`PostgresBackend.diagnose()`** never raises. Each sub-check is captured
   independently into `{"ok": False, "error": ...}`. Using catalog reads
   only, it returns:
   - `server`: version string, `server_version_num`, encoding, and
     `meets_floor` (>= `MIN_SERVER_VERSION_NUM`).
   - `pgvector`: `installed_version` and `schema` from `pg_extension`,
     `available_version` from `pg_available_extensions`, and
     `on_search_path`.
   - `schema`: name, `exists`, `format_version` from `popoto_schema` against
     `SCHEMA_FORMAT_VERSION`, whether the memory model's table exists, and
     `POPOTO_SCHEMA_AUTO`.
   - `health`: `Health.as_dict()`.

9. **`doctor`** prints a backend line and a health block. Exit 1 when:
   - the store is unreachable;
   - Postgres is below the floor;
   - pgvector is required but not installed (required means the memory model
     has an embedding field or the retrieval mode needs vectors);
   - or the schema format version is newer than the library's.

   Exit 0 otherwise, with warnings for:
   - pgvector available but not created;
   - an ignored `POPOTO_MEMORY_URL`;
   - `POPOTO_SCHEMA_AUTO` off with the table missing.

   `_measure_hook_read` is unchanged; it times `assemble` on whichever
   backend.
10. **Wording and the redaction helper:**
    - The MCP `_status` reads `reachable`, and the server instructions say
      "backed by Redis, Valkey or Postgres".
    - `demo` prints `backend`, calls `state.ping()` and says "left in the
      store".
    - `redact_url` also redacts conninfo `password=...` and URL-form
      Postgres DSNs.

### Flow

`hook UserPromptSubmit` → assemble (injected read → retrieve → injected add →
pending push → touch) → `additionalContext` → `hook Stop` → pending claim →
observe → touch → `doctor` shows counters, last success, and Postgres health.

## Failure Path Test Strategy

### Exception Handling Coverage
- The service's existing `except Exception` blocks around side state
  (`_touch`, `_record_failure`, `_mark_injected`, `_push_pending`) stay
  fail-open. Each one already logs and increments a failure counter.
  - **Test:** on the Postgres leg, make the backend raise
    `BackendUnavailableError` on `field_call` and assert:
    - `assemble` still returns context;
    - `errors` gains the failure;
    - `_store_down` trips.
- `diagnose()`: each sub-check fails independently. A role with no access to
  `pg_available_extensions`, or a missing `popoto_schema` table, yields
  `{"ok": False, "error": ...}` for that check alone, and doctor still prints
  the rest.

### Empty/Invalid Input Handling
- An empty `keys` list on `injected_add` / `pending_push` is a no-op. This
  matches Redis: `SADD` with no members is never sent.
- An agent id containing `%`, `_` or `\` must not leak another agent's
  counters into `counter_scan`.
  - **Test:** agents `a_b` and `axb`.
- A pending entry that fails to decode is skipped as today. Both stores feed
  the same `_decode_pending_entry`.

### Error State Rendering
- When Postgres is unreachable, doctor prints `postgres UNREACHABLE <dsn redacted>`
  and the error class, and exits 1. Its `--json` output carries
  `reachable: false`.
- **Test:** the DSN password never appears in human or JSON output, in either
  URL or conninfo form.

## Test Impact

These eight test files exercise `integrations/` and must be classified.

- [ ] `tests/test_integrations_service.py` (57 tests): UPDATE.
  - Mark backend-neutral tests `conformance`.
  - The tests that read Redis keys directly via `POPOTO_REDIS_DB` (around
    lines 28 and 524-585) stay Redis-only and are marked
    `redis_only(reason=...)` (the plugin enforces a reason and skips them on the
    Postgres leg).
  - `_UnreachableService` overrides `.redis`; rewrite it to override
    `state.ping`.
  - Assertions on `info["redis_reachable"]` move to `info["reachable"]` on the
    conformance tests.
- [ ] `tests/test_integrations_hooks.py` (43): UPDATE. Mark `conformance` the
  tests that drive `handle_payload` end to end; key-inspecting tests stay
  Redis-only.
- [ ] `tests/test_integrations_mcp.py` (25): UPDATE. `conformance`, and change
  the status assertion to `reachable`.
- [ ] `tests/test_integrations_cli.py` (4): UPDATE. Doctor output assertions
  become backend-aware; add Postgres doctor cases in the new file below.
- [ ] `tests/test_integrations_db0_isolation.py` (14): UPDATE. These stay
  Redis-only, plus one new case: a Postgres-bound service with no Redis URL
  constructs, and its DB-0 guard is skipped.
- [ ] `tests/test_integrations_latency.py` (4): UPDATE. Add a Postgres
  parametrization of the hook p95 test under the same 400 ms budget.
- [ ] `tests/test_integration_v144.py` (5): UPDATE if it asserts Redis
  wording; otherwise no change.
- [ ] `tests/test_hermes_plugin_contract.py` (8): UPDATE. `conformance` where
  it constructs `MemoryService()` in-process.
- [ ] **NEW** `tests/postgres/test_postgres_harness.py`:
  - **Zero-Redis full turn:** under `_RedisRecorder`, run `handle_payload`
    for `UserPromptSubmit` then `Stop`, plus the MCP tools, `doctor --json`,
    `demo` and `status`. Assert `calls == []`.
  - **Semantics, side by side with the Redis store:** pending cap 32, TTL
    expiry, LREM-first-match, the turn claim, legacy bare-list entries,
    injected suppression across turns, set-once, and prefix escaping.
  - **Concurrency:** two `Stop` claimers, exactly one wins.
  - **`diagnose()` shape**, including below-floor and pgvector-missing
    simulations.
- [ ] **NEW** Redis wire pin: record the commands one `assemble` + `feedback`
  turn sends on Redis before and after the refactor, and assert they are
  identical. This is the "no regression" proof.
- [ ] `tests/postgres/test_postgres_recipes.py`: UPDATE. Add `_harness` and
  `counter_scan` / `counter_set_once` adapter unit tests.

No `xfail` markers relate to this bug (`grep -rn 'xfail' tests/test_integration*`
returns none).

## Rabbit Holes

- **Migrating `$popoto_memory:*` keys from Redis to Postgres.** They are
  transient: pending and injected expire after 1 h, and counters are
  diagnostics. Counters simply restart at zero after a move.
- **A generic `KeyValueStore` abstraction for all Redis side structures** in
  popoto. That is #759 umbrella territory. This plan builds only what the
  harness needs.
- **Async MCP server rewrite onto `aio.py`.** The MCP server calls the sync
  service today, and that stays.
- **LISTEN/NOTIFY or pg_cron for TTL expiry.** Lazy expiry plus a bounded
  sweep is enough for tables this small.
- **Changing the hook's connect timeout through configuration.** If one is
  needed it is a pinned magic constant (Open Question 1), never a knob.

## Risks

### Risk 1: hook latency on a remote Postgres
**Impact:** Each hook invocation is a fresh process, so it pays the connect
cost and the server and table checks every turn. Locally that is 55-106 ms
(spike-4). On a managed Postgres, TLS and RTT could push p95 past 400 ms, and
a down server costs `PG_CONNECT_TIMEOUT_SECONDS = 5.0` against Redis's 1.0 s.
**Mitigation:**
- The side state reuses the model's connection and adds no new connects.
- Measure p95 in the Postgres latency parametrization.
- Open Question 1 decides whether the hook process pins a shorter connect
  timeout.

### Risk 2: #816 lands concurrently on the same lines
**Impact:** Merge conflict in `service.py` around `_redis_down` /
`OUTAGE_ERRORS`, or two different outage tuples.
**Mitigation:**
- The local widening is a single expression, deleted on rebase.
- Build rebases on main immediately before opening the PR.
- The PR description names #816.

### Risk 3: the Redis refactor changes the wire
**Impact:** A subtle change to pipeline grouping or order breaks the "no
regression" promise.
**Mitigation:**
- Move code verbatim.
- The recorded-command pin test compares the command sequence before and
  after.

### Risk 4: `doctor --json` consumers
**Impact:** Plugins or scripts reading `redis_reachable` break.
**Mitigation:**
- The `redis_*` keys are kept on the Redis leg.
- New keys are additive.
- On Postgres, `redis_reachable` is absent rather than `false`, because
  absent is honest and `false` would read as an outage. Open Question 2
  confirms this.

## Race Conditions

### Race 1: two `Stop` hooks claim the same pending turn
**Location:** `_pop_pending` → `list_remove_first` / `list_pop`.
**Trigger:** Overlapping Stop events for the same session, for example a
retry from the harness.
**Data prerequisite:** The entry must be pushed (UserPromptSubmit committed)
before Stop reads it.
**State prerequisite:** At most one claimer may record outcomes for an entry.
**Mitigation:**
- The claim is a single `DELETE ... RETURNING` on one row, chosen with
  `FOR UPDATE SKIP LOCKED`.
- The service records outcomes only if the delete returned a row (rowcount 1).
- This mirrors Redis, where `LREM` returning 1 is the claim.
- Tested with two threads.

### Race 2: push and trim interleave
**Location:** `list_push`.
**Trigger:** Two prompts in one session within milliseconds.
**Mitigation:**
- The push runs insert, trim and TTL refresh in one transaction.
- The trim keeps the newest `cap` rows by `seq` (identity, monotonic per
  insert).
- Two concurrent trims can only delete more rows, never fewer, so the cap
  holds. An over-trim by one under contention matches Redis pipelines, which
  are not transactional either.

### Race 3: an expired list is read while being re-pushed
**Location:** `list_push` sweep versus `list_range`.
**Mitigation:**
- Reads filter `expires_at > now`, so they never see expired rows whatever
  the sweep has done.
- The push's refresh of `expires_at` happens in the same transaction as its
  insert.

### Race 4: first-use DDL from concurrent hook processes
**Location:** `_engine("popoto_harness_*")`.
**Mitigation:** Already solved. `engine_table_ddl` takes the schema and then
the table advisory lock (#776).

## No-Gos (Out of Scope)

- Widening the shared `OUTAGE_ERRORS` tuple and fixing `set_backend(instance)`
  for `Meta.backend="postgres"` models belong to #816
  **[SEPARATE-SLUG #816]**. This plan only consumes the widening, with a local
  fallback until #816 merges.
- Migrating existing `$popoto_memory:*` Redis keys into Postgres. They are
  transient (1 h TTL) or diagnostic counters, so they restart empty
  **[DESTRUCTIVE]**: no migration tool will touch a live Redis store.
- A shorter Postgres connect timeout for the hook process, unless Open
  Question 1 says yes. If it is wanted, it lands as a pinned constant in the
  same PR. If deferred, it gets its own issue **[ORDERED]**: it must follow
  the latency measurement from this build.
- Creating the pgvector extension from doctor or the service. Doctor reports
  only; `CREATE EXTENSION` is an operator action that needs privileges popoto
  must not assume **[EXTERNAL]**.
- Async (`aio.py`) adapters for `_harness`. The harness path is sync end to
  end. The async backend can add `_harness` when an async caller exists
  **[SEPARATE-SLUG #759]**.
- `py.typed` / public typing of `integrations/`. That is unchanged and remains
  a separate published-API decision.

## Update System

- No new dependency. `psycopg` is already the `postgres` extra, and the
  harness does not require it on Redis installs.
  - `integrations/state.py` must import nothing from
    `popoto.backends.postgres` at module scope. A Redis-only install without
    the extra must still import the hook.
  - Test: run `python -c "import popoto.integrations.hooks"` with psycopg
    absent (the existing lock-import job covers it).
- Existing installs need no migration. The engine tables are created on first
  use, under the existing `POPOTO_SCHEMA_AUTO` rule.
  - With auto-DDL off, an operator who manages the schema needs the three new
    `popoto_harness_*` tables. Doctor names them when they are missing, and
    the Postgres backend doc lists their DDL next to the other engine tables.
- The plugins (`plugins/*`) need no change: they shell out to the hook, or
  construct `MemoryService()`, and both are backend-neutral after this
  change.

## Agent Integration

- The MCP server (`popoto.integrations.mcp_server`) is the agent-facing
  surface. Its tools (`search`, `remember`, `status`, ...) already go through
  `MemoryService`, so they become Postgres-native with the service. No new
  tool is added.
- Changes:
  - The `_status` tool reads `reachable` instead of `redis_reachable`, and
    returns the `postgres` health block on Postgres.
  - The server's instruction text names Postgres.
- Integration test: the zero-Redis test in
  `tests/postgres/test_postgres_harness.py` calls the MCP tool functions under
  `_RedisRecorder`. `tests/test_integrations_mcp.py` runs as `conformance` on
  both legs.

## Documentation

- [ ] Update `docs/features/harness-integration.md`:
  - a "Running on Postgres" section (`POPOTO_BACKEND=postgres` +
    `POPOTO_POSTGRES_URL`, with no Redis needed);
  - the doctor health block and its exit codes;
  - `POPOTO_MEMORY_URL` being ignored on Postgres.
- [ ] Update `docs/features/postgres-backend.md`:
  - list `popoto_harness_list` / `_set` / `_stamp` among the engine tables
    with their DDL;
  - replace the "MemoryService (the Redis-only integration) does not see that
    counter" sentence (around lines 762-763) now that it does;
  - add the harness to the zero-Redis surfaces next to the decision log.
- [ ] Update `plugins/claude-code/README.md`, `plugins/hermes/README.md` and
  `plugins/openclaw/README.md` wherever they say Redis is required: "Redis,
  Valkey or Postgres".
- [ ] Add a `CHANGELOG.md` `[Unreleased]` entry under Fixed: "the harness
  (hook, MCP server, doctor, demo) works on the Postgres backend with zero
  Redis commands; doctor reports Postgres health" (#814).
- [ ] Update docstrings in `integrations/cli.py`, `demo.py`, `mcp_server.py`
  and `service.py` that say Redis where they mean "the store".

## Success Criteria

- [ ] With `POPOTO_BACKEND=postgres`, a full hook turn (`UserPromptSubmit` →
  `Stop`) plus the MCP tools, `doctor --json`, `demo` and `status()` sends
  **zero** Redis commands. The `_RedisRecorder` test in
  `tests/postgres/test_postgres_harness.py` asserts `calls == []`.
- [ ] On Postgres, `feedback` after `assemble` records the injected
  memories' outcomes (non-zero), and a second `assemble` in the same session
  excludes the first turn's keys. These are the behaviours spike-2 showed
  broken.
- [ ] `MemoryService()` constructs on a Postgres-only host with no
  `REDIS_URL` / `POPOTO_MEMORY_URL`, and attempts no Redis command.
- [ ] `popoto-memory doctor` on Postgres reports:
  - the server version and whether it meets the >= 18 floor;
  - pgvector installed and available versions, its schema, and whether it is
    on the search path;
  - the schema name, whether it exists, its format version and the memory
    table.

  It exits 1 when Postgres is unreachable. It never prints a password.
- [ ] Redis leg: the recorded command sequence for one assemble + feedback
  turn is identical before and after the refactor. All existing integration
  tests pass unchanged, or with only the documented
  `reachable` / `redis_only` edits.
- [ ] The integration tests marked `conformance` pass on both legs
  (`POPOTO_CONFORMANCE_BACKENDS=redis,postgres`), and CI's `postgres` job is
  green.
- [ ] Hook p95 on Postgres is within the 400 ms budget in
  `tests/test_integrations_latency.py`, and the environment is stated.
- [ ] `scripts/mypy_ratchet.py` passes, with `integrations/` still exactly 0
  (the `clean` allowlist).
- [ ] `ruff check src/` and `black --check src/ tests/` are clean.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (harness-state)**
  - Name: state-builder
  - Role: `integrations/state.py`, the service refactor, the
    `bind_connection` early return, and the outage handling.
  - Agent Type: builder
  - Resume: true
- **Builder (pg-adapters)**
  - Name: pg-builder
  - Role: the `_harness` adapters, `counter_scan` / `counter_set_once`, and
    `PostgresBackend.diagnose()`.
  - Agent Type: builder
  - Resume: true
- **Builder (surfaces)**
  - Name: surface-builder
  - Role: `status()`, doctor, MCP `_status`, demo, and the redaction helper.
  - Agent Type: builder
  - Resume: true
- **Test engineer**
  - Name: harness-tester
  - Role: the conformance marking sweep, `test_postgres_harness.py`, the Redis
    wire pin and the latency parametrization.
  - Agent Type: test-engineer
  - Resume: true
- **Validator**
  - Name: harness-validator
  - Role: run both legs plus mypy, ruff and black; check the zero-Redis
    assertion is not vacuous (it must fail when one `self.redis` call is
    reintroduced).
  - Agent Type: validator
  - Resume: true
- **Documentarian**
  - Name: harness-docs
  - Role: the Documentation checklist.
  - Agent Type: documentarian
  - Resume: true

A solo builder can run these in sequence. The split only marks dependency
boundaries.

## Step by Step Tasks

### 1. Pin the Redis wire first
- **Task ID**: build-wire-pin
- **Depends On**: none
- **Validates**: tests/test_integrations_service.py (new pin test)
- **Assigned To**: harness-tester
- **Agent Type**: test-engineer
- **Parallel**: true
- Record the exact Redis command sequence of one `assemble` + `feedback` +
  `status` turn on the current code. Commit it as the expected value **before**
  any refactor.

### 2. Postgres adapters
- **Task ID**: build-pg-adapters
- **Depends On**: none
- **Validates**: tests/postgres/test_postgres_recipes.py
- **Assigned To**: pg-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `HARNESS_FIELD` and the three engine tables. Add the `list_*`, `set_*`
  and `stamp_*` ops, plus `counter_scan` (escaped LIKE) and `counter_set_once`.
- Add `PostgresBackend.diagnose()`, which uses catalog reads only and never
  raises.
- Add the bounded expired-row sweep, with its batch size as a `Defaults`
  constant.

### 3. Harness state and service refactor
- **Task ID**: build-state
- **Depends On**: build-wire-pin, build-pg-adapters
- **Validates**: tests/test_integrations_service.py, tests/test_integrations_hooks.py
- **Assigned To**: state-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `integrations/state.py`, with `RedisHarnessState` moved verbatim and
  `BackendHarnessState` using `field_call`. Selection uses
  `non_redis_backend(model)`.
- Route every side-state call in `service.py` through `self.state`. Keep
  `_decode_pending_entry` and the claim logic in the service.
- Add the `bind_connection` early return for a non-Redis memory backend.
- Rename `_redis_down` to `_store_down` and keep an alias. Widen the outage
  tuple locally unless #816 has merged.

### 4. Operator surfaces
- **Task ID**: build-surfaces
- **Depends On**: build-state
- **Validates**: tests/test_integrations_cli.py, tests/test_integrations_mcp.py
- **Assigned To**: surface-builder
- **Agent Type**: builder
- **Parallel**: false
- `status()`: add the `backend`, `reachable`, `server` and `ping_ms` keys; add
  the `postgres` block and the ignored `POPOTO_MEMORY_URL` warning.
- Doctor: add the health block and the exit rules.
- MCP `_status` and instructions: update both. `demo`: neutral wording.
- `redact_url`: handle conninfo and Postgres URLs.

### 5. Test sweep and zero-Redis turn
- **Task ID**: build-tests
- **Depends On**: build-surfaces
- **Validates**: tests/postgres/test_postgres_harness.py and the eight
  integration files
- **Assigned To**: harness-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Mark the backend-neutral tests `conformance` and the key-inspecting tests
  `redis_only(reason=...)`.
- Write `test_postgres_harness.py`: the zero-Redis full turn, side-by-side
  semantics, the two-claimer race, the `diagnose` shape, and the no-URL
  construction case.
- Add the Postgres parametrization to the latency test.

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: harness-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Work through the Documentation checklist above.

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: build-tests, document-feature
- **Assigned To**: harness-validator
- **Agent Type**: validator
- **Parallel**: false
- Run both legs, the ratchet, ruff and black.
- Run the non-vacuity check: temporarily reintroduce one `self.redis.incr` on
  the Postgres path and confirm the zero-Redis test fails.
- Report each number together with its environment.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Redis leg | `pytest tests/test_integration*.py tests/test_hermes_plugin_contract.py -q` | exit code 0 |
| Both legs (conformance) | `POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres pytest -m conformance -q` | exit code 0 |
| Postgres-only | `POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/postgres -q` | exit code 0 |
| Zero-Redis turn | `POSTGRES_URL=postgresql://localhost:5432/postgres pytest tests/postgres/test_postgres_harness.py -k zero_redis -q` | exit code 0 |
| Types (ratchet) | `scripts/mypy_ratchet.py` | exit code 0, `integrations` 0 |
| Lint | `ruff check src/` | exit code 0 |
| Format | `black --check src/ tests/` | exit code 0 |
| No new Redis import in integrations side state | `grep -n "POPOTO_REDIS_DB\|get_REDIS_DB" src/popoto/integrations/service.py` | output contains only the `redis` property |
| Doctor on Postgres | `POPOTO_BACKEND=postgres POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres POPOTO_POSTGRES_SCHEMA=harness_verify popoto-memory doctor --json` | output contains "pgvector" |

## Open Questions

1. **Hook connect timeout on Postgres.**
   - The hook process inherits `PG_CONNECT_TIMEOUT_SECONDS = 5.0`, against
     Redis's 1.0 s (`HOOK_SOCKET_TIMEOUT_SECONDS`).
   - When Postgres is down, every prompt therefore stalls up to 5 s before
     the hook fails open.
   - Should the hook process pin a shorter connect timeout (for example 1.0 s,
     as a magic constant mirroring the Redis hook) in this PR?
   - Recommendation: yes. It is a few lines, and it applies only in the hook
     entry point, not to the library default.
2. **`doctor --json` key shape on Postgres.**
   - Proposed: add `backend`, `reachable`, `server` and `postgres`, and *omit*
     `redis_url` / `redis_reachable` on Postgres rather than reporting
     `false`.
   - Is any consumer known to read `redis_reachable` unconditionally? Only
     the in-repo MCP server and `cli.py` do, and both are updated here.
3. **Ordering with #816.**
   - Build this on top of #816 once it merges (clean), or proceed in parallel
     with the local outage widening and rebase?
   - Recommendation: proceed in parallel. The overlap is one expression.
