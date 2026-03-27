---
status: In Progress
type: enhancement
appetite: Small
owner: Agent
created: 2026-03-27
tracking: https://github.com/tomcounsell/popoto/issues/299
last_comment_id:
---

# Auto-registering Pytest Plugin for Test DB Isolation

## Problem

Developers writing tests for Popoto-backed applications risk polluting their development database. There is no built-in mechanism to automatically route tests to a separate Redis logical database.

**Current behavior:**

- Tests run against whichever Redis DB the app is configured to use (typically DB 0).
- Popoto's own test suite has 32+ test files each defining their own cleanup fixtures using 3 different ad-hoc patterns: `flushdb()`, selective key deletion, and `Model.delete_all()`.
- The existing `popoto.testing` module provides `use_test_db()` and `flush_test_db()` helpers, but only 1 out of 72 test files uses them.
- Async tests require manual reset of the cached async Redis connection (`_POPOTO_ASYNC_REDIS_DB = None` + new `asyncio.Lock()`) to avoid "Future attached to a different loop" errors -- each async test file re-implements this independently.

**Desired outcome:**

- Zero-config test isolation: installing Popoto and running `pytest` automatically uses a dedicated Redis DB (default: 15) for tests.
- Automatic cleanup: each test starts with a clean database.
- Async connection handling: the async Redis connection is automatically reset per test to work with pytest-asyncio's per-test event loop.
- Configurable: users can override the test DB number via env var (`POPOTO_TEST_DB`) or pytest config (`popoto_test_db` ini option).

## Prior Art

- **`popoto.testing` module** (`src/popoto/testing.py`): Provides `use_test_db(db=15)` and `flush_test_db()` helpers. Only used by `tests/test_dx_polish.py` for import checks. The plugin will build on these primitives.
- **pytest-django**: Auto-registers via entry point, provides `--reuse-db` and `--create-db` flags, session-scoped DB setup. Our plugin follows the same entry-point pattern.
- **pytest-redis**: Provides Redis fixtures but manages its own Redis server processes -- heavier than what Popoto needs.
- **Ad-hoc async reset pattern**: Used in `test_async.py`, `test_connection.py`, `test_sorted_field_ordering.py`, `test_bulk_operations.py`, `test_stress.py`, and `test_stream_consumer.py`. Each sets `redis_db._POPOTO_ASYNC_REDIS_DB = None` and creates a new `asyncio.Lock()`. The plugin will centralize this.

## Data Flow

1. **Plugin registration**: `pyproject.toml` `[project.entry-points.pytest11]` registers `popoto.pytest_plugin` as `popoto`. Pytest auto-discovers it on startup.
2. **`pytest_addoption` hook**: Registers `popoto_test_db` ini option with default `"15"`.
3. **Session-scoped fixture `_popoto_test_db`** (autouse):
   - Reads test DB number from: `POPOTO_TEST_DB` env var > `popoto_test_db` ini option > default `15`.
   - Extracts current connection kwargs (host, port, password) from `POPOTO_REDIS_DB.connection_pool.connection_kwargs`.
   - Calls `set_REDIS_DB_settings()` with preserved auth + new DB number.
   - On teardown: flushes the test DB and restores the original connection.
4. **Function-scoped fixture `_popoto_flush_db`** (autouse):
   - Before each test: calls `POPOTO_REDIS_DB.flushdb()` to ensure clean state.
5. **Function-scoped fixture `_popoto_reset_async`** (autouse):
   - Before each test: sets `redis_db._POPOTO_ASYNC_REDIS_DB = None` and creates a new `asyncio.Lock()`.
   - This prevents "Future attached to a different loop" errors when pytest-asyncio creates a new event loop per test.

## Architectural Impact

- **Interface changes**: None to the public API. The plugin adds pytest fixtures only.
- **Coupling**: The plugin imports from `popoto.redis_db` (existing internal module). No new cross-module dependencies.
- **New dependencies**: None. pytest is already a dev dependency.
- **Data ownership**: No change. The plugin only switches which DB number is used during tests.
- **Reversibility**: Fully reversible. Removing the entry point disables the plugin entirely. Users can also disable it with `pytest -p no:popoto`.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

No prerequisites -- all required infrastructure (`set_REDIS_DB_settings`, `POPOTO_REDIS_DB`, async reset pattern) already exists.

## Solution

### Key Elements

- **New file `src/popoto/pytest_plugin.py`**: Contains `pytest_addoption` hook and three autouse fixtures.
- **Entry point in `pyproject.toml`**: `[project.entry-points.pytest11]` section registering the plugin.
- **Updated `testing.py` docstring**: Points users to the automatic plugin as the preferred approach.
- **New `tests/conftest.py`**: Minimal file that serves as documentation for contributors, showing the plugin is active.

### Technical Approach

1. **Create `src/popoto/pytest_plugin.py`**:

   - `pytest_addoption(parser)`: Register `popoto_test_db` ini option.
   - `_popoto_test_db(request)` fixture (session-scoped, autouse):
     - Read DB number from env var `POPOTO_TEST_DB`, falling back to ini option, falling back to 15.
     - Save current connection kwargs from `POPOTO_REDIS_DB.connection_pool.connection_kwargs`.
     - Call `set_REDIS_DB_settings(**preserved_kwargs, db=test_db_number)`.
     - Yield.
     - On teardown: `POPOTO_REDIS_DB.flushdb()`, then restore original connection via `set_REDIS_DB_settings(**original_kwargs)`.
   - `_popoto_flush_db()` fixture (function-scoped, autouse):
     - Call `POPOTO_REDIS_DB.flushdb()` before each test (setup phase, before yield).
   - `_popoto_reset_async()` fixture (function-scoped, autouse):
     - Import `popoto.redis_db` as module reference.
     - Set `redis_db._POPOTO_ASYNC_REDIS_DB = None`.
     - Set `redis_db._async_redis_lock = asyncio.Lock()`.

2. **Register entry point in `pyproject.toml`**:
   ```toml
   [project.entry-points.pytest11]
   popoto = "popoto.pytest_plugin"
   ```

3. **Update `testing.py` docstring**: Add a note that the pytest plugin handles DB isolation automatically, and that the manual helpers are still available for non-pytest usage.

4. **Create `tests/conftest.py`**: Minimal file with a comment explaining the plugin provides autouse fixtures. May optionally re-export or configure the test DB number for Popoto's own test suite.

5. **Write plugin tests in `tests/test_pytest_plugin.py`**:
   - Verify the plugin module is importable.
   - Verify `pytest_addoption` registers the ini option.
   - Verify the session fixture switches to the correct DB.
   - Verify function-scoped flush clears data between tests.
   - Verify async connection is reset (no stale loop references).
   - Verify `POPOTO_TEST_DB` env var overrides the default.
   - Verify REDIS_URL with auth credentials is preserved.

### Flow

**pytest startup** -> **entry point loads `popoto.pytest_plugin`** -> **`pytest_addoption` registers ini option** -> **session fixture switches to DB 15** -> **per-test: flush DB + reset async connection** -> **tests run in isolation** -> **session teardown: flush + restore original DB**

## Failure Path Test Strategy

### Exception Handling Coverage
- [x] Test plugin behavior when Redis is unreachable (fixture should raise clear error, not hang)
- [x] Test that invalid DB numbers (e.g., negative, > 15) produce clear errors

### Empty/Invalid Input Handling
- [x] Test with `POPOTO_TEST_DB=""` (should fall back to ini option or default)
- [x] Test with `POPOTO_TEST_DB` set to non-numeric value (should raise or warn)

### Error State Rendering
- [x] Not applicable -- no user-visible output changes beyond pytest fixture errors

## Test Impact

No existing tests affected -- the plugin is purely additive. Existing test files that define their own `flushdb()` fixtures will continue to work because:
- The plugin's autouse flush runs before each test (additive with existing per-file fixtures).
- Existing fixtures that call `POPOTO_REDIS_DB.flushdb()` are idempotent (flushing an already-empty DB is a no-op).
- The session-scoped DB switch happens once at the start, so all existing fixtures that import `POPOTO_REDIS_DB` will use the test DB automatically.

One consideration: existing tests that hard-code `set_REDIS_DB_settings(db=0)` or similar may conflict. A grep shows no such patterns in the current test suite.

## Rabbit Holes

- **Per-model DB routing**: Different models on different DBs is a separate feature with much larger scope. Not part of this work.
- **Removing existing per-file cleanup fixtures**: Tempting to clean up the 32+ files with ad-hoc fixtures, but that is a separate cleanup PR. The plugin is additive.
- **Custom pytest markers** (e.g., `@pytest.mark.popoto_db(14)`): Over-engineering for the initial implementation. The single DB number is sufficient.
- **Managing Redis server lifecycle**: Unlike pytest-redis, we assume Redis is already running. Starting/stopping Redis is out of scope.
- **Fixture opt-out mechanism**: pytest's built-in `-p no:popoto` is sufficient. No need for custom skip markers.

## Risks

### Risk 1: Entry point conflicts with user's conftest
**Impact:** If a user already defines fixtures with the same names (`_popoto_test_db`, `_popoto_flush_db`, `_popoto_reset_async`), pytest will error on duplicate fixtures.
**Mitigation:** Use underscore-prefixed names to signal these are internal. The names are specific enough (`_popoto_*`) that collisions are extremely unlikely.

### Risk 2: `connection_pool.connection_kwargs` may not contain all auth info
**Impact:** If REDIS_URL authentication uses a format that stores credentials differently in the connection pool, the plugin might lose auth when switching DBs.
**Mitigation:** Test explicitly with REDIS_URL containing `redis://user:password@host:port/0` format. Extract `host`, `port`, `password`, and `username` from `connection_kwargs`.

### Risk 3: Plugin runs in non-test contexts
**Impact:** None -- pytest entry points only load when pytest is the test runner.
**Mitigation:** The entry point mechanism guarantees this. No runtime code path loads the plugin.

## Race Conditions

No race conditions -- pytest fixtures run synchronously in a single process. The session-scoped fixture runs once before any tests, and function-scoped fixtures run sequentially per test.

## No-Gos (Out of Scope)

- Removing or modifying existing per-file cleanup fixtures in the 32+ test files
- Per-model or per-test-class DB routing
- Redis server lifecycle management (start/stop)
- Supporting test runners other than pytest (e.g., unittest, nose)
- Adding new runtime dependencies

## Update System

No update system changes required -- this is an open-source library (tomcounsell/popoto), not the Valor AI system. The plugin is distributed as part of the popoto package via PyPI.

## Agent Integration

No agent integration required -- this is an open-source library (tomcounsell/popoto), not the Valor AI system. There are no MCP servers, bridges, or agent tools involved.

## Documentation

- [x] Add docstring to `src/popoto/pytest_plugin.py` explaining plugin behavior, configuration options, and how to disable
- [x] Update `src/popoto/testing.py` module docstring to reference the automatic plugin
- [x] Add `tests/conftest.py` with comments explaining plugin-provided fixtures

## Success Criteria

- [x] Running `pytest` with popoto installed automatically uses Redis DB 15 (not DB 0)
- [x] `POPOTO_TEST_DB=14 pytest` overrides the default to DB 14
- [x] `popoto_test_db = 14` in `pyproject.toml` `[tool.pytest.ini_options]` works as override
- [x] Each test starts with an empty database (verified by `DBSIZE` returning 0)
- [x] DB 0 is unaffected after a full test run
- [x] Async tests pass without manually resetting the async connection
- [x] Existing test files with their own cleanup fixtures still pass
- [x] Plugin works with `REDIS_URL` containing authentication credentials
- [x] Plugin can be disabled with `pytest -p no:popoto`

## Team Orchestration

### Team Members

- **Builder (pytest-plugin)**
  - Name: plugin-builder
  - Role: Implement pytest_plugin.py, entry point, tests
  - Agent Type: builder
  - Resume: true

- **Validator (pytest-plugin)**
  - Name: plugin-validator
  - Role: Verify all acceptance criteria, run full test suite
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Create pytest plugin module
- **Task ID**: build-plugin-module
- **Depends On**: none
- **Validates**: src/popoto/pytest_plugin.py (create)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/pytest_plugin.py` with:
  - `pytest_addoption` hook registering `popoto_test_db` ini option (default: `"15"`)
  - Session-scoped autouse fixture `_popoto_test_db` that switches DB and preserves auth
  - Function-scoped autouse fixture `_popoto_flush_db` that flushes before each test
  - Function-scoped autouse fixture `_popoto_reset_async` that resets the async connection
  - Module-level docstring explaining usage and configuration

### 2. Register entry point in pyproject.toml
- **Task ID**: build-entry-point
- **Depends On**: none
- **Validates**: pyproject.toml (modify)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `[project.entry-points.pytest11]` section with `popoto = "popoto.pytest_plugin"`

### 3. Update testing.py docstring
- **Task ID**: build-testing-docs
- **Depends On**: none
- **Validates**: src/popoto/testing.py (modify)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: true
- Add note about the automatic plugin being the preferred approach
- Keep existing `use_test_db()` and `flush_test_db()` functions for non-pytest usage

### 4. Create tests/conftest.py
- **Task ID**: build-conftest
- **Depends On**: build-plugin-module
- **Validates**: tests/conftest.py (create)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: false
- Minimal conftest.py that documents the plugin's autouse fixtures
- May configure `popoto_test_db` ini option for Popoto's own test suite

### 5. Write plugin tests
- **Task ID**: build-plugin-tests
- **Depends On**: build-plugin-module
- **Validates**: tests/test_pytest_plugin.py (create)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: false
- Test that plugin module is importable and has expected hooks
- Test that session fixture switches to correct DB number
- Test that function-scoped flush clears data between tests
- Test that async connection is reset
- Test POPOTO_TEST_DB env var override
- Test that auth credentials are preserved when switching DB
- Test DBSIZE is 0 at start of each test

### 6. Validate all changes
- **Task ID**: validate-all
- **Depends On**: build-plugin-tests, build-entry-point, build-testing-docs, build-conftest
- **Assigned To**: plugin-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify DB 0 is unaffected after test run
- Verify plugin can be disabled with `-p no:popoto`
- Review all acceptance criteria from the issue

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Plugin tests pass | `pytest tests/test_pytest_plugin.py -v` | exit code 0 |
| Plugin loads | `pytest --co -q 2>&1 \| head -5` | no import errors |
| DB 0 unaffected | `redis-cli -n 0 DBSIZE` | same count before and after |
| Plugin disable works | `pytest -p no:popoto --co -q 2>&1 \| head -5` | no plugin fixtures |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| CONCERN | [agent-type] | [The concern raised] | [How/whether it was addressed] |

---

## Open Questions

1. **Should the plugin restore the original DB on teardown?** The session-scoped fixture could simply flush the test DB and leave the connection pointing at it (since the process is exiting anyway). However, restoring the original connection is safer for test runners that do post-test analysis or for users running tests in a REPL. The plan calls for restoring the original connection.

2. **Should the flush happen in setup or teardown?** Flushing in setup (before each test) guarantees a clean slate regardless of how the previous test ended (crash, skip, etc.). Flushing in teardown is cleaner but risks leaving data if a test crashes. The plan uses setup-phase flush (before yield) for maximum robustness.
