"""
Tests for pushing a query's limit down into the sorted set range read.

A query that filters and orders by the same SortedField can ask Redis for
only the top N members instead of the whole range. That is only sound when
nothing can drop a row after hydration, so the optimization is conditional:
every other query shape must keep reading the full range.

See Query._resolve_range_pushdown for the gate.
"""

import os
import sys
from unittest import mock

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import Query
from src.popoto.redis_db import POPOTO_REDIS_DB


class PushdownJob(popoto.Model):
    """Mirrors the shape the optimization exists for: a partitioned sorted
    field (jobs within a room, ordered by recency)."""

    room_id = popoto.KeyField(type=str)
    job_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")
    priority = popoto.SortedField(type=float, partition_by="room_id")
    status = popoto.Field(type=str, default="open")


ROOM = "room-a"
OTHER_ROOM = "room-b"
JOB_COUNT = 60


@pytest.fixture(autouse=True)
def jobs():
    """A room with JOB_COUNT jobs, last_active_at ascending with job number.

    Every tenth job is "rare" and sits in the low end of the range, so a
    query for rare jobs ordered by recency cannot be answered from the top
    of the sorted set.
    """
    created = []
    for i in range(JOB_COUNT):
        created.append(
            PushdownJob(
                room_id=ROOM,
                job_id=f"job-{i:03d}",
                last_active_at=float(i),
                priority=float(JOB_COUNT - i),
                status="rare" if i % 10 == 0 else "open",
            ).save()
        )
    # A second room to prove the partition is not being read
    PushdownJob(
        room_id=OTHER_ROOM, job_id="other", last_active_at=999.0, priority=0.0
    ).save()
    return created


@pytest.fixture
def redis_spy():
    """Watch both range-read forms without changing what they do."""
    with (
        mock.patch.object(
            POPOTO_REDIS_DB, "zrange", wraps=POPOTO_REDIS_DB.zrange
        ) as zrange,
        mock.patch.object(
            POPOTO_REDIS_DB, "zrangebyscore", wraps=POPOTO_REDIS_DB.zrangebyscore
        ) as zrangebyscore,
    ):
        yield zrange, zrangebyscore


def job_ids(results):
    return [job.job_id for job in results]


# ---------------------------------------------------------------------------
# Qualifying queries: partition + range on one sorted field, ordered by it
# ---------------------------------------------------------------------------


def test_descending_limit_reads_only_the_top_n(redis_spy):
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == [f"job-{i:03d}" for i in range(59, 54, -1)]
    zrangebyscore.assert_not_called()
    assert zrange.call_count == 1
    assert zrange.call_args.kwargs["num"] == 5
    assert zrange.call_args.kwargs["desc"] is True
    assert zrange.call_args.kwargs["byscore"] is True
    # REV BYSCORE takes its bounds high-to-low
    assert zrange.call_args.args[1:] == ("+inf", "0.0")


def test_ascending_limit_reads_only_the_bottom_n(redis_spy):
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="last_active_at",
        limit=5,
    )

    assert job_ids(results) == [f"job-{i:03d}" for i in range(5)]
    zrangebyscore.assert_not_called()
    assert zrange.call_args.kwargs["num"] == 5
    assert zrange.call_args.kwargs["desc"] is False
    assert zrange.call_args.args[1:] == ("0.0", "+inf")


def test_without_order_by_the_bound_follows_the_sets_own_order(redis_spy):
    """With no order_by, a sorted field filter already returns ascending
    score order, so the bound must be read from the low end."""
    zrange, _ = redis_spy

    results = PushdownJob.query.filter(room_id=ROOM, last_active_at__gte=0.0, limit=5)

    assert job_ids(results) == [f"job-{i:03d}" for i in range(5)]
    assert zrange.call_args.kwargs["desc"] is False


def test_bounded_read_narrows_the_range_too(redis_spy):
    zrange, _ = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=10.0,
        last_active_at__lt=20.0,
        order_by="-last_active_at",
        limit=3,
    )

    assert job_ids(results) == ["job-019", "job-018", "job-017"]
    assert zrange.call_args.args[1:] == ("(20.0", "10.0")


def test_only_the_bounded_keys_are_hydrated():
    """The point of the optimization: hydration is bounded by the limit,
    not by the size of the range."""
    hydrated = []
    real = Query.get_many_objects

    def spy(model, db_keys, *args, **kwargs):
        hydrated.append(len(db_keys))
        return real(model, db_keys, *args, **kwargs)

    with mock.patch.object(Query, "get_many_objects", staticmethod(spy)):
        bounded = PushdownJob.query.filter(
            room_id=ROOM,
            last_active_at__gte=0.0,
            order_by="-last_active_at",
            limit=5,
        )
        assert len(bounded) == 5
        unbounded = PushdownJob.query.filter(
            room_id=ROOM, last_active_at__gte=0.0, order_by="-last_active_at"
        )
        assert len(unbounded) == JOB_COUNT

    assert hydrated == [5, JOB_COUNT]


def test_limit_larger_than_the_range_returns_everything(redis_spy):
    zrange, _ = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=JOB_COUNT * 2,
    )

    assert len(results) == JOB_COUNT
    assert zrange.call_args.kwargs["num"] == JOB_COUNT * 2


def test_partition_is_respected(redis_spy):
    results = PushdownJob.query.filter(
        room_id=OTHER_ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == ["other"]


# ---------------------------------------------------------------------------
# Non-qualifying queries: must read the full range and stay complete
# ---------------------------------------------------------------------------


def test_client_side_predicate_blocks_the_pushdown(redis_spy):
    """status is unindexed, so it is applied after hydration. Reading only
    the top 5 of the range would return zero rows here, because the newest
    rare job is at position 50 of 60."""
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        status="rare",
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == ["job-050", "job-040", "job-030", "job-020", "job-010"]
    zrange.assert_not_called()
    assert zrangebyscore.call_count == 1


def test_second_sorted_field_predicate_blocks_the_pushdown(redis_spy):
    """priority intersects a second key set after the range read, so the
    range read cannot be truncated."""
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        priority__lte=15.0,
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == [f"job-{i:03d}" for i in range(59, 54, -1)]
    zrange.assert_not_called()
    assert zrangebyscore.called


def test_ordering_by_another_field_blocks_the_pushdown(redis_spy):
    """Top-N by last_active_at is not top-N by priority."""
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-priority",
        limit=3,
    )

    assert job_ids(results) == ["job-000", "job-001", "job-002"]
    zrange.assert_not_called()
    assert zrangebyscore.called


def test_q_objects_block_the_pushdown(redis_spy):
    """Q results are intersected with the kwargs result, so the kwargs read
    must stay complete."""
    from src.popoto.models.q import Q

    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        Q(job_id="job-010"),
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == ["job-010"]
    zrange.assert_not_called()


def test_no_limit_is_unchanged(redis_spy):
    zrange, zrangebyscore = redis_spy

    results = PushdownJob.query.filter(
        room_id=ROOM, last_active_at__gte=0.0, order_by="-last_active_at"
    )

    assert len(results) == JOB_COUNT
    zrange.assert_not_called()
    assert zrangebyscore.call_args.args[1:] == ("0.0", "+inf")


@pytest.mark.parametrize("limit", [0, -1, None, True, 2.5, "5"])
def test_non_positive_int_limits_do_not_push_down(redis_spy, limit):
    zrange, _ = redis_spy

    PushdownJob.query.filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=limit,
    )

    zrange.assert_not_called()


def test_count_is_not_truncated_by_a_limit():
    """count() must report the full match count regardless of limit."""
    assert PushdownJob.query.count(room_id=ROOM, last_active_at__gte=0.0) == JOB_COUNT
    assert (
        PushdownJob.query.filter(
            room_id=ROOM, last_active_at__gte=0.0, order_by="-last_active_at"
        )
        .limit(5)
        .count()
        == JOB_COUNT
    )


# ---------------------------------------------------------------------------
# Meta.order_by supplies the direction when the query does not
# ---------------------------------------------------------------------------


class MetaOrderedJob(popoto.Model):
    room_id = popoto.KeyField(type=str)
    job_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")

    class Meta:
        order_by = "-last_active_at"


class MetaOrderedByOtherField(popoto.Model):
    room_id = popoto.KeyField(type=str)
    job_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")

    class Meta:
        order_by = "job_id"


def test_meta_order_by_on_the_sorted_field_sets_the_direction(redis_spy):
    zrange, _ = redis_spy
    for i in range(20):
        MetaOrderedJob(
            room_id=ROOM, job_id=f"m-{i:03d}", last_active_at=float(i)
        ).save()

    results = MetaOrderedJob.query.filter(
        room_id=ROOM, last_active_at__gte=0.0, limit=3
    )

    assert [job.job_id for job in results] == ["m-019", "m-018", "m-017"]
    assert zrange.call_args.kwargs["desc"] is True


def test_meta_order_by_on_another_field_blocks_the_pushdown(redis_spy):
    zrange, zrangebyscore = redis_spy
    for i in range(20):
        MetaOrderedByOtherField(
            room_id=ROOM, job_id=f"z-{19 - i:03d}", last_active_at=float(i)
        ).save()

    results = MetaOrderedByOtherField.query.filter(
        room_id=ROOM, last_active_at__gte=0.0, limit=3
    )

    assert [job.job_id for job in results] == ["z-000", "z-001", "z-002"]
    zrange.assert_not_called()
    assert zrangebyscore.called


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_pushes_down_too(redis_spy):
    zrange, zrangebyscore = redis_spy

    results = await PushdownJob.query.async_filter(
        room_id=ROOM,
        last_active_at__gte=0.0,
        order_by="-last_active_at",
        limit=5,
    )

    assert job_ids(results) == [f"job-{i:03d}" for i in range(59, 54, -1)]
    zrangebyscore.assert_not_called()
    assert zrange.call_args.kwargs["num"] == 5
