"""LoCoMo dataset adapter.

Downloads and caches the LoCoMo (Long Conversation Modeling) dataset from
HuggingFace, then yields BenchmarkItem namedtuples for each QA pair.

Dataset: snap-research/locomo
License: Public research use (see https://snap-research.github.io/locomo/)
Size:    10 multi-session dialogues (1986 QA pairs), ~600 turns/dialogue

LoCoMo is grounded question-answering over long multi-session dialogues.
Each question is paired with one or more conversation turns as evidence
(referenced by their ``dia_id``). Image turns are ingested via their
``blip_caption`` so that evidence pointing at an image turn stays reachable;
a turn is skipped only when it has neither ``text`` nor ``blip_caption``.

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
from typing import List, Optional

from . import BenchmarkItem
from .sampling import sample_items

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
    """Parse one LoCoMo sample into a list of BenchmarkItems (one per QA pair).

    Real snap-research/locomo schema (locomo10.json) — ``conversation`` is a
    DICT, not a list:
    [
        {
            "sample_id": "...",
            "conversation": {
                "speaker_a": "<name>",
                "speaker_b": "<name>",
                "session_1_date_time": "...",
                "session_1": [
                    {
                        "speaker": "<name>",       # speaker_a or speaker_b name
                        "dia_id": "D1:11",         # canonical per-turn id
                        "text": "...",             # main text (may be absent)
                        "img_url": [...],          # optional, image turns
                        "blip_caption": "..."      # optional, image caption
                    },
                    ...
                ],
                "session_2_date_time": "...",
                "session_2": [ ... ],
                ...
            },
            "qa": [
                {
                    "question": "...",
                    "answer": "...",               # adversarial uses
                    "adversarial_answer": "...",   #   adversarial_answer instead
                    "evidence": ["D1:11", ...],    # dia_ids (absent if adversarial)
                    "category": 1                  # int question type
                },
                ...
            ]
        },
        ...
    ]

    History rows use the turn's ``dia_id`` verbatim as ``turn_id`` so that
    ``qa[].evidence`` (also ``dia_id``s) can intersect them. Image turns are
    ingested via ``blip_caption``; a turn is skipped only when it has neither
    ``text`` nor ``blip_caption``.

    Args:
        dialogue: Raw sample dict.
        dialogue_idx: Index for error messages.

    Returns:
        List of BenchmarkItem (one per QA pair in the sample).

    Raises:
        ValueError: If required fields are missing or have the wrong shape.
    """
    required = ["conversation", "qa"]
    for field in required:
        if field not in dialogue:
            raise ValueError(
                f"LoCoMo dialogue[{dialogue_idx}] missing required field: {field!r}"
            )

    sample_id = dialogue.get("sample_id", str(dialogue_idx))
    conversation = dialogue["conversation"]
    raw_qa = dialogue["qa"]

    if not isinstance(conversation, dict):
        raise ValueError(
            f"LoCoMo dialogue[{dialogue_idx}] 'conversation' must be a dict"
        )
    if not isinstance(raw_qa, list):
        raise ValueError(f"LoCoMo dialogue[{dialogue_idx}] 'qa' must be a list")

    # Map the two speaker names to deterministic role slots.
    name_to_role = {}
    if conversation.get("speaker_a") is not None:
        name_to_role[conversation["speaker_a"]] = "user"
    if conversation.get("speaker_b") is not None:
        name_to_role[conversation["speaker_b"]] = "assistant"

    # Select session_N keys (skip *_date_time / speaker_a / speaker_b), in N order.
    session_keys = sorted(
        (
            k
            for k in conversation
            if k.startswith("session_") and not k.endswith("_date_time")
        ),
        key=lambda k: int(k.split("_")[1]),
    )

    # Build history keyed by dia_id; ingest blip_caption for image turns.
    history = []
    for session_key in session_keys:
        turns = conversation[session_key]
        if not isinstance(turns, list):
            continue
        for turn in turns:
            content = turn.get("text") or turn.get("blip_caption")
            if not content or not str(content).strip():
                # Skip turns with neither text nor blip_caption.
                continue
            speaker = turn.get("speaker", "")
            history.append(
                {
                    "role": name_to_role.get(speaker, "user"),
                    "content": str(content).strip(),
                    "turn_id": str(turn.get("dia_id", "")),
                    "session_id": session_key,
                    "speaker": speaker,
                }
            )

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
        item_id = f"{sample_id}::qa{qa_idx}"

        # Evidence is a list of dia_ids; adversarial QAs have no evidence.
        relevant_ids = {
            str(e) for e in qa.get("evidence", []) if e is not None and e != ""
        }
        is_adversarial = "evidence" not in qa
        answer = qa.get("answer")
        if answer is None:
            answer = qa.get("adversarial_answer", "")

        items.append(
            BenchmarkItem(
                item_id=item_id,
                history=history,
                query=question,
                relevant_ids=relevant_ids,
                metadata={
                    "answer": answer,
                    "question_type": qa.get("category"),
                    "sample_id": sample_id,
                    "dataset": "locomo",
                    # Ground truth is annotated per turn (dia_id), so the
                    # harness ranks turns (issue #514).
                    "ground_truth_unit": "turn",
                    "adversarial": is_adversarial,
                    "note": "image turns ingested via blip_caption",
                },
            )
        )

    return items


def iter_items(
    fixture_path: Optional[Path] = None,
    limit: Optional[int] = None,
    sample: str = "head",
    seed: int = 0,
) -> List[BenchmarkItem]:
    """Load LoCoMo benchmark items (one per QA pair).

    All dialogues are parsed into a single flat item list *before* sampling,
    so ``stride``/``stratified`` span the whole corpus rather than a prefix of
    dialogues. The representative sampler then selects the limited subset.

    Args:
        fixture_path: If set, load from this JSON file instead of downloading.
            Used for unit tests (fixture-based).
        limit: If set, return at most this many items (after sampling).
        sample: Sample mode — ``head``/``stride``/``shuffle``/``stratified``.
            Defaults to ``head`` so callers that pass neither argument keep
            byte-for-byte legacy behaviour; the CLI passes a resolved mode.
        seed: Seed for the local RNG used by ``shuffle``/``stratified``.

    Returns:
        List of BenchmarkItem namedtuples.

    Raises:
        RuntimeError: If download fails (when fixture_path is None).
        ValueError: If the JSON file is malformed or ``sample`` is unknown.
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

    parsed: List[BenchmarkItem] = []
    for dialogue_idx, dialogue in enumerate(data):
        try:
            items = _parse_dialogue(dialogue, dialogue_idx)
        except ValueError as exc:
            logger.warning("Skipping malformed dialogue[%d]: %s", dialogue_idx, exc)
            continue
        parsed.extend(items)

    result = sample_items(parsed, limit, mode=sample, seed=seed)
    logger.info(
        "LoCoMo: yielded %d items (sample=%s seed=%s)",
        len(result),
        sample,
        seed,
    )
    return result
