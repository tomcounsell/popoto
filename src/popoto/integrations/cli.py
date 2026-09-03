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
    Print resolved configuration, Redis reachability, effective retrieval
    mode, record count, failure counters, and a measured hook round trip.
    This is the user-visible error surface; a hook has no console.
``demo``
    Seed a few memories, retrieve them, capture a turn, and report an
    outcome, against local Redis with no harness and no API keys.

Startup latency is the reason this module imports almost nothing at module
scope. The read hook is synchronous and on the critical path of every turn,
so ``popoto`` itself, ``redis``, and the ``mcp`` SDK are all imported inside
the subcommand that needs them.
"""

import argparse
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
            "Redis or Valkey. No API keys."
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
            "Prints resolved configuration, Redis reachability, effective "
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
        help="exercise the full loop against local Redis, no harness",
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
        help="leave the seeded records in Redis when finished",
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
    """Print the diagnostic report. Returns 1 when Redis is unreachable."""
    from .config import MemoryConfig
    from .service import MemoryService

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
                json.dumps({"redis_reachable": False, "error": str(exc)}, indent=2)
                + "\n"
            )
        else:
            sys.stdout.write(f"popoto-memory doctor\n\n{exc}\n")
        return 1
    info = service.status()

    if not args.no_latency and info.get("redis_reachable"):
        info["hook_read_ms"] = _measure_hook_read(service)

    if args.json:
        import json

        sys.stdout.write(json.dumps(info, indent=2, default=str) + "\n")
        return 0 if info.get("redis_reachable") else 1

    lines = ["popoto-memory doctor", ""]
    if info["enabled"]:
        lines.append("  status         enabled")
    else:
        lines.append("  status         DISABLED (POPOTO_MEMORY_ENABLED=0)")
    lines.append(f"  redis url      {info['redis_url']}")
    lines.append(f"  url source     {info.get('url_source', 'default')}")
    if info["redis_reachable"]:
        lines.append(
            f"  redis          reachable, {info['server']}, "
            f"ping {info.get('ping_ms')} ms"
        )
    else:
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
    failures = {k: v for k, v in counters.items() if not k.endswith("_ok")}
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
