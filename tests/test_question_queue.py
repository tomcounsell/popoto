"""Tests for the M7 question queue (recipes/question_queue.py, #566).

Covers: propose + dedup, the VOI gate truth table, the structural token bucket
(concurrent hammer + regressed turn), expiry, relevance timing, cooldown, the
answer path (evidence, defeasibility, deflection safety, idempotence, fault
after claim), the kill switch, fail-closed behaviour, VALID_OUTCOMES
stability, free-text non-storage and prune.
"""

import os
import sys
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
import redis

from src import popoto
from src.popoto.fields import observation
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.redis_db import get_REDIS_DB
from src.popoto.recipes import question_queue as qq
from src.popoto.recipes.question_queue import QuestionCandidate

AGENT = "qq-agent"
K = qq.QUESTION_BUDGET_TURNS


class QQFact(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class QQCappedFact(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(evidence_cap=3)


class QQPlainFact(popoto.Model):
    """A target with no ConfidenceField (like a JournalEntry side)."""

    name = popoto.UniqueKeyField()


@pytest.fixture(autouse=True)
def _bound_db_is_not_zero(monkeypatch):
    """Refuse to run against DB 0 (the live agent store) and default the kill
    switch to unset."""
    db = get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0)
    assert int(db) != 0, "question-queue tests refuse to run on Redis DB 0"
    monkeypatch.delenv("POPOTO_QUESTION_QUEUE_DISABLE", raising=False)


def _conf(inst):
    return ConfidenceField.get_confidence_data(inst, "certainty")


def _raw_conf(inst):
    field = inst._meta.fields["certainty"]
    return get_REDIS_DB().hget(
        field.get_data_hash_key(inst, "certainty"), inst.db_key.redis_key
    )


def _fact(name, cls=QQFact):
    inst = cls(name=name)
    inst.save()
    return inst


def _disjunction(a, b, turn=0, text=None, agent=AGENT):
    ka, kb = a.db_key.redis_key, b.db_key.redis_key
    return qq.propose(
        agent_id=agent,
        question_text=text or f"Is it {a.name} or {b.name} for the meeting slot?",
        kind="disjunction",
        source_module="test",
        target_keys=[ka, kb],
        options=[
            {"label": a.name, "acted": [ka], "contradicted": [kb]},
            {"label": b.name, "acted": [kb], "contradicted": [ka]},
        ],
        ambiguity_signal="disjunction",
        turn=turn,
    )


def _confirmation(key, turn=0, text="Is the deploy target still staging?"):
    return qq.propose(
        agent_id=AGENT,
        question_text=text,
        kind="confirmation",
        source_module="test",
        target_keys=[key],
        options=[
            {"label": "yes", "acted": [key]},
            {"label": "no", "contradicted": [key]},
        ],
        ambiguity_signal="gate_refusal",
        turn=turn,
    )


def _reload(cand):
    return QuestionCandidate.query.get(redis_key=cand.db_key.redis_key)


# ---------------------------------------------------------------------------
# propose + dedup
# ---------------------------------------------------------------------------


class TestPropose:
    def test_creates_pending_candidate(self):
        a, b = _fact("morning"), _fact("afternoon")
        cand = _disjunction(a, b, turn=3)
        stored = _reload(cand)
        assert stored.status == "pending"
        assert stored.created_turn == 3
        assert stored.expires_turn == 3 + qq.QUESTION_EXPIRY_TURNS
        assert stored.last_seen_turn == 3
        assert stored.ask_count == 0
        assert "morning" in stored.cue_tokens and "meeting" in stored.cue_tokens
        assert stored.options[0] == {
            "label": "morning",
            "acted": [a.db_key.redis_key],
            "contradicted": [b.db_key.redis_key],
        }

    def test_dedup_by_key_intersection_touches_pending(self):
        a, b, c = _fact("a1"), _fact("b1"), _fact("c1")
        first = _disjunction(a, b, turn=1)
        again = _disjunction(a, c, turn=4, text="A totally different phrasing?")
        assert again.candidate_id == first.candidate_id
        assert _reload(first).last_seen_turn == 4
        assert len(list(QuestionCandidate.query.filter(agent_id=AGENT))) == 1

    def test_dedup_by_normalized_text(self):
        k1, k2 = _fact("x1").db_key.redis_key, _fact("x2").db_key.redis_key
        first = _confirmation(k1, turn=0, text="Is staging the target?")
        again = _confirmation(k2, turn=2, text="  is STAGING the target  ")
        assert again.candidate_id == first.candidate_id
        assert _reload(first).last_seen_turn == 2

    def test_answered_within_retention_is_duplicate_expired_is_not(self):
        k = _fact("y1").db_key.redis_key
        first = _confirmation(k, turn=0)
        qq.record_answer(first, "yes", turn=1)
        dup = _confirmation(k, turn=2, text="Different words entirely?")
        assert dup.candidate_id == first.candidate_id
        assert _reload(first).status == "answered"
        late = _confirmation(
            k, turn=2 + qq.QUESTION_RETENTION_TURNS, text="Different words entirely?"
        )
        assert late.candidate_id != first.candidate_id

    def test_different_kind_same_keys_is_not_duplicate(self):
        a, b = _fact("p1"), _fact("p2")
        first = _disjunction(a, b)
        other = _confirmation(a.db_key.redis_key, text="Is p1 still true?")
        assert other.candidate_id != first.candidate_id

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"kind": "nope"},
            {"ambiguity_signal": "judge_abstention"},
            {"question_text": "  ?! "},
            {"options": [{"label": "x", "acted": ["K:1"], "contradicted": ["K:1"]}]},
            {"options": [{"label": "", "acted": []}]},
        ],
    )
    def test_invalid_input_raises(self, kwargs):
        base = dict(
            agent_id=AGENT,
            question_text="valid question here?",
            kind="confirmation",
            source_module="test",
            target_keys=["K:1"],
            ambiguity_signal="gate_refusal",
            turn=0,
        )
        base.update(kwargs)
        with pytest.raises(ValueError):
            qq.propose(**base)


class TestProposeConcurrency:
    def test_concurrent_identical_proposals_create_one_candidate(self):
        k = _fact("cc").db_key.redis_key
        barrier = threading.Barrier(8)
        got = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            cand = _confirmation(k, turn=0)
            with lock:
                got.append(cand)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        stored = list(QuestionCandidate.query.filter(agent_id=AGENT))
        assert len(stored) == 1
        assert {c.candidate_id for c in got} == {stored[0].candidate_id}
        assert get_REDIS_DB().get(qq._propose_lock_key(AGENT)) is None

    def test_busy_propose_lock_fails_closed(self, monkeypatch):
        monkeypatch.setattr(qq, "_PROPOSE_LOCK_ATTEMPTS", 2)
        lock_key = qq._propose_lock_key(AGENT)
        get_REDIS_DB().set(lock_key, "someone-else", px=60_000)
        try:
            assert _confirmation(_fact("busy").db_key.redis_key) is None
            assert list(QuestionCandidate.query.filter(agent_id=AGENT)) == []
            # Another owner's lock is never released by us.
            assert get_REDIS_DB().get(lock_key) in (b"someone-else", "someone-else")
        finally:
            get_REDIS_DB().delete(lock_key)


# ---------------------------------------------------------------------------
# VOI gate + bucket
# ---------------------------------------------------------------------------


class TestVoiGate:
    @pytest.mark.parametrize(
        "signal,last_seen,expected",
        [
            ("disjunction", 10, True),  # ambiguous + recent
            ("disjunction", 10 - qq.QUESTION_RECENT_USE_TURNS - 1, False),  # stale
            ("", 10, False),  # no known ambiguity, recent
            ("", 0, False),  # neither
        ],
    )
    def test_truth_table(self, signal, last_seen, expected):
        cand = QuestionCandidate(ambiguity_signal=signal, last_seen_turn=last_seen)
        assert qq.passes_voi_gate(cand, 10) is expected

    def test_all_enum_signals_pass_uniformly(self):
        for signal in qq.AMBIGUITY_SIGNALS:
            cand = QuestionCandidate(ambiguity_signal=signal, last_seen_turn=5)
            assert qq.passes_voi_gate(cand, 5)

    def test_not_recently_used_is_not_delivered_until_note_use(self):
        k = _fact("g1").db_key.redis_key
        cand = _confirmation(k, turn=0)
        late = qq.QUESTION_RECENT_USE_TURNS + 1
        assert qq.next_question(AGENT, turn=late) is None
        assert qq.note_use(AGENT, [k, "Other:1"], turn=late) == 1
        delivered = qq.next_question(AGENT, turn=late)
        assert delivered.candidate_id == cand.candidate_id
        assert delivered.status == "delivered" and delivered.ask_count == 1
        assert _reload(cand).status == "delivered"


class TestBudget:
    def _seed(self, n, turn=0):
        keys = []
        for i in range(n):
            k = _fact(f"h{i}").db_key.redis_key
            _confirmation(k, turn=turn, text=f"Question number {i} about topic?")
            keys.append(k)
        return keys

    def test_concurrent_hammer_one_delivery_per_k_turns(self):
        keys = self._seed(8)
        deliveries = []
        lock = threading.Lock()
        turns = 3 * K
        for turn in range(turns):
            qq.note_use(AGENT, keys, turn)
            barrier = threading.Barrier(8)

            def worker(t=turn):
                barrier.wait()
                got = qq.next_question(AGENT, turn=t)
                if got is not None:
                    with lock:
                        deliveries.append((t, got.candidate_id))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        assert [t for t, _ in deliveries] == list(range(0, turns, K))
        assert len({cid for _, cid in deliveries}) == len(deliveries)

    def test_regressed_turn_no_grant_and_never_rewinds(self):
        keys = self._seed(3, turn=10)
        assert qq.next_question(AGENT, turn=10) is not None
        bucket = qq._bucket_key(AGENT)
        assert get_REDIS_DB().get(bucket) in (b"10", "10")
        assert get_REDIS_DB().ttl(bucket) > 0
        assert qq.next_question(AGENT, turn=2) is None  # regressed
        assert get_REDIS_DB().get(bucket) in (b"10", "10")
        assert qq.next_question(AGENT, turn=10 + K - 1) is None
        qq.note_use(AGENT, keys, 10 + K)
        assert qq.next_question(AGENT, turn=10 + K) is not None

    def test_budget_exhausted_returns_none_without_reset(self):
        self._seed(3)
        assert qq.next_question(AGENT, turn=0) is not None
        before = get_REDIS_DB().get(qq._bucket_key(AGENT))
        for t in range(1, K):
            assert qq.next_question(AGENT, turn=t) is None
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) == before

    def test_nothing_eligible_does_not_spend_budget(self):
        assert qq.next_question(AGENT, turn=0) is None
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) is None

    @pytest.mark.parametrize("race", ["expired", "deflected"])
    def test_candidate_lost_after_read_does_not_spend_budget(self, monkeypatch, race):
        """Another worker expires (or a reply cools) the candidate between the
        read and the delivery script: nothing is delivered, budget unspent."""
        k = _fact("race").db_key.redis_key
        _confirmation(k, turn=0)
        real = qq._expire_stale_in

        def racing(candidates, turn):
            n, survivors = real(candidates, turn)
            for cand in survivors:
                fresh = _reload(cand)
                if race == "expired":
                    qq._expire(fresh, turn)
                else:
                    assert qq.record_answer(fresh, "not sure", turn).reason == "cooled"
            return n, survivors

        monkeypatch.setattr(qq, "_expire_stale_in", racing)
        assert qq.next_question(AGENT, turn=0) is None
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) is None

    def test_delivers_next_candidate_when_first_is_lost(self, monkeypatch):
        keys = self._seed(2)
        real = qq._expire_stale_in
        lost = []

        def racing(candidates, turn):
            n, survivors = real(candidates, turn)
            first = sorted(survivors, key=qq._impact_order)[0]
            qq._expire(_reload(first), turn)
            lost.append(first.candidate_id)
            return n, survivors

        monkeypatch.setattr(qq, "_expire_stale_in", racing)
        qq.note_use(AGENT, keys, 0)
        got = qq.next_question(AGENT, turn=0)
        assert got is not None and got.candidate_id != lost[0]
        stored = _reload(got)
        assert (stored.status, stored.ask_count, stored.delivered_turn) == (
            "delivered",
            1,
            0,
        )
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) in (b"0", "0")

    def test_delivery_script_error_fails_closed(self, monkeypatch):
        """Candidates read fine; only the delivery Lua raises."""
        cand = _confirmation(_fact("lua").db_key.redis_key, turn=0)

        def broken_run_lua(*args, **kwargs):
            raise redis.ConnectionError("down")

        monkeypatch.setattr(qq, "run_lua", broken_run_lua)
        assert qq.next_question(AGENT, turn=0) is None
        assert _reload(cand).status == "pending"
        assert get_REDIS_DB().get(qq._bucket_key(AGENT)) is None


# ---------------------------------------------------------------------------
# Expiry, staleness seam, timing, cooldown
# ---------------------------------------------------------------------------


class TestExpiryAndTiming:
    def test_expired_not_delivered_and_retained(self):
        k = _fact("e1").db_key.redis_key
        cand = _confirmation(k, turn=0)
        late = qq.QUESTION_EXPIRY_TURNS + 1
        qq.note_use(AGENT, [k], late)  # recently used, but past expiry
        assert qq.next_question(AGENT, turn=late) is None
        stored = _reload(cand)
        assert stored is not None and stored.status == "expired"
        assert stored.resolved_turn == late

    def test_registered_staleness_check_expires_instead_of_asking(self):
        a, b = _fact("s1"), _fact("s2")
        cand = _disjunction(a, b)

        def vanished(c, turn):
            return c.candidate_id == cand.candidate_id

        qq.register_staleness_check("disjunction", vanished)
        try:
            assert qq.next_question(AGENT, turn=1) is None
            assert _reload(cand).status == "expired"
        finally:
            qq.unregister_staleness_check("disjunction", vanished)

    def test_relevance_timing(self):
        k = _fact("r1").db_key.redis_key
        _confirmation(k, text="Is the deploy target still staging?")
        assert qq.next_question(AGENT, turn=1, query_cues="what's for lunch") is None
        got = qq.next_question(AGENT, turn=1, query_cues=["kick off the DEPLOY"])
        assert got is not None
        # query_cues=None disables timing
        k2 = _fact("r2").db_key.redis_key
        _confirmation(k2, turn=K + 1, text="Is the database replica healthy?")
        assert qq.next_question(AGENT, turn=K + 1, query_cues=None) is not None

    def test_cooldown_after_deflection_then_reask(self):
        k = _fact("c1").db_key.redis_key
        _confirmation(k, turn=0)
        q = qq.next_question(AGENT, turn=0)
        res = qq.record_answer(q, "Not sure!", turn=1)
        assert (res.classification, res.reason, res.applied) == (
            "deflected",
            "cooled",
            False,
        )
        stored = _reload(q)
        assert stored.status == "cooled"
        assert stored.cooldown_until == 1 + qq.QUESTION_COOLDOWN_TURNS
        reask_turn = 1 + qq.QUESTION_COOLDOWN_TURNS
        qq.note_use(AGENT, [k], reask_turn - 1)
        assert qq.next_question(AGENT, turn=reask_turn - 1) is None
        again = qq.next_question(AGENT, turn=reask_turn)
        assert again is not None and again.ask_count == 2

    def test_ignored_question_reasked_after_cooldown_subject_to_budget(self):
        """A delivered question that gets no reply is not stuck forever: after
        QUESTION_COOLDOWN_TURNS it is cooled and asked again -- but only when
        the budget allows."""
        C = qq.QUESTION_COOLDOWN_TURNS
        other_turn = C - 2
        assert K <= other_turn < C < other_turn + K < qq.QUESTION_EXPIRY_TURNS
        k = _fact("ign").db_key.redis_key
        first = _confirmation(k, turn=0)
        assert qq.next_question(AGENT, turn=0).candidate_id == first.candidate_id
        for t in range(1, other_turn):
            # Re-observed every turn, but it is waiting for its reply.
            assert _confirmation(k, turn=t).candidate_id == first.candidate_id
            qq.note_use(AGENT, [k], t)
            assert qq.next_question(AGENT, turn=t) is None
        k2 = _fact("ign2").db_key.redis_key
        second = _confirmation(k2, turn=other_turn, text="Is ign2 accurate?")
        assert qq.next_question(AGENT, turn=other_turn).candidate_id == (
            second.candidate_id
        )

        # Ignored for C turns -> cooled, but the budget was just spent.
        qq.note_use(AGENT, [k], C)
        assert qq.next_question(AGENT, turn=C) is None
        stored = _reload(first)
        assert (stored.status, stored.cooldown_until, stored.ask_count) == (
            "cooled",
            C,
            1,
        )

        reask = other_turn + K
        qq.note_use(AGENT, [k], reask)
        again = qq.next_question(AGENT, turn=reask)
        assert again is not None and again.candidate_id == first.candidate_id
        stored = _reload(first)
        assert (stored.status, stored.ask_count, stored.delivered_turn) == (
            "delivered",
            2,
            reask,
        )

    def test_stale_pass_cannot_cool_a_redelivered_question(self):
        """ABA: worker A reads `delivered` (delivered_turn=0); worker B cools
        and re-delivers it at C; A's pass on its stale read must not cool the
        question the host is now showing."""
        C = qq.QUESTION_COOLDOWN_TURNS
        k = _fact("aba").db_key.redis_key
        cand = _confirmation(k, turn=0)
        assert qq.next_question(AGENT, turn=0) is not None
        qq.note_use(AGENT, [k], C)
        stale_view = list(QuestionCandidate.query.filter(agent_id=AGENT))
        got = qq.next_question(AGENT, turn=C)
        assert got is not None and (got.ask_count, got.delivered_turn) == (2, C)

        assert qq._expire_stale_in(stale_view, C) == (0, [])
        stored = _reload(cand)
        assert (stored.status, stored.delivered_turn, stored.ask_count) == (
            "delivered",
            C,
            2,
        )
        assert qq.record_answer(got, "yes", turn=C + 1).reason == "applied"

    def test_legacy_delivered_without_delivered_turn_still_cools(self):
        """A delivered hash written before delivered_turn existed (field
        absent) is guarded as nil and cooled from created_turn."""
        C = qq.QUESTION_COOLDOWN_TURNS
        cand = _confirmation(_fact("legacy").db_key.redis_key, turn=0)
        assert qq.next_question(AGENT, turn=0) is not None
        get_REDIS_DB().hdel(cand.db_key.redis_key, "delivered_turn")
        qq.expire_stale(AGENT, turn=C)
        stored = _reload(cand)
        assert (stored.status, stored.cooldown_until) == ("cooled", C)

    def test_staleness_check_expires_a_delivered_candidate(self):
        a, b = _fact("ds1"), _fact("ds2")
        cand = _disjunction(a, b)
        assert qq.next_question(AGENT, turn=1).candidate_id == cand.candidate_id

        def vanished(c, turn):
            return c.candidate_id == cand.candidate_id

        qq.register_staleness_check("disjunction", vanished)
        try:
            assert qq.expire_stale(AGENT, turn=2) == 1
            assert _reload(cand).status == "expired"
        finally:
            qq.unregister_staleness_check("disjunction", vanished)

    @pytest.mark.parametrize("m5_fails", [False, True])
    def test_live_disjoins_read_once_per_pass(self, monkeypatch, m5_fails):
        facts = [_fact(f"m{i}") for i in range(6)]
        cands = []
        for i in range(3):
            a, b = facts[2 * i], facts[2 * i + 1]
            ka, kb = a.db_key.redis_key, b.db_key.redis_key
            cands.append(
                qq.propose(
                    agent_id=AGENT,
                    question_text=f"Is it {a.name} or {b.name}?",
                    kind="disjunction",
                    source_module="test",
                    target_keys=[ka, kb],
                    options=[
                        {"label": a.name, "acted": [ka], "contradicted": [kb]},
                        {"label": b.name, "acted": [kb], "contradicted": [ka]},
                    ],
                    ambiguity_signal="disjunction",
                    turn=0,
                    disjunction_id=f"dj-{i}",
                )
            )
        calls = []

        def counting(agent_id):
            calls.append(agent_id)
            if m5_fails:
                raise redis.ConnectionError("m5 down")
            return [("dj-0", "x", "y")]

        monkeypatch.setattr(qq, "_live_disjoins", counting)
        got = qq.next_question(AGENT, turn=1)
        assert calls == [AGENT]
        statuses = [_reload(c).status for c in cands]
        if m5_fails:
            assert got is None and statuses == ["pending"] * 3
        else:
            assert got.candidate_id == cands[0].candidate_id
            assert statuses == ["delivered", "expired", "expired"]
        calls.clear()
        qq.expire_stale(AGENT, turn=2)
        assert calls == [AGENT]


# ---------------------------------------------------------------------------
# Answer path
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "reply,expected",
        [
            ("Morning.", ("answered", 0)),
            ("2", ("answered", 1)),
            ("I don't know", ("deflected", None)),
            ("skip", ("deflected", None)),
            ("rather not say", ("deflected", None)),
            ("morning, I think", ("unrecognized", None)),
            ("3", ("unrecognized", None)),
            ("", ("unrecognized", None)),
        ],
    )
    def test_three_way(self, reply, expected):
        opts = [{"label": "morning"}, {"label": "afternoon"}]
        assert qq.classify_answer(opts, reply) == expected


class TestRecordAnswer:
    def test_answered_moves_confidence_with_weight(self):
        a, b = _fact("ans-a"), _fact("ans-b")
        q = _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)
        res = qq.record_answer(q, "ans-a", turn=1)
        assert res.applied and res.option_index == 0
        da, db = _conf(a), _conf(b)
        assert (
            da["confidence"] > 0.5 and da["corroborations"] == qq.QUESTION_ANSWER_WEIGHT
        )
        assert (
            db["confidence"] < 0.5 and db["contradictions"] == qq.QUESTION_ANSWER_WEIGHT
        )
        stored = _reload(q)
        assert stored.status == "answered" and stored.answer_option == 0

    def test_gate_refusal_end_to_end(self):
        k = _fact("gr").db_key.redis_key
        inst = QQFact.query.get(redis_key=k)
        _confirmation(k, turn=0)
        q = qq.next_question(AGENT, turn=0, query_cues="deploy now")
        assert q is not None
        assert qq.record_answer(q, "no", turn=1, instances=[inst]).applied
        assert _conf(inst)["confidence"] < 0.5

    def test_target_without_confidence_field_records_option_only(self):
        a, b = _fact("pa", QQPlainFact), _fact("pb", QQPlainFact)
        before = {
            k: get_REDIS_DB().hgetall(k)
            for k in (a.db_key.redis_key, b.db_key.redis_key)
        }
        _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)
        res = qq.record_answer(q, "1", turn=1)
        assert res.applied and _reload(q).answer_option == 0
        for k, h in before.items():
            assert get_REDIS_DB().hgetall(k) == h

    @pytest.mark.parametrize("cls,saturate", [(QQFact, False), (QQCappedFact, True)])
    def test_defeasible_after_answer(self, cls, saturate):
        inst = _fact(f"def-{cls.__name__}", cls)
        if saturate:
            for _ in range(10):
                ConfidenceField.update_confidence(inst, "certainty", signal=0.9)
            cap = inst._meta.fields["certainty"].evidence_cap
            assert _conf(inst)["evidence_count"] >= cap
        k = inst.db_key.redis_key
        _confirmation(k, turn=0)
        q = qq.next_question(AGENT, turn=0)
        assert qq.record_answer(q, "yes", turn=1).applied
        after_answer = _conf(inst)["confidence"]
        ConfidenceField.update_confidence(
            inst, "certainty", signal=observation.CONTRADICTED_CONFIDENCE_SIGNAL
        )
        assert _conf(inst)["confidence"] < after_answer

    @pytest.mark.parametrize("reply", ["pass", "maybe the first?"])
    def test_deflected_or_unrecognized_bit_identical(self, reply):
        a, b = _fact("bi-a"), _fact("bi-b")
        ConfidenceField.update_confidence(a, "certainty", signal=0.7)
        raw_a, raw_b = _raw_conf(a), _raw_conf(b)
        _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)
        res = qq.record_answer(q, reply, turn=1)
        assert not res.applied and res.reason == "cooled"
        assert _raw_conf(a) == raw_a and _raw_conf(b) == raw_b

    def test_second_record_answer_is_noop_bit_identical(self):
        a, b = _fact("twice-a"), _fact("twice-b")
        _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)
        assert qq.record_answer(q, "twice-a", turn=1).applied
        raw_a, raw_b = _raw_conf(a), _raw_conf(b)
        res = qq.record_answer(_reload(q), "twice-b", turn=2)
        assert (res.applied, res.reason) == (False, "not_open")
        assert _raw_conf(a) == raw_a and _raw_conf(b) == raw_b
        assert _reload(q).answer_option == 0

    def test_fault_after_claim_loses_evidence_keeps_answered(self, monkeypatch):
        a, b = _fact("fault-a"), _fact("fault-b")
        raw_a, raw_b = _raw_conf(a), _raw_conf(b)
        _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)

        def boom(*args, **kwargs):
            raise redis.ConnectionError("injected")

        monkeypatch.setattr(qq, "_apply_option_effects", boom)
        res = qq.record_answer(q, "fault-a", turn=1)
        assert (res.applied, res.reason) == (False, "apply_failed")
        assert _raw_conf(a) == raw_a and _raw_conf(b) == raw_b
        assert _reload(q).status == "answered"

    def test_free_text_never_stored(self):
        k = _fact("ft").db_key.redis_key
        _confirmation(k, turn=0)
        q = qq.next_question(AGENT, turn=0)
        secret = "my password is hunter2 zebra"
        qq.record_answer(q, secret, turn=1)
        raw = get_REDIS_DB().hgetall(q.db_key.redis_key)
        blob = b"".join(raw.keys()) + b"".join(raw.values())
        assert b"hunter2" not in blob and b"zebra" not in blob
        for key in get_REDIS_DB().keys("*"):
            if get_REDIS_DB().type(key) in (b"string", "string"):
                assert b"hunter2" not in (get_REDIS_DB().get(key) or b"")

    def test_valid_outcomes_unchanged(self):
        assert observation.VALID_OUTCOMES == {
            "acted",
            "dismissed",
            "deferred",
            "contradicted",
            "used",
        }

    def test_answer_never_sets_superseded_by(self, monkeypatch):
        seen = []
        real = observation._apply_contradicted

        def spy(instance, pipeline, superseded_by=None):
            seen.append((superseded_by, getattr(instance, "_superseded_by", None)))
            return real(instance, pipeline, superseded_by=superseded_by)

        monkeypatch.setattr(observation, "_apply_contradicted", spy)
        a, b = _fact("sup-a"), _fact("sup-b")
        b._superseded_by = a  # a caller-held attribute must not leak through
        _disjunction(a, b)
        q = qq.next_question(AGENT, turn=0)
        assert qq.record_answer(q, "sup-a", turn=1, instances=[a, b]).applied
        assert seen == [(None, None)]


# ---------------------------------------------------------------------------
# Kill switch, fail closed, prune
# ---------------------------------------------------------------------------


class TestKillSwitchAndFailures:
    def test_kill_switch_disables_propose_and_gating(self, monkeypatch):
        k = _fact("ks").db_key.redis_key
        cand = _confirmation(k, turn=0)
        monkeypatch.setenv("POPOTO_QUESTION_QUEUE_DISABLE", "1")
        assert _confirmation(k, turn=0, text="Another question entirely?") is None
        assert qq.next_question(AGENT, turn=0) is None
        assert qq.record_answer(cand, "yes", turn=1).reason == "disabled"
        monkeypatch.setenv("POPOTO_QUESTION_QUEUE_DISABLE", "0")
        assert qq.next_question(AGENT, turn=0) is not None

    def test_redis_error_fails_closed(self, monkeypatch):
        k = _fact("rf").db_key.redis_key
        cand = _confirmation(k, turn=0)

        class Broken:
            def __getattr__(self, name):
                raise redis.ConnectionError("down")

        monkeypatch.setattr(qq, "get_REDIS_DB", lambda: Broken())

        def broken_candidates(agent_id):
            raise redis.ConnectionError("down")

        monkeypatch.setattr(qq, "_candidates", broken_candidates)
        assert _confirmation(k, turn=1, text="Fresh question here?") is None
        assert qq.next_question(AGENT, turn=1) is None
        res = qq.record_answer(cand, "yes", turn=1)
        assert (res.applied, res.reason) == (False, "error")
        assert qq.note_use(AGENT, [k], 1) == 0
        assert qq.expire_stale(AGENT, 1) == 0
        assert qq.prune(AGENT, 10_000) == 0

    def test_prune_deletes_old_non_pending_only(self):
        k1, k2 = _fact("pr1").db_key.redis_key, _fact("pr2").db_key.redis_key
        answered = _confirmation(k1, turn=0)
        qq.record_answer(answered, "yes", turn=0)
        pending = _confirmation(k2, turn=0, text="Is pr2 still pending here?")
        now = qq.QUESTION_RETENTION_TURNS
        assert qq.prune(AGENT, now) == 0  # not yet older than retention
        assert qq.prune(AGENT, now + 1) == 1
        assert _reload(answered) is None
        assert _reload(pending).status == "pending"


def test_claim_lua_matches_model_encoding():
    """The CAS compares msgpack bytes; pin that the model writes the same."""
    k = _fact("enc").db_key.redis_key
    cand = _confirmation(k)
    raw = get_REDIS_DB().hget(cand.db_key.redis_key, "status")
    assert raw == msgpack.packb("pending")
    assert qq.next_question(AGENT, turn=3) is not None
    client = get_REDIS_DB()
    assert client.hget(cand.db_key.redis_key, "status") == msgpack.packb("delivered")
    # ask_count is incremented server-side with cmsgpack; it must still be the
    # bytes the model itself would write, and reload as an int.
    assert client.hget(cand.db_key.redis_key, "ask_count") == msgpack.packb(1)
    assert client.hget(cand.db_key.redis_key, "delivered_turn") == msgpack.packb(3)
    assert _reload(cand).ask_count == 1
