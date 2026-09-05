# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis/Valkey ORM providing Django-like model syntax: object persistence, queries, geographic search, time-series data, and pub/sub. See `README.md` for install/run/usage.

## Commands

```bash
pytest                          # requires Redis on localhost:6379; isolated on DB 15 via popoto_test_db in pyproject
pytest -k "test_name"           # single test by name
mypy src/                       # type checking
ruff check src/                 # lint (config in [tool.ruff.lint]; gated by lint.yml)
black src/ tests/               # format (`black --check src/ tests/` gated by lint.yml)
mkdocs serve                    # docs locally
scripts/ci-local.sh             # local CI gates: lint + tests + stress + docs (--all, --fast, or named gates)
```

Tests are isolated on Redis DB 15 by the `popoto.pytest_plugin` entry point, which is opt-in: this repo opts in with `popoto_test_db = "15"` under `[tool.pytest.ini_options]`, and a downstream project that never sets it (or `POPOTO_TEST_DB`) gets no DB swap and no flush. Both `import popoto` and `import src.popoto` collapse onto one canonical module/connection. Override with `POPOTO_TEST_DB=<n>`; DB 0 is rejected to prevent accidental production data loss.

**Ad-hoc scripts (outside pytest) default to DB 0, which is a LIVE agent store on this machine.** `POPOTO_TEST_DB` only binds the pytest plugin, and `POPOTO_REDIS_DB` is the Python global client's name, not an environment variable — setting it does nothing. The only env var that binds the connection is `REDIS_URL`, and it is read at import time: run repro scripts with `REDIS_URL=redis://localhost:6379/15` set *before* `import popoto` (see #577 — this mistake has written to the live store twice). Popoto's own client now refuses `FLUSHDB` when bound to database 0 and refuses `FLUSHALL` on any binding, raising `Db0FlushRefusedError` before the command reaches the server; `POPOTO_ALLOW_DB0_FLUSH=1` is the only escape hatch. This guard does not cover `redis-cli`/`valkey-cli` or any other raw client — those remain completely unguarded, so the `REDIS_URL`-before-import discipline above still matters. Copy `scripts/scratch_repro.py` as a safe starting template.

### Verifying in a worktree (read before trusting a test or mypy number)

A `.worktrees/` checkout can report a confident, wrong number in five ways — each cost a review round on PR #495. `scripts/ci-local.sh` checks four automatically:

1. **Wrong package under test**: if the venv's editable install doesn't resolve to this checkout, the suite silently tests another tree — new-API failures look like regressions.
2. **Fresh worktree venv deselects ~95 tests**: `.[dev]` alone omits `numpy`/`sentence-transformers`, and omitting `mcp` skips the MCP server tests. Install `.[dev,embeddings,benchmark,mcp]` (adding `dataframe` pulls pandas, which breaks `test_dataframe_field.py` collection on 3.x).
3. ~~**redis-py 8.x fails `test_pytest_plugin.py::test_isolated_db_subprocess`**~~ — fixed in #490 (PR #500). Root cause was not environmental: redis-py 8 injects pool-internal bookkeeping keys (`himport_registry`, `maint_notifications_*`, `orig_*`) into `connection_kwargs`, which `Redis.__init__` rejects when splatted. `redis_db.sibling_client_kwargs()` now whitelists only standard connection params for DB-0-probe sites.
4. **Every worktree shares Redis DB 15**: concurrent suites from other checkouts have produced 73-158 phantom failures. To isolate contention from regression, check out base into the same worktree and compare.
5. **mypy error delta is redis-py-version-dependent** (not automated): redis-py types every command `Awaitable[T] | T` for both sync/async clients, so 7.x flags sites 8.x narrows. Measure base-vs-branch in both a 7.x and 8.x environment before trusting a delta.

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
