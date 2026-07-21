"""Live mixed-workload harness — concurrent ingest writes + assembly reads (#460).

The live-agent-specific metric no static benchmark measures: does read
latency degrade under concurrent write load, and vice versa? Uses
``concurrent.futures.ThreadPoolExecutor`` (stdlib-only, matching
``tests/test_stress.py``'s existing threading precedent) — redis-py's
connection pool is thread-safe, so no additional locking is needed.

Caveat (documented in ``docs/benchmarks.md``'s RLT section, not hidden): a
thread-based load generator measures client-side thread-scheduling overhead
alongside genuine server-side latency; fully isolating server-only behavior
would need a multi-process load generator, which is out of scope for this
harness (see the plan's Risk 3).

Ordering: the corpus is fully planted *before* mixed-workload threads start
(read threads always have a non-empty baseline to query), and every submitted
future is awaited before percentiles are computed (no measurement is taken
on a latency list still being appended to by a background thread).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .latency import LatencyReport, measure_latency, percentile


class RltMixedWorkloadError(ValueError):
    """A mixed-workload precondition was violated."""


@dataclass
class MixedWorkloadReport:
    """Baseline vs. degraded read/write latency under concurrent load."""

    baseline_read: LatencyReport
    baseline_write: LatencyReport
    degraded_read: LatencyReport
    degraded_write: LatencyReport

    @property
    def read_degradation_ratio(self) -> Optional[float]:
        """``degraded_read.p99 / baseline_read.p99``, or ``None`` if undefined."""
        if self.baseline_read.p99 in (None, 0) or self.degraded_read.p99 is None:
            return None
        return self.degraded_read.p99 / self.baseline_read.p99

    @property
    def write_degradation_ratio(self) -> Optional[float]:
        """``degraded_write.p99 / baseline_write.p99``, or ``None`` if undefined."""
        if self.baseline_write.p99 in (None, 0) or self.degraded_write.p99 is None:
            return None
        return self.degraded_write.p99 / self.baseline_write.p99

    def to_dict(self) -> dict:
        return {
            "baseline_read": self.baseline_read.to_dict(),
            "baseline_write": self.baseline_write.to_dict(),
            "degraded_read": self.degraded_read.to_dict(),
            "degraded_write": self.degraded_write.to_dict(),
            "read_degradation_ratio": self.read_degradation_ratio,
            "write_degradation_ratio": self.write_degradation_ratio,
        }


def _run_concurrently(
    read_call, read_args, write_call, write_args, n_threads
) -> Tuple[List[float], List[float]]:
    """Run read+write calls concurrently; return (read_latencies_ms, write_latencies_ms)."""
    read_latencies: List[float] = []
    write_latencies: List[float] = []

    def _timed_read(arg):
        t0 = time.perf_counter()
        read_call(arg)
        return ("read", (time.perf_counter() - t0) * 1000.0)

    def _timed_write(arg):
        t0 = time.perf_counter()
        write_call(arg)
        return ("write", (time.perf_counter() - t0) * 1000.0)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_timed_read, a) for a in read_args]
        futures += [pool.submit(_timed_write, a) for a in write_args]
        for fut in as_completed(futures):
            try:
                kind, ms = fut.result()
            except Exception:
                continue
            if kind == "read":
                read_latencies.append(ms)
            else:
                write_latencies.append(ms)

    return read_latencies, write_latencies


def _report_from_latencies(
    latencies_ms: List[float], n_errors: int = 0
) -> LatencyReport:
    if not latencies_ms:
        return LatencyReport(
            p50=None, p95=None, p99=None, n_samples=0, n_errors=n_errors
        )
    return LatencyReport(
        p50=percentile(latencies_ms, 50),
        p95=percentile(latencies_ms, 95),
        p99=percentile(latencies_ms, 99),
        n_samples=len(latencies_ms),
        n_errors=n_errors,
        latencies_ms=latencies_ms,
    )


def run_mixed_workload(
    read_call: Callable[[object], object],
    read_args,
    write_call: Callable[[object], object],
    write_args,
    n_threads: int = 4,
) -> MixedWorkloadReport:
    """Measure read/write latency solo (baseline) vs. concurrent (degraded).

    Args:
        read_call: One-argument callable performing one read (e.g.
            ``assembler.assemble``).
        read_args: Sequence of read-call arguments (e.g. queries).
        write_call: One-argument callable performing one write (e.g.
            ``lambda content: model_class(...).save()``).
        write_args: Sequence of write-call arguments.
        n_threads: Worker threads for the concurrent (degraded) phase.

    Returns:
        A :class:`MixedWorkloadReport` with baseline and degraded latency for
        both directions, plus degradation ratios.

    Raises:
        RltMixedWorkloadError: If either arg sequence is empty or
            ``n_threads < 1``.
    """
    read_args = list(read_args)
    write_args = list(write_args)
    if not read_args or not write_args:
        raise RltMixedWorkloadError(
            "run_mixed_workload() requires at least one read arg and one write arg"
        )
    if n_threads < 1:
        raise RltMixedWorkloadError(f"n_threads must be >= 1, got {n_threads}")

    # Baseline: each direction measured solo, single-threaded.
    baseline_read = measure_latency(read_call, read_args)
    baseline_write = measure_latency(write_call, write_args)

    # Degraded: both directions run concurrently across a shared thread pool.
    read_latencies, write_latencies = _run_concurrently(
        read_call, read_args, write_call, write_args, n_threads
    )
    degraded_read = _report_from_latencies(
        read_latencies, n_errors=len(read_args) - len(read_latencies)
    )
    degraded_write = _report_from_latencies(
        write_latencies, n_errors=len(write_args) - len(write_latencies)
    )

    return MixedWorkloadReport(
        baseline_read=baseline_read,
        baseline_write=baseline_write,
        degraded_read=degraded_read,
        degraded_write=degraded_write,
    )
