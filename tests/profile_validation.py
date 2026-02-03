"""
Performance audit of validation logic in save() hot path.
Run: python tests/profile_validation.py
"""

import sys

sys.path.insert(0, "src")

import time
import statistics
import popoto
from popoto.redis_db import POPOTO_REDIS_DB

# --- Models ---


class SimpleModel(popoto.Model):
    """Minimal model - baseline."""

    key = popoto.KeyField()


class UniqueModel(popoto.Model):
    """Model with unique field - tests SMEMBERS overhead."""

    id = popoto.AutoKeyField()
    email = popoto.UniqueKeyField()


class ManyFieldsModel(popoto.Model):
    """Model with many fields - tests per-field validation cost."""

    key = popoto.KeyField()
    f1 = popoto.Field(type=str)
    f2 = popoto.Field(type=str)
    f3 = popoto.Field(type=str)
    f4 = popoto.Field(type=str)
    f5 = popoto.Field(type=str)
    f6 = popoto.IntField()
    f7 = popoto.IntField()
    f8 = popoto.IntField()
    f9 = popoto.BooleanField()
    f10 = popoto.BooleanField()


class IndexedModel(popoto.Model):
    """Model with unique_together index."""

    id = popoto.AutoKeyField()
    category = popoto.KeyField()
    code = popoto.Field(type=str)

    class Meta:
        unique_together = [("category", "code")]


# --- Profiling helpers ---


def time_operation(func, iterations, label):
    """Time an operation over N iterations, return stats."""
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        func(i)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)  # ms

    avg = statistics.mean(times)
    p50 = statistics.median(times)
    p99 = sorted(times)[int(len(times) * 0.99)]
    total = sum(times)

    print(f"  {label}:")
    print(f"    total={total:.1f}ms  avg={avg:.3f}ms  p50={p50:.3f}ms  p99={p99:.3f}ms")
    return {"avg": avg, "p50": p50, "p99": p99, "total": total}


def profile_phase(func, iterations, label):
    """Profile a specific phase of save()."""
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        func(i)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)
    avg = statistics.mean(times)
    print(f"    {label}: avg={avg:.3f}ms")
    return avg


# --- Tests ---

N = 1000


def test_baseline_save():
    """Measure save() with simplest possible model."""
    print(f"\n{'='*60}")
    print(f"1. BASELINE: SimpleModel save ({N} iterations)")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    time_operation(lambda i: SimpleModel(key=f"k{i}").save(), N, "SimpleModel.save()")


def test_unique_field_save():
    """Measure save() overhead from unique field SMEMBERS check."""
    print(f"\n{'='*60}")
    print(f"2. UNIQUE FIELD: UniqueModel save ({N} iterations)")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    time_operation(
        lambda i: UniqueModel(email=f"user{i}@test.com").save(), N, "UniqueModel.save()"
    )


def test_many_fields_save():
    """Measure per-field validation cost."""
    print(f"\n{'='*60}")
    print(f"3. MANY FIELDS: ManyFieldsModel save ({N} iterations)")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    time_operation(
        lambda i: ManyFieldsModel(
            key=f"k{i}",
            f1="a",
            f2="b",
            f3="c",
            f4="d",
            f5="e",
            f6=1,
            f7=2,
            f8=3,
            f9=True,
            f10=False,
        ).save(),
        N,
        "ManyFieldsModel.save()",
    )


def test_indexed_save():
    """Measure unique_together index check overhead."""
    print(f"\n{'='*60}")
    print(f"4. INDEXED: IndexedModel save ({N} iterations)")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    time_operation(
        lambda i: IndexedModel(category=f"cat{i % 10}", code=f"code{i}").save(),
        N,
        "IndexedModel.save()",
    )


def test_unique_field_scaling():
    """Measure if SMEMBERS cost grows as unique index SET grows."""
    print(f"\n{'='*60}")
    print(f"5. SCALING: UniqueModel SMEMBERS cost as data grows")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    # Pre-populate with increasing amounts of data, then measure save time
    for batch_label, pre_count in [
        ("0 existing", 0),
        ("1K existing", 1000),
        ("10K existing", 10000),
    ]:
        POPOTO_REDIS_DB.flushdb()
        # Pre-populate
        for i in range(pre_count):
            UniqueModel(email=f"pre{i}@test.com").save()

        # Now measure 100 saves
        times = []
        for i in range(100):
            start = time.perf_counter()
            UniqueModel(email=f"new{i}@test.com").save()
            elapsed = time.perf_counter() - start
            times.append(elapsed * 1000)

        avg = statistics.mean(times)
        p99 = sorted(times)[int(len(times) * 0.99)]
        print(f"  {batch_label}: avg={avg:.3f}ms  p99={p99:.3f}ms")


def test_is_valid_isolation():
    """Measure is_valid() alone vs full save()."""
    print(f"\n{'='*60}")
    print(f"6. ISOLATION: is_valid() vs pre_save() vs save()")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    instances = [
        ManyFieldsModel(
            key=f"k{i}",
            f1="a",
            f2="b",
            f3="c",
            f4="d",
            f5="e",
            f6=1,
            f7=2,
            f8=3,
            f9=True,
            f10=False,
        )
        for i in range(N)
    ]

    # is_valid only
    profile_phase(lambda i: instances[i].is_valid(), N, "is_valid() alone")

    # pre_save only
    profile_phase(lambda i: instances[i].pre_save(), N, "pre_save() alone")

    # full save
    profile_phase(lambda i: instances[i].save(), N, "save() full")


def test_smembers_vs_sismember():
    """Compare SMEMBERS (current) vs SISMEMBER (proposed) for unique check."""
    print(f"\n{'='*60}")
    print(f"7. OPTIMIZATION: SMEMBERS vs SISMEMBER lookup")
    print(f"{'='*60}")
    POPOTO_REDIS_DB.flushdb()

    # Create a SET with 1000 members (simulating a popular unique field value index)
    set_key = "test:unique:benchmark"
    for i in range(1000):
        POPOTO_REDIS_DB.sadd(set_key, f"member_{i}")

    # Measure SMEMBERS
    times_smembers = []
    for _ in range(1000):
        start = time.perf_counter()
        result = POPOTO_REDIS_DB.smembers(set_key)
        elapsed = time.perf_counter() - start
        times_smembers.append(elapsed * 1000)

    # Measure SCARD (just count)
    times_scard = []
    for _ in range(1000):
        start = time.perf_counter()
        result = POPOTO_REDIS_DB.scard(set_key)
        elapsed = time.perf_counter() - start
        times_scard.append(elapsed * 1000)

    # Measure SISMEMBER (check single member)
    times_sismember = []
    for _ in range(1000):
        start = time.perf_counter()
        result = POPOTO_REDIS_DB.sismember(set_key, "member_500")
        elapsed = time.perf_counter() - start
        times_sismember.append(elapsed * 1000)

    print(f"  SET with 1000 members:")
    print(
        f"    SMEMBERS:  avg={statistics.mean(times_smembers):.3f}ms  (returns all members)"
    )
    print(
        f"    SCARD:     avg={statistics.mean(times_scard):.3f}ms  (returns count only)"
    )
    print(
        f"    SISMEMBER: avg={statistics.mean(times_sismember):.3f}ms  (checks one member)"
    )


def test_double_validation_overhead():
    """Measure cost of duplicate validation in is_valid() loop."""
    print(f"\n{'='*60}")
    print(f"8. DUPLICATE WORK: is_valid() iterates fields twice")
    print(f"{'='*60}")

    # Count how many times field attributes are accessed per save
    access_count = {"getattr": 0}
    original_getattr = ManyFieldsModel.__getattribute__

    def counting_getattr(self, name):
        if not name.startswith("_") and name in (
            "f1",
            "f2",
            "f3",
            "f4",
            "f5",
            "f6",
            "f7",
            "f8",
            "f9",
            "f10",
            "key",
        ):
            access_count["getattr"] += 1
        return original_getattr(self, name)

    ManyFieldsModel.__getattribute__ = counting_getattr

    instance = ManyFieldsModel(
        key="test",
        f1="a",
        f2="b",
        f3="c",
        f4="d",
        f5="e",
        f6=1,
        f7=2,
        f8=3,
        f9=True,
        f10=False,
    )

    access_count["getattr"] = 0
    instance.is_valid()
    print(f"  Field attribute accesses in is_valid(): {access_count['getattr']}")
    print(f"  Fields on model: 11")
    print(f"  Accesses per field: {access_count['getattr'] / 11:.1f}")

    ManyFieldsModel.__getattribute__ = original_getattr


if __name__ == "__main__":
    print("POPOTO VALIDATION PERFORMANCE AUDIT")
    print(f"Testing with N={N} iterations per test")

    test_baseline_save()
    test_unique_field_save()
    test_many_fields_save()
    test_indexed_save()
    test_unique_field_scaling()
    test_is_valid_isolation()
    test_smembers_vs_sismember()
    test_double_validation_overhead()

    print(f"\n{'='*60}")
    print("AUDIT COMPLETE")
    print(f"{'='*60}")
