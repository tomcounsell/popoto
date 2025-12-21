# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Popoto is a Python Redis ORM (Object-Relational Mapper) library that provides Django-like model syntax for Redis. It supports object persistence, queries, geographic search, time-series data, and pub/sub messaging.

## Commands

```bash
# Install dependencies
poetry install

# Run all tests (requires Redis on localhost:6379)
pytest

# Run specific test file
pytest tests/test_queries.py

# Type checking
mypy src/

# Format code
black src/ tests/

# Serve docs locally
mkdocs serve
```

## Architecture

### Core Components (`src/popoto/`)

**Model System** (`models/`)
- `base.py` - `Model` base class and `ModelBase` metaclass. Models use declarative field definitions. `ModelOptions` tracks field metadata (key fields, sorted fields, geo fields, relationships).
- `query.py` - `Query` class provides Django-like filtering. Each model has `.query` attribute for queries.
- `db_key.py` - Redis key generation from model class name + KeyField values
- `encoding.py` - Serialization/deserialization for Redis storage using msgpack

**Field System** (`fields/`)
- `field.py` - Base `Field` class with `FieldBase` metaclass
- Mixins provide specialized behaviors:
  - `KeyFieldMixin` - Primary key fields (compose the Redis key)
  - `AutoFieldMixin` - Auto-generated UUIDs
  - `SortedFieldMixin` - Enables range queries via Redis sorted sets
  - `UniqueFieldMixin` - Uniqueness constraints
- `geo_field.py` - Geographic coordinates with distance queries
- `relationship.py` - References to other Model instances
- `shortcuts.py` - Convenience field types (IntField, StringField, etc.)

**Pub/Sub** (`pubsub/`)
- `publisher.py` / `subscriber.py` - Redis pub/sub for real-time messaging

**Finance Module** (`finance/`)
- Time-series indicators and models for streaming financial data

### Key Patterns

- All public model attributes must be Field instances (private attrs use underscore prefix)
- Field names must start with lowercase; reserved names: `limit`, `order_by`, `values`
- Models auto-generate an `_auto_key` if no KeyField is defined
- Query results are lazy-loaded Model instances
- Redis keys follow pattern: `ClassName:key1_value:key2_value:...`

### Redis Connection

Uses `REDIS_URL` environment variable or defaults to `localhost:6379`. Connection managed in `redis_db.py`.

## Code Style

- Line length: 88 (black), imports: 79 (isort)
- Python 3.8+ with strict mypy type checking
- All type annotations required (`disallow_untyped_defs = True`)
