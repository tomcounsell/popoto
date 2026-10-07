"""The Postgres backend: plain models on typed tables (#759 M1b).

``PostgresBackend`` implements protocol groups A-C (lifecycle, records, query)
natively -- one typed table per model (:mod:`.schema`), ``WHERE`` compiled
from the query (:mod:`.plan`) -- with no Redis structure emulated and no
msgpack stored. Since M2a, ``touch``, ``update_confidence``, ``rank_decayed``,
``rank_composite`` and the memory ``field_call`` adapters come from
:mod:`.memory`; the rest of groups D-H raise :class:`BackendCapabilityError`
until their milestone.

Selection (plan §4): ``Meta.backend = "postgres"`` on a model, or
``POPOTO_BACKEND=postgres`` for the process, with the DSN from
``POPOTO_POSTGRES_URL`` (or a ``PostgresBackend(dsn=…)`` handed to
``popoto.backends.set_backend``). ``POSTGRES_URL`` and ``DATABASE_URL`` are
never read. ``POPOTO_POSTGRES_SCHEMA`` names the schema (default ``popoto``);
``POPOTO_SCHEMA_AUTO=0`` turns off automatic create/additive DDL.
``POPOTO_POSTGRES_MAINTENANCE_URL`` (or ``PostgresBackend(...,
maintenance_dsn=…)``) optionally names a second DSN to the *same* database --
a direct or session-mode connection past a transaction-mode PgBouncer -- for
everything that needs a session (#800): ``rebuild_indexes()``'s ``REINDEX``,
``clean_indexes()``'s ``DROP INDEX CONCURRENTLY``, first-use DDL, and the
shared ``LISTEN`` session. Unset, all of it uses the main DSN, as before.

Nothing here touches the network until a model's first operation (``bind``
itself only compiles the table spec; the server and table checks run inside
that first save or query, so an outage there is charged to it), and
``psycopg`` is imported only then: ``import popoto`` and defining a
``Meta.backend = "postgres"`` model need neither the driver nor the server.

Topology (plan §3): one ``psycopg_pool.ConnectionPool`` per (DSN, pid),
``max_size`` ``Defaults.PG_POOL_MAX_SIZE``. No session state is ever set on
a pooled connection: every autocommit operation is one simple-query message
``SET LOCAL statement_timeout = N; <statement>``, which Postgres runs as one
implicit transaction -- atomic, one round trip, and safe behind PgBouncer in
transaction mode. Advisory locks are ``pg_advisory_xact_lock`` only. The one
exception is ``rebuild_indexes()``'s ``REINDEX``/``DROP INDEX
CONCURRENTLY``, which cannot run in any transaction block: it sets a session
``lock_timeout``/``statement_timeout`` on a dedicated connection it opens and
closes (:meth:`PostgresBackend._maintenance_connection`) -- on the
maintenance DSN when one is configured (:meth:`PostgresBackend._session_dsn`).

Outage contract (``[PG-only]``): an unreachable server, a connect timeout or
a statement timeout raises :class:`BackendUnavailableError`; the backend's
:attr:`PostgresBackend.health` record counts failures and dropped writes, and
the outage is logged at ERROR once per ``Defaults.PG_OUTAGE_LOG_WINDOW_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime
import logging
import os
import random
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Sequence, Union

from ...fields.constants import Defaults
from ..types import (
    BackendBusyError,
    BackendCapabilityError,
    BackendError,
    BackendRetryableError,
    BackendUnavailableError,
    Capabilities,
    Expiry,
    ModelSpec,
    QueryPlan,
    RecordId,
    Row,
    SaveOutcome,
    UnitOfWork,
)
from ..planning import has_filters
from .codec import decode_json, encode_json_element
from .events import EventsMixin, stream_deletes
from .geo import geo_save_values, resolve_geo
from .graph import GraphMixin, graph_delete_lock_sql, graph_delete_sql
from .longtail import LongtailOpsMixin, cyclic_save_parts
from .maintain import MaintainOpsMixin
from .recipes import RecipeOpsMixin
from .memory import NOT_HANDLED, PostgresMemoryOps
from .plan import (
    non_null_fields,
    render_order,
    render_where,
    to_column_value,
    to_db_value,
)
from .schema import (
    POPOTO_SCHEMA_TABLE,
    RELATIONSHIP_KINDS,
    SCHEMA_FORMAT_VERSION,
    UTCOFF_SUFFIX,
    TableSpec,
    compile_table,
    ensure_table,
    quote_ident,
)
from .search import (
    SearchMixin,
    prepare_save,
    record_lock_keys,
    record_lock_sql,
    require_extensions,
)
from .ttl import (
    Reaper,
    expiry_sql,
    live_sql,
    purge_expired_sql,
    reap,
    ttl_remaining,
)
from .validity import (
    ensure_validity_tables,
    pointer_table,
    refuse_valid_from_conflict,
    save_parts,
    validity_field_names,
)

__all__ = [
    "POSTGRES_URL_ENV",
    "POSTGRES_SCHEMA_ENV",
    "MAINTENANCE_URL_ENV",
    "MaintenanceDsnMismatchError",
    "MIN_SERVER_VERSION_NUM",
    "Health",
    "PostgresBackend",
    "PostgresUnitOfWork",
    "SecondConnectionError",
    "backend_from_env",
]

logger = logging.getLogger("POPOTO.postgres")

POSTGRES_URL_ENV = "POPOTO_POSTGRES_URL"
POSTGRES_SCHEMA_ENV = "POPOTO_POSTGRES_SCHEMA"
MAINTENANCE_URL_ENV = "POPOTO_POSTGRES_MAINTENANCE_URL"
"""An optional second DSN, to the same database as ``POPOTO_POSTGRES_URL``,
for the work that needs a real session (#800): ``REINDEX``/``DROP INDEX
CONCURRENTLY`` with their session timeouts, first-use DDL, and the shared
``LISTEN`` session (unless ``POPOTO_POSTGRES_LISTEN_URL`` names its own).
Point it at the server directly, or at a session-mode pool, when the main
DSN goes through PgBouncer in transaction mode. Read only by
:func:`backend_from_env`, alongside the main DSN it pairs with."""
SCHEMA_AUTO_ENV = "POPOTO_SCHEMA_AUTO"
DEFAULT_SCHEMA = "popoto"
MIN_SERVER_VERSION_NUM = 180000
"""Architect decision 2: Postgres 18 is the floor; no 16/17 fallback."""

POSTGRES_CAPABILITIES_GROUPS = frozenset("ABC")

#: Debug guard (#776): when true, checking out a second pooled connection
#: while a ``transaction()`` (or a ``popoto.batch()`` holding Postgres
#: writes) is open in the same task or thread raises
#: :class:`SecondConnectionError` instead of waiting for one. Off in
#: production; ``tests/conftest.py`` turns it on for the whole suite.
#:
#: Why it exists: popoto's own code once read through a second connection on
#: behalf of a unit that already held one (``pre_save``'s uniqueness read).
#: With every pool connection held by an open unit that read could never be
#: served, and it saw committed state, not the unit's own writes. Every read
#: popoto issues for a unit now runs on the unit's connection; the guard is
#: how the suite proves no path regressed. First-use DDL, which must commit
#: outside any caller's transaction, takes no pooled connection either: its
#: catalog check runs on the open unit's connection, and the DDL itself, when
#: there is any, on a dedicated connection outside the pool
#: (:meth:`PostgresBackend._ddl_connection`). Only a test that *as a user*
#: reads or writes outside the unit it has open declares
#: :meth:`PostgresBackend.second_connection_ok`.
STRICT_UNIT_CONNECTION = False


class SecondConnectionError(AssertionError):
    """A pooled connection was checked out while this task or thread has a
    Postgres unit of work open (``STRICT_UNIT_CONNECTION``, #776)."""


class MaintenanceDsnMismatchError(BackendError, ValueError):
    """The maintenance DSN (``POPOTO_POSTGRES_MAINTENANCE_URL`` or
    ``maintenance_dsn=``) reaches a different database than the main DSN
    (#800). Raised before any statement runs on it, so no DDL or ``REINDEX``
    ever lands on the wrong database. A configuration error, not an outage:
    the backend's ``health`` is untouched. The message names hosts and
    database names, never a password."""


def backend_from_env() -> "PostgresBackend":
    """The process backend for ``POPOTO_BACKEND=postgres`` /
    ``Meta.backend = "postgres"``: DSN from ``POPOTO_POSTGRES_URL`` only."""
    dsn = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not dsn:
        raise BackendUnavailableError(
            f"the Postgres backend is selected but {POSTGRES_URL_ENV} is not set; "
            f"set {POSTGRES_URL_ENV}=postgresql://host:5432/dbname (POSTGRES_URL "
            "and DATABASE_URL are deliberately not read)"
        )
    schema = os.environ.get(POSTGRES_SCHEMA_ENV, "").strip() or DEFAULT_SCHEMA
    maintenance = os.environ.get(MAINTENANCE_URL_ENV, "").strip() or None
    return PostgresBackend(dsn=dsn, schema=schema, maintenance_dsn=maintenance)


def _describe_dsn(dsn: str) -> str:
    """``host=… port=… dbname=… user=…`` for an error message: never the
    password, nor any other option a DSN may carry."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - unparseable: say nothing about it
        return "<unparseable DSN>"
    shown = [
        f"{key}={parts[key]}"
        for key in ("host", "hostaddr", "port", "dbname", "user")
        if parts.get(key)
    ]
    return " ".join(shown) or "<libpq defaults>"


def _schema_auto() -> bool:
    return os.environ.get(SCHEMA_AUTO_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# -- pool per (DSN, pid) -------------------------------------------------------

_pools: dict[tuple[str, int], Any] = {}
_pools_lock = threading.Lock()


def _import_psycopg() -> Any:
    try:
        import psycopg
        import psycopg_pool  # noqa: F401
    except ImportError as exc:
        raise BackendUnavailableError(
            "the Postgres backend needs psycopg and psycopg_pool: "
            "pip install 'popoto[postgres]'"
        ) from exc
    return psycopg


def _rollback_errors(psycopg: Any) -> tuple[type[BaseException], ...]:
    """The errors that mean "this transaction was rolled back by a concurrent
    one; run it again": SQLSTATE class 40's deadlock and serialization
    failures. psycopg 3 maps 40P01 and 40001 to ``DeadlockDetected`` and
    ``SerializationFailure``, which are *not* subclasses of its
    ``TransactionRollback`` (40000), so catching that class alone missed
    every real deadlock (#759 M2a)."""
    errors = psycopg.errors
    return (
        errors.TransactionRollback,
        errors.SerializationFailure,
        errors.DeadlockDetected,
    )


def _retryable(exc: BaseException, attempts: int = 1) -> BackendRetryableError:
    """The popoto type for a rolled-back transaction (plan §6, TD-2): a
    deadlock (40P01) or serialization failure (40001, 40000) reaches the
    caller as :class:`BackendRetryableError`, raised ``from`` the driver
    error, never as raw psycopg (#759 M2a, review B1). 40003
    (``StatementCompletionUnknown``) is deliberately not in
    :func:`_rollback_errors`: the statement may have committed, so it follows
    :meth:`PostgresBackend._may_retry_broken` (#769)."""
    sqlstate = getattr(exc, "sqlstate", None) or "40000"
    times = "" if attempts <= 1 else f" {attempts} times"
    return BackendRetryableError(
        f"rolled back by a concurrent transaction{times} "
        f"(SQLSTATE {sqlstate}: {exc}); retry it"
    )


# -- the async bridge (#759 M5) -----------------------------------------------

_async_bridge: Any = None
"""Installed by :mod:`.aio` on import. While sync backend code runs inside
one of its bridge greenlets (an ``async_*`` call on a Postgres model), the
helpers below route that code's I/O onto the event loop: the pool is the
loop's pool of ``AsyncConnection`` objects, a retry back-off is
``asyncio.sleep``, and a blocking provider call goes to a worker thread.
Everywhere else they are exactly what they were."""


def _set_async_bridge(bridge: Any) -> None:
    global _async_bridge
    _async_bridge = bridge


def _bridged() -> Any:
    bridge = _async_bridge
    if bridge is not None and bridge.active():
        return bridge
    return None


def _sleep(seconds: float) -> None:
    """A retry back-off: ``time.sleep``, or ``asyncio.sleep`` in the bridge."""
    bridge = _bridged()
    if bridge is not None:
        bridge.sleep(seconds)
    else:
        time.sleep(seconds)


def _blocking(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call a blocking non-Postgres function (an embedding provider): in
    place, or in a worker thread when inside the bridge, so it never blocks
    the event loop."""
    bridge = _bridged()
    if bridge is not None:
        return bridge.blocking(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def _pool_for(dsn: str) -> Any:
    """The process's pool for ``dsn``, created lazily (and again after a
    fork: a child never reuses the parent's sockets). Inside the async
    bridge, the running loop's pool instead (:mod:`.aio`)."""
    bridge = _bridged()
    if bridge is not None:
        return bridge.pool(dsn)
    key = (dsn, os.getpid())
    pool = _pools.get(key)
    if pool is not None:
        return pool
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            psycopg = _import_psycopg()
            from psycopg_pool import ConnectionPool

            timeout = float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=int(Defaults.PG_POOL_MAX_SIZE),
                timeout=timeout,
                # Validate each connection on checkout (one empty-query round
                # trip): a server restart, failover or idle reaper leaves the
                # pool holding dead sockets, and without this every one of
                # them would fail one call on a healthy server.
                check=ConnectionPool.check_connection,
                open=True,
                kwargs={
                    "autocommit": True,
                    # PgBouncer transaction mode: no server-side prepared
                    # statements, which are session state.
                    "prepare_threshold": None,
                    # Client-side binding lets one message carry
                    # "SET LOCAL ...; <statement>" with parameters.
                    "cursor_factory": psycopg.ClientCursor,
                    "connect_timeout": max(1, int(round(timeout))),
                },
                name=f"popoto-{os.getpid()}",
            )
            _pools[key] = pool
    return pool


# The asyncio tasks that held an open ``transaction()`` (or batch) of some
# Postgres backend when the current task was created, outermost first, as
# weak references (#784 review). Set in a task's *own* context -- by
# ``AsyncPostgresBackend.run`` after a call leaves the task with an open
# unit, never inside the bridge, whose greenlet runs in a per-call copy -- so
# a child task made afterwards (``gather``, ``create_task``) inherits it in
# its context copy and can see its ancestors' lock scopes, while sibling
# tasks, each in its own copy, never see each other's.
_ancestor_scopes: contextvars.ContextVar[tuple[weakref.ref[Any], ...]] = (
    contextvars.ContextVar("popoto_pg_ancestor_scopes", default=())
)


# Connections each sync pool has checked out to a caller right now, by
# ``id(pool)``: what tells a checkout timeout under contention (every
# connection is out) from one under an outage (#784 review).
_checked_out: dict[int, int] = {}
_checked_out_lock = threading.Lock()


@contextlib.contextmanager
def _checkout(pool: Any, timeout: Optional[float] = None) -> Iterator[Any]:
    """``pool.connection(timeout=...)``, marking a checkout timeout that is
    contention rather than an outage (``exc.popoto_busy = True``, then
    :func:`_busy` maps it to :class:`BackendBusyError`).

    The async bridge's pool marks its own timeouts: it opens connections
    inline, so its only timeout is the wait for a free slot. The sync
    ``psycopg_pool`` raises the same ``PoolTimeout`` for "every connection is
    in use" and for "the server is down and no connection could be made",
    so here a timeout is busy only when every connection the pool may hold
    is checked out to a caller at that moment; otherwise it stays an
    outage."""
    if _bridged() is not None:
        with pool.connection(timeout=timeout) as conn:
            yield conn
        return
    key = id(pool)
    with contextlib.ExitStack() as stack:
        try:
            conn = stack.enter_context(pool.connection(timeout=timeout))
        except Exception as exc:
            if type(exc).__name__ == "PoolTimeout":
                with _checked_out_lock:
                    out = _checked_out.get(key, 0)
                if out >= int(getattr(pool, "max_size", out + 1)):
                    exc.popoto_busy = True  # type: ignore[attr-defined]
            raise
        with _checked_out_lock:
            _checked_out[key] = _checked_out.get(key, 0) + 1
        try:
            yield conn
        finally:
            with _checked_out_lock:
                left = _checked_out.get(key, 1) - 1
                if left > 0:
                    _checked_out[key] = left
                else:
                    _checked_out.pop(key, None)


def _busy(exc: BaseException) -> Optional[BackendBusyError]:
    """The :class:`BackendBusyError` for a checkout timeout that found every
    connection in use, else ``None``."""
    if not getattr(exc, "popoto_busy", False):
        return None
    return BackendBusyError(
        f"no pooled Postgres connection became free in time ({exc}): every "
        f"connection (Defaults.PG_POOL_MAX_SIZE = {int(Defaults.PG_POOL_MAX_SIZE)}) "
        "is checked out to a caller -- contention, not an outage -- and "
        "nothing was sent, so retry it"
    )


def close_pools() -> None:
    """Close every pool this process opened (tests, interpreter shutdown),
    the async bridge's per-loop pools included."""
    if _async_bridge is not None:
        _async_bridge.close_all()
    with _pools_lock:
        for (dsn, pid), pool in list(_pools.items()):
            if pid == os.getpid():
                pool.close()
            del _pools[(dsn, pid)]


# -- health -------------------------------------------------------------------


@dataclass
class Health:
    """The backend health record (plan §1.1, ``[PG-only]``)."""

    ok: bool = True
    last_ok_at: Optional[float] = None
    consecutive_failures: int = 0
    dropped_writes: int = 0
    last_error: Optional[str] = None
    _last_logged_at: Optional[float] = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "last_ok_at": self.last_ok_at,
            "consecutive_failures": self.consecutive_failures,
            "dropped_writes": self.dropped_writes,
            "last_error": self.last_error,
        }


# -- unit of work -------------------------------------------------------------


class PostgresUnitOfWork(UnitOfWork):
    """A Postgres transaction handle from :meth:`PostgresBackend.transaction`.

    Pass it wherever a ``pipeline=`` is accepted on a Postgres-bound model:
    writes execute immediately on the transaction's connection, and the whole
    unit commits or rolls back together when the ``with`` block exits.
    ``bool(uow)`` is always ``True`` (TD-10).
    """

    __slots__ = (
        "conn",
        "_after_commit",
        "_before_commit",
        "_stream_appends",
        "reap",
        "locked",
    )

    def __init__(self, conn: Any) -> None:
        super().__init__(None, backend="postgres")
        self.conn = conn
        self._after_commit: list[Callable[[], Any]] = []
        self._before_commit: list[Callable[[], Any]] = []
        # Stream appends (#759 M5), keyed by stream: run at COMMIT, sorted.
        self._stream_appends: list[tuple[str, Callable[[], Any]]] = []
        # Meta.ttl tables written in this unit (M5): reaped after it commits.
        self.reap: dict[str, TableSpec] = {}
        # Record-key locks this unit holds (#783): another open unit -- or an
        # autocommit statement -- on the same thread that asks for one of
        # them would wait on its own thread forever.
        self.locked: set[str] = set()

    def after_commit(self, callback: Callable[[], Any]) -> None:
        """Run ``callback`` once this transaction has committed; drop it if
        the transaction rolls back. ``WriteFilterMixin``'s priority tag runs
        this way (#759 M4b B2); ``EventStreamMixin``'s entry no longer does --
        since M5 it is appended inside the transaction (:meth:`defer_stream_append`).
        Callbacks run in registration order, after ``COMMIT`` returns
        and the connection is back in the pool; one that raises is logged and
        the rest still run, because the transaction has already committed."""
        self._after_commit.append(callback)

    def before_commit(self, callback: Callable[[], Any]) -> None:
        """Run ``callback`` inside this transaction, just before its
        ``COMMIT``; an exception from it rolls the whole unit back. Callbacks
        run in registration order; one registered while they run runs too.
        A save's ``EventStreamMixin`` entry is not queued here but with
        :meth:`defer_stream_append`, which runs after these, in stream-key
        order (#759 M5)."""
        self._before_commit.append(callback)

    def defer_stream_append(self, stream: str, callback: Callable[[], Any]) -> None:
        """Queue one stream append (``callback`` appends to ``stream``) for
        this transaction's ``COMMIT`` (#759 M5). Appends run after the
        :meth:`before_commit` callbacks, **sorted by stream key** (stably, so
        one stream's entries keep the order they were queued in): every
        transaction then takes its ``popoto_stream`` row locks in one global
        order, after all of its record locks, and two transactions that
        append to the same streams in opposite orders cannot deadlock."""
        self._stream_appends.append((stream, callback))

    def _run_before_commit(self) -> None:
        while self._before_commit or self._stream_appends:
            while self._before_commit:
                self._before_commit.pop(0)()
            pending, self._stream_appends = self._stream_appends, []
            pending.sort(key=lambda queued: queued[0])
            for _stream, callback in pending:
                callback()

    def _run_after_commit(self) -> None:
        callbacks, self._after_commit = self._after_commit, []
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning("after-commit callback %r failed: %s", callback, exc)

    @contextlib.contextmanager
    def savepoint(self) -> Iterator["PostgresUnitOfWork"]:
        """A ``SAVEPOINT`` inside this unit (sync units only): when the block
        raises, every statement it ran is rolled back **and** every callback
        it queued on this unit -- stream appends, ``before_commit`` and
        ``after_commit`` callbacks, the TTL tables to reap -- is dropped, so
        the unit commits nothing of it. Without the second half a save rolled
        back to a savepoint would still append its stream entry at ``COMMIT``
        (#756: ``import_records(uow=...)`` lands each record this way). The
        exception propagates; the unit stays usable. The record-key locks the
        block took stay in :attr:`locked` (the server may keep them until the
        unit ends; over-reporting only refuses more self-waits)."""
        marks = (
            len(self._after_commit),
            len(self._before_commit),
            len(self._stream_appends),
        )
        reap = dict(self.reap)
        try:
            with self.conn.transaction():
                yield self
        except BaseException:
            del self._after_commit[marks[0] :]
            del self._before_commit[marks[1] :]
            del self._stream_appends[marks[2] :]
            self.reap = reap
            raise

    @property
    def is_redis_pipeline(self) -> bool:
        return False

    def commit(self) -> list[Any]:
        """Nothing to flush: statements ran as they were issued, and the
        ``transaction()`` context commits."""
        return []


def _pg_uow(uow: Optional[UnitOfWork]) -> Optional[PostgresUnitOfWork]:
    return uow if isinstance(uow, PostgresUnitOfWork) else None


def _and_live(ts: TableSpec) -> str:
    """`` AND <the TTL read filter>`` for a ``Meta.ttl`` model (M5): a write
    addressed to one record (``increment``, a capped push) treats an expired
    row as the record that does not exist. ``""`` otherwise."""
    live = live_sql(ts)
    return f" AND {live}" if live else ""


def _wrap_capped_lists(obj: Any, ts: TableSpec) -> None:
    """Re-wrap each ``ListField(max_length=N)`` value in a
    ``CappedListProxy`` after a save, as ``ListField.on_save`` does on Redis,
    so ``obj.<field>.push()`` keeps working on the saved instance."""
    if not ts.capped_fields:
        return
    from ...fields.shortcuts import CappedListProxy

    for name in ts.capped_fields:
        value = getattr(obj, name, None)
        if isinstance(value, CappedListProxy):
            value._model_instance = obj
            continue
        setattr(
            obj,
            name,
            CappedListProxy(
                data=value or [],
                model_instance=obj,
                field_name=name,
                max_length=obj._meta.fields[name].max_length,
            ),
        )


# -- the backend --------------------------------------------------------------


class PostgresBackend(
    SearchMixin,
    PostgresMemoryOps,
    GraphMixin,
    RecipeOpsMixin,
    EventsMixin,
    LongtailOpsMixin,
    MaintainOpsMixin,
):
    """The Postgres implementation of :class:`popoto.backends.Backend`.

    Search -- ``keyword_search``, ``vector_search``, ``membership_*`` and the
    ``[PG-only]`` ``recall`` -- comes from :class:`.search.SearchMixin`
    (#759 M2b). Groups D/E's ranking and memory state (``touch``,
    ``update_confidence``, ``rank_decayed``, ``rank_composite``) come from
    :class:`~.memory.PostgresMemoryOps` (#759 M2a); the co-occurrence graph
    (``graph_update``, ``graph_expand``) from :class:`~.graph.GraphMixin`, and
    the recipe-layer ``field_call`` adapters (``idle_seconds``, a sorted
    field's partition reads, counters, tombstones, the question queue) from
    :class:`~.recipes.RecipeOpsMixin` (#759 M4); ``CyclicDecayField``,
    ``TDValueField`` and ``PredictionLedgerMixin`` from
    :class:`~.longtail.LongtailOpsMixin` (#759 M5); ``maintain``, ``raw_update``
    and the record-column transfer state from
    :class:`~.maintain.MaintainOpsMixin` (#759 M5)."""

    name = "postgres"

    def __init__(
        self,
        dsn: str,
        schema: str = DEFAULT_SCHEMA,
        maintenance_dsn: Optional[str] = None,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        # #800: the DSN for work that needs a session (REINDEX, first-use
        # DDL, LISTEN). None -- unset, blank, or the main DSN itself -- means
        # the main DSN, exactly as before the setting existed.
        maintenance = (maintenance_dsn or "").strip() or None
        self.maintenance_dsn: Optional[str] = (
            None if maintenance == dsn else maintenance
        )
        self._maintenance_verified = False
        self.health = Health()
        self._tables: dict[str, tuple[ModelSpec, TableSpec]] = {}
        self._server_checked = False
        self._lock = threading.RLock()
        self._intent = threading.local()
        self._reaper = Reaper()
        # The held-record-lock registry (#783), keyed by the asyncio task
        # when there is one, else the thread (see _lock_scope).
        self._open: weakref.WeakKeyDictionary[Any, list[PostgresUnitOfWork]] = (
            weakref.WeakKeyDictionary()
        )
        # Scopes inside second_connection_ok(), with their nesting depth
        # (#776): the STRICT_UNIT_CONNECTION guard lets their checkouts pass.
        self._second_ok: weakref.WeakKeyDictionary[Any, int] = (
            weakref.WeakKeyDictionary()
        )
        # Units whose connection a scope's otherwise-autocommit *reads* run on
        # (reads_on, #776): model-level reads issued on behalf of a write that
        # was handed a unit (``Model.exists``, ``query.get``).
        self._read_units: weakref.WeakKeyDictionary[Any, list[PostgresUnitOfWork]] = (
            weakref.WeakKeyDictionary()
        )

    def __repr__(self) -> str:
        return f"<PostgresBackend schema={self.schema!r}>"

    # -- plumbing -------------------------------------------------------------

    @contextlib.contextmanager
    def _write_intent(self, write: bool) -> Iterator[None]:
        """Classify every failure inside the block by the *triggering*
        operation: the lazy table/server check a save runs first reads, but a
        save that fails there is still a dropped write."""
        previous = getattr(self._intent, "write", False)
        self._intent.write = previous or write
        try:
            yield
        finally:
            self._intent.write = previous

    def _fail(self, exc: BaseException, *, write: bool) -> BackendUnavailableError:
        h = self.health
        h.ok = False
        h.consecutive_failures += 1
        h.last_error = f"{type(exc).__name__}: {exc}"
        if write or getattr(self._intent, "write", False):
            h.dropped_writes += 1
        now = time.monotonic()
        window = float(Defaults.PG_OUTAGE_LOG_WINDOW_SECONDS)
        if h._last_logged_at is None or now - h._last_logged_at >= window:
            h._last_logged_at = now
            logger.error(
                "popoto Postgres backend unavailable (%s); %d consecutive "
                "failure(s), %d dropped write(s). Logged once per %.0fs.",
                h.last_error,
                h.consecutive_failures,
                h.dropped_writes,
                window,
            )
        return BackendUnavailableError(f"Postgres is unavailable: {h.last_error}")

    def _ok(self) -> None:
        h = self.health
        if not h.ok:
            logger.warning(
                "popoto Postgres backend recovered after %d failure(s)",
                h.consecutive_failures,
            )
        h.ok = True
        h.consecutive_failures = 0
        h.last_ok_at = time.time()
        h._last_logged_at = None

    @contextlib.contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[Any]:
        psycopg = _import_psycopg()
        self._guard_second_checkout("a transaction or DDL connection")
        try:
            pool = _pool_for(self.dsn)
            with _checkout(pool) as conn:
                yield conn
        except _rollback_errors(psycopg) as exc:
            # Raised by the block (a caller-owned ``transaction()``, including
            # its COMMIT) or by DDL: the work was rolled back, not lost to an
            # outage. Not retried here -- the caller owns the transaction.
            raise _retryable(exc) from exc
        except psycopg.OperationalError as exc:
            busy = _busy(exc)
            if busy is not None:  # contention: health untouched (#784 review)
                raise busy from exc
            raise self._fail(exc, write=write) from exc

    def _record_locked(
        self, ts: TableSpec, pks: Sequence[str], sql: str, params: Sequence[Any]
    ) -> tuple[str, list[Any]]:
        """``sql`` preceded by the record-key advisory locks of ``pks``
        (:func:`~.search.record_lock_sql`), in the same message: no extra
        round trip.

        Every statement that writes (and so row-locks) a record goes through
        this, so the whole backend has one lock order (plan §6, TD-2): any
        ``(model, field)`` lock, then the record-key locks in ``_pk`` byte
        order, then the row locks in ``_pk`` order. A transaction that row-
        locked a record (``update_confidence``) and then saves it already
        holds the record's key lock, so it can no longer cross a concurrent
        save that took the key lock and waits on the row (#774 review)."""
        lock_sql, lock_params = record_lock_sql(ts, pks)
        return lock_sql + sql, list(lock_params) + list(params)

    @staticmethod
    def _lock_scope() -> Any:
        """Who waits when a statement waits: the running asyncio task, else
        the thread (#784 review of #783).

        Under the async backend every task's sync code runs in a bridge
        greenlet on the event-loop thread, so a per-thread registry put every
        task's ``transaction()`` in one list and refused task B's wait on a
        lock task A holds -- a wait A releases as soon as B yields to the
        loop. A bridge greenlet runs inside its driving task's step, so
        ``asyncio.current_task()`` is that task for ``transaction()``'s
        enter, every ``async_*`` call in it, and its exit alike. (Not a
        ``ContextVar``: the bridge runs each call in a fresh copy of the
        context, so a value ``__enter__`` set would be gone by the next
        call.) Off the loop -- a sync caller, a worker thread -- there is no
        running task and the scope is the thread, as before."""
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no running event loop on this thread
            task = None
        return task if task is not None else threading.current_thread()

    def _open_units(self) -> list[PostgresUnitOfWork]:
        """The ``transaction()`` units open in this task (or, off the event
        loop, on this thread), outermost first."""
        scope = self._lock_scope()
        with self._lock:
            units = self._open.get(scope)
            if units is None:
                units = self._open[scope] = []
            return units

    def _refuse_self_wait(
        self, pg: Optional[PostgresUnitOfWork], keys: Sequence[str]
    ) -> None:
        """Refuse, before it is sent, a statement that would wait on a
        record-key lock another open unit of work *in this task or thread*
        holds (#783 review). That lock is released only when the other unit
        commits or rolls back, which this task or thread cannot do while it
        waits: the statement would hang to ``PG_STATEMENT_TIMEOUT_MS`` and
        then be misreported as an outage. Two nested ``popoto.batch()``es or
        ``transaction()``s that write one record, or a write outside a batch
        to a record saved in it, are the shapes. On Redis each is fine (a
        queued command holds no lock); here it is
        :class:`~popoto.backends.BackendCapabilityError`, at once.

        Only record-key locks (``_record_locked``) are tracked: a wait on
        another lock a unit holds -- a ``(model, field)`` validity lock, an
        uncommitted ``UNIQUE`` entry -- is not detected here and still runs
        to ``PG_STATEMENT_TIMEOUT_MS``.

        A unit of an *ancestor* task -- one that had a unit open when it
        created this task, directly or through intermediate tasks
        (:data:`_ancestor_scopes`) -- is refused too, with its own message
        (#784 review): the shape is ``async with transaction()`` around an
        ``await gather(child)`` or an awaited ``create_task``, where the
        parent cannot commit until the child finishes. A unit of any other
        task or thread -- a sibling, an unrelated task, another thread -- is
        never a reason to refuse: that one can commit while this one waits."""
        wanted = set(keys)
        for unit in self._open_units():
            if unit is not pg and not unit.locked.isdisjoint(wanted):
                held = sorted(unit.locked & wanted)[0]
                raise BackendCapabilityError(
                    f"this write would wait on the record lock {held!r}, which "
                    "another open Postgres transaction of this task or thread "
                    "holds (a nested popoto.batch() or transaction() that wrote "
                    "the same record, or a write outside the batch it was saved "
                    "in): it could never be granted. Write through the batch "
                    "that holds the record, or execute() it first"
                )
        ancestors = _ancestor_scopes.get()
        if not ancestors:
            return
        scope = self._lock_scope()
        with self._lock:
            held_by = [
                list(self._open.get(task) or ())
                for task in (ref() for ref in ancestors)
                if task is not None and task is not scope
            ]
        for units in held_by:
            for unit in units:
                if unit is not pg and not unit.locked.isdisjoint(wanted):
                    held = sorted(unit.locked & wanted)[0]
                    raise BackendCapabilityError(
                        f"this record ({held!r}) is locked by an enclosing "
                        "Postgres transaction in a parent task, which cannot "
                        "commit while it awaits this task: the write could "
                        "never be granted. Pass pipeline=uow (the parent's "
                        "unit of work or batch) to join it, or write the "
                        "record after the parent's transaction ends"
                    )

    def _unit_open_here(self) -> bool:
        """Whether a ``transaction()`` (or a ``popoto.batch()`` holding
        Postgres writes) is open in this task or thread, or in an ancestor
        task that awaits this one (#788 review): the sessions a statement
        that waits for *every* transaction on a table -- ``REINDEX``/``DROP
        INDEX CONCURRENTLY`` -- would wait on forever."""
        if self._open_units():
            return True
        ancestors = _ancestor_scopes.get()
        if not ancestors:
            return False
        scope = self._lock_scope()
        with self._lock:
            return any(
                self._open.get(task)
                for task in (ref() for ref in ancestors)
                if task is not None and task is not scope
            )

    @contextlib.contextmanager
    def second_connection_ok(self) -> Iterator[None]:
        """Declare that the block checks out a pooled connection of its own
        while a unit of work may be open in this task or thread, on purpose:
        first-use DDL (which must commit outside any caller's transaction),
        or a caller that reads or writes outside the unit it holds (a query
        inside ``with transaction()``, a nested batch). Only the debug guard
        (:data:`STRICT_UNIT_CONNECTION`) reads it; nothing else changes."""
        scope = self._lock_scope()
        with self._lock:
            self._second_ok[scope] = self._second_ok.get(scope, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                left = self._second_ok.get(scope, 1) - 1
                if left > 0:
                    self._second_ok[scope] = left
                else:
                    self._second_ok.pop(scope, None)

    @contextlib.contextmanager
    def reads_on(self, uow: Optional[UnitOfWork]) -> Iterator[None]:
        """Run every read in the block that names no unit of work on
        ``uow``'s connection (#776): for code that reads through model-level
        APIs (``Model.exists``, ``query.get``) on behalf of a write it was
        handed ``uow`` for, so the read sees the unit's own writes and takes
        no second pooled connection. A write is never redirected: one that
        names no unit stays an autocommit statement. ``None``, or a unit that
        is not this backend's kind, makes the block a no-op."""
        pg = _pg_uow(uow)
        if pg is None:
            yield
            return
        scope = self._lock_scope()
        with self._lock:
            stack = self._read_units.get(scope)
            if stack is None:
                stack = self._read_units[scope] = []
            stack.append(pg)
        try:
            yield
        finally:
            with self._lock:
                stack.remove(pg)
                if not stack:
                    self._read_units.pop(scope, None)

    def _read_unit(self) -> Optional[PostgresUnitOfWork]:
        """The innermost :meth:`reads_on` unit of this task or thread."""
        if not self._read_units:
            return None
        scope = self._lock_scope()
        with self._lock:
            stack = self._read_units.get(scope)
            return stack[-1] if stack else None

    def _guard_second_checkout(self, what: str) -> None:
        """Under :data:`STRICT_UNIT_CONNECTION`, refuse to check out a
        pooled connection while a unit of work is open in this task or
        thread, outside :meth:`second_connection_ok` (#776). Called just
        before every pooled checkout, after the record-lock self-wait check,
        so that refusal keeps its own error."""
        if not STRICT_UNIT_CONNECTION:
            return
        units = self._open_units()
        if not units:
            return
        scope = self._lock_scope()
        with self._lock:
            if self._second_ok.get(scope):
                return
        raise SecondConnectionError(
            f"popoto checked out a second pooled Postgres connection ({what}) "
            f"while {len(units)} unit(s) of work are open in this task or "
            "thread: a read or write issued for an open unit must run on the "
            "unit's own connection (pass uow=), and a deliberate second "
            "connection belongs inside backend.second_connection_ok() (#776)"
        )

    @contextlib.contextmanager
    def _dedicated_connection(self, **extra: Any) -> Iterator[Any]:
        """A dedicated ``autocommit`` connection, opened for the block and
        closed after it: never a pooled one, so it holds no pool slot and no
        session setting it makes can leak to another caller. Inside the async
        bridge it is an ``AsyncConnection`` on the loop. ``extra`` adds
        connect options; ``client_binding=True`` asks for client-side
        parameter binding, as the pool's connections have, so one message can
        carry several statements with parameters.

        Failing to *open* it is an outage (``BackendUnavailableError``;
        dropped-write accounting follows the triggering operation's write
        intent); errors from the block's own statements propagate raw, for
        the caller to classify."""
        psycopg = _import_psycopg()
        timeout = float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)
        bridge = _bridged()
        kwargs: dict[str, Any] = {
            "autocommit": True,
            "prepare_threshold": None,
            "connect_timeout": max(1, int(round(timeout))),
        }
        if extra.pop("client_binding", False):
            kwargs["cursor_factory"] = (
                psycopg.AsyncClientCursor
                if bridge is not None
                else psycopg.ClientCursor
            )
        kwargs.update(extra)
        dsn = self._session_dsn()
        with self._open_dedicated(dsn, kwargs) as conn:
            yield conn

    @contextlib.contextmanager
    def _open_dedicated(self, dsn: str, kwargs: dict[str, Any]) -> Iterator[Any]:
        """Open ``dsn`` outside the pool (an ``AsyncConnection`` inside the
        async bridge) for the block, and close it after: the connect half of
        :meth:`_dedicated_connection`. Failing to open is an outage."""
        psycopg = _import_psycopg()
        bridge = _bridged()
        try:
            if bridge is not None:
                conn = bridge.connect(dsn, **kwargs)
            else:
                conn = psycopg.connect(dsn, **kwargs)
        except psycopg.OperationalError as exc:
            raise self._fail(exc, write=False) from exc
        try:
            yield conn
        finally:
            try:
                conn.close()
            except BaseException:  # noqa: BLE001 - cancelled mid-close
                try:
                    conn.pgconn.finish()
                except Exception:  # noqa: BLE001 - already gone
                    pass

    def _session_dsn(self) -> str:
        """The DSN a connection that needs a *session* opens (#800): the
        maintenance DSN when one is configured -- checked, once per backend,
        to reach the same database as the main DSN -- else the main DSN.
        Used by :meth:`_dedicated_connection` (``REINDEX``, ``DROP INDEX
        CONCURRENTLY``, first-use DDL) and by the shared ``LISTEN`` session
        (:attr:`~.events.EventsMixin.listen_dsn`)."""
        maintenance = self.maintenance_dsn
        if maintenance is None:
            return self.dsn
        if not self._maintenance_verified:
            self._verify_maintenance_dsn(maintenance)
        return maintenance

    def _database_identity(self, dsn: str) -> dict[str, Any]:
        """What identifies the database ``dsn`` reaches: its name and OID,
        the cluster's start time and version, and -- where the role may read
        it -- the cluster's ``system_identifier``. Server addresses are
        deliberately not compared: through a pooler, a proxy or another
        interface the same server reports a different one. The start time
        also tells a primary from its physical replica, which share the
        system identifier (DDL there would fail anyway)."""
        timeout = float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)
        kwargs: dict[str, Any] = {
            "autocommit": True,
            "prepare_threshold": None,
            "connect_timeout": max(1, int(round(timeout))),
        }
        psycopg = _import_psycopg()
        with self._open_dedicated(dsn, kwargs) as conn:
            row = conn.execute(
                "SELECT current_database(), "
                "(SELECT oid FROM pg_database WHERE datname = current_database()), "
                "pg_postmaster_start_time(), "
                "current_setting('server_version_num')::int"
            ).fetchone()
            identity: dict[str, Any] = {
                "database": row[0],
                "database_oid": int(row[1]),
                "started": row[2],
                "server_version_num": int(row[3]),
            }
            try:
                (system,) = conn.execute(
                    "SELECT system_identifier FROM pg_control_system()"
                ).fetchone()
                identity["system_identifier"] = int(system)
            except psycopg.Error:  # not granted to this role: compare the rest
                pass
        return identity

    def _verify_maintenance_dsn(self, maintenance: str) -> None:
        """Refuse a maintenance DSN that reaches another database than the
        main one (:class:`MaintenanceDsnMismatchError`), before anything runs
        on it; on a match, remember it for this backend's lifetime."""
        main = self._database_identity(self.dsn)
        other = self._database_identity(maintenance)
        differ = [k for k in main if k in other and main[k] != other[k]]
        if differ:
            shown = ", ".join(f"{k}: {main[k]!r} vs {other[k]!r}" for k in differ)
            raise MaintenanceDsnMismatchError(
                f"the Postgres maintenance DSN ({_describe_dsn(maintenance)}; "
                f"{MAINTENANCE_URL_ENV} or maintenance_dsn=) reaches a "
                f"different database than the main DSN "
                f"({_describe_dsn(self.dsn)}; {POSTGRES_URL_ENV} or dsn=): "
                f"{shown}. Refusing to run DDL, REINDEX or LISTEN through it; "
                "point it at the same database (directly or through a "
                "session-mode pool)"
            )
        self._maintenance_verified = True

    @contextlib.contextmanager
    def _maintenance_connection(self, lock_timeout_ms: int) -> Iterator[Any]:
        """A :meth:`_dedicated_connection` for ``REINDEX``/``DROP INDEX
        CONCURRENTLY`` (#788 review): a long REINDEX holds no pool slot, and
        its session ``lock_timeout``/``statement_timeout`` (which
        ``CONCURRENTLY`` needs: it cannot run in a transaction block, so
        ``SET LOCAL`` cannot reach it) can never leak to another caller.
        Errors from the block's own statements propagate raw."""
        with self._dedicated_connection() as conn:
            statement_ms = int(Defaults.PG_MAINTAIN_STATEMENT_TIMEOUT_MS)
            conn.execute(f"SET lock_timeout = {max(0, int(lock_timeout_ms))}")
            conn.execute(f"SET statement_timeout = {max(0, statement_ms)}")
            yield conn

    @contextlib.contextmanager
    def _ddl_connection(self) -> Iterator[Any]:
        """The connection first-use DDL runs on (#776): a pooled one when no
        unit of work is open in this task or thread (or in an ancestor task
        that awaits it), else a :meth:`_dedicated_connection` outside the
        pool.

        DDL has to commit on its own, never inside a caller's unit (a
        rollback would leave the memo naming a table that is not there), so
        it cannot use the unit's connection. Taking a *pooled* one while the
        unit holds another starved the pool: N concurrent units on a cold
        model, N = ``PG_POOL_MAX_SIZE``, each held a slot and waited for one
        more, and every one of them failed with ``BackendBusyError``. A
        dedicated connection costs a connect, paid only when there is DDL to
        run: the common case -- the table exists -- is settled before this by
        a catalog read on the unit's own connection
        (:meth:`_table_if_current`, :meth:`_ensure_engine_table`). Failures
        are classified as :meth:`_connection` classifies them.

        The dedicated connection carries a session ``lock_timeout``
        (``Defaults.PG_DDL_LOCK_TIMEOUT_MS``) and ``statement_timeout``
        (``Defaults.PG_DDL_STATEMENT_TIMEOUT_MS``), so DDL that waits on
        another session's lock gives up instead of hanging with the
        first-use lock held (PR #793 review). Either timeout raises
        :class:`~popoto.backends.BackendRetryableError` -- contention, not an
        outage: health is untouched -- and nothing is memoised, so the next
        use runs the check again. A lock the open unit itself holds is
        refused before any DDL by :meth:`_refuse_ddl_under_own_lock`.

        When a maintenance DSN is configured (#800) the dedicated connection
        is always used, unit or not, and it connects through that DSN."""
        psycopg = _import_psycopg()
        lock_ms = max(1, int(Defaults.PG_DDL_LOCK_TIMEOUT_MS))
        statement_ms = max(1, int(Defaults.PG_DDL_STATEMENT_TIMEOUT_MS))
        try:
            # With a maintenance DSN (#800) first-use DDL always runs there,
            # on a dedicated connection: the main DSN may be a
            # transaction-mode pooler, and the maintenance role may be the
            # only one allowed to run DDL.
            if self.maintenance_dsn is None and not self._unit_open_here():
                # Pooled. The timeouts are SET LOCAL inside each DDL
                # transaction (schema.ddl_timeout_sql), so this connection
                # goes back to the pool unmodified.
                self._guard_second_checkout("a transaction or DDL connection")
                pool = _pool_for(self.dsn)
                with _checkout(pool) as conn:
                    yield conn
            else:
                with self._dedicated_connection(client_binding=True) as conn:
                    # A backstop for any statement outside ensure_table's own
                    # SET LOCAL phases (which override it per transaction).
                    conn.execute(f"SET lock_timeout = {lock_ms}")
                    conn.execute(f"SET statement_timeout = {statement_ms}")
                    yield conn
        except _rollback_errors(psycopg) as exc:
            raise _retryable(exc) from exc
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as exc:
            raise BackendRetryableError(
                f"first-use DDL gave up waiting (SQLSTATE "
                f"{getattr(exc, 'sqlstate', None)}: {exc}): another session "
                f"holds a lock it needs (Defaults.PG_DDL_LOCK_TIMEOUT_MS = "
                f"{lock_ms}, PG_DDL_SCHEMA_LOCK_TIMEOUT_MS = "
                f"{int(Defaults.PG_DDL_SCHEMA_LOCK_TIMEOUT_MS)}, "
                f"PG_DDL_STATEMENT_TIMEOUT_MS = {statement_ms}); "
                "nothing was changed, so retry it once that transaction ends"
            ) from exc
        except psycopg.OperationalError as exc:
            busy = _busy(exc)
            if busy is not None:
                raise busy from exc
            raise self._fail(exc, write=False) from exc

    def _own_unit(self) -> Optional[PostgresUnitOfWork]:
        """The innermost ``transaction()`` open in this task or thread, whose
        connection a first-use catalog read can run on for free (#776)."""
        units = self._open_units()
        return units[-1] if units else None

    def _ensure_engine_table(
        self,
        probe: str,
        params: Sequence[Any],
        present: Callable[[list[tuple[Any, ...]]], bool],
        ddl: Callable[[], tuple[str, list[Any]]],
        missing: str,
    ) -> None:
        """First use of an engine-owned side table (the event streams, the
        recipe tables, ``popoto_recall_proposal``): run the catalog ``probe``
        and, when ``present(rows)`` says the table is missing, ``ddl()``.

        Neither takes a pooled connection while a unit of work is open here
        (#776): the probe is a read, so it runs on the unit's own connection;
        the DDL must commit on its own, so it runs on :meth:`_ddl_connection`.
        With no unit open the DDL runs on a pooled one; either way it carries
        its own ``SET LOCAL`` lock and statement timeouts.
        ``missing`` describes the table for the ``POPOTO_SCHEMA_AUTO=0``
        error."""
        rows, _ = self._run(probe, params, uow=self._own_unit())
        if present(rows):
            return
        if not _schema_auto():
            from ..types import SchemaDriftError

            raise SchemaDriftError(f"{missing} and POPOTO_SCHEMA_AUTO=0")
        sql, ddl_params = ddl()
        # The DDL (which carries its own SET LOCAL timeouts) always runs on
        # _ddl_connection, so a wait on another session's lock ends in
        # BackendRetryableError whether or not a unit is open here.
        with self._write_intent(True), self._ddl_connection() as conn:
            cur = conn.execute(sql, ddl_params)
            while cur.nextset():
                pass

    def _note_open_scope(self) -> None:
        """Record the running task in :data:`_ancestor_scopes` of its own
        context when it has a unit of work open here, so the tasks it
        creates from now on can tell its record locks from a sibling's.
        Called by ``AsyncPostgresBackend.run`` in the task's context, after
        each bridged call (the call that opened the unit cannot set it: the
        bridge runs it in a copy of the context)."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return
        if task is None:
            return
        with self._lock:
            if not self._open.get(task):
                return
        ancestors = _ancestor_scopes.get()
        if ancestors and ancestors[-1]() is task:
            return
        _ancestor_scopes.set(ancestors + (weakref.ref(task),))

    def _statement_prefix(self) -> str:
        ms = int(Defaults.PG_STATEMENT_TIMEOUT_MS)
        return f"SET LOCAL statement_timeout = {ms}; " if ms > 0 else ""

    def _run(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        uow: Optional[UnitOfWork] = None,
        write: bool = False,
    ) -> tuple[list[tuple[Any, ...]], int]:
        """Run one statement; return the rows and rowcount of that statement.

        Inside a Postgres unit of work it runs on the transaction's
        connection. Otherwise it is one message -- ``SET LOCAL
        statement_timeout`` plus the statement, one implicit transaction --
        retried on a deadlock or serialization failure, and retried once on a
        fresh connection when the pooled one turns out to be dead and the
        statement cannot have committed (:meth:`_may_retry_broken`).
        """
        psycopg = _import_psycopg()
        pg = _pg_uow(uow)
        if pg is None and not write:
            pg = self._read_unit()  # reads_on (#776)
        lock_keys = record_lock_keys(sql, params)
        if lock_keys:
            self._refuse_self_wait(pg, lock_keys)
        if pg is not None:
            try:
                cur = pg.conn.execute(sql, params)
            except _rollback_errors(psycopg) as exc:
                # The caller owns the transaction, so it decides whether to
                # run it again: no internal retry, one popoto type.
                raise _retryable(exc) from exc
            except psycopg.OperationalError as exc:
                raise self._fail(exc, write=write) from exc
            # A multi-statement message (a lock, then the statement) replies
            # with the last statement's result, as on the autocommit path.
            while cur.nextset():
                pass
            rows = cur.fetchall() if cur.description else []
            pg.locked.update(lock_keys)
            return rows, cur.rowcount
        self._guard_second_checkout(f"autocommit {sql.split(None, 1)[0]!s}")
        attempts = int(Defaults.PG_TRANSACTION_RETRIES) + 1
        prefix = self._statement_prefix()
        attempt = 0
        reconnected = False
        while True:
            conn: Any = None
            try:
                pool = _pool_for(self.dsn)
                with _checkout(pool) as conn:
                    cur = conn.execute(prefix + sql, params)
                    while cur.nextset():
                        pass
                    rows = cur.fetchall() if cur.description else []
                    rowcount = cur.rowcount
                self._ok()
                return rows, rowcount
            except _rollback_errors(psycopg) as exc:
                attempt += 1
                if attempt >= attempts:
                    raise _retryable(exc, attempt) from exc
                _sleep(random.uniform(0.005, 0.05) * attempt)
            except psycopg.OperationalError as exc:
                busy = _busy(exc)
                if busy is not None:  # contention, not an outage
                    raise busy from exc
                if not reconnected and self._may_retry_broken(conn, exc, write):
                    reconnected = True
                    logger.warning(
                        "popoto Postgres: pooled connection was dead (%s: %s); "
                        "retrying once on a fresh connection",
                        type(exc).__name__,
                        exc,
                    )
                    continue
                raise self._fail(exc, write=write) from exc

    @staticmethod
    def _may_retry_broken(conn: Any, exc: BaseException, write: bool) -> bool:
        """Whether a failed autocommit statement may run again, once.

        Only for a connection-level failure on a connection the pool handed
        out (never a failed connect: that is an outage, and retrying would
        double its timeout), and only when the statement cannot have
        committed: a read always qualifies; a write only when the server
        itself reported an error for it (``sqlstate`` set, e.g. 57P01
        ``AdminShutdown`` from a terminated backend), because the implicit
        transaction of a statement the server failed is rolled back. A write
        whose reply was simply lost (no ``sqlstate``) may have committed, so
        it is not retried: it raises and counts as a dropped write.
        """
        if conn is None or not (conn.closed or conn.broken):
            return False
        return not write or getattr(exc, "sqlstate", None) is not None

    def _check_server(self, uow: Optional[UnitOfWork] = None) -> None:
        if self._server_checked:
            return
        self._check_server_facts(*self._server_facts(uow))

    def _check_server_facts(self, version: int, encoding: str) -> None:
        """Refuse a server below the version floor or not UTF8; else
        remember that this backend's server passed."""
        if version < MIN_SERVER_VERSION_NUM:
            raise BackendCapabilityError(
                f"popoto's Postgres backend requires PostgreSQL 18 or newer "
                f"(server_version_num >= {MIN_SERVER_VERSION_NUM}); the server at "
                f"this DSN reports {version}"
            )
        if encoding.upper() not in ("UTF8", "UTF-8"):
            raise BackendCapabilityError(
                f"popoto's Postgres backend requires server_encoding UTF8 (tie "
                f"order is bytewise UTF-8, as on Redis); the server reports "
                f"{encoding}"
            )
        self._server_checked = True

    def _server_facts(self, uow: Optional[UnitOfWork] = None) -> tuple[int, str]:
        rows, _ = self._run(
            "SELECT current_setting('server_version_num')::int, "
            "current_setting('server_encoding')",
            uow=uow,
        )
        return int(rows[0][0]), str(rows[0][1])

    def _table(self, spec: ModelSpec, *, write: bool = False) -> TableSpec:
        """The model's table, created or checked on first use and again
        whenever its spec object changes (``_auto_key`` is added to a model
        at its first instantiation, after a query may already have bound it).

        ``write`` names the operation that needs the table: when the server
        is unreachable at this first check, a write counts as dropped."""
        cached = self._tables.get(spec.name)
        if cached is not None and cached[0] is spec:
            return cached[1]
        with self._write_intent(write):
            return self._check_table(spec)

    def _check_table(self, spec: ModelSpec) -> TableSpec:
        # Inside a unit of work, settle the common case first -- the table
        # exists and is current -- with catalog reads on the unit's own
        # connection: no DDL, no second connection (#776).
        unit = self._own_unit()
        if unit is not None:
            current = self._table_if_current(spec, unit)
            if current is not None:
                with self._lock:
                    self._tables[spec.name] = (spec, current)
                return current
        # The RLock separates threads, not async tasks: every bridge greenlet
        # runs on the loop thread, so for them it is re-entrant and concurrent
        # first uses all pass it (M5). What serialises first use is the
        # server-side DDL lock `ensure_table` takes (pg_advisory_xact_lock on
        # "popoto:ddl:<schema>"); a second caller then finds the table made.
        # First-use DDL must commit on its own, never inside a caller's unit
        # of work (its rollback would leave the memo naming a table that is
        # not there): with a unit open it runs on a dedicated connection
        # outside the pool, never on a pool slot the open units may be
        # holding (_ddl_connection, #776). It runs once per model per process.
        with self._lock:
            cached = self._tables.get(spec.name)
            if cached is not None and cached[0] is spec:
                return cached[1]
            ts = compile_table(spec, self.schema)
            with self._ddl_connection() as conn:
                self._refuse_ddl_under_own_lock(conn, ts, spec)
                self._check_server(PostgresUnitOfWork(conn))
                require_extensions(conn, ts)
                ensure_table(conn, ts, auto=_schema_auto())
                # M3: a ValidityField's open-pointer table, outside any
                # transaction that will later hold the model table's locks.
                ensure_validity_tables(conn, ts, spec)
            self._ok()
            self._tables[spec.name] = (spec, ts)
            return ts

    def _units_here(self) -> list[PostgresUnitOfWork]:
        """Every ``transaction()`` open in this task or thread, or in an
        ancestor task that awaits this one: the units :meth:`_unit_open_here`
        reports, as a list."""
        units = list(self._open_units())
        ancestors = _ancestor_scopes.get()
        if ancestors:
            scope = self._lock_scope()
            with self._lock:
                for task in (ref() for ref in ancestors):
                    if task is not None and task is not scope:
                        units.extend(self._open.get(task) or ())
        return units

    def _refuse_ddl_under_own_lock(
        self, conn: Any, ts: TableSpec, spec: ModelSpec
    ) -> None:
        """Refuse first-use DDL for ``ts`` while a unit of work open here
        already holds a lock on that table (or one of its validity pointer
        tables), before any DDL is sent (PR #793 review).

        The DDL must commit on its own, so it runs on a connection other than
        the unit's (:meth:`_ddl_connection`); an ``ALTER TABLE`` there waits
        for every lock on the table, including the one this unit took when
        it read or wrote it -- and this unit cannot commit while this task or
        thread waits. Two shapes reach it: a model redefined with an added
        field after the unit already used its table, and an auto-key model
        whose table was bound before its first instance existed (the first
        instance adds ``_auto_key``) after the unit read it. Without this the
        wait lasts until ``PG_DDL_LOCK_TIMEOUT_MS``, with the first-use lock
        held; with it, the caller gets :class:`SchemaDriftError` at once.

        ``pg_locks`` is a cluster-wide view, so it is read on ``conn`` (the
        DDL connection itself) for the units' backend pids: no statement runs
        on a unit's connection, which may belong to an ancestor task."""
        units = self._units_here()
        if not units:
            return
        pids = []
        for unit in units:
            pid = getattr(getattr(unit.conn, "info", None), "backend_pid", None)
            if pid:
                pids.append(int(pid))
        if not pids:
            return
        names = [ts.qualified]
        names += [pointer_table(ts, f) for f in validity_field_names(spec)]
        held = conn.execute(
            "SELECT DISTINCT l.mode FROM pg_locks l WHERE l.locktype = 'relation' "
            "AND l.granted AND l.pid = ANY(%s) AND l.database = (SELECT oid FROM "
            "pg_database WHERE datname = current_database()) AND l.relation = "
            "ANY(ARRAY(SELECT to_regclass(n)::oid FROM unnest(%s::text[]) AS n "
            "WHERE to_regclass(n) IS NOT NULL))",
            (pids, names),
        ).fetchall()
        if not held:
            return
        # A table that is already current needs no DDL that conflicts with
        # the unit's lock (the check reached here from an ancestor task's
        # unit, whose catalog was not read first): let ensure_table say so.
        if self._table_if_current(spec, PostgresUnitOfWork(conn)) is not None:
            return
        from ..types import SchemaDriftError

        modes = ", ".join(sorted(str(row[0]) for row in held))
        raise SchemaDriftError(
            f"{ts.qualified} (model {ts.model}) needs a schema change (create "
            "or additive migration) while a unit of work open in this task or "
            f"thread already holds a lock on it ({modes}). The change must "
            "commit on its own connection, which would wait for this unit "
            "forever. Let the schema change happen outside the unit: use the "
            "model once -- a save, or a query after its first instance exists "
            "-- before opening the transaction, or commit the unit first "
            "(#776)."
        )

    def _table_if_current(
        self, spec: ModelSpec, unit: PostgresUnitOfWork
    ) -> Optional[TableSpec]:
        """``spec``'s table when the catalog, read on ``unit``'s connection,
        shows it exists and is current, else ``None`` (#776).

        "Current" is :func:`~.schema.ensure_table`'s own ``"current"``
        outcome -- the ``popoto_schema`` record names this model, a format
        this client understands and this spec's fingerprint -- plus every
        ``ValidityField`` pointer table present. The record and the DDL it
        describes commit in one transaction, so a committed matching record
        is the table in that shape; reading it needs no advisory lock.
        Anything else -- a missing schema, registry, table or record, a
        fingerprint to migrate to, drift -- returns ``None``, and the full
        check runs (and raises what it raises) on :meth:`_ddl_connection`.
        Every statement here is a catalog read (``to_regclass`` never raises
        for a missing relation, and the registry is read only once it is
        known to exist), so it cannot abort the caller's transaction."""
        ts = compile_table(spec, self.schema)
        registry = f"{quote_ident(self.schema)}.{POPOTO_SCHEMA_TABLE}"
        names = [registry, ts.qualified]
        names += [pointer_table(ts, f) for f in validity_field_names(spec)]
        regclass = ", ".join("to_regclass(%s) IS NOT NULL" for _ in names)
        rows, _ = self._run(
            "SELECT current_setting('server_version_num')::int, "
            f"current_setting('server_encoding'), {regclass}",
            names,
            uow=unit,
        )
        version, encoding, *present = rows[0]
        if not self._server_checked:
            self._check_server_facts(int(version), str(encoding))
        if not all(present):
            return None
        rows, _ = self._run(
            f"SELECT model, fingerprint, format_version FROM {registry} "
            "WHERE table_name = %s",
            [ts.table],
            uow=unit,
        )
        if not rows:
            return None
        model, fingerprint, fmt = rows[0]
        if (
            model != ts.model
            or int(fmt) > SCHEMA_FORMAT_VERSION
            or fingerprint != ts.fingerprint()
        ):
            return None
        require_extensions(unit.conn, ts)
        return ts

    def forget_tables(self) -> None:
        """Drop the in-process memo of checked tables (test isolation)."""
        with self._lock:
            self._tables.clear()
            self.__dict__.pop("_recall_ready", None)
            self._reaper.forget()
            self.__dict__.pop("_engine_ready", None)

    # -- A. lifecycle ----------------------------------------------------------

    def bind(self, spec: ModelSpec) -> Capabilities:
        """Validate ``spec`` against the type map and report capabilities,
        without touching the network.

        The server checks -- connect, version (>= 18), encoding (UTF8), then
        create or check the table and its ``popoto_schema`` record -- run
        inside the model's first operation (:meth:`_table`), so an outage at
        that moment is attributed to the operation that hit it: a first
        ``save()`` against an unreachable server is a dropped write, a first
        query is not."""
        compile_table(spec, self.schema)
        return Capabilities(
            backend=self.name,
            groups=POSTGRES_CAPABILITIES_GROUPS,
            field_kinds=frozenset(fs.kind for fs in spec.fields.values()),
            key_migration=False,
            # M5: Meta.ttl models expire (an _expires_at column, a read
            # filter and the reaper); an instance TTL needs Meta.ttl.
            record_ttl=spec.ttl is not None,
        )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[PostgresUnitOfWork]:
        """One ``READ COMMITTED`` transaction on one pooled connection: every
        write passed this unit of work (``pipeline=uow``) commits together or
        rolls back together. A deadlock or serialization failure inside it
        (or at its COMMIT) rolls the whole unit back and raises
        :class:`~popoto.backends.BackendRetryableError` at once -- it is not
        retried internally, because only the caller can run its block again.
        Single statements outside a unit of work are retried automatically
        (``Defaults.PG_TRANSACTION_RETRIES``) and raise the same type once
        the retries are spent.

        After the unit commits, the TTL reaper runs for each ``Meta.ttl``
        table it wrote (M5): never inside the caller's transaction, and not
        at all when the unit rolled back."""
        with self._connection(write=True) as conn:
            with conn.transaction():
                ms = int(Defaults.PG_STATEMENT_TIMEOUT_MS)
                if ms > 0:
                    conn.execute(f"SET LOCAL statement_timeout = {ms}")
                uow = PostgresUnitOfWork(conn)
                units = self._open_units()
                units.append(uow)
                try:
                    yield uow
                    # #759 M5: the stream appends, inside the transaction,
                    # while the unit is still registered as open.
                    uow._run_before_commit()
                finally:
                    units.remove(uow)
        # Reached only after COMMIT succeeded: an exception in the block, or
        # at COMMIT, propagates past this line and the callbacks are dropped.
        # (A popoto.batch() rolled back by reset() clears both lists first:
        # psycopg swallows its own Rollback signal, so that path gets here.)
        uow._run_after_commit()
        for ts in list(uow.reap.values()):
            reap(self, ts)

    def _after_write(self, ts: TableSpec, uow: Optional[UnitOfWork]) -> None:
        """A record write on ``ts`` succeeded: run the TTL reaper now when it
        committed (autocommit), or after its unit of work commits (M5). A
        model without ``Meta.ttl`` has nothing to reap."""
        if not ts.ttl:
            return
        pg = _pg_uow(uow)
        if pg is not None:
            pg.reap[ts.qualified] = ts
            return
        reap(self, ts)

    def ttl_remaining(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[int]:
        """``TTL``-shaped remaining expiry per record (``-2`` no live record,
        ``-1`` no expiry, else whole seconds): what a test or tool reads from
        ``redis.ttl(key)`` on the Redis backend (M5, ``.ttl``)."""
        return ttl_remaining(self, spec, [rid.canonical for rid in ids])

    def close(self) -> None:
        close_pools()
        self.forget_tables()

    # -- B. records ------------------------------------------------------------

    def _row_values(
        self, ts: TableSpec, obj: Any, names: Sequence[str]
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in names:
            py_type = ts.field_types[name]
            value = getattr(obj, name)
            if isinstance(value, str) and "\x00" in value:
                raise ValueError(
                    f"{type(obj).__name__}.{name}: Postgres text cannot store NUL "
                    "(\\x00) characters (a documented divergence from Redis)"
                )
            if py_type is datetime.time and getattr(value, "tzinfo", None):
                raise ValueError(
                    f"{type(obj).__name__}.{name}: a Postgres time column stores "
                    "wall-clock time only, so an aware time would lose its offset "
                    "(a documented divergence from Redis); store a naive time, or "
                    "use a DatetimeField when the offset matters"
                )
            if ts.kind(name) in RELATIONSHIP_KINDS:
                values[name] = self._relationship_key(obj, name, value)
                continue
            if py_type is not datetime.datetime:
                values[name] = to_column_value(ts, name, value)
                continue
            if py_type is datetime.datetime and isinstance(value, datetime.datetime):
                offset = value.utcoffset()
                values[name + UTCOFF_SUFFIX] = (
                    None if offset is None else int(offset.total_seconds())
                )
            elif py_type is datetime.datetime:
                values[name + UTCOFF_SUFFIX] = None
            values[name] = to_db_value(py_type, value)
        return values

    @staticmethod
    def _relationship_key(obj: Any, name: str, value: Any) -> Optional[str]:
        """The related record's key, as Redis stores it: a lazy key string
        passes through, a model instance must be one of the field's model
        (the same ``ModelException`` as ``encode_popoto_model_obj``)."""
        if value is None or isinstance(value, str):
            return value
        field_model = obj._meta.fields[name].model
        if field_model is not None and not isinstance(value, field_model):
            from ...exceptions import ModelException

            raise ModelException(
                f"Relationship field requires {field_model} model instance. "
                f"got {value} instead"
            )
        return str(value.db_key.redis_key)

    def save(
        self,
        obj: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        previous_id: Optional[RecordId] = None,
        expiry: Optional[Expiry] = None,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> SaveOutcome:
        """``INSERT … ON CONFLICT (_pk) DO UPDATE``; with ``fields``
        (``update_fields=``) only those columns are written.

        Returns the ``HSET``-shaped reply ``Model.save`` has always returned:
        the number of fields written when the record is new, ``0`` when it
        existed. With a Postgres unit of work the write runs on its
        transaction and the unit of work is returned; a *Redis* pipeline
        handed to a Postgres model cannot carry the write, so it runs at once
        and the pipeline is returned untouched for its caller to execute.
        """
        # EventStreamMixin (#759 M5): the mutation entry this save appends,
        # in the save's own transaction (EventsMixin._write_with_stream).
        stream = list(options.pop("stream_entries", None) or ())
        spec = obj._meta.spec
        # M5: the expiry this save writes (``expiry=``, else the instance's
        # _ttl / _expire_at), or None to keep the stored one. Refused before
        # any I/O on a model without Meta.ttl, or for a ttl EXPIRE refuses.
        expires = expiry_sql(spec, obj, expiry)
        ts = self._table(spec, write=True)
        new_key = obj.db_key.redis_key
        old_key = obj._redis_key
        if old_key and old_key != new_key and obj._saved_field_values:
            raise BackendCapabilityError(
                f"{type(obj).__name__}: changing a key field (migrate_key=True) is "
                f"not supported on the Postgres backend in v2; {old_key!r} would "
                f"become {new_key!r}. Create the new record and delete the old one."
            )
        names = list(fields) if fields is not None else list(spec.fields)
        # #573: never overwrite a quarantined field's preserved value with a
        # default -- the same guard the Redis path runs before its HSET.
        obj._raise_if_quarantine_blocks(names)
        # Side-effect fields (BM25Field, ExistenceFilter, FrequencySketch)
        # have no column; their rows are written by the search CTEs below.
        values = self._row_values(ts, obj, [n for n in names if n in ts.field_types])
        # M3: a ValidityField's interval columns, written as SUPERSEDE_LUA's
        # mode 'open' writes them (.validity.save_parts).
        state, overrides, guards = save_parts(ts, spec, obj, names)
        # M5: CyclicDecayField.on_save's CYCLES_MERGE_LUA, as columns and
        # ON CONFLICT expressions of this upsert (.longtail).
        cyclic = cyclic_save_parts(ts, spec, obj, names)
        returning = cyclic.returning if cyclic is not None else []
        if cyclic is not None:
            state.update(cyclic.cols)
            overrides.update(cyclic.overrides)
        # M5: GeoField.on_save's GEOADD / ZREM, as the score and decoded
        # position columns of this upsert (.geo); refused before any write.
        state.update(geo_save_values(ts, spec, obj, names))
        extra = "".join(f', {expr} AS "_r{i}"' for i, expr in enumerate(returning))
        search = (
            prepare_save(self, ts, obj, fields, values, new_key)
            if ts.search is not None
            else None
        )
        cols = ["_pk"] + list(values) + list(state)
        slots = ["%s"] * len(cols)
        if expires is not None:
            # M5: _expires_at from the server clock (now + ttl) or the
            # instance's _expire_at, as EXPIRE / EXPIREAT set it on Redis.
            cols.append("_expires_at")
            slots.append(expires[0])
        col_sql = ", ".join(quote_ident(c) for c in cols)
        placeholders = ", ".join(slots)
        # Key columns are a function of _pk, so a conflict on _pk means they
        # already hold these values: leave them out of the SET.
        updates = ", ".join(
            f"{quote_ident(c)} = " + (overrides.get(c) or f"EXCLUDED.{quote_ident(c)}")
            for c in cols[1:]
            if c not in ts.key_fields
        )
        # A native write clears _migrated_from (#756's import contract).
        updates = (updates + ", " if updates else "") + (
            '"_updated_at" = now(), "_migrated_from" = NULL'
        )
        guard = f" WHERE {' AND '.join(guards)}" if guards else ""
        sql = (
            f"INSERT INTO {ts.qualified} ({col_sql}) VALUES ({placeholders}) "
            f'ON CONFLICT ("_pk") DO UPDATE SET {updates}{guard} '
            f'RETURNING (xmax = 0) AS "_ins"{extra}'
        )
        params = [new_key] + list(values.values()) + list(state.values())
        if expires is not None:
            params += expires[1]
        if search is not None and search.ctes:
            # Postings, document length and membership rows ride in the same
            # statement as data-modifying CTEs: one round trip, one
            # transaction, and a scope change moves the postings atomically.
            extra_cols = "".join(f', "_r{i}"' for i in range(len(returning)))
            sql = (
                f'WITH "_row" AS ({sql}), '
                + ", ".join(search.ctes)
                + f' SELECT "_ins"{extra_cols} FROM "_row"'
            )
            params += search.params
        # The record-key lock goes first, as a statement of its own: the
        # search CTEs' snapshot is then taken after any concurrent writer of
        # this record committed, and every writer of a record takes it before
        # the row (record_lock_sql: one lock order, plan §6). The reply is the
        # last statement's.
        purge = purge_expired_sql(ts)
        if purge:
            # M5: an expired row under this key is dropped first (its side
            # rows cascade), so the upsert writes a fresh record, as HSET on a
            # key Redis has expired creates a new hash.
            sql, params = purge + sql, [new_key] + params
        sql, params = self._record_locked(ts, [new_key], sql, params)
        psycopg = _import_psycopg()
        try:
            if guards and search is not None and search.ctes:
                # A guarded upsert (a declared valid_from, M3) with search
                # CTEs: the CTEs run even when the guard refuses the row, so
                # the refusal must roll them back -- one owned transaction
                # (retried as _run would be), or a SAVEPOINT inside the
                # caller's, that raises before it commits. A caller that
                # catches the refusal and commits keeps nothing of this save.
                def guarded(tx: UnitOfWork) -> list[tuple[Any, ...]]:
                    found, _ = self._run(sql, params, uow=tx, write=True)
                    if not found:
                        refuse_valid_from_conflict(self, spec, obj, uow=tx)
                    if stream:
                        self.stream_append_all(stream, uow=tx)
                    return found

                rows = self._atomically(guarded, uow=uow)
            else:
                rows, _ = self._write_with_stream(sql, params, uow, stream)
        except psycopg.errors.UniqueViolation as exc:
            constraint = exc.diag.constraint_name or ""
            covered = ts.unique_indexes.get(constraint, ())
            if constraint in ts.meta_indexes:
                # Meta.indexes (is_unique=True): the text pre_save raises on
                # Redis, worded from the declared tuple.
                declared = next(
                    (
                        names
                        for names, unique in obj._meta.indexes
                        if unique and tuple(names) == covered
                    ),
                    covered,
                )
                shown = ", ".join(str(getattr(obj, f)) for f in covered)
                message = (
                    f"Unique index violation on {declared}: ({shown}) already exists"
                )
            elif len(covered) == 1:
                message = (
                    f"Unique constraint violated: {covered[0]}="
                    f"{getattr(obj, covered[0])} already exists on another instance"
                )
            else:
                message = f"Unique constraint violated: {exc.diag.message_detail}"
            if options.get("ignore_errors"):
                logger.error(message)
                return SaveOutcome(id=None, result=False)
            from ...exceptions import ModelException

            raise ModelException(message) from exc
        if guards and not rows:
            # The upsert's guard refused a declared valid_from that disagrees
            # with the stored start (a save racing past pre_save_validate).
            refuse_valid_from_conflict(self, spec, obj, uow=uow)
        inserted = bool(rows and rows[0][0])
        if cyclic is not None and rows:
            # The #698 reset log line, from the cycles the merge replaced.
            cyclic.report(obj, rows[0][1:])

        obj._redis_key = new_key
        obj.obsolete_redis_key = None
        if fields is not None:
            for name in names:
                obj._saved_field_values[name] = getattr(obj, name)
        else:
            obj._saved_field_values = {
                name: getattr(obj, name) for name in obj._meta.fields
            }
        obj._db_content = dict(values)
        obj._is_persisted = True
        _wrap_capped_lists(obj, ts)
        if search is not None and search.after is not None and _pg_uow(uow) is None:
            # Piggybacked embedding backfill (#758 D7): after the commit,
            # bounded by Defaults.PG_BACKFILL_*; never inside a transaction().
            search.after()
        # M5: the TTL reaper, after the commit (or the unit of work's).
        self._after_write(ts, uow)
        saved_id = RecordId(spec.name, (), new_key)
        if _pg_uow(uow) is not None:
            return SaveOutcome(id=saved_id, result=uow)
        if uow is not None:
            return SaveOutcome(id=saved_id, result=uow.pipeline)
        return SaveOutcome(id=saved_id, result=len(names) if inserted else 0)

    def _select_cols(
        self, ts: TableSpec, fields: Optional[Sequence[str]] = None
    ) -> list[str]:
        if fields is None:
            # Auxiliary columns (an embedding's vector, M2b) are not field
            # values; reads that hydrate records never fetch them.
            return [c.name for c in ts.columns if c.role != "aux"]
        cols = ["_pk"]
        for name in fields:
            if name not in ts.field_types:
                continue
            cols.append(name)
            if ts.field_types[name] is datetime.datetime:
                cols.append(name + UTCOFF_SUFFIX)
        return cols

    def _decode(
        self, ts: TableSpec, cols: list[str], row: tuple[Any, ...]
    ) -> dict[str, Any]:
        """A selected row as the protocol's decoded ``Row``: field values,
        with a ``DatetimeField`` given back its stored offset (``NULL``
        offset = naive, returned as naive UTC wall time), and ``_id``."""
        out = dict(zip(cols, row))
        pk = out.pop("_pk")
        for name, value in out.items():
            if value is not None and ts.is_json(name):
                out[name] = decode_json(ts.field_types[name], value)
        for name in ts.datetime_fields:
            if name + UTCOFF_SUFFIX not in out:
                continue
            offset = out.pop(name + UTCOFF_SUFFIX)
            value = out.get(name)
            if value is None:
                continue
            if offset is None:
                out[name] = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                out[name] = value.astimezone(
                    datetime.timezone(datetime.timedelta(seconds=offset))
                )
        out["_id"] = RecordId(ts.model, (), pk)
        return out

    def load(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        fields: Optional[Sequence[str]] = None,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> list[Optional[Row]]:
        """``SELECT <cols> FROM <table> WHERE _pk = ANY(…)``: decoded rows
        in ``ids`` order, ``None`` where there is no record. With a Postgres
        ``uow`` the read runs on its transaction's connection and sees its
        own writes (#776); otherwise it is an autocommit read."""
        if not ids:
            return []
        ts = self._table(spec)
        cols = self._select_cols(ts, fields)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        keys = [rid.canonical for rid in ids]
        if len(keys) == 1:
            sql = f'SELECT {col_sql} FROM {ts.qualified} WHERE "_pk" = %s'
            params: list[Any] = [keys[0]]
        else:
            sql = f'SELECT {col_sql} FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])'
            params = [keys]
        live = live_sql(ts)
        if live:
            # M5: an expired row is no record, reaped or not.
            sql += " AND " + live
        rows, _ = self._run(sql, params, uow=uow)
        by_pk = {row[0]: self._decode(ts, cols, row) for row in rows}
        return [by_pk.get(k) for k in keys]

    def delete(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> int:
        """``DELETE … WHERE _pk = ANY(…)``; returns how many existed. Clears
        the instances' saved state (``objs=``) as the Redis path does."""
        if not ids:
            return 0
        ts = self._table(spec, write=True)
        keys = [rid.canonical for rid in ids]
        # CoOccurrenceField.on_delete, as CTEs of the same statement (M4).
        graph_sql, uses = graph_delete_sql(ts, spec)
        statement = (
            f'{graph_sql}DELETE FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])'
        )
        if ts.ttl:
            # M5: "how many existed" counts live records only -- Redis's DEL
            # of a key it has expired returns 0. RETURNING reports it.
            statement += " RETURNING (" + (live_sql(ts) or "TRUE") + ")"
        # A symmetric graph field's reverse-edge CTE writes the partners'
        # edge sets, so their record-key locks join the deleted keys' (M4).
        lock_sql, lock_params = graph_delete_lock_sql(ts, spec, keys)
        if lock_sql:
            sql, params = lock_sql + statement, lock_params + [keys] * (uses + 1)
        else:
            sql, params = self._record_locked(ts, keys, statement, [keys] * (uses + 1))
        # EventStreamMixin (#759 M5): each instance's "delete" entry, in the
        # delete's own transaction.
        stream = stream_deletes(options.get("objs") or ())
        rows, count = self._write_with_stream(sql, params, uow, stream)
        for obj in options.get("objs") or ():
            obj._db_content = dict()
            obj._saved_field_values = dict()
        self._after_write(ts, uow)
        if ts.ttl:
            return sum(1 for (alive,) in rows if alive)
        return int(count or 0)

    def exists(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[bool]:
        if not ids:
            return []
        ts = self._table(spec)
        keys = [rid.canonical for rid in ids]
        live = live_sql(ts)
        rows, _ = self._run(
            f'SELECT "_pk" FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])'
            + (f" AND {live}" if live else ""),
            [keys],
            uow=uow,
        )
        found = {row[0] for row in rows}
        return [k in found for k in keys]

    def increment(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        delta: Union[int, float],
        *,
        uow: Optional[UnitOfWork] = None,
        obj: Any = None,
        **options: Any,
    ) -> Any:
        """``UPDATE … SET f = coalesce(f, 0) + Δ RETURNING f``. Past
        ``2**63`` a ``bigint`` raises ``NumericValueOutOfRange`` where Redis's
        Lua goes to float (a documented divergence)."""
        import decimal

        ts = self._table(spec, write=True)
        py_type = ts.field_types[field]
        col = quote_ident(field)
        if py_type is int and isinstance(delta, (float, decimal.Decimal)):
            expr = f"coalesce({col}, 0) + %s::numeric"
        else:
            expr = f"coalesce({col}, 0) + %s"
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f'UPDATE {ts.qualified} SET {col} = {expr}, "_updated_at" = now(), "_migrated_from" = NULL '
            f'WHERE "_pk" = %s{_and_live(ts)} RETURNING {col}',
            [delta, id.canonical],
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
        self._after_write(ts, uow)
        if not rows:
            from ...exceptions import ModelException

            raise ModelException(
                f"atomic_increment: {id.canonical} no longer exists in Postgres"
            )
        value = rows[0][0]
        if py_type is int:
            new_val: Any = int(value)
        elif py_type is decimal.Decimal:
            new_val = decimal.Decimal(value)
        else:
            new_val = float(value)
        if obj is not None:
            setattr(obj, field, new_val)
            if field in obj._saved_field_values or obj._saved_field_values:
                obj._saved_field_values[field] = new_val
        if uow is not None and _pg_uow(uow) is None:
            return uow.pipeline
        return new_val

    # -- C. query --------------------------------------------------------------

    def _plan(self, plan: QueryPlan) -> QueryPlan:
        """The plan as given: the query layer populates ``where``/``order_by``
        for every non-Redis backend (``popoto.backends.planning``). A plan
        that carries a filtering ``source`` but no ``where`` was never
        compiled, and running it would silently return every row."""
        source = plan.source
        if (
            plan.where is None
            and source is not None
            and source.kind != "keys"
            and has_filters(source)
        ):
            raise BackendCapabilityError(
                "PostgresBackend needs a compiled plan (QueryPlan.where); build it "
                "with popoto.backends.planning.plan_from_call"
            )
        return plan

    def select(
        self,
        spec: ModelSpec,
        plan: QueryPlan,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> list[Row]:
        """``SELECT … WHERE <predicate> ORDER BY …, _pk COLLATE "C" LIMIT``.
        ``project=()`` returns id-only rows; a projection returns rows with
        just those fields (and ``_id``). With a Postgres ``uow`` the read
        runs on its transaction's connection and sees its own writes
        (#776: ``pre_save``'s uniqueness read inside a unit of work)."""
        ts = self._table(spec)
        plan = self._plan(plan)
        kinds = {name: fs.kind for name, fs in spec.fields.items()}
        if plan.project == () or (
            plan.source is not None and plan.source.kind == "keys"
        ):
            # Id-only rows. The keys() call carries no filter; a plan with a
            # where/order/limit (sample_related_keys: ORDER BY random() LIMIT
            # n) is honoured rather than dropped.
            where, _, _ = resolve_geo(self, ts, plan.where, uow)
            where_sql, params = render_where(ts, kinds, where)
            sql = f'SELECT "_pk" FROM {ts.qualified}{where_sql}'
            sql += render_order(ts, plan.order_by, non_null_fields(where))
            if plan.limit:
                sql += f" LIMIT {int(plan.limit)}"
            if plan.offset:
                sql += f" OFFSET {int(plan.offset)}"
            rows, _ = self._run(sql, params, uow=uow)
            return [
                {"_id": RecordId(spec.name, (), pk, native=pk.encode())}
                for (pk,) in rows
            ]
        # M5: a geo leaf runs its search first and scopes the statement by
        # the keys it matched (.geo); with_distances orders by the distance.
        where, distances, unit = resolve_geo(self, ts, plan.where, uow)
        if not any(c.name == "_geo_distance" for c in plan.compute):
            distances = None
        where_sql, params = render_where(ts, kinds, where)
        cols = self._select_cols(ts, plan.project)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        sql = f"SELECT {col_sql} FROM {ts.qualified}"
        extra: tuple[str, ...] = ()
        if distances is not None and plan.project is None:
            # Redis sorts the hydrated objects by distance (a record the geo
            # leaf did not match last), then re-sorts stably by order_by and
            # reverses for a descending one: the distance is the term after
            # order_by's, in its direction.
            sql += (
                ' LEFT JOIN unnest(%s::text[], %s::float8[]) AS "_g"("_g_pk", '
                f'"_g_d") ON "_g"."_g_pk" = {ts.qualified}."_pk"'
            )
            params = [list(distances), list(distances.values())] + params
            reverse = bool(plan.order_by) and plan.order_by[0].descending
            extra = (
                '"_g"."_g_d" ' + ("DESC NULLS FIRST" if reverse else "ASC NULLS LAST"),
            )
        sql += where_sql
        sql += render_order(ts, plan.order_by, non_null_fields(where), extra)
        if plan.limit:
            sql += f" LIMIT {int(plan.limit)}"
        if plan.offset:
            sql += f" OFFSET {int(plan.offset)}"
        rows, _ = self._run(sql, params, uow=uow)
        out: list[Row] = []
        for row in rows:
            decoded = self._decode(ts, cols, row)
            pk = decoded["_id"].canonical
            if distances is not None and pk in distances:
                decoded["_geo_distance"] = distances[pk]
                decoded["_geo_distance_unit"] = unit
            out.append(decoded)
        return out

    def count(
        self,
        spec: ModelSpec,
        plan: QueryPlan,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> int:
        ts = self._table(spec)
        plan = self._plan(plan)
        kinds = {name: fs.kind for name, fs in spec.fields.items()}
        where, _, _ = resolve_geo(self, ts, plan.where, uow)
        where_sql, params = render_where(ts, kinds, where)
        rows, _ = self._run(
            f"SELECT count(*) FROM {ts.qualified}{where_sql}", params, uow=uow
        )
        return int(rows[0][0])

    # -- D-H: later milestones ---------------------------------------------------

    def _later(self, method: str, milestone: str) -> BackendCapabilityError:
        return BackendCapabilityError(
            f"PostgresBackend.{method} arrives in #759 {milestone}; this model is "
            "bound to Postgres, which supports records and queries (groups A-C) "
            "in this release"
        )

    def field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        /,
        *args: Any,
        uow: Optional[UnitOfWork] = None,
        **kwargs: Any,
    ) -> Any:
        """The field-adapter registry (plan §2), keyed by ``(field kind,
        op)``. M1.1 registers one adapter, ``ListField`` ``push``; the rest
        arrive with their milestones (M2 onward)."""
        fs = spec.fields.get(field)
        kind = fs.kind if fs is not None else None
        if (
            fs is not None
            and kind == "ListField"
            and op == "push"
            and fs.options.get("capped")
        ):
            return self._capped_push(spec, field, *args, uow=uow, **kwargs)
        handled = self._memory_field_call(spec, field, op, args, kwargs, uow)
        if handled is not NOT_HANDLED:
            return handled
        handled = self._recipe_field_call(spec, field, op, args, kwargs, uow)
        if handled is not NOT_HANDLED:
            return handled
        handled = self._longtail_field_call(spec, field, op, args, kwargs, uow)
        if handled is not NOT_HANDLED:
            return handled
        handled = self._maintain_field_call(spec, field, op, args, kwargs, uow)
        if handled is not NOT_HANDLED:
            return handled
        raise BackendCapabilityError(
            f"PostgresBackend.field_call({kind or field}, {op!r}) has no adapter"
        )

    def _capped_push(
        self,
        spec: ModelSpec,
        field: str,
        id: RecordId,
        value: Any,
        *,
        max_length: int,
        uow: Optional[UnitOfWork] = None,
    ) -> list[Any]:
        """``CappedListProxy.push`` on Postgres: one ``UPDATE`` prepends the
        type-tagged element and keeps the first ``max_length`` -- the
        ``LPUSH`` + ``LTRIM`` pair, atomic in one statement. Returns the
        stored list, decoded."""
        from psycopg.types.json import Jsonb

        ts = self._table(spec)
        col = quote_ident(field)
        last = max(int(max_length), 1) - 1
        sql, params = self._record_locked(
            ts,
            [id.canonical],
            f"UPDATE {ts.qualified} SET {col} = jsonb_path_query_array("
            f"jsonb_build_array(%s::jsonb) || coalesce({col}, '[]'::jsonb), "
            f'\'$[0 to {last}]\'), "_updated_at" = now(), "_migrated_from" = NULL '
            f'WHERE "_pk" = %s{_and_live(ts)} RETURNING {col}',
            [Jsonb(encode_json_element(value)), id.canonical],
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
        self._after_write(ts, uow)
        if not rows:
            from ...exceptions import ModelException

            raise ModelException(
                f"push(): {id.canonical} does not exist in Postgres; save() it first"
            )
        return decode_json(list, rows[0][0]) or []
