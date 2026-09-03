"""Stdio MCP server exposing the discretionary half of harness memory.

Four tools, one naming convention, frozen: ``memory_search``,
``memory_save``, ``memory_feedback``, ``memory_status``. They end up in
users' harness configs, in blog posts, and in other projects' docs, so
renaming one later breaks installs silently. A test asserts these four
names literally.

**These tools are not how recall and capture happen.** MCP tools are
agent-elected: the model calls them when it decides to. Subconscious memory
means memory runs on every turn whether or not the model asks, which is
what the hook path in :mod:`popoto.integrations.hooks` does. This server is
for the discretionary half -- searching mid-task, saving something
explicitly, and correcting a memory that turned out wrong -- plus clients
that have no hook surface at all.

The tool logic lives in :func:`tool_definitions` and :func:`dispatch`,
which are plain Python and import nothing from the MCP SDK. Only
:func:`build_server` and :func:`serve` touch ``mcp``, and they import it
lazily, so ``popoto.integrations`` stays usable on a bare
``pip install popoto``.
"""

import json
from typing import Any, Dict, List, Optional

TOOL_NAMES = (
    "memory_search",
    "memory_save",
    "memory_feedback",
    "memory_status",
)
"""The frozen public tool names. Do not add a second convention: Mem0
publishes ``add_memories``/``search_memory`` in one product and
``add_memory``/``search_memories`` in another, and pays for it in user
confusion."""

SERVER_NAME = "popoto-memory"
"""Server name as it appears in harness MCP configuration."""

SERVER_INSTRUCTIONS = (
    "Persistent memory backed by the user's own Redis or Valkey. Recall and "
    "capture already run automatically on every turn through hooks; use "
    "these tools only for deliberate acts: searching for something specific "
    "mid-task, saving a fact worth keeping, correcting a memory that proved "
    "wrong, or checking that memory is healthy."
)


def tool_definitions() -> List[Dict[str, Any]]:
    """Describe the four tools as plain JSON-schema dicts.

    Returns:
        One dict per tool with ``name``, ``description``, and
        ``inputSchema``, in the frozen :data:`TOOL_NAMES` order.
    """
    return [
        {
            "name": "memory_search",
            "description": (
                "Search stored memories for this project and return the "
                "most relevant, ranked. Use when you need something "
                "specific that was not already injected into the turn."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum records to return. Defaults to the "
                            "configured injection budget."
                        ),
                        "minimum": 1,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_save",
            "description": (
                "Save a fact to memory verbatim. Turns are already captured "
                "automatically, so use this only for something deliberate "
                "that the turn text would not preserve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text to remember, stored as written.",
                    },
                    "importance": {
                        "type": "number",
                        "description": (
                            "Base importance from 0 to 1, feeding time decay. "
                            "Defaults to 0.5."
                        ),
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "memory_feedback",
            "description": (
                "Report how a memory turned out. Marking a memory "
                "'contradicted' lowers its confidence so it ranks lower in "
                "future turns; 'acted' raises it. The memory is not deleted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": ("The record key returned by memory_search."),
                    },
                    "outcome": {
                        "type": "string",
                        "description": "How the memory was used.",
                        "enum": [
                            "acted",
                            "used",
                            "dismissed",
                            "deferred",
                            "contradicted",
                        ],
                    },
                },
                "required": ["key"],
            },
        },
        {
            "name": "memory_status",
            "description": (
                "Report memory health: connection, scope, retrieval mode, "
                "record count, and any recorded failures."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def dispatch(
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
    service: Any = None,
) -> Dict[str, Any]:
    """Execute one tool call and return an MCP-shaped result dict.

    Errors become readable messages with ``is_error`` set, never a
    traceback rendered as tool output: the model reads this text and a
    traceback teaches it nothing actionable.

    Args:
        name: One of :data:`TOOL_NAMES`.
        arguments: The tool's arguments.
        service: Optional :class:`~popoto.integrations.service.MemoryService`
            override; one is built from the environment when omitted.

    Returns:
        ``{"text": str, "is_error": bool, "structured": dict | None}``.
    """
    arguments = arguments or {}

    if name not in TOOL_NAMES:
        return _error(f"Unknown tool {name!r}. Available: {', '.join(TOOL_NAMES)}.")

    if service is None:
        from .service import MemoryService

        try:
            service = MemoryService()
        except Exception as exc:
            # Construction binds the connection, so it is a real failure
            # surface, not plumbing: a DB 0 refusal, a URL with no database
            # number, an unreachable server. Outside this handler it left
            # dispatch as an uncaught traceback rather than a tool error,
            # which is the one thing an MCP tool must never do. The message
            # carries the remediation, so pass it through whole.
            return _error(f"{name} failed: {type(exc).__name__}: {exc}")

    try:
        if name == "memory_search":
            return _search(service, arguments)
        if name == "memory_save":
            return _save(service, arguments)
        if name == "memory_feedback":
            return _feedback(service, arguments)
        return _status(service)
    except Exception as exc:
        return _error(f"{name} failed: {type(exc).__name__}: {exc}")


def _ok(text: str, structured: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"text": text, "is_error": False, "structured": structured}


def _error(text: str) -> Dict[str, Any]:
    return {"text": text, "is_error": True, "structured": None}


def _search(service: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error("memory_search needs a non-empty 'query'.")
    limit = arguments.get("limit")
    if limit is not None:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            return _error("memory_search 'limit' must be a positive integer.")
    results = service.search(query, limit=limit)
    if not results:
        return _ok(
            f"No memories matched {query!r} for agent {service.config.agent_id!r}.",
            {"results": []},
        )
    lines = [f"{len(results)} memory result(s):"]
    for item in results:
        lines.append(f"- {item['content']}  [key: {item['key']}]")
    return _ok("\n".join(lines), {"results": results})


def _save(service: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return _error("memory_save needs a non-empty 'content'.")
    importance = arguments.get("importance", 0.5)
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        return _error("memory_save 'importance' must be a number between 0 and 1.")
    if not 0.0 <= importance <= 1.0:
        return _error("memory_save 'importance' must be between 0 and 1.")
    keys = service.capture(content, session_id=None, importance=importance)
    if not keys:
        return _error(
            "Nothing was saved. Run `popoto-memory doctor` to see why "
            "(most often: Redis unreachable, or POPOTO_MEMORY_ENABLED=0)."
        )
    return _ok(f"Saved {len(keys)} memory record(s).", {"keys": keys})


def _feedback(service: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    key = arguments.get("key")
    if not isinstance(key, str) or not key.strip():
        return _error("memory_feedback needs a 'key' from memory_search.")
    outcome = arguments.get("outcome") or "contradicted"
    valid = {"acted", "used", "dismissed", "deferred", "contradicted"}
    if outcome not in valid:
        return _error(f"memory_feedback 'outcome' must be one of {sorted(valid)}.")
    if service.correct(key.strip(), outcome=outcome):
        return _ok(f"Recorded outcome {outcome!r} for {key}.", {"key": key})
    return _error(f"No memory found for key {key!r}.")


def _status(service: Any) -> Dict[str, Any]:
    info = service.status()
    if not info.get("redis_reachable"):
        return _error(
            f"Memory is unavailable: cannot reach {info['redis_url']}. "
            "Start Redis or Valkey, or set POPOTO_MEMORY_URL."
        )
    lines = [
        f"agent: {info['agent_id']}",
        f"server: {info['server']}",
        f"retrieval: {info['retrieval_mode']}"
        + (" (QUERY-BLIND)" if info.get("query_blind") else ""),
        f"records: {info['record_count']}",
        f"ingest: {info['ingest']}",
    ]
    failures = {
        k: v for k, v in (info.get("counters") or {}).items() if not k.endswith("_ok")
    }
    lines.append("failures: " + (json.dumps(failures) if failures else "none"))
    return _ok("\n".join(lines), info)


def build_server() -> Any:
    """Construct the MCP :class:`~mcp.server.Server` for this integration.

    Imports the MCP SDK, so it raises :class:`ImportError` without
    ``pip install popoto[mcp]``. The SDK has changed its registration API
    across major versions, so both the callback constructor (2.x) and the
    decorator style (1.x) are supported here; the tool behavior itself is
    shared and version-independent.

    Returns:
        A configured MCP ``Server``.
    """
    import mcp.types as types
    from mcp.server import Server

    def _tools() -> Any:
        return [types.Tool(**definition) for definition in tool_definitions()]

    def _content(result: Dict[str, Any]) -> Any:
        return [types.TextContent(type="text", text=result["text"])]

    async def on_list_tools(_context: Any, _params: Any = None) -> Any:
        return types.ListToolsResult(tools=_tools())

    async def on_call_tool(_context: Any, params: Any) -> Any:
        result = dispatch(params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=_content(result), is_error=result["is_error"]
        )

    try:
        # MCP SDK 2.x: handlers are constructor callbacks.
        return Server(
            SERVER_NAME,
            instructions=SERVER_INSTRUCTIONS,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
    except TypeError:
        pass

    # MCP SDK 1.x: handlers are registered by decorator. Reached through
    # getattr because those attributes do not exist on the 2.x Server, and a
    # direct reference would not type-check against the installed SDK.
    # 1.x's Server.__init__ also accepts instructions=, so pass it here too --
    # otherwise SERVER_INSTRUCTIONS (which tells the model recall/capture
    # already run automatically) never reaches a 1.x client.
    server = Server(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    async def _list_tools() -> Any:
        return _tools()

    async def _call_tool(name: str, arguments: Any) -> Any:
        result = dispatch(name, dict(arguments or {}))
        if result["is_error"]:
            raise ValueError(result["text"])
        return _content(result)

    getattr(server, "list_tools")()(_list_tools)
    getattr(server, "call_tool")()(_call_tool)
    return server


def serve() -> None:
    """Run the stdio MCP server until the client disconnects.

    Blocks. This is what ``popoto-memory mcp`` calls.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server()

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    anyio.run(_run)
