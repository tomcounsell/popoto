"""Prove the #655 failure mode: a module-level ``POPOTO_REDIS_DB`` import
holds a snapshot that ``set_REDIS_DB_settings()`` can never update.

``set_REDIS_DB_settings()`` *rebinds* ``redis_db``'s module global. Python does
not propagate a rebind to a name already imported elsewhere, so every module
that did ``from ..redis_db import POPOTO_REDIS_DB`` at load time keeps issuing
commands against the pre-reconfiguration client — writing to one database and
reading from another with no error.

**Why this file imports its targets at module scope.** #661 recorded the trap:
a spy on ``redis_db.POPOTO_REDIS_DB`` passes *vacuously* when the target module
is first imported inside the test, because the module-level import then
snapshots the spy itself. The failure only appears once something else has
already imported the module — which in a full-suite run is whatever ran before
``tests/test_connection.py`` rebinds the global. Importing the targets here, at
collection time, makes the order explicit instead of incidental: by the time
any test body runs, every module under test has already taken its snapshot.
``test_targets_were_imported_before_the_rebind`` asserts that precondition
directly, so the vacuous configuration fails loudly rather than passing.
"""

import sys

import pytest

from popoto import redis_db

# Imported at module scope on purpose — see the docstring. These are the
# probe points: the smallest call in each module that reaches Redis through
# the name under test.
from popoto import counters  # noqa: E402
from popoto.models import base as models_base  # noqa: E402
from popoto.backends import redis as backends_redis  # noqa: E402

# Built AND USED at collection time, BEFORE any rebind: the #631 rule is that
# ``RedisBackend`` stores no client, so an instance constructed against the
# pre-rebind global must still reach the post-rebind one. A backend that
# captured ``get_REDIS_DB()`` in ``__init__`` would make this object a frozen
# snapshot and fail the probe below. Construction alone is not enough: a
# ``client`` property that lazily caches ``get_REDIS_DB()`` on *first access*
# stores nothing at construction and, if first touched only inside a test
# body, caches the spy and passes vacuously. Touching ``.client`` and issuing
# one real command here forces that first access to happen against the real
# pre-rebind client, so a lazy cache is frozen on it before any spy exists.
_BACKEND_BUILT_BEFORE_REBIND = backends_redis.RedisBackend()
_CLIENT_SEEN_BEFORE_REBIND = _BACKEND_BUILT_BEFORE_REBIND.client
_BACKEND_BUILT_BEFORE_REBIND.record_exists("popoto_test:631:touched-at-collection")


class RecordingClient:
    """Delegates to the real client and records the commands it is asked for.

    A *stale* module never touches this object: it holds the pre-rebind client,
    so ``calls`` stays empty even though the command succeeded against Redis.
    Delegation (rather than a bare mock) keeps the exercised code path real, so
    a converted module is tested end to end and not just at the seam.
    """

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


@pytest.fixture
def recorder(monkeypatch):
    """Rebind the global to a recorder, the way ``set_REDIS_DB_settings`` does.

    ``monkeypatch.setattr`` on the module attribute is the same operation the
    real reconfiguration performs (``global POPOTO_REDIS_DB; POPOTO_REDIS_DB =
    ...``) without opening a second connection or touching another database.
    """
    real = redis_db.get_REDIS_DB()
    rec = RecordingClient(real)
    monkeypatch.setattr(redis_db, "POPOTO_REDIS_DB", rec)
    assert redis_db.get_REDIS_DB() is rec, "accessor must observe the rebind"
    return rec


def test_targets_were_imported_before_the_rebind():
    """Guard against the #661 vacuity: a target imported *after* the spy is
    installed snapshots the spy and passes no matter what."""
    for name in ("popoto.counters", "popoto.models.base", "popoto.backends.redis"):
        assert name in sys.modules, (
            f"{name} must already be imported before any rebind, or this "
            "file's spy tests pass vacuously (#661)"
        )
    # The same precondition for the backend instance: its first ``.client``
    # access happened at collection, against the real client, not a spy.
    assert not isinstance(_CLIENT_SEEN_BEFORE_REBIND, RecordingClient), (
        "the pre-rebind backend's first client access saw a spy -- the "
        "module-scope touch must run before any rebind, or a lazily cached "
        "client passes vacuously (#661)"
    )
    assert isinstance(_CLIENT_SEEN_BEFORE_REBIND, redis_db.GuardedRedis)


def test_counters_increment_uses_the_current_client(recorder):
    """``popoto.counters`` must reach Redis through the live global."""
    counters.increment("popoto_test:655:counter", 1)
    assert "incrby" in recorder.calls, (
        "counters.increment() bypassed the rebound client — it is holding a "
        "stale module-level POPOTO_REDIS_DB snapshot (#655)"
    )


def test_model_exists_uses_the_current_client(recorder):
    """``Model.exists()`` at models/base.py must reach Redis through the live
    global. This is the highest-traffic stale importer: 53 call sites."""

    class RebindProbe(models_base.Model):
        pass

    RebindProbe.exists("nonexistent-key")
    assert "exists" in recorder.calls, (
        "Model.exists() bypassed the rebound client — models/base.py is "
        "holding a stale module-level POPOTO_REDIS_DB snapshot (#655)"
    )


def test_redis_backend_uses_the_current_client(recorder):
    """``backends.redis.RedisBackend`` must reach Redis through the live global.

    Probed on an instance built *and already used* at collection time (before
    the rebind) so that a client captured in ``__init__`` -- the #655 bug one
    layer up, which the #631 plan forbids -- cannot pass by being constructed
    after the spy, and a client lazily cached on first ``.client`` access
    cannot pass by being first touched after the spy.
    """
    backend = _BACKEND_BUILT_BEFORE_REBIND
    assert _CLIENT_SEEN_BEFORE_REBIND is not recorder
    assert backend.client is recorder, (
        "RedisBackend.client is not the rebound client — the backend captured "
        "a client at construction or on first access instead of resolving "
        "get_REDIS_DB() per call"
    )
    backend.record_exists("popoto_test:631:nonexistent")
    assert "exists" in recorder.calls, (
        "RedisBackend.record_exists() bypassed the rebound client — "
        "backends/redis.py is holding a stale client reference (#631 rule 1)"
    )
    recorder.calls.clear()
    backend.sorted_count("popoto_test:631:nonexistent-zset")
    assert "zcard" in recorder.calls
    recorder.calls.clear()
    backend.begin()
    assert (
        "pipeline" in recorder.calls
    ), "RedisBackend.begin() must open the unit of work on the current client"


def test_redis_backend_stores_nothing():
    """The mechanism, not only the behaviour: no instance state at all.

    A future ``self._client = get_REDIS_DB()`` would pass the behavioural
    probes above as long as the session never reconfigures between
    construction and use; this pins the shape directly, the way
    ``test_popoto_redis_db_rebind.py`` asserts ``"POPOTO_REDIS_DB" not in
    vars(popoto)``.

    The instance dict is checked *after* the instance has been used, never on
    a fresh one: a lazily cached client (``if "_c" not in vars(self): self._c
    = get_REDIS_DB()``) leaves a fresh instance empty and only grows state on
    first access, so ``vars(RedisBackend()) == {}`` on its own proves nothing.
    """
    used = _BACKEND_BUILT_BEFORE_REBIND  # touched at collection and above
    assert vars(used) == {}, (
        f"RedisBackend grew instance state after use: {vars(used)!r} -- a "
        "cached client is a snapshot set_REDIS_DB_settings() never updates"
    )

    fresh = backends_redis.RedisBackend()
    fresh.client
    fresh.record_exists("popoto_test:631:nonexistent")
    fresh.sorted_count("popoto_test:631:nonexistent-zset")
    assert (
        vars(fresh) == {}
    ), f"RedisBackend grew instance state after use: {vars(fresh)!r}"

    assert "__init__" not in vars(backends_redis.RedisBackend)
    assert isinstance(vars(backends_redis.RedisBackend)["client"], property)
    assert vars(backends_redis.RedisBackend)["client"].fset is None
