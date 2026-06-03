"""
Redis/Valkey connection management for Popoto.

Connects to Redis or Valkey using the ``REDIS_URL`` environment variable, or falls
back to ``localhost:6379``. Use :func:`set_REDIS_DB_settings` to reconfigure the
connection at runtime.

Valkey Compatibility:
    Popoto fully supports Valkey, the open-source Redis fork. The redis-py client
    library works with both Redis and Valkey servers, so no code changes are needed.
    Simply point ``REDIS_URL`` at your Valkey server.

This module serves as the central point for Redis/Valkey connectivity throughout
Popoto. Rather than requiring each model, field, or query to manage its own
connection, this module provides a single global connection instance (POPOTO_REDIS_DB)
that all components share.

Async Support:
    Popoto provides native async Redis support via redis.asyncio. Use
    ``POPOTO_ASYNC_REDIS_DB`` for true non-blocking async I/O operations.

    The async connection is created lazily on first use via ``get_async_redis_db()``
    to avoid event loop issues at import time.

    Example::

        from popoto.redis_db import get_async_redis_db

        async def example():
            async_redis = await get_async_redis_db()
            await async_redis.hset(key, mapping=data)

Design Philosophy:
    Popoto follows a "configure once, use everywhere" pattern for database connections.
    The connection is established at module import time using environment variables,
    allowing applications to configure Redis/Valkey without modifying code. This mirrors
    Django's database configuration approach, making Popoto feel familiar to Django
    developers.

Configuration:
    - REDIS_URL: Full Redis/Valkey URL (e.g., "redis://user:pass@host:port/db")
    - Falls back to localhost:6379 if REDIS_URL is not set
    - Works with both Redis and Valkey servers

    Optional:
    - BEGINNING_OF_TIME: Unix timestamp (seconds) used as a floor for time-series
      queries. Defaults to 0 (1970-01-01). Useful for filtering out invalid dates.

Usage:
    Most Popoto code imports POPOTO_REDIS_DB directly for performance::

        from popoto.redis_db import POPOTO_REDIS_DB
        POPOTO_REDIS_DB.hset(key, mapping=data)

    For async operations, use get_async_redis_db()::

        from popoto.redis_db import get_async_redis_db

        async def my_async_function():
            redis = await get_async_redis_db()
            await redis.hset(key, mapping=data)

    For dynamic reconfiguration (e.g., testing), use set_REDIS_DB_settings()::

        from popoto.redis_db import set_REDIS_DB_settings
        set_REDIS_DB_settings(host='test-redis', port=6380)

Note:
    Commented-out REDIS_GRAPH references indicate planned RedisGraph support
    for relationship traversal queries, which is not yet implemented.
"""

import os
import logging
import asyncio
import redis
import redis.asyncio as aioredis

# from redisgraph import Graph

logger = logging.getLogger("POPOTO-REDIS_DB")

# Configure logging level from environment
_log_level = os.environ.get("POPOTO_LOG_LEVEL", "WARNING").upper()
_valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
if _log_level in _valid_levels:
    logging.getLogger("POPOTO-REDIS_DB").setLevel(getattr(logging, _log_level))


global POPOTO_REDIS_DB
global _POPOTO_ASYNC_REDIS_DB
# global REDIS_GRAPH
BEGINNING_OF_TIME = 0
ENCODING = "utf-8"

# Async connection is created lazily to avoid event loop issues at import time
_POPOTO_ASYNC_REDIS_DB = None
_async_redis_lock = asyncio.Lock()

# Upper bound on simultaneously-open async connections. redis.asyncio defaults
# to an effectively unbounded pool (max_connections == 2**31), so a burst of
# concurrent coroutines (e.g. asyncio.gather over hundreds of async_save calls)
# opens one socket per in-flight op and can exceed the server's ``maxclients``,
# surfacing as ``redis.exceptions.MaxConnectionsError: Too many connections``.
# Using a BlockingConnectionPool with this cap makes excess coroutines wait for
# a free connection instead of erroring. Override via POPOTO_ASYNC_MAX_CONNECTIONS.
try:
    _ASYNC_MAX_CONNECTIONS = int(
        os.environ.get("POPOTO_ASYNC_MAX_CONNECTIONS", "128")
    )
except ValueError:
    _ASYNC_MAX_CONNECTIONS = 128

# Upper bound on simultaneously-open sync connections. async_save/async_delete
# run the synchronous save()/delete() in a thread pool (see Model.async_save),
# so a burst of concurrent async coroutines funnels into the SYNC pool — one
# checked-out connection per in-flight thread. With the redis-py default
# (effectively unbounded) pool this opens a socket per op and can overrun the
# server's ``maxclients`` (surfacing as MaxConnectionsError / "Too many
# connections"). A BlockingConnectionPool makes excess threads wait for a free
# connection instead. Override via POPOTO_SYNC_MAX_CONNECTIONS.
try:
    _SYNC_MAX_CONNECTIONS = int(
        os.environ.get("POPOTO_SYNC_MAX_CONNECTIONS", "128")
    )
except ValueError:
    _SYNC_MAX_CONNECTIONS = 128

try:
    BEGINNING_OF_TIME = int(os.environ.get("BEGINNING_OF_TIME", 0))
except ValueError:
    logger.critical(
        "BEGINNING_OF_TIME is set but should be an integer in unix time seconds where 0 equals 1970-01-01"
    )
except Exception as e:
    logger.debug(e)

try:
    REDIS_URL = os.environ.get("REDIS_URL", "")
    if REDIS_URL:
        pool = redis.BlockingConnectionPool.from_url(
            REDIS_URL,
            socket_timeout=5,
            socket_connect_timeout=5,
            max_connections=_SYNC_MAX_CONNECTIONS,
        )
        POPOTO_REDIS_DB = redis.Redis(connection_pool=pool)
        logger.debug("Redis connection established.")
    else:
        REDIS_HOST, REDIS_PORT = "127.0.0.1:6379".split(":")
        pool = redis.BlockingConnectionPool(
            host=REDIS_HOST,
            port=int(REDIS_PORT),
            db=0,
            socket_timeout=5,
            socket_connect_timeout=5,
            max_connections=_SYNC_MAX_CONNECTIONS,
        )
        POPOTO_REDIS_DB = redis.Redis(connection_pool=pool)
        # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)

except Exception as e:
    logger.error(f"Redis connection failed: {e}")
    raise


def set_REDIS_DB_settings(env_partition_name: str = "", *args, **kwargs) -> None:
    """Reset the global Redis connection with new settings.

    This function enables dynamic connection switching, which is essential for:
    - Test isolation: Point tests at a separate Redis instance or database
    - Multi-tenant applications: Switch connections based on request context
    - Failover scenarios: Redirect to a backup Redis instance

    Args:
        env_partition_name: Optional namespace prefix for key isolation.
            Falls back to the ``ENV`` environment variable.
        *args, **kwargs: Passed directly to ``redis.Redis()``.
            Common kwargs: host, port, db, password, socket_timeout.

    Example:
        # For testing with a dedicated test database
        set_REDIS_DB_settings(host='localhost', port=6379, db=15)

        # With authentication
        set_REDIS_DB_settings(host='redis.prod', password='secret')
    """
    # todo: use this to mark keys in redis db, so they can be separated and deleted
    env_partition_name = env_partition_name or os.environ.get("ENV", "")

    # Apply default socket timeouts if not provided
    kwargs.setdefault("socket_timeout", 5)
    kwargs.setdefault("socket_connect_timeout", 5)

    global POPOTO_REDIS_DB
    POPOTO_REDIS_DB = redis.Redis(*args, **kwargs)
    # global REDIS_GRAPH
    # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)
    logger.debug("Redis connection reset.")


def get_REDIS_DB():
    """Return the current global Redis connection instance.

    Provides function-based access to the connection for cases where importing
    the global directly is problematic (e.g., circular imports, lazy evaluation).
    Most internal Popoto code imports POPOTO_REDIS_DB directly for performance,
    but external code may prefer this accessor for cleaner dependency injection
    and easier mocking in tests.

    Returns:
        redis.Redis: The configured Redis client instance.
    """
    return POPOTO_REDIS_DB


async def get_async_redis_db():
    """Return the global async Redis connection, creating it lazily if needed.

    This function provides access to an async Redis client for use in async/await
    contexts. The connection is created lazily on first call to avoid event loop
    issues at module import time.

    The async client uses the same connection parameters as the sync client
    (via REDIS_URL or localhost:6379 default).

    Returns:
        redis.asyncio.Redis: The configured async Redis client instance.

    Example:
        async def save_data():
            redis = await get_async_redis_db()
            await redis.hset("key", mapping={"field": "value"})

    Thread Safety:
        Uses an asyncio lock to ensure only one async connection is created
        even if called concurrently from multiple coroutines.

    Connection Cleanup:
        The connection pool is managed automatically by ``redis.asyncio``.
        No explicit cleanup (e.g., calling ``close()``) is needed in normal
        usage -- the pool is cleaned up when the process exits. If you need
        to reconfigure the connection at runtime, use
        ``set_async_redis_db_settings()`` which handles closing the old
        connection before creating a new one.
    """
    global _POPOTO_ASYNC_REDIS_DB

    if _POPOTO_ASYNC_REDIS_DB is not None:
        return _POPOTO_ASYNC_REDIS_DB

    async with _async_redis_lock:
        # Double-check after acquiring lock
        if _POPOTO_ASYNC_REDIS_DB is not None:
            return _POPOTO_ASYNC_REDIS_DB

        REDIS_URL = os.environ.get("REDIS_URL", "")
        if REDIS_URL:
            pool = aioredis.BlockingConnectionPool.from_url(
                REDIS_URL,
                socket_timeout=5,
                socket_connect_timeout=5,
                max_connections=_ASYNC_MAX_CONNECTIONS,
            )
            _POPOTO_ASYNC_REDIS_DB = aioredis.Redis(connection_pool=pool)
        else:
            pool = aioredis.BlockingConnectionPool(
                host="127.0.0.1",
                port=6379,
                db=0,
                socket_timeout=5,
                socket_connect_timeout=5,
                max_connections=_ASYNC_MAX_CONNECTIONS,
            )
            _POPOTO_ASYNC_REDIS_DB = aioredis.Redis(connection_pool=pool)
        logger.debug("Async Redis connection established.")
        return _POPOTO_ASYNC_REDIS_DB


async def set_async_redis_db_settings(
    env_partition_name: str = "", *args, **kwargs
) -> None:
    """Reset the global async Redis connection with new settings.

    This is the async equivalent of set_REDIS_DB_settings(). Use it to
    reconfigure the async connection for testing or multi-tenant scenarios.

    Args:
        env_partition_name: Optional namespace prefix for key isolation.
        *args, **kwargs: Passed directly to ``redis.asyncio.Redis()``.

    Example:
        await set_async_redis_db_settings(host='localhost', port=6379, db=15)
    """
    global _POPOTO_ASYNC_REDIS_DB

    kwargs.setdefault("socket_timeout", 5)
    kwargs.setdefault("socket_connect_timeout", 5)

    async with _async_redis_lock:
        if _POPOTO_ASYNC_REDIS_DB is not None:
            await _POPOTO_ASYNC_REDIS_DB.close()
        _POPOTO_ASYNC_REDIS_DB = aioredis.Redis(*args, **kwargs)
    logger.debug("Async Redis connection reset.")


def check_connection() -> bool:
    """
    Check if the Redis connection is healthy.

    Returns:
        True if Redis is reachable and responding, False otherwise.

    Example:
        >>> from popoto.redis_db import check_connection
        >>> if check_connection():
        ...     print("Redis is healthy")
    """
    try:
        POPOTO_REDIS_DB.ping()
        return True
    except Exception:
        return False


async def async_check_connection() -> bool:
    """
    Async version of check_connection().

    Returns:
        True if Redis is reachable and responding, False otherwise.

    Example:
        >>> from popoto.redis_db import async_check_connection
        >>> if await async_check_connection():
        ...     print("Redis is healthy")
    """
    try:
        redis = await get_async_redis_db()
        await redis.ping()
        return True
    except Exception:
        return False


def scan_keys(pattern: str, count: int = 1000) -> list:
    """Non-blocking replacement for KEYS using cursor-based SCAN.

    The Redis KEYS command blocks the server while scanning the entire keyspace,
    which can cause multi-second delays at scale (100K+ keys). SCAN iterates
    incrementally using a cursor, allowing other operations to interleave.

    Performance is similar to KEYS at small scale, but SCAN avoids blocking
    the Redis server, making it safe for production use.

    Args:
        pattern: Glob-style pattern to match keys (e.g., "User:*", "*:active").
        count: Hint for how many keys to return per iteration. Redis may return
            more or fewer. Higher values reduce round-trips but increase per-call
            latency. Default 1000 balances throughput and responsiveness.

    Returns:
        list: All keys matching the pattern. Unlike KEYS, results are collected
            across multiple SCAN iterations before returning.

    Example:
        # Find all User model keys
        user_keys = scan_keys("User:*")

        # Find keys ending with a pattern
        active_keys = scan_keys("*:active")
    """
    results = []
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(cursor=cursor, match=pattern, count=count)
        results.extend(keys)
        if cursor == 0:
            break
    return results


async def async_scan_keys(pattern: str, count: int = 1000) -> list:
    """Async version of scan_keys() using cursor-based SCAN.

    Args:
        pattern: Glob-style pattern to match keys (e.g., "User:*", "*:active").
        count: Hint for how many keys to return per iteration.

    Returns:
        list: All keys matching the pattern.

    Example:
        user_keys = await async_scan_keys("User:*")
    """
    redis = await get_async_redis_db()
    results = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=count)
        results.extend(keys)
        if cursor == 0:
            break
    return results


def print_redis_info() -> None:
    """Log Redis server info and memory usage to the POPOTO-REDIS_DB logger.

    A diagnostic utility for monitoring Redis health in production. When Redis
    has a maxmemory limit configured, this function calculates and logs the
    percentage of memory currently in use, helping operators anticipate
    capacity issues before they cause evictions or failures.

    The function logs at INFO level, so it will appear in standard production
    logs without requiring debug mode.

    Note:
        This function makes multiple INFO calls to Redis, which has minimal
        overhead but should not be called in tight loops.
    """
    logger.info(POPOTO_REDIS_DB.info())

    used_memory, maxmemory = (
        int(POPOTO_REDIS_DB.info()["used_memory"]),
        int(POPOTO_REDIS_DB.info()["maxmemory"]),
    )
    maxmemory_human = POPOTO_REDIS_DB.info()["maxmemory_human"]
    if maxmemory and maxmemory > 0:
        logger.info(
            f"Redis currently consumes {round(100 * used_memory / maxmemory, 2)}% out of {maxmemory_human}"
        )


class PopotoException(Exception):
    """Base exception for Popoto framework errors. Logs the message on init.

    Centralizes error handling across the ORM by ensuring all Popoto exceptions
    are automatically logged at ERROR level when raised. This design decision
    means developers don't need to add separate logging calls when catching
    and re-raising errors - the logging happens automatically at exception
    creation time.

    This class is intentionally placed in redis_db.py (rather than a dedicated
    exceptions module) because it's imported by nearly every Popoto module,
    and redis_db.py is already a universal dependency. This minimizes import
    complexity and circular import risks.

    Attributes:
        message: Human-readable error description, also logged automatically.

    Example:
        raise PopotoException("Model 'User' has no KeyField defined")
    """

    def __init__(self, message):
        self.message = message
        logger.error(message)
