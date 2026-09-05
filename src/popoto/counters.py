"""Durable named counters: the counter primitive recipes use (#630).

A counter is one Redis string holding an integer. ``increment`` is
``INCRBY`` and returns the running total; ``read`` is ``GET`` and reports
``0`` for a key that has never been touched. Both are atomic on the server,
so concurrent writers from several processes always converge on the true
sum.

The key string is the caller's contract. Recipes compose it themselves
(``f"{EVICTION_COUNTER_PREFIX}:{agent_id}:evicted"`` in
:mod:`popoto.recipes.default_memory`) so the layout that
``MemoryService._read_counters()`` scans stays exactly where the recipe
declares it. This module adds no prefix and rewrites no key.

Import by path (``from popoto import counters``); it is deliberately absent
from the ``popoto`` package namespace.
"""

from .redis_db import POPOTO_REDIS_DB


def increment(key: str, delta: int = 1) -> int:
    """Add ``delta`` to the counter at ``key`` and return the new total.

    Creates the key at ``delta`` when it does not exist (``INCRBY``
    semantics). ``delta`` may be ``0`` to read-through atomically.
    """
    return int(POPOTO_REDIS_DB.incrby(key, delta))


def read(key: str) -> int:
    """Current value of the counter at ``key``, or ``0`` when absent."""
    raw = POPOTO_REDIS_DB.get(key)
    return int(raw) if raw is not None else 0
