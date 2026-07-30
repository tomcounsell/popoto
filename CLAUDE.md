# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis/Valkey ORM (Object-Relational Mapper) library that provides Django-like model syntax for Redis and Valkey. It supports object persistence, queries, geographic search, time-series data, and pub/sub messaging.

## Setup

### Using uv (recommended)

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
```

### Using pip (fallback)

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Commands

```bash
pytest                          # Run all tests (requires Redis on localhost:6379)
pytest tests/test_queries.py    # Run specific test file
pytest -k "test_name"           # Run single test by name
pytest -p no:popoto             # Run tests without the auto-isolation plugin
mypy src/                       # Type checking
black src/ tests/               # Format code
mkdocs serve                    # Serve docs locally
```

Tests automatically use Redis DB 15 for isolation (via the `popoto.pytest_plugin` entry point). Each test gets a clean DB via `flushdb()`. Override with `POPOTO_TEST_DB=<n>` env var or `popoto_test_db` in `pyproject.toml` `[tool.pytest.ini_options]`. DB 0 is rejected to prevent accidental production data loss.

The isolation guarantee holds for **both** `import popoto` and `import src.popoto` paths. The plugin's `pytest_configure` hook collapses `src.popoto` (and all `popoto.*` submodules) onto the canonical `popoto` objects in `sys.modules` before any test module is imported, so there is only one Redis connection instance and it is always on DB 15. New test files do **not** need a manual `_clean_all()` autouse fixture to be stable — the plugin handles it.

### Local CI (`scripts/ci-local.sh`)

Run the meaningful CI gates locally before pushing, to catch failures without burning GitHub Actions minutes (or waiting on GitHub being up):

```bash
scripts/ci-local.sh              # default: tests + stress + docs
scripts/ci-local.sh --all        # everything, incl. build + guard
scripts/ci-local.sh --fast       # tests only
scripts/ci-local.sh docs build   # only the named gates
```

Gates mirror the workflows: `tests` → `test-valkey.yml` (full suite vs local Redis), `stress` → `stress-tests.yml`, `docs` → `deploy-docs.yml` (`mkdocs build --strict`), `build` → `release.yml` (`uv build`), `guard` → `guard-main-push.yml`. Valkey is intentionally skipped — redis-py treats both identically and the project never uses Redis modules, so the local-Redis suite covers it; GitHub still runs the real Valkey job on PR/merge as the final word. The runner auto-refreshes the editable install if `popoto.__version__` (read from package metadata) drifts from `pyproject.toml`, which otherwise causes a false `test_version` failure locally.

### Verifying in a worktree (read before trusting a test or mypy number)

Work done in a `.worktrees/` checkout can report a confident, wrong number in five
ways. Four are checked automatically in `scripts/ci-local.sh`'s preflight — run it (or
at least its `tests` gate) instead of bare `pytest` when the number will be reported to
anyone. The fifth cannot be automated.

1. **Tests exercise the installed package, not your branch.** The pytest plugin
   collapses `src.popoto` onto canonical `popoto`, so if the venv's editable install
   points elsewhere the suite silently tests *that* tree — failures on new APIs look
   exactly like real regressions. Preflight hard-fails on this.
2. **A fresh worktree venv silently deselects ~95 tests.** `.[dev]` alone omits
   `numpy`/`sentence-transformers`; a green run then covers far less than CI. Install
   `.[dev,embeddings,benchmark]`. (Adding `dataframe` pulls pandas, which on 3.x breaks
   `test_dataframe_field.py` collection.) Preflight warns.
3. **redis-py 8.x fails `test_pytest_plugin.py::test_isolated_db_subprocess`**
   (`maint_notifications_pool_handler`). Environmental, not a regression. Preflight
   predicts it so it isn't chased.
4. **Every worktree shares Redis DB 15.** Concurrent suites from other checkouts have
   produced 73–158 phantom failures. To separate contention from regression, check out
   base into the *same* worktree and compare — don't trust either number alone.
   Preflight warns when other `pytest` processes are running.
5. **The `mypy` error delta is redis-py-version-dependent.** redis-py shares one stub
   set between its sync and asyncio clients, typing every command `Awaitable[T] | T`;
   7.x flags sites 8.x narrows. A delta of `+0` in one environment can be `+4` in
   another, so "verified in one consistent environment" proves nothing about the diff.
   Measure base-vs-branch in both a 7.x and an 8.x environment, and prefer a
   centralized narrowing helper (see `_sync` in `recipes/memory_lifecycle.py`) over
   per-site `type: ignore`.

The general rule behind all five: **state the environment alongside any count**, and
reproduce a subagent's metric before relaying it. Three separate "verified" claims on
PR #495 failed independent review, each for an environmental reason.

## Debugging with Redis/Valkey CLI

Use `redis-cli` (or `valkey-cli` for Valkey) to inspect database state when debugging Popoto models. Both CLIs use identical commands.

```bash
# Connect to Redis or Valkey
redis-cli                       # Connect to localhost:6379 (default)
valkey-cli                      # Valkey equivalent (same commands)
redis-cli -u $REDIS_URL         # Connect using REDIS_URL

# Inspect keys
KEYS *                          # List all keys (dev only, slow on large DBs)
KEYS ClassName:*                # List keys for a specific model
TYPE keyname                    # Check key type (string, hash, set, zset, list)
TTL keyname                     # Check time-to-live (-1 = no expiry, -2 = doesn't exist)

# Read data by type
GET keyname                     # String values (Popoto model data is msgpack-encoded)
HGETALL keyname                 # Hash values
SMEMBERS keyname                # Set members
ZRANGE keyname 0 -1 WITHSCORES  # Sorted set members with scores
LRANGE keyname 0 -1             # List values

# Monitor commands in real-time (very useful for debugging)
MONITOR                         # Watch all Redis commands as they execute

# Cleanup
DEL keyname                     # Delete a specific key
FLUSHDB                         # Clear current database (use with caution)
```

**Popoto-specific patterns:**
- Model instances: `ClassName:key_value` (msgpack-encoded string)
- Sorted indexes: `ClassName:_field_name` (sorted set)
- Unique indexes: `ClassName:field_name:value` (string)
- Geo indexes: `ClassName:_geo_field_name` (geo set)

## Architecture

### Core Components (`src/popoto/`)

**Model System** (`models/`)
- `base.py` - `Model` base class and `ModelBase` metaclass. `ModelOptions` tracks field metadata (key fields, sorted fields, geo fields, relationships).
- `query.py` - `Query` class provides Django-like filtering (`Model.query.filter()`, `Model.query.get()`).
- `db_key.py` - Redis key generation from model class name + KeyField values
- `encoding.py` - Serialization/deserialization using msgpack

**Field System** (`fields/`)
- `field.py` - Base `Field` class with `FieldBase` metaclass
- Mixins in separate files provide specialized behaviors (KeyFieldMixin, AutoFieldMixin, SortedFieldMixin, UniqueFieldMixin)
- `shortcuts.py` - Convenience field types (AutoKeyField, UniqueKeyField, IntField, etc.)
- `relationship.py` - `Relationship` field handles three value types:
  - `Model` instance: fully loaded relationship
  - `str`: redis_key string (lazy-loaded to prevent infinite recursion in circular references)
  - `None`: no relationship set

**Pub/Sub** (`pubsub/`) - Redis pub/sub for real-time messaging

### Key Patterns

- Public model attributes must be Field instances; private attrs use underscore prefix
- Field names must start with lowercase; reserved names: `limit`, `order_by`, `values`
- Models auto-generate an `_auto_key` (AutoKeyField) if no KeyField is defined
- Redis keys follow pattern: `ClassName:key1_value:key2_value:...`
- Fields have `on_save()` and `on_delete()` hooks for maintaining secondary indexes
- Relationship fields support circular references via lazy-loading (field value stored as redis_key string)

### Redis/Valkey Connection

Uses `REDIS_URL` environment variable or defaults to `localhost:6379`. Connection managed in `redis_db.py`. Works with both Redis and Valkey servers.

## Code Style

- Line length: 88 (black), imports: 79 (isort)
- Python 3.10+

## Git Workflow

- Never push code changes directly to main - always create a feature branch and open a PR
- Documentation-only changes (docs/, CLAUDE.md, .claude/commands/) may be pushed directly to main
- Use descriptive branch names like `feature/query-performance` or `fix/scan-keys`
