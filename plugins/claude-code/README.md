# popoto-memory: Claude Code plugin

Bundles the two halves of the integration into one installable unit:

- `hooks/hooks.json` -- `UserPromptSubmit` recalls, `Stop` captures. These
  fire on every turn, whether or not the model asks for them. This is the
  subconscious half.
- `.mcp.json` -- registers the `popoto-memory mcp` server, exposing
  `memory_search`, `memory_save`, `memory_feedback`, and `memory_status`
  for deliberate use mid-task.

## Install

The plugin declares the config; the behavior lives in the pip package, so
install that first.

```bash
pip install 'popoto[mcp]'
popoto-memory doctor          # confirms Redis, model, retrieval mode
claude plugin marketplace add tomcounsell/popoto
claude plugin install popoto-memory@popoto
```

Restart Claude Code. The next turn carries relevant memories; the turn after
that remembers what was just said.

`popoto[mcp]` is only needed for the MCP tools. A bare `pip install popoto`
is enough for the hooks.

## Verify

```bash
popoto-memory doctor
```

`last assemble` and `last capture` timestamps appear once turns have run
through the hooks. `failures` should read `none`.

## Uninstall

```bash
claude plugin uninstall popoto-memory@popoto
pip uninstall popoto
```

Your memories stay in your Redis until you delete them.

## Configuration

Every setting is an environment variable and every one is optional. See
`docs/features/harness-integration.md` for the table. The one worth knowing
up front is `POPOTO_MEMORY_ENABLED=0`, a kill switch that needs no config
edit.
