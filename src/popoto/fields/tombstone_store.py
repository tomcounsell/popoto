"""TombstoneStore — field-layer keeper of the tombstone keyspace (#649).

Relocates the raw-Redis tombstone bookkeeping that used to live inline in
``popoto.recipes.memory_lifecycle`` (issue #491) into the field layer, as
part of the #630 "route recipes through the field layer" series. This is a
relocation, not a redesign: every method here issues the exact same Redis
commands, in the same order, as the recipe code it replaces, so wire
behavior is byte-identical.

The tombstone keyspace is deliberately kept OUTSIDE the model's own
keyspace (``$TOMB:{Model}:data`` / ``$TOMB:{Model}:index`` rather than any
``{Model}:*`` key) so that no query, index scan, or key-set walk can ever
surface a tombstoned record. That property must be preserved exactly —
tombstones are NOT a popoto Model; that was explicitly considered and
rejected.

Keyspace:
    ``$TOMB:{Model}:data``  — hash, redis_key -> msgpack-packed entry bytes
    ``$TOMB:{Model}:index`` — zset, redis_key -> death timestamp (score)
"""

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

import msgpack

from ..batch import batch as _batch

# The accessor, not ``from ..redis_db import POPOTO_REDIS_DB``: that plain
# import captures a snapshot, and ``set_REDIS_DB_settings()`` rebinds
# ``redis_db``'s global without updating it, so the importer keeps issuing
# commands against the pre-reconfiguration client (#655). This module is new
# code, so it takes the safe shape from the start.
from ..redis_db import get_REDIS_DB

# Rebound to the EXACT SAME logger name as the original recipe code. Do not
# derive this from the new module's __name__ — tests/test_memory_lifecycle.py
# (test_partial_tombstone_entry_is_dropped_not_inflated, ~L1145) asserts on
# this warning via `caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle")`.
# Renaming this logger breaks that test with no obvious cause.
logger = logging.getLogger("POPOTO.MemoryLifecycle")

# Namespace for tombstone structures, kept out of the model's own keyspace so
# no query, index scan, or key-set walk can ever surface a tombstoned record.
TOMBSTONE_KEY_PREFIX = "$TOMB"


# ---------------------------------------------------------------------------
# Tombstone — the record of a forgetting (#491)
# ---------------------------------------------------------------------------


@dataclass
class Tombstone:
    """Durable record that a memory was forgotten, and what it looked like.

    Forgetting tombstones rather than deletes so an aggressive low-confidence
    forget policy stays reversible (Risk 6) and so each death becomes negative
    evidence a future write path can consult (#494).

    Attributes:
        redis_key: The forgotten record's Redis key. Restore handle.
        fingerprint: ExistenceFilter fingerprint of the dead record — the
            identity token #494 matches new writes against.
        tier: Tier the record held at death.
        importance_at_death: Importance score at the moment of forgetting.
        confidence_at_death: ConfidenceField value at death, or None if the
            model carries no confidence signal.
        evidence_count: Observations backing that confidence.
        dismissal_count: Contradiction/dismissal count at death.
        tombstoned_at: Unix timestamp of the forgetting.
        reason: Free-form marker for what triggered it.
    """

    redis_key: str
    fingerprint: str
    tier: str
    importance_at_death: float
    confidence_at_death: Optional[float]
    evidence_count: int
    dismissal_count: int
    tombstoned_at: float
    reason: str = "policy"


_TOMBSTONE_FIELDS: Tuple[str, ...] = tuple(Tombstone.__dataclass_fields__)

# Fields with no dataclass default must be present in a stored entry — a
# Tombstone missing one of them is not a Tombstone, it is a None-filled shell.
_TOMBSTONE_REQUIRED_FIELDS: Tuple[str, ...] = tuple(
    name
    for name, f in Tombstone.__dataclass_fields__.items()
    if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
)


def _sync(reply: Any) -> Any:
    """Narrow a redis-py reply out of its ``Awaitable[T] | T`` union.

    redis-py shares one set of command stubs between its sync and asyncio
    clients, so every command is typed as possibly-awaitable. ``get_REDIS_DB()``
    always returns the sync client, so these replies are never awaitable. Which
    call sites this affects varies by redis-py version (7.x flags several that
    8.x does not), so the narrowing is centralized here rather than sprinkled
    as per-site ``type: ignore`` comments that would go stale in either
    direction.
    """
    return reply


def _decoded_members(reply: Any) -> List[str]:
    """Decode a Redis ZSET member reply into ``str`` keys.

    redis-py types ``zrange``/``zrevrange`` as a union covering their
    ``withscores`` overloads (member, or ``(member, score)`` pairs). These
    call sites never pass ``withscores``, so the reply is always a flat list
    of members — the cast records that, rather than blanket-ignoring the
    resulting type error at each call site.
    """
    return [
        m.decode() if isinstance(m, bytes) else m for m in cast(Iterable[Any], reply)
    ]


def _unpack_tombstone_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode a stored tombstone entry, or None if absent/corrupt/incomplete.

    A partial or foreign msgpack dict under ``$TOMB:{Model}:data`` is dropped
    on the same log-and-skip path as undecodable bytes, rather than being
    inflated into a Tombstone whose non-Optional fields are all None.
    """
    if raw is None:
        return None
    try:
        entry = msgpack.unpackb(raw, raw=False)
    except Exception as exc:
        logger.warning("tombstone entry is undecodable, skipping: %s", exc)
        return None
    if not isinstance(entry, dict):
        logger.warning(
            "tombstone entry is not a mapping (%s), skipping", type(entry).__name__
        )
        return None
    missing = [k for k in _TOMBSTONE_REQUIRED_FIELDS if k not in entry]
    if missing:
        logger.warning("tombstone entry is missing required keys %s, skipping", missing)
        return None
    return entry


def _tombstone_from_entry(entry: Dict[str, Any]) -> Tombstone:
    """Build a Tombstone from a stored entry, ignoring the archived payload.

    Callers must pass an entry that has already cleared
    ``_unpack_tombstone_entry``, which guarantees every required key is present.
    """
    return Tombstone(**{k: entry[k] for k in _TOMBSTONE_FIELDS if k in entry})


# ---------------------------------------------------------------------------
# TombstoneStore
# ---------------------------------------------------------------------------


class TombstoneStore:
    """Owns the ``$TOMB:{Model}:*`` keyspace for a single model class.

    Field-layer keeper of tombstone reads/writes. Every method issues the
    exact same Redis commands, in the same order, as the raw-Redis recipe
    code it replaces (see the table in issue #649). Callers (e.g.
    ``MemoryLifecycle``) are responsible for msgpack-packing entries before
    calling ``archive()`` and for unpacking what ``get_entry``/``get_entries``
    return — this store deals in raw bytes at that boundary, matching where
    the recipe drew the line.

    Args:
        model_class: The Popoto Model class whose tombstones this store
            manages. Only ``model_class.__name__`` is used, to derive the
            keyspace.
    """

    def __init__(self, model_class: Any):
        self.model_class = model_class

    def keys(self) -> Tuple[str, str]:
        """Return the (data hash, recency index) Redis keys for tombstones."""
        name = self.model_class.__name__
        return (
            f"{TOMBSTONE_KEY_PREFIX}:{name}:data",
            f"{TOMBSTONE_KEY_PREFIX}:{name}:index",
        )

    def archive(self, redis_key: str, entry_bytes: bytes, ts: float) -> None:
        """Write a tombstone entry: HSET the data hash, ZADD the recency index.

        Both commands are queued in one transactional pipeline (``HSET`` then
        ``ZADD``, matching the original write order) and executed together.
        """
        data_key, index_key = self.keys()
        pipeline = _batch()
        pipeline.hset(data_key, redis_key, entry_bytes)
        pipeline.zadd(index_key, {redis_key: ts})
        pipeline.execute()

    def count(self) -> int:
        """Return the number of retained tombstones (``ZCARD`` on the index)."""
        _, index_key = self.keys()
        return int(_sync(get_REDIS_DB().zcard(index_key)))

    def oldest_keys(self, n: int) -> List[str]:
        """Return up to ``n`` oldest tombstoned keys (``ZRANGE`` 0..n-1)."""
        _, index_key = self.keys()
        raw = get_REDIS_DB().zrange(index_key, 0, n - 1)
        return _decoded_members(raw)

    def newest_keys(self, stop: int) -> List[str]:
        """Return keys newest-death-first up to ``stop`` (``ZREVRANGE`` 0..stop)."""
        _, index_key = self.keys()
        raw = get_REDIS_DB().zrevrange(index_key, 0, stop)
        return _decoded_members(raw)

    def evict(self, keys: List[str]) -> None:
        """Remove tombstones for ``keys``: ``HDEL`` data + ``ZREM`` index.

        Both commands are queued in one transactional pipeline (``HDEL`` then
        ``ZREM``, matching the original eviction order) and executed together.
        """
        data_key, index_key = self.keys()
        pipeline = _batch()
        pipeline.hdel(data_key, *keys)
        pipeline.zrem(index_key, *keys)
        pipeline.execute()

    def get_entry(self, redis_key: str) -> Any:
        """Return the raw stored entry bytes for one key (``HGET``), or None."""
        data_key, _ = self.keys()
        return get_REDIS_DB().hget(data_key, redis_key)

    def get_entries(self, keys: List[str]) -> List[Any]:
        """Return raw stored entry bytes for ``keys`` (``HMGET``).

        Preserves argument order, with ``None`` holes for missing members —
        callers rely on positional correspondence with ``keys``.
        """
        data_key, _ = self.keys()
        return _sync(get_REDIS_DB().hmget(data_key, keys))

    def purge(self, redis_key: str) -> bool:
        """Drop one tombstone permanently: ``HDEL`` data + ``ZREM`` index.

        Both commands are queued in one transactional pipeline and executed
        together. Returns True if a tombstone was actually removed.
        """
        data_key, index_key = self.keys()
        pipeline = _batch()
        pipeline.hdel(data_key, redis_key)
        pipeline.zrem(index_key, redis_key)
        removed = pipeline.execute()
        return bool(removed and removed[0])

    def purge_all(self) -> int:
        """Drop every retained tombstone for this model: one ``DEL`` over both keys.

        The count read is best-effort and its failure is swallowed: the caller
        this was relocated from (``MemoryLifecycle.purge_all_tombstones``) read
        the count through an error-swallowing helper and issued the ``DEL``
        regardless. A failed count must not leave the tombstones in place — the
        return value is a report, the delete is the job.
        """
        try:
            count = self.count()
        except Exception:
            count = 0
        get_REDIS_DB().delete(*self.keys())
        return count
