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
messages, so it drops into the OpenAI SDK, an agent harness, or a
hand-rolled loop without any framework dependency.

Dependencies:
    ContextAssembler (from popoto.recipes)
    ObservationProtocol (from popoto.fields.observation)
    A Popoto Model class with at least Level 1 fields (DecayingSortedField).
    Omit ``model_class`` to use ``popoto.recipes.DefaultMemory``.

Example:
    from popoto.recipes import SubconsciousMemory

    sm = SubconsciousMemory(agent_id="agent-1")

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
from .default_memory import DefaultMemory
from ..extraction import HeuristicExtractionProvider
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
from ..fields.observation import ObservationProtocol
from ..privacy.never_record import scan_never_record, write_tombstone

logger = logging.getLogger("POPOTO.SubconsciousMemory")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_EXTRACTION_MIN_LENGTH = 10
"""Minimum sentence length (chars) to be considered a fact worth saving."""

DEFAULT_SYSTEM_PREAMBLE = "You are a helpful assistant."
"""Default system message preamble when no system message exists."""

DEFAULT_SCORE_WEIGHTS = {"relevance": 1.0}
"""Composite-path score weights used when the caller passes none.

The benchmarked configuration, not the ``{"relevance": 0.6,
"confidence": 0.3}`` pair the guides used to show. Source:
``tests/benchmarks/results/sweep_20260326_125145.json`` →
``constants.score_weights.best_value``, over the coding_assistant /
research_agent / support_agent scenarios (18/18 points OK). Full
transparency on the strength of that evidence: all six swept
configurations tied at nDCG@5 = 1.0 on those scenarios, so this is the
*selected* best_value and the simplest single-index vector, not a
configuration measured to beat the alternatives. It also matches what
the hybrid/lexical suites use throughout, and in those modes
``score_weights`` is ignored for the pull path anyway.

Copied per instance -- never share this dict across constructions.
"""

DEFAULT_OUTPUT_FORMAT = "content"
"""Injected-context format: memory text only, as a ``"- "`` bullet list.

Issue #513 measured the previous ``"structured"`` JSON default at ~2.8x
the character count of the content it wrapped, spending the difference on
``memory_id`` UUIDs, the ``agent_id`` the caller already knows, and
``relevance`` as a bare epoch float that no model can interpret. Pass
``output_format="structured"`` to restore the pre-#513 payload verbatim.
"""


# ---------------------------------------------------------------------------
# SubconsciousMemory
# ---------------------------------------------------------------------------


class SubconsciousMemory:
    """Automatic memory injection and extraction around LLM turns.

    Wraps an existing chat flow with:
    - Pre-turn: assemble relevant memories, inject as system context
    - Post-turn: extract facts/observations from LLM response, save as Memory
    - Outcome: report how injected memories were used

    The only required argument is ``agent_id``::

        sm = SubconsciousMemory(agent_id="agent-1")

    That uses :class:`popoto.recipes.DefaultMemory`, which declares a
    ``BM25Field`` -- so ``retrieval_mode='auto'`` resolves to the
    query-sensitive ``lexical`` mode rather than the query-blind
    ``composite`` path a hand-rolled Level 1 model falls into.

    Args:
        model_class: Popoto Model class (any level from the quickstart
            guide). Default ``None`` → :class:`popoto.recipes.DefaultMemory`,
            the batteries-included model. When left at ``None``, that
            model's ``confidence`` and ``associations`` fields are also
            wired up automatically (see ``confidence_field`` and
            ``co_occurrence_field`` below); passing an explicit
            ``model_class`` keeps those at ``None`` exactly as before.
        agent_id: Identifier for the agent whose memories to query/save.
            Required -- it is the partition key, so omitting it would mix
            every agent's memories into one pool.
        score_weights: Dict mapping field names to weights for
            ContextAssembler. Default ``None`` → ``DEFAULT_SCORE_WEIGHTS``
            (``{"relevance": 1.0}``, the benchmarked vector; see that
            constant for the sweep provenance). Ignored by the pull path in
            lexical/hybrid modes.
        output_format: Format of the injected context block.
            ``"content"`` (default) emits memory text only.
            ``"structured"`` restores the pre-#513 JSON payload;
            ``"xml"`` and ``"natural"`` are also accepted. See
            ``DEFAULT_OUTPUT_FORMAT``.
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
            ``None`` to skip confidence seeding entirely. Default ``None``,
            except when ``model_class`` is left unset — the default model
            then wires its own ``"confidence"`` field.
        co_occurrence_field: Name of a ``CoOccurrenceField`` on
            model_class to link co-mentioned entities in, or ``None`` to
            skip association seeding entirely. Default ``None``, except
            when ``model_class`` is left unset — the default model then
            wires its own ``"associations"`` field.

    Raises:
        ValueError: If ``agent_id`` is not given.
    """

    def __init__(
        self,
        model_class=None,
        agent_id=None,
        score_weights=None,
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
        output_format=DEFAULT_OUTPUT_FORMAT,
    ):
        if agent_id is None:
            raise ValueError(
                "SubconsciousMemory requires agent_id — it partitions every "
                "index on the model, so without it all agents share one "
                "memory pool. Example: SubconsciousMemory(agent_id='agent-1')"
            )

        # Batteries-included path: no model_class means DefaultMemory, whose
        # optional fields are wired for the caller. Only applied when the
        # caller supplied neither the model nor the field name, so an
        # explicit model_class keeps the historical None defaults.
        if model_class is None:
            model_class = DefaultMemory
            if confidence_field is None:
                confidence_field = "confidence"
            if co_occurrence_field is None:
                co_occurrence_field = "associations"

        self.model_class = model_class
        self.agent_id = agent_id
        # dict(...) so the module-level default is never handed out by
        # reference — one instance mutating score_weights must not reach
        # another.
        self.score_weights = (
            dict(score_weights)
            if score_weights is not None
            else dict(DEFAULT_SCORE_WEIGHTS)
        )
        self.max_items = max_items
        self.max_tokens = max_tokens
        self.extraction_min_length = extraction_min_length
        self.system_preamble = system_preamble
        self.content_field = content_field
        self.importance_field = importance_field
        self.agent_id_field = agent_id_field
        self.output_format = output_format

        self._extractor = extraction_provider or HeuristicExtractionProvider(
            min_length=extraction_min_length
        )
        self.confidence_field = confidence_field
        self.co_occurrence_field = co_occurrence_field

        self._assembler = ContextAssembler(
            model_class=model_class,
            score_weights=self.score_weights,
            max_items=max_items,
            max_tokens=max_tokens,
            output_format=output_format,
            content_field=content_field,
        )

        # Never-record firewall (#561): whether the most recent
        # extract_memories() call returned empty *because* content was
        # deliberately dropped, as opposed to failing. Callers use this to
        # keep a privacy drop out of their failure channel -- see
        # MemoryService.capture().
        self._last_extraction_privacy_dropped = False

    @property
    def last_extraction_privacy_dropped(self) -> bool:
        """Whether the last ``extract_memories()`` call dropped for privacy.

        True when that call returned ``[]`` because the never-record
        firewall blocked the turn, or blocked every candidate fact. Reset at
        the top of every ``extract_memories()`` call, so it always describes
        the immediately preceding one.

        This exists because an empty return is otherwise indistinguishable
        from an outage, and ``MemoryService.capture()`` treats an empty
        return from non-empty text as a failure worth logging. Without this
        flag, every successful privacy drop would be recorded as a broken
        write path -- noise proportional to how well the firewall works.
        """
        return self._last_extraction_privacy_dropped

    @property
    def assembler(self) -> ContextAssembler:
        """The :class:`ContextAssembler` this recipe assembles context with.

        Public accessor for callers (e.g. ``MemoryService``) that need to
        reuse this recipe's already-configured assembler -- same
        ``score_weights``, ``max_items``, ``max_tokens`` and
        ``output_format`` -- instead of constructing a second one or
        reaching through the private ``_assembler`` attribute, which would
        break silently on a rename.
        """
        return self._assembler

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
        self._last_extraction_privacy_dropped = False

        if not response_text or not response_text.strip():
            return []

        # Never-record firewall, turn level (#561). Runs before the extractor
        # so an off-the-record marker voids the WHOLE turn -- including facts
        # derived from adjacent sentences, which a per-record gate cannot do
        # because the marker may live in a sentence that produced no fact.
        # Running before the provider also means that on the
        # ClaudeExtractionProvider path the content is never sent to the LLM
        # API at all. Applies regardless of model_class, so the guarantee
        # does not depend on anyone remembering to add the mixin.
        if Defaults.NEVER_RECORD_ENABLED:
            verdict = scan_never_record(response_text)
            if verdict.blocked:
                write_tombstone(self.model_class.__name__, verdict)
                self._last_extraction_privacy_dropped = True
                return []

        facts = self._extractor.extract(response_text)
        saved = []
        privacy_dropped = False

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
                elif (
                    Defaults.NEVER_RECORD_ENABLED
                    and scan_never_record(fact.text).blocked
                ):
                    # save() returned False and the fact is firewall-blocked,
                    # so this is a deliberate drop, not a rejected write. Note
                    # it so an all-dropped turn is not misreported as an
                    # outage. The tombstone was already written inside save().
                    privacy_dropped = True
            except Exception as e:
                logger.warning("Failed to save extracted memory: %s", e)

        if not saved and privacy_dropped:
            self._last_extraction_privacy_dropped = True

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
