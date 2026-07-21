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
      (the default; stdlib-only, eagerly imported here)
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
    """

    text: str
    entities: List[str] = field(default_factory=list)
    importance: Optional[float] = None
    confidence: Optional[float] = None


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


__all__ = [
    "ExtractedFact",
    "AbstractExtractionProvider",
    "HeuristicExtractionProvider",
]
