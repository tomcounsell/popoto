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
- A non-Redis `set_backend` instance (today: `"postgres"`) serves every model
  (and every name-based lookup) whose backend name matches the instance's
  `name`; a different name keeps resolving by name. `Meta.backend = "redis"`
  resolution is unchanged (critique concern 1: the issue's Redis
  backward-compatibility constraint governs). `redis_db.OUTAGE_ERRORS` and the
  `set_backend` signature are unchanged.

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
3. **`_instance(name)`** (the fix point): if `name != "redis"` and `_default`
   is a non-string instance whose `name == name`, return it; else the
   `_instances` cache; else build from env and cache. `_instance("redis")`
   never consults `_default`, exactly as today.
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
  signature unchanged; its *semantics* widen for non-Redis names only: a
  Postgres-named instance now also serves `Meta.backend="postgres"` models.
  `Meta.backend="redis"` resolution is byte-for-byte unchanged.
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
  instance when its `name` matches and the name is not `"redis"`.

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
  and name != "redis" and getattr(current, "name", None) == name: return
  current`. Only then the `_instances` cache and the env build.
- **Why `name != "redis"`** (critique concern 1): the issue requires Redis
  behavior to stay backward compatible. Today `set_backend(MyRedisBackend())`
  (name `"redis"`) serves only un-pinned models, and `Meta.backend="redis"`
  models get the stock cached `RedisBackend()`. A symmetric rule would move
  those pinned models onto the custom instance, a Redis behavior change. No
  concrete reason requires symmetry: the plugin's Redis leg sets no instance
  (`pytest_plugin.py` binds an instance only on the Postgres leg), and the
  defect the issue names is Postgres-only. The exclusion is one condition and
  is pinned by a test (see Test Impact).
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
  Postgres-pinned model, and vice versa), and a `Meta.backend="redis"` model
  resolves to the cached stock `RedisBackend` even under a Redis-named
  instance default.
- `pytest_plugin.py:1098-1105`: keep the `_swap_instance` call. It is now
  redundant for the matching-name case but harmless, and other tests rely on
  `_swap_instance` directly; update its comment to say so. Removing it is a
  No-Go (see below).
- Update `set_backend`'s docstring to state the rule, and the
  `backends/postgres/__init__.py:13-14` docstring stays true as written.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The three `except Exception` handlers in `subconscious_memory.py`
  (`:432`, `:592`, `:998`) are the defect: each gets a test asserting a
  `BackendUnavailableError` raised underneath **propagates** (not logged and
  degraded). Each also keeps a sibling assertion that a non-outage exception
  (e.g. `ValueError`) still degrades with the existing warning, so the fix
  cannot be "remove the `except Exception`".
- [ ] `integrations/service.py::_record_failure`: a test feeds a
  `BackendUnavailableError` and asserts `_redis_down is True`, and that a later
  `context()` call short-circuits to `""` without calling the assembler; a
  `ValueError` leaves `_redis_down` False.
- [ ] `transfer/cli.py` handlers: export and import each get a test where the
  transfer function raises `BackendUnavailableError` -> one stderr line naming
  the message, exit 1, no traceback; plus one with
  `redis.exceptions.TimeoutError` (spike-3, a real behavior change: today a
  traceback).
- [ ] `except Exception: pass` blocks in `_record_failure` (log-file write,
  counter `INCR`) are pre-existing best-effort writes, not in scope.

### Empty/Invalid Input Handling
- [ ] `_instance(name)` when `_default` is an instance **without** a `name`
  attribute (`getattr(..., None)`): never matches; falls through as today.
- [ ] `_default` a string (`set_backend("postgres")`): unchanged path, no
  instance match.
- [ ] `set_backend(None)` after `set_backend(instance)`: the next
  `_instance("postgres")` builds from env (raises `BackendUnavailableError`
  with `POPOTO_POSTGRES_URL` unset), proving the instance was not cached.

### Error State Rendering
- [ ] `popoto-transfer` is the only user-visible renderer: tests assert the
  stderr text (`popoto-transfer export: <message>` / `popoto-transfer import:
  <message>`) and exit code 1, with `capsys`.

## Test Impact

No existing test needs to change. Verified per spike-4:

- [ ] `tests/test_backend_selection.py::test_meta_backend_wins_over_the_default` — no change: its instance is named `"recording"`, the explicit model is `"redis"`, names differ so it still resolves by name. It becomes the regression test for "a different name still resolves by name" in one direction; a new test covers the other.
- [ ] `tests/postgres/test_postgres_outage.py::test_selecting_postgres_without_a_url_names_the_variable` — no change: no instance default is set, so the env path still raises.
- [ ] `tests/postgres/conftest.py`, `pytest_plugin.py` conformance leg, and the ~10 `tests/postgres/*` sites pairing `set_backend(be)` with `_swap_instance("postgres", be)` — no change: both point at the same instance, so precedence between them is moot.
- [ ] `tests/test_integrations_service.py::test_status_survives_an_unreachable_server` / `test_feedback_degrades_quietly_when_redis_is_down` — no change: they raise the builtin `ConnectionError`, which is not in either tuple, before and after.

New tests (additive):
- `tests/test_outage_errors.py` (create): tuple membership; `redis_db.OUTAGE_ERRORS` value unchanged; `context_assembler.OUTAGE_ERRORS is popoto.backends.OUTAGE_ERRORS`; `BackendRetryableError` not a member; the AST drift guard (no module under `src/popoto` except `backends/types.py` imports `OUTAGE_ERRORS` from `redis_db`).
- `tests/test_subconscious_memory.py`: three outage-propagation tests + three non-outage-degrades siblings (stub the assembler / save / `ObservationProtocol.on_context_used` to raise).
- `tests/test_integrations_service.py`: breaker trips on `BackendUnavailableError`.
- `tests/test_transfer_cli.py`: export/import with `BackendUnavailableError` and with `redis.exceptions.TimeoutError`.
- `tests/test_backend_selection.py`: instance with `name="postgres"` serves a `Meta.backend="postgres"` model with `POPOTO_POSTGRES_URL` unset (use a `RedisBackend` subclass/stand-in named `"postgres"` or a stub whose `bind` returns capabilities, so no server is needed); a `Meta.backend="redis"` model under a Postgres-named instance still gets `RedisBackend`; instance beats a previously cached env/`_instances` entry; `set_backend(None)` stops serving it; `resolve_stream_backend(backend="postgres")` and the publisher's `_instance(pipeline.backend)` path return the same instance (spike-1).

## Rabbit Holes

- **Renaming `_redis_down` or porting the service's Redis bookkeeping** — that
  is #814's scope; touching it here guarantees a merge conflict.
- **Translating raw redis exceptions into `BackendUnavailableError` inside
  `RedisBackend`** — tempting "one type to rule them all", but it changes
  what Redis users catch today (`redis.exceptions.ConnectionError`), which the
  issue forbids. The tuple is the compatible unifier.
- **Making `_swap_instance` public or deleting it** — it has ~15 test call
  sites and a plugin use; it stays private and unchanged.
- **Auditing every `except Exception` in `src/` for outage leaks** — the issue
  scopes this to modules that already *consult* an outage tuple. A wider sweep
  is a separate question (see No-Gos).
- **Matching instances by type (`isinstance(current, PostgresBackend)`)
  instead of `name`** — `name` is the protocol's identity (`default_backend_name`
  already uses it) and avoids importing psycopg-adjacent modules.

## Risks

### Risk 1: A Redis-named custom instance now serves `Meta.backend="redis"` models
**Impact:** `set_backend(MyRedisBackend())` (name `"redis"`) previously left
explicitly-Redis models on the stock `RedisBackend()`; now they use the custom
instance. Anyone relying on that split would see different behavior.
**Mitigation:** this is the issue's stated rule applied symmetrically and is
what the docstrings promise. No in-repo test or caller relies on the split
(spike-4; `Recording` in `test_backend_planning.py` is named `"redis"` but
serves only un-pinned models). Call it out in the CHANGELOG entry.

### Risk 2: Widening the tuple changes which errors the recipes swallow
**Impact:** code paths that used to log-and-degrade on a Postgres outage now
raise into the caller.
**Mitigation:** that is the fix and matches the 1.9.0 Redis contract; the
harness boundary (`hooks.run`, MCP dispatcher, `MemoryService`) already
catches. Redis behavior is unchanged because the Redis pair is unchanged and a
Redis-bound model never raises `BackendUnavailableError`.

### Risk 3: Conflict with #814 in `integrations/service.py`
**Impact:** both lanes edit the same file.
**Mitigation:** this plan touches only the import line; coordination rule in
the Freshness Check. Whichever merges second rebases.

## Race Conditions

### Race 1: `set_backend` concurrent with `_instance`
**Location:** `backends/__init__.py:832-851`, `:900-911`
**Trigger:** one thread calls `set_backend(instance)` while another resolves a
`Meta.backend` model.
**Data prerequisite:** none beyond `_default`.
**State prerequisite:** the reader sees either the old or the new default,
never a torn value.
**Mitigation:** read `_default` once into a local inside `_instance`'s existing
`with _lock:` block (both functions already take the same `RLock`). A thread
that resolved just before the swap may finish one operation on the old
backend; that is the existing semantics for un-pinned models and is
documented ("discards memoised bindings").

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #814] Service-side Redis bookkeeping on a Postgres process,
  and any rename of `_redis_down`.
- [ORDERED] Removing the now-redundant `_swap_instance("postgres", ...)` call
  in `pytest_plugin.py` and the tests' paired calls: harmless, and removing
  them belongs after 1.10.0 ships so this release-blocker diff stays minimal;
  waits on the 1.10.0 release tag.

Not a deferral: `transfer/cli.py:286` (the Redis DB-0 guard) is legitimately
Redis-specific and was dropped in the issue's recon. Nothing else is deferred;
every acceptance criterion is in scope.

## Update System

No update system changes required — this is a library-internal fix with no
new dependencies, config, or migration. Downstream users get it with the
1.10.0 release.

## Agent Integration

No agent integration required. The integrations service (the hook/MCP path an
agent runs) is fixed in place by Defect 1; no new tool surface is added.

## Documentation

### Feature Documentation
- [ ] `docs/features/context-assembler.md:273-295`: teach
  `popoto.backends.OUTAGE_ERRORS` as the tuple to catch (both backends);
  keep a sentence that `popoto.redis_db.OUTAGE_ERRORS` is the Redis pair and
  that `context_assembler.OUTAGE_ERRORS` is now the same object as the neutral
  one.
- [ ] `docs/guides/subconscious-memory-recipe.md:286-300` ("Redis outages
  raise"): extend to Postgres (`BackendUnavailableError`) and switch the
  example import to `popoto.backends.OUTAGE_ERRORS`.
- [ ] `docs/features/llm-memory-extraction.md:122`: mention
  `BackendUnavailableError` alongside the Redis pair.
- [ ] `docs/features/harness-integration.md:332-338`: the one-attempt breaker
  also trips on a Postgres `BackendUnavailableError`.
- [ ] `docs/features/postgres-backend.md:65-67` and `:577-579`: state the
  instance rule (a `set_backend` instance serves `Meta.backend="postgres"`
  models too) and point outage catching at `popoto.backends.OUTAGE_ERRORS`.
- [ ] `CHANGELOG.md` `[Unreleased]` / `### Fixed`: one entry for #816 covering
  both defects, the Redis-timeout-in-transfer side fix, and Risk 1's behavior
  note.

### External Documentation Site
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] Docstrings: `backends.types.OUTAGE_ERRORS`, `set_backend`, `_instance`;
  comments at `redis_db.py:813-818` and `context_assembler.py:88-96`.

## Success Criteria

- [ ] `SubconsciousMemory.inject_context`, `extract_memories` (save path) and
  `report_outcomes` re-raise `BackendUnavailableError`, each with a test; a
  non-outage exception still degrades, each with a test.
- [ ] `MemoryService._record_failure` sets `_redis_down` on
  `BackendUnavailableError`, with a test.
- [ ] `popoto-transfer` export and import report `BackendUnavailableError` (and
  `redis.exceptions.TimeoutError`) through a handler that names the outage
  tuple: one stderr line, exit 1, with tests.
- [ ] `set_backend(<instance named "postgres">)` serves a
  `Meta.backend="postgres"` model with `POPOTO_POSTGRES_URL` unset, with a test.
- [ ] A `Meta.backend="postgres"` model under a Redis-instance default resolves
  to Postgres-by-name, and a `Meta.backend="redis"` model under a
  Postgres-instance default resolves to Redis, with tests.
- [ ] `_instance("postgres")` from streams and pubsub returns the same
  instance as the model path (spike-1), with a test.
- [ ] `redis_db.OUTAGE_ERRORS` value is unchanged; no module under
  `src/popoto` other than `backends/types.py` imports it (drift guard test).
- [ ] `set_backend` signature unchanged.
- [ ] Full suite passes (`pytest`), `ruff check src/`, `black --check src/ tests/`,
  `scripts/mypy_ratchet.py` (no rise), `mkdocs build --strict`.
- [ ] Documentation updated (`/do-docs`), CHANGELOG entry added.
- [ ] No xfail tests relate to this bug (searched: none), so none to convert.

## Team Orchestration

### Team Members

- **Builder (backend-core)**
  - Name: outage-binding-builder
  - Role: Implement both defects plus tests and docs on `fix/pg-outage-and-backend-binding`
  - Agent Type: builder
  - Domain: Redis/Popoto data (paste the Redis/Popoto rules from `DOMAIN_FRAMING.md`; ad-hoc scripts set `REDIS_URL=redis://localhost:6379/<n≠0>` before importing popoto)
  - Resume: true

- **Validator (backend-core)**
  - Name: outage-binding-validator
  - Role: Verify success criteria and the Verification table, state the environment with every count
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Neutral outage tuple and consumers (Defect 1)
- **Task ID**: build-outage-tuple
- **Depends On**: none
- **Validates**: tests/test_outage_errors.py (create), tests/test_subconscious_memory.py, tests/test_integrations_service.py, tests/test_transfer_cli.py
- **Informed By**: spike-2 (no import cycle; home is `backends/types.py`), spike-3 (transfer misses Redis timeouts)
- **Assigned To**: outage-binding-builder
- **Agent Type**: builder
- **Parallel**: true
- Define `OUTAGE_ERRORS` in `backends/types.py`; export from `backends/__init__.py` (`__all__` too).
- Re-point `context_assembler.py`, `subconscious_memory.py`, `integrations/service.py` imports; leave the `except` sites untouched.
- Replace `redis_exceptions.ConnectionError` in both `transfer/cli.py` handlers with the neutral tuple; drop unused `redis_exceptions` imports.
- Amend the `redis_db.py:813-818` comment (value unchanged).
- Write the tests listed under Test Impact for these four modules, including the AST drift guard and the non-outage-degrades siblings.

### 2. Instance-aware `_instance` (Defect 2)
- **Task ID**: build-instance-binding
- **Depends On**: none
- **Validates**: tests/test_backend_selection.py, tests/postgres/ (skips without POSTGRES_URL)
- **Informed By**: spike-1 (fix in `_instance`, not `_resolve`), spike-4 (no test asserts the old precedence)
- **Assigned To**: outage-binding-builder
- **Agent Type**: builder
- **Parallel**: true
- In `_instance(name)`, under `_lock`, return `_default` when it is a non-string instance with `getattr(_default, "name", None) == name`; do not write it into `_instances`.
- Update `set_backend` / `_instance` docstrings and the `pytest_plugin.py:1098` comment (call stays).
- Write the resolution tests listed under Test Impact (instance serves pinned model with env unset; cross-name pins still resolve by name in both directions; instance beats a cached entry; `set_backend(None)` falls back; streams/pubsub agree).

### 3. Validate code
- **Task ID**: validate-code
- **Depends On**: build-outage-tuple, build-instance-binding
- **Assigned To**: outage-binding-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table; run the full suite with `POPOTO_TEST_DB=<free n>` and report environment (redis-py version, extras, DB).
- Re-run the scratch repro from the Freshness Check against the branch: all four `isinstance`/`is` checks flip as expected and `_resolve(M)` returns the instance.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-code
- **Assigned To**: outage-binding-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Apply every item in the Documentation section, including the CHANGELOG entry.
- `mkdocs build --strict`.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: outage-binding-validator
- **Agent Type**: validator
- **Parallel**: false
- Re-run the Verification table and confirm every Success Criterion.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest -q -p no:cacheprovider` | exit code 0 |
| Targeted tests pass | `pytest -q tests/test_outage_errors.py tests/test_backend_selection.py tests/test_subconscious_memory.py tests/test_integrations_service.py tests/test_transfer_cli.py` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet | `scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Neutral tuple includes PG outage | `REDIS_URL=redis://localhost:6379/15 python -c "from popoto.backends import OUTAGE_ERRORS, BackendUnavailableError as B; print(issubclass(B, OUTAGE_ERRORS))"` | output contains True |
| Redis tuple unchanged | `REDIS_URL=redis://localhost:6379/15 python -c "import redis; from popoto.redis_db import OUTAGE_ERRORS as R; print(R == (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError))"` | output contains True |
| No src module imports the Redis tuple except types.py | `grep -rn "redis_db import OUTAGE_ERRORS" src/popoto \| grep -v "backends/types.py" \| wc -l` | match count == 0 |
| Transfer no longer names only the redis ConnectionError | `grep -c "redis_exceptions.ConnectionError" src/popoto/transfer/cli.py` | match count == 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

None that block the build. The issue's two planner questions are decided
above, recorded here so critique can challenge them:

1. **Where the neutral tuple lives, and `redis_db.OUTAGE_ERRORS`'s meaning.**
   Decided: `popoto.backends.types.OUTAGE_ERRORS` (re-exported from
   `popoto.backends`); `redis_db.OUTAGE_ERRORS` keeps its exact Redis-pair
   value for external importers. Alternative rejected: widening
   `redis_db.OUTAGE_ERRORS` in place — simpler, but puts a backend-neutral
   definition in the Redis module and silently changes an exported value.
2. **Precedence and memoisation.** Decided: matching `set_backend` instance >
   `_instances` cache / `_swap_instance` > env build; the instance is never
   cached in `_instances`, so `set_backend(None)` or a replacement needs no
   eviction; `_bound` needs no change (keyed by `id(backend)`, cleared by
   `set_backend`). The rule is name-symmetric, so a custom instance named
   `"redis"` now also serves `Meta.backend="redis"` models (Risk 1). If the
   maintainer wants the rule Postgres-only, it is a one-condition change.
