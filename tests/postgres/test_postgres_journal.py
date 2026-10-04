"""``[PG-only]`` behaviour of the provenance journal and the reconciler
(#759 M4; ``recipes/provenance_journal.py``, ``recipes/reconciliation.py``).

The parity is ``tests/test_provenance_journal.py``,
``tests/test_reconciliation_m5.py`` and ``tests/test_view_resolver.py`` on
both legs. This file holds the Postgres twins of the tests there that read
raw Redis structures or pin ``MULTI``/``EXEC`` behaviour (each named in its
twin's ``redis_only`` reason): on Postgres the annotate-and-close is one
transaction that commits or rolls back whole and reports the close at the
call, the append-only guard reads the row, ``hard_delete`` clears the chain
columns that name the erased record, and the reconciler's vector cache is a
table beside the entries.
"""

import pytest

import popoto
from popoto.backends import get_backend
from popoto.exceptions import AppendOnlyViolation
from popoto.fields.constants import Defaults
from popoto.fields.validity_field import ValidityField, ValidityMemberAbsentError
from popoto.recipes import provenance_journal as journal_module
from popoto.recipes.provenance_journal import (
    VALIDITY_FIELD_NAME,
    JournalEntry,
    ProvenanceJournal,
)
from popoto.recipes.reconciliation import (
    ClaimClass,
    ClaimMembership,
    erase_entry,
    reconcile_entry,
    shortlist_candidates,
)
from tests.test_reconciliation_m5 import FakeProvider, ScriptedJudge, always

AGENT = "pg-journal"
F = VALIDITY_FIELD_NAME


@pytest.fixture(autouse=True)
def _coupling():
    coupling = Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED
    yield
    Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = coupling
    journal_module._UNCOUPLED_WARNED.clear()


def _append(**kwargs):
    kwargs.setdefault("agent_id", AGENT)
    kwargs.setdefault("statement", "the launch slipped to the 30th")
    return ProvenanceJournal.append(**kwargs).entry


def _row(pg, model, key):
    ts = pg._table(model._meta.spec)
    rows, _ = pg._run(
        f'SELECT "{F}__valid_from", "{F}__invalid_at", "{F}__supersedes", '
        f'"{F}__superseded_by" FROM {ts.qualified} WHERE "_pk" = %s',
        [key],
    )
    return rows[0] if rows else None


def _count(pg, model):
    ts = pg._table(model._meta.spec)
    rows, _ = pg._run(f"SELECT count(*) FROM {ts.qualified}")
    return int(rows[0][0])


# -- the journal -------------------------------------------------------------------------


def test_the_append_only_contract_holds_on_postgres(pg):
    entry = _append()
    key = entry.db_key.redis_key
    assert _row(pg, JournalEntry, key) is not None
    with pytest.raises(AppendOnlyViolation):
        entry.delete()
    with pytest.raises(AppendOnlyViolation):
        JournalEntry.delete_all()
    with pytest.raises(AppendOnlyViolation):
        entry.save(migrate_key=True)
    entry.agent_id = "someone-else"
    with pytest.raises(popoto.exceptions.KeyMutationError):
        entry.save()
    with pytest.raises(AppendOnlyViolation, match="migrate_key"):
        entry.save(migrate_key=True)
    assert _row(pg, JournalEntry, key) is not None
    assert _count(pg, JournalEntry) == 1


def test_a_superseded_entry_is_still_returned_as_of_before_the_close(pg):
    t0 = 1_700_000_000.0
    target = _append(at=t0)
    ProvenanceJournal.supersede(target, agent_id=AGENT, at=t0 + 50.0)
    as_of = {
        r.db_key.redis_key for r in JournalEntry.query.filter(validity__as_of=t0 + 10.0)
    }
    assert target.db_key.redis_key in as_of
    valid_from, invalid_at, _, _ = _row(pg, JournalEntry, target.db_key.redis_key)
    assert (valid_from, invalid_at) == (t0, t0 + 50.0)


def test_a_caller_unit_of_work_carries_the_annotation_and_the_close(pg):
    """The annotate-and-close inside the caller's transaction: the result
    reports the close at the call (on Redis a caller pipeline reports
    ``None``, "unknown until you execute"), the unit is handed back, and a
    rollback discards the annotation *and* the close together."""
    t0 = 1_700_000_000.0
    target = _append(at=t0)
    key = target.db_key.redis_key
    with pytest.raises(RuntimeError, match="roll back"):
        with pg.transaction() as uow:
            result = ProvenanceJournal.supersede(
                target,
                agent_id=AGENT,
                statement="a correction",
                at=t0 + 50.0,
                pipeline=uow,
            )
            assert result.target_closed is True
            assert result.pipeline is uow and result.close_index is None
            raise RuntimeError("roll back")
    assert _row(pg, JournalEntry, key)[1] == float("inf")
    assert ProvenanceJournal.annotations_for(target) == []
    with pg.transaction() as uow:
        result = ProvenanceJournal.supersede(
            target,
            agent_id=AGENT,
            statement="a correction",
            at=t0 + 50.0,
            pipeline=uow,
        )
    assert _row(pg, JournalEntry, key)[1] == t0 + 50.0
    assert [a.db_key.redis_key for a in ProvenanceJournal.annotations_for(target)] == [
        result.entry.db_key.redis_key
    ]
    again = ProvenanceJournal.supersede(target, agent_id=AGENT, at=t0 + 60.0)
    assert again.target_closed is False  # the idempotency guard: one close


def test_only_the_backends_unit_of_work_is_accepted(pg):
    target = _append()
    for bad in (popoto.get_redis().pipeline(), object()):
        with pytest.raises(ValueError, match="unit of work"):
            ProvenanceJournal.supersede(target, agent_id=AGENT, pipeline=bad)
    assert ProvenanceJournal.annotations_for(target) == []
    assert _row(pg, JournalEntry, target.db_key.redis_key)[1] == float("inf")


def test_a_failing_close_writes_no_annotation(pg, monkeypatch):
    """The M3 divergence through the journal: a close that fails after the
    pre-flight (a target gone by the close) raises its typed error at the
    call, and the transaction rolls the annotation back -- on Redis the
    queued annotation is kept."""
    target = _append(at=1_700_000_000.0)
    real = ValidityField.execute_supersede

    def vanished(*args, **kwargs):
        raise ValidityMemberAbsentError("the target is gone (injected)")

    monkeypatch.setattr(ValidityField, "execute_supersede", vanished)
    with pytest.raises(ValidityMemberAbsentError):
        ProvenanceJournal.supersede(target, agent_id=AGENT, at=1_700_000_050.0)
    monkeypatch.setattr(ValidityField, "execute_supersede", real)
    assert _count(pg, JournalEntry) == 1
    assert _row(pg, JournalEntry, target.db_key.redis_key)[1] == float("inf")


def test_an_uncoupled_supersede_closes_nothing(pg):
    t0 = 1_700_000_000.0
    target = _append(at=t0)
    Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False
    result = ProvenanceJournal.supersede(target, agent_id=AGENT, at=t0 + 50.0)
    assert result.target_closed is False and result.coupling_enabled is False
    assert _row(pg, JournalEntry, target.db_key.redis_key)[1] == float("inf")
    assert len(ProvenanceJournal.annotations_for(target)) == 1


def test_hard_delete_erases_the_row_and_the_links_naming_it(pg):
    """``hard_delete`` on Postgres: the row (and its open-claim pointer, by
    cascade) goes, and the chain columns of the records that named it are
    cleared -- the value side of the chain hashes on Redis."""
    t0 = 1_700_000_000.0
    first = _append(at=t0, speaker="tom", subjects=["launch"])
    second = ProvenanceJournal.supersede(
        first, agent_id=AGENT, statement="a correction", at=t0 + 50.0
    ).entry
    k1, k2 = first.db_key.redis_key, second.db_key.redis_key
    assert _row(pg, JournalEntry, k1)[3] == k2
    assert _row(pg, JournalEntry, k2)[2] == k1
    assert JournalEntry.hard_delete(second) is True
    assert _row(pg, JournalEntry, k2) is None
    assert _row(pg, JournalEntry, k1)[3] is None
    assert JournalEntry.hard_delete(first) is True
    assert JournalEntry.query.filter(agent_id=AGENT, speaker="tom") == []
    assert JournalEntry.query.filter(subjects__contains="launch") == []
    assert _count(pg, JournalEntry) == 0


def test_a_refused_entry_model_writes_no_row(pg):
    from tests.test_provenance_journal import (
        PartiallyDeclaredJournal,
        SubclassedJournal,
    )

    target = _append()
    before = _count(pg, JournalEntry)
    for journal in (SubclassedJournal, PartiallyDeclaredJournal):
        with pytest.raises(TypeError):
            journal.append(agent_id=AGENT, statement="refused")
        with pytest.raises(TypeError):
            journal.supersede(target, agent_id=AGENT)
    assert _count(pg, JournalEntry) == before


# -- the reconciler ----------------------------------------------------------------------


def _capture(statement, *, claim_type=None, subject="dana", at=None):
    return ProvenanceJournal.append(
        agent_id="agent-m5",
        statement=statement,
        subjects=[subject] if subject else None,
        claim_type=claim_type,
        captured_at=at,
    ).entry


def _cached(pg, member):
    table = pg._engine("popoto_embedding_cache")
    rows, _ = pg._run(
        f"SELECT vector FROM {table} WHERE model = %s AND member = %s",
        ["JournalEntry", member],
    )
    return rows[0][0] if rows else None


def test_the_reconciler_caches_vectors_in_the_backend(pg):
    provider = FakeProvider()
    judge = ScriptedJudge(always("different"))
    near = _capture("dana prefers mornings", claim_type="preference", at=100.0)
    far = _capture("zzz qqq xxx", claim_type="note", subject="other", at=110.0)
    reconcile_entry(near, client=judge, provider=provider)
    reconcile_entry(far, client=judge, provider=provider)
    probe = _capture("dana prefers morning", claim_type="preference", at=200.0)
    ranked = shortlist_candidates(probe, provider=provider)
    assert ranked[0] == ClaimMembership.query.get(entry_redis_key=near.pk).class_id
    cached = _cached(pg, near.pk)
    assert cached and len(cached) == provider.dimensions
    before = provider.calls
    shortlist_candidates(probe, provider=provider)
    assert provider.calls < before + 3
    assert popoto.get_redis().exists("POPOTO:M5:embedding_cache") == 0


def test_erase_entry_cascades_through_every_leg(pg):
    provider = FakeProvider()
    judge = ScriptedJudge(always("same"))
    first = _capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(first, client=judge, provider=provider)
    second = _capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(second, client=judge, provider=provider)
    shortlist_candidates(second, provider=provider)
    assert _cached(pg, first.pk)
    assert erase_entry(first) is True
    assert JournalEntry.query.get(redis_key=first.pk) is None
    assert ClaimMembership.query.get(entry_redis_key=first.pk) is None
    assert _cached(pg, first.pk) is None
    row = ClaimClass.query.get(class_id=outcome.class_id)
    assert row is not None and row.representative_key != first.pk


def test_reconcile_never_mutates_an_entry_row(pg):
    judge = ScriptedJudge(always("same"))
    first = _capture("dana prefers mornings", claim_type="preference", at=100.0)
    reconcile_entry(first, client=judge)
    ts = pg._table(JournalEntry._meta.spec)
    snapshot = f'SELECT t::text FROM {ts.qualified} AS t WHERE "_pk" = %s'
    before, _ = pg._run(snapshot, [first.pk])
    second = _capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(second, client=judge)
    after, _ = pg._run(snapshot, [first.pk])
    assert before == after


def test_membership_rows_hold_no_claim_content(pg):
    judge = ScriptedJudge(always("same"))
    first = _capture(
        "dana prefers mornings in Berlin", claim_type="preference", at=100.0
    )
    reconcile_entry(first, client=judge)
    for model in (ClaimMembership, ClaimClass):
        ts = pg._table(model._meta.spec)
        rows, _ = pg._run(f"SELECT t::text FROM {ts.qualified} AS t")
        assert rows
        for (text,) in rows:
            for word in ("Berlin", "mornings", "preference"):
                assert word not in text
    _ = get_backend(JournalEntry)
