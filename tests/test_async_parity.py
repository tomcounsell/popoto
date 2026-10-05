"""Every routed ``async_*`` method agrees with its sync twin, on both legs
(#759 M5, plan §5 M5 gate (b)).

On the Postgres leg the ``async_*`` methods no longer run their sync twin in a
worker thread: they run it on ``AsyncPostgresBackend``, whose I/O is
``psycopg.AsyncConnection`` on the test's own event loop. Each test here
asserts the async result against the sync twin's on the same leg, from the
same test code, and the ``_no_worker_threads`` fixture makes any thread hop
on the Postgres leg a failure -- so a method that silently fell back to the
shim would fail here rather than pass slowly.

The Redis leg is the oracle: its ``async_*`` paths are unchanged by M5
(``redis.asyncio`` for the query reads, a worker thread for the writes).
"""

import asyncio
import sys

import pytest

from src import popoto
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.models.query import QueryException
import src.popoto.redis_db as redis_db_module

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class AsyncParityItem(popoto.Model):
    name = popoto.KeyField()
    group = popoto.KeyField(default="g")
    rank = popoto.SortedField(type=int, default=0)
    n = popoto.IntField(default=0)
    tag = popoto.Field(type=str, default="")


class AsyncParityTracked(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    tag = popoto.Field(type=str, default="")


MODELS = (AsyncParityItem, AsyncParityTracked)


@pytest.fixture(autouse=True)
def _clean():
    # The async Redis client is bound to an event loop; pytest-asyncio makes a
    # fresh loop per test, so drop the cached connection (see test_async.py).
    redis_db_module._POPOTO_ASYNC_REDIS_DB = None
    redis_db_module._async_redis_lock = asyncio.Lock()
    for model in MODELS:
        model.delete_all()
    yield
    for model in MODELS:
        model.delete_all()


@pytest.fixture(autouse=True)
def _no_worker_threads(backend, monkeypatch):
    """On the Postgres leg, a routed method must not hop to a thread."""
    if backend.is_redis:
        return

    def refuse(*args, **kwargs):
        raise AssertionError("a routed async_* method used a worker thread")

    base = sys.modules[popoto.Model.__module__]
    query = sys.modules[type(AsyncParityItem.query).__module__]
    monkeypatch.setattr(base, "to_thread", refuse)
    monkeypatch.setattr(query, "to_thread", refuse)
    monkeypatch.setattr(asyncio, "to_thread", refuse)


def _seed():
    rows = [("a", "g", 3, 1, "x"), ("b", "g", 1, 2, "y"), ("c", "h", 2, 3, "x")]
    for name, group, rank, n, tag in rows:
        AsyncParityItem.create(name=name, group=group, rank=rank, n=n, tag=tag)


def _names(items):
    return [item.name for item in items]


def _state(item):
    return (item.name, item.group, item.rank, item.n, item.tag)


# -- Model writes -------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_save_returns_what_save_returns():
    sync_new = AsyncParityItem(name="s1", rank=1).save()
    async_new = await AsyncParityItem(name="s2", rank=1).async_save()
    assert async_new == sync_new

    loaded = AsyncParityItem.query.get(name="s2", group="g")
    loaded.n = 9
    sync_again = AsyncParityItem.query.get(name="s1", group="g")
    sync_again.n = 9
    assert await loaded.async_save() == sync_again.save()
    assert AsyncParityItem.query.get(name="s2", group="g").n == 9


@pytest.mark.asyncio
async def test_async_save_passes_update_fields_and_skip_auto_now():
    item = await AsyncParityItem.async_create(name="u", n=1, tag="before")
    item.n = 2
    item.tag = "not saved"
    await item.async_save(update_fields=["n"])
    stored = AsyncParityItem.query.get(name="u", group="g")
    assert (stored.n, stored.tag) == (2, "before")


@pytest.mark.asyncio
async def test_async_create_matches_create():
    made = await AsyncParityItem.async_create(name="c1", rank=5, n=2, tag="t")
    twin = AsyncParityItem.create(name="c2", rank=5, n=2, tag="t")
    assert isinstance(made, AsyncParityItem)
    assert _state(made)[1:] == _state(twin)[1:]
    assert _state(AsyncParityItem.query.get(name="c1", group="g")) == _state(made)


@pytest.mark.asyncio
async def test_async_delete_matches_delete():
    _seed()
    a = AsyncParityItem.query.get(name="a", group="g")
    b = AsyncParityItem.query.get(name="b", group="g")
    assert await a.async_delete() == b.delete()
    assert AsyncParityItem.query.get(name="a", group="g") is None
    assert AsyncParityItem.query.count() == 1


@pytest.mark.asyncio
async def test_async_load_matches_load():
    _seed()
    loaded = await AsyncParityItem.async_load(name="a", group="g")
    assert _state(loaded) == _state(AsyncParityItem.load(name="a", group="g"))
    assert await AsyncParityItem.async_load(name="zz", group="g") is None


@pytest.mark.asyncio
async def test_async_get_or_create_matches_get_or_create():
    first, created = await AsyncParityItem.async_get_or_create(
        name="goc", group="g", defaults={"n": 4}
    )
    assert created is True and first.n == 4
    again, created = await AsyncParityItem.async_get_or_create(
        name="goc", group="g", defaults={"n": 5}
    )
    sync_again, sync_created = AsyncParityItem.get_or_create(
        name="goc", group="g", defaults={"n": 5}
    )
    assert (created, again.n) == (sync_created, sync_again.n) == (False, 4)


@pytest.mark.asyncio
async def test_async_update_or_create_matches_update_or_create():
    made, created = await AsyncParityItem.async_update_or_create(
        name="uoc", group="g", defaults={"n": 1}
    )
    assert created is True
    updated, created = await AsyncParityItem.async_update_or_create(
        name="uoc", group="g", defaults={"n": 2}
    )
    sync_updated, sync_created = AsyncParityItem.update_or_create(
        name="uoc", group="g", defaults={"n": 2}
    )
    assert (created, updated.n) == (sync_created, sync_updated.n) == (False, 2)
    assert AsyncParityItem.query.get(name="uoc", group="g").n == 2


@pytest.mark.asyncio
async def test_async_bulk_create_update_delete():
    made = await AsyncParityItem.async_bulk_create(
        [AsyncParityItem(name=f"k{i}", rank=i) for i in range(5)]
    )
    assert len(made) == 5 and AsyncParityItem.query.count() == 5

    items = AsyncParityItem.query.all()
    updated = await AsyncParityItem.async_bulk_update(items, tag="bulk")
    assert updated == 5
    assert {i.tag for i in AsyncParityItem.query.all()} == {"bulk"}

    deleted = await AsyncParityItem.async_bulk_delete(items[:2])
    assert deleted == 2 and AsyncParityItem.query.count() == 3


@pytest.mark.asyncio
async def test_async_delete_all_matches_delete_all():
    _seed()
    assert await AsyncParityItem.async_delete_all() == 3
    assert AsyncParityItem.query.count() == 0
    assert await AsyncParityItem.async_delete_all() == AsyncParityItem.delete_all()


# -- Query reads --------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_matches_get_on_every_path():
    _seed()
    key = AsyncParityItem.query.get(name="a", group="g").db_key.redis_key
    by_kwargs = await AsyncParityItem.query.async_get(name="a", group="g")
    by_key = await AsyncParityItem.query.async_get(redis_key=key)
    by_filter = await AsyncParityItem.query.async_get(tag="y")
    assert _state(by_kwargs) == _state(AsyncParityItem.query.get(name="a", group="g"))
    assert _state(by_key) == _state(AsyncParityItem.query.get(redis_key=key))
    assert _state(by_filter) == _state(AsyncParityItem.query.get(tag="y"))
    assert await AsyncParityItem.query.async_get(name="zz", group="g") is None
    with pytest.raises(QueryException):
        await AsyncParityItem.query.async_get(tag="x")


@pytest.mark.asyncio
async def test_async_get_many_matches_get_many():
    _seed()
    keys = [
        AsyncParityItem(name="c", group="h").db_key.redis_key,
        AsyncParityItem(name="zz", group="g").db_key.redis_key,
        AsyncParityItem(name="a", group="g").db_key.redis_key,
    ]
    got = await AsyncParityItem.query.async_get_many(keys)
    want = AsyncParityItem.query.get_many(keys)
    assert [i and _state(i) for i in got] == [i and _state(i) for i in want]
    assert got[1] is None
    skipped = await AsyncParityItem.query.async_get_many(keys, skip_none=True)
    assert _names(skipped) == ["c", "a"]
    assert await AsyncParityItem.query.async_get_many([]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"group": "g"},
        {"tag": "x"},
        {"rank__gte": 2},
        {"rank__gte": 1, "order_by": "-rank", "limit": 2},
        {"group": "g", "order_by": "name"},
        {"group": "g", "values": ("name", "n")},
    ],
    ids=["key", "plain", "range", "ranked", "ordered", "values"],
)
async def test_async_filter_matches_filter(kwargs):
    _seed()
    got = await AsyncParityItem.query.async_filter(**dict(kwargs))
    want = list(AsyncParityItem.query.filter(**dict(kwargs)))
    if "values" in kwargs:
        key = lambda row: row["name"]  # noqa: E731
        assert sorted(got, key=key) == sorted(want, key=key)
    elif "order_by" in kwargs:
        assert _names(got) == _names(want)
    else:
        assert sorted(_names(got)) == sorted(_names(want))


@pytest.mark.asyncio
async def test_async_all_matches_all():
    _seed()
    assert sorted(_names(await AsyncParityItem.query.async_all())) == sorted(
        _names(AsyncParityItem.query.all())
    )
    ordered = await AsyncParityItem.query.async_all(order_by="-rank")
    assert _names(ordered) == _names(AsyncParityItem.query.all(order_by="-rank"))
    projected = await AsyncParityItem.query.async_all(values=("name",))
    assert sorted(r["name"] for r in projected) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_async_count_matches_count():
    _seed()
    for kwargs in ({}, {"group": "g"}, {"tag": "x"}, {"rank__lte": 2}):
        assert await AsyncParityItem.query.async_count(
            **kwargs
        ) == AsyncParityItem.query.count(**kwargs), kwargs


@pytest.mark.asyncio
async def test_async_keys_matches_keys():
    _seed()
    got = await AsyncParityItem.query.async_keys()
    assert sorted(got) == sorted(AsyncParityItem.query.keys())
    assert len(got) == 3


# -- read staging (AccessTrackerMixin) ------------------------------------------


@pytest.mark.asyncio
async def test_async_reads_stage_exactly_as_the_sync_ones():
    """async_get / async_get_many / async_filter stage a read and _no_track
    does not -- read back through confirm_access(), which promotes the staged
    reads on either backend.

    ``async_all`` is left out on purpose: on Redis it stages (its native path
    hydrates through ``_async_get_many_objects``, which fires ``on_read``)
    while ``Query.all()`` is non-tracking by design. That predates M5 and is a
    Redis-path quirk M5 does not change (gate (a)); on Postgres ``async_all``
    is ``all()`` and does not stage, as before M5."""
    AsyncParityTracked.create(name="t", tag="k")
    record = AsyncParityTracked.query.get(name="t", _no_track=True)
    assert record.confirm_access() == 0

    await AsyncParityTracked.query.async_get(name="t", _no_track=True)
    assert record.confirm_access() == 0

    await AsyncParityTracked.query.async_get(name="t")
    await AsyncParityTracked.query.async_get_many([record.db_key.redis_key])
    await AsyncParityTracked.query.async_filter(tag="k")
    assert record.confirm_access() == 3


# -- concurrency: 50 tasks on one loop ------------------------------------------


@pytest.mark.asyncio
async def test_fifty_concurrent_tasks_save_query_and_rank():
    """50 tasks on one loop, each saving, reading back, filtering, ranking and
    counting -- interleaved at every await. No errors, and every task sees its
    own write and a consistent ranking."""

    async def task(i):
        item = await AsyncParityItem.async_create(
            name=f"t{i:02d}", group=f"p{i % 5}", rank=i, n=i
        )
        back = await AsyncParityItem.query.async_get(name=item.name, group=item.group)
        assert back is not None and back.n == i
        item.tag = "done"
        await item.async_save()
        same_group = await AsyncParityItem.query.async_filter(group=f"p{i % 5}")
        assert item.name in _names(same_group)
        top = await AsyncParityItem.query.async_filter(
            rank__gte=0, order_by="-rank", limit=3
        )
        ranks = [t.rank for t in top]
        assert ranks == sorted(ranks, reverse=True)
        return await AsyncParityItem.query.async_count(group=f"p{i % 5}")

    counts = await asyncio.gather(*(task(i) for i in range(50)))
    assert all(1 <= c <= 10 for c in counts)
    assert await AsyncParityItem.query.async_count() == 50
    assert await AsyncParityItem.query.async_count(tag="done") == 50
    top = await AsyncParityItem.query.async_filter(
        rank__gte=0, order_by="-rank", limit=3
    )
    assert _names(top) == ["t49", "t48", "t47"]
