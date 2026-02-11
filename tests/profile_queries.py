"""
Performance audit of query and filter operations.
Run: python tests/profile_queries.py
"""

import sys

sys.path.insert(0, "src")

import time
import statistics
import random
import popoto
from popoto.redis_db import POPOTO_REDIS_DB

# --- Models ---


class SimpleItem(popoto.Model):
    key = popoto.KeyField()
    value = popoto.Field(type=str)


class MultiKeyItem(popoto.Model):
    category = popoto.KeyField()
    subcategory = popoto.KeyField()
    value = popoto.Field(type=str)


class SortedItem(popoto.Model):
    key = popoto.KeyField()
    score = popoto.SortedField(type=int)


class GeoItem(popoto.Model):
    key = popoto.KeyField()
    location = popoto.GeoField()


# --- Helpers ---


def time_op(func, iterations, label):
    times = []
    for i in range(iterations):
        start = time.perf_counter()
        func(i)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)
    avg = statistics.mean(times)
    p50 = statistics.median(times)
    p99 = sorted(times)[int(len(times) * 0.99)]
    total = sum(times)
    print(f"  {label}:")
    print(f"    total={total:.1f}ms  avg={avg:.3f}ms  p50={p50:.3f}ms  p99={p99:.3f}ms")
    return {"avg": avg, "p50": p50, "p99": p99, "total": total}


def populate_simple(n):
    """Populate SimpleItem with n records."""
    POPOTO_REDIS_DB.flushdb()
    pipeline = POPOTO_REDIS_DB.pipeline()
    for i in range(n):
        item = SimpleItem(key=f"item{i:06d}", value=f"value{i}")
        item.save(pipeline=pipeline)
    pipeline.execute()
    return n


def populate_multikey(n):
    """Populate MultiKeyItem with n records across categories."""
    POPOTO_REDIS_DB.flushdb()
    categories = [f"cat{i}" for i in range(10)]
    subcategories = [f"sub{i}" for i in range(10)]
    for i in range(n):
        cat = categories[i % 10]
        sub = subcategories[(i // 10) % 10]
        MultiKeyItem(category=cat, subcategory=sub, value=f"val{i}").save()
    return n


def populate_sorted(n):
    """Populate SortedItem with n records with random scores."""
    POPOTO_REDIS_DB.flushdb()
    for i in range(n):
        SortedItem(key=f"item{i:06d}", score=random.randint(0, 100000)).save()
    return n


def populate_geo(n):
    """Populate GeoItem with n records spread geographically."""
    POPOTO_REDIS_DB.flushdb()
    for i in range(n):
        lat = 40.0 + (i % 100) * 0.01
        lon = -74.0 + (i // 100) * 0.01
        GeoItem(key=f"loc{i:06d}", location=(lat, lon)).save()
    return n


# --- Tests ---


def test_all_scaling():
    """Measure query.all() at different data sizes."""
    print(f"\n{'='*60}")
    print("1. SCALING: query.all() at various data sizes")
    print(f"{'='*60}")

    for n in [100, 1000, 5000]:
        populate_simple(n)
        times = []
        for _ in range(10):
            start = time.perf_counter()
            results = list(SimpleItem.query.all())
            elapsed = time.perf_counter() - start
            times.append(elapsed * 1000)
        avg = statistics.mean(times)
        p99 = sorted(times)[min(9, len(times) - 1)]
        print(
            f"  {n} items: avg={avg:.1f}ms  p99={p99:.1f}ms  ({n/avg*1000:.0f} items/sec)"
        )


def test_get_vs_filter():
    """Compare query.get() vs query.filter() for single item."""
    print(f"\n{'='*60}")
    print("2. GET vs FILTER: single item retrieval")
    print(f"{'='*60}")

    populate_simple(1000)

    # get() with known key
    time_op(
        lambda i: SimpleItem.query.get(key=f"item{i:06d}"), 100, "query.get(key=...)"
    )

    # filter() with exact match
    time_op(
        lambda i: SimpleItem.query.filter(key=f"item{i:06d}"),
        100,
        "query.filter(key=...)",
    )


def test_filter_exact_vs_pattern():
    """Compare exact match (SMEMBERS) vs pattern match (KEYS)."""
    print(f"\n{'='*60}")
    print("3. EXACT vs PATTERN: SMEMBERS vs KEYS command")
    print(f"{'='*60}")

    populate_simple(1000)

    # Exact match - uses SMEMBERS
    time_op(
        lambda i: SimpleItem.query.filter(key=f"item{i:06d}"),
        100,
        "filter(key=...) [SMEMBERS]",
    )

    # Startswith - uses KEYS
    time_op(
        lambda i: SimpleItem.query.filter(key__startswith=f"item{i:03d}"),
        100,
        "filter(key__startswith=...) [KEYS]",
    )

    # Endswith - uses KEYS
    time_op(
        lambda i: SimpleItem.query.filter(key__endswith=f"{i:03d}"),
        100,
        "filter(key__endswith=...) [KEYS]",
    )


def test_keys_scaling():
    """Measure KEYS command performance at different data sizes."""
    print(f"\n{'='*60}")
    print("4. KEYS SCALING: pattern match at different data sizes")
    print(f"{'='*60}")

    for n in [100, 1000, 5000]:
        populate_simple(n)
        times = []
        for _ in range(50):
            start = time.perf_counter()
            SimpleItem.query.filter(key__startswith="item000")
            elapsed = time.perf_counter() - start
            times.append(elapsed * 1000)
        avg = statistics.mean(times)
        p99 = sorted(times)[int(len(times) * 0.99)]
        print(f"  {n} items: avg={avg:.3f}ms  p99={p99:.3f}ms")


def test_filter_intersection():
    """Measure multi-field filter intersection performance."""
    print(f"\n{'='*60}")
    print("5. INTERSECTION: multi-field filter")
    print(f"{'='*60}")

    populate_multikey(1000)

    # Single field filter
    time_op(
        lambda i: MultiKeyItem.query.filter(category=f"cat{i % 10}"),
        100,
        "filter(category=...) [single field]",
    )

    # Two field filter (intersection)
    time_op(
        lambda i: MultiKeyItem.query.filter(
            category=f"cat{i % 10}", subcategory=f"sub{i % 10}"
        ),
        100,
        "filter(category=..., subcategory=...) [intersection]",
    )


def test_filter_in():
    """Measure __in filter with varying list sizes."""
    print(f"\n{'='*60}")
    print("6. __in FILTER: varying list sizes")
    print(f"{'='*60}")

    populate_simple(1000)

    for in_size in [5, 50, 200]:
        keys = [f"item{random.randint(0, 999):06d}" for _ in range(in_size)]
        times = []
        for _ in range(50):
            start = time.perf_counter()
            SimpleItem.query.filter(key__in=keys)
            elapsed = time.perf_counter() - start
            times.append(elapsed * 1000)
        avg = statistics.mean(times)
        print(f"  __in with {in_size} values: avg={avg:.3f}ms")


def test_sorted_field_queries():
    """Measure sorted field range queries."""
    print(f"\n{'='*60}")
    print("7. SORTED FIELD: range query performance")
    print(f"{'='*60}")

    populate_sorted(1000)

    # Range query returning ~10% of data
    time_op(
        lambda i: SortedItem.query.filter(score__gte=0, score__lte=10000),
        50,
        "filter(score__gte=0, score__lte=10000) [~10% of 1000]",
    )

    # Range query returning ~50% of data
    time_op(
        lambda i: SortedItem.query.filter(score__gte=0, score__lte=50000),
        50,
        "filter(score__gte=0, score__lte=50000) [~50% of 1000]",
    )

    # Narrow range
    time_op(
        lambda i: SortedItem.query.filter(score__gte=5000, score__lte=5100),
        50,
        "filter(score 5000-5100) [~1% of 1000]",
    )


def test_geo_queries():
    """Measure geo radius query performance."""
    print(f"\n{'='*60}")
    print("8. GEO QUERIES: radius search performance")
    print(f"{'='*60}")

    populate_geo(1000)

    # Small radius
    time_op(
        lambda i: GeoItem.query.filter(
            location_latitude=40.5,
            location_longitude=-73.95,
            location_radius=5,
            location_radius_unit="km",
        ),
        50,
        "geo radius 5km",
    )

    # Large radius
    time_op(
        lambda i: GeoItem.query.filter(
            location_latitude=40.5,
            location_longitude=-73.95,
            location_radius=50,
            location_radius_unit="km",
        ),
        50,
        "geo radius 50km",
    )


def test_count_vs_all():
    """Compare count() vs len(all()) efficiency."""
    print(f"\n{'='*60}")
    print("9. COUNT: query.count() vs len(query.all())")
    print(f"{'='*60}")

    populate_simple(1000)

    time_op(lambda i: SimpleItem.query.count(), 100, "query.count() [SCARD]")

    time_op(lambda i: len(SimpleItem.query.all()), 10, "len(query.all()) [full fetch]")


def test_order_by_overhead():
    """Measure order_by overhead on query results."""
    print(f"\n{'='*60}")
    print("10. ORDER BY: overhead on query results")
    print(f"{'='*60}")

    populate_simple(1000)

    time_op(lambda i: SimpleItem.query.all(), 10, "query.all() [no ordering]")

    time_op(
        lambda i: SimpleItem.query.all(order_by="key"),
        10,
        "query.all(order_by='key') [Python sort]",
    )


def test_values_optimization():
    """Measure values() projection vs full object fetch."""
    print(f"\n{'='*60}")
    print("11. VALUES: projection vs full object fetch")
    print(f"{'='*60}")

    populate_simple(1000)

    time_op(lambda i: SimpleItem.query.all(), 10, "query.all() [full objects]")

    time_op(
        lambda i: SimpleItem.query.all(values=("key",)),
        10,
        "query.all(values=('key',)) [key-only, no Redis fetch]",
    )

    time_op(
        lambda i: SimpleItem.query.all(values=("key", "value")),
        10,
        "query.all(values=('key','value')) [hmget]",
    )


def test_keys_vs_scan_raw():
    """Compare raw KEYS vs SCAN Redis commands."""
    print(f"\n{'='*60}")
    print("12. RAW REDIS: KEYS vs SCAN comparison")
    print(f"{'='*60}")

    populate_simple(5000)
    pattern = "SimpleItem:item000*"

    # KEYS
    times_keys = []
    for _ in range(100):
        start = time.perf_counter()
        result = POPOTO_REDIS_DB.keys(pattern)
        elapsed = time.perf_counter() - start
        times_keys.append(elapsed * 1000)
    keys_count = len(result)

    # SCAN
    times_scan = []
    for _ in range(100):
        start = time.perf_counter()
        result = []
        cursor = 0
        while True:
            cursor, keys = POPOTO_REDIS_DB.scan(
                cursor=cursor, match=pattern, count=1000
            )
            result.extend(keys)
            if cursor == 0:
                break
        elapsed = time.perf_counter() - start
        times_scan.append(elapsed * 1000)
    scan_count = len(result)

    print(f"  Pattern: {pattern}")
    print(f"  Matches: KEYS={keys_count}, SCAN={scan_count}")
    print(f"  KEYS: avg={statistics.mean(times_keys):.3f}ms  (BLOCKS Redis)")
    print(f"  SCAN: avg={statistics.mean(times_scan):.3f}ms  (non-blocking)")
    print("  Note: KEYS blocks the entire Redis server during execution.")
    print("  At 100K+ keys, KEYS can cause seconds of blocking.")


def test_deserialization_overhead():
    """Measure msgpack decode time vs Redis fetch time."""
    print(f"\n{'='*60}")
    print("13. DESERIALIZATION: decode overhead vs Redis I/O")
    print(f"{'='*60}")

    populate_simple(1000)

    # Fetch raw hashes (no decode)
    keys = SimpleItem.query.keys()
    pipeline = POPOTO_REDIS_DB.pipeline()
    for k in keys:
        pipeline.hgetall(k)

    times_fetch = []
    for _ in range(10):
        start = time.perf_counter()
        pipeline = POPOTO_REDIS_DB.pipeline()
        for k in keys:
            pipeline.hgetall(k)
        raw = pipeline.execute()
        elapsed = time.perf_counter() - start
        times_fetch.append(elapsed * 1000)

    # Decode
    from popoto.models.encoding import decode_popoto_model_hashmap

    times_decode = []
    for _ in range(10):
        start = time.perf_counter()
        for h in raw:
            if h:
                decode_popoto_model_hashmap(SimpleItem, h)
        elapsed = time.perf_counter() - start
        times_decode.append(elapsed * 1000)

    print("  1000 items:")
    print(f"    Pipeline HGETALL: avg={statistics.mean(times_fetch):.1f}ms")
    print(f"    msgpack decode:   avg={statistics.mean(times_decode):.1f}ms")
    print(
        f"    decode share:     {statistics.mean(times_decode)/(statistics.mean(times_fetch)+statistics.mean(times_decode))*100:.0f}%"
    )


if __name__ == "__main__":
    print("POPOTO QUERY PERFORMANCE AUDIT")
    print("=" * 60)

    test_all_scaling()
    test_get_vs_filter()
    test_filter_exact_vs_pattern()
    test_keys_scaling()
    test_filter_intersection()
    test_filter_in()
    test_sorted_field_queries()
    test_geo_queries()
    test_count_vs_all()
    test_order_by_overhead()
    test_values_optimization()
    test_keys_vs_scan_raw()
    test_deserialization_overhead()

    print(f"\n{'='*60}")
    print("AUDIT COMPLETE")
    print(f"{'='*60}")
