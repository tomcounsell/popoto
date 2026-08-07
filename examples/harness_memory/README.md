# Harness memory, without a harness

Everything the Claude Code hook does, driven from Python against your own
Redis or Valkey. No API keys, no harness installed, no network.

## Requirements

A Redis or Valkey server on `localhost:6379`, and `pip install popoto`. The
`[mcp]` extra is not needed here: the hook path does not use MCP.

```bash
redis-server        # or: valkey-server
```

## Run it

```bash
popoto-memory demo                          # narrated tour of the loop
python examples/harness_memory/verify.py    # the same loop as assertions
python examples/harness_memory/seed.py      # load your own notes
```

`demo` prints each step. `verify.py` runs the same steps as pass/fail checks
and exits non-zero on failure, so it doubles as a post-install smoke test.
Both clean up after themselves.

## What you are looking at

```
user types
    |
    v  UserPromptSubmit hook            <- popoto-memory hook
assemble from Redis (~5 ms)
    |
    v  context appended to the USER turn, not the system prompt
model
    |
    v  Stop hook, async                 <- popoto-memory hook
turn saved verbatim, outcomes reported
    |
    v
next turn's assembly is better
```

Two properties are worth watching for, because they are the two decisions
that make this different from a memory MCP server:

**Nothing here is a tool call.** The read and write both happen because the
harness reached a point in its own turn loop. The model never elects to
remember. MCP tools exist too (`memory_search`, `memory_save`,
`memory_feedback`, `memory_status`) but they are for deliberate use, not for
recall.

**One turn becomes one record, verbatim.** `verify.py` asserts this. Issue
#489 measured sentence-splitting extraction at 0.2078 judged accuracy
against 0.3636 for raw ingestion on the same slice, so raw is the harness
default. `POPOTO_MEMORY_INGEST=heuristic` switches to splitting and logs that
cost the first time.

## Pointing it somewhere else

```bash
export POPOTO_MEMORY_URL=redis://localhost:6379/3
export POPOTO_MEMORY_AGENT_ID=my-project
```

Without `POPOTO_MEMORY_URL`, these scripts use whatever connection Popoto
itself resolved, which is `REDIS_URL` if set and `localhost:6379/0`
otherwise. The full variable table is in
`docs/features/harness-integration.md`.

## Wiring it into a real harness

`docs/guides/harness-claude-code.md` is the reference. The other three are
`harness-codex.md`, `harness-hermes.md`, and `harness-openclaw.md`.
