"""``[PG-only]`` unique saves under concurrency (#776).

A unique save reads before it writes (``pre_save``'s uniqueness check) and
the ``UNIQUE`` index is the authority behind that read. Before #776 the read
always checked out a *second* pooled connection, even when the save was
handed a unit of work that already held one:

- ``PG_POOL_MAX_SIZE`` concurrent ``transaction()``s (or batches) that each
  saved a unique field held every connection and then waited for a fifth --
  every one of them timed out (``BackendBusyError``) and counted a dropped
  write, where exactly one should win;
- the read could not see the unit's own uncommitted writes, so a record the
  unit had just deleted still "held" its unique value (a spurious conflict),
  and a second save of a value the unit had just claimed went on to abort
  the whole transaction at the index.

The read now runs on the unit's own connection, and the strict-connection
guard (``STRICT_UNIT_CONNECTION``, on for the whole test session in
``tests/conftest.py``) fails any test in which an operation handed a unit
checks out another connection.
"""

import asyncio
import threading

import pytest

import popoto
from popoto.exceptions import ModelException

psycopg = pytest.importorskip("psycopg")

from popoto.fields.constants import Defaults  # noqa: E402


class RaceAccount(popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField()


class RaceItem(popoto.Model):
    sku = popoto.KeyField()
    email = popoto.UniqueField(type=str)


CONFLICT = "^Unique constraint violated: name=x already exists on another instance$"
WORKERS = int(Defaults.PG_POOL_MAX_SIZE)


def _warm(model, **kwargs):
    """Create the table outside any unit (first use runs DDL)."""
    model(**kwargs).save()
    model.query.get(**kwargs).delete()


def _race(target, n=WORKERS):
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        try:
            results[i] = target(i, barrier)
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(300)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return results


def _one_winner(results):
    wins = [r for r in results if not isinstance(r, BaseException)]
    losses = [r for r in results if isinstance(r, BaseException)]
    assert len(wins) == 1, results
    for exc in losses:
        assert isinstance(exc, ModelException), results
        assert "name=x already exists on another instance" in str(exc)


@pytest.mark.parametrize("rounds", [3])
def test_concurrent_autocommit_saves_of_one_unique_value_one_wins(pg, rounds):
    _warm(RaceAccount, name="w", agent="w")
    for r in range(rounds):
        RaceAccount.query.filter(name="x")  # noqa: B018 - keep the table hot

        def save(i, barrier):
            barrier.wait()
            return RaceAccount.create(name="x", agent=f"{r}-{i}")

        _one_winner(_race(save))
        assert RaceAccount.query.count(name="x") == 1
        for record in RaceAccount.query.filter(name="x"):
            record.delete()
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_concurrent_transactions_saving_one_unique_value_one_wins(pg):
    """The #776 review repro: every unit holds a pool connection, so a
    unique read on a second connection could never be served."""
    _warm(RaceAccount, name="w", agent="w")

    def save(i, barrier):
        with pg.transaction() as uow:
            barrier.wait()
            RaceAccount(name="x", agent=str(i)).save(pipeline=uow)
        return i

    _one_winner(_race(save))
    assert RaceAccount.query.count(name="x") == 1
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_concurrent_batches_saving_one_unique_value_one_wins(pg):
    _warm(RaceAccount, name="w", agent="w")

    def save(i, barrier):
        pipe = popoto.batch()
        try:
            # The first write opens the batch's transaction (and its
            # connection); the contended save then joins it.
            RaceAccount(name=f"own-{i}", agent=str(i)).save(pipeline=pipe)
            barrier.wait()
            RaceAccount(name="x", agent=str(i)).save(pipeline=pipe)
            pipe.execute()
        finally:
            pipe.reset()
        return i

    _one_winner(_race(save))
    assert RaceAccount.query.count(name="x") == 1
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.asyncio
async def test_concurrent_async_transactions_saving_one_unique_value_one_wins(pg):
    pytest.importorskip("greenlet")
    from popoto.backends.postgres import aio

    await RaceAccount.async_create(name="w", agent="w")
    twin = aio.get_async_backend(RaceAccount)
    entered = asyncio.Barrier(WORKERS)

    async def save(i):
        async with twin.transaction() as uow:
            await entered.wait()
            await RaceAccount(name="x", agent=str(i)).async_save(pipeline=uow)
        return i

    results = await asyncio.wait_for(
        asyncio.gather(*(save(i) for i in range(WORKERS)), return_exceptions=True),
        60,
    )
    _one_winner(results)
    assert await RaceAccount.query.async_count(name="x") == 1
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.asyncio
async def test_concurrent_async_autocommit_saves_one_wins(pg):
    pytest.importorskip("greenlet")
    await RaceAccount.async_create(name="w", agent="w")

    async def save(i):
        return await RaceAccount.async_create(name="x", agent=str(i))

    results = await asyncio.gather(
        *(save(i) for i in range(WORKERS)), return_exceptions=True
    )
    _one_winner(results)


def test_a_unit_sees_its_own_delete_of_the_holder(pg):
    """Delete the holder of a unique value and claim it, in one unit: the
    read must see the unit's own delete, not the committed row it removed."""
    holder = RaceAccount.create(name="x", agent="old")
    with pg.transaction() as uow:
        holder.delete(pipeline=uow)
        RaceAccount(name="x", agent="new").save(pipeline=uow)
    assert [r.agent for r in RaceAccount.query.filter(name="x")] == ["new"]

    holder = RaceAccount.query.get(name="x", agent="new")
    pipe = popoto.batch()
    holder.delete(pipeline=pipe)
    RaceAccount(name="x", agent="newer").save(pipeline=pipe)
    pipe.execute()
    assert [r.agent for r in RaceAccount.query.filter(name="x")] == ["newer"]


def test_a_second_claim_in_one_batch_is_refused_before_it_writes(pg):
    """The unit's own uncommitted claim is visible to the read: the second
    save raises before it sends anything and leaves the batch healthy, so
    ``execute()`` commits the first -- as a Redis batch applies it."""
    pipe = popoto.batch()
    RaceItem(sku="a", email="e").save(pipeline=pipe)
    with pytest.raises(
        ModelException,
        match="^Unique constraint violated: email=e already exists on another",
    ):
        RaceItem(sku="b", email="e").save(pipeline=pipe)
    pipe.execute()
    assert [i.sku for i in RaceItem.query.all()] == ["a"]
    # ignore_errors: the refusal is logged, the batch still commits.
    pipe = popoto.batch()
    RaceItem(sku="c", email="f").save(pipeline=pipe)
    assert RaceItem(sku="d", email="f").save(pipeline=pipe, ignore_errors=True) in (
        False,
        pipe,
    )
    pipe.execute()
    assert sorted(i.sku for i in RaceItem.query.all()) == ["a", "c"]


def test_the_index_still_decides_a_save_that_races_past_the_read(pg):
    """Two units claim one value; the second's read runs before the first
    commits (READ COMMITTED cannot see it), so the UNIQUE index decides --
    the second waits on the first's uncommitted entry and then gets the
    same ModelException, mapped from 23505."""
    first_saved = threading.Event()
    release = threading.Event()
    errors = []

    def first():
        with pg.transaction() as uow:
            RaceAccount(name="x", agent="1").save(pipeline=uow)
            first_saved.set()
            release.wait(10)

    t = threading.Thread(target=first)
    t.start()
    assert first_saved.wait(10)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        with pg.transaction() as uow:
            RaceAccount(name="x", agent="2").save(pipeline=uow)
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)
    t.join(10)
    assert len(errors) == 1 and isinstance(errors[0], ModelException), errors
    assert isinstance(errors[0].__cause__, psycopg.errors.UniqueViolation)
    assert [r.agent for r in RaceAccount.query.filter(name="x")] == ["1"]


def test_save_delete_churn_raises_no_spurious_conflict(pg):
    """The issue's original workload: four threads repeatedly save() and
    delete() the records that hold three shared unique values. Every thread
    targets the *same* record for a value, so no conflict is ever genuine:
    any ModelException is spurious."""
    import random

    _warm(RaceAccount, name="w", agent="w")
    spurious = []

    def churn(i, barrier):
        rng = random.Random(i)
        barrier.wait()
        for n in range(200):
            record = RaceAccount(name=f"v{rng.randrange(3)}", agent="same")
            try:
                if rng.random() < 0.5:
                    record.save()
                else:
                    record.delete()
            except ModelException as exc:
                spurious.append(exc)
        return i

    results = _race(churn)
    assert all(not isinstance(r, BaseException) for r in results), results
    assert spurious == []
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_delete_and_reclaim_churn_in_units_raises_no_spurious_conflict(pg):
    """Each unit deletes the holder of a value and claims the value with a
    new record. The uniqueness read runs on the unit's connection, so it
    sees the unit's own delete: no conflict is ever raised."""
    _warm(RaceAccount, name="w", agent="w")
    spurious = []

    def churn(i, barrier):
        holder = RaceAccount.create(name=f"v-{i}", agent="0")
        barrier.wait()
        for n in range(1, 60):
            try:
                with pg.transaction() as uow:
                    holder.delete(pipeline=uow)
                    holder = RaceAccount(name=f"v-{i}", agent=str(n))
                    holder.save(pipeline=uow)
            except ModelException as exc:
                spurious.append(exc)
                raise
        return i

    results = _race(churn)
    assert all(not isinstance(r, BaseException) for r in results), results
    assert spurious == []
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.slow  # ~100 s: each real deadlock costs deadlock_timeout (1 s)
def test_opposite_order_unique_claims_stress(pg):
    """4 threads x 40 transactions; in round ``n`` every thread claims the
    same two unique values with its own two records, half of them in the
    opposite order. Each transaction must end in one of the three outcomes
    the backend documents -- committed, ``ModelException`` (a genuine
    conflict, read or index), or ``BackendRetryableError`` (a deadlock
    Postgres broke) -- never a hang, a busy pool, or a guard trip; and the
    index must leave each value with at most one holder."""
    from popoto.backends import BackendRetryableError

    _warm(RaceAccount, name="w", agent="w")
    outcomes = {"ok": 0, "conflict": 0, "retryable": 0}
    other = []
    lock = threading.Lock()

    def work(i, barrier):
        for n in range(40):
            barrier.wait()
            a, b = f"a{n}", f"b{n}"
            first, second = (a, b) if i % 2 else (b, a)
            try:
                with pg.transaction() as uow:
                    RaceAccount(name=first, agent=f"{i}-{n}-1").save(pipeline=uow)
                    RaceAccount(name=second, agent=f"{i}-{n}-2").save(pipeline=uow)
                key = "ok"
            except ModelException:
                key = "conflict"
            except BackendRetryableError:
                key = "retryable"
            except BaseException as exc:  # noqa: BLE001
                other.append(exc)
                continue
            with lock:
                outcomes[key] += 1
        return i

    results = _race(work)
    assert all(not isinstance(r, BaseException) for r in results), results
    assert other == []
    print(f"opposite-order unique claims: {outcomes}")
    assert sum(outcomes.values()) == WORKERS * 40
    for n in range(40):
        assert RaceAccount.query.count(name=f"a{n}") <= 1
        assert RaceAccount.query.count(name=f"b{n}") <= 1
    assert pg.health.ok and pg.health.dropped_writes == 0
