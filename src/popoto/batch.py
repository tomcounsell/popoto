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
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Optional

from .redis_db import GuardedPipeline, get_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.client import Pipeline

__all__ = ["Batch", "batch", "join_unit", "unit_of"]

_MIXED = (
    "popoto.batch() cannot mix Redis and Postgres writes: a Redis MULTI/EXEC "
    "and a Postgres transaction cannot commit atomically together, so the "
    "batch would only look atomic. Open one batch per backend."
)


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
        import psycopg

        # psycopg's transaction block swallows its own Rollback signal, so
        # this rolls back and returns the connection without raising.
        stack.__exit__(psycopg.Rollback, psycopg.Rollback(), None)

    # -- the pipeline API --------------------------------------------------

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
