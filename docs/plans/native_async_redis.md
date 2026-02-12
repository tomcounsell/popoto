---
status: Ready
type: feature
appetite: Medium
owner: valorengels
created: 2026-02-11
tracking: https://github.com/tomcounsell/popoto/issues/123
---

# Native Async Redis Support (redis.asyncio)

## Problem

All `async_*` methods in Popoto currently wrap synchronous Redis calls in `asyncio.to_thread()`. Every async operation spawns a thread-pool task for what should be non-blocking I/O.

**Current behavior:**
Every `await Model.async_create(...)`, `await Model.query.async_get(...)`, etc. pays thread-pool overhead. The `redis-py` library has supported native async via `redis.asyncio` since v4.2+, and Popoto already requires `redis>=4.3.4`.

**Desired outcome:**
Read-path async methods (`async_get`, `async_filter`, `async_all`, `async_count`, `async_keys`, `async_load`) use `redis.asyncio` directly for true non-blocking I/O. Write-path methods (`async_save`, `async_delete`, `async_create`, bulk ops) keep `to_thread()` where field hooks prevent full async, with clear documentation explaining why.

## Appetite

**Size:** Medium

**Team:** Solo dev + PM. 1 check-in to align on scope (which methods go native vs stay thread-pool), 1 review round.

**Interactions:**
- PM check-ins: 1 (scope alignment on field hook boundary)
- Review rounds: 1 (code review)

## Prerequisites

No prerequisites -- `redis>=4.3.4` is already a dependency and includes `redis.asyncio`.

## Solution

### Key Elements

- **Lazy async connection** (`redis_db.py`): Thread-safe, lazy-initialized `redis.asyncio.Redis` client via `get_async_redis_db()` with double-check locking pattern
- **Native async reads** (`query.py`): `async_get`, `async_all`, `async_count`, `async_keys` use `redis.asyncio` directly; `async_filter` uses hybrid approach (sync filter logic + async bulk loading)
- **Native async batch loading** (`query.py`): New `_async_get_many_objects()` uses async pipelines for `HGETALL`/`HMGET`
- **Native async_load** (`base.py`): Delegates to `Query.async_get()` instead of wrapping sync `load()`
- **Documented thread-pool boundary**: Write methods (`async_save`, `async_delete`, `async_create`, bulk ops) stay on `asyncio.to_thread()` because field hooks (`on_save`, `on_delete`) are synchronous across 8+ field types
- **Python 3.9+ minimum**: Drop Python 3.8 support, remove custom `to_thread` backport, use stdlib `asyncio.to_thread` directly

### Flow

**Async read path:** Caller → `async_get()`/`async_filter()` → `get_async_redis_db()` → `redis.asyncio.Redis` → non-blocking I/O → decode → return model

**Async write path (unchanged):** Caller → `async_save()` → `to_thread(save)` → sync field hooks → `redis.Redis` → return

### Technical Approach

- Add `get_async_redis_db()` with lazy init + `asyncio.Lock()` for thread safety
- Convert Query read methods to call `redis.asyncio` directly
- Add `_async_get_many_objects()` for pipelined bulk loads (mirrors sync `get_many_objects`)
- Convert `async_load()` to use `Query.async_get()` natively
- Remove custom `to_thread` backport; use `asyncio.to_thread` directly (Python 3.9+)
- Update `python_requires` to `>=3.9` in `pyproject.toml`
- Add `set_async_redis_db_settings()` and `async_check_connection()` for testing/reconfiguration

## Rabbit Holes

- **Making field hooks async**: Converting `on_save()`/`on_delete()` across all 8+ field types (KeyFieldMixin, SortedFieldMixin, GeoField, Relationship, UniqueFieldMixin, etc.) would be a massive refactor for marginal gain. Thread-pool overhead on writes is negligible vs Redis network latency.
- **Async pub/sub**: The existing pub/sub uses a polling pattern that works fine with event loops. Native async pub/sub is a separate concern.
- **Connection pooling tuning**: The default `redis.asyncio` connection pool is sufficient. Custom pool configuration is a separate optimization.

## Risks

### Risk 1: Event loop conflicts at import time
**Impact:** `asyncio.Lock()` created at module level could conflict with some event loop implementations
**Mitigation:** Lock is created at module scope (safe per Python docs), async connection itself is lazy-initialized only when first awaited

### Risk 2: Async connection not cleaned up on shutdown
**Impact:** Unclosed async connections could leak resources in long-running processes
**Mitigation:** `redis.asyncio.Redis` handles its own connection pool cleanup automatically. Document this in the `get_async_redis_db()` docstring so it's not a hidden gotcha for devs.

## No-Gos (Out of Scope)

- Async field hooks (`on_save`, `on_delete`) -- separate project, massive scope
- Async pub/sub -- separate concern, different usage pattern
- Connection pool customization or tuning
- Async bulk operations beyond thread-pool wrapping

## Update System

No update system changes required -- this is a library-internal change with no new dependencies or configuration.

## Agent Integration

No agent integration required -- this is a library-internal performance improvement.

## Documentation

### Inline Documentation
- [x] Docstrings updated on all converted async methods explaining native async vs thread-pool
- [x] Module docstring in `redis_db.py` updated with async usage examples
- [ ] Verify docs build passes with `mkdocs serve`

### Feature Documentation
- [ ] Update `docs/` if async usage guide exists

## Success Criteria

- [ ] `get_async_redis_db()` returns a lazy-initialized `redis.asyncio.Redis` client
- [ ] `Query.async_get()` uses native async (no `to_thread`)
- [ ] `Query.async_all()` uses native async (no `to_thread`)
- [ ] `Query.async_count()` uses native async for unfiltered counts
- [ ] `Query.async_keys()` uses native async for non-clean operations
- [ ] `Query.async_filter()` uses native async for object loading phase
- [ ] `Query._async_get_many_objects()` uses async pipelines
- [ ] `Model.async_load()` uses native async via `Query.async_get()`
- [ ] All existing async tests pass (`pytest tests/test_async.py`)
- [ ] Write-path methods document why they use `to_thread()`
- [ ] `set_async_redis_db_settings()` works for test reconfiguration
- [ ] `python_requires` set to `>=3.9` in `pyproject.toml`
- [ ] Custom `to_thread` backport removed from `base.py` and `query.py` (use `asyncio.to_thread` directly)

## Team Orchestration

### Team Members

- **Builder (async-redis)**
  - Name: async-redis-builder
  - Role: Implement native async Redis methods and connection management
  - Agent Type: builder
  - Resume: true

- **Validator (async-redis)**
  - Name: async-redis-validator
  - Role: Verify all async methods work correctly and tests pass
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Implement async connection and read methods
- **Task ID**: build-async-redis
- **Depends On**: none
- **Assigned To**: async-redis-builder
- **Agent Type**: builder
- **Parallel**: false
- Update `python_requires` to `>=3.9` in `pyproject.toml`
- Remove custom `to_thread` backport from `base.py` and `query.py`; use `asyncio.to_thread` directly
- Add `get_async_redis_db()` with lazy init and `asyncio.Lock()` to `redis_db.py`
- Add `set_async_redis_db_settings()` and `async_check_connection()` to `redis_db.py`
- Convert `Query.async_get()` to native async with `redis.asyncio`
- Convert `Query.async_all()` to native async with `SMEMBERS` + async pipeline loading
- Convert `Query.async_count()` to native async `SCARD` for unfiltered, hybrid for filtered
- Convert `Query.async_keys()` to native async `SMEMBERS`/`KEYS`
- Add `Query._async_get_many_objects()` with async pipelines
- Convert `Model.async_load()` to delegate to `Query.async_get()`
- Add documentation comments to write-path methods explaining `to_thread()` rationale

### 2. Validate implementation
- **Task ID**: validate-async-redis
- **Depends On**: build-async-redis
- **Assigned To**: async-redis-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_async.py` -- all tests pass
- Run `pytest` -- full test suite passes, no regressions
- Verify `get_async_redis_db()` is imported and used in query.py and base.py
- Verify no `to_thread` remains in read-path methods (async_get, async_all, async_load)
- Verify write-path methods still use `to_thread` with explanatory comments
- Run `mypy src/` -- no new type errors

### 3. Final Validation
- **Task ID**: validate-all
- **Depends On**: validate-async-redis
- **Assigned To**: async-redis-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met
- Generate final report

## Validation Commands

- `pytest tests/test_async.py -v` -- all async tests pass
- `pytest` -- full test suite passes
- `mypy src/` -- no type errors
- `grep -n "to_thread" src/popoto/models/query.py` -- only in filter_for_keys_set, clean keys, and counted filtered paths
- `grep -n "get_async_redis_db" src/popoto/models/query.py src/popoto/models/base.py` -- imported and used
- `grep -rn "run_in_executor\|functools.partial.*to_thread" src/popoto/` -- no custom backport remains
- `grep "python_requires" pyproject.toml` -- shows `>=3.9`

---

## Resolved Questions

1. ~~**Python 3.8 support**~~ — Dropping 3.8, minimum is now Python 3.9+. Custom `to_thread` backport will be removed.
2. ~~**Async connection cleanup**~~ — Rely on `redis.asyncio`'s built-in connection pool cleanup. No explicit `async_close()` needed. Add a note in the `get_async_redis_db()` docstring so devs know the connection is managed automatically.
3. ~~**Existing working tree changes**~~ — Validate and incorporate the existing uncommitted changes in `redis_db.py`, `base.py`, and `query.py` into the feature branch.
