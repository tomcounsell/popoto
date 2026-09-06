"""
Thread-safety tests for the per-thread pushdown bookkeeping on ``Query`` (#600).

``Query`` is instantiated once per model class (``models/base.py``), so every
thread that calls ``Model.query.filter(...)`` shares one ``self``.
``filter_for_keys_set`` resets and repopulates eight bookkeeping attributes on
that shared object with blocking Redis calls in between, so two threads
querying different partitions could return each other's rows. ``_PerThreadAttr``
(see ``src/popoto/models/query.py``) backs those eight names with a
``threading.local`` cell per ``Query`` instance to fix that.

These tests exercise:
    A. Deterministic per-thread isolation via a barrier (no timing knob).
    B. A fresh thread reads descriptor defaults, not another thread's leftovers,
       and the ``{}`` defaults are not shared objects across threads.
    C. The exception path still disarms ``_pushdown_allowed``.
    D. The regression itself: real concurrent filter() calls under a tight
       switch interval must never clobber each other's keys or partitions.
    E. ``async_count`` must read the client-filter carrier, not the event-loop
       thread's (empty) per-thread cell.

Also (#640): ``_geo_distances`` / ``_geo_distance_unit`` are two more names
that were shared instance state on ``Query`` -- reset / mutate / read-back
around blocking geo queries, exactly like the pushdown names above, but never
folded into ``_PerThreadAttr`` (the async loop-thread / to_thread-worker split
would silently drop every async geo distance if they were). These tests
exercise:
    F. Stochastic concurrent geo filter() calls, each with its own center,
       unit and expected distance, asserting the per-row ``_geo_distance`` /
       ``_geo_distance_unit`` -- never just row identity.
    G. A deterministic two-thread version using a monkeypatch hook instead of
       a timing knob, so a green run does not depend on the scheduler.
    H. An async geo query attaches distances -- the test that fails loudly if
       the carrier is ever "simplified" back onto ``_PerThreadAttr``.
    I. A Q-object + geo query attaches distances -- the Q leaf must route
       into the same carrier the caller reads back from.
"""

import math
import os
import sys
import threading

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.q import Q
from src.popoto.models.query import Query as QueryClass
from src.popoto.models.query import QueryException
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module
import asyncio

# Same earth-radius constant Redis's GEO commands use (geohash-int.c), so a
# distance computed here from fixture coordinates matches what Redis returns
# to well under our tolerance -- this is not an independent approximation.
_GEO_EARTH_RADIUS_KM = 6372.797560856
_KM_TO_MI = 0.621371192


def _lat_offset_deg(distance_km):
    """Degrees of latitude that put a point ``distance_km`` due north.

    A meridian is a great circle, so the geodesic distance between two points
    that differ only in latitude is exactly ``R * radians(delta_lat)`` -- no
    approximation beyond the spherical-earth model Redis itself uses.
    """
    return math.degrees(distance_km / _GEO_EARTH_RADIUS_KM)


def _expected_km(offset_km, unit, is_anchor):
    if is_anchor:
        return 0.0
    return offset_km if unit == "km" else offset_km * _KM_TO_MI


def _tolerance(unit):
    return 0.05 if unit == "km" else 0.05 * _KM_TO_MI


class ThreadSafeDoc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    score = popoto.SortedField(type=float, partition_by="room_id")
    bucket = popoto.IndexedField(type=str, null=True)
    tag = popoto.Field(type=str, null=True)


class ConcurrentDoc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    score = popoto.SortedField(type=float, partition_by="room_id")
    bucket = popoto.IndexedField(type=str, null=True)
    tag = popoto.Field(type=str, null=True)


class AsyncCountDoc(popoto.Model):
    doc_id = popoto.KeyField(type=str)
    bucket = popoto.IndexedField(type=str, null=True)
    label = popoto.Field(type=str, null=True)


class GeoRaceDoc(popoto.Model):
    place_id = popoto.KeyField(type=str)
    coordinates = popoto.GeoField()
    bucket = popoto.IndexedField(type=str, null=True)


@pytest.fixture(autouse=True)
def clean_docs():
    """Flush this file's models and reset the cached async connection.

    The async reset lives here (rather than a second autouse fixture) so the
    sync tests keep exactly one flush around them, matching the convention in
    tests/test_sorted_range_pushdown.py. The async Redis connection is bound
    to an event loop and pytest-asyncio builds a fresh loop per test, so a
    leaked client raises "Future attached to a different loop".
    """
    _flush()
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()
    yield
    _flush()
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None


def _flush():
    for pattern in (
        "*ThreadSafeDoc*",
        "*ConcurrentDoc*",
        "*AsyncCountDoc*",
        "*GeoRaceDoc*",
    ):
        for key in POPOTO_REDIS_DB.keys(pattern):
            POPOTO_REDIS_DB.delete(key)


def _seed(model, room, count, tag="x", bucket="a"):
    for i in range(count):
        model(
            room_id=room,
            doc_id=f"d{i:03d}",
            score=float(i),
            tag=tag,
            bucket=bucket,
        ).save()


# ---------------------------------------------------------------------------
# A. Deterministic per-thread isolation (no timing knob)
# ---------------------------------------------------------------------------


def test_deterministic_two_thread_isolation():
    """Two threads write, meet at a barrier, then read -- each must only ever
    see its own value.

    With a shared (non-per-thread) attribute, whichever thread writes last
    "wins": after both writes complete and the barrier releases, both threads
    read the same surviving value, so at least one of them observes a value it
    did not write. That failure is deterministic -- it does not depend on
    catching a narrow timing window.
    """
    barrier = threading.Barrier(2)
    errors = []
    lock = threading.Lock()

    def run(filters_value, allowed_value):
        try:
            ThreadSafeDoc.query._pending_client_filters = {"who": filters_value}
            ThreadSafeDoc.query._pushdown_allowed = allowed_value
            barrier.wait(timeout=10)
            got_filters = ThreadSafeDoc.query._pending_client_filters
            got_allowed = ThreadSafeDoc.query._pushdown_allowed
            if got_filters != {"who": filters_value}:
                with lock:
                    errors.append(
                        f"filters clobbered: wrote {{'who': {filters_value!r}}}, "
                        f"read back {got_filters!r}"
                    )
            if got_allowed != allowed_value:
                with lock:
                    errors.append(
                        f"pushdown_allowed clobbered: wrote {allowed_value!r}, "
                        f"read back {got_allowed!r}"
                    )
        except Exception as exc:  # pragma: no cover - surfaced via errors
            with lock:
                errors.append(f"unexpected exception: {exc!r}")

    t1 = threading.Thread(target=run, args=("A", True))
    t2 = threading.Thread(target=run, args=("B", False))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# B. Fresh thread reads descriptor defaults, not another thread's leftovers
# ---------------------------------------------------------------------------


def test_fresh_thread_reads_defaults_not_leftovers():
    _seed(ThreadSafeDoc, room="leftover", count=10)

    # A real query on the main thread, so there ARE leftovers to leak: a plain
    # (unindexed) field filter populates _pending_client_filters, and the
    # sorted-field range read populates _sorted_field_order / _sorted_field_name.
    list(
        ThreadSafeDoc.query.filter(
            room_id="leftover",
            score__gte=0,
            tag="keep",
            order_by="-score",
            limit=3,
        )
    )
    assert ThreadSafeDoc.query._pending_client_filters == {"tag": "keep"}
    assert ThreadSafeDoc.query._sorted_field_order is not None

    captured = {}

    def fresh_thread():
        captured["sorted_field_order"] = ThreadSafeDoc.query._sorted_field_order
        captured["sorted_field_name"] = ThreadSafeDoc.query._sorted_field_name
        captured["pending_client_filters"] = dict(
            ThreadSafeDoc.query._pending_client_filters
        )
        captured["pushdown_limit"] = ThreadSafeDoc.query._pushdown_limit
        captured["pushdown_requested"] = ThreadSafeDoc.query._pushdown_requested
        captured["pushdown_fetched"] = ThreadSafeDoc.query._pushdown_fetched
        captured["pushdown_partition"] = dict(ThreadSafeDoc.query._pushdown_partition)
        captured["pushdown_allowed"] = ThreadSafeDoc.query._pushdown_allowed
        # Mutate this thread's dict defaults; must not be visible elsewhere.
        ThreadSafeDoc.query._pending_client_filters["leaked"] = True
        ThreadSafeDoc.query._pushdown_partition["leaked"] = True

    t = threading.Thread(target=fresh_thread)
    t.start()
    t.join(timeout=15)

    assert captured["sorted_field_order"] is None
    assert captured["sorted_field_name"] is None
    assert captured["pending_client_filters"] == {}
    assert captured["pushdown_limit"] is None
    assert captured["pushdown_requested"] == 0
    assert captured["pushdown_fetched"] == 0
    assert captured["pushdown_partition"] == {}
    assert captured["pushdown_allowed"] is False

    # The main thread's own leftover state must be untouched by the fresh
    # thread's mutation of its own default dict.
    assert "leaked" not in ThreadSafeDoc.query._pending_client_filters
    assert ThreadSafeDoc.query._pending_client_filters == {"tag": "keep"}

    # A second, independent fresh thread must get its own fresh default too --
    # the factory produces a new dict per thread, never a shared one.
    captured2 = {}

    def fresh_thread_2():
        captured2["pending_client_filters"] = dict(
            ThreadSafeDoc.query._pending_client_filters
        )
        captured2["pushdown_partition"] = dict(ThreadSafeDoc.query._pushdown_partition)

    t2 = threading.Thread(target=fresh_thread_2)
    t2.start()
    t2.join(timeout=15)

    assert captured2["pending_client_filters"] == {}
    assert captured2["pushdown_partition"] == {}


# ---------------------------------------------------------------------------
# C. Exception path leaves the arm flag disarmed
# ---------------------------------------------------------------------------


def test_exception_path_disarms_pushdown_flag():
    with pytest.raises(QueryException):
        list(ThreadSafeDoc.query.filter(bogus_param=1))
    assert ThreadSafeDoc.query._pushdown_allowed is False


# ---------------------------------------------------------------------------
# D. The regression test: concurrent sync filter() calls must not clobber
# each other's keys or partitions.
# ---------------------------------------------------------------------------


def test_concurrent_sync_filters_do_not_clobber_each_other():
    """Real concurrent filter() calls across partitions, under contention.

    Three ingredients open the race window and make it observable:

    1. A partitioned SortedField (``score``) plus a second IndexedField
       (``bucket``) that every query also filters on -- the second predicate
       forces a Redis round trip between the sorted-field populate and the
       later read of that bookkeeping, which is exactly the window a shared
       (non-per-thread) attribute would let another thread's call land in.
    2. ``sys.setswitchinterval`` cranked way down, so the interpreter yields
       the GIL far more often and interleaving is not a rare accident.
    3. Multiple partitions, two threads per partition, a distinct ``limit``
       per thread and mixed ascending/descending ``order_by``, run for many
       iterations.

    Every iteration checks two things per thread: the returned doc ids equal
    that thread's expected window, AND every returned row's ``room_id`` equals
    the partition that thread queried. The partition check is mandatory on its
    own -- an observed failure of this bug had entirely correct doc ids and
    entirely wrong partition values, which an id-only assertion would have
    missed.
    """
    # (room, population, limit, order_by, use_plain_field_filter)
    configs = [
        ("p0", 50, 5, "-score", False),
        ("p0", 50, 8, "score", False),
        ("p1", 60, 6, "-score", False),
        ("p1", 60, 9, "score", False),
        ("p2", 70, 7, "-score", False),
        ("p2", 70, 10, "score", False),
        ("p3", 55, 4, "-score", True),
        ("p3", 55, 11, "score", False),
    ]
    limits = [c[2] for c in configs]
    assert len(set(limits)) == len(limits), "every thread needs a distinct limit"

    seeded = {}
    for room, count, *_ in configs:
        if room not in seeded:
            _seed(ConcurrentDoc, room=room, count=count, tag="keep", bucket="a")
            seeded[room] = count

    iterations = 60
    errors = []
    errors_lock = threading.Lock()

    def worker(room, count, limit, order_by, use_tag):
        desc = order_by.startswith("-")
        if desc:
            expected = [f"d{i:03d}" for i in range(count - 1, count - 1 - limit, -1)]
        else:
            expected = [f"d{i:03d}" for i in range(0, limit)]

        filter_kwargs = dict(
            room_id=room,
            score__gte=0,
            bucket="a",
            order_by=order_by,
            limit=limit,
        )
        if use_tag:
            filter_kwargs["tag"] = "keep"

        for _ in range(iterations):
            try:
                rows = list(ConcurrentDoc.query.filter(**filter_kwargs))
            except Exception as exc:  # pragma: no cover - surfaced via errors
                with errors_lock:
                    errors.append(f"room={room} limit={limit}: exception {exc!r}")
                continue

            got_ids = [r.doc_id for r in rows]
            if got_ids != expected:
                with errors_lock:
                    errors.append(
                        f"room={room} limit={limit} order_by={order_by}: "
                        f"got doc ids {got_ids}, expected {expected}"
                    )
                continue

            bad_partition = [(r.doc_id, r.room_id) for r in rows if r.room_id != room]
            if bad_partition:
                with errors_lock:
                    errors.append(
                        f"room={room} limit={limit} order_by={order_by}: "
                        f"rows from the wrong partition leaked in: {bad_partition}"
                    )

    orig_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker, args=cfg) for cfg in configs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        sys.setswitchinterval(orig_interval)

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# E. async_count must read the client-filter carrier, not the event-loop
# thread's own (empty) per-thread cell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_count_reports_filtered_not_total():
    """Before the fix this crossed a thread boundary and returned the total.

    The key query runs in a ``to_thread`` worker, so reading
    ``self._pending_client_filters`` back on the event-loop thread sees that
    thread's own (empty) per-thread default, not the worker thread's populated
    value -- silently returning the unfiltered key count instead of the
    client-filtered one.
    """
    for i in range(5):
        AsyncCountDoc(doc_id=f"x{i:03d}", bucket="a", label="x").save()
    for i in range(3):
        AsyncCountDoc(doc_id=f"y{i:03d}", bucket="a", label="y").save()

    count = await AsyncCountDoc.query.async_count(label="x")
    assert count == 5, f"expected the filtered count (5), got {count}"


# ---------------------------------------------------------------------------
# F. The geo regression itself: stochastic concurrent geo filter() calls
# must each attach their own distances, never another query's.
# ---------------------------------------------------------------------------


def test_concurrent_geo_filters_attach_each_querys_own_distances():
    """Real concurrent geo filter() calls, each with a distinct center.

    Four threads, each anchored at a different latitude (2200+ km apart, far
    beyond any query radius here, so no thread's rows can genuinely overlap
    another's), each also filtering on its own ``bucket`` -- a second indexed
    predicate that forces a Redis round trip between the geo populate and the
    later distance read-back, which is exactly the window a shared (not
    per-call) ``_geo_distances`` / ``_geo_distance_unit`` would let another
    thread's reset or update land in.

    Threads alternate distance units (km / mi) so the single shared
    ``_geo_distance_unit`` string -- if it survives as shared state -- gets
    clobbered deterministically rather than only when two same-unit threads
    race. Each thread also gets a distinct expected offset distance, so a row
    that picks up another thread's distance value fails the tolerance check
    rather than coincidentally matching.

    The rows themselves would still be correct under this bug (right doc,
    right partition) -- only the ``_geo_distance`` / ``_geo_distance_unit``
    annotation would be wrong or missing, so every assertion here is against
    those two attributes, never row identity alone.
    """
    configs = []
    for i in range(4):
        configs.append(
            dict(
                idx=i,
                lat=i * 20 - 30,
                unit="km" if i % 2 == 0 else "mi",
                offset_km=(i + 1) * 3,
                bucket=f"race{i}",
            )
        )

    for cfg in configs:
        far_delta = _lat_offset_deg(cfg["offset_km"])
        GeoRaceDoc(
            place_id=f"race{cfg['idx']}_anchor",
            coordinates=popoto.GeoField.Coordinates(
                latitude=cfg["lat"], longitude=10.0
            ),
            bucket=cfg["bucket"],
        ).save()
        GeoRaceDoc(
            place_id=f"race{cfg['idx']}_far",
            coordinates=popoto.GeoField.Coordinates(
                latitude=cfg["lat"] + far_delta, longitude=10.0
            ),
            bucket=cfg["bucket"],
        ).save()

    iterations = 50
    errors = []
    errors_lock = threading.Lock()

    def worker(cfg):
        tol = _tolerance(cfg["unit"])
        for _ in range(iterations):
            try:
                rows = list(
                    GeoRaceDoc.query.filter(
                        coordinates=(cfg["lat"], 10.0),
                        coordinates_radius=20,
                        coordinates_radius_unit=cfg["unit"],
                        coordinates_with_distances=True,
                        bucket=cfg["bucket"],
                    )
                )
            except Exception as exc:  # pragma: no cover - surfaced via errors
                with errors_lock:
                    errors.append(f"idx={cfg['idx']}: exception {exc!r}")
                continue

            if len(rows) != 2:
                with errors_lock:
                    errors.append(f"idx={cfg['idx']}: expected 2 rows, got {len(rows)}")
                continue

            for row in rows:
                if not hasattr(row, "_geo_distance"):
                    with errors_lock:
                        errors.append(
                            f"idx={cfg['idx']} place_id={row.place_id}: "
                            "missing _geo_distance"
                        )
                    continue
                if row._geo_distance_unit != cfg["unit"]:
                    with errors_lock:
                        errors.append(
                            f"idx={cfg['idx']} place_id={row.place_id}: "
                            f"unit={row._geo_distance_unit!r}, expected "
                            f"{cfg['unit']!r}"
                        )
                is_anchor = row.place_id.endswith("_anchor")
                expected = _expected_km(cfg["offset_km"], cfg["unit"], is_anchor)
                if abs(row._geo_distance - expected) > tol:
                    with errors_lock:
                        errors.append(
                            f"idx={cfg['idx']} place_id={row.place_id}: "
                            f"distance={row._geo_distance}, expected "
                            f"{expected} (tolerance {tol})"
                        )

    orig_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker, args=(cfg,)) for cfg in configs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        sys.setswitchinterval(orig_interval)

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# G. Deterministic two-thread geo isolation, forced by a monkeypatch hook
# instead of a timing knob.
# ---------------------------------------------------------------------------


def test_deterministic_geo_distance_isolation():
    """Force thread B's entire filter() to run inside thread A's window.

    ``_filter_keys_with_pushdown`` is the seam both the pre-fix and post-fix
    code share: it is the point where a thread's own geo key-query and
    distance bookkeeping are already complete, but before that thread's
    ``_execute_filter`` hydrates objects and reads the distances back. On the
    pre-fix code that read-back is off the shared ``self._geo_distances`` /
    ``self._geo_distance_unit``, so forcing thread B's *entire* filter() call
    (reset, query, hydrate, read-back) to run inside that window reliably
    wipes thread A's entries before A reads them -- A's rows come back with
    no ``_geo_distance`` at all. On the post-fix code A's distances live in
    a carrier private to A's call, so B's run cannot touch them.
    """
    lat_a, lat_b = -40.0, 40.0
    offset_a_km, offset_b_km = 5.0, 9.0
    unit_a, unit_b = "km", "km"

    GeoRaceDoc(
        place_id="detA_anchor",
        coordinates=popoto.GeoField.Coordinates(latitude=lat_a, longitude=10.0),
        bucket="detA",
    ).save()
    GeoRaceDoc(
        place_id="detA_far",
        coordinates=popoto.GeoField.Coordinates(
            latitude=lat_a + _lat_offset_deg(offset_a_km), longitude=10.0
        ),
        bucket="detA",
    ).save()
    GeoRaceDoc(
        place_id="detB_anchor",
        coordinates=popoto.GeoField.Coordinates(latitude=lat_b, longitude=10.0),
        bucket="detB",
    ).save()
    GeoRaceDoc(
        place_id="detB_far",
        coordinates=popoto.GeoField.Coordinates(
            latitude=lat_b + _lat_offset_deg(offset_b_km), longitude=10.0
        ),
        bucket="detB",
    ).save()

    a_ready = threading.Event()
    b_done = threading.Event()
    errors = []
    orig_fkwp = QueryClass._filter_keys_with_pushdown

    def patched(self, allow_pushdown, kwargs):
        result = orig_fkwp(self, allow_pushdown, kwargs)
        if threading.current_thread().name == "GeoDetA":
            a_ready.set()
            if not b_done.wait(timeout=10):
                errors.append("thread B did not complete inside A's window")
        return result

    resultsA, resultsB = [], []

    def run_a():
        threading.current_thread().name = "GeoDetA"
        rows = list(
            GeoRaceDoc.query.filter(
                coordinates=(lat_a, 10.0),
                coordinates_radius=20,
                coordinates_radius_unit=unit_a,
                coordinates_with_distances=True,
                bucket="detA",
            )
        )
        resultsA.extend(rows)

    def run_b():
        if not a_ready.wait(timeout=10):
            errors.append("thread A never reached its window")
            b_done.set()
            return
        threading.current_thread().name = "GeoDetB"
        rows = list(
            GeoRaceDoc.query.filter(
                coordinates=(lat_b, 10.0),
                coordinates_radius=20,
                coordinates_radius_unit=unit_b,
                coordinates_with_distances=True,
                bucket="detB",
            )
        )
        resultsB.extend(rows)
        b_done.set()

    QueryClass._filter_keys_with_pushdown = patched
    try:
        ta = threading.Thread(target=run_a)
        tb = threading.Thread(target=run_b)
        ta.start()
        tb.start()
        ta.join(timeout=30)
        tb.join(timeout=30)
    finally:
        QueryClass._filter_keys_with_pushdown = orig_fkwp

    assert not errors, "\n".join(errors)
    assert len(resultsA) == 2, f"thread A expected 2 rows, got {len(resultsA)}"
    assert len(resultsB) == 2, f"thread B expected 2 rows, got {len(resultsB)}"

    for row in resultsA:
        assert hasattr(
            row, "_geo_distance"
        ), f"A row {row.place_id} missing _geo_distance"
        assert row._geo_distance_unit == unit_a
        expected = _expected_km(offset_a_km, unit_a, row.place_id.endswith("_anchor"))
        assert abs(row._geo_distance - expected) < _tolerance(unit_a), (
            f"A row {row.place_id}: distance={row._geo_distance}, "
            f"expected {expected}"
        )

    for row in resultsB:
        assert hasattr(
            row, "_geo_distance"
        ), f"B row {row.place_id} missing _geo_distance"
        assert row._geo_distance_unit == unit_b
        expected = _expected_km(offset_b_km, unit_b, row.place_id.endswith("_anchor"))
        assert abs(row._geo_distance - expected) < _tolerance(unit_b), (
            f"B row {row.place_id}: distance={row._geo_distance}, "
            f"expected {expected}"
        )


# ---------------------------------------------------------------------------
# H. Async geo query must attach distances -- fails loudly if the carrier is
# ever "simplified" back onto _PerThreadAttr (loop thread vs to_thread worker
# would split the write from the read).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_geo_filter_attaches_distances():
    lat = 25.0
    offset_km = 6.0

    GeoRaceDoc(
        place_id="asyncA_anchor",
        coordinates=popoto.GeoField.Coordinates(latitude=lat, longitude=10.0),
        bucket="asyncA",
    ).save()
    GeoRaceDoc(
        place_id="asyncA_far",
        coordinates=popoto.GeoField.Coordinates(
            latitude=lat + _lat_offset_deg(offset_km), longitude=10.0
        ),
        bucket="asyncA",
    ).save()

    rows = await GeoRaceDoc.query.async_filter(
        coordinates=(lat, 10.0),
        coordinates_radius=20,
        coordinates_radius_unit="km",
        coordinates_with_distances=True,
        bucket="asyncA",
    )

    assert len(rows) == 2
    for row in rows:
        assert hasattr(row, "_geo_distance"), f"{row.place_id} missing _geo_distance"
        assert row._geo_distance_unit == "km"
        expected = _expected_km(offset_km, "km", row.place_id.endswith("_anchor"))
        assert abs(row._geo_distance - expected) < _tolerance(
            "km"
        ), f"{row.place_id}: distance={row._geo_distance}, expected {expected}"


@pytest.mark.asyncio
async def test_async_geo_filter_concurrent_gather_do_not_clobber_each_other():
    """``asyncio.gather`` over distinct centers, each on its own unit.

    ``async_filter`` runs its key query in a ``to_thread`` worker while the
    caller stays on the event-loop thread, so a shared (not per-call)
    ``_geo_distances`` / ``_geo_distance_unit`` can be reset or overwritten by
    another coroutine's call between this coroutine's write and its
    read-back, exactly as the sync stochastic test exercises via real OS
    threads.
    """
    configs = []
    for i in range(4):
        configs.append(
            dict(
                idx=i,
                lat=i * 20 - 30 + 7,
                unit="km" if i % 2 == 0 else "mi",
                offset_km=(i + 1) * 2 + 1,
                bucket=f"asyncrace{i}",
            )
        )

    for cfg in configs:
        far_delta = _lat_offset_deg(cfg["offset_km"])
        GeoRaceDoc(
            place_id=f"asyncrace{cfg['idx']}_anchor",
            coordinates=popoto.GeoField.Coordinates(
                latitude=cfg["lat"], longitude=10.0
            ),
            bucket=cfg["bucket"],
        ).save()
        GeoRaceDoc(
            place_id=f"asyncrace{cfg['idx']}_far",
            coordinates=popoto.GeoField.Coordinates(
                latitude=cfg["lat"] + far_delta, longitude=10.0
            ),
            bucket=cfg["bucket"],
        ).save()

    async def run_one(cfg):
        rows = await GeoRaceDoc.query.async_filter(
            coordinates=(cfg["lat"], 10.0),
            coordinates_radius=20,
            coordinates_radius_unit=cfg["unit"],
            coordinates_with_distances=True,
            bucket=cfg["bucket"],
        )
        return cfg, rows

    errors = []
    for _ in range(15):
        results = await asyncio.gather(*(run_one(cfg) for cfg in configs))
        for cfg, rows in results:
            if len(rows) != 2:
                errors.append(f"idx={cfg['idx']}: expected 2 rows, got {len(rows)}")
                continue
            tol = _tolerance(cfg["unit"])
            for row in rows:
                if not hasattr(row, "_geo_distance"):
                    errors.append(
                        f"idx={cfg['idx']} place_id={row.place_id}: "
                        "missing _geo_distance"
                    )
                    continue
                if row._geo_distance_unit != cfg["unit"]:
                    errors.append(
                        f"idx={cfg['idx']} place_id={row.place_id}: "
                        f"unit={row._geo_distance_unit!r}, expected "
                        f"{cfg['unit']!r}"
                    )
                is_anchor = row.place_id.endswith("_anchor")
                expected = _expected_km(cfg["offset_km"], cfg["unit"], is_anchor)
                if abs(row._geo_distance - expected) > tol:
                    errors.append(
                        f"idx={cfg['idx']} place_id={row.place_id}: "
                        f"distance={row._geo_distance}, expected "
                        f"{expected} (tolerance {tol})"
                    )

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# I. Q-object + geo query must attach distances -- the Q leaf must route into
# the same carrier the caller reads back from (CRITIQUE-B1).
# ---------------------------------------------------------------------------


def test_q_object_geo_query_attaches_distances():
    lat = 15.0
    offset_km = 4.0

    GeoRaceDoc(
        place_id="qtest_anchor",
        coordinates=popoto.GeoField.Coordinates(latitude=lat, longitude=10.0),
        bucket="qtest",
    ).save()
    GeoRaceDoc(
        place_id="qtest_far",
        coordinates=popoto.GeoField.Coordinates(
            latitude=lat + _lat_offset_deg(offset_km), longitude=10.0
        ),
        bucket="qtest",
    ).save()
    # Same coordinates, different bucket -- must be excluded by the AND, and
    # must not contaminate the distances attached to the qtest-bucket rows.
    GeoRaceDoc(
        place_id="qtest_other_bucket",
        coordinates=popoto.GeoField.Coordinates(latitude=lat, longitude=10.0),
        bucket="other",
    ).save()

    rows = list(
        GeoRaceDoc.query.filter(
            Q(bucket="qtest")
            & Q(
                coordinates=(lat, 10.0),
                coordinates_radius=20,
                coordinates_radius_unit="km",
                coordinates_with_distances=True,
            )
        )
    )

    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    for row in rows:
        assert hasattr(row, "_geo_distance"), f"{row.place_id} missing _geo_distance"
        assert row._geo_distance_unit == "km"
        expected = _expected_km(offset_km, "km", row.place_id.endswith("_anchor"))
        assert abs(row._geo_distance - expected) < _tolerance(
            "km"
        ), f"{row.place_id}: distance={row._geo_distance}, expected {expected}"


# ---------------------------------------------------------------------------
# J. The carrier parameter must not shadow a model field named ``state``.
#
# #640 threads the geo bookkeeping through ``_PushdownState``, and the delegate
# that receives it is ``_filter_for_keys_set_with_state(self, state, /,
# **kwargs)``. The ``/`` is load-bearing. ``**kwargs`` there are candidate
# model *field names*, and this repo ships a field literally named ``state``
# (``src/popoto/extraction/decision_log.py:182``). Without the ``/`` the
# parameter is positional-OR-keyword, so Python binds it twice and raises
# ``TypeError: got multiple values for argument 'state'`` before the body runs
# -- on every entry point, for any model with such a field.
#
# Declaring a parameter before ``**kwargs`` does NOT make it positional-only;
# only ``/`` does. The general rule: a ``**kwargs``-collecting method may not
# grow a named parameter that a caller could supply by keyword.
# ---------------------------------------------------------------------------


class StateNamedFieldDoc(popoto.Model):
    """A model whose field name collides with the carrier parameter."""

    doc_id = popoto.KeyField()
    state = popoto.StringField(default="")


class TestCarrierParameterDoesNotShadowModelFields:
    @staticmethod
    def _fixture():
        for doc_id, state in (("carrier_a", "accept"), ("carrier_b", "reject")):
            StateNamedFieldDoc(doc_id=doc_id, state=state).save()

    def test_carrier_param_is_positional_only(self):
        """The declaration itself, so a future edit that drops ``/`` is loud."""
        import inspect

        params = inspect.signature(
            QueryClass._filter_for_keys_set_with_state
        ).parameters
        assert params["state"].kind is inspect.Parameter.POSITIONAL_ONLY

    def test_kwargs_filter_on_a_field_named_state(self):
        self._fixture()
        rows = list(StateNamedFieldDoc.query.filter(state="accept"))
        assert [r.doc_id for r in rows] == ["carrier_a"]

    def test_q_object_filter_on_a_field_named_state(self):
        self._fixture()
        rows = list(StateNamedFieldDoc.query.filter(Q(state="accept")))
        assert [r.doc_id for r in rows] == ["carrier_a"]

    def test_public_filter_for_keys_set_on_a_field_named_state(self):
        """The public method is the one a downstream caller reaches for."""
        self._fixture()
        keys = StateNamedFieldDoc.query.filter_for_keys_set(state="accept")
        assert isinstance(keys, set)
        assert len(keys) >= 1

    def test_async_filter_on_a_field_named_state(self):
        """The async path routes through the same delegate."""
        self._fixture()

        async def _run():
            return await StateNamedFieldDoc.query.async_filter(state="accept")

        rows = list(asyncio.run(_run()))
        assert [r.doc_id for r in rows] == ["carrier_a"]
