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

## Rabbit Holes

_placeholder_

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
