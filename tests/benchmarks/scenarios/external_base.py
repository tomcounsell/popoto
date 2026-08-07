"""ExternalScenario base class for external benchmark datasets.

Extends the Scenario ABC to support external dataset integration.
Each ExternalScenario wraps a single BenchmarkItem and:
1. Ingests the conversation history into Popoto memory primitives via
   SubconsciousMemory.extract_memories() (one record per turn).
2. Runs retrieval via ContextAssembler.assemble(query_cues={"topic": query}).
3. Returns a ScenarioResult with retrieved_ids and relevant_ids matching
   the BenchmarkItem's ground truth.

Model class used:
    ExternalBenchmarkMemory — a minimal Popoto Model with:
    - agent_id: KeyField (partitions data per benchmark item)
    - content: StringField (the turn text)
    - importance: FloatField (fixed at 0.5 for baseline)
    - relevance: DecayingSortedField (scored via CompositeScoreQuery)
    - certainty: ConfidenceField (initial 0.5)
    - content_index: BM25Field (lexical signal — present in lexical/hybrid)
    - embedding: EmbeddingField (vector signal — present in hybrid/vector)

    Class is re-created per benchmark item with a unique name prefix to
    ensure Redis key isolation between items. teardown() scans and deletes
    all keys matching the class prefix (and the per-class embedding dir).

Retrieval mode (issue #437, #455):
    For ``lexical``/``hybrid`` retrieval is driven through
    ``ContextAssembler.assemble()`` as the **primary** path. The assembler is
    constructed with ``retrieval_mode="auto"`` so field presence drives mode
    resolution (issue #395):
    - Model has BM25Field + EmbeddingField → ``"hybrid"`` (BM25 + vector via RRF)
    - Model has BM25Field only → ``"lexical"`` (query-sensitive BM25, no vector)
    - Model has neither → ``"composite"`` (query-blind)

    ``ExternalScenario(retrieval_mode="lexical")`` (the default) declares a
    BM25Field only, so auto-mode resolves to ``"lexical"``.
    ``ExternalScenario(retrieval_mode="hybrid")`` additionally declares an
    EmbeddingField with a local SentenceTransformersProvider, so auto-mode
    resolves to ``"hybrid"`` and the RRF fusion (k=60) runs.

    ``ExternalScenario(retrieval_mode="vector")`` is a harness-local diagnostic
    (issue #455): it declares an EmbeddingField and NO BM25Field, and
    **bypasses** the assembler entirely — ``run()`` ranks by pure cosine over
    the EmbeddingField (auto-mode would resolve an embedding-only model to the
    query-blind ``"composite"`` path, not vector search). No BM25, no RRF, no
    graph — it isolates only the dense arm.

Ranking unit (issue #514):
    Retrieved records are collapsed to result IDs at exactly ONE granularity
    per dataset — the granularity that dataset annotates its ground truth at:

    - LongMemEval-S → ``session`` (ground truth is ``answer_session_ids``)
    - LoCoMo        → ``turn`` (ground truth is ``qa[].evidence`` dia_ids)

    The unit is resolved from dataset metadata before retrieval runs, so the
    answer key influences only the final scoring, never which candidate ID a
    retrieved record emits or how results are deduplicated. Every retrieved
    record is treated identically regardless of whether it is gold.

This module is text-only: turns without content are silently skipped.
"""

import logging
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

from src import popoto
from src.popoto.embeddings.sentence_transformers import SentenceTransformersProvider
from src.popoto.fields.bm25_field import BM25Field
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.embedding_field import (
    EmbeddingField,
    _get_embeddings_dir,
    stop_invalidation_listeners,
)
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..datasets import GROUND_TRUTH_UNITS, BenchmarkItem, ground_truth_unit
from .base import Scenario, ScenarioResult

logger = logging.getLogger("POPOTO.Benchmark.ExternalScenario")

# Score weights — used by composite path when no BM25/embedding fields present
BASELINE_SCORE_WEIGHTS = {"relevance": 1.0}

# Number of records the assembler / vector diagnostic returns per query. Sized
# to cover Recall@10 with headroom.
MAX_ITEMS = 20

# Shared embedding provider, lazily constructed once per process.
#
# ``_build_external_model_class`` is called once per benchmark item (from
# ``setup()``), so constructing a fresh ``SentenceTransformersProvider`` inline
# would build a brand-new provider for every one of the 500 items. Each fresh
# instance reloads ``all-MiniLM-L6-v2`` on its first ``embed()`` (the model is
# cached on the instance, not the class), so the model would be reloaded into
# memory 500× over a full run. Sharing a single instance loads MiniLM once and
# reuses the loaded model across every item. The provider is read-only after its
# one-time model load (each ``embed()`` is an independent forward pass), so reuse
# across the per-item model classes is safe.
_SHARED_PROVIDER = None


def _get_shared_provider():
    """Return the process-wide shared ``SentenceTransformersProvider``.

    Constructed lazily on first call and cached at module level so MiniLM
    loads once per run rather than once per benchmark item. Constructing the
    provider does nothing heavy and triggers no model download — the model is
    loaded lazily on the first ``embed()`` call — so calling this accessor for
    its identity (without embedding) never loads the model.
    """
    global _SHARED_PROVIDER
    if _SHARED_PROVIDER is None:
        _SHARED_PROVIDER = SentenceTransformersProvider()
    return _SHARED_PROVIDER


def _build_graph_model_class(safe_prefix: str):
    """Build a BM25 + association-graph Model class for one benchmark item.

    Used by ``retrieval_mode="graph"`` (issue #484). Identical to the lexical
    model (BM25Field only, no EmbeddingField) plus the two association
    primitives graph traversal walks:

    - ``associations``: a ``CoOccurrenceField`` — weighted, symmetric edges.
    - ``prev_turn``: a self-referential ``Relationship`` — the only edge kind
      ``graph_traversal.expand_relationships()`` honors (a ``Relationship``
      pointing at a different model class is skipped by design).

    Edge *content* is a harness decision, not a library one: the LoCoMo/
    LongMemEval adapters carry no annotated entity graph, so ``setup()``
    builds conversational-adjacency edges (turn i <-> turn i-1 within a
    session). That is the only association signal derivable from the raw
    datasets without inventing an extraction model, and it is stated
    explicitly in the report so the number is not read as "popoto's semantic
    graph".
    """
    from src.popoto.fields.relationship import Relationship

    class ExternalBenchmarkMemory(popoto.Model):
        turn_id = popoto.AutoKeyField()
        agent_id = popoto.KeyField()
        content = popoto.StringField(default="")
        importance = popoto.FloatField(default=0.5)
        relevance = DecayingSortedField(
            decay_rate=0.5,
            base_score_field="importance",
            partition_by="agent_id",
        )
        certainty = ConfidenceField(initial_confidence=0.5)
        content_index = BM25Field(source="content")
        associations = CoOccurrenceField(symmetric=True, max_edges=100)

    ExternalBenchmarkMemory.__name__ = f"ExtMem{safe_prefix}"
    ExternalBenchmarkMemory.__qualname__ = f"ExtMem{safe_prefix}"
    # Self-referential Relationship must be registered post-hoc — the class
    # object does not exist inside its own body (same pattern as
    # tests/test_graph_traversal.py).
    ExternalBenchmarkMemory.prev_turn = Relationship(
        model=ExternalBenchmarkMemory, null=True
    )
    ExternalBenchmarkMemory._meta.add_field(
        "prev_turn", ExternalBenchmarkMemory.prev_turn
    )
    return ExternalBenchmarkMemory


def _build_external_model_class(
    safe_prefix: str,
    with_bm25: bool = True,
    with_embedding: bool = False,
):
    """Build a fresh Popoto Model class for one benchmark item.

    Uses a unique class name derived from ``safe_prefix`` so each item
    gets its own Redis key namespace. This prevents cross-item contamination
    when running multiple items sequentially.

    The declared retrieval fields select which retrieval arm the benchmark
    exercises. All three benchmark modes save **one record per turn** from the
    same ``content`` source, so the BM25 and embedding arms index the identical
    per-turn units (no granularity mismatch between arms):

    - ``with_bm25=True, with_embedding=False`` → **lexical** — BM25 only. Under
      ``ContextAssembler(retrieval_mode="auto")`` this resolves to ``"lexical"``.
    - ``with_bm25=True, with_embedding=True`` → **hybrid** — BM25 + vector
      (all-MiniLM-L6-v2) fused via RRF; auto-mode resolves to ``"hybrid"``.
    - ``with_bm25=False, with_embedding=True`` → **vector** — EmbeddingField
      only. NOTE: auto-mode would resolve an embedding-only model to
      ``"composite"`` (query-blind), so the vector benchmark path does **not**
      route through the assembler; it ranks by pure cosine directly (see
      ``ExternalScenario.run()``).

    Args:
        safe_prefix: Short alphanumeric prefix (e.g., "a3f9b2c1").
        with_bm25: If True, declare a BM25Field (lexical signal).
        with_embedding: If True, declare an EmbeddingField (vector signal)
            backed by the shared SentenceTransformersProvider.

    Returns:
        A new Popoto Model class with agent_id, content, importance, relevance,
        certainty, and the selected retrieval field(s).
    """

    if with_bm25 and with_embedding:

        class ExternalBenchmarkMemory(popoto.Model):
            turn_id = popoto.AutoKeyField()
            agent_id = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.5,
                base_score_field="importance",
                partition_by="agent_id",
            )
            certainty = ConfidenceField(initial_confidence=0.5)
            content_index = BM25Field(source="content")
            embedding = EmbeddingField(
                source="content",
                provider=_get_shared_provider(),
            )

    elif with_embedding:

        # Vector-only: EmbeddingField, no BM25Field. Ranked by pure cosine in
        # ExternalScenario.run() (the assembler is bypassed for this mode).
        class ExternalBenchmarkMemory(popoto.Model):
            turn_id = popoto.AutoKeyField()
            agent_id = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.5,
                base_score_field="importance",
                partition_by="agent_id",
            )
            certainty = ConfidenceField(initial_confidence=0.5)
            embedding = EmbeddingField(
                source="content",
                provider=_get_shared_provider(),
            )

    else:

        class ExternalBenchmarkMemory(popoto.Model):
            turn_id = popoto.AutoKeyField()
            agent_id = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.5,
                base_score_field="importance",
                partition_by="agent_id",
            )
            certainty = ConfidenceField(initial_confidence=0.5)
            content_index = BM25Field(source="content")

    ExternalBenchmarkMemory.__name__ = f"ExtMem{safe_prefix}"
    ExternalBenchmarkMemory.__qualname__ = f"ExtMem{safe_prefix}"
    return ExternalBenchmarkMemory


def collapse_to_ranking_unit(
    retrieved_keys: List[str], unit_map: Dict[str, List[str]]
) -> List[str]:
    """Collapse ranked Redis keys to ranked ground-truth IDs, gold-blind.

    Every retrieved record is mapped through ``unit_map`` — one map, one
    granularity — and deduplicated by first occurrence. There is deliberately
    no ``relevant_ids`` parameter: the answer key cannot reach this function,
    so it cannot influence which ID a record emits or how many rank slots the
    result consumes (issue #514).

    The defect this replaces merged the session-ID and turn-ID spaces into one
    candidate list per record and emitted whichever candidate appeared in the
    answer key, falling back to the first candidate (always the session ID)
    otherwise. On LoCoMo (turn-ID ground truth) that gave every gold turn its
    own unique rank slot while non-gold turns collapsed into one shared
    session slot, lifting gold ranks and inflating Recall@K/MRR. Measured
    compression over the full 1986-question LoCoMo corpus: 20 retrieved turns
    became 13.2 rank slots on average (``locomo_20260708.json``), against
    19.9 under this function (``locomo_20260807.json``).

    Args:
        retrieved_keys: Redis keys of the retrieved records, in rank order.
        unit_map: ``ground_truth_id -> [redis_key, ...]`` at a single
            granularity (``_turn_key_map`` or ``_session_key_map``).

    Returns:
        Ground-truth IDs in rank order, first occurrence wins. A key with no
        entry in ``unit_map`` emits the raw Redis key, which can never match
        ground truth — an honest miss rather than a silent drop.
    """
    redis_key_to_unit_id: Dict[str, str] = {}
    for id_value, keys in unit_map.items():
        for key in keys:
            # First writer wins: a record belongs to exactly one turn and one
            # session, so collisions only arise for degenerate inputs.
            redis_key_to_unit_id.setdefault(key, id_value)

    ranked: List[str] = []
    seen: set = set()
    for redis_key in retrieved_keys:
        unit_id = redis_key_to_unit_id.get(redis_key, redis_key)
        if unit_id not in seen:
            seen.add(unit_id)
            ranked.append(unit_id)
    return ranked


def _resolve_ranking_unit(item: BenchmarkItem) -> str:
    """Resolve the ranking unit for ``item`` without reading its answer key.

    Precedence: explicit ``metadata["ground_truth_unit"]`` (set by the dataset
    adapters) > the dataset-name mapping > the package default. Every input is
    a property of the *dataset*, fixed before any question is asked;
    ``item.relevant_ids`` is deliberately not consulted (issue #514).

    Args:
        item: The benchmark item being run.

    Returns:
        ``"session"`` or ``"turn"``.
    """
    metadata = item.metadata or {}
    declared = str(metadata.get("ground_truth_unit", "") or "").strip().lower()
    if declared in GROUND_TRUTH_UNITS:
        return declared
    return ground_truth_unit(str(metadata.get("dataset", "") or ""))


class ExternalScenario(Scenario):
    """Scenario wrapping a single external BenchmarkItem.

    Ingest the item's conversation history, query via ContextAssembler,
    and report Recall@K / MRR metrics.

    Args:
        item: BenchmarkItem from a dataset adapter.
        overrides: Optional override dict (unused in baseline; present for
            API compatibility with sweep infrastructure).
        retrieval_mode: ``"lexical"`` (default, BM25 only), ``"hybrid"``
            (BM25 + vector via RRF), ``"graph"`` (BM25 + graph traversal over
            CoOccurrence/Relationship adjacency edges), or ``"vector"``
            (EmbeddingField-only, pure cosine). Hybrid additionally declares an EmbeddingField on
            the model so auto-mode resolves to hybrid. Vector declares an
            EmbeddingField and no BM25Field, and ``run()`` ranks by raw
            cosine directly (the assembler is bypassed — a harness-local
            diagnostic).
    """

    name = "external_base"

    def __init__(
        self,
        item: BenchmarkItem,
        overrides: Optional[Dict[str, Any]] = None,
        retrieval_mode: str = "lexical",
        extraction_provider: Optional[Any] = None,
        extraction_stats: Optional[Any] = None,
    ):
        super().__init__(overrides)
        self.item = item
        self.retrieval_mode = retrieval_mode
        # Ingest arm (#489). None => write turns verbatim (committed baseline).
        self._extractor = extraction_provider
        self._extraction_stats = extraction_stats
        self._agent_id = f"extbench:{uuid.uuid4().hex[:12]}"
        self._model_class = None
        self._assembler = None
        self._saved_records: List[Any] = []
        # Map from session_id -> list of Redis keys saved for that session
        self._session_key_map: Dict[str, List[str]] = {}
        # Map from turn_id -> list of Redis keys saved for that turn. Kept
        # SEPARATE from the session map (issue #514): merging both id spaces
        # into one map forced run() to pick between two granularities per
        # record, and the old picker made that choice by consulting the answer
        # key. Two maps + one dataset-level unit removes the choice entirely.
        self._turn_key_map: Dict[str, List[str]] = {}
        # Ranking unit for this item, fixed by the dataset (session for
        # LongMemEval-S, turn for LoCoMo). Resolved at construction time from
        # dataset metadata — never from ``item.relevant_ids`` — so the answer
        # key cannot influence ranking or dedup, only final scoring.
        self._ranking_unit = _resolve_ranking_unit(item)

    def setup(self) -> None:
        """Ingest the benchmark item's conversation history into Redis.

        Each turn becomes one memory record. The turn's session_id is
        tracked so we can map retrieved Redis keys back to ground-truth
        session IDs during retrieval scoring.

        Raises:
            ConnectionError: If Redis is unavailable.
        """
        safe_prefix = uuid.uuid4().hex[:8]
        # Field presence per mode:
        #   lexical → BM25 only; hybrid → BM25 + embedding; vector → embedding
        #   only; graph → BM25 + CoOccurrenceField + self-referential Relationship.
        if self.retrieval_mode == "graph":
            self._model_class = _build_graph_model_class(safe_prefix)
        else:
            self._model_class = _build_external_model_class(
                safe_prefix,
                with_bm25=self.retrieval_mode != "vector",
                with_embedding=self.retrieval_mode in ("hybrid", "vector"),
            )
        # lexical/hybrid drive ContextAssembler.assemble() as the primary path with
        # retrieval_mode="auto": field presence resolves the mode (issue #395) —
        # BM25 + EmbeddingField → "hybrid" (RRF k=60); BM25 only → "lexical".
        #
        # vector mode is EmbeddingField-only. auto-mode would resolve that to
        # "composite" (query-blind), NOT vector search, so vector mode does NOT
        # build an assembler — run() ranks by pure cosine directly.
        if self.retrieval_mode == "vector":
            self._assembler = None
        elif self.retrieval_mode == "graph":
            # BM25 + graph arm. `graph_traversal_relationship_fields` is the
            # opt-in kwarg from PR #483 — without it the assembler walks
            # CoOccurrence edges only and never the Relationship field.
            self._assembler = ContextAssembler(
                model_class=self._model_class,
                score_weights=BASELINE_SCORE_WEIGHTS,
                max_items=MAX_ITEMS,
                retrieval_mode="auto",
                graph_traversal_relationship_fields=["prev_turn"],
            )
        else:
            self._assembler = ContextAssembler(
                model_class=self._model_class,
                score_weights=BASELINE_SCORE_WEIGHTS,
                max_items=MAX_ITEMS,
                retrieval_mode="auto",
            )

        prev_by_session: Dict[str, Any] = {}

        for turn in self.item.history:
            content = turn.get("content", "")
            if not content or not content.strip():
                continue  # Skip empty / image-only turns

            session_id = turn.get("session_id", "")
            turn_id = turn.get("turn_id", "")
            # Ingest arm (#489). Without an extractor the turn is written
            # verbatim (the committed baseline). With one, the turn may yield
            # zero, one, or many fact records — every record produced from
            # this turn is attributed back to the SAME session_id/turn_id, so
            # ground-truth scoring stays valid under a 1:N turn->record
            # mapping. A turn that extracts to nothing is written as nothing
            # and becomes unretrievable; that is a real property of the
            # extraction path, so it is counted rather than papered over.
            units = self._extract_units(content.strip())
            if self._extraction_stats is not None:
                self._extraction_stats.turns_seen += 1
                if not units:
                    self._extraction_stats.turns_dropped += 1

            # Representative record for this turn, used to build graph-mode
            # conversational-adjacency edges (turn i <-> turn i-1). With the
            # raw arm (1 unit/turn) this is simply the turn's single record, so
            # graph behavior is byte-identical to the pre-#489 path.
            turn_first_instance = None

            for unit_text, unit_importance in units:
                try:
                    instance = self._model_class(
                        agent_id=self._agent_id,
                        content=unit_text,
                        importance=unit_importance,
                    )
                    result = instance.save()
                    if result is False:
                        logger.warning(
                            "Failed to save unit for item %s (save() returned False)",
                            self.item.item_id,
                        )
                        continue
                    self._saved_records.append(instance)
                    if self._extraction_stats is not None:
                        self._extraction_stats.facts_written += 1
                    if turn_first_instance is None:
                        turn_first_instance = instance
                    # Track session_id -> redis_key AND turn_id -> redis_key in
                    # two separate maps. LongMemEval-S scores at session
                    # granularity, LoCoMo at turn granularity; run() collapses
                    # retrieved keys through exactly one of these maps, chosen
                    # by the dataset (issue #514).
                    try:
                        redis_key = instance.db_key.redis_key
                        if session_id:
                            self._session_key_map.setdefault(session_id, []).append(
                                redis_key
                            )
                        if turn_id:
                            self._turn_key_map.setdefault(turn_id, []).append(redis_key)
                    except Exception as e:
                        logger.debug("Could not get redis_key: %s", e)
                except Exception as e:
                    logger.warning(
                        "Error saving unit for item %s: %s", self.item.item_id, e
                    )

            # Graph mode (#484): build conversational-adjacency edges between
            # this turn's representative record and the previous turn's, via
            # the self-referential Relationship (walked forward AND reverse by
            # expand_relationships) plus a symmetric CoOccurrence edge. The
            # datasets ship no annotated entity graph, so adjacency is the only
            # edge derivable without an extraction model — see the report caveat.
            if self.retrieval_mode == "graph" and turn_first_instance is not None:
                prev = prev_by_session.get(session_id)
                if prev is not None:
                    try:
                        turn_first_instance.prev_turn = prev
                        turn_first_instance.save()
                        self._model_class._meta.fields["associations"].link(
                            self._model_class,
                            turn_first_instance.db_key.redis_key,
                            prev.db_key.redis_key,
                            initial_weight=0.5,
                        )
                    except Exception as e:
                        logger.warning("graph edge build failed: %s", e)
                prev_by_session[session_id] = turn_first_instance

    def _extract_units(self, content: str) -> List[tuple]:
        """Turn one turn's text into the record units to write.

        Mirrors ``SubconsciousMemory.extract_memories()``'s wiring: a
        provider's per-fact ``importance`` wins verbatim, and the flat 0.5 is
        only the fallback when the provider has no opinion. Confidence is
        intentionally not seeded — the recipe only seeds it when a
        ``confidence_field`` is configured, and this harness does not
        configure one, so leaving it at the field default keeps this arm
        faithful to the shipped path.

        Args:
            content: Stripped turn text.

        Returns:
            List of ``(text, importance)`` pairs. ``[(content, 0.5)]`` for the
            raw arm; zero or more extracted facts otherwise.
        """
        if self._extractor is None:
            return [(content, 0.5)]
        facts = self._extractor.extract(content)
        return [
            (
                f.text,
                f.importance if f.importance is not None else 0.5,
            )
            for f in facts
            if f.text and f.text.strip()
        ]

    def run(self) -> ScenarioResult:
        """Run retrieval and return ScenarioResult.

        For ``lexical``/``hybrid`` modes, retrieval is driven through
        ``ContextAssembler.assemble()`` as the **primary** path (issue #437).
        The assembler's effective mode is resolved from field presence:
        ``"lexical"`` (BM25 only) or ``"hybrid"`` (BM25 + vector fused via RRF).

        For the ``vector`` mode the assembler is **bypassed** entirely: the
        harness ranks by pure cosine over the EmbeddingField via
        ``QueryBuilder._get_vector_scores()`` (all-MiniLM-L6-v2, 384-dim,
        in-process numpy cosine). ``ContextAssembler`` auto-mode would resolve
        an embedding-only model to ``"composite"`` (query-blind), which is not
        vector search, so this harness-local diagnostic scores raw cosine
        directly. No BM25, no RRF, no graph.

        In every mode the selected records' Redis keys are mapped back to
        ground-truth IDs at a single granularity — ``self._ranking_unit``,
        fixed by the dataset — via ``_turn_key_map`` or ``_session_key_map``.
        The answer key is never consulted here (issue #514).

        Measures latency of the retrieval call only (not ingestion).

        Returns:
            ScenarioResult with retrieved_ids as ranking-unit IDs (turn IDs for
            LoCoMo, session IDs for LongMemEval-S) and relevant_ids from ground
            truth (same key space, via the mapping built during setup).
        """
        if self._model_class is None:
            return ScenarioResult(
                scenario_name=self.name,
                status="error",
                error_message="setup() not called",
            )

        if not self._saved_records:
            return ScenarioResult(
                scenario_name=self.name,
                status="skipped-empty",
                error_message="No turns ingested (empty history)",
            )

        t0 = time.monotonic()

        retrieved_keys: List[str] = []
        # Retrieved memory texts in rank order — consumed by the judged-answer
        # stage (#458). Only populated on the assembler path, where records carry
        # `.content`; the vector path holds only (redis_key, cosine) pairs and
        # judged mode rejects vector up front, so it is left empty there.
        retrieved_contents: List[str] = []
        if self.retrieval_mode == "vector":
            # Vector-only diagnostic: pure cosine over the EmbeddingField, no
            # ContextAssembler. _get_vector_scores() returns (redis_key, cosine)
            # pairs sorted by similarity descending, so the redis keys are
            # already the ranked retrieval and no hydration is needed to map
            # them back to ground-truth IDs.
            try:
                from src.popoto.models.query import QueryBuilder

                qb = QueryBuilder(self._model_class.query)
                scored_pairs = qb._get_vector_scores(self.item.query, limit=MAX_ITEMS)
                retrieved_keys = [redis_key for redis_key, _score in scored_pairs]
            except Exception as e:
                return ScenarioResult(
                    scenario_name=self.name,
                    status="error",
                    error_message=f"vector cosine ranking failed: {e}",
                )
            retrieval_method = "vector"
        else:
            # Primary retrieval: ContextAssembler.assemble(). With both BM25 and
            # EmbeddingField present (hybrid mode) this runs _pull_path_hybrid()
            # (RRF k=60); with BM25 only it runs the lexical pull path.
            try:
                assembly_result = self._assembler.assemble(
                    query_cues={"topic": self.item.query},
                    agent_id=self._agent_id,
                )
                for record in assembly_result.records:
                    try:
                        retrieved_keys.append(record.db_key.redis_key)
                    except Exception:
                        retrieved_keys.append(str(id(record)))
                    # Capture the record text (rank order) for the judged stage.
                    retrieved_contents.append(getattr(record, "content", "") or "")
            except Exception as e:
                return ScenarioResult(
                    scenario_name=self.name,
                    status="error",
                    error_message=f"assemble() failed: {e}",
                )

            # The assembler's effective mode (e.g. "lexical" / "hybrid").
            retrieval_method = getattr(
                self._assembler, "_effective_mode", self.retrieval_mode
            )

        retrieval_ms = (time.monotonic() - t0) * 1000

        # Collapse retrieved Redis keys to ranked IDs at ONE granularity — the
        # dataset's ground-truth unit (session for LongMemEval-S, turn for
        # LoCoMo), resolved in __init__ from dataset metadata. The answer key is
        # not in scope here; see ``collapse_to_ranking_unit`` (issue #514).
        unit_map = (
            self._turn_key_map
            if self._ranking_unit == "turn"
            else self._session_key_map
        )
        retrieved_unit_ids = collapse_to_ranking_unit(retrieved_keys, unit_map)

        return ScenarioResult(
            scenario_name=self.name,
            retrieved_ids=retrieved_unit_ids,
            relevant_ids=set(self.item.relevant_ids),
            metadata={
                "ranking_unit": self._ranking_unit,
                "item_id": self.item.item_id,
                "query": self.item.query,
                "n_history_turns": len(self.item.history),
                "n_saved_records": len(self._saved_records),
                "n_retrieved": len(retrieved_unit_ids),
                "retrieval_ms": round(retrieval_ms, 2),
                "dataset": self.item.metadata.get("dataset", "unknown"),
                "question_type": self.item.metadata.get("question_type", ""),
                "retrieval_method": retrieval_method,
                "retrieved_contents": retrieved_contents,
            },
        )

    def teardown(self) -> None:
        """Delete all Redis keys created during setup."""
        for record in self._saved_records:
            try:
                record.delete()
            except Exception:
                pass

        # Scan and clean by model class name
        if self._model_class:
            class_name = self._model_class.__name__
            try:
                cursor = 0
                while True:
                    cursor, keys = POPOTO_REDIS_DB.scan(
                        cursor, match=f"{class_name}:*", count=200
                    )
                    if keys:
                        POPOTO_REDIS_DB.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass

        # Also scan by agent_id prefix
        try:
            agent_prefix = self._agent_id.replace(":", "").replace("-", "")[:12]
            cursor = 0
            while True:
                cursor, keys = POPOTO_REDIS_DB.scan(
                    cursor, match=f"*{agent_prefix}*", count=200
                )
                if keys:
                    POPOTO_REDIS_DB.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

        # Remove on-disk embedding artifacts (.npy + index sidecar) for this
        # per-item model class. No-op in lexical mode (dir never created).
        if self._model_class:
            shutil.rmtree(
                os.path.join(_get_embeddings_dir(), self._model_class.__name__),
                ignore_errors=True,
            )

        # Stop this item's embedding-cache invalidation listener. Each listener
        # is a PubSubWorkerThread holding a checked-out pool connection keyed by
        # model class name; with a fresh class per item, an unbounded run would
        # exhaust the 128-connection BlockingConnectionPool at ~item 120 and
        # block forever. Items run sequentially, so stopping all is stopping
        # ours. No-op in lexical mode (no listener ever starts).
        stop_invalidation_listeners()

        super().teardown()
