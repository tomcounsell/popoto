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

Safety:
    Popoto's own client refuses two destructive commands, by default:
    ``FLUSHDB`` when the client is bound to database 0, and ``FLUSHALL`` on
    any binding (it destroys every database including 0). The refusal raises
    :class:`Db0FlushRefusedError` **before** the command reaches the socket.
    Set ``POPOTO_ALLOW_DB0_FLUSH=1`` to restore the previous behavior; the
    variable is read at call time, not at import.

    Binding to database 0 is still permitted and unchanged -- reads and writes
    are untouched. Only the commands that destroy a whole database are guarded.

    Not covered: ``redis-cli``/``valkey-cli``, a bare ``redis.Redis()`` the
    caller constructs itself, ``redis.call('FLUSHDB')`` inside an ``EVAL``ed Lua
    script (the guard sees ``EVAL``; Popoto ships no such script), other
    destructive commands (``SHUTDOWN``, ``CONFIG SET``, ``SCRIPT FLUSH``, mass
    ``DEL``), and raw connections checked out of the pool and driven directly.

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
import threading
from typing import Any
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
    _ASYNC_MAX_CONNECTIONS = int(os.environ.get("POPOTO_ASYNC_MAX_CONNECTIONS", "128"))
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
    _SYNC_MAX_CONNECTIONS = int(os.environ.get("POPOTO_SYNC_MAX_CONNECTIONS", "128"))
except ValueError:
    _SYNC_MAX_CONNECTIONS = 128

# ---------------------------------------------------------------------------
# Exceptions and the destructive-flush guard
#
# Both of these are defined *here*, above the first client construction,
# because every client Popoto builds below is a guarded subclass and the
# subclasses need the exception and the predicate to already exist.
# ---------------------------------------------------------------------------


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

    def __init__(self, message: Any) -> None:
        self.message = message
        logger.error(message)


#: Environment variable that disables the destructive-flush guard.
ALLOW_DB0_FLUSH_ENV = "POPOTO_ALLOW_DB0_FLUSH"

#: The only two commands the guard knows about. Deliberately not extended:
#: both recorded incidents were ``flushdb``, and there is no natural stopping
#: point once ``SHUTDOWN``/``CONFIG SET``/``SCRIPT FLUSH`` are on the list.
_DESTRUCTIVE_COMMANDS = frozenset({"FLUSHDB", "FLUSHALL"})

#: Same truthy set the integrations layer accepts, so the two opt-ins behave
#: identically even though they grant different permissions.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class Db0FlushRefusedError(PopotoException, ValueError):
    """Raised instead of running a flush whose blast radius includes database 0.

    Two bases on purpose. ``PopotoException`` supplies the automatic
    ERROR-level log, so a caller that swallows the refusal still leaves a
    trace in the log -- which is exactly the failure mode this guard exists to
    make visible. ``ValueError`` matches the house pattern established by
    ``popoto.integrations.config.Db0RefusedError``, so code already written
    against ``except ValueError`` keeps catching DB-0 refusals.
    """


def _flush_refusal_reason(command: Any, db: Any, suggest: bool = True) -> str | None:
    """Return a refusal message, or ``None`` if the command is permitted.

    A pure predicate: it issues no Redis command and mutates nothing. That is
    deliberate -- it is the only way to exercise the *permitted* branch in a
    test without ever running a real destructive command against database 0.

    Args:
        command: The command name as passed to ``execute_command``. May be
            ``bytes``, or something that is not a string at all; it is
            normalized before comparison.
        db: The database the connection is bound to. A pool built from a
            unix-socket or certain URL forms carries no ``db`` key, so callers
            resolve it with ``.get("db", 0) or 0`` and a ``None`` arriving
            here means database 0 (see #490).
        suggest: Whether to look up a free database to name in the message.
            The lookup is a *synchronous* ``INFO keyspace`` round trip, so the
            async overrides pass ``suggest=False`` rather than block the event
            loop for up to the socket timeout.

    Returns:
        The refusal message, or ``None`` when the command may proceed.
    """
    if os.environ.get(ALLOW_DB0_FLUSH_ENV, "").strip().lower() in _TRUTHY:
        return None

    if isinstance(command, bytes):
        name = command.decode("utf-8", "replace")
    else:
        name = str(command)
    name = name.strip().upper()

    if name not in _DESTRUCTIVE_COMMANDS:
        return None

    try:
        bound_db = int(db or 0)
    except (TypeError, ValueError):
        bound_db = 0

    if name == "FLUSHDB":
        if bound_db != 0:
            return None
        blast = "database 0"
    else:
        # FLUSHALL is refused on every binding: it destroys every database
        # including 0, whatever this client happens to be pointed at.
        blast = "every database on this server, including database 0"

    lines = [
        f"Popoto refused to run {name}: it would wipe {blast}.",
        f"This client is bound to database {bound_db}.",
        "Database 0 is the default binding when REDIS_URL is unset, so this is "
        "usually an ad-hoc script that meant to target an isolated database.",
    ]

    free_db = None
    if suggest:
        try:
            from .integrations.config import suggest_free_db

            free_db = suggest_free_db()
        except Exception:  # pragma: no cover - best-effort diagnostic only
            free_db = None

    if free_db is not None:
        lines.append(
            f"Export REDIS_URL=redis://localhost:6379/{free_db} BEFORE "
            "'import popoto' to work on a free database instead."
        )
    else:
        lines.append(
            "Export REDIS_URL with a non-zero database number BEFORE "
            "'import popoto' to work on an isolated database instead."
        )

    lines.append(
        f"To allow this anyway, set {ALLOW_DB0_FLUSH_ENV}=1 in the environment."
    )
    return " ".join(lines)


def _bound_db(client: Any) -> int:
    """Resolve the database a client is bound to, **at call time**.

    Read at call time and never captured at construction, so the guard judges
    the connection that will actually be wiped. That is what makes it correct
    across ``pytest_plugin._swap_db()`` and
    ``integrations.config.bind_connection()``, both of which rebind the pool
    attribute on the existing client object.
    """
    try:
        kwargs = client.connection_pool.connection_kwargs
    except AttributeError:
        return 0
    return kwargs.get("db", 0) or 0


def _check_flush(client: Any, args: tuple[Any, ...], suggest: bool = True) -> None:
    """Raise :class:`Db0FlushRefusedError` if ``args`` is a refused flush."""
    if not args:
        # ``execute_command()`` with no arguments is redis-py's problem, not
        # the guard's. Never raise IndexError from here.
        return
    reason = _flush_refusal_reason(args[0], _bound_db(client), suggest=suggest)
    if reason is not None:
        raise Db0FlushRefusedError(reason)


class GuardedPipeline(redis.client.Pipeline):
    """A sync pipeline that refuses destructive flushes. See :class:`GuardedRedis`."""

    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        _check_flush(self, args)
        return super().execute_command(*args, **kwargs)


class GuardedRedis(redis.Redis):
    """Popoto's sync client. Refuses ``FLUSHDB`` on database 0 and ``FLUSHALL``
    anywhere.

    The hook is ``execute_command`` rather than the ``flushdb``/``flushall``
    methods, because that single site also catches the raw
    ``execute_command("FLUSHDB")`` form that the method overrides would miss.

    ``Redis.pipeline()`` hard-codes the stock ``Pipeline`` class rather than
    ``type(self)``, so a pipeline off a guarded client would otherwise be
    unguarded. Reassigning ``__class__`` on the returned object guards both
    the buffered and the post-``watch()`` immediate paths without depending on
    the ``Pipeline`` constructor signature, which moves between redis-py
    versions.

    Not covered, stated plainly: ``redis-cli``, a bare ``redis.Redis()`` the
    caller constructs itself, ``redis.call('FLUSHDB')`` inside an ``EVAL``ed
    Lua script (the guard sees ``EVAL``; Popoto ships no such script), other
    destructive commands such as ``SHUTDOWN``/``CONFIG SET``, and raw
    connections checked out of the pool and driven directly.
    """

    def execute_command(self, *args: Any, **options: Any) -> Any:
        _check_flush(self, args)
        return super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = True, shard_hint: Any = None) -> Any:
        pipe = super().pipeline(transaction=transaction, shard_hint=shard_hint)
        pipe.__class__ = GuardedPipeline
        return pipe


class GuardedAsyncPipeline(aioredis.client.Pipeline):
    """The async pipeline counterpart of :class:`GuardedPipeline`.

    ``execute_command`` is a plain ``def`` here, matching
    ``redis.asyncio.client.Pipeline``: on the buffered path it returns the
    pipeline itself for chaining, and only the post-``watch()`` immediate path
    returns an awaitable. Declaring it ``async def`` would break buffered
    chaining -- ``pipe.set(...)`` would hand back a coroutine.

    ``suggest=False``: the free-database lookup is a synchronous round trip
    and must never run on the event loop.
    """

    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        _check_flush(self, args, suggest=False)
        return super().execute_command(*args, **kwargs)


class GuardedAsyncRedis(aioredis.Redis):
    """Popoto's async client, with the same two rules as :class:`GuardedRedis`.

    ``redis.asyncio.Redis.pipeline()`` hard-codes the stock async ``Pipeline``
    exactly as the sync client does, and it is a plain (non-``async``) method
    on both hierarchies -- so it is overridden, not awaited.
    """

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        _check_flush(self, args, suggest=False)
        return await super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = True, shard_hint: Any = None) -> Any:
        pipe = super().pipeline(transaction=transaction, shard_hint=shard_hint)
        pipe.__class__ = GuardedAsyncPipeline
        return pipe


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
        POPOTO_REDIS_DB = GuardedRedis(connection_pool=pool)
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
        POPOTO_REDIS_DB = GuardedRedis(connection_pool=pool)
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

    Like the module-level init path, the new client is backed by a
    ``BlockingConnectionPool`` capped at ``_SYNC_MAX_CONNECTIONS`` so a
    connection swap does not silently drop back-pressure. (A plain
    ``redis.Redis(**kwargs)`` builds an effectively unbounded pool,
    ``max_connections == 2**31``.)

    Args:
        env_partition_name: Optional namespace prefix for key isolation.
            Falls back to the ``ENV`` environment variable.
        *args, **kwargs: Connection parameters for ``redis.Redis()``.
            Common kwargs: host, port, db, password, socket_timeout.
            Passing ``connection_pool`` explicitly, or using positional
            args, bypasses the managed pool (see below).

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
    # ``redis.Redis`` and ``BlockingConnectionPool`` do not share a positional
    # signature, so positional args can't be forwarded to the pool. A caller
    # supplying its own ``connection_pool`` has already chosen its pooling
    # policy. Both cases fall through to the direct constructor.
    if args or "connection_pool" in kwargs:
        POPOTO_REDIS_DB = GuardedRedis(*args, **kwargs)
    else:
        pool = redis.BlockingConnectionPool(
            max_connections=_SYNC_MAX_CONNECTIONS, **kwargs
        )
        POPOTO_REDIS_DB = GuardedRedis(connection_pool=pool)
    # global REDIS_GRAPH
    # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)
    logger.debug("Redis connection reset.")


# Connection parameters that are safe to copy from a connection pool's
# ``connection_kwargs`` when constructing a *sibling* ``redis.Redis`` client
# (e.g. a DB-0 probe that reuses the live host/port/auth but targets a
# different DB). redis-py 8 injects pool-internal bookkeeping keys —
# ``himport_registry``, ``maint_notifications_config``,
# ``maint_notifications_pool_handler``, ``orig_host_address``,
# ``orig_socket_timeout``, ``orig_socket_connect_timeout`` — into a pool's
# ``connection_kwargs``. Splatting that full dict into ``redis.Redis(**kwargs)``
# raises ``TypeError: Redis.__init__() got an unexpected keyword argument
# 'himport_registry'`` (the exact key surfaced depends on kwarg-check order).
# Whitelisting the standard connection params sidesteps this on every redis-py
# version, and stays Valkey-safe (no Redis-module-specific options).
_SIBLING_CONNECTION_KEYS = (
    "host",
    "port",
    "db",
    "username",
    "password",
    "socket_timeout",
    "socket_connect_timeout",
    "ssl",
    "ssl_keyfile",
    "ssl_certfile",
    "ssl_ca_certs",
    "unix_socket_path",
)


def sibling_client_kwargs(
    source_kwargs: dict[str, object], **overrides: object
) -> dict[str, object]:
    """Whitelist connection kwargs safe to splat into ``redis.Redis(**kw)``.

    A connection pool's ``connection_kwargs`` cannot be passed wholesale to the
    ``redis.Redis`` constructor on redis-py 8: the pool injects internal keys
    (``himport_registry``, ``maint_notifications_pool_handler``, ``orig_*`` …)
    that ``Redis.__init__`` rejects. This returns only the standard connection
    parameters present in ``source_kwargs`` (dropping ``None`` values), then
    applies any explicit ``overrides`` (e.g. ``db=0`` to build a DB-0 probe on
    the same host/port/auth).

    Args:
        source_kwargs: A pool's ``connection_kwargs`` (or any mapping of
            connection parameters) to copy the safe subset from.
        **overrides: Parameters to set/replace on the returned dict, applied
            after the whitelist (so ``db=0`` wins over any inherited ``db``).

    Returns:
        A dict containing only whitelisted params plus the overrides, ready to
        splat into ``redis.Redis(**kwargs)`` on any redis-py version.

    Example::

        from popoto.redis_db import (
            GuardedRedis, POPOTO_REDIS_DB, sibling_client_kwargs,
        )

        kwargs = sibling_client_kwargs(
            POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0
        )
        db0 = GuardedRedis(**kwargs)  # guarded: a DB-0 probe reads, never flushes
    """
    out = {
        key: source_kwargs[key]
        for key in _SIBLING_CONNECTION_KEYS
        if source_kwargs.get(key) is not None
    }
    out.update(overrides)
    return out


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

    The async client mirrors the *current* sync client's connection parameters
    (host, port, db, auth) rather than re-reading ``REDIS_URL`` independently.
    This is what makes the async path follow any runtime connection swap — most
    importantly the pytest plugin's switch to the test DB. Re-reading
    ``REDIS_URL`` here would silently pin the async client to DB 0 while sync
    writes go to the test DB, so async reads would never see them.

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

        # Mirror the sync connection's parameters so the async client always
        # targets the same server/DB as POPOTO_REDIS_DB. The sync pool's
        # connection_kwargs were built from REDIS_URL (or the localhost
        # default) and reflect any later swap of the DB, so reusing them keeps
        # the two clients in lockstep without re-parsing REDIS_URL.
        sync_kwargs = POPOTO_REDIS_DB.connection_pool.connection_kwargs
        async_kwargs = {}
        for key in ("host", "port", "db", "password", "username"):
            value = sync_kwargs.get(key)
            if value is not None:
                async_kwargs[key] = value
        async_kwargs.setdefault("socket_timeout", 5)
        async_kwargs.setdefault("socket_connect_timeout", 5)

        pool = aioredis.BlockingConnectionPool(
            max_connections=_ASYNC_MAX_CONNECTIONS,
            **async_kwargs,
        )
        _POPOTO_ASYNC_REDIS_DB = GuardedAsyncRedis(connection_pool=pool)
        logger.debug(
            "Async Redis connection established (db=%s).",
            async_kwargs.get("db", 0),
        )
        return _POPOTO_ASYNC_REDIS_DB


async def set_async_redis_db_settings(
    env_partition_name: str = "", *args, **kwargs
) -> None:
    """Reset the global async Redis connection with new settings.

    This is the async equivalent of set_REDIS_DB_settings(). Use it to
    reconfigure the async connection for testing or multi-tenant scenarios.

    As with :func:`get_async_redis_db`, the new client is backed by a
    ``BlockingConnectionPool`` capped at ``_ASYNC_MAX_CONNECTIONS`` so a
    connection swap does not silently drop back-pressure.

    Args:
        env_partition_name: Optional namespace prefix for key isolation.
        *args, **kwargs: Connection parameters for ``redis.asyncio.Redis()``.
            Passing ``connection_pool`` explicitly, or using positional args,
            bypasses the managed pool.

    Example:
        await set_async_redis_db_settings(host='localhost', port=6379, db=15)
    """
    global _POPOTO_ASYNC_REDIS_DB

    kwargs.setdefault("socket_timeout", 5)
    kwargs.setdefault("socket_connect_timeout", 5)

    async with _async_redis_lock:
        if _POPOTO_ASYNC_REDIS_DB is not None:
            await _POPOTO_ASYNC_REDIS_DB.close()
        # See set_REDIS_DB_settings for why positional args and an explicit
        # connection_pool bypass the managed pool.
        if args or "connection_pool" in kwargs:
            _POPOTO_ASYNC_REDIS_DB = GuardedAsyncRedis(*args, **kwargs)
        else:
            pool = aioredis.BlockingConnectionPool(
                max_connections=_ASYNC_MAX_CONNECTIONS, **kwargs
            )
            _POPOTO_ASYNC_REDIS_DB = GuardedAsyncRedis(connection_pool=pool)
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


# ---------------------------------------------------------------------------
# Lua script registry
# ---------------------------------------------------------------------------

#: Exceptions that mean "the server is unreachable", as opposed to a bad
#: query. Recipes let these propagate so an outage never masquerades as an
#: empty retrieval; the harness boundary is where they get swallowed.
OUTAGE_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def normalize_redis_keys(keys: Any) -> set[str]:
    """Return ``keys`` as a set of ``str``.

    Raw Redis replies (``SMEMBERS``, ``SCAN``, ``filter_for_keys_set()``)
    are ``bytes``; ranked lists and ``db_key.redis_key`` are ``str``.
    Intersecting the two directly is a silent empty set, which is how the
    first cut of the #576 fix returned nothing for every query. Every site
    that compares keys from both worlds goes through this one function.
    """
    return {key.decode() if isinstance(key, bytes) else str(key) for key in keys}


_SCRIPTS: dict[str, Any] = {}
_SCRIPTS_LOCK = threading.Lock()
_LOADED_SHAS: set[str] = set()


def lua_script(script_text: str) -> Any:
    """Return a cached ``redis.commands.core.Script`` for ``script_text``.

    A ``Script`` sends ``EVALSHA`` and falls back to loading the source only
    when the server does not know the SHA, so a script is uploaded once per
    server rather than on every call. Scripts are keyed by their text and
    bound to the shared client; passing ``client=`` to the returned object
    (a sibling client) is supported by redis-py.
    """
    script = _SCRIPTS.get(script_text)
    if script is None:
        with _SCRIPTS_LOCK:
            script = _SCRIPTS.get(script_text)
            if script is None:
                script = POPOTO_REDIS_DB.register_script(script_text)
                _SCRIPTS[script_text] = script
    return script


def run_lua(client: Any, script_text: str, numkeys: int, *keys_and_args: Any) -> Any:
    """Run ``script_text`` with the ``EVAL``-style argument layout.

    Drop-in replacement for ``client.eval(script, numkeys, *args)``. On a
    plain client it goes through :func:`lua_script`, so the server sees
    ``EVALSHA`` after the first call and a ``NOSCRIPT`` reply self-heals.

    On a pipeline it queues ``EVALSHA`` directly. redis-py's ``Script``
    would register itself on the pipeline and make ``execute()`` pay a
    ``SCRIPT EXISTS`` round trip every time; instead the script is loaded
    once per process (``SCRIPT LOAD`` on first use) and the SHA reused. A
    ``SCRIPT FLUSH`` between that load and the pipeline's ``execute()``
    surfaces as ``NoScriptError`` from ``execute()``; the next direct call
    reloads it.
    """
    keys = list(keys_and_args[:numkeys])
    args = list(keys_and_args[numkeys:])
    script = lua_script(script_text)
    if isinstance(client, redis.client.Pipeline):
        if script.sha not in _LOADED_SHAS:
            script.sha = POPOTO_REDIS_DB.script_load(script_text)
            _LOADED_SHAS.add(script.sha)
        return client.evalsha(script.sha, numkeys, *keys, *args)
    return script(keys=keys, args=args, client=client)
