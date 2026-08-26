"""
Memory-extraction providers for Popoto's SubconsciousMemory recipe.

This module provides the AbstractExtractionProvider interface, the
ExtractedFact dataclass, and the built-in zero-dependency heuristic
provider used to turn raw LLM turn text into typed memory facts.

Providers are pluggable -- pass a provider instance to
SubconsciousMemory(extraction_provider=...). The default
HeuristicExtractionProvider preserves the historical sentence-splitting
behavior exactly and requires no external dependencies.

Available providers:
    - HeuristicExtractionProvider: zero-dependency sentence-split heuristic
      (the recipe default; stdlib-only, eagerly imported here)
    - RawTurnExtractionProvider: zero-dependency verbatim pass-through, one
      fact per turn. The default on the harness path
      (``popoto.integrations``) because issue #489 measured it ahead of the
      heuristic on judged accuracy.
    - ClaudeExtractionProvider (popoto.extraction.claude): opt-in LLM-based
      extraction via the Anthropic API (requires the ``anthropic`` package;
      imported lazily -- see popoto.extraction.claude, not re-exported here)
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_MIN_LENGTH = 10
"""Minimum sentence length (chars) to be considered a fact worth saving.

Matches ``DEFAULT_EXTRACTION_MIN_LENGTH`` in
``popoto.recipes.subconscious_memory`` -- the historical default preserved
by ``HeuristicExtractionProvider``.
"""


@dataclass
class ExtractedFact:
    """A single fact extracted from LLM turn text.

    Attributes:
        text: The fact's text content -- becomes the saved Memory record's
            content.
        entities: Proper nouns / named entities mentioned in the fact.
            Empty list means no entities were identified (or the provider
            doesn't extract entities, e.g. the heuristic provider).
        importance: Provider's opinion on this fact's importance, in
            [0.0, 1.0]. ``None`` means "no opinion; use the caller-supplied
            default importance instead" (importance-on-write fallback).
        confidence: Provider's opinion on how certain this fact is, in
            [0.0, 1.0]. ``None`` means "no opinion; leave the model's
            ConfidenceField at its default initial value".
        span_start: Start offset of the source span in the turn text.
        span_end: End offset of the source span in the turn text.
        turn_id: The turn this fact was extracted from.
        candidate_id: Identity of the candidate this fact was assembled
            from, matching the ``cand:`` subject tag on its journal entry.
        generator_rule: Which deterministic rule produced the candidate.

    The last five are populated only by the auditable extraction path
    (#562) and default to ``None`` everywhere else, so existing provider
    outputs and existing callers are unaffected.
    """

    text: str
    entities: List[str] = field(default_factory=list)
    importance: Optional[float] = None
    confidence: Optional[float] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    turn_id: Optional[str] = None
    candidate_id: Optional[str] = None
    generator_rule: Optional[str] = None


class AbstractExtractionProvider(ABC):
    """Abstract interface for memory-extraction providers.

    Implementations turn raw LLM response text into a list of
    ``ExtractedFact`` records. ``SubconsciousMemory.extract_memories()``
    delegates to a configured provider's ``extract()`` method.
    """

    @abstractmethod
    def extract(self, text: str) -> List[ExtractedFact]:
        """Extract facts from the given text.

        Args:
            text: Raw text to extract facts from (typically an LLM's
                response text for one turn).

        Returns:
            List of ExtractedFact records. Empty list if no facts were
            found, or on any recoverable failure -- implementations should
            not raise for ordinary extraction failures (see
            ClaudeExtractionProvider for the fail-open contract).
        """
        ...


class HeuristicExtractionProvider(AbstractExtractionProvider):
    """Zero-dependency sentence-splitting extraction provider.

    Wraps the historical sentence-split + min-length-filter behavior of
    ``SubconsciousMemory.extract_memories()`` exactly: splits on
    sentence-ending punctuation (.!?) followed by whitespace or
    end-of-string, strips each sentence, and drops any shorter than
    ``min_length``. Every surviving sentence becomes an
    ``ExtractedFact`` with no entities and no importance/confidence
    opinion (both ``None``), so downstream wiring falls back to the
    caller-supplied importance and leaves confidence untouched --
    byte-identical behavior to the pre-extraction-package heuristic.

    This is the default provider: no API key, no network call, no
    optional dependency.

    Args:
        min_length: Minimum sentence length (chars) to keep. Default
            ``DEFAULT_MIN_LENGTH`` (10).
    """

    def __init__(self, min_length: int = DEFAULT_MIN_LENGTH):
        self._min_length = min_length

    def extract(self, text: str) -> List[ExtractedFact]:
        """Split text into sentences and filter by minimum length.

        Args:
            text: Input text to extract facts from.

        Returns:
            List of ExtractedFact, one per surviving sentence, each with
            empty entities and ``importance=None``, ``confidence=None``.
        """
        if not text or not text.strip():
            return []

        sentences = self._split_sentences(text)
        facts = []
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < self._min_length:
                continue
            facts.append(ExtractedFact(text=sentence))
        return facts

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Split text into sentences using a simple regex heuristic.

        Splits on sentence-ending punctuation (.!?) followed by whitespace
        or end-of-string. Preserves abbreviations like "e.g." and "Dr."
        reasonably well for typical LLM output.

        Args:
            text: Input text to split.

        Returns:
            List of sentence strings.
        """
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in parts if p]


class RawTurnExtractionProvider(AbstractExtractionProvider):
    """Verbatim pass-through: one turn in, one ``ExtractedFact`` out.

    No sentence splitting, no length filter, no rewriting. The turn text is
    stripped of surrounding whitespace and returned as a single fact, so the
    saved record is the turn itself.

    Why this exists, and why it is the harness default (issue #489):
    PR #510 evaluated LLM extraction, heuristic sentence-splitting, and raw
    turn ingestion against the same slice. Every extraction arm lost to raw
    ingestion on judged accuracy -- heuristic scored **0.2078** against raw's
    **0.3636**. Sentence-splitting an assistant turn discards the context
    that makes each sentence answerable, and it multiplies one turn into
    many low-value records.

    ``popoto.integrations`` (the agent-harness hook and MCP path) writes
    through this provider by default, because a hook fires on every turn and
    would otherwise generate more memories through the measured-worst path
    than every other Popoto usage combined. Set
    ``POPOTO_MEMORY_INGEST=heuristic`` to opt into the split behavior.

    This provider states no importance or confidence opinion (both ``None``),
    so ``SubconsciousMemory.extract_memories()`` falls back to the
    caller-supplied importance and leaves ``ConfidenceField`` at its prior.

    Args:
        max_chars: Optional cap on the fact's length. ``None`` (default)
            stores the turn in full. When set, the text is truncated to
            ``max_chars`` characters -- use it only when turns are large
            enough to threaten Redis value limits, since truncation is the
            same information loss this provider exists to avoid.
    """

    def __init__(self, max_chars: Optional[int] = None):
        self._max_chars = max_chars

    def extract(self, text: str) -> List[ExtractedFact]:
        """Return the turn verbatim as a single fact.

        Args:
            text: Raw turn text.

        Returns:
            A one-element list holding the stripped text, or an empty list
            when the text is empty or whitespace-only.
        """
        if not text or not text.strip():
            return []

        content = text.strip()
        if self._max_chars is not None and len(content) > self._max_chars:
            content = content[: self._max_chars]
        return [ExtractedFact(text=content)]


__all__ = [
    "ExtractedFact",
    "AbstractExtractionProvider",
    "HeuristicExtractionProvider",
    "RawTurnExtractionProvider",
    # Auditable extraction (#562). Imported lazily by name below so that
    # `import popoto.extraction` stays free of the optional anthropic
    # probe in verdict.py.
    "Candidate",
    "generate_candidates",
    "Verdict",
    "ReasonCode",
    "VerdictResult",
    "llm_verdict",
    "DecisionRecord",
    "DecisionLog",
    "Metrics",
    "AuditableExtractionConfig",
]


def __getattr__(name):
    """Lazily re-export the auditable-extraction surface (PEP 562).

    Kept lazy rather than imported at module scope for one reason:
    ``verdict.py`` probes for the optional ``anthropic`` package at import
    time, and this module's contract (see the module docstring) is that
    ``import popoto.extraction`` never reaches for an optional dependency.
    """
    if name in ("Candidate", "generate_candidates"):
        from . import candidates

        return getattr(candidates, name)
    if name in ("Verdict", "ReasonCode", "VerdictResult", "llm_verdict"):
        from . import verdict

        return getattr(verdict, name)
    if name in (
        "DecisionRecord",
        "DecisionLog",
        "Metrics",
        "AuditableExtractionConfig",
    ):
        from . import decision_log

        return getattr(decision_log, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
