"""Tests for PolicyCache recipe — learned action selection from crystallized patterns.

Tests cover:
- PolicyEntry model creation and field composition
- Q-value temporal difference update via Lua script
- Crystallization handler with Wilson CI thresholds
- Temporal pattern discovery with chi-squared test
- CompositeScoreQuery integration
- ObservationProtocol synergy (on_context_used)
- CoOccurrence linking between related policies
- ExistenceFilter (Bloom filter) pre-check
- Fingerprint generation with various configurations
- End-to-end: event -> crystallize -> query -> observe -> update -> re-query
"""

import asyncio
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.constants import TemporalPeriod  # noqa: E402
from src.popoto.fields.observation import ObservationProtocol  # noqa: E402
from src.popoto.recipes.policy_cache import (  # noqa: E402
    CHI_SQUARED_CRITICAL_VALUES,
    PolicyEntry,
    chi_squared_uniform,
    compute_fingerprint,
    crystallization_handler,
    initialize_q_value,
    temporal_discovery_handler,
    update_q_value,
    wilson_ci_lower,
)
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(pattern="*PolicyEntry*"):
    """Remove all Redis keys matching pattern.

    WARNING: Uses KEYS command which is O(N) — test-only, never copy to
    production code. Use SCAN for production key iteration.
    """
    keys = POPOTO_REDIS_DB.keys(pattern)
    if keys:
        POPOTO_REDIS_DB.delete(*keys)


def _clean_streams(pattern="stream:policy*"):
    """Remove stream keys (test-only, uses KEYS)."""
    keys = POPOTO_REDIS_DB.keys(pattern)
    if keys:
        POPOTO_REDIS_DB.delete(*keys)


def _clean_bloom(pattern="$EF:PolicyEntry*"):
    """Remove bloom filter keys (test-only, uses KEYS)."""
    keys = POPOTO_REDIS_DB.keys(pattern)
    if keys:
        POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    """Clean all PolicyEntry-related keys (test-only, uses KEYS)."""
    for pattern in [
        "*PolicyEntry*",
        "stream:policy*",
        "$EF:PolicyEntry*",
        "$PL:PolicyEntry*",
        "$SortedF:PolicyEntry*",
        "$ConfidencF:PolicyEntry*",
        "$CoOccurrF:PolicyEntry*",
        "$AccessT:PolicyEntry*",
    ]:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


# ---------------------------------------------------------------------------
# Test Class
# ---------------------------------------------------------------------------


class TestPolicyCache:
    """Tests for the PolicyCache recipe module."""

    def setup_method(self):
        """Clean up before each test."""
        _clean_all()

    def teardown_method(self):
        """Clean up after each test."""
        _clean_all()

    # --- Model Tests ---

    def test_policy_entry_creation(self):
        """Create PolicyEntry with all field types, verify save/load."""
        fp = compute_fingerprint({"task": "deploy", "env": "staging"})

        policy = PolicyEntry(
            agent_id="agent-1",
            state_fingerprint=fp,
            state_features={"task": "deploy", "env": "staging"},
            action_type="run_playbook",
            action_spec={"playbook": "deploy.yml"},
        )
        policy.save()

        # Verify it was saved
        assert policy.db_key.redis_key is not None

        # Load it back
        loaded = PolicyEntry.query.filter(
            agent_id="agent-1",
            state_fingerprint=fp,
            action_type="run_playbook",
        ).first()
        assert loaded is not None
        assert loaded.agent_id == "agent-1"
        assert loaded.state_fingerprint == fp
        assert loaded.action_type == "run_playbook"

    def test_q_value_update(self):
        """TD update Lua script changes sorted set score correctly."""
        fp = compute_fingerprint({"task": "build"})
        policy = PolicyEntry(
            agent_id="agent-1",
            state_fingerprint=fp,
            state_features={"task": "build"},
            action_type="compile",
            action_spec={},
        )
        policy.save()

        # Initialize Q-value to 0 (save() sets it to timestamp)
        initialize_q_value(policy, initial_q=0.0)

        # Initial Q-value update with positive reward
        td_error = update_q_value(policy, reward=1.0, max_future_q=0.0)
        # Starting Q=0, reward=1.0, alpha=0.1, gamma=0.95
        # td_error = 1.0 + 0.95*0 - 0 = 1.0
        # new_q = 0 + 0.1 * 1.0 = 0.1
        assert abs(td_error - 1.0) < 0.001

        # Second update should show learning
        td_error2 = update_q_value(policy, reward=1.0, max_future_q=0.5)
        # current_q=0.1, reward=1.0, gamma=0.95, max_future=0.5
        # td_error = 1.0 + 0.95*0.5 - 0.1 = 1.375
        assert abs(td_error2 - 1.375) < 0.001

    def test_q_value_update_requires_save(self):
        """update_q_value on saved instance works; initial Q must be set."""
        policy = PolicyEntry(
            agent_id="agent-1",
            state_fingerprint="abc",
            state_features={},
            action_type="test",
            action_spec={},
        )
        policy.save()
        initialize_q_value(policy, 0.0)
        td_error = update_q_value(policy, reward=0.5)
        # Q=0, reward=0.5 -> td_error = 0.5, new_q = 0.05
        assert abs(td_error - 0.5) < 0.001

    # --- Crystallization Tests ---

    def test_crystallization_from_events(self):
        """Events meeting threshold create a PolicyEntry."""
        fp = compute_fingerprint({"task": "test"})
        entries = []
        # Need 7+ successes for Wilson CI lower > 0.6 threshold
        for i in range(8):
            entries.append(
                (
                    f"entry-{i}",
                    {
                        "state_fingerprint": fp,
                        "action_type": "run_tests",
                        "outcome": "success",
                        "agent_id": "agent-1",
                        "state_features": json.dumps({"task": "test"}),
                        "action_spec": json.dumps({"cmd": "pytest"}),
                    },
                )
            )

        asyncio.run(crystallization_handler(entries))

        # Should have created a PolicyEntry
        results = PolicyEntry.query.filter(
            agent_id="agent-1",
            state_fingerprint=fp,
        ).all()
        assert len(results) >= 1
        assert results[0].action_type == "run_tests"

    def test_crystallization_threshold(self):
        """Verify MIN_EVENTS gating — fewer events should not crystallize."""
        fp = compute_fingerprint({"task": "deploy"})

        # Only 2 events (below MIN_EVENTS_FOR_CRYSTALLIZATION=3)
        entries = [
            (
                f"entry-{i}",
                {
                    "state_fingerprint": fp,
                    "action_type": "deploy",
                    "outcome": "success",
                    "agent_id": "agent-2",
                },
            )
            for i in range(2)
        ]

        asyncio.run(crystallization_handler(entries))

        results = PolicyEntry.query.filter(
            agent_id="agent-2",
            state_fingerprint=fp,
        ).all()
        assert len(results) == 0

    def test_crystallization_low_success_rate(self):
        """Low success rate should not crystallize even with enough events."""
        fp = compute_fingerprint({"task": "flaky"})
        entries = []
        for i in range(10):
            entries.append(
                (
                    f"entry-{i}",
                    {
                        "state_fingerprint": fp,
                        "action_type": "flaky_action",
                        "outcome": "success" if i < 2 else "failure",
                        "agent_id": "agent-3",
                    },
                )
            )

        asyncio.run(crystallization_handler(entries))

        results = PolicyEntry.query.filter(
            agent_id="agent-3",
            state_fingerprint=fp,
        ).all()
        assert len(results) == 0

    def test_crystallization_skips_missing_fields(self):
        """Entries without state_fingerprint or action_type are skipped."""
        entries = [
            ("entry-1", {"action_type": "deploy", "outcome": "success"}),
            ("entry-2", {"state_fingerprint": "abc", "outcome": "success"}),
            ("entry-3", {}),
        ]

        # Should not raise
        asyncio.run(crystallization_handler(entries))

    def test_crystallization_empty_entries(self):
        """Empty entries batch is a no-op."""
        asyncio.run(crystallization_handler([]))

    # --- Composite Score / Query Tests ---

    def test_composite_score_query(self):
        """Query PolicyEntry via expected_value sorting."""
        # Create two policies with different Q-values
        for i, (action, reward) in enumerate(
            [("fast_deploy", 2.0), ("slow_deploy", 0.5)]
        ):
            fp = compute_fingerprint({"task": "deploy", "variant": str(i)})
            policy = PolicyEntry(
                agent_id="agent-q",
                state_fingerprint=fp,
                state_features={"task": "deploy"},
                action_type=action,
                action_spec={},
            )
            policy.save()
            initialize_q_value(policy, initial_q=0.0)
            update_q_value(policy, reward=reward)

        # Query by agent — both should be retrievable
        results = PolicyEntry.query.filter(agent_id="agent-q").all()
        assert len(results) == 2

    # --- Observation Protocol Tests ---

    def test_observation_updates_confidence(self):
        """ObservationProtocol.on_context_used with 'acted' updates confidence."""
        fp = compute_fingerprint({"task": "observe"})
        policy = PolicyEntry(
            agent_id="agent-obs",
            state_fingerprint=fp,
            state_features={"task": "observe"},
            action_type="observe_action",
            action_spec={},
        )
        policy.save()

        # Get initial confidence
        initial = ConfidenceField.get_confidence(policy, "confidence")

        # Simulate agent acting on this policy
        pk = policy.db_key.redis_key
        ObservationProtocol.on_context_used([policy], {pk: "acted"})

        # Confidence should increase after "acted" outcome
        updated = ConfidenceField.get_confidence(policy, "confidence")
        assert updated >= initial

    # --- Temporal Discovery Tests ---

    def test_temporal_discovery(self):
        """Events clustered at similar times produce discovered cycles."""
        # Create entries all on Mondays (tm_wday=0) to create a weekly pattern
        entries = []
        # Use timestamps that all fall on Monday
        # Monday Jan 6, 2025 = 1736121600 (a Monday)
        base_monday = 1736121600
        for i in range(20):
            # All Mondays, spread across 20 weeks
            ts = base_monday + (i * 7 * 86400)
            entries.append(
                (
                    f"entry-{i}",
                    {"ts": str(ts), "state_fingerprint": "temporal-test"},
                )
            )

        cycles = asyncio.run(temporal_discovery_handler(entries))

        # Should discover a weekly pattern (all events on same day-of-week)
        assert len(cycles) >= 1
        # At least one cycle should have WEEKLY period
        periods = [c[0] for c in cycles]
        assert TemporalPeriod.WEEKLY in periods

    def test_temporal_discovery_uniform(self):
        """Uniformly distributed timestamps should not produce cycles."""
        entries = []
        # Spread events evenly across all 7 days
        base = 1736121600  # Monday
        for i in range(70):
            ts = base + (i * 86400)  # One per day for 70 days
            entries.append((f"entry-{i}", {"ts": str(ts)}))

        cycles = asyncio.run(temporal_discovery_handler(entries))

        # Day-of-week should be roughly uniform — no significant weekly pattern
        # (70 events / 7 days = 10 per bucket — very uniform)
        weekly_cycles = [c for c in cycles if c[0] == TemporalPeriod.WEEKLY]
        assert len(weekly_cycles) == 0

    def test_temporal_discovery_insufficient_data(self):
        """Fewer than 3 timestamps should return empty."""
        entries = [("e1", {"ts": "1736121600"}), ("e2", {"ts": "1736208000"})]
        cycles = asyncio.run(temporal_discovery_handler(entries))
        assert cycles == []

    # --- Co-occurrence Tests ---

    def test_co_occurrence_linking(self):
        """Related policies strengthen their association."""
        fp1 = compute_fingerprint({"task": "build"})
        fp2 = compute_fingerprint({"task": "test"})

        p1 = PolicyEntry(
            agent_id="agent-co",
            state_fingerprint=fp1,
            state_features={"task": "build"},
            action_type="compile",
            action_spec={},
        )
        p1.save()

        p2 = PolicyEntry(
            agent_id="agent-co",
            state_fingerprint=fp2,
            state_features={"task": "test"},
            action_type="run_tests",
            action_spec={},
        )
        p2.save()

        # Link them via CoOccurrenceField
        pk1 = p1.db_key.redis_key
        pk2 = p2.db_key.redis_key
        PolicyEntry.related_policies.strengthen(PolicyEntry, pk1, pk2, delta=0.1)

        # Verify the edge exists via get_linked
        linked = PolicyEntry.related_policies.get_linked(PolicyEntry, pk1)
        linked_pks = [pk for pk, weight in linked]
        assert pk2 in linked_pks

    # --- Existence Filter Tests ---

    def test_existence_filter_precheck(self):
        """Bloom filter catches known fingerprints after save."""
        fp = compute_fingerprint({"task": "bloom-test"})

        # Before save, fingerprint should be missing
        assert PolicyEntry.bloom.definitely_missing(PolicyEntry, fp) is True

        policy = PolicyEntry(
            agent_id="agent-bloom",
            state_fingerprint=fp,
            state_features={"task": "bloom-test"},
            action_type="test",
            action_spec={},
        )
        policy.save()

        # After save, fingerprint should be present (might_exist = True)
        assert PolicyEntry.bloom.definitely_missing(PolicyEntry, fp) is False

    # --- Fingerprint Tests ---

    def test_fingerprint_generation(self):
        """compute_fingerprint produces consistent 16-char hex strings."""
        fp1 = compute_fingerprint({"task": "deploy", "env": "staging"})
        fp2 = compute_fingerprint({"task": "deploy", "env": "staging"})
        fp3 = compute_fingerprint({"task": "deploy", "env": "production"})

        # Same input = same output
        assert fp1 == fp2
        # Different input = different output
        assert fp1 != fp3
        # Format: 16 hex chars
        assert len(fp1) == 16
        assert all(c in "0123456789abcdef" for c in fp1)

    def test_fingerprint_include_fields(self):
        """include_fields selects which features are included."""
        full = compute_fingerprint({"task": "deploy", "env": "staging"})
        task_only = compute_fingerprint(
            {"task": "deploy", "env": "staging"}, include_fields=["task"]
        )
        # Different subsets = different fingerprints
        assert full != task_only

        # Same subset with different extra fields = same fingerprint
        task_only2 = compute_fingerprint(
            {"task": "deploy", "env": "production"}, include_fields=["task"]
        )
        assert task_only == task_only2

    def test_fingerprint_with_timestamp(self):
        """include_timestamp makes fingerprint time-dependent."""
        fp_no_ts = compute_fingerprint({"task": "deploy"})
        fp_with_ts = compute_fingerprint({"task": "deploy"}, include_timestamp=True)

        # With timestamp is different from without
        assert fp_no_ts != fp_with_ts

        # Two calls within same hour should be identical
        fp_with_ts2 = compute_fingerprint({"task": "deploy"}, include_timestamp=True)
        assert fp_with_ts == fp_with_ts2

    def test_fingerprint_empty_dict(self):
        """Empty dict produces a valid fingerprint."""
        fp = compute_fingerprint({})
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_fingerprint_include_fields_missing_key(self):
        """include_fields referencing missing keys raises KeyError."""
        with pytest.raises(KeyError):
            compute_fingerprint({"task": "deploy"}, include_fields=["nonexistent"])

    # --- Wilson CI Tests ---

    def test_wilson_ci_lower_basic(self):
        """Wilson CI lower bound with known values."""
        # All successes: lower bound should be high
        ci = wilson_ci_lower(10, 10)
        assert ci > 0.7

        # No successes: lower bound should be very low
        ci = wilson_ci_lower(0, 10)
        assert ci < 0.05

        # Empty: should be 0.0
        assert wilson_ci_lower(0, 0) == 0.0

    # --- Chi-squared Tests ---

    def test_chi_squared_uniform_basic(self):
        """Chi-squared against uniform with known distributions."""
        # Perfectly uniform: statistic should be 0
        uniform = [10, 10, 10, 10, 10, 10, 10]
        assert chi_squared_uniform(uniform, 10.0) == 0.0

        # Very skewed: statistic should be high
        skewed = [70, 0, 0, 0, 0, 0, 0]
        chi2 = chi_squared_uniform(skewed, 10.0)
        assert chi2 > CHI_SQUARED_CRITICAL_VALUES[6]  # df=6

    def test_chi_squared_zero_expected(self):
        """Chi-squared with zero expected returns 0.0."""
        assert chi_squared_uniform([1, 2, 3], 0.0) == 0.0

    # --- End-to-End Test ---

    def test_end_to_end(self):
        """Full path: create -> initialize -> update -> query -> verify."""
        fp = compute_fingerprint({"task": "e2e_test"})

        # Step 1: Create policy directly (simulating crystallization result)
        policy = PolicyEntry(
            agent_id="agent-e2e",
            state_fingerprint=fp,
            state_features={"task": "e2e_test"},
            action_type="e2e_action",
            action_spec={"step": "verify"},
        )
        policy.save()

        # Step 2: Initialize Q-value
        initialize_q_value(policy, initial_q=0.0)

        # Step 3: Update Q-value with reward (before observation, which
        # touches the decay clock and resets the sorted set score)
        td_error = update_q_value(policy, reward=1.0)
        assert isinstance(td_error, float)
        assert td_error > 0  # Positive reward should yield positive TD error

        # Step 4: Observe — agent acts on this policy (uses saved instance)
        pk = policy.db_key.redis_key
        ObservationProtocol.on_context_used([policy], {pk: "acted"})

        # Step 5: Verify confidence was updated
        conf = ConfidenceField.get_confidence(policy, "confidence")
        assert conf is not None
        assert conf >= 0.5  # Should be at least initial confidence

        # Step 6: Check bloom filter registered the fingerprint
        assert PolicyEntry.bloom.definitely_missing(PolicyEntry, fp) is False

        # Step 7: Re-query — policy should still be retrievable
        results = PolicyEntry.query.filter(
            agent_id="agent-e2e",
            state_fingerprint=fp,
        ).all()
        assert len(results) >= 1
        assert results[0].action_type == "e2e_action"

    def test_crystallization_then_query(self):
        """Crystallization handler creates queryable policies."""
        fp = compute_fingerprint({"task": "crystal_query"})

        entries = [
            (
                f"entry-{i}",
                {
                    "state_fingerprint": fp,
                    "action_type": "crystal_action",
                    "outcome": "success",
                    "agent_id": "agent-cq",
                    "state_features": json.dumps({"task": "crystal_query"}),
                    "action_spec": json.dumps({"cmd": "verify"}),
                },
            )
            for i in range(8)
        ]
        asyncio.run(crystallization_handler(entries))

        # Crystallized policy should be queryable
        results = PolicyEntry.query.filter(
            agent_id="agent-cq",
            state_fingerprint=fp,
        ).all()
        assert len(results) >= 1
        assert results[0].action_type == "crystal_action"
