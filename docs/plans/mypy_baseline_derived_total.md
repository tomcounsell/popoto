---
status: Planning
type: bug
appetite: Small
tracking: https://github.com/tomcounsell/popoto/issues/677
---

# mypy baseline: derive the total instead of storing it

## Problem

`scripts/mypy_baseline.json` stores per-package error counts **and** a `total` that must equal their
sum. The two are separate lines encoding the same fact, and git's line-based three-way merge has no
notion of the arithmetic relationship between them. Two PRs that improve *different* packages
therefore merge with no textual conflict and no warning into a file whose stored `total` is correct
for neither parent.

This is not hypothetical — it happened on PR #675 this morning:

| | `fields` | `recipes` | `total` |
|---|---|---|---|
| merge base `4e0dc9ed` | 423 | 147 | 1042 |
| `main` after #661 (`3b7b6a0a`) | 423 | **145** | 1040 |
| branch `session/sdlc-556` | **421** | 147 | 1040 |
| **git's merge** | **421** | **145** | **1040** ← sum is 1038 |

**The ratchet's own gate does not catch this.** `scripts/mypy_ratchet.py` compares the measured total
against the stored `data["total"]`. On that merge it reads 1040, measures 1038, and reports "at or
below baseline" — a pass. The failure mode is a **silently loosened ceiling**: a merge can raise the
stored `total` above the true sum and quietly grant headroom for regressions no single PR asked for.

The only thing that detects it today is `tests/test_mypy_ratchet.py:344`
(`assert sum(data["packages"].values()) == data["total"]`), which fires *after* the bad file exists
and catches it by arithmetic rather than by measurement. Exposure grows with lane parallelism, and
`fields`/`models`/`recipes` are the three most-touched entries.

## Freshness Check

**Disposition: Unchanged.** Baseline commit `7e738933` (current `origin/main`).

- `tests/test_mypy_ratchet.py:344` — re-read; still exactly
  `assert sum(data["packages"].values()) == data["total"]`. Reference has not drifted.
- **#675 merged at 08:37:34Z, after the issue was filed at 08:01:57Z, and it touched
  `scripts/mypy_baseline.json`** (commit `bbd8297a`, the only commit to touch the file or the ratchet
  script since). It applied the *workaround*, not the fix: merged main and re-ran `--update`, moving
  `fields` 423→421 and `total` 1040→1038. Its own commit message names the general hazard and defers
  it — this issue is that deferral. The committed file is currently self-consistent (total 1038 =
  sum 1038), so there is no live corruption to repair, only a schema to fix.
- #661 (merged 03:56:15Z) and #556 (closed) are the two parents in the table above; both landed
  before the issue was filed and neither changed the root cause.
- The defect is structural and fully present: nothing in the file's schema or the ratchet's read path
  has changed. Reproduced synthetically in spike-1 rather than by re-corrupting main.
- No active plan in `docs/plans/` touches the ratchet or the baseline. No overlap.

## Research

Skipped — no external libraries, APIs, or ecosystem patterns are involved. The change is confined to
one repo-internal script, its test file, and a JSON file only that script reads.

## Prior Art

- **#506** (closed) — created the ratchet and this baseline file. Established `total` as a *ceiling*,
  not an equality: the gate fails only when the measured count is **above** it. That framing is why
  a too-high stored total is a silent pass rather than an error, and it is the property this plan
  must preserve.
- **#675 / #556** (merged) — the PR that hit the defect. Resolution was to merge main and re-bank,
  which was correct for that PR and is not a fix for the class.
- **#661** (merged) — the other parent; independently improved `recipes`.
- **#663, #659** (closed) — recent work on `setup.cfg` mypy configuration. Both moved *what mypy
  measures*, neither touched the baseline schema or the ratchet's comparison logic.

No prior attempt has been made at this defect, so there is no **Why Previous Fixes Failed** section.

No `xfail`/`pytest.xfail()` markers exist anywhere in `tests/test_mypy_ratchet.py`, so there are no
expected-failure tests to convert.

## Spike Results

## Data Flow

## Solution

## Rabbit Holes

## Risks

## Step by Step Tasks

## Success Criteria

## No-Gos

## Open Questions
