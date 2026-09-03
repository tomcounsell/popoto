---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-03
tracking: https://github.com/tomcounsell/popoto/issues/595
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-03T09:35:06Z
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

## Prior Art

Every item below is a prior change to this same plugin or to the redis-py pool seam it now
touches. Two of them (#490/PR #500 and #422/#420) are the direct precedent for the monkeypatch
this plan proposes.

| Ref | What it was | Lesson carried into this plan |
|-----|-------------|-------------------------------|
| **PR #594** (merged) | Made the plugin opt-in: `_resolve_test_db()` returns `None` with no opt-in, and `_configure_test_db` / `_popoto_test_db` / `_popoto_flush_db` all early-return. | This plan is the follow-up #594 owed. It must not re-add any implicit isolation — only an advisory warning on the already-inert path. |
| **#549** (OPEN) | `tests/test_pytest_plugin.py` hardcodes DB 15 and does not cover the opt-in/inert paths. | Overlapping test file; this plan adds only the six warning tests (5a–5f) its own acceptance criteria name and leaves the broader coverage debt to #549 (see No-Gos). |
| **#522** (closed) | Module-level `Model.create(...)` ran during collection, before the session fixture, and wrote to DB 0 — the reason the DB swap lives in `pytest_configure` rather than a fixture. | The tripwire must be armed in `pytest_configure` for the same reason: a fixture arms too late to catch import-time model code, which is exactly the usage most likely to surprise a downstream suite. |
| **#490 / PR #500** (merged) | `test_isolated_db_subprocess` failed on redis-py 8 because a pool's `connection_kwargs` carries pool-internal keys (`himport_registry`, `maint_notifications_*`, `orig_*`) that `Redis.__init__` rejects when splatted. Fixed by `redis_db.sibling_client_kwargs()` (`src/popoto/redis_db.py:246`), which whitelists only standard connection params. | **Hard constraint on Task 2:** never inspect, reorder, reconstruct, or splat redis-py pool internals. The wrapper delegates `*args, **kwargs` verbatim (spike-2), and the only read from `connection_kwargs` is `.get("db", 0)` with a default — never `[...]`, never `dict(**kwargs)`. |
| **#422 / #420** (merged / closed) | The last change that monkeypatched shared global state from this plugin (`sys.modules` aliasing for `src.popoto`). The follow-up attempt at the alias-collapse fix regressed the suite 1 → 78 failures and was held as do-not-merge. | Monkeypatching from a pytest plugin is the highest-blast-radius move available to this repo, and the prior attempt's failure mode was *silent scope creep* onto objects other suites own. Hence: patch the pool **instance**, never `ConnectionPool` the class; identity-check before unwrapping; leave nothing behind (`__dict__.pop`, not reassignment). |
| **#577 / #584** (open / closed) | Ad-hoc scripts default to DB 0 (the live agent store); `popoto.integrations` `DEFAULT_URL` pointed at DB 0 and now refuses it. | The hazard this warning exists to surface. The #584 `Db0RefusedError` covers the `MemoryService`-on-DB-0 case only; this warning covers plain model usage on any DB (see Risks). |

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
   assign `pool.get_connection = wrapper` on the *pool instance*, and store
   `config._popoto_tripwire = (pool, wrapper)` for teardown.

2. **Wrapper body order is load-bearing — the Redis op must never be affected by the
   warning.** The wrapper runs inside an arbitrary `Model.create()` / query call, so any
   exception it raises (most realistically a `PopotoIsolationWarning` promoted by a
   downstream suite's `filterwarnings = error`) surfaces as a Redis failure — and popoto's own
   bare `except Exception` in `check_connection()` (`src/popoto/redis_db.py:425-429`) would
   swallow it and report "Redis is down" while the one-shot's only emission is consumed.
   The body must therefore be, in this exact order:

   1. Under a module-level `threading.Lock()`: if already fired, release and
      `return original(*args, **kwargs)` immediately; otherwise set `fired = True` and
      **disarm** (see step 4) while still holding the lock. Arming and firing happen on a
      `ConnectionPool` shared across threads, so the flag flip and the disarm must be atomic
      or two concurrent first-ops can both warn or race the restore.
   2. Resolve the DB **at trip time**, not at arm time: `db = pool.connection_kwargs.get("db", 0)`
      (mirrors `src/popoto/pytest_plugin.py:143`). Capturing it in the closure at configure
      time would make the message lie if anything rebound the connection in between, and
      `connection_kwargs` has no `"db"` key for some pool constructions (unix-socket / URL
      forms) — a bare `[...]` lookup would raise `KeyError` inside a user's Redis call. Read
      only with `.get(..., default)`; never splat `connection_kwargs` (see Prior Art, #490).
   3. `logger.warning(...)` the mirror line **first and unguarded**, so the signal survives
      even when the next step raises.
   4. `try: warnings.warn(msg, PopotoIsolationWarning, stacklevel=2)` / `except Exception: pass`.
   5. `return original(*args, **kwargs)` **unconditionally**.

3. The warning text names: the DB the connection currently points at, the fact that popoto is
   NOT isolating or flushing it, and both opt-ins (`popoto_test_db = "15"` in
   `[tool.pytest.ini_options]`, or `POPOTO_TEST_DB`), plus `-p no:popoto` to silence by intent.
   Emit via `warnings.warn(..., PopotoIsolationWarning)` where `PopotoIsolationWarning`
   subclasses `UserWarning` (defined in the plugin module) — it lands in pytest's warnings
   summary, is filterable by class, and needs no logging config to be visible. The
   `logger.warning` mirror (step 2.3) is what log-capture environments and `-W error` suites see.

4. **Disarm is identity-checked and leaves nothing behind.** Both on trip (step 2.1) and in
   `pytest_unconfigure`, disarm as:

   ```python
   pool, wrapper = getattr(config, "_popoto_tripwire", (None, None))
   if pool is not None and getattr(pool, "get_connection", None) is wrapper:
       pool.__dict__.pop("get_connection", None)
   ```

   `pop` rather than reassigning the bound method: assigning `original` back leaves a
   shadowing instance attribute plus a reference cycle, so the pool would *not* be
   byte-identical. The `is wrapper` identity check keeps the disarm from clobbering a later
   wrapper installed by someone else, and from touching a different pool if a conftest rebound
   `redis_db.POPOTO_REDIS_DB` after arm time. `pytest_unconfigure` (a new hook) makes this a
   no-op when the tripwire already fired, so the wrapper cannot leak past pytest either way.

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

Added by critique (not in the issue, but required for the above to hold):

- [ ] Under `filterwarnings = error` / `-W error::UserWarning`, the warning never escapes into
      the Redis call path: the popoto operation still succeeds and the log mirror is emitted.

## Step by Step Tasks

1. Define `PopotoIsolationWarning(UserWarning)` and a module-level `threading.Lock()` in
   `src/popoto/pytest_plugin.py`.
   *Verify:* `from popoto.pytest_plugin import PopotoIsolationWarning` and
   `issubclass(PopotoIsolationWarning, UserWarning)`; `ruff check src/` clean.
2. In `_configure_test_db`, on the `None` path: arm the one-shot `get_connection` wrapper on
   the pool *instance* and store `config._popoto_tripwire = (pool, wrapper)`; keep the
   existing early-return semantics otherwise. The wrapper body follows Solution step 2
   verbatim — lock/fired-check/disarm, `db = pool.connection_kwargs.get("db", 0)` at trip
   time, unguarded `logger.warning`, `try/except Exception: pass` around `warnings.warn`,
   then unconditional `return original(*args, **kwargs)`. Never inspect, reorder, or splat
   redis-py pool internals (Prior Art, #490/PR #500).
   *Verify:* new subprocess tests 5a and 5e (below) pass; grep the diff for `connection_kwargs[`
   and for any `**` splat of `connection_kwargs` — both must be absent.
3. Add the `pytest_unconfigure` hook and disarm there with the identity check + `__dict__.pop`
   from Solution step 4; it is a no-op if the tripwire already fired.
   *Verify:* after an inert+unused session, `"get_connection" not in pool.__dict__` and
   `pool.get_connection` is the plain `ConnectionPool` bound method (test 5f).
4. Warning copy: `"popoto is writing to Redis DB {db} during this pytest session and is NOT
   isolating or flushing it (the popoto pytest plugin is installed but not opted in). Set
   popoto_test_db = \"15\" under [tool.pytest.ini_options] or export POPOTO_TEST_DB to
   isolate, or pass -p no:popoto to silence this warning."` — `{db}` interpolated from the
   trip-time `pool.connection_kwargs.get("db", 0)`, not from a value captured at arm time.
   *Verify:* test 5a asserts the message contains the actual non-zero DB number the subprocess
   was pointed at.
5. Tests in `tests/test_pytest_plugin.py`, subprocess-based like the existing
   `test_isolated_db_subprocess` (the plugin's configure-time behavior cannot be re-entered
   in-process). Subprocess Redis target must be a non-zero DB via `REDIS_URL` set before
   `import popoto`.
   - **5a** inert + popoto used → exactly one `PopotoIsolationWarning`, naming that DB.
   - **5b** ini opt-in (`popoto_test_db`) → none.
   - **5c** env opt-in (`POPOTO_TEST_DB`) → none.
   - **5d** inert + popoto importable but unused (imports popoto, defines a model, performs no
     Redis op) → none.
   - **5e** inert + used **under `-W error::UserWarning`** → the popoto operation still
     succeeds (no exception escapes into the Redis call path), the subprocess exits 0, and the
     `logger.warning` mirror line is present in the captured output. This is the blocker's
     regression test.
   - **5f** inert + unused → after `pytest_unconfigure`, the pool carries no instance-level
     `get_connection` attribute (disarm left nothing behind).
   *Verify:* all six pass; each asserts on subprocess exit code as well as output.
6. Module docstring (`src/popoto/pytest_plugin.py:3-19`): (a) **correct the stale pre-#594
   text** — the opening list still claims the plugin "automatically" switches/flushes and
   documents "3. Default: 15"; rewrite it to state the plugin is inert unless `popoto_test_db`
   or `POPOTO_TEST_DB` is set, and that it then warns once on first Redis use. (b) Record the
   two known limits: async-only suites are not covered (no async pool exists at configure
   time), and the warning does not survive a manual `_swap_db()` / `set_REDIS_DB_settings()`
   pool rebind. One warning per xdist worker is expected.
   *Verify:* the strings "automatically" and "Default: 15" no longer appear in the docstring.
7. Docs: `docs/testing.md` gains the warning description in the opt-in section (including the
   async limit); CHANGELOG entry under the next release.
   *Verify:* `mkdocs build` clean; CHANGELOG has an Added entry referencing #595.

## No-Gos

- No warning at configure/collection time purely because popoto is installed — noise for
  transitive dependents is the failure mode the issue explicitly forbids.
- No behavior change to isolation itself; the plugin stays opt-in exactly as #594 shipped it.
- No new env var to suppress the warning — `-p no:popoto` and Python warning filters suffice.
- No async-pool instrumentation and no changes to `src/popoto/redis_db.py` — the sync seam is
  the whole of v1 (see Solution).
- No changes to `_popoto_db0_tripwire`; its inert-path behavior stays exactly as #594 shipped.
- Not fixing #549 here. That issue owes broader inert/opt-in coverage; this plan adds only the
  six warning tests (5a–5f) its acceptance criteria name.

## Risks / Rabbit Holes

- **redis-py pool internals**: wrap the bound method on the *pool instance*, not the class,
  so other pools (tripwire's DB-0 client, user-created clients) are untouched. redis-py 7 and
  8 both expose `ConnectionPool.get_connection`; redis-py 8 changed its signature
  (`get_connection()` with no args vs 7's `get_connection(command_name, *keys, **options)`) —
  the wrapper must accept `*args, **kwargs` and delegate verbatim. The wrapper reads nothing
  from the pool but `connection_kwargs.get("db", 0)`; splatting `connection_kwargs` is what
  broke #490 (see Prior Art).
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

- All six acceptance tests (5a–5f) green in `tests/test_pytest_plugin.py`.
- No `PopotoIsolationWarning` can propagate out of `ConnectionPool.get_connection`: test 5e
  passes under `-W error::UserWarning` with a zero exit code.
- Full non-slow suite green; this repo's own suite (opted in) emits zero new warnings.
- `ruff check src/` clean; mypy delta 0 vs main measured in the same environment.

## Documentation

- `docs/testing.md` — warning behavior added to the opt-in section.
- `CHANGELOG.md` — Added entry under the next release.

## Critique Results

<!-- Populated by /do-plan-critique (war room), 2026-09-03. Verdict: NEEDS REVISION (1 blocker). -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | risk-robustness + history-consistency | The warning is raised from inside the Redis call path (the wrapper around `ConnectionPool.get_connection`), so a downstream suite with `filterwarnings = error` turns it into a `PopotoIsolationWarning` raised out of an arbitrary `Model.create()`/query — and popoto's own bare `except Exception` on that path (`check_connection()`, `src/popoto/redis_db.py:425-429`) swallows it, reporting "Redis is down" while consuming the one-shot's only emission. Contradicts the plan's own "warn-only, never a correctness dependency" contract (lines 180-181) and can silently defeat acceptance criterion 1. | **Addressed** — Solution step 2 (wrapper body order); Task 2; new test 5e | Wrapper body order: (1) unwrap/disarm, (2) `try: warnings.warn(msg, PopotoIsolationWarning, stacklevel=2)` / `except Exception: pass` with the `logger.warning` mirror OUTSIDE the try so the signal survives `-W error`, (3) `return original(*args, **kwargs)` unconditionally. Add a fifth subprocess test asserting that under `-W error::UserWarning` the popoto op still succeeds and the log line is present. |
| CONCERN | risk-robustness | Disarm is not identity-checked, not thread-safe, and leaves an instance attribute behind: (a) restoring by assigning the bound method leaves a shadowing instance attribute + reference cycle, so the pool is not "byte-identical"; (b) `pytest_unconfigure` unwraps whatever is on the pool without checking it is still our wrapper, clobbering a later wrapper or targeting a different pool if a conftest rebound `POPOTO_REDIS_DB`; (c) `ConnectionPool` is shared across threads, so two concurrent first-ops can both warn or race the restore. | **Addressed** — Solution steps 1 and 4 (lock, identity check, `__dict__.pop`); Tasks 2–3; new test 5f | Arm with `pool.get_connection = wrapper`, store `config._popoto_tripwire = (pool, wrapper)`. Disarm: `if getattr(pool, "get_connection", None) is wrapper: pool.__dict__.pop("get_connection", None)` — pop, not assignment. Guard the fired-flag flip and the pop with a module-level `threading.Lock()`; set `fired = True` before warning; wrapper returns early to `original(...)` if already fired. |
| CONCERN | risk-robustness + scope-value | The DB number must be read at trip time, not arm time. Capturing it in `_configure_test_db` makes the message lie if anything rebound the connection between configure and first use, and `connection_kwargs` has no `"db"` key for some pool constructions (unix socket / URL forms) — a `KeyError` raised inside a user's Redis call. | **Addressed** — Solution step 2.2 (trip-time `.get("db", 0)`); Task 4 | In the wrapper: `db = pool.connection_kwargs.get("db", 0)` (mirrors `src/popoto/pytest_plugin.py:143`), then format the copy. Do not capture `db` in the closure at configure time. |
| CONCERN | history-consistency | No `## Prior Art` section — 100 of 109 plans in `docs/plans/` have one, and the omitted history is load-bearing: #490/PR #500 exists because redis-py 8 injects pool-internal keys that broke a naive splat of `connection_pool.connection_kwargs` (hence `sibling_client_kwargs`, `src/popoto/redis_db.py:246`), and #422/#420 is a prior plugin change that monkeypatched shared globals and regressed the suite. This plan monkeypatches a redis-py pool instance — same class of move — citing neither. | **Addressed** — new `## Prior Art` section (7 refs, with the redis-py-internals constraint stated) | Name PR #594, #549, #522, #490/PR #500, #422/#420, #577/#584 with the lesson carried forward. State explicitly (it constrains Task 2): never inspect, reorder, or reconstruct redis-py pool internals — delegate `*args, **kwargs` verbatim and read only `connection_kwargs.get(...)` with defaults, never splat it. |
| NIT | history-consistency | The plugin module docstring (`src/popoto/pytest_plugin.py:3-19`) still describes pre-#594 autouse behavior ("this plugin automatically: 1. Switches the Redis connection to a dedicated test database (default: DB 15)… 3. Default: 15") — exactly the behavior #594 removed. Task 6 opens this docstring anyway. | **Addressed** — Task 6(a) | While editing it for the async limit, correct the opening paragraph and the "Default: 15" priority list to state the plugin is inert without an opt-in. |

**Revision applied 2026-09-03** — all 5 findings addressed; see the Addressed By column.

### Round 2 (re-critique of the revised plan, 2026-09-03) — verdict: READY TO BUILD (with concerns)

Round-1 findings all confirmed addressed: `## Prior Art` present (7 refs), wrapper body order
specified, identity-checked `__dict__.pop` disarm under a module lock, trip-time
`connection_kwargs.get("db", 0)`, Task 6(a) docstring correction. 0 blockers this round.

| Severity | Critic | Finding | Implementation Note |
|----------|--------|---------|---------------------|
| CONCERN | risk-robustness + history-consistency | **The pool is a `BlockingConnectionPool`, not a `ConnectionPool`.** `redis_db.get_redis_db()` builds `redis.BlockingConnectionPool` (`src/popoto/redis_db.py:139,149,209`), so `redis_db.POPOTO_REDIS_DB.connection_pool` is a `BlockingConnectionPool` at `pytest_configure` time. The Solution, the Risks section, and Task 3's *Verify* all name `ConnectionPool` — Task 3's assertion ("`pool.get_connection` is the plain `ConnectionPool` bound method") is literally false, and spike-2's redis-py 7/8 signature analysis was run against the wrong class. | Verified locally (redis-py 7.1.1): `type(pool).__name__ == "BlockingConnectionPool"`; `BlockingConnectionPool.__slots__ == ()` but instances still carry a `__dict__`, so `pool.get_connection = wrapper` and `pool.__dict__.pop("get_connection", None)` both work exactly as planned. `BlockingConnectionPool.get_connection` overrides the base with its own `(self, command_name=None, *keys, **options)` on 7.x. Fix is textual: assert `pool.get_connection.__qualname__ == "BlockingConnectionPool.get_connection"` in test 5f (or just `"get_connection" not in pool.__dict__`), and reword Solution/Risks to say "the live pool instance (a `BlockingConnectionPool`)". The `*args, **kwargs`-verbatim wrapper is correct for both classes and both redis-py majors — no code change. |
| CONCERN | risk-robustness | **Test 5e cannot see the `logger.warning` mirror in subprocess output as written.** Under a *passing* pytest run, pytest's logging plugin captures `POPOTO-PYTEST` records into the report and prints nothing to stdout/stderr; the "captured log" section is emitted only on failure. Since 5e's whole point is that under `-W error::UserWarning` the `warnings.warn` is swallowed, the log line is the *only* evidence — and it will be invisible, so 5e fails for a reason unrelated to the code under test. | Run the 5e subprocess as `[sys.executable, "-m", "pytest", str(probe), "-q", "-W", "error::UserWarning", "-o", "log_cli=true", "--log-cli-level=WARNING"]` and assert against `result.stdout + result.stderr`. Do not use `caplog` — the trip happens in a different process. |
| CONCERN | risk-robustness | **The inert-path subprocess tests (5a, 5d, 5e, 5f) must strip `POPOTO_TEST_DB` from the inherited environment.** The existing pattern copies the parent env wholesale (`env = dict(os.environ)`, `tests/test_pytest_plugin.py:568`). If a developer has `POPOTO_TEST_DB` exported, `_resolve_test_db` returns a number, the plugin is not inert, and 5a/5e fail while 5d/5f pass **vacuously** — the silent half is the dangerous one. (The repo's own `popoto_test_db = "15"` ini is *not* a hazard here: verified empirically that pytest resolves `inipath = None` for a `tmp_path` test file even with `cwd=repo_root`, so the repo ini never reaches the subprocess.) | `env = dict(os.environ); env.pop("POPOTO_TEST_DB", None); env["REDIS_URL"] = "redis://localhost:6379/14"`. Then make inertness self-verifying rather than assumed: have every inert probe assert `pytestconfig.getini("popoto_test_db") == ""` and `"POPOTO_TEST_DB" not in os.environ` in its first line, so a leaked opt-in fails loudly instead of passing vacuously. |
| CONCERN | history-consistency | **The Risks section gives the wrong reason the DB-0 tripwire stays green in the new tests.** It claims pointing the subprocess at a non-zero DB "keeps the tripwire on its SKIP path". It does not: `_popoto_db0_tripwire` opens its own side client on **DB 0 regardless** of the session's DB (`sibling_client_kwargs(..., db=0)`), and its SKIP is governed solely by `before = db0_client.dbsize()` at session start. On clean CI (DB 0 empty) the tripwire is *armed*, not skipped. A builder trusting the stated mechanism could write the wrong assertion or mis-diagnose a CI-only failure. | Reword to the actual mechanism: the tripwire arms whenever DB 0 is idle at session start; what keeps the new tests green is that the subprocess writes land on a **non-zero** DB, so `after` stays 0 and `pytest.fail` is never reached. Keep the operative instruction (point the subprocess at a non-zero DB) — only the justification is wrong. |
| NIT | scope-value | Six separate `subprocess.run([sys.executable, "-m", "pytest", ...])` invocations is ~6 process spawns of suite time for one advisory warning. 5d and 5f are the same scenario (inert + unused) observed at two points, and 5b/5c differ only in which opt-in is set. | Merge 5f's `"get_connection" not in pool.__dict__` assertion into 5d's probe as a `pytest_unconfigure`-adjacent check (or a `request.addfinalizer`), and drive 5b/5c from one parametrized helper. Purely a suite-time optimization; correctness is unaffected either way. |
| NIT | scope-value | The plan emits the warning from *inside* the Redis call path and then spends a module lock, a `try/except Exception: pass`, an unguarded log mirror, and test 5e defending that position. Setting a flag in the wrapper and emitting the warning from `pytest_sessionfinish` would put the whole emission outside any user Redis call. (Flagged as a NIT, not a CONCERN: the chosen design is sound and already hardened, and re-architecting is out of this skill's scope.) | If ever revisited: `_pytest/warnings.py` wraps `pytest_sessionfinish` in `catch_warnings_for_item`, so a `warnings.warn` raised there still lands in the warnings summary. The wrapper would reduce to `fired = True; disarm; db = pool.connection_kwargs.get("db", 0); return original(*args, **kwargs)`. Not required for this build. |

**Structural checks (round 2)**: required sections PASS (`## Prior Art` now present); task numbering PASS (1–7, no gaps); dependencies PASS (none declared); per-task validation PASS (every task 1–7 carries a *Verify:* line — round-1 FAIL resolved); file paths PASS (5/5 exist); line citations PASS — re-verified `pytest_plugin.py:118` `_configure_test_db`, `:136-137` inert early-return, `:143` `.get("db", 0)`, `:187` `_resolve_test_db`, `:295` `_popoto_db0_tripwire`, `redis_db.py:246` `sibling_client_kwargs`, `:302` `get_async_redis_db`, `:425-429` `check_connection` bare `except`; freshness PASS (plugin 358 lines, no `pytest_unconfigure`/`pytest_sessionfinish`, entry point is `popoto` so `-p no:popoto` is correct); cross-references PASS (all 5 acceptance criteria map to Task 5). Independently **confirmed** the plan's unix-socket claim: `BlockingConnectionPool.from_url("unix://…")`, `rediss://host` and the direct constructor all omit `db` from `connection_kwargs`, so the `.get("db", 0)` guard is load-bearing, not defensive padding.

**Structural checks (at critique time)**: required sections FAIL (`## Prior Art` missing); task numbering PASS (1–7, no gaps); dependencies PASS (none declared); file paths PASS (5/5 exist, all line citations verified); freshness claims PASS (plugin is 358 lines, no `pytest_unconfigure`/`pytest_sessionfinish`, no `xfail` in `tests/test_pytest_plugin.py`, entry point is `popoto` so `-p no:popoto` in the warning copy is correct); cross-references PASS (all 4 acceptance criteria map to Task 5); per-task validation FAIL (minor — verification lives only in Success Criteria).

---

## Open Questions

None — the issue's acceptance criteria settle scope; trigger mechanism chosen above per the
issue's own "warn on first Redis-touching operation" guidance.
