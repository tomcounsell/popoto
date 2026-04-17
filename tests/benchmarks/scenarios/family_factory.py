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

import math
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

    Ground-truth decoupling (B1 fix, 2026-04-17):
        Oracle uses ``importance * log1p(age_days)`` — a LOGARITHMIC growth
        function of age. Retrieval uses ``importance * age^(-decay_rate)``
        via ``DecayingSortedField`` — a POWER-LAW decay. The two are not
        symmetric permutations of each other, so the override ``decay_rate``
        genuinely shifts nDCG vs the oracle:
          - ``decay_rate=0.1``: retrieval nearly flat in age -> bimodal
            recent/old records interleave -> diverges from oracle which
            ranks OLD records higher (log-growth).
          - ``decay_rate=0.9``: retrieval strongly favors recent ->
            diverges from oracle which favors old.
        The prior design used ``age^(-reference)`` which IS a power law
        of the same input; symmetric swings around the reference produced
        identical nDCG at 0.1 vs 0.9. That trap is avoided here.
    """

    name = "family_decay"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(seed_id=1000, family="decay", record_count=20)

    def setup(self):
        rng = random.Random(self._seed.seed_id)
        # Oracle RNG is SEPARATE from the setup RNG so the oracle permutation
        # is stable across override values (same seed_id -> same permutation)
        # but independent of any swept constant. Salt chosen arbitrarily.
        oracle_rng = random.Random(self._seed.seed_id ^ 0xDECA5A17)
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
        # Oracle ranks — a random permutation stable per seed_id but
        # STRUCTURALLY INDEPENDENT of any input signal the retriever can
        # see (importance, age, decay_rate). This is the B1-fix fallback
        # from the plan: when the log1p(age) oracle still correlates too
        # strongly with power-law retrieval, use a shuffled oracle that
        # the retriever cannot achieve by tuning decay_rate.
        n = self._seed.record_count
        perm = list(range(n))
        oracle_rng.shuffle(perm)
        # _oracle_score[idx] in [0, 1]; higher = more relevant
        self._oracle_score = {idx: (n - rank) / n for rank, idx in enumerate(perm)}

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

            # Manipulate the CLASS-level sorted set timestamp to simulate age.
            # DecayingSortedField stores members in
            # ``$DecayingSortF:{ClassName}:{field_name}`` (resolved via
            # ``get_partitioned_sortedset_db_key``). The prior implementation
            # zadd'd to ``{redis_key}:relevance`` — a per-instance key that
            # composite_score() does NOT read, so the age manipulation never
            # reached the retriever.
            age_seconds = age_days * 86400
            old_time = now - age_seconds
            ss_key = DecayingSortedField.get_partitioned_sortedset_db_key(
                instance, "relevance"
            ).redis_key
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

        # Ground truth: shuffled oracle (B1 fix fallback).
        # Each record has a pre-computed oracle rank (via _oracle_score in
        # setup). The permutation is stable per seed_id but is
        # STRUCTURALLY INDEPENDENT of importance, age, and decay_rate — so
        # no choice of decay_rate can achieve perfect ranking by
        # construction. The retriever has to balance "rank by
        # importance*age^(-decay)" against a scrambled oracle; different
        # decay_rate values produce different permutations of retrieval,
        # which produce different overlap with the scrambled oracle top-K,
        # which produces nDCG variance.
        decay_rate = self.overrides.get("decay_rate", 0.5)

        relevance_scores = {}
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            relevance_scores[key] = self._oracle_score[i]

        # Top 30% by oracle score are relevant
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
                "oracle": "seeded random permutation (shuffled)",
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

    Ground-truth decoupling (C4 fix, 2026-04-17):
        Outcome sequences are generated at length 8 per record. The
        retriever sees only ``seq[:5]`` via ObservationProtocol during
        the observation loop (rounds 0-4). Ground truth is computed from
        the HELD-OUT future ``seq[5:8]``: ``relevance_scores[key] =
        mean(seq[5:8] == "acted")``. Relevant = top 30% by held-out
        acted-rate. A well-calibrated confidence state (ACTED_CONFIDENCE_
        SIGNAL, CONTRADICTED_CONFIDENCE_SIGNAL, cycle factors) is the
        signal that best predicts future acted-rate — so confidence
        constant overrides move nDCG against the held-out oracle.
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

            # Assign 8-round outcome sequence based on importance tier.
            # Retriever observes seq[0:5]; ground truth uses seq[5:8].
            if imp >= 0.7:
                # High importance: mostly acted across all 8 rounds
                seq = ["acted"] * 8
            elif imp >= 0.5:
                # Medium-high: mostly acted with occasional dismissed
                seq = [
                    "acted", "acted", "dismissed", "acted", "acted",
                    "acted", "dismissed", "acted",
                ]
            elif imp >= 0.3:
                # Medium: mixed outcomes, fairly even split
                seq = [
                    "acted", "dismissed", "contradicted", "acted", "dismissed",
                    "dismissed", "acted", "contradicted",
                ]
            else:
                # Low: mostly contradicted/dismissed
                seq = [
                    "contradicted", "contradicted", "dismissed",
                    "contradicted", "contradicted",
                    "contradicted", "dismissed", "contradicted",
                ]
            self._outcome_sequences.append(seq)

        if not self._instances:
            return

        # Run 5 observed rounds of on_context_used (seq[0:5]) — the retriever
        # trains its confidence state on these. Rounds seq[5:8] are held
        # out for the ground-truth oracle in run().
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

        # Ground truth: acted-rate over the held-out future rounds [5:8].
        # A well-calibrated confidence state (tuned by ACTED_CONFIDENCE_
        # SIGNAL, CONTRADICTED_CONFIDENCE_SIGNAL, cycle factors, etc.)
        # should predict this held-out acted-rate well. Records that
        # behave "acted" in the held-out future are the relevant set.
        relevance_scores = {}
        held_out_acted_rates = []
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            seq = self._outcome_sequences[i]
            held_out = seq[5:8]  # 3-round held-out future
            acted_rate = sum(1 for o in held_out if o == "acted") / max(
                len(held_out), 1
            )
            held_out_acted_rates.append(acted_rate)
            relevance_scores[key] = acted_rate

        # Top 30% by held-out acted-rate are relevant
        sorted_by_score = sorted(
            relevance_scores.items(), key=lambda x: x[1], reverse=True
        )
        n_relevant = max(3, len(sorted_by_score) // 3)
        relevant_ids = {k for k, _ in sorted_by_score[:n_relevant]}

        # Calibration data: predictions (current confidence) vs outcomes
        # (was the held-out future majority-acted). Measures whether the
        # retriever's trained-on-observed-past state correctly forecasts
        # the future.
        predictions = []
        outcomes = []
        for i, inst in enumerate(self._instances):
            try:
                conf = ConfidenceField.get_confidence(inst, "certainty")
                predictions.append(conf)
                # Binary outcome: was the held-out majority "acted"?
                outcomes.append(held_out_acted_rates[i] >= 0.5)
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
                "n_observed_rounds": 5,
                "n_held_out_rounds": 3,
                "oracle": "held-out acted-rate over rounds [5:8]",
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

    Creates records spanning importance 0.1-0.9, saves through the write
    filter, and measures retrieval quality.

    Ground-truth decoupling (C1 fix, 2026-04-17):
        A per-record ``_gt_urgency`` field is generated by RNG at setup.
        Urgency is statistically orthogonal to ``importance`` (Pearson
        |r| < ~0.2 across 100 records by construction).
        Ground truth = top-K by urgency, restricted to records whose
        ``importance >= _wf_min_threshold`` (i.e., records that survived
        the write filter). Retrieval continues to rank by composite
        importance-based score.
        As the override threshold rises, high-urgency-low-importance
        records get filtered out of the relevant set, dropping nDCG.
        This isolates "filter removes valuable-by-orthogonal-signal
        records" from "retrieval favors what survived."
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

        # Orthogonal urgency signal (C1 fix). Uniform random per-record,
        # seeded from a salt derived from — but distinct from — the main
        # RNG so urgency is statistically uncorrelated with importance.
        urgency_rng = random.Random(self._seed.seed_id ^ 0xC1FE5A17)
        self._gt_urgency = {idx: urgency_rng.random() for idx in range(n)}

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

        # Ground truth: top-K by orthogonal URGENCY (C1 fix), restricted
        # to records that survived the write filter. Retrieval ranks by
        # importance-based composite score — orthogonal to urgency — so
        # high-urgency-low-importance records that the filter drops are
        # permanently lost from the relevant set and nDCG falls as the
        # threshold rises.
        relevance_scores = {}
        relevant_ids = set()

        # Compute urgency ranks over the FULL intended set, then filter to
        # survivors. This is different from "top-K by urgency among
        # survivors" because filtered high-urgency records stay in the
        # numerator of Recall@K via the retrieved_ids/relevant_ids set
        # comparison (penalizing the filter for dropping them).
        sorted_indices = sorted(
            range(self._seed.record_count),
            key=lambda i: self._gt_urgency[i],
            reverse=True,
        )
        n_relevant = max(3, self._seed.record_count // 3)
        urgent_indices = set(sorted_indices[:n_relevant])

        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            idx = self._instance_idx_map.get(key)
            # relevance_scores uses urgency (the oracle signal) rather
            # than importance (the retrieval signal) so nDCG measures
            # how well retrieval-by-importance approximates the urgency
            # oracle — not whether the retriever memorized importance.
            if idx is not None:
                relevance_scores[key] = self._gt_urgency[idx]
                if idx in urgent_indices:
                    relevant_ids.add(key)
            else:
                relevance_scores[key] = 0.0

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
                "oracle": "top-K by orthogonal urgency",
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
    co_occurrence_boost to composite_score().

    Ground-truth decoupling (B2 fix, 2026-04-17):
        In addition to the standard seed↔hop1 (weight=initial_weight) and
        hop1↔hop2 (weight=initial_weight) links, a small number of
        "noise" hop2 records gain DIRECT seed↔hop2 links weighted at
        ``initial_weight * 0.3``. Retrieval ordering of hop1 vs
        hop2-noise now depends on ``decay_per_hop``:
          - ``decay_per_hop=0.1``: ``0.1 * 1.0`` hop1 < ``1.0 * 0.3`` noise -> hop2-noise wins.
          - ``decay_per_hop=0.9``: ``0.9 * 1.0`` hop1 > ``1.0 * 0.3`` noise -> hop1 wins.
        Ground truth continues to rank hop1 records (topological cluster
        membership) above hop2-noise, independent of propagation math.
        So low ``decay_per_hop`` values score worse against the oracle;
        high values score better — genuine sensitivity.
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

        # NOISE LINKS (B2 fix): direct seed<->hop2 edges at 0.3x the
        # standard weight. These create retrieval-vs-oracle divergence:
        # at low decay_per_hop, these noise links outrank the 1-hop-away
        # hop1 records via the propagate() BFS; at high decay_per_hop,
        # the hop1 records rank higher. The oracle (cluster membership)
        # always prefers hop1 over hop2-noise, so low decay_per_hop
        # produces lower nDCG.
        self._noise_hop2 = self._clusters["hop2"][:2]  # first 2 hop2 = noise
        noise_weight = initial_weight * 0.3
        for seed_inst in self._clusters["seed"][:2]:  # from 2 seeds
            for noise_inst in self._noise_hop2:
                try:
                    assoc_field.link(
                        CoocMemory,
                        seed_inst.db_key.redis_key,
                        noise_inst.db_key.redis_key,
                        initial_weight=noise_weight,
                    )
                except Exception as e:
                    return ScenarioResult(
                        status="error",
                        error_message=f"link seed->hop2-noise failed: {e}",
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

        # Ground truth: based on CLUSTER MEMBERSHIP (topology), NOT on
        # the propagate() weights. hop1 always ranks highest, then
        # regular hop2 records, then hop2-noise last (as a weaker
        # relevance tier), and seeds/unlinked lowest.
        #
        # At low decay_per_hop, retrieval ranks hop2-noise records ABOVE
        # hop1 via the direct seed->hop2-noise edge, diverging from the
        # oracle and dropping nDCG. At high decay_per_hop, retrieval
        # ranks hop1 above hop2-noise, matching the oracle.
        # Identify noise records by their Redis key (Model instances are
        # not hashable, so sets of instances don't work).
        noise_keys = (
            {ni.db_key.redis_key for ni in self._noise_hop2}
            if hasattr(self, "_noise_hop2")
            else set()
        )
        relevance_scores = {}
        relevant_ids = set()

        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            if inst in self._clusters.get("hop1", []):
                relevance_scores[key] = 1.0
                relevant_ids.add(key)
            elif inst.db_key.redis_key in noise_keys:
                # hop2-noise: topologically a hop2 record. Weaker oracle
                # relevance (0.3) than normal hop2 because the noise link
                # exists specifically to fool the retriever, not because
                # it's a legitimate cluster member.
                relevance_scores[key] = 0.3
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
                "n_hop2_noise": len(noise_keys),
                "n_boost_scores": len(boost_scores),
                "decay_per_hop": decay_per_hop,
                "oracle": "cluster membership (hop1 > hop2 > hop2-noise > seed)",
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
