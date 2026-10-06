"""The outage contract, laziness and selection (#759 M1b, ``[PG-only]``).

None of these need a running server: they point the backend at a closed port
(``127.0.0.1:1``, refused at once) or block ``psycopg`` entirely. They need
``psycopg`` installed only where they exercise the pool.
"""

import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest

import popoto
from popoto.backends import (
    BackendCapabilityError,
    BackendUnavailableError,
    _swap_instance,
    get_backend,
    reset_bindings,
    set_backend,
)
from popoto.fields.constants import Defaults

UNREACHABLE = "postgresql://127.0.0.1:1/postgres"
REPO_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")


def _needs_psycopg():
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")


# Three ways a server is unreachable before the model's first use: a refused
# port (fails at once), a blackhole address (nothing answers; bounded only by
# the connect timeout) and a name that does not resolve.
REFUSED = UNREACHABLE
BLACKHOLE = "postgresql://10.255.255.1:5432/postgres"
BAD_DNS = "postgresql://nonexistent.invalid:5432/postgres"


def _install(monkeypatch, dsn):
    _needs_psycopg()
    from popoto.backends.postgres import PostgresBackend, close_pools

    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    backend = PostgresBackend(dsn=dsn, schema="popoto_outage_probe")
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)

    def restore():
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        close_pools()

    return backend, restore


@pytest.fixture
def unreachable(monkeypatch):
    """A PostgresBackend on a refused port, bound for the test, with short
    timeouts so a failure costs well under a second."""
    backend, restore = _install(monkeypatch, UNREACHABLE)
    try:
        yield backend
    finally:
        restore()


@pytest.fixture(
    params=[REFUSED, BLACKHOLE, BAD_DNS], ids=["refused", "blackhole", "bad-dns"]
)
def unreachable_from_start(request, monkeypatch):
    """A *fresh* backend (nothing bound, no table memo) whose server has
    never answered: the model's first use is the lazy bind itself."""
    backend, restore = _install(monkeypatch, request.param)
    try:
        yield backend
    finally:
        restore()


class OutageNote(popoto.Model):
    key = popoto.KeyField()
    n = popoto.IntField(default=0)


def test_unreachable_server_raises_backend_unavailable_on_first_use(unreachable):
    started = time.monotonic()
    with pytest.raises(BackendUnavailableError, match="Postgres is unavailable"):
        OutageNote.query.count()
    assert time.monotonic() - started < 3, "the connect timeout was not applied"
    assert unreachable.health.ok is False
    assert unreachable.health.consecutive_failures >= 1


def _bounded(call):
    started = time.monotonic()
    with pytest.raises(BackendUnavailableError, match="Postgres is unavailable"):
        call()
    elapsed = time.monotonic() - started
    assert elapsed < 3, f"took {elapsed:.1f}s: the connect timeout was not applied"


def test_dropped_writes_are_counted_from_the_first_use(unreachable_from_start):
    """#769 review blocker 1: the M1 exit scenario. The server is unreachable
    before the model was ever used, so every call's first step is the lazy
    table/server check -- a read. A save that fails there is still a dropped
    write; a query or count that fails there is not."""
    backend = unreachable_from_start
    reset_bindings()
    assert backend._tables == {} and backend._server_checked is False
    _bounded(lambda: OutageNote(key="k").save())
    assert backend.health.dropped_writes == 1
    _bounded(lambda: OutageNote.create(key="k2"))
    assert backend.health.dropped_writes == 2
    _bounded(lambda: list(OutageNote.query.filter(key="k")))
    _bounded(lambda: OutageNote.query.count())
    _bounded(lambda: OutageNote.query.get(key="k"))
    health = backend.health.as_dict()
    assert health["dropped_writes"] == 2, "a failed read was counted as a write"
    assert health["consecutive_failures"] == 5
    assert health["ok"] is False and health["last_error"]


def test_error_is_logged_once_per_window(unreachable, caplog, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_OUTAGE_LOG_WINDOW_SECONDS", 3600)
    with caplog.at_level(logging.ERROR, logger="POPOTO.postgres"):
        for _ in range(3):
            with pytest.raises(BackendUnavailableError):
                OutageNote.query.count()
    errors = [r for r in caplog.records if r.name == "POPOTO.postgres"]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    monkeypatch.setattr(Defaults, "PG_OUTAGE_LOG_WINDOW_SECONDS", 0)
    with caplog.at_level(logging.ERROR, logger="POPOTO.postgres"):
        with pytest.raises(BackendUnavailableError):
            OutageNote.query.count()
    errors = [r for r in caplog.records if r.name == "POPOTO.postgres"]
    assert len(errors) == 2


def test_statement_timeout_raises_backend_unavailable(pg, monkeypatch):
    monkeypatch.setattr(Defaults, "PG_STATEMENT_TIMEOUT_MS", 50)
    with pytest.raises(BackendUnavailableError, match="statement timeout"):
        pg._run("SELECT pg_sleep(%s)", [1])
    monkeypatch.setattr(Defaults, "PG_STATEMENT_TIMEOUT_MS", 30000)
    rows, _ = pg._run("SELECT 1")
    assert rows == [(1,)] and pg.health.ok


def _pool_pid(pg):
    rows, _ = pg._run("SELECT pg_backend_pid()")
    return rows[0][0]


def test_stale_pooled_connection_after_a_restart_is_not_a_dropped_write(pg, admin):
    """#769 review blocker 2: a server restart, failover or idle reaper kills
    the pool's backends while the server stays up. The next save must use a
    live connection, not fail on the dead one and count a dropped write."""
    from popoto.backends.postgres import close_pools

    close_pools()  # a fresh pool, so the one pooled connection is the one killed
    OutageNote.create(key="before")
    pid = _pool_pid(pg)
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    for _ in range(50):
        admin.execute("SELECT pg_stat_clear_snapshot()")
        alive = admin.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if alive is None:
            break
        time.sleep(0.02)
    dropped = pg.health.dropped_writes
    OutageNote.create(key="after")
    assert pg.health.dropped_writes == dropped
    assert pg.health.ok and pg.health.consecutive_failures == 0
    assert _pool_pid(pg) != pid
    assert {n.key for n in OutageNote.query.all()} == {"before", "after"}


def test_a_read_whose_connection_dies_mid_statement_is_retried_once(pg, admin):
    """The pool's checkout check cannot catch a backend killed *during* a
    statement. A read cannot have committed anything, so it runs once more on
    a fresh connection."""
    import threading

    OutageNote.create(key="x")
    killed = []

    def kill_when_sleeping():
        for _ in range(300):
            row = admin.execute(
                "SELECT pid FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                "AND query LIKE '%%pg_sleep(0.5), 7%%' AND state = 'active'"
            ).fetchone()
            if row:
                admin.execute("SELECT pg_terminate_backend(%s)", (row[0],))
                killed.append(row[0])
                return
            time.sleep(0.01)

    killer = threading.Thread(target=kill_when_sleeping)
    killer.start()
    rows, _ = pg._run("SELECT pg_sleep(0.5), 7")
    killer.join()
    assert killed, "the statement was never seen running"
    assert rows[0][1] == 7
    assert pg.health.ok and pg.health.dropped_writes == 0


class _Conn:
    def __init__(self, broken):
        self.closed = False
        self.broken = broken


class _Err(Exception):
    def __init__(self, sqlstate):
        super().__init__("boom")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "conn, sqlstate, write, retry",
    [
        # a failed connect (no connection handed out) is an outage: no retry
        (None, None, False, False),
        # a live connection with a statement-level error: not a reconnect case
        (_Conn(broken=False), "57014", False, False),
        # a dead connection under a read: always safe to run again
        (_Conn(broken=True), None, False, True),
        # a write the server failed (57P01 AdminShutdown): rolled back, retry
        (_Conn(broken=True), "57P01", True, True),
        # a write whose reply was lost: it may have committed, never retried
        (_Conn(broken=True), None, True, False),
    ],
    ids=[
        "connect-failed",
        "conn-alive",
        "read",
        "write-server-failed",
        "write-unknown",
    ],
)
def test_only_statements_that_cannot_have_committed_are_retried(
    conn, sqlstate, write, retry
):
    _needs_psycopg()
    from popoto.backends.postgres import PostgresBackend

    assert PostgresBackend._may_retry_broken(conn, _Err(sqlstate), write) is retry


def test_server_below_18_is_refused_at_bind(pg, monkeypatch):
    monkeypatch.setattr(
        type(pg), "_server_facts", lambda self, uow=None: (170005, "UTF8")
    )
    pg._server_checked = False
    pg.forget_tables()
    with pytest.raises(BackendCapabilityError, match="PostgreSQL 18 or newer.*170005"):
        OutageNote.query.count()


def test_non_utf8_server_is_refused_at_bind(pg, monkeypatch):
    monkeypatch.setattr(
        type(pg), "_server_facts", lambda self, uow=None: (180006, "LATIN1")
    )
    pg._server_checked = False
    pg.forget_tables()
    with pytest.raises(BackendCapabilityError, match="UTF8"):
        OutageNote.query.count()


def _run_isolated(code, **env):
    full_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("POPOTO_", "POSTGRES", "DATABASE"))
    }
    full_env.update(
        PYTHONPATH=os.path.abspath(REPO_SRC),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/14"),
        **env,
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_import_popoto_and_declare_with_psycopg_blocked():
    proc = _run_isolated(
        """
        import sys
        sys.modules["psycopg"] = None
        sys.modules["psycopg_pool"] = None
        import popoto

        class Blocked(popoto.Model):
            key = popoto.KeyField()
            class Meta:
                backend = "postgres"

        assert "psycopg" not in [m for m in sys.modules if sys.modules[m]]
        try:
            Blocked.query.count()
        except popoto.backends.BackendUnavailableError as exc:
            print("REFUSED:", exc)
        """,
        POPOTO_POSTGRES_URL=UNREACHABLE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED:" in proc.stdout and "popoto[postgres]" in proc.stdout


def test_declaring_a_postgres_model_never_dials_and_first_use_raises():
    _needs_psycopg()
    proc = _run_isolated(
        """
        import time
        import popoto
        from popoto.fields.constants import Defaults
        Defaults.PG_CONNECT_TIMEOUT_SECONDS = 0.3
        import popoto.backends.postgres as pgmod

        class Remote(popoto.Model):
            key = popoto.KeyField()
            class Meta:
                backend = "postgres"

        assert pgmod._pools == {}, "declaring a model opened a pool"
        try:
            Remote(key="a").save()
        except popoto.backends.BackendUnavailableError as exc:
            print("UNAVAILABLE:", type(exc).__name__)
        health = popoto.backends.get_backend(Remote).health
        print("DROPPED:", health.dropped_writes)
        """,
        POPOTO_POSTGRES_URL=UNREACHABLE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UNAVAILABLE: BackendUnavailableError" in proc.stdout
    assert "DROPPED: 1" in proc.stdout


def test_library_reads_only_popoto_postgres_url(monkeypatch):
    """#768 review: the library's DSN is POPOTO_POSTGRES_URL and nothing
    else. POSTGRES_URL/DATABASE_URL (which the *test harness* reads for its
    own schema) are never consulted, even when set."""
    read = []

    class Recording(dict):
        def get(self, key, default=None):
            read.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            read.append(key)
            return super().__getitem__(key)

    env = Recording(
        POSTGRES_URL="postgresql://wrong/one", DATABASE_URL="postgresql://wrong/two"
    )
    monkeypatch.setattr(os, "environ", env)
    from popoto.backends.postgres import backend_from_env

    with pytest.raises(BackendUnavailableError, match="POPOTO_POSTGRES_URL"):
        backend_from_env()
    assert "POSTGRES_URL" not in read and "DATABASE_URL" not in read
    env["POPOTO_POSTGRES_URL"] = "postgresql://right/db"
    backend = backend_from_env()
    assert backend.dsn == "postgresql://right/db" and backend.schema == "popoto"


def test_selecting_postgres_without_a_url_names_the_variable(monkeypatch):
    monkeypatch.delenv("POPOTO_POSTGRES_URL", raising=False)
    previous = _swap_instance("postgres", None)
    try:

        class NoUrl(popoto.Model):
            key = popoto.KeyField()

            class Meta:
                backend = "postgres"

        with pytest.raises(BackendUnavailableError, match="POPOTO_POSTGRES_URL"):
            get_backend(NoUrl)
    finally:
        _swap_instance("postgres", previous)
