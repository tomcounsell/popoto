# Add Memory to Hermes

Hermes is the only harness here whose hooks are Python. The handler runs
inside the gateway process, so the read path is a single Redis round trip
with no interpreter startup at all -- the fastest of the four.

!!! warning "Verified against documentation, not a live run"
    Hermes is not installed on any machine this integration is developed on.
    The hook contract below comes from Nous Research's documentation, and
    the payload fixtures in `tests/fixtures/harness_payloads/hermes_*.json`
    are docs-derived, so the round-trip tests prove our reading of the docs
    rather than the harness itself. If the field names have moved,
    `popoto-memory doctor` will show no `last assemble` timestamp and the
    fix is one function in `popoto/integrations/hooks.py`.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
mkdir -p ~/.hermes/hooks/popoto-memory
cp plugins/hermes/HOOK.yaml plugins/hermes/handler.py ~/.hermes/hooks/popoto-memory/
hermes mcp add popoto-memory -- popoto-memory mcp
```

Two files, no config file to edit.

`HOOK.yaml` declares the events:

```yaml
name: popoto-memory
events:
  - pre_llm_call
  - post_llm_call
```

`handler.py` exposes `async def handle(event_type, context)` and forwards
straight to the shared adapter. It builds one `MemoryService` and keeps it
for the life of the gateway, so there is no per-turn setup cost.

## Why in-process matters here

On Claude Code and Codex the read hook is a subprocess: p95 200 ms,
essentially all of it Python startup. On Hermes none of that applies. The
same assembly measures 1-2 ms in-process, which is what `popoto-memory
doctor` reports as `hook read`.

The tradeoff is that a bug in the handler is a bug in the gateway process.
The handler is written accordingly: it catches everything, returns `None` on
any failure, and lets the reason land in `~/.popoto/memory.log` and in
`popoto-memory doctor`.

## Injection lands in the user message

Hermes places `pre_llm_call` context in the user message rather than the
system prompt, and documents that the choice is to preserve prompt caching.
That is the same placement Claude Code's `additionalContext` gets, and it is
why per-turn injection does not invalidate a cached prefix.

`MemoryService` returns a context string and never touches a message array,
so this stays the harness's decision.

## Verify

```bash
popoto-memory doctor
```

After a few turns, `last assemble` and `last capture` carry recent
timestamps and `records` climbs.

If they stay at `never`, the handler is not being reached. Test the adapter
directly, bypassing Hermes:

```bash
echo '{"event_type":"pre_llm_call","session_id":"t","extra":{"user_message":"how do deploys work?"}}' \
  | popoto-memory hook
```

If that returns `{"context": "..."}` but Hermes still shows nothing, the
problem is the hook registration or the payload field names, not the memory
layer.

## MCP tools

`hermes mcp add popoto-memory -- popoto-memory mcp` registers
`memory_search`, `memory_save`, `memory_feedback`, and `memory_status` for
deliberate use. Recall and capture do not depend on them.

## Configuration

Identical to every other harness, and all environment-driven. See
[Harness Integration](../features/harness-integration.md) for the table. The
gateway must have these in its environment, not just your interactive shell,
since the handler runs in the gateway process.
