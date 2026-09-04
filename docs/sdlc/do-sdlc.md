# do-sdlc addendum — this repo only
<!-- Do not duplicate content from the global skill (~/.claude/skills/do-sdlc/SKILL.md). Only include what is unique to this repo. Max 300 lines. -->

## The REVIEW self-check gate (step 3d.4) is live here

This repo declares the `sdlc-tool` verdict substrate — see
[`do-pr-review.md`](do-pr-review.md). That means step 3d.4's
`sdlc-tool verdict selfcheck --pr N --issue-number M` is a real gate, and
`/do-pr-review` is expected to have called `sdlc-tool verdict finalize`
itself before returning.

If you see the halt:

```json
{"ok": false, "verdict_present": false, "trailer_matches_head": false,
 "marker_completed": false, "reason": "REVIEW_VERDICT_MISSING"}
```

…the review was almost certainly posted to GitHub correctly and only the
recording is missing. Historically this happened because popoto had no
`docs/sdlc/` directory at all, so `/do-pr-review` took its generic no-substrate
path and skipped finalize; five pipelines halted identically this way on
2026-08-13 (#554, #530, #540, #515, #559). This file is the fix. If the halt
recurs, do not paper over it — per the global body, HALT, print the `reason`
plus which of `verdict_present` / `trailer_matches_head` / `marker_completed`
is false, and surface it. Do not re-dispatch REVIEW yourself and do not advance
to DOCS/MERGE.

The underlying design gap — 3d.4 assumes a substrate that the generic
`/do-pr-review` path is entitled to skip — is filed upstream as
`tomcounsell/ai#2767`, together with the integer-count flag ergonomics below.

Note what this file can and cannot guarantee: the substrate declaration is a
prose instruction the reviewing model is asked to honor, not a mechanical
switch. Its existence has not been confirmed end to end — the confirmation is
the first live run that reaches 3d.4 with `ok:true`. Until then, check the
selfcheck result rather than assuming this file settled it.

Recovery, when a human decides to hand-run it, is one atomic call. The count
flags are integers, not prose (`--blockers "3 blockers found"` fails with
`argument --blockers: invalid int value`; `tomcounsell/ai#2767`):

```bash
sdlc-tool verdict finalize --pr 558 --issue-number 554 --run-id <hex> \
  --verdict "CHANGES REQUESTED" --blockers 3 --tech-debt 5
```

If the router stops producing a dispatch after a second REVIEW→PATCH cycle
(falls off the rule table rather than blocking with a named guard), that is the
second half of `tomcounsell/ai#2767` — report it as a stop condition; do not
hand-pick the next stage.

## G8 branch verification is a known upstream defect (`tomcounsell/ai#2765`)

**Popoto's branch naming does not change.** `CLAUDE.md`'s convention —
descriptive names like `feature/query-performance`, `fix/scan-keys` — is the
single rule here, for pipeline runs as much as for hand-authored work. What
follows is a defect to route around, not a convention to bend to.

G8 (artifact verification) never reads the PR's `headRefName`. Traced in
`tools/sdlc_next_skill.py`: the live-verification path derives the slug from
the **plan filename** (`slug = Path(plan_path).stem`), then verifies the BUILD
and PATCH artifacts with `git ls-remote --heads origin session/{slug}` (and
PLAN with `git show main:docs/plans/{slug}.md`). Nothing in that path consults
the branch the PR is actually open from. Filed upstream as
`tomcounsell/ai#2765`; the fix is to make G8 read `headRefName`.

**Expected symptom here:** a real, pushed, CI-green popoto branch under the
documented naming fails G8, because G8 is looking for a `session/{slug}` ref
that has never existed. G8 is a re-dispatch guard, not a blocking one, so it
silently re-dispatches BUILD (or PATCH) until G4's oscillation cap blocks the
run — the visible failure is stage oscillation, several dispatches removed
from the actual cause.

**Sanctioned response — override G8 and advance**, on two conditions, both
verified, not assumed:

1. The real head branch is pushed to origin (`git ls-remote --heads origin
   <headRefName>` returns a ref).
2. CI is green on that **exact head SHA**. Verify per run — resolve the PR's
   `headRefOid` and check the run's `headSha`, e.g. `gh pr view N --json
   headRefOid,statusCheckRollup`. Do not match on check name alone; a green
   check from an earlier SHA proves nothing about the commit under review.

Then **report that the override was exercised** — which PR, which head SHA, and
that G8 was overridden per ai#2765 — in the stage trail and the final report.
These reports are the evidence that accrues to the upstream issue; an
unreported override is indistinguishable from the gate having passed.

**Prohibited:** do NOT push a decoy `session/…` ref to satisfy the gate. That
was done once on 2026-08-13 and left a ref requiring manual cleanup after
merge. Do not commit the plan to `main` as a G8 workaround either — `docs/plans/`
is genuinely pushable under `guard-main-push.yml`, but the docs-only push
exemption exists for docs, not as a gate-satisfaction trick.

If the **first** `sdlc-tool next-skill` of a run returns blocked against a
`run_id` that is your own supervisor's, that is `tomcounsell/ai#2766`
(`session-ensure` not writing the `active_run_id` mirror), not a foreign lock.
Apply the `owner_run_id` self-identity check from the global Step 2 three-way
table: your own run_id means inherit and continue; only a genuinely foreign
`ISSUE_LOCKED` means stop.

## Verification commands the pipeline can rely on

```bash
pytest                    # full suite; requires Redis/Valkey on localhost:6379
mypy src/                 # type check
ruff check src/           # lint (E4,E7,E9,F); gated by lint.yml
black --check src/ tests/ # format (line length 88; isort 79); gated by lint.yml
mkdocs build --strict     # docs gate
scripts/ci-local.sh       # lint + tests + stress + docs (--all, --fast, or named gates)
```

There is no app to launch — popoto is a library
plus an mkdocs site.

## Concurrency: DB 15 is shared across every worktree

Every popoto checkout's suite isolates onto **Redis DB 15** via the
`popoto.pytest_plugin` entry point. Concurrent pipelines therefore collide on
one database and have produced 73–158 phantom failures. When several `/do-sdlc`
runs are live at once (routine here), set `POPOTO_TEST_DB=<n>` per run.

The catch, and it must be passed to every TEST and REVIEW stage: **six tests
fail by construction on a non-15 DB.** Five hardcode `assert db == 15` —
`tests/test_pytest_plugin.py::TestDatabaseIsolation::test_on_test_db`,
`::test_swap_happens_before_test_modules_are_imported`,
`TestAsyncIntegration::test_async_connection_on_test_db`,
`TestSrcPopotoImportPaths::test_src_popoto_redis_db_on_test_db`,
`::test_canonical_redis_db_on_test_db` — and
`tests/test_version.py::test_version_matches_pyproject` fails on a stale
editable install. That exact set is expected noise, not a regression. DB 0 is
rejected outright.

Full reviewer-facing detail, including the five worktree-verification gotchas
(wrong package under test, `.[dev]`-only venvs deselecting ~95 tests, the
redis-py 8.x item fixed in #490/PR #500, DB 15 contention, and
redis-py-version-dependent mypy deltas), lives in
[`do-pr-review.md`](do-pr-review.md). The rule that matters at the supervisor
level: **a stage report's count is not usable unless the stage also states its
environment.** Reproduce a subagent's metric before relaying it.

## Branch and merge constraints

- Never push directly to `main` except docs-only changes (`docs/`, `CLAUDE.md`,
  `.claude/commands/`) — enforced by
  `.github/workflows/guard-main-push.yml`. Everything else merges via PR.
- Branch names are descriptive (`feature/query-performance`, `fix/scan-keys`).
  The slug-owned `session/{slug}` + `.worktrees/{slug}` convention from the
  global body still applies to pipeline builds.

## Not declared here

Popoto uses the generic defaults for: multi-judge consensus, bot review
identity, cross-vendor judging, the verification-table runner, plan-checkbox
writing, and cross-repo `gh` targeting. There is no popoto-local diagnostic
dashboard; `sdlc-tool` state questions go through
`sdlc-tool stage-query --issue-number N`.
