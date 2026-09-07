# popoto-memory: OpenClaw wiring

OpenClaw gets both halves: the MCP tools the model elects to call, and per-turn
recall and capture that run whether it elects to or not.

## The MCP tools

Merge `openclaw.json.fragment` into `~/.openclaw/openclaw.json` under
`mcp.servers`:

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

That gives you `memory_search`, `memory_save`, `memory_feedback`, and
`memory_status`.

## The plugin

`popoto-memory-plugin/` is a shippable OpenClaw plugin — hand-written ESM, no npm
dependencies beyond OpenClaw's own peer SDK import. It registers
`before_prompt_build` (Modify, returns `appendContext`) and `llm_output`
(Observe), turns each `(event, ctx)` pair into the JSON envelope
`popoto-memory hook` reads on stdin, and returns what that command prints.

Install and rollback steps, the four operator gates, and the failure diagnostics
are in [the guide](../../docs/guides/harness-openclaw.md). Three details are
worth stating here, next to the code they explain:

- **`session_id`, `cwd` and `turn_id` come from the second argument.** OpenClaw
  puts them on `ctx` as `sessionId`, `workspaceDir` and `runId`, not on the
  event. Reading any of the three off `event` yields `undefined`.
- **`assistantTexts` is passed through as an array, unflattened.** The adapter's
  `_first_string()` reduces it. Joining it here would move the one non-trivial
  transformation on this path into untested JavaScript, and would make the
  committed fixture stop documenting what OpenClaw actually sends.
- **Both handlers fail silent.** A failed shell-out injects nothing and never
  fails the turn.

## Why there is no TypeScript reimplementation

Reimplementing the memory path in TypeScript would be a second implementation of
the core, and is out of scope at any price. That is a standing architectural
boundary, not a deferral: the plugin depends on popoto only through the
`popoto-memory` executable — no Python import, no shared schema beyond the JSON
envelope.

Whether an OpenClaw plugin may spawn a subprocess was the open question that kept
this half unshipped. It is settled by execution, not by reasoning about module
loading: on OpenClaw 2026.9.2 the shipped plugin spawned `popoto-memory hook`
from inside `before_prompt_build` during a live turn, and the model answered from
a fact that existed only in Redis. The fixtures in
`tests/fixtures/harness_payloads/openclaw_*.json` are what that run wrote.
