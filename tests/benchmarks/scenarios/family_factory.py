"""Family-aware scenario generation for constant sensitivity testing.

Each constant family (decay, confidence, write_filter, co_occurrence) gets
dedicated scenario variants that exercise the actual code paths those constants
control. This contrasts with the generic ScenarioFactory which varies data but
uses the same behavioral pathway for all scenarios.

Usage:
    from tests.benchmarks.scenarios.family_factory import FamilyScenarioFactory

    # Get scenarios for a specific constant's family
    scenarios = FamilyScenarioFactory.for_constant("decay_rate")

    # Get all family scenarios
    all_scenarios = FamilyScenarioFactory.create_all()
"""

import random
import time
from dataclasses import dataclass
from typing import List, Type

from src import popoto
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..overrides import apply_overrides
from .base import Scenario, ScenarioResult


# Constant-to-family mapping
CONSTANT_FAMILY_MAP = {
    # Decay family
    "decay_rate": "decay",
    # Confidence family
    "ACTED_CONFIDENCE_SIGNAL": "confidence",
    "CONTRADICTED_CONFIDENCE_SIGNAL": "confidence",
    "INITIAL_CONFIDENCE": "confidence",
    "initial_confidence": "confidence",
    "ACTED_CYCLE_STRENGTHEN_FACTOR": "confidence",
    "DISMISSED_CYCLE_WEAKEN_FACTOR": "confidence",
    "CONTRADICTED_CYCLE_WEAKEN_FACTOR": "confidence",
    "AUTO_DISCHARGE_CONFIDENCE_THRESHOLD": "confidence",
    # Write filter family
    "_wf_min_threshold": "write_filter",
    "_wf_priority_threshold": "write_filter",
    "WF_MIN_THRESHOLD": "write_filter",
    "WF_PRIORITY_THRESHOLD": "write_filter",
    # Co-occurrence family
    "initial_weight": "co_occurrence",
    "decay_per_hop": "co_occurrence",
    "decay_factor": "co_occurrence",
    "CO_OCCURRENCE_INITIAL_WEIGHT": "co_occurrence",
    "CO_OCCURRENCE_DECAY_PER_HOP": "co_occurrence",
    "CO_OCCURRENCE_DECAY_FACTOR": "co_occurrence",
}

FAMILY_NAMES = ("decay", "confidence", "write_filter", "co_occurrence")


@dataclass
class FamilySeed:
    """Seed configuration for a family-specific scenario.

    Attributes:
        seed_id: Unique integer for deterministic RNG seeding.
        family: Which constant family this scenario targets.
        record_count: Number of records to create.
        variant: Variant index within the family (for diversity).
    """

    seed_id: int
    family: str
    record_count: int
    variant: int = 0

    def __post_init__(self):
        assert self.family in FAMILY_NAMES, f"Unknown family: {self.family}"
        assert self.record_count >= 5, "Need at least 5 records"


class DecayFamilyScenario(Scenario):
    """Scenario exercising DECAY_RATE via clustered importance and bimodal age spread.

    Creates records with similar importance (0.45-0.55) so decay is the ranking
    tiebreaker. Half the records were accessed recently, half are months old.
    Ground truth ranks by importance * elapsed_days^(-decay_rate), so the
    "correct" ranking depends on the current decay_rate override value.
    """

    name = "family_decay"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(seed_id=1000, family="decay", record_count=20)

    def setup(self):
        rng = random.Random(self._seed.seed_id)
        decay_rate = self.overrides.get("decay_rate", 0.5)

        class DecayMemory(popoto.Model):
            gen_name = popoto.UniqueKeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=decay_rate, base_score_field="importance"
            )

        self._model_class = DecayMemory
        self._instances = []
        self._importance_values = []
        self._age_days = []

        now = time.time()
        n = self._seed.record_count

        for idx in range(n):
            # Clustered importance: all records between 0.45 and 0.55
            imp = rng.uniform(0.45, 0.55)
            self._importance_values.append(imp)

            instance = DecayMemory(
                gen_name=f"{self._prefix}decay_{idx:04d}",
                content=f"Decay scenario record {idx}",
                importance=imp,
            )
            try:
                instance.save()
                self._instances.append(instance)
                self._created_keys.append(instance.db_key.redis_key)
            except Exception as e:
                return ScenarioResult(status="error", error_message=f"save failed: {e}")

            # Bimodal age: half recent (0-1 day), half old (60-180 days)
            if idx < n // 2:
                age_days = rng.uniform(0.01, 1.0)
            else:
                age_days = rng.uniform(60, 180)
            self._age_days.append(age_days)

            # Manipulate sorted set timestamp to simulate age
            age_seconds = age_days * 86400
            old_time = now - age_seconds
            ss_key = f"{instance.db_key.redis_key}:relevance"
            try:
                POPOTO_REDIS_DB.zadd(ss_key, {instance.db_key.redis_key: old_time})
            except Exception as e:
                return ScenarioResult(
                    status="error", error_message=f"zadd age manipulation failed: {e}"
                )

    def run(self) -> ScenarioResult:
        if len(self._instances) < 3:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message=f"Only {len(self._instances)} instances (need >= 3)",
            )

        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 1.0},
                    limit=min(10, len(self._instances)),
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

        # Ground truth: rank by importance * elapsed_days^(-decay_rate)
        # This makes the ranking depend on the decay_rate override
        decay_rate = self.overrides.get("decay_rate", 0.5)

        relevance_scores = {}
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            imp = self._importance_values[i]
            age_days = self._age_days[i]
            # Power-law decay: importance * age^(-decay_rate)
            # Clamp age to avoid division by zero
            effective_age = max(age_days, 0.01)
            decayed_score = imp * (effective_age ** (-decay_rate))
            relevance_scores[key] = decayed_score

        # Top 30% by decayed score are relevant
        sorted_by_score = sorted(
            relevance_scores.items(), key=lambda x: x[1], reverse=True
        )
        n_relevant = max(3, len(sorted_by_score) // 3)
        relevant_ids = {k for k, _ in sorted_by_score[:n_relevant]}

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            metadata={
                "family": "decay",
                "seed_id": self._seed.seed_id,
                "n_instances": len(self._instances),
                "decay_rate": decay_rate,
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class") and hasattr(self, "_instances"):
            for inst in self._instances:
                try:
                    inst.delete()
                except Exception:
                    pass
        super().teardown()


class ConfidenceFamilyScenario(Scenario):
    """Scenario exercising confidence constants via multi-round mixed outcomes.

    Creates records and runs 5 rounds of on_context_used() with mixed outcomes:
    high-importance records get "acted", low-importance get "contradicted",
    mid-range get alternating outcomes. Queries with certainty-dominated weights
    so confidence differences drive the ranking.
    """

    name = "family_confidence"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=2000, family="confidence", record_count=20
        )

    def setup(self):
        initial_confidence = self.overrides.get("initial_confidence", 0.5)

        class ConfMemory(popoto.Model):
            gen_name = popoto.UniqueKeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.1, base_score_field="importance"
            )
            certainty = ConfidenceField(initial_confidence=initial_confidence)

        self._model_class = ConfMemory
        self._instances = []
        self._importance_values = []
        self._outcome_sequences = []

        n = self._seed.record_count

        # Create records with spread importance for outcome assignment
        for idx in range(n):
            imp = 0.15 + (0.80 * idx / max(n - 1, 1))
            self._importance_values.append(imp)

            instance = ConfMemory(
                gen_name=f"{self._prefix}conf_{idx:04d}",
                content=f"Confidence scenario record {idx}",
                importance=imp,
            )
            try:
                instance.save()
                self._instances.append(instance)
                self._created_keys.append(instance.db_key.redis_key)
            except Exception as e:
                return ScenarioResult(status="error", error_message=f"save failed: {e}")

            # Assign outcome sequence based on importance tier
            if imp >= 0.7:
                # High importance: mostly acted
                seq = ["acted"] * 5
            elif imp >= 0.5:
                # Medium-high: mostly acted with some dismissed
                seq = ["acted", "acted", "dismissed", "acted", "acted"]
            elif imp >= 0.3:
                # Medium: mixed outcomes
                seq = ["acted", "dismissed", "contradicted", "acted", "dismissed"]
            else:
                # Low: mostly contradicted
                seq = [
                    "contradicted",
                    "contradicted",
                    "dismissed",
                    "contradicted",
                    "contradicted",
                ]
            self._outcome_sequences.append(seq)

        if not self._instances:
            return

        # Run 5 rounds of on_context_used with mixed outcomes
        for round_idx in range(5):
            outcome_map = {}
            for i, inst in enumerate(self._instances):
                key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                outcome_map[key] = self._outcome_sequences[i][round_idx]

            with apply_overrides(self.overrides):
                try:
                    ObservationProtocol.on_context_used(self._instances, outcome_map)
                except Exception as e:
                    return ScenarioResult(
                        status="error",
                        error_message=f"on_context_used round {round_idx} failed: {e}",
                    )

    def run(self) -> ScenarioResult:
        if len(self._instances) < 3:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message=f"Only {len(self._instances)} instances (need >= 3)",
            )

        # Query with certainty-dominated weights
        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 0.3, "certainty": 0.7},
                    limit=min(10, len(self._instances)),
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

        # Ground truth: records with mostly "acted" outcomes should rank highest
        # Use the outcome sequences to compute expected confidence ranking
        relevance_scores = {}
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            seq = self._outcome_sequences[i]
            # Score based on fraction of positive outcomes
            acted_count = seq.count("acted")
            contradicted_count = seq.count("contradicted")
            score = (acted_count - contradicted_count * 0.5) / len(seq)
            # Combine with small importance component
            relevance_scores[key] = score * 0.7 + self._importance_values[i] * 0.3

        sorted_by_score = sorted(
            relevance_scores.items(), key=lambda x: x[1], reverse=True
        )
        n_relevant = max(3, len(sorted_by_score) // 3)
        relevant_ids = {k for k, _ in sorted_by_score[:n_relevant]}

        # Calibration data from actual confidence values
        predictions = []
        outcomes = []
        for i, inst in enumerate(self._instances):
            try:
                conf = ConfidenceField.get_confidence(inst, "certainty")
                predictions.append(conf)
                outcomes.append(self._outcome_sequences[i].count("acted") >= 3)
            except Exception:
                pass

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            predictions=predictions,
            outcomes=outcomes,
            metadata={
                "family": "confidence",
                "seed_id": self._seed.seed_id,
                "n_instances": len(self._instances),
                "n_rounds": 5,
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class") and hasattr(self, "_instances"):
            for inst in self._instances:
                try:
                    inst.delete()
                except Exception:
                    pass
        super().teardown()


class WriteFilterFamilyScenario(Scenario):
    """Scenario exercising WF_MIN_THRESHOLD via pre-filter ground truth.

    Creates records spanning importance 0.1-0.9, defines ground truth from
    the full intended set (top 30% by importance), then saves through the
    write filter. Measures retrieval quality against the full intended set,
    not just survivors. When threshold is high, relevant records get filtered
    out and nDCG drops.
    """

    name = "family_write_filter"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=3000, family="write_filter", record_count=30
        )

    def setup(self):
        rng = random.Random(self._seed.seed_id)
        wf_min = self.overrides.get("_wf_min_threshold", 0.2)
        wf_priority = self.overrides.get("_wf_priority_threshold", 0.7)

        class WFMemory(WriteFilterMixin, popoto.Model):
            _wf_min_threshold = wf_min
            _wf_priority_threshold = wf_priority

            gen_name = popoto.UniqueKeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.1, base_score_field="importance"
            )
            certainty = ConfidenceField(initial_confidence=0.5)

            def compute_filter_score(self):
                return self.importance or 0.0

        self._model_class = WFMemory
        self._instances = []
        self._all_importance_values = []
        self._all_keys = []
        self._instance_idx_map = {}

        n = self._seed.record_count

        # Generate importance values with a clustered mid-range distribution.
        # Many records near the threshold zone (0.15-0.45), with some noise
        # below and some anchors above.
        for idx in range(n):
            base_imp = 0.05 + (0.80 * idx / max(n - 1, 1))
            imp = base_imp + rng.uniform(-0.02, 0.02)
            imp = max(0.01, min(0.99, imp))
            self._all_importance_values.append(imp)

        # Create records -- some will be filtered by WriteFilterMixin
        for idx in range(n):
            imp = self._all_importance_values[idx]
            planned_key = f"{self._prefix}wf_{idx:04d}"
            self._all_keys.append(planned_key)

            instance = WFMemory(
                gen_name=planned_key,
                content=f"Write filter scenario record {idx}",
                importance=imp,
            )
            result = instance.save()
            if result and result is not False:
                # Record survived the write filter
                self._instances.append(instance)
                self._created_keys.append(instance.db_key.redis_key)
                self._instance_idx_map[instance.db_key.redis_key] = idx
            # else: filtered by WriteFilterMixin (save returns False)

        if not self._instances:
            return

        # Run confidence updates on surviving mid-importance records to make
        # them compete with high-importance records for top-K spots.
        # Records with importance 0.2-0.5 get "acted" outcomes to boost
        # their certainty, making them rank higher in certainty-weighted
        # composite queries. When the threshold filters them out, the
        # composite ranking changes.
        for inst in self._instances:
            imp = inst.importance or 0
            if 0.15 <= imp <= 0.50:
                # Boost confidence for mid-importance records
                for boost_round in range(4):
                    try:
                        ConfidenceField.update_confidence(
                            inst, "certainty", signal=0.95
                        )
                    except Exception as e:
                        return ScenarioResult(
                            status="error",
                            error_message=f"confidence boost failed round {boost_round}: {e}",
                        )

    def run(self) -> ScenarioResult:
        if len(self._instances) < 3:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message=f"Only {len(self._instances)} instances (need >= 3)",
            )

        # Query with certainty-weighted composite. Mid-importance records
        # with boosted confidence can outrank high-importance records.
        # When the threshold filters out those mid-importance records,
        # the ranking changes, producing nDCG variance.
        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 0.4, "certainty": 0.6},
                    limit=min(10, len(self._instances)),
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

        # Ground truth: top records by importance (from the full intended
        # set). Records that were filtered cannot be retrieved, penalizing
        # precision. Additionally, the certainty-boosted mid-importance
        # records change the retrieval ranking depending on whether they
        # survive the filter.
        relevance_scores = {}
        relevant_ids = set()

        # Top 30% by importance from full set are the relevant set
        sorted_indices = sorted(
            range(self._seed.record_count),
            key=lambda i: self._all_importance_values[i],
            reverse=True,
        )
        n_relevant = max(3, self._seed.record_count // 3)
        relevant_indices = set(sorted_indices[:n_relevant])

        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            idx = self._instance_idx_map.get(key)
            imp = inst.importance or 0
            relevance_scores[key] = imp
            if idx is not None and idx in relevant_indices:
                relevant_ids.add(key)

        # Count how many relevant records survived the write filter
        # (relevant_ids only contains survivors, so its size is n_relevant_survived)
        n_relevant_survived = len(relevant_ids)

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            metadata={
                "family": "write_filter",
                "seed_id": self._seed.seed_id,
                "n_planned": self._seed.record_count,
                "n_survived": len(self._instances),
                "n_relevant_planned": n_relevant,
                "n_relevant_survived": n_relevant_survived,
                "wf_min_threshold": self.overrides.get("_wf_min_threshold", 0.2),
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class") and hasattr(self, "_instances"):
            for inst in self._instances:
                try:
                    inst.delete()
                except Exception:
                    pass
        super().teardown()


class CoOccurrenceFamilyScenario(Scenario):
    """Scenario exercising co-occurrence constants via real graph propagation.

    Creates 3 topic clusters with inter-cluster links. Uses CoOccurrenceField.link()
    for real edges and propagate() for BFS scores. Passes propagation scores as
    co_occurrence_boost to composite_score(). Ground truth: records reachable from
    seed cluster via links should rank higher.
    """

    name = "family_co_occurrence"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=4000, family="co_occurrence", record_count=24
        )

    def setup(self):
        rng = random.Random(self._seed.seed_id)

        class CoocMemory(popoto.Model):
            gen_name = popoto.UniqueKeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.1, base_score_field="importance"
            )
            associations = CoOccurrenceField(symmetric=True, max_edges=100)

        self._model_class = CoocMemory
        self._instances = []
        self._clusters = {"seed": [], "hop1": [], "hop2": []}

        n = self._seed.record_count
        cluster_size = n // 3

        # Create 3 clusters of records
        for cluster_idx, cluster_name in enumerate(["seed", "hop1", "hop2"]):
            for j in range(cluster_size):
                idx = cluster_idx * cluster_size + j
                # All records have similar importance so co-occurrence boost is the differentiator
                imp = rng.uniform(0.45, 0.55)

                instance = CoocMemory(
                    gen_name=f"{self._prefix}cooc_{idx:04d}",
                    content=f"Co-occurrence {cluster_name} record {j}",
                    importance=imp,
                )
                try:
                    instance.save()
                    self._instances.append(instance)
                    self._clusters[cluster_name].append(instance)
                    self._created_keys.append(instance.db_key.redis_key)
                except Exception as e:
                    return ScenarioResult(
                        status="error", error_message=f"save failed: {e}"
                    )

        if not self._instances:
            return

        # Get the CoOccurrenceField instance from the model
        assoc_field = CoocMemory._meta.fields.get("associations")
        if not assoc_field or not isinstance(assoc_field, CoOccurrenceField):
            return

        # Create inter-cluster links using real CoOccurrenceField API
        initial_weight = self.overrides.get(
            "initial_weight",
            self.overrides.get("CO_OCCURRENCE_INITIAL_WEIGHT", 0.1),
        )

        # Link seed <-> hop1 (direct connections)
        for seed_inst in self._clusters["seed"]:
            for hop1_inst in self._clusters["hop1"][:3]:
                seed_pk = seed_inst.db_key.redis_key
                hop1_pk = hop1_inst.db_key.redis_key
                try:
                    assoc_field.link(
                        CoocMemory,
                        seed_pk,
                        hop1_pk,
                        initial_weight=initial_weight,
                    )
                except Exception as e:
                    return ScenarioResult(
                        status="error",
                        error_message=f"link seed->hop1 failed: {e}",
                    )

        # Link hop1 <-> hop2 (one more hop away)
        for hop1_inst in self._clusters["hop1"]:
            for hop2_inst in self._clusters["hop2"][:3]:
                hop1_pk = hop1_inst.db_key.redis_key
                hop2_pk = hop2_inst.db_key.redis_key
                try:
                    assoc_field.link(
                        CoocMemory,
                        hop1_pk,
                        hop2_pk,
                        initial_weight=initial_weight,
                    )
                except Exception as e:
                    return ScenarioResult(
                        status="error",
                        error_message=f"link hop1->hop2 failed: {e}",
                    )

        self._assoc_field = assoc_field

    def run(self) -> ScenarioResult:
        if len(self._instances) < 3:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message=f"Only {len(self._instances)} instances (need >= 3)",
            )

        if not hasattr(self, "_assoc_field"):
            return ScenarioResult(
                status="error",
                error_message="CoOccurrenceField not found on model",
            )

        # Propagate from seed cluster
        seed_pks = [inst.db_key.redis_key for inst in self._clusters.get("seed", [])]

        if not seed_pks:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message="No seed instances for propagation",
            )

        decay_per_hop = self.overrides.get(
            "decay_per_hop",
            self.overrides.get("CO_OCCURRENCE_DECAY_PER_HOP", 0.5),
        )

        with apply_overrides(self.overrides):
            try:
                boost_scores = self._assoc_field.propagate(
                    self._model_class,
                    seed_pks,
                    depth=2,
                    decay_per_hop=decay_per_hop,
                )
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"propagate failed: {e}",
                )

            if not boost_scores:
                return ScenarioResult(
                    status="skipped-degenerate",
                    error_message="propagate returned empty scores",
                )

            try:
                results = self._model_class.query.composite_score(
                    indexes={"relevance": 0.3},
                    co_occurrence_boost=boost_scores,
                    limit=min(10, len(self._instances)),
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

        # Ground truth: hop1 records (directly linked) should rank highest,
        # hop2 records (2 hops) should rank next, unlinked records last
        relevance_scores = {}
        relevant_ids = set()

        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            if inst in self._clusters.get("hop1", []):
                relevance_scores[key] = 1.0
                relevant_ids.add(key)
            elif inst in self._clusters.get("hop2", []):
                relevance_scores[key] = 0.5
                relevant_ids.add(key)
            elif inst in self._clusters.get("seed", []):
                relevance_scores[key] = 0.1  # Seeds are starting points, not targets
            else:
                relevance_scores[key] = 0.0

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            metadata={
                "family": "co_occurrence",
                "seed_id": self._seed.seed_id,
                "n_instances": len(self._instances),
                "n_seed": len(self._clusters.get("seed", [])),
                "n_hop1": len(self._clusters.get("hop1", [])),
                "n_hop2": len(self._clusters.get("hop2", [])),
                "n_boost_scores": len(boost_scores),
                "decay_per_hop": decay_per_hop,
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class") and hasattr(self, "_instances"):
            for inst in self._instances:
                try:
                    inst.delete()
                except Exception:
                    pass
        super().teardown()


# Map family names to scenario classes
FAMILY_SCENARIO_CLASSES = {
    "decay": DecayFamilyScenario,
    "confidence": ConfidenceFamilyScenario,
    "write_filter": WriteFilterFamilyScenario,
    "co_occurrence": CoOccurrenceFamilyScenario,
}


class FamilyScenarioFactory:
    """Generate family-specific scenario variants for constant sensitivity testing.

    Each constant family gets dedicated scenarios that exercise its code paths.
    The factory maps constant names to families and creates appropriate scenarios.
    """

    @staticmethod
    def for_constant(constant_name: str) -> List[Type[Scenario]]:
        """Get scenario classes targeting the family for a given constant.

        Args:
            constant_name: Name of the constant being swept.

        Returns:
            List of Scenario subclasses that exercise the constant's family.
            Falls back to all family scenarios if the constant is not mapped.
        """
        family = CONSTANT_FAMILY_MAP.get(constant_name)
        if family and family in FAMILY_SCENARIO_CLASSES:
            return [FAMILY_SCENARIO_CLASSES[family]]
        # Unknown constant: return all family scenarios
        return list(FAMILY_SCENARIO_CLASSES.values())

    @staticmethod
    def create_all() -> List[Type[Scenario]]:
        """Create one scenario class per family.

        Returns:
            List of all family scenario classes.
        """
        return list(FAMILY_SCENARIO_CLASSES.values())

    @staticmethod
    def create_varied(n_per_family: int = 3) -> List[Type[Scenario]]:
        """Create multiple scenario variants per family for diversity.

        Each variant uses a different seed for slightly different data
        while exercising the same code paths.

        Args:
            n_per_family: Number of variants per family. Default 3.

        Returns:
            List of Scenario subclasses (n_per_family * 4 total).
        """
        scenarios = []

        for family_name, scenario_class in FAMILY_SCENARIO_CLASSES.items():
            for variant in range(n_per_family):
                base_seed = {
                    "decay": 1000,
                    "confidence": 2000,
                    "write_filter": 3000,
                    "co_occurrence": 4000,
                }[family_name]

                seed = FamilySeed(
                    seed_id=base_seed + variant * 100,
                    family=family_name,
                    record_count={
                        "decay": 20,
                        "confidence": 20,
                        "write_filter": 30,
                        "co_occurrence": 24,
                    }[family_name],
                    variant=variant,
                )

                # Create a unique subclass per variant.
                # Use default parameter trick to capture loop variables by value,
                # avoiding late-binding closure bug where all variants share the
                # last iteration's values.
                variant_name = f"family_{family_name}_v{variant}"

                def _make_variant(cls=scenario_class, s=seed, vname=variant_name):
                    class VariantScenario(cls):
                        name = vname

                        def __init__(self, overrides=None, _bound_seed=s):
                            super().__init__(overrides=overrides, seed=_bound_seed)

                    return VariantScenario

                scenarios.append(_make_variant())

        return scenarios
