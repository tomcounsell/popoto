"""``[PG-only]`` :meth:`PostgresUnitOfWork.savepoint` and the ``uow=``
argument of :func:`popoto.transfer.import_records` and the fields'
``import_state`` (#794).

The migration tool (#756) lands every record with
``import_records(uow=...)``, each under a savepoint of the batch's unit, and
was the only thing exercising either. These tests pin the contract directly:

* a savepoint that raises rolls back its statements **and** drops what it
  queued on the unit -- stream appends, ``before_commit`` and
  ``after_commit`` callbacks, TTL tables to reap -- and nothing queued
  outside it;
* nesting composes (an inner rollback keeps the outer's work; an outer
  rollback drops a released inner one), a savepoint after a released one
  works, and the unit stays usable after an SQL error inside one;
* a ``BaseException`` (``KeyboardInterrupt``) is handled the same way;
* it works on the unit a ``popoto.batch()`` holds as well as on a
  ``backend.transaction()``;
* ``import_records(uow=...)`` refuses a Redis-bound model, an object that is
  not a Postgres unit, and ``preserve_keys=False``, with messages naming
  the fix, before it reads anything.
"""

import io
import logging

import pytest

psycopg = pytest.importorskip("psycopg")

import popoto  # noqa: E402
from popoto.batch import unit_of  # noqa: E402
from popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from popoto.transfer import export_records, import_records  # noqa: E402


class UwItem(EventStreamMixin, popoto.Model):
    _stream_name = "uw_items"

    name = popoto.UniqueKeyField()
    n = popoto.IntField(default=0)
    trust = popoto.ConfidenceField()


class UwTtl(popoto.Model):
    name = popoto.UniqueKeyField()

    class Meta:
        ttl = 3600


class UwOnRedis(popoto.Model):
    name = popoto.UniqueKeyField()

    class Meta:
        backend = "redis"


class Boom(Exception):
    pass


def _names(admin, schema):
    return sorted(
        r[0] for r in admin.execute(f'SELECT name FROM "{schema}".uw_item').fetchall()
    )


def _events(admin, schema):
    """The record names of every save event on ``UwItem``'s stream, in
    append order."""
    found = admin.execute(
        "SELECT to_regclass(%s)", (f'"{schema}".popoto_stream_entry',)
    ).fetchone()[0]
    if found is None:
        return []
    out = []
    for (fields,) in admin.execute(
        f'SELECT fields FROM "{schema}".popoto_stream_entry '
        "WHERE stream = 'stream:uw_items' ORDER BY ms, seq"
    ).fetchall():
        flat = [bytes(f) for f in fields]
        entry = dict(zip(flat[0::2], flat[1::2]))
        out.append(entry[b"pk"].decode().rsplit(":", 1)[-1])
    return out


@pytest.fixture
def db(pg, admin):
    """The bound backend, plus readers of the committed state. ``UwItem``'s
    tables exist before each test's unit opens (a first save creates them,
    and DDL inside a test's unit is not what is under test)."""
    UwItem(name="warm").save()
    UwItem.query.get(name="warm").delete()

    def clear_events():
        admin.execute(f'DELETE FROM "{pg.schema}".popoto_stream_entry')
        admin.commit()

    clear_events()

    class State:
        backend = pg
        clear = staticmethod(clear_events)

        @staticmethod
        def names():
            out = _names(admin, pg.schema)
            admin.commit()
            return out

        @staticmethod
        def events():
            out = _events(admin, pg.schema)
            admin.commit()
            return out

    return State


def _queued(uow):
    return (len(uow._after_commit), len(uow._before_commit), len(uow._stream_appends))


# -- PostgresUnitOfWork.savepoint() ------------------------------------------------


def test_a_rolled_back_savepoint_drops_only_what_it_queued(db):
    called = []
    with db.backend.transaction() as uow:
        UwItem(name="a1").save(pipeline=uow)
        uow.after_commit(lambda: called.append("after a1"))
        uow.before_commit(lambda: called.append("before a1"))
        before = _queued(uow)
        with pytest.raises(Boom):
            with uow.savepoint() as inner:
                assert inner is uow
                UwItem(name="a2").save(pipeline=uow)
                uow.after_commit(lambda: called.append("after a2"))
                uow.before_commit(lambda: called.append("before a2"))
                assert _queued(uow) > before
                raise Boom
        assert _queued(uow) == before
    assert db.names() == ["a1"]
    assert db.events() == ["a1"]
    assert called == ["before a1", "after a1"]


def test_a_released_savepoint_commits_with_the_unit(db):
    called = []
    with db.backend.transaction() as uow:
        with uow.savepoint():
            UwItem(name="r1").save(pipeline=uow)
            uow.after_commit(lambda: called.append("r1"))
        assert called == []  # not before COMMIT
    assert db.names() == ["r1"]
    assert db.events() == ["r1"]
    assert called == ["r1"]


def test_a_nested_rollback_keeps_the_outer_savepoints_work(db):
    called = []
    with db.backend.transaction() as uow:
        with uow.savepoint():
            UwItem(name="b1").save(pipeline=uow)
            uow.after_commit(lambda: called.append("b1"))
            with pytest.raises(Boom):
                with uow.savepoint():
                    UwItem(name="b2").save(pipeline=uow)
                    uow.after_commit(lambda: called.append("b2"))
                    raise Boom
            UwItem(name="b3").save(pipeline=uow)
            uow.after_commit(lambda: called.append("b3"))
    assert db.names() == ["b1", "b3"]
    assert db.events() == ["b1", "b3"]
    assert called == ["b1", "b3"]


def test_an_outer_rollback_drops_a_released_inner_savepoint(db):
    called = []
    with db.backend.transaction() as uow:
        UwItem(name="c0").save(pipeline=uow)
        with pytest.raises(Boom):
            with uow.savepoint():
                UwItem(name="c1").save(pipeline=uow)
                with uow.savepoint():
                    UwItem(name="c2").save(pipeline=uow)
                    uow.after_commit(lambda: called.append("c2"))
                raise Boom
    assert db.names() == ["c0"]
    assert db.events() == ["c0"]
    assert called == []


def test_a_savepoint_after_a_released_one(db):
    called = []
    with db.backend.transaction() as uow:
        with uow.savepoint():
            UwItem(name="d1").save(pipeline=uow)
            uow.after_commit(lambda: called.append("d1"))
        with pytest.raises(Boom):
            with uow.savepoint():
                UwItem(name="d2").save(pipeline=uow)
                uow.after_commit(lambda: called.append("d2"))
                raise Boom
        with uow.savepoint():
            UwItem(name="d3").save(pipeline=uow)
            uow.after_commit(lambda: called.append("d3"))
    assert db.names() == ["d1", "d3"]
    assert db.events() == ["d1", "d3"]
    assert called == ["d1", "d3"]


def test_the_same_key_saves_again_after_a_rolled_back_savepoint(db):
    with db.backend.transaction() as uow:
        with pytest.raises(Boom):
            with uow.savepoint():
                UwItem(name="k1", n=1).save(pipeline=uow)
                raise Boom
        UwItem(name="k1", n=2).save(pipeline=uow)
    assert UwItem.query.get(name="k1").n == 2
    assert db.events() == ["k1"]


def test_a_raising_after_commit_callback_is_logged_and_the_rest_run(db, caplog):
    called = []

    def broken():
        raise RuntimeError("callback boom")

    with caplog.at_level(logging.WARNING, logger="popoto.backends.postgres"):
        with db.backend.transaction() as uow:
            with uow.savepoint():
                UwItem(name="e1").save(pipeline=uow)
                uow.after_commit(broken)
                uow.after_commit(lambda: called.append("e1"))
    assert db.names() == ["e1"]  # committed before the callbacks ran
    assert called == ["e1"]
    assert "callback boom" in caplog.text


def test_a_raising_before_commit_callback_rolls_the_whole_unit_back(db):
    def broken():
        raise RuntimeError("before-commit boom")

    with pytest.raises(RuntimeError, match="before-commit boom"):
        with db.backend.transaction() as uow:
            UwItem(name="f0").save(pipeline=uow)
            with uow.savepoint():
                UwItem(name="f1").save(pipeline=uow)
                uow.before_commit(broken)
    assert db.names() == []
    assert db.events() == []


def test_a_callback_raising_inside_the_savepoint_rolls_it_back(db):
    """An exception raised by the block itself -- here a ``before_commit``
    callback the block calls directly -- is the block's exception: the
    savepoint rolls back and the unit carries on."""
    with db.backend.transaction() as uow:
        UwItem(name="g0").save(pipeline=uow)
        with pytest.raises(Boom):
            with uow.savepoint():
                UwItem(name="g1").save(pipeline=uow)

                def callback():
                    raise Boom

                uow.before_commit(callback)
                uow._run_before_commit()
        UwItem(name="g2").save(pipeline=uow)
    assert db.names() == ["g0", "g2"]
    assert db.events() == ["g0", "g2"]


def test_an_sql_error_inside_a_savepoint_leaves_the_unit_usable(db):
    with db.backend.transaction() as uow:
        UwItem(name="i1").save(pipeline=uow)
        with pytest.raises(psycopg.errors.DivisionByZero):
            with uow.savepoint():
                UwItem(name="i2").save(pipeline=uow)
                uow.conn.execute("SELECT 1/0")
        UwItem(name="i3").save(pipeline=uow)
    assert db.names() == ["i1", "i3"]
    assert db.events() == ["i1", "i3"]


def test_keyboard_interrupt_in_a_savepoint_rolls_the_unit_back(db):
    with pytest.raises(KeyboardInterrupt):
        with db.backend.transaction() as uow:
            UwItem(name="j1").save(pipeline=uow)
            with uow.savepoint():
                UwItem(name="j2").save(pipeline=uow)
                raise KeyboardInterrupt
    assert db.names() == []
    assert db.events() == []


def test_keyboard_interrupt_caught_inside_the_unit_drops_only_the_savepoint(db):
    """``savepoint()`` handles a ``BaseException`` like any other: the
    savepoint's work and callbacks go, the unit's stay."""
    called = []
    with db.backend.transaction() as uow:
        UwItem(name="m1").save(pipeline=uow)
        uow.after_commit(lambda: called.append("m1"))
        with pytest.raises(KeyboardInterrupt):
            with uow.savepoint():
                UwItem(name="m2").save(pipeline=uow)
                uow.after_commit(lambda: called.append("m2"))
                raise KeyboardInterrupt
    assert db.names() == ["m1"]
    assert db.events() == ["m1"]
    assert called == ["m1"]


def test_a_rolled_back_savepoint_forgets_the_ttl_tables_it_wrote(db):
    with db.backend.transaction() as uow:
        UwTtl(name="warm").save(pipeline=uow)  # creates the table
    UwTtl.query.get(name="warm").delete()
    with db.backend.transaction() as uow:
        with pytest.raises(Boom):
            with uow.savepoint():
                UwTtl(name="t1").save(pipeline=uow)
                assert uow.reap
                raise Boom
        assert uow.reap == {}
        with uow.savepoint():
            UwTtl(name="t2").save(pipeline=uow)
        assert len(uow.reap) == 1
    assert UwTtl.query.get(name="t1") is None
    assert UwTtl.query.get(name="t2") is not None


def test_a_savepoint_inside_popoto_batch(db):
    called = []
    with popoto.batch() as pipe:
        UwItem(name="h1").save(pipeline=pipe)
        uow = unit_of(pipe, db.backend)
        assert uow is not None and hasattr(uow, "savepoint")
        with pytest.raises(Boom):
            with uow.savepoint():
                UwItem(name="h2").save(pipeline=pipe)
                uow.after_commit(lambda: called.append("h2"))
                raise Boom
        with uow.savepoint():
            UwItem(name="h3").save(pipeline=pipe)
            uow.after_commit(lambda: called.append("h3"))
        assert db.names() == []  # nothing commits before execute()
        pipe.execute()
    assert db.names() == ["h1", "h3"]
    assert db.events() == ["h1", "h3"]
    assert called == ["h3"]


def test_a_released_savepoint_inside_a_reset_batch_commits_nothing(db):
    called = []
    pipe = popoto.batch()
    UwItem(name="x1").save(pipeline=pipe)
    uow = unit_of(pipe, db.backend)
    with uow.savepoint():
        UwItem(name="x2").save(pipeline=pipe)
        uow.after_commit(lambda: called.append("x2"))
    pipe.reset()
    assert db.names() == []
    assert db.events() == []
    assert called == []


# -- import_records(uow=...) and import_state(uow=...) ---------------------------


def _export_items(db, *names):
    for name in names:
        UwItem(name=name).save()
        ConfidenceField.update_confidence(UwItem.query.get(name=name), "trust", 0.9)
    text = export_records(UwItem).data
    for name in names:
        UwItem.query.get(name=name).delete()
    db.clear()  # the export's own saves and deletes are not under test
    return text


def test_import_records_refuses_a_redis_bound_model(db):
    with db.backend.transaction() as uow:
        with pytest.raises(ValueError) as refused:
            import_records(UwOnRedis, io.StringIO(""), uow=uow)
    assert str(refused.value) == (
        "uow= takes a Postgres unit of work (backend.transaction()) for a "
        "model bound to that backend"
    )


def test_import_records_refuses_a_uow_that_is_not_a_postgres_unit(db):
    stream = io.StringIO("not read")
    with pytest.raises(ValueError, match=r"takes a Postgres unit of work"):
        import_records(UwItem, stream, uow=object())
    assert stream.tell() == 0  # refused before reading a line


def test_import_records_refuses_uow_without_preserved_keys(db):
    stream = io.StringIO("not read")
    with db.backend.transaction() as uow:
        with pytest.raises(ValueError) as refused:
            import_records(UwItem, stream, uow=uow, preserve_keys=False)
    assert str(refused.value) == "uow= is supported with preserve_keys=True only"
    assert stream.tell() == 0


def test_import_records_lands_in_the_unit_and_rolls_back_with_it(db):
    text = _export_items(db, "p1", "p2")
    with pytest.raises(Boom):
        with db.backend.transaction() as uow:
            report = import_records(UwItem, io.StringIO(text), uow=uow)
            assert report.count("landed") == 2, report.summary()
            raise Boom
    assert db.names() == []
    with db.backend.transaction() as uow:
        report = import_records(UwItem, io.StringIO(text), uow=uow)
        assert db.names() == []  # not before COMMIT
    assert report.count("landed") == 2, report.summary()
    assert db.names() == ["p1", "p2"]
    data = ConfidenceField.get_confidence_data(UwItem.query.get(name="p1"), "trust")
    assert data["evidence_count"] == 1


def test_a_record_whose_state_fails_is_rolled_back_whole_in_the_unit(db):
    """No ``partial`` outcome under ``uow=``: the record's save, its carried
    state and its stream append go together."""
    import json

    text = _export_items(db, "q1", "q2")
    lines = text.splitlines()
    broken = json.loads(lines[2])
    broken["state"]["no_such_field"] = {}
    lines[2] = json.dumps(broken)
    with db.backend.transaction() as uow:
        report = import_records(UwItem, io.StringIO("\n".join(lines) + "\n"), uow=uow)
    assert report.count("landed") == 1, report.summary()
    assert report.count("errored") == 1, report.summary()
    (errored,) = [o for o in report.outcomes if o.category == "errored"]
    assert "Rolled back: nothing of this record was written" in errored.reason
    (landed,) = [o.key for o in report.outcomes if o.category == "landed"]
    assert db.names() == [landed.rsplit(":", 1)[-1]]
    assert db.events() == [landed.rsplit(":", 1)[-1]]


def test_import_state_joins_the_unit_and_its_savepoint(db):
    item = UwItem(name="s1")
    item.save()
    state = {
        "confidence": 0.75,
        "evidence_count": 4,
        "corroborations": 3,
        "contradictions": 1,
    }
    with db.backend.transaction() as uow:
        with pytest.raises(Boom):
            with uow.savepoint():
                ConfidenceField.import_state(item, "trust", state, uow=uow)
                raise Boom
    assert ConfidenceField.get_confidence_data(item, "trust")["evidence_count"] == 0
    with pytest.raises(Boom):
        with db.backend.transaction() as uow:
            ConfidenceField.import_state(item, "trust", state, uow=uow)
            raise Boom
    assert ConfidenceField.get_confidence_data(item, "trust")["evidence_count"] == 0
    with db.backend.transaction() as uow:
        with uow.savepoint():
            ConfidenceField.import_state(item, "trust", state, uow=uow)
    data = ConfidenceField.get_confidence_data(item, "trust")
    assert data["evidence_count"] == 4 and data["confidence"] == pytest.approx(0.75)
