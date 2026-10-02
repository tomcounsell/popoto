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
PopotoIsolationWarning: popoto is writing to Redis DB 0 during this pytest session and is NOT isolating
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

## Backend Conformance Tests (opt-in)

The storage backend seam ([#631](https://github.com/tomcounsell/popoto/issues/631))
adds a second, opt-in dimension to the plugin: a test marked `conformance` that
requests the `backend` fixture runs once per configured storage backend, with
`popoto.set_backend()` bound to that backend for the test and the previous
binding restored on teardown. Unmarked tests never see it, and a test that
requests `backend` without the marker gets the Redis backend, unparametrised.
The test-process default is Redis: an opted-in session (`popoto_test_db` /
`POPOTO_TEST_DB`) pins `RedisBackend` before collection regardless of
`POSTGRES_URL`, and the Postgres leg is fixture-scoped, so only a
`conformance` test's `[postgres]` parameter ever binds `PostgresBackend` and it
is unbound again when that test ends.

```python
import pytest

@pytest.mark.conformance
def test_record_roundtrip(backend):
    backend.save_record("Memory:1", {b"text": b"..."}, class_set="$Class:Memory")
    assert backend.load_record("Memory:1") is not None
```

Which backends run is configured the way `popoto_test_db` is, and defaults to
Redis only, so a project that never opts in sees no change:

```ini
# pyproject.toml
[tool.pytest.ini_options]
popoto_conformance_backends = "redis,postgres"
```

```bash
POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres \
    pytest -m conformance
```

The environment variable overrides the ini option. Names are `redis` and
`postgres`; anything else is a configuration error, not a silent Redis-only
run. Install the driver with `pip install 'popoto[postgres]'`.

**The Postgres leg skips, never fails, when it cannot run**: with `psycopg` not
installed or `POSTGRES_URL` unset, each `[postgres]` parameter reports
`SKIPPED` with a reason naming what is missing (`pytest -rs` shows it). Two
conditions are *refused* instead, raising `PostgresIsolationRefusedError`
before any statement reaches the server, mirroring the DB-0 refusal on the
Redis side: a `POSTGRES_URL` that names no database (libpq would resolve it to
the connecting role's default), and any attempt to use a schema other than `popoto_test_` plus exactly 32
lowercase hex characters (so `public` and `popoto_test_prod` are refused).

**Isolation** is one schema per session: the harness runs
`CREATE SCHEMA popoto_test_<32 hex>` at first use, truncates every table in it
before each test (the `FLUSHDB` mirror), and `DROP SCHEMA ... CASCADE`s it at
session end. The schema reaches `PostgresBackend` through the URL it is built
with (`options=-c search_path=<schema>`), so every connection the backend opens
resolves unqualified table names inside it. The Redis side is unchanged: the
plugin still needs Redis bound (`popoto_test_db` / `REDIS_URL`) because its
autouse flush runs before every test, including Postgres-leg tests.

Two helpers for assertions that only hold on one backend: the `redis_only`
marker skips a conformance test on every non-Redis leg, and the
`backend_is_redis` fixture returns `True` on the Redis leg for a test that
branches rather than skips. The `popoto_postgres_schema` session fixture exposes
the schema name and an admin `connect()` for a test that wants to inspect it.

In this repository `.github/workflows/tests.yml` runs `pytest -m conformance`
as the `pytest (Postgres)` job against a `postgres:16` service, with the Redis
service alongside it for the reason above.

**`str` at the backend boundary, and how a model-level test file runs on both
legs.** The protocol types every key, member, index name and class-set argument
`str`, while the query layer still carries the raw `bytes` Redis replies between
`Query.keys()`, `filter_for_keys_set()` and the field mixins (a filter composed
across families intersects those sets directly). Redis never noticed a `bytes`
key reaching a backend call, because a UTF-8 `str` and the same `bytes` are one
wire token; Postgres rejects it (`operator does not exist: text = bytea`). The
rule is therefore: decode at the call with `popoto.backends.as_key_str` /
`as_key_strs`, never inside a backend, and test the unit of work for presence
(`uow is not None`), never truthiness, since a Postgres unit of work is falsy
while empty. `tests/test_backend_str_boundary.py` installs a backend that
refuses anything else and drives every routed entry point through it. A test
file that uses models rather than the backend directly opts into both legs with
a module-level `pytestmark = pytest.mark.conformance` plus an autouse fixture
that requests `backend` (the plugin only parametrises tests whose fixture
closure contains it), and marks the tests that read Redis keys through the raw
client, spy on the Redis client, or exercise a family still stubbed on
Postgres with `redis_only(reason=...)`; `tests/test_indexed_fields.py` and
`tests/test_key_fields.py` are the pattern.

## Manual Test Helpers

The `popoto.testing` module provides helpers for non-pytest test runners or manual use:

```python
from popoto.testing import use_test_db, flush_test_db

use_test_db(db=15)   # Switch to test DB
flush_test_db()      # Clear the test DB
```

These are not needed when using the pytest plugin, which handles both automatically.

`flush_test_db()` resolves the client to flush via `get_REDIS_DB()` **at call
time**, so it always follows whatever database is currently bound — including
a rebind made by `use_test_db()` or `set_REDIS_DB_settings()` after import.
(Previously it captured the client once and could keep flushing the
originally-bound database after a later rebind, so the documented
`use_test_db()` + `flush_test_db()` pairing did not always compose.)
`flush_test_db()` also inherits Popoto's destructive-flush guard: if the
currently bound client resolves to database 0, it raises
`popoto.redis_db.Db0FlushRefusedError` instead of flushing. See
[Environment Variables](configuration.md#environment-variables) for the
`POPOTO_ALLOW_DB0_FLUSH` escape hatch.

## Safe Ad-Hoc/Repro Scripts

Popoto binds its global Redis client from `REDIS_URL` **at import time**.
Setting `REDIS_URL` (or exporting any other environment variable) *after*
`import popoto` has already run does nothing — the connection pool is
already built. With `REDIS_URL` unset, Popoto falls back to database 0,
which on a shared or agent-hosting machine is often a **live** store, not a
scratch area.

The safe pattern for a one-off repro script:

1. Set `REDIS_URL` to a non-zero database **before `import popoto`** — the
   ordering is load-bearing, since the binding happens at import time and
   cannot be changed afterward by mutating the environment.
2. After import, resolve the database the client actually bound to and
   verify it isn't 0 before running anything.
3. Prefer targeted deletes (scan a narrow key prefix, delete just those
   keys) over any blanket `flushdb()`/`flushall()`.

```bash
export REDIS_URL="redis://localhost:6379/15"  # non-zero, before import popoto
python my_repro_script.py
```

```python
import os
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import popoto  # must come after the REDIS_URL setdefault above
```

Copy `scripts/scratch_repro.py` as a starting point — it sets `REDIS_URL`
before `import popoto`, refuses to run if the resolved database is 0, and
demonstrates a targeted scan-and-delete instead of a blanket flush. See
[#577](https://github.com/tomcounsell/popoto/issues/577).
