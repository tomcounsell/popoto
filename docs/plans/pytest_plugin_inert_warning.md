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
6. Module docstring: record the two known limits — async-only suites are not covered (no async
   pool exists at configure time), and the warning does not survive a manual `_swap_db()` /
   `set_REDIS_DB_settings()` pool rebind. One warning per xdist worker is expected.
7. Docs: `docs/testing.md` gains the warning description in the opt-in section (including the
   async limit); CHANGELOG entry under the next release.

## No-Gos

- No warning at configure/collection time purely because popoto is installed — noise for
  transitive dependents is the failure mode the issue explicitly forbids.
- No behavior change to isolation itself; the plugin stays opt-in exactly as #594 shipped it.
- No new env var to suppress the warning — `-p no:popoto` and Python warning filters suffice.
- No async-pool instrumentation and no changes to `src/popoto/redis_db.py` — the sync seam is
  the whole of v1 (see Solution).
- No changes to `_popoto_db0_tripwire`; its inert-path behavior stays exactly as #594 shipped.
- Not fixing #549 here. That issue owes broader inert/opt-in coverage; this plan adds only the
  four warning tests its acceptance criteria name.

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

## Critique Results

<!-- Populated by /do-plan-critique (war room), 2026-09-03. Verdict: NEEDS REVISION (1 blocker). -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | risk-robustness + history-consistency | The warning is raised from inside the Redis call path (the wrapper around `ConnectionPool.get_connection`), so a downstream suite with `filterwarnings = error` turns it into a `PopotoIsolationWarning` raised out of an arbitrary `Model.create()`/query — and popoto's own bare `except Exception` on that path (`check_connection()`, `src/popoto/redis_db.py:425-429`) swallows it, reporting "Redis is down" while consuming the one-shot's only emission. Contradicts the plan's own "warn-only, never a correctness dependency" contract (lines 180-181) and can silently defeat acceptance criterion 1. | _pending revision_ (Solution steps 1–3; Tasks 2 and 4) | Wrapper body order: (1) unwrap/disarm, (2) `try: warnings.warn(msg, PopotoIsolationWarning, stacklevel=2)` / `except Exception: pass` with the `logger.warning` mirror OUTSIDE the try so the signal survives `-W error`, (3) `return original(*args, **kwargs)` unconditionally. Add a fifth subprocess test asserting that under `-W error::UserWarning` the popoto op still succeeds and the log line is present. |
| CONCERN | risk-robustness | Disarm is not identity-checked, not thread-safe, and leaves an instance attribute behind: (a) restoring by assigning the bound method leaves a shadowing instance attribute + reference cycle, so the pool is not "byte-identical"; (b) `pytest_unconfigure` unwraps whatever is on the pool without checking it is still our wrapper, clobbering a later wrapper or targeting a different pool if a conftest rebound `POPOTO_REDIS_DB`; (c) `ConnectionPool` is shared across threads, so two concurrent first-ops can both warn or race the restore. | _pending revision_ (Solution step 1; Tasks 2–3) | Arm with `pool.get_connection = wrapper`, store `config._popoto_tripwire = (pool, wrapper)`. Disarm: `if getattr(pool, "get_connection", None) is wrapper: pool.__dict__.pop("get_connection", None)` — pop, not assignment. Guard the fired-flag flip and the pop with a module-level `threading.Lock()`; set `fired = True` before warning; wrapper returns early to `original(...)` if already fired. |
| CONCERN | risk-robustness + scope-value | The DB number must be read at trip time, not arm time. Capturing it in `_configure_test_db` makes the message lie if anything rebound the connection between configure and first use, and `connection_kwargs` has no `"db"` key for some pool constructions (unix socket / URL forms) — a `KeyError` raised inside a user's Redis call. | _pending revision_ (Solution step 2; Task 4) | In the wrapper: `db = pool.connection_kwargs.get("db", 0)` (mirrors `src/popoto/pytest_plugin.py:143`), then format the copy. Do not capture `db` in the closure at configure time. |
| CONCERN | history-consistency | No `## Prior Art` section — 100 of 109 plans in `docs/plans/` have one, and the omitted history is load-bearing: #490/PR #500 exists because redis-py 8 injects pool-internal keys that broke a naive splat of `connection_pool.connection_kwargs` (hence `sibling_client_kwargs`, `src/popoto/redis_db.py:246`), and #422/#420 is a prior plugin change that monkeypatched shared globals and regressed the suite. This plan monkeypatches a redis-py pool instance — same class of move — citing neither. | _pending revision_ (new `## Prior Art` section) | Name PR #594, #549, #522, #490/PR #500, #422/#420, #577/#584 with the lesson carried forward. State explicitly (it constrains Task 2): never inspect, reorder, or reconstruct redis-py pool internals — delegate `*args, **kwargs` verbatim and read only `connection_kwargs.get(...)` with defaults, never splat it. |
| NIT | history-consistency | The plugin module docstring (`src/popoto/pytest_plugin.py:3-19`) still describes pre-#594 autouse behavior ("this plugin automatically: 1. Switches the Redis connection to a dedicated test database (default: DB 15)… 3. Default: 15") — exactly the behavior #594 removed. Task 6 opens this docstring anyway. | _pending revision_ (Task 6) | While editing it for the async limit, correct the opening paragraph and the "Default: 15" priority list to state the plugin is inert without an opt-in. |

**Structural checks**: required sections FAIL (`## Prior Art` missing); task numbering PASS (1–7, no gaps); dependencies PASS (none declared); file paths PASS (5/5 exist, all line citations verified); freshness claims PASS (plugin is 358 lines, no `pytest_unconfigure`/`pytest_sessionfinish`, no `xfail` in `tests/test_pytest_plugin.py`, entry point is `popoto` so `-p no:popoto` in the warning copy is correct); cross-references PASS (all 4 acceptance criteria map to Task 5); per-task validation FAIL (minor — verification lives only in Success Criteria).

---

## Open Questions

None — the issue's acceptance criteria settle scope; trigger mechanism chosen above per the
issue's own "warn on first Redis-touching operation" guidance.
