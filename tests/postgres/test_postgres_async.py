"""``[PG-only]`` the async Postgres backend (#759 M5, TD-6).

Parity of every routed ``async_*`` method with its sync twin is
``tests/test_async_parity.py`` (both legs) and ``tests/test_async.py``; this
file covers what has no Redis counterpart: the async backend's event-loop
behaviour (a pool per loop, closed with its loop, ``pg_stat_activity``
stable across loops), that its I/O never leaves the loop thread, and that it
keeps the sync backend's contracts -- the async ``transaction()``, the
deadlock and #769 rules, the savepoint rule, the outage record and the
first-use server checks -- because it *is* the sync backend's code.
"""

import asyncio
import contextvars
import os
import random
import threading
import time

import numpy as np
import pytest

import popoto
from popoto import DecayingSortedField, ValidityField, ValidityValidFromConflictError
from popoto.backends import (
    AsyncBackend,
    BackendBusyError,
    BackendCapabilityError,
    BackendRetryableError,
    BackendUnavailableError,
    _swap_instance,
    get_backend,
    set_backend,
)
from popoto.embeddings import AbstractEmbeddingProvider
from popoto.fields.bm25_field import BM25Field
from popoto.fields.constants import Defaults
from popoto.fields.embedding_field import EmbeddingField
from popoto.redis_db import get_REDIS_DB

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("greenlet")

from popoto.backends.postgres import aio  # noqa: E402


class _second_ok:
    """``async with _second_ok(pg)``: ``pg.second_connection_ok()`` as an
    async context manager, to stack with ``twin.transaction()`` (#776)."""

    def __init__(self, backend):
        self._cm = backend.second_connection_ok()

    async def __aenter__(self):
        self._cm.__enter__()

    async def __aexit__(self, *exc):
        return self._cm.__exit__(*exc)


class AioNote(popoto.Model):
    key = popoto.KeyField()
    n = popoto.IntField(default=0)


class AioMem(popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField(null=False)
    weight = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        decay_rate=0.5, base_score_field="weight", partition_by="agent"
    )


class _Hash(AbstractEmbeddingProvider):
    def __init__(self):
        self.threads = []

    def embed(self, texts, input_type=None):
        self.threads.append(threading.get_ident())
        out = []
        for text in texts:
            rng = np.random.RandomState(sum(map(ord, text)) % (2**31))
            out.append(rng.randn(8).tolist())
        return out

    @property
    def dimensions(self):
        return 8

    @property
    def max_batch_size(self):
        return 32


HASH = _Hash()


class AioDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")
    content = BM25Field(source="text")
    embedding = EmbeddingField(source="text", provider=HASH)
    validity = ValidityField()


def _clients(admin):
    """Client connections to this database other than the admin's own."""
    admin.execute("SELECT pg_stat_clear_snapshot()")
    (n,) = admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
    ).fetchone()
    return n


def _settle(admin, want_at_most, timeout=3.0):
    deadline = time.monotonic() + timeout
    n = _clients(admin)
    while n > want_at_most and time.monotonic() < deadline:
        time.sleep(0.02)
        n = _clients(admin)
    return n


# -- the protocol ---------------------------------------------------------------


def test_the_async_backend_is_the_sync_backends_twin(pg):
    twin = aio.get_async_backend(AioNote)
    assert isinstance(twin, AsyncBackend)
    assert twin.sync is pg and aio.get_async_backend(AioNote) is twin
    assert twin.health is pg.health and twin.schema == pg.schema


def test_redis_bound_models_have_no_async_backend():
    class AioRedisOnly(popoto.Model):
        key = popoto.KeyField()

        class Meta:
            backend = "redis"

    with pytest.raises(BackendCapabilityError, match="Postgres"):
        aio.get_async_backend(AioRedisOnly)


@pytest.mark.asyncio
async def test_protocol_methods_run_the_sync_methods(pg):
    twin = aio.get_async_backend(AioNote)
    obj = AioNote(key="p", n=1)
    outcome = await twin.save(obj)
    spec = AioNote._meta.spec
    rid = outcome.id
    (row,) = await twin.load(spec, [rid])
    assert row["n"] == 1
    assert await twin.exists(spec, [rid]) == [True]
    assert await twin.increment(spec, rid, "n", 4) == 5
    plan = popoto.backends.QueryPlan(project=())
    assert [r["_id"].canonical for r in await twin.select(spec, plan)] == [
        rid.canonical
    ]
    assert await twin.count(spec, popoto.backends.QueryPlan()) == 1
    assert await twin.delete(spec, [rid]) == 1
    assert await twin.exists(spec, [rid]) == [False]


# -- on the loop, not a thread ----------------------------------------------------


@pytest.mark.asyncio
async def test_every_statement_runs_on_the_loop_thread_in_the_bridge(pg, monkeypatch):
    seen = []
    original = pg._run

    def run(sql, params=(), **kw):
        seen.append((threading.get_ident(), aio._in_bridge()))
        return original(sql, params, **kw)

    monkeypatch.setattr(pg, "_run", run)
    loop_thread = threading.get_ident()
    record = await AioNote.async_create(key="a", n=1)
    record.n = 2
    await record.async_save()
    await AioNote.query.async_filter(key="a")
    await AioNote.query.async_count()
    await AioNote.query.async_get(key="a")
    await AioNote.async_delete_all()
    assert seen and all(t == loop_thread and bridged for t, bridged in seen), seen
    loop = asyncio.get_running_loop()
    (entry,) = [e for e in aio._apools.values() if e.loop_ref() is loop]
    assert entry.conns, "the loop's own pool carried the statements"


@pytest.mark.asyncio
async def test_a_provider_call_leaves_the_loop_and_the_sql_does_not(pg):
    HASH.threads.clear()
    await AioDoc(name="d", text="alpha beta", validity=1.0).async_save()
    assert HASH.threads and threading.get_ident() not in HASH.threads
    stored = await AioDoc.query.async_get(name="d")
    assert stored.text == "alpha beta"


@pytest.mark.asyncio
async def test_context_variables_reach_the_bridge_as_they_reach_to_thread(pg):
    var = contextvars.ContextVar("aio_probe", default="unset")
    var.set("caller")
    twin = aio.get_async_backend(AioNote)
    assert await twin.run(var.get) == "caller"


def test_a_sync_call_with_an_async_unit_of_work_is_refused(pg):
    async def main():
        twin = aio.get_async_backend(AioNote)
        async with twin.transaction() as uow:
            with pytest.raises(aio.BridgeMisuseError, match="async_save"):
                AioNote(key="x").save(pipeline=uow)

    asyncio.run(main())


# -- event loops ------------------------------------------------------------------


async def _work(i):
    await asyncio.gather(
        *(AioNote.async_create(key=f"l{i}-{j}", n=j) for j in range(8))
    )
    assert await AioNote.query.async_count() >= 8


def test_one_model_from_many_loops_leaks_no_connections(pg, admin):
    """Each asyncio.run() is a new loop with its own pool; the pool closes
    with its loop, so the server's client count does not grow."""
    asyncio.run(_work(0))
    baseline = _clients(admin)
    for i in range(1, 12):
        asyncio.run(_work(i))
        assert not [e for e in aio._apools.values() if e.loop_gone()]
    assert _settle(admin, baseline) <= baseline
    assert AioNote.query.count() == 12 * 8


def test_a_loop_closed_without_shutting_down_is_swept(pg, admin):
    """A loop closed directly (no shutdown_asyncgens) leaves its pool open;
    the next pool lookup, from any loop, closes those connections."""
    asyncio.run(_work(0))
    baseline = _clients(admin)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_work(1))
    finally:
        loop.close()  # no shutdown_asyncgens: the keeper never runs
    assert _clients(admin) > baseline, "the closed loop's connections are open"
    asyncio.run(_work(2))
    assert _settle(admin, baseline) <= baseline


@pytest.mark.asyncio
async def test_pytest_asyncio_loops_each_get_a_pool_one(pg):
    await _work(0)
    assert any(e.loop_ref() is asyncio.get_running_loop() for e in aio._apools.values())


@pytest.mark.asyncio
async def test_pytest_asyncio_loops_each_get_a_pool_two(pg):
    """A second function-scoped loop: the first loop's pool was closed with
    it, and this one opens its own."""
    await _work(1)
    loop = asyncio.get_running_loop()
    assert all(e.loop_ref() is loop for e in aio._apools.values())


@pytest.mark.asyncio
async def test_a_cancelled_statement_frees_its_slot_and_connection(pg):
    twin = aio.get_async_backend(AioNote)
    await AioNote.async_create(key="c")
    task = asyncio.ensure_future(twin.run(pg._run, "SELECT pg_sleep(3)"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry = next(
        e for e in aio._apools.values() if e.loop_ref() is asyncio.get_running_loop()
    )
    assert entry.slots._value == entry.max_size
    started = time.monotonic()
    assert await AioNote.query.async_count() == 1
    assert time.monotonic() - started < 1.0


# -- cancellation never leaks a session (#784 review, blocker 1) ----------------
#
# A task cancelled while the pool's checkout check (one empty query) was in
# flight used to release its slot but leave the connection open, owned by no
# caller and counted by no slot: 500 cancelled saves took the server from 1
# session to 22 with a pool of 4. These tests count sessions by an
# application_name of their own, so other clients of the server never move
# the number.

_LEAK_APP = "popoto_test_cancel_leak"


@pytest.fixture
def leak_pg(pg, admin):
    """``pg`` with every pooled connection tagged ``_LEAK_APP``."""
    from psycopg.conninfo import make_conninfo

    from popoto.backends.postgres import PostgresBackend

    backend = PostgresBackend(
        make_conninfo(pg.dsn, application_name=_LEAK_APP), schema=pg.schema
    )
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        aio.close_async_pools()
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s",
            (_LEAK_APP,),
        )


def _app_sessions(admin):
    admin.execute("SELECT pg_stat_clear_snapshot()")
    (n,) = admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
        (_LEAK_APP,),
    ).fetchone()
    return n


def _this_loops_pool():
    loop = asyncio.get_running_loop()
    return next(e for e in aio._apools.values() if e.loop_ref() is loop)


def _assert_accounted(entry):
    """Every tracked connection is idle or checked out, never neither."""
    assert len(entry.conns) <= entry.max_size
    assert entry.conns <= set(entry.idle) | entry.out


def _cancel_storm(admin, attempt, n):
    import random

    async def main():
        await AioNote.async_create(key="w")
        rnd = random.Random(784)
        for i in range(n):
            await attempt(i, rnd)
            if i % 50 == 49:
                entry = _this_loops_pool()
                assert _app_sessions(admin) <= entry.max_size
                assert len(entry.conns) <= entry.max_size
        entry = _this_loops_pool()
        assert _app_sessions(admin) <= entry.max_size
        # Every task has finished, so nothing is checked out: a tracked
        # connection that is not idle is an orphan.
        assert len(entry.conns) == len(entry.idle)
        _assert_accounted(entry)
        assert await AioNote.query.async_get(key="w") is not None
        await entry.aclose()
        assert not entry.conns
        assert _settle_app(admin, 0) == 0

    asyncio.run(main())
    assert _settle_app(admin, 0) == 0


def _settle_app(admin, want_at_most, timeout=3.0):
    deadline = time.monotonic() + timeout
    n = _app_sessions(admin)
    while n > want_at_most and time.monotonic() < deadline:
        time.sleep(0.02)
        n = _app_sessions(admin)
    return n


def test_cancelled_saves_leak_no_sessions(leak_pg, admin):
    """500 ``async_save`` tasks cancelled at a random point within 0-4 ms."""

    async def attempt(i, rnd):
        task = asyncio.ensure_future(AioNote(key=f"s{i % 7}", n=i).async_save())
        await asyncio.sleep(rnd.uniform(0, 0.004))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _cancel_storm(admin, attempt, 500)


def test_wait_for_timeouts_leak_no_sessions(leak_pg, admin):
    """``asyncio.wait_for`` timeouts, the shape real callers write."""

    async def attempt(i, rnd):
        try:
            await asyncio.wait_for(
                AioNote(key=f"w{i % 7}", n=i).async_save(), rnd.uniform(0.0002, 0.003)
            )
        except (asyncio.TimeoutError, TimeoutError):
            pass

    _cancel_storm(admin, attempt, 300)


def test_a_cancel_during_the_checkout_check_closes_the_connection(
    leak_pg, admin, monkeypatch
):
    """Deterministic: cancel exactly while ``_usable`` is awaiting. The
    connection it was checking is closed and untracked, and the slot freed."""
    original = aio._LoopPool._usable
    checking = []

    async def held_usable(conn):
        checking.append(conn)
        await asyncio.sleep(10)
        return await original(conn)

    async def main():
        await AioNote.async_create(key="k")  # leaves one idle connection
        entry = _this_loops_pool()
        assert len(entry.idle) == 1 and _app_sessions(admin) == 1
        monkeypatch.setattr(aio._LoopPool, "_usable", staticmethod(held_usable))
        task = asyncio.ensure_future(AioNote.query.async_get(key="k"))
        while not checking:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (conn,) = checking
        assert conn.closed
        assert conn not in entry.conns and not entry.out and not entry.idle
        assert entry.slots._value == entry.max_size
        assert _settle_app(admin, 0) == 0
        monkeypatch.setattr(aio._LoopPool, "_usable", original)
        assert await AioNote.query.async_get(key="k") is not None

    asyncio.run(main())


def test_aclose_closes_a_tracked_connection_nobody_holds(leak_pg, admin):
    """``aclose`` closes idle connections and any tracked one that is neither
    idle nor checked out, not only the idle ones."""

    async def main():
        await AioNote.async_create(key="k")
        entry = _this_loops_pool()
        conn = await entry.getconn()
        entry.out.discard(conn)  # an owner that vanished without putconn
        entry.slots.release()
        await entry.aclose()
        assert conn.closed and not entry.conns
        assert _settle_app(admin, 0) == 0

    asyncio.run(main())


# -- concurrency -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifty_tasks_save_query_and_rank_decay_on_one_loop(pg):
    twin = aio.get_async_backend(AioMem)

    async def task(i):
        mem = await AioMem.async_create(
            name=f"m{i:02d}", agent=f"a{i % 2}", weight=float(i + 1)
        )
        assert (await AioMem.query.async_get(name=mem.name)).weight == i + 1
        ranked = await twin.run(
            lambda: AioMem.query.filter(agent=f"a{i % 2}").top_by_decay(n=3)
        )
        scores = [r.weight for r in ranked]
        assert scores == sorted(scores, reverse=True)
        return await AioMem.query.async_count(agent=f"a{i % 2}")

    counts = await asyncio.gather(*(task(i) for i in range(50)))
    assert max(counts) == 25
    top = await twin.run(lambda: AioMem.query.filter(agent="a1").top_by_decay(n=2))
    assert [m.name for m in top] == ["m49", "m47"]


# -- the sync backend's contracts, kept ---------------------------------------------


@pytest.mark.asyncio
async def test_async_transaction_commits_and_rolls_back(pg):
    twin = aio.get_async_backend(AioNote)
    async with twin.transaction() as uow:
        await AioNote(key="t1").async_save(pipeline=uow)
        await AioNote(key="t2").async_save(pipeline=uow)
        with pg.second_connection_ok():  # a reader outside the unit (#776)
            assert await AioNote.query.async_count() == 0  # not committed yet
    assert await AioNote.query.async_count() == 2
    with pytest.raises(RuntimeError, match="boom"):
        async with twin.transaction() as uow:
            await AioNote(key="t3").async_save(pipeline=uow)
            raise RuntimeError("boom")
    assert await AioNote.query.async_get(key="t3") is None


@pytest.mark.asyncio
async def test_a_deadlock_between_two_async_transactions_raises_at_once(pg):
    """Two tasks on one loop take two records in opposite orders: Postgres
    picks a victim, whose transaction() raises BackendRetryableError (not
    retried: only the caller can rerun its block) and keeps nothing."""
    twin = aio.get_async_backend(AioNote)
    await AioNote.async_create(key="A")
    await AioNote.async_create(key="B")

    async def cross(first, second, delay):
        await asyncio.sleep(delay)
        async with twin.transaction() as uow:
            await AioNote(key=first, n=1).async_save(pipeline=uow)
            await asyncio.sleep(0.3)
            await AioNote(key=second, n=1).async_save(pipeline=uow)

    results = await asyncio.gather(
        cross("A", "B", 0.0), cross("B", "A", 0.05), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], BackendRetryableError), results
    assert isinstance(errors[0].__cause__, psycopg.errors.DeadlockDetected)
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.asyncio
async def test_a_single_statement_deadlock_is_retried_with_an_async_backoff(
    pg, monkeypatch
):
    await AioNote.async_create(key="r")
    failures = {"left": 2}
    original = aio._SyncConnection.execute

    def flaky(self, query, params=None, **kw):
        if "UPDATE" in str(query) and failures["left"]:
            failures["left"] -= 1
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return original(self, query, params, **kw)

    def no_blocking_sleep(seconds):
        raise AssertionError("time.sleep on the event loop")

    monkeypatch.setattr(aio._SyncConnection, "execute", flaky)
    monkeypatch.setattr(time, "sleep", no_blocking_sleep)
    record = await AioNote.query.async_get(key="r")
    twin = aio.get_async_backend(AioNote)
    spec = AioNote._meta.spec
    assert await twin.increment(spec, popoto.backends.record_id(record), "n", 1) == 1
    assert failures["left"] == 0

    failures["left"] = Defaults.PG_TRANSACTION_RETRIES + 1
    with pytest.raises(BackendRetryableError, match="40P01"):
        await twin.increment(spec, popoto.backends.record_id(record), "n", 1)


@pytest.mark.asyncio
async def test_a_refused_save_in_an_async_transaction_rolls_back_only_itself(
    pg, monkeypatch
):
    """The M3 savepoint rule, through the async unit of work."""
    a = AioDoc(name="a", text="alpha quick", validity=1000.0)
    b = AioDoc(name="b", text="bravo slow", validity=1000.0)
    await b.async_save()
    monkeypatch.setattr(ValidityField, "pre_save_validate", lambda *a, **k: None)
    b.text = "zulu different"
    b.validity = 500.0
    twin = aio.get_async_backend(AioDoc)
    async with twin.transaction() as tx:
        await a.async_save(pipeline=tx)
        with pytest.raises(ValidityValidFromConflictError):
            await b.async_save(pipeline=tx)
    assert (await AioDoc.query.async_get(name="a")).text == "alpha quick"
    stored = await AioDoc.query.async_get(name="b")
    assert stored.text == "bravo slow"
    hits = await twin.run(lambda: AioDoc.query.keyword_search("zulu"))
    assert not hits


def test_first_use_checks_the_server_version_through_the_async_path(pg, monkeypatch):
    monkeypatch.setattr(
        type(pg), "_server_facts", lambda self, uow=None: (170005, "UTF8")
    )
    pg._server_checked = False
    pg.forget_tables()
    with pytest.raises(BackendCapabilityError, match="PostgreSQL 18 or newer.*170005"):
        asyncio.run(AioNote.query.async_count())


def test_stale_pooled_async_connection_is_not_a_dropped_write(pg, admin):
    """#769, async: a pooled connection killed under a live loop is caught by
    the checkout check; the next save uses a fresh one and drops nothing."""

    async def main():
        twin = aio.get_async_backend(AioNote)
        await AioNote.async_create(key="before")
        rows, _ = await twin.run(pg._run, "SELECT pg_backend_pid()")
        admin.execute("SELECT pg_terminate_backend(%s)", (rows[0][0],))
        await asyncio.sleep(0.2)
        dropped = pg.health.dropped_writes
        await AioNote.async_create(key="after")
        assert pg.health.dropped_writes == dropped and pg.health.ok
        assert sorted(n.key for n in await AioNote.query.async_all()) == [
            "after",
            "before",
        ]

    asyncio.run(main())


def test_an_unreachable_server_is_an_outage_with_dropped_writes(monkeypatch):
    from popoto.backends import reset_bindings
    from popoto.backends.postgres import PostgresBackend, close_pools

    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    backend = PostgresBackend(
        dsn="postgresql://127.0.0.1:1/postgres", schema="popoto_aio_probe"
    )
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    reset_bindings()
    try:

        async def main():
            with pytest.raises(BackendUnavailableError, match="unavailable"):
                await AioNote(key="k").async_save()
            with pytest.raises(BackendUnavailableError):
                await AioNote.query.async_count()

        started = time.monotonic()
        asyncio.run(main())
        assert time.monotonic() - started < 3
        assert backend.health.dropped_writes == 1
        assert backend.health.consecutive_failures == 2
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        close_pools()


def test_without_greenlet_the_thread_shim_remains(pg, monkeypatch, caplog):
    monkeypatch.setattr(aio, "_greenlet_mod", None)
    monkeypatch.setattr(aio, "_warned_no_greenlet", False)

    async def main():
        await AioNote.async_create(key="g")
        return await AioNote.query.async_count()

    assert asyncio.run(main()) == 1
    assert "greenlet is not installed" in caplog.text
    assert get_backend(AioNote) is pg


# -- the held-record-lock registry is per task (#783 x #784 review) ------------------
#
# #783 refuses a write that would wait on a record lock its own thread holds in
# another open transaction. Under the async backend every task's I/O runs on
# the loop thread, so a per-thread registry refused task B's wait on task A's
# lock -- a wait A releases as soon as B yields: 18 of 20 concurrent async
# transactions failed with BackendCapabilityError. The registry is now keyed
# by the running task (the thread when there is none).


def _registry_empty(backend):
    return all(not units for units in list(backend._open.values()))


@pytest.mark.asyncio
async def test_twenty_concurrent_async_transactions_on_overlapping_records(pg):
    twin = aio.get_async_backend(AioNote)
    keys = [f"k{i}" for i in range(5)]
    for k in keys:
        await AioNote.async_create(key=k)

    async def work(i):
        mine = [k for j, k in enumerate(keys) if j != i % 5]  # 4 of 5, sorted
        async with twin.transaction() as uow:
            for k in mine:
                await AioNote(key=k, n=i).async_save(pipeline=uow)
                await asyncio.sleep(0)

    results = await asyncio.gather(
        *(work(i) for i in range(20)), return_exceptions=True
    )
    assert results == [None] * 20, [r for r in results if r is not None][:3]
    assert _registry_empty(pg)
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.slow  # ~30 s: each real deadlock costs deadlock_timeout (1 s)
@pytest.mark.asyncio
async def test_unsorted_concurrent_async_transactions_fail_only_retryably(
    pg, monkeypatch
):
    """Unsorted keys deadlock for real, and 20 transactions outnumber the
    pool: both are BackendRetryableError (a deadlock, or BackendBusyError),
    never a capability refusal or an outage, and every task completes on a
    rerun."""
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 1.0)
    twin = aio.get_async_backend(AioNote)
    keys = [f"u{i}" for i in range(5)]
    for k in keys:
        await AioNote.async_create(key=k)
    failures = []

    async def work(i):
        rng = random.Random(i)
        for _attempt in range(100):
            mine = rng.sample(keys, 4)
            try:
                async with twin.transaction() as uow:
                    for k in mine:
                        await AioNote(key=k, n=i).async_save(pipeline=uow)
                        await asyncio.sleep(0)
                return
            except BackendRetryableError as exc:
                failures.append(exc)
                await asyncio.sleep(rng.uniform(0.0, 0.05))
        raise AssertionError(f"task {i} never committed")

    results = await asyncio.gather(
        *(work(i) for i in range(20)), return_exceptions=True
    )
    assert results == [None] * 20, [r for r in results if r is not None][:3]
    kinds = {
        ("busy" if isinstance(f, BackendBusyError) else type(f.__cause__).__name__)
        for f in failures
    }
    assert kinds <= {"busy", "DeadlockDetected"}, kinds
    print(f"retryable failures: {len(failures)} of kinds {sorted(kinds)}")
    assert _registry_empty(pg)
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.asyncio
async def test_a_task_waits_for_another_tasks_transaction_then_succeeds(pg):
    """The other task is not a child of the transaction's task (it was
    created before the transaction opened), so it waits on the lock rather
    than being refused; a child created inside the block would be refused
    (``test_a_child_task_is_refused_at_once_on_its_parents_record_lock``)."""
    twin = aio.get_async_backend(AioNote)
    await AioNote.async_create(key="held")
    go = asyncio.Event()

    async def later():
        await go.wait()
        await AioNote(key="held", n=2).async_save()

    other = asyncio.create_task(later())
    async with twin.transaction() as uow:
        await AioNote(key="held", n=1).async_save(pipeline=uow)
        go.set()
        await asyncio.sleep(0.3)
        assert not other.done(), "the other task waits on the lock"
    await asyncio.wait_for(other, 10)
    assert (await AioNote.query.async_get(key="held")).n == 2
    assert _registry_empty(pg)


@pytest.mark.asyncio
async def test_a_task_colliding_with_its_own_open_transaction_is_refused_at_once(pg):
    twin = aio.get_async_backend(AioNote)
    await AioNote.async_create(key="own")
    async with twin.transaction() as uow:
        await AioNote(key="own", n=1).async_save(pipeline=uow)
        started = time.monotonic()
        with pytest.raises(BackendCapabilityError, match="this task or thread holds"):
            await AioNote(key="own", n=2).async_save()
        # A nested unit is a second connection on purpose (#776).
        async with _second_ok(pg), twin.transaction() as inner:
            with pytest.raises(
                BackendCapabilityError, match="this task or thread holds"
            ):
                await AioNote(key="own", n=3).async_save(pipeline=inner)
            await AioNote(key="free", n=3).async_save(pipeline=inner)
        assert time.monotonic() - started < 1.0
    assert (await AioNote.query.async_get(key="own")).n == 1
    assert (await AioNote.query.async_get(key="free")).n == 3
    assert _registry_empty(pg)
    assert pg.health.dropped_writes == 0


# -- a child task and its ancestors' record locks (#784 review) ---------------------
#
# A parent that awaits a child (gather, an awaited create_task) inside its own
# open transaction cannot commit until the child finishes, so a child that
# waits on one of the parent's record locks would hang to
# PG_STATEMENT_TIMEOUT_MS and then be booked as an outage. Each task remains
# its own lock scope; a task refuses, at once, a wait on a lock held by an
# *ancestor's* open unit. Siblings never see each other's units and still
# just wait.


@pytest.mark.asyncio
async def test_a_child_task_is_refused_at_once_on_its_parents_record_lock(
    pg, monkeypatch
):
    monkeypatch.setattr(Defaults, "PG_STATEMENT_TIMEOUT_MS", 5000)
    twin = aio.get_async_backend(AioNote)
    for k in ("k1", "k2", "k3"):
        await AioNote.async_create(key=k)
    dropped = pg.health.dropped_writes
    started = time.monotonic()
    async with twin.transaction() as uow:
        await AioNote(key="k1", n=1).async_save(pipeline=uow)
        with pytest.raises(BackendCapabilityError, match="enclosing") as gathered:
            await asyncio.gather(AioNote(key="k1", n=2).async_save())
        assert "pipeline=uow" in str(gathered.value)
        with pytest.raises(BackendCapabilityError, match="parent task"):
            await asyncio.create_task(AioNote(key="k1", n=3).async_save())

        async def grandchild():
            await asyncio.gather(AioNote(key="k1", n=4).async_save())

        with pytest.raises(BackendCapabilityError, match="parent task"):
            await asyncio.create_task(grandchild())
        # A child joining the parent's unit, and a child writing a record the
        # parent does not hold, both simply work.
        await asyncio.gather(
            AioNote(key="k1", n=5).async_save(pipeline=uow),
            AioNote(key="k2", n=5).async_save(),
        )
    assert time.monotonic() - started < 2.0
    assert (await AioNote.query.async_get(key="k1")).n == 5
    assert (await AioNote.query.async_get(key="k2")).n == 5
    assert pg.health.ok and pg.health.consecutive_failures == 0
    assert pg.health.dropped_writes == dropped
    assert _registry_empty(pg)
    # After the parent's transaction ends, a child may write the record.
    await asyncio.gather(AioNote(key="k1", n=6).async_save())
    assert (await AioNote.query.async_get(key="k1")).n == 6


@pytest.mark.asyncio
async def test_a_child_task_is_refused_on_a_batch_its_parent_holds(pg, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_STATEMENT_TIMEOUT_MS", 5000)
    await AioNote.async_create(key="b1")
    pipe = popoto.batch()
    try:
        await AioNote(key="b1", n=1).async_save(pipeline=pipe)
        started = time.monotonic()
        with pytest.raises(BackendCapabilityError, match="parent task"):
            await asyncio.gather(AioNote(key="b1", n=2).async_save())
        assert time.monotonic() - started < 1.0
        await asyncio.gather(AioNote(key="b1", n=3).async_save(pipeline=pipe))
        await pipe.async_execute()
    finally:
        await pipe.async_reset()
    assert (await AioNote.query.async_get(key="b1")).n == 3
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.asyncio
async def test_sibling_transactions_on_overlapping_records_wait_not_refuse(pg):
    """Two children of one parent -- itself inside a transaction on another
    record -- each open their own transaction on overlapping records: they
    serialize on the lock, neither is refused."""
    twin = aio.get_async_backend(AioNote)
    for k in ("p", "s1", "s2"):
        await AioNote.async_create(key=k)
    order = []

    async def sibling(i):
        async with twin.transaction() as uow:
            for k in ("s1", "s2"):
                await AioNote(key=k, n=i).async_save(pipeline=uow)
                await asyncio.sleep(0.05)
        order.append(i)

    async with twin.transaction() as uow:
        await AioNote(key="p", n=1).async_save(pipeline=uow)
        results = await asyncio.gather(sibling(1), sibling(2), return_exceptions=True)
    assert results == [None, None], results
    assert sorted(order) == [1, 2]
    last = order[-1]
    assert (await AioNote.query.async_get(key="s1")).n == last
    assert (await AioNote.query.async_get(key="s2")).n == last
    assert _registry_empty(pg)
    assert pg.health.ok and pg.health.dropped_writes == 0


# -- a busy pool is not an outage (#784 review) ---------------------------------------


def test_a_busy_async_pool_is_not_an_outage(pg, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_POOL_MAX_SIZE", 2)
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)

    async def main():
        twin = aio.get_async_backend(AioNote)
        await AioNote.async_create(key="x")  # this loop's pool: 2 connections
        dropped = pg.health.dropped_writes
        # Exhausting the pool from one task is the point (#776's guard off).
        async with _second_ok(pg), twin.transaction(), twin.transaction():
            with pytest.raises(BackendBusyError) as write:
                await AioNote(key="y").async_save()
            with pytest.raises(BackendBusyError):
                await AioNote.query.async_count()
        assert isinstance(write.value, BackendRetryableError)
        assert not isinstance(write.value, BackendUnavailableError)
        assert "contention, not an outage" in str(write.value)
        assert pg.health.ok and pg.health.consecutive_failures == 0
        assert pg.health.dropped_writes == dropped
        await AioNote(key="y").async_save()  # a slot is free again
        assert await AioNote.query.async_count() == 2

    asyncio.run(main())


def test_a_partitioned_async_pool_is_an_outage_not_busy(monkeypatch):
    """A listener that accepts the TCP handshake and never answers (a
    partition, a black-holed port): the first callers sit inside connect()
    holding the pool's slots, the rest time out waiting for one. Those
    waits are the outage's, not contention -- every failure is
    BackendUnavailableError and every write is counted dropped (#784
    review: 8 of 10 were BackendBusyError, uncounted)."""
    import socket

    from popoto.backends import reset_bindings
    from popoto.backends.postgres import PostgresBackend, close_pools

    monkeypatch.setattr(Defaults, "PG_POOL_MAX_SIZE", 2)
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 1.0)
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(64)  # never accepted: the handshake completes, nothing answers
    port = hole.getsockname()[1]
    backend = PostgresBackend(
        dsn=f"postgresql://127.0.0.1:{port}/postgres", schema="popoto_aio_probe"
    )
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    reset_bindings()
    try:

        async def one(i):
            try:
                await AioNote(key=f"w{i}").async_save()
            except BackendBusyError:
                return "busy"
            except BackendUnavailableError:
                return "outage"
            return "ok"

        async def main():
            return await asyncio.gather(*(one(i) for i in range(10)))

        results = asyncio.run(main())
        assert results == ["outage"] * 10, results
        assert not backend.health.ok
        assert backend.health.dropped_writes == 10
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        close_pools()
        hole.close()


class _SlowHandshakeProxy:
    """A TCP proxy to a real server that holds every new connection for
    ``delay`` seconds before dialling the server: a reachable server whose
    connects are slow (a loaded CI runner), which
    is what turned main red after #784 (run 37344318326)."""

    def __init__(self, upstream: tuple[str, int], delay: float) -> None:
        import socket

        self.upstream = upstream
        self.delay = delay
        self.dialled = 0
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(64)
        self.port = self.lsock.getsockname()[1]
        self.socks: list = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.lsock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client) -> None:
        import socket

        time.sleep(self.delay)
        try:
            server = socket.create_connection(self.upstream)
        except OSError:
            client.close()
            return
        with self.lock:
            self.dialled += 1
            self.socks += [client, server]
        for a, b in ((client, server), (server, client)):
            threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(a, b) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(2)
                except OSError:
                    pass

    def close(self) -> None:
        self.lsock.close()
        with self.lock:
            socks, self.socks = self.socks, []
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def test_slow_connects_to_a_reachable_server_are_contention_not_an_outage(
    pg, monkeypatch
):
    """The CI condition behind run 37344318326: a reachable server whose
    connects take longer than a waiter's checkout timeout. The first callers
    hold the pool's slots while *connecting*, so no slot is checked out when
    the queued callers time out -- and the old rule (busy only when every
    slot is checked out) counted those waits as an outage, with dropped
    writes. A connect in progress to a server that then answers is
    contention: every queued caller gets BackendBusyError (retryable), and
    the health record is untouched. Contrast
    ``test_a_partitioned_async_pool_is_an_outage_not_busy``, where the
    connects in progress *fail*."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from popoto.backends import reset_bindings
    from popoto.backends.postgres import PostgresBackend, close_pools

    AioNote.create(key="seed")  # the table, through pg
    params = conninfo_to_dict(pg.dsn)
    host = params.get("host") or "localhost"
    if host.startswith("/"):
        pytest.skip("the server is on a unix socket; the proxy speaks TCP")
    upstream = (host, int(params.get("port") or 5432))
    proxy = _SlowHandshakeProxy(upstream, delay=1.5)
    monkeypatch.setattr(Defaults, "PG_POOL_MAX_SIZE", 2)
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 1.0)
    backend = PostgresBackend(
        make_conninfo(
            pg.dsn,
            host="127.0.0.1",
            hostaddr="127.0.0.1",
            port=str(proxy.port),
            application_name="popoto_test_slow_connect",
        ),
        schema=pg.schema,
    )
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    reset_bindings()
    try:

        async def one(i):
            try:
                await AioNote(key=f"s{i}").async_save()
            except BackendBusyError:
                return "busy"
            except BackendUnavailableError as exc:
                return f"outage: {exc}"
            return "ok"

        async def main():
            return await asyncio.gather(*(one(i) for i in range(6)))

        results = asyncio.run(main())
        assert set(results) <= {"ok", "busy"}, results
        assert "busy" in results, results  # the queued callers did time out
        assert "ok" in results, results  # the connecting callers committed
        assert proxy.dialled >= 1, "the slow handshake was exercised"
        assert backend.health.ok and backend.health.consecutive_failures == 0
        assert backend.health.dropped_writes == 0
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        reset_bindings()
        close_pools()
        proxy.close()


def test_a_wait_behind_a_slow_checkout_check_is_contention_not_an_outage(
    pg, monkeypatch
):
    """The window that actually fired on CI (run 37344318326): a caller
    that took a slot is still validating an idle connection (the checkout
    check's empty-query round trip) when a queued caller's wait runs out, so
    fewer than every slot is checked out. Reproduced locally by slowing that
    round trip; the CI test then failed 2 of 2 runs before the fix. The
    check answers, so the queued caller is busy, and health is untouched."""
    monkeypatch.setattr(Defaults, "PG_POOL_MAX_SIZE", 1)
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    real_usable = aio._LoopPool._usable

    async def slow_usable(conn):
        await asyncio.sleep(0.6)  # outlasts the queued caller's 0.3 s wait
        return await real_usable(conn)

    async def main():
        await AioNote.async_create(key="x")  # one idle connection, warm
        monkeypatch.setattr(aio._LoopPool, "_usable", staticmethod(slow_usable))
        checking = asyncio.create_task(AioNote(key="a").async_save())
        await asyncio.sleep(0.05)  # it holds the only slot, in the check
        with pytest.raises(BackendBusyError):
            await AioNote(key="b").async_save()
        await checking
        assert await AioNote.query.async_count() == 2

    asyncio.run(main())
    assert pg.health.ok and pg.health.consecutive_failures == 0
    assert pg.health.dropped_writes == 0


def test_a_busy_sync_pool_is_not_an_outage(pg, monkeypatch):
    from psycopg.conninfo import make_conninfo

    from popoto.backends.postgres import PostgresBackend, _pools

    AioNote.create(key="x")  # the table, through pg
    monkeypatch.setattr(Defaults, "PG_POOL_MAX_SIZE", 2)
    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    backend = PostgresBackend(
        make_conninfo(pg.dsn, application_name="popoto_test_busy_sync"),
        schema=pg.schema,
    )
    spec = AioNote._meta.spec
    plan = popoto.backends.QueryPlan()
    try:
        assert backend.count(spec, plan) == 1  # this backend's own pool of 2
        # Exhausting the pool from one thread is the point (#776's guard off).
        with (
            backend.second_connection_ok(),
            backend.transaction(),
            backend.transaction(),
        ):
            with pytest.raises(BackendBusyError):
                backend.count(spec, plan)
            with pytest.raises(BackendBusyError):
                backend.save(AioNote(key="y"))
        assert backend.health.ok and backend.health.consecutive_failures == 0
        assert backend.health.dropped_writes == 0
        backend.save(AioNote(key="y"))
        assert backend.count(spec, plan) == 2
    finally:
        pool = _pools.pop((backend.dsn, os.getpid()), None)
        if pool is not None:
            pool.close()


# -- popoto.batch() from async code -----------------------------------------------------


class AioStream(popoto.EventStreamMixin, popoto.Model):
    name = popoto.KeyField()

    _stream_name = "test_aio_batch_effects"


_AIO_STREAM = "stream:test_aio_batch_effects"


def test_an_async_save_joins_a_batch_on_the_loop(pg, admin, monkeypatch):
    """``await obj.async_save(pipeline=popoto.batch())``: the batch's
    transaction is opened on the loop's async pool, nothing blocks the loop
    (no sync pool, no time.sleep), the stream entries are appended at the
    commit -- in the batch's own transaction, on the backend's events
    tables (#759 M5), with nothing sent to Redis -- and the batch is
    committed with ``await pipe.async_execute()``."""
    import psycopg_pool

    redis = get_REDIS_DB()
    redis.delete(_AIO_STREAM)

    def entries():
        # A raw admin connection: neither the sync pool nor Redis.
        (n,) = admin.execute(
            f'SELECT count(*) FROM "{pg.schema}"."popoto_stream_entry" '
            "WHERE stream = %s",
            [_AIO_STREAM],
        ).fetchone()
        return int(n)

    def no_sync_pool(*args, **kwargs):
        raise AssertionError("the sync pool was used on the event loop")

    def no_blocking_sleep(seconds):
        raise AssertionError("time.sleep on the event loop")

    async def main():
        await AioStream.async_create(name="warm")
        base = entries()
        monkeypatch.setattr(psycopg_pool.ConnectionPool, "getconn", no_sync_pool)
        monkeypatch.setattr(time, "sleep", no_blocking_sleep)

        pipe = popoto.batch()
        assert await AioStream(name="a").async_save(pipeline=pipe) is pipe
        await AioStream(name="b").async_save(pipeline=pipe)
        assert entries() == base  # nothing before the commit
        with pg.second_connection_ok():  # a reader outside the batch (#776)
            assert await AioStream.query.async_get(name="a") is None
        with pytest.raises(aio.BridgeMisuseError, match="async_execute"):
            pipe.execute()
        assert await pipe.async_execute() == []
        assert entries() == base + 2
        assert await AioStream.query.async_get(name="b") is not None

        pipe = popoto.batch()
        await AioStream(name="c").async_save(pipeline=pipe)
        await pipe.async_reset()
        assert await AioStream.query.async_get(name="c") is None

        pipe = popoto.batch()
        with pipe:  # a sync exit schedules the rollback on the loop
            await AioStream(name="d").async_save(pipeline=pipe)
        for _ in range(100):
            if _registry_empty(pg):
                break
            await asyncio.sleep(0.01)
        assert await AioStream.query.async_get(name="d") is None
        assert entries() == base + 2
        assert redis.xlen(_AIO_STREAM) == 0  # the stream never touched Redis
        assert _registry_empty(pg)

    try:
        asyncio.run(main())
    finally:
        redis.delete(_AIO_STREAM)


def test_an_async_opened_batch_carries_its_events_and_notifies_to_commit(pg, admin):
    """#787 x #784: a batch an ``async_*`` call opened commits on the loop.
    The stream appends of its saves, a custom ``_xadd_event`` and a
    ``Publisher``'s message handed the batch all ride that transaction:
    appended and delivered at ``await pipe.async_execute()``'s COMMIT, and
    none of them after ``await pipe.async_reset()``."""

    def entries():
        (n,) = admin.execute(
            f'SELECT count(*) FROM "{pg.schema}"."popoto_stream_entry" '
            "WHERE stream = %s",
            [_AIO_STREAM],
        ).fetchone()
        return int(n)

    sub = pg.pubsub()
    sub.subscribe("aio-batch")
    sub.get_message(timeout=0.2)
    publisher = popoto.Publisher(channel_name="aio-batch")

    def messages():
        out = []
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            m = sub.get_message(timeout=0.05)
            if m is not None and m["type"] == "message":
                out.append(m["data"])
        return out

    async def main(commit):
        pipe = popoto.batch()
        obj = AioStream(name=f"ev-{commit}")
        await obj.async_save(pipeline=pipe)
        obj._xadd_event("custom", extra_fields={"k": "v"}, pipeline=pipe)
        assert publisher.publish({"n": commit}, pipeline=pipe) is pipe
        assert entries() == base
        if commit:
            await pipe.async_execute()
        else:
            await pipe.async_reset()

    AioStream.create(name="warm")
    base = entries()
    try:
        asyncio.run(main(False))
        assert entries() == base
        assert messages() == []
        asyncio.run(main(True))
        assert entries() == base + 2  # the save's entry and the custom one
        assert len(messages()) == 1
    finally:
        sub.close()


def test_an_async_write_to_a_sync_opened_batch_is_refused(pg):
    """A batch a sync write opened holds a blocking connection: an async_*
    write joining it would drive that connection from the loop, stalling
    every task. It is refused before anything is sent, and the batch still
    commits from the sync side."""
    AioNote.create(key="seed")
    pipe = popoto.batch()
    AioNote(key="s1", n=1).save(pipeline=pipe)

    async def main():
        with pytest.raises(aio.BridgeMisuseError, match="sync call opened"):
            await AioNote(key="s2", n=2).async_save(pipeline=pipe)

    try:
        asyncio.run(main())
        assert pipe.execute() == []
    finally:
        pipe.reset()  # a regression must not leave the batch's lock held
    assert AioNote.query.get(key="s1").n == 1
    assert AioNote.query.get(key="s2") is None
    assert _registry_empty(pg)


def test_a_failed_scheduled_rollback_is_retrieved_and_logged(caplog):
    """The rollback a sync reset() schedules for an async-opened batch is a
    task nobody awaits: its exception is retrieved by the done callback and
    logged once, never "Task exception was never retrieved"."""
    import logging

    import importlib

    batch_mod = importlib.import_module("popoto.batch")

    async def broken():
        raise psycopg.OperationalError("the connection is lost")

    async def main():
        task = asyncio.get_running_loop().create_task(broken())
        batch_mod._pending.add(task)
        task.add_done_callback(batch_mod._rollback_done)
        await asyncio.sleep(0.01)
        return task

    with caplog.at_level(logging.WARNING, logger="popoto.batch"):
        task = asyncio.run(main())
    assert task not in batch_mod._pending
    assert task._log_traceback is False  # retrieved: no asyncio error at GC
    assert "scheduled rollback" in caplog.text
    assert caplog.text.count("the connection is lost") == 1


_OUTLIVE = """
import asyncio, gc, sys
import popoto
from popoto.backends import _swap_instance, set_backend
from popoto.backends.postgres import PostgresBackend, close_pools

backend = PostgresBackend(sys.argv[1], schema=sys.argv[2])
set_backend(backend)
_swap_instance("postgres", backend)


class AioNote(popoto.Model):
    key = popoto.KeyField()
    n = popoto.IntField(default=0)


pipe = popoto.batch()


async def main():
    await AioNote(key="outlived", n=1).async_save(pipeline=pipe)


asyncio.run(main())
if sys.argv[3] == "reset":
    pipe.reset()
del pipe
gc.collect()
print("ROWS", AioNote.query.count(), flush=True)
close_pools()
"""


@pytest.mark.parametrize("then", ["reset", "drop"])
def test_a_batch_outliving_its_loop_shuts_down_quietly(pg, then):
    """``asyncio.run()`` returns with an async-opened batch neither executed
    nor reset: the transaction rolls back as the loop shuts down, with no
    tracebacks from the shutdown and no "Exception ignored" at GC, whether
    the batch is then reset() or simply dropped (#784 review)."""
    import subprocess
    import sys

    AioNote.create(key="seed")
    db = get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0)
    env = dict(os.environ, REDIS_URL=f"redis://localhost:6379/{db}")
    done = subprocess.run(
        [sys.executable, "-X", "dev", "-c", _OUTLIVE, pg.dsn, pg.schema, then],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "ROWS 1" in done.stdout  # the seed only: rolled back
    for noise in ("Traceback", "Exception ignored", "never retrieved", "Error"):
        assert noise not in done.stderr, done.stderr
