"""One transaction, opened without reaching for the client (#630, #759 M5).

``batch()`` is the supported way for a recipe -- or a caller -- to open a
Popoto transaction. It returns a real ``redis.client.Pipeline``: commands
queue until you call ``execute()``, and everything queued applies in one
``MULTI``/``EXEC``.

The return type is deliberately the redis-py pipeline itself and not a
wrapper. Popoto's field layer decides whether a write joins the caller's
transaction with ``isinstance(pipeline, redis.client.Pipeline)`` at twenty
sites, several of them shaped ``pipeline if isinstance(...) else
POPOTO_REDIS_DB``. A wrapper object would fail those checks silently, fall
back to the shared client, and execute immediately -- voiding the atomicity
the batch was opened for, with no error raised anywhere.

Because it is an ordinary pipeline, it is also an ordinary context manager:
``with`` releases the connection on exit but does **not** execute. Call
``execute()`` yourself.

    import popoto

    pipe = popoto.batch()
    JournalEntry.append(agent_id="a", statement="...", pipeline=pipe)
    JournalEntry.append(agent_id="a", statement="...", pipeline=pipe)
    pipe.execute()

**Postgres-bound models (#759 M5, plan TD-5).** The pipeline is a
:class:`Batch`, a subclass of popoto's guarded pipeline, so everything above
holds byte for byte on Redis. A write from a model bound to Postgres that is
handed the batch *joins* it: the batch opens one Postgres ``transaction()``
on that model's backend at the first such write, the write runs inside it,
and ``execute()`` commits it. It is atomic: if a statement fails inside the
transaction, nothing in the batch is written (``execute()`` then raises),
where a Redis ``MULTI``/``EXEC`` applies the other queued commands. A save
refused before it sends anything (``pre_save``'s unique check against a
committed record, a validation error) raises at the call and leaves the
batch healthy, as on Redis. ``reset()``, leaving a
``with`` block, or dropping the batch without ``execute()`` rolls it back,
as a Redis pipeline that is never executed sends nothing. Until ``execute()``
other connections do not see the batch's writes, as with queued commands.

**One batch, one backend.** A batch that has queued Redis commands refuses a
Postgres write, and one holding a Postgres transaction refuses a Redis
command, with :class:`~popoto.backends.BackendCapabilityError`, before either
is sent. Two stores cannot commit atomically together, and a batch that
looked atomic but was not would be worse than a refusal. Open one batch per
backend.

**From async code.** When the first Postgres write to join a batch is an
``async_*`` call, the batch's transaction lives on the event loop's async
pool: commit it with ``await pipe.async_execute()`` (``execute()`` raises
``BridgeMisuseError``) and roll it back with ``await pipe.async_reset()``.
A batch is driven by whichever side opened it: a sync write to an
async-opened batch, and an ``async_*`` write to a batch a sync write opened
(whose blocking connection would stall the loop), both raise
``BridgeMisuseError`` before anything is sent, and leave the batch usable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Optional

from .redis_db import GuardedPipeline, get_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.client import Pipeline

__all__ = ["Batch", "batch", "join_unit", "open_unit_of", "unit_of"]

_MIXED = (
    "popoto.batch() cannot mix Redis and Postgres writes: a Redis MULTI/EXEC "
    "and a Postgres transaction cannot commit atomically together, so the "
    "batch would only look atomic. Open one batch per backend."
)


# Rollbacks of async-opened batches scheduled by a sync reset(): held so the
# loop does not drop a running task.
_pending: set[Any] = set()

logger = logging.getLogger(__name__)


def _rollback_done(task: Any) -> None:
    """Done callback of a scheduled rollback: forget the task and retrieve
    its exception, so a rollback that failed (a connection that broke under
    it) is logged once here instead of as "Task exception was never
    retrieved". The server rolls back a transaction whose connection is
    gone, so there is nothing to retry."""
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "popoto.batch(): the scheduled rollback of an async-opened "
            "Postgres transaction failed (%s: %s); the server rolls back a "
            "transaction whose connection is gone",
            type(exc).__name__,
            exc,
        )


def _in_bridge() -> bool:
    try:
        from .backends.postgres.aio import _in_bridge as in_bridge
    except ImportError:  # pragma: no cover - the postgres extra is absent
        return False
    return in_bridge()


class Batch(GuardedPipeline):
    """The pipeline ``batch()`` returns: popoto's guarded Redis pipeline, plus
    the Postgres transaction a Postgres-bound model's writes join (module
    docstring). Redis commands queue and execute exactly as on any pipeline;
    nothing here sends a byte to Redis that the plain pipeline would not."""

    _pg: Optional[tuple[Any, contextlib.ExitStack, Any]] = None

    # -- the Postgres side -------------------------------------------------

    def _popoto_join(self, backend: Any) -> Any:
        """The unit of work a write to ``backend`` (a non-Redis backend)
        runs in: this batch's transaction, opened on first use."""
        from .backends.types import BackendCapabilityError

        pg = self._pg
        if pg is not None:
            if pg[0] is not backend:
                raise BackendCapabilityError(
                    "popoto.batch() holds a transaction on another Postgres "
                    "backend; one batch commits to one database"
                )
            if _in_bridge() and not hasattr(pg[2].conn, "async_connection"):
                from .backends.postgres.aio import BridgeMisuseError

                raise BridgeMisuseError(
                    "this popoto.batch() holds a Postgres transaction a sync "
                    "call opened, on a blocking connection: an async_* write "
                    "joining it would block the event loop for every statement. "
                    "Write to it with the sync methods (in a worker thread from "
                    "async code), or open the batch with an async_* call"
                )
            return pg[2]
        if self.command_stack:
            raise BackendCapabilityError(_MIXED)
        stack = contextlib.ExitStack()
        uow = stack.enter_context(backend.transaction())
        self._pg = (backend, stack, uow)
        return uow

    def _popoto_refuse_redis(self) -> None:
        if self._pg is not None:
            from .backends.types import BackendCapabilityError

            raise BackendCapabilityError(_MIXED)

    def _popoto_discard(self) -> None:
        """Roll the Postgres transaction back, if one is open."""
        pg = self._pg
        if pg is None:
            return
        self._pg = None
        _backend, stack, uow = pg
        # Nothing committed, so nothing to reap and no Redis side effect to
        # send: psycopg swallows the Rollback signal below, so the
        # transaction() context runs its after-commit step on this path too.
        uow.reap.clear()
        uow._after_commit.clear()
        # The stream appends (#759 M5) run only on the commit path, which a
        # rollback never reaches; cleared anyway so none can outlive it.
        uow._before_commit.clear()
        uow._stream_appends.clear()
        import psycopg

        # psycopg's transaction block swallows its own Rollback signal, so
        # this rolls back and returns the connection without raising.
        stack.__exit__(psycopg.Rollback, psycopg.Rollback(), None)

    # -- the pipeline API --------------------------------------------------

    def _popoto_async_unit(self) -> bool:
        """Whether this batch's Postgres transaction was opened by an
        ``async_*`` call: its connection belongs to an event loop, so only
        the async bridge can commit or roll it back."""
        pg = self._pg
        return pg is not None and hasattr(pg[2].conn, "async_connection")

    def _popoto_discard_later(self) -> None:
        """``reset()`` of an async-opened batch from sync code (a ``with``
        block's exit, ``reset()`` on the loop): roll the transaction back on
        its loop, as a task. Without a running loop nothing can drive the
        connection; the loop's pool closes it at the socket when the loop
        goes, and the server rolls it back."""
        pg = self._pg
        if pg is None:
            return
        self._pg = None
        backend, stack, uow = pg
        uow.reap.clear()
        uow._after_commit.clear()
        uow._before_commit.clear()
        uow._stream_appends.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        import psycopg

        from .backends.postgres.aio import async_backend

        twin = async_backend(backend)
        if twin is None:  # pragma: no cover - an async unit implies greenlet
            return
        task = loop.create_task(
            twin.run(stack.__exit__, psycopg.Rollback, psycopg.Rollback(), None)
        )
        _pending.add(task)
        task.add_done_callback(_rollback_done)

    async def async_execute(self, raise_on_error: bool = True) -> Any:
        """``await pipe.async_execute()``: :meth:`execute` from a coroutine.

        A batch whose Postgres transaction an ``async_*`` call opened
        (``await obj.async_save(pipeline=pipe)``) commits on the event loop,
        through the async backend; :meth:`execute` cannot drive that
        connection and raises ``BridgeMisuseError``. Any other batch -- Redis
        commands, or a transaction a sync call opened -- runs :meth:`execute`
        in a worker thread, so the loop never blocks on it."""
        pg = self._pg
        if pg is not None and self._popoto_async_unit():
            from .backends.postgres.aio import async_backend

            twin = async_backend(pg[0])
            assert twin is not None  # an async unit implies greenlet
            return await twin.run(self.execute, raise_on_error)
        return await asyncio.to_thread(self.execute, raise_on_error)

    async def async_reset(self) -> None:
        """``await pipe.async_reset()``: :meth:`reset` from a coroutine,
        rolling an async-opened Postgres transaction back before it
        returns."""
        pg = self._pg
        if pg is not None and self._popoto_async_unit():
            from .backends.postgres.aio import async_backend

            twin = async_backend(pg[0])
            assert twin is not None
            await twin.run(self.reset)
            return
        self.reset()

    def pipeline_execute_command(self, *args: Any, **options: Any) -> Any:
        self._popoto_refuse_redis()
        return super().pipeline_execute_command(*args, **options)

    def execute(self, raise_on_error: bool = True) -> Any:
        """Execute the queued Redis commands -- or commit the Postgres
        transaction (returning ``[]``). A Postgres batch in which a statement
        failed is rolled back whole, and this raises
        :class:`~popoto.backends.BackendError`."""
        pg = self._pg
        if pg is None:
            return super().execute(raise_on_error)
        _backend, stack, uow = pg
        if self._popoto_async_unit() and not _in_bridge():
            from .backends.postgres.aio import BridgeMisuseError

            raise BridgeMisuseError(
                "this popoto.batch() holds a Postgres transaction an async_* call "
                "opened, whose connection belongs to the event loop: commit it "
                "with `await pipe.async_execute()` (or roll it back with "
                "`await pipe.async_reset()`)"
            )
        from psycopg.pq import TransactionStatus

        if uow.conn.info.transaction_status == TransactionStatus.INERROR:
            self._popoto_discard()
            from .backends.types import BackendError

            raise BackendError(
                "popoto.batch(): a statement in this batch failed, so the whole "
                "Postgres transaction was rolled back and nothing in the batch "
                "was written"
            )
        self._pg = None
        stack.close()  # COMMIT; then the TTL reaper for the tables it wrote
        return []

    def reset(self) -> None:  # type: ignore[override]
        if self._popoto_async_unit() and not _in_bridge():
            self._popoto_discard_later()
        else:
            self._popoto_discard()
        super().reset()


def batch(transaction: bool = True) -> "Pipeline":
    """Open a batch of queued commands against the shared connection.

    Args:
        transaction: Wrap the queued commands in ``MULTI``/``EXEC`` so they
            apply atomically. Defaults to ``True``. ``False`` gives plain
            command pipelining with no atomicity -- Popoto's own
            annotate-and-close paths refuse such a pipeline. Postgres writes
            joined to the batch run in one transaction either way.

    Returns:
        A ``redis.client.Pipeline`` (a :class:`Batch`). Queue commands on it
        (directly, or by passing it as ``pipeline=`` to Popoto model and
        recipe calls), then call ``execute()``.
    """
    pipe = get_REDIS_DB().pipeline(transaction=transaction)
    # The client builds the guarded pipeline; reassigning the class (as
    # GuardedRedis.pipeline does) adds the Postgres side without touching the
    # constructor, whose signature moves between redis-py versions.
    pipe.__class__ = Batch
    return pipe


def join_unit(pipeline: Any, backend: Any) -> Any:
    """What a ``pipeline=`` argument becomes for a write to ``backend``.

    A :class:`Batch` handed to a non-Redis backend's write returns the
    batch's Postgres unit of work (opening it); handed to a Redis write while
    it holds a Postgres transaction, it raises
    :class:`~popoto.backends.BackendCapabilityError` before anything is
    queued. Everything else -- a plain pipeline, a unit of work, ``None``, a
    batch for a Redis write -- returns ``None``: the caller keeps its own
    handling, unchanged."""
    if not isinstance(pipeline, Batch):
        return None
    if getattr(backend, "name", "redis") == "redis":
        pipeline._popoto_refuse_redis()
        return None
    return pipeline._popoto_join(backend)


def unit_of(pipeline: Any, backend: Any) -> Any:
    """The unit of work a non-Redis write runs in: ``pipeline`` itself when
    it is one (a ``transaction()``), the batch's when it is a
    :class:`Batch`, else ``None`` (a plain Redis pipeline cannot carry a
    Postgres write)."""
    from .backends.types import UnitOfWork

    if isinstance(pipeline, UnitOfWork):
        return pipeline
    return join_unit(pipeline, backend)


def open_unit_of(pipeline: Any, backend: Any) -> Any:
    """The unit of work a *read* issued for a write to ``backend`` runs on
    (#776): ``pipeline`` when it is a non-Redis unit of work (a
    ``transaction()``), the transaction a :class:`Batch` already holds on
    ``backend``, else ``None`` -- an autocommit read. Never opens a
    transaction: a batch with no Postgres write yet holds no connection, so
    a read before its first write has no unit to join."""
    from .backends.types import UnitOfWork

    if isinstance(pipeline, UnitOfWork):
        return pipeline if pipeline.backend != "redis" else None
    if isinstance(pipeline, Batch):
        pg = pipeline._pg
        if pg is not None and pg[0] is backend:
            return pg[2]
    return None
