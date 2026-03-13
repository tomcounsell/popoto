"""Tests for ListField(max_length=N) with push() method.

Tests capped list storage in a separate Redis list key, push() for
atomic LPUSH + LTRIM, and backward compatibility with uncapped ListField.
"""

import sys
import os

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402


class CappedListModel(popoto.Model):
    name = popoto.KeyField()
    events = popoto.ListField(max_length=5)


class UncappedListModel(popoto.Model):
    name = popoto.KeyField()
    items = popoto.ListField(default=[])


@pytest.fixture(autouse=True)
def cleanup():
    """Remove all test model instances before and after each test."""

    def _do_cleanup():
        for item in CappedListModel.query.all():
            item.delete()
        for item in UncappedListModel.query.all():
            item.delete()

    _do_cleanup()
    yield
    _do_cleanup()


def test_capped_list_save_and_load():
    """Save a model with a capped list, reload, and verify data round-trips."""
    m = CappedListModel(name="test1", events=[1, 2, 3])
    m.save()

    loaded = CappedListModel.query.get(name="test1")
    assert loaded.events == [1, 2, 3], f"Expected [1, 2, 3], got {loaded.events}"


def test_capped_list_push():
    """push() appends to the list via LPUSH + LTRIM without full read/write."""
    m = CappedListModel(name="test_push", events=[])
    m.save()

    m.events.push("a")
    m.events.push("b")
    m.events.push("c")

    # Reload from Redis to verify
    loaded = CappedListModel.query.get(name="test_push")
    # LPUSH prepends, so newest first
    assert loaded.events == ["c", "b", "a"], (
        f"Expected ['c', 'b', 'a'], got {loaded.events}"
    )


def test_capped_list_push_caps_at_max_length():
    """push() should cap the list at max_length items."""
    m = CappedListModel(name="test_cap", events=[])
    m.save()

    for i in range(10):
        m.events.push(i)

    loaded = CappedListModel.query.get(name="test_cap")
    assert len(loaded.events) == 5, f"Expected 5 items, got {len(loaded.events)}"
    # Newest first (LPUSH), so items 9, 8, 7, 6, 5
    assert loaded.events == [9, 8, 7, 6, 5], (
        f"Expected [9, 8, 7, 6, 5], got {loaded.events}"
    )


def test_capped_list_delete_cleans_redis_key():
    """Deleting a model with capped list should clean up the Redis list key."""
    m = CappedListModel(name="test_del", events=[1, 2, 3])
    m.save()

    # Verify the Redis list key exists
    list_key = f"{m._redis_key}::events"
    assert POPOTO_REDIS_DB.exists(list_key), f"Expected list key {list_key} to exist"

    m.delete()

    # Verify the Redis list key is gone
    assert not POPOTO_REDIS_DB.exists(list_key), (
        f"Expected list key {list_key} to be deleted"
    )


def test_capped_list_empty_returns_empty_list():
    """A capped list with no data should return empty list, not None."""
    m = CappedListModel(name="test_empty", events=[])
    m.save()

    loaded = CappedListModel.query.get(name="test_empty")
    assert loaded.events == [], f"Expected [], got {loaded.events}"


def test_push_on_unsaved_model_raises():
    """push() on a model without a redis key should raise ModelException."""

    class NoKeyModel(popoto.Model):
        events = popoto.ListField(max_length=5)

    m = NoKeyModel(events=[])
    # _auto_key is generated but _redis_key may be set; force it to None
    m._redis_key = None
    with pytest.raises(popoto.ModelException):
        m.events.push("value")

    for item in NoKeyModel.query.all():
        item.delete()


def test_push_complex_types():
    """push() should handle tuples, dicts, and Decimals correctly."""
    m = CappedListModel(name="test_complex", events=[])
    m.save()

    m.events.push({"key": "value"})
    m.events.push((1, 2, 3))
    m.events.push(42)

    loaded = CappedListModel.query.get(name="test_complex")
    assert len(loaded.events) == 3
    # Newest first
    assert loaded.events[0] == 42
    assert loaded.events[1] == (1, 2, 3)
    assert loaded.events[2] == {"key": "value"}


def test_uncapped_list_backward_compatibility():
    """ListField without max_length should work exactly as before."""
    m = UncappedListModel(name="test_compat", items=[1, 2, 3])
    m.save()

    loaded = UncappedListModel.query.get(name="test_compat")
    assert loaded.items == [1, 2, 3], f"Expected [1, 2, 3], got {loaded.items}"


def test_capped_list_save_replaces_list():
    """save() on a model with capped list should replace the entire Redis list."""
    m = CappedListModel(name="test_replace", events=[1, 2, 3])
    m.save()

    # Push some items
    m.events.push(99)

    # Now save with a new list value
    m.events = [10, 20, 30]
    m.save()

    loaded = CappedListModel.query.get(name="test_replace")
    assert loaded.events == [10, 20, 30], f"Expected [10, 20, 30], got {loaded.events}"


def test_push_none_value():
    """push(None) should store None as a valid list element."""
    m = CappedListModel(name="test_none", events=[])
    m.save()

    m.events.push(None)

    loaded = CappedListModel.query.get(name="test_none")
    assert loaded.events == [None], f"Expected [None], got {loaded.events}"


def test_capped_list_not_in_hash():
    """Capped list data should not be stored in the model hash."""
    m = CappedListModel(name="test_hash", events=[1, 2, 3])
    m.save()

    # Check that the 'events' field is NOT in the hash
    hash_data = POPOTO_REDIS_DB.hgetall(m._redis_key)
    assert b"events" not in hash_data, (
        "Capped list data should not be in the model hash"
    )
