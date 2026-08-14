"""Parametric scenario generation from seed configurations.

ScenarioFactory generates Scenario subclasses from ScenarioSeed dataclasses.
Each seed deterministically produces a scenario with specific record counts,
importance distributions, access patterns, and other axes that stress-test
different behavioral constants.

Usage:
    seeds = ScenarioFactory.default_seeds(n=50)
    for seed in seeds:
        scenario_class = ScenarioFactory.create(seed)
        # scenario_class conforms to the Scenario interface
        result = scenario_class(overrides={}).execute()
"""

import math
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Type

from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..overrides import apply_overrides
from .base import Scenario, ScenarioResult

IMPORTANCE_SHAPES = ("uniform", "clustered", "bimodal", "exponential", "flat")
ACCESS_PATTERNS = ("all_recent", "half_stale", "mostly_stale", "interleaved")


@dataclass
class ScenarioSeed:
    """Deterministic configuration for generating a Scenario class.

    Attributes:
        seed_id: Unique integer for deterministic RNG seeding.
        record_count: Number of Memory records created in setup (5-100).
        importance_shape: Distribution shape for importance values.
        access_pattern: Which records get recent access (affects decay).
        outcome_frequency: Fraction of records that receive "acted" outcomes (0.0-1.0).
        noise_ratio: Fraction of records that are pure noise with importance < 0.15 (0.0-0.5).
        link_density: Fraction of record pairs with co-occurrence links (0.0-1.0).
        age_spread_days: Range of record ages in days (1-365).
    """

    seed_id: int
    record_count: int
    importance_shape: str
    access_pattern: str
    outcome_frequency: float
    noise_ratio: float
    link_density: float
    age_spread_days: int

    def __post_init__(self):
        assert self.record_count >= 3, "Need at least 3 records"
        assert self.record_count <= 100, "Max 100 records"
        assert self.importance_shape in IMPORTANCE_SHAPES
        assert self.access_pattern in ACCESS_PATTERNS
        assert 0.0 <= self.outcome_frequency <= 1.0
        assert 0.0 <= self.noise_ratio <= 0.5
        assert 0.0 <= self.link_density <= 1.0
        assert 1 <= self.age_spread_days <= 365


def _generate_importance_values(
    rng: random.Random,
    n: int,
    shape: str,
    noise_ratio: float,
) -> List[float]:
    """Generate importance values according to the specified distribution shape.

    Returns a list of n floats in [0.01, 1.0]. A fraction equal to noise_ratio
    will have importance < 0.15 (noise records).
    """
    n_noise = int(n * noise_ratio)
    n_signal = n - n_noise

    if shape == "uniform":
        # Evenly spread from 0.15 to 0.95
        signal = [0.15 + (0.80 * i / max(n_signal - 1, 1)) for i in range(n_signal)]
    elif shape == "clustered":
        # Two tight clusters around 0.5 and 0.8
        mid_count = n_signal // 2
        high_count = n_signal - mid_count
        signal = [rng.gauss(0.5, 0.05) for _ in range(mid_count)]
        signal += [rng.gauss(0.8, 0.05) for _ in range(high_count)]
    elif shape == "bimodal":
        # Strong separation: low cluster (0.2-0.35) and high cluster (0.75-0.95)
        low_count = n_signal // 2
        high_count = n_signal - low_count
        signal = [rng.uniform(0.2, 0.35) for _ in range(low_count)]
        signal += [rng.uniform(0.75, 0.95) for _ in range(high_count)]
    elif shape == "exponential":
        # Exponential decay: many low, few high
        raw = [rng.expovariate(3.0) for _ in range(n_signal)]
        max_raw = max(raw) if raw else 1.0
        signal = [0.15 + 0.80 * (r / max_raw) for r in raw]
    elif shape == "flat":
        # All records at nearly the same importance (~0.5-0.55)
        signal = [rng.uniform(0.50, 0.55) for _ in range(n_signal)]
    else:
        raise ValueError(f"Unknown importance shape: {shape}")

    # Clamp signal values
    signal = [max(0.15, min(0.99, v)) for v in signal]

    # Generate noise records
    noise = [rng.uniform(0.01, 0.14) for _ in range(n_noise)]

    values = signal + noise
    rng.shuffle(values)
    return values


def _select_stale_indices(
    rng: random.Random,
    n: int,
    pattern: str,
) -> Set[int]:
    """Return set of indices that should be marked as stale (not recently accessed).

    Records NOT in the returned set are considered recently accessed.
    """
    all_indices = list(range(n))
    if pattern == "all_recent":
        return set()  # No stale records
    elif pattern == "half_stale":
        rng.shuffle(all_indices)
        return set(all_indices[: n // 2])
    elif pattern == "mostly_stale":
        # 80% stale
        rng.shuffle(all_indices)
        return set(all_indices[: int(n * 0.8)])
    elif pattern == "interleaved":
        # Alternating stale/recent
        return {i for i in range(n) if i % 2 == 0}
    else:
        raise ValueError(f"Unknown access pattern: {pattern}")


class ScenarioFactory:
    """Generate Scenario subclasses from parameterized seed configurations."""

    @staticmethod
    def create(seed: ScenarioSeed) -> Type[Scenario]:
        """Build a concrete Scenario subclass from a seed.

        The returned class conforms to the standard Scenario interface:
        - Has a unique `name` attribute
        - Constructor accepts `overrides` dict
        - `execute()` returns a valid ScenarioResult

        Determinism: given the same seed, the factory produces the same scenario
        with identical records. Uses random.Random(seed_id) for all stochastic
        decisions.

        Args:
            seed: ScenarioSeed configuration.

        Returns:
            A Scenario subclass (not an instance).
        """
        # Capture seed in closure
        _seed = seed

        class GeneratedScenario(Scenario):
            name = f"gen_{_seed.seed_id:03d}_{_seed.importance_shape}_{_seed.access_pattern}"

            def setup(self):
                rng = random.Random(_seed.seed_id)

                # Build model class with overrides
                decay_rate = self.overrides.get("decay_rate", 0.5)
                initial_confidence = self.overrides.get("initial_confidence", 0.5)
                wf_min = self.overrides.get("_wf_min_threshold", 0.2)
                wf_priority = self.overrides.get("_wf_priority_threshold", 0.7)

                class GenMemory(WriteFilterMixin, popoto.Model):
                    _wf_min_threshold = wf_min
                    _wf_priority_threshold = wf_priority

                    gen_name = popoto.UniqueKeyField()
                    content = popoto.StringField(default="")
                    importance = popoto.FloatField(default=0.5)
                    relevance = DecayingSortedField(
                        decay_rate=decay_rate, base_score_field="importance"
                    )
                    certainty = ConfidenceField(initial_confidence=initial_confidence)

                    def compute_filter_score(self):
                        return self.importance or 0.0

                self._model_class = GenMemory
                self._instances = []
                self._importance_values = _generate_importance_values(
                    rng, _seed.record_count, _seed.importance_shape, _seed.noise_ratio
                )

                # Create records
                for idx, imp in enumerate(self._importance_values):
                    instance = GenMemory(
                        gen_name=f"{self._prefix}rec_{idx:04d}",
                        content=f"Generated record {idx} seed {_seed.seed_id}",
                        importance=imp,
                    )
                    try:
                        instance.save()
                        self._instances.append(instance)
                        self._created_keys.append(instance.db_key.redis_key)
                    except Exception:
                        pass  # Filtered by WriteFilterMixin

                if not self._instances:
                    return

                # Determine which records are stale vs recently accessed
                stale_indices = _select_stale_indices(
                    rng, len(self._instances), _seed.access_pattern
                )

                # Apply age offsets to stale records by adjusting their
                # DecayingSortedField scores via Redis ZADD with older timestamps
                age_spread_seconds = _seed.age_spread_days * 86400
                now = time.time()
                for idx, inst in enumerate(self._instances):
                    if idx in stale_indices:
                        age_offset = rng.uniform(
                            age_spread_seconds * 0.3, age_spread_seconds
                        )
                        old_time = now - age_offset
                        # Update the sorted set score to simulate old access
                        try:
                            ss_key = f"{inst.db_key.redis_key}:relevance"
                            POPOTO_REDIS_DB.zadd(
                                ss_key, {inst.db_key.redis_key: old_time}
                            )
                        except Exception:
                            pass

                # Simulate acted outcomes based on outcome_frequency
                acted_instances = []
                for inst in self._instances:
                    imp = inst.importance or 0
                    if imp >= 0.15 and rng.random() < _seed.outcome_frequency:
                        acted_instances.append(inst)

                if acted_instances:
                    outcome_map = {}
                    for inst in acted_instances:
                        key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                        outcome_map[key] = "acted"
                    with apply_overrides(self.overrides):
                        ObservationProtocol.on_context_used(
                            acted_instances, outcome_map
                        )

                # Co-occurrence links based on link_density
                if _seed.link_density > 0 and len(self._instances) >= 2:
                    n_possible_pairs = (
                        len(self._instances) * (len(self._instances) - 1)
                    ) // 2
                    n_links = max(1, int(n_possible_pairs * _seed.link_density))
                    # Generate deterministic subset of pairs
                    pairs = []
                    for i in range(len(self._instances)):
                        for j in range(i + 1, len(self._instances)):
                            pairs.append((i, j))
                    rng.shuffle(pairs)
                    selected_pairs = pairs[:n_links]
                    for i, j in selected_pairs:
                        try:
                            key_i = self._instances[i].db_key.redis_key
                            key_j = self._instances[j].db_key.redis_key
                            link_key = f"{self._prefix}link:{key_i}:{key_j}"
                            POPOTO_REDIS_DB.set(link_key, "1")
                            self._created_keys.append(link_key)
                        except Exception:
                            pass

            def run(self) -> ScenarioResult:
                if len(self._instances) < 3:
                    return ScenarioResult(
                        status="skipped-degenerate",
                        error_message=(
                            f"Only {len(self._instances)} instances survived "
                            f"write filter (need >= 3)"
                        ),
                    )

                with apply_overrides(self.overrides):
                    try:
                        results = self._model_class.query.composite_score(
                            indexes={"relevance": 0.6, "certainty": 0.4},
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

                # Ground truth: top records by importance are relevant
                # Use top 30% of records (by importance) as relevant set
                sorted_instances = sorted(
                    self._instances,
                    key=lambda inst: inst.importance or 0,
                    reverse=True,
                )
                n_relevant = max(3, len(sorted_instances) // 3)
                relevant_instances = sorted_instances[:n_relevant]

                relevant_ids = set()
                relevance_scores = {}
                for inst in self._instances:
                    key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                    imp = inst.importance or 0
                    relevance_scores[key] = imp
                    if inst in relevant_instances:
                        relevant_ids.add(key)

                # Calibration data
                predictions = []
                outcomes = []
                for inst in self._instances:
                    try:
                        conf = ConfidenceField.get_confidence(inst, "certainty")
                        predictions.append(conf)
                        outcomes.append(inst in relevant_instances)
                    except Exception:
                        pass

                return ScenarioResult(
                    retrieved_ids=retrieved_ids,
                    relevant_ids=relevant_ids,
                    relevance_scores=relevance_scores,
                    predictions=predictions,
                    outcomes=outcomes,
                    metadata={
                        "seed_id": _seed.seed_id,
                        "record_count": _seed.record_count,
                        "importance_shape": _seed.importance_shape,
                        "access_pattern": _seed.access_pattern,
                        "n_instances": len(self._instances),
                        "n_relevant": len(relevant_ids),
                        "n_retrieved": len(retrieved_ids),
                    },
                )

            def teardown(self):
                if hasattr(self, "_model_class") and hasattr(self, "_instances"):
                    try:
                        for inst in self._instances:
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

        return GeneratedScenario

    @staticmethod
    def default_seeds(n: int = 50) -> List[ScenarioSeed]:
        """Produce a deterministic list of diverse seeds.

        Uses combinatorial iteration over axis values with emphasis on
        high-leverage combinations (larger record counts, clustered/bimodal
        importance, mixed staleness patterns).

        Args:
            n: Number of seeds to generate (default 50).

        Returns:
            List of ScenarioSeed objects with IDs 0 through n-1.
        """
        rng = random.Random(42)  # Fixed seed for deterministic generation

        # Define axis value pools
        record_counts = [8, 15, 25, 40, 60]
        shapes = list(IMPORTANCE_SHAPES)
        patterns = list(ACCESS_PATTERNS)
        outcome_freqs = [0.0, 0.2, 0.5, 0.8, 1.0]
        noise_ratios = [0.0, 0.1, 0.2, 0.3]
        link_densities = [0.0, 0.1, 0.3, 0.5]
        age_spreads = [7, 30, 90, 180, 365]

        seeds = []
        for i in range(n):
            # Deterministic selection cycling through axis values
            # with pseudo-random mixing for diversity
            rc = record_counts[i % len(record_counts)]
            shape = shapes[i % len(shapes)]
            pattern = patterns[(i * 3) % len(patterns)]
            outcome = outcome_freqs[(i * 7) % len(outcome_freqs)]
            noise = noise_ratios[(i * 11) % len(noise_ratios)]
            link = link_densities[(i * 13) % len(link_densities)]
            age = age_spreads[(i * 17) % len(age_spreads)]

            # Bias toward larger record counts for high-leverage coverage
            if i >= n // 2:
                rc = max(rc, record_counts[2])  # At least 25 records

            seeds.append(
                ScenarioSeed(
                    seed_id=i,
                    record_count=rc,
                    importance_shape=shape,
                    access_pattern=pattern,
                    outcome_frequency=outcome,
                    noise_ratio=noise,
                    link_density=link,
                    age_spread_days=age,
                )
            )

        return seeds

    @staticmethod
    def create_all(
        seeds: Optional[List[ScenarioSeed]] = None,
        n: int = 50,
    ) -> List[Type[Scenario]]:
        """Create scenario classes for all seeds.

        Args:
            seeds: Explicit seed list. If None, uses default_seeds(n).
            n: Number of default seeds if seeds is None.

        Returns:
            List of Scenario subclasses.
        """
        if seeds is None:
            seeds = ScenarioFactory.default_seeds(n)
        return [ScenarioFactory.create(seed) for seed in seeds]
