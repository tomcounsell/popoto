"""Coding Assistant scenario for Tier 4 benchmarks.

Simulates a development assistant tracking architecture decisions.
Tests confidence feedback on superseded decisions, score_weights
balance between recency and importance, and cross-reference handling.
"""

from .recipe_base import RecipeScenario


class CodingAssistantScenario(RecipeScenario):
    """Coding assistant tracking project architecture decisions."""

    name = "coding_assistant"
    fixture_file = "coding_assistant.json"
