# popoto-memory: Codex wiring

The same `popoto-memory hook` command string as Claude Code. Codex sends
`hook_event_name` on stdin and reads `hookSpecificOutput.additionalContext`
from stdout, exactly as Claude Code does, so one executable serves both.

Setup is longer here, and the extra steps are not incidental.

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
cp plugins/codex/hooks.json ~/.codex/hooks.json
cat plugins/codex/config.toml.fragment >> ~/.codex/config.toml
```

Then, inside Codex, run `/hooks` and review the entry. Codex hashes every
non-managed command hook and skips it until you have approved that exact
definition. Editing the command re-triggers the review. This is a security
feature: a hook is an arbitrary command that Codex runs on your behalf on
every turn.

Two things to know before you debug a silent failure:

- **A project-level `.codex/hooks.json` in an untrusted project is skipped
  with no message.** Verified first-hand against codex-cli 0.144.4: with a
  project hooks file and `codex exec --enable hooks
  --dangerously-bypass-hook-trust`, no hook ran and nothing was reported.
  Install at `~/.codex/hooks.json` instead.
- **Codex caps injected context** at 2500 tokens
  (`additionalContextLimit`). The default `POPOTO_MEMORY_MAX_TOKENS` is 800,
  well under it.

MCP alone, without hooks:

```bash
codex mcp add popoto-memory -- popoto-memory mcp
```

That gives the four tools but not the subconscious loop, because MCP tools
only run when the model elects to call them.

See `docs/guides/harness-codex.md` for the full guide.
