"""MemoryService's contract on every storage backend (#814).

The harness integration (hook CLI, MCP tools, ``doctor``) reaches storage only
through :class:`~popoto.integrations.service.MemoryService`. On Redis its
session state is raw keys; on Postgres it is the backend's ``_harness`` and
``_counter`` adapters. These tests go through the service's public surface
(plus the two handoff methods the hooks call), so the same assertions hold on
both legs: the conformance ``backend`` fixture runs each test once per
configured backend (``POPOTO_CONFORMANCE_BACKENDS=redis,postgres``).

The Redis-only suites (``test_integrations_service.py`` and friends) keep
pinning the raw key layout; nothing here reads a key or a table directly.
"""

import json

import pytest

from popoto.integrations import hooks
from popoto.integrations.config import MemoryConfig
from popoto.integrations.service import MAX_PENDING_TURNS, MemoryService

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

AGENT = "test-integrations-backends"


def make_service(tmp_path, **overrides):
    options = dict(
        agent_id=AGENT,
        log_path=tmp_path / "memory.log",
        max_items=5,
        max_tokens=800,
    )
    options.update(overrides)
    return MemoryService(MemoryConfig(**options))


class _Rec:
    """A stand-in record: the handoff only reads ``db_key.redis_key``."""

    def __init__(self, key):
        self.db_key = type("K", (), {"redis_key": key})()


def _recs(*keys):
    return [_Rec(k) for k in keys]


def test_the_service_reports_the_leg_it_runs_on(tmp_path, backend):
    service = make_service(tmp_path)
    assert service.backend_name == backend.name
    info = service.status()
    assert info["backend"] == backend.name
    assert info["reachable"] is True
    assert info["errors"] == []
    # One key set on both legs: a ``doctor --json`` consumer written against
    # either shape never meets a KeyError on the other.
    for key in ("backend", "reachable", "server", "postgres"):
        assert key in info, key
    for key in ("redis_url", "redis_reachable"):
        assert key in info, key
    if backend.name == "redis":
        assert info["redis_reachable"] is True
        assert info["redis_url"]
        assert info["postgres"] is None
    else:
        assert info["redis_url"] is None and info["redis_reachable"] is None
        assert info["postgres"]["supported"] is True
        assert info["server"].startswith("postgresql ")


def test_capture_then_assemble_round_trips(tmp_path):
    service = make_service(tmp_path)
    keys = service.capture("Deploys are blue-green with automatic rollback", "s1")
    assert len(keys) == 1
    service.capture("The staging database resets nightly at 02:00 UTC", "s1")
    context = service.assemble("how do deploys roll back?", session_id="s1")
    assert "blue-green" in context
    info = service.status()
    assert info["record_count"] == 2
    assert info["counters"].get("capture_ok") == 2
    assert info["counters"].get("assemble_ok") == 1
    assert set(info["last_success"]) == {"capture", "assemble"}


def test_a_memory_injected_once_is_suppressed_for_the_session(tmp_path):
    service = make_service(tmp_path)
    service.capture("Deploys are blue-green with automatic rollback", "s1")
    first = service.assemble("how do deploys roll back?", session_id="s1")
    assert "blue-green" in first
    assert service.assemble("how do deploys roll back?", session_id="s1") == ""
    # Another session has its own suppression set.
    assert "blue-green" in service.assemble("deploys roll back", session_id="s2")


def test_feedback_claims_the_turn_that_staged_it(tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", _recs("k:a"), turn_id="t1")
    service._push_pending("s1", _recs("k:b"), turn_id="t2")
    assert service._pop_pending("s1", turn_id="t2") == ["k:b"]
    assert service._pop_pending("s1", turn_id="t1") == ["k:a"]
    assert service._pop_pending("s1", turn_id="t1") == []


def test_a_redelivered_turn_stages_once(tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", _recs("k:a"), turn_id="t1")
    service._push_pending("s1", _recs("k:z"), turn_id="t1")
    assert service._pop_pending("s1", turn_id="t1") == ["k:a"]
    assert service._pop_pending("s1") == []


def test_an_unknown_turn_is_a_counted_miss_not_a_positional_pop(tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", _recs("k:a"), turn_id="t1")
    assert service._pop_pending("s1", turn_id="t-other") == []
    assert service.status()["counters"].get("pending_miss") == 1
    # The staged entry is still there for its own turn.
    assert service._pop_pending("s1", turn_id="t1") == ["k:a"]


def test_untagged_entries_pair_positionally(tmp_path):
    service = make_service(tmp_path)
    service._push_pending("s1", _recs("k:a"))
    service._push_pending("s1", _recs("k:b"))
    # A turn id against a queue with no tagged entry falls back to the head.
    assert service._pop_pending("s1", turn_id="t9") == ["k:a"]
    assert service._pop_pending("s1") == ["k:b"]
    assert service._pop_pending("s1") == []


def test_turn_keying_off_writes_and_pops_positionally(tmp_path):
    service = make_service(tmp_path, turn_keyed=False)
    service._push_pending("s1", _recs("k:a"), turn_id="t1")
    service._push_pending("s1", _recs("k:b"), turn_id="t2")
    assert service._pop_pending("s1", turn_id="t2") == ["k:a"]


def test_the_pending_handoff_is_capped(tmp_path):
    service = make_service(tmp_path)
    for i in range(MAX_PENDING_TURNS + 8):
        service._push_pending("s1", _recs(f"k:{i}"))
    popped = []
    while True:
        keys = service._pop_pending("s1")
        if not keys:
            break
        popped.extend(keys)
    assert popped == [f"k:{i}" for i in range(8, MAX_PENDING_TURNS + 8)]


def test_the_feedback_loop_reports_against_real_records(tmp_path):
    service = make_service(tmp_path)
    service.capture("Deploys are blue-green with automatic rollback", "s1")
    assert service.assemble("deploys roll back", session_id="s1", turn_id="t1")
    assert service.feedback("s1", outcome="used", turn_id="t1") == 1
    assert service.feedback("s1", outcome="used", turn_id="t1") == 0


def test_search_and_correct(tmp_path):
    service = make_service(tmp_path)
    service.capture("Rate limits are enforced in the gateway", None)
    found = service.search("gateway rate limits")
    assert [r["content"] for r in found] == ["Rate limits are enforced in the gateway"]
    assert service.correct(found[0]["key"], outcome="contradicted") is True
    assert service.correct(found[0]["key"] + "-missing") is False


def test_the_heuristic_notice_is_recorded_once(tmp_path):
    service = make_service(tmp_path, ingest="heuristic")
    service.extractor
    service.extractor
    assert service.status()["counters"].get("heuristic_notice") == 1


def test_the_hook_adapter_injects_and_reports(tmp_path):
    service = make_service(tmp_path)
    service.capture("The staging database resets nightly at 02:00 UTC", None)
    read = hooks.run(
        json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "when does the staging database reset?",
                "session_id": "s1",
                "prompt_id": "p1",
            }
        ),
        service=service,
    )
    context = json.loads(read)["hookSpecificOutput"]["additionalContext"]
    assert "02:00 UTC" in context
    write = hooks.run(
        json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "s1",
                "prompt_id": "p1",
                "last_assistant_message": "It resets at 02:00 UTC every night.",
            }
        ),
        service=service,
    )
    assert write is None
    counters = service.status()["counters"]
    assert counters.get("capture_ok") == 2
    assert not {k for k in counters if not k.endswith("_ok")}


def test_every_mcp_tool_runs(tmp_path, backend):
    from popoto.integrations.mcp_server import dispatch

    service = make_service(tmp_path)
    saved = dispatch(
        "memory_save", {"content": "Bundles are built with esbuild"}, service
    )
    assert saved["is_error"] is False, saved["text"]
    found = dispatch("memory_search", {"query": "esbuild bundles"}, service)
    assert found["is_error"] is False and "esbuild" in found["text"]
    key = saved["structured"]["keys"][0]
    fed = dispatch("memory_feedback", {"key": key, "outcome": "acted"}, service)
    assert fed["is_error"] is False, fed["text"]
    status = dispatch("memory_status", {}, service)
    assert status["is_error"] is False, status["text"]
    assert f"backend: {backend.name}" in status["text"]
