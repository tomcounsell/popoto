---
status: Planning
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/699
last_comment_id: none
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
- **#476 / `base.py:1755-1780`** — indexed/unique `on_save` hooks were made **eager direct EVALs** rather than pipeline-queued, precisely because a queued script cannot report its outcome in time to change the caller's behavior. That is the precedent for the placement decision in Solution.
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
- **Behavioral change at the seam**: `on_save`'s companion-hash writes stop being queued on the caller's pipeline and execute eagerly (see Solution → Placement, and Risk 2). This mirrors #476's eager indexed-field EVALs.
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
