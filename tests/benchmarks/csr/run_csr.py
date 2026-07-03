"""Runner + report writer for the deterministic CSR harness (issue #418).

Per case: plant the corpus -> build a ``ContextAssembler`` (case overrides or
the lexical defaults) -> ``assemble()`` twice (standard, adversarial) -> map
records back to planted ids -> ``evaluate_all`` each ranking -> record
per-case standard/adversarial CSR, ``rankings_identical`` and per-run
``executed_path``. Aggregate to RSR(standard), RSR(adversarial), Adversarial
Gap, per-primitive pass rates and per-case query-blindness flags, then write
``results/csr/csr_{date}.{json,md}`` + ``csr_latest.*`` (mirroring the
``run_external.save_reports`` conventions in the harness's own namespace).

Signal semantics (see the package docstring): a LARGE Adversarial Gap on the
lexical path is healthy keyword dependence; the #409 query-blind signature is
``rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`` — an identical
ranked list for both queries plus a low standard CSR.

Determinism: no unseeded randomness, no LLM, no wall-clock dependence in
scoring (the report's ``run_date`` is metadata only). Errors mark the case
``status="error"`` with zero passed assertions; they never crash the run.

Usage::

    python -m tests.benchmarks.csr.run_csr             # write report artifact
    python -m tests.benchmarks.csr.run_csr --dry-run   # print summary only
"""

import argparse
import json
import logging
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.popoto.fields.bm25_field import BM25Field
from src.popoto.recipes.context_assembler import ContextAssembler

from . import ADVERSARIAL_GAP_ALERT, ASSEMBLER_MAX_ITEMS, QUERY_BLIND_CSR_ALERT
from .corpus import BM25_FIELD_NAME, cleanup, plant
from .satisfaction import evaluate_all

logger = logging.getLogger("POPOTO.Benchmark.CSR")

RESULTS_DIR = Path(__file__).parent.parent / "results" / "csr"

RUN_LABELS = ("standard", "adversarial")


def _run_one_query(assembler, model_class, agent_id, query):
    """Execute one assemble() run and score it.

    Records ``executed_path`` per run so a silent composite fallback can
    never masquerade as a lexical number: for lexical/hybrid modes a
    pre-flight ``BM25Field.search`` hit count decides ``"lexical"`` vs
    ``"composite-fallback"`` (zero hits degrade to the query-blind composite
    path inside ``_pull_path_hybrid``); an explicit composite mode records
    ``"composite"``.
    """
    if assembler._effective_mode in ("lexical", "hybrid"):
        preflight_hits = BM25Field.search(model_class, BM25_FIELD_NAME, query, limit=1)
        executed_path = "lexical" if preflight_hits else "composite-fallback"
    else:
        executed_path = "composite"

    result = assembler.assemble(query_cues={"topic": query}, agent_id=agent_id)
    return result, executed_path


def run_case(case) -> dict:
    """Plant, retrieve (x2), score and clean up one CsrTestCase.

    Returns a per-case result dict. Any exception marks the case
    ``status="error"`` with zero passed assertions (surfaced in the report,
    never crashing the suite run).
    """
    planted_meta = {
        m.id: {
            "timestamp": m.timestamp,
            "topic": m.topic,
            "importance": m.importance,
        }
        for m in case.planted_corpus
    }
    n_assertions = len(case.assertions)
    base = {
        "case_id": case.case_id,
        "primitive": case.primitive,
        "retrieval_mode": case.retrieval_mode,
        "n_memories": len(case.planted_corpus),
        "n_assertions": n_assertions,
    }

    model_class = None
    agent_id = None
    try:
        model_class, agent_id, _, reverse_map = plant(case)
        assembler = ContextAssembler(
            model_class,
            score_weights=case.score_weights,
            max_items=ASSEMBLER_MAX_ITEMS,
            retrieval_mode=case.retrieval_mode,
        )

        runs = []
        rankings = {}
        for label, query in zip(
            RUN_LABELS, (case.standard_query, case.adversarial_query)
        ):
            result, executed_path = _run_one_query(
                assembler, model_class, agent_id, query
            )
            ranked_ids = []
            for record in result.records:
                planted_id = reverse_map.get(record.db_key.redis_key)
                if planted_id is not None:
                    ranked_ids.append(planted_id)
            passed, total, per_assertion = evaluate_all(
                case.assertions, ranked_ids, planted_meta
            )
            rankings[label] = ranked_ids
            runs.append(
                {
                    "run": label,
                    "query": query,
                    "executed_path": executed_path,
                    "effective_mode": assembler._effective_mode,
                    "ranked_ids": ranked_ids,
                    "passed": passed,
                    "total": total,
                    "csr": (passed / total) if total else 0.0,
                    "assertions": per_assertion,
                }
            )

        rankings_identical = rankings["standard"] == rankings["adversarial"]
        csr_std = runs[0]["csr"]
        csr_adv = runs[1]["csr"]
        return {
            **base,
            "status": "ok",
            "runs": runs,
            "csr_std": csr_std,
            "csr_adv": csr_adv,
            "rankings_identical": rankings_identical,
            # The #409 signature: the ranking ignores the query (identical
            # ranked lists) AND cannot satisfy query-relevance assertions.
            "query_blind_flag": rankings_identical and csr_std < QUERY_BLIND_CSR_ALERT,
        }
    except Exception as e:
        logger.warning("CSR case %s errored: %s", case.case_id, e)
        return {
            **base,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "runs": [],
            "csr_std": 0.0,
            "csr_adv": 0.0,
            "rankings_identical": False,
            "query_blind_flag": False,
        }
    finally:
        if model_class is not None:
            try:
                cleanup(model_class, agent_id)
            except Exception as e:
                logger.warning("CSR cleanup failed for %s: %s", case.case_id, e)


def compute_aggregate(case_results) -> dict:
    """Aggregate per-case results into the report dict.

    Error cases count with zero passed assertions (CSR 0.0) — they lower
    RSR visibly rather than silently dropping out of the mean.
    """
    n_total = len(case_results)
    n_errors = sum(1 for c in case_results if c["status"] == "error")
    rsr_std = sum(c["csr_std"] for c in case_results) / n_total if n_total else 0.0
    rsr_adv = sum(c["csr_adv"] for c in case_results) / n_total if n_total else 0.0
    gap = rsr_std - rsr_adv

    by_primitive = {}
    for c in case_results:
        bucket = by_primitive.setdefault(
            c["primitive"], {"n": 0, "csr_std_sum": 0.0, "csr_adv_sum": 0.0}
        )
        bucket["n"] += 1
        bucket["csr_std_sum"] += c["csr_std"]
        bucket["csr_adv_sum"] += c["csr_adv"]
    per_primitive = {
        prim: {
            "n": b["n"],
            "csr_std": b["csr_std_sum"] / b["n"],
            "csr_adv": b["csr_adv_sum"] / b["n"],
        }
        for prim, b in sorted(by_primitive.items())
    }

    return {
        "harness": "csr",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "machine": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "constants": {
            "ASSEMBLER_MAX_ITEMS": ASSEMBLER_MAX_ITEMS,
            "ADVERSARIAL_GAP_ALERT": ADVERSARIAL_GAP_ALERT,
            "QUERY_BLIND_CSR_ALERT": QUERY_BLIND_CSR_ALERT,
        },
        "summary": {
            "n_cases": n_total,
            "n_errors": n_errors,
            "rsr_std": rsr_std,
            "rsr_adv": rsr_adv,
            "adversarial_gap": gap,
            "gap_alert": gap >= ADVERSARIAL_GAP_ALERT,
            "query_blind_cases": [
                c["case_id"] for c in case_results if c["query_blind_flag"]
            ],
        },
        "per_primitive": per_primitive,
        "cases": case_results,
    }


def build_markdown_report(aggregate: dict) -> str:
    """Build the Markdown report from :func:`compute_aggregate` output."""
    s = aggregate["summary"]
    lines = [
        "# Popoto Deterministic CSR Benchmark",
        "",
        f"**Run date:** {aggregate['run_date']}  ",
        f"**Python:** {aggregate['machine']['python_version']}  ",
        f"**Platform:** {aggregate['machine']['platform']}  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Cases | {s['n_cases']} |",
        f"| Errors | {s['n_errors']} |",
        f"| RSR (standard) | {s['rsr_std']:.4f} |",
        f"| RSR (adversarial) | {s['rsr_adv']:.4f} |",
        f"| Adversarial Gap | {s['adversarial_gap']:.4f} |",
        "",
    ]

    if s["gap_alert"]:
        lines += [
            f"**Gap alert:** Adversarial Gap >= {ADVERSARIAL_GAP_ALERT} — "
            "likely keyword-dependent retrieval. Expected of the pure-BM25 "
            "lexical path (healthy signature); this is NOT the #409 "
            "query-blindness signature.",
            "",
        ]

    if s["query_blind_cases"]:
        lines += [
            "**Query-blindness flags (the #409 signature — identical "
            f"rankings + standard CSR < {QUERY_BLIND_CSR_ALERT}):** "
            + ", ".join(f"`{cid}`" for cid in s["query_blind_cases"]),
            "",
        ]

    lines += [
        "## Per primitive",
        "",
        "| Primitive | n | CSR (std) | CSR (adv) |",
        "|---|---|---|---|",
    ]
    for prim, m in aggregate["per_primitive"].items():
        lines.append(f"| {prim} | {m['n']} | {m['csr_std']:.4f} | {m['csr_adv']:.4f} |")

    lines += [
        "",
        "## Per case",
        "",
        "| Case | Primitive | Status | CSR (std) | CSR (adv) | "
        "Rankings identical | Query-blind |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in aggregate["cases"]:
        if c["status"] == "error":
            lines.append(
                f"| {c['case_id']} | {c['primitive']} | ERROR: "
                f"{c.get('error', '?')} | 0.0000 | 0.0000 | - | - |"
            )
        else:
            lines.append(
                f"| {c['case_id']} | {c['primitive']} | ok "
                f"| {c['csr_std']:.4f} | {c['csr_adv']:.4f} "
                f"| {c['rankings_identical']} | {c['query_blind_flag']} |"
            )
    lines += [
        "",
        "## Notes",
        "",
        "- Deterministic: lexical/BM25 + composite control only — no LLM "
        "judge, no embedding model, no Redis module. Identical on Redis "
        "and Valkey.",
        "- A LARGE Adversarial Gap on the lexical path = keyword dependence "
        "(healthy). Query-blindness (#409) = identical rankings for both "
        "queries AND a low standard CSR.",
        "- `executed_path` is recorded per run in the JSON; a composite "
        "fallback can never masquerade as a lexical number.",
        "",
    ]
    return "\n".join(lines)


def save_reports(aggregate: dict, dry_run: bool = False):
    """Write ``csr_{date}.{json,md}`` + ``csr_latest.*`` into results/csr/."""
    if dry_run:
        logger.info("[dry-run] Skipping report save.")
        return None, None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_name = f"csr_{date_str}.json"
    md_name = f"csr_{date_str}.md"

    json_path = RESULTS_DIR / json_name
    md_path = RESULTS_DIR / md_name

    with open(json_path, "w") as f:
        json.dump(aggregate, f, indent=2, default=str)
    logger.info("Saved JSON report: %s", json_path)

    with open(md_path, "w") as f:
        f.write(build_markdown_report(aggregate))
    logger.info("Saved Markdown report: %s", md_path)

    for src_name, latest_name in [
        (json_name, "csr_latest.json"),
        (md_name, "csr_latest.md"),
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


def run_suite(cases) -> dict:
    """Run every case and return the aggregate dict."""
    return compute_aggregate([run_case(case) for case in cases])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic CSR suite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the suite and print the summary without writing reports",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Import here so `--help` never touches Redis or the suite lint.
    from .suites.default import SUITE

    aggregate = run_suite(SUITE)
    s = aggregate["summary"]
    print(f"Cases: {s['n_cases']} (errors: {s['n_errors']})")
    print(f"RSR (standard):    {s['rsr_std']:.6f}")
    print(f"RSR (adversarial): {s['rsr_adv']:.6f}")
    print(f"Adversarial Gap:   {s['adversarial_gap']:.6f}")
    if s["gap_alert"]:
        print(
            f"Gap alert: gap >= {ADVERSARIAL_GAP_ALERT} "
            "(keyword dependence — expected of lexical BM25)"
        )
    if s["query_blind_cases"]:
        print(f"Query-blind cases (#409 signature): {s['query_blind_cases']}")

    save_reports(aggregate, dry_run=args.dry_run)
    return 1 if s["n_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
