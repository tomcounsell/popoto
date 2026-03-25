"""Research Agent scenario for Tier 4 benchmarks.

Simulates a research agent collecting facts from multiple sources.
Tests extraction quality across varied source quality, write filter
behavior with corroborated/contradicted facts, and score_weights
for ranking corroborated facts above unique ones.
"""

from .recipe_base import RecipeScenario


class ResearchAgentScenario(RecipeScenario):
    """Research agent collecting facts from multiple source documents."""

    name = "research_agent"
    fixture_file = "research_agent.json"
