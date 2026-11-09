"""Dated-memory adapter: questions asked on a day, against a dated corpus.

A deployment's own memory, measured as it stood on the day a question was
asked. Unlike LongMemEval-S and LoCoMo, which hand each question its own
haystack, this dataset is one corpus of dated records shared by every
question; a question's history is the records of its ``project`` dated
before its ``asked_on``. Records with no date (``"at": null``) stand before
every question, as an undated memory precedes every later one in use.
Ground truth is record-level (``turn`` unit).

Fixture JSON (``--dataset dated --fixture PATH``; there is no public copy,
the corpus is a deployment's own and stays private)::

    {
      "version": "v1",
      "records": [
        {"id": "r1", "text": "...", "project": "p", "at": "2026-06-01T09:00:00+00:00"},
        {"id": "r2", "text": "...", "project": "p", "at": null}
      ],
      "questions": [
        {"id": "q1", "type": "long-term", "asked_on": "2026-07-01",
         "project": "p", "query": "...", "gold": ["r1"]}
      ]
    }

A question with no ``gold`` (a confinement question, say) is skipped: it has
nothing to recall.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import BenchmarkItem
from .sampling import sample_items


def _epoch(value: str) -> float:
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def history_before(records: List[dict], project: str, asked_on: str) -> List[dict]:
    """The records of ``project`` from before ``asked_on``, undated ones
    first, then by time, as history rows."""
    asked = _epoch(asked_on)
    kept = [
        r
        for r in records
        if r.get("project") == project
        and (r.get("at") is None or _epoch(r["at"]) < asked)
    ]
    kept.sort(key=lambda r: (r.get("at") is not None, r.get("at") or ""))
    return [
        {
            "role": "user",
            "content": r["text"],
            "turn_id": r["id"],
            "session_id": r["id"],
            "session_date": _epoch(r["at"]) if r.get("at") else None,
        }
        for r in kept
    ]


def iter_items(
    fixture_path: Optional[Path] = None,
    limit: Optional[int] = None,
    sample: str = "stride",
    seed: int = 0,
) -> List[BenchmarkItem]:
    """One BenchmarkItem per question with gold records."""
    if fixture_path is None:
        raise RuntimeError("the dated dataset has no public copy: pass --fixture PATH")
    data = json.loads(Path(fixture_path).read_text())
    items = []
    for q in data["questions"]:
        gold = set(q.get("gold") or ())
        if not gold:
            continue
        items.append(
            BenchmarkItem(
                item_id=str(q["id"]),
                history=history_before(data["records"], q["project"], q["asked_on"]),
                query=q["query"],
                relevant_ids=gold,
                metadata={
                    "dataset": "dated",
                    "question_type": q.get("type", ""),
                    "asked_on": q["asked_on"],
                    "question_set": data.get("version", ""),
                },
            )
        )
    return sample_items(items, limit, mode=sample, seed=seed)
