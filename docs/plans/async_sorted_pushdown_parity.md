---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/571
last_comment_id: none
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

## Step by Step Tasks

1. Extract the sync pushdown arm/disarm + short-result guard into helpers if a clean seam
   exists; otherwise mirror with heavy cross-references.
2. Wire `async_filter` per Solution 1–3, including adding the `_allow_pushdown: bool = True`
   keyword to `async_filter`'s signature so the short-result guard can re-read unbounded.
3. Tests in `tests/test_sorted_range_pushdown.py` (async section): bounded async read issues a
   bounded, direction-correct range call and hydrates ≤ limit + overfetch margin (command-count
   assertion mirroring the sync suite's technique); async/sync result parity on the same data;
   orphan-density case on the async path (re-read guard returns full results); async pushdown
   declines when client-side plain-field filters are pending (returns correct rows, no
   truncation — the #594 regression shape); descending order issues `zrevrangebyscore`.
4. If #559's xfail exists on main by build time, convert it to a hard assertion in this PR.
5. CHANGELOG entry (perf/fixed).

## No-Gos

- No behavior change to sync pushdown or to `async_count`.
- No new pushdown conditions — parity with the sync condition set only.

## Risks / Rabbit Holes

- **Shared-state flags on `self`**: `_pushdown_allowed` etc. are instance state on a
  class-level `Model.query` object (see the audit's maintainability hazard). Do not attempt to
  fix that here — mirror the sync convention (set/reset in try/finally) and move on.
- **Event-loop discipline**: the helpers run inside `to_thread` on the sync client; keep the
  async client usage confined to `_async_get_many_objects` as today.

## Success Criteria

- New async pushdown tests green; full non-slow suite green; `black src/ tests/` clean;
  `ruff check src/` exits 0 (per CLAUDE.md; note `docs/sdlc/do-sdlc.md` claims ruff is absent —
  CLAUDE.md and `.github/workflows/lint.yml` are authoritative, run it); mypy delta 0 vs main
  measured in the *same* environment and redis-py major version (see CLAUDE.md's worktree
  verification notes — a bare error count is not a delta).
- Command-count assertion proves the async bound fires (hydration count drops from full
  partition to bounded).
- Test runs use `POPOTO_TEST_DB=11` in this pipeline (three pipelines share this machine; DB 15
  contention produces phantom failures). Never DB 0 — live agent store.

## Documentation

- CHANGELOG.md. `docs/query.md`'s pushdown/perf notes if they state the async caveat (check
  and update).

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

### Structural Check

| Check | Status | Detail |
|-------|--------|--------|
| Required sections | PARTIAL | Race Conditions, Test Impact, Verification missing vs. repo convention |
| Task numbering | PASS | 1–5, no gaps or cycles |
| File paths exist | PASS | 4 of 4 |
| Line references | PASS | All Freshness Check line numbers verified verbatim against `main` |
| Task validation commands | FAIL | No task carries a runnable validation command |
| Cross-references | PASS | Every Success Criterion maps to a task |
| Prior art status | PASS | #517 MERGED, #594 MERGED, #559 OPEN with branch unmerged — matches plan wording |
