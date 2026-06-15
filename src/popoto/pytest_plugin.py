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
    """Collapse src.popoto onto the canonical popoto module objects.

    When pytest adds the repo root to sys.path, both ``import popoto`` and
    ``import src.popoto`` resolve to the same physical file but create two
    distinct module objects.  The plugin swaps the connection pool on the
    ``popoto`` instance; the ``src.popoto`` instance keeps the DB-0 default.

    This hook runs before any test-module import.  It registers
    ``src.popoto`` (and every already-loaded ``popoto.*`` submodule) as the
    *same objects* as their ``popoto`` counterparts, so the duplicate instance
    never exists and the DB-15 swap covers everything automatically.

    Tolerant of environments where there is no ``src/`` layout (downstream
    users, sdist installs): if ``src`` is not importable the hook is a no-op.
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


@pytest.fixture(scope="session", autouse=True)
def _popoto_test_db(request):
    """Switch Popoto to a dedicated test database for the entire test session.

    Priority for DB number: POPOTO_TEST_DB env var > ini option > default 15.

    On teardown, flushes the test DB and restores the original connection.
    """
    # Determine test DB number
    env_db = os.environ.get("POPOTO_TEST_DB", "").strip()
    raw_value = env_db if env_db else request.config.getini("popoto_test_db")
    try:
        test_db = int(raw_value)
    except (ValueError, TypeError):
        raise ValueError(f"popoto_test_db must be an integer, got {raw_value!r}")
    if test_db == 0:
        raise ValueError(
            "popoto_test_db=0 is not allowed — DB 0 is typically production. "
            "Use a non-zero DB (default: 15) or disable the plugin with -p no:popoto."
        )

    # Save original connection kwargs for restoration
    original_kwargs = dict(redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs)
    original_db = original_kwargs.get("db", 0)

    # Switch to test DB by swapping the connection pool in-place
    _swap_db(test_db)
    logger.debug("Popoto test DB switched to DB %d", test_db)

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
    """Flush the test database before each test for a clean slate."""
    redis_db.POPOTO_REDIS_DB.flushdb()
    yield


@pytest.fixture(autouse=True)
def _popoto_reset_async():
    """Reset the async Redis connection before each test.

    This prevents 'Future attached to a different loop' errors when
    pytest-asyncio creates a new event loop per test function.

    The new async connection is pre-configured to use the same DB as the
    sync connection, since get_async_redis_db() would otherwise default
    to DB 0 or REDIS_URL (ignoring the plugin's DB switch).
    """
    import redis.asyncio as aioredis

    # Read current sync connection settings to mirror them for async
    pool_kwargs = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs
    async_kwargs = {}
    for key in ("host", "port", "password", "username", "db"):
        if key in pool_kwargs:
            async_kwargs[key] = pool_kwargs[key]
    async_kwargs.setdefault("socket_timeout", 5)
    async_kwargs.setdefault("socket_connect_timeout", 5)

    # Set a pre-configured async connection so get_async_redis_db() returns it
    redis_db._POPOTO_ASYNC_REDIS_DB = aioredis.Redis(**async_kwargs)
    redis_db._async_redis_lock = asyncio.Lock()
    yield
    # Clean up: close the async client's connection pool and set to None
    try:
        if redis_db._POPOTO_ASYNC_REDIS_DB is not None:
            pool = redis_db._POPOTO_ASYNC_REDIS_DB.connection_pool
            # disconnect() is a coroutine on async pools; run it synchronously
            try:
                loop = asyncio.get_running_loop()
                # Loop is running (e.g. async test); schedule cleanup
                loop.create_task(pool.disconnect())
            except RuntimeError:
                # No running loop; safe to use asyncio.run()
                asyncio.run(pool.disconnect())
    except Exception:
        pass
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
    # but always on database 0.
    try:
        pool_kwargs = redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs.copy()
        pool_kwargs["db"] = 0
        db0_client = _redis.Redis(**pool_kwargs)
        before = db0_client.dbsize()
    except Exception:
        # Can't connect to DB 0 — skip silently (not a failure).
        return

    if before != 0:
        logger.warning(
            "popoto pytest plugin: DB 0 is non-idle (%d keys) at session "
            "start — DB-0 tripwire SKIPPED.  Run against a clean Redis "
            "instance to enable the tripwire.",
            before,
        )
        return

    yield

    try:
        after = db0_client.dbsize()
    except Exception:
        return

    if after != 0:
        pytest.fail(
            f"DB isolation may be bypassed — test writes leaked into DB 0 "
            f"({after} keys found, 0 expected).  DB 0 was empty at session "
            f"start.",
        )
