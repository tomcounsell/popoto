"""Tests for the M4 reference-resolution stage (issue #563).

Covers ``popoto.extraction.resolution`` (the pure module: types, prompt,
schema, the LLM call, and Python re-validation), ``resolution_log.py``
(the sidecar), and the M4 widenings to ``decision_log.py`` /
``subconscious_memory.py``. See ``docs/plans/reference_resolution_m4.md``.

Style: hand-rolled fakes only, no ``unittest.mock``, following
``test_auditable_extraction.py:80-206``.
"""

import json
import logging
import math
import time

import pytest

from popoto.extraction import resolution as resolution_mod
from popoto.extraction import resolution_log as resolution_log_mod
from popoto.extraction.candidates import Candidate
from popoto.extraction.decision_log import AuditableExtractionConfig, DecisionLog
from popoto.extraction.resolution import (
    Resolution,
    ResolutionStatus,
    TurnContext,
    resolve_references,
)
from popoto.extraction.resolution_log import ResolutionLog
from popoto.extraction.verdict import ReasonCode, Verdict, VerdictResult
from popoto.fields.constants import Defaults
from popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
from popoto.recipes.subconscious_memory import SubconsciousMemory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_m4_defaults():
    """Undo any per-test flip of the M4 kill switch or role-set constant.

    Follows ``test_provenance_journal.py:146-159``'s autouse-fixture
    restore pattern. The plugin's own autouse ``flushdb()`` owns the
    keyspace, so no key wipe belongs here.
    """
    enabled = Defaults.M4_RESOLUTION_ENABLED
    roles = Defaults.M4_VALID_FROM_ROLES
    yield
    Defaults.M4_RESOLUTION_ENABLED = enabled
    Defaults.M4_VALID_FROM_ROLES = roles


# ---------------------------------------------------------------------------
# Hand-rolled fakes -- mirrors test_auditable_extraction.py:80-206, adapted
# to _request_resolution's reply shape (candidate_id/statement/references).
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
    """Mirrors the real Anthropic structured-output reply shape that
    ``_request_resolution`` parses: ``response.content`` is a list of
    blocks, each with ``.type``/``.text``, and the text block carries the
    JSON payload."""

    def __init__(self, response_text=None, raise_exc=None):
        self.messages = FakeMessages(response_text=response_text, raise_exc=raise_exc)

    @property
    def calls(self):
        return self.messages.calls


class _StubVerdict:
    """Accepts everything -- mirrors test_auditable_extraction.py's double,
    defined locally per the no-cross-file-fake-sharing convention."""

    def __init__(self, accept_all=True):
        self.accept_all = accept_all
        self.calls = []

    def __call__(self, candidate):
        self.calls.append(candidate)
        if self.accept_all:
            return VerdictResult(
                candidate.candidate_id, Verdict.ACCEPT, ReasonCode.ACCEPTED
            )
        return VerdictResult(
            candidate.candidate_id, Verdict.REJECT, ReasonCode.NOT_A_FACT
        )


class _StubResolution:
    """A deterministic resolution provider -- mirrors _StubVerdict's shape,
    for tests that need SubconsciousMemory's provider-or-callable seam
    (``AuditableExtractionConfig.resolution_provider``)."""

    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = []

    def __call__(self, candidate, turn_text, context):
        self.calls.append((candidate, turn_text, context))
        return self.resolution


class _RaisingResolutionProvider:
    def __call__(self, candidate, turn_text, context):
        raise RuntimeError("resolution provider down")


def make_candidate(text, candidate_id="t-1:sent:0", turn_id="t-1"):
    return Candidate(
        text=text,
        turn_id=turn_id,
        candidate_id=candidate_id,
        start=0,
        end=len(text),
        generator_rule="sent",
    )


def reply_json(candidate_id, statement, references=()):
    return json.dumps(
        {
            "candidate_id": candidate_id,
            "statement": statement,
            "references": list(references),
        }
    )


def ref_resolved(
    surface,
    start,
    end,
    kind="definite_reference",
    resolved_text="X",
    temporal_role="none",
    resolved_iso=None,
):
    return {
        "surface": surface,
        "start": start,
        "end": end,
        "kind": kind,
        "status": "resolved",
        "temporal_role": temporal_role,
        "resolved_text": resolved_text,
        "resolved_iso": resolved_iso,
        "assumption": None,
        "candidates": [],
        "question": None,
    }


def ref_assumed(
    surface,
    start,
    end,
    kind="definite_reference",
    resolved_text="X",
    assumption="picked the most recent antecedent",
    temporal_role="none",
    resolved_iso=None,
):
    return {
        "surface": surface,
        "start": start,
        "end": end,
        "kind": kind,
        "status": "assumed",
        "temporal_role": temporal_role,
        "resolved_text": resolved_text,
        "resolved_iso": resolved_iso,
        "assumption": assumption,
        "candidates": [],
        "question": None,
    }


def ref_evidence_gap(surface, start, end, candidates, question="Which one?"):
    return {
        "surface": surface,
        "start": start,
        "end": end,
        "kind": "pronoun",
        "status": "evidence_gap",
        "temporal_role": "none",
        "resolved_text": None,
        "resolved_iso": None,
        "assumption": None,
        "candidates": list(candidates),
        "question": question,
    }


def ref_indeterminate(surface, start, end, kind="pronoun"):
    return {
        "surface": surface,
        "start": start,
        "end": end,
        "kind": kind,
        "status": "indeterminate",
        "temporal_role": "none",
        "resolved_text": None,
        "resolved_iso": None,
        "assumption": None,
        "candidates": [],
        "question": None,
    }


# ===========================================================================
# (a) One test class per status
# ===========================================================================


class TestResolvedStatus:
    def test_resolved_reference_updates_statement_verbatim_tag_and_sidecar(self):
        candidate = make_candidate(
            "She deployed it.", candidate_id="t-res:sent:0", turn_id="t-res"
        )
        ref = ref_resolved("She", 0, 3, kind="pronoun", resolved_text="Alice")
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, "Alice deployed it.", [ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.status is ResolutionStatus.RESOLVED
        assert resolution.statement == "Alice deployed it."
        assert resolution.verbatim == candidate.text
        assert not resolution.degraded

        entry_id = DecisionLog().assemble(
            "agent-res-status", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry.statement == "Alice deployed it."
        assert entry.verbatim == candidate.text
        assert "res:resolved" in entry.subjects

        assert ResolutionLog().write(
            "agent-res-status",
            candidate.turn_id,
            candidate.candidate_id,
            resolution,
            entry_id=entry_id,
        )
        record = ResolutionLog().get(
            "agent-res-status", candidate.turn_id, candidate.candidate_id
        )
        assert record.status == "resolved"
        assert record.statement == "Alice deployed it."
        assert record.verbatim == candidate.text
        assert record.entry_id == entry_id


class TestAssumedStatus:
    def test_assumed_reference_updates_statement_verbatim_tag_and_sidecar(self):
        candidate = make_candidate(
            "She deployed it too.", candidate_id="t-asm:sent:0", turn_id="t-asm"
        )
        ref = ref_assumed(
            "She",
            0,
            3,
            kind="pronoun",
            resolved_text="Alice",
            assumption="picked the most recently mentioned speaker",
        )
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, "Alice deployed it too.", [ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.status is ResolutionStatus.ASSUMED
        assert resolution.statement == "Alice deployed it too."
        assert resolution.verbatim == candidate.text
        assert not resolution.degraded
        assert resolution.references[0].assumption

        entry_id = DecisionLog().assemble(
            "agent-asm-status", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry.statement == "Alice deployed it too."
        assert entry.verbatim == candidate.text
        assert "res:assumed" in entry.subjects

        assert ResolutionLog().write(
            "agent-asm-status",
            candidate.turn_id,
            candidate.candidate_id,
            resolution,
            entry_id=entry_id,
        )
        record = ResolutionLog().get(
            "agent-asm-status", candidate.turn_id, candidate.candidate_id
        )
        assert record.status == "assumed"
        assert record.statement == "Alice deployed it too."
        assert record.verbatim == candidate.text
        assert json.loads(record.references_json)[0]["assumption"]


class TestEvidenceGapStatus:
    def test_evidence_gap_reference_updates_statement_verbatim_tag_and_sidecar(self):
        candidate = make_candidate(
            "She deployed it.", candidate_id="t-gap:sent:0", turn_id="t-gap"
        )
        ref = ref_evidence_gap(
            "She", 0, 3, candidates=["Alice", "Bob"], question="Who is 'she'?"
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, [ref])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.status is ResolutionStatus.EVIDENCE_GAP
        assert resolution.statement == candidate.text
        assert resolution.verbatim == candidate.text
        assert not resolution.degraded
        assert 2 <= len(resolution.references[0].candidates) <= 4
        assert resolution.references[0].question

        entry_id = DecisionLog().assemble(
            "agent-gap-status", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry.statement == candidate.text
        assert entry.verbatim == candidate.text
        assert "res:evidence_gap" in entry.subjects

        assert ResolutionLog().write(
            "agent-gap-status",
            candidate.turn_id,
            candidate.candidate_id,
            resolution,
            entry_id=entry_id,
        )
        record = ResolutionLog().get(
            "agent-gap-status", candidate.turn_id, candidate.candidate_id
        )
        assert record.status == "evidence_gap"
        stored_ref = json.loads(record.references_json)[0]
        assert stored_ref["candidates"] == ["Alice", "Bob"]
        assert stored_ref["question"] == "Who is 'she'?"


class TestIndeterminateStatus:
    def test_indeterminate_reference_updates_statement_verbatim_tag_and_sidecar(self):
        candidate = make_candidate(
            "She deployed it.", candidate_id="t-ind:sent:0", turn_id="t-ind"
        )
        ref = ref_indeterminate("She", 0, 3, kind="pronoun")
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, [ref])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.status is ResolutionStatus.INDETERMINATE
        assert resolution.statement == candidate.text
        assert resolution.verbatim == candidate.text
        # A model-emitted abstention is NOT the same as a degraded run.
        assert not resolution.degraded

        entry_id = DecisionLog().assemble(
            "agent-ind-status", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry.statement == candidate.text
        assert entry.verbatim == candidate.text
        assert "res:indeterminate" in entry.subjects

        assert ResolutionLog().write(
            "agent-ind-status",
            candidate.turn_id,
            candidate.candidate_id,
            resolution,
            entry_id=entry_id,
        )
        record = ResolutionLog().get(
            "agent-ind-status", candidate.turn_id, candidate.candidate_id
        )
        assert record.status == "indeterminate"
        assert record.degraded is False


# ===========================================================================
# (d, last bullet) empty references array -- resolved, not degraded, not
# conflated with indeterminate.
# ===========================================================================


class TestEmptyReferencesArray:
    def test_empty_references_is_resolved_not_degraded(self):
        candidate = make_candidate(
            "Nothing to resolve here.",
            candidate_id="t-emptyrefs:sent:0",
            turn_id="t-emptyrefs",
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, [])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert resolution.status is ResolutionStatus.RESOLVED
        assert resolution.statement == resolution.verbatim == candidate.text
        assert resolution.valid_from is None
        assert resolution.subject_tag == "res:resolved"
        assert resolution.subject_tag != "res:indeterminate"


# ===========================================================================
# (b) The valid_from matrix
# ===========================================================================


class TestValidFromMatrix:
    def _resolve(self, text, ref, turn_id, tz="UTC"):
        refs = ref if isinstance(ref, list) else [ref]
        candidate = make_candidate(
            text, candidate_id=f"{turn_id}:sent:0", turn_id=turn_id
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, refs)
        )
        context = TurnContext(timezone=tz)
        return candidate, resolve_references(
            candidate, candidate.text, context, client=client
        )

    def test_onset_emits_valid_from(self):
        text = "She's been on Atlas since March."
        surface = "since March"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 1, 2026",
            temporal_role="onset",
            resolved_iso="2026-03-01T00:00:00+00:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-onset")
        assert resolution.valid_from is not None
        assert resolution.status is ResolutionStatus.RESOLVED

    def test_deadline_does_not_emit_valid_from(self):
        text = "File the report by Friday."
        surface = "by Friday"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 6, 2026",
            temporal_role="deadline",
            resolved_iso="2026-03-06T00:00:00+00:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-deadline")
        assert resolution.valid_from is None
        assert resolution.status is ResolutionStatus.RESOLVED

    def test_mention_does_not_emit_valid_from(self):
        text = "We talked about March in passing."
        surface = "March"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March",
            temporal_role="mention",
            resolved_iso="2026-03-01T00:00:00+00:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-mention")
        assert resolution.valid_from is None

    def test_zero_onsets_emits_no_valid_from(self):
        text = "She deployed it."
        ref = ref_resolved("She", 0, 3, kind="pronoun", resolved_text="Alice")
        _, resolution = self._resolve(text, ref, "t-vf-zero")
        assert resolution.valid_from is None
        assert resolution.status is ResolutionStatus.RESOLVED

    def test_two_onsets_abstain_with_assumed_status_and_tag(self):
        text = "She's been on Atlas since March and lead since June."
        s1 = "since March"
        s2 = "since June"
        start1 = text.index(s1)
        start2 = text.index(s2)
        ref1 = ref_resolved(
            s1,
            start1,
            start1 + len(s1),
            kind="relative_time",
            resolved_text="March 1, 2026",
            temporal_role="onset",
            resolved_iso="2026-03-01T00:00:00+00:00",
        )
        ref2 = ref_resolved(
            s2,
            start2,
            start2 + len(s2),
            kind="relative_time",
            resolved_text="June 1, 2026",
            temporal_role="onset",
            resolved_iso="2026-06-01T00:00:00+00:00",
        )
        candidate, resolution = self._resolve(text, [ref1, ref2], "t-vf-two")

        assert resolution.valid_from is None
        assert resolution.status is ResolutionStatus.ASSUMED
        assumption_refs = [r for r in resolution.references if r.assumption]
        assert assumption_refs, "a non-empty assumption line must be present"
        assert resolution.subject_tag == "res:assumed"

        entry_id = DecisionLog().assemble(
            "agent-vf-two", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert "res:assumed" in entry.subjects

    def test_future_onset_emits_valid_from(self):
        text = "The rollout starts since 2099."
        surface = "since 2099"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="2099",
            temporal_role="onset",
            resolved_iso="2099-01-01T00:00:00+00:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-future")
        assert resolution.valid_from is not None
        assert resolution.valid_from > time.time()

    def test_non_utc_timezone_anchors_a_naive_resolved_iso(self):
        text = "It's been live since last Tuesday."
        surface = "since last Tuesday"
        start = text.index(surface)
        # naive (no tzinfo) resolved_iso -- anchored to the context timezone.
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 3, 2026",
            temporal_role="onset",
            resolved_iso="2026-03-03T00:00:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-tz", tz="America/New_York")
        assert resolution.valid_from is not None

        from zoneinfo import ZoneInfo
        from datetime import datetime

        expected = datetime(
            2026, 3, 3, 0, 0, 0, tzinfo=ZoneInfo("America/New_York")
        ).timestamp()
        assert resolution.valid_from == pytest.approx(expected)

    def test_dst_boundary_date_does_not_raise(self):
        # 2026-03-08 is the US spring-forward DST boundary (2am -> 3am).
        text = "It kicked off since the DST switch."
        surface = "since the DST switch"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 8, 2026",
            temporal_role="onset",
            resolved_iso="2026-03-08T02:30:00",
        )
        _, resolution = self._resolve(text, ref, "t-vf-dst", tz="America/New_York")
        assert resolution.valid_from is not None
        assert math.isfinite(resolution.valid_from)

    def test_unknown_timezone_falls_back_to_utc_with_a_warning(self, caplog):
        text = "It's been live since March."
        surface = "since March"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 1, 2026",
            temporal_role="onset",
            resolved_iso="2026-03-01T00:00:00",
        )
        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            _, resolution = self._resolve(
                text, ref, "t-vf-badtz", tz="Nonexistent/Zone"
            )
        assert resolution.valid_from is not None
        assert math.isfinite(resolution.valid_from)
        assert any("unknown timezone" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "roles, temporal_role, expect_emission",
        [
            (("onset",), "onset", True),
            (("onset",), "deadline", False),
            (("onset", "deadline"), "deadline", True),
        ],
        ids=[
            "default-onset-emits",
            "default-deadline-abstains",
            "widened-deadline-emits",
        ],
    )
    def test_emission_is_parameterised_over_m4_valid_from_roles(
        self, roles, temporal_role, expect_emission
    ):
        """Success Criterion / Decision 4: a maintainer reversal of the
        onset-only rule is a one-tuple change to
        Defaults.M4_VALID_FROM_ROLES, proven here by widening it to
        include "deadline" and asserting that case then DOES emit."""
        Defaults.M4_VALID_FROM_ROLES = roles

        text = "File the report by Friday."
        surface = "by Friday"
        start = text.index(surface)
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 6, 2026",
            temporal_role=temporal_role,
            resolved_iso="2026-03-06T00:00:00+00:00",
        )
        _, resolution = self._resolve(text, ref, f"t-vf-param-{temporal_role}")

        if expect_emission:
            assert resolution.valid_from is not None
        else:
            assert resolution.valid_from is None


# ===========================================================================
# (c) Failure Path Test Strategy
# ===========================================================================


class TestClientAndDependencyFailures:
    def test_client_raise_produces_degraded_resolution_with_warning(self, caplog):
        candidate = make_candidate(
            "Something happened.",
            candidate_id="t-clientraise:sent:0",
            turn_id="t-clientraise",
        )
        client = FakeClient(raise_exc=RuntimeError("api exploded"))

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            resolution = resolve_references(
                candidate, candidate.text, TurnContext.now(), client=client
            )

        assert resolution.degraded is True
        assert resolution.status is ResolutionStatus.INDETERMINATE
        assert resolution.statement == resolution.verbatim == candidate.text
        assert any(
            candidate.candidate_id in r.message for r in caplog.records
        ), "the warning must name the candidate id"

    def test_anthropic_unavailable_degrades_without_a_network_call(self, monkeypatch):
        monkeypatch.setattr(resolution_mod, "anthropic_module", None)
        monkeypatch.setattr(resolution_mod, "_anthropic_available", False)

        calls = []
        original_request = resolution_mod._request_resolution

        def spy(*args, **kwargs):
            calls.append(args)
            return original_request(*args, **kwargs)

        monkeypatch.setattr(resolution_mod, "_request_resolution", spy)

        candidate = make_candidate(
            "Something happened.",
            candidate_id="t-noanthropic:sent:0",
            turn_id="t-noanthropic",
        )
        resolution = resolve_references(candidate, candidate.text, TurnContext.now())

        assert resolution.degraded is True
        assert resolution.status is ResolutionStatus.INDETERMINATE
        assert resolution.statement == resolution.verbatim == candidate.text
        assert calls == [], "no network call attempted when anthropic is unavailable"


class TestParseReplyRejections:
    """Each malformed-reply case, asserted individually, per the plan's
    Failure Path Test Strategy. Every case produces EITHER a dropped
    reference (others survive, resolution NOT degraded) OR a degraded
    envelope -- which one is asserted explicitly per case."""

    def test_malformed_json_degrades(self):
        candidate = make_candidate(
            "Alice deployed it.", candidate_id="t-badjson:sent:0", turn_id="t-badjson"
        )
        client = FakeClient(response_text="{not valid json")

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is True
        assert resolution.statement == resolution.verbatim == candidate.text

    def test_wrong_candidate_id_degrades(self):
        candidate = make_candidate(
            "Alice deployed it.", candidate_id="t-wrongid:sent:0", turn_id="t-wrongid"
        )
        client = FakeClient(
            response_text=reply_json("someone-elses-candidate-id", candidate.text, [])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is True
        assert resolution.statement == resolution.verbatim == candidate.text

    def test_unknown_enum_member_drops_the_reference_others_survive(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-badenum:sent:0",
            turn_id="t-badenum",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = dict(ref_resolved("She", 0, 3, kind="pronoun", resolved_text="Alice"))
        bad_ref["status"] = "bogus-status"
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert (
            resolution.degraded is False
        ), "a dropped reference must not degrade the envelope"
        assert len(resolution.references) == 1
        assert resolution.references[0].surface == "Bob"
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_surface_not_a_substring_at_offsets_drops_the_reference(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-badoffset:sent:0",
            turn_id="t-badoffset",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = ref_resolved(
            "Nope", 0, 3, kind="pronoun", resolved_text="Alice"
        )  # "She"[0:3] != "Nope"
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert len(resolution.references) == 1
        assert resolution.references[0].surface == "Bob"
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_evidence_gap_with_one_candidate_drops_the_reference(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-gap1:sent:0",
            turn_id="t-gap1",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = ref_evidence_gap("She", 0, 3, candidates=["Alice"])
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert len(resolution.references) == 1
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_evidence_gap_with_five_candidates_drops_the_reference(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-gap5:sent:0",
            turn_id="t-gap5",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = ref_evidence_gap("She", 0, 3, candidates=["A", "B", "C", "D", "E"])
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert len(resolution.references) == 1
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_assumed_with_empty_assumption_drops_the_reference(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-emptyassume:sent:0",
            turn_id="t-emptyassume",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = ref_assumed(
            "She", 0, 3, kind="pronoun", resolved_text="Alice", assumption="   "
        )
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert len(resolution.references) == 1
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_assumed_with_multiline_assumption_drops_the_reference(self):
        candidate = make_candidate(
            "She and Bob deployed it.",
            candidate_id="t-multilineassume:sent:0",
            turn_id="t-multilineassume",
        )
        good_ref = ref_resolved(
            "Bob", 8, 11, kind="definite_reference", resolved_text="Bob"
        )
        bad_ref = ref_assumed(
            "She",
            0,
            3,
            kind="pronoun",
            resolved_text="Alice",
            assumption="line one\nline two",
        )
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, candidate.text, [good_ref, bad_ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is False
        assert len(resolution.references) == 1
        assert resolution.status is ResolutionStatus.INDETERMINATE

    def test_statement_exceeding_growth_bound_degrades(self):
        candidate = make_candidate(
            "Short.", candidate_id="t-toolong:sent:0", turn_id="t-toolong"
        )
        max_len = (
            Defaults.M4_STATEMENT_MAX_GROWTH_FACTOR * len(candidate.text)
            + Defaults.M4_STATEMENT_MAX_GROWTH_CHARS
        )
        bloated_statement = "x" * (int(max_len) + 1)
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, bloated_statement, [])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is True
        assert resolution.statement == resolution.verbatim == candidate.text


class TestResolutionLogWriteFailure:
    def test_sidecar_write_failure_does_not_lose_the_journal_entry(
        self, monkeypatch, caplog
    ):
        def raising_save(self):
            raise RuntimeError("sidecar write exploded")

        monkeypatch.setattr(resolution_log_mod.ResolutionRecord, "save", raising_save)

        candidate = make_candidate(
            "Alice deployed it.",
            candidate_id="t-sidecarfail:sent:0",
            turn_id="t-sidecarfail",
        )
        resolution = Resolution(
            statement=candidate.text,
            verbatim=candidate.text,
            status=ResolutionStatus.RESOLVED,
        )

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            entry_id = DecisionLog().assemble(
                "agent-sidecarfail", candidate, ProvenanceJournal, resolution=resolution
            )

        assert entry_id, "assemble must still return the entry_id"
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry is not None
        assert "res:resolved" in entry.subjects
        assert any("ResolutionLog.write failed" in r.message for r in caplog.records)


class TestResolutionProviderRaises:
    def test_raising_resolution_provider_still_captures_the_candidate(self):
        memory = SubconsciousMemory(
            agent_id="agent-resprovider-explode",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_all=True),
                journal=ProvenanceJournal,
                resolution_provider=_RaisingResolutionProvider(),
            ),
        )

        facts = memory.extract_memories(
            "Alice deployed the service.", turn_id="t-resprovider-explode"
        )

        assert facts
        assert facts[0].resolution_status == "indeterminate"
        entries = list(JournalEntry.query.filter(turn_id="t-resprovider-explode"))
        assert entries
        assert any("res:degraded" in e.subjects for e in entries)

    def test_degraded_empty_resolution_skips_the_sidecar_write(self):
        """A degraded, reference-less resolution must not write a sidecar row.

        ``_resolve_for``'s fail-open fallback builds ``degraded=True,
        references=()`` -- that row would carry nothing but
        ``references_json="[]"``, duplicating the ``res:degraded``
        journal subject tag with no detail of its own. The write must be
        skipped entirely rather than persisting that empty artifact.
        """
        memory = SubconsciousMemory(
            agent_id="agent-resprovider-degraded-empty",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_all=True),
                journal=ProvenanceJournal,
                resolution_provider=_RaisingResolutionProvider(),
            ),
        )

        facts = memory.extract_memories(
            "Alice deployed the service.",
            turn_id="t-resprovider-degraded-empty",
        )

        assert facts
        candidate_id = facts[0].candidate_id
        row = ResolutionLog().get(
            "agent-resprovider-degraded-empty",
            "t-resprovider-degraded-empty",
            candidate_id,
        )
        assert (
            row is None
        ), "degraded-and-empty resolutions must not write a sidecar row"


# ===========================================================================
# (d) Empty/invalid input handling
# ===========================================================================


class TestEmptyOrInvalidInput:
    def test_empty_candidate_span_degrades_without_a_client_call(self):
        candidate = make_candidate(
            "   ", candidate_id="t-blank:sent:0", turn_id="t-blank"
        )
        client = FakeClient(response_text=reply_json(candidate.candidate_id, "x", []))

        resolution = resolve_references(
            candidate, "irrelevant turn text", TurnContext.now(), client=client
        )

        assert resolution.degraded is True
        assert client.calls == []

    def test_context_with_empty_window_none_speaker_and_unknown_timezone(self, caplog):
        context = TurnContext(
            speaker=None,
            captured_at=time.time(),
            timezone="Nonexistent/Zone",
            window=(),
        )
        text = "It starts since March."
        surface = "since March"
        start = text.index(surface)
        candidate = make_candidate(
            text, candidate_id="t-ctxedge:sent:0", turn_id="t-ctxedge"
        )
        ref = ref_resolved(
            surface,
            start,
            start + len(surface),
            kind="relative_time",
            resolved_text="March 1",
            temporal_role="onset",
            resolved_iso="2026-03-01T00:00:00",
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, [ref])
        )

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            resolution = resolve_references(
                candidate, candidate.text, context, client=client
            )

        # nothing raised, valid_from still computed via UTC fallback
        assert resolution.valid_from is not None
        assert any("unknown timezone" in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad_captured_at", [None, math.nan, math.inf, -math.inf])
    def test_turn_context_coerces_non_finite_captured_at(self, bad_captured_at, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            context = TurnContext(captured_at=bad_captured_at)

        assert math.isfinite(context.captured_at)
        assert any("captured_at" in r.message for r in caplog.records)

    def test_non_finite_valid_from_is_dropped_before_construction_and_entry_survives(
        self, monkeypatch
    ):
        """Round-1 blocker 3: without this guard the raise lands in
        _append_and_transition's except and the candidate is rejected as
        ASSEMBLY_FAILED instead of captured."""
        monkeypatch.setattr(
            resolution_mod,
            "_compute_valid_from",
            lambda references: (float("nan"), False, None),
        )
        candidate = make_candidate(
            "She deployed it.", candidate_id="t-nanvf:sent:0", turn_id="t-nanvf"
        )
        ref = ref_resolved("She", 0, 3, kind="pronoun", resolved_text="Alice")
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, "Alice deployed it.", [ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.valid_from is None, "the guard must drop the non-finite value"
        assert resolution.degraded is False

        entry_id = DecisionLog().assemble(
            "agent-nanvf", candidate, ProvenanceJournal, resolution=resolution
        )
        assert entry_id, "the candidate must still be captured"
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry is not None
        assert "res:resolved" in entry.subjects

    def test_non_finite_valid_from_inf_variant(self, monkeypatch):
        monkeypatch.setattr(
            resolution_mod,
            "_compute_valid_from",
            lambda references: (float("inf"), False, None),
        )
        candidate = make_candidate(
            "She deployed it.", candidate_id="t-infvf:sent:0", turn_id="t-infvf"
        )
        ref = ref_resolved("She", 0, 3, kind="pronoun", resolved_text="Alice")
        client = FakeClient(
            response_text=reply_json(
                candidate.candidate_id, "Alice deployed it.", [ref]
            )
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.valid_from is None
        entry_id = DecisionLog().assemble(
            "agent-infvf", candidate, ProvenanceJournal, resolution=resolution
        )
        assert entry_id
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert entry is not None


# ===========================================================================
# (e) The fifth tag literal: res:degraded
# ===========================================================================


class TestDegradedTagLiteral:
    def test_degraded_entry_is_tagged_res_degraded_not_res_indeterminate(self):
        candidate = make_candidate(
            "Something happened.", candidate_id="t-degtag:sent:0", turn_id="t-degtag"
        )
        client = FakeClient(raise_exc=RuntimeError("boom"))

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.degraded is True
        assert resolution.subject_tag == "res:degraded"

        entry_id = DecisionLog().assemble(
            "agent-degtag", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)
        assert "res:degraded" in entry.subjects
        assert "res:indeterminate" not in entry.subjects


# ===========================================================================
# (f) Kill-switch parity
# ===========================================================================


class TestKillSwitchParity:
    def test_disabled_switch_skips_the_client_and_stays_indeterminate_degraded(self):
        Defaults.M4_RESOLUTION_ENABLED = False
        candidate = make_candidate(
            "Alice deployed the service.",
            candidate_id="t-killswitch:sent:0",
            turn_id="t-killswitch",
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, "should never be used", [])
        )

        resolution = resolve_references(
            candidate, candidate.text, TurnContext.now(), client=client
        )

        assert resolution.statement == resolution.verbatim == candidate.text
        assert resolution.degraded is True
        assert client.calls == []

    def test_disabled_switch_end_to_end_matches_m3_byte_identically(self):
        Defaults.M4_RESOLUTION_ENABLED = False
        memory = SubconsciousMemory(
            agent_id="agent-killswitch-e2e",
            auditable_extraction=AuditableExtractionConfig(
                verdict_provider=_StubVerdict(accept_all=True),
                journal=ProvenanceJournal,
            ),
        )

        facts = memory.extract_memories(
            "Alice deployed the service.", turn_id="t-killswitch-e2e"
        )

        assert facts
        entries = list(JournalEntry.query.filter(turn_id="t-killswitch-e2e"))
        assert entries
        for entry in entries:
            assert entry.statement == entry.verbatim
            assert not any(
                s.startswith("res:") for s in entry.subjects
            ), "no res: tag at all when the switch is off"
            cand_tag = next(s for s in entry.subjects if s.startswith("cand:"))
            candidate_id = cand_tag[len("cand:") :]
            row = ResolutionLog().get(
                "agent-killswitch-e2e", entry.turn_id, candidate_id
            )
            assert row is None, "no sidecar row when the switch is off"
        for fact in facts:
            assert fact.verbatim is None
            assert fact.resolution_status is None
            assert fact.assumption is None


# ===========================================================================
# (g) speaker / captured_at reach the journal entry
# ===========================================================================


class TestContextReachesJournalEntry:
    def test_speaker_and_captured_at_from_context_reach_the_entry(self):
        captured_at = time.time() - 500
        context = TurnContext(
            speaker="alice", captured_at=captured_at, timezone="UTC", window=()
        )
        candidate = make_candidate(
            "Bob is on call.", candidate_id="t-ctxreach:sent:0", turn_id="t-ctxreach"
        )
        client = FakeClient(
            response_text=reply_json(candidate.candidate_id, candidate.text, [])
        )

        resolution = resolve_references(
            candidate, candidate.text, context, client=client
        )

        entry_id = DecisionLog().assemble(
            "agent-ctxreach", candidate, ProvenanceJournal, resolution=resolution
        )
        entry = JournalEntry.query.get(entry_id=entry_id)

        assert entry.speaker == "alice"
        assert entry.captured_at == pytest.approx(captured_at)
