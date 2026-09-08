---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/699
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-08T05:54:25Z
---

# #699 — Make the cycles/pressure companion read-modify-write atomic

## Problem

A record's cycle amplitudes are learned state: `strengthen_cycle()` /
`weaken_cycle()` multiply them, and `save()` merges them against the class
declaration. Both operations are **client-side read-modify-write cycles over
the same hash field** — `HGET`, decide in Python, `HSET` the whole entry back.

```python
# thread A                              # thread B
doc.save()                              doc.strengthen_cycle("relevance", 1.2)
#  HGET cycles m -> [[W, 2.0, 0, 2.0]]
#                                       #  HGET cycles m -> [[W, 2.0, 0, 2.0]]
#                                       #  HSET cycles m    [[W, 2.4, 0, 2.0]]
#  HSET cycles m    [[W, 2.0, 0, 2.0]]  <- the 2.4 is gone
```

An agent that reinforces a rhythm while any other writer saves the same record
silently loses the reinforcement. No exception, no log line, no signal at all —
the amplitude simply does not move, and whether it moves depends on interleaving.

**Current behavior:**

- `CyclicDecayField.on_save` — `cyclic_decay_field.py:623` (`HGET` cycles) and
  `:710` (`HSET` cycles); `:719` (`HGET` pressure) and `:724`/`:731` (`HSET`
  pressure). The reads always go straight to the client; the writes go through
  the caller's pipeline when one was passed.
- `Model._adjust_cycle_amplitudes` — `models/base.py:2749` (`HGET`) and
  `:2781`/`:2784` (`HSET`), behind `strengthen_cycle()` / `weaken_cycle()`.

Because the reads bypass the pipeline while the writes do not, the two writers
lose updates to each other *even inside one pipeline* — a hazard so real it is
documented as a usage rule rather than fixed, at `cyclic_decay_field.py:590-593`
and `docs/features/cyclic-decay-field.md:169-178` ("queue the **save first**").

**Desired outcome:**

Every read-modify-write of the cycles and pressure companion hashes happens
**inside one server-side Lua script**, so two concurrent writers serialize
instead of clobbering. The `#698` three-way merge rule and its reset log
survive unchanged in observable behavior; the "queue the save first" caveat
is deleted from both the docstring and the docs page because it stops being
true.

## Freshness Check

**Baseline commit:** `9986c086` (`origin/main`, `fix(#689): stop shipping tests/ in the sdist (#703)`)
**Issue filed at:** 2026-09-07T11:41:23Z
**Disposition:** Minor drift

**File:line references re-verified:**

| Issue's claim | Status at `9986c086` |
|---|---|
| `cyclic_decay_field.py:560-610` — `on_save` `hget` → merge → `hset` | **Drifted.** #698 landed after the issue was filed and grew the merge. `on_save` is now `:542-736`; the cycles `HGET` is `:623`, the cycles `HSET` `:710`, the pressure `HGET` `:719`, the pressure `HSET`s `:724`/`:731`. The claim — client-side RMW — still holds exactly. |
| `models/base.py:2734-2762` — `_adjust_cycle_amplitudes` `hget` → multiply → `hset` | **Drifted.** Now `:2703-2789`; `HGET` at `:2749`, `HSET` at `:2781` (pipeline) / `:2784` (direct). Claim holds. |
| `cyclic_decay_field.py:533-536` — the pipeline contract | **Drifted** to `:590-593`. Text unchanged. Mirrored in `docs/features/cyclic-decay-field.md:169-178`. |

**Cited sibling issues/PRs re-checked:**

- **#679 / PR #687** — merged 2026-09-07T11:20Z, before the issue was filed. Still the merge rule being raced against.
- **#698 / PR #700** — merged 2026-09-07T23:16Z (`c16faf8c`), **after** the issue was filed. It added the 4th `declared_baseline` slot to each cycles entry and a `logger.info` reset line. This is the one material change: the payload the script must merge is now a 4-tuple, not a 3-tuple, and there is now a *decision to report back to Python* (the reset log), which constrains the design (see Solution). It did not address atomicity, exactly as the issue says.

**Commits on main since issue was filed (touching referenced files):**

- `c16faf8c` — `CyclicDecayField: an edited declared amplitude can now override an already-learned one (#698) (#700)` — **changed the payload and added a log**, folded into this plan. No other commit touched either file.

**Active plans in `docs/plans/` overlapping this area:** `sdlc-698.md` (status `Ready`, shipped as `c16faf8c`) — merged, not active. No open plan touches these files.

**Bug still reproducible:** yes, by inspection of the code path — the `HGET` at `cyclic_decay_field.py:623` and the `HSET` at `:710` are separate round trips with no CAS, and the same at `base.py:2749`/`:2784`. Spike-3 exercises it against a live server.

## Prior Art

- **PR #687 (#679)** — *preserve learned cycle amplitudes across save*. Made `on_save` merge instead of overwrite. Introduced the client-side read this races against; did not claim atomicity.
- **PR #700 (#698)** — *an edited declared amplitude can override an already-learned one*. Added the `declared_baseline` 4th slot **inside the same entry**, explicitly so no second key could tear independently, and explicitly deferred atomicity to this issue.
- **PR #594 / #588 (`SUPERSEDE_LUA`)** — the closest precedent for the shape of this fix: a membership guard that had to move *into* the Lua script because a client-side check plus a server-side write is not one operation. The maintainer decision there ("patch options rejected, root fix demanded") is the governing precedent for preferring the script over a client-side lock.
- **`CAPPED_BAYESIAN_UPDATE_LUA` (`fields/confidence_field.py:63-120`)** and **`RESOLVE_PREDICTION_LUA` (`fields/prediction_ledger.py:52-83`)** — existing `HGET` → `cmsgpack.unpack` → mutate → `cmsgpack.pack` → `HSET` scripts in this repo. This plan copies their structure; they are the reference implementations.
- **#476 / `base.py:1773-1786`** — indexed/unique `on_save` hooks were made **eager direct EVALs** rather than pipeline-queued, precisely because a queued script cannot report its outcome in time to change the caller's behavior. That is the shape precedent for the placement decision in Solution — but note it applies **only** to the internal-pipeline `else:` branch (`:1760`+); the caller-supplied-pipeline branch queues indexed `on_save` through the generic loop at `:1720-1729`. See Technical Approach → Placement.
- No prior attempt to fix *this* race exists. Nothing to analyze under "Why Previous Fixes Failed" — the two prior PRs are the code being made atomic, not failed fixes of it.

## Research

**Queries used:**

- `Valkey Lua scripting cmsgpack library available EVAL 8.x`

**Key findings:**

- `cmsgpack` is a **built-in Lua library**, not a Redis module, and Valkey's Lua API reference lists it as available in both scripts and functions — https://valkey.io/topics/lua-api/. This satisfies the repo's hard Valkey-compatibility constraint (no `BF.*`/`CMS.*`-style module dependencies) and is already relied on by nine existing popoto scripts.
- **Valkey 8.0 evicts EVAL-loaded scripts LRU** (`evicted_scripts` in `INFO stats`) — https://valkey.io/topics/eval-intro/. `redis_db.run_lua` already handles this in both of its paths: the direct path uses redis-py's `Script`, which retries after `NOSCRIPT`, and the pipeline path uses an explicit `SCRIPT LOAD` whose failure surfaces as `NoScriptError` from `execute()` and self-heals on the next direct call. No new work; noted so a reviewer does not re-litigate it.

## Spike Results

All three spikes ran against the live server on this machine (Redis 8.6.2,
`REDIS_URL=redis://localhost:6379/9`, redis-py from `.venv`). Script:
`scratchpad/sdlc-699-spike-cmsgpack.py` (throwaway; not committed).

### spike-1: cmsgpack round-trips popoto's cycles payload
- **Assumption**: "A Lua script can decode, mutate and re-encode the exact msgpack payload `on_save` writes, without the Python side needing a format change."
- **Method**: prototype (live EVAL)
- **Finding**: Confirmed. `msgpack.packb([[86400, 5.0, 0, 5.0], [604800, 1.25, 3.5, 2.0]])` → `cmsgpack.unpack` gives a Lua array of arrays with `type()=='number'` in every numeric slot; a 4-slot entry survives with the 4th slot untouched. A **string** period (`"daily"`) decodes as a Lua string, so the script must not assume periods are numeric.
- **Confidence**: high
- **Impact on plan**: The script can own the whole merge. Period matching needs a type-safe key function (see Technical Approach), not `==` on raw values.

### spike-2: the Lua re-encode changes numeric *types*
- **Assumption**: "Re-packing in Lua preserves the values the Python side reads back."
- **Method**: prototype (live EVAL)
- **Finding**: Values yes, **types no**. Lua 5.1 has one number type, and `cmsgpack.pack` emits an msgpack *integer* for any integral value. Measured: `[[86400, 5.0, 0, 5.0]]` doubled in Lua came back to Python as `[86400, 10, 0, 5]` — `int`, not `float` — while the non-integral `1.25 * 2 = 2.5` stayed `float`. Float precision itself is exact: a pressure dict round-tripped `last_resolved = 1757000000.123456` bit-for-bit (`== True`), so the *pressure* fix loses nothing.
- **Confidence**: high
- **Impact on plan**: A real behavior change at the Python read boundary. Mitigated by coercing at the two places a value surfaces (`export_state`, the `strengthen_cycle`/`weaken_cycle` return) — see Risk 1. Equality semantics are unaffected: `5 == 5.0` is `True` in both languages, so #698's exact-equality baseline comparison keeps working.

### spike-3: the race is real on current main
- **Assumption**: "The lost update is reachable, not merely theoretical."
- **Method**: code-read of `cyclic_decay_field.py:623-710` and `base.py:2749-2784` at `9986c086`
- **Finding**: Confirmed by construction — two unsynchronized round trips per writer, no CAS, no lock, no script. The build must turn this into an executed red-state proof: the concurrency test in Task 4 is required to **fail against pre-fix code** before it is accepted as passing after.
- **Confidence**: high
- **Impact on plan**: Success criteria require the red-state paste, not just a green run.

### spike-4: what shape does the pressure hash actually have?
- **Assumption** (the issue's third open question): "The same race probably exists on the pressure companion hash — check, don't assume."
- **Method**: code-read of `base.py:2590-2653` (`resolve_pressure`) and `cyclic_decay_field.py:715-734`
- **Finding**: **Not the same shape — asymmetric.** `resolve_pressure` is a *blind write*: it builds `{"rate": field.pressure_rate, "last_resolved": now}` from the declaration and issues a single `HSET`. It never reads. `on_save` is the only reader-then-writer (`HGET` at `:719` → set `rate` → `HSET`). So there is exactly one lost-update ordering: `on_save` reads, `resolve_pressure` writes a fresh `last_resolved`, `on_save` writes back the stale one — the discharge is silently undone and the record keeps accumulating urgency it already paid off. Fixing the `on_save` side alone closes it completely, because a lone `HSET` is already atomic.
- **Confidence**: high
- **Impact on plan**: `resolve_pressure` is **not** modified. This answers open question 3 with evidence rather than the issue's guess.

## Data Flow

1. **Entry point A — `model.save()`** → `Model.save` (`base.py:1721` pipeline path / `:1830` internal-pipeline path) → `CyclicDecayField.on_save(pipeline=<pipeline>)`. Note the internal-pipeline path is the default, so **`on_save` almost always receives a pipeline**.
2. `on_save` → `super().on_save()` queues the `ZADD` on the pipeline (unchanged) → **today** reads cycles + pressure directly from the client, merges in Python, queues the `HSET`s on the pipeline. **After this plan**: one eager `EVAL` that does the read, the merge and the write server-side, and returns a decision report.
3. **Entry point B — `model.strengthen_cycle()` / `weaken_cycle()`** → `Model._adjust_cycle_amplitudes` (`base.py:2703`) → today `HGET` + Python multiply + `HSET`; after, one `EVAL` (queued when a pipeline is passed, direct otherwise).
4. **Entry point C — `model.resolve_pressure()`** → single blind `HSET`. Unchanged.
5. **Reader — query scoring** → `CYCLIC_DECAY_LUA` (`cyclic_decay_field.py:69`) `HGET`s both companion hashes per member inside the scoring script. It only ever reads, and tolerates entries of any arity (`c[3] or 0`), so nothing about it changes.
6. **Output**: the cycles hash entry `[period, amplitude, phase, declared_baseline]` and the pressure entry `{rate, last_resolved}` — same shapes as today, now written under serialization.

## Architectural Impact

- **New dependencies**: none. `cmsgpack` is built into the Lua interpreter Redis and Valkey both ship; `run_lua` / `lua_script` already exist.
- **Interface changes**: no public signature changes. `strengthen_cycle`/`weaken_cycle`/`save` keep their arguments and return contracts. One *documented* contract is deleted (the "queue save first" pipeline rule) because the hazard it warned about ceases to exist.
- **One observable behavior change the "return contracts" line does not cover
  (critique C6): the pipelined no-entry call now queues a command where it queued
  none.** At `base.py:2749-2754` the existence check is a **direct** `HGET`, and on a
  miss `_adjust_cycle_amplitudes` returns `pipeline` immediately without touching it.
  After the port the existence check moves inside the script, so `run_lua(pipeline, …)`
  queues an `EVALSHA` unconditionally. The Python-visible *return value* is identical
  (still the pipeline), but a caller who does
  `record.strengthen_cycle(..., pipeline=pipe)` against a member with **no stored
  cycles entry** and then indexes `pipe.execute()` sees one extra result and every
  later position shifted by one. This is accepted, not fixed — the queued command is
  what makes the operation atomic, and suppressing it would reintroduce the
  client-side existence check this plan exists to remove. It is disclosed here, carried
  as a step in Task 2, and pinned by a test (Test Impact,
  `tests/test_observation_protocol.py:693-701`) so it is recorded rather than
  discovered.
- **Behavioral change at the seam**: `on_save`'s companion-hash writes stop being queued on the caller's pipeline and execute eagerly, in **both** the internal-pipeline and the caller-supplied-pipeline branches of `Model.save` (see Solution → Placement, and Risk 2). #476's eager indexed-field EVALs (`base.py:1773-1786`) are the shape precedent but cover only the internal-pipeline branch; extending it to the caller-pipeline branch is new and is Open Question 1.
- **Coupling**: slightly increased — the #698 merge rule moves from Python into Lua, so the rule now has one authoritative implementation in a language with no test-time introspection. Mitigated by keeping the *decision report* in the return value so Python tests can assert on decisions, not just outcomes.
- **Data ownership**: unchanged; the field still owns both companion hashes.
- **Reversibility**: high. Each script is additive; reverting the two call sites restores the old code with no data migration — the stored payload shape is identical before and after.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (the eager-write placement decision is the one thing worth confirming)
- Review rounds: 2 (Lua correctness against the #698 merge rule is not obvious-on-sight)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -u redis://localhost:6379/9 ping` | The suite and every spike need a live server |
| `cmsgpack` in the server's Lua | `redis-cli -u redis://localhost:6379/9 eval "return type(cmsgpack)" 0` → `table` | The whole approach depends on it |
| Lane test DB bound | `test "$POPOTO_TEST_DB" = "9"` | DB 15 is shared across worktrees; DB 0 is the live store |
| Dev extras installed | `python -c "import msgpack, redis, pytest"` | A `.[dev]`-only venv deselects ~95 tests |

## Solution

### Key Elements

- **`CYCLES_MERGE_LUA`** (new, in `fields/cyclic_decay_field.py`): owns the whole
  `on_save` companion write. `KEYS = [cycles_hash, pressure_hash]`. Performs the
  #698 three-way cycles merge **and** the pressure rate-refresh/first-save branch
  **and** the two `HDEL` branches, then returns a msgpack-encoded decision report.
- **`CYCLES_ADJUST_LUA`** (new, same module, imported by `models/base.py`): owns
  `_adjust_cycle_amplitudes`. `KEYS = [cycles_hash]`. Mutates **index 2 (`amplitude`)
  in place**, clamps, and re-packs the same entry table with whatever arity it had —
  every other index, in particular **index 4 (`declared_baseline`)**, is re-packed
  byte-for-byte untouched. Do not `table.remove`, re-length, or rebuild the entry.

- **Slot numbering is 1-based Lua everywhere in this plan (critique C5).** Every
  "slot N" / "index N" in this document refers to the Lua entry table:
  **`1 = period`, `2 = amplitude`, `3 = phase`, `4 = declared_baseline`.** The Python
  source this plan ports from is 0-based — `base.py:2776`'s comment protects
  `cycle[3]`, and `cyclic_decay_field.py` reads `entry[3]` — and **that same slot is
  `c[4]` in Lua**. An earlier draft of this plan carried the phrase "never write or
  strip slot 3" lifted verbatim from that 0-based comment; in Lua `c[3]` is *phase*, so
  a builder protecting `c[3]` would leave `c[4]` fair game and silently destroy #698's
  baseline — the same crashless, logless failure mode B1 warned about. **Both halves of
  the comment at `base.py:2774-2777` need translating, not just one**: it reads
  "mutates only index **1** and repacks the same list objects … would turn this method
  into a **slot-3** writer/deleter". In Lua that is *mutate only index **2**, never
  touch slot **4***. Whenever a Python comment or a Python index is quoted into a Lua
  instruction — in this plan or in the shipped script — translate the number.
- **Decision report**: both scripts return `cmsgpack.pack(...)`, never bare Lua
  values, so Python keeps float amplitudes and structured reset records. Python
  logs the #698 reset lines and the "could not decode" warning from that report,
  so *observable* behavior is unchanged even though the decision moved server-side.
- **`resolve_pressure` and `import_state` are untouched** — both are blind single
  writes and already atomic (spike-4).
- **Straggler cleanup**: the module-level `from ..redis_db import POPOTO_REDIS_DB`
  in `cyclic_decay_field.py:49` and its **nine** use sites go away in favour of
  `get_REDIS_DB()`. Measured at `9986c086`, the grep is one import (`:49`) plus
  `:286`, `:301`, `:379`, `:387`, `:493`, `:501`, `:705`, `:719`, `:755` (N1 —
  an earlier draft said "seven"). **Three** of those nine (`:705`, `:719`, `:755`)
  sit inside the code this plan rewrites; the other six are in `export_state`
  (`:286`, `:301`), `import_state` (`:379`, `:387`) and the ranking path (`:493`,
  `:501`). `CLAUDE.md` names this file as one of the two remaining stale importers,
  held back only because #679/#698 were editing it; the file *already* mixes both
  forms (`:623` uses the accessor, `:719` does not), which is a latent
  read-from-the-wrong-database bug on the pressure path.
  `fields/write_filter.py` stays stale — #494 owns it.

  **Scope decision on the six out-of-path sites (critique C3): kept, with an
  explicit escape hatch.** The critic is right that this is import hygiene rather
  than atomicity. It is kept because a *partial* conversion leaves the file in the
  exact half-converted state #655 warns about — and because the conversion is nine
  single-token edits in one file with no behavior change, which is a smaller review
  burden than the split-issue bookkeeping.

  **The escape hatch, stated accurately (critique C7).** An earlier draft claimed
  "Task 3 depends on nothing that depends on it". That is **wrong as written** and the
  task note contradicted it parenthetically: Task 5 (`validate-cycles`) *does* list
  `build-redis-accessor` in its `Depends On`, and a Success Criterion references the
  conversion too. The true statement is narrower and still sufficient: **no task
  produces work that Task 3 consumes, and no task's implementation changes if Task 3 is
  dropped** — Tasks 1, 2 and 4 are untouched either way; Task 5's entry is a
  sequencing edge, not a data dependency. Dropping Task 3 is therefore safe but is
  **not free** — it is a four-edit operation, because deleting the task alone leaves a
  dangling `Depends On` ID (which the critique's structural check verifies) and an
  unsatisfiable Success Criterion. The four edits are enumerated in Task 3's scope
  note; the plan's **default is to keep Task 3**, under which no edit is needed.

### Flow

`record.strengthen_cycle("relevance")` → one `EVALSHA` → server reads, multiplies,
writes → returns packed cycles → Python truncates to the 3-slot public shape and
returns it. Concurrently, `other.save()` → one `EVALSHA` → server reads, merges
against the declaration, writes → returns a report → Python logs any reset. The two
scripts serialize on the server; neither can observe a half-written entry or write
over an unread one.

### Technical Approach

**Placement — the one decision worth arguing about.** `on_save` is called with a
pipeline in the dominant path (`base.py:1830`), and a pipeline-queued script cannot
return its decision until `execute()`, which `save()` does not surface. Two options:

1. **Queue the EVAL on the caller's pipeline.** Keeps the companion write inside the
   save transaction, but the #698 reset log and the decode warning become
   unreachable from `on_save` — an observability regression on the exact line #698's
   critique round 2 (C8) fought to make specific.
2. **Run the EVAL eagerly and directly (chosen).** Ignore the `pipeline` kwarg for
   these two writes. The log and the warning keep working, and the "read direct /
   write queued" split that caused this bug disappears rather than being narrowed.

Chosen: **(2)**. The cost is stated in Risk 2 and is not hidden.

**Corrected #476 citation, and how far this plan actually goes (critique C1).** An
earlier draft justified option 2 as "exactly what `Model.save` already does for
`IndexedFieldMixin` hooks (`base.py:1755-1780`)". That citation was wrong twice and
the claim it supported was too strong:

- The eager loop is `_eager_indexed_fields` at **`base.py:1773-1786`**. The range
  `1755-1780` straddles a branch boundary and points partly at the
  `EventStreamMixin` block at `:1754-1757`.
- That loop sits **only** inside the internal-pipeline `else:` branch beginning at
  `base.py:1760`. In the **caller-supplied-pipeline** branch (`base.py:1690-1758`,
  which returns `pipeline` for the caller to `.execute()` later), indexed-field
  `on_save()` is queued through the same generic per-field loop as every other
  field, at `base.py:1720-1729`. No eager special-casing exists there.

This plan ignores the `pipeline` kwarg **unconditionally, in both branches**, which
is *new ground*, not a repeat of #476. Concretely: in the caller-pipeline branch the
ported `CyclicDecayField.on_save` runs its `EVAL` synchronously inside the
`field.on_save(...)` call at `base.py:1721` — so the companion write commits to Redis
**before** `pipe.execute()` commits the model hash and index writes queued alongside
it on the same `pipe`. That is precisely the ordering exercised by the worked example
this plan deletes from `docs/features/cyclic-decay-field.md:171-175`.

#476 remains the right *shape* precedent — a script whose outcome must change the
caller's behavior cannot be queued — but it is a precedent for eager execution on the
internal-pipeline path only. Extending it to the caller-pipeline path is this plan's
own decision, and it is what Open Question 1 now asks the supervisor to sign off on.

**`CYCLES_MERGE_LUA` contract:**

```
KEYS[1] cycles hash key      KEYS[2] pressure hash key
ARGV[1] member key
ARGV[2] cycle count N        (0 => HDEL the cycles entry)
ARGV[3 .. 3N+2]              N triples: period, declared_amplitude, phase
ARGV[3N+3] pressure_rate     (<= 0 => HDEL the pressure entry)
ARGV[3N+4] now               (only used on a pressure first-save)
-> cmsgpack.pack({decode_failed, {{period, old_baseline, declared, learned}, ...}})
```

- Decode the stored cycles under `pcall`; **any** failure (bad msgpack, a non-table,
  an entry whose period slot is itself a table) sets `decode_failed = 1` and clears
  the learned buckets wholesale — the same all-or-nothing fallback the Python code
  takes today, for the same reason (a half-read payload must not seed some cycles
  and not others).
- **The table-valued period is a `type` guard, not a `pcall` (critique C4).** This is
  the one clause above that a faithful port silently *drops*, because the two
  languages fail differently. Python reaches the fallback by **crashing**:
  `learned.setdefault(entry[0], …)` at `cyclic_decay_field.py:647` raises
  `TypeError: unhashable type: 'list'` *inside* the `try`, which is why the handler at
  `:650-661` wraps decode **and** normalization. Lua has no unhashable-key failure and
  nothing raises — `tostring({})` cheerfully returns `"table: 0x…"`, so the `%.17g` /
  `tostring` key function specified in the next bullet would accept a table-valued
  period and merge it as a normal string-keyed bucket. There is no error for a `pcall`
  to catch. Guard the raw period **before** building its match key, inside the
  per-entry bucketing loop:

  ```lua
  if type(p) ~= 'number' and type(p) ~= 'string' then
      decode_failed = 1
      learned = {}
      break
  end
  ```

  Clearing `learned` on the same all-or-nothing path as a top-level `pcall` failure is
  what makes a *later* malformed entry discard an *earlier* well-formed one. Two
  existing tests, both of which this plan requires to pass **unchanged**, write exactly
  this payload and assert exactly that: `tests/test_cyclic_decay_field.py:1512`
  `test_unhashable_period_falls_back_instead_of_raising` (stores
  `[[[TemporalPeriod.DAILY], 9.0, 0]]`, asserts the declared `2.0` comes back and
  `"Could not decode cycles"` is logged) and `:1534`
  `test_partial_merge_discarded_when_a_later_entry_is_malformed` (first entry
  well-formed, second table-valued; asserts **both** declared cycles fall back
  together, `2.0` and `3.0`). Omitting the guard turns both red; that red is the
  feedback loop, not a stale-test signal.
- Bucket stored entries by period **in stored order**, pop index 1 per declared
  cycle: FIFO within duplicate periods, preserving today's semantics. Pop the
  `(amplitude, baseline)` pair together as one decision.
- Period matching must be type-safe, because a period can be a string (spike-1) and
  because Lua's default `tostring` on a float does not agree with Python's `repr`.
  Normalize both sides through one key function: `tonumber(v)` succeeds →
  `'n:' .. string.format('%.17g', n)`, else `'s:' .. tostring(v)`. Declared periods
  arrive as `str(period)` in ARGV and take the same path, so the two sides agree by
  construction. **The match key is for matching only — it is never written into the
  entry.** The key function is only ever reached with a `number` or a `string`,
  because the stored side passes the C4 `type` guard above first and the declared side
  is an ARGV string by construction; it must **not** be hardened with a `pcall` or a
  `tostring` fallback for tables, which would re-admit the case the guard exists to
  reject.
- **Every ARGV-sourced numeric slot must be converted before it is written back
  (critique B1).** `ARGV` is always a Lua string, and `period`/`amplitude`/`phase`/
  `declared_baseline` are all re-read from the declared side on each save, so a
  literal port would store *string*-typed values. Three distinct consequences, one
  of them a hard crash:

  | Slot | If left as an ARGV string | Required conversion |
  |---|---|---|
  | 1 `period` | **Hard error.** The pre-existing scorer does `local period = c[1] … if period > 0 then` at `cyclic_decay_field.py:163-166`, a bare relational comparison **outside** the `pcall` that guards only `cmsgpack.unpack` (`:159-160`). Lua 5.1 raises on `string > number`, aborting the whole script — every `rank_decayed`/`top_by_decay` against that partition raises `ResponseError`. This is the majority path for any model that declares cycles. | `coerce_period(raw)` — see below. Numeric-looking periods become numbers; a genuinely non-numeric (string) period stays a string, exactly as today. |
  | 2 `amplitude` | Silent type drift; arithmetic auto-coerces. | `tonumber()` |
  | 3 `phase` | No crash today — the scorer only uses phase in arithmetic (`now - phase`), which Lua 5.1 auto-coerces from a numeric string — but the same `isinstance` surprise as Risk 1 reaches Python. | `tonumber()` |
  | 4 `declared_baseline` | **Silent, total defeat of #698.** Lua's `==` never coerces across types and never errors, so an unconverted string baseline makes the `baseline == declared` branch evaluate false on *every* save, discarding every learned amplitude with no crash and no distinguishing log line. | `tonumber()` — and say so in the script header, because nothing about the failure is visible. |

  The helper, verbatim:

  ```lua
  local function coerce_period(raw)
      local n = tonumber(raw)
      if n then return n end
      return raw
  end
  ```

  Each output entry is built as
  `{coerce_period(argv_period), tonumber(amplitude), tonumber(phase), tonumber(new_baseline)}`
  — slot 1 is the **coerced value**, never the raw ARGV string and never the
  `'n:'`/`'s:'` match key.
- Baseline rule, verbatim from #698: slot 4 absent or non-numeric → baseline unknown
  → keep the learned amplitude; baseline `==` declared → keep the learned amplitude;
  baseline `~=` declared → take the declared amplitude and append a record to the
  report. Slot 4 is re-written as the incoming declared amplitude in every branch.
- Pressure branch: `HGET`; if present, decode, overwrite `rate`, re-`HSET` (this is
  the read-modify-write being fixed); if absent, `HSET {rate, last_resolved = now}`.
- The booleans: guard `isinstance(entry[3], bool)` has no Lua analogue because
  msgpack booleans decode to Lua booleans and `type(x) == 'number'` already excludes
  them. Same effect, different mechanism — worth a comment so a reader does not
  "restore" the missing check.

**`CYCLES_ADJUST_LUA` contract:**

```
KEYS[1] cycles hash key
ARGV[1] member key   ARGV[2] factor   ARGV[3] max_amplitude   ARGV[4] min_threshold
-> cmsgpack.pack(cycles) after the write
-> nil (Lua `return nil` -> Python None) when the member has no stored entry
```

Mutates only index 2 of each entry; re-packs the same tables, so a 4-slot entry
keeps its baseline (the property `base.py:2770-2779` currently documents at length).
**This sentence is the authoritative instruction** — "re-packs the entry unmodified
except index 2" needs no numbering base at all, which is why it is phrased that way
(critique C5). Where a slot number is unavoidable, it is 1-based Lua: the baseline is
**slot 4 (`declared_baseline`)**, never "slot 3" — that number belongs to the 0-based
Python comment at `base.py:2776`, where `cycle[3]` is the same field.
The clamp constants stay Python-side and arrive as ARGV so they remain single-sourced.

**The no-entry case is a `nil` return, not an empty bulk string (critique B2).**
Today's code short-circuits on a falsy `HGET` before it ever unpacks —
`base.py:2748-2754`: `raw = get_REDIS_DB().hget(...)`, then `if not raw:` returns
`pipeline` or `[]`. That check does not disappear in the port; it **moves to the
EVAL's return value**. The Lua takes an early exit:

```lua
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return nil end
```

and Python guards before unpacking, mirroring the `if not raw:` it replaces:

```python
result = run_lua(...)
if isinstance(pipeline, redis.client.Pipeline):
    return pipeline
if not result:
    return []
return [c[:3] for c in msgpack.unpackb(result, raw=False)]
```

An unconditional `msgpack.unpackb(run_lua(...))` crashes `strengthen_cycle()` /
`weaken_cycle()` for any member with no stored cycles, where the pre-fix code
cleanly returned `[]`. Task 2 carries this as an explicit step.

**Return-value encoding is load-bearing.** Redis converts Lua numbers to *integers*
when returning them over the protocol — `return 2.4` reaches the client as `2`.
Both scripts must therefore return **either** `cmsgpack.pack(...)` as a bulk string
(the value-carrying case, which Python `msgpack.unpackb`s) **or** `nil` (the
explicitly-nothing-to-report case above). Those are the only two shapes; what is
forbidden is returning a bare Lua table or number, which would silently truncate
every fractional amplitude in `strengthen_cycle`'s return value. The `nil` carve-out
is not an exception to the rule — `nil` carries no numbers to truncate, and it is
distinguishable from a packed empty result, which an empty bulk string is not.

**Pipeline handling after the change:**

| Call site | Before | After |
|---|---|---|
| `on_save` cycles + pressure | read direct, write via pipeline | one eager direct `EVAL` (pipeline kwarg ignored for these two writes; still forwarded to `super().on_save()`) |
| `_adjust_cycle_amplitudes`, no pipeline | `HGET` + `HSET` | direct `EVAL`, returns packed cycles |
| `_adjust_cycle_amplitudes`, pipeline | read direct, `HSET` queued | `run_lua` queues the `EVALSHA`; returns the pipeline, exactly as today |
| `_adjust_cycle_amplitudes`, pipeline, **member has no stored entry** | direct `HGET` misses → returns `pipeline` having queued **nothing** (`base.py:2750-2753`) | queues one `EVALSHA` (the existence check is now inside the script); still returns `pipeline`, but `pipe.execute()` yields **one extra result** and later positions shift (critique C6 — disclosed, not fixed) |
| `resolve_pressure` | single `HSET` | unchanged |
| `on_delete` | `HDEL` ×2 | unchanged |

## Failure Path Test Strategy

### Exception Handling Coverage
- `cyclic_decay_field.py:650-661` — the `except Exception` around the cycles decode.
  Its observable behavior is `logger.warning("Could not decode cycles data …")` plus
  the fall back to declared amplitudes. After the move it is driven by the script's
  `decode_failed` flag. Test: write garbage bytes into the cycles hash for a member,
  `save()`, assert the warning is emitted (`caplog`) **and** that the entry comes back
  as the declared cycles. There is an existing test for this — it must keep passing
  unchanged, which is the point.
- New handler risk: a `redis.exceptions.ResponseError` from a Lua runtime error must
  not be swallowed. Neither script gets a blanket `try/except` — a script error is a
  bug and must raise out of `save()`.

### Empty/Invalid Input Handling
- No cycles declared (`field.cycles == []`) → `HDEL` branch. Test both "entry existed"
  and "entry never existed".
- `pressure_rate == 0` → `HDEL` branch. Same two cases.
- Stored entry is an empty msgpack array, a map instead of an array, a 1-element
  entry, or an entry whose period is a nested list → all take the `decode_failed`
  path with the warning; none may raise.
- A member key with no stored entry at all → merge takes every declared amplitude
  with baseline unknown; adjust returns `[]` (or the pipeline).
- Non-numeric (string) period declared and stored → matches; no crash (spike-1).

### Error State Rendering
- Not user-facing UI. The two user-visible signals are the `logger.info` reset line
  (#698, must keep naming model, field, `member_key`, period, old baseline, new
  declared value and the discarded amplitude) and the `logger.warning` decode line.
  Both are asserted via `caplog` rather than assumed.

## Test Impact

- [ ] `tests/test_cyclic_decay_field.py` (88 tests) — UPDATE only where a test asserts
      *how* the write happens. Every test that asserts merge outcomes, reset logging,
      decode fallback or return shapes must pass **unchanged**; that invariance is the
      main regression signal for the Lua port. Audit each test that patches or spies on
      `hget`/`hset` — those spies stop firing once the work moves into a script and
      must be re-pointed at the script's effect (or at `run_lua`), not deleted.
- [ ] **`tests/test_cyclic_decay_field.py:1512` and `:1534` are the named acceptance
      tests for critique C4** — `test_unhashable_period_falls_back_instead_of_raising`
      and `test_partial_merge_discarded_when_a_later_entry_is_malformed`. Both write a
      table-valued period and assert the whole learned bucket is discarded with the
      decode warning. They must pass **unmodified**; if either needs editing to
      accommodate the port, the Lua is missing the C4 `type` guard, not the tests being
      stale.
- [ ] `tests/test_observation_protocol.py:693-701` (`test_pipeline_support`) — UPDATE
      for critique C6. It only asserts `result is pipe`, so it cannot see that a
      pipelined `strengthen_cycle`/`weaken_cycle` against a member with **no** stored
      cycles now queues one `EVALSHA` where it previously queued nothing. Extend it, or
      add a sibling test asserting `len(pipe.execute())` for the no-entry pipelined
      case, so the result-list shift is recorded rather than discovered downstream.
- [ ] `tests/test_transfer_fidelity_fields.py:466-600` — UPDATE if anything asserts a
      value's *type*. Its current assertions are `pytest.approx` and a
      `len(exported_cycles[0]) == 4` arity check, both int/float-agnostic, so the
      expectation is no change; verify rather than assume (spike-2).
- [ ] `tests/test_observation_protocol.py`, `tests/test_validity_field.py` — call
      `strengthen_cycle`/`weaken_cycle`/`resolve_pressure` via the observation path
      with pipelines. UPDATE only if the eager-write placement changes an ordering they
      assert.
- [ ] `tests/test_cyclic_subclass_companion_keys.py` — no change expected (key
      derivation only); listed so the builder confirms rather than skips it.
- [ ] **New**: `tests/test_cyclic_decay_atomicity.py` — the concurrency tests
      (Task 4). No existing coverage of concurrent writers exists.
- [ ] **New coverage for critique B1** — no listed file asserts the stored period's
      *type* (`test_transfer_fidelity_fields.py`'s arity check is period-blind). Add,
      in `tests/test_cyclic_decay_field.py`: (a) after a `save()`, the stored cycles
      entry decodes with a **numeric** period (and a declared string period stays a
      string); (b) a scoring-path test — `top_by_decay` / `rank_decayed` over a model
      that declares cycles — that does not raise `ResponseError` post-port; (c) a
      #698 baseline-preservation test that would fail if slot 4 were stored as a
      string (save, `strengthen_cycle`, save again with an unchanged declaration →
      learned amplitude survives).
- [ ] **New coverage for critique B2** — `strengthen_cycle()` / `weaken_cycle()` on a
      member with no stored cycles entry returns `[]` (or the pipeline) and does not
      raise.
- [ ] No xfail markers relate to this bug — `grep -rn 'pytest.mark.xfail\|pytest.xfail('
      tests/` returns nothing matching cycles/amplitude/pressure. Nothing to convert.

## Rabbit Holes

- **Rewriting `resolve_pressure` "for symmetry".** Spike-4 shows it is a blind write
  and already atomic. Touching it adds a script, a return-shape question and a risk
  for zero defect closed.
- **Making the whole save atomic.** The model hash, the zset, the class set and the
  companion hashes are still written by several operations. Closing *that* is a
  different, much larger project (`#476` territory). This plan closes the
  companion-hash lost update and nothing else.
- **A Python-side lock or a `WATCH`/`MULTI` optimistic retry.** Both were available
  before and neither survives contact with the pipeline path; the repo's own
  precedent (#588/PR #594) is that the guard belongs inside the script.
- **Unifying `CYCLES_MERGE_LUA` with the scoring script `CYCLIC_DECAY_LUA`.** They
  bind different KEYS indices for a documented reason (`cyclic_decay_field.py:76-78`)
  and unifying them silently mis-decodes a cycles array as a confidence dict.
- **Forcing Lua to emit msgpack floats.** There is no API for it (spike-2). Coerce on
  the Python read boundary and move on.
- **Converting `fields/write_filter.py`'s stale `POPOTO_REDIS_DB` import** while in the
  neighbourhood — #494 is actively editing that file.

## Risks

### Risk 1: integral amplitudes come back from Redis as `int`, not `float`
**Impact:** Any consumer doing `isinstance(x, float)` — downstream code or a future
test — breaks on a value that used to be `5.0` and is now `5` (spike-2, measured).
Arithmetic and equality are unaffected in both Python and Lua.
**Mitigation:** Coerce at the two boundaries where a stored amplitude surfaces:
`_adjust_cycle_amplitudes`'s public return (`float(c[1])`, and `float(c[2])` for
phase) and `CyclicDecayField.export_state`. Do **not** coerce `period` — it may
legitimately be a string. Add an explicit test asserting `weaken_cycle` returns
floats, so the coercion cannot be "simplified" away later.

### Risk 2: the companion write is no longer inside the save's transaction
**Impact:** With the eager placement, a `save()` that fails after the companion EVAL
(a uniqueness conflict, a connection drop) leaves a refreshed cycles/pressure entry
for a member whose model hash was never written — an orphan companion entry.
**Mitigation:** The orphan is inert: every reader (`CYCLIC_DECAY_LUA`, query scoring)
iterates the zset and looks members up in the hash, so an entry for a member not in
the zset is never read, and `PURGE_ORPHAN_LUA` already exists for cleanup. This is the
same trade #476 accepted for indexed fields, for a stronger reason. Documented in the
`on_save` docstring rather than left to be rediscovered.

### Risk 3: the #698 merge rule now has one implementation, in Lua, that unit tests
cannot introspect
**Impact:** A subtle divergence (FIFO order, the bool guard, the baseline-unknown
branch) passes review because the Lua reads plausibly.
**Mitigation:** The script returns a structured decision report, so tests assert
*which* branch fired, not only the resulting number. Every existing #698 behavioral
test must pass unmodified; a test that needed rewriting to accommodate the port is a
signal of divergence, not of a stale test.

### Risk 4: script eviction / `NOSCRIPT` in the pipeline path
**Impact:** Valkey 8 evicts EVAL-loaded scripts LRU; a `SCRIPT FLUSH` between
`run_lua`'s `SCRIPT LOAD` and a pipeline `execute()` surfaces as `NoScriptError`.
**Mitigation:** None needed — `run_lua` already documents and handles both paths
(`redis_db.py:857-882`). Listed so it is not re-solved.

### Risk 5: worktree/environment noise misread as a regression
**Impact:** DB 15 contention across worktrees has produced 73-158 phantom failures;
`test_version.py::test_version_matches_pyproject` fails by construction on a stale
editable install.
**Mitigation:** Run with `POPOTO_TEST_DB=9`, install `.[dev,embeddings,benchmark,mcp]`,
and state the environment alongside every count reported by a stage.

## Race Conditions

### Race 1: `save()` vs `strengthen_cycle()` / `weaken_cycle()` on the cycles hash
**Location:** `fields/cyclic_decay_field.py:623` + `:710` against `models/base.py:2749` + `:2781`/`:2784`
**Trigger:** Both writers `HGET` the same hash field before either `HSET`s it; the
later write wins wholesale. Reachable across processes, across threads, and — because
the reads bypass the pipeline while the writes do not — within a single pipeline.
**Data prerequisite:** a stored cycles entry for the member (so there is learned state
to lose).
**State prerequisite:** the two writers target the same `(cycles_hash_key, member_key)`
pair, i.e. the same instance of the same field.
**Mitigation:** `CYCLES_MERGE_LUA` and `CYCLES_ADJUST_LUA` — each does its read and
its write inside one script invocation, and Redis executes scripts one at a time, so
the interleaving that loses the update cannot occur.

### Race 2: `save()` vs `resolve_pressure()` on the pressure hash
**Location:** `fields/cyclic_decay_field.py:719` + `:724` against `models/base.py:2648-2653`
**Trigger:** `on_save` reads the pressure entry (holding the *old* `last_resolved`),
`resolve_pressure` writes a fresh one, `on_save` writes back the stale value — the
discharge is undone and the record keeps accruing urgency it already paid off. Only
this ordering exists; `resolve_pressure` never reads (spike-4).
**Data prerequisite:** an existing pressure entry (first save takes the write-only branch).
**State prerequisite:** `field.pressure_rate > 0` on both sides.
**Mitigation:** the pressure branch moves into `CYCLES_MERGE_LUA`. `resolve_pressure`
stays a single `HSET`, which is already atomic, so making the reader side atomic
closes the race without touching it.

### Race 3 (not fixed, stated): companion write vs. the rest of `save()`
**Location:** `models/base.py:1830` vs the eager EVAL introduced by this plan
**Trigger:** the companion entry is written before the model hash / zset commit, so a
later failure in the same `save()` leaves an orphan entry.
**Mitigation:** none, deliberately — see Risk 2. Inert data, existing purge path,
follows the #476 precedent's shape (with the scope correction in Placement: this
plan extends eager execution to the caller-pipeline branch, which #476 did not).
Closing it means making the whole save one transaction, which is out of scope.

### Race 4 (not fixed, stated): mixed-version writers during a rolling deploy
**Location:** any pre-fix process running `cyclic_decay_field.py:623`/`:710` or
`base.py:2749`/`:2784` concurrently with a post-fix process's `EVAL`
**Trigger:** the server serializes scripts against each other, but a pre-fix
process's client-side `HGET` … `HSET` pair is two independent commands; its read can
interleave with a new script's read-and-write and its write then clobbers the
script's output. Races 1 and 2 remain reachable in exactly their pre-fix form.
**Data/state prerequisite:** at least one un-upgraded writer process for the same
`(cycles_hash_key, member_key)`.
**Mitigation:** none available in code — no client-side protocol can exclude a writer
that does not participate in it. Handled as a **documented deployment requirement**:
the fix is complete only once every writer is upgraded. See Update System and Task 6.

## No-Gos (Out of Scope)

Nothing deferred — every relevant item is in scope for this plan. What this plan
does not touch is not deferred work: `resolve_pressure`, `import_state` and
`on_delete` are single blind writes that are *already* atomic (spike-4), so there
is nothing to fix in them; whole-`save()` transactionality, unifying the scoring
script, and the `write_filter.py` import straggler are named under **Rabbit Holes**
as avenues to avoid, not as promises. The anti-criteria in **Verification** assert
that the untouched writers stay untouched.

## Update System

No update-system changes required in the mechanical sense: popoto is a library plus
an mkdocs site; this change is internal to two modules, adds no dependency, no
config, and no data migration — the stored payload shape is byte-compatible before
and after (the only difference is msgpack integer-vs-float encoding of integral
values, which every reader already accepts, including the pre-existing
`CYCLIC_DECAY_LUA` scorer and older popoto versions).

**But "no migration" is not "safe to roll out gradually" (critique C2).** Script
atomicity serializes against other *scripts and single commands on the server*. It
gives **no** exclusion whatsoever against a concurrent **pre-fix process** still
doing client-side `HGET` → merge in Python → `HSET`: that process's read can land
between the new script's read and its write, and its later `HSET` clobbers the
script's result exactly as before. The race being closed here therefore stays fully
reachable for the entire duration of a rolling deploy, and is closed only once
**every** process that writes a given `CyclicDecayField`-bearing model is on
post-fix code.

This is a deployment caveat, not a defect in the fix, and it is the same class of
hazard the repo already documents for the sibling #698 mechanism at
`docs/features/cyclic-decay-field.md:150-152` ("a rolling deploy where old- and
new-version processes save the same record with different in-process
declarations"). It must be stated **next to that existing caveat in the user-facing
docs**, not only here — an operator reading "no update-system changes" without it
will reasonably conclude gradual rollout carries no caveat. Task 6 carries this.

## Agent Integration

No agent integration required. This is a field-internal correctness fix; no new
public surface, no MCP tool, no entry point. `popoto.get_redis()` and the field's
public methods keep their signatures.

## Documentation

### Feature Documentation
- [ ] `docs/features/cyclic-decay-field.md:165-180` — **delete** the "queue the
      **save first**" pipeline caveat and its worked example; replace with a short
      statement that both writers are atomic server-side and pipeline ordering no
      longer matters for them, plus the one new fact a user can observe (the
      companion write executes eagerly, not at `pipeline.execute()`).
- [ ] Same file — note that integral amplitudes may read back as `int` (Risk 1) and
      that popoto coerces at its own boundaries.
- [ ] `docs/features/cyclic-decay-field.md:150-152` — **extend the existing rolling-
      deploy caveat** (critique C2, Race 4). It currently covers mixed in-process
      *declarations*; add that during a rolling deploy the atomicity guarantee itself
      does not hold, because a pre-fix process's client-side read-modify-write is not
      excluded by a server-side script, so the lost update stays reachable until every
      writer of that model is upgraded.
- [ ] `docs/features/README.md` — index entry already exists; verify no text there
      repeats the deleted caveat.

### External Documentation Site
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `CyclicDecayField.on_save` docstring (`:542-597`) — remove the "an amplitude
      adjustment queued on the *same* pipeline as this save is still lost" paragraph;
      state instead that the companion write is one eager atomic script, and why it
      is eager (Risk 2 / #476 precedent).
- [ ] `Model._adjust_cycle_amplitudes` docstring (`:2703-2723`) — the long comment
      explaining why the baseline slot is repacked untouched must survive the port; the
      property is now enforced by the Lua, so the comment moves next to it. **Translate
      the index when it moves (critique C5):** the Python comment at `base.py:2776`
      calls it `cycle[3]` (0-based); in the Lua it is `c[4]`. Do not carry "slot 3"
      across — `c[3]` is *phase*.
- [ ] Both Lua scripts get header comments in the style of `CYCLIC_DECAY_LUA`
      (`:60-68`): KEYS/ARGV layout, and an explicit note that the return value is
      `cmsgpack`-packed because bare Lua numbers are truncated to integers over the
      protocol.

## Success Criteria

- [ ] A concurrency test (N threads interleaving `save()` with `strengthen_cycle()`
      on one member) ends with the amplitude equal to `declared * factor**K` for the
      K adjustments performed — no lost updates.
- [ ] The same test, run against pre-fix `main`, **fails** — red-state output pasted
      into the PR description (spike-3).
- [ ] A concurrency test for `save()` vs `resolve_pressure()` shows `last_resolved`
      never regresses.
- [ ] Every existing test in `tests/test_cyclic_decay_field.py` passes **unmodified**,
      except where a test spied on `hget`/`hset` directly (Test Impact).
- [ ] `grep` confirms no client-side `hget` of either companion hash remains in
      `on_save` or `_adjust_cycle_amplitudes`.
- [ ] A saved record's stored cycles entry decodes with a **numeric** period, and a
      ranked query (`top_by_decay`) over a cycle-declaring model returns without
      `ResponseError` (critique B1).
- [ ] `strengthen_cycle()` on a member with no stored cycles entry returns `[]`
      rather than raising (critique B2).
- [ ] The rolling-deploy caveat (Race 4 / critique C2) is present in
      `docs/features/cyclic-decay-field.md`, not only in this plan.
- [ ] `POPOTO_REDIS_DB` no longer appears in `fields/cyclic_decay_field.py`.
      *(Task 3 only. Delete this criterion if Open Question 3 is answered "split it
      out" — edit 4 of the removal procedure, critique C7.)*
- [ ] `tests/test_cyclic_decay_field.py:1512` and `:1534` (table-valued period →
      whole learned bucket discarded with the decode warning) pass **unmodified** —
      the acceptance test for the C4 `type` guard.
- [ ] A pipelined `strengthen_cycle`/`weaken_cycle` against a member with no stored
      cycles entry is covered by a test that pins `pipe.execute()`'s result length,
      recording the one-entry shift (critique C6).
- [ ] The "queue the save first" caveat is gone from the docstring and the docs page.
- [ ] Tests pass (`/do-test`) on `POPOTO_TEST_DB=9`, environment stated with the count.
- [ ] `scripts/mypy_ratchet.py`, `ruff check src/`, `black --check src/ tests/`,
      `mkdocs build --strict` all pass.
- [ ] Documentation updated (`/do-docs`).
- [ ] No xfail conversions needed (none exist for this bug).

## Team Orchestration

### Team Members

- **Builder (lua-scripts)**
  - Name: `cycles-lua-builder`
  - Role: Write both Lua scripts and port the two call sites.
  - Agent Type: builder
  - Domain: Redis/Popoto data — see `DOMAIN_FRAMING.md`
  - Resume: true

- **Builder (tests)**
  - Name: `atomicity-test-builder`
  - Role: Concurrency tests plus the red-state proof against pre-fix code.
  - Agent Type: test-engineer
  - Domain: async/concurrency — see `DOMAIN_FRAMING.md`
  - Resume: true

- **Validator**
  - Name: `cycles-validator`
  - Role: Verify #698/#679 semantics are bit-identical after the port and run the
    verification table.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `cycles-doc`
  - Role: Docstrings + `docs/features/cyclic-decay-field.md`.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Write `CYCLES_MERGE_LUA` and port `on_save`
- **Task ID**: build-merge-script
- **Depends On**: none
- **Validates**: `tests/test_cyclic_decay_field.py`, `tests/test_transfer_fidelity_fields.py`
- **Informed By**: spike-1 (cmsgpack round-trips the payload; string periods exist), spike-2 (Lua repacks integral floats as ints), spike-4 (pressure is asymmetric — only `on_save` reads)
- **Assigned To**: `cycles-lua-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `CYCLES_MERGE_LUA` to `src/popoto/fields/cyclic_decay_field.py` with the
  KEYS/ARGV layout from Technical Approach and a header comment.
- Port `on_save` (`:598-736`) to a single eager `run_lua(get_REDIS_DB(), …)` call —
  cycles merge, pressure merge, and both `HDEL` branches — keeping `super().on_save()`
  and its `pipeline` forwarding exactly as they are.
- Unpack the returned report with `msgpack.unpackb`; emit the #698 `logger.info` reset
  line (model, field, `member_key`, period, old baseline, new declared, discarded
  learned amplitude) and the `logger.warning` decode line from it. Normalize an empty
  report defensively — `cmsgpack` may pack an empty Lua table as either an array or a map.
- Match periods through the `%.17g` / string key function; do not compare raw values.
- **Guard a table-valued stored period by `type`, before the match key is built
  (critique C4).** In the bucketing loop:
  `if type(p) ~= 'number' and type(p) ~= 'string' then decode_failed = 1; learned = {}; break end`.
  This is a type check, **not** a `pcall` — nothing raises in Lua, so there is no
  error to catch, and a faithful port of the Python `try` alone silently accepts the
  payload. Clearing `learned` (not just skipping the entry) is what makes a later
  malformed entry discard an earlier well-formed one.
  `tests/test_cyclic_decay_field.py:1512` and `:1534` are the acceptance test for this
  step and must pass **unmodified**.
- **Convert every ARGV-sourced slot before writing it back (critique B1).** Add the
  `coerce_period()` helper and build each entry as
  `{coerce_period(argv_period), tonumber(amp), tonumber(phase), tonumber(baseline)}`.
  Slot 1 must never be the raw ARGV string (the scorer's bare `if period > 0` at
  `cyclic_decay_field.py:163-166` sits outside the `pcall` and hard-errors on
  `string > number`) and never the `'n:'`/`'s:'` match key. Slot 4 must be
  `tonumber()`-ed or #698's `baseline == declared` branch is false forever, silently.
  Note both in the script header comment — the slot-4 failure has no crash and no
  distinguishing log line.
- Keep the clamp/threshold constants where they are; do **not** add a new
  `Defaults` constant (every new one must be registered in
  `tests/benchmarks/test_defaults_sync.py`, which narrow lane test selection never reaches).

### 2. Write `CYCLES_ADJUST_LUA` and port `_adjust_cycle_amplitudes`
- **Task ID**: build-adjust-script
- **Depends On**: none
- **Validates**: `tests/test_cyclic_decay_field.py`, `tests/test_observation_protocol.py`
- **Informed By**: spike-2 (return values must be `cmsgpack`-packed or fractions truncate)
- **Assigned To**: `cycles-lua-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add the script beside `CYCLES_MERGE_LUA`; import it into `models/base.py` next to the
  existing `from ..fields.cyclic_decay_field import CyclicDecayField`.
- Port `base.py:2749-2789` to `run_lua`, passing `factor`, `max_amplitude` and
  `min_threshold` as ARGV. Preserve both return contracts: the pipeline when a
  pipeline is given, the 3-slot truncated list otherwise.
- **Carry the no-entry short-circuit across the port (critique B2).** The Lua early-
  exits with `local raw = redis.call('HGET', KEYS[1], ARGV[1]); if not raw then return nil end`
  — explicit `nil`, never `''`. Python guards on the EVAL's return value before
  unpacking, mirroring today's `if not raw: return []` at `base.py:2748-2754`:
  `result = run_lua(...)`; return `pipeline` if a pipeline was given; `[]` if
  `not result`; otherwise `[c[:3] for c in msgpack.unpackb(result, raw=False)]`. An
  unconditional `msgpack.unpackb(run_lua(...))` crashes `strengthen_cycle()` /
  `weaken_cycle()` for a member with no stored cycles.
- Add a test for exactly that case: `strengthen_cycle()` on a member with no stored
  cycles entry returns `[]` and does not raise, in both the pipeline and non-pipeline
  forms.
- Coerce amplitude and phase to `float` on the public return (Risk 1); leave `period`
  untouched.
- **Preserve the baseline slot, in the right numbering base (critique C5).** The Lua
  must mutate `c[2]` **in place** and re-pack the *same* entry table — no
  `table.remove`, no re-length, no rebuild — so every other index survives untouched.
  Stated as a number: the protected slot is **`c[4]` (`declared_baseline`)** in 1-based
  Lua, which is the field `base.py:2776`'s 0-based comment calls `cycle[3]`. Do not
  copy "slot 3" from that comment into the script: `c[3]` is *phase*, and protecting it
  instead would leave `c[4]` fair game and destroy #698's baseline with no crash and no
  log line. Keep the comment that explains why the slot is preserved, translated to the
  Lua index, next to the Lua.
- **Disclose the pipelined no-entry result-list shift (critique C6).** Today's direct
  `HGET` existence check at `base.py:2749-2754` returns `pipeline` on a miss having
  queued **nothing**; after the port the check lives inside the script, so
  `run_lua(pipeline, …)` queues an `EVALSHA` on **every** call. The Python return is
  unchanged (still the pipeline), but `pipe.execute()`'s result list gains one entry
  and every later position shifts for a caller that indexes it. No code fix — the
  queued command is required for atomicity and is the right trade. Record it: extend
  `tests/test_observation_protocol.py:693-701` (`test_pipeline_support`, which only
  asserts `result is pipe` and cannot see this), or add a sibling test asserting
  `len(pipe.execute())` for the no-entry pipelined case.

### 3. Convert the module's stale `POPOTO_REDIS_DB` import
- **Task ID**: build-redis-accessor
- **Depends On**: build-merge-script
- **Validates**: `tests/test_popoto_redis_db_rebind.py`, `tests/test_connection.py`
- **Assigned To**: `cycles-lua-builder`
- **Agent Type**: builder
- **Parallel**: false
- Replace the **nine** `POPOTO_REDIS_DB` uses in `fields/cyclic_decay_field.py`
  measured at `9986c086` — `:286`, `:301`, `:379`, `:387`, `:493`, `:501`, `:705`,
  `:719`, `:755` (the last three, or whatever survives of them, live in the code
  Tasks 1-2 rewrite) — with `get_REDIS_DB()`; drop the name from the import at `:49`.
- Do not touch `fields/write_filter.py`.
- **Scope note (critique C3):** six of the nine are outside this bug's code path.
  They are kept to avoid leaving the file half-converted (#655). No task consumes work
  this one produces, and no task's implementation changes if it is dropped — Tasks 1, 2
  and 4 are unaffected either way, and Task 5's `Depends On` entry is a sequencing edge
  rather than a data dependency.
- **Removal procedure if Open Question 3 is answered "split it out" — four edits, not
  two (critique C7).** Deleting the task alone leaves a dangling `Depends On` ID and an
  unsatisfiable Success Criterion, both of which the plan's structural check flags:
  1. Delete this task (Task 3) and renumber nothing — task IDs, not ordinals, are what
     `Depends On` resolves against.
  2. Delete the Verification row
     `Anti-criterion: stale client import gone` / `grep -c "POPOTO_REDIS_DB" … == 0`,
     which holds only on a **full** nine-site conversion.
  3. Drop `tests/test_popoto_redis_db_rebind.py` and `tests/test_connection.py` from
     this task's `Validates` list (moot once the task is gone; listed so the audit is
     complete).
  4. **Remove `build-redis-accessor` from Task 5's `Depends On`, and delete the
     Success Criterion "`POPOTO_REDIS_DB` no longer appears in
     `fields/cyclic_decay_field.py`".** These are the two the earlier draft missed.

  The plan's **default is to keep Task 3**; under the default none of the four edits
  apply.
- Verify in a **full-suite** run, not a file-scoped one: a stale snapshot and a
  converted call agree until `tests/test_connection.py` rebinds the global.

### 4. Concurrency tests + red-state proof
- **Task ID**: build-atomicity-tests
- **Depends On**: none
- **Validates**: `tests/test_cyclic_decay_atomicity.py` (create)
- **Informed By**: spike-3 (the race is reachable; the test must be shown failing first)
- **Assigned To**: `atomicity-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- New file `tests/test_cyclic_decay_atomicity.py`.
- Test A: K threads each load the record fresh and call `strengthen_cycle`, while
  other threads call `save()`; assert the final amplitude equals
  `declared * factor**K` (`pytest.approx`). The chain of same-factor multiplies is
  order-independent in value, so the assertion is deterministic under any interleaving.
- Test B: threads interleave `save()` with `resolve_pressure()`; assert the stored
  `last_resolved` never moves backwards.
- Test C: no client-side read remains — spy on the client's `hget` during `save()`
  and assert the cycles/pressure hashes are not read from Python.
- Run A and B against pre-fix `main` (same worktree, `git stash`) and capture the
  failure output for the PR description. A test that passes on both sides is not the test.
- Use `POPOTO_TEST_DB=9`; keep thread counts modest so the suite stays fast.

### 5. Validation
- **Task ID**: validate-cycles
- **Depends On**: build-merge-script, build-adjust-script, build-redis-accessor, build-atomicity-tests
  <br>*(`build-redis-accessor` is a **sequencing** edge only — Task 5 consumes nothing
  Task 3 produces. If Open Question 3 is answered "split it out", this ID must be
  removed here; it is edit 4 of Task 3's removal procedure — critique C7.)*
- **Assigned To**: `cycles-validator`
- **Agent Type**: validator
- **Parallel**: false
- Confirm every pre-existing `tests/test_cyclic_decay_field.py` test passes and report
  exactly which ones were modified and why.
- Run the Verification table; state the environment (redis-py version, server version,
  `POPOTO_TEST_DB`) alongside every count.

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-cycles
- **Assigned To**: `cycles-doc`
- **Agent Type**: documentarian
- **Parallel**: false
- Everything in the **Documentation** section — including the rolling-deploy caveat
  extension at `docs/features/cyclic-decay-field.md:150-152` (critique C2 / Race 4),
  which is a Success Criterion and a Verification row, not optional prose.

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: `cycles-validator`
- **Agent Type**: validator
- **Parallel**: false
- Full suite + all gates; verify every Success Criterion including the red-state paste.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Targeted tests pass | `POPOTO_TEST_DB=9 python -m pytest tests/test_cyclic_decay_field.py tests/test_cyclic_decay_atomicity.py tests/test_transfer_fidelity_fields.py tests/test_cyclic_subclass_companion_keys.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=9 python -m pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `python scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |
| Merge script exists | `grep -c "CYCLES_MERGE_LUA" src/popoto/fields/cyclic_decay_field.py` | output > 1 |
| Adjust script exists | `grep -c "CYCLES_ADJUST_LUA" src/popoto/fields/cyclic_decay_field.py` | output > 0 |
| Anti-criterion: no client-side cycles read in `on_save` | `grep -c "hget(cycles_hash_key" src/popoto/fields/cyclic_decay_field.py` | match count == 0 |
| Anti-criterion: no client-side pressure read in `on_save` | `grep -c "hget(pressure_hash_key" src/popoto/fields/cyclic_decay_field.py` | match count == 0 |
| Anti-criterion: no client-side cycles RMW in `_adjust_cycle_amplitudes` | `grep -c "hget(cycles_hash_key" src/popoto/models/base.py` | match count == 0 |
| Anti-criterion: stale client import gone (drop with Task 3 if split out — see C3) | `grep -c "POPOTO_REDIS_DB" src/popoto/fields/cyclic_decay_field.py` | match count == 0 |
| Stored period is numeric, not an ARGV string (B1) | `POPOTO_TEST_DB=9 python -m pytest tests/test_cyclic_decay_field.py -q -k "period_type or scoring_path"` | exit code 0 |
| Ranked query does not raise post-port (B1) | `POPOTO_TEST_DB=9 python -m pytest tests/test_cyclic_decay_field.py -q -k "top_by_decay"` | exit code 0 |
| No-entry adjust returns `[]` (B2) | `POPOTO_TEST_DB=9 python -m pytest tests/test_cyclic_decay_field.py -q -k "no_stored_cycles"` | exit code 0 |
| Table-valued period still falls back, whole bucket (C4) | `POPOTO_TEST_DB=9 python -m pytest tests/test_cyclic_decay_field.py -q -k "unhashable_period or partial_merge_discarded"` | exit code 0, **with the tests unmodified** (`git diff origin/main -- tests/test_cyclic_decay_field.py \| grep -c "unhashable_period"` → 0) |
| Lua type guard present, not a `pcall` (C4) | `grep -c "type(p) ~= 'number'" src/popoto/fields/cyclic_decay_field.py` | output > 0 |
| Baseline slot protected in Lua terms, not Python's — negative (C5, row fixed per C9) | `grep -Ec "slot[ -]3 writer" src/popoto/fields/cyclic_decay_field.py` | match count == 0 (measured **0** at `3ed94376`; hyphen-tolerant, so a verbatim copy of `base.py:2776`'s `slot-3 writer/deleter` into the new Lua is caught). Do **not** replace this with a whole-file `grep -c "slot 3"` — that returns **4** at `3ed94376` from correct 0-based Python prose at `:344`, `:558`, `:619`, `:680`, which no task rewrites |
| Baseline slot protected in Lua terms, not Python's — positive (C5, row fixed per C9) | `grep -c "c\[4\]" src/popoto/fields/cyclic_decay_field.py` | output > 0 (measured **0** at `3ed94376`, so this is red pre-build and green only once `CYCLES_ADJUST_LUA` names the baseline in 1-based Lua) |
| Pipelined no-entry result shift pinned by a test (C6, row fixed per C8) | `grep -c "len(pipe.execute())" tests/test_observation_protocol.py` | output > 0 (measured **0** at `3ed94376`. The earlier row grepped bare `pipe.execute()`, which returns **1** on unmodified `main` from `test_pipeline_support:701` — the very test C6 exists to strengthen — and so certified nothing) |
| Rolling-deploy caveat documented (C2) | `grep -c "rolling deploy" docs/features/cyclic-decay-field.md` | output >= 2 |
| Anti-criterion: `resolve_pressure` body left alone | `git diff origin/main -- src/popoto/models/base.py \| grep -c "^[-+][^-+].*resolve_pressure"` | match count == 0 |
| Anti-criterion: pipeline caveat removed from docs | `grep -c "save first" docs/features/cyclic-decay-field.md` | match count == 0 |
| Anti-criterion: pipeline caveat removed from docstring | `grep -c "queued on the \*same\* pipeline" src/popoto/fields/cyclic_decay_field.py` | match count == 0 |
| Anti-criterion: no new `Defaults` constant | `git diff origin/main -- src/popoto/fields/constants.py \| grep -c "^+"` | match count == 0 |
| No stale xfails for this bug | `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/ \| grep -ci "cycl\|amplitude\|pressure"` | match count == 0 |

## Critique Results

**Round 1** — 2026-09-08 · FULL depth · independent roster (3 critics: Risk &
Robustness, Scope & Value, History & Consistency) · **Verdict: NEEDS REVISION**
(2 blockers, 3 concerns, 1 nit).

| Severity | Critic(s) | Finding | Location |
|---|---|---|---|
| BLOCKER | Risk & Robustness | B1 — the rewritten cycles entry's `period` slot takes its value from ARGV, which is always a Lua string; the pre-existing scorer's bare `if period > 0` comparison (outside the pcall) then hard-errors, aborting every ranked query for that partition. The plan's `%.17g` key function covers matching only, never what is written into slot 1. | Technical Approach — `CYCLES_MERGE_LUA` contract |
| BLOCKER | Risk & Robustness + History & Consistency | B2 — `CYCLES_ADJUST_LUA`'s "empty bulk reply when no entry exists" contradicts the absolute "both scripts must return `cmsgpack.pack(...)`" rule two paragraphs later, and Task 2 never specifies the falsy-reply guard that replaces today's `if not raw: return []`; an unconditional `msgpack.unpackb` crashes `strengthen_cycle()` for a member with no stored cycles. | Technical Approach — `CYCLES_ADJUST_LUA` contract; Task 2 |
| CONCERN | History & Consistency | C1 — the #476 eager-EVAL precedent is mis-cited: that loop is at `base.py:1773-1786` and lives only in the internal-pipeline `else:` branch, while this plan ignores the `pipeline` kwarg in both branches, so the companion write now commits ahead of a caller's own explicit `pipe.execute()`. | Technical Approach — Placement |
| CONCERN | Risk & Robustness | C2 — the rolling-deploy mixed-version window is undocumented: script atomicity gives no exclusion against a still-running pre-fix process's client-side read-modify-write, so the race stays reachable until every writer is upgraded, yet Update System says no update-system changes are required. | Update System; Race Conditions |
| CONCERN | Scope & Value | C3 — Task 3's `POPOTO_REDIS_DB` conversion is import hygiene unrelated to atomicity; six of the sites sit in `export_state` / `import_state` / the ranking path, outside everything this plan rewrites. Already raised as the plan's own Open Question 3. | Task 3; Solution — Straggler cleanup |
| NIT | History & Consistency | N1 — the plan says "seven use sites"; the measured grep at `9986c086` shows one import plus nine use sites, and the "four of the seven sites" phrasing counts a fourth item against three grep hits. | Solution — Straggler cleanup; Task 3 |

### B1 (BLOCKER) — the rewritten cycles entry's `period` slot may become a Lua string, crashing the scoring script

*Critics: Risk & Robustness (corroborated by the aggregator's own read of
`cyclic_decay_field.py:163-166`).*
**Location:** Solution → Technical Approach → `CYCLES_MERGE_LUA` contract.

The `%.17g` key function is scoped only to **matching** a stored cycle against a
declared one. The plan never states what value is written into **slot 1** of the
rewritten `[period, amplitude, phase, declared_baseline]` entry. `ARGV` is always
a Lua string, and `period` is re-read from the declared side on every save
(`cyclic_decay_field.py:670-702`), so a literal port writes a **string-typed
period** for every declared cycle. The pre-existing scorer then does
`local period = c[1] … if period > 0 then` (`cyclic_decay_field.py:163-166`) — a
bare relational comparison **outside** the `pcall` (which guards only
`cmsgpack.unpack` at `:159-160`). Lua 5.1 hard-errors comparing string to number,
aborting the whole script: every `rank_decayed` / `top_by_decay` against that
partition raises `ResponseError`. This is the majority path for any model that
declares cycles, and no listed Test Impact file asserts the stored period's
*type* (`test_transfer_fidelity_fields.py`'s arity check is period-blind).

**Implementation Note:** add
`local function coerce_period(raw) local n = tonumber(raw); if n then return n end; return raw end`
and build each output entry as `{coerce_period(argv_period), amplitude, phase, new_baseline}`
— slot 1 must be the **coerced value**, never the raw ARGV string and never the
`'n:'`/`'s:'` match-key string. Add a test asserting a saved record's stored
cycles entry decodes with a numeric period, and a scoring-path test
(`top_by_decay`) that does not raise post-port.

Two related lower-severity notes on the entry's other ARGV-sourced slots:

- **Phase (slot 3)** has the identical ambiguity, but the scorer only uses phase
  in arithmetic (`now - phase`), which Lua 5.1 auto-coerces from a numeric
  string — no crash today. Apply the same conversion anyway for type purity
  (Risk 1's `isinstance` surprise applies here too).
- **`declared_baseline` (slot 4)** must be `tonumber()`-ed. Amplitude has no
  legitimate string case, and every reference script in the repo `tonumber()`s
  its numeric ARGV, so convention likely saves a builder here — but flag it in
  the script header, because the failure is **silent**: Lua's `==` never coerces
  across types and never errors, so an unconverted baseline makes the
  `baseline == declared` branch evaluate false on every save, permanently
  defeating #698's baseline preservation and discarding every learned amplitude
  with no crash and no distinguishing log line.

### B2 (BLOCKER) — `CYCLES_ADJUST_LUA`'s no-entry return contradicts the "always `cmsgpack.pack`" rule, and the Python guard is unspecified

*Critics: Risk & Robustness + History & Consistency (independent convergence;
elevated from CONCERN per the cross-validation rule).*
**Location:** Solution → Technical Approach → `CYCLES_ADJUST_LUA` contract vs.
"Return-value encoding is load-bearing"; Step by Step Tasks → Task 2.

"Both scripts must therefore return `cmsgpack.pack(...)` as a bulk string and let
Python `msgpack.unpackb` it" is stated as an absolute rule, but the
`CYCLES_ADJUST_LUA` contract two paragraphs earlier allows "**an empty bulk reply
when no entry exists**" — a second return shape that is not msgpack at all.
Today's code short-circuits explicitly on a falsy `HGET`
(`base.py:2748-2754`: `raw = get_REDIS_DB().hget(...)`, `if not raw: return []` /
`return pipeline`). After the port that check must move to the **EVAL's return
value**, and Task 2's steps never say so — a builder writing
`cycles = msgpack.unpackb(run_lua(...))` unconditionally crashes
`strengthen_cycle()` / `weaken_cycle()` for any member with no stored cycles,
where the pre-fix code cleanly returned `[]`.

**Implementation Note:** in the Lua, return an explicit `nil` (not `''`) for the
no-entry case — e.g. `if not redis.call('HGET', KEYS[1], ARGV[1]) then return nil end`
as an early exit. In `models/base.py`, guard before unpacking:
`result = run_lua(...)`, then
`return pipeline if isinstance(pipeline, redis.client.Pipeline) else ([] if not result else [c[:3] for c in msgpack.unpackb(result, raw=False)])`,
mirroring the `if not raw:` short-circuit it replaces. Also reconcile the
"load-bearing" paragraph so it carves out the no-entry case rather than stating
an absolute that the contract two paragraphs above contradicts.

### C1 (CONCERN) — the #476 eager-EVAL precedent is mis-cited; this plan goes further than it

*Critic: History & Consistency (verified by the aggregator against
`base.py:1700-1800`).*
**Location:** Solution → Technical Approach → Placement (option 2, chosen).

The plan says the eager placement is "exactly as `Model.save` already does for
`IndexedFieldMixin` hooks (`base.py:1755-1780`, #476)". Verified: that eager loop
is `_eager_indexed_fields` at **`base.py:1773-1786`**, and it sits **only** inside
the internal-pipeline `else:` branch beginning at `base.py:1760`. In the
**caller-supplied-pipeline** branch (`base.py:1690-1758`, which returns `pipeline`
for the caller to `.execute()` later), indexed-field `on_save()` is queued through
the same generic per-field loop as every other field (`base.py:1720-1729`) — no
eager special-casing exists there. This plan ignores the `pipeline` kwarg
**unconditionally, in both branches**, which is new ground rather than a repeat of
#476. (The cited range `1755-1780` also straddles the branch boundary and points
partly at the `EventStreamMixin` block at `:1754-1757`.)

**Implementation Note:** in the caller-pipeline branch the ported
`CyclicDecayField.on_save` runs its `EVAL` synchronously inside the
`field.on_save(...)` call at `base.py:1721` — i.e. it commits to Redis **before**
`pipe.execute()` commits the model hash and index writes queued alongside it on
the same `pipe`. That is exactly the ordering the worked example being deleted
from `docs/features/cyclic-decay-field.md:171-175` exercises. Correct the citation
to `base.py:1773-1786`, say plainly that this extends eager execution to the
caller-pipeline path too, and make Open Question 1's sign-off be about that actual
scope — the companion write now lands ahead of the caller's own explicit
`execute()`, not merely ahead of popoto's internal pipeline.

### C2 (CONCERN) — the rolling-deploy mixed-version window is not documented

*Critic: Risk & Robustness.*
**Location:** Update System; Solution → Placement; Race Conditions.

The plan treats this as zero-migration and purely internal ("no data migration —
the stored payload shape is byte-compatible before and after") and never
discusses a rolling deploy. Script atomicity serializes only against other
scripts and single commands on the server; it gives **no** exclusion against a
concurrent pre-fix process still doing client-side `HGET` → merge → `HSET`, whose
read can land between the new script's read and write. Until every writer process
is on post-fix code, the exact race being closed is still reachable. The repo
already calls out this class of hazard for the sibling #698 mechanism at
`docs/features/cyclic-decay-field.md:150-152`.

**Implementation Note:** state — next to that existing caveat in
`docs/features/cyclic-decay-field.md`, not only in this plan — that the fix is
complete only once every process writing a given `CyclicDecayField`-bearing model
is upgraded, so an operator does not read "no update-system changes" as "safe to
roll out gradually with no caveat."

### C3 (CONCERN) — Task 3 is unrelated scope inside a bug-fix plan

*Critic: Scope & Value. (The plan already surfaces this as Open Question 3; this
finding is the critic's recommendation on it, not a new discovery.)*
**Location:** Step by Step Tasks → Task 3; Solution → Key Elements → "Straggler
cleanup".

The `POPOTO_REDIS_DB` → `get_REDIS_DB()` conversion is import hygiene, not
atomicity. Sites at `:286`, `:301` (`export_state`), `:379`, `:387`
(`import_state`) and `:493`, `:501` (the ranking path) are outside everything
this plan rewrites and are converted purely to satisfy a `CLAUDE.md` rule, adding
diff surface to a review the Appetite section already forecasts as two rounds.

**Implementation Note:** if kept, scope Task 3 to the import line (`:49`) plus the
sites inside the rewritten `on_save` / `_adjust_cycle_amplitudes` / `on_delete`
code, leaving `export_state` / `import_state` / the ranking path to a separate
cleanup issue — and then drop the now-unreachable Verification anti-criterion
`grep -c "POPOTO_REDIS_DB" … == 0`, which only holds if **all** sites convert. If
split out entirely, also drop `tests/test_popoto_redis_db_rebind.py` and
`tests/test_connection.py` from Task 3's `Validates` list.

### N1 (NIT) — the `POPOTO_REDIS_DB` site count is wrong

*Critic: History & Consistency (measured by the aggregator).*
**Location:** Solution → "Straggler cleanup"; Task 3.

The plan says "seven use sites"; the measured grep of
`src/popoto/fields/cyclic_decay_field.py` at `9986c086` shows **one import
(`:49`) plus nine use sites** (`:286`, `:301`, `:379`, `:387`, `:493`, `:501`,
`:705`, `:719`, `:755`). The Solution's "four of the seven sites (`:705`, `:719`,
`:755`, plus the merge write)" also counts a fourth item against three grep hits.
Low risk — the Verification anti-criterion greps for zero regardless of the
narrative number — but the enumeration should be traceable to the grep.

### Round 1 fold-in — applied 2026-09-08T05:30:28Z

| Finding | Disposition | Where the plan now says it |
|---|---|---|
| B1 period/phase/baseline coercion | **Fixed** | Technical Approach → `CYCLES_MERGE_LUA` contract (per-slot table + `coerce_period` helper); Task 1 (new step); Test Impact (3 new tests); Success Criteria; Verification (2 rows) |
| B2 no-entry return shape | **Fixed** | Technical Approach → `CYCLES_ADJUST_LUA` contract (explicit `nil`, Python guard shown); "Return-value encoding is load-bearing" reconciled to two shapes; Task 2 (new steps); Test Impact; Success Criteria; Verification |
| C1 mis-cited #476 precedent | **Fixed** | Technical Approach → Placement (corrected to `base.py:1773-1786`, branch scope spelled out); Prior Art; Architectural Impact; Race 3; Open Question 1 restated at the true scope |
| C2 rolling-deploy window | **Fixed** | Update System (second paragraph); new Race 4; Documentation checklist; Task 6; Success Criteria; Verification |
| C3 Task 3 scope creep | **Kept, with rationale + escape hatch** | Solution → Straggler cleanup (scope-decision paragraph); Task 3 (scope note + exact removal procedure); Verification row annotated; Open Question 3 restated as non-blocking |
| N1 site count | **Fixed** | Solution → Straggler cleanup and Task 3 now say one import + **nine** sites, enumerated to the grep at `9986c086` |

Verified against the tree at `9986c086` while folding in: `grep -n POPOTO_REDIS_DB
src/popoto/fields/cyclic_decay_field.py` → 1 import + 9 uses (N1 confirmed);
`base.py:1773-1786` is `_eager_indexed_fields` inside the `else:` at `:1760`, and
`base.py:1720-1729` is the generic per-field `on_save` queue in the caller-pipeline
branch (C1 confirmed); `cyclic_decay_field.py:163-166` is a bare `if period > 0`
outside the `pcall` at `:159-160` (B1 confirmed); `base.py:2748-2754` is the
`if not raw: return []` short-circuit (B2 confirmed).

**Round 2** — 2026-09-08 · FULL depth · independent roster (3 critics: Risk &
Robustness, Scope & Value, History & Consistency) · **Verdict: READY TO BUILD
(with concerns)** (0 blockers, 4 concerns, 0 nits). All six round-1 findings
verified as landed in the plan body, not only in the fold-in table.

| Severity | Critic(s) | Finding | Location |
|---|---|---|---|
| CONCERN | Risk & Robustness (raised as BLOCKER; downgraded — see below) | C4 — the `CYCLES_MERGE_LUA` contract requires "an entry whose period slot is itself a table" to set `decode_failed = 1`, but the match-key function it supplies (`tonumber(v)` else `'s:' .. tostring(v)`) never errors on a table — `tostring({})` returns `"table: 0x…"` — so a builder implementing the key function faithfully silently *accepts* the case the same bullet says must fail. | Technical Approach — `CYCLES_MERGE_LUA` contract, the `pcall` bullet |
| CONCERN | History & Consistency | C5 — the plan uses two entry-slot numbering conventions without saying so. `CYCLES_MERGE_LUA`'s table is 1-based Lua (`Slot 4` = `declared_baseline`), but the `CYCLES_ADJUST_LUA` Key Element and Task 2 say "multiplies slot 2 … never writing or stripping **slot 3**" — "slot 3" is lifted verbatim from `base.py:2776`'s 0-based **Python** comment, where `cycle[3]` is the baseline. In Lua, `c[3]` is *phase*. | Solution → Key Elements (`CYCLES_ADJUST_LUA`); Task 2 |
| CONCERN | Risk & Robustness | C6 — a pipelined `strengthen_cycle`/`weaken_cycle` on a member with **no** stored cycles entry today returns `pipeline` at `base.py:2750-2753` having queued *nothing*; after the port `run_lua` unconditionally queues an `EVALSHA`, so the caller's `pipe.execute()` gains one result at a position it did not have before. The plan's "no public signature changes … keep their return contracts" does not cover this. | Technical Approach — "Pipeline handling after the change" table; Architectural Impact → Interface changes |
| CONCERN | Scope & Value | C7 — the C3 escape hatch says "**Task 3 depends on nothing that depends on it**", but Task 5 (`validate-cycles`) lists `build-redis-accessor` in its own `Depends On`, and Success Criterion "`POPOTO_REDIS_DB` no longer appears in `fields/cyclic_decay_field.py`" also references it. The written removal procedure names only the task, the Verification row and the `Validates` list — two of four edits. | Solution → Straggler cleanup; Task 3 removal procedure; Task 5 `Depends On`; Success Criteria |

### C4 (CONCERN) — the match-key function swallows the table-valued period case the same bullet requires it to fail

*Critic: Risk & Robustness. Raised as BLOCKER; **downgraded to CONCERN by the
aggregator** because the plan already states the required behavior in prose
("**any** failure … an entry whose period slot is itself a table … sets
`decode_failed = 1`") and because two existing tests catch a miss — this is a
missing mechanism for a stated requirement with a red-test feedback loop, not an
unbuildable gap. Contrast B1, where no existing test asserted the property at all
and three new ones had to be specified.*
**Location:** Solution → Technical Approach → `CYCLES_MERGE_LUA` contract.

Python reaches the fallback here by *crashing*: `learned.setdefault(entry[0], …)`
at `cyclic_decay_field.py:647` raises `TypeError: unhashable type: 'list'` inside
the `try`, which is why the guard at `:650-661` deliberately wraps decode **and**
normalization. Lua has no unhashable-key failure — the plan's key function turns
a table into the string `"table: 0x…"` and merges happily. Two existing tests,
both of which the plan requires to pass **unchanged**, write exactly this payload
and assert the whole bucket is discarded with the warning:

- `tests/test_cyclic_decay_field.py:1512` `test_unhashable_period_falls_back_instead_of_raising`
  — stores `[[[TemporalPeriod.DAILY], 9.0, 0]]`, asserts the declared `2.0` comes
  back and `"Could not decode cycles"` is logged.
- `tests/test_cyclic_decay_field.py:1534` `test_partial_merge_discarded_when_a_later_entry_is_malformed`
  — first entry well-formed, second table-valued; asserts **both** declared
  cycles fall back together (`2.0` and `3.0`).

**Implementation Note:** in the per-entry loop that buckets stored cycles, guard
the raw period *before* building the match key:
`if type(p) ~= 'number' and type(p) ~= 'string' then decode_failed = 1; learned = {}; break end`
— clearing the bucket table on the same all-or-nothing path as the top-level
`pcall` failure, which is what makes the second test's "both fall back together"
assertion hold. Note this is a **type** guard, not a `pcall`: nothing raises in
Lua, so there is no error to catch.

### C5 (CONCERN) — `CYCLES_ADJUST_LUA`'s protected slot is named in the wrong numbering base

*Critic: History & Consistency.*
**Location:** Solution → Key Elements (`CYCLES_ADJUST_LUA` bullet); Task 2, last step.

`CYCLES_MERGE_LUA`'s per-slot table fixes 1-based Lua numbering
(`1 period, 2 amplitude, 3 phase, 4 declared_baseline`) and uses it consistently.
The adjust script's description shifts *amplitude* into that base ("slot 2",
correct for Lua) but leaves the baseline reference at "slot 3", copied from
`base.py:2776`'s 0-indexed Python comment. A builder writing Lua from the Key
Elements bullet or Task 2 alone would protect `c[3]` — the phase — and consider
`c[4]` fair game, silently destroying #698's baseline: the identical silent,
crashless failure mode B1 warned about for the merge script.

**Implementation Note:** the correct instruction is already in the Technical
Approach paragraph — "Mutates only index 2 of each entry, re-packs the same
tables, so a 4-slot entry keeps its baseline". Build from that. Fix the two
downstream restatements to say **slot 4 (`declared_baseline`)**, or drop the slot
number entirely and say "re-packs the entry unmodified except index 2", which
needs no numbering base at all. Do not `table.remove`, re-length, or rebuild the
entry table — mutate `c[2]` in place and re-pack the same table.

### C6 (CONCERN) — the no-entry pipelined call now queues a command where it queued none

*Critic: Risk & Robustness.*
**Location:** Technical Approach → "Pipeline handling after the change"; Architectural Impact → Interface changes.

At `base.py:2749-2754` the existence check is a **direct** `HGET`, and on a miss
the method returns `pipeline` immediately without touching it. After the port the
existence check moves inside the script, so `run_lua(pipeline, …)` queues an
`EVALSHA` on **every** call. The Python-visible return is unchanged (still the
pipeline), but `pipe.execute()`'s result list grows by one entry and every later
position shifts for a caller that indexes it. This is a genuine behavior change
that "no public signature changes … keep their return contracts" does not
describe.

**Implementation Note:** no code fix — the queued command is required for
atomicity and is the right trade. Disclose it: add a line to Architectural Impact
→ Interface changes and a step to Task 2 stating that a pipelined
`strengthen_cycle`/`weaken_cycle` against a member with no stored cycles now
contributes one result to `pipe.execute()` where it previously contributed none.
`tests/test_observation_protocol.py:693-701` (`test_pipeline_support`) only
asserts `result is pipe` and will not catch it; extend it, or add a test asserting
`len(pipe.execute())` for the no-entry pipelined case, so the shift is recorded
rather than discovered.

### C7 (CONCERN) — the C3 escape hatch's removal procedure is two edits short of executable

*Critic: Scope & Value.*
**Location:** Solution → Straggler cleanup (the C3 scope-decision paragraph); Task 3 scope note; Task 5 `Depends On`; Success Criteria.

The scope decision is defended on the grounds that the hatch is "structural, not
a promise: **Task 3 depends on nothing that depends on it**". Task 5
(`validate-cycles`) lists `build-redis-accessor` in its `Depends On` — the task
note concedes this parenthetically ("a sequencing entry") while the Key Elements
paragraph asserts the opposite, and the written removal procedure covers only
three of the four places that reference Task 3. Deleting the task as written
leaves a dangling `Depends On` ID (which the structural check verifies) and an
unsatisfiable Success Criterion.

**Implementation Note:** if Open Question 3 is answered "split it out", the edit
is **four** places, not two: (1) delete Task 3; (2) delete the
`grep -c "POPOTO_REDIS_DB" … == 0` Verification row; (3) drop
`tests/test_popoto_redis_db_rebind.py` / `tests/test_connection.py` from the
`Validates` list *(already written down)*; (4) **remove `build-redis-accessor`
from Task 5's `Depends On` and delete the Success Criterion
"`POPOTO_REDIS_DB` no longer appears in `fields/cyclic_decay_field.py`"**. The
plan's default remains "keep Task 3", under which no edit is needed; this only
makes the hatch actually executable. Also reconcile the Key Elements sentence
with the task note — one of the two is wrong as written.

### Round 2 fold-in — applied 2026-09-08T05:44:30Z

The G2 critique cap (2 cycles) is exhausted; there is no round 3. All four round-2
concerns are folded into the plan **body**, not only into this table.

| Finding | Disposition | Where the plan now says it |
|---|---|---|
| C4 table-valued period accepted by the match-key function | **Fixed** | Technical Approach → `CYCLES_MERGE_LUA` contract (new "type guard, not a `pcall`" bullet with the verbatim Lua, plus a note on the match-key bullet forbidding a `pcall`/`tostring` fallback); Task 1 (new step); Test Impact (`:1512`/`:1534` named as the acceptance tests, must pass unmodified); Success Criteria; Verification (2 rows) |
| C5 mixed 1-based Lua / 0-based Python slot numbering | **Fixed** | Solution → Key Elements: `CYCLES_ADJUST_LUA` bullet rewritten to "index 2 in place / index 4 untouched", plus a new **"Slot numbering is 1-based Lua everywhere in this plan"** bullet fixing the convention and translating both halves of `base.py:2774-2777`; Technical Approach → `CYCLES_ADJUST_LUA` contract; Task 2 (step rewritten — "slot 3" removed); Inline Documentation; Verification (the round-2 row was `grep -c "slot 3"` == 0 — **superseded by the round-3 fold-in below**, see C9) |
| C6 pipelined no-entry call now queues an `EVALSHA` | **Disclosed (no code fix — correct trade)** | Architectural Impact → Interface changes (new bullet); Technical Approach → "Pipeline handling after the change" (new table row); Task 2 (new step); Test Impact (`test_observation_protocol.py:693-701` to be extended); Success Criteria; Verification |
| C7 escape hatch is two edits short of executable | **Fixed** | Solution → Straggler cleanup (the false "depends on nothing that depends on it" claim replaced with the accurate narrower one); Task 3 scope note now enumerates the **four** edits; Task 5's `Depends On` annotated as a sequencing edge; the `POPOTO_REDIS_DB` Success Criterion annotated as Task-3-conditional |

Verified against the tree while folding in: `tests/test_cyclic_decay_field.py:1512` and
`:1534` are `test_unhashable_period_falls_back_instead_of_raising` and
`test_partial_merge_discarded_when_a_later_entry_is_malformed` (C4 confirmed);
`tests/test_observation_protocol.py:693` is `test_pipeline_support` (C6 confirmed);
`base.py:2748-2755` is the `if not raw:` short-circuit returning `pipeline` or `[]`
with **no** command queued (C6 confirmed); `base.py:2774-2777` reads "mutates only
index 1 … slot-3 writer/deleter" in 0-based Python (C5 confirmed).

**Round 3 (concern re-critique)** — 2026-09-08 · FULL depth · independent roster
(3 critics: Risk & Robustness, Scope & Value, History & Consistency) · **Verdict:
READY TO BUILD (with concerns)** (0 blockers, 2 concerns, 0 nits).

This is the router's row-2b concern round (the critique verdict was stale after the
round-2 fold-in), bounded by `MAX_CONCERN_RECRITIQUE_ROUNDS` — **not** a third G2
cycle. The round-2 fold-in note above says "there is no round 3" of the G2 loop;
that remains true and is not contradicted by this entry. Scope was restricted to
verifying that C4–C7 landed correctly in the plan body. Scope & Value and History &
Consistency each returned **No findings** — the C4/C5/C6/C7 body text, its citations
(`base.py:2774-2777`, `tests/test_cyclic_decay_field.py:1512`/`:1534`,
`tests/test_observation_protocol.py:693-701`) and the Task 3 four-edit escape hatch
all verified accurate. The two findings below are **not** about the folded reasoning,
which is correct; they are about two of the *Verification rows* the fold-in added,
both of which are vacuous or unsatisfiable as written.

| Severity | Critic(s) | Finding | Location |
|---|---|---|---|
| CONCERN | Risk & Robustness (measured independently by the aggregator before dispatch) | C8 — the C6 verification row `grep -c "pipe.execute()" tests/test_observation_protocol.py` \| `output > 0` **already returns 1 on unmodified `main`** (the pre-existing `test_pipeline_support` at `:701`). The gate is green before the C6 test exists and stays green if the builder skips it. | Verification table, "Pipelined no-entry result shift pinned by a test (C6)" |
| CONCERN | Risk & Robustness (measured independently by the aggregator before dispatch) | C9 — the C5 verification row `grep -c "slot 3" src/popoto/fields/cyclic_decay_field.py` \| `== 0` **cannot reach 0**: four legitimate pre-existing 0-based *Python* uses live at `:344`, `:558`, `:619`, `:680`, three of them (`export_state`, and the `on_save` docstring/comment prose) in code no task rewrites. As written the gate either fails the build or forces edits to correct, out-of-scope comments. | Verification table, "Baseline slot protected in Lua terms, not Python's (C5)" |

### C8 (CONCERN) — the C6 verification row is vacuous: it passes on unmodified `main`

*Critic: Risk & Robustness, corroborated by the aggregator's own pre-dispatch
measurement.*
**Location:** Verification table, row "Pipelined no-entry result shift pinned by a
test (C6)".

Measured at HEAD `4cb3cd24`: `grep -c "pipe.execute()"
tests/test_observation_protocol.py` → **1**, from the bare `pipe.execute()` at
`tests/test_observation_protocol.py:701` inside the pre-existing
`test_pipeline_support` — the very test the plan says "only asserts `result is pipe`
… and cannot see" the shift. The row therefore certifies nothing: it is satisfied by
the code the concern exists to strengthen. This is the #661 vacuity trap in
verification-row form — a check that cannot distinguish done from not-done.

**Implementation Note:** retarget the row at a string that is **absent today** and
present only once the C6 test lands. The plan's own prose already names the
assertion shape ("add a sibling test asserting `len(pipe.execute())` for the no-entry
pipelined case"), so use:
`grep -c "len(pipe.execute())" tests/test_observation_protocol.py` | expected
`output > 0`. Confirm it reads **0** before the build starts — if it does not, pick a
different unique literal (e.g. the new test's function name) rather than shipping a
row that is already green.

### C9 (CONCERN) — the C5 verification row is unsatisfiable without editing out-of-scope comments

*Critic: Risk & Robustness, corroborated by the aggregator's own pre-dispatch
measurement.*
**Location:** Verification table, row "Baseline slot protected in Lua terms, not
Python's (C5)".

`grep -n "slot 3" src/popoto/fields/cyclic_decay_field.py` at `4cb3cd24` returns
**four** hits, every one a correct 0-based *Python* reference to `entry[3]`:

- `:344` — `export_state`'s docstring ("never carries slot 3 through"). `export_state`
  is explicitly outside this plan's rewrite ("the other six are in `export_state`
  (`:286`, `:301`), `import_state` … and the ranking path").
- `:558` — the `on_save` docstring's baseline rule ("a non-numeric slot 3").
- `:619`, `:680` — `on_save` comments ("baseline is None when slot 3 is …",
  "Baseline unknown (legacy entry, or corrupt slot 3)").

The C5 concern is about a *Lua* instruction inheriting Python's numbering, not about
these Python comments, which are right as they stand. A whole-file ban on the
substring is over-broad in one direction (it flags correct prose) while still being
weak in the other: `base.py:2776`'s actual wording is **`slot-3`** with a hyphen, so a
builder who copies that phrase verbatim into the new Lua is **not** caught by a grep
for `slot 3` at all.

**Implementation Note:** replace the row with a check that is both scoped and
hyphen-tolerant. Either (a) scope it to the new script literal —
`sed -n '/CYCLES_ADJUST_LUA = /,/^"""/p' src/popoto/fields/cyclic_decay_field.py |
grep -Ec "slot[ -]3"` | expected `0` (adjust the `sed` range to the literal's actual
delimiters once written) — or (b) grep the whole file for the specific mis-copied
phrase, `grep -Ec "slot[ -]3 (writer|writer/deleter)"
src/popoto/fields/cyclic_decay_field.py` | expected `0`, which measures 0 today and
is a true signal. Pair it with a positive check that the protection actually exists,
e.g. `grep -c "c\[4\]" src/popoto/fields/cyclic_decay_field.py` | `output > 0`. Do
**not** satisfy the row as currently written by editing `:344`/`:558`/`:619`/`:680`
— those are correct Python-side comments and rewording them is out of scope.

### Round 3 fold-in — applied 2026-09-08T05:54:25Z

Targeted pass. Both round-3 concerns are defects in *Verification rows* the round-2
fold-in added, not in the folded reasoning; no plan reasoning, task, or scope was
re-opened. Each replacement command was **run against the tree at `3ed94376` before
being written into the table** — a verification row that has never been executed is
what produced C8 and C9 in the first place.

| Finding | Disposition | Where the plan now says it |
|---|---|---|
| C8 the C6 row is vacuous (green on unmodified `main`) | **Fixed** | Verification table: the C6 row now greps `len(pipe.execute())` in `tests/test_observation_protocol.py` (`output > 0`). Measured **0** at `3ed94376`, so it is red pre-build and can only go green once the sibling no-entry-length test lands. The row records why the bare `pipe.execute()` form (which measures **1**, from `test_pipeline_support:701`) was rejected |
| C9 the C5 row is unsatisfiable and hyphen-blind | **Fixed** | Verification table: the single whole-file `grep -c "slot 3"` == 0 row is replaced by a **pair** — a scoped, hyphen-tolerant negative (`grep -Ec "slot[ -]3 writer"`, measured **0** at `3ed94376`, and it *does* match `base.py:2776`'s `slot-3 writer/deleter` so a verbatim copy into the new Lua is caught) plus a positive (`grep -c "c\[4\]"`, measured **0** at `3ed94376`, green only once `CYCLES_ADJUST_LUA` names the baseline in 1-based Lua). The row carries an explicit do-not-revert note that the whole-file form returns **4** from correct 0-based Python prose at `:344`, `:558`, `:619`, `:680`, which no task rewrites. The round-2 fold-in table's stale pointer to the old row is annotated as superseded |

Measured at `3ed94376` while folding in (`SDLC_TARGET_REPO`/main checkout):
`grep -c "len(pipe.execute())" tests/test_observation_protocol.py` → **0**;
`grep -c "pipe.execute()" tests/test_observation_protocol.py` → **1** (C8 confirmed);
`grep -Ec "slot[ -]3 writer" src/popoto/fields/cyclic_decay_field.py` → **0**;
`grep -c "c\[4\]" src/popoto/fields/cyclic_decay_field.py` → **0**;
`grep -c "slot 3" src/popoto/fields/cyclic_decay_field.py` → **4** (C9 confirmed);
`sed -n '2770,2780p' src/popoto/models/base.py` contains the literal
`slot-3 writer/deleter` (C9's hyphen-blindness confirmed).

Option (a) from C9's Implementation Note — a `sed` range scoped to the
`CYCLES_ADJUST_LUA` literal — was **rejected**: that literal does not exist yet, so
the `sed` range yields nothing and `grep -c` returns 0 vacuously today, reproducing
exactly the defect C8 names. Option (b) plus the positive `c[4]` check was taken
instead.

### Structural check results

**Round 2** — re-measured at HEAD `3f768d8a`.

| Check | Status | Detail |
|---|---|---|
| Required sections | PASS | All plan-template sections present and non-empty |
| Task numbering | PASS | Tasks 1-7, no gaps |
| Dependencies valid | PASS | All `Depends On` IDs resolve; no cycles |
| File paths exist | PASS | 16/17; `tests/test_cyclic_decay_atomicity.py` is intentionally new (Task 4) |
| Prerequisites met | PASS (3/4) | Redis 8.6.2 reachable on DB 9; `eval "return type(cmsgpack)"` → `table`; `msgpack`/`redis`/`pytest` importable. `POPOTO_TEST_DB` was unset in the critique shell — the build lane must export `9` |
| Cross-references | PASS | Every Success Criterion maps to a task; Rabbit Holes (`write_filter.py`, `resolve_pressure`) are consistently excluded in the tasks |
| Task numbering (r2) | PASS | Tasks 1-7, no gaps |
| Dependencies valid (r2) | PASS | All 7 `Depends On` IDs resolve; no cycles. (Note C7: they resolve only while Task 3 is kept) |
| File paths exist (r2) | PASS | 16/17; `tests/test_cyclic_decay_atomicity.py` is intentionally new (Task 4) |
| Prerequisites met (r2) | PASS (3/4) | `redis-cli -u redis://localhost:6379/9 ping` → `PONG` (Redis 8.6.2); `eval "return type(cmsgpack)" 0` → `table`; `import msgpack, redis, pytest` → ok. `POPOTO_TEST_DB` unset in the critique shell — the build lane must export `9` |
| Round-1 fold-in landed (r2) | PASS | B1, B2, C1, C2, C3, N1 each verified present in the plan **body**, not only the fold-in table. N1's count re-measured: 1 import (`:49`) + 9 uses |

**Round 3** — re-measured at HEAD `4cb3cd24`.

| Check | Status | Detail |
|---|---|---|
| Required sections (r3) | PASS | All plan-template sections present and non-empty |
| Task numbering (r3) | PASS | Tasks 1-7, no gaps |
| Dependencies valid (r3) | PASS | All `Depends On` IDs resolve to real task IDs; no cycles |
| File paths exist (r3) | PASS | Every referenced source/test/doc path exists except `tests/test_cyclic_decay_atomicity.py`, intentionally new (Task 4) |
| Cross-references (r3) | PASS | Every Success Criterion maps to a task; Rabbit Holes stay excluded from the tasks |
| Round-2 fold-in landed (r3) | PASS | C4 (type guard + verbatim Lua, Task 1 step, `:1512`/`:1534` named), C5 (1-based-Lua bullet, Key Elements + Task 2 rewritten, "slot 3" removed from the Lua instructions), C6 (Architectural Impact bullet, pipeline-table row, Task 2 step, Test Impact), C7 (four-edit removal procedure, Task 5 sequencing annotation, conditional Success Criterion) each verified present in the plan **body** |
| Verification rows are non-vacuous (r3) | **FAIL (2 of 24)** | Pre-measured at `4cb3cd24`: `type(p) ~= 'number'` → 0, `CYCLES_MERGE_LUA` → 0, `CYCLES_ADJUST_LUA` → 0, `hget(cycles_hash_key)` → 1 (cdf) / 1 (base), `hget(pressure_hash_key)` → 1, `POPOTO_REDIS_DB` → 10, `save first` → 1, `rolling deploy` → 1 — all correctly red pre-build. The two exceptions are the C6 row (`pipe.execute()` → **1**, already green: C8) and the C5 row (`slot 3` → **4**, unsatisfiable in scope: C9) |

---

## Open Questions

The issue's own three open questions are **answered in this plan**, by evidence
rather than by assumption:

1. *Do both writers become Lua, or does `_adjust_cycle_amplitudes` fold into the
   save-side script?* → **Two separate scripts.** They have different KEYS sets
   (the merge touches both companion hashes, the adjust only cycles), different
   ARGV shapes, and different call frequencies; folding them would force every
   `strengthen_cycle` to carry the full declared-cycle list it has no reason to know.
2. *Does the pipeline contract at `cyclic_decay_field.py:533-536` (now `:590-593`)
   survive?* → **It is deleted.** The hazard it documents is the bug; once the read
   and the write are one operation, "queue the save first" describes nothing.
3. *Does the pressure companion hash have the same shape?* → **No — it is
   asymmetric** (spike-4). `resolve_pressure` never reads, so only `on_save` needs
   fixing and `resolve_pressure` is left untouched.

What still needs supervisor input:

1. **The eager-write placement — restated with the corrected scope (critique C1).**
   `on_save`'s companion write stops being queued, in **both** branches of
   `Model.save`. The #476 precedent originally cited for this covers only the
   internal-pipeline branch (`base.py:1773-1786`, inside the `else:` at `:1760`); in
   the caller-supplied-pipeline branch, indexed `on_save` is queued through the
   generic loop at `:1720-1729` and nothing runs eagerly today. So the actual
   decision is broader than the precedent: **a caller who builds their own pipeline
   and calls `record.save(pipeline=pipe)` will have the companion hashes written
   before their `pipe.execute()` runs** — the companion write lands ahead of the
   model hash and index writes queued alongside it, and a failure at `execute()`
   leaves an inert orphan companion entry (Risk 2, Race 3).
   The alternative (queue the EVAL) keeps the write inside the caller's transaction
   but makes the #698 reset log and the decode warning unreachable from `on_save` —
   the exact observability #698's round-2 critique (C8) hardened.
   Confirm the trade at that scope, not merely at #476's.
2. **Numeric type drift (Risk 1).** Values round-trip exactly, but integral
   amplitudes come back as `int` instead of `float` once Lua does the packing
   (measured, spike-2). The plan coerces at popoto's own read boundaries and
   documents it. Is that acceptable, or is preserving the stored msgpack *type* a
   requirement — which would mean a different on-disk encoding and a migration?
3. **Straggler scope (Task 3) — critique C3.** Converting this module's stale
   `POPOTO_REDIS_DB` import is small (one import + nine sites, one file, no behavior
   change), is called for by `CLAUDE.md`, and fixes a latent wrong-database read on
   the pressure path at `:719` — but six of the nine sites (`export_state`,
   `import_state`, the ranking path) are unrelated to atomicity. **The plan's default
   is to keep it**, on the grounds that a partial conversion leaves the file in the
   half-converted state #655 warns about. This question is now non-blocking: Task 3
   is isolated (no task consumes what it produces), and the removal procedure if the
   answer is "split it out" is written into the task itself as **four** explicit edits
   — delete the task, delete the `grep -c "POPOTO_REDIS_DB" … == 0` Verification row,
   drop the two test files from `Validates`, and remove `build-redis-accessor` from
   Task 5's `Depends On` together with the matching Success Criterion (critique C7).
   Answer at leisure; the build can proceed on the default.
