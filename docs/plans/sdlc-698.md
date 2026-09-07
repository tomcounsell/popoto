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

_(placeholder)_

## Data Flow

_(placeholder)_

## Why Previous Fixes Failed

_(placeholder)_

## Architectural Impact

_(placeholder)_

## Appetite

_(placeholder)_

## Prerequisites

_(placeholder)_

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
