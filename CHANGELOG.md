# Changelog

All notable changes to Popoto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-02-03

This is the first stable release of Popoto, a Redis/Valkey ORM with Django-like model syntax. This release brings significant new features, performance improvements, and full feature parity with Redis OM Python.

### Added

#### Query System Enhancements

- **Chainable Query Builder** (#91): Fluent interface for building queries incrementally
  ```python
  # New chainable API
  results = Model.query.filter(status="active").order_by("name").limit(10).all()
  results = Model.query.filter(status="active").filter(type="premium").all()

  # Original kwargs API still works
  results = Model.query.filter(status="active", limit=10, order_by="name")
  ```
  - New `QueryBuilder` class with `filter()`, `limit()`, `order_by()`, `values()`, `all()`, `first()`, `count()` methods
  - Full backward compatibility - QueryBuilder behaves like a list

- **Q Objects for OR Queries** (#92): Django-style Q objects for complex query logic
  ```python
  from popoto import Q

  # OR logic
  Model.query.filter(Q(status="active") | Q(type="premium"))

  # AND logic (explicit)
  Model.query.filter(Q(status="active") & Q(rating__gt=4.0))

  # Negation
  Model.query.filter(~Q(status="inactive"))

  # Complex combinations
  Model.query.filter((Q(status="active") | Q(type="premium")) & Q(rating__gt=3.0))
  ```

- **Expression-Based Queries** (#96): Python comparison operators on Field attributes
  ```python
  # Instead of kwargs
  Model.query.filter(Model.rating > 4.0)
  Model.query.filter(Model.name == "Test")

  # Combined expressions
  Model.query.filter((Model.rating > 4.0) & (Model.status == "active"))
  Model.query.filter((Model.cuisine == "Italian") | (Model.cuisine == "Japanese"))

  # Mixed with kwargs
  Model.query.filter(Model.rating > 4.0, status="active", limit=10)
  ```

#### Bulk Operations

- **Bulk Create/Update/Delete** (#93): Efficient batch operations using Redis pipelines
  ```python
  # Bulk create - single pipeline, reduced network round-trips
  Model.bulk_create([obj1, obj2, obj3])

  # Bulk update - update fields on queryset results
  Model.bulk_update(Model.query.filter(status="pending"), status="active")

  # Bulk delete - delete queryset results
  Model.bulk_delete(Model.query.filter(status="inactive"))
  ```
  - All operations support `batch_size` parameter (default: 1000)
  - Async variants: `async_bulk_create()`, `async_bulk_update()`, `async_bulk_delete()`

#### Field Enhancements

- **Sortable ID Strategies** (#95): Time-sortable ID generation for AutoKeyField
  ```python
  class Order(Model):
      id = AutoKeyField(strategy="ulid")   # 26-char time-sortable (requires ulid-py)
      id = AutoKeyField(strategy="ksuid")  # 27-char time-sortable (requires cyksuid)
      id = AutoKeyField()                  # UUID4 hex (default, unchanged)
  ```
  - ULID: Universally Unique Lexicographically Sortable Identifier
  - KSUID: K-Sortable Unique Identifier
  - Optional dependencies: `pip install popoto[ulid]` or `pip install popoto[ksuid]`

#### Async Support

- **Full Async Support** (#89): Complete async/await API for all operations
  - `Model.async_save()`, `Model.async_delete()`, `Model.async_create()`, `Model.async_load()`
  - `Model.query.async_get()`, `Model.query.async_filter()`, `Model.query.async_all()`, `Model.query.async_count()`
  - Uses `asyncio.to_thread()` for Python 3.9+ compatibility

#### Django Compatibility

- **Model.objects Alias** (#94): Django-style query manager alias
  ```python
  # Both work identically
  Model.objects.filter(status="active")
  Model.query.filter(status="active")
  ```

### Changed

- **Performance: SCAN vs KEYS** (#77): Pattern queries now use SCAN instead of KEYS command
  - Prevents blocking Redis on large datasets
  - Incremental cursor-based iteration

- **Performance: Msgpack Deserialization** (#78): 60% faster query times
  - Lazy field deserialization - only decode when accessed
  - Reduced memory allocation during batch loads

- **Performance: Validation Logic** (#72, #73): Optimized field validation
  - Merged duplicate iteration loops in `is_valid()`
  - Reduced overhead during model instantiation and save

- **filter() with No Arguments** (#81): Now correctly returns all objects
  ```python
  Model.query.filter()  # Returns all instances (was returning empty)
  ```

### Fixed

- Exact match queries on SortedField now work correctly
- Field.name attribute properly set for expression-based queries
- Relationship field edge cases with lazy-loaded relationships during `on_delete()`

### Infrastructure

- **Valkey Support** (#55): Full compatibility with Valkey (Redis fork)
  - Works with both `REDIS_URL` and `VALKEY_URL` environment variables
  - Identical API for both Redis and Valkey backends

- **Optional Pandas Dependency** (#63): DataFrame field now optional
  - Core library no longer requires pandas
  - Install with `pip install popoto[dataframe]` for DataFrame support

- **Comprehensive Stress Tests** (#65): Production-grade test suite
  - Bulk operations, concurrent access, memory efficiency
  - Geo queries, relationship integrity, TTL expiration

- **Production Documentation** (#80): Complete documentation overhaul
  - API reference, usage examples, performance tips
  - Available at project documentation site

### Known Risks and Considerations

- **~Q Negation Performance**: Negating Q objects (`~Q(...)`) requires scanning all keys, which may be slow on large datasets. Use with caution in production.

- **Expression Query Metaclass Change**: Fields are now kept as class attributes for expression syntax (`Model.field > value`). Code that inspects model classes may see different behavior.

- **Bulk Operations Memory**: `bulk_update` and `bulk_delete` materialize the full queryset before processing. For very large datasets (100K+ items), consider batching.

### Migration Guide

No breaking changes from previous versions. All new features are additive and maintain full backward compatibility.

#### Recommended Upgrades

1. **Switch to chainable queries** for cleaner code:
   ```python
   # Before
   results = Model.query.filter(status="active", order_by="-created", limit=10)

   # After
   results = Model.query.filter(status="active").order_by("-created").limit(10).all()
   ```

2. **Use Q objects** for OR logic instead of multiple queries:
   ```python
   # Before
   active = set(Model.query.filter(status="active"))
   premium = set(Model.query.filter(type="premium"))
   results = list(active | premium)

   # After
   results = Model.query.filter(Q(status="active") | Q(type="premium"))
   ```

3. **Use bulk operations** for batch processing:
   ```python
   # Before
   for item in items:
       item.save()

   # After
   Model.bulk_create(items)
   ```

---

## [0.x.x] - Previous Releases

See commit history for changes prior to 1.0.0.
