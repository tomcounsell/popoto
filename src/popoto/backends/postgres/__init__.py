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

Nothing here touches the network until a model's first operation (``bind``
itself only compiles the table spec; the server and table checks run inside
that first save or query, so an outage there is charged to it), and
``psycopg`` is imported only then: ``import popoto`` and defining a
``Meta.backend = "postgres"`` model need neither the driver nor the server.

Topology (plan §3): one ``psycopg_pool.ConnectionPool`` per (DSN, pid),
``max_size`` ``Defaults.PG_POOL_MAX_SIZE``. No session state is ever set:
every autocommit operation is one simple-query message
``SET LOCAL statement_timeout = N; <statement>``, which Postgres runs as one
implicit transaction -- atomic, one round trip, and safe behind PgBouncer in
transaction mode. Advisory locks are ``pg_advisory_xact_lock`` only.

Outage contract (``[PG-only]``): an unreachable server, a connect timeout or
a statement timeout raises :class:`BackendUnavailableError`; the backend's
:attr:`PostgresBackend.health` record counts failures and dropped writes, and
the outage is logged at ERROR once per ``Defaults.PG_OUTAGE_LOG_WINDOW_SECONDS``.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence, Union

from ...fields.constants import Defaults
from ..types import (
    BackendCapabilityError,
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
from .memory import NOT_HANDLED, PostgresMemoryOps
from .plan import (
    non_null_fields,
    render_order,
    render_where,
    to_column_value,
    to_db_value,
)
from .schema import (
    RELATIONSHIP_KINDS,
    UTCOFF_SUFFIX,
    TableSpec,
    compile_table,
    ensure_table,
    quote_ident,
)
from .search import SearchMixin, prepare_save, record_lock_sql, require_extensions
from .validity import ensure_validity_tables, refuse_valid_from_conflict, save_parts

__all__ = [
    "POSTGRES_URL_ENV",
    "POSTGRES_SCHEMA_ENV",
    "MIN_SERVER_VERSION_NUM",
    "Health",
    "PostgresBackend",
    "PostgresUnitOfWork",
    "backend_from_env",
]

logger = logging.getLogger("POPOTO.postgres")

POSTGRES_URL_ENV = "POPOTO_POSTGRES_URL"
POSTGRES_SCHEMA_ENV = "POPOTO_POSTGRES_SCHEMA"
SCHEMA_AUTO_ENV = "POPOTO_SCHEMA_AUTO"
DEFAULT_SCHEMA = "popoto"
MIN_SERVER_VERSION_NUM = 180000
"""Architect decision 2: Postgres 18 is the floor; no 16/17 fallback."""

POSTGRES_CAPABILITIES_GROUPS = frozenset("ABC")


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
    return PostgresBackend(dsn=dsn, schema=schema)


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
three helpers below route that code's I/O onto the event loop: the pool is
the loop's ``AsyncConnectionPool``, a retry back-off is ``asyncio.sleep``, and
a blocking provider call goes to a worker thread. Everywhere else they are
exactly what they were."""


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

    __slots__ = ("conn",)

    def __init__(self, conn: Any) -> None:
        super().__init__(None, backend="postgres")
        self.conn = conn

    @property
    def is_redis_pipeline(self) -> bool:
        return False

    def commit(self) -> list[Any]:
        """Nothing to flush: statements ran as they were issued, and the
        ``transaction()`` context commits."""
        return []


def _pg_uow(uow: Optional[UnitOfWork]) -> Optional[PostgresUnitOfWork]:
    return uow if isinstance(uow, PostgresUnitOfWork) else None


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


class PostgresBackend(SearchMixin, PostgresMemoryOps):
    """The Postgres implementation of :class:`popoto.backends.Backend`.

    Search -- ``keyword_search``, ``vector_search``, ``membership_*`` and the
    ``[PG-only]`` ``recall`` -- comes from :class:`.search.SearchMixin`
    (#759 M2b). Groups D/E's ranking and memory state (``touch``,
    ``update_confidence``, ``rank_decayed``, ``rank_composite``) come from
    :class:`~.memory.PostgresMemoryOps` (#759 M2a)."""

    name = "postgres"

    def __init__(self, dsn: str, schema: str = DEFAULT_SCHEMA) -> None:
        self.dsn = dsn
        self.schema = schema
        self.health = Health()
        self._tables: dict[str, tuple[ModelSpec, TableSpec]] = {}
        self._server_checked = False
        self._lock = threading.RLock()
        self._intent = threading.local()

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
        try:
            pool = _pool_for(self.dsn)
            with pool.connection() as conn:
                yield conn
        except _rollback_errors(psycopg) as exc:
            # Raised by the block (a caller-owned ``transaction()``, including
            # its COMMIT) or by DDL: the work was rolled back, not lost to an
            # outage. Not retried here -- the caller owns the transaction.
            raise _retryable(exc) from exc
        except psycopg.OperationalError as exc:
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
            return rows, cur.rowcount
        attempts = int(Defaults.PG_TRANSACTION_RETRIES) + 1
        prefix = self._statement_prefix()
        attempt = 0
        reconnected = False
        while True:
            conn: Any = None
            try:
                pool = _pool_for(self.dsn)
                with pool.connection() as conn:
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

    def _check_server(self) -> None:
        if self._server_checked:
            return
        version, encoding = self._server_facts()
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

    def _server_facts(self) -> tuple[int, str]:
        rows, _ = self._run(
            "SELECT current_setting('server_version_num')::int, "
            "current_setting('server_encoding')"
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
        with self._lock:
            cached = self._tables.get(spec.name)
            if cached is not None and cached[0] is spec:
                return cached[1]
            self._check_server()
            ts = compile_table(spec, self.schema)
            with self._connection() as conn:
                require_extensions(conn, ts)
                ensure_table(conn, ts, auto=_schema_auto())
                # M3: a ValidityField's open-pointer table, outside any
                # transaction that will later hold the model table's locks.
                ensure_validity_tables(conn, ts, spec)
            self._ok()
            self._tables[spec.name] = (spec, ts)
            return ts

    def forget_tables(self) -> None:
        """Drop the in-process memo of checked tables (test isolation)."""
        with self._lock:
            self._tables.clear()
            self.__dict__.pop("_recall_ready", None)

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
            record_ttl=False,
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
        the retries are spent."""
        with self._connection(write=True) as conn:
            with conn.transaction():
                ms = int(Defaults.PG_STATEMENT_TIMEOUT_MS)
                if ms > 0:
                    conn.execute(f"SET LOCAL statement_timeout = {ms}")
                yield PostgresUnitOfWork(conn)

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
        if expiry is not None:
            raise BackendCapabilityError("save(expiry=) arrives on Postgres in M5")
        spec = obj._meta.spec
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
        search = (
            prepare_save(self, ts, obj, fields, values, new_key)
            if ts.search is not None
            else None
        )
        cols = ["_pk"] + list(values) + list(state)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        # Key columns are a function of _pk, so a conflict on _pk means they
        # already hold these values: leave them out of the SET.
        updates = ", ".join(
            f"{quote_ident(c)} = " + (overrides.get(c) or f"EXCLUDED.{quote_ident(c)}")
            for c in list(values) + list(state)
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
            "RETURNING (xmax = 0)"
        )
        params = [new_key] + list(values.values()) + list(state.values())
        if search is not None and search.ctes:
            # Postings, document length and membership rows ride in the same
            # statement as data-modifying CTEs: one round trip, one
            # transaction, and a scope change moves the postings atomically.
            sql = (
                f'WITH "_row" AS ({sql} AS "_ins"), '
                + ", ".join(search.ctes)
                + ' SELECT "_ins" FROM "_row"'
            )
            params += search.params
        # The record-key lock goes first, as a statement of its own: the
        # search CTEs' snapshot is then taken after any concurrent writer of
        # this record committed, and every writer of a record takes it before
        # the row (record_lock_sql: one lock order, plan §6). The reply is the
        # last statement's.
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
                    return found

                rows = self._atomically(guarded, uow=uow)
            else:
                rows, _ = self._run(sql, params, uow=uow, write=True)
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
        **options: Any,
    ) -> list[Optional[Row]]:
        """``SELECT <cols> FROM <table> WHERE _pk = ANY(…)``: decoded rows
        in ``ids`` order, ``None`` where there is no record."""
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
        rows, _ = self._run(sql, params)
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
        sql, params = self._record_locked(
            ts,
            keys,
            f'DELETE FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])',
            [keys],
        )
        rows, count = self._run(sql, params, uow=uow, write=True)
        for obj in options.get("objs") or ():
            obj._db_content = dict()
            obj._saved_field_values = dict()
        return int(count or 0)

    def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]:
        if not ids:
            return []
        ts = self._table(spec)
        keys = [rid.canonical for rid in ids]
        rows, _ = self._run(
            f'SELECT "_pk" FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])', [keys]
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
            f'WHERE "_pk" = %s RETURNING {col}',
            [delta, id.canonical],
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
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

    def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]:
        """``SELECT … WHERE <predicate> ORDER BY …, _pk COLLATE "C" LIMIT``.
        ``project=()`` returns id-only rows; a projection returns rows with
        just those fields (and ``_id``)."""
        ts = self._table(spec)
        plan = self._plan(plan)
        kinds = {name: fs.kind for name, fs in spec.fields.items()}
        if plan.project == () or (
            plan.source is not None and plan.source.kind == "keys"
        ):
            # Id-only rows. The keys() call carries no filter; a plan with a
            # where/order/limit (sample_related_keys: ORDER BY random() LIMIT
            # n) is honoured rather than dropped.
            where_sql, params = render_where(ts, kinds, plan.where)
            sql = f'SELECT "_pk" FROM {ts.qualified}{where_sql}'
            sql += render_order(ts, plan.order_by, non_null_fields(plan.where))
            if plan.limit:
                sql += f" LIMIT {int(plan.limit)}"
            if plan.offset:
                sql += f" OFFSET {int(plan.offset)}"
            rows, _ = self._run(sql, params)
            return [
                {"_id": RecordId(spec.name, (), pk, native=pk.encode())}
                for (pk,) in rows
            ]
        where_sql, params = render_where(ts, kinds, plan.where)
        cols = self._select_cols(ts, plan.project)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        sql = f"SELECT {col_sql} FROM {ts.qualified}{where_sql}"
        sql += render_order(ts, plan.order_by, non_null_fields(plan.where))
        if plan.limit:
            sql += f" LIMIT {int(plan.limit)}"
        if plan.offset:
            sql += f" OFFSET {int(plan.offset)}"
        rows, _ = self._run(sql, params)
        return [self._decode(ts, cols, row) for row in rows]

    def count(self, spec: ModelSpec, plan: QueryPlan) -> int:
        ts = self._table(spec)
        plan = self._plan(plan)
        kinds = {name: fs.kind for name, fs in spec.fields.items()}
        where_sql, params = render_where(ts, kinds, plan.where)
        rows, _ = self._run(f"SELECT count(*) FROM {ts.qualified}{where_sql}", params)
        return int(rows[0][0])

    # -- D-H: later milestones ---------------------------------------------------

    def _later(self, method: str, milestone: str) -> BackendCapabilityError:
        return BackendCapabilityError(
            f"PostgresBackend.{method} arrives in #759 {milestone}; this model is "
            "bound to Postgres, which supports records and queries (groups A-C) "
            "in this release"
        )

    def graph_update(self, *a: Any, **kw: Any) -> Any:
        raise self._later("graph_update", "M4")

    def graph_expand(self, *a: Any, **kw: Any) -> Any:
        raise self._later("graph_expand", "M4")

    def maintain(self, *a: Any, **kw: Any) -> Any:
        raise self._later("maintain", "M5")

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
        raise self._later(f"field_call({kind}, {op!r})", "M2")

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
            f'WHERE "_pk" = %s RETURNING {col}',
            [Jsonb(encode_json_element(value)), id.canonical],
        )
        rows, _ = self._run(sql, params, uow=uow, write=True)
        if not rows:
            from ...exceptions import ModelException

            raise ModelException(
                f"push(): {id.canonical} does not exist in Postgres; save() it first"
            )
        return decode_json(list, rows[0][0]) or []
