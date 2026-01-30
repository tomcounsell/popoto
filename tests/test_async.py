import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
import asyncio
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB


@pytest.fixture(autouse=True)
def flush_redis():
    """Clean Redis before each test."""
    POPOTO_REDIS_DB.flushdb()


class TestJob(popoto.Model):
    job_id = popoto.AutoKeyField()
    project_key = popoto.KeyField()
    status = popoto.KeyField(default="pending")
    priority = popoto.SortedField(type=int, sort_by="project_key")
    created_at = popoto.SortedField(type=float, sort_by="project_key")
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

    # Update status
    job.status = "running"
    await job.async_save()

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
