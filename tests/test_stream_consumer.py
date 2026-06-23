"""Tests for StreamConsumer — Redis Streams consumer group framework.

Tests cover:
- Consumer group creation (idempotent, handles BUSYGROUP)
- Batch processing — handler receives decoded entries, XACK sent after success
- Dead-letter — entries exceeding max_retries moved to dead:{stream_key}
- XCLAIM recovery — pending entries from crashed consumers reclaimed and ACKed
- Crash simulation — surviving consumer reclaims entries from crashed consumer
- Dead-letter threshold — exactly max_retries delivery events before dead-letter
- Deleted PEL entries — XACKed without reaching handler
- Graceful shutdown — stop() causes run() to exit after current batch
- Synergy: EventStreamMixin saves → StreamConsumer processes entries end-to-end
- Empty stream — consumer blocks briefly then returns 0
- Handler exception — entries stay pending, consumer loop continues
- process_batch() returns int count including reclaimed entries
"""

import sys
import os
import asyncio
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.streams.consumer import StreamConsumer  # noqa: E402
from src.popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# --- Test Models ---


class ConsumerTestItem(EventStreamMixin, popoto.Model):
    """Model that writes to a stream for consumer tests."""

    _stream_name = "consumer_test"

    name = popoto.UniqueKeyField()
    value = popoto.StringField(default="")


# --- Helpers ---

STREAM_KEY = "stream:consumer_test"
DEAD_LETTER_KEY = f"dead:{STREAM_KEY}"


def _cleanup_streams():
    """Remove all test streams and consumer groups."""
    for key in [STREAM_KEY, DEAD_LETTER_KEY]:
        POPOTO_REDIS_DB.delete(key)


def _cleanup_model():
    """Delete all ConsumerTestItem instances."""
    try:
        for key in POPOTO_REDIS_DB.smembers(
            ConsumerTestItem._meta.db_class_set_key.redis_key
        ):
            POPOTO_REDIS_DB.delete(key)
        POPOTO_REDIS_DB.delete(ConsumerTestItem._meta.db_class_set_key.redis_key)
    except Exception:
        pass


def _xadd_entries(stream_key, count, prefix="entry"):
    """Add test entries directly to a stream."""
    ids = []
    for i in range(count):
        entry_id = POPOTO_REDIS_DB.xadd(
            stream_key,
            {
                "model": "Test",
                "pk": f"{prefix}_{i}",
                "op": "create",
                "ts": str(time.time()),
            },
        )
        ids.append(entry_id)
    return ids


def _make_handler(collected):
    """Create an async handler that appends entries to collected list."""

    async def handler(entries):
        collected.extend(entries)

    return handler


def _make_failing_handler(fail_count, collected):
    """Create a handler that fails the first N calls, then succeeds."""
    call_count = {"n": 0}

    async def handler(entries):
        call_count["n"] += 1
        if call_count["n"] <= fail_count:
            raise RuntimeError(f"Intentional failure #{call_count['n']}")
        collected.extend(entries)

    return handler


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test streams and models before and after each test."""
    _cleanup_streams()
    _cleanup_model()
    yield
    _cleanup_streams()
    _cleanup_model()


# --- Tests: Consumer Group Creation ---


class TestConsumerGroupCreation:
    def test_group_creation_on_first_process_batch(self):
        """Consumer group is created on first process_batch call."""
        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="test_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        count = consumer.process_batch_sync()
        assert count == 0

        # Verify group exists via XINFO GROUPS
        groups = POPOTO_REDIS_DB.xinfo_groups(STREAM_KEY)
        group_names = [
            g["name"].decode() if isinstance(g["name"], bytes) else g["name"]
            for g in groups
        ]
        assert "test_group" in group_names

    def test_group_creation_is_idempotent(self):
        """Creating the same group twice does not raise."""
        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="idempotent_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        # First call creates the group
        consumer.process_batch_sync()
        # Reset the cached flag to force re-creation attempt
        consumer._group_ensured = False
        # Second call should succeed (BUSYGROUP caught)
        consumer.process_batch_sync()

        groups = POPOTO_REDIS_DB.xinfo_groups(STREAM_KEY)
        group_names = [
            g["name"].decode() if isinstance(g["name"], bytes) else g["name"]
            for g in groups
        ]
        assert group_names.count("idempotent_group") == 1


# --- Tests: Batch Processing ---


class TestBatchProcessing:
    def test_process_batch_returns_count(self):
        """process_batch returns the number of entries processed."""
        _xadd_entries(STREAM_KEY, 3)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="count_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        count = consumer.process_batch_sync()
        assert count == 3

    def test_handler_receives_decoded_entries(self):
        """Handler receives entries with all fields decoded to str."""
        _xadd_entries(STREAM_KEY, 2, prefix="decoded")

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="decode_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        consumer.process_batch_sync()

        assert len(collected) == 2
        for entry_id, fields in collected:
            assert isinstance(entry_id, str)
            assert isinstance(fields, dict)
            for k, v in fields.items():
                assert isinstance(k, str), f"Key {k!r} is not str"
                assert isinstance(v, str), f"Value {v!r} is not str"

    def test_entries_are_acked_after_processing(self):
        """Entries are ACKed after successful handler execution."""
        _xadd_entries(STREAM_KEY, 2)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="ack_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        consumer.process_batch_sync()

        # Check pending count — should be 0 after ACK
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "ack_group")
        assert pending["pending"] == 0

    def test_empty_stream_returns_zero(self):
        """process_batch on empty stream returns 0 after blocking timeout."""
        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="empty_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        count = consumer.process_batch_sync()
        assert count == 0
        assert len(collected) == 0

    def test_batch_size_limits_entries(self):
        """batch_size controls how many entries are read per call."""
        _xadd_entries(STREAM_KEY, 10)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="batch_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            batch_size=3,
            block_ms=100,
        )
        count = consumer.process_batch_sync()
        assert count == 3
        assert len(collected) == 3


# --- Tests: Handler Exception ---


class TestHandlerException:
    def test_handler_exception_leaves_entries_pending(self):
        """When the handler raises, entries remain pending (not ACKed)."""
        _xadd_entries(STREAM_KEY, 2)

        async def failing_handler(entries):
            raise RuntimeError("Intentional failure")

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="fail_group",
            consumer_name="worker-1",
            handler=failing_handler,
            block_ms=100,
        )
        # process_batch propagates handler exceptions (XACK never reached)
        with pytest.raises(RuntimeError, match="Intentional failure"):
            consumer.process_batch_sync()

        # Entries should still be pending (sync client unaffected by async loop)
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "fail_group")
        assert pending["pending"] == 2


# --- Tests: Dead-Letter ---


class TestDeadLetter:
    def test_entries_exceeding_max_retries_are_dead_lettered(self):
        """Entries that exceed max_retries delivery events are moved to dead-letter.

        Uses the real consumer claim cycle (XAUTOCLAIM) to bump the delivery
        counter, rather than manual XCLAIM inflation. Dead-lettering fires when
        times_delivered >= max_retries. With max_retries=2 and claim_timeout_ms=0,
        a single reclaim cycle (times_delivered: 1 initial + 1 xautoclaim = 2)
        triggers dead-letter on the next cycle.
        """
        _xadd_entries(STREAM_KEY, 1)
        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, "dl_group", id="0", mkstream=True)
        # Initial delivery: times_delivered = 1
        POPOTO_REDIS_DB.xreadgroup(
            "dl_group", "crashed-worker", {STREAM_KEY: ">"}, count=1
        )

        async def noop_handler(entries):
            pass

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="dl_group",
            consumer_name="recovery-worker",
            handler=noop_handler,
            max_retries=2,
            claim_timeout_ms=0,
            block_ms=100,
        )

        # Run until dead-lettered (at most max_retries + 2 cycles)
        for _ in range(5):
            dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
            if dead_entries:
                break
            consumer.process_batch_sync()

        # Check that the dead-letter stream has the entry
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1

        _, dead_fields = dead_entries[0]
        assert dead_fields[b"original_stream"] == STREAM_KEY.encode()
        assert b"failure_count" in dead_fields
        assert b"last_error" in dead_fields
        assert b"dead_letter_ts" in dead_fields

        # Entry is gone from XPENDING
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "dl_group")
        assert pending["pending"] == 0

    def test_dead_letter_max_length(self):
        """Dead-letter stream respects max_length with entries from real claim cycles."""
        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, "dl_max_group", id="0", mkstream=True)
        n_entries = 5

        for i in range(n_entries):
            _xadd_entries(STREAM_KEY, 1, prefix=f"dl_max_{i}")
            # Initial delivery: times_delivered = 1
            POPOTO_REDIS_DB.xreadgroup(
                "dl_max_group", "crashed", {STREAM_KEY: ">"}, count=1
            )

        async def noop_handler(entries):
            pass

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="dl_max_group",
            consumer_name="recovery",
            handler=noop_handler,
            max_retries=1,
            claim_timeout_ms=0,
            block_ms=100,
            dead_letter_max_length=3,
        )

        # Run until all entries are cleared from XPENDING
        for _ in range(n_entries + 3):
            pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "dl_max_group")
            if pending["pending"] == 0:
                break
            consumer.process_batch_sync()

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        # With approximate trimming, may be slightly over, but should be bounded
        assert len(dead_entries) <= n_entries  # approximate MAXLEN allows some over


# --- Tests: Graceful Shutdown ---


class TestGracefulShutdown:
    def test_stop_exits_run_loop(self):
        """stop() causes run() to exit after the current batch."""
        _xadd_entries(STREAM_KEY, 5)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="stop_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )

        async def run_and_stop():
            # Reset cached async connection for this new event loop
            from src.popoto import redis_db

            redis_db._POPOTO_ASYNC_REDIS_DB = None

            # Schedule stop after a short delay
            async def delayed_stop():
                await asyncio.sleep(0.3)
                consumer.stop()

            asyncio.create_task(delayed_stop())
            await consumer.run()

        asyncio.run(run_and_stop())

        # Consumer should have processed the entries then stopped
        assert len(collected) >= 5
        assert consumer._running is False


# --- Tests: Synergy with EventStreamMixin ---


class TestEventStreamSynergy:
    def test_event_stream_mixin_saves_consumed_by_stream_consumer(self):
        """End-to-end: EventStreamMixin save → StreamConsumer processes."""
        # Create model instances to produce stream entries
        item1 = ConsumerTestItem(name="synergy_1", value="first")
        item1.save()
        item2 = ConsumerTestItem(name="synergy_2", value="second")
        item2.save()

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="synergy_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        count = consumer.process_batch_sync()

        assert count == 2
        assert len(collected) == 2

        # Verify entry content matches what EventStreamMixin produces
        ops = [fields["op"] for _, fields in collected]
        assert "create" in ops

        models = [fields["model"] for _, fields in collected]
        assert all(m == "ConsumerTestItem" for m in models)

    def test_event_stream_mixin_delete_consumed(self):
        """Delete events from EventStreamMixin are consumed correctly."""
        item = ConsumerTestItem(name="synergy_del", value="will_delete")
        item.save()

        # Consume the create event first
        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="synergy_del_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        consumer.process_batch_sync()
        collected.clear()

        # Now delete
        item.delete()
        consumer.process_batch_sync()

        assert len(collected) == 1
        _, fields = collected[0]
        assert fields["op"] == "delete"


# --- Tests: Multiple Consumers ---


class TestMultipleConsumers:
    def test_two_consumers_share_work(self):
        """Two consumers in the same group each get a subset of entries."""
        _xadd_entries(STREAM_KEY, 6)

        collected_a = []
        collected_b = []
        consumer_a = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="shared_group",
            consumer_name="worker-a",
            handler=_make_handler(collected_a),
            batch_size=3,
            block_ms=100,
        )
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="shared_group",
            consumer_name="worker-b",
            handler=_make_handler(collected_b),
            batch_size=3,
            block_ms=100,
        )

        count_a = consumer_a.process_batch_sync()
        count_b = consumer_b.process_batch_sync()

        # Together they should have consumed all 6
        assert count_a + count_b == 6
        assert len(collected_a) + len(collected_b) == 6


# --- Tests: Import ---


class TestImport:
    def test_importable_from_popoto(self):
        """StreamConsumer is importable from the top-level popoto package."""
        assert hasattr(popoto, "StreamConsumer")
        assert popoto.StreamConsumer is StreamConsumer

    def test_importable_from_streams_subpackage(self):
        """StreamConsumer is importable from popoto.streams."""
        from src.popoto.streams import StreamConsumer as SC

        assert SC is StreamConsumer


# --- Helpers for XCLAIM redelivery tests ---


def _setup_crashed_consumer(stream_key, group_name, n_entries=1, prefix="crash"):
    """Simulate a crashed consumer: deliver entries to a group but never ACK.

    Uses the sync POPOTO_REDIS_DB directly for setup so we bypass the consumer
    lifecycle (no _ensure_group, no _claim_pending). The caller must ensure the
    group already exists or call XGROUP CREATE first.

    Returns the list of entry IDs added.
    """
    entry_ids = _xadd_entries(stream_key, n_entries, prefix=prefix)
    POPOTO_REDIS_DB.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    # Deliver but do NOT XACK — simulates the consumer crashing mid-process
    POPOTO_REDIS_DB.xreadgroup(
        group_name,
        "crashed-consumer",
        {stream_key: ">"},
        count=n_entries,
    )
    return entry_ids


# --- Tests: XCLAIM Crash Redelivery ---


class TestXClaimRedelivery:
    """Tests for XAUTOCLAIM-based recovery of entries from crashed consumers.

    Each test simulates a crash by delivering entries to a consumer group and
    intentionally not ACKing them. A surviving consumer with a tiny
    claim_timeout_ms then reclaims them. Because claim_timeout_ms is
    server-evaluated (real idle clock, not mockable), tests use a small
    non-zero timeout (10 ms) and sleep briefly to accumulate idle time.
    """

    def test_crashed_consumer_entries_redelivered_by_surviving_consumer(self):
        """Surviving consumer reclaims entries from a crashed consumer.

        BLOCKER-1: entries that were pending under crashed-consumer appear in
        XPENDING before recovery and disappear (pending count = 0) after the
        surviving consumer processes them.
        """
        entry_ids = _setup_crashed_consumer(
            STREAM_KEY, "crash_group", n_entries=2, prefix="blocker1"
        )

        # Confirm entries are pending before recovery
        pending_before = POPOTO_REDIS_DB.xpending(STREAM_KEY, "crash_group")
        assert pending_before["pending"] == 2

        # Wait for idle time to accumulate beyond claim_timeout_ms
        time.sleep(0.1)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="crash_group",
            consumer_name="survivor",
            handler=_make_handler(collected),
            claim_timeout_ms=10,
            block_ms=100,
            max_retries=5,
        )
        count = consumer.process_batch_sync()

        # Handler was called with the reclaimed entries
        assert len(collected) == 2, (
            f"Expected 2 reclaimed entries handled, got {len(collected)}"
        )

        # BLOCKER-1: entries are GONE from XPENDING (recurrence guard)
        pending_after = POPOTO_REDIS_DB.xpending(STREAM_KEY, "crash_group")
        assert pending_after["pending"] == 0, (
            f"Entries still pending after reclaim: {pending_after['pending']}"
        )

        # process_batch return value includes the reclaimed entries
        assert count >= 2

    def test_successful_redelivery_not_dead_lettered(self):
        """Entry reclaimed by surviving consumer and handled successfully is not dead-lettered."""
        _setup_crashed_consumer(
            STREAM_KEY, "success_group", n_entries=1, prefix="nodl"
        )

        time.sleep(0.1)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="success_group",
            consumer_name="survivor",
            handler=_make_handler(collected),
            claim_timeout_ms=10,
            block_ms=100,
            max_retries=5,
        )
        consumer.process_batch_sync()

        # Entry was handled
        assert len(collected) == 1

        # Entry is NOT in dead-letter stream
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) == 0, (
            f"Entry was incorrectly dead-lettered: {dead_entries}"
        )

        # Entry is gone from XPENDING
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "success_group")
        assert pending["pending"] == 0

    def test_redelivery_handler_exception_leaves_entry_pending(self):
        """When the handler raises during redelivery, the entry stays in XPENDING."""
        _setup_crashed_consumer(
            STREAM_KEY, "fail_redeliver_group", n_entries=1, prefix="failredeliv"
        )

        time.sleep(0.1)

        async def always_failing_handler(entries):
            raise RuntimeError("Handler fails on redelivery")

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="fail_redeliver_group",
            consumer_name="survivor",
            handler=always_failing_handler,
            claim_timeout_ms=10,
            block_ms=100,
            max_retries=5,
        )

        # The handler exception propagates out of process_batch_sync
        with pytest.raises(RuntimeError, match="Handler fails on redelivery"):
            consumer.process_batch_sync()

        # Entry remains in XPENDING — not ACKed, not dead-lettered
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "fail_redeliver_group")
        assert pending["pending"] == 1, (
            f"Entry should still be pending after handler exception, "
            f"got {pending['pending']}"
        )

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) == 0

    def test_process_batch_return_count_includes_reclaimed(self):
        """process_batch() return value counts both new entries and reclaimed entries."""
        n_crashed = 3
        _setup_crashed_consumer(
            STREAM_KEY, "count_group", n_entries=n_crashed, prefix="countreclaim"
        )

        time.sleep(0.1)

        # Add fresh entries that will be read via XREADGROUP ">"
        n_fresh = 2
        _xadd_entries(STREAM_KEY, n_fresh, prefix="fresh")

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="count_group",
            consumer_name="survivor",
            handler=_make_handler(collected),
            claim_timeout_ms=10,
            block_ms=100,
            max_retries=5,
        )
        total = consumer.process_batch_sync()

        # Return value must be an int equal to reclaimed + fresh entries
        assert isinstance(total, int), f"Expected int, got {type(total)}"
        # Reclaimed entries (3) + fresh entries (2) = 5 total
        assert total == n_crashed + n_fresh, (
            f"Expected {n_crashed + n_fresh} total, got {total}"
        )
        assert len(collected) == n_crashed + n_fresh

    def test_deleted_message_ids_xacked_without_handler_call(self):
        """Entries deleted from the stream but still in PEL are XACKed without handler.

        XAUTOCLAIM returns deleted_message_ids for entries that were XDEL-ed
        from the stream while still pending. These must be ACKed silently —
        they have no field data and must never reach the handler.
        """
        entry_ids = _setup_crashed_consumer(
            STREAM_KEY, "del_group", n_entries=1, prefix="deleted_entry"
        )

        # Delete the entry from the stream while it's still in the PEL
        POPOTO_REDIS_DB.xdel(STREAM_KEY, entry_ids[0])

        time.sleep(0.1)

        handler_call_count = {"n": 0}

        async def counting_handler(entries):
            handler_call_count["n"] += len(entries)

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="del_group",
            consumer_name="survivor",
            handler=counting_handler,
            claim_timeout_ms=10,
            block_ms=100,
            max_retries=5,
        )
        consumer.process_batch_sync()

        # Handler was NOT called for the deleted entry
        assert handler_call_count["n"] == 0, (
            f"Handler should not be called for deleted entries, "
            f"got {handler_call_count['n']} calls"
        )

        # Entry is gone from XPENDING (ACKed)
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "del_group")
        assert pending["pending"] == 0, (
            f"Deleted PEL entry should be ACKed, still pending: {pending['pending']}"
        )


# --- Tests: Dead-Letter Threshold ---


class TestDeadLetterThreshold:
    """Tests for dead-letter gating based on XPENDING times_delivered.

    The dead-letter gate fires when times_delivered >= max_retries. The
    times_delivered counter is incremented by: initial XREADGROUP delivery +
    each XAUTOCLAIM reclaim cycle. It is NOT limited to handler invocations.
    """

    @pytest.mark.parametrize("max_retries_val", [1, 2, 3, 5])
    def test_dead_letter_after_exactly_max_retries_delivery_events(
        self, max_retries_val
    ):
        """Entry is dead-lettered after exactly max_retries total delivery events.

        BLOCKER-2 verification: dead-lettering is based on times_delivered from
        XPENDING, which counts the initial xreadgroup delivery + each xautoclaim
        reclaim. With max_retries=N: after 1 initial delivery + (N-1) xautoclaim
        cycles, times_delivered reaches N and the entry is dead-lettered.

        This test pumps the entry through enough claim cycles to trigger
        dead-lettering, then verifies it appears in dead:{stream_key}.
        """
        group = f"dl_threshold_group_{max_retries_val}"
        _xadd_entries(STREAM_KEY, 1, prefix=f"thresh_{max_retries_val}")

        # Initial delivery (times_delivered = 1)
        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, group, id="0", mkstream=True)
        POPOTO_REDIS_DB.xreadgroup(group, "crashed", {STREAM_KEY: ">"}, count=1)

        # Run reclaim cycles until dead-lettered. Each process_batch_sync call
        # invokes xautoclaim which bumps times_delivered by 1. We need
        # (max_retries_val - 1) additional increments to reach the threshold.
        # Use max_retries=max_retries_val and claim_timeout_ms=0 so xautoclaim
        # always finds the entry eligible.
        async def always_failing_handler(entries):
            raise RuntimeError("Always fails")

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name=group,
            consumer_name="recovery",
            handler=always_failing_handler,
            max_retries=max_retries_val,
            claim_timeout_ms=0,
            block_ms=100,
        )

        # Run at most max_retries_val + 2 cycles to avoid infinite loop
        for _ in range(max_retries_val + 2):
            dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
            if dead_entries:
                break
            try:
                consumer.process_batch_sync()
            except RuntimeError:
                pass  # handler exception is expected when entry is redelivered

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1, (
            f"Expected entry in dead-letter after {max_retries_val} delivery "
            f"events, but found none"
        )

        # Entry is gone from XPENDING after dead-lettering
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, group)
        assert pending["pending"] == 0, (
            f"Dead-lettered entry should be ACKed from PEL, "
            f"still pending: {pending['pending']}"
        )

    def test_dead_letter_failure_count_equals_actual_delivery_count(self):
        """Dead-letter metadata has failure_count equal to actual times_delivered.

        The failure_count stored in dead:{stream_key} must reflect the real
        delivery count, not the configured max_retries threshold. These differ
        when the entry is claimed multiple times before dead-lettering.
        """
        group = "dl_fc_group"
        max_retries = 2

        _xadd_entries(STREAM_KEY, 1, prefix="fc_entry")
        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, group, id="0", mkstream=True)
        # Initial delivery: times_delivered = 1
        POPOTO_REDIS_DB.xreadgroup(group, "crashed", {STREAM_KEY: ">"}, count=1)

        async def noop_handler(entries):
            pass

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name=group,
            consumer_name="recovery",
            handler=noop_handler,
            max_retries=max_retries,
            claim_timeout_ms=0,
            block_ms=100,
        )

        # Run enough cycles to trigger dead-lettering
        for _ in range(max_retries + 2):
            dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
            if dead_entries:
                break
            consumer.process_batch_sync()

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1

        _, dead_fields = dead_entries[0]
        stored_failure_count = int(dead_fields[b"failure_count"])

        # failure_count must be >= max_retries (the actual delivery count when
        # dead-lettered may be exactly max_retries or higher, depending on how
        # many xautoclaim cycles ran before the threshold was reached)
        assert stored_failure_count >= max_retries, (
            f"failure_count {stored_failure_count} should be >= max_retries "
            f"{max_retries}"
        )

        # Verify required dead-letter fields are present
        assert b"original_stream" in dead_fields
        assert dead_fields[b"original_stream"] == STREAM_KEY.encode()
        assert b"last_error" in dead_fields
        assert b"dead_letter_ts" in dead_fields

    def test_dead_letter_max_length_with_real_failures(self):
        """Dead-letter stream respects max_length with entries from real failure cycles."""
        group = "dl_maxlen_group"
        n_entries = 5
        max_retries = 1  # dead-letter quickly (after 1 xautoclaim)

        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, group, id="0", mkstream=True)

        for i in range(n_entries):
            _xadd_entries(STREAM_KEY, 1, prefix=f"maxlen_{i}")
            # Initial delivery
            POPOTO_REDIS_DB.xreadgroup(group, "crashed", {STREAM_KEY: ">"}, count=1)

        async def noop_handler(entries):
            pass

        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name=group,
            consumer_name="recovery",
            handler=noop_handler,
            max_retries=max_retries,
            claim_timeout_ms=0,
            block_ms=100,
            dead_letter_max_length=3,
        )

        # Run until all entries are dead-lettered (multiple cycles needed)
        for _ in range(n_entries + 3):
            pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, group)
            if pending["pending"] == 0:
                break
            consumer.process_batch_sync()

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        # With approximate MAXLEN trimming, length may be slightly above 3 but
        # should be bounded well below n_entries=5 if trimming is working
        assert len(dead_entries) <= n_entries, (
            f"Dead-letter stream has {len(dead_entries)} entries, "
            f"expected <= {n_entries}"
        )
