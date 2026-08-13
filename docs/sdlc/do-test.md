# do-test addendum — this repo only
<!-- Do not duplicate content from the global skill (~/.claude/skills/do-test/SKILL.md). Only include what is unique to this repo. Max 300 lines. -->

## Runner, lint, and gates

```bash
pytest                    # full suite; requires Redis/Valkey on localhost:6379
pytest -k "test_name"     # single test
mypy src/                 # type check
black src/ tests/         # format — this repo has NO ruff (line length 88; isort 79)
mkdocs build --strict     # docs gate
scripts/ci-local.sh       # tests + stress + docs; --all adds build, lock, guard
```

Primary source dir is `src/`; tests live in `tests/`. `scripts/ci-local.sh`
mirrors the GitHub workflows locally (`tests` ← test-valkey.yml, `stress` ←
stress-tests.yml, `docs` ← deploy-docs.yml, `build` ← release.yml, `lock` ←
lock-check.yml, `guard` ← guard-main-push.yml) and is the preferred full run.
Valkey is deliberately not run locally: redis-py talks to Redis and Valkey
identically and the project forbids Redis modules, so local Redis covers the
same ground; GitHub runs the real Valkey job on PR.

## Test isolation: DB 15, and the six expected failures

The suite auto-isolates onto **Redis DB 15** through the `popoto.pytest_plugin`
entry point (both `import popoto` and `import src.popoto` collapse onto one
canonical module and connection). `POPOTO_TEST_DB=<n>` overrides it; DB 0 is
rejected to prevent production data loss.

Because every worktree shares DB 15, concurrent pipelines collide there and
have produced 73–158 phantom failures. Setting `POPOTO_TEST_DB=<n>` avoids the
collision but introduces a fixed, known failure set — **six tests fail by
construction on any non-15 DB.** Five hardcode `assert db == 15`:

- `tests/test_pytest_plugin.py::TestDatabaseIsolation::test_on_test_db`
- `tests/test_pytest_plugin.py::TestDatabaseIsolation::test_swap_happens_before_test_modules_are_imported`
- `tests/test_pytest_plugin.py::TestAsyncIntegration::test_async_connection_on_test_db`
- `tests/test_pytest_plugin.py::TestSrcPopotoImportPaths::test_src_popoto_redis_db_on_test_db`
- `tests/test_pytest_plugin.py::TestSrcPopotoImportPaths::test_canonical_redis_db_on_test_db`

plus `tests/test_version.py::test_version_matches_pyproject`, which fails on a
stale editable install (reinstall, don't file it).

Classify that exact set as **environmental, not PR-introduced**, and say in the
report which DB you ran on. Anything beyond that set on a non-15 DB is a real
signal. Never conclude "regression" without the DB stated.

## Before trusting any count

Four ways a worktree run reports a confident, wrong number (`scripts/ci-local.sh`
checks all four; the fifth is manual). Each cost a review round on PR #495:

1. **Wrong package under test** — if the venv's editable install doesn't
   resolve to this checkout, the suite tests another tree and new-API failures
   look like regressions.
2. **Fresh worktree venv deselects ~95 tests** — `.[dev]` alone omits `numpy`
   and `sentence-transformers`; install `.[dev,embeddings,benchmark]`. Do NOT
   add `dataframe`: pandas breaks `test_dataframe_field.py` collection on 3.x.
   A deselecting suite reports green while running fewer tests.
3. **redis-py 8.x vs `test_pytest_plugin.py::test_isolated_db_subprocess`** —
   fixed in #490 (PR #500); root cause was redis-py 8 injecting pool-internal
   keys (`himport_registry`, `maint_notifications_*`, `orig_*`) into
   `connection_kwargs`, now filtered by `redis_db.sibling_client_kwargs()`.
   Listed so it isn't re-diagnosed as environmental.
4. **Shared DB 15 contention** (above) — to separate contention from a real
   regression, check base out into the *same* worktree and compare.
5. **mypy deltas are redis-py-version-dependent** (not automated) — redis-py
   types every command `Awaitable[T] | T` for both sync and async clients, so
   7.x flags sites 8.x narrows. Measure base-vs-branch in both a 7.x and an 8.x
   environment before reporting a delta.

**Report the environment (Python version, redis-py version, extras installed,
`POPOTO_TEST_DB`) alongside every count.** A bare number is not usable
downstream.
