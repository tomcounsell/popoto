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
    slot 3. An export/import round-trip would therefore silently strip the
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

### Key Elements

> **Terminology.** A stored cycle entry is a msgpack array. Throughout this plan
> "**slot 3**" means the fourth element, i.e. index 3 in the zero-based array
> `[period, amplitude, phase, declared_baseline]` — `period` is slot 0,
> `amplitude` slot 1, `phase` slot 2. "4-element entry" and "an entry with a
> baseline" mean the same thing.

- **A declared baseline, stored in-place**: each stored cycle entry grows an
  optional slot 3 holding *the declared amplitude that was in force the last
  time `on_save` wrote this entry*. It is written by `on_save` only, never by
  the learning methods, so it always records a declaration and never a learned
  value.
- **A three-way merge in `on_save`**: baseline absent → preserve learned;
  baseline equals declared → preserve learned; baseline differs from declared →
  **declaration wins**, learned amplitude is reset to the declared value.
- **A loud reset**: the third branch emits one `logger.info` naming the model,
  field, period, old declared value, new declared value and the discarded
  learned amplitude. #679 exists because state was destroyed silently; this
  change destroys learned state by design and must say so.
- **Legacy-tolerant reads**: an entry with fewer than 4 slots is a first-class
  input meaning "baseline unknown", handled by falling through to today's
  behavior. No migration, no backfill, no version byte.
- **Transfer consistency**: `import_state` carries the optional slot 3 so an
  export/import round-trip does not silently strip it.

### Flow

Developer edits `amplitude=` in the model → deploys → next `save()` on any
record → `on_save` sees `baseline != declared` → **learned amplitude reset to
the new declared value, one INFO log line** → the record's ranking reflects the
edit, and future `strengthen_cycle` / `weaken_cycle` calls learn away from the
new declaration.

Contrast, unchanged: `strengthen_cycle()` → `save()` → `on_save` sees
`baseline == declared` → **learned amplitude preserved** (#679's behavior, and
the whole test class that pins it, stays green).

### Technical Approach

- **Storage format**: extend the stored cycle tuple from
  `[period, amplitude, phase]` to `[period, amplitude, phase, declared_baseline]`.
  Chosen over a separate `:cycle_baselines` companion hash because spike-1
  proved the read path ignores the extra slot, so the in-place option costs zero
  Lua changes, zero new keys, and zero new lifecycle plumbing — while a second
  hash would need creating, deleting (`on_delete`), partition-key derivation,
  export/import handling, and its own FIFO-alignment story with the entry it
  describes. Keeping the baseline physically adjacent to the amplitude it
  describes also makes FIFO pairing under duplicate periods automatic instead of
  a second thing to keep in step.
- **Merge is FIFO-by-period, exactly as today**: the existing `learned` dict
  becomes a dict of `(amplitude, baseline)` pairs (or a parallel `baselines`
  dict popped in lockstep). Whichever shape is chosen, the pop for amplitude and
  the pop for baseline must be a single decision — never two independent
  `.pop(0)` calls that could drift under a malformed payload.
- **Comparison is exact float equality against the *declared* value**, not a
  tolerance. Both sides are the same Python float taken from the same
  `field.cycles` literal on a round-trip through msgpack, which preserves IEEE
  doubles exactly. A tolerance would silently swallow small deliberate edits.
- **The corrupt-payload fallback is unchanged in spirit and must stay loud**:
  the existing `try/except` around decode+normalize (`:570-586`) discards the
  partial merge and logs. The baselines dict must be cleared on that same path,
  for the same reason — a half-read payload must not contribute a baseline to
  some cycles and not others.
- **`field.cycles` mutation in tests**: the #679 suite changes the declaration
  by assigning `field.cycles` on the field instance and restoring it in a
  `finally`. New tests follow that established pattern rather than defining new
  model classes per scenario.
- **Integration point with `_adjust_cycle_amplitudes`**: none required. Spike-2
  confirmed it mutates slot 1 in place and repacks, so slot 3 survives. Do not
  "helpfully" teach it about the baseline — if it ever writes slot 3, the
  baseline stops meaning "the declaration" and the whole mechanism collapses
  back into the two-value ambiguity this plan exists to remove.
- **Pipeline caveat unchanged**: `on_save` reads directly from Redis while
  `_adjust_cycle_amplitudes` may write through a pipeline. The existing docstring
  note ("Queue `save()` first when sharing one pipeline") still applies verbatim
  and is not in scope to fix.

## Failure Path Test Strategy

### Exception Handling Coverage
- [x] The one exception handler in scope is the decode/normalize `try/except
  Exception` at `cyclic_decay_field.py:570-586`. It is **not** silent — it
  `logger.warning`s and falls back to declared amplitudes, and
  `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_corrupt_stored_entry_falls_back_to_declared`
  already asserts the log via `caplog`. This change widens the guarded region to
  cover baseline extraction; the test must be extended to also assert the
  baseline dict was cleared (i.e. that the post-fallback write records the
  declared amplitude as the new baseline), so the widened handler has observable
  cover rather than inheriting the old assertion.
- [x] The new reset branch is itself a logging path, and its `logger.info` is
  asserted with `caplog` — an unlogged reset is a test failure, not a style nit.

### Empty/Invalid Input Handling
- [x] **Entry shorter than 4 slots** (legacy) — documented and tested:
  baseline is `None`, behavior degrades to #679's.
- [x] **Entry with a non-numeric slot 3** (hand-edited or foreign writer) —
  must be treated as "baseline unknown" (same as absent), not raise out of
  `save()`. Tested by writing a raw payload with a string in slot 3.
- [x] **`cycles=[]` on the field** — the `hdel` branch (`:608-610`) is
  untouched; the existing TC6 test covers it and must stay green.
- [x] **Amplitude learned to `0.0` by `weaken_cycle`** — the truth test in the
  merge is on the bucket list, not on the amplitude value (`:596`), so a learned
  `0.0` is still preserved when the declaration has not changed. This is
  consequence 1 of the #687 accept and is explicitly *not* being reverted; it
  needs a test asserting it survives the new code path.
- [x] Declared amplitude of `0.0` — a legal declaration (`__init__` rejects only
  `< 0`). `baseline == 0.0` must compare equal and preserve learning, not be
  mistaken for a falsy "no baseline". **This is the sharpest trap in the change**
  and gets its own test.

### Error State Rendering
- [x] No user-visible rendering surface — popoto is a library. The
  "user-visible" channel here is the logger, covered above.

## Test Impact

- [ ] `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_cycle_added_to_declaration_uses_declared_amplitude`
  — **UPDATE**. It strengthens DAILY to `6.0`, then swaps the declaration to
  `[(DAILY, 2.0, 0), (WEEKLY, 9.0, 0)]` and asserts DAILY stays `6.0`. Under the
  new rule DAILY's declared value is *unchanged* (`2.0` → `2.0`), so the
  assertion still holds — but only because the declaration for that period did
  not move. The test must be updated to say so explicitly (its point is the
  *added* WEEKLY cycle), otherwise it reads as pinning the behavior this plan
  changes.
- [ ] `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_phase_refreshes_from_declaration_while_amplitude_persists`
  — **UPDATE**, same reason: it swaps `field.cycles` to
  `[(DAILY, 2.0, 777)]`, keeping amplitude at `2.0`. Still passes; add a comment
  pinning that the amplitude is deliberately held constant so the test isolates
  `phase`.
- [ ] `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_cycle_removed_from_declaration_is_dropped`
  — **UPDATE**, same shape (declared amplitude held at `2.0`).
- [ ] `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_corrupt_stored_entry_falls_back_to_declared`
  — **UPDATE**: extend to assert the baseline is (re)recorded as the declared
  amplitude after the fallback.
- [ ] `tests/test_cyclic_decay_field.py::TestLearnedAmplitudePreservedOnSave::test_duplicate_periods_pair_fifo_and_keep_order`
  — **UPDATE**: extend the assertions to slot 3, so FIFO baseline pairing
  under duplicate periods is pinned, not incidental.
- [ ] `tests/test_cyclic_decay_field.py` helper `_read_cycles` — **UPDATE**
  (or leave and add a sibling): several existing assertions index `c[1]`/`c[2]`
  and are arity-agnostic already; any that assert on the whole list
  (`assert stored == [[...]]`) must be found and widened.
- [ ] `tests/test_transfer_fidelity_fields.py` (cyclic round-trip, ~line 505)
  — **UPDATE**: assert the baseline slot survives export→import, which is the
  regression cover for spike-2's truncation finding.
- [ ] `tests/test_cyclic_subclass_companion_keys.py` — **no change expected**
  (key derivation only, unaffected by payload arity). Listed so the builder
  confirms rather than assumes.
- [ ] `tests/test_validity_field.py::TestCyclicDecayGatingGap` and the
  `DECAY_SCORE_LUA` numkeys guard — **no change expected**; the Lua is not
  edited and numkeys stays 4. Listed because `CLAUDE.md` flags this file as one
  that scans *source text*, so a builder must confirm it is unaffected rather
  than discover it in CI.

New tests (all in `tests/test_cyclic_decay_field.py`, a new class
`TestDeclaredAmplitudeOverridesLearned`):

- Edited declaration resets a learned amplitude (the defect).
- Edited declaration resets a learned `0.0`.
- Unedited declaration preserves a learned amplitude (#679 unregressed).
- Declared `0.0` baseline compares equal and preserves learning.
- Legacy 3-element entry preserves learning **and** acquires a baseline.
- Non-numeric slot 3 is treated as absent, without raising.
- The reset emits an INFO log naming both values.
- Duplicate periods reset independently and in FIFO order.
- `strengthen_cycle` after a reset learns from the *new* declared value.

## Rabbit Holes

- **A generic "declared vs learned" framework for every field.**
  `ConfidenceField`, `DecayingSortedField.base_score_field` and the pressure
  `rate` all have declared-vs-stored surfaces. Solving them together is a
  redesign, not a bug fix. Fix `CyclicDecayField`'s amplitude only.
- **A schema/version byte on the cycles payload.** Tempting for "future"
  format changes; unnecessary here because arity *is* the discriminator and
  spike-1 proved the reader tolerates both. Adding a version byte would require
  a real migration, which the in-place approach specifically avoids.
- **Backfilling baselines into existing records.** A backfill would have to
  invent a baseline, and the only value it could invent is the current
  declaration — which is exactly what the next `save()` records anyway, for free
  and without a script that walks every cycles hash in the database.
- **Finishing the #655 accessor conversion in this file.** `CLAUDE.md` records
  that `fields/cyclic_decay_field.py` is one of two files deliberately held back
  from the #655 sweep, with a dedicated follow-up owning it. This plan edits
  `on_save` and will touch lines near a `POPOTO_REDIS_DB` use; it must **not**
  opportunistically convert the file's remaining call sites, which would collide
  with that follow-up. Use `get_REDIS_DB()` for any *new* call site (the
  existing read at `:562` already does), and leave the rest alone.
- **Fixing the same-pipeline ordering caveat.** Documented at
  `cyclic_decay_field.py:533-536`; genuinely separate.
- **Adding a "reset to declared" public method.** The docs already teach the
  recovery (delete the member's hash field, re-save). A new API is a feature.

## Risks

### Risk 1: The reset destroys learned state that a user wanted
**Impact:** A developer who touches `amplitude=` for an unrelated reason (a
refactor, a constant rename, a formatting change that alters a float literal)
wipes every record's accumulated learning for that period, irreversibly.
**Mitigation:** Only an actual *value* change triggers the reset — `2.0` →
`2.00` is the same float and compares equal. The reset logs at INFO with both
values and the discarded amplitude, so the event is attributable after the fact.
The docs section gains an explicit warning that editing a declared amplitude is
a destructive act on learned state. Open Question 1 asks whether proportional
rescale should replace outright reset; if the answer is yes, this risk mostly
evaporates.

### Risk 2: The first save after upgrade silently swallows a simultaneous edit
**Impact:** A developer who upgrades popoto *and* edits `amplitude=` in the same
deploy gets no reset — the legacy entry has no baseline, so the first save
adopts the new declaration as the baseline and preserves the learned value. The
edit appears to do nothing, exactly the symptom this issue reports.
**Mitigation:** Accepted and documented, not fixed. The alternative — treating
"no baseline" as "declaration changed" — would reset *every* learned amplitude
in the database on the first save after upgrade, which is strictly worse. The
documented remedy is the existing one: delete the member's cycles entry and
re-save. Called out in the docs and in the CHANGELOG entry.

### Risk 3: A stale in-process `field.cycles` makes the baseline oscillate
**Impact:** If two processes run different code versions during a rolling
deploy, each save flips the baseline between the old and new declaration, and
every flip resets the learned amplitude. Learning cannot accumulate until the
deploy settles.
**Mitigation:** Bounded and self-healing — it ends when the rollout does, and
the INFO logs make it visible. Documented as a known interaction. This is
inherent to "declaration wins" under a heterogeneous fleet and is not fixable
without a coordination mechanism popoto does not have.

### Risk 4: Payload growth
**Impact:** Each stored cycle gains one msgpack float (~9 bytes) per cycle per
member.
**Mitigation:** Negligible at the 20k-record scale target; no mitigation
planned. Noted so it is not raised as an unexamined objection at review.

## Race Conditions

### Race 1: Read-modify-write on the cycles entry is not atomic
**Location:** `src/popoto/fields/cyclic_decay_field.py:560-610` (`on_save`
`hget` → merge → `hset`), against `src/popoto/models/base.py:2734-2762`
(`_adjust_cycle_amplitudes` `hget` → multiply → `hset`).
**Trigger:** A concurrent `save()` and `strengthen_cycle()` on the same member.
Either can read before the other writes, and the later `hset` wins wholesale.
**Data prerequisite:** the member's cycles entry must exist for either to have
anything to merge.
**State prerequisite:** none beyond that.
**Mitigation:** **Pre-existing and explicitly not addressed here.** This race
ships today, unchanged by #687 and unchanged by this plan — both operations were
already non-atomic read-modify-writes on the same hash field. Adding a slot does
not widen the window, does not add a second key that could tear independently
(the whole reason the baseline goes *inside* the entry), and does not make a lost
update worse: a lost baseline update means one missed reset, recovered on the
next save. Making this atomic means moving the merge into Lua, which is a
separate, larger change (see No-Gos). The builder must not "fix" it opportunistically.

### Race 2: `on_save` reads directly while an adjustment writes through a pipeline
**Location:** `cyclic_decay_field.py:533-536` (documented), `:562` (direct read)
vs `base.py:2755-2757` (pipeline write).
**Trigger:** queueing `strengthen_cycle(pipeline=p)` before `save()` on the same
pipeline `p`.
**Data prerequisite:** the adjustment's `hset` must still be queued, unexecuted.
**State prerequisite:** a shared `redis.client.Pipeline`.
**Mitigation:** Already documented in the `on_save` docstring ("Queue `save()`
first when sharing one pipeline"), unchanged by this plan. The baseline is read
on the same `hget` as the amplitude, so it cannot desynchronize from it — the
two are always the same vintage.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #699] Making the cycles-hash read-modify-write atomic. Both
  `on_save` and `_adjust_cycle_amplitudes` do a client-side `hget` → merge →
  `hset` on the same hash field, so concurrent calls lose one update. This
  predates #679 and is not widened by this plan (Race 1). Fixing it means moving
  both merges into Lua — a structurally larger change with its own plan. Filed
  as #699.
- [SEPARATE-SLUG #699] Auditing the pressure companion hash for the same
  read-modify-write shape (`on_save` vs `resolve_pressure`). Named as an open
  question inside #699 rather than answered here.

**Answered, not deferred** — the issue listed four open questions for the plan;
three are settled above and one is escalated:

1. *Where the declared baseline lives* → in the cycles entry as an optional 4th
   slot (Solution / spike-1).
2. *What happens on a detected declaration change* → reset that member's learned
   amplitude, loudly. Whether it should instead be a proportional rescale is
   Open Question 1 — a product call, not a deferral.
3. *Whether the same gap exists for `phase`* → **checked, and it does not.**
   `phase` is fully declarative: `on_save` writes it from `field.cycles` on
   every save (`cyclic_decay_field.py:592,599`) and **nothing in the codebase
   ever mutates a stored phase** — `_adjust_cycle_amplitudes` touches only slot
   1 (`base.py:2745-2751`), and it is the sole writer besides `on_save` and
   `import_state`. With no learned phase there is no learned-vs-declared
   ambiguity, so a phase baseline would record a value that can never disagree.
   `period` is the match key and is declarative by the same argument. Verified
   rather than assumed, per the issue's request.
4. *Whether a deployment needs a migration* → **no**, and one is actively
   unwanted. A legacy 3-element entry is a valid input meaning "baseline
   unknown"; it acquires a baseline on its next ordinary save. The cost is
   Risk 2 (an upgrade-and-edit in the same deploy is swallowed once), which is
   strictly cheaper than the alternative of resetting every learned amplitude in
   the database on first save.

## Update System

No update-system changes required. This is a pure library-internal change: no
new dependency, no new config file, no new Redis key, no deployment step. The
stored-payload extension is self-adopting on ordinary saves and needs no
migration or backfill (see No-Gos item 4).

The one deploy-visible consequence is behavioral, not procedural, and belongs in
the CHANGELOG rather than in a deploy runbook: **after this ships, editing a
declared `amplitude=` destroys the learned amplitudes for that period on every
record that saves afterward.** That is the intended fix, and it must be stated
in the release notes as a semantics change so an operator is not surprised by it.

## Agent Integration

No agent integration required. `CyclicDecayField` is a popoto field, reached
through the ordinary model API; there is no MCP surface, tool wrapper, or bridge
entry point involved. The nearest agent-facing consumer is
`ObservationProtocol` (`src/popoto/fields/observation.py:294,343,406`), which
calls `strengthen_cycle` / `weaken_cycle` — those signatures and their behavior
are unchanged, so no wiring moves.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/cyclic-decay-field.md`. The load-bearing edit is
      **line 122**, which currently states the exact behavior this plan removes:
      *"If you edit a declared amplitude for a period that has already learned a
      value, the learned value wins. Popoto does not store the declared baseline
      separately, so it cannot tell 'the developer changed the default' from
      'learning diverged.'"* Replace with the three-way rule, and add:
      - a warning that editing a declared amplitude is **destructive** to
        learned state for that period;
      - the upgrade caveat (Risk 2): the first save after upgrading records a
        baseline, so an edit made in the same deploy is not detected;
      - a note that the stored entry is now
        `[period, amplitude, phase, declared_baseline]`, with slot 3
        optional, updating the storage description around line 160.
- [ ] `docs/features/README.md` index needs no new entry (no new feature page).
- [ ] Check `docs/fields.md` and `docs/field-authoring.md` for any statement of
      the cycles payload shape; update if present.

### External Documentation Site
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `CyclicDecayField.on_save` docstring — currently documents the two-way
      rule (`:514-536`). Rewrite the cycles paragraph for the three-way rule,
      keeping the pipeline caveat verbatim.
- [ ] Module docstring companion-hash description (`:17-19`) — note slot 3.
- [ ] `import_state` docstring — note that it carries the optional baseline.
- [ ] A comment at the merge site explaining why the baseline is compared
      against the **declared** value and never against the learned one, and why
      `_adjust_cycle_amplitudes` must never write slot 3.
- [ ] `CHANGELOG.md` entry recording the semantics change.

## Success Criteria

- [ ] An edited declared amplitude resets the learned amplitude on the next
      `save()`, for every record that had learned one.
- [ ] An unedited declaration still preserves the learned amplitude — the whole
      of `TestLearnedAmplitudePreservedOnSave` stays green, unmodified except
      for the four clarifying updates named in Test Impact.
- [ ] A declared amplitude of `0.0` is compared as a value, not as a falsy
      sentinel.
- [ ] A record written before this change (3-element entry) preserves its
      learned amplitude on the next save and acquires a baseline from it.
- [ ] A malformed slot 3 does not raise out of `save()`.
- [ ] Each reset emits exactly one INFO log naming the model, field, period, old
      declared value, new declared value and discarded learned amplitude.
- [ ] `export_state` → `import_state` round-trips the baseline
      (`tests/test_transfer_fidelity_fields.py`).
- [ ] `CYCLIC_DECAY_LUA` is unmodified and `numkeys` stays 4 — the read path is
      untouched.
- [ ] No new Redis key and no migration script.
- [ ] Tests pass (`/do-test`), on a stated environment (`POPOTO_TEST_DB=12`).
      `tests/test_version.py::test_version_matches_pyproject` failing on a stale
      editable install is expected noise, not a regression.
- [ ] `scripts/mypy_ratchet.py` does not rise above the ceiling.
- [ ] `ruff check src/` and `black --check src/ tests/` clean.
- [ ] Documentation updated (`/do-docs`), including
      `docs/features/cyclic-decay-field.md:122`.

## Team Orchestration

Small appetite, one file of production code plus two test files and one doc
page. Two builder/validator pairs plus a documentarian.

### Team Members

- **Builder (merge rule)**
  - Name: `merge-builder`
  - Role: The `on_save` three-way merge, the baseline write, the reset log, and
    the widened corrupt-payload guard. Owns
    `src/popoto/fields/cyclic_decay_field.py` only.
  - Agent Type: builder
  - Domain: Redis/Popoto data — paste the matching rules from
    `DOMAIN_FRAMING.md` into the assignment.
  - Resume: true

- **Builder (tests + transfer)**
  - Name: `test-builder`
  - Role: The new `TestDeclaredAmplitudeOverridesLearned` class, the six
    UPDATE dispositions in Test Impact, and the `import_state` widening plus its
    transfer-fidelity cover.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (semantics)**
  - Name: `merge-validator`
  - Role: Verifies the three-way rule against every row of the test matrix,
    confirms `CYCLIC_DECAY_LUA` and `numkeys` are untouched, and confirms no
    `POPOTO_REDIS_DB` line was opportunistically converted (the #655 rabbit
    hole).
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: `cyclic-doc`
  - Role: `docs/features/cyclic-decay-field.md` (line 122 above all), the
    docstrings, and the CHANGELOG entry.
  - Agent Type: documentarian
  - Resume: true

- **Validator (final)**
  - Name: `final-validator`
  - Role: Runs the whole Verification table and reports with its environment
    stated.
  - Agent Type: validator
  - Resume: true

### Available Agent Types

Per the standard roster (`builder`, `validator`, `code-reviewer`,
`test-engineer`, `documentarian`, …). No specialist agents needed.

## Step by Step Tasks

### 1. Extend the stored cycle tuple and implement the three-way merge
- **Task ID**: build-merge
- **Depends On**: none
- **Validates**: `tests/test_cyclic_decay_field.py`
- **Informed By**: spike-1 (confirmed: a fourth element (slot 3) is invisible to
  `CYCLIC_DECAY_LUA`; scores byte-identical, so no Lua or numkeys change)
- **Assigned To**: `merge-builder`
- **Agent Type**: builder
- **Parallel**: true
- In `CyclicDecayField.on_save`, extend the stored-entry read
  (`cyclic_decay_field.py:560-586`) to also collect slot 3 into a baseline
  bucket keyed by period, FIFO-aligned with the amplitude bucket. A stored entry
  with `len < 4`, or a non-numeric slot 3, yields `None` — "baseline unknown".
  Pop the amplitude and its baseline as **one decision**, never two independent
  `.pop(0)` calls.
- Widen the existing `try/except` so decode *and* baseline extraction are inside
  it, and clear the baseline bucket on the fallback path exactly as `learned` is
  cleared — a half-read payload must not contribute a baseline to some cycles
  and not others.
- Implement the merge (`:588-599`): baseline `None` → keep learned; baseline
  equals declared → keep learned; baseline differs from declared → **use the
  declared amplitude** and emit one `logger.info` naming model, field, period,
  old baseline, new declared value, and the discarded learned amplitude. In all
  three branches write slot 3 as the **declared** amplitude.
- Compare with exact float equality, not a tolerance. Treat `0.0` as a value:
  the "has a baseline" test must be `is not None`, never truthiness. Same rule
  as the existing amplitude bucket, whose truth test is deliberately on the list
  rather than the value (`:596`).
- Write entries as `[period, amplitude, phase, declared_baseline]`
  (`:606-610`). Leave the `hdel` / empty-cycles branch untouched.
- Do **not** touch `CYCLIC_DECAY_LUA`, `rank_decayed`, or `numkeys`.
- Do **not** convert any existing `POPOTO_REDIS_DB` use in this file — it is
  held back from the #655 sweep by a dedicated follow-up (`CLAUDE.md`). Any
  *new* call site uses `get_REDIS_DB()`.
- Do **not** make `_adjust_cycle_amplitudes` aware of slot 3. It preserves it by
  construction (spike-2); teaching it to write slot 3 destroys the mechanism.
- Update the `on_save` docstring, the module docstring's companion-hash
  description, and add the "why the comparison is against the declared value"
  comment.

### 2. Widen `import_state` and cover the transfer round-trip
- **Task ID**: build-transfer
- **Depends On**: none
- **Validates**: `tests/test_transfer_fidelity_fields.py`
- **Informed By**: spike-2 (confirmed: `export_state` preserves arity;
  `import_state` truncates to 3 slots at `cyclic_decay_field.py:344-349`;
  `_adjust_cycle_amplitudes` preserves slot 3 with no change)
- **Assigned To**: `test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- In `import_state`, carry an optional slot 3 through the normalization
  instead of rebuilding a 3-element list. A 3-element imported entry stays
  3-element (baseline unknown), not padded with a guess.
- Extend the cyclic case in `tests/test_transfer_fidelity_fields.py` (~line 505)
  to assert the baseline survives export → import, and that a 3-element legacy
  export imports without a fabricated baseline.
- Update the `import_state` docstring.

### 3. Build the semantics test matrix
- **Task ID**: build-tests
- **Depends On**: build-merge
- **Validates**: `tests/test_cyclic_decay_field.py`
- **Assigned To**: `test-builder`
- **Agent Type**: test-engineer
- **Parallel**: false
- Add `TestDeclaredAmplitudeOverridesLearned` covering: edited declaration
  resets a learned amplitude; edited declaration resets a learned `0.0`;
  unedited declaration preserves learning; a declared `0.0` baseline compares
  equal and preserves learning; a legacy 3-element entry preserves learning and
  acquires a baseline; a non-numeric slot 3 is treated as absent without
  raising; the reset emits exactly one INFO log naming both values; duplicate
  periods reset independently in FIFO order; `strengthen_cycle` after a reset
  learns from the new declared value.
- Change declarations by assigning `field.cycles` with a `finally` restore, the
  pattern the #679 suite already uses — do not add model classes per scenario.
- Apply the six UPDATE dispositions listed in Test Impact, including extending
  the `caplog` assertion in `test_corrupt_stored_entry_falls_back_to_declared`
  to the baseline, and extending
  `test_duplicate_periods_pair_fifo_and_keep_order` to slot 3.
- Confirm (do not assume) that `tests/test_cyclic_subclass_companion_keys.py`
  and `tests/test_validity_field.py::TestCyclicDecayGatingGap` need no change.

### 4. Validate the semantics
- **Task ID**: validate-merge
- **Depends On**: build-merge, build-transfer, build-tests
- **Assigned To**: `merge-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run the touched test files under `POPOTO_TEST_DB=12` and state the
  environment (popoto version, redis-py version, extras installed) with every
  count — required by `CLAUDE.md`.
- Confirm `CYCLIC_DECAY_LUA` is byte-identical to `origin/main` and `numkeys`
  is still 4.
- Confirm no `POPOTO_REDIS_DB` line was removed from
  `src/popoto/fields/cyclic_decay_field.py`.
- Confirm no new Redis key, no new script under `scripts/`, and no migration.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-merge
- **Assigned To**: `cyclic-doc`
- **Agent Type**: documentarian
- **Parallel**: false
- Rewrite `docs/features/cyclic-decay-field.md:122` for the three-way rule; add
  the destructive-edit warning, the Risk 2 upgrade caveat, and the 4-slot
  storage note near line 160.
- Sweep `docs/fields.md` and `docs/field-authoring.md` for any statement of the
  cycles payload shape.
- Add the CHANGELOG entry naming the semantics change.
- `mkdocs build --strict`.

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-merge, document-feature
- **Assigned To**: `final-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table.
- Run the full suite once, stating the environment; treat
  `tests/test_version.py::test_version_matches_pyproject` on a stale editable
  install as expected noise per `docs/sdlc/do-sdlc.md`.
- Report pass/fail per criterion.

## Verification

The `git diff origin/main` rows need a fetched remote — run `git fetch origin`
first. Every grep row below was smoke-tested against the unmodified tree at
`c046e1bd` at plan time: the four anti-criteria all returned `0` (green on a
no-op diff), `numkeys still 4` returned `1`, the `#679` regression class
returned `1`, and `Stale doc claim removed` returned **`1` — i.e. it FAILS
today**, which is its red-state proof: the sentence it forbids is currently
`docs/features/cyclic-decay-field.md:122`.

| Check | Command | Expected |
|-------|---------|----------|
| Touched suites pass | `POPOTO_TEST_DB=12 pytest tests/test_cyclic_decay_field.py tests/test_transfer_fidelity_fields.py tests/test_cyclic_subclass_companion_keys.py tests/test_validity_field.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=12 pytest -q --deselect tests/test_version.py::test_version_matches_pyproject` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| New semantics test class exists | `grep -c "class TestDeclaredAmplitudeOverridesLearned" tests/test_cyclic_decay_field.py` | output > 0 |
| #679 regression class still present | `grep -c "class TestLearnedAmplitudePreservedOnSave" tests/test_cyclic_decay_field.py` | output > 0 |
| Reset is logged | `grep -c "logger.info" src/popoto/fields/cyclic_decay_field.py` | output > 0 |
| Stale doc claim removed | `grep -c "the learned value wins" docs/features/cyclic-decay-field.md` | match count == 0 |
| Anti-criterion — Lua untouched (No-Go #699) | `git diff origin/main -- src/popoto/fields/cyclic_decay_field.py \| grep -c "^[+-].*redis\.call"` | match count == 0 |
| Anti-criterion — numkeys still 4 | `grep -A7 "CYCLIC_DECAY_LUA," src/popoto/fields/cyclic_decay_field.py \| grep -c "^ *4,"` | output > 0 |
| Anti-criterion — no new companion Redis key | `git diff origin/main -- src/popoto/ \| grep -c "^+.*:cycle_baselines\|^+.*:baselines"` | match count == 0 |
| Anti-criterion — no migration script added | `git diff --name-only origin/main -- scripts/ \| grep -c .` | match count == 0 |
| Anti-criterion — #655 sweep not pre-empted | `git diff origin/main -- src/popoto/fields/cyclic_decay_field.py \| grep -c "^-.*POPOTO_REDIS_DB"` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **On a detected declaration change: hard reset, or proportional rescale?**
   This plan assumes **hard reset** — the learned amplitude is discarded and the
   new declared value takes its place. The alternative is to preserve the
   learned *ratio*: `new_learned = new_declared * (old_learned / old_baseline)`,
   so a record that had learned "3x the default" keeps learning 3x the new
   default. Reset is more predictable and matches what a developer editing a
   constant probably expects; rescale is less destructive and keeps months of
   accumulated learning meaningful. Rescale needs an answer for
   `old_baseline == 0.0` (division by zero → fall back to the declared value)
   and would make Risk 1 largely disappear. **This is a product call and the one
   thing the plan cannot settle on its own.**

2. **Should the reset log at INFO or WARNING?** The plan says INFO: the reset is
   intentional and expected after a deliberate edit, so WARNING would cry wolf
   on every record of a normal deploy. But #679 exists precisely because state
   was destroyed quietly, and a fleet-wide reset triggered by an accidental edit
   is exactly the event an operator would want at WARNING. A middle option — one
   WARNING the first time per process per field, INFO thereafter — is more code
   than a Small appetite wants.

3. **Is the Risk 2 upgrade caveat acceptable as documentation only?** A
   developer who upgrades popoto and edits `amplitude=` in the same deploy sees
   the reported symptom once more, because the first save records a baseline
   rather than detecting a change. The plan accepts this and documents it. The
   only alternative that closes it is treating "no baseline" as "changed", which
   resets every learned amplitude in the database on first save after upgrade —
   materially worse. Confirming the acceptance is worth one sentence from the
   maintainer, since it means the fix does not fully work for the very first
   deploy that contains it.
