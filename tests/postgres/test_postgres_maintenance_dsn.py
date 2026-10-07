"""``[PG-only]`` the optional maintenance DSN (#800).

``POPOTO_POSTGRES_MAINTENANCE_URL`` / ``PostgresBackend(...,
maintenance_dsn=...)`` names a second DSN, to the same database, for the work
that needs a real session -- first-use DDL, ``REINDEX``/``DROP INDEX
CONCURRENTLY`` and the shared ``LISTEN`` session -- while ordinary queries
keep the pool on the main DSN.

The routing tests point both DSNs at the same server under two
``application_name``\\ s and record, server-side, which one each statement
came from: an event trigger logs every DDL command (``REINDEX CONCURRENTLY``
included) with the issuing session's ``application_name``, a row trigger
does the same for the model table's ``INSERT``\\ s, and ``pg_stat_activity``
shows the ``LISTEN`` session. None of this can pass by construction: with
the routing removed, every logged name is the main one.

The optional PgBouncer test runs only when ``POPOTO_TEST_PGBOUNCER_URL``
names a transaction-mode PgBouncer in front of the database
``POSTGRES_URL`` names.
"""

import os
import time
import uuid

import pytest

import popoto
from popoto.backends import _swap_instance, set_backend
from popoto.backends.postgres import (
    MAINTENANCE_URL_ENV,
    MaintenanceDsnMismatchError,
    PostgresBackend,
    _pools,
    backend_from_env,
)
from popoto.backends.postgres import events as events_module
from popoto.backends.postgres.listen import hub_for
from popoto.backends.postgres.schema import table_name_for

DOC_TABLE = table_name_for("DsnDoc")


class DsnDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    score = popoto.SortedField(type=float, default=0.0)


class DsnLater(popoto.Model):
    name = popoto.KeyField()
    note = popoto.Field(type=str, default="")


# -- no server: the setting's plumbing ----------------------------------------------


def test_unset_or_same_as_main_means_the_main_dsn(monkeypatch):
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    for maintenance in (None, "", "   ", "postgresql://h/db"):
        backend = PostgresBackend(dsn="postgresql://h/db", maintenance_dsn=maintenance)
        assert backend.maintenance_dsn is None
        # No connection is opened to decide it: there is nothing to verify.
        assert backend._session_dsn() == "postgresql://h/db"
        assert backend.listen_dsn == "postgresql://h/db"


def test_backend_from_env_reads_the_maintenance_url(monkeypatch):
    monkeypatch.setenv("POPOTO_POSTGRES_URL", "postgresql://pooler:6432/db")
    monkeypatch.delenv(MAINTENANCE_URL_ENV, raising=False)
    assert backend_from_env().maintenance_dsn is None
    monkeypatch.setenv(MAINTENANCE_URL_ENV, " postgresql://direct:5432/db ")
    backend = backend_from_env()
    assert backend.dsn == "postgresql://pooler:6432/db"
    assert backend.maintenance_dsn == "postgresql://direct:5432/db"
    # Constructing the backend touches no network.
    assert backend._maintenance_verified is False


def test_an_explicit_listen_url_still_wins(monkeypatch):
    backend = PostgresBackend(
        dsn="postgresql://pooler/db", maintenance_dsn="postgresql://direct/db"
    )
    monkeypatch.setenv(events_module.LISTEN_URL_ENV, "postgresql://listen/db")
    assert backend.listen_dsn == "postgresql://listen/db"


# -- two DSNs to one server -----------------------------------------------------------


def _conninfo(dsn, **extra):
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, **extra)


@pytest.fixture
def ddl_log(admin):
    """A server-side record of every DDL command in the database, with the
    issuing session's ``application_name``: ``(log table, read())``. Needs
    a superuser (event triggers); skips otherwise."""
    (superuser,) = admin.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if not superuser:
        pytest.skip("event triggers need a superuser")
    tag = uuid.uuid4().hex[:8]
    schema = f"popoto_t800_{tag}"
    trigger = f"popoto_t800_{tag}"
    admin.execute(f"CREATE SCHEMA {schema}")
    admin.execute(f"CREATE TABLE {schema}.log (app text, tag text, obj text)")
    admin.execute(
        f"CREATE FUNCTION {schema}.log_ddl() RETURNS event_trigger "
        "LANGUAGE plpgsql AS $$ DECLARE r record; BEGIN "
        "FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP "
        f"INSERT INTO {schema}.log VALUES (current_setting('application_name'), "
        "r.command_tag, r.object_identity); END LOOP; END $$"
    )
    admin.execute(
        f"CREATE FUNCTION {schema}.log_dml() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"INSERT INTO {schema}.log VALUES (current_setting('application_name'), "
        "TG_OP, TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME); RETURN NULL; END $$"
    )
    admin.execute(
        f"CREATE EVENT TRIGGER {trigger} ON ddl_command_end "
        f"EXECUTE FUNCTION {schema}.log_ddl()"
    )

    def read(pg_schema_name):
        return admin.execute(
            f"SELECT app, tag, obj FROM {schema}.log WHERE obj LIKE %s "
            "ORDER BY ctid",
            (pg_schema_name + ".%",),
        ).fetchall()

    try:
        yield schema, read
    finally:
        admin.execute(f"DROP EVENT TRIGGER IF EXISTS {trigger}")
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _close_pool(dsn):
    pool = _pools.pop((dsn, os.getpid()), None)
    if pool is not None:
        pool.close()


@pytest.fixture
def split(pg, admin, monkeypatch):
    """``(backend, main app, maintenance app)``: a backend whose main and
    maintenance DSNs reach the same database under two application names,
    bound as the process default for the test."""
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    tag = uuid.uuid4().hex[:8]
    main_app, maint_app = f"popoto_t800_main_{tag}", f"popoto_t800_maint_{tag}"
    main = _conninfo(pg.dsn, application_name=main_app)
    maint = _conninfo(pg.dsn, application_name=maint_app)
    backend = PostgresBackend(dsn=main, schema=pg.schema, maintenance_dsn=maint)
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend, main_app, maint_app
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        hub_for(maint).close()
        _close_pool(main)
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name IN (%s, %s)",
            (main_app, maint_app),
        )


def test_first_use_ddl_runs_on_the_maintenance_dsn_and_queries_on_the_main(
    split, ddl_log, admin, pg
):
    backend, main_app, maint_app = split
    log_schema, read = ddl_log

    DsnDoc(name="a", score=1.0).save()
    created = read(pg.schema)
    assert created, "the first save created the table"
    assert {app for app, _, _ in created} == {maint_app}
    assert backend._maintenance_verified is True

    # Ordinary writes ride the pool on the main DSN.
    admin.execute(
        f"CREATE TRIGGER t800_dml AFTER INSERT ON {pg.schema}.{DOC_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {log_schema}.log_dml()"
    )
    DsnDoc(name="b", score=2.0).save()
    inserts = [row for row in read(pg.schema) if row[1] == "INSERT"]
    assert inserts == [(main_app, "INSERT", f"{pg.schema}.{DOC_TABLE}")]
    assert DsnDoc.query.get(name="b").score == 2.0

    # First use inside an open unit: DDL still on the maintenance DSN, the
    # unit's own writes on the main one.
    before = len(read(pg.schema))
    with backend.transaction() as tx:
        DsnLater(name="x", note="n").save(pipeline=tx)
    later = read(pg.schema)[before:]
    assert later and {app for app, _, _ in later} == {maint_app}
    assert DsnLater.query.get(name="x").note == "n"
    assert backend.health.dropped_writes == 0


def test_rebuild_indexes_reindexes_on_the_maintenance_dsn(split, ddl_log, pg):
    backend, main_app, maint_app = split
    _, read = ddl_log
    DsnDoc(name="a", score=1.0).save()
    before = len(read(pg.schema))
    DsnDoc.rebuild_indexes()
    reindexed = [row for row in read(pg.schema)[before:] if row[1] == "REINDEX"]
    assert reindexed, "rebuild_indexes() ran REINDEX"
    assert {app for app, _, _ in reindexed} == {maint_app}


def test_the_listen_session_uses_the_maintenance_dsn(split, admin):
    backend, main_app, maint_app = split
    assert backend.listen_dsn == backend.maintenance_dsn
    sub = backend.pubsub()
    try:
        sub.subscribe("dsn800")
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            admin.execute("SELECT pg_stat_clear_snapshot()")
            rows = admin.execute(
                "SELECT application_name FROM pg_stat_activity "
                "WHERE query LIKE 'LISTEN %%' AND application_name IN (%s, %s)",
                (main_app, maint_app),
            ).fetchall()
            if rows:
                break
            time.sleep(0.02)
        assert rows == [(maint_app,)]
    finally:
        sub.close()


@pytest.fixture
def other_database(pg):
    """A DSN to a *different* database on the same server, read-only so
    that even a broken refusal could not run DDL there:
    ``POPOTO_TEST_OTHER_PG_DSN`` when set, else ``template1``."""
    other = os.environ.get("POPOTO_TEST_OTHER_PG_DSN", "").strip()
    if not other:
        other = _conninfo(pg.dsn, dbname="template1")
    return _conninfo(
        other,
        password="s3cret-800",
        options="-c default_transaction_read_only=on",
    )


def test_a_maintenance_dsn_to_another_database_is_refused(pg, other_database, admin):
    backend = PostgresBackend(
        dsn=pg.dsn, schema=pg.schema, maintenance_dsn=other_database
    )
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        with pytest.raises(MaintenanceDsnMismatchError) as caught:
            DsnDoc(name="a").save()
        with pytest.raises(MaintenanceDsnMismatchError):
            backend.listen_dsn
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
    message = str(caught.value)
    assert "database" in message and "s3cret-800" not in message
    assert isinstance(caught.value, ValueError)
    assert backend._maintenance_verified is False
    # Not an outage, and nothing was created anywhere.
    assert backend.health.ok and backend.health.dropped_writes == 0
    (exists,) = admin.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"{pg.schema}.{DOC_TABLE}",)
    ).fetchone()
    assert not exists


# -- optional: a real transaction-mode PgBouncer -------------------------------------


@pytest.fixture
def bouncer(pg, admin, monkeypatch):
    url = os.environ.get("POPOTO_TEST_PGBOUNCER_URL", "").strip()
    if not url:
        pytest.skip("POPOTO_TEST_PGBOUNCER_URL is unset (a transaction-mode PgBouncer)")
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    tag = uuid.uuid4().hex[:8]
    maint_app = f"popoto_t800_direct_{tag}"
    maint = _conninfo(pg.dsn, application_name=maint_app)
    backend = PostgresBackend(dsn=url, schema=pg.schema, maintenance_dsn=maint)
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend, maint_app
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        hub_for(maint).close()
        _close_pool(url)
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s",
            (maint_app,),
        )


def test_behind_transaction_mode_pgbouncer(bouncer, ddl_log, pg):
    backend, maint_app = bouncer
    _, read = ddl_log
    for i in range(5):
        DsnDoc(name=f"d{i}", score=float(i)).save()
    assert [d.name for d in DsnDoc.query.filter(order_by="score")] == [
        f"d{i}" for i in range(5)
    ]
    DsnDoc.rebuild_indexes()
    ddl = read(pg.schema)
    assert any(tag == "REINDEX" for _, tag, _ in ddl)
    assert {app for app, _, _ in ddl} == {maint_app}

    sub = backend.pubsub()
    try:
        sub.subscribe("dsn800")
        backend.publish("dsn800", "hello")
        deadline = time.monotonic() + 5
        got = None
        while time.monotonic() < deadline and got is None:
            message = sub.get_message(timeout=0.1)
            if message is not None and message["type"] == "message":
                got = message["data"]
        assert got in ("hello", b"hello")
    finally:
        sub.close()
