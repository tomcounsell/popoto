---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/704
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-08T05:51:00Z
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

### Key Elements

- **`plugins/hermes/plugin.yaml`** — a real Hermes plugin manifest. Declares
  `name: popoto-memory`, `version`, `description`, `author`, and
  `provides_hooks: [pre_llm_call, post_llm_call]`. The last key is **documentation
  for the operator and for `hermes plugins list`, not a registration mechanism**
  (spike-1); a comment in the file must say so, or a later reader will "fix" the
  registration by editing the manifest.
- **`plugins/hermes/__init__.py`** — `def register(ctx)` calling
  `ctx.register_hook("pre_llm_call", _on_pre)` and
  `ctx.register_hook("post_llm_call", _on_post)`. Both callbacks are **`def`, not
  `async def`**, and take `**kwargs` only — Hermes injects
  `telemetry_schema_version` unconditionally, and any future kwarg would be a
  `TypeError` on a positional signature. Each builds the envelope, calls
  `hooks.handle_payload(envelope, service=_service())` in-process, and fails silent.
  Keeps the existing lazy `_service()` singleton verbatim.
- **`plugins/hermes/README.md`** — rewritten install: `~/.hermes/plugins/popoto-memory/`,
  copy `plugin.yaml` + `__init__.py`, then **`hermes plugins enable popoto-memory`**
  (or the `plugins.enabled` YAML edit), then `hermes mcp add`. Verification status
  re-graded against what was actually executed.
- **`src/popoto/integrations/hooks.py`** — add `"assistant_response"` to
  `_RESPONSE_FIELDS`; correct three docstring claims (`:120` turn id, `:170` "Hermes
  nests there", `:285` `cwd`); leave `render_context`, `normalize`, and the nested
  search otherwise untouched.
- **`src/popoto/integrations/service.py`** — correct `_push_pending`'s docstring
  (`:632-634`): Hermes now sends a turn id; the FIFO fallback survives only for
  `POPOTO_MEMORY_TURN_KEYED=0` and for a harness that genuinely sends none.
- **Fixtures** — `hermes_pre_llm_call.json` / `hermes_post_llm_call.json` replaced
  with the envelope the *new* plugin emits under `POPOTO_HOOK_CAPTURE`, driven
  through the real `hermes_cli.plugins.invoke_hook` dispatcher, with a `_provenance`
  string that states precisely what was and was not executed. The row in
  `tests/fixtures/harness_payloads/README.md` is re-graded from "docs only" to that
  same precise claim — **not** to "yes, live", which would be a second fiction.
- **`.github/workflows/hermes-contract.yml`** — one job, its own venv, installs
  `hermes-agent==0.19.0` plus popoto, runs `tests/test_hermes_plugin_contract.py`
  from a temp working directory. This is the check that makes the integration
  falsifiable by machine.
- **`tests/test_hermes_plugin_contract.py`** — `importorskip("hermes_cli.plugins")`,
  so it is a silent skip in the normal suite and a hard gate in its own job.

### Flow

**Operator journey (what the rewritten README must make true):**

Fresh machine → `pip install 'popoto[mcp]'` → `popoto-memory doctor` (green) →
`mkdir -p ~/.hermes/plugins/popoto-memory && cp plugins/hermes/{plugin.yaml,__init__.py} ~/.hermes/plugins/popoto-memory/`
→ **`hermes plugins enable popoto-memory`** → `hermes plugins list` shows
`popoto-memory  enabled` (and *not* `not enabled in config`) → one turn →
`popoto-memory doctor` shows a `last assemble` timestamp → second turn recalls a
fact from the first.

**Failure affordance at each step**, because Hermes swallows every error
(spike-1(f)): `hermes plugins list` is the only place a load failure surfaces;
`~/.hermes/logs/agent.log` is the only place a callback exception surfaces;
`~/.popoto/memory.log` and `popoto-memory doctor` are where popoto's own failures
surface. The guide must name all three, in that order.

### Technical Approach

- **Delete, don't deprecate.** `HOOK.yaml` and `handler.py` are removed outright.
  Keeping them "for gateway users" would preserve a path that provably never fires
  and would leave two contradictory install stories in one directory. Also delete
  the stale `plugins/hermes/__pycache__/`.
- **The envelope is built from `**kwargs`, minus `conversation_history`.** Forward
  every scalar kwarg verbatim (`session_id`, `task_id`, `turn_id`, `user_message`,
  `assistant_response`, `model`, `platform`, `sender_id`, `is_first_turn`,
  `telemetry_schema_version`) plus a synthesized `hook_event_name` — `normalize()`
  needs an event name and the kwargs carry none. Drop `conversation_history`: it is
  unbounded, unread by the adapter, and committing it into a fixture would turn a
  contract document into a transcript. Record that omission in the fixture's
  `_provenance` so the fixture is not mistaken for the raw kwargs.
- **`hook_event_name`, not `event_type`.** `normalize()` accepts either
  (`hooks.py:201`), but `hook_event_name` is what the Claude Code, Codex and
  OpenClaw envelopes use; matching them means one spelling across all four plugins.
- **In-process, not subprocess.** Unlike OpenClaw's JS plugin, Hermes plugins are
  Python, so `handle_payload` is called directly — no `popoto-memory hook`
  shell-out, no interpreter startup. This is the existing design and the one genuine
  advantage the Hermes path has; keep it.
- **`agent_id` derivation must be addressed explicitly, not silently.** With no
  `cwd` in the payload and a prebuilt service, `MemoryConfig.from_env()` falls back
  to `os.getcwd()` of the **Hermes process**, which for a long-lived gateway is
  wherever the operator started it — stable but arbitrary. The remedy is
  documentation, not code: the guide must tell operators to set
  `POPOTO_MEMORY_AGENT_ID` explicitly for Hermes, and say why. Inventing a
  Hermes-specific `agent_id` heuristic is a rabbit hole (below). **Recommended, not
  required** (critique ruling 3): do **not** add a first-use `logger.warning` branch
  when `POPOTO_MEMORY_AGENT_ID` is unset — Hermes swallows plugin logging into
  `~/.hermes/logs/agent.log` where nobody reads it, so the warning adds a branch and
  buys nothing. Guide text next to the absent-`cwd` explanation, and stop. Do not
  raise either.
- **The contract test asserts the *shape*, against the real loader.** Concretely:
  point `HERMES_HOME` at a tmpdir, copy `plugins/hermes/` into
  `$HERMES_HOME/plugins/popoto-memory/`, write a `config.yaml` with
  `plugins: {enabled: [popoto-memory]}`, run `PluginManager.discover_plugins()`, and
  assert (a) the plugin loaded with no `error`, (b) `pre_llm_call` and
  `post_llm_call` each have a registered callback, (c) every registered hook name is
  in `VALID_HOOKS` — the assertion that would have caught the original defect, since
  the loaders only warn — and (d) with the `enabled` list emptied, the plugin does
  **not** load, which is what pins the opt-in step the README must teach.
  It also asserts (e) that `invoke_hook("pre_llm_call", **captured_kwargs)` returns
  a list whose first element is a `dict` with a `"context"` key when memory has
  something to inject, exercising the real dispatcher end to end minus the model.
- **Assertion (e) must be seeded, or it passes vacuously** (critique C1). A bare
  `isinstance(result[0], dict)` is not falsifiable: `handle_payload` does not guard
  `service.assemble()` (`hooks.py:305-313`), so with Redis unreachable the plugin's
  own mandated `except Exception: return None` swallows the error and `invoke_hook`
  returns `[None]` — the same observable as "nothing to inject". The contract job's
  **Redis service is therefore unconditional, not "if the test needs one"**, and the
  test builds a real `MemoryService` bound to it, `.capture()`s a known sentinel
  string, and then asserts
  `invoke_hook("pre_llm_call", **kwargs)[0]["context"]` **contains that sentinel**.
  If a reviewer judges a Redis service too heavy for this job, the only acceptable
  alternative is to split (e) into its own test whose skip is *visible* (an explicit
  `pytest.skip` with a reason, never a silent pass) and keep (a)–(d) as the
  Redis-free gate — assertions (a)–(d) require no Redis at all.
- **Pin `hermes-agent==0.19.0` in the job.** An unpinned install turns every
  upstream release into a popoto CI failure; a pinned one is a contract snapshot
  that must be bumped deliberately. Read it the way `CLAUDE.md` reads a green
  `lock-check`: it proves the manifest and entry point still satisfy *0.19.0's*
  loader, not that the integration is safe on whatever the user has installed.
  Say that in the workflow file — and say it **together with the advisory rule**
  (critique C2): *"green proves the manifest satisfies 0.19.0's loader; this job is
  advisory and must not gate merge."* The two sentences belong in one header comment
  because they are one argument.
- **Date the pin (critique C3).** No configured Dependabot lane can see this version:
  `.github/dependabot.yml` runs `uv` at `/` and `/examples` and `github-actions` at
  `/`, and none of the three reads a `pip install hermes-agent==0.19.0` inside a
  workflow `run` step. "Accept and document" with no owner and no cadence is the same
  silent rot this lane exists to close, one level up. The minimum required form is a
  dated comment on the install line —
  `# pinned 2026-09-08 (hermes-agent 0.19.0); re-check against latest by 2027-03` —
  plus one sentence in `docs/guides/harness-hermes.md` naming the pinned version and
  the date, so a reader can tell how stale the claim is without reading CI. A
  `schedule:`-triggered non-gating re-run against unpinned `hermes-agent` is the
  stronger option and is acceptable **only** if it cannot report failure on a PR.

## Failure Path Test Strategy

### Exception Handling Coverage

Blanket handlers in scope, each of which must have a test asserting observable
behavior:

- [ ] `plugins/hermes/__init__.py` — the `except Exception: return None` in each
      callback (inherited from `handler.py`'s current shape). Test: patch
      `hooks.handle_payload` to raise, assert the callback returns `None` and does
      **not** propagate, and assert the failure is recorded — either a line in
      `~/.popoto/memory.log` via `hooks._log_hook_error`, or, if the raise happens
      before that, a `logger.warning`. **A bare `except: return None` with no
      observable trace is not acceptable here**, precisely because Hermes swallows
      the exception a second time and a silent popoto plugin is indistinguishable
      from an absent one.
- [ ] `plugins/hermes/__init__.py::_service()` — deferred construction so a
      down Redis at gateway start does not block loading. Test: with
      `POPOTO_MEMORY_URL` pointing at a closed ephemeral port (the
      `_down_redis_url()` helper already in `tests/test_integrations_hooks.py`),
      assert `register(ctx)` still succeeds and both callbacks return `None`.
- [ ] `src/popoto/integrations/hooks.py` — `handle_payload`/`run` handlers are
      unchanged by this work and already covered; no new assertions needed.

### Empty/Invalid Input Handling

- [ ] `assistant_response=""` — spike-1 says Hermes skips `post_llm_call` entirely
      in that case, but the adapter must not depend on the vendor for that. Assert
      `normalize()` yields `kind="write"`, `text=""`, and that `handle_payload`
      does not capture an empty record. (`_reduce_value` already returns `""` for a
      whitespace-only string; this pins it for the new field name.)
- [ ] `turn_id=None` / `turn_id=""` — must fall back to the FIFO rather than staging
      an entry tagged with the empty string. Already guarded by
      `hooks.py:226` (`.strip() or None`); add a Hermes-shaped case so the guard is
      pinned against the *new* payload shape, not only the old.
- [ ] Callback invoked with **unexpected extra kwargs** — the real hazard from
      spike-1(c). Assert `_on_pre(**{... , "some_future_kwarg": 1})` does not raise.
      This is the test that would fail on a positional signature.
- [ ] `user_message` absent (a tool-result-driven turn) — assert the read path
      returns `None` rather than assembling against an empty query.

### Error State Rendering

- [ ] The user-visible output on this path is the injected context block. Assert
      that when `assemble()` returns empty or whitespace, the callback returns
      `None` and **not** `{"context": ""}` — spike-1 shows Hermes skips a falsy
      `context`, so both behave identically to the user, but emitting the empty key
      would be a silent contract drift the next reader could not distinguish from
      intent.
- [ ] Assert the injected block never reaches the system prompt: the existing
      `test_no_response_shape_touches_the_system_prompt` covers this for all read
      fixtures and must keep passing with the replaced Hermes fixture.

## Test Impact

- [ ] `tests/test_integrations_hooks.py:298-300 ::test_hermes_response_shape` —
      **UPDATE**: keep the `{"context": "x"}` assertion (spike-1 confirms it is
      correct for plugin hooks) but re-point it at the replaced fixture. Add a
      comment citing the invoke-site consumer (`agent/turn_context.py:720-741`) so
      the shape is traceable to executed code rather than to a doc.
- [ ] `tests/test_integrations_hooks.py:49-66 TURN_IDS` — **UPDATE**: the two Hermes
      entries change from `None` to the real captured turn id, **identical across
      the pair** (that identity is the assertion #688 is about). Rewrite the
      docstring's "Hermes sends none in the payloads popoto sees" sentence.
- [ ] `tests/test_integrations_hooks.py:68 SENDS_A_TURN_ID` — **DELETE**. Once all
      four harnesses send an id the `else` branch at `:183` and `:195` is dead, and
      a prefix tuple is a weaker assertion than the exact-value table beside it.
      Drive both branches off `TURN_IDS[name]` instead, which is already exact.
- [ ] `tests/test_integrations_hooks.py:71-90 CWDS` — **UPDATE**: both Hermes
      entries become `None`. Note this **weakens** `assert event.cwd == CWDS[name]`
      for those two rows from "exact value" to "absence", which the comment above
      the table currently claims never to do — so update that comment too, and add
      a dedicated assertion that the Hermes envelope contains **no `cwd` key at
      all** (not merely a null one), which is the falsifiable form.
- [ ] `tests/test_integrations_hooks.py:174-186 ::test_read_fixtures_normalize_to_the_prompt`
      and `:188-197 ::test_write_fixtures_normalize_to_the_assistant_message` —
      **UPDATE**: `assert "health checks" in event.text` / `"automatic rollback" in
      event.text` must survive, so the replaced fixtures have to carry the same
      probe strings. Have the capture harness send those exact strings as
      `user_message` / `assistant_response`; do not weaken the assertions.
- [ ] `tests/test_integrations_hooks.py` fixture-provenance test (`:~165-168`,
      `assert "captured-from:" in path.read_text()`) — **no change**, but the new
      `_provenance` strings must keep the prefix.
- [ ] `tests/test_integrations_service.py:451 ::test_untagged_harness_keeps_fifo_order`
      — **UPDATE (docstring only)**: "Hermes and OpenClaw send no turn id" is now
      false twice over — OpenClaw was corrected by #696 and this lane corrects
      Hermes. The *test* is still valuable (it covers `POPOTO_MEMORY_TURN_KEYED=0`
      and any future untagged harness); rename the claim to be about the untagged
      **encoding**, not about a harness roster.
- [ ] `tests/test_integrations_db0_isolation.py:223-233 ::test_the_hermes_handler_binds_too`
      — **REPLACE**: `sys.path.insert(...); import handler; handler._service()` cannot
      survive the rename. Rewrite to load `plugins/hermes/__init__.py` by path with
      `importlib.util.spec_from_file_location` under a synthetic module name —
      **not** `import plugins.hermes`, which would create a repo-root `plugins`
      package import and collide with `hermes-agent`'s top-level `plugins` module in
      any environment that has both (spike-2). Update the module docstring at `:17`.
- [ ] `tests/test_integrations_hooks.py` — **ADD**: a `_RESPONSE_FIELDS` case for
      `assistant_response` that is *falsifiable independent of the fixture* (a bare
      `{"hook_event_name": "post_llm_call", "assistant_response": "..."}` dict), so
      the one-line `src/` change has a test that fails without it.
- [ ] `tests/test_hermes_plugin_contract.py` — **NEW** (see Solution).
- [ ] `tests/fixtures/harness_payloads/README.md` — **UPDATE**: the two Hermes rows
      and the "the remaining four are still the maintainer's acceptance pass"
      sentence. **The corrected count is two, not three** (critique C4). That bucket
      holds four *files* — the Codex pair and the Hermes pair — and fixtures move in
      pairs, so it can only stay at four or drop to two. Rewrite the sentence as a
      unit with the new three-tier grading rather than patching the digit: after this
      lane the buckets are *live turn* (Claude Code, OpenClaw = 4 files), *real
      dispatcher, not a live turn* (Hermes = 2), *binary/docs, not a live turn*
      (Codex = 2). Shipping a wrong count in the one file whose job is honest
      bookkeeping would be this issue's own failure mode in miniature.

**Vacuity guard.** Per spike-3, `normalize()` already extracts a flat `turn_id`, so
any test written only at the adapter layer **passes before the fix** and proves
nothing. Every new turn-id assertion in this lane must run against the replaced
fixture or the plugin envelope, and the builder must demonstrate red-state: check
out the new tests against the old `plugins/hermes/` + old fixtures and show them
failing, before the implementation lands. This is the same discipline the
`CLAUDE.md` "#661 vacuity trap" note describes from the `POPOTO_REDIS_DB` sweep.

## Rabbit Holes

- **Reimplementing memory logic in the plugin.** Same standing boundary as
  OpenClaw's README states. The plugin is an envelope translator; anything that
  decides what a memory is stays in `src/popoto/`.
- **Inventing a Hermes-specific `agent_id` heuristic.** Plugin hooks carry no cwd,
  and it is tempting to derive a project scope from `task_id`, `platform`, or the
  gateway's config. Every such derivation is a new, untested scoping rule that would
  silently partition a user's memories. Document `POPOTO_MEMORY_AGENT_ID` and stop.
- **Supporting both Hermes hook systems.** A gateway-hook adapter for
  `agent:start`/`agent:end` looks like a cheap fallback and is not: #688's capture
  shows that context has no `turn_id`, truncates `message`/`response` to 500
  characters, and `agent:start` uses `emit()`, which discards return values — so
  injection is impossible there. Two half-working paths would be worse than one
  working one.
- **Chasing `transform_llm_output`, `pre_verify`, `on_session_*` and the other 20
  hooks in `VALID_HOOKS`.** The two-hook read/write contract is deliberate and
  shared across all four harnesses (`hooks.py:43-66` explains why per-tool injection
  is refused). Do not widen the surface here.
- **"Fixing" the nested `extra`/`context`/`data` search in `_first_string`.** Its
  Hermes justification evaporates, but `input`/`data` still serve other shapes and
  removing the branch is an unforced change to a function every harness crosses.
  Correct the comment; leave the code.
- **Running a live Hermes turn against a model provider.** It needs credentials and
  outward API calls, it cannot run in CI, and spike-1 already executes the loader
  and the dispatcher. Chasing the last increment of realism here would cost more
  than the whole rest of the lane. See No-Gos.
- **Adding `hermes-agent` to `pyproject.toml` extras or `uv.lock`.** Tempting for
  convenience; costs a 187 MB, 61-package install on every `uv sync --all-extras` in
  `lock-check.yml`, drags `hermes-agent` into the published extras surface, and
  imports the top-level `plugins`/`agent`/`tools` collision into the developer
  install path. The dedicated job exists to avoid exactly this.

## Risks

### Risk 1: The fixture grade is overstated, recreating the original defect one level up

**Impact:** The whole issue is that a `_provenance` string said "docs" and a README
row said "docs only" and nobody acted on it. Replacing them with "yes, live" when no
live agent turn was run would be strictly worse — a false green on the one artifact
whose job is to be honest about verification.
**Mitigation:** A distinct grade, stated in both files, naming exactly what
executed: *"real `hermes-agent` 0.19.0 plugin loader and `invoke_hook` dispatcher,
with kwargs taken verbatim from the 0.19.0 invoke sites; not a live model turn."*
The fixtures README already has a precedent column for partial grades
(`codex_*.json` → "binary, not a live turn"). Add a sentence to that README
distinguishing three levels — live turn / real dispatcher / docs — rather than two.
Critique ruling 2 confirms the three-level framing: Hermes's grade is backed by the
real `PluginManager` and `invoke_hook`, Codex's by a binary's schema and a *failed*
live attempt, and collapsing them onto one phrase would destroy a distinction the
project paid to establish. The count in that same sentence corrects from four to
**two**, not three — see critique C4 and Test Impact.

### Risk 2: `hermes-agent`'s top-level modules collide with popoto's repo layout

**Impact:** `hermes-agent` installs `plugins`, `agent`, `tools`, `gateway`,
`providers` as top-level modules. This plan adds `plugins/hermes/__init__.py`,
making `plugins` importable from popoto's repo root. In an environment with both,
`import plugins` resolves by `sys.path` order — and the contract test's job is
precisely to be such an environment.
**The invariant the collision actually turns on (critique C5):** `hermes-agent` ships
`plugins` as a **regular** package (it has an `__init__.py`); popoto's repo-root
`plugins/` has none, so it is only a PEP 420 namespace *portion* — and a namespace
portion never wins over a regular package found later on `sys.path`, whatever the
order. That asymmetry, not `sys.path` hygiene, is what makes the collision
survivable. It follows that `plugins/hermes/__init__.py` is **required** (Hermes
imports the plugin *directory* as a module) while **`plugins/__init__.py` must never
exist** — adding it is a plausible future "tidy-up", exactly the edit that
`plugins/hermes/__init__.py` invites, and it would silently flip popoto's `plugins`
into a regular package that shadows the vendor's inside the one job built to
exercise it. Pinned by the anti-criterion `test ! -e plugins/__init__.py` in
Verification and by a Success Criterion.

**Mitigation:** four layers. (a) The contract job runs `pytest` from a temp
directory with an explicit test path, never with the repo root leading `sys.path`.
(b) `tests/test_integrations_db0_isolation.py` loads the plugin by file path via
`importlib`, never by package name. (c) The main dev/CI venvs never install
`hermes-agent` — asserted as a Prerequisite check. (d) `plugins/__init__.py` never
exists, preserving the namespace-portion asymmetry above. If (a) proves fragile in
practice, the fallback is to run the contract assertions in a subprocess whose
`cwd` and `sys.path` are fully controlled, which is the shape
`tests/test_integrations_db0_isolation.py` already uses.

**Lint/format coverage of `plugins/` — decided, not deferred (critique C5, second
half).** Neither `ruff check src/` nor `black --check src/ tests/` reaches
`plugins/`, so `plugins/hermes/__init__.py` lands ungated by CI. **This lane accepts
that explicitly rather than widening `lint.yml`.** Widening is a cross-cutting change
— it edits `lint.yml`, contradicts `CLAUDE.md`'s Code Style paragraph which names
`black --check src/ tests/` verbatim, and pulls in the sibling
`plugins/openclaw/` JS tree that neither tool understands — and Task 4's no-touch
list keeps this PR out of `lint.yml` for good reason. Instead the lane gates the new
module **lane-locally**: two Verification rows (`black --check plugins/hermes/` and
`ruff check plugins/hermes/`, both exit 0) that the validator runs, so the file ships
formatted and lint-clean even though no CI job would have caught it. Widening the CI
gate to `plugins/` is a legitimate follow-up chore; it is not this PR.

### Risk 3: Pinning `hermes-agent==0.19.0` freezes the contract while users move

**Impact:** A pinned job cannot catch an upstream plugin-API change; a user on 0.20
could hit exactly the class of breakage this lane exists to prevent, with CI green.
**Mitigation:** Accept and document, matching how `CLAUDE.md` frames `lock-check`
("the lock installs and its packages import", never "the bump is safe"). The
workflow file must state that a green run proves the manifest satisfies *0.19.0's*
loader. Record the version and date in the fixture `_provenance` and the guide so a
future reader can tell how stale the claim is. Dependabot is not wired to this job
by design — a surprise `hermes-agent` bump landing as a red CI on an unrelated PR is
worse than a stale pin.

Two operational conclusions that Risk 3 previously left undrawn, both now mandatory:

- **The job is advisory and must never be a required status check (critique C2).**
  Branch protection is not configured in-repo — there is no `.github/settings.yml`
  and no CODEOWNERS — so whoever wires this workflow could reasonably mark it
  required, after which a PyPI outage or a yanked `hermes-agent` release blocks every
  unrelated PR on the repo. The rule goes in the workflow header comment next to the
  pin rationale *and* in Update System. Because it is a repo-settings action the PR
  diff cannot enforce, it is written down as a **manual post-merge note in the PR
  body**, not assumed.
- **The pin carries a dated staleness signal (critique C3).** No Dependabot lane
  reads a `pip install` inside a workflow `run` step, so the accepted risk gets a
  self-limiting mechanism rather than prose alone: a dated comment on the install
  line and the version+date named in `docs/guides/harness-hermes.md`. See Solution →
  Technical Approach for the exact wording.

### Risk 4: The `plugins.enabled` step is skipped by users and fails silently

**Impact:** The single most likely support outcome. Hermes records
`enabled=False` with a helpful error, but only in `hermes plugins list`; nothing
prints at startup and popoto's own `doctor` cannot see it, because popoto is never
loaded.
**Mitigation:** Make it a numbered, non-optional README step with the exact
`hermes plugins enable popoto-memory` command; make `hermes plugins list` the first
diagnostic in the guide's troubleshooting section, ahead of `popoto-memory doctor`;
and pin the behavior in the contract test's assertion (d) so the docs claim is
backed by an executed check rather than a reading.

### Risk 5: Deleting `HOOK.yaml`/`handler.py` breaks an existing installation

**Impact:** Anyone who followed the old README has files in
`~/.hermes/hooks/popoto-memory/`. After upgrading popoto they are not removed
automatically, and they will keep loading — and keep doing nothing.
**Mitigation:** Low severity, since the old path was inert. The README and the guide
get an explicit "if you followed the previous instructions, `rm -rf
~/.hermes/hooks/popoto-memory`" line, and the CHANGELOG entry says plainly that the
prior Hermes wiring never fired. Do not attempt programmatic cleanup — popoto has no
license to delete from a user's `~/.hermes`.

### Risk 6: `POPOTO_MEMORY_MAX_TOKENS` above Hermes's spill ceiling silently injects a file path

**Impact:** At the 800-token default there is ~3× headroom, but an operator tuning
recall upward crosses ~10,000 characters and Hermes substitutes a head/tail preview
plus a `hook_outputs/…txt` path. Memory appears to degrade for no visible reason.
**Mitigation:** Name the ceiling in the Hermes guide with the arithmetic, next to
the `POPOTO_MEMORY_MAX_TOKENS` row. No code change — popoto must not silently clamp
a value the operator set.

## Race Conditions

### Race 1: Overlapping turns pairing an outcome against the wrong turn's records

**Location:** `src/popoto/integrations/service.py:610-677` (`_push_pending`) and
`:738-790` (`_pop_pending`), reached from `hooks.handle_payload`
(`hooks.py:305-327`).
**Trigger:** Two Hermes turns in flight on one `session_id` — a gateway serving a
platform where a user sends a second message before the first finalizes, or a
subagent turn interleaving. Turn A's `pre_llm_call` pushes; turn B's `pre_llm_call`
pushes; turn B's `post_llm_call` fires first.
**Data prerequisite:** the pending entry staged by *this turn's* `assemble` must be
identifiable at `feedback` time.
**State prerequisite:** the same `turn_id` value must reach both hooks of one turn —
guaranteed structurally by Hermes, which mints it once at
`agent/turn_context.py:370` and passes the *same local* to both call sites.
**Mitigation:** this is the race the lane closes. Supplying `turn_id` moves Hermes
from positional FIFO pairing (correct only while turns do not overlap) to
claim-by-value. No new mechanism — #574's is already generic (spike-3).

### Race 2: A turn that assembles twice, or a redelivered `pre_llm_call`

**Location:** `service.py:648-666`.
**Trigger:** Two pushes for one `turn_id`.
**Data prerequisite:** one claimable entry per turn.
**State prerequisite:** none beyond the above.
**Mitigation:** already handled — `_has_pending_turn` is an advisory, deliberately
non-atomic check, documented in place as costing "one stale list element" rather
than a round trip per turn. Unchanged by this lane; noted so a reviewer does not
read it as newly introduced.

### Race 3: `_service()` built concurrently by two callbacks

**Location:** `plugins/hermes/__init__.py::_service()`.
**Trigger:** Hermes's plugin callbacks are synchronous (spike-1), but a gateway
serving multiple sessions may run turns on separate threads; two first-ever calls
could both see `_SERVICE is None`.
**Data prerequisite:** none — the loser's `MemoryService` is simply discarded.
**State prerequisite:** `MemoryService.__init__` must be idempotent with respect to
the Redis binding. It is: construction binds the configured database and, without an
explicit URL, never rebinds an existing connection
(`tests/test_integrations_db0_isolation.py::test_without_an_explicit_url_the_existing_connection_is_kept`).
**Mitigation:** none needed; do **not** add a lock. Record the reasoning as a
comment so a later reader does not "fix" it. The existing `handler.py` has the same
shape and the same non-problem.

### Race 4: An interrupted turn that never fires `post_llm_call`

**Location:** `agent/turn_finalizer.py:483` (guarded by `if final_response and not
interrupted`) against `service.py:_push_pending`.
**Trigger:** user aborts, empty model response.
**Data prerequisite:** the staged entry must not accumulate.
**State prerequisite:** bounded pending list.
**Mitigation:** already bounded by `LTRIM -MAX_PENDING_TURNS` and
`EXPIRE PENDING_TTL_SECONDS` (spike-4). Turn-keying strictly improves this case:
under positional pairing a skipped write shifted every later pairing by one, which
is #574's original defect.

## No-Gos (Out of Scope)

- **[EXTERNAL] A live end-to-end Hermes turn against a real model provider.** It
  needs provider credentials and outward API calls from a machine an agent cannot
  reach unattended, and it cannot run in CI at all. spike-1 executes the plugin
  loader and the `invoke_hook` dispatcher against the real 0.19.0 package and reads
  the invoke-site kwargs verbatim from the shipped source; what stays unexecuted is
  only that the agent loop reaches those call sites during a real turn. The fixture
  `_provenance` and the guide must both say so in those words rather than claiming
  a live capture.
- **[EXTERNAL] Re-grading the Codex fixture pair.** `codex_*.json` are graded
  "binary, not a live turn", and the fixtures README already records a first-hand
  failed attempt (`.codex/hooks.json` present, `codex exec --enable hooks
  --dangerously-bypass-hook-trust`, no hook ran). Closing it needs a machine with
  Codex installed and a human granting project-level hook trust interactively.
  Untouched here.
- **[EXTERNAL] Removing a user's stale `~/.hermes/hooks/popoto-memory/`.** popoto
  has no license to delete from a user's home directory; the old files are inert, so
  this is a documented manual step in the README and CHANGELOG, not code.

Everything else the issue lists — the plugin re-target, the field mapping, the
`turn_id` plumbing and FIFO retirement for Hermes, the README/guide rewrite, the
fixture replacement and re-grade, and the CI contract test — is **in scope for this
plan**.

## Update System

No update-system changes required in the deploy sense — popoto is a published
library plus an mkdocs site, and this lane adds no service, secret, or deploy step.

Two propagation facts do belong here:

- **`.github/workflows/hermes-contract.yml` is a new CI surface.** It installs
  `hermes-agent==0.19.0` in its own venv. It must **not** be added to
  `lock-check.yml`'s `uv sync --all-extras`, must **not** appear in
  `pyproject.toml`, and must **not** be listed in `scripts/check_lock_imports.py` —
  that script's package list is deliberately hand-maintained for popoto's *published
  extras*, and `hermes-agent` is not one. An anti-criterion in Verification pins
  this. **It is also advisory and must not be added to the repo's required status
  checks** (critique C2) — the pin can go stale or become transiently unfetchable,
  and a PyPI outage must not block every unrelated PR. Branch protection lives in
  repo settings, not in the diff, so the PR body must carry this as an explicit
  post-merge note to whoever administers the repo.
- **Existing installations need a manual migration**, stated in the CHANGELOG and
  both READMEs: remove `~/.hermes/hooks/popoto-memory/`, install to
  `~/.hermes/plugins/popoto-memory/`, and run `hermes plugins enable popoto-memory`.
  The CHANGELOG entry must say the previous wiring never fired — an upgrade note
  that implies a working feature got better would misdescribe the change.

## Agent Integration

This *is* the agent-integration lane: the deliverable is the surface through which
a Hermes agent reaches popoto's memory. Concretely:

- **Hook surface (this lane's subject):** `plugins/hermes/__init__.py` registers
  `pre_llm_call` and `post_llm_call` through `ctx.register_hook`, which is the
  subconscious half — recall and capture on every turn whether or not the model
  elects it. The contract test asserts both callbacks are actually registered
  against the real `PluginManager`, which is the "grep confirms X references Y"
  criterion in executable form.
- **MCP surface (unchanged):** `hermes mcp add popoto-memory -- popoto-memory mcp`
  exposes `memory_search` / `memory_save` / `memory_feedback` / `memory_status`.
  No change is planned, **but the command's spelling has never been verified against
  a real Hermes CLI** — it entered the docs from the same vendor-documentation
  reading that produced the bug. A build task verifies it against the installed
  0.19.0 CLI (`hermes mcp --help`) and corrects it if it has drifted.
- **No new tool names, no MCP schema change.** The four tool names are frozen
  (`integrations/mcp_server.py`); nothing here touches them.

## Documentation

### Feature Documentation

- [x] `docs/features/harness-integration.md` — six edits: the capability-matrix
      Hermes row `:53` (Setup cell "2-file hook directory" → the plugin install,
      and note the opt-in step), the verification-matrix Hermes row `:63`
      ("vendor documentation only" → the new grade), the turn-id section `:126-129`
      (delete the Hermes FIFO carve-out and the "its hooks are synchronous" reason,
      which is true but was not the reason), the read-path `(event, query_text,
      session_id, cwd)` normalization claim `:88-96` (Hermes carries no `cwd`),
      `render_context` shapes `:400-405` (unchanged, verify), and `:491`
      ("`plugins/` declarative harness assets" — no longer declarative for Hermes
      or OpenClaw).
- [ ] `docs/features/README.md:20` — index row: no content change needed; confirm.

### External Documentation Site

- [ ] `docs/guides/harness-hermes.md` — **near-total rewrite** (99 lines, the highest
      density of wrong claims in the repo). Replace: the "hooks run in the gateway
      process" framing `:3-5`/`:45-55`/`:96-99` (true of the *gateway* system, not
      the plugin system — plugin callbacks run in the agent process), the
      verification admonition `:7-14`, the install block `:25-27` and "Two files, no
      config file to edit" `:30`, the `HOOK.yaml` snippet `:32-39`, the
      `async def handle` description `:41-43`, and the manual-verification recipe
      `:76-86` (whose `echo '{"event_type":…,"extra":{"user_message":…}}'` asserts
      the nested shape that does not exist). **Keep and strengthen** the
      "injection lands in the user message" section `:57-65` — spike-1 and upstream
      FR #23739 both confirm it, and it is the one Hermes claim that survives.
      **Add**: the `plugins.enabled` opt-in step; the three-place failure-diagnosis
      order (`hermes plugins list` → `~/.hermes/logs/agent.log` → `popoto-memory
      doctor` / `~/.popoto/memory.log`); `POPOTO_MEMORY_AGENT_ID` guidance given the
      absent `cwd` (**recommended, not required** — critique ruling 3); the
      ~10,000-character spill ceiling next to `POPOTO_MEMORY_MAX_TOKENS`; the
      "remove your old `~/.hermes/hooks/` install" migration line; and **one sentence
      naming the CI-pinned `hermes-agent` version and the date it was pinned**
      (2026-09-08, 0.19.0) so a reader can judge how stale the verification claim is
      without reading CI (critique C3).
- [ ] `docs/features/prompt-cache-efficiency.md:48-54` — the `{"context": ...}` claim
      is correct; verify no gateway framing leaked in.
- [ ] `docs/features/never-record-firewall.md:33`, `docs/index.md:43-45`,
      `README.md:78,:126`, `examples/README.md:7`,
      `examples/harness_memory/README.md:76-77` — brand-list mentions only; no change
      expected. Confirm rather than assume.
- [ ] `mkdocs.yml:39` — nav entry already present; `mkdocs build --strict` must pass.
- [ ] `CHANGELOG.md` — new entry. Must state plainly that the shipped Hermes wiring
      never fired, name the migration, and record that Hermes now sends `turn_id`.
      Do **not** edit the historical 1.9.0 entry at `:95`; add a correcting entry
      instead.

### Inline Documentation

- [ ] `src/popoto/integrations/hooks.py` — `:120` (turn id), `:170` ("Hermes nests
      there"), `:253`/`:266` (verify the `context` shape claim, now backed by an
      executed reference), `:285` (`cwd`), and the module docstring `:13-15`.
- [ ] `src/popoto/integrations/service.py:632-634` — the `_push_pending` docstring's
      Hermes carve-out.
- [ ] `src/popoto/integrations/config.py:383` — the only `handler.py` reference in
      `src/popoto/integrations/` (verified at baseline: `grep -rn "handler.py"
      src/popoto/integrations/` returns that line and nothing else). Rename to the
      plugin's entry point.
- [ ] `src/popoto/integrations/config.py:129` — says "a Hermes handler" in prose;
      reword to the plugin callback.
- [ ] `src/popoto/integrations/__init__.py:11-12` — **confirm, no change expected**
      (critique N1). It names the event pair `pre_llm_call`/`post_llm_call`, which
      stays true after the re-target; it carries no `handler.py` reference. Do not
      edit a correct line.
- [ ] `plugins/hermes/README.md` — rewritten (see Solution).
- [ ] `tests/fixtures/harness_payloads/README.md` — Hermes rows re-graded; the
      two-level verified/not-verified framing widened to three levels (live turn /
      real dispatcher / docs); and the "remaining four" count corrected **to two**
      (critique C4 — four files move as two pairs, so four → two, never three),
      rewritten as one sentence with the new tiers so count and grade agree.

**Every rewritten claim must cite executed evidence** — a file:line in the installed
`hermes-agent==0.19.0` package, or the fixture that recorded it. A claim whose only
support is `hermes-agent.nousresearch.com` is the defect, not the fix, and the
Research section documents two places where that site is currently wrong.

## Success Criteria

- [ ] `plugins/hermes/` contains `plugin.yaml` + `__init__.py` + `README.md` and no
      `HOOK.yaml`, `handler.py`, or `__pycache__`.
- [ ] `plugins/hermes/__init__.py` defines `register(ctx)`, registers exactly
      `pre_llm_call` and `post_llm_call`, and contains **no** `async def`.
- [ ] The real `hermes_cli.plugins.PluginManager` loads the plugin from a scratch
      `HERMES_HOME` with `error is None`, both hooks registered, every registered
      name in `VALID_HOOKS`, and **does not** load it when `plugins.enabled` is
      empty.
- [ ] `hooks.normalize()` extracts non-empty `text` from a `post_llm_call` payload
      carrying `assistant_response`, proven by a test that fails on the current
      `_RESPONSE_FIELDS`.
- [ ] Hermes read and write fixtures carry the **same** `turn_id`, and
      `TURN_IDS[hermes_pre] == TURN_IDS[hermes_post] is not None`.
- [ ] `service._push_pending` stages a **tagged** entry (`{"t": …, "k": […]}`) for a
      Hermes-shaped payload, and `feedback` claims it by value — asserted on the
      encoding, not on a return count.
- [ ] No file in `src/` or `tests/` still claims Hermes sends no turn id.
- [ ] The Hermes rows in `tests/fixtures/harness_payloads/README.md` no longer read
      "docs only", and the replacement grade names what executed and what did not.
- [ ] `plugins/hermes/README.md` and `docs/guides/harness-hermes.md` both install to
      `~/.hermes/plugins/popoto-memory/` and both teach
      `hermes plugins enable popoto-memory`; neither contains
      `mkdir -p ~/.hermes/hooks`.
- [ ] `hermes-agent` appears in no published dependency surface — not
      `pyproject.toml`, not `uv.lock`, not `scripts/check_lock_imports.py`.
- [ ] **`plugins/__init__.py` does not exist** (critique C5) — popoto's `plugins/`
      stays a PEP 420 namespace portion so it can never shadow `hermes-agent`'s
      regular `plugins` package.
- [ ] The contract test's assertion (e) asserts on a **seeded sentinel's presence in
      the injected context**, not merely that a dict came back (critique C1), and the
      contract job declares a Redis service unconditionally. The *wiring* is checked,
      not the word (round-2 R2-C2): the file must contain a real `.capture(` call and
      an `assert … in …["context"]` containment assertion, since a comment, docstring
      or unused local named `sentinel` satisfies a bare `grep -ci 'sentinel'` while
      the corner-cut version of (e) survives.
- [ ] `.github/workflows/hermes-contract.yml` states in its header that the job is
      **advisory and must not be a required status check**, and its `hermes-agent`
      install line carries a **dated pin comment** (critique C2, C3). The PR body
      carries the matching post-merge note about branch protection.
- [ ] `tests/fixtures/harness_payloads/README.md` no longer contains the phrase
      "remaining four" **even across a line break** — the check is
      `tr '\n' ' ' < … | grep -cE 'remaining +four'` → 0, because the phrase is
      currently wrapped between lines 19-20 and a plain `grep -c 'remaining four'`
      returns 0 at baseline and therefore cannot detect its own violation
      (critique C4, round-2 R2-C1). Its replacement names **two** files in the
      not-yet-live-verified bucket, and the new middle tier label is positively
      asserted present (`grep -c 'real dispatcher' …` → output > 0), so the
      criterion is red before the edit and green only after it.
- [ ] `plugins/hermes/` is black-formatted and ruff-clean, verified lane-locally
      (`black --check plugins/hermes/`, `ruff check plugins/hermes/`) since no CI job
      covers `plugins/` and this lane deliberately does not widen `lint.yml`
      (critique C5, second half).
- [ ] Red-state proof recorded in the PR: the new tests, run against the pre-fix
      tree, fail — specifically the plugin-envelope turn-id assertion and the
      `assistant_response` assertion.
- [ ] Tests pass (`/do-test`), stating the environment and the DB (`POPOTO_TEST_DB=9`).
- [ ] Documentation updated (`/do-docs`); `mkdocs build --strict` green.
- [ ] `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py` all
      green.
- [ ] The PR body carries `Closes #704` **and** `Closes #688` outright, with no
      "partially addresses" hedge (critique ruling 4), plus the red-state proof and
      the C2 post-merge branch-protection note.
- [ ] No xfail conversions needed — `grep -rn 'pytest.mark.xfail\|pytest.xfail('
      tests/` returns nothing at the baseline commit, so there is no expected-failure
      marker documenting this bug.

## Team Orchestration

The lead agent coordinates and never builds directly. Work is in the lane worktree
`/Users/valorengels/src/popoto/.worktrees/sdlc-704` on branch `session/sdlc-704`,
with `POPOTO_TEST_DB=9` exported for every test run.

### Team Members

- **Builder (plugin)**
  - Name: `hermes-plugin-builder`
  - Role: `plugins/hermes/` — delete the gateway shape, write `plugin.yaml` and
    `__init__.py`, rewrite the plugin README.
  - Agent Type: `builder`
  - Domain: MCP-tool/API integration
  - Resume: true

- **Builder (adapter)**
  - Name: `adapter-builder`
  - Role: `src/popoto/integrations/` — `_RESPONSE_FIELDS`, and every docstring that
    asserts a false Hermes fact.
  - Agent Type: `builder`
  - Domain: Redis/Popoto data
  - Resume: true

- **Builder (fixtures + contract CI)**
  - Name: `contract-builder`
  - Role: capture the replacement fixtures through the real dispatcher, re-grade the
    fixtures README, write `tests/test_hermes_plugin_contract.py` and
    `.github/workflows/hermes-contract.yml`.
  - Agent Type: `test-engineer`
  - Resume: true

- **Builder (suite repair)**
  - Name: `suite-builder`
  - Role: the three existing test files in Test Impact, plus the red-state proof.
  - Agent Type: `test-engineer`
  - Resume: true

- **Documentarian**
  - Name: `hermes-documentarian`
  - Role: `docs/guides/harness-hermes.md` rewrite, `docs/features/harness-integration.md`
    matrix and turn-id edits, CHANGELOG, brand-list confirmations.
  - Agent Type: `documentarian`
  - Resume: true

- **Validator**
  - Name: `hermes-validator`
  - Role: verify every Success Criterion and run the Verification table.
  - Agent Type: `validator`
  - Resume: true

## Step by Step Tasks

### 1. Re-target the plugin directory

- **Task ID**: `build-plugin`
- **Depends On**: none
- **Validates**: `tests/test_hermes_plugin_contract.py` (create),
  `tests/test_integrations_db0_isolation.py`
- **Informed By**: spike-1 (sync callbacks; `register(ctx)`/`ctx.register_hook`;
  `provides_hooks` is cosmetic; exact kwargs; `{"context": …}` return; errors
  swallowed), spike-2 (namespace collision)
- **Assigned To**: `hermes-plugin-builder`
- **Agent Type**: `builder`
- **Parallel**: true
- Delete `plugins/hermes/HOOK.yaml`, `plugins/hermes/handler.py`, and
  `plugins/hermes/__pycache__/`.
- Add `plugins/hermes/plugin.yaml`: `name: popoto-memory`, `version`, `description`,
  `author`, `provides_hooks: [pre_llm_call, post_llm_call]` — with an inline comment
  stating that `provides_hooks` is parsed into the manifest but read by nothing that
  registers hooks (`hermes_cli/plugins.py:1642`), so registration lives in
  `__init__.py`.
- Add `plugins/hermes/__init__.py`: module docstring naming the install path and the
  `plugins.enabled` requirement; the existing lazy `_service()` singleton carried
  over verbatim, with a comment on why no lock (Race 3); `_envelope(event_name,
  kwargs)` dropping `conversation_history` and synthesizing `hook_event_name`;
  optional `POPOTO_HOOK_CAPTURE` tee mirroring
  `plugins/openclaw/popoto-memory-plugin/index.js`; **synchronous** `_on_pre` and
  `_on_post` taking `**kwargs` only; and `def register(ctx)` calling
  `ctx.register_hook` twice.
- The read callback returns `json.loads(output)` when `handle_payload` returns a
  string, and `None` otherwise — never `{"context": ""}`.
- Both callbacks wrap everything in `except Exception` and return `None`, but must
  leave an observable trace (`hooks._log_hook_error` or a `logger.warning`); a
  silent swallow is explicitly rejected by the Failure Path Test Strategy.
- Rewrite `plugins/hermes/README.md` per Solution → Flow, including the
  "remove your old `~/.hermes/hooks/popoto-memory/`" migration line.
- Verify `hermes mcp add popoto-memory -- popoto-memory mcp` against the installed
  0.19.0 CLI (`hermes mcp --help`) and correct the command if it has drifted.

### 2. Fix the adapter field mapping and retire the false claims

- **Task ID**: `build-adapter`
- **Depends On**: none
- **Validates**: `tests/test_integrations_hooks.py`, `tests/test_integrations_service.py`
- **Informed By**: spike-3 (only `_RESPONSE_FIELDS` needs a code change; `turn_id`
  and `user_message` already resolve flat), spike-1 (no `cwd` anywhere in plugin
  hooks)
- **Assigned To**: `adapter-builder`
- **Agent Type**: `builder`
- **Parallel**: true
- Add `"assistant_response"` to `_RESPONSE_FIELDS` (`hooks.py:88-98`), with a comment
  citing `agent/turn_finalizer.py:483-494`.
- Rewrite `hooks.py:120` (`NormalizedEvent.turn_id` docstring): Hermes **does** send
  `turn_id`, minted once per turn at `agent/turn_context.py:370` and passed to both
  hooks; the FIFO fallback now applies only to `POPOTO_MEMORY_TURN_KEYED=0`.
- Rewrite `hooks.py:170`: drop "(Hermes nests there)"; the nested search stays for
  `input`/`data`, and the code is **not** otherwise changed.
- Rewrite `hooks.py:285`: plugin-hook payloads carry no `cwd`; the Hermes path passes
  a prebuilt service, and operators should set `POPOTO_MEMORY_AGENT_ID`.
- Rewrite `hooks.py:13-15` (module docstring) and confirm `:253`/`:266` — the
  `{"context": …}` shape is correct; add the executed citation
  (`agent/turn_context.py:720-741`).
- Rewrite `service.py:632-634`'s Hermes carve-out.
- Rewrite `integrations/config.py:383` (the sole `handler.py` reference in the
  package) and `integrations/config.py:129` ("a Hermes handler" in prose).
  **Leave `integrations/__init__.py:11-12` alone** — critique N1 verified it carries
  no `handler.py` reference; it names the event pair, which stays true. Confirm and
  move on.
- **Do not** change `render_context`, `normalize`, `_first_string`, or any
  `service.py` behavior.

### 3. Capture replacement fixtures and re-grade

- **Task ID**: `build-fixtures`
- **Depends On**: `build-plugin`
- **Validates**: `tests/test_integrations_hooks.py`
- **Informed By**: spike-1 (invoke-site kwargs), spike-2 (`hermes-agent` installs
  cleanly, 0.54 s import, no API key)
- **Assigned To**: `contract-builder`
- **Agent Type**: `test-engineer`
- **Parallel**: false
- In a scratch venv **outside the repo** with `hermes-agent==0.19.0`, install the new
  plugin into a scratch `HERMES_HOME`, enable it in `config.yaml`, and drive
  `hermes_cli.plugins.invoke_hook("pre_llm_call", **kwargs)` /
  `("post_llm_call", **kwargs)` with the kwargs from spike-1's tables, using
  `POPOTO_HOOK_CAPTURE` to write the envelopes.
- The probe values must keep the assertions in
  `test_read_fixtures_normalize_to_the_prompt` and
  `test_write_fixtures_normalize_to_the_assistant_message` alive: `user_message`
  containing "health checks", `assistant_response` containing "automatic rollback".
  The `turn_id` must be **byte-identical across the pair** and in Hermes's own
  `<session>:<task>:<hex8>` shape.
- Replace `tests/fixtures/harness_payloads/hermes_pre_llm_call.json` and
  `hermes_post_llm_call.json`. `_provenance` must state: the exact package version
  and date; that the loader and `invoke_hook` dispatcher are real; that the kwargs
  were taken verbatim from the 0.19.0 invoke sites; that **no live model turn ran**;
  that `conversation_history` was dropped by popoto's plugin; and the exact command
  to reproduce.
- Update `tests/fixtures/harness_payloads/README.md`: re-grade both Hermes rows,
  widen the framing from two verification levels to three, and correct
  "the remaining four" **to two** (critique C4). Word the Hermes rows exactly:
  *"real `hermes-agent` 0.19.0 plugin loader and `invoke_hook` dispatcher; kwargs
  verbatim from the 0.19.0 invoke sites; not a live model turn."* Rewrite the
  count sentence together with the tier table — *live turn* (Claude Code, OpenClaw),
  *real dispatcher, not a live turn* (Hermes), *binary/docs, not a live turn*
  (Codex, 2 files) — so the number and the grade cannot drift apart. The
  Verification row pins that the old sentence is gone — and it must be the
  **newline-collapsing** form, `tr '\n' ' ' < … | grep -cE 'remaining +four'` → 0,
  because the phrase is line-wrapped at `README.md:19-20` and a plain
  `grep -c 'remaining four'` already returns 0 at baseline (round-2 R2-C1). Pair it
  with the positive companion row `grep -c 'real dispatcher' …` → output > 0 so the
  new tier label is asserted present, not merely the old wording absent.

### 4. Contract test and its CI job

- **Task ID**: `build-contract`
- **Depends On**: `build-plugin`
- **Validates**: `tests/test_hermes_plugin_contract.py` (create)
- **Informed By**: spike-1 (loader internals, `VALID_HOOKS`, gating), spike-2
  (namespace collision, install weight)
- **Assigned To**: `contract-builder`
- **Agent Type**: `test-engineer`
- **Parallel**: false
- `tests/test_hermes_plugin_contract.py`: `pytest.importorskip("hermes_cli.plugins")`
  at module scope; a fixture building a scratch `HERMES_HOME` with the plugin copied
  in and `config.yaml` written; assertions (a)–(e) from Solution → Technical
  Approach. Assertion (c) — every registered hook name is in `VALID_HOOKS` — is the
  one that would have caught the original defect and must be present.
- **Assertion (e) must be seeded** (critique C1): build a real `MemoryService` bound
  to the job's Redis, `.capture()` a sentinel string, then assert
  `invoke_hook("pre_llm_call", **kwargs)[0]["context"]` **contains that sentinel**.
  A bare `isinstance(result[0], dict)` passes on the swallowed-exception path and is
  rejected. If (e) is split out instead, its skip must be explicit and visible, never
  a silent pass; (a)–(d) need no Redis and stay the Redis-free gate.
- **The Verification rows for (e) check the wiring, not the word** (round-2 R2-C2).
  `grep -ci 'sentinel'` alone is satisfied by a comment, a docstring, or an unused
  local, so the corner-cut version of (e) would pass the row that exists to catch it.
  Two additional rows are required and are already in the Verification table:
  `grep -c '\.capture(' tests/test_hermes_plugin_contract.py` → output > 0 and
  `grep -cE 'assert .+ in .+\["context"\]' tests/test_hermes_plugin_contract.py` →
  output > 0. Both are red at baseline by construction (the file does not exist), so
  they need no separate red-state demonstration. The `sentinel` row stays — it is
  harmless, just insufficient alone.
- `.github/workflows/hermes-contract.yml`: one job, own venv, `pip install
  hermes-agent==0.19.0` plus `-e .[dev]`, **an unconditional Redis service** (C1 —
  the earlier "if the test needs one" hedge is resolved to *yes*), run `pytest`
  **from a temp working directory** with an absolute test path so the repo root does
  not lead `sys.path`. The header comment must state three things together: the
  pin's meaning (a green run proves the manifest and entry point satisfy *0.19.0's*
  loader, nothing more); that **this job is advisory and must not be added to
  required status checks** (C2); and the dated pin
  (`# pinned 2026-09-08 (hermes-agent 0.19.0); re-check against latest by 2027-03`,
  C3) on the install line.
- The PR body carries a **manual post-merge note** asking the repo administrator not
  to mark `hermes-contract` a required check — branch protection is repo settings, so
  the diff cannot enforce it (C2).
- Do **not** touch `pyproject.toml`, `uv.lock`, `scripts/check_lock_imports.py`,
  `lock-check.yml`, `tests.yml`, or `lint.yml`.

### 5. Repair the existing suite and prove red state

- **Task ID**: `build-suite`
- **Depends On**: `build-adapter`, `build-fixtures`
- **Validates**: `tests/test_integrations_hooks.py`,
  `tests/test_integrations_service.py`, `tests/test_integrations_db0_isolation.py`
- **Informed By**: spike-3 (the vacuity hazard), spike-4 (no leak to test for)
- **Assigned To**: `suite-builder`
- **Agent Type**: `test-engineer`
- **Parallel**: false
- Apply every disposition in Test Impact, including deleting `SENDS_A_TURN_ID` and
  driving both branches off `TURN_IDS`.
- Add the fixture-independent `assistant_response` normalization test and the Hermes
  envelope tests from Failure Path Test Strategy (extra kwargs tolerated; empty
  `assistant_response`; blank `turn_id`; absent `user_message`; down-Redis
  `register()`).
- Add an assertion that the Hermes envelope contains **no `cwd` key**, not merely a
  null one.
- Rewrite `test_the_hermes_handler_binds_too` to load
  `plugins/hermes/__init__.py` by file path via `importlib.util.spec_from_file_location`
  under a synthetic module name — never `import plugins.hermes`.
- **Red-state proof**: `git stash` the `src/` and `plugins/` changes (or check the
  new tests out against the baseline commit), run them, and paste the failure output
  into the PR description. Tests that pass in both states are vacuous and must be
  strengthened before proceeding.

### 6. Validate the build

- **Task ID**: `validate-build`
- **Depends On**: `build-plugin`, `build-adapter`, `build-fixtures`,
  `build-contract`, `build-suite`
- **Assigned To**: `hermes-validator`
- **Agent Type**: `validator`
- **Parallel**: false
- Run the Verification table. Report the environment (`redis` / `redis-py` versions,
  `POPOTO_TEST_DB=9`, worktree path, editable-install resolution) alongside every
  count — `CLAUDE.md`'s rule is that a count without its environment is unusable.
- Confirm the five worktree-verification gotchas: correct package under test, full
  extras installed (`.[dev,embeddings,mcp]`), DB isolation, and the known-noise
  `tests/test_version.py::test_version_matches_pyproject` failure on a stale editable
  install.

### 7. Documentation

- **Task ID**: `document-feature`
- **Depends On**: `validate-build`
- **Assigned To**: `hermes-documentarian`
- **Agent Type**: `documentarian`
- **Parallel**: false
- Execute every checkbox in the Documentation section.
- Every rewritten Hermes claim carries an executed citation — a file:line in the
  installed `hermes-agent==0.19.0` package or the replaced fixture. No claim may cite
  only the vendor website.
- `mkdocs build --strict` must pass.

### 8. Final validation

- **Task ID**: `validate-all`
- **Depends On**: `document-feature`
- **Assigned To**: `hermes-validator`
- **Agent Type**: `validator`
- **Parallel**: false
- Re-run the full Verification table plus the full suite.
- Verify every Success Criterion, including the red-state proof's presence in the PR
  description.
- Report pass/fail with the environment stated.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full suite passes | `POPOTO_TEST_DB=9 pytest -q` | exit code 0 |
| Integration tests pass | `POPOTO_TEST_DB=9 pytest tests/test_integrations_hooks.py tests/test_integrations_service.py tests/test_integrations_db0_isolation.py -q` | exit code 0 |
| Contract test skips cleanly without hermes-agent | `POPOTO_TEST_DB=9 pytest tests/test_hermes_plugin_contract.py -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Gateway shape deleted | `test ! -e plugins/hermes/HOOK.yaml && test ! -e plugins/hermes/handler.py` | exit code 0 |
| Plugin manifest present | `test -f plugins/hermes/plugin.yaml && test -f plugins/hermes/__init__.py` | exit code 0 |
| `register(ctx)` entry point exists | `python -c "import ast,pathlib;t=ast.parse(pathlib.Path('plugins/hermes/__init__.py').read_text());raise SystemExit(0 if any(getattr(n,'name',None)=='register' for n in t.body) else 1)"` | exit code 0 |
| Both hooks registered | `grep -c 'register_hook' plugins/hermes/__init__.py` | output > 1 |
| No async callbacks in the plugin | `python -c "import ast,pathlib;t=ast.parse(pathlib.Path('plugins/hermes/__init__.py').read_text());raise SystemExit(0 if not any(isinstance(n,ast.AsyncFunctionDef) for n in ast.walk(t)) else 1)"` | exit code 0 |
| `assistant_response` mapped in the adapter | `grep -c 'assistant_response' src/popoto/integrations/hooks.py` | output > 0 |
| Contract workflow present | `test -f .github/workflows/hermes-contract.yml` | exit code 0 |
| Anti-criterion: no "Hermes sends no turn id" claim survives | `grep -rn 'Hermes sends none\|no turn id (Hermes' src/ tests/ docs/features docs/guides plugins/` | exit code 1 |
| Anti-criterion: no gateway install instruction survives | `grep -rn 'mkdir -p ~/.hermes/hooks' plugins/ docs/guides/ docs/features/ README.md` | exit code 1 |
| Anti-criterion: Hermes fixtures no longer graded "docs only" | `grep -i 'hermes' tests/fixtures/harness_payloads/README.md \| grep -c 'docs only'` | match count == 0 |
| Anti-criterion: `hermes-agent` absent from published deps | `grep -c 'hermes-agent' pyproject.toml uv.lock scripts/check_lock_imports.py` | match count == 0 |
| Anti-criterion: plugin never imported as a package | `grep -rn 'import plugins.hermes\|from plugins.hermes' tests/ src/ \| grep -v '\`\`'` | exit code 1 |
| `plugins.enabled` step taught in the guide | `grep -c 'hermes plugins enable' docs/guides/harness-hermes.md` | output > 0 |
| `plugins.enabled` step taught in the plugin README | `grep -c 'hermes plugins enable' plugins/hermes/README.md` | output > 0 |
| Correct install path in both READMEs | `grep -l '\.hermes/plugins/popoto-memory' plugins/hermes/README.md docs/guides/harness-hermes.md \| wc -l` | output > 1 |
| Fixtures keep a provenance string | `grep -l 'captured-from:' tests/fixtures/harness_payloads/hermes_pre_llm_call.json tests/fixtures/harness_payloads/hermes_post_llm_call.json \| wc -l` | output > 1 |
| No stale xfails | `grep -rn 'xfail' tests/ \| grep -v '# open bug'` | exit code 1 |
| Anti-criterion: repo-root `plugins/` stays a namespace portion (C5) | `test ! -e plugins/__init__.py` | exit code 0 |
| Anti-criterion: the "remaining four" count is gone — whitespace-insensitive (C4, R2-C1) | `tr '\n' ' ' < tests/fixtures/harness_payloads/README.md \| grep -cE 'remaining +four'` | output == 0 |
| Positive companion: the new middle tier label is present (R2-C1) | `grep -c 'real harness, no model' tests/fixtures/harness_payloads/README.md` | output > 0 |
| New plugin module is black-clean (C5) | `black --check plugins/hermes/` | exit code 0 |
| New plugin module is ruff-clean (C5) | `ruff check plugins/hermes/` | exit code 0 |
| Contract job declares an unconditional Redis service (C1) | `grep -c 'services:' .github/workflows/hermes-contract.yml` | output > 0 |
| Contract job is marked advisory (C2) | `grep -ci 'advisory' .github/workflows/hermes-contract.yml` | output > 0 |
| Pin carries a dated staleness comment (C3) | `grep -c 'pinned 2026-09-08' .github/workflows/hermes-contract.yml` | output > 0 |
| Contract test seeds a sentinel rather than asserting bare `dict` (C1) | `grep -ci 'sentinel' tests/test_hermes_plugin_contract.py` | output > 0 |
| Contract test actually captures the sentinel through a real service (R2-C2) | `grep -c '\.capture(' tests/test_hermes_plugin_contract.py` | output > 0 |
| Contract test asserts containment in the injected context (R2-C2) | `grep -cE 'assert .+ in .+\["context"\]' tests/test_hermes_plugin_contract.py` | output > 0 |

**Red-state proof required.** Before the implementation lands, run the three
anti-criteria that can be falsified today — *"no `Hermes sends no turn id` claim
survives"*, *"Hermes fixtures no longer graded docs only"*, and the
newline-collapsing *"remaining four"* row (`tr '\n' ' ' < … | grep -cE 'remaining +four'`,
which returns **1** at baseline) — against the baseline tree, confirm they FAIL, and
paste that output into the PR description. An anti-criterion never demonstrated red
is indistinguishable from one that cannot detect its violation. That is not
hypothetical here: the round-1 form of the third row, `grep -c 'remaining four'`,
returned 0 at baseline because the phrase wraps across `README.md:19-20`, and it was
caught only in critique round 2 (R2-C1).

## Critique Results

**Verdict:** READY TO BUILD (with concerns) — 0 blockers, 5 concerns, 1 nit.
**Depth:** FULL. **Mode:** independent roster (3 critics) — Risk & Robustness,
Scope & Value, History & Consistency. Run `d46186192d5346478dc7853b5e958068`,
2026-09-08, against baseline `952c46b5`.

**Lead verification performed during critique** (executed, not read):

- The planner's spike-3 claim — *the `turn_id` half of #688 needs no adapter code*
  — is **confirmed by execution** at the baseline commit:
  `PYTHONPATH=src python -c "…hooks.normalize({'hook_event_name':'pre_llm_call',
  'session_id':'s','turn_id':'t-1','user_message':'health checks'})"` yields
  `turn_id='t-1'`, `kind='read'`, `text='health checks'`, `cwd=None` with no code
  change. The **vacuity hazard the plan names is therefore real and reproduced**: a
  turn-id test written at the `normalize()` layer passes today. The plan's rule —
  every new turn-id assertion runs against the replaced fixture or the plugin
  envelope, plus a recorded red state — is the correct and sufficient answer.
- The counterpart is **genuinely falsifiable**: the same call with
  `{'hook_event_name':'post_llm_call','assistant_response':'automatic rollback'}`
  yields `text=''` today, so the one-line `_RESPONSE_FIELDS` change does have a test
  that fails without it. Success Criterion 4 is achievable as written.
- Prerequisites re-run: `redis-cli -n 9 PING` → PONG; the lane worktree exists;
  `import hermes_cli` → `ModuleNotFoundError` (the collision guard holds today);
  `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/` → 0 hits.
- Structural checks all PASS: required sections present; tasks 1–8 with valid,
  acyclic `Depends On` and a `Validates` on every build task; every referenced path
  exists except the four the plan creates; success criteria map to tasks; no No-Go
  or Rabbit Hole reappears as planned work.

### C1 — Contract-test assertion (e) can pass vacuously without a seeded Redis

- **Severity:** CONCERN · **Critics:** Risk & Robustness
- **Location:** Solution → Technical Approach, assertion (e); Task 4 `build-contract`
- **Finding:** Assertion (e) — `invoke_hook("pre_llm_call", **kwargs)` returns a list
  whose first element is a dict with a `"context"` key — is only meaningful when
  memory has something to inject. `handle_payload` does not guard `service.assemble()`
  (`src/popoto/integrations/hooks.py:305-313`), so with Redis down the plugin's own
  mandated `except Exception: return None` swallows it and `invoke_hook` returns
  `[None]` — indistinguishable from "nothing to inject". Task 4 hedges with "Redis
  service **if the test needs one**", leaving exactly that condition unresolved.
- **Suggestion:** Make the contract job's Redis service unconditional, and seed a
  known record before assertion (e).
- **Implementation Note:** In `tests/test_hermes_plugin_contract.py`, build a real
  `MemoryService` bound to the job's Redis, `.capture()` a known sentinel string,
  then assert `invoke_hook("pre_llm_call", **kwargs)[0]["context"]` **contains that
  sentinel**. A bare `isinstance(result[0], dict)` passes on the swallowed-exception
  path. If a Redis service is judged too heavy for this job, split (e) into its own
  test marked so its skip is visible, and keep (a)–(d) as the Redis-free gate.

### C2 — The plan never says `hermes-contract.yml` must not be a required check

- **Severity:** CONCERN · **Critics:** Risk & Robustness
- **Location:** Update System; Risk 3
- **Finding:** Risk 3 accepts that a pinned `hermes-agent==0.19.0` can go stale or be
  transiently unfetchable, but never draws the operational conclusion. Branch
  protection is not configured in-repo (no `.github/settings.yml`, no CODEOWNERS), so
  whoever wires the new workflow could reasonably mark it required — after which a
  PyPI outage or a yanked release blocks every unrelated PR.
- **Suggestion:** State in the workflow header comment *and* in Update System that
  this job is advisory and must not be added to required status checks.
- **Implementation Note:** This is a repo-settings action the PR diff cannot enforce,
  so it must be written down as a manual post-merge note rather than assumed. Word it
  next to the existing pin rationale so the two are read together: "green proves the
  manifest satisfies *0.19.0's* loader; this job is advisory and must not gate merge."

### C3 — The pinned `hermes-agent` version has no staleness signal at all

- **Severity:** CONCERN · **Critics:** Scope & Value
- **Location:** Risk 3; Architectural Impact
- **Finding:** The pin is deliberately kept out of `pyproject.toml`, `uv.lock` and
  `scripts/check_lock_imports.py` — correctly — but the consequence is that **no
  configured Dependabot lane can ever see it**. `.github/dependabot.yml` runs `uv` at
  `/` (line 24) and `/examples` (line 70) and `github-actions` at `/` (line 108);
  none of the three reads a `pip install hermes-agent==0.19.0` inside a workflow `run`
  step. Risk 3's remedy is "accept and document", with no owner and no cadence — the
  same class of silent rot this lane exists to close, one level up.
- **Suggestion:** Give the accepted risk a self-limiting mechanism rather than prose
  alone.
- **Implementation Note:** Cheapest form that survives review: a
  `# pinned 2026-09-08; re-check against latest hermes-agent by 2027-03` comment on
  the `pip install` line, plus one sentence in `docs/guides/harness-hermes.md` naming
  the pinned version and date so a reader can tell how stale the claim is. A
  `schedule:`-triggered non-gating re-run against unpinned `hermes-agent` is the
  stronger option and is acceptable **only** if it cannot report failure on a PR.

### C4 — "the remaining four" corrects to two, not three

- **Severity:** CONCERN · **Critics:** History & Consistency, Scope & Value
  (independently converged)
- **Location:** Test Impact → fixtures README bullet; Documentation → Inline
  Documentation; Task 3 `build-fixtures`
- **Finding:** `tests/fixtures/harness_payloads/README.md` reads verbatim "Two pairs
  now test the harness -- Claude Code and OpenClaw -- and the remaining four are
  still the maintainer's acceptance pass". That bucket holds exactly four *files* —
  the Codex pair and the Hermes pair — and fixtures move in pairs, so the count can
  only stay at four or drop to **two**. The plan's instruction to write "three" is
  arithmetically unreachable under either grading, and shipping a wrong count in the
  one file whose job is honest bookkeeping is the issue's own failure mode in
  miniature.
- **Suggestion:** Correct the instruction to "two remain, not four", and pair the
  number with the third-tier label so count and grade agree.
- **Implementation Note:** The sentence must be rewritten as a unit with the new
  three-tier table, not patched by digit: after this lane the buckets are *live turn*
  (Claude Code, OpenClaw = 4 files), *real dispatcher, not a live turn* (Hermes = 2),
  *binary/docs, not a live turn* (Codex = 2). Add a Verification row pinning it:
  `grep -c 'remaining four' tests/fixtures/harness_payloads/README.md` → 0.

### C5 — Risk 2's mitigation omits the invariant that actually decides the collision

- **Severity:** CONCERN · **Critics:** Structural (lead), extending Risk &
  Robustness's namespace analysis
- **Location:** Risk 2; Success Criteria; Verification
- **Finding:** Risk 2's three layers (temp cwd, `importlib` by path, hermes-free dev
  venv) are all sound but none names the property the resolution actually turns on.
  `hermes-agent` ships `plugins` as a **regular** package; popoto's `plugins/` has no
  `__init__.py`, so it is only a PEP 420 namespace *portion* — and a namespace portion
  never wins over a regular package found later on `sys.path`, whatever the order.
  That is why the collision is survivable. Adding `plugins/__init__.py` at the repo
  root — a plausible future "tidy-up", and exactly the kind of edit `plugins/hermes/__init__.py`
  invites — would silently flip it and shadow the vendor package inside the one job
  built to exercise it. Nothing in the plan pins this.
- **Suggestion:** State the invariant in Risk 2 and pin it with an anti-criterion.
- **Implementation Note:** Add to Verification: `test ! -e plugins/__init__.py` →
  exit 0, and add the same line to Success Criteria. Note in Risk 2 that
  `plugins/hermes/__init__.py` is required (Hermes imports the *plugin directory*)
  while `plugins/__init__.py` must never exist. Related: neither `ruff check src/`
  nor `black --check src/ tests/` covers `plugins/`, so the new module lands
  ungated — either widen `black --check` to `plugins/` or accept it explicitly.

### N1 — `integrations/__init__.py:11-12` carries no `handler.py` reference

- **Severity:** NIT · **Critics:** History & Consistency
- **Location:** Documentation → Inline Documentation; Task 2 `build-adapter`
- **Finding:** The plan lists `src/popoto/integrations/__init__.py:11-12` alongside
  `config.py:129,:383` as needing a "a Hermes `handler.py`" correction. Verified at
  baseline: `grep -rn "handler.py" src/popoto/integrations/` returns **only**
  `config.py:383`. `__init__.py:11-12` names the event pair
  `pre_llm_call`/`post_llm_call`, which stays true after the re-target;
  `config.py:129` says "a Hermes handler" in prose and does want rewording.
- **Suggestion:** Change that row to "confirm, no change expected" for
  `__init__.py:11-12` rather than sending the builder to edit a correct line.

### Open Question Rulings (resolved in critique; not escalated)

1. **Is a `hermes-agent==0.19.0` CI job acceptable?** — **Yes, build it.** It is the
   only mechanism that makes this integration falsifiable by machine rather than by a
   human re-reading vendor docs, which is precisely what failed in #515, and its cost
   is confined to one job's own venv. Conditions: C1 (seed Redis or split assertion
   (e)), C2 (advisory, never a required check), C3 (dated pin), C5 (no
   `plugins/__init__.py`, ever).
2. **What should the new fixture grade say?** — **Adopt the plan's three-level
   framing.** Hermes's grade is backed by the real `PluginManager` and `invoke_hook`;
   Codex's is backed by a binary's schema and a *failed* live attempt. Collapsing
   them onto one phrase would destroy a distinction the project paid to establish.
   Word the Hermes rows: *"real `hermes-agent` 0.19.0 plugin loader and `invoke_hook`
   dispatcher; kwargs verbatim from the 0.19.0 invoke sites; not a live model turn."*
   Fix the count per C4.
3. **Should `POPOTO_MEMORY_AGENT_ID` be required on Hermes?** — **No — recommended,
   documented, not enforced.** The plan's own argument defeats the alternative:
   Hermes swallows plugin logging into `~/.hermes/logs/agent.log`, so a first-use
   warning adds a branch and buys nothing. Keep it as guide text next to the absent-`cwd`
   explanation; do not add the warning branch and do not raise.
4. **Does this PR close #688 outright?** — **Yes, outright.** #688 asked whether
   Hermes carries a per-turn id; its own comment answered yes, and resolution path (a)
   ("plumb it through as `turn_id`") is fully inside this lane's Success Criteria. No
   #688-owned work is deferred to a No-Go. The PR body carries `Closes #704` and
   `Closes #688`; it must **not** hedge with "partially addresses".

---

## Critique Results — Round 2 (re-critique of the revised plan)

**Verdict:** READY TO BUILD (with concerns) — 0 blockers, 2 concerns, 0 nits.
**Depth:** FULL. **Mode:** independent roster (3 critics) — Risk & Robustness,
Scope & Value, History & Consistency. Run `d46186192d5346478dc7853b5e958068`,
2026-09-08, against revised plan commit `b508e8c7`.

**Fold-in audit — all six round-1 items verified present and faithful:**

| Item | Folded into | Verified |
|---|---|---|
| C1 (seeded sentinel, unconditional Redis) | Technical Approach bullet; Task 4; Success Criteria; 2 Verification rows | yes |
| C2 (advisory, never a required check) | Risk 3 operational conclusions; Update System; Task 4; Success Criteria; Verification row | yes |
| C3 (dated pin + version/date in the guide) | Technical Approach; Risk 3; `docs/guides/harness-hermes.md` bullet; Verification row | yes |
| C4 (count corrects four → **two**, three-tier grading) | Risk 1; Test Impact; Documentation; Task 3; Success Criteria; Verification row | yes (but see R2-C1) |
| C5 (namespace-portion invariant; `plugins/` lint gap accepted) | Risk 2 rewritten; Success Criteria ×2; Verification rows ×3 | yes |
| N1 (`integrations/__init__.py:11-12` is confirm-only) | Documentation → Inline; Task 2 | yes |

Scope & Value and History & Consistency each returned **No findings** — no scope
crept in, no correct pre-existing content was deleted or weakened by the revision
diff, and the added file:line citations spot-check accurate against the real files.
Both round-2 concerns are hardening of *the new Verification rows themselves*; the
substantive instructions they guard are correct as written.

### R2-C1 — The C4 anti-criterion cannot detect its own violation (line-wrapped phrase)

- **Severity:** CONCERN · **Critics:** Structural (lead), executed
- **Location:** Verification table, row *"Anti-criterion: the 'remaining four' count
  is gone (C4)"*; Success Criteria, the matching bullet
- **Finding:** `tests/fixtures/harness_payloads/README.md:19-20` wraps the sentence
  between the two words — `…and the remaining\nfour are still the maintainer's
  acceptance pass`. `grep -c 'remaining four' tests/fixtures/harness_payloads/README.md`
  therefore returns **0 at the baseline commit, before any edit**, so the row passes
  vacuously and the Success Criterion that mirrors it is already satisfied. The plan's
  own *"Red-state proof required"* paragraph names exactly this failure — "an
  anti-criterion never demonstrated red is indistinguishable from one that cannot
  detect its violation" — and this row is one. (Executed at baseline: the row returns
  0/exit 1; the sibling rows `grep -c 'docs only'` → 2 and the `Hermes sends none`
  anti-criterion → 3 hits are properly red, so the defect is isolated to this one row.)
- **Suggestion:** Make the check whitespace-insensitive, and assert the *replacement*
  text rather than only the absence of the old.
- **Implementation Note:** Replace the row's command with a newline-collapsing form,
  e.g. `tr '\n' ' ' < tests/fixtures/harness_payloads/README.md | grep -cE 'remaining +four'`
  → 0, and add a positive companion row asserting the new tier label is present, e.g.
  `grep -c 'real dispatcher' tests/fixtures/harness_payloads/README.md` → output > 0.
  Update the matching Success Criterion to name the positive assertion too. Do **not**
  simply reword the criterion to "the count sentence is rewritten" — that is
  unverifiable by command. This is a Verification-table edit, not new build scope.

### R2-C2 — The C1 anti-criterion is a bare word grep, not a wiring check

- **Severity:** CONCERN · **Critics:** Risk & Robustness
- **Location:** Verification table, row *"Contract test seeds a sentinel rather than
  asserting bare `dict` (C1)"*
- **Finding:** The check is `grep -ci 'sentinel' tests/test_hermes_plugin_contract.py`
  → output > 0. A comment, a docstring, or an unused local named `sentinel` satisfies
  it without the test ever calling `.capture()` on a real `MemoryService` or asserting
  containment in the returned `["context"]`. The row that exists specifically to catch
  the corner-cut version of assertion (e) does not catch it.
- **Suggestion:** Assert the wiring, not the word.
- **Implementation Note:** Require the same literal token to appear both after
  `.capture(` and inside an `in`-assertion against the injected context. Cheapest
  command form that survives review:
  `grep -c '\.capture(' tests/test_hermes_plugin_contract.py` → output > 0, **and**
  `grep -cE 'assert .+ in .+\["context"\]' tests/test_hermes_plugin_contract.py` →
  output > 0, as two rows. Keep the existing `sentinel` row as well; it is harmless,
  just insufficient alone. The file does not exist at baseline, so both rows are red
  today by construction — this is a plan-level gap, not an implementation bug.

**Disposition.** The critique cycle cap is 2 and this is round 2, so these two
concerns are **accepted on the record** and the build proceeds. Both are single-row
edits to the Verification table that the builder (Task 4 / Task 3) applies in place;
neither changes the implementation contract.

**Round-2 fold-in applied** (2026-09-08, `revision_applied_at` below):

| Item | Folded into |
|---|---|
| R2-C1 (line-wrapped `remaining four`) | Verification row replaced with the `tr`-collapsing form + new positive companion row on `real dispatcher`; Success Criteria bullet rewritten; Task 3 bullet corrected; Red-state proof paragraph widened from two rows to three |
| R2-C2 (bare `sentinel` word grep) | Two Verification rows added (`\.capture(`, `assert … in …["context"]`); Success Criteria (e) bullet extended; Task 4 bullet added |

No implementation-contract text changed. The plan is settled; the `plan_revising`
lock is cleared.

---

## Critique Results — Round 3 (final bounded round — confirmation of the round-2 fold-ins)

**Verdict:** READY TO BUILD (no concerns) — 0 blockers, 0 concerns, 2 nits.
**Depth:** FULL. **Mode:** independent roster (3 critics) — Risk & Robustness,
Scope & Value, History & Consistency; roster 3/3 complete, all grounded. Run
`d46186192d5346478dc7853b5e958068`, 2026-09-08, against revised plan commit
`8d6ec6cb`.

**R2-C1 fold-in — verified real and non-vacuous (commands executed at baseline):**

| Command | Baseline result | Meaning |
|---|---|---|
| `tr '\n' ' ' < tests/fixtures/harness_payloads/README.md \| grep -cE 'remaining +four'` | **1** | genuinely RED before the edit — detects its own violation |
| `grep -c 'remaining four' tests/fixtures/harness_payloads/README.md` | 0 (exit 1) | the round-1 form, vacuously green — the defect R2-C1 named, confirmed |
| `grep -c 'real dispatcher' tests/fixtures/harness_payloads/README.md` | 0 | positive companion row is RED before the edit, green only after |

The phrase does wrap at `tests/fixtures/harness_payloads/README.md:19-20`
(`…and the remaining` / `four are still the maintainer's acceptance pass`),
exactly as R2-C1 stated.

**R2-C2 fold-in — verified red at baseline by construction:**
`tests/test_hermes_plugin_contract.py` does not exist, so
`grep -c '\.capture(' …` and `grep -cE 'assert .+ in .+\["context"\]' …` both
produce no matching output (exit 2). Both rows can only go green once the test
is written with real wiring; neither is satisfiable by a comment, a docstring, or
an unused local named `sentinel`.

**Regression check on the two revision diffs** (`b508e8c7`, `8d6ec6cb`): every hunk
is additive or a strict correction. No prior correct content was deleted, weakened,
or left contradicting the new text; no new build scope entered through the
Verification table — the `.capture(`/containment rows machine-check a requirement
round 1 had already written into the Technical Approach. The four R2-C1 touch
points (Verification row, Success Criteria bullet, Task 3 bullet, red-state-proof
paragraph, now correctly "three" rows) agree with each other, and the surviving
plain-`grep -c 'remaining four'` mentions are confined to the historical critique
records where they describe the defect under discussion.

**Structural checks:** required sections present; tasks 1-8 sequential with no gaps
or dangling dependencies; every referenced existing path resolves; prerequisites
green (`redis-cli -n 9 PING` → PONG, lane worktree present); success criteria all
map to tasks; no No-Go or Rabbit Hole appears as planned work.

### N-R3-1 — `README.md:23` still says "the other four"

- **Severity:** NIT
- **Location:** Task 3 / `tests/fixtures/harness_payloads/README.md:23`
- **Finding:** The same stale count survives a second time at line 23 ("a warning
  about the other four"), which no anti-criterion covers; after the Task 3 edit the
  README could still assert four unverified fixtures while every Verification row is
  green.
- **Suggestion:** When rewriting the count sentence in Task 3, correct line 23 in the
  same pass — the tier rewrite should leave no "four" describing the
  not-yet-live-verified bucket.

### N-R3-2 — the containment regex is single-line

- **Severity:** NIT · **Critic:** Scope & Value
- **Location:** Verification row *"Contract test asserts containment in the injected
  context (R2-C2)"*
- **Finding:** `assert .+ in .+\["context"\]` is a single-line regex and can
  false-negative on a correct assertion that black wraps across lines.
- **Suggestion:** If the builder's formatted assertion does not match during Task 4
  self-check, widen the row to the newline-collapsing `tr '\n' ' ' | grep -cE …`
  form used two rows above, rather than reading the red result as a plan failure.

**Disposition.** Both round-2 concerns are answered by the `8d6ec6cb` revision and
neither survives round 3. Nits do not block and require no revision pass; the
`plan_revising` lock stays clear. Proceed to `/do-build`.

---

## Open Questions

**All four open questions were ruled on during critique round 1 and are closed.**
See *Critique Results → Open Question Rulings* immediately above for the reasoning;
the rulings are folded into the plan body (Solution → Technical Approach, Risk 1,
Risk 3, Test Impact, Success Criteria, Verification) and are the build contract:

1. **`hermes-agent==0.19.0` CI job** — build it, conditioned on C1 (seeded
   sentinel + unconditional Redis service), C2 (advisory, never a required check),
   C3 (dated pin comment plus the version/date in the guide) and C5 (no
   `plugins/__init__.py`, ever).
2. **Fixture grade** — adopt the three-level framing (*live turn* / *real
   dispatcher, not a live turn* / *binary or docs, not a live turn*). Hermes rows
   read: "real `hermes-agent` 0.19.0 plugin loader and `invoke_hook` dispatcher;
   kwargs verbatim from the 0.19.0 invoke sites; not a live model turn." The count
   corrects from four to **two** (C4).
3. **`POPOTO_MEMORY_AGENT_ID`** — recommended and documented, **not** required. No
   first-use warning branch, no raise.
4. **#688** — closed outright. The PR body carries `Closes #704` **and**
   `Closes #688`, with no "partially addresses" hedge.

No question is escalated to the supervisor; nothing here blocks build.
