---
status: Planning
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/679
last_comment_id: none
---

# #679 — `CyclicDecayField.on_save` clobbers learned per-member cycle amplitudes

## Problem

`CyclicDecayField.on_save` unconditionally rewrites the per-member cycles hash
entry from the class-level `field.cycles` declaration on **every** save
(`src/popoto/fields/cyclic_decay_field.py:542-547`):

```python
# Store cycles data (always write field-level defaults)
if normalized_cycles:
    db.hset(cycles_hash_key, member_key, msgpack.packb(normalized_cycles))
else:
    db.hdel(cycles_hash_key, member_key)
```

`normalized_cycles` is built a few lines above from `field.cycles` alone, with
no read of the member's existing entry. Per-member amplitudes are **learned**
state — `Model.strengthen_cycle` / `Model.weaken_cycle` mutate them — so any
ordinary `save()` silently resets everything those calls accumulated.

There is no error and no warning. The symptom presents as cyclic resonance
"not working" rather than as a write being undone, which is why it survived
six months.

### Current behavior

1. `item.save()` → cycles hash entry = class defaults.
2. `item.strengthen_cycle("relevance", factor=1.5)` → entry amplitude ×1.5.
3. `item.save()` (for any reason, including an unrelated field edit) → entry
   silently reverts to class defaults. Step 2 is erased.

### Desired outcome

Step 3 preserves the learned amplitude. Declared cycle *parameters* (period,
phase) still refresh from the class declaration on every save; only the
learned *amplitude* is preserved.

## Freshness Check

Baseline: `78269b15` (worktree), re-verified against `main` @ `7e738933`.
Issue filed 2026-09-07, planned same day.

**Disposition: Minor drift.** Every cited reference still holds; one stale
cross-reference found that this plan now absorbs.

| Reference | Status |
|---|---|
| `cyclic_decay_field.py:542-547` — the clobber | Confirmed verbatim, unchanged |
| `cyclic_decay_field.py:256-260` — class comment asserting amplitudes diverge | Confirmed verbatim |
| `cyclic_decay_field.py:319-329` — `import_state` ordering docstring | Confirmed; already cites #679 (landed in `b9dd9ac5`) |
| `tests/test_transfer_fidelity_fields.py:34` — "deferred to **#556**" | **Stale.** `b9dd9ac5` repointed the `src/` comment to #679 but missed this one. Corrected by this plan. |
| Commits touching the field since the issue was filed | `bbd8297a` (#556, transfer carry) and `1d50bd83` (#648, context_assembler routing). Neither touches `on_save`. |

## Prior Art

| Ref | Relevance |
|---|---|
| **#201** (`a3a78367`, 2026-03-13) | Created `CyclicDecayField` **and** this write. Its plan explicitly scoped out cycle self-correction: *"do not implement cycle self-correction here. CyclicDecayField stores static cycle parameters as configured."* At that moment "always write field-level defaults" was **correct** — no learned state existed. |
| **#206** (`4a8a6a33`, 2026-03-14) | Added `strengthen_cycle` / `weaken_cycle` — **the next day**, in `models/base.py`. This commit touched **zero lines** of `cyclic_decay_field.py`. The write was never revisited. |
| **#554** (PR #558) | Added the class comment at `:256-260` asserting amplitudes diverge, and worked *around* the clobber by restoring after save. Its plan deferred the fix in its **Rabbit Holes** section (`docs/plans/generic_export_import_roundtrip.md:722`): *"It changes in-place save semantics for every existing user of that field. Out of scope."* Its No-Gos section paraphrases the same point at `:833-835` rather than repeating that wording. |
| **#556** (PR #675) | Re-affirmed the same No-Go; repointed the in-code reference to #679. |
| **#583** | Adjacent, not overlapping: `on_context_used` degradation on *unsaved* instances. Touches `observation.py`, which calls `strengthen_cycle`. Coordination only — no shared edit surface. |

### Why this was never "previously fixed"

It was never attempted. Two plans (#554, #556) identified it, scoped it out
deliberately and for a stated reason, and filed it forward. This is the first
plan to take it on.

## Spike Results

### spike-1: Does anything actually consume the learned divergence?

- **Assumption**: the learned amplitude has an observable effect, so preserving
  it is worth doing (as opposed to deleting the learning path as dead code).
- **Method**: code-read
- **Result**: **Confirmed — it is consumed.** `CYCLIC_DECAY_LUA`
  (`cyclic_decay_field.py:148-162`) does `HGET cycles_hash_key member` and
  computes `cyclic = cyclic + amplitude * math.cos(two_pi * (now - phase) / period)`,
  which feeds `effective_score = decayed + cyclic + pressure`, which is sorted
  and truncated. Reached from `Query.top_by_decay`, `Query.composite_score`,
  and `recipes/context_assembler.py:750-753`. Three real production writers
  exist: `observation.py:294` (`_apply_acted`), `:343` (`_apply_dismissed`),
  `:406` (`_apply_contradicted`).
- **Confidence**: high
- **Impact if false**: would have inverted the fix — delete the learning path
  instead of preserving it.

### spike-2: Is the unconditional write deliberate?

- **Assumption**: the self-describing comment "always write field-level
  defaults" means the clobber was an intentional design choice that a fix
  would be overriding.
- **Method**: code-read (git archaeology)
- **Result**: **Refuted — it is stale, not deliberate.** The write predates the
  learning it destroys by one day (#201 → #206), and #206 never touched this
  file. The comment was accurate when written and was never updated. The
  apparent contradiction between `:542-547` ("always write defaults") and
  `:256-260` ("amplitudes are LEARNED and diverge from defaults") is
  **chronological, not a design disagreement** — `:256-260` was added five
  months later (PR #558) and describes the intent that `:542-547` silently
  violates.
- **Confidence**: high
- **Impact if false**: would require a product decision about which behavior is
  intended rather than a straight bug fix.

### spike-3: Does any existing test lock in the clobber?

- **Assumption**: some test asserts post-save default amplitudes and would
  break on a fix.
- **Method**: code-read
- **Result**: **No.** In `tests/test_observation_protocol.py::TestCycleMethods`,
  every `save()` is setup **before** strengthening; no test saves afterward and
  asserts an amplitude. `test_transfer_fidelity_fields.py::test_learned_amplitude_and_pressure_age_survive`
  strengthens then exports/imports, and import restores *after* save, so it
  sidesteps the clobber entirely. Nothing would fail if the clobber were fixed.
- **Confidence**: high
- **Impact if false**: would require negotiating an intentional behavior change
  with existing test contracts.

## Data Flow

```
observation.py::_apply_acted/_apply_dismissed/_apply_contradicted
  └─> Model.strengthen_cycle / weaken_cycle          (models/base.py:2655/2675)
        └─> _adjust_cycle_amplitudes                 (models/base.py:2695-2763)
              read  HGET  cycles_hash  member  ──┐
              write HSET  cycles_hash  member  ──┘  amplitude *= factor, clamped [0, 100]

Model.save()  (any field, any reason)
  └─> CyclicDecayField.on_save                       (cyclic_decay_field.py:511-570)
        ├─ cycles branch  :531-547   ← THE DEFECT: writes field.cycles, no read
        └─ pressure branch :549-568  ← CORRECT: reads first, preserves last_resolved

Query.top_by_decay / composite_score / context_assembler
  └─> rank_decayed                                   (cyclic_decay_field.py:417-486)
        └─> CYCLIC_DECAY_LUA                         (cyclic_decay_field.py:148-162)
              HGET cycles_hash member
              cyclic += amplitude * cos(2π(now - phase) / period)
              effective_score = decayed + cyclic + pressure   → sort → top N
```

The write path and the read path are both real. Only the middle step is broken.

## Solution

Mirror the pressure branch that sits **directly below** the defect in the same
function. That branch already encodes the exact principle needed here:

> declared parameters refresh from the field; learned state is preserved.

For pressure, `rate` is declared (always refreshed from `field.pressure_rate`)
and `last_resolved` is learned (never overwritten). For cycles, **`period` and
`phase` are declared; `amplitude` is learned.** The fix applies the same split.

### Technical approach

Replace the cycles branch at `:531-547` with a read-then-merge:

```python
# Cycle periods and phases are declarative; amplitudes are LEARNED
# (strengthen_cycle / weaken_cycle). Mirror the pressure branch below:
# refresh the declared parameters, preserve the learned one. Read directly
# from Redis (not the pipeline) because the result is needed immediately.
#
# The read is gated on `field.cycles` so a CyclicDecayField declaring no
# cycles (one used only for pressure_rate) pays no extra round trip — it
# falls straight through to the unchanged `hdel` branch, exactly as today.
learned: dict[float, list[float]] = {}
if field.cycles:
    existing_raw = get_REDIS_DB().hget(cycles_hash_key, member_key)
    if existing_raw:
        try:
            stored = msgpack.unpackb(existing_raw, raw=False)
        except Exception:
            # Mirror export_state's handler (:284-291) — #679 exists because
            # this state was destroyed silently; do not add a second mute path.
            logger.warning(
                f"Could not decode cycles data for {member_key}; "
                f"falling back to declared defaults"
            )
            stored = None
        if isinstance(stored, list):
            for entry in stored:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    learned.setdefault(entry[0], []).append(entry[1])

normalized_cycles = []
for cycle in field.cycles:
    period, amplitude = cycle[0], cycle[1]
    phase = cycle[2] if len(cycle) > 2 else 0
    bucket = learned.get(period)
    if bucket:
        amplitude = bucket.pop(0)
    normalized_cycles.append([period, amplitude, phase])
```

`if bucket:` tests a **list**, not an amplitude, so a learned `0.0` is
preserved rather than falling through to the declared default. That is
deliberate — see the recovery path below.

The write at `:543-547` is then unchanged in shape; only what it writes changes.

**Matching identity is `period`**, FIFO within duplicate periods. A cycle *is*
its period (daily, weekly) — that is the stable identity across a declaration
edit. Consequences, all intended:

| Case | Behavior |
|---|---|
| Period declared and stored | Learned amplitude preserved; phase refreshed from declaration |
| Period newly added to declaration | Declared amplitude used (nothing learned yet) |
| Period removed from declaration | Dropped — declaration is authoritative about *which* cycles exist |
| Declared amplitude edited by developer, amplitude already learned | Learned wins. We cannot distinguish "developer changed the default" from "learning diverged" without storing a base, and preserving learning is the property this fix exists to protect. |
| Stored entry corrupt / unparseable | Falls back to declared defaults; no exception escapes `save()` |
| No stored entry (first save) | Declared defaults — unchanged from today |
| `field.cycles` empty | `hdel` — unchanged from today |

**Read client:** the new read uses `get_REDIS_DB()` rather than the module's
imported `POPOTO_REDIS_DB`, per CLAUDE.md's #655 rule that a plain import holds
a snapshot `set_REDIS_DB_settings()` never updates. This is a file-scoped
inconsistency with the adjacent pressure read at `:553` — deliberately left
alone, see Rabbit Holes.

**Pipeline:** the read goes direct, not through the pipeline, exactly as the
pressure branch does at `:551-553` and for the same stated reason — the result
is needed immediately to decide what to write. This inherits the pressure
branch's existing limitation (within one un-executed pipeline, all reads see
pre-pipeline state). Mirroring precedent rather than inventing new semantics.

## No-Gos

- **Do not change `_adjust_cycle_amplitudes`** (`models/base.py:2695-2763`). It
  is correct. The bug is entirely in `on_save`.
- **Do not change `CYCLIC_DECAY_LUA` or `rank_decayed`.** The read path is
  correct and already consumes amplitudes properly.
- **Do not invert the transfer import ordering.** `import_state` runs after
  `save()` by design; that stays true and stays correct after this fix.
- **Do not convert the file's other `POPOTO_REDIS_DB` uses** — #655 tracks that
  sweep. Only the newly added read uses the accessor.
- **Do not touch the pressure branch.** It is the reference implementation, not
  a subject of this change.
- **Do not add configuration** for whether amplitudes persist. Per CLAUDE.md,
  numeric/behavioral constants here are not user config; and a flag would just
  preserve the bug behind an option.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **In-place save semantics change for existing users.** The precise concern #554 and #556 cited when deferring. A deployment relying on save-resets-amplitudes would change behavior. | Low | That reliance would be reliance on a documented bug that erases the field's own advertised feature. No test encodes it (spike-3). Called out in CHANGELOG as a behavior fix. **This is a deliberate behavior change, not a pure defect repair** — see the note below; it needs a named approver on the record before merge, not a self-authorized reversal. |
| **Extra `HGET` per save per cyclic field.** | Certain | One additional round trip, only for `CyclicDecayField`s **that declare a non-empty `cycles`** — the read is gated on `field.cycles`, so a pressure-only field pays nothing. The pressure branch directly below already pays exactly this cost unconditionally. Acceptable and symmetric. |
| **(a) Cross-connection lost update** between a concurrent `save()` and `strengthen_cycle` / `weaken_cycle`. The fix makes the cycles write a read-modify-write, and `_adjust_cycle_amplitudes` (`models/base.py:2734-2762`) is already an unguarded `HGET`→`HSET` on the *same* key and member. Interleaving `read(save) → read(adjust) → write(adjust) → write(save)` drops the adjustment. | Low | **Accepted, documented limitation.** Today's behavior is *deterministic* loss (save always clobbers); after the fix it is *nondeterministic* loss inside a narrow window — strictly less data lost, less predictable. Closing it means moving both call sites into one Lua script or `WATCH`/`MULTI`, a larger change than this appetite that would also have to cover the identical pre-existing shape in the pressure branch. Priced, not missed. |
| **(b) Same-pipeline call-order loss.** Distinct from (a) and *worse*: `_adjust_cycle_amplitudes` reads **direct** (`POPOTO_REDIS_DB.hget`, `models/base.py:2736`) but writes **through** the caller's pipeline (`:2758`). So `instance.weaken_cycle(f, pipeline=p)` queued before `instance.save(pipeline=p)` on one pipeline loses the adjustment **deterministically, every time** — `save()`'s read runs before `p.execute()` and so never sees the queued `HSET`, and `save()`'s own `HSET` is queued last and wins. No timing dependency. Reachable through the library's own batching idiom, including `observation.py:343`'s `apply_outcome(..., pipeline=p)`. | Medium | **Not a regression** — the unconditional clobber loses this ordering today too, so the fix neither creates nor worsens it; it simply does not reach it. Scoped out here because closing it requires routing the adjust read through the pipeline, which is not expressible without executing mid-batch. **Mitigated by documenting the safe order** (`save()` first, adjust second, when sharing one pipeline) in both feature docs, and by TC8 asserting the non-pipelined case works. Named explicitly so a reader cannot mistake the fix for covering it. |
| **Declaration edits behave surprisingly** (learned amplitude survives a changed default). | Medium | Documented explicitly in the fidelity table above and in the feature doc. The alternative — resetting learning whenever a developer edits a default — is strictly worse for the feature's purpose. |
| **Corrupt hash entry crashes `save()`.** | Low | Explicit try/except falling back to declared defaults. Test TC7 covers it. |
| **Duplicate declared periods mis-pair.** | Very low | FIFO within a period bucket keeps pairing stable and order-preserving. Test TC5 covers ordering. |

### On overriding the #554 / #556 No-Go

Two prior plans declared fixing this out of scope, both citing the in-place
save-semantics change. This plan proceeds. #554's wording lives in that plan's
**Rabbit Holes** section (`generic_export_import_roundtrip.md:722`), which is
where a deferral belongs; its No-Gos section paraphrases at `:833-835` rather
than repeating it.

The reading that makes proceeding legitimate is that these were
**point-in-time scope guards** — "not in *this* PR" — and #679 was filed
precisely as the place the work would land. That is a **weaker** case than the
analogous #557 reasoning, and the difference matters: #557's anti-criteria
recorded that no consumer had been identified, whereas #554's stated reason is
a backward-compatibility risk, which does not expire just because the work
moved to its own issue.

What that reasoning does *not* by itself supply is authorization for the
observable behavior change. The evidence in spike-3 is stronger than what #554
and #556 had, but it is re-analysis of the same test suite by the same author,
not a maintainer decision. **Before merge, the PR must name who accepted the
semantics change.** Build and review may proceed in the meantime — the risk is
at merge, not at build.

## Documentation

- `docs/features/cyclic-decay-field.md` — state that learned amplitudes persist
  across saves, and that period/phase refresh from the declaration. **Also
  document the recovery path** (Decision 2): an amplitude weakened to `0.0`
  stays there across saves, and the way back to the declared default is to
  delete the member's field from the cycles hash. Name it next to the
  persistence note so it is discoverable without reading source.
- `docs/features/observation-protocol.md` — the outcome hooks that call
  `strengthen_cycle` / `weaken_cycle` now have durable effect; remove any
  wording implying otherwise. **Document the safe pipeline order** (Risks row
  b): when `save()` and an amplitude adjustment share one pipeline, `save()`
  must be queued first, or the adjustment is lost on `execute()`.
- `docs/features/cyclic-decay-field.md` — carry the same pipeline-ordering note,
  since the hazard belongs to the field, not only to the observation protocol.
- `src/popoto/fields/cyclic_decay_field.py:319-329` — rewrite the `import_state`
  ordering docstring. The ordering stays correct, but the *reason* changes: it
  is no longer "on_save clobbers, so we land on top." Do not delete the
  ordering note; restate why it still holds.
- `tests/test_transfer_fidelity_fields.py:30-38` — remove the "known
  pre-existing bug ... deferred to #556" note. It is both stale (wrong issue)
  and, after this change, false.
- `CHANGELOG.md` — `### Fixed` entry under `[Unreleased]`.

## Success Criteria

1. `save()` after `strengthen_cycle` / `weaken_cycle` preserves the learned
   amplitude.
2. Declared `period` and `phase` still refresh from `field.cycles` on save.
3. A cycle removed from the declaration is removed from storage; one added
   gets the declared amplitude.
4. The learned amplitude is observable **through ranking**, not merely in the
   hash — `rank_decayed` ordering reflects it after a save.
5. Corrupt stored state degrades to declared defaults without raising.
6. Transfer round-trip fidelity is unchanged (`test_transfer_fidelity_fields.py`
   still green, including `test_learned_amplitude_and_pressure_age_survive`).
7. Every new test fails if the fix is reverted (mutation-proven, table in the
   PR body).

## Verification

| Check | Command | Expected |
|---|---|---|
| New + existing cyclic tests | `pytest tests/test_cyclic_decay_field.py tests/test_observation_protocol.py -q` | all pass |
| Transfer not regressed | `pytest tests/test_transfer_fidelity_fields.py tests/test_transfer_history_state.py -q` | all pass |
| Lint | `ruff check src/` | exit 0 |
| Format | `black --check src/ tests/` | exit 0 |
| Types (ratchet) | `scripts/mypy_ratchet.py` | ≤ baseline; `integrations/`, `privacy/` at 0 |
| Docs | `mkdocs build --strict` | exit 0 |
| Anti-criterion: pressure branch untouched | `git diff main -- src/popoto/fields/cyclic_decay_field.py \| grep -c 'last_resolved'` | 0 |
| Anti-criterion: Lua untouched | `git diff main -- src/popoto/fields/cyclic_decay_field.py \| grep -c 'CYCLIC_DECAY_LUA'` | 0 |
| Stale #556 reference gone | `grep -c 'deferred to #556' tests/test_transfer_fidelity_fields.py` | 0 |

## Step by Step Tasks

1. **Write the failing regression test first (TC1).** `save` → `strengthen_cycle`
   → `save` → assert amplitude preserved. Confirm it FAILS on unmodified `main`;
   record the failure output. This is the proof the bug is real before any fix.
2. **Implement the read-then-merge** in `on_save`'s cycles branch
   (`cyclic_decay_field.py:531-547`), using `get_REDIS_DB()` for the read.
3. **Add remaining tests:**
   - TC2 first save writes declared defaults
   - TC3 nothing learned → declared amplitude used
   - TC4 period added → declared; period removed → dropped
   - TC5 phase/period refresh from declaration while amplitude preserved;
     duplicate-period ordering stable
   - TC6 `cycles=[]` → `hdel` still fires
   - TC7 corrupt stored entry → defaults, no raise
   - TC8 pipelined save preserves
   - TC9 **end-to-end ranking** — strengthen, save, assert `rank_decayed`
     ordering reflects the learned amplitude (proves the consuming path, not
     just hash bytes)
   - TC10 a `CyclicDecayField` with `cycles=[]` (pressure-only) issues **no**
     cycles `HGET` on save — assert via a spy on the client, so the Risks
     table's cost claim is enforced rather than asserted. The spy pins a
     mechanism, not a behavior, so its docstring must say so explicitly
     ("implementation-pinning: update deliberately on refactor") — the
     invariant it protects is that deleting the `if field.cycles:` gate fails
     the suite.
   - TC11 an amplitude weakened to `0.0` survives a subsequent save
     (Decision 2), and deleting the hash member restores declared defaults on
     the next save (the documented recovery path)
4. **Update the `import_state` ordering docstring** (`:319-329`) — ordering
   still correct, reason restated.
5. **Remove the stale bug note** in `tests/test_transfer_fidelity_fields.py:30-38`.
6. **Docs cascade** — `cyclic-decay-field.md`, `observation-protocol.md`,
   `CHANGELOG.md`.
7. **Mutation-prove every new test.** Revert the fix, confirm which tests die,
   restore, and put the table in the PR body.
8. **Run the Verification table** and record results with the environment stated.

## Decisions

These were carried as open questions through critique and are now settled.
Neither blocks build.

1. **Declared-amplitude edits lose to learned values** (fidelity table, row 4).
   The alternative is storing the declared base alongside the learned value so
   a declaration edit can reset learning — strictly more machinery and an
   on-disk format change. **Decided: learned wins.** File a follow-up only if a
   real consumer needs declaration edits to reset learning.
2. **An amplitude driven to `0.0` by `weaken_cycle` is preserved, not treated
   as "unconfigured."** `_adjust_cycle_amplitudes` deliberately snaps `< 0.01`
   to `0.0`, which reads as intentional suppression, so preserving it is the
   consistent choice. **Decided: preserve `0.0`.**

   This is the one case where "preserve learned" and "the record looks
   unconfigured" are indistinguishable on disk, and it is durable — every
   future `save()` keeps the zero. There is no public reset API today
   (`models/base.py` has `resolve_pressure` for the pressure companion, but no
   `reset_cycle` / `reset_amplitude`), so the **only** recovery is deleting the
   member's entry from the cycles hash, after which the next `save()` re-adopts
   the declared defaults. Adding a `reset_cycle()` method is out of appetite;
   **documenting the recovery path is not**, and is a task below. Deferring the
   method rather than the documentation is the whole point — an
   undiscoverable escape hatch is the trap, not the absence of sugar.

## Open Questions

None blocking. Both prior questions are resolved above; the one item requiring
someone else's input is the merge-time approver for the semantics change (see
"On overriding the #554 / #556 No-Go").
