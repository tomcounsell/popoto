"""Recipe-layer scenario base class for SubconsciousMemory benchmarks.

Extends the field-level Scenario with multi-turn simulation helpers
and SubconsciousMemory instantiation. Recipe-layer scenarios test
the end-to-end pipeline: inject_context -> LLM response (from fixture)
-> extract_memories -> report_outcomes.
"""

import json
import logging
import uuid
from abc import abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.recipes.subconscious_memory import SubconsciousMemory
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..metrics.extraction import extraction_f1
from ..metrics.token_efficiency import (
    importance_distribution_health,
    token_utilization_ratio,
)
from .base import Scenario, ScenarioResult

logger = logging.getLogger("POPOTO.Benchmark.RecipeScenario")

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _build_recipe_model_class(prefix, overrides):
    """Build a Memory model class for recipe-layer scenarios.

    Creates a model with DecayingSortedField, ConfidenceField, and
    optional WriteFilterMixin. Constructor kwargs are read from
    overrides dict.

    Args:
        prefix: Unique prefix for key isolation.
        overrides: Dict of constant overrides.

    Returns:
        A Popoto Model class configured for benchmarks.
    """
    decay_rate = overrides.get("decay_rate", 0.5)
    initial_confidence = overrides.get("initial_confidence", 0.5)
    wf_min = overrides.get("_wf_min_threshold", 0.2)
    wf_priority = overrides.get("_wf_priority_threshold", 0.7)

    # Use a unique class name to avoid Redis key collisions
    safe_prefix = prefix.replace(":", "").replace("-", "")[:8]

    class RecipeMemory(WriteFilterMixin, popoto.Model):
        _wf_min_threshold = wf_min
        _wf_priority_threshold = wf_priority

        agent_id = popoto.KeyField()
        content = popoto.StringField(default="")
        importance = popoto.FloatField(default=0.5)
        relevance = DecayingSortedField(
            decay_rate=decay_rate, base_score_field="importance"
        )
        certainty = ConfidenceField(initial_confidence=initial_confidence)

        def compute_filter_score(self):
            return self.importance or 0.0

    # Rename the class to include prefix for key isolation
    RecipeMemory.__name__ = f"RecipeMem{safe_prefix}"
    RecipeMemory.__qualname__ = f"RecipeMem{safe_prefix}"

    return RecipeMemory


def load_fixture(filename):
    """Load and validate a fixture JSON file.

    Args:
        filename: Name of the fixture file (e.g., "support_agent.json").

    Returns:
        Dict with fixture data.

    Raises:
        FileNotFoundError: If fixture file does not exist.
        ValueError: If fixture is missing required fields.
    """
    filepath = FIXTURES_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"Fixture not found: {filepath}")

    with open(filepath) as f:
        data = json.load(f)

    # Validate required fields
    required = ["scenario", "turns", "ground_truth"]
    for field in required:
        if field not in data:
            raise ValueError(f"Fixture {filename} missing required field: {field}")

    gt = data["ground_truth"]
    if "sentences" not in gt:
        raise ValueError(f"Fixture {filename} missing ground_truth.sentences")
    if "queries" not in gt:
        raise ValueError(f"Fixture {filename} missing ground_truth.queries")

    return data


class RecipeScenario(Scenario):
    """Base class for recipe-layer SubconsciousMemory benchmarks.

    Subclasses specify the fixture file and scenario-specific ground truth.
    The base class handles:
    - Loading fixture data
    - Building model classes with overrides
    - Creating SubconsciousMemory instances
    - Running multi-turn simulations
    - Computing metrics
    """

    name = "recipe_base"
    fixture_file = ""

    def __init__(self, overrides: Optional[Dict[str, Any]] = None):
        super().__init__(overrides)
        self._agent_id = f"bench_{self._prefix}"
        self._fixture = None
        self._model_class = None
        self._sm = None
        self._saved_memories = []
        self._extraction_results = []

    def setup(self):
        """Load fixture, build model class, create SubconsciousMemory."""
        self._fixture = load_fixture(self.fixture_file)

        self._model_class = _build_recipe_model_class(
            self._prefix, self.overrides
        )

        # Read SubconsciousMemory constructor args from overrides
        max_items = self.overrides.get("max_items", 100)
        max_tokens = self.overrides.get("max_tokens", 40000)
        extraction_min_length = self.overrides.get("extraction_min_length", 10)

        # score_weights: use override if provided, else default
        score_weights = self.overrides.get(
            "score_weights", {"relevance": 0.6, "certainty": 0.4}
        )

        self._sm = SubconsciousMemory(
            model_class=self._model_class,
            agent_id=self._agent_id,
            score_weights=score_weights,
            max_items=max_items,
            max_tokens=max_tokens,
            extraction_min_length=extraction_min_length,
            content_field="content",
            importance_field="importance",
            agent_id_field="agent_id",
        )

    def run(self) -> ScenarioResult:
        """Run multi-turn simulation and compute metrics."""
        if not self._fixture or not self._sm:
            return ScenarioResult(
                status="error",
                error_message="setup() not called or failed",
            )

        turns = self._fixture["turns"]
        importance = self.overrides.get("default_importance", 0.5)

        # Multi-turn simulation
        messages = []
        for turn in turns:
            messages.append({"role": turn["role"], "content": turn["content"]})

            if turn["role"] == "assistant":
                # Pre-turn: inject context (from prior memories)
                try:
                    injected_messages, assembly_result = self._sm.inject_context(
                        list(messages[:-1])  # All messages before this response
                    )
                except Exception as e:
                    logger.warning("inject_context failed: %s", e)
                    assembly_result = None

                # Post-turn: extract memories from assistant response
                try:
                    saved = self._sm.extract_memories(
                        turn["content"], importance=importance
                    )
                    self._saved_memories.extend(saved)
                    self._extraction_results.append(
                        {
                            "turn": len(messages),
                            "extracted_count": len(saved),
                            "texts": [
                                getattr(s, "content", str(s)) for s in saved
                            ],
                        }
                    )
                except Exception as e:
                    logger.warning("extract_memories failed: %s", e)

                # Report outcomes
                if assembly_result and assembly_result.records:
                    try:
                        self._sm.report_outcomes(assembly_result, outcome="acted")
                    except Exception as e:
                        logger.warning("report_outcomes failed: %s", e)

        # Compute metrics
        return self._compute_metrics()

    def _compute_metrics(self) -> ScenarioResult:
        """Compute all metrics from simulation results."""
        gt = self._fixture["ground_truth"]

        # Extraction F1 metric
        all_extracted_texts = []
        for er in self._extraction_results:
            all_extracted_texts.extend(er["texts"])

        ext_metrics = extraction_f1(all_extracted_texts, gt["sentences"])

        # Query-based retrieval metrics
        queries = gt.get("queries", {})
        all_retrieved_ids = []
        all_relevant_ids = set()
        all_relevance_scores = {}

        if queries and self._sm:
            # Run queries against the memory store
            first_query = list(queries.values())[0] if queries else None
            if first_query:
                try:
                    test_messages = [
                        {"role": "user", "content": first_query["text"]}
                    ]
                    _, result = self._sm.inject_context(test_messages)
                    if result and result.records:
                        for record in result.records:
                            try:
                                key = record.db_key.redis_key
                            except Exception:
                                key = str(id(record))
                            all_retrieved_ids.append(key)
                except Exception as e:
                    logger.warning("Query-based retrieval failed: %s", e)

        # Build relevant IDs from saved memories
        sentences = gt["sentences"]
        for memory in self._saved_memories:
            try:
                key = memory.db_key.redis_key
            except Exception:
                key = str(id(memory))
            content = getattr(memory, "content", "")
            imp = getattr(memory, "importance", 0.5) or 0.5
            all_relevance_scores[key] = imp

            # Check if this memory matches an essential sentence
            for sent in sentences:
                if sent["label"] == "meaningful" and sent.get("category") in (
                    "essential",
                    "corroborated",
                ):
                    sent_words = set(sent["text"].lower().split())
                    mem_words = set(content.lower().split())
                    if sent_words and mem_words:
                        overlap = len(sent_words & mem_words) / max(
                            len(sent_words), 1
                        )
                        if overlap >= 0.5:
                            all_relevant_ids.add(key)
                            break

        # Token utilization
        token_metrics = token_utilization_ratio(
            self._saved_memories,
            all_relevance_scores,
            key_fn=lambda r: (
                r.db_key.redis_key
                if hasattr(r, "db_key")
                else str(id(r))
            ),
        )

        # Importance distribution health
        importance_scores = [
            getattr(m, "importance", 0.5) or 0.5 for m in self._saved_memories
        ]
        dist_metrics = importance_distribution_health(importance_scores)

        # Write filter stats
        total_extracted = sum(er["extracted_count"] for er in self._extraction_results)
        noise_count = sum(
            1 for s in sentences if s["label"] == "noise"
        )
        meaningful_count = sum(
            1 for s in sentences if s["label"] == "meaningful"
        )

        return ScenarioResult(
            retrieved_ids=all_retrieved_ids,
            relevant_ids=all_relevant_ids,
            relevance_scores=all_relevance_scores,
            metadata={
                "extraction_f1": ext_metrics["f1"],
                "extraction_precision": ext_metrics["precision"],
                "extraction_recall": ext_metrics["recall"],
                "extraction_true_positives": ext_metrics["true_positives"],
                "extraction_false_positives": ext_metrics["false_positives"],
                "extraction_false_negatives": ext_metrics["false_negatives"],
                "token_utilization_ratio": token_metrics["utilization_ratio"],
                "total_tokens": token_metrics["total_tokens"],
                "importance_std_dev": dist_metrics["std_dev"],
                "importance_distinct_ranks": dist_metrics["distinct_ranks"],
                "importance_rank_ratio": dist_metrics["rank_ratio"],
                "n_memories_saved": len(self._saved_memories),
                "n_turns": len(self._fixture["turns"]),
                "n_extraction_rounds": len(self._extraction_results),
                "total_extracted": total_extracted,
                "ground_truth_meaningful": meaningful_count,
                "ground_truth_noise": noise_count,
            },
        )

    def teardown(self):
        """Clean up all Redis keys from the simulation."""
        # Delete saved memory instances
        for memory in self._saved_memories:
            try:
                memory.delete()
            except Exception:
                pass

        # Scan for any remaining keys with our prefix
        try:
            cursor = 0
            while True:
                cursor, keys = POPOTO_REDIS_DB.scan(
                    cursor, match=f"*{self._prefix}*", count=200
                )
                if keys:
                    POPOTO_REDIS_DB.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

        # Also clean by agent_id pattern
        try:
            cursor = 0
            while True:
                cursor, keys = POPOTO_REDIS_DB.scan(
                    cursor, match=f"*bench_{self._prefix}*", count=200
                )
                if keys:
                    POPOTO_REDIS_DB.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

        # Clean by model class name pattern
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

        super().teardown()
