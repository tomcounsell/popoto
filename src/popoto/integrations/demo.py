"""The ``popoto-memory demo`` loop: assemble, inject, capture, report.

Runs the entire subconscious cycle against a local Redis or Valkey with no
harness installed and no API keys, so a developer can see what the hook does
before wiring it into anything. Every step prints what it did, including
which extraction provider wrote the record -- the write path is the part
most worth seeing, because it is where memory systems usually differ.
"""

from typing import Any, List, Optional, TextIO

SEED_MEMORIES = [
    ("Deploys use blue-green with automatic rollback on health-check failure", 0.9),
    ("The staging database is reset every night at 02:00 UTC", 0.6),
    ("Rate limits are enforced in the gateway, not in each service", 0.7),
    ("Frontend bundles are built with esbuild, not webpack", 0.5),
]
"""Fixture memories for the demo. Distinct topics, so the retrieval step
visibly selects rather than returning everything."""

DEMO_QUERY = "what happens if a deploy fails health checks?"
DEMO_TURN = (
    "A failed health check triggers an automatic rollback to the previous "
    "green environment, and the deploy is marked failed in the pipeline."
)


def run_demo(
    agent_id: str = "popoto-memory-demo",
    keep: bool = False,
    out: Optional[TextIO] = None,
) -> int:
    """Seed, retrieve, capture, and report -- printing each step.

    Args:
        agent_id: Partition to seed and query. Isolated from real memories
            by default so the demo cannot pollute a project's corpus.
        keep: Leave the seeded records in Redis when finished.
        out: Stream to write the transcript to. Defaults to ``sys.stdout``.

    Returns:
        ``0`` on success, ``1`` when Redis is unreachable or the loop did
        not complete.
    """
    import sys

    out = out or sys.stdout

    from .config import MemoryConfig
    from .service import MemoryService

    config = MemoryConfig.from_env()
    config = MemoryConfig(
        url=config.url,
        agent_id=agent_id,
        max_items=config.max_items,
        max_tokens=config.max_tokens,
        ingest=config.ingest,
        enabled=True,
        log_path=config.log_path,
        url_is_explicit=config.url_is_explicit,
    )
    try:
        service = MemoryService(config)
    except ValueError as exc:
        out.write(f"popoto-memory demo\n\n{exc}\n")
        return 1

    out.write("popoto-memory demo\n")
    out.write(f"  redis   {config.url}\n")
    out.write(f"  agent   {agent_id}\n\n")

    try:
        service.redis.ping()
    except Exception as exc:
        out.write(f"Redis is not reachable: {exc}\n")
        out.write("Start one with `redis-server` or `valkey-server`, then retry.\n")
        return 1

    model = service.model
    seeded = []
    out.write("1. seed\n")
    for content, importance in SEED_MEMORIES:
        record = model(agent_id=agent_id, content=content, importance=importance)
        record.save()
        seeded.append(record)
        out.write(f"   + {content}\n")

    out.write(f"\n2. assemble  query: {DEMO_QUERY!r}\n")
    session_id = "demo-session"
    context = service.assemble(DEMO_QUERY, session_id=session_id)
    if not context.strip():
        out.write("   (nothing retrieved -- unexpected; see popoto-memory doctor)\n")
        _cleanup(seeded, keep, out)
        return 1
    for line in context.splitlines():
        out.write(f"   | {line}\n")

    out.write("\n3. inject\n")
    out.write("   the harness places that block in the user turn, as\n")
    out.write('   {"hookSpecificOutput": {"additionalContext": "..."}}\n')
    out.write("   the system prompt is untouched, so prompt caching survives\n")

    out.write("\n4. capture\n")
    out.write(f"   provider: {type(service.extractor).__name__}\n")
    keys = service.capture(DEMO_TURN, session_id=session_id)
    out.write(f"   wrote {len(keys)} record(s) for one turn\n")
    for key in keys:
        out.write(f"   + {key}\n")

    out.write("\n5. report outcome\n")
    updated = service.feedback(session_id, outcome="used")
    out.write(f"   marked {updated} injected record(s) as used\n")
    out.write(
        "   confirms the staged read and resolves predictions; "
        "confidence/decay are untouched\n"
    )
    out.write(
        "   (outcome=acted, which does affect confidence/decay, is a "
        "discretionary memory_feedback call)\n"
    )

    out.write("\n6. verify\n")
    again = service.assemble(DEMO_QUERY, session_id=None)
    out.write(f"   next turn would inject {len(again.splitlines())} line(s)\n")

    _cleanup(seeded + list(_written(model, keys)), keep, out)
    out.write("\ndone. `popoto-memory doctor` shows the same state any time.\n")
    return 0


def _written(model: Any, keys: List[str]) -> List[Any]:
    """Rehydrate the records the demo captured, for cleanup."""
    if not keys:
        return []
    try:
        return [r for r in model.query.get_many(keys, skip_none=True) if r is not None]
    except Exception:
        return []


def _cleanup(records: List[Any], keep: bool, out: TextIO) -> None:
    if keep:
        out.write("\n   (--keep: seeded records left in Redis)\n")
        return
    removed = 0
    for record in records:
        try:
            record.delete()
            removed += 1
        except Exception:
            continue
    out.write(f"\n   cleaned up {removed} demo record(s)\n")
