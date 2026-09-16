# Harness hook payload fixtures

Every file here carries a `_provenance` field beginning `captured-from:` that
states exactly where the payload came from and the command that reproduces it.
Read it before trusting a fixture: the four harnesses are not equally verified.
The grading below has three tiers, and the distance between them matters more
than the labels do:

- **live turn** -- a real model turn ran and the harness sent this payload.
- **real harness, no model** -- the harness's own loader/dispatcher (or binary
  schema) produced this payload, but no model was called. The field names are
  the harness's, not ours; what is unproven is only what a model turn adds.
- **docs only** -- nobody executed anything. The fixture tests our reading of
  someone's documentation. This tier is where #704 came from.

| Fixture | Source | Verified |
|---|---|---|
| `claude_code_user_prompt_submit.json` | live `claude` 2.1.220 headless run, 2026-08-07 | yes, live |
| `claude_code_stop.json` | same live run | yes, live |
| `codex_user_prompt_submit.json` | `codex-cli` 0.144.4 binary hook-input schema | binary, not a live turn |
| `codex_stop.json` | `codex-cli` 0.144.4 binary hook-input schema | binary, not a live turn |
| `hermes_pre_llm_call.json` | real `hermes-agent` 0.19.0 plugin loader and `invoke_hook` dispatcher, 2026-09-08 | real harness, no model |
| `hermes_post_llm_call.json` | same capture run | real harness, no model |
| `openclaw_before_prompt_build.json` | live OpenClaw 2026.9.2 turn through the shipped plugin, 2026-09-07 | yes, live |
| `openclaw_llm_output.json` | same live run | yes, live |

A docs-derived fixture tests our reading of the documentation, not the harness.
No pair is graded that way any more: Claude Code and OpenClaw ran live turns,
and Hermes and Codex were produced by the real harness without a model. The two
remaining for the maintainer's acceptance pass are therefore the Codex pair --
a live Codex turn -- plus, for Hermes, a live turn through a real gateway; each
file records the command to replace it.

The OpenClaw pair is worth reading as a warning about the other two. Its
predecessors round-tripped through `hooks.normalize()` perfectly well and were
still fiction: they named `message` and `text` where OpenClaw sends `prompt` and
`assistantTexts`, and they put `session_id` and `cwd` on the event when OpenClaw
puts them on a second `ctx` argument. Round-tripping proves the adapter is
self-consistent, not that anything sends what the fixture claims. Note also that
these fixtures are the envelope popoto's *plugin* emits rather than OpenClaw's
raw event -- a raw OpenClaw event carries no event-name field at all and could
never be normalized.

The live Codex attempt is worth recording because it failed informatively: with
`.codex/hooks.json` in the project and
`codex exec --enable hooks --dangerously-bypass-hook-trust`, no hook ran and
Codex reported nothing. That is the silent project-level skip called out in
`docs/guides/harness-codex.md`, reproduced first-hand.

The `_provenance` key is deliberately part of the payload rather than a sidecar
file. Real harnesses send fields this integration does not read, so carrying an
extra key through the tests also proves the adapter tolerates them.
