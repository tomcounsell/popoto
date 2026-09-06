---
status: Planning
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/635
last_comment_id:
---

# ci-local.sh must stop inventing a db-less REDIS_URL

## Problem

`scripts/ci-local.sh` invents a Redis URL when the developer's shell has none,
and exports it to every gate:

```sh
REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export REDIS_URL
```

That URL names no database. `tests/test_integrations_mcp.py::test_an_in_process_client_lists_and_calls_the_tools`
deliberately leaves the URL unset (its docstring says so) so `bind_connection`
stays inert and the MCP server keeps the pytest plugin's isolated database.
With the invented URL exported, the server resolves it as its connection source
and the `#584` no-database refusal fires inside the tool call.

**Current behavior:** `scripts/ci-local.sh tests` (also `--fast`, `--all`) on a
clean `main` checkout reports one failure whenever the developer has no
`REDIS_URL` in their shell — the common case. GitHub CI does not hit it because
`tests.yml` sets its own environment.

Observed in this worktree at `3cf8c2d0` (Python 3.12, redis-py 8.x, `POPOTO_TEST_DB=6`):

```
# passes
env -u REDIS_URL pytest tests/test_integrations_mcp.py -k test_an_in_process_client_lists_and_calls_the_tools
  -> 1 passed

# fails, exactly as the script arranges it
REDIS_URL=redis://localhost:6379 pytest tests/test_integrations_mcp.py -k test_an_in_process_client_lists_and_calls_the_tools
  -> assert 'gateway' in "memory_search failed: ValueError: POPOTO_MEMORY_URL='redis://localhost:6379'
     has no database number. ..."
```

**Desired outcome:** the script stops inventing a connection URL for the gates,
and the test enforces its own "URL deliberately unset" contract rather than
assuming the caller honours it.

## Freshness Check

**Baseline commit:** `3cf8c2d0946769ab75f8b9d719a4a4dacecbc24a` (`origin/main`)
**Issue filed at:** 2026-09-05T03:21:44Z
**Disposition:** Minor drift (line numbers only; every claim still holds)

**File:line references re-verified:**
- `scripts/ci-local.sh:46` — issue claims the fallback and `export` live here —
  **drifted to `scripts/ci-local.sh:53-54`**; the two lines are otherwise verbatim
  as described.
- `tests/test_integrations_mcp.py:308-318` — issue claims the docstring states the
  URL is left unset on purpose — **drifted to `tests/test_integrations_mcp.py:307-317`**;
  the docstring says exactly that, and line 325 already does
  `monkeypatch.delenv("POPOTO_MEMORY_URL", raising=False)` but not `REDIS_URL`.

**Cited sibling issues/PRs re-checked:**
- PR #634 — merged; confirmed by `git log` that it touched neither
  `scripts/ci-local.sh` nor `tests/test_integrations_mcp.py`. It is the review
  that surfaced the finding, not a cause.

**Commits on main since issue was filed (touching referenced files):**
- None. `git log --since="2026-09-05" -- scripts/ci-local.sh tests/test_integrations_mcp.py`
  is empty.

**Bug reproduced against current main:** yes — both commands above, run in
`.worktrees/sdlc-635` at `3cf8c2d0`.

**Active plans in `docs/plans/` overlapping this area:** none.

## Prior Art

- **PR #497**: "ci: preflight guards for the worktree verification traps (#495 post-mortem)" —
  added the environment preflight checks to `ci-local.sh`. It hardened *which
  package and extras* the script tests, not which database it points at, so it
  neither introduced nor addressed this fallback.
- **PR #529**: "fix(#523): sync uv.lock with pyproject and gate it in CI + ci-local" —
  added a gate to the same script; no connection-URL involvement.
- **Issue #420**: DB isolation bypass via `src.popoto` module aliasing — same
  general family (test writes escaping the isolated DB) but a different mechanism
  (import aliasing, not an environment variable). Not a prior attempt at this bug.
- **Issue #584 / PR #601**: the "refuse a db-less or DB-0 URL rather than silently
  repointing" work. It is why this bug surfaces as a loud `ValueError` instead of
  a silent write to DB 0. The guard is working as designed; the script is what
  feeds it a bad URL.

No prior attempt to fix this specific fallback exists.

## Research

No external research needed — the change is a shell variable in a repo-local
script plus one `monkeypatch.delenv` in a repo-local test. No external libraries,
APIs, or ecosystem patterns are involved.

## Data Flow

1. **Entry point**: developer runs `scripts/ci-local.sh tests` with no `REDIS_URL`
   in the shell.
2. **`scripts/ci-local.sh:53-54`**: the `${REDIS_URL:-...}` default substitutes
   `redis://localhost:6379` and `export` puts it in the environment of every gate.
3. **`pytest` process**: the `popoto` pytest plugin swaps the connection to the
   isolated DB (`popoto_test_db = "15"`, or `POPOTO_TEST_DB`). This is correct and
   unaffected by the env var.
4. **`test_an_in_process_client_lists_and_calls_the_tools`**: clears
   `POPOTO_MEMORY_URL` but not `REDIS_URL`, then builds the MCP server.
5. **`src/popoto/integrations/config.py:177-181`**: `MemoryConfig.from_env` finds
   no `POPOTO_MEMORY_URL`, falls back to `REDIS_URL`, and sets
   `url_source = "REDIS_URL"` with `url = "redis://localhost:6379"`.
6. **`src/popoto/integrations/config.py:327`**: the URL names no database, so the
   `#584` guard raises `ValueError: ... has no database number`.
7. **Output**: the `memory_search` tool call returns the error text instead of the
   seeded record, and the `"gateway" in seen["search"]` assertion fails.

The break is at step 2. Steps 5–6 are the safety guard behaving correctly.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -u redis://localhost:6379/6 ping` | Test gates need a server |
| `mcp` extra installed | `.venv/bin/python -c "import mcp"` | Without it the affected test is skipped, not run |

## Solution

### Key Elements

- **`scripts/ci-local.sh`**: keeps a Redis URL for its own reachability probe and
  status banner, but stops putting one into the environment of the gates.
- **`tests/test_integrations_mcp.py`**: enforces the "URL deliberately unset"
  contract the docstring already claims, so no caller's environment can break it.

### Flow

Developer with no `REDIS_URL` → runs `scripts/ci-local.sh tests` → script probes
Redis reachability with a local (unexported) default → pytest runs with the
environment the developer actually has → the plugin's isolated DB is the single
source of truth for the test connection → zero `test_integrations_mcp.py` failures.

### Technical Approach

The issue leaves the fix open between three options. Take the **first and the
third**; reject the second.

**Chosen — script stops exporting an invented URL.** Replace the exported
fallback at `scripts/ci-local.sh:53-54` with a local variable used only by the
two consumers that genuinely need a URL: the `redis-cli ... ping` reachability
probe (`scripts/ci-local.sh:96-97`) and the status banner (`scripts/ci-local.sh:333`).
A `REDIS_URL` the *developer* set is still in their environment and still reaches
the gates — the script simply stops inventing one. Reasons:

1. The pytest plugin is already the single source of truth for the test
   connection (`popoto_test_db`, `POPOTO_TEST_DB`). Exporting a script-invented
   URL gives the suite a second source of truth that contradicts the first.
2. The invented URL resolves to database 0, which on developer machines here is a
   **live agent store**. The `#584` refusal catches it loudly in this one code
   path, but the general shape — a script silently injecting a DB-0-resolving URL
   into every gate — is a safety hazard, not only a test failure. Fixing it at the
   source removes the hazard for gates that have no such guard.
3. It is the smallest change and removes state rather than adding it.

**Chosen — the test clears `REDIS_URL` itself.** Add
`monkeypatch.delenv("REDIS_URL", raising=False)` next to the existing
`POPOTO_MEMORY_URL` delenv at `tests/test_integrations_mcp.py:325`. This turns the
docstring's contract from an assumption about the caller into something the test
enforces. It is the established pattern in this repo:
`tests/test_integrations_cli.py:59-62` already clears **both** variables for a
sibling test, with a comment naming this exact script fallback. Making the MCP
test match removes an inconsistency as well as the failure.

**Rejected — export `redis://localhost:6379/15`.** It hardcodes database 15, so
it silently contradicts `POPOTO_TEST_DB`, which parallel worktree lanes rely on to
avoid colliding on shared Redis state. It also keeps the script as a second
source of truth for the connection, which is the actual defect.

Together the two changes satisfy both acceptance bullets: the script no longer
breaks the contract, *and* the test enforces it.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. The change is two lines of shell and one
`monkeypatch.delenv` call; neither adds nor modifies a `try`/`except`.

### Empty/Invalid Input Handling
The invalid input in question — a db-less Redis URL — is exactly what this plan
stops producing. Its handling is already covered and must stay covered:
`tests/test_integrations_db0_isolation.py` and `tests/test_integrations_cli.py`
assert the `#584` refusal for db-less and DB-0 URLs. Those tests must still pass
unchanged; this plan does not touch the guard.

### Error State Rendering
Not user-facing. The script's one user-visible error path — "Redis not reachable
at $URL (start it, or set REDIS_URL)" — must keep naming a concrete URL after the
variable becomes local, and must keep exiting 2.

## Test Impact

- [ ] `tests/test_integrations_mcp.py::test_an_in_process_client_lists_and_calls_the_tools` —
      UPDATE: add `monkeypatch.delenv("REDIS_URL", raising=False)` and extend the
      docstring to say the contract is enforced, not assumed.

No other existing tests affected. Every other test that reads `REDIS_URL`
(`tests/test_pytest_plugin.py`, `tests/test_default_memory_eviction.py`,
`tests/test_production_contracts.py`, `tests/test_transfer_cli.py`,
`tests/test_integrations_db0_isolation.py`) sets it explicitly in a subprocess
`env` dict rather than inheriting it, so removing the script's export cannot
change their behaviour. This must be **verified by running them**, not assumed —
see Verification.

## Rabbit Holes

- **Redesigning how `config.py` resolves the connection URL.** The resolution
  order and the `#584` refusal are correct and are load-bearing for several test
  files. The defect is upstream of them.
- **Touching the pytest plugin's DB binding.** `tests/test_pytest_plugin.py:40`
  flags `#595` as a No-Go to edit. Out of scope entirely.
- **Auditing every script in `scripts/` for the same pattern.** Tempting, but it
  is a separate sweep; this plan fixes the one script the issue names.
- **Changing `.github/workflows/tests.yml`.** CI sets its own environment and is
  green; changing it would be an unmeasured risk with no failing symptom.

## Risks

### Risk 1: a gate silently depended on the exported `REDIS_URL`
**Impact:** removing the export breaks a different gate (stress, docs, build) that
never named the variable in the script but read it from the environment.
**Mitigation:** `grep -n REDIS_URL scripts/ci-local.sh` shows only three uses, all
of which the local variable still serves. Beyond that, the Verification table runs
the full set of `REDIS_URL`-reading test files with the variable absent, which is
the real check.

### Risk 2: a developer relied on the script working with no Redis config at all
**Impact:** none for the probe — the local default keeps the reachability check and
its error message working exactly as before. The only behaviour change is that
gates no longer receive an invented URL, which is the fix.
**Mitigation:** keep the probe's default identical to today's string so the
"Redis not reachable" path is unchanged.

## Race Conditions

No race conditions identified. Both changes are to process startup configuration
— a shell variable assignment and a test-scoped environment deletion — with no
concurrency, shared mutable state, or async ordering involved.

## No-Gos (Out of Scope)

Nothing deferred — every relevant item is in scope for this plan.

## Update System

No update system changes required — this is a developer-tooling and test-local
change with no runtime, dependency, or deployment surface.

## Agent Integration

No agent integration required. The MCP server's tool surface is unchanged; only
the environment the test hands it is corrected.

## Documentation

### Feature Documentation
- [ ] No feature doc — this is a bug fix to a developer script with no user-facing
      behaviour change.

### External Documentation Site
- [ ] Check whether any `docs/` page instructs developers to set or rely on
      `REDIS_URL` when running `scripts/ci-local.sh`; correct it if so.
- [ ] `CLAUDE.md` already documents the DB-0 hazard around `REDIS_URL`; review
      whether the "Verifying in a worktree" section should note that `ci-local.sh`
      no longer supplies one.

### Inline Documentation
- [ ] Comment the local probe variable in `scripts/ci-local.sh` explaining why it
      must not be exported (points at this issue).
- [ ] Extend the test docstring to state that the test clears the variable itself.

## Success Criteria

- [ ] `env -u REDIS_URL scripts/ci-local.sh tests` reports zero failures from
      `tests/test_integrations_mcp.py`.
- [ ] The affected test passes with `REDIS_URL=redis://localhost:6379` set in the
      environment (the contract is enforced by the test, not the caller).
- [ ] `grep -n 'export REDIS_URL' scripts/ci-local.sh` finds nothing.
- [ ] The "Redis not reachable" preflight still fires and exits 2 when Redis is down.
- [ ] Every `REDIS_URL`-reading test file passes with the variable unset.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (script + test)**
  - Name: `ci-local-builder`
  - Role: apply both edits and the inline comments
  - Agent Type: builder
  - Resume: true

- **Validator (env matrix)**
  - Name: `ci-local-validator`
  - Role: run the affected tests with `REDIS_URL` unset, set db-less, and set to an
    explicit DB; confirm the preflight still fails closed
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Fix the script's exported fallback
- **Task ID**: build-script
- **Depends On**: none
- **Validates**: `scripts/ci-local.sh`
- **Assigned To**: ci-local-builder
- **Agent Type**: builder
- **Parallel**: true
- Replace the `REDIS_URL="${REDIS_URL:-...}"` + `export REDIS_URL` pair at
  `scripts/ci-local.sh:53-54` with a non-exported local (e.g. `REDIS_PROBE_URL`)
  defaulting to `${REDIS_URL:-redis://localhost:6379}`.
- Point the reachability probe (`:96-97`) and the status banner (`:333`) at the
  local variable; keep the failure message text and the `exit 2` unchanged.
- Add a comment naming issue #635 and stating the variable must not be exported.

### 2. Enforce the contract in the test
- **Task ID**: build-test
- **Depends On**: none
- **Validates**: `tests/test_integrations_mcp.py`
- **Assigned To**: ci-local-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `monkeypatch.delenv("REDIS_URL", raising=False)` beside the existing
  `POPOTO_MEMORY_URL` delenv, mirroring `tests/test_integrations_cli.py:59-62`.
- Update the docstring: the URL is cleared by the test, not merely assumed unset.

### 3. Validate across the environment matrix
- **Task ID**: validate-matrix
- **Depends On**: build-script, build-test
- **Assigned To**: ci-local-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the affected test with `REDIS_URL` unset, set to `redis://localhost:6379`,
  and set to `redis://localhost:6379/6`. All three must pass.
- Run every `REDIS_URL`-reading test file with the variable unset.
- Confirm the script's Redis preflight still exits 2 against an unreachable URL.

### 4. Documentation
- **Task ID**: document-fix
- **Depends On**: validate-matrix
- **Assigned To**: ci-local-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Grep `docs/` and `CLAUDE.md` for guidance that assumes `ci-local.sh` exports a
  `REDIS_URL`; correct anything stale.

## Verification

All commands run from the worktree root with `POPOTO_TEST_DB=6`.

| Check | Command | Expected |
|-------|---------|----------|
| Test passes with no REDIS_URL | `env -u REDIS_URL POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_integrations_mcp.py -q` | exit code 0 |
| Test passes despite a db-less REDIS_URL | `REDIS_URL=redis://localhost:6379 POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_integrations_mcp.py -k test_an_in_process_client_lists_and_calls_the_tools -q` | exit code 0 |
| Script exports no REDIS_URL | `grep -c '^export REDIS_URL' scripts/ci-local.sh` | match count == 0 |
| Script still probes a URL | `grep -c 'redis-cli -u' scripts/ci-local.sh` | output > 0 |
| Preflight fails closed when Redis is down | `REDIS_URL=redis://localhost:6399 scripts/ci-local.sh tests >/dev/null 2>&1; echo $?` | output contains 2 |
| REDIS_URL-reading suites pass unset | `env -u REDIS_URL POPOTO_TEST_DB=6 .venv/bin/pytest tests/test_pytest_plugin.py tests/test_integrations_cli.py tests/test_integrations_db0_isolation.py tests/test_integrations_service.py -q` | exit code 0 |
| Shell script parses | `bash -n scripts/ci-local.sh` | exit code 0 |
| Black clean | `.venv/bin/black --check src/ tests/` | exit code 0 |
| Ruff clean | `.venv/bin/ruff check src/` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

None. The issue left the fix choice open; the Technical Approach section decides
it with reasoning and rejects the hardcoded-DB option explicitly.
