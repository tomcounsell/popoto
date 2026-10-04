"""The Postgres backend: plain models on typed tables (#759 M1b).

``PostgresBackend`` implements protocol groups A-C (lifecycle, records, query)
natively -- one typed table per model (:mod:`.schema`), ``WHERE`` compiled
from the query (:mod:`.plan`) -- with no Redis structure emulated and no
msgpack stored. Groups D-H raise :class:`BackendCapabilityError` until their
milestone.

Selection (plan §4): ``Meta.backend = "postgres"`` on a model, or
``POPOTO_BACKEND=postgres`` for the process, with the DSN from
``POPOTO_POSTGRES_URL`` (or a ``PostgresBackend(dsn=…)`` handed to
``popoto.backends.set_backend``). ``POSTGRES_URL`` and ``DATABASE_URL`` are
never read. ``POPOTO_POSTGRES_SCHEMA`` names the schema (default ``popoto``);
``POPOTO_SCHEMA_AUTO=0`` turns off automatic create/additive DDL.

Nothing here touches the network until a model's first backend use
(``bind``), and ``psycopg`` is imported only then: ``import popoto`` and
defining a ``Meta.backend = "postgres"`` model need neither the driver nor
the server.

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
from .plan import non_null_fields, render_order, render_where, to_db_value
from .schema import (
    UTCOFF_SUFFIX,
    TableSpec,
    compile_table,
    ensure_table,
    quote_ident,
)

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


def _pool_for(dsn: str) -> Any:
    """The process's pool for ``dsn``, created lazily (and again after a
    fork: a child never reuses the parent's sockets)."""
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
    """Close every pool this process opened (tests, interpreter shutdown)."""
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


# -- the backend --------------------------------------------------------------


class PostgresBackend:
    """The Postgres implementation of :class:`popoto.backends.Backend`."""

    name = "postgres"

    def __init__(self, dsn: str, schema: str = DEFAULT_SCHEMA) -> None:
        self.dsn = dsn
        self.schema = schema
        self.health = Health()
        self._tables: dict[str, tuple[ModelSpec, TableSpec]] = {}
        self._server_checked = False
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"<PostgresBackend schema={self.schema!r}>"

    # -- plumbing -------------------------------------------------------------

    def _fail(self, exc: BaseException, *, write: bool) -> BackendUnavailableError:
        h = self.health
        h.ok = False
        h.consecutive_failures += 1
        h.last_error = f"{type(exc).__name__}: {exc}"
        if write:
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
        except psycopg.errors.TransactionRollback:
            raise
        except psycopg.OperationalError as exc:
            raise self._fail(exc, write=write) from exc

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
        retried on a deadlock or serialization failure.
        """
        psycopg = _import_psycopg()
        pg = _pg_uow(uow)
        if pg is not None:
            try:
                cur = pg.conn.execute(sql, params)
            except psycopg.errors.TransactionRollback:
                raise
            except psycopg.OperationalError as exc:
                raise self._fail(exc, write=write) from exc
            rows = cur.fetchall() if cur.description else []
            return rows, cur.rowcount
        attempts = int(Defaults.PG_TRANSACTION_RETRIES) + 1
        prefix = self._statement_prefix()
        for attempt in range(attempts):
            try:
                with self._connection(write=write) as conn:
                    cur = conn.execute(prefix + sql, params)
                    while cur.nextset():
                        pass
                    rows = cur.fetchall() if cur.description else []
                    rowcount = cur.rowcount
                self._ok()
                return rows, rowcount
            except psycopg.errors.TransactionRollback:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
            except psycopg.OperationalError as exc:
                raise self._fail(exc, write=write) from exc
        raise AssertionError("unreachable")  # pragma: no cover

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

    def _table(self, spec: ModelSpec) -> TableSpec:
        """The model's table, created or checked on first use and again
        whenever its spec object changes (``_auto_key`` is added to a model
        at its first instantiation, after a query may already have bound it)."""
        cached = self._tables.get(spec.name)
        if cached is not None and cached[0] is spec:
            return cached[1]
        with self._lock:
            cached = self._tables.get(spec.name)
            if cached is not None and cached[0] is spec:
                return cached[1]
            self._check_server()
            ts = compile_table(spec, self.schema)
            with self._connection() as conn:
                ensure_table(conn, ts, auto=_schema_auto())
            self._ok()
            self._tables[spec.name] = (spec, ts)
            return ts

    def forget_tables(self) -> None:
        """Drop the in-process memo of checked tables (test isolation)."""
        with self._lock:
            self._tables.clear()

    # -- A. lifecycle ----------------------------------------------------------

    def bind(self, spec: ModelSpec) -> Capabilities:
        """Lazy, first backend use per model: connect, check the server
        version (>= 18) and encoding (UTF8), then create or check the table
        and its ``popoto_schema`` record."""
        self._table(spec)
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
        rolls back together. Deadlock/serialization failures inside it are
        re-raised for the caller to retry; single-statement writes outside a
        unit of work are retried automatically."""
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
            if py_type is datetime.datetime and isinstance(value, datetime.datetime):
                offset = value.utcoffset()
                values[name + UTCOFF_SUFFIX] = (
                    None if offset is None else int(offset.total_seconds())
                )
            elif py_type is datetime.datetime:
                values[name + UTCOFF_SUFFIX] = None
            values[name] = to_db_value(py_type, value)
        return values

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
        ts = self._table(spec)
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
        values = self._row_values(ts, obj, names)
        cols = ["_pk"] + list(values)
        col_sql = ", ".join(quote_ident(c) for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        # Key columns are a function of _pk, so a conflict on _pk means they
        # already hold these values: leave them out of the SET.
        updates = ", ".join(
            f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}"
            for c in values
            if c not in ts.key_fields
        )
        updates = (updates + ", " if updates else "") + '"_updated_at" = now()'
        sql = (
            f"INSERT INTO {ts.qualified} ({col_sql}) VALUES ({placeholders}) "
            f'ON CONFLICT ("_pk") DO UPDATE SET {updates} RETURNING (xmax = 0)'
        )
        psycopg = _import_psycopg()
        try:
            rows, _ = self._run(
                sql, [new_key] + list(values.values()), uow=uow, write=True
            )
        except psycopg.errors.UniqueViolation as exc:
            covered = ts.unique_indexes.get(exc.diag.constraint_name or "", ())
            if len(covered) == 1:
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
            return [c.name for c in ts.columns]
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
        ts = self._table(spec)
        keys = [rid.canonical for rid in ids]
        rows, count = self._run(
            f'DELETE FROM {ts.qualified} WHERE "_pk" = ANY(%s::text[])',
            [keys],
            uow=uow,
            write=True,
        )
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

        ts = self._table(spec)
        py_type = ts.field_types[field]
        col = quote_ident(field)
        if py_type is int and isinstance(delta, (float, decimal.Decimal)):
            expr = f"coalesce({col}, 0) + %s::numeric"
        else:
            expr = f"coalesce({col}, 0) + %s"
        rows, _ = self._run(
            f'UPDATE {ts.qualified} SET {col} = {expr}, "_updated_at" = now() '
            f'WHERE "_pk" = %s RETURNING {col}',
            [delta, id.canonical],
            uow=uow,
            write=True,
        )
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
        if plan.project == () or (
            plan.source is not None and plan.source.kind == "keys"
        ):
            rows, _ = self._run(
                f'SELECT "_pk" FROM {ts.qualified} ORDER BY "_pk" COLLATE "C"'
            )
            return [
                {"_id": RecordId(spec.name, (), pk, native=pk.encode())}
                for (pk,) in rows
            ]
        kinds = {name: fs.kind for name, fs in spec.fields.items()}
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

    def touch(self, *a: Any, **kw: Any) -> Any:
        raise self._later("touch", "M2")

    def update_confidence(self, *a: Any, **kw: Any) -> Any:
        raise self._later("update_confidence", "M2")

    def supersede(self, *a: Any, **kw: Any) -> Any:
        raise self._later("supersede", "M3")

    def chain(self, *a: Any, **kw: Any) -> Any:
        raise self._later("chain", "M3")

    def rank_decayed(self, *a: Any, **kw: Any) -> Any:
        raise self._later("rank_decayed", "M2")

    def rank_composite(self, *a: Any, **kw: Any) -> Any:
        raise self._later("rank_composite", "M2")

    def vector_search(self, *a: Any, **kw: Any) -> Any:
        raise self._later("vector_search", "M2")

    def keyword_search(self, *a: Any, **kw: Any) -> Any:
        raise self._later("keyword_search", "M2")

    def graph_update(self, *a: Any, **kw: Any) -> Any:
        raise self._later("graph_update", "M4")

    def graph_expand(self, *a: Any, **kw: Any) -> Any:
        raise self._later("graph_expand", "M4")

    def membership_add(self, *a: Any, **kw: Any) -> Any:
        raise self._later("membership_add", "M2")

    def membership_query(self, *a: Any, **kw: Any) -> Any:
        raise self._later("membership_query", "M2")

    def maintain(self, *a: Any, **kw: Any) -> Any:
        raise self._later("maintain", "M5")

    def field_call(self, *a: Any, **kw: Any) -> Any:
        raise self._later("field_call", "M2")
