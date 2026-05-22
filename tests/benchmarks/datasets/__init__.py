"""External benchmark dataset adapters.

Provides BenchmarkItem namedtuple and adapter generators for:
- LongMemEval-S (longmemeval_s.py)
- LoCoMo (locomo.py)

All adapters yield BenchmarkItem namedtuples with a standard shape
so the ExternalScenario base class and run_external.py CLI can treat
both datasets uniformly.
"""

from collections import namedtuple

BenchmarkItem = namedtuple(
    "BenchmarkItem",
    [
        "item_id",        # Unique identifier for this question
        "history",        # list[dict] with "role", "content", "turn_id" keys
        "query",          # str — the question to answer via retrieval
        "relevant_ids",   # set[str] — ground-truth turn_ids (usually 1 item)
        "metadata",       # dict — dataset-specific extra fields
    ],
)
