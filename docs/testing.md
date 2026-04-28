# Testing

Popoto includes a pytest plugin that automatically isolates tests in a dedicated Redis DB. It is the recommended way to run a project's test suite against Popoto models without contaminating development or production data.

## Pytest Plugin (auto-registered)

The `popoto.pytest_plugin` module is registered as a [pytest11 entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others) and loads automatically when Popoto is installed. No configuration is required.

**What the plugin does:**

- Switches all Redis operations to DB 15 (or a configured DB) for the test session.
- Runs `flushdb()` before each test for a clean slate.
- Resets the async Redis connection per test to avoid event-loop conflicts.

**Configuration priority** (highest to lowest):

1. `POPOTO_TEST_DB` environment variable
2. `popoto_test_db` ini option in `pyproject.toml` `[tool.pytest.ini_options]`
3. Default: `15`

DB 0 is rejected to prevent accidental test runs against production data. Non-integer values produce a clear error message.

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
