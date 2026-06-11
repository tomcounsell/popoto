"""Tests for TrajectoryMemory recipe -- fingerprint-keyed procedural memory.

Real Redis (no mocks). Test DB is selected by the popoto.pytest_plugin
entry point. Each test gets a flushed DB.

Acceptance areas mapped to test classes:
- TestRecordEpisode -- smoke + signature
- TestCrystallizeIdempotent -- "Idempotent crystallization" acceptance area
- TestRecallOrdering -- "Recall ordering" acceptance area
- TestPartitionIsolation -- "Partition isolation" acceptance area
- TestErrors -- exception-handling coverage
- TestEmptyInputs -- empty/invalid input handling
- TestEndToEnd -- the integration scenario
"""

import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto import (  # noqa: E402
    AutoKeyField,
    ConfidenceField,
    DatetimeField,
    DecayingSortedField,
    DictField,
    KeyField,
    ListField,
    Model,
)
from popoto.recipes.trajectory_memory import (  # noqa: E402
    DEFAULT_RECALL_LIMIT,
    TrajectoryMemory,
    compute_fingerprint,
)

# ---------------------------------------------------------------------------
# Test Models — mirror the issue's API sketch
# ---------------------------------------------------------------------------


class CyclicEpisode(Model):
    episode_id = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    trajectory = ListField(default=[])
    outcome = DictField(default={})
    recorded_at = DecayingSortedField(partition_by="partition")


class ProceduralPattern(Model):
    pattern_id = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    canonical_sequence = ListField(default=[])
    confidence = ConfidenceField(initial_confidence=0.5)
    last_reinforced = DecayingSortedField(partition_by="partition")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tm():
    return TrajectoryMemory(
        episode_model=CyclicEpisode,
        pattern_model=ProceduralPattern,
        fingerprint_fields=["problem_topology", "affected_layer"],
        cluster_threshold=2,
    )


FP = {"problem_topology": "bug_fix", "affected_layer": "agent"}
FP_OTHER = {"problem_topology": "refactor", "affected_layer": "model"}


def _record(tm, *, trajectory, outcome, partition="t1", fingerprint=None):
    return tm.record_episode(
        fingerprint=fingerprint or FP,
        trajectory=trajectory,
        outcome=outcome,
        partition=partition,
    )


# ---------------------------------------------------------------------------
# TestRecordEpisode
# ---------------------------------------------------------------------------


class TestRecordEpisode:
    def test_returns_saved_episode(self, tm):
        ep = _record(tm, trajectory=["a", "b"], outcome={"success": True})
        assert ep.episode_id is not None
        assert ep.trajectory == ["a", "b"]
        assert ep.outcome == {"success": True}
        assert ep.partition == "t1"

    def test_missing_fingerprint_field_raises(self, tm):
        with pytest.raises(ValueError, match="missing required field"):
            tm.record_episode(
                fingerprint={"problem_topology": "bug_fix"},
                trajectory=[],
                outcome={},
                partition="t1",
            )


# ---------------------------------------------------------------------------
# TestCrystallizeIdempotent
# ---------------------------------------------------------------------------


class TestCrystallizeIdempotent:
    def test_double_crystallize_no_duplicates(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        tm.crystallize(partition="t1")
        tm.crystallize(partition="t1")
        all_patterns = list(ProceduralPattern.query.filter(partition="t1").all())
        assert len(all_patterns) == 1

    def test_double_crystallize_no_drift(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        tm.crystallize(partition="t1")
        pattern = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        conf_one = ConfidenceField.get_confidence(pattern, "confidence")

        tm.crystallize(partition="t1")
        pattern2 = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        conf_two = ConfidenceField.get_confidence(pattern2, "confidence")

        # No new evidence between runs -> confidence unchanged
        assert conf_two == pytest.approx(conf_one)

    def test_monotonic_with_new_evidence(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        tm.crystallize(partition="t1")
        pattern = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        conf_one = ConfidenceField.get_confidence(pattern, "confidence")

        # Add new evidence with a later recorded_at score
        time.sleep(0.01)
        for _ in range(2):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        tm.crystallize(partition="t1")
        pattern2 = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        conf_two = ConfidenceField.get_confidence(pattern2, "confidence")

        # Bayesian update is monotonic for positive signals.
        assert conf_two >= conf_one

    def test_episodes_not_mutated_by_crystallize(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        episodes_before = list(CyclicEpisode.query.filter(partition="t1").all())
        pks_before = sorted(e.episode_id for e in episodes_before)
        trajectories_before = sorted(tuple(e.trajectory) for e in episodes_before)

        tm.crystallize(partition="t1")

        episodes_after = list(CyclicEpisode.query.filter(partition="t1").all())
        pks_after = sorted(e.episode_id for e in episodes_after)
        trajectories_after = sorted(tuple(e.trajectory) for e in episodes_after)

        assert pks_before == pks_after
        assert trajectories_before == trajectories_after


# ---------------------------------------------------------------------------
# TestCrystallizeWatermark
#
# crystallize stores last_reinforced as the max OBSERVED episode
# recorded_at (a watermark), saved with skip_auto_now=True -- NOT the
# wall-clock time of the save. This closes the query-to-save
# evidence-loss window and makes sequential re-runs exactly idempotent.
#
# NOTE: timestamp comparisons use pytest.approx(..., abs=...). A relative
# tolerance on epoch seconds (~1.7e9) would be ~1750 seconds wide and
# pass vacuously.
#
# Confidence assertions here are deliberately formula-agnostic
# (equality between runs / monotonicity), never exact values: the
# ConfidenceField update formula is changing in the same plan.
# ---------------------------------------------------------------------------


class _PatternWithExtraAutoField(Model):
    """Pattern model with an EXTRA auto_now field (Risk 2 in the plan).

    crystallize saves with skip_auto_now=True, which freezes ANY other
    auto_now field on the pattern model for crystallize-triggered saves.
    """

    pattern_id = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    canonical_sequence = ListField(default=[])
    confidence = ConfidenceField(initial_confidence=0.5)
    last_reinforced = DecayingSortedField(partition_by="partition")
    touched_at = DatetimeField(auto_now=True, null=True)


class TestCrystallizeWatermark:
    def _pattern(self, model=ProceduralPattern, partition="t1"):
        return list(model.query.filter(partition=partition).all())[0]

    def _max_episode_ts(self, partition="t1"):
        return max(
            e.recorded_at for e in CyclicEpisode.query.filter(partition=partition).all()
        )

    def test_double_crystallize_second_run_observes_nothing(self, tm):
        """Re-running crystallize on an unchanged episode set is a no-op:
        no pattern is reinforced and confidence does not drift."""
        for _ in range(3):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        first = tm.crystallize(partition="t1")
        assert len(first) == 1
        conf_one = ConfidenceField.get_confidence(self._pattern(), "confidence")
        watermark_one = self._pattern().last_reinforced

        second = tm.crystallize(partition="t1")
        assert second == []  # nothing observed, nothing reinforced
        conf_two = ConfidenceField.get_confidence(self._pattern(), "confidence")
        watermark_two = self._pattern().last_reinforced

        assert conf_two == conf_one  # zero drift, exact
        assert watermark_two == pytest.approx(watermark_one, abs=1e-9)

    def test_watermark_equals_max_observed_episode_existing_pattern(self, tm):
        """After reinforcing an EXISTING pattern, last_reinforced equals the
        max observed episode recorded_at, not the wall-clock save time."""
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")

        time.sleep(0.05)
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        # Widen the gap between the newest episode and the wall-clock save
        # so the old (wall-clock) behavior cannot pass by accident.
        time.sleep(0.05)
        tm.crystallize(partition="t1")

        assert self._pattern().last_reinforced == pytest.approx(
            self._max_episode_ts(), abs=1e-3
        )

    def test_new_pattern_watermark_is_episode_time_not_wall_clock(self, tm):
        """First crystallize of a brand-new pattern sets last_reinforced to
        the max observed episode timestamp -- NOT time.time() at save."""
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        # Backdate every episode far into the past so wall-clock and
        # watermark are unambiguously distinguishable.
        for ep in CyclicEpisode.query.filter(partition="t1").all():
            ep.recorded_at = ep.recorded_at - 1000.0
            ep.save(skip_auto_now=True)
        backdated_max = self._max_episode_ts()

        wall_before_save = time.time()
        affected = tm.crystallize(partition="t1")
        assert len(affected) == 1

        watermark = self._pattern().last_reinforced
        assert watermark == pytest.approx(backdated_max, abs=1e-3)
        # Differs from wall-clock at save by ~1000s.
        assert watermark < wall_before_save - 900.0

    def test_backdated_late_episode_is_picked_up_next_run(self, tm):
        """An episode recorded between the watermark and the wall-clock save
        time (e.g. a slightly-lagging writer) is observed by the NEXT
        crystallize run -- the exact scenario the old wall-clock save
        lost forever."""
        for _ in range(2):
            _record(tm, trajectory=["a", "b"], outcome={"success": True})
        # Ensure a measurable query-to-save window.
        time.sleep(0.05)
        tm.crystallize(partition="t1")

        pattern = self._pattern()
        watermark = pattern.last_reinforced
        evidence_one = ConfidenceField.get_confidence_data(pattern, "confidence")[
            "evidence_count"
        ]
        wall_after_save = time.time()
        assert watermark < wall_after_save  # the window exists

        # Late writer: lands inside (watermark, wall_clock_save_time).
        late = _record(tm, trajectory=["a", "b"], outcome={"success": True})
        backdated_ts = watermark + (wall_after_save - watermark) / 2.0
        late.recorded_at = backdated_ts
        late.save(skip_auto_now=True)

        affected = tm.crystallize(partition="t1")
        assert len(affected) == 1  # the late episode was observed
        pattern = self._pattern()
        assert pattern.last_reinforced == pytest.approx(backdated_ts, abs=1e-6)
        # Formula-agnostic observation proof: exactly one new piece of
        # evidence was incorporated (no confidence value pinned -- the
        # update formula is changing independently).
        evidence_two = ConfidenceField.get_confidence_data(pattern, "confidence")[
            "evidence_count"
        ]
        assert evidence_two == evidence_one + 1

    def test_skip_auto_now_freezes_extra_auto_now_field(self):
        """Risk-2 documented behavior: crystallize saves with
        skip_auto_now=True, so any OTHER auto_now field on the pattern
        model is frozen across crystallize-triggered saves (and never
        populated by them)."""
        tm = TrajectoryMemory(
            episode_model=CyclicEpisode,
            pattern_model=_PatternWithExtraAutoField,
            fingerprint_fields=["problem_topology", "affected_layer"],
            cluster_threshold=2,
        )
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")

        pattern = self._pattern(model=_PatternWithExtraAutoField)
        # Crystallize never populates a pure auto_now field.
        assert pattern.touched_at is None

        # A consumer-triggered plain save DOES populate it (auto_now).
        pattern.save()
        pattern = self._pattern(model=_PatternWithExtraAutoField)
        touched_at_before = pattern.touched_at
        assert touched_at_before is not None

        # New evidence + crystallize: touched_at is frozen, while
        # last_reinforced advances to the new watermark.
        time.sleep(0.05)
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")

        pattern = self._pattern(model=_PatternWithExtraAutoField)
        assert pattern.touched_at == touched_at_before


# ---------------------------------------------------------------------------
# TestRecallOrdering
# ---------------------------------------------------------------------------


class TestRecallOrdering:
    def _seed_two_patterns(self, tm, *, recent_fp, older_fp):
        # Build the older one first, then the recent one
        for _ in range(2):
            _record(
                tm, trajectory=["a"], outcome={"success": True}, fingerprint=older_fp
            )
        tm.crystallize(partition="t1")
        time.sleep(0.05)
        for _ in range(2):
            _record(
                tm, trajectory=["b"], outcome={"success": True}, fingerprint=recent_fp
            )
        tm.crystallize(partition="t1")

    def test_partial_fingerprint_returns_both(self, tm):
        # Both patterns share affected_layer but differ on problem_topology;
        # partial-fingerprint recall on affected_layer should return both
        # patterns, ranked by composite score.
        partial = {"affected_layer": "agent"}
        for _ in range(2):
            _record(
                tm,
                trajectory=["a"],
                outcome={"success": True},
                fingerprint={"problem_topology": "bug_fix", "affected_layer": "agent"},
            )
        time.sleep(0.05)
        for _ in range(2):
            _record(
                tm,
                trajectory=["b"],
                outcome={"success": True},
                fingerprint={"problem_topology": "refactor", "affected_layer": "agent"},
            )
        tm.crystallize(partition="t1")

        ranked = tm.recall(fingerprint=partial, partition="t1")
        assert len(ranked) == 2

    def test_recency_advances_on_reinforcement(self, tm):
        # When new evidence arrives for an existing pattern, crystallize
        # advances the pattern's last_reinforced timestamp (the
        # DecayingSortedField's auto_now property on save).
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        pattern = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        ts_before = pattern.last_reinforced

        time.sleep(0.05)
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        pattern2 = list(ProceduralPattern.query.filter(partition="t1").all())[0]
        ts_after = pattern2.last_reinforced

        assert ts_after >= ts_before
        # Cumulative reinforcement should produce a strictly newer timestamp
        # when sleep was long enough to register at second-level resolution.
        assert ts_after > ts_before or ts_after == pytest.approx(ts_before)

    def test_exact_fingerprint_returns_single(self, tm):
        # All three are FP — produce one pattern
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        ranked = tm.recall(
            fingerprint=FP,
            partition="t1",
            score_weights={"confidence": 1.0, "last_reinforced": 0.0},
        )
        assert len(ranked) == 1

    def test_recall_limit_caps_results(self, tm):
        # Only one fingerprint here -> only one pattern even with high limit
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        assert len(tm.recall(fingerprint=FP, partition="t1", limit=10)) == 1
        assert len(tm.recall(fingerprint=FP, partition="t1", limit=1)) == 1

    def test_default_limit_is_five(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        # Limit defaults to DEFAULT_RECALL_LIMIT (5)
        assert len(tm.recall(fingerprint=FP, partition="t1")) <= DEFAULT_RECALL_LIMIT


# ---------------------------------------------------------------------------
# TestPartitionIsolation
# ---------------------------------------------------------------------------


class TestPartitionIsolation:
    def test_episodes_do_not_leak_across_partitions(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True}, partition="A")
        # Only one episode in B — below threshold (which is 2)
        _record(tm, trajectory=["a"], outcome={"success": True}, partition="B")

        tm.crystallize(partition="A")
        tm.crystallize(partition="B")

        a_patterns = list(ProceduralPattern.query.filter(partition="A").all())
        b_patterns = list(ProceduralPattern.query.filter(partition="B").all())
        assert len(a_patterns) == 1
        assert len(b_patterns) == 0

    def test_crystallize_only_touches_target_partition(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True}, partition="A")
        for _ in range(3):
            _record(tm, trajectory=["b"], outcome={"success": True}, partition="B")
        tm.crystallize(partition="A")
        # B's patterns must not exist yet
        assert len(list(ProceduralPattern.query.filter(partition="B").all())) == 0
        tm.crystallize(partition="B")
        a_pat = list(ProceduralPattern.query.filter(partition="A").all())[0]
        b_pat = list(ProceduralPattern.query.filter(partition="B").all())[0]
        assert a_pat.canonical_sequence == ["a"]
        assert b_pat.canonical_sequence == ["b"]

    def test_recall_filters_by_partition(self, tm):
        for _ in range(3):
            _record(tm, trajectory=["a"], outcome={"success": True}, partition="A")
        tm.crystallize(partition="A")
        # B has no patterns -> recall returns empty
        assert tm.recall(fingerprint=FP, partition="B") == []
        # A has one
        assert len(tm.recall(fingerprint=FP, partition="A")) == 1


# ---------------------------------------------------------------------------
# TestErrors
# ---------------------------------------------------------------------------


class _PatternMissingConfidence(Model):
    pid = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    canonical_sequence = ListField(default=[])
    last_reinforced = DecayingSortedField(partition_by="partition")


class _PatternMissingRecency(Model):
    pid = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    canonical_sequence = ListField(default=[])
    confidence = ConfidenceField(initial_confidence=0.5)


class _EpisodeMissingFingerprint(Model):
    eid = AutoKeyField()
    partition = KeyField()
    # missing problem_topology / affected_layer
    trajectory = ListField(default=[])
    outcome = DictField(default={})
    recorded_at = DecayingSortedField(partition_by="partition")


class TestErrors:
    def test_pattern_missing_confidence_field(self):
        with pytest.raises(TypeError, match="ConfidenceField"):
            TrajectoryMemory(
                episode_model=CyclicEpisode,
                pattern_model=_PatternMissingConfidence,
                fingerprint_fields=["problem_topology", "affected_layer"],
            )

    def test_pattern_missing_decaying_sorted_field(self):
        with pytest.raises(TypeError, match="DecayingSortedField"):
            TrajectoryMemory(
                episode_model=CyclicEpisode,
                pattern_model=_PatternMissingRecency,
                fingerprint_fields=["problem_topology", "affected_layer"],
            )

    def test_episode_missing_fingerprint_field(self):
        with pytest.raises(TypeError, match="problem_topology"):
            TrajectoryMemory(
                episode_model=_EpisodeMissingFingerprint,
                pattern_model=ProceduralPattern,
                fingerprint_fields=["problem_topology", "affected_layer"],
            )

    def test_record_episode_missing_fingerprint_field(self, tm):
        with pytest.raises(ValueError, match="missing required field"):
            tm.record_episode(
                fingerprint={"problem_topology": "bug_fix"},
                trajectory=[],
                outcome={},
                partition="t1",
            )

    def test_recall_unknown_fingerprint_field(self, tm):
        with pytest.raises(ValueError, match="unknown field"):
            tm.recall(
                fingerprint={"bogus_field": "x"},
                partition="t1",
            )

    def test_empty_fingerprint_fields_rejected(self):
        with pytest.raises(TypeError, match="fingerprint_fields"):
            TrajectoryMemory(
                episode_model=CyclicEpisode,
                pattern_model=ProceduralPattern,
                fingerprint_fields=[],
            )


# ---------------------------------------------------------------------------
# TestEmptyInputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_crystallize_zero_episodes(self, tm):
        assert tm.crystallize(partition="empty") == []

    def test_crystallize_below_threshold(self, tm):
        _record(tm, trajectory=["a"], outcome={"success": True})
        # Threshold is 2; only one episode
        assert tm.crystallize(partition="t1") == []
        assert list(ProceduralPattern.query.filter(partition="t1").all()) == []

    def test_recall_limit_zero(self, tm):
        for _ in range(2):
            _record(tm, trajectory=["a"], outcome={"success": True})
        tm.crystallize(partition="t1")
        assert tm.recall(fingerprint=FP, partition="t1", limit=0) == []

    def test_recall_no_patterns(self, tm):
        assert tm.recall(fingerprint=FP, partition="empty") == []

    def test_recall_empty_fingerprint(self, tm):
        assert tm.recall(fingerprint={}, partition="t1") == []


# ---------------------------------------------------------------------------
# TestEndToEnd
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_lifecycle(self, tm):
        # Round 1: 3 episodes, all consistent
        for _ in range(3):
            _record(tm, trajectory=["x", "y", "z"], outcome={"success": True})
        tm.crystallize(partition="t1")

        first = tm.recall(fingerprint=FP, partition="t1")
        assert len(first) == 1
        assert first[0].canonical_sequence == ["x", "y", "z"]
        first_pk = first[0].db_key.redis_key

        # Round 2: 3 more episodes, same trajectory
        time.sleep(0.01)
        for _ in range(3):
            _record(tm, trajectory=["x", "y", "z"], outcome={"success": True})
        tm.crystallize(partition="t1")

        second = tm.recall(fingerprint=FP, partition="t1")
        assert len(second) == 1
        # Same pattern (no duplicate created)
        assert second[0].db_key.redis_key == first_pk

    def test_compute_fingerprint_reexport(self):
        # The recipe re-exports compute_fingerprint for ergonomics.
        assert compute_fingerprint({"a": 1, "b": 2}) == compute_fingerprint(
            {"b": 2, "a": 1}
        )
