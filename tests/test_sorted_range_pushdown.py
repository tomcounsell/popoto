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

import asyncio
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto import Q
from src.popoto.fields.constants import Defaults
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module


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
    """Clear this model's keys and reset the cached async connection.

    The async reset lives here rather than in a second autouse fixture so the
    sync tests in this file keep exactly one flush around them. The async Redis
    connection is bound to an event loop and pytest-asyncio builds a fresh loop
    per test, so a leaked client raises "Future attached to a different loop".
    """
    _flush()
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()
    yield
    _flush()
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None


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
    re-reading tolerates orphans and only ``clean_indexes()`` removes them.
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
        "clean_indexes" in joined
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
    assert "clean_indexes" in joined, joined
    assert (
        "Re-reading the full range" not in joined
    ), f"an exhausted range must not trigger a pointless re-read: {joined}"


# ---------------------------------------------------------------------------
# count() must report the population, not the bounded read
# ---------------------------------------------------------------------------


def test_count_is_not_truncated_by_a_present_limit():
    """A limit bounds the rows you get back; it must not bound the tally.

    The limit has to be live for this to mean anything, so the same builder
    that reports the full count is also drained: without that, a count() that
    ignored the limit and a limit that was never applied look identical.
    """
    _seed()

    assert (
        PushdownDoc.query.count(room_id="r1", last_active_at__gte=0) == POPULATION
    ), "an unlimited count is the control for the three limited cases below"

    builder = PushdownDoc.query.filter(
        room_id="r1", last_active_at__gte=0, order_by="-last_active_at"
    ).limit(5)
    assert builder.count() == POPULATION, (
        "QueryBuilder.count() must tally the matching population, "
        "not the length of the bounded read"
    )
    assert len(list(builder)) == 5, (
        "the limit must still be live on the builder that just reported "
        f"{POPULATION}, otherwise the assertion above is vacuous"
    )

    assert (
        PushdownDoc.query.count(room_id="r1", last_active_at__gte=0, limit=5)
        == POPULATION
    ), "the kwargs form of the limit must be ignored by count() too"


# ---------------------------------------------------------------------------
# count() on the Q-object path must also ignore limit() (#610)
#
# The partition value must ride inside the Q, not as a sibling kwarg, or
# QueryException fires from sorted_field_mixin.py:760.
# ---------------------------------------------------------------------------


def test_q_count_unlimited_is_the_control():
    """Control: an unlimited Q count already reports the full population.

    If this fails, the other tests in this group are not testing what they
    claim to test.
    """
    _seed()
    assert (
        PushdownDoc.query.filter(Q(room_id="r1", last_active_at__gte=0)).count()
        == POPULATION
    )


def test_q_count_ignores_a_present_limit_descending():
    """Guard: count() must not truncate to the limit on the Q path.

    A dropped guard returns 5 (the limit) instead of POPULATION.
    """
    _seed()
    builder = PushdownDoc.query.filter(
        Q(room_id="r1", last_active_at__gte=0), order_by="-last_active_at"
    ).limit(5)
    assert builder.count() == POPULATION


def test_q_count_ignores_a_present_limit_ascending():
    """Guard: same as the descending case, with ascending order_by.

    A dropped guard returns 5 instead of POPULATION.
    """
    _seed()
    builder = PushdownDoc.query.filter(
        Q(room_id="r1", last_active_at__gte=0), order_by="last_active_at"
    ).limit(5)
    assert builder.count() == POPULATION


def test_q_count_does_not_make_the_limit_assertion_vacuous():
    """Guard: the same builder that reports the full count still yields 5 rows.

    Without this, a count() that ignores the limit and a limit that was never
    applied to all() would look identical. A dropped `all()`-side guard
    returns something other than 5 here.
    """
    _seed()
    builder = PushdownDoc.query.filter(
        Q(room_id="r1", last_active_at__gte=0), order_by="-last_active_at"
    ).limit(5)
    assert builder.count() == POPULATION
    assert len(list(builder)) == 5, (
        "the limit must still be live on the builder that just reported "
        f"{POPULATION}"
    )


def test_q_count_ignores_limit_with_computed_sort():
    """Guard: the computed_sort branch must also skip its post-sort slice.

    A dropped guard returns 5 (the computed_sort branch's own limit slice)
    instead of POPULATION.
    """
    _seed()
    builder = (
        PushdownDoc.query.filter(Q(room_id="r1", last_active_at__gte=0))
        .computed_sort(lambda d: -d.last_active_at)
        .limit(5)
    )
    assert builder.count() == POPULATION


def test_q_count_after_first_is_not_pinned_to_one():
    """Guard: first() mutates the builder's limit to 1; count() must ignore it.

    first() is limit(1).all(), and limit() mutates in place, so a later
    count() on the same builder inherits a live limit of 1 unless count()
    suppresses it. A dropped guard returns 1 instead of POPULATION.
    """
    _seed()
    builder = PushdownDoc.query.filter(Q(room_id="r1", last_active_at__gte=0))
    builder.first()
    assert builder.count() == POPULATION


class TrackedPushdownDoc(popoto.fields.access_tracker.AccessTrackerMixin, popoto.Model):
    """Small AccessTrackerMixin model, used only to pin the tracking guard.

    PushdownDoc does not mix in AccessTrackerMixin, so no other test in this
    module would catch a regression where count() starts firing on_read().
    """

    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")


def _seed_tracked(room="r1", count=POPULATION):
    docs = []
    for i in range(count):
        doc = TrackedPushdownDoc(room_id=room, doc_id=f"d{i}", last_active_at=float(i))
        doc.save()
        docs.append(doc)
    return docs


def _staged_key_lengths(docs):
    return [POPOTO_REDIS_DB.llen(doc._at_key("staged")) for doc in docs]


def test_q_count_leaves_staged_access_keys_unchanged():
    """THE TRACKING GUARD: a Q + limit count() must record no accesses.

    _fire_on_read() pipelines an RPUSH+EXPIRE per hydrated instance when
    _no_track is False. A dropped guard would turn an unbounded Q + limit
    count() into a population-scale burst of staged-access writes, changing
    every staged-access key length from 0 to something nonzero. all() on the
    same builder must still record accesses, proving the suppression is
    specific to count() and not a global tracking outage.
    """
    docs = _seed_tracked()
    try:
        builder = TrackedPushdownDoc.query.filter(
            Q(room_id="r1", last_active_at__gte=0)
        ).limit(5)

        result = builder.count()
        assert result == POPULATION

        after_count = _staged_key_lengths(docs)
        assert after_count == [0] * len(docs), (
            "count() on a Q + limit builder must not stage any access-tracker "
            f"writes, got lengths {after_count}"
        )

        list(builder)  # all() on the same builder: must still track reads
        after_all = _staged_key_lengths(docs)
        assert any(length > 0 for length in after_all), (
            "all() on the same builder must still record accesses; "
            "if it doesn't, tracking was disabled globally rather than "
            "suppressed only for count()"
        )
    finally:
        for key in POPOTO_REDIS_DB.keys("*TrackedPushdownDoc*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys("$AT:TrackedPushdownDoc:*"):
            POPOTO_REDIS_DB.delete(key)


# ---------------------------------------------------------------------------
# Meta.order_by participates in the pushdown gate
#
# Both _sorted_pushdown_args and _bound_keys_before_hydration resolve direction
# as `kwargs.get("order_by") or model._meta.order_by`, so a model that declares
# its order in Meta reaches the pushdown with no explicit order_by at the call
# site. Naming another field there must decline the bound: score order is not
# result order, and a bound spent on the wrong axis returns wrong rows.
# ---------------------------------------------------------------------------

META_POPULATION = 20


class PushdownDocMetaDesc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")
    bucket = popoto.IndexedField(type=str, null=True)

    class Meta:
        order_by = "-last_active_at"


class PushdownDocMetaAsc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")

    class Meta:
        order_by = "last_active_at"


class PushdownDocMetaOther(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    last_active_at = popoto.SortedField(type=float, partition_by="room_id")

    class Meta:
        order_by = "doc_id"


def _seed_meta(model, count=META_POPULATION, room="r1", reverse_doc_ids=False):
    """Seed a Meta-carrying model, optionally anti-correlating doc_id and score.

    ``reverse_doc_ids`` makes doc_id order the exact reverse of score order, so
    a result ordered by doc_id cannot be mistaken for one ordered by score.
    """
    for i in range(count):
        ordinal = (count - 1 - i) if reverse_doc_ids else i
        fields = dict(
            room_id=room,
            doc_id=f"m{ordinal:03d}",
            last_active_at=float(i),
        )
        if model is PushdownDocMetaDesc:
            fields["bucket"] = "a"
        model(**fields).save()


def test_meta_order_by_descending_supplies_direction_and_bound():
    """No explicit order_by: Meta supplies both the direction and the pushdown."""
    _seed_meta(PushdownDocMetaDesc)
    with HydrationCounter() as counter:
        results = list(
            PushdownDocMetaDesc.query.filter(
                room_id="r1", last_active_at__gte=0, limit=3
            )
        )

    assert [d.doc_id for d in results] == ["m019", "m018", "m017"]
    assert counter.count < 2 * META_POPULATION, (
        "Meta.order_by must reach the pushdown gate; a full read means the "
        f"Meta fallback was dropped ({counter.count} of {2 * META_POPULATION})"
    )


def test_meta_order_by_ascending_supplies_direction_and_bound():
    """Ascending Meta.order_by supplies direction and keeps the Redis-side bound.

    Weak discriminator (spike-3): with no Meta at all, sorted-set order is
    already ascending, so this case passes whether or not the Meta fallback in
    `_sorted_pushdown_args` is consulted. It survives the mutation that deletes
    `or self.model_class._meta.order_by`. Do not read a green result here as
    proof the Meta fallback works -- that guard is defended by the descending,
    other-field and key-list-slice tests. This case exists to pin that the
    ascending shape stays bounded, not to discriminate the fallback.
    """
    _seed_meta(PushdownDocMetaAsc)
    with HydrationCounter() as counter:
        results = list(
            PushdownDocMetaAsc.query.filter(
                room_id="r1", last_active_at__gte=0, limit=3
            )
        )

    assert [d.doc_id for d in results] == ["m000", "m001", "m002"]
    assert counter.count <= 2 * (
        3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN
    ), f"ascending Meta order must bound the read, got {counter.count}"
    assert counter.count < 2 * META_POPULATION


def test_meta_order_by_other_field_disables_pushdown():
    """Ordering by a non-sorted field must decline the bound.

    doc_id order is the reverse of score order here, so the correct head is the
    score-order tail. A bound spent on the score axis would return the other end
    of the range entirely.
    """
    _seed_meta(PushdownDocMetaOther, reverse_doc_ids=True)
    with HydrationCounter() as counter:
        results = list(
            PushdownDocMetaOther.query.filter(
                room_id="r1", last_active_at__gte=0, limit=3
            )
        )

    assert [d.doc_id for d in results] == ["m000", "m001", "m002"]
    assert counter.count >= 2 * META_POPULATION, (
        "ordering by another field must read the full range before slicing, "
        f"got {counter.count}"
    )
    assert counter.count > 2 * (3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN)


def test_meta_order_by_supplies_direction_to_the_key_list_slice():
    """The second indexed predicate declines the Redis-side bound.

    That leaves the pre-hydration key-list slice as the only thing bounding the
    read, and it resolves direction from Meta at query.py:2329. Dropping that
    resolution returns the ascending head re-sorted -- the wrong three rows,
    silently.
    """
    _seed_meta(PushdownDocMetaDesc)

    with HydrationCounter() as counter:
        results = list(
            PushdownDocMetaDesc.query.filter(
                room_id="r1",
                last_active_at__gte=0,
                bucket="a",
                limit=3,
            )
        )

    assert [d.doc_id for d in results] == ["m019", "m018", "m017"]
    assert (
        counter.count < 2 * META_POPULATION
    ), f"the key-list slice must still bound hydration, got {counter.count}"


# ---------------------------------------------------------------------------
# Async parity: async_filter must apply the same bounds as _execute_filter
#
# Two different clients are in play and patching the wrong one gives a vacuous
# assertion. Hydration runs on the async pipeline (Query._async_get_many_objects),
# so hydration counts must patch redis.asyncio.client.Pipeline. The sorted range
# read runs on the sync client inside to_thread, so direction assertions must
# patch redis.client.Redis.
# ---------------------------------------------------------------------------


class AsyncHydrationCounter:
    """Count async hydration commands, one per object actually loaded.

    Patches both ``hgetall`` and ``hmget`` on the async pipeline and sums them:
    ``_async_get_many_objects`` hydrates with ``hmget`` when ``values=`` is
    passed and ``hgetall`` otherwise, so counting only one of the two reads 0
    for half the query shapes. Counting both keeps the counter honest if a
    ``values=`` case is added later.
    """

    def __enter__(self):
        import redis.asyncio.client as async_client

        self.count = 0
        self._orig_hgetall = async_client.Pipeline.hgetall
        self._orig_hmget = async_client.Pipeline.hmget
        counter = self

        def hgetall(pipeline_self, name):
            counter.count += 1
            return counter._orig_hgetall(pipeline_self, name)

        def hmget(pipeline_self, name, keys, *args):
            counter.count += 1
            return counter._orig_hmget(pipeline_self, name, keys, *args)

        async_client.Pipeline.hgetall = hgetall
        async_client.Pipeline.hmget = hmget
        return self

    def __exit__(self, *exc):
        import redis.asyncio.client as async_client

        async_client.Pipeline.hgetall = self._orig_hgetall
        async_client.Pipeline.hmget = self._orig_hmget


class RangeCallRecorder:
    """Record the sorted-range reads issued on the sync client."""

    def __enter__(self):
        import redis

        self.calls = []
        self._orig_asc = redis.client.Redis.zrangebyscore
        self._orig_desc = redis.client.Redis.zrevrangebyscore
        recorder = self

        def zrangebyscore(client_self, name, *args, **kwargs):
            recorder.calls.append(("zrangebyscore", name, kwargs.get("num")))
            return recorder._orig_asc(client_self, name, *args, **kwargs)

        def zrevrangebyscore(client_self, name, *args, **kwargs):
            recorder.calls.append(("zrevrangebyscore", name, kwargs.get("num")))
            return recorder._orig_desc(client_self, name, *args, **kwargs)

        redis.client.Redis.zrangebyscore = zrangebyscore
        redis.client.Redis.zrevrangebyscore = zrevrangebyscore
        return self

    def __exit__(self, *exc):
        import redis

        redis.client.Redis.zrangebyscore = self._orig_asc
        redis.client.Redis.zrevrangebyscore = self._orig_desc

    def for_field(self, field_name):
        return [c for c in self.calls if field_name in str(c[1])]


@pytest.mark.asyncio
async def test_async_bounded_query_hydrates_only_limit():
    """The headline defect: async_filter hydrated the whole range.

    Both bounds are asserted. Without the lower bound a counter that observes
    nothing at all -- the trap of patching the sync pipeline -- would pass.
    """
    _seed()
    with AsyncHydrationCounter() as counter:
        results = await PushdownDoc.query.async_filter(
            room_id="r1",
            last_active_at__gte=0,
            order_by="-last_active_at",
            limit=5,
        )

    assert [d.doc_id for d in results] == ["d59", "d58", "d57", "d56", "d55"]
    assert 5 <= counter.count <= 5 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN, (
        f"async hydration must be bounded by limit + margin, "
        f"got {counter.count} of a {POPULATION}-row population"
    )


@pytest.mark.asyncio
async def test_async_range_read_is_bounded_and_direction_correct():
    """A descending async query must issue ZREVRANGEBYSCORE, with a num bound."""
    _seed()
    with RangeCallRecorder() as recorder:
        await PushdownDoc.query.async_filter(
            room_id="r1",
            last_active_at__gte=0,
            order_by="-last_active_at",
            limit=5,
        )
    calls = recorder.for_field("last_active_at")
    assert calls, "the query must read the sorted range"
    assert all(
        c[0] == "zrevrangebyscore" for c in calls
    ), f"a descending query must not read ascending: {calls}"
    assert all(
        c[2] == 5 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN for c in calls
    ), f"the range read must carry the bound: {calls}"

    with RangeCallRecorder() as recorder:
        await PushdownDoc.query.async_filter(
            room_id="r1",
            last_active_at__gte=0,
            order_by="last_active_at",
            limit=5,
        )
    calls = recorder.for_field("last_active_at")
    assert calls and all(
        c[0] == "zrangebyscore" for c in calls
    ), f"an ascending query must read ascending: {calls}"


@pytest.mark.asyncio
async def test_async_and_sync_agree_across_limits_and_directions():
    """Parity is the whole point: the bound may not change the answer."""
    _seed()
    for order_by in ("last_active_at", "-last_active_at"):
        for limit in (1, 5, 17, POPULATION + 10):
            sync_rows = [
                d.doc_id
                for d in PushdownDoc.query.filter(
                    room_id="r1",
                    last_active_at__gte=0,
                    order_by=order_by,
                    limit=limit,
                )
            ]
            async_rows = [
                d.doc_id
                for d in await PushdownDoc.query.async_filter(
                    room_id="r1",
                    last_active_at__gte=0,
                    order_by=order_by,
                    limit=limit,
                )
            ]
            assert async_rows == sync_rows, f"{order_by} limit={limit}"


@pytest.mark.asyncio
async def test_async_orphan_density_re_reads_and_returns_full(caplog):
    """Orphans past the margin must force a full re-read, not a short answer.

    The retry passes _allow_pushdown=False; hardcoding True there would
    re-apply the bound and return the same short result.
    """
    _seed(count=40)
    for i in range(39, 39 - 15, -1):
        removed = POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:r1")
        assert removed == 1, f"expected to orphan PushdownDoc:d{i}:r1"

    with caplog.at_level("WARNING", logger="POPOTO.Query"):
        results = await PushdownDoc.query.async_filter(
            room_id="r1",
            last_active_at__gte=0,
            order_by="-last_active_at",
            limit=5,
        )

    assert len(results) == 5, "the re-read must restore a complete answer"
    assert [d.doc_id for d in results] == ["d24", "d23", "d22", "d21", "d20"]
    joined = "\n".join(r.message for r in caplog.records if r.levelname == "WARNING")
    assert "Re-reading the full range" in joined, joined
    assert "clean_indexes" in joined, joined


@pytest.mark.asyncio
async def test_async_pending_client_filter_suppresses_the_bound():
    """A pending plain-field filter must see every candidate before truncation.

    `tag` is an unindexed plain Field, so it is filtered client-side after
    hydration. Bounding before that filter runs would cut rows it would keep.
    """
    for i in range(30):
        PushdownDoc(
            room_id="r2",
            doc_id=f"c{i}",
            last_active_at=float(i),
            tag="hit" if i < 3 else "miss",
            rank=0.0,
            bucket="a",
        ).save()

    results = await PushdownDoc.query.async_filter(
        room_id="r2",
        last_active_at__gte=0,
        tag="hit",
        order_by="-last_active_at",
        limit=5,
    )
    assert sorted(d.doc_id for d in results) == ["c0", "c1", "c2"], (
        "the client-side filter must run over the full candidate set, "
        f"got {[d.doc_id for d in results]}"
    )


@pytest.mark.asyncio
async def test_concurrent_async_filters_do_not_clobber_each_other():
    """Query is one instance per model class, shared by every coroutine.

    Reading self._pushdown_* after an await lets a second coroutine's
    filter_for_keys_set overwrite the first one's bookkeeping: the bound goes
    missing, the re-read is skipped, and the caller silently gets short results.
    Orphans are seeded so the short-result guard is live in every coroutine.
    """
    rooms = {"a": 40, "b": 30, "c": 50, "d": 35}
    for room, count in rooms.items():
        _seed(room=room, count=count)
        for i in range(count - 1, count - 12, -1):
            POPOTO_REDIS_DB.delete(f"PushdownDoc:d{i}:{room}")

    async def one(room, count, limit):
        rows = await PushdownDoc.query.async_filter(
            room_id=room,
            last_active_at__gte=0,
            order_by="-last_active_at",
            limit=limit,
        )
        top = count - 12  # the highest doc index that still has a hash
        expected = [f"d{i}" for i in range(top, top - limit, -1)]
        return room, [d.doc_id for d in rows], expected

    for _ in range(10):
        outcomes = await asyncio.gather(
            *(
                one(room, count, limit)
                for (room, count), limit in zip(rooms.items(), (5, 7, 4, 6))
            )
        )
        for room, got, expected in outcomes:
            assert got == expected, f"room {room}: got {got}, expected {expected}"


# ---------------------------------------------------------------------------
# Async Meta.order_by: #602 armed the async path through the same shared
# helpers, so the Meta fallback gates the async read too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_meta_order_by_descending_supplies_direction_and_bound():
    """Async: descending Meta.order_by supplies direction and the Redis bound.

    Guards the shared `_sorted_pushdown_args` Meta fallback on the async path.
    Drop that fallback and the async read loses its direction: it returns the
    ascending head (m000..m002) instead of the descending head, and hydrates
    the whole partition rather than the bounded window.
    """
    _seed_meta(PushdownDocMetaDesc)
    with AsyncHydrationCounter() as counter:
        results = await PushdownDocMetaDesc.query.async_filter(
            room_id="r1", last_active_at__gte=0, limit=3
        )

    assert [d.doc_id for d in results] == ["m019", "m018", "m017"]
    assert 3 <= counter.count <= 3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN, (
        "the async read must take its direction and bound from Meta, "
        f"got {counter.count} of a {META_POPULATION}-row population"
    )


@pytest.mark.asyncio
async def test_async_meta_order_by_other_field_disables_pushdown():
    """Async: Meta.order_by on a non-sorted field must decline the bound.

    Score order is not result order here (doc_id is anti-correlated with score),
    so a bound spent on the score axis would return the wrong end of the range.
    Drop the guard that returns early when the resolved order_by name is not the
    sorted field and this silently returns m019..m017 instead of m000..m002.
    """
    _seed_meta(PushdownDocMetaOther, reverse_doc_ids=True)
    with AsyncHydrationCounter() as counter:
        results = await PushdownDocMetaOther.query.async_filter(
            room_id="r1", last_active_at__gte=0, limit=3
        )

    assert [d.doc_id for d in results] == ["m000", "m001", "m002"]
    assert counter.count >= META_POPULATION, (
        "ordering by another field must read the full range on the async path "
        f"too, got {counter.count}"
    )
    assert counter.count > 3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN


@pytest.mark.asyncio
async def test_async_meta_order_by_supplies_direction_to_the_key_list_slice():
    """Async twin of the sync key-list-slice case: bucket declines the Redis bound."""
    _seed_meta(PushdownDocMetaDesc)
    with AsyncHydrationCounter() as counter:
        results = await PushdownDocMetaDesc.query.async_filter(
            room_id="r1", last_active_at__gte=0, bucket="a", limit=3
        )

    assert [d.doc_id for d in results] == ["m019", "m018", "m017"]
    assert (
        counter.count < META_POPULATION
    ), f"the key-list slice must still bound async hydration, got {counter.count}"
