---
status: Ready
type: bug
appetite: Medium
owner: plan-600
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/600
last_comment_id: none (issue has no comments)
---

# #600 — Move `Query` pushdown bookkeeping off shared instance state on the sync path

## Problem

`Query` is instantiated **once per model class** — `new_class.objects = new_class.query = Query(new_class)`
(`src/popoto/models/base.py:505`). Every `Model.query.filter(...)` on every thread shares one
`self`. `filter_for_keys_set` resets seven bookkeeping attributes on that shared object and then
repopulates them mid-flight, with Redis round trips in between:

| Stage | Location (origin/main `9c04b8c`) |
|---|---|
| reset all seven | `src/popoto/models/query.py:2511-2517` |
| populate from the sorted-field read | `src/popoto/models/query.py:2572-2579` |
| populate `_pending_client_filters` | `src/popoto/models/query.py:2633` |
| re-intersect `_sorted_field_order` | `src/popoto/models/query.py:2643-2646` |
| `_execute_filter` reads them back | `src/popoto/models/query.py:3053-3058`, `3089`, `3109` |

Between one thread's populate and its read there is at least one more blocking Redis call
whenever a second indexed predicate is present (the non-sorted field loop at `query.py:2580-2606`).
That call releases the GIL. A second thread's reset-and-populate lands inside the window, and the
first thread then hydrates the **other thread's key list**.

**Current behavior:** two threads querying different partitions of the same model return each
other's rows. Reproduced against `origin/main` (see `## Spike Results`, spike-1): a query filtered
`room_id="d"` returned six rows whose `room_id` was `"b"`. This is a cross-partition read, the
same isolation failure class as #576, and `docs/multi-tenancy.md:13` promises the opposite
("complete isolation"). `docs/configuration.md:405-425` promotes concurrent
`Model.query.get(...)` under a `ThreadPoolExecutor` as thread-safe, which is the pattern that
triggers it.

**Desired outcome:** concurrent `filter()` / `count()` calls on one model class, with different
sorted-field bounds, partitions and directions, each return exactly their own rows and their own
count, forever. No caller-visible API change: `filter_for_keys_set` stays public and the seven
attributes stay readable by their existing four in-repo readers.

## Freshness Check

**Baseline commit:** `9c04b8c30d92896b40cd6a7b7d346dd7d7f30853` (`origin/main`).
**Local `main` was behind by one commit at plan time** (`0dbce75`); every line reference in this
plan was read from `git show origin/main:src/popoto/models/query.py`, not from the working tree.
Builders must confirm `git rev-parse origin/main` still resolves to `9c04b8c` or re-check the
references below.

**Issue filed at:** 2026-09-04T06:15:32Z
**Disposition:** Minor drift — every claim in the issue holds; two line numbers moved.

**File:line references re-verified:**

- `src/popoto/models/base.py:505` — `new_class.objects = new_class.query = Query(new_class)` — **still exact.**
- The seven attributes named in the issue — all present, reset at `query.py:2511-2517`,
  populated at `2572-2579` / `2633` / `2643-2646` — **still exact.**
- `_snapshot_pushdown_state` — issue implies it exists; it does, at `query.py:2253` (drifted
  from 2246 in the local checkout by #609's added import).
- `_filter_keys_with_pushdown` — `query.py:2271`.
- `_bound_keys_before_hydration(..., state=)` — `query.py:2291`; its `state is None` branch
  snapshots and writes back to `self` at `2353-2355`.
- `_short_result_action` — `query.py:2358`; reads only the state object.
- `_execute_filter` — `query.py:3011`; arms `_pushdown_allowed` at `3040-3051`, reads
  `_sorted_field_order` at `3053-3058`, `_pending_client_filters` at `3089` and `3109`, and
  calls `_short_result_action(..., self._snapshot_pushdown_state())` at `3092-3094`.
- `async_filter` — `query.py:3532`, already fully on the carrier.

**Readers of the seven attributes outside `_execute_filter` (the "readers have to move with it"
list in the issue), all re-verified:**

| Reader | Location | Attribute | Same thread as the write? |
|---|---|---|---|
| `QueryBuilder.fuse()` | `src/popoto/models/query.py:1181` | `_pending_client_filters` | yes |
| `Query.count()` | `src/popoto/models/query.py:3281` | `_pending_client_filters` | yes |
| `Query.async_count()` | `src/popoto/models/query.py:3713` | `_pending_client_filters` | **NO — reads on the event-loop thread after `to_thread` at 3712** |
| `transfer.export` | `src/popoto/transfer/export.py:277` | `_pending_client_filters` | yes |
| `ContextAssembler._pull_path_hybrid` | `src/popoto/recipes/context_assembler.py:2368` | `_pending_client_filters` | yes |
| `tests/test_validity_field.py` | `802`, `810`, `815`, `832`, `853` | `_pushdown_limit`, `_sorted_field_order`, `_pushdown_allowed` | yes |

`_evaluate_filter_args` (`query.py:2650`) does **not** read any of the seven; it only calls
`filter_for_keys_set`. The issue's mention of it is a forward reference to its callers
(`_execute_filter`, `export.py`), both listed above.

**Cited sibling issues/PRs re-checked:**

- **#571 / PR #602** — merged 2026-09-04T06:36:32Z, *after* this issue was filed. It introduced
  `_PushdownState`, `_snapshot_pushdown_state`, `_filter_keys_with_pushdown`, the
  `state=` parameter and `_short_result_action`. This plan consumes those, adds nothing to the
  async path's design.
- **#575 / PR #609** — merged 2026-09-04T09:05:33Z. Routed three `[str(self._filters[pf]) ...]`
  comprehensions through `canonical_key_str` (`query.py:440`, `1383`, `1427`) and added the
  `from .canonical_key import canonical_key_str` import at `query.py:61`. All three sites are in
  `QueryBuilder`, far above the pushdown region; no semantic overlap, but every line number below
  ~2000 in older plans is off by one against this baseline.
- **#576** — CLOSED. Prior art for the cross-scope-leak framing used in `## Problem`.
- **#559 / PR #608** — merged. Added `test_count_is_not_truncated_by_a_present_limit` and the
  async Meta.order_by coverage now living in `tests/test_sorted_range_pushdown.py`.
- **#610** — OPEN, plan `docs/plans/count_q_object_limit.md` is `status: Ready`. It edits
  `QueryBuilder.count()` at `query.py:1821-1822`. No line overlap with this plan, but it is the
  same file. **Sequence this plan after #610 merges** so #610 keeps its minimal, localized diff.

**Commits on `origin/main` touching `src/popoto/models/query.py` since the issue was filed:**

- `7f057f9` (#602) — the async pushdown fix. Created the carrier this plan extends. Relevant.
- `9c04b8c` (#609) — `canonical_key_str` routing. Irrelevant to the mechanism; line-number drift only.

**Active plans overlapping this area:**

- `docs/plans/count_q_object_limit.md` (#610) — same file, disjoint lines. Ordering constraint above.
- `docs/plans/async_sorted_pushdown_parity.md` (#571) — shipped; its "Residual exposure" bullet
  *is* this issue.
- `docs/plans/sorted_pushdown_coverage_gaps.md` (#559) — shipped.

**Bug reproduced against the baseline:** yes. See `## Spike Results` spike-1 — three of three
runs, with the failure mode captured verbatim.

## Prior Art

- **#571 / PR #602** — *async_filter must apply the SortedField limit pushdown*. Merged. Closed
  the identical hazard on the async path with a per-call `_PushdownState` plus a per-running-loop
  `asyncio.Lock`, and named the sync gap explicitly as residual exposure with "file a follow-up
  issue" as a task. This issue is that follow-up. Its design is the one being extended.
- **#517** — *Bound SortedField queries before hydration* (`bc3b267`). Introduced
  `_pushdown_limit` / `_pushdown_requested` / `_pushdown_fetched` / `_pushdown_partition` as
  instance attributes, which is where the shared state originates.
- **#576 / PR #593** — *BM25/lexical retrieval leaks memories across `agent_id` scopes*. Closed.
  Same observable symptom (rows from another tenant's scope), different mechanism (an unscoped
  candidate fetch, not shared mutable state). Useful precedent for how seriously the repo treats
  a cross-scope read, and for the assertion style: assert on the scope field of every returned
  row, not just on row identity.
- **#594** — suppression of the pre-hydration `limit` when `_pending_client_filters` is
  non-empty. Both suppression sites are unconditional on the shared helpers, so this plan
  inherits them unchanged.

No prior attempt at a sync-path fix exists, so there is no `## Why Previous Fixes Failed` section.

## Research

No relevant external findings. This is an in-repo concurrency fix against popoto's own `Query`
internals: no external library, API, or ecosystem surface is involved. The one general-purpose
technique used — `threading.local()` behind a data descriptor — is CPython stdlib behavior that
spike-2 verified directly against this codebase rather than from documentation.

## Spike Results

Both spikes ran against `origin/main` content with `REDIS_URL=redis://localhost:6379/2` exported
**before** `import popoto` (CLAUDE.md, #577). Environment: `/Users/valorengels/src/popoto/.venv`,
CPython 3.x, **redis-py 7.1.1**, Redis on `localhost:6379`, DB 2 (`dbsize` 0 before and after;
every seeded key deleted on exit). DB 0 was never touched.

### spike-1: Does the sync race actually produce wrong rows, and what does it take to trigger it?

- **Assumption**: "the sync path is sound by accident of straight-line execution within one
  thread, and unsound across threads" (issue body).
- **Method**: prototype — four rooms in one model, two threads per room, tight `filter()` loop
  with per-thread limit and direction.
- **Finding**: **Confirmed, and the trigger conditions are narrow enough to matter for the test
  design.** Three ingredients are each load-bearing:
  1. **A second indexed predicate.** With only `room_id` (consumed as `partition_by`) and
     `score__gte`, the sorted field consumes every param, so no Redis call separates the populate
     at `query.py:2572-2579` from the read at `3053-3058`. 4 threads x 400 iterations over
     40-row rooms: **0 failures**. Adding an `IndexedField` predicate puts a full round trip in
     the window.
  2. **A shortened GIL switch interval.** Even with the extra round trip, localhost Redis
     replies fast enough that the interpreter rarely preempts: 8 threads x 200 iterations over
     300-row rooms at the default 5 ms interval gave **0 failures** in 11 s.
     `sys.setswitchinterval(1e-6)` makes it reliable.
  3. **Rows in more than one partition**, so a clobber is observable as a wrong `room_id`.
- **Reproduction** (4 rooms of 50-70 rows, 8 threads, 60 iterations each, ~3-4 s):
  **3 of 3 runs failed**, 1, 1 and 5 detections. Verbatim failures:
  ```
  room d got ['d0000'..'d0005'] exp ['d0000'..'d0005'] wrong_room ['b','b','b','b','b','b'] n=6 limit=6
  room c got ['d0054','d0053','d0052','d0051'] exp ['d0069','d0068','d0067','d0066'] wrong_room ['d','d','d','d']
  room a got ['d0069','d0068','d0067','d0066','d0065'] exp ['d0059'..'d0055'] wrong_room ['c','c','c','c','c']
  ```
  The first line is the sharp one: the doc ids match what room `d` should return, and every row
  belongs to room `b`. Asserting on doc ids alone would have passed it. **The regression test
  must assert the partition field of every returned row.**
- **Confidence**: high.
- **Impact on plan**: fixes Task 6's test recipe (second indexed predicate, switch-interval knob,
  per-row partition assertion) and upgrades the `## Problem` framing from "wrong limits" to
  "cross-partition rows".

### spike-2: Does per-thread backing for the attributes fix it without breaking the public read?

- **Assumption**: "`filter_for_keys_set` may still populate `self` for legacy readers while the
  internal path reads from the returned state" (task brief) — i.e. compatibility and correctness
  are simultaneously achievable.
- **Method**: prototype — monkeypatched the eight names (`_sorted_field_order`,
  `_sorted_field_name`, `_pending_client_filters`, `_pushdown_limit`, `_pushdown_requested`,
  `_pushdown_fetched`, `_pushdown_partition`, `_pushdown_allowed`) onto `Query` as data
  descriptors backed by a per-instance `threading.local()`, changing no other line, then re-ran
  spike-1's harness.
- **Finding**: **0 failures in 3 of 3 runs** (2.2 s, 2.6 s, 2.6 s — also faster than the racing
  baseline, which burned time on contention). The public read survives:
  `Model.query.filter(..., limit=3).all()` followed by `Model.query._pushdown_limit` still
  returns `3` on the calling thread, which is what `tests/test_validity_field.py:802` asserts.
- **Confidence**: high for the mechanism; medium for the mypy delta, which the descriptor's
  typing decides and which spike-2 did not measure.
- **Impact on plan**: makes per-thread storage the recommended primary fix (Solution step 1)
  rather than one option among several.

## Data Flow

One `Model.query.filter(room_id="d", score__gte=0, bucket="x", order_by="-score", limit=6).all()`
on thread A, concurrent with the same shape for `room_id="b"` on thread B:

1. **Entry** — `QueryBuilder.all()` → `Query._execute_filter` (`query.py:3011`), one shared
   `Query` instance for both threads.
2. **Arm** — `self._pushdown_allowed = _allow_pushdown` (`3047`). *Shared.*
3. **Key query** — `filter_for_keys_set` (`2471`): resets the seven (`2511-2517`), runs the
   sorted-field range read, writes `_sorted_field_order` / `_sorted_field_name` /
   `_pushdown_*` (`2572-2579`). *Shared. Blocking Redis I/O both before and after.*
4. **Second predicate** — the non-sorted field loop (`2580-2606`) issues another blocking Redis
   call. **This is the window.** B's step 3 can complete entirely inside A's step 4.
5. **Intersect** — `_sorted_field_order` narrowed to the agreed keys (`2643-2646`). A narrows
   *B's* list against *A's* intersection, or vice versa.
6. **Order + bound** — `_execute_filter` reads `_sorted_field_order` (`3058`) and
   `_bound_keys_before_hydration` slices it (`3071-3073`).
7. **Hydrate** — `Query.get_many_objects` loads whatever keys survived. Rows from B's partition
   are hydrated for A.
8. **Guard** — `_short_result_action(len(objects), _allow_pushdown, self._snapshot_pushdown_state())`
   (`3092-3094`) snapshots *now*, long after the write, so it can also be reading B's numbers.
9. **Output** — A returns B's rows.

After this plan, steps 2-8 read and write storage private to the calling thread, and steps 6-8
additionally read an explicit carrier produced by step 3 rather than re-reading `self`.

## Architectural Impact

- **New dependencies**: none. `threading` and `dataclasses` are already imported by `query.py`.
- **Interface changes**: none that a caller can observe. `filter_for_keys_set(**kwargs) -> set`
  keeps its signature, its return type and its side effect of leaving the seven attributes
  readable — *on the calling thread*. One private helper gains a `state` return
  (`_filter_keys_with_pushdown`, already present) and `_execute_filter` / `Query.count` /
  `Query.async_count` start consuming it.
- **Coupling**: decreases. The internal read path stops depending on instance attributes; the
  attributes become a compatibility view for the four external readers.
- **Data ownership**: the bookkeeping moves from "owned by the `Query` instance" to "owned by
  the call, with a per-thread compatibility mirror".
- **Reversibility**: high. The descriptors are eight class-level assignments; deleting them and
  the `state=` arguments restores today's behavior exactly.

## Appetite

**Size:** Medium

**Team:** Solo dev, plus one reviewer for the concurrency test

**Interactions:**
- PM check-ins: 1 (confirm the "keep the attributes, scope them per-thread" call rather than
  deprecating them)
- Review rounds: 1-2 (the test's timing knob and the mypy delta on the descriptor are the two
  things a reviewer will push on)

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| Redis/Valkey reachable | `redis-cli -n 2 ping` | the suite and the repro both need a live server |
| Test DB is not 0 | `python -c "import os;assert os.environ.get('POPOTO_TEST_DB','2')!='0'"` | DB 0 is the live agent store on this machine (CLAUDE.md, #577) |
| Editable install resolves to this checkout | `python -c "import popoto,os;print(os.path.realpath(popoto.__file__))"` | worktree hazard 1 in CLAUDE.md |
| Full extras installed | `python -c "import numpy, sentence_transformers, mcp"` | a `.[dev]`-only venv silently deselects ~95 tests |
| #610 merged | `gh pr list --search "610" --state merged --json number` | ordering constraint (see Freshness Check) |

## Solution

### Key Elements

- **Per-thread storage for the eight bookkeeping names** — the mechanism that actually closes the
  race, including the sync-vs-async case that #571's `asyncio.Lock` structurally cannot cover.
- **The `_PushdownState` carrier on the sync path** — `_execute_filter` stops re-reading `self`
  and reads the state returned alongside the keys, so the invariant "no per-call bookkeeping is
  read off the shared instance" is visible in the code rather than implied by the storage.
- **`async_count` moved onto the carrier** — it is the one reader that crosses a thread boundary
  today and would silently break under per-thread storage.
- **A concurrency regression test that fails on `main`** — with the three trigger ingredients
  spike-1 identified, otherwise it passes both ways and proves nothing.

### Flow

Two threads, one model → each calls `Model.query.filter(...)` → each gets its own arm flag, its
own key list, its own bound and its own short-result decision → each returns only rows from its
own partition, in its own direction, at its own limit. A single-threaded caller reading
`Model.query._pending_client_filters` right after `filter_for_keys_set` sees exactly what it sees
today.

### Technical Approach

**1. Back the eight names with per-thread storage (the fix).**

Add one private data descriptor to `query.py` and bind it to the eight names on `Query`:

```python
_T = TypeVar("_T")


class _PerThreadAttr(Generic[_T]):
    """Per-thread storage for bookkeeping that lives on a shared Query.

    `Query` is one instance per model class (models/base.py:505), so an
    attribute written by filter_for_keys_set is visible to every thread that
    queries that model. Each of these names is written and read within a single
    call, so per-thread storage preserves every existing read while removing the
    cross-thread aliasing. Defaults are produced per thread, never shared.
    """

    def __init__(self, name: str, default: "Callable[[], _T] | _T"):
        ...

    def __get__(self, obj, objtype=None) -> _T: ...
    def __set__(self, obj, value: _T) -> None: ...
```

Storage is a `threading.local()` created lazily in `obj.__dict__["_pushdown_tls"]`, so it is
per-`Query`-instance **and** per-thread; two model classes never share, and a dead thread's
values are collected with the thread. `_pending_client_filters` and `_pushdown_partition` take
`dict` as a factory so a default is never shared across threads.

The eight names and their defaults:

| Name | Default | Written at |
|---|---|---|
| `_sorted_field_order` | `None` | `2511`, `2573`, `2645`, `3044` |
| `_sorted_field_name` | `None` | `2512`, `2574`, `3045` |
| `_pending_client_filters` | `{}` (factory) | `2513`, `2633` |
| `_pushdown_limit` | `None` | `2514`, `2576`, `2353` |
| `_pushdown_requested` | `0` | `2515`, `2577`, `2354` |
| `_pushdown_fetched` | `0` | `2516`, `2578`, `2355` |
| `_pushdown_partition` | `{}` (factory) | `2517`, `2579` |
| `_pushdown_allowed` | `False` | `2284`, `2288`, `3040`, `3047`, `3051` |

`_pushdown_allowed` is in the list because it is armed on one thread and read by
`_sorted_pushdown_args` (`query.py:2443`) inside the same call: a concurrent
`filter(..., _allow_pushdown=False)` retry currently disarms other threads' in-flight queries.
Its `try/finally` disarm becomes per-thread too, which is strictly more correct.

Replace the four bare class-level annotations at `query.py:1953-1956` with the descriptor
assignments and keep the explanatory comment.

**2. Thread the carrier through the sync path (the invariant).**

- `_execute_filter` (`query.py:3011`) replaces its inline arm / `filter_for_keys_set` /
  `finally`-disarm block (`3046-3051`) with the existing
  `db_keys_set, state = self._filter_keys_with_pushdown(_allow_pushdown, kwargs)`. That helper
  (`query.py:2271`) already does arm → query → `_snapshot_pushdown_state()` with no yield inside,
  and returns the same `(keys, state)` pair the async path consumes.
- The Q-object branch (`3038-3045`) keeps calling `_evaluate_filter_args` and then clearing
  `_sorted_field_order` / `_sorted_field_name`, and builds its state with
  `state = self._snapshot_pushdown_state()` **after** the clear, so the Q path's "ordering is
  unreliable" decision travels in the carrier like everything else.
- The three post-key reads become state reads: `3053-3058` (`state.sorted_field_order`),
  `3089` (`state.pending_client_filters`), `3109` (`state.pending_client_filters`).
- `_bound_keys_before_hydration` is called with `state=state`, which takes its
  already-implemented non-`None` branch and writes the bound back into the carrier instead of
  `self` (`query.py:2341-2355`). **Consequence to keep in mind:** with `state` supplied,
  `self._pushdown_limit` is no longer updated by the pre-hydration slice. See "Compatibility"
  below — this is the one behavior a legacy reader can notice, and
  `tests/test_validity_field.py:802/810/815` reads exactly that attribute.
- `_short_result_action(len(objects), _allow_pushdown, state)` replaces the
  `self._snapshot_pushdown_state()` argument at `3092-3094`. The retry recursion is unchanged.
- `Query.count` (`query.py:3245`) switches to
  `db_keys, state = self._filter_keys_with_pushdown(False, kwargs)` and reads
  `state.pending_client_filters` at `3281`. Passing `False` preserves today's behavior exactly:
  `count()` never armed the pushdown, and it must not start — a bound tally is #610's bug.
- `Query.async_count` (`query.py:3692`) switches its `await to_thread(self.filter_for_keys_set, **kwargs)`
  at `3712` to `await to_thread(self._filter_keys_with_pushdown, False, kwargs)` and reads
  `state.pending_client_filters` at `3713`. **This is mandatory, not optional:** under per-thread
  storage the current code reads the event-loop thread's default `{}` and would silently return
  the unfiltered key count for any plain-`Field` filter. No existing test covers it (Task 5 adds
  one).
- `async_filter` (`query.py:3532`) is already fully on the carrier. Do not touch it.

**3. Compatibility: keep the attributes, scope them per-thread, deprecate nothing now.**

The four in-repo readers outside `_execute_filter` (`QueryBuilder.fuse` at `query.py:1181`,
`transfer/export.py:277`, `recipes/context_assembler.py:2368`, and `tests/test_validity_field.py`)
all read `_pending_client_filters` or `_pushdown_limit` on the same thread that called
`filter_for_keys_set`, so per-thread storage preserves all four verbatim. That is the whole
argument for this shape over a deprecation shim: **zero call-site churn, zero downstream break,
and the race closed at the storage layer where it actually lives.**

A deprecation shim was considered and rejected. It would have to warn on read, which fires on
three internal library paths popoto itself calls, and it would break unknown downstream readers
of a documented-by-usage attribute for no correctness gain — the shim does not close the race,
because the *write* is what races.

The one visible delta: after step 2, the pre-hydration slice writes its bound into the carrier
instead of `self`, so `Model.query._pushdown_limit` reads `None` for the query shape where only
the hydration was bounded (the Redis-side bound at `2576` still writes `self`, because
`filter_for_keys_set` still populates the instance). `tests/test_validity_field.py:802` asserts
`== 3` on a query that takes the Redis-side bound (spike-1's harness confirms condition 5
passes for `score__gte` + `order_by="score"`), so it is expected to keep passing.

**Revised by critique (Scope & Value, CONCERN-2): do not leave this to a test result.** Make
`_bound_keys_before_hydration` mirror the bound into `self` **unconditionally**, on both the
`state is None` and the `state is not None` branches, in addition to writing the carrier. Once
storage is per-thread the mirror is a per-thread write and costs nothing, and it removes the one
observable delta this plan would otherwise have — the compatibility contract then holds by
construction rather than by a passing assertion. Concretely, keep the three existing
`self._pushdown_limit / _pushdown_requested / _pushdown_fetched` assignments in place and add the
matching writes to `state` beside them, rather than replacing one with the other. Still run
`tests/test_validity_field.py` first as the tripwire, and record in the PR body that the
unconditional-mirror branch was taken.

## Failure Path Test Strategy

### Exception Handling Coverage

No `except Exception: pass` blocks exist in the touched code. `_filter_keys_with_pushdown`'s
`try/finally` (`query.py:2284-2288`) swallows nothing — it disarms and re-raises. Task 3 asserts
the disarm still happens on the exception path: a `filter(bogus_param=1)` raises `QueryException`
from `filter_for_keys_set` (`query.py:2625-2628`) and `_pushdown_allowed` must be `False`
afterwards on that thread.

### Empty/Invalid Input Handling

- `filter()` with no filter params returns `set(self.keys())` early (`query.py:2519-2521`) before
  any bookkeeping is populated; the carrier then carries the reset defaults. Covered by the
  existing suite (`Model.query.filter().all()` paths) and asserted in Task 3.
- A thread that never ran a query reads the descriptor defaults (`None` / `{}` / `0` / `False`),
  not another thread's leftovers. This is a behavior change and a strictly better one; Task 3
  pins it so it cannot silently regress to shared storage.
- `_pending_client_filters` empty vs non-empty drives the #594 limit suppression at
  `query.py:3089`; Task 6's harness includes one thread using a plain-`Field` filter so the
  suppression path is exercised under contention.

### Error State Rendering

Both `logger.warning` texts in `_short_result_action` (`query.py:2371-2394`) must remain
byte-identical; `tests/test_sorted_range_pushdown.py` asserts on `caplog` at four sites. This
plan does not edit that helper, only what is passed to it, so the four assertions are the
tripwire that the state handed in still carries the same numbers.

## Test Impact

| Test | Disposition |
|---|---|
| `tests/test_validity_field.py:802`, `810`, `815` (`_pushdown_limit` after a sync `filter`) | **UPDATE only if red.** Expected to pass unchanged (these take the Redis-side bound, which still writes `self`). If red, take the mirror branch in Solution step 3 rather than editing the assertion — the attribute is the compatibility contract. |
| `tests/test_validity_field.py:832`, `853` (sets `_pushdown_allowed` / `_sorted_field_order`, then calls `_sorted_pushdown_args`) | **No change.** Single-threaded write-then-read; the descriptor keeps it working. Confirms the descriptor is a *data* descriptor (assignment must reach the storage, not `obj.__dict__`). |
| `tests/test_sorted_range_pushdown.py` (whole file, sync + async, incl. the four `caplog` assertions) | **No change, must pass unchanged.** This is the primary regression gate for step 2. |
| `tests/test_async.py` incl. `test_async_count` | **No change.** `TestJob.status` is a `KeyField`, so `async_count`'s client-filter branch is never entered — which is why Task 5 adds the missing case rather than relying on this file. |
| `tests/test_client_side_filter.py` | **No change.** Covers the `_pending_client_filters` suppression the carrier now transports. |
| `tests/test_hybrid_retrieval.py:360` | **No change.** Exercises the `context_assembler.py:2368` reader on the calling thread. |
| `tests/test_stress.py` (`ThreadPoolExecutor` sections at 747/769/822) | **No change expected.** Re-run explicitly — it is the only existing multi-threaded coverage and the closest thing to a canary. |
| `tests/test_query_thread_safety.py` | **CREATE** — Tasks 3, 5, 6. |

## Rabbit Holes

- **Making `Model.query` return a fresh `Query` per access.** It would dissolve the whole problem
  class, and it changes `Model.query is Model.objects` identity, breaks
  `tests/test_validity_field.py`'s white-box reads, and forces a `ModelOptions` rebuild or cache
  on every attribute access. Out of scope, and not obviously desirable.
- **A per-`Query` lock around the whole filter.** Correct and trivial, and it serializes every
  concurrent query on a model class — turning a shared-state bug into a throughput regression on
  the exact multi-threaded workload the fix is for.
- **Auditing every other attribute on `Query` for the same hazard.** `_geo_distances` /
  `_geo_distance_unit` have it too (see `## Race Conditions`, residual). They cannot take the
  same fix: `async_filter` initializes them on the event-loop thread (`query.py:3560-3561`) and
  `filter_for_keys_set` mutates them in the `to_thread` worker, so per-thread storage would
  break async geo queries outright. They need the carrier, which means a signature change to a
  public method. Separate issue (Task 9).
- **Removing the per-loop `asyncio.Lock`** (`query.py:113`) as now-redundant. It is redundant
  once storage is per-thread — `to_thread` workers each get their own storage, and one worker
  thread never runs two hops at once — but removing it buys nothing and re-opens a settled
  review. Leave it.
- **Chasing a repro without `sys.setswitchinterval`.** spike-1 spent three escalations on it:
  4 threads x 400 iterations, then 8 threads x 200 over 300-row rooms (11 s), both clean. The
  interval knob is the ingredient, not a bigger loop.

## Risks

### Risk 1: The mypy delta on the descriptor

**Impact:** `Query._pushdown_limit` is annotated `Optional[int]` today (`query.py:1953`). Replaced
by a descriptor, mypy infers the attribute type from `__get__`'s return. A loosely typed
descriptor turns every one of the ~30 read sites into `Any` (silent, no error) or, worse,
produces new errors at the arithmetic sites in `_short_result_action`.
**Mitigation:** make the descriptor `Generic[_T]` with `__get__(self, obj: "Query", objtype: object = None) -> _T`
and an `__get__` overload returning `_PerThreadAttr[_T]` for `obj is None`, then declare each
binding with an explicit parameter (`_pushdown_limit: "_PerThreadAttr[Optional[int]]" = _PerThreadAttr("_pushdown_limit", None)`).
Measure the base-vs-branch delta in the same venv and record the redis-py version beside the
number (CLAUDE.md: a bare count is not a delta). Target: **0**.

### Risk 2: The concurrency test is timing-dependent and could flake

**Impact:** a test that fails once in fifty on unrelated CI load is worse than no test.
**Mitigation:** the test asserts a **positive invariant** ("every returned row belongs to the
queried partition, and the window matches"), so a fixed build passes regardless of scheduling —
only a broken build can fail it. The switch-interval knob only raises the *detection* rate on a
broken build; it cannot create a false positive on a fixed one. `sys.setswitchinterval` is
restored in a `finally`. Runtime measured at 2.2-2.6 s post-fix.

### Risk 3: The pre-hydration bound stops writing `self._pushdown_limit`

**Impact:** `tests/test_validity_field.py` and any downstream white-box reader observe `None`
where they saw an int, for the query shape where only the hydration was bounded.
**Mitigation:** covered explicitly in Solution step 3 with a decision procedure and a fallback
(keep the `self` mirror on the sync path). Task 2's validation runs
`tests/test_validity_field.py` **before** anything else, so this surfaces in the first green/red
signal rather than at review.

### Risk 4: A `threading.local` per `Query` instance grows unboundedly

**Impact:** memory, in a process with many short-lived threads.
**Mitigation:** it does not grow. `threading.local` storage is owned by the thread and freed with
it; the per-instance object holds no strong reference to any thread. The bound is
`models x live threads x 8 small values`. Worth one sentence in the descriptor docstring.

## Race Conditions

### Race 1: Concurrent `filter()` on one model class clobbers the sorted key list

**Location:** `src/popoto/models/query.py:2511-2517` (reset) and `2572-2579` (populate) versus
`3053-3058` (read); shared instance created at `src/popoto/models/base.py:505`.
**Trigger:** thread A is inside the non-sorted-field Redis call at `query.py:2580-2606` (or any
other blocking call between its populate and its read) when thread B resets and repopulates.
**Data prerequisite:** `_sorted_field_order` must still hold the key list produced by *this*
call's range read when `_execute_filter` reads it at `3058`.
**State prerequisite:** `_pushdown_allowed` must reflect *this* call's arm decision when
`_sorted_pushdown_args` consults it at `2443`.
**Observed:** rows from another partition returned to the caller — spike-1, 3 of 3 runs.
**Mitigation:** per-thread storage (Solution 1) makes A's and B's writes target different cells;
the carrier (Solution 2) additionally removes the read.

### Race 2: Concurrent `filter()` clobbers the bound, skipping the short-result re-read

**Location:** `query.py:2576-2578` and `2353-2355` (write) versus `3092-3094` (read via
`_snapshot_pushdown_state`).
**Trigger:** B's reset zeroes `_pushdown_limit` between A's bound and A's guard.
**Data prerequisite:** `_pushdown_limit` / `_pushdown_fetched` / `_pushdown_requested` must all
describe the same read when `_short_result_action` compares them.
**Consequence:** `short` computes `False`, the orphan re-read never fires, and the caller gets a
silently short answer. The inverse (reading a larger limit) forces a spurious full-range re-read
and logs a warning naming the wrong field.
**Mitigation:** same two layers. `_short_result_action` already takes a state object; step 2
hands it the one produced with the keys instead of a fresh `self` snapshot taken later.

### Race 3: A sync `filter()` clobbers an in-flight `async_filter`

**Location:** the sync writes above versus the `to_thread` hop at `query.py:3572`.
**Trigger:** any synchronous query on the same model while a coroutine's key query is in flight.
The per-loop `asyncio.Lock` (`query.py:113`) cannot cover it — the sync path never takes it.
This is the exposure #571 documented and deferred here.
**Mitigation:** per-thread storage closes it structurally: the `to_thread` worker and the calling
thread have separate cells. This is the specific reason storage, not just carrier-threading, is
required — carrier-threading alone leaves the *write* racing.

### Race 4: Two event loops on different threads

**Trigger:** each loop builds its own lock (`_PUSHDOWN_LOCKS` keyed by running loop), so
coroutines on loop A and loop B were never mutually serialized.
**Mitigation:** closed by the same per-thread storage, since distinct loops run on distinct
threads. Popoto documents no multi-loop pattern, so this was theoretical; it stops being a
question either way.

### Residual exposure (stated, not deferred silently)

- **`_geo_distances` / `_geo_distance_unit`** (`query.py:3035-3036` sync, `3560-3561` async;
  mutated inside `filter_for_keys_set` at `2559-2561`) race identically, and a clobber attaches
  another query's distances to this query's rows. They **cannot** take this plan's fix:
  `async_filter` writes them on the loop thread and `filter_for_keys_set` mutates them in the
  worker thread, so per-thread storage would break async geo queries. They need the carrier
  threaded through `filter_for_keys_set` itself, a public signature change. **Task 9 files the
  follow-up issue and links it from the PR body.** Exposure is unchanged from today's `main`.
- **Cross-process** concurrency needs no coverage: `self` is per-process.

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG — filed by Task 9]` Geo-distance bookkeeping (`_geo_distances`,
  `_geo_distance_unit`). It has the same hazard and a *different* fix (carrier through a public
  signature, not per-thread storage, because the async path writes and reads it on two different
  threads by design). Task 9 files the issue and the plan's tracking comment records the number;
  the anti-criterion in `## Verification` asserts this PR does not make them per-thread.
- `[ORDERED]` Merging before #610. `docs/plans/count_q_object_limit.md` is `Ready` and edits the
  same file at `query.py:1821-1822`. Its value is being a minimal localized diff; landing this
  first would force it to rebase across a `Query`-wide change. Human-gated event: #610's merge.
- Removing or deprecating the seven public-by-usage attributes. Explicitly rejected in Solution
  step 3 — a shim does not close the race and breaks four in-repo readers plus unknown downstream
  ones.
- Removing the per-running-loop `asyncio.Lock`. Redundant after this change, harmless, and out of
  scope (see Rabbit Holes). The anti-criterion in `## Verification` asserts it is still there.
- Any change to `async_filter`'s logic. It is already correct; only `async_count` moves.
- New pushdown conditions, or any change to which queries qualify for a bound.

## Update System

No update-system changes required. This is a library-internal change with no new dependency, no
config, no migration, and no on-disk or in-Redis format change. `/update` is unaffected.

## Agent Integration

No agent integration required. `Query` is reached through the existing model API; no MCP surface,
tool wrapper, or entry point changes. The agent-memory recipes that read
`_pending_client_filters` (`recipes/context_assembler.py:2368`) keep working unchanged, which is
itself an integration requirement and is covered by `tests/test_hybrid_retrieval.py`.

## Documentation

### Feature Documentation

- [ ] `docs/configuration.md` — the **Thread Safety** section (`400-460`). It currently lists
      only "Model instances should not be shared across threads" under *What is NOT Thread-Safe*,
      while the *What IS Thread-Safe* example runs `Counter.query.get(...)` across ten threads.
      Add that query bookkeeping on the shared per-model `Query` is now per-thread, so concurrent
      `filter()` / `count()` on one model class is safe, and name the one remaining exception
      (geo distances, once Task 9 has an issue number).
- [ ] `docs/multi-tenancy.md` — the isolation claim at line 13 ("complete isolation"). Add a note
      that partition isolation now holds under concurrent queries from multiple threads, since
      this bug broke exactly that promise.
- [ ] `docs/async.md` — #571's plan flagged sync-vs-async clobbering as residual. If the page
      carries any caveat about mixing sync and async queries on one model, update it; if it
      carries none, add nothing.
- [ ] `docs/query.md` — only if its pushdown notes state a concurrency caveat. Check
      `docs/query.md:1250-1260`; expected outcome is no change.

### External Documentation Site

- [ ] `mkdocs build` passes (both pages above are in the nav).

### Inline Documentation

- [ ] `_PerThreadAttr` docstring: why per-thread, what the storage lifetime is, and the memory
      bound (Risk 4).
- [ ] `filter_for_keys_set` docstring: one sentence stating that the bookkeeping it leaves behind
      is readable by the calling thread only.
- [ ] CHANGELOG.md — a **Fixed** entry. Lead with the cross-partition read, since that is the
      user-visible symptom, and note it is the sync-path completion of #571.

## Success Criteria

- [ ] `tests/test_query_thread_safety.py::test_concurrent_sync_filters_do_not_clobber_each_other`
      passes on the branch and **fails on `9c04b8c`** — demonstrated by running it against the
      base checkout and pasting the failure into the PR body. spike-1 measured 3 of 3 base runs
      failing; the PR must show at least one.
- [ ] Every row returned by a threaded `filter(room_id=X, ...)` has `room_id == X`. This is the
      assertion spike-1 proved is necessary: one observed failure had entirely correct doc ids
      and entirely wrong rooms.
- [ ] Two threads see independent `_pending_client_filters` and `_pushdown_allowed` under a
      barrier (deterministic, no timing knob) — Task 3.
- [ ] `async_count(plain_field="x")` returns the filtered count, not the key count — Task 5, new
      coverage, and the guard on the one cross-thread reader.
- [ ] `grep -n "getattr(self, \"_pending_client_filters\"" src/popoto/models/query.py` finds
      nothing inside `_execute_filter`, `Query.count` or `Query.async_count` — the internal path
      reads the carrier.
- [ ] Full suite green with `POPOTO_TEST_DB=2`, including `tests/test_validity_field.py`,
      `tests/test_sorted_range_pushdown.py` (all four `caplog` assertions unchanged),
      `tests/test_async.py`, `tests/test_client_side_filter.py`, `tests/test_hybrid_retrieval.py`
      and `tests/test_stress.py`.
- [ ] `ruff check src/` exits 0; `black --check src/ tests/` exits 0.
- [ ] `python scripts/mypy_ratchet.py --strict-env` exits 0 (the enforced gate). If the descriptor
      raises the `models` count, bank it with `--update` and say so in the PR body. Branch
      environment: redis-py 8.1.0, mypy 2.3.1 — matching `scripts/mypy_baseline.json`.
- [ ] Docs updated per `## Documentation`; `mkdocs build` clean.

## Team Orchestration

### Team Members

- **Builder (query-state)**
  - Name: `query-state-builder`
  - Role: the `query.py` change — descriptor, carrier threading, `async_count`
  - Agent Type: `builder` (Domain: async/concurrency + Redis/Popoto data)
  - Resume: true
- **Test engineer (concurrency)**
  - Name: `race-test-engineer`
  - Role: `tests/test_query_thread_safety.py`, including the must-fail-on-base proof
  - Agent Type: `test-engineer`
  - Resume: true
- **Validator**
  - Name: `pushdown-validator`
  - Role: full-suite + lint + mypy delta, base-vs-branch, environment recorded
  - Agent Type: `validator`
  - Resume: true
- **Documentarian**
  - Name: `thread-safety-doc`
  - Role: the four doc targets plus CHANGELOG
  - Agent Type: `documentarian`
  - Resume: true

## Step by Step Tasks

### 1. Add the per-thread descriptor

- **Task ID**: build-descriptor
- **Depends On**: none
- **Assigned To**: `query-state-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add `_PerThreadAttr` to `src/popoto/models/query.py` near `_PushdownState` (`query.py:80`),
  typed `Generic[_T]` per Risk 1.
- Bind it to all eight names on `Query`, replacing the bare annotations at `query.py:1953-1956`.
- Change nothing else. `filter_for_keys_set` keeps writing `self` exactly as today.
- **Validate**:
  ```bash
  POPOTO_TEST_DB=2 pytest tests/test_validity_field.py tests/test_sorted_range_pushdown.py \
    tests/test_client_side_filter.py -q
  ```
  All green. This step alone must not change any observable single-threaded behavior.

### 2. Thread the carrier through the sync path

- **Task ID**: build-carrier
- **Depends On**: build-descriptor
- **Assigned To**: `query-state-builder`
- **Agent Type**: builder
- **Parallel**: false
- `_execute_filter` (`query.py:3011`): use `_filter_keys_with_pushdown` on the non-Q branch,
  snapshot after the clear on the Q branch, and re-point the reads at `3053-3058`, `3089`,
  `3109` plus the `_bound_keys_before_hydration` and `_short_result_action` arguments.
- `Query.count` (`query.py:3245`): same, with `allow_pushdown=False`.
- Resolve the `_pushdown_limit` write-back question per Solution step 3 **by test result**, and
  record the branch taken in the PR body.
- **Validate**: same command as Task 1, plus
  `POPOTO_TEST_DB=2 pytest tests/test_async.py tests/test_hybrid_retrieval.py -q`. Run
  `tests/test_validity_field.py` **first** — it is the write-back tripwire.

### 3. Deterministic per-thread isolation tests

- **Task ID**: test-isolation
- **Depends On**: build-descriptor
- **Assigned To**: `race-test-engineer`
- **Agent Type**: test-engineer
- **Parallel**: true
- Create `tests/test_query_thread_safety.py`. No timing knob in these three:
  - two threads meeting at a `threading.Barrier`, each writing then reading
    `Model.query._pending_client_filters` and `_pushdown_allowed`, asserting each sees only its
    own value;
  - a thread that has run no query reads the descriptor defaults (`None` / `{}` / `0` / `False`),
    not another thread's leftovers;
  - `filter(bogus_param=1)` raises `QueryException` and leaves `_pushdown_allowed is False` on
    that thread (the `try/finally` disarm at `query.py:2284-2288`).
- **Validate**: `POPOTO_TEST_DB=2 pytest tests/test_query_thread_safety.py -q`

### 4. Move `async_count` onto the carrier

- **Task ID**: build-async-count
- **Depends On**: build-carrier
- **Assigned To**: `query-state-builder`
- **Agent Type**: builder
- **Parallel**: false
- `query.py:3712-3713`: `await to_thread(self._filter_keys_with_pushdown, False, kwargs)`, then
  read `state.pending_client_filters`.
- **Validate**: `POPOTO_TEST_DB=2 pytest tests/test_async.py -q` (must stay green; it does not
  cover the client-filter branch, which Task 5 adds).

### 5. `async_count` client-filter coverage

- **Task ID**: test-async-count
- **Depends On**: build-async-count
- **Assigned To**: `race-test-engineer`
- **Agent Type**: test-engineer
- **Parallel**: false
- Add to `tests/test_query_thread_safety.py` (or `tests/test_async.py`, builder's choice — state
  which): a model with a plain `popoto.Field` alongside an indexed field, rows split between two
  values of the plain field, and `await Model.query.async_count(plain_field="x")` asserting the
  filtered count, not the total. Confirm it fails against a build where Task 4 is reverted.
- **Validate**: `POPOTO_TEST_DB=2 pytest tests/test_query_thread_safety.py tests/test_async.py -q`

### 6. The concurrency regression test

- **Task ID**: test-race
- **Depends On**: build-carrier
- **Assigned To**: `race-test-engineer`
- **Agent Type**: test-engineer
- **Parallel**: false
- Add `test_concurrent_sync_filters_do_not_clobber_each_other` to
  `tests/test_query_thread_safety.py`. All three of spike-1's ingredients are required or the
  test passes on `main` and proves nothing:
  1. the model carries a partitioned `SortedField` **and** a second `IndexedField` that the
     query also filters on, so a Redis round trip separates populate from read;
  2. `sys.setswitchinterval(1e-6)` inside `try/finally`, restoring the previous value;
  3. four partitions of 50-70 rows, two threads each, distinct `limit` per thread and mixed
     `order_by` directions, ~60 iterations per thread.
- Assert, per iteration and per thread: the returned doc ids equal that thread's expected window
  **and** every returned row's partition field equals the queried partition. Include one thread
  filtering on a plain `Field` so #594's limit suppression is live under contention.
- Prove it fails on base: check out `9c04b8c` into a worktree, copy the test file in, run it,
  paste the failure into the PR body.
- **Validate**:
  ```bash
  POPOTO_TEST_DB=2 pytest tests/test_query_thread_safety.py -q          # branch: green, ~3s
  # and, in a base worktree at 9c04b8c, the same file: RED
  ```

### 7. Full validation

- **Task ID**: validate-all
- **Depends On**: test-race, test-isolation, test-async-count
- **Assigned To**: `pushdown-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every command in `## Verification`. Record the redis-py version and the venv path beside
  every count; reproduce any number a subagent reports before relaying it (CLAUDE.md).

### 8. Documentation

- **Task ID**: document-feature
- **Depends On**: validate-all
- **Assigned To**: `thread-safety-doc`
- **Agent Type**: documentarian
- **Parallel**: false
- The four targets in `## Documentation` plus the CHANGELOG entry and the two docstrings.
- **Validate**: `mkdocs build 2>&1 | tail -5`

### 9. File the geo-bookkeeping follow-up

- **Task ID**: file-geo-followup
- **Depends On**: validate-all
- **Assigned To**: `query-state-builder`
- **Agent Type**: builder
- **Parallel**: true
- File an issue: `_geo_distances` / `_geo_distance_unit` carry the same shared-instance hazard and
  need the carrier threaded through `filter_for_keys_set` (per-thread storage is wrong for them —
  `async_filter` writes on the loop thread at `query.py:3560-3561` and `filter_for_keys_set`
  mutates in the worker). Link it from the PR body and add the number to this plan's No-Gos.
- **Validate**: `gh issue view <N> --json number,title`

## Verification

| Check | Command | Expected |
|---|---|---|
| Race test green on branch | `POPOTO_TEST_DB=2 pytest tests/test_query_thread_safety.py -q` | exit code 0 |
| Pushdown suite unchanged | `POPOTO_TEST_DB=2 pytest tests/test_sorted_range_pushdown.py -q` | exit code 0 |
| Validity white-box reads survive | `POPOTO_TEST_DB=2 pytest tests/test_validity_field.py -q` | exit code 0 |
| Async suite unchanged | `POPOTO_TEST_DB=2 pytest tests/test_async.py -q` | exit code 0 |
| Client-filter + hybrid readers | `POPOTO_TEST_DB=2 pytest tests/test_client_side_filter.py tests/test_hybrid_retrieval.py -q` | exit code 0 |
| Existing threaded coverage | `POPOTO_TEST_DB=2 pytest tests/test_stress.py -q` | exit code 0 |
| Full suite | `POPOTO_TEST_DB=2 pytest -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| mypy ratchet (the enforced gate) | `python scripts/mypy_ratchet.py --strict-env` | exit code 0 |
| Internal path reads the carrier, not `self` | `sed -n '3011,3140p' src/popoto/models/query.py \| grep -c '_pending_client_filters'` | match count == 0 |
| `async_count` no longer reads `self` | `sed -n '3692,3740p' src/popoto/models/query.py \| grep -c 'getattr(self, "_pending_client_filters"'` | match count == 0 |
| Anti-criterion: geo state NOT made per-thread | `grep -c '_PerThreadAttr("_geo_distance' src/popoto/models/query.py` | match count == 0 |
| Anti-criterion: per-loop lock still present | `grep -c '_pushdown_lock_for_running_loop' src/popoto/models/query.py` | output > 1 |
| Anti-criterion: `filter_for_keys_set` signature unchanged | `grep -c 'def filter_for_keys_set(self, \*\*kwargs) -> set:' src/popoto/models/query.py` | output > 0 |
| Docs build | `mkdocs build 2>&1 \| tail -1` | exit code 0 |

Line ranges in the `sed` rows are against `9c04b8c` and will drift as the branch grows; the
validator should re-anchor them on the function boundaries rather than trusting the numbers.

**mypy gate procedure** (revised by critique — supersedes the plan-time raw-delta recipe):

The enforced gate in this repo is the **ratchet**, not a raw `mypy src/` count. `scripts/mypy_ratchet.py`
checks a per-package ceiling against `scripts/mypy_baseline.json`, which already records the current
environment (`redis==8.1.0`, `mypy==2.3.1`, `total: 1044`). Run:

```bash
python -c "import redis, mypy.version; print('redis-py', redis.__version__)"  # branch env: 8.1.0
python scripts/mypy_ratchet.py --strict-env
```

If the descriptor raises the `models` package count, regenerate the baseline with `--update` and say so
in the PR body rather than chasing a raw delta of 0. If a raw base-vs-branch delta is still wanted as
corroboration, the base is the **fork point**, not `9c04b8c`:

```bash
git checkout "$(git merge-base HEAD origin/main)"   # 3cf8c2d0, not 9c04b8c
```

`9c04b8c` is four `query.py`-touching commits behind the fork point (#624, #633, #634, and PR #625 for
#610); diffing against it folds those commits' typed surface into this plan's delta.

**Repro-script safety** (CLAUDE.md, #577): any ad-hoc script must export
`REDIS_URL=redis://localhost:6379/2` **before** `import popoto`, and assert the resolved DB:

```python
assert POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db") == 2
```

`POPOTO_TEST_DB` binds the pytest plugin only. DB 0 is the live agent store.

## Critique Results

Run 2026-09-06, FULL depth, independent roster (3 critics: Risk & Robustness, Scope & Value,
History & Consistency). Verdict: **READY TO BUILD (with concerns)** — 0 blockers, 3 concerns,
1 nit. Revision pass applied in this same commit; the plan text above already reflects it.

**Rebaselined:** this plan's `## Freshness Check` was written against `9c04b8c`. The branch's
actual fork point is `3cf8c2d0` (`git merge-base HEAD origin/main`), four `query.py`-touching
commits later: #624, #633, #634 and PR #625 (for #610). Every line number in the sections above
is stale by roughly +140; anchor on function names, not numbers. The mechanism survives the drift
intact — re-verified against the current tree: `class _PushdownState` L80,
`_pushdown_lock_for_running_loop` L113, `QueryBuilder.fuse` reader L1218, `QueryBuilder.count`
L1889, `_snapshot_pushdown_state` L2391, `_filter_keys_with_pushdown` L2409,
`_bound_keys_before_hydration` L2429, `_short_result_action` L2496, `filter_for_keys_set` L2609,
`_execute_filter` L3149, `Query.count` L3383, `async_filter` L3679, `async_count` L3847. The two
out-of-file readers moved: `transfer/export.py:305` (plan said 277) and
`models/base.py:506` (plan said 505); `recipes/context_assembler.py:2368` is unchanged.

**Ordering prerequisite satisfied:** #610 is CLOSED, merged as PR #625. PR #625 reworked
`QueryBuilder.count()` onto a shared `_execute(apply_limit=False, no_track=True)` seam, which
touches no pushdown bookkeeping; `Query.count` (L3383) still calls `filter_for_keys_set` and
reads `self._pending_client_filters` (L3419) exactly as this plan describes, so Solution step 2
applies unchanged.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness | The plan gates on a bare `mypy src/ \| tail -1` delta, but the repo's enforced gate is `scripts/mypy_ratchet.py` against `scripts/mypy_baseline.json`. Following the plan literally bypasses the ratchet's per-package `clean` allowlist and environment guard, risking a false green or a false red. | Revised `## Verification` "mypy gate procedure" and the matching Success Criterion. | The baseline JSON already records `redis==8.1.0`, `mypy==2.3.1`, `total: 1044` — i.e. this branch's environment, not the plan-time 7.1.1. Run `python scripts/mypy_ratchet.py --strict-env`; if the descriptor raises the `models` count, bank it with `--update` and commit the JSON rather than chasing a raw delta of 0. |
| CONCERN | Scope & Value | Solution step 2 (carrier-threading) is justified as making the invariant visible, not as closing a race step 1 does not already close — yet it is where Risk 3 (the `_pushdown_limit` write-back regression) originates. | Kept step 2, but removed its cost: the write-back is now unconditional rather than decided by test result. See Solution step 3. | Do not replace the `self._pushdown_limit / _pushdown_requested / _pushdown_fetched` assignments in `_bound_keys_before_hydration` with carrier writes — **add** the carrier writes beside them, on both the `state is None` and `state is not None` branches. Under per-thread storage the mirror is a per-thread write, so it is free and preserves `tests/test_validity_field.py:802/810/815` by construction. |
| CONCERN | History & Consistency | The mypy base-comparison command hardcodes `git checkout 9c04b8c`, now four `query.py`-touching commits behind the real fork point, folding #624/#633/#634/#625 into this plan's delta. | Revised `## Verification`; base is now the fork point. | `git merge-base HEAD origin/main` resolves to `3cf8c2d0`. `git log --oneline 9c04b8c..HEAD -- src/popoto/models/query.py` lists the four commits a `9c04b8c`-based diff would misattribute. |
| NIT | Scope & Value | Task 9 (file the geo follow-up issue) is pure ticket-filing given its own agent and dependency edge. | Accepted as-is; the task is two minutes and the No-Gos section depends on its issue number. | n/a (NIT) |

---

## Open Questions

None blocking. Two decisions were made in-plan rather than deferred, and either can be reversed
by a reviewer:

1. **Keep the seven attributes, scoped per-thread, rather than deprecating them.** Rationale in
   Solution step 3: four in-repo readers, a shim does not close the race, and a read-warning
   would fire on popoto's own paths.
2. **`_geo_distances` stays out of scope** with a follow-up issue (Task 9), because per-thread
   storage is the wrong fix for it and the right fix changes a public signature.
