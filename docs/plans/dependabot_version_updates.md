---
status: Ready
type: chore
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/551
last_comment_id: none
---

# #551 — Add `.github/dependabot.yml` so version updates stop depending on vulnerabilities

## Problem

The repo has no `.github/dependabot.yml`. Confirmed at plan time two ways: `git log --all -- .github/dependabot.yml` returns nothing (the file has never existed in this repository's history), and `gh api repos/tomcounsell/popoto/contents/.github/dependabot.yml` returns 404 against the default branch.

Without that file Dependabot runs in security-only mode. It opens a PR when an advisory names a package in a committed lockfile, and never otherwise. The repo *does* get Dependabot PRs — #388, #425, #478, #590, #599 all landed as `chore(deps): bump ... in the uv group` — but every one of them is a **grouped security update**, which GitHub enables from repository settings rather than from a config file. The group name in those titles is the ecosystem name that grouped security updates assign by default, not evidence of a `groups:` block.

**Current behavior:**

`uv.lock` only moves when a CVE forces it. Between advisories it drifts against `pyproject.toml`'s lower bounds, and the drift is only ever discovered as an alert. Forty-nine alerts have been fixed this way. Four are open right now, and two of them sit in `examples/uv.lock`, which no advisory-driven refresh has ever been able to touch because that lockfile cannot be regenerated at all (see the examples section below).

| State | Count |
|---|---|
| Open alerts | 4 |
| Fixed alerts | 49 |
| Dismissed | 0 |

The four open alerts, from `gh api repos/tomcounsell/popoto/dependabot/alerts?state=open`:

| Severity | Package | Manifest | First patched |
|---|---|---|---|
| medium | setuptools | `uv.lock` | 83.0.0 |
| high | msgpack | `examples/uv.lock` | 1.2.1 |
| low | Pygments | `uv.lock` | 2.20.0 |
| low | Pygments | `examples/uv.lock` | 2.20.0 |

The GitHub Actions used across the five workflows have never been version-checked by anything: `actions/checkout@v4`, `actions/setup-python@v5`, `astral-sh/setup-uv@v5`, `pypa/gh-action-pypi-publish@release/v1`. Action versions are not in any lockfile, so security-only mode gives them no coverage whatsoever.

**Desired outcome:**

A committed `.github/dependabot.yml` that schedules weekly version updates for the root `uv` project and for `github-actions`, groups routine minor and patch bumps into one PR per ecosystem, and leaves major bumps as individual reviewable PRs. Routine drift then arrives as scheduled maintenance instead of accumulating until an advisory converts it into an alert backlog.

## Freshness Check

**Baseline commit:** `53a65b8d318fe65f47a801d7771e65a6e9f5d566` (`git rev-parse main`, local main equals `origin/main` at plan time)
**Issue filed at:** 2026-08-10T02:35:02Z
**Disposition:** Minor drift — the issue's central claim is intact, its alert arithmetic is not.

**Issue claims re-verified:**

- "`.github/dependabot.yml` absent" — **still holds.** `.github/` contains only `workflows/`. Verified against the remote default branch, not just the local checkout.
- "23 open alerts (12 high, 8 medium, 3 low), 21 of them from a stale lockfile" — **drifted.** Four alerts are open today and 49 are fixed. The grouped security PRs #590 (4 updates, 2026-09) and #599 (mkdocs-material 9.7.6 → 9.7.7, merged 2026-09-04, the most recent commit to touch `uv.lock`) closed most of the backlog. The *mechanism* the issue describes is unchanged; only the count is stale. This plan does not repeat the issue's numbers.
- "Every constraint in `pyproject.toml` is a lower bound" — **still holds.** `requires-python = ">=3.10"`, `redis>=4.4.4`, `msgpack>=1.0.4`, and every optional extra is a `>=` floor. No upper caps anywhere in the manifest.
- "`examples/uv.lock` cannot be refreshed until `examples/pyproject.toml` stops declaring `requires-python = ">=3.8"`" — **still holds.** `examples/pyproject.toml` still declares `>=3.8` while depending on `popoto` as an editable path dependency that requires `>=3.10`.
- "Refreshing `examples/uv.lock` jumps popoto 1.0.0b1 → 1.8.2 and textual 0.73 → current" — **still holds and is worse than described.** That lockfile still pins `popoto 1.0.0b1` with `redis>=4.3.4` recorded as popoto's own requirement, and carries three marker-split textual pins (0.73.0, 6.2.1, 7.5.0). Its last commit is `876d8ea` on 2026-02-11, seven months ago.

**Commits on main since the issue was filed, touching referenced files:**

- `2e99b70` bump mkdocs-material in the uv group (#599) — `uv.lock` only. Partially addresses the alert backlog, does not address the cause.
- `1b4748c` bump the uv group with 4 updates (#590) — `uv.lock` only. Same disposition.
- `e220b2e` subconscious memory (#546) — touched `uv.lock` incidentally via a manifest change. Irrelevant to this plan.
- Nothing has touched `examples/` since 2026-02-11.

**Active plans overlapping this area:** none. `ls -lt docs/plans/` shows the recent lanes are all runtime work (`reference_resolution_m4`, `partition_by_canonical_rendering`, `sorted_pushdown_coverage_gaps`, `supersession_membership_guard_in_lua`, `pytest_plugin_db_override_coverage`). No plan touches `.github/`, `pyproject.toml` dependency metadata, or either lockfile.

## Prior Art

- **PR #599** — `chore(deps): bump mkdocs-material in the uv group across 1 directory`, merged 2026-09-04. Grouped security update. Changed `uv.lock` and nothing else.
- **PR #590** — `chore(deps): bump the uv group across 1 directory with 4 updates`. Grouped security update. `uv.lock` only.
- **PR #478** — `chore(deps): bump torch in the uv group`. `uv.lock` only.
- **PR #425** and **PR #388** — same shape, going back to the langchain-core bump. `uv.lock` only, in every case.
- **PR #529 (issue #523)** — added `.github/workflows/lock-check.yml`, which runs `uv lock --check` with uv pinned to `0.12.2`. This is the gate every future Dependabot PR must satisfy, and it is the reason the uv version pin matters below.
- **PR #536** — hand-synced `uv.lock` after the 1.8.2 version bump, a chore that a scheduled version-update cadence makes rarer.
- **PR #579** — added the `black --check` gate. Cited only as precedent for how this repo introduces CI config: one narrowly scoped workflow with a long explanatory header comment.

No prior attempt to add a Dependabot config exists, so there is no "why previous fixes failed" analysis to write.

## Research

**Queries used:**

- Dependabot options reference (`package-ecosystem` values, `groups`, `cooldown`, `open-pull-requests-limit`)
- Dependabot `uv` ecosystem support and `uv.lock` configuration examples
- `versioning-strategy` `lockfile-only` support across ecosystems, specifically `uv`

**Key findings:**

- `package-ecosystem: "uv"` is a first-class ecosystem. It reads `pyproject.toml` and `uv.lock` directly, which is the correct choice here — the `pip` ecosystem would not understand the lockfile. Source: [GitHub Dependabot options reference](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference) and [Astral's uv/Dependabot guide](https://docs.astral.sh/uv/guides/integration/dependabot/).
- **`versioning-strategy` does work for uv**, despite the docs lagging. dependabot-core issue [#12162](https://github.com/dependabot/dependabot-core/issues/12162) was closed 2026-02-13 with a maintainer confirming uv's UpdateChecker inherits from Python's, so `lockfile-only` and the rest route through the shared requirements updater. This is the single most important finding for a *published library*: without it, Dependabot may rewrite `pyproject.toml` lower bounds, which would raise the install floor for every downstream popoto user as a side effect of a routine bump.
- **`directory` does not support globbing.** Each project directory needs its own `updates` entry. `examples/` would be a second entry, never a wildcard. Source: Astral guide and the options reference.
- **Since July 2026 Dependabot applies a default 3-day cooldown** before opening a version-update PR, so a freshly published release is not proposed immediately. `cooldown.default-days` raises it. Security updates bypass cooldown entirely.
- **Security updates bypass `open-pull-requests-limit`** and are not governed by a `groups` block whose `applies-to` is `version-updates`. So the limits and grouping chosen here throttle routine noise without slowing an advisory response.
- **`groups` accepts `applies-to: version-updates | security-updates`, `patterns`, and `update-types`.** Restricting a group to `["minor", "patch"]` leaves majors ungrouped, which produces one PR per major bump — exactly the review granularity a major deserves.

Contradictory reports exist upstream (dependabot-core [#12788](https://github.com/dependabot/dependabot-core/issues/12788) describes PRs that update `uv.lock` without `pyproject.toml` and, separately, the reverse). This repo's own five-PR history is the tiebreaker and is recorded as spike-2 below.

## Spike Results

### spike-1: Is the config genuinely absent, or present-but-unread?
- **Assumption**: "The repo has no Dependabot config, so grouped `uv group` PR titles must come from grouped *security* updates."
- **Method**: code-read
- **Finding**: `git log --oneline --all -- .github/dependabot.yml` is empty and the contents API 404s on the default branch. The file has never existed. Grouped security updates, enabled in repository settings, fully explain the observed PR titles.
- **Confidence**: high
- **Impact on plan**: The issue's premise is correct, and the plan must not claim the new config is what produces grouped PRs — it already gets them for security. What changes is the *scheduled* lane.

### spike-2: Does Dependabot edit `pyproject.toml` in this repo, or only the lockfile?
- **Assumption**: "Dependabot may rewrite `pyproject.toml` lower bounds, which is unacceptable for a published library."
- **Method**: code-read (`git show --stat` on every Dependabot commit in history)
- **Finding**: All five Dependabot commits — `2e99b70`, `1b4748c`, `15b76e7`, `1814af1`, `bc7977c`, plus `a8edf22` — changed `uv.lock` and no other file. Zero `pyproject.toml` edits across the entire history.
- **Confidence**: high for the observed security lane, medium for the scheduled lane (which has never run here, and which is where the default versioning strategy would apply).
- **Impact on plan**: Declare `versioning-strategy: lockfile-only` explicitly rather than relying on the observed behavior continuing. It codifies what already happens and removes the library-floor hazard from the scheduled lane.

### spike-3: Can `examples/uv.lock` be regenerated at all today?
- **Assumption**: "`examples/` can be added to the config now."
- **Method**: code-read of both manifests and the stale lockfile
- **Finding**: No. `examples/pyproject.toml` declares `requires-python = ">=3.8"` and depends on `popoto` via `[tool.uv.sources] popoto = { path = "..", editable = true }`, where popoto declares `requires-python = ">=3.10"`. A resolver asked to satisfy a `>=3.8` floor from a `>=3.10` dependency has no solution for 3.8 and 3.9. The lockfile confirms it froze before that constraint tightened: it records popoto at `1.0.0b1` with `redis>=4.3.4`, a requirement string popoto has not published in many releases.
- **Confidence**: high (static, from the manifests). Not executed: the local uv is `0.6.10` while CI pins `0.12.2`, and the lock-check workflow header documents that a version-skewed uv reports a good lock as stale. Running `uv lock` locally would produce an untrustworthy result and risk writing to a lockfile this plan must not touch.
- **Impact on plan**: `examples/` is deferred. See the decision below.

## The `examples/` decision: defer, with the reason recorded in the config

**Recommendation: defer `examples/` to a follow-up issue. Do not add a second `updates` entry in this PR.**

Including `examples/` now is not a config change. It is a three-part code migration hiding behind one:

1. `examples/pyproject.toml` must move `requires-python` from `>=3.8` to `>=3.10`, matching the library it demonstrates.
2. `examples/uv.lock` must be regenerated from scratch. That single regeneration moves popoto `1.0.0b1` → `1.8.2` (eighteen months of API change), redis 6/7 → 8, and textual `0.73` → current major. Textual's widget, screen, and reactive APIs have broken repeatedly across those majors.
3. Someone must then fix `examples/popoto_kitchen/` — five screens, widgets, `app.py`, `models.py`, `operations.py`, `seed.py` — against both a new Textual major and eighteen months of popoto API drift.

Nothing in CI exercises `examples/`. The tests workflow installs `-e ".[dev,embeddings,benchmark]"` from the root and never enters that directory; the ruff gate explicitly scopes to `src/` and its header names `examples/` as not-yet-clean. So the migration has no automated signal — it needs someone to run the TUI and look at it. That is a Medium appetite of its own and it swamps this Small one.

Deferring costs two open low-and-high alerts in a lockfile for a demo that is not installed by any consumer of the package, and that is already unbuildable on its declared Python floor. Adding the entry now costs a stream of Dependabot PRs against a directory whose lock cannot be resolved, which produces either silent Dependabot job failures or a first PR that breaks the demo with nobody watching.

The config file records the deferral in a comment so the next reader does not re-derive this. The follow-up issue is filed as task 1 of this plan, so the deferral is tracked rather than promised.

## Architectural Impact

- **New dependencies**: none. No Python package, no service, no key.
- **Interface changes**: none. No source file changes.
- **Coupling**: adds a scheduled external actor that opens PRs against `uv.lock` and the workflow files. Those PRs must pass `lock-check`, `tests` (both the Redis and Valkey jobs), and `lint`, all of which already run on every pull request.
- **Data ownership**: unchanged.
- **Reversibility**: total. Deleting the file returns the repo to security-only mode with no residue.

## Appetite

**Size:** Small

**Team:** Solo dev, plus one validator pass

**Interactions:**
- PM check-ins: 0 — the one judgment call, `examples/` in or out, is decided in this plan.
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| PyYAML available | `python3 -c "import yaml"` | The YAML-validity verification row parses the config |
| `gh` authenticated with repo scope | `gh auth status` | Post-merge confirmation that Dependabot picked the config up |

`gh` is already authenticated in this environment with `repo` scope. PyYAML arrives with the `[docs]` extra; if the active environment lacks it, install it before running verification rather than skipping the row.

## Solution

### Key Elements

- **`.github/dependabot.yml`** — the only new file. Two `updates` entries, one for the root `uv` project and one for `github-actions`.
- **Grouping** — one group per ecosystem, scoped to `version-updates` and to `minor`/`patch`. Majors stay ungrouped and arrive one PR at a time.
- **Lockfile-only versioning** — the scheduled lane updates `uv.lock` and never rewrites `pyproject.toml`'s lower bounds, protecting the published install floor.
- **Throttle** — three open PRs per ecosystem, weekly, with a 7-day cooldown. Security updates ignore all three.
- **An explanatory header** — matching this repo's workflow-file convention, recording why `examples/` is absent and why `versioning-strategy` is pinned.

### Flow

Monday 06:00 UTC → Dependabot resolves `pyproject.toml` + `uv.lock` → one grouped PR titled `chore(deps): bump the uv-minor-patch group` → `lock-check`, `tests (Redis)`, `tests (Valkey)`, `lint` run → human merges or closes → next Monday.

### Technical Approach

The complete intended file. The builder may reformat comments but must not change the semantics of any key.

```yaml
# Dependabot version updates.
#
# Without this file Dependabot runs in security-only mode: it opens a PR when an
# advisory names a package in a committed lockfile, and never otherwise (#551).
# uv.lock then drifts against pyproject.toml between CVEs, and the drift only
# ever surfaces as an alert backlog -- 49 alerts have been closed that way.
#
# Grouped security updates are enabled separately in repository settings. They
# bypass both `cooldown` and `open-pull-requests-limit` and are unaffected by the
# `applies-to: version-updates` groups below, so nothing here slows an advisory
# response.
#
# examples/ is deliberately absent. examples/pyproject.toml declares
# requires-python ">=3.8" while depending on popoto (">=3.10") as an editable
# path dependency, so examples/uv.lock cannot be regenerated at all. Fixing that
# forces a from-scratch relock that jumps popoto 1.0.0b1 -> 1.8.2 and textual
# 0.73 -> current major, which breaks examples/popoto_kitchen/ -- and nothing in
# CI exercises that directory, so the migration has no automated signal. Tracked
# separately; see the follow-up issue linked from #551.

version: 2

updates:
  # Root Python project. `uv` (not `pip`) is the correct ecosystem: it reads
  # pyproject.toml and uv.lock directly.
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "06:00"
      timezone: "Etc/UTC"

    # popoto is a published library. Every dependency constraint in
    # pyproject.toml is a lower bound, and raising one raises the install floor
    # for every downstream consumer. lockfile-only keeps scheduled updates
    # inside uv.lock, which is what all six historical Dependabot commits here
    # already did. Support for this key on the uv ecosystem was confirmed in
    # dependabot-core#12162 (closed 2026-02-13) even though the reference docs
    # still omit it; if Dependabot ever rejects the key, delete these two lines
    # rather than switching ecosystems.
    versioning-strategy: "lockfile-only"

    open-pull-requests-limit: 3
    commit-message:
      prefix: "chore(deps)"

    # Routine minor/patch churn lands as one PR a week. Majors stay ungrouped so
    # each arrives on its own branch and gets its own review.
    groups:
      uv-minor-patch:
        applies-to: version-updates
        patterns:
          - "*"
        update-types:
          - "minor"
          - "patch"

    # Dependabot's own default is 3 days as of July 2026. A week gives the
    # ecosystem longer to flag a bad or malicious release before it reaches a
    # PR here. Security updates are exempt.
    cooldown:
      default-days: 7

  # Workflow action versions live in .github/workflows/*.yml, not in any
  # lockfile, so security-only mode gave them no coverage at all. checkout@v4,
  # setup-python@v5, setup-uv@v5 and gh-action-pypi-publish@release/v1 have
  # never been version-checked by anything.
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "06:00"
      timezone: "Etc/UTC"
    open-pull-requests-limit: 3
    commit-message:
      prefix: "chore(ci)"
    groups:
      github-actions:
        applies-to: version-updates
        patterns:
          - "*"
        update-types:
          - "minor"
          - "patch"
```

Three deliberate choices worth naming:

- **No lockfile refresh in this PR.** Adding the config and refreshing `uv.lock` are separable, and separating them is what makes the PR reviewable. A `uv lock --upgrade` here would move dozens of transitive pins in a diff nobody can read, must be produced with uv `0.12.2` to match `lock-check` (the local uv is `0.6.10`, and the workflow header documents that a version-skewed uv rewrites the lock in an older revision format and reports a good lock as stale), and would have to survive both the Redis and Valkey test jobs on unrelated grounds. The refresh is also unnecessary: once this config merges, the first scheduled run produces exactly that upgrade as its own reviewable PR, with a changelog per package. The two open root-lockfile alerts (setuptools, Pygments) are both in dev/docs tooling and neither is reachable from installed library code.
- **No `prefix-development`.** `commit-message.prefix-development` is not reliably honored across ecosystems and an unrecognized key risks a config-validation error for no benefit. One prefix per ecosystem.
- **`day` and `time` pinned.** Unpinned weekly scheduling drifts to whatever hour GitHub assigns. A fixed Monday morning slot makes the PRs predictable to triage.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope. This plan adds one YAML config file and touches no Python.

### Empty/Invalid Input Handling
The failure mode for a config file is a schema rejection, not a runtime exception. Two guards: the YAML-validity row in the Verification table catches malformed YAML before merge, and the post-merge check below catches a syntactically valid file that Dependabot's schema rejects. A rejected config leaves the repo exactly where it is today — security-only mode — so the failure is visible and inert rather than silent and destructive.

### Error State Rendering
No user-visible output. Dependabot surfaces its own parse errors in the repository's Dependabot job logs.

## Test Impact

No existing tests affected. Nothing under `tests/` reads `.github/`, no workflow validates the config directory's contents, and this plan adds no importable code. Adding a Python test that asserts the presence or shape of a GitHub config file would test GitHub's scheduler, not popoto, and is explicitly out of scope.

## Data Flow

Not applicable. There is no runtime data path — the artifact is a config file consumed by GitHub's scheduler, entirely outside the library.

## Rabbit Holes

- **Refreshing either lockfile in this PR.** Argued against above. The scheduled run produces the same result, reviewably.
- **The `examples/` Textual migration.** Three coupled majors and a TUI with no automated coverage. Its own issue.
- **Auto-merge automation for Dependabot PRs.** Tempting once the PRs start arriving weekly, but it needs a separate workflow, a permissions review, and a policy on which update types may merge unattended. Not part of adding a config file.
- **`ignore` rules to suppress heavy packages** (torch has produced its own PRs before). Write these only after observing a few weeks of real cadence. Guessing at them now bakes in noise suppression for churn that may not recur, and an `ignore` entry naming only a dependency silently blocks its *security* PRs too.
- **Switching to Renovate.** A genuinely different tool with a genuinely larger config surface. The issue asks for a Dependabot file.
- **Widening the ruff or black gate to `examples/`.** Adjacent, tempting while looking at that directory, and unrelated.

## Risks

### Risk 1: Dependabot rejects `versioning-strategy` on the uv ecosystem
**Impact:** The whole config fails schema validation, and the repo silently stays in security-only mode — the exact state this plan is meant to leave. The failure is invisible from the PR; it only appears in the Dependabot job log.
**Mitigation:** The post-merge check below is mandatory, not optional, precisely because of this risk. If the log shows a validation error, delete the two `versioning-strategy` lines. Fallback behavior is acceptable: all six historical Dependabot commits touched `uv.lock` only, so the observed default already matches lockfile-only. The comment in the file tells the next person to do exactly this.

### Risk 2: Weekly grouped PRs consume real CI budget
**Impact:** Every PR triggers `tests.yml`, which is two jobs at a 25-minute timeout each (Redis and Valkey), plus `lint` and `lock-check`. Two ecosystems at one grouped PR per week is a predictable but nonzero standing cost.
**Mitigation:** `open-pull-requests-limit: 3` caps concurrency per ecosystem, grouping collapses minor/patch churn into one PR rather than one per package, and the 7-day cooldown suppresses same-week re-bumps of a package that ships twice. If the cadence still proves noisy, move `interval` to `"monthly"` — a one-word change.

### Risk 3: The first scheduled run is large
**Impact:** With months of accumulated drift, the first grouped PR may move a lot of transitive pins at once, which is hard to review and more likely to trip a test.
**Mitigation:** Expected and acceptable. It is one PR, it runs the full suite on both Redis and Valkey before anyone merges it, and it is closable with no consequence — the next week's run simply reproposes it. This is strictly better than the status quo, where the same drift accumulates invisibly until an advisory names it.

### Risk 4: `lock-check` fails on a Dependabot PR because of uv version skew
**Impact:** A Dependabot PR shows red for a reason unrelated to the dependency it bumps, and a reviewer wastes a round on it.
**Mitigation:** Not introduced by this change — `lock-check.yml` already pins uv to `0.12.2` for exactly this reason, and its header documents the failure mode. Worth knowing when triaging the first few PRs: if `lock-check` fails while the diff looks sane, compare the lock's `revision` field against what `0.12.2` writes before suspecting the dependency.

## Race Conditions

No race conditions identified. The change adds a declarative config file consumed by an external scheduler. There is no shared mutable state, no concurrency, and no ordering requirement inside this repository.

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG #TBD]` **Adding an `examples/` entry to the config, fixing `examples/pyproject.toml`'s `requires-python`, and regenerating `examples/uv.lock`.** Deferred for the reasons argued above: it is a Textual-plus-popoto API migration against a TUI with zero CI coverage, not a config change. Task 1 of this plan files the tracking issue and substitutes the real number for `#TBD` in this line; the plan must not merge with the placeholder intact.
- `[SEPARATE-SLUG #TBD]` **Refreshing the root `uv.lock` via `uv lock --upgrade`.** The first scheduled Dependabot run produces this as its own reviewable PR with per-package changelogs. Folding it in here makes the config change unreviewable and requires uv `0.12.2` locally to match the CI gate. Covered by the same follow-up issue as the entry above, which records both deferrals.
- `[EXTERNAL]` **Confirming that grouped security updates remain enabled in repository settings.** That toggle lives in the GitHub web UI under Settings → Code security, is not expressible in `dependabot.yml`, and requires admin access. Nothing in this plan changes it; it is named so a future reader does not mistake this file for the source of the existing grouped security PRs.
- `[ORDERED]` **Verifying Dependabot parsed the config.** Dependabot only evaluates the file on the default branch, so this check cannot run until the PR merges. It is listed under post-merge verification below and is a merge follow-through, not a pre-merge gate.

## Update System

No update-system changes required. The file is consumed by GitHub, not by any deployed popoto installation, and it ships in no wheel or sdist.

## Agent Integration

No agent integration required. Nothing here is reachable from an MCP surface or a hook, and no tool needs to expose it.

## Documentation

The docs site has no contributing or dependency-policy page. Its "Contributing" nav entry points at `docs/style-guide.md`, which is a *documentation* style guide — tone, headings, canonical example models — and is the wrong home for a dependency policy. Creating a new contributing page and wiring it into `mkdocs.yml` nav is a larger change than this Small appetite supports, and `mkdocs build --strict` gates the nav.

Documentation therefore lands in the two places this repo already keeps CI knowledge:

### Inline Documentation
- [ ] The header comment in `.github/dependabot.yml`, as drafted in the Technical Approach. Every other workflow in `.github/workflows/` carries a header explaining why it exists; this file follows that convention, and it is where the `examples/` deferral and the `versioning-strategy` fallback are recorded.

### Repository Guidance
- [ ] Add a short note to `CLAUDE.md` under the Commands section recording that dependency updates arrive weekly as grouped Dependabot PRs, that the scheduled lane is lockfile-only so `pyproject.toml` floors are never machine-edited, and that `examples/` is excluded. Three or four lines, no more.

### Feature Documentation
Not applicable. This is CI configuration, not a library feature, so it gets no `docs/features/` page and no index entry.

### External Documentation Site
No page changes. `mkdocs build --strict` must still pass, which it will, since no doc under `docs/` changes.

## Success Criteria

- [ ] `.github/dependabot.yml` exists, parses as YAML, and declares exactly two ecosystems: `uv` at `/` and `github-actions` at `/`.
- [ ] The `uv` entry declares `versioning-strategy: "lockfile-only"`.
- [ ] Both entries declare a group scoped to `applies-to: version-updates` with `update-types: [minor, patch]`.
- [ ] No `directory:` key in the file names `examples`.
- [ ] Neither `uv.lock` nor `examples/uv.lock` appears in the PR diff.
- [ ] Neither `pyproject.toml` nor `examples/pyproject.toml` appears in the PR diff.
- [ ] The follow-up issue for `examples/` plus the root lockfile refresh is filed, and its number replaces both `#TBD` placeholders in the No-Gos section.
- [ ] `CLAUDE.md` records the dependency-update policy.
- [ ] Tests pass (`/do-test`) — unchanged by this PR, but the gate runs.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (dependabot-config)**
  - Name: `dependabot-builder`
  - Role: File the follow-up issue, write the config file and the CLAUDE.md note.
  - Agent Type: builder
  - Resume: true

- **Validator (dependabot-config)**
  - Name: `dependabot-validator`
  - Role: Run every Verification row, confirm the PR diff contains no lockfile or manifest, confirm no `#TBD` placeholder survives.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. File the `examples/` follow-up issue
- **Task ID**: file-followup-issue
- **Depends On**: none
- **Assigned To**: dependabot-builder
- **Agent Type**: builder
- **Parallel**: false
- Open an issue titled `examples/: unlock the demo project so Dependabot can cover it` describing all three coupled pieces — `requires-python` `>=3.8` → `>=3.10`, the from-scratch `examples/uv.lock` regeneration (popoto `1.0.0b1` → `1.8.2`, redis 6/7 → 8, textual `0.73` → current major), and the resulting `examples/popoto_kitchen/` source migration.
- Record that two open alerts (msgpack high, Pygments low) live in `examples/uv.lock` and stay open until this lands.
- Note that nothing in CI exercises `examples/`, so the migration needs a manual TUI run as its acceptance signal.
- Mention the root `uv.lock --upgrade` refresh as covered by the same issue, or the first scheduled Dependabot PR, whichever comes first.
- Reference #551. Do **not** use a closing keyword.
- Replace both `#TBD` placeholders in this plan's No-Gos section with the new issue number, and commit that edit with the rest of the work.

### 2. Write `.github/dependabot.yml`
- **Task ID**: build-config
- **Depends On**: file-followup-issue
- **Validates**: the Verification table below
- **Informed By**: spike-2 (every historical Dependabot commit touched `uv.lock` only), spike-3 (`examples/` is unresolvable today)
- **Assigned To**: dependabot-builder
- **Agent Type**: builder
- **Parallel**: false
- Create the file exactly as drafted in Technical Approach, including the header comment.
- Substitute the real follow-up issue number into the header comment's closing line.
- Change no other file except `CLAUDE.md` in task 3. Specifically: do not run `uv lock`, do not touch either lockfile, do not touch either `pyproject.toml`.

### 3. Record the policy in `CLAUDE.md`
- **Task ID**: document-policy
- **Depends On**: build-config
- **Assigned To**: dependabot-builder
- **Agent Type**: builder
- **Parallel**: false
- Add three or four lines under the Commands section: weekly grouped Dependabot PRs, scheduled lane is lockfile-only so `pyproject.toml` floors are never machine-edited, `examples/` excluded with a pointer to the follow-up issue.
- Do not restate the config file's contents. Point at it.

### 4. Validate
- **Task ID**: validate-all
- **Depends On**: file-followup-issue, build-config, document-policy
- **Assigned To**: dependabot-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every row in the Verification table and report each result individually.
- Confirm the PR diff contains exactly two files: `.github/dependabot.yml` and `CLAUDE.md`, plus this plan document's `#TBD` edit.
- Confirm no `#TBD` string survives in this plan document.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Config file exists | `test -f .github/dependabot.yml` | exit code 0 |
| YAML is valid | `python3 -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))"` | exit code 0 |
| Config version is 2 | `python3 -c "import yaml; print(yaml.safe_load(open('.github/dependabot.yml'))['version'])"` | output contains 2 |
| Exactly two ecosystems | `python3 -c "import yaml; print(len(yaml.safe_load(open('.github/dependabot.yml'))['updates']))"` | output contains 2 |
| uv ecosystem declared | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(sorted(u['package-ecosystem'] for u in d['updates']))"` | output contains uv |
| github-actions ecosystem declared | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(sorted(u['package-ecosystem'] for u in d['updates']))"` | output contains github-actions |
| Lockfile-only versioning on uv | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print([u.get('versioning-strategy') for u in d['updates'] if u['package-ecosystem']=='uv'])"` | output contains lockfile-only |
| Every entry is grouped | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(all(u.get('groups') for u in d['updates']))"` | output contains True |
| Groups scoped to version updates | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(all(g.get('applies-to')=='version-updates' for u in d['updates'] for g in u['groups'].values()))"` | output contains True |
| Open-PR limit set on every entry | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(all(isinstance(u.get('open-pull-requests-limit'), int) for u in d['updates']))"` | output contains True |
| No examples/ directory entry (anti-criterion) | `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(sum('examples' in str(u.get('directory','')) for u in d['updates']))"` | match count == 0 |
| No lockfile churn in this PR (anti-criterion) | `git diff --name-only origin/main...HEAD \| grep -c 'uv\.lock'` | match count == 0 |
| No manifest churn in this PR (anti-criterion) | `git diff --name-only origin/main...HEAD \| grep -c 'pyproject\.toml'` | match count == 0 |
| No unresolved issue placeholder (anti-criterion) | `grep -c '#TBD' docs/plans/dependabot_version_updates.md` | match count == 0 |
| CLAUDE.md records the policy | `grep -ci 'dependabot' CLAUDE.md` | output > 0 |
| Docs still build strict | `mkdocs build --strict` | exit code 0 |

**Post-merge only — not runnable before the PR lands.** Dependabot evaluates `dependabot.yml` on the default branch, so none of these can gate the PR:

- Confirm the file is on the default branch: `gh api repos/tomcounsell/popoto/contents/.github/dependabot.yml` returns 200 rather than the 404 recorded at plan time.
- Open the repository's Insights → Dependency graph → Dependabot tab and confirm both ecosystems are listed with a last-checked timestamp and no config error. A schema rejection appears here and nowhere else. This is the check that catches Risk 1.
- Within one week, confirm a scheduled PR appears whose title carries the `chore(deps)` or `chore(ci)` prefix and the group name — distinguishable from a grouped *security* PR, which is titled `bump ... in the uv group`.
- If the Dependabot tab reports a validation error naming `versioning-strategy`, delete those two lines in a follow-up PR and re-check. Do not switch the ecosystem to `pip`.

## Critique Results

<!-- Populated by /do-plan-critique (war room), FULL depth, round 1 of 1, 2026-09-04. -->

**Verdict: NEEDS REVISION** — 1 blocker, 3 concerns, 2 nits. Critics: Risk & Robustness, Scope & Value, History & Consistency, plus structural checks.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Structural | The `#TBD` anti-criterion can never pass. Eight lines contain the literal `#TBD`, but only the two `[SEPARATE-SLUG #TBD]` prefixes (No-Gos, lines 329-330) are real placeholders; the other six (lines 329-tail, 368, 385, 402, 432, 433, 452) are the plan's own instructions *about* the placeholders and survive substitution. So `grep -c '#TBD' <plan>` returns 6, not 0, and task 4's "Confirm no `#TBD` string survives" is unsatisfiable — the validator either fails a correct build or mangles the plan's instruction text to force the gate green. | Verification table row "No unresolved issue placeholder"; task 4 bullet 3; Success Criterion 7 | Narrow the pattern to the actual placeholder token, not the bare string. Replace the row's command with `grep -c '\[SEPARATE-SLUG #TBD\]' docs/plans/dependabot_version_updates.md \|\| true` expecting `0`, and reword task 4 bullet 3 and Success Criterion 7 to "no `[SEPARATE-SLUG #TBD]` placeholder survives in the No-Gos section". Do not delete the meta-references — they are the instructions that make the substitution auditable. |
| CONCERN | History & Consistency | Cooldown is asymmetric between ecosystems but the prose says it is uniform. Key Elements (line 171) states the throttle is "three open PRs per ecosystem, weekly, with a 7-day cooldown", and Risk 2's mitigation repeats it, but the drafted YAML attaches `cooldown: default-days: 7` only to the `uv` entry. The `github-actions` entry silently falls back to GitHub's own 3-day default that the Research section documents. | Solution > Key Elements; Technical Approach YAML; Risk 2 | Pick one and make it explicit. Either add the identical four-line `cooldown:\n      default-days: 7` block to the `github-actions` entry (indented to the same level as its `open-pull-requests-limit`), or scope the prose to "7-day cooldown on the uv entry; github-actions uses Dependabot's 3-day default". Either way add a Verification row: `python3 -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print([bool(u.get('cooldown')) for u in d['updates']])"` — no row currently checks cooldown at all, so this gap would merge undetected. |
| CONCERN | Risk & Robustness | Three anti-criterion Verification rows use `grep -c` and expect a count of 0, but `grep -c` exits 1 when it matches nothing. The desired state therefore reports a non-zero exit, so a validator (or any `set -e` wrapper) reads success as failure. Confirmed locally: `echo foo \| grep -c bar` prints `0` and exits `1`. | Verification table rows "No lockfile churn", "No manifest churn", "No unresolved issue placeholder" | Append `\|\| true` to each of the three `grep -c` commands so the pipeline exits 0, and add a sentence under the table telling task 4's validator to compare **stdout** against `0`, never the exit status. This repo already uses that guard — see the `\|\| true` in `.github/workflows/guard-main-push.yml`. |
| CONCERN | Risk & Robustness | The plan twice claims `lint` runs on every Dependabot PR — Flow (line 176) lists "`lock-check`, `tests (Redis)`, `tests (Valkey)`, `lint`", and Risk 2's impact assessment repeats it. It will not. `.github/workflows/lint.yml` filters `pull_request` to `src/**`, `tests/**`, `pyproject.toml`, `.github/workflows/lint.yml`; a `lockfile-only` PR touches `uv.lock` alone and triggers neither ruff nor black. Risk 2's CI-budget arithmetic is correspondingly overstated. | Solution > Flow; Risk 2 | Correct the prose rather than the workflow: a uv PR runs `lock-check` + `tests (Redis)` + `tests (Valkey)`; a github-actions PR runs `tests` (and `lint`/`lock-check` only when it happens to bump an action inside `lint.yml` or `lock-check.yml`, which are in their own path filters). No merge deadlock results — `gh api repos/tomcounsell/popoto/branches/main/protection` returns 404, so `main` has no required-status-check protection and an unrun check cannot block. Do not widen `lint.yml`'s path filters to chase this. |
| NIT | Scope & Value | Five of the ~15 top-level sections (Race Conditions, Update System, Agent Integration, Data Flow, Failure Path Test Strategy) are boilerplate "not applicable" restatements, so 468 lines carry perhaps 80 lines of decision for a Small single-file chore. The two sections that hold real judgment — the `examples/` deferral and the `versioning-strategy` risk — are diluted by the ceremony around them. | Overall plan structure | Template-conformance observation, not a build blocker. Raise with whoever owns the plan template for Small CI-config chores rather than editing this plan. |
| NIT | Scope & Value | The issue's suggested fix asks only for a `uv` entry (root, then `examples/`) and never mentions GitHub Actions, but the plan adds a full second `package-ecosystem: "github-actions"` block. It is well justified in the Problem section — action versions sit in no lockfile and get zero security-mode coverage — but a reviewer diffing plan against issue may read it as unrequested scope. | Technical Approach — `github-actions` entry | Add half a sentence to the Problem or Solution noting this is a deliberate widening beyond the issue's literal suggested fix. Same block shape as the uv entry, negligible cost — an expectation-setting note, not something to cut. |
