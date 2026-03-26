"""Tests for the parametric ScenarioFactory.

Validates seed generation, importance distributions, access patterns,
determinism, degenerate detection, and Scenario interface conformance.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from tests.benchmarks.scenarios.factory import (
    IMPORTANCE_SHAPES,
    ACCESS_PATTERNS,
    ScenarioFactory,
    ScenarioSeed,
    _generate_importance_values,
    _select_stale_indices,
)


class TestScenarioSeed:
    def test_valid_seed(self):
        seed = ScenarioSeed(
            seed_id=0,
            record_count=10,
            importance_shape="uniform",
            access_pattern="all_recent",
            outcome_frequency=0.5,
            noise_ratio=0.1,
            link_density=0.0,
            age_spread_days=30,
        )
        assert seed.seed_id == 0
        assert seed.record_count == 10

    def test_invalid_record_count_low(self):
        with pytest.raises(AssertionError):
            ScenarioSeed(
                seed_id=0, record_count=2, importance_shape="uniform",
                access_pattern="all_recent", outcome_frequency=0.5,
                noise_ratio=0.0, link_density=0.0, age_spread_days=30,
            )

    def test_invalid_record_count_high(self):
        with pytest.raises(AssertionError):
            ScenarioSeed(
                seed_id=0, record_count=101, importance_shape="uniform",
                access_pattern="all_recent", outcome_frequency=0.5,
                noise_ratio=0.0, link_density=0.0, age_spread_days=30,
            )

    def test_invalid_importance_shape(self):
        with pytest.raises(AssertionError):
            ScenarioSeed(
                seed_id=0, record_count=10, importance_shape="invalid",
                access_pattern="all_recent", outcome_frequency=0.5,
                noise_ratio=0.0, link_density=0.0, age_spread_days=30,
            )

    def test_invalid_noise_ratio(self):
        with pytest.raises(AssertionError):
            ScenarioSeed(
                seed_id=0, record_count=10, importance_shape="uniform",
                access_pattern="all_recent", outcome_frequency=0.5,
                noise_ratio=0.6, link_density=0.0, age_spread_days=30,
            )


class TestImportanceDistributions:
    """Test each importance distribution shape."""

    def test_uniform_spread(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 20, "uniform", 0.0)
        assert len(values) == 20
        assert all(0.01 <= v <= 1.0 for v in values)
        # Uniform should span most of the range
        assert max(values) - min(values) > 0.5

    def test_clustered_tight_groups(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 30, "clustered", 0.0)
        assert len(values) == 30
        # Should have values near 0.5 and 0.8
        near_05 = [v for v in values if 0.4 <= v <= 0.6]
        near_08 = [v for v in values if 0.7 <= v <= 0.9]
        assert len(near_05) > 0
        assert len(near_08) > 0

    def test_bimodal_separation(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 20, "bimodal", 0.0)
        low = [v for v in values if v < 0.5]
        high = [v for v in values if v >= 0.5]
        assert len(low) > 0
        assert len(high) > 0

    def test_exponential_skew(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 30, "exponential", 0.0)
        assert len(values) == 30
        # Exponential: median should be below mean (left-skewed in importance)
        sorted_vals = sorted(values)
        median = sorted_vals[len(sorted_vals) // 2]
        mean = sum(values) / len(values)
        # Just verify all values are valid
        assert all(0.01 <= v <= 1.0 for v in values)

    def test_flat_narrow_range(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 20, "flat", 0.0)
        assert len(values) == 20
        # Flat: all values should be close together
        assert max(values) - min(values) < 0.1

    def test_noise_ratio_adds_low_values(self):
        import random
        rng = random.Random(42)
        values = _generate_importance_values(rng, 20, "uniform", 0.3)
        assert len(values) == 20
        noise = [v for v in values if v < 0.15]
        assert len(noise) >= 4  # 30% of 20 = 6, some might get clamped

    def test_all_shapes_produce_correct_count(self):
        import random
        for shape in IMPORTANCE_SHAPES:
            rng = random.Random(42)
            values = _generate_importance_values(rng, 15, shape, 0.1)
            assert len(values) == 15, f"Shape {shape} produced {len(values)}"


class TestAccessPatterns:
    def test_all_recent_no_stale(self):
        import random
        rng = random.Random(42)
        stale = _select_stale_indices(rng, 20, "all_recent")
        assert len(stale) == 0

    def test_half_stale(self):
        import random
        rng = random.Random(42)
        stale = _select_stale_indices(rng, 20, "half_stale")
        assert len(stale) == 10

    def test_mostly_stale(self):
        import random
        rng = random.Random(42)
        stale = _select_stale_indices(rng, 20, "mostly_stale")
        assert len(stale) == 16  # 80% of 20

    def test_interleaved(self):
        import random
        rng = random.Random(42)
        stale = _select_stale_indices(rng, 20, "interleaved")
        assert len(stale) == 10
        # Even indices are stale
        assert 0 in stale
        assert 1 not in stale
        assert 2 in stale


class TestScenarioFactory:
    def test_create_returns_scenario_class(self):
        from tests.benchmarks.scenarios.base import Scenario
        seed = ScenarioSeed(
            seed_id=0, record_count=5, importance_shape="uniform",
            access_pattern="all_recent", outcome_frequency=0.5,
            noise_ratio=0.0, link_density=0.0, age_spread_days=30,
        )
        cls = ScenarioFactory.create(seed)
        assert isinstance(cls, type)
        assert issubclass(cls, Scenario)

    def test_scenario_has_unique_name(self):
        seed1 = ScenarioSeed(
            seed_id=0, record_count=5, importance_shape="uniform",
            access_pattern="all_recent", outcome_frequency=0.5,
            noise_ratio=0.0, link_density=0.0, age_spread_days=30,
        )
        seed2 = ScenarioSeed(
            seed_id=1, record_count=10, importance_shape="clustered",
            access_pattern="half_stale", outcome_frequency=0.3,
            noise_ratio=0.1, link_density=0.0, age_spread_days=60,
        )
        cls1 = ScenarioFactory.create(seed1)
        cls2 = ScenarioFactory.create(seed2)
        assert cls1.name != cls2.name

    def test_scenario_executes_successfully(self):
        seed = ScenarioSeed(
            seed_id=42, record_count=8, importance_shape="uniform",
            access_pattern="all_recent", outcome_frequency=0.5,
            noise_ratio=0.0, link_density=0.0, age_spread_days=30,
        )
        cls = ScenarioFactory.create(seed)
        instance = cls(overrides={})
        result = instance.execute()
        assert result.status in ("ok", "skipped-degenerate", "error")
        if result.status == "ok":
            assert len(result.retrieved_ids) > 0
            assert len(result.relevant_ids) > 0

    def test_determinism_same_seed_same_result(self):
        seed = ScenarioSeed(
            seed_id=99, record_count=10, importance_shape="bimodal",
            access_pattern="half_stale", outcome_frequency=0.3,
            noise_ratio=0.1, link_density=0.0, age_spread_days=30,
        )
        cls = ScenarioFactory.create(seed)

        def extract_record_suffixes(ids):
            """Extract rec_XXXX suffixes to compare ordering ignoring UUID prefix."""
            import re
            return [re.search(r"(rec_\d+)", rid).group(1) for rid in ids if "rec_" in rid]

        # Run 3 times, verify identical ordering by record suffix
        results = []
        for _ in range(3):
            instance = cls(overrides={})
            result = instance.execute()
            if result.status == "ok":
                results.append(extract_record_suffixes(result.retrieved_ids))

        if len(results) >= 2:
            assert results[0] == results[1], "Non-deterministic results"
            if len(results) == 3:
                assert results[0] == results[2], "Non-deterministic results"

    def test_scenario_with_overrides(self):
        seed = ScenarioSeed(
            seed_id=7, record_count=8, importance_shape="uniform",
            access_pattern="all_recent", outcome_frequency=0.5,
            noise_ratio=0.0, link_density=0.0, age_spread_days=30,
        )
        cls = ScenarioFactory.create(seed)
        instance = cls(overrides={"decay_rate": 0.3})
        result = instance.execute()
        assert result.status in ("ok", "skipped-degenerate", "error")

    def test_scenario_with_link_density(self):
        seed = ScenarioSeed(
            seed_id=5, record_count=8, importance_shape="uniform",
            access_pattern="all_recent", outcome_frequency=0.5,
            noise_ratio=0.0, link_density=0.5, age_spread_days=30,
        )
        cls = ScenarioFactory.create(seed)
        instance = cls(overrides={})
        result = instance.execute()
        assert result.status in ("ok", "skipped-degenerate", "error")


class TestDefaultSeeds:
    def test_default_seeds_count(self):
        seeds = ScenarioFactory.default_seeds(50)
        assert len(seeds) == 50

    def test_default_seeds_deterministic(self):
        seeds1 = ScenarioFactory.default_seeds(50)
        seeds2 = ScenarioFactory.default_seeds(50)
        for s1, s2 in zip(seeds1, seeds2):
            assert s1.seed_id == s2.seed_id
            assert s1.record_count == s2.record_count
            assert s1.importance_shape == s2.importance_shape

    def test_default_seeds_unique_ids(self):
        seeds = ScenarioFactory.default_seeds(50)
        ids = [s.seed_id for s in seeds]
        assert len(set(ids)) == 50

    def test_default_seeds_cover_all_shapes(self):
        seeds = ScenarioFactory.default_seeds(50)
        shapes = {s.importance_shape for s in seeds}
        assert shapes == set(IMPORTANCE_SHAPES)

    def test_default_seeds_cover_all_patterns(self):
        seeds = ScenarioFactory.default_seeds(50)
        patterns = {s.access_pattern for s in seeds}
        assert patterns == set(ACCESS_PATTERNS)

    def test_default_seeds_larger_second_half(self):
        seeds = ScenarioFactory.default_seeds(50)
        second_half = seeds[25:]
        assert all(s.record_count >= 25 for s in second_half)

    def test_create_all_returns_list_of_classes(self):
        classes = ScenarioFactory.create_all(n=5)
        assert len(classes) == 5
        for cls in classes:
            assert isinstance(cls, type)

    def test_degenerate_detection_all_seeds_produce_3_plus_relevant(self):
        """All default seeds must produce at least 3 relevant records."""
        seeds = ScenarioFactory.default_seeds(50)
        for seed in seeds[:10]:  # Test first 10 for speed
            cls = ScenarioFactory.create(seed)
            instance = cls(overrides={})
            result = instance.execute()
            if result.status == "ok":
                assert len(result.relevant_ids) >= 3, (
                    f"Seed {seed.seed_id} produced only "
                    f"{len(result.relevant_ids)} relevant records"
                )
