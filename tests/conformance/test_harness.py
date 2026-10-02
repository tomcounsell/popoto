"""The conformance harness, proven on itself (#631 WS2).

This was the one ``conformance``-marked file in WS2. Its job is to show that
the harness in ``popoto.pytest_plugin`` does what the plan says, not to test
any backend method. The one record round-trip below was a strict xfail on the
WS0 stub's ``NotImplementedError``; WS3a implemented the record family and
removed the mark, so it now passes on both legs (``test_records.py`` is where
the family is actually exercised).

Three layers:

1. **Parametrisation and binding** (both legs): ``backend`` is the
   process-wide ``get_backend()`` for the test and is reset afterwards; the
   Postgres leg carries the per-session schema in its URL.
2. **Postgres isolation** (Postgres leg only): the schema exists, is named
   ``popoto_test_<hex>``, is reachable through the backend URL's
   ``search_path``, and ``public`` is refused *before* any statement runs.
3. **The downstream shape** (unmarked, subprocess): with neither opt-in set a
   conformance test collects as ``[redis]`` only; with the opt-in and no
   ``POSTGRES_URL`` the Postgres leg *skips* with a reason naming the variable.
   These run as subprocesses so the parent session's own configuration (this
   repo may export ``POPOTO_CONFORMANCE_BACKENDS``) does not leak in.

Never touches database 0 or schema ``public``: every Redis command goes
through the plugin-bound client and every Postgres statement through the
harness's own schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from pathlib import Path

import msgpack
import pytest

from popoto import backends, pytest_plugin, redis_db
from popoto.backends.postgres import PostgresBackend
from popoto.backends.redis import RedisBackend
from popoto.pytest_plugin import (
    POSTGRES_TEST_SCHEMA_PREFIX,
    PostgresIsolationRefusedError,
    PostgresTestSchema,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "popoto_test:631:harness"


# -- 1. Parametrisation and binding -------------------------------------------


@pytest.mark.conformance
def test_backend_is_bound_process_wide(backend):
    """``set_backend(backend)`` ran: code under test resolving
    ``get_backend()`` sees the leg's object, on every leg."""
    assert backends.get_backend() is backend
    assert isinstance(backend, (RedisBackend, PostgresBackend))


def test_backend_fixture_resets_the_process_wide_backend_on_teardown():
    """Self-contained (order-free): drive the ``backend`` fixture's own
    generator, observe it bound, then finish it and observe the reset."""
    fixture = pytest_plugin.backend
    fn = getattr(fixture, "_get_wrapped_function", lambda: fixture)()
    backends.set_backend(None)
    gen = fn(SimpleNamespace(param="redis"))
    impl = next(gen)
    try:
        assert backends._BACKEND is impl, "the fixture must bind its backend"
    finally:
        with pytest.raises(StopIteration):
            next(gen)
    assert backends._BACKEND is None, "teardown must run set_backend(None)"


@pytest.mark.conformance
def test_record_roundtrip(backend):
    """The proof the harness works: one save / load / delete on each leg."""
    key = f"{PREFIX}:Record:1"
    class_set = f"$Class:{PREFIX}:Record"
    fields = {b"name": msgpack.packb("alice"), b"age": msgpack.packb(30)}

    backend.save_record(key, fields, class_set=class_set)
    assert backend.load_record(key) == fields
    assert backend.delete_record(key, class_set=class_set)
    assert backend.load_record(key) is None


@pytest.mark.conformance
@pytest.mark.redis_only
def test_redis_only_marker_skips_every_other_leg(backend, backend_is_redis):
    """``redis_only`` inside a conformance file: the Postgres leg skips in the
    fixture, so a body that reaches here is always on Redis."""
    assert backend_is_redis
    assert isinstance(backend, RedisBackend)
    assert backend.client is redis_db.get_REDIS_DB()


# -- 2. Postgres isolation -----------------------------------------------------


@pytest.fixture
def postgres_leg(request, backend, backend_is_redis):
    """The session schema, on the Postgres leg only. Requested lazily so the
    Redis leg never creates a schema as a side effect of this file."""
    if backend_is_redis:
        pytest.skip("Postgres-leg assertion")
    return request.getfixturevalue("popoto_postgres_schema")


@pytest.mark.conformance
def test_postgres_schema_exists_and_is_not_public(backend, postgres_leg):
    schema = postgres_leg
    assert schema.name.startswith(POSTGRES_TEST_SCHEMA_PREFIX)
    assert schema.name != "public"
    with schema.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema.name,)
        ).fetchone()
    assert row == (1,), f"schema {schema.name} was not created"


@pytest.mark.conformance
def test_postgres_backend_url_lands_in_the_session_schema(backend, postgres_leg):
    """The backend's URL carries ``options=-c search_path=<schema>``: a
    connection opened from it resolves unqualified names in the test schema,
    which is how the WS0 constructor learns the schema without changing."""
    import psycopg

    assert isinstance(backend, PostgresBackend)
    assert f"search_path%3D{postgres_leg.name}" in backend.url
    with psycopg.connect(backend.url) as conn:
        (current,) = conn.execute("SELECT current_schema()").fetchone()
    assert current == postgres_leg.name


@pytest.mark.conformance
def test_postgres_truncate_mirrors_flush(backend, postgres_leg):
    """A table created in the schema is emptied by the per-test truncate --
    without the harness needing to know which tables WS3 will create."""
    schema = postgres_leg
    with schema.connect() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {schema.name}.probe (n int)")
        conn.execute(f"INSERT INTO {schema.name}.probe VALUES (1)")
    # The backend's own tables (WS3a onwards) are truncated alongside it.
    assert "probe" in schema.truncate_all()
    with schema.connect() as conn:
        (count,) = conn.execute(f"SELECT count(*) FROM {schema.name}.probe").fetchone()
    assert count == 0


@pytest.mark.conformance
def test_public_schema_is_refused_before_any_statement(backend, postgres_leg):
    """A ``PostgresTestSchema`` named ``public`` refuses to create, truncate or
    drop -- with no connection opened (``_conn`` stays ``None``), so the
    refusal cannot have reached the server."""
    rogue = PostgresTestSchema(name="public", url=postgres_leg.url)
    for action in (rogue.create, rogue.truncate_all, rogue.drop):
        with pytest.raises(PostgresIsolationRefusedError, match="public"):
            action()
        assert rogue._conn is None
    with postgres_leg.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'public'"
        ).fetchone()
    assert row == (1,), "schema public must survive the refusal untouched"


# -- Refusals and resolution, no server needed (unmarked) ---------------------


@pytest.mark.parametrize(
    "name",
    [
        "public",
        "pg_catalog",
        "mine",
        "popoto_test_",
        "popoto_test_prod",
        "popoto_test_" + "A" * 32,
        "popoto_test_" + "a" * 31,
        "popoto_test_" + "a" * 33,
    ],
)
def test_check_schema_name_refuses_names_the_harness_did_not_generate(name):
    with pytest.raises(PostgresIsolationRefusedError):
        pytest_plugin._check_schema_name(name)


@pytest.mark.parametrize("action", ["create", "truncate_all", "drop"])
def test_popoto_test_prod_is_refused_before_any_connection(action):
    """No server needed: the guard fires before ``connect()``, so a bogus
    host is never dialled and ``_conn`` stays ``None``."""
    rogue = PostgresTestSchema(
        name="popoto_test_prod", url="postgresql://no-such-host.invalid/db"
    )
    with pytest.raises(PostgresIsolationRefusedError, match="popoto_test_prod"):
        getattr(rogue, action)()
    assert rogue._conn is None


def test_check_schema_name_accepts_a_generated_name():
    name = f"{POSTGRES_TEST_SCHEMA_PREFIX}{'0123abcd' * 4}"
    assert pytest_plugin._check_schema_name(name) == name


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost:5432",
        "postgresql://localhost:5432/",
        "postgres://user:pw@host",
        "dbname=postgres host=localhost",
    ],
)
def test_check_postgres_url_refuses_a_url_naming_no_database(url):
    with pytest.raises(PostgresIsolationRefusedError):
        pytest_plugin._check_postgres_url(url)


def test_check_postgres_url_accepts_a_named_database():
    url = "postgresql://localhost:5432/postgres"
    assert pytest_plugin._check_postgres_url(url) == url


def test_url_with_search_path_extends_existing_options():
    url = "postgresql://h/db?sslmode=disable&options=-c%20statement_timeout%3D5"
    out = pytest_plugin._url_with_search_path(url, "popoto_test_x")
    assert out == (
        "postgresql://h/db?sslmode=disable"
        "&options=-c%20statement_timeout%3D5%20-c%20search_path%3Dpopoto_test_x"
    )


class _Config:
    def __init__(self, ini: str = "") -> None:
        self._ini = ini

    def getini(self, name: str) -> str:
        assert name == "popoto_conformance_backends"
        return self._ini


def test_resolve_backends_defaults_to_redis_only(monkeypatch):
    monkeypatch.delenv("POPOTO_CONFORMANCE_BACKENDS", raising=False)
    assert pytest_plugin._resolve_conformance_backends(_Config()) == ("redis",)


def test_resolve_backends_env_beats_ini(monkeypatch):
    monkeypatch.setenv("POPOTO_CONFORMANCE_BACKENDS", "postgres")
    assert pytest_plugin._resolve_conformance_backends(_Config("redis")) == (
        "postgres",
    )
    monkeypatch.delenv("POPOTO_CONFORMANCE_BACKENDS")
    assert pytest_plugin._resolve_conformance_backends(
        _Config(" Redis, postgres ,redis ")
    ) == ("redis", "postgres")


def test_resolve_backends_rejects_an_unknown_name(monkeypatch):
    monkeypatch.setenv("POPOTO_CONFORMANCE_BACKENDS", "redis,sqlite")
    with pytest.raises(ValueError, match="sqlite"):
        pytest_plugin._resolve_conformance_backends(_Config())


def test_unmarked_test_requesting_backend_gets_redis(backend):
    """No ``conformance`` marker: ``backend`` is the Redis backend, once,
    whatever the session's opt-in says."""
    assert isinstance(backend, RedisBackend)
    assert backends.get_backend() is backend


# -- 3. The downstream shape (subprocess) --------------------------------------

_PROBE = textwrap.dedent("""
    import pytest

    @pytest.mark.conformance
    def test_probe(backend):
        assert backend is not None
    """)


def _run_probe(tmp_path: Path, env_overrides: dict[str, str | None]) -> str:
    db = redis_db.get_REDIS_DB().connection_pool.connection_kwargs.get("db")
    assert db not in (None, 0), f"refusing to run a subprocess against db={db!r}"
    env = dict(os.environ)
    env["REDIS_URL"] = f"redis://localhost:6379/{db}"
    env["POPOTO_TEST_DB"] = str(db)
    for name in ("POPOTO_CONFORMANCE_BACKENDS", "POSTGRES_URL"):
        env.pop(name, None)
    for name, value in env_overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    probe = tmp_path / "test_probe_conformance.py"
    probe.write_text(_PROBE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-v",
            "-rs",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_downstream_default_is_redis_only(tmp_path):
    out = _run_probe(tmp_path, {})
    assert "test_probe[redis] PASSED" in out, out
    assert "[postgres]" not in out, out


def test_opted_in_without_postgres_url_skips_with_a_visible_reason(tmp_path):
    out = _run_probe(tmp_path, {"POPOTO_CONFORMANCE_BACKENDS": "redis,postgres"})
    assert "test_probe[redis] PASSED" in out, out
    assert "test_probe[postgres] SKIPPED" in out, out
    assert "POSTGRES_URL is unset" in out, out
