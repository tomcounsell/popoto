"""Tests for InteractionWeight constants and combine() helper.

Tests cover:
- All source-axis constant values (HUMAN, AGENT, SYSTEM)
- All role-axis constant values (EXECUTIVE, MANAGER, PEER, SUBORDINATE)
- combine() returns sum of source + role
- InteractionWeight is importable from the top-level popoto package
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.fields.constants import InteractionWeight


class TestInteractionWeightConstants:
    """Verify all constant values are correct."""

    def test_human_value(self):
        assert InteractionWeight.HUMAN == 6.0

    def test_agent_value(self):
        assert InteractionWeight.AGENT == 1.0

    def test_system_value(self):
        assert InteractionWeight.SYSTEM == 0.2

    def test_executive_value(self):
        assert InteractionWeight.EXECUTIVE == 44.0

    def test_manager_value(self):
        assert InteractionWeight.MANAGER == 16.0

    def test_peer_value(self):
        assert InteractionWeight.PEER == 6.0

    def test_subordinate_value(self):
        assert InteractionWeight.SUBORDINATE == 1.0


class TestInteractionWeightCombine:
    """Verify combine() returns the sum of source and role."""

    def test_human_peer(self):
        """HUMAN + PEER = 6.0 + 6.0 = 12.0."""
        assert InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.PEER) == 12.0

    def test_agent_subordinate(self):
        """AGENT + SUBORDINATE = 1.0 + 1.0 = 2.0."""
        assert InteractionWeight.combine(InteractionWeight.AGENT, InteractionWeight.SUBORDINATE) == 2.0

    def test_human_executive(self):
        """HUMAN + EXECUTIVE = 6.0 + 44.0 = 50.0."""
        assert InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE) == 50.0

    def test_system_manager(self):
        """SYSTEM + MANAGER = 0.2 + 16.0 = 16.2."""
        assert InteractionWeight.combine(InteractionWeight.SYSTEM, InteractionWeight.MANAGER) == 16.2

    def test_combine_with_raw_floats(self):
        """combine() works with arbitrary float values."""
        assert InteractionWeight.combine(3.0, 7.0) == 10.0


class TestInteractionWeightImport:
    """Verify InteractionWeight is accessible from the top-level package."""

    def test_import_from_popoto(self):
        """from popoto import InteractionWeight works."""
        from src.popoto import InteractionWeight as IW

        assert IW is InteractionWeight

    def test_in_popoto_all(self):
        """InteractionWeight is listed in popoto.__all__."""
        import src.popoto as popoto

        assert "InteractionWeight" in popoto.__all__
