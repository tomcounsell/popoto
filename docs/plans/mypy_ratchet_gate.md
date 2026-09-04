---
status: Ready
type: chore
appetite: Medium
owner: valorengels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/506
last_comment_id: IC_kwDOExCOnM8AAAABNtpffA
---

# Mypy Ratchet Gate

## Problem

`setup.cfg` declares a strict mypy configuration for `src/` — `disallow_untyped_defs`,
`disallow_any_generics`, `check_untyped_defs`, `warn_unused_ignores`, `strict_optional`.
Nothing runs it. There is no mypy job in any workflow, no mypy gate in
`scripts/ci-local.sh`, and no `[tool.mypy]` section in `pyproject.toml`. The config is a
decoration.

The consequence is a config that lies in both directions. A contributor reading `setup.cfg`
believes annotations are required; a contributor running `mypy src/` discovers four figures
of errors and concludes the config is aspirational, which it is. Neither belief is
actionable, and there is nothing to stop the number from growing on every merge. It has:
PR #532 added two errors, PR #558 shipped a package whose type errors were waived in a PR
comment (that waiver is now #572).

**Current behavior:**

```
$ .venv/bin/mypy src/
Found 1178 errors in 71 files (checked 98 source files)
```

Measured at `0dbce759` under Python 3.12.13, mypy 2.1.0, redis-py 7.1.1. Every count in this
plan is environment-bound; see the environment matrix below and CLAUDE.md's worktree
hazard 5.

By error code:

| Code | Count |
|---|---|
| `no-untyped-def` | 574 |
| `type-arg` | 185 |
| `assignment` | 125 |
| `attr-defined` | 104 |
| `arg-type` | 78 |
| `var-annotated` | 24 |
| `union-attr` | 23 |
| `misc` | 22 |
| `return-value` | 13 |
| `index` | 12 |
| `operator` | 10 |
| `has-type` | 5 |
| `call-overload` | 2 |
| `method-assign` | 1 |

By package:

| Package | Errors |
|---|---|
| `fields/` | 457 |
| `models/` | 429 |
| `recipes/` | 154 |
| `transfer/` | 49 |
| (top-level modules) | 36 |
| `utils/` | 20 |
| `pubsub/` | 14 |
| `streams/` | 10 |
| `embeddings/` | 4 |
| `stores/` | 2 |
| `extraction/` | 2 |
| `integrations/` | 1 |
| `privacy/` | 0 |

Two files carry a third of the total: `models/base.py` (205) and `models/query.py` (164).
Exactly one package, `privacy/`, is already clean. 26 individual files are clean.

**Desired outcome:**

`mypy src/` runs in CI on every PR touching `src/`, and the error count can only go down.
The strict `setup.cfg` stays as written, because it describes where the code is going. A
checked-in baseline number describes where the code is. A per-package allowlist names the
packages that have already reached zero and pins them there, so a paid-down package cannot
silently re-accrue errors. Bumping mypy or redis-py becomes a deliberate re-baseline commit
rather than a surprise red build.

## Freshness Check

**Baseline commit:** `0dbce75917ec7d4db79a5de6908d1f980b5ee9eb`
**Issue filed at:** 2026-08-06T08:14:24Z
**Disposition:** Minor drift

**File:line references re-verified:**

- `src/popoto/stores/filesystem.py:38` — issue claims "Incompatible default None for str".
  Still holds, verbatim, at line 38: `base_path` declared `str` with a `None` default.
- `src/popoto/embeddings/voyage.py:41` — issue claims the same shape for `api_key`. Still
  holds at line 41. The sibling `src/popoto/embeddings/openai.py:39` has the identical defect
  and is not named in the issue; it belongs in the same fix.
- `src/popoto/pubsub/publisher.py:156-158` — issue claims "Optional defaults for
  Pipeline/dict/str". Still holds at exactly 156 (`data: dict`), 157 (`channel_name: str`),
  158 (`pipeline: Pipeline`).
- `src/popoto/fields/auto_field_mixin.py:306` — issue claims `Name "Model" is not defined`
  (F821). **Gone.** The current run reports zero `name-defined` errors anywhere in `src/`.
  Commit `926953c` ("chore(#505): ruff config + CI lint gate, 42 lint errors fixed") fixed
  the undefined names in string annotations that produced this class. The nearest surviving
  error in that file is a different defect at line 322 (implicit-Optional `Pipeline`).
- Issue body claims "~1150 error lines" / "1152 lines". Not reproducible as an absolute:
  measured 1126–1183 across four plausible tool-version combinations (matrix below). The
  issue's figure is inside that band and its *argument* holds; the number itself is not a
  constant and this plan never treats it as one.

**Cited sibling issues/PRs re-checked:**

- #572 — open. "Export/import: type-cleanliness of `src/popoto/transfer/` (waived mypy
  errors)". Its plan `docs/plans/transfer_type_cleanliness.md` is status Ready and explicitly
  defers all gating to #506: its Rabbit Holes name "Adding mypy to `scripts/ci-local.sh` or a
  CI workflow" as out of scope, and its No-Gos tag the repo-wide decision `[SEPARATE-SLUG
  #506]`. The two plans are complementary by design. Coordination is required on the
  baseline number only; see Risk 3.
- #532 / #521 — merged as v1.8.2. The `/do-docs` comment on #506 (2026-08-07) reported it
  added two `no-untyped-def` errors at `encoding.py:78` and `:115`. Re-verified: both
  `_decode_datetime` and `_decode_time` still take a bare `obj`, and `encoding.py` carries 16
  errors today. The comment's suggested two-line paydown is still available but is not this
  plan's business — the ratchet's job is to make it visible, not to pre-empt it.
- #505 / PR #542 — merged. Created `.github/workflows/lint.yml` and the `[tool.ruff.lint]`
  config. This is the direct precedent for how a gate is introduced and how its tool version
  is pinned in this repo, and this plan follows it.
- #579 / PR #607 — merged 2026-09-04. Added the `black` job to the same workflow, pinning
  `black==26.5.1` for the same stated reason. Confirms the pin-the-tool convention is current
  practice, not a one-off.

**Commits on main since issue was filed (touching referenced files):**

`git log --since=2026-08-06T08:14:24Z -- setup.cfg pyproject.toml .github/workflows/lint.yml
scripts/ci-local.sh` returns 8 commits.

- `926953c` chore(#505) ruff config + CI lint gate — **changed the landscape.** Created
  `lint.yml` (which this plan extends rather than creating a new workflow) and removed the
  `name-defined` error class the issue's acceptance criteria named.
- `a6d9f43` chore(#578,#579) gate black in CI — added the second job to `lint.yml`,
  establishing the multi-job shape this plan adds a third job to.
- `3dbecca` fix(#523) sync uv.lock and gate it — created `lock-check.yml`. Relevant: any
  change to `pyproject.toml`'s dev extra in this plan must be accompanied by a `uv lock`
  refresh or `lock-check.yml` fails.
- `8ea5416`, `e220b2e`, `16aa702`, `3199998`, `89b640f` — touched `pyproject.toml` for
  version bumps, dependency extras, and metadata. None touched `setup.cfg`'s `[mypy]` block,
  which is byte-unchanged since before the issue was filed.

**Active plans in `docs/plans/` overlapping this area:**
`docs/plans/transfer_type_cleanliness.md` (#572, status Ready). Overlap is deliberate and
already negotiated in both directions: #572 pays down one package and refuses to touch
gating; this plan builds the gate and refuses to touch `transfer/`. The only shared artifact
is the baseline number.

**Notes:** The one substantive drift is that a quarter of the issue's named acceptance
criteria (the `auto_field_mixin.py:306` F821) was fixed by an unrelated PR. The remaining
three named sites are real and are fixed here. The premise of the issue — declared but
unenforced type gates — is entirely intact.

## Prior Art

- **#505 / PR #542 (merged)**: "ruff config + CI lint gate, 42 lint errors fixed". Identical
  problem shape one tool over: `ruff` was declared in the dev extra, never configured, never
  run, and 42 errors had accumulated. The resolution was to configure a narrow rule set
  (`E4,E7,E9,F`), fix everything it flagged, and gate at zero. It succeeded because 42 errors
  is fixable in one PR. That is the load-bearing difference here: 1126 is not.
- **#579 / PR #607 (merged)**: "gate black in CI". Same pattern again, and its workflow
  comment states the reasoning this plan borrows verbatim for the mypy pin — an unpinned
  `latest` "turns an unrelated PR red".
- **#572 (open, planned)**: "Export/import: type-cleanliness of `src/popoto/transfer/`". The
  first per-module paydown increment. Its existence is what makes the allowlist half of this
  gate immediately useful rather than theoretical: `privacy/` alone is a one-entry list;
  `privacy/` + `transfer/` is a ritual.
- **#554 / PR #558 (merged)**: shipped `src/popoto/transfer/` with a Verification row
  ("Types clean: exit code 0") that the merged code did not satisfy. The row was waived in a
  PR comment. This is the concrete failure mode the gate exists to prevent: a type promise
  discharged by prose.
- No prior attempt to gate mypy exists. `gh issue list --state closed --search "mypy"`
  returns nothing, and no merged PR has touched `setup.cfg`'s `[mypy]` block.

## Research

No relevant external findings — proceeding with codebase context and training data. The work
is a CI wiring change using a tool the repo already depends on, following two in-repo
precedents (#505, #579) that are better evidence than anything external. The one external
fact that matters — that mypy's error output is not stable across mypy and stub versions — was
measured directly rather than researched (see Spike Results).

## Spike Results

### spike-1: Is the error count stable enough across plausible CI environments to gate on a bare integer?

- **Assumption**: "The count varies with redis-py major (CLAUDE.md hazard 5), but the
  variance is small enough that a single baseline number tolerates it."
- **Method**: prototype — four throwaway venvs outside the repo, same source tree at
  `0dbce759`, same `setup.cfg`, varying only the two tool versions.
- **Finding**: **False. The variance is 57 errors, and it moves in both dimensions.**

  | | redis-py 7.1.1 | redis-py 8.1.0 |
  |---|---|---|
  | **mypy 1.19.1** | 1183 | 1129 |
  | **mypy 2.1.0** | 1178 | 1126 |

  The redis-py major is worth ~53 errors, confirming CLAUDE.md's note quantitatively for the
  first time. The mypy minor is worth ~4. A bare baseline integer is meaningless without both
  pins.

  A second finding fell out of the setup: **CI does not install from `uv.lock`.** Both jobs in
  `tests.yml` and the jobs in `lint.yml` run `pip install -e ".[dev,...]"` or `pip install`
  with an explicit pin. `uv.lock` is only validated for internal consistency by
  `lock-check.yml` (`uv lock --check`); nothing installs from it. Today `pyproject.toml`
  floors mypy at `>=0.971` and redis at `>=4.4.4`, so a CI mypy job that simply installed the
  dev extra would resolve floating latest for both and re-baseline itself on every upstream
  release. `uv.lock` currently resolves mypy 1.19.1 and redis 7.1.1 — neither matches what a
  fresh `pip install` gets today.
- **Confidence**: high — reproduced the primary checkout's 1178 exactly in a lean venv
  containing only mypy, redis, numpy, msgpack, tiktoken, and mcp, which also proves the
  heavier optional extras (sentence-transformers, torch) do not affect the count.
- **Impact on plan**: The workflow job must install explicit `==` pins for both mypy and
  redis-py, exactly as `lint.yml` already does for ruff and black. The baseline file must
  record the pins alongside the number, and the checking script must compare the running
  environment against the recorded one.

### spike-2: Can `setup.cfg` be relaxed to what the code actually meets, and then gated at zero?

- **Assumption**: "Option B in the issue is viable — turn off the strict flags the code
  violates, and the remainder is small enough to fix and gate at zero."
- **Method**: prototype — progressive CLI relaxation of every strict flag in `setup.cfg`,
  same tree, mypy 1.19.1 + redis-py 7.1.1.
- **Finding**: **False, decisively.**

  | Config | Errors | Files |
  |---|---|---|
  | `setup.cfg` as written | 1183 | 71 |
  | `--allow-untyped-defs` | 609 | 56 |
  | `+ --allow-any-generics` | 425 | 49 |
  | `+ --implicit-optional` | 361 | 44 |
  | `+ --no-check-untyped-defs` | 250 | 30 |

  After surrendering every strictness knob the issue proposes surrendering — untyped defs
  allowed, bare generics allowed, implicit Optional allowed, and `check_untyped_defs` off —
  **250 errors remain in 30 files.** Their codes are not style: `attr-defined` (63),
  `assignment` (56), `arg-type` (54), `misc` (21), `return-value` (13), `var-annotated` (12),
  `union-attr` (12), `index` (9), `operator` (6), `call-overload` (2). These are mypy saying
  the code is wrong, not that it is unannotated.

  So "relax the config to reality and gate at zero" does not terminate at a config. It
  terminates at a `[mypy-popoto.<module>] ignore_errors = True` block for 30 modules — which
  is a baseline, encoded worse: unversioned, uncounted, with no ratchet direction and no
  signal when a module becomes clean.
- **Confidence**: high — the ladder is monotonic and each rung was measured, not estimated.
- **Impact on plan**: Option B is eliminated on evidence rather than taste. The plan takes
  Option A, and `setup.cfg`'s `[mypy]` block is left byte-unchanged.

### spike-3: Does an already-clean package stay clean when measured from a full-tree run versus a scoped run?

- **Assumption**: "The allowlist can be enforced by running `mypy src/popoto/<pkg>/`
  per package."
- **Method**: code-read plus measurement — compared per-path-prefix error counts extracted
  from one full `mypy src/` run against the counts a scoped invocation reports.
- **Finding**: **Partially false, and the difference matters.** A scoped run checks a
  different file set and follows imports differently, so its count is not a subset of the
  full run's. `follow_imports = silent` means a scoped run silences diagnostics in modules the
  scope pulls in, which can hide an error the full run reports. Enforcement must therefore be
  a *filter over one full run's output by path prefix*, not N scoped runs. This is also ~13x
  cheaper: one mypy invocation instead of one per package.
- **Confidence**: high
- **Impact on plan**: `scripts/mypy_ratchet.py` runs mypy exactly once and derives both the
  total and every per-package count from that single output.

## Data Flow

Developer-facing, not user-facing. The flow the gate implements:

1. **Entry point**: a PR touches `src/**`, `setup.cfg`, `scripts/mypy_ratchet.py`, or
   `scripts/mypy_baseline.json`, or a developer runs `scripts/ci-local.sh types`.
2. **`scripts/mypy_ratchet.py`**: reads `scripts/mypy_baseline.json`, compares the running
   mypy and redis-py versions against the `environment` block recorded there, then invokes
   `mypy src/` once as a subprocess and captures stdout.
3. **Parse**: each error line is attributed to a path prefix. The script derives the total
   error count and a per-package histogram from that one output.
4. **Compare**: total against `baseline.total`; each package named in `baseline.clean` against
   zero.
5. **Output**: a table of package counts, the total, the delta against baseline, and exit
   code 0 (match) or 1 (over baseline, under baseline, or a clean package regressed). On an
   environment mismatch it prints what it measured, refuses to compare, and exits 0 locally
   or 1 under `--strict-env`.

Nothing in `src/popoto/` changes behavior. The only runtime source edits in this plan are
five `Optional` annotations that correct declared types to match the defaults already
shipping.

## Architectural Impact

- **New dependencies**: none at runtime. mypy is already in the `dev` extra; this plan raises
  its floor. The CI job installs pinned mypy and redis-py into a job-local environment.
- **Interface changes**: none. The five `Optional` annotations widen declared parameter types
  to match the `None` defaults those parameters already accept and already receive; no
  caller's valid argument becomes invalid.
- **Coupling**: adds one coupling that did not exist — `scripts/mypy_baseline.json` is now a
  file that any PR reducing or increasing the error count must update. That is the intended
  cost, and it is the same cost `uv.lock` already imposes.
- **Data ownership**: unchanged.
- **Reversibility**: trivial. Delete the job, the script, and the baseline file. The five
  annotation fixes are independently correct and would be kept.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 — both design questions (ratchet vs. honest config; where to pin) were
  settled by measurement in spikes 1 and 2, not by preference.
- Review rounds: 1–2. The second round is plausible because a gate's failure modes are the
  substance of its review, and the anti-criteria in Verification are designed to be run by
  the reviewer rather than trusted.

**Justification for Medium over Small:** the code delta is small — one script, one workflow
job, one JSON file, one gate function in `ci-local.sh`, five annotations, and four doc
touches. What earns Medium is that the artifact is a merge gate for every future PR, so it
must be verified in both directions (a new error fails it, a removed error also fails it and
says so usefully), it must not fire spuriously when a contributor's local redis-py differs,
and it must be coordinated with #572, which will move the number it asserts. Getting a gate
80% right is worse than not shipping it: a flaky gate gets disabled and the config goes back
to being decoration.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| mypy in the venv | `.venv/bin/mypy --version` | The tool being gated |
| redis-py version is known | `.venv/bin/python -c "import redis; print(redis.__version__)"` | The count is meaningless without it (spike-1) |
| Editable install resolves to this checkout | `.venv/bin/python -c "import popoto, pathlib; print(pathlib.Path(popoto.__file__).resolve())"` | CLAUDE.md worktree hazard 1 |
| `uv` available | `uv --version` | Required to refresh `uv.lock` after the dev-extra bump, or `lock-check.yml` fails |
| `gh` authenticated | `gh auth status` | Reading #506/#572 state during build |

No Redis server is required. Nothing in this plan runs pytest against a live Redis except the
final full-suite regression check, which uses the standard isolated DB.

## Solution

### Key Elements

- **`scripts/mypy_ratchet.py`** — runs `mypy src/` once, prints a per-package table and the
  total, and compares both against a checked-in baseline. Exits non-zero when the total
  differs from the baseline in either direction, or when any allowlisted package is above
  zero. `--update` rewrites the baseline from the current measurement.
- **`scripts/mypy_baseline.json`** — the checked-in ground truth: the total, the allowlist of
  packages pinned at zero, and the exact mypy and redis-py versions the total was measured
  under. Human-readable and diff-legible on purpose; a reviewer should be able to see the
  number move.
- **`mypy` job in `.github/workflows/lint.yml`** — a third job alongside `ruff` and `black`,
  installing explicit `==` pins for mypy and redis-py, and running the script with
  `--strict-env`.
- **`types` gate in `scripts/ci-local.sh`** — mirrors the CI job locally, without
  `--strict-env`, so a contributor on a different redis-py gets a loud explanation instead of
  a mystery failure.
- **Five `Optional` corrections** — the three sites the issue names plus the one it misses
  (`embeddings/openai.py:39`, identical defect to the `voyage.py:41` it does name). Fixing
  these first demonstrates the ratchet-down half of the ritual inside the PR that introduces
  the ratchet.

### Flow

A contributor adds an unannotated function → opens a PR → the `mypy` job runs → the script
reports `total 1121, baseline 1119, +2` and names the two lines → the contributor annotates
them, or annotates something else to pay for them → job green.

A contributor cleans up a file → the script reports `total 1115, baseline 1119, -4 — run
scripts/mypy_ratchet.py --update and commit scripts/mypy_baseline.json` → the contributor
commits the lowered baseline → job green, and the new floor is permanent.

A contributor finishes a package → the script reports the package at 0 → the contributor adds
it to `clean` in the baseline file → the package can never regress.

### Technical Approach

- **Take Option A (ratchet), reject Option B (honest config).** Spike 2 is the argument:
  Option B does not reach zero at any setting of the strict flags. Relaxing everything the
  issue proposes relaxing still leaves 250 genuine type errors in 30 files, so the honest
  config would have to be an `ignore_errors` list for those 30 modules. That is a baseline
  with worse properties: it is not a number, so it cannot ratchet; it is not versioned against
  a tool pin, so it drifts silently; and it gives no signal when a module becomes clean. The
  ratchet keeps the aspiration (`setup.cfg` unchanged) and states the reality (a number in a
  file) as two separate, individually honest artifacts.
- **`setup.cfg`'s `[mypy]` block is byte-unchanged.** No flag is relaxed, no per-module
  override is added, and the mypy config is not migrated to `pyproject.toml`. The issue's
  acceptance criterion "config is single-sourced and matches enforced behavior" is satisfied
  as written: `setup.cfg` is already the single source (there is no `[tool.mypy]` in
  `pyproject.toml`), and after this plan it is enforced — the gate runs it exactly as
  written. Moving the block between files would be churn with no gate consequence.
- **Pin both mypy and redis-py in the workflow job, with `==`.** Spike 1 measured 57 errors
  of spread across four plausible version pairs, and spike 1's second finding is that CI
  installs from `pyproject.toml` floors, not `uv.lock`, so nothing today constrains either.
  Follow the `lint.yml` precedent exactly: `python -m pip install "mypy==X" "redis==Y"` in a
  named step, with a comment saying why, and bump deliberately alongside the re-baseline the
  bump requires.
- **Pin against redis-py 8.x, not 7.x.** 8.x is what a fresh `pip install -e ".[dev]"`
  resolves today, so it is what the `tests.yml` jobs are already running against; gating
  against 7.x would measure a tree nothing else in CI measures. Record the 7.x number in the
  baseline file as a documented non-gating reference so a local developer on 7.x can confirm
  the offset rather than chase it.
- **Raise the dev-extra mypy floor and refresh `uv.lock` in the same commit.** `mypy>=0.971`
  is stale by years and would hand a fresh contributor a materially different count. Raise it
  to the pinned major and run `uv lock`; `lock-check.yml` gates the pair and will fail if only
  one moves.
- **One mypy invocation, filtered by path prefix.** Spike 3 showed a scoped `mypy
  src/popoto/<pkg>/` run is not a subset of the full run under `follow_imports = silent`, so
  per-package enforcement derives from filtering one full run's output. This is also the only
  way the total and the per-package numbers are guaranteed mutually consistent.
- **Exact equality, not `<=`.** The gate fails when the count is *under* baseline as well as
  over, with a message naming the `--update` command. A `<=` ratchet never tightens: a PR that
  removes 40 errors banks no floor, and the next PR is free to add them back. Exact equality
  costs one extra file in the occasional diff and makes every improvement permanent.
- **Environment mismatch is a refusal, not a failure.** When the running mypy or redis-py does
  not match the baseline's recorded pins, the script prints both, states that the counts are
  not comparable, and exits 0 — unless `--strict-env` is passed, which CI passes. A local
  contributor on a different redis-py must never see a red gate they cannot act on. This
  directly implements CLAUDE.md's rule to state the environment alongside any count.
- **Fix the five `Optional` sites before generating the baseline.** They are the issue's
  acceptance criterion 2, they are genuine (a parameter declared `str` that is called with and
  defaults to `None`), and fixing them inside this PR exercises the ratchet-down path end to
  end. Sequence matters: fix, then `--update`, then wire the gate.

## Failure Path Test Strategy

### Exception Handling Coverage

- [x] No `except Exception: pass` blocks exist in the files this plan touches. The five
      `Optional` corrections are annotation-only and add no handler.
- [x] `scripts/mypy_ratchet.py` is new and has three failure paths, each of which must
      produce a distinct, actionable message and a test: (a) the baseline file is missing or
      unparseable, (b) the `mypy` subprocess fails to start or crashes with a non-error exit
      (mypy exits 1 for "errors found" and 2 for "internal error" — these must not be
      conflated), (c) mypy's output format does not parse into a total. A bare `except:` that
      turns any of these into "0 errors, gate passes" is the one catastrophic bug this script
      can have, and it is what the tests in Task 2 exist to prevent.

### Empty/Invalid Input Handling

- [x] Empty mypy stdout must be treated as a parse failure, not as zero errors. A gate that
      passes when the checker did not run is worse than no gate.
- [x] A baseline file with `total` absent, non-integer, or negative must be rejected loudly
      rather than defaulted.
- [x] A package name in `clean` that matches no directory under `src/popoto/` must be
      reported as a stale allowlist entry and fail, so a renamed or deleted package cannot
      leave a permanently-satisfied allowlist row behind.
- [x] Not agent-output processing; no silent-loop risk.

### Error State Rendering

- [x] The over-baseline message must name the delta and print the offending error lines, not
      just the count — a contributor seeing only "1121 > 1119" has to re-run mypy by hand.
- [x] The under-baseline message must name the exact `--update` command and the file to
      commit, because that path is a success being reported as a failure and is the most
      confusing state the gate has.
- [x] The clean-package-regressed message must name the package and its error lines
      separately from the total, since a regression there can coexist with a total that is on
      or under baseline.

## Test Impact

No existing tests affected. This plan adds a script, a workflow job, a shell gate function,
one JSON file, and five annotation corrections. No existing test asserts anything about mypy,
type annotations, or the parameters being widened — `grep -rn "mypy" tests/` returns nothing.

New tests, in `tests/test_mypy_ratchet.py`:

- Parser and comparator unit tests driven by synthetic mypy output fixtures. These must not
  invoke mypy; they exercise the pure functions against captured strings so the suite stays
  fast and does not depend on the tool version.
- One test per failure path enumerated above.

The new test file needs no Redis. It must not import `popoto`, so it stays unaffected by the
pytest plugin's DB isolation entirely.

## Rabbit Holes

- **Fixing errors while building the gate.** 1126 is not a number anyone pays down in a PR,
  and the five `Optional` sites are in scope only because the issue names them and because
  they prove the ratchet-down path. Every additional file "while we're here" makes the
  baseline harder to review and the gate's own correctness harder to see.
- **Migrating the mypy config from `setup.cfg` to `pyproject.toml`.** Tempting because the
  issue mentions single-sourcing, and worthless: the config is already single-sourced, mypy
  reads `setup.cfg` natively, and moving it changes the count by zero. It would also collide
  with `lock-check.yml`'s `pyproject.toml` path trigger for no benefit.
- **Gating `tests/` too.** `[mypy-tests.*] ignore_errors = True` stays. `tests/` is not even
  ruff-gated yet, and widening two gates at once means a failure in either is attributed to
  the wrong change.
- **Building a per-file allowlist instead of per-package.** 26 files are clean today versus
  one package, so per-file looks strictly better. It is not: files get renamed and split
  constantly, and every rename becomes an allowlist edit or a silently-dropped guarantee.
  Package granularity is stable enough to be maintained and coarse enough to mean something
  when it is satisfied.
- **Making the gate advisory (`continue-on-error: true`) "for the first few weeks".** An
  advisory gate is the state the repo is already in. If the number is right and the pins are
  right, it is enforceable on day one; if it is not, the fix is the pins, not the enforcement.
- **Chasing a portable error count.** Per CLAUDE.md and spike 1, there is no such thing. Every
  number in this plan and in the baseline file carries its environment. Reconciling a
  reviewer's different number is a matter of comparing pins, not of finding the true count.
- **Adding a mypy job to `tests.yml` instead of `lint.yml`.** `lint.yml` is the static-analysis
  workflow with two precedent jobs and the right path triggers. A second home for static
  checks splits the convention.

## Risks

### Risk 1: The gate fires spuriously on a contributor's machine and gets ignored

**Impact:** The failure mode that kills gates. A contributor whose local redis-py is 7.1.1
sees 52 phantom errors, concludes the gate is broken, and stops running it — and eventually
argues for disabling the CI job on the same evidence.
**Mitigation:** The environment check is a first-class feature, not an afterthought. Without
`--strict-env` the script refuses to compare and exits 0 with a message naming both versions
and the offset, so the local experience is "this doesn't apply to your environment", never a
red X. `scripts/ci-local.sh` invokes it without `--strict-env`; only CI, where the pins are
enforced by the install step, passes it. Verification includes an explicit test that a
mismatched environment exits 0 locally and 1 under `--strict-env`.

### Risk 2: The script silently passes when mypy did not actually run

**Impact:** The worst outcome available: a green gate that checks nothing, indefinitely, with
the count frozen at a stale baseline that nobody re-verifies because CI is green.
**Mitigation:** Empty or unparseable output is a hard failure with a distinct exit path, and
mypy's exit code 2 (internal error) is never conflated with exit code 1 (errors found). The
Verification table includes an inverse row that runs the script against a deliberately broken
mypy invocation and asserts it fails. This must be demonstrated red before the PR is opened,
per the anti-criteria convention.

### Risk 3: #572 lands and invalidates the baseline

**Impact:** Whichever of #506 and #572 merges second gets a red gate on a change that is
unambiguously an improvement — #572 takes `transfer/` from 49 errors to 0.
**Mitigation:** This is expected, benign, and exactly what the under-baseline message is for.
Concretely: if this plan merges first, #572's PR must run `scripts/mypy_ratchet.py --update`,
commit the lowered total, and add `transfer` to `clean`. If #572 merges first, this plan
generates its baseline after rebasing and lists `privacy` and `transfer` in `clean` from the
start. Either order works; the builder must check `gh issue view 572 --json state` and
`git log --oneline -- src/popoto/transfer/` immediately before generating the baseline, and
must generate it rather than copy any number from this document.

### Risk 4: The five `Optional` corrections change runtime behavior

**Impact:** A widened annotation is inert, but the temptation while editing a signature is to
"also fix" the `None` handling in the body, which is a real behavior change smuggled into a
CI plumbing PR.
**Mitigation:** The diff for those five sites must contain only type annotations —
`str` → `str | None` and equivalents — with no body edit, no default change, and no new
guard. The Verification table asserts the annotation edits are confined to signature lines.
The full suite must pass unchanged; any test that needs updating is evidence the constraint
was violated.

### Risk 5: The baseline becomes a ceiling nobody lowers

**Impact:** The gate stops drift but the number sits at 1119 forever, and the strict
`setup.cfg` is still aspirational — a nicer-looking version of today's problem.
**Mitigation:** Partly out of this plan's control, and worth saying rather than pretending
otherwise. What is in scope: the per-package allowlist gives paydown a visible finish line and
a permanent reward, #572 is already queued as the first increment, and the docs changes tell
contributors that lowering the baseline is a normal, welcome PR shape. The gate's floor
guarantee — the number can never rise — is the deliverable; the rate of descent is not.

## Race Conditions

No race conditions identified. The script is a single synchronous subprocess invocation
followed by string parsing and integer comparison. It shares no mutable state, spawns no
concurrency, and touches no Redis. The five annotation corrections add no executable
statement.

One ordering hazard exists but is a merge-sequencing concern rather than a race, and it is
handled explicitly in Risk 3: two PRs that both change the error count will conflict on
`scripts/mypy_baseline.json`. That conflict is a feature — it is a text conflict in a
one-line JSON field, resolved by re-running `--update` — and it is exactly how `uv.lock`
already behaves in this repo.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #572] Annotating `src/popoto/transfer/` (49 errors in the 7.1.1 environment).
  #572 owns that package and its plan is Ready. This PR must not touch any file under
  `src/popoto/transfer/`; if #572 has already merged, its zero simply appears in the
  generated baseline and `transfer` joins the allowlist.

Nothing else is deferred. The remaining ~1114 errors are not deferred work being quietly
pushed to a future issue — they are the gate's ongoing subject. #506's own scope statement
("Not in scope: fixing all 1150 lines in one PR — plan should scope an incremental allowlist
or per-module rollout") is satisfied by shipping the mechanism that makes the rollout
incremental and enforceable, not by promising a sweep in a later ticket.

## Update System

No `/update` skill changes required. The new script and baseline file propagate with the
repository like `scripts/ci-local.sh` does; there is no deployment target, no service, and no
migration. The one propagation detail: raising the dev-extra mypy floor means a contributor
running `/update` will get a newer mypy on their next dependency sync, which is intended — it
brings local counts in line with the pin the gate measures against.

## Agent Integration

No agent integration required. This is CI and repository tooling. Nothing here is reachable
from `popoto/__init__.py`, the MCP server, or the hook path, and no new capability is exposed
to an agent.

## Documentation

### Feature Documentation

- [ ] No `docs/features/` page. This is a CI gate, and the repo documents its gates in
      `CLAUDE.md` and `docs/sdlc/`, not in the feature index. Adding a feature page would put
      contributor workflow in the wrong place.

### External Documentation Site

- [ ] No page changes expected — `grep -rln 'mypy' docs/ --exclude-dir=plans` matches only
      `docs/sdlc/`, which is not in the mkdocs nav. Run `mkdocs build --strict` as a standard
      gate to confirm nothing broke.

### Inline Documentation

- [ ] `CLAUDE.md` Commands block: the bare `mypy src/  # type checking` line becomes the
      ratchet invocation, with the pinned environment named. The worktree section's hazard 5
      ("mypy error delta is redis-py-version-dependent (not automated)") is now partly
      automated by the environment check and must be rewritten to say so, quoting the measured
      53-error redis-py spread rather than describing it qualitatively.
- [ ] `docs/sdlc/do-test.md` (lines 9 and 74): the `mypy src/  # type check` command and the
      redis-py variance note. Point both at the script.
- [ ] `docs/sdlc/do-pr-review.md` (lines 76, 138, 162): same command, same variance note, plus
      line 162's "`mypy src/` with no new errors" review criterion, which becomes a concrete
      "the ratchet gate is green" check.
- [ ] `docs/sdlc/do-sdlc.md` (lines 107, 138): same command and the pointer to the variance
      documentation.
- [ ] No `CONTRIBUTING.md` exists in this repo (verified), so there is no contributor guide to
      update. Do not create one as a side effect of this plan.
- [ ] Header comment in `.github/workflows/lint.yml` explaining the mypy job's ratchet
      semantics and why both tool versions are pinned, matching the existing ruff and black
      comment style.
- [ ] Module docstring in `scripts/mypy_ratchet.py` stating the contract in three lines: the
      count must equal the baseline exactly, allowlisted packages must be zero, and the
      environment must match or the comparison is refused.

## Success Criteria

- [ ] `scripts/mypy_ratchet.py` exists, runs `mypy src/` once, and prints a per-package table
      plus the total and the delta
- [ ] `scripts/mypy_baseline.json` exists and records the total, the `clean` package
      allowlist, and the exact mypy and redis-py versions measured
- [ ] The gate passes at the current count on an unmodified tree
- [ ] A deliberately introduced type error makes the gate fail, with the offending line in the
      output
- [ ] A deliberately removed type error also makes the gate fail, with the `--update` command
      in the output
- [ ] Adding an error inside `src/popoto/privacy/` fails the gate on the allowlist check even
      when the total is paid for elsewhere
- [ ] An environment whose redis-py differs from the baseline exits 0 without `--strict-env`
      and 1 with it
- [ ] `.github/workflows/lint.yml` has a third job that installs pinned mypy and redis-py and
      runs the script with `--strict-env`, and its `paths:` triggers include `setup.cfg`,
      `scripts/mypy_ratchet.py`, and `scripts/mypy_baseline.json`
- [ ] `scripts/ci-local.sh` has a `types` gate, in the default gate set and in `--all`, with
      the header table updated
- [ ] `setup.cfg` is byte-unchanged
- [ ] The five `Optional` sites are corrected: `stores/filesystem.py:38`,
      `embeddings/voyage.py:41`, `embeddings/openai.py:39`, `pubsub/publisher.py:156-158`
- [ ] `pyproject.toml`'s dev-extra mypy floor is raised and `uv lock --check` passes
- [ ] No file under `src/popoto/transfer/` is modified
- [ ] Full suite shows no new failures against the `0dbce759` baseline
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (mypy-gate)**
  - Name: `mypy-gate-builder`
  - Role: Write the script, the baseline, the workflow job, the `ci-local.sh` gate, and the
    five `Optional` corrections
  - Agent Type: builder
  - Resume: true

- **Test engineer (mypy-gate)**
  - Name: `mypy-gate-tester`
  - Role: Write `tests/test_mypy_ratchet.py` covering the parser, the comparator, and every
    failure path
  - Agent Type: test-engineer
  - Resume: true

- **Validator (mypy-gate)**
  - Name: `mypy-gate-validator`
  - Role: Prove the gate fails in all four directions (error added, error removed, allowlisted
    package regressed, environment mismatched under `--strict-env`) and that `setup.cfg` and
    `src/popoto/transfer/` are untouched
  - Agent Type: validator
  - Resume: true

- **Documentarian (mypy-gate)**
  - Name: `mypy-gate-documentarian`
  - Role: Update `CLAUDE.md` and the three `docs/sdlc/` files
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Correct the five implicit-Optional sites

- **Task ID**: build-optional-fixes
- **Depends On**: none
- **Validates**: `.venv/bin/mypy src/ 2>&1 | grep -c "PEP 484 prohibits implicit Optional"`
  drops by 5; full suite unchanged
- **Informed By**: Freshness Check (three of the four sites the issue names still hold at the
  cited lines; the fourth was fixed by `926953c`)
- **Assigned To**: `mypy-gate-builder`
- **Agent Type**: builder
- **Parallel**: false
- `src/popoto/stores/filesystem.py:38` — `base_path: str = None` → `str | None`.
- `src/popoto/embeddings/voyage.py:41` — `api_key: str = None` → `str | None`.
- `src/popoto/embeddings/openai.py:39` — same defect, not named in the issue. Fix it; leaving
  its twin behind would be indefensible in review.
- `src/popoto/pubsub/publisher.py:156,157,158` — `data: dict`, `channel_name: str`,
  `pipeline: Pipeline`, all defaulting to `None`.
- Signature lines only. No body edit, no default change, no new `if x is None` guard. If a
  body genuinely mishandles `None`, that is a separate bug — file it, do not fix it here.

### 2. Write `scripts/mypy_ratchet.py` and its tests

- **Task ID**: build-ratchet-script
- **Depends On**: none
- **Validates**: `tests/test_mypy_ratchet.py` (create) — parser, comparator, and the three
  failure paths
- **Informed By**: spike-3 (one full run filtered by path prefix, never N scoped runs),
  spike-1 (the environment block is load-bearing)
- **Assigned To**: `mypy-gate-builder`, tests by `mypy-gate-tester`
- **Agent Type**: builder
- **Parallel**: true
- Invoke `mypy src/` once via `subprocess`, from the repo root, capturing stdout. Distinguish
  mypy exit 0 (clean), 1 (errors found), and 2 (internal error); only 0 and 1 are valid input
  to the comparison.
- Parse `path:line: error: message  [code]` lines. Attribute each to the first path segment
  under `src/popoto/`; top-level modules go to a `(root)` bucket.
- Treat empty stdout, a zero-length parse, or a missing `Found N errors` summary as a hard
  failure with its own message. Never default a parse failure to zero.
- Compare the total to `baseline.total` for exact equality, and every package in
  `baseline.clean` to zero. Report the two conditions separately.
- Verify each name in `baseline.clean` corresponds to a real directory under `src/popoto/`;
  a stale entry fails.
- Compare the running mypy and redis-py versions to `baseline.environment`. On mismatch,
  print both, state that counts are not comparable, and exit 0 — or 1 under `--strict-env`.
- `--update` rewrites `scripts/mypy_baseline.json` from the current measurement, preserving
  the `clean` list and refreshing the environment block.
- Tests use captured synthetic mypy output as fixtures and must not invoke mypy or import
  `popoto`.

### 3. Generate the baseline

- **Task ID**: build-baseline
- **Depends On**: build-optional-fixes, build-ratchet-script
- **Validates**: `python scripts/mypy_ratchet.py` exits 0 on the unmodified tree
- **Informed By**: Risk 3 (#572 ordering)
- **Assigned To**: `mypy-gate-builder`
- **Agent Type**: builder
- **Parallel**: false
- First: check `gh issue view 572 --json state` and `git log --oneline -- src/popoto/transfer/`.
  If #572 has merged, rebase before measuring and include `transfer` in `clean`.
- Install the pinned pair into the working venv, or a throwaway one, and record the exact
  versions. Pin against redis-py 8.x (what a fresh `pip install` resolves and what the
  `tests.yml` jobs run), not the 7.1.1 that `uv.lock` happens to hold.
- Run `python scripts/mypy_ratchet.py --update`. **Generate the number; do not copy any
  figure from this plan.** For orientation only, the pre-fix count under mypy 2.1.0 +
  redis-py 8.1.0 was 1126, so expect roughly 1119 after Task 1.
- Seed `clean` with `privacy` (measured at 0 today) and, if applicable, `transfer`.
- Also record the redis-py 7.1.1 count in a non-gating `reference` field, so a contributor on
  7.x can confirm the ~53-error offset instead of investigating it.

### 4. Wire the CI job and the local gate

- **Task ID**: build-ci-wiring
- **Depends On**: build-baseline
- **Validates**: `.github/workflows/lint.yml` parses; `scripts/ci-local.sh types` passes
- **Informed By**: spike-1's second finding (CI installs from `pyproject.toml` floors, not
  `uv.lock`)
- **Assigned To**: `mypy-gate-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add a `mypy` job to `.github/workflows/lint.yml`, matching the shape of the `ruff` and
  `black` jobs: `setup-python` at 3.12, an install step with explicit `==` pins for mypy and
  redis-py and a comment saying why, then `python scripts/mypy_ratchet.py --strict-env`.
- Extend the workflow's `paths:` triggers (both `pull_request` and `push`) to include
  `setup.cfg`, `scripts/mypy_ratchet.py`, and `scripts/mypy_baseline.json`. `setup.cfg` holds
  the mypy config and is currently not a trigger for any workflow — without this, a config
  edit skips its own gate.
- Add `gate_types()` to `scripts/ci-local.sh`, invoking the script **without**
  `--strict-env`. Register `types` in the argument parser's allowed-gate list, in the
  dispatch `case`, in the default gate set, and in `--all`. Update the gate table in the
  file's header comment, which is what `--help` prints.
- Raise `pyproject.toml`'s dev-extra `mypy>=0.971` to the pinned major and run `uv lock`.
  Both files must move together or `lock-check.yml` fails.

### 5. Prove the gate fails in all four directions

- **Task ID**: validate-gate
- **Depends On**: build-ci-wiring
- **Assigned To**: `mypy-gate-validator`
- **Agent Type**: validator
- **Parallel**: false
- Introduce one unannotated function in `src/popoto/redis_db.py`; assert the script exits 1,
  names the file and line, and reports `+1`. Revert.
- Annotate one existing unannotated function; assert the script exits 1 and its message
  contains `--update`. Revert.
- Introduce one error inside `src/popoto/privacy/` and simultaneously fix one elsewhere so the
  total is unchanged; assert the script still exits 1 on the allowlist check. Revert.
- Run with a deliberately mismatched redis-py; assert exit 0 without `--strict-env` and exit 1
  with it.
- Capture each FAIL output for the PR description, per the anti-criteria convention.
- Confirm `git diff --stat setup.cfg` is empty and `git diff --name-only -- src/popoto/transfer/`
  is empty.

### 6. Documentation

- **Task ID**: document-gate
- **Depends On**: validate-gate
- **Assigned To**: `mypy-gate-documentarian`
- **Agent Type**: documentarian
- **Parallel**: false
- `CLAUDE.md`: replace the bare `mypy src/` command with the ratchet invocation and name the
  pinned environment. Rewrite worktree hazard 5 to reflect that the variance is now checked
  automatically, quoting the measured spread.
- `docs/sdlc/do-test.md`, `docs/sdlc/do-pr-review.md`, `docs/sdlc/do-sdlc.md`: update the
  `mypy src/` command lines and the redis-py variance notes; in `do-pr-review.md`, turn the
  "no new errors" review criterion into "the ratchet gate is green".
- Do not create `CONTRIBUTING.md`.

### 7. Final validation

- **Task ID**: validate-all
- **Depends On**: document-gate
- **Assigned To**: `mypy-gate-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row in the Verification table.
- Run the full suite and compare against the `0dbce759` baseline.
- Confirm every Success Criteria checkbox.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Gate passes on clean tree | `python scripts/mypy_ratchet.py` | exit code 0 |
| Gate passes under strict env in CI shape | `python scripts/mypy_ratchet.py --strict-env` | exit code 0 |
| Baseline records its environment | `python -c "import json;d=json.load(open('scripts/mypy_baseline.json'));print(sorted(d['environment']))"` | output contains `mypy` |
| Baseline records the allowlist | `python -c "import json;print(json.load(open('scripts/mypy_baseline.json'))['clean'])"` | output contains `privacy` |
| Allowlisted package is actually clean | `.venv/bin/mypy src/ 2>&1 \| grep -c '^src/popoto/privacy/.*: error:'` | match count == 0 |
| Local gate registered | `bash scripts/ci-local.sh --help \| grep -c types` | output > 0 |
| CI job exists | `grep -c '^  mypy:' .github/workflows/lint.yml` | output > 0 |
| CI pins mypy exactly | `grep -c 'mypy==' .github/workflows/lint.yml` | output > 0 |
| CI pins redis-py exactly | `grep -c 'redis==' .github/workflows/lint.yml` | output > 0 |
| Workflow triggers on setup.cfg | `grep -c 'setup.cfg' .github/workflows/lint.yml` | output > 0 |
| setup.cfg unchanged | `git diff --stat main... -- setup.cfg \| wc -l` | match count == 0 |
| transfer/ untouched (anti-criterion for the #572 No-Go) | `git diff --name-only main... -- src/popoto/transfer/ \| wc -l` | match count == 0 |
| No blanket ignores added | `grep -rc 'ignore_errors' setup.cfg` | match count == 0 |
| No type-ignore comments added | `git diff main... -- src/popoto/ \| grep -c '^+.*type: ignore'` | match count == 0 |
| Implicit-Optional sites fixed | `.venv/bin/mypy src/ 2>&1 \| grep -c 'stores/filesystem.py:38\|embeddings/voyage.py:41\|embeddings/openai.py:39'` | match count == 0 |
| Lock stays in sync | `uv lock --check` | exit code 0 |
| Ratchet tests pass | `.venv/bin/pytest tests/test_mypy_ratchet.py -q` | exit code 0 |
| Full suite passes | `.venv/bin/pytest -q` | exit code 0 |
| Docs build | `.venv/bin/mkdocs build --strict` | exit code 0 |
| Lint still clean | `.venv/bin/ruff check src/` | exit code 0 |
| Format still clean | `.venv/bin/black --check src/ tests/` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

None blocking. Both questions the issue poses were resolved by measurement rather than
judgment: spike 2 eliminated Option B (no setting of the strict flags reaches zero — 250 real
type errors survive full relaxation), and spike 1 determined the pins (57 errors of spread
across four plausible version pairs, and CI installs from `pyproject.toml` floors rather than
`uv.lock`, so nothing currently constrains either tool).

One decision is recorded here rather than asked, because it is reversible and the plan should
not block on it: the gate pins against **redis-py 8.x**, on the grounds that it is what a
fresh `pip install -e ".[dev]"` resolves and therefore what the existing `tests.yml` jobs
already run against. The 7.1.1 count is recorded in the baseline as a non-gating reference.
If the maintainer would rather CI standardize on the `uv.lock`-resolved 7.1.1 across all
workflows, that is a one-line change to the pin plus a `--update`, and it should be decided
for the whole CI surface rather than for the mypy job alone.
