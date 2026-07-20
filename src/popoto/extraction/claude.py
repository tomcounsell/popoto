"""
Claude (Anthropic) memory-extraction provider.

Wraps the Anthropic Messages API behind the AbstractExtractionProvider
interface. Requires the anthropic package (pip install popoto[anthropic]).

Opt-in only: ``import popoto`` and ``import popoto.extraction`` never
import ``anthropic`` -- this module is imported lazily by callers who
want LLM-based extraction, mirroring popoto.embeddings.openai's pattern.

Example:
    from popoto.extraction.claude import ClaudeExtractionProvider
    provider = ClaudeExtractionProvider(api_key="your-key")
    facts = provider.extract("Alice met Bob at the conference in Paris.")
"""

import json
import logging
from typing import List, Optional

from . import AbstractExtractionProvider, ExtractedFact
from ..fields.constants import Defaults

try:
    import anthropic as anthropic_module

    _anthropic_available = True
except ImportError:
    # Keep the name bound (to None) even when the optional dependency is
    # absent, so callers/tests can always `monkeypatch.setattr(claude_mod,
    # "anthropic_module", ...)` regardless of whether `anthropic` is
    # installed in the current environment.
    anthropic_module = None
    _anthropic_available = False

logger = logging.getLogger("POPOTO.extraction")

# ---------------------------------------------------------------------------
# Pinned, non-user-configurable constants.
#
# Per this project's convention (see popoto.fields.constants.Defaults'
# module docstring and CLAUDE.md's "numeric constants are magic numbers for
# experimental tuning, not dev/user config"), the model and prompt used for
# extraction are pinned in-repo, not exposed as constructor kwargs.
# ---------------------------------------------------------------------------

EXTRACTION_MODEL = "claude-opus-4-8"
"""Pinned model for Claude-based extraction. Not user-configurable."""

EXTRACTION_PROMPT = """You are a memory-extraction engine for an AI agent. Given a \
block of text (typically an LLM's response during a conversation), extract the \
discrete, independently-useful facts it contains.

For each fact:
- "text": a short, self-contained statement of the fact, written so it makes sense \
on its own without the surrounding conversation.
- "entities": the proper nouns / named entities mentioned in the fact (people, \
places, organizations, products, dates treated as named references, etc.). Use an \
empty list if none are present.
- "importance": a score from 0.0 to 1.0 for how important this fact is likely to be \
to remember later (0.0 = trivial/filler, 1.0 = critical, load-bearing information).
- "confidence": a score from 0.0 to 1.0 for how certain the source text is asserting \
this fact (0.0 = highly speculative or hedged, 1.0 = stated as definite fact).

Only extract facts that carry real informational content -- skip greetings, filler, \
and purely conversational scaffolding. If the text contains no extractable facts, \
return an empty "facts" array."""
"""Pinned system prompt for Claude-based extraction. Not user-configurable."""

FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "importance": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["text", "entities", "importance", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}
"""JSON schema for structured extraction output. Not user-configurable."""


def _clamp01(value, default: float) -> float:
    """Clamp a value into [0.0, 1.0], falling back to ``default`` if invalid."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN check without importing math
        return default
    return max(0.0, min(1.0, f))


class ClaudeExtractionProvider(AbstractExtractionProvider):
    """Claude (Anthropic) memory-extraction provider.

    Opt-in provider that calls the Anthropic Messages API once per
    ``extract()`` call, using structured JSON-schema output
    (``output_config.format``) to get typed facts back directly -- no
    tool_use, no assistant-turn prefill (which 400s on this model
    family).

    Model and prompt are pinned module constants (``EXTRACTION_MODEL``,
    ``EXTRACTION_PROMPT``), not constructor kwargs, per this project's
    experimental-constant convention.

    On any API or parse failure, ``extract()`` logs a warning and returns
    an empty list -- it never raises, so a flaky extraction call never
    crashes the caller's turn loop.

    Args:
        api_key: Anthropic API key. If None, reads from the
            ``ANTHROPIC_API_KEY`` env var (via the anthropic SDK's normal
            resolution).

    Raises:
        ImportError: If the ``anthropic`` package is not installed.
    """

    def __init__(self, api_key: Optional[str] = None):
        if not _anthropic_available:
            raise ImportError(
                "anthropic is required to use ClaudeExtractionProvider. "
                "Install it with: pip install popoto[anthropic]"
            )
        self._client = anthropic_module.Anthropic(api_key=api_key)

    def extract(self, text: str) -> List[ExtractedFact]:
        """Extract facts from text via one Claude API call.

        Args:
            text: Input text to extract facts from.

        Returns:
            List of ExtractedFact. Empty list if the text is empty/blank,
            no facts were found, or the API call/parse failed (a warning
            is logged in the failure case).
        """
        if not text or not text.strip():
            return []

        try:
            response = self._client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=4096,
                system=EXTRACTION_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_config={
                    "format": {"type": "json_schema", "schema": FACTS_SCHEMA}
                },
            )
            raw_text = next(
                (block.text for block in response.content if block.type == "text"),
                None,
            )
            if raw_text is None:
                logger.warning("ClaudeExtractionProvider: no text block in response")
                return []
            parsed = json.loads(raw_text)
        except Exception as e:
            logger.warning("ClaudeExtractionProvider: extraction failed: %s", e)
            return []

        raw_facts = parsed.get("facts") if isinstance(parsed, dict) else None
        if not isinstance(raw_facts, list):
            logger.warning("ClaudeExtractionProvider: response missing 'facts' list")
            return []

        facts = []
        for raw_fact in raw_facts:
            if not isinstance(raw_fact, dict):
                continue
            fact_text = raw_fact.get("text")
            if not isinstance(fact_text, str) or not fact_text.strip():
                continue
            entities = raw_fact.get("entities")
            if not isinstance(entities, list):
                entities = []
            entities = [e for e in entities if isinstance(e, str)]
            importance = _clamp01(
                raw_fact.get("importance"), Defaults.EXTRACTION_DEFAULT_IMPORTANCE
            )
            confidence = _clamp01(
                raw_fact.get("confidence"), Defaults.EXTRACTION_DEFAULT_CONFIDENCE
            )
            facts.append(
                ExtractedFact(
                    text=fact_text.strip(),
                    entities=entities,
                    importance=importance,
                    confidence=confidence,
                )
            )
        return facts
