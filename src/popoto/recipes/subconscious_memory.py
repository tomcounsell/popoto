"""SubconsciousMemory -- Automatic memory injection and extraction around LLM turns.

Wraps an existing chat flow with:
- Pre-turn: assemble relevant memories, inject as system context
- Post-turn: extract facts/observations from LLM response, save as Memory
- Outcome: report how injected memories were used

Architecture::

    User message
        |
        v
    [Pre-turn hook: ContextAssembler.assemble() -> inject into messages]
        |
        v
    [LLM inference]
        |
        v
    [Post-turn hook: extract observations from response -> save as Memory records]
        |
        v
    [Outcome hook: report acted/dismissed/contradicted via ObservationProtocol]
        |
        v
    Agent response

The recipe is framework-agnostic -- it works with plain ``list[dict]``
messages. The guide shows how to wire it into PydanticAI or the OpenAI SDK,
but the recipe itself has no framework dependencies.

Dependencies:
    ContextAssembler (from popoto.recipes)
    ObservationProtocol (from popoto.fields.observation)
    A Popoto Model class with at least Level 1 fields (DecayingSortedField)

Example:
    from popoto.recipes.subconscious_memory import SubconsciousMemory

    sm = SubconsciousMemory(
        model_class=Memory,
        agent_id="agent-1",
        score_weights={"relevance": 0.6, "confidence": 0.3},
    )

    # Pre-turn: inject context
    messages, assembly_result = sm.inject_context(messages)

    # ... call LLM with messages ...

    # Post-turn: extract and save memories
    new_memories = sm.extract_memories(response_text, importance=0.6)

    # Outcome: report usage
    sm.report_outcomes(assembly_result)
"""

import logging
import re

from .context_assembler import AssemblyResult, ContextAssembler
from ..fields.observation import ObservationProtocol

logger = logging.getLogger("POPOTO.SubconsciousMemory")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_EXTRACTION_MIN_LENGTH = 10
"""Minimum sentence length (chars) to be considered a fact worth saving."""

DEFAULT_SYSTEM_PREAMBLE = "You are a helpful assistant."
"""Default system message preamble when no system message exists."""


# ---------------------------------------------------------------------------
# SubconsciousMemory
# ---------------------------------------------------------------------------


class SubconsciousMemory:
    """Automatic memory injection and extraction around LLM turns.

    Wraps an existing chat flow with:
    - Pre-turn: assemble relevant memories, inject as system context
    - Post-turn: extract facts/observations from LLM response, save as Memory
    - Outcome: report how injected memories were used

    Args:
        model_class: Popoto Model class (any level from the quickstart guide).
        agent_id: Identifier for the agent whose memories to query/save.
        score_weights: Dict mapping field names to weights for ContextAssembler
            (e.g., {"relevance": 0.6, "confidence": 0.3}).
        max_items: Maximum memory records to inject per turn. Default 10.
        max_tokens: Soft token budget for injected context. Default 4000.
        extraction_min_length: Minimum characters for a sentence to be
            extracted as a memory. Default 10.
        system_preamble: System message prefix used when injecting context.
            Default "You are a helpful assistant."
        content_field: Name of the field on model_class that stores the
            text content. Default "content".
        importance_field: Name of the field on model_class that stores
            importance score. Default "importance".
        agent_id_field: Name of the KeyField for agent partitioning.
            Default "agent_id".
    """

    def __init__(
        self,
        model_class,
        agent_id,
        score_weights,
        max_items=10,
        max_tokens=4000,
        extraction_min_length=DEFAULT_EXTRACTION_MIN_LENGTH,
        system_preamble=DEFAULT_SYSTEM_PREAMBLE,
        content_field="content",
        importance_field="importance",
        agent_id_field="agent_id",
    ):
        self.model_class = model_class
        self.agent_id = agent_id
        self.score_weights = score_weights
        self.max_items = max_items
        self.max_tokens = max_tokens
        self.extraction_min_length = extraction_min_length
        self.system_preamble = system_preamble
        self.content_field = content_field
        self.importance_field = importance_field
        self.agent_id_field = agent_id_field

        self._assembler = ContextAssembler(
            model_class=model_class,
            score_weights=score_weights,
            max_items=max_items,
            max_tokens=max_tokens,
        )

    def inject_context(self, messages):
        """Pre-turn: assemble memories and inject into the messages array.

        Finds or creates a system message at index 0 and appends assembled
        memory context to it. Returns the modified messages list and the
        AssemblyResult for later outcome reporting.

        If no memories are found, the messages are returned unchanged.

        Args:
            messages: List of message dicts with "role" and "content" keys.

        Returns:
            Tuple of (modified_messages, AssemblyResult). The messages list
            is modified in-place for convenience but also returned.
        """
        if not messages:
            return messages, AssemblyResult()

        # Extract user query cues from the last user message
        query_cues = {}
        for msg in reversed(messages):
            if msg.get("role") == "user" and msg.get("content"):
                query_cues["topic"] = msg["content"]
                break

        try:
            result = self._assembler.assemble(
                query_cues=query_cues if query_cues else None,
                agent_id=self.agent_id,
            )
        except Exception as e:
            logger.warning("Context assembly failed: %s", e)
            return messages, AssemblyResult()

        if not result.records:
            return messages, result

        # Inject into system message
        context_block = f"\n\nRelevant context:\n{result.formatted}"

        if messages and messages[0].get("role") == "system":
            messages[0] = dict(messages[0])
            messages[0]["content"] = messages[0].get("content", "") + context_block
        else:
            system_msg = {
                "role": "system",
                "content": self.system_preamble + context_block,
            }
            messages = [system_msg] + list(messages)

        return messages, result

    def extract_memories(self, response_text, importance=0.5):
        """Post-turn: extract facts from LLM response and save as Memory records.

        Uses a simple heuristic: split response into sentences, filter by
        minimum length, and save each as a separate Memory record.

        For LLM-based extraction (more accurate but requires an API call),
        override this method or pass extracted facts directly to your
        model_class.save().

        Args:
            response_text: The LLM's response text.
            importance: Default importance score for extracted memories.
                Float between 0.0 and 1.0. Default 0.5.

        Returns:
            List of saved model instances. Empty list if response_text
            is empty or contains no extractable facts.
        """
        if not response_text or not response_text.strip():
            return []

        sentences = self._split_sentences(response_text)
        saved = []

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < self.extraction_min_length:
                continue

            try:
                kwargs = {
                    self.agent_id_field: self.agent_id,
                    self.content_field: sentence,
                    self.importance_field: importance,
                }
                instance = self.model_class(**kwargs)
                result = instance.save()
                if result is not False:
                    saved.append(instance)
            except Exception as e:
                logger.warning("Failed to save extracted memory: %s", e)

        return saved

    def report_outcomes(self, assembly_result, outcome="acted"):
        """Outcome hook: report how injected memories were used.

        Calls ObservationProtocol.on_context_used() for all records in the
        assembly result with the specified outcome.

        Args:
            assembly_result: AssemblyResult from inject_context().
            outcome: How the agent used the memories. One of "acted",
                "dismissed", "contradicted", "deferred". Default "acted".
        """
        if not assembly_result or not assembly_result.records:
            return

        try:
            outcome_map = {}
            for record in assembly_result.records:
                try:
                    key = record.db_key.redis_key
                    outcome_map[key] = outcome
                except Exception:
                    continue

            if outcome_map:
                ObservationProtocol.on_context_used(
                    assembly_result.records, outcome_map
                )
        except Exception as e:
            logger.warning("Failed to report outcomes: %s", e)

    @staticmethod
    def _split_sentences(text):
        """Split text into sentences using a simple regex heuristic.

        Splits on sentence-ending punctuation (.!?) followed by whitespace
        or end-of-string. Preserves abbreviations like "e.g." and "Dr."
        reasonably well for typical LLM output.

        Args:
            text: Input text to split.

        Returns:
            List of sentence strings.
        """
        # Split on .!? followed by space or end of string
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in parts if p]
