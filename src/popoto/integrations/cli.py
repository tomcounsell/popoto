"""``popoto-memory`` -- the single console entry point for harness memory.

Four subcommands:

``hook``
    Read a harness hook payload on stdin, write the harness's response on
    stdout. One command string serves Claude Code and Codex, because both
    send ``hook_event_name`` and this command dispatches on it.
``mcp``
    Serve the discretionary memory tools over stdio MCP. Requires
    ``pip install popoto[mcp]``.
``doctor``
    Print resolved configuration, backend reachability (Redis, or on
    ``POPOTO_BACKEND=postgres`` the Postgres server version, schema, pgvector
    and health), effective retrieval mode, record count, failure counters,
    and a measured hook round trip. This is the user-visible error surface; a
    hook has no console.
``demo``
    Seed a few memories, retrieve them, capture a turn, and report an
    outcome, against the configured backend with no harness and no API keys.

Startup latency is the reason this module imports almost nothing at module
scope. The read hook is synchronous and on the critical path of every turn,
so ``popoto`` itself, ``redis``, and the ``mcp`` SDK are all imported inside
the subcommand that needs them.
"""

import argparse
import os
import sys
from typing import Any, List, Optional

USAGE_EPILOG = """\
examples:
  popoto-memory doctor
  popoto-memory demo
  echo '{"hook_event_name":"UserPromptSubmit","prompt":"how do we deploy?"}' \\
      | popoto-memory hook
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="popoto-memory",
        description=(
            "Subconscious memory for agent harnesses, backed by your own "
            "Redis, Valkey or Postgres. No API keys."
        ),
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    hook = sub.add_parser(
        "hook",
        help="handle one harness hook event on stdin",
        description=(
            "Reads one JSON hook payload on stdin and writes the harness's "
            "response on stdout. Always exits 0."
        ),
    )
    hook.add_argument(
        "--event",
        default=None,
        help=(
            "override hook_event_name, for harnesses that do not include it "
            "in the payload"
        ),
    )

    sub.add_parser(
        "mcp",
        help="serve the memory tools over stdio MCP",
        description="Runs the stdio MCP server. Requires popoto[mcp].",
    )

    doctor = sub.add_parser(
        "doctor",
        help="print resolved config and live state",
        description=(
            "Prints resolved configuration, backend reachability, effective "
            "retrieval mode, record count, failure counters, and a measured "
            "hook round trip."
        ),
    )
    doctor.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead"
    )
    doctor.add_argument(
        "--no-latency",
        action="store_true",
        help="skip the hook round-trip measurement",
    )

    demo = sub.add_parser(
        "demo",
        help="exercise the full loop against the configured backend, no harness",
        description=(
            "Seeds memories, assembles context for a query, captures a turn, "
            "and reports an outcome. Zero API keys."
        ),
    )
    demo.add_argument(
        "--agent-id",
        default="popoto-memory-demo",
        help="agent id to seed and query (default: popoto-memory-demo)",
    )
    demo.add_argument(
        "--keep",
        action="store_true",
        help="leave the seeded records in place when finished",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``popoto-memory`` console script.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code. ``hook`` always returns 0, whatever happened,
        because a memory failure must not fail the user's turn.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "hook":
        return _cmd_hook(args)
    if args.command == "mcp":
        return _cmd_mcp()
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "demo":
        return _cmd_demo(args)
    parser.print_help()
    return 0


def _cmd_hook(args: Any) -> int:
    """Handle one hook event. Always returns 0."""
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        return 0

    # No bind_connection here: MemoryService.__init__ owns that, so every
    # entry point resolves POPOTO_MEMORY_URL the same way.
    _bound_postgres_connect_timeout()
    try:
        from . import hooks

        if args.event:
            try:
                import json as _json

                payload = _json.loads(stdin_text)
                if isinstance(payload, dict):
                    payload.setdefault("hook_event_name", args.event)
                    stdin_text = _json.dumps(payload)
            except Exception:
                pass
        output = hooks.run(stdin_text)
    except Exception:
        output = None

    # One write, never a partial one: Codex reads stdout beginning with "{"
    # as JSON and treats a parse failure as a hook failure.
    if output:
        try:
            sys.stdout.write(output)
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _bound_postgres_connect_timeout() -> None:
    """Cap the Postgres connect wait at the hook's own budget (#814).

    The Redis connection the integration binds gets a 1-second connect and
    socket timeout (``HOOK_SOCKET_TIMEOUT_SECONDS``) because the read hook
    sits on the user's prompt path. The Postgres backend's connect timeout,
    ``Defaults.PG_CONNECT_TIMEOUT_SECONDS``, is 5 s, so a Postgres outage
    would stall every prompt five times longer than a Redis one. Lowered
    here and only here: the hook subcommand is a process of its own, so this
    reaches no host application; an in-process caller (the Hermes plugin,
    the MCP server) keeps the library default. Raising it is never done --
    a smaller value set by the operator wins.
    """
    try:
        from ..fields.constants import Defaults
        from .config import HOOK_SOCKET_TIMEOUT_SECONDS

        if Defaults.PG_CONNECT_TIMEOUT_SECONDS > HOOK_SOCKET_TIMEOUT_SECONDS:
            Defaults.PG_CONNECT_TIMEOUT_SECONDS = HOOK_SOCKET_TIMEOUT_SECONDS
    except Exception:
        pass


def _cmd_mcp() -> int:
    """Run the stdio MCP server."""
    try:
        from .mcp_server import serve
    except ImportError as exc:
        sys.stderr.write(
            "popoto-memory mcp needs the MCP Python SDK.\n"
            "  pip install 'popoto[mcp]'\n"
            f"({exc})\n"
        )
        return 1
    try:
        serve()
    except ImportError as exc:
        sys.stderr.write(
            "popoto-memory mcp needs the MCP Python SDK.\n"
            "  pip install 'popoto[mcp]'\n"
            f"({exc})\n"
        )
        return 1
    return 0


def _cmd_doctor(args: Any) -> int:
    """Print the diagnostic report. Returns 1 when the backend is unreachable
    (on Postgres: also when the server is below what popoto supports)."""
    from .config import MemoryConfig
    from .service import MemoryService, NON_FAILURE_COUNTERS

    config = MemoryConfig.from_env()
    try:
        service = MemoryService(config)
    except ValueError as exc:
        # MemoryService.__init__ -> bind_connection raises when
        # POPOTO_MEMORY_URL carries no database number. Doctor's entire job
        # is diagnosing misconfiguration, so it must print the message
        # rather than let it surface as a traceback.
        if args.json:
            import json

            sys.stdout.write(
                json.dumps(
                    {
                        "redis_url": None,
                        "redis_reachable": False,
                        "reachable": False,
                        "backend": os.environ.get("POPOTO_BACKEND", "").strip()
                        or "redis",
                        "server": None,
                        "postgres": None,
                        "error": str(exc),
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stdout.write(f"popoto-memory doctor\n\n{exc}\n")
        return 1
    info = service.status()

    if not args.no_latency and info.get("reachable"):
        info["hook_read_ms"] = _measure_hook_read(service)

    if args.json:
        import json

        sys.stdout.write(json.dumps(info, indent=2, default=str) + "\n")
        return 0 if info.get("reachable") else 1

    lines = ["popoto-memory doctor", ""]
    if info["enabled"]:
        lines.append("  status         enabled")
    else:
        lines.append("  status         DISABLED (POPOTO_MEMORY_ENABLED=0)")
    if info.get("backend", "redis") == "redis":
        if not _doctor_redis_lines(info, lines):
            sys.stdout.write("\n".join(lines) + "\n")
            return 1
    elif not _doctor_postgres_lines(info, lines):
        sys.stdout.write("\n".join(lines) + "\n")
        return 1

    lines.append(f"  agent id       {info['agent_id']}")
    lines.append(f"  model          {info['model']}")
    mode = info.get("retrieval_mode")
    if info.get("query_blind"):
        lines.append(
            f"  retrieval      {mode} -- QUERY-BLIND: prompt text is ignored "
            "when ranking. Expected 'lexical'; the model in use declares no "
            "BM25Field."
        )
    else:
        lines.append(f"  retrieval      {mode} (query-sensitive)")
    lines.append(f"  ingest         {info['ingest']}")
    if info["ingest"] != "raw":
        lines.append(
            "                 issue #489 measured heuristic extraction at "
            "0.2078 judged accuracy against 0.3636 for raw ingestion"
        )
    lines.append(
        f"  budget         {info['max_items']} items / {info['max_tokens']} tokens"
    )
    lines.append(f"  records        {info.get('record_count')}")
    if "hook_read_ms" in info:
        lines.append(f"  hook read      {info['hook_read_ms']} ms (in-process)")

    counters = info.get("counters") or {}
    failures = {
        k: v
        for k, v in counters.items()
        if not k.endswith("_ok") and k not in NON_FAILURE_COUNTERS
    }
    successes = {k: v for k, v in counters.items() if k.endswith("_ok")}
    lines.append(
        "  successes      "
        + (", ".join(f"{k[:-3]}={v}" for k, v in sorted(successes.items())) or "none")
    )
    if failures:
        lines.append(
            "  FAILURES       "
            + ", ".join(f"{k}={v}" for k, v in sorted(failures.items()))
        )
    else:
        lines.append("  failures       none")

    evicted = counters.get("evicted") or 0
    if evicted:
        lines.append(
            f"  DATA LOSS      {evicted} records selected for eviction "
            "past the per-agent cap (permanent, no tombstone)"
        )
        lines.append(
            "                 set POPOTO_DEFAULT_MEMORY_MAX_RECORDS to raise "
            "or lower the cap, or to 0/off to disable eviction"
        )

    last = info.get("last_success") or {}
    if last:
        for name, stamp in sorted(last.items()):
            lines.append(f"  last {name:<9} {stamp}")
    else:
        lines.append("  last activity  never (no turn has run through the hook yet)")

    lines.append(f"  log            {info['log_path']}")
    tail = info.get("log_tail") or []
    for entry in tail:
        lines.append(f"    {entry}")

    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _doctor_redis_lines(info: Any, lines: List[str]) -> bool:
    """The Redis connection block of ``doctor``; ``False`` when unreachable.

    Byte-identical to the report before the Postgres backend existed.
    """
    lines.append(f"  redis url      {info['redis_url']}")
    lines.append(f"  url source     {info.get('url_source', 'default')}")
    if info["redis_reachable"]:
        lines.append(
            f"  redis          reachable, {info['server']}, "
            f"ping {info.get('ping_ms')} ms"
        )
        return True
    lines.append("  redis          UNREACHABLE")
    for err in info["errors"]:
        lines.append(f"                 {err}")
    lines.append("")
    lines.append(
        # Not database 0: MemoryService refuses it, so suggesting it
        # here would trade one failure for another.
        "  Start a server, or point POPOTO_MEMORY_URL at one:\n"
        "    redis-server        (or: valkey-server)\n"
        "    export POPOTO_MEMORY_URL=redis://localhost:6379/1"
    )
    return False


def _doctor_postgres_lines(info: Any, lines: List[str]) -> bool:
    """The Postgres connection block of ``doctor`` (#814); ``False`` when the
    server is unreachable or below what popoto supports.

    Reports the backend, the DSN (host, port, database and user only -- never
    the password), the schema, the server version, pgvector and the
    backend's health record. Nothing about Redis: a Postgres-bound process
    reads no Redis variable and dials no Redis server.
    """
    pg = info.get("postgres") or {}
    lines.append(f"  backend        {info.get('backend')}")
    lines.append(f"  postgres dsn   {pg.get('dsn') or '(not configured)'}")
    lines.append(f"  url source     {info.get('url_source')}")
    if not info.get("reachable"):
        if pg.get("server_version"):
            lines.append(
                f"  postgres       UNSUPPORTED, postgresql {pg['server_version']}"
            )
        else:
            lines.append("  postgres       UNREACHABLE")
        for err in info["errors"]:
            lines.append(f"                 {err}")
        health = pg.get("health")
        if health:
            lines.append(
                f"  health         consecutive_failures="
                f"{health.get('consecutive_failures')}, "
                f"dropped_writes={health.get('dropped_writes')}"
            )
        lines.append("")
        lines.append(
            "  Point the backend at a PostgreSQL 18+ server:\n"
            "    export POPOTO_BACKEND=postgres\n"
            "    export POPOTO_POSTGRES_URL=postgresql://localhost:5432/agents"
        )
        return False
    lines.append(
        f"  postgres       reachable, {info['server']}, ping {info.get('ping_ms')} ms"
    )
    tables = pg.get("schema_tables")
    if pg.get("schema_exists"):
        lines.append(f"  schema         {pg.get('schema')} ({tables} tables)")
    else:
        lines.append(
            f"  schema         {pg.get('schema')} (not created yet; the first "
            "write creates it)"
        )
    vector = pg.get("pgvector")
    if vector:
        where = vector.get("schema")
        if not vector.get("on_search_path"):
            where = f"{where}, NOT on search_path"
        lines.append(f"  pgvector       {vector.get('version')} ({where})")
    else:
        lines.append(
            "  pgvector       not installed (DefaultMemory does not need it; "
            "EmbeddingField models do)"
        )
    health = pg.get("health") or {}
    state = "ok" if health.get("ok", True) else "DEGRADED"
    lines.append(
        f"  health         {state}, dropped_writes={health.get('dropped_writes', 0)}"
    )
    return True


def _measure_hook_read(service: Any) -> float:
    """Time one in-process read-path assembly, in milliseconds.

    This measures the Redis and assembly cost only. It deliberately does not
    include Python interpreter startup, which dominates the end-to-end
    subprocess number and is measured separately by
    ``tests/test_integrations_latency.py``.
    """
    import time

    t0 = time.perf_counter()
    try:
        service.assemble("doctor latency probe", session_id=None)
    except Exception:
        pass
    return round((time.perf_counter() - t0) * 1000, 2)


def _cmd_demo(args: Any) -> int:
    """Run the zero-key end-to-end loop."""
    from .demo import run_demo

    return run_demo(agent_id=args.agent_id, keep=args.keep, out=sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
