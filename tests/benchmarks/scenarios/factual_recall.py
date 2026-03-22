"""Factual Recall scenario — retrieval of known facts by relevance.

Simulates an agent storing facts with varying importance, then retrieving
them via composite_score. Measures whether the correct high-importance
facts are retrieved first.

Ground truth: facts with importance >= 0.7 are "relevant".
Query: composite_score with decay + confidence weights.
"""

import time

from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..overrides import apply_overrides
from .base import Scenario, ScenarioResult


def _build_model_class(overrides):
    """Build a Memory model class with optional field overrides."""
    decay_rate = overrides.get("decay_rate", 0.5)
    initial_confidence = overrides.get("initial_confidence", 0.5)
    wf_min = overrides.get("_wf_min_threshold", 0.2)
    wf_priority = overrides.get("_wf_priority_threshold", 0.7)

    class FactualMemory(WriteFilterMixin, popoto.Model):
        _wf_min_threshold = wf_min
        _wf_priority_threshold = wf_priority

        name = popoto.UniqueKeyField()
        content = popoto.StringField(default="")
        importance = popoto.FloatField(default=0.5)
        relevance = DecayingSortedField(
            decay_rate=decay_rate, base_score_field="importance"
        )
        certainty = ConfidenceField(initial_confidence=initial_confidence)

        def compute_filter_score(self):
            return self.importance or 0.0

    return FactualMemory


# Factual data: (name, content, importance)
FACTS = [
    ("capital_france", "The capital of France is Paris", 0.9),
    ("capital_germany", "The capital of Germany is Berlin", 0.85),
    ("capital_japan", "The capital of Japan is Tokyo", 0.8),
    ("water_boiling", "Water boils at 100 degrees Celsius", 0.75),
    ("earth_sun", "Earth orbits the Sun", 0.7),
    ("pi_value", "Pi is approximately 3.14159", 0.6),
    ("speed_light", "Speed of light is 3e8 m/s", 0.55),
    ("gravity_accel", "Gravitational acceleration is 9.8 m/s2", 0.5),
    ("random_trivia1", "The longest river is the Nile", 0.3),
    ("random_trivia2", "Honey never spoils", 0.25),
    ("random_trivia3", "Octopi have three hearts", 0.2),
    ("noise1", "Some filler text here", 0.1),
    ("noise2", "More filler content", 0.05),
]


class FactualRecallScenario(Scenario):
    """Retrieve factual knowledge ranked by importance and confidence."""

    name = "factual_recall"

    def setup(self):
        self._model_class = _build_model_class(self.overrides)
        self._instances = []

        for fact_name, content, importance in FACTS:
            # Only create if above write filter threshold
            instance = self._model_class(
                name=f"{self._prefix}{fact_name}",
                content=content,
                importance=importance,
            )
            try:
                instance.save()
                self._instances.append(instance)
                self._created_keys.append(instance.db_key.redis_key)
            except Exception:
                pass  # Filtered by WriteFilterMixin

        # Simulate acted outcomes on high-importance facts (corroborates confidence)
        acted_instances = [i for i in self._instances if (i.importance or 0) >= 0.7]
        if acted_instances:
            outcome_map = {}
            for inst in acted_instances:
                key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                outcome_map[key] = "acted"
            with apply_overrides(self.overrides):
                ObservationProtocol.on_context_used(acted_instances, outcome_map)

    def run(self) -> ScenarioResult:
        if not self._instances:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message="No instances survived write filter",
            )

        # Query via composite_score
        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 0.6, "certainty": 0.4},
                    limit=10,
                )
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"composite_score failed: {e}",
                )

        retrieved_ids = []
        for r in results:
            key = getattr(r, "_redis_key", None) or r.db_key.redis_key
            retrieved_ids.append(key)

        # Ground truth: facts with importance >= 0.7
        relevant_ids = set()
        relevance_scores = {}
        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            imp = inst.importance or 0
            relevance_scores[key] = imp
            if imp >= 0.7:
                relevant_ids.add(key)

        # Calibration: confidence should correlate with importance
        predictions = []
        outcomes = []
        for inst in self._instances:
            try:
                conf = ConfidenceField.get_confidence(inst, "certainty")
                predictions.append(conf)
                outcomes.append((inst.importance or 0) >= 0.7)
            except Exception:
                pass

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            predictions=predictions,
            outcomes=outcomes,
            metadata={
                "n_instances": len(self._instances),
                "n_relevant": len(relevant_ids),
                "n_retrieved": len(retrieved_ids),
            },
        )

    def teardown(self):
        """Clean up all model keys."""
        if hasattr(self, "_model_class"):
            try:
                for inst in self._instances:
                    inst.delete()
            except Exception:
                pass
            # Clean up sorted sets, companion hashes, etc.
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
        super().teardown()
