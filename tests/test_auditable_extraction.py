"""Tests for the M3 auditable-extraction pipeline (issue #562).

Covers the deterministic candidate generator, the enum-confined verdict
stage, and the per-candidate decision log. See
``docs/plans/auditable_extraction_m3.md``.
"""

import enum
import json
from dataclasses import fields as dataclass_fields

import pytest

from popoto.extraction.verdict import (
    LLM_REASON_CODES,
    LLM_VERDICTS,
    TERMINAL_VERDICTS,
    ReasonCode,
    Verdict,
    VerdictResult,
    llm_verdict,
)
from popoto.extraction import verdict as verdict_mod
from popoto.extraction import HeuristicExtractionProvider
from popoto.extraction.candidates import Candidate, generate_candidates
from popoto.privacy.never_record import scan_never_record

# A representative corpus for the candidate generator: multi-sentence prose,
# a repeated sentence, named entities, acronyms, punctuation variety, and
# turn id shapes the harness actually produces.
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


def make_candidate(text: str, candidate_id: str = "t-1:sent:0") -> Candidate:
    return Candidate(
        text=text,
        turn_id="t-1",
        candidate_id=candidate_id,
        start=0,
        end=len(text),
        generator_rule="sent",
    )


# ---------------------------------------------------------------------------
# Fake Anthropic client -- the same injection seam tests/test_extraction.py
# uses for ClaudeExtractionProvider (no network, no API key). Records every
# call so a test can assert the LLM was, or was not, invoked.
# ---------------------------------------------------------------------------


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [] if text is None else [FakeTextBlock(text)]


class FakeMessages:
    def __init__(self, response_text=None, raise_exc=None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(self.response_text)


class FakeClient:
    def __init__(self, response_text=None, raise_exc=None):
        self.messages = FakeMessages(response_text=response_text, raise_exc=raise_exc)

    @property
    def calls(self):
        return self.messages.calls


def _reply(candidate_id, verdict, reason_code, **extra):
    """Build a raw JSON reply the way a well-behaved model would."""
    payload = {
        "candidate_id": candidate_id,
        "verdict": verdict,
        "reason_code": reason_code,
    }
    payload.update(extra)
    return json.dumps(payload)


class TestVerdictVocabulary:
    """The state/reason vocabularies are load-bearing for decision_log.py."""

    def test_four_terminal_states_present(self):
        assert {v.value for v in TERMINAL_VERDICTS} == {
            "firewall_drop",
            "accept",
            "reject",
            "withhold",
        }

    def test_pending_is_non_terminal(self):
        assert Verdict.PENDING.value == "pending"
        assert Verdict.PENDING not in TERMINAL_VERDICTS
        assert Verdict.PENDING.is_terminal is False
        assert all(v.is_terminal for v in TERMINAL_VERDICTS)

    def test_llm_vocabulary_excludes_pending_and_firewall_drop(self):
        assert {v.value for v in LLM_VERDICTS} == {
            "accept",
            "reject",
            "withhold",
        }
        assert Verdict.PENDING not in LLM_VERDICTS
        assert Verdict.FIREWALL_DROP not in LLM_VERDICTS

    def test_reason_code_vocabulary(self):
        assert {r.value for r in ReasonCode} >= {
            "pre_llm_candidate_block",
            "post_accept_journal_block",
            "assembly_failed",
            "ambiguous_reconciliation",
            "llm_unavailable",
            "empty_turn",
            "accepted",
        }

    def test_trusted_only_reason_codes_are_not_offered_to_the_llm(self):
        offered = set().union(*LLM_REASON_CODES.values())
        for trusted_only in (
            ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
            ReasonCode.POST_ACCEPT_JOURNAL_BLOCK,
            ReasonCode.ASSEMBLY_FAILED,
            ReasonCode.AMBIGUOUS_RECONCILIATION,
            ReasonCode.LLM_UNAVAILABLE,
            ReasonCode.EMPTY_TURN,
        ):
            assert trusted_only not in offered

    def test_llm_reason_codes_are_keyed_by_the_llm_verdict_vocabulary(self):
        assert set(LLM_REASON_CODES) == set(LLM_VERDICTS)


class TestVerdictStage:
    """``llm_verdict`` — per-candidate firewall, then one enum-only call."""

    def test_firewall_blocked_candidate_is_dropped_without_llm_call(self):
        client = FakeClient(response_text=_reply("t-1:sent:0", "accept", "accepted"))
        candidate = make_candidate("my aws_secret_access_key = wJalrXUtnFEMI3xK7MDENG")

        result = llm_verdict(candidate, client=client)

        assert result.verdict is Verdict.FIREWALL_DROP
        assert result.reason_code is ReasonCode.PRE_LLM_CANDIDATE_BLOCK
        assert result.candidate_id == candidate.candidate_id
        assert client.calls == [], "the LLM must never see blocked candidate text"

    @pytest.mark.parametrize(
        "verdict_value,reason_value,expected_verdict,expected_reason",
        [
            ("accept", "accepted", Verdict.ACCEPT, ReasonCode.ACCEPTED),
            ("reject", "not_a_fact", Verdict.REJECT, ReasonCode.NOT_A_FACT),
            (
                "reject",
                "not_memorable",
                Verdict.REJECT,
                ReasonCode.NOT_MEMORABLE,
            ),
            (
                "withhold",
                "low_confidence",
                Verdict.WITHHOLD,
                ReasonCode.LOW_CONFIDENCE,
            ),
            (
                "withhold",
                "needs_confirmation",
                Verdict.WITHHOLD,
                ReasonCode.NEEDS_CONFIRMATION,
            ),
        ],
    )
    def test_wellformed_reply_parses(
        self, verdict_value, reason_value, expected_verdict, expected_reason
    ):
        candidate = make_candidate("Paris is the capital of France.")
        client = FakeClient(
            response_text=_reply(candidate.candidate_id, verdict_value, reason_value)
        )

        result = llm_verdict(candidate, client=client)

        assert result.verdict is expected_verdict
        assert result.reason_code is expected_reason
        assert result.candidate_id == candidate.candidate_id
        assert len(client.calls) == 1

    @pytest.mark.parametrize(
        "reply",
        [
            None,  # no text block in the response at all
            "",
            "   ",
            "not json at all",
            "[]",
            "{}",
            '{"verdict": "accept"}',  # missing reason_code
            '{"reason_code": "accepted"}',  # missing verdict
            '{"verdict": "maybe", "reason_code": "accepted"}',  # unknown verdict
            '{"verdict": "pending", "reason_code": "accepted"}',  # not LLM vocab
            '{"verdict": "firewall_drop", "reason_code": "accepted"}',
            '{"verdict": "accept", "reason_code": "vibes"}',  # unknown reason
            '{"verdict": "accept", "reason_code": "assembly_failed"}',  # not LLM's
            '{"verdict": "accept", "reason_code": "not_a_fact"}',  # bad pairing
            '{"verdict": "reject", "reason_code": "accepted"}',  # bad pairing
            '{"verdict": 3, "reason_code": 4}',
            '{"candidate_id": "someone-else", "verdict": "accept",'
            ' "reason_code": "accepted"}',
        ],
        ids=lambda r: repr(r)[:44],
    )
    def test_malformed_or_empty_reply_maps_to_reject_llm_unavailable(self, reply):
        candidate = make_candidate("Paris is the capital of France.")
        client = FakeClient(response_text=reply)

        result = llm_verdict(candidate, client=client)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code is ReasonCode.LLM_UNAVAILABLE
        assert result.candidate_id == candidate.candidate_id

    def test_api_exception_maps_to_reject_llm_unavailable(self):
        candidate = make_candidate("Paris is the capital of France.")
        client = FakeClient(raise_exc=RuntimeError("api exploded"))

        result = llm_verdict(candidate, client=client)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code is ReasonCode.LLM_UNAVAILABLE

    def test_blank_candidate_text_is_rejected_without_an_llm_call(self):
        client = FakeClient(response_text=_reply("t-1:sent:0", "accept", "accepted"))

        result = llm_verdict(make_candidate("   "), client=client)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code is ReasonCode.EMPTY_TURN
        assert client.calls == []

    def test_missing_client_maps_to_reject_llm_unavailable(self, monkeypatch):
        monkeypatch.setattr(verdict_mod, "anthropic_module", None)
        monkeypatch.setattr(verdict_mod, "_anthropic_available", False)
        candidate = make_candidate("Paris is the capital of France.")

        result = llm_verdict(candidate)

        assert result.verdict is Verdict.REJECT
        assert result.reason_code is ReasonCode.LLM_UNAVAILABLE

    def test_result_carries_only_enums_and_the_trusted_candidate_id(self):
        """No model-authored free text can reach the store via VerdictResult."""
        candidate = make_candidate("Paris is the capital of France.")
        client = FakeClient(
            response_text=_reply(
                candidate.candidate_id,
                "reject",
                "not_a_fact",
                explanation="free text the model tried to smuggle through",
            )
        )

        result = llm_verdict(candidate, client=client)

        names = {f.name for f in dataclass_fields(result)}
        assert names == {"candidate_id", "verdict", "reason_code"}
        assert isinstance(result.verdict, enum.Enum)
        assert isinstance(result.reason_code, enum.Enum)
        # The only string on the object is the id trusted code generated.
        assert result.candidate_id == candidate.candidate_id
        assert "smuggle" not in repr(result)

    def test_result_is_frozen(self):
        result = VerdictResult(
            candidate_id="t-1:sent:0",
            verdict=Verdict.ACCEPT,
            reason_code=ReasonCode.ACCEPTED,
        )
        with pytest.raises(Exception):
            result.verdict = Verdict.REJECT  # type: ignore[misc]


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

        sentences = [c.text for c in candidates if c.generator_rule == "sentence"]
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

        duplicates = [c for c in candidates if c.text == "Alice deployed the service."]
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
    def test_candidate_id_survives_the_never_record_firewall(self, turn_id, text):
        # The journal scans every subject tag at write time, so a
        # high-entropy (hash/digest) candidate_id would make M3's own writes
        # fail as `high_entropy`. Pins the low-entropy id format.
        for candidate in generate_candidates(turn_id, text):
            verdict = scan_never_record(f"cand:{candidate.candidate_id}")
            assert verdict.blocked is False, (
                f"candidate_id {candidate.candidate_id!r} blocked as "
                f"{verdict.reason!r}"
            )
