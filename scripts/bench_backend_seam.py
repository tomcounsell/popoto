#!/usr/bin/env python
"""Latency comparison of ``RedisBackend`` and ``PostgresBackend`` (#631 WS4).

Measures p50 / p95 / p99 wall-clock latency, same process, same machine,
warm, for the protocol-level operations the #631 slice is built on and for
two model-level entry points (``Model.save()`` and a ``filter()`` query)
driven through ``set_backend``. The numbers are a POC reading on a laptop
with the generic schema and no tuning; see
``docs/plans/postgres_backend_poc.md`` for what they mean and what they do
not.

Safety: this script binds Redis from ``REDIS_URL`` *before* importing popoto
(CLAUDE.md, #577) and refuses database 0. On Postgres it creates its own
``popoto_test_<uuid4().hex>`` schema and drops it at the end; it never
touches ``public`` and never drops a schema it did not create.

Usage::

    REDIS_URL=redis://localhost:6379/10 \\
    POSTGRES_URL=postgresql://localhost:5432/postgres \\
    python scripts/bench_backend_seam.py [--n 2000] [--iters 200]
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    sys.exit("set REDIS_URL=redis://localhost:6379/<n> (n != 0) before running")
if REDIS_URL.rstrip("/").endswith(":6379") or REDIS_URL.endswith("/0"):
    sys.exit(f"refusing to bench against DB 0 ({REDIS_URL!r}); name a non-zero DB")
POSTGRES_URL = os.environ.get("POSTGRES_URL", "")
if not POSTGRES_URL:
    sys.exit("set POSTGRES_URL=postgresql://host:port/db")
# The backend seam selects Postgres whenever POSTGRES_URL is set; this script
# binds each backend explicitly with set_backend, so the variable is removed
# from the environment the lazy selector would read.
_PG_URL = POSTGRES_URL
del os.environ["POSTGRES_URL"]

import msgpack  # noqa: E402
import psycopg  # noqa: E402

import popoto  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.backends.postgres import PostgresBackend  # noqa: E402
from popoto.backends.redis import RedisBackend, _validity_keys  # noqa: E402
from popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from popoto.fields.validity_field import ValidityField  # noqa: E402
from popoto.pytest_plugin import _url_with_search_path  # noqa: E402

DAY = 86400.0


class BenchMemory(popoto.Model):
    """The slice's workhorse shape: decay, confidence and validity arms."""

    name = popoto.KeyField()
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField(initial_confidence=0.5)
    validity = ValidityField()


def _percentiles(samples_ns: list[int]) -> tuple[float, float, float]:
    s = sorted(samples_ns)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return s[idx] / 1_000.0  # microseconds

    return pct(0.50), pct(0.95), pct(0.99)


def _timeit(fn: Callable[[int], Any], iters: int, warm: int = 20) -> list[int]:
    for i in range(warm):
        fn(i)
    out: list[int] = []
    for i in range(iters):
        t0 = time.perf_counter_ns()
        fn(i)
        out.append(time.perf_counter_ns() - t0)
    return out


def _seed(backend: Any, n: int, now: float) -> dict[str, Any]:
    """N records with a decay score, a confidence payload and an open interval."""
    prefix = "$ValidityF:BenchRaw"
    vk = _validity_keys(prefix, "validity")
    zidx = "BenchRaw:_relevance"
    conf_idx = "$ConfF:BenchRaw:certainty:data"
    keys = [f"BenchRaw:{i}" for i in range(n)]
    for i, key in enumerate(keys):
        backend.save_record(
            key,
            {
                b"name": msgpack.packb(str(i)),
                b"importance": msgpack.packb(0.5 + (i % 10) / 10.0),
            },
            class_set="$Class:BenchRaw",
        )
        backend.sorted_add(zidx, key, now - (i % 90) * DAY)
        backend.map_set(
            conf_idx,
            key,
            msgpack.packb(
                {
                    "confidence": 0.3 + (i % 7) / 10.0,
                    "evidence_count": 1,
                    "corroborations": 0,
                    "contradictions": 0,
                }
            ),
        )
        backend.supersede(
            prefix,
            "validity",
            mode="open",
            new_member=key,
            now=now,
            valid_from=now - (i % 90) * DAY,
            ingested_at=now,
            close_at=None,
            assert_valid_from=False,
            pointer_digest=None,
        )
    return {
        "prefix": prefix,
        "vk": vk,
        "zidx": zidx,
        "conf_idx": conf_idx,
        "keys": keys,
    }


def bench_protocol(backend: Any, n: int, iters: int) -> dict[str, list[int]]:
    now = time.time()
    s = _seed(backend, n, now)
    keys, zidx, conf_idx, vk, prefix = (
        s["keys"],
        s["zidx"],
        s["conf_idx"],
        s["vk"],
        s["prefix"],
    )
    out: dict[str, list[int]] = {}

    def save_record(i: int) -> None:
        backend.save_record(
            f"BenchRaw:new:{i}",
            {b"name": msgpack.packb(f"new{i}"), b"importance": msgpack.packb(1.0)},
            class_set="$Class:BenchRaw",
        )

    out["save_record (insert, 2 fields + class set)"] = _timeit(save_record, iters)
    out["load_record"] = _timeit(lambda i: backend.load_record(keys[i % n]), iters)

    def rank(**kw: Any) -> Callable[[int], Any]:
        return lambda i: backend.decayed_rank(
            zidx,
            now=now,
            decay_rate=0.5,
            limit=50,
            pretrim_max_ratio=4.0,
            **kw,
        )

    gate = (vk["invalid_at"], vk["valid_from"], now)
    out[f"decayed_rank plain (N={n}, limit 50)"] = _timeit(rank(), iters)
    out["decayed_rank + base_score_field"] = _timeit(
        rank(base_score_field="importance"), iters
    )
    out["decayed_rank + confidence modulation"] = _timeit(
        rank(confidence=(conf_idx, 0.5, 0.5)), iters
    )
    out["decayed_rank + validity gate"] = _timeit(rank(validity=gate), iters)
    out["decayed_rank + base + confidence + gate"] = _timeit(
        rank(
            base_score_field="importance",
            confidence=(conf_idx, 0.5, 0.5),
            validity=gate,
        ),
        iters,
    )

    def sup_open(i: int) -> None:
        backend.supersede(
            prefix,
            "validity",
            mode="open",
            new_member=f"BenchRaw:open:{i}",
            now=now,
            valid_from=now,
            ingested_at=now,
            close_at=None,
            assert_valid_from=False,
            pointer_digest=f"d{i}",
        )

    out["supersede (open, with pointer)"] = _timeit(sup_open, iters)

    def sup_replace(i: int) -> None:
        old, new = keys[(2 * i) % n], keys[(2 * i + 1) % n]
        backend.supersede(
            prefix,
            "validity",
            mode="supersede",
            new_member=new,
            old_member=old,
            now=now + 1,
            valid_from=None,
            ingested_at=None,
            close_at=now + 1,
            assert_valid_from=False,
            pointer_digest=None,
        )

    out["supersede (supersede mode, explicit incumbent)"] = _timeit(
        sup_replace, iters, warm=0
    )

    def swap(i: int) -> None:
        backend.swap_index(
            keys[i % n],
            "status",
            f"$IndexF:BenchRaw:status:{i}",
            msgpack.packb(f"s{i}"),
            unique=False,
        )

    out["swap_index (new value, non-unique)"] = _timeit(swap, iters)

    out["sorted_range (30-day window, reverse, limit 50)"] = _timeit(
        lambda i: backend.sorted_range(
            zidx, now - 30 * DAY, now, reverse=True, limit=50
        ),
        iters,
    )
    return out


def bench_model(backend: Any, n: int, iters: int) -> dict[str, list[int]]:
    set_backend(backend)
    try:
        now = time.time()
        out: dict[str, list[int]] = {}
        for i in range(n):
            BenchMemory(name=f"m{i}", importance=0.5 + (i % 10) / 10.0).save()
        cutoff = now - 30 * DAY

        def save(i: int) -> None:
            BenchMemory(name=f"new{i}", importance=1.0).save()

        out["Model.save() (key + decay + confidence + validity)"] = _timeit(save, iters)
        out["list(filter(relevance__gte=cutoff, limit=50)) + hydrate"] = _timeit(
            lambda i: list(BenchMemory.query.filter(relevance__gte=cutoff, limit=50)),
            iters,
        )
        out["top_by_decay(limit=50) through Query"] = _timeit(
            lambda i: BenchMemory.query.top_by_decay("relevance", n=50),
            iters,
        )
        return out
    finally:
        set_backend(None)


def _env_banner() -> str:
    redis_v = popoto.get_redis().info("server").get("redis_version")
    with psycopg.connect(_PG_URL, autocommit=True) as c:
        pg_v = c.execute("select version()").fetchone()[0]
    try:
        cpu = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    except Exception:  # noqa: BLE001 - best effort on non-mac
        cpu = platform.processor()
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True)
    import redis as _r

    return (
        f"python {platform.python_version()} | redis-py {_r.__version__} | "
        f"psycopg {psycopg.__version__} | msgpack {msgpack.version} | "
        f"Redis {redis_v} | {pg_v.split(',')[0]} | {cpu} | "
        f"{platform.platform()} | REDIS_URL={REDIS_URL} | head {head.strip()}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--iters", type=int, default=200)
    args = ap.parse_args()

    print(_env_banner())
    schema = f"popoto_test_{uuid.uuid4().hex}"
    admin = psycopg.connect(_PG_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pg = PostgresBackend(_url_with_search_path(_PG_URL, schema))
    rd = RedisBackend()
    popoto.get_redis().flushdb()
    results: dict[str, dict[str, list[int]]] = {}
    try:
        results["redis"] = bench_protocol(rd, args.n, args.iters)
        results["redis"].update(bench_model(rd, args.n, args.iters))
        results["postgres"] = bench_protocol(pg, args.n, args.iters)
        results["postgres"].update(bench_model(pg, args.n, args.iters))
    finally:
        pg.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
        popoto.get_redis().flushdb()

    print(f"\nN={args.n} records, {args.iters} iterations per op, microseconds\n")
    print("| op | Redis p50 | p95 | p99 | Postgres p50 | p95 | p99 | PG/Redis p50 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for op in results["redis"]:
        r = _percentiles(results["redis"][op])
        p = _percentiles(results["postgres"][op])
        print(
            f"| {op} | {r[0]:.0f} | {r[1]:.0f} | {r[2]:.0f} | "
            f"{p[0]:.0f} | {p[1]:.0f} | {p[2]:.0f} | {p[0] / r[0]:.1f}x |"
        )
    print(
        "\nmean of ratios:",
        f"{statistics.mean(_percentiles(results['postgres'][o])[0] / _percentiles(results['redis'][o])[0] for o in results['redis']):.1f}x",
    )


if __name__ == "__main__":
    main()
