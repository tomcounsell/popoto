"""RLT — Retrieval Latency & Throughput CLI (#460).

Manual/ad-hoc entrypoint for the **real** headline measurement runs. Mirrors
``tests/benchmarks/run_external.py``'s shape (argparse, machine metadata,
JSON+MD artifact writer with ``_latest`` pointers). NOT invoked by CI/pytest —
``tests/benchmarks/test_rlt.py`` (pytest, DB 15 only) is the CI-facing
correctness gate for this harness's code paths; this CLI is for running real
numbers against a real corpus once the machine is confirmed quiet.

Usage:
    # Requires --db explicitly — no default (see DB hygiene below):
    python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis
    python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis --mixed-workload
    python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis --dry-run

DB hygiene (issue #465 lineage — two prior incidents leaked keys to DB 0 by
running harness code outside pytest):
    ``--db`` is REQUIRED (unlike ``run_external.py``'s ``POPOTO_BENCH_DB``
    default-14 pattern). ``--db 0``, ``--db 14``, and ``--db 15`` are all
    rejected: 0 is production-shaped, 14 is reserved for the concurrently
    running external-benchmark chain (#457) at the time this harness was
    built, and 15 is the project's dedicated pytest-isolation DB (flushed by
    every test session) — a manual run against it would either get wiped
    mid-run by a concurrent ``pytest`` invocation or pollute the DB tests
    rely on being clean.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("RLTBenchmark")

RESULTS_DIR = Path(__file__).parent.parent / "results" / "rlt"

#: DBs this harness's manual CLI refuses to target, and why:
#:   0  -> production-shaped (mirrors run_external.py's db0 rejection).
#:   14 -> reserved for the concurrently running #457 external-benchmark chain
#:         at the time this harness was built (run_external.py's own default).
#:   15 -> the project's pytest DB-15 isolation target; flushed by every test
#:         session, so a manual run here would race with or pollute tests.
FORBIDDEN_DBS = frozenset({0, 14, 15})


class RltDbError(ValueError):
    """An invalid/forbidden ``--db`` value was supplied."""


def validate_db(db: int) -> int:
    """Validate a ``--db`` value against :data:`FORBIDDEN_DBS`.

    Returns:
        ``db`` unchanged, if valid.

    Raises:
        RltDbError: If ``db`` is in :data:`FORBIDDEN_DBS`.
    """
    if db in FORBIDDEN_DBS:
        raise RltDbError(
            f"--db {db} is not allowed (forbidden: {sorted(FORBIDDEN_DBS)} — "
            "0 is production-shaped, 14 is reserved for the concurrent "
            "external-benchmark chain, 15 is the pytest DB-15 isolation "
            "target). Pick an explicit, otherwise-unused DB number."
        )
    return db


def _point_connection_at_db(target_db: int) -> None:
    """Swap the Popoto connection pool in-place onto ``target_db``.

    Mirrors ``run_external.py``'s ``_point_connection_at_db`` /
    ``popoto.pytest_plugin._swap_db`` — mutates the existing
    ``POPOTO_REDIS_DB`` object rather than rebinding the global.
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


def build_machine_metadata(backend: str) -> dict:
    """Machine metadata block matching ``run_external.py``'s existing convention,
    extended with a ``backend`` field (redis/valkey — the first RLT-specific
    convention, since RLT is the first benchmark where the two backends could
    measurably differ)."""
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "backend": backend,
    }


def save_reports(aggregate: dict, backend: str, dry_run: bool = False):
    """Save JSON + Markdown report files, mirroring ``run_external.py``'s convention."""
    if dry_run:
        logger.info("[dry-run] Skipping report save.")
        return None, None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_name = f"rlt_{date_str}_{backend}.json"
    md_name = f"rlt_{date_str}_{backend}.md"
    json_path = RESULTS_DIR / json_name
    md_path = RESULTS_DIR / md_name

    with open(json_path, "w") as f:
        json.dump(aggregate, f, indent=2, default=str)
    logger.info("Saved JSON report: %s", json_path)

    md_lines = [
        f"# Popoto RLT Benchmark ({backend})",
        "",
        f"**Run date:** {aggregate['run_date']}  ",
        f"**Backend:** {backend}  ",
        f"**Python:** {aggregate['machine']['python_version']}  ",
        f"**Platform:** {aggregate['machine']['platform']}  ",
        "",
        "See the JSON artifact for full per-size / mixed-workload detail.",
        "",
    ]
    md_path.write_text("\n".join(md_lines))
    logger.info("Saved Markdown report: %s", md_path)

    for src_name, latest_name in [
        (json_name, f"rlt_latest_{backend}.json"),
        (md_name, f"rlt_latest_{backend}.md"),
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="RLT — Retrieval Latency & Throughput benchmark (issue #460)."
    )
    parser.add_argument(
        "--db",
        type=int,
        required=True,
        help=(
            "Redis/Valkey DB to run against. REQUIRED, no default. Rejects "
            f"{sorted(FORBIDDEN_DBS)} (production / concurrent-benchmark / "
            "pytest-isolation reservations — see module docstring)."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("redis", "valkey"),
        required=True,
        help=(
            "Informational label for the server REDIS_URL already points at "
            "(this CLI does not provision a server). Recorded in the artifact."
        ),
    )
    parser.add_argument(
        "--corpus-sizes",
        type=int,
        nargs="+",
        default=None,
        help="Scaling-curve corpus sizes (default: 1000 5000 10000 20000).",
    )
    parser.add_argument(
        "--mixed-workload",
        action="store_true",
        help="Also run the live mixed-workload measurement.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and print the plan without connecting or measuring.",
    )
    args = parser.parse_args(argv)

    try:
        validate_db(args.db)
    except RltDbError as exc:
        logger.error(str(exc))
        return 1

    if args.dry_run:
        print(
            f"[dry-run] Would run RLT against db={args.db} backend={args.backend} "
            f"corpus_sizes={args.corpus_sizes or 'default'} "
            f"mixed_workload={args.mixed_workload}"
        )
        return 0

    _point_connection_at_db(args.db)
    logger.info("RLT harness pointed at DB %d (backend=%s).", args.db, args.backend)

    from .scaling import DEFAULT_CORPUS_SIZES, run_scaling_curve

    corpus_sizes = args.corpus_sizes or list(DEFAULT_CORPUS_SIZES)
    logger.info("Running scaling curve over sizes: %s", corpus_sizes)
    scaling_points = run_scaling_curve(corpus_sizes=corpus_sizes)

    aggregate = {
        "benchmark": "rlt",
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "machine": build_machine_metadata(args.backend),
        "scaling_curve": [p.to_dict() for p in scaling_points],
        "notes": [
            "Latency measured by timing ContextAssembler.assemble() end to "
            "end (retrieve->rank->inject) — assemble_latency and "
            "retrieval_latency refer to the same measured call in this "
            "harness; there is no separate lower-level retrieval-only API "
            "surface to isolate without changing src/popoto/.",
        ],
    }

    if args.mixed_workload:
        logger.info(
            "Mixed-workload run not wired into the default CLI path in this "
            "PR — see rlt/mixed_workload.py's run_mixed_workload() for the "
            "harness function; a follow-up wires it into this CLI with a "
            "real corpus once the machine is confirmed quiet."
        )
        aggregate["mixed_workload"] = None

    save_reports(aggregate, args.backend, dry_run=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
