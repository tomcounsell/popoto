# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis/Valkey ORM providing Django-like model syntax: object persistence, queries, geographic search, time-series data, and pub/sub. See `README.md` for install/run/usage.

## Commands

```bash
pytest                          # requires Redis on localhost:6379; isolated on DB 15 via popoto_test_db in pyproject
pytest -k "test_name"           # single test by name
scripts/mypy_ratchet.py         # type checking, as a ratchet against scripts/mypy_baseline.json (gated by lint.yml)
ruff check src/                 # lint (config in [tool.ruff.lint]; gated by lint.yml)
black src/ tests/               # format (`black --check src/ tests/` gated by lint.yml)
mkdocs serve                    # docs locally
scripts/ci-local.sh             # local CI gates: lint + types + tests + stress + docs (--all, --fast, or named gates)
```

`src/` is not mypy-clean and no single PR can make it so, so the type gate is a **ratchet**, not a clean-tree check: `scripts/mypy_ratchet.py` runs `mypy src/` once and fails only when the total rises above the baseline in `scripts/mypy_baseline.json`. A count below baseline passes and prints a GitHub Actions warning naming the command to bank it (`scripts/mypy_ratchet.py --update`, then commit the JSON). Two packages that already measure zero — `integrations/` and `privacy/` — are pinned at exactly zero by that file's `clean` allowlist, so a regression there fails even when the total is flat. Bare `mypy src/` still works for reading errors; it just is not the gate.

The baseline is only meaningful in the environment it was measured in, which the JSON records and CI reproduces. Locally the script prints the mismatch and refuses to compare; `--strict-env` (what lint.yml passes) makes that a failure instead.

Tests are isolated on Redis DB 15 by the `popoto.pytest_plugin` entry point, which is opt-in: this repo opts in with `popoto_test_db = "15"` under `[tool.pytest.ini_options]`, and a downstream project that never sets it (or `POPOTO_TEST_DB`) gets no DB swap and no flush. Both `import popoto` and `import src.popoto` collapse onto one canonical module/connection. Override with `POPOTO_TEST_DB=<n>`; DB 0 is rejected to prevent accidental production data loss.

**Ad-hoc scripts (outside pytest) default to DB 0, which is a LIVE agent store on this machine.** `POPOTO_TEST_DB` only binds the pytest plugin, and `POPOTO_REDIS_DB` is the Python global client's name, not an environment variable — setting it does nothing. The only env var that binds the connection is `REDIS_URL`, and it is read at import time: run repro scripts with `REDIS_URL=redis://localhost:6379/15` set *before* `import popoto` (see #577 — this mistake has written to the live store twice). Popoto's own client now refuses `FLUSHDB` when bound to database 0 and refuses `FLUSHALL` on any binding, raising `Db0FlushRefusedError` before the command reaches the server; `POPOTO_ALLOW_DB0_FLUSH=1` is the only escape hatch. This guard does not cover `redis-cli`/`valkey-cli` or any other raw client — those remain completely unguarded, so the `REDIS_URL`-before-import discipline above still matters. Copy `scripts/scratch_repro.py` as a safe starting template.

`scripts/ci-local.sh` deliberately does **not** export a `REDIS_URL` for its gates (#635). It keeps one internally for its own reachability probe and banner, but passes your shell's environment through untouched, so the pytest plugin stays the only thing binding the test connection. It used to export a db-less `redis://localhost:6379` default, which resolves to DB 0 and broke any test whose contract is that the variable is unset.

`.github/workflows/tests.yml` takes the **opposite** approach on purpose (#639): both jobs export `REDIS_URL: redis://localhost:6379/15`, naming the database rather than omitting the variable. The two are not inconsistent — deleting the variable does not leave it unset, because `DEFAULT_URL` is `redis://localhost:6379/0`, so a from-env bind would resolve to DB 0 and hit the #584 refusal. On a developer machine that is the right trade, since a hardcoded 15 would silently contradict the `POPOTO_TEST_DB` override that parallel worktree lanes rely on. In CI there is no such override (no workflow sets `POPOTO_TEST_DB`), so naming the database is strictly better: a test that binds from the environment lands where the plugin isolates. `tests/test_ci_workflow_redis_url.py` fails if the workflow constant and `popoto_test_db` ever drift apart, in either direction, or if a job drops `REDIS_URL` altogether.

### Verifying in a worktree (read before trusting a test or mypy number)

A `.worktrees/` checkout can report a confident, wrong number in five ways — each cost a review round on PR #495. `scripts/ci-local.sh` checks four automatically:

1. **Wrong package under test**: if the venv's editable install doesn't resolve to this checkout, the suite silently tests another tree — new-API failures look like regressions.
2. **Fresh worktree venv deselects ~95 tests**: `.[dev]` alone omits `numpy`/`sentence-transformers`, and omitting `mcp` skips the MCP server tests. Install `.[dev,embeddings,benchmark,mcp]` (adding `dataframe` pulls pandas, which breaks `test_dataframe_field.py` collection on 3.x).
3. ~~**redis-py 8.x fails `test_pytest_plugin.py::test_isolated_db_subprocess`**~~ — fixed in #490 (PR #500). Root cause was not environmental: redis-py 8 injects pool-internal bookkeeping keys (`himport_registry`, `maint_notifications_*`, `orig_*`) into `connection_kwargs`, which `Redis.__init__` rejects when splatted. `redis_db.sibling_client_kwargs()` now whitelists only standard connection params for DB-0-probe sites.
4. **Every worktree shares Redis DB 15**: concurrent suites from other checkouts have produced 73-158 phantom failures. To isolate contention from regression, check out base into the same worktree and compare.
5. **mypy error delta is redis-py-version-dependent** (now partly automated): redis-py types every command `Awaitable[T] | T` for both sync/async clients, so 7.x flags sites 8.x narrows. Measured on the #506 baseline, the spread is 52 errors — 1120 under `redis==8.1.0` against 1172 under `redis==7.1.1`, same tree and same mypy. Missing optional extras move it too, because `ignore_missing_imports = True` resolves an absent package to `Any`. `scripts/mypy_ratchet.py` now refuses to compare when the running versions do not match the baseline's, which turns this from a silent wrong number into a printed one; it still cannot tell you which environment is the right one, so state yours alongside any count.

Rule: state the environment alongside any count, and reproduce a subagent's metric before relaying it.

## Dependency Updates

`.github/dependabot.yml` schedules weekly grouped version-update PRs for the root `uv` project and for `github-actions`; minor and patch bumps arrive as one PR per ecosystem, majors one at a time. The scheduled lane is `versioning-strategy: lockfile-only`, so `uv.lock` moves and `pyproject.toml`'s lower bounds are never machine-edited — popoto is a published library and a raised floor propagates to every downstream consumer. `examples/` is excluded because its lockfile cannot be regenerated at all; see #611.

## Debugging with Redis/Valkey CLI

`redis-cli`/`valkey-cli` (identical commands) for inspecting state. Popoto key patterns:
- `ClassName:key_value` — model instance (msgpack-encoded)
- `ClassName:_field_name` — sorted index
- `ClassName:field_name:value` — unique index
- `ClassName:_geo_field_name` — geo index

## Key Patterns

- Public model attributes must be `Field` instances; private attrs use underscore prefix
- Field names must start lowercase; reserved: `limit`, `order_by`, `values`
- Models auto-generate `_auto_key` (AutoKeyField) if no KeyField is defined
- `Relationship` fields support circular references via lazy-loading: value is stored as a redis_key string and loaded on access, not eagerly, to avoid infinite recursion
- Numeric constants are magic numbers for experimental tuning, not dev/user config — pin them in-repo (see `popoto.fields.constants.Defaults` docstring), don't expose as constructor kwargs

## Code Style

- Line length: 88 (black), imports: 79 (isort)
- Python 3.10+
- `ruff check src/` must exit 0 (enforced by `.github/workflows/lint.yml`). Selected rules are `E4,E7,E9,F`; formatting rules (E1/E2/E3/E501) and import order are left to black and isort so the tools do not fight. `tests/` is not ruff-gated yet, but formatting is: the same workflow runs `black --check src/ tests/`, so both trees must be black-clean.

## Git Workflow

- Never push directly to main except docs-only changes (`docs/`, `CLAUDE.md`, `.claude/commands/`) — enforced by `guard-main-push.yml`
- Use descriptive branch names like `feature/query-performance` or `fix/scan-keys`
