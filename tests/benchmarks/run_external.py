"""Run external benchmark datasets against Popoto memory retrieval.

Supports LongMemEval-S (500 questions) and LoCoMo (50 dialogues, ~350+ QA pairs).
Reports Recall@1/5/10, MRR, and p50/p95 latency per dataset.
Commits results to tests/benchmarks/results/external/.

Usage:
    # Full dataset run (requires dataset download):
    python -m tests.benchmarks.run_external --dataset longmemeval-s
    python -m tests.benchmarks.run_external --dataset locomo

    # Smoke test (fast, no download needed if using --fixture):
    python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20
    python -m tests.benchmarks.run_external --dataset locomo --limit 10

    # Dry-run (ingests but prints results without saving report):
    python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 5 --dry-run

    # Use fixture file for offline testing:
    python -m tests.benchmarks.run_external --dataset longmemeval-s --fixture tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json

Results are saved to:
    tests/benchmarks/results/external/{dataset}_{YYYYMMDD}.{json,md}
    tests/benchmarks/results/external/{dataset}_latest.{json,md}  (symlink/copy)
"""

import argparse
import json
import logging
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.benchmarks.datasets import BenchmarkItem
from tests.benchmarks.datasets.longmemeval_s import iter_items as iter_longmemeval
from tests.benchmarks.datasets.locomo import iter_items as iter_locomo
from tests.benchmarks.metrics.retrieval import mean_reciprocal_rank, recall_at_k
from tests.benchmarks.scenarios.external_base import ExternalScenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ExternalBenchmark")

RESULTS_DIR = Path(__file__).parent / "results" / "external"

DATASET_CHOICES = ("longmemeval-s", "locomo")


# ---------------------------------------------------------------------------
# Per-question result
# ---------------------------------------------------------------------------


class QuestionResult:
    """Metrics for a single benchmark question."""

    def __init__(
        self,
        item_id: str,
        recall_at_1: float,
        recall_at_5: float,
        recall_at_10: float,
        mrr: float,
        retrieval_ms: float,
        status: str,
        error: str = "",
        metadata: dict = None,
    ):
        self.item_id = item_id
        self.recall_at_1 = recall_at_1
        self.recall_at_5 = recall_at_5
        self.recall_at_10 = recall_at_10
        self.mrr = mrr
        self.retrieval_ms = retrieval_ms
        self.status = status
        self.error = error
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "recall_at_1": self.recall_at_1,
            "recall_at_5": self.recall_at_5,
            "recall_at_10": self.recall_at_10,
            "mrr": self.mrr,
            "retrieval_ms": self.retrieval_ms,
            "status": self.status,
            "error": self.error,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Run one item
# ---------------------------------------------------------------------------


def run_item(item: BenchmarkItem) -> QuestionResult:
    """Run a single benchmark item through ExternalScenario.

    Args:
        item: BenchmarkItem from a dataset adapter.

    Returns:
        QuestionResult with recall metrics and latency.
    """
    scenario = ExternalScenario(item=item)
    scenario_result = scenario.execute()

    if scenario_result.status != "ok":
        return QuestionResult(
            item_id=item.item_id,
            recall_at_1=0.0,
            recall_at_5=0.0,
            recall_at_10=0.0,
            mrr=0.0,
            retrieval_ms=0.0,
            status=scenario_result.status,
            error=scenario_result.error_message,
            metadata=scenario_result.metadata,
        )

    r1 = recall_at_k(scenario_result.retrieved_ids, scenario_result.relevant_ids, 1)
    r5 = recall_at_k(scenario_result.retrieved_ids, scenario_result.relevant_ids, 5)
    r10 = recall_at_k(scenario_result.retrieved_ids, scenario_result.relevant_ids, 10)
    mrr = mean_reciprocal_rank(scenario_result.retrieved_ids, scenario_result.relevant_ids)
    retrieval_ms = scenario_result.metadata.get("retrieval_ms", 0.0)

    return QuestionResult(
        item_id=item.item_id,
        recall_at_1=r1,
        recall_at_5=r5,
        recall_at_10=r10,
        mrr=mrr,
        retrieval_ms=retrieval_ms,
        status="ok",
        metadata=scenario_result.metadata,
    )


# ---------------------------------------------------------------------------
# Aggregate results
# ---------------------------------------------------------------------------


def compute_aggregate(results: list[QuestionResult], dataset: str) -> dict:
    """Compute aggregate metrics over all question results.

    Args:
        results: List of QuestionResult objects.
        dataset: Dataset name for labeling.

    Returns:
        Dict with aggregate metrics and per-question detail.
    """
    ok = [r for r in results if r.status == "ok"]
    errors = [r for r in results if r.status == "error"]
    skipped = [r for r in results if r.status not in ("ok", "error")]

    n_total = len(results)
    n_ok = len(ok)
    n_errors = len(errors)
    n_skipped = len(skipped)

    if ok:
        avg_r1 = statistics.mean(r.recall_at_1 for r in ok)
        avg_r5 = statistics.mean(r.recall_at_5 for r in ok)
        avg_r10 = statistics.mean(r.recall_at_10 for r in ok)
        avg_mrr = statistics.mean(r.mrr for r in ok)
        latencies = sorted(r.retrieval_ms for r in ok if r.retrieval_ms > 0)
        n_lat = len(latencies)
        p50 = latencies[int(n_lat * 0.50)] if n_lat else 0.0
        p95 = latencies[int(n_lat * 0.95)] if n_lat >= 2 else (latencies[-1] if latencies else 0.0)
    else:
        avg_r1 = avg_r5 = avg_r10 = avg_mrr = 0.0
        p50 = p95 = 0.0

    now = datetime.now(timezone.utc)
    return {
        "dataset": dataset,
        "run_date": now.strftime("%Y-%m-%d"),
        "run_timestamp": now.isoformat(),
        "machine": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "summary": {
            "n_total": n_total,
            "n_ok": n_ok,
            "n_errors": n_errors,
            "n_skipped": n_skipped,
            "recall_at_1": round(avg_r1, 4),
            "recall_at_5": round(avg_r5, 4),
            "recall_at_10": round(avg_r10, 4),
            "mrr": round(avg_mrr, 4),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
        },
        "notes": [
            "Baseline run: relevance-only scoring (DecayingSortedField).",
            "No vector/embedding retrieval wired in (BM25-style baseline).",
            "LoCoMo: image-only turns skipped (text-only evaluation).",
        ],
        "questions": [r.to_dict() for r in results],
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_markdown_report(aggregate: dict) -> str:
    """Build a Markdown summary report from aggregate results.

    Args:
        aggregate: Output of compute_aggregate().

    Returns:
        Markdown string.
    """
    ds = aggregate["dataset"]
    s = aggregate["summary"]
    run_date = aggregate["run_date"]
    machine = aggregate["machine"]

    lines = [
        f"# Popoto External Benchmark: {ds}",
        "",
        f"**Run date:** {run_date}  ",
        f"**Python:** {machine['python_version']}  ",
        f"**Platform:** {machine['platform']}  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Questions evaluated | {s['n_ok']} / {s['n_total']} |",
        f"| Errors | {s['n_errors']} |",
        f"| Skipped | {s['n_skipped']} |",
        f"| Recall@1 | {s['recall_at_1']:.4f} |",
        f"| Recall@5 | {s['recall_at_5']:.4f} |",
        f"| Recall@10 | {s['recall_at_10']:.4f} |",
        f"| MRR | {s['mrr']:.4f} |",
        f"| Latency p50 (ms) | {s['p50_ms']} |",
        f"| Latency p95 (ms) | {s['p95_ms']} |",
        "",
        "## Notes",
        "",
    ]
    for note in aggregate.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## Reference Numbers")
    lines.append("")
    lines.append("agentmemory BM25+Vector (all-MiniLM-L6-v2) on LongMemEval-S:")
    lines.append("- Recall@5: 95.2%, Recall@10: 98.6%, MRR: 88.2%")
    lines.append("")
    lines.append(
        "Popoto baseline is score-only (no embedding retrieval). "
        "Issue #395 will add hybrid BM25+vector retrieval to close this gap."
    )
    lines.append("")
    return "\n".join(lines)


def save_reports(aggregate: dict, dataset_slug: str, dry_run: bool = False) -> tuple[Path, Path]:
    """Save JSON and Markdown report files.

    Args:
        aggregate: Output of compute_aggregate().
        dataset_slug: Safe filename slug (e.g., "longmemeval_s").
        dry_run: If True, skip saving and return None paths.

    Returns:
        Tuple of (json_path, md_path).
    """
    if dry_run:
        logger.info("[dry-run] Skipping report save.")
        return None, None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_name = f"{dataset_slug}_{date_str}.json"
    md_name = f"{dataset_slug}_{date_str}.md"

    json_path = RESULTS_DIR / json_name
    md_path = RESULTS_DIR / md_name

    with open(json_path, "w") as f:
        json.dump(aggregate, f, indent=2, default=str)
    logger.info("Saved JSON report: %s", json_path)

    md_content = build_markdown_report(aggregate)
    with open(md_path, "w") as f:
        f.write(md_content)
    logger.info("Saved Markdown report: %s", md_path)

    # Update latest symlinks/copies
    for src_name, latest_name in [
        (json_name, f"{dataset_slug}_latest.json"),
        (md_name, f"{dataset_slug}_latest.md"),
    ]:
        latest_path = RESULTS_DIR / latest_name
        try:
            if latest_path.is_symlink() or latest_path.exists():
                latest_path.unlink()
            latest_path.symlink_to(src_name)
        except OSError:
            import shutil
            shutil.copy2(RESULTS_DIR / src_name, latest_path)

    return json_path, md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Run external benchmark datasets against Popoto memory retrieval."
    )
    parser.add_argument(
        "--dataset",
        choices=DATASET_CHOICES,
        required=True,
        help="Dataset to benchmark",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of questions to evaluate (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without saving report artifacts",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Load dataset from local fixture file (offline testing)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output directory (default: tests/benchmarks/results/external/)",
    )
    parser.add_argument(
        "--error-threshold",
        type=float,
        default=0.10,
        help="Exit with code 1 if error rate exceeds this fraction (default: 0.10)",
    )
    args = parser.parse_args()

    global RESULTS_DIR
    if args.output:
        RESULTS_DIR = args.output

    # Select dataset adapter
    if args.dataset == "longmemeval-s":
        dataset_slug = "longmemeval_s"
        items = iter_longmemeval(fixture_path=args.fixture, limit=args.limit)
    elif args.dataset == "locomo":
        dataset_slug = "locomo"
        items = iter_locomo(fixture_path=args.fixture, limit=args.limit)
    else:
        logger.error("Unknown dataset: %s", args.dataset)
        return 1

    logger.info(
        "Starting benchmark: dataset=%s limit=%s dry_run=%s",
        args.dataset,
        args.limit or "all",
        args.dry_run,
    )

    total_start = time.monotonic()
    results = []
    n_processed = 0

    for item in items:
        n_processed += 1
        if n_processed % 10 == 0 or n_processed == 1:
            elapsed = time.monotonic() - total_start
            logger.info(
                "Progress: %d questions processed (%.1fs elapsed)",
                n_processed,
                elapsed,
            )

        q_result = run_item(item)
        results.append(q_result)

        if q_result.status == "ok":
            logger.debug(
                "  [%s] R@1=%.2f R@5=%.2f R@10=%.2f MRR=%.2f lat=%.1fms",
                item.item_id,
                q_result.recall_at_1,
                q_result.recall_at_5,
                q_result.recall_at_10,
                q_result.mrr,
                q_result.retrieval_ms,
            )
        else:
            logger.warning(
                "  [%s] status=%s error=%s",
                item.item_id,
                q_result.status,
                q_result.error,
            )

    total_elapsed = time.monotonic() - total_start
    logger.info("Benchmark complete: %d questions in %.1fs", n_processed, total_elapsed)

    # Aggregate metrics
    aggregate = compute_aggregate(results, args.dataset)
    s = aggregate["summary"]

    # Print summary table
    print("\n" + "=" * 60)
    print(f"BENCHMARK RESULTS: {args.dataset.upper()}")
    print("=" * 60)
    print(f"  Questions evaluated : {s['n_ok']} / {s['n_total']}")
    print(f"  Errors              : {s['n_errors']}")
    print(f"  Recall@1            : {s['recall_at_1']:.4f}")
    print(f"  Recall@5            : {s['recall_at_5']:.4f}")
    print(f"  Recall@10           : {s['recall_at_10']:.4f}")
    print(f"  MRR                 : {s['mrr']:.4f}")
    print(f"  Latency p50 (ms)    : {s['p50_ms']}")
    print(f"  Latency p95 (ms)    : {s['p95_ms']}")
    print("=" * 60)

    # Save reports
    if not args.dry_run:
        json_path, md_path = save_reports(aggregate, dataset_slug, dry_run=False)
        print(f"\nReports saved:")
        print(f"  JSON: {json_path}")
        print(f"  Markdown: {md_path}")

    # Check error threshold
    if s["n_total"] > 0:
        error_rate = s["n_errors"] / s["n_total"]
        if error_rate > args.error_threshold:
            logger.error(
                "Error rate %.1f%% exceeds threshold %.1f%% — exiting with code 1",
                error_rate * 100,
                args.error_threshold * 100,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
