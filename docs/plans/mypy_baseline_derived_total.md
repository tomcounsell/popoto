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

### spike-1: does deriving the total actually fix the merge, or just move the problem?

- **Assumption**: "If `total` were not stored, git's merge of the #675 scenario would produce a
  correct, internally-consistent file."
- **Method**: prototype (synthetic three-way merge in an isolated worktree, real `git merge-file`)
- **Result**: **Confirmed, and the merged number is not merely consistent but correct.**
  - *With* `total`: merges cleanly (exit 0), yields `fields 421, recipes 145, total 1040`, sum 1038 —
    exactly reproduces the #675 defect.
  - *Without* `total`: merges cleanly, yields the same package counts, and `sum(packages.values())`
    evaluates to **1038 — the value the merged tree genuinely measures when mypy is run.**
  - *Both sides edit the same key* (`fields` 423→420 vs 423→421): git **conflicts loudly**, exit 1,
    with real `<<<<<<<`/`=======`/`>>>>>>>` markers on the `fields` line. It does not silently pick a
    winner.
- **Confidence**: high
- **Impact if false**: would have forced option 3 (a `.gitattributes` merge driver).

The reason this works is the load-bearing insight of the plan: **per-package counts compose
additively under a line merge; a total does not.** Each branch's package edit is independently
correct, so taking both is correct. A total is a function of *all* packages, so taking one branch's
copy is correct only if the other branch changed nothing.

### spike-2: is the stored total redundant, i.e. does deriving it lose information?

- **Assumption**: "`sum(packages.values()) == total` is a guaranteed invariant of measurement, so
  deriving it at read time loses nothing."
- **Method**: code-read of `scripts/mypy_ratchet.py` and `tests/test_mypy_ratchet.py` in full
- **Result**: **Yes for the *stored* total — but the spike found a distinction that changes the
  design.** There are two different totals and only one is redundant:
  1. **Parse-time total** (`mypy_ratchet.py:117-120`) is read from mypy's own `Found N errors`
     summary line and cross-checked against `len(error_lines)` at `:138`, raising `RatchetError` on
     disagreement. This is an *independent* source and a genuine safety property — it catches a
     parser that silently stops matching. `tests/test_mypy_ratchet.py:137`
     (`test_parse_disagreement_between_parser_and_summary_is_a_failure`) exists precisely to pin it.
     **Deriving this one would delete the guard the module docstring calls out**: *"A gate that
     passes because the checker did not run is worse than no gate."*
  2. **Stored total** (the JSON field) is written from the same `parse_output` return tuple as
     `packages` (`:234` → `:248`), so the two cannot disagree at write time. This one is pure
     redundancy.
- Every error line increments exactly one bucket — `ROOT_BUCKET` is a real bucket that is summed,
  not a discard — so `total == len(error_lines) == sum(packages.values())` holds by construction for
  any machine-written baseline.
- Load-bearing read sites of the stored total are only `base_total = baseline["total"]` (`:292`) and
  the two comparisons at `:293`/`:326`. `:154` validates it; `:259` and `:344` are print-only.
- **`reference.redis==7.1.1: 1172` is never read by the script at all** (no match for `reference`),
  is self-described as non-gating, and has no per-package breakdown to be redundant *with*. Out of
  scope.
- Nothing outside `mypy_ratchet.py` reads the file: `lint.yml:122` and `ci-local.sh:267` both just
  invoke the script.
- **Condition on the invariant**: it holds unconditionally for machine-written baselines, but
  `load_baseline` (`:146-164`) never cross-checks the two. A hand-edited or badly-merged file is
  accepted by the script today; only the test at `:344` rejects it.
- **Confidence**: high
- **Impact if false**: would have forced option 2 (keep storing, validate on load).

**A third finding the spike surfaced, not in the issue:** `load_baseline` validates `total`'s type
and non-negativity (`:154-158`) but **never validates `packages` at all** — it is read with
`baseline.get("packages", {})` at `:299`, purely for diagnostics. The schema validates the field this
plan is deleting and does not validate the field it is deriving from. That inversion has to be fixed
in the same change or the derivation rests on an unvalidated input.

## Data Flow

The defect does not live in any single function — it lives in the hand-off between a measurement and
a *git merge*, which is why reading either branch in isolation shows nothing wrong.

```
mypy src/  ──►  parse_output()          total (from mypy's summary) ⟵cross-check⟶ len(error_lines)
                                        packages{} (every error line → exactly one bucket)
                     │
                     ▼  (--update)
              write_baseline()          writes BOTH from the same tuple — consistent by construction
                     │
                     ▼
          scripts/mypy_baseline.json    ← two lines encoding one fact
                     │
                     ▼
             ***git three-way merge***  ← THE DEFECT: merges the two lines independently.
                     │                     Package lines compose; the total line does not.
                     ▼
              load_baseline()           validates `total`, does NOT validate `packages`,
                     │                  does NOT cross-check them
                     ▼
                 main()                 base_total = data["total"]  ← trusts the merged-wrong number
                                        measured > base_total ? fail : pass  ← silent pass at 1038<1040
```

Every arrow except the merge is consistent by construction. The fix must therefore act on the *file
schema* — remove the second representation — rather than on any comparison, because no comparison
runs between the merge and the gate.

## Solution

## Rabbit Holes

## Risks

## Step by Step Tasks

## Success Criteria

## No-Gos

## Open Questions
