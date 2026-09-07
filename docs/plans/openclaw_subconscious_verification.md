---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/552
---

# OpenClaw: subconscious memory, verified by live capture

## Problem

A developer runs OpenClaw all day. Popoto ships `popoto-memory` MCP tools for it,
so the model *can* search and save memories — but only when it elects to. Every
other supported harness (Claude Code, Codex, Hermes) gets memory on every turn
whether the model thinks to ask or not. `docs/guides/harness-openclaw.md` leads
with the honest sentence: "on OpenClaw this is instructed memory, not
subconscious memory."

That sentence is a placeholder for a missing verification, not a design choice.
#515 (PR #546) could not resolve spike-6 because OpenClaw was not installable on
any machine this repo is developed on. The reasoning "plugins are Node modules so
`child_process` should work" was plausible and was deliberately not shipped as if
it were verified.

**Current behavior:** OpenClaw users get MCP-only memory. `plugins/openclaw/`
contains a config fragment and a README explaining what is missing. The two
OpenClaw fixtures in `tests/fixtures/harness_payloads/` are hand-written from
vendor documentation and carry a `_provenance` string saying so.

**Desired outcome:** OpenClaw gets the same per-turn recall and capture the other
three harnesses get, shipped behind a real plugin, with fixtures captured from a
live run and a capability table that says "verified by live capture" because it
was.

## Freshness Check

**Baseline commit:** `1be9942d` (origin/main at plan time)
**Issue filed at:** 2026-08-10T02:35:16Z
**Disposition:** Minor drift

**Claims re-verified:**

- "OpenClaw is not installable in the development environment" — **no longer
  true.** `openclaw` is on npm at **2026.9.2**, published 2026-09-05. Installed
  into a scratch directory and run: `OpenClaw 2026.9.2 (3928bad)`. This is the
  premise the whole issue rests on, and it has lifted. See Spike Results.
- "OpenClaw support is MCP-only" — still true. `plugins/openclaw/` contains only
  `openclaw.json.fragment` (MCP server registration) and a README.
- "the fixtures round-trip" — **true but misleading.** They round-trip through
  `hooks.normalize()` because they were written to; they do not correspond to
  anything OpenClaw emits. See Spike Results spike-2.
- "the remaining plugin is roughly a dozen lines" — **understated.** See
  spike-2 and spike-4.
- `docs/guides/harness-openclaw.md` still leads with the instructed-memory
  sentence. `docs/features/harness-integration.md` still carries
  `| OpenClaw | vendor documentation only |` in the verification table.

**Cited sibling issues/PRs re-checked:**

- #515 — closed 2026-08-17. Resolution: PR #546 shipped the four-harness
  integration with OpenClaw deliberately MCP-only.
- PR #546 — merged 2026-08-17 as `e220b2e1`.

**Commits on main since the issue was filed (touching referenced files):**

- `15735cfd` fix(#574): key the harness pending-turn handoff on turn id (#628)
  — **changed the landscape.** It introduced `NormalizedEvent.turn_id`,
  `_TURN_FIELDS`, and the turn-keyed outcome handoff, and it documented OpenClaw
  as a harness that sends no turn id and must fall back to the session-wide FIFO
  (`src/popoto/integrations/hooks.py:113`, and `TURN_IDS` in
  `tests/test_integrations_hooks.py:61-62`). The live capture shows that claim is
  **false** — `ctx.runId` is a real per-turn identifier. This is in scope.
- `3a793d68`, `337b3f01`, `16aa702e`, `edf71ad8`, `bc307d92` — touched the
  recipes/service layers, not the OpenClaw path. Irrelevant to this plan.

**Active plans in `docs/plans/` overlapping this area:**
`harness_integration.md` (the #515 plan, shipped — spike-6 is the open item this
plan closes) and `pending_turn_handoff_turn_id.md` (the #574 plan, shipped —
this plan corrects one factual claim it made about OpenClaw). Neither is active.

**Notes:** The branch `session/sdlc-552` was cut at `e39c5cfd` and must be
rebased onto `1be9942d` before building.

## Prior Art

- **#515 / PR #546** — "Harness integration: add SubconsciousMemory to Claude
  Code, Codex, Hermes, and OpenClaw agents." Shipped three harnesses
  subconsciously and OpenClaw MCP-only. Its spike-6 is the exact unresolved item
  this plan closes. It established the pattern this plan follows: one
  `popoto-memory hook` executable, one `normalize()` function, per-harness
  response shapes in `render_context()`.
- **#574 / PR #628** — "key the harness pending-turn handoff on turn id."
  Introduced `turn_id` and the turn-keyed handoff. Correct in mechanism; wrong in
  one documented fact about OpenClaw, which this plan fixes.
- **#645, #651, #659** — the `get_redis()` / PEP 562 work. Not overlapping, but
  they set the house rule this plan's new code follows: never import
  `POPOTO_REDIS_DB` by name.

No prior attempt to write the OpenClaw plugin exists. This is greenfield within
an established pattern, so there is no "Why Previous Fixes Failed" section.

## Research

No WebSearch was required: the authoritative source is the shipped package
itself, which is now installable. Everything in Spike Results was read out of
`node_modules/openclaw/dist/*.d.ts` and `node_modules/openclaw/docs/plugins/`,
or observed from a live run — primary sources, not search results.

One finding worth recording from the vendor docs
(`docs/plugins/hooks.md:134`): the plugin hook registration gates "are specific
registration gates, not a sandbox or a universal filter." That is consistent with
the observed subprocess spawn, but the observation is the evidence, not the
sentence.

## Spike Results

All spikes were executed on 2026-09-07 against OpenClaw 2026.9.2 installed from
npm into a scratch directory, driving one live
`anthropic/claude-haiku-4-5-20251001` turn.

### spike-1: Can OpenClaw be installed and run on this machine today?

- **Assumption**: "OpenClaw is not installable in the development environment"
  (the issue's blocking premise).
- **Method**: prototype.
- **Finding**: **False — it installs and runs.** `openclaw` is published on npm
  at 2026.9.2 (2026-09-05, MIT). `node node_modules/openclaw/openclaw.mjs
  --version` → `OpenClaw 2026.9.2 (3928bad)`. npm reported five packages with
  uncovered install scripts, including OpenClaw's own pre/postinstall; none were
  needed for the CLI to run.
- **Confidence**: high (executed).
- **Impact on plan**: unblocks the entire issue. Without this every other spike
  is moot.

### spike-2: What do the real hook events look like?

- **Assumption**: "the fixtures round-trip, so the remaining plugin is roughly a
  dozen lines" (issue body and `plugins/openclaw/README.md`).
- **Method**: code-read of the shipped `dist/plugin-entry-*.d.ts`, confirmed
  against a live capture.
- **Finding**: the committed fixtures do not correspond to anything OpenClaw
  emits. Captured live:

  | | Committed fixture claims | OpenClaw actually sends |
  |---|---|---|
  | `before_prompt_build` | `{event, session_id, cwd, message}` | `{prompt, messages}` |
  | `llm_output` | `{event, session_id, cwd, text}` | `{runId, sessionId, provider, model, contextTokenBudget, contextWindowSource, resolvedRef, harnessId, assistantTexts: string[], lastAssistant, usage}` |

  Three specific divergences matter: the assistant text is an **array**
  (`assistantTexts`) not a string, and `hooks._RESPONSE_FIELDS` has no
  `assistantTexts` entry; there is **no event-name field at all** on either
  event, so `hooks.normalize()` — which dispatches on
  `hook_event_name`/`event` — cannot classify a raw OpenClaw event; and
  `session_id`/`cwd` are not on the event (see spike-3).

  The fixtures "round-trip" only because they were authored to satisfy
  `normalize()`. They are a fabricated stdin envelope for a plugin that was never
  written.
- **Confidence**: high (executed).
- **Impact on plan**: this is the plan's central design decision. The fixtures
  are not "wrong shape" to be corrected field-by-field — they describe the
  **stdin envelope the plugin emits**, and the honest way to produce them is to
  ship the plugin, run it, and dump what it actually wrote. See Solution.

### spike-3: Where do `session_id` and `cwd` come from?

- **Assumption**: they are on the event (what the fixtures imply).
- **Method**: live capture of both hook arguments.
- **Finding**: they are on the **second `ctx` argument**, as `sessionId` and
  `workspaceDir`. Captured `ctx` keys on `before_prompt_build`: `runId`, `trace`,
  `agentId`, `sessionKey`, `sessionId`, `workspaceDir`, `activeProjectKeys`,
  `modelProviderId`, `modelId`, `trigger`, `channel`, `messageProvider`,
  `channelId`, `chatId`, `senderId`.
- **Confidence**: high (executed).
- **Impact on plan**: the plugin reads from both arguments, not one. `cwd` maps
  to `ctx.workspaceDir`, which is what the service uses to derive the default
  agent id.

### spike-4: Can a third-party plugin spawn a subprocess?

- **Assumption**: "plugins load as in-process Node modules, so `child_process`
  should be reachable" — the exact unverified claim the issue exists for.
- **Method**: prototype. A plugin installed the way a user installs one
  (`openclaw plugins install npm-pack:…`) called `execFileSync` from inside its
  `before_prompt_build` handler during a live turn.
- **Finding**: **yes.** The subprocess ran and returned normally. Stronger: the
  handler's `appendContext` return value was really injected — the model's own
  reasoning quoted the probe string back ("The 'popoto-probe: subprocess-ok'
  appears to be some kind of system status message"). That is end-to-end
  evidence of injection, not merely a well-formed return value. `llm_output`
  fired on the same turn with the assistant text.
- **Confidence**: high (executed).
- **Impact on plan**: the shell-out architecture is sound and the issue's
  blocking question is answered by execution. This is the evidence that licenses
  moving the capability table's verification column.

### spike-5: Does OpenClaw send a per-turn identifier?

- **Assumption**: "Hermes and OpenClaw send neither, so this is `None` for them"
  (`src/popoto/integrations/hooks.py:113`, shipped in #574/PR #628).
- **Method**: live capture, comparing both hooks of one turn.
- **Finding**: **false for OpenClaw.** `ctx.runId` was
  `6af8a338-5fa8-4586-ad9c-104b333d3d56` on `before_prompt_build` and the
  identical value on `llm_output` of the same turn. `llm_output`'s *event* also
  carries `runId`. This is exactly the property the turn-keyed handoff needs.
- **Confidence**: high (executed).
- **Impact on plan**: OpenClaw joins the turn-keyed outcome handoff instead of
  falling back to the session-wide FIFO. `_TURN_FIELDS` already contains
  `turn_id`, so the plugin emits `turn_id: ctx.runId` and no adapter change is
  needed for the mechanism — only the docstring and the test's `TURN_IDS` table
  need correcting. This was not in the issue's scope and is a real capability
  gain, not a cleanup.

### spike-6: What must an operator do that the docs do not currently say?

- **Assumption**: installing the plugin is enough.
- **Method**: prototype, by failing three times.
- **Finding**: three gates, each of which produces a *silent or misleading*
  failure:
  1. `openclaw plugins install` **refuses** without `--accept-capabilities`.
     Loud, at least.
  2. Both hooks are **blocked** for non-bundled plugins unless the operator sets
     `plugins.entries.<id>.hooks.allowConversationAccess=true`. The plugin still
     reports `status: "loaded"`, `enabled: true`, `activated: true` — with
     `hookCount: 0`. The reason appears **only** in
     `openclaw plugins inspect <id> --runtime --json` under `diagnostics`.
  3. `openclaw agent exec` **does not load external plugins at all** — only
     `openclaw agent --local` and the gateway do. A verification run through
     `agent exec` completes successfully, produces no captures, and looks exactly
     like "the plugin does not work."
- **Confidence**: high (executed; gate 3 cost a real false negative during the
  probe).
- **Impact on plan**: these three are the substance of the guide's new
  troubleshooting section. A user hitting gate 2 or 3 has no way to diagnose it
  from popoto's docs as they stand.

## Data Flow

The plugin is a translation layer between OpenClaw's two-argument in-process hook
contract and popoto's one-JSON-object-on-stdin executable contract. Nothing about
the Python side changes.

**Recall (per turn, before the model sees the prompt):**

1. **Entry point**: OpenClaw fires `before_prompt_build(event, ctx)` in-process.
2. **Plugin**: builds the stdin envelope —
   `{hook_event_name: "before_prompt_build", prompt: event.prompt,
   session_id: ctx.sessionId, cwd: ctx.workspaceDir, turn_id: ctx.runId}`.
3. **Plugin**: `execFile("popoto-memory", ["hook"])`, writing that JSON to stdin.
4. **Adapter** (`hooks.normalize`): `before_prompt_build` ∈ `READ_EVENTS` → kind
   `"read"`; `prompt` is the first hit in `_QUERY_FIELDS`; `turn_id` is the first
   hit in `_TURN_FIELDS`.
5. **Service**: assembles context, stages a pending-turn entry keyed on
   `turn_id`.
6. **Adapter** (`hooks.render_context`): `before_prompt_build` → `{appendContext:
   "…"}` — already implemented, no change.
7. **Plugin**: parses that stdout and returns it as the handler's result.
8. **Output**: OpenClaw appends the text to the user turn. Verified end to end in
   spike-4.

**Capture (per turn, after the model answers):**

1. **Entry point**: OpenClaw fires `llm_output(event, ctx)`.
2. **Plugin**: builds `{hook_event_name: "llm_output", text:
   event.assistantTexts.join("\n"), session_id: ctx.sessionId, cwd:
   ctx.workspaceDir, turn_id: ctx.runId}`. **The join is load-bearing** —
   `assistantTexts` is an array and `_RESPONSE_FIELDS` expects a string.
3. **Plugin**: same shell-out, fire-and-forget (`llm_output` is an Observe hook;
   its return value is ignored).
4. **Adapter**: `llm_output` ∈ `WRITE_EVENTS` → kind `"write"`; `text` is in
   `_RESPONSE_FIELDS`.
5. **Service**: pairs the outcome with the pending entry staged under the same
   `turn_id` in step 5 above.
6. **Output**: nothing on stdout; the hook stays silent.

## Architectural Impact

- **New dependencies**: none in Python. One new **Node** artifact under
  `plugins/openclaw/` — hand-written ESM with no npm dependencies beyond
  OpenClaw's own peer SDK import. It is shipped as source for an operator to
  install, exactly as `plugins/hermes/handler.py` is.
- **Interface changes**: none. `normalize()`, `render_context()` and the
  `popoto-memory hook` stdin contract are all unchanged. This plan is a new
  *client* of an existing interface plus two factual corrections.
- **Coupling**: unchanged. The plugin depends on popoto only through the
  `popoto-memory` executable — no Python import, no shared schema beyond the JSON
  envelope. Reimplementing the memory path in TypeScript stays out of scope at
  any price (see No-Gos).
- **Data ownership**: unchanged.
- **Reversibility**: high. Deleting `plugins/openclaw/` and reverting the docs
  restores the current state; nothing in `src/` gains an OpenClaw-specific branch.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1-2 (the fixture-provenance question and the turn-id scope
  addition were both raised and settled before planning)
- Review rounds: 1

The probe — historically the expensive, blocking part — is already done. What
remains is one small Node file, one honest fixture recapture, two factual
corrections, and documentation.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| OpenClaw installed | `node "$OPENCLAW_HOME/openclaw.mjs" --version` | Live capture and re-capture |
| Node 22.22.3+ | `node --version` | OpenClaw plugin runtime requirement |
| A model provider credential | `test -n "$ANTHROPIC_API_KEY"` | A live turn requires a real model call |
| Redis on localhost:6379 | `redis-cli -n 5 ping` | Adapter/service tests |

The live-capture requirement is why the capture is performed once, by this plan,
and the captured payloads are committed — so the test suite never needs OpenClaw
or a model provider to run.

## Solution

_placeholder_

## Failure Path Test Strategy

_placeholder_

## Test Impact

_placeholder_

## Rabbit Holes

_placeholder_

## Risks

_placeholder_

## Race Conditions

_placeholder_

## No-Gos (Out of Scope)

_placeholder_

## Update System

_placeholder_

## Agent Integration

_placeholder_

## Documentation

_placeholder_

## Success Criteria

_placeholder_

## Step by Step Tasks

_placeholder_

## Verification

_placeholder_

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

## Open Questions

_placeholder_
