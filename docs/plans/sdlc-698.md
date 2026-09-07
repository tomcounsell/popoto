---
status: Ready
type: bug
revision_applied: true
revision_applied_at: 2026-09-07T12:04:00Z
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

**Scope of the fix — read this next to the sentence above.** Detection requires
a recorded baseline, and a baseline is only ever recorded by a save. So a record
written before this change needs **two saves** to honor an edit: the first save
records the baseline, and only an edit made *after* that save is detected. A
developer who upgrades popoto **and** edits `amplitude=` in the same deploy
therefore sees the reported symptom exactly one more time (Risk 2). This is
accepted, not overlooked — the only alternative is to treat "no baseline" as
"declaration changed", which resets every learned amplitude in the database on
first save after upgrade, and is strictly worse. The documented remedies are:
upgrade first and let every record save once before editing the declaration, or
`hdel` the member's cycles entry and re-save.

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
- **Impact on plan (revised after critique C2)**: the finding stands; the
  *conclusion drawn from it has been reversed*. The plan originally treated
  `import_state`'s truncation as a defect to fix by widening. C2 showed that
  carrying the exporter's baseline into a differently-declared target fires a
  spurious, destructive reset on the first post-import save. So the truncation is
  **kept and made deliberate** (documented, docstring'd and tested), and Task 2
  is a docs-plus-test task rather than a code-widening one.
  The second half of this spike — `_adjust_cycle_amplitudes` preserving an
  unknown 4th element — is what makes critique B1 real: it also *returns* what it
  preserved, so the public return needs an explicit truncation (see
  Architectural Impact and Task 1b).

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
8. **Transfer** — `export_state` carries all four slots; `import_state`
   deliberately normalizes back to 3 elements ("baseline unknown"), so an
   imported record re-baselines against the importing deployment on its next save
   (spike-2 found the truncation; critique C2 established that it is correct and
   must be made intentional rather than widened).
9. **Public return of `strengthen_cycle` / `weaken_cycle`** —
   `_adjust_cycle_amplitudes` returns the list it read (`base.py:2763`), which
   would leak slot 3 to callers. It is truncated to 3 elements **at the return
   site only**; the packed value keeps all four (critique B1).

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
- **Interface changes**: **one, and it is deliberately held at zero by an
  explicit truncation** (revised per critique B1). Signatures are unchanged
  (`CyclicDecayField.__init__`, `strengthen_cycle`, `weaken_cycle`,
  `get_cycles_hash_key`). The **return value** of the public
  `strengthen_cycle()` / `weaken_cycle()` is not automatically unchanged,
  though: `_adjust_cycle_amplitudes` unpacks the stored payload
  (`base.py:2743`), mutates only `cycle[1]`, and **returns the list it read**
  (`:2763`), which both public methods return verbatim (`:2673`, `:2693`). Once
  `on_save` writes 4-element entries, those methods would begin returning
  4-element sublists for any already-saved record, silently widening a
  documented public return ("The updated cycles list").

  **Decision: option (b) — strip slot 3 at the return site, keeping the public
  return contract 3-element.** `return cycles` at `base.py:2763` becomes
  `return [cycle[:3] for cycle in cycles]`. Rationale: the baseline is a
  deployment-local storage detail whose only legitimate writer is `on_save`
  (see Rabbit Holes). Letting it out through a public return makes it part of
  the published surface, invites callers to read or round-trip it, and directly
  undercuts the invariant that `_adjust_cycle_amplitudes` never participates in
  slot 3. Option (a) — documenting the widened shape — costs a public-API note
  in two docstrings, the docs page and the CHANGELOG, and buys nothing a caller
  wants. The truncation is one line and one test.

  **Constraint on the implementation:** truncate at the **return sites only**.
  The value packed at `base.py:2756-2762` must keep all four slots; truncating
  before `msgpack.packb` would make `_adjust_cycle_amplitudes` a slot-3 *writer*
  (in fact a slot-3 deleter) and destroy the mechanism. The pipeline branch
  (`:2758-2760`) returns the pipeline and is unaffected; the no-entry branch
  returns `[]` and is unaffected. `tests/test_observation_protocol.py:690`
  asserts only `result == []` on the no-entry path, so no existing test pins the
  arity — a new one must (Task 1b).

  The **stored payload shape** does change, from a 3-element to an
  optional-4-element inner tuple. That is a documented storage-format extension,
  not an API change, and after the truncation above it is not observable through
  any public Python return value.
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
- PM check-ins: 1 — **spent**. The reset-vs-rescale product call was answered by
  the supervisor during the critique-revision pass (see Decisions). No further
  check-in is budgeted or needed.
- Review rounds: 1 (critique round 1 complete; this is revision 1)

This is a ~40-line change to one method, a one-line truncation in
`_adjust_cycle_amplitudes`, and a documented no-op in `import_state`, with the
read path untouched and no migration. The cost is in the test matrix and in
getting the semantics named correctly in the docs, not in the code.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 12 ping` | The suite and the spike both need a live server |
| Lane-scoped test DB | `test "$POPOTO_TEST_DB" = "12"` | DB 15 is shared across worktrees (`docs/sdlc/do-sdlc.md`); DB 0 is the live agent store |
| Editable install resolves to **this** checkout | `python -c "import popoto,pathlib,sys,subprocess; root=pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip()).resolve(); sys.exit(0 if pathlib.Path(popoto.__file__).resolve().is_relative_to(root) else 1)"` run from the worktree root | Worktree gotcha 1 — a stale editable install silently tests another tree. **The previous form of this row (`'popoto' in str(popoto.__file__)`) was vacuous** and passed at critique time while resolving to `/Users/valorengels/src/popoto/src/popoto/` — the **main** checkout, not `.worktrees/sdlc-698`. So this row is **currently RED** and is a real gate, not a formality: before build, either `pip install -e .` from `.worktrees/sdlc-698`, or run the suite from the main checkout and say so in every stage report. Any count reported while this row is red is not usable (`CLAUDE.md`: state the environment alongside any count). |
| Optional extras installed | `python -c "import numpy, sentence_transformers"` | `.[dev]` alone deselects ~95 tests (worktree gotcha 2) |

**Build prerequisite, carried forward verbatim from the critique round-2 live
check (2026-09-07):** the editable install in `.worktrees/sdlc-698` currently
resolves `popoto.__file__` to `/Users/valorengels/src/popoto/src/popoto/__init__.py`
(the MAIN checkout). Either `pip install -e .` in the worktree before any
measurement, or every stage report must name the checkout it tested.

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
- **Hard reset, decided — not an open question** (supervisor decision, 2026-09-07,
  closing critique C1). On a detected declaration change the learned amplitude is
  **discarded outright** and replaced by the new declared value. It is
  predictable and matches what a developer editing a constant intends. No critic
  raised a technical objection to it. *Proportional rescale*
  (`new_learned = new_declared * (old_learned / old_baseline)`) is a **recorded
  rejected alternative**: it preserves accumulated learning but is harder to
  predict, needs an extra `old_baseline == 0.0` division-by-zero rule, and would
  turn the merge branch, its log line, its docstring and most of the new test
  class into a second design. If it is ever wanted it is a follow-up issue, not a
  variation of this one. `merge-builder` builds hard reset; nothing in this plan
  is gated on reopening this.
- **A loud reset**: the third branch emits one `logger.info` naming the model,
  field, **member key**, period, old declared value, new declared value and the
  discarded learned amplitude. The member key is what makes the per-record audit
  trail traceable back to a record — without it, two records that discarded the
  same amplitude for the same period emit indistinguishable lines (C8). It is
  already in scope at `cyclic_decay_field.py:547`
  (`member_key = model_instance.db_key.redis_key`) and the sibling
  corrupt-payload `logger.warning` already logs it, so this costs no extra Redis
  call. #679 exists because state was destroyed silently; this
  change destroys learned state by design and must say so. **INFO, decided**
  (supervisor decision, closing critique C5 / Decision 2): the reset is intentional and
  expected after a deliberate edit, so WARNING would raise a fleet-wide alarm on
  a normal deploy.
  **Volume, accepted explicitly (C5):** one INFO line per reset per record means
  a single declaration edit emits up to one line per learned record — at the 20k
  scale target, a burst of that order — as the records save. This is accepted as
  a **per-record audit trail**: the reset is per-record, so the evidence must be
  too, and the burst is bounded by the number of affected records, one-shot per
  edit (the baseline is rewritten by the same save), and only occurs on a
  deliberate declaration change. No sampling, aggregation or rate limit is added,
  and **no per-process-per-field dedupe** — Decision 2 already prices that as more code
  than a Small appetite wants. The volume gets one documented sentence next to
  the destructive-edit warning in the docs task.
- **Legacy-tolerant reads**: an entry with fewer than 4 slots is a first-class
  input meaning "baseline unknown", handled by falling through to today's
  behavior. No migration, no backfill, no version byte.
- **Transfer consistency — reversed by critique C2**: `import_state`
  **deliberately does not carry slot 3**. An imported entry is normalized to the
  3-element "baseline unknown" shape and acquires a baseline from the importing
  deployment on its next ordinary save. Spike-2 found that `import_state`
  truncates today and the plan originally called that a bug; C2 showed it is the
  correct behavior, and the fix is to make it *intentional and documented*
  instead of incidental. Reasoning: a baseline records **the declaration in force
  in the deployment that wrote the entry**, which is deployment-local by
  definition. Carrying the *exporter's* baseline into a target whose
  `field.cycles` declares a different amplitude makes the first post-import
  `save()` see `baseline != declared` and fire a reset — destroying exactly the
  learned amplitude that `roundtrip_policy = "carry"` exists to preserve, with no
  import-time signal. Dropping it preserves the learned amplitude in every case
  and costs one missed detection window, which is the same, already-accepted
  trade as Risk 2 and is self-healing on the next save (Risk 5).
  `export_state` still copies whatever arity is stored (no code change,
  `[list(cycle) for cycle in cycles]`); the asymmetry is intentional — an export
  is a faithful snapshot of stored bytes, an import re-establishes
  deployment-local meaning.

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
- **Integration point with `_adjust_cycle_amplitudes`**: exactly one, and it is
  on the *return* path only (critique B1). Spike-2 confirmed it mutates slot 1 in
  place and repacks, so slot 3 survives the write untouched — that must stay
  true. Do not "helpfully" teach it about the baseline on the **write** side: if
  it ever writes or strips slot 3 before `msgpack.packb`, the baseline stops
  meaning "the declaration" and the whole mechanism collapses back into the
  two-value ambiguity this plan exists to remove. The **only** sanctioned change
  is `return [cycle[:3] for cycle in cycles]` at `base.py:2763`, which keeps the
  public return of `strengthen_cycle` / `weaken_cycle` at 3 elements. Write side:
  four slots. Return side: three.
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
  The assertion must cover the **member key** as well as the old/new declared
  values and the discarded amplitude (C8); a reset line without the record
  identity is not an audit trail.

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
  — **UPDATE** (revised per C2): assert that a 4-element stored entry exports
  faithfully and **imports back as a 3-element "baseline unknown" entry**, and
  that the learned amplitude survives the round-trip and the first post-import
  save unchanged. This is the regression cover for the deliberate truncation —
  it pins the drop as intentional, so a later reader cannot "fix" it back into
  the spurious-reset behavior C2 identified.
- [ ] `tests/test_observation_protocol.py` (~line 690) — **ADD a sibling**:
  nothing currently pins the arity of the `strengthen_cycle` / `weaken_cycle`
  return (that assertion is `result == []` on the no-entry path). A new test must
  assert that both public methods return **3-element** sublists for a record
  whose stored entry has a baseline, so the B1 truncation cannot silently
  regress. Placement in `tests/test_cyclic_decay_field.py` is acceptable if it
  keeps the fixtures simpler; what matters is that the assertion exists.
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
- `strengthen_cycle` / `weaken_cycle` return 3-element sublists even when the
  stored entry carries a baseline (critique B1), while the stored entry keeps
  all four slots after the call.

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
a destructive act on learned state. Proportional rescale would have softened this
risk; it was **considered and rejected** (see Solution / Key Elements, supervisor
decision closing C1), so this risk is **accepted at full weight** and carried by
documentation plus the INFO audit trail, not reduced.

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

### Risk 5: An imported record does not detect an edit until it has saved once
**Impact:** Because `import_state` normalizes to the "baseline unknown" shape
(Solution / Transfer consistency, critique C2), a record restored from an export
behaves like a pre-upgrade record: its first save records a baseline rather than
detecting a change, so a declaration edit made between the export and the first
post-import save is swallowed once. Same shape as Risk 2, on the transfer path.
**Mitigation:** Accepted and documented. The alternative — carrying the
*exporter's* baseline — trades one swallowed edit for a **spurious destructive
reset** on every cross-deployment import into a differently-declared target,
which silently destroys the learned state `roundtrip_policy = "carry"` exists to
preserve. A missed detection is recoverable (edit again, or save once first); a
destroyed learned amplitude is not. Documented on the transfer page and in the
`import_state` docstring, with the same remedy as Risk 2.

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
not widen the window and does not add a second key that could tear independently
(the whole reason the baseline goes *inside* the entry). Making this atomic means
moving the merge into Lua, which is a separate, larger change (see No-Gos). The
builder must not "fix" it opportunistically.

**Corrected consequence (critique C3).** An earlier draft of this row said a lost
baseline update means "one missed reset, recovered on the next save." That
understates it, and the accurate version is recorded here so a maintainer
investigating an unexplained reset knows where to look. The new failure mode is a
**spurious, delayed reset that discards real learning**:

1. `_adjust_cycle_amplitudes` `hget`s `[period, learned_old, phase,
   baseline_old]` (`base.py:2736`).
2. `on_save` reads, sees `baseline_old != declared`, resets, and writes
   `[period, declared, phase, declared]`.
3. `_adjust_cycle_amplitudes` writes `[period, learned_old * factor, phase,
   baseline_old]` (`:2762`) — it preserves unknown slots (spike-2), so it
   repacks the **stale** baseline. The reset is clobbered *and* the superseded
   baseline is restored.
4. The next, **uncontended** save sees `baseline_old != declared` again and fires
   a second reset — this time discarding the learning applied in between.

This stays inside Race 1's accepted scope and the #699 No-Go: it is a symptom of
the pre-existing lost-update race, its blast radius is one member's learned
amplitude for one period, and it is bounded (the second reset writes the current
declaration as the baseline, so it does not repeat). Fixing it means the same Lua
move #699 owns. No code change here; this is documentation of a known
interaction.

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

- [FOLLOW-UP, not filed] Proportional rescale of learned amplitudes on a detected
  declaration change. Rejected for this plan by supervisor decision (see Solution
  / Key Elements). If it is ever wanted it is a new issue with its own semantics
  (including an `old_baseline == 0.0` rule), not a variation inside this one.

**Answered, not deferred** — the issue listed four open questions for the plan;
**all four are now settled**, the last by supervisor decision during this
revision:

1. *Where the declared baseline lives* → in the cycles entry as an optional 4th
   slot (Solution / spike-1).
2. *What happens on a detected declaration change* → **hard reset**: discard that
   member's learned amplitude, adopt the declared value, log one INFO line.
   Decided by the supervisor on 2026-09-07; proportional rescale is a recorded
   rejected alternative. No longer an open question.
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
        optional, updating the storage description around line 160;
      - **the log volume (C5)**: one INFO line per reset per record, so a single
        edit produces a burst proportional to the number of records that had
        learned that period — expected, one-shot per edit, and deliberately not
        sampled or deduplicated;
      - **the concrete operator remedies (C4)**: upgrade first and let every
        record save once before editing the declaration, or `hdel` the member's
        cycles entry and re-save;
      - **the transfer behavior (C2 / Risk 5)**: an imported record carries no
        baseline and re-baselines on its next save, so an edit made between
        export and the first post-import save is not detected. State that the
        public return of `strengthen_cycle` / `weaken_cycle` stays
        `[period, amplitude, phase]` (B1) — the baseline is internal.
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
- [ ] `import_state` docstring — record that it **deliberately drops** the
      optional baseline and why (C2 / Risk 5).
- [ ] `strengthen_cycle` / `weaken_cycle` docstrings (`base.py:2665-2671`,
      `:2685-2691`) — state the returned shape is `[period, amplitude, phase]`
      and that an internal slot is not exposed (B1).
- [ ] A comment at the merge site explaining why the baseline is compared
      against the **declared** value and never against the learned one, and why
      `_adjust_cycle_amplitudes` must never write slot 3.
- [ ] `CHANGELOG.md` entry recording the semantics change.

## Success Criteria

- [ ] An edited declared amplitude resets the learned amplitude on the next
      `save()`, **for every record that has a recorded baseline** — i.e. every
      record that has saved at least once since this change shipped. A record
      still carrying a pre-change 3-element entry acquires its baseline on that
      first save and detects edits from then on (Risk 2; two saves are required
      across the upgrade boundary, by design).
- [ ] An unedited declaration still preserves the learned amplitude — the whole
      of `TestLearnedAmplitudePreservedOnSave` stays green, unmodified except
      for the **five** clarifying updates named in Test Impact
      (`test_cycle_added_to_declaration_uses_declared_amplitude`,
      `test_phase_refreshes_from_declaration_while_amplitude_persists`,
      `test_cycle_removed_from_declaration_is_dropped`,
      `test_corrupt_stored_entry_falls_back_to_declared`,
      `test_duplicate_periods_pair_fifo_and_keep_order`), plus the module-level
      `_read_cycles` helper if any whole-list assertion needs widening.
- [ ] A declared amplitude of `0.0` is compared as a value, not as a falsy
      sentinel.
- [ ] A record written before this change (3-element entry) preserves its
      learned amplitude on the next save and acquires a baseline from it.
- [ ] A malformed slot 3 does not raise out of `save()`.
- [ ] Each reset emits exactly one INFO log naming the model, field, member key,
      period, old declared value, new declared value and discarded learned
      amplitude. The `caplog` assertion must require the member key of the record
      being reset to appear in the message, not only the amplitudes (C8).
- [ ] `export_state` → `import_state` round-trips the **learned amplitude**, and
      the imported entry deliberately carries **no** baseline
      (`tests/test_transfer_fidelity_fields.py`) — C2 / Risk 5.
- [ ] `strengthen_cycle()` / `weaken_cycle()` still return 3-element sublists,
      while the stored entry keeps four slots (B1).
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

Small appetite: two files of production code
(`src/popoto/fields/cyclic_decay_field.py`, plus a one-line return-site change
and docstrings in `src/popoto/models/base.py`), three test files, and one doc
page. Two builder/validator pairs plus a documentarian.

### Team Members

- **Builder (merge rule)**
  - Name: `merge-builder`
  - Role: The `on_save` three-way merge, the baseline write, the reset log, and
    the widened corrupt-payload guard in
    `src/popoto/fields/cyclic_decay_field.py`; plus the one-line return-site
    truncation and docstring updates in `src/popoto/models/base.py` (Task 1b).
  - Agent Type: builder
  - Domain: Redis/Popoto data — paste the matching rules from
    `DOMAIN_FRAMING.md` into the assignment.
  - Resume: true

- **Builder (tests + transfer)**
  - Name: `test-builder`
  - Role: The new `TestDeclaredAmplitudeOverridesLearned` class, the six
    UPDATE dispositions in Test Impact, and the `import_state`
    docstring/comment recording the deliberate 3-element truncation, plus its
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
  declared amplitude** and emit one `logger.info` naming model, field,
  `member_key`, period, old baseline, new declared value, and the discarded
  learned amplitude (`member_key` is already bound at `:547`; C8). In all
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

### 1b. Keep the public return of `strengthen_cycle` / `weaken_cycle` 3-element
- **Task ID**: build-return-arity
- **Depends On**: none (independent of `build-merge`; different file)
- **Validates**: `tests/test_cyclic_decay_field.py`,
  `tests/test_observation_protocol.py`
- **Informed By**: critique B1; spike-2 (`_adjust_cycle_amplitudes` preserves —
  and therefore also *returns* — unknown slots)
- **Assigned To**: `merge-builder`
- **Agent Type**: builder
- **Parallel**: true
- In `src/popoto/models/base.py`, change `_adjust_cycle_amplitudes`'s non-pipeline
  return (`:2763`) from `return cycles` to `return [cycle[:3] for cycle in cycles]`.
- **Truncate at the return site only.** The value packed at `:2756-2762` must
  keep all four slots. Truncating before `msgpack.packb` turns
  `_adjust_cycle_amplitudes` into a slot-3 writer/deleter and destroys the
  mechanism (see Rabbit Holes).
- Leave the pipeline branch (`:2758-2760`, returns the pipeline) and the
  no-entry branch (returns `[]`) untouched.
- Add one sentence to the `strengthen_cycle` and `weaken_cycle` docstrings
  (`:2665-2671`, `:2685-2691`) stating that the returned list is
  `[period, amplitude, phase]` and that the stored entry may carry an additional
  internal slot which is deliberately not exposed.
- Add the arity test named in Test Impact, and name it **exactly**
  `test_strengthen_cycle_return_omits_baseline_slot` (a Verification row greps
  for that identifier): both public methods return 3-element sublists for a
  record whose stored entry has a baseline, and the stored entry still has four
  slots after the call.
- Do **not** teach `_adjust_cycle_amplitudes` anything else about slot 3.

### 2. Make `import_state`'s baseline drop deliberate and cover the round-trip
- **Task ID**: build-transfer
- **Depends On**: none
- **Validates**: `tests/test_transfer_fidelity_fields.py`
- **Informed By**: spike-2 (`export_state` preserves arity; `import_state`
  truncates to 3 slots at `cyclic_decay_field.py:344-349`;
  `_adjust_cycle_amplitudes` preserves slot 3 with no change) **as revised by
  critique C2** — the truncation is correct and is being made intentional, not
  widened
- **Assigned To**: `test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- **Do not widen `import_state`.** Keep the existing rebuild to
  `[period, amplitude, phase]` (`cyclic_decay_field.py:344-349`) and add a
  comment plus a docstring paragraph recording *why*: a baseline is the
  declaration in force in the deployment that wrote it, so carrying the
  exporter's baseline into a differently-declared target would fire a spurious
  destructive reset on the first post-import save (Risk 5). An imported entry is
  "baseline unknown" and re-baselines on its next ordinary save.
- Never fabricate a baseline at import time (no padding from `field.cycles`).
- Extend the cyclic case in `tests/test_transfer_fidelity_fields.py` (~line 505)
  to assert: a 4-element stored entry exports faithfully; it imports back as a
  3-element entry; the learned amplitude is unchanged by the round-trip; and the
  first post-import save preserves that learned amplitude and records a baseline
  from the *importing* deployment's declaration.
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
  raising; the reset emits exactly one INFO log naming both values **and the
  member key of the record being reset** (assert the member key via `caplog`,
  not only the amplitudes — C8); duplicate
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
- **Depends On**: build-merge, build-return-arity, build-transfer, build-tests
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
- Confirm the B1 truncation is at the **return site only**: `msgpack.packb` in
  `_adjust_cycle_amplitudes` still receives 4-slot entries, and a record's stored
  entry still has four slots after `strengthen_cycle()`.
- Confirm `import_state` still normalizes to 3 elements (C2) and fabricates no
  baseline.
- **State whether the editable install resolves to this worktree** (Prerequisites
  row 3, currently RED). If it resolves to the main checkout, say so explicitly
  with every count.

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

The rows added in this revision were smoke-tested the same way at revision time
on the unmodified tree at `c046e1bd`: `cycle[:3]` in `base.py` returned **`0`**
(red, as it must be before the B1 change), the named-test grep returned **`0`**
(red), the `packb` anti-criterion returned `0` (green on a no-op diff), and the
C2 row `normalized.append([period, amplitude, phase])` returned **`1`** — green
today by construction, because that row pins *existing* behavior the plan is
deliberately preserving rather than changing. The C6 prerequisite row was run
live from `.worktrees/sdlc-698` and **exited 1**, resolving to
`/Users/valorengels/src/popoto/src/popoto/__init__.py`; it is genuinely red and
must be resolved or explicitly disclosed before any count from this lane is
usable.

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
| B1 — public return truncated to 3 slots | `grep -c "cycle\[:3\]" src/popoto/models/base.py` | output > 0 |
| B1 anti-criterion — truncation NOT applied before `packb` | `git diff origin/main -- src/popoto/models/base.py \| grep -c "^+.*packb(\[cycle\[:3\]"` | match count == 0 |
| B1 — return arity is pinned by a named test | `grep -rc "def test_strengthen_cycle_return_omits_baseline_slot" tests/ \| grep -vc ":0$"` | output > 0 |
| C2 — `import_state` still normalizes to 3 elements | `grep -c "normalized.append(\[period, amplitude, phase\])" src/popoto/fields/cyclic_decay_field.py` | output > 0 |
| C6 — editable install resolves to the checkout under test | the Prerequisites row-3 command, run from the checkout the suite was run in | exit code 0, **or** the stage report explicitly names the checkout that was tested |
| C7 — `test-builder` role names the truncation, not a widening | `grep -c "docstring/comment recording the deliberate 3-element truncation" docs/plans/sdlc-698.md` | output > 0 (a bare "no widening" grep over this file can never be 0 — the Critique Results section quotes the stale phrase verbatim, so the check is stated positively) |
| C8 — reset log carries the member key | `grep -A6 "logger.info" src/popoto/fields/cyclic_decay_field.py \| grep -c "member_key"` | output > 0 (currently 0 — the file has no `logger.info` at all pre-change, so this row is red by construction until the reset branch lands; the `-A6` window spans a multi-line f-string) |

## Critique Results

**Round:** 2 of 2 (cycle cap reached)
**Critics:** Risk & Robustness, Scope & Value, History & Consistency (FULL depth)
**Mode:** independent roster (3 critics)
**Findings:** 2 total (0 blockers, 2 concerns, 0 nits)
**Verdict:** READY TO BUILD (with concerns)

All eight round-1 findings (B1, C1–C6, N1) were re-verified against the revised
plan text *and* the current source, by all three critics independently. Every one
is genuinely resolved in the plan body, not merely asserted in the Revision log.
The settled supervisor decisions (hard reset, INFO level, Risk 2 as documentation,
Race 1 unfixed under No-Go #699) were not re-litigated by any critic.

| Severity | Critics | Finding | Location | Suggested fix |
|---|---|---|---|---|
| CONCERN | Scope & Value; History & Consistency (independent convergence) | C7 — The `test-builder` role still reads "the `import_state` **widening** plus its transfer-fidelity cover" — pre-revision language that C2 reversed. Team Orchestration is the section a builder scans first, and as written it points `test-builder` at exactly the change C2 rejected. | Team Orchestration / Team Members (`test-builder`) | Drop "widening": "…the `import_state` docstring/comment recording the deliberate 3-element truncation, plus its transfer-fidelity cover." Plan-text edit only; Task 2's bullets are already correct. |
| CONCERN | Risk & Robustness | C8 — The reset `logger.info` is specified in three places (Solution / Key Elements, Task 1, Success Criteria) as naming model, field, period, old and new declared value and the discarded amplitude — but never the **member key**. The plan declines sampling and dedupe on the grounds that this is a *per-record audit trail*; without the record identity, two records that discarded the same amplitude emit indistinguishable lines and the trail cannot be traced back to a record. | Solution / Key Elements ("A loud reset"); Task 1; Success Criteria | Add `member_key` to the log call and to all three places that enumerate its contents; extend the planned `caplog` assertion to require it. |

### Concerns — detail

#### C7 — Stale "import_state widening" in the builder role summary

- **Critics:** Scope & Value; History & Consistency (flagged independently)
- **Location:** Team Orchestration / Team Members, `test-builder` role line
- **Finding:** The role reads "the `import_state` widening plus its
  transfer-fidelity cover". C2 reversed that approach: Task 2 says
  "**Do not widen `import_state`.**", Solution / Key Elements calls the
  truncation deliberate, and spike-2's Impact records the conclusion as
  *reversed*. The role summary is the only surviving sentence written against
  the pre-revision approach.
- **Suggestion:** Reword the role line to match Task 2.
- **Implementation Note:** Plan-text edit to the `test-builder` role bullet only —
  no task-list or code change, because Task 2's bullets already state the correct
  behavior. After the edit, `grep -c "import_state\` widening"
  docs/plans/sdlc-698.md` must return 0. **Severity held at CONCERN despite
  two-critic convergence**: the authoritative task text contradicts the stale
  summary explicitly, and the Verification row
  `grep -c "normalized.append(\[period, amplitude, phase\])"` mechanically fails
  if a builder widens `import_state` anyway, so the misdirection cannot reach a
  merge undetected.

#### C8 — The reset audit line omits the record it describes

- **Critics:** Risk & Robustness
- **Location:** Solution / Key Elements ("A loud reset"); Task 1 (`build-merge`);
  Success Criteria bullet 6
- **Finding:** The plan accepts an uncapped INFO burst specifically because each
  line is a per-record audit record, but the enumerated contents identify only the
  *class-level* facts (model, field, period, old/new declared value) plus the
  discarded amplitude. Two records that had learned the same amplitude for the
  same period produce byte-identical lines, so the burst cannot be resolved back
  to the affected records — which is the entire justification for not
  deduplicating it.
- **Suggestion:** Include the member key in the log line and in the three places
  that enumerate its contents.
- **Implementation Note:** `member_key` is already in scope in `on_save` at
  `src/popoto/fields/cyclic_decay_field.py:547`
  (`member_key = model_instance.db_key.redis_key`), and the sibling
  corrupt-payload `logger.warning` in the same method already logs it — so this
  is one f-string field, matching an existing precedent, with no new lookup and
  no extra Redis call. Extend the planned `caplog` assertion for the reset log to
  require the member key in the message, otherwise the omission can regress
  silently.

### Structural check results — round 2

| Check | Status | Detail |
|---|---|---|
| Required sections | PASS | All plan sections present and non-empty |
| Task numbering | PASS | Tasks 1, 1b, 2-6; no gaps |
| Dependencies valid | PASS | `build-merge`, `build-return-arity`, `build-transfer`, `build-tests`, `validate-merge`, `document-feature` all resolve; no cycles |
| File paths exist | PASS | 15 of 15 referenced paths exist |
| Prerequisites met | PARTIAL (disclosed) | Redis DB 12 PONG; `numpy`/`sentence_transformers` import; **row 3 genuinely RED** — run from `.worktrees/sdlc-698` it exits 1, resolving to `/Users/valorengels/src/popoto/src/popoto/__init__.py`. The C6 rewrite works: the check now reports the failure instead of hiding it, and the plan already records the disclosure obligation. |
| Cross-references | PASS | N1 fixed — Success Criteria now says five and enumerates them, matching Test Impact |
| Verification rows reproduce | PASS | Re-run on the unmodified tree at round 2: `cycle[:3]`=0 (red, as required pre-change), named-test grep=0 (red), `normalized.append([period, amplitude, phase])`=1 (green by construction, C2), stale-doc-claim=1 (red, the sentence is live at `docs/features/cyclic-decay-field.md:122`), numkeys=1, `logger.info`=0, `TestLearnedAmplitudePreservedOnSave`=1 — every value matches the plan's stated smoke test |
| Cited line numbers | PASS | `base.py:2655/2675/2695/2756/2763` and `cyclic_decay_field.py:264/316/349/547` all verified against the source |

### Round-2 concern fold-in (2026-09-07, plan-text only — no re-scoping)

The critique cycle cap is exhausted and the verdict stands at READY TO BUILD
(with concerns). Both accepted round-2 concerns are folded into the plan text so
BUILD executes them. No settled supervisor decision was reopened: hard reset,
INFO level, Risk 2 as documentation only, and Race 1 deferred to #699 are
unchanged.

| Concern | Disposition |
|---|---|
| C7 — stale "`import_state` widening" in the `test-builder` role | **Folded in**: Team Orchestration's `test-builder` role now reads "the `import_state` docstring/comment recording the deliberate 3-element truncation, plus its transfer-fidelity cover", matching Task 2's "Do not widen `import_state`." Verification row added, stated **positively** — a "no widening" grep over this file can never be 0, because the critique tables above quote the stale phrase verbatim. |
| C8 — reset audit line omits the record it describes | **Folded in**: `member_key` added to all three enumerations of the log contents (Solution / Key Elements "A loud reset", Task 1, Success Criteria) and to the `caplog` requirement in Success Criteria and Task 3. `member_key` is already bound at `cyclic_decay_field.py:547` (`member_key = model_instance.db_key.redis_key`, re-verified against source at fold-in time), so this costs no extra Redis call. Verification row added; it measures 0 on the unmodified tree (the file has no `logger.info` at all pre-change), so it is red-by-construction rather than vacuous. |

Also carried forward into Prerequisites, verbatim from the round-2 live check:
the editable install in `.worktrees/sdlc-698` resolves `popoto.__file__` to the
MAIN checkout, so BUILD must either `pip install -e .` in the worktree before any
measurement or name the checkout it tested in every stage report.

---

## Critique Results — Round 1 (superseded, retained for history)

**Critics:** Risk & Robustness, Scope & Value, History & Consistency (FULL depth)
**Mode:** independent roster (3 critics)
**Findings:** 8 total (1 blocker, 6 concerns, 1 nit)
**Verdict:** NEEDS REVISION — all eight resolved by revision 1; see the Revision log.

| Severity | Critics | Finding | Location | Suggested fix |
|---|---|---|---|---|
| BLOCKER | History & Consistency | B1 — `strengthen_cycle()` / `weaken_cycle()` return the list read from storage (`base.py:2743`, `:2763`), so once `on_save` writes 4-element entries both public methods start returning 4-element sublists; "Interface changes: none to any public Python API" is false as written. | Architectural Impact | Decide and record: document the widened return shape in both docstrings, or strip slot 3 at the return site only (`return [cycle[:3] for cycle in cycles]`) — never before `msgpack.packb`. |
| CONCERN | Scope & Value | C1 — Open Question 1 is declared "the one thing the plan cannot settle on its own", yet Tasks 1 and 3 already implement and pin hard reset with no task gated on the decision. | Open Questions 1 / Step by Step Tasks | Commit to hard reset in the plan text (rescale deferred to a follow-up), or add an explicit decision dependency ahead of `build-merge` / `build-tests`. |
| CONCERN | History & Consistency | C2 — `import_state` carrying the exporter's slot 3 into a deployment with a different declared amplitude makes the first post-import `save()` fire a "declaration edited" reset, destroying the learned state `roundtrip_policy = "carry"` exists to preserve. | Solution / Key Elements ("Transfer consistency"), Risk 2, Task 2 | Add a named Risk and pick one: accept it explicitly, or re-baseline in `import_state` to the importing deployment's declared value (changes Task 2's transfer assertion). |
| CONCERN | Risk & Robustness | C3 — Race 1's "one missed reset, recovered on the next save" understates it: a concurrent `_adjust_cycle_amplitudes` repacks the stale slot 3 (`base.py:2762`), reverting the baseline, so a later uncontended save fires a spurious second reset that discards real learning. | Race Conditions / Race 1 | Correct the Race 1 mitigation text to name this consequence. No code change — stays inside Race 1's accepted scope and the #699 No-Go. |
| CONCERN | Scope & Value | C4 — Success Criterion 1 reads unconditionally ("resets ... on the next `save()`") while Risk 2 concedes the first post-upgrade save cannot detect an edit; the caveat is buried in Risks / OQ3. | Problem / Desired outcome, Success Criteria, Risk 2 | State the two-save requirement next to Desired Outcome and qualify Success Criterion 1 with "for every record that has a recorded baseline". |
| CONCERN | Risk & Robustness | C5 — One `logger.info` per reset per record means a single declaration edit emits an unbounded INFO burst across every learned record at the 20k-record scale target, with no sampling or aggregation discussed. | Solution / Key Elements ("A loud reset") | Accept explicitly as a per-record audit trail and note the volume in the docs task; do not add per-process dedupe (OQ2 already prices it as out of appetite). |
| CONCERN | structural check (Step 2d) | C6 — The editable-install prerequisite is vacuous: `'popoto' in str(...popoto.__file__)` is true for any install. Run live it PASSED while resolving to the main checkout, not `.worktrees/sdlc-698` — the exact failure it guards is live and the check is green. | Prerequisites, row 3 | Compare the resolved `popoto.__file__` against the checkout root (`is_relative_to(Path.cwd().resolve())`), or defer the row to `scripts/ci-local.sh`. |
| NIT | History & Consistency (first pass); structural check (Step 2e) | N1 — Success Criteria says "four clarifying updates named in Test Impact"; Test Impact actually lists five UPDATE dispositions inside `TestLearnedAmplitudePreservedOnSave`. | Success Criteria, bullet 2 | Say five, or enumerate them, so `final-validator` checks against the right count. |

### Blockers

#### B1 — `strengthen_cycle()` / `weaken_cycle()` return shape widens; "no interface changes" is false

- **Critics:** History & Consistency
- **Location:** Architectural Impact ("Interface changes: none to any public Python API")
- **Finding:** `_adjust_cycle_amplitudes` unpacks the stored payload
  (`base.py:2743`), mutates only `cycle[1]` (`:2748-2751`), and **returns the
  list it read** (`:2763`), surfaced through the public `strengthen_cycle` /
  `weaken_cycle` (`:2673`, `:2693`), whose docstrings say "The updated cycles
  list". Once `on_save` writes 4-element entries, those two public methods start
  returning 4-element sublists for any already-saved record. The plan's Rabbit
  Holes rule forbids `_adjust_cycle_amplitudes` from **writing** slot 3 but says
  nothing about it **returning** slot 3, so the claim of no public interface
  change is wrong as written.
- **Suggestion:** Make an explicit choice and record it: either (a) document the
  widened return shape in both docstrings and in Architectural Impact, or
  (b) strip slot 3 on the way out of `_adjust_cycle_amplitudes` so the public
  return contract stays 3-element. Add a task step and a test either way.
- **Implementation Note:** If (b), truncate at the return sites only — `return
  cycles` at `base.py:2763` becomes `return [cycle[:3] for cycle in cycles]`, and
  the packed value written at `:2756-2762` must keep all four slots. Do **not**
  truncate before `msgpack.packb`, which would make `_adjust_cycle_amplitudes` a
  slot-3 writer and destroy the mechanism (the Rabbit Hole this plan already
  names). The pipeline branch (`:2758-2760`) returns the pipeline and is
  unaffected. `tests/test_observation_protocol.py:690` asserts only `result ==
  []` on the no-entry path, so no existing test pins the arity — a new one must.

### Concerns

#### C1 — Open Question 1 is declared unresolvable by the plan, yet the tasks already implement one answer

- **Critics:** Scope & Value
- **Location:** Open Questions 1 / Step by Step Tasks (build-merge, build-tests)
- **Finding:** OQ1 calls hard-reset-vs-proportional-rescale "a product call and
  the one thing the plan cannot settle on its own", and Appetite budgets one PM
  check-in for it — but Tasks 1 and 3 fully implement and pin hard reset, with no
  task gated on the decision. A "rescale" answer rewrites the merge branch, its
  log message, its docstring and most of the new test class after the code exists.
- **Suggestion:** Resolve it in the plan text — state that hard reset ships and
  rescale is deferred to a follow-up issue — or add an explicit decision
  dependency ahead of `build-merge` / `build-tests`.
- **Implementation Note:** If hard reset is confirmed, add one sentence to OQ1
  ("Decided: hard reset ships; proportional rescale tracked separately") and drop
  the "the plan cannot settle" framing, so `merge-builder` is not building against
  an officially-open question. If rescale is chosen instead, the plan must also
  answer `old_baseline == 0.0` (division by zero → fall back to the declared
  value) before Task 1 starts.

#### C2 — Cross-deployment import carries the *exporter's* baseline and can fire a spurious reset

- **Critics:** History & Consistency
- **Location:** Solution / Key Elements ("Transfer consistency"), Risk 2, Task 2
- **Finding:** Risk 2 covers only the legacy no-baseline import. It does not
  cover importing a record whose slot 3 was recorded under the *exporting*
  deployment's declaration into a target whose `field.cycles` amplitude differs
  for that period. The first `save()` after import sees `baseline != declared`
  and resets — destroying exactly the learned amplitude
  `roundtrip_policy = "carry"` exists to preserve, with no import-time signal.
- **Suggestion:** Add this as a named Risk parallel to Risk 2 and either accept
  it explicitly (cross-deployment transfer assumes matching declarations) or have
  `import_state` re-baseline to the **importing** deployment's declared value.
- **Implementation Note:** Task 2 currently says carry slot 3 through
  normalization verbatim (`cyclic_decay_field.py:344-349`). Re-baselining instead
  means deriving the baseline from `field.cycles` at import time rather than from
  `cycle[3]` of the payload — which changes the assertion Task 2 adds to
  `tests/test_transfer_fidelity_fields.py` from byte-identical carry to
  re-baselining. Pick one before either task starts; the two are mutually
  exclusive and both are currently written into the plan.

#### C3 — Race 1's mitigation understates the new consequence: a spurious *delayed* reset

- **Critics:** Risk & Robustness
- **Location:** Race Conditions / Race 1
- **Finding:** Race 1 says a lost baseline update means "one missed reset,
  recovered on the next save." The actual new failure mode is worse: if
  `_adjust_cycle_amplitudes` reads the pre-reset entry and its `hset`
  (`base.py:2762`) lands after `on_save`'s, it repacks the **stale** slot 3
  (spike-2: it preserves unknown slots). The stored baseline reverts to the
  superseded value, and the *next, uncontended* save fires a second reset that
  discards the learning applied in between.
- **Suggestion:** Correct the Race 1 mitigation text to name this consequence, so
  a maintainer investigating an unexplained reset knows where to look. No code
  change — this remains inside Race 1's accepted scope and the No-Go for #699.
- **Implementation Note:** Interleaving to record verbatim: (1) `on_save` reads
  `baseline_old != declared`, writes `[period, declared, phase, declared]`;
  (2) `_adjust_cycle_amplitudes` had already `hget`'d
  `[period, learned_old, phase, baseline_old]` at `base.py:2736`; (3) it writes
  `[period, learned_old*factor, phase, baseline_old]` at `:2762`, clobbering the
  reset *and* restoring the stale baseline.

#### C4 — Risk 2's "first deploy does not detect the edit" is buried

- **Critics:** Scope & Value
- **Location:** Problem / Desired outcome, Success Criteria, Risk 2, OQ3
- **Finding:** Every currently-deployed record has a 3-element entry, so a
  developer who upgrades and edits `amplitude=` in the same deploy reproduces the
  reported symptom once more. Success Criterion 1 ("An edited declared amplitude
  resets the learned amplitude on the next `save()`") reads as unconditional and
  contradicts that, and the caveat appears only in Risk 2 / OQ3.
- **Suggestion:** State the two-save requirement next to Desired Outcome and
  qualify Success Criterion 1 ("...for every record that has a recorded
  baseline"), rather than leaving it to be inferred from Risks.
- **Implementation Note:** In `docs/features/cyclic-decay-field.md` (task
  `document-feature`), give the operator the concrete remedy already documented
  for a reset: `hdel` the member's cycles entry and re-save, or upgrade first and
  let every record save once before editing the declaration.

#### C5 — One INFO line per reset per record is an unbounded log burst

- **Critics:** Risk & Robustness
- **Location:** Solution / Key Elements ("A loud reset")
- **Finding:** A single declaration edit resets every record that has learned an
  amplitude for that period, one `logger.info` each, on their next save. On a hot
  field at the 20k-record scale target that is a burst with no sampling,
  aggregation or rate limit, and the plan never surfaces it.
- **Suggestion:** Accept it explicitly as a per-record audit trail (the cheap
  answer, and the reason INFO beats WARNING — see OQ2 below), and note the volume
  next to the destructive-edit warning in the docs task.
- **Implementation Note:** One sentence in `docs/features/cyclic-decay-field.md`
  alongside the line-122 rewrite already scheduled for `cyclic-doc`. No code
  change; do **not** add a per-process-per-field dedupe, which OQ2 already prices
  as more code than a Small appetite wants.

#### C6 — The editable-install prerequisite check is vacuous

- **Critics:** structural check (Step 2d)
- **Location:** Prerequisites, row 3
- **Finding:** `python -c "import popoto, pathlib, sys; sys.exit(0 if 'popoto' in
  str(pathlib.Path(popoto.__file__)) else 1)"` is true for *any* popoto
  installation, including a stale one in another checkout — it cannot detect the
  failure it cites (worktree gotcha 1). Run live at critique time it **passed**
  while resolving to `/Users/valorengels/src/popoto/src/popoto/__init__.py`, the
  **main** checkout, not `.worktrees/sdlc-698` — i.e. the exact condition the row
  exists to catch is currently live and the check is green.
- **Suggestion:** Compare against the checkout root, not the substring `popoto`.
- **Implementation Note:** Replace with
  `python -c "import popoto,pathlib,sys; sys.exit(0 if
  pathlib.Path(popoto.__file__).resolve().is_relative_to(pathlib.Path.cwd().resolve())
  else 1)"` run from the worktree root, or simply
  `python -c "import popoto; print(popoto.__file__)"` and eyeball it against
  `git rev-parse --show-toplevel`. `scripts/ci-local.sh` already performs this
  check — deferring to it is also acceptable, but the row as written must not
  stay, because it reports green on the failure.

### Nits

#### N1 — Success Criteria says "four clarifying updates"; Test Impact lists five

- **Critics:** History & Consistency (first pass), structural check (Step 2e)
- **Location:** Success Criteria, bullet 2
- **Finding:** Test Impact names five UPDATE dispositions inside
  `TestLearnedAmplitudePreservedOnSave` (`test_cycle_added_to_declaration_uses_declared_amplitude`,
  `test_phase_refreshes_from_declaration_while_amplitude_persists`,
  `test_cycle_removed_from_declaration_is_dropped`,
  `test_corrupt_stored_entry_falls_back_to_declared`,
  `test_duplicate_periods_pair_fifo_and_keep_order`), plus the module-level
  `_read_cycles` helper. Success Criteria says four.
- **Suggestion:** Say five, or enumerate them, so `final-validator` is not
  checking against the wrong count.

### Open Questions — critique disposition

1. **Hard reset vs. proportional rescale** — **ESCALATED, unresolved.** No critic
   found a technical objection to hard reset, and rescale adds an
   `old_baseline == 0.0` case, but this is a semantics choice for the maintainer.
   It must be answered in the plan text before build (C1), not left open while the
   tasks implement one side of it.
2. **INFO vs WARNING for the reset log** — **RESOLVED: INFO, as planned.** C5 is
   the argument: a single edit resets every learned record, so WARNING would be a
   fleet-wide alarm burst on an intentional, expected event. The per-record INFO
   line is the audit trail; the volume gets one documented sentence.
3. **Risk 2 upgrade caveat as documentation only** — **RESOLVED: acceptable as
   documentation.** The only alternative (treat "no baseline" as "changed")
   resets every learned amplitude in the database on first save, which is
   strictly worse. But the caveat's *placement* is a real defect (C4): it belongs
   next to Desired Outcome and must qualify Success Criterion 1, not sit only in
   Risks.

### Structural check results

| Check | Status | Detail |
|---|---|---|
| Required sections | PASS | All plan sections present and non-empty |
| Task numbering | PASS | Tasks 1-6, no gaps |
| Dependencies valid | PASS | `build-merge`, `build-transfer`, `build-tests`, `validate-merge`, `document-feature` all resolve; no cycles |
| File paths exist | PASS | 12 of 12 referenced paths exist |
| Prerequisites met | PARTIAL | Redis DB 12 PONG; extras import; editable-install row passes **vacuously** (C6) |
| Cross-references | FAIL | Success Criteria "four" vs Test Impact five (N1) |
| Verification rows reproduce | PASS | Re-ran at plan time on unmodified tree: `TestLearnedAmplitudePreservedOnSave`=1, stale-doc-claim=1 (red as documented), numkeys=1, `logger.info`=0, new class=0 — all match the plan's stated smoke test |

---

## Decisions (formerly Open Questions)

**No open questions remain.** All three were answered by the supervisor on
2026-09-07 during the critique-revision pass. They are recorded here as decided
and **must not be reopened** by a builder, reviewer or later critique round; a
new argument against one of them is a new issue, not a re-litigation of this
plan.

1. **On a detected declaration change: hard reset, or proportional rescale?** →
   **HARD RESET.** The learned amplitude is discarded and the new declared value
   takes its place. *Rationale (supervisor):* no critic found a technical
   objection to it; it is predictable and it matches developer intent when
   someone edits a declaration. **Rejected alternative, documented:** preserving
   the learned *ratio*
   (`new_learned = new_declared * (old_learned / old_baseline)`). It is less
   destructive and keeps accumulated learning meaningful, but it is harder to
   predict, needs a separate `old_baseline == 0.0` division-by-zero rule, and
   would rewrite the merge branch, its log line, its docstring and most of the
   new test class. Consequence: Risk 1 is accepted at full weight rather than
   mitigated (see Risk 1).

2. **INFO or WARNING for the reset log?** → **INFO.** *Rationale:* one edit
   resets every learned record, so WARNING would be a fleet-wide alarm burst on
   an intentional, expected event. **Volume is accepted explicitly** as a
   per-record audit trail — one line per reset per record, bounded by the number
   of affected records and one-shot per edit — and gets a documented sentence
   (C5, Solution / Key Elements, `document-feature`). No sampling, no
   aggregation, no per-process dedupe.

3. **Is the Risk 2 upgrade caveat acceptable as documentation only?** →
   **YES, accepted.** *Rationale:* the only alternative that closes it — treating
   "no baseline" as "declaration changed" — resets every learned amplitude in the
   database on the first save after upgrade, which is strictly worse. The
   two-save requirement is now stated next to the Desired Outcome and qualifies
   Success Criterion 1 (C4), rather than sitting only in Risks, and the concrete
   operator remedies are in the docs task.

### Revision log

Revision 1 (2026-09-07), responding to the NEEDS REVISION critique verdict
recorded above:

| Finding | Disposition |
|---|---|
| B1 (blocker) — public return shape widens | **Resolved by decision**: truncate at the return site (`base.py:2763`), keeping the 3-element public contract. New Task 1b, new named test, two new Verification rows, Architectural Impact rewritten. |
| C1 — hard reset vs rescale left open | **Resolved**: hard reset decided by the supervisor and recorded in Solution / Key Elements and Decisions; rescale recorded as a rejected alternative and moved to No-Gos. No task is gated on a decision any more. |
| C2 — import carries the exporter's baseline | **Resolved by reversing the approach**: `import_state` deliberately drops slot 3; Task 2 rewritten, Risk 5 added, transfer assertion inverted, Verification row added. |
| C3 — Race 1 understates the consequence | **Resolved**: Race 1 mitigation rewritten with the four-step interleaving and the spurious-delayed-reset consequence named. Documentation only; still inside the #699 No-Go. |
| C4 — Success Criterion 1 contradicts Risk 2 | **Resolved**: two-save requirement added next to Desired Outcome; Success Criterion 1 qualified with "for every record that has a recorded baseline"; operator remedies added to the docs task. |
| C5 — unbounded INFO burst | **Resolved**: accepted explicitly with rationale and bounds in Solution / Key Elements; one volume sentence added to the docs task. No code change. |
| C6 — vacuous editable-install prerequisite | **Resolved**: row replaced with an `is_relative_to(git rev-parse --show-toplevel)` check, run live and **failing** from `.worktrees/sdlc-698` (resolves to the main checkout). The red state and the disclosure obligation are recorded in the row, in `validate-merge` and in Verification. |
| N1 (nit) — "four" vs five updates | **Resolved**: Success Criteria now says five and enumerates them. |
