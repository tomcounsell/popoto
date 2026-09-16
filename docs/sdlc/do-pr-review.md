# do-pr-review addendum — this repo only
<!-- Do not duplicate content from the global skill (~/.claude/skills/do-pr-review/SKILL.md). Only include what is unique to this repo. Max 300 lines. -->

## Verdict substrate: `sdlc-tool verdict finalize` — DECLARED, not optional

**This repo declares a verdict-recording substrate.** The global skill's § 5
("Record the Verdict") says that with no substrate declared, the posted GitHub
review IS the verdict and the step is skipped. That is **not** the case here.
Skipping `finalize` in this repo halts `/do-sdlc` at its step 3d.4 gate with:

```json
{"ok": false, "verdict_present": false, "trailer_matches_head": false,
 "marker_completed": false, "reason": "REVIEW_VERDICT_MISSING"}
```

which then needs a human to hand-run `finalize`. That happened to five
concurrent popoto pipelines (#554, #530, #540, #515, #559) in one morning
before this file existed — the review was posted correctly every time; only
the recording was missing.

Run the finalize call **before** emitting the OUTCOME block, on **every** exit
path (APPROVED, CHANGES REQUESTED, `BLOCKED_ON_CONFLICT`, `PR_CLOSED`):

```bash
sdlc-tool verdict finalize --pr "$PR_NUMBER" --issue-number "$ISSUE_NUMBER" \
  --run-id "$RUN_ID" --verdict "APPROVED" --blockers 0 --tech-debt 0
```

### `--blockers` / `--tech-debt` take INTEGER COUNTS, not prose

This is the mistake that costs a retry (filed upstream as
`tomcounsell/ai#2767`). The flags are `int`-typed; passing a description fails
immediately:

```
argument --blockers: invalid int value: "unverified benchmark claim, ..."
```

A real, successful invocation from this repo:

```bash
sdlc-tool verdict finalize --pr 558 --issue-number 554 --run-id <hex> \
  --verdict "CHANGES REQUESTED" --blockers 3 --tech-debt 5
```

The finding *text* lives in the posted GitHub review body. `finalize` records
only counts. `--run-id` is required for this state-mutating subcommand
(missing → `RUN_ID_REQUIRED`); the supervisor supplies it, or run
`sdlc-tool session-ensure --issue-number N` once when invoked standalone.

`finalize` is atomic and self-verifying — it writes the verdict, the
`REVIEW_CONTEXT head_sha=` trailer, and (on APPROVED only) the REVIEW
`completed` marker, then reads all three back. A non-zero exit with a named
reason (`REVIEW_VERDICT_MISSING`, `REVIEW_TRAILER_MISSING`,
`REVIEW_MARKER_INCOMPLETE`) is a **hard stop**: do not emit OUTCOME. No
separate `verdict get` readback is needed. On non-APPROVED verdicts the marker
stays `in_progress` so the router re-runs review after `/do-patch`.

Stage marker at the start of the review, after § 1 resolves the issue number:

```bash
sdlc-tool stage-marker --stage REVIEW --status in_progress \
  --issue-number "$ISSUE_NUMBER" --run-id "$RUN_ID"
```

Not declared here (use the generic defaults): bot review identity, multi-judge
consensus, cross-vendor judge, verification-table runner, cross-repo `gh`
targeting. Post under the operator's `gh` credential; one reviewer, one
verdict.

Plan-checkbox updater: **declared — disabled**, not a generic default. See
"The reviewer has READ access to the branch, and only read access" below.

## The reviewer has READ access to the branch, and only read access (#642)

**`/do-pr-review` must not `git add`, `git commit`, or `git push` in this
repo — on any path, including the plan file.**

**Status as of 2026-09-16: this is the declared contract, not yet the enforced
behavior.** The mechanism that would make it automatic lives outside this
repository and has not landed: `~/.claude/skills/do-pr-review/sub-skills/post-review.md`
§ 2.5 still runs `git commit -m "docs(#N): sync plan checkboxes with review
verdict"` unconditionally (line 221, its "commit-then-post-review ordering —
non-negotiable" rationale intact), and `~/.claude/skills/do-docs/SKILL.md` has
no step that reads a `PLAN_CHECKBOX_SYNC` marker. Until both land, an operator
running `/do-pr-review` in this repo must manually skip the generic
checkbox-commit step, and plan checkboxes must be ticked by hand — checkbox
syncing currently happens in neither place. This is tracked as follow-up work
on #642; see `docs/plans/sdlc-642.md` Tasks A1–A4 (unchecked).

It used to run unconditionally, with no way to opt out. The generic plan-checkbox updater in the global skill's
`sub-skills/post-review.md` § 2.5 ticked `docs/plans/{slug}.md`'s Success
Criteria and pushed a `docs(#N): sync plan checkboxes with review verdict`
commit to the branch under review — authored under the operator's git identity,
so nothing in `git log` distinguished it from a human commit. That commit moved
the branch head past the SHA `sdlc-tool verdict finalize` had just pinned the
verdict to, so every later `selfcheck` returned:

```json
{"ok": false, "verdict_present": true, "approved": true,
 "trailer_matches_head": false, "marker_completed": false,
 "reason": "REVIEW_TRAILER_MISSING"}
```

Both orderings were unsound, which is why the fix is not a reordering: commit
then finalize records a head no reviewer inspected; finalize then commit records
a head that is stale the moment it is written. Observed on #635 / PR #637, where
`_verdicts.REVIEW.head_sha` was recorded as `0229bfcc` — the reviewer's own
commit.

The design is for plan-checkbox syncing to move to **`/do-docs`**, after the
verdict is recorded: the reviewer would publish the intended state as a
one-line `<!-- PLAN_CHECKBOX_SYNC {...} -->` marker in the review body, and
the docs cascade would parse it, apply the ticks, and carry them in its own
commit, with an unmatched criterion left alone. **That `/do-docs` step does
not exist yet** — see the status note above — so treat this paragraph as the
target design, not current behavior.

**Never re-run `finalize` to refresh a stale trailer.** Minting APPROVED
against an uninspected head is self-clearing a review gate. If `selfcheck`
reports `head_drift: "code"`, a source file genuinely changed after approval
and the remedy is another REVIEW pass.

### What still moves the head after REVIEW, and what changes once #642's
### control-plane half lands

`/do-docs` is a mandatory stage that commits *after* REVIEW by design, so the
head always moves before `/do-merge` evaluates the gate.

**The classifier below is not live.** `tools/sdlc_review_drift.py` exists only
on the unmerged `session/sdlc-642` branch of the control-plane repo
(`~/src/ai`, commit `3e0d4b737`); `~/src/ai`'s `main` has no such file. Until
that branch merges, `/do-merge`'s freshness check still compares SHAs for
strict equality, so *any* post-review commit — including a docs-only
`/do-docs` cascade — fails closed as `REVIEW_TRAILER_MISSING` and needs a
fresh REVIEW pass.

Once `tools/sdlc_review_drift.py` lands in the control-plane repo (unmerged as
of 2026-09-16), the gate will classify that drift instead of comparing SHAs
for equality: a range that strictly descends from the reviewed commit and
touches only documentation (`docs/`, top-level `*.md`, `.claude/commands/`)
will be fresh and report `head_drift: "docs_only"`. Everything else — a
changed file under `src/` or `tests/`, `mkdocs.yml`, a force-push, or any
error resolving the comparison — will still fail closed as
`REVIEW_TRAILER_MISSING`. That docs-only path set is one notch more
permissive than `guard-main-push.yml`'s human-ratified set and is proposed,
not yet architect-ratified — see the open questions in
`docs/plans/sdlc-642.md`.

The consequence worth knowing before you plan a cascade: a `/do-docs` pass that
fixes a **docstring inside a source file** produces code drift and will refuse
the merge gate. That is correct — the file was not in the reviewed diff — but it
means docstring corrections are better made during BUILD or PATCH, before the
verdict is pinned, than during the post-review cascade.

## Verification commands that exist in this repo

```bash
pytest                    # full suite; needs Redis/Valkey on localhost:6379
pytest -k "test_name"     # single test
scripts/mypy_ratchet.py   # type check as a ratchet vs scripts/mypy_baseline.json — gated by lint.yml
black src/ tests/         # format
mkdocs build --strict     # docs gate (mirrors deploy-docs.yml)
scripts/ci-local.sh       # lint + types + tests + stress + docs; --all adds build/lock/guard
```

`scripts/ci-local.sh --all` runs the gates mirroring every workflow
(`tests stress docs build lock guard`). `--fast` is tests only. Lint is
`ruff check src/`, gated by `lint.yml`; formatting is `black` (line length 88,
isort at 79).

## Test isolation contract, and the expected-failure set

`pytest` isolates onto **Redis DB 15** via the `popoto.pytest_plugin` entry
point (both `import popoto` and `import src.popoto` collapse onto one
canonical module and connection). The plugin is opt-in; this repo opts in
with `popoto_test_db = "15"` in `pyproject.toml`. Override with
`POPOTO_TEST_DB=<n>`; DB 0 is rejected outright to prevent production data
loss.

**`tests/test_pytest_plugin.py` no longer hardcodes DB 15 in the parent
process** — #549 parameterised it, and the whole file passes under an override
(verified 2026-09-06: `POPOTO_TEST_DB=6` → 43 passed). The two surviving
`assert db == 15` lines (`tests/test_pytest_plugin.py:832`, `:866`) sit inside
child-process probe scripts whose own pytest config pins DB 15, so the parent's
override does not reach them. Do not carry forward the old "five tests fail by
construction on a non-15 DB" caveat; it described the pre-#549 file.

Still expected: `tests/test_version.py::test_version_matches_pyproject` fails on
a stale editable install (reinstall the package, don't file it as a bug).

Before calling that one a blocker, state the DB you ran on. Misreading an
environmental failure as a regression has
already cost real review time.

## Reproducing counts: worktree verification gotchas

Hard Rule 10 (a number the PR claims is a claim, not evidence) has five
concrete failure modes in this repo. `scripts/ci-local.sh` checks the first
four automatically; each one produced a wrong, confident number on PR #495.

1. **Wrong package under test.** If the venv's editable install doesn't resolve
   to *this* checkout, the suite silently tests another tree — new-API failures
   look like regressions.
2. **Fresh worktree venv deselects ~95 tests.** `.[dev]` alone omits `numpy`
   and `sentence-transformers`. Install `.[dev,embeddings,benchmark]`. Do not
   add `dataframe` — it pulls pandas, which breaks `test_dataframe_field.py`
   collection on 3.x. A suite that silently deselects reports green while
   running fewer tests.
3. **redis-py 8.x vs `test_pytest_plugin.py::test_isolated_db_subprocess`** —
   fixed in #490 (PR #500), listed so nobody re-diagnoses it as environmental.
   redis-py 8 injects pool-internal bookkeeping keys (`himport_registry`,
   `maint_notifications_*`, `orig_*`) into `connection_kwargs`, which
   `Redis.__init__` rejects when splatted; `redis_db.sibling_client_kwargs()`
   now whitelists only standard connection params for DB-0-probe sites.
4. **Every worktree shares Redis DB 15.** Concurrent suites from other
   checkouts have produced 73–158 phantom failures. To separate contention from
   a real regression, check base out into the same worktree and compare.
5. **mypy error deltas are redis-py-version-dependent** (now partly automated).
   redis-py types every command `Awaitable[T] | T` for both sync and async
   clients, so 7.x flags sites 8.x narrows — measured at 52 errors on the #506
   baseline (1120 under `redis==8.1.0`, 1172 under `redis==7.1.1`). Missing
   optional extras move it too: `ignore_missing_imports = True` resolves an
   absent package to `Any`. `scripts/mypy_ratchet.py` refuses to compare when
   the running versions do not match `scripts/mypy_baseline.json`, so a
   mismatched delta is now printed rather than silently wrong.

**Rule: state the environment (Python version, redis-py version, extras
installed, `POPOTO_TEST_DB`) alongside any count you put in the review.** A
number without its environment is unverified, and per the mandatory
finding-verification rule it does not support a blocker.

## Repo-specific gates worth checking in the diff

- **Valkey compatibility.** Redis modules (`BF.*`, `CMS.*`, `JSON.*`,
  `FT.*`, …) are forbidden — every feature must work on both Redis and Valkey.
  A module command in the diff is a blocker.
- **Magic numbers stay in-repo.** Numeric constants are experimental tuning
  knobs, not user config; they belong in `popoto.fields.constants.Defaults`
  (see its docstring), not exposed as constructor kwargs.
- **Field/model conventions.** Public model attributes must be `Field`
  instances (private attrs use a leading underscore); field names start
  lowercase; `limit`, `order_by`, and `values` are reserved.
- **Relationship laziness.** `Relationship` values are stored as redis_key
  strings and loaded on access. A change that makes them eager reintroduces
  infinite recursion on circular references.
- **Format/type gates.** `black src/ tests/` clean and
  `scripts/mypy_ratchet.py` exiting 0 are gates, not nits. The type gate is a
  ratchet: a PR may leave `src/` with four figures of errors, but it may not
  ADD one, and `integrations/`/`privacy/` must stay at zero. A PR that lowers
  the count should bank it by committing `--update`'s new
  `scripts/mypy_baseline.json`; CI warns rather than fails when it does not.
- **UI screenshots.** Popoto is a library with a mkdocs site and no app to
  drive — the visual proof gate is a no-op in practice. If a diff genuinely
  touches rendered docs HTML/CSS, the global gate still applies.

## Merge constraint

Never push directly to `main` except docs-only changes (`docs/`, `CLAUDE.md`,
`.claude/commands/`) — enforced by `.github/workflows/guard-main-push.yml`,
which rejects any other path pushed straight to main. Everything else goes
through a PR from a descriptive branch (`feature/query-performance`,
`fix/scan-keys`).

One caveat for **pipeline** PRs: G8's artifact verification checks
`origin/session/{slug}` (slug = the stem of `docs/plans/{slug}.md`), not the
PR's `headRefName` — see [`do-sdlc.md`](do-sdlc.md) and `tomcounsell/ai#2765`.
A pipeline PR on a `feature/…` or `fix/…` branch will fail that check even
though the branch is pushed. That is a router/branch-naming mismatch, not a
code finding — do not raise it as a blocker against the diff.
