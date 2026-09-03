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

Claude Code `additionalContext`, Hermes `pre_llm_call` context, and OpenClaw
`appendContext` all land in the user turn. Hermes documents the choice as
caching-motivated. `MemoryService` returns a context *string* and never
touches a message array, so where the text goes is the harness's decision.

`SubconsciousMemory.inject_context()`, the library API, follows the same rule:
it appends after every existing message. Passing `position="system"` restores
the pre-1.9 placement in `messages[0]`, which invalidates a cached system
prefix on every turn — available for callers who need the block read as
system-level instruction, and priced accordingly. See
[Prompt Cache Efficiency](prompt-cache-efficiency.md).

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

### Credentials and off-the-record turns are dropped before ingestion

"Stored verbatim" is the point of raw ingestion and also its hazard: a hook
fires on every turn, and terminal turns contain pasted API keys. The
[never-record firewall](never-record-firewall.md) runs ahead of the write
path so that class of content never reaches Redis.

It is deterministic — regex and entropy, no model — and it runs at two
points. At the turn level, inside `SubconsciousMemory.extract_memories()`,
*before* the extraction provider is called: an off-the-record marker voids
the entire turn rather than a guessed span, and on the
`ClaudeExtractionProvider` path the text is never sent to the API at all. At
the record level, inside `Model.save()`, before serialization or any index
write. `DefaultMemory` — the model this harness writes through — carries the
mixin, so this is on by default with no configuration.

A drop leaves a content-free tombstone: a random id and a reason code, never
a fragment of the text. Read the tally with
`DefaultMemory.never_record_counts()`.

Two caveats worth knowing before you rely on it. Over-blocking is accepted by
design, so a long random-looking token in an otherwise useful turn can cost
you that memory. And the guarantee covers an enumerated class of shapes, not
"secrets" in general — the [feature
page](never-record-firewall.md#what-this-does-not-guarantee) lists the holes
explicitly. `POPOTO_NEVER_RECORD_DISABLE=1` turns it off entirely; note that
this is a core Popoto variable, not one of the `POPOTO_MEMORY_*` harness
variables below.

## Configuration

Every variable is optional, with one exception you will hit on a fresh
install: **the database must not be 0.** Name a database and the path is
"local Redis or Valkey on the default port, memories tagged with this
project's agent id".

| Variable | Default | Notes |
|---|---|---|
| `POPOTO_MEMORY_URL` | `REDIS_URL`, else `redis://localhost:6379/0` | Valkey URLs are identical. **Database 0 is refused** — see below |
| `POPOTO_MEMORY_ALLOW_DB0` | unset | Set to `1` to write agent memory to database 0 anyway. Deploy-level opt-in, deliberately an environment variable: a hook is a bare command string with nowhere to pass Python arguments |
| `POPOTO_MEMORY_AGENT_ID` | basename of the working directory | Tags writes and is honored as a read filter on every shipped retrieval path, lexical/BM25 included ([#576](https://github.com/tomcounsell/popoto/issues/576), fixed in 1.9.0). Before 1.9.0 the BM25 path ignored it and one project's memories could surface in another's turns on the same database |
| `POPOTO_MEMORY_MAX_ITEMS` | `5` | Diverges from the benchmark; see below |
| `POPOTO_MEMORY_MAX_TOKENS` | `800` | Under Codex's 2500-token `additionalContextLimit` |
| `POPOTO_MEMORY_INGEST` | `raw` | `raw` or `heuristic` |
| `POPOTO_MEMORY_ENABLED` | `1` | Kill switch that needs no config edit |
| `POPOTO_MEMORY_LOG` | `~/.popoto/memory.log` | Where swallowed errors land |

A malformed value falls back to its default rather than raising: a typo in a
harness config must not break a turn. The database number is the exception —
both a database-0 target and a URL with no database at all (`redis://host:6379/`,
which is *not* database 0) raise, because the alternative is writing your
memories somewhere you did not ask for.

### Database 0 is refused

`MemoryService` raises `Db0RefusedError` when the database it would write to
is 0. The error names the variable to change and suggests an empty database
it found on your server.

Database 0 is where a bare `redis-cli`, a scratch script, and most other
local tooling land by default, so it is the database most likely to already
hold something you care about — and the one most likely to be flushed by
someone debugging an unrelated problem. Popoto's own test plugin has always
refused it for the same reason; the library refusing it while steering users
into it was the contradiction ([#584](https://github.com/tomcounsell/popoto/issues/584)).

Pick any other database:

```bash
export POPOTO_MEMORY_URL=redis://localhost:6379/1
```

`Db0RefusedError` subclasses `ValueError`, so `doctor` prints it and exits 1
rather than tracebacking. If database 0 really is where this corpus belongs,
`POPOTO_MEMORY_ALLOW_DB0=1` opts in at deploy time.

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
  redis url      redis://localhost:6379/1
  url source     POPOTO_MEMORY_URL
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
- **`url source`** says which variable produced the URL — `POPOTO_MEMORY_URL`,
  `REDIS_URL`, or `default`. It is the fastest way to find out that the
  memories you are looking for went to a database you did not intend. Any
  password in the URL is redacted to `***`; `doctor` output ends up pasted
  into transcripts.
- **Redis unreachable** exits 1 and prints the command to fix it.
- **Database 0** exits 1 before connecting, with the refusal message.

`popoto-memory doctor --json` emits the same data machine-readably.

## Failure behavior

A memory failure must never break a user's turn, so every path swallows and
continues. Swallowing silently is the failure mode that makes users conclude
memory does not work, so each swallowed exception always appends a line to
`POPOTO_MEMORY_LOG` -- that's the reliable channel. It also tries to
increment a Redis counter that `doctor` reads back, but that write is
best-effort against the same client that just failed: when the failure is
Redis being unreachable, the counter does not get incremented, which is
exactly when `doctor` has nothing to read back anyway.

**An outage costs one attempt, not five.** The integration binds its
connection with a 1-second connect and socket timeout and no redis-py
retries (the default is 10), and once a `ConnectionError`/`TimeoutError` has
been recorded, every later Redis operation in that hook process is skipped
outright. The shipped Claude Code and Codex hook configs also carry an
explicit `"timeout": 10`. Against a server that accepts connections and
never answers, this is the difference between roughly 25 seconds on the
user's prompt and under 2.

| Situation | Behavior |
|---|---|
| Redis down, read hook | exit 0, no stdout, one log line, counter not incremented (same client is down). One connection attempt, ~1 s, not one per operation |
| Redis down, write hook | exit 0, turn dropped, logged. No retry queue |
| Database 0 with no opt-in | `Db0RefusedError` from `MemoryService` construction: `doctor` prints it and exits 1; the read hook logs it and exits 0, once per turn, until the URL is changed |
| Healthy Redis, empty corpus (fresh DB / first run) | exit 0, no stdout, one stderr line -- a BM25 advisory that it collected no query signal and fell back to composite (query-blind). Expected on a user's first turn after install; stderr goes quiet and stdout carries content once the corpus is seeded. See [Query-Blind Retrieval](../guides/query-blind-retrieval.md). |
| Malformed JSON on stdin | exit 0, no output, logged as `hook_decode` |
| Empty prompt | no retrieval attempted, no output |
| Empty assistant message | nothing written |
| Missing `session_id` | recall still runs; outcome reporting degrades to a no-op |
| Nothing retrieved | no `additionalContext` key at all, rather than an empty header |
| Turn dropped by the never-record firewall | nothing written, **no log line, no counter increment** -- a deliberate drop is not a failure. Counted in `$NR:DefaultMemory:counts` instead |

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

## Prompt cache efficiency

Latency is not the only per-turn cost. Injecting context on every turn puts
the memory layer in competition with the provider's prompt cache, and where
it writes decides whether that costs the injected tokens or the entire
prefix.

The integration is built to append at the tail and never above it.
`render_context()` in `hooks.py` emits into the user turn on all four
harnesses — `additionalContext` for Claude Code and Codex, `context` for
Hermes, `appendContext` for OpenClaw — precisely so the cached system prefix
survives across turns. The write path touches Redis only, so capture never
mutates anything the harness is reading.

The residual cost is that injected blocks accumulate: they cannot be removed
without invalidating everything behind them. Tuning `POPOTO_MEMORY_MAX_TOKENS`
and suppressing already-injected records are the two levers, and both stay
append-only.

See [Prompt Cache Efficiency](prompt-cache-efficiency.md) for the cost model,
the four rules, and how to measure it.

## Corpus growth

Every turn in every session writes a record. `agent_id` defaults to the
working directory's basename, so corpora are per-project rather than global,
and `doctor` reports the record count. `POPOTO_MEMORY_ENABLED=0` stops
writes without editing any harness config.

Popoto's decay primitives rank stale memories down over time, and since
1.9.0 `DefaultMemory` also **caps the corpus at 1000 records per
`agent_id`** (`Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT`). Past the
cap, each save deletes the stalest record by decay timestamp — a full
`delete()`, so every index is cleaned and nothing is recoverable
afterwards. Staleness is the `relevance` decay clock, so a memory that keeps
getting recalled or acted on stays; one nobody has touched goes first.

The cap exists because nothing on the default path evicted before: a
long-lived install grew one record per turn, forever. **If your corpus is
already above 1000 records for an agent, the first save after upgrading
starts deleting.** Check with `doctor` before you upgrade. To change the
cap, subclass `DefaultMemory` and set `_max_records_per_agent` (`0` or
`None` disables eviction entirely).

[`MemoryLifecycle`](../recipes.md) remains the tiered alternative, but it
requires a `tier` KeyField that `DefaultMemory` does not declare, so it is
not wired into the harness path; running it needs a model of your own.

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
