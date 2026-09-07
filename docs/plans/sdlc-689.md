---
status: Planning
type: bug
appetite: Small
owner: sdlc-689
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/689
last_comment_id:
---

# Stop shipping an unrunnable `tests/` in the sdist

## Problem

The published popoto sdist contains a `tests/` directory that cannot be run. It
is not a partial suite — it is a suite that fails at collection, and would still
fail at collection after the one file the issue names is added.

**Current behavior** (measured, not inferred — see Spike Results):

`popoto-1.9.0.tar.gz` downloaded from PyPI has 264 members. 141 of them are
under `tests/`: the directory entry plus 140 `tests/test_*.py` files. It does
**not** contain `tests/conftest.py`, `tests/__init__.py`, `tests/all_tests.py`,
or any of the four `tests/` subpackages (`benchmarks/`, `recipes/`,
`embeddings/`, `fixtures/`).

The mechanism is the distutils default sdist membership that setuptools falls
back to when no `MANIFEST.in` exists. In
`setuptools/_distutils/command/sdist.py`, `sdist._add_defaults_optional` is:

```python
def _add_defaults_optional(self):
    optional = ['tests/test*.py', 'test/test*.py', 'setup.cfg']
```

`tests/test*.py` is a non-recursive glob on filenames beginning `test`. Every
`tests/test_*.py` matches; `conftest.py` does not, `__init__.py` does not, and
nothing below `tests/*/` is reached at all. The issue's diagnosis is correct on
the cause and slightly off on the glob (it is `tests/test*.py`, listed ahead of
the legacy `test/test*.py`, not the legacy glob itself).

The effect is worse than "missing fixtures". Three independent reasons the
shipped tree cannot execute from an unpacked sdist:

1. **No `conftest.py`.** It defines an autouse `_stop_embedding_invalidation_listeners`
   fixture and the `assert_captured` fixture. Tests requesting `assert_captured`
   error at setup with `fixture 'assert_captured' not found`.
2. **23 shipped test files read repository paths that an sdist does not and
   cannot contain** — `docs/`, `scripts/`, `examples/`, `uv.lock`, `CLAUDE.md`,
   and `.github/workflows/`. Among them `test_docs_redis_url.py`,
   `test_ci_workflow_redis_url.py`, `test_mypy_ratchet.py`,
   `test_check_lock_imports.py`, `test_sdist_contents.py`,
   `test_anthropic_floor.py`, `test_guide_examples.py`. `.github/` is a dotfile
   path, which `scripts/check_sdist_contents.py` **hard-fails** on by design, so
   that subset can never be made to pass by shipping more files.
3. **The four `tests/` subpackages are absent**, so anything importing
   `tests.benchmarks.*` or reading `tests/fixtures/` has no source to import.

On top of that the suite needs a live Redis/Valkey, the `dev` extra, and several
optional extras (~95 tests deselect without `embeddings`/`benchmark`/`mcp`).

**Desired outcome:** the sdist stops carrying a broken artifact. `tests/` is
excluded from the sdist entirely, the exclusion is declared in one place, and
`scripts/check_sdist_contents.py`'s known top-level set plus the prose in
`CLAUDE.md`/`CHANGELOG.md` that asserts "no `MANIFEST.in` exists at all" are
updated in the same change so the repo's own documentation does not become
false.

## Freshness Check

**Baseline commit:** `a9d858fc74a35c44f66ec6a378472ce9f82a82f9` (plan branched
from this; `03f09def` is the skeleton commit on top of it)
**Issue filed at:** 2026-09-07T09:18:47Z
**Disposition:** Minor drift

**File:line references re-verified:**

The issue body cites no file:line pointers — it makes one empirical claim about
the published 1.9.0 sdist and one causal claim about setuptools defaults. Both
were re-verified from scratch rather than trusted:

- "the published 1.9.0 sdist contains all 141 `tests/test_*.py` files but not
  `tests/conftest.py`" — **holds exactly.** Downloaded from PyPI: 264 members,
  141 under `tests/` (140 files + the directory entry), zero `conftest`.
- "with no `MANIFEST.in`, setuptools falls back to the distutils default sdist
  membership, whose legacy glob is `test*.py`" — **holds, with a correction.**
  The glob that matches here is `tests/test*.py`, first in
  `distutils.command.sdist.sdist._add_defaults_optional`'s `optional` list; the
  legacy `test/test*.py` is second and matches nothing (there is no `test/`
  directory). Verified by reading the installed setuptools 84.0.0 source.
- "Configuring membership through `[tool.setuptools]` in `pyproject.toml` avoids
  that trade entirely and is probably the better route" — **does not hold.**
  See spike-3: no `[tool.setuptools]` key controls sdist membership. This is the
  one substantive correction the plan makes to the issue.

**Cited sibling issues/PRs re-checked:**

- **#678** — closed. Its PR **#691 merged at 2026-09-07T09:46:52Z, 28 minutes
  *after* this issue was filed.** So the issue was written against a `main` that
  did not yet contain `scripts/check_sdist_contents.py`, `tests/test_sdist_contents.py`,
  or the `setuptools>=83` floor. All three exist now, and all three are edited
  by this plan. This is the drift; it enlarges the change surface without
  changing the defect.

**Commits on main since issue was filed (touching referenced/adjacent files):**

- `4e493b57` "Raise build floor to setuptools>=83 and check sdist contents
  before publish (#678) (#691)" — **changed the landscape, not the root cause.**
  Adds the checker, its tests, and the build floor. The `setuptools>=83` floor
  it introduced is what makes a `MANIFEST.in` safe to add (see Risks).
- `c046e1bd`, `3ff7f471`, `baa9956c`, `542c11c0`, `ddfbed32`, `db0f10e3` —
  irrelevant (Redis client resolution, OpenClaw, decay fields, mypy ratchet).
  They add and remove `tests/test_*.py` files, which moves the *count* of
  shipped tests but not the defect.
- The defect was reproduced directly against baseline `a9d858fc`: a locally
  built sdist has 297 members, 167 under `tests/`, no `conftest.py`.

**Active plans in `docs/plans/` overlapping this area:**
`docs/plans/setuptools_build_floor_and_sdist_exposure.md` (#678) is **Complete**,
not active — it is this issue's parent and is treated as prior art, not as a
coordination conflict. `docs/plans/sdlc-698.md` is being written concurrently on
`main` by a sibling lane but touches `CyclicDecayField`, not packaging. No
overlap.

**Notes:** The issue predates its own parent PR merging. Nothing about the
defect changed; what changed is that the fix now has to keep
`scripts/check_sdist_contents.py` and two prose documents consistent with
itself.

## Prior Art

Searched `gh issue list --state all --search "sdist"` and
`gh pr list --state merged --search "sdist"`, plus `git log` over
`pyproject.toml`, `setup.cfg`, and `.github/workflows/release.yml`.

- **#678 / PR #691** — "Build backend allows setuptools<83, which has a
  MANIFEST.in exclusion bypass on macOS". Merged 2026-09-07. Raised
  `[build-system] requires` to `setuptools>=83`, added
  `scripts/check_sdist_contents.py` and `tests/test_sdist_contents.py`, wired the
  checker into `release.yml` between `python -m build` and publish. **This issue
  is its explicit spin-out.** Its plan
  (`docs/plans/setuptools_build_floor_and_sdist_exposure.md`) contains the
  "no `MANIFEST.in` exists at all" finding that this plan invalidates.
- **#694 / PR #695** — "CLAUDE.md and CHANGELOG overstate that the setuptools
  build floor never reaches consumers". Merged 2026-09-07. Corrected the claim to
  "the floor does not reach a wheel install, and it does constrain an sdist
  build, including a consumer's". Directly relevant: it establishes that the
  `setuptools>=83` floor **is** binding on anyone who builds popoto from sdist,
  which is what makes a `MANIFEST.in` exclusion trustworthy rather than
  best-effort.
- **PR #536** — "sync uv.lock with the 1.8.2 version bump". Unrelated packaging
  housekeeping; no sdist membership work.
- **#663** — mypy/`py.typed`. Touches `setup.cfg`, not sdist membership. Its
  finding that `src/popoto/py.typed` is deliberately unshipped is a reminder that
  wheel/sdist membership decisions in this repo are made explicitly, not by
  default.

No prior attempt to fix sdist membership exists. This is a first fix, not a
repeat.

## Research

**Queries used:**

- `setuptools control sdist contents without MANIFEST.in pyproject.toml exclude tests`

**Key findings:**

- **setuptools has no `pyproject.toml` equivalent for sdist file selection.**
  The official guide "Controlling files in the distribution"
  (<https://setuptools.pypa.io/en/latest/userguide/miscellaneous.html>) names
  `MANIFEST.in` as *the* mechanism for anything the default algorithm does not
  catch. This is the load-bearing finding: it contradicts the route the issue
  suggests, and it is corroborated by spike-3 reading the setuptools config
  schema directly.
- **`[tool.setuptools.packages.find] exclude` governs package discovery (wheel),
  not sdist membership**, and is a long-standing source of exactly this
  confusion — pypa/setuptools#3817 ("Can't exclude some of test code when build
  sdist") reports test code appearing in the sdist despite `pyproject.toml`
  changes while the wheel is correctly clean, and maintainers point at
  pypa/setuptools#3260. popoto's wheel already omits `tests/` (package discovery
  is `where = src`), so this key is not part of the fix.
  <https://github.com/pypa/setuptools/issues/3817>
- **`MANIFEST.in` directives are order-sensitive** — processed top to bottom, so
  a `prune` must follow any `graft`/`include` it is meant to override. The fix
  here is a single directive, so ordering does not bite, but a later editor
  adding an `include` line below the `prune` would.
- **The counter-position is real and should be named, not ignored:** many
  maintainers argue tests *should* ship in the sdist so downstream packagers
  (distros, conda-forge) can run them at build time, while being excluded from
  the wheel. That is the strongest argument for Option B in the Solution
  section, and it is rejected here on evidence specific to popoto — 23 of the
  shipped tests read repository paths that no sdist can legally contain — not on
  principle.
  <https://discuss.python.org/t/ignoring-py-files-in-sdist-and-bdist-with-setuptools-requires-a-manifest/39276>
- **`setuptools-scm` is the alternative to `MANIFEST.in`**: it makes sdist
  membership "everything git tracks". That is the wrong direction here — it
  would ship `docs/`, `.github/`, `examples/`, and `scripts/`, exploding the
  sdist and colliding head-on with `check_sdist_contents.py`'s dotfile
  hard-failure rule.

## Spike Results

All five spikes were run at plan time on this machine. Environment, because
CLAUDE.md requires a count to carry one: macOS 25.6 (APFS), Python 3.12,
setuptools 84.0.0 and `build` in a throwaway venv, no Redis involved (nothing
here executes popoto).

### spike-1: does the *published* 1.9.0 sdist really contain tests and no conftest?
- **Assumption**: "the published 1.9.0 sdist contains all 141 `tests/test_*.py`
  files but **not** `tests/conftest.py`"
- **Method**: prototype (download from PyPI, enumerate `tarfile` members)
- **Finding**: **Confirmed exactly.** 264 members total. Top-level counts:
  `tests` 141, `src` 117, plus `LICENSE`, `PKG-INFO`, `README.md`,
  `pyproject.toml`, `setup.cfg` — the seven entries `EXPECTED_TOP_LEVEL` names.
  The 141 is 140 `tests/test_*.py` files plus the `tests/` directory member.
  Zero members matching `conftest`.
- **Confidence**: high
- **Impact on plan**: the issue's premise is exact; no re-scoping needed.

### spike-2: does the defect still reproduce on current `main`?
- **Assumption**: "nothing since the issue was filed has changed sdist membership"
- **Method**: prototype (`python -m build --sdist` against baseline `a9d858fc`)
- **Finding**: **Reproduces.** 297 members; `tests` 167, `src` 124. Still no
  `conftest.py`, no `tests/__init__.py`, no `tests/all_tests.py`, and none of
  `tests/benchmarks/`, `tests/recipes/`, `tests/embeddings/`, `tests/fixtures/`.
  The count moved (167 vs 141) only because tests were added to the repo.
  **Build gotcha worth recording:** `python -m build` cannot be run from the repo
  root with the repo's own venv, because the untracked `build/` directory shadows
  the `build` module (`No module named build.__main__`). Build from a different
  cwd passing the source dir, in a venv that has `build` installed.
- **Confidence**: high
- **Impact on plan**: disposition "still broken"; no chance the parent PR fixed it.

### spike-3: is there a `[tool.setuptools]` key that controls sdist membership?
- **Assumption**: the issue's preferred route — "Configuring membership through
  `[tool.setuptools]` in `pyproject.toml` avoids that trade entirely"
- **Method**: code-read (enumerate the accepted keys in setuptools 84.0.0's
  `setuptools.config._validate_pyproject` schema) + web research
- **Finding**: **Assumption is false.** The complete `[tool.setuptools]` key set
  is: `cmdclass`, `data-files`, `dynamic`, `eager-resources`,
  `exclude-package-data`, `ext-modules`, `include-package-data`, `license-files`,
  `namespace-packages`, `obsoletes`, `package-data`, `package-dir`, `packages`,
  `platforms`, `provides`, `py-modules`, `script-files`, `zip-safe`. Every one
  of them feeds *package/wheel* content or metadata. None reaches
  `sdist.add_defaults`. `packages.find.exclude` is the one people reach for and
  it governs discovery, not the sdist (pypa/setuptools#3817). The one
  theoretical exception, `cmdclass`, is rejected in Rabbit Holes.
- **Confidence**: high
- **Impact on plan**: **this is the finding that selects the solution.** The
  issue asks for a route that does not exist; the plan adopts `MANIFEST.in` and
  documents why, rather than silently doing something the issue advised against.

### spike-4: does a one-line `MANIFEST.in` actually remove `tests/`?
- **Assumption**: "`prune tests` in `MANIFEST.in` excludes the directory from
  the sdist and changes nothing else"
- **Method**: prototype, in an isolated `git clone` of the repo (not the working
  tree), so no repo pollution
- **Finding**: **Works, one directive, no side effects.** With
  `MANIFEST.in` containing only `prune tests`: 131 members, top-level
  `src` 124, `LICENSE`, `MANIFEST.in`, `PKG-INFO`, `README.md`, `pyproject.toml`,
  `setup.cfg`. `tests` gone entirely; `src` membership byte-for-byte the same
  count as the unpruned build. `MANIFEST.in` itself ships (setuptools appends
  `self.template` to the file list) — so it *replaces* `tests` in the top-level
  set rather than shrinking it, and the set stays at seven entries.
  Running the real checker on that tarball:
  `WARNING: 'MANIFEST.in': unexpected top-level entry` then
  `OK: ... (131 members, 1 warning(s))`, exit 0 — confirming both that the
  warning fires and that it is non-blocking, i.e. `EXPECTED_TOP_LEVEL` must be
  updated in the same PR or every future release prints a spurious warning.
- **Confidence**: high
- **Impact on plan**: fixes the exact `MANIFEST.in` body and makes the
  `EXPECTED_TOP_LEVEL` edit a required task rather than a nicety.

### spike-5: *why* does `tests/` ship at all, and is a local build a valid oracle?
- **Assumption**: "the default membership rule is stable, so a local build tells
  us what CI will publish"
- **Method**: code-read of setuptools 84.0.0
  `setuptools/_distutils/command/sdist.py` and
  `setuptools/command/egg_info.py::manifest_maker`
- **Finding**: two distinct mechanisms, and only one of them is the defect.
  1. `distutils sdist._add_defaults_optional` globs
     `['tests/test*.py', 'test/test*.py', 'setup.cfg']`. That is the defect: a
     non-recursive filename glob that `conftest.py` cannot match.
  2. `manifest_maker.add_defaults` ends with
     `elif os.path.exists(self.manifest): self.read_manifest()`, where
     `self.manifest` is `src/popoto.egg-info/SOURCES.txt`. **A stale
     `SOURCES.txt` left in a developer's working tree is re-read and its entries
     re-added to a new sdist.** `src/popoto.egg-info/` is gitignored, so CI's
     `actions/checkout` never has one and CI builds are clean, but a local build
     can carry files forward from a previous build. The repo's current
     `SOURCES.txt` lists 166 `tests/` entries.
  This is why spike-4 was run in a fresh `git clone` and not in the working tree.
- **Confidence**: high
- **Impact on plan**: adds a hard constraint to the Verification section — any
  sdist reproduced by hand must be built from a clean checkout, or the result is
  not evidence. It also means `prune tests` should do double duty: `MANIFEST.in`
  is applied by `manifest_maker.run()`'s `read_template()`, which runs *after*
  `add_defaults()`, so the directive removes both mechanism 1's glob matches and
  mechanism 2's stale `SOURCES.txt` re-adds. That second half is read from the
  source, not measured — spike-4 ran in a fresh clone, which by construction has
  no stale `SOURCES.txt`. A build task verifies it in a dirty tree.

## Data Flow

The "data" here is a file list moving from the repository to PyPI. Tracing it is
what makes clear where an intervention can and cannot be placed.

1. **Entry point**: a `v*` tag push triggers `.github/workflows/release.yml`.
2. **`actions/checkout@v7`**: a clean tree — no `src/popoto.egg-info/`, no
   `build/`, no `.venv`. This is why CI membership is deterministic and a local
   build is not (spike-5).
3. **`python -m build`**: resolves an isolated build env against
   `[build-system] requires = ["setuptools>=83", "wheel"]`, then calls the
   setuptools backend.
4. **`egg_info` → `manifest_maker.run()`**: builds the file list.
   `add_defaults()` adds `src/**/*.py` (from `build_py`, driven by
   `setup.cfg`'s `packages = find:` / `where = src`), the standards
   (`README.md`, `LICENSE`, `setup.cfg`, `pyproject.toml`), the egg-info
   directory — **and `tests/test*.py` via `_add_defaults_optional`. This is the
   only step where `tests/` enters.** Then `read_template()` applies
   `MANIFEST.in` if one exists — **this is the single point where the fix
   lands** — then `add_license_files()`, `_add_referenced_files()`,
   `prune_file_list()`, and the list is written to
   `src/popoto.egg-info/SOURCES.txt`.
5. **`sdist.make_distribution()`**: copies the file list into
   `popoto-<version>/` and tars it into `dist/`.
6. **`scripts/check_sdist_contents.py dist/*.tar.gz`**: reads the tar member
   list, hard-fails on non-ASCII / dotfile / absolute-or-`..` / link members,
   warns on a top-level entry outside `EXPECTED_TOP_LEVEL`. **Observes; never
   changes membership.** After the fix its known set must read
   `MANIFEST.in` where it currently reads `tests`.
7. **Output**: `pypa/gh-action-pypi-publish` uploads `dist/*` to PyPI.

The wheel path is disjoint and unaffected: `bdist_wheel` takes its contents from
`build_py`/package discovery, which is already `where = src`, so `tests/` has
never been in the wheel and nothing in this plan touches that.

## Why Previous Fixes Failed

No previous fix targeted sdist membership, so there is no failure history to
analyze. One adjacent near-miss is worth recording, because it is the reason
this issue exists as a separate item rather than as a line in #678:

| Prior Fix | What It Did | Why It Was Incomplete (by design) |
|-----------|-------------|-----------------------------------|
| PR #691 (#678) | Raised the build floor to `setuptools>=83`; added `scripts/check_sdist_contents.py` as a *member-list assertion* run before publish | Scoped deliberately to asserting what the sdist contains, not to deciding what it should contain. Its `EXPECTED_TOP_LEVEL` therefore *records* `tests` as expected — it froze the defect as the baseline instead of flagging it. That is correct behavior for an assertion written against the shipped artifact, and it is exactly why the issue says "out of scope there". |

**Root cause pattern:** an observability gate calibrated against a broken
baseline blesses the baseline. Nothing here failed; the ordering just means the
membership decision has to be made now, and `EXPECTED_TOP_LEVEL` re-calibrated
when it is.

## Architectural Impact

- **New dependencies**: none. No runtime dependency, no build dependency, no new
  extra. `MANIFEST.in` is read by the setuptools already required.
- **Interface changes**: the *published sdist's* contents change — 141 fewer
  members in 1.9.0 terms, ~166 fewer at current `main`. Nothing importable
  changes: `tests/` was never a package in the sdist (`tests/__init__.py` never
  shipped) and never installed by any install path.
- **Coupling**: adds one coupling that must be maintained by hand —
  `MANIFEST.in`'s `prune tests` and `check_sdist_contents.py`'s
  `EXPECTED_TOP_LEVEL` must agree. This is the same hand-maintained shape
  CLAUDE.md already criticizes in `check_lock_imports.py`, and the mitigation is
  the same: a test that names the correspondence (see Test Impact).
- **Data ownership**: **this is the meaningful change.** Today, sdist membership
  is owned by a distutils default nobody in this repo chose. After this change,
  it is owned by a file in the repo. The `check_sdist_contents.py` docstring's
  reason for the warning-only severity — "there is no machine-readable
  declaration of intended sdist membership to parse against, because a
  `MANIFEST.in` *would* be that declaration" — **stops being true**, and the
  prose must be updated even though the severity split itself deliberately does
  not change (see No-Gos).
- **Reversibility**: total. Deleting `MANIFEST.in` restores the old behavior
  exactly; there is no state, no migration, and no consumer that can have
  depended on it between releases.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (one decision point: ship-nothing vs. ship-runnable — see
  Open Questions)
- Review rounds: 1

The code change is three lines across three files. The appetite is spent almost
entirely on the prose that must stay true (`CLAUDE.md`, `CHANGELOG.md`, the
`check_sdist_contents.py` docstring, and the #678 plan's superseded finding) and
on not accidentally flipping the warning-only severity that CLAUDE.md explicitly
warns future editors against "simplifying away".

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `build` frontend available in a venv that is not the repo root's | `python -c "import build"` run from a directory other than the repo root | Reproducing the sdist. The repo root's untracked `build/` directory shadows the module (spike-2). |
| Network access to PyPI | `curl -sfI https://pypi.org/pypi/popoto/1.9.0/json > /dev/null` | Only needed to re-verify the published-artifact claim; not needed to build or test. |
| Redis/Valkey on `localhost:6379` | `redis-cli ping` | Required by the popoto suite generally, not by any test this plan adds. Use `POPOTO_TEST_DB=9` for this lane. |

## Prerequisites

_placeholder_

## Solution

_placeholder_

## Failure Path Test Strategy

_placeholder_

## Test Impact

_placeholder_

## Rabbit Holes

_placeholder_

## Risks

_placeholder_

## Race Conditions

_placeholder_

## No-Gos (Out of Scope)

_placeholder_

## Update System

_placeholder_

## Agent Integration

_placeholder_

## Documentation

_placeholder_

## Success Criteria

_placeholder_

## Team Orchestration

_placeholder_

## Step by Step Tasks

_placeholder_

## Verification

_placeholder_

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

## Open Questions

_placeholder_
