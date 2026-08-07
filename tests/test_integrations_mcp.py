"""The stdio MCP server: tool names, contracts, and error rendering.

Two layers are tested. The tool logic (:func:`tool_definitions`,
:func:`dispatch`) is plain Python with no SDK dependency, so it is tested
directly and runs on any install. The wiring to the actual MCP ``Server`` is
tested through the SDK when ``popoto[mcp]`` is installed, and skipped when it
is not -- which also proves the hook path does not need ``mcp``.
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.integrations import mcp_server  # noqa: E402
from popoto.integrations.config import MemoryConfig  # noqa: E402
from popoto.integrations.service import MemoryService  # noqa: E402
from popoto.recipes import DefaultMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

AGENT = "test-integrations-mcp"

mcp_installed = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("mcp") is None,
    reason="requires the MCP Python SDK: pip install 'popoto[mcp]'",
)


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
    for key in POPOTO_REDIS_DB.scan_iter(
        match=f"$popoto_memory:*:{AGENT}:*", count=200
    ):
        POPOTO_REDIS_DB.delete(key)


def seed(service, *contents):
    for content in contents:
        service.model(agent_id=AGENT, content=content, importance=0.8).save()


# --- the name freeze -----------------------------------------------------------


def test_tool_names_are_frozen():
    """These four strings live in users' configs. A rename breaks installs.

    If this test fails because a tool was renamed, the rename is the bug.
    """
    assert mcp_server.TOOL_NAMES == (
        "memory_search",
        "memory_save",
        "memory_feedback",
        "memory_status",
    )


def test_name_freeze_covers_the_published_definitions():
    assert [d["name"] for d in mcp_server.tool_definitions()] == list(
        mcp_server.TOOL_NAMES
    )


def test_one_naming_convention_only():
    """A single `memory_` prefix, never a second convention alongside it."""
    for name in mcp_server.TOOL_NAMES:
        assert name.startswith("memory_")
        assert name.islower()


# --- tool declarations -----------------------------------------------------------


def test_every_tool_declares_a_description_and_a_schema():
    for definition in mcp_server.tool_definitions():
        assert definition["description"].strip()
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_required_arguments_are_declared():
    by_name = {d["name"]: d for d in mcp_server.tool_definitions()}
    assert by_name["memory_search"]["inputSchema"]["required"] == ["query"]
    assert by_name["memory_save"]["inputSchema"]["required"] == ["content"]
    assert by_name["memory_feedback"]["inputSchema"]["required"] == ["key"]
    assert "required" not in by_name["memory_status"]["inputSchema"]


def test_descriptions_say_that_recall_is_already_automatic():
    """The model must not be taught that it has to call a tool to remember."""
    by_name = {d["name"]: d for d in mcp_server.tool_definitions()}
    assert "automatic" in by_name["memory_save"]["description"].lower()
    assert "automatic" in mcp_server.SERVER_INSTRUCTIONS.lower()


# --- dispatch: memory_search -------------------------------------------------------


def test_search_returns_matching_content(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Rate limits are enforced in the gateway, not per service")
    result = mcp_server.dispatch("memory_search", {"query": "rate limits"}, service)
    assert result["is_error"] is False
    assert "gateway" in result["text"]
    assert result["structured"]["results"][0]["key"].startswith("DefaultMemory:")


def test_search_with_no_matches_is_not_an_error(tmp_path):
    result = mcp_server.dispatch(
        "memory_search", {"query": "nothing here"}, make_service(tmp_path)
    )
    assert result["is_error"] is False
    assert "No memories matched" in result["text"]


def test_search_without_a_query_is_a_readable_error(tmp_path):
    result = mcp_server.dispatch("memory_search", {}, make_service(tmp_path))
    assert result["is_error"] is True
    assert "query" in result["text"]


def test_search_honours_limit(tmp_path):
    service = make_service(tmp_path)
    seed(service, *[f"deploy note number {i}" for i in range(6)])
    result = mcp_server.dispatch(
        "memory_search", {"query": "deploy note", "limit": 2}, service
    )
    assert len(result["structured"]["results"]) <= 2


def test_search_with_a_bad_limit_is_a_readable_error(tmp_path):
    result = mcp_server.dispatch(
        "memory_search", {"query": "x", "limit": "many"}, make_service(tmp_path)
    )
    assert result["is_error"] is True
    assert "limit" in result["text"]


# --- dispatch: memory_save ----------------------------------------------------------


def test_save_writes_one_verbatim_record(tmp_path):
    service = make_service(tmp_path)
    text = "The staging database resets nightly. It runs at 02:00 UTC."
    result = mcp_server.dispatch("memory_save", {"content": text}, service)
    assert result["is_error"] is False
    stored = DefaultMemory.query.filter(agent_id=AGENT)
    assert len(stored) == 1
    assert stored[0].content == text


def test_save_without_content_is_a_readable_error(tmp_path):
    result = mcp_server.dispatch(
        "memory_save", {"content": "  "}, make_service(tmp_path)
    )
    assert result["is_error"] is True
    assert "content" in result["text"]


def test_save_rejects_an_out_of_range_importance(tmp_path):
    result = mcp_server.dispatch(
        "memory_save", {"content": "x", "importance": 4}, make_service(tmp_path)
    )
    assert result["is_error"] is True
    assert "importance" in result["text"]


# --- dispatch: memory_feedback -------------------------------------------------------


def test_feedback_marks_a_record(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Rate limits are enforced in the gateway")
    key = mcp_server.dispatch("memory_search", {"query": "rate limits"}, service)[
        "structured"
    ]["results"][0]["key"]
    result = mcp_server.dispatch(
        "memory_feedback", {"key": key, "outcome": "contradicted"}, service
    )
    assert result["is_error"] is False


def test_feedback_on_a_missing_key_is_a_readable_error(tmp_path):
    result = mcp_server.dispatch(
        "memory_feedback", {"key": "DefaultMemory:nope:nope"}, make_service(tmp_path)
    )
    assert result["is_error"] is True
    assert "No memory found" in result["text"]


def test_feedback_rejects_an_invented_outcome(tmp_path):
    result = mcp_server.dispatch(
        "memory_feedback", {"key": "k", "outcome": "vibes"}, make_service(tmp_path)
    )
    assert result["is_error"] is True
    assert "outcome" in result["text"]


# --- dispatch: memory_status ----------------------------------------------------------


def test_status_reports_the_live_configuration(tmp_path):
    service = make_service(tmp_path)
    seed(service, "Rate limits are enforced in the gateway")
    result = mcp_server.dispatch("memory_status", {}, service)
    assert result["is_error"] is False
    assert f"agent: {AGENT}" in result["text"]
    assert "retrieval: lexical" in result["text"]
    assert "records: 1" in result["text"]


# --- error rendering --------------------------------------------------------------------


def test_unknown_tool_names_the_available_ones(tmp_path):
    result = mcp_server.dispatch("memory_forget", {}, make_service(tmp_path))
    assert result["is_error"] is True
    for name in mcp_server.TOOL_NAMES:
        assert name in result["text"]


def test_an_unexpected_exception_is_not_returned_as_a_traceback(tmp_path):
    class Exploding(MemoryService):
        def search(self, *args, **kwargs):
            raise RuntimeError("boom")

    service = Exploding(MemoryConfig(agent_id=AGENT, log_path=tmp_path / "m.log"))
    result = mcp_server.dispatch("memory_search", {"query": "x"}, service)
    assert result["is_error"] is True
    assert "Traceback" not in result["text"]
    assert "boom" in result["text"]


# --- SDK wiring ----------------------------------------------------------------------------


def test_the_module_imports_without_the_mcp_sdk():
    """Tool logic must not import `mcp`, so hooks work on a bare install."""
    source = os.path.join(
        os.path.dirname(SCRIPT_DIR), "src", "popoto", "integrations", "mcp_server.py"
    )
    with open(source) as handle:
        lines = handle.read().splitlines()
    top_level_imports = [
        line
        for line in lines
        if line.startswith(("import ", "from ")) and "mcp" in line
    ]
    assert top_level_imports == []


@mcp_installed
def test_build_server_produces_a_real_mcp_server():
    server = mcp_server.build_server()
    assert server.name == mcp_server.SERVER_NAME


@mcp_installed
def test_the_sdk_accepts_every_tool_definition():
    import mcp.types as types

    tools = [types.Tool(**definition) for definition in mcp_server.tool_definitions()]
    assert [tool.name for tool in tools] == list(mcp_server.TOOL_NAMES)


@mcp_installed
def test_an_in_process_client_lists_and_calls_the_tools(tmp_path, monkeypatch):
    """The agent-invocation check: a real MCP client over a real session.

    Uses the SDK's in-memory stream pair rather than a subprocess, so the
    assertion is about protocol wiring rather than process plumbing that
    ``popoto-memory mcp`` shares with every other stdio server. The server
    builds its own service from the environment, which is why the agent id
    and log path are set here; the URL is deliberately left unset so
    ``bind_connection`` stays inert and the suite keeps the pytest plugin's
    isolated database.
    """
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    monkeypatch.setenv("POPOTO_MEMORY_AGENT_ID", AGENT)
    monkeypatch.setenv("POPOTO_MEMORY_LOG", str(tmp_path / "memory.log"))
    monkeypatch.delenv("POPOTO_MEMORY_URL", raising=False)

    seed(make_service(tmp_path), "Rate limits are enforced in the gateway")

    server = mcp_server.build_server()
    seen = {}

    async def scenario():
        async with create_client_server_memory_streams() as (
            (client_read, client_write),
            (server_read, server_write),
        ):
            async with anyio.create_task_group() as task_group:

                async def run_server():
                    await server.run(
                        server_read,
                        server_write,
                        server.create_initialization_options(),
                        raise_exceptions=True,
                    )

                task_group.start_soon(run_server)
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    seen["names"] = [tool.name for tool in listed.tools]
                    search = await session.call_tool(
                        "memory_search", {"query": "rate limits"}
                    )
                    seen["search"] = search.content[0].text
                    status = await session.call_tool("memory_status", {})
                    seen["status"] = status.content[0].text
                task_group.cancel_scope.cancel()

    anyio.run(scenario)

    assert seen["names"] == list(mcp_server.TOOL_NAMES)
    assert "gateway" in seen["search"]
    assert "retrieval:" in seen["status"]
