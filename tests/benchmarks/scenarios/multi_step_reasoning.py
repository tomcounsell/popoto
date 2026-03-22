"""Multi-Step Reasoning scenario — retrieval with co-occurrence propagation.

Simulates an agent that builds chains of related knowledge. Tests whether
co-occurrence edges and confidence signals help retrieve the right chain
of facts for multi-step reasoning.

Ground truth: items connected in a reasoning chain should be retrieved together.
Query: composite_score + co-occurrence propagation from seed items.
"""

from src import popoto
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..overrides import apply_overrides
from .base import Scenario, ScenarioResult


def _build_model_class(overrides):
    """Build a ReasoningMemory model class with optional field overrides."""
    decay_rate = overrides.get("decay_rate", 0.5)
    initial_confidence = overrides.get("initial_confidence", 0.5)
    decay_factor = overrides.get("decay_factor", 0.95)

    class ReasoningMemory(popoto.Model):
        name = popoto.UniqueKeyField()
        content = popoto.StringField(default="")
        importance = popoto.FloatField(default=0.5)
        relevance = DecayingSortedField(
            decay_rate=decay_rate, base_score_field="importance"
        )
        certainty = ConfidenceField(initial_confidence=initial_confidence)
        associations = CoOccurrenceField(
            symmetric=True, max_edges=100, decay_factor=decay_factor
        )

    return ReasoningMemory


# Reasoning chain: A -> B -> C -> D (each step builds on the previous)
# Distractors: X, Y, Z (unrelated items)
CHAIN_ITEMS = [
    ("step_a", "Premise: All mammals breathe air", 0.8),
    ("step_b", "Whales are mammals", 0.75),
    ("step_c", "Therefore whales breathe air", 0.7),
    ("step_d", "Whales surface to breathe despite living in water", 0.65),
]

DISTRACTOR_ITEMS = [
    ("distractor_x", "Python is a programming language", 0.6),
    ("distractor_y", "The moon orbits Earth", 0.55),
    ("distractor_z", "Chess has 64 squares", 0.5),
    ("distractor_w", "Coffee contains caffeine", 0.45),
    ("distractor_v", "Salt is sodium chloride", 0.4),
]


class MultiStepReasoningScenario(Scenario):
    """Retrieve connected reasoning chains via co-occurrence propagation."""

    name = "multi_step_reasoning"

    def setup(self):
        self._model_class = _build_model_class(self.overrides)
        self._chain_instances = []
        self._distractor_instances = []
        initial_weight = self.overrides.get("initial_weight", 0.1)
        delta = self.overrides.get("delta", 0.05)

        # Create chain items
        for item_name, content, importance in CHAIN_ITEMS:
            instance = self._model_class(
                name=f"{self._prefix}{item_name}",
                content=content,
                importance=importance,
            )
            instance.save()
            self._chain_instances.append(instance)
            self._created_keys.append(instance.db_key.redis_key)

        # Create distractor items
        for item_name, content, importance in DISTRACTOR_ITEMS:
            instance = self._model_class(
                name=f"{self._prefix}{item_name}",
                content=content,
                importance=importance,
            )
            instance.save()
            self._distractor_instances.append(instance)
            self._created_keys.append(instance.db_key.redis_key)

        # Link chain items via co-occurrence (A-B, B-C, C-D)
        field = self._model_class.associations
        for i in range(len(self._chain_instances) - 1):
            src = self._chain_instances[i]
            tgt = self._chain_instances[i + 1]
            src_pk = src.db_key.redis_key
            tgt_pk = tgt.db_key.redis_key
            field.link(
                self._model_class,
                src_pk,
                tgt_pk,
                initial_weight=initial_weight,
            )
            # Strengthen the link to simulate repeated co-occurrence
            for _ in range(3):
                field.strengthen(self._model_class, src_pk, tgt_pk, delta=delta)

        # Simulate acted on step_a (the seed for retrieval)
        if self._chain_instances:
            seed = self._chain_instances[0]
            key = getattr(seed, "_redis_key", None) or seed.db_key.redis_key
            with apply_overrides(self.overrides):
                ObservationProtocol.on_context_used([seed], {key: "acted"})

    def run(self) -> ScenarioResult:
        all_instances = self._chain_instances + self._distractor_instances
        if not all_instances:
            return ScenarioResult(
                status="error",
                error_message="No instances created",
            )

        # Step 1: Get co-occurrence neighbors from seed (step_a)
        seed = self._chain_instances[0]
        seed_pk = seed.db_key.redis_key
        decay_per_hop = self.overrides.get("decay_per_hop", 0.5)

        field = self._model_class.associations
        propagated = field.propagate(
            self._model_class,
            [seed_pk],
            depth=2,
            decay_per_hop=decay_per_hop,
        )

        # Step 2: Query via composite_score
        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 0.5, "certainty": 0.5},
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

        # Ground truth: chain items are relevant, distractors are not
        relevant_ids = set()
        relevance_scores = {}
        for inst in self._chain_instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            relevant_ids.add(key)
            relevance_scores[key] = 1.0

        for inst in self._distractor_instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            relevance_scores[key] = 0.0

        # Calibration: confidence should be higher for chain items
        predictions = []
        outcomes = []
        for inst in all_instances:
            try:
                conf = ConfidenceField.get_confidence(inst, "certainty")
                predictions.append(conf)
                key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                outcomes.append(key in relevant_ids)
            except Exception:
                pass

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            predictions=predictions,
            outcomes=outcomes,
            metadata={
                "n_chain": len(self._chain_instances),
                "n_distractors": len(self._distractor_instances),
                "n_propagated": len(propagated),
                "propagated_pks": dict(propagated),
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class"):
            all_inst = getattr(self, "_chain_instances", []) + getattr(
                self, "_distractor_instances", []
            )
            for inst in all_inst:
                try:
                    inst.delete()
                except Exception:
                    pass
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
