"""The conformance harness, proven on itself (#759 M0(b)).

Ported from ``tests/conformance/test_harness.py`` on the frozen
``poc/backend-seam`` archive (#631 WS2, with the fixes from #733's review,
#736, #738 and #739), minus everything that needed the POC's storage backend:
M0 ships none, so ``backend`` yields a :class:`ConformanceBackend` descriptor
and binds nothing. The POC's backend-binding tests (the session Redis pin, the
fixture restoring the previous binding) return with ``get_backend()`` in M1.

Three layers:

1. **Parametrisation** (both legs): ``backend`` names its leg; a model-level
   test runs on Redis and its Postgres leg skips with the named M1 reason.
2. **Postgres isolation** (Postgres leg, ``harness=True`` tests only): the
   schema exists, is named ``popoto_test_<hex>``, is reachable through the
   descriptor's DSN, is truncated without ``CASCADE``, and ``public`` /
   ``popoto`` are refused *before* any connection.
3. **The downstream shape** (unmarked, subprocess): a project that never opts
   in collects exactly as it does with the plugin disabled -- including one
   with its own ``conformance``/``redis_only`` markers and ``backend``
   fixture or parametrisation, which also wins in an opted-in session -- and
   has no ``backend`` fixture at all; with neither conformance opt-in a
   conformance test collects as ``[redis]`` only; with the opt-in the
   Postgres leg *skips* with a reason naming what is missing; a reasonless
   ``redis_only`` and a ``harness=True`` outside ``tests/conformance/`` are
   collection errors; a db-less ``POSTGRES_URL`` is refused even without
   ``psycopg``. These run as subprocesses so the parent session's own
   configuration does not leak in.

Never touches database 0 or schema ``public``: every Redis command goes
through the plugin-bound client and every Postgres statement through a
``popoto_test_<hex>`` schema this file's session created.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

import popoto
from popoto import pytest_plugin, redis_db
from popoto.pytest_plugin import (
    POSTGRES_MODELS_PENDING_REASON,
    POSTGRES_TEST_SCHEMA_PREFIX,
    ConformanceBackend,
    PostgresIsolationRefusedError,
    PostgresTestSchema,
)


class HarnessRecord(popoto.Model):
    name = popoto.KeyField()
    age = popoto.IntField(default=0)


# -- 1. Parametrisation --------------------------------------------------------


@pytest.mark.conformance(harness=True)
def test_backend_is_a_descriptor_naming_its_leg(backend, backend_is_redis):
    assert isinstance(backend, ConformanceBackend)
    assert backend.name in ("redis", "postgres")
    assert backend_is_redis is (backend.name == "redis")
    if backend_is_redis:
        assert backend.dsn is None and backend.schema is None
    else:
        assert backend.schema is not None and backend.dsn is not None
        assert backend.schema.startswith(POSTGRES_TEST_SCHEMA_PREFIX)


@pytest.mark.conformance
def test_model_roundtrip(backend):
    """The shape of an M1 model-level test. On Redis it runs; on Postgres it
    skips with :data:`POSTGRES_MODELS_PENDING_REASON`, because before M1 the
    model layer would still write to Redis under a Postgres label."""
    assert backend.is_redis, "the Postgres leg must have skipped in the fixture"
    HarnessRecord.create(name="alice", age=30)
    loaded = HarnessRecord.query.get(name="alice")
    assert loaded is not None and loaded.age == 30
    loaded.delete()
    assert HarnessRecord.query.get(name="alice") is None


@pytest.mark.conformance
@pytest.mark.redis_only(reason="harness self-test of the redis_only skip")
def test_redis_only_marker_skips_every_other_leg(backend):
    """``redis_only``: the Postgres leg skips in the fixture, so a body that
    reaches here is always on Redis."""
    assert backend.is_redis


def test_unmarked_test_requesting_backend_gets_redis(backend):
    """No ``conformance`` marker: ``backend`` is the Redis descriptor, once,
    whichever conformance legs are configured. (This repo opts in through
    ``popoto_test_db``; without an opt-in the fixture would not exist.)"""
    assert backend == ConformanceBackend(name="redis")


# -- 2. Postgres isolation -----------------------------------------------------


@pytest.fixture
def postgres_leg(request, backend):
    """The session schema, on the Postgres leg only. Requested lazily so the
    Redis leg never creates a schema as a side effect of this file."""
    if backend.is_redis:
        pytest.skip("Postgres-leg assertion")
    return request.getfixturevalue("popoto_postgres_schema")


@pytest.mark.conformance(harness=True)
def test_postgres_schema_exists_and_is_not_public(backend, postgres_leg):
    schema = postgres_leg
    assert backend.schema == schema.name
    assert pytest_plugin._POSTGRES_TEST_SCHEMA.fullmatch(schema.name)
    with schema.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema.name,)
        ).fetchone()
    assert row == (1,), f"schema {schema.name} was not created"


@pytest.mark.conformance(harness=True)
def test_postgres_dsn_lands_in_the_session_schema(backend, postgres_leg):
    """The descriptor's DSN carries ``options=-c search_path=<schema>``: a
    connection opened from it resolves unqualified names in the test schema,
    which is how M1's backend will learn the schema."""
    import psycopg

    assert backend.dsn is not None
    assert f"search_path%3D{postgres_leg.name}" in backend.dsn
    with psycopg.connect(backend.dsn) as conn:
        (current,) = conn.execute("SELECT current_schema()").fetchone()
    assert current == postgres_leg.name


@pytest.mark.conformance(harness=True)
def test_postgres_truncate_mirrors_flush(backend, postgres_leg):
    """A table created in the schema is emptied by the per-test truncate --
    without the harness needing to know which tables M1 will create."""
    schema = postgres_leg
    with schema.connect() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {schema.name}.probe (n int)")
        conn.execute(f"INSERT INTO {schema.name}.probe VALUES (1)")
    assert "probe" in schema.truncate_all()
    with schema.connect() as conn:
        (count,) = conn.execute(f"SELECT count(*) FROM {schema.name}.probe").fetchone()
    assert count == 0


@pytest.mark.conformance(harness=True)
def test_truncate_never_cascades_outside_the_schema(backend, postgres_leg):
    """#733 review, tech-debt 1: ``TRUNCATE ... CASCADE`` follows foreign keys
    across schemas, so a table *outside* the session schema that references one
    inside it would be emptied too. Without ``CASCADE`` the truncate refuses
    instead, and the outside row survives."""
    import psycopg

    schema = postgres_leg
    outside = PostgresTestSchema(
        name=f"{POSTGRES_TEST_SCHEMA_PREFIX}{uuid.uuid4().hex}", url=schema.url
    )
    outside.create()
    try:
        with schema.connect() as conn:
            conn.execute(f"CREATE TABLE {schema.name}.parent (id int PRIMARY KEY)")
            conn.execute(f"INSERT INTO {schema.name}.parent VALUES (1)")
            conn.execute(
                f"CREATE TABLE {outside.name}.child "
                f"(id int REFERENCES {schema.name}.parent (id))"
            )
            conn.execute(f"INSERT INTO {outside.name}.child VALUES (1)")
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            schema.truncate_all()
        with schema.connect() as conn:
            (count,) = conn.execute(
                f"SELECT count(*) FROM {outside.name}.child"
            ).fetchone()
        assert count == 1, "the truncate reached a table outside its schema"
    finally:
        outside.drop()
    # With the outside reference gone the per-test truncate works again.
    assert "parent" in schema.truncate_all()


@pytest.mark.conformance(harness=True)
def test_failed_create_closes_its_connection(backend, postgres_leg):
    """#733 review, tech-debt 4: ``CREATE SCHEMA`` failing (here, because the
    session schema already exists) closes the admin connection it opened and
    leaves ``_conn`` unset, rather than leaking it."""
    import psycopg

    opened: list = []
    twin = PostgresTestSchema(name=postgres_leg.name, url=postgres_leg.url)

    def connect():
        conn = psycopg.connect(postgres_leg.url, autocommit=True)
        opened.append(conn)
        return conn

    twin.connect = connect  # type: ignore[method-assign]
    with pytest.raises(psycopg.errors.DuplicateSchema):
        twin.create()
    assert twin._conn is None
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.conformance(harness=True)
@pytest.mark.parametrize("name", ["public", "popoto"])
def test_reserved_schemas_are_refused_before_any_statement(backend, postgres_leg, name):
    """A ``PostgresTestSchema`` named ``public`` or ``popoto`` refuses to
    create, truncate or drop -- with no connection opened (``_conn`` stays
    ``None``), so the refusal cannot have reached the server."""
    rogue = PostgresTestSchema(name=name, url=postgres_leg.url)
    for action in (rogue.create, rogue.truncate_all, rogue.drop):
        with pytest.raises(PostgresIsolationRefusedError, match=repr(name)):
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
        "popoto",
        "pg_catalog",
        "mine",
        "popoto_test_",
        "popoto_test_prod",
        "popoto_test_" + "A" * 32,
        "popoto_test_" + "a" * 31,
        "popoto_test_" + "a" * 33,
        "popoto_test_" + "a" * 32 + "\n",
    ],
)
def test_check_schema_name_refuses_names_the_harness_did_not_generate(name):
    with pytest.raises(PostgresIsolationRefusedError):
        pytest_plugin._check_schema_name(name)


@pytest.mark.parametrize("action", ["create", "truncate_all", "drop"])
def test_popoto_test_prod_is_refused_before_any_connection(action):
    """No server and no ``psycopg`` needed: the guard fires before the method
    imports anything or calls ``connect()``, so a bogus host is never dialled
    and ``_conn`` stays ``None``."""
    rogue = PostgresTestSchema(
        name="popoto_test_prod", url="postgresql://no-such-host.invalid/db"
    )
    with pytest.raises(PostgresIsolationRefusedError, match="popoto_test_prod"):
        getattr(rogue, action)()
    assert rogue._conn is None


def test_check_schema_name_accepts_a_generated_name():
    name = f"{POSTGRES_TEST_SCHEMA_PREFIX}{uuid.uuid4().hex}"
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


def test_importing_the_plugin_does_not_import_psycopg():
    """The plugin loads in every downstream pytest session; ``psycopg`` is
    imported only on the Postgres leg."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, popoto.pytest_plugin; "
            "assert 'psycopg' not in sys.modules, sorted(sys.modules)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "REDIS_URL": _test_redis_url()},
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -- 3. The downstream shape (subprocess) --------------------------------------


def _test_db() -> int:
    db = redis_db.get_REDIS_DB().connection_pool.connection_kwargs.get("db")
    assert db not in (None, 0), f"refusing to run a subprocess against db={db!r}"
    return int(db)


def _test_redis_url() -> str:
    return f"redis://localhost:6379/{_test_db()}"


_PROBE = textwrap.dedent("""
    import pytest

    @pytest.mark.conformance
    def test_probe(backend):
        assert backend is not None
    """)


def _run_pytest(
    tmp_path: Path,
    env_overrides: dict[str, str | None],
    files: dict[str, str],
    *args: str,
    expect_rc: int | None = 0,
    ini: str = "[pytest]\n",
) -> str:
    """Run pytest on a scratch project in ``tmp_path``, which carries its own
    ``pytest.ini`` (empty by default) so this repo's ``pyproject.toml`` never
    applies. ``files`` keys are paths relative to ``tmp_path``."""
    env = dict(os.environ)
    env["REDIS_URL"] = _test_redis_url()
    env["POPOTO_TEST_DB"] = str(_test_db())
    for name in ("POPOTO_CONFORMANCE_BACKENDS", "POSTGRES_URL"):
        env.pop(name, None)
    for name, value in env_overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    (tmp_path / "pytest.ini").write_text(ini)
    for relpath, source in files.items():
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    if expect_rc is not None:
        assert result.returncode == expect_rc, result.stdout + result.stderr
    return result.stdout + result.stderr


def _run_probe(tmp_path: Path, env_overrides: dict[str, str | None]) -> str:
    return _run_pytest(
        tmp_path,
        env_overrides,
        {"test_probe_conformance.py": _PROBE},
        "test_probe_conformance.py",
        "-v",
        "-rs",
    )


# A downstream suite that never opted in and happens to use both names this
# harness claims for unmarked work: its own ``backend`` fixture, parametrised
# by the project itself, and its own reasonless ``redis_only`` marker.
_DOWNSTREAM_CONFTEST = textwrap.dedent("""
    import pytest

    @pytest.fixture(params=["sqlite", "memory"])
    def backend(request):
        return request.param
    """)

_DOWNSTREAM_TESTS = textwrap.dedent("""
    import pytest

    def test_plain():
        pass

    def test_uses_own_backend(backend):
        assert backend in ("sqlite", "memory")

    @pytest.mark.redis_only
    def test_reasonless_mark_of_their_own():
        pass

    @pytest.mark.parametrize("n", [1, 2])
    def test_parametrised(n):
        pass
    """)


def _collected_ids(output: str) -> list[str]:
    return sorted(line for line in output.splitlines() if "::" in line)


def test_downstream_project_that_never_opted_in_collects_identically(tmp_path):
    """The M0 contract: with no ``popoto_test_db``, no
    ``popoto_conformance_backends`` and neither environment variable, the
    collected test ids are exactly those with the plugin disabled."""
    env = {"POPOTO_TEST_DB": None}
    files = {"conftest.py": _DOWNSTREAM_CONFTEST, "test_down.py": _DOWNSTREAM_TESTS}
    with_plugin = _run_pytest(tmp_path, env, files, "--collect-only", "-q")
    without = _run_pytest(
        tmp_path, env, files, "--collect-only", "-q", "-p", "no:popoto"
    )
    ids = _collected_ids(with_plugin)
    assert ids == _collected_ids(without)
    assert ids == sorted(
        [
            "test_down.py::test_parametrised[1]",
            "test_down.py::test_parametrised[2]",
            "test_down.py::test_plain",
            "test_down.py::test_reasonless_mark_of_their_own",
            "test_down.py::test_uses_own_backend[memory]",
            "test_down.py::test_uses_own_backend[sqlite]",
        ]
    ), with_plugin


# #763 review B1: a project that never opted in and uses the generic
# ``conformance`` marker for its *own* suite. Each case declares its markers
# under ``--strict-markers``, so it collects cleanly with the plugin disabled;
# the plugin must leave every id -- and the run -- exactly as it was.
_OWN_MARKERS_INI = textwrap.dedent("""
    [pytest]
    markers =
        conformance: our own DB-driver conformance suite
        redis_only: our own meaning -- needs a redis service
    addopts = --strict-markers
    """)

_OWN_PARAMETRISED_BACKEND = textwrap.dedent("""
    import pytest

    @pytest.fixture(params=["sqlite", "duckdb"])
    def backend(request):
        return request.param
    """)

_DOWNSTREAM_CONFORMANCE_CASES = {
    "own_parametrised_backend_fixture": (
        {
            "conftest.py": _OWN_PARAMETRISED_BACKEND,
            "test_x.py": textwrap.dedent("""
                import pytest

                @pytest.mark.conformance
                def test_x(backend):
                    assert backend in ("sqlite", "duckdb")
                """),
        },
        ["test_x.py::test_x[duckdb]", "test_x.py::test_x[sqlite]"],
    ),
    "parametrize_backend_directly": (
        {
            "test_x.py": textwrap.dedent("""
                import pytest

                @pytest.mark.conformance
                @pytest.mark.parametrize("backend", ["mysql", "pg"])
                def test_x(backend):
                    assert backend in ("mysql", "pg")
                """),
        },
        ["test_x.py::test_x[mysql]", "test_x.py::test_x[pg]"],
    ),
    "own_reasonless_redis_only": (
        {
            "conftest.py": _OWN_PARAMETRISED_BACKEND,
            "test_x.py": textwrap.dedent("""
                import pytest

                @pytest.mark.conformance
                @pytest.mark.redis_only
                def test_x():
                    pass

                @pytest.mark.conformance
                @pytest.mark.redis_only
                def test_y(backend):
                    pass
                """),
        },
        [
            "test_x.py::test_x",
            "test_x.py::test_y[duckdb]",
            "test_x.py::test_y[sqlite]",
        ],
    ),
    "own_unparametrised_backend_fixture": (
        {
            "test_x.py": textwrap.dedent("""
                import pytest

                @pytest.fixture
                def backend():
                    return "my-own-backend"

                @pytest.mark.conformance
                def test_x(backend):
                    assert backend == "my-own-backend"
                """),
        },
        ["test_x.py::test_x"],
    ),
}


@pytest.mark.parametrize("opted_in", [False, True], ids=["never-opted-in", "opted-in"])
@pytest.mark.parametrize("case", sorted(_DOWNSTREAM_CONFORMANCE_CASES))
def test_downstream_conformance_marker_of_their_own_is_left_alone(
    tmp_path, case, opted_in
):
    """#763 review B1. The plugin parametrises ``backend`` -- and enforces
    ``redis_only(reason=)`` -- only in a session that opted in, and only where
    the ``backend`` that resolves is the plugin's own. Each case collects the
    same ids as with ``-p no:popoto`` (an unparametrised fixture of the
    project's own keeps ``test_x``, not ``test_x[redis]``), and runs green --
    without the opt-in, and with it (``POPOTO_TEST_DB`` and both conformance
    legs set), where the project's own ``backend`` still wins."""
    files, expected = _DOWNSTREAM_CONFORMANCE_CASES[case]
    env: dict[str, str | None] = (
        {"POPOTO_CONFORMANCE_BACKENDS": "redis,postgres"}
        if opted_in
        else {"POPOTO_TEST_DB": None}
    )
    run = functools.partial(_run_pytest, tmp_path, env, files, ini=_OWN_MARKERS_INI)
    with_plugin = run("--collect-only", "-q")
    without = run("--collect-only", "-q", "-p", "no:popoto")
    assert _collected_ids(with_plugin) == _collected_ids(without), with_plugin
    assert _collected_ids(with_plugin) == sorted(expected), with_plugin
    out = run("-q")
    assert f"{len(expected)} passed" in out, out


def test_backend_fixture_is_not_registered_without_the_opt_in(tmp_path):
    """#763 review N3: in a session that never opted in, ``backend`` is not a
    fixture at all, so a test requesting it gets pytest's own error rather
    than a Redis descriptor nobody asked for."""
    source = "def test_x(backend):\n    pass\n"
    for extra in ((), ("-p", "no:popoto")):
        out = _run_pytest(
            tmp_path,
            {"POPOTO_TEST_DB": None},
            {"test_x.py": source},
            "test_x.py",
            *extra,
            expect_rc=1,
        )
        assert "fixture 'backend' not found" in out, out


def test_downstream_default_is_redis_only(tmp_path):
    out = _run_probe(tmp_path, {})
    assert "test_probe[redis] PASSED" in out, out
    assert "[postgres]" not in out, out


def test_model_level_postgres_leg_skips_with_the_m1_reason(tmp_path):
    """With the opt-in, ``POSTGRES_URL`` set and ``psycopg`` importable, a
    model-level conformance test's Postgres leg still skips -- with the named
    reason, before any connection (the URL names a host that does not
    exist)."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "psycopg.py").write_text("# importable stand-in for the driver\n")
    out = _run_probe(
        tmp_path,
        {
            "POPOTO_CONFORMANCE_BACKENDS": "redis,postgres",
            "POSTGRES_URL": "postgresql://no-such-host.invalid:5432/postgres",
            "PYTHONPATH": str(shim),
        },
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "test_probe[postgres] SKIPPED" in out, out
    assert POSTGRES_MODELS_PENDING_REASON in out, out


_HARNESS_PROBE = textwrap.dedent("""
    import pytest

    @pytest.mark.conformance(harness=True)
    def test_probe(backend):
        assert backend is not None
    """)

# harness=True is confined to tests/conformance/ (#763 review N2).
_HARNESS_PATH = "tests/conformance/test_probe_conformance.py"


def test_opted_in_without_postgres_url_skips_with_a_visible_reason(tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "psycopg.py").write_text("# importable stand-in for the driver\n")
    out = _run_pytest(
        tmp_path,
        {"POPOTO_CONFORMANCE_BACKENDS": "redis,postgres", "PYTHONPATH": str(shim)},
        {_HARNESS_PATH: _HARNESS_PROBE},
        _HARNESS_PATH,
        "-v",
        "-rs",
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "test_probe[postgres] SKIPPED" in out, out
    assert "POSTGRES_URL is unset" in out, out


def test_opted_in_without_psycopg_skips_with_a_visible_reason(tmp_path):
    """Shadow ``psycopg`` with a stub that raises ``ImportError`` so the probe
    sees "not installed" whatever the host venv has. A ``PYTHONPATH`` entry
    precedes site-packages on ``sys.path``, so the stub wins."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "psycopg.py").write_text(
        "raise ImportError('psycopg hidden by the conformance harness test')\n"
    )
    out = _run_pytest(
        tmp_path,
        {
            "POPOTO_CONFORMANCE_BACKENDS": "redis,postgres",
            "POSTGRES_URL": "postgresql://localhost:5432/postgres",
            "PYTHONPATH": str(shim),
        },
        {_HARNESS_PATH: _HARNESS_PROBE},
        _HARNESS_PATH,
        "-v",
        "-rs",
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "test_probe[postgres] SKIPPED" in out, out
    assert "psycopg is not installed" in out, out
    assert "1 passed, 1 skipped" in out, out


def test_db_less_postgres_url_is_refused_not_skipped(tmp_path):
    """A ``POSTGRES_URL`` naming no database is an error before any
    connection (the host does not exist), never a quiet skip."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "psycopg.py").write_text("# importable stand-in for the driver\n")
    out = _run_pytest(
        tmp_path,
        {
            "POPOTO_CONFORMANCE_BACKENDS": "redis,postgres",
            "POSTGRES_URL": "postgresql://no-such-host.invalid:5432",
            "PYTHONPATH": str(shim),
        },
        {_HARNESS_PATH: _HARNESS_PROBE},
        _HARNESS_PATH,
        "-v",
        expect_rc=1,
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "PostgresIsolationRefusedError" in out, out
    assert "names no database" in out, out


def test_db_less_postgres_url_is_refused_even_without_psycopg(tmp_path):
    """#763 review N1: the URL check needs no driver, so it runs first. A
    db-less ``POSTGRES_URL`` is a refusal whether or not ``psycopg`` imports --
    never the "not installed" skip that would hide it."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "psycopg.py").write_text(
        "raise ImportError('psycopg hidden by the conformance harness test')\n"
    )
    out = _run_pytest(
        tmp_path,
        {
            "POPOTO_CONFORMANCE_BACKENDS": "redis,postgres",
            "POSTGRES_URL": "postgresql://localhost:5432",
            "PYTHONPATH": str(shim),
        },
        {_HARNESS_PATH: _HARNESS_PROBE},
        _HARNESS_PATH,
        "-v",
        "-rs",
        expect_rc=1,
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "PostgresIsolationRefusedError" in out, out
    assert "names no database" in out, out
    assert "psycopg is not installed" not in out, out


def test_harness_flag_outside_tests_conformance_is_a_collection_error(tmp_path):
    """#763 review N2: ``conformance(harness=True)`` runs a Postgres leg that
    model code would silently satisfy on Redis, so it is confined to the
    harness's own directory. Anywhere else it fails collection, naming the
    test and the rule."""
    out = _run_pytest(
        tmp_path,
        {"POPOTO_CONFORMANCE_BACKENDS": "redis,postgres"},
        {"tests/test_probe_conformance.py": _HARNESS_PROBE},
        "tests/test_probe_conformance.py",
        expect_rc=None,
    )
    assert "ERROR collecting" in out or "errors during collection" in out, out
    assert "tests/test_probe_conformance.py::test_probe" in out, out
    assert (
        "conformance(harness=True) is only allowed under tests/conformance/" in out
    ), out


def test_unknown_backend_name_fails_the_session(tmp_path):
    out = _run_pytest(
        tmp_path,
        {"POPOTO_CONFORMANCE_BACKENDS": "redis,sqlite"},
        {"test_probe_conformance.py": _PROBE},
        "test_probe_conformance.py",
        expect_rc=None,
    )
    assert "unknown backend(s) ['sqlite']" in out, out
    assert "PASSED" not in out and " passed" not in out, out


@pytest.mark.parametrize(
    "mark",
    [
        "@pytest.mark.redis_only",
        '@pytest.mark.redis_only(reason="")',
        '@pytest.mark.redis_only("positional is not reason=")',
    ],
)
def test_redis_only_without_a_reason_is_a_collection_error(tmp_path, mark):
    """TD-16/TD-35: ``reason=`` is required. An opted-in session fails
    collection with a message naming the test and the fix."""
    source = textwrap.dedent(f"""
        import pytest

        @pytest.mark.conformance
        {mark}
        def test_probe(backend):
            pass
        """)
    out = _run_pytest(
        tmp_path,
        {},
        {"test_probe_conformance.py": source},
        "test_probe_conformance.py",
        expect_rc=None,
    )
    assert "ERROR collecting" in out or "errors during collection" in out, out
    assert "test_probe_conformance.py::test_probe" in out, out
    assert "redis_only needs a reason=" in out, out


def test_redis_only_with_a_reason_collects(tmp_path):
    source = textwrap.dedent("""
        import pytest

        @pytest.mark.conformance
        @pytest.mark.redis_only(reason="reads a Redis key")
        def test_probe(backend):
            pass
        """)
    out = _run_pytest(
        tmp_path,
        {"POPOTO_CONFORMANCE_BACKENDS": "redis,postgres"},
        {"test_probe_conformance.py": source},
        "test_probe_conformance.py",
        "-v",
        "-rs",
    )
    assert "test_probe[redis] PASSED" in out, out
    assert "test_probe[postgres] SKIPPED" in out, out
    assert "redis_only: reads a Redis key" in out, out
