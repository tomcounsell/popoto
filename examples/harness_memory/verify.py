"""Prove the harness memory loop works, with no harness and no API keys.

Drives the exact code path a real hook takes: feeds captured Claude Code hook
payloads through ``popoto.integrations.hooks`` and asserts that the read hook
injects and the write hook captures. Exits non-zero if any step fails, so it
works as a smoke test in CI or after an install.

Usage::

    python examples/harness_memory/verify.py

Requires a local Redis or Valkey. Nothing else: no API key, no harness, no
network.
"""

import json
import sys
import uuid

READ_PAYLOAD = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": None,  # filled in at runtime
    "cwd": None,
    "prompt": "how does the deploy roll back when health checks fail?",
}

WRITE_PAYLOAD = {
    "hook_event_name": "Stop",
    "session_id": None,
    "cwd": None,
    "stop_hook_active": False,
    "last_assistant_message": (
        "A failed health check triggers an automatic rollback to the previous "
        "green environment, and the deploy is marked failed in the pipeline."
    ),
}

FAILURES = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def main() -> int:
    from popoto.integrations import MemoryConfig, MemoryService
    from popoto.integrations import hooks

    run_id = uuid.uuid4().hex[:8]
    agent_id = f"harness-memory-example-{run_id}"
    session_id = f"session-{run_id}"

    base = MemoryConfig.from_env()
    config = MemoryConfig(
        url=base.url,
        agent_id=agent_id,
        max_items=base.max_items,
        max_tokens=base.max_tokens,
        ingest=base.ingest,
        enabled=True,
        log_path=base.log_path,
        url_is_explicit=base.url_is_explicit,
    )
    service = MemoryService(config)

    print(f"harness memory verification  (redis {config.url}, agent {agent_id})\n")

    try:
        service.redis.ping()
    except Exception as exc:
        print(f"Redis is not reachable at {config.url}: {exc}")
        print("Start one with `redis-server` or `valkey-server`, then retry.")
        return 1

    read = dict(READ_PAYLOAD, session_id=session_id, cwd=".")
    write = dict(WRITE_PAYLOAD, session_id=session_id, cwd=".")

    try:
        print("1. an empty corpus injects nothing")
        check(
            "read hook stays silent when there is nothing to recall",
            hooks.handle_payload(read, service=service) is None,
        )

        print("\n2. the write hook captures the turn")
        hooks.handle_payload(write, service=service)
        stored = service.model.query.filter(agent_id=agent_id)
        check("exactly one record for one turn", len(stored) == 1, f"got {len(stored)}")
        check(
            "the turn is stored verbatim, not sentence-split",
            len(stored) == 1 and stored[0].content == write["last_assistant_message"],
        )
        check(
            "the write path used raw ingestion",
            type(service.extractor).__name__ == "RawTurnExtractionProvider",
            type(service.extractor).__name__,
        )

        print("\n3. the read hook injects it on the next turn")
        output = hooks.handle_payload(read, service=service)
        check("the read hook produced output", output is not None)
        if output:
            decoded = json.loads(output)
            block = decoded["hookSpecificOutput"]["additionalContext"]
            check("output uses the harness's own response shape", bool(block))
            check("the captured turn came back", "automatic rollback" in block)
            check(
                "nothing was injected into a system message",
                "system" not in output.lower(),
            )

        print("\n4. outcomes are reported as used")
        check(
            "the injected records were reported as used "
            "(confirms the read, no confidence change)",
            service.feedback(session_id, outcome="used") == 1,
        )

        print("\n5. doctor sees a healthy setup")
        status = service.status()
        check("redis reachable", status["redis_reachable"] is True)
        check(
            "retrieval is query-sensitive",
            status["retrieval_mode"] == "lexical",
            str(status["retrieval_mode"]),
        )
        check(
            "no failures recorded",
            not [k for k in (status["counters"] or {}) if not k.endswith("_ok")],
        )
    finally:
        removed = 0
        for record in service.model.query.filter(agent_id=agent_id):
            try:
                record.delete()
                removed += 1
            except Exception:
                pass
        for pattern in (
            f"$popoto_memory:pending:{agent_id}:*",
            f"$popoto_memory:counter:{agent_id}:*",
            f"$popoto_memory:last:{agent_id}:*",
        ):
            for key in service.redis.scan_iter(match=pattern, count=200):
                service.redis.delete(key)
        print(f"\n   cleaned up {removed} record(s)")

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll checks passed. The same code path runs inside the harness.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
