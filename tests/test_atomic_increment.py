"""Tests for Model.atomic_increment() method.

Tests cover:
- Integer field increment/decrement
- Float field increment/decrement
- Decimal field increment/decrement
- SortedField index update after increment
- In-memory value update after increment
- Error cases: unsaved model, non-numeric field, nonexistent field
- Edge cases: zero delta, negative delta, None delta
- Pipeline support
- Concurrent increments don't lose updates
"""

import sys
import os
from decimal import Decimal
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


class Counter(popoto.Model):
    name = popoto.KeyField(null=False)
    count = popoto.IntField(default=0)
    score = popoto.FloatField(default=0.0)
    balance = popoto.DecimalField(default=Decimal("0"))
    label = popoto.StringField(default="")
    sorted_count = popoto.SortedField(type=int, default=0)


# --- Setup / Teardown ---


def setup_module():
    """Clean up test data before running tests."""
    Counter.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    Counter.delete_all()


# --- Integer increment tests ---


def test_increment_int_field():
    """atomic_increment on IntField returns new value and updates in-memory."""
    c = Counter.create(name="int_test", count=10)
    new_val = c.atomic_increment("count", 5)
    assert new_val == 15
    assert c.count == 15
    # Verify persisted in Redis
    reloaded = Counter.query.get(name="int_test")
    assert reloaded.count == 15
    c.delete()


def test_decrement_int_field():
    """Negative delta decrements the field."""
    c = Counter.create(name="dec_test", count=10)
    new_val = c.atomic_increment("count", -3)
    assert new_val == 7
    assert c.count == 7
    c.delete()


def test_zero_delta():
    """Delta of 0 returns current value without changing it."""
    c = Counter.create(name="zero_test", count=42)
    new_val = c.atomic_increment("count", 0)
    assert new_val == 42
    assert c.count == 42
    c.delete()


# --- Float increment tests ---


def test_increment_float_field():
    """atomic_increment on FloatField works with float delta."""
    c = Counter.create(name="float_test", score=1.5)
    new_val = c.atomic_increment("score", 2.5)
    assert abs(new_val - 4.0) < 1e-9
    assert abs(c.score - 4.0) < 1e-9
    c.delete()


# --- Decimal increment tests ---


def test_increment_decimal_field():
    """atomic_increment on DecimalField works and returns Decimal."""
    c = Counter.create(name="decimal_test", balance=Decimal("10.50"))
    new_val = c.atomic_increment("balance", Decimal("1.25"))
    assert isinstance(new_val, Decimal)
    assert new_val == Decimal("11.75")
    assert c.balance == Decimal("11.75")
    c.delete()


# --- SortedField index update ---


def test_sorted_field_index_updated():
    """atomic_increment updates the SortedField sorted set score."""
    c = Counter.create(name="sorted_test", sorted_count=10)
    c.atomic_increment("sorted_count", 5)
    # Query using the sorted index to verify the score was updated
    results = Counter.query.filter(sorted_count__gte=14, sorted_count__lte=16)
    found = any(r.name == "sorted_test" for r in results)
    assert found, "Instance should be findable by new sorted value"
    c.delete()


# --- Error cases ---


def test_unsaved_model_raises_type_error():
    """atomic_increment on unsaved model raises TypeError."""
    c = Counter(name="unsaved_test", count=0)
    try:
        c.atomic_increment("count", 1)
        assert False, "Should have raised TypeError"
    except TypeError:
        pass


def test_non_numeric_field_raises_type_error():
    """atomic_increment on a StringField raises TypeError."""
    c = Counter.create(name="non_numeric_test", label="hello")
    try:
        c.atomic_increment("label", 1)
        assert False, "Should have raised TypeError"
    except TypeError:
        pass
    finally:
        c.delete()


def test_nonexistent_field_raises_attribute_error():
    """atomic_increment on a field that doesn't exist raises AttributeError."""
    c = Counter.create(name="nofield_test")
    try:
        c.atomic_increment("nonexistent_field", 1)
        assert False, "Should have raised AttributeError"
    except AttributeError:
        pass
    finally:
        c.delete()


def test_none_delta_raises_type_error():
    """atomic_increment with None delta raises TypeError."""
    c = Counter.create(name="none_delta_test", count=5)
    try:
        c.atomic_increment("count", None)
        assert False, "Should have raised TypeError"
    except TypeError:
        pass
    finally:
        c.delete()


# --- Pipeline support ---


def test_pipeline_support():
    """atomic_increment works within a pipeline."""
    c = Counter.create(name="pipeline_test", count=0)
    pipe = popoto.POPOTO_REDIS_DB.pipeline()
    c.atomic_increment("count", 10, pipeline=pipe)
    pipe.execute()
    # After pipeline execution, reload to verify
    reloaded = Counter.query.get(name="pipeline_test")
    assert reloaded.count == 10
    c.delete()


# --- Concurrent increments ---


def test_concurrent_increments_no_lost_updates():
    """Multiple threads incrementing the same field don't lose updates."""
    c = Counter.create(name="concurrent_test", count=0)
    num_threads = 10
    increments_per_thread = 50

    def increment_worker():
        # Each thread needs its own instance reference pointing to same Redis key
        instance = Counter.query.get(name="concurrent_test")
        for _ in range(increments_per_thread):
            instance.atomic_increment("count", 1)

    threads = [threading.Thread(target=increment_worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reloaded = Counter.query.get(name="concurrent_test")
    expected = num_threads * increments_per_thread
    assert reloaded.count == expected, (
        f"Expected {expected}, got {reloaded.count}. Lost updates detected."
    )
    c.delete()


# --- saved_field_values update ---


def test_saved_field_values_updated():
    """atomic_increment updates _saved_field_values for proper cleanup."""
    c = Counter.create(name="saved_vals_test", count=10)
    c.atomic_increment("count", 5)
    assert c._saved_field_values.get("count") == 15
    c.delete()
