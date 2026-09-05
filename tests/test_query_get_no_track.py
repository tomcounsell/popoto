"""``Query.get(..., _no_track=True)``: load one record without staging a read.

``AccessTrackerMixin`` models stage a read (``RPUSH`` onto
``$AT:{Class}:staged:{key}``) every time ``Query.get`` hydrates them. Internal
callers that load a record only to delete it (``DefaultMemory`` eviction,
#630) need the same single ``HGETALL`` with no staging side effect.

Covered here, for both ``get`` and its async mirror ``async_get``:

- ``_no_track=True`` on the direct-key path leaves no staged list and no
  access log
- the default path still stages exactly one read
- a missing key returns ``None`` and stages nothing either way
- the filter-fallback path honors the flag the same way
"""

import asyncio
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module


class NoTrackItem(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    tag = popoto.Field(default="")


def _staged_key(record):
    return f"$AT:NoTrackItem:staged:{record.db_key.redis_key}"


def _access_log_key(record):
    return f"$AT:NoTrackItem:access_log:{record.db_key.redis_key}"


def _staged_len(record):
    return POPOTO_REDIS_DB.llen(_staged_key(record))


def _wipe_tracker_keys():
    for key in POPOTO_REDIS_DB.scan_iter("$AT:NoTrackItem:*"):
        POPOTO_REDIS_DB.delete(key)


@pytest.fixture(autouse=True)
def clean():
    NoTrackItem.delete_all()
    _wipe_tracker_keys()
    # The async client is bound to an event loop; pytest-asyncio makes a
    # fresh loop per test, so drop the cached connection (see test_async.py).
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()
    yield
    NoTrackItem.delete_all()
    _wipe_tracker_keys()


@pytest.fixture
def saved():
    record = NoTrackItem(name="alpha", tag="t1")
    record.save()
    assert _staged_len(record) == 0
    return record


class TestSyncGet:
    def test_no_track_direct_key_stages_nothing(self, saved):
        loaded = NoTrackItem.query.get(redis_key=saved.db_key.redis_key, _no_track=True)
        assert loaded is not None
        assert loaded.name == "alpha"
        assert not POPOTO_REDIS_DB.exists(_staged_key(saved))
        assert not POPOTO_REDIS_DB.exists(_access_log_key(saved))

    def test_no_track_by_key_fields_stages_nothing(self, saved):
        loaded = NoTrackItem.query.get(name="alpha", _no_track=True)
        assert loaded is not None
        assert not POPOTO_REDIS_DB.exists(_staged_key(saved))

    def test_default_direct_key_still_stages_one_read(self, saved):
        loaded = NoTrackItem.query.get(redis_key=saved.db_key.redis_key)
        assert loaded is not None
        assert _staged_len(saved) == 1

    def test_missing_key_returns_none_and_stages_nothing(self):
        ghost = NoTrackItem(name="ghost")
        assert NoTrackItem.query.get(redis_key=ghost.db_key.redis_key) is None
        assert (
            NoTrackItem.query.get(redis_key=ghost.db_key.redis_key, _no_track=True)
            is None
        )
        assert not POPOTO_REDIS_DB.exists(_staged_key(ghost))

    def test_no_track_on_filter_fallback(self, saved):
        loaded = NoTrackItem.query.get(tag="t1", _no_track=True)
        assert loaded is not None
        assert loaded.name == "alpha"
        assert not POPOTO_REDIS_DB.exists(_staged_key(saved))
        # The default fallback stages, proving the flag made the difference.
        # The plain-field filter path hydrates twice on main today (each
        # hydration stages a read), so pin "at least one" rather than the
        # count that fix will change.
        assert NoTrackItem.query.get(tag="t1") is not None
        assert _staged_len(saved) >= 1

    def test_exactly_one_hgetall_on_direct_key(self, saved, monkeypatch):
        """The untracked load must not add a probe or re-read."""
        calls = []
        real = POPOTO_REDIS_DB.hgetall

        def spy(key, *args, **kwargs):
            calls.append(key)
            return real(key, *args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "hgetall", spy)
        NoTrackItem.query.get(redis_key=saved.db_key.redis_key, _no_track=True)
        assert calls == [saved.db_key.redis_key]


class TestAsyncGet:
    @pytest.mark.asyncio
    async def test_no_track_direct_key_stages_nothing(self, saved):
        loaded = await NoTrackItem.query.async_get(
            redis_key=saved.db_key.redis_key, _no_track=True
        )
        assert loaded is not None
        assert loaded.name == "alpha"
        assert not POPOTO_REDIS_DB.exists(_staged_key(saved))
        assert not POPOTO_REDIS_DB.exists(_access_log_key(saved))

    @pytest.mark.asyncio
    async def test_default_direct_key_still_stages_one_read(self, saved):
        loaded = await NoTrackItem.query.async_get(redis_key=saved.db_key.redis_key)
        assert loaded is not None
        assert _staged_len(saved) == 1

    @pytest.mark.asyncio
    async def test_missing_key_returns_none_and_stages_nothing(self):
        ghost = NoTrackItem(name="ghost")
        assert (
            await NoTrackItem.query.async_get(
                redis_key=ghost.db_key.redis_key, _no_track=True
            )
            is None
        )
        assert not POPOTO_REDIS_DB.exists(_staged_key(ghost))

    @pytest.mark.asyncio
    async def test_no_track_on_filter_fallback(self, saved):
        loaded = await NoTrackItem.query.async_get(tag="t1", _no_track=True)
        assert loaded is not None
        assert not POPOTO_REDIS_DB.exists(_staged_key(saved))
        assert await NoTrackItem.query.async_get(tag="t1") is not None
        assert _staged_len(saved) == 1
