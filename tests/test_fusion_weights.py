"""Tests for the query-adaptive RRF fusion weighting policy (issue #457).

Covers:
- `_fusion_weights()` is a pure function of query text -- deterministic,
  no ML, no server round-trip.
- Token/name/date-specific queries (LoCoMo's shape) map to the BM25-lean
  regime.
- Paraphrastic queries (LongMemEval-S's shape) map to the vector-lean
  regime.
- Mixed-specificity queries map to the neutral regime.
- Empty/degenerate query text does not raise.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.recipes.context_assembler import (  # noqa: E402
    FUSION_REGIME_KEYWORD_LEAN,
    FUSION_REGIME_NEUTRAL,
    FUSION_REGIME_VECTOR_LEAN,
    FUSION_WEIGHT_GRAPH,
    _fusion_weights,
)


class TestFusionWeightsRegimeSelection:
    def test_name_date_query_is_keyword_lean(self):
        """A name/date-heavy query (LoCoMo shape) leans BM25."""
        weights = _fusion_weights("What did Sarah do on March 3, 2023?")
        assert weights == FUSION_REGIME_KEYWORD_LEAN
        assert weights["keyword"] > weights["vector"]

    def test_quoted_token_query_leans_more_keyword_than_paraphrastic(self):
        """A quoted-exact-token query is more BM25-leaning than a fully
        paraphrastic query with no specificity signal at all."""
        quoted = _fusion_weights('What did she mean by "burnt out"?')
        paraphrastic = _fusion_weights(
            "how did the person feel about their career change"
        )
        assert quoted["keyword"] / quoted["vector"] > (
            paraphrastic["keyword"] / paraphrastic["vector"]
        )

    def test_paraphrastic_query_is_vector_lean(self):
        """A low-specificity, paraphrastic query (LongMemEval-S shape) leans vector."""
        weights = _fusion_weights("how did the person feel about their career change")
        assert weights == FUSION_REGIME_VECTOR_LEAN
        assert weights["vector"] > weights["keyword"]

    def test_mixed_specificity_query_is_neutral(self):
        """A query with a middling specificity fraction lands in the neutral regime."""
        weights = _fusion_weights("What did John and Mary discuss during dinner")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_empty_query_does_not_raise(self):
        """Empty query text returns the neutral regime rather than raising."""
        weights = _fusion_weights("")
        assert weights == FUSION_REGIME_NEUTRAL

    def test_returns_new_dict_not_shared_mutable_reference(self):
        """Callers mutate the returned dict (adding 'graph'); must not alias
        the module-level constant."""
        weights = _fusion_weights("What did Sarah do on March 3, 2023?")
        weights["graph"] = FUSION_WEIGHT_GRAPH
        assert "graph" not in FUSION_REGIME_KEYWORD_LEAN

    def test_deterministic(self):
        """Same input always produces the same output (no randomness, no ML)."""
        text = "What restaurant did Alex visit on 2023-04-01?"
        assert _fusion_weights(text) == _fusion_weights(text)

    def test_capitalized_quoted_phrase_not_double_counted(self):
        """A capitalized proper noun inside a quoted span must be counted once
        (via the quoted-span pass), not again via the per-token capitalization
        pass -- regression test for a PR #479 review finding. A quoted phrase
        should not, by itself, swing an otherwise-paraphrastic query all the
        way into the keyword-lean regime just because the quoted words happen
        to be capitalized."""
        lowercase_quoted = _fusion_weights('What did she mean by "burnt out"?')
        capitalized_quoted = _fusion_weights('What did "Coach Ramirez" say?')

        # Both quote a two-word span out of a 7-token query (2/7 ≈ 0.286);
        # capitalization of the quoted words must not inflate the fraction
        # further, so both land in the same regime.
        assert lowercase_quoted == capitalized_quoted
        assert capitalized_quoted != FUSION_REGIME_KEYWORD_LEAN
