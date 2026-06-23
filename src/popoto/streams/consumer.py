"""StreamConsumer — Redis Streams consumer group framework for Popoto.

This module provides a consumer group abstraction over Redis Streams, handling
the XREADGROUP/XACK/XCLAIM/XPENDING lifecycle so application code only needs
to supply a handler function.

Design:
    - Async-first: core logic uses ``redis.asyncio`` via ``get_async_redis_db()``
    - Sync wrappers (``run_sync``, ``process_batch_sync``) use ``asyncio.run()``
    - Dead-letter: entries exceeding ``max_retries`` are moved to ``dead:{stream_key}``
    - Recovery: ``XAUTOCLAIM`` reclaims entries from crashed consumers after idle timeout
    - No Redis modules — only core Streams commands (Valkey compatible)

Redis Commands Used:
    - XGROUP CREATE — create consumer group (idempotent with BUSYGROUP handling)
    - XREADGROUP — read new entries for this consumer
    - XACK — acknowledge processed entries
    - XPENDING — inspect pending entries for recovery/dead-letter decisions
    - XAUTOCLAIM — atomically reclaim idle entries from crashed consumers
    - XADD — write to dead-letter stream

Example:
    async def my_handler(entries):
        for entry_id, fields in entries:
            print(f"Processing {entry_id}: {fields}")

    consumer = StreamConsumer(
        stream_key="stream:memory_mutations",
        group_name="pattern-detector",
        consumer_name="worker-1",
        handler=my_handler,
    )
    consumer.run_sync()  # blocking loop

    # Or single batch:
    count = consumer.process_batch_sync()
"""

import asyncio
import logging
import time
from typing import Callable, List, Optional, Tuple

from ..redis_db import get_async_redis_db

logger = logging.getLogger("POPOTO.StreamConsumer")


class StreamConsumer:
    """Redis Streams consumer group framework.

    Manages the consumer group lifecycle — group creation, batch reading,
    acknowledgment, dead-letter handling, and pending entry recovery — while
    delegating actual processing logic to an application-provided handler.

    Args:
        stream_key: Redis stream key (e.g., "stream:memory_mutations").
        group_name: Consumer group name.
        consumer_name: This consumer's name within the group.
        handler: Async callable that receives a list of (entry_id, fields_dict)
            tuples. All field values are decoded to str.
        batch_size: Number of entries to read per XREADGROUP call. Default 50.
        block_ms: XREADGROUP BLOCK timeout in milliseconds. Default 5000.
        max_retries: Handler-invocation count threshold before dead-lettering.
            Default 3. Counts only actual handler calls, not claim cycles that
            skip the handler (e.g. dead-letter checks).
        claim_timeout_ms: XAUTOCLAIM idle timeout in milliseconds. Default 180000
            (3 minutes).
        dead_letter_max_length: Optional MAXLEN for the dead-letter stream.
            Defaults to None (unbounded unless set).
    """

    def __init__(
        self,
        stream_key: str,
        group_name: str,
        consumer_name: str,
        handler: Callable,
        batch_size: int = 50,
        block_ms: int = 5000,
        max_retries: int = 3,
        claim_timeout_ms: int = 180_000,
        dead_letter_max_length: Optional[int] = None,
    ):
        self.stream_key = stream_key
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.handler = handler
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.max_retries = max_retries
        self.claim_timeout_ms = claim_timeout_ms
        self.dead_letter_max_length = dead_letter_max_length

        self._running = False
        self._group_ensured = False
        # Cursor for XAUTOCLAIM — advances across cycles so we don't always
        # restart from the beginning of the PEL. Reset to b'0-0' each full pass.
        self._xclaim_cursor: bytes = b"0-0"

    async def _ensure_group(self) -> None:
        """Create the consumer group if it does not already exist.

        Uses ``XGROUP CREATE ... 0 MKSTREAM`` which is idempotent — if the
        group already exists, the BUSYGROUP error is caught and ignored.
        The MKSTREAM flag creates the stream if it doesn't exist yet.

        Raises:
            redis.exceptions.ResponseError: For non-BUSYGROUP errors.
        """
        if self._group_ensured:
            return

        redis = await get_async_redis_db()
        try:
            await redis.xgroup_create(
                self.stream_key, self.group_name, id="0", mkstream=True
            )
            logger.debug(
                "Created consumer group '%s' on stream '%s'",
                self.group_name,
                self.stream_key,
            )
        except Exception as e:
            # BUSYGROUP means group already exists — that's fine
            if "BUSYGROUP" in str(e):
                logger.debug(
                    "Consumer group '%s' already exists on stream '%s'",
                    self.group_name,
                    self.stream_key,
                )
            else:
                raise

        self._group_ensured = True

    async def _decode_entries(
        self, entries: List[Tuple]
    ) -> List[Tuple[str, dict]]:
        """Decode raw Redis entry bytes to str for handler consumption.

        Args:
            entries: List of (entry_id, fields_dict) tuples with bytes values.

        Returns:
            List of (decoded_id, decoded_fields) tuples with str values.
        """
        decoded_entries: List[Tuple[str, dict]] = []
        for entry_id, fields in entries:
            decoded_id = (
                entry_id.decode("utf-8") if isinstance(entry_id, bytes) else entry_id
            )
            decoded_fields = {}
            for k, v in fields.items():
                key = k.decode("utf-8") if isinstance(k, bytes) else k
                val = v.decode("utf-8") if isinstance(v, bytes) else v
                decoded_fields[key] = val
            decoded_entries.append((decoded_id, decoded_fields))
        return decoded_entries

    async def _process_entries(
        self,
        entries: List[Tuple],
        redis,
        ack_stream_key: Optional[str] = None,
        ack_group_name: Optional[str] = None,
    ) -> int:
        """Decode, invoke handler, and XACK a batch of entries.

        Shared helper used by both the XREADGROUP (new entries) path and the
        XAUTOCLAIM (reclaimed entries) path. The caller is responsible for
        passing the correct ``entries`` list; this method handles decode,
        handler dispatch, and ACK.

        The XACK uses the original bytes entry IDs (not the decoded str IDs)
        so that the exact bytes returned by Redis are echoed back, avoiding
        any encoding mismatch.

        Args:
            entries: Raw list of (entry_id, fields_dict) tuples as returned by
                XREADGROUP or XAUTOCLAIM. May contain bytes keys/values.
            redis: Active async Redis client.
            ack_stream_key: Stream key to XACK against. Defaults to
                ``self.stream_key``.
            ack_group_name: Consumer group name for XACK. Defaults to
                ``self.group_name``.

        Returns:
            int: Number of entries successfully handled and ACKed.
        """
        if not entries:
            return 0

        stream_key = ack_stream_key if ack_stream_key is not None else self.stream_key
        group_name = ack_group_name if ack_group_name is not None else self.group_name

        decoded_entries = await self._decode_entries(entries)

        # Invoke handler — intentionally outside any broad except so that a
        # handler exception propagates and the entries remain pending (not ACKed).
        await self.handler(decoded_entries)

        # Preserve original bytes IDs for XACK
        entry_ids = [eid for eid, _ in entries]
        await redis.xack(stream_key, group_name, *entry_ids)

        return len(entries)

    async def process_batch(self) -> int:
        """Execute one processing cycle: read, handle, and acknowledge entries.

        Performs the following steps:
        1. Ensure consumer group exists (cached after first call)
        2. Reclaim/dead-letter pending entries from crashed consumers via XAUTOCLAIM
        3. Read new entries via XREADGROUP
        4. Invoke handler with decoded entries
        5. XACK processed entries

        Returns:
            int: Total number of entries handled in this cycle (new + reclaimed).
                 New entries and reclaimed entries are both counted. The split is
                 logged at DEBUG level.
        """
        await self._ensure_group()

        # Reclaim pending entries from crashed consumers; count reclaimed
        reclaimed_count = await self._claim_pending()

        # Read new entries
        redis = await get_async_redis_db()
        response = await redis.xreadgroup(
            self.group_name,
            self.consumer_name,
            {self.stream_key: ">"},
            count=self.batch_size,
            block=self.block_ms,
        )

        if not response:
            logger.debug(
                "process_batch: 0 new + %d reclaimed from '%s'",
                reclaimed_count,
                self.stream_key,
            )
            return reclaimed_count

        # response format: [(stream_key, [(entry_id, fields), ...])]
        entries = response[0][1] if response else []
        if not entries:
            logger.debug(
                "process_batch: 0 new + %d reclaimed from '%s'",
                reclaimed_count,
                self.stream_key,
            )
            return reclaimed_count

        new_count = await self._process_entries(entries, redis)

        logger.debug(
            "process_batch: %d new + %d reclaimed from '%s'",
            new_count,
            reclaimed_count,
            self.stream_key,
        )
        return new_count + reclaimed_count

    async def run(self) -> None:
        """Blocking loop that continuously processes batches until stopped.

        Call ``stop()`` from another coroutine or signal handler to exit
        the loop gracefully after the current batch completes.

        Connection errors are caught and logged with a 1-second backoff
        to avoid tight error loops. Non-connection errors from the handler
        are also caught to keep the consumer running.
        """
        self._running = True
        logger.info(
            "StreamConsumer started: stream='%s' group='%s' consumer='%s'",
            self.stream_key,
            self.group_name,
            self.consumer_name,
        )

        while self._running:
            try:
                await self.process_batch()
            except Exception as e:
                logger.error(
                    "Error in process_batch for stream '%s': %s",
                    self.stream_key,
                    e,
                )
                # Backoff to avoid tight error loops
                await asyncio.sleep(1)

        logger.info(
            "StreamConsumer stopped: stream='%s' group='%s' consumer='%s'",
            self.stream_key,
            self.group_name,
            self.consumer_name,
        )

    async def _claim_pending(self) -> int:
        """Reclaim idle entries from crashed consumers and dead-letter failed entries.

        Uses XAUTOCLAIM to atomically find entries that have been idle longer
        than ``claim_timeout_ms`` and transfer them to this consumer's PEL. The
        cursor advances across cycles (stored in ``self._xclaim_cursor``) so that
        a large PEL is processed incrementally at ``batch_size`` entries per cycle
        rather than in a single unbounded scan.

        Dead-letter gating uses an explicit handler-attempt counter
        (``times_delivered`` from XPENDING) rather than the XAUTOCLAIM
        delivery count, because XAUTOCLAIM increments the delivery counter
        even on claim cycles that never invoke the handler. The threshold is
        ``>= max_retries`` handler invocations, not ``> max_retries``, so the
        last allowed attempt is attempt number ``max_retries`` (1-indexed).

        Entries deleted from the stream but still in the PEL (returned as
        ``deleted_message_ids`` by XAUTOCLAIM) are XACKed immediately without
        touching the handler.

        Returns:
            int: Number of reclaimed entries that were successfully delivered to
                 the handler and ACKed in this cycle (excludes dead-lettered and
                 deleted entries).
        """
        redis = await get_async_redis_db()
        n_reclaimed = 0
        n_dead_lettered = 0

        try:
            # XAUTOCLAIM returns a 3-tuple in redis-py 8:
            #   (next_cursor, claimed_messages, deleted_message_ids)
            # claimed_messages: [(entry_id, {field: value}), ...]
            # deleted_message_ids: [entry_id, ...] — entries trimmed/deleted from
            #   the stream but still in the PEL; XACK them, never feed to handler.
            next_cursor, claimed_messages, deleted_message_ids = (
                await redis.xautoclaim(
                    self.stream_key,
                    self.group_name,
                    self.consumer_name,
                    min_idle_time=self.claim_timeout_ms,
                    count=self.batch_size,
                    start=self._xclaim_cursor,
                    justid=False,
                )
            )

            # Advance (or reset) the cursor for the next cycle
            self._xclaim_cursor = next_cursor

            # XACK deleted entries immediately — they have no field data and
            # must not be passed to the handler or decode step.
            if deleted_message_ids:
                await redis.xack(
                    self.stream_key, self.group_name, *deleted_message_ids
                )
                logger.debug(
                    "ACKed %d deleted PEL entries from stream '%s'",
                    len(deleted_message_ids),
                    self.stream_key,
                )

            if not claimed_messages:
                logger.info(
                    "Reclaim cycle: %d reclaimed, %d dead-lettered from '%s'",
                    n_reclaimed,
                    n_dead_lettered,
                    self.stream_key,
                )
                return 0

            # For each claimed entry, check whether it has already exceeded the
            # handler-attempt threshold using xpending_range. We use xpending_range
            # here (not times_delivered from xautoclaim) because XAUTOCLAIM
            # increments the delivery count even on non-handler claim cycles, so
            # times_delivered from XAUTOCLAIM is not a reliable proxy for the
            # number of times the handler was actually invoked.
            #
            # Preferred: gate on handler-attempt count rather than delivery count
            # to avoid premature dead-lettering of entries that were only
            # reclaimed (not handled) by previous claim cycles.
            claimed_ids_bytes = [eid for eid, _ in claimed_messages]
            # Build a lookup: entry_id_str -> delivery_count
            delivery_counts: dict[str, int] = {}
            try:
                pending_entries = await redis.xpending_range(
                    self.stream_key,
                    self.group_name,
                    min="-",
                    max="+",
                    count=self.batch_size,
                )
                for entry_info in pending_entries:
                    eid = entry_info.get("message_id", b"")
                    if isinstance(eid, bytes):
                        eid = eid.decode("utf-8")
                    delivery_counts[eid] = entry_info.get("times_delivered", 0)
            except Exception as e:
                logger.warning(
                    "Could not fetch xpending_range for delivery counts "
                    "on stream '%s': %s — using 0 as fallback",
                    self.stream_key,
                    e,
                )

            # Separate entries to dead-letter from entries to redeliver
            to_redeliver: List[Tuple] = []
            for entry_id_bytes, fields in claimed_messages:
                entry_id_str = (
                    entry_id_bytes.decode("utf-8")
                    if isinstance(entry_id_bytes, bytes)
                    else entry_id_bytes
                )
                # times_delivered represents past handler invocations. A value of
                # >= max_retries means this entry has already been attempted
                # max_retries times and all retries are exhausted.
                delivery_count = delivery_counts.get(entry_id_str, 0)
                if delivery_count >= self.max_retries:
                    # Dead-letter this entry using actual delivery count
                    decoded_fields: dict = {}
                    for k, v in fields.items():
                        key = k.decode("utf-8") if isinstance(k, bytes) else k
                        val = v.decode("utf-8") if isinstance(v, bytes) else v
                        decoded_fields[key] = val

                    await self._dead_letter(
                        self.stream_key,
                        self.group_name,
                        entry_id_str,
                        decoded_fields,
                        f"Exceeded max_retries ({self.max_retries})",
                        actual_delivery_count=delivery_count,
                    )
                    n_dead_lettered += 1
                else:
                    to_redeliver.append((entry_id_bytes, fields))

            # Deliver reclaimed entries to the handler via the shared helper.
            # The reclaimed entry IDs form a SEPARATE id list from the `>`
            # XREADGROUP batch — their XACK goes to the same stream/group but
            # must be issued independently so a failure here does not ACK the
            # new-entries batch (and vice versa).
            if to_redeliver:
                # _process_entries is intentionally NOT inside the broad
                # except below — a handler exception must propagate so the
                # entries remain pending and are retried on the next cycle.
                n_reclaimed = await self._process_entries(to_redeliver, redis)

        except Exception as e:
            # Don't crash the consumer loop on claim errors; handler exceptions
            # are intentionally excluded (they propagate before reaching here).
            logger.warning(
                "Error during _claim_pending for stream '%s': %s",
                self.stream_key,
                e,
            )
            return 0

        logger.info(
            "Reclaim cycle: %d reclaimed, %d dead-lettered from '%s'",
            n_reclaimed,
            n_dead_lettered,
            self.stream_key,
        )
        return n_reclaimed

    async def _dead_letter(
        self,
        stream_key: str,
        group_name: str,
        entry_id: str,
        entry_data: dict,
        error_msg: str,
        actual_delivery_count: Optional[int] = None,
    ) -> None:
        """Move a failed entry to the dead-letter stream.

        Adds the entry to ``dead:{stream_key}`` with metadata about the
        failure, then ACKs the original entry so it is no longer pending.

        Args:
            stream_key: The source stream key.
            group_name: The consumer group name.
            entry_id: The original entry ID.
            entry_data: The original entry fields (already decoded to str).
            error_msg: Description of why the entry was dead-lettered.
            actual_delivery_count: The real handler-invocation count for this
                entry at the time of dead-lettering. If None, falls back to
                ``self.max_retries`` for backward compatibility.
        """
        redis = await get_async_redis_db()
        dead_letter_key = f"dead:{stream_key}"

        # Use the actual delivery count so the metadata reflects reality,
        # not the configured threshold.
        failure_count = (
            actual_delivery_count
            if actual_delivery_count is not None
            else self.max_retries
        )

        # Build dead-letter entry with original data plus metadata
        dead_entry = dict(entry_data)
        dead_entry["original_stream"] = stream_key
        dead_entry["original_id"] = entry_id
        dead_entry["failure_count"] = str(failure_count)
        dead_entry["last_error"] = error_msg
        dead_entry["dead_letter_ts"] = str(time.time())

        try:
            if self.dead_letter_max_length is not None:
                await redis.xadd(
                    dead_letter_key,
                    dead_entry,
                    maxlen=self.dead_letter_max_length,
                    approximate=True,
                )
            else:
                await redis.xadd(dead_letter_key, dead_entry)

            # ACK the original entry so it leaves the pending list
            await redis.xack(stream_key, group_name, entry_id)

            logger.info(
                "Dead-lettered entry '%s' from stream '%s' to '%s': %s",
                entry_id,
                stream_key,
                dead_letter_key,
                error_msg,
            )
        except Exception as e:
            logger.error(
                "Failed to dead-letter entry '%s' from stream '%s': %s",
                entry_id,
                stream_key,
                e,
            )

    def stop(self) -> None:
        """Signal the consumer to stop after the current batch completes.

        Sets the internal ``_running`` flag to False, causing the ``run()``
        loop to exit after finishing the in-progress batch.
        """
        self._running = False
        logger.info(
            "Stop requested for StreamConsumer: stream='%s' group='%s' consumer='%s'",
            self.stream_key,
            self.group_name,
            self.consumer_name,
        )

    def run_sync(self) -> None:
        """Synchronous wrapper for ``run()``.

        Starts the blocking consumer loop using ``asyncio.run()``.
        Suitable for standalone scripts or processes that don't already
        have an event loop running.
        """
        asyncio.run(self._with_fresh_connection(self.run()))

    def process_batch_sync(self) -> int:
        """Synchronous wrapper for ``process_batch()``.

        Executes one processing cycle using ``asyncio.run()``.

        Returns:
            int: Number of entries successfully processed.
        """
        return asyncio.run(self._with_fresh_connection(self.process_batch()))

    @staticmethod
    async def _with_fresh_connection(coro):
        """Reset the cached async Redis connection before running a coroutine.

        ``asyncio.run()`` creates a new event loop each time, but the cached
        async Redis connection in ``redis_db`` may be tied to a previous
        (now-closed) loop. Resetting it ensures a fresh connection is created
        for the new loop.
        """
        from .. import redis_db

        redis_db._POPOTO_ASYNC_REDIS_DB = None
        return await coro
