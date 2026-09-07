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
import time
from pathlib import Path
from typing import List, Optional

from . import BenchmarkItem
from .sampling import sample_items

logger = logging.getLogger("POPOTO.Benchmark.LongMemEvalS")

#: strptime format of ``haystack_dates`` entries, e.g. "2023/05/20 (Sat) 02:21".
#: Minute precision — two sessions in one haystack can share a timestamp; see
#: docs/plans/sdlc-692.md, Race 1 "Tied dates".
HAYSTACK_DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"


#: Window of years this parser will accept. ``haystack_dates`` are ordinary
#: conversation timestamps, so anything outside this range is corrupt input,
#: not a date to key bitemporal ordering off. The bound is explicit because
#: ``time.mktime``'s own tolerance is PLATFORM-DEPENDENT (#692 review round 2):
#: for year 1000, macOS raises OverflowError while glibc happily returns
#: -30610224000.0, so a parser that relies on mktime to reject out-of-range
#: years has different behavior on a developer laptop and in CI. Checking the
#: year ourselves makes "out of range -> None" true on both.
MIN_SESSION_YEAR = 1900
MAX_SESSION_YEAR = 2200


def _parse_session_date(raw: Optional[str]) -> Optional[float]:
    """Parse one ``haystack_dates`` entry to an epoch float, or ``None``.

    Never raises and never substitutes a timestamp (#692) — a missing,
    unparseable, or out-of-range date must disable the supersession producer
    for that turn rather than inventing a value it would then treat as ground
    truth. A year outside ``[MIN_SESSION_YEAR, MAX_SESSION_YEAR]`` is treated
    as unparseable on every platform, independently of whether the local
    ``time.mktime`` would accept it.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = time.strptime(raw, HAYSTACK_DATE_FORMAT)
        if not MIN_SESSION_YEAR <= parsed.tm_year <= MAX_SESSION_YEAR:
            logger.debug("Out-of-range haystack_dates entry: %r", raw)
            return None
        # time.strptime also accepts in-window values that mktime can still
        # reject on some platforms; OverflowError is "unparseable" under this
        # function's contract just as ValueError is (#692 review).
        return time.mktime(parsed)
    except (ValueError, OverflowError):
        logger.debug("Unparseable haystack_dates entry: %r", raw)
        return None


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

    LongMemEval-S format (longmemeval_s_cleaned.json), real schema as
    published by xiaowu0162/longmemeval-cleaned:
    {
        "question_id": "...",
        "question_type": "...",
        "question": "...",
        "question_date": "...",
        "answer": "...",
        "answer_session_ids": ["session_id", ...],   # ground truth
        "haystack_dates": ["...", ...],               # parallel to sessions;
                                                        # parsed (#692) into a
                                                        # per-turn epoch float
                                                        # "session_date"
        "haystack_session_ids": ["session_id", ...],  # parallel to sessions
        "haystack_sessions": [                         # parallel to ids
            [ {"role": "user"|"assistant", "content": "..."}, ... ],
            ...
        ]
    }

    ``haystack_sessions`` and ``haystack_session_ids`` are parallel arrays:
    session ``haystack_sessions[i]`` has id ``haystack_session_ids[i]``.
    Ground truth ``answer_session_ids`` are a subset of
    ``haystack_session_ids`` (verified: all 500 questions match).

    Args:
        record: Raw dict from the JSON file.
        idx: Record index for error messages.

    Returns:
        BenchmarkItem.

    Raises:
        ValueError: If required fields are missing.
    """
    required = ["question", "haystack_sessions", "haystack_session_ids"]
    for field in required:
        if field not in record:
            raise ValueError(
                f"LongMemEval-S record[{idx}] missing required field: {field!r}"
            )

    question_id = record.get("question_id", str(idx))
    query = record["question"]
    sessions = record["haystack_sessions"]
    session_ids = record["haystack_session_ids"]
    raw_dates = record.get("haystack_dates", [])
    if not isinstance(raw_dates, list):
        raw_dates = []

    if not isinstance(sessions, list):
        raise ValueError(
            f"LongMemEval-S record[{idx}] 'haystack_sessions' must be a list, "
            f"got {type(sessions).__name__}"
        )
    if not isinstance(session_ids, list) or len(session_ids) != len(sessions):
        raise ValueError(
            f"LongMemEval-S record[{idx}] 'haystack_session_ids' "
            f"({len(session_ids) if isinstance(session_ids, list) else 'n/a'}) "
            f"must be a list parallel to 'haystack_sessions' ({len(sessions)})"
        )

    # Flatten parallel session arrays into a list of turns with session_id
    # metadata. Each session is itself a list of {role, content} turn dicts.
    history = []
    for session_idx, (session_id, turns) in enumerate(zip(session_ids, sessions)):
        # Missing / short haystack_dates -> None for every turn of that
        # session, never a substituted timestamp (#692).
        session_date = (
            _parse_session_date(raw_dates[session_idx])
            if session_idx < len(raw_dates)
            else None
        )
        for turn_idx, turn in enumerate(turns):
            history.append(
                {
                    "role": turn.get("role", "user"),
                    "content": turn.get("content", ""),
                    "turn_id": f"{session_id}::{turn_idx}",
                    "session_id": session_id,
                    "session_date": session_date,
                }
            )

    # Ground truth: answer_session_ids identify the relevant session(s)
    raw_evidence = record.get("answer_session_ids", [])
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
            # Ground truth is annotated per session (answer_session_ids), so
            # the harness ranks sessions (issue #514).
            "ground_truth_unit": "session",
        },
    )


def iter_items(
    fixture_path: Optional[Path] = None,
    limit: Optional[int] = None,
    sample: str = "head",
    seed: int = 0,
) -> List[BenchmarkItem]:
    """Load LongMemEval-S benchmark items.

    The full corpus is parsed into a list and then the representative
    sampler (:func:`~tests.benchmarks.datasets.sampling.sample_items`) selects
    the subset for a limited run, so a small ``--limit`` spans the whole
    category-grouped file rather than only the easiest prefix.

    Args:
        fixture_path: If set, load from this JSON file instead of
            downloading. Used for unit tests (fixture-based).
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

    parsed: List[BenchmarkItem] = []
    for idx, record in enumerate(data):
        try:
            item = _parse_record(record, idx)
        except ValueError as exc:
            logger.warning("Skipping malformed record[%d]: %s", idx, exc)
            continue
        parsed.append(item)

    result = sample_items(parsed, limit, mode=sample, seed=seed)
    logger.info(
        "LongMemEval-S: yielded %d items (sample=%s seed=%s)",
        len(result),
        sample,
        seed,
    )
    return result
