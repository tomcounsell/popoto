---
status: Planning
type: chore
appetite: Small
owner: Dev (SDLC lane sdlc-639)
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/639
last_comment_id:
---

# tests.yml db-less REDIS_URL

## Problem

`.github/workflows/tests.yml` sets `REDIS_URL: redis://localhost:6379` at job
level in both jobs (line 51, `pytest (Redis)`; line 121, `pytest (Valkey)`).
That URL names no database.

**Current behavior:** Nothing is broken today, and that is the whole difficulty
— the instance is fixed but the class is not. Any test or subprocess that
constructs a connection from the environment inherits a URL that popoto's
`#584` guard rejects at bind time. The test passes on a developer machine
(where `REDIS_URL` is either unset or names a real database) and fails only in
CI. That has already happened twice:

- **PR #603** — `test(#596): pin doctor eviction test env against CI's db-less
  REDIS_URL`. A test had to be pinned specifically because of this variable.
- **#635 / PR #637** — `scripts/ci-local.sh` exported the identical db-less
  default and broke `tests/test_integrations_mcp.py`. Fixed at the script, and
  the workflow was named an explicit Rabbit Hole and deliberately left alone.

This issue is the deferred half of #635. It was surfaced by the reviewer of PR
#637 as an acknowledged out-of-scope exclusion.

**Desired outcome:** A test that builds a connection from the environment
behaves the same in CI as it does locally, so this class of "passes locally,
fails in CI" defect stops recurring. Both the Redis and Valkey jobs stay green.

## Freshness Check

**Baseline commit:** `25d97c2e` (`fix(#635): stop ci-local.sh exporting a
db-less REDIS_URL (#637)`)
**Issue filed at:** 2026-09-06T10:00:24Z
**Disposition:** **Unchanged**

**File:line references re-verified:**

- `.github/workflows/tests.yml:51` — issue claims `REDIS_URL:
  redis://localhost:6379` at job level in `pytest (Redis)` — **still holds**,
  verbatim.
- `.github/workflows/tests.yml:121` — same claim for `pytest (Valkey)` —
  **still holds**, verbatim.

**Cited sibling issues/PRs re-checked:**

- **#635** — closed 2026-09-06 by PR #637 (merged `25d97c2e`). Fixed
  `ci-local.sh` only; the workflow was explicitly excluded. Confirms this
  issue's premise rather than overtaking it.
- **#584** — CLOSED: *"popoto.integrations DEFAULT_URL points at Redis DB 0,
  the same DB the test guard refuses"*. Directly load-bearing here — see
  Technical Approach.
- **PR #603** — MERGED: *"test(#596): pin doctor eviction test env against
  CI's db-less REDIS_URL"*. The precedent the issue alludes to.

**Commits on main since issue was filed (touching referenced files):** none.
`.github/workflows/tests.yml` was last touched by `71ed080d` and `f6ba5133`,
both dependabot action-version bumps that did not go near the `env:` blocks.

**Active plans in `docs/plans/` overlapping this area:**
`ci_local_redis_url_fallback.md` is the **completed** #635 plan for the sibling
fix in `ci-local.sh`. It is prior art, not a live overlap — no coordination
needed.

**Notes:** The issue was filed 20 minutes before planning began and no relevant
code has moved. All premises verified first-hand rather than inherited from the
issue text.

## Prior Art

- **#635 / PR #637**: *stop ci-local.sh exporting a db-less REDIS_URL*. Chose
  to stop exporting the variable rather than give it a database, on the reasoning
  that the pytest plugin should be the single binder. Succeeded. **This plan
  deliberately does not inherit that choice — see Technical Approach for why the
  reasoning does not transfer to a CI workflow.**
- **PR #603 (for #596)**: *pin doctor eviction test env against CI's db-less
  REDIS_URL*. Worked around this exact variable by pinning one test's
  environment. Succeeded locally as a patch, but treated the symptom: the next
  from-env test hit the same wall, which is #635, and then this issue.
- **#584**: *popoto.integrations DEFAULT_URL points at Redis DB 0, the same DB
  the test guard refuses*. Established that a bind resolving to DB 0 is refused
  rather than silently honoured. Closed. This is precisely what makes "just
  delete the variable" less safe than it looks.
- **#549 / PR #605**: *test_pytest_plugin.py hardcodes DB 15* → *resolve the
  expected test DB instead of hardcoding 15* (MERGED). The repo's closest
  precedent on the exact hardcode-vs-resolve tension this plan argues through.
  Five assertions hardcoded the literal `15` and broke under a `POPOTO_TEST_DB`
  override; the fix was to resolve the DB dynamically. The drift guard below
  already honours that precedent — it reads `popoto_test_db` from
  `pyproject.toml` rather than asserting a literal — and the workflow constant
  is safe against it because no CI workflow sets `POPOTO_TEST_DB` (see
  Technical Approach point 2). Surfaced by the plan critique.

## Research

No relevant external findings — proceeding with codebase context. This is a
change to one repository's own CI workflow and its own connection-resolution
code; no external library, API, or ecosystem pattern is involved.

## Data Flow

How `REDIS_URL` reaches a connection in CI, and where each candidate value
lands. Both paths were verified first-hand at plan time.

1. **Entry point**: `tests.yml` sets `REDIS_URL` as a job-level `env:`, so every
   step in both jobs inherits it, including any subprocess a test spawns without
   an explicit `env=`.
2. **Path A — the ORM's global client.** `src/popoto/redis_db.py:405` reads
   `REDIS_URL` at *import* time to build `POPOTO_REDIS_DB`. Under pytest, the
   plugin then calls `_swap_db()`, which sets `current_kwargs["db"] =
   target_db` **unconditionally** (`src/popoto/pytest_plugin.py:288-306`). The
   database component of `REDIS_URL` is therefore always discarded on this
   path; only host and port survive, and those are `localhost:6379` under every
   candidate value.
3. **Path B — the integrations/memory client.**
   `MemoryConfig.from_env` (`src/popoto/integrations/config.py:176-181`)
   resolves `POPOTO_MEMORY_URL` → `REDIS_URL` → `DEFAULT_URL`. This path
   honours the database component, and it is the only path that can fail.
4. **Output**: a bound connection, or a `#584` refusal raised before any
   command reaches the server.

Verified resolution on Path B (probe run in this worktree against
`MemoryConfig.from_env`, config-only, no connection opened):

| Workflow sets | Resolved URL | `url_source` |
|---|---|---|
| `redis://localhost:6379` (today) | `redis://localhost:6379` | `REDIS_URL` |
| nothing (option 2) | `redis://localhost:6379/0` | `default` |
| `redis://localhost:6379/15` (option 1) | `redis://localhost:6379/15` | `REDIS_URL` |

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|---|---|---|
| PR #603 | Pinned one test's env so CI's db-less `REDIS_URL` could not reach it | Treated the symptom in a single test. The variable stayed, so the next from-env consumer hit the identical wall. |
| PR #637 (#635) | Stopped `ci-local.sh` exporting the db-less default | Correct and complete **for the script**. The workflow sets the same value by a different mechanism and was explicitly out of scope. |

**Root cause pattern:** each fix removed one *consumer* from the blast radius
instead of fixing the *source*. Three rounds (#596, #635, #639) have now been
spent on one bad string in two places. The source is the value itself: a URL
that names no database, sitting in the environment of every CI step.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 1-2 (the fix choice departs from the #635 precedent and was
  explicitly flagged for reasoning, so the decision needs to be seen)
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| Worktree venv resolves to this checkout | `/Users/valorengels/src/popoto/.worktrees/sdlc-639/.venv/bin/python -c "import popoto; assert '.worktrees/sdlc-639' in popoto.__file__"` | Prevents testing another tree (CLAUDE.md gotcha 1) |
| MCP extra installed | `/Users/valorengels/src/popoto/.worktrees/sdlc-639/.venv/bin/python -c "import mcp"` | Without it the MCP tests silently skip (gotcha 2) |
| Redis reachable | `redis-cli -u redis://localhost:6379/6 ping` | Lane runs on DB 6 |

## Solution

### Key Elements

- **`tests.yml` (both jobs)**: `REDIS_URL` names a database — the same one the
  pytest plugin isolates onto — instead of naming none.
- **A drift guard test**: asserts the database number in the workflow's
  `REDIS_URL` equals `popoto_test_db` in `pyproject.toml`, so the two cannot
  silently disagree later — and, symmetrically, that if any workflow ever sets
  `POPOTO_TEST_DB`, it names the same database. Both halves guard the same
  invariant: everything CI can bind with resolves to one database.

### Flow

CI job starts → `REDIS_URL=redis://localhost:6379/15` in the job env → a test
builds a connection from the environment → it binds DB 15, the same database
the pytest plugin flushes and isolates onto → the test behaves exactly as it
does locally.

### Technical Approach

**The decision: option 1 (name the database), not option 2 (delete the
variable) — with a drift guard.**

The issue recommends option 2, and it matches the #635 precedent. I am not
taking it, and the reason is mechanical rather than stylistic.

**1. Option 2 does not fix the hazard class the issue describes.**
`DEFAULT_URL` in `src/popoto/integrations/config.py:44` is
`"redis://localhost:6379/0"`. Deleting `REDIS_URL` therefore does not produce
"no URL"; it produces a **DB 0** URL, via the `default` branch of `from_env`.
The failure mode changes from the `_no_db_message` refusal ("has no database
number") to the `#584` DB-0 refusal — a different exception, but still an
exception. A future from-env test would *still* pass locally and fail in CI,
which is the exact defect the issue asks us to close, and the exact defect PR
#603 already paid for once. Option 2 renames the symptom.

Option 1 is the only candidate under which a from-env bind in CI actually
*works*.

**2. The reasoning behind #635 does not transfer.** The objection that killed
`redis://localhost:6379/15` for `ci-local.sh` was that hardcoding 15 silently
contradicts a `POPOTO_TEST_DB` override, which parallel worktree lanes rely on.
That objection is about *developer machines*, where lanes genuinely do vary the
DB (this lane is on DB 6). **No workflow in `.github/workflows/` sets
`POPOTO_TEST_DB` at all** — verified by `grep -rn 'POPOTO_TEST_DB'
.github/`, which returns no matches (exit code 1) — so in CI the database is
always
`pyproject.toml`'s `popoto_test_db = "15"`. There is no override to contradict.
Inheriting the #635 conclusion here would be inheriting a premise that is false
in this environment.

**3. Nothing in CI needs a from-env bind today**, so this is a
forward-looking fix and cannot regress anything. An audit of all nine
`REDIS_URL`-touching test files found exactly three sites that inherit the
ambient value (`tests/test_production_contracts.py:263`,
`tests/test_pytest_plugin.py:642` and `:870`) — all three are subprocess spawns
that inherit `os.environ`, and every one is neutralised by a
higher-priority variable the test sets itself (`POPOTO_MEMORY_URL` or
`POPOTO_TEST_DB`). Every other reference sets or deletes the variable
explicitly. Two behaviours were verified directly rather than taken on report:
`_base_env` does inherit `os.environ`, and `_swap_db` overwrites `db`
unconditionally. Host and port are `localhost:6379` under all three candidate
values, so none of the three changes behaviour.

**4. The one new risk option 1 introduces is drift, and it is worth closing.**
If someone later changes `popoto_test_db` in `pyproject.toml`, the workflow's
hardcoded 15 becomes stale, and a from-env consumer would bind a *different*
database than the plugin isolates onto — a silent split-brain, which is worse
than a loud refusal. This is the strongest argument for option 2 and it should
not be waved away. It is closed cheaply by a test that parses both files and
asserts they agree. That test needs no Redis and no CI, so unlike the change
itself it is provable locally.

The critique raised a second drift vector on the same invariant: `_swap_db`
makes `POPOTO_TEST_DB` — not `pyproject.toml` — the actual authority for the
plugin's database (`src/popoto/pytest_plugin.py:288-306`). A future workflow
edit that introduced `POPOTO_TEST_DB` would move Path A while the hardcoded
`REDIS_URL` held Path B on 15, reproducing exactly the split-brain this section
argues against, with a one-sided guard still green. The guard therefore asserts
both directions. Today the second assertion is vacuous (no workflow sets the
variable), which is the point: it fires the moment that stops being true.

The critique also questioned whether a new permanent test belongs in a
two-line workflow fix at all, since none of the issue's three options proposed
one. The guard stays, because it is not decoration on the fix — it is the
mitigation that makes option 1 defensible instead of merely convenient.
Choosing option 1 without it would leave the plan's own Risk 2 unanswered and
the departure from #635 unearned. What the objection does correctly identify is
over-specification, so the guard is scoped down below to two assertions with
plain messages rather than an enumerated failure-mode spec.

**Considered and rejected:** deriving the workflow's `REDIS_URL` from
`pyproject.toml` at job setup (a step that writes `GITHUB_ENV`). It removes the
duplication at its root, but adds a scripted step to both jobs to eliminate a
constant that changes approximately never, and a failure in that step would be
far more confusing than a stale constant. The drift test buys the same safety
for a fraction of the moving parts.

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope. The change is two YAML scalars plus one new
  test; neither introduces nor modifies a `try`/`except` block.

### Empty/Invalid Input Handling
- The new drift test parses two files that are committed to the repo. Every
  failure must name both observed values ("workflow says 15, pyproject says
  14") rather than raising an opaque `AttributeError`/`IndexError` on a
  `None`/no-match, so a future editor sees the disagreement instead of a stack
  trace. That is the whole requirement; the builder owns how many assertions it
  takes.

### Error State Rendering
- No user-visible output. This is CI configuration.

## Test Impact

- [ ] `tests/test_ci_workflow_redis_url.py` — **CREATE**: the drift guard.
- [ ] `tests/test_production_contracts.py` — no change. Its ambient-inheritance
  site at `:263` sets `POPOTO_MEMORY_URL`, which outranks `REDIS_URL` in
  `from_env`, so it is unaffected by the value.
- [ ] `tests/test_pytest_plugin.py` — no change. Its two ambient sites (`:642`,
  `:870`) set `POPOTO_TEST_DB`, and `_swap_db` overrides the database
  unconditionally.

No existing test is modified. The change cannot alter any current test's
behaviour, because no current test reads the database component of the ambient
`REDIS_URL` — which is precisely why CI staying green is a necessary but weak
signal here, and why the drift test carries the real regression value.

## Rabbit Holes

- **Rewriting how the plugin and `from_env` share a connection.** There are two
  connection-resolution paths (ORM global vs. integrations) with different
  precedence rules. Unifying them is a real design question and completely out
  of scope for a two-line workflow fix.
- **Auditing every workflow for other env hazards.** Only `tests.yml` sets
  `REDIS_URL`; the other five workflows do not. Resist a general sweep.
- **Making `DEFAULT_URL` non-zero.** Tempting, since a DB-0 default is what
  makes option 2 unsafe. But it is a published library's runtime default, it
  affects every downstream consumer, and #584 already settled the current
  behaviour deliberately. Not a CI concern.
- **Chasing the two "unused" ambient sites.** They are inert today. Pinning
  their environments would be three more PR #603s — treating consumers instead
  of the source.

## Risks

### Risk 1: DB 15 is shared, and now a from-env consumer could reach it
**Impact:** A future test that binds from the environment lands on DB 15, the
same database the pytest plugin flushes at session start. Two things sharing a
database can surprise each other.
**Mitigation:** This is the intended and correct outcome, not an accident — DB
15 is exactly where such a test *should* land, because that is the isolated
test database. The alternative (a refusal) is not safety, it is a broken test.
The plugin's flush is a session-start event, so it cannot interleave with a
test that binds later.

### Risk 2: Drift between the workflow constant and `pyproject.toml`
**Impact:** A stale workflow value would put a from-env consumer on a different
database than the plugin isolates onto — silent, and worse than the loud
refusal we have today.
**Mitigation:** The drift guard test. It is the reason this plan is option 1
**plus** a test rather than option 1 alone, and it runs locally without Redis.

### Risk 3: Only CI can prove the real change
**Impact:** The workflow edit takes effect only in GitHub Actions. A green local
suite says nothing about whether the Redis and Valkey jobs still pass.
**Mitigation:** Accept iteration on the PR as the verification loop, and say so
in the PR body. Run the full local suite for regressions first so CI is
exercised on an already-plausible change rather than used as a first-pass
debugger.

## Race Conditions

No race conditions identified. The change is two static YAML values read once
at job start, plus one test that reads two files from disk synchronously. There
is no shared mutable state, no concurrency, and no async path.

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG #584]` Changing `DEFAULT_URL` away from
  `redis://localhost:6379/0`. It is the root reason option 2 is unsafe, but it
  is a published library default with downstream consumers and was settled
  deliberately in #584.
- `[EXTERNAL]` Confirming both CI jobs are green. Requires a push to GitHub and
  a real Actions run on GitHub-hosted runners; it cannot be produced locally.

## Update System

No update system changes required — this is a CI workflow change and does not
propagate to any installation.

## Agent Integration

No agent integration required — this changes CI configuration and adds one
test. No tool or MCP surface is involved.

## Documentation

### Feature Documentation
- Not applicable — no feature.

### External Documentation Site
- [ ] No mkdocs page describes CI's environment; nothing to update. Confirm
      `mkdocs build --strict` still exits 0.

### Inline Documentation
- [ ] A YAML comment above each `REDIS_URL` line stating that the database
      number must match `popoto_test_db` in `pyproject.toml`, and naming the
      guard test. Without it the next editor sees a bare constant with no
      indication it is coupled to another file.
- [ ] `CLAUDE.md` — the `ci-local.sh` paragraph added by #635 says the script
      does not export `REDIS_URL`. Record that the workflow takes the opposite
      approach and why, so the two do not read as an inconsistency.

## Success Criteria

- [ ] Both `tests.yml` jobs set `REDIS_URL` with an explicit database number
      matching `popoto_test_db`
- [ ] A guard test fails if the workflow and `pyproject.toml` disagree, in
      either direction (`REDIS_URL`'s database component, and `POPOTO_TEST_DB`
      if a workflow ever sets it)
- [ ] The `pytest (Redis)` and `pytest (Valkey)` jobs are both green on the PR
- [ ] The relationship to #635 is recorded in the PR body so the two are not
      re-litigated separately
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (ci-config)**
  - Name: `ci-config-builder`
  - Role: Edit both `tests.yml` env blocks and write the drift guard test
  - Agent Type: builder
  - Resume: true

Single builder, single worktree, disjoint files — no parallel lanes needed at
this size.

## Step by Step Tasks

### 1. Change the workflow and add the guard

- **Task ID**: build-ci-redis-url
- **Depends On**: none
- **Validates**: `tests/test_ci_workflow_redis_url.py` (create)
- **Informed By**: Data Flow table (probe-verified resolution of all three
  candidate values); the nine-file ambient audit in Technical Approach
- **Assigned To**: `ci-config-builder`
- **Agent Type**: builder
- **Parallel**: false
- Set `REDIS_URL: redis://localhost:6379/15` at `.github/workflows/tests.yml:51`
  and `:121`, with a comment above each naming `popoto_test_db` and the guard
  test.
- Create `tests/test_ci_workflow_redis_url.py` with two assertions:
  1. Every `REDIS_URL` in `.github/workflows/` has a database component, and
     that component equals `popoto_test_db` in `pyproject.toml`. Covering
     *every* occurrence rather than the two known lines means a future third
     job cannot slip through unchecked.
  2. If any workflow sets `POPOTO_TEST_DB`, its value equals `popoto_test_db`
     too. Vacuous today (no workflow sets it) and deliberately so — it exists
     to fire when that changes. See Technical Approach point 4.
  Failure messages must name both observed values.
- Outcome constraint, not an implementation directive: this check must not add
  a new test dependency. How the two files get parsed is the builder's call.

### 2. Verify locally, then on CI

- **Task ID**: validate-ci-redis-url
- **Depends On**: build-ci-redis-url
- **Assigned To**: `ci-config-builder`
- **Agent Type**: builder
- **Parallel**: false
- Run the guard test, plus the two files containing ambient sites and
  `test_integrations_mcp.py` (the #637 regression check), on DB 6.
- Confirm the guard actually fails when the two values disagree (red-state
  proof — temporarily edit one, observe the failure and its message, revert).
  Do the same for the `POPOTO_TEST_DB` assertion by temporarily adding a
  disagreeing value to one job's `env:`, since it is vacuous otherwise and an
  always-green assertion proves nothing.
- Write the PR body: it must state the relationship to #635 (same hazard class,
  opposite remedy) and why, so the two are not re-litigated separately, and
  must note that only CI can prove the workflow half.
- Push and read the Redis and Valkey job results; iterate on the PR.

### 3. Record the coupling in CLAUDE.md

- **Task ID**: docs-ci-redis-url
- **Depends On**: validate-ci-redis-url
- **Assigned To**: `ci-config-builder`
- **Agent Type**: builder
- **Parallel**: false
- The `ci-local.sh` paragraph added by #635 states the script does not export
  `REDIS_URL`. Record that `tests.yml` takes the opposite approach and why, so
  the two do not read as an inconsistency. Handled in the DOCS stage if
  `/do-docs` reaches it first; this task exists so the Documentation section
  maps to a task rather than to nothing.

## Verification

| Check | Command | Expected |
|---|---|---|
| Drift guard passes | `POPOTO_TEST_DB=6 .venv/bin/python -m pytest tests/test_ci_workflow_redis_url.py -q` | exit code 0 |
| Ambient-site files unaffected | `POPOTO_TEST_DB=6 .venv/bin/python -m pytest tests/test_pytest_plugin.py tests/test_production_contracts.py -q` | exit code 0 |
| No regression on #637's fix | `POPOTO_TEST_DB=6 .venv/bin/python -m pytest tests/test_integrations_mcp.py -q` | exit code 0 |
| No db-less REDIS_URL left in any workflow | `grep -rn 'redis://localhost:6379$' .github/workflows/` | exit code 1 |
| Both jobs carry a database number | `grep -c 'REDIS_URL: redis://localhost:6379/15' .github/workflows/tests.yml` | output contains 2 |
| Workflow and pyproject agree | `grep -o 'popoto_test_db = "[0-9]*"' pyproject.toml` | output contains 15 |
| Anti-criterion: pyproject's test DB untouched | `git diff origin/main -- pyproject.toml \| grep -c popoto_test_db` | match count == 0 |
| Docs build | `.venv/bin/mkdocs build --strict` | exit code 0 |
| Format clean | `.venv/bin/python -m black --check src/ tests/` | exit code 0 |

## Critique Results

**Depth:** FULL (3 critics, independent roster) · **Verdict:** NEEDS REVISION →
revised in one round, routed to BUILD · **Findings:** 6 (0 blockers, 3
concerns, 3 nits)

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | CONCERN | Drift guard checks only `REDIS_URL` vs `pyproject.toml`; `_swap_db` makes `POPOTO_TEST_DB` the real authority for Path A, so a future workflow adding it would split-brain with the guard still green | **Accepted.** Second assertion added (Technical Approach point 4, Task 1). |
| 2 | CONCERN | The drift guard is scope creep — none of the issue's three options proposed a new permanent test | **Partly accepted.** Guard kept (it is what makes option 1 defensible rather than merely convenient); its spec scaled back from enumerated failure modes to two assertions with plain messages. |
| 3 | CONCERN | Prior Art omits #549 / PR #605, the repo's closest precedent on hardcode-vs-resolve | **Accepted.** Bullet added; both verified to exist via `gh`. |
| 4 | NIT | "No workflow sets `POPOTO_TEST_DB`" was asserted without evidence | **Accepted.** Verifying command and its result inlined. |
| 5 | NIT | Task 1 over-specified the parsing method | **Accepted.** Restated as an outcome constraint ("no new test dependency"). |
| 6 | NIT | Verification table lists `test_integrations_mcp.py` as an ambient site; it clears `REDIS_URL` itself at `tests/test_integrations_mcp.py:334` | **Accepted.** Split into its own "no regression on #637's fix" row. |

Structural checks: all required sections present and non-empty; task numbering
and dependencies valid, no cycles; every referenced path exists except
`tests/test_ci_workflow_redis_url.py`, which this plan creates. One orphaned
success criterion (the #635 relationship in the PR body) and one unmapped
Documentation item (`CLAUDE.md`) now map to Tasks 2 and 3.

Findings 1 and 2 flag the same component from opposite directions — one wanting
the guard stronger, one wanting it gone. Resolved by keeping it and taking the
actionable half of each.

---

## Open Questions

1. The plan departs from the #635 precedent (option 1 rather than option 2) on
   the grounds that `DEFAULT_URL` is `.../0`, so deleting the variable trades a
   "no database" refusal for a "database 0" refusal without fixing the class.
   Confirm that reading, since the issue itself recommends option 2. The
   critique did not challenge the departure itself — no critic argued for
   option 2, and the one objection near it (finding 2) was about the guard's
   size, not the choice. So this remains a supervisor confirmation, not an
   unresolved technical question; BUILD proceeds on option 1 unless told
   otherwise.
