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
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.embedding_field import (
    EmbeddingField,
    _get_embeddings_dir,
    stop_invalidation_listeners,
)
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..datasets import BenchmarkItem
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


class ExternalScenario(Scenario):
    """Scenario wrapping a single external BenchmarkItem.

    Ingest the item's conversation history, query via ContextAssembler,
    and report Recall@K / MRR metrics.

    Args:
        item: BenchmarkItem from a dataset adapter.
        overrides: Optional override dict (unused in baseline; present for
            API compatibility with sweep infrastructure).
        retrieval_mode: ``"lexical"`` (default, BM25 only), ``"hybrid"``
            (BM25 + vector via RRF), or ``"vector"`` (EmbeddingField-only,
            pure cosine). Hybrid additionally declares an EmbeddingField on
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
    ):
        super().__init__(overrides)
        self.item = item
        self.retrieval_mode = retrieval_mode
        self._agent_id = f"extbench:{uuid.uuid4().hex[:12]}"
        self._model_class = None
        self._assembler = None
        self._saved_records: List[Any] = []
        # Map from session_id -> list of Redis keys saved for that session
        self._session_key_map: Dict[str, List[str]] = {}

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
        #   lexical → BM25 only; hybrid → BM25 + embedding; vector → embedding only.
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
        else:
            self._assembler = ContextAssembler(
                model_class=self._model_class,
                score_weights=BASELINE_SCORE_WEIGHTS,
                max_items=MAX_ITEMS,
                retrieval_mode="auto",
            )

        for turn in self.item.history:
            content = turn.get("content", "")
            if not content or not content.strip():
                continue  # Skip empty / image-only turns

            session_id = turn.get("session_id", "")
            turn_id = turn.get("turn_id", "")
            try:
                instance = self._model_class(
                    agent_id=self._agent_id,
                    content=content.strip(),
                    importance=0.5,
                )
                result = instance.save()
                if result is False:
                    logger.warning(
                        "Failed to save turn for item %s (save() returned False)",
                        self.item.item_id,
                    )
                    continue
                self._saved_records.append(instance)
                # Track session_id -> redis_key AND turn_id -> redis_key mappings.
                # LongMemEval-S uses session_id as relevant_id; LoCoMo uses turn_id.
                try:
                    redis_key = instance.db_key.redis_key
                    if session_id:
                        self._session_key_map.setdefault(session_id, []).append(
                            redis_key
                        )
                    if turn_id:
                        self._session_key_map.setdefault(turn_id, []).append(redis_key)
                except Exception as e:
                    logger.debug("Could not get redis_key: %s", e)
            except Exception as e:
                logger.warning(
                    "Error saving turn for item %s: %s", self.item.item_id, e
                )

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
        ground-truth session/turn IDs via ``_session_key_map``.

        Measures latency of the retrieval call only (not ingestion).

        Returns:
            ScenarioResult with retrieved_ids as session/turn IDs and
            relevant_ids from ground truth (same key space, via the
            session_id/turn_id -> redis_keys mapping built during setup).
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

        # Build reverse-map: redis_key -> list of IDs (session_id or turn_id).
        # We need to match retrieved redis keys back to whatever ID type the
        # ground truth uses (session_id for LongMemEval-S, turn_id for LoCoMo).
        # _session_key_map tracks both; we prefer IDs that appear in relevant_ids.
        redis_key_to_ids: Dict[str, List[str]] = {}
        for id_value, keys in self._session_key_map.items():
            for key in keys:
                redis_key_to_ids.setdefault(key, []).append(id_value)

        relevant_ids = set(self.item.relevant_ids)
        retrieved_session_ids = []
        seen: set = set()

        for rk in retrieved_keys:
            candidate_ids = redis_key_to_ids.get(rk, [rk])
            # Prefer IDs that appear in relevant_ids (match ground truth key space)
            matching = [cid for cid in candidate_ids if cid in relevant_ids]
            chosen_ids = matching if matching else candidate_ids[:1]
            for chosen_id in chosen_ids:
                if chosen_id not in seen:
                    seen.add(chosen_id)
                    retrieved_session_ids.append(chosen_id)

        return ScenarioResult(
            scenario_name=self.name,
            retrieved_ids=retrieved_session_ids,
            relevant_ids=set(self.item.relevant_ids),
            metadata={
                "item_id": self.item.item_id,
                "query": self.item.query,
                "n_history_turns": len(self.item.history),
                "n_saved_records": len(self._saved_records),
                "n_retrieved": len(retrieved_session_ids),
                "retrieval_ms": round(retrieval_ms, 2),
                "dataset": self.item.metadata.get("dataset", "unknown"),
                "question_type": self.item.metadata.get("question_type", ""),
                "retrieval_method": retrieval_method,
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
