"""Event streams: :class:`StreamConsumer` and the backend-neutral stream
client (#759 M5).

A stream lives where the model that writes it lives: a Redis stream for a
Redis-bound ``EventStreamMixin`` model, the events tables for a
Postgres-bound one. :func:`stream_client` returns the client that holds it --
the Redis client, or the Postgres backend's redis-py-shaped stream store --
so ``xrange``, ``xreadgroup``, ``xpending`` and the rest read the same on
both.
"""

from typing import Any, Optional

from .consumer import StreamConsumer

__all__ = ["StreamConsumer", "resolve_stream_backend", "stream_client"]


def _stream_models_for(stream_key: str) -> list[Any]:
    from ..fields.event_stream import STREAM_MODELS

    key = stream_key
    while key.startswith("dead:"):
        key = key[len("dead:") :]
    found = []
    for model in list(STREAM_MODELS):
        name = getattr(model, "_stream_name", None)
        if not name or not hasattr(model, "_meta"):
            continue
        base = f"stream:{name}"
        if key == base or key.startswith(base + ":"):
            found.append(model)
    return found


def resolve_stream_backend(
    stream_key: Any = None, *, model: Any = None, backend: Any = None
) -> Any:
    """The backend that holds ``stream_key``: ``backend`` when given, else
    ``model``'s, else the backend of the ``EventStreamMixin`` models that
    write that key (``stream:<name>`` or ``stream:<name>:<partition>``, and
    their ``dead:`` letter streams) when they all agree, else the process
    default. Resolving issues no command on either backend."""
    from ..backends import _instance, _resolve, get_backend

    if backend is not None:
        return _instance(backend) if isinstance(backend, str) else backend
    if model is not None:
        return get_backend(model)
    if stream_key is not None:
        if isinstance(stream_key, bytes):
            stream_key = stream_key.decode("utf-8", "replace")
        backends = {
            id(b): b for b in (_resolve(m) for m in _stream_models_for(str(stream_key)))
        }
        if len(backends) == 1:
            return next(iter(backends.values()))
    return get_backend()


def stream_client(
    model: Any = None, *, stream_key: Optional[Any] = None, backend: Any = None
) -> Any:
    """The synchronous client for a stream: ``popoto.get_redis()``'s client
    when the stream is on Redis, the Postgres backend's
    :class:`~popoto.backends.postgres.events.StreamStore` when it is on
    Postgres. Both answer the redis-py stream commands with the same
    replies."""
    resolved = resolve_stream_backend(stream_key, model=model, backend=backend)
    if getattr(resolved, "name", "redis") == "redis":
        from ..redis_db import get_REDIS_DB

        return get_REDIS_DB()
    return resolved.streams()
