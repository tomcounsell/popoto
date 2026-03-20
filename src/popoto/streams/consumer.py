"""StreamConsumer — Redis Streams consumer group framework for Popoto.

This module provides a consumer group abstraction over Redis Streams, handling
the XREADGROUP/XACK/XCLAIM/XPENDING lifecycle so application code only needs
to supply a handler function.

Design:
    - Async-first: core logic uses ``redis.asyncio`` via ``get_async_redis_db()``
    - Sync wrappers (``run_sync``, ``process_batch_sync``) use ``asyncio.run()``
    - Dead-letter: entries exceeding ``max_retries`` are moved to ``dead:{stream_key}``
    - Recovery: ``XCLAIM`` reclaims entries from crashed consumers after idle timeout
    - No Redis modules — only core Streams commands (Valkey compatible)

Redis Commands Used:
    - XGROUP CREATE — create consumer group (idempotent with BUSYGROUP handling)
    - XREADGROUP — read new entries for this consumer
    - XACK — acknowledge processed entries
    - XPENDING — inspect pending entries for recovery/dead-letter decisions
    - XCLAIM — reclaim entries from idle consumers
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
        claim_timeout_ms: XCLAIM idle timeout in milliseconds. Default 180000
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

    async def process_batch(self) -> int:
        """Execute one processing cycle: read, handle, and acknowledge entries.

        Performs the following steps:
        1. Ensure consumer group exists (cached after first call)
        2. Reclaim/dead-letter pending entries from crashed consumers
        3. Read new entries via XREADGROUP
        4. Invoke handler with decoded entries
        5. XACK processed entries

        Returns:
            int: Number of entries successfully processed in this batch.
        """
        await self._ensure_group()

        # Reclaim pending entries from crashed consumers
        await self._claim_pending()

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
            return 0

        # response format: [(stream_key, [(entry_id, fields), ...])]
        entries = response[0][1] if response else []
        if not entries:
            return 0

        # Decode bytes to str for all entry fields
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

        # Invoke handler
        await self.handler(decoded_entries)

        # ACK all entries
        entry_ids = [eid for eid, _ in entries]
        await redis.xack(self.stream_key, self.group_name, *entry_ids)

        logger.debug(
            "Processed and ACKed %d entries from '%s'",
            len(entries),
            self.stream_key,
        )
        return len(entries)

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

    async def _claim_pending(self) -> None:
        """Reclaim entries from crashed consumers and dead-letter failed entries.

        Uses XPENDING to find entries that have been idle longer than
        ``claim_timeout_ms``. Entries that have exceeded ``max_retries``
        delivery attempts are moved to the dead-letter stream. Others
        are reclaimed via XCLAIM for reprocessing by this consumer.
        """
        redis = await get_async_redis_db()

        try:
            # Get summary of pending entries for this group
            pending_summary = await redis.xpending(self.stream_key, self.group_name)

            # pending_summary: {
            #   'pending': count, 'min': id, 'max': id,
            #   'consumers': [{'name': ..., 'pending': ...}, ...]
            # }
            pending_count = pending_summary.get("pending", 0)
            if not pending_count:
                return

            # Get detailed info on pending entries
            pending_entries = await redis.xpending_range(
                self.stream_key,
                self.group_name,
                min="-",
                max="+",
                count=self.batch_size,
            )

            for entry_info in pending_entries:
                entry_id = entry_info.get("message_id", b"")
                if isinstance(entry_id, bytes):
                    entry_id = entry_id.decode("utf-8")

                idle_time = entry_info.get("time_since_delivered", 0)
                delivery_count = entry_info.get("times_delivered", 0)

                # Skip entries that haven't been idle long enough
                if idle_time < self.claim_timeout_ms:
                    continue

                if delivery_count > self.max_retries:
                    # Dead-letter this entry
                    # Read the entry data first via XCLAIM so we have the fields
                    claimed = await redis.xclaim(
                        self.stream_key,
                        self.group_name,
                        self.consumer_name,
                        min_idle_time=0,
                        message_ids=[entry_id],
                    )
                    if claimed:
                        claimed_id, claimed_fields = claimed[0]
                        if isinstance(claimed_id, bytes):
                            claimed_id = claimed_id.decode("utf-8")
                        # Decode fields for dead-letter metadata
                        decoded_fields = {}
                        for k, v in claimed_fields.items():
                            key = k.decode("utf-8") if isinstance(k, bytes) else k
                            val = v.decode("utf-8") if isinstance(v, bytes) else v
                            decoded_fields[key] = val

                        await self._dead_letter(
                            self.stream_key,
                            self.group_name,
                            claimed_id,
                            decoded_fields,
                            f"Exceeded max_retries ({self.max_retries})",
                        )
                else:
                    # Reclaim for reprocessing
                    await redis.xclaim(
                        self.stream_key,
                        self.group_name,
                        self.consumer_name,
                        min_idle_time=self.claim_timeout_ms,
                        message_ids=[entry_id],
                    )
                    logger.debug(
                        "Claimed pending entry '%s' from stream '%s'",
                        entry_id,
                        self.stream_key,
                    )

        except Exception as e:
            # Don't crash the consumer loop on claim errors
            logger.warning(
                "Error during _claim_pending for stream '%s': %s",
                self.stream_key,
                e,
            )

    async def _dead_letter(
        self,
        stream_key: str,
        group_name: str,
        entry_id: str,
        entry_data: dict,
        error_msg: str,
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
        """
        redis = await get_async_redis_db()
        dead_letter_key = f"dead:{stream_key}"

        # Build dead-letter entry with original data plus metadata
        dead_entry = dict(entry_data)
        dead_entry["original_stream"] = stream_key
        dead_entry["original_id"] = entry_id
        dead_entry["failure_count"] = str(self.max_retries)
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
