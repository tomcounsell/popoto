#!/usr/bin/env python
"""Benchmark ``async_save()`` and ``async_filter()`` (#759 M5).

Three legs, same model shape, same machine, interleaved per run:

- ``pg-native``: a Postgres-bound model on the async backend (M5): the sync
  code in a greenlet, I/O on ``psycopg.AsyncConnection`` on the loop.
- ``pg-thread``: the same model on the pre-M5 thread shim (``to_thread`` of
  the sync method, on the sync pool) -- what ``main`` did before M5.
- ``redis``: a Redis-bound model's async path (``async_save`` is a worker
  thread on Redis, unchanged; ``async_filter`` is ``redis.asyncio``).

Each run reports the p50/p95 of ``--ops`` sequential awaited calls (latency),
then the wall time of ``--tasks`` concurrent tasks doing a save and a filter
each (throughput under contention on one loop). The load average is printed
with every run, because a shared machine moves these numbers.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/popoto_m5async \\
        python scripts/bench_async.py --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

_URL = os.environ.get("REDIS_URL", "")
if not _URL or _URL.rstrip("/").endswith("/0") or _URL.count("/") < 3:
    sys.exit("refusing to run: set REDIS_URL to a non-zero database, e.g. /10")
if not os.environ.get("POPOTO_POSTGRES_URL"):
    sys.exit("set POPOTO_POSTGRES_URL to a scratch database")
os.environ.setdefault("POPOTO_POSTGRES_SCHEMA", "popoto_bench_async")

import popoto  # noqa: E402
from popoto.backends.postgres import aio  # noqa: E402

GROUPS = 10
PER_GROUP = 50


class BenchAioPg(popoto.Model):
    name = popoto.KeyField()
    group = popoto.KeyField()
    rank = popoto.SortedField(type=int, default=0)
    n = popoto.IntField(default=0)
    note = popoto.Field(type=str, default="")

    class Meta:
        backend = "postgres"


class BenchAioRedis(popoto.Model):
    name = popoto.KeyField()
    group = popoto.KeyField()
    rank = popoto.SortedField(type=int, default=0)
    n = popoto.IntField(default=0)
    note = popoto.Field(type=str, default="")

    class Meta:
        backend = "redis"


def _seed(model) -> None:
    model.delete_all()
    for g in range(GROUPS):
        for i in range(PER_GROUP):
            model.create(name=f"s{g}-{i}", group=f"g{g}", rank=i, n=i, note="x")


def _analyze() -> None:
    from popoto.backends import get_backend

    backend = get_backend(BenchAioPg)
    ts = backend._table(BenchAioPg._meta.spec)
    backend._run(f"ANALYZE {ts.qualified}")


class _ThreadShim:
    """The pre-M5 path: run_async falls back to to_thread of the sync call."""

    def __enter__(self):
        self.saved = aio.async_backend
        aio.async_backend = lambda sync: None
        aio._warned_no_greenlet = True
        return self

    def __exit__(self, *exc):
        aio.async_backend = self.saved


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


async def _latency(model, ops: int, tag: str):
    saves, filters = [], []
    for i in range(ops):
        obj = model(name=f"{tag}-{i}", group="w", rank=i, n=i)
        t0 = time.perf_counter()
        await obj.async_save()
        saves.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        rows = await model.query.async_filter(group=f"g{i % GROUPS}")
        filters.append((time.perf_counter() - t0) * 1000)
        assert len(rows) == PER_GROUP, len(rows)
    return saves, filters


async def _throughput(model, tasks: int, tag: str) -> float:
    async def one(i):
        await model(name=f"{tag}-c{i}", group="w", rank=i).async_save()
        await model.query.async_filter(group=f"g{i % GROUPS}")

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(tasks)))
    return (time.perf_counter() - t0) * 1000


async def _leg(name: str, model, ops: int, tasks: int, run: int):
    # warm: bind, pool, first connections
    await model.query.async_count()
    await _latency(model, 20, f"warm{run}")
    saves, filters = await _latency(model, ops, f"r{run}")
    wall = await _throughput(model, tasks, f"r{run}")
    for obj in await model.query.async_filter(group="w"):
        await obj.async_delete()
    return {
        "leg": name,
        "save_p50": statistics.median(saves),
        "save_p95": _pct(saves, 0.95),
        "filter_p50": statistics.median(filters),
        "filter_p95": _pct(filters, 0.95),
        "concurrent_ms": wall,
    }


async def _run_all(ops: int, tasks: int, run: int):
    results = [await _leg("pg-native", BenchAioPg, ops, tasks, run)]
    with _ThreadShim():
        results.append(await _leg("pg-thread", BenchAioPg, ops, tasks, run))
    results.append(await _leg("redis", BenchAioRedis, ops, tasks, run))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--ops", type=int, default=300)
    parser.add_argument("--tasks", type=int, default=50)
    args = parser.parse_args()

    _seed(BenchAioPg)
    _seed(BenchAioRedis)
    _analyze()
    import platform

    import psycopg

    print(
        f"python {platform.python_version()}  psycopg {psycopg.__version__}  "
        f"ops={args.ops} tasks={args.tasks} filter hydrates {PER_GROUP} rows"
    )
    header = (
        f"{'run':>3} {'load1':>5} {'leg':<9} {'save p50':>8} {'p95':>6} "
        f"{'filter p50':>10} {'p95':>6} {'50 tasks ms':>11}"
    )
    print(header)
    for run in range(1, args.runs + 1):
        load = os.getloadavg()[0]
        for r in asyncio.run(_run_all(args.ops, args.tasks, run)):
            print(
                f"{run:>3} {load:>5.2f} {r['leg']:<9} {r['save_p50']:>8.3f} "
                f"{r['save_p95']:>6.3f} {r['filter_p50']:>10.3f} "
                f"{r['filter_p95']:>6.3f} {r['concurrent_ms']:>11.1f}"
            )
    BenchAioPg.delete_all()
    BenchAioRedis.delete_all()


if __name__ == "__main__":
    main()
