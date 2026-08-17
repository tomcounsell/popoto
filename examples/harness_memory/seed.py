"""Seed a project's memory corpus without running a harness.

Useful for trying retrieval against content you care about instead of the
demo's fixtures, and for pre-loading a repo's conventions so the first turn
in a new session already has something to recall.

Usage::

    python examples/harness_memory/seed.py
    python examples/harness_memory/seed.py --agent-id my-project --file notes.txt

With ``--file``, each non-empty line becomes one memory record, matching the
one-record-per-turn shape the hook writes.
"""

import argparse
import sys

DEFAULT_NOTES = [
    "Deploys use blue-green with automatic rollback on health-check failure",
    "The staging database is reset every night at 02:00 UTC",
    "Rate limits are enforced in the gateway, not in each service",
    "Frontend bundles are built with esbuild, not webpack",
    "Migrations run in a separate job before the new version takes traffic",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--agent-id",
        default=None,
        help="memory partition (default: the basename of the working directory)",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="file whose non-empty lines each become one memory record",
    )
    parser.add_argument(
        "--importance",
        type=float,
        default=0.8,
        help="base importance for every seeded record (default: 0.8)",
    )
    args = parser.parse_args(argv)

    from popoto.integrations import MemoryConfig, MemoryService

    config = MemoryConfig.from_env()
    if args.agent_id:
        config = MemoryConfig(
            url=config.url,
            agent_id=args.agent_id,
            max_items=config.max_items,
            max_tokens=config.max_tokens,
            ingest=config.ingest,
            enabled=True,
            log_path=config.log_path,
            url_is_explicit=config.url_is_explicit,
        )
    service = MemoryService(config)

    try:
        service.redis.ping()
    except Exception as exc:
        print(f"Redis is not reachable at {config.url}: {exc}")
        print("Start one with `redis-server` or `valkey-server`, then retry.")
        return 1

    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            notes = [line.strip() for line in handle if line.strip()]
    else:
        notes = DEFAULT_NOTES

    for note in notes:
        service.model(
            agent_id=config.agent_id, content=note, importance=args.importance
        ).save()
        print(f"+ {note}")

    print(f"\nSeeded {len(notes)} record(s) for agent {config.agent_id!r}.")
    print("Run `python examples/harness_memory/verify.py` to exercise the loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
