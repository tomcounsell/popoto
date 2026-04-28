# Async Operations

Food delivery apps handle many simultaneous requests: customers browsing menus, drivers
updating locations, orders streaming in. Blocking on each Redis call wastes time your
users do not have. Popoto provides async counterparts for every Model and Query method so
you can serve all of these requests concurrently without blocking the event loop.

## Async Model Methods

Every synchronous model operation has an `async_` prefixed equivalent. These methods
behave identically to their sync counterparts but return awaitables, letting you use
them inside `async def` functions.

### async_create()

Create and persist a new model instance in a single step.

```python
import asyncio
from popoto import (
    Model, KeyField, AutoKeyField, UniqueKeyField,
    Field, SortedField, GeoField, Relationship, DatetimeField,
)

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

class Customer(Model):
    username = KeyField()
    email = UniqueKeyField()
    name = Field(type=str)
    address = GeoField()

class Driver(Model):
    driver_id = AutoKeyField()
    name = Field(type=str)
    phone = UniqueKeyField()
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship(Customer)
    restaurant = Relationship(Restaurant)
    driver = Relationship(Driver, null=True)
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days

async def main():
    restaurant = await Restaurant.async_create(
        name="Siam Garden",
        cuisine="Thai",
        rating=4.7,
        location=(40.748, -73.985),
    )
    print(restaurant.name)
    # => "Siam Garden"

asyncio.run(main())
```

### async_save()

Save changes to an existing instance. This is useful when you need to update fields
after the initial creation.

```python
async def update_restaurant():
    restaurant = await Restaurant.async_create(
        name="Bella Napoli",
        cuisine="Italian",
        rating=4.3,
        location=(40.750, -73.990),
    )

    # Received a new review, bump the rating
    restaurant.rating = 4.5
    await restaurant.async_save()

    print(restaurant.rating)
    # => 4.5
```

### async_delete()

Remove an instance and all of its associated indexes from Redis.

```python
async def close_restaurant():
    restaurant = await Restaurant.query.async_get(name="Bella Napoli")
    deleted = await restaurant.async_delete()
    print(deleted)
    # => True
```

### async_load()

Load a single instance by its key fields. Returns `None` if the object does not exist.

```python
async def load_restaurant():
    restaurant = await Restaurant.async_load(name="Siam Garden")
    if restaurant:
        print(f"{restaurant.name} serves {restaurant.cuisine}")
        # => "Siam Garden serves Thai"
```

!!! note
    `async_load()` performs a direct key lookup, making it the fastest way to retrieve
    a single object when you already know the key values.

## Async Query Methods

The `query` manager exposes async versions of every query method. Use them exactly as
you would the synchronous API, but with `await`.

### async_get()

Retrieve a single instance by key fields or filtered criteria.

```python
async def find_customer():
    customer = await Customer.query.async_get(username="foodie42")
    if customer:
        print(customer.email)
```

### async_filter()

Return a list of instances matching the given criteria. Supports the same lookups as
the synchronous `filter()` -- see [Making Queries](query.md) for the full list.

```python
async def find_expensive_orders():
    big_orders = await Order.query.async_filter(total__gte=50.0)
    for order in big_orders:
        print(f"Order {order.order_id}: ${order.total}")
```

### async_all()

Retrieve every instance of a model.

```python
async def list_restaurants():
    restaurants = await Restaurant.query.async_all()
    print(f"Total restaurants: {len(restaurants)}")
```

!!! warning
    `async_all()` loads every instance into memory. For models with many records, prefer
    `async_filter()` with appropriate constraints.

### async_count()

Count instances without loading full objects. Accepts the same filter arguments as
`async_filter()`.

```python
async def dashboard_stats():
    total_orders = await Order.query.async_count()
    big_orders = await Order.query.async_count(total__gte=50.0)
    print(f"{big_orders} of {total_orders} orders are over $50")
```

### async_keys()

Return a list of Redis keys matching the model, without deserializing objects.

```python
async def audit_keys():
    keys = await Order.query.async_keys()
    print(f"Order keys in Redis: {len(keys)}")
```

### async_get_many()

Retrieve multiple instances by Redis key in a single async pipeline. This is the async
counterpart of `query.get_many()` -- see [Making Queries](query.md#get-multiple-objects-by-key)
for full details on ordering and `skip_none` behavior.

```python
async def bulk_lookup():
    keys = ["Restaurant:Siam Garden", "Restaurant:Bella Napoli", "Restaurant:Gone"]
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys)
    # => [<Restaurant>, <Restaurant>, None]

    # Drop missing entries
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys, skip_none=True)
    # => [<Restaurant>, <Restaurant>]
```

!!! tip
    `async_get_many()` uses a native async Redis pipeline, so it does not block the event
    loop even when hydrating hundreds of keys.

## Concurrent Operations

The real power of async shows up when you need to perform independent operations at the
same time. Use `asyncio.gather()` to run them concurrently instead of sequentially.

A customer opens the app and expects to see nearby restaurants and available drivers on
the same screen. With sync code you would wait for one query, then the other. With
async, both queries run at the same time.

```python
async def home_screen(customer_username: str):
    customer = await Customer.async_load(username=customer_username)

    nearby_restaurants, available_drivers = await asyncio.gather(
        Restaurant.query.async_filter(
            location=customer.address,
            location_radius=10,
            location_radius_unit='km',
        ),
        Driver.query.async_filter(
            location=customer.address,
            location_radius=5,
            location_radius_unit='km',
        ),
    )

    print(f"{len(nearby_restaurants)} restaurants nearby")
    print(f"{len(available_drivers)} drivers available")
```

You can also create several records at once without waiting for each one individually.

```python
async def seed_restaurants():
    restaurants = await asyncio.gather(
        Restaurant.async_create(
            name="Siam Garden", cuisine="Thai",
            rating=4.7, location=(-73.985, 40.748),
        ),
        Restaurant.async_create(
            name="Bella Napoli", cuisine="Italian",
            rating=4.3, location=(-73.990, 40.750),
        ),
        Restaurant.async_create(
            name="Tokyo Bowl", cuisine="Japanese",
            rating=4.8, location=(40.752, -73.978),
        ),
    )
    print(f"Created {len(restaurants)} restaurants")
    # => "Created 3 restaurants"
```

!!! tip
    `asyncio.gather()` is most beneficial when each individual operation involves
    network latency. When talking to a remote Redis instance the time savings add up
    quickly.

## Framework Integration: FastAPI

FastAPI is a natural fit because its route handlers are already async. Below is a
minimal food delivery API with two endpoints.

```python
from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.post("/orders")
async def create_order(
    customer_username: str,
    restaurant_name: str,
    total: float,
):
    customer, restaurant = await asyncio.gather(
        Customer.async_load(username=customer_username),
        Restaurant.async_load(name=restaurant_name),
    )
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    order = await Order.async_create(
        customer=customer,
        restaurant=restaurant,
        total=total,
    )
    return {
        "order_id": order.order_id,
        "status": order.status,
        "total": order.total,
    }

@app.get("/restaurants/nearby")
async def nearby_restaurants(lat: float, lon: float, radius_km: float = 5):
    restaurants = await Restaurant.query.async_filter(
        location=(lat, lon),
        location_radius=radius_km,
        location_radius_unit='km',
    )
    return [
        {
            "name": r.name,
            "cuisine": r.cuisine,
            "rating": r.rating,
        }
        for r in restaurants
    ]
```

!!! note
    Because Popoto's async methods are awaitable, FastAPI can handle many incoming
    requests without any of them blocking on Redis I/O.

## Framework Integration: Background Workers

Long-running order processing tasks can run as async background workers. The pattern
below polls for recent orders, assigns a driver, and updates the status.

```python
async def order_processing_worker():
    """Continuously process recent orders."""
    while True:
        recent_orders = await Order.query.async_filter(
            total__gte=0.0,
            limit=10,
        )

        for order in recent_orders:
            driver = await find_nearest_driver(order)
            if driver:
                order.driver = driver
                order.status = "assigned"
                driver.active = False
                await asyncio.gather(
                    order.async_save(),
                    driver.async_save(),
                )
                print(f"Order {order.order_id} assigned to {driver.name}")

        await asyncio.sleep(2)

async def find_nearest_driver(order):
    """Find the closest active driver to the restaurant."""
    restaurant = order.restaurant
    if isinstance(restaurant, str):
        restaurant = await Restaurant.async_load(name=restaurant)

    drivers = await Driver.query.async_filter(
        location=restaurant.location,
        location_radius=10,
        location_radius_unit='km',
        order_by="-rating",
        limit=1,
    )
    return drivers[0] if drivers else None
```

!!! tip
    In production you would run this worker alongside your web server using
    `asyncio.create_task()` or a task runner like `arq` or `taskiq`.

## Implementation Details

### Native Async with redis.asyncio

All async methods use `redis.asyncio` for true non-blocking I/O. This approach
provides three key properties:

- **No event loop blocking** -- Redis calls use native async I/O so your event
  loop stays free to handle other coroutines.
- **No thread pool overhead** -- Operations run directly on the event loop without
  spawning worker threads.
- **Identical behavior** -- Validation, serialization, error handling, and Redis
  commands work exactly the same as the synchronous path.

## Performance Considerations

Async does not make individual Redis commands faster. It makes your **application**
faster by letting other work proceed while waiting for Redis responses.

When async helps the most:

- **Remote Redis instances** -- network round-trips of 1-5 ms benefit significantly
  from concurrency. Three sequential 3 ms calls take 9 ms; with `gather()` they
  complete in roughly 3 ms.
- **High-concurrency web servers** -- frameworks like FastAPI can serve other requests
  while one request awaits Redis.
- **Fan-out queries** -- searching restaurants, drivers, and orders at the same time
  (as shown in the concurrent operations section above).

When async adds little benefit:

- **Local Redis, single operation** -- a sub-millisecond `GET` on localhost is already
  fast. The thread pool overhead is negligible but the async machinery does not speed
  it up either.
- **CPU-bound processing** -- if most time is spent computing rather than waiting on
  I/O, async will not help. Consider `multiprocessing` instead.

!!! tip
    Profile before optimizing. If your Redis calls are already sub-millisecond on
    localhost, switching from sync to async will not produce a noticeable improvement
    for single operations. Focus on `gather()` for parallel queries first.

## Error Handling

Async methods raise the same exceptions as their synchronous equivalents. Wrap calls in
standard `try`/`except` blocks.

```python
from popoto.exceptions import ModelException
from popoto.exceptions import QueryException

async def safe_order_create(customer_username, restaurant_name, total):
    try:
        customer = await Customer.async_load(username=customer_username)
        restaurant = await Restaurant.async_load(name=restaurant_name)

        order = await Order.async_create(
            customer=customer,
            restaurant=restaurant,
            total=total,
        )
        return order

    except ModelException as e:
        print(f"Model error: {e}")
        return None
    except QueryException as e:
        print(f"Query error: {e}")
        return None
```

You can also handle errors inside `asyncio.gather()` by passing
`return_exceptions=True`. This prevents one failure from cancelling sibling tasks.

```python
async def resilient_lookups():
    results = await asyncio.gather(
        Restaurant.async_load(name="Siam Garden"),
        Restaurant.async_load(name="Nonexistent Place"),
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            print(f"Lookup failed: {result}")
        elif result is None:
            print("Restaurant not found")
        else:
            print(f"Found: {result.name}")
```

!!! warning
    When using `return_exceptions=True`, remember to check each result for exceptions
    before accessing model attributes.

## Async Bulk Operations

Popoto also provides async versions of all bulk operations. These methods are ideal for
importing large datasets, batch updates, and cleanup tasks without blocking the event loop.

| Sync | Async |
|------|-------|
| `Model.bulk_create(instances)` | `await Model.async_bulk_create(instances)` |
| `Model.bulk_update(queryset, **updates)` | `await Model.async_bulk_update(queryset, **updates)` |
| `Model.bulk_delete(queryset)` | `await Model.async_bulk_delete(queryset)` |

```python
async def import_restaurants(data: list[dict]):
    """Bulk import restaurants from external data."""
    instances = [
        Restaurant(
            name=item["name"],
            cuisine=item["cuisine"],
            rating=item["rating"],
        )
        for item in data
    ]

    created = await Restaurant.async_bulk_create(instances)
    print(f"Imported {len(created)} restaurants")
    return created

async def feature_top_restaurants():
    """Mark highly-rated restaurants as featured."""
    count = await Restaurant.async_bulk_update(
        Restaurant.query.filter(rating__gte=4.5),
        is_featured=True
    )
    print(f"Featured {count} top restaurants")

async def cleanup_inactive():
    """Remove inactive restaurants."""
    count = await Restaurant.async_bulk_delete(
        Restaurant.query.filter(status="inactive")
    )
    print(f"Deleted {count} inactive restaurants")
```

All bulk methods use Redis pipelines internally to batch operations, dramatically reducing
network round-trips. The `batch_size` parameter (default 1000) controls how many instances
are processed per pipeline execution.

See [Bulk Operations](recipes.md#bulk-operations) in the Recipes for complete
documentation.

## Test Coverage

The async API is thoroughly tested across four test files covering all public methods
and edge cases. Each scenario listed below has at least one dedicated test.

| Category | Tested Scenarios |
|----------|-----------------|
| **CRUD basics** | `async_create`, `async_save`, `async_delete`, `async_load`, `async_get` |
| **Query methods** | `async_filter`, `async_all`, `async_count`, `async_keys`, `async_get_many` |
| **SortedField queries** | `__lte`, `__lt`, `__gt`, `__gte`, range (between), `limit`, `order_by` |
| **GeoField queries** | Radius search via `async_filter` with coordinates and distance units |
| **Relationship fields** | Lazy-loading related objects through async queries |
| **Projection** | `values=` parameter with `async_filter` and `async_all` |
| **Error paths** | `async_get` returns `None` on miss; raises `QueryException` on multiple matches |
| **Bulk operations** | `async_bulk_create`, `async_bulk_update`, `async_bulk_delete` |
| **Connection management** | `async_check_connection` (success, failure, timeout), `set_async_redis_db_settings` |
| **Key scanning** | `async_scan_keys` pattern matching, `async_keys` with `catchall` and `clean` flags |
| **Client-side filtering** | Filtering on plain `Field` (non-indexed) via `async_filter` |
| **Meta options** | `Meta.order_by` default sorting, `Meta.ttl` automatic expiration |
| **Atomicity** | Pipeline-based `async_save`, concurrent writes (last-write-wins) |
| **Index maintenance** | `async_check_indexes`, `async_clean_indexes`, `async_rebuild_indexes` via `to_thread` |

Run the async tests with:

```bash
pytest tests/test_async.py tests/test_connection.py tests/test_bulk_operations.py tests/test_migrations.py -v
```

## See Also

- [Models and Fields](fields.md) -- define your data models
- [Making Queries](query.md) -- query patterns and filter lookups
- [Model Meta Options](meta.md) -- configure `order_by`, `ttl`, and other model behavior
- [Bulk Operations](recipes.md#bulk-operations) -- efficient batch processing
