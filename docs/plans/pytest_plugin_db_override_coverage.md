---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/549
last_comment_id: none
---

# #549 — test_pytest_plugin.py: stop hardcoding DB 15; cover env-override and inert states

## Problem

`tests/test_pytest_plugin.py` hardcodes `== 15` in 5 assertions, so any run using the
documented `POPOTO_TEST_DB=<n>` override reports exactly 5 failures that must be manually
classified as benign — defeating the override's purpose (concurrent worktrees avoiding DB-15
contention, CLAUDE.md gotcha 4). Post-#594 the plugin has three states (env-override / ini /
inert) and the file covers none by resolution: the ini state passes only by literal.

## Freshness Check

Verified 2026-09-04 against main `57d2ddc` (baseline before this revision: `ab48b0b`).

- The 5 hardcoded sites still exist. Line numbers drifted when #595 landed (PR #597, +~290
  lines). Current locations in `tests/test_pytest_plugin.py`:

  | Was (issue) | Now | Assertion |
  |---|---|---|
  | 74 | 77 | `assert current_db == 15` (`TestDatabaseIsolation::test_on_test_db`) |
  | 89 | 92 | `assert _DB_AT_IMPORT_TIME == 15` (`test_swap_happens_before_test_modules_are_imported`) |
  | 194 | 197 | `assert async_db == 15` (`test_async_connection_on_test_db`) |
  | 316 | 319 | `assert db == 15` (`src.popoto` alias-collapse test) |
  | 380 | 383 | `assert db == 15` (`test_canonical_redis_db_on_test_db`) |

- Two *additional* `== 15` literals now exist at lines 785 and 819, inside #595's
  `TestIsolationWarning::test_5b/5c` subprocess probe sources. These are **correct by
  construction** — those probes set the opt-in to 15 explicitly (`-o popoto_test_db=15` /
  `POPOTO_TEST_DB=15`) in the child environment, so they are independent of the parent
  session's DB. **Do not rewrite them.**
- Reproduced this session: `POPOTO_TEST_DB=14 pytest tests/test_pytest_plugin.py` → 5
  failures, all `assert ... == 15`. On DB 15 the file is fully green.
- #595 merged (PR #597) and gives the inert path *warning*-behavior coverage (one-shot
  `PopotoIsolationWarning`; silence when opted in or when popoto is unused). It does **not**
  assert the *plumbing* contract — that an inert plugin performs no swap and no flush. That
  gap is still #549's.
- No `xfail`/`pytest.xfail` markers exist in this file — nothing to convert.
- No other active plan in `docs/plans/` targets `tests/test_pytest_plugin.py`.
  (`pytest_plugin_inert_warning.md` is #595's, already shipped — out of scope, do not touch.)

**Disposition: Minor drift** — line numbers moved with #597, two new literals classified as
out-of-scope, one solution item (inert plumbing) confirmed still unmet.

## Prior Art

- **PR #594** — made the plugin opt-in; `_resolve_test_db()` lost its `15` default and now
  returns `None` when neither env var nor ini is set. This is what turned one state into three.
- **PR #597 (#595)** — added `TestIsolationWarning` with reusable subprocess helpers
  (`_repo_root`, `_base_env`, `_inert_env`, `_run`). `_inert_env` already strips
  `POPOTO_TEST_DB` and pins `REDIS_URL=redis://localhost:6379/14`. **Reuse these**; do not
  build a parallel harness.
- **PR #527 (#522)** — moved the swap into `pytest_configure`; the reason
  `_DB_AT_IMPORT_TIME` exists at all.
- **PR #500 (#490)** — `test_isolated_db_subprocess`, the original subprocess pattern.
- **#603** — CI exports a db-less `REDIS_URL`; a subprocess that inherits it unpinned lands
  wherever the URL points. Every child process this plan spawns must set `REDIS_URL` with an
  explicit non-zero db.
- **#577** — the DB 0 hazard the isolation plumbing exists to prevent.

No prior attempt fixed the hardcoded literals, so there is no failed-fix history to analyze.

## Research

No external research needed — this is internal test-harness work against pytest APIs
(`pytestconfig`, `config.getini`, subprocess-launched inner sessions) already used throughout
the file. No new dependencies, no infra changes, no `docs/infra/` entry.

## Technical Approach

Resolution source: `popoto.pytest_plugin._resolve_test_db(config)` is the single function the
plugin itself uses (env var > ini > `None`). Tests should ask it the same question rather than
restating `15`.

Add one session-scoped fixture near the top of `tests/test_pytest_plugin.py`:

```python
@pytest.fixture(scope="session")
def expected_test_db(pytestconfig):
    """The DB this session is supposed to be isolated on, resolved the way the
    plugin resolves it. Skips if the plugin is inert (`-p no:popoto`, or no opt-in)."""
    from popoto.pytest_plugin import _resolve_test_db

    db = _resolve_test_db(pytestconfig)
    if db is None:
        pytest.skip("plugin is inert (no popoto_test_db opt-in) — nothing to assert")
    return db
```

Then swap each of the 5 literals for `== expected_test_db`, keeping the existing
literal-independent invariants (`!= 0`, and the `_DB_AT_IMPORT_TIME != 0` assertion) exactly
as they are — those are the DB-0 safety assertions and must stay unconditional.

**Tautology risk and its mitigation.** Reading the expectation from `_resolve_test_db` means a
bug *inside the resolver* would be invisible to these five tests: both sides of the assertion
would move together. The observed side still comes from the live connection pool, so a
swap-plumbing bug is still caught. To pin the resolver independently, task 2's env-override
test asserts a **literal** `7` in a child process whose env we control end to end. That test,
not the five rewritten ones, is what holds the resolution chain honest.

## Solution

1. **Resolution-based assertions.** Add the `expected_test_db` fixture and replace the 5
   hardcoded `== 15` assertions with it, so the file passes on any documented override.
2. **Env-beats-ini override test.** A subprocess test running an inner pytest session with
   `POPOTO_TEST_DB=7` *and* the ini opt-in present (`-o popoto_test_db=15` on the child's
   argv), asserting the child lands on DB 7 — env wins over ini, with a literal expectation.
3. **Inert plumbing test.** A subprocess test using `TestIsolationWarning._inert_env` (no
   opt-in, `REDIS_URL` pinned to a non-zero DB): seed a marker key in that DB from the parent
   before launching, have the child assert its connection is still on the `REDIS_URL` DB (no
   swap), then assert from the parent afterwards that the marker key survived (no flush).
   Complements #595's warning tests with the plumbing half.
4. **Never DB 0.** Every child process sets `REDIS_URL` with an explicit non-zero db before
   importing popoto (#603). The parent-side marker key is written and cleaned up on the same
   non-zero DB — never DB 0, never DB 15's contents beyond the plugin's own flush.

## Data Flow

Not applicable at system scale — but the child-session flow the new tests exercise is:

`parent test` → `subprocess.run([python, -m, pytest, probe])` with a controlled env
(`REDIS_URL` pinned, `POPOTO_TEST_DB` set or stripped, `PYTHONPATH` pinned to this worktree)
→ child `pytest_configure` → `_resolve_test_db(config)` → `_swap_db(db)` or early return →
child probe test reads `redis_db.POPOTO_REDIS_DB.connection_pool.connection_kwargs["db"]` and
asserts → parent asserts on the child's return code/output, plus (task 3) on the marker key's
survival in the DB the child was pointed at.

## Step by Step Tasks

1. Add the `expected_test_db` session fixture to `tests/test_pytest_plugin.py` (near
   `_get_db()` / `_DB_AT_IMPORT_TIME`).
   *Validate:* `pytest tests/test_pytest_plugin.py -q` still green.
2. Rewrite the 5 assertions (lines ~77, 92, 197, 319, 383) to compare against
   `expected_test_db`; leave every `!= 0` assertion untouched; leave lines ~785/819 untouched.
   *Validate:* `pytest tests/test_pytest_plugin.py -q` green, and
   `POPOTO_TEST_DB=14 pytest tests/test_pytest_plugin.py -q` green (the repro that fails today).
3. Add the env-beats-ini subprocess test (Solution 2), following `TestIsolationWarning`'s
   helper style; assert the child's DB literal is 7 despite the child's ini saying 15.
   *Validate:* the new test passes; deliberately flipping the child's env to 15 makes it fail
   (sanity-check it is not vacuous).
4. Add the inert-plumbing subprocess test (Solution 3): parent seeds marker key on DB 14
   (or another non-zero, non-15 DB), child asserts no swap, parent asserts marker survives and
   then deletes it.
   *Validate:* the new test passes; it fails if `_inert_env` is replaced with `_base_env`
   (i.e. it is not vacuous when the plugin is *not* inert).
5. Run the gates: `pytest tests/test_pytest_plugin.py` on the default DB and under
   `POPOTO_TEST_DB=7` and `POPOTO_TEST_DB=14`; then the full non-slow suite on the default DB;
   `ruff check src/`, `black --check src/ tests/`, `mypy src/`.
6. CHANGELOG entry under Unreleased (test-infrastructure note).

## No-Gos

- **No plugin behavior changes** — this is test-only. If a new test exposes a real defect in
  `src/popoto/pytest_plugin.py`, file an issue; do not fix it inline in this PR.
- Do not modify #595's `TestIsolationWarning` tests, including the `== 15` literals at ~785
  and ~819 (correct by construction).
- Do not touch `docs/plans/pytest_plugin_inert_warning.md` (#595's plan).
- Do not weaken any `!= 0` assertion into a resolved comparison — those are the DB-0 guards.
- No new dependencies; no changes to `pyproject.toml`'s `popoto_test_db = "15"` opt-in.

## Risks

| Risk | Mitigation |
|---|---|
| Resolver-side bug becomes invisible (tautology) | Task 3's literal-`7` child test pins the resolution chain independently; `!= 0` guards stay literal. |
| A subprocess inherits CI's db-less `REDIS_URL` and lands on DB 0 (#603) | Every child env sets `REDIS_URL=redis://localhost:6379/<non-zero>` explicitly; reuse `_inert_env`, which already does. |
| Parent-side marker key contends with a concurrent worktree on the same DB | Use a uuid-suffixed key name, delete it in a `finally`; never `flushdb` from the parent. |
| Inert-path test passes vacuously because the developer has `POPOTO_TEST_DB` exported | Child self-asserts its own inertness first (`"POPOTO_TEST_DB" not in os.environ` and `getini(...) == ""`), the pattern #595 already established. |
| Line numbers drift again before build | Build step re-greps `== 15` rather than trusting the table above. |

## Success Criteria

- `POPOTO_TEST_DB=7 pytest tests/test_pytest_plugin.py` and `POPOTO_TEST_DB=14 ...` are both
  fully green (currently: 5 failures each).
- Default run `pytest tests/test_pytest_plugin.py` fully green.
- A test named for the env-beats-ini precedence exists and fails if the precedence is
  inverted.
- A test asserting the inert plugin performs no swap and no flush exists and fails if the
  child is given an opt-in.
- No `== 15` literal remains in the file outside `TestIsolationWarning`'s child-process probe
  sources.
- Full non-slow suite green on the default DB; `ruff check src/` exits 0; black clean; mypy
  error count unchanged vs base in the same environment (state the environment with the count
  — CLAUDE.md gotcha 5).

## Documentation

- CHANGELOG: Unreleased → test-infrastructure note that the plugin tests now resolve the
  expected DB instead of hardcoding 15, and cover the env-override and inert paths.
- CLAUDE.md gotcha 4 already documents the override correctly; no change required.
- No user-facing docs change (`docs/` untouched) — the plugin's public behavior is unchanged.

## Open Questions

None. The scope is test-only, the resolution chain is fixed by #594, and the subprocess
harness to reuse already exists from #597.
