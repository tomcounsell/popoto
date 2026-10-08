"""``[PG-only]`` behaviour of the decision log on Postgres (#811).

The model-level parity is the conformance suite: ``tests/test_auditable_extraction.py``
and ``tests/test_reference_resolution.py`` run on both legs. This file holds
what has no Redis leg: the Postgres twins of the tests that read raw Redis
structures (each named in its twin's ``redis_only`` reason), the lease
expiry, the concurrency of the guard and the claim across real threads, the
``turn_summary`` aggregate, the outage path, and a whole
``SubconsciousMemory`` extraction run with the Redis connection layer
refusing every command.
"""

import threading
import time

import pytest

from popoto.backends import BackendUnavailableError, get_backend
from popoto.extraction.candidates import Candidate
from popoto.extraction.decision_log import (
    AuditableExtractionConfig,
    DecisionLog,
    DecisionRecord,
)
from popoto.extraction.resolution import Resolution, ResolutionStatus
from popoto.extraction.resolution_log import ResolutionLog
from popoto.extraction.verdict import ReasonCode, Verdict, VerdictResult
from popoto.fields.constants import Defaults
from popoto.recipes.provenance_journal import JournalEntry, ProvenanceJournal
from popoto.recipes.subconscious_memory import SubconsciousMemory
from tests.postgres.test_postgres_recipes import _RedisRecorder


def _candidate(ordinal=0, turn="t-pg", text="Alice deployed the service."):
    return Candidate(
        text=text,
        turn_id=turn,
        candidate_id=f"{turn}:sentence:{ordinal}",
        start=0,
        end=len(text),
        generator_rule="sentence",
    )


class _AcceptAll:
    def __call__(self, candidate):
        return VerdictResult(
            candidate.candidate_id, Verdict.ACCEPT, ReasonCode.ACCEPTED
        )


class _StubResolution:
    def __init__(self, resolution):
        self.resolution = resolution

    def __call__(self, candidate, turn_text, context):
        return self.resolution


def _lease_table(pg):
    return pg._engine("popoto_lease")


# -- the schema -----------------------------------------------------------------


def test_the_composite_key_is_a_unique_index(pg):
    """The Redis path's single row per candidate is a unique constraint here:
    a second row for one composite key cannot exist, however it is written."""
    log = DecisionLog()
    candidate = _candidate()
    log.write_pending("agent", candidate)
    ts = pg._table(DecisionRecord._meta.spec)
    rows, _ = pg._run(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
        [pg.schema, ts.table],
    )
    assert any("UNIQUE" in row[0].upper() for row in rows)
    log.write_terminal("agent", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT)
    log.write_terminal("agent", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT)
    assert len(list(DecisionRecord.query.filter(agent_id="agent"))) == 1


# -- the claim ------------------------------------------------------------------


def test_claim_carries_a_finite_ttl(pg):
    """Twin of ``TestAssemblyWiring::test_claim_carries_a_finite_ttl``: the
    lease row's ``expires_at`` is the claim TTL from now, and a lease that
    has expired is takeable again."""
    log = DecisionLog()
    args = ("agent-ttl", "t-90", "t-90:sentence:0")
    token = log.acquire_claim(*args)
    assert token is not None
    key = DecisionLog.claim_key(*args)
    table = _lease_table(pg)
    rows, _ = pg._run(f"SELECT expires_at FROM {table} WHERE key = %s", [key])
    remaining = rows[0][0] - time.time()
    assert 0 < remaining <= Defaults.M3_ASSEMBLY_CLAIM_TTL_MS / 1000.0 + 1

    assert log.acquire_claim(*args) is None, "a live lease admits no second runner"
    pg._run(
        f"UPDATE {table} SET expires_at = expires_at - 3600 WHERE key = %s",
        [key],
        write=True,
    )
    second = log.acquire_claim(*args)
    assert second is not None and second != token
    assert log.release_claim(*args, token) is False, "the old token no longer owns it"
    assert log.release_claim(*args, second) is True


def test_racing_claims_admit_exactly_one_runner(pg):
    log = DecisionLog()
    args = ("agent-race", "t-91", "t-91:sentence:0")
    barrier = threading.Barrier(8)
    won = []

    def run():
        barrier.wait()
        won.append(log.acquire_claim(*args))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([t for t in won if t is not None]) == 1


# -- the guard ------------------------------------------------------------------


def test_racing_terminal_writes_never_overwrite_an_assembled_accept(pg):
    """Eight threads race an accept-with-entry against rejects on one
    candidate. Whatever the order, the assembled accept is never replaced and
    the summary agrees with the one row."""
    log = DecisionLog()
    candidate = _candidate(turn="t-guard")
    log.write_pending("agent-guard", candidate)
    barrier = threading.Barrier(8)

    def accept():
        barrier.wait()
        log.write_terminal(
            "agent-guard",
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id="e-1",
        )

    def reject():
        barrier.wait()
        log.write_terminal(
            "agent-guard", candidate, Verdict.REJECT, ReasonCode.ASSEMBLY_FAILED
        )

    threads = [threading.Thread(target=accept)] + [
        threading.Thread(target=reject) for _ in range(7)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # A reject may land first; the accept then replaces it. Once the accept
    # has landed, every later reject is refused. Close the race and assert the
    # invariant: after one more reject, an accept-with-entry row is still there.
    log.write_terminal(
        "agent-guard", candidate, Verdict.ACCEPT, ReasonCode.ACCEPTED, entry_id="e-1"
    )
    assert (
        log.write_terminal(
            "agent-guard", candidate, Verdict.REJECT, ReasonCode.ASSEMBLY_FAILED
        )
        is False
    )
    row = log.get("agent-guard", "t-guard", candidate.candidate_id)
    assert row.state == "accept" and row.entry_id == "e-1"
    assert len(list(DecisionRecord.query.filter(agent_id="agent-guard"))) == 1
    assert log.turn_summary("agent-guard", "t-guard") == {
        "state:accept": 1,
        "reason:accepted": 1,
    }


# -- the summary ----------------------------------------------------------------


def test_a_null_reason_folds_to_reason_colon(pg):
    """A row whose reason code is NULL counts under ``reason:``, as Redis
    counts an empty reason."""
    log = DecisionLog()
    candidate = _candidate(turn="t-null")
    log.write_terminal("agent-null", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT)
    ts = pg._table(DecisionRecord._meta.spec)
    pg._run(
        f'UPDATE {ts.qualified} SET "reason_code" = NULL WHERE "agent_id" = %s',
        ["agent-null"],
        write=True,
    )
    assert log.turn_summary("agent-null", "t-null") == {
        "state:reject": 1,
        "reason:": 1,
    }


def test_rebuild_turn_summary_is_turn_summary(pg):
    log = DecisionLog()
    for ordinal, (state, reason) in enumerate(
        [
            (Verdict.REJECT, ReasonCode.NOT_A_FACT),
            (Verdict.FIREWALL_DROP, ReasonCode.PRE_LLM_CANDIDATE_BLOCK),
        ]
    ):
        log.write_terminal("agent-rb", _candidate(ordinal, turn="t-rb"), state, reason)
    assert log.rebuild_turn_summary("agent-rb", "t-rb") == log.turn_summary(
        "agent-rb", "t-rb"
    )
    assert log.turn_summary("agent-rb", "t-rb")["state:reject"] == 1
    assert log.turn_summary("agent-rb", "t-nobody") == {}


def test_metrics_are_identical_with_the_journal_table_empty(pg):
    """Twin of the Redis keyspace-absent test: metrics read the log alone."""
    log = DecisionLog()
    gold = {}
    for ordinal in range(3):
        candidate = _candidate(ordinal, turn="t-met")
        if ordinal == 0:
            log.write_terminal(
                "agent-met",
                candidate,
                Verdict.ACCEPT,
                ReasonCode.ACCEPTED,
                entry_id="e-0",
            )
            gold[candidate.candidate_id] = True
        else:
            log.write_terminal(
                "agent-met", candidate, Verdict.REJECT, ReasonCode.NOT_A_FACT
            )
            gold[candidate.candidate_id] = False
    before = log.compute_metrics("agent-met", gold)
    ts = pg._table(JournalEntry._meta.spec)
    pg._run(f"DELETE FROM {ts.qualified}", write=True)
    after = log.compute_metrics("agent-met", gold)
    assert (after.precision, after.recall, after.f1) == (
        before.precision,
        before.recall,
        before.f1,
    )
    assert after.per_reason_code == before.per_reason_code


# -- zero Redis -----------------------------------------------------------------


def test_an_auditable_extraction_runs_with_zero_redis_commands(pg, monkeypatch):
    """The whole path -- construct, candidates, claim, pending, journal
    append, terminal write, resolution sidecar, summary, metrics -- on a
    Postgres-bound process while the Redis connection layer refuses every
    command."""
    monkeypatch.setattr(Defaults, "M4_RESOLUTION_ENABLED", True)
    recorder = _RedisRecorder(monkeypatch)
    resolution = Resolution(
        statement="Alice deployed the service.",
        verbatim="Alice deployed the service.",
        status=ResolutionStatus.RESOLVED,
    )
    memory = SubconsciousMemory(
        agent_id="agent-zero",
        auditable_extraction=AuditableExtractionConfig(
            verdict_provider=_AcceptAll(),
            journal=ProvenanceJournal,
            resolution_provider=_StubResolution(resolution),
        ),
    )
    facts = memory.extract_memories(
        "Alice deployed the service. Bob reviewed the change.", turn_id="t-zero"
    )
    assert facts
    log = DecisionLog()
    rows = log.list_for_agent("agent-zero")
    assert rows and all(row.is_terminal for row in rows)
    assert not log.list_pending("agent-zero")
    assert log.turn_summary("agent-zero", "t-zero")["state:accept"] == len(facts)
    assert ResolutionLog().get("agent-zero", "t-zero", facts[0].candidate_id)
    assert len(list(JournalEntry.query.filter(turn_id="t-zero"))) == len(facts)
    log.compute_metrics("agent-zero", {})
    assert recorder.calls == []


class _Mixed:
    """Verdict by marker word, so one turn exercises every terminal state."""

    def __call__(self, candidate):
        text = candidate.text.lower()
        if "secret" in text:
            return VerdictResult(
                candidate.candidate_id,
                Verdict.FIREWALL_DROP,
                ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
            )
        if "rejected" in text:
            return VerdictResult(
                candidate.candidate_id, Verdict.REJECT, ReasonCode.NOT_A_FACT
            )
        if "withheld" in text:
            return VerdictResult(
                candidate.candidate_id, Verdict.WITHHOLD, ReasonCode.LOW_CONFIDENCE
            )
        return VerdictResult(
            candidate.candidate_id, Verdict.ACCEPT, ReasonCode.ACCEPTED
        )


def test_every_decision_log_path_runs_with_zero_redis_commands(pg, monkeypatch):
    """The full sequence under one recorder: empty turn, firewall drop,
    reject, withhold, accept, duplicate assembly (the same turn re-run),
    claim contention (a pre-held claim), list_pending, turn_summary and
    compute_metrics. Not one Redis command."""
    recorder = _RedisRecorder(monkeypatch)
    memory = SubconsciousMemory(
        agent_id="agent-seq",
        auditable_extraction=AuditableExtractionConfig(
            verdict_provider=_Mixed(), journal=ProvenanceJournal
        ),
    )
    log = DecisionLog()

    # empty turn
    assert memory.extract_memories("   ", turn_id="t-empty") == []
    assert log.turn_summary("agent-seq", "t-empty")

    # firewall drop, reject, withhold, accept in one turn
    text = (
        "The secret sentence is here. Bob rejected the change. "
        "Carol withheld the review. Alice deployed the service."
    )
    first = memory.extract_memories(text, turn_id="t-mixed")
    assert len(first) == 1
    summary = log.turn_summary("agent-seq", "t-mixed")
    for state in ("firewall_drop", "reject", "withhold", "accept"):
        assert summary[f"state:{state}"] == 1, summary
    assert memory._last_extraction_privacy_dropped is False

    # duplicate assembly: the same turn again adds no second journal entry
    memory.extract_memories(text, turn_id="t-mixed")
    assert len(list(JournalEntry.query.filter(turn_id="t-mixed"))) == 1

    # claim contention: a pre-held claim leaves the candidate unassembled
    held = log.acquire_claim("agent-seq", "t-held", "t-held:sentence:0")
    assert held is not None
    assert memory.extract_memories("Dave shipped it.", turn_id="t-held") == []
    assert log.release_claim("agent-seq", "t-held", "t-held:sentence:0", held)

    assert log.list_pending("agent-seq") is not None
    log.list_for_agent("agent-seq")
    log.turn_summary("agent-seq", "t-held")
    log.compute_metrics("agent-seq", {})
    assert recorder.calls == []


# -- outage ---------------------------------------------------------------------


def test_an_outage_propagates_from_a_terminal_write(pg, monkeypatch):
    """The decision log has no Redis-style swallow: a Postgres outage raises
    out of the write rather than being recorded as a refused write."""
    log = DecisionLog()

    def down(*args, **kwargs):
        raise BackendUnavailableError("postgres is down")

    monkeypatch.setattr(get_backend(DecisionRecord), "_run", down)
    with pytest.raises((BackendUnavailableError, ConnectionError)):
        log.write_terminal(
            "agent-out", _candidate(), Verdict.REJECT, ReasonCode.NOT_A_FACT
        )
