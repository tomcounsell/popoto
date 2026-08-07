"""Pytest plugin for automatic Popoto test DB isolation.

When popoto is installed and pytest runs, this plugin automatically:

1. Switches the Redis connection to a dedicated test database (default: DB 15)
2. Flushes the test database before each test for a clean slate
3. Resets the async Redis connection per test to avoid event loop conflicts

Configuration:

    The test database number can be set via (in priority order):

    1. Environment variable: ``POPOTO_TEST_DB=14 pytest``
    2. pytest ini option in ``pyproject.toml``::

        [tool.pytest.ini_options]
        popoto_test_db = 14

    3. Default: 15

Disabling:

    To disable the plugin entirely::

        pytest -p no:popoto

Auth Preservation:

    The plugin preserves any host, port, password, and username from the
    current Redis connection when switching databases, so REDIS_URL with
    authentication continues to work.
"""

import asyncio
import logging
import os

import pytest
import redis

from popoto import redis_db

logger = logging.getLogger("POPOTO-PYTEST")


def pytest_configure(config):
    """Prepare module aliasing and switch onto the test DB before collection.

    Both steps must happen before pytest imports any test module, so they live
    in this hook rather than in a fixture.
    """
    _collapse_src_popoto()
    _configure_test_db(config)


def _collapse_src_popoto():
    """Collapse src.popoto onto the canonical popoto module objects.

    When pytest adds the repo root to sys.path, both ``import popoto`` and
    ``import src.popoto`` resolve to the same physical file but create two
    distinct module objects.  The plugin swaps the connection pool on the
    ``popoto`` instance; the ``src.popoto`` instance keeps the DB-0 default.

    This runs before any test-module import.  It registers
    ``src.popoto`` (and every already-loaded ``popoto.*`` submodule) as the
    *same objects* as their ``popoto`` counterparts, so the duplicate instance
    never exists and the DB-15 swap covers everything automatically.

    Tolerant of environments where there is no ``src/`` layout (downstream
    users, sdist installs): if ``src`` is not importable this is a no-op.
    """
    import importlib
    import sys

    # Ensure the canonical submodules we care about are loaded first.
    try:
        import popoto as _popoto  # noqa: F401

        importlib.import_module("popoto.redis_db")
    except ImportError:
        return  # popoto not installed — nothing to collapse

    # Obtain or create the src package object.
    src_pkg = sys.modules.get("src")
    if src_pkg is None:
        try:
            src_pkg = importlib.import_module("src")
        except ImportError:
            return  # No src/ layout — downstream install, skip silently.

    import popoto as _canonical_popoto

    # Bind the attr on the src package so attribute access (src.popoto) works.
    setattr(src_pkg, "popoto", _canonical_popoto)

    # Register src.popoto and every already-loaded popoto.* submodule into
    # sys.modules under the src.* key so import statements work too.
    # Collect first to avoid mutating the dict while iterating.
    aliases = {}
    for name, mod in list(sys.modules.items()):
        if name == "popoto" or name.startswith("popoto."):
            aliases["src." + name] = mod

    for alias_name, mod in aliases.items():
        existing = sys.modules.get(alias_name)
        if existing is not None and existing is not mod:
            # Already loaded as a *different* object — overwrite and log.
            logger.warning(
                "popoto pytest plugin: overwriting sys.modules[%r] "
                "(was %r, now collapsed onto canonical %r)",
                alias_name,
                existing,
                mod,
            )
        sys.modules[alias_name] = mod


def _configure_test_db(config):
    """Swap onto the test DB before pytest imports any test module.

    Test modules that run model code at import time (``Model.create(...)`` at
    module level rather than inside a test function) execute during collection.
    A session-scoped autouse fixture does not run until the first test, which
    is *after* that — so those writes landed in DB 0, the developer's real
    database, and the DB-0 tripwire could not see them either. Doing the swap
    here closes that window for every test module at once. See #522.

    Failures are non-fatal: an unreachable Redis must not break collection.
    The ``_popoto_test_db`` fixture re-asserts the swap, so a miss here is
    recovered before the first test body runs.
    """
    try:
        test_db = _resolve_test_db(config)
    except ValueError:
        raise  # Misconfiguration (e.g. db=0) — fail loudly and early.

    try:
        original_kwargs = dict(
            redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs
        )
        config._popoto_original_db = original_kwargs.get("db", 0)
        _swap_db(test_db)
        logger.debug("Popoto test DB switched to DB %d (pre-collection)", test_db)
    except Exception as e:
        logger.warning(
            "popoto pytest plugin: could not swap to test DB %d before "
            "collection (%s); the session fixture will retry.",
            test_db,
            e,
        )


def _swap_db(target_db, **extra_kwargs):
    """Swap the connection pool on the existing POPOTO_REDIS_DB object.

    This modifies the existing object in-place rather than creating a new one,
    so all modules that imported POPOTO_REDIS_DB at load time continue to use
    the correct connection.
    """
    db_obj = redis_db.POPOTO_REDIS_DB
    current_kwargs = dict(db_obj.connection_pool.connection_kwargs)
    current_kwargs.update(extra_kwargs)
    current_kwargs["db"] = target_db
    # Preserve socket timeouts
    current_kwargs.setdefault("socket_timeout", 5)
    current_kwargs.setdefault("socket_connect_timeout", 5)
    connection_class = db_obj.connection_pool.connection_class
    new_pool = redis.ConnectionPool(connection_class=connection_class, **current_kwargs)
    old_pool = db_obj.connection_pool
    db_obj.connection_pool = new_pool
    old_pool.disconnect()


def pytest_addoption(parser):
    """Register the ``popoto_test_db`` ini option."""
    parser.addini(
        "popoto_test_db",
        "Redis database number to use for tests (default: 15)",
        default="15",
    )


def _resolve_test_db(config):
    """Resolve the test DB number.

    Priority: POPOTO_TEST_DB env var > ini option > default 15.
    """
    env_db = os.environ.get("POPOTO_TEST_DB", "").strip()
    raw_value = env_db if env_db else config.getini("popoto_test_db")
    try:
        test_db = int(raw_value)
    except (ValueError, TypeError):
        raise ValueError(f"popoto_test_db must be an integer, got {raw_value!r}")
    if test_db == 0:
        raise ValueError(
            "popoto_test_db=0 is not allowed — DB 0 is typically production. "
            "Use a non-zero DB (default: 15) or disable the plugin with -p no:popoto."
        )
    return test_db


@pytest.fixture(scope="session", autouse=True)
def _popoto_test_db(request):
    """Yield the test DB number and restore the original connection at teardown.

    The swap itself happens in ``pytest_configure``, not here. A session
    fixture first runs when the *first test executes*, which is after pytest
    has imported every test module during collection — so any module-level
    model code (``Model.create(...)`` at import time) would run against DB 0,
    the developer's real database, before this fixture ever fired. Swapping in
    ``pytest_configure`` puts the connection on the test DB before the first
    import. See #522.
    """
    test_db = _resolve_test_db(request.config)
    original_db = getattr(request.config, "_popoto_original_db", 0)

    # pytest_configure already swapped; re-assert in case a plugin or an
    # earlier import rebound the global.
    if redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db") != test_db:
        _swap_db(test_db)

    yield test_db

    # Teardown: flush test DB and restore original connection
    try:
        redis_db.POPOTO_REDIS_DB.flushdb()
    except Exception:
        pass

    _swap_db(original_db)
    logger.debug("Popoto connection restored to original DB")


@pytest.fixture(autouse=True)
def _popoto_flush_db(_popoto_test_db):
    """Flush the test database before each test for a clean slate.

    Also re-asserts the test DB if a prior test drifted the connection off it.
    Tests that call ``set_REDIS_DB_settings()`` (or otherwise rebind
    ``redis_db.POPOTO_REDIS_DB``) replace the global with a connection that
    defaults to DB 0 and may not restore it. Without this guard, every
    subsequent test — and the lazily-rebuilt async client, which mirrors the
    sync DB — would silently run against DB 0, bypassing isolation. Re-swapping
    only when the DB has drifted keeps the common path a cheap no-op.
    """
    current_db = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db")
    if current_db != _popoto_test_db:
        _swap_db(_popoto_test_db)
    redis_db.POPOTO_REDIS_DB.flushdb()
    yield


@pytest.fixture(autouse=True)
def _popoto_reset_async():
    """Reset the async Redis connection before and after each test.

    pytest-asyncio creates a fresh event loop per test function, so an async
    client created in one test's loop is bound to a now-closed loop in the
    next test ("Future attached to a different loop"). Clearing the cached
    global forces ``get_async_redis_db()`` to lazily rebuild the client
    *inside* the running test's own loop on first use.

    Crucially, the client is NOT pre-created here. This fixture runs in a
    synchronous setup context, outside the test's event loop; a client built
    now would be bound to the wrong loop. ``get_async_redis_db()`` already
    mirrors the swapped sync DB (see redis_db.py), so lazy in-loop creation
    lands on the test DB without any pre-configuration in this fixture.
    """
    redis_db._POPOTO_ASYNC_REDIS_DB = None
    redis_db._async_redis_lock = asyncio.Lock()
    yield
    redis_db._POPOTO_ASYNC_REDIS_DB = None


@pytest.fixture(scope="session", autouse=True)
def _popoto_db0_tripwire(request):
    """CI/clean-DB-0-only tripwire: assert DB 0 stays empty during the session.

    Runs only when DB 0 is verifiably idle at session start (dbsize == 0).
    On a non-idle DB 0 (developer box with real app data, or an upstream
    ``REDIS_URL`` pointing at DB 0) this fixture SKIPs loudly — it never
    causes a failure in that scenario.

    Uses only Redis-core commands (DBSIZE) — compatible with both Redis and
    Valkey.  The real fix-proof is the regression test
    (``test_src_popoto_writes_to_test_db``), which needs no idle DB 0.
    """
    import redis as _redis

    # Connect to DB 0 using the same host/port as the test connection,
    # but always on database 0.  ``before is None`` means the tripwire is
    # disabled (couldn't connect, or DB 0 was non-idle at session start).
    db0_client = None
    before = None
    try:
        # Whitelist the connection params: a pool's connection_kwargs carries
        # redis-py 8 pool-internal keys (himport_registry, maint_notifications_*)
        # that redis.Redis(**kwargs) rejects, which would silently disable the
        # tripwire (this branch is except-guarded) on redis-py 8.
        pool_kwargs = redis_db.sibling_client_kwargs(
            redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0
        )
        db0_client = _redis.Redis(**pool_kwargs)
        before = db0_client.dbsize()
    except Exception:
        # Can't connect to DB 0 — disable the tripwire (never a failure).
        before = None

    if before not in (0, None):
        logger.warning(
            "popoto pytest plugin: DB 0 is non-idle (%d keys) at session "
            "start — DB-0 tripwire SKIPPED.  Run against a clean Redis "
            "instance to enable the tripwire.",
            before,
        )

    # Always yield exactly once (a session yield-fixture that returns before
    # yielding errors under pytest-asyncio auto mode), and always release the
    # DB-0 client so the tripwire never leaks a connection.
    try:
        yield
    finally:
        after = None
        if before == 0 and db0_client is not None:
            try:
                after = db0_client.dbsize()
            except Exception:
                after = None
        if db0_client is not None:
            try:
                db0_client.close()
            except Exception:
                pass
        if before == 0 and after:
            pytest.fail(
                f"DB isolation may be bypassed — test writes leaked into DB 0 "
                f"({after} keys found, 0 expected).  DB 0 was empty at session "
                f"start.",
            )
