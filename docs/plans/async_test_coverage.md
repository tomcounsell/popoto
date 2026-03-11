---
status: Ready
type: chore
appetite: Medium
owner: Valor
created: 2026-03-11
tracking: https://github.com/tomcounsell/popoto/issues/172
last_comment_id:
---

# Async Test Coverage: 17 Missing Scenarios

## Problem

PR #130 added native `redis.asyncio` support across the public API. The current async test suite (`tests/test_async.py`, 273 lines) covers only the happy-path CRUD basics: `async_create`, `async_save`, `async_filter`, `async_get`, `async_delete`, `async_all`, `async_count`, `async_load`, `async_keys`, plus limit/order_by/concurrent operations.

**Current behavior:**
17 async code paths have zero test coverage. These include bulk operations, GeoField queries, relationship lazy-loading, `values=` projections, error paths (`async_get` on duplicates/missing keys), connection management (`async_check_connection`, `set_async_redis_db_settings`, `async_scan_keys`), client-side filters, `Meta.order_by`, `Meta.ttl`, pipeline-based saves, and write contention.

**Desired outcome:**
Every public async method has at least one focused test exercising its primary code path. The gaps identified in issue #172 are fully closed with passing tests.

## Prior Art

- **PR #130**: "Native async Redis support (redis.asyncio) + Python 3.9+" -- the original async implementation (merged 2026-02-11). Added the async methods but only basic tests.
- **PR #60**: "Add async support for Model and Query operations" -- earlier async support via `to_thread()`, superseded by PR #130.
- **PR #169**: "Remove flaky async Redis ping test and apply ruff formatting" -- removed a flaky async connection test (merged 2026-03-11). Confirms async connection tests are fragile and need care.
- **PR #148**: "Make save() atomic via internal pipeline" -- made sync save atomic. The async path uses `to_thread()` wrapping this, so pipeline behavior is relevant.
- **Issue #147**: "save() without pipeline is non-atomic" -- closed with PR #148. Relevant context for Gap 16 (async_save with pipeline arg).

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (requirements are fully specified in issue #172)
- Review rounds: 1 (PR review)

This is 17 test scenarios but each is a focused, self-contained test function. No production code changes needed -- purely additive test code. The models and patterns already exist in sync tests to reference.

## Prerequisites

No prerequisites -- this work requires only a running Redis instance on localhost:6379, which is already the standard test environment.

## Solution

### Key Elements

- **Test model definitions**: Reuse existing `TestJob` from `test_async.py` where possible. Define additional models (with `GeoField`, `Relationship`, `Meta.ttl`, `Meta.order_by`, plain `Field` for client-side filtering) as needed within the test file.
- **Async test patterns**: All tests use `@pytest.mark.asyncio` and the existing `flush_redis` fixture that resets the async connection per test.
- **Connection tests**: Add `TestAsyncCheckConnection` and `TestAsyncConnectionReconfiguration` classes to `tests/test_connection.py`. Add `TestAsyncScanKeys` to `tests/test_async.py`.
- **Bulk operation tests**: Add `TestAsyncBulkOperations` class to `tests/test_bulk_operations.py`.
- **Migration tests**: Add `TestAsyncRebuildIndexes` to `tests/test_migrations.py`.

### Technical Approach

- Each gap maps to 1-3 test functions
- Tests follow the existing pattern: create data with `async_create`/`async_save`, then assert via the async method under test
- Connection tests use `unittest.mock.patch` to simulate failures, matching the sync test patterns in `test_connection.py`
- The `flush_redis` fixture from `test_async.py` must be duplicated or imported into files that add async tests (`test_connection.py`, `test_bulk_operations.py`, `test_migrations.py`)
- Concurrency tests use `asyncio.gather` to fire parallel operations

## Failure Path Test Strategy

### Exception Handling Coverage
- Gap 6 (`async_get` raises on >1 match) directly tests an exception path
- Gap 9 (`async_check_connection`) tests ConnectionError and TimeoutError handling
- No new `except Exception: pass` blocks are created -- this is test-only code

### Empty/Invalid Input Handling
- Gap 7 (`async_get` returns None on miss) tests the empty/missing-key input path
- Gap 5 (`values=` projection) will verify dict output vs model instances

### Error State Rendering
- Not applicable -- no user-visible UI in this library

## Rabbit Holes

- **Performance benchmarking async vs sync**: Not in scope. Tests verify correctness, not performance.
- **Testing actual network failures**: Mocking Redis errors is sufficient. Don't set up Docker containers or kill Redis mid-test.
- **Exhaustive operator coverage for SortedField**: Gap 2 needs focused tests for `__lte`, `__lt`, `__gt`, `__between`. Don't expand into every possible operator combination.
- **Fixing any bugs discovered**: If a test reveals a bug in the async implementation, document it and mark the test with `@pytest.mark.xfail(reason="...")`. Don't fix production code in this PR.

## Risks

### Risk 1: Async event loop conflicts between test files
**Impact:** Tests pass in isolation but fail when run together (the classic pytest-asyncio issue)
**Mitigation:** Each file's `flush_redis` fixture resets `_POPOTO_ASYNC_REDIS_DB = None` and recreates `_async_redis_lock`. The fixture is `autouse=True` per test.

### Risk 2: GeoField/Relationship models require specific Redis data structures
**Impact:** Tests could be brittle if model setup is complex
**Mitigation:** Keep test models minimal (1-2 fields beyond the GeoField/Relationship). Copy patterns from existing sync tests (`test_geofield.py`, `test_relationship.py`).

## Race Conditions

### Race 1: Concurrent async_save to same key (Gap 17)
**Location:** `src/popoto/models/base.py` async_save (uses `to_thread`)
**Trigger:** Multiple coroutines calling `async_save` on the same Redis key simultaneously
**Data prerequisite:** Object must exist in Redis before concurrent writes
**State prerequisite:** Multiple coroutines sharing the same event loop
**Mitigation:** The test verifies last-write-wins behavior -- it doesn't prevent races, it documents the behavior. Redis MULTI/EXEC in save() provides per-key atomicity but not cross-coroutine serialization.

## No-Gos (Out of Scope)

- Fixing bugs in async implementation (xfail and file a new issue instead)
- Modifying production code (`src/popoto/`)
- Performance/benchmark tests
- Testing private/internal async methods not in the public API
- Adding async equivalents of sync tests that don't correspond to the 17 gaps

## Update System

No update system changes required -- popoto is a library, not a deployed service.

## Agent Integration

No agent integration required -- popoto is a standalone library with no agent/bridge/MCP components.

## Documentation

- [ ] Update `docs/async.md` to note improved test coverage and list the tested scenarios
- [ ] Add inline docstrings to each new test function describing what gap it covers

## Success Criteria

- [ ] All 17 gaps from issue #172 have at least one passing test
- [ ] `pytest tests/test_async.py tests/test_connection.py tests/test_bulk_operations.py tests/test_migrations.py -v` passes with 0 failures
- [ ] Full suite `pytest tests/` passes with no new failures introduced
- [ ] Each test function has a docstring referencing the gap number (e.g., "Gap 1: async_bulk_create")
- [ ] Tests pass (`/do-test`)

## Team Orchestration

### Team Members

- **Builder (async-tests)**
  - Name: async-test-builder
  - Role: Implement all 17 async test gaps across 4 test files
  - Agent Type: test-writer
  - Resume: true

- **Validator (test-suite)**
  - Name: test-validator
  - Role: Verify all tests pass in isolation and as full suite
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add async tests to test_async.py (Gaps 2, 3, 4, 5, 6, 7, 11, 12, 13, 14, 15, 16, 17)
- **Task ID**: build-async-tests
- **Depends On**: none
- **Assigned To**: async-test-builder
- **Agent Type**: test-writer
- **Parallel**: true
- Define additional test models as needed:
  - `AsyncGeoModel` with `GeoField` for Gap 4
  - `AsyncRelModel` + `AsyncRelTarget` with `Relationship` for Gap 3
  - `AsyncOrderModel` with plain `Field` for client-side filters (Gap 13)
  - `AsyncOrderByModel` with `class Meta: order_by = "priority"` for Gap 14
  - `AsyncTTLModel` with `class Meta: ttl = 5` for Gap 15
- Implement test functions:
  - `test_async_filter_sorted_lte` / `test_async_filter_sorted_lt` / `test_async_filter_sorted_gt` / `test_async_filter_sorted_between` (Gap 2)
  - `test_async_relationship_lazy_load` (Gap 3)
  - `test_async_filter_geofield` (Gap 4)
  - `test_async_filter_values_projection` / `test_async_all_values_projection` (Gap 5)
  - `test_async_get_raises_on_multiple_matches` (Gap 6)
  - `test_async_get_returns_none_on_miss` (Gap 7)
  - `test_async_scan_keys` (Gap 11)
  - `test_async_keys_catchall` / `test_async_keys_clean` (Gap 12)
  - `test_async_filter_client_side` (Gap 13)
  - `test_async_filter_meta_order_by` / `test_async_all_meta_order_by` (Gap 14)
  - `test_async_save_meta_ttl` (Gap 15)
  - `test_async_save_with_pipeline` (Gap 16)
  - `test_async_concurrent_save_same_key` (Gap 17)

### 2. Add async bulk operation tests to test_bulk_operations.py (Gap 1)
- **Task ID**: build-bulk-tests
- **Depends On**: none
- **Assigned To**: async-test-builder
- **Agent Type**: test-writer
- **Parallel**: true
- Add `flush_redis_async` fixture (reset `_POPOTO_ASYNC_REDIS_DB` and `_async_redis_lock`)
- Implement `TestAsyncBulkOperations` class:
  - `test_async_bulk_create_basic`
  - `test_async_bulk_update_from_all`
  - `test_async_bulk_delete_from_all`

### 3. Add async connection tests to test_connection.py (Gaps 9, 10)
- **Task ID**: build-connection-tests
- **Depends On**: none
- **Assigned To**: async-test-builder
- **Agent Type**: test-writer
- **Parallel**: true
- Add `flush_redis_async` fixture
- Implement `TestAsyncCheckConnection` class (Gap 9):
  - `test_async_check_connection_success`
  - `test_async_check_connection_failure` (mock ConnectionError)
  - `test_async_check_connection_timeout` (mock TimeoutError)
- Implement `TestAsyncConnectionReconfiguration` class (Gap 10):
  - `test_set_async_redis_db_settings_reconnects`

### 4. Add async rebuild_indexes test to test_migrations.py (Gap 8)
- **Task ID**: build-migration-tests
- **Depends On**: none
- **Assigned To**: async-test-builder
- **Agent Type**: test-writer
- **Parallel**: true
- Add `flush_redis_async` fixture
- Implement `TestAsyncRebuildIndexes` class:
  - `test_async_rebuild_indexes`

### 5. Validate all tests
- **Task ID**: validate-tests
- **Depends On**: build-async-tests, build-bulk-tests, build-connection-tests, build-migration-tests
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_async.py -v` -- all new tests pass
- Run `pytest tests/test_connection.py -v` -- all new tests pass
- Run `pytest tests/test_bulk_operations.py -v` -- all new tests pass
- Run `pytest tests/test_migrations.py -v` -- all new tests pass
- Run `pytest tests/ -v` -- full suite passes with no regressions

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-tests
- **Assigned To**: async-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Update `docs/async.md` to note the expanded test coverage
- Verify all test docstrings reference their gap number

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite one final time
- Verify all success criteria met
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Async tests pass | `pytest tests/test_async.py -v -q` | exit code 0 |
| Connection tests pass | `pytest tests/test_connection.py -v -q` | exit code 0 |
| Bulk tests pass | `pytest tests/test_bulk_operations.py -v -q` | exit code 0 |
| Migration tests pass | `pytest tests/test_migrations.py -v -q` | exit code 0 |
| New test count | `grep -c "async def test_" tests/test_async.py tests/test_connection.py tests/test_bulk_operations.py tests/test_migrations.py` | output > 30 |
| All gaps covered | `grep -c "Gap [0-9]" tests/test_async.py tests/test_connection.py tests/test_bulk_operations.py tests/test_migrations.py` | output > 16 |
