"""AccessTrackerMixin — read pattern tracking with staged vs confirmed reads.

This module provides a mixin class that adds read access tracking to any Model.
It uses a staging pattern: reads are first recorded to a staging list, then
atomically promoted to the confirmed access log via a Lua script.

Design:
    - on_read() appends a timestamp to a staging list (cheap, fire-and-forget)
    - confirm_access() atomically promotes staged timestamps to the confirmed log
    - discard_staged_access() discards staged reads without affecting confirmed data
    - Access log is capped at max_access_log entries (default 100)

Redis Key Patterns:
    - $AT:{ClassName}:staged:{redis_key} — staging list (RPUSH timestamps)
    - $AT:{ClassName}:access_log:{redis_key} — confirmed access timestamps
    - $AT:{ClassName}:meta:{redis_key} — hash with access_count and last_accessed

Example:
    class Memory(AccessTrackerMixin, Model):
        key = UniqueKeyField()
        content = StringField()

    memory = Memory.query.get(key="important")  # auto-stages on_read
    memory.confirm_access()  # promote staged reads to confirmed log
    print(memory.access_count)  # total confirmed read count
    print(memory.last_accessed)  # timestamp of most recent confirmed read
"""

import logging
import time

from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.AccessTracker")

# Lua script: atomically promote staged reads to confirmed access log.
# KEYS[1] = staged list key
# KEYS[2] = access log key
# KEYS[3] = meta hash key
# ARGV[1] = max_access_log (cap)
CONFIRM_ACCESS_LUA = """
local staged = redis.call('LRANGE', KEYS[1], 0, -1)
if #staged == 0 then return 0 end
for _, ts in ipairs(staged) do
    redis.call('RPUSH', KEYS[2], ts)
end
redis.call('LTRIM', KEYS[2], -tonumber(ARGV[1]), -1)
redis.call('HINCRBY', KEYS[3], 'access_count', #staged)
redis.call('HSET', KEYS[3], 'last_accessed', staged[#staged])
redis.call('DEL', KEYS[1])
return #staged
"""


class AccessTrackerMixin:
    """Mixin that adds read access tracking to any Model.

    Add this as a base class alongside Model to enable read tracking:

        class MyModel(AccessTrackerMixin, Model):
            ...

    Class Attributes:
        _max_access_log: Maximum number of timestamps to keep in the access log.
            Older entries are trimmed on confirm. Default 100.
        _track_reads: Whether to automatically track reads from queries.
            Default True.
        _staged_ttl_seconds: TTL applied to the staging list on every on_read()
            call. Default 86400 (24h). Magic-number tuning knob — increase if
            your stage→confirm cadence exceeds 24h. See on_read() docstring for
            the staged-read TTL contract.

    Note: Attributes are prefixed with underscore to avoid conflict with
    Popoto's ModelBase metaclass, which requires public attributes to be Fields.
    """

    _max_access_log = 100
    _track_reads = True
    _staged_ttl_seconds: int = (
        86400  # 24h — magic-number tuning knob; see on_read() docstring
    )

    # Export/import: this is a Model-level mixin, reached by the transfer
    # driver's MRO walk rather than by iterating _meta.fields, so its
    # export_state/import_state take the same signature minus ``field_name``.
    # The meta hash counters are carried; the confirmed access log and any
    # staged reads are a function of read *history* and are not.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "access_count and last_accessed are carried; the confirmed access log "
        "($AT:{Class}:access_log:{key}) and staged reads are not carried or "
        "rebuilt by import; see #556"
    )

    @classmethod
    def export_state(cls, model_instance, **kwargs):
        """Export the access-tracker meta counters for one instance.

        Returns:
            ``{"access_count": int, "last_accessed": float | None}``, or
            ``None`` when this instance has never had a confirmed access.
        """
        meta_key = model_instance._at_key("meta")
        raw_count = POPOTO_REDIS_DB.hget(meta_key, "access_count")
        raw_last = POPOTO_REDIS_DB.hget(meta_key, "last_accessed")
        if raw_count is None and raw_last is None:
            return None

        state = {}
        if raw_count is not None:
            try:
                state["access_count"] = int(raw_count)
            except (TypeError, ValueError):
                logger.warning(f"Non-numeric access_count in {meta_key}; skipping")
        if raw_last is not None:
            try:
                state["last_accessed"] = float(raw_last)
            except (TypeError, ValueError):
                logger.warning(f"Non-numeric last_accessed in {meta_key}; skipping")
        return state or None

    @classmethod
    def import_state(cls, model_instance, state, **kwargs):
        """Restore the access-tracker meta counters after import.

        ``access_count`` and ``last_accessed`` are exposed only as read-only
        properties, so this writes the meta hash directly -- the same hash
        ``CONFIRM_ACCESS_LUA`` maintains. The access log itself is not
        restored (see ``roundtrip_note``), so a subsequent
        ``confirm_access()`` continues the carried count rather than
        restarting it.
        """
        if not state:
            return None

        mapping = {}
        if state.get("access_count") is not None:
            mapping["access_count"] = int(state["access_count"])
        if state.get("last_accessed") is not None:
            mapping["last_accessed"] = float(state["last_accessed"])
        if mapping:
            POPOTO_REDIS_DB.hset(model_instance._at_key("meta"), mapping=mapping)
        return None

    def _at_key(self, kind):
        """Build an access tracker Redis key.

        Args:
            kind: One of 'staged', 'access_log', 'meta'

        Returns:
            str: Redis key like '$AT:ClassName:kind:redis_key'
        """
        class_name = type(self).__name__
        redis_key = self._redis_key or self.db_key.redis_key
        return f"$AT:{class_name}:{kind}:{redis_key}"

    def on_read(self, pipeline=None):
        """Record a read access by staging a timestamp.

        Appends the current timestamp to the staging list and refreshes the
        TTL on the staged key. This is a cheap operation suitable for
        fire-and-forget use from query hooks.

        Staged-read TTL contract:
            Applications MUST call ``confirm_access()`` within
            ``_staged_ttl_seconds`` (default 24h) of staging reads via
            ``on_read()``. Reads left unconfirmed past the TTL window are
            permanently dropped from ``access_count`` / ``last_accessed`` by
            design. Set ``_staged_ttl_seconds`` higher if a longer
            stage→confirm cadence is required. The ``_staged_ttl_seconds``
            constant is a magic-number tuning knob — it is not user-facing
            configuration.

        Args:
            pipeline: Optional Redis pipeline for batch operations. When
                provided, the RPUSH and EXPIRE are queued in the same pipeline
                call so they execute atomically.
        """
        ts = str(time.time())
        staged_key = self._at_key("staged")
        if pipeline:
            pipeline.rpush(staged_key, ts)
            pipeline.expire(staged_key, self._staged_ttl_seconds)
        else:
            POPOTO_REDIS_DB.rpush(staged_key, ts)
            POPOTO_REDIS_DB.expire(staged_key, self._staged_ttl_seconds)

    def confirm_access(self, pipeline=None):
        """Atomically promote staged reads to the confirmed access log.

        Uses a Lua script to:
        1. Move all staged timestamps to the access log
        2. Trim the log to max_access_log entries
        3. Update access_count and last_accessed in the meta hash
        4. Delete the staging list

        TTL contract (confirm side):
            A staged key that expired before confirm contributes 0 to
            ``access_count`` and does not update ``last_accessed`` — this is
            by design (see on_read() staged-read TTL contract). When this
            happens a DEBUG log is emitted.

        Args:
            pipeline: Optional Redis pipeline (not used for Lua eval,
                reserved for future use).

        Returns:
            int: Number of staged reads that were promoted (0 if the staged
                key was empty or had already expired).

        Raises:
            TypeError: If the model instance has not been saved to Redis.
        """
        if not hasattr(self, "_redis_key") and not hasattr(self, "db_key"):
            raise TypeError("confirm_access() requires a saved model instance")
        try:
            redis_key = self._redis_key or self.db_key.redis_key
        except Exception:
            raise TypeError("confirm_access() requires a saved model instance")

        # Check if the model has been saved (has a valid redis key in the DB)
        if not POPOTO_REDIS_DB.exists(redis_key):
            raise TypeError("confirm_access() requires a saved model instance")

        staged_key = self._at_key("staged")
        log_key = self._at_key("access_log")
        meta_key = self._at_key("meta")

        count = POPOTO_REDIS_DB.eval(
            CONFIRM_ACCESS_LUA,
            3,  # number of KEYS
            staged_key,
            log_key,
            meta_key,
            str(self._max_access_log),
        )
        count = int(count)
        if count == 0:
            logger.debug(
                "AccessTracker: staged key empty/expired at confirm for %s — read dropped (TTL contract)",
                self.db_key,
            )
        return count

    def discard_staged_access(self, pipeline=None):
        """Discard all staged reads without affecting confirmed data.

        Args:
            pipeline: Optional Redis pipeline for batch operations.
        """
        staged_key = self._at_key("staged")
        if pipeline:
            pipeline.delete(staged_key)
        else:
            POPOTO_REDIS_DB.delete(staged_key)

    @property
    def access_count(self):
        """Total number of confirmed read accesses.

        Returns:
            int: The cumulative access count, or 0 if never confirmed.
        """
        meta_key = self._at_key("meta")
        raw = POPOTO_REDIS_DB.hget(meta_key, "access_count")
        if raw is None:
            return 0
        return int(raw)

    @property
    def last_accessed(self):
        """Timestamp of the most recent confirmed read access.

        Returns:
            float or None: Unix timestamp, or None if never confirmed.
        """
        meta_key = self._at_key("meta")
        raw = POPOTO_REDIS_DB.hget(meta_key, "last_accessed")
        if raw is None:
            return None
        return float(raw)

    def _delete_access_tracker_keys(self, pipeline=None):
        """Remove all access tracker Redis keys for this instance.

        Called during model deletion to clean up tracking data.

        Args:
            pipeline: Optional Redis pipeline for batch operations.
        """
        keys = [
            self._at_key("staged"),
            self._at_key("access_log"),
            self._at_key("meta"),
        ]
        if pipeline:
            for key in keys:
                pipeline.delete(key)
        else:
            POPOTO_REDIS_DB.delete(*keys)
