---
status: Ready
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
   `Meta` in the test module. Four distinct branches are unverified:
   - `Meta.order_by` naming the sorted field **descending** supplies direction
     *and* keeps the **Redis-side** bound (`query.py:2445`);
   - the same **ascending** (weak discriminator — see spike-3);
   - `Meta.order_by` naming a **different** field **declines** the pushdown,
     because score order is not result order;
   - `Meta.order_by` naming the sorted field **descending** supplies direction to
     the **pre-hydration key-list slice** (`query.py:2329`) when a second indexed
     predicate has already declined the Redis-side bound. This branch is reached
     by no other shape, and dropping it returns **wrong rows silently** — see
     spike-6, added in the 2026-09-04 revision pass in response to critique C2.

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

**Desired outcome:** roughly 140 lines added to
`tests/test_sorted_range_pushdown.py` pinning these behaviors as **hard
assertions** — no `xfail`, no marker, nothing deferred. No file outside `tests/`
changes.

> **Line-count note (critique N1):** issue #559's Deliverable still reads
> "roughly 80 lines", written on 2026-08-13 when the scope was three sync tests.
> The current figure is ~140: eight tests, three model classes and a seeding
> helper. The issue body is being rewritten in this revision pass (Open Question
> 2, resolved) and the figure reconciled there. The plan is authoritative.

## Freshness Check

**Baseline commit:** `7f057f9` — the last commit to touch `src/` or `tests/` as of
2026-09-04. `origin/main` is now **at or above** it (plan-doc and critique commits
have landed since, none of them touching code); every check below is written as
an ancestor test, not a tip equality (critique N5).
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

All four spikes were re-run on **2026-09-04** against `origin/main` `7f057f9` as a
single throwaway pytest module (`tests/test_zz_spike559.py`, deleted after the
run) in the **main checkout** `/Users/valorengels/src/popoto`, using
`.venv/bin/python`, on `POPOTO_TEST_DB=12`.

**Environment, stated per CLAUDE.md's rule:**

- `.venv/bin/python -c "import popoto; print(popoto.__file__)"` →
  `/Users/valorengels/src/popoto/src/popoto/__init__.py`, and `import src.popoto`
  resolves to the same file (the plugin's alias collapse). The checkout is on
  `main` at `7f057f9`, so the package under test **is** current main. This is the
  check that invalidated the prior plan's first spike pass.
- `POPOTO_TEST_DB=12` — DB 15 is shared by every concurrent worktree (five other
  lanes were live during this run) and DB 0 is the live agent store. DB 12 was
  swept clean afterwards (`keys '*SpikeDoc*'` → 0).
- pytest 9.1.1, redis-py via `popoto-1.8.2` plugin, Python 3.12.13,
  `asyncio: mode=Mode.STRICT`.

Raw output: `7 passed in 0.23s`.

> **The prior plan's spike-1 and spike-6 are deleted, not updated.** They measured
> the async gap that #602 closed. Their conclusions ("results match, the bound
> does not fire"; "`HydrationCounter` reports 0 on the async path") no longer
> describe main, and the tests they justified now exist. spike-5 is retained
> because its instrument finding still governs how the new tests measure.

### spike-2: Is `count()` truncated by a limit? (re-verified)
- **Assumption**: "`count()` reports the full match count when a limit is present,
  in every form a caller might write."
- **Method**: prototype
- **Finding**: **Confirmed, in all four shapes**, 60 matching records:

  | call | result |
  |---|---|
  | `SpikeDoc.query.count(room_id="r1", last_active_at__gte=0)` | `60` |
  | `qb = ...filter(...).limit(5)`; `qb.count()` | `60` |
  | `len(list(qb))` on that same builder | `5` |
  | `SpikeDoc.query.count(..., limit=5)` | `60` |
  | `await SpikeDoc.query.async_count(..., limit=5)` | `60` |

  Structurally safe by two independent routes, both re-read on `7f057f9`:
  `QueryBuilder.count` (`query.py:1814`) calls `self._query.count(**self._filters)`
  and never forwards `_limit_value`; `Query.count` (`query.py:3238`) returns
  `len(db_keys)` from `filter_for_keys_set`, which cannot bound because
  `_pushdown_allowed` is `False` outside `_execute_filter` /
  `_filter_keys_with_pushdown`.
- **Confidence**: high
- **Impact on plan**: test as specified. Assert the `QueryBuilder.limit(n).count()`
  form **and** the `Query.count(..., limit=n)` kwargs form — they reach
  `Query.count` by different routes and only one is what a caller is likely to
  write. `async_count` is confirmed but is **not** added as a test (see No-Gos):
  it does not exercise the pushdown gate, and this issue is scoped to the
  pushdown suite.

### spike-3: Does `Meta.order_by` on the sorted field set direction and keep the bound? (re-verified, now covering async)
- **Assumption**: "`Meta.order_by = '-last_active_at'` makes an unordered query
  descending *and* keeps the bound — on both the sync and the async path."
- **Method**: prototype (dedicated model classes — `Meta` is class-level)
- **Finding**: **Confirmed on both paths.** 20 records, `limit=3`, no explicit
  `order_by`:

  | path | results | hydration count |
  |---|---|---|
  | sync, `Meta = "-last_active_at"`, `limit=3` | `['m019','m018','m017']` | **22** |
  | sync, same model, **no** limit | `['m019','m018','m017', ...]` | **40** (full) |
  | sync, `Meta = "last_active_at"`, `limit=3` | `['m000','m001','m002']` | **22** |
  | **async**, `Meta = "-last_active_at"`, `limit=3` | `['m019','m018','m017']` | **11** |

  `22 = 2 x (3 + 8) = 2 x (limit + SORTED_PUSHDOWN_OVERFETCH_MARGIN)`;
  `11 = 1 x (3 + 8)`. Both the direction and the bound come from `Meta`.
- **Confidence**: high
- **Impact on plan**: three model classes needed (`-last_active_at`,
  `last_active_at`, and the other-field case in spike-4). The **ascending** case is
  a weak discriminator — with no `Meta` at all the sorted-set order is *already*
  ascending, so an ascending-`Meta` test proves "Meta did not break it" rather
  than "Meta supplied it". Assert results + bounded hydration, and say so in a
  comment. The async descending case is new coverage that #602 did not add.

### spike-4: Does `Meta.order_by` on another field block the pushdown? (re-verified, now covering async)
- **Assumption**: "`Meta.order_by = 'doc_id'` declines the bound and still returns
  complete, correctly-ordered results — on both paths."
- **Method**: prototype (dedicated model, `doc_id` deliberately anti-correlated
  with score: `doc_id` descends as `last_active_at` ascends)
- **Finding**: **Confirmed on both paths.** 20 records, `limit=3`, no explicit
  `order_by`:

  | path | results | hydration count |
  |---|---|---|
  | sync | `['z000','z001','z002']` | **40** (2 x 20 — full range) |
  | **async** | `['z000','z001','z002']` | **20** (1 x 20 — full range) |

  `['z000','z001','z002']` is the correct `doc_id`-ordered head, which is the
  *tail* of the score order. The bound correctly declined on both paths.
- **Confidence**: high
- **Impact on plan**: test as specified, on both paths. This is the one case where
  a dropped guard returns **wrong rows**, not merely short ones — it earns a
  docstring saying exactly that. Note the ordering does *not* come from the
  `kwargs["order_by"]` assignment at `query.py:3050-3055` / `3572-3578` (both are
  skipped, because `sorted_field_order` is truthy) — it comes from
  `prepare_results` falling back to `_meta.order_by`. The test asserts the
  observable result, not that mechanism.

### spike-5: What instrument actually observes the bound? (retained, re-confirmed)
- **Assumption**: "#518's `redis_spy` fixture (patching `zrange` / `zrangebyscore`)
  transfers to main."
- **Method**: code-read + prototype
- **Finding**: **It does not.** `sorted_field_mixin.py` dispatches four ways:
  bounded+desc → `zrevrangebyscore(..., start=0, num=_limit)`; bounded+asc →
  `zrangebyscore(..., start=0, num=_limit)`; and the two unbounded variants.
  **`zrange` is never called**, so #518's `zrange.assert_not_called()` would
  vacuously pass and its `zrange.call_args.kwargs["num"] == 5` would
  `AttributeError`. Two further traps: `num` is `limit + 8`, never `limit`; and a
  single sync `list(filter(...))` issues **two** `zrevrangebyscore` calls, so
  `call_count == 1` fails.
- **Confidence**: high
- **Impact on plan**: **do not port `redis_spy`.** Use the module's own
  `HydrationCounter` for sync tests and its `AsyncHydrationCounter` (added by
  #602) for the async ones. If a range-call assertion is ever wanted, #602 also
  left `RangeCallRecorder` in the file — but this plan does not need it, because
  hydration count is the property that matters to a caller.
  **Counting convention, measured:** `HydrationCounter` counts **2 per object**
  (sync pipeline issues `hgetall` twice per key); `AsyncHydrationCounter` counts
  **1 per object**. Never write an assertion that assumes one convention on both.
  **`AsyncHydrationCounter` is a *synchronous* context manager** — it defines
  `__enter__`/`__exit__` only (`tests/test_sorted_range_pushdown.py:549`). Inside
  an `async def` test it is still `with AsyncHydrationCounter() as counter:`;
  writing `async with` raises
  `TypeError: 'AsyncHydrationCounter' object does not support the asynchronous
  context manager protocol` (hit live during the spike-6 run, 2026-09-04).

### spike-6: Does any planned test reach the `Meta` resolution in `_bound_keys_before_hydration`? (new, added by the revision pass for critique C2)
- **Assumption**: "the three originally-planned `Meta` models never reach
  `query.py:2329` with the bound still live, so the `Meta`-supplied **descending**
  branch of the key-list slice is unpinned."
- **Method**: prototype (throwaway `tests/test_zz_spike559b.py`, deleted after the
  run) + guard mutation
- **Environment**: main checkout `/Users/valorengels/src/popoto` at `dc0cf0b`
  (`7f057f9` is an ancestor; no `src/` change since), `.venv/bin/python`,
  `POPOTO_TEST_DB=9`, `pytest -q -p no:randomly`. DB 9 swept afterwards
  (`redis-cli -n 9 keys '*PushdownDoc*'` → empty, `dbsize` → 0). DB 9 rather than
  the spike-2/3/4 DB 12 only because five other lanes were live.
- **Finding**: **Assumption confirmed, and the fix verified.** Giving
  `PushdownDocMetaDesc` a `bucket = popoto.IndexedField(type=str, null=True)` and
  filtering on it declines `_sorted_pushdown_args` (its `remaining` check) while
  leaving the slice live, so `state.pushdown_limit` is `None` at `query.py:2321`
  and execution reaches the `Meta` resolution at 2329. Measured, 20 records,
  `limit=3`, **no** explicit `order_by`:

  | case | results | hydration count |
  |---|---|---|
  | sync, `Meta` desc + `bucket="a"` filter | `['m019','m018','m017']` | **22** = `2 x (3 + 8)` |
  | async, same | `['m019','m018','m017']` | **11** = `1 x (3 + 8)` |
  | sync, `Meta`-other-field (C1 re-measure) | `['z000','z001','z002']` | **40** (full) |
  | async, `Meta`-other-field (C1 re-measure) | `['z000','z001','z002']` | **20** (full) |

  **Guard mutation:** deleting `or self.model_class._meta.order_by` from
  `query.py:2329` leaves `desc=False`, slices the ascending **head**, and returns
  `['m010','m009','m008']` — wrong rows, not short ones — on **both** paths. Every
  originally-planned test stayed green under that mutation; only the new
  `bucket`-filtered tests go red. `src/` was restored from a scratchpad copy and
  `git status --short` confirmed empty.
- **Confidence**: high
- **Impact on plan**: `PushdownDocMetaDesc` gains a `bucket` IndexedField, and two
  tests are added (task 3a sync, task 4a async) — bringing the total to **eight**.
  Critique C2 asked for one; the async twin is included because
  `_bound_keys_before_hydration` takes a different state route on each path
  (shared `self` vs. the `state=` snapshot #602 introduced) and the plan pairs
  every other `Meta` case sync/async. `bucket` is added **only** to
  `PushdownDocMetaDesc`; the asc and other-field models keep the minimal shape.

## Data Flow

Both bounds live on the read path between the filter call and object hydration.
Since #602 the sync and async paths are structurally parallel; the numbered steps
below apply to both unless noted.

1. **Entry**: `Model.query.filter(room_id=..., last_active_at__gte=..., limit=n)`
   (sync) or `await Model.query.async_filter(...)`.
2. **`QueryBuilder`** — accumulates `_limit_value` / `_order_by_value`.
   **`.count()` (`query.py:1814`) branches off here and forwards only
   `_filters`, never the limit.** This is the entire mechanism behind gap 1.
3. **Arming the gate**:
   - sync — `Query._execute_filter` sets `self._pushdown_allowed = _allow_pushdown`
     (`query.py:3040`), `finally`-reset at `3044`.
   - async — `Query._filter_keys_with_pushdown` (`query.py:2264`) does the same
     inside one `to_thread` hop, held under `_pushdown_lock_for_running_loop()`,
     and returns a `_PushdownState` snapshot (#602).
   `Query.count` opens neither gate, which is the second, independent reason
   `count()` cannot be bounded.
4. **`Query.filter_for_keys_set`** — per sorted field, calls
   `_sorted_pushdown_args` (`query.py:2538`). **Gap 2 lives at `query.py:2445`**:
   `order_by = kwargs.get("order_by", None) or self.model_class._meta.order_by`,
   then `if (order_by[1:] if desc else order_by) != field_name: return None, False`.
   On approval, passes `_limit = limit + MARGIN` and `_desc` into
   `field.filter_query`.
5. **`SortedFieldMixin.filter_query`** — four-way dispatch to
   `zrevrangebyscore` / `zrangebyscore`, with or without `start=0, num=`.
6. **`Query._bound_keys_before_hydration`** (`query.py:2284`; called at `3071`
   sync and `3590` async) — slices the already-intersected ordered key list when
   the Redis-side bound could not apply. **Carries the same `Meta` guard at
   `query.py:2329`**, so gap 2 has two independent code sites and one test shape
   can only cover the one its query shape reaches.
7. **Hydration** — `Query.get_many_objects` (sync) / `_async_get_many_objects`
   (async), one `HGETALL` per surviving key. **This is what the counters observe
   and the only place the optimization is visible from a test.**
8. **Short-result guard** — `_short_result_action`; a bounded read that came back
   short on a non-exhausted range logs a warning and re-reads unbounded.
9. **`prepare_results`** — applies `order_by` (falling back to `_meta.order_by`,
   `query.py:3203-3204`) and re-applies `limit` after any client-side filter.
   **This is where the `Meta.order_by = "doc_id"` case gets its result order**
   (spike-4).
10. **Output**: ordered list of model instances.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none. Test-only.
- **Coupling**: adds three model classes to
  `tests/test_sorted_range_pushdown.py`. They **must** carry the `PushdownDoc`
  name prefix so the module's existing `_flush()` glob
  (`POPOTO_REDIS_DB.keys("*PushdownDoc*")`) sweeps both their hashes and their
  sorted-set keys (`$SortF:<ClassName>:<field>:<partition>` embeds the class name).
- **Data ownership**: unchanged.
- **Reversibility**: trivial — delete the added tests.

## Appetite

**Size:** Small (smaller than the 2026-08-13 draft: #602 absorbed one of three
deliverables and pre-built both async helpers).

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 — the one open question the prior draft carried (how to split
  the async criterion) has been answered by events.
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| venv resolves to the checkout under test | `.venv/bin/python -c "import popoto; print(popoto.__file__)"` and confirm the path is this checkout's `src/` | CLAUDE.md worktree gotcha #1 — this exact mistake invalidated the prior plan's first spike pass |
| Checkout is at or above `7f057f9` | `git merge-base --is-ancestor 7f057f9 HEAD` | #602 must be present; the whole re-scope depends on it |
| Full extras installed (no ~95 deselects) | `.venv/bin/python -c "import numpy, sentence_transformers"` | CLAUDE.md gotcha #2 |
| Redis/Valkey reachable on the chosen DB | `redis-cli -n <N> ping` | Suite needs a live server |
| A private test DB is exported | `test -n "$POPOTO_TEST_DB" && test "$POPOTO_TEST_DB" != 0` | DB 15 is shared by every concurrent worktree; DB 0 is the live agent store |

If the build runs in a fresh worktree, install `.[dev,embeddings,benchmark,mcp]`
(omit `dataframe` — it breaks `test_dataframe_field.py` collection).

## Solution

### Key Elements

- **Three `Meta`-carrying model classes** in
  `tests/test_sorted_range_pushdown.py`: `PushdownDocMetaDesc`
  (`order_by = "-last_active_at"`), `PushdownDocMetaAsc`
  (`order_by = "last_active_at"`), `PushdownDocMetaOther`
  (`order_by = "doc_id"`). `Meta` is class-level, so each case needs its own
  model. Same field shape as `PushdownDoc`'s pushdown-relevant subset:
  `room_id` KeyField, `doc_id` KeyField, `last_active_at` SortedField partitioned
  on `room_id`.
- **A small seeding helper for the `Meta` models** — `_seed()` is hard-bound to
  `PushdownDoc`; the `Meta` tests need a `model`-parametrized twin (and, for the
  other-field case, `doc_id` values anti-correlated with score).
- **Six new test functions**: one `count()`, three sync `Meta` (desc / asc /
  other-field), two async `Meta` (desc / other-field).
- **No new helpers.** `HydrationCounter` and `AsyncHydrationCounter` both already
  exist in the module; `Defaults` is already imported at line 25. Nothing is
  added to the file's infrastructure.
- **No `xfail`, no `skip`, no marker.** Every assertion is hard and passes on
  `7f057f9`. This is a deliberate reversal of the 2026-08-13 draft.

### Flow

`_seed_meta(Model, n)` into one partition → issue the query under the matching
hydration counter → assert the returned `doc_id` list **exactly** → assert the
hydration count is on the correct side of the
`limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN` boundary, scaled by the
counter's per-object convention.

### Technical Approach

- **Import the margin, never hardcode it.** `Defaults` is already imported;
  express every bound as `limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`.
  The constant is an experimental tuning knob (CLAUDE.md); a literal `11` or `8`
  turns a future retune into a false regression.
- **Respect the counter conventions (spike-5).** `HydrationCounter` counts 2 per
  object, `AsyncHydrationCounter` counts 1. Assert
  `counter.count <= 2 * (limit + MARGIN)` on sync and
  `counter.count <= limit + MARGIN` on async, each paired with a
  `counter.count < POPULATION_SCALED` "it really was bounded" claim and an
  assertion message naming the guard that broke. Match #602's existing async
  assertion idiom (`5 <= counter.count <= 5 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`)
  rather than inventing a new one.
- **`count()` test:** four assertions in one function, all against the same
  60-record seed —
  (a) `PushdownDoc.query.count(room_id="r1", last_active_at__gte=0) == POPULATION`;
  (b) build one `QueryBuilder` with `.limit(5)` and assert `.count() == POPULATION`;
  (c) `len(list(qb)) == 5` **on that same builder object** — this is what proves
  the limit was live and `count()` deliberately ignored it, and without it (b) is
  vacuous;
  (d) the kwargs form `PushdownDoc.query.count(..., limit=5) == POPULATION`.
- **`Meta` desc/asc tests:** seed 20, query with `limit=3` and **no** `order_by`,
  assert the exact three-element head, assert bounded hydration.
- **`Meta` other-field tests:** seed 20 with `doc_id` order the *reverse* of score
  order, query with `limit=3` and no `order_by`, assert the `doc_id`-ordered head
  (which is the score-order **tail**), and assert the hydration count shows a
  **full unbounded read** — the bound must have declined.
- **Async `Meta` tests** carry `@pytest.mark.asyncio` (pytest-asyncio is in
  `Mode.STRICT`) and use `AsyncHydrationCounter`. They go in the async section at
  the bottom of the file, after #602's tests, not interleaved with the sync ones.
- **Do not touch existing tests.** Everything is additive.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. The added tests exercise no `try/except` in
production code; the pushdown guards are plain early returns. (The one `try/finally`
on this path — `_filter_keys_with_pushdown`'s `_pushdown_allowed` reset — is
already covered by #602's concurrency test.)

### Empty/Invalid Input Handling
Already covered by the existing `test_non_positive_int_limit_does_not_bound`
parametrization (`0`, `-1`, `None`, `True`) and `test_no_limit_reads_full_range`.
The one new input shape — `Meta.order_by` set to a non-string — is guarded at
`query.py:2447` and `2331` (`if not isinstance(order_by, str): return`), but is
**out of scope**: `Meta.order_by` is validated at model-definition time, not
query time, so constructing that case requires bypassing model validation.

### Error State Rendering
The short-result warning path (`_short_result_action`) is the "error state" here
and is already covered by four existing tests
(`test_orphan_density_past_the_margin_warns_and_still_returns_full`,
`test_margin_absorbs_light_orphan_density_without_a_re_read`,
`test_margin_absorbs_orphans_on_the_key_list_slice_too`,
`test_exhausted_range_short_on_orphans_still_warns`) plus #602's
`test_async_orphan_density_re_reads_and_returns_full`. No new coverage needed —
but the new tests must not *trip* it, so their seeded data must contain no
orphans and they need no `caplog` assertions.

## Test Impact

No existing tests affected — this work is purely additive to
`tests/test_sorted_range_pushdown.py`. The 30 currently-collected tests keep
passing unchanged; the new model classes are new names and the widened `_flush()`
coverage is a superset of the current glob.

- [ ] `tests/test_sorted_range_pushdown.py` — UPDATE (additive only): add three
  `Meta` model classes, one seeding helper, and six test functions. **Do not
  modify any existing function body, helper, or import.**

Expected collected count after the change: **36** (30 + 6).

## Rabbit Holes

- **Re-litigating the async scope.** #602 shipped both the fix and six async
  tests on 2026-09-04. Re-adding a parity test, a bound test, or (worst) the
  `xfail(strict=True)` the 2026-08-13 draft specified is duplicate work at best
  and an immediate `XPASS`-under-strict suite failure at worst. Read the async
  section of the test file before writing anything async.
- **Cherry-picking #518's tests verbatim.** They target `_resolve_range_pushdown`
  / `_range_pushdown_limit`, which do not exist on main, and spy on `zrange`,
  which is never called (spike-5). A "port" that compiles would be a test that
  asserts nothing. Rewrite against main's symbols.
- **Porting #518's `redis_spy` fixture.** Same reason. There is an entire
  afternoon available in trying to make `zrange.call_args.kwargs["num"] == 5`
  work against code that calls `zrevrangebyscore(..., num=11)` twice.
- **Tightening the counters to an exact object count.** Sync counts 2 `HGETALL`s
  per object and async counts 1; chasing an exact number is a fragile detour into
  redis-py pipeline internals. Assert boundaries, not equalities.
- **Making the ascending-`Meta` test "prove" more than it can.** With no `Meta`,
  sorted-set order is already ascending. No query shape distinguishes "Meta
  supplied ascending" from "the default was already ascending" through public
  behavior. Assert what is observable and comment the limit.
- **Testing `async_count`.** It reports `60` (spike-2) but it does not route
  through the pushdown gate, so a test for it belongs to the async-query suite,
  not this one. See No-Gos.
- **Asserting on `self._pushdown_limit` / `_sorted_field_order` internals.**
  #600 is about to move exactly that bookkeeping off shared instance state on
  the sync path. Assert results and hydration counts only, and these tests
  survive it.

## Risks

### Risk 1: The plan is built from the stale 2026-08-13 draft
**Impact:** the builder writes the deleted `xfail(strict=True)` async test, which
`XPASS`es under `strict=True` and turns the suite red — the exact failure the
/do-docs cascade warned about in issue comment `5536628727`.
**Mitigation:** the async task is **deleted from this plan**, not rewritten; the
Freshness Check names the six #602 tests that subsume it; and the Verification
table carries an explicit anti-criterion asserting the diff adds **zero** `xfail`
markers.

### Risk 2: New model classes leak state into other tests on a shared DB
**Impact:** phantom failures in unrelated modules — the failure mode CLAUDE.md
gotcha #4 describes (73–158 phantom failures observed historically).
**Mitigation:** prefix every new model `PushdownDoc*` so the autouse `clean_docs`
fixture's existing `keys("*PushdownDoc*")` glob sweeps both the model hashes and
the `$SortF:<ClassName>:...` sorted-set keys. Verify by running the new tests,
then the full module, then `redis-cli -n <DB> keys '*Pushdown*'` and expecting
empty.

### Risk 3: A metric measured in the wrong venv or the wrong DB is reported as truth
**Impact:** this already happened once during the 2026-08-13 planning pass — 12
spurious failures inherited from `bench/530-post-correction-refresh` because the
spike ran against a venv whose editable install resolved elsewhere.
**Mitigation:** the Prerequisites table's first two checks are programmatic; run
them before any pytest invocation and state the venv path, the commit, and the
DB alongside every count reported (CLAUDE.md's rule).

### Risk 4: Model-name collision at import time
**Impact:** popoto registers models globally at class-definition time; a duplicate
class name across modules raises or silently shadows.
**Mitigation:** `git grep -n "PushdownDocMeta" tests/` returns nothing today.
Re-confirm before adding.

### Risk 5: The async `Meta` tests hit the event-loop-bound async client
**Impact:** "Future attached to a different loop" — the failure the module's
`clean_docs` fixture already resets `_POPOTO_ASYNC_REDIS_DB` and
`_async_redis_lock` for.
**Mitigation:** none needed beyond placing the new async tests in the same module
so the autouse fixture covers them, which is why they must not move to a new file.

## Race Conditions

No race conditions in scope for the *new* tests. The sync path is synchronous and
single-threaded. The async path's shared-`Query` hazard was the subject of #602
and is already covered by its `test_concurrent_async_filters_do_not_clobber_each_other`;
the two new async `Meta` tests are single-coroutine and add no concurrency. The
sync-side twin of that hazard is tracked separately as **#600** and is not
addressed here.

One **test-level** hazard, unchanged from the prior draft: the module's autouse
`clean_docs` fixture flushes before and after each test, and several SDLC
pipelines share this repo. Running on DB 15 has historically produced 73–158
phantom failures. Export a private `POPOTO_TEST_DB` for every pytest invocation
in this plan. The six tests that fail by construction on a non-15 DB — five
`assert db == 15` tests in `tests/test_pytest_plugin.py` plus
`tests/test_version.py::test_version_matches_pyproject` on a stale editable
install — are **expected noise, not regressions** (see `docs/sdlc/do-sdlc.md`).

**Measured baseline**, stated with its environment per CLAUDE.md's rule:
main checkout `/Users/valorengels/src/popoto` at `7f057f9`, `.venv/bin/python`
(editable install resolving to this checkout's `src/`), `POPOTO_TEST_DB=12`,
`pytest -q -p no:randomly`, 318s:

```
5 failed, 3416 passed, 26 skipped
```

The five are exactly the `assert db == 15` set —
`test_pytest_plugin.py::TestDatabaseIsolation::test_on_test_db`,
`::test_swap_happens_before_test_modules_are_imported`,
`TestAsyncIntegration::test_async_connection_on_test_db`,
`TestSrcPopotoImportPaths::test_src_popoto_redis_db_on_test_db`,
`::test_canonical_redis_db_on_test_db`.
`test_version_matches_pyproject` **passes** here (the editable install is fresh).
Any other failure after this work is a regression. Expected after the change:
`5 failed, 3422 passed, 26 skipped`.

## No-Gos (Out of Scope)

- **Any async pushdown *implementation* work.** Shipped in #571 / PR #602.
- **Any async pushdown *parity* test.** Shipped in PR #602 (six tests). Adding
  more is duplicate coverage.
- **The `xfail(strict=True)` async bound test** from the 2026-08-13 draft. It
  would `XPASS` and fail the suite. Deliberately deleted, not deferred.
- **[SEPARATE-ISSUE #600] Moving sync pushdown bookkeeping off shared instance
  state.** Structural refactor; this plan's tests are behavioral and will cover it.
- **`async_count` coverage.** Verified correct (spike-2) but it does not route
  through the pushdown gate. If wanted, file it against the async query suite.
- **`Meta.order_by` validation edge cases** (non-string, unknown field name).
  Validated at model-definition time, not query time.
- **Anything outside `tests/`.** This issue is test-only.

## Update System

No update system changes required — test-only, with no runtime, deploy, or
dependency surface.

## Agent Integration

No agent integration required — this adds pytest functions to an existing test
module. No MCP surface, no tool wrapper, no entry point.

## Documentation

No feature documentation changes required — this plan adds no feature and changes
no public API. The behaviors being covered are already documented as part of the
#517 pushdown work and #602's `docs/async.md` note.

### Inline Documentation
- [ ] Each new test carries a docstring naming the guard it defends and what a
      dropped guard would return — **short results** (the `Meta`-on-the-sorted-field
      cases) vs. **wrong results** (the `Meta`-on-another-field cases) — matching
      the existing file's style.
- [ ] The ascending-`Meta` test comments that it is a weak discriminator
      (spike-3): with no `Meta`, sorted-set order is already ascending.
- [ ] The `count()` test comments that assertion (c) — `len(list(qb)) == 5` on the
      same builder — is what makes assertion (b) non-vacuous.
- [ ] The async `Meta` tests comment that they cover the gate #602 armed on the
      async path but did not exercise with `Meta`.

## Success Criteria

- [ ] `count()` with a limit reports the full match count, asserted for **both**
      the `QueryBuilder.limit(n).count()` and `Query.count(..., limit=n)` forms,
      alongside a `len(list(qb)) == n` assertion **on the same builder** proving
      the limit was otherwise live.
- [ ] `Meta.order_by` naming the sorted field **descending** sets direction with
      no explicit `order_by`, on the **sync** path — exact result order plus a
      bounded hydration count.
- [ ] The same, **ascending**, on the sync path, with a comment noting it is a
      weak discriminator.
- [ ] `Meta.order_by` naming the sorted field **descending** sets direction and
      keeps the bound on the **async** path — new coverage #602 did not add.
- [ ] `Meta.order_by` naming a **different** field declines the pushdown on the
      **sync** path: results complete and in `Meta` order, hydration count shows
      the full range was read.
- [ ] The same on the **async** path.
- [ ] `Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN` is used, not hardcoded — no
      bare `8` or `11` in the new assertions.
- [ ] **Zero `xfail` / `skip` markers added.** Every new assertion is hard.
- [ ] No file outside `tests/` is modified.
- [ ] `tests/test_sorted_range_pushdown.py` collects **36** and passes in full.
- [ ] Full suite: `5 failed, 3422 passed, 26 skipped` — the five being exactly
      the documented `assert db == 15` set for a non-15 `POPOTO_TEST_DB`
      (baseline on `7f057f9`: `5 failed, 3416 passed, 26 skipped`; see Race
      Conditions for the full environment statement).
- [ ] `black --check tests/test_sorted_range_pushdown.py` exits 0.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (pushdown-tests)**
  - Name: `pushdown-test-builder`
  - Role: write the six new tests, the three `Meta` model classes, and the
    seeding helper in `tests/test_sorted_range_pushdown.py`
  - Agent Type: test-engineer
  - Resume: true

- **Validator (pushdown-tests)**
  - Name: `pushdown-test-validator`
  - Role: verify each new test fails for the right reason — mutate the guard it
    defends in a scratch copy and confirm the matching test goes red
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 0. Read the current test file before writing anything
- **Task ID**: read-current
- **Depends On**: none
- **Assigned To**: `pushdown-test-builder`
- **Parallel**: false
- Confirm `git merge-base --is-ancestor 7f057f9 HEAD` succeeds — #602 must be present.
- Read the async section at the bottom of `tests/test_sorted_range_pushdown.py`
  (`AsyncHydrationCounter`, `RangeCallRecorder`, and the six `test_async_*`
  functions). **Nothing in this plan re-adds async parity or async bound
  coverage; those exist.** This step exists so the builder does not rediscover
  that by writing a duplicate.
- Confirm `Defaults` is already imported and `git grep -n "PushdownDocMeta"
  tests/` is empty.
- Record the pre-change collected count: expected **30**.

### 1. Add the count() coverage
- **Task ID**: build-count
- **Depends On**: read-current
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-2 (all four sync/async forms report the full count;
  `QueryBuilder.count` at `query.py:1814` never forwards `_limit_value`;
  `Query.count` at `query.py:3238` opens no pushdown gate)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `test_count_is_not_truncated_by_a_limit`. Seed with the existing `_seed()`
  (`POPULATION = 60`); no new model needed — `PushdownDoc` is the right subject.
- Assert `PushdownDoc.query.count(room_id="r1", last_active_at__gte=0) == POPULATION`.
- Build **one** `QueryBuilder` with `.limit(5)`; assert `.count() == POPULATION`
  **and** `len(list(qb)) == 5` on that same object.
- Assert the kwargs form `PushdownDoc.query.count(..., limit=5) == POPULATION`.
- Docstring: `count()` reports matches, not the bounded read's length; the
  `len(list(qb))` assertion is what makes the previous one non-vacuous.

### 2. Add the three Meta model classes and the seeding helper
- **Task ID**: build-meta-models
- **Depends On**: read-current
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: Architectural Impact (the `PushdownDoc*` prefix is what
  `_flush()`'s glob sweeps), Risk 4 (no name collisions today)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: false
- Define `PushdownDocMetaDesc`, `PushdownDocMetaAsc`, `PushdownDocMetaOther`,
  each with `room_id` KeyField, `doc_id` KeyField,
  `last_active_at = popoto.SortedField(type=float, partition_by="room_id")`, and
  its own `class Meta: order_by = ...` (`"-last_active_at"`, `"last_active_at"`,
  `"doc_id"` respectively).
- Add a `model`-parametrized seeding helper alongside `_seed()` — `_seed()` is
  hard-bound to `PushdownDoc` and must not be changed. The helper takes the model
  class, a count, and a flag for anti-correlated `doc_id` values (`z{n-1-i:03d}`
  descending as score ascends) used by the other-field cases.
- Keep the `PushdownDoc` prefix on all three names so `_flush()` sweeps their
  hashes and their `$SortF:<ClassName>:...` sorted-set keys.

### 3. Add the sync Meta.order_by coverage
- **Task ID**: build-meta-sync
- **Depends On**: build-meta-models
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-3 (sync desc: `['m019','m018','m017']` at 22 `HGETALL`s
  vs 40 unbounded; sync asc: `['m000','m001','m002']` at 22), spike-4 (sync
  other-field: `['z000','z001','z002']` at 40 — full read), spike-5
  (`HydrationCounter` counts 2 per object; never `zrange`)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: false
- `test_meta_order_by_on_the_sorted_field_sets_descending_direction` — seed
  `PushdownDocMetaDesc` with 20, query `room_id="r1", last_active_at__gte=0,
  limit=3` and **no** `order_by`; assert the exact head `["m019","m018","m017"]`;
  under `HydrationCounter` assert
  `counter.count <= 2 * (3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN)` and
  `counter.count < 2 * 20`.
- `test_meta_order_by_on_the_sorted_field_keeps_ascending_bounded` — same shape on
  `PushdownDocMetaAsc`, head `["m000","m001","m002"]`; comment that this is a weak
  discriminator (spike-3).
- `test_meta_order_by_on_another_field_blocks_the_pushdown` — seed
  `PushdownDocMetaOther` with anti-correlated `doc_id`s; assert the head is
  `["z000","z001","z002"]` (the `doc_id`-ordered head, i.e. the score-order
  **tail**) and that `counter.count == 2 * 20`, proving the full range was read.
  Docstring: a dropped guard here returns **wrong rows**, not short ones.
- Express every bound via `Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`; the import
  already exists.

### 4. Add the async Meta.order_by coverage
- **Task ID**: build-meta-async
- **Depends On**: build-meta-models
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-3 (async desc: `['m019','m018','m017']` at **11**
  `HGETALL`s = `1 x (3 + 8)`), spike-4 (async other-field:
  `['z000','z001','z002']` at **20** = full read), spike-5
  (`AsyncHydrationCounter` counts **1** per object — do not reuse the sync
  arithmetic), Freshness Check (#602 armed this gate but added no `Meta` coverage)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: false
- Place both functions in the async section at the **bottom** of the file, after
  #602's tests, each with `@pytest.mark.asyncio` (pytest-asyncio is `Mode.STRICT`).
- `test_async_meta_order_by_on_the_sorted_field_sets_descending_direction` — same
  seed and query as task 3's first test but through
  `await PushdownDocMetaDesc.query.async_filter(...)`; assert the same exact head
  and, under `AsyncHydrationCounter`, `counter.count <= 3 +
  Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`.
- `test_async_meta_order_by_on_another_field_blocks_the_pushdown` — the async twin
  of task 3's third test; assert `["z000","z001","z002"]` and `counter.count == 20`.
- Docstring both: they cover the gate #602 armed on the async path via the shared
  `_sorted_pushdown_args` / `_bound_keys_before_hydration` helpers but did not
  exercise with `Meta`.

### 5. Verify each test fails for the right reason
- **Task ID**: validate-guards
- **Depends On**: build-count, build-meta-sync, build-meta-async
- **Assigned To**: `pushdown-test-validator`
- **Agent Type**: validator
- **Parallel**: false
- In a **scratch, never-committed** copy of `src/popoto/models/query.py`, delete
  the `order_by`/`_meta.order_by` early return in `_sorted_pushdown_args`
  (`query.py:2445-2452`) and confirm **both** other-field tests (sync and async)
  go red.
- Do the same for the mirrored return in `_bound_keys_before_hydration`
  (`query.py:2329-2336`).
- Make `QueryBuilder.count` forward `_limit_value` and confirm
  `test_count_is_not_truncated_by_a_limit` goes red.
- Restore `src/` to pristine; `git status --short src/` must be empty **before
  the next step runs**.

### 6. Full-module and full-suite verification
- **Task ID**: validate-all
- **Depends On**: validate-guards
- **Assigned To**: `pushdown-test-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every Prerequisites check first and record its output verbatim.
- `POPOTO_TEST_DB=<n> .venv/bin/python -m pytest
  tests/test_sorted_range_pushdown.py -q` — expect all green, **36 collected,
  0 xfailed**.
- `redis-cli -n <n> keys '*Pushdown*'` — expect empty.
- `POPOTO_TEST_DB=<n> .venv/bin/python -m pytest -q` — expect only the documented
  non-15-DB expected failures.
- `git diff --name-only origin/main` — expect exactly
  `tests/test_sorted_range_pushdown.py` (plus this plan doc, already on main).
- Report every count alongside the commit, the venv path, and the DB number.

## Verification

Run from the checkout under test; `<n>` is the private `POPOTO_TEST_DB`.

| Check | Command | Expected |
|-------|---------|----------|
| #602 is present | `git merge-base --is-ancestor 7f057f9 HEAD` | exit code 0 |
| Correct package under test | `.venv/bin/python -c "import popoto; print(popoto.__file__)"` | path is this checkout's `src/popoto/__init__.py` |
| Pushdown module green | `POPOTO_TEST_DB=<n> .venv/bin/python -m pytest tests/test_sorted_range_pushdown.py -q` | exit code 0 |
| Six new tests collected | `POPOTO_TEST_DB=<n> .venv/bin/python -m pytest tests/test_sorted_range_pushdown.py --collect-only -q \| tail -1` | output contains 36 |
| Margin used, not hardcoded | `git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -c '^+.*SORTED_PUSHDOWN_OVERFETCH_MARGIN'` | output > 0 |
| Anti-criterion: no new xfail/skip | `git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -cE '^\+.*(xfail\|mark\.skip)'` | match count == 0 |
| Anti-criterion: no async parity duplicate | `git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -cE '^\+.*def test_async_(and_sync\|bounded_query\|range_read)'` | match count == 0 |
| Anti-criterion: no production change | `git diff --name-only origin/main -- src/ \| wc -l \| tr -d ' '` | output is 0 |
| Anti-criterion: no bare margin literal | `git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -cE '^\+.*(num=11\|MARGIN = 8)'` | match count == 0 |
| Meta models are flush-swept | `git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -c '^+class PushdownDocMeta'` | output is 3 |
| No leaked Redis state | `redis-cli -n <n> keys '*Pushdown*' \| wc -l \| tr -d ' '` | output is 0 |
| Format clean | `.venv/bin/python -m black --check tests/test_sorted_range_pushdown.py` | exit code 0 |

## Critique Results

**Run:** 2026-09-04 · depth FULL (3 lenses: Risk & Robustness, Scope & Value,
History & Consistency) · **Verdict: NEEDS REVISION (1 blocker)**

> Note on roster: no Agent/Task dispatch tool was available in this session, so
> the three lenses were applied directly by the critique driver rather than by
> three forked critics. Every finding below is grounded in a verified read of
> `src/popoto/models/query.py`, `src/popoto/fields/constants.py` and
> `tests/test_sorted_range_pushdown.py` at `HEAD`, not on the plan's own claims.

### Findings Summary

| Severity | Critic | Finding | Location |
|---|---|---|---|
| BLOCKER | Risk & Robustness | B1 — Every anti-criterion in the Verification table is inert: markdown-escaped pipes (`\|`) leak into the shell/ERE commands, so all four anti-criteria pass vacuously. | Verification table |
| CONCERN | History & Consistency | C1 — Tasks 3 and 4 mandate the exact-equality hydration assertions that the plan's own Rabbit Holes forbid; builder gets two contradictory instructions. | Rabbit Holes vs. Tasks 3-4 |
| CONCERN | Risk & Robustness | C2 — The `Meta`-supplied descending branch of `_bound_keys_before_hydration` (`query.py:2329`) is left unpinned; a regression there returns wrong rows silently and every planned test stays green. | Data Flow step 6; Tasks 3-4 |
| CONCERN | Risk & Robustness | C3 — Task 1 is `Parallel: true` but edits the same file as tasks 2-4; concurrent dispatch means two writers on one file. | Step by Step Tasks, task 1 |
| CONCERN | Scope & Value | C4 — Open Question 1 is unresolved, yet Success Criteria hardcode 36 collected / 3422 passed; cutting the async `Meta` tests makes both criteria false failures. | Open Questions 1 vs. Success Criteria |
| CONCERN | Risk & Robustness | C5 — The last Prerequisite fails as written: `POPOTO_TEST_DB` is unset, and `<n>` is left unbound in the leak-check rows. | Prerequisites |
| NIT | Scope & Value | N1 — Plan says "roughly 110 lines"; issue #559's Deliverable still says "roughly 80 lines". | Problem statement |
| NIT | History & Consistency | N2 — `git diff --name-only origin/main` expectation ignores this plan doc, which the critique and revision passes both modify. | Task 6 / Verification |
| NIT | Risk & Robustness | N3 — `--collect-only -q \| tail -1` is fragile; `grep -c '::'` is stable. | Verification |
| NIT | History & Consistency | N4 — Documentation section says "no feature documentation changes required" while Success Criteria carries "Documentation updated (`/do-docs`)". | Documentation vs. Success Criteria |
| NIT | History & Consistency | N5 — Baseline commit `7f057f9` is no longer the `origin/main` tip; should read "at or above". | Freshness Check |

### Blockers

**B1 — Every anti-criterion in the Verification table is inert: markdown-escaped
pipes (`\|`) leak into the shell/ERE commands.**
*Location:* Verification table (rows "no new xfail/skip", "no async parity
duplicate", "no bare margin literal", "no production change", "No leaked Redis
state").
*Finding:* The rows are written as e.g.
`grep -cE '^\+.*(xfail\|mark\.skip)'`. In ERE, `\|` matches a **literal pipe
character**, so the pattern searches for the string `xfail|mark.skip` and can
never match. Verified: a line containing `@pytest.mark.xfail(strict=True)` scores
`0` under the escaped form and `1` under the correct form. The rows using `\|` as
a *shell* pipe (`... \| wc -l \| tr -d ' '`) do not run at all — `grep` receives a
literal `|` as an argument. Risk 1's named mitigation ("the Verification table
carries an explicit anti-criterion asserting the diff adds zero `xfail` markers")
is therefore not implemented, and the plan's entire anti-regression apparatus
passes vacuously.
*Suggestion:* Fence each command so markdown does not require pipe escaping (use
a fenced code block per row, or a single "Verification commands" code block that
the table references by number), and restate the expectation as an exit code
rather than a match count.
*Implementation Note:* Inside a markdown table cell, `|` **must** be escaped, so a
piped command cannot be written correctly there — the fix is to move the command
out of the table, not to unescape it in place. The corrected forms are
`grep -cE '^\+.*(xfail|mark\.skip)'`,
`grep -cE '^\+.*def test_async_(and_sync|bounded_query|range_read)'`,
`grep -cE '^\+.*(num=11|MARGIN = 8)'`. Note `grep -c` exits **1** when the count is
0, so an "expect 0" row must be asserted as `[ "$(… | grep -cE …)" = 0 ]`, never as
"exit code 0".

### Concerns

**C1 — Tasks 3 and 4 mandate the exact-equality hydration assertions that the
plan's own Rabbit Holes forbid.**
*Location:* Rabbit Holes ("Tightening the counters to an exact object count …
Assert boundaries, not equalities") vs. Task 3 (`counter.count == 2 * 20`) and
Task 4 (`counter.count == 20`).
*Finding:* A declared Rabbit Hole appears verbatim as planned work. The builder
has two contradictory instructions and no tiebreak.
*Suggestion:* Either carve out an explicit exception in Rabbit Holes for the
"bound must have **declined**" case, or restate tasks 3/4 as lower bounds.
*Implementation Note:* The declining case genuinely needs a lower bound, not an
equality: assert `counter.count >= 2 * 20` (sync) / `counter.count >= 20` (async)
plus `counter.count > 2 * (3 + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN)`. An
equality also breaks the moment `get_many_objects`' KeyField pre-slice path is
ever reached, and it is only 40 today because `_execute_filter` leaves
`kwargs["order_by"]` unset when `_sorted_field_order` is truthy (`query.py:3049-3055`).

**C2 — The `Meta`-supplied *descending* branch of `_bound_keys_before_hydration`
(`query.py:2329`) is left unpinned, and a regression there returns wrong rows
silently.**
*Location:* Data Flow step 6; Solution Key Elements; Tasks 3-4.
*Finding:* The three planned models carry no second indexed field, so no planned
test can reach the key-list-slice path with the bound still live. In the
`Meta`-desc tests the Redis-side bound fires first, so
`_bound_keys_before_hydration` returns at `if state.pushdown_limit: return db_keys`
(`query.py:2321`) and never evaluates the `Meta` resolution at 2329. Only the
*declining* branch of 2329 is covered (by the other-field tests). Dropping
`or self.model_class._meta.order_by` from line 2329 would leave `desc=False`, slice
the **ascending** head instead of the descending tail, and return the oldest rows
— and every planned test would stay green. This is the exact "silently drop it"
failure the plan exists to prevent.
*Suggestion:* Give `PushdownDocMetaDesc` a `bucket = popoto.IndexedField(type=str,
null=True)` and add one test that filters on it so the Redis bound declines and the
key-list slice applies with `Meta`-supplied direction.
*Implementation Note:* Model the query on the existing
`test_key_list_slice_bounds_hydration_with_second_indexed_filter`
(`tests/test_sorted_range_pushdown.py:255`): a second indexed predicate blocks
`_sorted_pushdown_args` (its `remaining` check) but not
`_bound_keys_before_hydration`, which is precisely the shape that reaches 2329
with `state.pushdown_limit` still `None`.

**C3 — Task 1 is `Parallel: true` but edits the same file as tasks 2-4.**
*Location:* Step by Step Tasks, task 1 (`build-count`).
*Finding:* `build-count` and `build-meta-models` both depend only on
`read-current` and both modify `tests/test_sorted_range_pushdown.py`. Concurrent
dispatch means two writers on one file.
*Suggestion:* Set task 1 `Parallel: false`, or state that it must land before
`build-meta-models`.
*Implementation Note:* Task 1 needs no new model (it reuses `PushdownDoc` and
`_seed()`), so serializing it first is free; sequencing 1 → 2 → 3 → 4 costs
nothing and removes the hazard entirely.

**C4 — Open Question 1 is unresolved, yet the acceptance numbers already assume
its answer.**
*Location:* Open Questions 1 vs. Success Criteria ("collects **36**", "`5 failed,
3422 passed`") and Verification ("output contains 36").
*Finding:* The plan asks the reader to "confirm, or cut" the two async `Meta`
tests, but cutting them makes 36 → 34 and 3422 → 3420, turning two pinned
success criteria into false failures.
*Suggestion:* Resolve the question in the revision pass (the plan's own reasoning
supports keeping them) and delete the option, or parameterize the two counts.
*Implementation Note:* Keeping them is the defensible call: #602 armed the shared
`_sorted_pushdown_args` / `_bound_keys_before_hydration` guards on the async path
and added no `Meta` coverage, so async `Meta`-other-field is the only test that
pins `query.py:2329` on the async side. Cutting it would leave the async decline
branch entirely unguarded.

**C5 — The last Prerequisite fails as written in the current shell.**
*Location:* Prerequisites, row "A private test DB is exported".
*Finding:* `POPOTO_TEST_DB` is **unset** in this checkout's environment; the check
`test -n "$POPOTO_TEST_DB"` fails today. The other four prerequisites pass
(`popoto.__file__` → `/Users/valorengels/src/popoto/src/popoto/__init__.py`;
`numpy`/`sentence_transformers` import; `redis-cli -n 12 ping` → PONG;
`git merge-base --is-ancestor 7f057f9 HEAD` → 0).
*Suggestion:* State the concrete DB the build must export (the plan measured on
12) rather than leaving `<n>` unbound, and make exporting it the first line of
task 0.
*Implementation Note:* `POPOTO_TEST_DB` only binds the pytest plugin — it does
nothing for the ad-hoc `redis-cli -n <n> keys '*Pushdown*'` verification rows,
which take `<n>` separately. Pin one number in both places or the leak check
inspects a different DB than the suite used.

### Nits

- **N1** — Problem says "roughly 110 lines"; issue #559's Deliverable still says
  "roughly 80 lines". Harmless, but Open Question 2's issue-body rewrite should
  reconcile it.
- **N2** — Task 6 / Verification expect `git diff --name-only origin/main` to show
  only `tests/test_sorted_range_pushdown.py`, but this critique and the revision
  pass both modify this plan doc. Say "plus `docs/plans/sorted_pushdown_coverage_gaps.md`".
- **N3** — `--collect-only -q | tail -1` is fragile (pytest's last `-q` collect line
  varies by plugin set). `--collect-only -q | grep -c '::'` is stable.
- **N4** — Documentation says "no feature documentation changes required" while
  Success Criteria carries "Documentation updated (`/do-docs`)". Boilerplate
  tension; state that the docs stage is expected to be a no-op cascade.
- **N5** — "Baseline commit: `7f057f9` (`origin/main` at revision time)" is no
  longer the `origin/main` tip (three plan-doc commits have landed since). The
  ancestor check still holds; the sentence should say "at or above".

### Verified-correct (checked, no finding)

Every file:line claim in the Freshness Check re-verified at `HEAD`:
`_sorted_pushdown_args` 2405 / `Meta` resolution 2445; `_bound_keys_before_hydration`
2284 / resolution 2329; `SORTED_PUSHDOWN_OVERFETCH_MARGIN = 8` at `constants.py:358`;
`QueryBuilder.count` 1814 (`return self._query.count(**self._filters)`, no
`_limit_value`); `Query.count` 3238; `_filter_keys_with_pushdown` sets
`_pushdown_allowed` at 2277; `async_filter` 3525; slice call sites 3071 (sync) /
3590 (async); `prepare_results` `_meta.order_by` fallback 3203-3204. Test module
has **27 test functions** as stated. `git grep PushdownDocMeta tests/` is empty
(Risk 4 holds). `Meta.order_by` field-existence validation confirmed at
`base.py:460-464`, so `order_by = "doc_id"` is legal. `filter_for_keys_set`
(`query.py:2503-2507`) resets `_sorted_field_order` / `_pending_client_filters` /
`_pushdown_*` on entry, so task 1's ordering of assertions (b) and (c) on the shared
per-class `Query` singleton is safe. Task 5's guard-mutation validation is sound:
deleting either the 2445 or the 2329 `Meta` return reddens the other-field tests
by different routes.

---

## Open Questions

The 2026-08-13 draft carried three questions, all about how to split the async
criterion between #559 and #571. **All three are answered by events**: #571
shipped with its own tests on 2026-09-04, so there is no split to make and no
`xfail` to decide the strictness of. They are removed rather than answered.

Two remain, both low-stakes and neither blocking a build:

1. **Async `Meta` coverage is a scope addition.** #559 as filed asked only for
   sync `Meta.order_by` coverage, because at filing time the async path had no
   pushdown at all. Now that #602 routes async through the same
   `_sorted_pushdown_args` / `_bound_keys_before_hydration` guards, tasks 4's two
   async `Meta` tests cover a real, unpinned branch for ~20 lines. Included on
   that reasoning — confirm, or cut them to keep the issue literally as filed.
2. **Issue-body hygiene.** #559's body still carries the 2026-08-13
   "Plan-time correction" block describing the async gap as open and the `xfail`
   as the plan. It is now wrong in both halves. Preference: rewrite the block to
   point at #602, or leave it as filed with this plan's Freshness Check as the
   record of the correction? (This plan assumes **rewrite**, and the plan link in
   the body is being repointed from the unmerged branch to `main` regardless.)
