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
    new_pool = redis.ConnectionPool(**current_kwargs)
    db_obj.connection_pool = new_pool


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
    if env_db:
        test_db = int(env_db)
    else:
        ini_val = request.config.getini("popoto_test_db")
        test_db = int(ini_val)

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
def _popoto_flush_db():
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
    # Clean up: set to None so next test gets a fresh connection
    redis_db._POPOTO_ASYNC_REDIS_DB = None
