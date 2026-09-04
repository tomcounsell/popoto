---
status: Ready
type: bug
appetite: Medium
owner: Tom Counsell
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/574
last_comment_id:
---

# Pending-Turn Handoff Keyed on Turn ID

## Problem

A developer wires Popoto memory into Claude Code with the eight lines in
`docs/guides/harness-claude-code.md`. The `Stop` hook is `"async": true`, as the
guide recommends, so the write path runs off the turn's critical path. They
submit a prompt, hit escape to abort it, and type a new one. From that moment on,
every outcome report in the session lands on the wrong turn's records: turn N's
`used` outcome is applied to turn N-1's injected memories, forever, until the
32-entry cap rolls the queue over.

Nothing crashes. Nothing is logged. Confidence and decay clocks are being
refreshed against memories that were never shown for the response being scored,
which is precisely the signal the whole subconscious loop is built on.

**Current behavior:**

`MemoryService._push_pending()` (`src/popoto/integrations/service.py:522`, cited
as `:474` in the issue before drift) stores each read hook's selected record keys
as an untagged JSON array `RPUSH`ed onto one list per session,
`$popoto_memory:pending:{agent_id}:{session_id}`. `_pop_pending()`
(`service.py:625`) does a bare `LPOP`. The pairing between a read and its write
is positional, not identified.

Two ways the position shifts:

1. **A read with no paired write.** An aborted turn, a crashed session, or a
   `Stop` hook that never fired leaves its entry at the head. Every later `LPOP`
   is off by one, permanently.
2. **More pops than pushes.** A session configured with `SubagentStop` fires one
   write event per subagent against a single earlier read. The first pop takes
   the correct entry; each additional subagent steals an entry belonging to some
   other turn.

Both harnesses that matter already send the identifier that would make this
exact: Claude Code sends `prompt_id`, Codex sends `turn_id`, and both send it on
the read event *and* the write event of the same turn. The integration ignores
it.

**Desired outcome:**

An outcome report resolves the turn that actually staged it, or resolves nothing
at all. Never someone else's turn. Harnesses that supply no turn identifier keep
the FIFO behavior they have today, unchanged, and adopters who upgrade mid-session
lose nothing.

## Freshness Check

**Baseline commit:** `b8e1dc469c7c2e75846017b436418d66cd98fcff`
**Issue filed at:** 2026-08-13T09:04:45Z
**Disposition:** Minor drift

**File:line references re-verified:**

- `src/popoto/integrations/service.py:474` — the issue cites this as
  `_push_pending`. **Drifted to `service.py:522`.** The function body is
  byte-identical to what PR #546 shipped; the offset comes from additions above
  it. The claim still holds in full.
- `_pop_pending` — not cited by line in the issue; it is at `service.py:625`, also
  unchanged since #546.
- `docs/features/harness-integration.md` "Known gap" section — still present,
  lines 111-122, still accurate.

**Cited sibling issues/PRs re-checked:**

- **#515** (harness integration) — closed. Its plan is
  `docs/plans/harness_integration.md`, status `Planning` in frontmatter but
  shipped via PR #546.
- **PR #546** — merged 2026-08-17T02:52:45Z. This is the PR whose round-2 review
  raised the finding. The `NOTE (PR #546 review, tech debt)` block in the
  `_push_pending` docstring names #574 explicitly and is the text this plan
  replaces.

**Commits on main since issue was filed (touching referenced files):**

```
edf71ad fix(#596): deploy-level kill switch and loud first eviction for DefaultMemory (#598)
16aa702 Agent memory production audit: contracts and P0 fixes (#594)
3a793d6 feat(recipes): exclude_keys suppression + tail-position injection (#592)
bc307d9 docs: prompt cache efficiency for subconscious memory
337b3f0 feat(#561): never-record firewall (#587)
```

Diffed each against `src/popoto/integrations/service.py`. **None touched the
pending handoff.** `git diff e220b2e..HEAD -- src/popoto/integrations/service.py`
shows no `-`/`+` line inside `_push_pending` or `_pop_pending`.

One change is adjacent and worth naming: #592 added `_mark_injected` /
`_injected_keys`, a per-session **SET** at
`$popoto_memory:injected:{agent_id}:{session_id}` carrying the same
`PENDING_TTL_SECONDS`. It shares the TTL and the naming pattern with the pending
list but has different semantics on purpose (accumulated union for suppression,
never consumed). This plan does not touch it, and must not: keying suppression on
a turn id would defeat suppression.

**Active plans in `docs/plans/` overlapping this area:**
`docs/plans/harness_integration.md` — the parent plan for #515. It is the source
of the shipped design, not a competing active lane. No conflict.

**Notes:** The corrected line numbers (`service.py:522` push, `service.py:625`
pop) are used throughout the Technical Approach below.

## Prior Art

- **PR #546** — `feat(#515): subconscious memory for Claude Code, Codex, Hermes,
  and OpenClaw`, merged 2026-08-17. Shipped the FIFO deliberately, with the
  reasoning recorded in the `_push_pending` docstring: a single pending slot per
  session would let an async `Stop` for turn N report against turn N+1's records,
  so a queue was the minimum correct thing. Turn-id keying was scoped out because
  it needs a second code path for harnesses that send no identifier. That
  reasoning is still right; this plan builds the second path rather than
  replacing the first.
- **Issue #552** — `OpenClaw ships MCP-only: hook-based subconscious memory
  unverified (#515 follow-up)`, open. Sibling follow-up. Relevant because it means
  the OpenClaw fallback path this plan preserves is itself unverified against a
  real OpenClaw; the fallback is preserved on the strength of the docs-derived
  fixture, not a live run. Not a blocker: preserving current behavior for
  untagged payloads is the conservative choice regardless.
- No prior attempt to key the handoff on a turn id exists. This is the first.

## Research

No external research needed. The one external fact this plan depends on is the
Claude Code and Codex hook payload schema, and that is settled by fixtures
captured in-repo plus a documentation check — recorded under Spike Results rather
than here.

Queries used: none. The work is a change to Popoto's own Redis handoff; the
harness contract was verified from captured payloads and the published hooks
reference, both cited in spike-1.

## Spike Results

### spike-1: Both harnesses send the same turn identifier on the read and write events

- **Assumption**: "Claude Code's `prompt_id` and Codex's `turn_id` appear on the
  pre-turn event *and* the post-turn event, carrying the same value for one turn."
  If this is false the whole plan collapses, because the write hook would have
  nothing to match against.
- **Method**: code-read (repo fixtures) + web-research (published hooks reference)
- **Finding**: **Confirmed, and confirmed twice.**

  From `tests/fixtures/harness_payloads/`, whose `_provenance` fields state
  `claude_code_user_prompt_submit.json` and `claude_code_stop.json` were captured
  from one live `claude` 2.1.220 headless run on 2026-08-07:

  | Fixture | identifier | value | session_id |
  |---|---|---|---|
  | `claude_code_user_prompt_submit.json` | `prompt_id` | `ebc66c1d-1aff-4008-a78c-5d8c443fde5f` | `9126bdcd-…` |
  | `claude_code_stop.json` | `prompt_id` | `ebc66c1d-1aff-4008-a78c-5d8c443fde5f` | `9126bdcd-…` |
  | `codex_user_prompt_submit.json` | `turn_id` | `2f0f0f1b-1f2c-4a1e-9c1a-4b0d3d9e5f21` | `019fdbed-…` |
  | `codex_stop.json` | `turn_id` | `2f0f0f1b-1f2c-4a1e-9c1a-4b0d3d9e5f21` | `019fdbed-…` |

  The Claude Code pair is a live capture: same session, same turn, identical
  `prompt_id`. That is empirical proof, not a reading of documentation.

  The published Claude Code hooks reference independently lists `prompt_id`
  (uuid) on `UserPromptSubmit`, `Stop`, `SubagentStop`, and `PostToolUse`, along
  with a `turn_number` integer on each.

  The Codex pair is derived from the `codex-cli` 0.144.4 binary's own hook-input
  field list, not a live turn (the fixture README records that a live Codex
  capture failed on the project-level hook skip). So Codex is
  schema-verified, not run-verified. See Risk 2.
- **Confidence**: high for Claude Code, medium for Codex.
- **Impact on plan**: The dual-path design is viable as designed. Codex's
  medium confidence costs nothing, because a Codex payload that turned out to
  omit `turn_id` would simply take the untagged FIFO path.

### spike-2: `SubagentStop` carries the parent turn's identifier, not a per-subagent one

- **Assumption**: "`SubagentStop` sends something that distinguishes one subagent
  from another, so each subagent could resolve its own pending entry."
- **Method**: web-research (published hooks reference)
- **Finding**: **Assumption is false, in a way that simplifies the plan.**
  `SubagentStop` carries `session_id` (the *parent* session), `prompt_id` (the
  *parent* turn's id), plus `agent_id` and `agent_type` identifying the subagent.
  There is no per-subagent pending entry to resolve, because the read hook fired
  once for the parent turn and staged exactly one entry.
- **Confidence**: high (documented), medium as to whether the local fixture set
  covers it — `tests/test_integrations_hooks.py::test_subagent_stop_is_a_write_event`
  exists but there is no captured `SubagentStop` payload fixture.
- **Impact on plan**: Do **not** key on `agent_id`. One read, one outcome report.
  Under turn-id keying, the first `SubagentStop` consumes the parent turn's entry
  and every later `SubagentStop` (and the final `Stop`) resolves nothing — which
  is the correct answer and is exactly the second failure mode #574 names. This
  becomes a test, not a code path.

### spike-3: `LREM`-by-value is available as the mutual-exclusion primitive on both servers

- **Assumption**: "A remove-by-value on a list can serve as the compare-and-claim
  step, on Redis and Valkey alike, with no module and no Lua."
- **Method**: code-read
- **Finding**: Confirmed. `LREM key 1 <element>` is a core list command on both
  servers, returns the number of elements removed, and is atomic. Because each
  staged element embeds a unique turn id, its serialized bytes are unique within
  the list, so `LREM` returning `1` is an exclusive claim and returning `0` means
  someone else already claimed it. No `WATCH`, no Lua, no module — which keeps
  the Valkey-compatibility rule intact.
- **Confidence**: high
- **Impact on plan**: Settles the concurrency design (see Race 2 and Race 3).
  Rules out the tempting Lua script (see Rabbit Holes).

## Data Flow

Two hook invocations per turn, in separate OS processes, with Redis as the only
shared state between them.

1. **Entry point (read).** The harness fires its pre-turn hook and writes JSON to
   `popoto-memory hook` on stdin. Claude Code sends `hook_event_name:
   "UserPromptSubmit"`, `session_id`, `prompt_id`, `prompt`, `cwd`.
2. **`hooks.normalize()`** reduces the payload to a `NormalizedEvent`. **Change:**
   it now also extracts a `turn_id` from the first present of `prompt_id`,
   `turn_id`, `promptId`, `turnId`, using the same one-level-nested search
   `_first_string` already uses for the prompt text.
3. **`hooks.handle_payload()`** calls
   `service.assemble(text, session_id=..., turn_id=...)`.
4. **`MemoryService.assemble()`** retrieves through `ContextAssembler`, then calls
   `_push_pending(session_id, records, turn_id=...)` and, unchanged,
   `_mark_injected(session_id, records)`.
5. **`_push_pending`** `RPUSH`es one JSON **object** `{"t": <turn id or null>,
   "k": [<record keys>]}` onto `$popoto_memory:pending:{agent_id}:{session_id}`,
   then `LTRIM` to `MAX_PENDING_TURNS` and `EXPIRE` to `PENDING_TTL_SECONDS`. Key
   name, cap, and TTL are all unchanged.
6. **Output (read).** The assembled block goes back to the harness as
   `hookSpecificOutput.additionalContext`. Redis now holds one tagged pending
   entry.

   *(The model answers. Time passes. The user may abort, spawn subagents, or
   submit another prompt before the write hook runs.)*

7. **Entry point (write).** The harness fires `Stop` (or `SubagentStop`) in a new
   process, carrying `last_assistant_message` and the **same** `prompt_id`.
8. **`hooks.normalize()`** extracts the same `turn_id`.
9. **`hooks.handle_payload()`** calls `service.capture(...)` unchanged, then
   `service.feedback(session_id, outcome="used", turn_id=...)`.
10. **`MemoryService.feedback()`** calls `_pop_pending(session_id,
    turn_id=...)`.
11. **`_pop_pending`** — the only branch in the whole change:
    - **turn id present, turn keying enabled**: `LRANGE key 0 -1` (bounded at 32
      entries), find the first element whose `t` equals the turn id, then
      `LREM key 1 <that exact raw element>`. Proceed only if `LREM` returned
      `>= 1`. No match → return `[]` (see the one compat exception in Technical
      Approach).
    - **no turn id, or turn keying disabled**: `LPOP key`, then decode through
      the *same* tolerant decode the claiming branch uses. Not "exactly as
      today": under default-on turn keying an untagged harness stages
      `{"t":null,"k":[...]}`, and today's reader (`json.loads` then
      `[k for k in keys if isinstance(k, str)]`) iterates a dict's **keys** and
      returns the literal `["t","k"]`. See the shared-decode rule in Technical
      Approach — getting this wrong silently zeroes outcome reporting for Hermes
      and OpenClaw on the default configuration.
12. **Output (write).** `ObservationProtocol.on_context_used()` applies the
    outcome to the records the *matching* turn staged. The hook writes nothing to
    stdout and exits 0.

## Architectural Impact

- **New dependencies**: none. No new imports, no new Redis data type, no Lua, no
  module. `LRANGE` and `LREM` are core commands on Redis and Valkey.
- **Interface changes**: three private and two public signatures gain one
  optional keyword-only `turn_id: Optional[str] = None`:
  `MemoryService.assemble`, `MemoryService.feedback`, `_push_pending`,
  `_pop_pending`. Every existing call site keeps working untouched.
  `NormalizedEvent` gains a `turn_id` slot — it uses `__slots__`, so the slot
  tuple and `__init__` both change; both are internal to `hooks.py`.
  `MemoryConfig` gains a `turn_keyed: bool` field.
- **Coupling**: unchanged. `hooks.py` remains the single place a harness schema
  is read; `turn_id` extraction lands there beside the existing field probes,
  not in the service.
- **Data ownership**: unchanged. Same Redis key, same TTL, same cap. Only the
  serialized element shape changes, and both shapes are readable.
- **Reversibility**: high. `POPOTO_MEMORY_TURN_KEYED=0` restores the exact
  current behavior at deploy time with no code edit; a full revert leaves only
  object-shaped elements in live queues, which the pre-change reader would
  mis-parse — see Risk 3.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (the fix is fully specified by the issue and this plan)
- Review rounds: 1

**Why Medium and not Small.** A Small appetite would be a single-function patch.
This is not one: the change spans `hooks.py` (turn-id extraction and a `__slots__`
change), `service.py` (two storage functions plus two public signatures),
`config.py` (a new env flag), `demo.py` (so the runnable example demonstrates the
default path rather than the fallback), four documentation files, and — the part
that actually sets the size — a concurrency test matrix covering interleaved
turns across two sessions, duplicate writes for one turn, aborted turns, the
untagged fallback, and mixed-shape queues left by an in-flight upgrade. The
correctness argument lives in those tests, so they are the deliverable, not the
trimming.

It is not Large either: no new Redis structure, no migration, no Lua, no change
to retrieval, no change to what gets stored as memory.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey on localhost:6379 | `redis-cli -h localhost -p 6379 ping` | The pending handoff is Redis state; every test in scope needs a live server |
| Editable install resolves to this checkout | `python -c "import popoto, pathlib, sys; p=pathlib.Path(popoto.__file__).resolve(); sys.exit(0 if 'src/popoto' in str(p) else 1)"` | A worktree venv pointing at another tree silently tests the wrong package (CLAUDE.md, gotcha 1) |
| `redis-py` version recorded | `python -c "import redis; print(redis.__version__)"` | The mypy error count is redis-py-version-dependent; the baseline below is on 7.1.1 |

## Solution

### Key Elements

- **Turn-id extraction in the hook adapter**: `NormalizedEvent` carries the
  harness's own per-turn identifier when it sends one, `None` when it does not.
  One place, beside the existing field probes.
- **Self-describing pending entries**: each staged entry records which turn staged
  it. The list key, its cap, and its TTL do not change.
- **A claiming pop**: a write event that knows its turn id claims that turn's
  entry by value, or claims nothing. It can never take a different turn's entry.
- **A preserved FIFO fallback**: a write event with no turn id pops the head, as
  today. Hermes and OpenClaw behave identically to the current release.
- **A deploy-level kill switch**: `POPOTO_MEMORY_TURN_KEYED=0` forces the legacy
  path on both sides, for an operator who cannot edit code.

### Flow

**Read hook fires** → adapter extracts `prompt_id` → **context assembled** →
entry staged tagged with the turn id → **harness injects context** → *(model
answers; the user may abort, fan out to subagents, or submit again)* → **write
hook fires with the same `prompt_id`** → adapter extracts it → **entry for that
exact turn is claimed** → outcome applied to that turn's records → **hook exits
silently**

The branch that matters is the one that does *not* happen: a write hook whose
turn was aborted, or a second `SubagentStop` for an already-resolved turn, finds
nothing to claim and reports nothing. Today it takes the next turn's entry.

### Technical Approach

**Entry encoding.** `_push_pending` writes
`json.dumps({"t": turn_id, "k": keys}, sort_keys=True, separators=(",", ":"))`.
Deterministic separators and key order matter: the raw string is the value passed
to `LREM`, so its bytes must be reproducible from what `LRANGE` returned. In
practice `LREM` is given the exact bytes `LRANGE` handed back, never a
re-serialization — this is belt and braces, and the test suite pins it.

**Reader tolerates both shapes, on both branches.** The decode is **one shared
step**, applied to a raw element however it was fetched — by `LPOP` on the
fallback branch or by `LRANGE` on the claiming branch. Neither branch may keep a
private parser:

```python
elem = json.loads(raw)
if isinstance(elem, dict):
    keys = elem.get("k")
elif isinstance(elem, list):
    keys = elem            # legacy untagged entry
else:
    keys = None            # corrupt
```

`None` (or a non-list `k`) is corrupt: log through `_record_failure("pending_pop",
...)`, `LREM` it out of the way so it cannot jam later claims, and skip it.
Otherwise filter `[k for k in keys if isinstance(k, str)]` as today.

This is the single most important rule in the change. The pre-change reader
parses only the bare-array shape, and under default-on turn keying *every*
entry is object-shaped — including an untagged harness's `{"t":null,"k":[...]}`.
A fallback branch that kept the old parser would iterate the dict's keys and
return `["t","k"]`, resolving no records, so `feedback` would return 0 for every
Hermes and OpenClaw turn while looking perfectly healthy.

**Claiming pop, turn id present.**

1. `LRANGE key 0 -1` — bounded by `MAX_PENDING_TURNS` (32), so this is a small
   read, not a scan.
2. Walk the elements in order, decode each, take the **first** whose `t` equals
   the turn id.
3. `LREM key 1 <that raw element>`. If it returns `0`, another process claimed it
   first: return `[]`, report nothing. If `>= 1`, return that entry's keys.

**Miss policy — the one decision worth arguing about.** When the turn id matches
nothing, `_pop_pending` returns `[]` and the outcome is dropped. It does **not**
fall back to `LPOP`, because a fallback would reintroduce exactly the
misattribution this plan exists to remove, at exactly the moment (an aborted or
already-claimed turn) when misattribution is most likely.

There is **one** exception, for upgrade-in-flight only: if the turn id matched
nothing **and every element in the list is legacy-shaped**, fall back to `LPOP`.
"Legacy-shaped" means the decoded element is a bare `list` — i.e. the `t` **key
is absent from the object entirely**. It does *not* mean "`t` is falsy". Under
default-on turn keying an untagged-harness entry is `{"t":null,"k":[...]}`,
which carries the key with a null value; reading the predicate as value
truthiness would wrongly re-enable the `LPOP` fallback for those entries and
reintroduce the misattribution this plan removes. That is the case where the read was staged by
the pre-upgrade code and the write is running the new code. The fallback is
bounded — it cannot fire once a single tagged entry exists in the list — and its
behavior is exactly the current release's, so it cannot be worse than the status
quo. Once the queue has turned over, this branch is dead for the rest of the
session.

**Kill switch.** `MemoryConfig` gains `turn_keyed: bool`, read from
`POPOTO_MEMORY_TURN_KEYED` through the existing `_as_bool` helper, defaulting to
`True`. When false, `_push_pending` writes the legacy bare-array shape and
`_pop_pending` uses `LPOP` — a byte-for-byte return to current behavior, with no
mixed-shape residue. Default-on with a deploy-level escape hatch is the house
rule for capability changes; an operator running Popoto from PyPI cannot edit
model code, so an env var is the only escape hatch that actually reaches them.

**`SubagentStop` needs no special case.** It carries the parent turn's
`prompt_id` (spike-2), so the first one to fire claims the parent turn's entry and
the rest find nothing. That is the desired semantics — one read, one outcome
report — and it arrives for free from the claiming pop.

**What deliberately does not change:** the Redis key name, `MAX_PENDING_TURNS`,
`PENDING_TTL_SECONDS`, the `RPUSH`/`LTRIM`/`EXPIRE` push pipeline, the injected-key
suppression SET, `MemoryService.search`'s refusal to consume a pending slot, and
every existing signature's positional arguments.

## Failure Path Test Strategy

### Exception Handling Coverage

`_push_pending` and `_pop_pending` each wrap their whole body in
`except Exception` and route to `self._record_failure(...)`, which logs a warning,
appends a line to the failure log, and increments a counter. That is observable
behavior, and the tests assert it rather than asserting a swallow.

- [ ] A corrupt element in the list (non-JSON bytes, or valid JSON of the wrong
      type) must produce a `pending_pop` line in the failure log and must not
      raise. New test.
- [ ] `_pop_pending` with Redis down must return `[]` and record a failure. The
      existing `test_feedback_degrades_quietly_when_redis_is_down` covers the
      FIFO path; extend it to the turn-id path so both branches are proven quiet.
- [ ] `_push_pending` with a `turn_id` and Redis down must not raise and must not
      block the read path's return value. New assertion on the existing
      Redis-down fixture.

### Empty/Invalid Input Handling

- [ ] `turn_id=""` and `turn_id="   "` are normalized to `None` at the adapter
      boundary, so an empty-string identifier takes the FIFO path instead of
      staging an entry tagged with the empty string. `hooks._first_string`
      already applies the `.strip()` truthiness rule; the turn-id probe reuses it.
- [ ] `turn_id=None` with an empty pending list returns `[]` — already covered by
      `test_feedback_without_a_pending_turn_is_a_no_op`, re-asserted for the
      tagged path.
- [ ] `feedback(session_id="")` remains a no-op regardless of `turn_id` — the
      existing guard short-circuits before any Redis call. Extend
      `test_feedback_without_a_session_id_is_a_no_op` with a `turn_id` argument.
- [ ] A payload carrying a non-string `prompt_id` (a number, an object) yields
      `turn_id=None` rather than a `TypeError`. The adapter's `isinstance(value,
      str)` check already enforces this; pin it with a test.

### Error State Rendering

The write hook has no user-visible output by contract — it returns `None` and the
process exits 0. The user-visible surface is `popoto-memory doctor`.

- [ ] The failure counters `doctor` prints must include `pending_pop` failures
      from the turn-id path. `test_doctor_reports_the_failure_counters` already
      asserts the shape; confirm the new failure path feeds the same counter name.
- [ ] No new stdout is emitted on the write path under any turn-id outcome —
      claimed, missed, or errored. Assert `handle_payload` still returns `None`
      for every `Stop` variant.

## Test Impact

All new tests land in the two existing files; no test file is created or deleted.

- [ ] `tests/test_integrations_service.py::test_interleaved_turns_do_not_cross_report`
      (`tests/test_integrations_service.py:276`) — UPDATE. The plan previously
      named this `test_pending_list_survives_out_of_order_writes`, which does not
      exist anywhere in `tests/`; that was a plan error, corrected in critique.
      The real test asserts the FIFO ordering contract by calling
      `_pop_pending("s1")` twice positionally. Its premise is still valid for
      untagged sessions. Keep it, and rename nothing; add explicit `turn_id=None`
      arguments so it reads as a fallback-path test rather than as the default
      contract. **Also strengthen it**: its current body asserts only
      `first != second` plus a final empty pop, and an inequality assertion passes
      under the very misattribution being fixed. Assert the exact resolved record
      keys instead.
      **Before starting task 5**, run
      `grep -n "^def test_" tests/test_integrations_service.py tests/test_integrations_hooks.py`
      and re-verify every name cited below against the real files rather than
      trusting this plan's line numbers.
- [ ] `tests/test_integrations_service.py::test_feedback_reports_against_the_turn_that_was_injected`
      — UPDATE: currently passes no turn id. Add a sibling test for the tagged
      path rather than changing this one; it is the fallback path's regression
      guard.
- [ ] `tests/test_integrations_service.py::test_pending_list_is_capped` and
      `::test_pending_key_has_a_ttl` — UPDATE: assert the cap and TTL still hold
      when entries are tagged. The push pipeline is unchanged, so these should
      pass with only a `turn_id` argument threaded through the setup; if they do
      not, the push path was changed more than intended.
- [ ] `tests/test_integrations_service.py::test_feedback_degrades_quietly_when_redis_is_down`
      — UPDATE: parametrize over `turn_id=None` and `turn_id="t1"`.
- [ ] `tests/test_integrations_hooks.py::test_read_fixtures_normalize_to_the_prompt`
      and `::test_write_fixtures_normalize_to_the_assistant_message` — UPDATE:
      these already assert `event.session_id` is truthy for every fixture. Add a
      turn-id assertion split by harness: truthy for the Claude Code and Codex
      fixtures, `None` for the Hermes and OpenClaw ones.
- [ ] `tests/test_integrations_hooks.py::test_outcome_is_reported_on_the_following_stop`
      — UPDATE: it loads the Claude Code fixture pair, which shares a `prompt_id`,
      so it now exercises the tagged path. Assert explicitly that it resolves via
      the turn id, not by position, so the test stops being ambiguous about which
      path it proves.
- [ ] `tests/test_integrations_hooks.py::test_write_path_reports_used_not_acted`
      — UPDATE: its stub service defines `feedback(self, session_id,
      outcome="acted")`. That signature will no longer match the call once
      `turn_id` is passed. Update the stub and assert the recorded call includes
      the fixture's `prompt_id`.
- [ ] `tests/test_integrations_cli.py` — no changes expected. The demo change
      alters only lines the CLI tests do not assert on
      (`test_demo_prints_the_rejection_instead_of_a_traceback` asserts the DB-0
      rejection path, which returns before the handoff runs). Verify, do not
      assume.

**The new tests**, all in `tests/test_integrations_service.py` unless noted:

1. `test_feedback_resolves_the_turn_that_staged_it` — stage turns `t1` and `t2`
   in one session, report `t2` first, then `t1`. Each resolves its own record
   keys. This is the out-of-order case that the FIFO gets wrong.
2. `test_interleaved_turns_from_two_sessions_do_not_cross` — **the test the issue
   is about.** Sessions `sA` and `sB`, distinct seeded records per session so the
   resolved keys are distinguishable. Stage in the order `sA/t1`, `sB/t1`,
   `sA/t2`, `sB/t2`; report in the scrambled order `sB/t2`, `sA/t1`, `sB/t1`,
   `sA/t2`. Assert each report resolves exactly its own turn's keys and that no
   key from the other session is ever touched. Assert both pending lists are
   empty at the end.
3. `test_aborted_turn_does_not_shift_later_pairings` — stage `t1` and `t2`, never
   report `t1`, report `t2`. `t2` resolves `t2`'s keys, and `t1`'s entry is still
   in the list afterwards.
4. `test_subagent_stop_resolves_the_parent_turn_once` — two write events carrying
   the same turn id. First returns the record count; second returns `0`. Then a
   later, different turn's report still resolves correctly, proving the second
   subagent stole nothing.
5. `test_untagged_harness_keeps_fifo_order` — Hermes/OpenClaw shape, `turn_id=None`
   on both sides, ordering preserved. The current behavior, pinned. **Must assert
   that the returned record keys equal the seeded keys**, not merely ordering or
   counts: the blocker this plan was revised for returns `["t","k"]` from the
   fallback branch, which satisfies a count-only or ordering-only assertion while
   resolving no records at all.
6. `test_legacy_entries_are_claimed_after_an_upgrade` — hand-write a legacy
   bare-array element with `RPUSH`, then report with a turn id. It resolves via
   the upgrade-in-flight fallback.
7. `test_upgrade_fallback_stops_once_a_tagged_entry_exists` — a legacy element
   *and* a tagged element in the same list; a report for an unknown turn id
   resolves nothing and leaves both elements in place. This is the boundary of the
   compat exception and the test that stops it from becoming a general fallback.
8. `test_turn_keyed_kill_switch_restores_fifo` — with
   `POPOTO_MEMORY_TURN_KEYED=0`, entries are stored in the legacy shape and pops
   are positional even when a turn id is supplied.
9. `test_corrupt_pending_entry_is_logged_and_skipped` — `RPUSH` raw garbage, then
   report; no raise, a `pending_pop` line in the log, and the garbage no longer
   blocks a later valid claim.
12. `test_duplicate_push_for_same_turn_stages_one_claimable_entry` — call
    `_push_pending` twice with the same `session_id`, `turn_id` and records;
    assert `LLEN` is 1. Then report twice for that turn: the first returns the
    record count, the second returns `0`. Guards the advisory same-turn check
    added to task 3.
10. `tests/test_integrations_hooks.py::test_turn_id_is_normalized_from_every_harness`
    — parametrized over all eight fixtures; asserts the exact expected turn id per
    fixture, including `None` for the four docs-derived ones.
11. `tests/test_integrations_hooks.py::test_a_full_turn_pair_resolves_by_turn_id`
    — end to end through `handle_payload` with the live Claude Code fixture pair,
    plus a second synthetic pair with a different `prompt_id`, interleaved.

## Rabbit Holes

- **Rewriting the handoff as a Redis hash keyed by turn id.** It looks cleaner
  than a list scan, and it is — until you need the cap. Hashes are unordered, so
  `MAX_PENDING_TURNS` would need a companion index to know which field is oldest,
  which is more moving parts than the `LRANGE` of at most 32 small elements this
  plan does. Do not.
- **A Lua script for atomic find-and-remove.** `LREM`-by-value already gives the
  exclusive claim (spike-3). A script would also put this on the wrong side of
  the Valkey-compatibility rule's spirit and add an `EVALSHA`/`NOSCRIPT` reload
  path for no correctness gain.
- **Keying on `agent_id` for `SubagentStop`.** Tempting because the field exists.
  It is wrong: the read hook staged one entry for the parent turn, so there is
  nothing per-subagent to resolve, and one outcome report per turn is the correct
  semantics (spike-2).
- **Extending turn-id keying to the injected-key suppression SET.** They share a
  TTL and a naming pattern, so they look like the same thing. They are not: the
  SET is a session-wide accumulated union and keying it per turn would defeat
  suppression entirely and reintroduce the quadratic cache growth #592 fixed.
- **Making `turn_id` required, or plumbing it through the MCP tools.** The MCP
  `memory_feedback` tool resolves a record by explicit key through
  `MemoryService.correct()` and never touches the pending queue. Leave it alone.
- **Chasing a live Codex or OpenClaw capture.** Codex's live hook run is blocked
  by the project-level trust skip already documented in
  `docs/guides/harness-codex.md`, and OpenClaw's hook path is #552's problem. The
  fallback makes both safe without a capture.

## Risks

### Risk 1: The miss policy silently drops outcome reports

**Impact:** Under turn-id keying, a write whose turn id matches nothing reports
nothing. If some real harness sequence produces mismatched ids between the read
and write events, outcome reporting stops entirely for that harness — and it
stops the same way the current bug operates, silently. Silence is much safer than
misattribution, but it is still silence.
**Mitigation:** Every miss goes through `_record_failure("pending_miss", ...)`,
so it lands in the failure log and in the counters `popoto-memory doctor` prints,
rather than being a bare `return []`. An operator whose reports stopped can see
why in one command. The counter name is distinct from `pending_pop` so a miss is
never confused with a Redis error.

### Risk 2: Codex's `turn_id` is schema-verified, not run-verified

**Impact:** The Codex fixtures come from the 0.144.4 binary's field list, not a
live turn (spike-1). If the live payload omits `turn_id`, or sends a different
value on `Stop` than on `user_prompt_submit`, Codex either falls back to FIFO
(harmless) or misses every report (Risk 1).
**Mitigation:** The fallback is the failure mode for an absent id, so the bad
case is bounded to "no worse than today". For a mismatched id, the `pending_miss`
counter makes it diagnosable. Document in `docs/guides/harness-codex.md` that the
turn-keyed path is unverified against a live Codex run, matching the honesty the
existing fixture README already applies.

### Risk 3: A revert leaves object-shaped entries the old reader cannot parse

**Impact:** If this change ships and is later reverted, live pending lists may
hold `{"t":…,"k":[…]}` elements. The pre-change `_pop_pending` does
`json.loads(raw)` then `[k for k in keys if isinstance(k, str)]` — over a dict
that iterates the *keys of the dict* (`"t"`, `"k"`), yielding a list of two
strings that are not record keys. `get_many` would return nothing for them, so
`feedback` returns 0. Not a crash, but a silent dead queue until the TTL expires.
**Mitigation:** Prefer the kill switch over a code revert, and say so in the
feature doc: `POPOTO_MEMORY_TURN_KEYED=0` returns to legacy behavior *and* legacy
encoding without leaving unreadable residue. The blast radius of an actual revert
is bounded by `PENDING_TTL_SECONDS` (3600) anyway.

### Risk 4: `NormalizedEvent.__slots__` is a compatibility surface

**Impact:** `NormalizedEvent` uses `__slots__` and a positional `__init__`. Adding
`turn_id` changes both. Any code constructing one positionally — the test stubs
do — breaks at the call, not silently.
**Mitigation:** Add `turn_id` as the last parameter with a `None` default so
existing positional construction keeps working. Breaks are loud either way, and
the class is internal to the integration.

### Risk 5: Test-suite DB collision produces phantom failures

**Impact:** Every worktree shares Redis DB 15 by default, and concurrent suites
from other checkouts have produced 73-158 phantom failures in this repo before.
A concurrency test that reads back list state is exactly the kind that breaks
under a foreign flush.
**Mitigation:** Pin the DB per the Verification section: `POPOTO_TEST_DB=12` for
the targeted files, `POPOTO_TEST_DB=10` for the full suite. Rationale in that
section. Every new test uses a session id unique to the test so two tests in the
same file cannot share a pending list.

## Race Conditions

The whole issue is a race, so this section is the substance of the plan rather
than a formality.

### Race 1: Two sessions' write hooks run concurrently

**Location:** `src/popoto/integrations/service.py:522` (`_push_pending`),
`:625` (`_pop_pending`)
**Trigger:** A developer runs Claude Code in two terminals. Both sessions fire
`Stop` at the same moment, in separate processes, against one Redis.
**Data prerequisite:** Each session's pending entries must be reachable only by
that session's write hook.
**State prerequisite:** No shared mutable structure between sessions.
**Mitigation:** Already structural — the list key embeds `session_id`
(`$popoto_memory:pending:{agent_id}:{session_id}`), so the two sessions touch
disjoint keys and cannot interact at all. This plan does not change the key.
Test 2 covers it end to end anyway, because "structurally impossible" is a claim
worth a test when the issue is about cross-turn contamination.

### Race 2: Two turns in one session, write hooks out of order

**Location:** `_pop_pending`, the claiming branch
**Trigger:** `Stop` is `"async": true` on the reference wiring. Turn N's write
hook can still be running when turn N+1's fires, and the OS can schedule them in
either order. Today `LPOP` hands whichever arrives first the *oldest* entry.
**Data prerequisite:** The staged entry must record which turn staged it, before
any write hook can run. Guaranteed: the push happens inside the read hook, which
completes before the harness renders the turn.
**State prerequisite:** Exactly one process may claim a given entry.
**Mitigation:** `LREM key 1 <raw element>` is the claim. Order of arrival stops
mattering because each write hook names the entry it wants by value rather than
by position. A write hook that arrives second for a *different* turn removes a
*different* element; `LREM` by value does not care about position.

### Race 3: Two write hooks for the *same* turn (`Stop` plus `SubagentStop`)

**Location:** `_pop_pending`, between the `LRANGE` and the `LREM`
**Trigger:** A session configured with both `Stop` and `SubagentStop` fires both
for one parent turn, both carrying the same `prompt_id`. Both `LRANGE` calls
return the same element; both then attempt to claim it.
**Data prerequisite:** none beyond the staged entry.
**State prerequisite:** At most one outcome report per staged read.
**Mitigation:** `LREM` returns the number of elements removed. The winner gets
`1` and applies the outcome; the loser gets `0` and returns `[]`. The code must
branch on that return value — a `_pop_pending` that ignores `LREM`'s count
reintroduces double-reporting, so it is called out here and asserted by test 4.

### Race 4: `LTRIM` evicts an element between the `LRANGE` and the `LREM`

**Location:** `_pop_pending`, same window as Race 3
**Trigger:** A session at the 32-entry cap. A read hook for a new turn `RPUSH`es
and `LTRIM`s away the oldest entry in the exact window between another process's
`LRANGE` and its `LREM`.
**Data prerequisite:** none.
**State prerequisite:** A claim must never resolve an entry that is no longer
there.
**Mitigation:** `LREM` returns `0` and the report is dropped, which is the
correct outcome — the entry was evicted, so its turn's records are genuinely
unresolvable. This is a `pending_miss`, counted and logged. No misattribution is
possible because `LREM` names the element by value.

### Race 5: A read and a write for the same turn interleave

**Location:** `_push_pending` versus `_pop_pending`
**Trigger:** Hypothetically, a write hook firing before its own read hook
finished pushing.
**Data prerequisite:** The entry must be pushed before it can be claimed.
**State prerequisite:** The harness's own ordering — the pre-turn hook completes
before the model runs, which completes before the post-turn hook fires.
**Mitigation:** Enforced by the harness, not by us. If it were ever violated the
write would find no matching entry, count a `pending_miss`, and report nothing —
the same safe degradation as every other miss. No lock is needed and none is
added.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #552] Verifying the hook-based path against a live OpenClaw run.
  OpenClaw is not installable in the development environment; that is #552's
  entire subject. This plan preserves OpenClaw's current FIFO behavior unchanged
  and makes no new claim about it.
- [EXTERNAL] Capturing a live Codex `user_prompt_submit`/`stop` payload pair to
  upgrade the Codex fixtures from binary-schema to live. Codex's project-level
  hook trust gate silently skips non-approved hooks, and clearing it requires a
  human to run `/hooks` and approve the entry inside an interactive Codex
  session. Recorded as Risk 2 and disclosed in the Codex guide instead.
- [SEPARATE-SLUG #552] Adding turn-id support to Hermes and OpenClaw payload
  shapes beyond the generic probe. Neither harness is documented to send a turn
  identifier; the generic probe would pick one up for free if either adds one.

## Update System

No update-system changes required. This is library-internal: no new dependency,
no new config file, no new service, and no migration. `POPOTO_MEMORY_TURN_KEYED`
is a new *optional* env var with a safe default, so an existing installation that
never sets it gets the new behavior with no action, which is the intent.

Live pending queues need no migration: the reader accepts both element shapes,
and any residue expires within `PENDING_TTL_SECONDS` (3600) regardless.

## Agent Integration

No agent-integration changes required, and this is a deliberate boundary rather
than an omission.

The MCP surface (`src/popoto/integrations/mcp_server.py`) exposes
`memory_search`, `memory_save`, `memory_feedback`, and `memory_status`.
`memory_feedback` resolves a record by an explicit `key` returned from
`memory_search` and routes through `MemoryService.correct()`, which never touches
the pending queue — `MemoryService.search()` deliberately does not stage a
pending entry, because a discretionary search is not a subconscious injection.
None of that changes here.

The integration surface this plan *does* touch is the hook adapter, which is not
an agent-facing tool: it fires because the harness reached a point in its turn
loop, not because a model chose to call it. Its wiring in the four guides is
unchanged — the same `popoto-memory hook` command string, the same eight lines of
Claude Code settings.

- [ ] Confirm by grep that `mcp_server.py` contains no reference to
      `_push_pending`, `_pop_pending`, or `turn_id` after the change. This is an
      anti-criterion in the Verification table.

## Documentation

### Feature Documentation

- [ ] `docs/features/harness-integration.md` — **replace** the "Known gap: the
      read→write handoff is a session-wide FIFO, not a turn ID" section (lines
      111-122) with the shipped contract: turn-keyed by default on Claude Code
      and Codex, FIFO fallback on Hermes and OpenClaw, the miss policy and why
      dropping beats misattributing, and the `POPOTO_MEMORY_TURN_KEYED` kill
      switch. Do not merely edit the heading — the section currently says "not
      fixed in this PR", which becomes false.
- [ ] Same file — add `POPOTO_MEMORY_TURN_KEYED` to the environment variable
      table with its default (`1`) and its purpose.
- [ ] `docs/features/README.md` — check whether the harness-integration entry's
      summary mentions the gap; update if so.

### External Documentation Site

- [ ] `docs/guides/harness-claude-code.md` — note that the outcome handoff is
      keyed on `prompt_id`, which the reference wiring gets for free, and that
      `SubagentStop` now resolves the parent turn once rather than consuming one
      entry per subagent. Add `POPOTO_MEMORY_TURN_KEYED` to its env table.
- [ ] `docs/guides/harness-codex.md` — same, keyed on `turn_id`, with Risk 2's
      disclosure: the field is read from the 0.144.4 binary's schema, and the
      turn-keyed path has not been exercised against a live Codex run.
- [ ] `docs/guides/harness-hermes.md` and `docs/guides/harness-openclaw.md` —
      one paragraph each stating these harnesses send no turn identifier, so the
      handoff uses the session FIFO, with the aborted-turn caveat that still
      applies to them.
- [ ] `mkdocs build --strict` must pass (run via `scripts/ci-local.sh docs`).

### Inline Documentation

- [ ] `_push_pending` docstring — **remove** the `NOTE (PR #546 review, tech
      debt)` block naming #574. Replace it with the entry-encoding contract and
      the both-shapes-readable rule. Leaving the stale NOTE is the single most
      likely documentation miss in this change.
- [ ] `_pop_pending` docstring — document the claiming pop, the `LREM` return
      check, the miss policy, and the bounded upgrade-in-flight fallback.
- [ ] `MemoryService.assemble` and `.feedback` docstrings — document `turn_id`,
      including that `None` means FIFO rather than an error.
- [ ] `hooks.normalize` and the `NormalizedEvent` class docstring — document the
      `turn_id` slot and which harnesses populate it.
- [ ] `MemoryConfig` module docstring env table — add `POPOTO_MEMORY_TURN_KEYED`.

## Success Criteria

- [ ] An outcome report from a turn-id-bearing harness resolves the records that
      turn staged, or resolves nothing — never another turn's records.
- [ ] Interleaved turns from two concurrent sessions each resolve their own
      records, verified by test 2 asserting on record keys rather than counts.
- [ ] An aborted turn leaves later pairings correct (test 3).
- [ ] A second `SubagentStop` for one parent turn resolves nothing and steals
      nothing from a later turn (test 4).
- [ ] Hermes and OpenClaw payloads produce byte-identical behavior to the current
      release (test 5).
- [ ] A pending list staged by the pre-upgrade code is still resolvable after
      upgrade (test 6), and that fallback stops the moment a tagged entry exists
      (test 7).
- [ ] `POPOTO_MEMORY_TURN_KEYED=0` restores the current behavior and the current
      encoding (test 8).
- [ ] The Redis key name, `MAX_PENDING_TURNS`, and `PENDING_TTL_SECONDS` are
      unchanged.
- [ ] The stale `#574` tech-debt NOTE is gone from `_push_pending` and the "Known
      gap" section is gone from the feature doc.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] No xfail conversions needed — `grep -rn 'pytest.mark.xfail\|pytest.xfail('
      tests/` returns nothing related to the pending handoff. Verified at plan
      time; the Verification table re-checks it.

## Team Orchestration

### Team Members

- **Builder (handoff)**
  - Name: `handoff-builder`
  - Role: The service, hooks, config, and demo changes — the whole code change,
    kept in one head because the push and pop encodings must agree.
  - Agent Type: builder
  - Domain: Redis/Popoto data, async/concurrency
  - Resume: true

- **Test engineer (concurrency)**
  - Name: `handoff-tester`
  - Role: The eleven new tests and the eight test updates, especially the
    two-session interleaving matrix.
  - Agent Type: test-engineer
  - Domain: async/concurrency
  - Resume: true

- **Validator**
  - Name: `handoff-validator`
  - Role: Runs the Verification table, confirms the untouched-surface claims by
    grep, and reproduces every metric rather than relaying it.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `handoff-docs`
  - Role: The feature doc, four guides, and the docstring replacements.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Turn-id extraction in the hook adapter

- **Task ID**: build-adapter
- **Depends On**: none
- **Validates**: `tests/test_integrations_hooks.py`
- **Informed By**: spike-1 (both harnesses send the id on both events), spike-2
  (`SubagentStop` carries the parent turn's id)
- **Assigned To**: `handoff-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `turn_id` to `NormalizedEvent.__slots__` and to `__init__` as the **last**
  parameter with a `None` default, so existing positional construction survives.
- In `normalize()`, probe `prompt_id`, `turn_id`, `promptId`, `turnId` in that
  order, reusing the one-level-nested search pattern `_first_string` uses, and
  applying the same `isinstance(value, str)` and `.strip()` truthiness rules.
  Empty or whitespace-only values become `None`.
- Populate `turn_id` on read, write, **and** ignore events — the field is a fact
  about the payload, not about the branch.
- Update the `NormalizedEvent` class docstring and `normalize`'s docstring.

### 2. Config flag and kill switch

- **Task ID**: build-config
- **Depends On**: none
- **Validates**: `tests/test_integrations_service.py`
- **Assigned To**: `handoff-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `turn_keyed: bool` to `MemoryConfig`, read in `from_env` from
  `POPOTO_MEMORY_TURN_KEYED` via the existing `_as_bool` helper, default `True`.
- Add the variable to the module docstring's env table with its default and
  purpose.
- Place the field so no existing positional construction of `MemoryConfig`
  breaks; if the dataclass is constructed positionally anywhere, append rather
  than insert.

### 3. Dual-path pending handoff

- **Task ID**: build-handoff
- **Depends On**: build-config
- **Validates**: `tests/test_integrations_service.py`
- **Informed By**: spike-3 (`LREM`-by-value is the exclusive claim; no Lua)
- **Assigned To**: `handoff-builder`
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: false
- `_push_pending(session_id, records, turn_id=None)`: when turn keying is enabled,
  serialize `{"t": turn_id, "k": keys}` with `json.dumps(..., sort_keys=True,
  separators=(",", ":"))`; when disabled, keep the legacy bare-array shape. The
  `RPUSH`/`LTRIM`/`EXPIRE` pipeline, the key name, the cap, and the TTL do not
  change.
- `_pop_pending(session_id, turn_id=None)`: with a turn id and turn keying on,
  `LRANGE key 0 -1`, find the first element whose `t` matches, `LREM key 1 <that
  exact raw element>`, and **return `[]` unless `LREM` returned `>= 1`**. Pass
  `LREM` the bytes `LRANGE` returned, never a re-serialization.
- Decode tolerantly through **one shared decode step used by both branches** —
  the `LPOP` fallback branch must not keep the old bare-array parser. A `list`
  element is legacy (untagged), a `dict` is tagged, anything else is corrupt: log
  via `_record_failure("pending_pop", ...)` and `LREM` it out of the way so it
  cannot jam later claims. See the shared-decode snippet in Technical Approach.
  Skipping this is the blocker the critique caught: it zeroes outcome reporting
  for Hermes and OpenClaw on the default configuration, silently.
- Make the push idempotent per turn. When `turn_id` is truthy and turn keying is
  on, before the `RPUSH`/`LTRIM`/`EXPIRE` pipeline, `LRANGE` the list, decode
  tolerantly, and return early if any element is a dict whose `t` equals this
  turn id. Do not raise and do not count it as a failure — a re-fired read hook
  is not an error. The check is **advisory, not atomic**: two concurrent pushes
  for one turn can both pass it. That is acceptable because the residual window
  is far narrower than today's unconditional double-push, and because the harm is
  bounded to double-applying an outcome to the *correct* turn's records. Say so
  in the docstring rather than implying exclusivity.
- Implement the bounded upgrade-in-flight fallback: fall back to `LPOP` only when
  the turn id matched nothing **and** every element is a bare `list` (the `t` key
  is absent entirely). Never read the predicate as "`t` is falsy" — an untagged
  harness's entry carries `t: null` under default-on keying, and treating that as
  legacy re-enables positional popping for exactly the harnesses the fallback is
  supposed to leave alone.
- **Layout invariant**: keep the `# -- per-session injection suppression` block
  physically between `_push_pending` and `_pop_pending`. The suppression
  anti-criterion in the Verification table reads that region, and colocating the
  two handoff functions would silently break its range.
- Count a miss as `_record_failure("pending_miss", ...)`, a counter name distinct
  from `pending_pop`, so a dropped report is diagnosable in `doctor` and is never
  confused with a Redis error.
- Thread `turn_id` through `MemoryService.assemble(query, session_id=None,
  turn_id=None)` and `MemoryService.feedback(session_id, outcome="used",
  turn_id=None)`, both keyword-optional so no existing call site changes.
- Rewrite the `_push_pending` docstring: **delete** the `NOTE (PR #546 review,
  tech debt)` block referencing #574, and document the encoding contract.
- Do not touch `_mark_injected`, `_injected_keys`, or `search`.

### 4. Wire the adapter to the service, and the demo to the default path

- **Task ID**: build-wiring
- **Depends On**: build-adapter, build-handoff
- **Validates**: `tests/test_integrations_hooks.py`, `tests/test_integrations_cli.py`
- **Assigned To**: `handoff-builder`
- **Agent Type**: builder
- **Parallel**: false
- In `handle_payload`, pass `turn_id=event.turn_id` to both `service.assemble(...)`
  and `service.feedback(...)`.
- In `demo.py`, pass a `turn_id` on both the assemble and the feedback call so the
  runnable example demonstrates the default path rather than the fallback. Adjust
  only the code, not the printed narrative, unless a printed line becomes
  inaccurate.
- Confirm `mcp_server.py` and `cli.py` need no change (`cli.py:349` calls
  `assemble(..., session_id=None)`, which stages nothing).

### 5. Concurrency and fallback test matrix

- **Task ID**: build-tests
- **Depends On**: build-wiring
- **Validates**: `tests/test_integrations_service.py`, `tests/test_integrations_hooks.py`
- **Informed By**: Race 1-5, Test Impact
- **Assigned To**: `handoff-tester`
- **Agent Type**: test-engineer
- **Domain**: async/concurrency, Redis/Popoto data
- **Parallel**: false
- Write the eleven new tests listed under Test Impact. Test 2 (two concurrent
  sessions, interleaved staging and scrambled reporting) is the acceptance test
  for this issue and must assert on **record keys**, not counts — a count-only
  assertion passes under the very misattribution being fixed.
- Apply the eight listed test updates, including the `feedback` stub signature in
  `test_write_path_reports_used_not_acted`.
- Give every test a session id unique to that test so no two tests share a
  pending list.
- Assert the untouched surfaces: key name, `MAX_PENDING_TURNS`, and
  `PENDING_TTL_SECONDS` still hold for tagged entries.

### 6. Validation

- **Task ID**: validate-handoff
- **Depends On**: build-tests
- **Assigned To**: `handoff-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table and report each result with the
  environment stated alongside it (redis-py version, test DB, which checkout the
  editable install resolves to).
- Reproduce any metric before relaying it. Do not relay a subagent's number.
- Confirm by grep that the stale `#574` NOTE and the feature doc's "Known gap"
  section are gone.

### 7. Documentation

- **Task ID**: document-feature
- **Depends On**: validate-handoff
- **Assigned To**: `handoff-docs`
- **Agent Type**: documentarian
- **Parallel**: false
- Everything in the Documentation section: the feature doc's replaced section and
  env table, the four guides, and `docs/features/README.md`.
- Run `mkdocs build --strict`.

### 8. Final validation

- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: `handoff-validator`
- **Agent Type**: validator
- **Parallel**: false
- Full Verification table, full suite on `POPOTO_TEST_DB=10`, docs build, and
  every Success Criteria box.

## Verification

**Test database, and why two of them.** Targeted runs use `POPOTO_TEST_DB=12`.
The full suite must **not**: `tests/test_pytest_plugin.py` sets
`_ENV_OVERRIDE_CHILD_DB = 12` and spawns a child pytest that flushes DB 12, and
the test asserts the parent session is not on that DB — so a full run on 12 fails
loudly by design (`test_pytest_plugin.py:1056`). DB 14 is `_INERT_PROBE_DB` and
15 is the repo default, both of which concurrent worktrees contend for. The full
suite therefore runs on `POPOTO_TEST_DB=10`, which no test reserves. Never DB 0 —
it is the live agent store on this machine.

**mypy baseline — measured in two environments**, because the count is
redis-py-version-dependent (CLAUDE.md, gotcha 5):

| Environment | Errors in `src/popoto/integrations/` |
|---|---|
| redis-py 7.1.1, at `b8e1dc4` | 1 — `config.py:301` `Item "Awaitable[Any]" of "Awaitable[Any] \| Any" has no attribute "__iter__"`, the redis-py-7.x-only union-narrowing artifact |
| redis-py 8.1.0, mypy 2.3.1, Python 3.13.2, this worktree | 0 — 8.x narrows the union, so the artifact disappears |

The gate below is `<= 1` for the integrations package precisely so it is
satisfiable in both, and it is scoped to the touched package rather than the tree
so unrelated drift cannot mask a regression here. **State the redis-py version
alongside any count reported against this row.**

| Check | Command | Expected |
|-------|---------|----------|
| Targeted tests pass | `POPOTO_TEST_DB=12 python -m pytest tests/test_integrations_service.py tests/test_integrations_hooks.py -q` | exit code 0 |
| Two-session interleave test exists and passes | `POPOTO_TEST_DB=12 python -m pytest tests/test_integrations_service.py -q -k "interleaved_turns_from_two_sessions"` | exit code 0 |
| Subagent double-report test passes | `POPOTO_TEST_DB=12 python -m pytest tests/test_integrations_service.py -q -k "subagent_stop_resolves_the_parent_turn_once"` | exit code 0 |
| Untagged FIFO fallback preserved | `POPOTO_TEST_DB=12 python -m pytest tests/test_integrations_service.py -q -k "untagged_harness_keeps_fifo_order"` | exit code 0 |
| Kill switch restores legacy behavior | `POPOTO_TEST_DB=12 python -m pytest tests/test_integrations_service.py -q -k "turn_keyed_kill_switch_restores_fifo"` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=10 python -m pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| mypy no worse in integrations | `test $(python -m mypy src/ 2>&1 \| grep -c '^src/popoto/integrations/.*error:') -le 1` | exit code 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |
| Kill switch is documented | `grep -rn "POPOTO_MEMORY_TURN_KEYED" docs/features/harness-integration.md src/popoto/integrations/config.py` | exit code 0 |
| Stale #574 tech-debt NOTE removed | `test $(grep -c "PR #546 review, tech debt" src/popoto/integrations/service.py) -eq 0` | exit code 0 |
| Feature doc "Known gap" section removed | `test $(grep -c "Known gap: the read" docs/features/harness-integration.md) -eq 0` | exit code 0 |
| Pending key name unchanged | `test $(grep -c 'PENDING_KEY_PREFIX = "\$popoto_memory:pending"' src/popoto/integrations/service.py) -eq 1` | exit code 0 |
| Cap and TTL unchanged | `test $(grep -c "MAX_PENDING_TURNS = 32" src/popoto/integrations/service.py) -eq 1 && test $(grep -c "PENDING_TTL_SECONDS = 3600" src/popoto/integrations/config.py) -eq 1` | exit code 0 |
| Anti-criterion: no Lua in the handoff | `test $(grep -c "register_script\|EVALSHA\|evalsha\|\.eval(" src/popoto/integrations/service.py) -eq 0` | exit code 0 |
| Anti-criterion: MCP surface untouched by the handoff | `test $(grep -c "_push_pending\|_pop_pending\|turn_id" src/popoto/integrations/mcp_server.py) -eq 0` | exit code 0 |
| Anti-criterion: suppression SET not keyed per turn | `test $(awk '/def _injected_key\(\|def _injected_keys\(\|def _mark_injected\(/,/^    def [a-z_]+\(self/' src/popoto/integrations/service.py \| grep -c "turn_id") -eq 0` | exit code 0 |
| No stale xfails introduced | `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/test_integrations_service.py tests/test_integrations_hooks.py` | exit code 1 |

## Critique Results

**Verdict: NEEDS REVISION** (1 blocker, 4 concerns, 1 nit) — war room run 2026-09-04, FULL depth, critics: Risk & Robustness, Scope & Value, History & Consistency.

Environment for every measured number below: redis-py **8.1.0**, mypy **2.3.1**, Python **3.13.2**, Redis on localhost:6379, editable install resolving to `.worktrees/sdlc-574/src/popoto/__init__.py`; targeted tests on `POPOTO_TEST_DB=12`.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness, Scope & Value | **The untagged-harness pop branch mis-parses the new tagged element shape.** Technical Approach makes `_push_pending` write `{"t": turn_id, "k": keys}` whenever `turn_keyed` is on (the default) — including when `turn_id` is `None`, so Hermes and OpenClaw get `{"t":null,"k":[...]}`. But Data Flow step 11 specifies the no-turn-id branch as "`LPOP key`, exactly as today", and today's reader (`service.py:634-635`) is `keys = json.loads(raw); return [k for k in keys if isinstance(k, str)]`. Iterating a dict yields its keys, so that returns the literal `["t","k"]` — verified: `json.loads(json.dumps({"t":None,"k":["A:1"]}))` filtered that way gives `['t','k']`. `get_many` then resolves nothing and `feedback` returns 0 for **every** untagged-harness turn on the default configuration. This falsifies the Success Criterion "Hermes and OpenClaw payloads produce byte-identical behavior to the current release (test 5)", and it is the same silent-dead-queue shape the plan's own Risk 3 reserves for a revert — except it fires forward, by default, on ship. | build-handoff (task 3), build-tests (task 5) | Delete the phrase "exactly as today" from Data Flow step 11 and from task 3. The tolerant decode is **one** step shared by both branches, applied after the element is fetched by either `LPOP` or `LRANGE`: `elem = json.loads(raw)`; `keys = elem["k"] if isinstance(elem, dict) else (elem if isinstance(elem, list) else None)`; `None` → corrupt, `_record_failure("pending_pop", ...)`, skip. Then filter `[k for k in keys if isinstance(k, str)]`. Separately, pin the meaning of the upgrade-fallback predicate: "no element carries a `t`" must mean **the `t` key is absent from the decoded object** (i.e. the element is a bare `list`), NOT "`t` is falsy" — under default-on, untagged-harness elements carry `t: null`, so a value-truthiness reading would wrongly re-enable the LPOP fallback for them. Test 5 must assert the **returned record keys equal the seeded keys**, not just ordering or counts; a count-only assertion passes with `['t','k']` returned. |
| CONCERN | Risk & Robustness | **A duplicate push for one turn defeats the `LREM` exclusivity claim.** spike-3 asserts each element's bytes are unique within the list because it embeds a unique turn id. Nothing enforces one push per turn: `assemble()` calls `self._push_pending(session_id, result.records)` unconditionally at `service.py:238` with no same-turn check. A re-fired read hook for one turn (duplicated hook config, harness retry) can RPUSH two byte-identical elements — `sort_keys=True, separators=(",",":")` guarantees identical bytes for identical keys. Two write events for that turn then each `LREM key 1 <element>` successfully and each returns `1`, so both apply the outcome; Race 3's count check does not catch it because it guards duplicate *pops* of one push, not duplicate *pushes*. Reachability is partly damped by the `_mark_injected` suppression SET (a second `assemble` for the same turn would normally exclude the already-injected keys and so stage different bytes or nothing), and the harm is bounded to double-applying an outcome to the **correct** turn's records rather than misattributing to another turn — hence CONCERN, not BLOCKER. | build-handoff (task 3), build-tests (task 5) | In `_push_pending`, when `turn_id` is truthy and turn keying is on, make the push idempotent per turn before the `RPUSH`/`LTRIM`/`EXPIRE` pipeline: `for raw in self.redis.lrange(redis_key, 0, -1):` decode tolerantly and `if isinstance(elem, dict) and elem.get("t") == turn_id: return`. Do not raise and do not count it as a failure — a re-fired read is not an error. Note the check is advisory, not atomic (two concurrent pushes for one turn can both pass it); that is acceptable because the residual window is far narrower than today's unconditional double-push. Add `test_duplicate_push_for_same_turn_stages_one_claimable_entry`: call `_push_pending` twice with the same `session_id`/`turn_id`/records, assert `LLEN == 1`, then two reports for that turn — first returns the record count, second returns `0`. |
| CONCERN | History & Consistency | **Five Verification rows hit the `grep -c` exit-code-1-on-zero-matches trap that bit issue #551's plan.** `grep -c` prints `0` but exits `1` when there are no matches, so a runner checking exit codes reports a false failure on the *correct* outcome. Reproduced live in this worktree today: `grep -c "register_script\|EVALSHA\|evalsha\|\.eval(" src/popoto/integrations/service.py` → stdout `0`, **rc=1**; the MCP-untouched row → stdout `0`, **rc=1**; the suppression-SET `sed` row → stdout `0`, **rc=1**. All three already "fail" on the pristine pre-build tree. The "Stale #574 tech-debt NOTE removed" row currently prints `1` (rc=0) and the "Feature doc Known gap removed" row likewise — both will flip from pass to false-fail the moment the builder and documentarian correctly do their jobs. The "Cap and TTL unchanged" row (`grep -c ...; grep -c ...`) inherits only the second grep's exit code, so a regression in `MAX_PENDING_TURNS` would be invisible to an exit-code check. | validate-handoff (task 6), validate-all (task 8) | Rewrite each affected row so the assertion is on the count, not on grep's status, using the `test $(...) -eq 0` form the mypy row already uses correctly: anti-Lua → `test $(grep -c "register_script\|EVALSHA\|evalsha\|\.eval(" src/popoto/integrations/service.py) -eq 0`; MCP-untouched → `test $(grep -c "_push_pending\|_pop_pending\|turn_id" src/popoto/integrations/mcp_server.py) -eq 0`; stale NOTE → `test $(grep -c "PR #546 review, tech debt" src/popoto/integrations/service.py) -eq 0`; Known gap → `test $(grep -c "Known gap: the read" docs/features/harness-integration.md) -eq 0`; cap/TTL → `test $(grep -c "MAX_PENDING_TURNS = 32" src/popoto/integrations/service.py) -eq 1 && test $(grep -c "PENDING_TTL_SECONDS = 3600" src/popoto/integrations/config.py) -eq 1`. Set every affected row's Expected column to `exit code 0`. Leave the "No stale xfails introduced" row alone — it expects `exit code 1` and is already correct. |
| CONCERN | History & Consistency | **The suppression-SET anti-criterion `sed` range breaks under the refactor the plan itself directs.** The row `sed -n '/# -- per-session injection suppression/,/def _pop_pending/p' ... \| sed '$d' \| grep -c "turn_id"` works only because of the current incidental layout: `_push_pending` at `service.py:522`, the suppression marker at `:566`, `_pop_pending` at `:625`. Task build-handoff rewrites `_push_pending` and `_pop_pending` together and the natural refactor colocates them; if `def _pop_pending` no longer appears *after* the marker, `sed` finds no end pattern, prints to EOF, and the row silently captures the new `turn_id`-bearing code — turning a green anti-criterion into a meaningless one or a spurious failure, with no signal that the range broke. | build-handoff (task 3), validate-handoff (task 6) | Replace the fragile range with a self-delimiting one that does not depend on function ordering: `test $(sed -n '/# -- per-session injection suppression/,/^    # -- /{/^    # -- per-session/!{/^    # -- /q};p}' src/popoto/integrations/service.py \| grep -c "turn_id") -eq 0`. Simpler and preferred: assert on the three suppression functions by name instead of on a line range — `test $(awk '/def _injected_key\(|def _injected_keys\(|def _mark_injected\(/,/^    def [a-z_]+\(self/' src/popoto/integrations/service.py \| grep -c "turn_id") -eq 0`. Whichever form is chosen, add an explicit instruction to task 3: **keep the `# -- per-session injection suppression` block physically between `_push_pending` and `_pop_pending`**, so the row's premise is a stated invariant rather than an accident of the current file. |
| CONCERN | Scope & Value | **A cited test to be updated does not exist.** Test Impact item 1 directs the builder to UPDATE `tests/test_integrations_service.py::test_pending_list_survives_out_of_order_writes` "around line 280". `grep -rn "def test_pending_list_survives_out_of_order_writes" tests/` returns nothing (rc=1). The test actually at that location is `test_interleaved_turns_do_not_cross_report`, at `tests/test_integrations_service.py:276`. A builder taking the plan literally will either create a new test under the wrong name or skip the update, leaving the FIFO-ordering regression guard un-threaded. The plan's seven other UPDATE citations were verified present: `test_feedback_reports_against_the_turn_that_was_injected:228`, `test_feedback_without_a_pending_turn_is_a_no_op:265`, `test_pending_list_is_capped:297`, `test_pending_key_has_a_ttl:316`, `test_feedback_degrades_quietly_when_redis_is_down:405`. | build-tests (task 5) | Correct Test Impact item 1 to name `test_interleaved_turns_do_not_cross_report` at `tests/test_integrations_service.py:276`. Note its current body asserts only `first != second` plus a final empty pop — an inequality assertion that passes under misattribution — so when threading `turn_id=None` through it, also strengthen it to assert the exact resolved record keys. Before starting task 5, run `grep -n "^def test_" tests/test_integrations_service.py tests/test_integrations_hooks.py` and re-verify every cited name against the real files rather than the plan's line numbers. |
| NIT | History & Consistency | **The stated mypy baseline is stale for the environment the gate will run in.** The Verification preamble records "exactly 1 error, `config.py:301`" measured on redis-py 7.1.1. Re-measured in this worktree — redis-py **8.1.0**, mypy **2.3.1**, Python **3.13.2**, editable install resolving to `.worktrees/sdlc-574/src/popoto/__init__.py` — `python -m mypy src/ 2>&1 \| grep -c '^src/popoto/integrations/.*error:'` returns **0**, not 1. The gate itself is fine: `test $(...) -le 1` exits 0 at both counts, exactly as the plan intended when it chose `<= 1` for 7.x/8.x portability. Only the prose number is stale. | — | Amend the preamble to state both measurements: 1 error on redis-py 7.1.1 (`config.py:301`, the union-narrowing artifact), 0 on redis-py 8.1.0 with mypy 2.3.1 — and keep the `-le 1` threshold, which is what makes the row satisfiable in both environments. |

### Revision (round 1 of 1, applied 2026-09-04)

One round only, as scoped. All six findings are resolved in-plan; no second critique.

- **BLOCKER (untagged pop mis-parses the tagged shape)** — Technical Approach now specifies **one shared tolerant decode** used by the `LPOP` fallback branch and the `LRANGE` claiming branch alike, with the snippet inline. Data Flow step 11 no longer says "exactly as today" and states the failure it would cause. Task 3 carries the same rule and names it as the blocker. Test 5 must now assert resolved record keys, since a count-only assertion passes while returning `["t","k"]`.
- **BLOCKER, second half (fallback predicate)** — "no element carries a `t`" is pinned to mean the `t` key is **absent** (a bare `list`), never "`t` is falsy". Stated in both the miss-policy prose and task 3, with the reason: an untagged entry carries `t: null` under default-on keying.
- **CONCERN (duplicate push defeats LREM exclusivity)** — task 3 gains an advisory same-turn check before the push pipeline, explicitly documented as non-atomic rather than implying exclusivity. New test 12 pins it.
- **CONCERN (grep exit-code trap, five rows)** — every affected row is rewritten in the `test $(grep -c ...) -eq N` form with Expected set to `exit code 0`. The cap-and-TTL row now checks both greps instead of inheriting only the second one's status. The xfail row is untouched: it expects exit code 1 and was already correct.
- **CONCERN (fragile suppression `sed` range)** — replaced with a function-name-scoped `awk` range that does not depend on function ordering, plus a stated layout invariant in task 3 keeping the suppression block between the two handoff functions.
- **CONCERN (cited test does not exist)** — Test Impact item 1 now names `test_interleaved_turns_do_not_cross_report` at `tests/test_integrations_service.py:276`, records that the old name was a plan error, and directs the builder to re-verify every cited test name by grep before task 5. It also strengthens that test, whose `first != second` assertion passes under misattribution.
- **NIT (stale mypy baseline)** — the preamble now gives both measurements in a table, 1 error on redis-py 7.1.1 and 0 on 8.1.0 with mypy 2.3.1, and requires the redis-py version to be stated alongside any reported count. The `-le 1` threshold is unchanged and satisfiable in both.

Status is Ready for build.
