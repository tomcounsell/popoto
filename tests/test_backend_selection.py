"""Backend selection rules (#759 M1a, plan §4).

* ``Meta.backend`` on a model wins; otherwise the process default --
  :func:`set_backend`, else ``POPOTO_BACKEND``, else ``"redis"``.
* Selection is lazy: nothing is read or bound at import or class creation.
* ``POSTGRES_URL`` / ``DATABASE_URL`` are never read: Postgres is only ever
  selected explicitly.
* The Redis backend holds no client (CLAUDE.md, #655): it resolves
  ``get_REDIS_DB()`` on every operation, so a rebind of the global after the
  backend was created is observed (the probe below mirrors
  ``tests/test_redis_db_rebind_staleness.py``).
* Under this repo's opted-in pytest session, the plugin pins the default to
  Redis before collection.

The backend module and a model are imported at module scope on purpose: the
rebind probe must run against objects that existed *before* the rebind, or it
passes vacuously (#661).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import popoto
from popoto import backends, redis_db
from popoto.backends import (
    BackendCapabilityError,
    BackendUnavailableError,
    default_backend_name,
    get_backend,
    set_backend,
)
from popoto.backends.redis import RedisBackend
from popoto.exceptions import ModelException


class SelProbe(popoto.Model):
    name = popoto.KeyField()
    rank = popoto.SortedField(type=int, default=0)


class SelExplicitRedis(popoto.Model):
    name = popoto.KeyField()

    class Meta:
        backend = "redis"


@pytest.fixture
def unpinned(monkeypatch):
    """Lift the session pin for one test and restore it afterwards."""
    previous = set_backend(None)
    monkeypatch.delenv("POPOTO_BACKEND", raising=False)
    yield
    set_backend(previous)


# -- the session pin ----------------------------------------------------------


def test_opted_in_session_is_pinned_to_redis(monkeypatch):
    """The plugin pinned the default in pytest_configure, so even an
    environment naming another backend leaves this session on Redis."""
    assert backends._default == "redis"
    monkeypatch.setenv("POPOTO_BACKEND", "postgres")
    assert default_backend_name() == "redis"
    assert isinstance(get_backend(SelProbe), RedisBackend)


# -- selection rules ----------------------------------------------------------


def test_default_is_redis(unpinned):
    assert default_backend_name() == "redis"
    assert isinstance(get_backend(), RedisBackend)
    assert get_backend().name == "redis"


def test_env_var_selects_default_lazily(unpinned, monkeypatch):
    monkeypatch.setenv("POPOTO_BACKEND", "redis")
    assert default_backend_name() == "redis"
    monkeypatch.setenv("POPOTO_BACKEND", "postgres")
    assert default_backend_name() == "postgres"
    with pytest.raises(BackendUnavailableError, match="POPOTO_POSTGRES_URL"):
        get_backend()


def test_unknown_env_value_is_an_error(unpinned, monkeypatch):
    monkeypatch.setenv("POPOTO_BACKEND", "mongo")
    with pytest.raises(ValueError, match="unknown popoto backend 'mongo'"):
        get_backend()


def test_set_backend_wins_over_env_and_returns_previous(unpinned, monkeypatch):
    monkeypatch.setenv("POPOTO_BACKEND", "postgres")
    previous = set_backend("redis")
    assert previous is None
    assert default_backend_name() == "redis"
    assert set_backend(None) == "redis"
    with pytest.raises(ValueError):
        set_backend("mongo")


def test_meta_backend_wins_over_the_default(unpinned):
    sentinel = RedisBackend()
    sentinel.name = "recording"
    set_backend(sentinel)
    assert get_backend(SelProbe) is sentinel
    explicit = get_backend(SelExplicitRedis)
    assert explicit is not sentinel and isinstance(explicit, RedisBackend)


def test_meta_backend_must_be_a_known_name():
    with pytest.raises(ModelException, match="Meta.backend must be one of"):

        class SelBadBackend(popoto.Model):
            name = popoto.KeyField()

            class Meta:
                backend = "mongo"


def test_generic_database_urls_are_never_read(unpinned, monkeypatch):
    """Postgres is never auto-selected: with every generic URL set, selection
    is Redis, and none of those variables is even looked up."""
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/x")
    monkeypatch.setenv("POPOTO_POSTGRES_URL", "postgresql://localhost/x")
    looked_up: list[str] = []
    real_environ = os.environ

    class RecordingEnviron(dict):
        def get(self, key, default=None):
            looked_up.append(key)
            return real_environ.get(key, default)

        def __getitem__(self, key):
            looked_up.append(key)
            return real_environ[key]

    monkeypatch.setattr(backends.os, "environ", RecordingEnviron(real_environ))
    backends.reset_bindings()
    assert isinstance(get_backend(SelProbe), RedisBackend)
    assert "POPOTO_BACKEND" in looked_up
    assert not {"POSTGRES_URL", "DATABASE_URL"} & set(looked_up)


def test_selection_is_lazy_at_import_and_class_creation(tmp_path):
    """In a fresh interpreter with an invalid POPOTO_BACKEND and a model that
    declares Postgres, importing popoto and defining both models succeeds; the
    first *use* is where selection and binding happen."""
    script = textwrap.dedent("""
        import popoto
        from popoto.backends import BackendUnavailableError

        class Plain(popoto.Model):
            name = popoto.KeyField()

        class OnPostgres(popoto.Model):
            name = popoto.KeyField()
            score = popoto.SortedField(type=float, default=0.0)

            class Meta:
                backend = "postgres"

        try:
            Plain.query.count()
        except ValueError as exc:
            print("plain:", type(exc).__name__)
        try:
            OnPostgres.query.count()
        except BackendUnavailableError as exc:
            print("postgres:", type(exc).__name__)
        """)
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "POPOTO"))
    }
    env["POPOTO_BACKEND"] = "bogus"
    env["REDIS_URL"] = "redis://localhost:6379/11"
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "plain: ValueError" in out.stdout
    assert "postgres: BackendUnavailableError" in out.stdout


def test_bind_is_lazy_and_memoised(unpinned):
    calls: list[str] = []

    class CountingBackend(RedisBackend):
        name = "counting"

        def bind(self, spec):
            calls.append(spec.name)
            return super().bind(spec)

    set_backend(CountingBackend())

    class SelLazy(popoto.Model):
        name = popoto.KeyField()

    assert calls == []  # class creation binds nothing
    SelLazy(name="a").save()
    SelLazy.query.get(name="a")
    SelLazy.exists(name="a")
    assert calls == ["SelLazy"]
    backends.reset_bindings([SelLazy])
    SelLazy.query.count()
    assert calls == ["SelLazy", "SelLazy"]
    SelLazy(name="a").delete()


# -- no stale client ----------------------------------------------------------


def test_redis_backend_holds_no_client():
    backend = get_backend(SelProbe)
    assert vars(backend) == {}
    assert not any(
        isinstance(v, (redis_db.GuardedRedis,)) for v in vars(RedisBackend).values()
    )


class _RecordingClient:
    def __init__(self, real):
        self._real = real
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _recorded(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return _recorded


def test_backend_created_before_a_rebind_sees_the_new_client(monkeypatch):
    """The #655 probe: the backend instance (cached since module import, or
    created here before the rebind) must route through the rebound global."""
    backend = get_backend(SelProbe)
    real = redis_db.get_REDIS_DB()
    rec = _RecordingClient(real)
    monkeypatch.setattr(redis_db, "POPOTO_REDIS_DB", rec)
    assert get_backend(SelProbe) is backend
    SelProbe(name="rebind", rank=1).save()
    assert SelProbe.exists(name="rebind")
    assert SelProbe.query.get(name="rebind").rank == 1
    assert SelProbe.query.count() >= 1
    SelProbe.query.get(name="rebind").delete()
    for command in ("pipeline", "exists", "hgetall", "scard"):
        assert command in rec.calls, (command, rec.calls)


# -- class-creation refusal (validate_spec at declaration) --------------------


def test_postgres_model_with_unsupported_field_is_refused_at_declaration(
    monkeypatch,
):
    def no_network():
        raise AssertionError("class creation must not touch Redis")

    monkeypatch.setattr(redis_db, "get_REDIS_DB", no_network)

    class HookingField(popoto.Field):
        def on_save(self, *args, **kwargs):  # pragma: no cover - never called
            return super().on_save(*args, **kwargs)

    with pytest.raises(BackendCapabilityError, match="hooked .*overrides on_save"):

        class SelPgHooked(popoto.Model):
            name = popoto.KeyField()
            hooked = HookingField(type=str, null=True)

            class Meta:
                backend = "postgres"


def test_session_pin_is_gated_on_the_plugin_opt_in(unpinned):
    """A downstream session that never opted in keeps its runtime selection:
    the pin is a no-op for ``test_db=None`` and pins for an opted-in DB."""
    from popoto.pytest_plugin import _pin_session_backend_to_redis

    _pin_session_backend_to_redis(None)
    assert backends._default is None
    _pin_session_backend_to_redis(11)
    assert backends._default == "redis"
