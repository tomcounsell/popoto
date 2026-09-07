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

Placeholder.

## Appetite

Placeholder.

## Solution

Placeholder.

## Test Impact

Placeholder.

## Rabbit Holes

Placeholder.

## Risks

Placeholder.

## No-Gos (Out of Scope)

Placeholder.

## Documentation

Placeholder.

## Success Criteria

Placeholder.

## Step by Step Tasks

Placeholder.

## Verification

Placeholder.

## Open Questions

Placeholder.
