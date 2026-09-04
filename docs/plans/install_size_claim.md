---
status: Ready
type: bug
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/550
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-04
---

# Install-Size Claim: One Measured Number, One Stated Environment

## Problem

Popoto publishes its install weight as a headline differentiator, and the repo
carries two different numbers for it.

**Current behavior:**

| Number | Where | Wording |
|---|---|---|
| 8.7 MB | `README.md:23` | "3 packages, 8.7 MB of site-packages measured in a clean Python 3.12 venv" |
| 8.7 MB | `docs/index.md:15` | "Three packages, 8.7 MB of site-packages in a clean Python 3.12 venv, no API key" |
| 7.9 MB | `docs/plans/docs_repositioning.md:50` | "popoto installs 3 packages / 7.9 MB / zero keys" |
| 7.9 MB | `docs/plans/docs_repositioning.md:112` | "3-package/7.9 MB/zero-key install vs mem0ai's 32/105 MB" |
| 7.9 MB | `docs/plans/harness_integration.md:165` | "Core install stays at 3 packages, 7.9 MB ... must not regress" |

Three consequences follow. It is a marketing claim on both the README and the
docs hero. It is written as a regression budget in `harness_integration.md`, so
a stale figure can be enforced as a merge gate. And it blocked external copy:
the v1.8.2 announcement draft dropped the install size entirely, keeping only
the package count and zero-key claims that agree everywhere.

The companion competitor figure, mem0ai at "32 packages / 105 MB", is under the
same doubt and was dropped from the same draft.

**Desired outcome:**

One install-size figure appears in the repo. It is a real measurement, its
measurement environment sits beside it including the resolved dependency
versions, and the regression budget in `harness_integration.md` cites the same
number. The mem0ai comparison is either re-measured or removed.

## Freshness Check

**Baseline commit:** `53a65b8d318fe65f47a801d7771e65a6e9f5d566`
**Issue filed at:** 2026-08-10T02:34:50Z
**Disposition:** Unchanged

**File:line references re-verified:**

- `README.md:23` — 8.7 MB claim — still holds, exact line, unchanged since `3199998` (2026-08-07, PR #524)
- `docs/index.md:15` — 8.7 MB claim — still holds, exact line, unchanged since `9200c6b` (2026-08-07, PR #531)
- `docs/plans/docs_repositioning.md:50` — 7.9 MB + mem0ai 32/105 MB — still holds, exact line
- `docs/plans/docs_repositioning.md:112` — 7.9 MB + mem0ai 32/105 MB — still holds, exact line
- `docs/plans/harness_integration.md:151` — the issue cites line 151; the regression-budget sentence is now at **line 165**. Minor line drift, claim intact.

**Cited sibling issues/PRs re-checked:** the issue cites none.

**Commits on main since the issue was filed touching the referenced files:**
`16aa702`, `3a793d6`, `e220b2e`, `845e850`, `31535a3`. None edits an install-size
sentence; they add memory, export/import, and benchmark content. Irrelevant to
the root cause.

**Additional occurrences found by a wider grep** (the issue lists five; these are
adjacent and must not be touched by mistake):

- `README.md:13` — "three packages with no API key", no size figure. Leave alone.
- `docs/index.md:61` — "Three packages and no API key, verified in a clean venv", no size figure. Leave alone.
- `docs/llms.txt:7` — "three packages, no API key", no size figure. Leave alone.
- `docs/plans/harness_integration.md:440`, `:535` — package count and zero-key only, no size figure. Leave alone.

## Prior Art

`gh issue list --state all --search "install size"` returns only #550 itself. No
prior issue or PR has attempted to reconcile these numbers. This is the first
pass. The two figures were each introduced by a separate docs-repositioning PR
in the same week (#524 for the README, #531 for the docs site), which is how they
diverged without either author noticing.

## Spike Results

The measurement was performed at plan time so the plan can carry the number
rather than defer it to the builder. Four clean Python 3.12 venvs, all created
with `python3.12 -m venv` and populated by non-editable `pip install`.

### spike-1: What does `pip install popoto` actually weigh today?

- **Assumption**: "One of 8.7 MB or 7.9 MB is correct and the other is stale."
- **Method**: prototype (clean venv measurement)
- **Finding**: **Neither.** The current figure is **9.0 MB**.
- **Confidence**: high
- **Impact on plan**: both figures get replaced, not one of them.

Environment for the headline number:

| Field | Value |
|---|---|
| Host | macOS, Darwin 25.6.0, arm64, APFS |
| Python | 3.12.13 |
| Command | `python3.12 -m venv v && v/bin/pip install popoto` |
| popoto | 1.8.2 (from PyPI) |
| redis | 8.1.0 |
| msgpack | 1.2.2 |
| Packages | 3 (excluding `pip`, which the venv ships) |
| Method | `du -sk site-packages`, minus `pip/` and `pip-*.dist-info/` |
| Result | **9.0 MB** (9228 KB) |
| Date | 2026-09-04 |

Per-package breakdown, PyPI install, `du -sk`:

| Package | KB |
|---|---|
| redis 8.1.0 | 5876 |
| popoto 1.8.2 | 2888 |
| msgpack 1.2.2 | 332 |
| dist-info (3) | 132 |

### spike-2: Why did the number drift?

- **Assumption**: "The figures diverged because one was measured sloppily."
- **Method**: prototype (pin `redis` to each major line, re-measure)
- **Finding**: `pyproject.toml:47` declares `redis>=4.4.4`, unpinned. redis-py has roughly doubled in size across its 6 → 8 releases, and it dominates the install. Both historical figures were honest measurements taken against different resolutions.
- **Confidence**: high
- **Impact on plan**: the published sentence must name the resolved `redis` version, or it will go stale again on the next redis-py release. This is the real fix, not the arithmetic.

| Resolved `redis` | `redis/` KB | Total site-packages net of pip |
|---|---|---|
| 6.4.0 (`redis<7`) | 2876 | 6.1 MB |
| 7.4.1 (`redis<8`) | 4288 | 7.5 MB |
| 8.1.0 (unpinned, current) | 5876 | 9.0 MB |

The 7.9 MB in the plan docs sits beside the redis-py 7.x measurement. The 8.7 MB
in the shipped docs sits between 7.x and 8.x. Both are consistent with an honest
`du` taken when those resolutions were current. Neither is reproducible now.

### spike-3: Does the local source install match the release?

- **Assumption**: "`pip install /path/to/popoto` is a valid proxy for `pip install popoto`."
- **Method**: prototype (both, side by side)
- **Finding**: It is not. The local non-editable install produces `popoto/` at 4184 KB against the PyPI wheel's 2888 KB, a 1.3 MB gap, because the source-tree install carries files the wheel excludes. Local total net of pip is 10.3 MB against the release's 9.0 MB.
- **Confidence**: high
- **Impact on plan**: the published number must be measured from **PyPI**, since `pip install popoto` is what the README instructs a reader to run. Any future re-measure or CI check must install from the index, not from the checkout.

### spike-4: Is the mem0ai comparison still true?

- **Assumption**: "mem0ai is 32 packages / 105 MB."
- **Method**: prototype (clean venv, `pip install mem0ai`)
- **Finding**: mem0ai **2.0.20** installs **34 packages** (excluding pip) weighing **151 MB** by the same `du` method. The old figure was directionally right and is now understated.
- **Confidence**: high
- **Impact on plan**: the comparison is defensible and gets kept with refreshed numbers, rather than dropped.

Largest contributors in the mem0ai venv: `grpc` 39688 KB, `numpy` 34552 KB,
`openai` 20188 KB, `sqlalchemy` 18800 KB, `posthog` 6400 KB, `qdrant_client`
4992 KB.

### Measurement-method note

`du` reports allocated blocks, which on APFS rounds every file up to 4 KB.
Summed apparent file sizes for the same PyPI venv come to 8.1 MB net of pip.
The plan publishes the `du` figure at 9.0 MB because `du -sh site-packages` is
what a skeptical reader will type to check the claim, and a published number
should match the check. The apparent-size figure is recorded here so a future
re-measure on a different filesystem can tell a method difference from a real
regression.

## Data Flow

Not applicable. This is a documentation-correctness change with no runtime
component.

## Why Previous Fixes Failed

No prior fix was attempted. The divergence arose from two same-week PRs (#524,
#531) each measuring independently and neither cross-checking the other.

**Root cause pattern:** an unpinned dependency floor (`redis>=4.4.4`) under a
published size claim, with no stated resolution beside the number and no check
that would catch drift.

## Architectural Impact

- **New dependencies**: none
- **Interface changes**: none
- **Coupling**: none
- **Data ownership**: none
- **Reversibility**: trivial; the change is five sentences of prose

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 — the measurement is already in this plan and the number is decided
- Review rounds: 1

## Prerequisites

The measurement is complete and recorded above. A builder does **not** need to
re-run it. If a reviewer wants to reproduce it:

| Requirement | Check Command | Purpose |
|---|---|---|
| Python 3.12 | `python3.12 -V` | matches the stated measurement environment |
| Network access to PyPI | `python3.12 -m pip download --no-deps -d /tmp/pd popoto` | the number is measured from the release, not the checkout |

## Solution

### Key Elements

- **One figure, 9.0 MB**: replaces both 8.7 MB and 7.9 MB everywhere they appear.
- **Environment beside the number**: every occurrence names Python 3.12 and the resolved `redis` version, so the next reader can tell a stale claim from a wrong one.
- **Regression budget corrected**: `harness_integration.md` cites 9.0 MB, so the gate it describes enforces a real number.
- **mem0ai comparison refreshed**: 34 packages / 151 MB, measured 2026-09-04, replacing 32 packages / 105 MB.

### Flow

Reader lands on README → reads "3 packages, 9.0 MB of site-packages, Python
3.12 with redis-py 8.1.0" → runs the same two commands → sees 9.0 MB → claim
holds.

### Technical Approach

Five prose edits across four files. No code, no tests, no dependency changes.

1. `README.md:23` — replace `8.7 MB` with `9.0 MB` and extend the environment clause to name the resolved redis-py version and the measurement date.
2. `docs/index.md:15` — same substitution, in the hero's shorter register.
3. `docs/plans/docs_repositioning.md:50` — `7.9 MB` → `9.0 MB`; `32 packages / 105 MB` → `34 packages / 151 MB`; add the environment clause (`Python 3.12`, `redis-py 8.1.0`) and the measurement date.
4. `docs/plans/docs_repositioning.md:112` — same substitutions in the compressed `3-package/…` form, with the same environment clause.
5. `docs/plans/harness_integration.md:165` — `7.9 MB` → `9.0 MB` with the environment clause, and reword the regression budget so it is a budget on the *popoto-attributable* portion. redis-py's growth is outside popoto's control and must not read as a popoto regression.

**Every rewritten figure carries `Python 3.12` and `redis-py 8.1.0` on the same
line.** That is a single uniform rule across all four target files, and it is what
the two environment rows in the Verification table check. It is also the actual fix
from spike-2: a bare number goes stale silently, a number beside its resolution
does not.

**`docs_repositioning.md` is a closed record** (`status: Complete`, shipped in
PR #531; confirmed by `git log -3 -- docs/plans/docs_repositioning.md`, whose most
recent commit is `2b123ed` "Plan complete (docs_repositioning): shipped in PR
#531"). Its two lines are corrected in place rather than left stale, because a
reader of that document has no signal that its numbers are superseded. To avoid
silently falsifying a dated research record, each corrected line gains a short
italic correction note pointing at this plan, which holds the superseded figures
and the re-measurement. The note must **not** restate the old numbers inline, or
the stale-figure greps would match it.

`harness_integration.md` is by contrast `status: Planning` — genuinely in-flight —
so its regression budget is a live gate and is rewritten outright.

On item 5, the sentence today says the whole 3-package total "must not regress."
Under an unpinned `redis>=4.4.4` that is a promise popoto cannot keep, as spike-2
shows. The corrected wording budgets what the `popoto[mcp]` extra adds to the
core install, which is the thing that plan is actually gating.

**Suggested replacement wording for `README.md:23`:**

> That pulls `popoto`, `redis`, and `msgpack`: 3 packages, 9.0 MB of
> site-packages measured 2026-09-04 in a clean Python 3.12 venv resolving
> redis-py 8.1.0. Point it at Redis or Valkey on `localhost:6379` and you are
> running.

**Suggested replacement wording for `docs/index.md:15`:**

> Three packages, 9.0 MB of site-packages in a clean Python 3.12 venv
> (redis-py 8.1.0, measured 2026-09-04), no API key.

**Suggested replacement wording for `docs/plans/docs_repositioning.md:50`:**

> - mem0ai installs 34 packages / 151 MB / requires an OpenAI key; popoto installs
>   3 packages / 9.0 MB / zero keys, verified in a clean venv (Python 3.12,
>   redis-py 8.1.0, mem0ai 2.0.20, measured 2026-09-04). This is an unclaimed
>   differentiator. *(Figures corrected 2026-09-04 per #550; the superseded
>   originals and the re-measurement are recorded in
>   `docs/plans/install_size_claim.md`.)*

**Suggested replacement wording for `docs/plans/docs_repositioning.md:112`:**

> - Promote undersold assets: #489 negative result; 3-package/9.0 MB/zero-key
>   install (Python 3.12, redis-py 8.1.0, measured 2026-09-04) vs mem0ai 2.0.20's
>   34/151 MB/key-required; Valkey support; transparency practice (judge-prompt
>   SHA, environment capture, published negative results).

**Suggested replacement wording for `docs/plans/harness_integration.md:165`:**

> - **New dependencies**: `mcp` (the Python MCP SDK) under a new `popoto[mcp]`
>   extra. Core install stays at 3 packages — 9.0 MB of site-packages on Python
>   3.12 with redis-py 8.1.0, measured 2026-09-04 — and zero API keys. The
>   published differentiator is the package count and the zero-key property; the
>   byte figure tracks whatever the unpinned `redis>=4.4.4` floor resolves to and
>   is not popoto's to hold flat. What must not regress is popoto's own
>   contribution: adding the `mcp` extra must not increase the *core* install's
>   package count, and must not increase the popoto-attributable share of the
>   install size. The hook path deliberately does **not** require `mcp`; hooks
>   work on a bare `pip install popoto`.

### Decision: keep the mem0ai comparison

The team-lead brief said drop it unless re-measured. It was re-measured
(spike-4), so it stays, with 34 packages / 151 MB. Both figures come from the
same host, same day, same `du` method as popoto's, which is the condition that
makes a comparison honest.

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope. This change touches only Markdown prose.

### Empty/Invalid Input Handling
- Not applicable. No functions are added or modified.

### Error State Rendering
- Not applicable. No user-visible runtime output changes.

## Test Impact

No existing tests affected. The change is confined to `README.md`, `docs/index.md`,
and two files under `docs/plans/`. No test in `tests/` asserts on any of these
files, and no packaging or dependency metadata is modified, so no install-time or
import-time behavior can shift.

## Rabbit Holes

- **Pinning an upper bound on `redis`.** The drift is real, but capping `redis<9` in `pyproject.toml:47` to stabilize a marketing number would constrain every downstream user for a docs problem. Out of scope; note it and move on.
- **Automating the measurement in CI.** A workflow that installs from PyPI on every push and fails on a size delta is a defensible idea and a much larger piece of work: it needs a network-dependent job, a tolerance band, and a policy for who unblocks a legitimate upstream growth. Not this plan.
- **Trimming the popoto wheel.** Spike-3 found the source-tree install carries 1.3 MB the wheel does not, which invites an audit of what ships in the wheel. Interesting, unrelated to the claim being wrong, and a separate change with real regression risk.
- **Chasing the exact provenance of 8.7 vs 7.9.** Spike-2 explains the mechanism well enough to fix the docs. Bisecting redis-py releases to reproduce each historical figure to one decimal buys nothing.
- **Restating the size in `docs/llms.txt` or `docs/index.md:61`.** Those lines deliberately claim only the package count. Adding a size figure there creates more surfaces to keep in sync.

## Risks

### Risk 1: The number goes stale again on the next redis-py release
**Impact:** The repo returns to publishing a figure a reader cannot reproduce, which is exactly the state #550 describes.
**Mitigation:** Every occurrence names the resolved redis-py version, so a mismatch is self-diagnosing rather than silently wrong. The rewritten `harness_integration.md` budget no longer treats upstream growth as a popoto regression.

### Risk 2: A partial edit leaves a stale figure behind
**Impact:** Two numbers again, which is the original bug.
**Mitigation:** The Verification table greps the whole tree for `8.7 MB`, `7.9 MB`, `105 MB`, and `32 packages` and requires zero matches, then requires `9.0 MB` to appear in all four target files.

### Risk 3: The reviewer measures on Linux/ext4 and gets a different figure
**Impact:** The claim looks wrong in review.
**Mitigation:** The method note above records both the `du` figure (9.0 MB) and the apparent-size figure (8.1 MB) for the same venv, so a filesystem-driven difference is distinguishable from a real change.

### Risk 4: `harness_integration.md` is an in-flight plan and editing it collides with active work
**Impact:** Merge conflict, or a builder on that plan working from a number this plan is changing underneath them.
**Mitigation:** The edit is one line, line 165. Check `git log` on that file before editing, and if another branch is touching it, coordinate rather than force.

## Race Conditions

No race conditions identified. Every change is a static text edit to a Markdown
file; there is no concurrency, no shared state, and no runtime component.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #550] Nothing is deferred to another issue. Every item this plan touches is completed within it.

Beyond that, the following are deliberate non-actions rather than deferrals, and
each is argued in Rabbit Holes above: no upper bound is added to the `redis`
dependency, no CI size check is created, the wheel contents are not audited, and
the package-count-only sentences at `README.md:13`, `docs/index.md:61`,
`docs/llms.txt:7`, `docs/plans/harness_integration.md:440` and `:535` are left
exactly as they are.

## Update System

No update system changes required. Nothing is deployed or propagated; the docs
site picks the change up on its next `mkdocs` build.

## Agent Integration

No agent integration required. This change adds no callable surface.

## Documentation

This plan *is* a documentation change, so the usual cascade collapses into the
edits themselves.

### Feature Documentation
- Not applicable. No feature is added, so no `docs/features/` page is created.

### External Documentation Site
- [ ] `docs/index.md:15` updated with the measured figure and environment
- [ ] `mkdocs build --strict` passes

### Inline Documentation
- Not applicable. No code is touched.

### Repo Documentation
- [ ] `README.md:23` updated
- [ ] `docs/plans/docs_repositioning.md:50` and `:112` updated, including the mem0ai figures
- [ ] `docs/plans/harness_integration.md:165` updated, with the budget reworded to cover the popoto-attributable delta

## Success Criteria

- [ ] `9.0 MB` is the only install-size figure among the four target claim sites
      (`README.md`, `docs/index.md`, `docs/plans/docs_repositioning.md`,
      `docs/plans/harness_integration.md`). The figures in this plan's Spike
      Results (6.1 / 7.5 / 8.1 / 10.3 MB) are dated measurement records, not
      claims, and are deliberately excluded from that criterion.
- [ ] No occurrence of `8.7 MB`, `7.9 MB`, `105 MB`, or `32 packages` remains in
      the four target files
- [ ] Every occurrence of the figure in those files names Python 3.12 and
      redis-py 8.1.0 on the same line
- [ ] `harness_integration.md`'s regression budget cites 9.0 MB and scopes the
      non-regression promise to popoto's own contribution rather than the
      absolute 3-package total
- [ ] `docs_repositioning.md`, a closed record, carries a dated correction note
      pointing at this plan rather than a silent rewrite
- [ ] The mem0ai comparison reads 34 packages / 151 MB with its measurement date
- [ ] `mkdocs build --strict` passes
- [ ] Documentation cascade run (`/do-docs`, task 4)

## Team Orchestration

### Team Members

Solo dev, matching the Small appetite. The change is five prose edits; a
two-agent documentarian/validator split was declared in the pre-critique draft
and has been collapsed. The builder applies the edits and then runs the
Verification table itself.

- **Builder/validator (install-size copy)**
  - Name: `install-size-scribe`
  - Role: Apply the prose edits across the four files, then run every row of the Verification table
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Update the shipped claims
- **Task ID**: build-shipped-docs
- **Depends On**: none
- **Validates**: no test files; verified by grep in the Verification table
- **Informed By**: spike-1 (9.0 MB, PyPI, redis-py 8.1.0), spike-3 (measure from PyPI, not the checkout)
- **Assigned To**: install-size-scribe
- **Parallel**: true
- Replace `8.7 MB` at `README.md:23` with `9.0 MB` and extend the environment clause per the suggested wording
- Replace `8.7 MB` at `docs/index.md:15` likewise
- Leave `README.md:13`, `docs/index.md:61`, and `docs/llms.txt:7` untouched

### 2. Update the plan-doc claims
- **Task ID**: build-plan-docs
- **Depends On**: none
- **Validates**: no test files; verified by grep in the Verification table
- **Informed By**: spike-2 (the budget must scope to popoto's own delta), spike-4 (mem0ai 34 / 151 MB)
- **Assigned To**: install-size-scribe
- **Parallel**: true
- `docs/plans/docs_repositioning.md:50` and `:112`: apply the suggested wording — `7.9 MB` → `9.0 MB`, `32 packages` → `34 packages`, `105 MB` → `151 MB`, plus the `Python 3.12, redis-py 8.1.0` clause, the 2026-09-04 date, and the correction note pointing at this plan. The note must not restate the superseded numbers.
- `docs/plans/harness_integration.md:165`: apply the suggested wording — `7.9 MB` → `9.0 MB` with the environment clause, and reword so the non-regression promise covers popoto's own contribution rather than the absolute 3-package total
- `git log -3` was run on both files at revision time: `docs_repositioning.md` is `status: Complete` (last commit `2b123ed`, shipped PR #531) and `harness_integration.md` is `status: Planning` (last commit `e220b2e`, shipped PR #546). Neither has an open branch editing the target lines; re-check before editing and coordinate rather than force if that has changed.

### 3. Validate
- **Task ID**: validate-install-size
- **Depends On**: build-shipped-docs, build-plan-docs
- **Assigned To**: install-size-scribe
- **Parallel**: false
- Run every command in the Verification table, reading counted output rather than exit status for the four stale-figure rows
- Confirm the untouched package-count-only lines are byte-identical to `origin/main`
- Report pass/fail

### 4. Documentation cascade
- **Task ID**: docs-cascade
- **Depends On**: validate-install-size
- **Assigned To**: install-size-scribe
- **Parallel**: false
- Run `/do-docs` so the change is checked against the rest of the documentation tree before merge
- This task exists so the "Documentation updated" success criterion maps to a step; the critique flagged that it previously mapped to none

## Verification

The four "no stale figure" rows are scoped to the four target claim sites and
**must not** be run as `grep -r … docs/`: this plan file quotes `8.7 MB`,
`7.9 MB`, `105 MB` and `32 packages` many times as historical record, and nothing
removes that prose, so a recursive grep over `docs/` can never reach zero.

`grep -c` exits 1 when a file has no match, so every row below is judged on
**counted output, never on `$?`**. Run the four stale-figure rows under `set +e`,
or use the `| grep -v ":0$" | wc -l` form given, which exits 0 either way.

Below, `F` stands for the four literal paths
`README.md docs/index.md docs/plans/docs_repositioning.md docs/plans/harness_integration.md`,
and they must be typed out in full in the command actually run. Do **not**
collapse them into a shell variable: this repo's default shell is zsh, which
does not word-split an unquoted parameter expansion, so `grep -c "..." $TARGETS`
treats the whole string as one missing filename and every row reports a false
pass. That was hit for real while validating this change.

| Check | Command | Expected |
|-------|---------|----------|
| No stale 8.7 MB | `grep -c "8\.7 MB" F \| grep -v ":0$" \| wc -l` | output is `0` |
| No stale 7.9 MB | `grep -c "7\.9 MB" F \| grep -v ":0$" \| wc -l` | output is `0` |
| No stale mem0ai size | `grep -c "105 MB" F \| grep -v ":0$" \| wc -l` | output is `0` |
| No stale mem0ai count | `grep -c "32 packages" F \| grep -v ":0$" \| wc -l` | output is `0` |
| README carries the figure | `grep -c "9\.0 MB" README.md` | output > 0 |
| Docs hero carries the figure | `grep -c "9\.0 MB" docs/index.md` | output > 0 |
| Repositioning plan updated | `grep -c "9\.0 MB" docs/plans/docs_repositioning.md` | output is `2` |
| Harness budget updated | `grep -c "9\.0 MB" docs/plans/harness_integration.md` | output > 0 |
| Python version stated beside every figure | `grep -h "9\.0 MB" F \| grep -vc "3\.12" \| cat` | output is `0` |
| redis-py version stated beside every figure | `grep -h "9\.0 MB" F \| grep -vic "redis-py 8\.1\.0" \| cat` | output is `0` |
| mem0ai comparison refreshed | `grep -c "151 MB" docs/plans/docs_repositioning.md` | output is `2` |
| Closed record annotated | `grep -c "install_size_claim.md" docs/plans/docs_repositioning.md` | output > 0 |
| Package-count-only lines untouched | `git diff --stat origin/main -- docs/llms.txt` | empty output |
| Untouched claim lines byte-identical | `git diff origin/main -- README.md docs/index.md \| grep -c "^[-+].*three packages"` | output is `0` |
| No dependency metadata changed | `git diff --exit-code origin/main -- pyproject.toml` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Only the four target files changed | `git diff --name-only $(git merge-base HEAD origin/main)` | exactly the 4 targets plus `docs/plans/install_size_claim.md`. Use the merge-base, not bare `origin/main`: main advances during the run and unrelated new files otherwise appear as deletions. |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

**Critique run:** 2026-09-04, FULL depth (Risk & Robustness, Scope & Value, History & Consistency). **Verdict:** NEEDS REVISION (2 blockers, 3 concerns, 1 nit). **Revision applied** 2026-09-04: all 2 blockers, all 3 concerns and the nit are addressed below. One critique round only, per the routing instruction.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness, History & Consistency | The four "No stale ..." rows in the Verification table grep `README.md docs/`, which recursively includes this plan file. This document quotes `8.7 MB` 16x, `7.9 MB` 14x, `105 MB` 12x and `32 packages` 7x as historical record, and no task removes that prose, so the four checks can never report zero matches even after a perfect edit. The gate is unsatisfiable by construction. | Verification table rewritten: rows scoped to the four target files, never `docs/` recursively; a note above the table states why | Replace the four commands with file-scoped equivalents: `grep -rc "8\.7 MB" README.md docs/index.md docs/plans/docs_repositioning.md docs/plans/harness_integration.md` (repeat for `7\.9 MB`, `105 MB`, `32 packages`), each still expecting match count == 0. Equivalently add `--exclude=install_size_claim.md`. |
| BLOCKER | Risk & Robustness, Scope & Value | Verification rows "Environment stated beside every figure" and "redis-py version stated beside every figure" require `3.12` and `redis` on the same line as every `9.0 MB` occurrence, but Technical Approach items 3-5 and Tasks 1-2 only add the measurement date to the `docs/plans/` lines. `docs_repositioning.md:50`/`:112` and `harness_integration.md:165` contain neither `3.12` nor `redis` today, so following the tasks literally produces lines that fail the plan's own checks. | Technical Approach items 3-5 now require `Python 3.12` and `redis-py 8.1.0` on the same line as every rewritten figure, uniformly across all four files; suggested wording added for each of the three plan-doc lines | Either extend items 3-5 to write the figure as `9.0 MB (Python 3.12, redis-py 8.1.0)` on those three lines, or rescope the two environment-clause Verification rows to `README.md docs/index.md` only. Pick one; do not leave both as written. |
| CONCERN | History & Consistency | Success Criterion "`9.0 MB` is the only install-size figure in the repo for the core install" is contradicted by this plan's own permanent content: spike-2 records 6.1 MB and 7.5 MB, spike-3 records 10.3 MB, and the method note records 8.1 MB. All survive the merge. | Success Criterion 1 rewritten to scope to the four target claim sites and to name the Spike Results figures as excluded historical records | Reword to: "`9.0 MB` is the only install-size figure among the four target claim sites (`README.md`, `docs/index.md`, `docs/plans/docs_repositioning.md`, `docs/plans/harness_integration.md`); the Spike Results figures (6.1/7.5/8.1/10.3 MB) are historical measurement records, not claims, and are excluded." |
| CONCERN | Risk & Robustness | `grep -rc PATTERN <files>` exits status 1 when every file has zero matches, so the four "match count == 0" rows and Task 3's "Run every command in the Verification table" misreport the passing case as a failure under `set -e`. | All four rows restated in the `\| grep -v ":0$" \| wc -l` form with an explicit instruction to judge on counted output, never `$?` | State the expectation as output-based, not exit-code-based: `grep -rc ... \| grep -v ":0$" \| wc -l` expecting `0`, or prefix each row with `set +e`. The validator must read counted output, never `$?`, for these four rows. |
| CONCERN | Scope & Value | The plan overwrites historical figures in `docs/plans/docs_repositioning.md` without stating whether that document is a closed decision record or active work. `harness_integration.md` gets that justification (Risk 4, "in-flight"); `docs_repositioning.md` gets none. | `git log -3` run on both plan docs; `docs_repositioning.md` confirmed `status: Complete` (shipped PR #531). Its lines are corrected in place **and** annotated with a dated correction note pointing at this plan, so the record is not silently falsified. `harness_integration.md` is `status: Planning`, so its budget is rewritten outright | Run `git log -3 -- docs/plans/docs_repositioning.md` (same technique Task 2 already mandates for `harness_integration.md`). If the plan is closed/shipped, append a dated erratum line rather than rewriting the original measurement in place. |
| NIT | Scope & Value | A two-agent Team Orchestration section (documentarian + validator, both `Resume: true`) is declared for what the plan itself calls "five sentences of prose" under a Solo-dev Small appetite. | Collapsed to a single solo-dev role that applies the edits and then runs the Verification table | n/a |

## Open Questions

None. The measurement is done, the number is chosen, the environment is
recorded, and the mem0ai comparison decision is settled by spike-4.
