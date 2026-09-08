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

### spike-1: Read the real `hermes-agent==0.19.0` plugin API off the installed package

- **Assumption**: "The issue's description of the plugin system (`plugin.yaml` with
  `provides_hooks`, `__init__.py` with `register(ctx)`, kwargs callbacks) is
  accurate enough to build against."
- **Method**: prototype — fresh venv, `pip install 'hermes-agent==0.19.0'` (exact
  version resolved, no fallback), read `hermes_cli/plugins.py`, `agent/turn_context.py`,
  `agent/turn_finalizer.py`, `hermes_cli/config.py` in site-packages.
- **Finding**: mostly accurate, with **four corrections that change the build**.

  1. **Callbacks must be synchronous.** `invoke_hook` calls `ret = cb(**kwargs)` and
     never awaits (`hermes_cli/plugins.py:1911-1927`). An `async def` callback
     returns a coroutine that is appended to the results list, filtered out by the
     consumer's `isinstance` check, and never awaited — a never-awaited-coroutine
     warning and no effect. popoto's current `async def handle` is wrong twice over.
  2. **`provides_hooks` is cosmetic.** It is parsed into the manifest dataclass
     (`plugins.py:1642`) and read by nothing that decides loading, registration, or
     gating — the only other reference in the tree is a docstring in
     `hermes_cli/web_server.py:916`. Registration happens exclusively through
     `ctx.register_hook(hook_name, callback)` at `plugins.py:1158-1173`.
     **No manifest key is required**; `name` defaults to the directory name
     (`plugins.py:1580`). Hermes's own bundled plugins declare a *different*,
     equally-unparsed key (`hooks:`).
  3. **The exact invoke-site kwargs**, quoted verbatim:

     | hook | kwargs |
     |---|---|
     | `pre_llm_call` (`agent/turn_context.py:692-703`) | `session_id`, `task_id`, **`turn_id`**, `user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`, `sender_id` |
     | `post_llm_call` (`agent/turn_finalizer.py:483-494`) | `session_id`, `task_id`, **`turn_id`**, `user_message`, **`assistant_response`**, `conversation_history`, `model`, `platform` |

     `telemetry_schema_version` is injected on **both** by the manager, not the call
     site (`plugins.py:1911`, `kwargs.setdefault`). **No `cwd` key exists on either**
     — a working directory is serialized only by the unrelated *shell*-hook
     subsystem (`agent/shell_hooks.py:541-550`). `post_llm_call` does not fire at
     all when the response is empty or the turn was interrupted.
  4. **Gating is real and is `plugins.enabled`.** `~/.hermes/config.yaml`
     (`hermes_cli/config.py:749-751`), shape `plugins: {enabled: [...], disabled: [...]}`,
     read at `plugins.py:256-270`. A plugin on disk but absent from the list is
     recorded as `enabled=False` with error `"not enabled in config (run
     `hermes plugins enable <key>` to activate)"` and never loaded
     (`plugins.py:1459-1470`). A missing `plugins` key means **nothing** standalone
     loads. `HERMES_SAFE_MODE=1` skips discovery entirely.

  Additionally confirmed:
  - **Return contract**: `{"context": "<str>"}` is exact, and a bare non-empty `str`
    is equally accepted; all plugins' pieces are joined with `"\n\n"` and appended to
    the **user** message (`agent/turn_context.py:720-741`). popoto's existing
    `render_context()` output for `pre_llm_call` is already correct. `post_llm_call`
    return values are **discarded** (`turn_finalizer.py:485`, statement position).
  - **Errors are swallowed twice** — per-callback in the manager
    (`plugins.py:1915-1926`, `logger.warning` only) and again at both invoke sites.
    Load failures are swallowed too (`plugins.py:1825-1830`). A broken popoto hook is
    invisible outside `~/.hermes/logs/agent.log` and `hermes plugins list`.
  - **Neither loader validates event names.** The plugin loader warns on an unknown
    `hook_name` and **still registers the callback** ("so forward-compatible plugins
    don't break", `plugins.py:1164-1172`); manifest-declared names are never checked
    at all. The issue's claim about the gateway loader generalizes to both.
- **Confidence**: **high** for everything read out of the installed package.
  **Medium** for behavior in a live agent turn: the invoke sites are present and
  reachable, but spike-1 did not run a real turn against a model provider.
- **Impact on plan**: sets the exact file contents for `plugins/hermes/`
  (`plugin.yaml` + `__init__.py`, sync callbacks taking `**kwargs`), removes the
  `assistant_response` and `cwd` guesswork from the adapter change, makes
  `plugins.enabled` a mandatory README step, and supplies the ground truth the new
  fixtures must match.

### spike-2: Is `hermes-agent` viable as a CI dependency for a contract test?

- **Assumption**: "A loader-level regression test against the real Hermes package is
  affordable in CI."
- **Method**: prototype — measured the install from spike-1's venv.
- **Finding**: **yes, with one sharp caveat.** 61 packages, 187 MB site-packages,
  wheels only, **no torch / transformers / numpy / nvidia**. Heaviest deps are
  `cryptography` and `Pillow`. `from hermes_cli.plugins import PluginManager,
  VALID_HOOKS, invoke_hook` succeeds in **0.54 s** with no API key set and a
  throwaway `HOME`; `invoke_hook("pre_llm_call", session_id="x")` returns `[]`
  cleanly.

  **The caveat is a namespace collision.** `hermes-agent` installs `hermes_cli`,
  `agent`, `gateway`, `tools`, `plugins`, `providers`, `cli.py` and
  `batch_runner.py` as **top-level modules** — there is no `hermes_agent` namespace
  package. popoto's repo root contains a `plugins/` directory, and this plan adds
  `plugins/hermes/__init__.py`, which makes `plugins` an importable package from the
  repo root. Installing `hermes-agent` into popoto's main dev/CI venv would make
  `import plugins` ambiguous depending on `sys.path` order.
- **Confidence**: high (measured), high (collision is structural, not speculative).
- **Impact on plan**: the contract test gets its **own workflow job with its own
  venv**, run from a temporary working directory outside the repo root, and is
  `importorskip`-guarded so the default suite skips it. `hermes-agent` is **not**
  added to `pyproject.toml`'s extras and **not** added to `uv.lock` — it never
  enters the developer install path, so `scripts/check_lock_imports.py` and the
  `lock-check` contract are untouched.

### spike-3: Does the existing turn-keyed handoff need any code change for Hermes?

- **Assumption**: "Plumbing `turn_id` for Hermes requires changes in
  `service.py`/`hooks.py` beyond documentation."
- **Method**: code-read of `hooks.py:82-98`, `hooks.py:167-181`, `hooks.py:221-246`,
  `service.py:610-677`, `service.py:738-790`.
- **Finding**: **no.** `_TURN_FIELDS` already contains `"turn_id"`;
  `_first_string()` searches the **flat payload first** before descending into
  `extra`/`context`/`data`/`input`. A flat `turn_id` kwarg is picked up by
  `normalize()` with zero code change, and `_push_pending`/`_pop_pending` are
  harness-agnostic. The FIFO fallback is not a Hermes branch — it is simply what
  happens when `turn_id` is `None`. **Retiring it for Hermes means supplying the
  value, then deleting the stale prose that says Hermes cannot.**
  Likewise `_QUERY_FIELDS` already contains `"user_message"`, so the read path's
  flat kwarg works untouched. The **only** adapter code change required is adding
  `"assistant_response"` to `_RESPONSE_FIELDS`.
- **Confidence**: high.
- **Impact on plan**: collapses the `src/` change to one tuple entry plus docstring
  corrections, and moves the weight of the work into `plugins/hermes/`, the fixtures,
  the tests, and the docs. It also raises a **vacuity hazard** the test plan must
  answer: an adapter test that "passes turn_id through" would pass today, before any
  fix. The falsifiable assertion has to be made at the plugin's envelope, not at
  `normalize()`.

### spike-4: Does an abandoned/interrupted Hermes turn leak a pending entry?

- **Assumption**: "`post_llm_call` not firing on an interrupted turn strands a
  turn-tagged pending entry forever."
- **Method**: code-read of `service.py:610-677` against spike-1's finding that
  `post_llm_call` is skipped when `final_response` is empty or the turn was
  interrupted.
- **Finding**: **no leak.** `_push_pending` writes through an
  `RPUSH`/`LTRIM -MAX_PENDING_TURNS`/`EXPIRE PENDING_TTL_SECONDS` pipeline, so an
  unclaimed entry is bounded both by list length and by TTL. This is exactly the
  case #574 designed for; turn-keying makes it *better* than the FIFO, because under
  positional pairing a skipped write shifts every later pairing by one.
- **Confidence**: high.
- **Impact on plan**: no new expiry/cleanup work. Worth one sentence in the guide —
  moving Hermes off the FIFO **fixes** an existing correctness bug for interrupted
  turns rather than merely tidying.

## Data Flow

**Read path (recall), after this change:**

1. **Entry point** — Hermes reaches `agent/turn_context.py:692`, calls
   `invoke_hook("pre_llm_call", session_id=…, task_id=…, turn_id=…, user_message=…,
   conversation_history=[…], is_first_turn=…, model=…, platform=…, sender_id=…)`.
   The plugin manager adds `telemetry_schema_version`.
2. **`plugins/hermes/__init__.py`** — the registered sync callback receives those as
   `**kwargs`, builds a JSON-able envelope: `{"hook_event_name": "pre_llm_call",
   "session_id": …, "turn_id": …, "user_message": …}` plus the remaining scalar
   kwargs, with `conversation_history` **dropped** (unbounded, unread, and it would
   make the committed fixture a transcript). Optionally tees the envelope to
   `$POPOTO_HOOK_CAPTURE/pre_llm_call.json`.
3. **`hooks.handle_payload(envelope, service=_service())`** — in-process, no
   subprocess. `normalize()` reads the event name, `session_id`, `turn_id`
   (flat, via `_TURN_FIELDS`) and the query text (flat, via `_QUERY_FIELDS`
   → `user_message`). `cwd` is `None`; the prebuilt service already has its
   `agent_id`, so nothing consults it.
4. **`MemoryService.assemble(text, session_id=…, turn_id=…)`** — Redis read,
   exclusion of already-injected keys, token-budgeted context block, and
   `_push_pending(session_id, records, turn_id=turn_id)` stages a **turn-tagged**
   entry `{"t": turn_id, "k": [...]}`.
5. **`render_context()`** → `{"context": "<block>"}` → `json.dumps` → the callback
   `json.loads`es it and returns the dict.
6. **Output** — `agent/turn_context.py:720-741` reads `r["context"]`, spills if over
   ~10k chars, joins with any other plugin's piece, and appends to the **user**
   message. The system prompt — and therefore the cache prefix — is untouched.

**Write path (capture + outcome), after this change:**

1. **Entry point** — `agent/turn_finalizer.py:483`, `invoke_hook("post_llm_call", …,
   turn_id=<same value>, assistant_response=<final text>, …)`. Skipped entirely if
   the response is empty or the turn was interrupted.
2. **`plugins/hermes/__init__.py`** — same envelope shape with
   `"hook_event_name": "post_llm_call"` and `assistant_response`.
3. **`hooks.handle_payload`** — `normalize()` selects the write branch;
   `_RESPONSE_FIELDS` must contain `assistant_response` for `event.text` to be
   non-empty. **This is the one line whose absence silently empties the whole write
   path.**
4. **`MemoryService.capture(text, session_id=…)`** then
   **`feedback(session_id, outcome="used", turn_id=turn_id)`** →
   `_pop_pending(session_id, turn_id=…)` claims *this turn's* tagged entry by value
   rather than popping the head of the FIFO.
5. **Output** — the callback returns `None`; Hermes discards write-hook returns
   anyway.

**The load path (why nothing fires today):** `PluginManager.discover_plugins` scans
`~/.hermes/plugins/*/plugin.yaml`, checks membership in `plugins.enabled`, imports
`__init__.py` as `hermes_plugins.<slug>`, and calls `register(ctx)`. popoto's
directory has no `plugin.yaml` and no `__init__.py`, so it is invisible to this path
entirely; installed at the *other* location it registers against a vocabulary that
is never emitted.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| #515 / PR #546 | Shipped the four-harness integration including `plugins/hermes/` | Wrote the Hermes contract from vendor documentation. The docs describe the plugin *events* but popoto's author matched them to the gateway *file shape*, and nothing executed either. The PR was honest about it — the fixtures say "docs only" — but honesty in a `_provenance` string is not a gate. |
| #574 / PR #628 | Turn-keyed the pending-turn handoff | Correct and complete for what it could see. It carved out Hermes on the strength of `hooks.py:120`'s docstring claim, inheriting #546's unverified reading rather than re-deriving it. A false premise propagated into a correct mechanism as a named exception. |
| #552 / PR #696 | Fixed the identical class of defect for OpenClaw | Did not fail — it succeeded, and its own No-Gos split Hermes out as #688 rather than fixing it in the same lane. The delay is deliberate scope control, not a failed fix. |

**Root cause pattern:** *a docs-derived integration with a self-consistent
round-trip test.* Every layer agreed with every other layer because they were all
derived from the same wrong source, and the test suite could only observe that
agreement. `tests/fixtures/harness_payloads/README.md` names this exactly —
"round-tripping proves the adapter is self-consistent, not that anything sends what
the fixture claims" — and the warning did not prevent the recurrence because it was
prose in a README rather than a check. The durable remedy is therefore not "capture
better fixtures" (that fixes today's instance) but **a CI check that executes
against the vendor's own loader**, which is what makes the claim falsifiable by
something other than a human re-reading the docs.

## Architectural Impact

- **New dependencies**: none in the shipped package. `hermes-agent==0.19.0` is added
  **only** as a CI job's pip install for the contract test — not to
  `[project.optional-dependencies]`, not to `uv.lock`, not to
  `scripts/check_lock_imports.py`. popoto's published dependency surface, the
  floor-propagation doctrine, and the `lock-check` contract are all untouched.
- **Interface changes**: `plugins/hermes/` changes shape entirely — `HOOK.yaml` and
  `handler.py` are **deleted**, replaced by `plugin.yaml` and `__init__.py`. This is
  a breaking change for anyone who installed the old files, and it breaks something
  that never worked, so there is no migration to preserve. In `src/`, the only
  behavioral change is one entry appended to `_RESPONSE_FIELDS`, which is purely
  additive and cannot alter any other harness's normalization (no existing fixture
  carries an `assistant_response` key).
- **Coupling**: **decreases.** The Hermes handler currently reaches into a nested
  `extra` sub-object that the adapter special-cases for it (`hooks.py:170`); after
  this change Hermes uses the same flat-payload path as every other harness, and the
  "Hermes nests there" justification for the nested search disappears. The nested
  search itself stays — `input`/`data` are still probed and removing it is a
  separate, unforced change — but its comment stops naming a harness that does not
  do it.
- **Data ownership**: unchanged. `MemoryService` still owns records and the pending
  list; the plugin owns only the envelope translation, exactly as OpenClaw's does.
- **Reversibility**: high. Everything is confined to `plugins/hermes/`, one tuple in
  `hooks.py`, two fixture files, docs, tests, and one new workflow. `git revert` of
  a single PR restores the prior (broken) state with no data migration — the pending
  list's tagged and untagged encodings already coexist by design (`_decode_pending_entry`).

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (scope confirmation on the CI-dependency question), code
reviewer

**Interactions:**
- PM check-ins: 1-2 — one on whether a `hermes-agent` CI job is acceptable, one on
  the honest fixture grade (see Open Questions).
- Review rounds: 1-2 — the plan-critique round plus PR review.

The coding is small and well-bounded; spike-1 removed essentially all of the
technical unknown. The cost is in breadth — six file groups (`plugins/`, `src/`,
fixtures, three test files, five docs, one workflow) — and in getting the *claims*
right, which is what failed last time.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 9 PING` | The suite's integration tests; this lane uses DB 9 (`POPOTO_TEST_DB=9`) |
| Lane worktree present | `test -d /Users/valorengels/src/popoto/.worktrees/sdlc-704` | Isolated build checkout on `session/sdlc-704` |
| Full dev extras installed | `python -c "import numpy, sentence_transformers, mcp"` | Avoids the ~95-test silent deselection in a fresh worktree venv (`CLAUDE.md`, worktree gotcha 2) |
| Network access to PyPI | `pip download --no-deps -d /tmp/hermes-probe 'hermes-agent==0.19.0'` | The contract-test job and any fixture recapture need the real package |
| `hermes-agent` **not** in the main venv | `python -c "import hermes_cli" 2>&1 \| grep -q ModuleNotFoundError` | Guards the `plugins`/`agent`/`tools` top-level namespace collision from spike-2 |

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
