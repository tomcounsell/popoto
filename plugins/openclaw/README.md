# popoto-memory: OpenClaw wiring

**OpenClaw gets the MCP tools this cycle. It does not get automatic
injection or capture.** That is the honest state, not a soft launch.

Merge `openclaw.json.fragment` into `~/.openclaw/openclaw.json` under
`mcp.servers`:

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

That gives you `memory_search`, `memory_save`, `memory_feedback`, and
`memory_status`. The model calls them when it decides to, which means memory
is something it elects to use rather than something that happens every turn.

## Why the automatic half is missing

OpenClaw's per-turn hooks (`before_prompt_build`, `llm_output`) live in
TypeScript plugins, not in a command string. A thin plugin that shells out
to `popoto-memory hook` is the right shape -- the adapter already speaks
OpenClaw's `appendContext` response, and
`tests/fixtures/harness_payloads/openclaw_*.json` round-trip through it --
but whether an OpenClaw plugin may spawn a subprocess is unverified.
Plugins load as in-process Node modules, so `child_process` should be
reachable, and no documentation says otherwise. "Should be" is not a
verification, and OpenClaw is not installed on any machine this repo is
developed on, so the plugin is not shipped.

The alternative -- reimplementing the memory path in TypeScript -- is a
second implementation of the core and is out of scope at any price.

Resolving this needs one person with OpenClaw installed to run a plugin that
calls `child_process.execFile`. When that lands, the plugin is thin: pass the
event through to `popoto-memory hook` and return its JSON.
