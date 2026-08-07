---
status: Planning
type: feature
appetite: Large
owner: Tom Counsell
created: 2026-08-07
tracking: https://github.com/tomcounsell/popoto/issues/515
last_comment_id:
---

# Harness Integration: Subconscious Memory for Claude Code, Codex, Hermes, and OpenClaw

## Problem

Popoto has zero integrations with any agent harness. A developer who runs Claude Code, Codex, Hermes, or OpenClaw and wants Popoto-backed memory has to write the glue themselves: instantiate `SubconsciousMemory`, author an 8-field model, find a place in the harness to call `inject_context()` before the model call and `extract_memories()` after, and keep a Redis connection alive across processes. Nothing in the docs tells them where those call sites are, because in a harness the developer does not own the LLM call.

**Current behavior:** `src/popoto/recipes/subconscious_memory.py` takes a `list[dict]` of OpenAI-shaped messages and returns a mutated list. That contract fits a Python program that makes its own LLM calls. It fits no harness, because in every harness the message array is built inside the harness process. `grep -ri mcp src/ pyproject.toml mkdocs.yml` returns nothing; there is no CLI entry point, no MCP server, and no wiring guide. The module docstring at `src/popoto/recipes/subconscious_memory.py:28` still points at a PydanticAI guide that does not exist.

The consequence for the funnel: there is no inbound search path from "claude code persistent memory" or "codex memory" to Popoto, and no way for a developer to try subconscious memory in the tool they already use.

**Desired outcome:** `pip install popoto[mcp]` plus a short config block gives a developer on any of the four harnesses memory that injects before every turn and captures after every turn, backed by their own local Redis or Valkey, with no API keys and no schema authoring. The same install also exposes discretionary memory tools over MCP for mid-task search, explicit save, and correction.

## The design tension this plan resolves

Subconscious memory means the memory layer runs on **every** turn whether or not the model asks for it. A plain MCP tool server cannot deliver that: MCP tools are agent-elected, so memory becomes something the model chooses to call. That is instructed memory wearing a subconscious label, and it is the single most important thing to get right in this work.

The prior art agrees, including the vendors who bet the other way. Supermemory, explaining why they shipped a plugin on top of their MCP server: *"We cannot control when claude code chooses to run the tools. This means that we have no control / data point to learn things from, and a memory system is only good if there's things to recall later."* Mem0's own troubleshooting page tells users that if memories are not being captured they installed the MCP-only variant and need the marketplace plugin, "since MCP-only installs require manual memory operations."

Every harness in scope has a deterministic, model-independent surface that fires per turn. That surface, not MCP, is where the subconscious loop belongs. MCP remains the portable common core for the discretionary half (search on demand, save explicitly, correct a wrong memory) and for reaching clients where no hook surface exists.

**Split of responsibility, fixed for this plan: hooks recall and capture; MCP tools search, save, and correct.**

## Freshness Check

**Baseline commit:** `63cf035` (`Plan: docs repositioning around agent memory (#511)`)
**Issue filed at:** 2026-08-07T06:10:34Z
**Disposition:** Unchanged

- Issue #515 was filed hours ago in this session. Commits on main since: only `63cf035`, the sibling docs plan, which touches no file this plan touches.
- The one file:line reference in the surrounding context — the false PydanticAI guide claim — was re-verified: `src/popoto/recipes/subconscious_memory.py:28` still reads "The guide shows how to wire it into PydanticAI or the OpenAI SDK." Its removal is owned by #513, not by this plan.
- `grep -rin mcp` over `src/`, `pyproject.toml`, `mkdocs.yml` returns zero hits — confirmed greenfield, no prior integration surface to reconcile.
- `pyproject.toml` has no `[project.scripts]` table today; adding one is new, not a modification.
- Cited siblings re-checked: #511 OPEN (plan committed), #512 OPEN, #513 OPEN, #514 OPEN. None closed since filing.
- Active plans in `docs/plans/` overlapping this area: `docs_repositioning.md` (#511) lists this work as `[SEPARATE-SLUG #515]` in its No-Gos and states "the harness-integration sibling issue owns that surface" — coordination, not conflict. `integration_feedback_dx_gaps.md` (#370, shipped) is the closest prior art for "an external integrator hit friction" but predates the harness framing.

## Prior Art

**In-repo:**

- **#370 / `integration_feedback_dx_gaps.md`** (shipped): the only prior integration-driven work. Its finding — that integrators outside `ContextAssembler.assemble()` had no way to compute `RetrievalQuality` — is the same shape of gap this plan closes at the harness layer. `RetrievalQuality.from_records()` exists because of it and is directly useful to the hook's telemetry path.
- **#489 / PR #510** (evaluated, `4e8e316`): LLM extraction vs heuristic vs raw ingestion. **Every extraction arm lost to raw ingestion on judged accuracy: heuristic 0.2078 vs raw 0.3636 on the same slice.** This is the single most load-bearing prior result for this plan, because `SubconsciousMemory.extract_memories()` defaults to `HeuristicExtractionProvider`. A hook that calls it on every turn would ship Popoto's measured-worst write path, at maximum volume, on the most visible surface.
- **#513** (open): batteries-included default `Memory` model, benchmarked `score_weights` default, content-only injection format. A **hard prerequisite** — see Prerequisites.
- **#456** (epic, strategy of record): live-agent memory, native benchmarks, no leaderboard chasing. Harness integration is the distribution arm of that strategy; nothing here changes the claims posture.
- **#511 / `docs_repositioning.md`**: memory-first docs restructure. This plan adds a nav destination ("Add memory to your harness") that the repositioning must accommodate; sequencing is discussed under Risks.
- **#409** (closed): silent query-blind retrieval when a model lacks `BM25Field`. The harness path must never resolve to composite mode silently — the default model from #513 carries `BM25Field`, and the `doctor` command surfaces the resolved mode.

**External (see Research for URLs):** Basic Memory ships an MCP server as a console entry point inside its main pip package with the Claude Code plugin in the same repo; Mem0 and Graphiti split the MCP server into a separate package or directory and pay for it in setup friction. Mem0's Claude Code plugin registers `SessionStart`, `UserPromptSubmit`, `Stop`, `PreToolUse`, `PostToolUse`, and `PreCompact` hooks alongside its MCP server — direct confirmation that the hooks-plus-MCP shape is the one that works.

## Research

WebSearch and WebFetch, 2026-08-07. Primary-source fetches were done for every claim that constrains the design; secondary sources are marked.

**Queries used:**
- Claude Code hooks reference (primary fetch: `code.claude.com/docs/en/hooks`)
- Claude Code plugin structure and marketplaces (primary fetch: `code.claude.com/docs/en/plugins`)
- Codex CLI MCP configuration (primary fetch: `learn.chatgpt.com/docs/extend/mcp?surface=cli`)
- Codex CLI hooks / config reference (primary fetch: `learn.chatgpt.com/docs/config-file/config-reference`)
- OpenClaw plugin hooks and MCP (primary fetches: `docs.openclaw.ai/plugins/hooks`, `docs.openclaw.ai/cli/mcp`)
- Hermes agent harness hooks and MCP (Nous Research; secondary sources + docs summaries)
- Mem0 / OpenMemory / Graphiti / Basic Memory / Supermemory MCP-and-hooks shape (primary fetch: `docs.mem0.ai/integrations/claude-code`)

**Key findings:**

1. **Claude Code and Codex have converged on the same hook contract.** Both fire `UserPromptSubmit` before the model sees the prompt and accept `hookSpecificOutput.additionalContext` (or plain stdout) as injected context. Both fire `Stop` at end of turn and both put the assistant's text in an input field named `last_assistant_message`. Sources: `code.claude.com/docs/en/hooks`, `learn.chatgpt.com/docs/config-file/config-reference`. **This is the central design finding: one hook executable, reading harness JSON on stdin and writing harness JSON on stdout, covers two of the four harnesses with no per-harness code.**
2. **Claude Code hook input carries `transcript_path` on every event and `last_assistant_message` on `Stop`/`SubagentStop`** — "the final assistant text of the current turn (for hooks that need it instead of reading the transcript file)." No transcript parsing is needed for the write path.
3. **Only three Claude Code events can inject context:** `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`. Every other event's stdout goes to the debug log. This bounds the read path to those events.
4. **Claude Code hooks support `"async": true`, `"type": "http"`, and `"type": "mcp_tool"`.** The async flag lets the write path run off the critical path; the http type is the escape hatch if process startup cost is too high; the `mcp_tool` type means a hook can invoke an MCP tool without the model electing to.
5. **Claude Code plugins bundle hooks and an MCP server in one installable unit** (`.claude-plugin/plugin.json`, `hooks/hooks.json`, `.mcp.json` at plugin root), distributed via a git-repo marketplace (`.claude-plugin/marketplace.json`). Install is `claude plugin marketplace add <owner>/<repo>` then `claude plugin install <name>@<marketplace>`. This is the two-command install path.
6. **Codex gates hooks twice.** `[features] hooks = true` in `config.toml`, and every non-managed command hook is hashed and **skipped until reviewed via `/hooks`**; editing the definition re-triggers review. A known rough edge silently skips untrusted project-level `.codex/hooks.json` with no prompt. Codex MCP config is `[mcp_servers.NAME]` in `~/.codex/config.toml` (`command`/`args`/`env_vars`, or `url`/`bearer_token_env_var`), or `codex mcp add NAME -- <cmd>`. Codex also enforces an `additionalContextLimit` (default 2500 tokens).
7. **Hermes (Nous Research) has a Python filesystem hook surface that is a near-perfect fit.** `pre_llm_call` receives `extra.user_message`, `extra.conversation_history`, `extra.is_first_turn`, `session_id`, `cwd` and returns `{"context": "..."}`; `post_llm_call` and `transform_llm_output` receive the final assistant text. Hooks live at `~/.hermes/hooks/<name>/` as `HOOK.yaml` + `handler.py` exposing `async def handle(event_type, context)`. Gateway events: `gateway:startup`, `session:start|end|reset|compress`, `agent:start|step|end`, `command:*`. MCP via `hermes mcp add` / a top-level `mcp_servers:` config key. **Hermes docs state injected context lands in the user message, never the system prompt, explicitly to preserve prompt caching** — independent confirmation of the approach in finding 9. Confidence: high on the hook contract, medium on the exact on-disk layout (repo not fetched directly).
8. **OpenClaw is a TypeScript-plugin gateway with a rich hook set.** `agent_turn_prepare` and `before_prompt_build` return `prependContext`/`appendContext`/`systemPrompt`/`prependSystemContext`/`appendSystemContext`; `llm_output` and `agent_end` observe generated text; `before_agent_reply` can short-circuit a turn. Plugins are Node modules (`definePluginEntry`, `api.on(...)`), installed via `openclaw plugins install` from `clawhub:`, `npm:`, `git:`, or a local path. MCP config lives at `mcp.servers` in `~/.openclaw/openclaw.json` (stdio `command`/`args`, or `url` + `transport: "streamable-http"`), and `openclaw mcp serve` exposes OpenClaw's own conversations to external MCP clients. Whether a plugin may spawn a subprocess is **unverified** — plugins run in-process as Node modules so `child_process` should be reachable, but no doc statement confirms it.
9. **Injecting into the user turn rather than the system prompt preserves prompt caching.** Hermes says so explicitly; the same holds for Claude Code's `additionalContext`. Note the contrast with the library path: `SubconsciousMemory.inject_context()` mutates `messages[0]` (the system message), which would invalidate a cached system prefix on every turn. The harness path is architecturally better here, and the shared core must not inherit the system-message mutation.
10. **Hooks must return fast.** `claude-mem` enqueues to a background worker because LLM compression takes 5-30s. Popoto's measured retrieval p50 is 3.0 ms at 1k records and 6.0 ms at 20k — the retrieval itself is nowhere near the budget. The risk is Python process startup, not Redis.
11. **Packaging: in-package console entry point wins.** Basic Memory (`uvx basic-memory mcp`, plugin in the same repo under `plugins/claude-code`, installed with `--sparse`) has the lowest-friction story. Mem0's OpenMemory needs Docker + Postgres + Qdrant; Graphiti's needs a repo clone and `cd mcp_server`.
12. **No API key is a real, unclaimed differentiator.** Mem0 hosted requires `MEM0_API_KEY`; OpenMemory requires `OPENAI_API_KEY` for embeddings; Graphiti requires an LLM key for graph operations. Popoto's lexical/BM25 path needs neither.
13. **Tool-naming discipline matters.** Mem0 publishes `add_memories`/`search_memory` in OpenMemory and `add_memory`/`search_memories` in hosted — two conventions for one product, and a recurring source of user confusion. Pick one prefix and never publish a second.
14. **No vendor publishes a token or latency budget for always-on injection.** A widely repeated "MCP servers cost 35x more tokens than CLI tools" claim could not be traced to a primary benchmark and must not be cited. Publishing our own measured injection cost is therefore an available transparency asset.

## Spike Results

### spike-1: Do Claude Code and Codex hooks share enough contract to run one executable?
- **Assumption**: "Per-harness hook scripts are required; each harness needs its own adapter."
- **Method**: web-research (primary docs fetched for both harnesses)
- **Finding**: **False, and this reshapes the plan.** Both harnesses send JSON on stdin containing `hook_event_name`, both accept `hookSpecificOutput.additionalContext` from `UserPromptSubmit`, and both name the assistant text `last_assistant_message` on `Stop`. A single executable that dispatches on `hook_event_name` serves both. Harness-specific work collapses to a declarative config file per harness.
- **Confidence**: high
- **Impact on plan**: The unit of work becomes one hook executable plus four config recipes, not four integrations. Claude Code first buys Codex for near-zero marginal cost.

### spike-2: Can a hook observe the assistant's response without parsing the transcript?
- **Assumption**: "Post-turn extraction requires reading the JSONL transcript."
- **Method**: web-research (primary: `code.claude.com/docs/en/hooks`)
- **Finding**: No parsing needed. `Stop`/`SubagentStop` include `last_assistant_message`, documented as being there "for hooks that need it instead of reading the transcript file." Codex `Stop` provides the same field. Hermes `post_llm_call` and OpenClaw `llm_output`/`agent_end` provide equivalents.
- **Confidence**: high
- **Impact on plan**: The write path is a pure function of the hook payload. No transcript-format coupling, no breakage when the JSONL schema changes.

### spike-3: Is "< 10 lines of config" achievable on each harness?
- **Assumption**: "A config snippet is all the user needs."
- **Method**: web-research
- **Finding**: **Claude Code yes** (8-line `settings.json` hooks block, or two commands via the plugin marketplace). **Codex no, not honestly** — beyond the config there is `[features] hooks = true` plus a mandatory per-hash trust review through `/hooks`, and untrusted project-level hooks are silently skipped. **Hermes yes** (a two-file hook directory). **OpenClaw: config yes, but the automatic path requires a TypeScript plugin,** and whether it can shell out is unverified.
- **Confidence**: high for Claude Code and Codex; medium for Hermes; medium-low for OpenClaw
- **Impact on plan**: The acceptance criterion is met on Claude Code and stated honestly per-harness elsewhere. The Codex trust step is documented as a step, not hidden. OpenClaw ships MCP-only in this cycle unless the shell-out spike (spike-6, at build time) resolves favorably.

### spike-4: Is the default write path safe to run on every turn?
- **Assumption**: "The hook calls `extract_memories()`, the recipe's post-turn method."
- **Method**: code-read (`src/popoto/recipes/subconscious_memory.py:219-283`) + prior measurement (#489)
- **Finding**: **Unsafe as a default.** `extract_memories()` resolves to `HeuristicExtractionProvider` when no provider is passed, which sentence-splits the response. #489 measured that arm at 0.2078 judged accuracy against 0.3636 for raw-turn ingestion on the same slice. There is currently no raw-turn write path in the codebase.
- **Confidence**: high (measurement is committed, in-repo)
- **Impact on plan**: A `RawTurnExtractionProvider` is added to `popoto.extraction` and becomes the harness default. This is the one code change outside the integration package, and it is non-negotiable — see Risk 1.

### spike-5: Does injecting per turn break prompt caching?
- **Assumption**: "Per-turn injection invalidates the cached prefix and makes memory expensive."
- **Method**: web-research
- **Finding**: Not on the hook path. Both Claude Code `additionalContext` and Hermes `pre_llm_call` context land in the user turn; Hermes documents the choice as caching-motivated. The library path is the one with the problem: `inject_context()` mutates `messages[0]`, the system message.
- **Confidence**: high for Hermes (explicit doc statement), medium-high for Claude Code (mechanism inferred from `additionalContext` semantics; re-verify at build time)
- **Impact on plan**: The shared core returns a context *string*; message placement is the harness adapter's decision. The core must not carry `inject_context()`'s system-message mutation.

### spike-6: May an OpenClaw plugin spawn a subprocess? (build time, time-boxed)
- **Assumption**: "An OpenClaw TypeScript plugin can shell out to `popoto-memory hook`, so OpenClaw gets the automatic path too."
- **Method**: attempted install; documentation review
- **Finding**: **Unresolved, and not resolvable here.** OpenClaw is not installed on any machine this repo is developed on, and installing a full agent gateway to test one `child_process` call was outside the time box. Plugins load as in-process Node modules, so `child_process` should be reachable, and no documentation says otherwise -- but "should be" is not verification.
- **Confidence**: low
- **Impact on plan**: Followed the plan's stated fallback. OpenClaw ships MCP-only this cycle, `docs/guides/harness-openclaw.md` states the gap plainly rather than softening it, and the capability table marks auto-inject and auto-capture as "no, plugin required". The adapter work is already done: `render_context()` emits `{"appendContext": ...}` and the OpenClaw fixtures round-trip through it, so the plugin, once someone can verify shell-out, is a dozen lines. Recommend filing it as a follow-up issue.

### spike-1 re-verification at build time (Claude Code and Codex)
Both halves of spike-1 were re-checked against installed software rather than docs.

- **Claude Code**: verified live. A headless `claude` 2.1.220 run with capture hooks produced `UserPromptSubmit` carrying `hook_event_name`, `session_id`, `cwd`, `prompt`, and `Stop` carrying `last_assistant_message` -- confirming spike-1 and spike-2 exactly. Both payloads are committed as fixtures.
- **Codex**: the `codex-cli` 0.144.4 binary's own hook-input schema lists `session_id`, `transcript_path`, `hook_event_name`, `permission_mode`, `turn_id`, `agent_transcript_path`, `agent_type`, `last_assistant_message`, `prompt`, and its output wire includes `hookSpecificOutput.additionalContext` under a `"const": "UserPromptSubmit"`. Field for field identical to Claude Code, so the one-executable finding holds.
- **A live Codex capture was attempted and failed informatively.** With `.codex/hooks.json` in the project and `codex exec --enable hooks --dangerously-bypass-hook-trust`, no hook fired and Codex reported nothing. That is the silent project-level skip spike-3 predicted, reproduced first-hand, and it is now documented in the Codex guide as the first thing to check.

## Data Flow

Read path, per turn:

1. **Entry point**: harness fires its pre-model event (Claude Code / Codex `UserPromptSubmit`, Hermes `pre_llm_call`, OpenClaw `before_prompt_build`) and hands over the user's prompt text.
2. **Hook adapter** (`popoto.integrations.hooks`): normalizes the harness payload into `(event, query_text, session_id, cwd)`.
3. **MemoryService** (`popoto.integrations.service`): resolves config from environment plus cwd, opens the Redis/Valkey connection, calls `ContextAssembler.assemble(query_cues={"topic": query_text}, agent_id=...)` through the default `Memory` model from #513.
4. **Formatting**: `AssemblyResult.formatted` (content-only text form per #513) is capped to the harness injection budget.
5. **Output**: adapter emits the harness's own JSON shape — `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}` for Claude Code and Codex, `{"context": "..."}` for Hermes, `{"appendContext": "..."}` for OpenClaw. The harness places it in the user turn; the system prompt is untouched.
6. **Telemetry**: the assembled record keys are written to a short-TTL Redis key scoped by `session_id`, so the next `Stop` can report outcomes against them.

Write path, per turn:

1. **Entry point**: harness fires its post-model event (`Stop` / `post_llm_call` / `llm_output`) carrying `last_assistant_message`.
2. **Hook adapter**: normalizes to `(event, assistant_text, session_id)`.
3. **MemoryService**: saves the turn through `RawTurnExtractionProvider` — one record per turn, verbatim, matching the benchmarked ingestion arm. Heuristic and LLM extraction are opt-in via env var.
4. **Outcome reporting**: reads the pending record keys from step 6 above and calls `report_outcomes(...)`, feeding the confidence and decay loop.
5. **Output**: exit 0, no stdout. On Claude Code this hook is configured `"async": true` so it never sits on the turn's critical path.

MCP path, on demand: harness MCP client → stdio `popoto-memory mcp` → the same `MemoryService` → `memory_search` / `memory_save` / `memory_feedback` / `memory_status`.

Shared invariant: hook process, MCP process, and any user Python program all reach the same Redis keyspace through the same default model. There is exactly one schema.

## Architectural Impact

- **New dependencies**: `mcp` (the Python MCP SDK) under a new `popoto[mcp]` extra. Core install stays at 3 packages, 7.9 MB, zero API keys — that number is a published differentiator and must not regress. The hook path deliberately does **not** require `mcp`; hooks work on a bare `pip install popoto`.
- **Interface changes**: additive only. New `popoto.integrations` package, new `RawTurnExtractionProvider` in `popoto.extraction`, new `[project.scripts] popoto-memory` entry point (the repo has no `[project.scripts]` table today). No existing signature changes.
- **Coupling**: `popoto.integrations` depends on `popoto.recipes` and `popoto.extraction`; nothing in core depends on `popoto.integrations`. One-directional, and the package is importable-but-optional.
- **Data ownership**: unchanged. All state stays in the user's Redis/Valkey. No new service, no hosted component, no telemetry egress.
- **New public surface with a compatibility cost**: four MCP tool names and the hook payload contract become things users configure. Renaming either later is a breaking change for every installed config. Name them once (finding 13).
- **Reversibility**: high. Removing the extra and the `integrations` package leaves core untouched; uninstalling removes the entry point; harness configs are user-owned files.

## Appetite

**Size:** Large

**Team:** Solo dev + builder/validator subagent pairs; maintainer runs the real-harness acceptance pass (see Prerequisites — an agent cannot install Codex/Hermes/OpenClaw and drive a live turn).

**Interactions:**
- PM check-ins: 2-3 (tool-name and env-var freeze; per-harness honesty of the "< 10 lines" claim; Codex trust-step framing)
- Review rounds: 2 (one after the Claude Code reference path, one before publishing the marketplace manifest)

Large is driven by breadth and by the irreversibility of the public names, not by algorithmic difficulty. The core service is thin; the surface area and the four-harness verification matrix are what cost.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| **#513 default `Memory` model shipped** | `python -c "from popoto.recipes import DefaultMemory"` | Setup must be an import, not a schema exercise. Hard blocker — see below. |
| Local Redis or Valkey reachable | `redis-cli -h localhost -p 6379 ping` | Reference example and all integration tests |
| Valkey available for the module-free check | `docker run --rm -d -p 6380:6379 valkey/valkey:8` | Valkey-safe acceptance |
| Claude Code installed | `claude --version` | Reference-harness acceptance pass |
| MCP Python SDK installable | `python -c "import mcp"` after `pip install -e '.[mcp]'` | MCP server build |

**#513 is a hard blocker, not a soft one.** If the harness work ships first it must define its own memory model, and that model becomes a second schema competing with #513's — permanently, because it will be embedded in every user's installed config. Do not start the build until `DefaultMemory` (final name per #513) is importable. If #513 slips, the correct move is to reorder, not to vendor a temporary model.

## Solution

### Key Elements

- **`MemoryService`** — one harness-agnostic object over the #513 default model: `assemble(query, session) -> str`, `capture(text, session) -> ids`, `feedback(session, outcome)`, `status() -> dict`. Returns a context *string*; it never touches a message array and never mutates a system prompt.
- **`popoto-memory` CLI** — a single console entry point with subcommands `hook` (read harness JSON on stdin, write harness JSON on stdout), `mcp` (stdio MCP server), `doctor` (print resolved config, Redis reachability, effective retrieval mode, measured hook latency), and `demo` (seed and exercise the loop with no harness and no keys).
- **Hook adapter** — dispatches on `hook_event_name`, so one command string serves Claude Code and Codex identically. Hermes and OpenClaw adapters translate their native payloads into the same normalized shape.
- **MCP server** — four tools, one naming convention: `memory_search`, `memory_save`, `memory_feedback`, `memory_status`. Discretionary half only; recall and capture do not depend on the model calling these.
- **`RawTurnExtractionProvider`** — returns the turn verbatim as a single `ExtractedFact`. The benchmarked ingestion arm (#489), and the harness default.
- **Claude Code plugin** — `plugins/claude-code/` in this repo bundling `hooks/hooks.json` and `.mcp.json`, published through a root `.claude-plugin/marketplace.json`. Two-command install.
- **Per-harness wiring guides** — one page each, each stating plainly what is automatic and what is not on that harness.

### Flow

Developer journey:

`pip install popoto[mcp]` → `popoto-memory doctor` (confirms Redis, model, retrieval mode, latency) → paste the harness's config block (or `claude plugin install`) → restart the harness → next turn silently carries relevant memories, and the turn after that remembers what was just said → `popoto-memory doctor` again to see records written and the effective mode.

Per-turn runtime:

`user types` → **pre-model hook** → assemble from Redis (~5 ms) → context appended to the user turn → **model** → **post-model hook (async)** → turn saved verbatim + outcomes reported → next turn's assembly is better.

### Technical Approach

**Layout** (mirrors Basic Memory, the lowest-friction prior art):

```
src/popoto/integrations/
    __init__.py        # MemoryService export only; no heavy imports at module scope
    service.py         # MemoryService — the shared core
    config.py          # env + cwd resolution, agent_id derivation, budget defaults
    hooks.py           # normalize harness payload -> service call -> harness payload
    mcp_server.py      # stdio MCP server; imports `mcp` lazily
    cli.py             # popoto-memory: hook | mcp | doctor | demo
plugins/
    claude-code/       # .claude-plugin/plugin.json, hooks/hooks.json, .mcp.json, README
    codex/             # hooks.json + config.toml fragments + README (trust step documented)
    hermes/            # HOOK.yaml + handler.py + README
    openclaw/          # openclaw.json MCP fragment + README (+ plugin if spike-6 passes)
.claude-plugin/
    marketplace.json   # points at plugins/claude-code
examples/harness_memory/
    README.md, seed.py, verify.py   # local Redis, zero keys, no harness required
```

Python core in `src/` (ships in the wheel); harness assets at repo root under `plugins/` (a marketplace needs a git path, and these are declarative files with no logic). All behavior lives in the pip-installed CLI, so a user who declines the plugin can still wire it by hand.

**Startup latency is the real engineering constraint.** The read hook is synchronous and on the critical path; Redis costs ~5 ms but a cold Python process plus `import popoto` may not. Approach: keep `popoto.integrations.__init__` free of heavy imports, import `redis` and the model lazily inside `service.py`, and gate the build on a measured p95. Budget: **< 400 ms p95 end-to-end for `popoto-memory hook` on the read path.** If unmet after lazy-import work, escalate in order — (a) mark the write hook `"async": true` (already planned) so only the read path is exposed, (b) add `popoto-memory serve` as a local HTTP daemon and switch the Claude Code recipe to `"type": "http"`, which the harness supports natively. Do not adopt the daemon pre-emptively; a daemon is a second process users must manage.

**Configuration, all environment-driven, all optional:**

| Variable | Default | Notes |
|---|---|---|
| `POPOTO_MEMORY_URL` | `redis://localhost:6379/0` | Standard Redis URL; Valkey identical |
| `POPOTO_MEMORY_AGENT_ID` | basename of cwd | Per-project scoping by default, so one repo's memories do not bleed into another |
| `POPOTO_MEMORY_MAX_ITEMS` | 5 | Harness default; diverges from the benchmark, stated explicitly |
| `POPOTO_MEMORY_MAX_TOKENS` | 800 | Under Codex's 2500-token `additionalContextLimit` |
| `POPOTO_MEMORY_INGEST` | `raw` | `raw` \| `heuristic` \| `llm`; non-raw prints the #489 measured cost on first use |
| `POPOTO_MEMORY_ENABLED` | `1` | Kill switch that does not require editing harness config |

Scoring stays at the benchmarked configuration: `score_weights={"relevance": 1.0}` and the lexical/BM25 retrieval path from #513's model. **`max_items`/`max_tokens` are the deliberate divergence** — the benchmark used `max_items=20`, which is defensible for a QA eval and wrong for a coding harness where turns are frequent and context is contested. The guides state the divergence and the reason; they do not quietly present 5 as "the benchmarked config."

**Per-harness wiring, verbatim shapes from the docs:**

*Claude Code* — plugin (`claude plugin marketplace add tomcounsell/popoto`, `claude plugin install popoto-memory@popoto`) or manual `settings.json`:

```json
{ "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "popoto-memory hook" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "popoto-memory hook", "async": true }] }]
} }
```

*Codex* — the same `hooks.json` body at `~/.codex/hooks.json`, plus `[features] hooks = true` in `~/.codex/config.toml`, plus `/hooks` to review and trust. MCP via `codex mcp add popoto-memory -- popoto-memory mcp`. The trust step is a documented step, and the guide warns that a project-level `.codex/hooks.json` in an untrusted project is skipped silently.

*Hermes* — `~/.hermes/hooks/popoto-memory/` containing `HOOK.yaml` (`events: [pre_llm_call, post_llm_call]`) and a `handler.py` whose `async def handle(event_type, context)` calls `MemoryService` in-process. Hermes is the only harness where the hook is Python, so it skips process startup entirely. MCP via `hermes mcp add`.

*OpenClaw* — MCP through `mcp.servers` in `~/.openclaw/openclaw.json` ships this cycle. The automatic path needs a TypeScript plugin registering `before_prompt_build` (returns `appendContext`) and `llm_output`; whether that plugin may spawn `popoto-memory` is unverified and is resolved by spike-6 at build time. If it cannot, OpenClaw ships MCP-only with the gap stated in its guide, and the plugin becomes a follow-up issue.

**Honest per-harness capability table**, published in the docs and kept accurate:

| Harness | Auto-inject | Auto-capture | Discretionary tools | Setup |
|---|---|---|---|---|
| Claude Code | yes (`UserPromptSubmit`) | yes (`Stop`, async) | yes (MCP) | 2 commands, or 8 config lines |
| Codex | yes (`UserPromptSubmit`) | yes (`Stop`) | yes (MCP) | config + feature flag + `/hooks` trust |
| Hermes | yes (`pre_llm_call`) | yes (`post_llm_call`) | yes (MCP) | 2-file hook directory |
| OpenClaw | plugin required | plugin required | yes (MCP) | MCP config now; plugin pending spike-6 |

## Failure Path Test Strategy

### Exception Handling Coverage

`SubconsciousMemory` swallows broadly (`inject_context` at `subconscious_memory.py:197`, `extract_memories` at `:280`, `report_outcomes` at `:413`, each `logger.warning` then continues). That posture is right for a hook — a memory failure must never break the user's turn — but it must be *observable*, which is exactly what those call sites are not from inside a harness.

- [ ] Every `except` in `popoto.integrations` logs to a file under `POPOTO_MEMORY_LOG` (default `~/.popoto/memory.log`) **and** increments a counter readable by `popoto-memory doctor`. Silent failure is the failure mode that makes users conclude "memory doesn't work," and there is no console to print to.
- [ ] Test: Redis down → read hook exits 0, emits no `additionalContext`, writes one log line, and the turn proceeds unmodified.
- [ ] Test: Redis down → write hook exits 0, logs, drops the turn. No retry queue in v1.
- [ ] Test: malformed JSON on stdin → exit 0, log, no stdout. Codex treats stdout that starts with `{` but fails to parse as a hook failure, so a partial write must never occur — build the whole JSON string, then write once.
- [ ] Test: `doctor` reports non-zero failure counters after each of the above.

### Empty/Invalid Input Handling

- [ ] Empty or whitespace-only `prompt` → no assembly attempted, no stdout, exit 0.
- [ ] Empty or whitespace-only `last_assistant_message` → nothing written (`extract_memories` already returns `[]`; assert it at the hook boundary too).
- [ ] Missing `session_id` → fall back to a cwd-derived id; assert outcome reporting degrades to a no-op rather than raising.
- [ ] Assembly returns zero records → emit no `additionalContext` key at all, rather than an empty context block (an empty "Relevant context:" header is worse than silence).
- [ ] Assembled context exceeding the token budget → truncated at a record boundary, never mid-record.

### Error State Rendering

- [ ] `popoto-memory doctor` is the user-visible error surface: Redis reachability, resolved model and **effective retrieval mode** (surfacing #409's query-blind footgun), record count, failure counters, last 5 log lines, and measured hook round-trip. Test each degraded state renders a specific, actionable line.
- [ ] MCP tool errors return an MCP error result with a readable message, never a traceback string as tool output.
- [ ] Test that a `doctor` run against a model resolving to composite mode prints an explicit query-blind warning.

## Test Impact

No existing tests are affected — this is a greenfield package with no prior coverage, and every change outside it is additive (`RawTurnExtractionProvider` is a new class; the `[project.scripts]` table is new).

New coverage required:

- [ ] `tests/test_integrations_service.py` — CREATE: `MemoryService` against real Redis on DB 15 via the pytest plugin.
- [ ] `tests/test_integrations_hooks.py` — CREATE: golden-payload round trip. **Fixtures are captured from real harness runs, not hand-written**, and stored under `tests/fixtures/harness_payloads/` with the capture command recorded in the file. Hand-written fixtures would test our reading of the docs rather than the harnesses.
- [ ] `tests/test_integrations_mcp.py` — CREATE: in-process MCP client, tool listing and each tool's contract. Includes a name-freeze assertion listing the four tool names literally, so a rename breaks a test rather than users' configs.
- [ ] `tests/test_raw_turn_extraction.py` — CREATE: verbatim single-fact behavior; explicitly assert no sentence splitting.
- [ ] `tests/test_integrations_latency.py` — CREATE, marked `slow`: subprocess-level p95 of `popoto-memory hook` on the read path against the 400 ms budget.

## Rabbit Holes

- **Building a memory daemon before measuring startup.** A persistent server is the obvious fix for process startup and the wrong first move — it is a second process users must manage, supervise, and debug. Measure first; the HTTP-hook escape hatch already exists in Claude Code if the number is bad.
- **Writing an OpenClaw TypeScript plugin before confirming shell-out.** Time-boxed spike, then decide. A Node reimplementation of the memory path is a second implementation of the core and is out of scope at any price.
- **Chasing MCP `sampling` or `elicitation` as an injection mechanism.** They are server-initiated model calls, not per-turn context injection, and they do not make MCP subconscious. Hooks solve this; do not relitigate it.
- **Supporting every hook event each harness offers.** `PreCompact` memory checkpointing, `SessionStart` briefings, and `PostToolUse` capture are all plausible and all additive later. v1 is two events: one read, one write.
- **Perfecting `agent_id` scoping.** cwd basename is coarse and will occasionally collide. It is also understandable and overridable by env var. A project-identity scheme is a separate design problem.
- **Making the Claude Code plugin do more because plugins can.** Skills, agents, monitors, and LSP entries are all available in the manifest. Ship hooks plus MCP; a memory plugin that installs a subagent is a different product.
- **Re-tuning `score_weights` for coding-agent traffic.** Tempting and unmeasured. Ship the benchmarked weights; a harness-traffic sweep is a benchmark issue, not an integration issue.

## Risks

### Risk 1: The default write path ships the measured-worst ingestion arm
**Impact:** The obvious implementation calls `extract_memories()`, which defaults to `HeuristicExtractionProvider`. #489 measured that at 0.2078 judged accuracy versus 0.3636 for raw ingestion. Because a hook fires on every turn, the harness surface would generate more memories through the weakest path than every other Popoto usage combined, and the recall quality of that corpus is precisely what a new user judges the product on. It would also contradict the sibling docs plan, which teaches raw ingestion as the measured-best write path. **This is the biggest risk in the plan.**
**Mitigation:** `RawTurnExtractionProvider` is built first and is the harness default (`POPOTO_MEMORY_INGEST=raw`). A Verification-table grep asserts no call to a bare `extract_memories()` without an explicit provider anywhere in `src/popoto/integrations/`. Selecting `heuristic` or `llm` prints the #489 measured cost on first use. The write path is exercised end-to-end in the zero-key example so the ingestion arm is visible, not implicit.

### Risk 2: Startup latency makes the read hook a felt tax on every turn
**Impact:** Redis costs ~5 ms; a cold Python process is the unknown. If the read hook adds a visible pause before every response, users uninstall regardless of recall quality, and the "RAM-speed memory" claim reads as false from the first turn.
**Mitigation:** Measured budget (400 ms p95) enforced by a test in the Verification table, lazy imports throughout, write hook marked `"async": true`, documented escape hatch to `"type": "http"` + `popoto-memory serve`. The measured number is published in the guide — it is a differentiator against LLM-extraction memory systems that cannot meet a per-turn budget at all.

### Risk 3: Harness hook contracts are young and will move
**Impact:** Codex hooks sit behind a feature flag and gained per-hash trust review across three releases this year. Claude Code's event roster has grown past thirty events. A schema change silently breaks injection, and the failure is invisible — the agent simply stops remembering.
**Mitigation:** Fixtures captured from real harness runs (not hand-written), with the capture command recorded so re-capture is one line. `popoto-memory doctor` reports last-successful-injection timestamps so a break is visible without reading logs. Guides pin the harness versions verified against. The normalized-payload boundary means a schema change touches one adapter function.

### Risk 4: Public names are effectively permanent once installed
**Impact:** Four MCP tool names, one CLI name, and six env vars end up in users' config files, blog posts, and other projects' docs. Renaming later breaks installs silently. Mem0 already demonstrates the cost of two naming conventions for one product.
**Mitigation:** Names frozen at the first PM check-in, before any guide is written. A name-freeze test asserts the four tool names literally. Single `memory_*` prefix, one convention, published once.

### Risk 5: Unbounded per-turn writes grow the corpus without bound
**Impact:** Every turn in every session writes a record. A heavy Claude Code user generates thousands per week; retrieval quality degrades as the corpus fills with routine turns, and Redis memory grows unattended.
**Mitigation:** `agent_id` defaults to the cwd basename so corpora are per-project rather than global. The reference wiring composes `MemoryLifecycle.tick()` into `SessionEnd` so decay and tombstoning run without a separate cron. `doctor` reports record count and growth rate. Documented `POPOTO_MEMORY_ENABLED=0` kill switch.

### Risk 6: "< 10 lines of config" is not honestly true on three of four harnesses
**Impact:** The acceptance criterion is met on Claude Code and overstated elsewhere — Codex needs a feature flag plus a trust review, OpenClaw needs a plugin for the automatic half. Overstating it in the docs contradicts the radical-transparency posture the sibling plan is built on, on the exact page a skeptic reads first.
**Mitigation:** The per-harness capability table above ships in the docs and states each harness's real setup cost. The acceptance criterion is read as "at least one harness," which is what #515 says. The Codex trust step is presented as a security feature, because it is.

## Race Conditions

### Race 1: Read hook and write hook for the same turn overlap
**Location:** `src/popoto/integrations/hooks.py` (pending-keys handoff), `service.py` (outcome reporting)
**Trigger:** The write hook runs `"async": true`. A fast user submits the next prompt before the previous turn's write completes, so `UserPromptSubmit` for turn N+1 assembles while `Stop` for turn N is still writing.
**Data prerequisite:** The pending record keys written by the read path must exist before the write path reports outcomes against them.
**State prerequisite:** Turn N's outcome report must not overwrite turn N+1's pending set.
**Mitigation:** Pending keys are stored per `(session_id, turn_marker)`, not per session, with a short TTL. The write path reads and deletes its own turn's set atomically (`GETDEL`). A missing set degrades to "no outcome reported," never to reporting against another turn's records. Test with an interleaved read/write/read/write sequence.

### Race 2: Concurrent harness sessions in the same project write the same corpus
**Location:** `service.py` capture path
**Trigger:** Two Claude Code windows, or a session plus subagents (`SubagentStop` fires per subagent), in one cwd — the same `agent_id`.
**Data prerequisite:** None; writes are independent records.
**State prerequisite:** `AutoKeyField` must not collide across processes, and index updates must stay consistent.
**Mitigation:** Popoto's existing per-record atomic index maintenance covers this (`atomic_index_maintenance_lua.md`); the integration adds no cross-record invariant. Covered by an existing-primitives test at the integration level (two processes writing concurrently, assert index consistency), not by new locking.

### Race 3: `doctor` reads counters while a hook writes them
**Location:** failure-counter path in `service.py`
**Trigger:** `doctor` run during an active session.
**Mitigation:** Counters are Redis `INCR`, atomic by construction. `doctor` reads are advisory; no consistency guarantee is claimed or needed.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #513] The default `Memory` model, the query-blind resolution warning, and the content-only injection format. This plan **consumes** them and must not define a competing model. Anti-criterion in Verification.
- [SEPARATE-SLUG #511] Docs-site repositioning and nav restructure. This plan adds guide pages; where they sit in the nav is #511's call.
- [SEPARATE-SLUG #512] PyPI metadata and README repositioning. The `popoto[mcp]` extra will want a README mention; #512 owns that copy.
- [SEPARATE-SLUG #514] LoCoMo scoring-defect fix. No guide in this plan cites a LoCoMo number until it lands.
- [EXTERNAL] Submitting the plugin to the `claude-community` marketplace. Requires an Anthropic in-app submission form and human review; the repo-hosted marketplace works without it.
- [EXTERNAL] Live acceptance runs on Codex, Hermes, and OpenClaw. Each needs an installed harness, an account, and a driven turn on a machine an agent cannot reach. Automated coverage stops at captured-fixture round trips; the maintainer signs off on live behavior.
- [ORDERED] Publishing `popoto[mcp]` to PyPI. Every wiring guide's `pip install popoto[mcp]` is dead until a release ships, and releases are maintainer-gated via `/do-deploy`. Guides carry an install note until then.
- [SEPARATE-SLUG #486] Comparative recall claims against Mem0, Zep, or Supermemory memory in a harness. Adapters do not exist; the guides describe architecture, never relative quality.

Deliberately **in** scope, not deferred: the `RawTurnExtractionProvider`, all four wiring guides (thin where the harness is thin), the zero-key runnable example, and the Claude Code plugin plus its marketplace manifest.

## Update System

- New `popoto[mcp]` extra must be added to the worktree install line in `CLAUDE.md`; the documented full-dev install becomes `.[dev,embeddings,benchmark,mcp]`.
- `scripts/ci-local.sh` gains the integration tests. The latency test is `slow`-marked and excluded from `--fast`.
- Existing installations: purely additive. No migration. Users on `pip install popoto` keep working; the extra is opt-in.
- The Claude Code plugin is versioned by `plugin.json`; bumping it is a release-time step in `/do-deploy` so plugin and package versions do not drift.

## Agent Integration

This plan *is* the agent-integration surface for Popoto, so the section is load-bearing rather than a formality.

- **MCP surface**: `popoto-memory mcp`, stdio, four tools (`memory_search`, `memory_save`, `memory_feedback`, `memory_status`). Registered per harness through that harness's own MCP config — Claude Code `.mcp.json`, Codex `[mcp_servers.popoto-memory]`, Hermes `hermes mcp add`, OpenClaw `mcp.servers`.
- **Hook surface**: `popoto-memory hook`, one command string for Claude Code and Codex; a Python `handler.py` for Hermes; a TypeScript plugin for OpenClaw pending spike-6.
- **Entry point**: `[project.scripts] popoto-memory = "popoto.integrations.cli:main"` in `pyproject.toml`. The repo has no `[project.scripts]` table today, so this is the first one.
- **Integration tests that verify the agent can actually invoke the capability**: an in-process MCP client lists tools and calls each one (`tests/test_integrations_mcp.py`); captured real-harness payloads drive the hook end to end (`tests/test_integrations_hooks.py`). Neither substitutes for the maintainer's live-harness pass, which is the [EXTERNAL] No-Go above.
- **Anti-goal**: the model must never need to call an MCP tool for recall or capture to happen. If a build task can only make memory work by instructing the model to call `memory_search`, that task has failed its acceptance.

## Documentation

### Feature Documentation
- [ ] `docs/features/harness-integration.md` — the architecture: why hooks recall and MCP tools search, the per-harness capability table, the config reference, `doctor` output.
- [ ] `docs/guides/harness-claude-code.md` — reference wiring guide, plugin and manual paths, verification steps, measured latency.
- [ ] `docs/guides/harness-codex.md` — including the feature flag and the `/hooks` trust review, framed as the security feature it is.
- [ ] `docs/guides/harness-hermes.md` — `HOOK.yaml` + `handler.py`, the in-process Python advantage.
- [ ] `docs/guides/harness-openclaw.md` — MCP config; automatic-injection status stated honestly per spike-6.
- [ ] `docs/features/llm-memory-extraction.md` — add `RawTurnExtractionProvider` to the provider table and name it the harness default with the #489 rationale.

### External Documentation Site
- [ ] Nav placement coordinated with #511 (an "Add memory to your harness" destination).
- [ ] `mkdocs build --strict` passes.
- [ ] Guides carry the "requires a published `popoto[mcp]` release" note until the [ORDERED] release lands.

### Inline Documentation
- [ ] `popoto/integrations/__init__.py` module docstring states the hooks-recall / tools-search split so the next reader does not "simplify" recall into an MCP tool.
- [ ] `RawTurnExtractionProvider` docstring cites #489's measured numbers.
- [ ] Every public `MemoryService` method documented; `mkdocstrings` picks the package up in the API reference.

## Success Criteria

- [ ] `pip install popoto[mcp]` then an 8-line `settings.json` block gives working subconscious memory in Claude Code: memories injected before a turn, the turn captured after, both verified live by the maintainer
- [ ] The same `popoto-memory hook` command string works unmodified in Codex (feature flag and trust review documented, not hidden)
- [ ] Hermes hook directory and OpenClaw MCP config verified against captured payloads; OpenClaw automatic-injection status resolved by spike-6 and stated honestly
- [ ] `examples/harness_memory/` runs against local Redis with zero API keys and no harness installed, exercising assemble → inject → capture → report
- [ ] Default write path is raw-turn ingestion; no code path reaches `HeuristicExtractionProvider` without explicit opt-in
- [ ] Retrieval configuration is #513's default model with `score_weights={"relevance": 1.0}`; the `max_items`/`max_tokens` divergence is documented with its reason
- [ ] Read-hook p95 under 400 ms, measured and published
- [ ] Whole flow passes against Valkey with no Redis modules
- [ ] Four wiring guides published; the per-harness capability table is accurate
- [ ] Core `pip install popoto` still resolves 3 packages with zero API keys — the extra does not leak into core
- [ ] MCP tool names frozen and asserted by test
- [ ] Tests pass (`/do-test`); docs updated (`/do-docs`); `mkdocs build --strict` passes

## Team Orchestration

### Team Members

- **Builder (core service)** — Name: `integration-core-builder` — Role: `MemoryService`, config resolution, `RawTurnExtractionProvider` — Agent Type: builder — Domain: Redis/Popoto data — Resume: true
- **Builder (hooks + CLI)** — Name: `hook-builder` — Role: hook adapter, CLI, `doctor`, latency work — Agent Type: builder — Domain: MCP-tool/API integration — Resume: true
- **Builder (MCP server)** — Name: `mcp-builder` — Role: stdio MCP server and its tests — Agent Type: builder — Domain: MCP-tool/API integration — Resume: true
- **Builder (harness assets)** — Name: `harness-assets-builder` — Role: `plugins/` tree, marketplace manifest, example — Agent Type: builder — Resume: true
- **Validator (subconscious invariant)** — Name: `subconscious-validator` — Role: prove recall and capture happen with zero model election; prove no bare `extract_memories()` — Agent Type: validator — Resume: true
- **Validator (latency + Valkey)** — Name: `runtime-validator` — Role: p95 measurement, Valkey run, install-weight check — Agent Type: validator — Resume: true
- **Documentarian** — Name: `harness-documentarian` — Role: four guides plus the feature doc — Agent Type: documentarian — Resume: true

## Step by Step Tasks

### 0. Confirm #513 has landed
- **Task ID**: gate-513
- **Depends On**: none
- **Assigned To**: integration-core-builder
- **Agent Type**: builder
- **Parallel**: false
- Run `python -c "from popoto.recipes import DefaultMemory"`. If it fails, **stop and report** — do not vendor a substitute model.

### 1. Raw-turn extraction provider
- **Task ID**: build-raw-extraction
- **Depends On**: gate-513
- **Validates**: `tests/test_raw_turn_extraction.py` (create)
- **Informed By**: spike-4 (heuristic 0.2078 vs raw 0.3636, #489)
- **Assigned To**: integration-core-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `RawTurnExtractionProvider` to `popoto.extraction`, returning the text verbatim as one `ExtractedFact`; export it; cite #489 in the docstring

### 2. MemoryService + config
- **Task ID**: build-service
- **Depends On**: build-raw-extraction
- **Validates**: `tests/test_integrations_service.py` (create)
- **Informed By**: spike-5 (return a context string; never mutate a system message)
- **Assigned To**: integration-core-builder
- **Agent Type**: builder
- **Parallel**: false
- `assemble` / `capture` / `feedback` / `status`; env + cwd config resolution; per-turn pending-key handoff with TTL and `GETDEL`; failure counters and file logging

### 3. Hook adapter + CLI
- **Task ID**: build-hooks-cli
- **Depends On**: build-service
- **Validates**: `tests/test_integrations_hooks.py` (create), `tests/test_integrations_latency.py` (create)
- **Informed By**: spike-1 (one executable serves Claude Code and Codex), spike-2 (`last_assistant_message`, no transcript parsing), spike-3 (per-harness setup reality)
- **Assigned To**: hook-builder
- **Agent Type**: builder
- **Parallel**: false
- `popoto-memory hook|mcp|doctor|demo`; dispatch on `hook_event_name`; lazy imports; `[project.scripts]` entry point; capture real harness payload fixtures with the capture command recorded in each file; build stdout as one string and write once

### 4. MCP server
- **Task ID**: build-mcp
- **Depends On**: build-service
- **Validates**: `tests/test_integrations_mcp.py` (create)
- **Assigned To**: mcp-builder
- **Agent Type**: builder
- **Parallel**: true (with build-hooks-cli)
- Four tools with frozen names; `mcp` imported lazily; `popoto[mcp]` extra; name-freeze test; errors as MCP error results, never tracebacks

### 5. Harness assets + zero-key example
- **Task ID**: build-assets
- **Depends On**: build-hooks-cli, build-mcp
- **Assigned To**: harness-assets-builder
- **Agent Type**: builder
- **Parallel**: false
- `plugins/{claude-code,codex,hermes,openclaw}/`, root `.claude-plugin/marketplace.json`, `examples/harness_memory/`; compose `MemoryLifecycle.tick()` into the session-end recipe

### 6. spike-6: OpenClaw plugin shell-out (time-boxed, 30 min)
- **Task ID**: spike-openclaw
- **Depends On**: build-hooks-cli
- **Assigned To**: harness-assets-builder
- **Agent Type**: builder
- **Parallel**: true
- Determine whether an OpenClaw plugin may spawn a subprocess. Yes → thin plugin calling `popoto-memory hook`. No → MCP-only for OpenClaw, state the gap in its guide, file a follow-up. Either way, record the finding in this plan's Spike Results.

### 7. Validate the subconscious invariant
- **Task ID**: validate-subconscious
- **Depends On**: build-assets
- **Assigned To**: subconscious-validator
- **Agent Type**: validator
- **Parallel**: false
- Prove recall and capture occur with zero model tool election; prove no bare `extract_memories()` in `integrations/`; prove no second `Memory` model is defined; prove no system-message mutation on the hook path

### 8. Validate runtime
- **Task ID**: validate-runtime
- **Depends On**: build-assets
- **Assigned To**: runtime-validator
- **Agent Type**: validator
- **Parallel**: true
- Read-hook p95 vs the 400 ms budget; full flow against Valkey; core install still 3 packages / zero keys; report the measured latency for publication

### 9. Documentation
- **Task ID**: document-harness
- **Depends On**: validate-subconscious, validate-runtime, spike-openclaw
- **Assigned To**: harness-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- Feature doc, four guides, capability table, extraction-provider table update; coordinate nav with #511; carry the pre-release install note

### 10. Final validation
- **Task ID**: validate-all
- **Depends On**: all
- **Assigned To**: runtime-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table; walk the Claude Code install from a clean venv; confirm every Success Criterion; report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -q -m "not slow"` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Types clean | `mypy src/popoto/integrations/` | exit code 0 |
| CLI entry point installed | `popoto-memory --help` | exit code 0 |
| Doctor runs against local Redis | `popoto-memory doctor` | output contains `redis` |
| Zero-key example runs | `python examples/harness_memory/verify.py` | exit code 0 |
| Read-hook latency budget | `pytest tests/test_integrations_latency.py -q` | exit code 0 |
| MCP tool names frozen | `pytest tests/test_integrations_mcp.py -k name_freeze -q` | exit code 0 |
| Harness fixtures are captured, not invented | `grep -rL "captured-from:" tests/fixtures/harness_payloads/` | output does not contain `.json` |
| **Anti-criterion (Risk 1):** no bare heuristic extraction | `pytest tests/test_integrations_service.py -k subconscious_memory_construction -q` | exit code 0 |
| **Anti-criterion (Risk 1):** raw ingestion is the default | `grep -c "RawTurnExtractionProvider" src/popoto/integrations/service.py` | output > 0 |
| **Anti-criterion (#513):** no second Memory model | `grep -rn "class .*Memory.*(.*Model)" src/popoto/integrations/` | match count == 0 |
| **Anti-criterion (spike-5):** no system-message mutation | `grep -rn "\"role\": \"system\"\|role.*system" src/popoto/integrations/` | match count == 0 |
| **Anti-criterion:** core install unaffected | `grep -c "mcp" pyproject.toml` requires the hit to be under `[project.optional-dependencies]` only; check core deps | `python -c "import tomllib,sys;d=tomllib.load(open('pyproject.toml','rb'));sys.exit(0 if not any('mcp' in x for x in d['project']['dependencies']) else 1)"` → exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Capability table published | `grep -c "Auto-inject" docs/features/harness-integration.md` | output > 0 |
| No unmeasured competitor claims | `grep -rin "faster than mem0\|better than zep\|beats mem0" docs/guides/harness-*.md` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| | | *(Not yet run.)* | | |

---

## Open Questions

The three questions #515 raised for planning are answered above; the recommendations and their reasoning are restated here for sign-off.

1. **Integration surface — MCP server, per-harness hook scripts, or both, and which ships first?**
   **Recommendation: both, in one artifact, with hooks carrying the subconscious loop and shipping first.** They are not alternatives: MCP tools are agent-elected, so an MCP-only shipment is instructed memory. The finding that makes "both" cheap is spike-1 — Claude Code and Codex have converged on the same hook contract, so a single `popoto-memory hook` executable covers both harnesses and the MCP server is a second entry point over the same `MemoryService`. Ship order: `RawTurnExtractionProvider` → `MemoryService` → hook adapter and CLI → MCP server → harness assets. Confirm?

2. **Where does it live: `src/popoto/mcp/`, a separate package, or examples-only?**
   **Recommendation: in-repo at `src/popoto/integrations/`, behind a `popoto[mcp]` extra, with a `popoto-memory` console entry point; harness assets at `plugins/` in the repo root.** Not `mcp/` — the hooks are the more important half and are not MCP, and the name would mislead the next reader into treating the MCP server as the product. Not a separate package: Mem0 and Graphiti both split theirs out and pay in setup friction (Docker, Postgres, Qdrant, or a repo clone), while Basic Memory's in-package console entry point with the plugin in the same repo is the lowest-friction prior art. Not examples-only: an example cannot satisfy "< 10 lines of config." The extra keeps the core install at 3 packages and zero API keys; the hook path works without `mcp` installed at all. Confirm?

3. **Which harness gets the reference wiring guide first?**
   **Recommendation: Claude Code.** It has the richest verified contract (`UserPromptSubmit` + `additionalContext` for injection, `Stop` + `last_assistant_message` for capture, `"async": true`, and `"type": "http"` as a latency escape hatch); it is the only harness with a packaged two-command install and a marketplace distribution path, which is the inbound-funnel point of #515; Codex's identical hook schema means the reference transfers for near-zero marginal cost, so first harness buys two; the maintainer uses it daily, so dogfooding is free; and it is where the competitive set already lives — Mem0, Supermemory, and Basic Memory all ship Claude Code plugins, all requiring a cloud API key, against which local Redis and zero keys is the sharpest available contrast. Confirm?

4. **One question this plan raises that #515 did not:** the harness injection budget diverges from the benchmarked configuration. Scoring stays benchmarked (`{"relevance": 1.0}`, lexical/BM25), but `max_items` drops from 20 to 5 and `max_tokens` to 800, because a coding harness contests context on every turn and Codex caps injected context at 2500 tokens. The alternative is to ship `max_items=20` for strict benchmark fidelity and let users tune down. Recommendation is the smaller default with the divergence stated in the guides. Confirm, or hold to 20?
