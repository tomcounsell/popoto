"""Latency measurement — p50/p95/p99 per retrieval + end-to-end assemble (#460).

``ContextAssembler.assemble()`` already *is* the full retrieve->rank->inject
pipeline (confirmed against ``src/popoto/recipes/context_assembler.py`` at
plan time), so there is no separate lower-level "just the pull query" API
surface to time independently without changing ``src/popoto/``, which is out
of scope for this harness. Metric (1) ("p50/p95/p99 per retrieval; end-to-end
assemble latency") is satisfied by timing ``assemble()`` end to end and
reporting it under both labels — ``assemble_latency`` is the canonical
measurement, ``retrieval_latency`` is the same number under this harness's
retrieval mode (no separate lower pipeline stage exists to isolate).

``percentile()`` reuses ``tests/benchmarks/run_external.py``'s existing
nearest-rank formula rather than introducing a second, numerically different
percentile definition into the same benchmark suite (its
``compute_aggregate()`` already computes p50/p95 this way for the external
harness, and ``docs/benchmarks.md`` presents both documents' numbers
side by side).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile over ``values`` (matches ``run_external.py``).

    Args:
        values: Non-empty sequence of numeric samples.
        p: Percentile in ``[0, 100]``.

    Returns:
        The nearest-rank percentile value.

    Raises:
        ValueError: If ``values`` is empty. A silent ``0.0`` would be
            indistinguishable from a genuine zero-latency measurement in a
            downstream artifact, so callers that might see an empty sample
            set (e.g. an all-errors batch) must handle that case explicitly
            before calling this function — see :func:`measure_latency`.
    """
    if not values:
        raise ValueError("percentile() requires at least one value")
    ordered = sorted(values)
    n = len(ordered)
    idx = min(int(n * (p / 100.0)), n - 1)
    return ordered[idx]


@dataclass
class LatencyReport:
    """p50/p95/p99 + sample/error counts for one batch of timed calls.

    ``p50``/``p95``/``p99`` are ``None`` when every call in the batch failed
    (see :func:`measure_latency`) — a degraded-but-valid report, never a
    crash, so a scaling-curve run with one bad point still completes.
    """

    p50: Optional[float]
    p95: Optional[float]
    p99: Optional[float]
    n_samples: int
    n_errors: int
    latencies_ms: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "p50_ms": self.p50,
            "p95_ms": self.p95,
            "p99_ms": self.p99,
            "n_samples": self.n_samples,
            "n_errors": self.n_errors,
        }


def measure_latency(
    call: Callable[[], object],
    queries: Sequence[object],
    timer: Callable[[], float] = time.perf_counter,
) -> LatencyReport:
    """Time ``call(query)`` once per entry in ``queries``; return a report.

    A single failing query is caught and counted, never aborts the batch
    (mirrors ``run_external.py``'s ``run_item()`` per-item error handling).
    If **every** query fails, the all-errors edge case is handled explicitly
    (short-circuits before calling :func:`percentile` on an empty list) —
    hardened per plan-critique finding: an uncaught ``percentile([])``
    ``ValueError`` would otherwise abort an entire scaling/throughput run
    over one bad point.

    Args:
        call: A one-argument callable ``call(query) -> Any``; only its
            wall-clock duration is measured, its return value is discarded.
        queries: The sequence of query objects to pass to ``call``.
        timer: Monotonic timer function (``time.perf_counter`` by default —
            immune to wall-clock adjustment, the stdlib-recommended choice
            for interval timing).

    Returns:
        A :class:`LatencyReport`.
    """
    latencies_ms: List[float] = []
    n_errors = 0
    for query in queries:
        t0 = timer()
        try:
            call(query)
        except Exception:
            n_errors += 1
            continue
        latencies_ms.append((timer() - t0) * 1000.0)

    if not latencies_ms:
        return LatencyReport(
            p50=None,
            p95=None,
            p99=None,
            n_samples=0,
            n_errors=n_errors,
            latencies_ms=[],
        )

    return LatencyReport(
        p50=percentile(latencies_ms, 50),
        p95=percentile(latencies_ms, 95),
        p99=percentile(latencies_ms, 99),
        n_samples=len(latencies_ms),
        n_errors=n_errors,
        latencies_ms=latencies_ms,
    )
