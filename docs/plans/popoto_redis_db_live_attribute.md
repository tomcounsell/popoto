---
status: Planning
type: bug
appetite: Small
owner: Dev (sdlc-651)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/651
last_comment_id:
---

# `popoto.POPOTO_REDIS_DB` tracks reconfiguration instead of freezing at import

## Problem

`src/popoto/__init__.py:115` does `from .redis_db import POPOTO_REDIS_DB`, which copies the *name* into the package namespace at import time. `set_REDIS_DB_settings()` rebinds `redis_db`'s own module global, and Python does not propagate a rebind to an already-imported copy.

**Current behavior:** after any reconfiguration, `popoto.POPOTO_REDIS_DB` is a client bound to the *previous* database. A caller who reconfigures and then uses the attribute writes to one database and reads from another, silently. Reproduced in this lane (Python 3.12.14, redis-py 8.1.0, `REDIS_URL=redis://localhost:6379/7` set before import, no writes):

```
--- before reconfiguration ---
popoto.POPOTO_REDIS_DB      db = 7
redis_db.POPOTO_REDIS_DB    db = 7
redis_db.get_REDIS_DB()     db = 7
--- after set_REDIS_DB_settings(db=8) ---
popoto.POPOTO_REDIS_DB      db = 7      <-- stale
redis_db.POPOTO_REDIS_DB    db = 8
redis_db.get_REDIS_DB()     db = 8
```

**Desired outcome:** `popoto.POPOTO_REDIS_DB` resolves to the current connection on every access. The name keeps working for the code that already uses it; it stops being a name that looks current and is not.

## Freshness Check

**Baseline commit:** `1c302a7a` (`main`, `2d58c73a` at the time of the first probe; rebased onto `1c302a7a` before planning)
**Issue filed at:** 2026-09-06T11:26:27Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/__init__.py:114` — claimed `from .redis_db import POPOTO_REDIS_DB` — **drifted to line 115**, claim holds exactly (`from .redis_db import POPOTO_REDIS_DB, get_async_redis_db`).
- `src/popoto/redis_db.py:413,425,475,480` — claimed rebind sites — all four still hold, each is `POPOTO_REDIS_DB = GuardedRedis(...)`.

**Cited sibling issues/PRs re-checked:**
- #645 — closed; its fix ships in **PR #652, still OPEN** on `session/sdlc-645`. Not merged, so `get_redis()` in this lane's base still returns the stale package global.
- #652 — OPEN. Touches `src/popoto/__init__.py`, `docs/configuration.md`, `docs/features/confidence-field.md`, `CLAUDE.md`, `tests/test_docs_redis_url.py`, `tests/test_get_redis_rebind.py`. **Overlaps this plan on `src/popoto/__init__.py`** — see Risk 1.

**Commits on main since issue was filed (touching referenced files):** none. `git log --since=2026-09-06T11:26:27Z -- src/popoto/__init__.py src/popoto/redis_db.py` is empty.

**Bug still reproduces:** yes, output above.

**Active plans in `docs/plans/` overlapping this area:** `docs_dbless_redis_url.md` (the #645/#652 lane). Coordination, not a blocker — its code change and this one converge on the same line of `get_redis()`.

## Prior Art

- **#645 / PR #652** — *Docs: stop teaching a db-less Redis client; make get_redis() rebind-safe*. Fixed the identical defect one level up: `popoto.get_redis()` now delegates to `redis_db.get_REDIS_DB()` on every call instead of returning the frozen package global. Added `tests/test_get_redis_rebind.py`. That lane deliberately left the `POPOTO_REDIS_DB` re-export alone because changing a public name is an API decision outside a docs lane's appetite — which is exactly this issue. **This plan matches that PR's test shape and its delegation idiom.**
- **#655** (filed during this plan's recon) — the same defect in 33 `src/popoto/` modules that import `POPOTO_REDIS_DB` at module scope. Confirmed by reproduction. Out of scope here; see No-Gos.
- **#527 / PR #527** — *swap to the test DB before collection, not on first test*. Context for why `pytest_plugin._swap_db()` mutates the pool on the existing client instead of rebinding the name. That workaround is the reason the defect has never broken the test suite; it is not evidence the defect is absent, and per the issue it must not be "fixed".
- **#577** — the `REDIS_URL`-before-import discipline this defect undermines. No code overlap.

No prior attempt to change the `POPOTO_REDIS_DB` re-export exists, so there is no **Why Previous Fixes Failed** section.

## Research

**Queries used:**
- `mypy module-level __getattr__ PEP 562 suppresses attr-defined errors module attribute`

**Key findings:**
- [PEP 562](https://peps.python.org/pep-0562/) — a module `__getattr__` fires only when normal lookup on the *module object* fails. Critically, **"looking up a name as a module global bypasses module `__getattr__`"**: unqualified global reads *inside the defining module* do not go through the hook. This directly determines the shape of the fix — see Technical Approach, point 2.
- [mypy PR #3647](https://github.com/python/mypy/pull/3647) and [Adam Johnson on gradual typing](https://adamj.eu/tech/2022/08/23/python-type-hints-gradually-add-types-for-third-party-packages/) — mypy treats a module `__getattr__` returning `Any` as "every attribute of this module exists," suppressing `attr-defined` for *all* unresolved attributes of that module. Historically mypy restricted the hook to stub files; whether current mypy honors it in a `.py` file was the one open question, resolved by spike-1.

## Spike Results

### spike-1: mypy 2.3.1 and runtime behavior of a module `__getattr__` in a `.py` file
- **Assumption**: "A PEP 562 `__getattr__` in `src/popoto/__init__.py` preserves `popoto.POPOTO_REDIS_DB`, `from popoto import POPOTO_REDIS_DB`, and `dir(popoto)`, and does not move the mypy count."
- **Method**: prototype (throwaway package + `mypy` + runtime probe, scratchpad, not committed)
- **Finding**: Runtime — `pkg.LIVE` **works**, `from pkg import LIVE` **works**, unknown names still raise `AttributeError`. `'LIVE' in dir(pkg)` is **False**: PEP 562 names vanish from `dir()` unless a `__dir__` is supplied. Today `'POPOTO_REDIS_DB' in dir(popoto)` is `True`, so a `__dir__` is required to avoid an introspection regression. mypy 2.3.1 — **does** honor the hook in a `.py` file, and consequently `reveal_type(pkg.ANY_TYPO)` is `Any` with no error: typo detection on `popoto.<attr>` is lost for importing modules.
- **Confidence**: high (measured, not inferred)
- **Impact on plan**: adds the mandatory `__dir__` (Technical Approach point 3) and the mypy-suppression trade-off (Risk 2). Blast radius measured: only four real `src/` files import the `popoto` package at all (`_error_reporting.py`, `__init__.py` ×2, `batch.py`, `pytest_plugin.py`), all function-scope, and **no `src/` file reads `popoto.POPOTO_REDIS_DB`**. The ratchet delta is a Verification row.

### spike-2: who actually uses the package-level name
- **Assumption**: "The re-export is load-bearing enough that removing it (option 1) is breaking."
- **Method**: code-read (`grep` over `src/`, `tests/`, `docs/`, `examples/`, `scripts/`)
- **Finding**: **70 occurrences of `popoto.POPOTO_REDIS_DB` across 3 test files** — `tests/test_atomic_increment.py`, `tests/test_cyclic_decay_field.py`, `tests/test_decaying_sorted_field.py`. Zero in `src/`, zero in `docs/`, zero in `examples/`, zero in `scripts/`. The name is **not in `popoto.__all__`**, so `from popoto import *` never exported it and star-imports are unaffected. **No site anywhere assigns to `popoto.POPOTO_REDIS_DB`.** Every `docs/` mention of the symbol is the submodule spelling `popoto.redis_db.POPOTO_REDIS_DB`, not the package attribute.
- **Confidence**: high
- **Impact on plan**: settles the option decision — see Technical Approach.

## Data Flow

1. **Entry point**: `import popoto` → `__init__.py:115` executes `from .redis_db import POPOTO_REDIS_DB`, binding the package global to whatever client object `redis_db` built at its own import.
2. **`redis_db` module import**: builds `POPOTO_REDIS_DB = GuardedRedis(connection_pool=pool)` from `REDIS_URL` (or the `127.0.0.1:6379/0` fallback).
3. **Reconfiguration**: `set_REDIS_DB_settings(...)` executes `global POPOTO_REDIS_DB; POPOTO_REDIS_DB = GuardedRedis(...)`. This rebinds **only** `redis_db.__dict__["POPOTO_REDIS_DB"]`.
4. **Divergence**: `popoto.__dict__["POPOTO_REDIS_DB"]` still points at the step-2 object. `redis_db.get_REDIS_DB()` reads the module global at call time and returns the step-3 object.
5. **Output**: a caller reading `popoto.POPOTO_REDIS_DB` issues commands on the step-2 client — the previous database.

After the fix, step 1 binds no package global at all; step 5 resolves through `redis_db.get_REDIS_DB()` at access time and lands on the step-3 client.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: `popoto.POPOTO_REDIS_DB` changes from a plain module attribute to a PEP 562 dynamic attribute. Same spelling, same type, same object identity as `redis_db.POPOTO_REDIS_DB` — the *semantics* change from snapshot to live. `popoto.get_redis()` gains the same delegation PR #652 gives it (forced, not optional — see Technical Approach point 2).
- **Coupling**: unchanged; `__init__` already depends on `redis_db`.
- **Data ownership**: consolidates it. `redis_db` becomes the single owner of the current-connection binding at the package boundary; the package namespace stops holding a second, divergent copy.
- **Reversibility**: trivial — restoring one import line and one `return` reverts it.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1 (report the option choice with evidence before building — done in this plan)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Worktree venv resolves to THIS checkout | `.venv/bin/python -c "import popoto, pathlib, sys; sys.exit(0 if pathlib.Path(popoto.__file__).resolve() == pathlib.Path('src/popoto/__init__.py').resolve() else 1)"` | A venv resolving elsewhere silently tests another tree (#495) |
| Full extras installed | `.venv/bin/python -c "import numpy, sentence_transformers, mcp"` | `.[dev]` alone deselects ~95 tests |
| Redis reachable, non-zero DB | `REDIS_URL=redis://localhost:6379/7 .venv/bin/python -c "import popoto; popoto.POPOTO_REDIS_DB.ping()"` | Never DB 0 (#577) |

## Solution

### Key Elements

- **PEP 562 `__getattr__` on the package**: resolves `POPOTO_REDIS_DB` through `redis_db.get_REDIS_DB()` at access time, so the attribute can never be stale.
- **PEP 562 `__dir__` on the package**: keeps the name in `dir(popoto)`, which the plain re-export provided for free and the hook does not.
- **`get_redis()` delegation**: forced by the removal of the package global (a bare global read inside `__init__.py` bypasses `__getattr__` and would raise `NameError`). Converges with PR #652.
- **Regression test**: `tests/test_popoto_redis_db_rebind.py`, mirroring `tests/test_get_redis_rebind.py` including its restore-by-object-reassignment discipline.

### Flow

`import popoto` → `popoto.POPOTO_REDIS_DB` (db=7) → `set_REDIS_DB_settings(db=8)` → `popoto.POPOTO_REDIS_DB` (**db=8**) → reads and writes land on the same database.

### Technical Approach

**Option decision: option 2 (PEP 562 `__getattr__`), on the following evidence.**

- **Option 1, drop the re-export — rejected.** spike-2 found 70 live call sites across 3 test files. Since popoto is a published library and the name has been importable for the life of the package, removal is breaking for downstream users too, and a silent removal is not available to us; it would need a deprecation cycle whose cost exceeds this Small appetite. Nothing about the name is wrong — only its staleness is.
- **Option 3, document it as a snapshot — rejected.** The issue's Desired Outcome forbids it explicitly ("not a name that looks current and is not"), and it leaves a silent write-here-read-there footgun in the public namespace. Documentation does not fix a data-corruption path.
- **Option 2, PEP 562 — chosen.** It preserves the spelling, every existing call site, and the object's type, while making the value correct. spike-2 clears the compatibility checklist the issue asks for: not in `__all__` so **star-imports are unaffected**; **no assignment sites** anywhere, so no caller depends on it being a settable plain attribute; `from popoto import POPOTO_REDIS_DB` at module scope **still works** (the import machinery falls back to `getattr`, spike-1); `hasattr` still `True`; the value is a `redis.Redis` client, which is not pickled anywhere and would not be picklable regardless. `dir()` is the one surface that regresses, and point 3 restores it.
- **Deprecation story: none required.** This is not a removal. The name, its type, and every call site survive; only staleness is removed. The CHANGELOG records the semantic change under a *Fixed* heading.

Implementation, three parts:

1. **`src/popoto/__init__.py:115`** — drop `POPOTO_REDIS_DB` from the import, keeping `from .redis_db import get_async_redis_db`. This is what makes normal attribute lookup fail so the hook can run; leaving the import in place would leave a real attribute that shadows `__getattr__` forever.
2. **`src/popoto/__init__.py:137`** — `get_redis()` currently does `return POPOTO_REDIS_DB`, an unqualified global read. PEP 562 explicitly does **not** route module-global reads through `__getattr__`, so after step 1 this raises `NameError`. Change it to `return redis_db.get_REDIS_DB()` — the identical delegation PR #652 introduces, so the two lanes converge rather than conflict semantically.
3. **Add `__getattr__` and `__dir__` at module scope**, after `__all__`:
   - `__getattr__(name)` returns `redis_db.get_REDIS_DB()` for `"POPOTO_REDIS_DB"` and otherwise raises `AttributeError(f"module {__name__!r} has no attribute {name!r}")` — matching the message CPython produces, so nothing that pattern-matches on it regresses.
   - `__dir__()` returns `sorted({*globals(), "POPOTO_REDIS_DB"})`.
   - Both carry a docstring naming #651 and the PEP 562 global-lookup rule from point 2, so a future reader does not "simplify" the delegation in `get_redis()` back into a bare global.

## Failure Path Test Strategy

### Exception Handling Coverage
- No `except Exception: pass` blocks are added or touched. The only exception path introduced is the deliberate `AttributeError` re-raise in `__getattr__`, which is asserted directly (`popoto.NoSuchName` raises `AttributeError`) rather than swallowed.

### Empty/Invalid Input Handling
- `__getattr__`'s only input is an attribute name supplied by the interpreter, always a `str`. The unknown-name path is the invalid-input path and is tested. There is no empty-output loop and no agent output processing in scope.

### Error State Rendering
- No user-visible rendering surface. The failure mode is an `AttributeError` with CPython's standard message, asserted in the test.

## Test Impact

- [ ] `tests/test_popoto_redis_db_rebind.py` — **CREATE**: the regression test for this fix.
- [ ] `tests/test_atomic_increment.py`, `tests/test_cyclic_decay_field.py`, `tests/test_decaying_sorted_field.py` — **NO CHANGE EXPECTED**, but all three must be run: they are the 70 existing consumers of `popoto.POPOTO_REDIS_DB` and are the direct evidence that the hook is a drop-in. If any of them needs an edit, the "drop-in" premise is false and that is a build-stopping finding, not a test to update.
- [ ] `tests/test_pytest_plugin.py` — **NO CHANGE**: `_swap_db()` mutates the pool rather than rebinding, so it is unaffected (stated in the issue). Run it because it is the suite's most connection-sensitive file and `TestAuthPreservation` is documented in `test_get_redis_rebind.py` as sensitive to a careless restore.
- [ ] `tests/test_get_redis_rebind.py` — **not in this lane's base** (ships with PR #652). Do not duplicate it; do not add a second `get_redis()` rebind test.

No existing test expectation is modified by this plan. The PM authorized changing expectations if needed; the evidence says none is needed, and any that turns out to be needed will be justified individually in the PR body.

## Rabbit Holes

- **Fixing the 33 internal module-scope imports.** The same defect, ~9× the blast radius, and it needs a design decision (mutate-the-pool vs proxy vs call-site conversion) that this Small appetite cannot hold. Filed as #655.
- **Turning `redis_db.POPOTO_REDIS_DB` into a forwarding proxy.** Tempting because it would fix all 34 sites at once, but it breaks `isinstance(POPOTO_REDIS_DB, GuardedRedis)` checks (one lives in `docs/plans/adhoc_db0_guard.md`'s verification table) and drags in the whole proxy-semantics surface. Belongs to #655's design discussion, not here.
- **Rewriting `redis_db.py:67-70`'s "imports POPOTO_REDIS_DB directly for performance" docstring.** It is now in tension with correctness, but rewriting it is #655's call — this lane would be guessing at the resolution.
- **Adding `POPOTO_REDIS_DB` to `__all__`.** It has never been there; adding it would newly export it from `from popoto import *`, widening the public surface while fixing a bug. Out of scope.
- **Micro-optimizing the hook.** One dict lookup plus a function call per access, on a path that then makes a network round-trip. Not worth a benchmark.

## Risks

### Risk 1: Conflict with the open PR #652 on `src/popoto/__init__.py`
**Impact:** Both PRs edit `get_redis()`'s return statement and the region around line 115. Whichever merges second hits a textual conflict.
**Mitigation:** The two changes are semantically *identical* on that line (both become `return redis_db.get_REDIS_DB()`), so the conflict is textual and trivially resolvable. Rebase onto `origin/main` immediately before merge and re-run the narrow test set. Do not import or duplicate `tests/test_get_redis_rebind.py`. If #652 lands first, step 2 of the Technical Approach becomes a no-op and the plan is otherwise unchanged.

### Risk 2: The module `__getattr__` suppresses mypy `attr-defined` for every `popoto.<attr>` access
**Impact:** mypy stops flagging typos like `popoto.POPOTO_REDIS_DBB` in modules that `import popoto`. Measured behavior, not a guess (spike-1).
**Mitigation:** Blast radius measured and small — four `src/` files import the package, all at function scope, and none reads a package-level Redis attribute; the 3 consumer test files are not in the mypy gate. The Verification table asserts the ratchet does not *drop*, which is how a suppression would show up (`scripts/mypy_ratchet.py` warns on a below-baseline count). State the resulting count with its environment. Accepted as the cost of option 2; option 1 and option 3 were rejected on stronger grounds.

### Risk 3: A test or downstream caller assigns to `popoto.POPOTO_REDIS_DB`
**Impact:** An assignment would create a real module global that permanently shadows `__getattr__`, silently restoring the stale-snapshot bug for the rest of the process.
**Mitigation:** spike-2 grepped `src/`, `tests/`, `docs/`, `examples/`, `scripts/` and found **zero** assignment sites. The Verification table carries this as an anti-criterion so a future PR reintroducing one is caught. Note `tests/test_get_redis_rebind.py` restores via `redis_db.POPOTO_REDIS_DB = original_client` — the *submodule* attribute, which is correct and unaffected; the new test uses the same spelling.

### Risk 4: Running an ad-hoc repro against DB 0
**Impact:** DB 0 is a live agent store on this machine; two prior incidents (#577).
**Mitigation:** Every repro in this lane sets `REDIS_URL=redis://localhost:6379/7` **before** `import popoto`, from `scripts/scratch_repro.py`'s template. No repro calls `set_REDIS_DB_settings(db=0)`. The new test asserts `original != 0` before doing anything, exactly as `test_get_redis_rebind.py` does, and picks its target as `7 if original != 7 else 8` — never 0.

## Race Conditions

**No race conditions identified.** `__getattr__` performs a single read of `redis_db`'s module global via `get_REDIS_DB()`; module-global reads are atomic under the GIL and the hook holds no state of its own. The change removes shared mutable state (the duplicate package-level binding) rather than adding any. A concurrent `set_REDIS_DB_settings()` during an access yields either the old or the new client — the same interleaving that already exists for `get_REDIS_DB()`, and strictly better than today's guaranteed-stale read. `docs/configuration.md` already warns that reconfiguration may fail in-flight operations.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #655] The 33 `src/popoto/` modules that hold their own stale `POPOTO_REDIS_DB` snapshots. Filed with reproduction evidence during this plan's recon; needs an approach decision this appetite cannot hold.
- [SEPARATE-SLUG #645] `popoto.get_redis()`'s rebind-safety as a *deliverable* — owned by PR #652. This plan changes the same line only because removing the package global forces it, and lands the identical delegation.
- [ORDERED] Rebasing onto PR #652 once it merges. Blocked on a human-gated merge in another lane; handled at merge time per Risk 1.

## Update System

No update system changes required — this is a source-internal change to an already-installed package with no new dependencies, config files, or migration steps.

## Agent Integration

No agent integration required — this is a library-internal change. No MCP surface, tool wrapper, or entry point needs to expose it.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` page covers connection binding, and this change does not warrant creating one.

### External Documentation Site
- [ ] `docs/configuration.md` — under **Reconfiguring at Runtime**, add a note that a module-scope `from popoto.redis_db import POPOTO_REDIS_DB` freezes at import and goes stale after `set_REDIS_DB_settings()`, and name the two spellings that stay live: `popoto.POPOTO_REDIS_DB` and `get_REDIS_DB()`. Link #655 for the internal case. This is the only user-facing doc that teaches reconfiguration.
- [ ] `docs/field-authoring.md` — teaches `from popoto.redis_db import POPOTO_REDIS_DB` at lines 84/159/169 for field authors. Confirm whether a staleness caveat belongs here; if the guidance changes it must change with the decision. **Resolve during DOCS, do not pre-commit to an edit.**
- [ ] `CHANGELOG.md` — a *Fixed* entry naming #651, the before/after `db=` output, and the PEP 562 mechanism.
- [ ] `mkdocs build` must pass.

### Inline Documentation
- [ ] `__getattr__` docstring: why the hook exists (#651) and why `get_redis()` must not go back to a bare global (PEP 562 bypasses module globals).
- [ ] `__dir__` docstring: that it exists solely to keep `POPOTO_REDIS_DB` in `dir(popoto)`, which the hook otherwise drops.

## Success Criteria

- [ ] `popoto.POPOTO_REDIS_DB` returns a client bound to the new database after `set_REDIS_DB_settings()`, and is `is`-identical to `redis_db.get_REDIS_DB()`.
- [ ] `from popoto import POPOTO_REDIS_DB`, `hasattr(popoto, "POPOTO_REDIS_DB")`, and `"POPOTO_REDIS_DB" in dir(popoto)` all behave exactly as they do on `main`.
- [ ] `popoto.<unknown name>` raises `AttributeError` with CPython's standard message.
- [ ] The 3 existing consumer test files pass **unmodified**.
- [ ] `tests/test_pytest_plugin.py` passes (connection-sensitive; `_swap_db()` untouched).
- [ ] mypy ratchet does not exceed baseline; the count is reported with its environment.
- [ ] `ruff check src/` and `black --check src/ tests/` clean.
- [ ] Both the Redis and the Valkey CI jobs pass.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] No xfail conversions needed — `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/` returns nothing repo-wide.

## Team Orchestration

Small appetite, one file of source plus one test file. Executed directly by the lane dev with a single reviewer pass; no builder fan-out — parallel builders on one file would only interleave commits.

### Team Members

- **Reviewer (pr)**
  - Name: `sdlc-651-reviewer`
  - Role: Review the PR against this plan, with particular attention to the compatibility checklist in Technical Approach and to Risk 2's mypy delta.
  - Agent Type: code-reviewer
  - Resume: true

## Step by Step Tasks

### 1. Make the attribute live
- **Task ID**: build-live-attribute
- **Depends On**: none
- **Validates**: tests/test_popoto_redis_db_rebind.py (create), tests/test_atomic_increment.py, tests/test_cyclic_decay_field.py, tests/test_decaying_sorted_field.py, tests/test_pytest_plugin.py
- **Informed By**: spike-1 (mypy honors the hook in `.py`; `dir()` needs `__dir__`; unknown names still raise), spike-2 (70 call sites in 3 test files, not in `__all__`, no assignment sites)
- **Assigned To**: lane dev
- **Agent Type**: builder
- **Parallel**: false
- Drop `POPOTO_REDIS_DB` from the `from .redis_db import ...` line in `src/popoto/__init__.py`.
- Change `get_redis()` to `return redis_db.get_REDIS_DB()` (forced by PEP 562's module-global rule).
- Add `__getattr__` and `__dir__` with the docstrings named in the Documentation section.
- Write `tests/test_popoto_redis_db_rebind.py` mirroring `tests/test_get_redis_rebind.py`: assert `original != 0` first, carry host/port/auth via `redis_db.sibling_client_kwargs`, restore by re-assigning the **original client object** to `redis_db.POPOTO_REDIS_DB` (not by a second `set_REDIS_DB_settings()` call, which would drop host/port and break `TestAuthPreservation`). Cover: rebind is tracked; identity with `get_REDIS_DB()`; `from popoto import POPOTO_REDIS_DB` still resolves; `dir()` and `hasattr` unchanged; unknown attribute raises `AttributeError`.

### 2. Verify
- **Task ID**: validate-live-attribute
- **Depends On**: build-live-attribute
- **Assigned To**: lane dev
- **Agent Type**: validator
- **Parallel**: false
- Run the narrow test set (Verification table). Full-suite runs collide on Redis state across worktrees — do not run one.
- Run `scripts/mypy_ratchet.py` and record the count with redis-py/mypy/Python versions.
- Run `ruff check src/` and `black --check src/ tests/`.

### 3. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-live-attribute
- **Assigned To**: lane dev
- **Agent Type**: documentarian
- **Parallel**: false
- Apply the Documentation section's items. Land these **before** `verdict finalize`, since DOCS commits move the head the review verdict pins (#642).

### 4. Review
- **Task ID**: review-pr
- **Depends On**: document-feature
- **Assigned To**: `sdlc-651-reviewer`
- **Agent Type**: code-reviewer
- **Parallel**: false
- Review the full delta including docs; re-review after any patch.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Rebind regression test | `.venv/bin/python -m pytest tests/test_popoto_redis_db_rebind.py -q` | exit code 0 |
| Existing consumers unmodified | `git diff --name-only origin/main... -- tests/test_atomic_increment.py tests/test_cyclic_decay_field.py tests/test_decaying_sorted_field.py` | output does not contain `tests/` |
| Existing consumers pass | `.venv/bin/python -m pytest tests/test_atomic_increment.py tests/test_cyclic_decay_field.py tests/test_decaying_sorted_field.py -q` | exit code 0 |
| Connection-sensitive suite passes | `.venv/bin/python -m pytest tests/test_pytest_plugin.py -q` | exit code 0 |
| No package global remains | `grep -c '^from \.redis_db import POPOTO_REDIS_DB' src/popoto/__init__.py` | match count == 0 |
| No assignment to the package attribute (anti-criterion, Risk 3) | `grep -rn 'popoto\.POPOTO_REDIS_DB[[:space:]]*=' src/ tests/ docs/ examples/ scripts/` | exit code 1 |
| Internal snapshots untouched (anti-criterion, No-Go #655) | `git diff --name-only origin/main... -- src/popoto/fields src/popoto/models src/popoto/recipes src/popoto/pubsub` | output does not contain `src/popoto` |
| `_swap_db` untouched (issue says do not "fix" it) | `git diff --name-only origin/main... -- src/popoto/pytest_plugin.py` | output does not contain `pytest_plugin` |
| mypy ratchet | `scripts/mypy_ratchet.py` | exit code 0 |
| Lint clean | `.venv/bin/python -m ruff check src/` | exit code 0 |
| Format clean | `.venv/bin/python -m black --check src/ tests/` | exit code 0 |
| No stale xfails | `grep -rn 'pytest.mark.xfail\|pytest.xfail(' tests/` | exit code 1 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

None. The one API decision the PM flagged — options 1/2/3 — is settled in Technical Approach on spike-2's usage evidence, and reported to the PM before build.
