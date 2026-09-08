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

### Key Elements

- **`CYCLES_MERGE_LUA`** (new, in `fields/cyclic_decay_field.py`): owns the whole
  `on_save` companion write. `KEYS = [cycles_hash, pressure_hash]`. Performs the
  #698 three-way cycles merge **and** the pressure rate-refresh/first-save branch
  **and** the two `HDEL` branches, then returns a msgpack-encoded decision report.
- **`CYCLES_ADJUST_LUA`** (new, same module, imported by `models/base.py`): owns
  `_adjust_cycle_amplitudes`. `KEYS = [cycles_hash]`. Multiplies slot 2 of every
  entry, clamps, and re-packs with whatever arity the entry had — never writing or
  stripping slot 3.
- **Decision report**: both scripts return `cmsgpack.pack(...)`, never bare Lua
  values, so Python keeps float amplitudes and structured reset records. Python
  logs the #698 reset lines and the "could not decode" warning from that report,
  so *observable* behavior is unchanged even though the decision moved server-side.
- **`resolve_pressure` and `import_state` are untouched** — both are blind single
  writes and already atomic (spike-4).
- **Straggler cleanup**: the module-level `from ..redis_db import POPOTO_REDIS_DB`
  in `cyclic_decay_field.py:49` and its seven use sites go away in favour of
  `get_REDIS_DB()`. `CLAUDE.md` names this file as one of the two remaining stale
  importers, held back only because #679/#698 were editing it; four of the seven
  sites (`:705`, `:719`, `:755`, plus the merge write) are inside the code this
  plan rewrites, and the file *already* mixes both forms (`:623` uses the accessor,
  `:719` does not), which is a latent read-from-the-wrong-database bug on the
  pressure path. `fields/write_filter.py` stays stale — #494 owns it.

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
   these two writes, exactly as `Model.save` already does for `IndexedFieldMixin`
   hooks (`base.py:1755-1780`, #476), where the reason was identical: a script whose
   outcome must change the caller's behavior cannot be queued. The log and the
   warning keep working, and the "read direct / write queued" split that caused this
   bug disappears rather than being narrowed.

Chosen: **(2)**. The cost is stated in Risk 2 and is not hidden.

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
- Bucket stored entries by period **in stored order**, pop index 1 per declared
  cycle: FIFO within duplicate periods, preserving today's semantics. Pop the
  `(amplitude, baseline)` pair together as one decision.
- Period matching must be type-safe, because a period can be a string (spike-1) and
  because Lua's default `tostring` on a float does not agree with Python's `repr`.
  Normalize both sides through one key function: `tonumber(v)` succeeds →
  `'n:' .. string.format('%.17g', n)`, else `'s:' .. tostring(v)`. Declared periods
  arrive as `str(period)` in ARGV and take the same path, so the two sides agree by
  construction.
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
-> cmsgpack.pack(cycles) after the write, or an empty bulk reply when no entry exists
```

Mutates only index 2 of each entry; re-packs the same tables, so a 4-slot entry
keeps its baseline (the property `base.py:2770-2779` currently documents at length).
The clamp constants stay Python-side and arrive as ARGV so they remain single-sourced.

**Return-value encoding is load-bearing.** Redis converts Lua numbers to *integers*
when returning them over the protocol — `return 2.4` reaches the client as `2`.
Both scripts must therefore return `cmsgpack.pack(...)` as a bulk string and let
Python `msgpack.unpackb` it. Returning bare tables would silently truncate every
fractional amplitude in `strengthen_cycle`'s return value.

**Pipeline handling after the change:**

| Call site | Before | After |
|---|---|---|
| `on_save` cycles + pressure | read direct, write via pipeline | one eager direct `EVAL` (pipeline kwarg ignored for these two writes; still forwarded to `super().on_save()`) |
| `_adjust_cycle_amplitudes`, no pipeline | `HGET` + `HSET` | direct `EVAL`, returns packed cycles |
| `_adjust_cycle_amplitudes`, pipeline | read direct, `HSET` queued | `run_lua` queues the `EVALSHA`; returns the pipeline, exactly as today |
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
matches the #476 precedent. Closing it means making the whole save one transaction,
which is out of scope.

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
