"""CLI entry point for the SIQ benchmark (issue #459).

Runs every committed trace against a chosen adapter, prints a per-trace + mean
table, and (unless ``--dry-run``) writes JSON + Markdown artifacts under
``tests/benchmarks/results/siq/`` following the Tier-5 naming convention
(``siq_{YYYYMMDD}_{adapter}.{json,md}`` + ``siq_latest_{adapter}.*`` pointers).

DB hygiene: this is a **manual CLI, not a pytest test**, so the db15 isolation
plugin does not apply. It therefore reuses ``run_external``'s DB machinery — a
dedicated bench DB (default 14, ``POPOTO_BENCH_DB`` override, **db0 rejected**)
and an in-place connection swap done only at ``main()`` time — so it never
touches db0 and never runs at import time. The **required, CI-facing** SIQ
surface is ``tests/benchmarks/test_siq.py`` (db15 plugin); this CLI is for
ad-hoc/publishable report generation. To avoid colliding with a running
external-benchmark chain on db14, point it at a free DB, e.g.
``POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq``.

Usage:
    python -m tests.benchmarks.siq.run_siq                 # native adapter
    python -m tests.benchmarks.siq.run_siq --adapter query_stub
    python -m tests.benchmarks.siq.run_siq --dry-run       # no artifacts
    POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq

Real Mem0/Zep/Hindsight adapters are a tracked follow-up; only ``native`` and
``query_stub`` are registered today (no fabricated competitor numbers).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from tests.benchmarks.run_external import (
    _point_connection_at_db,
    _resolve_bench_db,
)

from .adapters import ADAPTERS
from .corpus import FIXTURES_DIR, load_all_traces
from .metrics import _mean
from .runner import run_trace

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results" / "siq"


def run_suite(adapter_name: str) -> dict:
    """Run every committed trace against ``adapter_name``; return a report dict."""
    factory = ADAPTERS[adapter_name]
    traces = load_all_traces(FIXTURES_DIR)
    per_trace = [run_trace(t, factory) for t in traces]

    def _agg(key):
        return _mean([r[key] for r in per_trace])

    return {
        "adapter": adapter_name,
        "n_traces": len(per_trace),
        "aggregate": {
            "precision_at_budget": _agg("precision_at_budget"),
            "recall_at_budget": _agg("recall_at_budget"),
            "budget_efficiency": _agg("budget_efficiency"),
            "mean_anticipation_lead": _agg("mean_anticipation_lead"),
            "total_anticipation_misses": sum(
                r["anticipation_misses"] for r in per_trace
            ),
            "total_recall_targets": sum(r["n_recall_targets"] for r in per_trace),
        },
        "per_trace": per_trace,
    }


def build_markdown(report: dict) -> str:
    agg = report["aggregate"]
    lines = [
        f"# SIQ Benchmark — adapter: `{report['adapter']}`",
        "",
        "Subconscious Injection Quality (issue #459). Deterministic committed "
        "fixtures; no runtime RNG. See `docs/benchmarks.md`.",
        "",
        "## Aggregate",
        "",
        "| metric | value |",
        "|---|---|",
        f"| precision @ budget | {_fmt(agg['precision_at_budget'])} |",
        f"| recall @ budget | {_fmt(agg['recall_at_budget'])} |",
        f"| budget efficiency | {_fmt(agg['budget_efficiency'])} |",
        f"| mean anticipation lead (turns) | {_fmt(agg['mean_anticipation_lead'])} |",
        f"| anticipation misses | {agg['total_anticipation_misses']}"
        f" / {agg['total_recall_targets']} targets |",
        "",
        "## Per trace",
        "",
        "| trace | recall | precision | efficiency | mean lead | misses |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["per_trace"]:
        lines.append(
            f"| {r['trace_id']} | {_fmt(r['recall_at_budget'])} | "
            f"{_fmt(r['precision_at_budget'])} | {_fmt(r['budget_efficiency'])} | "
            f"{_fmt(r['mean_anticipation_lead'])} | "
            f"{r['anticipation_misses']}/{r['n_recall_targets']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def save_reports(report: dict, results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    date = _dt.datetime.now().strftime("%Y%m%d")
    adapter = report["adapter"]
    json_path = results_dir / f"siq_{date}_{adapter}.json"
    md_path = results_dir / f"siq_{date}_{adapter}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(build_markdown(report))
    # latest pointers (plain copies — mirrors run_external's convention)
    (results_dir / f"siq_latest_{adapter}.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    (results_dir / f"siq_latest_{adapter}.md").write_text(build_markdown(report))
    return json_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIQ benchmark.")
    parser.add_argument(
        "--adapter",
        choices=sorted(ADAPTERS),
        default="native",
        help="Which memory system to replay (default: native).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report but do not write artifacts.",
    )
    args = parser.parse_args(argv)

    # DB hygiene: point the connection at the dedicated bench DB (default 14,
    # db0 rejected) BEFORE any planting. Never runs at import time.
    bench_db = _resolve_bench_db()
    _point_connection_at_db(bench_db)
    print(f"SIQ: bench DB = {bench_db} (db0 rejected; db15 is pytest-only)")

    report = run_suite(args.adapter)
    print(build_markdown(report))

    if not args.dry_run:
        path = save_reports(report)
        print(f"\nArtifacts written: {path.parent}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
