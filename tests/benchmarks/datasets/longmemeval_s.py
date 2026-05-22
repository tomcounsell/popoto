"""LongMemEval-S dataset adapter.

Downloads and caches the LongMemEval-S dataset from HuggingFace, then yields
BenchmarkItem namedtuples for each of the 500 evaluation questions.

Dataset: xiaowu0162/longmemeval-cleaned
File:    longmemeval_s_cleaned.json
License: Public research use (see https://github.com/xiaowu0162/longmemeval)
Size:    ~264 MB

Each item represents one question with:
- A conversation history (multiple sessions, ~48 sessions/question)
- A single ground-truth session/turn that contains the answer

Usage:
    from tests.benchmarks.datasets.longmemeval_s import iter_items

    # With downloaded data:
    for item in iter_items():
        print(item.item_id, item.query, item.relevant_ids)

    # From fixture (for testing):
    from pathlib import Path
    for item in iter_items(fixture_path=Path("fixtures/longmemeval_s_sample.json")):
        ...

Caching:
    Downloaded dataset is cached at ~/.cache/popoto_benchmarks/longmemeval_s_cleaned.json.
    Subsequent calls skip the download.
"""

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from . import BenchmarkItem

logger = logging.getLogger("POPOTO.Benchmark.LongMemEvalS")

CACHE_DIR = Path.home() / ".cache" / "popoto_benchmarks"
CACHED_FILE = CACHE_DIR / "longmemeval_s_cleaned.json"

HF_REPO_ID = "xiaowu0162/longmemeval-cleaned"
HF_FILENAME = "longmemeval_s_cleaned.json"


def _download_dataset() -> Path:
    """Download LongMemEval-S from HuggingFace and cache it locally.

    Returns:
        Path to the cached file.

    Raises:
        RuntimeError: If download fails. Includes manual download instructions.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHED_FILE.exists():
        logger.info("LongMemEval-S: using cached file at %s", CACHED_FILE)
        return CACHED_FILE

    logger.info("LongMemEval-S: downloading from HuggingFace (%s)...", HF_REPO_ID)

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise RuntimeError(
            "huggingface_hub is required to download LongMemEval-S. "
            "Install it with: pip install -e '.[benchmark]'\n"
            "Or manually place longmemeval_s_cleaned.json at: "
            f"{CACHED_FILE}"
        )

    try:
        downloaded = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=HF_FILENAME,
            repo_type="dataset",
            local_dir=str(CACHE_DIR),
        )
        logger.info("LongMemEval-S: downloaded to %s", downloaded)
        return Path(downloaded)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download LongMemEval-S: {exc}\n"
            f"Manual download: place '{HF_FILENAME}' at {CACHED_FILE}\n"
            "Source: https://github.com/xiaowu0162/longmemeval"
        ) from exc


def _parse_record(record: dict, idx: int) -> BenchmarkItem:
    """Parse one record from the LongMemEval-S JSON format.

    LongMemEval-S format (longmemeval_s_cleaned.json):
    {
        "question_id": "...",
        "question": "...",
        "answer": "...",
        "evidence_session_id": "...",  # may be a list or str
        "conversation_history": [
            {
                "session_id": "...",
                "turns": [
                    {"role": "user"|"assistant", "content": "..."},
                    ...
                ]
            },
            ...
        ]
    }

    Args:
        record: Raw dict from the JSON file.
        idx: Record index for error messages.

    Returns:
        BenchmarkItem.

    Raises:
        ValueError: If required fields are missing.
    """
    required = ["question", "conversation_history"]
    for field in required:
        if field not in record:
            raise ValueError(
                f"LongMemEval-S record[{idx}] missing required field: {field!r}"
            )

    question_id = record.get("question_id", str(idx))
    query = record["question"]
    raw_history = record["conversation_history"]

    if not isinstance(raw_history, list):
        raise ValueError(
            f"LongMemEval-S record[{idx}] 'conversation_history' must be a list, "
            f"got {type(raw_history).__name__}"
        )

    # Flatten session list into a list of turns with session_id metadata
    history = []
    for session in raw_history:
        session_id = session.get("session_id", "")
        turns = session.get("turns", [])
        for turn_idx, turn in enumerate(turns):
            history.append(
                {
                    "role": turn.get("role", "user"),
                    "content": turn.get("content", ""),
                    "turn_id": f"{session_id}::{turn_idx}",
                    "session_id": session_id,
                }
            )

    # Ground truth: evidence_session_id identifies the relevant session
    raw_evidence = record.get("evidence_session_id", "")
    if isinstance(raw_evidence, list):
        relevant_ids = set(raw_evidence)
    elif raw_evidence:
        relevant_ids = {raw_evidence}
    else:
        relevant_ids = set()

    return BenchmarkItem(
        item_id=question_id,
        history=history,
        query=query,
        relevant_ids=relevant_ids,
        metadata={
            "answer": record.get("answer", ""),
            "question_type": record.get("question_type", ""),
            "dataset": "longmemeval-s",
        },
    )


def iter_items(
    fixture_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Iterator[BenchmarkItem]:
    """Iterate over LongMemEval-S benchmark items.

    Args:
        fixture_path: If set, load from this JSON file instead of
            downloading. Used for unit tests (fixture-based).
        limit: If set, yield at most this many items.

    Yields:
        BenchmarkItem namedtuples.

    Raises:
        RuntimeError: If download fails (when fixture_path is None).
        ValueError: If the JSON file is malformed.
    """
    if fixture_path is not None:
        source = fixture_path
        logger.info("LongMemEval-S: loading from fixture %s", source)
    else:
        source = _download_dataset()

    with open(source) as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(
            f"Expected a JSON array at the top level of {source}, "
            f"got {type(data).__name__}"
        )

    count = 0
    for idx, record in enumerate(data):
        if limit is not None and count >= limit:
            break
        try:
            item = _parse_record(record, idx)
        except ValueError as exc:
            logger.warning("Skipping malformed record[%d]: %s", idx, exc)
            continue
        yield item
        count += 1

    logger.info("LongMemEval-S: yielded %d items", count)
