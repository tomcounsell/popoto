"""Tests for the ratchet loop.

Validates RatchetLoop, cliff safety margin, accept/reject logic,
and summary report format.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from tests.benchmarks.ratchet import RatchetDecision, RatchetLoop, RatchetSummary


class TestRatchetDecision:
    def test_accept_decision(self):
        d = RatchetDecision(
            constant_name="decay_rate",
            current_value=0.5,
            proposed_value=0.3,
            train_delta=0.08,
            validation_delta=0.05,
            action="accept",
            reason="Validated improvement",
        )
        assert d.action == "accept"
        assert d.validation_delta > 0

    def test_reject_decision(self):
        d = RatchetDecision(
            constant_name="TD_ALPHA",
            current_value=0.1,
            proposed_value=0.05,
            train_delta=0.03,
            validation_delta=-0.01,
            action="reject",
            reason="No validation improvement",
        )
        assert d.action == "reject"

    def test_cliff_warning(self):
        d = RatchetDecision(
            constant_name="decay_factor",
            current_value=0.95,
            proposed_value=0.85,
            train_delta=0.06,
            validation_delta=0.04,
            action="accept",
            reason="Validated",
            cliff_warning=True,
            cliff_margin=0.05,
        )
        assert d.cliff_warning is True
        assert d.cliff_margin == 0.05


class TestRatchetSummary:
    def test_format_report_empty(self):
        summary = RatchetSummary()
        report = summary.format_report()
        assert "RATCHET SUMMARY" in report
        assert "Accepted: 0" in report
        assert "Rejected: 0" in report

    def test_format_report_with_decisions(self):
        accepted = RatchetDecision(
            constant_name="decay_rate",
            current_value=0.5,
            proposed_value=0.3,
            train_delta=0.08,
            validation_delta=0.05,
            action="accept",
            reason="Validated",
        )
        rejected = RatchetDecision(
            constant_name="TD_ALPHA",
            current_value=0.1,
            proposed_value=0.05,
            train_delta=0.03,
            validation_delta=-0.01,
            action="reject",
            reason="No validation improvement",
        )
        no_sens = RatchetDecision(
            constant_name="TD_GAMMA",
            current_value=0.95,
            proposed_value=0.95,
            train_delta=0.001,
            validation_delta=0.0,
            action="no_sensitivity",
            reason="No measurable sensitivity",
        )

        summary = RatchetSummary(
            decisions=[accepted, rejected, no_sens],
            accepted=[accepted],
            rejected=[rejected],
            no_sensitivity=[no_sens],
            duration_seconds=12.5,
        )

        report = summary.format_report()
        assert "Accepted: 1" in report
        assert "Rejected: 1" in report
        assert "No sensitivity: 1" in report
        assert "decay_rate" in report
        assert "TD_ALPHA" in report
        assert "TD_GAMMA" in report
        assert "ACCEPT" in report
        assert "REJECT" in report
        assert "12.5s" in report


class TestRatchetLoop:
    def test_ratchet_with_single_constant(self):
        """Run ratchet on a single constant to verify the pipeline works."""
        loop = RatchetLoop(
            n_seeds=10,
            improvement_threshold=0.01,
        )
        # Run with just one constant for speed
        summary = loop.run(tiers={"decay_rate": [0.3, 0.5, 0.7]})
        assert len(summary.decisions) == 1
        assert summary.decisions[0].constant_name == "decay_rate"
        assert summary.decisions[0].action in ("accept", "reject", "no_sensitivity")
        assert summary.duration_seconds > 0

    def test_ratchet_decisions_are_complete(self):
        """Each decision has all required fields populated."""
        loop = RatchetLoop(n_seeds=10)
        summary = loop.run(
            tiers={
                "decay_rate": [0.3, 0.5, 0.7],
                "initial_confidence": [0.3, 0.5, 0.7],
            }
        )
        assert len(summary.decisions) == 2
        for d in summary.decisions:
            assert d.constant_name != ""
            assert d.action in ("accept", "reject", "no_sensitivity")
            assert d.reason != ""

    def test_ratchet_categorizes_correctly(self):
        """Accepted + rejected + no_sensitivity should equal total decisions."""
        loop = RatchetLoop(n_seeds=10)
        summary = loop.run(
            tiers={
                "decay_rate": [0.3, 0.5, 0.7],
                "_wf_min_threshold": [0.1, 0.2, 0.3],
            }
        )
        total = len(summary.accepted) + len(summary.rejected) + len(summary.no_sensitivity)
        assert total == len(summary.decisions)

    def test_ratchet_report_is_string(self):
        loop = RatchetLoop(n_seeds=10)
        summary = loop.run(tiers={"decay_rate": [0.3, 0.5, 0.7]})
        report = summary.format_report()
        assert isinstance(report, str)
        assert len(report) > 50
