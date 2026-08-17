# Harness hook payload fixtures

Every file here carries a `_provenance` field beginning `captured-from:` that
states exactly where the payload came from and the command that reproduces it.
Read it before trusting a fixture: the four harnesses are not equally verified.

| Fixture | Source | Verified |
|---|---|---|
| `claude_code_user_prompt_submit.json` | live `claude` 2.1.220 headless run, 2026-08-07 | yes, live |
| `claude_code_stop.json` | same live run | yes, live |
| `codex_user_prompt_submit.json` | `codex-cli` 0.144.4 binary hook-input schema | binary, not a live turn |
| `codex_stop.json` | `codex-cli` 0.144.4 binary hook-input schema | binary, not a live turn |
| `hermes_pre_llm_call.json` | Nous Research Hermes hook docs | docs only |
| `hermes_post_llm_call.json` | Nous Research Hermes hook docs | docs only |
| `openclaw_before_prompt_build.json` | OpenClaw plugin hook docs | docs only |
| `openclaw_llm_output.json` | OpenClaw plugin hook docs | docs only |

A docs-derived fixture tests our reading of the documentation, not the harness.
The Claude Code pair is the one that tests the harness, and it is the reference
path for exactly that reason. Replacing the other six with live captures is part
of the maintainer's acceptance pass; each file records the command to do it.

The live Codex attempt is worth recording because it failed informatively: with
`.codex/hooks.json` in the project and
`codex exec --enable hooks --dangerously-bypass-hook-trust`, no hook ran and
Codex reported nothing. That is the silent project-level skip called out in
`docs/guides/harness-codex.md`, reproduced first-hand.

The `_provenance` key is deliberately part of the payload rather than a sidecar
file. Real harnesses send fields this integration does not read, so carrying an
extra key through the tests also proves the adapter tolerates them.
