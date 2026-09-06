"""One transaction, opened without reaching for the client (#630).

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

What this buys is that recipes no longer import the Redis client to open a
transaction. It is *not* on its own a seam a storage backend could swap: the
twenty ``isinstance`` sites above would have to change first.
"""

from typing import TYPE_CHECKING

from .redis_db import POPOTO_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.client import Pipeline


def batch(transaction: bool = True) -> "Pipeline":
    """Open a batch of queued commands against the shared connection.

    Args:
        transaction: Wrap the queued commands in ``MULTI``/``EXEC`` so they
            apply atomically. Defaults to ``True``. ``False`` gives plain
            command pipelining with no atomicity -- Popoto's own
            annotate-and-close paths refuse such a pipeline.

    Returns:
        A ``redis.client.Pipeline``. Queue commands on it (directly, or by
        passing it as ``pipeline=`` to Popoto model and recipe calls), then
        call ``execute()``.
    """
    return POPOTO_REDIS_DB.pipeline(transaction=transaction)
