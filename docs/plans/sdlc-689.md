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

_placeholder_

## Data Flow

_placeholder_

## Why Previous Fixes Failed

_placeholder_

## Architectural Impact

_placeholder_

## Appetite

_placeholder_

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
