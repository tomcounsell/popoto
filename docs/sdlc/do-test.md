# do-test addendum — this repo only
<!-- Do not duplicate content from the global skill (~/.claude/skills/do-test/SKILL.md). Only include what is unique to this repo. Max 300 lines. -->

## Runner, lint, and gates

```bash
pytest                    # full suite; requires Redis/Valkey on localhost:6379
pytest -k "test_name"     # single test
scripts/mypy_ratchet.py   # type check as a ratchet vs scripts/mypy_baseline.json — gated by lint.yml
ruff check src/           # lint — gated by lint.yml
black src/ tests/         # format (line length 88; isort 79)
mkdocs build --strict     # docs gate
scripts/ci-local.sh       # lint + types + tests + stress + docs; --all adds build, lock, guard
```

Primary source dir is `src/`; tests live in `tests/`. `scripts/ci-local.sh`
mirrors the GitHub workflows locally (`lint` ← lint.yml, `tests` ← tests.yml,
`docs` ← deploy-docs.yml, `build` ← release.yml, `lock` ← lock-check.yml,
`guard` ← guard-main-push.yml) and is the preferred full run. The `lock` gate is
a *partial* mirror: since #669, lock-check.yml also runs `uv sync --locked
--all-extras --no-extra benchmark` and `scripts/check_lock_imports.py`, proving
the lock installs and that the extras no other job installs still import.
`ci-local.sh` deliberately stops at `uv lock --check`, because `uv sync` writes
to your `.venv` and a local gate should not rearrange your environment as a side
effect; reproduce the other two by hand against a scratch
`UV_PROJECT_ENVIRONMENT`. `stress` mirrors
no workflow at all: tests.yml runs `-m "not slow"`, so the stress suite is a
local-only gate. Valkey is deliberately not run locally: redis-py talks to
Redis and Valkey identically and the project forbids Redis modules, so local
Redis covers the same ground; tests.yml runs the real Valkey job on every PR
and every push to `main`. `examples.yml` (the kitchen demo's lockfile check and
headless TUI smoke test) is likewise not mirrored by `ci-local.sh` — it is a
separate uv project on Redis DB 13; run it with
`cd examples && uv sync --extra dev && REDIS_URL=redis://localhost:6379/13 uv run --no-sync pytest tests/`.

## Test isolation: DB 15, and the six expected failures

The suite isolates onto **Redis DB 15** through the `popoto.pytest_plugin`
entry point (both `import popoto` and `import src.popoto` collapse onto one
canonical module and connection). The plugin is opt-in and this repo opts in
with `popoto_test_db = "15"` in `pyproject.toml`; `POPOTO_TEST_DB=<n>`
overrides that, and DB 0 is rejected to prevent production data loss. A
checkout whose `pyproject.toml` lost that line gets no isolation and no
warning — if flush-dependent tests start failing in a worktree, check it
first.

Because every worktree shares DB 15, concurrent pipelines collide there and
have produced 73–158 phantom failures. Setting `POPOTO_TEST_DB=<n>` avoids the
collision. It no longer costs you a known failure set: `tests/test_pytest_plugin.py`
used to hardcode `assert db == 15` in five tests, but #549 parameterised them and
the file passes under an override (verified 2026-09-06: `POPOTO_TEST_DB=6` →
43 passed). The two `assert db == 15` lines that remain (`:832`, `:866`) are
inside child-process probe scripts that pin DB 15 in their own config, so the
parent's override does not reach them.

The one failure still expected outside a fresh install is
`tests/test_version.py::test_version_matches_pyproject`, on a stale editable
install (reinstall, don't file it).

Classify that one as **environmental, not PR-introduced**, and say in the
report which DB you ran on. Anything beyond it on a non-15 DB is a real
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
5. **mypy deltas are redis-py-version-dependent** (now partly automated) —
   redis-py types every command `Awaitable[T] | T` for both sync and async
   clients, so 7.x flags sites 8.x narrows, measured at 52 errors on the #506
   baseline. `scripts/mypy_ratchet.py` refuses to compare when the running
   mypy/redis-py/Python do not match `scripts/mypy_baseline.json`, so state the
   environment and let the script decide whether a delta is even comparable.
   `src/` is not clean and is not expected to be: the gate fails only when the
   total rises ABOVE baseline, plus a hard zero for `integrations/` and
   `privacy/`.

**Report the environment (Python version, redis-py version, extras installed,
`POPOTO_TEST_DB`) alongside every count.** A bare number is not usable
downstream.
