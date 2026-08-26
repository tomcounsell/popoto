"""Tests for the M3 auditable-extraction pipeline (issue #562).

Covers the deterministic candidate generator, the enum-confined verdict
stage, and the per-candidate decision log. See
``docs/plans/auditable_extraction_m3.md``.
"""

import enum
import json
import threading
import time
from dataclasses import dataclass
from itertools import count
from types import SimpleNamespace
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
from popoto.exceptions import JournalBlockedError
from popoto.extraction.decision_log import (
    AuditableExtractionConfig,
    DecisionLog,
    DecisionRecord,
)
from popoto.fields.constants import Defaults
from popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
from popoto.recipes.subconscious_memory import SubconsciousMemory
from popoto.privacy.never_record import scan_never_record
from popoto.redis_db import POPOTO_REDIS_DB

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


# ---------------------------------------------------------------------------
# Assembly test doubles.
#
# _FakeJournal stands in for ProvenanceJournal: it records every append and
# answers the cand:-tag identity probe from its own entries, so the dedup
# logic is exercised without depending on the real journal's keyspace. The
# probe goes through `entry_model.query.filter(...)`, which is the same
# attribute the real façade exposes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    entry_id: str
    turn_id: str
    subjects: list


class _FakeEntryQuery:
    def __init__(self, journal):
        self._journal = journal

    def filter(self, turn_id=None, subjects__all=None):
        wanted = set(subjects__all or [])
        return [
            entry
            for entry in self._journal.entries
            if entry.turn_id == turn_id and wanted.issubset(set(entry.subjects))
        ]


class _FakeEntryModel:
    def __init__(self, journal):
        self.query = _FakeEntryQuery(journal)


class _FakeJournal:
    """Records appends; raises on demand; answers the identity probe."""

    _ids = count(1)

    def __init__(self, raise_exc=None, on_append=None):
        self.appends = []
        self.entries = []
        self._raise_exc = raise_exc
        self._on_append = on_append
        self.entry_model = _FakeEntryModel(self)

    def append(self, **kwargs):
        if self._on_append is not None:
            self._on_append()
        if self._raise_exc is not None:
            raise self._raise_exc
        entry_id = f"entry-{next(self._ids)}"
        entry = _FakeEntry(
            entry_id=entry_id,
            turn_id=kwargs.get("turn_id"),
            subjects=list(kwargs.get("subjects") or []),
        )
        self.entries.append(entry)
        self.appends.append({**kwargs, "entry_id": entry_id})
        return SimpleNamespace(entry=entry, target_closed=False)


class _StubVerdict:
    """A deterministic verdict provider keyed on the candidate's ordinal."""

    def __init__(self, accept_ordinals=(), accept_all=False):
        self.accept_ordinals = set(accept_ordinals)
        self.accept_all = accept_all
        self.calls = []

    def __call__(self, candidate):
        self.calls.append(candidate)
        ordinal = int(candidate.candidate_id.rsplit(":", 1)[1])
        if self.accept_all or ordinal in self.accept_ordinals:
            return VerdictResult(
                candidate.candidate_id, Verdict.ACCEPT, ReasonCode.ACCEPTED
            )
        return VerdictResult(
            candidate.candidate_id, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )


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
    def test_candidate_id_not_firewall_blocked(self, turn_id, text):
        # The journal scans every subject tag at write time, so a
        # high-entropy (hash/digest) candidate_id would make M3's own writes
        # fail as `high_entropy`. Pins the low-entropy id format.
        for candidate in generate_candidates(turn_id, text):
            verdict = scan_never_record(f"cand:{candidate.candidate_id}")
            assert verdict.blocked is False, (
                f"candidate_id {candidate.candidate_id!r} blocked as "
                f"{verdict.reason!r}"
            )


class TestDecisionLogCore:
    """Task 3a: composite-key rows, the guarded terminal write, recovery."""

    def _candidate(self, ordinal=0, text="Alice deployed the service."):
        return Candidate(
            text=text,
            turn_id="t-41",
            candidate_id=f"t-41:sentence:{ordinal}",
            start=0,
            end=len(text),
            generator_rule="sentence",
        )

    def _rows_for(self, agent_id, turn_id, candidate_id):
        """Count raw Redis rows for one composite key, bypassing the ORM."""
        key = DecisionLog.row_key(agent_id, turn_id, candidate_id)
        return POPOTO_REDIS_DB.keys(f"{key}*")

    # -- keying ----------------------------------------------------------

    def test_single_row_per_candidate_transitions_in_place(self):
        """THE test that pins the design: one row, not one row per save.

        An AutoKeyField on DecisionRecord would mint a second row here and
        break every idempotency guarantee in the module at once.
        """
        log = DecisionLog()
        candidate = self._candidate()

        log.write_pending("agent-key", candidate)
        assert len(self._rows_for("agent-key", "t-41", candidate.candidate_id)) == 1

        log.write_terminal(
            "agent-key",
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id="entry-1",
        )

        rows = self._rows_for("agent-key", "t-41", candidate.candidate_id)
        assert len(rows) == 1, f"expected exactly one Redis row, got {rows}"

        stored = log.get("agent-key", "t-41", candidate.candidate_id)
        assert stored.state == Verdict.ACCEPT.value
        assert stored.entry_id == "entry-1"
        assert stored.is_terminal is True

        # And the ORM agrees there is one row, not two.
        matching = [
            row
            for row in log.list_for_agent("agent-key")
            if row.candidate_id == candidate.candidate_id
        ]
        assert len(matching) == 1

    def test_key_field_names_are_declared_not_auto(self):
        assert DecisionRecord._meta.key_field_names == {
            "agent_id",
            "turn_id",
            "candidate_id",
        }
        assert "_auto_key" not in DecisionRecord._meta.fields
        assert DecisionRecord._meta.auto_field_names == set()

    def test_redis_key_joins_key_fields_alphabetically(self):
        # Not declaration order: candidate_id lands in the middle.
        key = DecisionLog.row_key("agent-1", "t-41", "t-41:sentence:0")
        assert key.startswith("DecisionRecord:agent/-1:")
        assert key.endswith(":t/-41")
        # Colons inside a value are escaped, which is correct -- the raw
        # candidate_id must not add key segments.
        assert key.count(":") == 3

    # -- written_at ------------------------------------------------------

    def test_written_at_is_stamped_on_both_writes(self):
        log = DecisionLog()
        candidate = self._candidate()
        before = time.time()

        log.write_pending("agent-ts", candidate)
        pending_at = log.get("agent-ts", "t-41", candidate.candidate_id).written_at
        assert pending_at is not None and pending_at >= before

        time.sleep(0.01)
        log.write_terminal(
            "agent-ts",
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id="entry-1",
        )
        terminal_at = log.get("agent-ts", "t-41", candidate.candidate_id).written_at

        assert terminal_at is not None
        assert terminal_at > pending_at, "the terminal write must restamp written_at"

    # -- list_pending ----------------------------------------------------

    def test_list_pending_returns_stale_rows_oldest_first(self):
        log = DecisionLog()
        for ordinal in range(3):
            log.write_pending("agent-stale", self._candidate(ordinal))
            time.sleep(0.01)

        # A terminal row must not appear in list_pending.
        decided = self._candidate(9)
        log.write_terminal(
            "agent-stale", decided, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )

        rows = log.list_pending("agent-stale")

        assert [row.candidate_id for row in rows] == [
            "t-41:sentence:0",
            "t-41:sentence:1",
            "t-41:sentence:2",
        ]
        stamps = [row.written_at for row in rows]
        assert stamps == sorted(stamps)

    def test_list_pending_older_than_filters_by_written_at(self):
        log = DecisionLog()
        log.write_pending("agent-older", self._candidate(0))
        time.sleep(0.01)
        cutoff = time.time()
        time.sleep(0.01)
        log.write_pending("agent-older", self._candidate(1))

        rows = log.list_pending("agent-older", older_than=cutoff)

        assert [row.candidate_id for row in rows] == ["t-41:sentence:0"]

    def test_list_pending_is_scoped_to_one_agent(self):
        log = DecisionLog()
        log.write_pending("agent-a", self._candidate(0))
        log.write_pending("agent-b", self._candidate(1))

        assert [r.candidate_id for r in log.list_pending("agent-a")] == [
            "t-41:sentence:0"
        ]
        assert [r.candidate_id for r in log.list_pending("agent-b")] == [
            "t-41:sentence:1"
        ]

    # -- detail_code -----------------------------------------------------

    def test_detail_code_is_a_free_form_string_not_an_enum(self):
        log = DecisionLog()
        candidate = self._candidate()

        # An exception class name -- the assembly_failed payload shape.
        log.write_terminal(
            "agent-detail",
            candidate,
            Verdict.REJECT,
            ReasonCode.ASSEMBLY_FAILED,
            detail_code="ConnectionError",
        )
        row = log.get("agent-detail", "t-41", candidate.candidate_id)
        assert row.detail_code == "ConnectionError"
        assert not isinstance(row.detail_code, enum.Enum)
        assert "ConnectionError" not in {r.value for r in ReasonCode}

        # A comma-joined entry_id list -- the ambiguous_reconciliation shape.
        other = self._candidate(1)
        log.write_terminal(
            "agent-detail",
            other,
            Verdict.REJECT,
            ReasonCode.AMBIGUOUS_RECONCILIATION,
            detail_code=",".join(["entry-1", "entry-2"]),
        )
        assert (
            log.get("agent-detail", "t-41", other.candidate_id).detail_code
            == "entry-1,entry-2"
        )

    def test_detail_code_defaults_to_empty(self):
        log = DecisionLog()
        candidate = self._candidate()
        log.write_terminal(
            "agent-detail-empty", candidate, Verdict.WITHHOLD, ReasonCode.LOW_CONFIDENCE
        )
        assert (
            log.get("agent-detail-empty", "t-41", candidate.candidate_id).detail_code
            == ""
        )

    # -- the guarded terminal write --------------------------------------

    @pytest.mark.parametrize(
        "state,reason",
        [
            (Verdict.FIREWALL_DROP, ReasonCode.PRE_LLM_CANDIDATE_BLOCK),
            (Verdict.REJECT, ReasonCode.NOT_A_FACT),
            (Verdict.WITHHOLD, ReasonCode.LOW_CONFIDENCE),
        ],
    )
    def test_fresh_candidate_terminal_write_succeeds(self, state, reason):
        """No prior row to conflict with -- the guard lets it through."""
        log = DecisionLog()
        agent = f"agent-fresh-{state.value}"
        candidate = self._candidate()

        assert log.write_terminal(agent, candidate, state, reason) is True

        row = log.get(agent, "t-41", candidate.candidate_id)
        assert row.state == state.value
        assert row.reason_code == reason.value
        assert row.detail_code == ""
        assert row.written_at is not None
        # A row the script created from scratch is still queryable.
        assert len(self._rows_for(agent, "t-41", candidate.candidate_id)) == 1

    def test_lua_created_row_is_visible_to_the_orm_query_api(self):
        """A row whose FIRST write is terminal must not be a ghost.

        The guard script writes the row hash directly, so Popoto's
        on_save index bookkeeping never runs for it. Without the script's
        own SADDs the row would sit in Redis in full while both .all()
        and .filter() failed to see it -- silently partial results for
        any later caller.
        """
        log = DecisionLog()
        candidate = self._candidate()
        log.write_terminal(
            "agent-orm", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )

        by_filter = list(
            DecisionRecord.query.filter(
                agent_id="agent-orm",
                turn_id="t-41",
                candidate_id=candidate.candidate_id,
            )
        )
        assert len(by_filter) == 1
        assert by_filter[0].state == Verdict.REJECT.value

        assert any(row.agent_id == "agent-orm" for row in DecisionRecord.query.all())

    def test_terminal_write_is_refused_against_an_assembled_accept_row(self):
        log = DecisionLog()
        candidate = self._candidate()
        log.write_pending("agent-guard", candidate)
        log.write_terminal(
            "agent-guard",
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id="entry-77",
        )

        # A retried, non-deterministic verdict resolves reject this time.
        refused = log.write_terminal(
            "agent-guard", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )

        assert refused is False
        row = log.get("agent-guard", "t-41", candidate.candidate_id)
        assert row.state == Verdict.ACCEPT.value, "the accept row must stand"
        assert row.entry_id == "entry-77"
        assert row.reason_code == ReasonCode.ACCEPTED.value
        assert row.detail_code == DecisionLog.CONFLICT_REFUSED
        assert len(self._rows_for("agent-guard", "t-41", candidate.candidate_id)) == 1

    def test_accept_row_without_an_entry_id_is_not_protected(self):
        """The guard keys on an assembled row, not on the accept state alone.

        An accept row with no entry_id has no journal entry behind it, so
        there is nothing for the log to disagree with.
        """
        log = DecisionLog()
        candidate = self._candidate()
        log.write_terminal(
            "agent-noentry", candidate, Verdict.ACCEPT, ReasonCode.ACCEPTED
        )

        assert (
            log.write_terminal(
                "agent-noentry", candidate, Verdict.REJECT, ReasonCode.ASSEMBLY_FAILED
            )
            is True
        )
        assert (
            log.get("agent-noentry", "t-41", candidate.candidate_id).state
            == Verdict.REJECT.value
        )

    def test_pending_row_is_not_protected_by_the_guard(self):
        """pending -> terminal is the normal path and must never be refused."""
        log = DecisionLog()
        candidate = self._candidate()
        log.write_pending("agent-pending", candidate)

        assert (
            log.write_terminal(
                "agent-pending",
                candidate,
                Verdict.FIREWALL_DROP,
                ReasonCode.POST_ACCEPT_JOURNAL_BLOCK,
            )
            is True
        )
        row = log.get("agent-pending", "t-41", candidate.candidate_id)
        assert row.state == Verdict.FIREWALL_DROP.value
        assert row.reason_code == ReasonCode.POST_ACCEPT_JOURNAL_BLOCK.value

    def test_write_terminal_refuses_the_non_terminal_pending_marker(self):
        log = DecisionLog()
        with pytest.raises(ValueError, match="terminal"):
            log.write_terminal(
                "agent-x", self._candidate(), Verdict.PENDING, ReasonCode.ACCEPTED
            )

    # -- per-turn summary ------------------------------------------------

    def test_summary_counts_terminal_states_only(self):
        log = DecisionLog()
        log.write_pending("agent-sum", self._candidate(0))  # never counted
        log.write_terminal(
            "agent-sum", self._candidate(1), Verdict.REJECT, ReasonCode.NOT_A_FACT
        )
        log.write_terminal(
            "agent-sum", self._candidate(2), Verdict.REJECT, ReasonCode.NOT_MEMORABLE
        )
        log.write_terminal(
            "agent-sum",
            self._candidate(3),
            Verdict.FIREWALL_DROP,
            ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
        )

        summary = log.turn_summary("agent-sum", "t-41")

        assert summary["state:reject"] == 2
        assert summary["state:firewall_drop"] == 1
        assert summary["reason:not_a_fact"] == 1
        assert summary["reason:not_memorable"] == 1
        assert "state:pending" not in summary

    def test_summary_counts_a_transitioned_candidate_once(self):
        log = DecisionLog()
        candidate = self._candidate()
        log.write_pending("agent-once", candidate)
        log.write_terminal(
            "agent-once",
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id="entry-1",
        )
        # A refused retry must not touch the counts either.
        log.write_terminal(
            "agent-once", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )

        summary = log.turn_summary("agent-once", "t-41")
        assert summary["state:accept"] == 1
        assert "state:reject" not in summary


class TestAssemblyWiring:
    """Task 3b: the claim, identity reconciliation, and the opt-in flag."""

    def _candidate(self, ordinal=0, text="Alice deployed the service.", turn="t-90"):
        return Candidate(
            text=text,
            turn_id=turn,
            candidate_id=f"{turn}:sentence:{ordinal}",
            start=0,
            end=len(text),
            generator_rule="sentence",
        )

    # -- Race 4: the atomic claim ----------------------------------------

    def test_claim_loser_appends_nothing(self):
        log = DecisionLog()
        args = ("agent-claim", "t-90", "t-90:sentence:0")

        first = log.acquire_claim(*args)
        second = log.acquire_claim(*args)

        assert first is not None
        assert second is None, "SET NX must admit exactly one runner"

        assert log.release_claim(*args, first) is True
        assert log.acquire_claim(*args) is not None, "released claim is re-takeable"

    def test_release_is_token_checked(self):
        log = DecisionLog()
        args = ("agent-token", "t-90", "t-90:sentence:0")
        log.acquire_claim(*args)

        # A runner whose claim already expired must not delete the claim
        # its successor now holds.
        assert log.release_claim(*args, "not-my-token") is False
        assert log.acquire_claim(*args) is None, "the real owner still holds it"

    def test_claim_carries_a_finite_ttl(self):
        log = DecisionLog()
        args = ("agent-ttl", "t-90", "t-90:sentence:0")
        log.acquire_claim(*args)

        ttl_ms = POPOTO_REDIS_DB.pttl(DecisionLog.claim_key(*args))

        assert 0 < ttl_ms <= Defaults.M3_ASSEMBLY_CLAIM_TTL_MS

    def test_concurrent_claim_racing_runners_produce_exactly_one_entry(self):
        journal = _FakeJournal()
        candidate = self._candidate()
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            results.append(DecisionLog().assemble("agent-race", candidate, journal))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(journal.appends) == 1, "the loser must append nothing"
        # A runner that arrives after the winner released the claim reads
        # the terminal accept row and returns the same entry_id -- that is
        # the idempotent re-run, not a second assembly. What must never
        # happen is two entries, or two runners reporting *different* ones.
        reported = {r for r in results if r is not None}
        assert len(reported) == 1

        log = DecisionLog()
        rows = [
            row
            for row in log.list_for_agent("agent-race")
            if row.candidate_id == candidate.candidate_id
        ]
        assert len(rows) == 1
        assert rows[0].state == Verdict.ACCEPT.value
        assert rows[0].entry_id == journal.appends[0]["entry_id"]

    # -- Race 3: reconciliation across an interrupted run -----------------

    def test_assembly_idempotency_interrupted_run_reconciles_without_a_second_append(
        self,
    ):
        """The pending row survived; the entry landed. Reconcile, don't append."""
        journal = _FakeJournal()
        candidate = self._candidate()
        log = DecisionLog()

        # Simulate the crash: pending row committed, append() landed, the
        # terminal write never ran.
        log.write_pending("agent-interrupted", candidate)
        entry_id = journal.append(
            agent_id="agent-interrupted",
            kind="assert",
            verbatim=candidate.text,
            statement=candidate.text,
            speaker=None,
            turn_id=candidate.turn_id,
            subjects=[f"cand:{candidate.candidate_id}"],
        ).entry.entry_id

        reconciled = log.assemble("agent-interrupted", candidate, journal)

        assert reconciled == entry_id
        assert len(journal.appends) == 1, "must not re-append"
        row = log.get("agent-interrupted", candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.ACCEPT.value
        assert row.entry_id == entry_id

    def test_assembly_idempotency_rerunning_a_completed_candidate_appends_nothing(self):
        journal = _FakeJournal()
        candidate = self._candidate()
        log = DecisionLog()

        first = log.assemble("agent-rerun", candidate, journal)
        second = log.assemble("agent-rerun", candidate, journal)

        assert first == second
        assert len(journal.appends) == 1

    def test_duplicate_text_reconcile_onto_own_entries(self):
        """Reconciliation is by cand: tag, never by verbatim text."""
        journal = _FakeJournal()
        text = "Alice deployed the service."
        first = self._candidate(0, text=text)
        second = self._candidate(1, text=text)
        assert first.text == second.text
        log = DecisionLog()

        entry_ids = {}
        for candidate in (first, second):
            log.write_pending("agent-dup", candidate)
            entry_ids[candidate.candidate_id] = journal.append(
                agent_id="agent-dup",
                kind="assert",
                verbatim=candidate.text,
                statement=candidate.text,
                speaker=None,
                turn_id=candidate.turn_id,
                subjects=[f"cand:{candidate.candidate_id}"],
            ).entry.entry_id

        for candidate in (first, second):
            assert (
                log.assemble("agent-dup", candidate, journal)
                == entry_ids[candidate.candidate_id]
            ), "a text match would have cross-wired these two"
        assert len(journal.appends) == 2

    def test_ambiguous_reconciliation_appends_nothing_and_records_entry_ids(self):
        journal = _FakeJournal()
        candidate = self._candidate()
        log = DecisionLog()
        log.write_pending("agent-ambig", candidate)

        # Two entries carrying the same cand: tag, written out of band.
        for _ in range(2):
            journal.append(
                agent_id="agent-ambig",
                kind="assert",
                verbatim=candidate.text,
                statement=candidate.text,
                speaker=None,
                turn_id=candidate.turn_id,
                subjects=[f"cand:{candidate.candidate_id}"],
            )
        appends_before = len(journal.appends)

        assert log.assemble("agent-ambig", candidate, journal) is None

        assert len(journal.appends) == appends_before, "must append nothing"
        row = log.get("agent-ambig", candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.REJECT.value
        assert row.reason_code == ReasonCode.AMBIGUOUS_RECONCILIATION.value
        assert sorted(row.detail_code.split(",")) == sorted(
            entry["entry_id"] for entry in journal.appends
        )

    # -- the append() transition table -----------------------------------

    def test_journal_block_maps_to_firewall_drop_post_accept(self):
        journal = _FakeJournal(raise_exc=JournalBlockedError("refused"))
        candidate = self._candidate()
        log = DecisionLog()

        assert log.assemble("agent-blocked", candidate, journal) is None

        row = log.get("agent-blocked", candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.FIREWALL_DROP.value
        assert row.reason_code == ReasonCode.POST_ACCEPT_JOURNAL_BLOCK.value
        assert row.entry_id == ""

    @pytest.mark.parametrize(
        "exc", [ValueError("bad"), TypeError("worse"), ConnectionError("flaky")]
    )
    def test_other_append_failures_map_to_reject_assembly_failed(self, exc):
        journal = _FakeJournal(raise_exc=exc)
        candidate = self._candidate()
        agent = f"agent-fail-{type(exc).__name__}"
        log = DecisionLog()

        assert log.assemble(agent, candidate, journal) is None

        row = log.get(agent, candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.REJECT.value
        assert row.reason_code == ReasonCode.ASSEMBLY_FAILED.value
        assert row.detail_code == type(exc).__name__

    def test_pending_written_before_append(self):
        """Ordering: no candidate reaches append() with zero rows."""
        candidate = self._candidate()
        seen = {}

        def on_append():
            seen["row"] = DecisionLog().get(
                "agent-order", candidate.turn_id, candidate.candidate_id
            )

        journal = _FakeJournal(on_append=on_append)
        DecisionLog().assemble("agent-order", candidate, journal)

        assert seen["row"] is not None, "append() ran with no decision-log row"
        assert seen["row"].state == Verdict.PENDING.value

    def test_candidate_identity_tag_on_assembled_entries(self):
        journal = _FakeJournal()
        candidate = self._candidate(text="  Alice   deployed the SERVICE.  ")
        DecisionLog().assemble("agent-bytes", candidate, journal)

        call = journal.appends[0]
        assert call["verbatim"] == candidate.text
        assert call["statement"] == candidate.text
        assert f"cand:{candidate.candidate_id}" in call["subjects"]
        assert call["agent_id"] == "agent-bytes"
        assert call["kind"] == "assert"

    def test_terminal_conflict_refused_survives_a_retried_reject(self):
        journal = _FakeJournal()
        candidate = self._candidate()
        log = DecisionLog()
        entry_id = log.assemble("agent-conflict", candidate, journal)

        refused = log.write_terminal(
            "agent-conflict", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )

        assert refused is False
        row = log.get("agent-conflict", candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.ACCEPT.value
        assert row.entry_id == entry_id
        assert row.detail_code == DecisionLog.CONFLICT_REFUSED
        assert entry_id in {entry["entry_id"] for entry in journal.appends}

    # -- offline metrics --------------------------------------------------

    def _seed_metrics_corpus(self, agent_id):
        log = DecisionLog()
        journal = _FakeJournal()
        # 2 true positives, 1 false positive, 1 false negative, plus one
        # privacy drop and one infrastructure loss that are gold-labelled
        # accept -- so the breakdown has to separate them.
        log.assemble(agent_id, self._candidate(0), journal)
        log.assemble(agent_id, self._candidate(1), journal)
        log.assemble(agent_id, self._candidate(2), journal)
        log.write_terminal(
            agent_id, self._candidate(3), Verdict.REJECT, ReasonCode.NOT_A_FACT
        )
        log.write_terminal(
            agent_id,
            self._candidate(4),
            Verdict.FIREWALL_DROP,
            ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
        )
        log.write_terminal(
            agent_id, self._candidate(5), Verdict.REJECT, ReasonCode.LLM_UNAVAILABLE
        )
        return {
            "t-90:sentence:0": True,
            "t-90:sentence:1": True,
            "t-90:sentence:2": False,
            "t-90:sentence:3": True,
            "t-90:sentence:4": True,
            "t-90:sentence:5": True,
        }

    def test_precision_recall_computable_from_log_alone(self):
        gold = self._seed_metrics_corpus("agent-metrics")

        metrics = DecisionLog().compute_metrics("agent-metrics", gold)

        assert metrics.true_positives == 2
        assert metrics.false_positives == 1
        assert metrics.false_negatives == 3
        assert metrics.precision == pytest.approx(2 / 3)
        assert metrics.recall == pytest.approx(2 / 5)
        assert metrics.f1 == pytest.approx(0.5)

    def test_metrics_breakdown_separates_privacy_model_and_infrastructure(self):
        gold = self._seed_metrics_corpus("agent-breakdown")

        metrics = DecisionLog().compute_metrics("agent-breakdown", gold)

        # Three structurally different losses, distinguishable in one run.
        assert metrics.per_reason_code["pre_llm_candidate_block"] == 1  # privacy
        assert metrics.per_reason_code["not_a_fact"] == 1  # model reject
        assert metrics.per_reason_code["llm_unavailable"] == 1  # infrastructure
        assert metrics.per_generator_rule["sentence"] == {
            "accept": 3,
            "reject": 2,
            "firewall_drop": 1,
        }

    def test_metrics_are_identical_with_the_journal_keyspace_absent(self):
        gold = self._seed_metrics_corpus("agent-isolated")
        before = DecisionLog().compute_metrics("agent-isolated", gold)

        # The fake journal holds its entries in memory; drop every real
        # journal key too, so nothing journal-shaped survives to be read.
        for key in POPOTO_REDIS_DB.scan_iter(match="JournalEntry*"):
            POPOTO_REDIS_DB.delete(key)

        after = DecisionLog().compute_metrics("agent-isolated", gold)

        assert (after.precision, after.recall, after.f1) == (
            before.precision,
            before.recall,
            before.f1,
        )
        assert after.per_reason_code == before.per_reason_code

    def test_metrics_are_defined_on_an_empty_log(self):
        metrics = DecisionLog().compute_metrics("agent-nothing", {})
        assert (metrics.precision, metrics.recall, metrics.f1) == (0.0, 0.0, 0.0)


class TestSubconsciousMemoryWiring:
    """The opt-in flag. The default path must not move a byte."""

    def test_default_path_is_unchanged(self):
        """auditable_extraction defaults to None and nothing else changes."""
        memory = SubconsciousMemory(agent_id="agent-default")
        assert memory.decision_log is None

        saved = memory.extract_memories(
            "Alice deployed the service. Bob reviewed the change."
        )

        assert saved, "the default path still saves model instances"
        assert all(isinstance(item, memory.model_class) for item in saved)
        assert DecisionLog().list_for_agent("agent-default") == []

    def test_default_path_output_matches_an_unflagged_instance(self):
        text = "Alice deployed the service. Bob reviewed the change."
        plain = SubconsciousMemory(agent_id="agent-plain")
        also_plain = SubconsciousMemory(
            agent_id="agent-plain-2", auditable_extraction=None
        )

        first = [getattr(m, "content") for m in plain.extract_memories(text)]
        second = [getattr(m, "content") for m in also_plain.extract_memories(text)]

        assert first == second

    def test_auditable_path_logs_every_candidate_terminally_and_no_pending_survives(
        self,
    ):
        journal = _FakeJournal()
        memory = SubconsciousMemory(
            agent_id="agent-auditable",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_ordinals={0}),
                journal=journal,
            ),
        )

        facts = memory.extract_memories(
            "Alice deployed the service. Bob reviewed the change.",
            turn_id="t-777",
        )

        rows = DecisionLog().list_for_agent("agent-auditable")
        assert rows, "every candidate must be logged"
        assert all(row.is_terminal for row in rows), "no pending row may survive"
        assert not DecisionLog().list_pending("agent-auditable")

        accepted = [r for r in rows if r.state == Verdict.ACCEPT.value]
        assert all(row.entry_id for row in accepted)
        assert len(facts) == len(accepted)
        assert all(fact.candidate_id and fact.turn_id == "t-777" for fact in facts)

    def test_auditable_empty_turn_logs_a_reject_row(self):
        memory = SubconsciousMemory(
            agent_id="agent-empty",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(), journal=_FakeJournal()
            ),
        )

        assert memory.extract_memories("   ", turn_id="t-empty") == []

        rows = DecisionLog().list_for_agent("agent-empty")
        assert len(rows) == 1
        assert rows[0].state == Verdict.REJECT.value
        assert rows[0].reason_code == ReasonCode.EMPTY_TURN.value

    def test_auditable_path_keeps_the_turn_level_firewall_first(self):
        journal = _FakeJournal()
        memory = SubconsciousMemory(
            agent_id="agent-turnfw",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_all=True), journal=journal
            ),
        )

        result = memory.extract_memories(
            "off the record: Alice deployed the service.", turn_id="t-fw"
        )

        assert result == []
        assert journal.appends == [], "a voided turn must never reach the journal"
        assert memory.last_extraction_privacy_dropped is True

    def test_a_raising_verdict_provider_does_not_lose_the_candidate(self):
        class Exploding:
            def __call__(self, candidate):
                raise RuntimeError("provider down")

        memory = SubconsciousMemory(
            agent_id="agent-explode",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=Exploding(), journal=_FakeJournal()
            ),
        )

        assert memory.extract_memories("Alice deployed it.", turn_id="t-boom") == []

        rows = DecisionLog().list_for_agent("agent-explode")
        assert rows
        assert all(row.state == Verdict.REJECT.value for row in rows)
        assert all(row.reason_code == ReasonCode.LLM_UNAVAILABLE.value for row in rows)


class TestAssemblyAgainstTheRealJournal:
    """The fake journal proves the logic; this proves the integration.

    Everything above exercises assembly against _FakeJournal, which would
    happily keep passing if append()'s real signature, AnnotationResult's
    shape, or the subjects__all identity probe drifted. These tests use the
    real ProvenanceJournal so that drift fails here.
    """

    def _candidate(self, ordinal=0, text="Alice deployed the service.", turn="t-real"):
        return Candidate(
            text=text,
            turn_id=turn,
            candidate_id=f"{turn}:sentence:{ordinal}",
            start=0,
            end=len(text),
            generator_rule="sentence",
        )

    def test_accepted_candidate_lands_in_the_real_journal(self):
        candidate = self._candidate()
        log = DecisionLog()

        entry_id = log.assemble("agent-real", candidate, ProvenanceJournal)

        assert entry_id
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry.statement == entry.verbatim == candidate.text
        assert f"cand:{candidate.candidate_id}" in entry.subjects
        assert entry.agent_id == "agent-real"
        assert entry.kind == "assert"

        row = log.get("agent-real", candidate.turn_id, candidate.candidate_id)
        assert row.state == Verdict.ACCEPT.value
        assert row.entry_id == entry_id

    def test_real_identity_probe_reconciles_an_interrupted_run(self):
        candidate = self._candidate(1)
        log = DecisionLog()

        # Crash shape: pending row committed, append landed, no terminal write.
        log.write_pending("agent-real-retry", candidate)
        entry_id = str(
            ProvenanceJournal.append(
                agent_id="agent-real-retry",
                kind="assert",
                verbatim=candidate.text,
                statement=candidate.text,
                turn_id=candidate.turn_id,
                subjects=[f"cand:{candidate.candidate_id}"],
            ).entry.entry_id
        )
        before = len(list(JournalEntry.query.filter(turn_id=candidate.turn_id)))

        assert (
            log.assemble("agent-real-retry", candidate, ProvenanceJournal) == entry_id
        )

        after = list(JournalEntry.query.filter(turn_id=candidate.turn_id))
        assert len(after) == before, "reconciliation must not append a duplicate"
        assert (
            log.get(
                "agent-real-retry", candidate.turn_id, candidate.candidate_id
            ).entry_id
            == entry_id
        )

    def test_candidate_id_tag_is_not_blocked_by_the_journals_own_firewall(self):
        """The journal scans every subject tag; a bad id shape fails M3's writes."""
        for candidate in generate_candidates(
            "t-real-fw", "Alice deployed the service. Bob reviewed it."
        ):
            assert (
                DecisionLog().assemble("agent-real-fw", candidate, ProvenanceJournal)
                is not None
            ), f"cand:{candidate.candidate_id} was refused by the journal"

    def test_end_to_end_through_subconscious_memory(self):
        memory = SubconsciousMemory(
            agent_id="agent-e2e",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_all=True),
                journal=ProvenanceJournal,
            ),
        )

        facts = memory.extract_memories(
            "Alice deployed the service. Bob reviewed the change.",
            turn_id="t-e2e",
        )

        assert facts
        rows = DecisionLog().list_for_agent("agent-e2e")
        assert all(row.is_terminal for row in rows)
        entries = list(JournalEntry.query.filter(turn_id="t-e2e"))
        assert len(entries) == len([r for r in rows if r.state == Verdict.ACCEPT.value])
        for fact in facts:
            assert fact.text in {entry.statement for entry in entries}
