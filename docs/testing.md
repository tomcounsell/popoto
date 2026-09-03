# Testing

Popoto includes a pytest plugin that automatically isolates tests in a dedicated Redis DB. It is the recommended way to run a project's test suite against Popoto models without contaminating development or production data.

## Pytest Plugin (opt-in)

The `popoto.pytest_plugin` module is registered as a [pytest11 entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others), so pytest loads it wherever Popoto is installed. It stays inert until you name a test database: set `popoto_test_db` in your pytest ini options or export `POPOTO_TEST_DB`. A project that merely depends on Popoto and never opts in keeps every database untouched, including DB 15.

```ini
# pyproject.toml
[tool.pytest.ini_options]
popoto_test_db = "15"
```

**What the plugin does:**

- Switches all Redis operations to DB 15 (or a configured DB) for the test session. The
  switch happens in `pytest_configure`, *before* pytest imports any test module, so test
  files that touch models at module level (rather than inside a test function) are still
  covered. A session-scoped fixture would not run until the first test — after collection —
  leaving those import-time writes to land in DB 0.
- Runs `flushdb()` before each test for a clean slate.
- Resets the async Redis connection per test to avoid event-loop conflicts.
- Collapses `src.popoto` (and all `popoto.*` submodules) onto the canonical `popoto` objects in `sys.modules` so that tests using either `import popoto` or `import src.popoto` share the same DB-15 connection (no DB-0 leaks from `src/`-layout imports).
- Enforces a DB-0 tripwire: aborts the session if the test DB resolves to DB 0, preventing silent writes to production data.

**Configuration priority** (highest to lowest):

1. `POPOTO_TEST_DB` environment variable
2. `popoto_test_db` ini option in `pyproject.toml` `[tool.pytest.ini_options]`

With neither set the plugin does nothing — but it warns.

DB 0 is rejected to prevent accidental test runs against production data. Non-integer values produce a clear error message.

**Isolation warning when not opted in:** a session that neither sets `popoto_test_db` nor
`POPOTO_TEST_DB` gets exactly one `PopotoIsolationWarning` the first time popoto touches
Redis — naming the DB it just wrote to and both opt-in mechanisms. Merely importing popoto
or defining a `Model` subclass without ever performing a Redis operation stays silent (a
transitive dependency on popoto must not produce noise). The warning is advisory only: it
never affects the outcome of the Redis operation it fires alongside, even under a downstream
suite's `filterwarnings = error`.

```
UserWarning: popoto is writing to Redis DB 0 during this pytest session and is NOT isolating
or flushing it (the popoto pytest plugin is installed but not opted in). Set
popoto_test_db = "15" under [tool.pytest.ini_options] or export POPOTO_TEST_DB to isolate,
or pass -p no:popoto to silence this warning.
```

Known limits: the warning does not cover an async-only suite (there is no async connection
pool in existence at `pytest_configure` time to arm it on — `get_async_redis_db()` builds its
client lazily inside the running event loop), and it does not survive a manual `_swap_db()` /
`set_REDIS_DB_settings()` pool rebind after arming. Both are acceptable — this is an advisory
signal, never a correctness dependency. `pytest -p no:popoto` silences it along with the rest
of the plugin.

```ini
# pyproject.toml
[tool.pytest.ini_options]
popoto_test_db = "14"
```

**Disabling the plugin:**

```bash
pytest -p no:popoto
```

## Manual Test Helpers

The `popoto.testing` module provides helpers for non-pytest test runners or manual use:

```python
from popoto.testing import use_test_db, flush_test_db

use_test_db(db=15)   # Switch to test DB
flush_test_db()      # Clear the test DB
```

These are not needed when using the pytest plugin, which handles both automatically.
