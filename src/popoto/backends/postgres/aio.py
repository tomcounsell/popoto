"""Native async on Postgres: ``AsyncPostgresBackend`` (#759 M5, TD-6).

Before M5, every ``async_*`` method of a Postgres-bound model ran the sync
call in a worker thread (``asyncio.to_thread``). This module replaces that
shim with an ``AsyncPostgresBackend`` on ``psycopg.AsyncConnection`` -- one
connection pool per (DSN, pid, event loop) -- without a second copy of the
backend.

How: one implementation, two drivers
------------------------------------
The sync :class:`~popoto.backends.postgres.PostgresBackend` is ~7,000 lines
whose statements are interleaved with the Python that decides them: the
lazy ``bind()`` (version, encoding, extension and DDL checks), the record-key
lock order, the deadlock retry and the #769 dead-connection rule, the
savepoint a guarded save takes inside a caller's transaction, the search
CTEs, the validity refusals, the access-tracker staging after a read. An
async copy would duplicate all of it, and the first behaviour the two copies
disagreed on would be a parity bug no test of one copy could see.

So the async backend runs **the sync code itself** inside a greenlet on the
event loop thread (the technique SQLAlchemy's asyncio extension uses), and
swaps the one thing that does I/O: :func:`popoto.backends.postgres._pool_for`
returns, inside such a greenlet, a sync-shaped facade over this loop's
pool of ``AsyncConnection`` objects. Every ``conn.execute`` the sync code issues becomes
``await AsyncConnection.execute`` on the loop -- the greenlet switches back to
the coroutine driving it, which awaits the driver call and switches the
result back in. No worker thread is involved, and no SQL, lock order, error
mapping or retry rule exists twice: the SQL builders *are* the sync
backend's, by construction.

What stays on a thread: an embedding provider call (``provider.embed`` is a
sync API) and the backfill's wait on it, through
:func:`popoto.backends.postgres._blocking`; and any *Redis* I/O a Postgres
model's sync path makes (``EventStreamMixin``'s ``XADD``), which runs on the
loop thread as a blocking call. See ``docs/features/postgres-backend.md``.

Event loops
-----------
The pool is popoto's own, not ``psycopg_pool.AsyncConnectionPool`` (see
:class:`_LoopPool` for why: its workers swallow cancellation, so one left open
at loop shutdown hangs ``asyncio.run()``). Pools are per loop: an ``AsyncConnection`` belongs to the loop that opened
it, so a model used from two loops (pytest-asyncio's function-scoped loops,
``asyncio.run`` called twice) gets a pool per loop. A loop's pool is closed
with the loop: the pool registers an async generator whose ``finally`` closes
it, and ``loop.shutdown_asyncgens()`` -- which ``asyncio.run``,
``asyncio.Runner`` and pytest-asyncio all call before closing a loop --
finalizes it. A loop closed without that call is swept on the next pool
lookup from any loop (and when it is garbage collected): its connections are
closed at the socket, so ``pg_stat_activity`` does not grow with the number
of loops a process has used.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import importlib
import logging
import os
import sys
import threading
import weakref
from typing import Any, AsyncIterator, Callable, Iterator, Optional, Sequence, Union

from ...fields.constants import Defaults
from ..types import (
    Capabilities,
    Expiry,
    ModelSpec,
    QueryPlan,
    RecordId,
    Row,
    SaveOutcome,
    UnitOfWork,
)

_greenlet_mod: Any
try:  # the ``postgres`` extra brings greenlet; without it, the thread shim
    _greenlet_mod = importlib.import_module("greenlet")
except ImportError:  # pragma: no cover - see test_without_greenlet_...
    _greenlet_mod = None

__all__ = [
    "AsyncPostgresBackend",
    "BridgeMisuseError",
    "async_backend",
    "close_async_pools",
    "get_async_backend",
    "run_async",
]

logger = logging.getLogger("POPOTO.postgres")


class BridgeMisuseError(RuntimeError):
    """A sync call reached an async-only Postgres object outside the bridge.

    Raised when a unit of work from ``AsyncPostgresBackend.transaction()`` is
    handed to a *sync* method (``obj.save(pipeline=uow)`` instead of
    ``await obj.async_save(pipeline=uow)``): its connection belongs to an
    event loop and can only be driven from a coroutine on that loop."""


# -- the greenlet bridge -----------------------------------------------------

if _greenlet_mod is not None:

    class _BridgeGreenlet(_greenlet_mod.greenlet):
        """A greenlet running sync backend code for a coroutine. ``driver``
        is the greenlet of the coroutine awaiting on its behalf."""

        def __init__(self, fn: Callable[..., Any], driver: Any) -> None:
            super().__init__(fn, driver)
            self.driver = driver


def _in_bridge() -> bool:
    if _greenlet_mod is None:
        return False
    return isinstance(_greenlet_mod.getcurrent(), _BridgeGreenlet)


def _await(awaitable: Any) -> Any:
    """Await ``awaitable`` from sync code running inside the bridge: switch
    to the driving coroutine, which awaits it on the loop and switches the
    result (or the exception) back."""
    if not _in_bridge():
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()  # never leave an un-awaited coroutine behind
        raise BridgeMisuseError(
            "this Postgres connection belongs to an asyncio event loop: use the "
            "async API (await obj.async_save(pipeline=uow), await "
            "Model.query.async_filter(...)) with a unit of work from "
            "AsyncPostgresBackend.transaction(), not the sync one"
        )
    return _greenlet_mod.getcurrent().driver.switch(awaitable)


async def _spawn(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn(*args, **kwargs)`` -- sync code -- in a bridge greenlet,
    awaiting on the loop every awaitable it hands back through
    :func:`_await`. The greenlet starts in a copy of the caller's context, as
    ``asyncio.to_thread`` runs its function, so context variables read the
    same as on the thread shim."""
    assert _greenlet_mod is not None
    child = _BridgeGreenlet(fn, _greenlet_mod.getcurrent())
    child.gr_context = contextvars.copy_context()
    result = child.switch(*args, **kwargs)
    while not child.dead:
        try:
            value = await result
        except BaseException:  # noqa: BLE001 - thrown into the sync code
            result = child.throw(*sys.exc_info())
        else:
            result = child.switch(value)
    return result


@contextlib.contextmanager
def _sync_cm(acm: Any) -> Iterator[Any]:
    """An async context manager driven from sync code inside the bridge.

    Exited outside the bridge -- a generator holding it closed by the garbage
    collector after its loop is gone (a ``popoto.batch()`` that outlived
    ``asyncio.run()``) -- nothing can drive the async exit, so it is skipped
    rather than raising ``BridgeMisuseError`` into "Exception ignored" noise:
    the loop's pool closes the connection at the socket, and the server rolls
    back whatever it held."""
    value = _await(acm.__aenter__())
    try:
        yield value
    except BaseException:
        if not _in_bridge():
            raise
        if not _await(acm.__aexit__(*sys.exc_info())):
            raise
    else:
        _await(acm.__aexit__(None, None, None))


class _SyncCursor:
    """``psycopg.Cursor``'s surface over an ``AsyncCursor``: what the sync
    backend calls on a cursor (``execute``, ``fetch*``, ``nextset``,
    ``description``, ``rowcount``)."""

    __slots__ = ("_cur",)

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> "_SyncCursor":
        _await(self._cur.execute(query, params, **kwargs))
        return self

    def executemany(self, query: Any, params_seq: Any, **kwargs: Any) -> None:
        _await(self._cur.executemany(query, params_seq, **kwargs))

    def fetchone(self) -> Any:
        return _await(self._cur.fetchone())

    def fetchmany(self, size: int = 0) -> list[Any]:
        return list(_await(self._cur.fetchmany(size)))

    def fetchall(self) -> list[Any]:
        return list(_await(self._cur.fetchall()))

    def nextset(self) -> Optional[bool]:
        return self._cur.nextset()  # sync on AsyncCursor too: no I/O

    def close(self) -> None:
        _await(self._cur.close())

    def __iter__(self) -> Iterator[Any]:
        while (row := self.fetchone()) is not None:
            yield row

    def __enter__(self) -> "_SyncCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class _SyncConnection:
    """``psycopg.Connection``'s surface over an ``AsyncConnection``."""

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @property
    def async_connection(self) -> Any:
        return self._conn

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> _SyncCursor:
        return _SyncCursor(_await(self._conn.execute(query, params, **kwargs)))

    def cursor(self, *args: Any, **kwargs: Any) -> _SyncCursor:
        return _SyncCursor(self._conn.cursor(*args, **kwargs))

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        return _sync_cm(self._conn.transaction(*args, **kwargs))

    def commit(self) -> None:
        _await(self._conn.commit())

    def rollback(self) -> None:
        _await(self._conn.rollback())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)  # closed, broken, info, pgconn...


class _SyncPool:
    """``psycopg_pool.ConnectionPool.connection()`` over this loop's
    :class:`_LoopPool`: what ``_pool_for`` returns inside the bridge."""

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @contextlib.contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[_SyncConnection]:
        with _sync_cm(self._pool.connection(timeout=timeout)) as conn:
            yield _SyncConnection(conn)


class _GreenletLocal:
    """``threading.local`` keyed by greenlet: each thread's main greenlet is
    its own, so for sync callers this is per-thread exactly as before, and a
    bridge greenlet -- many share the loop thread -- gets its own slot. The
    sync backend's write-intent record (``_write_intent``) moves onto one of
    these when its async twin is created, so two coroutines binding at once
    cannot read each other's intent."""

    def __init__(self) -> None:
        object.__setattr__(self, "_slots", weakref.WeakKeyDictionary())

    def _slot(self) -> dict[str, Any]:
        key: Any = (
            _greenlet_mod.getcurrent()
            if _greenlet_mod is not None
            else threading.current_thread()
        )
        slots = object.__getattribute__(self, "_slots")
        slot = slots.get(key)
        if slot is None:
            slot = slots[key] = {}
        return slot

    def __getattr__(self, name: str) -> Any:
        try:
            return self._slot()[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self._slot()[name] = value


# -- pools: one per (DSN, pid, event loop) -------------------------------------
#
# Not psycopg_pool.AsyncConnectionPool, on purpose. Its maintenance workers are
# tasks that swallow CancelledError (psycopg_pool's CLIENT_EXCEPTIONS is
# ``(Exception, CancelledError)`` around each task they run), so a pool still
# open when its loop shuts down hangs ``asyncio.run()`` and pytest-asyncio's
# teardown whenever a worker is mid-task (a connection being added for a burst
# of concurrent callers): the loop's ``_cancel_all_tasks`` waits forever on a
# worker that caught its cancellation and went back to its queue. Measured
# here: 5 hangs in 6 runs of two conformance tests. A per-loop pool must
# survive loops that are closed without anyone closing it first, so this one
# has no background task at all: a connection is opened by the caller that
# needs it, validated on checkout, and returned or discarded inline.


class _LoopPool:
    """One event loop's connections to one DSN.

    Mirrors the sync pool (``_pool_for``): at most
    ``Defaults.PG_POOL_MAX_SIZE`` connections, a wait for a free one bounded
    by ``Defaults.PG_CONNECT_TIMEOUT_SECONDS`` (then ``PoolTimeout``, marked
    busy -- the backend raises ``BackendBusyError``, contention, not an
    outage -- unless the server failed to answer: see :meth:`_contended`),
    each validated on checkout
    with one empty-query round trip, opened ``autocommit`` with no
    server-side prepared statements (PgBouncer transaction mode) and a
    client-side cursor, so one message carries ``SET LOCAL …; <statement>``.
    A connection that comes back closed, broken or not idle (a statement
    cancelled mid-flight) is closed rather than reused.

    Every connection the pool opened is in ``conns`` until it is closed, and
    is either ``idle`` or checked out (``out``); none is ever neither. A
    cancellation (``CancelledError`` is a ``BaseException``) while the
    checkout check or a discard is awaiting closes the connection at the
    socket before it propagates, so a task cancelled at any point cannot
    leave an open session that no slot counts and no caller owns."""

    __slots__ = (
        "key",
        "dsn",
        "loop_ref",
        "max_size",
        "timeout",
        "idle",
        "conns",
        "out",
        "slots",
        "keeper",
        "opened",
        "closed",
        "attempts",
        "down",
    )

    def __init__(self, key: tuple[str, int, int], loop: Any) -> None:
        self.key = key
        self.dsn = key[0]
        self.loop_ref = weakref.ref(loop)
        self.max_size = max(1, int(Defaults.PG_POOL_MAX_SIZE))
        self.timeout = float(Defaults.PG_CONNECT_TIMEOUT_SECONDS)
        self.idle: collections.deque[Any] = collections.deque()
        self.conns: set[Any] = set()  # every open connection this pool made
        self.out: set[Any] = set()  # the checked-out subset of ``conns``
        self.slots = asyncio.Semaphore(self.max_size)
        self.keeper: Any = None
        self.opened = False
        self.closed = False
        # The outcome of every round trip to the server in progress -- a
        # connect, or the checkout check on an idle connection -- as a future
        # resolving True (the server answered), False (a connect failed) or
        # None (no evidence either way). What a timed-out waiter consults to
        # tell contention from an outage (:meth:`_contended`).
        self.attempts: set[asyncio.Future[Optional[bool]]] = set()
        self.down = False  # the last connect attempt to finish failed

    def loop_gone(self) -> bool:
        loop = self.loop_ref()
        return loop is None or loop.is_closed()

    # -- checkout ------------------------------------------------------------

    def connection(self, timeout: Optional[float] = None) -> "_Checkout":
        """``async with pool.connection() as conn``. A plain async context
        manager, not an ``asynccontextmanager`` generator: the loop's
        ``shutdown_asyncgens()`` closes every live async generator at once,
        and a checkout held past its loop (a batch outliving
        ``asyncio.run()``) would then close its connection while psycopg's
        own ``transaction()`` generator is rolling back on it -- the
        ``KeyError`` / ``OSError`` / "socket closed" tracebacks of #784's
        review. Not a generator, it is left alone: the transaction rolls back
        cleanly, and the connection closes with the pool."""
        return _Checkout(self, timeout)

    async def getconn(self, timeout: Optional[float] = None) -> Any:
        import psycopg
        from psycopg_pool import PoolTimeout

        if self.closed:
            raise psycopg.OperationalError("the popoto async pool is closed")
        wait = self.timeout if timeout is None else float(timeout)
        try:
            await asyncio.wait_for(self.slots.acquire(), wait)
        except asyncio.TimeoutError:
            timed_out = PoolTimeout(f"couldn't get a connection after {wait:.2f} sec")
            if await self._contended():
                # PoolTimeout may resolve to Any
                setattr(timed_out, "popoto_busy", True)
            raise timed_out from None
        try:
            while self.idle:
                conn = self.idle.pop()  # most recently used first
                probe = self._attempt()
                try:
                    usable = await self._usable(conn)
                except BaseException:
                    # Cancelled mid-check (the empty query is in flight):
                    # the connection is in no state to reuse, and nobody
                    # owns it once this raises. Close it here.
                    self._settle(probe, None)
                    self._finish(conn)
                    raise
                # An answer proves the server reachable; a dead socket proves
                # nothing (an idle reaper, a restart the next connect rides).
                self._settle(probe, True if usable else None)
                if usable:
                    self.out.add(conn)
                    return conn
                await self._discard(conn)
            probe = self._attempt()
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self.dsn,
                    autocommit=True,
                    prepare_threshold=None,
                    cursor_factory=psycopg.AsyncClientCursor,
                    connect_timeout=self._connect_timeout(),
                )
            except Exception:
                self.down = True
                self._settle(probe, False)
                raise
            except BaseException:  # cancelled mid-connect: no evidence
                self._settle(probe, None)
                raise
            # No await from here to the return: tracked and owned at once.
            self.down = False
            self._settle(probe, True)
            self.conns.add(conn)
            self.out.add(conn)
            return conn
        except BaseException:
            self.slots.release()
            raise

    # -- busy or outage --------------------------------------------------------

    def _connect_timeout(self) -> int:
        return max(1, int(round(self.timeout)))

    def _attempt(self) -> "asyncio.Future[Optional[bool]]":
        fut: asyncio.Future[Optional[bool]] = asyncio.get_running_loop().create_future()
        self.attempts.add(fut)
        return fut

    def _settle(
        self, fut: "asyncio.Future[Optional[bool]]", reached: Optional[bool]
    ) -> None:
        """Record an attempt's outcome. No await: safe on any exit path."""
        self.attempts.discard(fut)
        if not fut.done():
            fut.set_result(reached)

    async def _contended(self) -> bool:
        """Whether a wait for a slot that timed out is contention (busy:
        ``BackendBusyError``, health untouched) rather than an outage.

        Decided by what the server did, not by what the slots were doing when
        the wait ran out. Every slot checked out is contention outright. A
        slot can also be held by a caller still opening a connection, or
        checking an idle one -- which on a loaded runner (a shared CI host, say)
        can outlast a waiter's timeout against a perfectly healthy
        server: classified by slot state, that wait was an outage with a
        dropped write, which is what turned main red after #784 (run
        37344318326). So the waiter waits for the evidence those round trips
        produce: one that reaches the server makes it contention; a connect
        that *fails* (refused, timed out at the socket, a partition) makes it
        an outage; none answering within the connect timeout is an outage
        too (the server is not answering), so a black-holed port still counts
        every write dropped (#784 review). With nothing in progress, the
        outcome of the last connect to finish decides. The extra wait is at
        most the connect timeout, and only after a timeout."""
        if len(self.out) >= self.max_size:
            return True
        loop = asyncio.get_running_loop()
        # psycopg, like libpq, enforces at least 2 s per connect attempt.
        deadline = loop.time() + max(2, self._connect_timeout()) + 0.5
        while True:
            pending = [f for f in self.attempts if not f.done()]
            if not pending:
                return not self.down
            left = deadline - loop.time()
            if left <= 0:
                return False  # nothing answered within a connect timeout
            done, _ = await asyncio.wait(
                pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
            )
            for fut in done:
                reached = fut.result()
                if reached is not None:
                    return reached

    async def putconn(self, conn: Any) -> None:
        from psycopg.pq import TransactionStatus

        self.out.discard(conn)
        try:
            if (
                self.closed
                or conn.closed
                or conn.broken
                or conn.info.transaction_status != TransactionStatus.IDLE
            ):
                await self._discard(conn)
            else:
                self.idle.append(conn)
        finally:
            self.slots.release()

    @staticmethod
    async def _usable(conn: Any) -> bool:
        """The sync pool's ``check_connection``: one empty query."""
        if conn.closed or conn.broken:
            return False
        try:
            await conn.execute("")
        except Exception:  # noqa: BLE001 - a dead socket in any shape
            return False
        return True

    async def _discard(self, conn: Any) -> None:
        """Close and untrack. Untracked only after the close: a cancel
        during ``await conn.close()`` finishes the socket in ``_finish``
        rather than leaving an open connection the pool no longer knows."""
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - already dead
            pass
        finally:
            self._finish(conn)

    def _finish(self, conn: Any) -> None:
        """Close ``conn`` at the socket, without awaiting (safe while a
        cancellation is propagating), and forget it. Idempotent."""
        self.conns.discard(conn)
        self.out.discard(conn)
        try:
            if not conn.closed:
                conn.pgconn.finish()
        except Exception:  # noqa: BLE001 - best effort, already dead
            pass

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        """Close on the loop: every idle connection, every tracked one that
        is neither idle nor checked out (none should exist; closed at the
        socket if one does), and every checked-out one when it comes back."""
        if self.closed:
            return
        self.closed = True
        if not self.out:
            _forget(self)
        # else: a checkout outlives the loop (a batch nobody executed). It
        # cannot be closed here without racing the rollback psycopg's own
        # transaction() generator is running on it in this same
        # shutdown_asyncgens() pass, so the closed entry stays registered
        # and the next lookup, or the loop's finalizer, closes it at the
        # socket (_sweep_dead_loops).
        while self.idle:
            await self._discard(self.idle.pop())
        for conn in [c for c in self.conns if c not in self.out]:
            self._finish(conn)

    def hard_close(self) -> None:
        """Close every connection at the socket, without the loop (it is
        closed or gone, or this is a sync teardown). Same process only: a
        forked child must never terminate its parent's sessions."""
        self.closed = True
        self.idle.clear()
        self.out.clear()
        conns, self.conns = list(self.conns), set()
        if self.key[1] != os.getpid():
            return
        for conn in conns:
            try:
                if not conn.closed:
                    conn.pgconn.finish()
            except Exception:  # noqa: BLE001 - best effort at teardown
                pass


class _Checkout:
    """:meth:`_LoopPool.connection`'s context manager."""

    __slots__ = ("pool", "timeout", "conn")

    def __init__(self, pool: _LoopPool, timeout: Optional[float]) -> None:
        self.pool = pool
        self.timeout = timeout
        self.conn: Any = None

    async def __aenter__(self) -> Any:
        self.conn = await self.pool.getconn(self.timeout)
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            await self.pool.putconn(conn)


_apools: dict[tuple[str, int, int], _LoopPool] = {}
_apools_lock = threading.Lock()  # guards the dict only; no await under it


def _forget(entry: _LoopPool) -> None:
    with _apools_lock:
        if _apools.get(entry.key) is entry:
            del _apools[entry.key]


def _sweep_dead_loops() -> None:
    """Close, at the socket, the pools of loops that are closed or gone."""
    with _apools_lock:
        dead = [e for e in _apools.values() if e.loop_gone() or e.closed]
        for entry in dead:
            del _apools[entry.key]
    for entry in dead:
        entry.hard_close()


async def _keeper(entry: _LoopPool) -> AsyncIterator[None]:
    """Parked at its first ``yield`` for the life of the loop; the loop's
    ``shutdown_asyncgens()`` closes it, and the ``finally`` closes the pool
    while the loop can still run the close."""
    try:
        yield
    finally:
        await entry.aclose()


async def _pool_for_loop(dsn: str) -> _LoopPool:
    """This loop's pool for ``dsn``, created lazily: no connection is opened
    until a caller needs one."""
    loop = asyncio.get_running_loop()
    key = (dsn, os.getpid(), id(loop))
    entry = _apools.get(key)
    if entry is None or entry.loop_ref() is not loop or entry.closed:
        _sweep_dead_loops()
        from . import _import_psycopg

        _import_psycopg()
        fresh = _LoopPool(key, loop)
        with _apools_lock:
            entry = _apools.get(key)
            if entry is None or entry.loop_ref() is not loop or entry.closed:
                entry = _apools[key] = fresh
                weakref.finalize(loop, _sweep_dead_loops)
    if not entry.opened:
        entry.opened = True  # no await between the check and the set
        agen = _keeper(entry)
        entry.keeper = agen
        await agen.__anext__()  # registers with the loop's asyncgen hooks
    return entry


def close_async_pools() -> None:
    """Close every async pool this process opened, at the socket (sync
    teardown: ``PostgresBackend.close()``, the test harness). A pool whose
    loop is still running is dropped too; its next use opens a new one."""
    with _apools_lock:
        entries = list(_apools.values())
        _apools.clear()
    for entry in entries:
        entry.hard_close()


class _Bridge:
    """What ``popoto.backends.postgres`` consults (``_bridged()``) to route
    its I/O while sync code runs inside a bridge greenlet."""

    @staticmethod
    def active() -> bool:
        return _in_bridge()

    @staticmethod
    def pool(dsn: str) -> _SyncPool:
        return _SyncPool(_await(_pool_for_loop(dsn)))

    @staticmethod
    def sleep(seconds: float) -> None:
        _await(asyncio.sleep(seconds))

    @staticmethod
    def blocking(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return _await(asyncio.to_thread(fn, *args, **kwargs))

    @staticmethod
    def close_all() -> None:
        close_async_pools()


def _install() -> None:
    from . import _set_async_bridge

    _set_async_bridge(_Bridge() if _greenlet_mod is not None else None)


# -- the backend -------------------------------------------------------------


class AsyncPostgresBackend:
    """The async twin of a :class:`~popoto.backends.postgres.PostgresBackend`
    (plan §2, ``AsyncBackend``): the same ``dsn``, ``schema``, table memo and
    :attr:`health` record, with I/O on ``psycopg.AsyncConnection``.

    Get it with :func:`async_backend`, one per sync backend. Every method
    runs the sync backend's own method in the bridge, so its SQL, lock
    order, error mapping (``BackendRetryableError``,
    ``BackendUnavailableError`` with dropped-write accounting, the #769
    no-blind-retry rule), savepoint rule and first-use server checks are the
    sync backend's, not a copy of them.
    """

    name = "postgres"

    def __init__(self, sync: Any) -> None:
        self.sync = sync

    def __repr__(self) -> str:
        return f"<AsyncPostgresBackend schema={self.sync.schema!r}>"

    @property
    def dsn(self) -> str:
        return str(self.sync.dsn)

    @property
    def schema(self) -> str:
        return str(self.sync.schema)

    @property
    def health(self) -> Any:
        return self.sync.health

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run sync popoto code -- ``Model.save``, ``Query.get`` -- with its
        Postgres I/O on this loop's async pool. The routed ``async_*``
        methods are this call around their sync twins.

        Afterwards, in this task's own context, it records the task as an
        enclosing lock scope when it now has a unit of work open, so a child
        task it creates can be refused at once rather than wait on the
        parent's record locks (``_refuse_self_wait``, #784 review)."""
        try:
            return await _spawn(fn, *args, **kwargs)
        finally:
            self.sync._note_open_scope()

    # -- A. lifecycle ----------------------------------------------------------

    async def bind(self, spec: ModelSpec) -> Capabilities:
        """No network (plan decision 5): the checks run at first use."""
        caps: Capabilities = self.sync.bind(spec)
        return caps

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        """``async with backend.transaction() as uow:`` -- one ``READ
        COMMITTED`` transaction on one connection of this loop's pool.
        Pass ``uow`` as ``pipeline=`` to the ``async_*`` methods; it commits
        or rolls back with the block, and a deadlock or serialization
        failure inside it raises ``BackendRetryableError`` at once (the sync
        ``transaction()``'s rule: only the caller can rerun its block)."""
        cm = self.sync.transaction()
        uow = await self.run(cm.__enter__)
        try:
            yield uow
        except BaseException:
            if not await self.run(cm.__exit__, *sys.exc_info()):
                raise
        else:
            await self.run(cm.__exit__, None, None, None)

    async def close(self) -> None:
        """Close this loop's pools (the sync pools are ``sync.close()``'s)."""
        loop = asyncio.get_running_loop()
        with _apools_lock:
            mine = [e for e in _apools.values() if e.loop_ref() is loop]
        for entry in mine:
            await entry.aclose()

    # -- B. records ------------------------------------------------------------

    async def save(
        self,
        obj: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        previous_id: Optional[RecordId] = None,
        expiry: Optional[Expiry] = None,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> SaveOutcome:
        outcome: SaveOutcome = await self.run(
            self.sync.save,
            obj,
            fields=fields,
            previous_id=previous_id,
            expiry=expiry,
            uow=uow,
            **options,
        )
        return outcome

    async def load(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        fields: Optional[Sequence[str]] = None,
        **options: Any,
    ) -> list[Optional[Row]]:
        rows: list[Optional[Row]] = await self.run(
            self.sync.load, spec, ids, fields=fields, **options
        )
        return rows

    async def delete(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> int:
        count: int = await self.run(self.sync.delete, spec, ids, uow=uow, **options)
        return count

    async def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]:
        found: list[bool] = await self.run(self.sync.exists, spec, ids)
        return found

    async def increment(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        delta: Union[int, float],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> Union[int, float]:
        value: Union[int, float] = await self.run(
            self.sync.increment, spec, id, field, delta, uow=uow, **options
        )
        return value

    # -- C. query --------------------------------------------------------------

    async def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]:
        rows: list[Row] = await self.run(self.sync.select, spec, plan)
        return rows

    async def count(self, spec: ModelSpec, plan: QueryPlan) -> int:
        n: int = await self.run(self.sync.count, spec, plan)
        return n

    # -- H. the field-adapter registry (read staging after a hydration) --------

    async def field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        /,
        *args: Any,
        uow: Optional[UnitOfWork] = None,
        **kwargs: Any,
    ) -> Any:
        return await self.run(
            self.sync.field_call, spec, field, op, *args, uow=uow, **kwargs
        )


_twins_lock = threading.Lock()
_warned_no_greenlet = False


def async_backend(sync: Any) -> Optional[AsyncPostgresBackend]:
    """The async twin of ``sync`` (a ``PostgresBackend``), created once and
    kept on it; ``None`` when greenlet is not installed (then the routed
    methods keep the thread shim)."""
    if _greenlet_mod is None:
        return None
    twin = sync.__dict__.get("_async_twin")
    if twin is not None:
        return twin
    with _twins_lock:
        twin = sync.__dict__.get("_async_twin")
        if twin is None:
            if not isinstance(getattr(sync, "_intent", None), _GreenletLocal):
                sync._intent = _GreenletLocal()
            twin = AsyncPostgresBackend(sync)
            sync.__dict__["_async_twin"] = twin
    return twin


def get_async_backend(model: Any = None) -> AsyncPostgresBackend:
    """The async backend of ``model`` (a class or instance; ``None`` means
    the process default), for ``async with get_async_backend(M).transaction()
    as uow``. Postgres only: a Redis-bound model's ``async_*`` methods talk to
    ``redis.asyncio`` (or a worker thread) directly and have no async twin, so
    asking for one raises :class:`~popoto.backends.BackendCapabilityError`."""
    from .. import get_backend
    from ..types import BackendCapabilityError, BackendUnavailableError
    from . import PostgresBackend

    if model is not None and not isinstance(model, type):
        model = type(model)
    backend = get_backend(model)
    if not isinstance(backend, PostgresBackend):
        raise BackendCapabilityError(
            f"no async backend for the {backend.name!r} backend: AsyncBackend is "
            "Postgres's (#759 M5); Redis-bound models use their async_* methods "
            "directly"
        )
    twin = async_backend(backend)
    if twin is None:
        raise BackendUnavailableError(
            "the async Postgres backend needs greenlet: pip install 'popoto[postgres]'"
        )
    return twin


async def run_async(
    backend: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """What a routed ``async_*`` method of a non-Redis model awaits: the
    sync twin ``fn`` on the async Postgres backend, or -- for another
    backend, or without greenlet -- in a worker thread, as before M5."""
    from . import PostgresBackend

    if isinstance(backend, PostgresBackend):
        twin = async_backend(backend)
        if twin is not None:
            return await twin.run(fn, *args, **kwargs)
        global _warned_no_greenlet
        if not _warned_no_greenlet:
            _warned_no_greenlet = True
            logger.warning(
                "popoto: greenlet is not installed, so async_* on Postgres-bound "
                "models runs the sync call in a worker thread; pip install "
                "'popoto[postgres]' for the native async backend"
            )
    return await asyncio.to_thread(fn, *args, **kwargs)


_install()
