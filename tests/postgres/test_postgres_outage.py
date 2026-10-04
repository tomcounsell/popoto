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


@pytest.fixture
def unreachable(monkeypatch):
    """A PostgresBackend on a refused port, bound for the test, with short
    timeouts so a failure costs well under a second."""
    _needs_psycopg()
    from popoto.backends.postgres import PostgresBackend, close_pools

    monkeypatch.setattr(Defaults, "PG_CONNECT_TIMEOUT_SECONDS", 0.3)
    backend = PostgresBackend(dsn=UNREACHABLE, schema="popoto_outage_probe")
    previous = set_backend(backend)
    previous_instance = _swap_instance("postgres", backend)
    try:
        yield backend
    finally:
        _swap_instance("postgres", previous_instance)
        set_backend(previous)
        close_pools()


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


def test_dropped_writes_are_counted(unreachable):
    reset_bindings()
    # Bind succeeds lazily only when the server answers, so prime the table
    # memo by hand: the write itself is what must fail and be counted.
    from popoto.backends.postgres.schema import compile_table

    spec = OutageNote._meta.spec
    unreachable._tables[spec.name] = (spec, compile_table(spec, unreachable.schema))
    unreachable._server_checked = True
    for _ in range(2):
        with pytest.raises(BackendUnavailableError):
            OutageNote(key="k").save()
    assert unreachable.health.dropped_writes == 2
    with pytest.raises(BackendUnavailableError):
        OutageNote.query.count()
    assert unreachable.health.dropped_writes == 2  # a read is not a write
    assert unreachable.health.as_dict()["consecutive_failures"] == 3


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


def test_server_below_18_is_refused_at_bind(pg, monkeypatch):
    monkeypatch.setattr(type(pg), "_server_facts", lambda self: (170005, "UTF8"))
    pg._server_checked = False
    pg.forget_tables()
    with pytest.raises(BackendCapabilityError, match="PostgreSQL 18 or newer.*170005"):
        OutageNote.query.count()


def test_non_utf8_server_is_refused_at_bind(pg, monkeypatch):
    monkeypatch.setattr(type(pg), "_server_facts", lambda self: (180006, "LATIN1"))
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
        """,
        POPOTO_POSTGRES_URL=UNREACHABLE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UNAVAILABLE: BackendUnavailableError" in proc.stdout


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
