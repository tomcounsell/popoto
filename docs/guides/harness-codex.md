# Add Memory to Codex

Codex sends `hook_event_name` on stdin and reads
`hookSpecificOutput.additionalContext` from stdout, exactly as Claude Code
does. The same `popoto-memory hook` command string serves both, so the
behavior is identical once it is wired up.

Wiring it up is not identical. Codex gates hooks twice, and both gates are
worth understanding before you debug a silent failure.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

Write `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "user_prompt_submit": [
      { "hooks": [{ "type": "command", "command": "popoto-memory hook" }] }
    ],
    "stop": [
      { "hooks": [{ "type": "command", "command": "popoto-memory hook", "async": true }] }
    ]
  }
}
```

Note the snake_case event names: Codex matches hooks on `user_prompt_submit`
and `stop` in configuration, while the payload it sends carries
`hook_event_name: "UserPromptSubmit"`. The adapter accepts both spellings.

Turn hooks on in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

Then, inside Codex, run `/hooks` and approve the entry.

## The trust review is a feature, not a step to skip

Codex hashes every non-managed command hook and skips it until you have
reviewed that exact definition. Editing the command re-triggers the review.

That is correct behavior. A hook is an arbitrary command that Codex runs on
your behalf before and after every turn, with your environment. A memory
integration that silently installed one would be indistinguishable from
something worse. Read the command, then approve it.

There is a `--dangerously-bypass-hook-trust` flag for automation. The name
is accurate; do not use it interactively.

## The failure mode that wastes an afternoon

**A project-level `.codex/hooks.json` in an untrusted project is skipped
with no message at all.** Codex does not warn, log, or fail. Memory simply
never happens.

This was reproduced first-hand against codex-cli 0.144.4: with a project
`.codex/hooks.json` and `codex exec --enable hooks
--dangerously-bypass-hook-trust`, no hook ran and nothing was reported.

Install at `~/.codex/hooks.json` instead.

`POPOTO_MEMORY_AGENT_ID` tags writes and is honored as a read filter on
every shipped retrieval path since 1.9.0, the default lexical/BM25 one
included ([#576](https://github.com/tomcounsell/popoto/issues/576)). On
1.8.2 and earlier it was honored only on the composite-score path, so setting
it per project did **not** isolate one project's memories from another's on
the same database:

```bash
export POPOTO_MEMORY_AGENT_ID=my-project
```

For a boundary enforced by Redis rather than by a query filter, point each
project at its own `POPOTO_MEMORY_URL` database (any database but 0, which
is refused — see [Harness Integration](../features/harness-integration.md#database-0-is-refused)).

## Corpus growth and eviction

!!! danger "Upgrading to 1.9.0+ on an existing corpus can delete records"
    `DefaultMemory` caps itself at 1000 records per `agent_id`. If your
    corpus is already over that when you upgrade, the first save afterward
    deletes the *entire* excess at once, synchronously, inside that one
    save — not a gradual trim, and no tombstone. Check the record count
    with `popoto-memory doctor` before upgrading. `POPOTO_DEFAULT_MEMORY_MAX_RECORDS=0`
    (no config edit needed) disables eviction first if you want to size or
    stage the cleanup yourself. See
    [Corpus growth](../features/harness-integration.md#corpus-growth) for
    the full mechanics.

## Context limit

Codex caps injected context at 2500 tokens (`additionalContextLimit`). The
default `POPOTO_MEMORY_MAX_TOKENS` is 800, comfortably under it. If you
raise it, stay below the cap: Codex will truncate rather than tell you.

## MCP tools

```bash
codex mcp add popoto-memory -- popoto-memory mcp
```

Or in `~/.codex/config.toml`:

```toml
[mcp_servers.popoto-memory]
command = "popoto-memory"
args = ["mcp"]
```

This registers `memory_search`, `memory_save`, `memory_feedback`, and
`memory_status`. It does **not** give you the subconscious loop: MCP tools
only run when the model elects to call them. If you configure MCP and skip
the hooks, you have instructed memory, not subconscious memory.

## Verify

```bash
popoto-memory doctor
```

After a few turns, `last assemble` and `last capture` carry recent
timestamps and `records` climbs. If both stay at `never`, the hooks are not
firing: check `[features] hooks = true`, check that you approved them via
`/hooks`, and check that the file is at `~/.codex/hooks.json` rather than in
a project.

Test the command directly, which is what Codex does:

```bash
echo '{"hook_event_name":"UserPromptSubmit","session_id":"t","prompt":"how do deploys work?"}' \
  | popoto-memory hook
```

## Verified against

`codex-cli` 0.144.4. The hook input contract was read from that binary's own
schema: `session_id`, `transcript_path`, `hook_event_name`,
`permission_mode`, `turn_id`, `agent_transcript_path`, `agent_type`,
`last_assistant_message`, `prompt`, `cwd`, `stop_hook_active` -- matching
Claude Code field for field, which is why one executable serves both.

The fixtures in `tests/fixtures/harness_payloads/codex_*.json` are derived
from that schema, **not** from a live turn: the live capture attempt hit the
silent project-level skip described above. A live acceptance run on a
trusted project is still outstanding. Each fixture records this in its
`_provenance` field.
