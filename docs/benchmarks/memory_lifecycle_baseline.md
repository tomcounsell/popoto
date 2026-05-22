# MemoryLifecycle Benchmark Baseline

**Generated:** 2026-05-22
**Branch:** session/memory_lifecycle
**Issue:** #396

## Overview

This document establishes the pre-lifecycle baseline for the MemoryLifecycle feature
(issue #396) and records the sweep grid parameters added to `tests/benchmarks/run_sweeps.py`.

The external benchmark harness (LoCoMo + LongMemEval-S) was established in issue #394.
Baseline retrieval metrics (without lifecycle) are from the existing harness runs on `main`.

## Pre-Lifecycle Baseline (from main branch, 2026-05-22)

Source: `tests/benchmarks/results/external/locomo_latest.md`

| Metric | Baseline (no lifecycle) |
|--------|------------------------|
| Recall@1 | 0.0000 |
| Recall@5 | 0.0000 |
| Recall@10 | 0.0000 |
| MRR | 0.0000 |
| Questions evaluated | 6 / 6 |

Source: `tests/benchmarks/results/external/longmemeval_s_latest.md`

| Metric | Baseline (no lifecycle) |
|--------|------------------------|
| Recall@1 | 0.0000 |
| Recall@5 | 0.0000 |
| Recall@10 | 0.0000 |
| MRR | 0.0000 |
| Questions evaluated | 10 / 10 |

**Note:** Baseline scores are 0 because the current harness uses score-only retrieval
(no embedding/BM25). Issue #395 will add hybrid retrieval. The lifecycle layer is
designed to improve signal quality by filtering low-value memories; its impact will
be measurable once #395 closes the retrieval gap.

**Reference target:** agentmemory BM25+Vector achieves 95.2% R@5 on LoCoMo with lifecycle.

## Sweep Grid Parameters Added (Tier 5)

Added to `TIER5_SWEEPS` in `tests/benchmarks/run_sweeps.py`:

| Constant | Sweep Values | Default | Rationale |
|----------|-------------|---------|-----------|
| `LIFECYCLE_PROMOTION_ACCESS_COUNT` | [1, 2, 3, 5, 10] | 3 | Access threshold for episodic→semantic |
| `LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD` | [0.3, 0.5, 0.6, 0.7, 0.9] | 0.6 | Confidence floor for promotion |
| `LIFECYCLE_PROMOTION_MIN_AGE_SECONDS` | [0.0, 60.0, 300.0, 1800.0, 7200.0] | 300.0 | Age guard against burst promotion |
| `LIFECYCLE_FORGET_IMPORTANCE_FLOOR` | [0.01, 0.05, 0.1, 0.2, 0.5] | 0.1 | Importance threshold for auto-forget |
| `LIFECYCLE_FORGET_IDLE_SECONDS` | [3600.0, 21600.0, 86400.0, 259200.0, 604800.0] | 86400.0 | Idle time threshold for auto-forget |

## How to Run a Sweep

Once issue #395 (hybrid retrieval) is merged and the LoCoMo harness produces
non-zero retrieval scores, run the Tier 5 sweep with:

```bash
python -m tests.benchmarks.run_sweeps --tier 5
```

This will produce results in `tests/benchmarks/results/sweep_YYYYMMDD_HHMMSS.json`.
Compare pre/post lifecycle Recall@5 on LoCoMo against the agentmemory 95.2% target.

## Next Steps

1. Merge issue #395 (hybrid retrieval) to enable non-zero LoCoMo scores
2. Run `python -m tests.benchmarks.run_sweeps --tier 5` on a corpus with lifecycle active
3. Update this document with sweep results and calibrate the Tier 5 constants
4. If LoCoMo Recall@5 improves, update `Defaults.LIFECYCLE_*` with optimal values
