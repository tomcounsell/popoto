"""TombstonePriorStore — tombstones as negative prior (#494).

A tombstone (#491) is durable evidence that a particular kind of memory was
learned to be worthless. Until now nothing read that evidence, so the same
low-value content could be re-ingested, re-injected, re-dismissed and
re-forgotten indefinitely — the corpus relearned the same lesson forever. This
module makes the evidence transfer forward: it remembers *how many times* a
given content fingerprint has been buried, and ``WriteFilterMixin`` draws the
write-filter score of a matching new record down accordingly.

Why not the existing Bloom filter. ``ExistenceFilter`` already fingerprints
content and answers membership in O(1), which makes it the obvious candidate,
and it is the wrong one on three counts. Its ``on_save`` tokenizes the
fingerprint into words and its ``might_exist`` returns True when **any** single
token matches, so two records sharing one common word "match" — as a negative
prior that penalizes nearly every write. A Bloom filter cannot store a
per-entry burial count, which escalation requires. And its ``delete`` is a
documented no-op, so it cannot be bounded. ``ExistenceFilter`` is used here only
as the *source* of the fingerprint string, never as the matcher.

Matching is therefore exact: the fingerprint is whitespace-stripped,
case-folded, and hashed. Two records match only when their fingerprints are
equal under that normalization (or at a 2^-128 hash collision), which makes
"a dissimilar record is not penalized" an exact property rather than a tuned
threshold. Near-duplicate / paraphrase matching is deliberately out of scope;
it needs a retrieval at write time and a false-positive policy of its own.

The digest, not the raw fingerprint, is the hash field name — a fingerprint may
be user text, and this keyspace has no business holding content.

Like ``$TOMB:``, the keyspace is deliberately kept OUTSIDE the model's own
keyspace so that no query, index scan, or key-set walk can surface it. It is
purely derived state: dropping it entirely degrades the system to its
pre-#494 behavior and loses nothing else.

Keyspace:
    ``$TOMBPRIOR:{Model}:burials`` — hash, fingerprint digest -> burial count
    ``$TOMBPRIOR:{Model}:index``   — zset, fingerprint digest -> last burial ts
    ``$TOMBPRIOR:{Model}:stats``   — hash, {penalized, drawdown_total}

Every Redis call here is best-effort: a failure logs a warning and degrades to
"no penalty" or "burial not recorded". A memory system whose ``save()`` dies
because a telemetry hash is unreachable is worse than one that occasionally
misses a drawdown.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple, cast

from ..batch import batch as _batch

# The accessor, never a plain module-level import of the ``POPOTO_REDIS_DB``
# global: that import captures a snapshot, and ``set_REDIS_DB_settings()``
# rebinds ``redis_db``'s global without updating it, so the importer keeps
# issuing commands against the pre-reconfiguration client (#655). The
# anti-pattern is deliberately described here rather than quoted, so that a
# grep for it does not match this comment.
from ..redis_db import get_REDIS_DB
from .constants import Defaults

logger = logging.getLogger("POPOTO.TombstonePrior")

#: Namespace for the negative-prior structures, kept out of the model's own
#: keyspace so no query, index scan, or key-set walk can ever surface them.
TOMBSTONE_PRIOR_KEY_PREFIX = "$TOMBPRIOR"

#: Hash field names on the ``:stats`` hash.
STAT_PENALIZED = "penalized"
STAT_DRAWDOWN_TOTAL = "drawdown_total"


def digest_fingerprint(fingerprint: Optional[str]) -> Optional[str]:
    """Normalize and hash a fingerprint, or return None if there isn't one.

    Normalization is a whitespace strip plus a case fold, so trivial
    presentation differences do not defeat an exact match. An empty or
    whitespace-only fingerprint is *not* an identity — hashing it would give
    every content-less record the same digest and let them accumulate burials
    against each other — so it returns None and the caller skips.

    Args:
        fingerprint: The ExistenceFilter fingerprint string, or None.

    Returns:
        A 32-character hex digest, or None when there is no usable fingerprint.
    """
    if not fingerprint:
        return None
    normalized = str(fingerprint).strip().lower()
    if not normalized:
        return None
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).hexdigest()


def penalty_for(burials: int) -> float:
    """Return the multiplicative drawdown for ``burials`` prior burials.

    Zero burials is no evidence, so the score passes through untouched. Each
    subsequent burial compounds ``Defaults.TOMBSTONE_PRIOR_DECAY``, floored at
    ``Defaults.TOMBSTONE_PRIOR_FLOOR`` so suppression is asymptotic rather than
    absolute — a memory buried for situational reasons is drawn down, never
    annihilated.

    Both constants are read at call time so a runtime override (e.g.
    ``tests/benchmarks/overrides.apply_overrides``) is observed.
    """
    if burials <= 0:
        return 1.0
    return max(
        float(Defaults.TOMBSTONE_PRIOR_FLOOR),
        float(Defaults.TOMBSTONE_PRIOR_DECAY) ** burials,
    )


def _decoded_members(reply: Any) -> List[str]:
    """Decode a Redis ZSET member reply into ``str`` keys.

    redis-py types ``zrange`` as a union covering its ``withscores`` overload.
    This module never passes ``withscores``, so the reply is always a flat list
    of members; the cast records that rather than blanket-ignoring the error.
    """
    return [m.decode() if isinstance(m, bytes) else m for m in cast(Any, reply)]


class TombstonePriorStore:
    """Owns the ``$TOMBPRIOR:{Model}:*`` keyspace for a single model class.

    Sibling of ``TombstoneStore``: same out-of-model-keyspace convention, same
    pipelined-pairs construction, same ``get_REDIS_DB()`` accessor. Where
    ``TombstoneStore`` archives *what died*, this store remembers *how often a
    given shape of content has died*.

    Args:
        model_class: The Popoto Model class whose negative prior this store
            manages. Only ``model_class.__name__`` is used, to derive the
            keyspace.
    """

    def __init__(self, model_class: Any):
        self.model_class = model_class

    def keys(self) -> Tuple[str, str, str]:
        """Return the (burials hash, recency index, stats hash) Redis keys."""
        name = self.model_class.__name__
        return (
            f"{TOMBSTONE_PRIOR_KEY_PREFIX}:{name}:burials",
            f"{TOMBSTONE_PRIOR_KEY_PREFIX}:{name}:index",
            f"{TOMBSTONE_PRIOR_KEY_PREFIX}:{name}:stats",
        )

    # -- burial side --------------------------------------------------------

    def record_burial(self, fingerprint: Optional[str], ts: float) -> bool:
        """Record that ``fingerprint`` was buried at ``ts``.

        Increments the burial count and refreshes the recency index in one
        transactional pipeline, then enforces the retention bound.

        Best-effort by contract: the caller (``MemoryLifecycle.tombstone``) has
        already archived and removed the record, and a failure to record the
        negative evidence must never roll that back.

        Returns:
            True if a burial was recorded, False if there was no usable
            fingerprint or the write failed.
        """
        key = digest_fingerprint(fingerprint)
        if key is None:
            return False
        burials_key, index_key, _ = self.keys()
        try:
            pipeline = _batch()
            pipeline.hincrby(burials_key, key, 1)
            pipeline.zadd(index_key, {key: ts})
            pipeline.execute()
        except Exception as exc:
            logger.warning("tombstone prior: burial write failed for %s: %s", key, exc)
            return False
        self._enforce_limit()
        return True

    def _enforce_limit(self) -> int:
        """Age out the oldest digests beyond ``Defaults.TOMBSTONE_PRIOR_LIMIT``.

        Same shape and same error posture as
        ``MemoryLifecycle._enforce_tombstone_retention``: a failed sweep is
        logged and swallowed, because the burial it follows is already durable
        and an unbounded-by-one keyspace is not worth raising into a caller.

        Returns:
            The number of digests evicted.
        """
        burials_key, index_key, _ = self.keys()
        limit = int(Defaults.TOMBSTONE_PRIOR_LIMIT)
        try:
            excess = int(get_REDIS_DB().zcard(index_key)) - limit
            if excess <= 0:
                return 0
            keys = _decoded_members(get_REDIS_DB().zrange(index_key, 0, excess - 1))
            if not keys:
                return 0
            pipeline = _batch()
            pipeline.hdel(burials_key, *keys)
            pipeline.zrem(index_key, *keys)
            pipeline.execute()
        except Exception as exc:
            logger.warning("tombstone prior: retention sweep failed: %s", exc)
            return 0
        logger.debug("aged out %d prior entr(ies) past limit %d", len(keys), limit)
        return len(keys)

    # -- read side ----------------------------------------------------------

    def burial_count(self, fingerprint: Optional[str]) -> int:
        """Return how many times ``fingerprint`` has been buried.

        Returns 0 — meaning "no negative evidence, no penalty" — for a missing
        fingerprint, a missing digest, an unreachable Redis, or a stored value
        that does not read as a non-negative integer. Every one of those is a
        reason to leave the write alone rather than to fail it.
        """
        key = digest_fingerprint(fingerprint)
        if key is None:
            return 0
        burials_key, _, _ = self.keys()
        try:
            raw = get_REDIS_DB().hget(burials_key, key)
        except Exception as exc:
            logger.warning("tombstone prior: burial read failed for %s: %s", key, exc)
            return 0
        if raw is None:
            return 0
        try:
            count = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "tombstone prior: burial count for %s is not an integer (%r), "
                "treating as 0",
                key,
                raw,
            )
            return 0
        return count if count > 0 else 0

    # -- telemetry ----------------------------------------------------------

    def note_penalty(self, before: float, after: float) -> None:
        """Count one penalized write and the score it gave up.

        Both counters move in one transactional pipeline so they can never
        disagree about how much drawdown the penalized writes account for.
        """
        _, _, stats_key = self.keys()
        try:
            pipeline = _batch()
            pipeline.hincrby(stats_key, STAT_PENALIZED, 1)
            pipeline.hincrbyfloat(stats_key, STAT_DRAWDOWN_TOTAL, before - after)
            pipeline.execute()
        except Exception as exc:
            logger.warning("tombstone prior: telemetry write failed: %s", exc)

    def stats(self) -> Dict[str, float]:
        """Return the drawdown telemetry for this model.

        Returns:
            ``{"penalized": int, "drawdown_total": float}`` — zeros when
            nothing has been penalized or the read fails, so a caller can
            always render the numbers.
        """
        _, _, stats_key = self.keys()
        result: Dict[str, float] = {STAT_PENALIZED: 0, STAT_DRAWDOWN_TOTAL: 0.0}
        try:
            raw = cast(Any, get_REDIS_DB().hgetall(stats_key)) or {}
        except Exception as exc:
            logger.warning("tombstone prior: stats read failed: %s", exc)
            return result
        for field, coerce in ((STAT_PENALIZED, int), (STAT_DRAWDOWN_TOTAL, float)):
            value = raw.get(field.encode()) if raw else None
            if value is None and raw:
                value = raw.get(field)
            if value is None:
                continue
            try:
                result[field] = coerce(value)
            except (TypeError, ValueError):
                logger.warning(
                    "tombstone prior: stat %s is unreadable (%r), reporting 0",
                    field,
                    value,
                )
        return result

    # -- maintenance --------------------------------------------------------

    def count(self) -> int:
        """Return how many distinct buried fingerprints are tracked."""
        _, index_key, _ = self.keys()
        return int(cast(Any, get_REDIS_DB().zcard(index_key)))

    def purge_all(self) -> int:
        """Drop the whole negative prior for this model: one ``DEL``.

        The count read is best-effort — the return value is a report, the
        delete is the job.
        """
        try:
            count = self.count()
        except Exception:
            count = 0
        get_REDIS_DB().delete(*self.keys())
        return count
