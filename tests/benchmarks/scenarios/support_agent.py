"""Support Agent scenario for Tier 4 benchmarks.

Simulates a customer support agent accumulating context over a
billing dispute conversation. Tests extraction noise filtering,
importance ranking, and token budget efficiency.
"""

from .recipe_base import RecipeScenario


class SupportAgentScenario(RecipeScenario):
    """Support agent handling a billing issue conversation."""

    name = "support_agent"
    fixture_file = "support_agent.json"
