---
status: Planning
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/698
last_comment_id: none
---

# #698 — An edited declared amplitude cannot override an already-learned one

## Problem

A developer ships a model with a `CyclicDecayField`, records accumulate learned
amplitudes through `strengthen_cycle()` / `weaken_cycle()`, and then the
developer decides the declared amplitude was wrong:

```python
class Directive(popoto.Model):
    relevance = CyclicDecayField(cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)])
    #                                                             ^^^ edited to 1.0
```

They deploy. Nothing happens. Every record that has already learned an
amplitude for `QUARTERLY` keeps its learned value forever; records that have
*not* learned one pick up `1.0`. There is no error, no warning, no log line —
and the split across records depends on interaction history, so the same
deployment behaves two ways.

**Current behavior:**

Since #679 (PR #687, `baa9956c`), `CyclicDecayField.on_save` treats a cycle's
`period` and `phase` as declarative (refreshed from `field.cycles` on every
save) and its `amplitude` as learned (carried over from the stored entry when
one exists — `cyclic_decay_field.py:596-599`). That merge rule is right for
"learning diverged from the declaration" and wrong for "the developer edited
the declaration", and popoto stores nothing that can tell the two apart: the
cycles companion hash holds `[period, amplitude, phase]` and no record of the
declaration that was in force when the amplitude was learned.

**Desired outcome:**

`on_save` compares the incoming declared amplitude against the **declaration in
force when the value was learned**, not against the learned value. When the
declaration has changed, the declaration wins and the member's learned
amplitude is reset to the new declared value. When it has not changed, learning
wins exactly as it does today. Records with no recorded baseline (everything
written before this change) degrade to today's behavior on the first save and
acquire a baseline from that save forward.

## Freshness Check

**Baseline commit:** `c046e1bd` (`origin/main`, `refactor(#655): resolve the
Redis client at call time in 29 modules (#697)`)
**Issue filed at:** 2026-09-07T11:21:15Z
**Disposition:** Unchanged

The issue cites no `file:line` pointers — it cites PR/commit identities — so
the drift check is over those plus the code the issue describes in prose. All
of it was re-verified by reading `src/popoto/fields/cyclic_decay_field.py` at
`c046e1bd`.

**Claims re-verified against current `main`:**

| Claim | Status |
|---|---|
| `on_save` preserves learned amplitudes rather than rewriting from `field.cycles` | Confirmed — `cyclic_decay_field.py:560-599` (the `learned` dict + FIFO-by-period match) |
| popoto stores no declared baseline | Confirmed — the stored entry is `[period, amplitude, phase]` (`:599`); no baseline exists in the cycles hash, the model hash, or anywhere else |
| A record with nothing learned picks up the new declaration | Confirmed — `bucket = learned.get(period)`; falsy bucket leaves `amplitude` at the declared value (`:596-598`) |
| `phase` is refreshed from the declaration on every save | Confirmed — `:592`, `:599`; nothing in the codebase ever mutates a stored `phase` |
| No test encodes the "declared wins" behavior that #679 removed | Confirmed — `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave` asserts the opposite in TC4/TC5 |

**Cited sibling issues/PRs re-checked:**

- **#679** — CLOSED 2026-09-07T11:20:24Z. Resolution is PR #687; this issue is
  its recorded consequence 2, not a regression in it.
- **PR #687** — MERGED 2026-09-07T11:20:23Z at `baa9956c`. Merge-gate accept
  comment [`#687#issuecomment-5569845442`](https://github.com/tomcounsell/popoto/pull/687#issuecomment-5569845442)
  re-read: it names this exact gap as consequence 2 and states it is "being
  tracked as its own issue rather than folded in here."
- **#554**, **#556** — both CLOSED (shipped as PR #558 / PR #675). Their
  deferral of the save-semantics change is already spent: #687 made the change.
  They are context, not a live blocker.

**Commits on main since issue was filed (touching referenced files):**

- `c046e1bd` `refactor(#655): resolve the Redis client at call time in 29
  modules (#697)` — **irrelevant to the premise, relevant to the edit.** It did
  **not** touch `src/popoto/fields/cyclic_decay_field.py` (verified with
  `git show --stat`); per `CLAUDE.md` that file is one of the two deliberately
  held back from the #655 sweep, so it still carries a module-level
  `from ..redis_db import POPOTO_REDIS_DB` alongside `get_REDIS_DB`. It *did*
  convert `models/base.py`, so `_adjust_cycle_amplitudes` now resolves the
  client through `get_REDIS_DB()`. See Rabbit Holes for why this plan does not
  finish that conversion.

**Active plans in `docs/plans/` overlapping this area:** none.
`cyclic_decay_field.md` is Archived (#196, shipped);
`cyclic_decay_on_save_amplitude_clobber.md` is the #679 plan, now merged;
`observation_unsaved_cyclic_degradation.md` is #583 (unsaved-instance
degradation in `observation.py`), a different concern in the same field family
and already Ready/shipped. No open `plan`-labeled issue other than #698 touches
`CyclicDecayField`.

**Bug reproduction:** the defect is a *design gap*, not a crash, so
"reproduction" is the assertion in TC4 of the #679 suite
(`test_cycle_added_to_declaration_uses_declared_amplitude`, line ~1390) read in
reverse: it swaps `field.cycles` to a new declaration and asserts the learned
`6.0` survives. That test passing on `c046e1bd` *is* the symptom.

## Prior Art

- **#196 / PR #201** (2026-03-13): added `CyclicDecayField`. The original
  `on_save` wrote declared amplitudes unconditionally — declaration always won,
  learning never survived.
- **#206** (`4a8a6a33`, 2026-03-14): added `strengthen_cycle` / `weaken_cycle`
  without touching the field file, making the learning methods silently inert
  across saves. This is the origin of #679.
- **#554 / PR #558**, **#556 / PR #675**: generic export/import and round-trip
  carry. Both explicitly declined to change in-place save semantics
  (`docs/plans/generic_export_import_roundtrip.md:722`). Relevant here because
  they established `roundtrip_policy = "carry"` and the
  `export_state`/`import_state` pair that this plan must keep consistent with a
  4-element stored tuple.
- **#679 / PR #687** (`baa9956c`, merged 2026-09-07): inverted the merge rule to
  "learned wins", with periods/phases still declarative. Direct predecessor;
  this plan adds the missing third input (the baseline) so the rule can be
  conditional instead of unconditional.
- **#583 / PR #615**: unsaved-instance degradation for the same field family.
  No overlap with the merge rule; listed so a reviewer does not mistake it for
  one.
- **#415**: phase-unit mismatch between temporal discovery and
  `CyclicDecayField`. Touches `phase`, which this plan deliberately leaves
  declarative. No interaction.

## Research

Skipped per `/do-plan` Phase 0.7 — this work is purely internal. It changes the
shape of a msgpack payload popoto itself writes and reads, inside popoto's own
Lua script and `on_save`. No external library, API, or ecosystem pattern is
introduced or newly relied on; `msgpack` and the Redis Lua `cmsgpack` binding
are both already load-bearing on this exact path, and their behavior under the
proposed change was verified empirically instead (see spike-1).

No relevant external findings — proceeding with codebase context and the spike.

## Spike Results

Appetite is Small, so the spike budget is 2. One was run empirically; the
second was resolvable by code-read and is recorded as such.

### spike-1: A 4th element in a stored cycle tuple is invisible to `CYCLIC_DECAY_LUA`

- **Assumption**: "Appending a declared-baseline slot to the stored cycle tuple
  (`[period, amplitude, phase, declared]`) does not change any score the ranking
  Lua produces, so the read path needs no change and old/new payloads can
  coexist in one hash."
- **Method**: prototype (live, against Redis DB 12)
- **Finding**: **Confirmed.** Two members in one sorted set, identical
  timestamps, one with `[[86400, 5.0, 0]]` and one with `[[86400, 5.0, 0, 2.0]]`
  in the cycles hash, evaluated through the real `CYCLIC_DECAY_LUA` via
  `run_lua`, returned byte-identical scores:
  `[b'm3', b'-3.9751751056492', b'm4', b'-3.9751751056492']`. The script reads
  `c[1]`/`c[2]`/`c[3]` from each inner table (`cyclic_decay_field.py:152-159`)
  and never inspects `#c`, so `cmsgpack.unpack` producing a 4-entry inner table
  is inert. **Environment:** popoto @ `c046e1bd`, editable install in
  `/Users/valorengels/src/popoto/.venv`, `REDIS_URL=redis://localhost:6379/12`
  set before `import popoto`.
- **Confidence**: high
- **Impact on plan**: This is what makes the whole approach Small. The baseline
  can live *inside the existing cycles entry* rather than in a second companion
  hash, with no Lua change, no new Redis key, no new `on_delete` cleanup, and
  no numkeys change. A mixed-vintage hash (some members 3-element, some
  4-element) is a valid steady state, not a migration window to be closed.

### spike-2: `_adjust_cycle_amplitudes` preserves an unknown 4th element; `import_state` truncates it

- **Assumption**: "The learning path and the transfer path both round-trip a
  4-element tuple without special-casing."
- **Method**: code-read (`src/popoto/models/base.py:2695-2762`,
  `src/popoto/fields/cyclic_decay_field.py:263-370`)
- **Finding**: **Split.**
  - `_adjust_cycle_amplitudes` **preserves** it: it unpacks, mutates `cycle[1]`
    in place, and repacks the same list objects
    (`base.py:2740-2751`). Extra slots survive untouched. No change needed.
  - `export_state` **preserves** it: `[list(cycle) for cycle in cycles]`
    (`cyclic_decay_field.py:293`) copies whatever arity is stored.
  - `import_state` **truncates** it: it rebuilds each entry as
    `[period, amplitude, phase]` (`cyclic_decay_field.py:344-349`), discarding a
    4th slot. An export/import round-trip would therefore silently strip the
    baseline, and the imported record's next save would re-adopt the current
    declaration as its baseline.
- **Confidence**: high
- **Impact on plan**: `import_state` must be widened to carry the optional 4th
  slot. This is the one non-obvious edit in the change and is called out as its
  own task and its own Verification row rather than left to a builder to notice.

## Data Flow

The change touches one write path and leaves the read path alone.

1. **Entry point**: `instance.save()` on a model with a `CyclicDecayField`.
2. **`Model.save()`** → field-lifecycle dispatch → `CyclicDecayField.on_save`
   (`cyclic_decay_field.py:513`).
3. **`on_save`, read half** — `hget(cycles_hash_key, member_key)`, msgpack
   decode, build `learned: dict[period, list[amplitude]]`
   (`:560-586`). **Change:** build a parallel `baselines: dict[period,
   list[baseline | None]]` from slot 3 of each stored entry, `None` when the
   entry has fewer than 4 slots.
4. **`on_save`, merge half** — for each declared cycle, pop the FIFO-matched
   stored amplitude (`:588-599`). **Change:** also pop the FIFO-matched
   baseline. Decide:
   - baseline is `None` (legacy entry) → keep the learned amplitude
     (today's behavior), and record the declared amplitude as the baseline.
   - baseline `== declared` → declaration unchanged → keep the learned
     amplitude, re-record the same baseline.
   - baseline `!= declared` → **declaration edited** → discard the learned
     amplitude, adopt the declared one, record it as the new baseline, and emit
     one `logger.info`.
5. **`on_save`, write half** — `hset` the normalized list, now
   `[period, amplitude, phase, baseline]` (`:606-610`). The `hdel`
   (empty-cycles) branch is untouched.
6. **Learning** — `strengthen_cycle` / `weaken_cycle` mutate slot 1 only; slot 3
   is carried through unchanged (spike-2). The baseline therefore keeps
   recording *the declaration*, never the learned value.
7. **Read/output** — `CYCLIC_DECAY_LUA` ranks on slots 1-3 and ignores slot 3's
   neighbor (spike-1). Scores are unchanged for every record whose declaration
   has not moved.
8. **Transfer** — `export_state` carries all four slots; `import_state` must be
   widened to write them back (spike-2).

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| PR #201 (#196) | `on_save` wrote declared amplitudes unconditionally | Correct when written — there was no learning yet. Went stale within 24 hours when #206 landed the learning methods, and stayed stale for six months because no test encoded either intent. |
| PR #687 (#679) | Inverted to "learned amplitude always wins; period/phase stay declarative" | Not a failed fix — it is the right fix for the case it addressed, and it is what makes this gap *expressible*. It is incomplete only in that a two-input merge (declared, learned) cannot express a three-state question. |

**Root cause pattern:** both attempts computed the merge from two values when
the decision needs three. With only *declared* and *learned* in hand, "they
differ" is ambiguous, and each fix resolved the ambiguity by picking a constant
winner — first declared, then learned. The fix is not a third choice of winner
but the missing third input: the declaration *as of the last save*, which turns
"they differ" into two distinguishable facts (*the developer moved it* vs *the
learner moved it*).

## Architectural Impact

- **New dependencies**: none. No new import, key, script, or Redis command.
- **Interface changes**: none to any public Python API.
  `CyclicDecayField.__init__`, `strengthen_cycle`, `weaken_cycle`,
  `get_cycles_hash_key` and friends keep their signatures. The **stored payload
  shape** changes, from a 3-element to an optional-4-element inner tuple, which
  is a documented format extension rather than an API change.
- **Coupling**: unchanged. The baseline lives in the entry it describes, so
  nothing new needs to be created, deleted, partitioned or key-derived. The
  rejected alternative — a `:cycle_baselines` companion hash — would have added
  a third key to `on_save`, `on_delete`, `export_state`, `import_state` and the
  subclass-companion-key tests.
- **Data ownership**: unchanged. `CyclicDecayField` already owns the cycles
  hash end to end.
- **Reversibility**: high, in both directions. Reverting the code leaves
  4-element entries in Redis that the reverted `on_save` reads (it takes
  `entry[0]`/`entry[1]` from anything with `len >= 2`) and rewrites as
  3-element on the next save. Forward-adoption is equally soft: a legacy
  3-element entry is a valid input that acquires a baseline on its next save.
  **No migration script is needed or wanted.**

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 — one decision is a genuine product call (Open Question 1:
  reset vs. proportional rescale on a detected edit).
- Review rounds: 1

This is a ~40-line change to one method plus a widened `import_state`, with the
read path untouched and no migration. The cost is in the test matrix and in
getting the semantics named correctly in the docs, not in the code.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 12 ping` | The suite and the spike both need a live server |
| Lane-scoped test DB | `test "$POPOTO_TEST_DB" = "12"` | DB 15 is shared across worktrees (`docs/sdlc/do-sdlc.md`); DB 0 is the live agent store |
| Editable install resolves to this checkout | `python -c "import popoto, pathlib, sys; sys.exit(0 if 'popoto' in str(pathlib.Path(popoto.__file__)) else 1)"` | Worktree gotcha 1 — a stale editable install silently tests another tree |
| Optional extras installed | `python -c "import numpy, sentence_transformers"` | `.[dev]` alone deselects ~95 tests (worktree gotcha 2) |

## Solution

_(placeholder)_

## Failure Path Test Strategy

_(placeholder)_

## Test Impact

_(placeholder)_

## Rabbit Holes

_(placeholder)_

## Risks

_(placeholder)_

## Race Conditions

_(placeholder)_

## No-Gos (Out of Scope)

_(placeholder)_

## Update System

_(placeholder)_

## Agent Integration

_(placeholder)_

## Documentation

_(placeholder)_

## Success Criteria

_(placeholder)_

## Team Orchestration

_(placeholder)_

## Step by Step Tasks

_(placeholder)_

## Verification

_(placeholder)_

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

_(placeholder)_
