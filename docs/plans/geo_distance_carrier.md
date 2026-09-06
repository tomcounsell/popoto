---
status: Planning
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/640
last_comment_id:
---

# Thread the geo-distance bookkeeping through a carrier

## Problem

A geo query returns rows whose `_geo_distance` annotation belongs to a **different query**.

`Query` is instantiated once per model class (`src/popoto/models/base.py:506` — `new_class.objects =
new_class.query = Query(new_class)`), so `_geo_distances` and `_geo_distance_unit` are one dict and
one string shared by every thread and every coroutine that queries that model. The shape is
write / blocking-call / read:

- `Query.__init__` seeds them (`query.py:2208-2209`).
- `_execute_filter` resets them at the top of every call (`query.py:3299-3300`), and `async_filter`
  does the same on the event-loop thread (`query.py:3845-3846`).
- `filter_for_keys_set` mutates them mid-flight at two sites (`query.py:2831-2832` and
  `query.py:2871-2872`), with blocking Redis I/O on either side.
- `_execute_filter` reads them back after hydration to attach distances and to sort by them
  (`query.py:3395-3420`); `async_filter` reads them back at `query.py:3922-3943`.

**Current behavior:** two concurrent geo queries on one model class interleave in that window. Thread
B's reset can wipe thread A's distances (rows come back with no `_geo_distance` at all, and the
distance sort silently degrades to `float('inf')` for every row), or B's `update()` can land inside
A's window so A attaches B's distances to A's rows. The rows themselves are correct — only the
annotation is wrong, which makes this quieter than #600's cross-partition read and much easier to
ship without noticing.

**Desired outcome:** the distances a geo query attaches are the ones its own key query produced,
under concurrency, on both the sync and the async path — with `filter_for_keys_set`'s public
signature unchanged.

## Freshness Check

**Baseline commit:** `4d9d5419` (origin/main at plan time; the merge of PR #641 for #600)
**Issue filed at:** 2026-09-06 (filed by the #600 lane as its declared residual exposure)
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/models/base.py:506` — `new_class.objects = new_class.query = Query(new_class)` — still
  holds, exact line.
- `src/popoto/models/query.py` `self._geo_distances = {}` / `self._geo_distance_unit = None` — the
  issue described these without pinning lines. Current locations: seed at `2208-2209`, sync reset at
  `3299-3300`, async reset at `3845-3846`.
- `filter_for_keys_set` mutation sites — `2831-2832` (sorted-field loop) and `2871-2872` (remaining
  fields loop). Both still `self._geo_distances.update(distances)` + `self._geo_distance_unit = unit`.
- Read-back sites — sync `3395-3420`, async `3922-3943`. Both still read `self._geo_distances` and
  sort by the attached `_geo_distance`.

**Cited sibling issues/PRs re-checked:**
- #600 — CLOSED, merged as PR #641 at `4d9d5419`. It moved eight pushdown names onto `_PerThreadAttr`
  and threaded `_PushdownState` through the sync path; it deliberately did not touch the two geo
  names. Confirmed by reading `query.py:89-130` (`_PushdownState`, which has seven fields and no geo
  member) and `2507-2545` (`_snapshot_pushdown_state` / `_filter_keys_with_pushdown`).
- #571 — CLOSED, merged as PR #602. Introduced `_PushdownState` for the async path; its docstring at
  `query.py:90-99` is the carrier's rationale and is the precedent this plan extends.

**Commits on main since the issue was filed (touching referenced files):**
- `4d9d5419` fix(#600) — the merge the issue was filed against; it is this plan's baseline, not drift.
- No later commit touches `src/popoto/models/query.py`.

**Active plans in `docs/plans/` overlapping this area:** `sync_pushdown_state_carrier.md` (#600) is
Complete and merged; it is precedent, not overlap. No active plan touches `query.py`'s geo path.

**Notes:** The issue's claim that no existing test covers concurrent geo queries is confirmed:
`tests/test_geo_with_distances.py` is entirely single-threaded, and `tests/test_query_thread_safety.py`
(added by #600) has no geo model.

## Prior Art

- **PR #641 / issue #600** — *Move Query pushdown bookkeeping off shared instance state on the sync
  path*. Merged 2026-09-06 as `4d9d5419`. Backed eight pushdown names with `_PerThreadAttr` and
  threaded `_PushdownState` through `_execute_filter`, `Query.count` and `Query.async_count`. It
  explicitly scoped the two geo names OUT and filed this issue, because the geo names need a
  different fix. Its regression test
  (`tests/test_query_thread_safety.py::test_concurrent_sync_filters_do_not_clobber_each_other`) is the
  shape to reuse.
- **PR #602 / issue #571** — introduced the `_PushdownState` dataclass and the per-loop pushdown lock
  for the async path. This plan adds two members to that carrier rather than inventing a second one.
- **PR #53** — *Implement GeoField with_distances for geo queries*. Merged 2026-01-29. This is where
  the tuple return (`keys_set, distances, unit`) and the `self._geo_distances` stash were introduced.
  Nothing about it was concurrency-aware; the shared-instance stash dates from here.
- No closed issue matches a geo/thread/race search — this is the first time the geo path's
  concurrency has been examined.

## Research

No relevant external findings — proceeding with codebase context and training data. The change is
purely internal (no external library, API, or ecosystem pattern is involved); the two mechanisms in
play — a dataclass carrier and Python's `threading.local` — are both already in this file and were
reviewed in #571 and #600.

## Data Flow

Today (sync path, `_execute_filter`):

1. **Entry point**: `Model.query.filter(location__latlon=..., ...)` → `QueryBuilder` → `_execute_filter`.
2. **Reset**: `_execute_filter` clears `self._geo_distances` / `self._geo_distance_unit`
   (`3299-3300`) — **shared instance state, no lock**.
3. **Key query**: `_filter_keys_with_pushdown` → `filter_for_keys_set` → `GeoField.filter_query`
   returns `(keys_set, distances, unit)`; `filter_for_keys_set` folds them into
   `self._geo_distances` (`2831-2832`, `2871-2872`) — **blocking Redis I/O either side, GIL released**.
4. **Hydration**: objects are loaded from Redis — more blocking I/O.
5. **Read-back**: `_execute_filter` reads `self._geo_distances` (`3395`), attaches `_geo_distance` /
   `_geo_distance_unit` per row, and sorts by distance (`3420`).
6. **Output**: annotated, distance-sorted rows.

Steps 2→5 span two blocking Redis round trips, so any other thread's step 2 or step 3 on the same
model class lands inside the window.

The async path is the same with `to_thread` in step 3 and a per-loop lock around it
(`_pushdown_lock_for_running_loop`), but the geo reset in step 2 sits **outside** that lock and the
read-back in step 5 is after hydration, so the lock does not close this hole.

After this plan: steps 2, 3 and 5 all move onto a carrier object created per call, so the only shared
state left is a back-compat mirror nobody reads.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| PR #53 | Introduced `with_distances`, returning `(keys, distances, unit)` and stashing the distances on `self` | Written single-threaded; the stash on a per-class shared `Query` was never a safe place to leave per-call data |
| PR #602 (#571) | Added `_PushdownState` and threaded it through the async path | Scoped to the pushdown names only; the geo names were not in the carrier |
| PR #641 (#600) | Backed eight pushdown names with per-thread storage, threaded the carrier through the sync path | Deliberately excluded the geo names — per-thread storage is the wrong tool for them (see Solution), and the right tool needed a signature change it would not take on inline |

**Root cause pattern:** per-call data was parked on an object whose lifetime is the model class.
Each fix has narrowed the set of names that still are; this plan removes the last two.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: `filter_for_keys_set(**kwargs) -> set` keeps its exact public signature. One
  new private method (`_filter_for_keys_set_with_state`) and one new optional keyword on two private
  seams (`_evaluate_filter_args`, `q.evaluate_q`). `_PushdownState` gains two members.
- **Coupling**: reduced. Two more names stop being read off shared instance state; the carrier
  becomes the single way per-call bookkeeping crosses a call boundary.
- **Data ownership**: per-call geo data moves from the `Query` instance to the call's carrier. The
  instance attributes remain as a written-but-unread back-compat mirror.
- **Reversibility**: high. The change is additive — reverting the read sites to `self._geo_*` restores
  today's behavior exactly, which is also why the plan needs an anti-criterion (below) to stop a
  future "simplification" from doing that silently.

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
