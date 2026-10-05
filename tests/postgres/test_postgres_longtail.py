"""``[PG-only]`` behaviour of the long-tail fields (#759 plan §5 M5):
``CyclicDecayField``, ``TDValueField`` and ``PredictionLedgerMixin``.

Parity with Redis is the conformance suite (``test_cyclic_decay_field.py``,
``test_prediction_ledger.py``, ``test_td_value_field.py``, the observation
and assembler files), ``tests/test_backend_parity_longtail.py`` and
``scripts/probe_longtail_parity.py``. This file covers what has no Redis
counterpart: the columns the compiler adds and the engine tables; the
statement count of each operation (the merge inside the save's upsert, one
``UPDATE`` per adjustment and per TD update, one statement per resolution);
the clamped expression never raising where C saturates; the observation
batch's auto-resolve inside its transaction (lock order, no self-wait); and
that none of it touches Redis.
"""

import math
import threading
import time
from decimal import Decimal

import pytest

import popoto
from popoto import (
    AccessTrackerMixin,
    ConfidenceField,
    CyclicDecayField,
    ObservationProtocol,
)
from popoto.backends import RecordId, get_backend
from popoto.backends.postgres import longtail
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.td_value_field import TDValueField

# The Redis and Valkey CI jobs do not install the postgres extra.
psycopg = pytest.importorskip("psycopg")

DAY = 86400.0
LOCK = "SELECT pg_advisory_xact_lock(hashtextextended("


class PgCyc(popoto.Model):
    name = popoto.UniqueKeyField()
    agent = popoto.KeyField(null=False)
    weight = popoto.FloatField(default=1.0)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        base_score_field="weight",
        partition_by="agent",
        cycles=[(86400, 2.0, 0), (604800, 3.0, 100)],
        pressure_rate=0.1,
    )
    certainty = ConfidenceField()


class PgTd(popoto.Model):
    name = popoto.KeyField()
    q_value = TDValueField(default=Decimal("0"))


class PgLedger(AccessTrackerMixin, PredictionLedgerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5, cycles=[(604800, 4.0, 0)], pressure_rate=0.2
    )
    certainty = ConfidenceField()


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


def _rid(record):
    return RecordId.from_key(record._meta.model_name, record.db_key.redis_key)


def _state(backend, record, field="relevance"):
    return backend.field_call(type(record)._meta.spec, field, "state", _rid(record))


# -- schema -------------------------------------------------------------------


def test_cyclic_columns_and_index(pg, admin, pg_schema):
    PgCyc.create(name="a", agent="x")
    cols = _columns(admin, pg_schema.name, "pg_cyc")
    assert cols["relevance"] == "double precision"
    for suffix in longtail.CYCLE_SUFFIXES:
        assert cols["relevance" + suffix] == "ARRAY"
    for suffix in longtail.PRESSURE_SUFFIXES:
        assert cols["relevance" + suffix] == "double precision"
    rows = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = "
        "'pg_cyc' AND indexdef LIKE '%%relevance%%'",
        (pg_schema.name,),
    ).fetchall()
    assert len(rows) == 1 and '(agent, relevance, _pk COLLATE "C")' in rows[0][0]


def test_td_value_is_a_numeric_column(pg, admin, pg_schema):
    PgTd.create(name="a")
    assert _columns(admin, pg_schema.name, "pg_td")["q_value"] == "numeric"


def test_the_ledger_tables_are_engine_tables(pg, admin, pg_schema):
    record = PgLedger.create(name="l")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    PredictionLedgerMixin.auto_resolve(record, "used")
    tables = {
        row[0]
        for row in admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s",
            (pg_schema.name,),
        ).fetchall()
    }
    assert {"popoto_prediction_ledger", "popoto_prediction_error"} <= tables
    assert not any(t.startswith("pg_ledger__") for t in tables)


# -- one statement each ----------------------------------------------------------


def test_the_cycles_merge_rides_in_the_save_statement(pg, monkeypatch):
    record = PgCyc.create(name="m", agent="x")
    record.strengthen_cycle("relevance", factor=2.0)
    seen = _statements(monkeypatch, pg)
    record.save()
    writes = [s for s in seen if "INSERT INTO" in s]
    assert len(writes) == 1, "the merge must not be a second statement"
    assert "ON CONFLICT" in writes[0] and 'old."relevance__cycle_amp"' in writes[0]
    assert _state(pg, record)["cycles"] == [[86400, 4.0, 0, 2], [604800, 6.0, 100, 3]]


def test_an_adjustment_is_one_update(pg, monkeypatch):
    record = PgCyc.create(name="adj", agent="x")
    seen = _statements(monkeypatch, pg)
    assert record.weaken_cycle("relevance", factor=0.5) == [
        [86400, 1.0, 0.0],
        [604800, 1.5, 100.0],
    ]
    assert len(seen) == 1 and seen[0].startswith(LOCK)
    assert "UPDATE" in seen[0] and "RETURNING" in seen[0]


def test_td_update_is_one_statement_and_writes_the_lua_tostring(pg, monkeypatch):
    record = PgTd.create(name="t", q_value=Decimal("0.1"))
    seen = _statements(monkeypatch, pg)
    td = TDValueField.td_update(record, "q_value", reward=1.0, gamma=0.95, alpha=0.1)
    assert td == float("%.14g" % (1.0 + 0.95 * 0.0 - 0.1))
    assert len(seen) == 1 and seen[0].startswith(LOCK) and "FOR UPDATE" in seen[0]
    # %.14g of 0.1 + 0.1 * 0.9 = 0.19: the script stores tostring(new_q).
    assert PgTd.query.get(name="t").q_value == Decimal("0.19")


def test_td_update_of_a_missing_record_writes_nothing(pg):
    """Redis's HSET would leave a hash holding only q_value; Postgres writes
    no row, and replies what the script replies from Q = 0 (documented)."""
    record = PgTd.create(name="gone")
    record.delete()
    assert TDValueField.td_update(record, "q_value", reward=2.0) == 2.0
    assert PgTd.query.get(name="gone") is None


def test_td_constants_never_fold_into_an_underflow(pg):
    """A subnormal reward reaches the clamped helpers' ``x / 2``; as a SQL
    literal the planner would fold that at plan time and raise
    "underflow" (the MATERIALIZED CTE keeps it a column)."""
    record = PgTd.create(name="sub", q_value=Decimal("-0.15"))
    td = TDValueField.td_update(
        record, "q_value", reward=5e-324, max_future_q=0.0, alpha=0.5, gamma=0.0
    )
    assert td == 0.15
    assert PgTd.query.get(name="sub").q_value == Decimal("-0.075")


def test_td_update_saturates_where_postgres_would_raise(pg):
    record = PgTd.create(name="big", q_value=Decimal("1E+308"))
    td = TDValueField.td_update(record, "q_value", reward=-1e308, alpha=1.0, gamma=0.0)
    assert td == -math.inf
    assert PgTd.query.get(name="big").q_value == Decimal("-Infinity")


def test_a_resolution_is_one_statement(pg, monkeypatch):
    record = PgLedger.create(name="r")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    seen = _statements(monkeypatch, pg)
    assert PredictionLedgerMixin.resolve_prediction(record, actual={"x": 0.5}) == 0.5
    writes = [s for s in seen if "UPDATE" in s]
    assert len(writes) == 1 and writes[0].startswith(LOCK)
    assert "INSERT INTO" in writes[0] and "popoto_prediction_error" in writes[0]


# -- the clamped expression ---------------------------------------------------------


@pytest.mark.parametrize(
    "cycle",
    [
        (5e-324, 1.0, 0.0),  # 2*pi*x / 5e-324 overflows: cos(inf) is NaN in C
        (1e300, 1.0, -1e300),
        (86400.0, math.inf, 0.0),
        (86400.0, 1.7e308, 0.0),
        (86400.0, 5e-324, 0.0),
        (math.nan, 1.0, 0.0),
    ],
)
def test_pathological_cycles_never_raise(pg, cycle):
    record = PgCyc.create(name="p", agent="x")
    pg.field_call(
        PgCyc._meta.spec, "relevance", "import", _rid(record), [list(cycle)], None
    )
    pg.rank_decayed(PgCyc._meta.spec, "relevance", now=time.time(), n=5)
    pg.rank_decayed(PgCyc._meta.spec, "relevance", now=0.0, n=5)


def test_pathological_pressure_never_raises(pg):
    record = PgCyc.create(name="pp", agent="x")
    for rate, last in ((1e300, -1e300), (math.inf, 0.0), (1e-300, 1e300)):
        pg.field_call(
            PgCyc._meta.spec,
            "relevance",
            "import",
            _rid(record),
            None,
            {"rate": rate, "last_resolved": last},
        )
        pg.rank_decayed(PgCyc._meta.spec, "relevance", now=time.time(), n=5)


def test_a_cyclic_ranking_ignores_the_validity_gate(pg):
    """CYCLIC_DECAY_LUA has no validity gate (TestCyclicDecayGatingGap): the
    backend accepts the argument and ranks as if it were absent."""

    class PgCycValid(popoto.Model):
        name = popoto.UniqueKeyField()
        relevance = CyclicDecayField(cycles=[(86400, 1.0, 0)])
        validity = popoto.ValidityField()

    old = PgCycValid.create(name="old")
    new = PgCycValid.create(name="new")
    from popoto.fields.supersession import SupersessionProtocol

    SupersessionProtocol.invalidate(old, superseded_by=new)
    spec = PgCycValid._meta.spec
    now = time.time()
    plain = pg.rank_decayed(spec, "relevance", now=now, n=5)
    gated = pg.rank_decayed(spec, "relevance", now=now, n=5, validity_field="validity")
    assert plain == gated and len(gated) == 2


# -- the observation batch ------------------------------------------------------------


def test_contradicted_auto_resolves_inside_the_batch_transaction(pg, monkeypatch):
    """The ledger resolution, its confidence feedback and the pressure
    auto-discharge all run on the batch's connection: with every autocommit
    statement made to fail, the batch still completes."""
    record = PgLedger.create(name="c")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    # The engine tables are created on first use (DDL, outside any
    # transaction, like M4b's); create them before the batch.
    PredictionLedgerMixin.get_highest_errors(PgLedger)
    table = pg._table(PgLedger._meta.spec).qualified
    pg._run(
        f'UPDATE {table} SET "certainty__conf" = 0.05, "certainty__n" = 20 '
        'WHERE "_pk" = %s',
        [record.db_key.redis_key],
        write=True,
    )
    original = pg._run

    def run(sql, params=(), *, uow=None, write=False):
        # First-use DDL of an engine table (the recall proposals) is not an
        # effect: it runs once, outside any transaction, under its own lock.
        ddl = sql.startswith("SELECT pg_advisory_xact_lock(hashtext(")
        if write and uow is None and not ddl:
            raise AssertionError(f"an effect ran outside the batch: {sql[:80]}")
        return original(sql, params, uow=uow, write=write)

    monkeypatch.setattr(pg, "_run", run)
    ObservationProtocol.on_context_used(
        [record], {record.db_key.redis_key: "contradicted"}
    )
    monkeypatch.undo()
    data = PredictionLedgerMixin.get_prediction_data(record)
    assert data["resolved"] is True and data["resolution_mode"] == "observed"
    assert data["prediction_error"] == pytest.approx(0.9)
    state = _state(pg, record)
    assert abs(state["pressure"]["last_resolved"] - time.time()) < 5
    # two confidence updates: the contradicted signal, then the feedback
    conf = ConfidenceField.get_confidence_data(record, "certainty")
    assert conf["evidence_count"] == 22


def test_a_rolled_back_batch_resolves_nothing(pg):
    record = PgLedger.create(name="rb")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    with pytest.raises(RuntimeError):
        with pg.transaction() as tx:
            ObservationProtocol.on_context_used(
                [record], {record.db_key.redis_key: "acted"}, pipeline=tx
            )
            raise RuntimeError("roll it back")
    assert PredictionLedgerMixin.get_prediction_data(record)["resolved"] is False
    assert PredictionLedgerMixin.get_highest_errors(PgLedger) == []
    assert _state(pg, record)["cycles"] == [[604800, 4.0, 0, 4]]


def test_crossing_batches_with_ledgers_do_not_deadlock(pg):
    records = [PgLedger.create(name=f"x{i}") for i in range(4)]
    for record in records:
        PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    errors = []

    def batch(order):
        try:
            for _ in range(5):
                ObservationProtocol.on_context_used(
                    order, {r.db_key.redis_key: "dismissed" for r in order}
                )
        except Exception as exc:  # pragma: no cover - the failure being tested
            errors.append(exc)

    threads = [
        threading.Thread(target=batch, args=(records,)),
        threading.Thread(target=batch, args=(list(reversed(records)),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(PredictionLedgerMixin.get_highest_errors(PgLedger)) == 4


def test_concurrent_td_updates_lose_nothing(pg):
    record = PgTd.create(name="race", q_value=Decimal("0"))

    def worker():
        for _ in range(25):
            TDValueField.td_update(record, "q_value", reward=1.0, alpha=1.0, gamma=0.0)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    # alpha = 1: each update sets Q to the reward; the TD error of the last
    # update of 100 serialized ones is 0 -- and every one was applied.
    assert PgTd.query.get(name="race").q_value == Decimal("1")


# -- no Redis ---------------------------------------------------------------------------


def test_the_long_tail_never_touches_redis(pg, monkeypatch):
    client = popoto.get_redis()

    def boom(*args, **kwargs):
        raise AssertionError("Redis was called for a Postgres-bound model")

    monkeypatch.setattr(client, "execute_command", boom)
    monkeypatch.setattr(client, "pipeline", boom)
    cyc = PgCyc.create(name="n", agent="x")
    cyc.strengthen_cycle("relevance")
    cyc.weaken_cycle("relevance")
    cyc.resolve_pressure("relevance")
    cyc.save()
    assert PgCyc.query.filter(agent="x").top_by_decay(n=3)
    assert PgCyc.query.filter(agent="x").composite_score({"relevance": 1.0})
    state = CyclicDecayField.export_state(cyc, "relevance", None)
    CyclicDecayField.import_state(cyc, "relevance", state)
    td = PgTd.create(name="n")
    TDValueField.td_update(td, "q_value", reward=1.0)
    led = PgLedger.create(name="n")
    PredictionLedgerMixin.record_prediction(led, predicted={"x": 1.0})
    ObservationProtocol.on_context_used([led], {led.db_key.redis_key: "acted"})
    assert PredictionLedgerMixin.get_prediction_data(led)["resolved"] is True
    assert PredictionLedgerMixin.error_summary(PgLedger)["__all__"]["count"] == 1
    exported = PredictionLedgerMixin.export_state(led)
    PredictionLedgerMixin.import_state(led, exported)


def test_the_ledger_survives_a_record_delete_as_on_redis(pg):
    """Redis keeps ``$PL:`` entries after the record is deleted (nothing
    cleans them); the engine tables are not cascaded either."""
    record = PgLedger.create(name="d")
    PredictionLedgerMixin.record_prediction(record, predicted={"x": 1.0})
    PredictionLedgerMixin.auto_resolve(record, "dismissed")
    record.delete()
    assert PredictionLedgerMixin.get_highest_errors(PgLedger) == [("PgLedger:d", 0.5)]
    assert PredictionLedgerMixin.get_prediction_data(record)["resolved"] is True


def test_validate_spec_accepts_the_long_tail():
    from popoto.backends import validate_spec

    for model in (PgCyc, PgTd, PgLedger):
        validate_spec(model._meta.spec, "postgres")
    assert get_backend(PgCyc) is not None
