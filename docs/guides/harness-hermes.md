# Add Memory to Hermes

Hermes is the only harness here whose hooks are Python. The plugin runs
inside the gateway process, so the read path is a single Redis round trip
with no interpreter startup at all -- the fastest of the four.

!!! note "Two hook systems, not one"
    Hermes 0.19.0 ships two independent mechanisms: gateway **hooks**
    (`~/.hermes/hooks/`, shell-invoked) and **plugins**
    (`~/.hermes/plugins/`, Python, loaded in-process). This integration is a
    plugin. If you set up an earlier version of this guide that installed
    into `~/.hermes/hooks/popoto-memory`, remove it first:
    `rm -rf ~/.hermes/hooks/popoto-memory`.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`.

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
mkdir -p ~/.hermes/plugins/popoto-memory
cp plugins/hermes/plugin.yaml plugins/hermes/__init__.py ~/.hermes/plugins/popoto-memory/
hermes plugins enable popoto-memory
hermes mcp add popoto-memory --command popoto-memory --args mcp
```

Two files, no config file to edit -- but the enable step is not optional.
Plugins are opt-in via `plugins.enabled` in `~/.hermes/config.yaml`
(read by `_get_enabled_plugins()`, `hermes_cli/plugins.py:243-270`); a plugin
present on disk but absent from that list is recorded as `enabled=False`
and never loaded, with no startup warning. Confirm with:

```bash
hermes plugins list   # must show "popoto-memory  enabled"
```

`plugin.yaml` declares the plugin's identity and the events it registers
for (`pre_llm_call`, `post_llm_call`); `__init__.py` builds one
`MemoryService` at first use and keeps it for the life of the gateway, so
there is no per-turn setup cost.

## Why in-process matters here

On Claude Code and Codex the read hook is a subprocess: p95 200 ms,
essentially all of it Python startup. On Hermes none of that applies. The
same assembly measures 1-2 ms in-process, which is what `popoto-memory
doctor` reports as `hook read`.

The tradeoff is that a bug in the plugin is a bug in the gateway process.
It is written accordingly: every callback catches everything, returns
`None` on any failure, and lets the reason land in `~/.popoto/memory.log`
and in `popoto-memory doctor`. Hermes also swallows callback exceptions a
second time at the invoke site, so nothing a broken plugin does can break a
user's turn -- and nothing it does surfaces there either. See
Troubleshooting below.

## Injection lands in the user message

Hermes places `pre_llm_call` context in the user message rather than the
system prompt, confirmed against the installed 0.19.0 source: `pre_llm_call`
results are joined into `plugin_user_context` (`agent/turn_context.py:708-741`)
and appended by `compose_user_api_content` (`agent/turn_context.py:44-73`),
whose docstring records that the injections reach "the *API copy* of the user
message only -- the stored content stays clean". That is the same placement
Claude Code's `additionalContext` gets, and it is why per-turn injection does
not invalidate a cached prefix.

`MemoryService` returns a context string and never touches a message array,
so this stays the harness's decision.

## Environment

- `POPOTO_MEMORY_AGENT_ID` -- **set this explicitly.** Plugin hook payloads
  carry no working directory at all (only the separate *shell*-hook
  subsystem serializes one), so without it popoto falls back to the Hermes
  gateway process's own `os.getcwd()` -- stable for the life of the
  gateway, but an arbitrary scope for a long-lived process serving multiple
  projects.
- `POPOTO_MEMORY_MAX_TOKENS` -- Hermes caps per-hook injected context at
  roughly 10,000 characters (~2,400 tokens) and spills any overflow to
  `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt`, substituting a
  head/tail preview plus that file path in the injected text. popoto's
  default of 800 tokens (~3,200 characters) has comfortable headroom;
  raising it much past ~2,400 tokens risks injecting a file path instead of
  memories, silently.

## Verify

```bash
popoto-memory doctor
```

After a few turns, `last assemble` and `last capture` carry recent
timestamps and `records` climbs.

If they stay at `never`, walk these in order -- Hermes swallows every
plugin error, so no single place shows all of it:

1. `hermes plugins list` -- the only place a **load** failure surfaces
   (usually: the enable step above was skipped).
2. `~/.hermes/logs/agent.log` -- the only place a **callback exception**
   surfaces.
3. `popoto-memory doctor` / `~/.popoto/memory.log` -- popoto's own
   failures (a misconfigured `POPOTO_MEMORY_URL`, an unreachable Redis).

You can also test the adapter directly, bypassing Hermes entirely:

```bash
echo '{"hook_event_name":"pre_llm_call","session_id":"t","user_message":"how do deploys work?"}' \
  | popoto-memory hook
```

If that returns `{"context": "..."}` but Hermes still shows nothing, the
problem is plugin registration or the payload field names, not the memory
layer.

## MCP tools

`hermes mcp add popoto-memory --command popoto-memory --args mcp` registers
`memory_search`, `memory_save`, `memory_feedback`, and `memory_status` for
deliberate use. Recall and capture do not depend on them.

## Verification status

Verified against the installed `hermes-agent==0.19.0` package (pinned
2026-09-08; re-check by 2027-03): the real
plugin loader (`hermes_cli.plugins.PluginManager`) loads this plugin from a
scratch `HERMES_HOME`, both hooks register against the real `invoke_hook`
dispatcher, and the payload shapes documented above were read verbatim from
the 0.19.0 invoke sites and captured through that dispatcher into
`tests/fixtures/harness_payloads/hermes_*.json`. **No live model turn has
been run** -- that needs provider credentials this repo's CI cannot hold.

## Configuration

Identical to every other harness, and all environment-driven. See
[Harness Integration](../features/harness-integration.md) for the table. The
gateway must have these in its environment, not just your interactive shell,
since the plugin runs in the gateway process.
