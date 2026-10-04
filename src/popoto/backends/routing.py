"""Field-layer routing to a non-Redis backend (#759 M2a).

The memory fields and mixins keep today's Redis code exactly as it is (gate
(a): the Redis wire does not change) and branch to the model's backend only
when that backend is not Redis. This module is the one place that branch is
decided, so it reads the same everywhere.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["non_redis_backend", "record_id_of"]


def non_redis_backend(model: Any) -> Optional[Any]:
    """The model's backend when it is not Redis, else ``None``.

    ``model`` is a ``Model`` class or instance. Resolving the backend binds it
    lazily (no network: ``bind()`` only compiles the table spec) and issues no
    Redis command, so a Redis-bound model's wire is unchanged by the check.
    """
    from . import get_backend

    model_cls = model if isinstance(model, type) else type(model)
    if not hasattr(model_cls, "_meta"):
        return None
    backend = get_backend(model_cls)
    return None if backend.name == "redis" else backend


def record_id_of(instance: Any) -> Any:
    """The :class:`~popoto.backends.RecordId` of a saved (or keyable)
    instance: the key it was last saved under, else its current key."""
    from .types import RecordId

    key = getattr(instance, "_redis_key", None) or instance.db_key.redis_key
    return RecordId.from_key(instance._meta.model_name, key)
