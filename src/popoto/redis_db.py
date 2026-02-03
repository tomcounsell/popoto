"""
Redis connection management for Popoto.

Connects to Redis using the ``REDIS_URL`` environment variable, or falls back
to ``localhost:6379``. Use :func:`set_REDIS_DB_settings` to reconfigure the
connection at runtime.

This module serves as the central point for Redis connectivity throughout Popoto.
Rather than requiring each model, field, or query to manage its own connection,
this module provides a single global connection instance (POPOTO_REDIS_DB) that
all components share.

Design Philosophy:
    Popoto follows a "configure once, use everywhere" pattern for database connections.
    The connection is established at module import time using environment variables,
    allowing applications to configure Redis without modifying code. This mirrors
    Django's database configuration approach, making Popoto feel familiar to Django
    developers.

Configuration:
    - REDIS_URL: Full Redis URL (e.g., "redis://user:pass@host:port/db")
    - Falls back to localhost:6379 if REDIS_URL is not set

    Optional:
    - BEGINNING_OF_TIME: Unix timestamp (seconds) used as a floor for time-series
      queries. Defaults to 0 (1970-01-01). Useful for filtering out invalid dates.

Usage:
    Most Popoto code imports POPOTO_REDIS_DB directly for performance::

        from popoto.redis_db import POPOTO_REDIS_DB
        POPOTO_REDIS_DB.hset(key, mapping=data)

    For dynamic reconfiguration (e.g., testing), use set_REDIS_DB_settings()::

        from popoto.redis_db import set_REDIS_DB_settings
        set_REDIS_DB_settings(host='test-redis', port=6380)

Note:
    Commented-out REDIS_GRAPH references indicate planned RedisGraph support
    for relationship traversal queries, which is not yet implemented.
"""

import os
import logging
import redis

# from redisgraph import Graph

logger = logging.getLogger("POPOTO-REDIS_DB")

global POPOTO_REDIS_DB
# global REDIS_GRAPH
BEGINNING_OF_TIME = 0
ENCODING = "utf-8"

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
        POPOTO_REDIS_DB = redis.from_url(REDIS_URL)
        logger.debug("Redis connection established.")
    else:
        REDIS_HOST, REDIS_PORT = "127.0.0.1:6379".split(":")
        pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0)
        POPOTO_REDIS_DB = redis.Redis(connection_pool=pool)
        # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)

except Exception as e:
    logger.info(str(e))


def set_REDIS_DB_settings(
    env_partition_name: str = "", *args, **kwargs
) -> None:
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

    used_memory, maxmemory = int(POPOTO_REDIS_DB.info()["used_memory"]), int(
        POPOTO_REDIS_DB.info()["maxmemory"]
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
