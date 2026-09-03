---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/571
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-03T10:29:52Z
---

# #571 — async_filter must apply the SortedField limit pushdown (bound parity with sync)

## Problem

`Query.async_filter` returns correct rows for a bounded SortedField query but reads the entire
range and hydrates every member: neither the Redis-side bound (`_sorted_pushdown_args`) nor the
pre-hydration key-list slice (`_bound_keys_before_hydration`) fires on the async path, so
`async_filter(..., limit=5)` costs the same as no limit — the exact cost #517 removed from the
sync path. Also, `_desc` is never threaded down, so a descending async query issues an ascending
`zrangebyscore`. Pure cost defect, not correctness (verified in the issue: identical result rows).

## Freshness Check

Re-verified 2026-09-03 against main at `d72d393` (baseline SHA). Every commit between the
previous baseline `16aa702` and `d72d393` touches only `docs/plans/*.md` (#595, #588, #596
planning traffic) — `git log 16aa702..d72d393 -- src/popoto/models/query.py
tests/test_sorted_range_pushdown.py` is empty. All references below were re-read at
`d72d393` and match exactly; no further drift.

Original check (2026-09-03, against main after #594 / `16aa702`): the issue's mechanism
holds; line numbers drifted under #594's query.py changes — corrected references:

- `_pushdown_allowed` is set only in `_execute_filter` (query.py:2875–2886, with try/finally
  reset) and gated in `_sorted_pushdown_args` (query.py:2278).
- `_bound_keys_before_hydration` (def at query.py:2197) has exactly one call site, in
  `_execute_filter` (query.py:2913).
- `async_filter` (query.py:3403) calls `await to_thread(self.filter_for_keys_set, **kwargs)`
  at query.py:3432 with neither mechanism armed, then hands the full key set to
  `_async_get_many_objects`.
- **New since the issue was filed (#594):** both paths now suppress the pre-hydration `limit`
  when `_pending_client_filters` is non-empty (query.py:2914–2929 sync, 3452–3461 async). The
  async pushdown must respect the same suppression — pushdown and key-slice must NOT bound when
  client-side filters are pending. **Settled at `d72d393` — no build-time re-verification
  needed:** `_sorted_pushdown_args` condition 5 (query.py:2293–2300) declines whenever any filter
  param survives the sorted field and its partitions — exactly the case that populates
  `_pending_client_filters` — and `_bound_keys_before_hydration` independently bails at
  query.py:2217 (`if getattr(self, "_pending_client_filters", None): return db_keys`). Both
  suppressions are unconditional on the helper, so reusing the helpers inherits #594's semantics
  for free. The build must NOT add a parallel check; it only adds the test that confirms the
  reused helpers fire (Task 3).
- `#559` is still **open** and its branch `test/559-pushdown-coverage` is still unmerged;
  `grep -rn 'pytest.mark.xfail\|pytest.xfail('` over `tests/` at `d72d393` finds **no** async
  pushdown xfail on main, so Step 4 below is still conditional, not yet actionable.
- No other plan in `docs/plans/` touches the async pushdown path (only #559's test-only plan,
  already in Prior Art). **No overlap.**

**Disposition: Minor drift** — claims hold, references corrected above; nothing further has
moved since the original check.

## Research

No relevant external findings — this is a pure in-repo parity fix against popoto's own
`Query` internals, with no external library, API, or ecosystem surface involved. Proceeding
on codebase context.

## Prior Art

- #517 (merged) — the sync pushdown implementation being mirrored.
- #559 (open, test-only) — plans a hard async result-parity assertion plus an
  `xfail(strict=True)` on the async hydration count referencing this issue. Its branch
  `test/559-pushdown-coverage` has NOT merged; if it merges first, flip that xfail off in this
  PR. If this merges first, #559 drops the xfail and asserts the bound directly.
- #594 — reshaped `_execute_filter`; the client-filter limit-suppression interplay above.

## Solution

Mirror `_execute_filter`'s bounding into `async_filter`, reusing the sync helpers (no forked
logic) — but **carry the pushdown bookkeeping in a per-call state object rather than reading it
off `self` after an await**. That last clause is the whole difference between this plan and the
naive port, and it is why the design is spelled out concretely below rather than left to build
time. See `## Race Conditions` for the reasoning.

0. **Introduce a per-call state carrier.** Add a small mutable dataclass, private to
   `query.py`:

   ```python
   @dataclass
   class _PushdownState:
       sorted_field_order: "Optional[list]" = None
       sorted_field_name: "Optional[str]" = None
       pending_client_filters: "dict[str, Any]" = field(default_factory=dict)
       pushdown_limit: "Optional[int]" = None
       pushdown_requested: int = 0
       pushdown_fetched: int = 0
       pushdown_partition: "dict[str, Any]" = field(default_factory=dict)
   ```

   These are exactly the seven attributes `filter_for_keys_set` writes onto `self`
   (query.py:2346–2352 reset, 2411–2414 populate) and that the post-hydration guard reads
   (query.py:2940–2949).

1. **Arm, execute, and snapshot inside one thread hop.** Add a sync helper that does the whole
   arm/execute/disarm/snapshot with no await inside it:

   ```python
   def _filter_keys_with_pushdown(self, allow_pushdown, kwargs) -> "tuple[Any, _PushdownState]":
       self._pushdown_allowed = allow_pushdown
       try:
           db_keys = self.filter_for_keys_set(**kwargs)
       finally:
           self._pushdown_allowed = False
       return db_keys, _PushdownState(
           sorted_field_order=self._sorted_field_order,
           sorted_field_name=self._sorted_field_name,
           pending_client_filters=dict(self._pending_client_filters or {}),
           pushdown_limit=self._pushdown_limit,
           pushdown_requested=self._pushdown_requested,
           pushdown_fetched=self._pushdown_fetched,
           pushdown_partition=dict(self._pushdown_partition or {}),
       )
   ```

   `async_filter` calls `await to_thread(self._filter_keys_with_pushdown, _allow_pushdown, kwargs)`
   in place of today's `await to_thread(self.filter_for_keys_set, **kwargs)` (query.py:3432),
   **while holding the per-loop lock described in `## Race Conditions`**. This threads `_desc`
   automatically: `_sorted_pushdown_args` computes it and `filter_for_keys_set` passes
   `{"_limit": ..., "_desc": ...}` into `field.filter_query` (query.py:2390–2394), so a
   descending async query issues `zrevrangebyscore` with no extra wiring.

   The sync path is NOT refactored onto this helper (No-Gos forbid behavior change to sync); it
   keeps its inline try/finally at query.py:2881–2886.

2. **Bound the key list from the state object.** Give `_bound_keys_before_hydration` a
   keyword-only `state: "Optional[_PushdownState]" = None`. When `state` is None it reads and
   writes `self` exactly as today (sync path unchanged, byte-for-byte in behavior); when a state
   is supplied it reads `pending_client_filters` / `pushdown_limit` / `sorted_field_order` /
   `_sorted_field_name` from it and writes `pushdown_limit` / `pushdown_requested` /
   `pushdown_fetched` back into it (the writes at query.py:2242–2244). `async_filter` passes
   `q_objects=None` (it has no Q-object path) and its own `_allow_pushdown`, so the helper's
   `if q_objects or not allow_pushdown` guard reduces to the flag alone.

   Everything else `async_filter` reads after the await — the `_sorted_field_order` check at
   query.py:3440–3448 and the `_pending_client_filters` check at query.py:3452–3461 — must be
   re-pointed at the state object too. Those two reads are a *pre-existing* instance-state race
   on the async path; re-pointing them costs nothing and closes it as a side effect.

3. **Port the short-result re-read guard** (`_pushdown_limit` / `_pushdown_fetched` /
   `_pushdown_requested` orphan handling — sync at query.py:2936–2977) so a bounded async read
   cannot return short when orphaned index members eat the budget. Without this the async
   pushdown is a correctness regression, not an optimization.

   **Extract, do not duplicate** (the critique closed this open choice). The two branches are
   asymmetric — `_allow_pushdown and short and not exhausted` **re-reads** by recursing, while
   `short and orphans > 0` only warns — and the retry differs per path
   (`return self._execute_filter(...)` vs `return await self.async_filter(...)`). So the shared
   piece decides and logs only; each caller owns its own recursion:

   ```python
   def _short_result_action(self, n_objects, allow_pushdown, state) -> bool:
       """Emit the standard warnings; return True iff the caller must re-read unbounded."""
   ```

   It must emit both existing `logger.warning` texts **verbatim** — `tests/test_sorted_range_pushdown.py`
   asserts on `caplog` at lines 398 / 434 / 457 / 485 and those tests must pass unchanged. Its
   only inputs are the object count, the flag, and the state object; no I/O, no retry ownership.
   The sync call site replaces query.py:2940–2977 with a `_short_result_action(...)` call plus
   its existing `return self._execute_filter(..., _allow_pushdown=False, **kwargs)`.

   **Signature consequence:** the sync re-read works because `_execute_filter` takes an
   `_allow_pushdown` parameter kept out of `**kwargs`. `async_filter` today is
   `async def async_filter(self, **kwargs)` (query.py:3403) and forwards `**kwargs` straight into
   `filter_for_keys_set`, which raises `QueryException` on any non-field param. So the new
   parameter must be **keyword-only with a default**:

   ```python
   async def async_filter(self, *, _allow_pushdown: bool = True, **kwargs) -> list:
   ```

   and the retry is `return await self.async_filter(_allow_pushdown=False, **kwargs)`. Pass
   `_allow_pushdown` (never a literal `True`) as the third positional argument of
   `_bound_keys_before_hydration`, otherwise the retry re-applies the bound and returns the same
   short answer — the exact trap in the issue body's suggested one-liner
   `_bound_keys_before_hydration(db_keys_set, None, True, kwargs)`. Field names must start
   lowercase (CLAUDE.md), so there is no collision risk with a user filter kwarg. The internal
   caller `async_get` (query.py:3347) passes nothing and still type-checks.

4. `async_count` stays untouched (verified correct: full match count).

## Race Conditions

### The hazard

`Query` is instantiated **once per model class** — `new_class.objects = new_class.query = Query(new_class)`
(`src/popoto/models/base.py:505`). Every call to `Model.query.async_filter(...)` therefore shares
one `self`. `filter_for_keys_set` resets and then populates its bookkeeping on that shared object:
`_sorted_field_order` / `_sorted_field_name` / `_pending_client_filters` / `_pushdown_limit` /
`_pushdown_requested` / `_pushdown_fetched` / `_pushdown_partition` (query.py:2346–2352 reset,
2411–2414 populate).

The sync guard at query.py:2940–2977 reads those attributes with **no yield point** between the
write and the read, so on the sync path the convention is sound by accident of straight-line
execution. The async port has two awaits in between — `to_thread(...)` (query.py:3432) and
`_async_get_many_objects(...)` (query.py:3456) — so a second coroutine can run to completion in
the gap and overwrite all seven fields. The failure modes:

- Coroutine A's `_pushdown_limit` is cleared or lowered by B ⇒ `short` computes False ⇒ **A skips
  the re-read and silently returns short results** — the precise regression this plan exists to
  prevent.
- Coroutine A reads B's larger `_pushdown_limit` ⇒ spurious full-range re-read (a silent perf
  cliff, plus a misleading warning naming the wrong field).
- `_sorted_field_order` swapped mid-flight ⇒ A hydrates B's key list ⇒ **wrong rows**.

This is not hypothetical: `docs/async.md:221–231` promotes exactly the concurrency pattern that
triggers it — two `Model.query.async_filter(...)` calls run concurrently under a single
`asyncio.gather`. (Corrected citation: the earlier pointer to `docs/async.md:327–342` was wrong;
that block gathers `async_save()` calls inside a sequential loop and is not the hazard. The
hazard claim is unchanged — only the line reference moves.) Two of the seven reads
(`_sorted_field_order` at query.py:3440, `_pending_client_filters` at query.py:3452) already
happen after the await **today**, so a narrower version of this bug is already live on the async
path; this plan closes it rather than widening it.

### The fix (two parts, both required)

**1. Snapshot into locals — never read `self._pushdown_*` after an await.**
`_filter_keys_with_pushdown` (Solution 1) returns `(db_keys, _PushdownState)` from inside the
single `to_thread` hop. Every downstream consumer — `_bound_keys_before_hydration`,
`_short_result_action`, the order-by and client-filter branches — takes the state object, not
`self`. After the first await, `async_filter` never touches `self._pushdown_*`,
`self._sorted_field_order`, or `self._pending_client_filters` again.

**2. Serialize arm-through-snapshot with a per-loop `asyncio.Lock`.**
Snapshotting alone is insufficient: two coroutines' `to_thread` calls run in *different worker
threads concurrently*, and `filter_for_keys_set` does blocking Redis I/O that releases the GIL, so
B's reset (query.py:2346–2352) can land between A's populate and A's snapshot. The critical
section is arm → `filter_for_keys_set` → snapshot, i.e. the entire body of
`_filter_keys_with_pushdown`. Hold the lock across the single `await to_thread(...)` and release
immediately after; the second await (`_async_get_many_objects`) is outside it and stays fully
concurrent, so throughput on the hydration leg — the expensive leg — is unaffected.

**Lock construction: per running loop, created lazily, never at import.**
A `Lock()` created at class-definition or import time binds the loop it is first awaited on and
raises on any other. This repo already has that exact bug's workaround: `redis_db.py:100` builds
`_async_redis_lock` at import, and `tests/test_async.py:14–26` carries an autouse fixture that
reassigns it per test because pytest-asyncio creates a fresh loop each test. Do not add a second
thing that needs that fixture. Instead key the lock by the running loop, guarded for
thread-safety by a plain `threading.Lock` (no I/O under it, so the cost is a few hundred ns):

```python
# module level in query.py
_PUSHDOWN_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)
_PUSHDOWN_LOCKS_GUARD = threading.Lock()

def _pushdown_lock_for_running_loop() -> "asyncio.Lock":
    loop = asyncio.get_running_loop()
    with _PUSHDOWN_LOCKS_GUARD:
        lock = _PUSHDOWN_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _PUSHDOWN_LOCKS[loop] = lock
        return lock
```

The `WeakKeyDictionary` self-cleans as loops are collected, so a per-test loop leaves nothing
behind and no fixture is required. Scope note: this is a module-level lock, not per-`Query`.
Per-`Query` would be finer-grained but has to live on the shared instance and gains little — the
critical section is short and the contended case (many concurrent `async_filter` calls on one
model) is exactly the case a per-`Query` lock would serialize anyway.

### Residual exposure (stated, not deferred silently)

- **Sync-vs-async.** A synchronous `Model.query.filter(...)` running on another thread
  concurrently with an `async_filter` can still clobber the shared attributes mid-flight. The
  async lock cannot cover it, because the sync path does not take it. Closing this requires
  moving the sync path's bookkeeping off `self` as well — a behavior-affecting change to
  `_execute_filter` that this plan's No-Gos forbid. **Action: file a follow-up issue** ("move
  `Query` pushdown bookkeeping off shared instance state on the sync path") and link it from the
  PR body. The exposure is unchanged from today's `main`; this plan does not widen it.
- **Multiple event loops in separate threads.** Each loop gets its own lock, so coroutines on
  loop A and loop B are not mutually serialized. Same residual as above, same follow-up. Popoto
  has no documented multi-loop pattern, so this is theoretical.
- **Not covered:** cross-process concurrency needs no coverage here — `self` is per-process.

### Regression test

`asyncio.gather` of N concurrent `async_filter` calls on the *same model* with **different**
partitions and limits, over a dataset seeded with orphaned index members so the short-result
guard is live. Assert each coroutine gets exactly its own correct rows and its own full count.
Run it with enough iterations to be meaningfully load-bearing rather than incidentally passing,
and confirm it **fails** against a deliberately un-snapshotted variant before accepting it.

## Test Impact

| Test | Impact |
|------|--------|
| `tests/test_sorted_range_pushdown.py:398/434/457/485` (caplog assertions on both warning texts) | Must pass **unchanged**. The `_short_result_action` extraction has to emit both messages verbatim; these four are the tripwire for that. |
| Rest of `tests/test_sorted_range_pushdown.py` (sync) | Must pass unchanged — No-Gos forbid sync behavior change. `_bound_keys_before_hydration` keeps its `state=None` → read/write `self` path for exactly this reason. |
| `tests/test_async.py` | Must pass unchanged; `async_get` (query.py:3347) is the only internal `async_filter` caller and gets `_allow_pushdown`'s default. |
| New async section in `tests/test_sorted_range_pushdown.py` | 6 tests — see Task 4. Each needs an explicit `@pytest.mark.asyncio` (`pyproject.toml` declares no `asyncio_mode`; house pattern is `tests/test_async.py:38`) and an async-connection-reset autouse fixture **replicated in this file** (modelled on `tests/test_async.py:14–26` — it is module-local and cannot be imported into autouse) and **composed with the existing `clean_docs` autouse fixture at tests/test_sorted_range_pushdown.py:39**, or the cached async client leaks across loops. |
| #559's `xfail(strict=True)` on async hydration count | Not on `main` at `d72d393`. If it lands before build, convert to a hard assertion (Task 4). |

### Failure Path Test Strategy

Each failure mode gets a test that fails without the fix:

1. **Bound never fires** → async hydration count assertion with a **lower** bound (see Task 3);
   a vacuous 0-count cannot pass.
2. **Wrong direction** → assert `zrevrangebyscore` (not `zrangebyscore`) is the call issued.
3. **Short result from orphans** → orphan-density dataset; assert full correct row set returned
   and the re-read warning logged.
4. **Retry re-applies the bound** → assert the retry path returns the complete set, not the same
   short answer (guards against passing a literal `True` for `allow_pushdown`).
5. **Concurrent clobber** → the `gather` test above.
6. **#594 suppression lost** → pending plain-field client filter; assert correct rows, no
   truncation.

## Step by Step Tasks

1. **Add `_PushdownState` + `_short_result_action` + the per-loop lock helper** (Solution 0, 3,
   and `## Race Conditions`). `_short_result_action` is a pure decision-and-log function: it
   emits both existing warning texts verbatim and returns `bool` (re-read needed). Rewire the
   sync call site at query.py:2940–2977 to use it, keeping its own
   `return self._execute_filter(..., _allow_pushdown=False, **kwargs)` recursion. This is a
   **mechanical extraction with zero behavior change** — the decision is settled, not left to
   build time.
   Validate: `POPOTO_TEST_DB=11 pytest tests/test_sorted_range_pushdown.py -q` — all green,
   including the four caplog assertions at lines 398/434/457/485, with no edits to that file yet.

2. **Add `_filter_keys_with_pushdown` and thread `state` through `_bound_keys_before_hydration`**
   (Solution 1–2). `state` is keyword-only, defaults to `None` = today's `self` behavior.
   Validate: same command as Task 1 — still green (sync path untouched by construction).

3. **Wire `async_filter`** (Solution 1–3): keyword-only `_allow_pushdown: bool = True`, lock-held
   `to_thread(self._filter_keys_with_pushdown, ...)`, all post-await reads re-pointed at the
   state object, `_bound_keys_before_hydration(db_keys_set, None, _allow_pushdown, kwargs, state=state)`,
   `_short_result_action` + `return await self.async_filter(_allow_pushdown=False, **kwargs)`.
   Validate: `POPOTO_TEST_DB=11 pytest tests/test_async.py -q` — green (no async pushdown tests
   yet; this proves nothing regressed).

4. **Add the async test section** to `tests/test_sorted_range_pushdown.py` — six tests, each
   `@pytest.mark.asyncio`.

   **Fixture: replicate, do not import.** `tests/test_async.py:14–26` (`flush_redis`) is a
   *module-local* `autouse` fixture — importing it from another module does not make it autouse
   there, and pytest will not collect it. The builder must **write a new async-connection-reset
   fixture inside `tests/test_sorted_range_pushdown.py`** that performs the same two resets
   (`redis_db_module._POPOTO_ASYNC_REDIS_DB = None` and
   `redis_db_module._async_redis_lock = asyncio.Lock()`), and must **compose it with the file's
   existing autouse fixture `clean_docs` at tests/test_sorted_range_pushdown.py:39** rather than
   assuming either one covers the other: `clean_docs` flushes but does not reset the async
   connection, and blindly adding a second `flushdb()` fixture double-flushes around every sync
   test in the file. Preferred shape: extend `clean_docs` itself (or add a separate autouse
   fixture that only does the async reset, no flush), then confirm the *existing* sync tests in
   the file still pass unchanged. Without the async reset, the cached async client leaks across
   pytest-asyncio's per-test loops and the new tests fail with
   "Future attached to a different loop".

   The six tests:
   - **(a) Hydration is bounded.** Add an `AsyncHydrationCounter` alongside the existing
     `HydrationCounter` (tests/test_sorted_range_pushdown.py:63–80): patch
     `redis.asyncio.client.Pipeline.hgetall`, **not** `redis.client.Pipeline.hgetall` —
     `_async_get_many_objects` hydrates on the async pipeline (query.py:3686–3688), so the sync
     counter records **0** and a bare `assert count < POPULATION` would pass whether or not the
     bound fires. Assert **both** bounds:
     `limit <= counter.count <= limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`. The lower
     bound is what makes the headline criterion falsifiable.

     **Scope the counter explicitly — `hgetall` is not the only hydration path.**
     `_async_get_many_objects` hydrates via `pipeline.hgetall` only in the no-`values=` branch
     (query.py:3686–3688); when `values=` is passed it hydrates with `pipeline.hmget`
     (query.py:3672–3680). An `AsyncHydrationCounter` that patches `hgetall` alone therefore
     reads **0** for any `values=` query — silently vacuous, the same trap as patching the sync
     pipeline. Do one of the two, and say which in a comment on the counter class:
     (i) **preferred** — patch **both** `redis.asyncio.client.Pipeline.hgetall` and
     `redis.asyncio.client.Pipeline.hmget` and sum the calls, so the counter stays valid if a
     `values=` case is ever added; or (ii) patch `hgetall` only and add a docstring/comment
     stating "valid only for queries without `values=`; a `values=` query hydrates via `hmget`
     and would count 0." None of the six tests here use `values=`, so either is correct today —
     but the choice must be recorded, not implicit.
   - **(b) Range call is bounded and direction-correct.** The range read is issued on the *sync*
     client inside `to_thread`, so patch `redis.client.Redis.zrangebyscore` /
     `zrevrangebyscore`. Assert a descending query hits `zrevrangebyscore` with a `num` bound,
     and that the ascending case hits `zrangebyscore`.
   - **(c) Async/sync result parity** on identical data across limits and directions.
   - **(d) Orphan density.** Delete hashes out from under index members so the bounded read comes
     up short; assert the full correct row set is returned and the re-read warning is logged.
   - **(e) #594 suppression.** A pending unindexed plain-field filter: assert correct rows and no
     truncation.
   - **(f) Concurrency.** The `asyncio.gather` regression test from `## Race Conditions`.
   Validate: `POPOTO_TEST_DB=11 pytest tests/test_sorted_range_pushdown.py -q`.

5. If #559's xfail exists on `main` by build time, convert it to a hard assertion in this PR and
   remove the marker. (Conditional — absent at `d72d393`.)

6. **File the follow-up issue** named in `## Race Conditions` (sync-path bookkeeping off shared
   instance state) and link it from the PR body.

7. CHANGELOG entry (perf/fixed).

## No-Gos

- No behavior change to sync pushdown or to `async_count`.
- No new pushdown conditions — parity with the sync condition set only.

## Risks / Rabbit Holes

- **Shared-state flags on `self`** — *no longer a "move on" item; it is the blocker this plan
  resolves.* `_pushdown_allowed` and the seven bookkeeping attributes are instance state on a
  class-level `Model.query` object (`models/base.py:505`). The sync try/finally convention is
  only sound because sync has no await between write and read; copying it onto the async path
  produces silent short results. The design is pinned in `## Race Conditions` — snapshot into a
  `_PushdownState` inside the single `to_thread` hop, serialize that hop with a per-running-loop
  `asyncio.Lock`, never read `self._pushdown_*` after an await. Residual sync-vs-async exposure
  is documented there and gets a follow-up issue (Task 6), not silence.
- **Lock construction is the sharp edge.** An `asyncio.Lock()` built at import or
  class-definition time binds one loop and raises on any other — `redis_db.py:100` has exactly
  that shape and needs `tests/test_async.py:14–26` to paper over it. Build lazily, keyed by the
  running loop, in a `WeakKeyDictionary` guarded by a `threading.Lock`.
- **Extraction touches live sync code.** `_short_result_action` rewrites query.py:2940–2977,
  which the No-Gos declare off-limits for *behavior* change. The extraction is mechanical and its
  tripwire is the four caplog assertions (tests/test_sorted_range_pushdown.py:398/434/457/485) —
  if any warning text drifts by a character, they fail. Do Task 1 and confirm green *before*
  touching the async path.
- **Event-loop discipline**: the helpers run inside `to_thread` on the sync client; keep the
  async client usage confined to `_async_get_many_objects` as today. Consequence for tests:
  hydration counts are on `redis.asyncio.client.Pipeline`, range calls are on `redis.client.Redis`
  — patching the wrong one is the vacuous-assertion trap in Task 4(a).

## Success Criteria

- All six new async tests green (Task 4), and each demonstrably fails against the unfixed code —
  a test that passes both ways proves nothing.
- **Falsifiable hydration bound:** `AsyncHydrationCounter` patching `redis.asyncio.client.Pipeline.hgetall`
  reports `limit <= count <= limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN` for a bounded
  async query against a population much larger than `limit`. Both bounds asserted; a 0-count
  no-op must fail. (The previous single-sided `count < POPULATION` form was unfalsifiable — the
  sync counter reads 0 on the async path regardless.)
- Descending async query issues `zrevrangebyscore` (asserted on `redis.client.Redis`, where the
  range read actually happens).
- Concurrency regression test (`asyncio.gather`, Task 4f) green, and confirmed failing against an
  un-snapshotted variant.
- Sync suite unchanged: `tests/test_sorted_range_pushdown.py` and `tests/test_async.py` pass with
  no edits to existing sync assertions, including the four caplog checks.
- Full non-slow suite green; `black src/ tests/` clean; `ruff check src/` exits 0 (per CLAUDE.md;
  note `docs/sdlc/do-sdlc.md` claims ruff is absent — CLAUDE.md and `.github/workflows/lint.yml`
  are authoritative, run it); mypy delta 0 vs main measured in the *same* environment and
  redis-py major version, with the version recorded alongside the number (see CLAUDE.md's
  worktree verification notes — a bare error count is not a delta).
- Test runs use `POPOTO_TEST_DB=11` in this pipeline (three pipelines share this machine; DB 15
  contention produces phantom failures). Never DB 0 — live agent store.

## Verification

Run in the worktree `/Users/valorengels/src/popoto/.worktrees/sdlc-571`, after
`pip install -e '.[dev,embeddings,benchmark,mcp]'` and confirming the editable install resolves
to *this* checkout (CLAUDE.md, worktree hazard 1):

```bash
# 1. Targeted suites (the gate for Tasks 1-4)
POPOTO_TEST_DB=11 pytest tests/test_sorted_range_pushdown.py tests/test_async.py -q

# 2. Full non-slow suite
POPOTO_TEST_DB=11 pytest -q -m "not slow"

# 3. Lint + format
ruff check src/          # must exit 0
black --check src/ tests/

# 4. mypy delta — base vs branch, SAME env, record the redis-py version
python -c "import redis; print('redis-py', redis.__version__)"
mypy src/ 2>&1 | tail -1                      # branch
git stash && git checkout main && mypy src/ 2>&1 | tail -1   # base, same venv
```

Report every count with its environment and redis-py version attached; a bare number is not a
result (CLAUDE.md). Reproduce any subagent-reported metric before relaying it.

## Documentation

- CHANGELOG.md. `docs/query.md`'s pushdown/perf notes if they state the async caveat (check
  and update).
- `docs/async.md` — the concurrent-`async_filter`-under-`gather` pattern at **lines 221–231**
  (not 327–342, which gathers `async_save()` in a sequential loop) is now explicitly supported
  for bounded `async_filter`; if the page carries any caveat about concurrent `async_filter` or
  about limits not applying, update it.

## Critique Results

Verdict: **NEEDS REVISION** (2026-09-03, run `336cfd30`). Three FULL lenses (Risk & Robustness,
Scope & Value, History & Consistency) against verified source at `main`; all plan line
references re-checked and confirmed.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | risk/consistency | Porting the short-result guard onto shared instance state makes concurrent `async_filter` calls return wrong/short results. `Query` is one instance per model class (`models/base.py:505`), and `filter_for_keys_set` writes `_pushdown_limit`/`_pushdown_requested`/`_pushdown_fetched`/`_pushdown_partition`/`_sorted_field_order` on `self` (`query.py:2348–2352`, `2411–2414`). The sync guard reads them with no yield in between (`query.py:2940–2975`); the async port must read them after two awaits (`to_thread` at 3432, then `_async_get_many_objects`), so a second coroutine — the `asyncio.gather` pattern `docs/async.md:327–342` explicitly promotes — can clobber the state mid-flight and either skip the re-read (short result: the exact regression this plan forbids) or re-read spuriously. The plan's Risks section forecloses the fix ("mirror the sync convention and move on") without noticing that convention is only sound because sync has no await between write and read. Plan also has no Race Conditions section. | Needs revision: new `## Race Conditions` section | Do not read `self._pushdown_*` / `_sorted_field_order` after the second await. Snapshot into locals immediately on return from `await to_thread(self.filter_for_keys_set, **kwargs)` and pass the locals to the shared guard helper. Snapshot alone is insufficient (two coroutines' `to_thread` calls run concurrently against the same `self`), so serialize arm-through-snapshot with a lazily created per-`Query` `asyncio.Lock` (never created at class-definition time — that binds a loop at import). Add a `gather()` regression test. State any residual sync-thread exposure explicitly rather than deferring it silently. |
| CONCERN | risk/user | The planned async command-count assertion cannot observe async hydration and passes vacuously. The sync technique is `HydrationCounter` (`tests/test_sorted_range_pushdown.py:63–80`), which patches the **sync** `redis.client.Pipeline.hgetall`; `_async_get_many_objects` hydrates via the `redis.asyncio` pipeline, so the counter records 0 and `assert counter.count < POPULATION` passes whether or not the bound fires — making the headline success criterion unfalsifiable. | Needs revision: Task 3 + Success Criteria bullet 2 | Patch `redis.asyncio.client.Pipeline.hgetall` for hydration counts and assert a **lower** bound too (`limit <= count <= limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN`) so a zero-count no-op cannot pass. The range call itself is issued on the sync client inside `to_thread`, so direction assertions must patch `redis.client.Redis.zrangebyscore`/`zrevrangebyscore`. The file has no async tests and `pyproject.toml` declares no `asyncio_mode`, so each new test needs an explicit `@pytest.mark.asyncio` (house pattern: `tests/test_async.py`). |
| CONCERN | risk | `async_filter` cannot take `_allow_pushdown` the way `_execute_filter` does. The sync guard recurses via `self._execute_filter(..., _allow_pushdown=False, **kwargs)` (`query.py:2965–2970`), which works because that parameter is explicit and kept out of `**kwargs`. `async_filter(self, **kwargs)` (`query.py:3403`) forwards `**kwargs` straight into `filter_for_keys_set`, which raises `QueryException` on any non-field param. The issue body's literal suggestion `_bound_keys_before_hydration(db_keys_set, None, True, kwargs)` hardcodes `allow_pushdown=True`, which would re-apply the bound on the retry and return the same short answer. | Needs revision: Solution 3 / Task 2 wording | `async def async_filter(self, *, _allow_pushdown: bool = True, **kwargs)`; pass `_allow_pushdown` (never a literal `True`) as the third positional arg of `_bound_keys_before_hydration` and as the retry gate. Internal caller `async_get` (`query.py:3347`) still type-checks — the parameter is keyword-only with a default. |
| CONCERN | scope/consistency | Task 1 leaves the central design decision ("extract helpers if a clean seam exists; otherwise mirror") to build time. The two outcomes have different review surfaces and risk: extraction rewrites live sync code (`query.py:2940–2975`) that the No-Gos declare off-limits for behavior change; duplication creates a second copy of the very guard whose subtlety is the point of the issue. A reviewer cannot tell which PR they are getting. | Needs revision: Task 1 | Pick extraction and bound it: a pure decision helper, no I/O, no retry ownership. The retry differs (`return self._execute_filter(...)` vs `return await self.async_filter(...)`), so the shared helper must decide and log only — `_short_result_action(self, objects, allow_pushdown, push_limit, fetched, requested) -> bool`, emitting both existing `logger.warning` texts verbatim so `tests/test_sorted_range_pushdown.py:398/434/457/485` (which assert on `caplog`) keep passing unchanged. |
| CONCERN | history | Plan omits the repo's standard `## Race Conditions`, `## Test Impact` / `## Failure Path Test Strategy`, and `## Verification` sections that sibling plans carry (`computed_sort.md`, `bm25_first_class_retrieval_mode.md`). No task carries a runnable validation command, and Success Criteria state outcomes without the commands/environment that produce them — the measurement-provenance problem CLAUDE.md calls out. | Needs revision: document structure | Add all three; Race Conditions is where the blocker above is resolved. Verification commands: `pytest tests/test_sorted_range_pushdown.py tests/test_async.py -q` with `POPOTO_TEST_DB=11`, `ruff check src/`, and a base-vs-branch `mypy src/` in the same redis-py minor version, recording the version alongside the number. |
| NIT | history | Freshness Check's final bullet asks the builder to re-verify something already settled: `_sorted_pushdown_args` condition 5 (`query.py:2317–2323`) declines whenever an unindexed plain-field param survives, and `_bound_keys_before_hydration` independently bails on `_pending_client_filters` (`query.py:2217–2218`). Both suppressions are unconditional on the helper, so reusing the helpers gets #594's semantics for free. | Optional cleanup | Drop the "verify condition 5 covers it" ask; keep the test that confirms the reused helpers fire. |

### Revision Response (2026-09-03, run `336cfd30`)

All six findings resolved. Nothing deferred.

| Severity | Finding | Resolution |
|----------|---------|------------|
| BLOCKER | Shared instance state read after awaits | **Resolved in the new `## Race Conditions` section.** Design chosen explicitly: (1) per-call `_PushdownState` snapshot taken inside the single `to_thread` hop by a new `_filter_keys_with_pushdown`, so `self._pushdown_*` / `_sorted_field_order` / `_pending_client_filters` are **never** read after an await — this also closes the two pre-existing post-await reads at query.py:3440/3452; (2) because two `to_thread` calls run in different worker threads concurrently and `filter_for_keys_set` releases the GIL on Redis I/O, snapshotting alone is insufficient, so arm→execute→snapshot is serialized by a **lazily created, per-running-loop** `asyncio.Lock` held in a `WeakKeyDictionary` guarded by a `threading.Lock` — never constructed at import (`redis_db.py:100` is the anti-pattern this avoids, and it needs `tests/test_async.py:14–26` to compensate). The hydration await stays outside the lock, so concurrency on the expensive leg is unaffected. The Risks bullet that said "mirror the sync convention and move on" is **deleted** and replaced with a pointer to the new section. Residual sync-vs-async and multi-loop exposure is stated explicitly with a follow-up issue as Task 6. `asyncio.gather` regression test added as Task 4(f), with the requirement that it be confirmed failing against an un-snapshotted variant. |
| CONCERN | Command-count assertion unfalsifiable | **Resolved in Task 4(a) + Success Criteria.** New `AsyncHydrationCounter` patches `redis.asyncio.client.Pipeline.hgetall` (verified: async hydration is at query.py:3686), not the sync `redis.client.Pipeline.hgetall` the existing `HydrationCounter` uses (tests/test_sorted_range_pushdown.py:63–80). Assertion is two-sided — `limit <= count <= limit + Defaults.SORTED_PUSHDOWN_OVERFETCH_MARGIN` — so a 0-count no-op fails. Direction assertions moved to `redis.client.Redis.zrangebyscore`/`zrevrangebyscore` (Task 4b), since the range read runs on the sync client inside `to_thread`. Explicit `@pytest.mark.asyncio` and the `tests/test_async.py:14–26` connection-reset fixture required per test (no `asyncio_mode` in `pyproject.toml`). |
| CONCERN | `async_filter` cannot take `_allow_pushdown` | **Resolved in Solution 3.** Pinned to `async def async_filter(self, *, _allow_pushdown: bool = True, **kwargs)` — keyword-only with a default, so it stays out of `**kwargs` and never reaches `filter_for_keys_set`. Retry is `return await self.async_filter(_allow_pushdown=False, **kwargs)`, and `_allow_pushdown` (never a literal `True`) is passed as `_bound_keys_before_hydration`'s third positional arg; the issue body's `..., None, True, kwargs)` one-liner is called out by name as the trap. `async_get` (query.py:3347) unaffected. Failure Path Test Strategy item 4 tests exactly this. |
| CONCERN | Task 1 defers extract-vs-duplicate to build time | **Resolved: extraction, and it is bounded.** `_short_result_action(self, n_objects, allow_pushdown, state) -> bool` — a pure decision-and-log helper, no I/O, no retry ownership; each caller keeps its own recursion because the sync and async retries differ. Both `logger.warning` texts must be emitted verbatim, with tests/test_sorted_range_pushdown.py:398/434/457/485 named as the tripwire. Task 1 is now a standalone step ending in a green sync suite *before* the async path is touched. |
| CONCERN | Missing standard sections / no runnable commands | **Resolved.** Added `## Race Conditions`, `## Test Impact` (incl. `### Failure Path Test Strategy`), and `## Verification`. Every task now carries a `Validate:` command with `POPOTO_TEST_DB=11`. Success Criteria restated as measurements with the commands and environment that produce them, incl. the redis-py-version-qualified mypy delta. |
| NIT | Freshness Check asks builder to re-verify settled #594 semantics | **Resolved.** The bullet now reads "Settled at `d72d393` — no build-time re-verification needed" and states both suppression sites as fact; only the confirming test (Task 4e) remains. |

### Structural Check

| Check | Status | Detail |
|-------|--------|--------|
| Required sections | ~~PARTIAL~~ → PASS | Race Conditions, Test Impact + Failure Path Test Strategy, and Verification added in the revision pass |
| Task numbering | PASS | 1–7, no gaps or cycles (corrected 2026-09-03: the round-1 self-report read "1–5", written before Tasks 6–7 were added in the revision pass) |
| File paths exist | PASS | 4 of 4 |
| Line references | PASS | All Freshness Check line numbers verified verbatim against `main` |
| Task validation commands | ~~FAIL~~ → PASS | Every task in the revised list carries a `Validate:` command; `## Verification` carries the full gate sequence |
| Cross-references | PASS | Every Success Criterion maps to a task |
| Prior art status | PASS | #517 MERGED, #594 MERGED, #559 OPEN with branch unmerged — matches plan wording |

### Critique Round 2 — Verification Pass (2026-09-03, run `336cfd30`)

Verdict: **READY TO BUILD (with concerns)**. Scope was verification of the round-1 findings only,
not a new design pass. Every claim below was re-read from source in the worktree
`.worktrees/sdlc-571` at `24a04ff` (not from the main checkout, which is on `session/sdlc-596`
and carries a stale 5.5 KB copy of this plan).

| Round-1 finding | Status | Verification |
|---|---|---|
| BLOCKER — shared instance state read across awaits | **RESOLVED** | `models/base.py:505` confirms one `Query` per model class. `## Race Conditions` pins the fix concretely: `_PushdownState` snapshot taken inside a single `to_thread` hop, arm→execute→snapshot serialized by a per-running-loop `asyncio.Lock` in a `WeakKeyDictionary` guarded by a `threading.Lock`, never built at import. The anti-pattern it avoids is real and cited correctly — `redis_db.py:100` builds `_async_redis_lock` at import and `tests/test_async.py:14–26` exists solely to reassign it per test. Residual sync-vs-async and multi-loop exposure is stated, not deferred silently, with Task 6 filing the follow-up. Regression test 4(f) must be confirmed failing against an un-snapshotted variant. |
| CONCERN — command count unfalsifiable | **RESOLVED** | Confirmed `HydrationCounter` (tests/test_sorted_range_pushdown.py:63–83) patches the **sync** `redis.client.Pipeline.hgetall`, while async hydration runs `pipeline.hgetall` on the async pipeline (query.py:3686). Task 4(a) now patches `redis.asyncio.client.Pipeline.hgetall` with a two-sided bound, so a 0-count no-op fails. |
| CONCERN — `_allow_pushdown` cannot travel through `**kwargs` | **RESOLVED** | Confirmed `async_filter` is `async def async_filter(self, **kwargs)` (query.py:3403) today, and that `_bound_keys_before_hydration`'s third positional parameter is `allow_pushdown` (query.py:2197–2203). Plan pins keyword-only `*, _allow_pushdown: bool = True` and names the literal-`True` trap explicitly. |
| CONCERN — extract-vs-duplicate deferred to build | **RESOLVED** | Extraction chosen and bounded to decision-and-log. Both `logger.warning` texts confirmed at query.py:2949–2977, and the four caplog tests confirmed at tests/test_sorted_range_pushdown.py:398/434/457/485. All seven attributes the guard reads are present on `_PushdownState`. |
| CONCERN — missing standard sections | **RESOLVED** | `## Race Conditions`, `## Test Impact` (+ `### Failure Path Test Strategy`), and `## Verification` all present; every task carries a `Validate:` command. |
| NIT — Freshness Check re-verify ask | **RESOLVED** | Restated as settled fact at `d72d393`. |

Carried into build as concerns (none blocking). **All four were folded into the task text on
2026-09-03 (run `336cfd30`) — see the "Folded into" note under each; the builder does not need to
read this list to get them right:**

1. **Fixture reuse is a copy, not an import.** `tests/test_async.py:14–26` is a module-local
   autouse fixture; `tests/test_sorted_range_pushdown.py` has its own autouse fixture at line 39.
   The builder must replicate the async-connection/lock reset in the pushdown file and confirm it
   composes with the existing flush fixture rather than assuming it applies.
   **Folded into:** Task 4 preamble ("Fixture: replicate, do not import") and the Test Impact row
   for the new async section.
2. **`AsyncHydrationCounter` covers `hgetall` only.** `_async_get_many_objects` has a separate
   `hmget` path for `values=` (query.py:3672–3680). None of the six planned tests use `values=`,
   so the counter is sufficient as scoped — but a later `values=` test would silently read 0.
   **Folded into:** Task 4(a), "Scope the counter explicitly" — patch both `hgetall` and `hmget`,
   or document the `values=`-excluded scope on the counter class.
3. **Citation is off by one example.** The gather-over-`async_filter` pattern the Race Conditions
   section relies on is at `docs/async.md:221–231` (two concurrent `async_filter` calls under one
   `gather`), not `327–342`, which gathers `async_save()` calls inside a sequential loop. The
   hazard claim is correct; the pointer should be corrected when the section is next touched.
   **Folded into:** `## Race Conditions` → "The hazard" and `## Documentation` — both now cite
   221–231 and name 327–342 as the wrong pointer. Verified against source 2026-09-03.
4. **Stale self-report.** The Structural Check row above reads "Task numbering PASS 1–5"; the task
   list now runs 1–7. Cosmetic only.
   **Folded into:** the Structural Check "Task numbering" row, now reading 1–7.
