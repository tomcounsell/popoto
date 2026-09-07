"""Tests for TombstoneStore — field-layer keeper of the tombstone keyspace (#649).

Coverage:
- key naming: $TOMB:{ModelName}:data / $TOMB:{ModelName}:index
- each method's exact Redis command sequence (transactional methods issue
  their two commands inside one transactional pipeline)
- eviction order is oldest-first
- get_entries preserves argument order, including None holes
- purge returns False for an absent key
- purge_all issues a single DEL over both keys
- _unpack_tombstone_entry logs through the POPOTO.MemoryLifecycle logger and
  returns the drop signal on a partial entry (guards the logger rebinding)
"""

import logging
import time

import msgpack
import redis.client

from src import popoto
from src.popoto.fields.tombstone_store import (
    TOMBSTONE_KEY_PREFIX,
    TombstoneStore,
    _unpack_tombstone_entry,
)


class Widget(popoto.Model):
    """Trivial model — only its class name feeds the tombstone keyspace."""

    key = popoto.AutoKeyField()


def _store():
    return TombstoneStore(Widget)


def _pack(**overrides):
    entry = {
        "redis_key": "Widget:1",
        "fingerprint": "fp1",
        "tier": "episodic",
        "importance_at_death": 0.5,
        "confidence_at_death": None,
        "evidence_count": 0,
        "dismissal_count": 0,
        "tombstoned_at": time.time(),
        "reason": "policy",
    }
    entry.update(overrides)
    return msgpack.packb(entry, use_bin_type=True)


# ---------------------------------------------------------------------------
# Command spying helpers
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def record(self, name):
        self.calls.append(name)

    def names(self):
        return list(self.calls)


def _spy_client_methods(monkeypatch, client, recorder, method_names):
    """Wrap named client methods to record (name) before calling through."""
    for name in method_names:
        original = getattr(client, name)

        def make_wrapper(name=name, original=original):
            def wrapper(*args, **kwargs):
                recorder.record(name.upper())
                return original(*args, **kwargs)

            return wrapper

        monkeypatch.setattr(client, name, make_wrapper())


def _spy_pipeline_execute(monkeypatch, recorder):
    """Wrap Pipeline.execute to record queued command names before running."""
    original_execute = redis.client.Pipeline.execute

    def wrapper(self, *args, **kwargs):
        for entry in self.command_stack:
            # redis-py queues each command as (args, kwargs); args[0] is the
            # command name (e.g. "HSET").
            cmd_args = entry.args if hasattr(entry, "args") else entry[0]
            recorder.record(str(cmd_args[0]).upper())
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(redis.client.Pipeline, "execute", wrapper)


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------


def test_keys_returns_expected_names():
    data_key, index_key = _store().keys()
    assert data_key == f"{TOMBSTONE_KEY_PREFIX}:Widget:data"
    assert index_key == f"{TOMBSTONE_KEY_PREFIX}:Widget:index"


# ---------------------------------------------------------------------------
# Command sequences
# ---------------------------------------------------------------------------


def test_archive_issues_hset_then_zadd_in_one_transaction(monkeypatch):
    recorder = _Recorder()
    _spy_pipeline_execute(monkeypatch, recorder)

    store = _store()
    store.archive("Widget:1", _pack(), time.time())

    assert recorder.names() == ["HSET", "ZADD"]

    data_key, index_key = store.keys()
    assert popoto.get_redis().hexists(data_key, "Widget:1")
    assert popoto.get_redis().zscore(index_key, "Widget:1") is not None


def test_count_issues_zcard(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), time.time())

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["zcard"])

    assert store.count() == 1
    assert recorder.names() == ["ZCARD"]


def test_oldest_keys_issues_zrange(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)
    store.archive("Widget:2", _pack(), 200.0)

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["zrange"])

    result = store.oldest_keys(1)
    assert result == ["Widget:1"]
    assert recorder.names() == ["ZRANGE"]


def test_newest_keys_issues_zrevrange(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)
    store.archive("Widget:2", _pack(), 200.0)

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["zrevrange"])

    result = store.newest_keys(-1)
    assert result == ["Widget:2", "Widget:1"]
    assert recorder.names() == ["ZREVRANGE"]


def test_evict_issues_hdel_then_zrem_in_one_transaction(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)

    recorder = _Recorder()
    _spy_pipeline_execute(monkeypatch, recorder)

    store.evict(["Widget:1"])

    assert recorder.names() == ["HDEL", "ZREM"]
    assert store.count() == 0


def test_get_entry_issues_hget(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["hget"])

    raw = store.get_entry("Widget:1")
    assert raw is not None
    assert recorder.names() == ["HGET"]


def test_get_entries_issues_hmget(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["hmget"])

    raws = store.get_entries(["Widget:1"])
    assert len(raws) == 1
    assert recorder.names() == ["HMGET"]


def test_purge_issues_hdel_then_zrem_in_one_transaction(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)

    recorder = _Recorder()
    _spy_pipeline_execute(monkeypatch, recorder)

    assert store.purge("Widget:1") is True
    assert recorder.names() == ["HDEL", "ZREM"]
    assert store.count() == 0


def test_purge_all_issues_single_del_over_both_keys(monkeypatch):
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)
    data_key, index_key = store.keys()

    recorder = _Recorder()
    _spy_client_methods(monkeypatch, popoto.get_redis(), recorder, ["delete"])

    removed = store.purge_all()
    assert removed == 1
    assert recorder.names() == ["DELETE"]

    redis_client = popoto.get_redis()
    assert redis_client.exists(data_key) == 0
    assert redis_client.exists(index_key) == 0


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


def test_eviction_order_is_oldest_first():
    store = _store()
    store.archive("Widget:1", _pack(), 100.0)
    store.archive("Widget:2", _pack(), 200.0)
    store.archive("Widget:3", _pack(), 50.0)

    assert store.oldest_keys(2) == ["Widget:3", "Widget:1"]


def test_get_entries_preserves_order_with_none_holes():
    store = _store()
    store.archive("Widget:1", _pack(redis_key="Widget:1"), 100.0)
    store.archive("Widget:3", _pack(redis_key="Widget:3"), 300.0)

    raws = store.get_entries(["Widget:1", "Widget:2", "Widget:3"])
    assert len(raws) == 3
    assert raws[1] is None
    assert msgpack.unpackb(raws[0], raw=False)["redis_key"] == "Widget:1"
    assert msgpack.unpackb(raws[2], raw=False)["redis_key"] == "Widget:3"


def test_purge_returns_false_for_absent_key():
    store = _store()
    assert store.purge("Widget:does-not-exist") is False


def test_purge_all_returns_zero_when_empty():
    store = _store()
    assert store.purge_all() == 0


# ---------------------------------------------------------------------------
# Logger rebinding guard
# ---------------------------------------------------------------------------


def test_unpack_partial_entry_logs_via_memory_lifecycle_logger_and_drops(caplog):
    partial = msgpack.packb({"redis_key": "Widget:1"}, use_bin_type=True)

    with caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle"):
        result = _unpack_tombstone_entry(partial)

    assert result is None
    assert any(
        "missing required keys" in record.message
        for record in caplog.records
        if record.name == "POPOTO.MemoryLifecycle"
    )


def test_unpack_none_returns_none():
    assert _unpack_tombstone_entry(None) is None


def test_unpack_complete_entry_returns_dict():
    packed = _pack()
    entry = _unpack_tombstone_entry(packed)
    assert entry is not None
    assert entry["redis_key"] == "Widget:1"
