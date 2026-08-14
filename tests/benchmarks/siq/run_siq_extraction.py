"""Run the SIQ suite across #489 ingest arms and emit a comparison artifact.

Arms: the committed ``native`` baseline (fixture-authored memories, hand-tuned
importance) plus one extraction arm per provider, which extracts from the raw
utterance and takes the provider's importance.
"""

import json
import sys
from pathlib import Path

from tests.benchmarks.extraction_axis import resolve_arm
from tests.benchmarks.run_external import _point_connection_at_db, _resolve_bench_db
from tests.benchmarks.siq.adapters import NativeAdapter
from tests.benchmarks.siq.corpus import load_all_traces
from tests.benchmarks.siq.extraction_adapter import ExtractionAdapter
from tests.benchmarks.siq.run_siq import FIXTURES_DIR
from tests.benchmarks.siq.runner import run_trace


def _mean(vals):
    d = [v for v in vals if v is not None]
    return sum(d) / len(d) if d else None


def main():
    bench_db = _resolve_bench_db()
    _point_connection_at_db(bench_db)
    traces = load_all_traces(FIXTURES_DIR)
    print(f"SIQ bench DB={bench_db}  traces={len(traces)}")

    arms = [("native", None, None)]
    for spec in sys.argv[1:]:
        if spec == "heuristic":
            arms.append(("heuristic", "heuristic", None))
        else:
            arms.append((spec, "claude", spec))

    report = {"bench_db": bench_db, "n_traces": len(traces), "arms": {}}

    for label, arm, model in arms:
        if arm is None:
            factory = NativeAdapter
            ident = {"arm": "native (fixture-authored memories)"}
            diag = None
        else:
            provider, stats, ident = resolve_arm(arm, model=model or "claude-opus-4-8")
            diags = []

            def factory(trace, _p=provider, _l=label, _d=diags):
                a = ExtractionAdapter(trace, _p, _l)
                _d.append(a)
                return a

            diag = diags

        per_trace = [run_trace(t, factory) for t in traces]
        agg = {
            k: _mean([r[k] for r in per_trace])
            for k in (
                "precision_at_budget",
                "recall_at_budget",
                "budget_efficiency",
                "mean_anticipation_lead",
            )
        }
        agg["total_anticipation_misses"] = sum(
            r["anticipation_misses"] for r in per_trace
        )
        agg["total_recall_targets"] = sum(r["n_recall_targets"] for r in per_trace)
        entry = {"identity": ident, "aggregate": agg, "per_trace": per_trace}
        if diag:
            merged = {}
            for a in diag:
                for k, v in a.stats.items():
                    merged[k] = merged.get(k, 0) + v
            entry["ingest_stats"] = merged
        report["arms"][label] = entry
        print(f"\n=== {label} ===")
        print(json.dumps(agg, indent=2))
        if diag:
            print("ingest:", json.dumps(entry["ingest_stats"]))

    out = Path("tests/benchmarks/results/siq/siq_extraction_axis_489.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
