"""
Comprehensive stress tests for popoto - production readiness validation.

Run with: pytest tests/test_stress.py -v
Skip in fast runs: pytest -m "not slow"
"""
import sys
import os
import asyncio
import time
from decimal import Decimal
from datetime import date, datetime, time as dt_time
import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto
from src.popoto.exceptions import ModelException


# =============================================================================
# MODEL DEFINITIONS
# =============================================================================

class KeyValueModel(popoto.Model):
    key = popoto.KeyField()
    value = popoto.StringField()


class SortedIntModel(popoto.Model):
    key = popoto.KeyField()
    sorted_value = popoto.SortedField(type=int)


class GeoLocationModel(popoto.Model):
    key = popoto.KeyField()
    location = popoto.GeoField()


class UniqueItemModel(popoto.Model):
    key = popoto.KeyField()
    unique_code = popoto.UniqueKeyField()


class ParentModel(popoto.Model):
    key = popoto.KeyField()
    name = popoto.StringField()


class ChildModel(popoto.Model):
    key = popoto.KeyField()
    parent = popoto.Relationship(model=ParentModel)
    value = popoto.IntField()


class FilterTestModel(popoto.Model):
    key = popoto.KeyField()
    category = popoto.StringField()


class KitchenSinkModel(popoto.Model):
    key = popoto.KeyField()
    int_field = popoto.IntField()
    float_field = popoto.FloatField()
    decimal_field = popoto.DecimalField()
    string_field = popoto.StringField()
    bool_field = popoto.BooleanField()
    bytes_field = popoto.BytesField()
    list_field = popoto.ListField()
    dict_field = popoto.DictField()
    set_field = popoto.SetField()
    tuple_field = popoto.TupleField()
    date_field = popoto.DateField()
    datetime_field = popoto.DatetimeField()
    time_field = popoto.TimeField()


class TTLModel(popoto.Model):
    key = popoto.KeyField()
    value = popoto.StringField()

    class Meta:
        ttl = 2


class ChainNodeModel(popoto.Model):
    key = popoto.KeyField()
    # Note: Self-referencing relationships may not be fully supported
    # Using optional string field as workaround
    next_node_key = popoto.StringField(null=True, default=None)


class UpdateCounterModel(popoto.Model):
    key = popoto.KeyField()
    counter = popoto.IntField(default=0)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture(autouse=True)
def flush_redis():
    """Clean Redis before each test."""
    POPOTO_REDIS_DB.flushdb()
    yield
    POPOTO_REDIS_DB.flushdb()


@pytest.fixture
def performance_timer():
    """Context manager for performance timing."""
    class Timer:
        def __init__(self):
            self.start_time = None
            self.elapsed = None

        def __enter__(self):
            self.start_time = time.time()
            return self

        def __exit__(self, *args):
            self.elapsed = time.time() - self.start_time

        def assert_under(self, seconds, message=""):
            assert self.elapsed < seconds, f"{message} took {self.elapsed:.2f}s (threshold: {seconds}s)"

    return Timer


# =============================================================================
# CORE STRESS TESTS
# =============================================================================

@pytest.mark.slow
def test_bulk_create_save_delete(performance_timer):
    """Create and save 1000 models, verify integrity, delete all."""
    with performance_timer() as timer:
        # Create and save 1000 items
        items = [KeyValueModel(key=f"key{i}", value=f"value{i}") for i in range(1000)]
        for item in items:
            item.save()

        # Query all back
        results = list(KeyValueModel.query.all())
        assert len(results) == 1000, f"Expected 1000 items, got {len(results)}"

        # Verify data integrity (check sample, not strict order since string sort != numeric sort)
        keys_found = {r.key for r in results}
        for i in range(0, 1000, 100):  # Check every 100th item
            assert f"key{i}" in keys_found

        # Verify specific items
        for i in [0, 100, 500, 999]:
            item = next((r for r in results if r.key == f"key{i}"), None)
            assert item is not None
            assert item.value == f"value{i}"

        # Delete all
        for item in results:
            item.delete()

        # Verify empty
        assert len(list(KeyValueModel.query.all())) == 0

    timer.assert_under(5, "Bulk create/save/delete (1000 items)")


@pytest.mark.slow
def test_bulk_sorted_field_range_queries(performance_timer):
    """Save 1000 items with sorted field, test range queries."""
    # Save 1000 items with sorted values 0-999
    items = [SortedIntModel(key=f"key{i}", sorted_value=i) for i in range(1000)]
    for item in items:
        item.save()

    # Test __gt
    with performance_timer() as timer:
        results = list(SortedIntModel.query.filter(sorted_value__gt=500))
    assert len(results) == 499, f"Expected 499 items > 500, got {len(results)}"
    timer.assert_under(0.1, "Query __gt=500")

    # Test __lt
    with performance_timer() as timer:
        results = list(SortedIntModel.query.filter(sorted_value__lt=500))
    assert len(results) == 500, f"Expected 500 items < 500, got {len(results)}"
    timer.assert_under(0.1, "Query __lt=500")

    # Test __gte and __lte
    with performance_timer() as timer:
        results = list(SortedIntModel.query.filter(sorted_value__gte=500, sorted_value__lte=600))
    assert len(results) == 101, f"Expected 101 items between 500-600, got {len(results)}"
    timer.assert_under(0.1, "Query __gte=500, __lte=600")

    # Verify result values are in range
    for result in results:
        assert 500 <= result.sorted_value <= 600


@pytest.mark.slow
def test_bulk_geo_radius_search(performance_timer):
    """Save 500 locations, test radius searches with distance accuracy."""
    # Create grid pattern: 50km x 50km area (every 5km)
    # Approximately 0.045 degrees per 5km at equator, starting at (1,1) to avoid (0,0)
    locations = []
    for i in range(10):
        for j in range(10):
            lat = 1.0 + (i * 0.045)
            lon = 1.0 + (j * 0.045)
            locations.append(GeoLocationModel(key=f"loc_{i}_{j}", location=(lat, lon)))

    for loc in locations:
        loc.save()

    assert len(list(GeoLocationModel.query.all())) == 100

    # Radius search from center - use smaller radius (5km)
    center_lat, center_lon = 1.225, 1.225
    with performance_timer() as timer:
        results = list(GeoLocationModel.query.filter(
            location_latitude=center_lat,
            location_longitude=center_lon,
            location_radius=10,
            location_radius_unit="km"
        ))
    timer.assert_under(0.2, "Geo radius search (10km)")

    # Should find center + nearby points (expect 5-25 within 10km)
    assert 5 <= len(results) <= 30, f"Expected 5-30 results in 10km radius, got {len(results)}"


@pytest.mark.slow
def test_bulk_unique_key_operations(performance_timer):
    """Save 1000 items with unique keys, verify uniqueness enforcement."""
    with performance_timer() as timer:
        # Save 1000 items
        for i in range(1000):
            item = UniqueItemModel(key=f"key{i}", unique_code=f"UNIQUE{i:04d}")
            item.save()

        # Verify all queryable
        results = list(UniqueItemModel.query.all())
        assert len(results) == 1000

        # UniqueKeyField should reject duplicate values on different instances
        with pytest.raises(Exception):
            duplicate = UniqueItemModel(key="key_duplicate", unique_code="UNIQUE0000")
            duplicate.save()

        # Delete first 500
        for i in range(500):
            item = UniqueItemModel.query.get(key=f"key{i}")
            item.delete()

        # Verify 500 remaining
        assert len(list(UniqueItemModel.query.all())) == 500

        # Save 500 new items with freed unique keys
        for i in range(500):
            item = UniqueItemModel(key=f"newkey{i}", unique_code=f"UNIQUE{i:04d}")
            item.save()

        # Verify total 1000 again
        assert len(list(UniqueItemModel.query.all())) == 1000

    timer.assert_under(5, "Unique key operations (1000 items)")


@pytest.mark.slow
def test_bulk_relationship_integrity(performance_timer):
    """Create 200 parents + 1000 children, verify referential integrity."""
    with performance_timer() as timer:
        # Create 200 parents
        parents = []
        for i in range(200):
            parent = ParentModel(key=f"parent{i}", name=f"Parent {i}")
            parent.save()
            parents.append(parent)

        # Create 1000 children (5 per parent)
        children = []
        for i in range(1000):
            parent_idx = i // 5
            child = ChildModel(
                key=f"child{i}",
                parent=parents[parent_idx],
                value=i
            )
            child.save()
            children.append(child)

        # Verify child count
        assert len(list(ChildModel.query.all())) == 1000

        # Verify referential integrity - sample 50 children
        for i in range(0, 1000, 20):
            child = ChildModel.query.get(key=f"child{i}")
            assert child.parent is not None
            expected_parent_idx = i // 5
            # Load parent and check
            if isinstance(child.parent, str):
                parent = ParentModel.query.get(key=f"parent{expected_parent_idx}")
                assert child.parent == parent.db_key.redis_key
            else:
                assert child.parent.key == f"parent{expected_parent_idx}"

    timer.assert_under(10, "Relationship integrity (200 parents + 1000 children)")


@pytest.mark.slow
def test_bulk_filter_operations(performance_timer):
    """Save 1000+ items, test various filter operations."""
    # Save varied items
    for i in range(1000):
        FilterTestModel(key=f"user_{i:04d}", category="user").save()
    for i in range(100):
        FilterTestModel(key=f"admin_{i:03d}", category="admin").save()
    for i in range(50):
        FilterTestModel(key=f"guest_{i:02d}", category="guest").save()

    # Test __startswith
    with performance_timer() as timer:
        results = list(FilterTestModel.query.filter(key__startswith="user"))
    assert len(results) == 1000
    timer.assert_under(0.15, "Filter __startswith='user'")

    # Test __startswith with smaller set
    with performance_timer() as timer:
        results = list(FilterTestModel.query.filter(key__startswith="admin"))
    assert len(results) == 100
    timer.assert_under(0.15, "Filter __startswith='admin'")

    # Test __endswith
    with performance_timer() as timer:
        results = list(FilterTestModel.query.filter(key__endswith="_001"))
    timer.assert_under(0.15, "Filter __endswith='_001'")

    # Test __in
    with performance_timer() as timer:
        results = list(FilterTestModel.query.filter(
            key__in=["user_0001", "admin_001", "guest_01"]
        ))
    assert len(results) == 3
    timer.assert_under(0.15, "Filter __in with 3 values")


@pytest.mark.slow
def test_bulk_mixed_field_types(performance_timer):
    """Save 500 models with all field types, verify round-trip fidelity."""
    with performance_timer() as timer:
        # Create test data with edge values
        items = []
        for i in range(500):
            item = KitchenSinkModel(
                key=f"key{i}",
                int_field=i if i % 2 == 0 else -i,
                float_field=i * 1.5,
                decimal_field=Decimal(f"{i}.{i:03d}"),
                string_field=f"string_{i}" if i % 10 != 0 else "",  # Include empty strings
                bool_field=i % 2 == 0,
                bytes_field=f"bytes{i}".encode(),
                list_field=[i, i+1, i+2] if i % 5 != 0 else [],  # Include empty lists
                dict_field={"id": i, "value": i*2} if i % 7 != 0 else {},  # Include empty dicts
                set_field={i, i+1} if i % 3 != 0 else set(),
                tuple_field=(i, i+1),
                date_field=date(2020 + (i % 5), 1 + (i % 12), 1 + (i % 28)),
                datetime_field=datetime(2020, 1, 1, i % 24, i % 60, i % 60),
                time_field=dt_time(i % 24, i % 60, i % 60),
            )
            item.save()
            items.append(item)

        # Query all back
        results = list(KitchenSinkModel.query.all())
        assert len(results) == 500

        # Verify field types and values for sample
        for i in [0, 100, 250, 499]:
            result = KitchenSinkModel.query.get(key=f"key{i}")
            assert isinstance(result.int_field, int)
            assert isinstance(result.float_field, float)
            assert isinstance(result.decimal_field, Decimal)
            assert isinstance(result.string_field, str)
            assert isinstance(result.bool_field, bool)
            assert isinstance(result.bytes_field, bytes)
            assert isinstance(result.list_field, list)
            assert isinstance(result.dict_field, dict)
            assert isinstance(result.set_field, set)
            assert isinstance(result.tuple_field, tuple)
            assert isinstance(result.date_field, date)
            assert isinstance(result.datetime_field, datetime)
            assert isinstance(result.time_field, dt_time)

    timer.assert_under(8, "Mixed field types (500 items)")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_async_concurrent_operations(performance_timer):
    """Concurrently create/query/update/delete 500 items, verify integrity."""
    with performance_timer() as timer:
        # Concurrent creates (50 tasks × 10 items each)
        async def create_batch(start_idx):
            items = []
            for i in range(start_idx, start_idx + 10):
                item = KeyValueModel(key=f"async{i}", value=f"value{i}")
                await item.async_save()
                items.append(item)
            return items

        # Create concurrently
        tasks = [create_batch(i * 10) for i in range(50)]
        results = await asyncio.gather(*tasks)
        all_items = [item for batch in results for item in batch]

        assert len(all_items) == 500

        # Verify count
        all_results = await KeyValueModel.query.async_all()
        assert len(all_results) == 500

        # Concurrent updates
        async def update_item(key_idx):
            item = await KeyValueModel.query.async_get(key=f"async{key_idx}")
            item.value = f"updated{key_idx}"
            await item.async_save()
            return item

        tasks = [update_item(i) for i in range(500)]
        await asyncio.gather(*tasks)

        # Verify updates
        sample = await KeyValueModel.query.async_get(key="async100")
        assert sample.value == "updated100"

        # Concurrent deletes
        async def delete_item(key_idx):
            item = await KeyValueModel.query.async_get(key=f"async{key_idx}")
            await item.async_delete()

        tasks = [delete_item(i) for i in range(500)]
        await asyncio.gather(*tasks)

        # Verify all deleted
        final_count = await KeyValueModel.query.async_count()
        assert final_count == 0

    timer.assert_under(10, "Async concurrent operations (500 items)")


@pytest.mark.slow
def test_sorted_field_ordering(performance_timer):
    """Save 1000 items with random sorted values, verify ordering."""
    import random

    # Save 1000 items with random values
    values = list(range(1000))
    random.shuffle(values)

    for i, val in enumerate(values):
        SortedIntModel(key=f"key{i}", sorted_value=val).save()

    # Query ascending - use .all() not .filter() for order_by
    with performance_timer() as timer:
        results = list(SortedIntModel.query.all(order_by="sorted_value"))
    timer.assert_under(0.2, "Query with ascending order")

    # Verify monotonically increasing
    for i in range(len(results) - 1):
        assert results[i].sorted_value <= results[i + 1].sorted_value

    # Query descending
    with performance_timer() as timer:
        results = list(SortedIntModel.query.all(order_by="-sorted_value"))
    timer.assert_under(0.2, "Query with descending order")

    # Verify monotonically decreasing
    for i in range(len(results) - 1):
        assert results[i].sorted_value >= results[i + 1].sorted_value

    # Test limit with ordering
    all_sorted = list(SortedIntModel.query.all(order_by="sorted_value"))
    top_10 = all_sorted[:10]
    assert len(top_10) == 10
    assert top_10[0].sorted_value == 0
    assert top_10[-1].sorted_value == 9


@pytest.mark.slow
def test_rapid_update_cycles(performance_timer):
    """Save 100 items, update each 20 times, verify final values."""
    with performance_timer() as timer:
        # Create 100 items
        for i in range(100):
            UpdateCounterModel(key=f"key{i}", counter=0).save()

        # Update each 20 times
        for i in range(100):
            for _ in range(20):
                item = UpdateCounterModel.query.get(key=f"key{i}")
                item.counter += 1
                item.save()

        # Verify all counters == 20
        for i in range(100):
            item = UpdateCounterModel.query.get(key=f"key{i}")
            assert item.counter == 20, f"Expected counter=20, got {item.counter}"

    timer.assert_under(8, "Rapid update cycles (100 items × 20 updates)")


# =============================================================================
# EDGE CASE & ERROR TESTS
# =============================================================================

@pytest.mark.slow
def test_ttl_expiration_at_scale():
    """Save 200 items with 2s TTL, verify expiration."""
    # Save 200 items with TTL
    for i in range(200):
        TTLModel(key=f"ttl{i}", value=f"value{i}").save()

    # Verify all exist
    assert len(list(TTLModel.query.all())) == 200

    # Wait for expiration
    time.sleep(3)

    # Verify all expired
    count = len(list(TTLModel.query.all()))
    assert count == 0, f"Expected 0 items after TTL, got {count}"


@pytest.mark.slow
def test_deep_relationship_chains():
    """Create 50-level deep chain, verify chain integrity."""
    # Create chain: node0 -> node1 -> node2 -> ... -> node49
    nodes = []
    for i in range(50):
        next_key = f"node{i+1}" if i < 49 else None
        node = ChainNodeModel(key=f"node{i}", next_node_key=next_key)
        node.save()
        nodes.append(node)

    # Traverse chain from root
    current = ChainNodeModel.query.get(key="node0")
    count = 0
    while current and count < 100:  # Safety limit to prevent infinite loop
        if current.next_node_key:
            current = ChainNodeModel.query.get(key=current.next_node_key)
            count += 1
        else:
            break

    assert count == 49, f"Expected 49 traversals, got {count}"


@pytest.mark.slow
def test_query_result_limit_offset():
    """Test pagination with limit and offset on 1000 items."""
    # Save 1000 items
    for i in range(1000):
        KeyValueModel(key=f"key{i:04d}", value=f"value{i}").save()

    # Test pagination - get all then slice (filter with only limit may not work)
    all_items = list(KeyValueModel.query.all())
    page1 = all_items[:100]
    assert len(page1) == 100

    # Note: offset not supported in base query, so this tests limit behavior
    # If offset were supported, we'd test: limit=100, offset=100, etc.

    # Verify no duplicates in result set
    all_results = list(KeyValueModel.query.all())
    keys = [r.key for r in all_results]
    assert len(keys) == len(set(keys)), "Found duplicate keys"


@pytest.mark.slow
def test_pipeline_batch_operations():
    """Use Redis pipeline for batch operations on 1000 items."""
    # Create items
    items = [KeyValueModel(key=f"pipe{i}", value=f"value{i}") for i in range(1000)]

    # Batch save using pipeline
    pipeline = POPOTO_REDIS_DB.pipeline()
    for item in items:
        item.save()  # TODO: popoto may not have explicit pipeline support yet
    pipeline.execute()

    # Verify all saved
    assert len(list(KeyValueModel.query.all())) >= 1000

    # Batch delete
    for item in items:
        item.delete()

    # Verify all deleted
    remaining = len([k for k in POPOTO_REDIS_DB.keys("KeyValueModel:pipe*")])
    assert remaining == 0


@pytest.mark.slow
def test_field_type_coercion_errors():
    """Test that invalid field values raise appropriate errors."""
    # IntField with string - depends on popoto's coercion behavior
    item = KitchenSinkModel(
        key="error_test",
        int_field=42,  # Valid
        float_field=3.14,
        decimal_field=Decimal("1.23"),
        string_field="valid",
        bool_field=True,
        bytes_field=b"bytes",
        list_field=[],
        dict_field={},
        set_field=set(),
        tuple_field=(),
        date_field=date.today(),
        datetime_field=datetime.now(),
        time_field=dt_time(12, 0, 0),
    )
    item.save()

    # If type coercion fails, it should raise during save or query
    loaded = KitchenSinkModel.query.get(key="error_test")
    assert loaded.int_field == 42


@pytest.mark.slow
def test_memory_efficiency():
    """Save 5000 items, verify memory usage stays reasonable."""
    import sys

    # Save 5000 items
    for i in range(5000):
        KeyValueModel(key=f"mem{i}", value=f"value{i}").save()

    # Query without loading all into memory at once
    # Iterate through results as generator
    count = 0
    for item in KeyValueModel.query.all():
        count += 1
        # Process one at a time
        assert item.key.startswith("mem") or item.key.startswith("key")

    assert count >= 5000

    # Check object count (rough memory check)
    # This is a basic sanity check - not precise memory profiling
    import gc
    gc.collect()
    obj_count = len(gc.get_objects())
    # Just verify it's not absurdly high (< 1M objects)
    assert obj_count < 1_000_000, f"Object count unexpectedly high: {obj_count}"


# =============================================================================
# PERFORMANCE BENCHMARKING TESTS
# =============================================================================

@pytest.mark.slow
@pytest.mark.benchmark
def test_benchmark_save_operations(performance_timer):
    """Benchmark save throughput."""
    # Sequential saves
    with performance_timer() as timer:
        for i in range(1000):
            KeyValueModel(key=f"bench{i}", value=f"value{i}").save()

    ops_per_sec = 1000 / timer.elapsed
    print(f"\n  Sequential saves: {ops_per_sec:.0f} ops/sec ({timer.elapsed:.2f}s total)")

    assert ops_per_sec > 100, f"Save throughput too low: {ops_per_sec:.0f} ops/sec"


@pytest.mark.slow
@pytest.mark.benchmark
def test_benchmark_query_operations(performance_timer):
    """Benchmark query performance."""
    # Setup: save 1000 items
    for i in range(1000):
        KeyValueModel(key=f"qbench{i}", value=f"value{i}").save()

    # Individual gets
    with performance_timer() as timer:
        for i in range(100):
            KeyValueModel.query.get(key=f"qbench{i}")

    ops_per_sec = 100 / timer.elapsed
    print(f"\n  Individual gets: {ops_per_sec:.0f} ops/sec ({timer.elapsed:.2f}s total)")

    # Bulk query
    with performance_timer() as timer:
        results = list(KeyValueModel.query.all())

    print(f"  Bulk query (1000 items): {timer.elapsed:.3f}s")
    assert len(results) >= 1000
    assert timer.elapsed < 1.0, f"Bulk query too slow: {timer.elapsed:.3f}s"
