#!/usr/bin/env python
"""Model-level latency: Redis vs the v2 Postgres backend (#759 M1b).

Ported from the #631 archive (``poc/backend-seam``,
``scripts/bench_backend_seam.py``) to the v2 seam: there is no
protocol-level section any more -- the protocol *is* model-level -- so every
row drives the public API (``Model.save()``, ``Query.filter`` + hydration,
``Query.get``) on one model, bound to each backend in turn with
``popoto.backends.set_backend``.

Method (plan §6, WS4 §4 lessons): seed N records, ``ANALYZE`` the Postgres
table after seeding (the archive's two bimodal rows were a missing
``ANALYZE``), warm up, then time each op ``--runs`` times (default 3) of
``--iters`` iterations and report every run's p50 and the min-max range.
The M1 exit criteria compared are ``save`` p50 <= 2x Redis and
``filter+hydrate`` p50 <= 1x Redis.

Safety: Redis is bound from ``REDIS_URL`` *before* importing popoto
(CLAUDE.md, #577) and database 0 is refused. On Postgres the script creates
its own ``popoto_test_<uuid4().hex>`` schema and drops only that; it never
touches ``public``. The DSN comes from ``POPOTO_POSTGRES_URL``, the variable
the library itself reads.

Usage::

    REDIS_URL=redis://localhost:6379/14 \\
    POPOTO_POSTGRES_URL=postgresql://localhost:5432/postgres \\
    python scripts/bench_backend_seam.py [--n 2000] [--iters 300] [--runs 3]
"""

from __future__ import annotations

import argparse
import datetime
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")


def _redis_db_number(url: str) -> int:
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if path:
        return int(path)
    qs = parse_qs(parts.query).get("db")
    return int(qs[0]) if qs else 0


if _redis_db_number(REDIS_URL) == 0:
    sys.exit(f"refusing to bench against DB 0 ({REDIS_URL!r}); name a non-zero DB")
PG_URL = os.environ.get("POPOTO_POSTGRES_URL", "")
if not PG_URL:
    sys.exit("set POPOTO_POSTGRES_URL=postgresql://host:port/db")

import psycopg  # noqa: E402

import popoto  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.backends.postgres import PostgresBackend  # noqa: E402
from popoto.backends.redis import RedisBackend  # noqa: E402

UTC = datetime.timezone.utc


class BenchNote(popoto.Model):
    """The M1 plain-model slice: keys, scalars, a datetime and a sorted field."""

    owner = popoto.KeyField()
    slug = popoto.KeyField()
    hits = popoto.IntField(default=0)
    weight = popoto.FloatField(default=1.0)
    body = popoto.StringField(null=True)
    seen = popoto.DatetimeField(null=True)
    score = popoto.SortedField(type=float, default=0.0)


def _p(samples_ns: list[int], q: float) -> float:
    s = sorted(samples_ns)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))] / 1_000.0


def _timeit(fn: Callable[[int], Any], iters: int, offset: int) -> list[int]:
    out: list[int] = []
    for i in range(iters):
        t0 = time.perf_counter_ns()
        fn(offset + i)
        out.append(time.perf_counter_ns() - t0)
    return out


def _seed(n: int) -> None:
    now = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        BenchNote(
            owner=f"o{i % 20}",
            slug=f"s{i}",
            hits=i % 13,
            weight=0.5 + (i % 10) / 10.0,
            body=f"note body {i}",
            seen=now - datetime.timedelta(minutes=i),
            score=float(i % 1000),
        ).save()


OPS: dict[str, Callable[[int], Any]] = {
    "Model.save() new record": lambda i: BenchNote(
        owner="new", slug=f"n{i}", hits=1, body="x", score=float(i % 1000)
    ).save(),
    "Model.save() existing record": lambda i: BenchNote(
        owner=f"o{i % 20}", slug=f"s{i % 500}", hits=i, body="y", score=1.0
    ).save(),
    "filter(score__gte, limit=50) + hydrate": lambda i: list(
        BenchNote.query.filter(score__gte=float(i % 900), limit=50)
    ),
    "filter(owner=, score__lt, limit=20) + hydrate": lambda i: list(
        BenchNote.query.filter(owner=f"o{i % 20}", score__lt=500.0, limit=20)
    ),
    "Query.get(owner=, slug=)": lambda i: BenchNote.query.get(
        owner=f"o{i % 20}", slug=f"s{i % 2000}"
    ),
}
EXIT_CRITERIA = {
    "Model.save() new record": 2.0,
    "Model.save() existing record": 2.0,
    "filter(score__gte, limit=50) + hydrate": 1.0,
    "filter(owner=, score__lt, limit=20) + hydrate": 1.0,
}


def seed(backend: Any, n: int, analyze: Callable) -> None:
    set_backend(backend)
    try:
        _seed(n)
        analyze()
    finally:
        set_backend(None)


def bench(
    backends: dict[str, Any], iters: int, runs: int
) -> dict[str, dict[str, list[list[int]]]]:
    """Each run times every op on each backend back to back, so the two legs
    of one run share the machine's load at that moment (a loaded laptop
    otherwise skews whichever backend happens to run during a busy spell)."""
    results = {name: {op: [] for op in OPS} for name in backends}
    for run in range(runs):
        for op, fn in OPS.items():
            for name, backend in backends.items():
                set_backend(backend)
                try:
                    _timeit(fn, 30, offset=10_000_000 + run * 100_000)  # warm
                    results[name][op].append(_timeit(fn, iters, offset=run * 100_000))
                finally:
                    set_backend(None)
    return results


def _env_banner(pg_backend: PostgresBackend) -> str:
    import redis as _r

    redis_v = popoto.get_redis().info("server").get("redis_version")
    with psycopg.connect(PG_URL, autocommit=True) as c:
        pg_v = c.execute("select version()").fetchone()[0]
    try:
        cpu = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    except Exception:  # noqa: BLE001 - best effort off macOS
        cpu = platform.processor()
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True)
    return (
        f"python {platform.python_version()} | redis-py {_r.__version__} | "
        f"psycopg {psycopg.__version__} | Redis {redis_v} | {pg_v.split(',')[0]} | "
        f"{cpu} | {platform.platform()} | localhost (loopback) both | "
        f"REDIS_URL={REDIS_URL} | head {head.strip()}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(PG_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(dsn=PG_URL, schema=schema)
    print(_env_banner(pg))
    popoto.get_redis().flushdb()
    try:
        rd = RedisBackend()
        seed(rd, args.n, lambda: None)
        seed(pg, args.n, lambda: admin.execute(f'ANALYZE "{schema}".bench_note'))
        res = bench({"redis": rd, "postgres": pg}, args.iters, args.runs)
        redis_res, pg_res = res["redis"], res["postgres"]
    finally:
        pg.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        popoto.get_redis().flushdb()

    print(
        f"\nN={args.n} seeded records, {args.runs} runs x {args.iters} iterations "
        "per op (backends interleaved per op within each run), microseconds; "
        "ANALYZE after seeding\n"
    )
    print(
        "| op | Redis p50 (runs) | Postgres p50 (runs) | PG/Redis p50 "
        "(median, range) | Postgres p95 | exit criterion |"
    )
    print("|---|---|---|---|---:|---|")
    for op in OPS:
        r50 = [_p(s, 0.5) for s in redis_res[op]]
        p50 = [_p(s, 0.5) for s in pg_res[op]]
        ratios = [p / r for p, r in zip(p50, r50)]
        p95 = statistics.median(_p(s, 0.95) for s in pg_res[op])
        limit = EXIT_CRITERIA.get(op)
        verdict = (
            "—"
            if limit is None
            else (
                f"<= {limit:g}x: {'PASS' if statistics.median(ratios) <= limit else 'FAIL'}"
            )
        )
        print(
            f"| {op} | {' / '.join(f'{v:.0f}' for v in r50)} | "
            f"{' / '.join(f'{v:.0f}' for v in p50)} | "
            f"{statistics.median(ratios):.2f}x ({min(ratios):.2f}-{max(ratios):.2f}) | "
            f"{p95:.0f} | {verdict} |"
        )


if __name__ == "__main__":
    main()
