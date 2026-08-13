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
consensus, cross-vendor judge, verification-table runner, plan-checkbox
updater, cross-repo `gh` targeting. Post under the operator's `gh` credential;
one reviewer, one verdict.

## Verification commands that exist in this repo

```bash
pytest                    # full suite; needs Redis/Valkey on localhost:6379
pytest -k "test_name"     # single test
mypy src/                 # type check
black src/ tests/         # format
mkdocs build --strict     # docs gate (mirrors deploy-docs.yml)
scripts/ci-local.sh       # tests + stress + docs; --all adds build/lock/guard
```

`scripts/ci-local.sh --all` runs the gates mirroring every workflow
(`tests stress docs build lock guard`). `--fast` is tests only. There is no
`ruff` here — formatting is `black` (line length 88, isort at 79).

## Test isolation contract, and the expected-failure set

`pytest` auto-isolates onto **Redis DB 15** via the `popoto.pytest_plugin`
entry point (both `import popoto` and `import src.popoto` collapse onto one
canonical module and connection). Override with `POPOTO_TEST_DB=<n>`; DB 0 is
rejected outright to prevent production data loss.

**Five tests hardcode `assert db == 15` and therefore fail by construction
under any non-15 DB.** This is not a regression — it is the expected result of
running with `POPOTO_TEST_DB` set to anything else (which reviewers running
alongside other pipelines routinely do):

- `tests/test_pytest_plugin.py::TestDatabaseIsolation::test_on_test_db`
- `tests/test_pytest_plugin.py::TestDatabaseIsolation::test_swap_happens_before_test_modules_are_imported`
- `tests/test_pytest_plugin.py::TestAsyncIntegration::test_async_connection_on_test_db`
- `tests/test_pytest_plugin.py::TestSrcPopotoImportPaths::test_src_popoto_redis_db_on_test_db`
- `tests/test_pytest_plugin.py::TestSrcPopotoImportPaths::test_canonical_redis_db_on_test_db`

Also expected: `tests/test_version.py::test_version_matches_pyproject` fails on
a stale editable install (reinstall the package, don't file it as a bug).

Before calling any of these six a blocker, state the DB you ran on. If you ran
on a non-15 DB and see exactly this set, the correct review note is "expected
under `POPOTO_TEST_DB=<n>`", not a finding. Misreading it as a regression has
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
5. **mypy error deltas are redis-py-version-dependent** (not automated).
   redis-py types every command `Awaitable[T] | T` for both sync and async
   clients, so 7.x flags sites 8.x narrows. Measure base-vs-branch in both a
   7.x and an 8.x environment before trusting a delta.

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
- **Format/type gates.** `black src/ tests/` clean and `mypy src/` with no new
  errors (measured per gotcha 5) are gates, not nits.
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
