---
status: Ready
type: bug
appetite: Small
owner: Dev (sdlc-674)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/674
last_comment_id:
revision_applied: false
---

# Forget-guard tests that never reach their guard

## Problem

Five tests in `tests/test_memory_lifecycle.py` set `lifecycle.FORGET_IDLE_SECONDS = 0.0`
and then evaluate a freshly-saved record. Forget eligibility is a **strict**
inequality — `src/popoto/recipes/memory_lifecycle.py:336`:

```python
idle = _get_idle_seconds(record)
if idle <= lifecycle.FORGET_IDLE_SECONDS:
    return False
```

A record written microseconds earlier has `idle` of essentially `0.0`, so
`0.0 <= 0.0` returns early and the record is never a forget candidate. Every
guard downstream of that return — the re-check-tier guard, the absent-key skip,
the custom-callable dispatch — is unreachable for those tests.

The tests pass. They pass because nothing runs.

**Desired outcome:** each of the five tests exercises the guard its name claims,
and fails when that guard is removed or inverted.

## Freshness Check

**Baseline commit:** `bcdfe883` (`origin/main`), worktree `.worktrees/sdlc-674` on
`session/sdlc-674` rebased onto it.
**Issue filed:** 2026-09-07T04:06:53Z.
**Disposition: Unchanged.**

- `git log --since=<filed> -- tests/test_memory_lifecycle.py src/popoto/recipes/memory_lifecycle.py`
  returns nothing. Neither file has moved since the issue was written.
- Every file:line the issue cites was re-read and is exact: source `:316` (docstring
  criterion `AND idle > FORGET_IDLE_SECONDS`) and `:336` (the implementing
  `if idle <= ...: return False`); test sites `:322`, `:447`, `:506`, `:790`, `:824`;
  explanatory comments at `:289-294` and `:871-874`.
- Cited sibling #649 is CLOSED (merged as PR #661, "Route recipes/memory_lifecycle
  through the field layer"). That PR introduced the `load_fields` three-way contract
  the re-check-tier guard depends on; it did not touch the five sites.
- No active plan in `docs/plans/` covers this area.

## Prior Art

| Ref | Relevance |
|---|---|
| #649 / PR #661 | Found the vacuity while debugging its own corrupt-tier test; filed rather than fixed because changing an existing test's expectation was outside its contract. Its `test_forget_guard_forgets_record_with_undecodable_tier` is the one forget-guard test written correctly (`-1.0`, with the comment at `:871-874` explaining why). |
| #491 / PR #495 | Introduced confidence-modulated forgetting and the tombstone path; wrote the `-1.0` comment at `:289-294`. |
| #413 / PR #429 | Introduced the single-pass `_tick_pass` and the re-check-tier guard that two of the five tests target. |
| #661 (the empty-capture defect) | Same defect class named in the issue: an assertion evaluated against no state, failing in the direction that looks like success. |

No prior fix attempted this sweep, so there is no "why previous fixes failed" to write.

### Expected-failure search

`grep -n 'xfail' tests/test_memory_lifecycle.py` returns nothing. No markers to convert.

## Spike Results

### spike-1: Are all five sites actually vacuous?

- **Assumption**: all five tests still pass when the guard they name is deleted.
- **Method**: mutation harness — patch the source to remove each guard, run the
  test that claims to pin it, restore.
- **Result**: **confirmed, 5/5.** Measured on `bcdfe883`, worktree venv with
  `.[dev,embeddings,benchmark,mcp]`, Python 3.12.14, `POPOTO_TEST_DB=4`.

| Mutation | Test | Result on unfixed tree |
|---|---|---|
| M1 — delete `if tier == "semantic": return False` from `_default_should_forget` | `test_tick_does_not_forget_semantic` | **passed** (vacuous) |
| M1 | `test_assess_semantic_not_forget_eligible` | **passed** (vacuous) |
| M2 — call `_default_should_forget` instead of `self._should_forget` | `test_custom_should_forget` | **passed** (vacuous) |
| M3 — delete the `live_tier == "semantic"` skip in `_tick_pass` | `test_forget_guard_skips_record_promoted_to_semantic` | **passed** (vacuous) |
| M4 — delete the `tier_field not in fetched` skip in `_tick_pass` | `test_forget_guard_skips_absent_key` | **passed** (vacuous) |

- **Confidence**: high. Direct measurement, not inference.
- **Impact if false**: none — it was not false. The issue's "unaudited" column for
  `:322`, `:447`, `:506` is now resolved: all three are vacuous by the same mechanism
  as the two the #649 lane confirmed.

Note the ordering subtlety for `:322` and `:506`: the semantic check sits *above*
the idle check in `_default_should_forget`, so those two tests do reach the tier
branch. They are vacuous for the adjacent reason the issue predicted — they assert a
negative, and the idle gate produces the same negative when the tier guard is gone.

### spike-2: Is the strict `>` itself the defect?

- **Assumption**: the source operator, not the tests, is wrong.
- **Method**: code-read of the constant's contract and its blast radius.
- **Result**: **no — keep `>`.** Three reasons.
  1. `FORGET_IDLE_SECONDS` is a *threshold*: "idle for longer than N seconds".
     Strict `>` is the conventional reading of a threshold and is what both the
     module docstring (`:37`) and the policy docstring (`:316`) already state.
  2. It is a live tuning knob, not an internal detail — it is in the benchmark sweep
     grid and in the env-var override map at `:411`
     (`LIFECYCLE_FORGET_IDLE_SECONDS`). Relaxing to `>=` makes a deployed `0.0`
     mean "forget anything, however recently written". That is the strictly more
     destructive reading of a plausible operator value, reachable by config.
  3. The failure being fixed here is a *test* that could not fail. Changing a
     production operator to accommodate it would trade a test defect for a data-loss
     defect.
- **Confidence**: high.
- **Impact if false**: the alternative is a one-character source change plus a
  behavior note in CHANGELOG; the test fixes below stand either way.

The real hazard is ergonomic, not logical: `0.0` *reads* as "no idle requirement" and
*means* "unreachable for a fresh record". That is what acceptance criterion 4 asks to
catch, and it is caught by guarding against the value, not by changing the operator.

## Solution

Two changes, both in `tests/`. No source change.

**1. Fix the five sites.** Each becomes `-1.0` with the one-line reason already used
at `:294` and `:874`, so the file states the trap once per site rather than twice per
file. `-1.0` is right for all five, including the three negative assertions: with
`-1.0` the record genuinely clears the idle gate and the assertion's negative then
comes from the guard under test rather than from an early return. Making the record
"genuinely idle" instead (the issue's alternative) would mean sleeping in five tests
to buy the same discrimination.

**2. Add a source-shape guard**, `tests/test_forget_idle_seconds_guard.py`, that scans
the test tree for `FORGET_IDLE_SECONDS` assigned a non-negative literal and fails,
naming the file and line. Precedent for asserting source shape rather than behavior:
`tests/test_type_checking_guard.py`, `tests/test_docs_redis_url.py`,
`tests/test_ci_workflow_redis_url.py`. Behavior cannot catch this class — a vacuous
test is indistinguishable from a passing one at runtime, which is the whole defect.

### Data Flow

Not applicable — the change is confined to test setup values plus one new
static-scan test. The runtime path (`tick()` → `_tick_pass` → `_should_forget` →
re-check-tier guard) is unmodified.

## Risks

- **A `-1.0` record becomes forget-eligible where the test did not expect it.**
  Mitigated by running the full `tests/test_memory_lifecycle.py` file, not just the
  five, and by the mutation harness re-run.
- **The new scan test is over-broad** and fires on a legitimate future `0.0`
  (for instance an integration test that deliberately sleeps). Mitigated by an
  explicit opt-out comment marker the scan honors, documented in the test's docstring.
- **Redis contention with parallel lanes.** This lane is pinned to DB 4 via
  `POPOTO_TEST_DB=4`; only `tests/test_memory_lifecycle.py` and the new file are run.

## Step by Step Tasks

1. Change `:322`, `:447`, `:506`, `:790`, `:824` from `0.0` to `-1.0`, each with the
   one-line reason comment.
2. Add `tests/test_forget_idle_seconds_guard.py` — AST/regex scan of `tests/` for a
   non-negative `FORGET_IDLE_SECONDS` assignment, with an opt-out marker.
3. Re-run the mutation harness against the fixed tests: all five must now **fail**
   under their respective mutation (M1–M4) and pass on the unmutated tree.
4. Run the full `tests/test_memory_lifecycle.py` plus the new file on DB 4.
5. `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py`.
6. CHANGELOG entry.

## Success Criteria

- All five tests pass on an unmutated tree.
- Each of the five **fails** when the guard it names is removed (M1–M4 above), proven
  by a re-run of the harness and reported with the environment it was measured in.
- `tests/test_forget_idle_seconds_guard.py` fails if any of the five is reverted
  to `0.0`.
- `tests/test_memory_lifecycle.py` is green in full.
- `ruff check src/` exits 0; `black --check src/ tests/` passes; the mypy ratchet does
  not rise (no source change, so the expectation is a flat count).

## No-Gos

- Do not change `>` to `>=` in `_default_should_forget` (spike-2).
- Do not touch the two tests that already use `-1.0` correctly (`:294`, `:874`) or the
  two at `:935`/`:989`.
- No blanket `0.0` → `-1.0` sed across the file; each site was audited individually.

## Documentation

No user-facing behavior changes, so no `docs/` page moves. CHANGELOG entry under
Fixed, naming the vacuity rather than the constant.

## Open Questions

None. Both decisions the issue left open (per-site fix shape, and whether the strict
`>` is itself wrong) were resolved by measurement in spike-1 and spike-2.
