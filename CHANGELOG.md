# Changelog

All notable changes to Popoto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-11

Popoto 1.0.0 is the first General Availability release. It marks the project's graduation from beta to a stable, production-ready Redis/Valkey ORM with Django-like model syntax. This release consolidates all features and fixes from the beta series (1.0.0b1, 1.0.0b2) plus additional hardening work.

### Highlights

- **Full async/await support** with native `redis.asyncio` — no more `asyncio.to_thread()` wrappers
- **Chainable query builder** with Q objects and expression-based filtering
- **Bulk operations** (create, update, delete) via Redis pipelines
- **Atomic saves** via internal pipeline for data integrity
- **Migration utilities** for production schema changes
- **Comprehensive index integrity** — all known ghost-entry and corruption bugs resolved
- **Valkey compatibility** — works identically with Redis and Valkey

### Added

#### Query System
- **Chainable Query Builder** (#91): Fluent interface for building queries incrementally
  ```python
  results = Model.query.filter(status="active").order_by("name").limit(10).all()
  ```
  `QueryBuilder` supports `filter()`, `limit()`, `order_by()`, `values()`, `all()`, `first()`, `last()`, `count()`

- **Q Objects for OR Queries** (#92): Django-style Q objects for complex query logic
  ```python
  from popoto import Q
  Model.query.filter(Q(status="active") | Q(type="premium"))
  Model.query.filter(~Q(status="inactive"))
  ```

- **Expression-Based Queries** (#96): Python comparison operators on Field attributes
  ```python
  Model.query.filter(Model.rating > 4.0)
  Model.query.filter((Model.rating > 4.0) & (Model.status == "active"))
  ```

- **`__between` range query operator** (#131): Filter SortedField by range
  ```python
  Model.query.filter(score__between=(50, 100))
  ```

- **Plain Field filtering** (#122): Filter on non-indexed fields with client-side fallback

- **`last()` query method** (#137): Retrieve the last result from a query

- **Sorted field ordering preservation** (#139): Queries filtering on SortedField return results in sorted order by default

#### Model Methods
- **`get_or_create()` and `update_or_create()`** (#132): Django-style convenience methods
  ```python
  obj, created = Model.query.get_or_create(name="test", defaults={"score": 100})
  obj, created = Model.query.update_or_create(name="test", defaults={"score": 200})
  ```
  Async variants: `async_get_or_create()`, `async_update_or_create()`

- **`to_dict()` method** (#129): Dictionary serialization with relationship expansion
  ```python
  obj.to_dict()                          # All fields
  obj.to_dict(include=["name", "score"]) # Specific fields
  obj.to_dict(expand=True, max_depth=2)  # Expand relationships
  ```

- **`delete_all()` classmethod** (#115): Delete all instances of a model with index cleanup

- **`Model.pk` property** (#121): Clean primary key access

- **`Model.objects` alias** (#94): Django-style query manager — `Model.objects.filter()` works identically to `Model.query.filter()`

#### Bulk Operations
- **Bulk Create/Update/Delete** (#93): Efficient batch operations using Redis pipelines
  ```python
  Model.bulk_create([obj1, obj2, obj3])
  Model.bulk_update(Model.query.filter(status="pending"), status="active")
  Model.bulk_delete(Model.query.filter(status="inactive"))
  ```
  All support `batch_size` parameter and async variants

#### Migration Utilities
- **`save(skip_auto_now, update_fields)`** (#144): Fine-grained save control for migrations
- **`rebuild_indexes()`** (#146): Rebuild all secondary indexes from stored data
- **`raw_update()`** (#146): Low-level field updates bypassing hooks
- **Comprehensive migration cookbook** (#143): Step-by-step guide for common migration scenarios

#### Field Enhancements
- **Sortable ID Strategies** (#95): ULID and KSUID support for `AutoKeyField`
  ```python
  id = AutoKeyField(strategy="ulid")   # Time-sortable (requires ulid-py)
  id = AutoKeyField(strategy="ksuid")  # Time-sortable (requires cyksuid)
  ```

- **`auto_now_add` and `auto_now` on SortedField** (#133): Automatic timestamps
  ```python
  created_at = SortedField(type=float, auto_now_add=True)
  updated_at = SortedField(type=float, auto_now=True)
  ```

- **Renamed `sort_by` to `partition_by`** (#138): Better reflects the parameter's purpose. Deprecation shim maintains backward compatibility.

#### Async Support
- **Native `redis.asyncio` support** (#130): True async Redis operations — significant performance improvement over the `asyncio.to_thread()` wrapper used in beta 1
- **Full async API**: `async_save()`, `async_delete()`, `async_create()`, `async_load()`, `async_get()`, `async_filter()`, `async_all()`, `async_count()`, `async_get_or_create()`, `async_update_or_create()`, `async_delete_all()`, `async_bulk_create()`, `async_bulk_update()`, `async_bulk_delete()`

#### Developer Experience
- **`get_redis()` helper** (#137): Direct access to the Redis connection
- **`popoto.testing` module** (#137): `use_test_db()` and `flush_test_db()` helpers
- **Popoto Kitchen TUI** (#112): Interactive terminal example app for exploring features

#### Infrastructure
- **Valkey Support** (#55): Full compatibility with Valkey (Redis fork) via `REDIS_URL` or `VALKEY_URL`
- **Optional Pandas** (#63): Core library no longer requires pandas — install with `pip install popoto[dataframe]`
- **Comprehensive stress tests** (#65): Bulk ops, concurrent access, memory efficiency, geo queries, TTL

### Changed

- **Atomic saves** (#148): `save()` now executes via internal Redis pipeline for atomicity
- **SCAN vs KEYS** (#77): Pattern queries use SCAN instead of KEYS to prevent blocking
- **Msgpack deserialization** (#78): 60% faster query times via lazy field deserialization
- **Validation logic** (#72, #73): Optimized field validation with merged iteration loops
- **`filter()` with no arguments** (#81): Now correctly returns all objects
- **Pre-release polish** (#171): Exception hierarchy cleanup, connection hardening, logging improvements, type hints on core CRUD methods

### Fixed

- **KeyField index corruption on value mutation** (#150): `on_save()` now removes instance from old index Set when field value changes
- **SortedField ghost entries on partition key change** (#159): `on_save()` and `on_delete()` clean up old partition's sorted set
- **Obsolete key in class set after key change** (#161): `save()` removes old redis_key from class tracking set
- **Partial save obsolete key cleanup** (#156): `update_fields` saves properly clean up obsolete redis keys
- **Relationship index cleanup on value change** (#155): `Relationship.on_save()` removes old relationship indexes
- **Relationship validation on re-save** (#113): Lazy-loaded `redis_key` strings accepted during validation
- **Exact match queries on SortedField**: Now work correctly
- **Field.name attribute**: Properly set for expression-based queries
- **Relationship on_delete edge cases**: Lazy-loaded relationships handled correctly during deletion

### Known Considerations

- **`~Q` Negation Performance**: Negating Q objects requires scanning all keys — use with caution on large datasets
- **Bulk Operations Memory**: `bulk_update` and `bulk_delete` materialize the full queryset before processing. For 100K+ items, consider batching.

### Migration Guide from 0.x

No breaking changes. All new features are additive with full backward compatibility.

**Recommended upgrades:**

1. Switch to chainable queries: `Model.query.filter(status="active").order_by("-created").limit(10).all()`
2. Use Q objects for OR logic: `Model.query.filter(Q(status="active") | Q(type="premium"))`
3. Use bulk operations for batch processing: `Model.bulk_create(items)`
4. Rename `sort_by` to `partition_by` on SortedField (old name still works via deprecation shim)

---

## [1.0.0b2] - 2026-02-12

Beta 2 release. See [1.0.0] above for consolidated changelog.

## [1.0.0b1] - 2026-02-03

Beta 1 release. See [1.0.0] above for consolidated changelog.

## [0.9.0] - 2025-12-15

See [commit history](https://github.com/tomcounsell/popoto/compare/v0.8.3...v0.9.0) for changes.

## [0.8.3] and earlier

See [commit history](https://github.com/tomcounsell/popoto/commits/v0.8.3) for changes.
