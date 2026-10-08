"""``[PG-only]`` work popoto does *for* an open unit stays on its connection
(#776, PR #793 review).

Three paths still took a second connection while a ``transaction()`` (or a
``popoto.batch()`` holding Postgres writes) was open:

- ``AppendOnlyMixin.hard_delete(..., pipeline=uow)`` cleared the neighbour's
  chain link with two autocommit statements: a caller that rolled the unit
  back kept the erased record but lost the link, already committed;
- the never-record tombstone of a save refused inside a unit was an
  autocommit write, and its ``except Exception: pass`` swallowed the strict
  guard's ``SecondConnectionError``, so the tombstone was silently dropped;
- first-use DDL (``_check_table``, the engine side tables) checked out a
  pooled connection inside the unit: with the pool full of open units on a
  cold model, every one of them failed with ``BackendBusyError``.

``STRICT_UNIT_CONNECTION`` is on for the whole session (``tests/conftest.py``),
so a regression on any of these paths fails here as ``SecondConnectionError``.
"""

import asyncio
import threading
import time

import pytest

import popoto
from popoto.backends.types import BackendBusyError
from popoto.fields.constants import Defaults
from popoto.privacy.never_record import NeverRecordMixin
from popoto.recipes.provenance_journal import (
    VALIDITY_FIELD_NAME,
    JournalEntry,
    ProvenanceJournal,
)

psycopg = pytest.importorskip("psycopg")

F = VALIDITY_FIELD_NAME
WORKERS = int(Defaults.PG_POOL_MAX_SIZE)
SECRET = "my key is sk-ant-api03-" + "A" * 40


class UnitPrivate(NeverRecordMixin, popoto.Model):
    name = popoto.KeyField()
    content = popoto.StringField(default="")


class ColdAccount(popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField()


# -- hard_delete --------------------------------------------------------------


def _links(pg, key):
    ts = pg._table(JournalEntry._meta.spec)
    rows, _ = pg._run(
        f'SELECT "{F}__supersedes", "{F}__superseded_by" FROM {ts.qualified} '
        'WHERE "_pk" = %s',
        [key],
    )
    return rows[0] if rows else None


def _chain():
    t0 = 1_700_000_000.0
    first = ProvenanceJournal.append(agent_id="hd", statement="s1", at=t0).entry
    second = ProvenanceJournal.supersede(
        first, agent_id="hd", statement="s2", at=t0 + 50
    ).entry
    return first, second, first.db_key.redis_key, second.db_key.redis_key


class _Abort(Exception):
    pass


def test_hard_delete_rolled_back_in_a_transaction_leaves_both_records(pg):
    first, second, k1, k2 = _chain()
    with pytest.raises(_Abort):
        with pg.transaction() as uow:
            assert JournalEntry.hard_delete(second, pipeline=uow) is True
            raise _Abort
    assert _links(pg, k2) == (k1, None)
    assert _links(pg, k1) == (None, k2)  # the link came back with the record


def test_hard_delete_committed_in_a_transaction_clears_the_link(pg):
    first, second, k1, k2 = _chain()
    with pg.transaction() as uow:
        assert JournalEntry.hard_delete(second, pipeline=uow) is True
        # Inside the unit, the unit's own delete and link update are visible.
        assert pg._run(
            f"SELECT 1 FROM {pg._table(JournalEntry._meta.spec).qualified} "
            'WHERE "_pk" = %s',
            [k2],
            uow=uow,
        ) == ([], 0)
    assert _links(pg, k2) is None
    assert _links(pg, k1) == (None, None)


def test_hard_delete_in_a_batch_rolls_back_with_reset_and_commits_with_execute(pg):
    first, second, k1, k2 = _chain()
    pipe = popoto.batch()
    try:
        assert JournalEntry.hard_delete(second, pipeline=pipe) is True
    finally:
        pipe.reset()
    assert _links(pg, k2) == (k1, None)
    assert _links(pg, k1) == (None, k2)

    pipe = popoto.batch()
    try:
        assert JournalEntry.hard_delete(second, pipeline=pipe) is True
        pipe.execute()
    finally:
        pipe.reset()
    assert _links(pg, k2) is None
    assert _links(pg, k1) == (None, None)


@pytest.mark.asyncio
async def test_hard_delete_in_an_async_transaction_rolls_back_and_commits(pg):
    pytest.importorskip("greenlet")
    from popoto.backends.postgres import aio

    first, second, k1, k2 = _chain()
    twin = aio.get_async_backend(JournalEntry)
    with pytest.raises(_Abort):
        async with twin.transaction() as uow:
            assert await twin.run(JournalEntry.hard_delete, second, pipeline=uow)
            raise _Abort
    assert _links(pg, k2) == (k1, None)
    assert _links(pg, k1) == (None, k2)

    async with twin.transaction() as uow:
        assert await twin.run(JournalEntry.hard_delete, second, pipeline=uow)
    assert _links(pg, k2) is None
    assert _links(pg, k1) == (None, None)


# -- the never-record tombstone -----------------------------------------------


def _counts_in(pg, uow):
    return pg.field_call(UnitPrivate._meta.spec, "_never_record", "counts", uow=uow)


def test_a_refused_save_writes_its_tombstone_in_the_transaction(pg):
    """The tombstone is written on the unit's connection: visible inside
    it, gone when it rolls back, kept when it commits. Cold engine tables,
    so their first-use DDL runs inside the unit too."""
    with pytest.raises(_Abort):
        with pg.transaction() as uow:
            assert UnitPrivate(name="a", content=SECRET).save(pipeline=uow) is uow
            assert sum(_counts_in(pg, uow).values()) == 1
            raise _Abort
    assert UnitPrivate.never_record_counts() == {}
    assert UnitPrivate.never_record_log() == []

    with pg.transaction() as uow:
        assert UnitPrivate(name="b", content=SECRET).save(pipeline=uow) is uow
    assert sum(UnitPrivate.never_record_counts().values()) == 1
    assert len(UnitPrivate.never_record_log()) == 1
    assert UnitPrivate.query.get(name="b") is None


def test_a_refused_save_writes_its_tombstone_in_the_batch(pg):
    pipe = popoto.batch()
    try:
        UnitPrivate(name="ok", content="nothing secret").save(pipeline=pipe)
        UnitPrivate(name="a", content=SECRET).save(pipeline=pipe)
    finally:
        pipe.reset()
    assert UnitPrivate.never_record_counts() == {}

    pipe = popoto.batch()
    try:
        UnitPrivate(name="ok", content="nothing secret").save(pipeline=pipe)
        UnitPrivate(name="a", content=SECRET).save(pipeline=pipe)
        pipe.execute()
    finally:
        pipe.reset()
    assert sum(UnitPrivate.never_record_counts().values()) == 1
    assert UnitPrivate.query.get(name="ok") is not None


def test_the_tombstone_never_swallows_a_second_connection_error(pg, monkeypatch):
    """The best-effort ``except`` is narrowed to the backend's outage
    classes: the strict guard's ``SecondConnectionError`` (an
    ``AssertionError``) propagates, and so does an outage inside a unit,
    whose transaction it has already aborted. An outage on an autocommit
    write is still swallowed: the save is refused either way."""
    from popoto.backends.postgres import SecondConnectionError
    from popoto.backends.types import BackendUnavailableError

    def boom(exc):
        def call(*args, **kwargs):
            raise exc

        return call

    monkeypatch.setattr(pg, "field_call", boom(SecondConnectionError("strict")))
    with pytest.raises(SecondConnectionError):
        UnitPrivate(name="a", content=SECRET).save()

    monkeypatch.setattr(pg, "field_call", boom(BackendUnavailableError("down")))
    assert UnitPrivate(name="a", content=SECRET).save() is False
    with pytest.raises(BackendUnavailableError):
        with pg.transaction() as uow:
            UnitPrivate(name="a", content=SECRET).save(pipeline=uow)


# -- first-use DDL inside a unit (cold start) ---------------------------------


def _cold_sync(pg, n, duration=0.0, *, existing=False):
    """``n`` threads, each holding a ``transaction()`` before the model's
    first use in this process; returns ``(ok, errors)``."""
    if existing:
        ColdAccount(name="warm", agent="w").save()
        pg.forget_tables()  # a new process: the table exists, the memo is cold
    barrier = threading.Barrier(n)
    ok = [0] * n
    errors: list[BaseException] = []
    start = time.monotonic()

    # Up to the pool size, every unit holds its slot before the first use;
    # past it, the extra units could never enter, so all start together.
    inside = n <= WORKERS

    def worker(i):
        k = 0
        if not inside:
            barrier.wait()
        while True:
            k += 1
            try:
                with pg.transaction() as uow:
                    if k == 1 and inside:
                        barrier.wait()  # every unit holds a pool slot first
                    ColdAccount(name=f"n{i}-{k}", agent=str(i)).save(pipeline=uow)
                ok[i] += 1
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)
            if time.monotonic() - start >= duration:
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(duration + 120)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return sum(ok), errors


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
def test_cold_start_sync_units_fill_the_pool_and_all_commit(pg, existing):
    """The review repro: ``PG_POOL_MAX_SIZE`` units on a cold model. Before,
    one in four failed with ``BackendBusyError`` (after a 5 s wait)."""
    ok, errors = _cold_sync(pg, WORKERS, existing=existing)
    assert errors == [] and ok == WORKERS
    assert ColdAccount.query.count() == WORKERS + (1 if existing else 0)
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
@pytest.mark.asyncio
async def test_cold_start_async_units_fill_the_pool_and_all_commit(pg, existing):
    """Before: all four failed with ``BackendBusyError`` on a fresh model."""
    pytest.importorskip("greenlet")
    from popoto.backends.postgres import aio

    if existing:
        ColdAccount(name="warm", agent="w").save()
        pg.forget_tables()
    twin = aio.get_async_backend(ColdAccount)
    entered = asyncio.Barrier(WORKERS)

    async def unit(i):
        async with twin.transaction() as uow:
            await entered.wait()
            await ColdAccount(name=f"n{i}", agent=str(i)).async_save(pipeline=uow)
        return i

    results = await asyncio.wait_for(
        asyncio.gather(*(unit(i) for i in range(WORKERS)), return_exceptions=True),
        120,
    )
    assert results == list(range(WORKERS))
    assert ColdAccount.query.count() == WORKERS + (1 if existing else 0)
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_cold_start_with_more_units_than_the_pool(pg):
    """Twice the pool's units on a fresh model, one round each: the ones
    waiting for a slot get one as the others commit; none is starved."""
    ok, errors = _cold_sync(pg, 2 * WORKERS)
    assert not [e for e in errors if isinstance(e, BackendBusyError)], errors
    assert errors == [] and ok == 2 * WORKERS


@pytest.mark.slow
def test_cold_start_sustained_units_for_thirty_seconds(pg):
    """The review's 8 x 30 s shape (pool 4): before, 27 of the units failed
    with ``BackendBusyError``."""
    ok, errors = _cold_sync(pg, 2 * WORKERS, duration=30.0)
    assert [type(e).__name__ for e in errors] == []
    assert ok > 2 * WORKERS


def test_an_existing_table_needs_no_ddl_connection_inside_a_unit(pg, monkeypatch):
    """A table that exists and is current is settled by catalog reads on the
    unit's own connection: no dedicated connection, no pooled one."""
    ColdAccount(name="warm", agent="w").save()
    pg.forget_tables()

    def refuse(*args, **kwargs):
        raise AssertionError("first use opened a DDL connection for a current table")

    monkeypatch.setattr(pg, "_dedicated_connection", refuse)
    with pg.transaction() as uow:
        ColdAccount(name="a", agent="a").save(pipeline=uow)
    assert ColdAccount.query.count() == 2


def test_a_missing_table_inside_a_unit_is_created_outside_it(pg):
    """The DDL commits on its own connection: a unit that rolls back after a
    model's first use leaves the table (and the memo naming it) in place."""
    with pytest.raises(_Abort):
        with pg.transaction() as uow:
            ColdAccount(name="a", agent="a").save(pipeline=uow)
            raise _Abort
    assert ColdAccount.query.count() == 0  # the table is there; the row is not
    ColdAccount(name="b", agent="b").save()
    assert ColdAccount.query.count() == 1


# -- first-use DDL never waits on its own unit's locks (PR #793 re-review) -----


def _redefined(name, *, extra):
    """A model class named ``name``; ``extra`` adds one field, as a redeploy
    that grew the model would (a fresh class each call: a fresh spec)."""
    ns: dict = {}
    exec(  # noqa: S102 - a test-local class definition
        "import popoto\n"
        f"class {name}(popoto.Model):\n"
        "    name = popoto.KeyField()\n"
        + ("    extra = popoto.Field(null=True)\n" if extra else ""),
        ns,
    )
    return ns[name]


def _bounded(fn, seconds):
    """Run ``fn`` on a thread; return ``(result, exc, elapsed)``, failing the
    test (not hanging it) if it outlives ``seconds``."""
    out: dict = {}

    def target():
        t0 = time.monotonic()
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the assert
            out["exc"] = exc
        out["elapsed"] = time.monotonic() - t0

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"hung for {seconds}s"
    return out.get("result"), out.get("exc"), out["elapsed"]


def _other_thread_first_use(pg):
    """Another thread's first use of a cold model completes promptly: the
    backend's first-use lock was released."""
    Fresh = _redefined("DdlBystander", extra=False)
    pg.forget_tables()
    _, exc, elapsed = _bounded(lambda: Fresh(name="x").save(), 30)
    assert exc is None, exc
    assert elapsed < 5


def test_a_field_added_after_the_unit_used_its_table_is_refused_at_once(pg):
    """Review shape (a): the model gains a field after the unit has saved to
    its table. The ``ALTER TABLE`` would run on the DDL connection and wait
    for the unit's own ``RowExclusiveLock`` forever (it hung on main too)."""
    from popoto.backends.types import SchemaDriftError

    Before = _redefined("DdlGrown", extra=False)
    Before(name="warm").save()

    def unit():
        with pg.transaction() as uow:
            Before(name="a").save(pipeline=uow)
            After = _redefined("DdlGrown", extra=True)
            After(name="b", extra="x").save(pipeline=uow)

    _, exc, elapsed = _bounded(unit, 30)
    assert isinstance(exc, SchemaDriftError), exc
    assert "outside the unit" in str(exc) and "RowExclusiveLock" in str(exc)
    assert elapsed < Defaults.PG_DDL_LOCK_TIMEOUT_MS / 1000.0
    assert pg.health.ok and pg.health.dropped_writes == 0
    _other_thread_first_use(pg)

    # Outside a unit the same change migrates, and the unit's work can rerun.
    After = _redefined("DdlGrown", extra=True)
    After(name="b", extra="x").save()
    with pg.transaction() as uow:
        After(name="a").save(pipeline=uow)
    assert sorted(o.name for o in After.query.all()) == ["a", "b", "warm"]


def test_an_auto_key_table_bound_before_its_first_instance_needs_no_ddl(pg):
    """Review shape (b), closed by #826: an auto-key model's table bound by a
    query before any instance existed. It used to lack ``_auto_key`` (the
    first instance added the field), so a unit that read it and then saved
    needed ``ADD COLUMN "_auto_key"`` under its own lock and was refused. The
    field is registered at class creation now: the query binds the full
    table, and the unit reads and saves with no schema change."""

    class DdlAutoKeyed(popoto.Model):
        title = popoto.Field(null=True)

    assert DdlAutoKeyed._meta.key_field_names == {"_auto_key"}
    assert DdlAutoKeyed.query.count() == 0  # binds the table
    bound = pg._table(DdlAutoKeyed._meta.spec)
    assert "_auto_key" in bound.column_map()

    def unit():
        with pg.transaction() as uow:
            with pg.reads_on(uow):
                assert DdlAutoKeyed.query.count() == 0
            DdlAutoKeyed(title="t").save(pipeline=uow)

    _, exc, _elapsed = _bounded(unit, 30)
    assert exc is None, exc
    assert pg._table(DdlAutoKeyed._meta.spec) is bound  # no rebind, no DDL
    assert DdlAutoKeyed.query.count() == 1


def test_ddl_waiting_on_another_sessions_lock_times_out(pg, admin, monkeypatch):
    """A lock held by *another* session cannot be refused up front: the
    DDL connection's ``lock_timeout`` turns that wait into
    ``BackendRetryableError`` within ``PG_DDL_LOCK_TIMEOUT_MS`` -- not an
    outage -- and the first-use lock is released for every other thread."""
    from popoto.backends.types import BackendRetryableError

    monkeypatch.setattr(Defaults, "PG_DDL_LOCK_TIMEOUT_MS", 300)
    Before = _redefined("DdlForeign", extra=False)
    Before(name="warm").save()
    table = pg._table(Before._meta.spec).qualified
    After = _redefined("DdlForeign", extra=True)

    def unit():
        with pg.transaction() as uow:
            After(name="b", extra="x").save(pipeline=uow)

    with admin.transaction():
        admin.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
        _, exc, elapsed = _bounded(unit, 30)
        assert isinstance(exc, BackendRetryableError), exc
        assert "PG_DDL_LOCK_TIMEOUT_MS" in str(exc)
        assert 0.3 <= elapsed < 5
        assert pg.health.ok and pg.health.dropped_writes == 0
        _other_thread_first_use(pg)
    # The foreign transaction ended: the retry migrates and commits.
    _, exc, _ = _bounded(unit, 30)
    assert exc is None, exc
    assert After.query.get(name="b").extra == "x"


@pytest.mark.asyncio
async def test_an_async_unit_is_refused_at_once_too(pg):
    """The async bridge's units are tracked per task and their connections
    are bridged ``AsyncConnection``s: the same refusal, no wait."""
    pytest.importorskip("greenlet")
    from popoto.backends.postgres import aio
    from popoto.backends.types import SchemaDriftError

    Before = _redefined("DdlGrownAsync", extra=False)
    Before(name="warm").save()
    twin = aio.get_async_backend(Before)
    t0 = time.monotonic()
    with pytest.raises(SchemaDriftError, match="outside the unit"):
        async with twin.transaction() as uow:
            await Before(name="a").async_save(pipeline=uow)
            After = _redefined("DdlGrownAsync", extra=True)
            await asyncio.wait_for(
                After(name="b", extra="x").async_save(pipeline=uow), 30
            )
    assert time.monotonic() - t0 < Defaults.PG_DDL_LOCK_TIMEOUT_MS / 1000.0


def test_a_database_error_from_the_tombstone_is_a_popoto_error(pg, admin):
    """A real database error from the tombstone write -- here the audit log
    table dropped under a warm memo -- reaches the caller as popoto's
    ``BackendError`` (the driver error is its ``__cause__``), not as raw
    psycopg, inside a unit and on an autocommit save alike."""
    from popoto.backends.types import BackendError

    UnitPrivate(name="a", content=SECRET).save()  # warms the audit tables
    admin.execute(f'DROP TABLE "{pg.schema}".popoto_never_record_log')
    with pytest.raises(BackendError) as caught:
        with pg.transaction() as uow:
            UnitPrivate(name="b", content=SECRET).save(pipeline=uow)
    assert isinstance(caught.value.__cause__, psycopg.errors.UndefinedTable)
    with pytest.raises(BackendError) as caught:
        UnitPrivate(name="c", content=SECRET).save()
    assert isinstance(caught.value.__cause__, psycopg.errors.UndefinedTable)
    assert UnitPrivate.query.get(name="b") is None


# -- every DDL statement runs under timeouts, on any connection (PR #793) -------


def _session_settings(pg):
    """The pooled connections' session ``lock_timeout`` / ``statement_timeout``
    as the server reports them."""
    with pg._connection() as conn:
        return (
            conn.execute("SHOW lock_timeout").fetchone()[0],
            conn.execute("SHOW statement_timeout").fetchone()[0],
        )


def test_pooled_ddl_waiting_on_an_open_unit_cannot_deadlock_the_first_use_lock(
    pg, monkeypatch
):
    """The cross-thread shape. Thread B has a unit open that saved to model M
    (so it holds a lock on M's table). Thread A, with no unit of its own,
    triggers M's schema change: pooled-connection DDL that waits on B's lock
    *while holding the backend's first-use lock*. B then needs that lock for
    a fresh model N. Without a ``lock_timeout`` on the pooled connection A
    waits on B and B on A forever; with it A gives up with
    ``BackendRetryableError`` and B proceeds."""
    from popoto.backends.types import BackendRetryableError

    monkeypatch.setattr(Defaults, "PG_DDL_LOCK_TIMEOUT_MS", 400)
    Old = _redefined("DdlCross", extra=False)
    Old(name="warm").save()
    New = _redefined("DdlCross", extra=True)
    Fresh = _redefined("DdlCrossFresh", extra=False)
    settings = _session_settings(pg)

    b_saved = threading.Event()
    out: dict = {}

    def thread_b():
        try:
            with pg.transaction() as uow:
                Old(name="b").save(pipeline=uow)  # RowExclusiveLock on M
                b_saved.set()
                time.sleep(0.2)  # let A reach its DDL and block on that lock
                Fresh(name="n").save(pipeline=uow)  # needs the first-use lock
            out["b"] = None
        except BaseException as exc:  # noqa: BLE001
            out["b"] = exc

    def thread_a():
        b_saved.wait(10)
        t0 = time.monotonic()
        try:
            New(name="a", extra="x").save()
        except BaseException as exc:  # noqa: BLE001
            out["a"] = exc
        out["a_elapsed"] = time.monotonic() - t0

    ta = threading.Thread(target=thread_a, daemon=True)
    tb = threading.Thread(target=thread_b, daemon=True)
    ta.start()
    tb.start()
    ta.join(30)
    tb.join(30)
    assert not ta.is_alive() and not tb.is_alive(), "deadlocked"
    assert isinstance(out.get("a"), BackendRetryableError), out
    assert "PG_DDL_LOCK_TIMEOUT_MS" in str(out["a"])
    assert out["a_elapsed"] < 5
    assert out["b"] is None, out["b"]  # B got the first-use lock and committed
    assert Fresh.query.get(name="n").name == "n"
    assert pg.health.ok and pg.health.dropped_writes == 0
    # The pooled connection A used was not left modified (SET LOCAL).
    assert _session_settings(pg) == settings
    # Nothing was cached for M: the next use rechecks, and migrates.
    New(name="a", extra="x").save()
    assert New.query.get(name="a").extra == "x"
    assert _session_settings(pg) == settings


@pytest.mark.asyncio
async def test_async_pooled_ddl_waiting_on_an_open_unit_times_out(pg, monkeypatch):
    """Async: task B holds a unit that saved to M; task A, with no unit,
    triggers M's schema change and waits on B's lock. B commits only after A
    finishes, so without a timeout on the pooled connection neither ends."""
    pytest.importorskip("greenlet")
    from popoto.backends.postgres import aio
    from popoto.backends.types import BackendRetryableError

    monkeypatch.setattr(Defaults, "PG_DDL_LOCK_TIMEOUT_MS", 400)
    Old = _redefined("DdlCrossAsync", extra=False)
    Old(name="warm").save()
    New = _redefined("DdlCrossAsync", extra=True)
    twin = aio.get_async_backend(Old)
    settings = _session_settings(pg)
    b_saved, a_done = asyncio.Event(), asyncio.Event()

    async def task_b():
        async with twin.transaction() as uow:
            await Old(name="b").async_save(pipeline=uow)
            b_saved.set()
            await asyncio.wait_for(a_done.wait(), 30)

    async def task_a():
        await b_saved.wait()
        t0 = time.monotonic()
        try:
            with pytest.raises(BackendRetryableError):
                await asyncio.wait_for(New(name="a", extra="x").async_save(), 30)
            assert time.monotonic() - t0 < 5
        finally:
            a_done.set()

    await asyncio.wait_for(asyncio.gather(task_b(), task_a()), 60)
    assert _session_settings(pg) == settings


def test_engine_table_ddl_waiting_on_the_schema_lock_times_out(pg, admin, monkeypatch):
    """Engine side tables (recall, recipes, streams) run their DDL under the
    same timeouts, with or without a unit open: a session holding the
    schema's DDL advisory lock turns the wait into
    ``BackendRetryableError``, nothing is memoised, and the retry works."""
    from popoto.backends.postgres.events import STREAM_TABLES
    from popoto.backends.postgres.memory import RECALL_TABLE
    from popoto.backends.postgres.recipes import ENGINE_TABLES
    from popoto.backends.postgres.schema import schema_lock_key
    from popoto.backends.types import BackendRetryableError

    # The wait for the schema lock runs under the long backstop, not the DDL
    # lock timeout (tests/postgres/test_postgres_rolling_deploy.py).
    monkeypatch.setattr(Defaults, "PG_DDL_SCHEMA_LOCK_TIMEOUT_MS", 300)
    recipe = next(iter(ENGINE_TABLES))
    for name in (*STREAM_TABLES, RECALL_TABLE, recipe):
        admin.execute(f'DROP TABLE IF EXISTS "{pg.schema}"."{name}" CASCADE')
    pg.forget_tables()
    settings = _session_settings(pg)
    calls = {
        "recall": pg._recall_table,
        "recipe": lambda: pg._engine(recipe),
        "events": pg._events_ready,
    }
    for in_unit in (False, True):
        for label, call in calls.items():

            def run():
                if not in_unit:
                    return call()
                with pg.transaction():
                    return call()

            with admin.transaction():
                admin.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (schema_lock_key(pg.schema),),
                )
                _, exc, elapsed = _bounded(run, 30)
                assert isinstance(exc, BackendRetryableError), (label, in_unit, exc)
                assert 0.3 <= elapsed < 5
            assert pg.health.ok and pg.health.dropped_writes == 0
    assert _session_settings(pg) == settings
    for call in calls.values():  # nothing cached: the retry creates them
        _, exc, _ = _bounded(call, 30)
        assert exc is None, exc
    assert _session_settings(pg) == settings
