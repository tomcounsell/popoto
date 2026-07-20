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

import itertools
import logging

from .context_assembler import AssemblyResult, ContextAssembler
from ..extraction import HeuristicExtractionProvider
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
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
        extraction_provider: An ``AbstractExtractionProvider`` instance
            (see ``popoto.extraction``) used to turn LLM response text
            into ``ExtractedFact`` records in ``extract_memories()``.
            Default ``None``, which resolves to
            ``HeuristicExtractionProvider(min_length=extraction_min_length)``
            -- the historical sentence-splitting behavior, preserved
            byte-identical when no new kwargs are passed. Pass a
            ``ClaudeExtractionProvider`` (``popoto.extraction.claude``)
            for LLM-based extraction with entities/importance/confidence.
        confidence_field: Name of a ``ConfidenceField`` on model_class to
            seed from each extracted fact's ``confidence`` opinion, or
            ``None`` (default) to skip confidence seeding entirely.
        co_occurrence_field: Name of a ``CoOccurrenceField`` on
            model_class to link co-mentioned entities in, or ``None``
            (default) to skip association seeding entirely.
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
        extraction_provider=None,
        confidence_field=None,
        co_occurrence_field=None,
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

        self._extractor = extraction_provider or HeuristicExtractionProvider(
            min_length=extraction_min_length
        )
        self.confidence_field = confidence_field
        self.co_occurrence_field = co_occurrence_field

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

        Delegates to ``self._extractor`` (an ``AbstractExtractionProvider``,
        see ``popoto.extraction``) to turn ``response_text`` into
        ``ExtractedFact`` records, then saves each as a Memory record. By
        default ``self._extractor`` is a ``HeuristicExtractionProvider``,
        which splits the response into sentences and filters by minimum
        length -- this reproduces the original sentence-splitting behavior
        of this method byte-for-byte when no new constructor kwargs are
        passed. Pass ``extraction_provider=ClaudeExtractionProvider(...)``
        (see ``popoto.extraction.claude``) for LLM-based extraction with
        entities, importance, and confidence opinions.

        Importance-on-write nuance: each ``ExtractedFact.importance`` is
        used verbatim when the provider has an opinion (not ``None``);
        otherwise the ``importance`` argument passed to this call is used
        as the fallback. The heuristic provider never has an opinion, so
        its output always uses the caller-supplied ``importance``.

        If ``co_occurrence_field`` is configured and a fact names two or
        more distinct entities, every unordered pair is linked in that
        field's co-occurrence graph (see ``_seed_associations``). If
        ``confidence_field`` is configured and a fact has a confidence
        opinion, that field is seeded via ``_seed_confidence`` -- note
        ``ConfidenceField.update_confidence()`` blends the signal with the
        field's fixed ``initial_confidence`` rather than storing it
        verbatim; see ``_seed_confidence`` for the exact formula.

        Args:
            response_text: The LLM's response text.
            importance: Fallback importance score used for any extracted
                fact that has no importance opinion of its own (i.e.
                ``ExtractedFact.importance is None``). Float between 0.0
                and 1.0. Default 0.5.

        Returns:
            List of saved model instances. Empty list if response_text
            is empty or contains no extractable facts.
        """
        if not response_text or not response_text.strip():
            return []

        facts = self._extractor.extract(response_text)
        saved = []

        for fact in facts:
            eff_importance = (
                fact.importance if fact.importance is not None else importance
            )
            kwargs = {
                self.agent_id_field: self.agent_id,
                self.content_field: fact.text,
                self.importance_field: eff_importance,
            }
            try:
                instance = self.model_class(**kwargs)
                if instance.save() is not False:
                    saved.append(instance)
                    self._seed_associations(instance, fact)
                    self._seed_confidence(instance, fact)
            except Exception as e:
                logger.warning("Failed to save extracted memory: %s", e)

        return saved

    def _seed_associations(self, instance, fact):
        """Link co-mentioned entities in ``self.co_occurrence_field``.

        No-op unless ``self.co_occurrence_field`` is set, the named field
        exists on ``self.model_class``, and ``fact.entities`` contains at
        least two distinct (case-sensitive, whitespace-trimmed) names.

        Entity name strings -- not the saved ``instance``'s PK -- are used
        as the graph nodes: co-mention within one fact is treated as an
        association between entities, which is the natural unit for a
        write-time extraction graph (there is no record PK for an
        abstract entity). This is a deliberate departure from the field's
        usual record-PK-as-node convention; entity-name nodes and record
        PK nodes coexist in the same keyspace under
        ``$CoOcF:{ClassName}:{field}:``.

        Entities are deduplicated (order-stable) before pairing, since
        ``CoOccurrenceField.link()`` rejects self-pairs. Each pair is
        linked independently with its own try/except so one failing pair
        (e.g. a residual self-pair, or a transient Redis error) never
        drops the remaining valid pairs or the already-saved memory
        record.

        The deduped entity list is truncated to
        ``Defaults.EXTRACTION_MAX_ENTITIES_PER_FACT`` before pairing:
        combination count grows O(n^2), so an extraction result with an
        unusually large entity set (malformed provider output, or an
        adversarial input) can't blow up into a burst of co-occurrence
        writes for a single fact.

        Args:
            instance: The already-saved model instance for this fact.
            fact: The ``ExtractedFact`` that produced ``instance``.
        """
        if not self.co_occurrence_field:
            return
        if self.model_class._meta.fields.get(self.co_occurrence_field) is None:
            return

        seen = set()
        norm_entities = []
        for e in fact.entities:
            e = (e or "").strip()
            if e and e not in seen:
                seen.add(e)
                norm_entities.append(e)

        if len(norm_entities) < 2:
            return

        if len(norm_entities) > Defaults.EXTRACTION_MAX_ENTITIES_PER_FACT:
            norm_entities = norm_entities[: Defaults.EXTRACTION_MAX_ENTITIES_PER_FACT]

        field = getattr(self.model_class, self.co_occurrence_field)
        for a, b in itertools.combinations(norm_entities, 2):
            try:
                field.link(
                    self.model_class,
                    a,
                    b,
                    initial_weight=Defaults.EXTRACTION_ENTITY_PAIR_LINK_WEIGHT,
                )
            except Exception as exc:
                logger.warning("entity link %r<->%r failed: %s", a, b, exc)

    def _seed_confidence(self, instance, fact):
        """Seed ``self.confidence_field`` from ``fact.confidence``.

        No-op unless ``self.confidence_field`` is set, the named field
        exists on ``self.model_class``, and ``fact.confidence is not
        None``.

        Note: ``ConfidenceField`` has no per-instance "set initial value"
        API. The companion hash is seeded in ``on_save`` with the field's
        fixed ``initial_confidence`` (pseudo-count 1); this method's call
        to ``update_confidence()`` is the *first evidence update* against
        that prior, not a hard override. Concretely, seeding with signal
        ``s`` yields a stored confidence of ``(initial_confidence + s) /
        2`` -- not ``s`` verbatim. Callers should not expect
        ``fact.confidence`` to equal the stored value after this call.

        Args:
            instance: The already-saved model instance for this fact.
            fact: The ``ExtractedFact`` that produced ``instance``.
        """
        if not self.confidence_field:
            return
        if self.model_class._meta.fields.get(self.confidence_field) is None:
            return
        if fact.confidence is None:
            return

        try:
            ConfidenceField.update_confidence(
                instance, self.confidence_field, signal=fact.confidence
            )
        except Exception as exc:
            logger.warning(
                "confidence seed for %r failed: %s", self.confidence_field, exc
            )

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

        Delegates to ``HeuristicExtractionProvider._split_sentences`` --
        kept here (rather than removed) for backward compatibility with
        callers/tests that reference this staticmethod directly.
        ``extract_memories()`` no longer calls this method itself; it
        delegates to ``self._extractor`` instead (see
        ``popoto.extraction.HeuristicExtractionProvider``, which contains
        the canonical implementation).

        Args:
            text: Input text to split.

        Returns:
            List of sentence strings.
        """
        return HeuristicExtractionProvider._split_sentences(text)
