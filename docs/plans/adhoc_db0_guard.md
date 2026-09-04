---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/577
last_comment_id: none
---

# Refuse a Destructive Flush of Redis DB 0 on Popoto's Own Client

## Problem

On 2026-08-13/14 two review subagents, hours apart, each ran `flushdb` against
Redis DB 0 on the maintainer's machine. DB 0 held live agent state — SDLC
`PipelineLedger:*`, `AgentSession:*`, `session:issuelock:*`, `Reflection:*`,
`analytics:daily:*`. Both wipes destroyed it, and the loss was first misread as
a substrate bug: ledgers emptying mid-run, run_ids dying, verdicts vanishing
minutes after being written.

Issue #577 was rewritten on 2026-09-03 after PR #594 landed. Two of the three
original remedies are done. This plan covers only the **Residual gap** the
rewrite defines.

**Current behavior:**

`popoto.integrations` refuses DB 0 (`Db0RefusedError`, opt-in via
`POPOTO_MEMORY_ALLOW_DB0`), and the pytest plugin rejects `popoto_test_db=0`.
Neither binds the path the incidents took. A scratch script that does
`import popoto` binds `POPOTO_REDIS_DB` from `REDIS_URL` at import time,
defaults to DB 0 when `REDIS_URL` is unset, and then hands the caller a plain
`redis.Redis` on which `flushdb()` works. Nothing anywhere in popoto refuses
`FLUSHDB` or `FLUSHALL` — the two commands that actually caused the loss —
regardless of which database the caller landed on.

PR #594 also made the pytest plugin opt-in (#595), so a downstream suite that
never sets `popoto_test_db` now runs unisolated against its `REDIS_URL`
default. The exposed population grew.

**Desired outcome:**

Popoto's own client refuses to execute the destructive flush commands when the
blast radius includes database 0, by default, with a deploy-level environment
variable as the escape hatch. A script that reaches for `flushdb()` on an
unisolated connection gets a loud, actionable error naming the variable to set
and a free database to move to — instead of a silent wipe.

## Freshness Check

**Baseline commit:** `0eef7362bffc7a29739db6fdb4b78a6b70adc5cf`
**Issue filed at:** 2026-08-14T03:12:56Z (body rewritten 2026-09-03T02:59:27Z)
**Disposition:** Unchanged

The issue's own rewrite is what makes the premises current: it was edited
*after* PR #594 merged and states the residual gap in terms of what #594 left
open. Every residual-gap claim was re-verified against the baseline commit.

**Claims re-verified against current main:**

- `src/popoto/redis_db.py` binds the global client from `REDIS_URL` at import
  time, with `db=0` on the no-`REDIS_URL` fallback branch. Still true. Both
  construction branches build a plain `redis.Redis`.
- No refusal, warning, or opt-in exists anywhere on the core ORM connection
  path. Still true.
- Nothing in `src/` refuses `FLUSHDB`/`FLUSHALL`. Confirmed by grep: the only
  matches in `src/` are `src/popoto/pytest_plugin.py` (the per-test flush of
  the isolated DB), `src/popoto/testing.py:79` (`flush_test_db()`, a public
  helper that flushes the client bound **at import time** — not, as this line
  originally read, whatever database is currently bound. `set_REDIS_DB_settings`
  rebinds the module global and `flush_test_db()` does not follow it, so the
  documented `use_test_db()` + `flush_test_db()` pattern already mis-flushes;
  Task 1 fixes it by routing through `get_REDIS_DB()`), and one prose mention
  inside the integrations refusal message.
- `src/popoto/integrations/config.py` carries `Db0RefusedError`,
  `effective_db()`, `suggest_free_db()`, and the `POPOTO_MEMORY_ALLOW_DB0`
  opt-in. Still true; this plan reuses the pattern rather than changing it.
- `CLAUDE.md:23` carries the ad-hoc-script paragraph naming `REDIS_URL` as the
  only binding variable. Still true; this plan amends it rather than
  replacing it.

**Cited sibling issues/PRs re-checked:**

- #584 — closed by PR #594 (merged). The integrations-layer half. Its refusal
  semantics are the template for this plan's error and message.
- #595 — closed by PR #597 (merged, commit `044c546`), which made the plugin
  opt-in and added `PopotoIsolationWarning`. Widens this gap exactly as the
  issue says.
- #549 — the plugin's own tests; unrelated to the destructive-command path.

**Commits on main since the issue was filed (touching referenced files):**

- `044c546` Warn once when the pytest plugin is inert but popoto is used
  (#595) — changed the plugin, not `redis_db.py`. Does not close the gap.
- `16aa702` Agent memory production audit (#594) — added the integrations
  refusal. Narrowed the issue; the rewrite already accounts for it.
- `e220b2e` subconscious memory (#546) — introduced the DB-0-defaulting
  integration that raised severity in the first place.

**Active plans in `docs/plans/` overlapping this area:**
`integrations_refuse_db0.md` (#584) and `pytest_plugin_inert_warning.md`
(#595). Both shipped; their frontmatter status is stale, not their content.
Neither touches `redis_db.py`, so there is no file-level collision. They are
the adjacent layers, treated here as prior art.

## Prior Art

- **Issue #584 / PR #594**: DB-0 refusal in `popoto.integrations`. Shipped.
  Establishes the house pattern this plan follows: a dedicated error type, a
  message that names the variable to change and suggests a free database via
  `suggest_free_db()`, and a deploy-level environment opt-in rather than a
  constructor argument. Its `effective_db()` docstring also records the trap
  this plan must not repeat — judging a *configured* URL rather than the
  *live* connection refuses on every test run.
- **Issue #595 / PR #597**: the inert-plugin isolation warning. Shipped.
  Supplies the one-shot advisory precedent, and the evidence that an advisory
  scoped to pytest is acceptable where a process-wide one would not be.
- **Issue #522**: module-level test writes landing in DB 0 during collection.
  Fixed by moving the swap into `pytest_configure`. Same family of bug —
  writes reaching DB 0 through a path the guard did not cover.
- **PR #468 (#465)**: isolated the external benchmark harness from live DB 0.
  Precedent for treating DB 0 as hostile territory rather than a default.

## Research

No external research was needed. The work is internal: a subclass of
`redis.Redis` used at popoto's own construction sites. The only external
surface is redis-py's client/pipeline class layout, which spike-1 and spike-2
measured directly against the installed version rather than inferring from
documentation.

## Spike Results

All spikes ran with a non-zero `REDIS_URL` exported before `import popoto`,
against redis-py 7.1.1. No spike wrote to or read DB 0. The spike text below
names DB 13; the lane database for build and verification is **DB 4** (see
Prerequisites).

### spike-1: Does overriding `execute_command` on a `redis.Redis` subclass catch every flush shape?
- **Assumption**: "A single `execute_command` override catches `flushdb()`,
  `flushall()`, and a raw `execute_command('FLUSHDB')` alike."
- **Method**: prototype
- **Finding**: Confirmed for all three. A `GuardedRedis(redis.Redis)` with an
  `execute_command` override refused `flushdb()`, `flushall()`, and
  `execute_command("FLUSHDB")`, while ordinary `set`/`get` traffic was
  unaffected. Overriding the two *methods* instead would miss the raw form.
- **Confidence**: high
- **Impact on plan**: The guard hooks `execute_command`, not the named
  methods.

### spike-2: Do pipelines and the async client bypass the guard?
- **Assumption**: "`client.pipeline()` and the async client inherit the
  guard."
- **Method**: prototype
- **Finding**: They do **not**, by default. `redis.client.Pipeline` subclasses
  `Redis`, but `Redis.pipeline()` constructs `Pipeline(...)` explicitly rather
  than `type(self)(...)`, so a pipeline off a guarded client is unguarded — a
  queued `flushdb()` executed and wiped DB 13 in the spike. The async client is
  a separate class hierarchy and is likewise unguarded. Both close cleanly:
  overriding `pipeline()` to reassign the returned object's `__class__` to a
  `GuardedPipeline` subclass guarded both the buffered path and the immediate
  (post-`watch()`) path, and an async subclass with an `async execute_command`
  override guarded the async path.
- **Confidence**: high
- **Impact on plan**: Three guarded classes, not one. The `__class__`
  reassignment form is preferred over reimplementing `pipeline()`, because it
  does not depend on the `Pipeline` constructor signature, which differs
  across redis-py versions.

### spike-3: Does a guarded client break ordinary ORM operation?
- **Assumption**: "Swapping the class under `POPOTO_REDIS_DB` is transparent
  to models, queries, pipelines, and Lua."
- **Method**: prototype
- **Finding**: Confirmed. With `POPOTO_REDIS_DB.__class__` set to the guarded
  class, `Model.create()`, `query.filter()`, `delete()`, and an explicit
  pipeline `set`/`get`/`execute` all behaved identically, while `flushdb()` on
  the client and on a pipeline both refused.
- **Confidence**: high
- **Impact on plan**: No ORM changes are needed; the guard is confined to
  `redis_db.py`'s construction sites.

### spike-4: Does Lua bypass the guard?
- **Assumption**: "`EVAL` of a script containing `redis.call('FLUSHDB')` is
  caught."
- **Method**: prototype
- **Finding**: It is **not** caught. The guard sees `EVAL`, not the flush
  inside it, and the script ran. popoto ships no Lua that flushes (grep over
  `src/` finds no `FLUSHDB`/`FLUSHALL` in any script body), so this is a
  documented limit rather than a live hole.
- **Confidence**: high
- **Impact on plan**: Listed under "What this does not cover" and in the
  guard's own docstring.

### spike-5: Does `PopotoException` preserve its message?
- **Assumption**: "`PopotoException.__init__` sets `self.message` and logs but
  never calls `super().__init__`, so `str(e)` may be empty — which would make
  a long refusal message invisible in a traceback."
- **Method**: prototype
- **Finding**: The message survives. `BaseException.__new__` populates `args`
  from the constructor arguments, so `str(e)` returns the full text and
  `e.args` is the one-tuple.
- **Confidence**: high
- **Impact on plan**: The new error can subclass `PopotoException` and inherit
  its automatic ERROR-level logging, which is desirable here: the refusal
  should land in the log even if the caller swallows the exception.

## Data Flow

1. **Entry point**: a scratch script, a downstream application, or a test runs
   `import popoto`.
2. **`redis_db.py` module body**: reads `REDIS_URL`. Set — builds a pool from
   the URL. Unset — builds a pool on `127.0.0.1:6379` with `db=0`. Either way
   the client is constructed **once** and every popoto module imports that one
   object for the life of the process.
3. **Caller**: reaches `POPOTO_REDIS_DB` directly, or via
   `popoto.testing.flush_test_db()`, and calls `flushdb()`.
4. **Guard (new)**: `execute_command` inspects the command name. `FLUSHDB` on
   a client whose pool is bound to database 0, or `FLUSHALL` on any binding,
   raises `Db0FlushRefusedError` **before** the command reaches the socket.
   Anything else is delegated untouched.
5. **Output**: either the data survives and the caller gets a message naming
   `POPOTO_ALLOW_DB0_FLUSH` and a free database, or — with the opt-in set —
   the flush proceeds exactly as today.

The pool-swap paths matter to this flow. Both `popoto.pytest_plugin._swap_db`
and `integrations.config.bind_connection` rebind the *pool attribute* on the
existing client object rather than replacing the client, precisely so
already-imported modules follow the swap. That is what makes a class-level
guard durable: it survives every swap, and it re-reads the bound database at
call time, so it judges the connection that will actually be wiped.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|--------------------------------|
| CLAUDE.md ad-hoc paragraph (remedy 1) | Documented that scratch scripts default to DB 0 and must export `REDIS_URL` before importing popoto | Documentation cannot bind an agent that does not read it at the moment it writes the script. The second wipe happened after the first was understood. |
| PR #594 / #584 (remedy 2, integrations half) | Refuses DB 0 in `MemoryService`, opt-in via `POPOTO_MEMORY_ALLOW_DB0` | Correct, but bound to the layer nobody was using at the time of either incident. Both wipes went through the core ORM connection. |
| Pytest plugin DB-0 rejection | Rejects `popoto_test_db=0` | Guards the path that was never the risk. PR #594 made the plugin opt-in, so it now guards even less of the population. |

**Root cause pattern:** every fix so far has guarded a *configuration
surface* — which database you asked for. None has guarded the *command* that
destroys data. The blast radius of `FLUSHALL` does not depend on which
database you configured at all.

## Architectural Impact

- **New dependencies**: none. Subclasses of classes redis-py already exports.
- **Interface changes**: `POPOTO_REDIS_DB` and the object returned by
  `get_async_redis_db()` become instances of popoto subclasses. Both remain
  `redis.Redis` / `redis.asyncio.Redis` instances by inheritance, so
  `isinstance` checks, type annotations, and duck-typed callers are unaffected.
  One new public name in `popoto.redis_db`: `Db0FlushRefusedError`.
- **Coupling**: unchanged. The guard lives entirely inside `redis_db.py`,
  which is already the universal dependency.
- **Data ownership**: unchanged.
- **Reversibility**: high. The behavior is off with one environment variable,
  and the code reverts by constructing the stock classes again.

## Appetite

**Size:** Small

**Team:** Solo dev, plus a validator pass

**Interactions:**
- PM check-ins: 0 (the mechanism decision is made in this plan, see Technical
  Approach)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey reachable | `redis-cli -n 4 ping` | Test and spike target. **DB 4 is this plan's single lane database**, everywhere: tests and every Verification row. DB 0 (live agent store) and the shared DB 15 are both untouched. The spike transcripts above name DB 13 because they predate this decision; read every `13` in them as `4`. |
| Editable install resolves to **this** checkout | run from the worktree root: `python -c "import popoto, pathlib, sys; sys.exit(0 if pathlib.Path(popoto.__file__).resolve() == pathlib.Path('src/popoto/__init__.py').resolve() else 1)"` | Guards against the worktree hazard in `CLAUDE.md`. The substring form (`'src/popoto' in str(path)`) passes on **any** popoto checkout and is not acceptable here: a false pass would let a verification command run against an unguarded package. |

## Solution

### Key Elements

- **`Db0FlushRefusedError(PopotoException, ValueError)`**: raised instead of
  executing a flush whose blast radius includes database 0. `PopotoException`
  supplies the automatic ERROR-level log, so the refusal is visible even when
  swallowed; `ValueError` matches the #584 house pattern
  (`Db0RefusedError(ValueError)`), so a caller written against the established
  `except ValueError` idiom still catches this refusal.
- **`_flush_refusal_reason(command, db)`**: a pure predicate returning the
  refusal message or `None`. Factored out on purpose — it is the only way to
  test the *permitted* branch without ever issuing a real destructive command.
- **`GuardedRedis` / `GuardedPipeline` / `GuardedAsyncRedis`**: the three
  classes popoto constructs instead of the stock redis-py ones, each hooking
  `execute_command` and consulting the predicate.
- **`POPOTO_ALLOW_DB0_FLUSH`**: the deploy-level escape hatch, read at call
  time so it can be set without restarting a process and monkeypatched in
  tests.
- **`scripts/scratch_repro.py`**: a copy-me template that binds a free
  non-zero database before importing popoto, so the safe path is the easy one.

### Flow

Scratch script → `import popoto` → (no `REDIS_URL`, so bound to DB 0) →
`POPOTO_REDIS_DB.flushdb()` → **refusal naming DB 0, a free database, and
`POPOTO_ALLOW_DB0_FLUSH`** → author exports `REDIS_URL=redis://localhost:6379/4`
before the import → flush proceeds on DB 4 → live agent state intact.

### Technical Approach

**The recommended mechanism is a destructive-command refusal on popoto's own
client**, not a refusal to bind DB 0 and not an import-time warning. The
reasoning, and the options rejected, are below.

**Refusal semantics.** Two rules, both default-on:

| Command | Refused when | Rationale |
|---------|--------------|-----------|
| `FLUSHDB` | the client's pool is bound to `db == 0` | Wipes exactly the bound database. Harmless elsewhere. |
| `FLUSHALL` | always | Destroys every database including 0, whatever the client is bound to. A binding-conditional rule would miss the worst command. |

The bound database is read from
`connection_pool.connection_kwargs.get("db", 0) or 0` **at call time**, never
captured at construction. This is what makes the guard correct across
`_swap_db()` and `bind_connection()`, and it mirrors the resolution
`integrations.config.effective_db()` already performs. Use `.get(..., 0)`,
never a bare lookup — some pool constructions (unix-socket and certain URL
forms) carry no `db` key at all, a trap already recorded in #490.

**Where the guard hooks.** `execute_command`, per spike-1: it covers
`flushdb()`, `flushall()`, and a raw `execute_command("FLUSHDB")` in one
place, whereas overriding the named methods misses the raw form. The check is
a `frozenset` membership test on an upper-cased first argument, on a path that
already does msgpack encoding and a socket round trip.

**Four classes** (revised from three after CRITIQUE). `Redis.pipeline()`
hard-codes the stock `Pipeline` class, so `GuardedRedis.pipeline()` must
reassign the returned object's `__class__` to `GuardedPipeline`; reimplementing
the constructor call would bind the plan to a signature that moves between
redis-py versions. `GuardedAsyncRedis` needs an `async def execute_command`
override because the async client is a separate hierarchy — and
`redis.asyncio.Redis.pipeline()` hard-codes the stock async `Pipeline` exactly
as the sync one does, so a fourth class,
`GuardedAsyncPipeline(aioredis.client.Pipeline)`, is required, with its own
`async def execute_command` override plus a `GuardedAsyncRedis.pipeline()`
override that reassigns `pipe.__class__`. This is not hypothetical:
`src/popoto/models/query.py` calls `async_redis.pipeline()` on the live query
path. `pipeline()` is a plain, non-`async` method on **both** hierarchies — do
not `await` it.

**Definition order inside `redis_db.py`.** `PopotoException` is defined near
the bottom of the file, well after the module-level connection block, but the
guarded classes are needed *at* that block. Move the `PopotoException` class
definition above the connection block (a pure reorder within the one file, no
import change), then define `Db0FlushRefusedError`, `_flush_refusal_reason`,
and the four guarded classes immediately after it and before the first client
is constructed.

**`suggest_free_db()` is opt-out on the async path.** The suggestion helper
issues a *synchronous* `INFO keyspace` round trip on the sync global, which
would block the event loop for up to the socket timeout if called from an
`async def execute_command`. `_flush_refusal_reason` therefore takes
`suggest: bool = True`, and both async overrides pass `suggest=False`,
degrading to the already-specified no-suggestion message. Import
`suggest_free_db` **lazily, inside the function** — `popoto.redis_db` is
imported by `popoto/__init__`, so a module-level import of
`popoto.integrations.config` is an import cycle.

**Construction sites to change**, all inside `redis_db.py`:
the two module-level branches (the `REDIS_URL` branch and the localhost
fallback), both branches of `set_REDIS_DB_settings`, `get_async_redis_db`, and
both branches of `set_async_redis_db_settings`. Miss one and a runtime
reconfiguration silently drops the guard — `set_REDIS_DB_settings` is the
likeliest miss, since it is the documented reconfiguration entry point and
constructs a bare `redis.Redis` in both of its branches today.

**What the guard deliberately does not do**: it does not refuse to *bind* DB 0,
does not warn on binding, and does not touch reads or writes. A process on DB 0
keeps working exactly as it does today right up to the moment it tries to
destroy the database.

### Options weighed

| Option | Verdict |
|--------|---------|
| **A. Refuse to bind DB 0 at import time in `redis_db.py`** (the issue's remedy 1) | **Rejected.** `docs/configuration.md` documents `redis://localhost:6379/0` as the recommended URL and the no-`REDIS_URL` fallback *is* DB 0, so this turns `import popoto` into a crash for the entire documented zero-configuration path and every downstream PyPI adopter on a stock install. The issue itself says it "needs a maintainer decision and a release-note callout, not a quiet ship". It is also aimed at the wrong target: binding DB 0 is legitimate; flushing it is not. |
| **B. Import-time or first-touch DB-0 warning** | **Rejected as the primary mechanism.** A process-wide advisory fires for every legitimate DB-0 user of the ORM on every run, and Python's last-resort handler puts `logger.warning` on stderr for libraries that configure no logging. The pytest-scoped `PopotoIsolationWarning` from #595 already covers the population where the noise is acceptable. A warning also would not have stopped either incident: an agent that ignores a CLAUDE.md paragraph ignores a stderr line. |
| **C. Destructive-command refusal (recommended)** | Binds the exact command that caused both wipes, on the exact client both wipes used. Default-on with a deploy-level escape hatch, matching repo doctrine. Zero effect on any non-destructive operation, so no legitimate user is broken. Scoped to one file. |
| **D. Guarded scratch harness in `scripts/`** (the issue's remedy 2) | **Included as a secondary.** Cheap, and it makes the safe path the easy one, but it only helps someone who chooses to use it. Not sufficient alone. |
| **E. Documentation only** (remedy 3) | **Already partly shipped and demonstrably insufficient** — the second wipe followed the first. Included here as the docs cascade, not as a mechanism. |

### What this does NOT cover

Stated plainly so nobody reads more safety into the guard than it has:

- **`redis-cli` / `valkey-cli`.** Outside popoto entirely and always will be.
  Only convention and tooling reach it.
- **A bare `redis.Redis()` constructed by the caller.** Not popoto's object,
  not guarded.
- **`redis.call('FLUSHDB')` inside an `EVAL`ed Lua script** (spike-4). The
  guard sees `EVAL`. popoto ships no such script.
- **Other destructive commands**: `SHUTDOWN NOSAVE`, `DEBUG`,
  `CONFIG SET`, `SCRIPT FLUSH`, and mass `DEL`/`UNLINK` over a scanned
  keyspace. Only `FLUSHDB`/`FLUSHALL` are in scope; both incidents were
  `flushdb`, and widening the list invites a whack-a-mole with no natural
  boundary.
- **Raw connections checked out of the pool** and driven directly.
- **Binding to DB 0 at all** — permitted, by design (option A above).

## Failure Path Test Strategy

### Exception Handling Coverage
- The guard adds no `except Exception: pass`. It raises; it never swallows.
- Existing swallow sites in the touched file are unchanged. The module-level
  connection block's `except Exception: raise` and
  `suggest_free_db()`'s blanket `except Exception: return None` in the
  integrations layer both stay as they are.
- The message builder calls `suggest_free_db()`, which is best-effort and
  returns `None` on any failure. A test must assert that the refusal still
  raises with a usable message when the suggestion is unavailable, so a
  diagnostic can never become the reason the guard fails to fire.

### Empty/Invalid Input Handling
- `execute_command()` called with no arguments must not raise `IndexError`
  from the guard. Test it.
- A command name arriving as `bytes` or as a non-string must not break the
  membership test. Normalize before comparing, and test the `bytes` form.
- A pool whose `connection_kwargs` carries no `db` key must be treated as
  database 0, not crash. Test it (this is the #490 trap).

### Error State Rendering
- The refusal message must name the command, the database, the
  `POPOTO_ALLOW_DB0_FLUSH` variable, and — when available — a free database.
  Assert each of those substrings.
- `Db0FlushRefusedError` inherits `PopotoException`, so the message is logged
  at ERROR on construction. Assert the log record via `caplog`, since a caller
  that swallows the exception must still leave a trace.

## Test Impact

- [ ] `tests/test_pytest_plugin.py` — UPDATE only if a case asserts the exact
  type of `redis_db.POPOTO_REDIS_DB`. The plugin's own per-test `flushdb()`
  runs against the isolated database (15 by default, 4 for this plan's lane), so
  the guard never fires and no behavior changes.
- [ ] `tests/test_stress.py`, `tests/test_async.py`,
  `tests/test_meta_indexes.py`, `tests/test_meta_ttl.py`,
  `tests/test_meta_order_by.py`, `tests/test_sorted_field_ordering.py`,
  `tests/test_context_assembler_hybrid.py`,
  `tests/test_retrieval_quality_regression.py` — no change. Each calls
  `POPOTO_REDIS_DB.flushdb()` under the plugin's isolated database, which the
  guard permits. They are the regression evidence that the guard does not
  break ordinary flushing.
- [ ] `tests/test_db0_flush_guard.py` — CREATE. The new coverage.

No existing test asserts that a flush of DB 0 succeeds, so nothing has to be
rewritten.

## Rabbit Holes

- **Refusing to bind DB 0 at all.** Option A. It is the issue's own "real fix"
  and it is still the wrong first move: it breaks the documented default for
  every downstream user to prevent a command this plan already prevents. If a
  maintainer wants it later, it is a separate release-noted change.
- **Growing the destructive-command list.** `SHUTDOWN`, `CONFIG SET`,
  `SCRIPT FLUSH`, `DEBUG`, keyspace-wide `DEL`. There is no natural stopping
  point and no incident evidence. Two commands, both from the incident record.
- **Guarding Lua.** Parsing script bodies for `redis.call('FLUSHDB')` is a
  parser and a maintenance burden for a hole popoto's own scripts do not have.
  Document it instead.
- **Wrapping raw pool connections.** Reaching below the client means owning
  redis-py's connection lifecycle. Out.
- **A "safe mode" that redirects DB-0 writes elsewhere.** Silently repointing
  a connection is the exact behavior #584 decided against. Refuse loudly,
  never rebind.
- **Turning `flush_test_db()` into something clever.** It flushes the bound
  database and now inherits the guard for free. Leave the function alone and
  document the new failure mode.

## Risks

### Risk 1: A construction site is missed and the guard silently disappears
**Impact:** A process that calls `set_REDIS_DB_settings()` — the documented
reconfiguration entry point — gets an unguarded client back, and the guard
looks present while protecting nothing.
**Mitigation:** A test asserts the class of `POPOTO_REDIS_DB` after each
reconfiguration path (`set_REDIS_DB_settings` with kwargs, with positional
args, and with an explicit `connection_pool`), and after the plugin's
`_swap_db()`. A Verification row greps `redis_db.py` for any surviving bare
`redis.Redis(` / `aioredis.Redis(` construction.

### Risk 2: A legitimate DB-0 operator is broken by the default
**Impact:** Someone running popoto on database 0 in production and flushing it
deliberately gets an exception on upgrade.
**Mitigation:** `POPOTO_ALLOW_DB0_FLUSH=1` restores the old behavior with no
code change, per the repo's deploy-level-escape-hatch doctrine. The refusal
message names the variable. CHANGELOG calls it out as a behavior change.

### Risk 3: The `__class__` reassignment on pipelines breaks on another redis-py version
**Impact:** `pipeline()` raises, or the pipeline is silently unguarded, on
redis-py 8.
**Mitigation:** spike-2 verified 7.1.1 for both the buffered and the
`watch()`-immediate paths. Per `CLAUDE.md`, redis-py behavior differs between
7.x and 8.x — the pipeline test must be run in both environments before the
delta is trusted, and the environment stated alongside the result. If 8.x
diverges, fall back to documenting pipelines as an uncovered path rather than
reimplementing the constructor.

### Risk 4: A test accidentally flushes DB 0 while proving the opt-in works
**Impact:** The plan's own test suite reproduces the incident it exists to
prevent.
**Mitigation:** Structural, not procedural. The permitted branch is *only*
ever exercised through `_flush_refusal_reason(command, db)`, the pure
predicate, which issues no command. No test in this plan may call
`flushdb()`/`flushall()` on a client bound to database 0 with the opt-in set —
a Verification row greps the new test file to enforce it. Client-level tests
cover the refusing branch only, where the command never reaches the socket.

### Risk 5: Per-command overhead on the hot path
**Impact:** Every Redis command pays the check.
**Mitigation:** One `frozenset` membership test on an upper-cased string,
against a call that already performs msgpack encoding and a socket round trip.
Not measurable. No benchmark is warranted; do not add one.

## Race Conditions

No new race conditions. The guard is a synchronous, read-only check on the
arguments of a call already in flight; it introduces no shared mutable state,
no lock, and no ordering requirement.

One pre-existing interleaving is worth naming rather than fixing: the bound
database is read at call time, so a concurrent `_swap_db()` between the check
and the socket write could in principle move the connection. The window is the
same one that already exists for every command in the library, the swap paths
are single-threaded setup code (`pytest_configure`, `bind_connection`), and
the failure mode is a *refusal* on a database that just became non-zero, not a
missed refusal on database 0. Accepted.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #577] Nothing is deferred to another issue. Every item this
  plan commits to is completed within it.

Beyond that, the following are deliberate non-actions, each argued in
"Options weighed" or "Rabbit Holes" above, not deferrals: `redis_db.py` does
**not** refuse to bind database 0; no import-time or first-touch DB-0 warning
is added; the destructive-command list is **not** extended past
`FLUSHDB`/`FLUSHALL`; Lua script bodies are not parsed; raw pool connections
are not wrapped; and `redis-cli` is out of reach by construction.

## Update System

No update system changes required. The guard ships with the library and takes
effect on import; nothing is deployed or propagated separately.

## Agent Integration

No agent integration required. `popoto.integrations` reaches Redis through
`POPOTO_REDIS_DB` and therefore inherits the guard automatically, but no
integration surface, tool, or hook changes. `MemoryService`'s existing
`Db0RefusedError` is untouched and remains the earlier, stricter refusal for
that layer.

## Documentation

### Feature Documentation
- Not applicable. This is a safety guard on an existing connection, not a
  feature warranting a `docs/features/` page.

### External Documentation Site
- [ ] `docs/configuration.md` Environment Variables table: add a
  `POPOTO_ALLOW_DB0_FLUSH` row (default unset/falsy) describing the default-on
  refusal, the two rules, and what it does not cover.
- [ ] `docs/configuration.md`: a short prose note where the DB-0 default URL
  is recommended, so a reader who copies `redis://localhost:6379/0` learns the
  flush rule at the same moment.
- [ ] `docs/testing.md`: document that `popoto.testing.flush_test_db()` now
  raises on a DB-0 connection, and add the safe scratch-script pattern
  (`REDIS_URL` exported *before* `import popoto`, targeted deletes over
  blanket flushes) — the issue's remedy 3, which has never reached `docs/`.
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `redis_db.py` module docstring: a Safety section naming the guard, the
  two rules, the opt-in, and the four uncovered paths.
- [ ] Docstrings on `Db0FlushRefusedError`, `_flush_refusal_reason`, and each
  guarded class, following the file's existing style of explaining *why*.

### Repo Documentation
- [ ] `CLAUDE.md:23`: amend the ad-hoc-script paragraph to state that popoto's
  own client now refuses `FLUSHDB` on DB 0 and `FLUSHALL` anywhere, that
  `redis-cli` is still unguarded, and that `REDIS_URL` before the import
  remains the rule.
- [ ] `CHANGELOG.md`: a behavior-change entry naming `POPOTO_ALLOW_DB0_FLUSH`.
- [ ] `scripts/scratch_repro.py`: the guarded template, with a header comment
  explaining the import-time ordering requirement.

## Success Criteria

- [ ] `FLUSHDB` on a popoto client bound to database 0 raises
      `Db0FlushRefusedError` and never reaches the server
- [ ] `FLUSHALL` on a popoto client raises regardless of the bound database
- [ ] `FLUSHDB` on any non-zero database still succeeds
- [ ] The refusal covers the direct method, the raw `execute_command` form,
      the sync pipeline path, the async client, and the async pipeline path
- [ ] `popoto.testing.flush_test_db()` flushes the currently bound client, not
      an import-time snapshot
- [ ] The guard survives `set_REDIS_DB_settings()`, `_swap_db()`, and
      `bind_connection()`
- [ ] `POPOTO_ALLOW_DB0_FLUSH=1` restores the previous behavior, read at call
      time
- [ ] The refusal message names the command, the database, the environment
      variable, and a free database when one can be found
- [ ] No test in the suite executes a real flush against database 0
- [ ] Full suite green on `POPOTO_TEST_DB=4`; `ruff check src/` clean;
      `black --check src/ tests/` clean; mypy error count not increased
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (guard)**
  - Name: `db0-flush-guard-builder`
  - Role: Implement the predicate, the error, the three guarded classes, and
    every construction site in `redis_db.py`
  - Agent Type: builder
  - Resume: true

- **Test engineer (guard)**
  - Name: `db0-flush-guard-tests`
  - Role: Write `tests/test_db0_flush_guard.py`, including the
    never-flush-DB-0 discipline
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `db0-flush-guard-scribe`
  - Role: The docs cascade and the scratch-script template
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `db0-flush-guard-validator`
  - Role: Run every Verification row, in a stated environment
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement the guard in `redis_db.py`
- **Task ID**: build-guard
- **Depends On**: none
- **Validates**: `tests/test_db0_flush_guard.py` (create)
- **Informed By**: spike-1 (hook `execute_command`, not the named methods),
  spike-2 (three classes; `__class__` reassignment for pipelines), spike-5
  (`PopotoException` preserves the message)
- **Assigned To**: db0-flush-guard-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `Db0FlushRefusedError(PopotoException, ValueError)` and a module constant
  `ALLOW_DB0_FLUSH_ENV = "POPOTO_ALLOW_DB0_FLUSH"`
- Add `_flush_refusal_reason(command, db) -> str | None`: returns a message
  for `FLUSHDB` when `db == 0`, for `FLUSHALL` on any `db`, and `None`
  otherwise or when `POPOTO_ALLOW_DB0_FLUSH` is truthy. Read the environment
  **inside** the function, accepting the same truthy set as the integrations
  layer (`1`/`true`/`yes`/`on`). Normalize a `bytes` or non-string command
  before comparing
- Build the message from `suggest_free_db()` when it returns a value, and
  degrade to a still-actionable message when it returns `None`
- Add `GuardedRedis(redis.Redis)` with an `execute_command` override that
  reads the bound database as
  `self.connection_pool.connection_kwargs.get("db", 0) or 0`, and a
  `pipeline()` override that reassigns the returned object's `__class__` to
  `GuardedPipeline`
- Add `GuardedPipeline(redis.client.Pipeline)`,
  `GuardedAsyncRedis(aioredis.Redis)`, and
  `GuardedAsyncPipeline(aioredis.client.Pipeline)` with the same override
  (`async def execute_command` for the two async classes). Give
  `GuardedAsyncRedis` a `pipeline()` override that reassigns
  `pipe.__class__ = GuardedAsyncPipeline`, mirroring the sync one; `pipeline()`
  is NOT a coroutine on either hierarchy, so do not `await` it
- Give `_flush_refusal_reason` a `suggest: bool = True` parameter; both async
  overrides pass `suggest=False`. Import `suggest_free_db` lazily inside the
  function to avoid the `popoto/__init__` import cycle
- Move the `PopotoException` class definition above the module-level
  connection block so the guarded classes can be defined before the first
  client is constructed
- Fix `popoto/testing.py`'s `flush_test_db()` to call
  `redis_db.get_REDIS_DB().flushdb()` instead of the import-time
  `POPOTO_REDIS_DB` binding, so it flushes the database that is actually bound
  after `set_REDIS_DB_settings` rebinds the global
- Replace **every** construction site: both module-level branches, both
  branches of `set_REDIS_DB_settings`, `get_async_redis_db`, and both branches
  of `set_async_redis_db_settings`
- Extend the module docstring with the Safety section

### 2. Write the guard's tests
- **Task ID**: build-tests
- **Depends On**: build-guard
- **Validates**: `tests/test_db0_flush_guard.py`
- **Informed By**: spike-2 (pipeline and async are separate paths), spike-4
  (Lua is an accepted limit — assert it as documented behavior, not a bug)
- **Assigned To**: db0-flush-guard-tests
- **Agent Type**: test-engineer
- **Parallel**: false
- **Discipline, non-negotiable:** no test calls `flushdb()` or `flushall()` on
  a client bound to database 0 with the opt-in enabled. The permitted branch
  is tested only through `_flush_refusal_reason`
- Refusal cases through a real client bound to database 0: `flushdb()`,
  `execute_command("FLUSHDB")`, `execute_command(b"FLUSHDB")`, and a pipeline
  `flushdb()` — each raises before the socket
- `FLUSHALL` refused on a client bound to database 4
- `flushdb()` on database 4 succeeds
- Async: `await GuardedAsyncRedis(...).flushdb()` on database 0 refuses, and
  an async pipeline off that client refuses too (the fourth-class case)
- Client-level DB-0 refusal is proven **without a live DB-0 connection**: build
  the client against a pool whose `connection_kwargs` say `db=0` and
  monkeypatch the transport so the test fails loudly if the guard ever lets a
  command reach it
- Predicate unit tests: opt-in permits both commands; opt-in read at call time
  (set the variable after import via monkeypatch); a pool with no `db` key is
  treated as database 0; no-argument `execute_command` does not raise
  `IndexError`
- Class-persistence tests: `POPOTO_REDIS_DB` is a `GuardedRedis` after
  `set_REDIS_DB_settings(db=4)`, after the kwargs / positional-args /
  explicit-`connection_pool` branches, and after `pytest_plugin._swap_db(4)`
- Message tests: names the command, the database, `POPOTO_ALLOW_DB0_FLUSH`;
  names a free database when `suggest_free_db()` returns one and still raises
  usefully when it returns `None`
- Log test: `caplog` captures the ERROR record from `PopotoException.__init__`

### 3. Ship the guarded scratch template
- **Task ID**: build-scratch-harness
- **Depends On**: none
- **Validates**: Verification row "Scratch template never binds DB 0"
- **Assigned To**: db0-flush-guard-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `scripts/scratch_repro.py`: sets `os.environ["REDIS_URL"]` to a
  non-zero database **before** `import popoto`, refuses to run if the resolved
  database is 0, and demonstrates targeted deletes rather than a blanket flush
- Header comment explains the import-time ordering requirement and points at
  #577

### 4. Documentation cascade
- **Task ID**: document-guard
- **Depends On**: build-guard, build-scratch-harness
- **Assigned To**: db0-flush-guard-scribe
- **Agent Type**: documentarian
- **Parallel**: false
- `docs/configuration.md`: the `POPOTO_ALLOW_DB0_FLUSH` table row and the
  prose note beside the DB-0 URL recommendation
- `docs/testing.md`: `flush_test_db()`'s new failure mode plus the safe
  scratch-script pattern
- `CLAUDE.md:23`: amend the ad-hoc paragraph
- `CHANGELOG.md`: the behavior-change entry

### 5. Validate
- **Task ID**: validate-all
- **Depends On**: build-guard, build-tests, build-scratch-harness,
  document-guard
- **Assigned To**: db0-flush-guard-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every row in the Verification table and report pass/fail
- State the redis-py version alongside the pipeline result, and re-run the
  pipeline test under the other major version before the result is trusted
  (`CLAUDE.md`, worktree rule 5)
- Confirm `redis-cli -n 0 dbsize` is unchanged before and after the suite

## Verification

Run with `POPOTO_TEST_DB=4` so the lane touches neither DB 0 nor the shared
DB 15. **Standing constraint on this table: no row may bind database 0.**
DB-0 refusal is verified through the pure predicate and through tests whose
transport is monkeypatched to fail if touched. (This is stated as a rule
rather than a grep row, because a row that greps this file for its own
pattern always matches itself.)

| Check | Command | Expected |
|-------|---------|----------|
| Guard tests pass | `POPOTO_TEST_DB=4 pytest tests/test_db0_flush_guard.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=4 pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| FLUSHDB refused on DB 0 (predicate only — **no verification command may bind database 0**) | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto import redis_db as r; assert r._flush_refusal_reason('FLUSHDB', 0) is not None"` | exit code 0 |
| Refusal fires before the socket | `POPOTO_TEST_DB=4 pytest tests/test_db0_flush_guard.py -q -k never_reaches_server` | exit code 0 (client-level DB-0 refusal is proven only against a pool whose `connection_kwargs` say `db=0`, with the transport monkeypatched to fail the test if it is ever touched) |
| Refusal names the opt-in | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto import redis_db as r; print(r._flush_refusal_reason('FLUSHDB', 0))"` | output contains POPOTO_ALLOW_DB0_FLUSH |
| FLUSHALL refused off DB 0 | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.flushall()"` | exit code != 0 |
| Non-zero DB still flushable | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.flushdb()"` | exit code 0 |
| Refusal reason is `None` off DB 0 | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto import redis_db as r; print(r._flush_refusal_reason('FLUSHDB', 4))"` | output contains None |
| No unguarded sync construction | `grep -c "= redis.Redis(" src/popoto/redis_db.py` | match count == 0 (docstring examples included) |
| No unguarded async construction | `grep -c "= aioredis.Redis(" src/popoto/redis_db.py` | match count == 0 |
| Guard survives reconfiguration | `REDIS_URL=redis://localhost:6379/4 python -c "import popoto; from popoto import redis_db as r; r.set_REDIS_DB_settings(db=4); assert isinstance(r.POPOTO_REDIS_DB, r.GuardedRedis)"` | exit code 0 |
| Tests never flush DB 0 with the opt-in | `grep -c "POPOTO_ALLOW_DB0_FLUSH.*\(flushdb\|flushall\)" tests/test_db0_flush_guard.py` | match count == 0 |
| Guard tests never bind database 0 | `grep -c "6379/0" tests/test_db0_flush_guard.py` | match count == 0 (pre-existing `tests/test_integrations_db0_isolation.py` legitimately binds DB 0 to assert the #584 *refusal*; it runs no flush) |
| Scratch template never binds DB 0 | `grep -c "6379/0" scripts/scratch_repro.py` | match count == 0 |
| Env var documented | `grep -c "POPOTO_ALLOW_DB0_FLUSH" docs/configuration.md` | output > 0 |
| Safe pattern documented | `grep -c "before .import popoto." docs/testing.md` | output > 0 |
| CLAUDE.md amended | `grep -c "POPOTO_ALLOW_DB0_FLUSH" CLAUDE.md` | output > 0 |
| CHANGELOG entry | `grep -c "POPOTO_ALLOW_DB0_FLUSH" CHANGELOG.md` | output > 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

**redis-py dual-version result (Risk 3 / Task 5, performed).** The guard test
file and the pipeline paths were run under both major versions on Python
3.13.2: redis-py **8.1.0** (26 guard tests pass; full suite 3481 passed / 27
skipped) and redis-py **7.1.1** (26 guard tests plus 43 pytest-plugin tests
pass; sync and async pipelines both return the guarded subclass, chain
normally, and refuse `FLUSHALL` at queue time and on the post-`watch()`
immediate path). The `__class__`-reassignment form does not diverge between
the two versions, so the fallback contemplated in Risk 3 is not needed.

Report the mypy delta as base-vs-branch counts in a stated environment
(`mypy src/` on the merge base, then on the branch, same interpreter and same
redis-py version). The guard adds typed code only; the expected delta is zero
new errors.

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness | Async pipelines are unguarded. `redis.asyncio.Redis.pipeline()` hard-codes `return Pipeline(self.connection_pool, ...)` (verified, redis-py 7.1.1), so `GuardedAsyncRedis.pipeline()` returns a stock async Pipeline. `src/popoto/models/query.py:3501` and `:3784` already use `async_redis.pipeline()`. The Success Criterion claiming the refusal covers "the pipeline path and the async client" is unmet by the three-class design. | Task 1 (build-guard) + Task 2 (build-tests): add a fourth class | Add `GuardedAsyncPipeline(aioredis.client.Pipeline)` with an `async def execute_command` override, and give `GuardedAsyncRedis` a `pipeline()` override that reassigns `pipe.__class__ = GuardedAsyncPipeline`. `pipeline()` is a plain (non-async) method on both hierarchies — do NOT `await` it; reassign `__class__` on the returned object, mirroring the sync override. |
| BLOCKER | History & Consistency | The Verification row "FLUSHDB refused on DB 0" runs a real `POPOTO_REDIS_DB.flushdb()` against `redis://localhost:6379/0`. If the guard is absent, misbuilt, or the wrong package is imported, that row performs the exact live DB-0 wipe this plan exists to prevent — contradicting Risk 4, whose no-flush-DB-0 discipline is scoped only to `tests/test_db0_flush_guard.py`. The Prerequisites check that is supposed to protect it is a substring test (`'src/popoto' in str(path)`) that passes on ANY popoto checkout: run from this worktree it exits 0 while `popoto.__file__` is `/Users/valorengels/src/popoto/src/popoto/__init__.py` (main checkout) and `pip show popoto` reports the editable install at a third tree, `.worktrees/cooccurrence_edge_weight_clamp`. Verified. | Prerequisites table + Verification table | Replace the prerequisite with an exact-path comparison run from the worktree root: `python -c "import popoto, pathlib, sys; sys.exit(0 if pathlib.Path(popoto.__file__).resolve() == pathlib.Path('src/popoto/__init__.py').resolve() else 1)"`. Rewrite the destructive row to assert through the pure predicate — `r._flush_refusal_reason('FLUSHDB', 0) is not None` — and never bind `redis://localhost:6379/0` in any verification command. |
| CONCERN | Risk & Robustness | The refusal message builder calls `suggest_free_db()`, which issues a synchronous `POPOTO_REDIS_DB.info("keyspace")` round trip on the sync global (`integrations/config.py:287-311`). Invoked from `GuardedAsyncRedis.execute_command` (an `async def`), that blocks the event loop for the duration of the INFO call — up to the 5s socket timeout on an unreachable server. | Task 1 (build-guard) | Gate the lookup behind a parameter: `_flush_refusal_reason(command, db, suggest=True)`, and have the async override pass `suggest=False` so it degrades to the already-specified no-suggestion message. Import `suggest_free_db` lazily INSIDE the function — `popoto.redis_db` is imported by `popoto/__init__`, so a module-level import of `popoto.integrations.config` would be a cycle. |
| CONCERN | History & Consistency | The Freshness Check calls `flush_test_db()` "a public helper that flushes whatever database is bound". It does not. `testing.py:46` binds `POPOTO_REDIS_DB` at import time, while `set_REDIS_DB_settings` REBINDS the module global to a new client object (unlike `_swap_db`/`bind_connection`, which swap the pool in place — the plan's Data Flow section relies on that distinction). The documented `use_test_db(15)` + `flush_test_db()` conftest pattern in `testing.py`'s own docstring therefore already flushes the import-time snapshot, and the new guard catches it only when that snapshot happens to be on db 0. | Task 1 or Task 4 (docs cascade) — pick one and say which | One-line fix in `testing.py`: have `flush_test_db()` call `redis_db.get_REDIS_DB().flushdb()` (accessor already exported) instead of the import-time `POPOTO_REDIS_DB` binding. If deferred instead, correct the Freshness Check claim and add an explicit `docs/testing.md` warning that `use_test_db()` + `flush_test_db()` do not compose today, independent of the guard. |
| CONCERN | Scope & Value | The lane database is inconsistent throughout. Prerequisites checks `redis-cli -n 4` while its own Purpose column says "DB 13 is used"; Verification pins `POPOTO_TEST_DB=4` and `.../4`; Spike Results, Test Impact ("13 for this plan's lane"), Risk 4 and Task 2's cases all say 13. The Flow example is self-contradictory: exporting `REDIS_URL=redis://localhost:6379/4` cannot make "flush proceeds on DB 13". | Whole plan — Prerequisites, Solution/Flow, Test Impact, Risks, Tasks, Verification | Not cosmetic: a validator running the Verification table as written uses DB 4 while the tests built in Task 2 assert against DB 13. Pick one number and rewrite every occurrence, including the Flow sentence and `set_REDIS_DB_settings(db=13)` in Task 2. |
| NIT | History & Consistency | `Db0FlushRefusedError` is specified to subclass `PopotoException` (plain `Exception`), but Prior Art claims it follows the #584 house pattern, where `integrations/config.py:225` defines `Db0RefusedError(ValueError)`. A caller written against the established `except ValueError` idiom will not catch the new refusal. | Task 1 (build-guard) | — |
| NIT | Scope & Value | The Success Criterion "No test in the suite executes a real flush against database 0" has no Verification row that checks it. The existing grep row matches only a flush and the opt-in string on one line — a stricter, different pattern. | Verification table | — |

## Open Questions

None. The mechanism choice is settled in "Options weighed": the destructive-
command refusal ships, the bind-time refusal does not, and the import-time
warning does not. `POPOTO_ALLOW_DB0_FLUSH` is the escape-hatch name, chosen so
it cannot be confused with the integrations layer's `POPOTO_MEMORY_ALLOW_DB0`,
which grants a different permission.
