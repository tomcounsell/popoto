---
status: Ready
type: chore
appetite: Small
owner: Valor Engels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/545
last_comment_id: 5216396110
---

# Re-strengthen the README Valkey claim on observed CI evidence

## Problem

`README.md:92` says Valkey is a first-class target and then supports it with the
weakest available evidence:

> **Valkey is a first-class target.** Popoto uses core Redis data types and commands
> only, with no Redis-module dependency, and the suite carries explicit Valkey-safety
> tests asserting that indexes stay on plain types. The same code runs against either
> server.

Every clause is true. "The suite carries explicit Valkey-safety tests" resolves to
`tests/test_tag_field.py::TestTagFieldValkeySafety` and the parity guards in
`tests/test_validity_field.py:403` and `tests/test_existence_filter.py:17`. But the
paragraph describes *tests that exist*, not *a server the suite was run against*. When
it was written that was the honest ceiling: #405 had deleted `test-valkey.yml`, so no
Valkey server ran anywhere except a developer laptop that, by the local runner's own
policy, never points at one.

That is no longer the ceiling. PR #544 landed `.github/workflows/tests.yml` on
2026-08-10 with a `pytest (Valkey)` job against a `valkey/valkey:8-alpine` service
container, and the job has now run 59 times on `main`. The README is understating what
the project can prove.

**Current behavior:**

1. `README.md:92` and its mirror at `docs/index.md:108-113` claim compatibility from
   test *design*, and never mention that CI runs the whole suite against a real Valkey
   server. A reader evaluating Popoto for a Valkey deployment cannot tell from the
   README whether anyone has ever run it on Valkey.
2. `docs/sdlc/do-test.md:16-17` still tells agents that `scripts/ci-local.sh` mirrors
   `test-valkey.yml` and `stress-tests.yml`. Both workflow files were deleted by #405
   fourteen months of commits ago; neither name exists in `.github/workflows/`. The
   same paragraph's line 11 asserts "this repo has NO ruff", which #505/#542 made false.
3. `.github/workflows/tests.yml:23-24` asserts "Runs on main are never cancelled, so
   every merged commit keeps a complete result." 31 of the 92 `main` runs since #544
   are `cancelled` with zero jobs. The comment describes an invariant the workflow does
   not have.
4. The workflow's Valkey health check prints `redis_version`, which
   `valkey/valkey:8-alpine` reports as `7.2.4` (its Redis-compatibility version). No
   step ever prints the actual Valkey version, so a README claim naming "Valkey 8"
   rests on the image tag alone and the run log cannot corroborate it.

**Desired outcome:**

The README and docs index state the concrete, falsifiable fact — the full suite runs
against a pinned Valkey image on every pull request and every push to `main`, with a
link to the workflow — and the three stale assertions above are corrected so the
supporting comments say what is actually true.

## Freshness Check

**Baseline commit:** `53a65b8d318fe65f47a801d7771e65a6e9f5d566`
**Issue filed at:** 2026-08-07T11:13:27Z
**Disposition:** Minor drift — one of the issue's three scope items is already done.

**File:line references re-verified:**

- `README.md:92` — issue quotes the "Valkey is a first-class target" paragraph — still
  holds, byte-identical to the issue's quote.
- `docs/index.md:108-113` — not named by the issue; found during planning to carry the
  same claim in prose form. In scope as the mirror the task brief anticipated.
- `scripts/ci-local.sh` — issue claims it "still names `test-valkey.yml` and
  `stress-tests.yml`" and still asserts "GitHub still runs the real Valkey job on
  PR/merge as the final word" — **drifted, already fixed.** The header table at
  `scripts/ci-local.sh:11` now reads `tests → tests.yml`, and lines 18-24 read
  "tests.yml runs a real Valkey job on every PR as the final word (#544)" and "The
  stress suite has no workflow". Nothing stale remains in that file. This plan
  therefore does not modify `scripts/ci-local.sh` at all; it only asserts, in
  Verification, that the stale names have not returned.
- `docs/sdlc/do-test.md:16-17` — carries the exact stale text the issue attributes to
  `ci-local.sh`. This is where scope item 3 actually lands.

**Cited sibling issues/PRs re-checked:**

- #544 — merged 2026-08-10T02:44:15Z as `d675218`. Added `tests.yml`. Intentionally left
  the README alone, which is why this issue exists.
- #405 — merged. Removed `test-valkey.yml` and `stress-tests.yml` in favour of
  `scripts/ci-local.sh`. Source of every stale workflow name in the tree.
- #55 "full support for Valkey" — closed. Predates both.

**Commits on main since issue was filed (touching referenced files):**

- `d675218` ci: run pytest on PRs against Redis and Valkey service containers (#544) —
  creates the evidence this plan consumes.
- `926953c` chore(#505): ruff config + CI lint gate — makes `docs/sdlc/do-test.md:11`'s
  "NO ruff" false.
- `a6d9f43` chore(#578,#579): correct TAG_SWAP_LUA migration comment, gate black in CI
  (#607) — last touch to `scripts/ci-local.sh`; part of why its header is already correct.

**Active plans in `docs/plans/` overlapping this area:** none. No plan slug mentions
Valkey CI, the README claim block, or `docs/sdlc/do-test.md`.

**Notes:** The issue's closing criterion 3 is two-thirds satisfied by drift. Rather than
narrow the plan, the same class of defect is fixed where it actually lives
(`docs/sdlc/do-test.md`), and Verification pins the already-correct file so the fix
cannot silently regress.

## Prior Art

- **PR #405**: *chore(ci): shift CI validation local, remove redundant GH test
  workflows* — merged. Deleted `test-valkey.yml` and `stress-tests.yml`, motivated by
  GitHub Actions unreliability. Succeeded at its stated goal and left one unstated
  cost: zero automated Valkey coverage, while the README kept implying otherwise. Every
  stale workflow name this plan corrects traces to this PR.
- **PR #544**: *ci: run pytest on PRs against Redis and Valkey service containers* —
  merged 2026-08-10. Restored the Valkey job as an independently named check. Succeeded.
  Deliberately did not touch the README, deferring that to this issue so the claim would
  rest on observed runs rather than on a workflow nobody had watched.
- **Issue #55**: *full support for Valkey* — closed long before either. Established the
  no-Redis-modules rule that makes the compatibility claim true in the first place.

**Root cause pattern (why this keeps recurring):** documentation asserts a CI fact, CI
changes, documentation is not part of the CI change's diff. This plan adds grep-based
Verification rows naming the deleted workflow files, so the next deletion or rename
fails a check instead of quietly aging into another stale sentence.

## Research

Purely internal — the evidence is this repository's own GitHub Actions history and its
tracked files. No external libraries, APIs, or ecosystem patterns are involved, and no
WebSearch was performed.

One externally-sourced fact was verified from run logs rather than assumed: the
`valkey/valkey:8-alpine` image reports `redis_version: 7.2.4` in `INFO server` while
reporting `server_name: valkey`. Valkey publishes a Redis-compatibility version in that
field and its own version separately. This is why the claim must be anchored to the
image tag and, after task 3, to a printed `server_version`.

## Evidence: the Valkey job on `main`

All figures below come from `gh run list --workflow=tests.yml --branch=main` and
`gh run view <id> --json jobs` executed at plan time against
`53a65b8d318fe65f47a801d7771e65a6e9f5d566`. Job conclusions were read per run; none are
inferred from the run-level rollup.

Aggregate over all 92 `tests.yml` runs on `main` since `d675218` (2026-08-10) through
plan time:

| Run conclusion | Count |
|---|---|
| success | 59 |
| failure | 1 |
| cancelled (0 jobs started) | 31 |
| in progress at plan time | 1 |

Consecutive green runs, most recent first, each with `pytest (Valkey)` confirmed
`success` individually:

| Run ID | Head SHA | Created (UTC) | pytest (Valkey) | pytest (Redis) |
|---|---|---|---|---|
| 33850587942 | `a6d9f434` | 2026-09-04T07:50:37Z | success | success |
| 33849675202 | `65cffcf1` | 2026-09-04T07:38:45Z | success | success |
| 33849503418 | `ab4762c7` | 2026-09-04T07:36:26Z | success | success |
| 33848987612 | `f35ca9e8` | 2026-09-04T07:29:49Z | success | success |
| 33848603103 | `bb38f423` | 2026-09-04T07:24:49Z | success | success |
| 33848216728 | `dc049ad8` | 2026-09-04T07:19:43Z | success | success |
| 33742861319 | `cddff6ba` | 2026-09-03T10:10:15Z | success | success |
| 33709220996 | `07b7268c` | 2026-09-03T02:51:47Z | success | success |
| 31350596164 | `d6752180` | 2026-08-10T02:44:17Z | success | success |

The last row is the merge commit of #544 itself — the job has been green from its first
run on `main` onward.

**The one failure is not a Valkey failure.** Run 33843909094 (`edf71ad8`,
2026-09-04T06:19:46Z, "fix(#596): deploy-level kill switch and loud first eviction")
failed in *both* jobs: `pytest (Redis) -> failure` and `pytest (Valkey) -> failure`. A
code defect that reproduced identically on both servers. Across 60 completed runs there
has been **zero** Valkey-only divergence.

**Identical pass counts.** From the log of run 33850587942:

```
pytest (Redis)   collected: 3472 → 3406 passed, 36 skipped, 32 deselected in 150.80s
pytest (Valkey)  collected: 3472 → 3406 passed, 36 skipped, 32 deselected in 203.24s
```

**Server identity is asserted, not assumed.** The Valkey job's "Verify server is Valkey"
step reads `INFO server` and exits non-zero unless `server_name == "valkey"`
(`.github/workflows/tests.yml:137-152`). Run 33850587942 printed:

```
pytest (Valkey)  server_name: valkey   version: 7.2.4
pytest (Redis)   server_name: redis    version: 7.4.11
```

**Verdict on the closing criterion:** criterion 1 is met with margin. The bar was "at
least one merged commit on `main`, ideally a few consecutive ones". Observed: 59 green
runs across 25 days, 9 of them spot-checked at job level, the only failure server-neutral.

**What this evidence does not establish.** Per the issue's Notes, the two metric families
stay separate. This establishes *the suite passes against Valkey*. It does not establish
*Valkey and Redis produce identical results* — the pass counts match, but the two jobs
run on different containers against different data, not a differential comparison of
outputs. No wording produced by this plan may claim parity of results.

## Data Flow

Not applicable — no runtime data path changes. The change is four text edits (two
user-facing prose blocks, two comment blocks) plus two `echo`-equivalent lines in a
workflow health check.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none. No Python source file is touched.
- **Coupling**: unchanged at runtime. Documentation coupling *increases deliberately*:
  the README will name `.github/workflows/tests.yml` and its image pins, so moving the
  pins obliges a README edit. Verification rows enforce the reverse direction.
- **Data ownership**: unchanged.
- **Reversibility**: trivial. `git revert` on a docs-and-comments commit.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (evidence gathered and recorded in this plan; nothing to align on)
- Review rounds: 1 (wording accuracy is the whole deliverable, so it gets one read)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `gh` authenticated | `gh auth status` | Re-confirm run IDs in the evidence table if a reviewer challenges them |
| Repo checkout at plan baseline or later | `git merge-base --is-ancestor 53a65b8d318fe65f47a801d7771e65a6e9f5d566 HEAD` | The `tests.yml` line numbers cited below assume it |

No Redis or Valkey server is required to build or verify this work. **No task in this
plan connects to a Redis server at all**, so the DB 0 hazard in `CLAUDE.md` cannot arise;
the Verification commands are `grep`, `bash -n`, and `mkdocs`.

## Solution

### Key Elements

- **README claim block** (`README.md:92`): keeps the mechanism sentence, adds the
  observed-CI sentence naming both image pins and linking the workflow.
- **Docs index mirror** (`docs/index.md:108-113`): same upgrade in the docs site's own
  voice, with an mkdocs-appropriate absolute link.
- **SDLC test doc** (`docs/sdlc/do-test.md:10,16-17`): replaces two deleted workflow
  filenames with `tests.yml`, and drops the false "NO ruff" clause.
- **Workflow comment honesty** (`.github/workflows/tests.yml:23-24`): the
  "never cancelled" claim is replaced with what the concurrency group actually does.
- **Valkey version print** (`.github/workflows/tests.yml:137-152`): the health check
  also prints `server_version`, so a run log can corroborate the README's "Valkey 8".

### Flow

Reader lands on README → reaches "Valkey is a first-class target" → reads that the full
suite runs against `valkey/valkey:8-alpine` on every PR and every push to `main` → clicks
the workflow link → sees `pytest (Valkey)` as an independently named green check → opens
a run log → sees `server_name: valkey` and a real Valkey `server_version`.

Agent reads `docs/sdlc/do-test.md` → sees `tests` mirrors `tests.yml` → the name resolves
to a file that exists → runs `scripts/ci-local.sh` with correct expectations.

### Technical Approach

**Proposed README wording** (builder may improve the prose, must not weaken or overstate
the claim):

> **Valkey is a first-class target.** Popoto uses core Redis data types and commands
> only, with no Redis-module dependency, and the suite carries explicit Valkey-safety
> tests asserting that indexes stay on plain types. Since August 2026 the test suite
> (everything except the `slow`-marked stress tests) also runs against a real Valkey
> server on every pull request and every push to `main`, as a separately named
> `pytest (Valkey)` check in [`tests.yml`](.github/workflows/tests.yml), pinned to
> `valkey/valkey:8-alpine` alongside the Redis job's `redis:7-alpine`. The job asserts
> via `INFO server` that the container really is Valkey before pytest starts, and there
> has been no Valkey-only failure across 60 completed runs.

Three wording constraints, each traceable to the issue:

1. **Name the pins.** `valkey/valkey:8-alpine` and `redis:7-alpine` appear literally, so
   the claim expires visibly when the images move.
2. **Do not claim result parity.** "no Valkey-only failure" is a statement about run
   conclusions, which is what was read for all 60 completed runs. Pass-count identity
   was read for exactly one run (33850587942) and must NOT be generalised to "every
   run". Anything of the form "identical results",
   "produces the same output", or "verified equivalent" is out of bounds — that is the
   differential-run claim the issue explicitly excludes.
3. **Do not claim the stress suite.** `tests.yml` runs `-m "not slow"`, so the phrase
   "full test suite" is out of bounds in both files. Use the explicit form above —
   "the test suite (everything except the `slow`-marked stress tests)".

**`docs/index.md`** carries the same three constraints. Its link must be
`https://github.com/tomcounsell/popoto/blob/main/.github/workflows/tests.yml` — a
repo-relative path does not resolve from the rendered docs site.

**`.github/workflows/tests.yml:23-24`**, replacing the false invariant with the real
behavior. A queued run *is* superseded when a newer push lands; only *started* runs are
protected:

> A newer push to a PR cancels the older run for that PR. On `main`, `cancel-in-progress`
> is false, so a run that has started always finishes — but a run still queued when the
> next push arrives is superseded by it, which is why some `main` runs show as
> `cancelled` with no jobs.

**`.github/workflows/tests.yml` health check**: add a `server_version` line to the
existing print block in the Valkey job, reading the field defensively:
`print("server_version:", info.get("server_version") or info.get("valkey_version"))`.
The key name is not guaranteed across images, and this plan may not assert on it. `redis_version` on Valkey is the
Redis-compatibility version (`7.2.4`), not the Valkey version, and the current log is
therefore mildly misleading next to a README that says "Valkey 8". Keep the existing
`redis_version` print; add rather than replace. The `server_name != "valkey"` guard is
not touched. Mirroring the addition into the Redis job is optional and harmless.

## Failure Path Test Strategy

### Exception Handling Coverage
- [x] No exception handlers in scope. This plan touches no Python source file. The only
      executable change is two print lines inside an existing `python - <<'PY'` heredoc
      in a workflow step whose failure mode is the step's own non-zero exit, already
      covered by the `server_name` guard.

### Empty/Invalid Input Handling
- [x] Not applicable to prose. For the workflow addition: `info.get("server_version")`
      returns `None` on a server that does not publish the field, which prints as
      `server_version: None` and must **not** be allowed to fail the step. Do not add a
      guard on it — the identity guard is `server_name`, and a hard assertion on
      `server_version` would make the job brittle across image updates for zero gain.

### Error State Rendering
- [x] The user-visible surface is rendered documentation. The failure mode is a broken
      link or a docs build error, covered by `mkdocs build --strict` in Verification.

## Test Impact

No existing tests affected. This work changes two Markdown prose blocks, one Markdown
comment block, and one workflow file's comments and health-check prints. No Python
module, no public API, and no test fixture is touched, and no test asserts on README
text. The `mkdocs build --strict` gate is the only automated consumer of any changed
file and is run in Verification.

## Rabbit Holes

- **Building the differential Redis-vs-Valkey run.** Tempting because the pass counts
  already match. It is a genuinely different claim needing per-assertion output
  comparison, and the issue rules it out of scope. Do not start it.
- **Auditing every "works on both Redis and Valkey" sentence in the tree.** `docs/fields.md`
  has five, `docs/benchmarks.md` has more. They are per-feature mechanism claims and are
  true; rewriting them all to cite CI would balloon a Small into a survey.
  `docs/benchmarks.md:1087-1091` is the one that genuinely records a missing Valkey run,
  and it is about a *benchmark*, not the suite — a different metric family. Leave it.
- **Restructuring `scripts/ci-local.sh`.** Its header is already correct. Reading it
  closely and deciding the gate table could be clearer is scope creep with a real
  regression risk in a script nobody's tests cover.
- **Chasing the 31 cancelled `main` runs as a CI bug.** Superseding a *queued* run is
  documented GitHub concurrency behavior, not a misconfiguration. The defect is the
  comment claiming otherwise. Fix the comment, not the workflow's concurrency block.
- **Making the "since August 2026" date precise to the commit.** `d675218` is in this
  plan for the record. Putting a SHA in the README ages worse than a month.

## Risks

### Risk 1: The strengthened claim outlives the evidence
**Impact:** A future PR bumps `valkey/valkey:8-alpine` to `9-alpine`, deletes the job, or
renames the workflow. The README then makes a specific, checkable, false claim — strictly
worse than today's vague-but-true one, and exactly the failure #405 caused.
**Mitigation (partial — stated honestly):** Verification rows assert that
`.github/workflows/tests.yml` exists, that it still contains `valkey/valkey:8-alpine`,
and that the README names the same string. **These are one-time build-time greps run by
task 5 inside this PR; nothing in `.github/workflows/` runs them on future PRs.** A later
bump to `valkey:9-alpine` therefore fails no automated check, and the residual rot risk
is accepted on the record rather than papered over. Adding a standing grep step to
`lint.yml` was considered and deliberately deferred: it widens this Small's file set
beyond the four files task 5 asserts on, and belongs in its own change.

### Risk 2: Wording drifts into the parity claim
**Impact:** "runs against Valkey" quietly becomes "verified identical on Valkey", merging
two metric families the issue insists stay separate — the repo's standing doctrine.
**Mitigation:** The three wording constraints above are explicit, and Verification carries
an anti-criterion grepping the README and docs index for parity vocabulary.

### Risk 3: The stale-name fix lands in the wrong file
**Impact:** The issue names `scripts/ci-local.sh`; that file is already clean. A builder
following the issue text literally either edits nothing and reports done, or "fixes"
correct text into something worse.
**Mitigation:** The Freshness Check states the drift outright and task 2 names
`docs/sdlc/do-test.md` as the target. A Verification row asserts the stale names are
absent from `scripts/ci-local.sh` *and* `docs/`, so either file regressing fails.

### Risk 4: The docs-site link is written as a repo-relative path
**Impact:** `.github/workflows/tests.yml` renders as a dead link on popoto.io, and
`mkdocs build --strict` may not catch an external-looking relative path.
**Mitigation:** The absolute URL is specified in Technical Approach, and a Verification
row greps `docs/index.md` for the `https://github.com/` form specifically.

## Race Conditions

No race conditions identified. The change is to static text files and workflow comments.
There is no concurrent access, no shared mutable state, and no ordering requirement
between the tasks beyond the plan's stated dependencies.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #545] The differential Redis-vs-Valkey output-comparison run. It is the
  second metric family named in this issue's own Notes as out of scope for #544 and not
  claimed here; if it is ever wanted it needs its own issue, harness, and appetite. This
  plan's anti-criterion rows enforce that no wording implies it has been done.
- [ORDERED] Bumping `valkey/valkey:8-alpine` to a newer Valkey major. Any bump must be
  observed green on `main` before the README names the new version, which is the same
  gate that produced this plan — it waits on runs that do not exist yet.

Everything else the issue asks for is done inside this plan: the README, the docs mirror,
and the stale workflow names are all in scope and all in the task list.

## Update System

No update system changes required. Nothing is deployed or propagated by this work; the
docs site rebuild is the existing `deploy-docs.yml` path and needs no modification.

## Agent Integration

No agent integration required — no tool surface, MCP server, or entry point is involved.
One agent-facing artifact *is* corrected: `docs/sdlc/do-test.md` is read by agents running
`/do-test`, and task 2 fixes the two deleted workflow names and the false "NO ruff"
clause it feeds them.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` page applies. This is a correction to top-level claims, not a
      new capability, so no new feature page and no index entry.

### External Documentation Site
- [ ] Update `docs/index.md:108-113` ("Valkey is a first-class target") to match the
      README's strengthened claim, using the absolute workflow URL.
- [ ] Verify `mkdocs build --strict` passes.

### Inline Documentation
- [ ] `.github/workflows/tests.yml:23-24` — replace the false "never cancelled"
      invariant with the real concurrency behavior.
- [ ] `docs/sdlc/do-test.md:10,16-17` — `tests.yml` replaces the two deleted workflow
      names; drop the "NO ruff" clause.

## Success Criteria

- [ ] `README.md` states that the test suite — everything except the `slow`-marked
      stress tests — runs against Valkey on every PR and push to `main`, names
      `valkey/valkey:8-alpine` and `redis:7-alpine`, and links
      `.github/workflows/tests.yml`. It does not use the unqualified phrase "full test
      suite", and does not claim pass-count identity on "every run".
- [ ] `docs/index.md` carries the same claim with an absolute GitHub URL.
- [ ] Neither file claims that Redis and Valkey produce identical *results*.
- [ ] `test-valkey.yml` and `stress-tests.yml` appear nowhere in `docs/` or `scripts/`,
      except in `.github/workflows/tests.yml:5`, where the reference is historical and
      correct ("#405 removed stress-tests.yml and test-valkey.yml").
- [ ] `.github/workflows/tests.yml` no longer claims runs on `main` are never cancelled.
- [ ] The Valkey health check prints a non-`redis_version` server version field (or
      `None` if the server publishes neither `server_version` nor `valkey_version`) in
      addition to `redis_version`, reading both key names. Checkable from the diff.
- [ ] `bash -n scripts/ci-local.sh` exits 0 and the file is unmodified by this work.
- [ ] `mkdocs build --strict` passes.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (claims)**
  - Name: `claim-builder`
  - Role: README and docs-index wording; the workflow comment and health-check edit
  - Agent Type: builder
  - Resume: true

- **Documentarian (sdlc-doc)**
  - Name: `sdlc-doc-writer`
  - Role: `docs/sdlc/do-test.md` stale-name and ruff-clause corrections
  - Agent Type: documentarian
  - Resume: true

- **Validator (claims)**
  - Name: `claim-validator`
  - Role: verifies wording against the three constraints and runs the Verification table
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Strengthen the README and docs-index claim
- **Task ID**: build-claim
- **Depends On**: none
- **Validates**: no test files; verified by the grep rows in `## Verification`
- **Informed By**: the Evidence section above — 59 green `main` runs, 9 spot-checked,
  one server-neutral failure, identical pass counts on run 33850587942
- **Assigned To**: claim-builder
- **Agent Type**: builder
- **Parallel**: true
- Rewrite the `README.md:92` paragraph per the proposed wording in Technical Approach.
- Rewrite `docs/index.md:108-113` to match, using
  `https://github.com/tomcounsell/popoto/blob/main/.github/workflows/tests.yml`.
- Preserve the existing mechanism sentence (core types only, no modules, Valkey-safety
  tests) in both — it is true and is what makes the CI result meaningful.
- Preserve the `REDIS_URL` example block and surrounding structure in `docs/index.md`.
- Obey all three wording constraints. Do not write "identical results", "parity", or
  "equivalent output" in either file.
- Run `black --check src/ tests/` is not needed; run `mkdocs build --strict`.

### 2. Correct the stale workflow names in the SDLC test doc
- **Task ID**: build-sdlc-doc
- **Depends On**: none
- **Assigned To**: sdlc-doc-writer
- **Agent Type**: documentarian
- **Parallel**: true
- In `docs/sdlc/do-test.md:16-17`, replace `test-valkey.yml` and `stress-tests.yml` with
  `tests.yml`, matching how `scripts/ci-local.sh:11` already describes the same gates.
  Note that `stress` has no workflow at all: `tests.yml` runs `-m "not slow"`, so stress
  is local-only.
- Update the closing sentence so it names `tests.yml` as what runs the Valkey job.
- At `docs/sdlc/do-test.md:10`, drop the "this repo has NO ruff" clause — `ruff check
  src/` is gated by `lint.yml` as of #505/#542. Keep the line-length and isort notes.
- Do not modify `scripts/ci-local.sh`. Its header is already correct.

### 3. Make the workflow comments and health check honest
- **Task ID**: build-workflow
- **Depends On**: none
- **Assigned To**: claim-builder
- **Agent Type**: builder
- **Parallel**: true
- Replace `.github/workflows/tests.yml:23-24` ("Runs on main are never cancelled…") with
  the corrected description in Technical Approach. Do not change the `concurrency:` block
  itself.
- In the Valkey job's "Verify server is Valkey" step, add
  `print("server_version:", info.get("server_version") or info.get("valkey_version"))`
  alongside the existing `redis_version` print. Read both keys — `valkey/valkey:8-alpine`
  is not guaranteed to publish the version under `server_version`, and a bare
  `info.get("server_version")` would silently print `None` while every check passed. Do
  not assert on it and do not remove the `redis_version` print or the `server_name` guard.
- Leave `.github/workflows/tests.yml:5` alone — its mention of the two deleted workflows
  is a historical statement about #405 and is correct.

### 4. Documentation pass
- **Task ID**: document-feature
- **Depends On**: build-claim, build-sdlc-doc, build-workflow
- **Assigned To**: sdlc-doc-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Run `/do-docs` and confirm no other page asserts a Valkey CI fact that the change
  contradicts.
- Confirm `mkdocs build --strict` passes.
- Do not expand into `docs/fields.md` or `docs/benchmarks.md` — see Rabbit Holes.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: build-claim, build-sdlc-doc, build-workflow, document-feature
- **Assigned To**: claim-validator
- **Agent Type**: validator
- **Parallel**: false
- Run every row in `## Verification`.
- Read the two rewritten paragraphs against the three wording constraints and confirm
  none of them claims result parity.
- Confirm `git diff --stat` touches only `README.md`, `docs/index.md`,
  `docs/sdlc/do-test.md`, `.github/workflows/tests.yml`, and this plan file.
- Report pass/fail per criterion.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| README names the Valkey pin | `grep -c 'valkey/valkey:8-alpine' README.md` | output > 0 |
| README names the Redis pin | `grep -c 'redis:7-alpine' README.md` | output > 0 |
| README links the workflow | `grep -c 'workflows/tests.yml' README.md` | output > 0 |
| Docs index names the pin | `grep -c 'valkey/valkey:8-alpine' docs/index.md` | output > 0 |
| Docs index uses an absolute workflow URL | `grep -c 'https://github.com/tomcounsell/popoto/blob/main/.github/workflows/tests.yml' docs/index.md` | output > 0 |
| Workflow file still exists | `test -f .github/workflows/tests.yml` | exit code 0 |
| Workflow still pins Valkey 8 | `grep -c 'valkey/valkey:8-alpine' .github/workflows/tests.yml` | output > 0 |
| Valkey job still named | `grep -c 'pytest (Valkey)' .github/workflows/tests.yml` | output > 0 |
| Health check prints server_version | `grep -c 'server_version' .github/workflows/tests.yml` | output > 0 |
| "never cancelled" claim is gone | `grep -rn 'never cancelled' .github/workflows/tests.yml` | exit code 1 |
| Stale workflow names gone from docs and scripts | `grep -rn 'test-valkey.yml\|stress-tests.yml' docs/ scripts/ --include='*.md' --include='*.sh' \| grep -v 'docs/plans/'` | exit code 1 |
| ci-local.sh names the right workflow | `grep -c 'tests.yml' scripts/ci-local.sh` | output > 0 |
| ci-local.sh still parses | `bash -n scripts/ci-local.sh` | exit code 0 |
| ci-local.sh untouched by this work | `git diff --name-only main -- scripts/ci-local.sh` | exit code 0 |
| do-test.md ruff clause corrected | `grep -c 'NO ruff' docs/sdlc/do-test.md` | match count == 0 |
| Anti-criterion: no result-parity claim in README | `grep -in 'identical results\|produces the same output\|verified equivalent\|result parity' README.md \| wc -l` | match count == 0 |
| Anti-criterion: no result-parity claim in docs index | `grep -in 'identical results\|produces the same output\|verified equivalent\|result parity' docs/index.md \| wc -l` | match count == 0 |
| Anti-criterion: no over-broad pass-count claim | `grep -in 'same pass count as the Redis job on every run' README.md docs/index.md \| wc -l` | match count == 0 |
| Anti-criterion: no unqualified "full test suite" claim | `grep -in 'full test suite\|full suite' README.md docs/index.md \| wc -l` | match count == 0 |
| Health check reads both version keys | `grep -c 'valkey_version' .github/workflows/tests.yml` | output > 0 |
| Anti-criterion: no differential-run claim | `grep -in 'differential run\|differential comparison' README.md docs/index.md \| wc -l` | match count == 0 |
| Docs build strict | `mkdocs build --strict` | exit code 0 |

Note on the two `exit code 1` rows: `grep` exits 1 when it finds no matches, which is
the passing state for "this stale string is absent". They are positive exact-match rows
per the template's convention, not inverse rows.

Note on the `git diff --name-only main -- scripts/ci-local.sh` row: it exits 0 whether
or not there is a diff, so it is a smoke check that the path resolves. The real assertion
is the validator's `git diff --stat` file-list read in task 5.

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | History & Consistency | Proposed README wording claims the Valkey job "has reported the same pass count as the Redis job on every run", but pass counts were read for exactly one run (33850587942); only 9 of 59 green runs were spot-checked at job level. The sentence meant to fix an over-broad claim is itself over-broad. | Task 1 (build-claim) — narrow the quantifier | Replace the clause with a claim the Evidence table supports verbatim: "with no Valkey-only failure across 60 completed runs". Do NOT say "every run" about pass counts; pass-count identity is single-run evidence. Add a Verification anti-criterion row: `grep -in 'same pass count as the Redis job on every run' README.md docs/index.md \| wc -l` == 0. |
| CONCERN | Risk & Robustness | Risk 1's mitigation says "A pin bump fails the check and forces the README edit into the same PR", but the `## Verification` grep rows are run manually by task 5 inside this PR only. Nothing in `.github/workflows/` runs them, so a future `valkey:9-alpine` bump fails nothing and the strengthened README rots exactly as #405's did. | Risk 1 mitigation text (and optionally a new lint.yml step) | Either (a) reword Risk 1 to state the greps are a one-time build-time check, not a standing gate, and accept the residual risk on the record; or (b) add a step to `.github/workflows/lint.yml` (already PR-gated) running `grep -q 'valkey/valkey:8-alpine' README.md && grep -q 'valkey/valkey:8-alpine' .github/workflows/tests.yml`. Do NOT add it to `tests.yml` — that file is the thing being pinned. |
| CONCERN | Structural + History & Consistency | Wording constraint 3 flags that `tests.yml` runs `-m "not slow"` so "full test suite" is ambiguous — yet the plan's own proposed wording ships the unqualified phrase "the full test suite", and Success Criterion 1 repeats it ("states that the full suite runs against Valkey"). The plan hands the builder a baseline sentence that violates its own constraint. | Technical Approach proposed wording + Success Criterion 1 | Change the baseline sentence to "the test suite (everything except the `slow`-marked stress tests)" and amend Success Criterion 1 to match. The anti-criterion greps do not catch this phrase, so it must be fixed in the plan text rather than left to builder judgement. |
| CONCERN | Structural (Adversary) | Success Criterion 6 requires the health check to print "a real Valkey `server_version`", but the Failure Path section forbids asserting on it and the Verification row only greps the workflow file for the string `server_version`. If `valkey/valkey:8-alpine` publishes its version under `valkey_version` rather than `server_version`, the job prints `server_version: None`, every check still passes, and the sole justification for touching the workflow is silently unmet. | Task 3 (build-workflow) + Success Criterion 6 | Print defensively rather than guessing the key name: `print("server_version:", info.get("server_version") or info.get("valkey_version"))`, keeping the existing `redis_version` print and the `server_name` guard untouched and adding no assertion. Reword Success Criterion 6 to "prints a non-`redis_version` server version field (or `None` if the server publishes none)" so it is checkable from the diff alone. |
| NIT | Scope & Value | Three named agents and five tasks for what Architectural Impact itself calls "four text edits ... plus two `echo`-equivalent lines". | Optional — Team Orchestration | n/a (NIT) |
| NIT | Risk & Robustness | Evidence table rows sum to 92 (59 + 1 + 31 + 1) but the surrounding prose says "all 91 `tests.yml` runs on `main`" and "31 of the 91". | Evidence section arithmetic | n/a (NIT) |
| NIT | History & Consistency | Line-number drift: `docs/sdlc/do-test.md:11` is actually line 10; the "Verify server is Valkey" step is at `tests.yml:137-152`, not `135-152`/`145-152`. | Citation cleanup | n/a (NIT) |
| NIT | Risk & Robustness | `grep -c 'server_version' .github/workflows/tests.yml` passes on any mention of the string, including a comment; and `git diff --name-only main -- scripts/ci-local.sh` always exits 0 (the plan already flags this row as a smoke check). | Verification table | n/a (NIT) |

**Verdict: READY TO BUILD (with concerns)** — 0 blockers, 4 concerns, 4 nits (FULL war room: Risk & Robustness, Scope & Value, History & Consistency; 2026-09-04).

---

## Open Questions

None blocking. Two decisions were made in-plan rather than escalated:

1. **The issue's scope item 3 targets a file that is already fixed.** Resolved by
   redirecting the fix to `docs/sdlc/do-test.md`, where the identical stale text lives,
   and pinning `scripts/ci-local.sh` with a Verification row instead of editing it.
2. **Whether to touch `.github/workflows/tests.yml`.** Resolved yes, minimally. The
   issue's Notes ask that version pins stay falsifiable, and the current health check
   prints only Valkey's Redis-compatibility version (`7.2.4`), which does not corroborate
   a README saying "Valkey 8". Adding one print line is the smallest change that closes
   that gap. The false "never cancelled" comment is corrected in the same pass because
   it is the same class of defect the issue was filed about.
