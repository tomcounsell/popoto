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
"""

import os
import sys
import threading

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.query import QueryException
from src.popoto.redis_db import POPOTO_REDIS_DB
import src.popoto.redis_db as redis_db_module
import asyncio


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
    for pattern in ("*ThreadSafeDoc*", "*ConcurrentDoc*", "*AsyncCountDoc*"):
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
