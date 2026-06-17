# Restart Plan — Fix #420 (src.popoto DB-isolation) WITHOUT regressing the suite

> ## ✅ RESOLVED (2026-06-16)
>
> The regression is fixed and #420 is closed. Full suite is back to **main parity** —
> two consecutive clean runs: **1484 passed, 0 failed** (≤1 gate met with margin), and
> ~45 s vs the broken branch's 183 s (connection thrashing gone).
>
> **Confirmed mechanism (measured, NOT the plan's "wrong event loop" hypothesis).** A live
> mid-test probe (`sync_db=15 async_db=0 sync_keys=1 loaded=False`) showed the async write
> *does* land in DB 15 (it runs via `to_thread(create→save)` on the **sync** pool — the plan's
> "write lands in neither DB" was confounded by the session-teardown flush). The bug was a
> **DB-targeting mismatch**: `get_async_redis_db()` lazily rebuilt the async client from
> `REDIS_URL` (→ **DB 0**), ignoring the swapped sync DB, so async reads hit DB 0 while sync
> writes hit DB 15. Every async path resets `_POPOTO_ASYNC_REDIS_DB=None` to rebuild in-loop, so
> all of them funneled into the DB-0 lazy builder. A second, independent contributor surfaced once
> the async path was fixed: `tests/test_connection.py::test_set_redis_db_settings_with_valid_url`
> rebinds the global `POPOTO_REDIS_DB` to a **DB-0** connection and never restores it; on `main`
> this was masked (the `src.popoto` path was always DB 0), but after the collapse it drifted the
> shared connection off the test DB for every subsequent test.
>
> **Fix (3 changes, no `sys.meta_path` hook needed):**
> 1. `redis_db.get_async_redis_db()` now mirrors the *current* sync
>    `POPOTO_REDIS_DB.connection_pool.connection_kwargs` (host/port/db/auth) instead of re-reading
>    `REDIS_URL`. The async client always follows the sync DB — including the plugin's swap.
> 2. `pytest_plugin._popoto_reset_async` sets the async global to `None` (lazy in-loop rebuild)
>    instead of pre-creating an off-loop client. Removes the wrong-loop risk entirely.
> 3. `pytest_plugin._popoto_flush_db` re-asserts the test DB per-test when the connection has
>    drifted (cheap no-op unless a test rebound the connection). Honors the plugin's "isolation
>    just works" promise against any `set_REDIS_DB_settings()` caller, sync or async.
>
> The configure-time alias-collapse from `c8cea88` is kept as-is: it correctly makes `src.popoto`
> and `src.popoto.redis_db` the canonical objects, and lazily-imported `src.popoto.*` submodules
> still resolve `redis_db` through the aliased `sys.modules` entry — so the DB invariant holds
> without a `MetaPathFinder` (§4.2). The 0-failure suite confirms it empirically. The good pieces
> of `0158e4a` (hardened tripwire, `TestIsolatedDbSubprocess` proof) are retained and pass; the 3
> `TestAsyncReset`/`TestAsyncIntegration` plugin tests were updated to assert the new
> lazy-in-loop contract.
>
> Everything below is the original handoff, kept for provenance.

---

**Status:** PR #422 is on HOLD (do not merge). Its approach (Option D alias-collapse) is
verified to **regress the full suite from 1 → 78 failures**. This document is a self-contained
handoff so a fresh agent can restart. Read it fully before touching code.

**Issue:** #420 · **PR:** #422 (`session/pytest-plugin-isolation`, open, do-not-merge) ·
**Branch HEAD:** `0158e4a` · **Main:** `61f36aa` (release/v1.7.1)

---

## 1. The original bug (#420) — still real, still unfixed

`import popoto` and `import src.popoto` produce **two distinct module objects** for the same
physical files. The pytest plugin swaps only the `popoto` instance's Redis connection to the
test DB (15); the `src.popoto` instance keeps its import-time default (**DB 0**). So `Model.save()`
from the ~75 test files that `import src.popoto` lands in DB 0, is never flushed, and pollutes
later runs. This root cause is confirmed and unchanged.

## 2. What PR #422 tried, and the verified result

**Approach (Option D, commit `c8cea88`):** in `pytest_configure`, rewrite `sys.modules["src.popoto*"]`
to point at the canonical `popoto*` objects ("alias-collapse"), so only one instance exists; plus a
DB-0 "tripwire" fixture. Commits `5d691d6`/`da1558e` added regression tests; `0158e4a` (this session)
added an `isolated_db_subprocess` fix-proof and hardened the tripwire.

**Verified outcome (clean single-tree runs — see §5 for the exact protocol):**

| Tree | Full suite (excl. benchmarks), redis-py 7.1.1, default DB 15 | Time |
|------|--------------------------------------------------------------|------|
| **main `61f36aa`** | **1 failed**, 1408 passed | 108 s |
| **PR #422 `0158e4a`** | **78 failed**, 1406 passed | 183 s |

Main is green (the 1 failure is a known intermittent flake in
`test_subconscious_memory_integration.py`). **All ~77 extra failures are introduced by the fix**,
not pre-existing debt — the plan's original "Risk 1 / exposed-not-introduced" premise is **false**.

Introduced-failure clusters (clean run, `0158e4a`): `test_async.py` (24), `test_stream_consumer.py`
(12), `test_memory_lifecycle.py` (10), `test_pytest_plugin.py` (7 — including the plugin's **own**
`TestDatabaseIsolation::test_not_on_db_zero`/`test_on_test_db` failing mid-suite),
`test_list_field_capped.py` (6), `test_sorted_field_ordering.py` (5), `test_trajectory_memory.py` (4),
`test_indexed_fields.py` (3), `test_get_many.py` (3), `test_stress.py` (2), + 3 singletons.

## 3. Root cause of the REGRESSION (verified + leading hypothesis)

**Ruled OUT (by direct probe):** a static probe importing exactly like `test_async.py`
(`from src.popoto.redis_db import POPOTO_REDIS_DB`) confirms the collapse works: after
`pytest_configure`, `src.popoto is popoto`, `src.popoto.redis_db is popoto.redis_db`, both sync
connections are on **DB 15**, and the async global is shared and on **DB 15**. So this is **not** a
"fresh DB-0 module slipped through aliasing" bug, and **not** an async-leaks-to-DB-0 bug. (An
earlier PR comment claimed "async leaks to DB 0" — that was external-process noise in DB 0
[`email:relay:*`] misattributed; disregard it.)

**Verified symptom:** running one failing async test with both DBs flushed first, the async write
persists to **neither** DB 0 nor DB 15 (`db0=0`→only an unrelated external key; `db15=0`). The
async `Model.async_create()` silently no-ops; the subsequent `async_get()` returns `None`.

**Leading hypothesis (confirm before fixing):** the autouse fixture `_popoto_reset_async`
(`src/popoto/pytest_plugin.py:186-226`) **pre-creates** an async client at fixture-setup time
(synchronous context, outside the test's event loop):

```python
redis_db._POPOTO_ASYNC_REDIS_DB = aioredis.Redis(**async_kwargs)   # line 209, created off-loop
```

On **main**, `src.popoto`-imported async tests used a *separate* module global
(`src.popoto.redis_db._POPOTO_ASYNC_REDIS_DB`) that this fixture never touched, so
`get_async_redis_db()` (`src/popoto/redis_db.py:217`) created the client **lazily, inside the test's
own event loop** → worked. **After the collapse**, those tests are forced onto the fixture's
pre-created, wrong-loop client → async ops attach to a different loop and silently fail → writes
are lost. This explains why it is async-specific, why static wiring looks correct, and why writes
land nowhere.

**Open question for the rework agent:** confirm the loop hypothesis (instrument the async client:
log `id(asyncio.get_running_loop())` at write time vs. the loop the fixture client was bound to, or
use `redis-cli MONITOR` to see whether the write command ever reaches the server). Then verify the
**same** root cause explains the non-`test_async.py` clusters (`stream_consumer`, `get_many`,
`list_field_capped`, etc.) — some may be sync tests breaking for a *different* collapse-related
reason. Do not assume one mechanism covers all 77 until measured.

## 4. Recommended direction (design, not prescription)

The collapse idea is reasonable for the **sync** path. The failure is the **async** path's
loop-bound pre-created client. Two complementary fixes to evaluate:

1. **Make async resolution test-DB-aware and lazy/in-loop.** Have `_popoto_reset_async` set the
   async global to `None` (not a pre-created client) so `get_async_redis_db()` builds it lazily
   inside the test's loop — and make `get_async_redis_db()` resolve the **swapped sync DB**
   (`redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs["db"]`) instead of defaulting to
   DB 0 / `REDIS_URL` (`redis_db.py:257-275`). This removes the off-loop client entirely.

2. **Prefer a `sys.meta_path` import hook over configure-time `sys.modules` rewriting** for the
   collapse, so *lazily-imported* `src.popoto.*` submodules (loaded after `pytest_configure`) are
   redirected at import time too. The current loop only aliases modules already in `sys.modules` at
   configure time; a `MetaPathFinder` that maps `src.popoto[.x]` → `popoto[.x]` is robust to import
   order. (Flagged as a BLOCKER by the war-room critics — Adversary/Archaeologist/Skeptic — before
   build; never resolved.)

**Acceptance gate:** the rework is done only when the **faithful** full suite (§5) returns to
**main parity (≤ 1 failure)**. The single allowed failure is the pre-existing
`test_subconscious_memory_integration.py` flake — verify it's that one, not a new one.

Also keep this session's genuinely-good pieces from `0158e4a`: the hardened `_popoto_db0_tripwire`
(always-yields, closes its client) and the `TestIsolatedDbSubprocess` fix-proof. Re-confirm the
subprocess test passes under the reworked design.

## 5. Faithful verification protocol (REQUIRED — this is where hours were lost)

Local runs in this repo are confounded three ways. Use this exact recipe for any measurement:

1. **Worktree editable-install trap.** The shared `.venv` at the repo root editable-installs
   `popoto` from the **main** working tree, *not* from a worktree. Running `pytest` inside a
   worktree silently tests **unfixed** code. → Build a **worktree-local venv** from the tree under
   test:
   ```bash
   cd <worktree>
   uv venv .venv
   VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev]" 'redis==7.1.1' -q
   .venv/bin/python -c "import popoto; print(popoto.__file__)"   # must be THIS tree
   ```
2. **redis-py version trap.** `pyproject` declares `redis>=4.4.4` (no upper bound); a fresh install
   pulls **redis-py 8.0.0**, which breaks `Redis(**connection_pool.connection_kwargs)` (rejects the
   new `maint_notifications_pool_handler` kwarg) across the plugin and tests. Pin `redis==7.1.1` in
   the test venv (above), and consider adding `redis<8` to `pyproject` as a separate fix.
3. **Concurrency/contention trap.** Two suites against one Redis + one CPU corrupt timing-sensitive
   stress/async tests. Run **sequentially**, nothing else hitting Redis. Use a distinct
   `POPOTO_TEST_DB` only if you must overlap — but then `test_on_test_db` (asserts `db==15`) will
   fail spuriously; prefer default DB 15 and sequential runs.

Canonical command (sequential, complete failure list to a file you control):
```bash
cd <worktree>; export REDIS_URL="redis://localhost:6379"
redis-cli -n 15 flushdb >/dev/null
.venv/bin/python -m pytest -q --tb=line -p no:cacheprovider -m "not benchmark" \
  --ignore=tests/benchmarks > /tmp/run.txt 2>&1
grep -E '^(FAILED|ERROR)' /tmp/run.txt | sed -E 's/^(FAILED|ERROR) //; s/::.*//' | sort | uniq -c | sort -rn
grep -E 'failed|passed' /tmp/run.txt | tail -1
```
Establish the **main baseline** the same way in a throwaway `git worktree add .worktrees/baseline-main 61f36aa`
(its own venv, redis 7.1.1, default DB 15) before claiming any failure is introduced vs pre-existing.

**No GitHub test-CI exists** (only `deploy-docs`/`guard`/`release` workflows; `test-valkey.yml` /
`stress-tests.yml` referenced in CLAUDE.md are **not in the repo**). The only test gate is this local
protocol / `scripts/ci-local.sh` (which needs a worktree-local `.venv`). Whatever you ship must be
verified locally — nothing will catch a regression downstream.

## 6. Quick single-test diagnostics that paid off

```bash
# Is a regression introduced vs pre-existing? Compare one file across trees:
.venv/bin/python -m pytest tests/test_async.py -q --tb=no            # fix: 23 failed / main: 32 passed
# Where does a failing async write actually land? (flush both, run one, inspect)
redis-cli -n 0 flushdb; redis-cli -n 15 flushdb
.venv/bin/python -m pytest tests/test_async.py::test_async_save_with_pipeline -q
redis-cli -n 0 dbsize; redis-cli -n 15 dbsize    # both ~0 → write no-ops (not a DB-routing bug)
```

## 7. Out of scope (file separately, don't bundle)
- `redis` unpinned → redis-py 8 incompatibility (its own small PR: pin `redis<8`).
- `test_subconscious_memory_integration.py` intermittent flake (pre-exists on main).
- The src-layout itself allowing `import src.popoto` (structural; deferred in the original plan).

## 8. Definition of done
- [ ] Mechanism of the 77 introduced failures **measured and confirmed** (not assumed).
- [ ] #420 actually fixed: a model saved via `src.popoto` (sync **and** async) lands on the test DB,
      flushed between tests; `isolated_db_subprocess` proof passes.
- [ ] Faithful full suite at **main parity (≤1 failure)**, verified per §5, with a fresh main
      baseline run for comparison.
- [ ] PR body/comments corrected; no unverified claims (e.g. "N failures filed separately" must be
      true or removed).
