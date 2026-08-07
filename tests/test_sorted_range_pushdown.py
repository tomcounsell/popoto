"""
Tests for pushing ``limit`` into the SortedField range read.

When a query is ordered by the same SortedField it filters on, and nothing
downstream can eliminate a row, the bound belongs in the Redis call: the
sorted set returns N members and only N objects get hydrated. Every other
shape must read the full range exactly as before, because a bound spent
before a later filter runs would silently return short results.

The negative cases are the point of this file. Guard conditions live in
``Query._sorted_pushdown_args``.
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto import Q
from src.popoto.redis_db import POPOTO_REDIS_DB


class PushdownDoc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")
    tag = popoto.Field(type=str, null=True)
    rank = popoto.SortedField(type=float, null=False, default=0.0)
    bucket = popoto.IndexedField(type=str, null=True)


POPULATION = 60


@pytest.fixture(autouse=True)
def clean_docs():
    _flush()
    yield
    _flush()


def _flush():
    for key in POPOTO_REDIS_DB.keys("*PushdownDoc*"):
        POPOTO_REDIS_DB.delete(key)


def _seed(room="r1", count=POPULATION, tag="x", bucket="a"):
    for i in range(count):
        PushdownDoc(
            room_id=room,
            doc_id=f"d{i}",
            last_active_at=float(i),
            tag=tag,
            rank=float(count - i),
            bucket=bucket,
        ).save()


class HydrationCounter:
    """Count HGETALL commands, which is one per object actually loaded."""

    def __enter__(self):
        import redis

        self.count = 0
        self._orig = redis.client.Pipeline.hgetall
        counter = self

        def hgetall(pipeline_self, name):
            counter.count += 1
            return counter._orig(pipeline_self, name)

        redis.client.Pipeline.hgetall = hgetall
        return self

    def __exit__(self, *exc):
        import redis

        redis.client.Pipeline.hgetall = self._orig


# ---------------------------------------------------------------------------
# Positive: the bound reaches Redis
# ---------------------------------------------------------------------------


def test_descending_bound_hydrates_only_limit():
    _seed()
    with HydrationCounter() as counter:
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                order_by="-last_active_at",
                limit=5,
            )
        )
    assert [d.doc_id for d in results] == ["d59", "d58", "d57", "d56", "d55"]
    assert (
        counter.count < POPULATION
    ), f"bound did not reach Redis: hydrated {counter.count} of {POPULATION}"


def test_ascending_bound_hydrates_only_limit():
    _seed()
    with HydrationCounter() as counter:
        results = list(
            PushdownDoc.query.filter(room_id="r1", last_active_at__gte=0, limit=5)
        )
    assert [d.doc_id for d in results] == ["d0", "d1", "d2", "d3", "d4"]
    assert counter.count < POPULATION


def test_bounded_matches_unbounded_ordering():
    """The bounded result must equal the full read sorted and sliced."""
    _seed()
    everything = list(PushdownDoc.query.filter(room_id="r1", last_active_at__gte=0))
    everything.sort(key=lambda d: d.last_active_at, reverse=True)
    expected = [d.doc_id for d in everything[:7]]

    bounded = list(
        PushdownDoc.query.filter(
            room_id="r1", last_active_at__gte=0, order_by="-last_active_at", limit=7
        )
    )
    assert [d.doc_id for d in bounded] == expected


def test_range_bounds_still_apply_under_pushdown():
    _seed()
    results = list(
        PushdownDoc.query.filter(
            room_id="r1",
            last_active_at__gte=10,
            last_active_at__lt=20,
            order_by="-last_active_at",
            limit=3,
        )
    )
    assert [d.doc_id for d in results] == ["d19", "d18", "d17"]


def test_limit_larger_than_population_returns_everything():
    _seed(count=12)
    results = list(
        PushdownDoc.query.filter(
            room_id="r1", last_active_at__gte=0, order_by="-last_active_at", limit=500
        )
    )
    assert len(results) == 12


def test_partition_isolates_rooms():
    _seed(room="r1", count=20)
    _seed(room="r2", count=20)
    results = list(
        PushdownDoc.query.filter(
            room_id="r2", last_active_at__gte=0, order_by="-last_active_at", limit=3
        )
    )
    assert len(results) == 3
    assert all(d.room_id == "r2" for d in results)


# ---------------------------------------------------------------------------
# Negative: the bound must NOT reach Redis, or rows would vanish
# ---------------------------------------------------------------------------


def test_plain_field_filter_disables_pushdown():
    """The case that silently returns short results without the guard.

    One row matches ``tag='keep'`` and it holds the lowest score, so a bound
    spent before the client-side tag filter would return nothing at all.
    """
    PushdownDoc(
        room_id="r1", doc_id="keeper", last_active_at=1.0, tag="keep", rank=0.0
    ).save()
    for i in range(40):
        PushdownDoc(
            room_id="r1",
            doc_id=f"noise{i}",
            last_active_at=100.0 + i,
            tag="drop",
            rank=0.0,
        ).save()

    results = list(
        PushdownDoc.query.filter(
            room_id="r1",
            last_active_at__gte=0,
            tag="keep",
            order_by="-last_active_at",
            limit=5,
        )
    )
    assert [d.doc_id for d in results] == ["keeper"]


def test_plain_field_filter_reads_full_range():
    _seed()
    with HydrationCounter() as counter:
        list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                tag="x",
                order_by="-last_active_at",
                limit=5,
            )
        )
    assert (
        counter.count >= POPULATION
    ), "a pending client-side filter must force the full range read"


def test_second_sorted_field_filter_disables_range_bound():
    """A second indexed predicate blocks the Redis-side bound but not the slice.

    The sorted set alone cannot honor the other index, so the range read stays
    unbounded. The key-level intersection still happens without hydrating
    anything, so the ordered list can be sliced before loading.
    """
    _seed()
    results = list(
        PushdownDoc.query.filter(
            room_id="r1",
            last_active_at__gte=0,
            rank__gte=58,
            order_by="-last_active_at",
            limit=5,
        )
    )
    # rank = POPULATION - i, so rank >= 58 keeps d0, d1, d2
    assert [d.doc_id for d in results] == ["d2", "d1", "d0"]


def test_key_list_slice_bounds_hydration_with_second_indexed_filter():
    """The pre-hydration slice is the cut that matters, and it applies here.

    An `IndexedField` predicate blocks the Redis-side bound: the sorted set
    cannot honor the other index, so the range read stays full. The
    intersection still happens at the key level though, so the ordered list
    can be sliced before a single object is loaded.
    """
    _seed()
    with HydrationCounter() as counter:
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                bucket="a",  # matches every record, so only the slice can bound it
                order_by="-last_active_at",
                limit=5,
            )
        )
    assert [d.doc_id for d in results] == ["d59", "d58", "d57", "d56", "d55"]
    assert counter.count < POPULATION, (
        f"pre-hydration slice did not fire: hydrated {counter.count} "
        f"of {POPULATION} with a second indexed filter present"
    )


def test_key_list_slice_ascending_takes_the_head():
    """Ascending and descending take opposite ends of the ordered key list."""
    _seed()
    results = list(
        PushdownDoc.query.filter(
            room_id="r1", last_active_at__gte=0, bucket="a", limit=5
        )
    )
    assert [d.doc_id for d in results] == ["d0", "d1", "d2", "d3", "d4"]


def test_key_list_slice_matches_unbounded_with_second_indexed_filter():
    """A partial-match index filter must give the same rows bounded or not."""
    for i in range(30):
        PushdownDoc(
            room_id="r1",
            doc_id=f"d{i}",
            last_active_at=float(i),
            tag="x",
            rank=0.0,
            bucket="keep" if i % 3 == 0 else "skip",
        ).save()

    everything = [
        d
        for d in PushdownDoc.query.filter(room_id="r1", last_active_at__gte=0)
        if d.bucket == "keep"
    ]
    everything.sort(key=lambda d: d.last_active_at, reverse=True)
    expected = [d.doc_id for d in everything[:4]]

    bounded = list(
        PushdownDoc.query.filter(
            room_id="r1",
            last_active_at__gte=0,
            bucket="keep",
            order_by="-last_active_at",
            limit=4,
        )
    )
    assert [d.doc_id for d in bounded] == expected


def test_order_by_other_field_disables_pushdown():
    """Score order is not result order here, so top-N by score is the wrong N."""
    _seed(count=20)
    results = list(
        PushdownDoc.query.filter(
            room_id="r1", last_active_at__gte=0, order_by="-rank", limit=3
        )
    )
    # rank = 20 - i, so the highest ranks are the lowest last_active_at values
    assert [d.doc_id for d in results] == ["d0", "d1", "d2"]


def test_q_objects_disable_pushdown():
    """Q objects union several key sets, so score order is not result order.

    The partition value rides inside the Q because a partitioned SortedField
    cannot resolve its sorted set without one.
    """
    _seed(count=20)
    with HydrationCounter() as counter:
        results = list(
            PushdownDoc.query.filter(
                Q(room_id="r1", last_active_at__gte=15),
                order_by="-last_active_at",
                limit=3,
            )
        )
    assert [d.doc_id for d in results] == ["d19", "d18", "d17"]
    assert counter.count >= 5, "Q object path must not bound the range read"


def test_no_limit_reads_full_range():
    _seed()
    results = list(PushdownDoc.query.filter(room_id="r1", last_active_at__gte=0))
    assert len(results) == POPULATION


@pytest.mark.parametrize("bad_limit", [0, -1, None, True])
def test_non_positive_int_limit_does_not_bound(bad_limit):
    """Only a positive int bounds the range read.

    ``True`` matters because bool subclasses int: without the explicit check
    it would reach Redis as ``num=1``. These values keep whatever slicing
    behavior the post-hydration path already gave them; what this asserts is
    that the range read itself stays unbounded, so nothing is lost before
    that path runs.
    """
    _seed()
    with HydrationCounter() as counter:
        list(
            PushdownDoc.query.filter(
                room_id="r1", last_active_at__gte=0, limit=bad_limit
            )
        )
    # The population is well past `limit + margin`, so a bound of any kind
    # shows up as a hydration count far below the full range.
    assert counter.count >= POPULATION, (
        f"limit={bad_limit!r} must not bound the range read, "
        f"hydrated only {counter.count}"
    )


# ---------------------------------------------------------------------------
# Stale index members
# ---------------------------------------------------------------------------


def test_stale_members_do_not_shorten_a_bounded_read():
    """Index entries outliving their hash must not eat the bound.

    ``get_many_objects`` drops keys whose hash is gone. Under a bounded read
    those dropped rows would otherwise come straight off the result count.
    """
    _seed(count=20)
    for i in (19, 18, 17):
        removed = POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")
        assert removed == 1, f"expected to orphan PushdownDoc:d{i}:r1"

    results = list(
        PushdownDoc.query.filter(
            room_id="r1", last_active_at__gte=0, order_by="-last_active_at", limit=5
        )
    )
    assert len(results) == 5
    assert [d.doc_id for d in results] == ["d16", "d15", "d14", "d13", "d12"]


def test_orphan_density_past_the_margin_warns_and_still_returns_full(caplog):
    """Past the over-fetch margin the re-read fires, and it must be audible.

    A short bounded result is a wrong answer rather than a slow one, so it
    cannot pass silently. The warning has to point at index repair, because
    re-reading tolerates orphans and only ``repair_indexes()`` removes them.
    """
    _seed(count=40)
    orphaned = list(range(39, 39 - 15, -1))
    for i in orphaned:
        removed = POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")
        assert removed == 1, f"expected to orphan PushdownDoc:d{i}:r1"

    with caplog.at_level("WARNING", logger="POPOTO.Query"):
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                order_by="-last_active_at",
                limit=5,
            )
        )

    assert len(results) == 5, "the re-read must restore a complete answer"
    assert [d.doc_id for d in results] == ["d24", "d23", "d22", "d21", "d20"]

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a short bounded read must not be silent"
    joined = "\n".join(warnings)
    assert (
        "repair_indexes" in joined
    ), f"the warning must point at index repair, got: {joined}"
    assert "PushdownDoc" in joined and "last_active_at" in joined
    assert "room_id" in joined, "the warning must name the partition"


def test_margin_absorbs_light_orphan_density_without_a_re_read(caplog):
    """Inside the margin the orphans cost nothing beyond the same round trip."""
    _seed(count=40)
    for i in (39, 38):
        POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")

    with caplog.at_level("WARNING", logger="POPOTO.Query"):
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                order_by="-last_active_at",
                limit=5,
            )
        )

    assert [d.doc_id for d in results] == ["d37", "d36", "d35", "d34", "d33"]
    re_reads = [
        r.message for r in caplog.records if "Re-reading the full range" in r.message
    ]
    assert not re_reads, f"margin should have absorbed 2 orphans, got: {re_reads}"


def test_margin_absorbs_orphans_on_the_key_list_slice_too(caplog):
    """The pre-hydration slice needs the same margin as the range read.

    The `bucket` predicate keeps the Redis-side bound off, so this exercises
    the slice path specifically.
    """
    _seed(count=40)
    for i in (39, 38):
        POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")

    with caplog.at_level("WARNING", logger="POPOTO.Query"):
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                bucket="a",
                order_by="-last_active_at",
                limit=5,
            )
        )

    assert [d.doc_id for d in results] == ["d37", "d36", "d35", "d34", "d33"]
    re_reads = [
        r.message for r in caplog.records if "Re-reading the full range" in r.message
    ]
    assert not re_reads, f"slice margin should have absorbed 2 orphans: {re_reads}"


def test_exhausted_range_short_on_orphans_still_warns(caplog):
    """Short because the range ran out is not wrong, but it is still short.

    No re-read can help here, so the only correct behavior is to say so and
    point at the index-hygiene path.
    """
    _seed(count=10)
    for i in (9, 8, 7):
        POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")

    with caplog.at_level("WARNING", logger="POPOTO.Query"):
        results = list(
            PushdownDoc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                order_by="-last_active_at",
                limit=20,
            )
        )

    assert len(results) == 7, "every surviving row must still come back"
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    joined = "\n".join(warnings)
    assert warnings, "a short result must not be silent even when unavoidable"
    assert "repair_indexes" in joined, joined
    assert (
        "Re-reading the full range" not in joined
    ), f"an exhausted range must not trigger a pointless re-read: {joined}"
