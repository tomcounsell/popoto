#!/usr/bin/env python
"""Record the exact Redis wire traffic of every ``async_*`` model/query method.

Why this exists (#759 M5)
-------------------------
M5 routes the ``async_*`` methods of a *Postgres*-bound model to the async
Postgres backend. A Redis-bound model must keep today's async path byte for
byte: the ``redis.asyncio`` reads in ``Query.async_*`` and the worker-thread
writes in ``Model.async_*``. ``scripts/trace_redis_wire.py`` traces the sync
surface only, so this is its async twin: every ``async_*`` method, with each
command recorded from whichever client sent it -- ``S`` for the sync client
(the ``to_thread`` paths), ``A`` for ``redis.asyncio`` -- plus the
Python-visible result or exception.

Method: the same as ``trace_redis_wire.py`` (the #751 review's). The sync
client's RESP serializers and the async connection's ``pack_command`` are
hooked, so every command, including each one queued on a pipeline, is recorded
as the argv tokens redis-py encodes for the wire. ``uuid.uuid4`` and
``time.time`` are frozen; run under ``PYTHONHASHSEED=0``.

Usage -- trace a base tree and the working tree, then compare::

    git archive origin/main src | tar -x -C /tmp/base
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 PYTHONPATH=/tmp/base/src \\
        python scripts/trace_async_redis_wire.py > base.trace
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 \\
        python scripts/trace_async_redis_wire.py > head.trace
    cmp base.trace head.trace

The script refuses to run unless ``REDIS_URL`` names a non-zero database
(CLAUDE.md, #577), and it clears only the keys its own models own.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import threading
import time
import uuid
from typing import Any, Awaitable, Callable

_URL = os.environ.get("REDIS_URL", "")
if not _URL or _URL.rstrip("/").endswith("/0") or _URL.count("/") < 3:
    sys.exit("refusing to run: set REDIS_URL to a non-zero database, e.g. /11")

_uuid_counter = itertools.count(1)
uuid.uuid4 = lambda: uuid.UUID(int=next(_uuid_counter))  # type: ignore[assignment]
_FROZEN = 1_760_000_000.0
time.time = lambda: _FROZEN

import redis.asyncio.connection  # noqa: E402
import redis.connection  # noqa: E402

import popoto  # noqa: E402
import popoto.redis_db as redis_db  # noqa: E402
from popoto.fields.access_tracker import AccessTrackerMixin  # noqa: E402
from popoto.redis_db import get_REDIS_DB  # noqa: E402

if get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0) == 0:
    sys.exit("refusing to run: bound to database 0")

_TRACE: list[tuple[str, list[bytes]]] | None = None
_TRACE_LOCK = threading.Lock()


def _record(source: str, tokens: list[bytes]) -> None:
    if _TRACE is not None:
        with _TRACE_LOCK:
            _TRACE.append((source, tokens))


def _hook_sync(cls: Any) -> None:
    original = cls.pack

    def pack(self: Any, *args: Any) -> Any:
        if _TRACE is not None:
            encode = getattr(self, "encode", None)
            tokens = []
            for arg in args:
                if isinstance(arg, str) and " " in arg and arg.split()[0].isupper():
                    tokens.extend(part.encode() for part in arg.split())
                else:
                    tokens.append(encode(arg) if encode else repr(arg).encode())
            _record("S", tokens)
        return original(self, *args)

    cls.pack = pack


_hook_sync(redis.connection.PythonRespSerializer)
if hasattr(redis.connection, "HiredisRespSerializer"):
    _hook_sync(redis.connection.HiredisRespSerializer)

_async_pack = redis.asyncio.connection.AbstractConnection.pack_command


def _pack_async(self: Any, *args: Any) -> Any:
    if _TRACE is not None:
        parts: tuple[Any, ...] = args
        if isinstance(args[0], str):
            parts = tuple(args[0].encode().split()) + args[1:]
        elif isinstance(args[0], bytes) and b" " in args[0]:
            parts = tuple(args[0].split()) + args[1:]
        _record("A", [self.encoder.encode(a) for a in parts])
    return _async_pack(self, *args)


redis.asyncio.connection.AbstractConnection.pack_command = _pack_async  # type: ignore[method-assign]


class AtUser(popoto.Model):
    name = popoto.KeyField()
    org = popoto.KeyField()
    rank = popoto.SortedField(type=int)
    pscore = popoto.SortedField(type=float, partition_by="org", default=0.0)
    note = popoto.Field(type=str, null=True)
    hits = popoto.IntField(default=0)


class AtTracked(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    tag = popoto.Field(type=str, default="")


class AtTtl(popoto.Model):
    name = popoto.KeyField()
    note = popoto.Field(type=str, null=True)

    class Meta:
        ttl = 600


class AtOrdered(popoto.Model):
    code = popoto.KeyField()
    rank = popoto.SortedField(type=int)

    class Meta:
        order_by = "-rank"


class AtAuto(popoto.Model):
    label = popoto.Field(type=str)
    rank = popoto.SortedField(type=int, default=0)


MODELS = (AtUser, AtTracked, AtTtl, AtOrdered, AtAuto)


def _clear() -> None:
    client = get_REDIS_DB()
    for model in MODELS:
        for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
            client.delete(key)


def _norm(value: Any) -> Any:
    if isinstance(value, popoto.Model):
        fields = {
            name: _norm(getattr(value, name, None))
            for name in sorted(value._meta.fields)
        }
        return [type(value).__name__, _norm(value._redis_key), fields]
    if isinstance(value, redis.client.Pipeline):
        return f"<pipeline {type(value).__name__} queued={len(value.command_stack)}>"
    if isinstance(value, dict):
        return {repr(k): _norm(v) for k, v in sorted(value.items(), key=repr)}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, set):
        return sorted((_norm(v) for v in value), key=repr)
    return repr(value)


SCENARIOS: list[tuple[str, Callable[[], Awaitable[Any]]]] = []


def scenario(fn: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _seed() -> None:
    for name, org, rank, pscore, note, hits in (
        ("ann", "acme", 1, 1.0, "hit", 2),
        ("bob", "acme", 2, 2.0, None, 0),
        ("cat", "beta", 3, 3.0, "hit", 5),
        ("dan", "beta", 4, -4.0, "miss", 1),
    ):
        AtUser(
            name=name, org=org, rank=rank, pscore=pscore, note=note, hits=hits
        ).save()
    for i, code in enumerate(("c1", "c2", "c3")):
        AtOrdered(code=code, rank=i).save()
    AtTracked(name="t1", tag="k").save()


# -- Model.async_* --------------------------------------------------------------


@scenario
async def async_save_new():
    return await AtUser(name="eve", org="acme", rank=9).async_save()


@scenario
async def async_save_existing_sorted_change():
    u = AtUser.query.get(name="ann", org="acme")
    u.rank = 7
    u.note = "changed"
    return await u.async_save()


@scenario
async def async_save_update_fields_and_skip_auto_now():
    u = AtUser.query.get(name="bob", org="acme")
    u.hits = 4
    return await u.async_save(update_fields=["hits"], skip_auto_now=True)


@scenario
async def async_save_with_pipeline():
    pipe = get_REDIS_DB().pipeline()
    result = await AtUser(name="fay", org="acme", rank=11).async_save(pipeline=pipe)
    return [result is pipe, pipe.execute()]


@scenario
async def async_save_migrate_key():
    u = AtUser.query.get(name="dan", org="beta")
    u.org = "gamma"
    return await u.async_save(migrate_key=True)


@scenario
async def async_save_meta_ttl():
    return await AtTtl(name="x", note="n").async_save()


@scenario
async def async_create():
    made = await AtUser.async_create(name="gus", org="beta", rank=12)
    return made


@scenario
async def async_create_auto_key():
    return await AtAuto.async_create(label="auto", rank=3)


@scenario
async def async_delete():
    u = AtUser.query.get(name="gus", org="beta")
    return [await u.async_delete(), await u.async_delete()]


@scenario
async def async_load():
    return [
        await AtUser.async_load(name="ann", org="acme"),
        await AtUser.async_load(name="nobody", org="acme"),
    ]


@scenario
async def async_get_or_create():
    return [
        await AtUser.async_get_or_create(name="cat", org="beta"),
        await AtUser.async_get_or_create(name="hal", org="acme", defaults={"rank": 13}),
    ]


@scenario
async def async_update_or_create():
    return [
        await AtUser.async_update_or_create(
            name="hal", org="acme", defaults={"note": "updated"}
        ),
        await AtUser.async_update_or_create(
            name="ivy", org="acme", defaults={"rank": 14}
        ),
    ]


@scenario
async def async_bulk_create():
    return await AtAuto.async_bulk_create(
        [AtAuto(label=f"b{i}", rank=i) for i in range(3)]
    )


@scenario
async def async_bulk_update():
    items = sorted(AtAuto.query.all(), key=lambda a: a.label)
    return await AtAuto.async_bulk_update(items, rank=99)


@scenario
async def async_bulk_delete():
    items = sorted(AtAuto.query.all(), key=lambda a: a.label)[:2]
    return await AtAuto.async_bulk_delete(items)


@scenario
async def async_check_clean_rebuild_indexes():
    return [
        await AtUser.async_check_indexes(),
        await AtUser.async_clean_indexes(),
        await AtUser.async_rebuild_indexes(),
    ]


@scenario
async def async_delete_all():
    return await AtAuto.async_delete_all()


# -- Query.async_* -----------------------------------------------------------------


@scenario
async def async_get_paths():
    key = AtUser(name="ann", org="acme").db_key.redis_key
    return [
        await AtUser.query.async_get(name="ann", org="acme"),
        await AtUser.query.async_get(redis_key=key),
        await AtUser.query.async_get(note="miss"),
        await AtUser.query.async_get(name="nobody", org="acme"),
    ]


@scenario
async def async_get_multiple_raises():
    return await AtUser.query.async_get(note="hit")


@scenario
async def async_get_tracked_and_no_track():
    return [
        await AtTracked.query.async_get(name="t1"),
        await AtTracked.query.async_get(name="t1", _no_track=True),
        await AtTracked.query.async_get(tag="k"),
    ]


@scenario
async def async_get_many():
    keys = [
        AtUser(name="cat", org="beta").db_key.redis_key,
        AtUser(name="nobody", org="x").db_key.redis_key,
        AtUser(name="ann", org="acme").db_key.redis_key,
    ]
    return [
        await AtUser.query.async_get_many(keys),
        await AtUser.query.async_get_many(keys, skip_none=True),
        await AtUser.query.async_get_many([]),
        await AtTracked.query.async_get_many([AtTracked(name="t1").db_key.redis_key]),
    ]


@scenario
async def async_filter_shapes():
    out = []
    for kwargs in (
        {"org": "acme"},
        {"rank__gte": 2},
        {"rank__lte": 3, "order_by": "-rank", "limit": 2},
        {"org": "beta", "pscore__gte": 0},
        {"note": "hit"},
        {"org": "acme", "values": ("name", "hits")},
        {"org": "acme", "order_by": "name"},
    ):
        out.append(await AtUser.query.async_filter(**kwargs))
    out.append(await AtOrdered.query.async_filter())
    out.append(await AtTracked.query.async_filter(tag="k"))
    return out


@scenario
async def async_all():
    return [
        await AtUser.query.async_all(order_by="name"),
        await AtUser.query.async_all(values=("name",)),
        await AtOrdered.query.async_all(),
    ]


@scenario
async def async_count():
    return [
        await AtUser.query.async_count(),
        await AtUser.query.async_count(org="acme"),
        await AtUser.query.async_count(note="hit"),
        await AtUser.query.async_count(rank__gte=2),
    ]


@scenario
async def async_keys():
    return [
        sorted(await AtUser.query.async_keys()),
        sorted(await AtUser.query.async_keys(catchall=True)),
        sorted(await AtUser.query.async_keys(clean=True)),
    ]


async def main() -> None:
    global _TRACE
    redis_db._POPOTO_ASYNC_REDIS_DB = None
    redis_db._async_redis_lock = asyncio.Lock()
    _clear()
    _seed()
    out = sys.stdout
    for name, fn in SCENARIOS:
        _TRACE = []
        try:
            result = _norm(await fn())
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result = f"!! {type(exc).__name__}: {exc}"
        commands, _TRACE = _TRACE, None
        out.write(f"=== {name} ({len(commands)} commands)\n")
        for source, argv in commands:
            out.write(f"  {source} " + " ".join(repr(tok) for tok in argv) + "\n")
        out.write(f"  -> {result}\n")
    _clear()


if __name__ == "__main__":
    asyncio.run(main())
