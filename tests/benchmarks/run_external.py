"""Run external benchmark datasets against Popoto memory retrieval.

Supports LongMemEval-S (500 questions) and LoCoMo (10 dialogues, 1986 QA pairs).
Reports Recall@1/5/10, MRR, and p50/p95 latency per dataset.
Commits results to tests/benchmarks/results/external/.

Usage:
    # Full dataset run (requires dataset download):
    python -m tests.benchmarks.run_external --dataset longmemeval-s
    python -m tests.benchmarks.run_external --dataset locomo

    # Smoke test (fast). --limit is representative by default: the subset is
    # selected with --sample stride so it spans the whole (category-grouped)
    # dataset rather than only the easiest contiguous prefix. Use
    # --sample stratified to guarantee every question_type is represented, or
    # --sample head (with --seed ignored) to reproduce legacy prefix runs.
    python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 20
    python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 12 --sample stratified --seed 0
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

import redis

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
# Benchmark DB isolation (issue #465)
# ---------------------------------------------------------------------------
#
# The external harness runs against the live default Popoto connection — db0 by
# design, because it is a benchmark rather than a pytest test, so the db15
# test-isolation plugin does not apply. Interrupted / killed / wedged runs
# leaked ExternalBenchmarkMemory / ExtMem* / $BM25:ExtMem* keys into db0
# permanently, polluting any store sharing db0 (a dogfood machine accumulated
# 1,825 stale keys). Two belt-and-braces mitigations, both scoped to this
# entrypoint (they activate only inside main(), never at import time, so pytest
# collection and the db15 plugin are unaffected):
#
#   1. Point the connection at a dedicated bench DB (default 14), mirroring the
#      plugin's db15 posture and its db0 rejection.
#   2. Sweep any pre-existing residue at run start via SCAN+DEL.

# Dedicated non-default DB for the external benchmark harness. 14 mirrors the
# plugin's db15 isolation posture (tests → 15, benchmarks → 14). Operational,
# overridable via POPOTO_BENCH_DB (like POPOTO_TEST_DB); db0 is rejected.
BENCH_DB_DEFAULT = 14

# Key patterns left behind by prior/interrupted runs. ExtMem* covers the
# per-item model keys and their sorted-index keys (ExtMem<hash>:_relevance,
# etc.); $BM25:ExtMem* covers the BM25 index keys; ExternalBenchmarkMemory:*
# covers any keys written under the un-renamed base class name. All are literal
# outside the trailing glob (``$`` is not a Redis SCAN metacharacter).
_STALE_KEY_PATTERNS = (
    "ExternalBenchmarkMemory:*",
    "ExtMem*",
    "$BM25:ExtMem*",
)


def _resolve_bench_db() -> int:
    """Resolve the benchmark Redis DB number.

    Priority: ``POPOTO_BENCH_DB`` env var > default (:data:`BENCH_DB_DEFAULT`,
    14). Mirrors the pytest plugin's db0 rejection so a misconfigured
    ``POPOTO_BENCH_DB=0`` cannot repollute production.

    Returns:
        The resolved bench DB number (non-zero int).

    Raises:
        ValueError: If the env value is non-integer or 0.
    """
    raw = os.environ.get("POPOTO_BENCH_DB", "").strip()
    if not raw:
        return BENCH_DB_DEFAULT
    try:
        bench_db = int(raw)
    except (ValueError, TypeError):
        raise ValueError(f"POPOTO_BENCH_DB must be an integer, got {raw!r}")
    if bench_db == 0:
        raise ValueError(
            "POPOTO_BENCH_DB=0 is not allowed — DB 0 is typically production. "
            "Use a non-zero DB (default: 14)."
        )
    return bench_db


def _point_connection_at_db(target_db: int) -> None:
    """Swap the Popoto connection pool in-place onto ``target_db``.

    Mirrors ``popoto.pytest_plugin._swap_db``: it mutates the existing
    ``POPOTO_REDIS_DB`` object rather than rebinding the global, so every module
    that imported ``POPOTO_REDIS_DB`` at load time (e.g. the scenario module)
    keeps using the correct connection. Host/port/auth are preserved from the
    current pool so a ``REDIS_URL`` with credentials keeps working.

    Called ONLY from the harness entrypoint (main()); never at import time, so
    it cannot interfere with the pytest db15 plugin.
    """
    from src.popoto import redis_db

    db_obj = redis_db.POPOTO_REDIS_DB
    current_kwargs = dict(db_obj.connection_pool.connection_kwargs)
    current_kwargs["db"] = target_db
    current_kwargs.setdefault("socket_timeout", 5)
    current_kwargs.setdefault("socket_connect_timeout", 5)
    connection_class = db_obj.connection_pool.connection_class
    new_pool = redis.ConnectionPool(connection_class=connection_class, **current_kwargs)
    old_pool = db_obj.connection_pool
    db_obj.connection_pool = new_pool
    old_pool.disconnect()


def _sweep_stale_benchmark_keys(redis_conn) -> int:
    """SCAN + DEL any benchmark residue left by prior/interrupted runs.

    Uses cursor-based SCAN (non-blocking, Valkey-safe — no Redis modules) to
    enumerate keys matching :data:`_STALE_KEY_PATTERNS` and DELs them. Operates
    on whatever connection it is handed, so it targets exactly the DB the
    harness is pointed at.

    Args:
        redis_conn: A ``redis.Redis`` client (the live Popoto connection).

    Returns:
        The number of stale keys deleted.
    """
    deleted = 0
    for pattern in _STALE_KEY_PATTERNS:
        cursor = 0
        while True:
            cursor, keys = redis_conn.scan(cursor, match=pattern, count=500)
            if keys:
                deleted += redis_conn.delete(*keys)
            if cursor == 0:
                break
    return deleted


def _select_bench_db() -> int:
    """Point the Popoto connection at the isolated bench DB and sweep residue.

    Resolves the bench DB (env > default 14, db0 rejected), swaps the live
    connection onto it, then sweeps any stale keys left by prior runs. Both
    steps are scoped to the harness entrypoint. Returns the bench DB number.
    """
    from src.popoto.redis_db import POPOTO_REDIS_DB

    bench_db = _resolve_bench_db()
    _point_connection_at_db(bench_db)
    logger.info(
        "Benchmark isolation: Popoto connection pointed at DB %d "
        "(override via POPOTO_BENCH_DB; db0 rejected).",
        bench_db,
    )
    swept = _sweep_stale_benchmark_keys(POPOTO_REDIS_DB)
    logger.info(
        "Swept %d stale benchmark key(s) from DB %d at startup.",
        swept,
        bench_db,
    )
    return bench_db


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


def run_item(item: BenchmarkItem, retrieval_mode: str = "lexical") -> QuestionResult:
    """Run a single benchmark item through ExternalScenario.

    Args:
        item: BenchmarkItem from a dataset adapter.
        retrieval_mode: ``"lexical"`` (BM25 only), ``"hybrid"`` (BM25 +
            vector via RRF), or ``"vector"`` (EmbeddingField-only, pure
            cosine — harness-local diagnostic). Threaded into the scenario.

    Returns:
        QuestionResult with recall metrics and latency.
    """
    scenario = ExternalScenario(item=item, retrieval_mode=retrieval_mode)
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
    mrr = mean_reciprocal_rank(
        scenario_result.retrieved_ids, scenario_result.relevant_ids
    )
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


def _by_question_type(ok: list[QuestionResult]) -> dict:
    """Per-``question_type`` metric breakdown over the successful results.

    Groups ``ok`` results by their ``question_type`` metadata (empty/``None``
    routes to ``"(unlabeled)"``) and reports n + R@1/R@5/R@10/MRR per category.
    Categories are returned in sorted key order for a stable report.
    """
    groups: dict = {}
    for r in ok:
        qt = r.metadata.get("question_type") or "(unlabeled)"
        groups.setdefault(qt, []).append(r)

    breakdown: dict = {}
    for qt in sorted(groups, key=str):
        rs = groups[qt]
        breakdown[qt] = {
            "n": len(rs),
            "recall_at_1": round(statistics.mean(r.recall_at_1 for r in rs), 4),
            "recall_at_5": round(statistics.mean(r.recall_at_5 for r in rs), 4),
            "recall_at_10": round(statistics.mean(r.recall_at_10 for r in rs), 4),
            "mrr": round(statistics.mean(r.mrr for r in rs), 4),
        }
    return breakdown


def compute_aggregate(
    results: list[QuestionResult],
    dataset: str,
    sample_mode: str = "head",
    seed: int = 0,
    limit=None,
    retrieval_mode: str = "lexical",
) -> dict:
    """Compute aggregate metrics over all question results.

    Args:
        results: List of QuestionResult objects.
        dataset: Dataset name for labeling.
        sample_mode: Sampling mode used for this run (recorded for
            reproducibility).
        seed: RNG seed used for this run (recorded for reproducibility).
        limit: ``--limit`` value used for this run (``None`` = full dataset).
        retrieval_mode: ``"lexical"``, ``"hybrid"``, or ``"vector"`` — the
            retrieval mode the run was executed in (recorded and surfaced in
            the report).

    Returns:
        Dict with aggregate metrics, per-``question_type`` breakdown, the
        sampling parameters, and per-question detail.
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
        p95 = (
            latencies[int(n_lat * 0.95)]
            if n_lat >= 2
            else (latencies[-1] if latencies else 0.0)
        )
    else:
        avg_r1 = avg_r5 = avg_r10 = avg_mrr = 0.0
        p50 = p95 = 0.0

    if retrieval_mode == "hybrid":
        mode_notes = [
            "Retrieval mode: hybrid — ContextAssembler.assemble() is the "
            "primary path; effective mode resolves to 'hybrid'.",
            "Hybrid fuses BM25 (lexical) + vector (all-MiniLM-L6-v2, 384-dim, "
            "in-process numpy cosine) via Reciprocal Rank Fusion (k=60).",
        ]
    elif retrieval_mode == "vector":
        mode_notes = [
            "Retrieval mode: vector — pure cosine over the EmbeddingField "
            "(all-MiniLM-L6-v2, 384-dim, in-process numpy cosine); "
            "ContextAssembler is BYPASSED (auto-mode would resolve an "
            "embedding-only model to 'composite', which is query-blind, so "
            "the harness ranks by raw cosine directly). No BM25, no RRF, no "
            "graph.",
        ]
    else:
        mode_notes = [
            "Retrieval mode: lexical — ContextAssembler.assemble() is the "
            "primary path; effective mode resolves to 'lexical'.",
            "Lexical uses BM25 (query-sensitive) only; no vector/embedding "
            "signal is fused in this mode.",
        ]

    now = datetime.now(timezone.utc)
    return {
        "dataset": dataset,
        "retrieval_mode": retrieval_mode,
        "run_date": now.strftime("%Y-%m-%d"),
        "run_timestamp": now.isoformat(),
        "sampling": {
            "sample_mode": sample_mode,
            "seed": seed,
            "limit": limit,
        },
        "machine": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "by_question_type": _by_question_type(ok),
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
        "notes": mode_notes
        + [
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
    retrieval_mode = aggregate.get("retrieval_mode", "lexical")
    sampling = aggregate.get("sampling", {})
    sample_mode = sampling.get("sample_mode", "head")
    seed = sampling.get("seed", 0)
    limit = sampling.get("limit")
    limit_str = "all" if limit is None else str(limit)

    lines = [
        f"# Popoto External Benchmark: {ds}",
        "",
        f"**Run date:** {run_date}  ",
        f"**Retrieval mode:** {retrieval_mode}  ",
        f"**Python:** {machine['python_version']}  ",
        f"**Platform:** {machine['platform']}  ",
        f"**Sample mode:** {sample_mode}  ",
        f"**Seed:** {seed}  ",
        f"**Limit:** {limit_str}  ",
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
    ]

    by_qt = aggregate.get("by_question_type", {})
    if by_qt:
        lines.append("## By question_type")
        lines.append("")
        lines.append("| question_type | n | Recall@1 | Recall@5 | Recall@10 | MRR |")
        lines.append("|---|---|---|---|---|---|")
        for qt, m in by_qt.items():
            lines.append(
                f"| {qt} | {m['n']} | {m['recall_at_1']:.4f} | "
                f"{m['recall_at_5']:.4f} | {m['recall_at_10']:.4f} | "
                f"{m['mrr']:.4f} |"
            )
        lines.append("")

    lines += [
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
    lines.append("Popoto BM25-only baseline on LongMemEval-S (any-hit, #438):")
    lines.append("- Recall@5: 95.2%, Recall@10: 97.8%")
    lines.append("")
    if retrieval_mode == "hybrid":
        lines.append(
            "This run used **hybrid** retrieval (BM25 + all-MiniLM-L6-v2 vector "
            "fused via RRF, k=60). Compare Recall@5/Recall@10 above against the "
            "BM25-only baseline and the agentmemory hybrid reference."
        )
    elif retrieval_mode == "vector":
        lines.append(
            "This run used **vector** retrieval (all-MiniLM-L6-v2, pure cosine, "
            "no BM25/RRF) — a harness-local diagnostic that bypasses "
            "ContextAssembler and isolates ONLY the dense arm. Vector-only "
            "LoCoMo R@1 substantially below lexical confirms a weak vector arm / "
            "RRF-dilution effect; competitive with hybrid reopens the "
            "fusion-mechanism hypothesis."
        )
    else:
        lines.append(
            "This run used **lexical** retrieval (BM25 only). Re-run with "
            "`--retrieval-mode hybrid` to fuse the all-MiniLM-L6-v2 vector "
            "signal (RRF, k=60) and compare against the agentmemory reference."
        )
    lines.append("")
    return "\n".join(lines)


def save_reports(
    aggregate: dict,
    dataset_slug: str,
    retrieval_mode: str = "lexical",
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Save JSON and Markdown report files.

    Args:
        aggregate: Output of compute_aggregate().
        dataset_slug: Safe filename slug (e.g., "longmemeval_s").
        retrieval_mode: Retrieval mode for this run (default ``"lexical"``).
            For ``"lexical"`` the artifacts keep the exact unsuffixed names
            (``{slug}_{date}.{json,md}`` and ``{slug}_latest.{json,md}``) — the
            committed lexical baseline naming. For any non-lexical mode (e.g.
            ``"hybrid"``) a ``_{mode}`` suffix is woven into both the dated
            filenames and the ``_latest`` pointers (``{slug}_{date}_hybrid.*``
            and ``{slug}_latest_hybrid.*``), so a non-lexical run never
            overwrites the lexical baseline or its ``_latest`` symlinks.
        dry_run: If True, skip saving and return None paths.

    Returns:
        Tuple of (json_path, md_path).
    """
    if dry_run:
        logger.info("[dry-run] Skipping report save.")
        return None, None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Lexical keeps the unsuffixed baseline names; any other mode is suffixed so
    # it cannot clobber the committed lexical artifacts.
    suffix = "" if retrieval_mode == "lexical" else f"_{retrieval_mode}"

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_name = f"{dataset_slug}_{date_str}{suffix}.json"
    md_name = f"{dataset_slug}_{date_str}{suffix}.md"

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
        (json_name, f"{dataset_slug}_latest{suffix}.json"),
        (md_name, f"{dataset_slug}_latest{suffix}.md"),
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
        "--sample",
        choices=("head", "stride", "shuffle", "stratified"),
        default="stride",
        help=(
            "How to select the --limit subset (default: stride). "
            "'stride'/'stratified'/'shuffle' span the whole dataset so a "
            "limited run is representative; 'head' is the legacy contiguous "
            "prefix (benchmarks only the easiest category — opt-in)."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for shuffle/stratified sampling (default: 0)",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=("lexical", "hybrid", "vector"),
        default="lexical",
        help=(
            "Retrieval mode (default: lexical). 'lexical' uses BM25 only and "
            "needs no model download; 'hybrid' adds an all-MiniLM-L6-v2 vector "
            "signal fused with BM25 via RRF (k=60) and requires the "
            "[benchmark] extra plus a one-time ~90MB model download; 'vector' "
            "uses the EmbeddingField only (same ~90MB all-MiniLM-L6-v2 download "
            "as hybrid) and ranks by pure cosine with no BM25/RRF — a "
            "harness-local diagnostic that bypasses ContextAssembler."
        ),
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

    # Benchmark DB isolation (issue #465): point the live Popoto connection at a
    # dedicated bench DB (default 14, db0 rejected) and sweep any residue left
    # by prior/interrupted runs BEFORE any ingestion. Scoped to this entrypoint
    # so it never affects import-time behavior or the pytest db15 plugin.
    bench_db = _select_bench_db()

    # When --limit is combined with the legacy contiguous-prefix mode, warn:
    # 'head' benchmarks only the easiest on-disk category and is not
    # representative of the whole dataset.
    if args.sample == "head" and args.limit is not None:
        logger.warning(
            "--sample head with --limit benchmarks only the contiguous "
            "prefix (often a single easy category) and is NOT representative; "
            "use --sample stride (default) or stratified for a real estimate."
        )

    # Select dataset adapter
    if args.dataset == "longmemeval-s":
        dataset_slug = "longmemeval_s"
        items = iter_longmemeval(
            fixture_path=args.fixture,
            limit=args.limit,
            sample=args.sample,
            seed=args.seed,
        )
    elif args.dataset == "locomo":
        dataset_slug = "locomo"
        items = iter_locomo(
            fixture_path=args.fixture,
            limit=args.limit,
            sample=args.sample,
            seed=args.seed,
        )
    else:
        logger.error("Unknown dataset: %s", args.dataset)
        return 1

    logger.info(
        "Starting benchmark: dataset=%s limit=%s sample=%s seed=%s "
        "retrieval_mode=%s dry_run=%s bench_db=%d",
        args.dataset,
        args.limit or "all",
        args.sample,
        args.seed,
        args.retrieval_mode,
        args.dry_run,
        bench_db,
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

        q_result = run_item(item, retrieval_mode=args.retrieval_mode)
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
    aggregate = compute_aggregate(
        results,
        args.dataset,
        sample_mode=args.sample,
        seed=args.seed,
        limit=args.limit,
        retrieval_mode=args.retrieval_mode,
    )
    s = aggregate["summary"]

    # Print summary table
    print("\n" + "=" * 60)
    print(f"BENCHMARK RESULTS: {args.dataset.upper()}")
    print("=" * 60)
    print(f"  Retrieval mode      : {args.retrieval_mode}")
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
        json_path, md_path = save_reports(
            aggregate,
            dataset_slug,
            retrieval_mode=args.retrieval_mode,
            dry_run=False,
        )
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
