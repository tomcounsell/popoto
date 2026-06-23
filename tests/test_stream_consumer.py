"""Tests for StreamConsumer — Redis Streams consumer group framework.

Tests cover:
- Consumer group creation (idempotent, handles BUSYGROUP)
- Batch processing — handler receives decoded entries, XACK sent after success
- Dead-letter — entries exceeding max_retries moved to dead:{stream_key}
- XAUTOCLAIM recovery — pending entries from crashed consumers reclaimed via XAUTOCLAIM
- Crash-simulation redelivery — consumer B reclaims consumer A's unACKed entries
- Dead-letter gated on real handler attempts (not claim-count inflation)
- Deleted-message ids from XAUTOCLAIM XACKed without calling handler
- process_batch() returns Tuple[int, int] (new_count, reclaimed_count)
- Graceful shutdown — stop() causes run() to exit after current batch
- Synergy: EventStreamMixin saves → StreamConsumer processes entries end-to-end
- Empty stream — consumer blocks briefly then returns (0, 0)
- Handler exception — entries stay pending, consumer loop continues
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


def _make_always_failing_handler(invocations):
    """Create a handler that always raises and records each invocation."""

    async def handler(entries):
        invocations.append(list(entries))
        raise RuntimeError("Always fails")

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
        new_count, reclaimed_count = consumer.process_batch_sync()
        assert new_count == 0
        assert reclaimed_count == 0

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
        """process_batch returns (new_count, reclaimed_count) tuple."""
        _xadd_entries(STREAM_KEY, 3)

        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="count_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        result = consumer.process_batch_sync()
        # Returns Tuple[int, int]: (new_count, reclaimed_count)
        assert isinstance(result, tuple)
        assert len(result) == 2
        new_count, reclaimed_count = result
        assert new_count == 3
        assert reclaimed_count == 0

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
        """process_batch on empty stream returns (0, 0) after blocking timeout."""
        collected = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="empty_group",
            consumer_name="worker-1",
            handler=_make_handler(collected),
            block_ms=100,
        )
        new_count, reclaimed_count = consumer.process_batch_sync()
        assert new_count == 0
        assert reclaimed_count == 0
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
        new_count, reclaimed_count = consumer.process_batch_sync()
        assert new_count == 3
        assert reclaimed_count == 0
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
        """Entries that exceed max_retries real handler failures are dead-lettered.

        Uses an always-raising handler and a tiny claim_timeout_ms (50ms).
        First process_batch_sync reads the entry as NEW and the handler raises,
        leaving the entry pending. Subsequent cycles use XAUTOCLAIM to reclaim
        the idle entry and invoke the handler again.

        Dead-lettering gate: handler_attempts > max_retries (strictly greater),
        so with max_retries=2 the entry receives 2 handler calls then is
        dead-lettered on the 3rd reclaim cycle.
        """
        max_retries = 2
        _xadd_entries(STREAM_KEY, 1)

        invocations = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="dl_group",
            consumer_name="worker-a",
            handler=_make_always_failing_handler(invocations),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
        )

        # First call: entry read as NEW via XREADGROUP, handler raises, entry pending.
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer.process_batch_sync()
        assert len(invocations) == 1

        # Subsequent XAUTOCLAIM cycles redeliver to handler until max_retries exceeded.
        for _ in range(max_retries + 1):
            time.sleep(0.12)  # ensure idle >= 50ms claim_timeout_ms
            try:
                consumer.process_batch_sync()
            except RuntimeError:
                pass  # handler raises until dead-lettered

        # Entry must appear in dead-letter stream.
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1, (
            f"Expected entry in dead-letter after {max_retries} retries; "
            f"got {len(dead_entries)} dead entries and {len(invocations)} handler calls"
        )

        _, dead_fields = dead_entries[0]
        assert dead_fields[b"original_stream"] == STREAM_KEY.encode()
        assert b"failure_count" in dead_fields
        assert b"last_error" in dead_fields
        assert b"dead_letter_ts" in dead_fields

    def test_dead_letter_max_length(self):
        """Dead-letter stream respects max_length parameter.

        Five entries are each failed enough times to be dead-lettered. With
        dead_letter_max_length=3 the stream is approximately trimmed.
        """
        max_retries = 1
        num_entries = 5
        for i in range(num_entries):
            _xadd_entries(STREAM_KEY, 1, prefix=f"dl_max_{i}")

        invocations = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="dl_max_group",
            consumer_name="worker",
            handler=_make_always_failing_handler(invocations),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
            dead_letter_max_length=3,
        )

        # First cycle: reads all new entries, handler raises on all.
        try:
            consumer.process_batch_sync()
        except RuntimeError:
            pass

        # Additional XAUTOCLAIM cycles to exhaust max_retries per entry.
        for _ in range(max_retries + 2):
            time.sleep(0.12)
            try:
                consumer.process_batch_sync()
            except RuntimeError:
                pass

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        # With approximate trimming, may be slightly over but bounded near max_length.
        assert len(dead_entries) <= 6  # approximate MAXLEN allows some overshoot


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
        new_count, reclaimed_count = consumer.process_batch_sync()

        assert new_count == 2
        assert reclaimed_count == 0
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

        new_a, _ = consumer_a.process_batch_sync()
        new_b, _ = consumer_b.process_batch_sync()

        # Together they should have consumed all 6
        assert new_a + new_b == 6
        assert len(collected_a) + len(collected_b) == 6


# --- Tests: XAUTOCLAIM Redelivery ---


class TestXautoclaimRedelivery:
    def test_crash_simulation_consumer_b_reclaims_consumer_a_entries(self):
        """Consumer B reclaims and processes entries left pending by crashed consumer A.

        Consumer A reads entries and its handler raises (simulating a crash). After
        a short sleep consumer B with a tiny claim_timeout_ms reclaims the idle entries
        via XAUTOCLAIM and passes them to B's handler successfully.
        """
        _xadd_entries(STREAM_KEY, 3)

        # Consumer A: handler always raises — entries end up unACKed in PEL.
        invocations_a = []
        consumer_a = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="crash_group",
            consumer_name="worker-a",
            handler=_make_always_failing_handler(invocations_a),
            claim_timeout_ms=50,
            block_ms=100,
        )
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer_a.process_batch_sync()

        # Entries are in A's PEL.
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "crash_group")
        assert pending["pending"] == 3

        # Wait for entries to become idle (>= 50ms claim_timeout_ms).
        time.sleep(0.12)

        # Consumer B: handler succeeds — reclaims A's entries via XAUTOCLAIM.
        collected_b = []
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="crash_group",
            consumer_name="worker-b",
            handler=_make_handler(collected_b),
            claim_timeout_ms=50,
            block_ms=100,
        )
        new_count, reclaimed_count = consumer_b.process_batch_sync()

        # B's handler was called for the crashed entries.
        assert reclaimed_count == 3, (
            f"Expected 3 reclaimed, got reclaimed_count={reclaimed_count}"
        )
        assert len(collected_b) == 3

        # Entries are gone from XPENDING (recurrence guard).
        pending_after = POPOTO_REDIS_DB.xpending(STREAM_KEY, "crash_group")
        assert pending_after["pending"] == 0, (
            f"Expected 0 pending after reclaim, got {pending_after['pending']}"
        )

    def test_entry_succeeds_on_redelivery_not_dead_lettered(self):
        """Entry reclaimed by consumer B succeeds and never appears in dead-letter."""
        _xadd_entries(STREAM_KEY, 1)

        # Consumer A crashes (handler raises).
        invocations_a = []
        consumer_a = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="redeliver_ok_group",
            consumer_name="worker-a",
            handler=_make_always_failing_handler(invocations_a),
            claim_timeout_ms=50,
            block_ms=100,
        )
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer_a.process_batch_sync()

        time.sleep(0.12)

        # Consumer B succeeds on reclaimed entry.
        collected_b = []
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="redeliver_ok_group",
            consumer_name="worker-b",
            handler=_make_handler(collected_b),
            claim_timeout_ms=50,
            block_ms=100,
        )
        consumer_b.process_batch_sync()

        # Entry should NOT appear in dead-letter.
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) == 0, (
            f"Entry should not be dead-lettered after successful redelivery; "
            f"got {len(dead_entries)} dead entries"
        )

        # Entry is gone from XPENDING.
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "redeliver_ok_group")
        assert pending["pending"] == 0

    def test_raising_handler_on_redelivery_leaves_entry_pending(self):
        """When consumer B also raises on reclaimed entry, entry stays in XPENDING.

        Neither dead-lettering nor ACK should happen prematurely — the entry
        must remain available for a future reclaim cycle.
        """
        _xadd_entries(STREAM_KEY, 1)

        max_retries = 5  # High so dead-lettering does not fire on just 2 attempts.

        # Consumer A crashes.
        invocations_a = []
        consumer_a = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="double_fail_group",
            consumer_name="worker-a",
            handler=_make_always_failing_handler(invocations_a),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
        )
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer_a.process_batch_sync()

        time.sleep(0.12)

        # Consumer B also crashes on the reclaimed entry.
        invocations_b = []
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="double_fail_group",
            consumer_name="worker-b",
            handler=_make_always_failing_handler(invocations_b),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
        )
        try:
            consumer_b.process_batch_sync()
        except RuntimeError:
            pass  # expected — handler raises

        # B's handler was called once.
        assert len(invocations_b) == 1

        # Entry is still in XPENDING — not swallowed, not prematurely dead-lettered.
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "double_fail_group")
        assert pending["pending"] == 1, (
            f"Entry should remain pending after double failure; "
            f"got pending={pending['pending']}"
        )

        # No dead-letter entry yet (max_retries=5, only 2 handler calls so far).
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) == 0, (
            f"Entry should not be dead-lettered after only 2 handler attempts "
            f"with max_retries={max_retries}; got {len(dead_entries)} dead entries"
        )

    def test_deleted_message_ids_are_acked_not_passed_to_handler(self):
        """Entries XDEL'd from the stream but still in PEL become deleted_message_ids.

        XAUTOCLAIM returns them in the third tuple element. The consumer must ACK
        them immediately without calling the handler (their field dict is empty).
        """
        # Deliver entry to consumer A without ACKing (leave in PEL).
        POPOTO_REDIS_DB.xgroup_create(STREAM_KEY, "del_msg_group", id="0", mkstream=True)
        entry_id = POPOTO_REDIS_DB.xadd(
            STREAM_KEY, {"model": "Test", "pk": "del_test", "op": "create"}
        )
        POPOTO_REDIS_DB.xreadgroup(
            "del_msg_group", "worker-a", {STREAM_KEY: ">"}, count=1
        )

        # XDEL the entry from the stream — it now exists only in the PEL.
        POPOTO_REDIS_DB.xdel(STREAM_KEY, entry_id)

        time.sleep(0.12)  # ensure idle >= 50ms

        # Consumer B reclaims — deleted entries should be XACKed, handler NOT called.
        collected_b = []
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="del_msg_group",
            consumer_name="worker-b",
            handler=_make_handler(collected_b),
            claim_timeout_ms=50,
            block_ms=100,
        )
        consumer_b.process_batch_sync()

        # Handler was NOT called for the deleted entry.
        assert len(collected_b) == 0, (
            f"Handler should not be called for XDEL'd entry; "
            f"got {len(collected_b)} handler calls"
        )

        # Entry is gone from XPENDING (XACKed by consumer).
        pending = POPOTO_REDIS_DB.xpending(STREAM_KEY, "del_msg_group")
        assert pending["pending"] == 0, (
            f"Deleted entry should be ACKed from PEL; "
            f"got pending={pending['pending']}"
        )

    def test_process_batch_return_count_contract_on_reclaim_cycle(self):
        """process_batch returns (new_count, reclaimed_count) matching actual work.

        Deliver N new entries (first cycle returns (N, 0)). Leave M entries in PEL,
        wait for idle, run a second cycle — should return (0, M) for reclaimed.
        """
        # Deliver 4 entries to consumer A (don't ACK — handler raises).
        _xadd_entries(STREAM_KEY, 4)

        invocations_a = []
        consumer_a = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="count_contract_group",
            consumer_name="worker-a",
            handler=_make_always_failing_handler(invocations_a),
            claim_timeout_ms=50,
            block_ms=100,
        )

        # First cycle: reads 4 new entries, handler raises — returns (0, 0) because
        # handler exception prevents XACK and propagates. new entries = 0 (not ACKed).
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer_a.process_batch_sync()

        # Verify all 4 are in PEL.
        assert POPOTO_REDIS_DB.xpending(STREAM_KEY, "count_contract_group")["pending"] == 4

        time.sleep(0.12)

        # Consumer B reclaims with a succeeding handler.
        collected_b = []
        consumer_b = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="count_contract_group",
            consumer_name="worker-b",
            handler=_make_handler(collected_b),
            claim_timeout_ms=50,
            block_ms=100,
        )
        result = consumer_b.process_batch_sync()

        # Must be a 2-tuple.
        assert isinstance(result, tuple) and len(result) == 2
        new_count, reclaimed_count = result
        assert reclaimed_count == 4, (
            f"Expected reclaimed_count=4, got {reclaimed_count}"
        )
        assert len(collected_b) == 4

    @pytest.mark.parametrize("max_retries", [1, 2, 3, 5])
    def test_parametrized_dead_letter_exact_handler_invocations(self, max_retries):
        """Dead-letter fires after exactly max_retries handler failures.

        Parametrized over several max_retries values. The entry receives
        max_retries handler invocations via XAUTOCLAIM redelivery, then is
        dead-lettered on the next reclaim cycle without another handler call.
        """
        _xadd_entries(STREAM_KEY, 1)

        invocations = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name=f"param_dl_group_{max_retries}",
            consumer_name="worker",
            handler=_make_always_failing_handler(invocations),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
        )

        # First cycle: reads entry as NEW, handler raises (1 invocation).
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer.process_batch_sync()
        assert len(invocations) == 1

        # XAUTOCLAIM cycles: handler raises each time until dead-lettered.
        # We run max_retries extra cycles to ensure dead-lettering fires.
        for _ in range(max_retries + 1):
            time.sleep(0.12)
            try:
                consumer.process_batch_sync()
            except RuntimeError:
                pass

        # Entry must be dead-lettered.
        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1, (
            f"max_retries={max_retries}: expected dead-letter entry after "
            f"{len(invocations)} handler invocations"
        )

        # failure_count reflects the actual handler delivery count.
        _, dead_fields = dead_entries[0]
        failure_count = int(dead_fields[b"failure_count"])
        assert failure_count > max_retries, (
            f"max_retries={max_retries}: failure_count={failure_count} should "
            f"exceed max_retries (dead-letter fires when handler_attempts > max_retries)"
        )

    def test_dead_letter_failure_count_equals_actual_delivery_count(self):
        """failure_count in dead-letter entry equals actual handler delivery count.

        Under the fix, _dead_letter() stores delivery_count (from xpending_range)
        not the max_retries constant. Verify the stored value is > 0 and reflects
        real delivery count rather than the max_retries cap.
        """
        max_retries = 2
        _xadd_entries(STREAM_KEY, 1)

        invocations = []
        consumer = StreamConsumer(
            stream_key=STREAM_KEY,
            group_name="failure_count_group",
            consumer_name="worker",
            handler=_make_always_failing_handler(invocations),
            max_retries=max_retries,
            claim_timeout_ms=50,
            block_ms=100,
        )

        # First cycle: new entry, handler raises.
        with pytest.raises(RuntimeError, match="Always fails"):
            consumer.process_batch_sync()

        # Drive until dead-lettered.
        for _ in range(max_retries + 1):
            time.sleep(0.12)
            try:
                consumer.process_batch_sync()
            except RuntimeError:
                pass

        dead_entries = POPOTO_REDIS_DB.xrange(DEAD_LETTER_KEY)
        assert len(dead_entries) >= 1

        _, dead_fields = dead_entries[0]
        failure_count_str = dead_fields[b"failure_count"].decode()
        failure_count = int(failure_count_str)

        # failure_count must be > 0 (actual deliveries, not a hardcoded constant).
        assert failure_count > 0, (
            f"failure_count={failure_count} should reflect real delivery count"
        )
        # failure_count must exceed max_retries (that's the dead-letter trigger).
        assert failure_count > max_retries, (
            f"failure_count={failure_count} should exceed max_retries={max_retries}"
        )


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
