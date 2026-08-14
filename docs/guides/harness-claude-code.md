# Add Memory to Claude Code

Claude Code is the reference wiring. It has the richest verified hook
contract, a packaged two-command install, and it is the harness this
integration's payload fixtures were captured from live.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`. Hooks work on a bare
    `pip install popoto`; the extra is only for the MCP tools.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

`doctor` must print `redis reachable` and `retrieval lexical
(query-sensitive)` before you go further. If Redis is not running:

```bash
redis-server        # or: valkey-server
```

Then either the plugin or the manual block. They do the same thing.

### Plugin

```bash
claude plugin marketplace add tomcounsell/popoto
claude plugin install popoto-memory@popoto
```

### Manual

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "popoto-memory hook" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "popoto-memory hook", "async": true }] }
    ]
  }
}
```

Eight lines. `"async": true` on `Stop` keeps the write off the turn's
critical path.

To scope memory to one project rather than to `~/.claude/settings.json`
globally, put the same block in that project's `.claude/settings.json`.

Restart Claude Code either way.

## Verify

Have a conversation that establishes a fact, then start a new session and
ask about it.

```
you:    we deploy blue-green, and a failed health check rolls back automatically
claude: ...

(new session)

you:    what happens if a deploy fails its health check?
claude: it rolls back automatically to the previous green environment
```

The recall happened because `UserPromptSubmit` fired, not because Claude
decided to look something up.

Then:

```bash
popoto-memory doctor
```

`last assemble` and `last capture` carry recent timestamps, `records` is
climbing, and `failures` reads `none`. Those three lines are the whole
health check.

## What is stored, and where

In your Redis or Valkey, in the `DefaultMemory:*` keyspace, tagged with an
`agent_id` that defaults to the basename of your working directory. That tag
is honored as a read filter only on the composite-score retrieval path; the
shipped default lexical/BM25 path does not filter by it yet ([#576](https://github.com/tomcounsell/popoto/issues/576)), so two
projects on one Redis can retrieve each other's memories. Use a distinct
`POPOTO_MEMORY_URL` database per project for real isolation.

One turn becomes one record, verbatim. Issue #489 measured
sentence-splitting extraction at 0.2078 judged accuracy against 0.3636 for
raw ingestion on the same slice, so raw is the default here.

Nothing leaves your machine. There is no API key because there is nothing to
authenticate to.

Inspect it directly:

```bash
redis-cli --scan --pattern 'DefaultMemory:*' | head
```

## Latency

Measured on an Apple M-series laptop, Python 3.12.13, redis 8.6.2 on
localhost, 50 records in scope, 25 subprocess invocations of the read hook:
**p50 193 ms, p95 200 ms, max 211 ms**, against a 400 ms p95 budget that
`tests/test_integrations_latency.py` enforces.

Nearly all of that is Python interpreter startup. The Redis work itself is
1-2 ms.

## The MCP tools

The plugin also registers a stdio MCP server, or add it yourself:

```json
{ "mcpServers": { "popoto-memory": { "command": "popoto-memory", "args": ["mcp"] } } }
```

`memory_search`, `memory_save`, `memory_feedback`, `memory_status`. These
are for deliberate use: searching for something specific mid-task, saving a
fact the turn text would not preserve, marking a memory wrong. Recall and
capture do not depend on them, and Claude never has to call one for memory
to work.

## Tuning

Every setting is an environment variable and every one is optional. The full
table is in [Harness Integration](../features/harness-integration.md). The
three worth knowing:

```bash
export POPOTO_MEMORY_AGENT_ID=my-project      # share one corpus across directories
export POPOTO_MEMORY_MAX_ITEMS=10             # default 5
export POPOTO_MEMORY_ENABLED=0                # kill switch, no config edit
```

Hooks inherit your shell environment, so exporting these in your shell
profile is enough.

## When it is not working

```bash
popoto-memory doctor
```

| What `doctor` says | What it means |
|---|---|
| `redis UNREACHABLE` | No server, or the wrong `POPOTO_MEMORY_URL`. It prints the fix. |
| `last activity never` | The hooks are not firing. Check that `popoto-memory` is on the `PATH` your harness sees, and that you restarted Claude Code. |
| `retrieval composite -- QUERY-BLIND` | Ranking is ignoring the prompt text. See [Query-Blind Retrieval](query-blind-retrieval.md). |
| `FAILURES assemble=N` | Something is raising on the read path. The last five log lines print underneath. |
| `records 0` after several turns | Capture is not running. Check the `Stop` hook specifically; `UserPromptSubmit` can work while `Stop` does not. |

Test a hook by hand, which is exactly what Claude Code does:

```bash
echo '{"hook_event_name":"UserPromptSubmit","session_id":"t","prompt":"how do deploys work?"}' \
  | popoto-memory hook
```

You should get one line of JSON, or nothing at all if no memory matched.
Never a traceback, and never a non-zero exit.

## Uninstall

```bash
claude plugin uninstall popoto-memory@popoto   # or delete the settings block
pip uninstall popoto
```

Your memories stay in your Redis until you delete them:

```bash
redis-cli --scan --pattern 'DefaultMemory:*' | xargs redis-cli del
```

## Verified against

`claude` 2.1.220, on macOS, 2026-08-07. The `UserPromptSubmit` and `Stop`
payloads in `tests/fixtures/harness_payloads/claude_code_*.json` were
captured from a live run of that version, and each file records the command
that reproduces the capture.
