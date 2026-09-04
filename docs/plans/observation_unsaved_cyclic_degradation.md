---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/583
last_comment_id: none
---

# #583 — Make `on_context_used` degrade on unsaved instances for CyclicDecayField models

## Problem

`ObservationProtocol.on_context_used` is documented as a fire-and-forget application-layer
hook. The module docstring presents it that way, and `_apply_acted` / `_apply_dismissed` /
`_apply_contradicted` each wrap their `ConfidenceField` and `PredictionLedgerMixin` effects in
`except (TypeError, ValueError): pass`, commented `# Graceful degradation for unsaved instances`
(two sites) or `# Graceful degradation` (three). That is an explicit, five-site contract inside
the outcome appliers: an unsaved instance in the batch is a no-op, not a raise.

The cycle-amplitude calls do not honor it. `instance.weaken_cycle(...)` in `_apply_dismissed`
and `_apply_contradicted`, and `instance.strengthen_cycle(...)` / `instance.resolve_pressure(...)`
in `_apply_acted`, are called bare. All three delegate to code that raises `TypeError` when
`not self._db_content and not self._saved_field_values`. `_apply_acted` additionally calls
`instance.touch(...)`, which raises the same way on a `DecayingSortedField`.

**Current behavior:**

```python
ObservationProtocol.on_context_used([unsaved], {key: "contradicted"})
# TypeError: Cannot adjust cycle amplitudes on an unsaved model instance. Save the model first.
```

on any model declaring a `CyclicDecayField`. `on_context_used` iterates instances in a plain
`for` loop and `_apply_outcome` is called per instance, so one unsaved member aborts the
remaining instances' effects mid-batch and leaves the internal pipeline unexecuted. A bulk
outcome reporter — which is the intended caller — loses every effect after the first bad member.

**Desired outcome:**

`on_context_used` never raises because a member was unsaved, for any outcome and any field
combination. Unsaved members are skipped; saved members in the same batch still get their full
effects. Direct calls to `weaken_cycle` / `strengthen_cycle` / `touch` / `resolve_pressure`
keep raising exactly as they do today — the degradation belongs to the protocol layer, not to
the model methods.

## Freshness Check

**Baseline commit:** `53a65b8d318fe65f47a801d7771e65a6e9f5d566` (main, 2026-09-04)
**Issue filed at:** 2026-08-16T18:14:57Z
**Disposition:** Minor drift — line numbers moved, every claim still holds, and the defect
reproduces by reading the code path on current main.

**File:line references re-verified:**

- `src/popoto/fields/observation.py:362-366` — issue's cited `weaken_cycle` in
  `_apply_contradicted` — **drifted to `observation.py:379-383`**. The call is still bare.
  The drift is from the #580 supersession docstring and logic added to `_apply_contradicted`
  by PR #582.
- "the corresponding call in `_apply_dismissed`" — **confirmed at `observation.py:319-323`**,
  also bare.
- `src/popoto/models/base.py:2296` — `Model._adjust_cycle_amplitudes` raises on unsaved —
  **drifted to `base.py:2347` (def) / `base.py:2376-2380` (the raise)**. Text unchanged:
  `"Cannot adjust cycle amplitudes on an unsaved model instance. Save the model first."`
- Graceful-degradation neighbors still present inside the appliers, with their `pass` lines at
  `observation.py:291`, `300`, `332`, `395` and `406`. A sixth, unrelated to the appliers, sits
  at `observation.py:544`.

**Additional unwrapped sites found during re-verification (not named in the issue):**

- `observation.py:275-279` — `strengthen_cycle` in `_apply_acted`, bare.
- `observation.py:280-281` — `resolve_pressure` in `_apply_acted`, bare.
  `Model.resolve_pressure` raises the same unsaved `TypeError` (`base.py:2281-2285`).
- `observation.py:267-269` — `touch` in `_apply_acted`, bare. `Model.touch` raises
  `"Cannot call touch() on an unsaved model instance."` (`base.py:2215-2217`).

So the `acted` outcome is broken for unsaved instances on *both* `CyclicDecayField` and
`DecayingSortedField` models, which widens the issue's claim that "models with only a
`DecayingSortedField` are unaffected" — that holds for `dismissed`/`contradicted` only.
This plan fixes the whole contract rather than the two calls the issue names; see
Technical Approach for why that is not scope creep.

**Cited sibling issues/PRs re-checked:**

- #580 / PR #582 — merged. Its unsaved-instance test deliberately uses a `DecayingSortedField`
  model with a docstring explaining the workaround. That workaround stays valid after this fix
  and is not touched here.

**Commits on main since the issue was filed (touching referenced files):**

- `16aa702` Agent memory production audit: contracts and P0 fixes (#594) — touched
  `base.py` (and 58 other files) but did **not** touch `src/popoto/fields/observation.py`.
- `90fc3d3` fix(#588): decide supersession membership inside SUPERSEDE_LUA (#601) — supersession
  Lua only; the bare calls are untouched.

**Active plans in `docs/plans/` overlapping this area:** none. The recent plans
(`supersession_membership_guard_in_lua.md`, `reference_resolution_m4.md`,
`agent_memory_production_audit.md`) touch supersession, references and service wiring, not the
outcome appliers' unsaved path.

## Prior Art

- **PR #582 (#580)** — added supersession-hook coverage and the first unsaved-instance test in
  this area. Succeeded, but routed around this bug rather than fixing it; that is what surfaced
  #583. Its test model choice is the workaround this plan makes unnecessary.
- **PR #594 (#593)** — production-audit contract fixes across the agent-memory surface, including
  `ConfidenceField.update_confidence` gaining pipeline support and server-side skipping of unsaved
  instances. Same theme (unsaved instances must not raise out of a batch path), different call site.
  Its resolution supports the narrow-`try`/`except` idiom used here.
- No prior attempt to fix the cycle call sites. No stale xfail exists for this behavior.

## Research

No relevant external findings — this is internal ORM contract work with no external library,
API, or ecosystem dependency. Proceeding on codebase context.

## Data Flow

1. **Entry point**: application calls `ObservationProtocol.on_context_used(instances, outcome_map)`.
2. **`on_context_used`** (`observation.py:186-202`): validates every outcome string upfront, then
   loops instances, resolving each key via `_get_instance_key` and dispatching to `_apply_outcome`.
3. **`_apply_outcome`** (`observation.py:217-256`): opens an internal pipeline when the caller
   supplied none, dispatches to the per-outcome applier, then `pipeline.execute()`.
4. **`_apply_*`**: iterates `instance._meta.fields`, calling model methods per field type. Today a
   `TypeError` from `touch` / `strengthen_cycle` / `weaken_cycle` / `resolve_pressure` escapes
   `_apply_outcome` before `execute()`, so the internal pipeline is dropped and the enclosing
   `for` loop in `on_context_used` aborts.
5. **Output**: after the fix, an unsaved instance contributes no commands, the pipeline executes
   with whatever the saved members queued, and the loop continues.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (scope is fully determined by the issue plus the freshness re-verification)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 13 PING` | Test suite target for this lane |
| Dev extras installed | `python -c "import popoto, pytest, msgpack"` | Suite must not silently deselect |

## Solution

### Key Elements

- **Guarded cycle effects**: every model-method call in `_apply_acted`, `_apply_dismissed` and
  `_apply_contradicted` that can raise on an unsaved instance is wrapped in the same
  `except (TypeError, ValueError): pass` idiom its neighbors already use.
- **Contract regression test**: `on_context_used` on an unsaved `CyclicDecayField`-bearing model
  is a no-op for `contradicted` and `dismissed`, plus an `acted` case and a saved-instance control
  proving the effects still apply.
- **Documented contract**: the unsaved-instance guarantee is stated in
  `docs/features/observation-protocol.md`, which currently never mentions it, and in the CHANGELOG.

### Flow

Application reports outcomes in bulk → `on_context_used` iterates → an unsaved member's field
effects raise internally and are swallowed → that member contributes nothing → the loop continues
→ saved members' effects execute → caller gets `None` and no exception.

### Technical Approach

- **Wrap narrowly, do not hoist.** Wrap each call site individually rather than putting one
  `try` around the whole field loop. A hoisted guard would also swallow the
  `AttributeError`/`TypeError` that `_adjust_cycle_amplitudes` raises for a nonexistent or
  wrong-typed field, and would abandon remaining fields after the first failure on a saved
  instance. Narrow wraps match the five existing neighbors exactly, so the file keeps one idiom.
- **Do not introduce an `_is_saved(instance)` helper.** It would duplicate `base.py`'s private
  `not self._db_content and not self._saved_field_values` invariant into `observation.py`, giving
  two places to update when the saved-detection rule changes. Rejected in favor of catching the
  exception the model methods already define as the signal.
- **Fix all seven unsaved-raising sites, not only the two the issue names.** `_apply_acted`'s
  `touch`, `confirm_access`, `strengthen_cycle` and `resolve_pressure` fail identically, and
  `_apply_used`'s `confirm_access` fails on every `AccessTrackerMixin` model regardless of its
  decay fields. Shipping the two named calls would leave `acted` — the most common outcome —
  and `used` raising out of the same protocol, and a caller would file the same issue again.
  The `used` and `confirm_access` sites were missed by the first draft and added at critique.
- **Keep `strengthen_cycle` and `resolve_pressure` in one `try` block** in `_apply_acted`:
  `resolve_pressure` is only reached when the strengthen succeeded, and pairing them preserves
  the existing per-field ordering.
- **Do not change any model method.** `weaken_cycle`, `strengthen_cycle`, `touch` and
  `resolve_pressure` keep raising on unsaved instances for direct callers.
  `tests/test_observation_protocol.py::test_unsaved_instance_raises` (line 651) must keep passing
  unmodified — it is the anti-regression proof that the model-level contract did not move.

Corrected line references for the builder (baseline `53a65b8`):

| Site | Location | Call | Raises on unsaved |
|---|---|---|---|
| `_apply_acted` | `observation.py:266` | `instance.touch(...)` | `Cannot call touch() on an unsaved model instance.` |
| `_apply_acted` | `observation.py:270` | `instance.confirm_access(...)` | `confirm_access() requires a saved model instance` |
| `_apply_acted` | `observation.py:275-279` | `instance.strengthen_cycle(...)` + `instance.resolve_pressure(...)` | `Cannot adjust cycle amplitudes on an unsaved model instance.` |
| `_apply_dismissed` | `observation.py:321` | `instance.weaken_cycle(...)` | `Cannot adjust cycle amplitudes on an unsaved model instance.` |
| `_apply_contradicted` | `observation.py:381` | `instance.weaken_cycle(...)` | `Cannot adjust cycle amplitudes on an unsaved model instance.` |
| `_apply_used` | `observation.py:535` | `instance.confirm_access(...)` | `confirm_access() requires a saved model instance` |

**Seven calls across four appliers.** Two sites already carry guards and are NOT to be
touched: `resolve_pressure` at `observation.py:422` (inside the auto-discharge `try`) and the
five pre-existing `ConfidenceField`/`PredictionLedgerMixin` blocks.

`discard_staged_access` (`observation.py:316`, `346`, `376`) needs **no** guard — `deferred`
degrades cleanly today, confirmed empirically. `confirm_access` ignores its `pipeline` kwarg
and evals Lua directly; that is pre-existing and out of scope, the guard wraps the call as-is.

Empirical baseline (repro on DB 13, `on_context_used([unsaved], {key: outcome})`):

| Model | acted | dismissed | contradicted | used | deferred |
|---|---|---|---|---|---|
| `AccessTrackerMixin` + `CyclicDecayField` | raise | raise | raise | raise | ok |
| `AccessTrackerMixin` + `DecayingSortedField` | raise | ok | ok | raise | ok |

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] Each new `except (TypeError, ValueError): pass` block gets coverage through the
      `on_context_used`-on-unsaved-instance tests, which assert observable behavior: the call
      returns without raising **and** no cycles/pressure/decay key was written for that member.
- [ ] The existing five swallow-blocks are unchanged and keep their current coverage.
- [ ] These are deliberate silent no-ops, not error suppression: an unsaved instance has no Redis
      key to write to, so there is nothing to log. No logger call is added, matching the neighbors.

### Empty/Invalid Input Handling

- [ ] `on_context_used([], {})` already returns early (`observation.py:186-187`); the fix does not
      change it. A regression assertion for the empty-batch case is included.
- [ ] An unsaved instance missing from `outcome_map` defaults to `deferred`, which touches no
      raising call. Covered by the mixed-batch test.
- [ ] No agent output processing is involved.

### Error State Rendering

- [ ] No user-visible rendering. The observable contract is "returns `None`, raises nothing,
      saved members still get effects" and is asserted directly.

## Test Impact

- [ ] `tests/test_observation_protocol.py::TestCycleAmplitudes::test_unsaved_instance_raises`
      (line 651) — **UNCHANGED**: must still pass. Direct `strengthen_cycle` on an unsaved
      instance still raises `TypeError`. Any edit to this test is a signal the fix went into the
      wrong layer.
- [ ] `tests/test_observation_protocol.py` — **ADD**: a new test class for the protocol-level
      unsaved contract (see Step by Step Tasks). No existing case is rewritten.
- [ ] PR #582's unsaved-instance supersession test on a `DecayingSortedField` model —
      **UNCHANGED**: still valid, still passes. Its explanatory docstring may be left as-is;
      rewriting it is out of scope.

No other existing tests are affected — the change is purely additive guarding on a path that
previously raised, so nothing that passes today can start failing.

## Step by Step Tasks

### 1. Guard the unsaved-raising call sites

- **Task ID**: build-guard
- **Depends On**: none
- **Validates**: `tests/test_observation_protocol.py`
- **Assigned To**: observation-builder
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: false
- In `src/popoto/fields/observation.py`, wrap each of the **seven** calls in the corrected-line
  table above with `try: ... except (TypeError, ValueError): pass`, carrying a comment that
  matches the neighboring convention (`# Graceful degradation for unsaved instances`).
- The two `confirm_access` sites (`_apply_acted` at 270, `_apply_used` at 535) are the ones the
  issue and the first plan draft both missed; do not skip them.
- Leave `discard_staged_access` and the already-guarded `resolve_pressure` at 422 alone.
- Keep `strengthen_cycle` and its guarded `resolve_pressure` in a single `try` block.
- Change nothing in `src/popoto/models/base.py`.
- Re-check the file for any other bare model-method call in an `_apply_*` function that can raise
  on an unsaved instance, and guard it the same way if found.

### 2. Regression tests for the protocol-level contract

- **Task ID**: build-tests
- **Depends On**: build-guard
- **Validates**: `tests/test_observation_protocol.py`
- **Assigned To**: observation-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Add a test class covering, on an unsaved instance of a `CyclicDecayField`-bearing model
  (`FullMemory` already exists at `tests/test_observation_protocol.py:40`):
  - `contradicted` → `on_context_used` returns without raising, and no cycles hash entry exists
    for the instance key.
  - `dismissed` → same assertions.
  - `acted` → same assertions, covering the `touch` / `confirm_access` / `strengthen_cycle` /
    `resolve_pressure` sites. Note `FullMemory` is `AccessTrackerMixin`, so this case only
    passes once task 1's `confirm_access` guard is in — build task 1 first.
  - `used` → same assertions, covering the `_apply_used` `confirm_access` site.
  - `deferred` → control: already degrades today and must keep degrading.
- Add the **saved-instance control**: the same three outcomes on a saved `FullMemory` still move
  cycle amplitudes in the expected direction. Without this, the guards could swallow real failures
  and the unsaved tests would still pass.
- Add a **mixed-batch** test: `[unsaved, saved]` in one `on_context_used` call — the saved member's
  effects land, proving the loop no longer aborts mid-batch.
- Add a `DecayOnlyMemory` (`tests/test_observation_protocol.py:52`) unsaved case for `acted` and
  for `used`, covering the `touch` and `_apply_used` `confirm_access` sites on a model with no
  `CyclicDecayField`.
- Add a **blast-radius control** (critique CONCERN): write a corrupt, non-msgpack payload into a
  SAVED instance's cycles hash and assert the failure stays observable — the new guards must not
  turn a `msgpack` `UnpackValueError` (a `ValueError` subclass) into a silent no-op that is
  indistinguishable from success. Assert amplitudes did not move rather than widening the caught
  tuple; the point is to pin the blast radius, not to change it.
- Add `test_no_unguarded_raising_calls`: an `ast` walk over `src/popoto/fields/observation.py`
  asserting every `instance.touch` / `confirm_access` / `strengthen_cycle` / `weaken_cycle` /
  `resolve_pressure` Call node has an enclosing `ast.Try` whose handlers catch `TypeError`. This
  replaces the first draft's character-offset grep, which black reformatting could flip and which
  could not see the `touch` / `confirm_access` / `resolve_pressure` sites at all.
- Assert `test_unsaved_instance_raises` still passes untouched.

### 3. Documentation

- **Task ID**: document-contract
- **Depends On**: build-tests
- **Validates**: `mkdocs build --strict`
- **Assigned To**: observation-documentarian
- **Agent Type**: documentarian
- **Parallel**: false
- `docs/features/observation-protocol.md` (170 lines, currently contains no mention of unsaved
  instances or degradation): state the `on_context_used` contract — unsaved members are skipped,
  never raise, and do not abort the batch — and note that the model methods themselves still raise
  for direct callers.
- `CHANGELOG.md` under `## [Unreleased]` → `### Fixed`: describe the behavior change, name the
  outcomes affected (`acted`, `dismissed`, `contradicted`, `used`), and state explicitly that direct
  `weaken_cycle`/`strengthen_cycle`/`touch`/`resolve_pressure` calls are unchanged.
- Check `docs/guides/agent-memory-quickstart.md` and `docs/guides/subconscious-memory-recipe.md`
  for any `on_context_used` description that implies or contradicts the contract; update only if
  one exists.

### 4. Final validation

- **Task ID**: validate-all
- **Depends On**: build-guard, build-tests, document-contract
- **Validates**: the Verification table
- **Assigned To**: observation-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every command in the Verification table and report pass/fail per row.
- Confirm `git diff --stat` touches only `src/popoto/fields/observation.py`,
  `tests/test_observation_protocol.py`, `docs/features/observation-protocol.md`, `CHANGELOG.md`
  and this plan document.

## Rabbit Holes

- **Redesigning the degradation contract.** Do not introduce a `strict=` flag, a warning channel,
  a collected-errors return value, or per-instance result objects. The contract is "silently skip",
  set by five existing sites; this plan makes it uniform, not different.
- **Auditing every other module for unsaved-instance handling.** The blast radius here is the
  `_apply_*` functions in `observation.py`. `ContextAssembler`, `MemoryService` and the recipes are
  out of scope.
- **Making unsaved instances work.** Buffering cycle adjustments to apply on a later `save()` is a
  feature, not this bug fix.
- **Rewriting PR #582's workaround test.** It is correct and passes either way.

## Risks

### Risk 1: The guards mask a genuine failure on a saved instance

**Impact:** A real bug in `_adjust_cycle_amplitudes` — a msgpack decode failure, a wrong-typed
field name, a `ValueError` from clamping — becomes a silent no-op instead of a raise, and cycle
effects quietly stop applying in production.
**Mitigation:** The saved-instance control test in task 2 is the direct guard: it asserts
amplitudes actually move for saved instances on all three outcomes, so a guard that swallows too
much fails the suite. Narrow per-call wraps (rather than a hoisted loop guard) keep the swallowed
surface to one call each.

### Risk 2: Scope beyond the issue's two named calls draws review friction

**Impact:** A reviewer reads the diff against the issue text and flags the three extra
`_apply_acted` wraps as unrequested.
**Mitigation:** The Freshness Check section records the re-verification that found them, with
file:line and the identical `TypeError`. The PR description must cite it. This is the same defect
class in the same function family, and the issue's own "Why it matters" argument applies unchanged
to `acted`.

### Risk 3: Test-DB contention produces phantom failures

**Impact:** A concurrent suite from another worktree on the shared test DB yields failures unrelated
to this change, per the CLAUDE.md worktree guidance.
**Mitigation:** This lane pins `POPOTO_TEST_DB=13`. Never run against DB 0 — it is the live agent
store on this machine. Any repro script must export `REDIS_URL=redis://localhost:6379/13` before
`import popoto`.

## Race Conditions

No race conditions identified. The change adds exception handling around synchronous, single-
threaded calls; it queues no new commands and alters no ordering. The one timing-adjacent
behavior change is positive: the internal pipeline in `_apply_outcome` now reaches `execute()`
on a batch containing an unsaved member, where it was previously dropped un-executed.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #580] Supersession-edge behavior on `contradicted` for `ValidityField` models.
  Merged in PR #582 and unchanged here; its unsaved-instance path already degrades via its own
  guard.
- Nothing else is deferred — every item this bug touches, including the three additional
  `_apply_acted` sites found during the freshness check, is in scope for this plan.

## Update System

No update system changes required — this is a library-internal behavior fix with no new
dependencies, config files, or migration steps. No stored key or on-disk format changes.

## Agent Integration

No agent integration required. `on_context_used` is an existing public API already reachable
through `popoto.integrations` and the MCP surface; this plan changes its failure behavior, not
its signature or its exposure. No new MCP tool and no `.mcp.json` change.

## Documentation

### Feature Documentation

- [ ] Update `docs/features/observation-protocol.md` with the unsaved-instance contract for
      `on_context_used` (the page currently does not mention it at all).
- [ ] No new feature page and no index entry — this documents existing behavior.

### External Documentation Site

- [ ] The updated page is already in the mkdocs tree; verify `mkdocs build` still passes.
- [ ] Check `docs/guides/agent-memory-quickstart.md` and `docs/guides/subconscious-memory-recipe.md`
      for contradicting descriptions; update only if present.

### Inline Documentation

- [ ] The `on_context_used` docstring gains one line stating that unsaved instances are skipped.
- [ ] Each new `except` block carries the neighbors' verbatim comment.
- [ ] `CHANGELOG.md` entry under `## [Unreleased]` → `### Fixed`.

## Success Criteria

- [ ] `on_context_used` on an unsaved instance raises nothing for **every** outcome —
      `acted`, `dismissed`, `contradicted`, `used` and `deferred` — on a `CyclicDecayField`
      model, a `DecayingSortedField` model and an `AccessTrackerMixin`-only model.
- [ ] A batch of `[unsaved, saved]` applies the saved member's effects in full.
- [ ] A saved instance's cycle amplitudes still move on all three cycle outcomes (control test).
- [ ] A corrupt cycles payload on a SAVED instance does not silently pass as success
      (blast-radius control).
- [ ] `test_unsaved_instance_raises` passes unmodified — direct model-method calls still raise.
- [ ] `docs/features/observation-protocol.md` states the contract; `CHANGELOG.md` records it.
- [ ] Tests pass (`/do-test`), docs updated (`/do-docs`).
- [ ] No xfail is introduced and none is left stale.

## Team Orchestration

The lead deploys team members and coordinates; it does not build directly.

### Team Members

- **Builder (observation guards)**
  - Name: `observation-builder`
  - Role: wrap the four unsaved-raising call sites in `observation.py`
  - Agent Type: builder
  - Resume: true

- **Test engineer (contract regression)**
  - Name: `observation-tester`
  - Role: protocol-level unsaved tests plus the saved-instance control and mixed-batch case
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `observation-documentarian`
  - Role: feature page contract statement, CHANGELOG entry, guide check
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `observation-validator`
  - Role: run the Verification table, confirm the diff's file list
  - Agent Type: validator
  - Resume: true

## Verification

Baseline for the grep rows at `3b1e9e4`: `grep -c "Graceful degradation for unsaved instances"`
on `src/popoto/fields/observation.py` returns **2** today. Seven new guards, each carrying that
comment, must take it to **9**.

| Check | Command | Expected |
|-------|---------|----------|
| Observation suite passes | `POPOTO_TEST_DB=13 python -m pytest tests/test_observation_protocol.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=13 python -m pytest -q` | exit code 0 |
| Model-level raise preserved | `POPOTO_TEST_DB=13 python -m pytest tests/test_observation_protocol.py -q -k test_unsaved_instance_raises` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Guards present at all seven new sites | `grep -c "Graceful degradation for unsaved instances" src/popoto/fields/observation.py` | output == 9 |
| No unguarded raising call left (AST, not a char-offset grep) | `POPOTO_TEST_DB=13 python -m pytest tests/test_observation_protocol.py -q -k test_no_unguarded_raising_calls` | exit code 0 |
| Every outcome degrades on an unsaved instance | `POPOTO_TEST_DB=13 python -m pytest tests/test_observation_protocol.py -q -k unsaved` | exit code 0 |
| Model methods unchanged | `git diff --name-only main -- src/popoto/models/base.py` | output does not contain base.py |
| No stale xfails added | `git diff main -- tests/test_observation_protocol.py \| grep '^+' \| grep -c xfail` | match count == 0 |
| Docs contract stated | `grep -c "unsaved" docs/features/observation-protocol.md` | output > 0 |
| CHANGELOG names the `used` outcome | `grep -c "used" CHANGELOG.md` | output > 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

Round 1 (2026-09-04, FULL depth: Risk & Robustness, Scope & Value, History & Consistency +
structural pass). Verdict: **NEEDS REVISION** (2 blockers, 2 concerns, 2 nits).

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | structural + dev repro | The four-site table misses `instance.confirm_access(pipeline=pipeline)` at `observation.py:270` (`_apply_acted`) and `observation.py:535` (`_apply_used`). `AccessTrackerMixin.confirm_access` (`access_tracker.py:189-224`) raises `TypeError("confirm_access() requires a saved model instance")` in three places, one of them a `POPOTO_REDIS_DB.exists(redis_key)` check. `_apply_used` is an applier and `used` is a valid outcome, so Success Criterion "never raises ... for any outcome" is unmet. Empirical repro on DB 13: `used` raises on BOTH a CyclicDecayField model and a DecayingSortedField model; `acted` raises on any AccessTrackerMixin model regardless of cycle fields. | task 1 (site table), task 2 (test matrix), task 3 (docs/CHANGELOG must name `used`) | Guard all SEVEN sites, not four: `touch` (266), `confirm_access` (270), `strengthen_cycle` (275), `resolve_pressure` (279), `weaken_cycle` (321), `weaken_cycle` (381), `confirm_access` (535). `resolve_pressure` at 422 is already inside a `try`. `discard_staged_access` (316/346/376) needs NO guard — `deferred` degrades cleanly today, confirmed empirically. Note `confirm_access` ignores its `pipeline` kwarg and evals Lua directly, so its guard is `try: instance.confirm_access(pipeline=pipeline) except (TypeError, ValueError): pass` at both sites. |
| BLOCKER | structural | Task 2's stated test — unsaved `FullMemory` + `acted` — fails against the plan's own four-site fix. `FullMemory` (`tests/test_observation_protocol.py:40`) is `AccessTrackerMixin, popoto.Model`, so `_apply_acted` reaches the unguarded `confirm_access` at line 270 before any cycle call. The plan's fix list and its test list contradict each other. | task 1 then task 2 | Fix task 1 first (add the `confirm_access` guards); only then does the `acted`-on-unsaved-`FullMemory` assertion pass. Add an AccessTrackerMixin-only unsaved case and `used` cases for both `FullMemory` and `DecayOnlyMemory` (`tests/test_observation_protocol.py:52`). |
| BLOCKER | Risk & Robustness, Scope & Value, History & Consistency (3/3) | The Verification row "No bare weaken_cycle left" greps only `instance\.(weaken_cycle\|strengthen_cycle)\(`, so it reports `0` (pass) even if `touch`, `resolve_pressure` or `confirm_access` are left unguarded — the row cannot prove what the Technical Approach commits to. It is also a character-offset heuristic (`'try:' not in s[m.start()-260:m.start()]`) that black reformatting or an unrelated nearby `try:` can flip either way. | Verification table | Either delete the row and rely on the behavioral tests from task 2 (which assert "returns without raising" per site), or replace the alternation with `instance\.(touch\|weaken_cycle\|strengthen_cycle\|resolve_pressure\|confirm_access)\(` AND drop the 260-char window in favour of an AST walk that checks each Call node has an enclosing `ast.Try` handler catching `TypeError`. |
| CONCERN | Risk & Robustness | The new `except (TypeError, ValueError): pass` also swallows genuine failures on SAVED instances: `_adjust_cycle_amplitudes` calls `msgpack.unpackb` on the cycles hash after the unsaved check passes, and `msgpack.exceptions.UnpackValueError` subclasses `ValueError`. Risk 1's mitigation (a saved-instance control on valid data) exercises only happy paths and cannot catch this class. | Risks / task 2 | Add one control test that writes a corrupt (non-msgpack) payload into the cycles hash for a SAVED instance and asserts the failure is still observable — either it raises, or the test asserts amplitudes did not move and a DEBUG log fired. Do not widen the caught tuple; the point is a test that pins the blast radius. |
| CONCERN | History & Consistency | Freshness Check states commit `16aa702` (#594) "touched `observation.py` and `base.py`". `git diff 16aa702^..16aa702 --name-only` shows it touched `base.py` (and 58 other files) but NOT `src/popoto/fields/observation.py`; only `90fc3d3` (#601) touched `observation.py` in the post-issue window. The conclusion is still right, the cited evidence is wrong. | Freshness Check | Correct the bullet to "touched `base.py`; did not touch `observation.py`" — a reviewer who re-runs the cited command sees a mismatch and discounts the whole Freshness Check. |
| NIT | Scope & Value | Task 1 dictates the comment string verbatim rather than "match the neighbouring convention". | task 1 | n/a |
| NIT | structural | Tasks 3 and 4 carry no `Validates:` field, unlike tasks 1 and 2. | Step by Step Tasks | n/a |

---

## Open Questions

None blocking. Two decisions were made in-plan rather than escalated, recorded here so a reviewer
can overturn them cheaply:

1. **Fix all four sites vs. only the two the issue names.** Decided: all four. The three
   `_apply_acted` sites raise the identical `TypeError` from the same protocol entry point, and
   `acted` is the most common outcome. Overturning this narrows task 1 and drops the `acted`
   cases from task 2.
2. **`try`/`except` per call vs. an `_is_saved(instance)` helper.** Decided: `try`/`except`, to
   match the five existing neighbors and to avoid duplicating `base.py`'s private saved-detection
   invariant into `observation.py`.
