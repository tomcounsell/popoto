---
status: Ready
revision_applied: true
revision_applied_at: 2026-09-07
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
2. **Plugin**: builds `{hook_event_name: "llm_output", assistantTexts:
   event.assistantTexts, session_id: ctx.sessionId, cwd: ctx.workspaceDir,
   turn_id: ctx.runId}` — the array passed through unflattened.
3. **Plugin**: same shell-out, fire-and-forget (`llm_output` is an Observe hook;
   its return value is ignored).
4. **Adapter**: `llm_output` ∈ `WRITE_EVENTS` → kind `"write"`; `assistantTexts`
   is the first hit in `_RESPONSE_FIELDS`, and `_first_string`'s new list branch
   reduces it to a single string.
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

### Key Elements

- **`plugins/openclaw/popoto-memory-plugin/`** — a shippable OpenClaw plugin
  (package.json, `openclaw.plugin.json` manifest, one ESM entry file) that
  registers `before_prompt_build` and `llm_output`, translates each
  `(event, ctx)` pair into popoto's stdin envelope, shells out to
  `popoto-memory hook`, and returns the parsed stdout on the read hook.
- **Captured fixtures** — `tests/fixtures/harness_payloads/openclaw_*.json`
  replaced with the exact envelopes the shipped plugin emitted during a live
  turn, with a `_provenance` string that says so and names the OpenClaw version.
- **Two factual corrections** — the `turn_id` docstring in
  `src/popoto/integrations/hooks.py` and the `TURN_IDS` table in
  `tests/test_integrations_hooks.py`, both of which currently assert OpenClaw
  sends no per-turn identifier.
- **Operator documentation** — the three gates from spike-6, in
  `docs/guides/harness-openclaw.md`, plus the capability-table move in
  `docs/features/harness-integration.md`.

### Flow

Install popoto memory → `openclaw plugins install … --accept-capabilities` →
`openclaw config set plugins.entries.popoto-memory.hooks.allowConversationAccess
true` → restart the gateway → every subsequent turn recalls and captures with no
model participation.

### Technical Approach

**The fixtures are captured from the shipped plugin, not hand-corrected.** This
is the plan's one significant design decision, and it follows from spike-2. The
fixtures are consumed by `hooks.normalize()`, so they represent the plugin's
**stdin envelope**, not OpenClaw's raw event — which is why "the fixture field
names are wrong" is the wrong framing. A raw OpenClaw event has no event-name
field at all and could never be normalized. The correct sequence is therefore:
write the plugin first, run it against a live turn with a `POPOTO_HOOK_CAPTURE`
environment variable set that makes it tee its stdin to a file, and commit those
files verbatim. Any other order produces a second generation of hand-authored
fixtures, which is the defect being fixed.

**The array reduction happens in the adapter, not the plugin.** This was
reversed from an earlier draft of this plan, and the reasoning matters.

The tempting version is to have the plugin `join()` `assistantTexts` into a
string, because then every mapping already exists on the Python side —
`_QUERY_FIELDS` has `prompt`, `_RESPONSE_FIELDS` has `text`, `_TURN_FIELDS` has
`turn_id` — and `src/popoto/` gains nothing OpenClaw-shaped. That argument is
real but it loses on two counts. It puts the one non-trivial transformation in
this whole change into untested JavaScript (Risk 4's exposure, applied to the
part most likely to be wrong). And it makes the committed fixture carry an
already-flattened `text` field, so the fixture stops documenting what OpenClaw
actually sends and the mutation proof has nothing meaningful to corrupt — a
fixture test that passes against the old wrong shape is exactly the vacuous test
this repo keeps catching.

So: the plugin passes `assistantTexts` through **verbatim, as an array**, and
`src/popoto/integrations/hooks.py` learns to reduce it:

- `"assistantTexts"` joins `_RESPONSE_FIELDS`.
- `_first_string()` gains a list branch: a value that is a list of strings
  reduces to its non-empty members joined with `"\n"`, and anything else is
  skipped exactly as a non-string scalar is today.

The list branch is written as a **general** rule about payload values, not an
`if harness == "openclaw"`. `_first_string` is already the one place that
decides what counts as a usable value, and "a list of strings is the strings"
is the same kind of rule as its existing "a non-empty string is the string" and
its one-level `extra`/`context`/`data` descent. The anti-criterion in
Verification still holds: no harness *name* branches anywhere in `src/`.

Everything else the plugin needs already exists on the Python side, and
`render_context()` already emits `appendContext` for `before_prompt_build`.

**Fail silent, never fail the turn.** The handlers wrap everything. A failed
shell-out returns `undefined` from `before_prompt_build` (inject nothing) and
nothing from `llm_output`. This mirrors the adapter's own "always exit 0" rule
and OpenClaw's documented failure policy for these two hooks ("log and skip the
failed handler" / "log and continue"), so a memory outage degrades to no memory
rather than to a broken agent.

**Timeout.** `before_prompt_build` has a 15-second default per-handler budget,
and a timed-out handler is skipped but **not cancelled**. The plugin therefore
sets its own tighter `execFile` timeout so the subprocess cannot outlive the
budget that would ignore it.

## Failure Path Test Strategy

### Exception Handling Coverage

The Python side of this change is two comment/table corrections and adds no
exception handlers. The new handlers live in JavaScript, where the failure
paths are asserted by the plugin's own behavior rather than by pytest:

- Both handlers wrap the shell-out in `try`/`catch`. The catch path is exercised
  during the live capture session by pointing `POPOTO_MEMORY_BIN` at a
  nonexistent binary and confirming the turn still completes normally with no
  injection — recorded in the PR body as evidence, since it cannot be a
  committed pytest.
- No `except Exception: pass` blocks are introduced in Python.

### Empty/Invalid Input Handling

- `event.prompt` empty or whitespace-only: `_first_string()` already returns
  `""`, and `handle_payload` already emits nothing for empty context. A fixture
  round-trip test asserts the empty-prompt envelope normalizes to a read event
  with `text == ""`.
- `assistantTexts` empty array, or a list whose members are all empty or
  whitespace: `_first_string`'s list branch yields `""`, `_first_string` moves on
  to the next candidate field, and the write path stores nothing. Asserted in the
  adapter tests, together with the mixed case (a list containing one empty
  string and one real one reduces to the real one, with no stray newline).
- `assistantTexts` containing a non-string member: the list branch skips
  non-strings rather than raising, matching how the existing scalar path treats a
  non-string `prompt_id`.
- `ctx.runId` absent (a runtime that does not populate it): `_TURN_FIELDS`
  probing yields `None` and the service falls back to the session FIFO — the
  pre-existing behavior. Asserted with a fixture variant that omits `turn_id`.

### Error State Rendering

The user-visible output here is injected context. The failure rendering is
**silence** — the documented, deliberate behavior — and the assertion is that a
failed hook injects nothing rather than injecting an error string into the
model's prompt. Covered by the empty-context path already tested in
`test_integrations_hooks.py`.

## Test Impact

- [ ] `tests/test_integrations_hooks.py:61-62` (`TURN_IDS`) — UPDATE: the two
      OpenClaw entries change from `None` to the captured `runId`. The
      surrounding docstring, which names OpenClaw as a harness that sends no
      turn id, must change with them.
- [ ] `tests/test_integrations_hooks.py:67` (`SENDS_A_TURN_ID`) — UPDATE: add
      `"openclaw"`. This is the entry that makes the turn-keyed handoff tests
      actually exercise OpenClaw rather than skip it.
- [ ] `tests/test_integrations_hooks.py:230` (`test_openclaw_response_shape`) —
      UPDATE: unchanged in intent, but it loads the replaced fixture, so it must
      still pass against the captured envelope. It is the canary that the new
      fixture still normalizes.
- [ ] `tests/fixtures/harness_payloads/README.md` — UPDATE: the OpenClaw rows
      describing the fixtures as docs-derived.
- [ ] `tests/test_integrations_hooks.py` read/write fixture round-trip
      parametrizations — no change needed; they are name-driven and pick up the
      replaced files automatically. This is the reason the fixture swap is
      low-risk.
- [ ] `tests/test_integrations_hooks.py` — ADD: direct unit tests for
      `_first_string`'s new list branch (all-empty list, mixed list, list with a
      non-string member, list absent entirely). These do not go through a fixture
      and are the only place the reduction rule itself is pinned.

**Mutation proof.** Every new fixture-driven assertion must be shown non-vacuous
before the PR opens: corrupt one field name in the captured payload (e.g.
`assistantTexts` → `assistant_texts`), confirm the test **fails**, restore,
confirm it passes. The table goes in the PR body. A fixture test that still
passes against the old wrong shape is the specific failure this repo keeps
catching, and the fixture swap is exactly the change that invites it.

## Rabbit Holes

- **Reimplementing the memory path in TypeScript.** Explicitly rejected in
  `plugins/openclaw/README.md` "at any price," and that judgment stands. The
  shell-out is now verified; there is no remaining argument for a second
  implementation of the core.
- **Publishing the plugin to npm or ClawHub.** The other three harnesses ship
  config fragments and a handler file for the operator to install. Matching that
  shape is the whole scope. A published package brings versioning, a release
  process, and a compatibility matrix against OpenClaw's own version — a project,
  not a task.
- **Generalizing `_first_string` beyond a flat list of strings.** The list branch
  handles `list[str]`. Nested lists, dicts with a `text` key, or a full
  content-block walk (OpenClaw's `lastAssistant.content` is an array of typed
  blocks) all look like the next logical step and none are needed: the plugin
  sends `assistantTexts`, which the vendor already flattened to plain strings for
  exactly this purpose.
- **Chasing the other twenty-odd hooks OpenClaw exposes.** `before_tool_call`,
  `agent_end`, `before_compaction` and friends all look useful. The pre-turn read
  hook plus the post-turn write hook is the contract every other harness uses,
  and turn granularity is what the outcome handoff is paired against.
- **Making the test suite drive OpenClaw.** Capturing once and committing the
  payloads is the entire point; a suite that needs Node, an OpenClaw install and
  a model provider is a suite that does not run in CI.

## Risks

### Risk 1: The captured fixtures encode one OpenClaw version's shape

**Impact:** OpenClaw is on a calendar-version release train (2026.9.2 was
published two days before this plan). A field rename in a later release would
make the committed fixtures describe a contract that no longer exists — the same
defect class as the docs-derived fixtures, arriving more slowly.
**Mitigation:** the `_provenance` string names the exact version and the exact
recapture command, so the staleness is legible rather than invisible. The plugin,
not the fixture, is what would break, and the plugin reads named fields whose
absence yields empty strings rather than exceptions. Additionally, the fixtures
assert the **envelope popoto's plugin emits**, which popoto controls — an
upstream rename changes the plugin, not the envelope, so the fixtures stay valid
across the class of change most likely to occur.

### Risk 2: The three operator gates are undiscoverable, and two fail silently

**Impact:** a user follows the guide, installs the plugin, sees `status:
"loaded"`, and gets no memory — with no error anywhere. Support burden, and a
plausible bug report against popoto for an OpenClaw policy default.
**Mitigation:** documenting the gates is in scope, but documentation alone is
weak against a silent failure. The guide gets a copy-pasteable verification
command (`openclaw plugins inspect popoto-memory --runtime --json`) and states
what a *working* install looks like (`hookCount: 2`, empty `diagnostics`), so the
check is positive rather than "look for an error."

### Risk 3: Promoting the capability table on partial evidence

**Impact:** the exact failure the issue was filed to prevent. The capability
table deliberately separates capability from verification, and moving the
verification column on anything less than a live capture would repeat the
mistake #546 avoided.
**Mitigation:** the verification cell names the OpenClaw version and the date of
the capture, matching the Claude Code row's existing "captured from a live
`claude` 2.1.220 run" phrasing. If the live re-capture with the shipped plugin
fails for any reason, the capability row may move but the verification column
does **not** — and the plan stops and reports rather than shipping the
promotion. This is a hard gate, not a preference.

### Risk 4: The plugin is not exercised by CI

**Impact:** the JavaScript ships untested by the repo's own suite and can rot.
**Mitigation:** accepted, and bounded. The translation the plugin performs is
asserted from the other side — the committed envelopes are exactly what it
emitted, and the adapter tests prove those envelopes normalize correctly. What is
untested is the ten lines of glue between OpenClaw's arguments and that envelope.
`plugins/hermes/handler.py` carries the same exposure today. Adding a Node test
job is out of scope (see No-Gos) and would not have caught anything this plan
found.

## Race Conditions

### Race 1: `llm_output` capture overlapping the next turn's `before_prompt_build`

**Location:** the plugin's two handlers; `MemoryService`'s pending-turn handoff.
**Trigger:** `llm_output` is an **Observe** hook, and OpenClaw's documented
contract is that observation handlers "run concurrently" and the emitter "may
await completion or dispatch fire-and-forget," with the explicit warning that
"fire-and-forget events can overlap later events, and callbacks are not a durable
event queue." So turn N's capture can still be in flight when turn N+1's recall
begins.
**Data prerequisite:** the pending entry staged by turn N's read must be present
before turn N's write tries to pair with it. Within one turn this is ordered by
the harness (prompt build precedes model output), so it holds.
**State prerequisite:** turn N's outcome must not be paired against turn N+1's
pending entry.
**Mitigation:** this is precisely what `turn_id` buys, and it is why spike-5
matters beyond tidiness. With `turn_id: ctx.runId` on both hooks, the handoff is
keyed on the turn rather than popped off a session-wide FIFO, so an overlap
pairs correctly by construction. Had OpenClaw genuinely sent no turn id — as the
code currently claims — this race would be live and unmitigable from popoto's
side.

**No other race conditions identified.** The plugin's shell-out is a separate
process per event with no shared mutable state; the Python side is unchanged.

## No-Gos (Out of Scope)

- [EXTERNAL] Publishing the plugin to npm or ClawHub. Requires a registry
  account, a release process, and a human decision about a public package name
  and its compatibility promise. The other three harnesses ship installable
  source; this one matches them.
- [EXTERNAL] Verifying against OpenClaw's gateway/daemon deployment or any
  non-local channel (Telegram, Discord, Slack). Requires standing infrastructure
  and third-party accounts. The embedded local runner is the path popoto's docs
  teach, and it is what was verified.
- [SEPARATE-SLUG #688] Extending the turn-keyed handoff to Hermes. #574 shipped
  the mechanism and Hermes genuinely sends no turn id in the payloads popoto has;
  whether its `ctx` equivalent carries one is the same question this plan
  answered for OpenClaw, and it needs its own probe against a Hermes install.
  Filed as #688 rather than tagged against #574, which is closed — a tag pointing
  at a closed issue is the tracking-only promise the tag exists to reject.
- Reimplementing the memory path in TypeScript — rejected in
  `plugins/openclaw/README.md` and reaffirmed here now that the shell-out is
  verified. This is a permanent architectural boundary, not a deferral, so it
  carries no tag and gets an anti-criterion in Verification instead.
- A Node/CI test job for the plugin — same reasoning as Risk 4. This is a
  standing decision about test topology, not a deferred task.

## Update System

No update-system changes. Popoto is a library installed from PyPI; the OpenClaw
plugin is operator-installed source under `plugins/`, exactly like
`plugins/hermes/handler.py` and `plugins/claude-code/hooks/hooks.json`. Nothing
is deployed or propagated.

## Agent Integration

No new MCP surface. The `popoto-memory` executable and its `hook` subcommand
already exist and are already how the other three harnesses reach the memory
path; this plan adds a fourth caller of an unchanged entry point. The MCP tools
(`memory_search`, `memory_save`, `memory_feedback`, `memory_status`) stay exactly
as they are — the discretionary half is unaffected, and OpenClaw keeps it
alongside the new automatic half.

## Documentation

### Feature Documentation

- [ ] `docs/features/harness-integration.md` — move OpenClaw's capability row
      from `no, plugin required` to `yes (before_prompt_build)` /
      `yes (llm_output)`, and update Setup. **Separately**, move the verification
      row from `vendor documentation only` to a live-capture statement naming the
      OpenClaw version — these two tables are deliberately independent and the
      second only moves if the shipped plugin's own capture succeeds.

### External Documentation Site

- [ ] `docs/guides/harness-openclaw.md` — replace the instructed-memory lead,
      add the plugin install path, and add a troubleshooting section covering the
      three operator gates from spike-6 with a positive verification command.
- [ ] `mkdocs build --strict` passes (any new page must be reachable from the
      nav).

### Inline Documentation

- [ ] `src/popoto/integrations/hooks.py` — correct the `turn_id` docstring
      (currently: "Hermes and OpenClaw send neither"). Hermes still does not;
      OpenClaw does, via `ctx.runId`. State how it is obtained, since it comes
      from the hook's second argument rather than the event.
- [ ] `plugins/openclaw/README.md` — currently explains why the automatic half is
      missing. Rewrite: it is no longer missing, and the "should be reachable"
      caveat is resolved by a named, dated capture.
- [ ] `tests/fixtures/harness_payloads/README.md` — the OpenClaw provenance rows.
- [ ] The new plugin's entry file — comment the two non-obvious lines: the
      `assistantTexts` join, and why `turn_id` comes from `ctx` and not `event`.

## Success Criteria

- [ ] A live OpenClaw turn, driven by the **shipped** plugin (not a probe),
      injects popoto context and captures the turn.
- [ ] `tests/fixtures/harness_payloads/openclaw_before_prompt_build.json` and
      `openclaw_llm_output.json` are byte-for-byte what that plugin wrote to
      stdin, with `_provenance` naming OpenClaw 2026.9.2 and the recapture
      command.
- [ ] `TURN_IDS` and `SENDS_A_TURN_ID` in `tests/test_integrations_hooks.py`
      reflect that OpenClaw sends a per-turn id, and the turn-keyed handoff tests
      exercise it rather than skipping it.
- [ ] `src/popoto/integrations/hooks.py` no longer claims OpenClaw sends no turn
      id.
- [ ] The three operator gates are documented with a positive verification
      command.
- [ ] The capability table's verification column names a live capture — **and is
      left alone if the live capture with the shipped plugin does not succeed.**
- [ ] `_RESPONSE_FIELDS` carries `assistantTexts` and `_first_string()` reduces a
      list of strings, written as a general rule about payload values — so the
      plugin passes the array through verbatim and no `join()` happens in
      JavaScript.
- [ ] **Mutation proof** for each new fixture-driven test: corrupt one field name
      in the captured payload, confirm the test **fails**, restore, confirm it
      passes. The table goes in the PR body. A fixture test that would still pass
      against the old wrong shape is vacuous and does not count.
- [ ] No OpenClaw-specific branch is added to `src/popoto/`.
- [ ] Tests pass (`/do-test`), narrow scope: `tests/test_integrations_hooks.py`.
- [ ] Documentation updated (`/do-docs`).

## Step by Step Tasks

### 1. Write the OpenClaw plugin

- **Task ID**: build-plugin
- **Depends On**: none
- **Validates**: manual live run (no committed automated test — see Risk 4)
- **Informed By**: spike-2 (real event shapes), spike-3 (`ctx` carries
  `sessionId`/`workspaceDir`), spike-4 (subprocess allowed), spike-5 (`ctx.runId`)
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `plugins/openclaw/popoto-memory-plugin/` with `package.json`,
  `openclaw.plugin.json`, and `index.js` (ESM, no npm dependencies).
- Register `before_prompt_build` and `llm_output` via `definePluginEntry`.
- Build the stdin envelope from **both** handler arguments; pass
  `assistantTexts` through as an array (the adapter reduces it); carry `turn_id`
  from `ctx.runId`.
- Shell out to `popoto-memory hook` with an explicit timeout below OpenClaw's
  15-second handler budget.
- Wrap both handlers so any failure injects nothing and never fails the turn.
- Support `POPOTO_HOOK_CAPTURE` (tee stdin to a file) — this is what makes
  task 2 an honest capture rather than a transcription.

### 2. Capture fixtures from a live turn

- **Task ID**: capture-fixtures
- **Depends On**: build-plugin
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: false
- Install the shipped plugin the way the guide will tell a user to, including
  both operator gates.
- Run one turn via `openclaw agent --local` (**not** `agent exec` — spike-6
  gate 3).
- Commit the two teed envelopes verbatim, adding only `_provenance`.
- Exercise the failure path once (`POPOTO_MEMORY_BIN` pointed at a nonexistent
  binary), confirm the turn completes with no injection, and record the output
  for the PR body.

### 3. Adapter: list reduction and the turn-id correction

- **Task ID**: build-adapter
- **Depends On**: capture-fixtures
- **Validates**: `tests/test_integrations_hooks.py`
- **Informed By**: spike-2 (`assistantTexts` is an array), spike-5 (`ctx.runId`)
- **Assigned To**: adapter-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `"assistantTexts"` to `_RESPONSE_FIELDS` and the list branch to
  `_first_string()`, documented as a general value rule rather than a harness
  case.
- Add the four direct `_first_string` list-branch unit tests.
- Correct the `NormalizedEvent.turn_id` docstring in
  `src/popoto/integrations/hooks.py` (finding (c) is a **docstring correction
  plus plumbing only** — `ctx.runId` arrives as `turn_id` and the existing
  pairing machinery consumes it unchanged).
- Update `TURN_IDS`, its docstring, and `SENDS_A_TURN_ID` in the test.
- Add a fixture-variant test asserting an absent `turn_id` still falls back to
  the session FIFO.
- **Stop condition:** if plumbing `turn_id` turns out to change outcome-pairing
  *semantics* rather than just supplying an identifier the existing path already
  handles, stop and file an issue. Do not design new handoff behavior inside this
  PR.

### 4. Documentation

- **Task ID**: document-feature
- **Depends On**: build-adapter
- **Assigned To**: harness-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Rewrite `docs/guides/harness-openclaw.md` and `plugins/openclaw/README.md`.
- Move both rows in `docs/features/harness-integration.md`, treating the
  verification row as gated on task 2 succeeding.
- Update `tests/fixtures/harness_payloads/README.md`.

### 5. Final validation

- **Task ID**: validate-all
- **Depends On**: build-plugin, capture-fixtures, build-adapter, document-feature
- **Assigned To**: lane-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table.
- Confirm the verification column moved only on live-capture evidence.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Adapter tests pass | `POPOTO_TEST_DB=5 ./.venv/bin/python -m pytest tests/test_integrations_hooks.py -q` | exit code 0 |
| Lint clean | `./.venv/bin/python -m ruff check src/` | exit code 0 |
| Format clean | `./.venv/bin/python -m black --check src/ tests/` | exit code 0 |
| Docs build | `./.venv/bin/python -m mkdocs build --strict` | exit code 0 |
| Fixtures are live-captured | `grep -c "captured-from: the OpenClaw plugin hook reference" tests/fixtures/harness_payloads/openclaw_before_prompt_build.json tests/fixtures/harness_payloads/openclaw_llm_output.json` | match count == 0 |
| Fixtures name the version | `grep -l "2026.9.2" tests/fixtures/harness_payloads/openclaw_before_prompt_build.json tests/fixtures/harness_payloads/openclaw_llm_output.json \| wc -l` | output contains 2 |
| Adapter knows `assistantTexts` | `grep -c '"assistantTexts"' src/popoto/integrations/hooks.py` | output > 0 |
| Plugin does not flatten the array | `grep -c "join(" plugins/openclaw/popoto-memory-plugin/index.js` | match count == 0 |
| Turn-id claim corrected | `grep -c "Hermes and OpenClaw send neither" src/popoto/integrations/hooks.py` | match count == 0 |
| OpenClaw sends a turn id in tests | `grep -c "openclaw" <(sed -n '/^SENDS_A_TURN_ID/,/^"""/p' tests/test_integrations_hooks.py)` | output > 0 |
| Guide no longer says instructed-only | `grep -c "instructed memory, not subconscious memory" docs/guides/harness-openclaw.md` | match count == 0 |
| Capability table verification moved | `grep -c "^| OpenClaw | vendor documentation only |" docs/features/harness-integration.md` | match count == 0 |
| Operator gates documented | `grep -c "allowConversationAccess" docs/guides/harness-openclaw.md` | output > 0 |
| Anti-criterion: no TypeScript reimplementation of the memory path | `find plugins/openclaw -name '*.ts' -o -name '*.js' \| xargs grep -lc "redis\|zadd\|POPOTO" 2>/dev/null \| wc -l` | output contains 0 |
| Anti-criterion: no OpenClaw branch in src/ | `grep -rci "openclaw" src/popoto/ --include=*.py \| grep -v ':0$' \| grep -v "integrations/hooks.py"` | exit code 1 |

## Critique Results

**Round 1** — 2026-09-07, FULL depth, independent roster (3 critics: Risk &
Robustness, Scope & Value, History & Consistency). Verdict: **READY TO BUILD
(with concerns)** — 0 blockers, 4 concerns, 1 nit.

### C1 — `POPOTO_MEMORY_BIN` is depended on but never specified (CONCERN)

- **Critic**: Risk & Robustness (Skeptic)
- **Location**: Step by Step Tasks task 1, and Failure Path Test Strategy
- **Finding**: Both the failure-path strategy and task 2 exercise the fail-silent
  path by pointing `POPOTO_MEMORY_BIN` at a nonexistent binary, but task 1's list
  of what `index.js` must implement never requires reading that variable, and
  nothing in the existing tree establishes it as a convention.
- **Implementation note**: `const bin = process.env.POPOTO_MEMORY_BIN ||
  "popoto-memory"; execFile(bin, ["hook"], {timeout: <ms below 15000>}, cb)`.

### C2 — `event.prompt` is read from a single capture (CONCERN)

- **Critic**: Risk & Robustness (Adversary)
- **Location**: Data Flow, recall step 2
- **Finding**: The real `before_prompt_build` event carries `{prompt, messages}`
  and the plugin reads only `prompt`, observed on one turn. If a continuation or
  tool-result-driven turn leaves `prompt` empty while `messages` carries the
  content, `JSON.stringify` drops the key, no `_QUERY_FIELDS` name matches the
  plural `messages`, and recall silently emits nothing with no operator signal.
- **Implementation note**: `const prompt = event.prompt ||
  lastUserMessageText(event.messages) || "";` taking the final user-role entry's
  text before building the envelope.

### C3 — Reversibility is stated for the repo, not the operator (CONCERN)

- **Critic**: Risk & Robustness (Operator)
- **Location**: Architectural Impact (Reversibility), Documentation
- **Finding**: Deleting `plugins/openclaw/` restores the repo, not a machine that
  already installed the plugin and set `allowConversationAccess=true`. Combined
  with the deliberate fail-silent design and Risk 4's accepted absence of CI
  coverage, a regression degrades to silent no-memory, and no uninstall step is
  documented.
- **Implementation note**: extend the troubleshooting section's positive
  verification command (`openclaw plugins inspect popoto-memory --runtime
  --json`) with `openclaw plugins uninstall popoto-memory` as the rollback.

### C4 — Inline-doc task contradicts the adopted design (CONCERN)

- **Critic**: History & Consistency (Consistency Auditor)
- **Location**: Documentation > Inline Documentation
- **Finding**: The task says to comment "the `assistantTexts` join" in the plugin
  entry file, but the Technical Approach forbids a join there, and both a Success
  Criterion and a Verification anti-criterion (`grep -c "join("` == 0) enforce its
  absence. A builder following the doc task would add the very call the gate
  rejects. Leftover wording from the pre-revision design.
- **Implementation note**: replace with "passing `assistantTexts` through
  unjoined — the adapter, not the plugin, reduces it".

### N1 — Provenance verification row is negative-only (NIT)

- **Critic**: History & Consistency (Consistency Auditor)
- **Location**: Verification table, "Fixtures are live-captured"
- **Finding**: Asserting the old docs-derived sentence is gone does not assert the
  new `_provenance` documents a live capture; the existing provenance test only
  requires the `captured-from:` substring to exist at all, so any replacement
  text would pass.
- **Suggestion**: grep positively for a fixed phrase naming the shipped plugin,
  expecting a count of 2.

**Scope & Value returned `No findings.`**

## Open Questions

All three questions raised at plan time have been answered; they are kept here
with their answers rather than deleted, because each one constrains the build.

1. ~~**Does the verification column move on a capture from *this* machine?**~~
   **Answered: yes, but only for the shipped plugin.** OpenClaw moves
   instructed→subconscious only if the shipped plugin — not the probe — is
   live-verified end to end. If it cannot be, the table does not move and the PR
   says why. This is a hard gate on task 2, not a judgment call at review time.
2. ~~**Should `plugins/openclaw/openclaw.json.fragment` (MCP) stay as-is?**~~
   **Answered: yes.** The automatic and discretionary halves are complementary,
   not alternatives, and the other three harnesses ship both.
3. ~~**Is the Hermes turn-id question worth filing now?**~~ **Answered: filed as
   #688**, and the No-Gos tag now points there instead of at closed #574.
