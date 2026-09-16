# popoto-memory: Hermes wiring

This is a real Hermes **plugin** (`~/.hermes/plugins/`), not a gateway hook
(`~/.hermes/hooks/`) -- Hermes 0.19.0 has two independent hook systems, and
they are not interchangeable. If you followed an older version of this
README, remove the stale gateway install first:

```bash
rm -rf ~/.hermes/hooks/popoto-memory
```

## Install

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
mkdir -p ~/.hermes/plugins/popoto-memory
cp plugins/hermes/plugin.yaml plugins/hermes/__init__.py ~/.hermes/plugins/popoto-memory/
hermes plugins enable popoto-memory
hermes mcp add popoto-memory --command popoto-memory --args mcp
```

(Verified against `hermes mcp add --help` on the installed 0.19.0 CLI: it
takes `--command`/`--args` flags, not a bare `--` separator, which is how
this line read before this rewrite.)

**`hermes plugins enable popoto-memory` is not optional.** Plugins are
opt-in via `plugins.enabled` in `~/.hermes/config.yaml`; a plugin present on
disk but absent from that list is recorded as `enabled=False` and never
loads -- silently, with no startup warning. Confirm with:

```bash
hermes plugins list   # must show "popoto-memory  enabled", not
                       # "not enabled in config (run `hermes plugins enable ...`)"
```

Hermes hooks are Python, so the plugin runs in the gateway's own process:
the read path costs one Redis round trip with no interpreter startup at
all, which is the fastest of the four harnesses.

Injected context lands in the user message, never the system prompt --
confirmed against the installed 0.19.0 source. `pre_llm_call` results are
joined into `plugin_user_context` (`agent/turn_context.py:708-741`) and
appended by `compose_user_api_content` (`agent/turn_context.py:44-73`),
whose own docstring says the injections go on "the *API copy* of the user
message only -- the stored content stays clean". That is what keeps the
cached system prefix intact across turns.

## Environment

- `POPOTO_MEMORY_AGENT_ID` -- **set this explicitly.** Plugin hook payloads
  carry no working directory (only the separate *shell*-hook subsystem
  does), so without it popoto falls back to the Hermes gateway process's own
  `os.getcwd()` -- stable for the life of the gateway, but an arbitrary
  scope for a long-lived process serving multiple projects.
- `POPOTO_MEMORY_MAX_TOKENS` -- Hermes caps per-hook injected context at
  roughly 10,000 characters (~2,400 tokens) and spills any overflow to
  `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt`, substituting a
  head/tail preview plus that file path in the injected text. popoto's
  default of 800 tokens (~3,200 characters) has comfortable headroom; raising
  it much past ~2,400 tokens risks injecting a file path instead of memories,
  silently.

## Verification status

Verified against the installed `hermes-agent==0.19.0` package (pinned
2026-09-08; re-check by 2027-03): the real plugin loader
(`hermes_cli.plugins.PluginManager`) loads this plugin from a scratch
`HERMES_HOME`, both hooks register against the real `invoke_hook`
dispatcher, and the exact kwargs above were read verbatim from the 0.19.0
invoke sites. **No live model turn has been run** -- that needs provider
credentials this repo's CI cannot hold. `tests/fixtures/harness_payloads/`
records the same grade next to the fixtures it produced.

## Troubleshooting

Hermes swallows every plugin error (load failures and callback exceptions
alike), so diagnosis has to walk three places in this order:

1. `hermes plugins list` -- the only place a **load** failure surfaces (e.g.
   the opt-in step above was skipped).
2. `~/.hermes/logs/agent.log` -- the only place a **callback exception**
   surfaces.
3. `popoto-memory doctor` / `~/.popoto/memory.log` -- popoto's own failures
   (a misconfigured `POPOTO_MEMORY_URL`, an unreachable Redis).
