---
status: Planning
type: bug
appetite: Small
owner: Dev (SDLC lane sdlc-678)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/678
last_comment_id:
---

# setuptools build floor sits ~40 majors below the patched version, and nothing checks what reaches PyPI

## Problem

`pyproject.toml:1-3` declares the build backend as:

```toml
[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"
```

A Dependabot security alert flagged setuptools for a **MANIFEST.in exclusion
bypass in sdist via Unicode normalization collision (NFC/NFD) on macOS
APFS/HFS+** — moderate severity, first patched in **83.0.0**. A path excluded by
`MANIFEST.in` can still be packaged into the sdist when its filename differs
only by normalization form, because APFS and HFS+ normalize filenames and the
exclusion rule silently matches nothing.

The declared floor admits every vulnerable version and then some. That much the
issue got right.

**What the issue got wrong is the exposure.** The advisory needs three
preconditions to bite, and popoto has none of them (Spike Results below):

1. a `MANIFEST.in` with exclusion rules — popoto has no `MANIFEST.in` at all;
2. a path whose filename has two normalization forms — every path in the
   repository and in the published sdist is pure ASCII;
3. a build on APFS/HFS+ — releases are built on `ubuntu-latest` by
   `.github/workflows/release.yml`, not on the maintainer's macOS box.

So this is not a live exposure being remediated. It is a floor that is wrong on
its own terms, sitting under a release process that has no assertion about what
it ships. Both are worth fixing; neither is urgent, and the plan should not
pretend otherwise.

The second half is the part with durable value. Each of the three preconditions
above is absent *today* and could return one at a time — a `MANIFEST.in` added
for a legitimate reason, a fixture filename with an accented character, a
release cut by hand on the macOS machine during a CI outage — and **none of them
would announce itself**. Nothing in the repository asserts anything about sdist
contents. The 1.9.0 tarball is clean, and we know that only because this recon
went and looked.

## Freshness Check

**Disposition: Unchanged**, with the caveat that the issue's own premises were
never verified before filing (the body says so explicitly: *"I have not done
these checks"*). Baseline commit: `408a43ef`.

- Issue #678 filed 2026-09-07, planned same day.
- `pyproject.toml:1-3` re-read at `408a43ef`: `requires = ["setuptools>=42", "wheel"]`,
  unchanged from what the issue quotes.
- `git log --since` over `pyproject.toml` and `.github/workflows/release.yml`
  since the issue was filed: only #670's `anthropic` floor change
  (`11e891dc`), which touches `[project.optional-dependencies]` and not
  `[build-system]`.
- No sibling issues or PRs are cited in the issue body.
- No active plan in `docs/plans/` touches packaging or the release workflow.
- This is a bug issue, so the defect was confirmed present rather than assumed:
  the floor is still `>=42`, and no sdist-contents check exists anywhere in the
  tree.

## Research

- **The advisory itself** — GHSA for setuptools MANIFEST.in exclusion bypass via
  Unicode normalization collision. Platform-scoped to macOS APFS/HFS+, which
  normalize filenames; the same `MANIFEST.in` on ext4 behaves correctly. Impact
  is *files reaching the sdist that were meant to be excluded* — not code
  execution.
- **setuptools release metadata** (PyPI JSON API, read 2026-09-07):

  | version | published | requires-python |
  |---|---|---|
  | 82.0.0 | 2026-02-08 | >=3.9 |
  | 82.0.1 | 2026-03-09 | >=3.9 |
  | **83.0.0** | **2026-07-04** | **>=3.10** |
  | 84.0.0 | 2026-08-08 | >=3.10 |

  83.0.0 is the first patched release and raises `requires-python` to `>=3.10`,
  which is already popoto's own floor (`setup.cfg` `python_requires = >=3.10`).
  Pinning the build requirement at 83 therefore excludes no interpreter popoto
  supports. 84.0.0 exists but the evidence supports 83, not latest.
- **Default sdist contents without a `MANIFEST.in`** — setuptools falls back to
  the distutils defaults: package sources implied by `packages`/`package_dir`,
  the setup/config files, the README named by `long_description`, `LICENSE`, the
  generated `*.egg-info`, and the legacy `test*.py` glob. That last one explains
  a finding below that would otherwise look deliberate.

## Spike Results

Four spikes, all resolved. None left an open question for a human.

### spike-1: does popoto have a `MANIFEST.in` at all?

- **Assumption**: "the exclusion rules exist and some of them matter"
- **Method**: code-read
- **Result**: **No `MANIFEST.in` exists.** Not at the repo root, not anywhere in
  `git ls-files`, and `git grep MANIFEST.in` returns nothing — no doc, script,
  or workflow so much as mentions one. Sdist membership is decided entirely by
  the setuptools defaults plus `setup.cfg`'s `[options] packages = find: /
  where = src`; there is no `[tool.setuptools]` section in `pyproject.toml`
  either.
- **Confidence**: high
- **Impact if false**: would reopen AC1 as a real question. It is not false.

An exclusion bypass with zero exclusion rules has nothing to bypass. This alone
drives the practical exposure to nil.

### spike-2: does any path have a non-ASCII character?

- **Assumption**: "some filename somewhere could have two normalization forms"
- **Method**: code-read
- **Result**: **No.** `git ls-files | grep -P '[^\x00-\x7F]'` returns no matches
  across the whole repository, and the same scan over all 264 members of the
  published `popoto-1.9.0.tar.gz` returns no matches either.
- **Confidence**: high
- **Impact if false**: a single non-ASCII path would make precondition 2 live —
  though still inert without preconditions 1 and 3.

A filename with one normalization form cannot collide with itself. This is the
check the issue itself named as the one that would settle severity, and it
settles it at zero.

### spike-3: where are releases actually built?

- **Assumption**: "releases are cut on macOS — the only platform the advisory
  applies to" (the issue's premise 3, and the one that moved it from noise to
  action)
- **Method**: code-read + artifact forensics
- **Result**: **False. Releases are built on `ubuntu-latest`.**
  `.github/workflows/release.yml` triggers on `v*`/`popoto-v*` tags, runs
  `python -m build` on `ubuntu-latest`, and publishes via
  `pypa/gh-action-pypi-publish` with OIDC. `gh run list --workflow release.yml`
  shows a successful run for every release back through v1.7.0, including
  *"Bump version to 1.9.0"* on 2026-09-05 — matching the 1.9.0 upload timestamp.

  The published artifact corroborates this independently of the workflow file:
  every member of `popoto-1.9.0.tar.gz` is owned `runner/runner`, the GitHub
  Actions account, not a developer uid. On the runner's ext4 filesystem the
  advisory is inert.
- **Confidence**: high
- **Impact if false**: precondition 3 would be live. The residual path — a
  maintainer running `python -m build` by hand on macOS during a CI outage — is
  real but unevidenced in the last five releases, and preconditions 1 and 2
  would still block it.

### spike-4: does `uv.lock`'s `setuptools 81.0.0` describe the build environment?

- **Assumption**: "`uv.lock` currently sits at 81.0.0, also below 83.0.0" (the
  issue's premise 1, second half)
- **Method**: code-read
- **Result**: **It does not describe the build environment.** The `setuptools`
  entry at `uv.lock:4397` is a *runtime* transitive dependency — `torch`, from
  the `benchmark` extra, requires it (`uv.lock:4656`). Build-system requires for
  the root project are not resolved into the lock at all. Raising
  `[build-system] requires` will not move that entry, and the 81.0.0 figure is
  not evidence about anything this issue is about.
- **Confidence**: high
- **Impact if false**: would make `uv.lock` regeneration part of the task list.
  It is not — but the task list verifies `uv lock --check` anyway rather than
  assuming, because #670 was bitten by exactly that assumption in `examples/`.

### Incidental finding (not an acceptance criterion)

**`tests/` ships in the sdist but `conftest.py` does not.** All 141 test files
in `popoto-1.9.0.tar.gz` match `test_*.py`; the conftest that makes them
runnable does not match the legacy `test*.py` glob and so was left behind. The
shipped tests cannot be run by a consumer. This is the distutils default
asserting itself, not a decision anyone made. It is harmless, it is out of scope
here (changing sdist membership is a packaging change with its own blast
radius), and it is precisely the kind of thing the AC4 check would surface. See
No-Gos.

## Prior Art

- **#670 / PR #682** (merged 2026-09-07, this same lane) — the sibling floor
  issue. Established the working method this plan reuses: verify the real
  minimum yourself, correct the issue's premises in the PR body, and raise to
  the minimum the evidence supports rather than to latest. Also established that
  a floor change must be checked against `uv.lock` *and* `examples/uv.lock`,
  because `lock-check.yml`'s path filters never reach the second one.
- **#669** (`lock-check.yml` hardening) — the closest prior art in intent. It
  found that `uv.lock` was the one file no CI job exercised, and closed the gap
  by adding *installability* and *import-surface* checks beside the existing
  *consistency* check. The sdist is in the same position today: the artifact
  users actually download is asserted about by nothing. CLAUDE.md's framing
  there — read a green check as exactly what it proves and no more — is the
  standard this plan's check should be written to.
- **`tests/test_ci_workflow_redis_url.py`** — precedent for a unit test that
  asserts a *workflow file's* content, so a CI wiring cannot be silently
  deleted. The AC4 work reuses this shape.
- **`scripts/check_lock_imports.py`** — precedent for a small standalone script
  invoked by a workflow rather than logic embedded in YAML. Also a cautionary
  precedent: it hand-lists its packages and nothing enforces the correspondence,
  which CLAUDE.md flags as a review-blocking omission rather than a CI failure.
  The sdist check should avoid inheriting that shape where it can.

## Data Flow

The path from repository to consumer, with the assertion gap marked:

```
git tag v1.9.0
  -> release.yml (ubuntu-latest)
       -> pip install build
       -> python -m build            <-- resolves [build-system] requires FRESH
       |                                 from PyPI; uv.lock plays no part here
       -> dist/popoto-1.9.0.tar.gz   <-- ***nothing asserts anything about this***
       -> pypa/gh-action-pypi-publish
  -> PyPI
  -> pip install popoto             <-- wheel, normally; sdist on fallback
```

Two facts fall out of this trace and both matter to the solution:

1. `python -m build` resolves `[build-system] requires` from PyPI at release
   time, in an isolated environment. The floor in `pyproject.toml` is the *only*
   thing constraining which setuptools builds the artifact — no lockfile, no
   pinned CI dependency, nothing else. That is why the floor is worth correcting
   even with exposure at nil: it is load-bearing and currently says almost
   nothing.
2. The gap is between *build* and *publish*, and it is one step wide. That is
   where the AC4 check belongs — not in the test suite, which never sees a real
   sdist, and not after publication, which is too late to matter.

## Solution

Three changes, smallest first.

### 1. Raise the build floor to the patched version

```toml
[build-system]
requires = ["setuptools>=83", "wheel"]
```

`>=83`, not `>=84` and not a pin. 83.0.0 is the first patched release; anything
higher is unsupported by the evidence, and popoto is not in a position to have
opinions about setuptools minor versions.

The published-library objection that governs popoto's other floors — a raised
floor propagates to every downstream consumer, which is why the Dependabot root
lane is `versioning-strategy: lockfile-only` — **does not apply here**.
`[build-system] requires` constrains the environment that *builds* popoto, not
the environment that installs it. A consumer installing the wheel never
resolves it at all; a consumer installing the sdist resolves it in an isolated
build env, and 83.0.0's `requires-python >=3.10` matches popoto's own floor
exactly. The blast radius is popoto's own release job and anyone building from
source.

Add a comment recording *why* the floor is 83 specifically, so the next person
to see `>=83` does not have to re-derive it from an advisory ID.

### 2. Assert what the sdist contains, between build and publish

A new `scripts/check_sdist_contents.py` takes a path to a `.tar.gz` and fails
non-zero when the tarball contains anything unexpected. Four rules, each
motivated by something this recon actually found rather than by imagination:

| Rule | Why |
|---|---|
| every member path is ASCII | directly forecloses the advisory's precondition 2, permanently, on the artifact itself |
| no dotfiles at any depth | `.env`, `.git`, `.github`, credentials — the leak shapes that matter |
| top-level entries within an allowlist | catches a stray directory the defaults or a future `MANIFEST.in` pulls in |
| no absolute paths and no `..` members | ordinary tarball hygiene; cheap to assert while we are here |

The allowlist is the six entries the 1.9.0 sdist actually has (`LICENSE`,
`PKG-INFO`, `README.md`, `pyproject.toml`, `setup.cfg`, plus `src/` and
`tests/`). Membership changes will fail the release and require someone to say
so deliberately — which is the point.

Wire it into `release.yml` as one step between `python -m build` and the publish
action. This is the only place in the pipeline where a real sdist exists, and
failing there means the bad artifact never reaches PyPI.

### 3. Keep the wiring from being silently deleted

Two tests, both fast and neither building anything:

- unit tests for the script's rules, driven against synthetic tarballs
  constructed in a `tmp_path` — a non-ASCII member, a dotfile, an unexpected
  top-level directory, a `..` member, and a clean control. No `python -m build`,
  no network.
- a test asserting `release.yml` still invokes the script between the build and
  publish steps, in the shape of `tests/test_ci_workflow_redis_url.py`. A check
  wired into a workflow that anyone can delete in a one-line diff is not a
  check.

## Rabbit Holes

- **Rebuilding 1.9.0 to diff it against the published artifact.** Tempting as
  proof, and it proves nothing this recon has not already established by reading
  the tarball directly. The published sdist *is* the evidence.
- **Auditing every historical sdist back to 1.0.** AC3 asks for the most recent.
  Preconditions 1 and 2 have been absent for the life of the repository (no
  `MANIFEST.in` has ever been tracked), so older tarballs cannot have been
  exposed by a mechanism that requires one.
- **Fixing the `tests/`-without-`conftest.py` wart.** A real packaging question,
  a genuinely separate one, and one with consumer-visible consequences. It gets
  an issue, not a hitchhiking commit.
- **Generalizing the check into a reusable packaging-policy framework.** The
  appetite is Small. One script, four rules, one workflow step.
- **Pinning setuptools exactly, or adding an upper bound.** Neither is supported
  by the evidence, and an upper bound would create a maintenance obligation with
  no offsetting benefit.

## No-Gos

- **No release, no tag, no PyPI publish.** The issue says this explicitly and so
  does the lane's brief: the release decision is Tom's. This PR changes the
  floor and adds the check; it does not exercise them against PyPI.
- **No change to `[project.dependencies]` or any runtime floor.** Nothing in
  this issue touches what consumers resolve at install time.
- **No change to sdist membership.** Not adding `conftest.py`, not adding a
  `MANIFEST.in`, not excluding `tests/`. The check asserts what is; changing
  what is belongs to a separate decision.
- **No `MANIFEST.in` introduced as a "fix".** Adding one would *create* the
  precondition the advisory needs. The absence is a feature here.
- **No regeneration of `uv.lock` unless `uv lock --check` says otherwise.**
  spike-4 establishes that build-system requires do not enter the lock; the task
  list verifies rather than assumes, but a diff-free lock is the expected
  outcome.

## Risks

- **The check is written against a single observed artifact.** An allowlist
  derived from 1.9.0 will fail a future release that legitimately adds a
  top-level file. That is the intended behavior — it fails loudly at release
  time with an obvious one-line remedy — but it must be *documented* in the
  script's error message, or someone mid-release will read it as a bug in the
  check. Mitigation: the failure message names the file, the allowlist, and the
  line to edit.
- **`release.yml` cannot be tested end-to-end without cutting a release.** The
  step is verified by reading the workflow and by running the script by hand
  against the real 1.9.0 tarball — which is a genuine test of the script,
  because that tarball is a real artifact of the real build. What stays
  unverified until the next release is the YAML wiring itself; the workflow test
  narrows that to "the step exists and is ordered correctly".
- **Raising the floor could surprise a source-install user on an old
  setuptools.** They get a clear resolution error naming the requirement, not a
  silent failure, and 83 does not raise the Python floor. Low.
- **The advisory could be re-scoped.** If a later analysis extends it beyond
  APFS/HFS+, spike-3's mitigation evaporates — but spikes 1 and 2 do not depend
  on platform, and the floor will already be at 83.

## Success Criteria

- `pyproject.toml` declares `setuptools>=83` with a comment naming the advisory
  and why 83 specifically.
- `scripts/check_sdist_contents.py` exits 0 against the real published
  `popoto-1.9.0.tar.gz` and non-zero against each of the four synthetic
  violations.
- `release.yml` runs the script after `python -m build` and before the publish
  action; the publish step is unreachable when the check fails.
- New tests pass: the script's unit tests and the workflow-wiring test.
- `ruff check src/` exits 0; `black --check src/ tests/` passes (the new script
  lives under `scripts/`, which is not ruff-gated, but is kept black-clean by
  hand for consistency).
- `scripts/mypy_ratchet.py` at or below the baseline read at measurement time.
- `uv lock --check` passes with no lock regeneration required.
- AC1's answer — exposure nil, with the three absent preconditions — is recorded
  durably in the repository, not only in the issue thread.

## Step by Step Tasks

1. Raise `[build-system] requires` to `setuptools>=83` with an explanatory
   comment.
2. Write `scripts/check_sdist_contents.py`: four rules, a clear failure message
   naming the offending member and the remedy, a `--help` that explains what it
   is for.
3. Run it against the real `popoto-1.9.0.tar.gz` downloaded from PyPI; confirm
   exit 0.
4. Add the step to `.github/workflows/release.yml` between build and publish.
5. Write `tests/test_sdist_contents.py`: synthetic-tarball unit tests for each
   rule plus a clean control.
6. Extend it (or add alongside) the workflow-wiring assertion, modeled on
   `tests/test_ci_workflow_redis_url.py`.
7. Verify `uv lock --check` and `examples/` lock consistency are unaffected.
8. Run the narrow gates: the new tests, `ruff`, `black`, the mypy ratchet.
9. Document the outcome — see Documentation.

## Documentation

- **CLAUDE.md**, in the packaging/dependency area: what the sdist check asserts,
  what it deliberately does not prove (it validates *contents*, never that the
  packaged code works), and the standing fact that popoto has no `MANIFEST.in`
  — including the warning that adding one would create the advisory's
  precondition. This is the durable home for AC1's answer.
- **`release.yml`** gains an inline comment pointing at the script and the
  issue.
- No user-facing docs change. Nothing here alters an API, an install command, or
  anything a consumer of the library can observe.

## Open Questions

None for a human. All four acceptance criteria were resolved by the spikes
above, and the one judgment call AC4 leaves open — whether an sdist-contents
assertion is worth building — is answered in Solution with its reasoning stated
so critique can challenge it: the check is justified by the preconditions being
*absent but returnable*, not by a live exposure.
