# API Reference

Complete reference for all public classes, methods, and functions in the Popoto Redis ORM.

```python
from popoto import Model, Field, KeyField, AutoKeyField, UniqueKeyField
from popoto import IndexedField, UniqueField
from popoto import SortedField, SortedKeyField, GeoField, DatetimeField, Relationship
from popoto import DecayingSortedField, CyclicDecayField, TemporalPeriod, InteractionWeight, Defaults
from popoto import AccessTrackerMixin, ObservationProtocol, RecallProposal, ConfidenceField, PredictionLedgerMixin
from popoto import RetrievalQuality, ContextAssembler, AdaptiveAssembler
from popoto import ContentField, EmbeddingField
from popoto import Publisher, Subscriber
from popoto import ModelException, KeyMutationError, QueryException, PublisherException, SubscriberException
```

---

## Version introspection

`popoto.__version__` resolves to the installed distribution's version string via `importlib.metadata` (PEP 566). `pyproject.toml` is the single source of truth — the package exposes whatever release-please wrote to `[project].version`. When importing from an uninstalled source tree, `__version__` falls back to the PEP 440-compliant sentinel `"0.0.0+unknown"`.

```python
import popoto
print(popoto.__version__)  # e.g. "1.6.0"
```

No separate `VERSION` file, no static string in `__init__.py` — so there is no risk of version skew between the code on disk and the version reported at runtime.

---

## Model Class

`popoto.Model` is the base class for all Popoto models. Define public attributes as `Field` instances.
Each model is persisted as a Redis hash at the key `ClassName:key1:key2:...`.

See [Making Queries](query.md) for query usage and [Fields](fields.md) for field types.

### Model.\_\_init\_\_(\*\*kwargs)

```python
Model(**kwargs)
```

Create a new in-memory instance with the given field values. Does not persist to Redis. An `AutoKeyField`
named `_auto_key` is added automatically if no `KeyField` is defined on the model.

```python
restaurant = Restaurant(name="Burger Palace", cuisine="American", rating=4.5)
```

!!! note
    Validation runs on instantiation with `null_check=False`. A `ModelException` is raised if type
    constraints fail.

### Model.create()

```python
@classmethod
Model.create(pipeline: redis.client.Pipeline = None, **kwargs) -> Model
```

Create a new instance, save it to Redis, and return it. This is the primary way to persist new objects.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pipeline` | `redis.client.Pipeline` | Optional Redis pipeline for batching operations. |
| `**kwargs` | | Field values for the new instance. |

**Returns:** `Model` instance (or the pipeline if one was provided).

```python
restaurant = Restaurant.create(name="Taco Town", cuisine="Mexican", rating=4.2)
```

### Model.save()

```python
Model.save(
    pipeline: redis.client.Pipeline = None,
    ignore_errors: bool = False,
    skip_auto_now: bool = False,
    update_fields: list = None,
    migrate_key: bool = False,
    **kwargs,
)
```

Persist the instance to Redis using `HSET`. Also triggers all field `on_save` hooks (sorted-set indexes,
geo indexes, relationship sets, unique constraints, etc.).

If a `KeyField` value has changed since the instance was loaded, `save()` automatically handles the key
migration: it deletes the old Redis hash, removes the old key from the class set, migrates all field
indexes to the new key, and adds the new key to the class set. This applies to both full saves and
partial saves via `update_fields`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |
| `ignore_errors` | `bool` | If `True`, log validation errors instead of raising `ModelException`. |
| `skip_auto_now` | `bool` | If `True`, suppress `auto_now` timestamp updates. Useful during migrations. |
| `update_fields` | `list` | Optional list of field names for partial save. Only the listed fields are written to Redis and only their `on_save` hooks fire. An empty list is a no-op. `auto_now` fields are excluded unless explicitly listed. |
| `migrate_key` | `bool` | If `True`, allow KeyField value changes (key migration). By default, changing a KeyField value after initial save raises `KeyMutationError`. |

**Returns:** Redis `HSET` response (int) or pipeline.

**Raises:** `KeyMutationError` if a KeyField value has changed and `migrate_key` is not `True`.

```python
restaurant = Restaurant(name="Sushi Spot", cuisine="Japanese", rating=4.8)
restaurant.save()

# Partial save -- only update the rating field
restaurant.rating = 4.9
restaurant.save(update_fields=["rating"])
```

### Model.delete()

```python
Model.delete(pipeline: redis.client.Pipeline = None, **kwargs)
```

Delete the instance from Redis. Removes the hash key, removes the instance from the class set, triggers
all field `on_delete` hooks, and cleans up indexes.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Returns:** `bool` indicating whether the object existed and was deleted (or pipeline if one was provided).

```python
restaurant.delete()
```

### Model.atomic_increment()

```python
Model.atomic_increment(field_name: str, delta, pipeline: redis.client.Pipeline = None)
```

Atomically increment a numeric field value in Redis using a Lua script. This prevents lost
updates from concurrent read-modify-write cycles. The in-memory instance is updated to
reflect the new value after the operation.

| Parameter | Type | Description |
|-----------|------|-------------|
| `field_name` | `str` | Name of the field to increment. Must be a numeric field (`int`, `float`, or `Decimal`). |
| `delta` | numeric | The amount to add. Use negative values to decrement. Must not be `None`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. When provided, operations are queued but not executed. |

**Returns:** The new field value after incrementing, matching the field's type.

**Raises:**

- `TypeError` if the model has not been saved, the field is not numeric, or `delta` is `None`.
- `AttributeError` if `field_name` does not exist on the model.

```python
# Increment an integer field
restaurant = Restaurant.query.get(name="Burger Palace")
new_count = restaurant.atomic_increment("order_count", 1)
print(new_count)
# => 43

# Decrement a field
new_count = restaurant.atomic_increment("order_count", -5)

# Float fields work too
new_rating = restaurant.atomic_increment("score", 0.5)
```

!!! tip
    `atomic_increment()` is safe for concurrent access. Multiple processes can increment
    the same field simultaneously without lost updates, unlike the read-modify-write
    pattern of loading, changing, and saving.

!!! note
    If the field is a `SortedField`, the sorted set index score is also updated
    atomically via `ZINCRBY`, keeping the index in sync with the field value.

### Model.touch()

```python
Model.touch(field_name: str, pipeline: redis.client.Pipeline = None)
```

Update a `DecayingSortedField` or `CyclicDecayField` timestamp without a full save. Refreshes
the decay clock by setting the sorted set score to the current time.

| Parameter | Type | Description |
|-----------|------|-------------|
| `field_name` | `str` | Name of a `DecayingSortedField` or `CyclicDecayField` on the model. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Returns:** The new timestamp (`float`), or the pipeline if one was provided.

**Raises:**

- `TypeError` if the model has not been saved or the field is not a `DecayingSortedField` (or subclass).
- `AttributeError` if `field_name` does not exist on the model.

```python
memory = Memory.query.get(agent_id="agent-1")
new_ts = memory.touch("relevance")
# The decay clock is reset — this memory will rank higher in top_by_decay()
```

### Model.resolve\_pressure()

```python
Model.resolve_pressure(field_name: str, pipeline: redis.client.Pipeline = None)
```

Reset homeostatic pressure for a `CyclicDecayField` member. Discharges accumulated urgency
by updating `last_resolved` to the current time in the pressure companion hash.

| Parameter | Type | Description |
|-----------|------|-------------|
| `field_name` | `str` | Name of a `CyclicDecayField` with `pressure_rate > 0`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Returns:** The new `last_resolved` timestamp (`float`), or the pipeline if one was provided.

**Raises:**

- `TypeError` if the model has not been saved, the field is not a `CyclicDecayField`, or `pressure_rate` is 0.
- `AttributeError` if `field_name` does not exist on the model.

```python
directive = Directive.query.get(agent_id="agent-1")
directive.resolve_pressure("relevance")
# Pressure resets — the record's urgency score drops to 0
```

### Model.strengthen\_cycle()

```python
Model.strengthen_cycle(field_name: str, factor: float = 1.2, pipeline: redis.client.Pipeline = None)
```

Multiply all cycle amplitudes of a `CyclicDecayField` by a factor (>1.0 strengthens).
Amplitudes are clamped to `[0.0, 100.0]`. Values below `0.01` snap to zero.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `field_name` | `str` | | Name of a `CyclicDecayField` on the model. |
| `factor` | `float` | `1.2` | Multiplier for amplitudes. |
| `pipeline` | `redis.client.Pipeline` | `None` | Optional pipeline for batching. |

**Returns:** The updated cycles list, or the pipeline if one was provided.

**Raises:**

- `TypeError` if the model has not been saved or the field is not a `CyclicDecayField`.
- `AttributeError` if `field_name` does not exist on the model.

```python
memory.strengthen_cycle("relevance", factor=1.5)
```

### Model.weaken\_cycle()

```python
Model.weaken_cycle(field_name: str, factor: float = 0.8, pipeline: redis.client.Pipeline = None)
```

Multiply all cycle amplitudes of a `CyclicDecayField` by a factor (<1.0 weakens).
Same mechanics as `strengthen_cycle` with a different default factor.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `field_name` | `str` | | Name of a `CyclicDecayField` on the model. |
| `factor` | `float` | `0.8` | Multiplier for amplitudes. |
| `pipeline` | `redis.client.Pipeline` | `None` | Optional pipeline for batching. |

**Returns:** The updated cycles list, or the pipeline if one was provided.

**Raises:**

- `TypeError` if the model has not been saved or the field is not a `CyclicDecayField`.
- `AttributeError` if `field_name` does not exist on the model.

```python
memory.weaken_cycle("relevance", factor=0.5)
```

### Model.load()

```python
@classmethod
Model.load(db_key: str = None, **kwargs) -> Model
```

Load an existing instance from Redis by `db_key` or by field values that construct the key.

| Parameter | Type | Description |
|-----------|------|-------------|
| `db_key` | `str` | The full Redis key string. |
| `**kwargs` | | Key field values to construct the key. |

**Returns:** `Model` instance or `None` if not found.

```python
restaurant = Restaurant.load(name="Taco Town")
```

### Model.is_valid()

```python
Model.is_valid(null_check: bool = True) -> bool
```

Validate the instance's field values against type, null, and max_length constraints.

| Parameter | Type | Description |
|-----------|------|-------------|
| `null_check` | `bool` | When `False`, skip null validation (used during init). |

**Returns:** `True` if all validations pass.

### Model.get_info()

```python
@classmethod
Model.get_info() -> dict
```

Return metadata about the model: its name, field names, and available query filter parameters.

**Returns:** `dict` with keys `"name"`, `"fields"`, and `"query_filters"`.

```python
Restaurant.get_info()
# => {"name": "Restaurant", "fields": ["name", "cuisine", ...], "query_filters": [...]}
```

### Model.db_key

```python
@property
Model.db_key -> DB_key
```

The computed Redis key for this instance, based on the class name and sorted key field values.

### Async Methods

Every synchronous Model method has an async counterpart that runs in a thread pool. See
[Async Operations](async.md) for details.

| Sync | Async |
|------|-------|
| `Model.create(**kwargs)` | `await Model.async_create(**kwargs)` |
| `instance.save()` | `await instance.async_save()` |
| `instance.delete()` | `await instance.async_delete()` |
| `Model.load(db_key=...)` | `await Model.async_load(db_key=...)` |
| `Model.bulk_create(...)` | `await Model.async_bulk_create(...)` |
| `Model.bulk_update(...)` | `await Model.async_bulk_update(...)` |
| `Model.bulk_delete(...)` | `await Model.async_bulk_delete(...)` |

All async methods accept the same parameters as their sync counterparts.

---

## Bulk Operations

Popoto provides bulk operation methods for efficient batch processing using Redis pipelines.
These methods significantly reduce network round-trips compared to individual operations,
making them ideal for importing data, batch updates, and cleanup tasks.

### Model.bulk_create()

```python
@classmethod
Model.bulk_create(instances: list, batch_size: int = 1000) -> list
```

Create multiple instances efficiently using a Redis pipeline. All instances are saved in
batched transactions, dramatically reducing network overhead.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `instances` | `list` | | List of unsaved model instances to create. |
| `batch_size` | `int` | `1000` | Maximum instances per pipeline batch. |

**Returns:** `list` of created instances.

```python
# Create many restaurants at once
restaurants = [
    Restaurant(name="Taco Town", cuisine="Mexican", rating=4.2),
    Restaurant(name="Burger Palace", cuisine="American", rating=4.0),
    Restaurant(name="Sushi Spot", cuisine="Japanese", rating=4.8),
]
created = Restaurant.bulk_create(restaurants)
print(f"Created {len(created)} restaurants")
# => Created 3 restaurants
```

!!! tip "Performance Benefit"
    Creating 1000 instances with individual `save()` calls requires 1000 network
    round-trips. With `bulk_create()`, the same operation completes in a single
    pipeline execution, often 10-100x faster depending on network latency.

!!! note
    All instances must be of the same Model class. Validation runs on each instance
    during save, and a `ModelException` is raised if any instance fails validation.

### Model.bulk_update()

```python
@classmethod
Model.bulk_update(queryset_or_instances, batch_size: int = 1000, **updates) -> int
```

Update multiple instances efficiently using a Redis pipeline. Applies the given field
updates to all instances in the queryset or list.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `queryset_or_instances` | `list` or query result | | Instances to update (from `query.filter()` or a list). |
| `batch_size` | `int` | `1000` | Maximum instances per pipeline batch. |
| `**updates` | | | Field names and new values to apply. |

**Returns:** `int` count of updated instances.

```python
# Update all pending restaurants to active
count = Restaurant.bulk_update(
    Restaurant.query.filter(status="pending"),
    status="active"
)
print(f"Activated {count} restaurants")

# Update from a list of instances
featured_restaurants = [r1, r2, r3]
count = Restaurant.bulk_update(featured_restaurants, is_featured=True, rating=5.0)
```

!!! note
    Each instance is fully validated before saving. If any instance fails validation,
    a `ModelException` is raised.

### Model.bulk_delete()

```python
@classmethod
Model.bulk_delete(queryset_or_instances, batch_size: int = 1000) -> int
```

Delete multiple instances efficiently using a Redis pipeline. Properly cleans up all
associated indexes (sorted fields, geo fields, unique constraints, relationships).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `queryset_or_instances` | `list` or query result | | Instances to delete (from `query.filter()` or a list). |
| `batch_size` | `int` | `1000` | Maximum instances per pipeline batch. |

**Returns:** `int` count of deleted instances.

```python
# Delete all inactive restaurants
count = Restaurant.bulk_delete(
    Restaurant.query.filter(status="inactive")
)
print(f"Deleted {count} inactive restaurants")

# Delete from a list
old_restaurants = [r1, r2, r3]
count = Restaurant.bulk_delete(old_restaurants)
```

!!! warning
    Bulk delete is permanent. All instances and their indexes are removed from Redis.
    There is no undo operation.

### Model.delete_all()

```python
@classmethod
Model.delete_all(batch_size: int = 1000) -> int
```

Delete **all instances** of this model, including all secondary indexes. This is a convenience
wrapper around `bulk_delete()` that handles the full cleanup automatically.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_size` | `int` | `1000` | Maximum instances per pipeline batch. |

**Returns:** `int` count of deleted instances.

```python
# Delete all restaurants
deleted = Restaurant.delete_all()
print(f"Deleted {deleted} restaurants")

# Delete all models (delete referencing models first)
for model in [Order, MenuItem, Restaurant]:
    model.delete_all()
```

!!! warning "Why use delete_all() instead of Redis DEL/FLUSHDB?"
    **Never delete Popoto data directly with Redis commands like `DEL`, `FLUSHDB`, or `KEYS ... | xargs redis-cli DEL`.**

    Popoto maintains secondary indexes for fast queries:

    - **SortedField** → Redis sorted sets for range queries
    - **GeoField** → Redis geo sets for location queries
    - **UniqueKeyField** → Redis keys for uniqueness constraints
    - **Class sets** → Track all instances of each model

    If you delete instance keys directly, these indexes become orphaned:

    - Range queries return stale results
    - Geo queries find deleted locations
    - Unique constraints block valid values
    - `count()` returns wrong numbers

    `delete_all()` properly invokes each instance's `delete()` method, which triggers all
    field `on_delete` hooks to clean up indexes. This is the **only safe way** to bulk-delete
    Popoto data.

    ```python
    # ✅ CORRECT - cleans up all indexes
    Restaurant.delete_all()

    # ❌ WRONG - leaves orphaned indexes
    redis_client.delete(*redis_client.keys("Restaurant:*"))
    ```

### Batch Size Parameter

All bulk methods accept a `batch_size` parameter (default 1000) that controls memory
usage and pipeline size. When processing more instances than `batch_size`, operations
are automatically split into multiple pipeline executions.

```python
# Process 10,000 instances in batches of 500
Restaurant.bulk_create(large_list, batch_size=500)
```

**When to adjust batch size:**

- **Increase** for faster throughput when memory is not a concern
- **Decrease** when instances are large or memory is constrained
- **Default (1000)** works well for most use cases

### Async Bulk Methods

All bulk operations have async counterparts that run in a thread pool to avoid blocking
the event loop. See [Async Operations](async.md) for details.

| Sync | Async |
|------|-------|
| `Model.bulk_create(instances)` | `await Model.async_bulk_create(instances)` |
| `Model.bulk_update(queryset, **updates)` | `await Model.async_bulk_update(queryset, **updates)` |
| `Model.bulk_delete(queryset)` | `await Model.async_bulk_delete(queryset)` |
| `Model.delete_all()` | `await Model.async_delete_all()` |

```python
# Async bulk create
restaurants = await Restaurant.async_bulk_create([
    Restaurant(name="Async Eats", cuisine="Fusion", rating=4.5),
    Restaurant(name="Pipeline Pizzeria", cuisine="Italian", rating=4.3),
])

# Async bulk update
count = await Restaurant.async_bulk_update(
    Restaurant.query.filter(rating__gte=4.0),
    is_featured=True
)

# Async bulk delete
count = await Restaurant.async_bulk_delete(
    Restaurant.query.filter(status="closed")
)
```

### Example Use Cases

**Data Import**

```python
# Import restaurants from CSV
import csv

with open("restaurants.csv") as f:
    reader = csv.DictReader(f)
    instances = [
        Restaurant(
            name=row["name"],
            cuisine=row["cuisine"],
            rating=float(row["rating"]),
        )
        for row in reader
    ]

created = Restaurant.bulk_create(instances)
print(f"Imported {len(created)} restaurants")
```

**Batch Status Update**

```python
# Mark all orders older than 30 days as archived
from datetime import datetime, timedelta

cutoff = datetime.now() - timedelta(days=30)
old_orders = Order.query.filter(created_at__lt=cutoff)
count = Order.bulk_update(old_orders, status="archived")
print(f"Archived {count} old orders")
```

**Cleanup Task**

```python
# Remove all soft-deleted records
deleted_count = Restaurant.bulk_delete(
    Restaurant.query.filter(is_deleted=True)
)
print(f"Permanently removed {deleted_count} restaurants")
```

---

## Index Maintenance

Popoto maintains secondary indexes (sorted sets, key field sets, geo indexes, composite
indexes, and the class set) alongside your model data. Over time, indexes can accumulate
orphaned entries -- references to instance keys that no longer exist in Redis. This
typically happens after direct Redis deletions, TTL expirations, or interrupted operations.

### Model.check_indexes()

```python
@classmethod
Model.check_indexes(batch_size: int = 1000) -> dict
```

Read-only health check that counts orphaned index entries. Scans all five index types
and checks whether each referenced instance key still exists in Redis.

This method makes **zero writes** to Redis. It is safe to call in production at any time.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_size` | `int` | 1000 | Number of EXISTS commands per pipeline batch. Lower values use less memory but require more round-trips. |

**Returns:** Dict with orphan counts per index type:

```python
{
    'class_set': int,
    'key_fields': {field_name: int, ...},
    'sorted_fields': {field_name: int, ...},
    'geo_fields': {field_name: int, ...},
    'composite_indexes': {index_key: int, ...},
    'total': int,
}
```

```python
# Check for orphaned index entries
result = User.check_indexes()
if result['total'] > 0:
    print(f"Found {result['total']} orphaned index entries")
    # Surgically remove orphans (production-safe)
    User.clean_indexes()
    # Or fully rebuild indexes (destructive)
    # User.rebuild_indexes()

# Check with smaller batches for memory-constrained environments
result = User.check_indexes(batch_size=100)
```

!!! tip
    Use `check_indexes()` as a diagnostic step before deciding whether to run the
    destructive `rebuild_indexes()`. This is especially useful in production where
    you want to assess index health without modifying any data.

### Model.rebuild_indexes()

```python
@classmethod
Model.rebuild_indexes(batch_size: int = 1000) -> int
```

Delete all secondary indexes and reconstruct them from source hash data. This is useful
for repairing corrupted indexes, after bulk data imports that bypassed normal `save()`
hooks, or when upgrading field types that change index structure.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_size` | `int` | 1000 | Number of instances to process per pipeline batch. |

**Returns:** Number of instances processed.

```python
# Rebuild all indexes for User model
count = User.rebuild_indexes()
print(f"Rebuilt indexes for {count} users")
```

!!! warning
    `rebuild_indexes()` is a destructive operation that deletes and recreates all
    secondary indexes. During the rebuild window, queries relying on those indexes
    may return incomplete results.

### Model.clean_indexes()

```python
@classmethod
Model.clean_indexes(batch_size: int = 1000) -> int
```

Production-safe orphan cleanup that surgically removes orphaned index entries without
rebuilding from scratch. Scans all five index types using SCAN-based iteration and
removes only the entries whose referenced instance keys no longer exist in Redis.

Unlike `rebuild_indexes()`, this method does not delete and recreate indexes. It
performs targeted removals (SREM, ZREM, HDEL) only on orphaned entries, leaving
valid entries untouched. This makes it safe to run during normal operations with
minimal impact on concurrent queries.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_size` | `int` | 1000 | Number of EXISTS commands per pipeline batch. Lower values use less memory but require more round-trips. |

**Returns:** Integer count of orphaned entries removed across all index types.

```python
# Remove orphaned index entries
removed = User.clean_indexes()
print(f"Removed {removed} orphaned index entries")

# Typical workflow: check first, then clean
result = User.check_indexes()
if result['total'] > 0:
    removed = User.clean_indexes()
    print(f"Cleaned {removed} orphans")

# Verify cleanup was complete
result = User.check_indexes()
assert result['total'] == 0
```

!!! tip
    Use `check_indexes()` to diagnose index health, `clean_indexes()` to surgically
    remove orphans, and `rebuild_indexes()` as a last resort for full reconstruction.
    In most production scenarios, `clean_indexes()` is the right choice because it
    preserves valid index entries and avoids the query gap that `rebuild_indexes()`
    creates.

!!! example "Live demo"
    The [Popoto Kitchen example app](../examples/README.md) demonstrates the
    `check_indexes()` then `clean_indexes()` workflow across all models. Run
    `python -m popoto_kitchen --ops` to see it in action. Source:
    [`examples/popoto_kitchen/operations.py`](../examples/popoto_kitchen/operations.py).

### Async Index Maintenance

All three index maintenance methods have async counterparts that use `asyncio.to_thread`
under the hood, keeping the event loop free during potentially long-running scans.

| Sync | Async |
|------|-------|
| `Model.check_indexes()` | `await Model.async_check_indexes()` |
| `Model.clean_indexes()` | `await Model.async_clean_indexes()` |
| `Model.rebuild_indexes()` | `await Model.async_rebuild_indexes()` |

#### async_check_indexes()

Read-only health check. Returns the same dict structure as the synchronous version.

```python
async def audit_index_health():
    result = await User.async_check_indexes()
    if result['total'] > 0:
        print(f"Found {result['total']} orphaned entries")
        for field, count in result.get('sorted_fields', {}).items():
            if count > 0:
                print(f"  {field}: {count} orphans")
    else:
        print("All indexes healthy")
```

#### async_clean_indexes()

Production-safe orphan cleanup. Returns the number of entries removed.

```python
async def scheduled_cleanup():
    result = await User.async_check_indexes()
    if result['total'] > 0:
        removed = await User.async_clean_indexes()
        print(f"Cleaned {removed} orphaned index entries")

        # Verify cleanup was complete
        after = await User.async_check_indexes()
        assert after['total'] == 0
```

#### async_rebuild_indexes()

Full destructive rebuild as a last resort. Returns the number of instances processed.

```python
async def emergency_rebuild():
    count = await User.async_rebuild_indexes()
    print(f"Rebuilt indexes for {count} users")
```

These async methods work well with `asyncio.gather()` when maintaining indexes across
multiple models:

```python
async def maintain_all_indexes():
    """Check and clean indexes for all models concurrently."""
    results = await asyncio.gather(
        User.async_check_indexes(),
        Restaurant.async_check_indexes(),
        Order.async_check_indexes(),
    )

    for model_name, result in zip(["User", "Restaurant", "Order"], results):
        if result['total'] > 0:
            print(f"{model_name}: {result['total']} orphans found, cleaning...")

    # Clean only models with orphans
    if results[0]['total'] > 0:
        await User.async_clean_indexes()
    if results[1]['total'] > 0:
        await Restaurant.async_clean_indexes()
    if results[2]['total'] > 0:
        await Order.async_clean_indexes()
```

!!! warning
    `async_rebuild_indexes()` is destructive -- during the rebuild window, queries relying
    on those indexes may return incomplete results. Prefer `async_clean_indexes()` for
    routine maintenance.

See [Async Operations](async.md) for the full async API reference.

---

### Meta Inner Class

Configure model-level behavior by defining a `Meta` inner class. See [Meta Options](meta.md) for
full documentation.

| Option | Type | Description |
|--------|------|-------------|
| `order_by` | `str` | Default ordering field. Prefix with `-` for descending. |
| `ttl` | `int` | Default time-to-live in seconds for all instances. |
| `indexes` | `tuple` | Composite indexes as `((field_names,), is_unique)` tuples. |
| `abstract` | `bool` | If `True`, the model cannot be instantiated directly. |

```python
class Order(Model):
    order_id = AutoKeyField()
    total = SortedField(type=float)
    created_at = DatetimeField(auto_now_add=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days
```

### Instance TTL Attributes

Every model instance exposes two attributes for controlling expiration. These
are set per-instance before calling `save()`. See [TTL](ttl.md) for full
documentation and examples.

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_ttl` | `int` or `None` | Value of `Meta.ttl` | Time-to-live in seconds. Set to `None` to make the instance permanent. Takes precedence over `Meta.ttl`. |
| `_expire_at` | `datetime` or `None` | `None` | Absolute expiration timestamp. Calls Redis `EXPIREAT` on save. |

!!! warning
    Setting both `_ttl` and `_expire_at` on the same instance raises a
    `ModelException` during validation. Use one or the other.

```python
from datetime import datetime

# Override model TTL for one instance
order = Order(order_id="rush-123", total=49.99)
order._ttl = 604800  # 7 days instead of the default 30
order.save()

# Set absolute expiration
order._ttl = None
order._expire_at = datetime(2026, 12, 31, 23, 59, 59)
order.save()
```

---

## Query Class

`popoto.models.query.Query` is attached to every model as `Model.query` (also aliased as `Model.objects`).
It provides the interface for retrieving and filtering stored instances.

See [Making Queries](query.md) for usage patterns.

### Query.get()

```python
Query.get(db_key: DB_key = None, redis_key: str = None, **kwargs) -> Model
```

Retrieve a single model instance. Look up by `db_key`, `redis_key`, or keyword field values. Raises
`QueryException` if more than one match is found.

| Parameter | Type | Description |
|-----------|------|-------------|
| `db_key` | `DB_key` | A `DB_key` object to look up. |
| `redis_key` | `str` | A raw Redis key string. |
| `**kwargs` | | Field values for lookup. |

**Returns:** `Model` instance or `None`.

```python
restaurant = Restaurant.query.get(name="Taco Town")
```

### Query.get\_many()

```python
Query.get_many(redis_keys: list[str], skip_none: bool = False) -> list
```

Retrieve multiple model instances by their Redis keys in a single pipelined round-trip.
The returned list preserves the order of `redis_keys`; positions where no Redis hash exists
contain `None` (or are omitted when `skip_none=True`).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `redis_keys` | `list[str]` | | List of Redis key strings to look up. |
| `skip_none` | `bool` | `False` | When `True`, missing keys are dropped instead of appearing as `None`. |

**Returns:** `list` of `Model` instances (and `None` placeholders unless `skip_none=True`).

```python
keys = ["Restaurant:Burger Palace", "Restaurant:Sushi Zen"]
restaurants = Restaurant.query.get_many(redis_keys=keys)
```

The async counterpart is `await Model.query.async_get_many(redis_keys=keys)`.

### Query.filter()

```python
Query.filter(**kwargs) -> list
```

Return all instances matching the given filter parameters. Supports field lookups, ordering, limiting,
and value projection.

| Parameter | Type | Description |
|-----------|------|-------------|
| `**kwargs` | | Filter parameters (see below), plus optional `order_by`, `limit`, `values`. |

**Reserved keyword arguments:**

| Keyword | Type | Description |
|---------|------|-------------|
| `order_by` | `str` | Field name to sort by. Prefix with `-` for descending. |
| `limit` | `int` | Maximum number of results. |
| `values` | `tuple` | Tuple of field names to return as dicts instead of model instances. |

**Returns:** `list` of `Model` instances (or dicts when `values` is specified).

```python
cheap_items = MenuItem.query.filter(price__lte=9.99, order_by="price", limit=10)
```

!!! tip
    Available filter lookups depend on the field type. See the [Field Classes](#field-classes) section
    for each field's supported lookups.

### Query.all()

```python
Query.all(**kwargs) -> list
```

Return all instances of the model. Accepts `order_by`, `limit`, and `values` keyword arguments.

**Returns:** `list` of `Model` instances (or dicts when `values` is specified).

```python
all_restaurants = Restaurant.query.all()
```

### Query.count()

```python
Query.count(**kwargs) -> int
```

Count instances matching the given filters, or all instances if no filters are provided. Uses
`SCARD` when counting all instances (no filters), which is O(1).

**Returns:** `int` count of matching instances.

```python
total = Restaurant.query.count()
expensive = MenuItem.query.count(price__gte=20.0)
```

### Query.keys()

```python
Query.keys(catchall: bool = False, clean: bool = False, **kwargs) -> list
```

Return a list of Redis key bytes for all instances of this model.

| Parameter | Type | Description |
|-----------|------|-------------|
| `catchall` | `bool` | Use `KEYS *ClassName*` pattern (debug only). |
| `clean` | `bool` | Remove orphaned keys from the class set (debug only). |

**Returns:** `list` of Redis key bytes.

!!! warning
    Both `catchall` and `clean` are intended for debugging only and should not be used in production.

### Query.top_by_decay()

```python
Query.top_by_decay(field_name: str = None, n: int = 10, decay_rate: float = None, base_score_field: str = None) -> list
```

Return top-N instances ranked by time-decayed score. Executes a Lua script server-side
that computes `base_score * elapsed_days^(-decay_rate)` for each member in the sorted set.
For `CyclicDecayField`, the Lua script also adds cyclical resonance and homeostatic pressure
components to the score.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `field_name` | `str` | `None` | Name of a `DecayingSortedField` or `CyclicDecayField` on the model. Optional when the model has exactly one `DecayingSortedField` (or subclass). |
| `n` | `int` | `10` | Maximum number of results. |
| `decay_rate` | `float` | `None` | Override the field's decay_rate for this query. |
| `base_score_field` | `str` | `None` | Override the field's base_score_field for this query. |

**Returns:** `list` of Model instances in decayed-score order.

**Raises:**

- `QueryException` if `field_name` is omitted and the model has zero or multiple `DecayingSortedField` fields.
- `QueryException` if the field is not a `DecayingSortedField` (or subclass) or a required `partition_by` filter is missing.

```python
# Auto-detect field_name (works when model has exactly one DecayingSortedField)
results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=10)

# Explicit field_name (required when model has multiple DecayingSortedFields)
results = Memory.query.filter(agent_id="agent-1").top_by_decay("relevance", n=10)

# Aggressive decay — only very recent records
hot = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5, decay_rate=1.0)
```

Also available directly on `Query`: `Memory.query.top_by_decay(n=10)`.
For partitioned fields, use `filter()` first to specify the partition value.

### Query.composite\_score()

```python
Query.composite_score(
    indexes: dict[str, float],
    limit: int = 10,
    aggregate: str = "SUM",
    min_score: float = None,
    post_filter: Callable[[str, float], bool] = None,
    co_occurrence_boost: dict = None,
    temperature: float = 1.0,
) -> list
```

Return top-K instances ranked by a weighted composite of multiple sorted set indexes via Redis
`ZUNIONSTORE`. Combines decay, confidence, access frequency, write filter priority, and
co-occurrence signals into a single retrieval call.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `indexes` | `dict[str, float]` | *required* | Field names mapped to weights. Relative ratios matter, not absolute values. |
| `limit` | `int` | `10` | Maximum results to return. |
| `aggregate` | `str` | `"SUM"` | Score combination mode: `"SUM"`, `"MIN"`, or `"MAX"`. |
| `min_score` | `float` | `None` | Minimum composite score threshold. Results below are excluded. |
| `post_filter` | `Callable` | `None` | `(redis_key, score) -> bool` filter applied after scoring, before hydration. |
| `co_occurrence_boost` | `dict` | `None` | `{redis_key: weight}` from `CoOccurrenceField.propagate()`. |
| `temperature` | `float` | `1.0` | Score scaling factor. Divides each score by this value. Low values (0.02-0.1) sharpen; high values (2.0+) flatten. Must be > 0. |

Supported index types: `DecayingSortedField`, `CyclicDecayField`, `SortedField`, `ConfidenceField`,
`"access_count"` / `"access_score"` (AccessTrackerMixin), and `"priority"` (WriteFilterMixin).

```python
# Multi-factor retrieval with four scoring signals
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={
        "relevance": 0.4,      # DecayingSortedField
        "certainty": 0.3,      # ConfidenceField
        "access_count": 0.2,   # AccessTrackerMixin
        "priority": 0.1,       # WriteFilterMixin
    },
    limit=10,
)

# With co-occurrence boost from graph propagation
assoc_field = Memory._meta.fields["associations"]
co_scores = assoc_field.propagate(Memory, seed_pks=["key1"], depth=2)
results = Memory.query.filter(agent_id="agent-1").composite_score(
    indexes={"relevance": 0.3, "certainty": 0.3},
    co_occurrence_boost=co_scores,
    limit=10,
)
```

Also available directly on `Query`: `Memory.query.composite_score(indexes={...})`.
For partitioned fields, use `filter()` first to specify the partition value.

See [CompositeScoreQuery feature docs](features/composite-score-query.md) for the full reference
including index resolution strategies, temp key management, and error handling.

### Query.semantic\_search()

```python
QueryBuilder.semantic_search(
    query_text: str,
    indexes: dict = None,
    limit: int = 10,
    aggregate: str = "SUM",
    min_score: float = None,
    post_filter: Callable = None,
    co_occurrence_boost: dict = None,
    temperature: float = 1.0,
) -> list
```

Return top-K instances ranked by semantic similarity to `query_text`. Requires the model to have
an `EmbeddingField` and a configured embedding provider (via `popoto.configure()` or per-field).

When `indexes` is None, results are ranked by cosine similarity alone. When `indexes` is provided,
similarity scores are injected into `composite_score()` as an additional weighted signal.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_text` | `str` | *(required)* | Text to embed and search for. |
| `indexes` | `dict` | `None` | Sorted field names to weights for composite scoring. |
| `limit` | `int` | `10` | Maximum results. |
| `aggregate` | `str` | `"SUM"` | Aggregation mode for ZUNIONSTORE. |
| `min_score` | `float` | `None` | Minimum composite score threshold. |
| `post_filter` | `Callable` | `None` | `(redis_key, score) -> bool` filter function. |
| `co_occurrence_boost` | `dict` | `None` | `{redis_key: weight}` association boost dict. |
| `temperature` | `float` | `1.0` | Score scaling factor. |

```python
results = Memory.query.semantic_search("revenue trends", limit=5)

# Combined with sorted indexes
results = Memory.query.semantic_search(
    "revenue trends",
    indexes={"relevance": 0.4, "confidence": 0.3},
    limit=10,
)
```

Also available directly on `Query`: `Memory.query.semantic_search(...)`.

See [Semantic Search](query.md#semantic-search) for conceptual overview and
[Content and Embedding Fields](features/content-and-embedding-fields.md) for the full feature reference.

### Async Query Methods

| Sync | Async |
|------|-------|
| `Model.query.get(...)` | `await Model.query.async_get(...)` |
| `Model.query.filter(...)` | `await Model.query.async_filter(...)` |
| `Model.query.all(...)` | `await Model.query.async_all(...)` |
| `Model.query.count(...)` | `await Model.query.async_count(...)` |
| `Model.query.keys(...)` | `await Model.query.async_keys(...)` |

---

## Field Classes

All fields inherit from `Field`. Fields define value type, validation, defaults, and optional
Redis-backed indexes. See [Fields](fields.md) for a conceptual overview.

### Field

```python
Field(type=str, null=True, default=None, max_length=1024)
```

Base class for all model fields. Stores a value in the model's Redis hash.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `type` | `type` | `str` | Python type for the value. Must be one of: `int`, `float`, `Decimal`, `str`, `bool`, `bytes`, `list`, `dict`, `set`, `tuple`, `date`, `datetime`, `time`. |
| `null` | `bool` | `True` | Allow `None` values. |
| `default` | any | `None` | Default value (or callable) for new instances. |
| `max_length` | `int` | `1024` | Maximum string length enforced on save. |

```python
class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    active = Field(type=bool, default=True)
```

### KeyField

```python
KeyField(type=str, null=True, max_length=128, **kwargs)
```

A field that forms part of the model's Redis key. All `KeyField` values together enforce a
unique-together constraint. Backed by Redis sets for fast lookups.

**Supported filter lookups:**

| Lookup | Example | Description |
|--------|---------|-------------|
| exact | `name="Taco Town"` | Exact match. |
| `__isnull` | `name__isnull=True` | Match `None` values. |
| `__contains` | `name__contains="Taco"` | Substring match (uses Redis `KEYS` pattern). |
| `__startswith` | `name__startswith="Taco"` | Prefix match. |
| `__endswith` | `name__endswith="Town"` | Suffix match. |
| `__in` | `name__in=["Taco Town", "Burger Palace"]` | Match any value in the list. |

**Valid types:** `int`, `float`, `Decimal`, `str`, `bool`, `date`, `datetime`, `time`.

### AutoKeyField

```python
AutoKeyField(**kwargs)
```

A `UniqueKeyField` whose value is auto-generated using a UUID. Automatically added (as `_auto_key`)
to models that define no `KeyField` of their own.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `auto_uuid_length` | `int` | `32` | Length of the generated hex UUID. |

!!! note
    You cannot set `unique=False` or `null=True` on an `AutoKeyField`.

### UniqueKeyField

```python
UniqueKeyField(type=str, **kwargs)
```

A `KeyField` with a per-value uniqueness constraint. Cannot be null. Useful for fields like email
addresses or phone numbers that must be globally unique.

!!! warning
    Setting `unique=False` or `null=True` raises `ModelException`.

```python
class Customer(Model):
    username = KeyField()
    email = UniqueKeyField()
```

### IndexedField

```python
IndexedField(type=str, null=True, unique=False, **kwargs)
```

A non-key field with Set-based secondary indexing. Enables efficient `filter()` queries
without making the field part of the model's Redis key. Equivalent to `Field(indexed=True)`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `type` | `type` | `str` | Python type for the value. |
| `null` | `bool` | `True` | Allow `None` values. |
| `unique` | `bool` | `False` | Enforce per-value uniqueness. |

**Supported filter lookups:**

| Lookup | Example | Description |
|--------|---------|-------------|
| exact | `status="active"` | Exact match (SMEMBERS). |
| `__isnull` | `status__isnull=True` | Match `None` values. |
| `__startswith` | `status__startswith="act"` | Prefix match (SCAN). |
| `__endswith` | `status__endswith="ive"` | Suffix match (SCAN). |
| `__in` | `status__in=["active", "pending"]` | Match any value (SUNION). |

```python
class Order(Model):
    order_id = AutoKeyField()
    status = IndexedField(type=str)
    region = IndexedField(type=str, null=True)
```

See [Indexed Fields](indexed_fields.md) for the full guide.

### UniqueField

```python
UniqueField(type=str, **kwargs)
```

An indexed non-key field with a per-value uniqueness constraint. Cannot be null.
Equivalent to `Field(indexed=True, unique=True)`.

!!! warning
    Setting `unique=False` or `null=True` raises `ModelException`.

```python
class User(Model):
    user_id = AutoKeyField()
    email = UniqueField(type=str)
```

Supports the same filter lookups as `IndexedField`. See [Indexed Fields](indexed_fields.md).

### SortedField

```python
SortedField(type=float, null=False, default=None, partition_by=(), **kwargs)
```

A field backed by a Redis sorted set for fast range queries. Must be a numeric type.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `type` | `type` | `float` | Must be `int`, `float`, `Decimal`, `date`, or `datetime`. |
| `null` | `bool` | `False` | Must be `False` (sorted fields cannot be null). |
| `partition_by` | `tuple` | `()` | Partition the sorted set by other field names. `sort_by` is accepted as a deprecated alias. |

**Supported filter lookups:**

| Lookup | Example | Description |
|--------|---------|-------------|
| exact | `price=9.99` | Exact value. |
| `__gt` | `price__gt=10.0` | Greater than (exclusive). |
| `__gte` | `price__gte=10.0` | Greater than or equal (inclusive). |
| `__lt` | `price__lt=20.0` | Less than (exclusive). |
| `__lte` | `price__lte=20.0` | Less than or equal (inclusive). |

```python
affordable = MenuItem.query.filter(price__lte=9.99)
```

### SortedKeyField

```python
SortedKeyField(**kwargs)
```

A field that combines `KeyField` and `SortedField` behaviors. It forms part of the Redis key and
is also indexed in a sorted set for range queries. Supports all lookups from both `KeyField` and
`SortedField`.

### DecayingSortedField

```python
from popoto.fields.decaying_sorted_field import DecayingSortedField

DecayingSortedField(decay_rate=0.1, base_score_field=None, partition_by=(), **kwargs)
```

A `SortedField` subclass that stores timestamps as scores and computes time-decayed rankings
via a server-side Lua script. See [DecayingSortedField](fields.md#decayingsortedfield) for
usage examples and [Agent Memory](features/agent-memory.md) for the broader context.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.1` | Controls decay speed. Must be > 0. (Empirically tuned in sweep 2026-04-17; prior default was `0.5`.) |
| `base_score_field` | `str` | `None` | Companion field name for base score multiplier. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values. |

### CyclicDecayField

```python
from popoto.fields.cyclic_decay_field import CyclicDecayField

CyclicDecayField(decay_rate=0.1, base_score_field=None, cycles=[], pressure_rate=0.0, partition_by=(), **kwargs)
```

A `DecayingSortedField` subclass that adds cyclical resonance and homeostatic pressure
to time-decayed scoring. All three components are computed atomically in a single Lua script.
See [CyclicDecayField](features/cyclic-decay-field.md) for usage examples and
[Agent Memory](features/agent-memory.md) for the broader context.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.1` | Controls decay speed (inherited). Must be > 0. (Empirically tuned in sweep 2026-04-17; prior default was `0.5`.) |
| `base_score_field` | `str` | `None` | Companion field name for base score multiplier (inherited). |
| `cycles` | `list` | `[]` | List of `(period, amplitude, phase)` tuples. Use `TemporalPeriod` constants. |
| `pressure_rate` | `float` | `0.0` | Rate of urgency buildup per unresolved day. Must be >= 0. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values (inherited). |

#### CyclicDecayField.get\_cycles\_hash\_key(instance, field\_name)

Build the Redis key for the cycles companion hash from a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |

**Returns:** `str` -- Redis key for the cycles companion hash.

**Key pattern:** `$CyclicDecayF:{Model}:{field}:{partitions}:cycles`

#### CyclicDecayField.get\_pressure\_hash\_key(instance, field\_name)

Build the Redis key for the pressure companion hash from a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |

**Returns:** `str` -- Redis key for the pressure companion hash.

**Key pattern:** `$CyclicDecayF:{Model}:{field}:{partitions}:pressure`

#### CyclicDecayField.get\_cycles\_hash\_key\_from\_parts(model\_class, field\_name, \*partition\_values)

Class method. Build the cycles hash key from a model class and explicit partition values,
without needing a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |
| `*partition_values` | | Positional partition field values. |

**Returns:** `str` -- Redis key for the cycles companion hash.

#### CyclicDecayField.get\_pressure\_hash\_key\_from\_parts(model\_class, field\_name, \*partition\_values)

Class method. Build the pressure hash key from a model class and explicit partition values,
without needing a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |
| `*partition_values` | | Positional partition field values. |

**Returns:** `str` -- Redis key for the pressure companion hash.

### AccessTrackerMixin

```python
from popoto import AccessTrackerMixin

class MyModel(AccessTrackerMixin, Model):
    _max_access_log = 100  # max confirmed timestamps kept (default)
    _track_reads = True    # auto-fire on_read from queries (default)
```

A model mixin that tracks read access patterns with a two-stage pipeline (staged → confirmed).
See [Agent Memory — AccessTracker](features/agent-memory.md#accesstracker) for full usage guide.

#### AccessTrackerMixin.on\_read(pipeline=None)

Stage a read by appending the current timestamp to the staging list. Called automatically by query hooks.

#### AccessTrackerMixin.confirm\_access(pipeline=None) -> int

Atomically promote all staged timestamps to the confirmed access log via Lua script. Returns the number of staged reads promoted. Raises `TypeError` if the instance has not been saved.

#### AccessTrackerMixin.discard\_staged\_access(pipeline=None)

Clear the staging list without affecting confirmed data.

#### AccessTrackerMixin.access\_count -> int

Total number of confirmed read accesses. Returns `0` if never confirmed.

#### AccessTrackerMixin.last\_accessed -> float | None

Timestamp of the most recent confirmed read. Returns `None` if never confirmed.

### ObservationProtocol

```python
from popoto import ObservationProtocol
```

Stateless coordinator that provides lifecycle hooks for outcome-driven memory effects.
All methods are static. See [Agent Memory — ObservationProtocol](features/agent-memory.md#observationprotocol)
for the full usage guide.

#### ObservationProtocol.on\_read(instance, pipeline=None)

Fire when query hydrates an instance. Delegates to `AccessTrackerMixin.on_read()` if available. No-op for models without `AccessTrackerMixin`.

#### ObservationProtocol.on\_surfaced(instances, reason="proactive", partition=None, pipeline=None)

Fire when proactive system pushes memories into agent context. Creates `RecallProposal` entries. Side-effect-free on the memories themselves.

#### ObservationProtocol.on\_context\_used(instances, outcome\_map, pipeline=None)

Fire when application reports how agent responded to surfaced memories. Applies outcome-specific effects atomically.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instances` | `list` | Model instances that were in the agent's context. |
| `outcome_map` | `dict` | Maps instance Redis keys to outcomes: `"acted"`, `"used"`, `"dismissed"`, `"deferred"`, `"contradicted"`. Instances not in the map default to `"deferred"`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Raises:** `ValueError` if any outcome string is not valid.

### RecallProposal

```python
from popoto import RecallProposal
```

Internal ORM infrastructure for tracking proactively surfaced memories. ZSET-backed with TTL-based expiration.

#### RecallProposal.create\_batch(instances, reason="proactive", partition=None, pipeline=None)

Create pending proposals for a batch of instances. Stores in `$RP:{ClassName}:pending:{partition}` ZSET scored by surfaced_at.

#### RecallProposal.resolve(instance, outcome, partition=None, pipeline=None) -> int

Remove a resolved proposal from the pending set. Idempotent — returns 0 if already removed.

#### RecallProposal.expire\_stale(model\_class, partition=None, ttl=None, pipeline=None) -> list

Remove proposals older than TTL (default 3600 seconds). Returns list of expired member key strings.

#### RecallProposal.get\_pending(model\_class, partition=None) -> list

Return all pending proposals as `(member_key, surfaced_at)` tuples.

### ConfidenceField

```python
from popoto import ConfidenceField
# or: from popoto.fields.confidence_field import ConfidenceField
```

A `Field` subclass that tracks Bayesian confidence metadata per member, updated atomically via Lua script.
Precision grows with `sqrt(n)` — early evidence has outsized effect while established beliefs resist change.

See [ConfidenceField feature docs](features/confidence-field.md) for the full reference including the
Bayesian update formula, convergence behavior, and entrainment with ObservationProtocol.

```python
ConfidenceField(initial_confidence=0.5, partition_by=(), **kwargs)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `initial_confidence` | `float` | `0.5` | Starting confidence for new members (0-1). |
| `partition_by` | `str` or `tuple` | `()` | Field name(s) to partition the companion hash. Splits the single Redis hash into per-partition hashes for efficient reads. Mirrors SortedField's `partition_by` API. |

#### ConfidenceField.update\_confidence(instance, field\_name, signal)

Atomically update confidence using the Bayesian formula: `new = prior + (signal - prior) / sqrt(evidence_count + 1)`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `ConfidenceField` on the model. |
| `signal` | `float` | Value 0-1. Values >= 0.5 corroborate, < 0.5 contradict. |

**Returns:** The new confidence value (float).

**Raises:** `TypeError` if instance is unsaved or field is wrong type; `ValueError` if signal is out of range.

#### ConfidenceField.get\_confidence(instance, field\_name)

Read the current confidence value.

**Returns:** Float confidence value, or `initial_confidence` if no data exists.

#### ConfidenceField.get\_confidence\_data(instance, field\_name)

Read all confidence metadata.

**Returns:** Dict with keys `confidence`, `evidence_count`, `corroborations`, `contradictions`.

#### ConfidenceField.get\_confidence\_filtered(model\_class, field\_name, pattern="\*")

Get confidence data for members matching an HSCAN pattern. Useful for filtered reads on
unpartitioned hashes without loading all entries into memory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `ConfidenceField`. |
| `pattern` | `str` | Redis MATCH pattern for HSCAN (default `*`). |

**Returns:** Dict of `{member_key: {confidence, evidence_count, ...}}` for matching entries.

#### ConfidenceField.migrate\_to\_partitioned(model\_class, field\_name, dry\_run=False)

Migrate existing unpartitioned hash data into partitioned hashes. Reads all entries from the
single companion hash, loads each model instance to determine partition field values, and
writes to the appropriate partitioned hash.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class with the `ConfidenceField`. |
| `field_name` | `str` | Name of the `ConfidenceField` to migrate. |
| `dry_run` | `bool` | If `True`, report what would happen without modifying data. |

**Returns:** Dict with `total`, `migrated`, `errors`, and `partitions` counts.

**Raises:** `ModelException` if the field has no `partition_by` configured.

#### ConfidenceField.get\_data\_hash\_key(instance, field\_name)

Build the Redis key for the confidence companion hash from a model instance. When
`partition_by` is set, appends partition field values to the key.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `ConfidenceField`. |

**Returns:** `str` -- Redis key for the companion hash.

**Key pattern (unpartitioned):** `$ConfidencF:{Model}:{field}:data`
**Key pattern (partitioned):** `$ConfidencF:{Model}:{field}:data:{partition_val}`

```python
field = Memory._options.fields["certainty"]
hash_key = field.get_data_hash_key(memory, "certainty")
# => "$ConfidencF:Memory:certainty:data"
```

#### ConfidenceField.get\_data\_hash\_key\_from\_values(model\_class, field\_name, \*\*partition\_values)

Build the companion hash key from explicit partition values, without needing a model
instance. Useful in query paths and bulk operations scoped to a partition.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `ConfidenceField`. |
| `**partition_values` | | Mapping of partition field names to values. |

**Returns:** `str` -- Redis key for the companion hash.

**Raises:** `QueryException` if a required partition field value is missing.

```python
field = Memory._options.fields["certainty"]
key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
# => "$ConfidencF:Memory:certainty:data:atlas"
```

#### ConfidenceField.get\_old\_data\_hash\_key(instance, field\_name)

Build the companion hash key using saved (pre-mutation) partition field values. Used
during `on_save`/`on_delete` to locate the old partition hash when a partition key has
changed. Also useful for custom partition migration logic.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `ConfidenceField`. |

**Returns:** `str` or `None` -- The old hash key, or `None` if no saved values exist.

#### ObservationProtocol entrainment

When used with `ObservationProtocol.on_context_used()`, confidence is automatically updated:

| Outcome | Effect |
|---------|--------|
| `acted` | Corroborate (signal=0.9) |
| `contradicted` | Contradict (signal=0.1); auto-discharge pressure if confidence drops below 0.1 |
| `dismissed` / `deferred` | No change |

### PredictionLedgerMixin

```python
from popoto import PredictionLedgerMixin
# or: from popoto.fields.prediction_ledger import PredictionLedgerMixin
```

A model mixin that adds prediction recording, resolution, and error tracking. Records
prediction-outcome pairs and computes prediction error. High errors feed back into
`ConfidenceField` to reduce confidence. Auto-resolution via `ObservationProtocol` handles
outcomes inferred from behavior.

See [Agent Memory -- PredictionLedger](features/agent-memory.md#predictionledger) for
the full usage guide and [Fields -- PredictionLedgerMixin](fields.md#predictionledgermixin) for setup examples.

```python
class MyModel(PredictionLedgerMixin, Model):
    _pl_partition = "default"                  # partition for error sorted set
    _pl_confidence_error_threshold = 0.7       # error above which confidence is reduced
    _pl_confidence_low_signal = 0.2            # signal sent to ConfidenceField
    _pl_auto_resolve_errors = {                # outcome-to-error mapping
        "acted": 0.1, "dismissed": 0.5, "contradicted": 0.9,
    }
```

#### PredictionLedgerMixin.record\_prediction(instance, predicted, pipeline=None)

Store a prediction for a saved model instance. The prediction can later be resolved with
`resolve_prediction()` or `auto_resolve()`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `predicted` | `dict` | Dict of predicted values. Must not be `None`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Raises:** `TypeError` if instance is unsaved; `ValueError` if `predicted` is `None`.

#### PredictionLedgerMixin.resolve\_prediction(instance, actual, pipeline=None)

Resolve a prediction with actual outcome values. Atomically reads the prediction, computes
error, marks resolved, and ZADDs error to the error sorted set via Lua script.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance with a recorded prediction. |
| `actual` | `dict` | Dict of actual values. Must not be `None`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Returns:** `float` prediction error, or `None` if no prediction exists or already resolved.

**Raises:** `TypeError` if instance is unsaved; `ValueError` if `actual` is `None`.

#### PredictionLedgerMixin.auto\_resolve(instance, outcome, pipeline=None)

Auto-resolve a prediction based on an ObservationProtocol outcome. Maps the outcome string
to a prediction error value using the `_pl_auto_resolve_errors` class attribute.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance with a recorded prediction. |
| `outcome` | `str` | One of `"acted"`, `"dismissed"`, `"contradicted"`. |
| `pipeline` | `redis.client.Pipeline` | Optional pipeline for batching. |

**Returns:** `float` prediction error, or `None` if no prediction exists or already resolved.

**Raises:** `ValueError` if outcome is not valid.

#### PredictionLedgerMixin.get\_prediction\_data(instance)

Read current prediction metadata for an instance.

**Returns:** Dict with keys `predicted`, `resolved`, `resolution_mode`, `prediction_error`, `resolved_at`, `recorded_at`, or `None` if no prediction exists.

#### PredictionLedgerMixin.get\_highest\_errors(model\_class, partition="default", limit=10)

Query instances with the highest prediction errors from the error sorted set.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model_class` | `type` | | The Model class to query. |
| `partition` | `str` | `"default"` | Partition key. |
| `limit` | `int` | `10` | Maximum results. |

**Returns:** List of `(member_key_str, error_float)` tuples, ordered by descending error.

#### PredictionLedgerMixin.compute\_prediction\_error(predicted, actual)

Compute prediction error between predicted and actual dicts. Overridable on subclasses
for custom error metrics.

- Numeric values: `|predicted - actual| / max(|predicted|, |actual|, 1)`
- String values: `0.0` if equal, `1.0` if different
- Missing keys: `1.0` error per missing key
- Overall: mean across all keys

**Returns:** Float in `[0, 1]`.

#### PredictionLedgerMixin.error\_summary(model\_class, partition="default", group\_by=None, limit=100)

Aggregate prediction errors with optional grouping. Reads up to `limit` members from the error sorted set and fetches per-instance metadata in a pipelined batch.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model_class` | `type` | | The Model class to query. |
| `partition` | `str` | `"default"` | Partition key. |
| `group_by` | `str` \| `callable` \| `None` | `None` | `None` for overall stats; `"hour"`, `"weekday"`, or `"day"` for built-in time bucketers; callable `(member_key, error) -> label` for custom grouping. Unknown strings raise `ValueError`. |
| `limit` | `int` | `100` | Max members to sample from the error set. |

**Returns:** `dict` mapping group labels to stats dicts with keys `count`, `mean`, `stddev`, `p50`, `p90`, `p99`, `max`. Ungrouped key is `"__all__"`. Empty set returns `{"__all__": {"count": 0, ...}}`.

See [Metacognitive Layer — error_summary](features/metacognitive-layer.md) for full documentation.

### CoOccurrenceField

```python
from popoto.fields.co_occurrence_field import CoOccurrenceField
```

A field that maintains a co-occurrence graph as Redis sorted sets. Each primary key gets
an edge sorted set tracking weighted links to other PKs. See
[CoOccurrenceField feature docs](features/co-occurrence-field.md) for usage examples.

#### CoOccurrenceField.get\_edge\_key(model\_class, pk)

Build the Redis key for a PK's edge sorted set. Use this for direct Redis access to
a specific node's edges (e.g., bulk edge inspection, custom graph queries, monitoring).

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class (or instance). |
| `pk` | `str` | The primary key string. |

**Returns:** `str` -- Redis key for this PK's edge sorted set.

**Key pattern:** `$CoOcF:{ClassName}:{field_name}:{pk}`

```python
field = Memory._options.fields["associations"]
edge_key = field.get_edge_key(Memory, "fact1")
# => "$CoOcF:Memory:associations:fact1"
```

#### CoOccurrenceField.get\_edge\_key\_prefix(model\_class)

Build the Redis key prefix for scanning or iterating over all edge sorted sets for a
field (e.g., graph analytics, bulk cleanup).

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |

**Returns:** `str` -- Key prefix ending with colon.

**Key pattern:** `$CoOcF:{ClassName}:{field_name}:`

### InteractionWeight

```python
from popoto import InteractionWeight
# or: from popoto.fields.constants import InteractionWeight
```

Weight constants for source/role-based importance scoring, designed for use with `DecayingSortedField`'s `base_score_field` parameter. Two axes combined by addition: source (what kind of entity) and role (authority level).

#### Source axis

| Constant | Value | Description |
|----------|-------|-------------|
| `InteractionWeight.HUMAN` | `6.0` | Human interaction |
| `InteractionWeight.AGENT` | `1.0` | Agent-to-agent interaction |
| `InteractionWeight.SYSTEM` | `0.2` | Automated system event |

#### Role axis

| Constant | Value | Description |
|----------|-------|-------------|
| `InteractionWeight.EXECUTIVE` | `44.0` | Executive-level authority |
| `InteractionWeight.MANAGER` | `16.0` | Manager-level authority |
| `InteractionWeight.PEER` | `6.0` | Peer-level authority |
| `InteractionWeight.SUBORDINATE` | `1.0` | Subordinate-level authority |

#### InteractionWeight.combine(source, role)

```python
InteractionWeight.combine(source: float, role: float) -> float
```

Add source and role weights together. With `decay_rate=0.5`, effective lifetime is approximately `score**2` days. (The current default `decay_rate=0.1` produces a much slower decay curve; the `score**2` mnemonic applies only when `decay_rate=0.5` is passed explicitly.)

```python
# Human executive directive — stays relevant for ~7 years
score = InteractionWeight.combine(InteractionWeight.HUMAN, InteractionWeight.EXECUTIVE)
# => 50.0

# Agent peer observation — ~7 weeks
score = InteractionWeight.combine(InteractionWeight.AGENT, InteractionWeight.PEER)
# => 7.0
```

See [Agent Memory — Source weighting](features/agent-memory.md#source-weighting-for-teamwork) for the full lifetime table and usage patterns.

### Defaults

```python
from popoto import Defaults
# or: from popoto.fields.constants import Defaults
```

Centralized registry of all tuning constants across the 14 agent-memory primitives. Override globally by setting class attributes, or per-field via explicit kwargs (explicit kwargs always win).

#### Constants by primitive

| Constant | Default | Owner |
|----------|---------|-------|
| `Defaults.DECAY_RATE` | `0.5` | DecayingSortedField |
| `Defaults.INITIAL_CONFIDENCE` | `0.5` | ConfidenceField |
| `Defaults.ACTED_CONFIDENCE_SIGNAL` | `0.9` | ObservationProtocol |
| `Defaults.CONTRADICTED_CONFIDENCE_SIGNAL` | `0.1` | ObservationProtocol |
| `Defaults.ACTED_CYCLE_STRENGTHEN_FACTOR` | `1.2` | ObservationProtocol |
| `Defaults.DISMISSED_CYCLE_WEAKEN_FACTOR` | `0.8` | ObservationProtocol |
| `Defaults.CONTRADICTED_CYCLE_WEAKEN_FACTOR` | `0.5` | ObservationProtocol |
| `Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` | `0.1` | ObservationProtocol |
| `Defaults.WF_MIN_THRESHOLD` | `0.2` | WriteFilterMixin |
| `Defaults.WF_PRIORITY_THRESHOLD` | `0.7` | WriteFilterMixin |
| `Defaults.CO_OCCURRENCE_DECAY_FACTOR` | `0.95` | CoOccurrenceField |
| `Defaults.CO_OCCURRENCE_INITIAL_WEIGHT` | `1.0` | CoOccurrenceField |
| `Defaults.CO_OCCURRENCE_DECAY_PER_HOP` | `0.5` | CoOccurrenceField |
| `Defaults.PL_CONFIDENCE_ERROR_THRESHOLD` | `0.7` | PredictionLedgerMixin |
| `Defaults.PL_CONFIDENCE_LOW_SIGNAL` | `0.2` | PredictionLedgerMixin |
| `Defaults.PL_AUTO_RESOLVE_ACTED` | `0.1` | PredictionLedgerMixin |
| `Defaults.PL_AUTO_RESOLVE_DISMISSED` | `0.5` | PredictionLedgerMixin |
| `Defaults.PL_AUTO_RESOLVE_CONTRADICTED` | `0.9` | PredictionLedgerMixin |
| `Defaults.PL_AUTO_RESOLVE_USED` | `0.3` | PredictionLedgerMixin / metacognitive layer |
| `Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE` | `20` | AdaptiveAssembler |
| `Defaults.MIN_EVENTS_FOR_CRYSTALLIZATION` | `3` | PolicyCache |
| `Defaults.WILSON_CI_THRESHOLD` | `0.6` | PolicyCache |
| `Defaults.TD_ALPHA` | `0.1` | PolicyCache |
| `Defaults.TD_GAMMA` | `0.95` | PolicyCache |
| `Defaults.CHI_SQUARED_P_THRESHOLD` | `0.05` | PolicyCache |
| `Defaults.INITIAL_CYCLE_AMPLITUDE` | `0.5` | PolicyCache |
| `Defaults.COMPETITIVE_SUPPRESSION_SIGNAL` | `0.3` | ContextAssembler |
| `Defaults.DEFAULT_SURFACING_THRESHOLD` | `0.5` | ContextAssembler |

#### Global override example

```python
from popoto import Defaults

# All DecayingSortedFields will use 0.7 unless they pass decay_rate= explicitly
Defaults.DECAY_RATE = 0.7
```

See [Tuning Magic Numbers](guides/tuning-magic-numbers.md) for benchmark-validated override guidance.

### RetrievalQuality

```python
from popoto import RetrievalQuality
# or: from popoto.recipes import RetrievalQuality
# or: from popoto.recipes.context_assembler import RetrievalQuality
```

Dataclass returned by `ContextAssembler.assess()` and attached to `AssemblyResult.metadata["quality"]` when `assess_quality=True`. A purely mechanical retrieval quality signal — no LLM self-reporting.

| Field | Type | Description |
|-------|------|-------------|
| `avg_confidence` | `float` | Mean `ConfidenceField.get_confidence()` across selected records. `1.0` when no `ConfidenceField` is configured. |
| `score_spread` | `float` | Coefficient of variation (`stddev / mean`) of per-record composite scores. `0.0` when `abs(mean) < 1e-9`. |
| `fok_score` | `float` | Feeling-of-knowing (0.0–1.0). Formula: `0.4 * cue_familiarity + 0.4 * partial_retrieval_count + 0.2 * subthreshold_activation`, averaged across cues. `0.0` when no cues provided. |
| `staleness_ratio` | `float` | Fraction of selected records with `DecayingSortedField` score below `surfacing_threshold`. `0.0` when no `DecayingSortedField`. |
| `score_distribution` | `list[float]` | Full list of per-record composite scores. Empty when unavailable. |
| `per_cue_fok` | `dict` | Maps cue value → `{cue_familiarity, partial_retrieval_count, subthreshold_activation, component_score}`. |

See [Metacognitive Layer](features/metacognitive-layer.md) for full documentation.

### ContextAssembler

```python
from popoto.recipes.context_assembler import ContextAssembler
```

Orchestrates pull-path (query-driven) and push-path (proactive) retrieval into a single `assemble()` call. See [ContextAssembler feature docs](features/context-assembler.md) for full usage.

#### ContextAssembler(model\_class, score\_weights, ...)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model_class` | `type` | | Popoto Model class to query. |
| `score_weights` | `dict` | | Maps field names to weights for `CompositeScoreQuery`. |
| `max_items` | `int` | `10` | Maximum records to return. |
| `max_tokens` | `int` \| `None` | `None` | Soft token budget; records dropped to fit. |
| `surfacing_threshold` | `float` | `0.5` | Minimum score for push-path records. |
| `propagation_depth` | `int` | `2` | BFS depth for CoOccurrence expansion. |
| `output_format` | `str` | `"structured"` | `"structured"` (JSON), `"xml"`, or `"natural"`. |
| `token_counter` | `callable` \| `None` | `None` | `callable(record) -> int`. Default: `len(str(r)) // 4`. |

#### ContextAssembler.assemble(query\_cues=None, agent\_id=None, partition\_filters=None, assess\_quality=False)

Execute the full retrieval pipeline. Returns `AssemblyResult`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_cues` | `dict` \| `None` | `None` | Query cues. If `None`, pull path is skipped. |
| `agent_id` | `str` \| `None` | `None` | Added to `partition_filters` as `{"agent_id": agent_id}`. |
| `partition_filters` | `dict` \| `None` | `None` | Partition key-value pairs for filtering. |
| `assess_quality` | `bool` | `False` | When `True`, compute `RetrievalQuality` and attach to `AssemblyResult.metadata["quality"]`. Off by default; existing result shape is unchanged. |

#### ContextAssembler.assess(query\_cues=None, partition\_filters=None, probe\_limit=None)

Pre-retrieval quality probe. Runs `ExistenceFilter` + a single low-limit `composite_score` call — no propagation, no push path, no post-effects.

**Returns:** `RetrievalQuality`.

### AdaptiveAssembler

```python
from popoto.recipes.adaptive_assembler import AdaptiveAssembler
# or: from popoto.recipes import AdaptiveAssembler
# or: from popoto import AdaptiveAssembler
```

Wraps a `ContextAssembler` with an autoresearch-style keep/revert loop that adjusts `score_weights` online. Single-threaded by design; adaptation is per-process only (does not survive restarts).

#### AdaptiveAssembler(inner, window\_size=20, quality\_metric=None, weight\_perturbation=0.05, rng=None)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `inner` | `ContextAssembler` | | The assembler to wrap. |
| `window_size` | `int` | `Defaults.ADAPTIVE_QUALITY_WINDOW_SIZE` (20) | Calls per rolling window. |
| `quality_metric` | `callable` \| `None` | `lambda q: q.fok_score * q.avg_confidence` | Scalarizes `RetrievalQuality` → `float`. |
| `weight_perturbation` | `float` | `0.05` | How much to shift per weight proposal. |
| `rng` | `random.Random` \| `None` | `None` | Optional seeded RNG for deterministic tests. |

#### AdaptiveAssembler.assemble(query\_cues=None, \*\*kwargs)

Delegates to `inner.assemble()` with `assess_quality=True` forced, records the quality sample, and runs the keep/revert state machine. Returns the inner `AssemblyResult` unchanged.

#### AdaptiveAssembler.current\_weights

`dict` — copy of the currently-active `score_weights`.

#### AdaptiveAssembler.baseline\_quality

`float | None` — rolling-window mean quality under baseline weights.

#### AdaptiveAssembler.is\_testing\_candidate

`bool` — `True` while gathering quality samples under a candidate perturbation.

See [Metacognitive Layer](features/metacognitive-layer.md) for full documentation.

### ContentField

```python
from popoto.fields.content_field import ContentField

ContentField(store="filesystem", **kwargs)
```

Routes large content values to filesystem storage. Redis stores only a compact
`$CF:{hash}:{path}` reference string. Content is lazy-loaded on attribute access.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `store` | `AbstractContentStore` or `"filesystem"` | `"filesystem"` | Content store backend. |

| Class Method | Returns | Description |
|-------------|---------|-------------|
| `on_save(instance, field_name, value, pipeline)` | pipeline | Write content to filesystem, store reference in Redis. |
| `on_delete(instance, field_name, value, pipeline)` | pipeline | No-op (append-only storage). |
| `garbage_collect(model_class)` | `int` | Remove orphaned content files. |

See [Fields > ContentField](fields.md#contentfield) for detailed usage.

### EmbeddingField

```python
from popoto.fields.embedding_field import EmbeddingField

EmbeddingField(source=None, provider=None, auto_embed=True, cache=True, **kwargs)
```

Generates vector embeddings from a source field on save. Stores embeddings as `.npy` files
and maintains an in-memory cache for fast cosine similarity computation.

Requires numpy: `pip install popoto[embeddings]`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source` | `str` | `None` | Field name to read content from for embedding. |
| `provider` | `AbstractEmbeddingProvider` | `None` | Provider instance, or None for global default. |
| `auto_embed` | `bool` | `True` | Generate embeddings automatically on save. |
| `cache` | `bool` | `True` | Cache embeddings in memory for fast similarity. |

| Class Method | Returns | Description |
|-------------|---------|-------------|
| `on_save(instance, field_name, value, pipeline)` | pipeline | Generate embedding and store as `.npy`. |
| `on_delete(instance, field_name, value, pipeline)` | pipeline | Remove `.npy` file. |
| `load_embeddings(model_class)` | `(matrix, keys)` | Load all embeddings into a pre-normalized numpy matrix. |
| `garbage_collect(model_class)` | `int` | Remove orphaned `.npy` files. |

See [Fields > EmbeddingField](fields.md#embeddingfield) for detailed usage.

### GeoField

```python
GeoField(null=True, **kwargs)
```

A field that stores geospatial coordinates and enables radius search. Values are
`GeoField.Coordinates(latitude, longitude)` namedtuples (plain tuples are also accepted). Backed by
a Redis GEO set.

See [Fields > GeoField](fields.md) for detailed usage.

**Supported filter lookups:**

| Lookup | Example | Description |
|--------|---------|-------------|
| exact | `location=(40.7, -74.0)` | Center point for radius search. |
| `_latitude` | `location_latitude=40.7` | Latitude component. |
| `_longitude` | `location_longitude=-74.0` | Longitude component. |
| `_radius` | `location_radius=5` | Search radius (default `1`). |
| `_radius_unit` | `location_radius_unit="km"` | Unit: `m`, `km`, `ft`, or `mi` (default `m`). |
| `_member` | `location_member=instance` | Search around another instance. |
| `_with_distances` | `location_with_distances=True` | Attach `_geo_distance` to results. |

```python
nearby = Restaurant.query.filter(
    location=(40.7128, -74.0060),
    location_radius=5,
    location_radius_unit="km",
    location_with_distances=True,
)
```

### DatetimeField

```python
DatetimeField(auto_now_add: bool = False, auto_now: bool = False, **kwargs)
```

A field that stores `datetime` values with optional auto-timestamping.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `auto_now_add` | `bool` | `False` | Set to current datetime on first save only. |
| `auto_now` | `bool` | `False` | Update to current datetime on every save. |

```python
class Order(Model):
    order_id = AutoKeyField()
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)
```

### Relationship

```python
Relationship(model: Model, null=True, many=False, **kwargs)
```

A field that stores a reference to another model instance. Internally persisted as the related
instance's `redis_key` string. Lazy-loaded on access to prevent infinite recursion with circular
references.

See [Relationships](relationship.md) for detailed usage.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | `Model` | `None` | The related model class (required). |
| `null` | `bool` | `True` | Allow `None` (no relationship). |
| `many` | `bool` | `False` | Reserved for future many-to-many support. |

A field value can be one of three types at runtime:

- **`Model` instance** -- fully loaded relationship.
- **`str`** -- a `redis_key` (lazy-loaded, not yet resolved).
- **`None`** -- no relationship set.

```python
class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship(model=Customer)
    driver = Relationship(model=Driver, null=True)
```

Filter through relationships using double-underscore syntax:

```python
orders = Order.query.filter(customer=some_customer)
orders = Order.query.filter(customer__username="alice")
```

### Typed Shortcut Fields

These convenience fields set the `type` parameter automatically.

| Class | Stored Type | Notes |
|-------|------------|-------|
| `IntField` | `int` | |
| `FloatField` | `float` | |
| `DecimalField` | `Decimal` | |
| `StringField` | `str` | Same as base `Field`. |
| `BooleanField` | `bool` | |
| `BytesField` | `bytes` | |
| `ListField` | `list` | Supports `max_length=N` for capped lists with `push()`. |
| `DictField` | `dict` | |
| `SetField` | `set` | |
| `TupleField` | `tuple` | |
| `DateField` | `datetime.date` | |
| `TimeField` | `datetime.time` | |

All accept the same keyword arguments as `Field` (except `type`, which is preset).

### ExistenceFilter

```python
from popoto.fields.existence_filter import ExistenceFilter

ExistenceFilter(error_rate=0.01, capacity=100_000, fingerprint_fn=None, **kwargs)
```

Bloom filter for O(1) probabilistic membership checks. A "side-effect field" that maintains a Bloom
filter index in Redis via `on_save()` hooks. Does not store a value on the model instance.

See [ExistenceFilter feature docs](features/existence-filter.md) for architecture and tokenization details.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `error_rate` | `float` | `0.01` | Target false positive rate (1%). |
| `capacity` | `int` | `100_000` | Expected number of distinct items. |
| `fingerprint_fn` | `callable` | `None` | Takes a model instance, returns a fingerprint string. Required. |

#### ExistenceFilter.might\_exist()

```python
ExistenceFilter.might_exist(model_class, fingerprint) -> bool
```

Check if a fingerprint might exist in the Bloom filter. Returns `True` if possibly present
(may be a false positive), `False` if definitely absent (guaranteed correct). The query is
tokenized using the same rules as `on_save()`.

#### ExistenceFilter.definitely\_missing()

```python
ExistenceFilter.definitely_missing(model_class, fingerprint) -> bool
```

Convenience inverse of `might_exist()`. Returns `True` when the caller can safely skip
expensive retrieval.

#### ExistenceFilter.might\_exist\_batch()

```python
ExistenceFilter.might_exist_batch(model_class, fingerprints) -> dict[str, bool]
```

Check multiple fingerprints against the Bloom filter in a single Redis round-trip. Each
fingerprint is tokenized; a fingerprint is a hit if ANY of its tokens is found. Uses a
single Lua EVAL for all tokens, avoiding per-keyword network overhead.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class to check against. |
| `fingerprints` | `list[str]` | List of fingerprint strings to check. |

**Returns:** `dict[str, bool]` mapping each fingerprint to its result. Duplicates are deduplicated.
Returns empty dict for empty input.

```python
# Check many keywords in one round-trip instead of N separate calls
results = Memory.bloom.might_exist_batch(Memory, ["kubernetes", "deployment", "postgres"])
# {"kubernetes": True, "deployment": True, "postgres": False}

# Use as a pre-filter before expensive queries
candidates = [kw for kw, hit in results.items() if hit]
```

#### ExistenceFilter.might\_exist\_count()

```python
ExistenceFilter.might_exist_count(model_class, fingerprints) -> int
```

Count how many fingerprints might exist in the Bloom filter. Convenience wrapper around
`might_exist_batch()` that returns just the hit count.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class to check against. |
| `fingerprints` | `list[str]` | List of fingerprint strings to check. |

**Returns:** `int` -- number of fingerprints that might exist.

```python
# Quick selectivity check: how many of these keywords are known?
hit_count = Memory.bloom.might_exist_count(Memory, ["kubernetes", "deployment", "postgres"])
if hit_count == 0:
    return []  # Skip query entirely -- nothing relevant in the corpus
```

#### ExistenceFilter.fill\_ratio()

```python
ExistenceFilter.fill_ratio(model_class) -> float
```

Diagnostic: proportion of set bits in the Bloom filter (0.0 to 1.0). When `fill_ratio`
approaches 0.5, the false positive rate degrades beyond the configured `error_rate`.

### BM25Field

```python
from popoto.fields.bm25_field import BM25Field

BM25Field(source=None, **kwargs)
```

BM25 ranked keyword search field backed by Redis sorted sets. A "side-effect field" that reads
content from a `source` field and maintains an inverted index and corpus statistics via
`on_save()`/`on_delete()` hooks.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source` | `str` | `None` | Name of the field to read content from. Required. |

#### BM25Field.search()

```python
@classmethod
BM25Field.search(model_class, field_name, query_text, limit=10) -> list[tuple[str, float]]
```

Run a BM25-ranked keyword search. Returns a list of `(redis_key, bm25_score)` tuples
ordered by relevance.

#### BM25Field.get\_idf()

```python
@classmethod
BM25Field.get_idf(model_class, field_name, tokens) -> dict[str, float]
```

Get IDF (inverse document frequency) scores for tokens without running a full search. Reads
document frequency from the existing BM25 sorted set and computes standard BM25 IDF:
`idf = log((N - df + 0.5) / (df + 0.5) + 1)`.

Uses `ZMSCORE` (Redis >= 6.2, Valkey compatible) for batch lookup with automatic `ZSCORE`
fallback.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the BM25Field on the model. |
| `tokens` | `str` or `list[str]` | Single token or list of tokens to score. |

**Returns:** `dict[str, float]` mapping each token to its IDF score. Tokens absent from the
corpus get maximum IDF (`log(N + 1)` when df=0). Returns `{token: 0.0}` for all tokens when
the corpus is empty.

```python
# Lightweight selectivity signal -- no full search needed
idf_scores = BM25Field.get_idf(Memory, "content", ["kubernetes", "the", "deployment"])
# {"kubernetes": 4.2, "the": 0.1, "deployment": 2.8}

# Use IDF to rank query terms by selectivity before searching
selective_terms = sorted(idf_scores, key=idf_scores.get, reverse=True)
```

---

## PubSub

Popoto provides an abstract publisher/subscriber system built on Redis pub/sub. Messages are
serialized with msgpack. See [Pub/Sub](pubsub.md) for usage patterns.

### Publisher

```python
class Publisher(ABC)
```

Abstract base class for publishing msgpack-encoded messages to Redis channels. Subclass and call
`publish()` to send data.

#### Publisher.\_\_init\_\_(\*args, \*\*kwargs)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `channel_name` | `str` | Class name | Override the default channel name. |

#### Publisher.publish()

```python
Publisher.publish(
    data: dict = None,
    channel_name: str = None,
    pipeline: redis.client.Pipeline = None,
)
```

Publish `data` as msgpack to the given (or default) channel.

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `dict` | Payload to publish. Falls back to `_publish_data`. |
| `channel_name` | `str` | Override the default channel for this call. |
| `pipeline` | `redis.client.Pipeline` | Optional Redis pipeline for batching. |

**Returns:** Number of subscribers that received the message (or pipeline when batching).

#### Publisher.channel_name

A read/write property for the channel name. Defaults to the class name.

### Subscriber

```python
class Subscriber(ABC)
```

Abstract base class for consuming messages from Redis pub/sub channels. Set `sub_channel_names`
and override `handle()` to process incoming messages.

#### Subscriber.\_\_init\_\_(\*args, \*\*kwargs)

Subscribes to all channels listed in `sub_channel_names` on initialization.

#### Subscriber.\_\_call\_\_()

```python
subscriber()
```

Poll for the next message. If a message is available, it is deserialized and dispatched to
`pre_handle()` then `handle()`.

#### Subscriber.handle()

```python
Subscriber.handle(channel: str, data, *args, **kwargs)
```

Process an incoming message. Override this in your subclass.

| Parameter | Type | Description |
|-----------|------|-------------|
| `channel` | `str` | The channel name the message arrived on. |
| `data` | any | The deserialized (msgpack-unpacked) message payload. |

#### Subscriber.pre_handle()

```python
Subscriber.pre_handle(channel: str, data, *args, **kwargs)
```

Hook called before `handle()`. Override for logging, filtering, or preprocessing.

#### Subscriber.sub_channel_names

```python
sub_channel_names: list = []
```

Class attribute listing the channel names to subscribe to.

---

## Utility Functions

These functions manage the global Redis connection and global configuration. See
[Configuration](configuration.md) for setup guidance.

### popoto.configure()

```python
popoto.configure(
    embedding_provider=None,
    content_store=None,
    content_path: str = None,
) -> None
```

Set global defaults for `ContentField` and `EmbeddingField`. Call once at application startup.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `embedding_provider` | `AbstractEmbeddingProvider` | `None` | Default provider for EmbeddingField and `semantic_search()`. |
| `content_store` | `AbstractContentStore` | `None` | Default content store for ContentField. |
| `content_path` | `str` | `None` | Base directory for filesystem storage. Overrides `POPOTO_CONTENT_PATH`. |

```python
import popoto
from popoto.embeddings.voyage import VoyageProvider

popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
    content_path="/data/popoto-content",
)
```

### set_REDIS_DB_settings()

```python
set_REDIS_DB_settings(env_partition_name: str = "", *args, **kwargs) -> None
```

Reset the global Redis connection with new settings. All positional and keyword arguments after
`env_partition_name` are passed directly to `redis.Redis()`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `env_partition_name` | `str` | Optional namespace prefix. Falls back to the `ENV` environment variable. |
| `*args, **kwargs` | | Passed to `redis.Redis()`. |

```python
from popoto.redis_db import set_REDIS_DB_settings

set_REDIS_DB_settings(host="redis.example.com", port=6380, db=1)
```

### get_REDIS_DB()

```python
get_REDIS_DB() -> redis.Redis
```

Return the current global Redis connection instance.

```python
from popoto.redis_db import get_REDIS_DB

r = get_REDIS_DB()
r.ping()
# => True
```

### check_connection()

```python
check_connection() -> bool
```

Ping Redis and return `True` if the connection is healthy, `False` otherwise. Useful for
health check endpoints in web applications and load balancer probes.

```python
from popoto.redis_db import check_connection

if check_connection():
    print("Redis is healthy")
else:
    print("Redis is unreachable")
```

### scan_keys()

```python
scan_keys(pattern: str, count: int = 1000) -> list
```

Non-blocking replacement for Redis `KEYS` using cursor-based `SCAN`. The `KEYS` command blocks
the entire Redis server while scanning, which causes timeouts at scale. `SCAN` iterates
incrementally, allowing other operations to interleave.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pattern` | `str` | | Glob-style pattern to match keys (e.g., `"User:*"`). |
| `count` | `int` | `1000` | Hint for keys per iteration. Higher values reduce round-trips. |

**Returns:** `list` of all matching keys.

```python
from popoto.redis_db import scan_keys

user_keys = scan_keys("User:*")
active_keys = scan_keys("*:active")
```

!!! warning
    Used internally by Popoto's query system. Most users should use `Model.query` methods
    instead of calling `scan_keys()` directly.

### print_redis_info()

```python
print_redis_info() -> None
```

Log Redis server info and memory usage to the `POPOTO-REDIS_DB` logger. Useful for debugging
connection issues or monitoring memory consumption.

### enable_error_reporting()

```python
enable_error_reporting(dsn: Optional[str] = None) -> None
```

Enable opt-in error reporting for Popoto-specific exceptions. When enabled, exceptions such as
`ModelException` and `QueryException` are automatically reported to the Popoto maintainers via
an isolated Sentry client. Requires the `monitoring` extra (`pip install popoto[monitoring]`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `dsn` | `str` | Optional Sentry DSN. Falls back to the `POPOTO_SENTRY_DSN` environment variable. |

The reporter uses an isolated `sentry_sdk.Client` and `Scope` — it never calls `sentry_sdk.init()`
and never interferes with the application's own Sentry configuration. If `sentry-sdk` is not
installed or no DSN is available, this function silently does nothing.

```python
import popoto

popoto.enable_error_reporting()
```

See [Configuration — Error Reporting](configuration.md#error-reporting-opt-in) for full details.

---

## Testing

Popoto includes a pytest plugin that automatically isolates tests in a dedicated Redis DB.

### Pytest Plugin (auto-registered)

The `popoto.pytest_plugin` module is registered as a [pytest11 entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others) and loads automatically when Popoto is installed. No configuration is required.

**What the plugin does:**

- Switches all Redis operations to DB 15 (or a configured DB) for the test session
- Runs `flushdb()` before each test for a clean slate
- Resets the async Redis connection per test to avoid event-loop conflicts

**Configuration priority** (highest to lowest):

1. `POPOTO_TEST_DB` environment variable
2. `popoto_test_db` ini option in `pyproject.toml` `[tool.pytest.ini_options]`
3. Default: `15`

DB 0 is rejected to prevent accidental test runs against production data. Non-integer values produce a clear error message.

```ini
# pyproject.toml
[tool.pytest.ini_options]
popoto_test_db = "14"
```

**Disabling the plugin:**

```bash
pytest -p no:popoto
```

### Manual Test Helpers

The `popoto.testing` module provides helpers for non-pytest test runners or manual use:

```python
from popoto.testing import use_test_db, flush_test_db

use_test_db(db=15)   # Switch to test DB
flush_test_db()      # Clear the test DB
```

These are not needed when using the pytest plugin, which handles both automatically.

---

## Exceptions

### ModelException

```python
class ModelException(Exception)
```

Raised when a model operation fails: validation errors, save failures, unique constraint violations,
delete or load errors. Defined in `popoto.exceptions` and importable from the main namespace.
Automatically reported when [error reporting](configuration.md#error-reporting-opt-in) is enabled.

```python
from popoto import ModelException
```

### KeyMutationError

```python
class KeyMutationError(ModelException)
```

Raised when a `KeyField` value is changed after initial save and `save()` is called without
`migrate_key=True`. This prevents accidental identity changes that could orphan references.

```python
from popoto import KeyMutationError

instance = MyModel.query.get(name="old_name")
instance.name = "new_name"

try:
    instance.save()  # Raises KeyMutationError
except KeyMutationError:
    instance.save(migrate_key=True)  # Intentional migration succeeds
```

### QueryException

```python
class QueryException(Exception)
```

Raised when a query is malformed or produces an unexpected result (e.g., invalid filter parameters,
`get()` returning multiple results). Defined in `popoto.models.query`.
Automatically reported when [error reporting](configuration.md#error-reporting-opt-in) is enabled.

### PublisherException

```python
class PublisherException(Exception)
```

Raised when a publish operation fails (e.g., missing channel name). Defined in
`popoto.pubsub.publisher`.
Automatically reported when [error reporting](configuration.md#error-reporting-opt-in) is enabled.

### SubscriberException

```python
class SubscriberException(Exception)
```

Raised when a subscriber's message handler fails. Defined in `popoto.pubsub.subscriber`.
Automatically reported when [error reporting](configuration.md#error-reporting-opt-in) is enabled.

### PopotoException

```python
class PopotoException(Exception)
```

Base exception for Popoto framework errors. Logs the error message on initialization. Defined in
`popoto.redis_db`.

