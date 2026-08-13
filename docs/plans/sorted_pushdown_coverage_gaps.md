---
status: Planning
type: chore
appetite: Small
owner: sdlc-518
created: 2026-08-13
tracking: https://github.com/tomcounsell/popoto/issues/559
last_comment_id: none
---

# SortedField pushdown suite: async parity, count(), and Meta.order_by coverage

## Problem

`tests/test_sorted_range_pushdown.py` has 21 test functions (24 collected, one is
parametrized x4) covering the bounded read, range bounds, partition isolation,
non-positive-int limits, the Q-object / plain-field / second-sorted-field blockers,
the key-list slice path, and five orphan-density cases. Three behaviors that the
production code *does* implement have no test at all:

1. `count()` must not be truncated by a `limit`.
2. `Meta.order_by` naming the sorted field supplies the direction when the query
   passes no explicit `order_by`.
3. `Meta.order_by` naming a *different* field declines the pushdown, because score
   order is not result order.

A fourth — async pushdown parity — turns out **not** to be implemented (see
Freshness Check). The async path returns correct rows but reads the full range.

**Current behavior:** all four behaviors are unverified. A refactor of
`_sorted_pushdown_args` or `_bound_keys_before_hydration` can silently drop the
three that work, and nothing records that the fourth is missing.

**Desired outcome:** roughly 80 lines added to `tests/test_sorted_range_pushdown.py`
pinning the three working behaviors as hard assertions, plus one `xfail(strict=True)`
test pinning the async gap to issue #571 so it flips green the day that gap is closed.
No file outside `tests/` changes.

## Freshness Check

**Baseline commit:** `7ffc8e8` (identical to `origin/main` at plan time)
**Issue filed at:** 2026-08-13T05:26:49Z
**Disposition:** **Major drift** — one of the issue's four premises is false. Resolved
in-plan by re-scoping that one criterion and filing #571; the other three are Unchanged.

**File:line references re-verified:**

- `src/popoto/models/query.py:1944` `_sorted_pushdown_args` — still present, still resolves
  direction via `kwargs.get("order_by", None) or self.model_class._meta.order_by` (line 1985)
  and returns `(None, False)` when the resolved name is not this field. **Holds.**
- `src/popoto/models/query.py:1894` `_bound_keys_before_hydration` — same `order_by`/Meta
  resolution at line 1927, same early return. **Holds.**
- `src/popoto/fields/constants.py:230` `SORTED_PUSHDOWN_OVERFETCH_MARGIN = 8`. **Holds**
  (measured live: a `limit=5` descending query issues `num=13`).
- Issue says the suite has "21 tests". `pytest --collect-only` reports **24 collected**
  from 21 test functions — `test_non_positive_int_limit_does_not_bound` is parametrized
  over 4 values. Cosmetic; no impact.

**The premise that failed:**

> "The production code already implements all four." — issue #559

It implements three. `Query.async_filter` (`query.py:3051`) applies **neither** bound:

- `_sorted_pushdown_args` gates on `getattr(self, "_pushdown_allowed", False)`
  (`query.py:1975`). `_pushdown_allowed` is set `True` in exactly one place,
  `_execute_filter` (`query.py:2555`), and reset `False` in that method's `finally`
  (`query.py:2557`). `async_filter` calls `await to_thread(self.filter_for_keys_set, ...)`
  directly (`query.py:3081`), so the attribute is `False` and the Redis-side bound never fires.
- `_bound_keys_before_hydration` is called only from `_execute_filter` (`query.py:2586`).
  `async_filter` never calls it, so the pre-hydration slice never fires either.

Measured, not inferred (see Spike Results). Filed as **#571**. Acceptance criterion 1 of
#559 ("the bound actually fires") is therefore unsatisfiable under a test-only change and
is re-scoped below.

**Cited sibling issues/PRs re-checked:**

- **#517** — "Bound SortedField queries before hydration instead of after" — MERGED
  2026-08-07T08:25:54Z as `bc3b267`. This is the implementation under test. Still in main.
- **#518** — "perf(query): conditional limit pushdown into SortedField range reads" — CLOSED
  2026-08-13T07:01:17Z, superseded. Branch `feature/sorted-range-pushdown` (HEAD `245f98f`)
  still exists and still carries the four source tests. Confirmed readable via
  `git show 245f98f:tests/test_sorted_range_pushdown.py`.

**Commits on main since issue was filed (touching referenced files):** none.
`git log --since=<issue createdAt> origin/main -- src/popoto/models/query.py
src/popoto/fields/sorted_field_mixin.py tests/test_sorted_range_pushdown.py` is empty.
`origin/main` is still `7ffc8e8`.

**Active plans in `docs/plans/` overlapping this area:** none. `computed_sort.md` and
`decaying_sorted_field.md` touch sorted fields but neither covers the pushdown gate.

## Prior Art

- **PR #517** — "Bound SortedField queries before hydration instead of after" — MERGED,
  shipped the two-mechanism implementation (`_sorted_pushdown_args` for the Redis-side
  bound, `_bound_keys_before_hydration` for the pre-hydration slice) plus the 21-test suite
  this plan extends. This is the code under test; nothing here changes it.
- **PR #518** — "perf(query): conditional limit pushdown into SortedField range reads" —
  CLOSED as superseded. A trial rebase onto current main resolved to a zero-line diff on the
  production side, but its test file contains four tests main's suite lacks. Those tests are
  the source material. They are **not** cherry-pickable — see Rabbit Holes.
- No prior attempt has tried to add these specific tests to main. This is additive coverage,
  not a repeated fix.

## Research

No relevant external findings — proceeding with codebase context. The work is purely
internal (pytest, an in-repo ORM, and a Redis/Valkey connection already under test). The one
ecosystem-adjacent fact needed was confirmed locally rather than by search: pytest-asyncio
runs in `Mode.STRICT` in this repo (visible in the pytest header), so a new async test needs
an explicit `@pytest.mark.asyncio` marker. Twenty-plus existing test modules already do this
(`tests/test_async.py`, `tests/test_bulk_operations.py`, `tests/test_get_many.py`, ...).

## Spike Results

All spikes were run as throwaway pytest modules inside the worktree
(`/Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage`), against the **worktree's
own venv** (`.venv/bin/python`, editable install resolving to the worktree `src/`), with
`POPOTO_TEST_DB=14`. Scratch modules were deleted after each run.

> **Environment warning that cost this plan a full re-run.** The first spike pass used the
> main checkout's `.venv`. Its editable install resolves to `/Users/valorengels/src/popoto/src`,
> which is on branch `bench/530-post-correction-refresh`, and the popoto pytest plugin collapses
> `import popoto` and `import src.popoto` onto one canonical module — so the worktree's `src/`
> was never executed. Twelve of that run's failures came from the other branch's
> `encoding.py` / `indexed_field_mixin.py`. Every number below is from the second pass.
> This is CLAUDE.md worktree gotcha #1, live.

### spike-1: Does the async path push the bound down?
- **Assumption**: "Async pushdown parity holds — the bound fires and results match sync."
  (issue #559, acceptance criterion 1)
- **Method**: prototype (throwaway pytest module, `mock.patch.object` spies on
  `POPOTO_REDIS_DB.zrangebyscore` / `zrevrangebyscore`, plus counters on
  `redis.client.Pipeline.hgetall` and `redis.asyncio.client.Pipeline.hgetall`)
- **Finding**: **Results match, the bound does not fire.** 60 records in one partition,
  `order_by="-last_active_at"`, `limit=5`:

  | path | range call issued | `HGETALL` count |
  |---|---|---|
  | sync `filter()` | `zrevrangebyscore(key, '+inf', '0', start=0, num=13)` | 26 (13 objects x 2) |
  | `async_filter()` | `zrangebyscore(key, '0', '+inf')` — unbounded, not even `desc` | 60 (full population) |

  Both return `['d059','d058','d057','d056','d055']`.
- **Confidence**: high (measured twice, on both checkouts; `query.py` is byte-identical
  between them, so the structural read and the measurement agree)
- **Impact on plan**: acceptance criterion 1 is unsatisfiable test-only. Re-scoped to
  (a) a hard result-parity assertion and (b) an `xfail(strict=True)` bound-fires assertion
  referencing **#571**. Filed as #571.

### spike-2: Is `count()` truncated by a limit?
- **Assumption**: "`count()` reports the full match count when a limit is present."
- **Method**: prototype
- **Finding**: **Confirmed, in all three shapes.** With 60 matching records:
  `Query.count(room_id=..., last_active_at__gte=0)` -> 60;
  `.filter(...).limit(5).count()` -> 60 while `len(list(qb))` -> 5;
  `Query.count(..., limit=5)` -> 60; `await async_count(..., limit=5)` -> 60.
  Structurally safe by two independent routes: `QueryBuilder.count` (`query.py:1474`)
  calls `self._query.count(**self._filters)` and never forwards `_limit_value`, and
  `Query.count` (`query.py:2781`) returns `len(db_keys)` from `filter_for_keys_set`, which
  cannot bound because `_pushdown_allowed` is `False` outside `_execute_filter`.
- **Confidence**: high
- **Impact on plan**: test as specified. Assert both the `QueryBuilder.limit(n).count()`
  form and the `Query.count(..., limit=n)` kwargs form — they reach `Query.count` by
  different routes and only one of them is what a caller is likely to write.

### spike-3: Does `Meta.order_by` on the sorted field set the direction?
- **Assumption**: "`Meta.order_by = '-last_active_at'` makes an unordered query descending
  *and* keeps the bound."
- **Method**: prototype (dedicated model class — `Meta` is class-level, so this needs its own model)
- **Finding**: **Confirmed.** 20 records, `limit=3`, no explicit `order_by`:
  results `['m019','m018','m017']` (descending) at 22 `HGETALL`s; the same query without
  `limit` costs 40. 22 = 11 objects x 2 = `(limit 3 + margin 8) x 2`. Both the direction
  and the bound come from `Meta`.
- **Confidence**: high
- **Impact on plan**: two model classes needed (`-last_active_at` and `last_active_at`).
  Note the ascending case is a weak discriminator — with no `Meta` at all the sorted-set
  order is *already* ascending, so an ascending-`Meta` test proves "Meta did not break it"
  rather than "Meta supplied it". Assert results + bounded hydration and say so in a comment.

### spike-4: Does `Meta.order_by` on another field block the pushdown?
- **Assumption**: "`Meta.order_by = 'doc_id'` declines the bound and still returns complete
  results."
- **Method**: prototype (dedicated model, `doc_id` deliberately anti-correlated with score)
- **Finding**: **Confirmed.** 20 records seeded so `doc_id` descends as `last_active_at`
  ascends; `limit=3`, no explicit `order_by` -> `['z000','z001','z002']` (the correct
  `doc_id`-ordered head, which is the *tail* of the score order) at 40 `HGETALL`s = the full
  unbounded read. The bound correctly declined.
- **Confidence**: high
- **Impact on plan**: test as specified. This is the one case where a dropped guard returns
  *wrong rows*, not just short ones, so it earns a comment saying that.

### spike-5: What instrument actually observes the bound on main?
- **Assumption**: "#518's `redis_spy` fixture (patching `zrange` / `zrangebyscore`) transfers."
- **Method**: code-read + prototype
- **Finding**: **It does not.** `sorted_field_mixin.py:762-785` dispatches four ways:
  bounded+desc -> `zrevrangebyscore(..., start=0, num=_limit)`; bounded+asc ->
  `zrangebyscore(..., start=0, num=_limit)`; unbounded+desc -> `zrevrangebyscore(...)`;
  unbounded+asc -> `zrangebyscore(...)`. **`zrange` is never called.** #518's
  `zrange.assert_not_called()` would vacuously pass and its
  `zrange.call_args.kwargs["num"] == 5` would `AttributeError`. Two further traps:
  `num` is `limit + 8`, never `limit`; and a single `list(filter(...))` issues
  **two** `zrevrangebyscore` calls, so `call_count == 1` fails.
- **Confidence**: high
- **Impact on plan**: **do not port `redis_spy`.** Use the file's existing
  `HydrationCounter` for the sync tests (it is the house instrument and it works), and add
  an async twin for the async test. Note `HydrationCounter` counts **2 per object**
  (measured: 20 objects -> 40) — the existing tests' `counter.count < POPULATION` assertions
  are loose by design; new assertions should compare against the same kind of bound rather
  than an exact object count.

### spike-6: Does `HydrationCounter` work on the async path?
- **Assumption**: "The existing `HydrationCounter` can measure async hydration."
- **Method**: prototype
- **Finding**: **No — it reports 0.** It patches `redis.client.Pipeline.hgetall`; the async
  path loads through `redis.asyncio.client.Pipeline.hgetall` via `_async_get_many_objects`.
  A twin patching `redis.asyncio.client.Pipeline.hgetall` reports 60. (Note the async count
  is 1 per object, not 2 — 60 records, 60 calls.)
- **Confidence**: high
- **Impact on plan**: the async test needs its own counter class. Reusing `HydrationCounter`
  would produce a test that "passes" by measuring nothing.

## Data Flow

Both bounds live on the read path between the filter call and object hydration.

1. **Entry point**: `Model.query.filter(room_id=..., last_active_at__gte=..., order_by=..., limit=n)`
2. **`QueryBuilder`** (`query.py:112`) — accumulates `_limit_value` / `_order_by_value`;
   `.count()` (line 1474) branches off here and forwards only `_filters`, never the limit.
3. **`Query._execute_filter`** (`query.py:2519`) — sets `_pushdown_allowed = True`
   (line 2555, `finally`-reset at 2557). **This is the gate the async path never opens.**
4. **`Query.filter_for_keys_set`** (`query.py:2003`) — per sorted field, calls
   `_sorted_pushdown_args` (line 2077). On approval, passes `_limit=limit+8`, `_desc` into
   `field.filter_query`.
5. **`SortedFieldMixin.filter_query`** (`sorted_field_mixin.py:762-785`) — four-way dispatch
   to `zrevrangebyscore` / `zrangebyscore`, with or without `start=0, num=`.
6. **`Query._bound_keys_before_hydration`** (`query.py:1894`, called at 2586) — slices the
   already-intersected ordered key list to `limit+8` when the Redis-side bound could not
   apply. **Also never reached from async.**
7. **`Query.get_many_objects`** — `HGETALL` per surviving key. This is what `HydrationCounter`
   observes and the only place the optimization is visible from a test.
8. **Short-result guard** (`query.py:2605-2650`) — if a bounded read came back short and the
   range was not exhausted, log a warning and re-read unbounded.
9. **Output**: ordered list of model instances.

The async variant (`async_filter`, `query.py:3051`) enters at step 4 directly and exits to
`_async_get_many_objects`, skipping steps 3, 6, and 8 entirely.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none. Test-only.
- **Coupling**: adds three model classes to `tests/test_sorted_range_pushdown.py`. They must
  be named with a `PushdownDoc` prefix so the file's existing
  `_flush()` glob (`POPOTO_REDIS_DB.keys("*PushdownDoc*")`) sweeps their hashes *and* their
  sorted-set keys (`$SortF:<ClassName>:<field>:<partition>` embeds the class name).
- **Data ownership**: unchanged.
- **Reversibility**: trivial — delete the added tests.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (the re-scoped async criterion — see Open Questions)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Worktree venv resolves to *this* checkout | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && .venv/bin/python -c "import popoto,sys; assert '.worktrees/559-pushdown-coverage' in popoto.__file__, popoto.__file__"` | CLAUDE.md gotcha #1 — main's venv silently tests `bench/530-post-correction-refresh` |
| Full extras installed (no ~95 deselects) | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && .venv/bin/python -c "import numpy, sentence_transformers"` | CLAUDE.md gotcha #2 |
| Redis/Valkey reachable | `redis-cli -n 14 ping` | Suite needs a live server |
| `POPOTO_TEST_DB=14` exported | `test "$POPOTO_TEST_DB" = 14` | CLAUDE.md gotcha #4 — five pipelines share DB 15 |

The venv already exists and satisfies checks 1 and 2 (created during planning with
`.[dev,embeddings,benchmark]`; `dataframe` deliberately omitted, it breaks
`test_dataframe_field.py` collection).

## Solution

### Key Elements

- **`AsyncHydrationCounter`**: twin of the existing `HydrationCounter`, patching
  `redis.asyncio.client.Pipeline.hgetall`. Without it the async test measures nothing.
- **Three `Meta`-carrying model classes**: `PushdownDocMetaDesc` (`order_by = "-last_active_at"`),
  `PushdownDocMetaAsc` (`order_by = "last_active_at"`), `PushdownDocMetaOther`
  (`order_by = "doc_id"`). `Meta` is class-level, so each case needs its own model.
- **Five new tests**: two async (one hard, one `xfail(strict=True)`), one `count()`,
  three `Meta.order_by` — six functions total.

### Flow

`_seed(...)` into a partition -> issue the query under a hydration counter -> assert the
returned `doc_id` list exactly -> assert the hydration count is on the correct side of the
`limit + SORTED_PUSHDOWN_OVERFETCH_MARGIN` boundary.

### Technical Approach

- **Import the margin, do not hardcode 8.**
  `from src.popoto.fields.constants import Defaults`, then express bounds as
  `limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`. The constant is explicitly an
  experimental tuning knob (CLAUDE.md); a literal `13` turns a future retune into a
  false regression.
- **Assert on the boundary, not on an exact count.** `HydrationCounter` counts 2 per
  object on the sync path and the async twin counts 1 — so an exact expectation is
  fragile across paths. Use the existing file's idiom (`counter.count < POPULATION`,
  `counter.count >= POPULATION`) and, where a tighter claim is wanted, assert
  `counter.count < 2 * (limit + MARGIN) + slack` with a message naming what failed.
- **Async test 1 (hard assertion, passes today):** run the same filter through
  `filter()` and `await async_filter()` and assert the `doc_id` lists are *identical*.
  This is the criterion that actually protects users, and it is true today.
- **Async test 2 (`@pytest.mark.xfail(strict=True, reason="#571 ...")`):** assert under
  `AsyncHydrationCounter` that `count < POPULATION`. It fails today (60 of 60). `strict=True`
  means it errors the moment #571 lands, which is exactly the notification wanted. The
  `reason=` string must contain `#571`.
- **`count()` test:** three assertions in one function —
  `Query.count(**filters)` == `POPULATION`, `.filter(**filters).limit(5).count()` ==
  `POPULATION`, and `len(list(same_qb))` == 5. The third is what makes the second meaningful.
- **`Meta` tests:** dedicated models, `PushdownDoc`-prefixed. Seed the "other field" case so
  `doc_id` order is the *reverse* of score order, so a leaked bound returns visibly wrong rows.
- **Do not touch existing tests.** Everything is additive.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. The added tests exercise no `try/except` in production code;
the pushdown guards are plain early returns.

### Empty/Invalid Input Handling
Already covered by the existing `test_non_positive_int_limit_does_not_bound` parametrization
(`0`, `-1`, `None`, `True`) and `test_no_limit_reads_full_range`. Nothing to add. The one new
input shape — `Meta.order_by` naming a non-existent or non-string value — is out of scope
(`Meta.order_by` is validated at model-definition time, not query time).

### Error State Rendering
The short-result warning path (`query.py:2605-2650`) is the "error state" here and is already
covered by three existing tests
(`test_orphan_density_past_the_margin_warns_and_still_returns_full`,
`test_margin_absorbs_light_orphan_density_without_a_re_read`,
`test_exhausted_range_short_on_orphans_still_warns`). No new coverage needed — but the new
`Meta` tests must not trip it, so their assertions on `caplog` are unnecessary and their
seeded data must contain no orphans.

## Test Impact

No existing tests affected — this work is purely additive to
`tests/test_sorted_range_pushdown.py`. The 24 currently-collected tests keep passing
unchanged; the new model classes are new names and the new `_flush()` coverage is a
superset of the current glob.

- [ ] `tests/test_sorted_range_pushdown.py` — UPDATE (additive only): add
  `AsyncHydrationCounter`, three `Meta` model classes, six test functions, and the
  `Defaults` import. Do not modify any existing function body.

## Rabbit Holes

- **Cherry-picking #518's tests verbatim.** They target `_resolve_range_pushdown` /
  `_range_pushdown_limit`, which do not exist, and spy on `zrange`, which is never called
  (spike-5). A "port" that compiles would be a test that asserts nothing. Rewrite against
  main's symbols and main's `HydrationCounter` idiom.
- **Porting #518's `redis_spy` fixture.** Same reason. Two whole afternoons are available
  in trying to make `zrange.call_args.kwargs["num"] == 5` work against code that calls
  `zrevrangebyscore(..., num=13)` twice.
- **Fixing the async pushdown.** In scope for #571, not here. #559 is test-only and the
  correct fix needs the orphan re-read guard ported too, or it is a correctness regression.
- **Tightening `HydrationCounter` to an exact object count.** It counts 2 `HGETALL`s per
  object on the sync path and 1 on the async path; chasing an exact number is a fragile
  detour into redis-py pipeline internals.
- **Making the ascending-`Meta` test "prove" more than it can.** With no `Meta`, sorted-set
  order is already ascending. There is no query shape that distinguishes "Meta supplied
  ascending" from "the default was already ascending" through public behavior. Assert what
  is observable and comment the limit.

## Risks

### Risk 1: The async `xfail` is read as a broken test rather than a filed gap
**Impact:** a future contributor deletes the marker or the test, losing the only record that
async pushdown is missing.
**Mitigation:** `strict=True` (an accidental fix errors loudly rather than passing silently),
`reason=` names issue #571 inline, and #571's body states that closing it means flipping the
marker.

### Risk 2: New model classes leak state into other tests on a shared DB
**Impact:** phantom failures in unrelated modules, the failure mode CLAUDE.md gotcha #4
describes.
**Mitigation:** prefix every new model `PushdownDoc*` so the autouse `clean_docs` fixture's
existing `keys("*PushdownDoc*")` glob sweeps both the model hashes and the
`$SortF:<ClassName>:...` sorted-set keys. Verify by running the new tests, then the full
module, then `redis-cli -n 14 keys '*Pushdown*'` and expecting empty.

### Risk 3: A metric measured in the wrong venv is reported as truth
**Impact:** this already happened once during planning — 12 spurious failures from
`bench/530-post-correction-refresh` (see the Spike Results warning box).
**Mitigation:** the Prerequisites table's first check is programmatic; run it before any
pytest invocation and state the venv path alongside every count reported.

### Risk 4: `Meta.order_by` model definitions collide with existing model registration
**Impact:** popoto registers models globally at class-definition time; a duplicate class name
across modules raises or silently shadows.
**Mitigation:** the three names (`PushdownDocMetaDesc`, `PushdownDocMetaAsc`,
`PushdownDocMetaOther`) do not appear anywhere in `tests/` today — confirmed by
`git grep -c "PushdownDocMeta" tests/` returning nothing. Re-confirm before adding.

## Race Conditions

No race conditions identified in the code under test. The sync path is synchronous and
single-threaded. The async path's only concurrency is `to_thread(self.filter_for_keys_set)`,
which mutates per-`Query`-instance state (`_sorted_field_order`, `_pushdown_limit`, ...) —
a real hazard if two coroutines shared a `Query`, but out of scope here and untouched by
test-only work.

One **test-level** hazard: the module's autouse `clean_docs` fixture flushes before and
after each test, and five other SDLC pipelines share this repo. Running on DB 15 has
historically produced 73-158 phantom failures. `POPOTO_TEST_DB=14` is mandatory for every
pytest invocation in this plan; the five `assert db == 15` tests in
`tests/test_pytest_plugin.py` are expected failures under any non-15 DB and are **not**
regressions.

**Measured baseline** (worktree venv, `POPOTO_TEST_DB=14`, `7ffc8e8`, full suite,
208s): `5 failed, 2618 passed, 26 skipped` — the five failures are exactly the
`assert db == 15` tests listed above. `test_version.py::test_version_matches_pyproject`
passes, because the worktree venv's editable install is fresh (1.8.2); it only fails on a
stale install. Any other failure after this work is a regression.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #571] Making `async_filter` actually apply the pushdown (open
  `_pushdown_allowed` around the `to_thread` call, call `_bound_keys_before_hydration`, port
  the orphan re-read guard). #559 is test-only; this plan pins the gap with a strict `xfail`
  instead of fixing it.
- Nothing else deferred — every other item in #559's acceptance criteria is in scope.

## Update System

No update system changes required — this is a test-only change with no runtime, deploy, or
dependency surface.

## Agent Integration

No agent integration required — this adds pytest functions to an existing test module. No
MCP surface, no tool wrapper, no entry point.

## Documentation

No feature documentation changes required — this plan adds no feature and changes no public
API. The behaviors being covered are already documented as part of the #517 pushdown work.

### Inline Documentation
- [ ] Each new test carries a docstring naming the guard it defends and what a dropped guard
      would return (short results vs. wrong results) — matching the existing file's style.
- [ ] The async `xfail` carries an inline comment explaining that async parity of *results*
      holds while the *bound* does not, with the measured numbers.
- [ ] The ascending-`Meta` test comments that it is a weak discriminator (spike-3).

## Success Criteria

- [ ] `count()` with a limit reports the full match count, asserted for both the
      `QueryBuilder.limit(n).count()` and `Query.count(..., limit=n)` forms, alongside a
      `len(list(qb)) == n` assertion proving the limit was otherwise live.
- [ ] `Meta.order_by` on the sorted field sets direction with no explicit `order_by` —
      covered for both descending and ascending, each asserting exact result order and a
      bounded hydration count.
- [ ] `Meta.order_by` naming a different field declines the pushdown, results are complete
      and in `Meta` order, and the hydration count shows the full range was read.
- [ ] Async result parity is asserted as a hard equality against the sync path's rows.
- [ ] The async *bound* is pinned by `@pytest.mark.xfail(strict=True)` whose `reason`
      contains `#571`.
- [ ] `SORTED_PUSHDOWN_OVERFETCH_MARGIN` is imported, not hardcoded — no bare `13` or `8`
      in the new assertions.
- [ ] No file outside `tests/` is modified.
- [ ] `tests/test_sorted_range_pushdown.py` passes in full on `POPOTO_TEST_DB=14` in the
      worktree venv.
- [ ] Full suite passes apart from the five documented expected failures (baseline: 5 failed, 2618 passed, 26 skipped).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (pushdown-tests)**
  - Name: `pushdown-test-builder`
  - Role: write the six new tests, the async counter, and the three `Meta` models in
    `tests/test_sorted_range_pushdown.py`
  - Agent Type: test-engineer
  - Domain: Redis/Popoto data
  - Resume: true

- **Validator (pushdown-tests)**
  - Name: `pushdown-test-validator`
  - Role: verify the tests fail for the right reason before they pass — mutate each guard
    in a scratch copy and confirm the matching test goes red
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Add the async instrument and the async parity pair
- **Task ID**: build-async
- **Depends On**: none
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-1 (async bound does not fire; results do match), spike-6
  (`HydrationCounter` reports 0 on the async path — needs a `redis.asyncio` twin)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `AsyncHydrationCounter`, patching `redis.asyncio.client.Pipeline.hgetall`, mirroring
  the existing `HydrationCounter` context-manager shape. Note it counts 1 per object.
- Add `test_async_filter_returns_the_same_rows_as_sync` — `@pytest.mark.asyncio`, seed
  `_seed()`, run the identical filter through `filter()` and `await async_filter()`, assert
  the `doc_id` lists are equal *and* equal to the known expected slice.
- Add `test_async_filter_does_not_yet_bound_the_read` — `@pytest.mark.asyncio` plus
  `@pytest.mark.xfail(strict=True, reason="#571: async_filter applies neither the Redis-side
  bound nor the pre-hydration slice")`. Under `AsyncHydrationCounter`, assert
  `counter.count < POPULATION`.
- Docstring the pair: results are correct, cost is not; measured 60 of 60 hydrations for
  `limit=5` at plan time.

### 2. Add the count() coverage
- **Task ID**: build-count
- **Depends On**: none
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-2 (all three forms report the full count; `QueryBuilder.count`
  never forwards `_limit_value`)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `test_count_is_not_truncated_by_a_limit`. Seed `_seed()` (POPULATION=60).
- Assert `PushdownDoc.query.count(room_id="r1", last_active_at__gte=0) == POPULATION`.
- Build one `QueryBuilder` with `.limit(5)`; assert `.count() == POPULATION` and
  `len(list(qb)) == 5` on the same object — the second assertion is what proves the limit
  was live and `count()` deliberately ignored it.
- Assert the kwargs form `PushdownDoc.query.count(..., limit=5) == POPULATION` too.

### 3. Add the Meta.order_by coverage
- **Task ID**: build-meta
- **Depends On**: none
- **Validates**: `tests/test_sorted_range_pushdown.py` (modify)
- **Informed By**: spike-3 (desc `Meta` sets direction and keeps the bound: 22 vs 40
  `HGETALL`s at `limit=3` over 20 records), spike-4 (other-field `Meta` declines: 40
  `HGETALL`s, `doc_id`-ordered results), spike-5 (use `HydrationCounter`, never `zrange`)
- **Assigned To**: `pushdown-test-builder`
- **Agent Type**: test-engineer
- **Parallel**: true
- Define `PushdownDocMetaDesc`, `PushdownDocMetaAsc`, `PushdownDocMetaOther` — same field
  shape as `PushdownDoc` (`room_id` KeyField, `doc_id` KeyField, `last_active_at`
  SortedField partitioned on `room_id`), each with its own `class Meta: order_by = ...`.
  Keep the `PushdownDoc` name prefix so `_flush()` sweeps them.
- Add `test_meta_order_by_on_the_sorted_field_sets_descending_direction` — seed 20, query
  with `limit=3` and no `order_by`, assert the descending head exactly, assert the hydration
  count is below the unbounded cost.
- Add `test_meta_order_by_on_the_sorted_field_keeps_ascending_bounded` — same shape on
  `PushdownDocMetaAsc`; comment that this is a weak discriminator (spike-3).
- Add `test_meta_order_by_on_another_field_blocks_the_pushdown` — seed
  `PushdownDocMetaOther` so `doc_id` order is the reverse of score order, query with
  `limit=3` and no `order_by`, assert the `doc_id`-ordered head (which is the score-order
  *tail*), and assert the hydration count shows a full unbounded read.
- Express every bound via `limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`; add the
  `from src.popoto.fields.constants import Defaults` import.

### 4. Verify the tests fail for the right reason
- **Task ID**: validate-guards
- **Depends On**: build-async, build-count, build-meta
- **Assigned To**: `pushdown-test-validator`
- **Agent Type**: validator
- **Parallel**: false
- In a scratch copy of `src/popoto/models/query.py` (never committed, never in the worktree's
  tracked tree), delete the `order_by`/`_meta.order_by` early return in
  `_sorted_pushdown_args` and confirm
  `test_meta_order_by_on_another_field_blocks_the_pushdown` goes red.
- Do the same for the mirrored return in `_bound_keys_before_hydration`.
- Confirm the async `xfail` currently reports `xfail` (not `xpass`, which under
  `strict=True` is a failure) and that removing the marker produces a red test.
- Restore `src/` to pristine; `git status --short src/` must be empty.

### 5. Full-module and full-suite verification
- **Task ID**: validate-all
- **Depends On**: validate-guards
- **Assigned To**: `pushdown-test-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run the Prerequisites table's venv check first and record its output.
- `POPOTO_TEST_DB=14 .venv/bin/python -m pytest tests/test_sorted_range_pushdown.py -q`
  — expect all green (24 existing + 6 new, with 1 xfail).
- `redis-cli -n 14 keys '*Pushdown*'` — expect empty (no leaked state).
- `POPOTO_TEST_DB=14 .venv/bin/python -m pytest -q` — expect exactly the five documented
  expected failures and nothing else.
- `git diff --name-only origin/main` — expect only `tests/test_sorted_range_pushdown.py`
  and `docs/plans/sorted_pushdown_coverage_gaps.md`.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Correct venv under test | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && .venv/bin/python -c "import popoto; assert '.worktrees/559-pushdown-coverage' in popoto.__file__"` | exit code 0 |
| Pushdown module green | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && POPOTO_TEST_DB=14 .venv/bin/python -m pytest tests/test_sorted_range_pushdown.py -q` | exit code 0 |
| Six new tests collected | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && POPOTO_TEST_DB=14 .venv/bin/python -m pytest tests/test_sorted_range_pushdown.py --collect-only -q \| tail -1` | output contains 30 |
| Async gap pinned to #571 | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && grep -c 'xfail(strict=True' tests/test_sorted_range_pushdown.py` | output > 0 |
| xfail names the issue | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && grep -c '#571' tests/test_sorted_range_pushdown.py` | output > 0 |
| Margin imported, not hardcoded | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && grep -c 'SORTED_PUSHDOWN_OVERFETCH_MARGIN' tests/test_sorted_range_pushdown.py` | output > 0 |
| Anti-criterion: no production change (#571 not fixed here) | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && git diff --name-only origin/main -- src/ \| wc -l \| tr -d ' '` | output contains 0 |
| Anti-criterion: no bare margin literal | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && git diff origin/main -- tests/test_sorted_range_pushdown.py \| grep -c '^+.*num=13'` | match count == 0 |
| No leaked Redis state | `redis-cli -n 14 keys '*Pushdown*' \| wc -l \| tr -d ' '` | output contains 0 |
| Format clean | `cd /Users/valorengels/src/popoto/.worktrees/559-pushdown-coverage && .venv/bin/python -m black --check tests/test_sorted_range_pushdown.py` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **Async criterion re-scope — confirm.** #559's acceptance criterion 1 asks that the async
   bound "actually fires". Measurement shows it does not, and fixing it is a production
   change that #559 explicitly forbids. This plan satisfies the criterion's *intent* with a
   hard result-parity assertion plus a strict `xfail` referencing the newly-filed **#571**.
   Confirm that is the right split, rather than expanding #559's scope to include the fix.
2. **Should #559's acceptance-criteria checklist be edited on the issue** to reflect the
   re-scope, or left as filed with this plan as the record of the correction?
3. **`strict=True` on the async `xfail`** means the suite goes red the moment #571 lands, on
   whatever branch lands it. That is the intended notification, but it puts a coupling on
   #571's author. Acceptable, or prefer non-strict?
