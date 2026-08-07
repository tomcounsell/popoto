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

# Ranking unit per dataset (issue #514).
#
# Each dataset states its ground truth at one granularity: LongMemEval-S
# annotates evidence at the *session* level (``answer_session_ids``), LoCoMo at
# the *turn* level (``qa[].evidence`` = ``dia_id``s). The retrieval harness must
# collapse retrieved records to that one unit for EVERY retrieved record —
# gold and non-gold alike — otherwise gold records get a ranking granularity
# that non-gold records do not, which lifts recall/MRR artificially.
#
# This mapping is a fixed property of the dataset, known before any question is
# asked. It is deliberately NOT derived from an item's ``relevant_ids``: the
# answer key must not influence which candidates are ranked or how they are
# deduplicated, only the final scoring.
GROUND_TRUTH_UNITS = ("session", "turn")

_DATASET_GROUND_TRUTH_UNIT = {
    "longmemeval-s": "session",
    "longmemeval_s": "session",
    "locomo": "turn",
}

# Unit used when a dataset name is unknown (e.g. hand-built fixtures in tests).
DEFAULT_GROUND_TRUTH_UNIT = "session"


def ground_truth_unit(dataset: str) -> str:
    """Return the ranking unit (``"session"`` / ``"turn"``) for ``dataset``.

    Args:
        dataset: Dataset label as it appears in ``BenchmarkItem.metadata
            ["dataset"]`` or on the ``--dataset`` CLI flag.

    Returns:
        ``"session"`` or ``"turn"``. Unknown labels fall back to
        :data:`DEFAULT_GROUND_TRUTH_UNIT`.
    """
    return _DATASET_GROUND_TRUTH_UNIT.get(
        (dataset or "").strip().lower(), DEFAULT_GROUND_TRUTH_UNIT
    )
