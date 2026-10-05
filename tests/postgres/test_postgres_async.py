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
import threading
import time

import numpy as np
import pytest

import popoto
from popoto import DecayingSortedField, ValidityField, ValidityValidFromConflictError
from popoto.backends import (
    AsyncBackend,
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

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("greenlet")

from popoto.backends.postgres import aio  # noqa: E402


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
    monkeypatch.setattr(type(pg), "_server_facts", lambda self: (170005, "UTF8"))
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
