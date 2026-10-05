"""``[PG-only]`` behaviour of event streams, consumer groups and pub/sub on
Postgres (#759 M5; ``backends/postgres/events.py``, ``pubsub.py``).

The parity -- the same assertions on both legs -- is
``tests/test_event_stream_mixin.py``, ``tests/test_stream_consumer.py`` and
``tests/test_pubsub.py``. What lives here has no Redis twin: the entry
committing (or rolling back) with its record, ids that stay ordered across
transactions, exact ``MAXLEN`` trimming, ``SKIP LOCKED`` claims, the
``LISTEN`` session (its notification, its fallback poll and its
reconnection), the ``NOTIFY`` payload limit, and the proof that a
Postgres-bound model with these mixins sends Redis nothing at all.
"""

import asyncio
import importlib.util
import threading
import time
from pathlib import Path

import pytest

import popoto
from popoto.backends import get_backend
from popoto.backends.postgres import events as events_module
from popoto.backends.postgres.events import (
    StreamCommandError,
    StreamDataError,
    StreamStore,
)
from popoto.backends.postgres.pubsub import PubSubPayloadTooLarge, glob_to_regex
from popoto.fields.event_stream import EventStreamMixin
from popoto.streams import StreamConsumer, stream_client


class EvItem(EventStreamMixin, popoto.Model):
    _stream_name = "pg_events"
    _stream_metadata_fields = ("tag",)

    name = popoto.UniqueKeyField()
    tag = popoto.StringField(default="")
    trust = popoto.ConfidenceField()
    links = popoto.CoOccurrenceField()


class EvSmall(EventStreamMixin, popoto.Model):
    _stream_name = "pg_events_small"
    _stream_max_length = 5

    name = popoto.UniqueKeyField()


class EvBroken(EventStreamMixin, popoto.Model):
    _stream_name = ""

    name = popoto.UniqueKeyField()


KEY = "stream:pg_events"


def _store(pg):
    return pg.streams()


def _admin_count(admin, pg, table):
    return admin.execute(f'SELECT count(*) FROM "{pg.schema}"."{table}"').fetchone()[0]


# -- the entry is part of the write ---------------------------------------------


def test_the_entry_commits_and_rolls_back_with_its_record(pg, admin):
    EvItem(name="a").save()
    assert EvItem.stream_len() == 1
    with pytest.raises(RuntimeError, match="roll back"):
        with pg.transaction() as uow:
            EvItem(name="b").save(pipeline=uow)
            EvItem(name="a").delete(pipeline=uow)
            raise RuntimeError("roll back")
    assert EvItem.stream_len() == 1
    assert EvItem.query.get(name="b") is None
    with pg.transaction() as uow:
        EvItem(name="c").save(pipeline=uow)
        # Appended just before COMMIT: nothing yet, from outside or inside.
        assert _admin_count(admin, pg, "popoto_stream_entry") == 1
    ops = [f[b"op"] for _, f in EvItem.stream_range()]
    assert ops == [b"create", b"create"]


def test_a_failing_append_fails_the_save_and_keeps_no_record(pg, monkeypatch):
    """A documented divergence: on Redis a failed ``XADD`` after an
    immediate save is logged and the record stays; on Postgres the append is
    part of the save's transaction, so the save raises and nothing is kept."""

    def boom(self, item, *, uow=None):
        raise RuntimeError("append failed")

    monkeypatch.setattr(StreamStore, "append", boom)
    with pytest.raises(RuntimeError, match="append failed"):
        EvItem(name="x").save()
    monkeypatch.undo()
    assert EvItem.query.get(name="x") is None
    assert EvItem.stream_len() == 0


def test_an_entry_that_cannot_be_built_is_skipped_as_on_redis(pg, caplog):
    """An empty ``_stream_name`` fails while building the entry: logged and
    skipped (the Redis no-pipeline behaviour), the record saved; inside a
    unit of work it raises, as a queued ``XADD`` does."""
    EvBroken(name="ok").save()
    assert EvBroken.query.get(name="ok") is not None
    assert "EventStreamMixin XADD failed for EvBroken" in caplog.text
    with pytest.raises(popoto.exceptions.ModelException):
        with pg.transaction() as uow:
            EvBroken(name="strict").save(pipeline=uow)
    assert EvBroken.query.get(name="strict") is None


def test_ids_are_redis_shaped_and_strictly_increasing(pg):
    store = _store(pg)
    ids = [store.xadd(KEY, {"i": i}) for i in range(50)]
    parts = [tuple(int(p) for p in i.split(b"-")) for i in ids]
    assert parts == sorted(parts) and len(set(parts)) == 50
    now_ms = int(time.time() * 1000)
    assert all(abs(ms - now_ms) < 60_000 for ms, _ in parts)
    # Several in one millisecond take seq 0, 1, 2 ...
    same = [p for p in parts if p[0] == parts[-1][0]]
    assert [s for _, s in same] == list(range(len(same)))
    # The last id survives deleting the entry that holds it.
    store.xdel(KEY, ids[-1])
    after = store.xadd(KEY, {"i": "after"})
    assert tuple(int(p) for p in after.split(b"-")) > parts[-1]
    # Explicit ids: Redis's validation and its error text.
    with pytest.raises(StreamCommandError, match="equal or smaller than the target"):
        store.xadd(KEY, {"i": 1}, id="1-1")
    with pytest.raises(StreamCommandError, match="must be greater than 0-0"):
        store.xadd("stream:fresh", {"i": 1}, id="0-0")
    assert store.exists("stream:fresh") == 0
    assert store.xadd("stream:fresh", {"i": 1}, id="5-*") == b"5-0"
    assert store.xadd("stream:fresh", {"i": 1}, id="5-*") == b"5-1"
    assert store.xadd("stream:fresh", {"i": 1}, id="7") == b"7-0"
    assert store.xadd("stream:none", {"i": 1}, nomkstream=True) is None
    assert store.exists("stream:none") == 0
    with pytest.raises(StreamDataError, match="Invalid input of type: 'bool'"):
        store.xadd(KEY, {"i": True})
    with pytest.raises(StreamDataError, match="non-empty dict"):
        store.xadd(KEY, {})


def test_concurrent_appenders_commit_in_id_order(pg):
    """The stream row's lock is held to the end of each appending
    transaction, so ids commit in id order: a reader polling the stream sees
    an id prefix every time, never a gap that fills in later."""
    store = _store(pg)
    store.xadd(KEY, {"seed": 1})
    stop = threading.Event()
    seen_gaps = []

    def writer(n):
        for i in range(40):
            with pg.transaction() as uow:
                pg.stream_append(
                    events_module.StreamAppend.of(KEY, {"w": n, "i": i}), uow=uow
                )

    def reader():
        last = None
        while not stop.is_set():
            ids = [i for i, _ in store.xrange(KEY)]
            if last is not None and ids[: len(last)] != last:
                seen_gaps.append((last, ids))
            last = ids

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()
    assert seen_gaps == []
    assert store.xlen(KEY) == 121


def test_maxlen_trims_exactly_a_documented_divergence(pg):
    """``MAXLEN ~ 5`` on Redis keeps whole radix nodes, so at least 5 and
    usually more; Postgres keeps exactly 5, the newest."""
    for i in range(12):
        EvSmall(name=f"n{i}").save()
    entries = EvSmall.stream_range()
    assert len(entries) == 5
    assert [f[b"pk"] for _, f in entries] == [
        f"EvSmall:n{i}".encode() for i in range(7, 12)
    ]
    store = _store(pg)
    assert store.xtrim("stream:pg_events_small", maxlen=2, approximate=False) == 3
    assert EvSmall.stream_len() == 2
    first = EvSmall.stream_range()[1][0]
    assert store.xtrim("stream:pg_events_small", minid=first) == 1
    assert store.xadd("stream:pg_events_small", {"a": 1}, maxlen=0) is not None
    assert EvSmall.stream_len() == 0


def test_confidence_and_strengthen_events_reach_the_stream(pg):
    a, b = EvItem(name="a", tag="t"), EvItem(name="b")
    a.save()
    b.save()
    popoto.ConfidenceField.update_confidence(a, "trust", signal=0.9)
    links = EvItem._meta.fields["links"]
    links.link(EvItem, "a", "b", initial_weight=0.5)
    links.strengthen(EvItem, "a", "b", delta=0.25)
    ops = [
        (f[b"op"], f.get(b"field"), f.get(b"delta")) for _, f in EvItem.stream_range()
    ]
    assert ops[-2:] == [
        (b"confidence_update", b"trust", None),
        (b"strengthen", None, b"0.25"),
    ]
    _, conf = EvItem.stream_range()[-2]
    assert float(conf[b"new_confidence"]) == pytest.approx(a.trust)
    assert conf[b"tag"] == b"t"


# -- consumer groups ----------------------------------------------------------------


def test_two_consumers_never_receive_one_entry(pg):
    store = _store(pg)
    store.xgroup_create(KEY, "g", id="0", mkstream=True)
    for i in range(60):
        store.xadd(KEY, {"i": i})
    got = {"w1": [], "w2": []}

    def work(name):
        while True:
            reply = store.xreadgroup("g", name, {KEY: ">"}, count=7)
            if not reply:
                return
            got[name].extend(i for i, _ in reply[0][1])

    threads = [threading.Thread(target=work, args=(n,)) for n in got]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not set(got["w1"]) & set(got["w2"])
    assert len(got["w1"]) + len(got["w2"]) == 60
    assert store.xpending(KEY, "g")["pending"] == 60


def test_claims_skip_a_pending_entry_another_transaction_holds(pg):
    """``XCLAIM``/``XAUTOCLAIM`` take the pending rows ``FOR UPDATE SKIP
    LOCKED``: one a concurrent claimer holds is passed over, not waited on
    and not claimed twice."""
    store = _store(pg)
    store.xgroup_create(KEY, "g", id="0", mkstream=True)
    ids = [store.xadd(KEY, {"i": i}) for i in range(3)]
    store.xreadgroup("g", "crashed", {KEY: ">"})
    t = pg._events_ready()
    with pg.transaction() as uow:
        uow.conn.execute(
            f"SELECT 1 FROM {t['popoto_stream_pending']} WHERE stream = %s AND "
            "grp = %s AND (ms, seq) = (%s, %s) FOR UPDATE",
            [KEY, "g", *(int(p) for p in ids[0].split(b"-"))],
        )
        cursor, claimed, deleted = store.xautoclaim(KEY, "g", "rescuer", 0)
        assert [i for i, _ in claimed] == ids[1:]
        assert store.xclaim(KEY, "g", "other", 0, [ids[0]]) == []
    owners = {
        p["message_id"]: p["consumer"]
        for p in store.xpending_range(KEY, "g", "-", "+", 10)
    }
    assert owners == {ids[0]: b"crashed", ids[1]: b"rescuer", ids[2]: b"rescuer"}


def test_a_blocking_read_wakes_on_the_append(pg):
    store = _store(pg)
    store.xgroup_create(KEY, "g", id="$", mkstream=True)
    timer = threading.Timer(0.3, lambda: EvItem(name="late").save())
    timer.start()
    t0 = time.monotonic()
    reply = store.xreadgroup("g", "w", {KEY: ">"}, block=5000)
    elapsed = time.monotonic() - t0
    timer.join()
    assert [f[b"pk"] for _, f in reply[0][1]] == [b"EvItem:late"]
    assert 0.25 < elapsed < 2.0
    assert store.xreadgroup("g", "w", {KEY: ">"}, block=200) == []


def test_a_lost_notification_is_covered_by_the_fallback_poll(pg, monkeypatch):
    """An append whose notification never arrives (sent on another
    channel here) is still read within one poll interval."""
    monkeypatch.setattr(events_module, "STREAM_WAIT_POLL_SECONDS", 0.3)
    store = _store(pg)
    store.xgroup_create(KEY, "g", id="$", mkstream=True)
    store._listener().ensure()
    monkeypatch.setattr(type(pg), "events_channel", lambda self: "popoto_elsewhere")
    threading.Timer(0.2, lambda: store.xadd(KEY, {"quiet": 1})).start()
    t0 = time.monotonic()
    reply = store.xreadgroup("g", "w", {KEY: ">"}, block=5000)
    assert reply and reply[0][1][0][1] == {b"quiet": b"1"}
    assert time.monotonic() - t0 < 1.5


def test_a_dropped_listen_session_is_reopened(pg, admin):
    store = _store(pg)
    store.xgroup_create(KEY, "g", id="$", mkstream=True)
    listener = store._listener()
    listener.ensure()
    pid = listener.conn.info.backend_pid
    admin.execute("SELECT pg_terminate_backend(%s)", [pid])
    threading.Timer(0.3, lambda: store.xadd(KEY, {"after": 1})).start()
    reply = store.xreadgroup("g", "w", {KEY: ">"}, block=5000)
    assert reply and reply[0][1][0][1] == {b"after": b"1"}
    listener.ensure()
    assert listener.conn.info.backend_pid != pid
    assert listener.reconnects >= 1


def test_the_consumer_runs_on_postgres_end_to_end(pg):
    seen = []

    async def handler(entries):
        seen.extend(f["pk"] for _, f in entries)

    for i in range(3):
        EvItem(name=f"e{i}").save()
    consumer = StreamConsumer(KEY, "g", "w1", handler, block_ms=100, model=EvItem)
    assert consumer.process_batch_sync() == 3
    assert seen == [f"EvItem:e{i}" for i in range(3)]
    assert _store(pg).xpending(KEY, "g")["pending"] == 0
    consumer.close()


# -- pub/sub ------------------------------------------------------------------------


def test_publish_counts_live_subscriptions_and_matches_patterns(pg):
    sub = pg.pubsub()
    sub.subscribe("orders")
    sub.psubscribe("ord*", "x?z")
    assert pg.publish("orders", b"one") == 2
    assert pg.publish("xyz", b"two") == 1
    assert pg.publish("nobody", b"three") == 0
    kinds = []
    for _ in range(8):
        message = sub.get_message(timeout=0.5)
        if message is None:
            break
        kinds.append((message["type"], message["channel"], message["data"]))
    assert kinds == [
        ("subscribe", b"orders", 1),
        ("psubscribe", b"ord*", 2),
        ("psubscribe", b"x?z", 3),
        ("message", b"orders", b"one"),
        ("pmessage", b"orders", b"one"),
        ("pmessage", b"xyz", b"two"),
    ]
    sub.close()
    assert pg.publish("orders", b"gone") == 0


def test_a_message_published_in_a_transaction_arrives_on_commit_only(pg):
    sub = pg.pubsub()
    sub.subscribe("tx")
    sub.get_message(timeout=0.1)  # the confirmation
    with pytest.raises(RuntimeError):
        with pg.transaction() as uow:
            pg.publish("tx", b"never", uow=uow)
            raise RuntimeError("roll back")
    assert sub.get_message(timeout=0.3) is None
    with pg.transaction() as uow:
        pg.publish("tx", b"kept", uow=uow)
        assert sub.get_message(timeout=0.2) is None
    assert sub.get_message(timeout=1.0)["data"] == b"kept"
    sub.close()


def test_a_payload_past_the_notify_limit_is_refused(pg):
    sub = pg.pubsub()
    sub.subscribe("big")
    pg.publish("big", b"x" * 5000)
    with pytest.raises(PubSubPayloadTooLarge, match="shorter than 8000 bytes"):
        pg.publish("big", b"x" * 6000)
    with pytest.raises(popoto.PublisherException):
        popoto.Publisher().publish({"blob": "x" * 7000}, channel_name="big")
    sub.close()


@pytest.mark.parametrize(
    "pattern, hits, misses",
    [
        ("news.*", ["news.a", "news."], ["news", "xnews.a"]),
        ("h?llo", ["hello", "hallo"], ["hllo", "heello"]),
        ("h[ae]llo", ["hello", "hallo"], ["hillo"]),
        ("h[^e]llo", ["hallo"], ["hello"]),
        ("h[a-b]llo", ["hallo", "hbllo"], ["hcllo"]),
        ("a\\*b", ["a*b"], ["axb"]),
        ("a.b(c)", ["a.b(c)"], ["aXb(c)"]),
    ],
)
def test_glob_patterns_mean_what_redis_means(pg, pattern, hits, misses):
    import re

    regex = re.compile(glob_to_regex(pattern), re.DOTALL)
    for name in hits:
        assert regex.fullmatch(name), (pattern, name)
    for name in misses:
        assert not regex.fullmatch(name), (pattern, name)
    sub = pg.pubsub()
    sub.psubscribe(pattern)
    for name in hits:
        assert pg.publish(name, b"m") == 1, (pattern, name)
    for name in misses:
        assert pg.publish(name, b"m") == 0, (pattern, name)
    sub.close()


def test_a_dropped_subscriber_session_resubscribes(pg, admin):
    sub = pg.pubsub()
    sub.subscribe("re")
    sub.get_message(timeout=0.1)
    admin.execute("SELECT pg_terminate_backend(%s)", [sub.pid])
    assert sub.get_message(timeout=0.2) is None  # notices the drop
    assert sub.get_message(timeout=0.1) is None  # reconnects, re-LISTENs
    assert pg.publish("re", b"back") == 1
    assert sub.get_message(timeout=1.0)["data"] == b"back"
    sub.close()


# -- zero Redis ---------------------------------------------------------------------


class _RedisRecorder:
    """Every Redis command, sync or async, and every connection checkout is
    recorded and refused."""

    def __init__(self, monkeypatch):
        import redis.asyncio.connection
        import redis.connection

        self.calls = []

        def refuse(conn_self, *args, **kwargs):
            self.calls.append(args[:1])
            raise RuntimeError("a Redis command was issued")

        async def refuse_async(conn_self, *args, **kwargs):
            self.calls.append(args[:1])
            raise RuntimeError("a Redis command was issued")

        for cls in (redis.connection.Connection,):
            monkeypatch.setattr(cls, "send_packed_command", refuse)
            monkeypatch.setattr(cls, "send_command", refuse)
        for cls in (
            redis.connection.ConnectionPool,
            redis.connection.BlockingConnectionPool,
        ):
            monkeypatch.setattr(cls, "get_connection", refuse)
        monkeypatch.setattr(
            redis.asyncio.connection.AbstractConnection,
            "send_packed_command",
            refuse_async,
        )
        monkeypatch.setattr(
            redis.asyncio.connection.ConnectionPool, "get_connection", refuse_async
        )


def test_a_postgres_bound_model_with_these_mixins_sends_redis_nothing(pg, monkeypatch):
    recorder = _RedisRecorder(monkeypatch)
    a, b = EvItem(name="a"), EvItem(name="b")
    a.save()
    b.save()
    a.tag = "x"
    a.save(update_fields=["tag"])
    popoto.ConfidenceField.update_confidence(a, "trust", signal=0.8)
    EvItem._meta.fields["links"].link(EvItem, "a", "b", initial_weight=0.5)
    EvItem._meta.fields["links"].strengthen(EvItem, "a", "b", delta=0.1)
    with pg.transaction() as uow:
        EvItem(name="c").save(pipeline=uow)
        b.delete(pipeline=uow)
    assert [f[b"op"] for _, f in EvItem.stream_range()] == [
        b"create",
        b"create",
        b"update",
        b"confidence_update",
        b"strengthen",
        b"create",
        b"delete",
    ]
    seen = []

    async def handler(entries):
        seen.extend(entries)
        if len(seen) < 8:
            raise RuntimeError("retry me")

    consumer = StreamConsumer(
        KEY, "g", "w", handler, block_ms=50, claim_timeout_ms=0, max_retries=1
    )
    with pytest.raises(RuntimeError):
        consumer.process_batch_sync()
    consumer.process_batch_sync()  # reclaims and redelivers
    consumer.process_batch_sync()  # dead-letters what is left
    consumer.close()
    assert stream_client(EvItem).xpending(KEY, "g")["pending"] == 0

    sub = popoto.Subscriber()
    sub.pubsub.subscribe("chan")
    assert popoto.Publisher().publish({"k": 1}, channel_name="chan") == 1
    received = []
    sub.handle = lambda channel, data: received.append((channel, data))
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        sub()
        time.sleep(0.01)
    sub.pubsub.close()
    assert received == [("chan", {"k": 1})]
    assert recorder.calls == []


def test_a_postgres_journal_and_its_reconciler_send_redis_nothing(pg, monkeypatch):
    """The provenance journal's appends and the reconciler's production
    trigger -- the ``StreamConsumer`` on the journal stream -- run on
    Postgres alone (the twin of ``test_reconciliation_m5.py``'s Redis
    command-allowlist spy)."""
    from popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
    from popoto.recipes.reconciliation import (
        ClaimMembership,
        reconciliation_consumer,
    )
    from tests.test_reconciliation_m5 import FakeProvider, ScriptedJudge, always

    recorder = _RedisRecorder(monkeypatch)
    first = ProvenanceJournal.append(
        agent_id="pg-recon", statement="dana prefers mornings", claim_type="preference"
    ).entry
    second = ProvenanceJournal.append(
        agent_id="pg-recon",
        statement="dana likes early starts",
        claim_type="preference",
    ).entry
    ProvenanceJournal.supersede(first, agent_id="pg-recon", statement="no longer")
    assert JournalEntry.stream_len() == 3
    consumer = reconciliation_consumer(
        agent_id="pg-recon",
        consumer_name="w",
        client=ScriptedJudge(always("same")),
        provider=FakeProvider(),
    )
    consumer.block_ms = 50
    assert consumer.process_batch_sync() == 3
    consumer.close()
    for entry in (first, second):
        assert ClaimMembership.query.get(entry_redis_key=entry.pk) is not None
    assert recorder.calls == []


def test_get_backend_is_the_streams_home(pg):
    """``stream_client`` resolves a Postgres-bound model's stream to the
    backend's store, and a stream key to the backend of the models that
    write it."""
    assert isinstance(stream_client(EvItem), StreamStore)
    assert isinstance(stream_client(stream_key=KEY), StreamStore)
    assert isinstance(stream_client(stream_key="dead:" + KEY), StreamStore)
    assert stream_client(EvItem).backend is get_backend(EvItem)


def test_the_async_store_runs_in_a_worker_thread(pg):
    store = pg.async_streams()

    async def go():
        await store.xgroup_create(KEY, "g", id="0", mkstream=True)
        await store.xadd(KEY, {"a": 1})
        return await store.xreadgroup("g", "w", {KEY: ">"}, block=100)

    reply = asyncio.run(go())
    store.close()
    assert reply[0][1][0][1] == {b"a": b"1"}


# -- the seeded probe, CI-sized ---------------------------------------------------


PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe_events_parity.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_events_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_events_probe_finds_no_undocumented_mismatch(pg):
    probe = _load_probe().run(pg, seeds=[759], shapes=20)
    assert probe.shapes == 20
    assert sum(probe.checks.values()) > 400
    assert not probe.mismatches, probe.report()
