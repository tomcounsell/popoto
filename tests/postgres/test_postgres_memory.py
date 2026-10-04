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
from popoto.fields.bm25_field import BM25Field
from popoto.backends.postgres.memory import decay_score_sql, lua_tostring

# The Redis and Valkey CI jobs do not install the postgres extra.
psycopg = pytest.importorskip("psycopg")

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


LOCK = "SELECT pg_advisory_xact_lock(hashtextextended("


def _body(sql):
    """A statement without its leading record-key lock (plan §6: every
    writer of a record row takes that lock first, in the same message)."""
    if sql.startswith(LOCK) or sql.startswith("SELECT count(pg_advisory_xact_lock("):
        return sql.split("; ", 1)[1]
    return sql


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


def test_partitioned_confidence_is_accepted_at_declaration():
    """Refused until #759 M3; the state lives in the row, so the partition is
    the row's own columns and there is nothing to refuse."""

    class PgPartConf(popoto.Model):
        name = popoto.KeyField()
        project = popoto.KeyField()
        certainty = ConfidenceField(partition_by="project")

        class Meta:
            backend = "postgres"

    assert PgPartConf._meta.spec.fields["certainty"].options["partition_by"] == (
        "project",
    )


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
    assert [_body(s).split()[0] for s in seen] == ["SELECT", "SELECT", "WITH"]
    assert seen[2].startswith(LOCK)  # one record staged: one key lock


def test_update_confidence_is_one_update_returning(pg, monkeypatch):
    record = PgMem.create(name="c", agent="x")
    seen = _statements(monkeypatch, pg)
    assert ConfidenceField.update_confidence(record, "certainty", 0.9) == 0.7
    assert len(seen) == 1 and seen[0].startswith(LOCK)
    assert _body(seen[0]).startswith("UPDATE") and "RETURNING" in seen[0]


def test_rank_decayed_refusals(pg):
    spec = PgMem._meta.spec
    with pytest.raises(BackendCapabilityError, match="not a ValidityField"):
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
    assert seen[0].startswith("SELECT count(pg_advisory_xact_lock(")
    assert "FOR UPDATE" in seen[0] and 'ORDER BY "_pk" COLLATE "C"' in seen[0]
    touched = [
        s for s in seen if _body(s).startswith("UPDATE") and '"relevance" =' in s
    ]
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


class PgSearchMem(popoto.Model):
    """A memory model whose save rewrites companion rows (BM25 postings), the
    shape #774's record-key lock was added for."""

    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")
    content = BM25Field(source="text")
    certainty = ConfidenceField()


def _confidence_then_save_vs_save(pg, mode, saves_only=None, monkeypatch=None):
    """#774 review, blocker 2: a caller's transaction row-locks X
    (``update_confidence``) and then saves X, while a plain save of X runs
    (``mode="auto"``, with the internal retry off so a deadlock surfaces) or
    a save of X in its own ``transaction()`` (``mode="tx"``). Returns each
    side's outcome."""
    from popoto.fields.constants import Defaults

    PgSearchMem.create(name="x", text="alpha beta")
    first = PgSearchMem.query.get(name="x")
    second = PgSearchMem.query.get(name="x")
    if monkeypatch is not None:
        monkeypatch.setattr(Defaults, "PG_TRANSACTION_RETRIES", 0)
    if saves_only is not None:
        # The control: the record-key lock on saves only (the 2d95561a code),
        # so the confidence update takes the row without the key lock.
        real = pg._record_locked

        def locked(ts, pks, sql, params):
            if "INSERT INTO" in sql:
                return real(ts, pks, sql, params)
            return sql, list(params)

        monkeypatch.setattr(pg, "_record_locked", locked)
    holding = threading.Event()
    results = {}

    def caller():
        try:
            with pg.transaction() as tx:
                ConfidenceField.update_confidence(first, "certainty", 0.9, pipeline=tx)
                holding.set()
                time.sleep(0.5)  # the other save now waits on X
                first.text = "kappa lambda"
                first.save(pipeline=tx)
            results["caller"] = "ok"
        except Exception as exc:  # noqa: BLE001 - asserted on below
            holding.set()
            results["caller"] = exc

    def other():
        holding.wait(5)
        time.sleep(0.1)
        try:
            second.text = "sigma tau"
            if mode == "tx":
                with pg.transaction() as tx:
                    second.save(pipeline=tx)
            else:
                second.save()
            results["other"] = "ok"
        except Exception as exc:  # noqa: BLE001 - asserted on below
            results["other"] = exc

    threads = [threading.Thread(target=caller), threading.Thread(target=other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.mark.parametrize("mode", ["auto", "tx"])
def test_a_confidence_update_then_save_cannot_deadlock_a_save(pg, monkeypatch, mode):
    """Every writer of a record row takes the record-key lock first (plan
    §6), so the caller's transaction already holds X's key lock when it
    saves, and the other save queues behind it instead of crossing it."""
    results = _confidence_then_save_vs_save(pg, mode, monkeypatch=monkeypatch)
    assert results == {"caller": "ok", "other": "ok"}
    record = PgSearchMem.query.get(name="x")
    assert record.text == "sigma tau"  # the queued save ran second
    assert (
        ConfidenceField.get_confidence_data(record, "certainty")["evidence_count"] == 1
    )


@pytest.mark.parametrize("mode", ["auto", "tx"])
def test_the_same_race_deadlocks_with_the_key_lock_on_saves_only(pg, monkeypatch, mode):
    """The control: with the key lock on saves only, the same interleaving
    deadlocks -- so the test above can tell the two lock orders apart."""
    results = _confidence_then_save_vs_save(
        pg, mode, saves_only=True, monkeypatch=monkeypatch
    )
    errors = [r for r in results.values() if r != "ok"]
    assert len(errors) == 1, results
    assert isinstance(errors[0], BackendRetryableError)
    assert isinstance(errors[0].__cause__, psycopg.errors.DeadlockDetected)


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


# -- B1: a rolled-back transaction surfaces as BackendRetryableError ----------


def _inject(monkeypatch, marker, error, calls):
    """Make every statement containing ``marker`` raise ``error``."""
    original = psycopg.Connection.execute

    def failing(self, query, params=None, **kwargs):
        if marker in str(query):
            calls.append(query)
            raise error("injected")
        return original(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", failing)


def test_a_real_deadlock_in_a_caller_transaction_is_a_typed_error(pg):
    """Review B1 on #773: the caller's ``transaction()`` updates B then A
    while ``on_context_used`` locks A then B. A short ``deadlock_timeout``
    in the caller's transaction makes its backend run the deadlock check
    first, so it is the victim. It must see ``BackendRetryableError`` chained
    from the driver's ``DeadlockDetected`` -- not raw psycopg -- and is not
    retried internally, while ``on_context_used`` retries and lands."""
    a = PgMem.create(name="A", agent="x")
    b = PgMem.create(name="B", agent="x")
    holding_b = threading.Event()
    results = {}

    def user_tx():
        statements = []
        try:
            with pg.transaction() as tx:
                tx.conn.execute("SET LOCAL deadlock_timeout = 50")
                ConfidenceField.update_confidence(b, "certainty", 0.9, pipeline=tx)
                statements.append("B")
                holding_b.set()
                # on_context_used now takes A and blocks on B
                time.sleep(0.5)
                ConfidenceField.update_confidence(a, "certainty", 0.9, pipeline=tx)
                statements.append("A")
            results["user"] = "ok"
        except Exception as exc:  # noqa: BLE001 - asserted on below
            results["user"] = exc
        results["statements"] = statements

    def observe():
        holding_b.wait(5)
        try:
            ObservationProtocol.on_context_used(
                [a, b], {a.db_key.redis_key: "acted", b.db_key.redis_key: "acted"}
            )
            results["observe"] = "ok"
        except Exception as exc:  # noqa: BLE001 - asserted on below
            results["observe"] = exc

    threads = [threading.Thread(target=user_tx), threading.Thread(target=observe)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    err = results["user"]
    assert isinstance(err, BackendRetryableError), repr(err)
    assert isinstance(err.__cause__, psycopg.errors.DeadlockDetected)
    assert "40P01" in str(err)
    # the victim ran once and rolled back as a unit: B's update is gone
    assert results["statements"] == ["B"]
    assert results["observe"] == "ok"
    for record in (a, b):
        data = ConfidenceField.get_confidence_data(record, "certainty")
        assert data["evidence_count"] == 1
    assert pg.health.ok and pg.health.dropped_writes == 0


@pytest.mark.parametrize(
    "error", ["DeadlockDetected", "SerializationFailure", "TransactionRollback"]
)
def test_a_rollback_inside_a_caller_transaction_is_not_retried(pg, monkeypatch, error):
    record = PgMem.create(name="s", agent="x")
    calls = []
    _inject(monkeypatch, '"certainty__n"', getattr(psycopg.errors, error), calls)
    with pytest.raises(BackendRetryableError) as info:
        with pg.transaction() as tx:
            ConfidenceField.update_confidence(record, "certainty", 0.9, pipeline=tx)
    assert type(info.value.__cause__) is getattr(psycopg.errors, error)
    assert len(calls) == 1
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_a_rollback_reported_at_commit_is_a_typed_error(pg, monkeypatch):
    """A serialization failure can be reported by the COMMIT itself."""
    original = psycopg.Transaction.__exit__

    def failing_exit(self, exc_type, exc, tb):
        result = original(self, exc_type, exc, tb)
        if exc_type is None:
            raise psycopg.errors.SerializationFailure("could not serialize")
        return result

    monkeypatch.setattr(psycopg.Transaction, "__exit__", failing_exit)
    with pytest.raises(BackendRetryableError) as info:
        with pg.transaction():
            pass
    assert isinstance(info.value.__cause__, psycopg.errors.SerializationFailure)


def test_exhausted_statement_retries_raise_a_typed_error(pg, monkeypatch):
    """Outside a unit of work a single statement keeps its bounded internal
    retry; once spent, the caller gets ``BackendRetryableError``."""
    from popoto.fields.constants import Defaults

    record = PgMem.create(name="e", agent="x")
    calls = []
    _inject(monkeypatch, '"relevance" = ', psycopg.errors.DeadlockDetected, calls)
    with pytest.raises(BackendRetryableError) as info:
        record.touch("relevance")
    assert isinstance(info.value.__cause__, psycopg.errors.DeadlockDetected)
    assert len(calls) == Defaults.PG_TRANSACTION_RETRIES + 1
    assert pg.health.ok and pg.health.dropped_writes == 0


def test_unknown_completion_is_not_a_retryable_error(pg, monkeypatch):
    """40003 (``StatementCompletionUnknown``) may have committed, so it is
    not a rollback. On a live connection a write is neither retried nor
    typed retryable: it is an outage with a dropped write (#769)."""
    from popoto.backends import BackendUnavailableError
    from popoto.backends.postgres import Health

    record = PgMem.create(name="u3", agent="x")
    # the session backend's health record is shared: count this test's
    # dropped writes on a fresh one and leave the shared one untouched
    monkeypatch.setattr(pg, "health", Health())
    calls = []
    _inject(
        monkeypatch, '"relevance" = ', psycopg.errors.StatementCompletionUnknown, calls
    )
    with pytest.raises(BackendUnavailableError) as info:
        record.touch("relevance")
    assert not isinstance(info.value, BackendRetryableError)
    assert len(calls) == 1
    assert pg.health.dropped_writes == 1
    calls.clear()
    with pytest.raises(BackendUnavailableError) as info:
        with pg.transaction() as tx:
            record.touch("relevance", pipeline=tx)
    assert not isinstance(info.value, BackendRetryableError)
    assert len(calls) == 1
