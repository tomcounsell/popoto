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
import socket
import threading
import time
import uuid

import pytest

import popoto
from popoto.backends import BackendUnavailableError, _swap_instance, set_backend
from popoto.backends.postgres import (
    GRANT_DOCS_URL,
    GRANT_MAIN_ROLE_ENV,
    MAINTENANCE_URL_ENV,
    MainRolePermissionError,
    MaintenanceConnectionError,
    MaintenanceDsnMismatchError,
    PostgresBackend,
    _pools,
    backend_from_env,
)
from popoto.backends.postgres import events as events_module
from popoto.backends.postgres.listen import hub_for
from popoto.backends.postgres.schema import (
    POPOTO_SCHEMA_TABLE,
    catalog_before,
    grant_created,
    table_name_for,
)

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


def test_a_dsn_differing_only_in_order_or_password_is_the_main_dsn(monkeypatch):
    # Parsing a DSN takes the driver; without it (where no connection could
    # be made either) DSNs compare as text.
    pytest.importorskip("psycopg")
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    main = "host=h port=5432 dbname=db user=app"
    for same in (
        "dbname=db user=app host=h port=5432",
        "postgresql://app@h:5432/db",
        "host=h port=5432 dbname=db user=app password=other",
    ):
        backend = PostgresBackend(dsn=main, maintenance_dsn=same)
        assert backend.maintenance_dsn is None, same
        assert backend._session_dsn() == main
    for other in ("host=h port=5432 dbname=db user=owner", "host=direct dbname=db"):
        assert PostgresBackend(dsn=main, maintenance_dsn=other).maintenance_dsn


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


def test_grants_to_the_main_role_are_opt_in(monkeypatch):
    """#800: nothing is granted unless asked, by the constructor or by
    ``POPOTO_POSTGRES_GRANT_MAIN_ROLE`` (read only by ``backend_from_env``)."""
    monkeypatch.setenv("POPOTO_POSTGRES_URL", "postgresql://pooler:6432/db")
    monkeypatch.delenv(GRANT_MAIN_ROLE_ENV, raising=False)
    assert backend_from_env().grant_main_role is False
    assert PostgresBackend(dsn="postgresql://h/db").grant_main_role is False
    assert PostgresBackend(dsn="h", grant_main_role=True).grant_main_role is True
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv(GRANT_MAIN_ROLE_ENV, value)
        assert backend_from_env().grant_main_role is True, value
    for value in ("", "0", "false", "no", "off", "2"):
        monkeypatch.setenv(GRANT_MAIN_ROLE_ENV, value)
        assert backend_from_env().grant_main_role is False, value
    # The constructor never reads the variable.
    monkeypatch.setenv(GRANT_MAIN_ROLE_ENV, "1")
    assert PostgresBackend(dsn="postgresql://h/db").grant_main_role is False


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
    """``(dsn, password)``: a DSN to a *different* database on the same
    server, read-only so that even a broken refusal could not run DDL there
    (``POPOTO_TEST_OTHER_PG_DSN`` when set, else ``template1``), and the
    password it carries -- its own when it has one (CI's does, and the server
    checks it), else a dummy one a trust-auth server never asks for."""
    from psycopg.conninfo import conninfo_to_dict

    other = os.environ.get("POPOTO_TEST_OTHER_PG_DSN", "").strip()
    if not other:
        other = _conninfo(pg.dsn, dbname="template1")
    password = conninfo_to_dict(other).get("password") or "s3cret-800"
    dsn = _conninfo(
        other,
        password=password,
        options="-c default_transaction_read_only=on",
    )
    return dsn, password


def test_a_maintenance_dsn_to_another_database_is_refused(pg, other_database, admin):
    other, password = other_database
    backend = PostgresBackend(dsn=pg.dsn, schema=pg.schema, maintenance_dsn=other)
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
    assert "database" in message and "password" not in message.lower()
    # CI's password is "postgres", also its user and database name, which the
    # message does show: look for the password itself only when it is not one.
    from psycopg.conninfo import conninfo_to_dict

    shown = [str(v) for k, v in conninfo_to_dict(other).items() if k != "password"]
    shown += [str(v) for k, v in conninfo_to_dict(pg.dsn).items() if k != "password"]
    if not any(password in value for value in shown):
        assert password not in message
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


def test_concurrent_first_uses_check_the_identity_once(split, monkeypatch):
    backend, _, _ = split
    calls = []
    real = backend._database_identity

    def counted(dsn):
        calls.append(dsn)
        time.sleep(0.05)  # widen the window two unguarded checks would share
        return real(dsn)

    monkeypatch.setattr(backend, "_database_identity", counted)
    barrier = threading.Barrier(8)
    results = []

    def first_use():
        barrier.wait()
        results.append(backend._session_dsn())

    threads = [threading.Thread(target=first_use) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [backend.maintenance_dsn] * 8
    assert len(calls) == 2  # main + maintenance, once


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_an_unreachable_maintenance_dsn_is_not_an_outage(pg, monkeypatch):
    """Only the work that needs the maintenance DSN fails, with an error
    naming it; the main backend stays healthy and no write is counted as
    dropped (#800 review)."""
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    down = _conninfo(
        pg.dsn,
        host="127.0.0.1",
        hostaddr="127.0.0.1",
        port=str(_closed_port()),
        password="s3cret-800-down",
    )
    backend = PostgresBackend(dsn=pg.dsn, schema=pg.schema, maintenance_dsn=down)
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        with pytest.raises(MaintenanceConnectionError) as caught:
            DsnDoc(name="a").save()
        with pytest.raises(MaintenanceConnectionError):
            backend.listen_dsn
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        _close_pool(pg.dsn)
    assert not isinstance(caught.value, BackendUnavailableError)
    assert isinstance(caught.value, ConnectionError)
    message = str(caught.value)
    assert "maintenance DSN" in message and "s3cret-800-down" not in message
    assert backend.health.ok
    assert backend.health.consecutive_failures == 0
    assert backend.health.dropped_writes == 0


# -- two roles: an app role on the main DSN, an owner role on the maintenance one ----
#
# Grants to the app role are opt-in (#800 review): by default first-use DDL on
# the maintenance DSN grants nothing, the app role's first save fails with
# MainRolePermissionError, and the documented SQL fixes it. With
# grant_main_role=True each DDL transaction grants on exactly what it created.

#: The names a PgBouncer auth file must list for the ``pgbouncer`` leg
#: (``<prefix>_app``, ``<prefix>_owner``); direct runs add a random suffix.
ROLE_PREFIX = os.environ.get("POPOTO_TEST_ROLE_PREFIX", "").strip() or "popoto_t800"

#: The SQL ``docs/features/postgres-backend.md`` tells an administrator to run
#: (the default, no-grant path), with ``{schema}``, ``{app}`` and ``{owner}``
#: for the docs' ``popoto``, ``app`` and ``owner``.
DOCUMENTED_GRANT_SQL = (
    "GRANT USAGE ON SCHEMA {schema} TO {app};",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {app};",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {app};",
    "REVOKE INSERT, UPDATE, DELETE ON {schema}.popoto_schema FROM {app};",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app};",
    "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
    "GRANT USAGE, SELECT ON SEQUENCES TO {app};",
)

DOCS = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "features", "postgres-backend.md"
)


def _q(name):
    return '"' + name.replace('"', '""') + '"'


class _Roles:
    """The two-role setup of one test: ``app`` and ``owner`` login roles, a
    schema neither has seen, and :meth:`bind` to make a backend over them."""

    def __init__(self, pg, admin, transport, monkeypatch):
        self.admin = admin
        self.transport = transport
        bouncer = os.environ.get("POPOTO_TEST_PGBOUNCER_URL", "").strip()
        tag = uuid.uuid4().hex[:8]
        if transport == "pgbouncer":
            self.app, self.owner = f"{ROLE_PREFIX}_app", f"{ROLE_PREFIX}_owner"
        else:
            self.app = f"{ROLE_PREFIX}_app_{tag}"
            self.owner = f"{ROLE_PREFIX}_owner_{tag}"
        self.extra_roles = []
        self.password = "popoto-t800-pw"
        self.schema = f"popoto_t800_roles_{tag}"
        self.tag = tag
        (self.database,) = admin.execute("SELECT current_database()").fetchone()
        self.main = _conninfo(
            bouncer if transport == "pgbouncer" else pg.dsn,
            user=self.app,
            password=self.password,
        )
        self.maint = _conninfo(
            pg.dsn,
            user=self.owner,
            password=self.password,
            application_name=f"popoto_t800_owner_{tag}",
        )
        self.admin_dsn = pg.dsn
        self._bound = []
        self._monkeypatch = monkeypatch

    def create(self):
        self.drop()  # a previous run interrupted before its cleanup
        for role in (self.app, self.owner):
            self.admin.execute(
                f"CREATE ROLE {_q(role)} LOGIN PASSWORD '{self.password}'"
            )
        self.admin.execute(
            f'GRANT CREATE ON DATABASE "{self.database}" TO {_q(self.owner)}'
        )

    def bind(self, *, grant=False, maint=None):
        backend = PostgresBackend(
            dsn=self.main,
            schema=self.schema,
            maintenance_dsn=maint or self.maint,
            grant_main_role=grant,
        )
        previous = set_backend(backend)
        previous_instance = _swap_instance("postgres", backend)
        self._bound.append((backend, previous, previous_instance))
        return backend

    def owner_conn(self):
        import psycopg

        return psycopg.connect(self.maint, autocommit=True)

    def privileges(self, table, role=None):
        """``(schema USAGE, schema CREATE, all four DML, any write, owner)``.
        (``has_table_privilege`` with a list answers "any of", so each
        privilege is asked on its own.)"""
        role = role or self.app
        qualified = f"{_q(self.schema)}.{_q(table)}"
        return self.admin.execute(
            "SELECT has_schema_privilege(%(r)s, %(s)s, 'USAGE'), "
            "has_schema_privilege(%(r)s, %(s)s, 'CREATE'), "
            "has_table_privilege(%(r)s, %(t)s, 'SELECT') "
            "AND has_table_privilege(%(r)s, %(t)s, 'INSERT') "
            "AND has_table_privilege(%(r)s, %(t)s, 'UPDATE') "
            "AND has_table_privilege(%(r)s, %(t)s, 'DELETE'), "
            "has_table_privilege(%(r)s, %(t)s, 'INSERT, UPDATE, DELETE'), "
            "(SELECT tableowner FROM pg_tables WHERE schemaname = %(s)s "
            "AND tablename = %(n)s)",
            {"r": role, "s": self.schema, "t": qualified, "n": table},
        ).fetchone()

    def any_privilege(self, relation, role=None):
        """Whether ``role`` holds any privilege at all on ``relation``
        (a table or a sequence), read from its ACL."""
        (held,) = self.admin.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c, "
            "aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
            "WHERE c.oid = %s::regclass AND a.grantee = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s))",
            (f"{_q(self.schema)}.{_q(relation)}", role or self.app),
        ).fetchone()
        return held

    def default_acls(self):
        (count,) = self.admin.execute(
            "SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n "
            "ON n.oid = d.defaclnamespace WHERE n.nspname = %s",
            (self.schema,),
        ).fetchone()
        return count

    def close(self):
        for backend, previous, previous_instance in reversed(self._bound):
            _swap_instance("postgres", previous_instance)
            set_backend(previous)
            if backend.maintenance_dsn:
                hub_for(backend.maintenance_dsn).close()
        _close_pool(self.main)
        self.drop()

    def drop(self):
        roles = (self.app, self.owner, *self.extra_roles)
        for role in roles:
            self.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = %s",
                (role,),
            )
        self.admin.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        for role in roles:
            exists = self.admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            if exists:
                self.admin.execute(f"DROP OWNED BY {_q(role)}")
                self.admin.execute(f"DROP ROLE {_q(role)}")


@pytest.fixture
def two_roles(request, pg, admin, monkeypatch):
    """A :class:`_Roles`: the documented split -- a login role with no DDL
    rights on the main DSN and a role allowed to create schemas on the
    maintenance DSN -- over a schema neither has seen. Parametrise with
    ``indirect=True`` and ``"pgbouncer"`` to put the main DSN behind
    ``POPOTO_TEST_PGBOUNCER_URL`` (whose auth file must then list
    ``<POPOTO_TEST_ROLE_PREFIX>_app`` and ``_owner``, default prefix
    ``popoto_t800``). Needs a superuser to create the roles; skips
    otherwise. Drops the roles and everything they own afterwards."""
    (superuser,) = admin.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if not superuser:
        pytest.skip("creating roles needs a superuser")
    transport = getattr(request, "param", "direct")
    if (
        transport == "pgbouncer"
        and not os.environ.get("POPOTO_TEST_PGBOUNCER_URL", "").strip()
    ):
        pytest.skip("POPOTO_TEST_PGBOUNCER_URL is unset (a transaction-mode PgBouncer)")
    monkeypatch.delenv(events_module.LISTEN_URL_ENV, raising=False)
    roles = _Roles(pg, admin, transport, monkeypatch)
    roles.create()
    try:
        yield roles
    finally:
        roles.close()


def _receive(backend, channel):
    sub = backend.pubsub()
    try:
        sub.subscribe(channel)
        backend.publish(channel, "hello")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = sub.get_message(timeout=0.1)
            if message is not None and message["type"] == "message":
                return message["data"]
        return None
    finally:
        sub.close()


def _assert_registry_is_read_only(roles):
    """The registry: read by the app role (the warm-start check), never
    written by it."""
    qualified = f"{_q(roles.schema)}.{POPOTO_SCHEMA_TABLE}"
    (select,) = roles.admin.execute(
        "SELECT has_table_privilege(%s, %s, 'SELECT')", (roles.app, qualified)
    ).fetchone()
    assert select
    assert roles.privileges(POPOTO_SCHEMA_TABLE)[3] is False  # no write of any kind


def test_by_default_nothing_is_granted_and_the_first_save_names_the_docs(
    two_roles,
):
    """No flag: first-use DDL runs as the owner and grants the app role
    nothing, so the app role's first save fails loudly -- a
    ``MainRolePermissionError`` carrying the server's ``permission denied``
    and the docs link -- and the backend stays healthy (not an outage)."""
    roles = two_roles
    backend = roles.bind()
    assert backend.grant_main_role is False
    with pytest.raises(MainRolePermissionError) as refused:
        DsnDoc(name="a").save()
    message = str(refused.value)
    assert "permission denied" in message, message
    assert GRANT_DOCS_URL in message and "grant_main_role=True" in message
    assert GRANT_MAIN_ROLE_ENV in message
    assert isinstance(refused.value, PermissionError)
    assert type(refused.value.__cause__).__name__ == "InsufficientPrivilege"
    assert backend._grant_to is None
    # The table is there, the owner's, and the app role holds nothing on it,
    # on the schema, or on the registry.
    assert roles.privileges(DOC_TABLE) == (False, False, False, False, roles.owner)
    assert not roles.any_privilege(POPOTO_SCHEMA_TABLE)
    assert roles.default_acls() == 0
    assert backend.health.ok and backend.health.dropped_writes == 0


def test_the_documented_sql_makes_the_default_two_role_setup_work(two_roles):
    """The docs' SQL, run once as the owner after the failing first use,
    is all the app role needs: saves, queries, a model first used later,
    and only ``SELECT`` on the registry."""
    roles = two_roles
    with open(DOCS, encoding="utf-8") as fh:
        docs = " ".join(fh.read().split())
    for template in DOCUMENTED_GRANT_SQL:
        shown = template.format(schema="popoto", app="app", owner="owner")
        assert " ".join(shown.split()) in docs, shown

    roles.bind()
    with pytest.raises(MainRolePermissionError):
        DsnDoc(name="a").save()
    with roles.owner_conn() as conn:
        for template in DOCUMENTED_GRANT_SQL:
            conn.execute(
                template.format(
                    schema=_q(roles.schema), app=_q(roles.app), owner=_q(roles.owner)
                )
            )
    DsnDoc(name="a", score=1.0).save()
    DsnDoc(name="b", score=2.0).save()
    assert [d.name for d in DsnDoc.query.filter(order_by="score")] == ["a", "b"]
    DsnLater(name="x", note="n").save()  # covered by the default privileges
    assert DsnLater.query.get(name="x").note == "n"
    _assert_registry_is_read_only(roles)


@pytest.mark.parametrize("two_roles", ["direct", "pgbouncer"], indirect=True)
def test_with_grant_main_role_the_app_role_works_end_to_end(two_roles):
    """``grant_main_role=True``: first-use DDL grants the app role exactly
    what it created -- the schema (popoto made it), the tables, ``SELECT``
    on the registry -- so saves, queries, a model first used later inside a
    unit, ``rebuild_indexes`` and ``LISTEN``/``NOTIFY`` all work as the app
    role, directly and through transaction-mode PgBouncer. No default
    privileges are set."""
    roles = two_roles
    backend = roles.bind(grant=True)

    DsnDoc(name="a", score=1.0).save()
    DsnDoc(name="b", score=2.0).save()
    assert backend._grant_to == roles.app
    assert roles.privileges(DOC_TABLE) == (True, False, True, True, roles.owner)
    _assert_registry_is_read_only(roles)
    assert roles.default_acls() == 0
    assert [d.name for d in DsnDoc.query.filter(order_by="score")] == ["a", "b"]
    doc = DsnDoc.query.get(name="a")
    doc.score = 3.0
    doc.save()
    assert DsnDoc.query.get(name="a").score == 3.0

    # A model first used later, inside a unit: its own DDL grants on it.
    with backend.transaction() as tx:
        DsnLater(name="x", note="n").save(pipeline=tx)
    later = table_name_for("DsnLater")
    assert roles.privileges(later) == (True, False, True, True, roles.owner)
    assert DsnLater.query.get(name="x").note == "n"

    DsnDoc.rebuild_indexes()  # REINDEX as the owner of the table
    assert _receive(backend, "dsn800roles") in ("hello", b"hello")

    DsnDoc.query.get(name="b").delete()
    assert [d.name for d in DsnDoc.query.all()] == ["a"]
    assert backend.health.ok and backend.health.dropped_writes == 0


def test_a_table_made_by_hand_after_the_first_save_is_not_granted(two_roles):
    """The reviewer's ``payroll_secret``: a table and a sequence the owner
    creates by hand in the schema after popoto's first use are not reachable
    by the app role -- there are no default privileges to carry a grant to
    them -- and a later first use of another model does not reach them
    either."""
    roles = two_roles
    roles.bind(grant=True)
    DsnDoc(name="a").save()
    with roles.owner_conn() as conn:
        conn.execute(f"CREATE TABLE {_q(roles.schema)}.payroll_secret (salary int)")
        conn.execute(f"CREATE SEQUENCE {_q(roles.schema)}.secret_seq")
    DsnLater(name="x").save()  # another first use, after the hand-made table
    assert roles.privileges(table_name_for("DsnLater"))[2]
    assert not roles.any_privilege("payroll_secret")
    assert not roles.any_privilege("secret_seq")
    assert roles.default_acls() == 0


def test_a_superuser_owner_never_grants_on_another_roles_table(two_roles, pg):
    """A superuser maintenance DSN (a common "owner" choice) in a schema an
    administrator made, holding another team's table: popoto grants the app
    role its own tables only. It made neither the schema nor that table, so
    it grants ``USAGE`` on neither (the administrator granted that)."""
    roles = two_roles
    other = f"{ROLE_PREFIX}_other_{roles.tag}"
    roles.extra_roles.append(other)
    admin = roles.admin
    admin.execute(f"CREATE ROLE {_q(other)} NOLOGIN")
    admin.execute(f"CREATE SCHEMA {_q(roles.schema)}")
    admin.execute(f"GRANT CREATE, USAGE ON SCHEMA {_q(roles.schema)} TO {_q(other)}")
    admin.execute(f"SET ROLE {_q(other)}")
    try:
        admin.execute(
            f"CREATE TABLE {_q(roles.schema)}.other_team_table "
            "(id bigint GENERATED ALWAYS AS IDENTITY, secret text)"
        )
    finally:
        admin.execute("RESET ROLE")
    admin.execute(f"GRANT USAGE ON SCHEMA {_q(roles.schema)} TO {_q(roles.app)}")
    superuser_maint = _conninfo(
        roles.admin_dsn, application_name=f"popoto_t800_su_{roles.tag}"
    )
    backend = roles.bind(grant=True, maint=superuser_maint)
    DsnDoc(name="a").save()
    assert DsnDoc.query.get(name="a").name == "a"
    assert backend._grant_to == roles.app
    assert roles.privileges(DOC_TABLE)[2]
    assert not roles.any_privilege("other_team_table")
    (seq,) = admin.execute(
        "SELECT pg_get_serial_sequence(%s, 'id')",
        (f"{_q(roles.schema)}.other_team_table",),
    ).fetchone()
    assert seq and not roles.any_privilege(seq.split(".", 1)[1].strip('"'))
    assert roles.privileges(DOC_TABLE)[1] is False  # never CREATE
    assert roles.default_acls() == 0


def test_a_revoke_survives_a_new_process(two_roles):
    """A warm start grants nothing: once the tables exist, a ``REVOKE`` an
    administrator runs stays revoked when a new backend (a new process: no
    memo, no identity check yet) makes its first use, and the app role's
    save is refused again rather than silently re-granted."""
    roles = two_roles
    roles.bind(grant=True)
    DsnDoc(name="a").save()
    assert roles.privileges(DOC_TABLE)[2]
    with roles.owner_conn() as conn:
        conn.execute(
            f"REVOKE ALL ON TABLE {_q(roles.schema)}.{_q(DOC_TABLE)} "
            f"FROM {_q(roles.app)}"
        )
    (acl_before,) = roles.admin.execute(
        "SELECT relacl::text FROM pg_class WHERE oid = %s::regclass",
        (f"{_q(roles.schema)}.{_q(DOC_TABLE)}",),
    ).fetchone()

    fresh = roles.bind(grant=True)  # a new process's backend, flag still on
    assert not fresh._tables and not fresh._maintenance_verified
    with pytest.raises(MainRolePermissionError):
        DsnDoc(name="b").save()
    assert fresh._grant_to == roles.app  # it would have granted, had it created
    (acl_after,) = roles.admin.execute(
        "SELECT relacl::text FROM pg_class WHERE oid = %s::regclass",
        (f"{_q(roles.schema)}.{_q(DOC_TABLE)}",),
    ).fetchone()
    assert acl_after == acl_before
    assert not roles.privileges(DOC_TABLE)[2]


def test_injection_shaped_role_names_are_quoted(admin, pg_schema):
    """A role name is the server's, quoted as Postgres quotes identifiers:
    a grant to ``…_i" TO PUBLIC; --`` or ``…_"x`` names that literal role,
    and ``PUBLIC`` gets nothing."""
    (superuser,) = admin.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if not superuser:
        pytest.skip("creating roles needs a superuser")
    tag = uuid.uuid4().hex[:6]
    names = [f'{ROLE_PREFIX}_i{tag}" TO PUBLIC; --', f'{ROLE_PREFIX}_"x{tag}']
    schema = f"popoto_t800_inj_{tag}"
    try:
        for name in names:
            admin.execute(f"CREATE ROLE {_q(name)} NOLOGIN")
        for i, name in enumerate(names):
            with admin.transaction():
                before = catalog_before(admin, schema)
                admin.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
                admin.execute(f"CREATE TABLE {_q(schema)}.t{i} (id serial)")
                ran = grant_created(admin, schema, name, before)
            assert any(_q(name) in statement for statement in ran), ran
            rows = admin.execute(
                "SELECT a.grantee, r.rolname, a.privilege_type FROM pg_class c, "
                "aclexplode(c.relacl) a LEFT JOIN pg_roles r ON r.oid = a.grantee "
                "WHERE c.oid = %s::regclass",
                (f"{_q(schema)}.t{i}",),
            ).fetchall()
            grantees = {rolname for grantee, rolname, _ in rows if grantee != 0}
            assert name in grantees, rows
            assert all(grantee != 0 for grantee, _, _ in rows), rows  # PUBLIC
            # The sequence the serial column made is granted to the role too.
            rows = admin.execute(
                "SELECT a.grantee, r.rolname FROM pg_class c, aclexplode(c.relacl) a "
                "LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE c.oid = "
                "pg_get_serial_sequence(%s, 'id')::regclass",
                (f"{_q(schema)}.t{i}",),
            ).fetchall()
            assert name in {rolname for _, rolname in rows}, rows
            assert all(grantee != 0 for grantee, _ in rows), rows
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_q(schema)} CASCADE")
        for name in names:
            if admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)
            ).fetchone():
                admin.execute(f"DROP OWNED BY {_q(name)}")
                admin.execute(f"DROP ROLE {_q(name)}")


def test_grant_created_skips_what_existed_before_the_transaction(admin):
    """The unit of the rule: a table altered (its ``pg_class`` row rewritten
    by this transaction) is not "created" and gets nothing; one created in
    it does; a schema that existed gets no ``USAGE``."""
    tag = uuid.uuid4().hex[:6]
    schema = f"popoto_t800_unit_{tag}"
    try:
        admin.execute(f"CREATE SCHEMA {_q(schema)}")
        admin.execute(f"CREATE TABLE {_q(schema)}.old (a int)")
        with admin.transaction():
            before = catalog_before(admin, schema)
            admin.execute(f"ALTER TABLE {_q(schema)}.old ADD COLUMN b int")
            admin.execute(f"CREATE TABLE {_q(schema)}.new (a int)")
            ran = grant_created(admin, schema, "pg_monitor", before)
        assert ran == [
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{schema}"."new" '
            'TO "pg_monitor"'
        ], ran
        # Nothing was created: nothing is granted (a warm start).
        with admin.transaction():
            before = catalog_before(admin, schema)
            admin.execute(f"CREATE TABLE IF NOT EXISTS {_q(schema)}.new (a int)")
            assert grant_created(admin, schema, "pg_monitor", before) == []
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_q(schema)} CASCADE")


# -- #808: the snapshot is taken under the locks; the wrapping is precise -----------

_ROLLING_CHILD = """
import sys
import popoto
from popoto.backends import _swap_instance, set_backend
from popoto.backends.postgres import PostgresBackend

main, maint, schema, grant, v2 = sys.argv[1:6]


class F808Doc(popoto.Model):
    name = popoto.UniqueKeyField()
    if v2 == "1":
        note = popoto.Field(type=str, default="")


backend = PostgresBackend(
    dsn=main, schema=schema, maintenance_dsn=maint, grant_main_role=grant == "1"
)
set_backend(backend)
_swap_instance("postgres", backend)
try:
    F808Doc(name="x").save()  # first use: the DDL; the save may be refused
except Exception as exc:
    print(type(exc).__name__)
"""


def _waiting_advisory(admin):
    (count,) = admin.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    ).fetchone()
    return count


def test_a_table_an_old_process_made_first_is_not_granted_by_a_new_one(two_roles):
    """Rolling deploy (#808): an old process (``grant_main_role`` off) and a
    new one (on, and its model has one more column) cold-start together and
    queue on the schema's advisory lock, the old one first. The old one
    creates the table; the new one then only ADDs a column to it. The table
    existed before the new process got the lock, so it was not created by
    it: the app role is granted nothing. (A snapshot taken before the lock
    saw an empty schema and granted the table.)"""
    import subprocess
    import sys

    import psycopg

    from popoto.backends.postgres.schema import schema_lock_key

    roles = two_roles
    table = table_name_for("F808Doc")
    env = dict(os.environ)
    redis_db = popoto.get_redis().connection_pool.connection_kwargs.get("db", 15)
    env["REDIS_URL"] = f"redis://localhost:6379/{redis_db}"

    def child(grant, v2):
        argv = [sys.executable, "-c", _ROLLING_CHILD]
        argv += [roles.main, roles.maint, roles.schema, grant, v2]
        return subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_waiting(n):
        deadline = time.monotonic() + 30
        while _waiting_advisory(roles.admin) < n:
            assert time.monotonic() < deadline, "a process never reached the lock"
            time.sleep(0.05)

    holder = psycopg.connect(roles.admin_dsn, autocommit=True)
    try:
        holder.execute(
            "SELECT pg_advisory_lock(hashtext(%s))", (schema_lock_key(roles.schema),)
        )
        old = child("0", "0")
        wait_waiting(1)
        new = child("1", "1")
        wait_waiting(2)
    finally:
        holder.close()  # releases the lock: the old process is first in line
    outputs = [p.communicate(timeout=120) for p in (old, new)]
    assert old.returncode == 0 and new.returncode == 0, outputs
    columns = {
        r[0]
        for r in roles.admin.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (roles.schema, table),
        ).fetchall()
    }
    assert "note" in columns, columns  # the new process did migrate the table
    assert roles.privileges(table) == (False, False, False, False, roles.owner)
    assert not roles.any_privilege(table)
    assert not roles.any_privilege(POPOTO_SCHEMA_TABLE)


def test_main_role_permission_error_wraps_only_popoto_privileges():
    """Table, schema and sequence privileges on popoto's schema are wrapped
    (with the grants help); any other ``InsufficientPrivilege`` -- a
    function, or an object elsewhere -- is handed back unchanged."""
    import psycopg

    backend = PostgresBackend(dsn="postgresql://h/db", schema="popoto")
    sql = 'SELECT * FROM "popoto"."t"'

    def denied(text):
        return psycopg.errors.InsufficientPrivilege(text)

    for text in (
        'permission denied for table "t"',
        "permission denied for table t",
        "permission denied for sequence t_id_seq",
    ):
        err = denied(text)
        wrapped = backend._main_role_permission_error(err, sql)
        assert isinstance(wrapped, MainRolePermissionError), text
        assert text in str(wrapped) and GRANT_DOCS_URL in str(wrapped)
    schema_err = denied("permission denied for schema popoto")
    assert isinstance(
        backend._main_role_permission_error(schema_err, ""), MainRolePermissionError
    )
    for err, stmt in (
        (denied("permission denied for function pg_read_file"), "SELECT 1"),
        (denied("permission denied for schema other"), sql),
        (denied("permission denied for table x"), 'SELECT * FROM "other"."x"'),
        (denied("must be superuser to do this"), sql),
    ):
        assert backend._main_role_permission_error(err, stmt) is err, str(err)


def test_a_function_privilege_error_reaches_the_caller_raw(two_roles):
    """On the live main DSN: ``pg_read_file`` is refused with the driver's
    own error, not a ``MainRolePermissionError``, and the backend stays
    healthy."""
    import psycopg

    roles = two_roles
    backend = roles.bind()
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
        backend._run("SELECT pg_read_file('/etc/hosts')", [], write=False)
    assert not isinstance(refused.value, MainRolePermissionError)
    assert backend.health.ok
