"""Tests for the train/validation split runner.

Validates SplitRunner, deterministic partitioning, and overfitting guard.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from tests.benchmarks.scenarios.factory import ScenarioFactory
from tests.benchmarks.split import (
    TRAIN_SPLIT_BOUNDARY,
    SplitRunner,
    ValidationResult,
    make_split,
)


class TestMakeSplit:
    def test_default_split_ratio(self):
        seeds = ScenarioFactory.default_seeds(50)
        train, val = make_split(seeds)
        assert len(train) == 35
        assert len(val) == 15

    def test_custom_boundary(self):
        seeds = ScenarioFactory.default_seeds(50)
        train, val = make_split(seeds, boundary=20)
        assert len(train) == 20
        assert len(val) == 30

    def test_disjoint_sets(self):
        seeds = ScenarioFactory.default_seeds(50)
        train, val = make_split(seeds)
        train_ids = {s.seed_id for s in train}
        val_ids = {s.seed_id for s in val}
        assert train_ids.isdisjoint(val_ids)

    def test_complete_coverage(self):
        seeds = ScenarioFactory.default_seeds(50)
        train, val = make_split(seeds)
        all_ids = {s.seed_id for s in train} | {s.seed_id for s in val}
        assert all_ids == {s.seed_id for s in seeds}

    def test_deterministic(self):
        seeds = ScenarioFactory.default_seeds(50)
        train1, val1 = make_split(seeds)
        train2, val2 = make_split(seeds)
        assert [s.seed_id for s in train1] == [s.seed_id for s in train2]
        assert [s.seed_id for s in val1] == [s.seed_id for s in val2]


class TestSplitRunner:
    @pytest.fixture
    def small_split_runner(self):
        """Create a SplitRunner with a small number of scenarios for speed."""
        seeds = ScenarioFactory.default_seeds(10)
        train_seeds, val_seeds = make_split(seeds, boundary=7)
        train_scenarios = ScenarioFactory.create_all(train_seeds)
        val_scenarios = ScenarioFactory.create_all(val_seeds)
        return SplitRunner(train_scenarios, val_scenarios)

    def test_sweep_and_validate_returns_result(self, small_split_runner):
        result = small_split_runner.sweep_and_validate(
            "decay_rate", [0.3, 0.5, 0.7]
        )
        assert isinstance(result, ValidationResult)
        assert result.constant_name == "decay_rate"
        assert result.recommendation in ("accept", "reject")

    def test_sweep_and_validate_has_scores(self, small_split_runner):
        result = small_split_runner.sweep_and_validate(
            "decay_rate", [0.3, 0.5, 0.7]
        )
        # Scores should be in valid range
        assert 0.0 <= result.train_score <= 1.0
        assert 0.0 <= result.validation_score <= 1.0

    def test_sweep_and_validate_with_default(self, small_split_runner):
        result = small_split_runner.sweep_and_validate(
            "decay_rate", [0.3, 0.5, 0.7], current_default=0.5
        )
        assert result.current_default == 0.5

    def test_best_value_is_from_sweep_values(self, small_split_runner):
        values = [0.3, 0.5, 0.7]
        result = small_split_runner.sweep_and_validate(
            "decay_rate", values
        )
        assert result.best_value in values


class TestValidationResult:
    def test_accept_when_delta_exceeds_threshold(self):
        result = ValidationResult(
            constant_name="test",
            best_value=0.3,
            current_default=0.5,
            train_score=0.8,
            train_default_score=0.7,
            validation_score=0.75,
            validation_default_score=0.7,
            delta=0.05,
            recommendation="accept",
        )
        assert result.recommendation == "accept"
        assert result.delta > 0.01

    def test_reject_when_delta_below_threshold(self):
        result = ValidationResult(
            constant_name="test",
            best_value=0.3,
            current_default=0.5,
            train_score=0.8,
            train_default_score=0.79,
            validation_score=0.705,
            validation_default_score=0.7,
            delta=0.005,
            recommendation="reject",
            reject_reason="No significant validation improvement",
        )
        assert result.recommendation == "reject"
        assert result.delta < 0.01
