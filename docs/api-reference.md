# API Reference

Complete reference for all public classes, methods, and functions in the Popoto Redis ORM.

```python
from popoto import Model, Field, KeyField, AutoKeyField, UniqueKeyField
from popoto import SortedField, SortedKeyField, GeoField, DatetimeField, Relationship
from popoto import Publisher, Subscriber
from popoto import ModelException, QueryException, PublisherException, SubscriberException
```

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

**Returns:** Redis `HSET` response (int) or pipeline.

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
| `ListField` | `list` | |
| `DictField` | `dict` | |
| `SetField` | `set` | |
| `TupleField` | `tuple` | |
| `DateField` | `datetime.date` | |
| `TimeField` | `datetime.time` | |

All accept the same keyword arguments as `Field` (except `type`, which is preset).

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

These functions manage the global Redis connection. See [Configuration](configuration.md) for setup
guidance.

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

---

## Exceptions

### ModelException

```python
class ModelException(Exception)
```

Raised when a model operation fails: validation errors, save failures, unique constraint violations,
delete or load errors. Defined in `popoto.exceptions` and importable from the main namespace.

```python
from popoto import ModelException
```

### QueryException

```python
class QueryException(Exception)
```

Raised when a query is malformed or produces an unexpected result (e.g., invalid filter parameters,
`get()` returning multiple results). Defined in `popoto.models.query`.

### PublisherException

```python
class PublisherException(Exception)
```

Raised when a publish operation fails (e.g., missing channel name). Defined in
`popoto.pubsub.publisher`.

### SubscriberException

```python
class SubscriberException(Exception)
```

Raised when a subscriber's message handler fails. Defined in `popoto.pubsub.subscriber`.

### PopotoException

```python
class PopotoException(Exception)
```

Base exception for Popoto framework errors. Logs the error message on initialization. Defined in
`popoto.redis_db`.

