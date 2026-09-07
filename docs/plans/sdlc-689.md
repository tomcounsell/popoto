---
status: Planning
type: bug
appetite: Small
owner: sdlc-689
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/689
last_comment_id: none
revision_applied: false
revision_applied_at:
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

The code change is one new one-directive file, a two-line edit to a frozenset,
one new guard test, and one new `scripts/verify/` shell script. The appetite is
spent almost
entirely on the prose that must stay true (`CLAUDE.md`, `CHANGELOG.md`, the
`check_sdist_contents.py` docstring, and the #678 plan's superseded finding) and
on not accidentally flipping the warning-only severity that CLAUDE.md explicitly
warns future editors against "simplifying away".

## Prerequisites

**Measured state of this machine, 2026-09-07** (critique BLOCKER; re-measured
during the revision pass, not taken on trust). `/Users/valorengels/src/popoto/.venv`,
Python 3.12, macOS 25.6 (APFS):

- `setuptools 81.0.0` — **below the `>=83` floor**, so a `--no-isolation` build
  is not available here and the isolated branch must be taken.
- `import build` from `/tmp` → `ModuleNotFoundError`. The `build` frontend is
  **not installed**.
- `import build` from the repo root **succeeds vacuously**: the untracked
  `build/` directory resolves as a namespace package
  (`build.__file__ is None`, `__path__ = _NamespacePath(['…/popoto/build'])`).
  A bare `python -c "import build"` run from the repo root is therefore **not a
  valid probe** — it self-confirms a prerequisite that is unmet.
- `import pip` → `ModuleNotFoundError`. `.venv` is uv-managed and has no `pip`,
  so `.venv/bin/pip install build` cannot be the remedy.

Consequence for this plan: nothing may *assume* a working `build`. Every
sdist-building step resolves the frontend on demand into a throwaway venv, and
every probe runs from a cwd outside the repo.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `build` frontend, resolved on demand (do **not** assume it is present) | `python -m venv "$TMP/venv" && "$TMP/venv/bin/python" -m pip install build` — then use `"$TMP/venv/bin/python" -m build`. Never `.venv/bin/pip` (absent). | Reproducing the sdist. Measured: `build` is not installed in `.venv` and `.venv` has no `pip`. |
| A *non-vacuous* `build` probe, if you probe at all | `cd /tmp && python -c "import build, sys; sys.exit(0 if getattr(build, '__file__', None) else 1)"` | The repo root's untracked `build/` directory shadows the module as a namespace package with `__file__ is None`, so the naive probe passes while the import is useless (spike-2, and the critique BLOCKER). |
| Isolation branch chosen from a *measured* setuptools version | `python -c "import setuptools; from packaging.version import Version; print(int(Version(setuptools.__version__) >= Version('83')))"` → `--no-isolation` only on `1`, otherwise an isolated build | Measured here: `81.0.0` → `0` → isolated build (needs network). `packaging` is present in `.venv` (26.2); if it is ever absent, fall back to comparing `int(setuptools.__version__.split('.')[0]) >= 83`. |
| Network access to PyPI | `curl -sfI https://pypi.org/pypi/popoto/1.9.0/json > /dev/null` | Needed to re-verify the published-artifact claim **and**, on this machine, to run the isolated build (setuptools 81 forces it). |
| Redis/Valkey on `localhost:6379` | `redis-cli ping` | Required by the popoto suite generally, not by any test this plan adds. Use `POPOTO_TEST_DB=9` for this lane. |


## Solution

**Decision: stop shipping `tests/` in the sdist.** The issue offers two
defensible ends of the fork; this picks the first, on evidence rather than
taste.

Why not Option B ("ship `conftest.py` and whatever else the suite needs"): the
"whatever else" is unbounded and partly *illegal*. Making the shipped suite
runnable requires `tests/conftest.py`, `tests/__init__.py`, four `tests/`
subpackages including `tests/benchmarks/` (which carries datasets and results),
and then the repository paths that 23 of the shipped tests read — `docs/`,
`scripts/`, `examples/`, `uv.lock`, `CLAUDE.md`, and `.github/workflows/`.
That last one cannot ship: `scripts/check_sdist_contents.py` **hard-fails** on a
dotfile member at any depth, so `tests/test_ci_workflow_redis_url.py` can never
pass from an sdist without weakening a security rule. Option B ends at "ship the
whole repository", i.e. `setuptools-scm`, which is a different project. Option A
is one directive.

### Key Elements

- **`MANIFEST.in`** (new, repo root, one directive): declares that `tests/` is
  not part of the source distribution. This is the only mechanism setuptools
  offers — spike-3 and the setuptools user guide both say so; the route the
  issue preferred does not exist.
- **`scripts/check_sdist_contents.py`**: `EXPECTED_TOP_LEVEL` re-calibrated —
  `tests` out, `MANIFEST.in` in. Still seven entries. Rule severities untouched.
- **A guard test** in `tests/test_sdist_contents.py`: asserts the
  `MANIFEST.in` ↔ `EXPECTED_TOP_LEVEL` correspondence that nothing else
  enforces, and asserts `tests` is *absent* from the known set so a later editor
  cannot re-add it without reading why.
- **Prose that becomes false**: `CLAUDE.md` and `CHANGELOG.md` both state, as
  verified fact, that "no `MANIFEST.in` exists at all"; `CLAUDE.md` and the
  `check_sdist_contents.py` module docstring both justify the warning-only
  severity by "a `MANIFEST.in` *would* be that declaration [and adding one would
  create the advisory's own precondition]". All four passages need updating —
  not deleting, since the reasoning stays correct as a record of why the floor
  was raised first.

### Flow

Tag push → `release.yml` checkout (clean) → `python -m build` → setuptools
`manifest_maker` adds defaults **including `tests/test*.py`** → `read_template()`
applies `MANIFEST.in`'s `prune tests` → `tests/` removed → tarball →
`check_sdist_contents.py` sees six known top-level entries plus `MANIFEST.in`,
zero warnings → publish.

### Technical Approach

- **`MANIFEST.in` body is `prune tests` and nothing else.** Spike-4 measured
  this exact body: it removes `tests/` completely and leaves `src/` membership
  identical. Resist adding `global-exclude *.pyc`, `recursive-exclude` lines, or
  a `graft` — every extra directive is another exclusion rule, and exclusion
  rules are what the setuptools advisory is about. One directive keeps the
  blast radius of a hypothetical bypass to one directory that is not shipped
  anyway.
- **Do not touch the rule severities in `check_sdist_contents.py`.** CLAUDE.md
  names the split as "the part most likely to be 'simplified' away by a later
  editor" and `tests/test_sdist_contents.py` has a test naming it. Adding a
  `MANIFEST.in` genuinely weakens the *stated reason* for warning-only
  (a machine-readable declaration now exists), but promoting the top-level rule
  to a hard failure is a separate judgment call under release pressure and is
  explicitly a No-Go here.
- **The `setuptools>=83` floor (#678) is what makes this safe, and #694 is what
  makes it binding.** The advisory is an exclusion *bypass*: with
  setuptools<83, a `MANIFEST.in` exclusion can be defeated by an NFC/NFD
  filename collision on APFS/HFS+. popoto's floor is `>=83`, and per #694 that
  floor constrains sdist builds — including a consumer's `--no-binary` build —
  so the exclusion is honored everywhere the sdist can be built. Precondition 1
  of the advisory does now exist; preconditions 2 (non-ASCII path) and 3
  (APFS/HFS+ build host) still do not, and `check_sdist_contents.py`'s non-ASCII
  hard-failure rule is what keeps precondition 2 from returning silently. Say
  this in the docs: the rule moves from defense-in-depth to load-bearing, which
  is the trade the issue flagged and which the plan accepts rather than denies.
- **Version bump / release is not part of this change.** The fix takes effect at
  the next release from `release.yml`; no republish of 1.9.0 is possible or
  attempted.

## Failure Path Test Strategy

### Exception Handling Coverage
- [x] No exception handlers in scope. `MANIFEST.in` contains no code.
  `scripts/check_sdist_contents.py`'s only edit is a frozenset literal and its
  docstring; the script has no `except` blocks at all (its two argument-resolution
  failures raise `SystemExit` deliberately, and CLAUDE.md records that "zero
  matches and two matches are both hard errors, never a vacuous pass").

### Empty/Invalid Input Handling
- [x] The relevant empty-input hazard is **a vacuous guard test**, which is this
  repo's documented failure mode (the #661 empty-capture trap, and CLAUDE.md's
  "if a spy test cannot tell stale from converted, it is not the test"). The new
  guard test must therefore assert on **content it parses**, not on a file
  merely existing: read `MANIFEST.in`, assert the parsed directive set is
  exactly `{"prune tests"}`, and assert `"tests" not in EXPECTED_TOP_LEVEL` and
  `"MANIFEST.in" in EXPECTED_TOP_LEVEL`. A test that only asserts
  `MANIFEST_IN.exists()` passes against an empty file and is not the test.
- [x] `check_members([])` — an empty member list — must not be treated as a
  pass by the new test's helpers. The existing suite builds synthetic tarballs
  in `tmp_path`; keep that shape.

### Error State Rendering
- [x] The user-visible failure path is the checker's own output, and spike-4
  already exercised it in the *pre-fix* direction: running the current checker
  against a pruned sdist prints
  `WARNING: 'MANIFEST.in': unexpected top-level entry` and exits 0. Paste that
  output into the PR as the red-state proof that the `EXPECTED_TOP_LEVEL` edit is
  load-bearing, then show it absent after. A synthetic-tarball test asserting
  that a member list of `{MANIFEST.in, PKG-INFO, README.md, pyproject.toml,
  setup.cfg, LICENSE, src/...}` produces **zero** warnings covers the green
  direction without building anything.

## Test Impact

No existing test breaks. Verified by reading `tests/test_sdist_contents.py`
rather than assuming: its synthetic fixture is
`CLEAN = ["PKG-INFO", "README.md", "pyproject.toml", "src/popoto/__init__.py"]`,
which contains no `tests` member, so removing `tests` from `EXPECTED_TOP_LEVEL`
changes none of the twelve existing assertions.
`test_unexpected_top_level_warns_without_failing` uses `docs/index.md` as its
unexpected entry and is likewise unaffected.

- [ ] `tests/test_sdist_contents.py` — UPDATE (additive): add the guard test
      described in Failure Path Test Strategy. Add nothing that re-asserts
      `tests` as an expected member.
- [ ] `tests/test_sdist_contents.py::test_unexpected_top_level_warns_without_failing`
      — KEEP UNCHANGED. It is the test CLAUDE.md points at as protecting the
      severity split; if a build agent finds itself editing it, that is the
      signal something went wrong.
- [ ] `tests/test_sdist_contents.py::test_build_system_floor_is_at_least_83`
      — KEEP UNCHANGED. Its floor is now load-bearing for a live `MANIFEST.in`
      rather than for a hypothetical one; the assertion is identical, only its
      docstring's justification could be sharpened.

No xfail/xpass markers exist anywhere in `tests/test_sdist_contents.py`, so
there is nothing to convert.

## Rabbit Holes

- **Chasing a `pyproject.toml`-only solution because the issue suggested one.**
  Spike-3 enumerated every `[tool.setuptools]` key; none touches sdist
  membership. The one that looks like it might, `packages.find.exclude`, governs
  wheel discovery — and popoto's is already `where = src`, so `tests/` has never
  been in the wheel. Do not add it; it would be a no-op that reads like a fix.
- **`[tool.setuptools] cmdclass` pointing at a custom `sdist` command.** This is
  technically the only pyproject-expressible route, and it is a trap: the
  cmdclass module must be importable from the *extracted sdist* for any consumer
  who builds from source, so it would itself have to ship — which requires
  either a `MANIFEST.in` (circular) or declaring it in `py-modules`, which
  installs a build helper into every consumer's `site-packages`. Strictly worse
  than the one-line `MANIFEST.in`.
- **`setuptools-scm` to make membership "whatever git tracks".** Ships `docs/`,
  `examples/`, `scripts/`, and `.github/` — the last of which
  `check_sdist_contents.py` hard-fails on. It would trade a 300-member sdist for
  a several-thousand-member one and break the release gate on the first run.
- **Making the shipped suite runnable (Option B).** Bounded only by "ship the
  whole repository". 23 shipped tests read repository paths; one of them reads
  `.github/workflows/`, which cannot legally ship. Reject it here rather than
  discovering the wall three files in.
- **Auditing the whole `tests/` tree for what a "minimal runnable subset" would
  need.** That is Option B wearing a smaller hat. The answer is not needed to
  decide, because the decision is Option A.
- **"Fixing" the stale-`SOURCES.txt` behavior from spike-5.** It is upstream
  setuptools behavior, it does not affect CI (clean checkout), and `prune tests`
  neutralizes it for this directory. Adding `src/*.egg-info` cleanup steps or a
  pre-build `rm -rf` to `release.yml` solves a problem CI does not have.
- **Touching the CHANGELOG's #678 entry beyond the one false clause.** It is a
  historical record of why the floor was raised. Correct "there is no
  `MANIFEST.in` in the repository at all" and leave the rest of the reasoning
  intact.


## Risks

### Risk 1: adding `MANIFEST.in` creates precondition 1 of the setuptools advisory
**Impact:** The advisory (#678) is an exclusion *bypass*: on setuptools<83, a
`MANIFEST.in` exclusion can be defeated by an NFC/NFD filename collision on
APFS/HFS+, so an excluded file ships anyway. Today popoto's exposure is nil on
three counts, one of which is "no `MANIFEST.in` exists at all". This change
spends that count deliberately. `check_sdist_contents.py`'s non-ASCII rule stops
being defense-in-depth and becomes the thing keeping precondition 2 away.
**Mitigation:** three, and all three are already in place — (a) the build floor
is `setuptools>=83`, the release that *fixes* the bypass, and per #694 that floor
binds every sdist build including a consumer's `--no-binary`; (b) precondition 3
is absent, releases build on `ubuntu-latest`, not APFS/HFS+; (c) precondition 2
is guarded by a hard failure that runs before publish. Additionally the single
directive excludes a directory whose *leaking* is the status quo, so a bypass
degrades to today's behavior rather than to something new. Document the
demotion of the non-ASCII rule from defense-in-depth to load-bearing in
`CLAUDE.md` — the issue asked for exactly this trade to be named, not hidden.

### Risk 2: the `MANIFEST.in` ↔ `EXPECTED_TOP_LEVEL` correspondence is hand-maintained
**Impact:** Someone later adds a `graft`/`include` to `MANIFEST.in`, the new
top-level entry is not in the allowlist, and the release prints a warning nobody
reads — or, worse, someone removes `MANIFEST.in` and `tests/` silently returns.
This is the same shape CLAUDE.md criticizes in `check_lock_imports.py`.
**Mitigation:** the guard test asserts the correspondence in both directions
(`MANIFEST.in` present in the set, `tests` absent from it) and parses the
directive rather than checking the file exists. Name the coupling in the
`check_sdist_contents.py` docstring so a reader of either file finds the other.

### Risk 3: a downstream packager was running the shipped tests
**Impact:** A distro or conda-forge recipe that runs `pytest` against the
unpacked sdist would lose that step. This is the strongest argument for keeping
tests in the sdist and it is a real ecosystem convention.
**Mitigation:** it cannot regress anyone, because the shipped suite has never
been runnable — no `conftest.py`, no `tests/__init__.py`, no subpackages, and 23
files reading paths that never shipped. Any packager doing this is already
carrying a patch or skipping collection. A CHANGELOG entry states the removal
plainly and points at `git clone` as the supported way to run popoto's suite.

### Risk 4: verifying the fix by building an sdist in a dirty working tree
**Impact:** spike-5's finding — a stale `src/popoto.egg-info/SOURCES.txt` is
re-read by `manifest_maker.add_defaults` and its 166 `tests/` entries re-added.
A reviewer who builds in the working tree without understanding this could
report either a false pass or a false failure, and the repo already has a
documented history (`CLAUDE.md`, the five worktree-verification gotchas) of
confident wrong numbers from environment drift.
**Mitigation:** every verification command in this plan builds from a fresh
`git clone`, and the Verification table says so. State the build environment
alongside any member count, per CLAUDE.md's standing rule.

## Race Conditions

No race conditions identified. Nothing in this change executes at runtime,
concurrently, or against shared state: `MANIFEST.in` is read once by
`manifest_maker` inside a single-threaded build, and `check_sdist_contents.py`
is a single-process read of a tar member list. The only ordering constraint in
scope is a *sequential* one already asserted by
`test_release_workflow_invokes_the_check_before_publishing`: the checker must run
between `python -m build` and the publish action, and this plan does not move it.

The one lane-level concurrency concern is procedural, not code: `docs/plans/` is
committed directly on the shared `main` checkout and a sibling lane (`sdlc-698`)
is writing there at the same time. Commit each plan section as soon as it is
coherent and never leave `docs/plans/` dirty across an await.


## No-Gos (Out of Scope)

Everything mechanically doable is in scope — the code change is four files and
the doc cascade is four passages, all of which this plan's tasks cover. Two
entries are genuinely outside an agent's reach:

- [ORDERED] **The fix reaches PyPI only at the next release.** PyPI artifacts are
  immutable, so 1.9.0's sdist keeps its broken `tests/` forever; nothing can
  republish it. The corrected membership ships when a human pushes a `v*` tag and
  `release.yml` runs. Gating event: a maintainer-initiated release, out of this
  PR's control.
- [EXTERNAL] **Confirming no downstream packager depended on the shipped tests.**
  Requires surveying distro/conda-forge recipes maintained by other people.
  Risk 3 argues the dependency cannot exist (the suite was never runnable), but
  proving it is a human/world action.

Deliberately *not* deferred and *not* done — these are decisions, and the plan's
position is "no", not "later":

- Promoting `check_sdist_contents.py`'s top-level rule from warning to hard
  failure. The severity split is deliberate and CLAUDE.md names it as the thing
  most likely to be simplified away. An anti-criterion in the Verification table
  asserts the top-level rule still appends to `warnings`, never `failures`.
- Adding `[tool.setuptools.packages.find] exclude = ["tests*"]`. A no-op that
  reads like a fix — the wheel already excludes `tests/` via `where = src`.
- Making the shipped test suite runnable (Option B). Rejected on evidence in the
  Solution section, not postponed.

## Update System

No update-system changes required. popoto is a library plus an mkdocs site; there
is no deployed instance and no `/update` path that propagates sdist membership.
The only propagation channel is `release.yml`, which needs no edit — the checker
invocation and its position between build and publish are unchanged.

## Agent Integration

No agent integration required. `MANIFEST.in` is consumed by setuptools during a
build; `scripts/check_sdist_contents.py` is invoked by a workflow step. Neither
is reachable from, or relevant to, popoto's MCP server or `popoto-memory` hook
surface.



## Documentation

The documentation work here is **correcting statements that this change makes
false**, not describing a new feature. Nothing in `docs/` (the published mkdocs
site) mentions the sdist, `MANIFEST.in`, or shipped tests — grepped and
confirmed — so no user-facing page is created or edited.

### Feature Documentation
- [ ] No `docs/features/` page. Sdist membership is a packaging property with no
      user-facing API; the site has no packaging section and inventing one for
      this would be out of proportion. Recorded here so the DOCS stage can see
      the decision was made rather than missed.

### External Documentation Site
- [ ] No mkdocs page changes. `mkdocs build --strict` must still pass (it is a
      CI gate) but nothing in this change touches it.

### Repository Documentation (the actual cascade — all four are required)
- [ ] `CLAUDE.md` (the `[build-system] requires` paragraph): "no `MANIFEST.in`
      exists at all" is now false. Rewrite the three-count exposure argument to
      say precondition 1 was **deliberately spent** by #689, name the remaining
      two counts (no non-ASCII path; `ubuntu-latest` builds), and state that the
      non-ASCII rule in `check_sdist_contents.py` is now load-bearing rather
      than defense-in-depth.
- [ ] `CLAUDE.md` (the `check_sdist_contents.py` paragraph): the justification
      "there is no machine-readable declaration of intended sdist membership to
      parse against, because a `MANIFEST.in` *would* be that declaration" no
      longer holds — one now exists. Keep the warning-only severity, but replace
      the reason with the one that survives: a legitimate packaging addition must
      never block a release under release pressure. Note the new
      `MANIFEST.in` ↔ `EXPECTED_TOP_LEVEL` coupling and the test that guards it.
- [ ] `CHANGELOG.md`: correct the same false clause in the #678 entry ("there is
      no `MANIFEST.in` in the repository at all"), and add an entry for #689
      stating plainly that `tests/` no longer ships in the sdist, why it never
      worked, and that the supported way to run popoto's suite is a `git clone`.
- [ ] `docs/plans/setuptools_build_floor_and_sdist_exposure.md`: mark spike-1's
      finding ("**No `MANIFEST.in` exists.**") as superseded by #689 with a
      pointer, rather than editing the historical result. That plan is an
      archive of what was true at the time.

### Inline Documentation
- [ ] `MANIFEST.in`: a comment above `prune tests` naming issue #689, why the
      directive exists, and why it is the *only* directive (every added
      exclusion rule widens the advisory's surface).
- [ ] `scripts/check_sdist_contents.py` module docstring: update the
      warning-only rationale to match the CLAUDE.md rewrite, and update the
      `EXPECTED_TOP_LEVEL` comment ("The seven top-level entries the published
      1.9.0 sdist has") — it is still seven, but they are a different seven and
      they now describe the *intended* set rather than the observed one.

## Success Criteria

- [ ] `MANIFEST.in` exists at the repo root, contains exactly one directive
      (`prune tests`) plus comments, and names #689 in a comment.
- [ ] An sdist built from a **fresh `git clone`** of the branch contains zero
      members under `tests/`.
- [ ] The same sdist's `src/` member count is unchanged from a pre-fix build of
      the same commit — the fix removes `tests/` and nothing else.
- [ ] `python scripts/check_sdist_contents.py <that sdist>` exits 0 with **zero
      warnings** (today it would print one for `MANIFEST.in`).
- [ ] `EXPECTED_TOP_LEVEL` contains `MANIFEST.in` and does not contain `tests`.
- [ ] The top-level-entry rule still appends to `warnings`, never `failures`
      (anti-criterion, asserted in the Verification table).
- [ ] The new guard test parses `MANIFEST.in`'s content — it fails against an
      empty `MANIFEST.in`, demonstrated red-state before green.
- [ ] All four documentation cascade items landed; no repository document still
      claims popoto has no `MANIFEST.in`.
- [ ] `pytest tests/test_sdist_contents.py` passes (`POPOTO_TEST_DB=9`).
- [ ] Full suite passes (`/do-test`), `ruff check src/` clean,
      `black --check src/ tests/` clean, `scripts/mypy_ratchet.py` at or below
      ceiling, `mkdocs build --strict` clean.
- [ ] Documentation updated (`/do-docs`).
- [ ] No xfail/xpass conversions needed — none exist in scope.

## Team Orchestration

Small appetite, one coherent change. Two builders would contend on
`scripts/check_sdist_contents.py`, so the packaging change and its test are one
task; the documentation cascade is genuinely separable and runs in parallel.

### Team Members

- **Builder (packaging)**
  - Name: `sdist-builder`
  - Role: `MANIFEST.in`, `EXPECTED_TOP_LEVEL`, the guard test, and the inline
    docstring updates in `scripts/check_sdist_contents.py`
  - Agent Type: builder
  - Resume: true

- **Documentarian (cascade)**
  - Name: `sdist-documentarian`
  - Role: `CLAUDE.md` (two paragraphs), `CHANGELOG.md` (one correction + one new
    entry), the superseded-finding note in the #678 plan
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `sdist-validator`
  - Role: build an sdist from a fresh clone, verify membership and checker
    output, run the Verification table, confirm no document still claims popoto
    has no `MANIFEST.in`
  - Agent Type: validator
  - Resume: true


## Step by Step Tasks

Branch: `fix/sdist-excludes-tests` (descriptive, per CLAUDE.md's naming rule).
Note for the supervisor: G8 artifact verification looks for `session/sdlc-689`
and will not find it — that is the known upstream defect `tomcounsell/ai#2765`;
do not push a decoy ref.

### 1. Capture the red state
- **Task ID**: `red-state`
- **Depends On**: none
- **Validates**: no test — this produces the paper trail the PR body needs
- **Informed By**: spike-2 (build gotcha: the repo root's `build/` dir shadows
  the module), spike-4 (checker output on a pruned sdist), spike-5 (build from a
  clean clone or the number is not evidence)
- **Assigned To**: `sdist-builder`
- **Agent Type**: builder
- **Parallel**: false
- `git clone` the branch point into a scratch directory; from a cwd *outside*
  the repo, build an sdist with a venv that has `build` installed.
- Record: total member count, per-top-level counts, and the absence of
  `tests/conftest.py`. State the environment (OS, Python, setuptools version)
  alongside every count.
- Run `python scripts/check_sdist_contents.py` on it and paste the output.
- Save both outputs for the PR description as the before half.

### 2. Add `MANIFEST.in` and re-calibrate the checker
- **Task ID**: `build-manifest`
- **Depends On**: `red-state`
- **Validates**: `tests/test_sdist_contents.py`
- **Informed By**: spike-3 (no `[tool.setuptools]` key does this — do not reach
  for `packages.find.exclude`), spike-4 (`prune tests` is the whole body; the
  file itself becomes a top-level member)
- **Assigned To**: `sdist-builder`
- **Agent Type**: builder
- **Parallel**: false
- Create `MANIFEST.in` at the repo root: a comment block naming #689 and why
  this is the only directive, then `prune tests`. Nothing else.
- In `scripts/check_sdist_contents.py`, remove `"tests"` from
  `EXPECTED_TOP_LEVEL` and add `"MANIFEST.in"`. Update the adjacent comment: the
  set is now the intended membership, not the observed 1.9.0 membership.
- Update the module docstring's warning-only rationale — the "no
  machine-readable declaration exists" reason is now false; the surviving reason
  is that a legitimate packaging addition must not block a release. Add a line
  naming the `MANIFEST.in` ↔ `EXPECTED_TOP_LEVEL` coupling and the guard test.
- Do **not** change any rule severity, and do not touch `check_members`' logic.

### 3. Add the guard test
- **Task ID**: `build-guard-test`
- **Depends On**: `build-manifest`
- **Validates**: `tests/test_sdist_contents.py`
- **Informed By**: the #661 vacuity trap — a guard that passes against an empty
  file is not a guard
- **Assigned To**: `sdist-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add a test that reads `MANIFEST.in`, strips comments and blank lines, and
  asserts the remaining directive set is exactly `{"prune tests"}`.
- Assert `"tests" not in checker.EXPECTED_TOP_LEVEL` with a message explaining
  that re-adding it would re-bless the #689 defect.
- Assert `"MANIFEST.in" in checker.EXPECTED_TOP_LEVEL`, so the release stops
  printing a spurious warning and a future removal of the entry is caught.
- Add a synthetic-tarball case (reusing `_make_sdist`) whose members are the
  post-fix top-level set, asserting `check_members` returns **zero** warnings
  and zero failures.
- **Red-state proof:** before finishing, temporarily blank `MANIFEST.in` and
  confirm the directive test FAILS; paste that output into the PR. Restore.

### 4. Documentation cascade
- **Task ID**: `docs-cascade`
- **Depends On**: `build-manifest`
- **Validates**: no test; `mkdocs build --strict` must still pass
- **Informed By**: the Documentation section's four required items
- **Assigned To**: `sdist-documentarian`
- **Agent Type**: documentarian
- **Parallel**: true (no file overlap with tasks 2-3)
- `CLAUDE.md`: rewrite the "exposure was nil on three counts" clause and the
  `check_sdist_contents.py` warning-only justification, per the Documentation
  section. Keep the #678 reasoning; change only what became false.
- `CHANGELOG.md`: correct the false clause in the #678 entry; add a #689 entry.
- `docs/plans/setuptools_build_floor_and_sdist_exposure.md`: append a
  superseded-by-#689 note to spike-1's finding; do not rewrite the finding.
- Do not create a `docs/features/` page (see Documentation section).

### 5. Validation
- **Task ID**: `validate-all`
- **Depends On**: `build-manifest`, `build-guard-test`, `docs-cascade`
- **Assigned To**: `sdist-validator`
- **Agent Type**: validator
- **Parallel**: false
- Build an sdist from a **fresh clone of the branch** and confirm zero `tests/`
  members and an unchanged `src/` count versus the red-state build.
- Additionally build once in a **dirty tree that has a stale
  `src/popoto.egg-info/SOURCES.txt` listing `tests/` entries**, to confirm
  spike-5's reasoned claim that `prune` also defeats the stale-manifest re-add.
  This is the one assertion in the plan carried by reasoning rather than
  measurement; close it here.
- Run the Verification table. Run `pytest tests/test_sdist_contents.py` and the
  full suite with `POPOTO_TEST_DB=9`, stating the environment with the counts.
- Confirm no repository document still claims popoto has no `MANIFEST.in`.

#### 5a. `scripts/verify/sdist_excludes_tests.sh` — full specification

The critique's BLOCKER was that this script, as previously described, could not
run on this machine and its Verification rows would have reported the failure as
a pass. It is **kept** (the anti-criterion has to live somewhere, and the repo's
`scripts/verify/` convention is where) but it is now specified rather than
gestured at. It is heavier than `no_new_deps.sh`'s one-liner; that cost is
accepted and priced into the Appetite section, because the alternative is
inlining the same clone-build-count into two Verification rows.

Required behavior, in order:

1. `#!/bin/sh` then `set -eu`. Every failure must abort with a non-zero exit.
   A silent abort that prints nothing is exactly the false pass the critique
   found.
2. `TMP=$(mktemp -d)` and `trap 'rm -rf "$TMP"' EXIT`.
3. Clone `HEAD` into `$TMP/clone`, then **guard against a stale clone**: assert
   `git -C "$TMP/clone" rev-parse HEAD` equals `git -C <repo> rev-parse HEAD`,
   and exit non-zero if not. A clone of the wrong commit is a false oracle in
   both directions.
4. Resolve the `build` frontend on demand: `python3 -m venv "$TMP/venv"` then
   `"$TMP/venv/bin/python" -m pip install --quiet build`. Do **not** use
   `.venv/bin/pip` (it does not exist) and do **not** assume the ambient
   interpreter can `import build` (it cannot, and from the repo root the import
   succeeds vacuously against the untracked `build/` directory).
5. Choose the isolation branch from the *measured* setuptools version of the
   venv that will run the build, using the Prerequisites-table command. Pass
   `--no-isolation` only when it prints `1`; otherwise take the isolated
   (network) build. Measured on this machine: `81.0.0` → isolated.
6. Build with cwd **outside** the clone: `"$TMP/venv/bin/python" -m build
   --sdist --outdir "$TMP/dist" "$TMP/clone"`, invoked from `$TMP`.
   Exactly one tarball must land in `$TMP/dist`; zero or two is a hard error
   (the same rule `check_sdist_contents.py` applies to its own argv).
7. **Default mode** (no flags): print only the count of tar members whose path
   is under `tests/`, and nothing else, then exit 0. The expected value is `0`.
8. **`--run-checker` mode**: after the build, run
   `python scripts/check_sdist_contents.py "$SDIST"` (the checker from the
   *clone*, not the working tree) and emit its stdout **verbatim** so that
   `grep -c WARNING` sees it. Propagate the checker's exit status. Do not print
   the member count in this mode — the two modes must not share an exit-code
   meaning or a stdout shape.
9. Any unrecognized flag is a hard error, not a silent fallthrough to default
   mode.

Every caller — including both Verification rows — must assert the script's
**exit code as well as** its output. A script that dies before building prints
nothing, and `grep -c` on nothing is `0`, which is indistinguishable from
success.

## Verification

All commands run from the repository root. The sdist row shells out to a script
that builds from a **temporary clone**, never from the working tree — spike-5
showed a stale `src/popoto.egg-info/SOURCES.txt` in a dirty tree re-adds
`tests/` entries, which would make a working-tree build a false oracle in both
directions.

| Check | Command | Expected |
|-------|---------|----------|
| MANIFEST.in prunes tests | `grep -c '^prune tests$' MANIFEST.in` | output > 0 |
| MANIFEST.in has exactly one directive | `python -c "import pathlib; print(sum(1 for l in pathlib.Path('MANIFEST.in').read_text().splitlines() if l.strip() and not l.strip().startswith('#')))"` | output contains 1 |
| Checker expects MANIFEST.in | `grep -c '"MANIFEST.in"' scripts/check_sdist_contents.py` | output > 0 |
| Checker no longer blesses tests | `python -c "import importlib.util as u; s=u.spec_from_file_location('c','scripts/check_sdist_contents.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(int('tests' in m.EXPECTED_TOP_LEVEL))"` | match count == 0 |
| Severity split intact (anti-criterion) | `awk '/for entry in sorted/,/return failures, warnings/' scripts/check_sdist_contents.py \| grep -c 'failures.append'` | match count == 0 |
| Sdist ships no tests (anti-criterion) | `sh scripts/verify/sdist_excludes_tests.sh` | match count == 0 |
| Sdist check is warning-free | `sh scripts/verify/sdist_excludes_tests.sh --run-checker \| grep -c WARNING` | match count == 0 |
| CLAUDE.md no longer claims no MANIFEST.in | `grep -c 'exists at all' CLAUDE.md` | match count == 0 |
| CHANGELOG no longer claims no MANIFEST.in | `grep -c 'in the repository at all' CHANGELOG.md` | match count == 0 |
| Sdist guard tests pass | `POPOTO_TEST_DB=9 python -m pytest tests/test_sdist_contents.py -q` | exit code 0 |
| Full suite passes | `POPOTO_TEST_DB=9 python -m pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Types at or below ceiling | `scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |
| No stale xfails introduced | `grep -rn 'xfail' tests/test_sdist_contents.py` | exit code 1 |

Two rows are anti-criteria and must be shown failing before they pass, with the
FAIL output pasted into the PR description:

- *Severity split intact* — temporarily move the top-level rule's append from
  `warnings` to `failures` and confirm the row FAILS.
- *Sdist ships no tests* — run it at the branch point (before `MANIFEST.in`
  lands) and confirm it FAILS with a non-zero count. spike-4 already produced
  the equivalent output for the checker row:
  `WARNING: 'MANIFEST.in': unexpected top-level entry ... OK: ... 1 warning(s)`.

## Critique Results

War room run 2026-09-07, FULL depth, independent roster (3 critics: Risk &
Robustness, Scope & Value, History & Consistency). Verdict: **NEEDS REVISION**
(1 blocker). Environment for every measured claim below: macOS 25.6 (APFS),
`/Users/valorengels/src/popoto/.venv` on Python 3.12, setuptools 81.0.0,
`build` frontend **not installed**.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Risk & Robustness (converged: Scope & Value, History & Consistency all flagged the same script) | Task 5's `scripts/verify/sdist_excludes_tests.sh` cannot run in this repo's environment, and its Verification rows would report the failure as a pass. Measured: `.venv` has setuptools **81.0.0**, below the `>=83` the plan's `--no-isolation` branch requires, so it must fall back to an isolated (network) build — and the `build` frontend is **not installed** in `.venv` (`python -c "import build"` from `/tmp` → `ModuleNotFoundError`). The Prerequisites row for `build` is therefore unmet today. Worse, `import build` *succeeds vacuously from the repo root* because the untracked `build/` directory resolves as a namespace package (`_NamespacePath(['/Users/valorengels/src/popoto/build'])`, `__file__ is None`) — so the naive prerequisite probe self-confirms. Both anti-criterion rows that carry the plan's central assertion (`Sdist ships no tests`, `Sdist check is warning-free`) are `grep -c` on the script's **stdout** with no exit-code check, so a script that dies before building prints nothing and reads as `0` — a false pass indistinguishable from success. | Task 5 (`validate-all`), Prerequisites table, Verification table rows "Sdist ships no tests" / "Sdist check is warning-free" | The script must start `set -eu` and probe rather than assume: `python -c "import build" 2>/dev/null` is **not** a valid probe from the repo root — check `python -c "import build,sys; sys.exit(0 if getattr(build,'__file__',None) else 1)"`, or simply always run it from a cwd outside the repo. Resolve `build` on demand into a throwaway venv (`python -m venv $TMP/venv && $TMP/venv/bin/pip install build`) — **not** `.venv/bin/pip`, which does not exist (`No module named pip`; the venv is uv-managed). Choose the isolation branch off the *measured* value, not an assumption: `python -c "import setuptools; from packaging.version import Version; print(int(Version(setuptools.__version__) >= Version('83')))"` → `--no-isolation` only on `1`, else isolated. Every Verification row invoking the script must assert **exit code 0 AND** the count, e.g. `sh scripts/verify/sdist_excludes_tests.sh && test "$(sh scripts/verify/sdist_excludes_tests.sh)" = "0"`, so an aborted run can never be read as zero shipped tests. |
| CONCERN | History & Consistency | The Verification row `sh scripts/verify/sdist_excludes_tests.sh --run-checker \| grep -c WARNING` requires a `--run-checker` mode that Task 5 never specifies — Task 5 only asks the script to "print the count of members under `tests/`". A builder implementing Task 5 to spec produces a script that makes that row error. | Task 5 (`validate-all`) | Add the mode to Task 5 explicitly: with `--run-checker`, after building the sdist from the temp clone, invoke `python scripts/check_sdist_contents.py "$SDIST"` and emit its stdout verbatim (so `grep -c WARNING` sees it), still exiting non-zero on checker failure. Without the flag, print only the `tests/` member count. Do not let the two modes share an exit-code meaning. |
| CONCERN | History & Consistency | The four-item CLAUDE.md/CHANGELOG cascade misses a fifth stale clause. `CLAUDE.md:81` says the warning-only rule covers "a top-level entry outside the seven **the 1.9.0 sdist has**" — an *observed-membership* framing that this change replaces with an *intended-membership* one (the seven become a different seven). The plan flags exactly this phrase for correction in `check_sdist_contents.py`'s inline comment but never in CLAUDE.md, and no Verification grep row would catch it left stale. | Task 4 (`docs-cascade`), Documentation section, Verification table | Add to the `CLAUDE.md` bullet: rewrite the clause at `CLAUDE.md:81` from "outside the seven the 1.9.0 sdist has" to name the *intended* set declared by `MANIFEST.in` + `EXPECTED_TOP_LEVEL`. Add a Verification row `grep -c 'the 1.9.0 sdist has' CLAUDE.md` → match count == 0. Note the same phrase also appears in `scripts/check_sdist_contents.py`'s `EXPECTED_TOP_LEVEL` comment ("The seven top-level entries the published 1.9.0 sdist has"), which Task 2 already covers — the grep must be scoped to CLAUDE.md or it will match the script too. |
| CONCERN | Risk & Robustness | The "Severity split intact" anti-criterion is a source-text scan (`awk '/for entry in sorted/,/return failures, warnings/' … \| grep -c 'failures.append'`) anchored on two exact phrases in `scripts/check_sdist_contents.py`. This is the brittle grep-the-source shape CLAUDE.md already criticizes (`test_transfer_roundtrip.py`, `test_validity_field.py`): if either anchor is reworded the awk range empties and the row prints `0` — "split intact" — vacuously. Measured: the row already outputs `0` on unmodified `main`. | Verification table, Task 3 (`build-guard-test`) | Back the row with a behavioral assertion rather than replacing it: in `tests/test_sdist_contents.py`, build a synthetic sdist with an unexpected top-level entry and assert `failures == []` **and** `warnings != []` from `checker.check_members(...)` — `test_unexpected_top_level_warns_without_failing` already has this shape, so extend it rather than adding a parallel test. Keep the awk row only as the red-state demo the plan already mandates (move the append from `warnings` to `failures`, confirm FAIL). |
| CONCERN | Scope & Value | `scripts/verify/sdist_excludes_tests.sh` (clone to temp dir, build an sdist from a cwd outside the repo, count members) is heavy for a Small appetite next to the repo's own convention — `scripts/verify/no_new_deps.sh` is a one-line `git diff \| grep -c` — and it re-implements the clean-clone build that Task 1's red-state capture and spikes 2/4/5 already perform. | Task 5 (`validate-all`), Appetite | If the script is kept, it must guard against the stale-clone false pass: assert `git -C "$CLONE" rev-parse HEAD` equals the branch HEAD before building, and build from a cwd outside the clone (spike-2's `build/` namespace-shadowing gotcha — see the BLOCKER's probe note). If it is dropped, the Verification rows must inline the clone-build-count so the anti-criterion still exists somewhere; do not delete the rows along with the script. |
| CONCERN | Scope & Value | No-Gos and Open Question 3 give contradictory registers for the same decision. No-Gos lists promoting the top-level rule to a hard failure as a decision where "the plan's position is 'no', not 'later'", while OQ3 re-opens it as a live PM check-in — and only "1-2 PM check-ins" are budgeted. A builder has no signal whether Task 2's "do not change any rule severity" is settled or pending. | No-Gos, Open Questions, Task 2 (`build-manifest`) | Collapse to one source of truth. If No-Gos is authoritative, delete OQ3 and cite the No-Go from it. If OQ3 is a real gate, add an explicit `Blocked On: OQ3` line to Task 2 so the builder waits for the check-in instead of guessing — do not leave both standing, because a builder resolving the ambiguity by reading No-Gos will silently answer a question the PM was asked to decide. |
| NIT | Scope & Value | Every Verification row is a mechanical grep or member count; none installs the built sdist, despite the plan quoting CLAUDE.md's own doctrine that a green checker run "says nothing about whether the packaged code works". | Verification table | Optional row: `pip install --no-deps dist/popoto-*.tar.gz && python -c "import popoto"` in a scratch venv. `--no-deps` keeps it within the Small appetite. |

---

## Open Questions

1. **Confirm the fork: stop shipping `tests/` rather than making them
   runnable.** The plan picks "stop shipping" on evidence (23 shipped tests read
   repository paths that no sdist can contain, one of them a dotfile path the
   release gate hard-fails on). The counter-argument is a real ecosystem
   convention — distro and conda-forge packagers expect to run a project's tests
   from the sdist. Is there any downstream packaging relationship that would
   make Option B worth its unbounded scope?

2. **Is spending advisory-precondition 1 acceptable?** #678 recorded popoto's
   exposure as nil on three independent counts and raised the build floor
   *because* the preconditions were "absent-but-returnable". This change
   deliberately returns one of them, relying on the `setuptools>=83` floor that
   fixes the bypass and on `check_sdist_contents.py`'s non-ASCII rule — which is
   promoted from defense-in-depth to load-bearing. The issue itself flags this
   trade. Accept and document, or is there appetite for a route that avoids it
   (there is no cheap one — see spike-3 and Rabbit Holes)?

3. **Should the top-level-entry rule stay warning-only?** The plan says yes and
   makes it an anti-criterion, because CLAUDE.md names the severity split as the
   thing most likely to be simplified away. But CLAUDE.md's *stated reason* for
   warning-only — "there is no machine-readable declaration of intended sdist
   membership to parse against, because a `MANIFEST.in` would be that
   declaration" — is exactly what this change invalidates. Keep the severity and
   rewrite the reason (the plan's position), or is the disappearance of that
   reason the moment to promote it to a hard failure?
