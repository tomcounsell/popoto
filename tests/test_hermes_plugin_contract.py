"""The shipped Hermes plugin, driven by the real Hermes plugin loader.

Every other test in this repo reads a fixture. This one imports
``hermes_cli.plugins`` from a genuinely installed ``hermes-agent``, stages
``plugins/hermes/`` into a scratch ``HERMES_HOME`` exactly as the README
tells an operator to, and lets Hermes discover, load, and dispatch to it.
That is the only way to catch the class of defect #704 was: a plugin wired
to a hook system that exists but never calls it. A fixture round-trip
cannot see that, because the fixture is written by us.

``hermes-agent`` is not a dependency of popoto and is not installed by
`tests.yml` -- it is a large package pulled only by
`.github/workflows/hermes-contract.yml`, which is advisory. So this module
skips itself when the import is missing rather than failing. A skip here is
not a pass: it means nobody checked. The workflow exists so that somebody
does.

No model turn happens here and no provider credentials are needed. The
dispatcher is real; what is above it -- an actual Hermes gateway serving an
actual model -- is out of scope for this repo's CI, and the fixtures'
``_provenance`` says so in those words.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "hermes_cli.plugins",
    reason="hermes-agent is not installed (see .github/workflows/hermes-contract.yml)",
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPT_DIR), "src"))

from popoto.integrations import MemoryService  # noqa: E402
from popoto.integrations.config import MemoryConfig  # noqa: E402
from popoto.recipes import DefaultMemory  # noqa: E402

REPO_ROOT = Path(SCRIPT_DIR).parent
PLUGIN_SRC = REPO_ROOT / "plugins" / "hermes"
AGENT = "test-hermes-plugin-contract"

# The kwargs the 0.19.0 invoke sites pass, verbatim:
#   pre_llm_call  -- agent/turn_context.py:692-703
#   post_llm_call -- agent/turn_finalizer.py:483-494
SESSION = "sess-c0ffee"
TASK = "task-1a2b"
TURN = f"{SESSION}:{TASK}:5e6f7a8b"

# Distinctive enough that a substring match cannot be satisfied by the query
# echoing itself back, by a stock phrase, or by another test's data.
SENTINEL = "the mongoose latch is bolted at torque setting eleven"


def _purge():
    for record in DefaultMemory.query.filter(agent_id=AGENT):
        try:
            record.delete()
        except Exception:
            pass


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with the plugin installed the documented way."""
    home = tmp_path / "hermes_home"
    plugin_dir = home / "plugins" / "popoto-memory"
    plugin_dir.mkdir(parents=True)
    for name in ("plugin.yaml", "__init__.py"):
        shutil.copy(PLUGIN_SRC / name, plugin_dir / name)
    # Plugins are opt-in: without this key the plugin is discovered, recorded
    # as enabled=False, and never loaded. That silence is the failure mode the
    # README's "not optional" warning is about, so the test installs it the
    # same way rather than reaching past it.
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - popoto-memory\n")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("POPOTO_MEMORY_AGENT_ID", AGENT)
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    _purge()
    yield home
    _purge()


@pytest.fixture
def manager(hermes_home):
    """The real plugin manager, after a real discovery sweep."""
    from hermes_cli.plugins import get_plugin_manager

    mgr = get_plugin_manager()
    # force=True because the manager is a process-wide singleton and may
    # already hold a sweep from the developer's own ~/.hermes.
    mgr.discover_and_load(force=True)
    yield mgr
    mgr.discover_and_load(force=True)


@pytest.fixture
def capture_dir(tmp_path, monkeypatch):
    directory = tmp_path / "capture"
    directory.mkdir()
    monkeypatch.setenv("POPOTO_HOOK_CAPTURE", str(directory))
    return directory


def _record(manager):
    for entry in manager.list_plugins():
        if entry.get("name") == "popoto-memory":
            return entry
    return None


def _pre_kwargs():
    return dict(
        session_id=SESSION,
        task_id=TASK,
        turn_id=TURN,
        user_message="Remind me how the mongoose latch is set up.",
        conversation_history=[],
        is_first_turn=True,
        model="claude-sonnet-4-6",
        platform="cli",
        sender_id="local",
    )


def _post_kwargs():
    return dict(
        session_id=SESSION,
        task_id=TASK,
        turn_id=TURN,
        user_message="Remind me how the mongoose latch is set up.",
        assistant_response=SENTINEL,
        conversation_history=[],
        model="claude-sonnet-4-6",
        platform="cli",
    )


# (a) the plugin is found and actually loaded ----------------------------------


def test_the_real_loader_loads_the_shipped_plugin(manager):
    """Discovered, enabled, and no load error -- all three, not just the first.

    A plugin with a bad manifest or an ImportError in ``register()`` still
    appears in ``list_plugins()``; only ``error`` distinguishes it. Hermes
    swallows load failures, so this assertion is the only place one surfaces.
    """
    entry = _record(manager)
    assert entry is not None, "popoto-memory was not discovered at all"
    assert entry["enabled"] is True, entry
    assert entry["error"] is None, entry["error"]
    assert entry["source"] == "user", entry


# (b) both hooks are registered against the real registry ----------------------


def test_both_hooks_register_on_the_real_dispatcher(manager):
    """``hooks: 2``, and both by the names Hermes will dispatch under.

    ``provides_hooks`` in plugin.yaml is documentation and gates nothing;
    registration happens only through ``ctx.register_hook``. This asserts the
    registry, not the manifest.
    """
    from hermes_cli.plugins import VALID_HOOKS

    assert manager.has_hook("pre_llm_call")
    assert manager.has_hook("post_llm_call")
    assert _record(manager)["hooks"] == 2
    # If a future Hermes renames these, the plugin goes silent rather than
    # erroring -- so pin the names against the version's own vocabulary.
    assert {"pre_llm_call", "post_llm_call"} <= set(VALID_HOOKS)


# (c) the envelope the plugin builds from real dispatch ------------------------


def test_the_dispatched_envelope_carries_a_turn_id_and_no_cwd(manager, capture_dir):
    """What the plugin actually received, not what a fixture says it did."""
    from hermes_cli.plugins import invoke_hook

    invoke_hook("pre_llm_call", **_pre_kwargs())

    envelope = json.loads((capture_dir / "pre_llm_call.json").read_text())
    assert envelope["hook_event_name"] == "pre_llm_call"
    assert envelope["turn_id"] == TURN
    assert envelope["user_message"] == _pre_kwargs()["user_message"]
    # No working directory reaches a plugin hook, which is why the guide tells
    # operators to set POPOTO_MEMORY_AGENT_ID explicitly.
    assert "cwd" not in envelope
    # Injected by the manager rather than the invoke site: the callback must
    # tolerate kwargs no invoke site lists.
    assert envelope["telemetry_schema_version"]
    # Unbounded and unread; committing it would turn a fixture into a
    # transcript.
    assert "conversation_history" not in envelope


def test_the_post_envelope_carries_the_assistant_response(manager, capture_dir):
    from hermes_cli.plugins import invoke_hook

    invoke_hook("post_llm_call", **_post_kwargs())

    envelope = json.loads((capture_dir / "post_llm_call.json").read_text())
    assert envelope["hook_event_name"] == "post_llm_call"
    assert envelope["assistant_response"] == SENTINEL
    assert envelope["turn_id"] == TURN


# (d) the write path stores what the model said --------------------------------


def test_a_dispatched_post_hook_stores_the_response(manager):
    """Read back out of Redis, not inferred from a return value.

    ``post_llm_call``'s return is discarded by Hermes, so the only observable
    effect is the record. Querying for it is what makes this test able to fail
    when the write path breaks.
    """
    from hermes_cli.plugins import invoke_hook

    invoke_hook("post_llm_call", **_post_kwargs())

    contents = [r.content for r in DefaultMemory.query.filter(agent_id=AGENT)]
    assert any(SENTINEL in c for c in contents), contents


# (e) the read path returns a real memory in Hermes's own response shape -------


def test_a_dispatched_pre_hook_returns_the_seeded_memory(manager):
    """The whole loop, with the seed placed in Redis by popoto itself.

    Deliberately not an ``isinstance(dict)`` check: a plugin that returned
    ``{"context": ""}`` for every turn would satisfy the shape and remember
    nothing. The assertion is that a sentinel this test wrote into Redis --
    and that appears nowhere in the query -- comes back inside the payload
    Hermes will inject.
    """
    from hermes_cli.plugins import invoke_hook

    service = MemoryService(MemoryConfig(agent_id=AGENT))
    service.model(agent_id=AGENT, content=SENTINEL, importance=0.9).save()

    returned = invoke_hook("pre_llm_call", **_pre_kwargs())

    assert returned, "the pre_llm_call hook returned nothing at all"
    payload = returned[0]
    assert set(payload) == {"context"}, payload
    assert SENTINEL in payload["context"], payload["context"]
    # The sentinel is not in the prompt, so it cannot have been echoed.
    assert SENTINEL not in _pre_kwargs()["user_message"]


# failure paths ----------------------------------------------------------------


def test_a_pre_hook_with_nothing_remembered_returns_no_context(manager):
    """An empty store must yield no injection rather than an empty string.

    Hermes treats any returned dict as context to inject; ``{"context": ""}``
    would spend a turn's injection budget on nothing.
    """
    from hermes_cli.plugins import invoke_hook

    assert invoke_hook("pre_llm_call", **_pre_kwargs()) == []


def test_an_unreachable_redis_does_not_break_the_turn(manager, monkeypatch):
    """A memory failure degrades to no context, never to a raised turn.

    Hermes swallows callback exceptions, so a plugin that raised would look
    identical to one that had nothing to say. The contract is that the
    dispatch completes and returns nothing.
    """
    from hermes_cli.plugins import invoke_hook

    def boom(*args, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr("popoto.integrations.hooks.handle_payload", boom, raising=True)
    assert invoke_hook("pre_llm_call", **_pre_kwargs()) == []
