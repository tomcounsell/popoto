---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/595
last_comment_id: none
---

# #595 — Warn once when the pytest plugin is inert but popoto is actually used

## Problem

PR #594 made `popoto.pytest_plugin` opt-in: it ships as a `pytest11` entry point (loads in
every project that depends on popoto) but does nothing unless `popoto_test_db` is set in the
pytest ini options or `POPOTO_TEST_DB` is exported. Refusing to flush a database nobody asked
for is correct. But a downstream suite that unknowingly relied on the old autouse behavior now:

- runs against whatever DB its `REDIS_URL` names — DB 0 by default (the #577/#584 hazard);
- keeps state between tests (nothing flushes);
- gets no warning, no log line, no failure pointing at the popoto version bump.

The failure presents days later as flaky cross-test pollution or tests mutating real data.
`docs/testing.md` is the only signal today, and only someone who already suspects the cause
reads it.

## Freshness Check

Verified 2026-09-03 against main `16aa702` (the #594 squash-merge, ~30 min before this plan):

- `src/popoto/pytest_plugin.py` matches every claim in the issue: `_resolve_test_db()`
  returns `None` when neither opt-in is set (lines 187–212); `_configure_test_db` early-returns
  on `None` (line 136–137, "Not opted in: leave the developer's connection alone");
  `_popoto_test_db` and `_popoto_flush_db` both early-return on `None` (lines 228–230, 262–264).
- No warning of any kind exists on the inert path — confirmed by reading the whole plugin.
- Related issue #549 (plugin test coverage of the inert path) is OPEN; #584 CLOSED via #594.

**Disposition: Unchanged.** The issue was filed against the #594 branch which is now main.

Re-verified 2026-09-03 (second pass) against main `c7fc167`: `src/popoto/pytest_plugin.py`
is 358 lines and unchanged since #594; there is still no `pytest_unconfigure` /
`pytest_sessionfinish` hook in the module (the plan adds the first one); #549 is still OPEN;
no `xfail` markers exist in `tests/test_pytest_plugin.py`, so nothing needs converting.

## Spike Results

### spike-1: the pool-instance `get_connection` seam actually works and is silent on import

- **Assumption**: wrapping `redis_db.POPOTO_REDIS_DB.connection_pool.get_connection` on the
  *instance* catches the first real Redis op, and neither `import popoto` nor defining a
  `Model` subclass trips it (required for "importable but unused → no warning").
- **Method**: prototype (probe script, `REDIS_URL=redis://localhost:6379/15` set before import).
- **Result**: confirmed. After `import popoto` → 0 calls; after a `class Probe(popoto.Model)`
  definition with a `KeyField` → still 0 calls; `Probe.create(...)` → 1 call;
  `Probe.query.get(...)` → 2 calls. Instance-level assignment sticks
  (`pool.get_connection is wrapper` → True).
- **Confidence**: high.
- **Impact if false**: would have forced a model-layer hook instead; not needed.

### spike-2: redis-py signature variance across 7.x / 8.x

- **Assumption**: `ConnectionPool.get_connection` has an incompatible signature between
  redis-py 7 and 8, so the wrapper must be signature-agnostic.
- **Method**: code-read + local introspection.
- **Result**: confirmed. Local env is redis-py **7.1.1**, where the signature is
  `get_connection(self, command_name=None, *keys, **options)`. redis-py 8 drops the
  positional `command_name`. `def wrapper(*args, **kwargs)` delegating verbatim covers both;
  the wrapper must never inspect or reorder the arguments.
- **Confidence**: high.
- **Impact if false**: none — the `*args/**kwargs` form is correct either way.

## Appetite

Small. One warning, one trigger mechanism, subprocess tests, a docs touch.

## Solution

Warn exactly once per pytest session when BOTH are true: the plugin loaded but is inert
(no opt-in), AND popoto actually touches Redis during the session. Stay silent when popoto is
merely importable but unused — a transitive dependency must not produce noise.

Mechanism (zero steady-state cost, no hot-path check):

1. In `_configure_test_db`, when `_resolve_test_db()` returns `None`, do not just return —
   record the inert state and arm a **one-shot tripwire** on the live connection pool:
   wrap `redis_db.POPOTO_REDIS_DB.connection_pool.get_connection` with a function that
   (a) emits the warning once, (b) restores the original method, (c) delegates. After the
   first Redis op the pool is byte-identical to before.
2. The warning text names: the DB the connection currently points at, the fact that popoto is
   NOT isolating or flushing it, and both opt-ins (`popoto_test_db = "15"` in
   `[tool.pytest.ini_options]`, or `POPOTO_TEST_DB`), plus `-p no:popoto` to silence by intent.
3. Emit via `warnings.warn(..., PopotoIsolationWarning)` where `PopotoIsolationWarning`
   subclasses `UserWarning` (defined in the plugin module) — it lands in pytest's warnings
   summary, is filterable by class, and needs no logging config to be visible. Also mirror one
   line at `logger.warning` for log-capture environments.
4. Disarm on session teardown (unwrap if never fired) so the wrapper cannot leak past pytest.

Why pool-level and not model-level: every popoto read/write goes through the shared pool;
wrapping `get_connection` catches sync usage with one seam and no per-op overhead after the
first call — spike-1 confirms it fires on the first `create`/`query` and stays silent through
import and model definition.

**Async is explicitly out of scope for v1.** `get_async_redis_db()`
(`src/popoto/redis_db.py:302`) builds its client lazily *inside* the running event loop, so
there is no async pool in existence at `pytest_configure` time to arm. Covering it would mean
editing production code in `redis_db.py`, not the plugin — outside this appetite. An
async-only popoto test suite that never touches the sync client gets no warning; record that
as a known limit in the plugin docstring and in `docs/testing.md`.

## Acceptance Criteria (from the issue, verbatim)

- [ ] A pytest session that uses popoto models with neither opt-in set produces exactly one
      warning naming `popoto_test_db` / `POPOTO_TEST_DB` and the DB in use.
- [ ] A session with either opt-in set produces none.
- [ ] A session where popoto is importable but unused produces none.
- [ ] Covered in `tests/test_pytest_plugin.py` (whose inert-path coverage is also owed by #549).

## Step by Step Tasks

1. Define `PopotoIsolationWarning(UserWarning)` in `src/popoto/pytest_plugin.py`.
2. In `_configure_test_db`, on the `None` path: arm the one-shot `get_connection` wrapper
   (store the original on `config` for teardown); keep the existing early-return semantics
   otherwise.
3. Unwrap in `pytest_unconfigure` (add the hook) if the tripwire never fired.
4. Warning copy: `"popoto is writing to Redis DB {db} during this pytest session and is NOT
   isolating or flushing it (the popoto pytest plugin is installed but not opted in). Set
   popoto_test_db = \"15\" under [tool.pytest.ini_options] or export POPOTO_TEST_DB to
   isolate, or pass -p no:popoto to silence this warning."`
5. Tests in `tests/test_pytest_plugin.py`, subprocess-based like the existing
   `test_isolated_db_subprocess` (the plugin's configure-time behavior cannot be re-entered
   in-process): inert+used → exactly one `PopotoIsolationWarning`; ini opt-in → none;
   env opt-in → none; inert+unused (test imports popoto but performs no Redis op) → none.
   Subprocess Redis target must be a non-zero DB via `REDIS_URL` set before `import popoto`.
6. Docs: `docs/testing.md` gains the warning description in the opt-in section; CHANGELOG entry.

## No-Gos

- No warning at configure/collection time purely because popoto is installed — noise for
  transitive dependents is the failure mode the issue explicitly forbids.
- No behavior change to isolation itself; the plugin stays opt-in exactly as #594 shipped it.
- No new env var to suppress the warning — `-p no:popoto` and Python warning filters suffice.

## Risks / Rabbit Holes

- **redis-py pool internals**: wrap the bound method on the *pool instance*, not the class,
  so other pools (tripwire's DB-0 client, user-created clients) are untouched. redis-py 7 and
  8 both expose `ConnectionPool.get_connection`; redis-py 8 changed its signature
  (`get_connection()` with no args vs 7's `get_connection(command_name, *keys, **options)`) —
  the wrapper must accept `*args, **kwargs` and delegate verbatim.
- **The #584 refusal interplay**: `MemoryService` under an inert plugin with `REDIS_URL`
  naming DB 0 now raises `Db0RefusedError` before any write — the warning and the refusal are
  complementary, not redundant (the warning covers non-zero DBs and plain model usage too).
- **Warning dedup across xdist workers**: out of scope; one warning per worker process is
  acceptable (note in docstring).
- **Overlap with the existing `_popoto_db0_tripwire` fixture** (`pytest_plugin.py:295`):
  that session-scoped autouse fixture does **not** early-return on the inert path — it runs in
  every downstream popoto-dependent suite, opens a side client on DB 0, and `pytest.fail`s at
  session end if DB 0 was empty at session start and gained keys. So the inert path is not
  *entirely* silent today, but the existing signal is (a) failure-shaped rather than
  advisory, (b) limited to DB 0, and (c) disabled whenever DB 0 already has keys — which is
  the normal developer box, i.e. exactly the case the issue is about. The new warning covers
  non-zero DBs and non-idle DB 0. Do not fold the two together and do not change the
  tripwire's behavior in this change; just verify in the new subprocess tests that a warning
  and a tripwire failure can coexist without the tripwire masking the warning assertion
  (point the subprocess at a non-zero DB, which keeps the tripwire on its SKIP path).
- **The wrapper is lost if the pool object is replaced.** `_swap_db()` builds a *new*
  `redis.ConnectionPool` and `set_REDIS_DB_settings()` can rebind `POPOTO_REDIS_DB` wholesale;
  either drops the armed wrapper and the warning never fires. On the inert path the plugin
  itself never calls `_swap_db`, so this only bites a downstream suite that reconfigures the
  connection by hand. Degrading to silence is acceptable (warn-only feature, never a
  correctness dependency) — but do not attempt to re-arm on rebind, that is a rabbit hole.

## Success Criteria

- All four acceptance tests green in `tests/test_pytest_plugin.py`.
- Full non-slow suite green; this repo's own suite (opted in) emits zero new warnings.
- `ruff check src/` clean; mypy delta 0 vs main measured in the same environment.

## Documentation

- `docs/testing.md` — warning behavior added to the opt-in section.
- `CHANGELOG.md` — Added entry under the next release.

## Open Questions

None — the issue's acceptance criteria settle scope; trigger mechanism chosen above per the
issue's own "warn on first Redis-touching operation" guidance.
