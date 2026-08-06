"""Write a dialogue-scoped LoCoMo subset fixture (#489).

The external harness bounds a run with ``--limit``, which samples over the
flat QA list — a 100-item stratified sample spans most of the 10 dialogues
and therefore touches nearly every one of LoCoMo's 5,882 unique turns. When
the ingest arm costs an API call per turn, the useful bound is on
*dialogues*, not questions: 2 dialogues is 788 unique turns and still 304 QA
pairs to sample from.

Usage:
    python tests/benchmarks/make_locomo_subset.py conv-26,conv-30 /tmp/sub.json
"""

import json
import sys
from pathlib import Path

CACHED = Path.home() / ".cache" / "popoto_benchmarks" / "locomo.json"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    sample_ids = set(sys.argv[1].split(","))
    out = Path(sys.argv[2])

    if not CACHED.exists():
        print(f"LoCoMo not cached at {CACHED}; run the benchmark once to fetch it.")
        return 1

    data = json.loads(CACHED.read_text())
    keep = [d for d in data if d.get("sample_id") in sample_ids]
    missing = sample_ids - {d.get("sample_id") for d in keep}
    if missing:
        print(f"warning: sample_id(s) not found: {sorted(missing)}")
    if not keep:
        return 1

    out.write_text(json.dumps(keep))
    turns = sum(
        1
        for d in keep
        for k, v in d["conversation"].items()
        if k.startswith("session_") and not k.endswith("_date_time")
        for t in v
        if (t.get("text") or t.get("blip_caption") or "").strip()
    )
    qa = sum(len(d["qa"]) for d in keep)
    print(f"wrote {out}: {len(keep)} dialogues, {turns} turns, {qa} QA pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
