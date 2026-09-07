---
status: Ready
type: chore
appetite: Small
owner: Dev (sdlc-611)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/611
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-07T10:34:00Z
---

# examples/: unlock the demo project so Dependabot can cover it

## Problem

`.github/dependabot.yml` covers the repository root only. `examples/` is excluded
because adding it is not a config change — `examples/pyproject.toml` declares
`requires-python = ">=3.8"` while depending on popoto (`>=3.10`) as an editable
path dependency, so `examples/uv.lock` cannot be regenerated at all.

**Current behavior:** `examples/uv.lock` was last touched `876d8ea` (2026-02-11).
It pins popoto `1.0.0b1`, redis 6/7, and three marker-split textual pins. Two
Dependabot alerts live in it and cannot be cleared: msgpack (high, first patched
1.2.1) and Pygments (low, 2.20.0). Nothing in CI exercises `examples/`, so the
demo can rot silently — and has.

**Desired outcome:** `examples/` relocks cleanly, the demo works against current
pins, Dependabot covers the directory, and `examples/` gains its first automated
signal so it cannot rot unobserved again.

## Freshness Check

**Baseline commit:** `d101820d`
**Issue filed at:** 2026-09-04T08:42:45Z
**Disposition:** Minor drift — the issue's *blocking* claims all hold; its
*severity* claims about source damage are substantially overstated (see below).

**Claims re-verified empirically** (not by reading — by running the code):

- `examples/pyproject.toml` `requires-python = ">=3.8"` — **holds**. Confirmed
  the relock is blocked solely by this; raising it to `>=3.10` makes
  `uv lock --upgrade` succeed on the first try.
- `examples/uv.lock` pins popoto `1.0.0b1` and is `revision = 1` while the root
  lock is `revision = 3` — **holds**, and is the concrete proof that relocking
  with the local uv 0.6.10 would regress the lock format.
- Both alerts clear on relock — **holds**: msgpack `1.1.1/1.1.2` → **1.2.2**
  (≥1.2.1), pygments `2.19.2` → **2.21.0** (≥2.20.0).
- "eighteen months of popoto API drift … breaks the demo" — **overstated**. All
  five screens mount, render, and populate correctly with **zero** source
  changes. Real repair is ~25 lines. See Spike Results.

**Cited sibling issues/PRs re-checked:**

- #551 — CLOSED. Landed as #614 (`ec316336`, 2026-09-04), which added
  `dependabot.yml` with the `examples/` deferral paragraph this plan removes.
- #166 — CLOSED 2026-03-11. Directly relevant: it *added* the rename and
  move-category buttons specifically to exercise KeyField mutation. Those are
  precisely the two features now broken by popoto's `migrate_key=True`
  requirement. The demo's KeyField showcase became its own canary.

**Commits on main since issue was filed (touching referenced files):**

- `ec316336` chore(#551): add `.github/dependabot.yml` — expected; this plan
  edits the file it created. Not a conflict.

**Root `uv.lock` half of the issue:** no Dependabot PR has appeared (zero open,
none in history since #614 merged 2026-09-04). The schedule is Monday 06:00 UTC
and today is Monday 2026-09-07, so the first scheduled run is due now. See
No-Gos.

**Active plans overlapping this area:** none. No plan in `docs/plans/` touches
`examples/` or `dependabot.yml`.

## Prior Art

- **#551 / PR #614**: Added `.github/dependabot.yml` for scheduled version
  updates, deliberately root-only. Succeeded. Its header comment names `examples/`
  as deferred and points at #611 — this plan removes that paragraph.
- **#166**: "Update Popoto Kitchen TUI demo to exercise index mutation edge
  cases" (closed 2026-03-11). Added the rename / move-category / toggle buttons
  so the demo would exercise KeyField and indexed-field mutation after PRs
  #159–#163 fixed index-corruption bugs. Succeeded at the time. **Its features
  are what broke**: popoto later made KeyField mutation opt-in via
  `save(migrate_key=True)`, and with no CI on `examples/` nothing noticed.

## Research

Empirical over documentary: rather than search for Textual migration guides, the
drift was measured directly by relocking and running the app headlessly against
the new pins. That produces exact findings for *this* codebase instead of general
advice. See Spike Results — every spike was executed, not estimated.

Internal API references confirmed by reading source:

- `save(migrate_key=True)` — `src/popoto/models/base.py:1283` (signature),
  raise path at `1380-1391`, docstring example at `1370`. Documented at
  `docs/indexed_fields.md:285-295` and `docs/recipes.md:305-327`. This is the
  sanctioned path, not a workaround.
- SortedField partition requirement — `src/popoto/fields/sorted_field_mixin.py:830`.

## Spike Results

### spike-1: Can uv 0.12.2 be obtained without touching the machine's global uv?
- **Assumption**: "A worktree-scoped standalone install is possible."
- **Method**: prototype
- **Finding**: Yes. `UV_INSTALL_DIR` + `UV_NO_MODIFY_PATH=1` installs a pinned
  standalone build. Reports `uv 0.12.2 (46ead6098 2026-08-05 aarch64-apple-darwin)`,
  matching `.github/workflows/lock-check.yml:34` exactly. Global uv (0.6.10)
  untouched. **The binary must live outside the worktree** — `.uvbin/` is not
  gitignored and a 40MB binary is a staging accident waiting to happen.
- **Confidence**: high
- **Impact on plan**: The hard prerequisite is solved. Lock is produced by the
  CI-pinned version, so `lock-check` cannot fail for format-skew reasons.

### spike-2: Does the relock actually succeed, and does it clear both alerts?
- **Assumption**: "`requires-python` is the only thing blocking the relock."
- **Method**: prototype
- **Finding**: Yes. After `>=3.8` → `>=3.10`, `uv lock --upgrade` resolved 14
  packages in 370ms. msgpack → **1.2.2**, pygments → **2.21.0** (both alerts
  clear), popoto `1.0.0b1` → `1.9.0`, textual `0.73/6.2.1/7.5.0` → `8.2.8`,
  redis `6/7` → `8.1.0`. The three marker-split pins collapsed to single pins.
- **Confidence**: high
- **Impact on plan**: Pieces 1 and 2 of the issue are ~2 lines plus a generated
  file. All the risk is in piece 3.

### spike-3: How badly is `popoto_kitchen/` actually broken?
- **Assumption**: "A new Textual major plus 18 months of popoto drift breaks the demo."
- **Method**: prototype (headless `App.run_test()` against seeded Redis DB 13)
- **Finding**: **Far less than predicted.** All five screens import, mount, and
  render with zero source changes, and every DataTable populates with counts
  exactly matching seeded data (restaurants=5, menu=50, orders=12, drivers=4,
  recent-orders=10). The popoto import surface the demo uses is small and has
  been stable across the entire 1.0.0b1 → 1.9.0 range.
- **Confidence**: high
- **Impact on plan**: Appetite drops to Small. No split needed.

### spike-4: What is the real drift, precisely?
- **Assumption**: "Drift is diffuse and requires a rewrite."
- **Method**: prototype (headless interaction sweep over every non-destructive button)
- **Finding**: Five bounded defects in two families.

  *Textual (mechanical, 20 sites):*
  1. `Select.BLANK` is now the bare bool `False`; the sentinel is `Select.NULL`.
     Assigning it raises `InvalidSelectValueError`. 12 sites, 4 files.
  2. `DataTable._row_locations` is **private** and `TwoWayDict` lost `.keys()`.
     8 sites, 4 files.

  *popoto (semantic, 3 features):*
  3. `btn-rename` (restaurants): `Cannot change KeyField 'name' … without migrate_key=True`
  4. `btn-move-category` (menu): same, on KeyField `category`
  5. Menu price filter: `price field is sorted on category. Query filter must
     also specify a value for category`

  Everything else works, including geo-search on both screens (an apparent geo
  failure was a probe artifact — `btn-clear-filter` wiping the inputs before
  `btn-geo-search` ran; in isolation both return 10 rows cleanly).
- **Confidence**: high
- **Impact on plan**: Defines Step by Step Tasks exactly. Note 1 and 2 are
  **behavior fixes, not renames** — see Technical Approach.

### spike-5: Is there a public API for asserting on notifications?
- **Assumption**: "The smoke test must read the private `app._notifications`."
- **Method**: code-read against textual 8.2.8
- **Finding**: `App.notifications` does **not** exist; only `_notifications`.
  **But `App.notify()` itself is public** with a stable keyword-only signature
  (`message, *, title, severity, timeout, markup`). Overriding it in a test
  subclass captures every notification with **zero private access**.
- **Confidence**: high
- **Impact on plan**: The smoke test uses no private Textual API — avoiding a
  repeat of the exact defect being fixed in task 2. No version-pinned comment or
  future breakage hazard.

## Data Flow

The smoke test's assertion path, which is the novel part of this work:

1. **Entry point**: pytest fixture seeds Redis (bounded counts) via
   `popoto_kitchen.seed`. **`REDIS_URL` must already be bound in the process
   environment** — see the binding contract below; the fixture only *asserts*
   the binding, it cannot establish it.
2. **App construction**: a `PopotoKitchen` subclass overrides the public
   `notify()` to append `(severity, message)` to a list, then delegates to `super()`.
3. **Pilot**: `async with app.run_test()` mounts the app; the test sets
   `TabbedContent.active` across all five tabs, pausing between each.
4. **Screen `on_mount` / `refresh_data`**: each screen queries popoto and fills
   its DataTable. Any exception is caught by the screen's broad
   `except Exception: self.app.notify(..., severity="error")`.
5. **Output**: the test asserts (a) zero captured `severity="error"`
   notifications, and (b) each DataTable's `row_count` equals the seeded count.

Step 4 is why (a) matters: without it, a screen whose every query fails still
mounts and renders, and a naive "does it mount" test passes green.

**Redis binding contract (critique finding — this is a safety requirement, not a
style note).** `REDIS_URL` **cannot** be bound from a fixture or `conftest.py`.
popoto ships a `pytest11` entry point, and because popoto is an installed
dependency of `examples/`, that plugin loads. Its `pytest_configure`
(`src/popoto/pytest_plugin.py:81-89`) calls `_collapse_src_popoto()`, which does
`import popoto` and `importlib.import_module("popoto.redis_db")` — **before
collection, and therefore before any fixture body runs**. `popoto_kitchen.seed`
also imports popoto at module scope. By the time a fixture executes, the
connection is already bound, and `DEFAULT_URL` is `redis://localhost:6379/0` —
the LIVE agent store.

Therefore:

- The CI step and every documented local invocation set `REDIS_URL` as a
  **process environment variable ahead of the `pytest` command**, exactly as the
  Verification table does.
- `examples/tests/conftest.py` MUST NOT contain `os.environ["REDIS_URL"] = ...`.
  It is a no-op for safety and would give a false sense of protection.
- The session fixture's only safety job is to **assert** the binding and fail
  loudly otherwise:
  `assert not os.environ.get("REDIS_URL", DEFAULT).rstrip("/").endswith("/0")`.

## Architectural Impact

- **New dependencies**: none for the library. `examples/` gains a `dev` extra
  with `pytest`/`pytest-asyncio` for the smoke test. `src/popoto` is untouched.
- **Interface changes**: none. No public popoto API changes.
- **Coupling**: *decreases*. Task 2 removes the demo's dependence on a private
  Textual attribute; the smoke test deliberately uses only public API.
- **Data ownership**: unchanged.
- **Reversibility**: high. Every change is confined to `examples/` and
  `.github/`. Reverting the commit fully restores prior state.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1-2 (drift scope confirmed; fix approach for the three popoto defects approved)
- Review rounds: 1

Scope was de-risked up front by measuring the drift instead of estimating it.
The remaining work is ~25 lines of source repair plus a new test and a CI job.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| uv 0.12.2 available | `"$UV_012" --version \| grep -q '0\.12\.2'` | Lock must match `lock-check.yml:34` |
| Redis reachable | `redis-cli -n 13 ping` | Smoke test and seeding need a live server |
| Non-zero test DB bound | `[ "${REDIS_URL##*/}" != "0" ]` | DB 0 is a LIVE agent store (#577) |

## Solution

### Key Elements

- **Manifest**: raise `requires-python` to `>=3.10`, and raise the `textual`
  floor to match the API the source now uses.
- **Lockfile**: regenerate with the CI-pinned uv 0.12.2.
- **Source repair**: five bounded fixes — two mechanical Textual migrations, three
  popoto-semantics fixes that the demo should *teach* rather than paper over.
- **Smoke test**: headless `App.run_test()` covering all five screens, asserting
  no error notifications and correct row counts, using only public API.
- **CI**: a separate lightweight job so `examples/` has a real signal.
- **Config**: second `uv` entry for `/examples`, deferral paragraph removed.

### Flow

`dependabot opens a PR against examples/uv.lock` → **examples-smoke CI job** →
`seeds Redis, drives all five screens headlessly` → **pass/fail** →
`a bump that breaks the demo is caught before merge, not eighteen months later`

### Technical Approach

**Two of the five fixes are behavior fixes riding inside a dependency bump, and
must be called out rather than buried:**

- `Select.BLANK` was not merely deprecated. Because it now evaluates to the bare
  bool `False`, the *read* paths (`if select.value != Select.BLANK`) compared
  the live value against `False` and so treated "no selection" as a real filter
  value. **The demo's filters have been silently wrong**, not just crash-prone
  on clear. Fixing to `Select.NULL` corrects filtering behavior.
- `list(table._row_locations.keys())[cursor_row]` indexed *insertion* order.
  `table.ordered_rows[cursor_row].key` indexes *display* order. Since these
  screens have sort buttons, the old code selected the wrong row after any sort.
  The public replacement is also a correctness fix. `.key` is a `RowKey`, so the
  downstream `.value` access is unchanged — a true drop-in.

**The three popoto fixes should showcase, not hide, the semantics** (#166 added
these buttons to demonstrate exactly this class of behavior):

- Rename and move-category call `save(migrate_key=True)`, with a one-line
  comment at each site naming the behavior: KeyField identity is immutable by
  default, and migration is an explicit opt-in that deletes the old hash.
- The menu price filter requires a category. Rather than silently disabling the
  control, the UI states *why* — a demo whose job is teaching should explain a
  refusal, not just perform it.

**The smoke test uses no private Textual API** (spike-5): a `PopotoKitchen`
subclass overrides the public `notify()`. This matters because reaching into
`_notifications` would repeat the precise defect task 2 removes.

## Failure Path Test Strategy

### Exception Handling Coverage
- The screens are dense with `except Exception: self.app.notify(..., severity="error")`
  and several bare `except Exception: pass`. These are the reason a naive smoke
  test is worthless here.
- The smoke test's error-notification assertion gives every one of the
  `notify(severity="error")` handlers observable behavior under test. This is
  already proven: it is what surfaced all three popoto defects.
- The remaining `except Exception: pass` blocks (e.g. `_set_stat`) are covered
  indirectly by asserting rendered stat values against seeded counts — a
  swallowed failure there leaves the stat at `"0"` and fails the assertion.

### Empty/Invalid Input Handling
- Geo-search on empty inputs is *existing, correct* behavior
  (`float("")` → `ValueError` → "Please enter valid coordinates"). The smoke
  test must fill the geo inputs before pressing, or it will assert against its
  own artifact — this was observed during spike-4 and is a documented trap.
- The price filter's new "category required" path is an invalid-input path and
  gets an explicit assertion.

### Error State Rendering
- Covered directly: the test asserts on the user-visible error channel
  (`notify`), which is exactly how these screens surface failures.

## Test Impact

No existing tests affected. `examples/` has never had any test coverage — that
is the core defect this plan fixes. Nothing under `tests/` imports from
`examples/`, and `src/popoto` is untouched, so the main suite is unaffected.

New: `examples/tests/test_kitchen_smoke.py`.

## Rabbit Holes

- **Rewriting the demo for modern Textual idiom.** The screens are `Container`s
  in a `TabbedContent` rather than real `Screen`s, and several use dated
  patterns. It all works. Restyling is not this issue.
- **Chasing the geo "failure".** It is a probe artifact (spike-4). Do not
  "fix" working geo-search code.
- **Making `examples/` ruff-clean.** `examples/` is explicitly out of the ruff
  gate's scope. Do not widen the lint surface here.
- **Fixing the root `uv.lock` by hand.** See No-Gos — a dozens-of-pins diff
  would swamp the config change and duplicate an imminent scheduled PR.
- **Deleting the broad `except Exception` handlers.** Tempting, since they hid
  this rot for months. But they also keep a demo usable when Redis is
  half-populated. The smoke test neutralizes them by making them observable;
  that is the cheaper fix.

## Risks

### Risk 1: The smoke test needs a live Redis, so it is an integration test
**Impact:** A flaky or absent Redis makes `examples/` CI red for reasons
unrelated to the change under review — the same class of false signal the
uv-version pin exists to prevent.
**Mitigation:** Run it as its own job with a `redis` service container (the
tests workflow already does this), pinned to a non-zero DB. Because it is a
separate job, a red result never blocks an unrelated library PR from being
understood — the failure is attributable on sight. Seed counts are small and
fixed, so runtime is a few seconds.

### Risk 2: Textual 8.2.8 is pinned only by a floor, and majors keep breaking
**Impact:** A future major re-breaks the demo.
**Mitigation:** That is precisely what this plan fixes — Dependabot will now
open the PR *and* the smoke job will fail it, instead of the breakage landing
unobserved. The failure mode changes from "rots for 18 months" to "a red check
on a bot PR". The smoke test's avoidance of private API (spike-5) is what keeps
this durable.

### Risk 3: Raising the `textual` floor could strand a downstream user
**Impact:** Someone pinning old textual cannot install the demo.
**Mitigation:** Acceptable and correct. `examples/` is a non-published demo
application, not the library — the lower-bound discipline in
`.github/dependabot.yml` (`versioning-strategy: lockfile-only`) exists for
`popoto` itself, whose floors this plan does not touch. Leaving the floor at
`>=0.47.0` would make the manifest *lie*, since the source now uses
`Select.NULL` and `ordered_rows`.

## Race Conditions

No race conditions identified in the shipped code — the demo is synchronous and
single-threaded, and the plan adds no concurrency.

One test-level timing hazard: `App.run_test()` is async and screens populate
during `on_mount`. Asserting immediately after setting `TabbedContent.active`
reads a half-mounted tree.

### Race 1: Asserting before the pilot has processed mount/refresh
**Location:** `examples/tests/test_kitchen_smoke.py`
**Trigger:** Setting `active` then asserting `row_count` in the same step.
**Data prerequisite:** Redis must be seeded *before* the app is constructed.
**State prerequisite:** The tab's widgets must be mounted and `refresh_data`
must have run.
**Mitigation:** `await pilot.pause()` after every tab switch and every button
press — this settles Textual's message queue. Verified across all five spikes.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #660] Root `uv.lock` refresh. #611 folds it in as "whichever
  comes first — the scheduled PR or a deliberate refresh here". No Dependabot PR
  exists yet, but the schedule is Monday 06:00 UTC and today *is* Monday
  2026-09-07, so the scheduled run is due now. Doing it by hand would collide
  with that PR and discard the per-package changelogs that make a
  dozens-of-pins diff reviewable. Leaving it to Dependabot is the issue's own
  stated preference and keeps this PR's config change readable.

  Filed as **#660** during critique. The original draft tagged this
  `[SEPARATE-SLUG #611]`, which was wrong: #611 is this plan's own tracking
  issue and this PR carries `Closes #611`, so the deferral would have had
  nowhere to land the moment the PR merged.

Modernizing `popoto_kitchen`'s Textual idiom is not listed here — it is not
deferred work with a home, it is simply not being done. It is recorded under
Rabbit Holes, which is the correct place for a tempting avenue we are declining.

## Update System

No update system changes required — this work is confined to `examples/` and
`.github/`, and changes nothing about how popoto is installed or deployed.

## Agent Integration

No agent integration required — `examples/` is a standalone demo application
with no MCP or tool surface.

## Documentation

### Feature Documentation
- [ ] `examples/README.md` — document the smoke test: how to run it locally, the
  `REDIS_URL`-before-import requirement, and the non-zero DB rule.

### External Documentation Site
- [ ] No mkdocs page changes expected — `examples/` is not part of the docs
  site build. Verify `mkdocs build` still passes regardless.

### Inline Documentation
- [ ] Comment at each `save(migrate_key=True)` site naming the immutable-by-default
  KeyField semantics and the old-hash cleanup.
- [ ] Comment on the price-filter category gate pointing at the SortedField contract.
- [ ] Docstring on the smoke test explaining *why* it asserts on notifications —
  the broad `except Exception` handlers make "it mounted" a worthless assertion.

### Repo Instructions
- [ ] `.github/dependabot.yml` — remove the `examples/` deferral paragraph and
  replace it with a comment explaining what now keeps the directory honest.

## Success Criteria

- [ ] `examples/pyproject.toml` declares `requires-python = ">=3.10"`
- [ ] `examples/uv.lock` regenerates cleanly under uv 0.12.2 and `lock-check` passes
- [ ] msgpack ≥ 1.2.1 and pygments ≥ 2.20.0 in `examples/uv.lock` (both alerts cleared)
- [ ] No private Textual API anywhere in `examples/` — source or test
- [ ] All five screens drive headlessly with zero error notifications
- [ ] Rename, move-category, and price-filter paths work and are exercised by the test
- [ ] `.github/dependabot.yml` has a `uv` entry for `/examples`; deferral paragraph gone
- [ ] The smoke job runs in CI and includes an `examples/` lockfile-sync check
- [ ] **Manual TUI check** (the issue's own acceptance bar, retained alongside
      the automated signal): launch `popoto-kitchen` against a non-zero Redis DB
      and visually confirm the price-filter explanation text and the rename /
      move-category flows render as intended. The headless test asserts these
      paths do not error; only a human confirms they read well.
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

Small appetite, single coherent migration across a handful of files in one
worktree. The dev executes directly; fanning out parallel builders over ~25
lines in shared files would only risk interleaved commits.

### Team Members

- **Reviewer**
  - Name: `kitchen-reviewer`
  - Role: Review the PR against this plan
  - Agent Type: code-reviewer
  - Resume: true

## Step by Step Tasks

### 1. Manifest and lockfile
- **Task ID**: build-manifest
- **Depends On**: none
- **Validates**: `lock-check` gate
- **Informed By**: spike-1 (uv 0.12.2 worktree-scoped), spike-2 (relock clears both alerts)
- **Parallel**: false
- Set `requires-python = ">=3.10"`.
- Raise the `textual` floor to `>=8.2` to match the API the source will use.
- Regenerate `examples/uv.lock` with the uv 0.12.2 binary, by absolute path.
- Confirm msgpack and pygments versions in the new lock.

### 2. Textual API migration
- **Task ID**: build-textual
- **Depends On**: build-manifest
- **Informed By**: spike-4 (exact sites enumerated)
- **Parallel**: false
- Replace `Select.BLANK` → `Select.NULL` (12 sites, 4 files).
- Replace `list(table._row_locations.keys())[table.cursor_row]` →
  `table.ordered_rows[table.cursor_row].key` (8 sites, 4 files).
- Confirm no `_row_locations` or other private Textual attribute remains.

### 3. popoto semantics repair
- **Task ID**: build-popoto
- **Depends On**: build-textual
- **Informed By**: spike-4, prior art #166
- **Parallel**: false
- Rename (restaurants) and move-category (menu): `save(migrate_key=True)`, each
  with a comment naming the immutable-by-default KeyField semantics.
- Menu price filter: require a category, and make the UI say why.

### 4. Headless smoke test
- **Task ID**: build-smoke
- **Depends On**: build-popoto
- **Validates**: `examples/tests/test_kitchen_smoke.py` (create)
- **Informed By**: spike-5 (public `notify()` override; no private API)
- **Parallel**: false
- Add a `dev` extra to `examples/pyproject.toml` (pytest, pytest-asyncio); relock.
- Session fixture seeds Redis with small fixed counts, `REDIS_URL` bound before
  `import popoto`, asserting the DB is not 0.
- `PopotoKitchen` subclass overriding public `notify()` to capture severities.
- Drive all five tabs; assert zero error notifications and exact row counts.
- Cover rename, move-category, and the price-filter category gate.
- Fill geo inputs before pressing geo-search (spike-4 trap).

### 5. CI job and Dependabot config
- **Task ID**: build-ci
- **Depends On**: build-smoke
- **Parallel**: false
- Add a separate lightweight workflow job: `redis` service, install
  `examples/[dev]`, run the smoke test.
- Set `REDIS_URL` on the **job/step env**, not in a fixture (see the Redis
  binding contract in Data Flow). Use a non-zero DB.
- **Add a lockfile-sync step to the same job**: `cd examples && uv lock --check`,
  using `astral-sh/setup-uv@v7` with `version: "0.12.2"` to match the pin at
  `.github/workflows/lock-check.yml:34`. This closes a real gap — `lock-check.yml`
  filters on root `pyproject.toml`/`uv.lock` only, so without this step a
  Dependabot PR editing `examples/uv.lock` would have no sync gate at all,
  which is precisely the drift `#523` created that workflow to prevent.
- Trigger on changes to `examples/**`, `src/popoto/**`, and the workflow file.
  The `src/popoto/**` trigger is deliberate and was challenged in critique —
  see the rationale note below.
- Add the `/examples` `uv` entry to `.github/dependabot.yml`; remove the
  deferral paragraph.

**Rationale for the `src/popoto/**` trigger (critique CONCERN, accepted with
justification).** Scoping the job to `examples/**` alone would be cheaper and
would keep library PRs off a Redis-backed job. It was rejected because it
catches only half the documented failure mode: of the five defects spike-4
found, **three were caused by popoto's own API evolving** (`migrate_key`,
SortedField partitioning) — changes that land under `src/popoto/` and never
touch `examples/`. An `examples/**`-only trigger would have caught none of them,
and the demo would rot exactly as it already did. The cost is one short job
(seeded counts are small; the main `tests.yml` already runs a Redis service, so
no new infrastructure). Narrowed from `src/**` to `src/popoto/**` so unrelated
tree changes do not trigger it.

### 6. Review
- **Task ID**: review-pr
- **Depends On**: build-ci
- **Assigned To**: `kitchen-reviewer`
- **Agent Type**: code-reviewer
- **Parallel**: false
- Review the PR against this plan.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| requires-python raised | `grep -c 'requires-python = ">=3.10"' examples/pyproject.toml` | output contains 1 |
| No py3.8 floor remains | `grep 'requires-python' examples/pyproject.toml` | output does not contain 3.8 |
| msgpack alert cleared | `grep -A1 'name = "msgpack"' examples/uv.lock \| grep version` | output contains 1.2.2 |
| pygments alert cleared | `grep -A1 'name = "pygments"' examples/uv.lock \| grep version` | output contains 2.21.0 |
| Lock format not regressed | `grep '^revision' examples/uv.lock` | output does not contain revision = 1 |
| No private Textual row API | `grep -rc '_row_locations' examples/` | match count == 0 |
| No deprecated Select sentinel | `grep -rc 'Select\.BLANK' examples/` | match count == 0 |
| No private notification read | `grep -rc '_notifications' examples/` | match count == 0 |
| Dependabot covers examples | `grep -c 'directory: "/examples"' .github/dependabot.yml` | output contains 1 |
| Deferral paragraph removed | `grep 'examples/ is deliberately absent' .github/dependabot.yml` | exit code != 0 |
| Smoke test passes | `cd examples && REDIS_URL=redis://localhost:6379/13 .venv/bin/python -m pytest tests/ -q` | exit code 0 |
| Library suite unaffected | `python -m pytest tests/ -q -x` | exit code 0 |

## Critique Results

FULL roster, independent (3 critics), one round. 0 blockers, 5 concerns — all
addressed in the revision pass below.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness (Skeptic) | `REDIS_URL` cannot be bound from a fixture — popoto's `pytest11` plugin imports `popoto.redis_db` in `pytest_configure`, before collection, so the connection is already bound (to DB 0 by default) before any fixture runs | Added the **Redis binding contract** to Data Flow; task 5 sets `REDIS_URL` on the job env | Never write `os.environ["REDIS_URL"]` in `conftest.py` — it is a no-op that gives false confidence. Bind as a process env var ahead of `pytest`; the fixture only asserts the DB is not 0. Verified at `src/popoto/pytest_plugin.py:81-89` |
| CONCERN | Risk & Robustness (Operator) | `lock-check.yml` filters on root `pyproject.toml`/`uv.lock` only, so after this lands a Dependabot PR editing `examples/uv.lock` would have **no** lock-sync gate | Added a `uv lock --check` step to the new job in task 5 | `cd examples && uv lock --check` with `setup-uv@v7` `version: "0.12.2"`, matching the pin at `lock-check.yml:34`. Reuses the existing version-pin rationale instead of creating a second format-skew surface. Confirmed: `lock-check.yml` `on.*.paths` has no `examples/` entry |
| CONCERN | Scope & Value (Simplifier) | Triggering the new job on `src/**` puts a Redis-backed job on every library PR — scope beyond the ask | **Accepted with justification, not dropped.** Narrowed `src/**` → `src/popoto/**`; rationale recorded inline in task 5 | Three of the five defects originated in popoto's own API (`migrate_key`, SortedField partitioning) and touch only `src/popoto/`. An `examples/**`-only trigger catches none of them, which is the rot this plan exists to stop |
| CONCERN | Scope & Value (User) | All success criteria are automated; the issue's own stated bar is a manual TUI look, and this plan changes user-visible text | Added a manual-TUI Success Criterion alongside the automated signal | The headless test proves these paths do not error; only a human confirms the new price-filter explanation and rename flows *read* well. Costs nothing — the dev runs the app during tasks 3-4 anyway |
| CONCERN | History & Consistency | Both No-Gos were tagged `[SEPARATE-SLUG #611]`, but #611 is this plan's own tracking issue and the PR carries `Closes #611` — the deferrals would have had nowhere to land | Filed **#660** for the root-lock deferral and retagged; moved the Textual-idiom item out of No-Gos entirely | A No-Go tag must point at an issue that *survives* this PR. The Textual-idiom entry was never deferred work with a home — it duplicated an existing Rabbit Hole, which is the right section for a declined avenue |

---

## Open Questions

None outstanding. The decisions that needed judgment — how to fix the three
popoto defects, and whether to hand-refresh the root `uv.lock` — were resolved
with the PM before this plan was finalized, and the five critique concerns were
resolved in the revision pass above.
