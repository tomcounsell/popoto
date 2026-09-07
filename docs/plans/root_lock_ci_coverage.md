---
status: Ready to Build
type: chore
appetite: Small
owner: Dev (sdlc-669)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/669
last_comment_id: none
revision_applied: true
---

# Root uv.lock: decide the support contract, then make a green lockfile-only PR mean something

## Problem

`uv.lock` at the repo root is gated by exactly one job, `lock-check.yml`, which runs
`uv lock --check`. That proves the lock is **internally consistent with
`pyproject.toml`**. It does not install the locked set, import anything, or run a
test against it.

No other job consumes the lock. Verified at `8151e8c0`:

| Job | Install line | What it resolves |
|---|---|---|
| `tests.yml:72` (pytest Redis) | `pip install -e ".[dev,embeddings,benchmark]"` | fresh from `pyproject.toml` floors → **latest** |
| `tests.yml:139` (pytest Valkey) | `pip install -e ".[dev,embeddings,benchmark]"` | fresh from floors → **latest** |
| `lint.yml:112-113` (mypy) | `pip install -e ".[dev,embeddings,mcp]"` then `pip install "mypy==2.3.1" "redis==8.1.0"` | floors, then pinned on top to match the ratchet baseline |

`.github/dependabot.yml` schedules the root entry as `versioning-strategy:
lockfile-only`, so **every scheduled dependency PR touches only `uv.lock`** — the one
file nothing exercises. A green CI run on such a PR means "the suite passed against a
dependency set that ignored the diff under review."

There is a second, sharper gap underneath it. `uv.lock` locks the **full** extra set,
but CI installs only `dev`, `embeddings`, `benchmark`, and `mcp`. Seven declared extras
are installed by **no job at all**:

`anthropic`, `openai`, `voyage`, `monitoring`, `dataframe`, `ulid`, `ksuid`

Each is a published `[project.optional-dependencies]` entry a real consumer can install.
Nothing in CI has ever installed one.

## Freshness Check

**Disposition: Unchanged.** Baseline `8151e8c0` (main at plan time). #669 was filed
2026-09-07 and planned the same day.

Every file:line reference in the issue was re-read at `8151e8c0` and all three are
exact — `tests.yml:72`, `tests.yml:139`, `lint.yml:112-113` carry the install lines the
issue quotes, and `lock-check.yml`'s only step is `uv lock --check`, path-scoped to
`pyproject.toml`, `uv.lock`, and itself.

Cited siblings re-checked: #666, #667, #668 are **open** and lockfile-only, as described.
#660 (root-lock refresh) is open; the team lead owns closing it down its
"scheduled Dependabot PR landed" branch and this plan does not touch it. #611 (the
`examples/` lane whose `examples.yml` is the prior art here) merged as `e5634772`.

No commits have touched `uv.lock`, `lock-check.yml`, `tests.yml`, or `lint.yml` since the
issue was filed. No active plan in `docs/plans/` covers CI dependency installation.

## Prior Art

- **#611 / PR #664** (`e5634772`) — built `examples.yml`, the only workflow in the repo
  that installs from a lockfile (`uv sync --extra dev` → `uv run --no-sync pytest`,
  `examples.yml:93-97`). It exists because "nothing in CI touched the directory" let real
  API drift accumulate undetected. The root lock is in the same position, one directory up.
- **#551** — created `.github/dependabot.yml`, choosing `lockfile-only` for the root
  entry because popoto is published and a raised floor propagates to every consumer.
  That reasoning is sound and this plan does not revisit it; it is also precisely what
  makes every scheduled PR land in the unexercised file.
- **#523** — the defect that motivated `lock-check.yml`: the `anthropic` extra was
  declared in `pyproject.toml` but absent from the lock, and the locked project version
  sat at 1.7.1 while `pyproject.toml` said 1.8.1. That gate answers *consistency*, and
  was never scoped to answer *installability*.
- **#506** — the mypy ratchet. `scripts/mypy_baseline.json:26` is the load-bearing
  artifact for this plan's first acceptance criterion; see The Decision below.

No prior attempt to install the root lock in CI exists, so there is no
"Why Previous Fixes Failed" section.

## Research

No WebSearch was needed. Every question this plan turns on is answerable against the
repo and the pinned `uv` binary, and was resolved empirically in the spikes below —
which is strictly better evidence than documentation prose about what `uv` "should" do.

## Spike Results

All spikes ran on macOS arm64 against `uv 0.12.2` (the version `lock-check.yml` pins),
Python 3.12, worktree `.worktrees/sdlc-611` at `8151e8c0`, into a scratch
`UV_PROJECT_ENVIRONMENT` outside the checkout so the lane's `.venv` was untouched.

### spike-1: Does the locked set actually install, and how expensive is it?

- **Assumption**: "`uv sync --locked` on the root lock succeeds and is cheap enough to gate PRs."
- **Method**: prototype
- **Result**: **Yes.** `uv sync --locked --python 3.12 --all-extras --no-extra benchmark`
  succeeds and completes in **0.71s warm**. `--no-extra` requires `--all-extras` and is
  supported on 0.12.2. Excluding `benchmark` avoids pulling `torch`/`sentence-transformers`
  (hundreds of MB, uncached in CI); those packages are already installed from floors by
  both `tests.yml` jobs, so their installability is proven elsewhere.
- **Confidence**: high
- **Impact if false**: the whole "add a job" branch dies and only documentation remains.

### spike-2: Do the locked extras import on 3.12?

- **Assumption**: "The seven never-installed extras import cleanly under the lock."
- **Method**: prototype
- **Result**: **Yes.** Direct imports of `anthropic` (0.120.2), `openai` (3.5.0),
  `voyageai` (0.5.0), `pandas`, and `sentry_sdk` all succeed, as do all nine popoto
  modules that reference them.
- **Confidence**: high

### spike-3 (decisive negative): Would an import smoke have caught #667?

- **Assumption**: "An import-surface job would have produced signal on the `anthropic`
  0.120.2 → 1.2.0 major bump — the case that motivated this issue."
- **Method**: prototype — installed `anthropic==1.2.0` into the synced env and re-ran the smoke.
- **Result**: **No. The assumption is false.** Under 1.2.0, all three popoto extraction
  modules import cleanly, `anthropic.Anthropic` still exists, and
  `client.messages.create` still exists. The smoke is green on both sides of the major.
- **Confidence**: high
- **Impact if false (i.e. that it is false)**: this is the finding that shapes the plan.
  It removes the strongest-sounding justification for a new job, and it must be stated
  in `CLAUDE.md` rather than quietly omitted — otherwise the next person assumes the new
  step covers major bumps, which it does not.

### spike-4 (decisive negative): Is importing popoto's own modules a useful probe?

- **Assumption**: "Importing `popoto.extraction.claude` et al. proves the extras work."
- **Method**: code-read
- **Result**: **No.** Every extra-backed module guards its third-party import:

  ```python
  try:
      import anthropic as anthropic_module
      _anthropic_available = True
  except ImportError:
      ...
  ```

  Confirmed at `extraction/claude.py:24-28`, `embeddings/openai.py:17-21`,
  `fields/dataframe_field.py:59-63`, and `models/encoding.py:60-64`. These modules import
  successfully whether the package is present, absent, or broken, so they carry **zero**
  signal about the locked extras.
- **Confidence**: high
- **Impact**: the smoke must import the **third-party packages directly**. A design that
  imported only popoto modules would be vacuous — green by construction.

## The Decision (acceptance criterion 1)

**Is the root `uv.lock` a supported install path? Split verdict — and the split is the
whole answer.**

**It is NOT a supported *consumer* install path.** popoto publishes to PyPI from
`pyproject.toml` floors. `pip install popoto` resolves those floors fresh and never sees
`uv.lock`. No downstream consumer can install via this lock even if they wanted to.

**It IS the supported *developer* install path, and the repo already relies on that.**
This is not a judgment call — it is recorded in the tree. `scripts/mypy_baseline.json:26`
reads:

> "Recorded so a contributor on the uv.lock-resolved 7.1.1 can confirm the offset instead
> of investigating it."

The mypy baseline explicitly anchors an error-count offset for a contributor whose
environment came from `uv sync`. Declaring the lock unsupported would contradict a
comment the type gate already depends on. So the flat "no, document it and move on"
answer — legitimate on its face, and the one the issue offers as a way out — is
**foreclosed by existing code**.

**What follows from the split.** The lock's job is to give developers a reproducible
environment. The gap is therefore not "the suite doesn't run against the lock" but the
narrower, sharper: **`uv lock --check` proves the lock is *consistent*; nothing proves it
is *installable*.** Those are different assertions, and only the second one can break a
developer's `uv sync`.

**What this plan deliberately does NOT do, and why.** It does not add a second test
matrix. `tests.yml` installs from floors, which resolves to **latest**; the lock is by
construction a **lagging** pin. A full suite run against the lock would exercise an
arbitrary middle point — neither the floor the library declares nor the latest a consumer
gets — at double the CI cost, to re-prove what `pip install -e` already covers better.
spike-3 independently confirms the low ceiling on such a job's value: even for the major
bump that motivated this issue, no cheap job produces signal, because the break (if any)
lives behind an API key at runtime where CI cannot reach.

## Appetite

**Small.** One new step in an existing workflow, one small script, and two documentation
edits. If this grows into a second test matrix, a caching strategy, or a scheduled job,
the appetite has been exceeded and the extra scope should be dropped rather than absorbed.

## Solution

### Key Elements

1. **Extend `lock-check.yml` rather than adding a workflow.** It is already path-scoped
   to exactly `pyproject.toml` + `uv.lock` + itself, and already pins `astral-sh/setup-uv`
   at `0.12.2`. A lockfile-only PR then shows **one** check whose name and steps say what
   was verified — which is acceptance criterion 4, satisfied structurally rather than by
   asking the reader to cross-reference three workflows.
2. **Add an installability step**: `uv sync --locked --all-extras --no-extra benchmark`.
   `--locked` re-asserts lock/pyproject agreement (so the existing guarantee is not
   weakened) *and* materializes the environment, which is the new assertion.
3. **Add an import step that imports the third-party packages directly**, never popoto's
   guarded wrappers (spike-4). This is the only way the seven never-installed extras get
   any coverage at all.
4. **Document what green means** in `CLAUDE.md`, including spike-3's negative result.

### Flow

```
PR touches pyproject.toml, uv.lock, scripts/check_lock_imports.py, or the workflow
  └─ lock-check job (uv 0.12.2, python 3.12)
       ├─ uv lock --check                                  [existing: consistency]
       ├─ uv sync --locked --all-extras --no-extra benchmark [new: installability]
       └─ uv run --no-sync python scripts/check_lock_imports.py [new: import surface]
```

`scripts/check_lock_imports.py` is added to the job's `paths:` filters. Without it, a PR
that edits only the script never triggers the job that runs it — the script could be
broken on main and nothing would say so. The job id stays `lock-check` and `on:` is
otherwise untouched, so no required-check registration moves.

**No Redis service, and no `popoto` import.** The script imports third-party packages
only; it never imports popoto and therefore never binds a Redis client. This resolves what
would otherwise be a contradiction with spike-4 — importing popoto's own modules proves
nothing about the extras, so there is no reason to import it, and consequently no
`REDIS_URL` discipline to apply here.

### Technical Approach

`scripts/check_lock_imports.py` imports each third-party package that a locked extra
provides and reports its resolved version, collecting every failure rather than stopping at
the first, and exiting non-zero if any failed. It enumerates the packages explicitly rather
than parsing `pyproject.toml`, so that adding an extra without adding it here is a visible
omission rather than a silent one. Nothing in CI can catch that omission, so the script's
header comment says so directly: it names `[project.optional-dependencies]` as the list to
keep in step, and states that a mismatch is review-blocking.

The import loop lives behind a callable `check(specs: list[tuple[str, str]]) -> list[str]`
(import name, owning extra → list of failure messages), with `main()` a thin wrapper over
it and the package list a module-level constant. That shape exists for one reason: it lets
a unit test pass a deliberately bogus import name and assert the failure list is non-empty,
so the script's ability to fail is asserted by the suite rather than by a one-time manual
check recorded in a PR body.

`benchmark` is excluded from the sync (spike-1) and its packages are therefore excluded
from the import list, with the reason stated inline: `tests.yml` already installs them
from floors.

## Test Impact

No change to existing tests. This plan adds CI coverage; it does not alter library
behavior, so no existing unit test can observe it.

One new test file, `tests/test_check_lock_imports.py`, with two cases:

- `check()` returns an empty failure list for a package guaranteed present (`json` — a
  stdlib module, so the test does not itself depend on an optional extra being installed).
- `check()` returns a non-empty failure list for a deliberately bogus import name.

The second case is the one that matters: it asserts the script *can* fail. A smoke that
cannot fail is worth nothing, which spike-4 demonstrates concretely, and a non-vacuity
proof that lives only in a PR body decays the moment someone edits the script. Both cases
run under the ordinary `pytest` invocation and need no Redis.

## Rabbit Holes

- **Running the test suite against the lock.** Rejected in The Decision. If it resurfaces
  during build, the answer is the same: `tests.yml` covers latest-from-floors and the lock
  is a lagging middle point.
- **Trying to make the smoke catch API breaks.** spike-3 settled this. Detecting the
  `anthropic` 1.x break would require calling the API (needs a key, network, and money) or
  hand-maintaining a signature allowlist that rots. Out of scope; documented as a
  limitation instead.
- **Auto-generating the import list from `pyproject.toml`.** Tempting and wrong: a parser
  turns "someone added an extra and forgot the smoke" from a visible diff into silence,
  and it has to encode dist-name → import-name mapping (`ulid-py` → `ulid`, `cyksuid` →
  `cyksuid`, `sentry-sdk` → `sentry_sdk`, `msgpack-numpy` → `msgpack_numpy`) anyway.
- **Adding a Redis service to `lock-check.yml`.** The script never connects. Adding a
  service container to be safe would double the job's setup cost for nothing.
- **Caching `uv`'s environment.** 0.71s warm locally; even a cold CI sync of this set is
  small once `benchmark` is excluded. Revisit only if the step is measured slow.
- **Touching `uv.lock` itself.** This plan changes no dependency versions. #666/#667/#668
  are the team lead's, and #660 closes down its own branch.

## Risks

- **The new step fails on a lock that was already broken before this PR.** Likely, and
  that is the point — but it would surface as *this* PR's failure. Mitigation: the sync is
  run locally against the current lock during build (spike-1 already did, and it passed),
  so the step lands green.
- **`--no-extra benchmark` drifts if `benchmark`'s contents change.** The exclusion is
  about install cost, not correctness; if `benchmark` ever stops pulling torch-sized
  packages the flag is merely unnecessary, never wrong. A comment in the workflow states
  the reason so a future reader can drop it deliberately.
- **A dependabot bump lands a package that imports but is broken at call time.** Real, and
  explicitly not covered (spike-3). The mitigation is documentation, not code: `CLAUDE.md`
  must state what a green lock-check does and does not prove, or the gate becomes a false
  assurance — a strictly worse outcome than the current honest gap.
- **Job-name churn breaking a required-check registration.** Avoided by extending the
  existing `lock-check` job rather than adding a workflow or renaming anything.

## No-Gos (Out of Scope)

- No second test-matrix job, no `pytest` run against the lock.
- No change to `tests.yml`, `lint.yml`, or `.github/dependabot.yml`.
- No change to `pyproject.toml` floors, and no dependency-version changes of any kind.
- No edits to `scripts/mypy_baseline.json`, `setup.cfg`, or `src/popoto/pytest_plugin.py`
  (fenced to another active lane).
- No work on #660, #666, #667, or #668.
- No attempt to detect API-signature breaks.

## Documentation

`CLAUDE.md`, in the **Dependency Updates** section (acceptance criterion 3), gains a
paragraph recording:

1. The support contract: the root lock is the **developer** install path (citing
   `scripts/mypy_baseline.json`'s reliance on it), not a consumer path — PyPI resolves
   `pyproject.toml` floors.
2. What `lock-check.yml` now proves: consistency (`uv lock --check`) **and** installability
   (`uv sync --locked`) **and** import surface for the extras no other job installs.
3. What it does **not** prove: that the bumped packages still work. spike-3's
   `anthropic` 1.2.0 result is named explicitly so the next reader does not over-trust a
   green check.
4. Why `benchmark` is excluded and where those packages are covered instead.

The `Verifying in a worktree` list mentions the extras a fresh worktree venv omits; no edit
is needed there, since this plan changes CI rather than local setup.

## Success Criteria

1. `.github/workflows/lock-check.yml` installs the locked environment and exercises its
   import surface. Its `paths:` filters gain `scripts/check_lock_imports.py` and are
   otherwise unchanged; the job id stays `lock-check`.
2. `scripts/check_lock_imports.py` exits 0 on the current lock, and its ability to fail is
   asserted by `tests/test_check_lock_imports.py` — not by a one-time manual demonstration.
3. `CLAUDE.md`'s Dependency Updates section states the support contract, what green proves,
   and what it does not (including the spike-3 limitation).
4. A reader looking at a lockfile-only PR's checks can tell from the `lock-check` job alone
   whether the lock was installed. No cross-referencing of `tests.yml` or `lint.yml`
   required (acceptance criterion 4). Concretely: every step in the job has a `name:` that
   states what it proves, so the check's step list is self-describing in the GitHub UI.
5. No file outside `.github/workflows/lock-check.yml`, `scripts/check_lock_imports.py`,
   `tests/test_check_lock_imports.py`, `CLAUDE.md`, and this plan is modified.

## Step by Step Tasks

1. **Write `scripts/check_lock_imports.py`.** Module-level constant listing (import name,
   owning extra) pairs covering `dataframe`, `ulid`, `ksuid`, `embeddings`, `voyage`,
   `openai`, `anthropic`, `monitoring`, `mcp` — note the dist-name/import-name splits
   (`ulid-py` → `ulid`, `sentry-sdk` → `sentry_sdk`, `msgpack-numpy` → `msgpack_numpy`).
   Expose `check(specs) -> list[str]`; `main()` prints each resolved version, prints the
   failures, and exits non-zero if the list is non-empty. Header comment states: keep in
   step with `[project.optional-dependencies]` and treat a mismatch as review-blocking
   (nothing in CI enforces it); `benchmark` is intentionally absent and why; the imports
   are of third-party packages *directly* because popoto's own modules guard them
   (spike-4); the script must not import popoto.
   *Validate:* `python scripts/check_lock_imports.py` exits 0 in the synced env.
2. **Write `tests/test_check_lock_imports.py`.** Two cases per Test Impact — `check()`
   clean on `json`, `check()` non-empty on a bogus import name.
   *Validate:* `pytest tests/test_check_lock_imports.py` passes, and passes with no Redis
   reachable.
3. **Extend `.github/workflows/lock-check.yml`.** After the existing `uv lock --check`
   step, add `uv sync --locked --all-extras --no-extra benchmark`, then `uv run --no-sync
   python scripts/check_lock_imports.py`. Give each step a `name:` that states what it
   proves (Success Criterion 4) and a comment explaining the `--no-extra benchmark`
   exclusion. Add `scripts/check_lock_imports.py` to both `paths:` lists. Do not rename the
   job.
   *Validate:* `python -c 'import yaml'` parses the file; job id still `lock-check`; both
   `paths:` lists contain the script.
4. **Update `CLAUDE.md`.** Add the Dependency Updates paragraph per the Documentation
   section above, naming spike-3's `anthropic` 1.2.0 result explicitly.
   *Validate:* re-read for accuracy against the final workflow.
5. **Run the Verification table.** Then open the PR with `Closes #669` at line 1.

## Verification

| # | Command | Expected |
|---|---|---|
| 1 | `uv sync --locked --all-extras --no-extra benchmark` (uv 0.12.2, py3.12) | exit 0 |
| 2 | `uv run --no-sync python scripts/check_lock_imports.py` | exit 0, one version line per package |
| 3 | `uv pip uninstall anthropic && uv run --no-sync python scripts/check_lock_imports.py` | **non-zero**, names `anthropic` |
| 4 | `pytest tests/test_check_lock_imports.py -q` | 2 passed |
| 5 | `python -c "import yaml; d=yaml.safe_load(open('.github/workflows/lock-check.yml')); print(list(d['jobs'])); print(d[True]['pull_request']['paths']); print([s.get('name') for s in d['jobs']['lock-check']['steps']])"` | `['lock-check']`; paths include `scripts/check_lock_imports.py`; every added step has a non-null `name` (Success Criterion 4) |
| 6 | `git diff --stat origin/main...HEAD` | only the five files in Success Criterion 5 |
| 7 | `git diff origin/main...HEAD -- CLAUDE.md \| grep -c 'anthropic'` | ≥ 1 — anchors on the spike-3 limitation, which is unique to the new paragraph (a bare `grep uv.lock CLAUDE.md` already matches the pre-existing text and would pass vacuously) |

Rows 1–3 run in a scratch `UV_PROJECT_ENVIRONMENT` outside the checkout so the lane's
`.venv` is untouched, and row 3 restores `anthropic` afterwards. Row 4 runs in the lane's
`.venv`. No Redis is required for any row.

## Critique Findings (round 1, applied)

`/do-plan-critique` FULL roster, 2026-09-07 — verdict **READY TO BUILD (with concerns)**:
0 blockers, 4 concerns, 3 nits. All are folded into the sections above; the run is capped
at one round by the supervisor, so build proceeds from this revision.

| Critic | Finding | Resolution |
|---|---|---|
| Risk & Robustness | `paths:` never lists the new script, so a PR editing only it skips the job | Path filter added — Solution, Task 3, Success Criterion 1 |
| Risk & Robustness | Non-vacuity proven once by hand, recorded only in a PR body | Replaced by `tests/test_check_lock_imports.py` and a `check()` seam — Test Impact, Task 2 |
| Risk & Robustness (nit) | A new extra can land with no matching import line, unenforced | Script header states the mismatch is review-blocking — Task 1 |
| Scope & Value | Flow/Task 1 set `REDIS_URL` "before importing popoto" while Technical Approach says the script never imports popoto | Contradiction removed: the script imports no popoto and needs no `REDIS_URL` |
| Scope & Value (nit) | Criterion 4 had no mechanical check | Verification row 5 asserts every added step carries a `name:` |
| History & Consistency | `grep -c 'uv.lock' CLAUDE.md` passes on pre-existing text — vacuous | Replaced by a diff-scoped grep anchored on `anthropic` — Verification row 7 |
| History & Consistency (nit) | Row 3 used a bare `python`, inconsistent with row 2's interpreter | Row 3 now uses `uv run --no-sync` |

## Open Questions

None. The one genuine decision (acceptance criterion 1) is resolved above with evidence
from the tree rather than deferred to the reviewer, and the three spikes that could have
changed the design all returned before the plan was written.
