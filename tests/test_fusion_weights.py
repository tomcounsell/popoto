"""Tests for the query-adaptive RRF fusion weighting policy (issue #457).

Covers:
- `_fusion_weights()` is a pure function of query text -- deterministic,
  no ML, no server round-trip.
- Name/date/token-specific, non-first-person queries (LoCoMo's shape) map to
  the keyword-lean regime (vector weight 0 -> hybrid converges to lexical).
- First-person / paraphrastic queries (LongMemEval-S's shape) map to the
  neutral (unweighted RRF) regime, preserving the LongMemEval-S hybrid win.
- A first-person pronoun vetoes keyword-lean even when a name is present.
- Empty/degenerate query text does not raise.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.recipes.context_assembler import (  # noqa: E402
    FUSION_REGIME_KEYWORD_LEAN,
    FUSION_REGIME_NEUTRAL,
    FUSION_WEIGHT_GRAPH,
    _fusion_weights,
)


class TestFusionWeightsRegimeSelection:
    def test_name_anchored_query_is_keyword_lean(self):
        """A third-person, name-anchored query (LoCoMo's shape) drops the
        dense arm (vector weight 0)."""
        weights = _fusion_weights("When did Caroline go to the support group?")
        assert weights == FUSION_REGIME_KEYWORD_LEAN
        assert weights["vector"] == 0.0
        assert weights["keyword"] == 1.0

    def test_date_query_is_keyword_lean(self):
        """A date/digit-bearing, non-first-person query drops the dense arm."""
        weights = _fusion_weights("What happened on 2023-04-01 at the office")
        assert weights == FUSION_REGIME_KEYWORD_LEAN

    def test_quoted_token_query_is_keyword_lean(self):
        """A quoted exact-token query (non-first-person) drops the dense arm."""
        weights = _fusion_weights('What did the report call "burnt out"?')
        assert weights == FUSION_REGIME_KEYWORD_LEAN

    def test_first_person_paraphrastic_query_is_neutral(self):
        """A first-person self-recall query (LongMemEval-S's shape) keeps the
        dense arm at full weight (unweighted RRF)."""
        weights = _fusion_weights("What degree did I graduate with?")
        assert weights == FUSION_REGIME_NEUTRAL
        assert weights["keyword"] == weights["vector"] == 1.0

    def test_first_person_vetoes_keyword_lean_even_with_name(self):
        """A first-person pronoun keeps the query neutral even when a proper
        noun (e.g. a brand) is present -- this is LongMemEval-S's shape, where
        the dense arm helps and must not be dropped."""
        weights = _fusion_weights(
            "What is the name of the playlist I created on Spotify?"
        )
        assert weights == FUSION_REGIME_NEUTRAL

    def test_plain_paraphrastic_query_is_neutral(self):
        """A query with no name/date/quote and no first-person marker still
        falls through to neutral (unweighted RRF) rather than dropping the
        dense arm."""
        weights = _fusion_weights("how did the person feel about the change")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_interrogative_not_mistaken_for_name(self):
        """A capitalized interrogative or sentence-initial word must not be
        read as a proper-noun name."""
        # 'Where'/'What' are the only capitalized tokens; neither is a name.
        weights = _fusion_weights("Where did they go for the weekend")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_all_caps_acronym_not_mistaken_for_name(self):
        """An ALL-CAPS acronym is not a proper-noun name (would otherwise be
        an over-eager keyword-lean trigger)."""
        weights = _fusion_weights("did they discuss the LGBTQ topic")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_empty_query_does_not_raise(self):
        """Empty query text returns the neutral regime rather than raising."""
        weights = _fusion_weights("")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_returns_new_dict_not_shared_mutable_reference(self):
        """Callers mutate the returned dict (adding 'graph'); must not alias
        the module-level constant."""
        weights = _fusion_weights("When did Caroline go to the group?")
        weights["graph"] = FUSION_WEIGHT_GRAPH
        assert "graph" not in FUSION_REGIME_KEYWORD_LEAN

    def test_deterministic(self):
        """Same input always produces the same output (no randomness, no ML)."""
        text = "What restaurant did Alex visit on 2023-04-01?"
        assert _fusion_weights(text) == _fusion_weights(text)

    def test_keyword_lean_drops_vector_entirely(self):
        """The keyword-lean regime must set vector weight to exactly 0.0 so the
        hybrid RRF converges to the lexical result (the parity guarantee)."""
        assert FUSION_REGIME_KEYWORD_LEAN["vector"] == 0.0

    def test_neutral_is_unweighted(self):
        """The neutral regime must be plain unweighted RRF (all weights 1.0),
        the blend that produced the LongMemEval-S hybrid win."""
        assert FUSION_REGIME_NEUTRAL["keyword"] == 1.0
        assert FUSION_REGIME_NEUTRAL["vector"] == 1.0
