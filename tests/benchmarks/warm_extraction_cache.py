"""Warm the #489 extraction cache over the bounded LoCoMo sample.

Extracts every UNIQUE turn in the 2-dialogue fixture once per model tier and
persists to the on-disk cache, so the benchmark runs that follow make zero
API calls and every tier sees byte-identical inputs.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from tests.benchmarks.datasets.locomo import iter_items
from tests.benchmarks.extraction_axis import resolve_arm

FIXTURE = sys.argv[1]
MODELS = sys.argv[2].split(",")
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 12

items = iter_items(fixture_path=__import__("pathlib").Path(FIXTURE))
# All QA items of one dialogue share the same history list; dedupe by text.
turns = []
seen = set()
for it in items:
    for t in it.history:
        c = (t.get("content") or "").strip()
        if c and c not in seen:
            seen.add(c)
            turns.append(c)
print(f"unique turns: {len(turns)}")

summary = {}
for model in MODELS:
    provider, stats, ident = resolve_arm("claude", model=model)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        facts = list(ex.map(provider.extract, turns))
    provider.flush()
    el = time.monotonic() - t0
    nf = sum(len(f) for f in facts)
    empty = sum(1 for f in facts if not f)
    summary[model] = {
        "unique_turns": len(turns),
        "api_calls": stats.calls,
        "cache_hits": stats.cache_hits,
        "failures": stats.failures,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cost_usd": round(stats.cost_usd(model), 4),
        "facts_total": nf,
        "facts_per_turn": round(nf / len(turns), 4),
        "turns_yielding_zero_facts": empty,
        "wall_seconds": round(el, 1),
    }
    print(model, json.dumps(summary[model]))

print("\nTOTAL COST USD:", round(sum(v["cost_usd"] for v in summary.values()), 4))
json.dump(summary, open("warm_summary.json", "w"), indent=2)
