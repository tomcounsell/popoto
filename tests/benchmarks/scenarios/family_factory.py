"""Family-aware scenario generation for constant sensitivity testing.

Each constant family gets dedicated scenario variants that exercise the
actual code paths those constants control. This contrasts with the generic
ScenarioFactory which varies data but uses the same behavioral pathway
for all scenarios.

Families (as of issue #362, 2026-04-20):
    - decay            — DECAY_RATE
    - confidence       — ACTED/CONTRADICTED_CONFIDENCE_SIGNAL, cycle factors, etc.
    - write_filter     — WF_MIN_THRESHOLD / WF_PRIORITY_THRESHOLD
    - co_occurrence    — CO_OCCURRENCE_DECAY_PER_HOP / INITIAL_WEIGHT / DECAY_FACTOR
    - prediction_ledger — PL_* constants (auto-resolution error triad + thresholds)
    - context_assembler — COMPETITIVE_SUPPRESSION_SIGNAL / DEFAULT_SURFACING_THRESHOLD
    - policy_cache     — MIN_EVENTS_FOR_CRYSTALLIZATION / WILSON_CI_THRESHOLD

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
from src.popoto.fields.constants import Defaults, TemporalPeriod
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.existence_filter import ExistenceFilter
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin
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
    # PredictionLedger family (issue #362)
    "PL_CONFIDENCE_ERROR_THRESHOLD": "prediction_ledger",
    "PL_CONFIDENCE_LOW_SIGNAL": "prediction_ledger",
    "PL_AUTO_RESOLVE_ACTED": "prediction_ledger",
    "PL_AUTO_RESOLVE_DISMISSED": "prediction_ledger",
    "PL_AUTO_RESOLVE_CONTRADICTED": "prediction_ledger",
    # ContextAssembler family (issue #362)
    "COMPETITIVE_SUPPRESSION_SIGNAL": "context_assembler",
    "DEFAULT_SURFACING_THRESHOLD": "context_assembler",
    # PolicyCache family (issue #362)
    "MIN_EVENTS_FOR_CRYSTALLIZATION": "policy_cache",
    "WILSON_CI_THRESHOLD": "policy_cache",
}

FAMILY_NAMES = (
    "decay",
    "confidence",
    "write_filter",
    "co_occurrence",
    "prediction_ledger",
    "context_assembler",
    "policy_cache",
)


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
        # Oracle (B1 fix): purely importance-based ranking, with SPREAD
        # importance values (not clustered) so there's a clear importance
        # ordering. Retrieval uses ``importance * age^(-decay_rate)`` which
        # MIXES importance with age. Low decay_rate -> retrieval ~
        # importance-dominated -> matches oracle well; high decay_rate ->
        # retrieval mostly age-dominated -> diverges from the importance
        # oracle. Oracle has no age dependency so there's a monotonic
        # relationship between decay_rate and nDCG (high decay_rate
        # should score worse).

        now = time.time()
        n = self._seed.record_count

        for idx in range(n):
            # SPREAD importance: strong variation (0.1-0.95) so the
            # importance-based oracle has a clear ordering. Prior version
            # used clustered 0.45-0.55, which combined with age bimodality
            # meant retrieval was 100% driven by decay — no importance
            # component for decay_rate to trade off against. With spread
            # importance, retrieval ``importance * age^(-decay_rate)``
            # genuinely mixes the two signals.
            imp = rng.uniform(0.1, 0.95)
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

        # Ground truth (B1 fix): rank by IMPORTANCE ALONE. No age
        # component. Retrieval mixes importance * age^(-decay_rate). Low
        # decay_rate -> retrieval ~= importance-ordered -> high nDCG; high
        # decay_rate -> retrieval dominated by age -> recent records top
        # regardless of importance -> diverges from the oracle -> low
        # nDCG. This gives a monotonic nDCG-vs-decay_rate relationship
        # and substantial variance.
        decay_rate = self.overrides.get("decay_rate", 0.5)

        relevance_scores = {}
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            relevance_scores[key] = self._importance_values[i]

        # Top 30% by importance are relevant
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
                "oracle": "importance-only ranking (no age component)",
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
        Outcome sequences are generated at length 10 per record. The
        retriever sees only ``seq[:5]`` via ObservationProtocol during
        the observation loop (rounds 0-4). Ground truth is computed from
        the HELD-OUT future ``seq[5:10]``: ``relevance_scores[key] =
        mean(seq[5:10] == "acted")``. Relevant = top 30% by held-out
        acted-rate. A well-calibrated confidence state (ACTED_CONFIDENCE_
        SIGNAL, CONTRADICTED_CONFIDENCE_SIGNAL, cycle factors) is the
        signal that best predicts future acted-rate — so confidence
        constant overrides move nDCG against the held-out oracle.

    Tier-B tightening (issue #362, 2026-04-20):
        Sequence length extended 8 -> 10 (held-out 3 -> 5, reducing RNG
        noise in the oracle signal), and tier distributions sharpened so
        high- and low-importance records have an order-of-magnitude gap
        in acted-probability instead of a ~2x gap. Together these push
        ACTED_CONFIDENCE_SIGNAL / CONTRADICTED_CONFIDENCE_SIGNAL variance
        across the 0.05 acceptance threshold.
    """

    name = "family_confidence"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=2000, family="confidence", record_count=20
        )

    def setup(self):
        rng = random.Random(self._seed.seed_id)
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

        def _sample_tier(p_acted: float, p_dismissed: float, length: int):
            """Sample a length-N outcome sequence.

            Each round independently draws from
            {acted, dismissed, contradicted} with the given probabilities
            (contradicted fills the remainder). Per-record randomness makes
            records within the same importance tier have different outcome
            histories, so ACTED_CONFIDENCE_SIGNAL / CONTRADICTED_CONFIDENCE_
            SIGNAL produce different confidence rankings instead of
            collapsing every tier to a single constant.
            """
            outcomes = []
            for _ in range(length):
                r = rng.random()
                if r < p_acted:
                    outcomes.append("acted")
                elif r < p_acted + p_dismissed:
                    outcomes.append("dismissed")
                else:
                    outcomes.append("contradicted")
            return outcomes

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

            # Assign 10-round outcome sequence as a STOCHASTIC draw from a
            # per-tier distribution. Tier distributions are SHARPENED (issue
            # #362 Tier-B fix) so the top-30% held-out-acted set aligns more
            # reliably with the high-importance tier while still preserving
            # tier overlap for borderline records. The sharper tier gap
            # amplifies the per-record confidence-state signal, so overrides
            # to ACTED_CONFIDENCE_SIGNAL / CONTRADICTED_CONFIDENCE_SIGNAL
            # produce measurably different certainty rankings. Retriever
            # observes seq[0:5]; ground truth uses seq[5:10] (5-round
            # held-out window reduces oracle-signal noise vs the prior 3).
            if imp >= 0.7:
                # High-importance tier: 70% acted / 20% dismissed / 10% contradicted
                seq = _sample_tier(0.70, 0.20, 10)
            elif imp >= 0.5:
                # Medium-high: 55% acted / 25% dismissed / 20% contradicted
                seq = _sample_tier(0.55, 0.25, 10)
            elif imp >= 0.3:
                # Medium: 35% acted / 30% dismissed / 35% contradicted
                seq = _sample_tier(0.35, 0.30, 10)
            else:
                # Low: 20% acted / 35% dismissed / 45% contradicted
                seq = _sample_tier(0.20, 0.35, 10)
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

        # Ground truth: acted-rate over the held-out future rounds [5:10].
        # A well-calibrated confidence state (tuned by ACTED_CONFIDENCE_
        # SIGNAL, CONTRADICTED_CONFIDENCE_SIGNAL, cycle factors, etc.)
        # should predict this held-out acted-rate well. Records that
        # behave "acted" in the held-out future are the relevant set.
        # Held-out window widened 3 -> 5 rounds (issue #362 Tier-B) to
        # reduce per-record oracle variance.
        relevance_scores = {}
        held_out_acted_rates = []
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            seq = self._outcome_sequences[i]
            held_out = seq[5:10]  # 5-round held-out future
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
                "n_held_out_rounds": 5,
                "oracle": "held-out acted-rate over rounds [5:10]",
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


class PredictionLedgerFamilyScenario(Scenario):
    """Scenario exercising PL_* constants via record_prediction + auto_resolve.

    Creates a pool of PredictionLedgerMixin-equipped records with spread
    importance. For each record, runs 5 observed rounds of
    ``record_prediction`` + ``auto_resolve(outcome=seq[r])`` where ``seq``
    is an 8-round per-record outcome sequence drawn from a per-tier
    distribution. The last 3 rounds are held out as the ground-truth
    oracle (mirroring ConfidenceFamilyScenario's decoupling pattern).

    The sensitivity signal: each ``auto_resolve`` call passes the outcome
    through ``_pl_auto_resolve_errors`` to get a prediction_error, which
    then flows into ``_apply_confidence_feedback``. When the error exceeds
    ``PL_CONFIDENCE_ERROR_THRESHOLD``, the record's confidence is reduced
    by ``PL_CONFIDENCE_LOW_SIGNAL``. Overriding any of the 5 PL_* constants
    directly shifts the certainty ranking, which the composite_score query
    (weight 0.7 on certainty) then propagates into nDCG@5.

    Primary-measured constant: ``PL_AUTO_RESOLVE_CONTRADICTED`` (its swept
    grid [0.5, 0.7, 0.8, 0.9, 0.95] straddles the default 0.7 threshold,
    so sweep extremes cross the ``_apply_confidence_feedback`` gate).

    Defensive override: setup pre-seeds ``PL_CONFIDENCE_ERROR_THRESHOLD``
    to 0.02 in ``self.overrides`` (unless caller already set it), so when
    the sweep harness varies ``PL_AUTO_RESOLVE_ACTED`` or
    ``PL_AUTO_RESOLVE_DISMISSED`` alone, the corresponding error values
    still trigger ``_apply_confidence_feedback``. Without this floor, the
    acted/dismissed sweeps would leave confidence unchanged and produce
    variance=0.

    Ground-truth decoupling:
        Retrieval = composite_score({relevance: 0.3, certainty: 0.7}),
        where certainty was trained on seq[0:5] via auto_resolve.
        Oracle = acted-rate over seq[5:8], generated independently per
        record from the same tier distribution (structural independence
        modulo tier assignment). This is the same pattern as
        ConfidenceFamilyScenario.
    """

    name = "family_prediction_ledger"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=5000, family="prediction_ledger", record_count=18
        )
        # Defensive threshold floor: for sweeps over any PL_* constant
        # OTHER than ``PL_AUTO_RESOLVE_CONTRADICTED``, set the confidence
        # error threshold to a very low value so the auto-resolve error
        # reliably crosses the ``_apply_confidence_feedback`` gate
        # regardless of the swept value. The sweep extremes for
        # ``PL_AUTO_RESOLVE_CONTRADICTED`` (0.5, 0.95) are carefully
        # chosen to straddle the default gate (0.7), so lowering the
        # threshold there would defeat the gate-crossing signal. This is
        # the sole PL_* constant the plan budgets variance for; the other
        # four remain inert-by-design (per plan Technical Approach §2).
        if "PL_AUTO_RESOLVE_CONTRADICTED" not in self.overrides:
            self.overrides.setdefault("PL_CONFIDENCE_ERROR_THRESHOLD", 0.02)

    def setup(self):
        rng = random.Random(self._seed.seed_id)

        class PLMemory(PredictionLedgerMixin, popoto.Model):
            gen_name = popoto.UniqueKeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.1, base_score_field="importance"
            )
            certainty = ConfidenceField(initial_confidence=0.5)

            _pl_partition = "default"

        self._model_class = PLMemory
        self._instances = []
        self._importance_values = []
        self._outcome_sequences = []

        n = self._seed.record_count

        def _sample_tier(p_acted: float, p_dismissed: float, length: int):
            outcomes = []
            for _ in range(length):
                r = rng.random()
                if r < p_acted:
                    outcomes.append("acted")
                elif r < p_acted + p_dismissed:
                    outcomes.append("dismissed")
                else:
                    outcomes.append("contradicted")
            return outcomes

        for idx in range(n):
            imp = 0.15 + (0.80 * idx / max(n - 1, 1))
            self._importance_values.append(imp)

            instance = PLMemory(
                gen_name=f"{self._prefix}pl_{idx:04d}",
                content=f"PL scenario record {idx}",
                importance=imp,
            )
            try:
                instance.save()
                self._instances.append(instance)
                self._created_keys.append(instance.db_key.redis_key)
            except Exception as e:
                return ScenarioResult(status="error", error_message=f"save failed: {e}")

            # 8-round outcome sequence per tier. Tier gap is SHARP for
            # contradicted-heavy vs acted-heavy tiers so the
            # gate-crossing on PL_AUTO_RESOLVE_CONTRADICTED produces a
            # dramatic per-record certainty shift between the two sweep
            # regimes (<=0.7 gate skip vs >0.7 gate fire). High-imp tier:
            # mostly acted, rarely contradicted -> the gate-crossing
            # barely moves certainty. Low-imp tier: mostly contradicted
            # -> the gate-crossing fires on every round, compounding
            # certainty drop. The contrast between tiers is what moves
            # nDCG against the held-out acted-rate oracle.
            if imp >= 0.7:
                # High tier: acted-dominated, some contradicted (~20%)
                seq = _sample_tier(0.55, 0.25, 8)
            elif imp >= 0.5:
                # Medium-high: balanced acted/contradicted
                seq = _sample_tier(0.45, 0.30, 8)
            elif imp >= 0.3:
                # Medium: slight contradicted emphasis
                seq = _sample_tier(0.35, 0.30, 8)
            else:
                # Low tier: contradicted-emphasis but still some acted
                seq = _sample_tier(0.25, 0.25, 8)
            self._outcome_sequences.append(seq)

        if not self._instances:
            return

        # Run 5 observed rounds of record_prediction + auto_resolve under
        # apply_overrides so PL_AUTO_RESOLVE_* and PL_CONFIDENCE_* are
        # read with the swept values.
        self._n_resolutions = 0
        with apply_overrides(self.overrides):
            for round_idx in range(5):
                for i, inst in enumerate(self._instances):
                    try:
                        PredictionLedgerMixin.record_prediction(
                            inst, predicted={"outcome": "acted"}
                        )
                    except Exception as e:
                        return ScenarioResult(
                            status="error",
                            error_message=f"record_prediction r{round_idx} i{i}: {e}",
                        )
                    outcome = self._outcome_sequences[i][round_idx]
                    try:
                        err = PredictionLedgerMixin.auto_resolve(inst, outcome=outcome)
                        if err is not None:
                            self._n_resolutions += 1
                    except Exception as e:
                        return ScenarioResult(
                            status="error",
                            error_message=f"auto_resolve r{round_idx} i{i}: {e}",
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

        # Ground truth: acted-rate over held-out rounds [5:8].
        relevance_scores = {}
        held_out_acted_rates = []
        for i, inst in enumerate(self._instances):
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            seq = self._outcome_sequences[i]
            held_out = seq[5:8]
            acted_rate = sum(1 for o in held_out if o == "acted") / max(
                len(held_out), 1
            )
            held_out_acted_rates.append(acted_rate)
            relevance_scores[key] = acted_rate

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
                "family": "prediction_ledger",
                "seed_id": self._seed.seed_id,
                "n_instances": len(self._instances),
                "n_observed_rounds": 5,
                "n_held_out_rounds": 3,
                "n_resolutions": getattr(self, "_n_resolutions", 0),
                "oracle": "held-out acted-rate over rounds [5:8]",
                "pl_error_threshold_floor": self.overrides.get(
                    "PL_CONFIDENCE_ERROR_THRESHOLD"
                ),
            },
        )

    def teardown(self):
        # Delete instances first (also clears model rows).
        if hasattr(self, "_model_class") and hasattr(self, "_instances"):
            for inst in self._instances:
                try:
                    inst.delete()
                except Exception:
                    pass
            # PredictionLedgerMixin metadata lives in $PL:{ClassName}:meta:*
            # and $PL:{ClassName}:errors:* — neither is deleted by
            # Model.delete(). Scan and remove them so sweep runs stay
            # teardown-clean across scenarios.
            try:
                class_name = self._model_class.__name__
                cursor = 0
                while True:
                    cursor, keys = POPOTO_REDIS_DB.scan(
                        cursor, match=f"$PL:{class_name}:*", count=100
                    )
                    if keys:
                        POPOTO_REDIS_DB.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass
        super().teardown()


class ContextAssemblerFamilyScenario(Scenario):
    """Scenario exercising ContextAssembler suppression/surfacing constants.

    Creates a pool of records with ConfidenceField + CyclicDecayField +
    ExistenceFilter composing the minimal ContextAssembler target model.
    Runs ``assemble()`` TWICE under the override context: the first call
    executes ``_post_effects`` which applies ``COMPETITIVE_SUPPRESSION_
    SIGNAL`` to non-selected pull candidates (mutating their certainty).
    The second call's retrieval order is what nDCG measures — at high
    suppression the first-call losers have depressed certainty and drop
    in the second-call ranking; at low suppression they move less.

    Constants exercised:
        - ``COMPETITIVE_SUPPRESSION_SIGNAL``: read at the
          ``ConfidenceField.update_confidence(..., signal=...)`` call site
          in ``_post_effects``; must be active during the FIRST assemble()
          call (the mutation step).
        - ``DEFAULT_SURFACING_THRESHOLD``: captured by
          ``ContextAssembler.__init__`` as ``self.surfacing_threshold``.
          Must be active during construction — patching the module alias
          after construction does not retroactively change the instance
          attribute. That's why the constructor runs INSIDE the
          apply_overrides block below.

    Ground-truth decoupling:
        Retrieval = second-call ``assemble()`` record order (post-
        suppression). Oracle = pre-suppression importance × topic-match
        indicator (0/1). The suppression signal changes the second-call
        ranking without touching the oracle.

    Sizing notes (from critique CONCERN 2):
        ``max_items=3`` (vs the default 10) forces competitive suppression
        to actually fire: with 3-topic × 5-record pools (~5 candidates per
        topic), ``max_items=10`` would let every candidate into the
        selected set and suppress nothing. Small pool (15 records) keeps
        the scenario fast in sweep loops.
    """

    name = "family_context_assembler"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=6000, family="context_assembler", record_count=24
        )

    def setup(self):
        rng = random.Random(self._seed.seed_id)

        class CAMemory(popoto.Model):
            gen_name = popoto.UniqueKeyField()
            topic = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            # ``partition_by='topic'`` — ContextAssembler's pull path calls
            # ``query.filter(topic=...).composite_score(...)``; without
            # partitioning, composite_score silently ignores the
            # KeyField filter and returns the global top-K regardless of
            # topic, making the topic-match oracle unreachable.
            relevance = DecayingSortedField(
                decay_rate=0.1,
                base_score_field="importance",
                partition_by="topic",
            )
            certainty = ConfidenceField(initial_confidence=0.5, partition_by="topic")
            push_score = CyclicDecayField(
                cycles=[(TemporalPeriod.DAILY, 1.0, 0)],
            )
            bloom = ExistenceFilter(
                error_rate=0.01,
                capacity=1000,
                fingerprint_fn=lambda inst: inst.topic,
            )

        self._model_class = CAMemory
        self._instances = []
        self._topics = ["topic_0", "topic_1", "topic_2"]
        self._topic_of = {}
        self._pre_suppression_importance = {}

        n = self._seed.record_count
        per_topic = n // len(self._topics)

        for t_idx, topic in enumerate(self._topics):
            for j in range(per_topic):
                idx = t_idx * per_topic + j
                # Importance spread 0.4-0.6 (tight mid-range cluster) so
                # the competitive-suppression mutation on certainty is
                # the dominant tiebreaker in the final composite ranking.
                # Too-wide a spread lets importance decide the top-K
                # regardless of signal strength; too tight a cluster
                # makes the oracle ordering ambiguous.
                imp = rng.uniform(0.4, 0.6)
                instance = CAMemory(
                    gen_name=f"{self._prefix}ca_{idx:04d}",
                    topic=topic,
                    content=f"CA record {idx} in {topic}",
                    importance=imp,
                )
                try:
                    instance.save()
                    # Register topic in the ExistenceFilter so the
                    # pull-path pre-check doesn't short-circuit with
                    # "all cues definitely missing".
                    try:
                        CAMemory.bloom.add(CAMemory, topic)
                    except Exception:
                        pass
                    self._instances.append(instance)
                    self._created_keys.append(instance.db_key.redis_key)
                    key = instance.db_key.redis_key
                    self._topic_of[key] = topic
                    self._pre_suppression_importance[key] = imp
                except Exception as e:
                    return ScenarioResult(
                        status="error", error_message=f"save failed: {e}"
                    )

    def run(self) -> ScenarioResult:
        if len(self._instances) < 3:
            return ScenarioResult(
                status="skipped-degenerate",
                error_message=f"Only {len(self._instances)} instances (need >= 3)",
            )

        # Import inside run() so module-level aliases we patch under
        # apply_overrides resolve correctly (construction is what binds
        # surfacing_threshold onto the instance).
        from src.popoto.recipes.context_assembler import ContextAssembler

        # Snapshot pre-assemble confidence values so the sanity test (and
        # debug logs) can confirm the first-call side effect actually
        # mutated at least one non-selected candidate.
        pre_certainty = {}
        for inst in self._instances:
            try:
                pre_certainty[inst.db_key.redis_key] = ConfidenceField.get_confidence(
                    inst, "certainty"
                )
            except Exception:
                pass

        query_topic = self._topics[0]
        second_records = []

        with apply_overrides(self.overrides):
            try:
                surfacing_threshold = self.overrides.get(
                    "DEFAULT_SURFACING_THRESHOLD",
                    Defaults.DEFAULT_SURFACING_THRESHOLD,
                )
                # certainty-weighted so competitive suppression (which
                # only mutates confidence) has leverage over the final
                # ranking; relevance stays weighted so the oracle
                # (pre-suppression importance) remains discoverable.
                assembler = ContextAssembler(
                    self._model_class,
                    score_weights={"relevance": 0.4, "certainty": 0.6},
                    surfacing_threshold=surfacing_threshold,
                    max_items=3,
                )
                # ``query_cues`` drives the ExistenceFilter pre-check only
                # (it doesn't narrow the composite_score query). For actual
                # topic filtering we also pass ``partition_filters`` so
                # query.filter(topic=...) scopes the pull path to the
                # queried topic; otherwise composite_score returns the
                # global top-K regardless of topic, and the oracle
                # (topic-match × importance) never aligns with retrieval.
                #
                # First (warmup) call: executes _post_effects which
                # reduces certainty for the max_items*2 - max_items = 3
                # non-selected records in the pool under
                # COMPETITIVE_SUPPRESSION_SIGNAL. The mutation happens
                # regardless of our partition_filter scope.
                assembler.assemble(
                    query_cues={"topic": query_topic},
                    partition_filters={"topic": query_topic},
                )
                # Second (measured) call: same topic; the final ranking
                # reflects the suppressed certainty of the warmup's
                # non-selected pool. nDCG of this call vs the
                # pre-suppression oracle is what moves with signal
                # strength.
                result_final = assembler.assemble(
                    query_cues={"topic": query_topic},
                    partition_filters={"topic": query_topic},
                )
                second_records = result_final.records
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"assemble failed: {e}",
                )

        post_certainty = {}
        for inst in self._instances:
            try:
                post_certainty[inst.db_key.redis_key] = ConfidenceField.get_confidence(
                    inst, "certainty"
                )
            except Exception:
                pass

        # Count records whose certainty moved in EITHER direction after
        # the warmup assemble() call. Suppression with signal < 0.5 is a
        # "contradiction" and moves certainty DOWN; signal > 0.5 is a
        # "corroboration" and moves certainty UP. Either way the
        # suppression mechanism fired — the sanity test just needs to
        # know _post_effects was not a no-op.
        n_certainty_moved = sum(
            1
            for k, pre in pre_certainty.items()
            if k in post_certainty and abs(post_certainty[k] - pre) > 1e-6
        )
        n_suppressed = sum(
            1
            for k, pre in pre_certainty.items()
            if k in post_certainty and post_certainty[k] < pre - 1e-6
        )

        retrieved_ids = []
        for r in second_records:
            key = getattr(r, "_redis_key", None) or r.db_key.redis_key
            retrieved_ids.append(key)

        # Ground truth: pre-suppression importance × topic-match indicator.
        # Only records in the queried topic contribute; among those, top
        # 30% by importance are relevant.
        relevance_scores = {}
        matched_importance = []
        for inst in self._instances:
            key = inst.db_key.redis_key
            if self._topic_of.get(key) == query_topic:
                relevance_scores[key] = self._pre_suppression_importance[key]
                matched_importance.append((key, relevance_scores[key]))
            else:
                relevance_scores[key] = 0.0

        matched_importance.sort(key=lambda x: x[1], reverse=True)
        n_relevant = max(2, max(1, len(matched_importance) // 3))
        relevant_ids = {k for k, _ in matched_importance[:n_relevant]}

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            metadata={
                "family": "context_assembler",
                "seed_id": self._seed.seed_id,
                "n_instances": len(self._instances),
                "n_matched": len(matched_importance),
                "n_suppressed": n_suppressed,
                "n_certainty_moved": n_certainty_moved,
                "query_topic": query_topic,
                "surfacing_threshold_used": surfacing_threshold,
                "suppression_signal_default": Defaults.COMPETITIVE_SUPPRESSION_SIGNAL,
                "oracle": "pre-suppression importance x topic-match",
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


class PolicyCacheFamilyScenario(Scenario):
    """Scenario exercising PolicyCache crystallization thresholds.

    Drives ``crystallization_handler`` (the async StreamConsumer handler)
    with a synthesized batch of 8 distinct (state_fingerprint, action_type)
    groups carrying varying (successes, total) counts. Under
    ``apply_overrides``, the handler reads ``MIN_EVENTS_FOR_CRYSTALLIZATION``
    and ``WILSON_CI_THRESHOLD`` at its module-level binding site, so
    sweeping those constants changes the set of PolicyEntry rows that
    crystallize (and therefore the set retrievable by the composite-score
    query).

    Ground-truth decoupling:
        Retrieval = PolicyEntry ranked by ``expected_value`` (Wilson CI
        lower bound set at crystallization time). Oracle = an
        INDEPENDENTLY-generated "truth ratio" per (fingerprint, action)
        group, drawn from a separate RNG. Even with perfect
        crystallization, retrieval rank-order need not match the
        independent optimality label — so override values that let noisy
        groups crystallize (low ``MIN_EVENTS``) drop nDCG; values that
        crystallize only well-attested groups raise it.

    Implementation notes (from critique CONCERN 1):
        - The handler is ``async def``, so we run it via ``asyncio.run``.
        - Each scenario run uses unique state_fingerprints tied to
          ``self._prefix`` so the PolicyEntry bloom pre-check
          (``PolicyEntry.bloom.definitely_missing``) doesn't dedupe
          across runs.
        - We use the real ``PolicyEntry`` model rather than a minimal
          subclass because ``crystallization_handler`` constructs
          ``PolicyEntry`` directly (policy_cache.py:499-506). Cleanup
          in teardown handles the composed-mixin residue ($PL:* meta
          hashes, agent-partitioned sorted sets).
    """

    name = "family_policy_cache"

    def __init__(self, overrides=None, seed=None):
        super().__init__(overrides=overrides)
        self._seed = seed or FamilySeed(
            seed_id=7000, family="policy_cache", record_count=8
        )

    def setup(self):
        rng = random.Random(self._seed.seed_id)
        # Groups straddling the threshold boundaries: success/total pairs
        # chosen so MIN_EVENTS_FOR_CRYSTALLIZATION and WILSON_CI_THRESHOLD
        # overrides partition the crystallized set differently.
        # (successes, total):
        #   (1, 1) — total < 3 (typical min_events); Wilson CI high
        #   (2, 3) — borderline
        #   (3, 5) — modest count, high success
        #   (5, 5) — perfect success, modest count
        #   (3, 10) — low success rate, medium count
        #   (8, 10) — high success, medium count
        #   (10, 15) — strong signal, high count
        #   (2, 20) — very low success, high count (good at high min_events)
        # Spec chosen so (successes, total) Wilson CI lower bounds span
        # the sweep range [0.3, 0.5, 0.6, 0.7, 0.8] and totals span the
        # MIN_EVENTS sweep [1, 2, 3, 5, 10]. See wilson_ci_lower table in
        # tests/benchmarks/results for calibration. The mix guarantees
        # that different (min_events, wilson) override combinations yield
        # different crystallized-set sizes:
        #   (1, 1)   ci=0.207, total<2
        #   (2, 2)   ci=0.342, total>=2
        #   (3, 3)   ci=0.438, total>=3
        #   (5, 5)   ci=0.566
        #   (8, 10)  ci=0.490
        #   (9, 10)  ci=0.596
        #   (13, 15) ci=0.621
        #   (20, 20) ci=0.839 (passes every sweep setting)
        self._group_specs = [
            (1, 1),
            (2, 2),
            (3, 3),
            (5, 5),
            (8, 10),
            (9, 10),
            (13, 15),
            (20, 20),
        ]

        # Oracle: independently-generated "truth ratio" per group. Drawn
        # from a distinct RNG so the oracle is decoupled from the success
        # rate that drives crystallization. A high truth_ratio means the
        # (fp, action) pair is actually optimal; low means it is not. The
        # retriever only sees crystallized rows, so strict thresholds may
        # drop high-truth-ratio rows (reducing recall and nDCG).
        oracle_rng = random.Random(self._seed.seed_id ^ 0xACE0)
        self._truth_ratios = {
            idx: oracle_rng.random() for idx in range(len(self._group_specs))
        }

        # Build the synthetic event batch as (entry_id, fields) tuples in
        # the StreamConsumer contract shape.
        #
        # Fingerprint format (important!): each group gets a SHA-1-derived
        # fully-divergent hex string. Two constraints:
        # (i) no colons/underscores (tokenizer splits there and shared
        #     tokens pollute the bloom across groups);
        # (ii) no shared prefix — the DJB2/FNV double-hash the bloom uses
        #     collides heavily on strings that share a long prefix and
        #     differ only in the last 1-2 bytes (tested locally: 8 FPs
        #     differing only in the last character all hash to
        #     overlapping bit positions after a single insert). Using
        #     scenario-prefix-keyed SHA-1 digests ensures every FP has
        #     a unique prefix AND is a single token.
        import hashlib

        prefix_hex = self._prefix.replace("bench:", "").replace(":", "")
        self._entries = []
        for idx, (successes, total) in enumerate(self._group_specs):
            fp = hashlib.sha1(f"{prefix_hex}-fp-{idx}".encode()).hexdigest()[:20]
            action = hashlib.sha1(f"{prefix_hex}-act-{idx}".encode()).hexdigest()[:12]
            for ev_i in range(total):
                outcome = "success" if ev_i < successes else "failure"
                entry = (
                    f"{fp}-{ev_i}",
                    {
                        "state_fingerprint": fp,
                        "action_type": action,
                        "outcome": outcome,
                        "agent_id": "default",
                    },
                )
                self._entries.append(entry)

        # Record fingerprints by group so run() and teardown() can match
        # them back to truth_ratios / cleanup.
        self._fp_by_idx = {
            idx: hashlib.sha1(f"{prefix_hex}-fp-{idx}".encode()).hexdigest()[:20]
            for idx in range(len(self._group_specs))
        }

    def run(self) -> ScenarioResult:
        if not getattr(self, "_entries", None):
            return ScenarioResult(
                status="skipped-degenerate",
                error_message="No entries prepared",
            )

        import asyncio

        from src.popoto.recipes.policy_cache import (
            PolicyEntry,
            crystallization_handler,
        )

        with apply_overrides(self.overrides):
            try:
                asyncio.run(crystallization_handler(self._entries))
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"crystallization_handler failed: {e}",
                )

            # Query: what crystallized for this scenario's prefix? Fetch
            # all PolicyEntries and filter by fingerprint prefix so we
            # don't leak across sibling scenarios or stale state.
            crystallized = []
            try:
                for idx, fp in self._fp_by_idx.items():
                    matches = PolicyEntry.query.filter(state_fingerprint=fp)
                    for p in matches:
                        crystallized.append((idx, p))
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"PolicyEntry query failed: {e}",
                )

        # Track crystallized rows for teardown cleanup.
        self._crystallized = [p for _, p in crystallized]

        if not crystallized:
            # Nothing crystallized at current thresholds — scenario is
            # still valid for the override sensitivity signal (very strict
            # thresholds legitimately yield zero rows) but can't produce
            # a ranking.
            return ScenarioResult(
                status="skipped-degenerate",
                error_message="No PolicyEntries crystallized at current thresholds",
                metadata={
                    "family": "policy_cache",
                    "n_crystallized": 0,
                    "min_events_used": self.overrides.get(
                        "MIN_EVENTS_FOR_CRYSTALLIZATION",
                        Defaults.MIN_EVENTS_FOR_CRYSTALLIZATION,
                    ),
                    "wilson_ci_used": self.overrides.get(
                        "WILSON_CI_THRESHOLD", Defaults.WILSON_CI_THRESHOLD
                    ),
                },
            )

        # Retrieval: sort crystallized rows by their group index's initial
        # expected_value (Wilson CI lower bound). We approximate by
        # group's success rate since that correlates with expected_value
        # and we don't re-read the sorted set here — the group-idx order
        # is what matters for the nDCG comparison against the oracle.
        def _group_score(group_idx):
            succ, total = self._group_specs[group_idx]
            return succ / max(total, 1)

        crystallized.sort(key=lambda t: _group_score(t[0]), reverse=True)

        retrieved_ids = [p.db_key.redis_key for _, p in crystallized]

        # Oracle: top 30% of the 8 groups by truth_ratio are relevant,
        # regardless of whether they crystallized. Retrieval misses
        # high-truth groups that failed to crystallize -> nDCG drops.
        relevance_scores = {}
        oracle_sorted = sorted(
            self._truth_ratios.items(), key=lambda x: x[1], reverse=True
        )
        n_relevant_groups = max(2, len(oracle_sorted) // 3)
        relevant_group_idxs = {g for g, _ in oracle_sorted[:n_relevant_groups]}

        relevant_ids = set()
        for group_idx, p in crystallized:
            key = p.db_key.redis_key
            relevance_scores[key] = self._truth_ratios[group_idx]
            if group_idx in relevant_group_idxs:
                relevant_ids.add(key)

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            metadata={
                "family": "policy_cache",
                "seed_id": self._seed.seed_id,
                "n_groups": len(self._group_specs),
                "n_crystallized": len(crystallized),
                "n_relevant_groups": n_relevant_groups,
                "min_events_used": self.overrides.get(
                    "MIN_EVENTS_FOR_CRYSTALLIZATION",
                    Defaults.MIN_EVENTS_FOR_CRYSTALLIZATION,
                ),
                "wilson_ci_used": self.overrides.get(
                    "WILSON_CI_THRESHOLD", Defaults.WILSON_CI_THRESHOLD
                ),
                "oracle": "independent truth-ratio per group",
            },
        )

    def teardown(self):
        # Delete any crystallized PolicyEntries this run created.
        if hasattr(self, "_crystallized"):
            for p in self._crystallized:
                try:
                    p.delete()
                except Exception:
                    pass
        # Clean up PL meta/error hashes left by PredictionLedgerMixin
        # composed into PolicyEntry, plus the partitioned expected_value
        # sorted set. Use SCAN with the PolicyEntry namespace -- safe
        # because fingerprints are prefixed with self._prefix so we
        # match only our own rows' metadata.
        try:
            patterns = [
                f"$PL:PolicyEntry:*",
                f"$DecayingSortF:PolicyEntry:*",
                f"$ConfidencF:PolicyEntry:*",
                f"$CoOccurF:PolicyEntry:*",
                f"$ExistenceF:PolicyEntry:*",
                f"PolicyEntry:*{self._prefix}*",
            ]
            for pat in patterns:
                cursor = 0
                while True:
                    cursor, keys = POPOTO_REDIS_DB.scan(cursor, match=pat, count=100)
                    if keys:
                        try:
                            POPOTO_REDIS_DB.delete(*keys)
                        except Exception:
                            pass
                    if cursor == 0:
                        break
        except Exception:
            pass
        super().teardown()


# Map family names to scenario classes
FAMILY_SCENARIO_CLASSES = {
    "decay": DecayFamilyScenario,
    "confidence": ConfidenceFamilyScenario,
    "write_filter": WriteFilterFamilyScenario,
    "co_occurrence": CoOccurrenceFamilyScenario,
    "prediction_ledger": PredictionLedgerFamilyScenario,
    "context_assembler": ContextAssemblerFamilyScenario,
    "policy_cache": PolicyCacheFamilyScenario,
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
                    "prediction_ledger": 5000,
                    "context_assembler": 6000,
                    "policy_cache": 7000,
                }[family_name]

                seed = FamilySeed(
                    seed_id=base_seed + variant * 100,
                    family=family_name,
                    record_count={
                        "decay": 20,
                        "confidence": 20,
                        "write_filter": 30,
                        "co_occurrence": 24,
                        "prediction_ledger": 18,
                        "context_assembler": 24,
                        "policy_cache": 8,
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
