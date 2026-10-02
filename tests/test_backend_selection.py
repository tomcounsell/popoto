"""Backend selection for the storage seam (#631 WS0).

The rules under test are the plan's "Backend selection and the stale-client
trap" section, in the order they break things:

1. selection is lazy -- nothing is chosen at import, and the first
   ``get_backend()`` call caches its choice;
2. ``src/popoto/__init__.py`` exports ``get_backend``/``set_backend`` as
   functions and never the ``_BACKEND`` cache (mirroring
   ``tests/test_popoto_redis_db_rebind.py``'s ``"POPOTO_REDIS_DB" not in
   vars(popoto)``);
3. a ``REDIS_URL``-only environment selects ``RedisBackend``;
4. ``POSTGRES_URL`` with ``psycopg`` absent falls back to Redis, and with
   ``psycopg`` present selects the Postgres stub -- without this module ever
   importing ``psycopg`` at module scope;
5. ``set_backend(None)`` resets the cache.

Never touches database 0: the only Redis traffic here is the pytest plugin's
own binding. The subprocess test sets ``REDIS_URL`` to the database the
session is already bound to, and ``import popoto`` issues no commands.
"""

import os
import subprocess
import sys
import types

import pytest

import popoto
from popoto import backends, redis_db
from popoto.backends.postgres import PostgresBackend
from popoto.backends.redis import RedisBackend


@pytest.fixture(autouse=True)
def _isolate_backend_cache(monkeypatch):
    """Every test starts from an empty cache and leaves the session's alone."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    saved = backends._BACKEND
    backends.set_backend(None)
    try:
        yield
    finally:
        backends.set_backend(saved)


def test_nothing_is_selected_at_import():
    """Rule 3: lazy. A fresh interpreter that imports popoto has no backend
    bound, so ``import popoto`` can never dial a second database."""
    db = redis_db.get_REDIS_DB().connection_pool.connection_kwargs.get("db")
    assert db not in (None, 0), f"refusing to run a subprocess against db={db!r}"
    env = {**os.environ, "REDIS_URL": f"redis://localhost:6379/{db}"}
    env.pop("POSTGRES_URL", None)
    code = (
        "import popoto, sys\n"
        "from popoto import backends\n"
        "assert backends._BACKEND is None, backends._BACKEND\n"
        "assert 'psycopg' not in sys.modules\n"
        "print('lazy')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "lazy"


def test_first_call_selects_and_caches():
    assert backends._BACKEND is None
    first = backends.get_backend()
    assert backends._BACKEND is first
    assert backends.get_backend() is first


def test_redis_url_only_environment_selects_redis(monkeypatch):
    """Rule 3's downstream guarantee: ``REDIS_URL`` and no ``POSTGRES_URL``
    sees no change."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    assert isinstance(backends.get_backend(), RedisBackend)


def test_postgres_url_without_psycopg_falls_back_to_redis(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/popoto_test")
    # ``None`` in sys.modules makes ``import psycopg`` raise ImportError, which
    # is how an absent package presents regardless of what is installed here.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert isinstance(backends.get_backend(), RedisBackend)


def test_postgres_url_with_psycopg_selects_the_stub(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/popoto_test")
    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    backend = backends.get_backend()
    assert isinstance(backend, PostgresBackend)
    assert backend.url == "postgresql://localhost/popoto_test"


def test_the_stub_names_the_method_it_refuses(monkeypatch):
    stub = PostgresBackend("postgresql://localhost/x")
    with pytest.raises(NotImplementedError, match=r"PostgresBackend\.load_record"):
        stub.load_record("Memory:1")
    with pytest.raises(NotImplementedError, match="Redis-only"):
        stub.native()


def test_postgres_module_does_not_import_psycopg_at_module_scope(monkeypatch):
    """Importing the stub must work with ``psycopg`` unimportable."""
    import importlib

    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.delitem(sys.modules, "popoto.backends.postgres", raising=False)
    module = importlib.import_module("popoto.backends.postgres")
    assert hasattr(module, "PostgresBackend")
    assert "psycopg" not in vars(module)


def test_set_backend_none_resets_the_cache(monkeypatch):
    class Custom(RedisBackend):
        pass

    custom = Custom()
    backends.set_backend(custom)
    assert backends.get_backend() is custom
    assert popoto.get_backend() is custom

    backends.set_backend(None)
    assert backends._BACKEND is None
    reselected = backends.get_backend()
    assert reselected is not custom
    assert isinstance(reselected, RedisBackend)


def test_package_exports_functions_never_the_cache():
    """Rule 2, asserted on the mechanism: a package-level ``_BACKEND`` would be
    a snapshot ``set_backend()`` could never update (#651 one layer up)."""
    assert "_BACKEND" not in vars(popoto)
    assert popoto.get_backend is backends.get_backend
    assert popoto.set_backend is backends.set_backend
    assert "get_backend" in popoto.__all__
    assert "set_backend" in popoto.__all__
    assert "_BACKEND" not in popoto.__all__


def test_backends_module_never_imports_the_client_by_name():
    """Rule 2's other half: only the accessors, never ``POPOTO_REDIS_DB``."""
    import ast
    import inspect

    from popoto.backends import redis as backends_redis

    for module in (backends, backends_redis):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            # The *code* must never bind or read the name; docstrings and
            # comments are free to explain why, so this walks the AST rather
            # than grepping the text.
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                assert "POPOTO_REDIS_DB" not in imported, (
                    f"{module.__name__} imports POPOTO_REDIS_DB by name "
                    f"(line {node.lineno}) -- a snapshot set_REDIS_DB_settings() "
                    "never updates (#655)"
                )
            if isinstance(node, ast.Attribute):
                assert node.attr != "POPOTO_REDIS_DB", (
                    f"{module.__name__} reads .POPOTO_REDIS_DB at line "
                    f"{node.lineno}; use get_REDIS_DB()"
                )
    assert "POPOTO_REDIS_DB" not in vars(backends_redis)
    assert "POPOTO_REDIS_DB" not in vars(backends)


def test_protocol_enumerates_the_planned_forty_two_methods():
    """The plan's count, pinned so a silent addition or removal shows up."""
    methods = sorted(
        name
        for name, value in vars(backends.Backend).items()
        if not name.startswith("_") and callable(value)
    )
    assert len(methods) == 42, methods
    for impl in (RedisBackend, PostgresBackend):
        missing = [m for m in methods if not callable(getattr(impl, m, None))]
        assert not missing, f"{impl.__name__} lacks {missing}"


def test_unit_of_work_is_the_guarded_pipeline():
    """Architect decision 1: on Redis the unit of work *is* the pipeline, so an
    external caller's ``pipeline=`` keeps working and ``commit()`` is
    ``execute()``."""
    uow = RedisBackend().begin()
    assert isinstance(uow, redis_db.GuardedPipeline)
    assert callable(uow.commit)
    uow.reset()
