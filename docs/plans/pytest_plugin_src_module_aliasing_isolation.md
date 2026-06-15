---
status: Planning
type: bug
appetite: Medium
owner: valor
created: 2026-06-15
tracking: https://github.com/tomcounsell/popoto/issues/420
last_comment_id:
---

# pytest plugin: src.popoto module aliasing bypasses DB-15 isolation

## Problem

Popoto ships a pytest plugin (`popoto.pytest_plugin`) that is supposed to isolate
every test on Redis DB 15 and `flushdb()` between tests. CLAUDE.md documents this
as a hard guarantee: "Each test gets a clean DB via `flushdb()`" and "DB 0 is
rejected to prevent accidental production data loss."

That guarantee silently does not hold for any test file that imports via
`from src.popoto import ...` instead of `from popoto import ...`. Those tests
write to **DB 0** (the production default), their writes are never flushed, and
they accumulate across sessions — polluting later runs and producing flakes that
pass in isolation but fail in the full suite.

**Current behavior:**
- The editable install puts `/Users/valorengels/src/popoto/src` on `sys.path`, so
  `import popoto` resolves to `src/popoto/__init__.py`.
- pytest also puts the repo root on `sys.path` (rootdir + `tests/__init__.py` +
  `src/__init__.py` make `src.popoto` an importable package), so
  `import src.popoto` loads the **same physical file as a second, distinct module
  object** keyed `src.popoto` in `sys.modules`.
- These two module instances each have their own `redis_db` submodule and their own
  module-level `POPOTO_REDIS_DB` connection object.
- The plugin imports `from popoto import redis_db` and swaps **only that instance's**
  connection pool to DB 15. The `src.popoto` instance's `POPOTO_REDIS_DB` keeps the
  import-time default (DB 0, or `REDIS_URL`).
- Result: `Model.save()` from `src.popoto`-importing test files lands in DB 0, is
  never flushed, survives the session, and pollutes subsequent runs.

Confirmed empirically at plan time (see Freshness Check). DB 0 on this dev box
currently holds 405 keys, including test-model leakage like `Memory:valor:...`
and `$BM25:Memory:bm25:*` keys mixed in with real application data.

**Desired outcome:**
- The DB-15 isolation guarantee holds regardless of which import path a test file
  uses (`popoto` vs `src.popoto`).
- No test write ever lands in DB 0; the existing "DB 0 is rejected" protection is
  no longer bypassable.
- The known flake (`tests/test_subconscious_memory_integration.py::TestRetrievalRelevance::test_retrieves_multiple_relevant_memories`)
  stops failing in full-suite runs.
- New test files do not each need to hand-roll a `_clean_all()` autouse fixture to
  be stable.

## Freshness Check

**Baseline commit:** 61f36aa (HEAD of release/v1.7.1 at plan time)
**Issue filed at:** 2026-06-12T07:19:51Z
**Disposition:** Unchanged — every claim in the issue was re-verified true against current main.

**Root-cause hypothesis (issue's primary "Next Step") — CONFIRMED:**

```
$ python -c "import sys; sys.path.insert(0,'.'); import popoto, src.popoto; print(popoto is src.popoto)"
False
# popoto.__file__ == src.popoto.__file__ == .../src/popoto/__init__.py  (same file, two module objects)
# popoto.redis_db is not src.popoto.redis_db                            -> True (distinct submodules)
# popoto.redis_db.POPOTO_REDIS_DB is not src.popoto.redis_db.POPOTO_REDIS_DB -> True (distinct connections)
```

Simulating the plugin's swap proves the leak directly:

```
$ python -c "...; from popoto.pytest_plugin import _swap_db; _swap_db(15); ..."
popoto (plugin)    db: 15      # plugin-managed instance moves to DB 15
src.popoto         db: 0       # src.popoto instance stays on DB 0, never flushed
```

**File:line references re-verified:**
- `src/popoto/pytest_plugin.py:41` — `from popoto import redis_db` (plugin binds to the `popoto` instance only) — holds.
- `src/popoto/pytest_plugin.py:46-64` — `_swap_db()` mutates `redis_db.POPOTO_REDIS_DB.connection_pool` in-place on that single instance — holds.
- `src/popoto/redis_db.py:109-130` — connection established at import time from `REDIS_URL` / localhost DB 0 — holds; this is why each module instance gets its own DB-0-default connection.
- `tests/test_context_assembler.py:106-138` — `_clean_keys`/`_clean_all` glob-delete mitigation — present (now at ~lines 104-138); still the workaround pattern other suites copy.
- Known flake `tests/test_subconscious_memory_integration.py` — present; imports `from popoto import ...` (line 26), so it reads DB 15. (See Data Flow for the exact propagation chain.)

**Cited sibling issues/PRs re-checked:**
- PR #419 (the #408 build where this was root-caused) — referenced as the origin; not re-opened here.
- PR #302 "Auto-registering pytest plugin for test DB isolation" — the commit that introduced the plugin (`ae06673`). It is the change that established the single-instance swap design. Its tests all import `popoto` (DB 15), which is exactly why the `src.popoto` gap was never exercised or caught.

**Commits on main since issue was filed (touching referenced files):**
- `61f36aa` fix(DatetimeField) — irrelevant to the plugin/isolation path.

**Active plans in `docs/plans/` overlapping this area:** none. Grep matches in other plans (`async_docs_examples`, `integration_feedback_dx_gaps`, etc.) are incidental references to test imports, not isolation work.

## Prior Art

- **PR #302** — "Auto-registering pytest plugin for test DB isolation" (commit `ae06673`,
  the only commit ever to touch `pytest_plugin.py`): introduced the entire isolation
  mechanism — session fixture swaps to DB 15, per-test `flushdb()`, async reset. Its design
  assumes a **single** `popoto` module instance. All of its own tests (`tests/test_pytest_plugin.py`)
  import `popoto`, so the `src.popoto` second-instance path is untested. This is the direct
  ancestor of the bug, not a failed fix.
- No closed issues found for this problem (`gh issue list --state closed --search "src.popoto isolation pytest DB"` → empty).
- No prior fix attempt exists. The only mitigation in the tree is the per-file `_clean_all()`
  autouse fixture pattern (e.g. `tests/test_context_assembler.py`), which is a symptom-level
  workaround, not a fix.

## Data Flow

Tracing the pollution and the flake end-to-end:

1. **pytest startup** — editable `.pth` adds `.../src` to `sys.path`; rootdir + `src/__init__.py`
   make the repo root importable, so `src.popoto` is a valid package path.
2. **Plugin session fixture** (`_popoto_test_db`) — imports `from popoto import redis_db`,
   calls `_swap_db(15)`. Only the `popoto` instance's `POPOTO_REDIS_DB` now points at DB 15.
3. **A `src.popoto`-importing test runs** (e.g. `tests/test_relationship.py`,
   `tests/test_hybrid_retrieval.py`, ~22 files with no manual cleanup) — its
   `src.popoto.redis_db.POPOTO_REDIS_DB` is still on DB 0. `Model.save()` writes to DB 0.
4. **Per-test `flushdb()`** (`_popoto_flush_db`) — flushes DB 15 only. DB-0 writes survive.
5. **End of session** — teardown `flushdb()` again hits DB 15 only. DB-0 keys persist to the
   next session.
6. **Flake surface** — `test_subconscious_memory_integration.py` reads DB 15 via `popoto`.
   The cross-test interference manifests when state written by other test files (or stale state)
   changes retrieval ranking for `test_retrieves_multiple_relevant_memories`. The
   deterministic, reproducible root fact is the two-instance / two-DB split; the precise
   ranking-perturbation chain is a build-time reproduction detail (see Risks).

The fix must act at step 2 (or earlier) so that *both* module instances — or the process as a
whole — resolve to DB 15 before any test writes.

## Architectural Impact

- **New dependencies:** none.
- **Interface changes:** none to public ORM API. Changes are confined to the test-harness layer
  (`pytest_plugin.py`, possibly `conftest.py`, possibly an import-normalization sweep of `tests/`).
- **Coupling:** if we normalize imports, coupling *decreases* (one canonical module path). If we
  make the plugin instance-proof, the plugin gains a small amount of `sys.modules` awareness.
- **Data ownership:** none changed.
- **Reversibility:** high. Import normalization is mechanical and revertible. A plugin guard is
  additive.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (one scope decision: which fix shape), code reviewer

**Interactions:**
- PM check-ins: 1-2 (confirm fix shape — see Open Questions)
- Review rounds: 1

This is a well-understood, fully root-caused defect. The work is bounded: pick a fix shape,
implement it in the plugin/conftest, add a regression test that exercises the `src.popoto` path,
and (depending on shape) sweep test imports. The bottleneck is the one design decision, not coding.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli ping` | Plugin and tests need a live server |
| Editable install present | `.venv/bin/python -c "import popoto; print(popoto.__file__)"` | Confirms the `src/` layout the bug depends on |

## Solution

### Key Elements

Three candidate fix shapes (the issue's final "Next Step" asks us to decide). The plan
**recommends Option A as the primary fix and Option C as a permanent safety net**, with Option B
as the lower-value fallback. The PM picks before build (Open Question 1).

- **Option A — Make isolation import-path-proof at the plugin/session level (RECOMMENDED).**
  Resolve the test DB **once**, at process/session start, and apply it to *every* loaded popoto
  module instance, not just `popoto`. Concretely: in `_swap_db` / the session fixture, iterate
  `sys.modules` for any module whose name is `popoto` or ends with `.popoto` (i.e. `src.popoto`)
  and whose `redis_db.POPOTO_REDIS_DB` exists, and swap *each* one's pool to the test DB. Also
  mirror this in the async reset fixture. This neutralizes the second instance no matter how it
  was imported, and is robust to future test files using either path.

- **Option B — Normalize all test imports to one module path.** Mechanically rewrite
  `from src.popoto` / `import src.popoto` → `from popoto` / `import popoto` across `tests/`
  (~59 files, ~22 of which currently lack manual cleanup). Removes the second instance entirely
  *for tests as written*, but is fragile: one future `src.popoto` import silently reintroduces
  the leak, and it does nothing for `tests/benchmarks/` or external users who import `src.popoto`.
  Lower durability than A.

- **Option C — Session-scoped DB-0 guard that fails loudly (SHIP ALONGSIDE A).** Add a
  session-scoped autouse fixture that, at teardown (and optionally per-test), asserts DB 0 has
  not grown during the test session — or more precisely, that no popoto-model keys were written
  to DB 0. If the guard trips, the suite fails with a clear message pointing at module aliasing.
  This converts a silent, flaky failure mode into a loud, deterministic one and protects against
  regressions of A. Must respect a real `REDIS_URL` pointing at DB 0 (skip/relax when the user
  legitimately runs on DB 0 — though the plugin already rejects `popoto_test_db=0`).

### Flow

Build run → plugin session fixture resolves test DB (15) → **swaps every popoto instance in
`sys.modules` to DB 15** (Option A) → per-test `flushdb()` on DB 15 → tests using either import
path write to DB 15 → DB-0 guard (Option C) confirms DB 0 untouched at teardown → green suite.

### Technical Approach

- **Option A implementation point:** generalize `_swap_db` (or add a helper called from the
  session fixture) to enumerate candidate modules:
  ```python
  for name, mod in list(sys.modules.items()):
      if (name == "popoto" or name.endswith(".popoto")) and mod is not None:
          rd = getattr(mod, "redis_db", None)
          if rd is not None and getattr(rd, "POPOTO_REDIS_DB", None) is not None:
              _swap_pool_on(rd, target_db)
  ```
  Save and restore each instance's original kwargs for teardown. Apply the same enumeration in
  `_popoto_reset_async` so the async connection on every instance points at the test DB.
- **Ordering caveat:** instances imported *after* the session fixture runs (a test file imported
  lazily mid-session) would miss the one-time swap. Mitigate by re-running the enumeration in the
  per-test `_popoto_flush_db` fixture (cheap; it already runs every test), so any newly loaded
  instance is corrected before its first write is flushed. Confirm this ordering during build.
- **Option C implementation point:** a session-scoped autouse fixture that snapshots
  `redis.Redis(db=0).dbsize()` (or scans for popoto-prefixed keys) before/after the session and
  asserts no popoto-model keys were added. Use SCAN, not KEYS (per CLAUDE.md), and Redis-core
  commands only (no modules — Valkey compatibility).
- **Regression test:** add to `tests/test_pytest_plugin.py` a test that imports `src.popoto`,
  saves a model through it, and asserts the key lands in the test DB (not DB 0) and is flushed.
  This is the test that PR #302 was missing.

## Failure Path Test Strategy

### Exception Handling Coverage
- `pytest_plugin.py` has `except Exception: pass` blocks in teardown (lines 110-113, 153-165).
  These swallow flush/cleanup errors. The new DB-0 guard (Option C) must NOT be wrapped in a
  silent `except` — its entire purpose is to fail loudly. Add a test asserting the guard raises
  (or fails the session) when a model is deliberately written to DB 0.
- The instance-enumeration swap (Option A) must tolerate a module that has a `redis_db` attr but
  no live connection (skip it) — test with a stub module in `sys.modules`.

### Empty/Invalid Input Handling
- DB resolution already validates non-integer and `db=0` inputs (`pytest_plugin.py:87-97`); keep
  those paths covered. Add a case where `sys.modules` contains a `*.popoto` entry that is `None`
  (partially-imported module) — the enumeration must skip it without raising.

### Error State Rendering
- The DB-0 guard's failure message must name the cause ("test writes detected in DB 0 — likely a
  `src.popoto` vs `popoto` module-aliasing import") so a future developer immediately understands.
  Assert the message content in the guard's test.

## Test Impact

- [ ] `tests/test_pytest_plugin.py` — UPDATE: add regression tests exercising the `src.popoto`
  import path (currently 100% `popoto`-only). This is the core proof the fix works.
- [ ] `tests/test_context_assembler.py` (and the ~22 other files with hand-rolled `_clean_all()`
  autouse fixtures) — KEEP AS-IS for this PR. Once Option A lands, these manual fixtures become
  redundant, but removing them is a separate cleanup (see No-Gos) to keep this PR's diff focused
  and the bisect surface small. They are harmless once isolation works.
- [ ] If Option B is chosen instead of/in addition to A: ~59 test files get import rewrites —
  REPLACE the import lines. Mechanical; verify no test references `src.popoto`-only behavior.

No production (`src/popoto/`) test behavior changes — the fix is harness-layer only.

## Rabbit Holes

- **Chasing the exact ranking-perturbation chain of the one named flake.** The root cause (two
  instances → DB 0 leak) is proven and is what we fix. Spending build time reverse-engineering
  precisely which key in which order tips `test_retrieves_multiple_relevant_memories` is a
  symptom-level tangent. Confirm the flake disappears after the fix; don't dissect it first.
- **Reworking the editable-install / `sys.path` layout** (removing `src/__init__.py`, switching to
  a src-layout-without-namespace, etc.). Tempting as a "real" fix, but it risks breaking the
  package's import surface and every `src.popoto` importer (including benchmarks and possibly
  downstream users). Out of scope — the harness-level fix is lower-risk and sufficient.
- **Globally banning `src.popoto` imports via a lint rule.** Possibly nice, but it's a policy
  change beyond this bug and would need maintainer buy-in. Don't bundle it.
- **Making the plugin import-system-agnostic for arbitrary aliasing** (e.g. someone does
  `importlib` tricks). Cover `popoto` and `*.popoto`; don't build a general module-dedup engine.

## Risks

### Risk 1: Instance enumeration misses a lazily-imported module instance
**Impact:** A test file imported mid-session after the session fixture runs could still write to
DB 0 before the next per-test swap.
**Mitigation:** Re-run the enumeration in the per-test `_popoto_flush_db` fixture (runs before
every test). Verify ordering during build with a test that imports `src.popoto` inside a test body.

### Risk 2: The named flake has a second, independent cause
**Impact:** Fixing isolation doesn't make the flake go away, suggesting another source of
cross-test state.
**Mitigation:** Build step explicitly reproduces the flake in a full-suite run before the fix,
then re-runs after. If it persists, escalate as a separate finding rather than expanding this PR.

### Risk 3: Option C guard false-positives against a legitimate DB-0 application dataset
**Impact:** On a dev box where DB 0 holds real app data (observed: 405 keys here), a naive
"dbsize grew" check could trip on unrelated writes.
**Mitigation:** Scope the guard to popoto-model key patterns written *during the session*
(snapshot-diff of popoto-prefixed keys), not raw dbsize. Allow opt-out via the existing plugin
disable switch.

### Risk 4: Restoring original connections on teardown across N instances leaks pools
**Impact:** Swapping pools on multiple instances without disconnecting old pools could leak
connections.
**Mitigation:** Mirror the existing `_swap_db` pattern (`old_pool.disconnect()`) for every
instance; save per-instance original kwargs in a dict keyed by module name.

## Race Conditions

### Race 1: Module instance imported between session swap and first write
**Location:** `pytest_plugin.py` session fixture vs. test-module import time
**Trigger:** A test file that imports `src.popoto` lazily (inside a function/fixture) loads its
DB-0-default connection after the one-time session swap.
**Data prerequisite:** The test DB swap must be applied to an instance before that instance's
first `Model.save()`.
**State prerequisite:** All popoto instances in `sys.modules` point at the test DB before any
write that will be asserted-on or flushed.
**Mitigation:** Re-apply enumeration in the per-test (function-scoped) flush fixture, which runs
before each test's body — covering any instance loaded since the last test.

### Race 2: Async connection per-instance reset
**Location:** `pytest_plugin.py:_popoto_reset_async`
**Trigger:** Async tests using `src.popoto`'s `get_async_redis_db()` would build a DB-0 async
client lazily.
**Mitigation:** Extend the async reset to set `_POPOTO_ASYNC_REDIS_DB` on every popoto instance,
mirroring the sync enumeration.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #420] Removing the now-redundant per-file `_clean_all()` autouse fixtures from
  the ~22+ suites that hand-rolled them. Deferred to keep this PR's diff focused on the fix +
  regression test; the manual fixtures are harmless once isolation works. Filed under this issue's
  tracking for a follow-up cleanup PR. <!-- tracked on #420 itself; a dedicated cleanup issue
  will be opened at build time if the maintainer wants it split -->
- Changing the `src/` editable-install layout or removing `src/__init__.py` — see Rabbit Holes
  (higher-risk structural change, not needed for the fix).
- Adding a lint/CI rule banning `src.popoto` imports — policy change beyond this bug.

## Update System

No update system changes required — this is an internal test-harness fix with no deployed-machine
footprint.

## Agent Integration

No agent integration required — this is a test-infrastructure change with no MCP or bridge surface.

## Documentation

### Feature Documentation
- [ ] Update CLAUDE.md's testing section: note that isolation now holds for both `popoto` and
  `src.popoto` import paths, and that new test files no longer need a manual cleanup fixture.

### External Documentation Site
- [ ] No docs-site page covers the pytest plugin internals; if one exists under `docs/`, add a
  one-line note. Otherwise state "no docs-site change."

### Inline Documentation
- [ ] Update `pytest_plugin.py` module docstring to document the multi-instance swap and why it
  exists (the `src.popoto` aliasing pitfall).
- [ ] Comment the DB-0 guard explaining the failure it catches.

## Success Criteria

- [ ] `import popoto; import src.popoto` both resolve to DB 15 under the plugin in a running test
  (asserted by a new test in `tests/test_pytest_plugin.py`).
- [ ] A model saved via `src.popoto` during a test writes to the test DB, not DB 0, and is flushed
  between tests.
- [ ] DB 0 key count is unchanged by a full test-suite run (verified manually:
  `redis-cli -n 0 dbsize` before == after, on a box where DB 0 is otherwise idle).
- [ ] `tests/test_subconscious_memory_integration.py::TestRetrievalRelevance::test_retrieves_multiple_relevant_memories`
  passes in a full-suite run (`pytest`), not just in isolation.
- [ ] Option C guard fails loudly (with an aliasing-specific message) when a model is deliberately
  written to DB 0 in a test — proven by a dedicated test.
- [ ] Full suite passes (`pytest`).
- [ ] Documentation updated (CLAUDE.md testing note + plugin docstring).

## Team Orchestration

### Team Members

- **Builder (plugin)**
  - Name: plugin-builder
  - Role: Implement the chosen fix shape (Option A swap-all-instances + Option C DB-0 guard) in `pytest_plugin.py`
  - Agent Type: builder
  - Resume: true

- **Builder (regression-tests)**
  - Name: test-builder
  - Role: Add `src.popoto`-path regression tests and the DB-0-guard test to `tests/test_pytest_plugin.py`; reproduce the named flake before/after
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: isolation-validator
  - Role: Verify both import paths resolve to DB 15, DB 0 stays clean across a full run, and the flake is gone
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update CLAUDE.md testing note and plugin docstring
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Reproduce the flake on current main
- **Task ID**: repro-flake
- **Depends On**: none
- **Validates**: documents pre-fix failure of `tests/test_subconscious_memory_integration.py::TestRetrievalRelevance::test_retrieves_multiple_relevant_memories` in a full-suite run
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Run the full suite, capture the failure; snapshot `redis-cli -n 0 dbsize` before/after to quantify the leak.
- Confirm the named test passes in isolation. Record both results for the PR.

### 2. Implement import-path-proof isolation + DB-0 guard
- **Task ID**: build-plugin
- **Depends On**: none
- **Validates**: tests/test_pytest_plugin.py (new cases added in task 3 must pass against this)
- **Informed By**: Freshness Check (confirmed two-instance split), Solution Option A + C
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: true
- Generalize the connection swap to enumerate every `popoto` / `*.popoto` module in `sys.modules` and swap each instance's `POPOTO_REDIS_DB` pool to the test DB; save per-instance originals for teardown.
- Re-apply the enumeration in the per-test `_popoto_flush_db` fixture and extend `_popoto_reset_async` to cover every instance (Race 1 & 2).
- Add a session-scoped DB-0 guard that fails loudly (aliasing-specific message) if popoto-model keys appear in DB 0 during the session; scope to popoto-prefixed keys, use SCAN, Redis-core only.

### 3. Add regression tests
- **Task ID**: build-tests
- **Depends On**: build-plugin
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Add a test that imports `src.popoto`, saves a model, asserts the key is on the test DB (not DB 0) and is flushed between tests.
- Add a test asserting the DB-0 guard fails (with the right message) when a model is written to DB 0.
- Re-run the full suite and confirm the named flake now passes.

### 4. Validate isolation end-to-end
- **Task ID**: validate-isolation
- **Depends On**: build-tests
- **Assigned To**: isolation-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify both import paths resolve to DB 15; DB 0 dbsize unchanged across a full run on an idle DB 0; flake gone; full suite green.

### 5. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-isolation
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update CLAUDE.md testing section and `pytest_plugin.py` docstring.

### 6. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: isolation-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification checks; confirm every success criterion; generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full suite passes | `pytest -q` | exit code 0 |
| Plugin regression tests pass | `pytest tests/test_pytest_plugin.py -q` | exit code 0 |
| Named flake passes in full run | `pytest tests/test_subconscious_memory_integration.py::TestRetrievalRelevance::test_retrieves_multiple_relevant_memories -q` | exit code 0 |
| Both module instances on test DB | `pytest tests/test_pytest_plugin.py -k "src_popoto" -q` | exit code 0 |
| DB 0 not polluted by a run (idle DB 0) | `redis-cli -n 0 flushdb && pytest -q >/dev/null; redis-cli -n 0 dbsize` | output contains `0` |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

1. **Fix shape:** The plan recommends Option A (swap-all-instances) + Option C (loud DB-0 guard),
   treating Option B (rewrite all `src.popoto` imports) as a fragile fallback. Confirm A+C, or
   prefer B, or want all three?
2. **Redundant `_clean_all()` cleanup:** Once isolation holds, the ~22+ hand-rolled per-file
   cleanup fixtures become dead weight. Remove them now (bigger diff, cleaner tree) or in a
   separate follow-up PR (recommended — keeps this PR focused)?
3. **DB-0 guard strictness:** Should the guard run per-test (catches the offending file
   immediately, slower) or session-scoped at teardown (cheaper, less precise)? Recommendation:
   session-scoped, with per-test as an opt-in via env var.
