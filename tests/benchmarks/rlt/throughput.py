"""Throughput measurement — queries/sec at a fixed corpus size (#460).

Single-threaded baseline by default; ``concurrency > 1`` reports throughput
under a bounded ``ThreadPoolExecutor`` pool (redis-py's connection pool is
thread-safe — each worker thread checks out its own connection — so no
additional locking is needed here, matching ``tests/test_stress.py``'s
existing threading precedent).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Sequence


class RltThroughputError(ValueError):
    """A throughput-measurement precondition was violated."""


@dataclass
class ThroughputReport:
    """Queries/sec for one batch, plus how many succeeded/failed."""

    queries_per_second: float
    n_queries: int
    n_errors: int
    concurrency: int
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "queries_per_second": self.queries_per_second,
            "n_queries": self.n_queries,
            "n_errors": self.n_errors,
            "concurrency": self.concurrency,
            "elapsed_seconds": self.elapsed_seconds,
        }


def measure_throughput(
    call: Callable[[object], object],
    queries: Sequence[object],
    concurrency: int = 1,
    timer: Callable[[], float] = time.perf_counter,
) -> ThroughputReport:
    """Run every entry in ``queries`` through ``call``; report queries/sec.

    Args:
        call: A one-argument callable ``call(query) -> Any``.
        queries: Non-empty sequence of query objects.
        concurrency: Number of worker threads. ``1`` (default) runs
            queries back-to-back on the calling thread; ``> 1`` dispatches
            via ``ThreadPoolExecutor(max_workers=concurrency)``.
        timer: Monotonic timer function.

    Returns:
        A :class:`ThroughputReport`.

    Raises:
        RltThroughputError: If ``queries`` is empty or ``concurrency < 1``.
    """
    if not queries:
        raise RltThroughputError("measure_throughput() requires at least one query")
    if concurrency < 1:
        raise RltThroughputError(f"concurrency must be >= 1, got {concurrency}")

    n_errors = 0
    t0 = timer()

    if concurrency == 1:
        for query in queries:
            try:
                call(query)
            except Exception:
                n_errors += 1
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(call, q) for q in queries]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    n_errors += 1

    elapsed = timer() - t0
    n_queries = len(queries)
    qps = n_queries / elapsed if elapsed > 0 else float("inf")

    return ThroughputReport(
        queries_per_second=qps,
        n_queries=n_queries,
        n_errors=n_errors,
        concurrency=concurrency,
        elapsed_seconds=elapsed,
    )
