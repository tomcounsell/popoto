"""``[PG-only]`` behaviour of the Postgres backend (#759 M1b).

The model-level parity is the conformance suite (gate (b)); this file covers
what has no Redis counterpart: the typed table and its engine columns, the
``popoto_schema`` record and drift refusals, the documented divergences
(``migrate_key``, NUL, atomic units of work), unique-constraint mapping, the
#573 quarantine guard on the Postgres write, and the Redis-only APIs refusing
rather than reading Redis.
"""

import datetime
import json

import pytest

import popoto
from popoto.backends import (
    BackendCapabilityError,
    SchemaDriftError,
    get_backend,
    reset_bindings,
)
from popoto.backends.postgres import PostgresBackend
from popoto.backends.postgres.schema import SCHEMA_FORMAT_VERSION, table_name_for
from popoto.exceptions import CorruptFieldError, ModelException
from popoto.fields.shortcuts import UniqueKeyField


class PgNote(popoto.Model):
    owner = popoto.KeyField()
    slug = popoto.KeyField()
    hits = popoto.IntField(default=0)
    score = popoto.SortedField(type=float, default=0.0)
    body = popoto.StringField(null=True)
    seen = popoto.DatetimeField(null=True)


class PgAccount(popoto.Model):
    username = popoto.KeyField()
    email = UniqueKeyField()


def _columns(admin, schema, table):
    rows = admin.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchall()
    return dict(rows)


# -- the typed table ----------------------------------------------------------


def test_typed_table_with_engine_columns(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s", hits=1, score=2.0)
    cols = _columns(admin, pg_schema.name, "pg_note")
    assert cols == {
        "_pk": "text",
        "owner": "text",
        "slug": "text",
        "hits": "bigint",
        "score": "double precision",
        "body": "text",
        "seen": "timestamp with time zone",
        "seen__utcoff": "integer",
        "_created_at": "timestamp with time zone",
        "_updated_at": "timestamp with time zone",
    }
    (pk,) = admin.execute(f'SELECT _pk FROM "{pg_schema.name}".pg_note').fetchone()
    assert pk == PgNote.query.get(owner="o", slug="s").pk == "PgNote:o:s"


def test_updated_at_moves_and_created_at_does_not(pg, pg_schema, admin):
    note = PgNote.create(owner="o", slug="s")
    table = f'"{pg_schema.name}".pg_note'
    created, updated = admin.execute(
        f"SELECT _created_at, _updated_at FROM {table}"
    ).fetchone()
    note.hits = 5
    note.save()
    created2, updated2 = admin.execute(
        f"SELECT _created_at, _updated_at FROM {table}"
    ).fetchone()
    assert created2 == created
    assert updated2 > updated


def test_indexes_for_keys_and_sorted_fields(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s")
    defs = [
        row[0]
        for row in admin.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (pg_schema.name, "pg_note"),
        ).fetchall()
    ]
    assert any("UNIQUE" in d and "(owner, slug)" in d for d in defs), defs
    assert any("(slug)" in d and "UNIQUE" not in d for d in defs), defs
    assert any('(score, _pk COLLATE "C")' in d for d in defs), defs


def test_datetime_offset_round_trips(pg):
    plus7 = datetime.timezone(datetime.timedelta(hours=7))
    aware = datetime.datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=plus7)
    naive = datetime.datetime(2026, 1, 2, 3, 4, 5, 6)
    PgNote.create(owner="a", slug="aware", seen=aware)
    PgNote.create(owner="a", slug="naive", seen=naive)
    got_aware = PgNote.query.get(owner="a", slug="aware").seen
    got_naive = PgNote.query.get(owner="a", slug="naive").seen
    assert got_aware == aware and got_aware.utcoffset() == aware.utcoffset()
    assert got_naive == naive and got_naive.tzinfo is None


# -- popoto_schema, DDL once per process, drift --------------------------------


def _record(admin, schema, table):
    return admin.execute(
        f'SELECT fingerprint, columns, format_version FROM "{schema}".popoto_schema '
        "WHERE table_name = %s",
        (table,),
    ).fetchone()


def test_schema_record_written_once(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s")
    fingerprint, columns, fmt = _record(admin, pg_schema.name, "pg_note")
    assert fmt == SCHEMA_FORMAT_VERSION
    assert columns["hits"] == "bigint"
    # A second bind in the same process does no DDL: the record is untouched.
    reset_bindings([PgNote])
    PgNote.query.count()
    assert _record(admin, pg_schema.name, "pg_note")[0] == fingerprint


def test_newer_schema_format_refuses_to_write(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s")
    admin.execute(
        f'UPDATE "{pg_schema.name}".popoto_schema SET format_version = %s',
        (SCHEMA_FORMAT_VERSION + 1,),
    )
    pg.forget_tables()
    reset_bindings([PgNote])
    with pytest.raises(SchemaDriftError, match="Upgrade popoto"):
        PgNote.create(owner="o", slug="t")


def test_a_column_the_model_lacks_is_drift(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s")
    record = _record(admin, pg_schema.name, "pg_note")
    columns = dict(record[1], extra="text")
    admin.execute(
        f'UPDATE "{pg_schema.name}".popoto_schema SET columns = %s, fingerprint = %s',
        (json.dumps(columns), "someone-else"),
    )
    pg.forget_tables()
    reset_bindings([PgNote])
    with pytest.raises(SchemaDriftError, match="extra"):
        PgNote.query.count()


def test_additive_change_is_applied(pg, pg_schema, admin):
    PgNote.create(owner="o", slug="s")
    table = f'"{pg_schema.name}".pg_note'
    admin.execute(f"ALTER TABLE {table} DROP COLUMN body")
    record = _record(admin, pg_schema.name, "pg_note")
    columns = {k: v for k, v in record[1].items() if k != "body"}
    admin.execute(
        f'UPDATE "{pg_schema.name}".popoto_schema SET columns = %s, fingerprint = %s',
        (json.dumps(columns), "older"),
    )
    pg.forget_tables()
    reset_bindings([PgNote])
    PgNote.create(owner="o", slug="t", body="back")
    assert PgNote.query.get(owner="o", slug="t").body == "back"
    assert "body" in _columns(admin, pg_schema.name, "pg_note")


def test_schema_auto_off_refuses_to_create(pg, monkeypatch):
    monkeypatch.setenv("POPOTO_SCHEMA_AUTO", "0")
    with pytest.raises(SchemaDriftError, match="POPOTO_SCHEMA_AUTO=0"):
        PgNote.query.count()


def test_a_table_popoto_did_not_create_is_not_adopted(pg, pg_schema, admin):
    admin.execute(f'CREATE TABLE "{pg_schema.name}".pg_note (_pk text)')
    with pytest.raises(SchemaDriftError, match="will not adopt"):
        PgNote.query.count()


def test_same_name_models_in_two_modules_bind_separately(pg):
    """#768 review: the bind memo is keyed by class, so a second class with
    the same name is checked against the stored schema instead of riding on
    the first one's binding."""

    def declare(module, **fields):
        attrs = {"__module__": module, "key": popoto.KeyField(), **fields}
        return type("PgTwin", (popoto.Model,), attrs)

    first = declare("pkg.one", a=popoto.IntField(default=0))
    first.create(key="x", a=1)
    second = declare("pkg.two", b=popoto.StringField(null=True))
    assert get_backend(first) is pg
    # Bound on its own (not through first's memo), so the stored schema is
    # checked -- and it is first's.
    with pytest.raises(SchemaDriftError, match="column a"):
        second.query.count()
    assert first.query.count() == 1


# -- divergences and contracts --------------------------------------------------


def test_migrate_key_raises(pg):
    note = PgNote.create(owner="o", slug="old")
    note.slug = "new"
    with pytest.raises(BackendCapabilityError, match="migrate_key"):
        note.save(migrate_key=True)
    assert PgNote.query.get(owner="o", slug="old") is not None
    assert PgNote.query.get(owner="o", slug="new") is None


def test_nul_in_text_is_refused(pg):
    with pytest.raises(ValueError, match="NUL"):
        PgNote.create(owner="o", slug="s", body="a\x00b")
    assert PgNote.query.count() == 0


def test_transaction_is_atomic(pg):
    with pytest.raises(RuntimeError):
        with pg.transaction() as uow:
            assert bool(uow) is True
            PgNote(owner="o", slug="one").save(pipeline=uow)
            PgNote(owner="o", slug="two").save(pipeline=uow)
            raise RuntimeError("roll it back")
    assert PgNote.query.count() == 0
    with pg.transaction() as uow:
        PgNote(owner="o", slug="one").save(pipeline=uow)
        PgNote(owner="o", slug="two").save(pipeline=uow)
    assert PgNote.query.count() == 2


def test_bulk_create_is_one_transaction(pg):
    good = PgAccount(username="a", email="same@x")
    dup = PgAccount(username="b", email="same@x")
    with pytest.raises(ModelException):
        PgAccount.bulk_create([good, dup])
    assert PgAccount.query.count() == 0


def test_unique_key_field_conflict_is_a_model_exception(pg):
    PgAccount.create(username="a", email="e@x")
    with pytest.raises(
        ModelException,
        match="Unique constraint violated: email=e@x already exists on another",
    ):
        PgAccount.create(username="b", email="e@x")
    assert PgAccount(username="b", email="e@x").save(ignore_errors=True) is False


def test_quarantine_guard_blocks_the_postgres_write(pg):
    """#573 on the Postgres path: a field whose stored value could not be
    decoded must not be overwritten by a save that writes it."""
    note = PgNote.create(owner="o", slug="s", body="kept")
    loaded = PgNote.query.get(owner="o", slug="s")
    loaded._corrupt_fields["body"] = b"\xff"
    with pytest.raises(CorruptFieldError):
        loaded.save()
    loaded.save(update_fields=["hits"])  # not writing body is allowed
    assert PgNote.query.get(owner="o", slug="s").body == note.body


def test_increment_returns_and_persists(pg):
    note = PgNote.create(owner="o", slug="s", hits=1, score=1.5)
    assert note.atomic_increment("hits", 4) == 5
    assert note.atomic_increment("score", 0.5) == 2.0
    fresh = PgNote.query.get(owner="o", slug="s")
    assert (fresh.hits, fresh.score) == (5, 2.0)


def test_unknown_filter_operator_is_refused(pg):
    """#768 review: ``name__bogus`` is an unknown operator, not an exact match
    on a field named ``name__bogus`` -- same refusal as Redis."""
    from popoto.models.query import QueryException

    with pytest.raises(QueryException, match="Invalid filter parameters"):
        PgNote.query.filter(slug__bogus="x").all()
    with pytest.raises(QueryException, match="Invalid filter parameters"):
        PgNote.query.count(nope=1)


@pytest.mark.parametrize(
    "call",
    [
        lambda: PgNote.load_raw_hash("PgNote:o:s"),
        lambda: PgNote.idle_seconds("PgNote:o:s"),
        lambda: PgNote.query.keys(catchall=True),
        lambda: PgNote.query.keys(clean=True),
    ],
    ids=["load_raw_hash", "idle_seconds", "keys_catchall", "keys_clean"],
)
def test_redis_only_apis_refuse_instead_of_reading_redis(pg, call):
    with pytest.raises(BackendCapabilityError):
        call()


def test_later_groups_raise_capability_error(pg):
    # touch / rank_decayed arrived in M2a (tests/postgres/test_postgres_memory.py),
    # supersede / chain in M3 (tests/postgres/test_postgres_validity.py).
    for method in ("graph_expand", "maintain"):
        with pytest.raises(BackendCapabilityError, match="arrives in"):
            getattr(pg, method)(PgNote._meta.spec, "score")


def test_hydrated_instance_matches_a_fresh_one(pg):
    PgNote.create(owner="o", slug="s", hits=3, body="b")
    loaded = PgNote.query.get(owner="o", slug="s")
    assert loaded == PgNote(owner="o", slug="s")
    assert loaded._is_persisted and loaded._redis_key == "PgNote:o:s"
    assert loaded._saved_field_values["hits"] == 3


def test_table_name_mapping():
    assert table_name_for("PgNote") == "pg_note"
    assert table_name_for("HTTPRequestLog") == "http_request_log"
    assert len(table_name_for("X" * 100)) <= 63


def test_backend_instance_holds_no_connection_until_used():
    backend = PostgresBackend(dsn="postgresql://127.0.0.1:1/never", schema="s")
    assert backend.health.ok and backend._tables == {}
