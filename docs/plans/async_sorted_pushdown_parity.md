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

Verified 2026-09-03 against main after #594 (`16aa702` + docs commits). The issue's mechanism
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
  client-side filters are pending (the sync `_sorted_pushdown_args` already declines via its
  condition set; verify condition 5 covers it and mirror exactly).

**Disposition: Minor drift** — claims hold, references corrected above.

## Prior Art

- #517 (merged) — the sync pushdown implementation being mirrored.
- #559 (open, test-only) — plans a hard async result-parity assertion plus an
  `xfail(strict=True)` on the async hydration count referencing this issue. Its branch
  `test/559-pushdown-coverage` has NOT merged; if it merges first, flip that xfail off in this
  PR. If this merges first, #559 drops the xfail and asserts the bound directly.
- #594 — reshaped `_execute_filter`; the client-filter limit-suppression interplay above.

## Solution

Mirror `_execute_filter`'s bounding into `async_filter`, reusing the sync helpers (no forked
logic):

1. Arm/disarm `_pushdown_allowed` around the `to_thread(self.filter_for_keys_set, ...)` call
   with the same try/finally shape and the same conditions `_execute_filter` uses (including
   threading `_desc` so descending queries issue a descending range read).
2. After key-set resolution, call `_bound_keys_before_hydration(...)` exactly as the sync path
   does (query.py:2913), including the #594 client-filter suppression semantics.
3. Port the short-result re-read guard (`_pushdown_limit` / `_pushdown_fetched` /
   `_pushdown_requested` orphan handling — sync at ~query.py:2930–2975) so a bounded async read
   cannot return short when orphaned index members eat the budget. Without this the async
   pushdown is a correctness regression, not an optimization. Prefer extracting the guard into a
   shared helper called by both paths over copy-paste.
4. `async_count` stays untouched (verified correct: full match count).

## Step by Step Tasks

1. Extract the sync pushdown arm/disarm + short-result guard into helpers if a clean seam
   exists; otherwise mirror with heavy cross-references.
2. Wire `async_filter` per Solution 1–3.
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

- New async pushdown tests green; full non-slow suite green; ruff/black clean; mypy delta 0 vs
  main (same environment).
- Command-count assertion proves the async bound fires (hydration count drops from full
  partition to bounded).

## Documentation

- CHANGELOG.md. `docs/query.md`'s pushdown/perf notes if they state the async caveat (check
  and update).
