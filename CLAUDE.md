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

## Knowledge Base (KB)

This project's knowledge has two sources. Pull from both before answering substantive questions.

**1. Vault (curated docs, iCloud-synced)**
- Location: `~/work-vault/Popoto/`
- Index: see that directory's `README.md` for the file index
- Source of truth for business context, project notes, decisions, and assets

**2. Memory system (Redis, agent-learned observations)**
- Project key: `popoto` (declared in `~/src/ai/config/projects.json`)
- Search: `python -m tools.memory_search search "<query>" --project popoto` (run from `~/src/ai`)
- Save: `python -m tools.memory_search save "<content>" --project popoto` (run from `~/src/ai`)

Curated vault = what humans wrote. Memory = what the agent learned (corrections, decisions, patterns, surprises). Both partition by project — don't leak cross-project context.

Convention reference: `~/src/ai/docs/conventions/knowledge-base-section.md`
