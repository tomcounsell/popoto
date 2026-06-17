---
status: docs_complete
type: bug
appetite: Medium
owner: valor
created: 2026-06-15
tracking: https://github.com/tomcounsell/popoto/issues/420
last_comment_id:
revision_applied: true
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
- New test files do not each need to hand-roll a `_clean_all()` autouse fixture to
  be stable.

> **Scope of the leak (re-baselined after critique):** **75** of the suite's
> test files import via `src.popoto` (the leaking path) — this is the *majority*
> of the suite, not an edge case. **37** files are `popoto`-only (correctly
> isolated today). **8** files hand-roll a `_clean_all()` fixture. The leak being
> the common case raises severity and has a consequence: when the fix flips all 75
> files onto a *flushed* DB 15, latent test failures previously masked by
> persistent DB-0 state may surface. This is an expected, budgeted risk (see Risks),
> not a regression of the fix.

> **The named flake is NOT a confirmed symptom of this leak (correction).** The
> flake (`tests/test_subconscious_memory_integration.py::TestRetrievalRelevance::test_retrieves_multiple_relevant_memories`)
> imports `from popoto.redis_db import POPOTO_REDIS_DB` (DB 15, verified line 38)
> and runs its own `_clean_all()` on DB 15 (lines 96-121). A leak that lands in
> **DB 0** cannot perturb a reader isolated on **DB 15**. Therefore "the flake stops
> failing" is treated as a *hypothesis under test* (gated on a bisect in Task 1),
> **not** a desired outcome or success criterion of this fix. If the bisect shows
> the flake is caused by DB-15 contention from a sibling file, that is a *separate*
> real bug to be re-scoped, not evidence for or against this fix.

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
   `tests/test_hybrid_retrieval.py`, **75 files total**, only 8 of which carry a manual
   `_clean_all()`) — its `src.popoto.redis_db.POPOTO_REDIS_DB` is still on DB 0. `Model.save()`
   writes to DB 0. Note: the leak also reaches `$`-namespaced side-effect keys (e.g.
   `$BM25:{Class}:{field}:...`), not just `ClassName:*` keys — there is no single popoto prefix.
4. **Per-test `flushdb()`** (`_popoto_flush_db`) — flushes DB 15 only. DB-0 writes survive.
5. **End of session** — teardown `flushdb()` again hits DB 15 only. DB-0 keys persist to the
   next session.

The fix must act at step 2 (or earlier) so that *both* module instances — or the process as a
whole, by collapsing them into one — resolve to DB 15 before any test writes.

**On the named flake:** `test_subconscious_memory_integration.py` reads DB 15 via `popoto`
(line 38) and self-cleans DB 15. The DB-0 leak above cannot reach it. The flake's true trigger
is unknown until the Task-1 bisect runs; it is intentionally excluded from this fix's
causal claims (see Problem). Do not assume the two-instance split explains the flake.

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

This is a well-understood, fully root-caused defect. The work is bounded: pick a fix shape
(Option D recommended), implement it in `pytest_plugin.py` (`pytest_configure`), add regression
tests for both `src.popoto` import paths, run the Task-1 flake bisect, and budget triage time for
latent failures that surface when 75 files move onto a flushed DB 15. The bottleneck is the one
design decision plus latent-failure triage, not the core coding.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli ping` | Plugin and tests need a live server |
| Editable install present | `.venv/bin/python -c "import popoto; print(popoto.__file__)"` | Confirms the `src/` layout the bug depends on |

## Solution

### Key Elements

Four candidate fix shapes. After critique, the plan **recommends Option D (alias-collapse)** as
the primary fix — it is the lowest-machinery, lowest-risk durable shape and eliminates the races
and pool-restore risks that Options A/C carry by construction. Corrected Option A is the fallback
if D proves order-fragile in practice. Option C is demoted to a clean-DB-only CI tripwire (not a
runtime guard). Option B is not recommended. The PM picks before build (Open Question 1).

- **Option D — Alias-collapse so the second instance never exists (RECOMMENDED).**
  In `pytest_configure` (the earliest safe hook, before any test-module import), register
  `src.popoto` and its submodules as the *same objects* as `popoto`, so the duplicate instance is
  never created and the DB-15 swap (which already works on the single `popoto` instance) covers
  everything. Empirically verified at plan time:
  ```python
  # pytest_configure(config):
  import importlib, sys, popoto
  importlib.import_module("popoto.redis_db")           # ensure submodules loaded
  src_pkg = sys.modules.get("src") or _ensure_src_pkg()
  setattr(src_pkg, "popoto", popoto)                   # parent-attr bind (NOT just setdefault)
  sys.modules["src.popoto"] = popoto
  for name, mod in list(sys.modules.items()):           # alias already-loaded submodules
      if name == "popoto" or name.startswith("popoto."):
          sys.modules["src." + name] = mod
  ```
  Verified result: `src.popoto is popoto`, `src.popoto.redis_db is popoto.redis_db`,
  `src.popoto.redis_db.POPOTO_REDIS_DB is popoto.redis_db.POPOTO_REDIS_DB` — all `True`. This
  collapses Options A, B, and the runtime portion of C into a handful of lines and removes
  Races 1/2 and Risks 1/4 entirely (there is only one instance to swap).
  **Caveats found in testing (must be honored):** (1) a naive 3-line `setdefault` is **not**
  sufficient — you must bind the attr on the `src` package object *and* alias already-loaded
  `popoto.*` submodules; (2) it is order-sensitive — it must run before the first `src.popoto.*`
  import, so `pytest_configure` (not a fixture) is the correct hook; (3) it also repairs the
  intentional `src.popoto` coupling in `conftest.py:31-34` (the embedding listener registry),
  which a path-rewrite (Option B) would silently break.

- **Option A — Make isolation import-path-proof by swapping every instance (FALLBACK, re-spec'd).**
  Keep two instances alive but swap the DB on *all* of them. The original matcher
  (`name.endswith(".popoto")`) was rejected by critique: it **over-reaches** (would `disconnect()`
  an unrelated downstream `acme.popoto` / vendored fork during that user's own session — the
  plugin is a public auto-registered entry point) and **under-reaches** (the leak lives in the
  `redis_db` *submodule*; `sys.modules` holds `src.popoto.redis_db`, and direct-submodule imports
  like `import src.popoto.redis_db as X` at `tests/test_stress.py:108` are missed). Re-spec:
  match on **module identity, not name suffix** — enumerate modules whose `__file__` equals the
  plugin's own canonical `popoto/__init__.py` (resolve via `popoto.__file__`), and for each, swap
  `POPOTO_REDIS_DB` on its `redis_db` *submodule object directly* (also identity-matched against
  the canonical `redis_db.py`). This touches only true duplicate-load instances of *this* package
  and never a same-named downstream module.

- **Option B — Normalize all test imports to one module path (NOT recommended).** Mechanically
  rewrite `from src.popoto` / `import src.popoto` → `popoto` across `tests/` (**75 files**).
  Fragile (one future `src.popoto` import reintroduces the leak), does nothing for benchmarks or
  downstream users, and **silently breaks the intentional `src.popoto` coupling at
  `conftest.py:31-34`**. Lowest durability.

- **Option C — Clean-DB-0-only CI tripwire (SHIP ALONGSIDE D/A, re-spec'd).** The original
  loud-runtime-guard design was rejected as unworkable: (1) there is **no single popoto key
  prefix** — instance keys are `ClassName:*` but BM25/side-effect keys are `$BM25:{Class}:...`
  (verified `bm25_field.py:18-20`), so a class-name SCAN misses exactly the `$BM25:*` keys the
  issue reported leaking, and a guard that *passes* would falsely certify the leak as safe; (2) on
  a real dev box DB 0 holds ~405 app keys including `Memory:*`, so the guard cannot distinguish a
  test leak from concurrent app writes → false positives → devs disable it; (3) downstream users
  commonly run with no `REDIS_URL` (app legitimately on DB 0), so the guard misfires for the
  default setup and its `src.popoto`-aliasing message is jargon to them. Re-spec: make it a
  **`dbsize` delta tripwire that runs only when DB 0 is verifiably idle/clean** (CI, or an
  explicit opt-in). On a non-idle DB 0 it emits a loud **SKIP** (never a failure). Message is
  generic ("test writes leaked into DB 0 — DB isolation may be bypassed"), not aliasing jargon.
  The *real* fix-proof is the regression test below, which needs no idle DB 0.

### Flow

Build run → `pytest_configure` collapses `src.popoto` → `popoto` (Option D) → plugin session
fixture swaps the single `popoto` instance to DB 15 → per-test `flushdb()` on DB 15 → tests using
either import path write to DB 15 (same object) → CI tripwire (Option C) confirms DB 0 untouched
*only when DB 0 is clean*, else SKIP → green suite.

### Technical Approach

- **Option D implementation point:** add `pytest_configure(config)` to `pytest_plugin.py`. Ensure
  `popoto.redis_db` (and any submodule the suite imports via `src.popoto.*`) is imported, then
  bind the attr on the `src` package and alias every loaded `popoto.*` entry into `src.popoto.*`.
  Idempotent and order-safe because `pytest_configure` runs before test collection/import. No
  per-test re-scan and no async mirroring needed — there is one instance.
- **Option A implementation point (only if D is rejected):** add a helper called from the session
  fixture that enumerates modules by `__file__` identity (not name) against `popoto.__file__`, and
  swaps `POPOTO_REDIS_DB` on each instance's `redis_db` submodule object directly (identity-matched
  against `popoto.redis_db.__file__`). Save per-instance original kwargs for teardown; mirror the
  existing `old_pool.disconnect()` pattern per instance. Re-run the enumeration in the per-test
  `_popoto_flush_db` fixture (covers lazily-imported instances) and extend `_popoto_reset_async`.
- **Option C implementation point:** a session-scoped autouse fixture that, *only if* DB 0 is
  clean at session start (`dbsize == 0`), snapshots `dbsize` and asserts it is still 0 at
  teardown; otherwise it logs a loud SKIP and does nothing. Use Redis-core commands only (Valkey
  compatibility); no key-pattern scanning. Generic, downstream-safe message.
- **Regression test (the keeper — what PR #302 was missing):** in `tests/test_pytest_plugin.py`,
  (a) import `src.popoto`, save a model, assert the key lands on the test DB (not DB 0) and is
  flushed between tests; (b) the **direct-submodule** path `import src.popoto.redis_db as X`
  (mirrors `test_stress.py:108`) resolves to the test DB; (c) for Option A only — a decoy
  `sys.modules["acme.popoto"]` with a stub `redis_db` is left **untouched** (proves identity
  matching doesn't stomp unrelated downstream modules). Under Option D, (c) is moot because nothing
  is enumerated, but keep (a) and (b) as the cross-import-path proof.

## Failure Path Test Strategy

### Exception Handling Coverage
- `pytest_plugin.py` has `except Exception: pass` blocks in teardown (lines 110-113, 153-165).
  These swallow flush/cleanup errors. The Option C CI tripwire must NOT be wrapped in a silent
  `except` when it *does* run (clean DB 0) — its purpose is to fail loudly there. But it must
  itself never crash the session on a non-idle DB 0: that path emits a SKIP, not an error.
- **Option D:** `pytest_configure` must be tolerant if `src` is not importable (no `src/` layout
  in a downstream consumer) — bind only what exists; never raise. Test with `src` absent.
- **Option A (fallback only):** the identity-matched enumeration must tolerate a module that has a
  `redis_db` attr but no live connection (skip it) — test with a stub module in `sys.modules`.

### Empty/Invalid Input Handling
- DB resolution already validates non-integer and `db=0` inputs (`pytest_plugin.py:87-97`); keep
  those paths covered.
- **Option D:** if `src.popoto` was *already* imported as a distinct object before
  `pytest_configure` (shouldn't happen, but defend), overwrite the `sys.modules` entry with the
  canonical `popoto` and log it; assert idempotency on a second call.
- **Option A (fallback only):** a `sys.modules` entry whose `__file__` is missing/None
  (partially-imported or namespace module) must be skipped without raising.

### Error State Rendering
- The Option C tripwire's message must be **generic and downstream-safe** ("test writes leaked
  into DB 0 — DB isolation may be bypassed"), NOT aliasing jargon, because downstream users have
  no `src/` layout. Assert the message content (and the SKIP-on-non-idle-DB-0 path) in its test.

## Test Impact

- [ ] `tests/test_pytest_plugin.py` — UPDATE: add regression tests exercising the `src.popoto`
  top-level import path AND the direct-submodule path (`import src.popoto.redis_db as X`,
  mirroring `test_stress.py:108`). Currently 100% `popoto`-only. This is the core proof the fix
  works and the gate PR #302 was missing.
- [ ] `tests/test_context_assembler.py` (and the other files among the **8** that hand-roll a
  `_clean_all()` fixture) — KEEP AS-IS for this PR. **Caveat (critique correction):** "harmless
  once isolation works" is NOT universally true — some of these files already ran on DB 15 yet
  *still* needed `_clean_all()`, which means they have an intra-DB-15 contention reason
  independent of the leak. Removing them is deferred (No-Gos) and must be evaluated per-file, not
  assumed safe.
- [ ] **Latent-failure triage budget:** flipping 75 `src.popoto` files onto a flushed DB 15 may
  surface test failures previously masked by persistent DB-0 state. Build must budget time to
  triage these and classify each as (a) pre-existing latent bug to file separately, or (b) a real
  regression of the fix. Do NOT silently absorb them into this PR.
- [ ] Option B (not recommended) would rewrite **75** files and break `conftest.py:31-34` — only
  if explicitly chosen by PM.

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

### Risk 1: Latent test failures surface when 75 files move onto a flushed DB 15
**Impact:** Tests previously passing only because stale DB-0 state happened to satisfy them will
now run against a clean DB 15 and may fail. This is the *correct* behavior (those tests were
relying on pollution), but it can look like the fix "broke" things.
**Mitigation:** Task 1 quantifies the pre-fix DB-0 leak; build budgets triage time to classify
each new failure as pre-existing-latent (file separately) vs. fix-regression. Treat surfacing
latent failures as expected, not as a reason to revert.

### Risk 2: Option D alias-collapse is order-sensitive / incomplete
**Impact:** If the collapse runs after a `src.popoto.*` import, or misses a submodule, a duplicate
instance can still exist for that submodule and leak.
**Mitigation:** Run it in `pytest_configure` (before collection/import — verified safe point), and
alias *all* already-loaded `popoto.*` submodules, not just the package. Regression test (b) covers
the direct-submodule path. Build asserts `src.popoto is popoto` at test time.

### Risk 3: Option C tripwire false-positives against a real DB-0 dataset
**Impact:** On a box where DB 0 holds real app data (observed ~405 keys), any dbsize check trips on
unrelated writes; there is no popoto-only prefix to filter by (`$BM25:*` defeats class-name scans).
**Mitigation:** The re-spec'd tripwire runs *only* when DB 0 is verifiably idle (`dbsize == 0` at
session start); otherwise it SKIPs loudly. It never scans patterns and never fails on a non-idle
DB 0. The durable fix-proof is the regression test, not the tripwire.

### Risk 4: The named flake is not fixed by this change (and is a separate bug)
**Impact:** The flake reads DB 15 and self-cleans DB 15; the DB-0 leak cannot reach it. The fix may
leave it failing, or — by routing 75 files' writes into DB 15 — change DB-15 contention.
**Mitigation:** Task 1 is a *gating bisect* (identify the triggering sibling file and which DB it
writes). If the trigger is DB-15 contention, that is re-scoped as a separate issue; this PR makes
no flake-fix claim. "Flake fixed" is removed from Success Criteria.

## Race Conditions

> Under the **recommended Option D**, there are no live duplicate instances to race-patch, so the
> races below **do not exist**. They apply *only if the PM selects fallback Option A*.

### Race A1 (Option A only): Instance imported between session swap and first write
**Location:** `pytest_plugin.py` session fixture vs. test-module import time
**Trigger:** A test file importing `src.popoto` lazily (inside a function/fixture) loads its
DB-0-default connection after the one-time session swap.
**Mitigation:** Re-apply the identity-matched enumeration in the per-test (function-scoped) flush
fixture, which runs before each test body.

### Race A2 (Option A only): Async connection per-instance reset
**Location:** `pytest_plugin.py:_popoto_reset_async`
**Trigger:** Async tests using a second instance's `get_async_redis_db()` build a DB-0 async client
lazily.
**Mitigation:** Extend the async reset to set `_POPOTO_ASYNC_REDIS_DB` on every identity-matched
instance.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #420] Removing the per-file `_clean_all()` fixtures from the **8** suites that
  hand-rolled them. Deferred to keep this PR focused. **Not assumed harmless:** some of these
  files already ran on DB 15 yet still needed `_clean_all()`, implying an intra-DB-15 contention
  reason independent of the leak — each must be evaluated per-file before removal, not bulk-deleted.
  Filed under this issue's tracking for a follow-up cleanup PR.
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
- [ ] Update `pytest_plugin.py` module docstring to document the alias-collapse (Option D) and why
  it exists (the `src.popoto` duplicate-instance pitfall) — or, if Option A, the identity-matched
  multi-instance swap.
- [ ] Comment the Option C tripwire explaining the clean-DB-0-only condition and the SKIP path.

## Success Criteria

- [ ] `import popoto; import src.popoto` both resolve to DB 15 under the plugin in a running test
  (asserted by a new test in `tests/test_pytest_plugin.py`). Under Option D, assert
  `src.popoto is popoto`.
- [ ] A model saved via `src.popoto` (top-level) AND via `import src.popoto.redis_db as X`
  (direct submodule) during a test writes to the test DB, not DB 0, and is flushed between tests.
- [ ] **(Option A fallback only)** A decoy `sys.modules["acme.popoto"]` is left untouched by the
  swap — proven by a dedicated test (identity matching does not stomp unrelated downstream modules).
- [ ] DB 0 is unpolluted by a full run — verified by an **isolated-DB subprocess test** (spawns
  pytest against a stand-in DB, asserts that DB stays empty), NOT by destructively flushing the
  developer's real DB 0.
- [ ] Option C tripwire: when DB 0 is clean it fails loudly (generic message) on a deliberate DB-0
  write; when DB 0 is non-idle it SKIPs loudly without failing — both proven by dedicated tests.
- [ ] Task 1 bisect result recorded: which sibling file triggers the named flake and which DB it
  writes. (NOT a pass/fail gate on this fix — a diagnostic that decides whether the flake is a
  separate issue. The flake is explicitly NOT a success criterion of this PR.)
- [ ] Latent failures surfaced by moving 75 files to a flushed DB 15 are triaged and classified
  (pre-existing vs. regression); pre-existing ones filed separately.
- [ ] Full suite passes (`pytest`), or any remaining failures are documented as pre-existing latent
  bugs with separate issues filed.
- [ ] Documentation updated (CLAUDE.md testing note + plugin docstring).

## Team Orchestration

### Team Members

- **Builder (plugin)**
  - Name: plugin-builder
  - Role: Implement the chosen fix shape (Option D alias-collapse in `pytest_configure`, recommended; or identity-matched Option A) + Option C clean-DB-0 tripwire in `pytest_plugin.py`
  - Agent Type: builder
  - Resume: true

- **Builder (regression-tests)**
  - Name: test-builder
  - Role: Run the gating flake bisect (Task 1); add `src.popoto` top-level + direct-submodule regression tests, the isolated-DB subprocess test, and the Option C tripwire tests to `tests/test_pytest_plugin.py`
  - Agent Type: test-engineer
  - Resume: true

- **Validator**
  - Name: isolation-validator
  - Role: Verify both import paths resolve to DB 15 (Option D: `src.popoto is popoto`), DB 0 stays clean (isolated-DB subprocess test), latent failures are triaged, and the bisect result is recorded
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update CLAUDE.md testing note and plugin docstring
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Gating flake bisect (NOT a flake-fix gate)
- **Task ID**: bisect-flake
- **Depends On**: none
- **Validates**: determines whether the named flake is caused by this leak (DB 0) or by DB-15 contention (a separate bug). Result is a diagnostic, not a pass/fail criterion for this fix.
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Reproduce the flake in a full-suite run; confirm it passes in isolation. Bisect to find which preceding sibling file triggers it, and observe which DB that file writes to (instrument or `redis-cli MONITOR`).
- **Decision gate:** if the trigger writes **DB 15**, the leak is NOT the flake's cause — record this, file a separate issue for the DB-15 contention, and proceed with the isolation fix on its own merits (do NOT claim the fix resolves the flake). If it writes **DB 0**, note the (surprising) mechanism for the validator to re-check after the fix.
- Snapshot `redis-cli -n 0 dbsize` before/after a full run to quantify the leak for the PR.

### 2. Implement alias-collapse (Option D) + clean-DB-0 tripwire
- **Task ID**: build-plugin
- **Depends On**: none
- **Validates**: tests/test_pytest_plugin.py (new cases added in task 3 must pass against this)
- **Informed By**: Freshness Check (two-instance split), Solution Option D (recommended) / Option A (fallback) + Option C
- **Assigned To**: plugin-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `pytest_configure` to `pytest_plugin.py` that collapses `src.popoto` (and all loaded `popoto.*` submodules) onto the canonical `popoto` objects via parent-attr bind + `sys.modules` aliasing (NOT bare `setdefault`). Tolerant if `src` is absent. Idempotent.
- **(Fallback only, if PM rejects D):** identity-matched (`__file__`-based, not name-suffix) instance enumeration swapping `POPOTO_REDIS_DB` on each `redis_db` submodule directly; per-instance pool restore; re-scan in `_popoto_flush_db`; mirror in `_popoto_reset_async`.
- Add the Option C tripwire: runs only when DB 0 is verifiably clean (`dbsize == 0` at session start), asserts it stays 0; on a non-idle DB 0 emits a loud SKIP (never a failure). Generic, downstream-safe message; Redis-core only; no pattern scanning.

### 3. Add regression tests
- **Task ID**: build-tests
- **Depends On**: build-plugin
- **Assigned To**: test-builder
- **Agent Type**: test-engineer
- **Parallel**: false
- Test: import `src.popoto` (top-level), save a model, assert the key is on the test DB (not DB 0) and flushed between tests. Under Option D, also assert `src.popoto is popoto`.
- Test: the direct-submodule path `import src.popoto.redis_db as X` (mirrors `test_stress.py:108`) resolves to the test DB.
- **(Option A fallback only)** Test: a decoy `sys.modules["acme.popoto"]` stub is left untouched by the swap.
- Test: an isolated-DB subprocess test (spawn pytest against a stand-in DB) asserts DB 0 / the real default DB is never written — replaces the destructive manual flush.
- Test: Option C tripwire fails loudly on a clean DB 0 with a deliberate DB-0 write, and SKIPs on a non-idle DB 0.

### 4. Validate isolation end-to-end
- **Task ID**: validate-isolation
- **Depends On**: build-tests
- **Assigned To**: isolation-validator
- **Agent Type**: validator
- **Parallel**: false
- Verify both import paths resolve to the test DB; isolated-DB subprocess test confirms DB 0 untouched; triage any latent failures surfaced by 75 files moving to a flushed DB 15 (classify pre-existing vs. regression, file pre-existing separately); record the Task-1 bisect outcome. Do NOT gate on the flake passing.

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
| Full suite passes (or remaining failures filed as pre-existing) | `pytest -q` | exit code 0, or documented latent failures |
| Plugin regression tests pass | `pytest tests/test_pytest_plugin.py -q` | exit code 0 |
| Both `src.popoto` import paths resolve to test DB | `pytest tests/test_pytest_plugin.py -k "src_popoto" -q` | exit code 0 |
| DB 0 not polluted (isolated-DB subprocess test — non-destructive) | `pytest tests/test_pytest_plugin.py -k "isolated_db_subprocess" -q` | exit code 0 |
| Option C tripwire (clean-DB-0 fail + non-idle SKIP) | `pytest tests/test_pytest_plugin.py -k "db0_tripwire" -q` | exit code 0 |
| Flake bisect outcome recorded (diagnostic, not a gate) | see Task 1 notes in PR | DB written by trigger identified; separate issue filed if DB 15 |

## Critique Results

**Verdict: NEEDS REVISION** — run 2026-06-15 (war room: Skeptic, Operator, Adversary, Simplifier, Archaeologist, User + structural checks).

Root cause is correct and empirically re-confirmed (two distinct module objects, distinct `redis_db.POPOTO_REDIS_DB`, plugin swaps only `popoto`). But the critique surfaced **three blockers** that must be resolved before build, plus a simpler fix shape the plan never considered.

### BLOCKER 1 — The flake-causation claim is internally contradicted (Skeptic + Archaeologist, independently)
The plan's headline desired-outcome and Success Criterion #4 bet on Option A making the named flake pass. But the flake file imports `from popoto.redis_db import POPOTO_REDIS_DB` (DB 15) — verified at `tests/test_subconscious_memory_integration.py:38` — and runs its own `_clean_all()` on DB 15. The leaking `src.popoto` tests write to **DB 0**. `_popoto_flush_db` (autouse) unconditionally flushes DB 15 before every test. **A DB-0 leak cannot perturb a DB-15-isolated reader.** So Option A may not touch the flake at all — and by moving 75 files' writes *into* DB 15, could make DB-15 contention worse. The plan demotes this to Risk 2; it is actually the load-bearing hypothesis.
**Required revision:** Make Task 1 (repro-flake) a *gating bisect*: identify which preceding test file triggers the flake and whether it writes DB 0 or DB 15. If DB 15, the diagnosis is wrong for the flake and Option A is fixing a different (real) bug — re-scope. Demote "flake fixed" from a success criterion to a hypothesis under test.

### BLOCKER 2 — Option A's `name.endswith(".popoto")` matcher is both over- and under-reaching (Adversary + User)
- **Over-reach (User):** The plugin is a public auto-registered entry point. Matching any `sys.modules` name ending in `.popoto` will swap and `disconnect()` the connection of an *unrelated* downstream module (`acme.popoto`, a vendored fork `myapp.vendor.popoto`) during that user's own test session — strictly worse than today's under-reach.
- **Under-reach (Adversary):** The leak lives in the `redis_db` *submodule* object. `sys.modules` holds `src.popoto.redis_db` (ends in `.redis_db`, not `.popoto`). Direct-submodule imports (`import src.popoto.redis_db as X`, verified shape at `tests/test_stress.py:108`) and lazy package access can leave the package object without `redis_db` resolvable via `getattr` — the candidate is skipped, leak persists. The regression test (import top-level `src.popoto`) exercises only the one path that works, giving false confidence.
**Required revision:** Match on **module identity, not name suffix** — enumerate modules whose `__file__` equals the plugin's own canonical `popoto/__init__.py` (and the corresponding `redis_db.py`). Swap `POPOTO_REDIS_DB` on the `redis_db` submodule directly. Add regression tests for (a) a decoy `sys.modules["acme.popoto"]` that must be left untouched, and (b) the `import src.popoto.redis_db as X` direct-submodule path.

### BLOCKER 3 — Option C's DB-0 guard is unworkable as specified (Operator + Adversary + User, converging)
- There is **no single popoto key prefix** to scan. Instance keys are `ClassName:*`, indexes `ClassName:_field`, and side-effect fields use `$`-namespaces — BM25 is `$BM25:{Class}:{field}:...` (verified `bm25_field.py:18-20`). A class-name SCAN misses every `$`-prefixed key — i.e. exactly the `$BM25:Memory:*` keys the issue reported leaking. A loud guard that *passes* would then certify the leak as safe.
- On the dev box DB 0 holds 405 real keys including `Memory:*`; the guard cannot distinguish a test leak from the real app writing `Memory:*` concurrently. False-positive → devs disable it → zero protection.
- For downstream users the common case is **no `REDIS_URL`** (app legitimately on DB 0); the guard misfires for the default setup, and its `src.popoto`-aliasing message is jargon to someone with no `src/` layout.
**Required revision:** Drop pattern-scanning. Make the guard a **CI/clean-DB-0-only `dbsize` delta tripwire** (the Verification-table row already assumes a flushed DB 0); on a non-idle DB 0 emit a loud SKIP, not a failure. Make the real fix-proof the regression test (save via aliased import → assert key on DB 15), which needs no idle DB 0. The destructive `redis-cli -n 0 flushdb && pytest` verification row is unsafe on a real DB 0 — replace with a subprocess test against an isolated stand-in DB.

### MAJOR — A simpler fix shape was never considered (Simplifier; strongly seconded)
Instead of *chasing* the second instance with a permanent `sys.modules`-enumeration engine + per-test re-scan + async mirroring + a guard (Option A+C, with Risks 1/4 and Races 1/2 existing only because two live instances are raced-to-patch), **collapse the alias so the second instance never exists**: register `src.popoto` (and its submodules) as the same object as `popoto` in `conftest.py` / `pytest_configure`, before any test import. Verified empirically: with parent-attr binding + submodule aliasing, `src.popoto is popoto`, shared `redis_db`, shared `POPOTO_REDIS_DB`. This collapses A, B, and most of C into a handful of lines and eliminates Races 1/2 and Risks 1/4 by construction. Caveats found in testing: a naive 3-line `setdefault` is **not** sufficient (must bind the attr on the `src` package and alias already-loaded `popoto.*` submodules), and it is order-sensitive (must run before the first `src.popoto.*` import — `pytest_configure` is the safe point). Note (Archaeologist): `tests/conftest.py:34` *deliberately* imports `from src.popoto...` to match the `src.popoto` listener registry — collapse fixes that latent split too, but Option B (rewrite-to-`popoto`) would silently break it.
**Required revision:** Add alias-collapse as Option D and have the PM weigh it against the corrected Option A in Open Question 1. It is the lower-machinery, lower-risk durable fix.

### Corrections to plan facts (Archaeologist + Skeptic + structural checks)
- **75** test files import `src.popoto` (not "~22" / "~59"); **37** are `popoto`-only; **8** files use `_clean_all` (verified). The leaking path is the *majority* of the suite, not an edge case — this inverts the severity framing and means flipping 75 files onto a flushed DB 15 may **surface latent failures** previously masked by persistent DB-0 state. Budget for triage; "harmless once isolation works" (lines 251-252, 322-326) is false for the files that already ran on DB 15 yet still needed `_clean_all`.
- Option B blast radius is ~75 files (not 59), and B contradicts the intentional `src.popoto` coupling in `conftest.py:31-34`.

### What holds up
Root-cause diagnosis (the two-instance split) is solid and re-verified. The regression test (`tests/test_pytest_plugin.py`: import via the aliased path, save, assert DB 15 + flushed) is the correct keeper and the gate PR #302 was missing — keep it regardless of fix shape. Reversibility and harness-only scoping are sound. The decision to defer the `src/__init__.py` layout change is defensible (but record whether any *non-test* code imports `src.popoto` — if none, the "breaks downstream" justification weakens).

### Required actions before build
1. Re-baseline file counts (75 / 37 / 8) throughout the plan.
2. Gate on the flake bisect (Blocker 1) before committing to Option A as the flake fix.
3. Re-spec Option A matcher to identity-based + submodule-direct, or adopt Option D (alias-collapse) (Blockers 2, Major).
4. Re-spec Option C as a CI/clean-DB-only tripwire with a generic, downstream-safe message; drop pattern-scanning (Blocker 3).
5. Replace the destructive manual DB-0 verification with an automated isolated-DB subprocess test.
6. PM to decide fix shape (Open Question 1) with Option D on the table.

### Revision applied — 2026-06-15 (all six required actions addressed)

1. **File counts re-baselined to 75 / 37 / 8** throughout (Problem, Data Flow, Test Impact, Risks,
   No-Gos, Open Questions). Re-verified by `grep -rlE`: 75 import `src.popoto`, 37 are
   `popoto`-only, 8 use `_clean_all`. Severity reframed: the leak is the *majority* path.
2. **Flake demoted to a hypothesis under test.** Removed from Desired Outcome and Success Criteria.
   Task 1 is now a *gating bisect* with an explicit decision gate: if the trigger writes DB 15, the
   leak is not the cause → file a separate issue, make no flake-fix claim. Re-verified the flake
   imports `from popoto.redis_db` (DB 15, line 38) and self-cleans DB 15.
3. **Option A matcher re-spec'd to identity-based** (`__file__` equals `popoto.__file__`) **with
   direct `redis_db`-submodule swap**; added decoy `acme.popoto` (must be untouched) and
   `import src.popoto.redis_db as X` direct-submodule regression tests. Verified `test_stress.py:108`
   uses the direct-submodule form.
4. **Option D (alias-collapse) added and recommended.** Empirically verified at plan time:
   `src.popoto is popoto`, shared `redis_db`, shared `POPOTO_REDIS_DB` (all True) after
   parent-attr bind + `sys.modules` submodule aliasing in `pytest_configure`. Caveats recorded
   (no bare `setdefault`; order-sensitive; repairs the `conftest.py:31-34` coupling). It eliminates
   Races 1/2 and Risks 1/4 by construction.
5. **Option C re-spec'd to a clean-DB-0-only `dbsize` tripwire** (SKIP on non-idle DB 0; generic
   message; no pattern-scanning). Verified BM25 keys use the `$BM25:` prefix (`bm25_field.py:18-20`),
   confirming no single popoto prefix exists.
6. **Destructive `redis-cli -n 0 flushdb && pytest` verification row removed**, replaced with an
   isolated-DB subprocess test in both the Verification table and Success Criteria.
7. **Latent-failure risk surfaced** (Risk 1, Test Impact): flipping 75 files onto a flushed DB 15
   may expose previously-masked failures; build budgets triage time. "Harmless once isolation works"
   corrected to per-file evaluation.

Non-test code importing `src.popoto` (What-holds-up note): re-verified that **no `src/`-tree
production module imports `src.popoto`** — every `src.popoto` importer is under `tests/`
(including `tests/conftest.py:31-34` and `tests/benchmarks/`). This slightly weakens the
"breaks downstream users" justification for deferring the layout change, but the deferral still
stands on its higher structural risk; recorded for transparency.

---

## Open Questions

1. **Fix shape (the one decision that needs PM sign-off):** The revised plan **recommends Option D
   (alias-collapse in `pytest_configure`)** — empirically verified to make `src.popoto is popoto`
   with shared `redis_db`/`POPOTO_REDIS_DB`, collapsing Options A+B+most-of-C into a handful of
   lines and eliminating the races/pool-restore risks by construction. Corrected (identity-matched)
   Option A is the documented fallback if D proves order-fragile. Option C (clean-DB-0 tripwire)
   ships alongside either as a CI safety net. Confirm **Option D**, or prefer the Option A fallback?
   *(This is the sole genuine judgment call left for the maintainer.)*

   **Resolved by critique signal (recorded, not open):**
2. **Redundant `_clean_all()` cleanup** — RESOLVED: defer to a separate follow-up PR. The **8**
   files are not all "harmless to remove" (some needed `_clean_all` despite running on DB 15), so
   removal must be per-file-evaluated, not bulk. Keeping it out of this PR keeps the diff focused.
   (No maintainer input needed; recorded in No-Gos.)
3. **DB-0 tripwire strictness** — RESOLVED by Blocker 3: the tripwire is **clean-DB-0-only,
   session-scoped**, SKIPping (not failing) on a non-idle DB 0, with a generic downstream-safe
   message. Per-test scanning is dropped entirely (no popoto-only prefix exists; `$BM25:*` defeats
   it). The durable fix-proof is the regression test, not the tripwire. (No maintainer input needed.)
