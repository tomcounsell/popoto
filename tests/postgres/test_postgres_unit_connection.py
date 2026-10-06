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
