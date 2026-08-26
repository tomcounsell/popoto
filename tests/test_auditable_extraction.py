"""Tests for the M3 auditable-extraction pipeline (issue #562).

Organized one class per pipeline stage so later tasks can extend this file
without colliding:

    TestCandidateGeneration -- src/popoto/extraction/candidates.py (Task 1)

See ``docs/plans/auditable_extraction_m3.md``.
"""

import pytest

from popoto.extraction import HeuristicExtractionProvider
from popoto.extraction.candidates import Candidate, generate_candidates
from popoto.privacy.never_record import scan_never_record

CANDIDATE_CORPUS = [
    (
        "turn-001",
        "Alice deployed the service. Bob reviewed the change! "
        "Did Carol approve it? Alice deployed the service.",
    ),
    (
        "agent:main:42",
        "The Popoto ORM stores facts in Redis. "
        "New York Times reported on OpenAI yesterday.",
    ),
    ("t1", "Short."),
    ("turn_with_underscores", "No entities here, just lowercase prose."),
]

class TestCandidateGeneration:
    """Task 1: deterministic, exhaustive, LLM-free candidate enumeration."""

    def test_returns_candidate_instances_with_full_identity(self):
        candidates = generate_candidates("turn-001", "Alice shipped Popoto.")

        assert candidates, "expected at least one candidate"
        for candidate in candidates:
            assert isinstance(candidate, Candidate)
            assert candidate.turn_id == "turn-001"
            assert candidate.text
            assert candidate.generator_rule
            assert 0 <= candidate.start < candidate.end

    @pytest.mark.parametrize("turn_id,text", CANDIDATE_CORPUS)
    def test_deterministic_across_repeated_calls(self, turn_id, text):
        first = generate_candidates(turn_id, text)
        second = generate_candidates(turn_id, text)
        third = generate_candidates(turn_id, text)

        assert first == second == third

    @pytest.mark.parametrize("turn_id,text", CANDIDATE_CORPUS)
    def test_spans_slice_back_to_the_source_text(self, turn_id, text):
        for candidate in generate_candidates(turn_id, text):
            assert text[candidate.start : candidate.end] == candidate.text

    def test_enumerates_every_sentence_of_a_multi_sentence_turn(self):
        text = (
            "Alice deployed the service. Bob reviewed the change! "
            "Did Carol approve it?"
        )
        candidates = generate_candidates("turn-001", text)

        sentences = [
            c.text for c in candidates if c.generator_rule == "sentence"
        ]
        # Exhaustive: byte-identical to the heuristic provider's own split,
        # with no min-length filter applied (rejection is the caller's job).
        assert sentences == HeuristicExtractionProvider._split_sentences(text)

    def test_enumerates_entities_alongside_sentences(self):
        text = "Alice deployed the service. New York Times covered OpenAI."
        candidates = generate_candidates("turn-001", text)

        entities = [c.text for c in candidates if c.generator_rule == "entity"]
        # Multi-token runs are entities wherever they sit, including at the
        # start of a sentence.
        assert "New York Times" in entities
        # A mid-sentence single capitalized token is an entity too.
        assert "OpenAI" in entities
        # But a sentence-initial single capitalized token is orthography,
        # not evidence of an entity -- lifting it would flood the candidate
        # set with "The" / "Did" / "This". The sentence rule still covers
        # the text, so nothing goes unenumerated.
        assert "Alice" not in entities

    def test_mid_sentence_single_token_entities_are_lifted(self):
        text = "The change was approved by Alice and shipped."
        candidates = generate_candidates("turn-001", text)

        entities = [c.text for c in candidates if c.generator_rule == "entity"]
        assert entities == ["Alice"]

    def test_short_sentences_are_not_dropped(self):
        # The heuristic provider's min-length drop is exactly the silent drop
        # M3 exists to make visible: the generator must still emit it.
        candidates = generate_candidates("t1", "Short.")

        assert any(c.text == "Short." for c in candidates)

    @pytest.mark.parametrize("text", ["", "   ", "\n\t  \n", None])
    def test_empty_turn_yields_no_candidates(self, text):
        assert generate_candidates("turn-001", text) == []

    @pytest.mark.parametrize("turn_id,text", CANDIDATE_CORPUS)
    def test_candidate_ids_are_unique_within_a_turn(self, turn_id, text):
        ids = [c.candidate_id for c in generate_candidates(turn_id, text)]

        assert len(ids) == len(set(ids))

    def test_byte_identical_texts_get_distinct_candidate_ids(self):
        text = "Alice deployed the service. Alice deployed the service."
        candidates = generate_candidates("turn-001", text)

        duplicates = [
            c for c in candidates if c.text == "Alice deployed the service."
        ]
        assert len(duplicates) == 2
        assert duplicates[0].candidate_id != duplicates[1].candidate_id

    @pytest.mark.parametrize("turn_id,text", CANDIDATE_CORPUS)
    def test_candidate_id_format_is_turn_rule_ordinal(self, turn_id, text):
        for candidate in generate_candidates(turn_id, text):
            expected_prefix = f"{turn_id}:{candidate.generator_rule}:"
            assert candidate.candidate_id.startswith(expected_prefix)
            ordinal = candidate.candidate_id[len(expected_prefix) :]
            assert ordinal.isdigit()

    @pytest.mark.parametrize("turn_id,text", CANDIDATE_CORPUS)
    def test_candidate_id_survives_the_never_record_firewall(
        self, turn_id, text
    ):
        # The journal scans every subject tag at write time, so a
        # high-entropy (hash/digest) candidate_id would make M3's own writes
        # fail as `high_entropy`. Pins the low-entropy id format.
        for candidate in generate_candidates(turn_id, text):
            verdict = scan_never_record(f"cand:{candidate.candidate_id}")
            assert verdict.blocked is False, (
                f"candidate_id {candidate.candidate_id!r} blocked as "
                f"{verdict.reason!r}"
            )
