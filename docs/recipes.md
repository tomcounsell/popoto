# Recipes

Patterns and walkthroughs for common Popoto operations. Symbol-level reference
documentation lives under [the API Reference](reference/SUMMARY.md) and is
auto-generated from docstrings — this page captures the prose and worked
examples that don't naturally live next to a single symbol.

## Version Introspection

`popoto.__version__` resolves to the installed distribution's version string
via `importlib.metadata` (PEP 566). `pyproject.toml` is the single source of
truth — the package exposes whatever is set in `[project].version`. When
importing from an uninstalled source tree, `__version__` falls back to the
PEP 440-compliant sentinel `"0.0.0+unknown"`.

```python
import popoto
print(popoto.__version__)  # e.g. "1.6.0"
```

No separate `VERSION` file, no static string in `__init__.py` — so there is no
risk of version skew between the code on disk and the version reported at
runtime.

## Bulk Operations

Popoto provides bulk operation methods for efficient batch processing using
Redis pipelines. These methods significantly reduce network round-trips
compared to individual operations, making them ideal for importing data,
batch updates, and cleanup tasks.

### Choosing a Batch Size

All bulk methods accept a `batch_size` parameter (default 1000) that controls
memory usage and pipeline size. When processing more instances than
`batch_size`, operations are automatically split into multiple pipeline
executions.

```python
# Process 10,000 instances in batches of 500
Restaurant.bulk_create(large_list, batch_size=500)
```

**When to adjust batch size:**

- **Increase** for faster throughput when memory is not a concern.
- **Decrease** when instances are large or memory is constrained.
- **Default (1000)** works well for most use cases.

### Async Bulk Methods

All bulk operations have async counterparts that run in a thread pool to
avoid blocking the event loop. See [Async Operations](async.md) for details.

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

### Why `delete_all()` instead of `DEL`/`FLUSHDB`?

**Never delete Popoto data directly with Redis commands like `DEL`,
`FLUSHDB`, or `KEYS ... | xargs redis-cli DEL`.** Popoto maintains secondary
indexes for fast queries:

- **SortedField** → Redis sorted sets for range queries
- **GeoField** → Redis geo sets for location queries
- **UniqueKeyField** → Redis keys for uniqueness constraints
- **Class sets** → Track all instances of each model

If you delete instance keys directly, these indexes become orphaned:

- Range queries return stale results
- Geo queries find deleted locations
- Unique constraints block valid values
- `count()` returns wrong numbers

`delete_all()` properly invokes each instance's `delete()` method, which
triggers all field `on_delete` hooks to clean up indexes. This is the **only
safe way** to bulk-delete Popoto data.

```python
# CORRECT - cleans up all indexes
Restaurant.delete_all()

# WRONG - leaves orphaned indexes
redis_client.delete(*redis_client.keys("Restaurant:*"))
```

### Bulk Operations: Worked Examples

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

## Index Maintenance

Popoto maintains secondary indexes (sorted sets, key field sets, geo indexes,
composite indexes, and the class set) alongside your model data. Over time,
indexes can accumulate orphaned entries — references to instance keys that no
longer exist in Redis. This typically happens after direct Redis deletions,
TTL expirations, or interrupted operations.

The recommended workflow is **diagnose → clean → verify**:

```python
# Step 1: Read-only health check (zero writes)
result = User.check_indexes()
print(f"Found {result['total']} orphaned index entries")

# Step 2: Production-safe surgical cleanup
if result['total'] > 0:
    removed = User.clean_indexes()
    print(f"Cleaned {removed} orphans")

# Step 3: Verify
after = User.check_indexes()
assert after['total'] == 0
```

`check_indexes()` returns a per-index-type breakdown:

```python
{
    'class_set': int,         # absent-hash orphans (EXISTS == 0)
    'partial_writes': int,    # hash exists but missing the AutoKeyField value
    'key_fields': {field_name: int, ...},
    'sorted_fields': {field_name: int, ...},
    'geo_fields': {field_name: int, ...},
    'composite_indexes': {index_key: int, ...},
    'total': int,             # sum of all the above
}
```

### Partial-Write Orphans

For models whose primary key is a single `AutoKeyField`, `check_indexes()`
also detects **partial-write orphans**: hashes that exist in Redis but are
missing the auto-key field value. These appear as ghost rows in
`query.all()` (with `id=None`, `_redis_key=None`) and `instance.delete()`
silently no-ops on them. Common causes are crashed saves and mid-pipeline
process exits.

When `clean_indexes()` encounters a partial-write orphan it removes the
class-set membership AND issues `DEL` on the corrupt hash — the hash is
unrecoverable and must not linger in Redis. Models with composite
`KeyField`s (no single `AutoKeyField`) skip this check; their behavior is
unchanged.

> **Operational guidance:** Do not run `clean_indexes()` during active
> migrations or HDEL-based field migrations. A brief HDEL window on the
> auto-key field can cause healthy hashes to be misclassified as
> partial-write orphans and deleted. Run during low-traffic periods.

### When to Use `rebuild_indexes()` vs `clean_indexes()`

`clean_indexes()` is the right choice for routine maintenance — it surgically
removes only the orphaned entries (SREM, ZREM, HDEL) and leaves valid index
data untouched, so concurrent queries continue to return correct results.

`rebuild_indexes()` deletes all secondary indexes and reconstructs them from
source hash data. Use it as a last resort: for repairing structurally
corrupted indexes, after bulk imports that bypassed normal `save()` hooks, or
when upgrading field types that change index structure. **During the rebuild
window, queries relying on those indexes may return incomplete results.**

### Async Index Maintenance

All three index maintenance methods have async counterparts that use
`asyncio.to_thread` under the hood, keeping the event loop free during
potentially long-running scans.

| Sync | Async |
|------|-------|
| `Model.check_indexes()` | `await Model.async_check_indexes()` |
| `Model.clean_indexes()` | `await Model.async_clean_indexes()` |
| `Model.rebuild_indexes()` | `await Model.async_rebuild_indexes()` |

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

    if results[0]['total'] > 0:
        await User.async_clean_indexes()
    if results[1]['total'] > 0:
        await Restaurant.async_clean_indexes()
    if results[2]['total'] > 0:
        await Order.async_clean_indexes()
```

A live demo is available in the
[Popoto Kitchen example app](https://github.com/tomcounsell/popoto/tree/main/examples)
— run `python -m popoto_kitchen --ops` to see the
`check_indexes()` → `clean_indexes()` workflow across multiple models.

## Instance TTL Attributes

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

## Exceptions: When Each Is Raised

These descriptions complement the auto-generated reference at
[`popoto.exceptions`](reference/popoto/exceptions.md).

- **`ModelException`** — raised when a model operation fails: validation
  errors, save failures, unique constraint violations, delete or load errors.
  Automatically reported when [error reporting](configuration.md#error-reporting-opt-in)
  is enabled.
- **`KeyMutationError`** (subclass of `ModelException`) — raised when a
  `KeyField` value is changed after initial save and `save()` is called
  without `migrate_key=True`. This prevents accidental identity changes that
  could orphan references. Override with `instance.save(migrate_key=True)`
  when you genuinely intend to migrate.
- **`QueryException`** — raised when a query is malformed or produces an
  unexpected result (e.g., invalid filter parameters, `get()` returning
  multiple results).
- **`PublisherException`** — raised when a publish operation fails (e.g.,
  missing channel name).
- **`SubscriberException`** — raised when a subscriber's message handler
  fails.
- **`PopotoException`** — base exception class for Popoto framework errors.
  Logs the error message on initialization.

```python
from popoto import KeyMutationError

instance = MyModel.query.get(name="old_name")
instance.name = "new_name"

try:
    instance.save()  # Raises KeyMutationError
except KeyMutationError:
    instance.save(migrate_key=True)  # Intentional migration succeeds
```
