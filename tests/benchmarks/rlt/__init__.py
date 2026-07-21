"""RLT — Retrieval Latency & Throughput harness (issue #460).

Measures the axis no other benchmark in this suite touches: **speed under
load**, not retrieval/injection quality. Strategy:
``docs/plans/benchmarking_strategy_2026-07.md`` §2.2; plan:
``docs/plans/rlt_benchmark.md``.

Five metrics, mirrored one-to-one onto submodules:

1. Latency (p50/p95/p99 per retrieval + end-to-end assemble) — :mod:`.latency`.
2. Throughput (queries/sec at fixed corpus size) — :mod:`.throughput`.
3. Scaling curve (latency vs. corpus size, 10^3 -> 2x10^4) — :mod:`.scaling`.
4. Live mixed workload (concurrent ingest + assembly reads) —
   :mod:`.mixed_workload`.
5. Recall-vs-p99 Pareto frontier, joint with the retrieval harness —
   :mod:`.pareto`.

Plus :mod:`.corpus` (synthetic corpus + query-set builder) and
:mod:`.comparators` (the ``RltAdapter`` Protocol extension point for
Mem0/Zep/vector-DB adapters — real adapters are a tracked follow-up, not
implemented here; see ``docs/benchmarks.md``'s RLT section).

This package builds and unit-tests the harness only. The real headline
measurement runs (``run_rlt.py`` against a real corpus) are deliberately NOT
executed as part of building this package — see the RLT section of
``docs/benchmarks.md`` for why (machine contention with a concurrently running
benchmark chain) and the tracked follow-up issue for running them for real.
"""
