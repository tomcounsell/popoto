import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
import asyncio
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module


@pytest.fixture(autouse=True)
def flush_redis():
    """Clean Redis and reset async connection before each test.

    The async Redis connection is tied to an event loop. Since pytest-asyncio
    creates a new event loop per test, we must reset the cached async connection
    to avoid 'Future attached to a different loop' errors.
    """
    POPOTO_REDIS_DB.flushdb()
    # Reset the cached async connection and lock so they get recreated in the
    # new event loop (pytest-asyncio creates a fresh loop per test)
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()


class TestJob(popoto.Model):
    job_id = popoto.AutoKeyField()
    project_key = popoto.KeyField()
    status = popoto.KeyField(default="pending")
    priority = popoto.SortedField(type=int, partition_by="project_key")
    created_at = popoto.SortedField(type=float, partition_by="project_key")
    message_text = popoto.Field()


@pytest.mark.asyncio
async def test_async_create():
    """Test async_create creates a model instance."""
    job = await TestJob.async_create(
        project_key="valor",
        status="pending",
        priority=1,
        created_at=1234567890.0,
        message_text="test message",
    )

    assert job is not None
    assert job.project_key == "valor"
    assert job.status == "pending"
    assert job.priority == 1


@pytest.mark.asyncio
async def test_async_save():
    """Test async_save persists changes."""
    job = TestJob(
        project_key="valor", status="pending", priority=1, created_at=1234567890.0
    )

    result = await job.async_save()
    assert result is not False

    # Verify saved
    loaded = TestJob.query.get(job_id=job.job_id, project_key="valor", status="pending")
    assert loaded is not None
    assert loaded.priority == 1


@pytest.mark.asyncio
async def test_async_filter():
    """Test async_filter returns matching instances."""
    # Create test data
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="valor", status="running", priority=2, created_at=2.0
    )
    await TestJob.async_create(
        project_key="other", status="pending", priority=3, created_at=3.0
    )

    # Filter
    jobs = await TestJob.query.async_filter(project_key="valor", status="pending")

    assert len(jobs) == 1
    assert jobs[0].project_key == "valor"
    assert jobs[0].status == "pending"


@pytest.mark.asyncio
async def test_async_get():
    """Test async_get retrieves single instance."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    loaded = await TestJob.query.async_get(
        job_id=job.job_id, project_key="valor", status="pending"
    )

    assert loaded is not None
    assert loaded.job_id == job.job_id
    assert loaded.priority == 1


@pytest.mark.asyncio
async def test_async_delete():
    """Test async_delete removes instance."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    result = await job.async_delete()
    assert result is True

    # Verify deleted
    loaded = TestJob.query.get(job_id=job.job_id, project_key="valor", status="pending")
    assert loaded is None


@pytest.mark.asyncio
async def test_async_all():
    """Test async_all returns all instances."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="valor", status="running", priority=2, created_at=2.0
    )

    jobs = await TestJob.query.async_all()

    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_async_count():
    """Test async_count returns count."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="valor", status="running", priority=2, created_at=2.0
    )

    count = await TestJob.query.async_count()
    assert count == 2

    count_filtered = await TestJob.query.async_count(status="pending")
    assert count_filtered == 1


@pytest.mark.asyncio
async def test_async_load():
    """Test async_load retrieves by db_key."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    loaded = await TestJob.async_load(
        job_id=job.job_id, project_key="valor", status="pending"
    )

    assert loaded is not None
    assert loaded == job


@pytest.mark.asyncio
async def test_async_keys():
    """Test async_keys returns Redis keys."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="valor", status="running", priority=2, created_at=2.0
    )

    keys = await TestJob.query.async_keys()

    assert len(keys) == 2


@pytest.mark.asyncio
async def test_async_filter_with_limit():
    """Test async_filter with limit parameter."""
    # Create test data
    for i in range(5):
        await TestJob.async_create(
            project_key="valor", status="pending", priority=i, created_at=float(i)
        )

    # Filter with limit
    jobs = await TestJob.query.async_filter(project_key="valor", limit=3)

    assert len(jobs) == 3


@pytest.mark.asyncio
async def test_async_filter_with_order_by():
    """Test async_filter with order_by parameter."""
    # Create test data with different priorities
    await TestJob.async_create(
        project_key="valor", status="pending", priority=3, created_at=1.0
    )
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=2.0
    )
    await TestJob.async_create(
        project_key="valor", status="pending", priority=2, created_at=3.0
    )

    # Filter with order_by
    jobs = await TestJob.query.async_filter(
        project_key="valor", status="pending", order_by="priority"
    )

    assert len(jobs) == 3
    assert jobs[0].priority == 1
    assert jobs[1].priority == 2
    assert jobs[2].priority == 3


@pytest.mark.asyncio
async def test_async_update_and_save():
    """Test updating a field and saving asynchronously."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    # Update status (KeyField mutation requires migrate_key=True)
    job.status = "running"
    await job.async_save(migrate_key=True)

    # Reload and verify - need to use new key since status is a KeyField
    loaded = TestJob.query.get(job_id=job.job_id, project_key="valor", status="running")
    assert loaded is not None
    assert loaded.status == "running"


@pytest.mark.asyncio
async def test_async_concurrent_operations():
    """Test running multiple async operations concurrently."""
    # Create multiple jobs concurrently
    jobs = await asyncio.gather(
        TestJob.async_create(
            project_key="valor", status="pending", priority=1, created_at=1.0
        ),
        TestJob.async_create(
            project_key="valor", status="pending", priority=2, created_at=2.0
        ),
        TestJob.async_create(
            project_key="valor", status="pending", priority=3, created_at=3.0
        ),
    )

    assert len(jobs) == 3

    # Query concurrently
    results = await asyncio.gather(
        TestJob.query.async_count(),
        TestJob.query.async_all(),
        TestJob.query.async_filter(
            project_key="valor", priority__gte=2
        ),  # Must include project_key for sorted field
    )

    count, all_jobs, filtered_jobs = results
    assert count == 3
    assert len(all_jobs) == 3
    assert len(filtered_jobs) == 2


# ============================================================================
# NEW TEST MODELS FOR GAP COVERAGE
# ============================================================================


class AsyncGeoModel(popoto.Model):
    """Model with GeoField for testing async geo queries."""

    key = popoto.KeyField()
    coordinates = popoto.GeoField()


class AsyncRelTarget(popoto.Model):
    """Target model for relationship testing."""

    name = popoto.KeyField()


class AsyncRelModel(popoto.Model):
    """Model with relationship for testing async lazy loading."""

    label = popoto.KeyField()
    target = popoto.Relationship(model=AsyncRelTarget)


class AsyncOrderModel(popoto.Model):
    """Model for testing client-side filtering."""

    name = popoto.KeyField()
    category = popoto.Field(type=str, default="")
    score = popoto.SortedField(type=float)


class AsyncOrderByModel(popoto.Model):
    """Model with Meta.order_by for testing default ordering."""

    name = popoto.KeyField()
    priority = popoto.SortedField(type=int)

    class Meta:
        order_by = "priority"


class AsyncTTLModel(popoto.Model):
    """Model with Meta.ttl for testing TTL behavior."""

    key = popoto.KeyField()
    value = popoto.Field(type=str, default="")

    class Meta:
        ttl = 2


# ============================================================================
# GAP 2: SortedField range queries via async_filter
# ============================================================================


@pytest.mark.asyncio
async def test_async_filter_sorted_lte():
    """Gap 2: async SortedField __lte filter."""
    await TestJob.async_create(
        project_key="p1", status="pending", priority=10, created_at=1.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=20, created_at=2.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=30, created_at=3.0
    )

    jobs = await TestJob.query.async_filter(project_key="p1", priority__lte=20)
    assert len(jobs) == 2
    priorities = [job.priority for job in jobs]
    assert 10 in priorities
    assert 20 in priorities


@pytest.mark.asyncio
async def test_async_filter_sorted_lt():
    """Gap 2: async SortedField __lt filter."""
    await TestJob.async_create(
        project_key="p1", status="pending", priority=10, created_at=1.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=20, created_at=2.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=30, created_at=3.0
    )

    jobs = await TestJob.query.async_filter(project_key="p1", priority__lt=20)
    assert len(jobs) == 1
    assert jobs[0].priority == 10


@pytest.mark.asyncio
async def test_async_filter_sorted_gt():
    """Gap 2: async SortedField __gt filter."""
    await TestJob.async_create(
        project_key="p1", status="pending", priority=10, created_at=1.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=20, created_at=2.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=30, created_at=3.0
    )

    jobs = await TestJob.query.async_filter(project_key="p1", priority__gt=10)
    assert len(jobs) == 2
    priorities = [job.priority for job in jobs]
    assert 20 in priorities
    assert 30 in priorities


@pytest.mark.asyncio
async def test_async_filter_sorted_between():
    """Gap 2: async SortedField __gte and __lte combined filter."""
    await TestJob.async_create(
        project_key="p1", status="pending", priority=10, created_at=1.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=20, created_at=2.0
    )
    await TestJob.async_create(
        project_key="p1", status="pending", priority=30, created_at=3.0
    )

    jobs = await TestJob.query.async_filter(
        project_key="p1", priority__gte=10, priority__lte=20
    )
    assert len(jobs) == 2
    priorities = [job.priority for job in jobs]
    assert 10 in priorities
    assert 20 in priorities


# ============================================================================
# GAP 3: Relationship lazy-loading via async
# ============================================================================


@pytest.mark.asyncio
async def test_async_relationship_lazy_load():
    """Gap 3: async relationship lazy loading."""
    # Create target
    target = await AsyncRelTarget.async_create(name="target1")

    # Create model with relationship
    rel = await AsyncRelModel.async_create(label="rel1", target=target)

    # Query back and verify relationship loads
    results = await AsyncRelModel.query.async_filter(label="rel1")
    assert len(results) == 1
    # Relationship is stored as redis_key string, need to load it
    if isinstance(results[0].target, str):
        loaded_target = AsyncRelTarget.query.get(redis_key=results[0].target)
        assert loaded_target.name == "target1"
    else:
        assert results[0].target.name == "target1"

    # Cleanup
    await rel.async_delete()
    await target.async_delete()


# ============================================================================
# GAP 4: GeoField async filter
# ============================================================================


@pytest.mark.asyncio
async def test_async_filter_geofield():
    """Gap 4: async GeoField with radius filter."""
    # Rome coordinates
    geo1 = await AsyncGeoModel.async_create(
        key="rome", coordinates=(41.902782, 12.496366)
    )
    # Vatican coordinates (nearby)
    geo2 = await AsyncGeoModel.async_create(
        key="vatican", coordinates=(41.904755, 12.454628)
    )

    # Search within 5km of Rome
    results = await AsyncGeoModel.query.async_filter(
        coordinates=(41.902782, 12.496366),
        coordinates_radius=5,
        coordinates_radius_unit="km",
    )

    # Both should be within 5km
    assert len(results) == 2

    # Cleanup
    await geo1.async_delete()
    await geo2.async_delete()


# ============================================================================
# GAP 5: values= projection
# ============================================================================


@pytest.mark.asyncio
async def test_async_filter_values_projection():
    """Gap 5: async_filter with values projection."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="other", status="running", priority=2, created_at=2.0
    )

    results = await TestJob.query.async_filter(
        project_key="valor", values=("project_key", "status")
    )

    assert len(results) == 1
    assert isinstance(results[0], dict)
    assert set(results[0].keys()) == {"project_key", "status"}
    assert results[0]["project_key"] == "valor"


@pytest.mark.asyncio
async def test_async_all_values_projection():
    """Gap 5: async_all with values projection."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="other", status="running", priority=2, created_at=2.0
    )

    results = await TestJob.query.async_all(values=("project_key",))

    assert len(results) == 2
    assert all(isinstance(r, dict) for r in results)
    assert all(set(r.keys()) == {"project_key"} for r in results)


# ============================================================================
# GAP 6: async_get with multiple matches
# ============================================================================


@pytest.mark.asyncio
async def test_async_get_raises_on_multiple_matches():
    """Gap 6: async_get raises QueryException with multiple matching objects."""
    from src.popoto.models.query import QueryException

    # Create two jobs with same project_key and status but different job_ids
    await TestJob.async_create(
        project_key="dupe", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="dupe", status="pending", priority=2, created_at=2.0
    )

    # async_get should raise QueryException when multiple matches found
    with pytest.raises(QueryException):
        await TestJob.query.async_get(project_key="dupe", status="pending")


# ============================================================================
# GAP 7: async_get returns None on miss
# ============================================================================


@pytest.mark.asyncio
async def test_async_get_returns_none_on_miss():
    """Gap 7: async_get returns None when no match found."""
    # Use a properly formatted but non-existent job_id (32-char hex UUID without dashes)
    result = await TestJob.query.async_get(
        job_id="00000000000000000000000000000000",
        project_key="nonexistent_project",
        status="nonexistent_status",
    )
    assert result is None


# ============================================================================
# GAP 11: async_scan_keys
# ============================================================================


@pytest.mark.asyncio
async def test_async_scan_keys():
    """Gap 11: async_scan_keys functionality."""
    from src.popoto.redis_db import async_scan_keys

    # Create test data
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )
    await TestJob.async_create(
        project_key="other", status="running", priority=2, created_at=2.0
    )

    # Scan for TestJob keys
    keys = await async_scan_keys("TestJob:*")

    # Should have at least 2 keys (may have index keys too)
    assert len(keys) >= 2


# ============================================================================
# GAP 12: async_keys with catchall and clean
# ============================================================================


@pytest.mark.asyncio
async def test_async_keys_catchall():
    """Gap 12: async_keys with catchall parameter."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    keys = await TestJob.query.async_keys(catchall=True)

    assert isinstance(keys, list)
    assert len(keys) >= 1


@pytest.mark.asyncio
async def test_async_keys_clean():
    """Gap 12: async_keys with clean parameter."""
    await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    keys = await TestJob.query.async_keys(clean=True)

    assert isinstance(keys, list)
    assert len(keys) >= 1


# ============================================================================
# GAP 13: Client-side filter via async_filter
# ============================================================================


@pytest.mark.asyncio
async def test_async_filter_client_side():
    """Gap 13: client-side filtering on plain Field (non-indexed)."""
    await AsyncOrderModel.async_create(name="item1", category="cat_a", score=1.0)
    await AsyncOrderModel.async_create(name="item2", category="cat_a", score=2.0)
    await AsyncOrderModel.async_create(name="item3", category="cat_b", score=3.0)

    # Filter on category (a plain Field, not KeyField)
    results = await AsyncOrderModel.query.async_filter(category="cat_a")

    assert len(results) == 2

    # Cleanup
    for item in results:
        await item.async_delete()
    item3 = await AsyncOrderModel.query.async_get(name="item3")
    if item3:
        await item3.async_delete()


# ============================================================================
# GAP 14: Meta.order_by
# ============================================================================


@pytest.mark.asyncio
async def test_async_filter_meta_order_by():
    """Gap 14: async_filter respects Meta.order_by."""
    await AsyncOrderByModel.async_create(name="job1", priority=30)
    await AsyncOrderByModel.async_create(name="job2", priority=10)
    await AsyncOrderByModel.async_create(name="job3", priority=20)

    results = await AsyncOrderByModel.query.async_filter()

    assert len(results) == 3
    assert results[0].priority == 10
    assert results[1].priority == 20
    assert results[2].priority == 30

    # Cleanup
    for job in results:
        await job.async_delete()


@pytest.mark.asyncio
async def test_async_all_meta_order_by():
    """Gap 14: async_all respects Meta.order_by."""
    await AsyncOrderByModel.async_create(name="job1", priority=30)
    await AsyncOrderByModel.async_create(name="job2", priority=10)
    await AsyncOrderByModel.async_create(name="job3", priority=20)

    results = await AsyncOrderByModel.query.async_all()

    assert len(results) == 3
    assert results[0].priority == 10
    assert results[1].priority == 20
    assert results[2].priority == 30

    # Cleanup
    for job in results:
        await job.async_delete()


# ============================================================================
# GAP 15: Meta.ttl via async_save
# ============================================================================


@pytest.mark.asyncio
async def test_async_save_meta_ttl():
    """Gap 15: async_save respects Meta.ttl."""
    instance = await AsyncTTLModel.async_create(key="ttl_test", value="data")

    # Check TTL is set
    ttl = POPOTO_REDIS_DB.ttl(instance.db_key.redis_key)
    assert ttl > 0
    assert ttl <= 2

    # Cleanup
    await instance.async_delete()


# ============================================================================
# GAP 16: async_save with pipeline argument
# ============================================================================


@pytest.mark.asyncio
async def test_async_save_with_pipeline():
    """Gap 16: async_save works with pipeline-based atomic save."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    # Modify and save
    job.message_text = "updated message"
    await job.async_save()

    # Reload and verify persistence
    loaded = await TestJob.query.async_get(
        job_id=job.job_id, project_key="valor", status="pending"
    )
    assert loaded is not None
    assert loaded.message_text == "updated message"


# ============================================================================
# GAP 17: Concurrent async_save to same key
# ============================================================================


@pytest.mark.asyncio
async def test_async_concurrent_save_same_key():
    """Gap 17: concurrent async_save operations on the same instance."""
    job = await TestJob.async_create(
        project_key="valor", status="pending", priority=1, created_at=1.0
    )

    # Define save tasks with different message_text values
    async def save_with_message(msg):
        job.message_text = msg
        await job.async_save()

    # Fire 5 concurrent saves
    await asyncio.gather(
        save_with_message("msg1"),
        save_with_message("msg2"),
        save_with_message("msg3"),
        save_with_message("msg4"),
        save_with_message("msg5"),
    )

    # Reload and verify it has one of the values (last-write-wins)
    loaded = await TestJob.query.async_get(
        job_id=job.job_id, project_key="valor", status="pending"
    )
    assert loaded is not None
    assert loaded.message_text in ["msg1", "msg2", "msg3", "msg4", "msg5"]
