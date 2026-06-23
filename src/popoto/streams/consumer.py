"""StreamConsumer — Redis Streams consumer group framework for Popoto.

This module provides a consumer group abstraction over Redis Streams, handling
the XREADGROUP/XACK/XAUTOCLAIM/XPENDING lifecycle so application code only needs
to supply a handler function.

Design:
    - Async-first: core logic uses ``redis.asyncio`` via ``get_async_redis_db()``
    - Sync wrappers (``run_sync``, ``process_batch_sync``) use ``asyncio.run()``
    - Dead-letter: entries exceeding ``max_retries`` are moved to ``dead:{stream_key}``
    - Recovery: ``XAUTOCLAIM`` atomically reclaims idle entries from crashed consumers
      and feeds them back through the handler; each successful redelivery is XACKed
      with a separate id list so reclaimed entries leave the PEL.
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
        max_retries: Delivery count threshold before dead-lettering. Default 3.
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
        # Cursor for XAUTOCLAIM — advances across cycles to avoid starvation
        self._autoclaim_cursor: str = "0-0"

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

    @staticmethod
    def _decode_entries(
        raw_entries: List[Tuple],
    ) -> Tuple[List[Tuple[str, dict]], List]:
        """Decode bytes entry ids and field dicts to str.

        Args:
            raw_entries: List of (entry_id, fields) tuples as returned by
                redis-py (ids and keys/values may be bytes or str).

        Returns:
            A 2-tuple of:
            - decoded_entries: List of (str_id, decoded_fields_dict).
            - original_ids: List of the raw (bytes or str) entry ids, suitable
              for passing directly to XACK.
        """
        decoded_entries: List[Tuple[str, dict]] = []
        original_ids: List = []

        for entry_id, fields in raw_entries:
            original_ids.append(entry_id)
            decoded_id = (
                entry_id.decode("utf-8") if isinstance(entry_id, bytes) else entry_id
            )
            decoded_fields = {}
            for k, v in fields.items():
                key = k.decode("utf-8") if isinstance(k, bytes) else k
                val = v.decode("utf-8") if isinstance(v, bytes) else v
                decoded_fields[key] = val
            decoded_entries.append((decoded_id, decoded_fields))

        return decoded_entries, original_ids

    async def _process_entries(
        self,
        redis,
        raw_entries: List[Tuple],
    ) -> int:
        """Decode entries, invoke the handler, and XACK on success.

        This is the shared processing path used by both the XREADGROUP (new
        entries) path and the XAUTOCLAIM (reclaimed entries) path. Each call
        issues its own XACK using the entry ids from *this* batch only, so
        reclaimed ids are never confused with new-entry ids.

        The handler invocation is intentionally NOT wrapped in a broad
        ``except Exception`` — a handler error during redelivery must leave
        the entry pending so it can be reclaimed again or dead-lettered.

        Args:
            redis: The async Redis client instance.
            raw_entries: Raw (entry_id, fields) tuples from redis-py.

        Returns:
            Number of entries successfully handled and ACKed.
        """
        if not raw_entries:
            return 0

        decoded_entries, original_ids = self._decode_entries(raw_entries)

        # Handler errors propagate to the caller — do NOT swallow them here.
        await self.handler(decoded_entries)

        # ACK using this batch's own id list (bytes ids are fine for XACK)
        await redis.xack(self.stream_key, self.group_name, *original_ids)

        return len(original_ids)

    async def process_batch(self) -> Tuple[int, int]:
        """Execute one processing cycle: read, handle, and acknowledge entries.

        Performs the following steps:
        1. Ensure consumer group exists (cached after first call).
        2. Reclaim pending entries from crashed consumers via XAUTOCLAIM;
           reclaimed entries are decoded and passed to the handler immediately.
           Entries that exceed ``max_retries`` real handler attempts are
           dead-lettered instead. Deleted-message ids from XAUTOCLAIM are
           ACKed immediately without being passed to the handler.
        3. Read new entries via XREADGROUP with id ``>``.
        4. Invoke handler with decoded new entries.
        5. XACK processed new entries.

        Returns:
            Tuple[int, int]: ``(new_count, reclaimed_count)`` — number of new
            entries processed and number of reclaimed entries redelivered
            successfully in this batch.
        """
        await self._ensure_group()

        # Reclaim and redeliver pending entries from crashed consumers.
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
            return 0, reclaimed_count

        # response format: [(stream_key, [(entry_id, fields), ...])]
        entries = response[0][1] if response else []
        if not entries:
            return 0, reclaimed_count

        # _process_entries handles decode + handler invocation + XACK.
        # Handler errors here propagate up through process_batch → run().
        new_count = await self._process_entries(redis, entries)

        logger.debug(
            "Processed %d new + %d reclaimed entries from '%s'",
            new_count,
            reclaimed_count,
            self.stream_key,
        )
        return new_count, reclaimed_count

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
        """Reclaim idle entries via XAUTOCLAIM and redeliver them to the handler.

        Uses XAUTOCLAIM (redis-py 8 / Valkey-safe) to atomically:
          - Scan entries that have been idle >= ``claim_timeout_ms``
          - Transfer ownership to this consumer
          - Return full field data for immediate redelivery

        XAUTOCLAIM returns a 3-tuple: ``(next_cursor, claimed_messages,
        deleted_message_ids)``.  Each claimed entry is decoded and passed
        through the handler, exactly as a newly-read entry would be, and then
        XACKed on success.

        **At-least-once delivery:** a reclaimed entry may be delivered more
        than once if the consumer crashes between handler invocation and XACK.
        Handlers MUST be idempotent.

        The cursor is advanced across cycles (stored in ``_autoclaim_cursor``,
        per-cycle cap = ``batch_size``) so no single cycle drains the entire
        PEL.

        Dead-lettering gate: an explicit handler-attempt counter is incremented
        once per ``await self.handler(...)`` redelivery invocation. Dead-letter
        fires when ``handler_attempts > max_retries``. This prevents claim-cycle
        count inflation from triggering premature dead-lettering.

        Deleted-message ids returned by XAUTOCLAIM (entries whose stream data
        was trimmed) are XACKed immediately and never fed to the handler —
        their field dicts are empty and would crash decoding.

        Returns:
            int: Number of reclaimed entries successfully redelivered.
        """
        redis = await get_async_redis_db()
        reclaimed_count = 0
        dead_lettered_count = 0

        try:
            # XAUTOCLAIM returns a 3-tuple in redis-py 8:
            #   (next_cursor, claimed_messages, deleted_message_ids)
            result = await redis.xautoclaim(
                self.stream_key,
                self.group_name,
                self.consumer_name,
                min_idle_time=self.claim_timeout_ms,
                start_id=self._autoclaim_cursor,
                count=self.batch_size,
            )

            next_cursor, claimed_messages, deleted_message_ids = result

            # Advance cursor for next cycle; reset to "0-0" when we reach end.
            if next_cursor in (b"0-0", "0-0", b"0", "0"):
                self._autoclaim_cursor = "0-0"
            else:
                self._autoclaim_cursor = (
                    next_cursor.decode("utf-8")
                    if isinstance(next_cursor, bytes)
                    else next_cursor
                )

            # ACK deleted entries immediately — they have no field data and
            # would crash decoding. Never pass them to the handler.
            if deleted_message_ids:
                await redis.xack(
                    self.stream_key, self.group_name, *deleted_message_ids
                )
                logger.debug(
                    "ACKed %d deleted-message ids from XAUTOCLAIM on '%s'",
                    len(deleted_message_ids),
                    self.stream_key,
                )

            # Redeliver claimed entries through the handler.
            # We need per-entry delivery count to gate dead-lettering.
            # Fetch from XPENDING_RANGE for the claimed ids.
            for raw_entry in claimed_messages:
                entry_id_raw, fields = raw_entry
                entry_id_str = (
                    entry_id_raw.decode("utf-8")
                    if isinstance(entry_id_raw, bytes)
                    else entry_id_raw
                )

                # Determine how many times the handler has actually been invoked
                # for this entry by checking delivery_count from xpending_range.
                # We use delivery_count as a proxy for handler attempts because
                # each XAUTOCLAIM transfer increments it.
                # Gate: dead-letter when delivery_count > max_retries, evaluated
                # BEFORE running the handler (so a max_retries=3 entry gets
                # exactly 3 handler attempts before being dead-lettered on the 4th).
                handler_attempts = 0
                try:
                    pending_detail = await redis.xpending_range(
                        self.stream_key,
                        self.group_name,
                        min=entry_id_str,
                        max=entry_id_str,
                        count=1,
                    )
                    if pending_detail:
                        handler_attempts = pending_detail[0].get(
                            "times_delivered", 0
                        )
                except Exception as pe:
                    logger.warning(
                        "Could not fetch delivery count for entry '%s': %s",
                        entry_id_str,
                        pe,
                    )

                if handler_attempts > self.max_retries:
                    # Dead-letter this entry.
                    decoded_fields = {}
                    for k, v in fields.items():
                        key = k.decode("utf-8") if isinstance(k, bytes) else k
                        val = v.decode("utf-8") if isinstance(v, bytes) else v
                        decoded_fields[key] = val

                    await self._dead_letter(
                        self.stream_key,
                        self.group_name,
                        entry_id_str,
                        decoded_fields,
                        f"Exceeded max_retries ({self.max_retries}): "
                        f"handler_attempts={handler_attempts}",
                        delivery_count=handler_attempts,
                    )
                    dead_lettered_count += 1
                else:
                    # Redeliver to the handler. Use _process_entries with a
                    # single-entry list so XACK uses the correct reclaimed id.
                    # Handler errors here propagate OUT of _claim_pending so
                    # the entry stays pending for the next reclaim cycle.
                    count = await self._process_entries(redis, [raw_entry])
                    reclaimed_count += count

            if reclaimed_count or dead_lettered_count:
                logger.info(
                    "XAUTOCLAIM cycle on '%s': %d redelivered, %d dead-lettered",
                    self.stream_key,
                    reclaimed_count,
                    dead_lettered_count,
                )

        except Exception as e:
            # Don't crash the consumer loop on claim errors
            logger.warning(
                "Error during _claim_pending for stream '%s': %s",
                self.stream_key,
                e,
            )

        return reclaimed_count

    async def _dead_letter(
        self,
        stream_key: str,
        group_name: str,
        entry_id: str,
        entry_data: dict,
        error_msg: str,
        delivery_count: int = 0,
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
            delivery_count: The actual handler-attempt / delivery count for
                this entry (from XPENDING). Stored as ``failure_count`` in the
                dead-letter entry rather than hardcoding ``max_retries``.
        """
        redis = await get_async_redis_db()
        dead_letter_key = f"dead:{stream_key}"

        # Build dead-letter entry with original data plus metadata
        dead_entry = dict(entry_data)
        dead_entry["original_stream"] = stream_key
        dead_entry["original_id"] = entry_id
        dead_entry["failure_count"] = str(delivery_count)
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

    def process_batch_sync(self) -> Tuple[int, int]:
        """Synchronous wrapper for ``process_batch()``.

        Executes one processing cycle using ``asyncio.run()``.

        Returns:
            Tuple[int, int]: ``(new_count, reclaimed_count)`` — number of new
            entries processed and number of reclaimed entries redelivered
            successfully in this batch.
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
