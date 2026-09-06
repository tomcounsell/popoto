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

**Size:** Medium

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 1-2 (the public-signature decision below is the one worth confirming)
- Review rounds: 1

The code change is small and local to one file. The cost is in proving it: a genuinely-failing
concurrency regression test plus an async test that pins the design choice.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 2 ping` | Tests need a server; **DB 2** for this lane |
| Worktree venv resolves to this checkout | `.venv/bin/python -c "import popoto; print(popoto.__file__)"` | CLAUDE.md worktree hazard 1 — a venv resolving elsewhere silently tests another tree |
| Full extras installed | `.venv/bin/python -c "import numpy, sentence_transformers, mcp"` | CLAUDE.md hazard 2 — `.[dev]` alone deselects ~95 tests |
| Baseline mypy environment | `.venv/bin/python --version` reports 3.12 | `scripts/mypy_ratchet.py --strict-env` refuses to compare off-baseline (hazard 5) |

Every gate in this plan runs with `POPOTO_TEST_DB=2`. Ad-hoc scripts must set
`REDIS_URL=redis://localhost:6379/2` **before** `import popoto`. **Never touch DB 0 — it is a live
agent store** (#577).

## Solution

### Key Elements

- **Two new carrier members**: `_PushdownState` gains `geo_distances: dict` (default-factory) and
  `geo_distance_unit: Optional[str]`. One carrier, not a second sibling class — the async path
  already threads this object through the exact hop the geo data has to cross.
- **A private state-taking delegate**: `_filter_for_keys_set_with_state(state, **kwargs)` holds the
  whole body of today's `filter_for_keys_set` and writes the geo results **into `state`**. The public
  `filter_for_keys_set(**kwargs)` becomes a thin wrapper that builds a throwaway carrier, delegates,
  and mirrors the result back onto `self` for compatibility.
- **Carrier-reading call sites**: `_execute_filter` and `async_filter` read `state.geo_distances` /
  `state.geo_distance_unit` instead of `self._geo_*` when attaching distances and sorting.
- **Q-object threading**: `_evaluate_filter_args` and `q.evaluate_q` take an optional carrier so geo
  distances accumulated across several `filter_for_keys_set` calls land in the caller's carrier.
- **A back-compat mirror**: `self._geo_distances` / `self._geo_distance_unit` keep being written,
  exactly as #600 kept its `_pushdown_limit` mirror unconditional. Nothing in-repo reads them after
  this change; the mirror exists so an unknown downstream reader is not broken.

### Flow

`Model.query.filter(location__latlon=...)` → `_execute_filter` creates the per-call carrier →
`_filter_keys_with_pushdown` → `_filter_for_keys_set_with_state` writes distances **into the carrier**
→ hydration → `_execute_filter` reads **the carrier** → annotated, distance-sorted rows.

### Technical Approach

**The decision: an internal delegate, not a keyword-only `state=` parameter.**

The issue offers both. Take the delegate — the `state=` keyword is not merely less tidy, it is
**silently wrong**:

`filter_for_keys_set(**kwargs)` treats every keyword as a candidate *filter field name*. A model with
a field literally named `state` is ordinary, and this repo ships one: `src/popoto/extraction/
decision_log.py:182` declares `state = StringField(default="")`, and `status` appears as a field name
in a dozen more places (`src/popoto/fields/indexed_field_mixin.py:38`,
`src/popoto/extraction/resolution_log.py:94`, and eight test models). Adding a keyword-only `state=`
parameter means `Model.query.filter_for_keys_set(state="draft")` stops filtering on the `state` field
and instead passes the string `"draft"` where a `_PushdownState` is expected. That is a silent
behavior change for a public method — the worst possible shape, since the query returns *something*
rather than raising. No `**kwargs`-collecting public method can safely grow a named parameter.

The delegate has no such hazard:

```python
def filter_for_keys_set(self, **kwargs) -> set:
    state = _PushdownState()
    keys = self._filter_for_keys_set_with_state(state, **kwargs)
    self._geo_distances = state.geo_distances      # back-compat mirror
    self._geo_distance_unit = state.geo_distance_unit
    return keys
```

`_filter_for_keys_set_with_state(self, state, **kwargs)` takes the carrier **positionally**, so no
field name can ever collide with it.

**Why per-thread storage (#600's `_PerThreadAttr`) is the wrong tool here — and must stay wrong.**

`async_filter` initializes the geo names on the **event-loop thread** (`query.py:3845-3846`), while
`filter_for_keys_set` mutates them inside the `asyncio.to_thread` **worker thread**
(`query.py:2831-2832`, `2871-2872`), and the read-back at `query.py:3922` happens back on the loop
thread. Per-thread cells would put the write and the read in different cells: the loop thread would
read an empty dict, and **every async geo query would silently return rows with no distances at all**
— a regression strictly worse than the race being fixed, and one no existing test would catch. This
is the whole reason #600 scoped these two names out instead of folding them into its own change. The
plan therefore requires a test that fails if someone later "simplifies" the carrier into
`_PerThreadAttr`, and a `## Verification` anti-criterion asserting the two geo names are not bound to
the descriptor.

**Why the existing async lock does not already cover this.** `async_filter` holds
`_pushdown_lock_for_running_loop()` around the `to_thread` hop, but the geo **reset** happens before
the lock is acquired and the **read-back** happens after hydration, far outside it. Widening that
lock to span hydration would serialize every concurrent async geo query on a model class — a
throughput regression traded for a correctness fix that the carrier gives for free. Not the approach.

**Integration points** (all in `src/popoto/models/query.py` unless noted):
- `_PushdownState` (`~89`) — two new members.
- `_snapshot_pushdown_state` (`2507`) — must NOT snapshot the geo names off `self`. Snapshotting them
  after the fact reproduces the race it is meant to fix, because the window between another thread's
  `update()` and this snapshot is exactly the hazard. The geo members are written directly by
  `_filter_for_keys_set_with_state` and are already populated in the carrier it was handed.
- `filter_for_keys_set` (`2727`) → thin wrapper; body moves to `_filter_for_keys_set_with_state`.
- `_filter_keys_with_pushdown` (`2525`) — creates the carrier before the call rather than snapshotting
  after, and passes it down. It already returns `(db_keys, state)`, so its signature is unchanged.
- `_evaluate_filter_args` (`2912`) — takes `state` (positional `q_objects, kwargs` today, so a
  keyword-only `state=None` is safe here: it does not collect `**kwargs`).
- `src/popoto/models/q.py:222` `evaluate_q(query_instance, q_obj, all_keys)` — gains an optional
  `state=None` and passes it through, so Q-object geo filters accumulate into the caller's carrier
  instead of only the mirror.
- `_execute_filter` (`3275`) and `async_filter` (`3811`) — reset onto the carrier, read from the
  carrier.
- `QueryBuilder._execute` (`1290`) calls `filter_for_keys_set` for `fuse()` scoping and never reads
  geo distances — it keeps using the public wrapper unchanged.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] No `except Exception: pass` blocks exist in the touched region of `query.py` — verify with
      `grep -n "except Exception" src/popoto/models/query.py` and state the finding in the PR rather
      than assuming.
- [ ] `_filter_keys_with_pushdown` already disarms `_pushdown_allowed` in a `finally`. The carrier
      must be created **before** the `try`, so an exception mid-query cannot leave a caller reading an
      unbound name; add a test that a `QueryException` from a bad geo filter propagates and leaves no
      partial annotation on any row.

### Empty/Invalid Input Handling
- [ ] A geo query that matches nothing must produce `state.geo_distances == {}` and no
      `_geo_distance` attribute on any row (today's behavior — `tests/test_geo_with_distances.py:225`
      and `:263` already assert the no-distance case; they must keep passing unchanged).
- [ ] A non-geo query must leave `geo_distance_unit is None` and must not sort by distance.

### Error State Rendering
- [ ] Not user-facing in the UI sense; the observable "error state" is a missing or wrong
      `_geo_distance`. Both are asserted directly by the new tests rather than inferred from row
      identity.

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
