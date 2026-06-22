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
import time
from decimal import Decimal

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


# ---------------------------------------------------------------------------
# Q-value Storage-Slot Separation: Regression + Round-Trip Tests
# Issue #410: PolicyCache Q-value / Decay-Timestamp Storage-Slot Separation
# ---------------------------------------------------------------------------


class TestQValueStorageSlotSeparation:
    """Regression tests for #410: Q-value stored in model hash, decay timestamp
    stored in ZSET score.  These tests verify the two slots stay independent."""

    def setup_method(self):
        """Clean up before each test."""
        _clean_all()

    def teardown_method(self):
        """Clean up after each test."""
        _clean_all()

    # -------------------------------------------------------------------------
    # 1. Save-after-learn: Q survives save()
    # -------------------------------------------------------------------------

    def test_q_value_survives_save(self):
        """Q-value written by update_q_value survives a subsequent save()."""
        fp = compute_fingerprint({"task": "survives_save"})
        policy = PolicyEntry(
            agent_id="agent-sv",
            state_fingerprint=fp,
            state_features={"task": "survives_save"},
            action_type="compile",
            action_spec={},
        )
        policy.save()
        initialize_q_value(policy, initial_q=0.0)

        # Learn some Q
        update_q_value(policy, reward=2.0, max_future_q=0.0)
        # current_q=0, alpha=0.1, reward=2.0 -> new_q=0.2

        # Reload and mutate an unrelated field, then save again
        reloaded = PolicyEntry.query.filter(
            agent_id="agent-sv",
            state_fingerprint=fp,
            action_type="compile",
        ).first()
        assert reloaded is not None

        reloaded.action_spec = {"updated": True}
        reloaded.save()

        # Reload again — q_value must not be overwritten
        after_save = PolicyEntry.query.filter(
            agent_id="agent-sv",
            state_fingerprint=fp,
            action_type="compile",
        ).first()
        assert after_save is not None
        assert isinstance(after_save.q_value, Decimal)
        assert abs(float(after_save.q_value) - 0.2) < 0.001, (
            f"Expected q_value ~0.2 after save(), got {after_save.q_value}"
        )

    # -------------------------------------------------------------------------
    # 2. Touch-after-learn: Q survives touch()
    # -------------------------------------------------------------------------

    def test_q_value_survives_touch(self):
        """Q-value survives touch('expected_value') — touch only updates ZSET timestamp."""
        fp = compute_fingerprint({"task": "survives_touch"})
        policy = PolicyEntry(
            agent_id="agent-touch",
            state_fingerprint=fp,
            state_features={"task": "survives_touch"},
            action_type="deploy",
            action_spec={},
        )
        policy.save()
        initialize_q_value(policy, initial_q=0.0)
        update_q_value(policy, reward=1.5)
        # new_q = 0 + 0.1 * 1.5 = 0.15

        # Touch the decay field (refreshes ZSET timestamp, must NOT change hash)
        policy.touch("expected_value")

        # Reload and verify q_value is unchanged
        reloaded = PolicyEntry.query.filter(
            agent_id="agent-touch",
            state_fingerprint=fp,
            action_type="deploy",
        ).first()
        assert reloaded is not None
        assert isinstance(reloaded.q_value, Decimal)
        assert abs(float(reloaded.q_value) - 0.15) < 0.001, (
            f"Expected q_value ~0.15 after touch(), got {reloaded.q_value}"
        )

    # -------------------------------------------------------------------------
    # 3. "acted" outcome: Q survives ObservationProtocol acted handler
    # -------------------------------------------------------------------------

    def test_q_value_survives_acted_outcome(self):
        """Q-value survives ObservationProtocol 'acted' handler (which touches
        DecayingSortedFields)."""
        fp = compute_fingerprint({"task": "survives_acted"})
        policy = PolicyEntry(
            agent_id="agent-acted",
            state_fingerprint=fp,
            state_features={"task": "survives_acted"},
            action_type="observe_and_act",
            action_spec={},
        )
        policy.save()
        initialize_q_value(policy, initial_q=0.0)
        update_q_value(policy, reward=3.0)
        # new_q = 0 + 0.1 * 3.0 = 0.3

        # Apply "acted" outcome — this touches all DecayingSortedFields via
        # _apply_acted(), which must NOT overwrite the q_value hash slot
        pk = policy.db_key.redis_key
        ObservationProtocol.on_context_used([policy], {pk: "acted"})

        # Reload and verify q_value survived
        reloaded = PolicyEntry.query.filter(
            agent_id="agent-acted",
            state_fingerprint=fp,
            action_type="observe_and_act",
        ).first()
        assert reloaded is not None
        assert isinstance(reloaded.q_value, Decimal)
        assert abs(float(reloaded.q_value) - 0.3) < 0.001, (
            f"Expected q_value ~0.3 after acted, got {reloaded.q_value}"
        )

    # -------------------------------------------------------------------------
    # 4. Lua<->Python encoding round-trip
    # -------------------------------------------------------------------------

    def test_q_value_encoding_round_trip(self):
        """Q written by Lua (update_q_value) reads back via Python as Decimal
        with the correct value."""
        test_cases = [
            # (initial_q, reward, max_future_q, expected_new_q)
            # new_q = current_q + alpha * (reward + gamma * max_future_q - current_q)
            (0.0, 0.0, 0.0, 0.0),       # Zero stays zero
            (0.0, 5.0, 0.0, 0.5),       # 0 + 0.1 * 5.0
            (0.0, -1.5, 0.0, -0.15),    # Negative reward -> negative Q
            (0.0, 0.25, 0.0, 0.025),    # Fractional precision
        ]

        for idx, (initial_q, reward, max_future_q, expected_q) in enumerate(test_cases):
            fp = compute_fingerprint({"task": f"roundtrip_{idx}"})
            policy = PolicyEntry(
                agent_id="agent-rt",
                state_fingerprint=fp,
                state_features={"task": f"roundtrip_{idx}"},
                action_type=f"action_{idx}",
                action_spec={},
            )
            policy.save()
            initialize_q_value(policy, initial_q=initial_q)

            if reward != 0.0 or max_future_q != 0.0:
                update_q_value(policy, reward=reward, max_future_q=max_future_q)

            reloaded = PolicyEntry.query.filter(
                agent_id="agent-rt",
                state_fingerprint=fp,
                action_type=f"action_{idx}",
            ).first()
            assert reloaded is not None, f"Case {idx}: policy not found"
            assert isinstance(reloaded.q_value, Decimal), (
                f"Case {idx}: expected Decimal, got {type(reloaded.q_value)}"
            )
            assert abs(float(reloaded.q_value) - expected_q) < 0.001, (
                f"Case {idx}: expected q_value ~{expected_q}, got {reloaded.q_value}"
            )

    # -------------------------------------------------------------------------
    # 5. Negative-base ranking
    # -------------------------------------------------------------------------

    def test_negative_q_value_ranking(self):
        """Negative Q ranks below positive Q of equal recency; zero Q ranks
        below positive Q but above (i.e. less negative than) negative Q."""
        agent = "agent-rank"
        state_fp = compute_fingerprint({"task": "ranking_test"})

        # Create 3 policies with same recency but different q_values
        q_configs = [
            ("action-neg", -0.4),
            ("action-zero", 0.0),
            ("action-pos", 0.6),
        ]
        for action, q_val in q_configs:
            fp = compute_fingerprint({"task": "ranking_test", "action": action})
            policy = PolicyEntry(
                agent_id=agent,
                state_fingerprint=fp,
                state_features={"task": "ranking_test"},
                action_type=action,
                action_spec={},
                q_value=Decimal(str(q_val)),
            )
            policy.save()

        # Query top by decay for this agent
        results = PolicyEntry.query.filter(agent_id=agent).top_by_decay(
            "expected_value", n=10
        )
        assert len(results) == 3

        action_order = [r.action_type for r in results]
        pos_idx = action_order.index("action-pos")
        zero_idx = action_order.index("action-zero")
        neg_idx = action_order.index("action-neg")

        assert pos_idx < neg_idx, (
            f"Positive Q must rank above negative Q. Got order: {action_order}"
        )
        # zero Q with base_score=0.0 → decayed_score = 0 * decay = 0
        # neg Q with base_score=-0.4 → decayed_score < 0
        # So zero should rank above negative
        assert zero_idx < neg_idx, (
            f"Zero Q must rank above negative Q. Got order: {action_order}"
        )

    # -------------------------------------------------------------------------
    # 6. Rank derives from stored Q, not 1.0 fallback
    # -------------------------------------------------------------------------

    def test_rank_derives_from_stored_q_value(self):
        """Decayed rank derives from stored q_value, not a silent 1.0 fallback."""
        agent = "agent-q-rank"
        configs = [
            ("high-q-action", 0.6),
            ("low-q-action", 0.2),
        ]
        for action, q_val in configs:
            fp = compute_fingerprint({"task": "q_rank", "action": action})
            policy = PolicyEntry(
                agent_id=agent,
                state_fingerprint=fp,
                state_features={"task": "q_rank"},
                action_type=action,
                action_spec={},
                q_value=Decimal(str(q_val)),
            )
            policy.save()

        results = PolicyEntry.query.filter(agent_id=agent).top_by_decay(
            "expected_value", n=10
        )
        assert len(results) == 2

        first_action = results[0].action_type
        assert first_action == "high-q-action", (
            f"Expected high-q-action first, got {first_action}. "
            "If both ranked the same, the 1.0 fallback may have been used."
        )

    # -------------------------------------------------------------------------
    # 7. Race-3 interleaving tests
    # -------------------------------------------------------------------------

    def test_td_update_then_save_q_survives(self):
        """After TD update, saving with unrelated field change does not reset
        q_value (Race 3 Order 1: save → update_q_value → reload → save)."""
        fp = compute_fingerprint({"task": "race3_order1"})
        policy = PolicyEntry(
            agent_id="agent-race3a",
            state_fingerprint=fp,
            state_features={"task": "race3_order1"},
            action_type="race_action",
            action_spec={"v": 1},
        )
        policy.save()
        initialize_q_value(policy, initial_q=0.0)

        # TD update writes q_value to hash via Lua
        update_q_value(policy, reward=1.0)
        # expected new_q = 0.1

        # Reload, mutate unrelated field, save again
        reloaded = PolicyEntry.query.filter(
            agent_id="agent-race3a",
            state_fingerprint=fp,
            action_type="race_action",
        ).first()
        assert reloaded is not None

        reloaded.action_spec = {"v": 2}
        reloaded.save()

        # Verify q_value survived the second save
        after = PolicyEntry.query.filter(
            agent_id="agent-race3a",
            state_fingerprint=fp,
            action_type="race_action",
        ).first()
        assert after is not None
        assert isinstance(after.q_value, Decimal)
        assert abs(float(after.q_value) - 0.1) < 0.001, (
            f"Race 3 Order 1: expected q_value ~0.1, got {after.q_value}"
        )

    def test_save_then_td_then_save_no_reset(self):
        """save → td_update → reload → save(unrelated mutation) preserves the
        TD-written Q (Race 3 Order 2)."""
        fp = compute_fingerprint({"task": "race3_order2"})
        policy = PolicyEntry(
            agent_id="agent-race3b",
            state_fingerprint=fp,
            state_features={"task": "race3_order2"},
            action_type="race_action_b",
            action_spec={"v": 1},
        )
        # First save establishes the record
        policy.save()
        initialize_q_value(policy, initial_q=0.0)

        # TD update (Lua writes q_value into hash)
        update_q_value(policy, reward=2.0)
        # expected new_q = 0 + 0.1 * 2.0 = 0.2

        # Reload, mutate unrelated field, save
        reloaded = PolicyEntry.query.filter(
            agent_id="agent-race3b",
            state_fingerprint=fp,
            action_type="race_action_b",
        ).first()
        assert reloaded is not None

        reloaded.action_spec = {"v": 3}
        reloaded.save()

        # Final reload — TD Q must persist
        final = PolicyEntry.query.filter(
            agent_id="agent-race3b",
            state_fingerprint=fp,
            action_type="race_action_b",
        ).first()
        assert final is not None
        assert isinstance(final.q_value, Decimal)
        assert abs(float(final.q_value) - 0.2) < 0.001, (
            f"Race 3 Order 2: expected q_value ~0.2, got {final.q_value}"
        )

    # -------------------------------------------------------------------------
    # 8. Edge cases
    # -------------------------------------------------------------------------

    def test_td_update_nil_q_treated_as_zero(self):
        """TD update on an instance with no prior q_value initialization treats
        current Q as 0 (default field value acts as zero baseline)."""
        fp = compute_fingerprint({"task": "nil_q"})
        policy = PolicyEntry(
            agent_id="agent-nil",
            state_fingerprint=fp,
            state_features={"task": "nil_q"},
            action_type="nil_action",
            action_spec={},
            # q_value not set → uses DecimalField default=Decimal("0")
        )
        policy.save()

        # Do NOT call initialize_q_value — rely on default
        reward = 4.0
        td_error = update_q_value(policy, reward=reward, max_future_q=0.0)

        # With current_q=0 (from Decimal default in hash): td_error = reward = 4.0
        # new_q = 0 + alpha * 4.0 = 0.4
        assert abs(td_error - reward) < 0.001, (
            f"Expected td_error={reward} when current_q=0, got {td_error}"
        )

        reloaded = PolicyEntry.query.filter(
            agent_id="agent-nil",
            state_fingerprint=fp,
            action_type="nil_action",
        ).first()
        assert reloaded is not None
        expected_new_q = 0.1 * reward  # alpha=0.1, current_q=0
        assert abs(float(reloaded.q_value) - expected_new_q) < 0.001, (
            f"Expected q_value ~{expected_new_q}, got {reloaded.q_value}"
        )

    def test_decay_rank_with_missing_q_value(self):
        """A PolicyEntry with no explicit q_value still decay-ranks (base 1.0
        fallback path in Lua — no error raised, entry appears in results)."""
        fp = compute_fingerprint({"task": "no_q_rank"})
        policy = PolicyEntry(
            agent_id="agent-noq",
            state_fingerprint=fp,
            state_features={"task": "no_q_rank"},
            action_type="no_q_action",
            action_spec={},
            # q_value not set — default Decimal("0") will be stored
        )
        policy.save()

        # Should not raise; entry must appear in results
        results = PolicyEntry.query.filter(agent_id="agent-noq").top_by_decay(
            "expected_value", n=10
        )
        assert len(results) >= 1, (
            "Expected at least one result even with default q_value"
        )
        found_actions = [r.action_type for r in results]
        assert "no_q_action" in found_actions
