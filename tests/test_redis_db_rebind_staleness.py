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
    for name in ("popoto.counters", "popoto.models.base"):
        assert name in sys.modules, (
            f"{name} must already be imported before any rebind, or this "
            "file's spy tests pass vacuously (#661)"
        )


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
