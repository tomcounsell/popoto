"""``[PG-only]`` behaviour of validity and supersession (#759 plan §5 M3).

Parity with Redis is the conformance suite (``tests/test_validity_field.py``,
``tests/test_validity_parity.py``); this file covers what has no Redis
counterpart, or whose Redis counterpart counts Redis client calls: the
columns, indexes and pointer table the compiler adds, the storage decision
(``double precision``, not ``tstzrange``), the lock order, one transaction per
supersede and its rollback, the typed retryable error, the composite mask's
control, a caller's unit of work, ``export_state``/``import_state``, the
contradicted wiring, a partitioned ``ConfidenceField`` whose partition moves,
and the documented divergences.
"""

import math
import threading
import time

import pytest

import popoto
from popoto import (
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
    SupersessionProtocol,
    ValidityField,
    ValidityMemberAbsentError,
)
from popoto.backends import BackendRetryableError, RecordId
from popoto.backends.postgres import validity as pg_validity
from popoto.fields.bm25_field import BM25Field

psycopg = pytest.importorskip("psycopg")

INF = float("inf")


class PgFact(popoto.Model):
    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField()
    validity = ValidityField()


class PgTeamFact(popoto.Model):
    """A partitioned ConfidenceField whose partition is not a key field."""

    name = popoto.UniqueKeyField()
    team = popoto.StringField(default="a")
    certainty = ConfidenceField(partition_by="team")


def _columns(admin, schema, table):
    rows = admin.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


def _statements(monkeypatch, backend):
    seen = []
    original = backend._run

    def run(sql, params=(), **kw):
        seen.append(sql)
        return original(sql, params, **kw)

    monkeypatch.setattr(backend, "_run", run)
    return seen


def _interval(backend, record):
    state = backend.field_call(
        PgFact._meta.spec,
        "validity",
        "interval",
        RecordId.from_key("PgFact", record.db_key.redis_key),
    )
    return state["valid_from"], state["invalid_at"], state["ingested_at"]


def _save(name, **kwargs):
    record = PgFact(name=name, **kwargs)
    record.save()
    return record


# -- schema and the storage decision ---------------------------------------------


def test_the_interval_is_three_double_precision_columns(pg, admin, pg_schema):
    _save("a")
    columns = _columns(admin, pg_schema.name, "pg_fact")
    assert columns["validity"] == "double precision"  # the declared value
    for suffix in ("__valid_from", "__invalid_at", "__ingested_at"):
        assert columns["validity" + suffix] == "double precision"
    for suffix in ("__supersedes", "__superseded_by"):
        assert columns["validity" + suffix] == "text"
    pointer = _columns(admin, pg_schema.name, "pg_fact__validity__open")
    assert pointer == {"digest": "text", "member": "text"}
    fks = admin.execute(
        "SELECT confdeltype FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = %s::regclass",
        (f"{pg_schema.name}.pg_fact__validity__open",),
    ).fetchall()
    assert fks == [("c",)], "the pointer must cascade with its record"
    indexes = dict(
        admin.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
            "AND tablename = 'pg_fact'",
            (pg_schema.name,),
        ).fetchall()
    )
    assert any("(validity__invalid_at)" in d for d in indexes.values())
    assert any("(validity__valid_from)" in d for d in indexes.values())


def test_timestamptz_would_not_hold_the_redis_score(pg, admin):
    """The evidence for the storage decision: a ``timestamptz`` (and so a
    ``tstzrange`` bound) keeps microseconds, so the epoch a Redis sorted set
    holds does not survive it -- and one ulp either side of a close is the
    gate's whole question."""
    score = 1_700_000_000.1234567
    (back,) = admin.execute(
        "SELECT extract(epoch FROM to_timestamp(%s::float8))::float8", (score,)
    ).fetchone()
    assert back != score
    nudged = math.nextafter(score, INF)
    (folded,) = admin.execute(
        "SELECT to_timestamp(%s::float8) = to_timestamp(%s::float8)", (score, nudged)
    ).fetchone()
    assert folded, "two distinct Redis scores collapse to one timestamptz"
    record = _save("a")
    pg.field_call(
        PgFact._meta.spec,
        "validity",
        "import",
        RecordId.from_key("PgFact", record.db_key.redis_key),
        {"invalid_at": nudged},
    )
    assert _interval(pg, record)[1] == nudged


def test_a_save_writes_the_interval_in_the_upsert(pg, monkeypatch):
    seen = _statements(monkeypatch, pg)
    record = _save("a")
    # The upsert follows the record-key lock in the same message (M2b).
    writes = [s for s in seen if "INSERT INTO" in s and "ON CONFLICT" in s]
    assert len(writes) == 1 and '"validity__valid_from"' in writes[0]
    vf, ia, ig = _interval(pg, record)
    assert ia == INF and vf == ig


# -- supersede: one transaction, lock order, rollback --------------------------------


def test_supersede_locks_the_field_first_then_the_rows_in_pk_order(pg, monkeypatch):
    identity = SupersessionProtocol.identity_key("u", "plan")
    old, new = _save("zz-old"), _save("aa-new")
    SupersessionProtocol.supersede(old, identity_key=identity)
    seen = _statements(monkeypatch, pg)
    assert SupersessionProtocol.supersede(new, identity_key=identity) == (
        old.db_key.redis_key
    )
    assert seen[0].startswith("SELECT pg_advisory_xact_lock")
    locks = [s for s in seen if "FOR UPDATE" in s]
    assert len(locks) == 1 and 'ORDER BY "_pk" COLLATE "C" FOR UPDATE' in locks[0]
    assert seen.index(locks[0]) > 0
    writes = [s for s in seen if s.startswith(("UPDATE", "INSERT"))]
    assert len(writes) == 2  # the interval/link states, then the pointer


def test_a_fault_after_the_close_rolls_the_whole_supersede_back(pg, monkeypatch):
    """The twin of the Redis "fault after EVAL" test: here the fault lands
    between the state write and the pointer repoint, inside the one
    transaction, so neither survives."""
    identity = SupersessionProtocol.identity_key("u", "plan")
    old, new = _save("old"), _save("new")
    SupersessionProtocol.supersede(old, identity_key=identity)
    before_old, before_new = _interval(pg, old), _interval(pg, new)
    original = pg._run

    def exploding(sql, params=(), **kw):
        if sql.startswith("INSERT INTO") and "__open" in sql:
            raise RuntimeError("injected fault after the close")
        return original(sql, params, **kw)

    monkeypatch.setattr(pg, "_run", exploding)
    with pytest.raises(RuntimeError, match="injected"):
        SupersessionProtocol.supersede(new, identity_key=identity)
    monkeypatch.undo()
    assert _interval(pg, old) == before_old
    assert _interval(pg, new) == before_new
    assert SupersessionProtocol.superseded_by(old) is None
    assert pg.field_call(PgFact._meta.spec, "validity", "pointer", identity) == (
        old.db_key.redis_key
    )


def test_resolve_excluded_keys_is_one_select(pg, monkeypatch):
    old = _save("old")
    SupersessionProtocol.invalidate(old)
    seen = _statements(monkeypatch, pg)
    excluded = ValidityField.resolve_excluded_keys(PgFact, "validity")
    assert excluded == {old.db_key.redis_key}
    assert len(seen) == 1 and seen[0].startswith("SELECT")


def test_nan_instants_are_refused_before_any_write(pg):
    record = _save("a")
    with pytest.raises(ValueError, match="NaN"):
        SupersessionProtocol.invalidate(record, at=float("nan"))
    assert _interval(pg, record)[1] == INF


# -- a caller's unit of work ------------------------------------------------------------


def test_save_and_supersede_in_a_callers_transaction(pg):
    identity = SupersessionProtocol.identity_key("u", "plan")
    first = _save("first")
    SupersessionProtocol.supersede(first, identity_key=identity)
    with pg.transaction() as uow:
        result = SupersessionProtocol.save_and_supersede(
            PgFact(name="second"), identity_key=identity, pipeline=uow
        )
        # Inside the transaction the outcome is known, not "unknown until
        # you execute" as a Redis pipeline's is.
        assert result.closed_key == first.db_key.redis_key
        assert result.pipeline is uow and result.close_index is None
    assert _interval(pg, first)[1] != INF


def test_a_callers_rolled_back_transaction_applies_nothing(pg):
    identity = SupersessionProtocol.identity_key("u", "plan")
    first = _save("first")
    SupersessionProtocol.supersede(first, identity_key=identity)
    with pytest.raises(RuntimeError, match="abort"):
        with pg.transaction() as uow:
            SupersessionProtocol.save_and_supersede(
                PgFact(name="second"), identity_key=identity, pipeline=uow
            )
            raise RuntimeError("abort the block")
    assert PgFact.query.get(name="second") is None
    assert _interval(pg, first)[1] == INF
    assert pg.field_call(PgFact._meta.spec, "validity", "pointer", identity) == (
        first.db_key.redis_key
    )


def test_a_typed_error_inside_a_unit_of_work_is_raised_at_the_call(pg):
    old = _save("old")
    with pytest.raises(ValidityMemberAbsentError):
        with pg.transaction() as uow:
            SupersessionProtocol.invalidate(
                old, superseded_by=PgFact(name="ghost"), pipeline=uow
            )
    assert _interval(pg, old)[1] == INF


def test_a_redis_pipeline_is_refused_by_save_and_supersede(pg):
    from popoto.redis_db import get_REDIS_DB

    with pytest.raises(ValueError, match="unit of work"):
        SupersessionProtocol.save_and_supersede(
            PgFact(name="x"),
            identity_key=("u", "p"),
            pipeline=get_REDIS_DB().pipeline(),
        )


def test_a_redis_pipeline_is_refused_by_supersede_and_invalidate(pg):
    """As ``save_and_*`` refuses one (#777 review): a Redis pipeline cannot
    carry a Postgres write, so it is refused rather than run at once."""
    from popoto.redis_db import get_REDIS_DB

    old, new = _save("old"), _save("new")
    pipe = get_REDIS_DB().pipeline()
    with pytest.raises(ValueError, match="unit of work"):
        SupersessionProtocol.supersede(new, identity_key=("u", "p"), pipeline=pipe)
    with pytest.raises(ValueError, match="unit of work"):
        SupersessionProtocol.invalidate(old, pipeline=pipe)
    assert len(pipe.command_stack) == 0
    assert _interval(pg, old)[1] == INF


def _deadlocks(admin):
    admin.execute("SELECT pg_stat_clear_snapshot()")
    (count,) = admin.execute(
        "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
    ).fetchone()
    return int(count)


def test_a_cross_operation_deadlock_is_a_retryable_error(pg, admin):
    """The residual the lock order cannot prevent: a caller's transaction
    locks a row, then supersedes, while a concurrent supersede holds the
    ``(model, field)`` lock and waits for that row. Postgres breaks the cycle;
    the caller sees :class:`BackendRetryableError` (or completes, if the
    other side was the one aborted -- its owned transaction retries)."""
    k, other, m = _save("k"), _save("other"), _save("m")
    errors: list[BaseException] = []
    caller_saved = threading.Event()
    waiter_started = threading.Event()

    def caller():
        try:
            with pg.transaction() as uow:
                k.importance = 2.0
                k.save(pipeline=uow)
                caller_saved.set()
                waiter_started.wait(5)
                time.sleep(0.3)
                SupersessionProtocol.invalidate(other, pipeline=uow)
        except BaseException as e:
            errors.append(e)

    def waiter():
        caller_saved.wait(5)
        waiter_started.set()
        try:
            ValidityField.execute_supersede(
                PgFact,
                "validity",
                new_member=m.db_key.redis_key,
                mode="supersede",
                old_member=k.db_key.redis_key,
            )
        except BaseException as e:
            errors.append(e)

    before = _deadlocks(admin)
    threads = [threading.Thread(target=caller), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(isinstance(e, BackendRetryableError) for e in errors), errors
    assert not any(isinstance(e, psycopg.Error) for e in errors)
    deadline = time.monotonic() + 5
    while _deadlocks(admin) == before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _deadlocks(admin) > before, "the interleaving never deadlocked"


def _lock_waiters(admin):
    (count,) = admin.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock'"
    ).fetchone()
    return int(count)


def test_save_and_supersede_of_an_existing_record_takes_the_supersede_lock_order(
    pg, admin, monkeypatch
):
    """#777 review: ``save_and_supersede`` of an existing record used to save
    (its record-key and row locks) before the supersede took the
    ``(model, field)`` lock, so a concurrent supersede naming that record --
    holding the field lock, waiting on the row -- deadlocked against it. It
    now takes the supersede's locks before the save. Forced: the owner pauses
    between its save and its close until the other writer is blocked, three
    rounds; the deadlock counter must not move. (With the pre-lock removed
    every round deadlocks.)"""
    original = ValidityField.execute_supersede.__func__
    paused, release = threading.Event(), threading.Event()

    def pausing(cls, *args, **kwargs):
        if threading.current_thread().name == "owner":
            paused.set()
            release.wait(10)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(ValidityField, "execute_supersede", classmethod(pausing))
    before = _deadlocks(admin)
    for round_ in range(3):
        identity = SupersessionProtocol.identity_key("u", f"r{round_}")
        incumbent = _save(f"p{round_}")
        SupersessionProtocol.supersede(incumbent, identity_key=identity)
        x, z = _save(f"x{round_}"), _save(f"z{round_}")
        paused.clear()
        release.clear()
        out: dict[str, object] = {}

        def owner(x=x, identity=identity):
            x.importance = 2.0
            try:
                result = SupersessionProtocol.save_and_supersede(
                    x, identity_key=identity
                )
                out["owner"] = result.closed_key
            except BaseException as e:  # pragma: no cover - asserted below
                out["owner"] = e

        def other(x=x, z=z):
            try:
                out["other"] = original(
                    ValidityField,
                    PgFact,
                    "validity",
                    new_member=z.db_key.redis_key,
                    mode="supersede",
                    old_member=x.db_key.redis_key,
                )
            except BaseException as e:  # pragma: no cover - asserted below
                out["other"] = e

        first = threading.Thread(target=owner, name="owner")
        first.start()
        assert paused.wait(10)
        second = threading.Thread(target=other, name="other")
        second.start()
        deadline = time.monotonic() + 10
        while _lock_waiters(admin) == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _lock_waiters(admin) > 0, "the other writer never blocked"
        release.set()
        first.join(30)
        second.join(30)
        assert out["owner"] == incumbent.db_key.redis_key, out
        assert out["other"] == x.db_key.redis_key, out
        assert SupersessionProtocol.superseded_by(x).name == f"z{round_}"
    assert _deadlocks(admin) == before


# -- the composite mask's control ---------------------------------------------------


def test_without_the_mask_the_closed_member_leaks_through_the_confidence_arm(pg):
    from popoto.backends import RankTerm

    old, new = _save("old"), _save("new")
    for _ in range(4):
        ConfidenceField.update_confidence(old, "certainty", signal=0.95)
    ConfidenceField.update_confidence(new, "certainty", signal=0.2)
    SupersessionProtocol.invalidate(old, superseded_by=new)
    terms = [
        RankTerm("decay", 0.5, "relevance", options={"confidence_field": None}),
        RankTerm("confidence", 0.5, "certainty"),
    ]
    kwargs = dict(
        limit=10,
        aggregate="SUM",
        min_score=None,
        where=None,
        as_of=None,
        temperature=1.0,
    )
    leaked = [
        rid.canonical
        for rid, _ in pg.rank_composite(PgFact._meta.spec, terms, **kwargs)
    ]
    masked = [
        rid.canonical
        for rid, _ in pg.rank_composite(
            PgFact._meta.spec, terms, validity_field="validity", **kwargs
        )
    ]
    assert old.db_key.redis_key in leaked, "control: the leak must reproduce"
    assert masked == [new.db_key.redis_key]


# -- export / import, the contradicted wiring ------------------------------------------


def test_export_and_import_state_round_trip(pg):
    identity = SupersessionProtocol.identity_key("u", "plan")
    old, new = _save("old"), _save("new")
    SupersessionProtocol.supersede(old, identity_key=identity)
    SupersessionProtocol.supersede(new, identity_key=identity)
    exported_old = ValidityField.export_state(old, "validity", None)
    exported_new = ValidityField.export_state(new, "validity", None)
    assert exported_old["chain_fwd"] == new.db_key.redis_key
    assert exported_new["chain_rev"] == old.db_key.redis_key
    assert exported_new["invalid_at"] == "+inf"
    assert exported_new["open_pointers"] == [identity]
    for record in (old, new):
        record.delete()
    old2, new2 = _save("old"), _save("new")
    ValidityField.import_state(old2, "validity", exported_old)
    ValidityField.import_state(new2, "validity", exported_new)
    assert ValidityField.export_state(old2, "validity", None) == exported_old
    assert ValidityField.export_state(new2, "validity", None) == exported_new
    assert [r.name for r in PgFact.query.filter(validity__current=True)] == ["new"]


def test_contradicted_with_a_successor_closes_and_chains(pg):
    old, new = _save("old"), _save("new")
    old._superseded_by = new
    ObservationProtocol.on_context_used([old], {old.db_key.redis_key: "contradicted"})
    assert _interval(pg, old)[1] != INF
    assert SupersessionProtocol.superseded_by(old).name == "new"
    assert ConfidenceField.get_confidence(old, "certainty") < 0.5


def test_the_observe_batch_takes_the_field_lock_before_its_rows(pg, monkeypatch):
    record = _save("a")
    seen = _statements(monkeypatch, pg)
    ObservationProtocol.on_context_used([record], {record.db_key.redis_key: "acted"})
    lock = next(i for i, s in enumerate(seen) if "pg_advisory_xact_lock" in s)
    rows = next(i for i, s in enumerate(seen) if "FOR UPDATE" in s)
    assert lock < rows


# -- partitioned confidence -----------------------------------------------------------


def test_a_partition_change_keeps_the_confidence_state_in_the_row(pg):
    item = PgTeamFact(name="t1", team="a")
    item.save()
    ConfidenceField.update_confidence(item, "certainty", signal=0.9)
    item.team = "b"
    item.save()
    data = ConfidenceField.get_confidence_data(item, "certainty")
    assert abs(data["confidence"] - 0.7) < 1e-12 and data["evidence_count"] == 1


# -- documented divergences ------------------------------------------------------------


def test_mode_open_on_a_member_with_no_record_writes_nothing(pg):
    """On Redis ``ZADD NX`` indexes a member with no record; here the interval
    is the record's row, so there is nothing to write."""
    ValidityField.execute_supersede(
        PgFact, "validity", new_member="PgFact:not-a-record", mode="open"
    )
    assert pg.field_call(PgFact._meta.spec, "validity", "dump")["valid_from"] == []


def test_a_pointer_cannot_name_a_record_that_does_not_exist(pg):
    """The pointer table's foreign key: the dangling-pointer hint Redis reads
    as "no incumbent" cannot be stored here."""
    _save("a")
    with pytest.raises(ValidityMemberAbsentError, match="never-saved") as err:
        pg.field_call(
            PgFact._meta.spec,
            "validity",
            "import",
            RecordId.from_key("PgFact", "PgFact:never-saved"),
            {"open_pointers": ["d" * 16]},
        )
    assert isinstance(err.value, ValueError)
    assert isinstance(err.value.__cause__, psycopg.errors.ForeignKeyViolation)


def test_the_exclusion_sql_is_the_lua_rule():
    rule = pg_validity.excluded_sql("validity", 5.0)
    assert '"validity__invalid_at" <= (5.0::float8)' in rule
    assert '"validity__valid_from" > (5.0::float8)' in rule
    assert pg_validity.included_sql("validity", float("nan")) == "TRUE"
    assert pg_validity.included_sql("validity", None) == "TRUE"


# -- recall and top_by_relevance honour the validity gate (M3 review) -----------


class PgRecallFact(popoto.Model):
    """A searchable, decaying, validity-gated model: every ``recall`` arm."""

    name = popoto.UniqueKeyField()
    text = popoto.Field(type=str, default="deploy runbook")
    lexical = BM25Field(source="text")
    relevance = DecayingSortedField()
    validity = ValidityField()


class PgRecallPlain(popoto.Model):
    name = popoto.UniqueKeyField()
    text = popoto.Field(type=str, default="deploy runbook")
    lexical = BM25Field(source="text")
    relevance = DecayingSortedField()


def _gate_fixture():
    """``old`` (closed 500 s ago), ``cur`` (open), ``fut`` (starts in a
    million seconds)."""
    now = time.time()
    old, cur, _fut = (
        PgRecallFact(name=n, validity=v)
        for n, v in (("old", now - 1000), ("cur", now - 1000), ("fut", now + 1e6))
    )
    for r in (old, cur, _fut):
        r.save()
    SupersessionProtocol.invalidate(old, at=now - 500, superseded_by=cur)
    return now


def _names(hits):
    return sorted(r.name for r, _score in hits)


def test_top_by_relevance_excludes_closed_and_not_yet_valid_records(pg):
    _gate_fixture()
    assert _names(PgRecallFact.query.top_by_relevance(limit=10)) == ["cur"]


def test_top_by_relevance_as_of_includes_the_then_valid_record(pg):
    now = _gate_fixture()
    hits = PgRecallFact.query.top_by_relevance(limit=10, as_of=now - 700)
    assert _names(hits) == ["cur", "old"]
    hits = PgRecallFact.query.top_by_relevance(limit=10, as_of=now - 100)
    assert _names(hits) == ["cur"]


def test_recall_excludes_closed_and_not_yet_valid_records_in_every_arm(pg):
    _gate_fixture()
    # The BM25 arm matches all three; the decay arm ranks all three.
    assert _names(PgRecallFact.query.recall("deploy", limit=10)) == ["cur"]
    only_decay = PgRecallFact.query.recall("zzz", limit=10)
    assert _names(only_decay) == ["cur"]


def test_recall_as_of_includes_the_then_valid_record(pg):
    now = _gate_fixture()
    hits = PgRecallFact.query.recall("deploy", limit=10, as_of=now - 700)
    assert _names(hits) == ["cur", "old"]


def test_recall_and_top_by_relevance_are_unchanged_without_a_validity_field(pg):
    for n in ("a", "b"):
        PgRecallPlain(name=n).save()
    assert len(PgRecallPlain.query.top_by_relevance(limit=10)) == 2
    assert len(PgRecallPlain.query.recall("deploy", limit=10)) == 2
    assert len(PgRecallPlain.query.recall("deploy", limit=10, as_of=1.0)) == 2
