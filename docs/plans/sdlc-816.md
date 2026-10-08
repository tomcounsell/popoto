---
status: Planning
type: bug
appetite: Small
owner: Solo dev
created: 2026-10-08
tracking: https://github.com/tomcounsell/popoto/issues/816
last_comment_id:
---

# Postgres Outage Classification and Instance Binding (#816)

Implementation branch: `fix/pg-outage-and-backend-binding`.

## Problem

Two places in the backend plumbing still assume Redis. Both block 1.10.0 and
ship together.

**Defect 1: a Postgres outage is not an outage outside `ContextAssembler`.**
`BackendUnavailableError` (`src/popoto/backends/types.py:95`) subclasses the
builtin `ConnectionError`, not `redis.exceptions.ConnectionError`, so it is
not matched by `redis_db.OUTAGE_ERRORS` (`src/popoto/redis_db.py:818`, the
Redis pair). Only `recipes/context_assembler.py:97` widens the tuple. The
other consumers import the Redis-only one:

- `recipes/subconscious_memory.py:66` (import), `:427` / `:590` / `:996`:
  `except OUTAGE_ERRORS: raise` precedes `except Exception`. A Postgres outage
  falls through to the generic handler, so `inject_context` returns the turn
  with an empty `AssemblyResult()` ("outage reads as no memories", the exact
  outcome the comment at `:428-430` forbids), extraction drops the write with
  "Failed to save extracted memory", and outcome reporting logs and moves on.
- `integrations/service.py:44` (import), `:808` (`_record_failure`):
  `isinstance(exc, OUTAGE_ERRORS)` trips `self._redis_down`, the breaker that
  `:269` / `:273` / `:820` consult. On Postgres it never trips, so every hook
  call retries a dead server.
- `transfer/cli.py:408-413` and `:477-482` name `redis_exceptions.ConnectionError`
  explicitly. A `BackendUnavailableError` is caught only incidentally via the
  `OSError` entry. Plan-time finding (spike-3): the same tuples list the
  *builtin* `TimeoutError`, which `redis.exceptions.TimeoutError` does not
  subclass (MRO: `TimeoutError -> RedisError -> Exception`), so a **Redis**
  timeout during `popoto-transfer export/import` escapes the handler today as
  a traceback instead of a one-line error and exit 1.

**Defect 2: `set_backend(instance)` is ignored by `Meta.backend = "postgres"`
models.** `_resolve()` (`backends/__init__.py:854-862`) returns
`_instance(explicit)` for any model with `Meta.backend` before it looks at the
`set_backend` default. `_instance("postgres")` (`:832-851`) builds once from
`postgres.backend_from_env()` (`backends/postgres/__init__.py:237`), which reads
only `POPOTO_POSTGRES_URL`, and caches the result in `_instances`. A
`PostgresBackend(dsn=...)` handed to `set_backend` is never consulted for such
a model, contrary to the module docstring (`backends/postgres/__init__.py:13-14`)
and `docs/features/postgres-backend.md:65-67`.

**Current behavior:**
- Postgres outage inside `SubconsciousMemory` -> logged warning, empty
  context / dropped write / dropped outcomes, turn continues as if healthy.
- Postgres outage in a harness hook -> breaker never trips, every later hook
  call retries the dead server.
- `set_backend(PostgresBackend(dsn=A))` + `Meta.backend="postgres"` model ->
  `BackendUnavailableError` when `POPOTO_POSTGRES_URL` is unset, or silently
  talks to the *environment's* database B when it is set.

**Desired outcome:**
- One backend-neutral outage tuple, consumed by every "is this an outage?"
  decision in `src/`, so `ContextAssembler`, `SubconsciousMemory`, the
  integrations service and `popoto-transfer` cannot drift again.
- A `set_backend` instance serves every model (and every name-based lookup)
  whose backend name matches the instance's `name`; a different name keeps
  resolving by name. `redis_db.OUTAGE_ERRORS` and the `set_backend` signature
  are unchanged.

## Freshness Check

**Baseline commit:** `7e9d1c20` (origin/main at plan time). `src/` and
`tests/` are byte-identical to the issue's base `e8552e69`
(`git diff --stat e8552e69 origin/main -- src tests` is empty); the two
intervening commits add `docs/plans/harness-postgres.md` only.
**Issue filed at:** 2026-10-08T05:22:12Z
**Disposition:** Unchanged (with an Overlap surfaced: #814, below)

**File:line references re-verified:**
- `backends/types.py:95` — `class BackendUnavailableError(BackendError, ConnectionError)` — still holds.
- `redis_db.py:818` — `OUTAGE_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)` — still holds.
- `recipes/context_assembler.py:97` — widened tuple — still holds.
- `recipes/subconscious_memory.py:66,427,590,996` — still holds.
- `integrations/service.py:44,808` (and breaker reads `:269,:273,:820`) — still holds.
- `transfer/cli.py:408-413,477-482` — still holds (`redis_exceptions` imported function-locally at `:384` / `:462`).
- `backends/__init__.py` — `set_backend` `:900`, `_instance` `:832`, `_resolve` `:854`, `_swap_instance` `:914` — still holds.
- `backends/postgres/__init__.py:13-14,237` — still holds.
- `pytest_plugin.py:1098-1105` — `_swap_instance("postgres", instance)` workaround — still holds.

**Reproduced on the baseline** (scratch script, `REDIS_URL=redis://localhost:6379/14`,
`POPOTO_POSTGRES_URL` unset, `PYTHONPATH` = this checkout):
`isinstance(e, redis_db.OUTAGE_ERRORS)` False, `isinstance(e, context_assembler.OUTAGE_ERRORS)`
True, `subconscious_memory.OUTAGE_ERRORS is redis_db.OUTAGE_ERRORS` True,
`service.OUTAGE_ERRORS is redis_db.OUTAGE_ERRORS` True; `set_backend(Fake())`
then `_resolve(M)` raised `BackendUnavailableError: ... POPOTO_POSTGRES_URL is not set`.

**Cited sibling issues/PRs re-checked:**
- #755 — OPEN (umbrella).
- #759 — OPEN (v2 Postgres backend); its M2c PR #779 (merged 2026-10-04) added the widened tuple in `ContextAssembler` only.
- #811 — CLOSED via PR #813; same release train, no overlap in touched code.

**Commits on main since issue was filed (touching referenced files):** none.

**Active plans in `docs/plans/` overlapping this area:** `harness-postgres.md`
(#814, Planning, filed 05:13Z) touches `integrations/service.py` and names this
issue as a dependency for the `_redis_down` breaker (its Freshness Check, lines
92-97). Coordination rule: **this plan owns `service.py:44` and `:808` only**
(the import and the `isinstance` check); it does not rename `_redis_down` or
touch any of the service's Redis bookkeeping, which #814 is porting. If #814
lands first and moves those lines, rebase and re-point the import; the change
is two lines.

## Prior Art

- **PR #779** (feat #759 M2c): introduced `context_assembler.OUTAGE_ERRORS =
  _REDIS_OUTAGE_ERRORS + (BackendUnavailableError,)`. Correct, but local to one
  module, which is the drift this plan removes.
- **PR #594** (agent-memory production audit): introduced `redis_db.OUTAGE_ERRORS`
  and the "outages raise, quality failures degrade" contract in the recipes,
  when Redis was the only backend.
- **PR #656** (#648): routed `ContextAssembler` through the field layer; kept
  the outage re-raises.
- **PRs #732 / #733 / #738 / #739 / #768** (#631, #759 M1a): built the backend
  selection layer (`set_backend`, `_resolve`, `_instances`, `_bound`) and the
  conformance harness. The plugin's `_swap_instance` call was added there to
  make Postgres-leg instances visible to `Meta.backend` models; nothing
  recorded that `Meta.backend` should *override* an instance default.
- No closed issue or merged PR attempted either fix before.

## Research

No relevant external findings — the work is internal to popoto's exception
taxonomy and backend registry; no third-party API or ecosystem pattern is
involved. One environment fact was checked locally rather than searched:
`redis.exceptions.TimeoutError` (redis-py 7.1.1) does not subclass the builtin
`TimeoutError` (see spike-3).

## Spike Results

### spike-1: Is `_resolve` the only name-to-instance lookup?
- **Assumption**: "Fixing `_resolve` is enough for Defect 2."
- **Method**: code-read (`grep -rn '_instance(\|_resolve(' src/popoto`)
- **Finding**: False. `_instance(name)` is also called directly by
  `streams/__init__.py:47` (`resolve_stream_backend(backend="postgres")`) and
  `pubsub/publisher.py:271,278` (a `UnitOfWork` whose `.backend` is
  `"postgres"`, and `_pubsub_backend = "postgres"`). Patching only `_resolve`
  would let a model resolve to the `set_backend` instance while a publish that
  joins *that model's* Postgres transaction resolves by name to an
  env-built backend on another DSN: a split brain.
- **Confidence**: high
- **Impact on plan**: the instance match goes in `_instance(name)` itself, so
  every name-based lookup agrees; `_resolve` needs no separate branch.

### spike-2: Does a module import cycle block a neutral tuple in `backends/types.py`?
- **Assumption**: "`backends/types.py` can define the tuple next to the error."
- **Method**: code-read
- **Finding**: `backends/types.py:31` already imports `PopotoException` from
  `..redis_db`, and `redis_db` imports nothing from `backends`. Defining
  `OUTAGE_ERRORS = _redis_db.OUTAGE_ERRORS + (BackendUnavailableError,)` there
  adds no new edge. `subconscious_memory`, `integrations/service` and
  `transfer/cli` can all import from `..backends.types` (context_assembler
  already does).
- **Confidence**: high
- **Impact on plan**: canonical home is `popoto.backends.types.OUTAGE_ERRORS`,
  re-exported as `popoto.backends.OUTAGE_ERRORS`.

### spike-3: Does `popoto-transfer` handle a Redis timeout?
- **Assumption**: "transfer/cli.py is hygiene only."
- **Method**: code-read + `python -c` MRO check (redis 7.1.1)
- **Finding**: Its tuples list the builtin `TimeoutError`, but
  `redis.exceptions.TimeoutError.__mro__` is `(TimeoutError, RedisError,
  Exception, BaseException, object)` — not the builtin. A Redis timeout
  escapes the handler as a traceback.
- **Confidence**: high
- **Impact on plan**: replacing `redis_exceptions.ConnectionError` with the
  neutral tuple fixes this for free; one test covers it.

### spike-4: What existing tests encode the current precedence?
- **Assumption**: "Some test asserts `Meta.backend` beats an instance default."
- **Method**: code-read of `tests/test_backend_selection.py`,
  `tests/test_backend_planning.py`, `tests/test_models_route_through_backend.py`
- **Finding**: `test_meta_backend_wins_over_the_default` uses an instance named
  `"recording"` and a `Meta.backend="redis"` model; under the new rule the
  names differ, so it still resolves by name and passes unchanged. The other
  instance defaults (`RecordingBackend` named `"recording"`; `Recording`
  inheriting `name="redis"`) serve only models without `Meta.backend`, which
  already took the instance. No test asserts the old precedence.
- **Confidence**: high (verified by the full suite at build time)
- **Impact on plan**: no test deletions; additive tests only.

## Data Flow

**Outage (Defect 1):**
1. **Entry point**: an agent turn calls `SubconsciousMemory.inject_context` /
   `extract_memories` / `report_outcomes`, or a harness hook calls a
   `MemoryService` method.
2. **Model operation**: a Postgres-bound model's query/save reaches
   `PostgresBackend`, which raises `BackendUnavailableError` on refused
   connect, connect/statement timeout, or missing DSN/extra.
3. **Recipe boundary** (`subconscious_memory.py:427/590/996`): the
   `except OUTAGE_ERRORS: raise` clause must match it; today it does not and
   `except Exception` degrades. After the fix it propagates.
4. **Harness boundary** (`integrations/service.py`): the service's own
   `except Exception` calls `_record_failure`, whose `isinstance(exc,
   OUTAGE_ERRORS)` sets `_redis_down`; later calls short-circuit at
   `:269/:273/:820`.
5. **Output**: the hook still fails open (returns `""`), but stops retrying a
   dead server for the rest of the process; a direct recipe caller sees the
   exception instead of an empty context.

**Resolution (Defect 2):**
1. **Entry point**: `set_backend(PostgresBackend(dsn=A))`, then any use of a
   `Meta.backend="postgres"` model (or `resolve_stream_backend(backend="postgres")`,
   or a publish joining a Postgres `UnitOfWork`).
2. **`get_backend(model)` -> `_resolve(model)`**: sees `Meta.backend`, calls
   `_instance("postgres")`.
3. **`_instance(name)`** (the fix point): if `_default` is a non-string
   instance whose `name == "postgres"`, return it; else the `_instances` cache;
   else build from env and cache.
4. **`_ensure_bound(backend, model_cls)`**: memoised by `id(backend)`, so a
   different instance binds afresh; `set_backend` already clears `_bound`.
5. **Output**: the model's operations run on DSN A.

## Architectural Impact

- **New dependencies**: none. `subconscious_memory`, `integrations/service`
  and `transfer/cli` gain an import of `..backends.types` (already imported by
  `context_assembler`; spike-2 found no cycle).
- **Interface changes**: one new public name, `popoto.backends.OUTAGE_ERRORS`
  (also `popoto.backends.types.OUTAGE_ERRORS`). `redis_db.OUTAGE_ERRORS` keeps
  its exact value. `context_assembler.OUTAGE_ERRORS` remains importable (it
  becomes the same object as the neutral tuple; the docs name it). `set_backend`
  signature unchanged; its *semantics* widen: an instance now also serves
  `Meta.backend` models of the same name.
- **Coupling**: decreases. Outage classification has one definition instead
  of a Redis tuple plus a per-module widening.
- **Data ownership**: unchanged.
- **Reversibility**: trivial; three import lines and one branch in `_instance`.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (scope fixed by the issue's acceptance criteria)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 15 ping` | Full suite (DB 15 via `popoto_test_db`) |
| Dev extras installed | `python -c "import pytest, popoto"` | Test runner |

No Postgres server is required: every new test drives `BackendUnavailableError`
directly or uses an instance that never connects (spike-4). Postgres-leg tests
in `tests/postgres/` skip without `POSTGRES_URL`; if it is available locally,
running them is a bonus check that the plugin's `_swap_instance` pairing still
works.

## Solution

### Key Elements

- **Neutral outage tuple**: `popoto.backends.types.OUTAGE_ERRORS` = the Redis
  pair + `BackendUnavailableError`, re-exported from `popoto.backends`. The
  single answer to "is this an outage?".
- **Consumers re-pointed**: `ContextAssembler`, `SubconsciousMemory`, the
  integrations service and `popoto-transfer` import the neutral tuple.
- **Drift guard**: a test that fails if any module under `src/popoto` other
  than `backends/types.py` imports `OUTAGE_ERRORS` from `redis_db`.
- **Instance-aware name lookup**: `_instance(name)` returns the `set_backend`
  instance when its `name` matches.

### Flow

`set_backend(PostgresBackend(dsn=A))` -> define `Meta.backend="postgres"`
model -> `Model.save()` -> `get_backend` -> `_resolve` -> `_instance("postgres")`
-> **the instance on DSN A** (not `POPOTO_POSTGRES_URL`).

Postgres down -> `SubconsciousMemory.inject_context` -> `BackendUnavailableError`
-> **re-raised** (not an empty context). Hook path -> `_record_failure` ->
**`_redis_down = True`** -> later hook calls return `""` without a round trip.

### Technical Approach

**Defect 1**
- In `src/popoto/backends/types.py`, after `BackendUnavailableError`, define
  `OUTAGE_ERRORS: tuple[type[BaseException], ...] = _REDIS_OUTAGE_ERRORS +
  (BackendUnavailableError,)` with `from ..redis_db import OUTAGE_ERRORS as
  _REDIS_OUTAGE_ERRORS` (next to the existing `PopotoException` import). Add
  it to `types.__all__` and to `backends/__init__.py`'s import list and
  `__all__`. Docstring: an outage, not a bad query; `BackendRetryableError`
  (pool/lock contention, `types.py:136-141`) is deliberately **not** in it.
- `recipes/context_assembler.py:85-97`: replace the local widening with
  `from ..backends.types import OUTAGE_ERRORS` (keep the module-level name so
  `context_assembler.OUTAGE_ERRORS` stays importable; it is now the same
  object). Keep the explanatory comment, re-pointed at the shared definition.
- `recipes/subconscious_memory.py:66`: import from `..backends.types`. The
  three `except OUTAGE_ERRORS: raise` sites are untouched.
- `integrations/service.py:44`: import from `..backends.types`. `:808` is
  untouched. Do not rename `_redis_down` (#814 owns the service's naming).
- `transfer/cli.py:408-413` and `:477-482`: replace
  `redis_exceptions.ConnectionError` with the neutral tuple's members
  (`*OUTAGE_ERRORS`, or list `BackendUnavailableError` and the Redis pair);
  keep `TimeoutError`, `OSError`, `ModelException`, `QueryException`, and
  `KeyboardInterrupt` on export. Drop the now-unused function-local
  `redis_exceptions` imports at `:384` / `:462` if nothing else uses them.
  `:286` (DB-0 guard) is out of scope.
- `redis_db.py:813-818`: value unchanged; amend the comment to say it is the
  Redis pair and point backend-neutral callers at `popoto.backends.OUTAGE_ERRORS`.

**Defect 2**
- In `_instance(name)` (`backends/__init__.py:832`), first, under `_lock`:
  `current = _default; if current is not None and not isinstance(current, str)
  and getattr(current, "name", None) == name: return current`. Only then the
  `_instances` cache and the env build.
- Precedence, decided here: **matching `set_backend` instance > `_instances`
  cache (including `_swap_instance`) > env build.** The instance must win over
  the cache, or a process that resolved `"postgres"` from env before calling
  `set_backend(instance)` would keep the cached env backend: the bug again.
- The `set_backend` instance is **not** written into `_instances`. So
  `set_backend(None)` (or a different instance) cleanly stops serving it, and
  the next `_instance("postgres")` falls back to the cache/env, with no stale
  entry to evict. `_bound` is keyed by `id(backend)` and already cleared by
  `set_backend`, so no memoisation change is needed.
- `_resolve` keeps its shape; a `Meta.backend` naming a different backend than
  the instance still resolves by name (Redis-default process hosting a
  Postgres-pinned model, and vice versa).
- `pytest_plugin.py:1098-1105`: keep the `_swap_instance` call. It is now
  redundant for the matching-name case but harmless, and other tests rely on
  `_swap_instance` directly; update its comment to say so. Removing it is a
  No-Go (see below).
- Update `set_backend`'s docstring to state the rule, and the
  `backends/postgres/__init__.py:13-14` docstring stays true as written.

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
