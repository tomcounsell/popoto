---
status: Planning
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/704
last_comment_id:
---

# Hermes: re-target the shipped plugin at the real hook system, and plumb `turn_id`

## Problem

A user follows `plugins/hermes/README.md` exactly: `pip install 'popoto[mcp]'`,
`popoto-memory doctor`, `mkdir -p ~/.hermes/hooks/popoto-memory`, copy two files,
start Hermes. Hermes prints

```
[hooks] Loaded hook 'popoto-memory' for events: ['pre_llm_call', 'post_llm_call']
```

…and popoto never runs. Not once, not degraded — zero events reach the handler for
the life of the process. The line that looks like confirmation is the whole defect:
it is emitted by a loader that does not validate event names, so a hook declaring
events from a different subsystem's vocabulary registers cleanly and then sits idle.

Hermes 0.19.0 has **two independent hook systems**:

| | plugin hooks | gateway hooks |
|---|---|---|
| location | `~/.hermes/plugins/<name>/` | `~/.hermes/hooks/<name>/` |
| manifest | `plugin.yaml` | `HOOK.yaml` |
| entry point | `register(ctx)` in `__init__.py` | `handle(event_type, context)` in `handler.py` |
| callback shape | flat keyword args, **sync** | one dict, `async def` |
| events | `pre_llm_call`, `post_llm_call`, `pre_tool_call`, … | `gateway:startup`, `session:*`, `agent:start|step|end`, `command:*` |
| gating | opt-in via `plugins.enabled` in `config.yaml` | none |

popoto ships the **gateway** file shape (`HOOK.yaml` + `handler.py` with
`async def handle`) while declaring **plugin** events. `pre_llm_call` is not in the
gateway vocabulary, so the wiring is a category error rather than a typo.

Three secondary defects ride along in the same integration, each of which would
still break recall even after the file shapes were corrected:

1. **Field mapping.** `src/popoto/integrations/hooks.py:170` reads an
   `extra`/`context`/`data` sub-object because "Hermes nests there". Plugin hooks
   pass **flat keyword args** — there is no sub-object. The write hook's text
   arrives as **`assistant_response`**, which is not in `_RESPONSE_FIELDS`
   (`hooks.py:88-98`), so a captured turn would normalize to empty text.
2. **`cwd`.** `hooks.py:285` documents deriving the default `agent_id` from the
   payload's `cwd`. Plugin hooks carry no working directory at all — only the
   separate *shell*-hook subsystem serializes one.
3. **`turn_id` (#688).** `hooks.py:120` asserts "Hermes sends none in the payloads
   popoto sees", which is why `service._push_pending` (`service.py:632-634`) drops
   Hermes onto the session-wide positional FIFO. That claim is **false**: Hermes
   mints a turn id once per turn and passes the *same* value to both
   `pre_llm_call` and `post_llm_call`.

Finally, the install instructions point at the wrong directory and omit that plugins
are **opt-in** — a plugin correctly installed but absent from `plugins.enabled` in
`~/.hermes/config.yaml` does not load. `~/.hermes/plugins/` appears nowhere in
popoto's docs.

**Current behavior:** the shipped Hermes integration cannot fire. Every test that
covers it — `test_hermes_response_shape`, the round-trip fixtures, the turn-id and
cwd expectation tables — passes against a fiction assembled from vendor
documentation. This is the OpenClaw failure (#552) recurring on the one harness
that was never live-checked, and `tests/fixtures/harness_payloads/README.md` had
already written down the warning that predicted it.

**Desired outcome:** `plugins/hermes/` is a real Hermes *plugin*; popoto's adapter
reads the field names Hermes actually sends; Hermes joins Claude Code, Codex and
OpenClaw on the turn-keyed outcome handoff instead of the FIFO fallback; the docs
describe an install that works; the fixtures record what the real dispatcher
delivered, graded honestly; and CI holds the manifest/entry-point contract against
the real loader so this cannot silently rot again.

## Freshness Check

**Baseline commit:** `0b2feed2a340e92923ca751e642ff4f848865465`
**Issue filed at:** 2026-09-08T05:07:42Z (#704); #688's resolving comment
2026-09-08T05:07:17Z
**Disposition:** **Unchanged**

**File:line references re-verified** (all read at the baseline commit):

- `src/popoto/integrations/hooks.py:120` — "Hermes sends none in the payloads popoto
  sees, so this is `None` there and the service falls back to its session-wide FIFO
  (see #688)" — **still present, still false.**
- `src/popoto/integrations/hooks.py:170` — "searching one level into an
  `extra`/`context`/`data` sub-object (Hermes nests there)" — **still present.**
  Note the function searches the **flat payload first** (`sources = [payload]`,
  `hooks.py:171`), so flat kwargs already work for `user_message` and `turn_id`;
  only the comment and the missing `assistant_response` name are wrong.
- `src/popoto/integrations/hooks.py:285` — the `cwd`-derived service default —
  **still present.** Reached only when `service is None`; the Hermes handler passes
  a prebuilt service, so this is a docs defect on that path rather than a crash.
- `src/popoto/integrations/hooks.py:88-98` `_RESPONSE_FIELDS` — **`assistant_response`
  is absent**, confirmed.
- `src/popoto/integrations/hooks.py:266` `if event.event in ("pre_llm_call",)` →
  `{"context": context}` — **already the correct plugin-hook return shape.**
- `plugins/hermes/HOOK.yaml`, `plugins/hermes/handler.py` — **unchanged gateway
  shape**, `async def handle(event_type, context)`.
- `src/popoto/integrations/service.py:632-634` — "Harnesses that send no turn id
  (Hermes) … keep the positional pairing" — **still present.**
- Hermes-side references from the #688 capture (`agent/turn_context.py:370`,
  `:692-703`; `agent/turn_finalizer.py:483-494`; `gateway/hooks.py:145-146`) —
  **re-verified against a freshly installed `hermes-agent==0.19.0`** in spike-1;
  line numbers hold.

**Cited sibling issues/PRs re-checked:**

- **#688** — open. Its single comment (2026-09-08) is the live capture that produced
  #704. This lane closes the plumbing half of #688; see No-Gos for what #688 keeps.
- **#552** — the OpenClaw equivalent. Its implementation merged as **PR #696 on
  2026-09-07 11:22Z**, i.e. *before* #704 was filed. It is the direct template for
  this work: plugin directory under `plugins/<harness>/`, `turn_id` forwarded from
  wherever the harness actually keeps it, fixtures captured through the shipped
  plugin, and a `POPOTO_HOOK_CAPTURE` tee to reproduce them.
- **#574 / PR #628** — closed/merged 2026-09-05. Built the turn-keyed pending-turn
  handoff. **The mechanism is generic and needs no change here** — `_TURN_FIELDS`
  already probes `turn_id`, so plumbing Hermes is a matter of the plugin emitting
  the kwarg, not of new handoff code.
- **#515 / PR #546** — closed/merged 2026-08-17. Shipped the original four-harness
  integration, including the defective Hermes wiring.

**Commits on main since the issue was filed (touching referenced files):** none.
`git log --since=2026-09-08T05:07:42Z -- src/popoto/integrations plugins/hermes
tests/fixtures/harness_payloads tests/test_integrations_hooks.py` is empty. The
most recent relevant commit is `3ff7f471` (PR #696, OpenClaw, 2026-09-07 18:22
+0700), which predates the issue and is the premise it was written on.

**Active plans in `docs/plans/` overlapping this area:**
`openclaw_subconscious_verification.md` (shipped as #696) is adjacent but complete —
it is precedent, not contention. No open lane touches
`src/popoto/integrations/` or `plugins/`. Open PRs on the repo: none.

**Bug reproduction:** re-confirmed at the code-reading level and, for the Hermes
half, by execution in spike-1 — a freshly installed `hermes-agent==0.19.0`'s
`PluginManager` requires `plugin.yaml` + `__init__.py` + `register()`, none of which
`plugins/hermes/` provides, so the directory is not loadable as a plugin at all;
and `gateway/hooks.py`'s event vocabulary contains neither `pre_llm_call` nor
`post_llm_call`, so the gateway path registers and never fires. Both halves of the
defect are present at the baseline commit.

## Prior Art

- **#515 / PR #546** — *Harness integration: add SubconsciousMemory to Claude Code,
  Codex, Hermes, and OpenClaw agents* (merged 2026-08-17). Shipped
  `src/popoto/integrations/` and all four `plugins/` directories. Claude Code was
  live-verified; **Hermes and Codex were written from vendor documentation.** This
  is the PR that introduced the defect, and it introduced it *knowingly* — the
  fixtures it committed carry `_provenance` strings that say so.
- **#574 / PR #628** — *Key the harness pending-turn handoff on turn id* (merged
  2026-09-05). Replaced session-wide positional pairing with a turn-tagged pending
  list (`{"t": turn_id, "k": keys}`), gated on `POPOTO_MEMORY_TURN_KEYED`, with an
  `LTRIM` cap and TTL. Succeeded, and left an explicit named exception for Hermes
  that this lane removes.
- **#552 / PR #696** — *OpenClaw: subconscious memory, verified by live capture*
  (merged 2026-09-07). The closest analogue: same class of defect (payload-only
  reading missed `ctx.runId`), same remedy (live capture, fixture replacement,
  README re-grade, `turn_id` plumbed). It succeeded and is the pattern to copy.
- **#511** — *Reposition the docs site around agent memory* (closed 2026-08-07).
  Only relevant because it is where the four-harness marketing claims in
  `docs/index.md` / `README.md` originate; those claims need no change (the harness
  is still supported), but the guide behind them does.

No prior attempt has been made to fix the Hermes wiring specifically. This is a
first fix, not a repeat.

## Research

**Queries used:**

- `hermes-agent NousResearch plugin.yaml register(ctx) register_hook pre_llm_call plugins.enabled`
- (source-of-truth reading substituted for further searching — see spike-1: the
  installed 0.19.0 package was read directly, which supersedes the docs for every
  question the docs and the code disagree on, and they disagree on several)

**Key findings:**

- **The public docs are wrong in at least two ways that matter, which is itself the
  most important finding.** The [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
  and [PR #1544](https://github.com/NousResearch/hermes-agent/pull/1544) describe a
  `provides:` block (`tools: true`, `hooks: true`) in `plugin.yaml`; the installed
  0.19.0 parser reads `provides_hooks` (a list) and ignores `provides:` entirely.
  The permissions docs name `plugins.entries`; the *loader gate* is
  `plugins.enabled` (a list). **Build against spike-1's readings of the installed
  package, never against the website.** This is the same failure mode that produced
  the bug: popoto's Hermes wiring was written from this documentation.
- **`pre_llm_call` return values are capped and spilled.** Hermes caps per-hook
  injected context at ~10,000 characters, writing the overflow to
  `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt` and substituting a head/tail
  preview plus the file path
  ([DeepWiki](https://deepwiki.com/NousResearch/hermes-agent/10.7-plugins-and-memory-providers),
  confirmed in-source as `tools/hook_output_spill.py` via `_spill_if_oversized`).
  popoto's `DEFAULT_MAX_TOKENS = 800` (`integrations/config.py:68`) is roughly 3,200
  characters, comfortably clear — but an operator who raises
  `POPOTO_MEMORY_MAX_TOKENS` past ~2,400 tokens on Hermes would start injecting a
  *file path* instead of memories, silently. This belongs in the guide as a named
  ceiling.
- **Injection is user-turn, inject-only, by explicit upstream design** — Hermes
  appends the returned context to the user message and never rewrites the message
  list, specifically to preserve the prompt-cache prefix
  ([open FR #23739](https://github.com/NousResearch/hermes-agent/issues/23739) asks
  to relax this and has not been granted). popoto's existing claim that Hermes
  injection preserves caching is therefore **correct** and survives the rewrite —
  it is the one Hermes claim in the docs that does not need changing.
- **The "hooks declared but never invoked" history is real but resolved.**
  [Issue #2817](https://github.com/NousResearch/hermes-agent/issues/2817) reported
  `pre_llm_call`/`post_llm_call` as documented-but-never-called; fixed in v0.5.0.
  0.19.0 has live invoke sites (spike-1 quotes them). Worth recording so a future
  reader who finds #2817 does not conclude the plugin path is dead.

Sources: [Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins),
[hermes-agent#2817](https://github.com/NousResearch/hermes-agent/issues/2817),
[hermes-agent#23739](https://github.com/NousResearch/hermes-agent/issues/23739),
[hermes-agent PR #1544](https://github.com/NousResearch/hermes-agent/pull/1544),
[DeepWiki: Plugins and Memory Providers](https://deepwiki.com/NousResearch/hermes-agent/10.7-plugins-and-memory-providers).

## Spike Results

_(skeleton)_

## Data Flow

_(skeleton)_

## Why Previous Fixes Failed

_(skeleton)_

## Architectural Impact

_(skeleton)_

## Appetite

_(skeleton)_

## Prerequisites

_(skeleton)_

## Solution

_(skeleton)_

## Failure Path Test Strategy

_(skeleton)_

## Test Impact

_(skeleton)_

## Rabbit Holes

_(skeleton)_

## Risks

_(skeleton)_

## Race Conditions

_(skeleton)_

## No-Gos (Out of Scope)

_(skeleton)_

## Update System

_(skeleton)_

## Agent Integration

_(skeleton)_

## Documentation

_(skeleton)_

## Success Criteria

_(skeleton)_

## Team Orchestration

_(skeleton)_

## Step by Step Tasks

_(skeleton)_

## Verification

_(skeleton)_

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

_(skeleton)_
