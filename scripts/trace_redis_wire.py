#!/usr/bin/env python
"""Record the exact Redis wire traffic of popoto's M1 model/query surface.

Why this exists (#759 M1a)
--------------------------
The v2 plan moves today's ``Model``/``Query`` bodies into
``popoto.backends.redis.RedisBackend`` and requires that the move change
nothing Redis is sent. The #631 POC learnt that a test suite cannot prove that:
its partial-save ``SADD``/``EXPIRE`` reorder (PR #735 review, B1/B2) and its
``inf`` -> ``+inf`` bound rendering (PR #746 review, TD1) were both invisible
to every test and visible only in a command trace. This script is that trace.

Method (the #751 review's): redis-py's RESP serializers are hooked, so every
command -- including ``MULTI``/``EXEC`` and each command queued on a pipeline --
is recorded as the argv tokens redis-py encodes for the wire (a ``str`` and the
same UTF-8 ``bytes`` are one token, exactly as on the wire). ``uuid.uuid4`` and
``time.time`` are frozen, and the caller runs under ``PYTHONHASHSEED=0`` so set
iteration order is reproducible. Each scenario records its commands *and* the
Python-visible result or exception, so a change that keeps the wire but alters
a return value is caught too.

Usage -- trace a base tree and the working tree, then compare::

    git archive origin/main src | tar -x -C /tmp/base
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 PYTHONPATH=/tmp/base/src \\
        python scripts/trace_redis_wire.py > base.trace
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 \\
        python scripts/trace_redis_wire.py > head.trace
    cmp base.trace head.trace

The script refuses to run unless ``REDIS_URL`` names a non-zero database
(CLAUDE.md, #577), and it clears only the keys its own models own.
"""

from __future__ import annotations

import itertools
import os
import sys
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

_URL = os.environ.get("REDIS_URL", "")
if not _URL or _URL.rstrip("/").endswith("/0") or _URL.count("/") < 3:
    sys.exit("refusing to run: set REDIS_URL to a non-zero database, e.g. /11")

_uuid_counter = itertools.count(1)
uuid.uuid4 = lambda: uuid.UUID(int=next(_uuid_counter))  # type: ignore[assignment]
_FROZEN = 1_760_000_000.0
time.time = lambda: _FROZEN

import redis.connection  # noqa: E402

import popoto  # noqa: E402
from popoto import Q  # noqa: E402
from popoto.redis_db import get_REDIS_DB  # noqa: E402

if get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0) == 0:
    sys.exit("refusing to run: bound to database 0")

_TRACE: list[list[bytes]] | None = None


def _hook(cls: Any) -> None:
    original = cls.pack

    def pack(self: Any, *args: Any) -> Any:
        if _TRACE is not None:
            encode = getattr(self, "encode", None)
            tokens = []
            for arg in args:
                if isinstance(arg, str) and " " in arg and arg.split()[0].isupper():
                    # redis-py splits a multi-word command name ("SCRIPT LOAD")
                    tokens.extend(part.encode() for part in arg.split())
                else:
                    tokens.append(encode(arg) if encode else repr(arg).encode())
            _TRACE.append(tokens)
        return original(self, *args)

    cls.pack = pack


_hook(redis.connection.PythonRespSerializer)
if hasattr(redis.connection, "HiredisRespSerializer"):
    _hook(redis.connection.HiredisRespSerializer)


class TrUser(popoto.Model):
    name = popoto.KeyField()
    org = popoto.KeyField()
    rank = popoto.SortedField(type=int)
    score = popoto.SortedField(type=float, default=0.0)
    pscore = popoto.SortedField(type=float, partition_by="org", default=0.0)
    note = popoto.Field(type=str, null=True)
    hits = popoto.IntField(default=0)
    ratio = popoto.FloatField(default=0.0)
    amount = popoto.DecimalField(null=True)


class TrTtl(popoto.Model):
    name = popoto.KeyField()
    email = popoto.IndexedField(type=str, null=True)
    note = popoto.Field(type=str, null=True)

    class Meta:
        ttl = 600


class TrShort(popoto.Model):
    name = popoto.KeyField()
    note = popoto.Field(type=str, max_length=3)


class TrAuto(popoto.Model):
    label = popoto.Field(type=str)


class TrOrdered(popoto.Model):
    code = popoto.KeyField()
    when = popoto.SortedField(type=datetime)
    label = popoto.Field(type=str, null=True)

    class Meta:
        order_by = "-when"


MODELS = (TrUser, TrTtl, TrShort, TrAuto, TrOrdered)


def _clear() -> None:
    client = get_REDIS_DB()
    for model in MODELS:
        for pattern in (f"*{model.__name__}*",):
            for key in client.scan_iter(match=pattern, count=1000):
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


SCENARIOS: list[tuple[str, Callable[[], Any]]] = []


def scenario(fn: Callable[[], Any]) -> Callable[[], Any]:
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _seed() -> None:
    rows = [
        ("ann", "acme", 1, 0.5, 1.0, "hit", 2, 0.25, Decimal("1.50")),
        ("bob", "acme", 2, 1.5, 2.0, None, 0, 0.0, None),
        ("cat", "beta", 3, 2.5, 3.0, "hit", 5, 1.5, Decimal("2")),
        ("dan", "beta", 4, -1.0, -4.0, "miss", 1, 0.0, None),
    ]
    for name, org, rank, score, pscore, note, hits, ratio, amount in rows:
        TrUser(
            name=name,
            org=org,
            rank=rank,
            score=score,
            pscore=pscore,
            note=note,
            hits=hits,
            ratio=ratio,
            amount=amount,
        ).save()
    for i, code in enumerate(("c1", "c2", "c3")):
        TrOrdered(code=code, when=datetime(2026, 1, 1 + i), label=f"L{i}").save()


# -- save -------------------------------------------------------------------


@scenario
def save_full_new():
    return TrUser(name="eve", org="acme", rank=9, score=3.0).save()


@scenario
def save_full_resave_sorted_change():
    u = TrUser.query.get(name="ann", org="acme")
    u.rank = 7
    u.note = "changed"
    return u.save()


@scenario
def save_external_pipeline():
    pipe = get_REDIS_DB().pipeline()
    a = TrUser(name="fay", org="acme", rank=11).save(pipeline=pipe)
    b = TrUser(name="gus", org="beta", rank=12).save(pipeline=pipe)
    return [a is pipe, b is pipe, pipe.execute()]


@scenario
def save_batch_helper():
    with popoto.batch() as pipe:
        TrUser(name="hal", org="acme", rank=13).save(pipeline=pipe)
        return pipe.execute()


@scenario
def save_partial_existing():
    u = TrUser.query.get(name="bob", org="acme")
    u.note = "partial"
    u.hits = 4
    return u.save(update_fields=["note", "hits"])


@scenario
def save_partial_sorted():
    u = TrUser.query.get(name="bob", org="acme")
    u.rank = 22
    return u.save(update_fields=["rank"])


@scenario
def save_partial_fresh_instance():
    # #735 review B1: a never-saved instance must stay out of the class set.
    r = TrUser(name="ivy", org="acme", rank=1, note="q").save(update_fields=["note"])
    return [r, TrUser.query.count(), sorted(TrUser.query.keys())]


@scenario
def save_partial_external_pipeline():
    u = TrUser.query.get(name="cat", org="beta")
    u.note = "piped"
    pipe = get_REDIS_DB().pipeline()
    r = u.save(update_fields=["note"], pipeline=pipe)
    return [r is pipe, pipe.execute()]


@scenario
def save_partial_ttl_fresh_batch():
    # #735 review B2: EXPIRE must follow the INDEX_SWAP EVAL that creates the hash.
    with popoto.batch() as pipe:
        TrTtl(name="t", email="e@x").save(update_fields=["email"], pipeline=pipe)
        out = pipe.execute()
    return [out, get_REDIS_DB().ttl("TrTtl:t")]


@scenario
def save_partial_ttl_internal():
    r = TrTtl(name="t2", email="f@x", note="n").save(update_fields=["email", "note"])
    return [r, get_REDIS_DB().ttl("TrTtl:t2")]


@scenario
def save_full_ttl():
    r = TrTtl(name="t3", email="g@x").save()
    return [r, get_REDIS_DB().ttl("TrTtl:t3")]


@scenario
def save_empty_update_fields():
    u = TrUser.query.get(name="dan", org="beta")
    return [u.save(update_fields=[]), u.save(update_fields=[], pipeline=None)]


@scenario
def save_key_migration_full():
    u = TrUser.query.get(name="dan", org="beta")
    u.name = "dana"
    return [u.save(migrate_key=True), TrUser.query.get(name="dan", org="beta")]


@scenario
def save_key_migration_external_pipeline():
    u = TrUser.query.get(name="dana", org="beta")
    u.name = "dan"
    pipe = get_REDIS_DB().pipeline()
    u.save(migrate_key=True, pipeline=pipe)
    return [pipe.execute(), TrUser.query.get(name="dan", org="beta")]


@scenario
def save_key_migration_partial():
    TrUser(name="mig", org="acme", rank=5, note="m").save()
    u = TrUser.query.get(name="mig", org="acme")
    u.name = "migb"
    return [u.save(update_fields=["name"], migrate_key=True), u._redis_key]


@scenario
def save_key_migration_partial_pipeline():
    TrUser(name="pmig", org="acme", rank=6).save()
    u = TrUser.query.get(name="pmig", org="acme")
    u.name = "pmigb"
    pipe = get_REDIS_DB().pipeline()
    u.save(update_fields=["name"], migrate_key=True, pipeline=pipe)
    return pipe.execute()


@scenario
def save_key_mutation_refused():
    u = TrUser.query.get(name="dan", org="beta")
    u.name = "nope"
    return u.save()


@scenario
def save_auto_key():
    a = TrAuto(label="x")
    return [a.save(), a._redis_key]


@scenario
def save_ignore_errors():
    u = TrShort(name="bad", note="ok")
    u.note = "too long"
    pipe = get_REDIS_DB().pipeline()
    return [u.save(ignore_errors=True), u.save(ignore_errors=True, pipeline=pipe) is pipe]


@scenario
def save_validation_error():
    u = TrShort(name="bad", note="ok")
    u.note = "too long"
    return u.save()


# -- load ---------------------------------------------------------------------


@scenario
def get_by_kwargs():
    return TrUser.query.get(name="ann", org="acme")


@scenario
def get_by_redis_key():
    return [
        TrUser.query.get(redis_key="TrUser:cat:beta"),
        TrUser.query.get("TrUser:cat:beta"),
    ]


@scenario
def get_by_db_key():
    return TrUser.query.get(db_key=TrUser(name="ann", org="acme").db_key)


@scenario
def get_miss():
    return [TrUser.query.get(name="zzz", org="acme"), TrUser.query.get("TrUser:x:y")]


@scenario
def get_filter_fallback():
    return TrUser.query.get(rank=3)


@scenario
def get_filter_fallback_multiple():
    return TrUser.query.get(org="acme")


@scenario
def model_load():
    return [
        TrUser.load(name="cat", org="beta"),
        TrUser.load(db_key="TrUser:cat:beta"),
    ]


@scenario
def get_many_with_missing():
    keys = ["TrUser:ann:acme", "TrUser:missing:acme", "TrUser:cat:beta"]
    return [
        TrUser.query.get_many(keys),
        TrUser.query.get_many(keys, skip_none=True),
        TrUser.query.get_many([]),
    ]


@scenario
def get_many_single():
    return TrUser.query.get_many(["TrUser:ann:acme"])


@scenario
def get_many_bytes_keys():
    return TrUser.query.get_many(sorted(TrUser.query.keys()))


@scenario
def exists_shapes():
    return [
        TrUser.exists(name="ann", org="acme"),
        TrUser.exists("TrUser:ann:acme"),
        TrUser.exists(redis_key="TrUser:nope:acme"),
        TrUser.exists(TrUser(name="cat", org="beta").db_key),
    ]


@scenario
def load_fields_shapes():
    return [
        TrUser.load_fields("TrUser:ann:acme", "note"),
        TrUser.load_fields("TrUser:ann:acme", "note", "hits", "rank"),
        TrUser.load_fields("TrUser:nope:acme", "note"),
    ]


# -- increment ----------------------------------------------------------------


@scenario
def increment_int_direct():
    u = TrUser.query.get(name="cat", org="beta")
    return [u.atomic_increment("hits", 3), u.hits]


@scenario
def increment_float_direct():
    u = TrUser.query.get(name="cat", org="beta")
    return [u.atomic_increment("ratio", 0.5), u.atomic_increment("ratio", -2)]


@scenario
def increment_decimal_direct():
    u = TrUser.query.get(name="cat", org="beta")
    return u.atomic_increment("amount", Decimal("0.25"))


@scenario
def increment_sorted_direct():
    u = TrUser.query.get(name="cat", org="beta")
    return u.atomic_increment("rank", 2)


@scenario
def increment_pipeline():
    u = TrUser.query.get(name="ann", org="acme")
    pipe = get_REDIS_DB().pipeline()
    r1 = u.atomic_increment("hits", 1, pipeline=pipe)
    r2 = u.atomic_increment("rank", 1, pipeline=pipe)
    return [r1 is pipe, r2 is pipe, pipe.execute(), u.hits, u.rank]


@scenario
def increment_errors():
    out = []
    for call in (
        lambda: TrUser(name="new", org="x").atomic_increment("hits", 1),
        lambda: TrUser.query.get(name="ann", org="acme").atomic_increment("note", 1),
        lambda: TrUser.query.get(name="ann", org="acme").atomic_increment("nope", 1),
        lambda: TrUser.query.get(name="ann", org="acme").atomic_increment("hits", None),
    ):
        try:
            out.append(call())
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            out.append(f"{type(exc).__name__}: {exc}")
    return out


# -- query --------------------------------------------------------------------


@scenario
def filter_key_exact_in():
    return [
        TrUser.query.filter(org="acme").all(),
        TrUser.query.filter(name__in=["ann", "cat", "zzz"]).all(),
        TrUser.query.filter(name__in=[]).all(),
    ]


@scenario
def filter_key_patterns():
    return [
        TrUser.query.filter(name__startswith="a").all(),
        TrUser.query.filter(name__endswith="t").all(),
        TrUser.query.filter(note__isnull=True).all(),
        TrUser.query.filter(name__isnull=False).all(),
    ]


@scenario
def filter_sorted_int_ranges():
    return [
        TrUser.query.filter(rank__gt=1).all(),
        TrUser.query.filter(rank__gte=2, rank__lte=7).all(),
        TrUser.query.filter(rank__lt=3).all(),
        TrUser.query.filter(rank=3).all(),
    ]


@scenario
def filter_sorted_float_ranges():
    return [
        TrUser.query.filter(score__gt=0.5).all(),
        TrUser.query.filter(score__gte=1).all(),
        TrUser.query.filter(score__lte=2.5, score__gt=-1.0).all(),
    ]


@scenario
def filter_sorted_infinite_bounds():
    # #746 review TD1: `inf` vs `+inf` rendering of a user-supplied bound.
    inf = float("inf")
    return [
        TrUser.query.filter(score__lte=inf).all(),
        TrUser.query.filter(score__lt=inf).all(),
        TrUser.query.filter(score__gte=-inf).all(),
        TrUser.query.filter(score__gt=-inf, score__lt=inf).all(),
    ]


@scenario
def filter_partitioned_sorted():
    return [
        TrUser.query.filter(org="acme", pscore__gte=1.0).all(),
        TrUser.query.filter(org="beta", pscore__lt=0).all(),
    ]


@scenario
def filter_q_objects():
    return [
        TrUser.query.filter(Q(name="ann", org="acme") | Q(rank__gte=3)).all(),
        TrUser.query.filter(Q(org="acme") & Q(rank__lte=2)).all(),
        TrUser.query.filter(~Q(org="acme")).all(),
        TrUser.query.filter(Q(org="beta"), rank__gt=0).all(),
    ]


@scenario
def filter_expressions():
    return [
        TrUser.query.filter(TrUser.rank > 2).all(),
        TrUser.query.filter((TrUser.rank >= 1) & (TrUser.score < 2.0)).all(),
    ]


@scenario
def filter_order_limit_values():
    return [
        TrUser.query.filter(org="acme", order_by="rank").all(),
        TrUser.query.filter(org="acme", order_by="-rank", limit=1).all(),
        TrUser.query.filter(rank__gte=0, order_by="-rank", limit=2).all(),
        TrUser.query.filter(rank__gte=0, limit=2).all(),
        TrUser.query.filter(org="beta", values=("name", "rank")).all(),
        TrUser.query.filter(org="beta", values=("name",)).all(),
        TrUser.query.filter(org="acme", order_by="name").all(),
    ]


@scenario
def filter_chain():
    qb = TrUser.query.filter(org="acme")
    return [
        qb.order_by("-rank").limit(2).all(),
        TrUser.query.filter(rank__gte=1).filter(org="beta").values("name").all(),
        TrUser.query.filter(org="acme").first(),
        TrUser.query.filter(org="acme").order_by("rank").last(),
        len(TrUser.query.filter(org="acme")),
        list(TrUser.query.filter(org="beta")),
        bool(TrUser.query.filter(org="nobody")),
    ]


@scenario
def filter_client_side():
    return [
        TrUser.query.filter(note="hit").all(),
        TrUser.query.filter(org="acme", note="hit", limit=1).all(),
    ]


@scenario
def filter_computed_sort():
    return (
        TrUser.query.filter(rank__gte=0)
        .computed_sort(lambda u: (u.hits, u.name), reverse=True)
        .limit(2)
        .all()
    )


@scenario
def filter_invalid_param():
    return TrUser.query.filter(bogus=1).all()


@scenario
def count_shapes():
    return [
        TrUser.query.count(),
        TrUser.query.count(org="acme"),
        TrUser.query.count(rank__gte=2),
        TrUser.query.count(note="hit"),
        TrUser.query.filter(Q(org="acme") | Q(rank__gt=3)).count(),
        TrUser.query.filter(org="beta").limit(1).count(),
    ]


@scenario
def all_shapes():
    return [
        TrUser.query.all(),
        TrUser.query.all(order_by="-rank", limit=2),
        TrUser.query.all(values=("name", "org")),
        TrOrdered.query.all(),
        TrOrdered.query.filter(when__gte=datetime(2026, 1, 2)).all(),
        TrAuto.query.all(),
    ]


@scenario
def keys_shapes():
    return [sorted(TrUser.query.keys()), TrUser.query.filter_for_keys_set(org="acme")]


# -- delete -------------------------------------------------------------------


@scenario
def delete_direct_and_double():
    u = TrUser.query.get(name="eve", org="acme")
    return [u.delete(), u.delete(), TrUser.query.get(name="eve", org="acme")]


@scenario
def delete_pipeline():
    u = TrUser.query.get(name="fay", org="acme")
    pipe = get_REDIS_DB().pipeline()
    r = u.delete(pipeline=pipe)
    return [r is pipe, pipe.execute()]


@scenario
def delete_never_saved():
    return TrUser(name="ghost", org="acme", rank=1).delete()


@scenario
def delete_ttl_indexed():
    return TrTtl.query.get(name="t3").delete()


@scenario
def delete_all_shapes():
    return [
        TrAuto.delete_all(),
        TrOrdered.delete_all(),
        TrUser.delete_all(),
        TrTtl.delete_all(),
        TrUser.query.count(),
    ]


def main() -> None:
    global _TRACE
    _clear()
    _seed()
    out = sys.stdout
    for name, fn in SCENARIOS:
        _TRACE = []
        try:
            result = _norm(fn())
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result = f"!! {type(exc).__name__}: {exc}"
        commands, _TRACE = _TRACE, None
        out.write(f"=== {name} ({len(commands)} commands)\n")
        for argv in commands:
            out.write("  " + " ".join(repr(tok) for tok in argv) + "\n")
        out.write(f"  -> {result}\n")
    _clear()


if __name__ == "__main__":
    main()
