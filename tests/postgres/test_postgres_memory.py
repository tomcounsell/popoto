"""``[PG-only]`` behaviour of ranking and memory state (#759 plan §5 M2a).

Parity with Redis is the conformance suite and
``tests/test_backend_parity_memory.py``; this file covers what has no Redis
counterpart: the columns and indexes the compiler adds, the statement count of
each operation (one ``SELECT`` per ranking, one ``UPDATE … RETURNING`` per
confidence update), the clamped decay expression agreeing with the plain one,
the §6 lock order and the typed retryable error, the ``RecallProposal`` table,
and that none of it touches Redis.
"""

import math
import threading
import time

import psycopg
import pytest

import popoto
from popoto import (
    AccessTrackerMixin,
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
    RecallProposal,
)
from popoto.backends import (
    BackendCapabilityError,
    BackendRetryableError,
    RecordId,
    SchemaDriftError,
    get_backend,
)
from popoto.backends.postgres import memory
from popoto.backends.postgres.memory import decay_score_sql, lua_tostring

DAY = 86400.0


class PgMem(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField(null=False)
    weight = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(
        decay_rate=0.5, base_score_field="weight", partition_by="agent"
    )
    certainty = ConfidenceField()


class PgPlain(popoto.Model):
    name = popoto.KeyField()
    score = popoto.SortedField(type=float, default=0.0)


def _columns(admin, schema, table):
    rows = admin.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


def _indexes(admin, schema, table):
    rows = admin.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


def _statements(monkeypatch, backend):
    """Record every statement the backend runs (and through which path)."""
    seen = []
    original = backend._run

    def run(sql, params=(), **kw):
        seen.append(sql)
        return original(sql, params, **kw)

    monkeypatch.setattr(backend, "_run", run)
    return seen


def _backdate(backend, record, ts):
    table = backend._table(type(record)._meta.spec)
    backend._run(
        f'UPDATE {table.qualified} SET "relevance" = %s WHERE "_pk" = %s',
        [ts, record.db_key.redis_key],
    )


# -- schema -------------------------------------------------------------------


def test_memory_columns_and_indexes(pg, admin, pg_schema):
    PgMem.create(name="a", agent="x")
    columns = _columns(admin, pg_schema.name, "pg_mem")
    assert columns["relevance"] == "double precision"
    assert columns["certainty"] == "double precision"
    assert columns["certainty__conf"] == "double precision"
    for suffix in ("__n", "__corr", "__contra"):
        assert columns["certainty" + suffix] == "bigint"
    assert columns["_access_count"] == "bigint"
    assert columns["_last_accessed"] == "double precision"
    assert columns["_staged_reads"] == "bigint"
    assert columns["_staged_at"] == "double precision"
    indexes = _indexes(admin, pg_schema.name, "pg_mem")
    sort = indexes["pg_mem__sort__relevance__idx"]
    assert '(agent, relevance, _pk COLLATE "C")' in sort
    # No index on a state column: each would make every update non-HOT.
    assert not [d for d in indexes.values() if "__conf" in d or "_access" in d]


def test_a_plain_model_gains_no_state_columns(pg, admin, pg_schema):
    PgPlain.create(name="p", score=1.0)
    columns = _columns(admin, pg_schema.name, "pg_plain")
    assert not [c for c in columns if "__" in c or c.startswith(("_access", "_staged"))]


def test_partitioned_confidence_is_refused_at_declaration():
    with pytest.raises(BackendCapabilityError, match="M3"):

        class PgPartConf(popoto.Model):
            name = popoto.KeyField()
            project = popoto.KeyField()
            certainty = ConfidenceField(partition_by="project")

            class Meta:
                backend = "postgres"


def test_state_column_collisions_are_refused(pg):
    class PgCollide(AccessTrackerMixin, popoto.Model):
        name = popoto.KeyField()
        _access_count = popoto.IntField(default=0)

    with pytest.raises(BackendCapabilityError, match="_access_count"):
        PgCollide.create(name="c")


# -- one statement each ------------------------------------------------------


def test_rank_decayed_is_one_select(pg, monkeypatch):
    for i in range(3):
        PgMem.create(name=f"m{i}", agent="x", weight=1.0 + i)
    seen = _statements(monkeypatch, pg)
    scored = pg.rank_decayed(
        PgMem._meta.spec,
        "relevance",
        now=time.time(),
        n=2,
        where=memory.partition_where({"agent": "x"}),
        base_score_field="weight",
        confidence_field="certainty",
    )
    assert len(seen) == 1 and seen[0].startswith("SELECT")
    assert "ORDER BY" in seen[0] and 'COLLATE "C"' in seen[0]
    assert [rid.canonical for rid, _ in scored] == ["PgMem:x:m2", "PgMem:x:m1"]
    seen.clear()
    assert pg.rank_decayed(PgMem._meta.spec, "relevance", now=time.time(), n=0) == []
    assert seen == []


def test_top_by_decay_is_rank_load_and_stage(pg, monkeypatch):
    PgMem.create(name="a", agent="x")
    seen = _statements(monkeypatch, pg)
    assert [r.name for r in PgMem.query.filter(agent="x").top_by_decay(n=5)] == ["a"]
    assert [s.split()[0] for s in seen] == ["SELECT", "SELECT", "WITH"]


def test_update_confidence_is_one_update_returning(pg, monkeypatch):
    record = PgMem.create(name="c", agent="x")
    seen = _statements(monkeypatch, pg)
    assert ConfidenceField.update_confidence(record, "certainty", 0.9) == 0.7
    assert len(seen) == 1 and seen[0].startswith("UPDATE") and "RETURNING" in seen[0]


def test_rank_decayed_refusals(pg):
    spec = PgMem._meta.spec
    with pytest.raises(BackendCapabilityError, match="M3"):
        pg.rank_decayed(spec, "relevance", now=time.time(), n=5, validity_field="v")
    with pytest.raises(BackendCapabilityError, match="not a DecayingSortedField"):
        pg.rank_decayed(spec, "weight", now=time.time(), n=5)


# -- the clamped expression ---------------------------------------------------


def _both_paths(pg, record, now, **kw):
    """The plain and the clamped expression for one row, side by side."""
    spec = type(record)._meta.spec
    table = pg._table(spec)
    plain = decay_score_sql(table, spec, "relevance", now=now, **kw)
    clamped = decay_score_sql(table, spec, "relevance", now=now, clamped=True, **kw)
    rows, _ = pg._run(
        f'SELECT {plain}, {clamped} FROM {table.qualified} AS t WHERE t."_pk" = %s',
        [record.db_key.redis_key],
    )
    return rows[0]


@pytest.mark.parametrize("days", [0.0, 0.5, 1.0, 3.7, 40.0, 900.0])
@pytest.mark.parametrize("weight", [1.0, -2.5, 0.0, 1e-30, 7e20])
@pytest.mark.parametrize("confidence", [None, 0.0, 0.31, 0.5, 1.0])
def test_the_clamped_path_agrees_with_the_plain_one(pg, days, weight, confidence):
    record = PgMem.create(name="p", agent="x", weight=weight)
    now = time.time()
    _backdate(pg, record, now - days * DAY)
    if confidence is not None:
        table = pg._table(PgMem._meta.spec)
        pg._run(
            f'UPDATE {table.qualified} SET "certainty__conf" = %s WHERE "_pk" = %s',
            [confidence, record.db_key.redis_key],
        )
    plain, clamped = _both_paths(
        pg,
        record,
        now,
        rate=0.5,
        base_field="weight",
        confidence_field="certainty",
        strength=0.5,
    )
    assert plain == clamped


@pytest.mark.parametrize(
    "days, rate, weight, strength",
    [
        (0.0, 155.0, 1.0, 0.5),
        (0.0, 400.0, 1e300, 0.5),
        (100.0, 200.0, 1.0, 0.5),
        (1e9, 3.0, -1e-200, 0.5),
        (30.0, 0.5, 1.0, 5000.0),
        (30.0, 1e-320, 1.0, 0.5),
        (30.0, -50.0, 1e300, 0.5),
        (30.0, 1e308, 1.0, 0.5),
    ],
)
def test_pathological_inputs_saturate_and_never_raise(pg, days, rate, weight, strength):
    record = PgMem.create(name="z", agent="x", weight=weight)
    now = time.time()
    _backdate(pg, record, now - days * DAY)
    table = pg._table(PgMem._meta.spec)
    pg._run(
        f'UPDATE {table.qualified} SET "certainty__conf" = 0.05 WHERE "_pk" = %s',
        [record.db_key.redis_key],
    )
    _plain, clamped = _both_paths(
        pg,
        record,
        now,
        rate=rate,
        base_field="weight",
        confidence_field="certainty",
        strength=strength,
    )

    def c_pow(x, y):
        try:
            return math.pow(x, y)
        except OverflowError:
            return math.inf

    elapsed = max((now - (now - days * DAY)) / 86400, 0.01)
    sign = -1 if weight < 0 else 1
    expected = sign * abs(weight) * c_pow(elapsed, -rate)
    c = 0.05
    eff = rate * c_pow(2, strength * 2 * (0.5 - c))
    expected = expected * c_pow(max(elapsed, 1.0), -(eff - rate))
    assert lua_tostring(clamped) == lua_tostring(expected)


# -- lock order and the typed retryable error ---------------------------------


def test_on_context_used_locks_rows_in_pk_order_first(pg, monkeypatch):
    records = [PgMem.create(name=n, agent="x") for n in ("b", "C", "a")]
    seen = _statements(monkeypatch, pg)
    ObservationProtocol.on_context_used(
        records, {r.db_key.redis_key: "acted" for r in records}
    )
    assert "FOR UPDATE" in seen[0] and 'ORDER BY "_pk" COLLATE "C"' in seen[0]
    touched = [s for s in seen if s.startswith("UPDATE") and '"relevance" =' in s]
    assert len(touched) == 3


def test_a_deadlock_is_retried_then_raised_as_a_typed_error(pg, monkeypatch):
    record = PgMem.create(name="d", agent="x")
    attempts = []

    def deadlock(*args, **kwargs):
        attempts.append(1)
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(pg, "_lock_rows", deadlock)
    with pytest.raises(BackendRetryableError):
        ObservationProtocol.on_context_used(
            [record], {record.db_key.redis_key: "acted"}
        )
    from popoto.fields.constants import Defaults

    assert len(attempts) == Defaults.PG_TRANSACTION_RETRIES + 1
    attempts.clear()
    with pytest.raises(BackendRetryableError):
        with pg.transaction() as uow:
            ObservationProtocol.on_context_used(
                [record], {record.db_key.redis_key: "acted"}, pipeline=uow
            )
    assert len(attempts) == 1


def test_crossing_batches_do_not_deadlock(pg):
    records = [PgMem.create(name=f"x{i}", agent="x") for i in range(6)]
    errors = []

    def batch(order):
        try:
            for _ in range(10):
                ObservationProtocol.on_context_used(
                    order, {r.db_key.redis_key: "acted" for r in order}
                )
        except Exception as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    threads = [
        threading.Thread(target=batch, args=(records,)),
        threading.Thread(target=batch, args=(list(reversed(records)),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = ConfidenceField.get_confidence_data(records[0], "certainty")
    assert data["evidence_count"] == 20


def test_a_unit_of_work_carries_the_confidence_update(pg):
    record = PgMem.create(name="u", agent="x")
    with pytest.raises(RuntimeError):
        with pg.transaction() as uow:
            value = ConfidenceField.update_confidence(
                record, "certainty", 0.9, pipeline=uow
            )
            assert value == 0.7 and record.certainty == 0.7
            raise RuntimeError("roll back")
    data = ConfidenceField.get_confidence_data(record, "certainty")
    assert data["evidence_count"] == 0


def test_touch_on_a_deleted_record_is_a_no_op(pg):
    record = PgMem.create(name="t", agent="x")
    record.delete()
    rid = RecordId.from_key("PgMem", record.db_key.redis_key)
    assert pg.touch(PgMem._meta.spec, rid, "relevance", at=123.0) == 123.0
    assert PgMem.query.count() == 0


# -- RecallProposal -----------------------------------------------------------


def test_the_recall_table_is_created_on_first_use(pg, admin, pg_schema):
    record = PgMem.create(name="r", agent="x")
    RecallProposal.create_batch([record], partition="p")
    tables = {
        row[0]
        for row in admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (pg_schema.name,)
        ).fetchall()
    }
    assert "popoto_recall_proposal" in tables
    assert RecallProposal.get_pending(PgMem, "p")[0][0] == record.db_key.redis_key


def test_the_recall_table_honours_schema_auto_off(pg, monkeypatch):
    PgMem.create(name="r", agent="x")
    monkeypatch.setenv("POPOTO_SCHEMA_AUTO", "0")
    with pytest.raises(SchemaDriftError, match="POPOTO_SCHEMA_AUTO=0"):
        RecallProposal.get_pending(PgMem)


# -- no Redis -----------------------------------------------------------------


def test_the_memory_surface_never_touches_redis(pg, monkeypatch):
    """With the Redis client made to raise, every M2a call on a
    Postgres-bound model still completes (plan §5 M2, [PG-only])."""
    client = popoto.get_redis()

    def boom(*args, **kwargs):
        raise AssertionError("Redis was called for a Postgres-bound model")

    monkeypatch.setattr(client, "execute_command", boom)
    monkeypatch.setattr(client, "pipeline", boom)
    records = [PgMem.create(name=f"n{i}", agent="x") for i in range(3)]
    for record in records:
        record.on_read()
    hits = PgMem.query.filter(agent="x").top_by_decay(n=3)
    assert len(hits) == 3
    ObservationProtocol.on_surfaced(records, partition="p")
    ObservationProtocol.on_context_used(
        records,
        {
            records[0].db_key.redis_key: "acted",
            records[1].db_key.redis_key: "contradicted",
            records[2].db_key.redis_key: "used",
        },
    )
    # its own on_read() plus the one top_by_decay staged, confirmed by acted
    assert records[0].access_count == 2
    assert ConfidenceField.get_confidence(records[1], "certainty") < 0.5
    ranked = PgMem.query.filter(agent="x").composite_score(
        {"relevance": 0.5, "certainty": 0.3, "access_count": 0.2}
    )
    assert ranked[0].name == "n0"
    assert RecallProposal.expire_stale(PgMem, "p", ttl=0) == sorted(
        r.db_key.redis_key for r in records
    )


def test_backend_spec_carries_the_memory_options(pg):
    spec = PgMem._meta.spec
    assert spec.mixins >= {"AccessTrackerMixin"}
    assert spec.fields["relevance"].options["decay_rate"] == 0.5
    assert spec.fields["relevance"].options["base_score_field"] == "weight"
    assert spec.fields["certainty"].options["initial_confidence"] == 0.5
    assert spec.fields["certainty"].options["evidence_cap"] == 20
    assert get_backend(PgMem).name == "postgres"
    assert RecordId.from_key("PgMem", "PgMem:x:a").canonical == "PgMem:x:a"


def test_a_deadlocked_statement_is_retried_not_reported_as_an_outage(pg, monkeypatch):
    """psycopg 3 does not derive ``DeadlockDetected`` (40P01) or
    ``SerializationFailure`` (40001) from ``TransactionRollback``: before
    #759 M2a a real deadlock skipped the retry and was logged as an outage
    with a dropped write."""
    record = PgMem.create(name="dl", agent="x")
    original = psycopg.Connection.execute
    raised = []

    def flaky(self, query, params=None, **kwargs):
        if '"relevance" = ' in str(query) and not raised:
            raised.append(query)
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return original(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", flaky)
    record.touch("relevance")
    assert raised, "the deadlock was never injected"
    assert pg.health.ok and pg.health.dropped_writes == 0
