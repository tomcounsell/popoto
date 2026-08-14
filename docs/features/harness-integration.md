# Harness Integration

Subconscious memory for Claude Code, Codex, Hermes, and OpenClaw, backed by
your own Redis or Valkey. No API keys, no hosted service, no schema to write.

```bash
pip install 'popoto[mcp]'
popoto-memory doctor
```

Then paste your harness's config block. The next turn silently carries
relevant memories; the turn after that remembers what was just said.

!!! note "Pre-release"
    `popoto[mcp]` is not on PyPI yet. Until a release ships, install from a
    checkout: `pip install -e '.[mcp]'`. The hook path needs only
    `pip install popoto`; the extra is for the MCP server.

## Hooks recall and capture; MCP tools search, save, and correct

That split is the whole design, and it is the part worth understanding
before anything else.

Subconscious memory means the memory layer runs on **every** turn, whether
or not the model asks for it. A plain MCP tool server cannot deliver that,
because MCP tools are agent-elected: the model calls a tool when it decides
to. Memory then becomes something the model chooses, which is instructed
memory wearing a subconscious label.

Vendors who bet the other way say so. Supermemory, explaining why it shipped
a plugin on top of its MCP server: "We cannot control when claude code
chooses to run the tools. This means that we have no control / data point to
learn things from, and a memory system is only good if there's things to
recall later." Mem0's troubleshooting page tells users that if memories are
not being captured, they installed the MCP-only variant and need the
marketplace plugin, "since MCP-only installs require manual memory
operations."

Every harness here has a deterministic, model-independent surface that fires
per turn. That surface, not MCP, carries recall and capture. MCP remains the
portable core for the discretionary half: searching on demand, saving
explicitly, correcting a memory that turned out wrong.

## Per-harness capability

Auto-inject means memories reach the model with no tool call. Auto-capture
means the turn is stored with no tool call.

| Harness | Auto-inject | Auto-capture | Discretionary tools | Setup |
|---|---|---|---|---|
| Claude Code | yes (`UserPromptSubmit`) | yes (`Stop`, async) | yes (MCP) | 2 commands, or 8 config lines |
| Codex | yes (`UserPromptSubmit`) | yes (`Stop`) | yes (MCP) | config + feature flag + `/hooks` trust review |
| Hermes | yes (`pre_llm_call`) | yes (`post_llm_call`) | yes (MCP) | 2-file hook directory |
| OpenClaw | no, plugin required | no, plugin required | yes (MCP) | MCP config only |

Verification is not uniform either, and the difference matters more than the
capability table:

| Harness | Contract verified against |
|---|---|
| Claude Code | a live `claude` 2.1.220 run; payloads captured and committed as test fixtures |
| Codex | the `codex-cli` 0.144.4 binary's own hook-input schema, not a live turn |
| Hermes | vendor documentation only |
| OpenClaw | vendor documentation only |

Guides: [Claude Code](../guides/harness-claude-code.md) (the reference),
[Codex](../guides/harness-codex.md), [Hermes](../guides/harness-hermes.md),
[OpenClaw](../guides/harness-openclaw.md).

## What happens on a turn

```
user types
    |
    v  pre-model hook              popoto-memory hook
assemble from Redis
    |
    v  context appended to the USER turn
model
    |
    v  post-model hook, async      popoto-memory hook
turn saved verbatim, outcomes reported
    |
    v
next turn's assembly is better
```

**Read path.** The harness fires its pre-model event with the user's prompt.
The adapter normalizes the payload to `(event, query_text, session_id, cwd)`,
`MemoryService.assemble()` runs `ContextAssembler` over
[`DefaultMemory`](agent-memory.md) on the lexical/BM25 path, and the
formatted block goes back in the harness's own response shape:
`{"hookSpecificOutput": {"additionalContext": "..."}}` for Claude Code and
Codex, `{"context": "..."}` for Hermes, `{"appendContext": "..."}` for
OpenClaw. The selected record keys are queued under the session so the
following write event can report outcomes against exactly those records.

**Write path.** The harness fires its post-model event carrying the
assistant's final text. The turn is saved through
`RawTurnExtractionProvider`, one verbatim record. The queued keys from the
read path are popped and reported through `ObservationProtocol` with
outcome `used`, which confirms the staged read and auto-resolves
predictions — `_apply_used()` (`src/popoto/fields/observation.py:391`)
explicitly does not touch `ConfidenceField`, `CyclicDecayField`, or
`DecayingSortedField`. Outcome `acted`, which does raise confidence and
affect decay, is reserved for an explicit, discretionary `memory_feedback`
MCP tool call — the automatic hook path never emits it. Nothing is written
to stdout, and on Claude Code this hook is configured `"async": true` so it
never sits on the turn's critical path.

**Known gap: the read→write handoff is a session-wide FIFO, not a turn ID.**
`MemoryService` queues selected record keys per `session_id` as a plain
list and pops one entry per write event; it does not key the entry on a
turn identifier, even though Claude Code and Codex fixtures both carry one
(`prompt_id` / `turn_id`). A read whose paired write event never fires — an
aborted turn, a crashed session — leaves its entry in the list and shifts
every later push/pop pairing by one, and a `SubagentStop`-configured
session pops one queued entry per subagent against a single earlier read.
The failure mode is misattributed outcome reporting (confidence/decay
adjustments land on the wrong turn's records), not a crash, and it is
bounded by `MAX_PENDING_TURNS` (32) per session. Tracked as follow-up work
in #574; not fixed in this PR.

Neither path parses a transcript file. Every harness supplies the assistant's
final text on the event itself, so a change to the JSONL format cannot break
capture.

### Injection lands in the user turn, never the system prompt

`SubconsciousMemory.inject_context()`, the library API, appends to
`messages[0]`, which invalidates a cached system prefix on every turn. The
hook path does not do that: Claude Code `additionalContext`, Hermes
`pre_llm_call` context, and OpenClaw `appendContext` all land in the user
turn. Hermes documents the choice as caching-motivated. `MemoryService`
returns a context *string* and never touches a message array, so where the
text goes is the harness's decision.

## The write path is raw ingestion, on purpose

Issue #489 evaluated LLM extraction, heuristic sentence-splitting, and raw
turn ingestion on the same slice. **Every extraction arm lost to raw
ingestion on judged accuracy: heuristic 0.2078 against raw 0.3636.**

A hook fires on every turn, so the harness surface would otherwise generate
more memories through the measured-worst write path than every other Popoto
usage combined, and the quality of that corpus is exactly what a new user
judges the product on. So
[`RawTurnExtractionProvider`](llm-memory-extraction.md) is the default here:
one turn in, one record out, stored verbatim.

`POPOTO_MEMORY_INGEST=heuristic` switches to sentence-splitting and logs
that measured cost the first time it is used.

## Configuration

Every variable is optional. The zero-configuration path is "local Redis or
Valkey on the default port, memories tagged with this project's agent id" —
see the scoping caveat on `POPOTO_MEMORY_AGENT_ID` below before wiring the
hook into more than one project on one database.

| Variable | Default | Notes |
|---|---|---|
| `POPOTO_MEMORY_URL` | `REDIS_URL`, else `redis://localhost:6379/0` | Valkey URLs are identical |
| `POPOTO_MEMORY_AGENT_ID` | basename of the working directory | Tags writes and is honored as a read filter on the composite-score retrieval path. The shipped default is the lexical/BM25 path, which does **not** yet filter by it ([#576](https://github.com/tomcounsell/popoto/issues/576)) — a project's memories can be retrieved by another `agent_id` on the same database. Not a project-isolation boundary today; use a distinct `POPOTO_MEMORY_URL` database per project for that instead |
| `POPOTO_MEMORY_MAX_ITEMS` | `5` | Diverges from the benchmark; see below |
| `POPOTO_MEMORY_MAX_TOKENS` | `800` | Under Codex's 2500-token `additionalContextLimit` |
| `POPOTO_MEMORY_INGEST` | `raw` | `raw` or `heuristic` |
| `POPOTO_MEMORY_ENABLED` | `1` | Kill switch that needs no config edit |
| `POPOTO_MEMORY_LOG` | `~/.popoto/memory.log` | Where swallowed errors land |

A malformed value falls back to its default rather than raising. A typo in a
harness config must not break a turn.

### The one deliberate divergence from the benchmarked configuration

Scoring is unchanged: `score_weights={"relevance": 1.0}` and the
lexical/BM25 retrieval path that `DefaultMemory` selects, exactly as
benchmarked.

`max_items` and `max_tokens` are not. The retrieval benchmark ran at
`max_items=20`, a reasonable budget for a question-answering evaluation and
the wrong one for a coding harness, where a turn fires every few seconds and
context is contested by file contents, tool output, and the system prompt.
Codex additionally caps injected context at 2500 tokens. The defaults here
are 5 items and 800 tokens. Raise them with the environment variables if
your turns are long and infrequent.

## Diagnosing

A hook has no console, so `popoto-memory doctor` is the error surface.

```
$ popoto-memory doctor
popoto-memory doctor

  status         enabled
  redis url      redis://localhost:6379/0
  redis          reachable, valkey 9.1.0, ping 4.91 ms
  agent id       my-project
  model          DefaultMemory
  retrieval      lexical (query-sensitive)
  ingest         raw
  budget         5 items / 800 tokens
  records        142
  hook read      1.71 ms (in-process)
  successes      assemble=88, capture=142
  failures       none
  last assemble  2026-08-07T18:41:03+00:00
  last capture   2026-08-07T18:41:07+00:00
  log            /Users/dev/.popoto/memory.log
```

What to read:

- **`retrieval`** should say `lexical (query-sensitive)`. If it says
  `composite -- QUERY-BLIND`, the prompt text is being ignored when ranking.
  See [Query-Blind Retrieval](../guides/query-blind-retrieval.md).
- **`last assemble` / `last capture`** are how a silent break becomes
  visible. A hook that stopped firing shows a stale timestamp, or `never`.
- **`failures`** counts swallowed exceptions per operation. Every one of
  them also wrote a line to the log, and the last five lines are printed
  underneath.
- **Redis unreachable** exits 1 and prints the command to fix it.

`popoto-memory doctor --json` emits the same data machine-readably.

## Failure behavior

A memory failure must never break a user's turn, so every path swallows and
continues. Swallowing silently is the failure mode that makes users conclude
memory does not work, so each swallowed exception both appends to
`POPOTO_MEMORY_LOG` and increments a Redis counter that `doctor` reads back.

| Situation | Behavior |
|---|---|
| Redis down, read hook | exit 0, no output, one log line, counter incremented |
| Redis down, write hook | exit 0, turn dropped, logged. No retry queue |
| Malformed JSON on stdin | exit 0, no output, logged as `hook_decode` |
| Empty prompt | no retrieval attempted, no output |
| Empty assistant message | nothing written |
| Missing `session_id` | recall still runs; outcome reporting degrades to a no-op |
| Nothing retrieved | no `additionalContext` key at all, rather than an empty header |

Output is built as one string and written once, because Codex treats stdout
that starts with `{` but fails to parse as a hook failure.

## MCP tools

Four tools, one `memory_` prefix, frozen. They end up in users' configs and
in other projects' docs, so a rename breaks installs silently; a test
asserts the four names literally.

| Tool | Purpose |
|---|---|
| `memory_search` | Find something specific mid-task that was not already injected |
| `memory_save` | Store a deliberate fact the turn text would not preserve |
| `memory_feedback` | Mark a memory `contradicted` or `acted`, adjusting its confidence without deleting it |
| `memory_status` | Connection, scope, retrieval mode, record count, failures |

Errors come back as MCP error results with a readable message, never a
traceback rendered as tool output.

An explicit `memory_search` does not consume the outcome-reporting slot of a
subconscious injection: a deliberate search is not a per-turn recall and must
not be reported as one.

## Latency

The read hook is synchronous and on the critical path of every turn. The
budget is 400 ms p95 end to end, enforced by
`tests/test_integrations_latency.py`.

Measured on an Apple M-series laptop, Python 3.12.13, redis 8.6.2 on
localhost, 50 records in scope, 25 subprocess invocations:

| Measure | Value |
|---|---|
| p50 | 193 ms |
| p95 | 200 ms |
| max | 211 ms |

Nearly all of that is Python interpreter startup, not Redis: in-process
assembly measures 1-2 ms. `popoto.integrations` keeps its module scope free
of heavy imports for this reason, and the write hook runs `"async": true` on
Claude Code so only the read path is ever on the critical path.

## Corpus growth

Every turn in every session writes a record. `agent_id` defaults to the
working directory's basename, so corpora are per-project rather than global,
and `doctor` reports the record count. `POPOTO_MEMORY_ENABLED=0` stops
writes without editing any harness config.

Popoto's decay primitives rank stale memories down over time.
[`MemoryLifecycle`](../recipes.md) can hard-delete them, but it requires a
`tier` KeyField that `DefaultMemory` does not declare, so it is not wired
into the harness path; running it needs a model of your own.

## Try it without a harness

```bash
popoto-memory demo                          # narrated tour of the loop
python examples/harness_memory/verify.py    # the same loop as assertions
```

Both run against local Redis or Valkey with no API keys and clean up after
themselves. See `examples/harness_memory/README.md`.

## Where the code lives

```
src/popoto/integrations/
    service.py      MemoryService: assemble, capture, feedback, search, correct, status
    config.py       environment and cwd resolution
    hooks.py        harness payload in, harness payload out
    mcp_server.py   stdio MCP server, four frozen tool names
    cli.py          popoto-memory: hook | mcp | doctor | demo
    demo.py         the zero-key loop
plugins/            declarative harness assets, one directory per harness
.claude-plugin/     marketplace manifest for the Claude Code plugin
```

`popoto.integrations` depends on `popoto.recipes` and `popoto.extraction`;
nothing in core depends on it. The core install is unchanged, and the hook
path does not require `mcp` at all.
