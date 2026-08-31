# Prompt Cache Efficiency

A subconscious memory layer writes into the model's context on every turn.
That puts it in direct competition with the provider's prompt cache for the
same bytes, and the outcome is not a rounding error: the difference between
a memory system that appends and one that rewrites is roughly the difference
between paying 10% and 100% of the input price on every turn of a long
session.

This page explains the mechanism, states the four rules that keep memory
cache-neutral, and points at where Popoto's recipes and integration already
follow them.

## Why position is the only variable that matters

Every provider caches the same way, whatever the vendor-specific naming:
the cache is keyed on an **exact token prefix**, bounded by a TTL, and
reusable only up to the first position where the current request diverges
from the cached one.

Two consequences follow, and they are the whole story.

**The cost of a change is not proportional to the size of the change.** It is
proportional to everything that follows it. Flipping one token at position
500 of a 100,000-token prompt costs 99,500 tokens of re-prefill. A memory
system that rewrites a single line near the top of the context is more
expensive than one that appends a full page at the bottom.

**So the only design question is where memory writes.** A prompt has three
writable regions, and their prices differ by orders of magnitude:

| Region | Contents | Cost of writing there |
|---|---|---|
| Preamble | system prompt, tool definitions, session-start files | the entire prefix, every turn |
| History | sealed prior turns and tool results | everything behind the edit |
| Tail | the current turn | the new tokens only |

Only the tail is cheap, and it is cheap for exactly one reason: nothing
follows it.

## The four rules

### 1. Recall appends at the tail, never anywhere else

Injected context must land inside the current user turn, after all sealed
history. This is the load-bearing rule — everything else here is a corollary.

Popoto's harness adapter encodes it directly. `render_context()` in
`popoto/integrations/hooks.py` emits `hookSpecificOutput.additionalContext`
for Claude Code and Codex, `context` for Hermes, and `appendContext` for
OpenClaw. All four are user-turn channels, and the function's docstring
names the reason: they "place the text in the user turn rather than the
system prompt, which is what keeps the cached system prefix intact across
turns."

The tempting alternative is to maintain one tidy "current memories" block
near the top of the context and rewrite it each turn. Do not. That design
invalidates the entire prefix on every turn where recall changes — which,
for a working memory system, is every turn.

### 2. The preamble is a snapshot, not a live view

Files loaded into the context at session start must not be re-read
mid-session. When they are only read once, writing to them is invisible to
the prefix, so capture costs nothing.

Popoto's write path holds this by construction: the `Stop` hook writes to
Redis and touches no file the harness is reading. Nothing in
`MemoryService.capture()` mutates prompt-visible state.

The rule has a price, and you should pay it knowingly: a memory written at
turn five does not reach any session-start file until the next session.
Freshness *within* a session is the recall path's job, never the preamble's.

!!! warning "The expensive version of this mistake"
    If your harness re-reads a memory index file on every turn, each capture
    becomes a full-prefix invalidation. A hundred-turn session with capture
    on every turn then pays for its entire context a hundred times over.
    Load once, at session start.

### 3. Memory never touches the tool block or system prompt

Those sit at position zero. Any per-turn variation there — a memory count in
a system line, a tool description that mentions recent recalls — costs the
entire context on every turn. There is no cheap version of this mistake.

Popoto's MCP server holds four frozen tool names with static descriptions
for this reason among others. Tool definitions that varied with memory state
would be the single most expensive thing the integration could do.

### 4. The injected block is a pure function of query and store

The rendered text must be reproducible from the same inputs. Wall-clock
values that reach the *rendered output* — a decay score printed in the
block, an access counter, a timestamp in the header — make a replayed turn
produce different bytes than the original.

Within one linear session this is harmless, because past turns are sealed in
the transcript and never re-rendered. It bites on retry after an API error,
on session fork or resume, and on any harness that rebuilds the prompt from
state rather than replaying a transcript.

This is why `SubconsciousMemory` defaults to `output_format="content"`
(`DEFAULT_OUTPUT_FORMAT` in `popoto/recipes/subconscious_memory.py`). That
format emits the memory text alone — no field names, no key values, and no
scores — so the block is stable under replay.

!!! danger "The other output formats are not replay-stable"
    `structured`, `xml`, and `natural` serialize through `_record_to_dict()`,
    which walks **every** field on the model. On a memory model that includes
    decay, confidence, or access-count fields, those values change between
    turns, so the same query renders different bytes on a replay.

    They remain correct choices when you want the full record shape and
    control the replay behavior yourself. They are the wrong default for
    per-turn injection, which is why they are not the default.

Note that ranking may safely depend on wall clock. Decay deciding *which*
memories appear in a new block is fine; a decay score appearing *as text
inside* one is not.

## The cost you cannot remove

Rule 1 buys cache-safety by forbidding removal, and that has a consequence
nobody gets to opt out of: every block ever injected stays in the context for
the rest of the session.

At a budget of `B` tokens per turn over `T` turns, memory tokens resident in
the prefix at turn `t` are roughly `B · t`, and every one of them is re-read
on every later turn. Cumulative cache-read attributable to memory therefore
grows with the **square** of session length:

```
resident at turn t  =  B · t
cumulative read     =  B · T(T+1) / 2

at B = 800 (the integration default), T = 100 turns:
    ≈ 4.0M cache-read tokens
    ≈ 400K full-price-equivalent tokens at a 0.1x read multiplier
```

You cannot fix this by pruning. Removing a stale block is a mutation of
sealed history, which costs everything behind it — strictly worse than
leaving it in place. The quadratic is the honest price of an immutable
prefix.

Two techniques reduce it without breaking rule 1. Both work by adding less,
never by removing.

### Shrink the constant: inject stubs, not full content

`B` is set by `POPOTO_MEMORY_MAX_TOKENS` (default 800) and
`POPOTO_MEMORY_MAX_ITEMS` (default 5). Those defaults inject full memory
content, which is the right call when there is no discretionary channel to
fall back on.

When the agent also has the MCP tools available, you can inject a one-line
stub per memory — an identifier, a category, a title — and let the model call
`memory_search` for the full content of the ones that look relevant. A stub
costs roughly 15-25 tokens against 150+ for full content, which cuts `B` by
close to an order of magnitude while keeping everything reachable. The
on-demand fetch arrives as a tool result at the tail, so it is cache-safe
too.

Progressive disclosure like this is the single highest-leverage knob on this
page, and it is only available because hooks and MCP tools are wired
together: the hook guarantees the stub arrives every turn, and the tool makes
the rest retrievable without paying for it up front.

### Flatten the curve: do not re-inject what you already injected

Consecutive prompts within one session are topically near-identical by
nature, so the same top-`k` records recur turn after turn. Suppressing an
already-injected memory is append-only by construction — you are declining
to add, not removing — so it costs nothing in cache terms, and it changes
growth from quadratic in *turns* to linear in *distinct memories*, which is
bounded by store size rather than session length.

!!! note "Currently caller-side"
    `ContextAssembler.assemble()` does not yet accept an exclusion set, so
    per-session injection suppression is the integrator's job today. Track
    the record keys you have injected this session — `MemoryService` already
    records them per session for outcome feedback — and filter
    `result.records` before rendering.

    A per-session sidecar file holding the injected keys, passed into
    retrieval as an exclusion set, is the pattern that works today. Adding a
    first-class `exclude_keys` parameter to the assembler is a known gap.

## What breaks it

Concrete anti-patterns, in rough order of how much they cost:

- **Rendering memory into the system prompt or a tool description.** Full
  prefix, every turn.
- **Re-reading a memory file into the context each turn.** Full prefix on
  every capture.
- **Editing or removing an earlier injected block.** Everything behind the
  edit.
- **Printing decay scores, access counts, or timestamps into the block.**
  Free until a retry, fork, or resume, then a full miss.
- **Injecting into short-lived subagents.** Not a correctness bug, but a
  subagent is a fresh prefix with no history to amortize against and often
  lives two or three turns — injected memory is prefilled at full price and
  read back once, if at all. Budget it well below the main-thread budget, or
  skip it.

## Measuring it

Providers report per-call cache accounting, and harnesses record it in their
session transcripts. Two numbers tell you almost everything:

**Hit rate**, as cache reads over total input. A healthy long session sits in
the 90s. Enabling memory should move it by a point or two, not by tens.

**Short-gap full misses.** Any call with zero cache read that follows the
previous call by less than the cache TTL is a prefix mutation, not an
expiry. That count should be zero. If it is not, something is writing above
the tail.

```python
read = usage.get("cache_read_input_tokens", 0)
create = usage.get("cache_creation_input_tokens", 0)
fresh = usage.get("input_tokens", 0)

hit_rate = read / (read + create + fresh)
# read == 0 and gap < cache_ttl  ->  prefix mutation, investigate
```

The honest way to price the memory layer is not to measure the injected
block. Run the same scripted session twice, once with memory enabled and
once without, and compare total cache creation plus uncached input. That
difference is what memory actually costs.

## Related

- [Harness Integration](harness-integration.md) — where the hooks attach
- [ContextAssembler](context-assembler.md) — `output_format` and token budgets
- [SubconsciousMemory](../guides/subconscious-memory-recipe.md) — the recall/capture loop
