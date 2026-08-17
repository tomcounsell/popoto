"""RawTurnExtractionProvider: one turn in, one verbatim fact out.

The provider exists because issue #489 measured sentence-splitting
extraction at 0.2078 judged accuracy against 0.3636 for raw ingestion. These
tests pin the behavior that produces that difference: no splitting, no
length filter, no rewriting.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.extraction import (  # noqa: E402
    AbstractExtractionProvider,
    ExtractedFact,
    HeuristicExtractionProvider,
    RawTurnExtractionProvider,
)

MULTI_SENTENCE = (
    "Deploys are blue-green. A failed health check rolls back automatically. "
    "The pipeline marks the deploy failed."
)


def test_is_an_extraction_provider():
    assert isinstance(RawTurnExtractionProvider(), AbstractExtractionProvider)


def test_single_fact_for_a_multi_sentence_turn():
    facts = RawTurnExtractionProvider().extract(MULTI_SENTENCE)
    assert len(facts) == 1
    assert isinstance(facts[0], ExtractedFact)


def test_no_sentence_splitting():
    """The explicit anti-assertion: raw must not do what heuristic does."""
    raw = RawTurnExtractionProvider().extract(MULTI_SENTENCE)
    heuristic = HeuristicExtractionProvider().extract(MULTI_SENTENCE)
    assert len(heuristic) == 3, "fixture should be splittable, else the test is vacuous"
    assert len(raw) == 1
    assert raw[0].text == MULTI_SENTENCE


def test_text_is_verbatim_apart_from_surrounding_whitespace():
    facts = RawTurnExtractionProvider().extract("  keep   inner  spacing.\n\n")
    assert facts[0].text == "keep   inner  spacing."


def test_short_text_is_kept_where_the_heuristic_drops_it():
    short = "Use uv."
    assert HeuristicExtractionProvider().extract(short) == []
    facts = RawTurnExtractionProvider().extract(short)
    assert len(facts) == 1
    assert facts[0].text == short


def test_no_importance_or_confidence_opinion():
    fact = RawTurnExtractionProvider().extract(MULTI_SENTENCE)[0]
    assert fact.importance is None
    assert fact.confidence is None
    assert fact.entities == []


def test_empty_and_whitespace_yield_nothing():
    provider = RawTurnExtractionProvider()
    assert provider.extract("") == []
    assert provider.extract("   \n\t ") == []
    assert provider.extract(None) == []


def test_max_chars_truncates_only_when_set():
    long_text = "x" * 500
    assert len(RawTurnExtractionProvider().extract(long_text)[0].text) == 500
    capped = RawTurnExtractionProvider(max_chars=100).extract(long_text)
    assert len(capped[0].text) == 100


def test_text_without_terminal_punctuation_survives():
    facts = RawTurnExtractionProvider().extract("no trailing period here")
    assert facts[0].text == "no trailing period here"
