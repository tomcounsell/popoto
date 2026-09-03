"""Hook adapter: captured harness payloads in, harness payloads out.

The fixtures under ``tests/fixtures/harness_payloads/`` are not hand-written
to match this module. Each records its provenance in a ``_provenance`` field
beginning ``captured-from:``; the Claude Code pair came off a live
``claude`` 2.1.220 run, so the round trip below tests the harness rather
than our reading of the docs. See that directory's README for the rest.

The subprocess tests at the bottom are the ones that hold the real
guarantees: exit 0 whatever happens, one write or none, never a partial
JSON document on stdout.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.integrations import hooks  # noqa: E402
from popoto.integrations.config import MemoryConfig  # noqa: E402
from popoto.integrations.service import MemoryService  # noqa: E402
from popoto.recipes import DefaultMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

FIXTURES = Path(SCRIPT_DIR) / "fixtures" / "harness_payloads"
AGENT = "test-integrations-hooks"

READ_FIXTURES = [
    "claude_code_user_prompt_submit.json",
    "codex_user_prompt_submit.json",
    "hermes_pre_llm_call.json",
    "openclaw_before_prompt_build.json",
]
WRITE_FIXTURES = [
    "claude_code_stop.json",
    "codex_stop.json",
    "hermes_post_llm_call.json",
    "openclaw_llm_output.json",
]


def load(name):
    return json.loads((FIXTURES / name).read_text())


def _down_redis_url():
    """A ``redis://`` URL that is guaranteed closed at call time.

    ``redis://127.0.0.1:6399/0`` used to be hardcoded here as a stand-in for
    "Redis is down", but this suite does not own port 6399 -- any other
    process (a developer's spare redis-server, another worktree's fixture)
    can be listening on it, which flips these tests from "hook degrades
    gracefully" to "hook writes real records into a stranger's DB 0". Bind
    an ephemeral port and close it immediately: the OS hands out a port
    nothing is listening on, so ``ECONNREFUSED`` is guaranteed regardless of
    what else is running on the machine.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    return f"redis://127.0.0.1:{port}/{SUBPROCESS_DB}"


def make_service(tmp_path, **overrides):
    defaults = dict(agent_id=AGENT, log_path=tmp_path / "memory.log")
    defaults.update(overrides)
    return MemoryService(MemoryConfig(**defaults))


@pytest.fixture(autouse=True)
def clean_agent():
    _purge()
    yield
    _purge()


def _purge():
    for record in DefaultMemory.query.filter(agent_id=AGENT):
        try:
            record.delete()
        except Exception:
            pass
    for pattern in (
        f"$popoto_memory:pending:{AGENT}:*",
        f"$popoto_memory:counter:{AGENT}:*",
        f"$popoto_memory:last:{AGENT}:*",
    ):
        for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=200):
            POPOTO_REDIS_DB.delete(key)


# --- fixture hygiene ---------------------------------------------------------


def test_every_fixture_records_its_provenance():
    files = sorted(FIXTURES.glob("*.json"))
    assert len(files) == 8
    for path in files:
        assert "captured-from:" in path.read_text(), path.name


# --- normalization ------------------------------------------------------------


@pytest.mark.parametrize("name", READ_FIXTURES)
def test_read_fixtures_normalize_to_the_prompt(name):
    event = hooks.normalize(load(name))
    assert event.kind == "read"
    assert "health checks" in event.text
    assert event.session_id
    assert event.cwd == "/Users/dev/src/demo"


@pytest.mark.parametrize("name", WRITE_FIXTURES)
def test_write_fixtures_normalize_to_the_assistant_message(name):
    event = hooks.normalize(load(name))
    assert event.kind == "write"
    assert "automatic rollback" in event.text
    assert event.session_id


def test_write_path_never_reads_the_transcript():
    """`last_assistant_message` is present, so no JSONL parsing is needed."""
    payload = load("claude_code_stop.json")
    assert payload["last_assistant_message"]
    payload.pop("transcript_path")
    assert hooks.normalize(payload).text == (
        "A failed health check triggers an automatic rollback to the "
        "previous green environment, and the deploy is marked failed in "
        "the pipeline."
    )


def test_subagent_stop_is_a_write_event():
    assert hooks.normalize({"hook_event_name": "SubagentStop"}).kind == "write"


def test_unhandled_events_are_ignored():
    for event in ("PreToolUse", "PostToolUse", "PreCompact", "SessionEnd", "Notify"):
        assert hooks.normalize({"hook_event_name": event}).kind == "ignore"


def test_unknown_extra_fields_are_tolerated():
    payload = load("claude_code_user_prompt_submit.json")
    payload["some_future_field"] = {"nested": True}
    assert hooks.normalize(payload).kind == "read"


# --- response shapes -----------------------------------------------------------


def test_claude_code_and_codex_share_one_response_shape():
    for name in (
        "claude_code_user_prompt_submit.json",
        "codex_user_prompt_submit.json",
    ):
        event = hooks.normalize(load(name))
        rendered = hooks.render_context(event, "remembered thing")
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "remembered thing",
            }
        }


def test_hermes_response_shape():
    event = hooks.normalize(load("hermes_pre_llm_call.json"))
    assert hooks.render_context(event, "x") == {"context": "x"}


def test_openclaw_response_shape():
    event = hooks.normalize(load("openclaw_before_prompt_build.json"))
    assert hooks.render_context(event, "x") == {"appendContext": "x"}


def test_no_response_shape_touches_the_system_prompt():
    """Injection lands in the user turn, which is what preserves caching."""
    for name in READ_FIXTURES:
        event = hooks.normalize(load(name))
        rendered = json.dumps(hooks.render_context(event, "x"))
        assert "system" not in rendered.lower()


# --- end to end through the service ---------------------------------------------


def seed(service, *contents):
    for content in contents:
        service.model(agent_id=AGENT, content=content, importance=0.8).save()


@pytest.mark.parametrize("name", READ_FIXTURES)
def test_read_round_trip_injects_a_relevant_memory(tmp_path, name):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green and roll back on failed health checks")
    output = hooks.handle_payload(load(name), service=service)
    assert output is not None
    decoded = json.loads(output)
    body = json.dumps(decoded)
    assert "blue-green" in body


@pytest.mark.parametrize("name", WRITE_FIXTURES)
def test_write_round_trip_captures_the_turn(tmp_path, name):
    service = make_service(tmp_path)
    assert hooks.handle_payload(load(name), service=service) is None
    stored = DefaultMemory.query.filter(agent_id=AGENT)
    assert len(stored) == 1
    assert "automatic rollback" in stored[0].content


def test_a_full_turn_pair_recalls_then_captures(tmp_path):
    """The subconscious loop, with no tool election anywhere in it."""
    service = make_service(tmp_path)
    hooks.handle_payload(load("claude_code_stop.json"), service=service)
    output = hooks.handle_payload(
        load("claude_code_user_prompt_submit.json"), service=service
    )
    assert output is not None
    assert "automatic rollback" in output


def test_read_with_no_memories_emits_nothing(tmp_path):
    service = make_service(tmp_path)
    assert (
        hooks.handle_payload(
            load("claude_code_user_prompt_submit.json"), service=service
        )
        is None
    )


def test_empty_prompt_assembles_nothing(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green")
    payload = load("claude_code_user_prompt_submit.json")
    payload["prompt"] = "   "
    assert hooks.handle_payload(payload, service=service) is None


def test_empty_assistant_message_writes_nothing(tmp_path):
    service = make_service(tmp_path)
    payload = load("claude_code_stop.json")
    payload["last_assistant_message"] = "  "
    hooks.handle_payload(payload, service=service)
    assert len(DefaultMemory.query.filter(agent_id=AGENT)) == 0


def test_missing_session_id_still_injects(tmp_path):
    """Outcome reporting degrades to a no-op; recall must not."""
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green and roll back on failed health checks")
    payload = load("claude_code_user_prompt_submit.json")
    payload.pop("session_id")
    output = hooks.handle_payload(payload, service=service)
    assert output is not None
    assert "blue-green" in output


def test_kill_switch_silences_both_paths(tmp_path):
    service = make_service(tmp_path, enabled=False)
    seed(service, "Deploys are blue-green and roll back on failed health checks")
    assert (
        hooks.handle_payload(
            load("claude_code_user_prompt_submit.json"), service=service
        )
        is None
    )
    hooks.handle_payload(load("claude_code_stop.json"), service=service)
    assert len(DefaultMemory.query.filter(agent_id=AGENT)) == 1


def test_outcome_is_reported_on_the_following_stop(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green and roll back on failed health checks")
    hooks.handle_payload(load("claude_code_user_prompt_submit.json"), service=service)
    session = load("claude_code_stop.json")["session_id"]
    assert POPOTO_REDIS_DB.llen(service._pending_key(session)) == 1
    hooks.handle_payload(load("claude_code_stop.json"), service=service)
    assert POPOTO_REDIS_DB.llen(service._pending_key(session)) == 0


def test_write_path_reports_used_not_acted():
    # A hook fires every turn and cannot know whether a surfaced memory
    # influenced the response, so it must never claim "acted" -- that would
    # strengthen ConfidenceField/decay clocks on every turn and defeat decay
    # entirely (see fields/observation.py). Pin the outcome the write path
    # passes to service.feedback so this can't silently regress.
    class RecordingService:
        def __init__(self):
            self.config = SimpleNamespace(enabled=True)
            self.feedback_calls = []

        def capture(self, text, session_id=None):
            pass

        def feedback(self, session_id, outcome="acted"):
            self.feedback_calls.append((session_id, outcome))

    service = RecordingService()
    payload = load("claude_code_stop.json")
    hooks.handle_payload(payload, service=service)
    assert service.feedback_calls == [(payload["session_id"], "used")]


# --- malformed input --------------------------------------------------------------


def test_malformed_stdin_returns_nothing():
    assert hooks.run("this is not json") is None
    assert hooks.run("") is None
    assert hooks.run("   ") is None
    assert hooks.run("[1, 2, 3]") is None
    assert hooks.run('{"unterminated": ') is None


def test_output_is_a_single_complete_json_document(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Deploys are blue-green and roll back on failed health checks")
    output = hooks.run(
        json.dumps(load("claude_code_user_prompt_submit.json")), service=service
    )
    assert output.startswith("{") and output.endswith("}")
    json.loads(output)


# --- subprocess behavior, the guarantees that actually matter ------------------------


SUBPROCESS_DB = os.environ.get("POPOTO_TEST_DB") or "15"
"""Database the subprocess tests use. The hook process resolves its own
connection from ``POPOTO_MEMORY_URL``, so it does not inherit the pytest
plugin's isolation and would otherwise land on database 0.

``or "15"`` rather than a ``get`` default: an explicitly-empty
``POPOTO_TEST_DB=`` satisfies ``get`` and yields ``""``, which builds
``redis://localhost:6379/`` -- a URL whose database ``parse_url`` drops
entirely, resolving to database 0. That is the developer's real data."""


def _run_cli(payload, env_extra=None):
    env = dict(os.environ)
    env.pop("POPOTO_TEST_DB", None)
    env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    env["POPOTO_MEMORY_URL"] = f"redis://localhost:6379/{SUBPROCESS_DB}"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "popoto.integrations.cli", "hook"],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_read_hook_with_redis_down(tmp_path):
    """The real server-down case: exit 0, no stdout, one log line."""
    log = tmp_path / "down.log"
    result = _run_cli(
        load("claude_code_user_prompt_submit.json"),
        {
            "POPOTO_MEMORY_URL": _down_redis_url(),
            "POPOTO_MEMORY_LOG": str(log),
        },
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert log.exists()
    lines = log.read_text().splitlines()
    # One failure line, then the service stops trying: a hung server must
    # not cost the prompt one timeout per operation.
    assert len(lines) == 1, lines
    assert "ConnectionError" in lines[0]


def test_write_hook_with_redis_down(tmp_path):
    log = tmp_path / "down.log"
    result = _run_cli(
        load("claude_code_stop.json"),
        {
            "POPOTO_MEMORY_URL": _down_redis_url(),
            "POPOTO_MEMORY_LOG": str(log),
        },
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert log.exists()


def test_malformed_stdin_in_a_subprocess(tmp_path):
    log = tmp_path / "malformed.log"
    result = _run_cli("{ not json at all", {"POPOTO_MEMORY_LOG": str(log)})
    assert result.returncode == 0
    assert result.stdout == ""
    assert "hook_decode" in log.read_text()


def test_ignored_event_in_a_subprocess():
    result = _run_cli({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert result.returncode == 0
    assert result.stdout == ""


def test_doctor_reports_the_failure_counters(tmp_path):
    """Every degraded state above must be visible without reading the log."""
    log = tmp_path / "down.log"
    env = {
        "POPOTO_MEMORY_URL": _down_redis_url(),
        "POPOTO_MEMORY_LOG": str(log),
    }
    _run_cli(load("claude_code_user_prompt_submit.json"), env)

    full_env = dict(os.environ)
    full_env.pop("POPOTO_TEST_DB", None)
    full_env["POPOTO_MEMORY_AGENT_ID"] = AGENT
    full_env["POPOTO_MEMORY_URL"] = f"redis://localhost:6379/{SUBPROCESS_DB}"
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "popoto.integrations.cli", "doctor"],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=120,
    )
    assert result.returncode == 1
    assert "UNREACHABLE" in result.stdout
