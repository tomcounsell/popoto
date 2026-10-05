"""Publisher / Subscriber on both conformance legs (#759 M5).

On Redis a subscriber holds a ``PubSub`` connection and a publish is
``PUBLISH``; on Postgres the subscriber holds a dedicated ``LISTEN`` session
and a publish is ``pg_notify`` (``backends/postgres/pubsub.py``). The same
assertions run on both: what ``publish`` returns, the message dicts the
subscriber's ``pubsub`` hands back, ``pre_handle``/``handle`` dispatch, and a
pipeline publish that is delivered when its unit of work runs.

Channel names carry a fresh suffix: Redis ``PUBLISH`` ignores the database
number, so another suite running concurrently on another DB shares channels.
"""

import contextlib
import sys
import os
import time
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.backends import get_backend  # noqa: E402
from src.popoto.redis_db import get_REDIS_DB  # noqa: E402

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class MyPublishableModel(popoto.Model):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher = popoto.Publisher()

    def save(self, pipeline=None, *args, **kwargs):
        super().save(pipeline=pipeline, *args, **kwargs)
        self.publisher.publish(
            {"key": self.db_key, "value": self.value}, pipeline=pipeline
        )


pub_thing = MyPublishableModel()


def _channel(name):
    return f"{name}-{uuid.uuid4().hex[:8]}"


def _next(subscriber, timeout=2.0, kinds=("message", "pmessage")):
    """The next message of ``kinds`` from the subscriber's pubsub."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = subscriber.pubsub.get_message(timeout=0.05)
        if message is not None and message["type"] in kinds:
            return message
    return None


@contextlib.contextmanager
def _unit_of_work():
    """A Redis pipeline executed on exit, or the Postgres backend's
    transaction committed on exit."""
    backend = get_backend()
    if backend.name == "redis":
        pipe = get_REDIS_DB().pipeline()
        yield pipe
        pipe.execute()
    else:
        with backend.transaction() as uow:
            yield uow


class Recorder(popoto.Subscriber):
    sub_channel_names: list = []

    def __init__(self, channels, *args, **kwargs):
        self.sub_channel_names = list(channels)
        self.seen = []
        self.pre = []
        super().__init__(*args, **kwargs)

    def pre_handle(self, channel, data, *args, **kwargs):
        self.pre.append(channel)

    def handle(self, channel, data, *args, **kwargs):
        self.seen.append((channel, data))


def _drain(subscriber, want, timeout=2.0):
    deadline = time.monotonic() + timeout
    while len(subscriber.seen) < want and time.monotonic() < deadline:
        subscriber()
        time.sleep(0.005)


@pytest.fixture
def subscribers():
    made = []
    yield made
    for sub in made:
        sub.pubsub.close()


def test_publish_reaches_the_subscriber_and_counts_it(subscribers):
    ch = _channel("orders")
    sub = Recorder([ch])
    subscribers.append(sub)
    publisher = popoto.Publisher()
    assert publisher.publish({"order": 1, "status": "new"}, channel_name=ch) == 1
    _drain(sub, 1)
    assert sub.seen == [(ch, {"order": 1, "status": "new"})]
    assert sub.pre == [ch]


def test_publish_with_no_subscriber_counts_zero():
    assert popoto.Publisher().publish({"a": 1}, channel_name=_channel("nobody")) == 0


def test_the_subscription_confirmation_and_message_shapes(subscribers):
    ch = _channel("shape")
    sub = Recorder([ch])
    subscribers.append(sub)
    confirmation = _next(sub, kinds=("subscribe",))
    assert confirmation == {
        "type": "subscribe",
        "pattern": None,
        "channel": ch.encode(),
        "data": 1,
    }
    popoto.Publisher().publish({"x": 1}, channel_name=ch)
    message = _next(sub)
    assert message["type"] == "message"
    assert message["pattern"] is None
    assert message["channel"] == ch.encode()
    assert isinstance(message["data"], bytes)


def test_pattern_subscriptions_deliver_pmessages(subscribers):
    base = _channel("news")
    sub = Recorder([])
    subscribers.append(sub)
    sub.pubsub.psubscribe(f"{base}.*")
    assert _next(sub, kinds=("psubscribe",))["channel"] == f"{base}.*".encode()
    assert popoto.Publisher().publish({"n": 1}, channel_name=f"{base}.sport") == 1
    message = _next(sub)
    assert message["type"] == "pmessage"
    assert message["pattern"] == f"{base}.*".encode()
    assert message["channel"] == f"{base}.sport".encode()
    assert popoto.Publisher().publish({"n": 2}, channel_name=f"{base}x") == 0


def test_multi_channel_subscriber_routes_by_channel(subscribers):
    a, b = _channel("a"), _channel("b")
    sub = Recorder([a, b])
    subscribers.append(sub)
    popoto.Publisher().publish({"v": "first"}, channel_name=a)
    popoto.Publisher().publish({"v": "second"}, channel_name=b)
    _drain(sub, 2)
    assert sub.seen == [(a, {"v": "first"}), (b, {"v": "second"})]


def test_unsubscribe_stops_delivery(subscribers):
    ch = _channel("gone")
    sub = Recorder([ch])
    subscribers.append(sub)
    sub.pubsub.unsubscribe(ch)
    confirmation = _next(sub, kinds=("unsubscribe",))
    assert confirmation["channel"] == ch.encode() and confirmation["data"] == 0
    assert popoto.Publisher().publish({"v": 1}, channel_name=ch) == 0
    assert _next(sub, timeout=0.3) is None


def test_a_pipeline_publish_is_delivered_when_the_unit_runs(subscribers):
    ch = _channel("batch")
    sub = Recorder([ch])
    subscribers.append(sub)
    publisher = popoto.Publisher(channel_name=ch)
    with _unit_of_work() as pipe:
        assert publisher.publish({"order": "o1"}, pipeline=pipe) is pipe
        assert publisher.publish({"order": "o2"}, pipeline=pipe) is pipe
        sub()
        assert sub.seen == []  # nothing before the unit runs
    _drain(sub, 2)
    assert sub.seen == [(ch, {"order": "o1"}), (ch, {"order": "o2"})]


def test_an_empty_publish_sends_nothing():
    assert popoto.Publisher().publish({}, channel_name=_channel("empty")) is None


def test_identical_messages_in_one_unit_are_each_delivered(subscribers):
    """#787 review blocker 2: a Redis ``MULTI`` delivers every ``PUBLISH``;
    Postgres folds identical ``NOTIFY`` payloads in one transaction unless
    each carries a nonce. ``dup, dup, other, dup`` arrives whole, in order."""
    ch = _channel("dups")
    sub = Recorder([ch])
    subscribers.append(sub)
    publisher = popoto.Publisher(channel_name=ch)
    with _unit_of_work() as pipe:
        for value in ("dup", "dup", "other", "dup"):
            publisher.publish({"v": value}, pipeline=pipe)
    _drain(sub, 4)
    assert [data["v"] for _, data in sub.seen] == ["dup", "dup", "other", "dup"]


class BatchNote(popoto.Model):
    name = popoto.KeyField()


def test_a_batch_publish_is_delivered_at_execute_and_never_after_reset(
    subscribers,
):
    """#787 review blocker 4: a ``Publisher`` handed a ``popoto.batch()``
    publishes on its own backend inside the batch -- a queued ``PUBLISH`` on
    Redis, a ``NOTIFY`` in the batch's transaction on Postgres (never a
    Redis ``PUBLISH``) -- so the message arrives at ``execute()``, never
    after ``reset()``, and rides in one batch with a model save."""
    ch = _channel("batched")
    sub = Recorder([ch])
    subscribers.append(sub)
    publisher = popoto.Publisher(channel_name=ch)
    backend = get_backend()

    opened = []
    try:
        pipe = popoto.batch()
        opened.append(pipe)
        BatchNote(name="dropped").save(pipeline=pipe)
        assert publisher.publish({"n": "dropped"}, pipeline=pipe) is pipe
        pipe.reset()

        pipe = popoto.batch()
        opened.append(pipe)
        BatchNote(name="kept").save(pipeline=pipe)
        assert publisher.publish({"n": "kept"}, pipeline=pipe) is pipe
        queued = [args[0][0] for args in pipe.command_stack]
        if backend.name == "redis":
            assert "PUBLISH" in queued
        else:
            assert queued == []  # nothing for Redis: it joined the transaction
        sub()
        assert sub.seen == []
        pipe.execute()
    finally:
        for opened_pipe in opened:
            opened_pipe.reset()  # never leave a transaction holding locks
    _drain(sub, 1)
    assert sub.seen == [(ch, {"n": "kept"})]
    assert BatchNote.query.get(name="kept") is not None
    assert BatchNote.query.get(name="dropped") is None
    for note in BatchNote.query.all():
        note.delete()
