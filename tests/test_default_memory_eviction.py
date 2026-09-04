"""Deploy-level eviction escape hatch for ``DefaultMemory`` (#596).

``POPOTO_DEFAULT_MEMORY_MAX_RECORDS`` is the env-var override for the
per-``agent_id`` record cap introduced in #594. These tests pin the four
properties the plan calls load-bearing:

1. **Call-time reading.** The value is read inside ``save()``, never bound at
   import, so an operator can flip it in a process that has already imported
   popoto (and so a hook subprocess honours it without a Python seam --
   :func:`test_env_var_disables_eviction_in_subprocess`).
2. **Asymmetric precedence.** The env var may lower, raise, or disable the
   *default* cap, but it must never re-arm eviction on a model whose
   ``_max_records_per_agent`` is falsy. That opt-out is the escape hatch three
   shipped docs advertise; a symmetric rule would make ``=5000`` (exported to
   *raise* a cap) a silent data-destroying regression.
3. **The notice fires before the deletes.** Records are unrecoverable once the
   loop runs, and the enclosing ``except`` would swallow a post-loop warning on
   the loudest path, so the WARNING and the counter must both survive a
   mid-loop failure.
4. **The counter reports records *selected* for eviction, not deleted.** The
   ``INCRBY`` is fixed at ``excess`` and fires pre-loop; the loop legitimately
   deletes fewer (own-key skip, orphan purge, mid-loop abort). The invariant is
   ``counter >= deleted``, with equality only on the clean path.

Note on class-attribute cases: ``DefaultMemory`` cannot be subclassed usefully
today -- a subclass inherits *no* fields (``_meta.fields == []``), which is a
pre-existing ORM limitation unrelated to #596 -- so the "library author set a
different cap" scenarios are expressed with
``monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", ...)``, which
exercises the identical ``self._max_records_per_agent`` read in ``save()``.
"""

import logging
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from popoto.fields import constants as constants_module
from popoto.recipes import default_memory as dm_module
from popoto.recipes.default_memory import EVICTION_COUNTER_PREFIX, DefaultMemory
from popoto.redis_db import POPOTO_REDIS_DB

ENV_VAR = "POPOTO_DEFAULT_MEMORY_MAX_RECORDS"
DM_LOGGER = "POPOTO.DefaultMemory"
CONSTANTS_LOGGER = "POPOTO.constants"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_agent():
    """A hex-only agent id, so the DB-key escaping cannot break glob cleanup."""
    return "evict" + uuid.uuid4().hex[:12]


def _counter_key(agent):
    return f"{EVICTION_COUNTER_PREFIX}:{agent}:evicted"


def _counter(agent):
    raw = POPOTO_REDIS_DB.get(_counter_key(agent))
    return int(raw) if raw else 0


def _zset_key(record):
    field = record._meta.fields["relevance"]
    return field.get_partitioned_sortedset_db_key(record, "relevance").redis_key


def _seed(agent, n, prefix="memory"):
    """Save ``n`` records for ``agent``; return them in save order."""
    records = []
    for i in range(n):
        record = DefaultMemory(agent_id=agent, content=f"{prefix} {i}", importance=1.0)
        record.save()
        records.append(record)
    return records


def _count(agent):
    return DefaultMemory.query.filter(agent_id=agent).count()


def _purge(agent):
    for key in POPOTO_REDIS_DB.scan_iter(match=f"*{agent}*"):
        POPOTO_REDIS_DB.delete(key)
    POPOTO_REDIS_DB.delete(_counter_key(agent))


class _ZcardSpy:
    """Records every ``ZCARD`` key while delegating to the real client."""

    def __init__(self, monkeypatch):
        self.keys = []
        real = POPOTO_REDIS_DB.zcard

        def spy(key, *args, **kwargs):
            self.keys.append(key if isinstance(key, str) else str(key))
            return real(key, *args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "zcard", spy)

    def saw(self, key):
        return key in self.keys


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_eviction_state(monkeypatch):
    """Env unset and both process-lifetime dedupe sets cleared.

    ``_EVICTION_WARNED`` and ``_WARNED_BAD_ENV`` are module-level and survive
    across tests, so a "warns exactly once" assertion is meaningless without
    this. Restored afterwards so the fixture cannot itself leak state.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    warned_evictions = set(dm_module._EVICTION_WARNED)
    warned_env = set(constants_module._WARNED_BAD_ENV)
    dm_module._EVICTION_WARNED.clear()
    constants_module._WARNED_BAD_ENV.clear()
    yield
    dm_module._EVICTION_WARNED.clear()
    dm_module._EVICTION_WARNED.update(warned_evictions)
    constants_module._WARNED_BAD_ENV.clear()
    constants_module._WARNED_BAD_ENV.update(warned_env)


@pytest.fixture
def agent(clean_eviction_state):
    """A unique agent id whose records and eviction counter are purged after."""
    agent_id = _new_agent()
    yield agent_id
    _purge(agent_id)


# ---------------------------------------------------------------------------
# Cap resolution
# ---------------------------------------------------------------------------


class TestCapResolution:
    def test_env_unset_enforces_the_default_cap_of_1000(self, agent):
        """No env var -> the shipped 1000-per-agent cap still applies."""
        _seed(agent, 1005)
        assert _count(agent) == 1000
        # Five saves crossed the cap, each selecting exactly one record.
        assert _counter(agent) == 5

    @pytest.mark.parametrize("value", ["0", "off", "false", "no", "OFF", " False "])
    def test_disable_values_skip_eviction_and_the_zcard(
        self, agent, monkeypatch, value
    ):
        """A disabling value must short-circuit before the ZCARD round trip."""
        monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", 2)
        monkeypatch.setenv(ENV_VAR, value)
        first = _seed(agent, 1)[0]
        zset_key = _zset_key(first)
        spy = _ZcardSpy(monkeypatch)
        _seed(agent, 5, prefix="after")
        assert _count(agent) == 6
        assert not spy.saw(zset_key), (
            f"ZCARD issued on {zset_key} despite {ENV_VAR}={value!r}; "
            "the disable path must return before touching Redis"
        )
        assert _counter(agent) == 0

    def test_env_five_caps_at_five(self, agent, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "5")
        _seed(agent, 8)
        assert _count(agent) == 5

    def test_env_one_means_a_cap_of_one_not_enabled(self, agent, monkeypatch):
        """``1`` is a member of ``_TRUTHY``; integers are parsed first."""
        monkeypatch.setenv(ENV_VAR, "1")
        _seed(agent, 4)
        assert _count(agent) == 1

    def test_env_set_after_import_still_takes_effect(self, agent, monkeypatch):
        """The discriminating anti-regression for call-time reading.

        Setting the variable *before* ``import popoto`` passes even under an
        import-time binding. popoto is already imported here (this module
        imports it at collection time), so a cached read would ignore this.
        """
        assert "popoto.recipes.default_memory" in sys.modules
        monkeypatch.setenv(ENV_VAR, "2")
        _seed(agent, 5)
        assert _count(agent) == 2

    @pytest.mark.parametrize("value", ["1k", "-3"])
    def test_malformed_value_warns_once_and_falls_back(
        self, agent, monkeypatch, caplog, value
    ):
        """One warning per distinct bad value across several saves."""
        monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", 3)
        monkeypatch.setenv(ENV_VAR, value)
        with caplog.at_level(logging.WARNING, logger=CONSTANTS_LOGGER):
            _seed(agent, 6)
        warnings = [
            r
            for r in caplog.records
            if r.name == CONSTANTS_LOGGER and ENV_VAR in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"{len(warnings)} warnings for {value!r} across 6 saves; "
            "_WARNED_BAD_ENV must dedupe per distinct raw value"
        )
        assert repr(value) in warnings[0].getMessage()
        # The save still succeeded and the class attribute still applied.
        assert _count(agent) == 3


# ---------------------------------------------------------------------------
# Asymmetric precedence -- the round-1 BLOCKER
# ---------------------------------------------------------------------------


class TestAsymmetricPrecedence:
    @pytest.mark.parametrize("value", ["5000", "2", "0"])
    def test_falsy_class_attribute_is_never_re_armed(self, agent, monkeypatch, value):
        """A library author's opt-out survives any env value.

        ``=5000`` is the regression case from the plan's BLOCKER: an operator
        exporting it merely to *raise* the default cap must not process-globally
        re-arm hard ``delete()`` on a model that deliberately turned eviction
        off. ``=2`` is the discriminating variant (a re-armed cap would visibly
        evict); ``=0`` proves the disabled state simply stays disabled.
        """
        monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", 0)
        monkeypatch.setenv(ENV_VAR, value)
        first = _seed(agent, 1)[0]
        zset_key = _zset_key(first)
        spy = _ZcardSpy(monkeypatch)
        _seed(agent, 7, prefix="after")
        assert _count(agent) == 8
        assert not spy.saw(zset_key), (
            f"ZCARD issued with a falsy class attribute and {ENV_VAR}={value!r}; "
            "the falsy attribute must short-circuit before the env var"
        )
        assert _counter(agent) == 0

    def test_truthy_class_attribute_honored_when_env_unset(self, agent, monkeypatch):
        monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", 3)
        _seed(agent, 6)
        assert _count(agent) == 3

    def test_truthy_class_attribute_overridden_when_env_set(self, agent, monkeypatch):
        monkeypatch.setattr(DefaultMemory, "_max_records_per_agent", 3)
        monkeypatch.setenv(ENV_VAR, "6")
        _seed(agent, 8)
        assert _count(agent) == 6


# ---------------------------------------------------------------------------
# First-eviction notice
# ---------------------------------------------------------------------------


def _eviction_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == DM_LOGGER and r.levelno == logging.WARNING
    ]


class TestFirstEvictionNotice:
    def test_notice_fires_once_per_class_and_agent(self, agent, monkeypatch, caplog):
        monkeypatch.setenv(ENV_VAR, "2")
        caplog.set_level(logging.DEBUG, logger=DM_LOGGER)
        other = _new_agent()
        try:
            # Three over-cap saves for one agent -> one WARNING, then DEBUG.
            _seed(agent, 5)
            warnings = _eviction_warnings(caplog)
            assert len(warnings) == 1, (
                f"{len(warnings)} first-eviction warnings for one "
                "(class, agent_id) pair"
            )
            assert agent in warnings[0].getMessage()
            assert ENV_VAR in warnings[0].getMessage()
            assert "cap exceeded" in warnings[0].getMessage()
            debugs = [
                r
                for r in caplog.records
                if r.name == DM_LOGGER and r.levelno == logging.DEBUG
            ]
            assert debugs, "repeat evictions must still log at DEBUG"

            # A different agent_id is a different key -> warns again.
            caplog.clear()
            _seed(other, 4)
            warnings = _eviction_warnings(caplog)
            assert len(warnings) == 1
            assert other in warnings[0].getMessage()
        finally:
            _purge(other)

    def test_notice_survives_a_mid_loop_failure(self, agent, monkeypatch, caplog):
        """The loudest case must not be the quietest log.

        The notice is emitted before the ``zrange`` loop precisely so that an
        error inside the loop -- swallowed by the enclosing ``except`` -- still
        leaves the operator a record of what was about to be deleted.
        """
        monkeypatch.setenv(ENV_VAR, "0")
        _seed(agent, 3)
        monkeypatch.setenv(ENV_VAR, "1")
        caplog.set_level(logging.WARNING, logger=DM_LOGGER)

        real_zrange = POPOTO_REDIS_DB.zrange
        armed = {"on": True}

        def boom(key, *args, **kwargs):
            if armed["on"]:
                raise RuntimeError("zrange exploded")
            return real_zrange(key, *args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "zrange", boom)
        DefaultMemory(agent_id=agent, content="last", importance=1.0).save()
        armed["on"] = False

        warnings = _eviction_warnings(caplog)
        assert any(
            "cap exceeded" in r.getMessage() for r in warnings
        ), "the pre-loop notice was lost when the delete loop raised"
        # 4 records, cap 1 -> 3 selected, none deleted.
        assert _counter(agent) == 3
        assert _count(agent) == 4


# ---------------------------------------------------------------------------
# Counter semantics: selected for eviction, not deleted
# ---------------------------------------------------------------------------


class TestEvictionCounter:
    def test_counter_prefix_matches_the_service_constant(self):
        """The duplicated string is what keeps ``doctor`` reporting evictions."""
        from popoto.integrations import service

        assert EVICTION_COUNTER_PREFIX == service.COUNTER_KEY_PREFIX

    def test_counter_equals_excess_on_the_clean_path(self, agent, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "0")
        _seed(agent, 6)
        assert _count(agent) == 6
        monkeypatch.setenv(ENV_VAR, "2")
        DefaultMemory(agent_id=agent, content="trigger", importance=1.0).save()
        # 7 records, cap 2 -> excess 5; the newest (own) record is not in the
        # eviction window, so all 5 selected records are actually deleted.
        assert _counter(agent) == 5
        assert _count(agent) == 2
        deleted = 7 - _count(agent)
        assert _counter(agent) == deleted

    def test_counter_exceeds_deletions_when_own_key_is_in_the_window(
        self, agent, monkeypatch
    ):
        """The saving record's own key is skipped, so deleted < selected.

        The decay score is a timestamp, so the record being saved is always the
        *newest* member and never lands in the stalest-first window naturally.
        ``zrange`` is reordered here to put it there, which is the only way to
        reach the ``victim == own_key: continue`` branch deterministically.
        """
        monkeypatch.setenv(ENV_VAR, "0")
        seeded = _seed(agent, 3)
        zset_key = _zset_key(seeded[0])
        real_zrange = POPOTO_REDIS_DB.zrange
        armed = {"on": True}

        def rotated(key, start, end, *args, **kwargs):
            if armed["on"] and key == zset_key and end >= 0:
                members = list(real_zrange(key, 0, -1))
                members = [members[-1]] + members[:-1]
                return members[start : end + 1]
            return real_zrange(key, start, end, *args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "zrange", rotated)
        monkeypatch.setenv(ENV_VAR, "1")
        DefaultMemory(agent_id=agent, content="trigger", importance=1.0).save()
        armed["on"] = False

        # 4 records, cap 1 -> 3 selected; the window's first entry is the
        # saving record itself, which the loop skips.
        assert _counter(agent) == 3
        remaining = _count(agent)
        deleted = 4 - remaining
        assert deleted == 2
        assert _counter(agent) > deleted
        assert _counter(agent) >= deleted

    def test_counter_still_incremented_when_the_loop_aborts(self, agent, monkeypatch):
        """A mid-loop error leaves counter > deleted, never the reverse."""
        monkeypatch.setenv(ENV_VAR, "0")
        _seed(agent, 4)
        monkeypatch.setenv(ENV_VAR, "1")

        real_hgetall = POPOTO_REDIS_DB.hgetall
        state = {"calls": 0, "armed": True}

        def flaky(key, *args, **kwargs):
            if state["armed"]:
                state["calls"] += 1
                if state["calls"] > 1:
                    raise RuntimeError("hgetall exploded mid-loop")
            return real_hgetall(key, *args, **kwargs)

        monkeypatch.setattr(POPOTO_REDIS_DB, "hgetall", flaky)
        DefaultMemory(agent_id=agent, content="trigger", importance=1.0).save()
        state["armed"] = False

        # 5 records, cap 1 -> 4 selected; the loop dies on the second victim.
        assert _counter(agent) == 4
        remaining = _count(agent)
        deleted = 5 - remaining
        assert deleted < _counter(agent)
        assert _counter(agent) >= deleted


# ---------------------------------------------------------------------------
# No Python seam: the switch works for a hook subprocess
# ---------------------------------------------------------------------------


_SUBPROCESS_SCRIPT = """
import os
import sys

agent, expected_db = sys.argv[1], int(sys.argv[2])

# REDIS_URL is read at import time and is already in this process's env
# (see CLAUDE.md / #577): without it popoto binds DB 0, the live agent store.
assert expected_db != 0, "refusing to run against DB 0"
assert os.environ["REDIS_URL"].endswith("/%d" % expected_db), os.environ["REDIS_URL"]

import popoto  # noqa: F401
from popoto.recipes.default_memory import DefaultMemory, EVICTION_COUNTER_PREFIX
from popoto.redis_db import POPOTO_REDIS_DB

actual = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db")
assert actual == expected_db, "bound DB %r, expected %r" % (actual, expected_db)

for i in range(1005):
    DefaultMemory(agent_id=agent, content="hook turn %d" % i, importance=1.0).save()

count = DefaultMemory.query.filter(agent_id=agent).count()
evicted = POPOTO_REDIS_DB.get("%s:%s:evicted" % (EVICTION_COUNTER_PREFIX, agent))
print("COUNT=%d EVICTED=%s" % (count, int(evicted or 0)))
"""


def test_env_var_disables_eviction_in_subprocess(agent):
    """The switch must work with no Python seam at all.

    Modeled on ``tests/test_pytest_plugin.py::test_isolated_db_subprocess``: a
    fresh interpreter -- the shape of a Claude Code / Codex hook invocation --
    gets only environment variables, never a subclass or a monkeypatch.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Point the child at the DB this session is already isolated on rather than
    # a hardcoded 15: DB 0 is the live agent store (CLAUDE.md, #577), and a
    # hardcoded 15 would both write a database this session is not using and
    # leave ``_purge`` below cleaning the wrong one.
    db = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db")
    assert db, f"refusing to spawn a child against DB {db!r}"
    env = {
        **os.environ,
        # Must be set before ``import popoto`` in the child, or the script
        # writes the live DB 0 (CLAUDE.md, #577).
        "REDIS_URL": f"redis://localhost:6379/{db}",
        ENV_VAR: "0",
        "PYTHONPATH": os.path.join(repo_root, "src"),
    }
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(_SUBPROCESS_SCRIPT),
                agent,
                str(db),
            ],
            env=env,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert (
            proc.returncode == 0
        ), f"child failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("COUNT=")]
        assert line, f"no result line in child stdout:\n{proc.stdout}"
        count_part, evicted_part = line[-1].split()
        count = int(count_part.split("=")[1])
        evicted = int(evicted_part.split("=")[1])
        assert count == 1005, (
            f"{count} records survived with {ENV_VAR}=0; the default cap of "
            "1000 was still enforced in a process with no Python seam"
        )
        assert count > 1000
        assert evicted == 0, f"{evicted} records selected for eviction"
    finally:
        # The child wrote to DB 15 directly, outside the pytest plugin's
        # bookkeeping, so clean up explicitly.
        _purge(agent)
