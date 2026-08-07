# Add Memory to OpenClaw

**OpenClaw gets the MCP tools. It does not get automatic injection or
automatic capture.** That is the current state, stated plainly rather than
buried: on OpenClaw this is instructed memory, not subconscious memory.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

Add to `~/.openclaw/openclaw.json` under `mcp.servers`:

```json
{
  "mcp": {
    "servers": {
      "popoto-memory": {
        "command": "popoto-memory",
        "args": ["mcp"]
      }
    }
  }
}
```

Restart OpenClaw. You get four tools:

| Tool | Purpose |
|---|---|
| `memory_search` | Find something stored earlier |
| `memory_save` | Store a fact |
| `memory_feedback` | Mark a memory `contradicted` or `acted` |
| `memory_status` | Connection, scope, retrieval mode, record count |

## What you are missing, and why

On Claude Code, Codex, and Hermes, memory runs on every turn because the
harness fires a hook that this package handles. The model is never asked.
That is the property that makes memory reliable: a memory system is only
worth having if things were actually recorded to recall later, and a model
that must choose to record will not choose consistently.

On OpenClaw the model has to call `memory_save` and `memory_search`. It will
sometimes. It will often not.

OpenClaw's per-turn hooks -- `before_prompt_build`, which returns
`appendContext`, and `llm_output` -- live in TypeScript plugins rather than
in a command string. The adapter here already speaks that contract:
`tests/fixtures/harness_payloads/openclaw_*.json` round-trip through it and
produce `{"appendContext": "..."}`. A thin plugin that shells out to
`popoto-memory hook` is all that is missing.

Whether an OpenClaw plugin may spawn a subprocess is unverified. Plugins
load as in-process Node modules, so `child_process` should be reachable, and
no documentation says otherwise. "Should be" is not verification, and
OpenClaw is not installed on any machine this integration is developed on,
so the plugin is not shipped.

The alternative -- reimplementing the memory path in TypeScript -- would be
a second implementation of the core, and is not going to happen.

Resolving this takes one person with OpenClaw installed running a plugin
that calls `child_process.execFile`. If that works, the plugin is a dozen
lines: pass the event through to `popoto-memory hook`, return its JSON.

## Getting more out of the tools in the meantime

Because recall is model-elected here, telling the agent when to search
actually helps, which is not true on the other harnesses. Something like
this in your instructions:

```
Before answering a question about how this project works, call
memory_search. After learning something durable about the project, call
memory_save.
```

That is a workaround for a missing hook surface, not the intended design.

## Verify

```bash
popoto-memory doctor
```

`records` climbing means `memory_save` is being called. If `records` stays
at 0 across a long session, the model is not electing to use the tools --
which is the whole problem described above, not a bug in the setup.

## Configuration

Identical to every other harness, all environment-driven. See
[Harness Integration](../features/harness-integration.md).
