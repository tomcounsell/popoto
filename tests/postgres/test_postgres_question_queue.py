"""``[PG-only]`` behaviour of the question queue (#759 M4, plan §2 H).

The parity is ``tests/test_question_queue.py`` on both legs (including its
8-thread hammer) and ``scripts/probe_queue_parity.py``'s seeded two-leg
probe, a slice of which runs here. This file holds the Postgres twins of the
tests that read or plant raw Redis structures (the token bucket, the propose
lock) or inject a fault into the Lua, the deterministic interleavings that
show the delivery is atomic under concurrency -- with controls that show it
is not vacuous -- and the documented ``SKIP LOCKED`` divergence.
"""

import importlib.util
import threading
from pathlib import Path

import pytest

import popoto
from popoto.backends import get_backend
from popoto.fields import observation
from popoto.fields.confidence_field import ConfidenceField
from popoto.recipes import question_queue as qq
from popoto.recipes.question_queue import QuestionCandidate

AGENT = "pgqq"
K = qq.QUESTION_BUDGET_TURNS


class PgqqFact(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.delenv("POPOTO_QUESTION_QUEUE_DISABLE", raising=False)


def _fact(name):
    inst = PgqqFact(name=name)
    inst.save()
    return inst


def _ask(key, turn=0, text="Is the deploy target still staging?", agent=AGENT):
    return qq.propose(
        agent_id=agent,
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


def _bucket(pg, agent=AGENT):
    table = pg._engine("popoto_question_bucket")
    rows, _ = pg._run(
        f"SELECT last_turn, expires_at - extract(epoch from clock_timestamp()) "
        f"FROM {table} WHERE agent = %s",
        [agent],
    )
    return None if not rows else (int(rows[0][0]), float(rows[0][1]))


def _lease(pg, key):
    table = pg._engine("popoto_lease")
    rows, _ = pg._run(f"SELECT token FROM {table} WHERE key = %s", [key])
    return rows[0][0] if rows else None


# -- twins of the raw-Redis tests -------------------------------------------------------


def test_a_held_propose_lease_fails_closed(pg, monkeypatch):
    monkeypatch.setattr(qq, "_PROPOSE_LOCK_ATTEMPTS", 2)
    key = qq._propose_lock_key(AGENT)
    assert pg.field_call(
        QuestionCandidate._meta.spec, "_qq", "lock", key, "someone-else", 60_000
    )
    assert _ask(_fact("busy").db_key.redis_key) is None
    assert _lease(pg, key) == "someone-else"
    assert pg.field_call(
        QuestionCandidate._meta.spec, "_qq", "release", key, "someone-else"
    )
    assert _ask(_fact("free").db_key.redis_key) is not None
    assert _lease(pg, key) is None  # released by its owner


def test_an_expired_propose_lease_is_taken(pg):
    """``SET NX PX``: a lease past its expiry is absent."""
    key = qq._propose_lock_key(AGENT)
    spec = QuestionCandidate._meta.spec
    assert pg.field_call(spec, "_qq", "lock", key, "old", 1)
    threading.Event().wait(0.01)
    assert pg.field_call(spec, "_qq", "lock", key, "new", 60_000)
    assert not pg.field_call(spec, "_qq", "lock", key, "third", 60_000)
    assert pg.field_call(spec, "_qq", "release", key, "old") == 0
    assert _lease(pg, key) == "new"


def test_a_regressed_turn_never_rewinds_the_bucket(pg):
    keys = [_fact(f"h{i}").db_key.redis_key for i in range(3)]
    for i, k in enumerate(keys):
        _ask(k, turn=10, text=f"Question number {i} about topic?")
    assert qq.next_question(AGENT, turn=10) is not None
    last, ttl = _bucket(pg)
    assert last == 10 and ttl > 0
    assert qq.next_question(AGENT, turn=2) is None
    assert _bucket(pg)[0] == 10
    assert qq.next_question(AGENT, turn=10 + K - 1) is None
    qq.note_use(AGENT, keys, 10 + K)
    assert qq.next_question(AGENT, turn=10 + K) is not None
    assert _bucket(pg)[0] == 10 + K


def test_an_expired_bucket_grants_again(pg):
    """The bucket's wall-clock TTL backstop: past ``expires_at`` the row
    reads as absent, as an expired Redis key does."""
    keys = [_fact(f"e{i}").db_key.redis_key for i in range(2)]
    for i, k in enumerate(keys):
        _ask(k, turn=0, text=f"Expiry question {i} here?")
    assert qq.next_question(AGENT, turn=0) is not None
    assert qq.next_question(AGENT, turn=1) is None
    table = pg._engine("popoto_question_bucket")
    pg._run(f"UPDATE {table} SET expires_at = 0")
    qq.note_use(AGENT, keys, 1)
    assert qq.next_question(AGENT, turn=1) is not None


def test_nothing_eligible_spends_no_budget(pg):
    assert qq.next_question(AGENT, turn=0) is None
    assert _bucket(pg) is None


def test_the_next_candidate_is_delivered_when_the_first_is_lost(pg, monkeypatch):
    keys = [_fact(f"l{i}").db_key.redis_key for i in range(2)]
    for i, k in enumerate(keys):
        _ask(k, turn=0, text=f"Lost question {i} here?")
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
    assert _bucket(pg)[0] == 0


def test_a_failing_delivery_fails_closed(pg, monkeypatch):
    cand = _ask(_fact("lua").db_key.redis_key)
    real = pg.field_call

    def broken(spec, field, op, *args, **kwargs):
        if field == "_qq" and op == "deliver":
            raise RuntimeError("down")
        return real(spec, field, op, *args, **kwargs)

    monkeypatch.setattr(pg, "field_call", broken)
    assert qq.next_question(AGENT, turn=0) is None
    assert _reload(cand).status == "pending"
    assert _bucket(pg) is None


def test_a_backend_error_fails_closed(pg, monkeypatch):
    k = _fact("rf").db_key.redis_key
    cand = _ask(k)

    def broken(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(pg, "field_call", broken)

    def broken_candidates(agent_id):
        raise RuntimeError("down")

    monkeypatch.setattr(qq, "_candidates", broken_candidates)
    assert _ask(k, turn=1, text="Fresh question here?") is None
    assert qq.next_question(AGENT, turn=1) is None
    res = qq.record_answer(cand, "yes", turn=1)
    assert (res.applied, res.reason) == (False, "error")
    assert qq.note_use(AGENT, [k], 1) == 0
    assert qq.expire_stale(AGENT, 1) == 0


def test_an_answer_is_evidence_not_supersession(pg, monkeypatch):
    """The Postgres twin of ``test_answer_never_sets_superseded_by``: the
    answer reaches the targets as ``acted`` / ``contradicted`` outcomes
    through ``ObservationProtocol``'s backend transaction, with no
    ``superseded_by``; both facts survive with their confidence moved."""
    seen = []
    real = observation._on_context_used_on_backend

    def spy(instances, outcome_map, pipeline=None):
        seen.append(dict(outcome_map))
        return real(instances, outcome_map, pipeline)

    monkeypatch.setattr(observation, "_on_context_used_on_backend", spy)
    a, b = _fact("sup-a"), _fact("sup-b")
    b._superseded_by = a
    ka, kb = a.db_key.redis_key, b.db_key.redis_key
    qq.propose(
        agent_id=AGENT,
        question_text="Is it sup-a or sup-b for the meeting slot?",
        kind="disjunction",
        source_module="test",
        target_keys=[ka, kb],
        options=[
            {"label": "sup-a", "acted": [ka], "contradicted": [kb]},
            {"label": "sup-b", "acted": [kb], "contradicted": [ka]},
        ],
        ambiguity_signal="disjunction",
        turn=0,
    )
    q = qq.next_question(AGENT, turn=0)
    assert qq.record_answer(q, "sup-a", turn=1, instances=[a, b]).applied
    assert seen and seen[0] == {ka: "acted", kb: "contradicted"}
    fa = ConfidenceField.get_confidence(PgqqFact.query.get(name="sup-a"), "certainty")
    fb = ConfidenceField.get_confidence(PgqqFact.query.get(name="sup-b"), "certainty")
    assert fa > 0.5 > fb


# -- the delivery under concurrency -----------------------------------------------------


def _interleaved_deliveries(pg, monkeypatch, lock):
    """Two candidates; one delivery runs inside an open transaction, a second
    for the same agent and turn starts from another thread, and only then
    does the first commit. Returns the deliveries and whether the second
    waited."""
    keys = [_fact(f"i{i}").db_key.redis_key for i in range(2)]
    cands = [
        _ask(k, turn=0, text=f"Interleaved question {i}?") for i, k in enumerate(keys)
    ]
    ordered = [c.db_key.redis_key for c in cands]
    spec = QuestionCandidate._meta.spec
    args = (AGENT, ordered, 0, K, qq.QUESTION_BUCKET_TTL_SECONDS, ("cooled", "pending"))
    if not lock:
        real_run = pg._run

        def unlocked(sql, params=(), **kw):
            if "popoto:qq:" in str(params[:1]):
                sql = sql.replace(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)); ", ""
                )
                params = list(params)[1:]
            return real_run(sql, params, **kw)

        monkeypatch.setattr(pg, "_run", unlocked)
    out = []
    finished = threading.Event()

    def second():
        out.append(pg.field_call(spec, "_qq", "deliver", *args))
        finished.set()

    with pg.transaction() as uow:
        out.append(pg.field_call(spec, "_qq", "deliver", *args, uow=uow))
        thread = threading.Thread(target=second)
        thread.start()
        waited = not finished.wait(0.5)
    thread.join()
    return out, waited


def test_two_deliveries_in_one_turn_serialize_on_the_bucket(pg, monkeypatch):
    """The second delivery queues on the agent's bucket lock until the first
    commits, then reads its bucket row and refuses: one ask per turn."""
    out, waited = _interleaved_deliveries(pg, monkeypatch, lock=True)
    assert waited
    assert out[0] == (1, 1) and out[1] is None


def test_without_the_bucket_lock_two_deliveries_grant(pg, monkeypatch):
    """The control: with the bucket lock removed, the second delivery's
    snapshot has no bucket row, it skips the first's locked candidate and
    claims the other -- two asks in one turn -- so the test above is not
    vacuous."""
    out, _waited = _interleaved_deliveries(pg, monkeypatch, lock=False)
    assert out[0] == (1, 1) and out[1] == (2, 1)


def test_a_candidate_another_writer_holds_is_skipped(pg):
    """The documented ``SKIP LOCKED`` divergence: a candidate whose row a
    concurrent transaction holds is passed over for the next one (on Redis
    the script would wait its turn and see it)."""
    keys = [_fact(f"s{i}").db_key.redis_key for i in range(2)]
    cands = [_ask(k, turn=0, text=f"Skipped question {i}?") for i, k in enumerate(keys)]
    first = cands[0]
    spec = QuestionCandidate._meta.spec
    ts = pg._table(spec)
    got = []
    with pg.transaction() as uow:
        pg._run(
            f'SELECT 1 FROM {ts.qualified} WHERE "_pk" = %s FOR UPDATE',
            [first.db_key.redis_key],
            uow=uow,
        )
        thread = threading.Thread(
            target=lambda: got.append(
                pg.field_call(
                    spec,
                    "_qq",
                    "deliver",
                    AGENT,
                    [c.db_key.redis_key for c in cands],
                    0,
                    K,
                    qq.QUESTION_BUCKET_TTL_SECONDS,
                    ("cooled", "pending"),
                )
            )
        )
        thread.start()
        thread.join(5)
    assert got == [(2, 1)]


def test_the_hammer_across_agents(pg):
    """8 threads × 3K turns over two agents: each agent gets exactly one
    delivery per K turns, and no candidate is delivered twice."""
    agents = ["ha", "hb"]
    keys = {a: [_fact(f"{a}{i}").db_key.redis_key for i in range(6)] for a in agents}
    for a in agents:
        for i, k in enumerate(keys[a]):
            _ask(k, turn=0, text=f"Hammer {a} question {i} topic?", agent=a)
    deliveries = []
    lock = threading.Lock()
    for turn in range(3 * K):
        for a in agents:
            qq.note_use(a, keys[a], turn)
        barrier = threading.Barrier(8)

        def worker(a, t=turn):
            barrier.wait()
            got = qq.next_question(a, turn=t)
            if got is not None:
                with lock:
                    deliveries.append((a, t, got.candidate_id))

        threads = [
            threading.Thread(target=worker, args=(agents[i % 2],)) for i in range(8)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    for a in agents:
        turns = [t for ag, t, _ in deliveries if ag == a]
        assert sorted(turns) == list(range(0, 3 * K, K))
    assert len({cid for _, _, cid in deliveries}) == len(deliveries)


# -- the seeded probe, CI-sized ---------------------------------------------------------

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe_queue_parity.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_queue_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_queue_probe_finds_no_undocumented_mismatch(pg):
    probe = _load_probe().run(pg, seeds=[759], shapes=20)
    assert probe.shapes == 20
    assert sum(probe.checks.values()) > 300
    assert not probe.mismatches, probe.report()


def test_a_postgres_bound_queue_issues_no_redis_command(pg, monkeypatch):
    import redis.connection

    calls = []

    def refuse(conn_self, *args, **kwargs):
        calls.append(args[:1])
        raise RuntimeError("a Redis command was issued")

    monkeypatch.setattr(redis.connection.Connection, "send_packed_command", refuse)
    monkeypatch.setattr(redis.connection.Connection, "send_command", refuse)
    monkeypatch.setattr(redis.connection.ConnectionPool, "get_connection", refuse)
    k = _fact("nr").db_key.redis_key
    cand = _ask(k, turn=0)
    assert cand is not None
    q = qq.next_question(AGENT, turn=0)
    assert q is not None
    assert qq.record_answer(q, "yes", turn=1).applied
    assert qq.expire_stale(AGENT, 2) == 0
    assert qq.prune(AGENT, 10_000) == 1
    assert calls == []
    assert get_backend(QuestionCandidate).name == "postgres"
