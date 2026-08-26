"""Deterministic candidate enumeration for auditable extraction (M3, #562).

A *candidate* is a span of one conversation turn that the rest of the
extraction pipeline decides on: the firewall scans it, the verdict stage
votes on it, and the decision log records exactly one terminal state for it.

This module is the pipeline's only source of candidates, and it is
deliberately the dullest stage:

* **Pure.** No Redis, no LLM, no network, no clock. ``generate_candidates``
  is a function of ``(turn_id, text)`` alone.
* **Deterministic.** The same input always yields the same list, in the same
  order, with the same ids. Auditability depends on this -- a
  non-deterministic candidate set makes a decision log unreplayable.
* **Exhaustive.** Nothing is filtered here. Short sentences, duplicate
  sentences and low-value entities are all emitted, because *dropping* a
  candidate is a decision that must be logged by the caller, not a silence
  produced here. There is deliberately no per-turn cap.

Two generator rules produce the v1 candidate set:

``sentence``
    One candidate per sentence span, using the same split regex as
    :class:`~popoto.extraction.HeuristicExtractionProvider`.
``entity``
    One candidate per pattern-lifted named entity. The lift is a regex, not
    a model call -- an LLM here would make the candidate set
    non-reproducible.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: Sentence-split heuristic, shared with ``HeuristicExtractionProvider``:
#: sentence-ending punctuation (.!?) followed by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: A run of capitalized tokens: "Alice", "New York Times", "OpenAI", "NASA".
#: Tokens may carry internal capitals/digits so acronyms and product names
#: ("Popoto", "S3", "GitHub") survive intact.
_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*)*\b")

SENTENCE_RULE = "sentence"
ENTITY_RULE = "entity"


@dataclass(frozen=True)
class Candidate:
    """One deterministically enumerated span of a turn.

    Attributes:
        text: The verbatim span. Byte-identical to
            ``turn_text[start:end]`` -- nothing is normalized, distilled or
            rewritten (that is M4's job).
        turn_id: Id of the turn this span came from.
        candidate_id: ``f"{turn_id}:{generator_rule}:{ordinal}"``. Unique
            within a turn, deterministic, and deliberately **low-entropy**:
            it is written to the provenance journal as a ``cand:`` subject
            tag, and the journal's write-time firewall blocks high-entropy
            tags such as hex digests. Never make this a hash.
        start: Character offset of the span's first character in the turn
            text.
        end: Character offset one past the span's last character.
        generator_rule: Which rule produced this candidate --
            ``"sentence"`` or ``"entity"``.
    """

    text: str
    turn_id: str
    candidate_id: str
    start: int
    end: int
    generator_rule: str


def generate_candidates(turn_id: str, text: Optional[str]) -> List[Candidate]:
    """Enumerate the complete v1 candidate set for one turn.

    Args:
        turn_id: Id of the turn being extracted. Becomes the first segment
            of every ``candidate_id``.
        text: The turn's raw text.

    Returns:
        Sentence candidates in document order, followed by entity
        candidates in document order. Empty list when ``text`` is empty,
        whitespace-only or not a string -- an empty turn produces *zero*
        candidates, and the caller (not this module) logs the
        ``reject(empty_turn)`` row. This module has no decision-log
        dependency.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    candidates = [
        _make(turn_id, text, span, SENTENCE_RULE, ordinal)
        for ordinal, span in enumerate(_sentence_spans(text))
    ]
    candidates += [
        _make(turn_id, text, span, ENTITY_RULE, ordinal)
        for ordinal, span in enumerate(_entity_spans(text))
    ]
    return candidates


def _make(
    turn_id: str,
    text: str,
    span: Tuple[int, int],
    generator_rule: str,
    ordinal: int,
) -> Candidate:
    """Build one candidate, minting its low-entropy composite id."""
    start, end = span
    return Candidate(
        text=text[start:end],
        turn_id=turn_id,
        candidate_id=f"{turn_id}:{generator_rule}:{ordinal}",
        start=start,
        end=end,
        generator_rule=generator_rule,
    )


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    """Offsets of each sentence, using the heuristic provider's regex.

    ``HeuristicExtractionProvider._split_sentences`` returns strings; the
    decision log needs offsets, so the same split is walked positionally
    here. Surrounding whitespace is trimmed off each span (matching the
    provider's ``strip()``) and empty spans are dropped.
    """
    spans = []
    cursor = 0
    for separator in _SENTENCE_SPLIT.finditer(text):
        spans.append((cursor, separator.start()))
        cursor = separator.end()
    spans.append((cursor, len(text)))

    trimmed = (_trim(text, start, end) for start, end in spans)
    return [(start, end) for start, end in trimmed if start < end]


def _entity_spans(text: str) -> List[Tuple[int, int]]:
    """Offsets of each pattern-lifted entity.

    A single capitalized word at the start of a sentence is orthography,
    not evidence of an entity, so single-token matches are lifted only when
    they do not open a sentence. Multi-token runs ("New York Times") are
    always lifted, wherever they sit.
    """
    sentence_starts = {start for start, _ in _sentence_spans(text)}
    spans = []
    for match in _ENTITY.finditer(text):
        start, end = match.span()
        is_multi_token = " " in match.group() or "\t" in match.group()
        if not is_multi_token and start in sentence_starts:
            continue
        spans.append((start, end))
    return spans


def _trim(text: str, start: int, end: int) -> Tuple[int, int]:
    """Shrink ``[start, end)`` past leading and trailing whitespace."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
