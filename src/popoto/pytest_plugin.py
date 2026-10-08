"""Pytest plugin for opt-in Popoto test DB isolation.

This plugin ships as a ``pytest11`` entry point, so it loads in every project
that depends on popoto — but it is **inert** (does nothing) unless you opt in.
When you do, it:

1. Switches the Redis connection to a dedicated test database
2. Flushes the test database before each test for a clean slate
3. Resets the async Redis connection per test to avoid event loop conflicts

Configuration:

    The test database number can be set via (in priority order):

    1. Environment variable: ``POPOTO_TEST_DB=14 pytest``
    2. pytest ini option in ``pyproject.toml``::

        [tool.pytest.ini_options]
        popoto_test_db = 14

    Neither set: the plugin does nothing — no swap, no flush. A session that
    uses popoto models without opting in gets exactly one
    ``PopotoIsolationWarning`` on the first Redis-touching operation, naming
    the DB it is writing to and both opt-in mechanisms. A session where
    popoto is merely importable but never touches Redis stays silent.

Disabling:

    To disable the plugin entirely (including the isolation warning)::

        pytest -p no:popoto

Conformance harness (#759 M0):

    Tests marked ``conformance`` that request the ``backend`` fixture run once
    per configured storage backend. Opt in with
    ``popoto_conformance_backends = "redis,postgres"`` (ini) or the
    ``POPOTO_CONFORMANCE_BACKENDS`` environment variable; the default is Redis
    only, and nothing changes for a project that uses neither the marker nor
    the fixture. See the "Conformance harness" section at the bottom of this
    module.

Auth Preservation:

    The plugin preserves any host, port, password, and username from the
    current Redis connection when switching databases, so REDIS_URL with
    authentication continues to work.

Known limits:

    - Async-only suites are not covered: ``get_async_redis_db()`` builds its
      client lazily inside the running event loop, so there is no async pool
      in existence at ``pytest_configure`` time to arm the warning on. A
      suite that only ever touches the async client gets no warning.
    - The warning does not survive a manual ``_swap_db()`` /
      ``set_REDIS_DB_settings()`` pool rebind — that replaces the pool object
      the warning was armed on. Degrading to silence is acceptable (this is
      an advisory, never a correctness dependency).
    - One warning per xdist worker process is expected/acceptable.
"""

import asyncio
import logging
import os
import re
import threading
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest
import redis

from popoto import redis_db

logger = logging.getLogger("POPOTO-PYTEST")

# Guards the fired-flag flip and the disarm on the shared connection pool
# (see `_make_tripwire`). Module-level: shared across every tripwire armed in
# this interpreter, since the pool itself is process-global.
_tripwire_lock = threading.Lock()


class PopotoIsolationWarning(UserWarning):
    """Raised once per session when popoto touches Redis without test isolation.

    The pytest plugin is installed (it ships as a ``pytest11`` entry point)
    but neither ``popoto_test_db`` nor ``POPOTO_TEST_DB`` opted it in, so
    nothing is swapping or flushing the connection. See the module docstring.
    """


def pytest_configure(config):
    """Prepare module aliasing and switch onto the test DB before collection.

    Both steps must happen before pytest imports any test module, so they live
    in this hook rather than in a fixture.
    """
    _collapse_src_popoto()
    test_db = _configure_test_db(config)
    # Conformance harness (#759 M0). Resolving the backends here makes an
    # unknown name fail the session at startup, like a non-integer
    # popoto_test_db. Everything else -- markers, fixtures, parametrisation,
    # the reason rule -- exists only in a session that opted in (#763 B1).
    config._popoto_opted_in = test_db is not None
    config._popoto_conformance_backends = _resolve_conformance_backends(config)
    config._popoto_conformance_active = (
        config._popoto_opted_in or _conformance_backends_configured(config)
    )
    if config._popoto_conformance_active:
        _register_conformance_markers(config)
        config.pluginmanager.register(
            _ConformanceFixtures(), _CONFORMANCE_FIXTURES_PLUGIN
        )
    _pin_session_backend_to_redis(test_db)


def _pin_session_backend_to_redis(test_db: Any) -> None:
    """Pin the process default storage backend to Redis for the session
    (#759 M1a; deferred from M0 because it needs ``get_backend()``).

    Runs before collection, so a module-scope ``Model.save()`` and every
    unmarked test bind Redis even when ``POPOTO_BACKEND`` names another
    backend; only the conformance ``backend`` fixture moves a test off it, and
    it restores this pin on teardown. Gated on the plugin opt-in
    (``popoto_test_db`` / ``POPOTO_TEST_DB``): the plugin loads in every
    downstream pytest session, and one that never opted in keeps the runtime
    selection ``import popoto`` gives it. Pinning captures no client --
    ``RedisBackend`` resolves ``get_REDIS_DB()`` per operation -- so the DB
    swap above and any later ``set_REDIS_DB_settings()`` are still observed.
    """
    if test_db is None:
        return
    from popoto.backends import set_backend

    set_backend("redis")


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

    Returns the resolved test DB, or ``None`` when the session is not opted
    in, so ``pytest_configure`` can gate the conformance harness's opt-in-only
    checks on the same decision without resolving it twice.
    """
    try:
        test_db = _resolve_test_db(config)
    except ValueError:
        raise  # Misconfiguration (e.g. db=0) — fail loudly and early.
    if test_db is None:
        # Not opted in: leave the developer's connection alone, but arm a
        # one-shot tripwire so a session that actually touches Redis gets a
        # single advisory warning instead of silent, unisolated writes.
        #
        # Non-fatal by construction: this hook runs in EVERY downstream
        # pytest session that has popoto in its dependency tree, and today
        # the inert path is a bare `return` that touches nothing. A conftest
        # that rebinds `redis_db.POPOTO_REDIS_DB` to something without a
        # `.connection_pool` (e.g. `redis.RedisCluster`) must not be able to
        # abort collection — that would be strictly worse than the silence
        # this warning exists to fix.
        try:
            pool = redis_db.POPOTO_REDIS_DB.connection_pool
            original = pool.get_connection  # bound method, captured pre-swap
            wrapper = _make_tripwire(pool, original)
            # Assigning over a bound method is the point — this arms the
            # tripwire. The ignore is only valid while setup.cfg resolves
            # popoto's self-imports (#663); revert that and warn_unused_ignores
            # turns this line into an error, which is the intended coupling.
            pool.get_connection = wrapper  # type: ignore[method-assign]
            config._popoto_tripwire = (pool, wrapper)
        except Exception as e:  # never break a downstream collection
            logger.debug("popoto pytest plugin: isolation warning not armed (%s)", e)
        return None

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
    return test_db


def _make_tripwire(pool: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    """Build a one-shot ``get_connection`` wrapper that warns on first use.

    Only fires once (module-level lock + closure-local ``fired`` flag — a
    module-level flag would make a second arm in the same interpreter
    permanently silent). Disarms itself the moment it fires, so the wrapper
    never sits on the hot path after the first Redis operation.

    Wrapper body order is load-bearing: the Redis op this wrapper wraps must
    never be affected by the warning, so ``warnings.warn`` is exception-guarded
    and the ``logger.warning`` mirror runs first, unguarded, so the signal
    survives even a downstream suite's ``filterwarnings = error``.
    """
    fired = False

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        # Hold the lock only for the fired check/flip — never across the
        # delegated `original(...)` call, which can block on pool exhaustion
        # and would otherwise serialize all connection acquisition.
        with _tripwire_lock:
            first_fire = not fired
            if first_fire:
                fired = True
                _disarm_tripwire(pool, wrapper)
        if not first_fire:
            return original(*args, **kwargs)

        # Resolve at trip time, not at arm time: capturing this in the
        # closure at configure time would make the message lie if anything
        # rebound the connection in between. `connection_kwargs` has no
        # "db" key for some pool constructions (unix-socket / URL forms) —
        # `.get(..., default)` only, never a bare `[...]` lookup (see #490).
        db = pool.connection_kwargs.get("db", 0)
        msg = (
            f"popoto is writing to Redis DB {db} during this pytest session "
            "and is NOT isolating or flushing it (the popoto pytest plugin "
            'is installed but not opted in). Set popoto_test_db = "15" '
            "under [tool.pytest.ini_options] or export POPOTO_TEST_DB to "
            "isolate, or pass -p no:popoto to silence this warning."
        )
        # Unguarded and first: this is what log-capture / -W error suites see
        # even when the warnings.warn below is swallowed by the caller.
        logger.warning(msg)
        try:
            warnings.warn(msg, PopotoIsolationWarning, stacklevel=2)
        except Exception:
            pass
        return original(*args, **kwargs)

    return wrapper


def _disarm_tripwire(pool: Any, wrapper: Callable[..., Any] | None) -> None:
    """Remove the tripwire wrapper from ``pool`` if it is still ours.

    Identity-checked so this never clobbers a wrapper installed by someone
    else, and never touches a different pool if a conftest rebound
    ``redis_db.POPOTO_REDIS_DB`` after arm time. ``pop``, not reassigning the
    bound method back: assigning `original` would leave a shadowing instance
    attribute plus a reference cycle, so the pool would not be byte-identical
    to its pre-arm state.
    """
    if getattr(pool, "get_connection", None) is wrapper:
        pool.__dict__.pop("get_connection", None)


def pytest_unconfigure(config: Any) -> None:
    """Disarm the isolation tripwire, if it is still armed and unfired.

    A no-op when the tripwire already fired (it self-disarms on trip) or was
    never armed (e.g. the opt-in path, or arming failed non-fatally).
    """
    pool, wrapper = getattr(config, "_popoto_tripwire", (None, None))
    if pool is not None:
        with _tripwire_lock:
            _disarm_tripwire(pool, wrapper)


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
    """Register the ``popoto_test_db`` and ``popoto_conformance_backends`` ini
    options."""
    parser.addini(
        "popoto_test_db",
        "Redis database number to isolate Popoto tests on. Setting this (or "
        "the POPOTO_TEST_DB environment variable) is what activates the "
        "plugin; without either it does nothing.",
        default="",
    )
    parser.addini(
        "popoto_conformance_backends",
        "Comma-separated storage backends the `backend` fixture is "
        "parametrised over in tests marked `conformance` (any of: redis, "
        "postgres). The POPOTO_CONFORMANCE_BACKENDS environment variable "
        "overrides it. Unset: redis only.",
        default="",
    )


def _resolve_test_db(config):
    """Resolve the test DB number.

    Priority: POPOTO_TEST_DB env var > ini option. Returns ``None`` when
    neither is set: the plugin is installed through a ``pytest11`` entry
    point, so it loads in every project that depends on popoto, and it
    must not flush a database nobody asked it to touch. Opting in is one
    line of ``pyproject.toml`` (``popoto_test_db = "15"``) or the env var.
    """
    env_db = os.environ.get("POPOTO_TEST_DB", "").strip()
    ini_value = config.getini("popoto_test_db")
    if not isinstance(ini_value, str):
        ini_value = ""
    raw_value = env_db if env_db else ini_value.strip()
    if not raw_value:
        return None
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
    if test_db is None:
        yield None
        return
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
    if _popoto_test_db is None:
        yield
        return
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
        # dict[str, Any], not the helper's stricter dict[str, object]: these are
        # opaque connection params that redis-py validates at call time, and
        # mypy cannot correlate a runtime whitelist's keys with GuardedRedis's
        # parameters (#663). Annotated here rather than on
        # sibling_client_kwargs() so the widening stays at the one site that
        # needs it and a future caller starts from the accurate signature.
        pool_kwargs: dict[str, Any] = redis_db.sibling_client_kwargs(
            redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0
        )
        # GuardedRedis, not a bare _redis.Redis: this client is explicitly bound
        # to db=0. It only ever calls dbsize(), but a Popoto-constructed DB-0
        # client that sits outside the #577 flush guard is exactly the shape of
        # object that caused the incident.
        db0_client = redis_db.GuardedRedis(**pool_kwargs)
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


# -- Conformance harness (#759 M0(b); ported from #631's poc/backend-seam) ----
#
# A test marked ``conformance`` that requests the ``backend`` fixture runs once
# per configured storage backend. Which backends is an opt-in in the same shape
# as ``popoto_test_db``:
#
#   POPOTO_CONFORMANCE_BACKENDS=redis,postgres pytest -m conformance
#
#   [tool.pytest.ini_options]
#   popoto_conformance_backends = "redis,postgres"
#
# Neither set: Redis only. Unmarked tests are never parametrised; a test that
# requests ``backend`` without the marker gets the Redis descriptor, once.
#
# All of it is gated on the session having opted in -- ``popoto_test_db`` /
# ``POPOTO_TEST_DB``, or ``popoto_conformance_backends`` /
# ``POPOTO_CONFORMANCE_BACKENDS`` set. Without that, the markers are not
# registered and the fixtures do not exist, so a project that never opted in
# collects exactly what ``-p no:popoto`` collects -- including one with its own
# ``conformance`` marker, ``backend`` fixture or ``redis_only`` marker (#763
# B1). Even when opted in, parametrisation and the ``redis_only`` reason rule
# apply only where the ``backend`` that resolves for the test is the plugin's
# own: a project fixture, or ``@pytest.mark.parametrize("backend", ...)``,
# wins and is left alone.
#
# ``backend`` yields a :class:`ConformanceBackend` descriptor (a name and, on
# the Postgres leg, the session schema, a DSN whose ``search_path`` is that
# schema, and the bound ``PostgresBackend``). Since #759 M1b each leg also
# *binds* its backend as the process default for the test -- ``set_backend``
# -- and restores the previous binding on teardown, discarding memoised
# ``bind()`` state both ways, so module-level test models run on whichever leg
# is active with no re-declaration. (M0's ``conformance(harness=True)`` flag
# and its "Postgres backend arrives in M1" skip are gone: every leg now has a
# backend to run against.)
#
# The Postgres leg isolates on one schema per session, ``popoto_test_<32 hex>``,
# created at first use and ``DROP SCHEMA ... CASCADE``d at session end, with
# every table in it dropped before each test (the FLUSHDB mirror: Redis has no
# schema to survive a flush, and test modules declare same-named models with
# different fields, so a table must not outlive its test either). The reset
# names every table in one ``DROP TABLE`` and has no ``CASCADE``: a foreign
# key reaching into the schema from outside it makes the reset fail rather
# than drop a table the harness does not own. The session-end ``DROP SCHEMA
# ... CASCADE`` does reach outside, but only to objects that depend on a test
# table: a view elsewhere selecting from one is dropped, and a foreign key
# elsewhere referencing one loses that constraint (its table and rows stay).
# Two refusals mirror ``Db0FlushRefusedError`` and are raised *before* any
# connection, as :class:`PostgresIsolationRefusedError`: a schema name the
# harness did not generate (``public`` and ``popoto`` included), and a
# ``POSTGRES_URL`` that names no database -- checked before ``psycopg`` is
# imported, so it is refused even where the driver is absent. When ``psycopg``
# is not installed or ``POSTGRES_URL`` is unset the leg *skips* with a visible
# reason rather than failing.
#
# ``psycopg`` is only ever imported inside the Postgres-leg code below, never at
# module scope: this plugin loads in every downstream pytest session.

CONFORMANCE_BACKENDS: tuple[str, ...] = ("redis", "postgres")
"""Every backend name ``popoto_conformance_backends`` may list."""

POSTGRES_TEST_SCHEMA_PREFIX = "popoto_test_"
"""Every schema the harness creates, truncates or drops carries this prefix."""

_CONFORMANCE_FIXTURES_PLUGIN = "popoto-conformance-fixtures"
"""Plugin name the conformance fixtures register under, in opted-in sessions."""

_POSTGRES_TEST_SCHEMA = re.compile(
    rf"^{re.escape(POSTGRES_TEST_SCHEMA_PREFIX)}[0-9a-f]{{32}}$"
)


class PostgresIsolationRefusedError(redis_db.PopotoException, ValueError):
    """Raised before any connection when the conformance harness would touch a
    Postgres namespace it must not own.

    The Postgres mirror of :class:`~popoto.redis_db.Db0FlushRefusedError`,
    with the same two bases for the same reasons: ``PopotoException`` logs the
    refusal even when a caller swallows it, ``ValueError`` matches the
    ``popoto_test_db=0`` refusal. Two cases raise it: a schema name other than
    ``popoto_test_`` plus 32 lowercase hex characters (so ``public``,
    ``popoto`` and ``popoto_test_prod`` are all refused), and a
    ``POSTGRES_URL`` that names no database, which libpq would silently
    resolve to the connecting role's default.
    """


@dataclass(frozen=True)
class ConformanceBackend:
    """What the ``backend`` fixture yields: which leg this is, and where.

    The fixture also binds the leg's backend process-wide for the test (see
    the module notes); ``instance`` is that backend on the Postgres leg.
    ``dsn`` is ``POSTGRES_URL`` with ``options=-c search_path=<schema>``
    appended, so any connection opened from it resolves unqualified names
    inside the session schema; ``dsn``, ``schema`` and ``instance`` are
    ``None`` on the Redis leg.
    """

    name: str
    dsn: str | None = None
    schema: str | None = None
    instance: Any = None

    @property
    def is_redis(self) -> bool:
        return self.name == "redis"


def _register_conformance_markers(config: Any) -> None:
    """Register ``conformance`` / ``redis_only`` so an opted-in suite using
    the fixture needs no ``markers`` entry of its own. Called only in a session
    that opted in: registering them anywhere else would let a project's
    undeclared ``conformance`` mark pass ``--strict-markers`` with the plugin
    and fail without it."""
    config.addinivalue_line(
        "markers",
        "conformance: storage-backend conformance test; the `backend` fixture "
        "is parametrised over POPOTO_CONFORMANCE_BACKENDS / "
        "popoto_conformance_backends (default: redis only) and binds each "
        "leg's backend for the test",
    )
    config.addinivalue_line(
        "markers",
        "redis_only(reason): an assertion that only holds on Redis -- skipped "
        "on every other conformance leg. reason= is required",
    )


def _resolve_conformance_backends(config: Any) -> tuple[str, ...]:
    """The backends ``backend`` is parametrised over, in configured order.

    Priority mirrors ``_resolve_test_db``: ``POPOTO_CONFORMANCE_BACKENDS`` >
    ``popoto_conformance_backends`` ini > ``("redis",)``. Names are
    case-insensitive and de-duplicated; an unknown name is a ``ValueError``
    (fail loudly, like a non-integer ``popoto_test_db``) rather than a silent
    Redis-only run that *looks* like Postgres coverage.
    """
    env_value = os.environ.get("POPOTO_CONFORMANCE_BACKENDS", "").strip()
    ini_value = config.getini("popoto_conformance_backends")
    if not isinstance(ini_value, str):
        ini_value = ""
    raw_value = env_value if env_value else ini_value.strip()
    if not raw_value:
        return ("redis",)
    names = tuple(
        name
        for name in dict.fromkeys(part.strip().lower() for part in raw_value.split(","))
        if name
    )
    unknown = [name for name in names if name not in CONFORMANCE_BACKENDS]
    if unknown:
        raise ValueError(
            f"popoto_conformance_backends names unknown backend(s) {unknown!r}; "
            f"choose from {list(CONFORMANCE_BACKENDS)!r}"
        )
    return names or ("redis",)


def _conformance_backends_configured(config: Any) -> bool:
    """Whether the conformance opt-in itself is set (env or ini), whatever
    ``popoto_test_db`` says."""
    if os.environ.get("POPOTO_CONFORMANCE_BACKENDS", "").strip():
        return True
    ini_value = config.getini("popoto_conformance_backends")
    return isinstance(ini_value, str) and bool(ini_value.strip())


def _uses_popoto_backend(metafunc: Any) -> bool:
    """True only when the session opted in and the ``backend`` fixture that
    resolves for this test is the plugin's own (#763 B1).

    One check covers both halves. The fixtures are registered only in a
    session that opted in (``pytest_configure``), so without the opt-in there
    is nothing of ours to resolve. With it, the closest definition wins in
    pytest -- the last ``FixtureDef`` -- so a project's ``backend`` fixture in
    a conftest, module or class shadows ours; and pytest leaves an argument
    named in ``@pytest.mark.parametrize("backend", ...)`` out of the fixture
    closure altogether, so a direct parametrisation has no ``FixtureDef`` for
    it at all. In every one of those cases the test belongs to the project.
    """
    if "backend" not in metafunc.fixturenames:
        return False
    fixturedefs = metafunc._arg2fixturedefs.get("backend") or ()
    if not fixturedefs:
        return False
    ours = metafunc.config.pluginmanager.get_plugin(_CONFORMANCE_FIXTURES_PLUGIN)
    return ours is not None and getattr(fixturedefs[-1].func, "__self__", None) is ours


def _check_redis_only_reasons(metafunc: Any) -> None:
    """Fail collection on a ``redis_only`` mark with no ``reason=``.

    A reasonless mark cannot be audited: nobody can tell later whether the
    Redis-only property it guards still exists (#631 TD-16/TD-35, where 13 POC
    marks had none). Checked only where the mark can take effect -- a test
    that resolves the plugin's own ``backend`` fixture, which is the only
    thing that reads it -- so a project using a marker of the same name for
    something else sees no change.
    """
    definition = metafunc.definition
    for mark in definition.iter_markers("redis_only"):
        reason = mark.kwargs.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            pytest.fail(
                f"{definition.nodeid}: @pytest.mark.redis_only needs a "
                "reason= naming the Redis-only property it guards, e.g. "
                '@pytest.mark.redis_only(reason="reads the $Class: set through '
                'the raw client"). A mark without one cannot be audited.',
                pytrace=False,
            )


def pytest_generate_tests(metafunc: Any) -> None:
    """Parametrise the plugin's ``backend`` over the configured backends --
    only for tests carrying the ``conformance`` marker, in a session that
    opted in.

    Three opt-ins, all required: the session (see ``pytest_configure``), the
    marker (an unmarked test requesting ``backend`` gets the Redis leg once),
    and the fixture itself being the plugin's (see
    :func:`_uses_popoto_backend`). Anything else is left exactly as pytest
    would collect it with ``-p no:popoto``.
    """
    if not _uses_popoto_backend(metafunc):
        return
    _check_redis_only_reasons(metafunc)
    conformance = metafunc.definition.get_closest_marker("conformance")
    if conformance is None:
        return
    names = getattr(metafunc.config, "_popoto_conformance_backends", None)
    if names is None:
        names = _resolve_conformance_backends(metafunc.config)
    metafunc.parametrize("backend", names, indirect=True, ids=names)


def _check_schema_name(name: str) -> str:
    """Refuse any schema the harness did not generate -- before any SQL.

    ``public`` is the documented refusal; requiring the ``popoto_test_``
    prefix plus exactly 32 lowercase hex characters (what ``uuid4().hex``
    generates) is the same guard stated positively, so a ``DROP SCHEMA ...
    CASCADE`` can never reach a namespace with someone else's tables in it --
    ``popoto``, the plan's production schema, included. The name is also
    interpolated through ``psycopg.sql.Identifier``; pinning it to a plain
    identifier keeps a quoted schema name from ever being a surprise.
    """
    if not _POSTGRES_TEST_SCHEMA.fullmatch(name):
        raise PostgresIsolationRefusedError(
            f"refusing to use Postgres schema {name!r} for tests: the "
            f"conformance harness only touches schemas named "
            f"{POSTGRES_TEST_SCHEMA_PREFIX}<32 lowercase hex characters> that it "
            "generated itself (schemas 'public' and 'popoto' are never test "
            "schemas, mirroring the DB-0 refusal)"
        )
    return name


def _check_postgres_url(url: str) -> str:
    """Refuse a ``POSTGRES_URL`` that names no database -- before connecting.

    libpq resolves a db-less URL to a database named after the connecting
    role, which is the Postgres shape of the db-less ``REDIS_URL`` resolving to
    database 0 (#584/#639): a valid-looking binding to somewhere nobody chose.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres"):
        raise PostgresIsolationRefusedError(
            f"POSTGRES_URL must be a postgresql:// URL, got {url!r}"
        )
    if not parts.path.strip("/"):
        raise PostgresIsolationRefusedError(
            f"POSTGRES_URL is {url!r}, which names no database; libpq would "
            "resolve it to the connecting role's default. Name the database "
            "explicitly (e.g. postgresql://localhost:5432/postgres)."
        )
    return url


def _url_with_search_path(url: str, schema: str) -> str:
    """``url`` with libpq's ``options=-c search_path=<schema>`` appended.

    Every connection opened from the result resolves unqualified table names
    inside the test schema. An existing ``options`` value is extended, not
    replaced, and the appended directive comes last, so it wins over a
    ``search_path`` the caller already set.
    """
    parts = urlsplit(url)
    directive = f"-c search_path={schema}"
    merged: list[tuple[str, str]] = []
    seen = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "options":
            merged.append((key, f"{value} {directive}".strip()))
            seen = True
        else:
            merged.append((key, value))
    if not seen:
        merged.append(("options", directive))
    new_query = urlencode(merged, quote_via=quote)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def _psycopg_importable() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


def _require_postgres() -> str:
    """The validated ``POSTGRES_URL``, or ``pytest.skip`` with a reason that
    names what is missing. A refusal (db-less URL) is an error, not a skip --
    and it is checked first, because it needs no driver: a missing ``psycopg``
    must not turn a refusal into a quiet skip (#763 N1)."""
    url = os.environ.get("POSTGRES_URL", "").strip()
    if url:
        _check_postgres_url(url)
    if not _psycopg_importable():
        pytest.skip(
            "psycopg is not installed (pip install 'psycopg[binary]'); "
            "Postgres conformance leg skipped"
        )
    if not url:
        pytest.skip("POSTGRES_URL is unset; Postgres conformance leg skipped")
    return url


@dataclass
class PostgresTestSchema:
    """The per-session Postgres namespace the conformance harness owns.

    ``url`` is the validated ``POSTGRES_URL`` (no search_path); ``dsn`` is the
    same URL pinned to this schema. ``connect()`` opens a fresh autocommit
    admin connection for a test that wants to look at the schema directly.
    Every method that issues SQL checks the name first, so a refused name
    never opens a connection.
    """

    name: str
    url: str
    _conn: Any = field(default=None, repr=False)
    _backend: Any = field(default=None, repr=False)

    @property
    def dsn(self) -> str:
        return _url_with_search_path(self.url, self.name)

    def connect(self) -> Any:
        import psycopg

        return psycopg.connect(self.url, autocommit=True)

    def create(self) -> None:
        _check_schema_name(self.name)
        from psycopg import sql

        conn = self.connect()
        try:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.name)))
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def truncate_all(self) -> list[str]:
        """``TRUNCATE`` every table in the schema (the per-test FLUSHDB
        mirror), in one statement and without ``CASCADE``. Returns the table
        names it truncated; empty while nothing has created a table."""
        _check_schema_name(self.name)
        from psycopg import sql

        rows = self._conn.execute(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = %s "
            "ORDER BY tablename",
            (self.name,),
        ).fetchall()
        tables = [row[0] for row in rows]
        if tables:
            self._conn.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
                    sql.SQL(", ").join(
                        sql.Identifier(self.name, table) for table in tables
                    )
                )
            )
        return tables

    def drop_tables(self) -> list[str]:
        """``DROP TABLE`` every table in the schema -- the per-test reset the
        ``backend`` fixture runs (#759 M1b) -- in one statement and without
        ``CASCADE``, so a foreign key reaching in from outside the schema makes
        it fail rather than drop a table the harness does not own. Returns the
        table names it dropped."""
        _check_schema_name(self.name)
        from psycopg import sql

        rows = self._conn.execute(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = %s "
            "ORDER BY tablename",
            (self.name,),
        ).fetchall()
        tables = [row[0] for row in rows]
        if tables:
            self._conn.execute(
                sql.SQL("DROP TABLE {}").format(
                    sql.SQL(", ").join(
                        sql.Identifier(self.name, table) for table in tables
                    )
                )
            )
        return tables

    def backend(self) -> Any:
        """The session's ``PostgresBackend``, bound to this schema: one per
        session, so its connection pool is reused across tests."""
        _check_schema_name(self.name)
        if self._backend is None:
            from popoto.backends.postgres import PostgresBackend

            self._backend = PostgresBackend(dsn=self.url, schema=self.name)
        return self._backend

    def drop(self) -> None:
        """``DROP SCHEMA ... CASCADE`` on this schema, then close the admin
        connection.

        ``CASCADE`` follows dependencies, not schema boundaries: an object in
        *another* schema that depends on a table in this one goes too. Verified
        on PostgreSQL 18.6, a view elsewhere selecting from a test table is
        dropped, and a foreign key elsewhere referencing one loses that
        constraint while its table and rows survive. Objects that do not
        depend on a test table are untouched. Nothing outside the harness
        should depend on a ``popoto_test_<hex>`` table, so in practice this
        only matters to a test that builds such a dependency on purpose.
        """
        _check_schema_name(self.name)
        from psycopg import sql

        if self._backend is not None:
            self._backend.close()
            self._backend = None
        try:
            self._conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.name)
                )
            )
        finally:
            self._conn.close()
            self._conn = None


class _ConformanceFixtures:
    """The conformance fixtures, registered as a plugin only in a session that
    opted in (``pytest_configure``).

    They live on a class rather than at module scope because pytest registers
    every fixture it finds on a plugin module unconditionally: at module scope,
    ``backend`` would exist in every downstream session, a project's own
    ``backend`` would be silently shadowed-or-shadowing, and a test requesting
    a missing ``backend`` would get a Redis descriptor instead of pytest's
    "fixture 'backend' not found" (#763 N3).
    """

    @pytest.fixture(scope="session")
    def popoto_postgres_schema(self) -> Any:
        """One ``popoto_test_<hex>`` schema for the session: created here,
        dropped with ``CASCADE`` at teardown (see :meth:`PostgresTestSchema.drop`
        for what that reaches outside the schema). Skips (never fails) when
        ``psycopg`` or ``POSTGRES_URL`` is absent; raises
        :class:`PostgresIsolationRefusedError` on a db-less URL before
        connecting."""
        url = _require_postgres()
        schema = PostgresTestSchema(
            name=f"{POSTGRES_TEST_SCHEMA_PREFIX}{uuid.uuid4().hex}", url=url
        )
        schema.create()
        logger.debug(
            "Popoto conformance harness created Postgres schema %s", schema.name
        )
        try:
            yield schema
        finally:
            schema.drop()
            logger.debug(
                "Popoto conformance harness dropped Postgres schema %s", schema.name
            )

    @pytest.fixture
    def backend(self, request: Any) -> Any:
        """The conformance leg under test, as a :class:`ConformanceBackend`.

        Parametrised by :func:`pytest_generate_tests` for ``conformance``
        tests; unparametrised (Redis) otherwise. In an opted-in session each
        leg binds its backend as the process default for the test and restores
        the previous binding on teardown (#759 M1b), so module-level models
        need no re-declaration. The Postgres leg skips when the test is
        ``redis_only`` and when ``psycopg`` or ``POSTGRES_URL`` is missing;
        otherwise it drops every table in the session schema (the FLUSHDB
        mirror) and binds the session's ``PostgresBackend``. A model with an
        explicit ``Meta.backend = "postgres"`` resolves to that same instance
        for the test.
        """
        name = getattr(request, "param", "redis")
        if name == "redis":
            # In an opted-in session, bind the leg's backend as the process
            # default for the test and restore the previous binding (the
            # session pin) on teardown. set_backend() discards memoised
            # bind() state both ways, so module-level models rebind lazily.
            if getattr(request.config, "_popoto_opted_in", False):
                from popoto.backends import set_backend

                previous = set_backend("redis")
                request.addfinalizer(lambda: set_backend(previous))
            return ConformanceBackend(name="redis")
        if name != "postgres":  # unreachable through _resolve_conformance_backends
            raise ValueError(f"unknown conformance backend {name!r}")
        redis_only = request.node.get_closest_marker("redis_only")
        if redis_only is not None:
            pytest.skip(f"redis_only: {redis_only.kwargs.get('reason', '')}")
        _require_postgres()
        schema = request.getfixturevalue("popoto_postgres_schema")
        schema.drop_tables()
        instance = schema.backend()
        instance.forget_tables()
        from popoto.backends import _swap_instance, set_backend

        # Since #816 a Postgres-named ``set_backend`` instance also serves
        # ``Meta.backend = "postgres"`` models on its own, so the
        # ``_swap_instance`` call is now redundant for them. It is kept as a
        # harmless redundancy (both point at the same instance) until after
        # 1.10.0 ships; other tests use ``_swap_instance`` directly.
        previous = set_backend(instance)
        previous_instance = _swap_instance("postgres", instance)

        def _restore() -> None:
            _swap_instance("postgres", previous_instance)
            set_backend(previous)

        request.addfinalizer(_restore)
        return ConformanceBackend(
            name="postgres", dsn=schema.dsn, schema=schema.name, instance=instance
        )

    @pytest.fixture(autouse=True)
    def _popoto_conformance_leg(self, request: Any) -> None:
        """Bind a conformance test's leg *before* the module's own fixtures.

        A plugin autouse fixture is set up ahead of a test module's autouse
        fixtures, so resolving ``backend`` here means a module fixture that
        seeds rows (``setup_and_teardown`` style) already writes to the leg's
        backend. Without it, the seed would land on Redis and the Postgres leg
        would test an empty table. Acts only on a test this plugin
        parametrised (``conformance`` + the plugin's ``backend``).
        """
        callspec = getattr(request.node, "callspec", None)
        if callspec is None or "backend" not in callspec.params:
            return
        if request.node.get_closest_marker("conformance") is None:
            return
        request.getfixturevalue("backend")

    @pytest.fixture
    def backend_is_redis(self, backend: ConformanceBackend) -> bool:
        """``True`` on the Redis leg -- for an assertion that must branch
        rather than skip (``redis_only`` is the marker for the skip shape)."""
        return backend.is_redis
