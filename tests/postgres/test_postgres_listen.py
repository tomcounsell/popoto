"""``[PG-only]`` the shared per-process ``LISTEN`` session (#799,
``backends/postgres/listen.py``).

Every Postgres subscriber and blocking stream read in a process rides one
dedicated session per DSN. These tests count sessions by an
``application_name`` of their own (the ``POPOTO_POSTGRES_LISTEN_URL`` they
set), so the pool's connections and other clients of the server never move
the number; each test terminates whatever it left.
"""

import asyncio
import os
import threading
import time
import uuid
import warnings

import pytest

from popoto.backends.postgres import events as events_module
from popoto.backends.postgres import listen as listen_module
from popoto.backends.postgres.listen import ListenHub, hub_for
from popoto.backends.postgres.pubsub import pubsub_channel

KEY = "stream:listen799"


@pytest.fixture
def tagged(pg, admin, monkeypatch):
    """``(application_name, listen dsn)``: every ``LISTEN`` session this
    test opens reports that name."""
    from psycopg.conninfo import make_conninfo

    app = f"popoto_test_listen_{uuid.uuid4().hex[:8]}"
    dsn = make_conninfo(pg.dsn, application_name=app)
    monkeypatch.setenv(events_module.LISTEN_URL_ENV, dsn)
    try:
        yield app, dsn
    finally:
        hub_for(dsn).close()
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s",
            (app,),
        )


def _sessions(admin, app):
    admin.execute("SELECT pg_stat_clear_snapshot()")
    (n,) = admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (app,)
    ).fetchone()
    return n


def _eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _drain(sub, timeout=0.6, want=None):
    out = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = sub.get_message(timeout=0.05)
        if message is not None and message["type"] in ("message", "pmessage"):
            out.append(message["data"])
            if want is not None and len(out) >= want:
                break
    return out


def _subscribed(pg, *channels):
    sub = pg.pubsub()
    sub.subscribe(*channels)
    return sub


# -- one session, however many subscribers ------------------------------------------


def test_fifty_subscribers_hold_one_listen_session(pg, admin, tagged):
    """N subscribers used to hold N sessions; now one, and every subscriber
    gets every message, in publish order."""
    app, dsn = tagged
    subs = [_subscribed(pg, "fan") for _ in range(50)]
    assert _sessions(admin, app) == 1
    hub = hub_for(dsn)
    assert hub.sink_count(pubsub_channel(pg.schema)) == 50
    assert all(s.pid == hub.backend_pid for s in subs)
    counts = [pg.publish("fan", f"m{i}".encode()) for i in range(5)]
    assert counts == [50] * 5  # every subscription counted, under one pid
    want = [f"m{i}".encode() for i in range(5)]
    for sub in subs:
        assert _drain(sub, 2.0, want=5) == want
    assert _sessions(admin, app) == 1
    for sub in subs:
        sub.close()


def test_the_last_subscriber_to_leave_frees_the_channel_and_the_session(
    pg, admin, tagged
):
    app, dsn = tagged
    hub = hub_for(dsn)
    a, b = _subscribed(pg, "free"), _subscribed(pg, "free")
    waiter = events_module.EventListener(dsn, pg.events_channel())
    waiter.ensure()
    channel = pubsub_channel(pg.schema)
    assert hub.listening() == {channel, pg.events_channel()}
    a.close()
    assert hub.sink_count(channel) == 1 and channel in hub.listening()
    assert pg.publish("free", b"x") == 1
    b.close()
    # The pub/sub channel is UNLISTENed; the stream waiter keeps the session.
    assert _eventually(lambda: hub.listening() == {pg.events_channel()})
    admin.execute("SELECT pg_stat_clear_snapshot()")
    (query,) = admin.execute(
        "SELECT query FROM pg_stat_activity WHERE application_name = %s", (app,)
    ).fetchone()
    assert query == f'UNLISTEN "{channel}"'  # the session's last statement
    assert pg.publish("free", b"y") == 0
    waiter.close()
    # Nothing left to deliver to: the session closes.
    assert _eventually(lambda: _sessions(admin, app) == 0)
    assert not hub.connected
    # ... and opens again on the next subscription.
    c = _subscribed(pg, "free")
    assert _sessions(admin, app) == 1
    assert pg.publish("free", b"z") == 1
    assert _drain(c, want=1) == [b"z"]
    c.close()


def test_unsubscribing_everything_detaches_and_resubscribing_reattaches(pg, tagged):
    _, dsn = tagged
    hub = hub_for(dsn)
    channel = pubsub_channel(pg.schema)
    sub = _subscribed(pg, "re.a")
    assert hub.sink_count(channel) == 1
    sub.unsubscribe()
    assert hub.sink_count(channel) == 0
    assert pg.publish("re.a", b"while-away") == 0
    sub.subscribe("re.a")
    assert pg.publish("re.a", b"back") == 1
    assert _drain(sub, want=1) == [b"back"]
    sub.close()


def test_a_later_subscriber_never_sees_an_earlier_message(pg, tagged):
    """The barrier: a subscriber that joins a channel the session already
    listens on is live from a point in the notification stream, so a
    message published before it subscribed is not handed to it -- as a
    Redis ``SUBSCRIBE`` never receives an earlier ``PUBLISH``."""
    first = _subscribed(pg, "early")
    for i in range(20):
        pg.publish("early", f"before{i}".encode())
        late = _subscribed(pg, "early")
        assert _drain(late, 0.1) == []
        late.close()
    assert _drain(first, 2.0, want=20) == [f"before{i}".encode() for i in range(20)]
    first.close()


def test_nonce_dedup_and_order_survive_the_fan_out(pg, tagged):
    """``dup, dup, other, dup`` in one transaction reaches every subscriber
    as all four, in order (#787 blocker 2), through one shared session."""
    subs = [_subscribed(pg, "dups") for _ in range(5)]
    with pg.transaction() as uow:
        for message in (b"dup", b"dup", b"other", b"dup"):
            pg.publish("dups", message, uow=uow)
    for sub in subs:
        assert _drain(sub, 2.0, want=4) == [b"dup", b"dup", b"other", b"dup"]
        sub.close()


# -- reconnecting ---------------------------------------------------------------------


@pytest.fixture
def held_reconnect(monkeypatch):
    """Hold every reconnect (not the first connect) until ``release()``:
    the window a dropped session is down for, made as wide as a test needs."""
    gate = threading.Event()
    real = ListenHub._connect

    def gated(self):
        if self.connects > 0:
            gate.wait(10)
        return real(self)

    monkeypatch.setattr(ListenHub, "_connect", gated)
    return gate


def test_a_terminated_listen_session_reconnects_and_relistens(
    pg, admin, tagged, held_reconnect
):
    """The documented gap: a message published while the shared session is
    down is lost (and counted 0 -- its registrations name a dead pid), as a
    Redis subscriber loses what was published while it was disconnected.
    Once the hub reconnects and re-LISTENs, every later message arrives."""
    app, dsn = tagged
    hub = hub_for(dsn)
    subs = [_subscribed(pg, "gap") for _ in range(3)]
    assert pg.publish("gap", b"before") == 3
    old_pid = hub.backend_pid
    admin.execute("SELECT pg_terminate_backend(%s)", (old_pid,))
    assert _eventually(lambda: hub.backend_pid is None)
    assert pg.publish("gap", b"in-the-gap") == 0
    held_reconnect.set()
    assert _eventually(lambda: hub.backend_pid not in (None, old_pid))
    assert hub.reconnects == 1
    assert _sessions(admin, app) == 1
    # The hub re-registers every subscription under its new pid itself, on
    # its own session: no subscriber has polled since the drop.
    new_pid = hub.backend_pid

    def registered():
        admin.execute("SELECT pg_stat_clear_snapshot()")
        (n,) = admin.execute(
            f'SELECT count(*) FROM "{pg.schema}"."popoto_pubsub_listener" '
            "WHERE pid = %s",
            (new_pid,),
        ).fetchone()
        return n

    assert _eventually(lambda: registered() == 3)
    assert pg.publish("gap", b"after") == 3
    for sub in subs:
        assert _drain(sub, 2.0, want=2) == [b"before", b"after"]
        assert sub.pid == hub.backend_pid
        sub.close()


def test_a_reconnect_mid_stream_keeps_order_and_loses_only_the_gap(pg, admin, tagged):
    """Terminate the session while a publisher is mid-stream: what arrives
    is in publish order with no duplicate, and everything published after
    the reconnect arrives."""
    _, dsn = tagged
    hub = hub_for(dsn)
    sub = _subscribed(pg, "stream")
    published = []
    reconnected_at = []

    def publisher():
        for i in range(60):
            if i == 20:
                admin_conn = pg_admin()
                admin_conn.execute(
                    "SELECT pg_terminate_backend(%s)", (hub.backend_pid,)
                )
                admin_conn.close()
            if not reconnected_at and hub.reconnects >= 1 and hub.backend_pid:
                reconnected_at.append(i)
            pg.publish("stream", str(i).encode())
            published.append(i)
            time.sleep(0.01)

    def pg_admin():
        import psycopg

        return psycopg.connect(pg.dsn, autocommit=True)

    t = threading.Thread(target=publisher)
    t.start()
    got = []
    while t.is_alive():
        got.extend(int(d) for d in _drain(sub, 0.05))
    t.join()
    got.extend(int(d) for d in _drain(sub, 0.5))
    assert got == sorted(set(got))  # in order, no duplicate
    assert reconnected_at, "the hub never reconnected"
    # Every message after the reconnect was seen (registration catches up
    # on the subscriber's next call, delivery does not wait for it).
    assert set(range(reconnected_at[0] + 1, 60)) <= set(got)
    assert set(range(0, 20)) <= set(got)
    sub.close()


def test_a_blocking_stream_read_loses_nothing_across_a_reconnect(
    pg, admin, tagged, held_reconnect, monkeypatch
):
    """Streams have no gap: the entry appended while the session was down
    is a row, and the reconnect wakes the waiter to read it -- well before
    the fallback poll (stretched to 30 s here) would."""
    monkeypatch.setattr(events_module, "STREAM_WAIT_POLL_SECONDS", 30.0)
    _, dsn = tagged
    hub = hub_for(dsn)
    store = pg.streams()
    store.xgroup_create(KEY, "g", id="$", mkstream=True)
    listener = events_module.EventListener(dsn, pg.events_channel())
    out = {}

    def reader():
        t0 = time.monotonic()
        out["reply"] = store.xreadgroup(
            "g", "w", {KEY: ">"}, block=20000, listener=listener
        )
        out["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=reader)
    t.start()
    assert _eventually(lambda: hub.sink_count(pg.events_channel()) == 1)
    time.sleep(0.2)  # the reader is waiting
    old_pid = hub.backend_pid
    admin.execute("SELECT pg_terminate_backend(%s)", (old_pid,))
    assert _eventually(lambda: hub.backend_pid is None)
    store.xadd(KEY, {"during": 1})
    held_reconnect.set()
    t.join(10)
    assert out["reply"] and out["reply"][0][1][0][1] == {b"during": b"1"}
    assert out["elapsed"] < 5.0
    assert listener.reconnects == 1
    listener.close()


# -- fork ----------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_child_opens_its_own_session_and_leaves_the_parents(pg, admin, tagged):
    app, dsn = tagged
    sub = _subscribed(pg, "forked")
    assert sub.get_message(timeout=0.1)["type"] == "subscribe"
    parent_hub = hub_for(dsn)
    parent_pid = parent_hub.backend_pid
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads
        child = os.fork()
    if child == 0:  # pragma: no cover - runs in the child
        code = 1
        try:
            # The inherited subscriber re-attaches to the child's own hub.
            assert sub.get_message(timeout=0.1) is None
            mine = hub_for(dsn)
            assert mine is not parent_hub and mine.backend_pid != parent_pid
            assert sub.pid == mine.backend_pid
            assert pg.publish("forked", b"from-child") == 2
            got = _drain(sub, 3.0, want=1)
            os.write(write_fd, repr((mine.backend_pid, got)).encode())
            sub.close()
            code = 0
        except BaseException:
            import traceback

            os.write(write_fd, traceback.format_exc().encode())
        finally:
            os._exit(code)
    os.close(write_fd)
    _, status = os.waitpid(child, 0)
    report = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    assert os.waitstatus_to_exitcode(status) == 0, report
    child_pid, child_got = eval(report)
    assert child_got == [b"from-child"]
    assert child_pid != parent_pid
    # The parent's session survived the child (which never closed it).
    assert parent_hub.backend_pid == parent_pid
    assert _eventually(lambda: _sessions(admin, app) == 1)
    assert pg.publish("forked", b"from-parent") == 1
    assert _drain(sub, 2.0, want=2) == [b"from-child", b"from-parent"]
    sub.close()


# -- async, and sync and async together -----------------------------------------------


def test_fifty_async_stream_readers_hold_one_listen_session(pg, admin, tagged):
    app, dsn = tagged
    store = pg.streams()
    for i in range(50):
        store.xgroup_create(KEY, f"g{i}", id="$", mkstream=True)

    async def go():
        clients = [pg.async_streams() for _ in range(50)]
        tasks = [
            asyncio.create_task(c.xreadgroup(f"g{i}", "w", {KEY: ">"}, block=10000))
            for i, c in enumerate(clients)
        ]
        await asyncio.sleep(0.5)
        sessions = _sessions(admin, app)
        store.xadd(KEY, {"to": "all"})  # not to_thread: the readers fill its pool
        replies = await asyncio.wait_for(asyncio.gather(*tasks), 20)
        for c in clients:
            c.close()
        return sessions, replies

    sessions, replies = asyncio.run(go())
    assert sessions == 1
    bad = [
        (i, r)
        for i, r in enumerate(replies)
        if not (r and r[0][1][0][1] == {b"to": b"all"})
    ]
    assert not bad, bad[:3]


def test_cancelling_an_async_read_ends_its_wait_without_claiming(
    pg, tagged, monkeypatch
):
    """Cancellation safety: the worker thread is woken at once and returns
    without reading again, so an entry appended after the cancel stays in
    the group for the next reader instead of being claimed by nobody."""
    monkeypatch.setattr(events_module, "STREAM_WAIT_POLL_SECONDS", 30.0)
    store = pg.streams()
    store.xgroup_create(KEY, "g", id="$", mkstream=True)
    finished = []
    real = events_module.StreamStore.xreadgroup

    def tracked(self, *args, **kwargs):
        try:
            return real(self, *args, **kwargs)
        finally:
            finished.append(time.monotonic())

    monkeypatch.setattr(events_module.StreamStore, "xreadgroup", tracked)

    async def go():
        client = pg.async_streams()
        task = asyncio.create_task(client.xreadgroup("g", "w", {KEY: ">"}, block=20000))
        await asyncio.sleep(0.3)
        task.cancel()
        t0 = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        client.close()
        return t0

    t0 = asyncio.run(go())
    assert finished and finished[0] - t0 < 1.0
    store.xadd(KEY, {"after": "cancel"})
    assert store.xpending(KEY, "g")["pending"] == 0
    reply = store.xreadgroup("g", "w2", {KEY: ">"})
    assert reply[0][1][0][1] == {b"after": b"cancel"}


def test_sync_subscribers_and_async_readers_share_the_session(pg, admin, tagged):
    app, dsn = tagged
    store = pg.streams()
    for i in range(10):
        store.xgroup_create(KEY, f"m{i}", id="$", mkstream=True)
    subs = [_subscribed(pg, "mixed") for _ in range(10)]
    sync_replies = []

    def sync_reader(i):
        sync_replies.append(store.xreadgroup(f"m{i}", "w", {KEY: ">"}, block=10000))

    threads = [threading.Thread(target=sync_reader, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()

    async def go():
        clients = [pg.async_streams() for _ in range(5)]
        tasks = [
            asyncio.create_task(c.xreadgroup(f"m{i + 5}", "w", {KEY: ">"}, block=10000))
            for i, c in enumerate(clients)
        ]
        await asyncio.sleep(0.5)
        sessions = _sessions(admin, app)
        store.xadd(KEY, {"mixed": 1})
        pg.publish("mixed", b"hello")
        replies = await asyncio.wait_for(asyncio.gather(*tasks), 20)
        for c in clients:
            c.close()
        return sessions, replies

    sessions, async_replies = asyncio.run(go())
    for t in threads:
        t.join(10)
    assert sessions == 1
    assert len(sync_replies) == 5 and all(sync_replies)
    assert all(async_replies)
    for sub in subs:
        assert _drain(sub, 2.0, want=1) == [b"hello"]
        sub.close()


def test_a_dead_subscriber_detaches_when_collected(pg, tagged):
    import gc

    _, dsn = tagged
    hub = hub_for(dsn)
    channel = pubsub_channel(pg.schema)
    sub = _subscribed(pg, "gc")
    assert hub.sink_count(channel) == 1
    del sub
    gc.collect()
    assert hub.sink_count(channel) == 0


def test_hubs_are_per_dsn_and_per_process(pg, tagged):
    _, dsn = tagged
    assert hub_for(dsn) is hub_for(dsn)
    assert hub_for(dsn) is not hub_for(dsn + " ")
    assert hub_for(dsn).pid == os.getpid()
    assert listen_module._hubs[(dsn, os.getpid())] is hub_for(dsn)


def test_an_unreachable_listen_url_fails_subscribe_and_leaves_no_thread(
    pg, monkeypatch
):
    """``subscribe`` raises the connection's error, as a direct connect did,
    and a hub nobody could attach to stops its thread."""
    import psycopg

    bad = "postgresql://localhost:1/nowhere?connect_timeout=1"
    monkeypatch.setenv(events_module.LISTEN_URL_ENV, bad)
    sub = pg.pubsub()
    with pytest.raises(psycopg.OperationalError):
        sub.subscribe("nowhere")
    hub = hub_for(bad)
    assert _eventually(lambda: hub._thread is None)
    assert hub.sink_count() == 0


# -- #803: byte cap, fork-safe cleanup, slow-subscriber reconnect, dead sessions -----


def test_an_unread_subscriber_is_capped_by_bytes_and_counts_what_it_drops(
    pg, tagged, monkeypatch, caplog
):
    """A subscriber that stops reading keeps at most
    ``Defaults.PG_LISTEN_QUEUE_MAX_BYTES`` of payload (as well as the
    message-count cap): the oldest go, the newest stay in order, the drop is
    logged, and both the subscriber and the hub count it."""
    import logging

    from popoto.fields.constants import Defaults

    _, dsn = tagged
    fast = _subscribed(pg, "bytes")  # made under the 32 MiB default
    # The caps are read when a subscriber is made: only ``slow`` gets 20 KB.
    monkeypatch.setattr(Defaults, "PG_LISTEN_QUEUE_MAX_BYTES", 20_000)
    slow = _subscribed(pg, "bytes")  # never read until the end
    hub = hub_for(dsn)
    body = b"x" * 4000  # ~5.3 KB per notification once base64-encoded
    sent = [str(i).encode() + body for i in range(12)]
    with caplog.at_level(logging.WARNING, logger="POPOTO.postgres.listen"):
        for message in sent:
            assert pg.publish("bytes", message) == 2
        assert _drain(fast, 3.0, want=12) == sent  # the reader loses nothing
        assert _eventually(lambda: slow.dropped >= 9)
    kept = _drain(slow, 2.0)
    assert kept and kept == sent[-len(kept) :]  # the newest, in order
    assert len(kept) + slow.dropped == len(sent)
    assert slow.queued_bytes == 0  # drained
    assert hub.dropped == slow.dropped and fast.dropped == 0
    assert any("PG_LISTEN_QUEUE_MAX_BYTES" in r.getMessage() for r in caplog.records)
    slow.close()
    fast.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.parametrize("how", ["close", "unsubscribe"])
def test_a_forked_child_never_drops_the_parents_registrations(pg, tagged, how):
    """An inherited subscriber closed (or unsubscribed) in a forked child
    before it ever polled there must not delete the rows ``publish`` counts
    for the parent, which is still subscribed and still receiving."""
    sub = _subscribed(pg, "fork-rows")
    assert sub.get_message(timeout=0.1)["type"] == "subscribe"
    assert pg.publish("fork-rows", b"before") == 1
    assert _drain(sub, 2.0, want=1) == [b"before"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads
        child = os.fork()
    if child == 0:  # pragma: no cover - runs in the child
        code = 1
        try:
            if how == "close":
                sub.close()
            else:
                sub.unsubscribe("fork-rows")
            code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert pg.publish("fork-rows", b"after") == 1
    assert _drain(sub, 2.0, want=1) == [b"after"]
    sub.close()


def test_a_slow_subscriber_write_does_not_stall_the_reconnect(pg, admin, tagged):
    """Re-registration after a reconnect works from a snapshot: a subscriber
    whose own registration write is slow (here: a handler that subscribes,
    with the write held for 4 s) neither delays the hub's reconnect nor the
    other subscribers' counts and deliveries."""
    _, dsn = tagged
    hub = hub_for(dsn)
    release = threading.Event()
    in_write = threading.Event()

    def slow_handler(message):
        slow.subscribe("slow-b")  # its registration write is the slow part

    slow = _subscribed(pg, "slow-a")
    slow.subscribe(**{"slow-trigger": slow_handler})
    fast = _subscribed(pg, "fast")
    real_insert = slow._insert

    def held_insert(row):
        in_write.set()
        release.wait(4.0)
        real_insert(row)

    slow._insert = held_insert
    reader = threading.Thread(target=lambda: _drain(slow, 6.0))
    reader.start()
    try:
        pg.publish("slow-trigger", b"go")
        assert in_write.wait(3.0)
        old_pid = hub.backend_pid
        admin.execute("SELECT pg_terminate_backend(%s)", (old_pid,))
        started = time.monotonic()
        assert _eventually(
            lambda: hub.backend_pid not in (None, old_pid)
            and pg.publish("fast", b"probe") == 1,
            timeout=2.0,
        )
        assert _drain(fast, 2.0, want=1) == [b"probe"]
        assert time.monotonic() - started < 2.5  # well inside the held write
        assert pg.publish("slow-a", b"x") == 1  # re-registered from the snapshot
    finally:
        release.set()
        reader.join(10)
    assert _eventually(lambda: pg.publish("slow-b", b"y") == 1)
    slow.close()
    fast.close()


class _Proxy:
    """A TCP forwarder to the Postgres server whose existing connections can
    be frozen -- every byte held, every socket left open -- the way a NAT
    entry that silently expired looks from the client: no FIN, no RST,
    nothing. New connections are forwarded normally."""

    def __init__(self, host, port):
        import socket

        self._upstream = (host, port)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self._pairs = []
        self._frozen = set()
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import socket

        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            server = socket.create_connection(self._upstream)
            pair = (client, server)
            self._pairs.append(pair)
            threading.Thread(target=self._pump, args=(pair,), daemon=True).start()

    def _pump(self, pair):
        import select

        client, server = pair
        while not self._closed:
            if id(pair) in self._frozen:
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select([client, server], [], [], 0.05)
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return self._shut(pair)
                    (server if sock is client else client).sendall(data)
            except OSError:
                return self._shut(pair)

    def _shut(self, pair):
        for sock in pair:
            try:
                sock.close()
            except OSError:
                pass

    def freeze(self):
        self._frozen.update(id(p) for p in self._pairs)

    def close(self):
        self._closed = True
        self._listener.close()
        for pair in self._pairs:
            self._shut(pair)


@pytest.fixture
def proxied(pg, admin, monkeypatch):
    """``(application_name, dsn, proxy)``: the shared session reaches the
    server through a :class:`_Proxy`."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(pg.dsn)
    host = params.get("host") or "localhost"
    if host.startswith("/"):
        pytest.skip("the Postgres URL names a unix socket; the proxy needs TCP")
    proxy = _Proxy(host, int(params.get("port") or 5432))
    app = f"popoto_test_listen_{uuid.uuid4().hex[:8]}"
    dsn = make_conninfo(
        pg.dsn,
        host="127.0.0.1",
        hostaddr="127.0.0.1",
        port=proxy.port,
        application_name=app,
    )
    monkeypatch.setenv(events_module.LISTEN_URL_ENV, dsn)
    try:
        yield app, dsn, proxy
    finally:
        hub_for(dsn).close()
        proxy.close()
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s",
            (app,),
        )


def test_the_shared_session_sets_tcp_keepalives(pg, proxied):
    from popoto.fields.constants import Defaults

    _, dsn, _ = proxied
    sub = _subscribed(pg, "keepalive")
    info = {
        o.keyword.decode(): (o.val or b"").decode()
        for o in hub_for(dsn)._conn.pgconn.info
    }
    assert info["keepalives"] == str(Defaults.PG_LISTEN_KEEPALIVES)
    assert info["keepalives_idle"] == str(Defaults.PG_LISTEN_KEEPALIVES_IDLE_SECONDS)
    assert info["keepalives_interval"] == str(
        Defaults.PG_LISTEN_KEEPALIVES_INTERVAL_SECONDS
    )
    assert info["keepalives_count"] == str(Defaults.PG_LISTEN_KEEPALIVES_COUNT)
    sub.close()


def test_a_silently_dead_session_is_detected_and_replaced(
    pg, admin, proxied, monkeypatch
):
    """A connection that goes silent (a NAT drop: no FIN, no RST) is noticed
    by the hub's bounded liveness check -- a ``SELECT 1`` after
    ``PG_LISTEN_LIVENESS_INTERVAL_SECONDS`` without traffic, answered within
    ``PG_LISTEN_LIVENESS_TIMEOUT_SECONDS`` or the session is dropped -- and
    replaced, re-registered and delivering again within that bound."""
    from popoto.fields.constants import Defaults

    monkeypatch.setattr(Defaults, "PG_LISTEN_LIVENESS_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(Defaults, "PG_LISTEN_LIVENESS_TIMEOUT_SECONDS", 0.5)
    _, dsn, proxy = proxied
    sub = _subscribed(pg, "nat")
    hub = hub_for(dsn)
    assert pg.publish("nat", b"before") == 1
    assert _drain(sub, 2.0, want=1) == [b"before"]
    old_pid = hub.backend_pid
    proxy.freeze()
    started = time.monotonic()
    assert _eventually(lambda: hub.backend_pid not in (None, old_pid), timeout=5.0)
    assert time.monotonic() - started < 3.0  # interval + timeout, plus slack
    assert hub.reconnects == 1
    assert _eventually(lambda: pg.publish("nat", b"probe") == 1)
    got = _drain(sub, 2.0)
    assert got and set(got) == {b"probe"}
    sub.close()
