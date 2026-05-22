"""LoCoMo dataset adapter.

Downloads and caches the LoCoMo (Long Conversation Modeling) dataset from
HuggingFace, then yields BenchmarkItem namedtuples for each QA pair.

Dataset: snap-research/locomo
License: Public research use (see https://snap-research.github.io/locomo/)
Size:    ~50 multi-session dialogues, ~600 turns/dialogue, ~16K tokens each

LoCoMo is grounded question-answering over long multi-session dialogues.
Each question is paired with a specific conversation segment as evidence.
Only text turns are used (vision/image turns are skipped — Popoto does not
support multimodal content).

Usage:
    from tests.benchmarks.datasets.locomo import iter_items

    for item in iter_items():
        print(item.item_id, item.query, item.relevant_ids)

    # From fixture:
    from pathlib import Path
    for item in iter_items(fixture_path=Path("fixtures/locomo_sample.json")):
        ...

Caching:
    Downloaded dataset is cached at ~/.cache/popoto_benchmarks/locomo.json.
"""

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from . import BenchmarkItem

logger = logging.getLogger("POPOTO.Benchmark.LoCoMo")

CACHE_DIR = Path.home() / ".cache" / "popoto_benchmarks"
CACHED_FILE = CACHE_DIR / "locomo.json"

HF_REPO_ID = "snap-research/locomo"
HF_FILENAME = "locomo10.json"


def _download_dataset() -> Path:
    """Download LoCoMo from HuggingFace and cache it locally.

    Returns:
        Path to the cached file.

    Raises:
        RuntimeError: If download fails.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHED_FILE.exists():
        logger.info("LoCoMo: using cached file at %s", CACHED_FILE)
        return CACHED_FILE

    logger.info("LoCoMo: downloading from HuggingFace (%s)...", HF_REPO_ID)

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise RuntimeError(
            "huggingface_hub is required to download LoCoMo. "
            "Install it with: pip install -e '.[benchmark]'\n"
            "Or manually place locomo.json at: "
            f"{CACHED_FILE}"
        )

    # Try a few known filenames for the LoCoMo dataset
    filenames_to_try = [HF_FILENAME, "locomo.json", "locomo_full.json"]
    last_exc = None
    for filename in filenames_to_try:
        try:
            downloaded = hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=filename,
                repo_type="dataset",
                local_dir=str(CACHE_DIR),
            )
            # Symlink / copy to the canonical cached path if needed
            downloaded_path = Path(downloaded)
            if downloaded_path != CACHED_FILE:
                import shutil
                shutil.copy2(downloaded_path, CACHED_FILE)
            logger.info("LoCoMo: downloaded to %s", CACHED_FILE)
            return CACHED_FILE
        except Exception as exc:
            last_exc = exc
            logger.debug("LoCoMo: tried '%s', got: %s", filename, exc)

    raise RuntimeError(
        f"Failed to download LoCoMo: {last_exc}\n"
        "Manual download: place the LoCoMo JSON file at "
        f"{CACHED_FILE}\n"
        "Source: https://snap-research.github.io/locomo/"
    ) from last_exc


def _parse_dialogue(dialogue: dict, dialogue_idx: int) -> list[BenchmarkItem]:
    """Parse one LoCoMo dialogue into a list of BenchmarkItems (one per QA pair).

    LoCoMo format (locomo10.json / locomo.json):
    [
        {
            "dialogue_id": "...",
            "conversation": [
                {
                    "session": 1,
                    "turn_id": "...",   # may be int or str
                    "role": "speaker1"|"speaker2",
                    "text": "...",      # main text field
                    "blip_caption": ... # image caption (skip if no text)
                },
                ...
            ],
            "qa": [
                {
                    "question": "...",
                    "answer": "...",
                    "evidence_turn_id": "...",   # may be int, str, or list
                    "type": "...",               # question type
                },
                ...
            ]
        },
        ...
    ]

    Args:
        dialogue: Raw dialogue dict.
        dialogue_idx: Index for error messages.

    Returns:
        List of BenchmarkItem (one per QA pair in the dialogue).

    Raises:
        ValueError: If required fields are missing.
    """
    required = ["conversation", "qa"]
    for field in required:
        if field not in dialogue:
            raise ValueError(
                f"LoCoMo dialogue[{dialogue_idx}] missing required field: {field!r}"
            )

    dialogue_id = dialogue.get("dialogue_id", str(dialogue_idx))
    raw_conversation = dialogue["conversation"]
    raw_qa = dialogue["qa"]

    if not isinstance(raw_conversation, list):
        raise ValueError(
            f"LoCoMo dialogue[{dialogue_idx}] 'conversation' must be a list"
        )
    if not isinstance(raw_qa, list):
        raise ValueError(
            f"LoCoMo dialogue[{dialogue_idx}] 'qa' must be a list"
        )

    # Build history: only text turns (skip image-only turns)
    history = []
    turn_id_set: set[str] = set()
    for turn in raw_conversation:
        text = turn.get("text", "") or turn.get("content", "")
        if not text or not text.strip():
            # Skip image-only or empty turns
            continue
        raw_turn_id = turn.get("turn_id", turn.get("id", ""))
        turn_id = str(raw_turn_id) if raw_turn_id != "" else str(len(history))
        session = turn.get("session", 1)
        role = turn.get("role", "user")
        # Normalize role to standard names
        if role in ("speaker1", "user", "human"):
            role = "user"
        elif role in ("speaker2", "assistant", "bot"):
            role = "assistant"
        history.append(
            {
                "role": role,
                "content": text.strip(),
                "turn_id": turn_id,
                "session_id": str(session),
            }
        )
        turn_id_set.add(turn_id)

    items = []
    for qa_idx, qa in enumerate(raw_qa):
        if "question" not in qa:
            logger.warning(
                "LoCoMo dialogue[%d] qa[%d] missing 'question', skipping",
                dialogue_idx,
                qa_idx,
            )
            continue

        question = qa["question"]
        item_id = f"{dialogue_id}::qa{qa_idx}"

        # Parse evidence_turn_id (may be int, str, or list)
        raw_evidence = qa.get("evidence_turn_id", qa.get("evidence", ""))
        if isinstance(raw_evidence, list):
            relevant_ids = {str(e) for e in raw_evidence if e != ""}
        elif raw_evidence != "" and raw_evidence is not None:
            relevant_ids = {str(raw_evidence)}
        else:
            relevant_ids = set()

        items.append(
            BenchmarkItem(
                item_id=item_id,
                history=history,
                query=question,
                relevant_ids=relevant_ids,
                metadata={
                    "answer": qa.get("answer", ""),
                    "question_type": qa.get("type", ""),
                    "dialogue_id": dialogue_id,
                    "dataset": "locomo",
                    "note": "text-only (image turns skipped)",
                },
            )
        )

    return items


def iter_items(
    fixture_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Iterator[BenchmarkItem]:
    """Iterate over LoCoMo benchmark items (one per QA pair).

    Args:
        fixture_path: If set, load from this JSON file instead of downloading.
            Used for unit tests (fixture-based).
        limit: If set, yield at most this many items.

    Yields:
        BenchmarkItem namedtuples.

    Raises:
        RuntimeError: If download fails (when fixture_path is None).
        ValueError: If the JSON file is malformed.
    """
    if fixture_path is not None:
        source = fixture_path
        logger.info("LoCoMo: loading from fixture %s", source)
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
    for dialogue_idx, dialogue in enumerate(data):
        if limit is not None and count >= limit:
            break
        try:
            items = _parse_dialogue(dialogue, dialogue_idx)
        except ValueError as exc:
            logger.warning("Skipping malformed dialogue[%d]: %s", dialogue_idx, exc)
            continue
        for item in items:
            if limit is not None and count >= limit:
                break
            yield item
            count += 1

    logger.info("LoCoMo: yielded %d items", count)
