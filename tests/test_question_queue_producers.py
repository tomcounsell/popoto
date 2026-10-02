"""M7 question-queue producers and end-to-end paths (#566).

Covers the three thin producer adapters in ``recipes/question_queue.py``:

- ``propose_from_disjunctions`` over real M5 ``disjoin`` annotations, including
  the retraction case (a retracted disjoin expires its candidate, never asks);
- ``propose_from_gate`` over real ``ContextAssembler.assemble()`` metadata;
- ``propose_from_resolution`` over an M4 ``ResolutionRecord``'s
  ``evidence_gap`` references;

plus the per-producer-absent guarantee (the queue still delivers with each
producer missing in turn) and the two end-to-end criteria from the plan's
Revision 2: the gate-refusal path moves a ConfidenceField, and the disjunction
path records ``answer_option`` while leaving the ``JournalEntry`` sides
untouched.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

from src import popoto
from src.popoto.extraction.resolution_log import ResolutionRecord
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.recipes import question_queue as qq
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
from src.popoto.recipes.question_queue import QuestionCandidate
from src.popoto.recipes.reconciliation import merge_log_entries, reconcile_entry
from src.popoto.redis_db import get_REDIS_DB

AGENT = "qq-producer-agent"
K = qq.QUESTION_BUDGET_TURNS

#: The canonical module the producers close over (``import popoto`` and
#: ``import src.popoto`` collapse onto one module), used to hide M5.
QQ_MODULE = sys.modules[qq.propose.__module__]
RECON_MODULE_NAME = QQ_MODULE.__name__.rsplit(".", 1)[0] + ".reconciliation"


class QQGateMemory(popoto.Model):
    """A pull-path model with a ConfidenceField, for the gate-refusal path."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    topic = popoto.Field(type=str)
    relevance = popoto.DecayingSortedField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)


@pytest.fixture(autouse=True)
def _bound_db_is_not_zero(monkeypatch):
    db = get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0)
    assert int(db) != 0, "question-queue tests refuse to run on Redis DB 0"
    monkeypatch.delenv("POPOTO_QUESTION_QUEUE_DISABLE", raising=False)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _SameJudge:
    """An Anthropic-shaped judge client that always answers ``same``."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        reply = type("Reply", (), {})()
        reply.content = [_Block(json.dumps({"verdict": "same"}))]
        return reply


def _m5_disjunction(agent=AGENT):
    """A real M5 precedence tie -> ``disjoin`` annotation. Returns the sides."""
    sides = []
    for statement in ("deadline is monday", "deadline is friday"):
        sides.append(
            ProvenanceJournal.append(
                agent_id=agent,
                statement=statement,
                subjects=["launch"],
                claim_type="deadline",
                captured_at=100.0,
            ).entry
        )
    reconcile_entry(sides[0], client=_SameJudge())
    outcome = reconcile_entry(sides[1], client=_SameJudge())
    assert outcome.action == "disjoined"
    return sides


def _disjoin_annotations(agent=AGENT):
    return [e for e in merge_log_entries(agent) if e.kind == "disjoin"]


def _gate_refusal(agent=AGENT, topic="deploy target"):
    """A real ``assemble()`` refusal. Returns (record, metadata)."""
    record = QQGateMemory(agent_id=agent, topic=topic)
    record.save()  # confidence 0.5
    assembler = ContextAssembler(
        model_class=QQGateMemory,
        score_weights={"relevance": 1.0},
        retrieval_mode="composite",
        confidence_gate_threshold=0.9,  # 0.5 < 0.9 -> refused
        confidence_gate_mode="refuse",
    )
    result = assembler.assemble(
        query_cues={"topic": topic}, partition_filters={"agent_id": agent}
    )
    gate = result.metadata["gate"]
    assert gate["gated"] is True and result.metadata["pull_count"] == 0
    assert gate["refused_keys"][0] == record.db_key.redis_key
    return record, result.metadata


def _resolution_record(agent=AGENT, turn_id="t-1", refs=None):
    if refs is None:
        refs = [
            {
                "surface": "she",
                "start": 0,
                "end": 3,
                "status": "evidence_gap",
                "candidates": ["Dana", "Priya"],
                "question": "Who is 'she' -- Dana or Priya?",
            },
            {"surface": "today", "start": 10, "end": 15, "status": "resolved"},
        ]
    record = ResolutionRecord(
        agent_id=agent,
        turn_id=turn_id,
        candidate_id=f"{turn_id}:sentence:0",
        status="evidence_gap",
        references_json=json.dumps(refs),
    )
    record.save()
    return record


def _candidates(agent=AGENT):
    return list(QuestionCandidate.query.filter(agent_id=agent))


def _reload(cand):
    return QuestionCandidate.query.get(redis_key=cand.db_key.redis_key)


# ---------------------------------------------------------------------------
# Producer happy paths
# ---------------------------------------------------------------------------


class TestDisjunctionProducer:
    def test_proposes_mirrored_exclusive_options(self):
        a, b = _m5_disjunction()
        annotation = _disjoin_annotations()[0]
        assert qq.propose_from_disjunctions(AGENT, turn=1) == 1

        (cand,) = _candidates()
        assert cand.kind == "disjunction"
        assert cand.ambiguity_signal == "disjunction"
        assert cand.source_module == "reconciliation"
        assert json.loads(annotation.payload)["disjunction_id"] == (cand.disjunction_id)
        sides = {e.pk: e.statement for e in (a, b)}
        ka, kb = cand.target_keys
        assert set(cand.target_keys) == set(sides)
        assert cand.options == [
            {"label": sides[ka], "acted": [ka], "contradicted": [kb]},
            {"label": sides[kb], "acted": [kb], "contradicted": [ka]},
        ]

    def test_reobservation_touches_rather_than_duplicates(self):
        _m5_disjunction()
        qq.propose_from_disjunctions(AGENT, turn=1)
        assert qq.propose_from_disjunctions(AGENT, turn=4) == 1
        (cand,) = _candidates()
        assert cand.last_seen_turn == 4

    def test_retracted_disjoin_expires_instead_of_delivering(self):
        _m5_disjunction()
        qq.propose_from_disjunctions(AGENT, turn=1)
        (cand,) = _candidates()

        ProvenanceJournal.retract(_disjoin_annotations()[0], agent_id=AGENT)
        assert _disjoin_annotations() == []

        assert qq.next_question(AGENT, turn=2) is None
        stored = _reload(cand)
        assert stored.status == "expired" and stored.ask_count == 0
        # The budget was not spent on a question that was never asked.
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) is None

    def test_unreadable_m5_neither_expires_nor_delivers(self, monkeypatch):
        """If M5 vanishes after proposal, staleness is unknown: fail closed."""
        _m5_disjunction()
        qq.propose_from_disjunctions(AGENT, turn=1)
        (cand,) = _candidates()
        _hide_m5(monkeypatch)
        assert qq.next_question(AGENT, turn=2) is None
        assert _reload(cand).status == "pending"

    def test_live_disjoin_is_not_stale(self):
        _m5_disjunction()
        qq.propose_from_disjunctions(AGENT, turn=1)
        assert qq.next_question(AGENT, turn=2) is not None


class TestGateProducer:
    def test_proposes_confirmation_on_top_refused_key(self):
        record, metadata = _gate_refusal()
        cand = qq.propose_from_gate(AGENT, metadata, turn=1, query_text="deploy")
        key = record.db_key.redis_key
        assert cand.kind == "confirmation"
        assert cand.ambiguity_signal == "gate_refusal"
        assert cand.target_keys == [key]
        assert cand.options == [
            {"label": "yes", "acted": [key], "contradicted": []},
            {"label": "no", "acted": [], "contradicted": [key]},
        ]
        assert "deploy" in cand.cue_tokens

    @pytest.mark.parametrize(
        "metadata",
        [
            {},  # no threshold configured: dormant
            {"gate": {"applied": False, "gated": False}},
            {"gate": {"applied": True, "gated": False}},
            {"gate": {"applied": True, "gated": True}},  # no refused_keys
            {"gate": {"applied": True, "gated": True, "refused_keys": []}},
            None,
            "garbage",
        ],
    )
    def test_no_refusal_proposes_nothing(self, metadata):
        assert qq.propose_from_gate(AGENT, metadata, turn=1, query_text="x") is None
        assert _candidates() == []

    def test_dormant_without_a_threshold(self):
        QQGateMemory(agent_id=AGENT, topic="deploy").save()
        result = ContextAssembler(
            model_class=QQGateMemory,
            score_weights={"relevance": 1.0},
            retrieval_mode="composite",
        ).assemble(query_cues={"topic": "deploy"}, partition_filters={})
        assert qq.propose_from_gate(AGENT, result, turn=1, query_text="x") is None


class TestResolutionProducer:
    def test_one_referent_question_per_evidence_gap(self):
        record = _resolution_record()
        assert qq.propose_from_resolution(record, turn=1) == 1
        (cand,) = _candidates()
        assert cand.kind == "referent"
        assert cand.ambiguity_signal == "evidence_gap"
        assert cand.question_text == "Who is 'she' -- Dana or Priya?"
        assert cand.options == [
            {"label": "Dana", "acted": [], "contradicted": []},
            {"label": "Priya", "acted": [], "contradicted": []},
        ]
        assert cand.target_keys == [f"{record.db_key.redis_key}#ref:0:3"]

    def test_two_gaps_in_one_record_are_two_questions(self):
        gap = {"status": "evidence_gap", "candidates": ["Dana", "Priya"]}
        record = _resolution_record(
            refs=[
                dict(gap, surface="she", start=0, end=3, question="Who is she?"),
                dict(gap, surface="her", start=9, end=12, question="Who is her?"),
            ]
        )
        assert qq.propose_from_resolution(record, turn=1) == 2
        assert len(_candidates()) == 2

    def test_answer_is_recorded_only(self):
        qq.propose_from_resolution(_resolution_record(), turn=1)
        q = qq.next_question(AGENT, turn=1)
        result = qq.record_answer(q, "Priya", turn=2)
        assert result.applied and result.option_index == 1
        assert _reload(q).answer_option == 1

    @pytest.mark.parametrize(
        "refs_json", ["not json", "{}", json.dumps([{"status": "evidence_gap"}])]
    )
    def test_malformed_record_fails_closed(self, refs_json):
        record = ResolutionRecord(
            agent_id=AGENT, turn_id="t", candidate_id="t:s:0", references_json="[]"
        )
        record.references_json = refs_json
        assert qq.propose_from_resolution(record, turn=1) == 0
        assert qq.propose_from_resolution(object(), turn=1) == 0


# ---------------------------------------------------------------------------
# Independence: each producer absent in turn, the queue still delivers
# ---------------------------------------------------------------------------


def _hide_m5(monkeypatch):
    """Make ``reconciliation`` unimportable, and prove the hiding took."""
    monkeypatch.setitem(sys.modules, RECON_MODULE_NAME, None)
    with pytest.raises(ImportError):
        QQ_MODULE._reconciliation()


def _run_producers(absent, monkeypatch):
    """Feed the queue from every producer except ``absent``."""
    if absent == "disjunction":
        # M5 missing entirely: a real disjoin exists, but the lazy import
        # fails, so the producer degrades to "no input".
        _m5_disjunction()
        _hide_m5(monkeypatch)
        assert qq.propose_from_disjunctions(AGENT, turn=1) == 0
    else:
        _m5_disjunction()
        assert qq.propose_from_disjunctions(AGENT, turn=1) == 1
    if absent != "gate":
        _, metadata = _gate_refusal(topic="schedule")
        assert qq.propose_from_gate(AGENT, metadata, 1, "schedule") is not None
    if absent != "resolution":
        assert qq.propose_from_resolution(_resolution_record(), turn=1) == 1


@pytest.mark.parametrize("absent", ["disjunction", "gate", "resolution", "all"])
def test_queue_works_with_each_producer_absent(absent, monkeypatch):
    if absent == "all":
        _m5_disjunction()
        _hide_m5(monkeypatch)
        assert qq.propose_from_disjunctions(AGENT, turn=1) == 0
        assert _candidates() == []
        assert qq.next_question(AGENT, turn=1) is None
        return
    _run_producers(absent, monkeypatch)
    assert len(_candidates()) == 2
    delivered = qq.next_question(AGENT, turn=1)
    assert delivered is not None
    expected_signals = {"disjunction", "gate_refusal", "evidence_gap"} - {
        {"disjunction": "disjunction", "gate": "gate_refusal"}.get(
            absent, "evidence_gap"
        )
    }
    assert delivered.ambiguity_signal in expected_signals
    # The other one is delivered after the budget refills.
    assert qq.next_question(AGENT, turn=1 + K) is not None


def test_producers_honour_the_kill_switch(monkeypatch):
    _m5_disjunction()
    _, metadata = _gate_refusal()
    record = _resolution_record()
    monkeypatch.setenv("POPOTO_QUESTION_QUEUE_DISABLE", "1")
    assert qq.propose_from_disjunctions(AGENT, turn=1) == 0
    assert qq.propose_from_gate(AGENT, metadata, 1, "deploy") is None
    assert qq.propose_from_resolution(record, turn=1) == 0
    monkeypatch.delenv("POPOTO_QUESTION_QUEUE_DISABLE")
    assert _candidates() == []


def test_producers_fail_closed_on_redis_error(monkeypatch):
    _m5_disjunction()
    _, metadata = _gate_refusal()

    def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(QQ_MODULE, "_candidates", boom)
    assert qq.propose_from_disjunctions(AGENT, turn=1) == 0
    assert qq.propose_from_gate(AGENT, metadata, 1, "deploy") is None
    assert qq.propose_from_resolution(_resolution_record(), turn=1) == 0


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_end_to_end_gate_refusal_moves_confidence():
    """assemble() refuses -> propose_from_gate -> next_question ->
    record_answer: exactly one delivery within K turns, and the refused
    fact's confidence moves."""
    record, metadata = _gate_refusal()
    before = ConfidenceField.get_confidence(record, "confidence")

    assert qq.propose_from_gate(AGENT, metadata, 1, "deploy target") is not None
    # A second refusal on a different fact competes for the same budget.
    _, metadata2 = _gate_refusal(topic="deploy region")
    assert qq.propose_from_gate(AGENT, metadata2, 1, "deploy region") is not None

    deliveries = [q for t in range(1, 1 + K) if (q := qq.next_question(AGENT, turn=t))]
    assert len(deliveries) == 1

    q = deliveries[0]
    result = qq.record_answer(q, "yes", turn=2)
    assert result.applied and result.reason == "applied"
    target = QQGateMemory.query.get(redis_key=q.target_keys[0])
    assert ConfidenceField.get_confidence(target, "confidence") > before
    if q.target_keys[0] == record.db_key.redis_key:
        assert ConfidenceField.get_confidence(record, "confidence") > before


def test_end_to_end_disjunction_records_answer_and_leaves_sides_untouched():
    """M5 disjoin -> propose -> next_question -> record_answer: the answer is
    recorded on the candidate; the JournalEntry sides (no ConfidenceField)
    keep their validity and every stored byte."""
    a, b = _m5_disjunction()
    redis = get_REDIS_DB()
    snapshot = {e.pk: redis.hgetall(e.pk) for e in (a, b)}

    assert qq.propose_from_disjunctions(AGENT, turn=1) == 1
    deliveries = [q for t in range(1, 1 + K) if (q := qq.next_question(AGENT, turn=t))]
    assert len(deliveries) == 1
    q = deliveries[0]
    result = qq.record_answer(q, "2", turn=3)
    assert result.applied and result.option_index == 1

    stored = _reload(q)
    assert stored.status == "answered" and stored.answer_option == 1
    assert {e.pk: redis.hgetall(e.pk) for e in (a, b)} == snapshot
    live = {e.pk for e in JournalEntry.query.filter(validity__current=True)}
    assert {a.pk, b.pk} <= live
