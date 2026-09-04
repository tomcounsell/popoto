---
status: Planning
type: chore
appetite: Small
owner: sdlc-559
created: 2026-08-13
revised: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/559
last_comment_id: 5536628727
---

# SortedField pushdown suite: count() and Meta.order_by coverage

## Problem

`tests/test_sorted_range_pushdown.py` on `origin/main` (`7f057f9`) has **27 test
functions** (30 collected — `test_non_positive_int_limit_does_not_bound` is
parametrized x4). They cover the bounded read in both directions, range bounds,
partition isolation, non-positive-int limits, the Q-object / plain-field /
second-sorted-field blockers, the key-list slice path, five orphan-density cases,
and — since PR #602 landed on 2026-09-04 — six async cases.

Two behaviors that the production code *does* implement still have **no test at all**:

1. **`count()` must not be truncated by a `limit`.** Neither
   `QueryBuilder.limit(n).count()` nor `Query.count(..., limit=n)` may report the
   bounded read's length. Zero occurrences of `count(` in the test module.
2. **`Meta.order_by` participates in the pushdown gate.** Both
   `_sorted_pushdown_args` (`src/popoto/models/query.py:2445`) and
   `_bound_keys_before_hydration` (`query.py:2329`) resolve direction via
   `kwargs.get("order_by", None) or self.model_class._meta.order_by`, then return
   early when the resolved name is not the sorted field. Zero occurrences of
   `Meta` in the test module. Three distinct branches are unverified:
   - `Meta.order_by` naming the sorted field **descending** supplies direction
     *and* keeps the bound;
   - the same **ascending** (weak discriminator — see spike-3);
   - `Meta.order_by` naming a **different** field **declines** the pushdown,
     because score order is not result order.

A third item from the original 2026-08-13 issue — **async pushdown parity** — is
**no longer in scope**: PR #602 (issue #571, merged 2026-09-04) both implemented
the async bound and shipped six async tests for it. See Freshness Check.

**New gap surfaced by that same PR:** #602 armed the pushdown on the async path
through the *shared* `_sorted_pushdown_args` / `_bound_keys_before_hydration`
helpers, so `Meta.order_by` now gates the async read too — and #602 added no
`Meta` coverage. Measured on current main (spike-3/4): async `Meta`-descending
hydrates 11 of 20; async `Meta`-on-another-field hydrates 20 of 20 and returns
the correct `doc_id`-ordered head. Both branches work and neither is pinned.

**Current behavior:** all of the above is unverified. A refactor of
`_sorted_pushdown_args`, `_bound_keys_before_hydration`, or `QueryBuilder.count`
can silently drop any of it and the suite stays green.

**Desired outcome:** roughly 110 lines added to
`tests/test_sorted_range_pushdown.py` pinning these behaviors as **hard
assertions** — no `xfail`, no marker, nothing deferred. No file outside `tests/`
changes.

## Freshness Check

**Baseline commit:** `7f057f9` (`origin/main` at revision time, 2026-09-04).
**Previous plan revision:** 2026-08-13 on branch `test/559-pushdown-coverage`
(`a280c54`), against baseline `7ffc8e8`. That plan was never merged to `main`.
**Issue filed at:** 2026-08-13T05:26:49Z.
**Disposition:** **Major drift** — one of the plan's three deliverables was
shipped by another issue, and its central mechanism (a strict `xfail`) would now
be actively harmful. Re-scoped in-plan; the other two are Unchanged and one new
sub-case is added.

### What moved

**#571 / PR #602 shipped the async pushdown (merged 2026-09-04T06:36:32Z as
`7f057f9`).** The prior plan's spike-1 measured `async_filter` reading the full
range (60 of 60 hydrations at `limit=5`) and scoped the gap as
`@pytest.mark.xfail(strict=True, reason="#571 ...")`. That is now false in both
directions:

- `async_filter` (`query.py:3525`) is now `async def async_filter(self, *,
  _allow_pushdown: bool = True, **kwargs)`. It arms the pushdown via
  `self._filter_keys_with_pushdown` inside a single `to_thread` hop under
  `_pushdown_lock_for_running_loop()` (`query.py:3564-3567`), calls
  `_bound_keys_before_hydration(..., state=state)` (`query.py:3590-3592`), and
  ports the short-result re-read guard.
- The planned `xfail(strict=True)` would **xpass and fail the suite the moment it
  was written** (confirmed by the /do-docs cascade comment on the issue,
  `5536628727`).
- PR #602 also added the tests: `AsyncHydrationCounter` and `RangeCallRecorder`
  helpers plus six async test functions
  (`test_async_bounded_query_hydrates_only_limit`,
  `test_async_range_read_is_bounded_and_direction_correct`,
  `test_async_and_sync_agree_across_limits_and_directions`,
  `test_async_orphan_density_re_reads_and_returns_full`,
  `test_async_pending_client_filter_suppresses_the_bound`,
  `test_concurrent_async_filters_do_not_clobber_each_other`). Both of the prior
  plan's planned async tests — the parity assertion and the bound assertion —
  are subsumed. **Task "build-async" is deleted from this plan, not rewritten.**

**Consequently the prior plan's spike-1, spike-6, `AsyncHydrationCounter` key
element, Risk 1, and No-Go for #571 are all obsolete.** They are preserved in git
history on `test/559-pushdown-coverage` and are not carried forward.

### File:line references re-verified on `7f057f9`

| Prior plan said | Now | Status |
|---|---|---|
| `_sorted_pushdown_args` at `query.py:1944`, Meta resolution at `1985` | `query.py:2405`, Meta resolution at `2445` | **Drifted, claim holds** |
| `_bound_keys_before_hydration` at `query.py:1894`, resolution at `1927` | `query.py:2284`, resolution at `2329` | **Drifted, claim holds** (now takes a keyword-only `state=` snapshot; the `order_by`/Meta guard is byte-identical) |
| `SORTED_PUSHDOWN_OVERFETCH_MARGIN = 8` at `constants.py:230` | `constants.py:358`, still `8` | **Drifted, value holds** |
| `_pushdown_allowed` set only in `_execute_filter` | Also set in `_filter_keys_with_pushdown` (`query.py:2277`), which `async_filter` calls | **Changed — this is the #602 fix** |
| `QueryBuilder.count` at `query.py:1474`, never forwards `_limit_value` | `query.py:1814`, still `return self._query.count(**self._filters)` | **Drifted, claim holds** |
| `Query.count` at `query.py:2781` | `query.py:3238` | **Drifted, claim holds** |
| Suite has "21 tests" (24 collected) | **27 functions, 30 collected** | **Changed** (#602 added 6; #594 added 1) |
| `Defaults` import must be added to the test file | Already imported (`tests/test_sorted_range_pushdown.py:25`) | **Already done** |

### Cited sibling issues/PRs re-checked

- **#517** (PR, merged 2026-08-07 `bc3b267`) — the sync implementation under
  test. Still in main, refactored but not behavior-changed by #602.
- **#518** (closed, superseded) — source material for the harvested tests. Branch
  `feature/sorted-range-pushdown` (HEAD `245f98f`) still exists and is still
  readable. Its `test_async_filter_pushes_down_too` is now moot; its
  `test_count_is_not_truncated_by_a_limit`,
  `test_meta_order_by_on_the_sorted_field_sets_the_direction` and
  `test_meta_order_by_on_another_field_blocks_the_pushdown` remain the source
  material — **not** cherry-pickable, see Rabbit Holes.
- **#571** (closed 2026-09-04) / **PR #602** (merged) — see above.
- **#594** (PR, merged 2026-09-03) — client-filter limit suppression; added
  `test_key_list_slice_bounds_hydration_with_second_indexed_filter`-adjacent
  coverage and the "pending client filter suppresses the bound" invariant that
  #602 inherits. Does not touch `count()` or `Meta`.
- **#600** (OPEN) — "Move Query pushdown bookkeeping off shared instance state on
  the sync path." Adjacent: it will refactor `self._pushdown_*` on the sync path
  the way #602 did for async. The tests this plan adds are **behavioral, not
  structural** (they assert results and hydration counts, never `self._pushdown_*`),
  so they survive that refactor and in fact become useful regression cover for it.
  No coordination required, but noted.

### Commits on main touching referenced files since the issue was filed

`git log --since=2026-08-13T05:26:49Z origin/main -- src/popoto/models/query.py
src/popoto/fields/sorted_field_mixin.py tests/test_sorted_range_pushdown.py`
returns the #594 and #602 merges, both analyzed above.

### Active plans in `docs/plans/` overlapping this area

- `async_sorted_pushdown_parity.md` — #571's plan, **status Complete**, shipped.
  This plan's async scope is exactly what that one delivered. No live overlap.
- `decaying_sorted_field.md`, `computed_sort.md` — touch sorted fields, not the
  pushdown gate. No overlap.

### Bug reproduction

Not applicable — `type: chore`, additive test coverage. The behaviors being
pinned all pass today; the four spikes below are the "reproduction" and each was
measured live on `7f057f9`.

## Prior Art

- **PR #517** — "Bound SortedField queries before hydration instead of after" —
  MERGED 2026-08-07. Shipped the two-mechanism sync implementation
  (`_sorted_pushdown_args` for the Redis-side bound, `_bound_keys_before_hydration`
  for the pre-hydration slice) plus the test suite this plan extends. Code under
  test; nothing here changes it.
- **PR #602 / issue #571** — "apply the SortedField limit pushdown on the async
  path" — MERGED 2026-09-04. Extracted `_PushdownState`, `_short_result_action`
  and a per-running-loop lock, then armed the pushdown on `async_filter`. **Took
  over this issue's async deliverable entirely**, including the tests. Its plan
  (`docs/plans/async_sorted_pushdown_parity.md`) is the record.
- **PR #594** — "Agent memory production audit: contracts and P0 fixes" — MERGED
  2026-09-03. Introduced the pending-client-filter limit suppression that both
  paths now share. Unrelated to `count()` / `Meta`.
- **PR #518** — "perf(query): conditional limit pushdown into SortedField range
  reads" — CLOSED as superseded (trial rebase onto main resolved to a zero-line
  production diff). Its test file carries the three surviving source tests. They
  target `_resolve_range_pushdown` / `_range_pushdown_limit`, which do not exist
  on main.
- No prior attempt has landed these specific tests on main. This is additive
  coverage, not a repeated fix — so there is no **Why Previous Fixes Failed**
  section.

## Research

No relevant external findings — proceeding with codebase context. The work is
purely internal (pytest, an in-repo ORM, a Redis/Valkey connection already under
test). The one ecosystem-adjacent fact was confirmed locally rather than by
search: pytest-asyncio runs in `Mode.STRICT` here (visible in the pytest header
of the spike run below), so any new async test needs an explicit
`@pytest.mark.asyncio` marker — which `tests/test_sorted_range_pushdown.py`
already demonstrates six times since #602.

## Spike Results

<!-- skeleton -->

## Data Flow

<!-- skeleton -->

## Architectural Impact

<!-- skeleton -->

## Appetite

<!-- skeleton -->

## Prerequisites

<!-- skeleton -->

## Solution

<!-- skeleton -->

## Failure Path Test Strategy

<!-- skeleton -->

## Test Impact

<!-- skeleton -->

## Rabbit Holes

<!-- skeleton -->

## Risks

<!-- skeleton -->

## Race Conditions

<!-- skeleton -->

## No-Gos (Out of Scope)

<!-- skeleton -->

## Update System

<!-- skeleton -->

## Agent Integration

<!-- skeleton -->

## Documentation

<!-- skeleton -->

## Success Criteria

<!-- skeleton -->

## Team Orchestration

<!-- skeleton -->

## Step by Step Tasks

<!-- skeleton -->

## Verification

<!-- skeleton -->

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

<!-- skeleton -->
