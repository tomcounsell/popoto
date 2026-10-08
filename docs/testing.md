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

Popoto v2 adds a Postgres backend behind the same model API
([#759](https://github.com/tomcounsell/popoto/issues/759),
[Postgres Backend](features/postgres-backend.md)). The plugin carries the
harness that runs one test against each backend: each leg binds its backend
for the test, so the same test code exercises Redis on the `[redis]` leg and
Postgres on the `[postgres]` leg.

In a session that opted in (below), a test marked `conformance` that requests
the `backend` fixture runs once per configured backend, with ids `[redis]` and
`[postgres]`. Unmarked tests are never parametrised. A test that requests
`backend` without the marker gets the Redis leg, once.

```python
import pytest

@pytest.mark.conformance
def test_save_and_load(backend):
    Memory.create(key="m1", text="...")
    assert Memory.query.get(key="m1") is not None
```

Which backends run is configured the way `popoto_test_db` is, and defaults to
Redis only:

```ini
# pyproject.toml
[tool.pytest.ini_options]
popoto_conformance_backends = "redis,postgres"
```

```bash
POPOTO_CONFORMANCE_BACKENDS=redis,postgres POSTGRES_URL=postgresql://localhost:5432/postgres \
    pytest -m conformance
```

The environment variable overrides the ini option. The valid names are `redis`
and `postgres`. Any other name fails the session at startup, so a typo cannot
produce a Redis-only run that looks like Postgres coverage.

**`POSTGRES_URL` is the harness's variable, not the library's.** The harness
reads `POSTGRES_URL` to find a server for its throwaway `popoto_test_<hex>`
schema, and hands the leg's `PostgresBackend` that DSN explicitly. The library
itself never reads `POSTGRES_URL` or `DATABASE_URL`: outside tests the DSN
comes only from `POPOTO_POSTGRES_URL` or an explicit `PostgresBackend(dsn=...)`
(see [Postgres Backend](features/postgres-backend.md#selecting-the-backend)).

`[PG-only]` tests (capabilities with no Redis counterpart, such as the outage
contract and the schema record) live in `tests/postgres/` and run with plain
`pytest tests/postgres`; the ones that need a server take this repository's
`pg` fixture (defined in `tests/postgres/conftest.py`, not shipped by the
plugin), which binds the session schema's backend and skips when
`POSTGRES_URL` is unset.

**Projects that do not opt in see no change.** A session opts in by setting
`popoto_test_db` / `POPOTO_TEST_DB` or `popoto_conformance_backends` /
`POPOTO_CONFORMANCE_BACKENDS`. Without one of those, the plugin registers
neither marker and defines none of the `backend`, `backend_is_redis` and
`popoto_postgres_schema` fixtures. A test requesting `backend` gets pytest's
usual `fixture 'backend' not found`, and the project collects exactly the same
test ids as with `-p no:popoto`. That holds even when the project uses its own
`conformance` and `redis_only` markers alongside its own `backend` fixture
(parametrised or not) or `@pytest.mark.parametrize("backend", ...)`.

Opting in does not take over a project's own `backend` either. Parametrisation
and the `redis_only` reason rule below apply only where the `backend` fixture
that resolves for the test is the plugin's. A `backend` fixture the project
defines, in a conftest, module or class, shadows the plugin's, as any closer
fixture does in pytest. A direct `parametrize("backend", ...)` replaces it. In
both cases the test is collected as if the plugin were absent.

**What `backend` is.** The fixture yields a `ConformanceBackend` descriptor
with `name` (`"redis"` or `"postgres"`) and `is_redis`, plus `schema`, `dsn`
and `instance` (the bound `PostgresBackend`) on the Postgres leg. It also
**binds** that leg's backend as the process default for the test, and as the
instance `Meta.backend = "postgres"` resolves to, restoring the previous
binding on teardown (since #759 M1b). Module-level models therefore run on
the active leg with no re-declaration. Since #816 the first binding implies the
second: a Postgres-named `set_backend` instance serves `Meta.backend =
"postgres"` models on its own. The fixture still also installs it with the
private `_swap_instance("postgres", ...)`, a harmless redundancy (both name the
same instance) kept until after the 1.10.0 release; tests that pair the two
calls need no change. A plugin autouse fixture resolves
`backend` before a test module's own autouse fixtures, so a module fixture
that seeds rows already writes to the leg's backend.

To run a whole existing test module on both legs, mark it at module level;
the tests need not request `backend` themselves:

```python
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]
```

M0's `conformance(harness=True)` flag and its "Postgres backend arrives in
M1" skip are gone: every leg has a backend to run against.

**The Postgres leg skips when it cannot run.** If `psycopg` is not installed or
`POSTGRES_URL` is unset, each `[postgres]` parameter reports `SKIPPED` with a
reason naming what is missing (`pytest -rs` shows it). `psycopg` is imported
only on that leg, so the plugin never needs it to load. Two conditions are
*refused* instead. Each raises `PostgresIsolationRefusedError` before any
connection is opened, mirroring the DB-0 refusal on the Redis side. The URL is
checked before `psycopg` is imported, so a db-less URL is refused even where the
driver is not installed:

- a `POSTGRES_URL` that names no database, which libpq would resolve to the
  connecting role's default;
- any schema other than `popoto_test_` plus exactly 32 lowercase hex characters.
  This refuses `public`, `popoto` and `popoto_test_prod`.

**Isolation** is one schema per session. The harness runs
`CREATE SCHEMA popoto_test_<32 hex>` at first use and drops every table in it
before each Postgres-leg test, as the Postgres counterpart of `FLUSHDB`
(Redis has no schema to survive a flush, and test modules declare same-named
models with different fields, so a table must not outlive its test). At
session end it runs `DROP SCHEMA ... CASCADE`, even when tests failed. The
per-test reset names every table in one `DROP TABLE` without `CASCADE`. A
foreign key reaching into the schema from outside makes it fail rather than
drop a table the harness does not own. (`truncate_all()` remains available
for a test that wants to empty tables but keep them.) The session-end `DROP SCHEMA ... CASCADE` is different. It follows
dependencies across schemas, so it removes an object elsewhere *only if that
object depends on a test table*. On PostgreSQL 18.6, a view in another schema
that selects from a test table is dropped. A foreign key in another schema that
references a test table loses that constraint, while its table and rows remain.
Objects that do not depend on a test table are untouched. The descriptor's
`dsn` is `POSTGRES_URL` with
`options=-c search_path=<schema>` appended, so a connection opened from it
resolves unqualified names inside the schema. In a session that sets
`popoto_test_db` / `POPOTO_TEST_DB`, Redis must still be reachable, because the
plugin's autouse flush runs before every test, including Postgres-leg tests. A
session that opts in only through `popoto_conformance_backends` does not flush
Redis.

**Assertions that only hold on Redis.** Mark them
`@pytest.mark.redis_only(reason="...")`, which skips every non-Redis leg. The
`reason=` is required. A `redis_only` mark without one fails collection on any
test that uses the plugin's `backend` fixture, which is the only thing that
reads the mark, so every mark can be audited later. For a test that must branch
rather than skip,
the `backend_is_redis` fixture returns `True` on the Redis leg. The
`popoto_postgres_schema` session fixture exposes the schema name and an admin
`connect()` for a test that wants to inspect it.

In this repository the harness's own tests live in
`tests/conformance/test_harness.py`.

## On Postgres

Testing Postgres models goes through the conformance harness above. In short:

- Set `popoto_conformance_backends = "redis,postgres"` and point `POSTGRES_URL`
  (the harness's variable, with a database name) at a server. Mark tests
  `conformance` to run them on both legs, and `redis_only(reason=...)` for
  assertions that hold only on Redis.
- Each session gets a throwaway `popoto_test_<hex>` schema. Tables are dropped
  before each Postgres-leg test and the schema at session end.
- In a session that sets `popoto_test_db`, the plugin pins the process default
  backend to Redis before collection, so an unmarked test and a module-level
  `save()` run on Redis even when `POPOTO_BACKEND=postgres`. Only the `backend`
  fixture moves a test onto Postgres.
- A model that declares `Meta.backend = "postgres"` runs on the harness's schema
  only inside the `backend` fixture's Postgres leg. Anywhere else it resolves as
  in production code: a Postgres instance passed to `set_backend()`, else the
  library's own `POPOTO_POSTGRES_URL`. Leave
  `POPOTO_POSTGRES_URL` unset in test environments so such a test fails with
  `BackendUnavailableError` instead of writing to a real database.
- `use_test_db()` and `flush_test_db()` below act on Redis only.

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
