"""Tests for ListField(max_length=N) with push() method.

Tests capped list storage in a separate Redis list key, push() for
atomic LPUSH + LTRIM, and backward compatibility with uncapped ListField.
"""

import sys
import os

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


def _cleanup():
    """Remove all test model instances."""
    for item in CappedListModel.query.all():
        item.delete()
    for item in UncappedListModel.query.all():
        item.delete()


def test_capped_list_save_and_load():
    """Save a model with a capped list, reload, and verify data round-trips."""
    _cleanup()
    m = CappedListModel(name="test1", events=[1, 2, 3])
    m.save()

    loaded = CappedListModel.query.get(name="test1")
    assert loaded.events == [1, 2, 3], f"Expected [1, 2, 3], got {loaded.events}"
    _cleanup()


def test_capped_list_push():
    """push() appends to the list via LPUSH + LTRIM without full read/write."""
    _cleanup()
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
    _cleanup()


def test_capped_list_push_caps_at_max_length():
    """push() should cap the list at max_length items."""
    _cleanup()
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
    _cleanup()


def test_capped_list_delete_cleans_redis_key():
    """Deleting a model with capped list should clean up the Redis list key."""
    _cleanup()
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
    _cleanup()


def test_capped_list_empty_returns_empty_list():
    """A capped list with no data should return empty list, not None."""
    _cleanup()
    m = CappedListModel(name="test_empty", events=[])
    m.save()

    loaded = CappedListModel.query.get(name="test_empty")
    assert loaded.events == [], f"Expected [], got {loaded.events}"
    _cleanup()


def test_push_on_unsaved_model_raises():
    """push() on a model without a redis key should raise ModelException."""
    _cleanup()

    class NoKeyModel(popoto.Model):
        events = popoto.ListField(max_length=5)

    m = NoKeyModel(events=[])
    # _auto_key is generated but _redis_key may be set; force it to None
    m._redis_key = None
    try:
        m.events.push("value")
        assert False, "Expected ModelException"
    except popoto.ModelException:
        pass

    for item in NoKeyModel.query.all():
        item.delete()
    _cleanup()


def test_push_complex_types():
    """push() should handle tuples, dicts, and Decimals correctly."""
    _cleanup()
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
    _cleanup()


def test_uncapped_list_backward_compatibility():
    """ListField without max_length should work exactly as before."""
    _cleanup()
    m = UncappedListModel(name="test_compat", items=[1, 2, 3])
    m.save()

    loaded = UncappedListModel.query.get(name="test_compat")
    assert loaded.items == [1, 2, 3], f"Expected [1, 2, 3], got {loaded.items}"
    _cleanup()


def test_capped_list_save_replaces_list():
    """save() on a model with capped list should replace the entire Redis list."""
    _cleanup()
    m = CappedListModel(name="test_replace", events=[1, 2, 3])
    m.save()

    # Push some items
    m.events.push(99)

    # Now save with a new list value
    m.events = [10, 20, 30]
    m.save()

    loaded = CappedListModel.query.get(name="test_replace")
    assert loaded.events == [10, 20, 30], f"Expected [10, 20, 30], got {loaded.events}"
    _cleanup()


def test_push_none_value():
    """push(None) should store None as a valid list element."""
    _cleanup()
    m = CappedListModel(name="test_none", events=[])
    m.save()

    m.events.push(None)

    loaded = CappedListModel.query.get(name="test_none")
    assert loaded.events == [None], f"Expected [None], got {loaded.events}"
    _cleanup()


def test_capped_list_not_in_hash():
    """Capped list data should not be stored in the model hash."""
    _cleanup()
    m = CappedListModel(name="test_hash", events=[1, 2, 3])
    m.save()

    # Check that the 'events' field is NOT in the hash
    hash_data = POPOTO_REDIS_DB.hgetall(m._redis_key)
    assert b"events" not in hash_data, (
        "Capped list data should not be in the model hash"
    )
    _cleanup()


if __name__ == "__main__":
    test_capped_list_save_and_load()
    test_capped_list_push()
    test_capped_list_push_caps_at_max_length()
    test_capped_list_delete_cleans_redis_key()
    test_capped_list_empty_returns_empty_list()
    test_push_on_unsaved_model_raises()
    test_push_complex_types()
    test_uncapped_list_backward_compatibility()
    test_capped_list_save_replaces_list()
    test_push_none_value()
    test_capped_list_not_in_hash()
    print("All tests passed!")
