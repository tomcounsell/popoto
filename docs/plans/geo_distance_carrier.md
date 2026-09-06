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

- [ ] `tests/test_geo_with_distances.py` (all 10 tests) — UPDATE only if they break. They are
      single-threaded and read `_geo_distance` off returned rows, which is unchanged public surface,
      so the expectation is **zero edits**. They are the compatibility gate: if any of them needs a
      change, the wrapper's back-compat mirror is wrong.
- [ ] `tests/test_query_thread_safety.py` — UPDATE: add the new geo tests to this file rather than
      creating a parallel one, so the concurrency suite stays in one place. Existing tests in it must
      not change.
- [ ] `tests/test_validity_field.py` — no change expected, but it is the one place that assigns to
      the per-thread bookkeeping white-box (`_pending_client_filters`), so it is the canary if the
      descriptor is touched by accident. Run it explicitly.

No other existing tests are affected: nothing else in `tests/` references `_geo_distances` (the
plural, instance-level name) — only `_geo_distance` on returned rows, which is preserved.

## Rabbit Holes

- **Making the geo path async-native.** `filter_for_keys_set` runs sync Redis inside `to_thread`
  because field `filter_query` implementations are sync. Rewriting `GeoField.filter_query` for async
  is a real project and has nothing to do with this race. Leave the `to_thread` hop exactly as is.
- **Widening `_pushdown_lock_for_running_loop` to cover hydration.** It would fix the async half by
  serializing concurrent geo queries per model class. That is a throughput regression bought with a
  correctness fix the carrier already provides for free.
- **Deprecating or removing `self._geo_distances` / `self._geo_distance_unit`.** They are private by
  name but they have been on a shared public object since PR #53. #600 faced the identical question
  for eight names and kept the mirror; do the same and do not relitigate it.
- **Refactoring the two near-identical geo blocks in `_execute_filter` and `async_filter` into one
  helper.** Tempting (they differ only in comments) but it widens the diff across the sync/async
  boundary and makes the base-revert proof harder to read. If it is worth doing, it is worth its own
  issue.
- **Chasing every other attribute on `Query`.** After this change the audit is: is any remaining
  `self._x` written and read across a blocking call? Answer it in the PR body as a claim with a grep
  behind it, and file anything found. Do not fix it here.

## Risks

### Risk 1: The back-compat mirror hides a missed read site
**Impact:** A call site that still reads `self._geo_distances` keeps working in single-threaded tests
and stays racy under concurrency — the fix looks complete and is not.
**Mitigation:** After the change, `grep -n "self\._geo_distance" src/popoto/models/query.py` must show
**only** the mirror writes in the public wrapper (and the `__init__` seed). Encoded as a
`## Verification` row, not left to review-by-eye.

### Risk 2: A future "simplification" moves the geo names onto `_PerThreadAttr`
**Impact:** Async geo queries silently return rows with no distances. Nothing today would catch it.
**Mitigation:** The async geo test (Task 3) asserts the distance is attached on the async path, so
per-thread cells fail it loudly. Plus a `## Verification` anti-criterion asserting neither geo name is
bound to `_PerThreadAttr`.

### Risk 3: The regression test is timing-dependent and passes on base by luck
**Impact:** The whole proof evaporates — this is the failure mode the repo's own rule about worktree
metrics exists for.
**Mitigation:** Prove the failure on base `4d9d5419` by reverting **only `src/popoto/models/query.py`**
in a separate checkout with its own venv (`PYTHONPATH` loses to the editable install — CLAUDE.md
hazard 1), and paste the verbatim failure output into the PR body. Also ship one deterministic
barrier-based test alongside the stochastic one, as #600 did, so a green suite does not depend on the
scheduler.

### Risk 4: Q-object geo queries are missed
**Impact:** `filter(Q(...), location__latlon=...)` keeps reading distances off the mirror and stays
racy — a partial fix presented as a complete one.
**Mitigation:** Thread the carrier through `_evaluate_filter_args` and `q.evaluate_q` (Task 2), and
cover the Q + geo combination in the test file. If the combination turns out to be unsupported today,
say so explicitly in the PR body with the evidence rather than leaving it unmentioned.

## Race Conditions

### Race 1: reset / mutate / read-back on shared instance state (the bug)
**Location:** `src/popoto/models/query.py` — reset `3299-3300` (sync) and `3845-3846` (async);
mutation `2831-2832` and `2871-2872`; read-back `3395-3420` (sync) and `3922-3943` (async).
**Trigger:** Two threads (or two coroutines) run a geo `filter()` on the same model class. Thread B's
reset or `update()` lands between thread A's key query and A's read-back — a window that spans at
least two blocking Redis round trips, during which the GIL is released.
**Data prerequisite:** The distances attached to a result set must be the ones produced by *that
query's* `filter_query` call.
**State prerequisite:** No other call may write the store those distances live in between the write
and the read.
**Mitigation:** The store becomes a per-call `_PushdownState` created by the caller and passed down
positionally, so there is no shared store to clobber. Not a lock: nothing is serialized.

### Race 2: snapshot-after-the-fact (the tempting wrong fix)
**Location:** `_snapshot_pushdown_state` (`2507`).
**Trigger:** Adding the geo names to the snapshot rather than writing them into the carrier directly.
The snapshot runs after `filter_for_keys_set` returns, so another thread's `update()` or reset can
land between the mutation and the snapshot.
**Data prerequisite:** none — this variant is simply unsound.
**Mitigation:** `_filter_for_keys_set_with_state` writes the geo members into the carrier at the
moment the tuple is unpacked. The plan states explicitly that `_snapshot_pushdown_state` must not
grow geo members, and a `## Verification` row asserts it.

### Race 3: async loop-thread / worker-thread split
**Location:** `async_filter` (`3845`, `3922`) vs `filter_for_keys_set` (`2831`), across the
`to_thread` boundary.
**Trigger:** Any async geo query, if the storage is per-thread.
**Data prerequisite:** The worker thread's writes must be visible to the loop thread.
**Mitigation:** The carrier is an ordinary object passed across the hop, so the worker's writes are
visible by reference. This is precisely why per-thread storage is refused here.

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG #640]` — n/a: this plan *is* #640. Nothing is deferred to another issue.
- Making `GeoField.filter_query` async-native — a separate project (see Rabbit Holes); no issue filed
  because nothing is known to be broken there, and filing a tracking-only issue to satisfy a tag would
  be exactly the laziness the tag legend forbids. It is a Rabbit Hole, not a No-Go.
- Removing the `self._geo_distances` / `self._geo_distance_unit` mirror — explicitly rejected in the
  Technical Approach, on the same reasoning #600 used for its eight names. This is a design decision
  in scope, not deferred work.

Nothing deferred — every relevant item is in scope for this plan.

## Update System

No update system changes required — this is a purely internal change to one module. No new
dependency, config file, or migration; nothing propagates to another environment.

## Agent Integration

No agent integration required. `Query.filter` and `Query.async_filter` are already the surfaces the
memory layer and MCP tools call; this change alters neither signature nor return shape. The geo
annotation (`_geo_distance` on returned rows) is unchanged, so no tool wrapper needs updating.

## Documentation

### Feature Documentation
- [ ] `docs/fields.md:1788-1800` documents `_geo_distance` / `_geo_distance_unit` on returned rows.
      Confirm the text stays true (it should — the annotation is unchanged) and add nothing if so.

### External Documentation Site
- [ ] `docs/configuration.md` **Thread Safety** — #600 added a "One remaining exception" admonition
      naming the geo annotation and pointing at #640. When this ships, that admonition must be removed
      or rewritten, and the surrounding paragraph updated to say concurrent geo queries are safe. This
      is the highest-value doc edit in the plan: leaving a stale "known broken" note is worse than
      having no note.
- [ ] `docs/multi-tenancy.md` — #600 added a concurrency paragraph cross-linking Thread Safety. Check
      whether it needs the geo case mentioned; likely not.
- [ ] `CHANGELOG.md` — a **Fixed** entry under Unreleased. Lead with the user-visible symptom (a geo
      query attaching another query's distances, or losing them entirely), note it completes the
      residual exposure #600 declared, and do **not** name an unreleased version number in prose.
- [ ] `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `filter_for_keys_set`'s docstring already explains the per-thread bookkeeping contract (added by
      #600). Extend it to say the geo results now travel in the carrier and that the instance
      attributes are a back-compat mirror.
- [ ] `_PushdownState`'s docstring covers seven fields; update it for the two new members and say why
      they must be written directly rather than snapshotted.
- [ ] A comment at the `_PerThreadAttr` binding block stating why the geo names are deliberately not
      there — the next reader's obvious "simplification" is the async-breaking one.

## Success Criteria

- [ ] Two concurrent geo `filter()` calls on one model class each attach their **own** distances —
      asserted per row, not by row identity.
- [ ] An async geo query attaches distances (the test that fails if the carrier is ever replaced by
      per-thread storage).
- [ ] The new regression test **fails on base `4d9d5419`** with only `src/popoto/models/query.py`
      reverted, verified in a separate checkout with its own venv, and the verbatim failure output is
      pasted in the PR body.
- [ ] `filter_for_keys_set(**kwargs) -> set` has its exact pre-change signature —
      `inspect.signature` comparison, not eyeballing.
- [ ] No read of `self._geo_distances` / `self._geo_distance_unit` remains outside the public
      wrapper's mirror writes and the `__init__` seed.
- [ ] Neither geo name is bound to `_PerThreadAttr` (anti-criterion).
- [ ] `tests/test_geo_with_distances.py` passes **unchanged** — the compatibility gate.
- [ ] Narrow-scope tests pass with `POPOTO_TEST_DB=2`; the full suite is run once before the PR opens.
- [ ] `ruff check src/` clean, `black --check src/ tests/` clean.
- [ ] `scripts/mypy_ratchet.py --strict-env` at or below baseline, with the environment stated
      alongside the number.
- [ ] Documentation updated (`/do-docs`), including removing #600's now-stale "One remaining
      exception" geo admonition in `docs/configuration.md`.

## Team Orchestration

### Team Members

- **Builder (carrier)**
  - Name: `geo-carrier-builder`
  - Role: The `query.py` / `q.py` change — carrier members, delegate, call sites, mirror.
  - Agent Type: builder
  - Domain: async/concurrency (see `DOMAIN_FRAMING.md`)
  - Resume: true

- **Test engineer (concurrency)**
  - Name: `geo-race-tester`
  - Role: The concurrency and async geo tests, plus the base-revert proof.
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: `geo-carrier-validator`
  - Role: Re-runs every `## Verification` row and reproduces the base-failure claim independently.
  - Agent Type: validator
  - Resume: true

Every subagent prompt must state: worktree `.worktrees/sdlc-640`, its own venv, `POPOTO_TEST_DB=2`,
never DB 0, and `REDIS_URL=redis://localhost:6379/2` before `import popoto` for any ad-hoc script.

## Step by Step Tasks

### 1. Carrier members and the state-taking delegate
- **Task ID**: build-carrier
- **Depends On**: none
- **Validates**: `tests/test_geo_with_distances.py` (must pass unchanged)
- **Assigned To**: geo-carrier-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `geo_distances` (default-factory dict) and `geo_distance_unit` to `_PushdownState`; extend its
  docstring to say these two are written directly, never snapshotted, and why.
- Move the body of `filter_for_keys_set` into `_filter_for_keys_set_with_state(self, state, **kwargs)`
  — carrier **positional**, never a keyword.
- At both tuple-unpack sites, write into `state` and keep the `self._geo_*` mirror write.
- Reduce `filter_for_keys_set` to the wrapper: build a throwaway `_PushdownState`, delegate, mirror,
  return. Signature byte-identical to before.
- Do **not** add geo members to `_snapshot_pushdown_state`.

### 2. Thread the carrier through the call sites
- **Task ID**: build-callsites
- **Depends On**: build-carrier
- **Validates**: `tests/test_geo_with_distances.py`, `tests/test_query_thread_safety.py`
- **Assigned To**: geo-carrier-builder
- **Agent Type**: builder
- **Parallel**: false
- `_filter_keys_with_pushdown`: create the carrier before the `try`, pass it to the delegate, return
  it (signature unchanged).
- `_execute_filter` and `async_filter`: reset onto the carrier and read `state.geo_distances` /
  `state.geo_distance_unit` at the attach-and-sort block.
- `_evaluate_filter_args` and `q.evaluate_q`: accept an optional carrier and pass it down so Q-object
  geo filters accumulate into the caller's carrier.
- Add the comment at the `_PerThreadAttr` binding block explaining why the geo names are not there.

### 3. Concurrency and async geo tests
- **Task ID**: test-races
- **Depends On**: build-callsites
- **Validates**: `tests/test_query_thread_safety.py`
- **Assigned To**: geo-race-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- A **stochastic** regression test in the shape of
  `test_concurrent_sync_filters_do_not_clobber_each_other`: a geo model, several threads with
  distinct query centers, a second indexed predicate, `sys.setswitchinterval(1e-6)` in try/finally,
  many iterations. Assert **the attached distance per row** against the distance computed from that
  thread's own center — never just row identity.
- A **deterministic** barrier-based two-thread test, so the suite does not depend on the scheduler.
- An **async geo test** asserting `_geo_distance` is attached after `async_filter` — the test that
  fails loudly if the carrier is ever replaced by `_PerThreadAttr`.
- A Q-object + geo case (or, if unsupported today, evidence of that recorded in the PR body).

### 4. Prove the failure on base
- **Task ID**: verify-base-failure
- **Depends On**: test-races
- **Assigned To**: geo-race-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Separate checkout of `4d9d5419` with its **own venv** (`PYTHONPATH` loses to the editable install).
- Revert only `src/popoto/models/query.py` (and `q.py` if touched), keep the new tests, run them with
  `POPOTO_TEST_DB=2`, and capture verbatim output.
- Confirm `import popoto` resolves to the *base* tree before trusting the result.

### 5. Validation sweep
- **Task ID**: validate-all
- **Depends On**: verify-base-failure
- **Assigned To**: geo-carrier-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every `## Verification` row, reproduce the base-failure claim, and run the full suite once.
- State the environment (Python, mypy, redis-py versions) alongside every number.

### 6. Documentation
- **Task ID**: document-fix
- **Depends On**: validate-all
- **Assigned To**: documentarian (via `/do-docs`)
- **Agent Type**: documentarian
- **Parallel**: false
- Remove/rewrite #600's stale geo admonition in `docs/configuration.md`; CHANGELOG **Fixed** entry;
  docstrings; `mkdocs build --strict`.
- Dispatch `/do-docs` **before** the review verdict is recorded, so the verdict's head SHA is not
  invalidated by a later docs commit.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Geo compat suite unchanged | `POPOTO_TEST_DB=2 .venv/bin/pytest tests/test_geo_with_distances.py -q` | exit code 0 |
| Concurrency suite passes | `POPOTO_TEST_DB=2 .venv/bin/pytest tests/test_query_thread_safety.py -q` | exit code 0 |
| Geo compat tests were not edited | `git diff --stat origin/main -- tests/test_geo_with_distances.py \| wc -l` | output contains 0 |
| Public signature unchanged | `.venv/bin/python -c "import inspect, popoto; print(inspect.signature(popoto.models.query.Query.filter_for_keys_set))"` | output contains `(self, **kwargs)` |
| Carrier is positional, not a keyword | `grep -c "def _filter_for_keys_set_with_state(self, state" src/popoto/models/query.py` | output contains 1 |
| No `state=` keyword on the public method | `grep -c "def filter_for_keys_set(self, \*, state" src/popoto/models/query.py` | match count == 0 |
| Anti-criterion: geo names not per-thread | `grep -c "_geo_distances.*_PerThreadAttr\|_geo_distance_unit.*_PerThreadAttr" src/popoto/models/query.py` | match count == 0 |
| Anti-criterion: no geo in the snapshot | `grep -c "geo_distance" <(sed -n '/def _snapshot_pushdown_state/,/^    def /p' src/popoto/models/query.py)` | match count == 0 |
| No stray reads of the mirror | `grep -n "self\._geo_distances" src/popoto/models/query.py \| grep -v "= state\.\|= {}" \| wc -l` | output contains 0 |
| Lint clean | `.venv/bin/ruff check src/` | exit code 0 |
| Format clean | `.venv/bin/black --check src/ tests/` | exit code 0 |
| Type ratchet | `.venv/bin/python scripts/mypy_ratchet.py --strict-env` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Stale geo admonition removed | `grep -c "One remaining exception" docs/configuration.md` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
