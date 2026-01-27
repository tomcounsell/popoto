# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis ORM (Object-Relational Mapper) library that provides Django-like model syntax for Redis. It supports object persistence, queries, geographic search, time-series data, and pub/sub messaging.

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
mypy src/                       # Type checking
black src/ tests/               # Format code
mkdocs serve                    # Serve docs locally
```

## Debugging with Redis CLI

Use `redis-cli` to inspect Redis state when debugging Popoto models.

```bash
# Connect to Redis
redis-cli                       # Connect to localhost:6379 (default)
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

**Pub/Sub** (`pubsub/`) - Redis pub/sub for real-time messaging

### Key Patterns

- Public model attributes must be Field instances; private attrs use underscore prefix
- Field names must start with lowercase; reserved names: `limit`, `order_by`, `values`
- Models auto-generate an `_auto_key` (AutoKeyField) if no KeyField is defined
- Redis keys follow pattern: `ClassName:key1_value:key2_value:...`
- Fields have `on_save()` and `on_delete()` hooks for maintaining secondary indexes

### Redis Connection

Uses `REDIS_URL` environment variable or defaults to `localhost:6379`. Connection managed in `redis_db.py`.

## Code Style

- Line length: 88 (black), imports: 79 (isort)
- Python 3.8+
