# Add Memory to OpenClaw

OpenClaw gets both halves: the four MCP tools the model can call on purpose,
and — since the `popoto-memory` plugin shipped — per-turn recall and capture
that happen whether the model thinks to ask or not.

The automatic half runs on two OpenClaw hooks. `before_prompt_build` returns
`appendContext`, which is injected into the user turn before the model sees it;
`llm_output` reports the turn's outcome afterwards. The plugin is a translation
layer and nothing else: it turns OpenClaw's `(event, ctx)` arguments into the
JSON that `popoto-memory hook` reads on stdin, and hands back what that command
prints. Everything that decides what a memory is lives in Python, the same code
Claude Code, Codex, and Hermes reach.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

`popoto-memory` must be on the `PATH` of the process that runs OpenClaw. If it
is not — a virtualenv OpenClaw does not inherit is the usual reason — set
`POPOTO_MEMORY_BIN` to its absolute path instead.

### The MCP tools (the discretionary half)

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

### The plugin (the subconscious half)

The plugin ships as source under `plugins/openclaw/popoto-memory-plugin/` in the
popoto checkout. It is not published to npm or ClawHub, so you install it from a
local archive:

```bash
cd plugins/openclaw/popoto-memory-plugin
npm pack --pack-destination /tmp

openclaw plugins install npm-pack:/tmp/openclaw-popoto-memory-1.0.0.tgz \
    --accept-capabilities --force
openclaw config set \
    plugins.entries.popoto-memory.hooks.allowConversationAccess true
```

Then restart the gateway (or start a new `openclaw agent --local` run).

Both flags on the install are required and neither is optional politeness:
`--accept-capabilities` acknowledges the surface the plugin registers, and
`--force` acknowledges that a local archive is outside ClawHub's review and trust
metadata. Without them the install is refused — loudly, which makes this the
easiest of the four gates below.

The plugin's config key is `popoto-memory`, from its `openclaw.plugin.json`
manifest id, **not** `openclaw-popoto-memory`, its npm package name. The install
output says so; the `config set` line above uses the manifest id.

## Verify

```bash
openclaw plugins inspect popoto-memory --runtime --json
```

A working install reports:

```json
{
  "plugin": { "status": "loaded", "enabled": true, "activated": true,
              "hookCount": 2 },
  "diagnostics": []
}
```

`hookCount: 2` is the load-bearing number. Check it positively rather than
looking for an error, because the two ways this setup fails do not produce one.

## Troubleshooting

### `openclaw agent exec` loads no external plugins

This is the expensive one, and it is worth reading before anything else on this
page. A verification run through `openclaw agent exec` completes normally,
produces a perfectly good answer, and fires none of your plugin's hooks. Nothing
reports an error, because from `exec`'s point of view nothing went wrong. The
result looks exactly like "this capability does not exist" — it cost a full false
negative during popoto's own verification of this feature.

Verify with `openclaw agent --local` or through the gateway. Never with
`agent exec`.

### `status: "loaded"` with `hookCount: 0`

Hooks are blocked for non-bundled plugins unless the operator opts in. The plugin
still reports `enabled: true`, `activated: true`, and `status: "loaded"` — it did
load; it just was not allowed to register hooks. The reason appears only under
`diagnostics` in `openclaw plugins inspect popoto-memory --runtime --json`.

The fix is the config key from the install steps:

```bash
openclaw config set \
    plugins.entries.popoto-memory.hooks.allowConversationAccess true
```

### Memory is silent

The plugin fails silent by design: if `popoto-memory` cannot be reached, the
`before_prompt_build` handler injects nothing and the turn proceeds without
memory. That is deliberate — a memory outage should degrade to an agent without
memory, never to an agent that cannot answer — but it means a broken `PATH` looks
identical to an empty memory store.

To tell them apart, set `POPOTO_HOOK_CAPTURE` to a writable directory and run one
turn. The plugin tees every envelope it sends to `<dir>/before_prompt_build.json`
and `<dir>/llm_output.json`, and appends any handler failure to
`<dir>/errors.log`:

```
before_prompt_build: Error: spawnSync popoto-memory ENOENT
```

If the envelopes are there and `errors.log` is not, the plugin is working and the
store is simply empty; `popoto-memory doctor` will confirm the record count.

## Uninstall / rollback

Two steps, because the config key survives the uninstall:

```bash
openclaw plugins uninstall popoto-memory
openclaw config set \
    plugins.entries.popoto-memory.hooks.allowConversationAccess false
```

Removing the plugin leaves the MCP tools in place; the model can still call
`memory_search` and `memory_save`. To remove those too, delete the
`mcp.servers.popoto-memory` entry from `openclaw.json`. Nothing stored in Redis
is touched by either — uninstalling stops new memories being written, it does not
delete the ones you have.

## Configuration

Identical to every other harness, all environment-driven. See
[Harness Integration](../features/harness-integration.md).
