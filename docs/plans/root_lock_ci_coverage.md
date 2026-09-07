---
status: Planning
type: chore
appetite: Small
owner: Dev (sdlc-669)
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/669
last_comment_id: none
revision_applied: false
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
PR touches pyproject.toml or uv.lock
  └─ lock-check job (uv 0.12.2, python 3.12)
       ├─ uv lock --check                                  [existing: consistency]
       ├─ uv sync --locked --all-extras --no-extra benchmark [new: installability]
       └─ python scripts/check_lock_imports.py              [new: import surface]
```

No Redis service is needed: the script imports only, never connects. It nonetheless sets
`REDIS_URL` to an explicit non-zero database before importing popoto, per the repo's
standing DB-0 discipline — `popoto` binds `DEFAULT_URL` (database 0) at import time, and
while importing opens no socket, the script must not be the place that normalizes an
unbound import.

### Technical Approach

`scripts/check_lock_imports.py` imports each third-party package that a locked extra
provides and reports its resolved version, exiting non-zero on the first failure. It
enumerates the packages explicitly rather than parsing `pyproject.toml`, so that adding an
extra without adding it here is a visible omission rather than a silent one — and a
comment in the script says so, naming `[project.optional-dependencies]` as the list to
keep it in step with.

`benchmark` is excluded from the sync (spike-1) and its packages are therefore excluded
from the import list, with the reason stated inline: `tests.yml` already installs them
from floors.

## Test Impact

No change to `tests/`. This plan adds CI coverage; it does not alter library behavior, so
no unit test can observe it.

The new script is itself the assertion, and its non-vacuity is verified by hand during
build (see Verification): with a required package uninstalled it must exit non-zero. A
smoke that cannot fail is worth nothing, which spike-4 demonstrates concretely.

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
   import surface, and its path filters are unchanged (still `pyproject.toml`, `uv.lock`,
   the workflow itself).
2. `scripts/check_lock_imports.py` exits 0 on the current lock and exits **non-zero** when
   a required package is absent — demonstrated, not asserted.
3. `CLAUDE.md`'s Dependency Updates section states the support contract, what green proves,
   and what it does not (including the spike-3 limitation).
4. A reader looking at a lockfile-only PR's checks can tell from the `lock-check` job alone
   whether the lock was installed. No cross-referencing of `tests.yml` or `lint.yml`
   required (acceptance criterion 4).
5. No file outside `.github/workflows/lock-check.yml`, `scripts/check_lock_imports.py`,
   `CLAUDE.md`, and this plan is modified.

## Step by Step Tasks

1. **Write `scripts/check_lock_imports.py`.** Explicit list of (import name, extra) pairs
   covering `dataframe`, `ulid`, `ksuid`, `embeddings`, `voyage`, `openai`, `anthropic`,
   `monitoring`, `mcp`. Import each, print its resolved version, collect failures, exit
   non-zero on any. Header comment states: keep in step with
   `[project.optional-dependencies]`; `benchmark` is intentionally absent and why; the
   imports are of third-party packages *directly* because popoto's own modules guard them
   (spike-4). Set `REDIS_URL` to a non-zero database before importing popoto, per the
   repo's DB-0 discipline.
   *Validate:* `python scripts/check_lock_imports.py` exits 0 in the synced env.
2. **Prove the script is not vacuous.** Uninstall one required package in a scratch
   environment, re-run, confirm non-zero exit and a named failure; reinstall.
   *Validate:* the observed exit code and message are recorded in the PR body.
3. **Extend `.github/workflows/lock-check.yml`.** Add a `uv python install 3.12` /
   `actions/setup-python` step as needed, then `uv sync --locked --all-extras --no-extra
   benchmark`, then `uv run --no-sync python scripts/check_lock_imports.py`. Comment each
   new step with what it proves. Do not rename the job or touch `on:`/`paths:`.
   *Validate:* `yq`/`python -c 'import yaml'` parses the file; job id still `lock-check`.
4. **Update `CLAUDE.md`.** Add the Dependency Updates paragraph per the Documentation
   section above.
   *Validate:* `black --check`-irrelevant (markdown); re-read for accuracy against the
   final workflow.
5. **Run the Verification table.** Then open the PR with `Closes #669` at line 1.

## Verification

| # | Command | Expected |
|---|---|---|
| 1 | `uv sync --locked --all-extras --no-extra benchmark` (uv 0.12.2, py3.12) | exit 0 |
| 2 | `uv run --no-sync python scripts/check_lock_imports.py` | exit 0, one version line per package |
| 3 | `uv pip uninstall anthropic && python scripts/check_lock_imports.py` | **non-zero**, names `anthropic` |
| 4 | `python -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/lock-check.yml')); print(list(d['jobs']))"` | `['lock-check']` |
| 5 | `git diff --stat origin/main...HEAD` | only the four files in Success Criterion 5 |
| 6 | `grep -c 'uv.lock' CLAUDE.md` | > 0, and the new paragraph reads correctly |

Commands 1–3 run in a scratch `UV_PROJECT_ENVIRONMENT` outside the checkout so the lane's
`.venv` is untouched. No Redis is required for any of them.

## Open Questions

None. The one genuine decision (acceptance criterion 1) is resolved above with evidence
from the tree rather than deferred to the reviewer, and the three spikes that could have
changed the design all returned before the plan was written.
