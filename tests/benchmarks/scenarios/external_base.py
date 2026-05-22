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
    - content_index: BM25Field (hybrid retrieval — lexical signal)

    Class is re-created per benchmark item with a unique name prefix to
    ensure Redis key isolation between items. teardown() scans and deletes
    all keys matching the class prefix.

Retrieval mode:
    ``ContextAssembler`` is constructed with ``retrieval_mode="auto"``.
    Because the model has a BM25Field but no EmbeddingField (no provider
    configured), auto-mode resolves to ``"composite"`` — but BM25 lexical
    search is available for direct use via ``keyword_search()``.

    Effective mode selection (issue #395):
    - Model has BM25Field + EmbeddingField → ``"hybrid"`` (BM25 + vector via RRF)
    - Model has BM25Field only → ``"composite"`` (auto fallback)
    - Model has EmbeddingField only → ``"composite"`` (auto fallback)
    - Model has neither → ``"composite"`` (original behaviour)

    The benchmark baseline uses BM25Field only. Adding an EmbeddingField
    with a configured provider would enable full hybrid mode.

This module is text-only: turns without content are silently skipped.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from src import popoto
from src.popoto.fields.bm25_field import BM25Field
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..datasets import BenchmarkItem
from .base import Scenario, ScenarioResult

logger = logging.getLogger("POPOTO.Benchmark.ExternalScenario")

# Score weights — used by composite path when no BM25/embedding fields present
BASELINE_SCORE_WEIGHTS = {"relevance": 1.0}


def _build_external_model_class(safe_prefix: str):
    """Build a fresh Popoto Model class for one benchmark item.

    Uses a unique class name derived from ``safe_prefix`` so each item
    gets its own Redis key namespace. This prevents cross-item contamination
    when running multiple items sequentially.

    Includes BM25Field for lexical retrieval. ContextAssembler will use
    ``retrieval_mode="auto"``; when both BM25Field and EmbeddingField are
    present the hybrid (RRF) pull path is used automatically.

    Args:
        safe_prefix: Short alphanumeric prefix (e.g., "a3f9b2c1").

    Returns:
        A new Popoto Model class with agent_id, content, importance,
        relevance, certainty, and content_index fields.
    """

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
    """

    name = "external_base"

    def __init__(self, item: BenchmarkItem, overrides: Optional[Dict[str, Any]] = None):
        super().__init__(overrides)
        self.item = item
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
        self._model_class = _build_external_model_class(safe_prefix)
        # retrieval_mode="auto": selects hybrid when BM25+EmbeddingField
        # detected, composite otherwise. The baseline model has BM25Field
        # only → composite path, but keyword_search() is available via
        # BM25Field.search() for direct use.
        self._assembler = ContextAssembler(
            model_class=self._model_class,
            score_weights=BASELINE_SCORE_WEIGHTS,
            max_items=20,
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

        Uses BM25 keyword search (via ``content_index`` BM25Field) to rank
        turns by lexical relevance to the query. When the assembled result
        contains no records from the composite path, falls back to BM25-only
        retrieval to maximise R@K signal.

        Measures latency of the retrieval call only (not ingestion).
        Maps retrieved Redis keys to session IDs via _session_key_map to
        produce comparable relevant_ids and retrieved_ids.

        Returns:
            ScenarioResult with retrieved_ids as Redis keys and relevant_ids
            as session IDs (from ground truth). Both are in the same key space
            because relevant_ids from the dataset are session IDs and we build
            a session_id -> redis_keys mapping during setup.
        """
        if not self._assembler:
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

        # Primary retrieval: BM25 keyword search on content (issue #395 hybrid path)
        # The model has BM25Field(source="content"), so we can search lexically.
        retrieved_keys: List[str] = []
        retrieval_method = "bm25"
        try:
            bm25_results = BM25Field.search(
                self._model_class,
                "content_index",
                self.item.query,
                limit=20,
            )
            if bm25_results:
                retrieved_keys = [rk for rk, _score in bm25_results]
        except Exception as e:
            logger.warning("BM25 search failed, falling back to assembler: %s", e)
            retrieval_method = "composite_fallback"

        # Fallback: if BM25 produced nothing, use composite assembler
        if not retrieved_keys:
            try:
                assembly_result = self._assembler.assemble(
                    query_cues={"topic": self.item.query},
                    agent_id=self._agent_id,
                )
                retrieved_keys = []
                for record in assembly_result.records:
                    try:
                        retrieved_keys.append(record.db_key.redis_key)
                    except Exception:
                        retrieved_keys.append(str(id(record)))
                retrieval_method = "composite_fallback"
            except Exception as e:
                return ScenarioResult(
                    scenario_name=self.name,
                    status="error",
                    error_message=f"assemble() failed: {e}",
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

        super().teardown()
