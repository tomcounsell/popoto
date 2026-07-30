# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis/Valkey ORM providing Django-like model syntax: object persistence, queries, geographic search, time-series data, and pub/sub. See `README.md` for install/run/usage.

## Commands

```bash
pytest                          # requires Redis on localhost:6379; auto-isolated on DB 15
pytest -k "test_name"           # single test by name
mypy src/                       # type checking
black src/ tests/               # format
mkdocs serve                    # docs locally
scripts/ci-local.sh             # local CI gates: tests + stress + docs (--all, --fast, or named gates)
```

Tests are auto-isolated on Redis DB 15 via the `popoto.pytest_plugin` entry point (both `import popoto` and `import src.popoto` collapse onto one canonical module/connection). Override with `POPOTO_TEST_DB=<n>`; DB 0 is rejected to prevent accidental production data loss.

### Verifying in a worktree (read before trusting a test or mypy number)

A `.worktrees/` checkout can report a confident, wrong number in five ways — each cost a review round on PR #495. `scripts/ci-local.sh` checks four automatically:

1. **Wrong package under test**: if the venv's editable install doesn't resolve to this checkout, the suite silently tests another tree — new-API failures look like regressions.
2. **Fresh worktree venv deselects ~95 tests**: `.[dev]` alone omits `numpy`/`sentence-transformers`. Install `.[dev,embeddings,benchmark]` (adding `dataframe` pulls pandas, which breaks `test_dataframe_field.py` collection on 3.x).
3. **redis-py 8.x fails `test_pytest_plugin.py::test_isolated_db_subprocess`** (`maint_notifications_pool_handler`) — environmental, not a regression.
4. **Every worktree shares Redis DB 15**: concurrent suites from other checkouts have produced 73-158 phantom failures. To isolate contention from regression, check out base into the same worktree and compare.
5. **mypy error delta is redis-py-version-dependent** (not automated): redis-py types every command `Awaitable[T] | T` for both sync/async clients, so 7.x flags sites 8.x narrows. Measure base-vs-branch in both a 7.x and 8.x environment before trusting a delta.

Rule: state the environment alongside any count, and reproduce a subagent's metric before relaying it.

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

## Git Workflow

- Never push directly to main except docs-only changes (`docs/`, `CLAUDE.md`, `.claude/commands/`) — enforced by `guard-main-push.yml`
- Use descriptive branch names like `feature/query-performance` or `fix/scan-keys`
