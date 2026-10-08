# Model Meta Options

Every Popoto model can include a `Meta` inner class to configure model-level
behavior like the storage backend, default ordering, automatic expiration, and
composite indexes.
The `Meta` class is processed at class definition time, and its options
become available via `ModelClass._meta`. `Meta.backend` also chooses where the
model is stored: Redis/Valkey (the default) or PostgreSQL.

## When to Use Meta Options

You should define a `Meta` class when you want to:

- **Set default ordering** for query results without specifying `order_by`
  every time
- **Store a model in Postgres** instead of Redis
- **Automatically expire data** with a TTL (great for temporary orders,
  sessions, or cached data)
- **Enforce uniqueness** across multiple fields (composite unique constraints)
- **Store the model on PostgreSQL** instead of Redis (`backend = "postgres"`)

Without a `Meta` class, your models work fine -- you just configure behavior
at query time instead.

## backend

`backend` chooses where the model's records are stored: `"redis"` or
`"postgres"`.

```python
class Note(Model):
    owner = KeyField()
    slug = KeyField()
    body = Field(type=str)

    class Meta:
        backend = "postgres"
```

Without `backend`, the model uses the process default. That default is
`POPOTO_BACKEND` when it is set, else `"redis"`; it can also be changed at runtime
with `popoto.backends.set_backend()`. An explicit `Meta.backend` always wins over the
process default. A Postgres instance passed to `set_backend()` also serves models that
declare `Meta.backend = "postgres"`, ahead of `POPOTO_POSTGRES_URL`; a model that declares
`Meta.backend = "redis"` always gets the stock Redis backend.

What to know:

- **Any other value is an error at class definition.** `backend = "mysql"` raises
  `ModelException: Meta.backend must be one of redis, postgres, got 'mysql'`.
- **The fields are checked when the class is defined.** With
  `backend = "postgres"`, a field Postgres cannot store raises
  `BackendCapabilityError` (from `popoto.backends`) at class definition. A model
  that gets Postgres from the process default is checked on its first query or
  save instead. See [Models and Fields](fields.md#on-postgres) for what is
  supported.
- **Defining the class never connects.** The connection, the version check, and the
  table creation all happen on the model's first query or save. The DSN comes from
  `POPOTO_POSTGRES_URL`, and the `postgres` extra must be installed
  (`pip install 'popoto[postgres]'`). If either is missing, that first use raises
  `BackendUnavailableError`.
- **It is not inherited.** `backend` is read from the class's own `Meta`, so a
  subclass of an abstract model that sets `backend = "postgres"` does not get it.
  Set `backend` on each concrete model.

The read value is `Model._meta.backend`, which is `None` when the model takes the
process default. The rest of the setup (environment variables, schema, roles,
outages) is in [Selecting the backend](features/postgres-backend.md#selecting-the-backend).

## order_by

The `order_by` option sets a default sort order for all queries. This is
useful when you almost always want results in the same order -- for example,
showing the most recent orders first.

```python
from popoto import Model, AutoKeyField, Field, SortedField
from popoto import Relationship, DatetimeField

class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship("Customer")
    restaurant = Relationship("Restaurant")
    driver = Relationship("Driver", null=True)
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"
```

The minus sign prefix (`-`) means descending order. Without it, results are
ascending. In this case, the newest orders always appear first.

Queries automatically respect the default ordering:

```python
orders = Order.query.all()
print(orders[0].status)
# => "pending"  (the most recently created order)
```

### Override per Query

You can override the default at query time:

```python
orders = Order.query.all(order_by="total")
# => Returns orders sorted by total ascending, ignoring Meta default

expensive_first = Order.query.all(order_by="-total")
# => Returns orders sorted by total descending
```

!!! note
    The field specified in `order_by` must exist on the model. Popoto
    validates this at class definition time and raises a `ModelException`
    if the field does not exist.

## ttl

!!! tip
    For a dedicated guide covering model-level TTL, per-instance overrides,
    absolute expiration, and a complete session example, see the
    [TTL documentation](ttl.md).

The `ttl` (time-to-live) option deletes model instances automatically after a
specified number of seconds -- ideal for completed orders, delivery tracking
records, or temporary promotions. On Redis, saving a model with a TTL calls
`EXPIRE` on the key, and Redis removes the key after that many seconds. On
Postgres, the row gets an expiry time instead (see [On Postgres](#on-postgres)).

Adding `ttl` to the Order model from the previous section:

```python
    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days (30 * 24 * 60 * 60)
```

Every order instance now expires 30 days after its most recent save:

```python
order = Order.create(customer=alice, restaurant=sakura, total=29.50)
# => The order is deleted automatically after 30 days
```

The TTL resets every time you call `save()`. Updating an order 15 days in
restarts the 30-day clock:

```python
order.status = "delivered"
order.save()
# => TTL resets to 30 days from now
```

When a record expires, it is gone silently. Any subsequent `load()` or
`query.get()` call returns `None`. Popoto handles orphaned secondary index
entries gracefully during queries.

!!! warning
    TTL is refreshed on every `save()` call, not just on creation. Background
    processes that update records will extend the expiration window.

## Instance-Level TTL

You can override the Meta TTL for specific instances using the `_ttl`
attribute. For example, a rush order might need a shorter retention period:

```python
rush_order = Order(
    customer=alice, restaurant=sakura, total=49.99, status="rush"
)
rush_order._ttl = 604800  # 7 days instead of the default 30
rush_order.save()
```

To make a specific instance permanent (no expiration), set `_ttl` to `None`:

```python
vip_order = Order(
    customer=alice, restaurant=sakura, total=199.99, status="vip"
)
vip_order._ttl = None  # Never expires, even though Meta.ttl is set
vip_order.save()
```

!!! note
    The instance-level `_ttl` takes precedence over `Meta.ttl`. Setting it
    to `None` disables expiration for that instance only.

## Absolute Expiration

Instead of a relative TTL, you can set an absolute expiration with
`_expire_at`. This accepts a `datetime`, and the record expires at that exact
moment (`EXPIREAT` on Redis).

```python
from datetime import datetime, timedelta

order = Order(
    customer=alice, restaurant=sakura, total=35.00, status="scheduled"
)
order._expire_at = datetime(2026, 3, 1, 0, 0, 0)  # Expires March 1st
order.save()
```

You can also compute the deadline dynamically:

```python
end_of_day = datetime.now().replace(hour=23, minute=59, second=59)
order._expire_at = end_of_day
order.save()
# => Order expires at the end of the current day
```

!!! warning
    You cannot set both `_ttl` and `_expire_at` on the same instance. Popoto
    raises a `ModelException` if both are set. Use one or the other.

## indexes

The `indexes` option creates composite indexes that can enforce uniqueness
across combinations of fields. Each index is a tuple of `(field_names,
is_unique)` where `field_names` is a tuple of strings and `is_unique` is
a boolean:

Adding `indexes` to the Order model alongside the other Meta options:

```python
    class Meta:
        order_by = "-created_at"
        ttl = 2592000
        indexes = (
            (("restaurant", "status"), True),  # Unique composite index
        )
```

With this index, each restaurant can only have one order per status value:

```python
order1 = Order.create(
    customer=alice, restaurant=sakura, total=25.00, status="pending"
)
# => Saved successfully

order2 = Order.create(
    customer=bob, restaurant=sakura, total=18.00, status="pending"
)
# => ModelException: Unique index violation on ('restaurant', 'status')
```

A different status for the same restaurant works fine:

```python
order3 = Order.create(
    customer=bob, restaurant=sakura, total=18.00, status="preparing"
)
# => Saved successfully
```

### Non-Unique Indexes

Set the uniqueness flag to `False` to create an index for grouping without
a uniqueness constraint:

```python
class Meta:
    indexes = (
        (("restaurant", "status"), False),
    )
```

### NULL Handling

Following SQL standard behavior, `NULL` values do not participate in
uniqueness checks. Multiple instances can have `NULL` in an indexed field
and both will save successfully.

## Update and Delete Handling

Popoto maintains composite indexes automatically. Updates that would violate
a unique index are rejected before the save occurs:

```python
order1 = Order.create(
    customer=alice, restaurant=sakura, total=25.00, status="pending"
)
order2 = Order.create(
    customer=bob, restaurant=sakura, total=18.00, status="preparing"
)

order2.status = "pending"  # Would collide with order1
order2.save()
# => ModelException: Unique index violation on ('restaurant', 'status')
```

When you delete an instance, its index entries are cleaned up, freeing the
slot for future records:

```python
order1.delete()

order3 = Order.create(
    customer=charlie, restaurant=sakura, total=22.00, status="pending"
)
# => Saved successfully -- the slot was freed by deleting order1
```

## backend

`Meta.backend` picks the storage backend for one model: `"redis"` or
`"postgres"`. Leave it out and the model takes the process default, which is
`POPOTO_BACKEND` (or `popoto.backends.set_backend(...)`), else `"redis"`.

```python
from popoto import Model, KeyField, IntField

class Note(Model):
    owner = KeyField()
    slug = KeyField()
    hits = IntField(default=0)

    class Meta:
        backend = "postgres"
```

A Postgres-bound model needs `pip install 'popoto[postgres]'` and
`POPOTO_POSTGRES_URL`. Declaring it never opens a connection. Popoto checks
the fields against the backend's capability table at class creation and
raises `BackendCapabilityError` for a field Postgres cannot store. The
connection and the table's DDL wait for the model's first query or save.

The environment variables that configure Postgres:

| Variable | Default | Meaning |
|---|---|---|
| `POPOTO_BACKEND` | `redis` | The process default for models without `Meta.backend`. |
| `POPOTO_POSTGRES_URL` | (none) | The Postgres DSN. Required for Postgres. |
| `POPOTO_POSTGRES_SCHEMA` | `popoto` | The Postgres schema the tables live in. |
| `POPOTO_SCHEMA_AUTO` | `1` | Create missing tables and apply additive changes on first use. `0` runs no DDL. |
| `POPOTO_POSTGRES_MAINTENANCE_URL` | (the main DSN) | A direct DSN for DDL, `REINDEX` and `LISTEN`, when the main one is PgBouncer in transaction mode. |
| `POPOTO_POSTGRES_GRANT_MAIN_ROLE` | off | `1` grants the main role access to tables the maintenance role creates. |

See [Use Postgres](guides/postgres-quickstart.md) for a walkthrough and the
[Postgres backend](features/postgres-backend.md) reference for every
supported field. `Meta.ttl` and `Meta.indexes` work on both backends.

## Complete Example

Here is the Order model combining all three Meta options:

```python
from popoto import Model, AutoKeyField, Field, SortedField
from popoto import Relationship, DatetimeField

class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship("Customer")
    restaurant = Relationship("Restaurant")
    driver = Relationship("Driver", null=True)
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000
        indexes = (
            (("restaurant", "status"), True),
        )
```

This model returns orders newest-first by default, expires them after 30
days, and prevents duplicate restaurant/status combinations.

## Accessing Meta at Runtime

After your model is defined, access Meta options via the `_meta` attribute:

```python
print(Order._meta.order_by)
# => "-created_at"

print(Order._meta.ttl)
# => 2592000

print(Order._meta.indexes)
# => ((("restaurant", "status"), True),)
```

You can also inspect field metadata through `_meta`:

```python
print(Order._meta.sorted_field_names)
# => {"total"}

print(Order._meta.relationship_field_names)
# => {"customer", "restaurant", "driver"}
```

!!! warning
    Do not access `Order.Meta` directly. It is consumed during class creation
    by the metaclass. Always use `Order._meta` instead.

## Meta Validation

All Meta options are validated at class definition time, not at runtime.
You get immediate feedback about configuration errors when Python loads
your model class.

```python
class BadOrder(Model):
    order_id = AutoKeyField()
    total = SortedField(type=float)

    class Meta:
        order_by = "nonexistent_field"
# => ModelException: Meta.order_by references 'nonexistent_field'
#    but this field does not exist on BadOrder
```

TTL and index definitions are also validated:

```python
class BadTTL(Model):
    order_id = AutoKeyField()

    class Meta:
        ttl = -1
# => ModelException: Meta.ttl must be a positive integer (seconds), got -1

class BadIndex(Model):
    order_id = AutoKeyField()
    status = Field(type=str)

    class Meta:
        indexes = (
            (("status", "missing_field"), True),
        )
# => ModelException: Unknown field 'missing_field' in Meta.indexes
#    for BadIndex
```

### Invalid backend

```python
class BadBackend(Model):
    name = KeyField()

    class Meta:
        backend = "mysql"
# => ModelException: Meta.backend must be one of redis, postgres, got 'mysql'
```

!!! tip
    Because validation happens at import time, you will catch configuration
    errors during development rather than at runtime in production. Define
    your Meta options early and run your test suite to verify them.

## On Postgres

Every option on this page works on a Postgres model. How each one maps:

- **`order_by`**: an `ORDER BY` on the column. With no `order_by` at all, results
  come back in key order (`_pk`, compared bytewise) rather than Redis's arbitrary set
  order.
- **`ttl`, `_ttl`, `_expire_at`**: an `_expires_at` column, filtered out of every
  read the instant it passes and deleted in small batches after later writes. See
  [TTL](ttl.md#on-postgres) for the differences, such as `_ttl` on a model without
  `Meta.ttl` being refused.
- **`indexes`**: one composite B-tree per entry, `UNIQUE` when the entry is unique.
  The `ModelException` text on a violation is the same as on Redis.
- **`backend`**: see [backend](#backend) above.

Removing `Meta.ttl` from a model whose table already has the expiry column raises
`SchemaDriftError` on first use; see
[Records and other behaviour](features/postgres-backend.md#records-and-other-behaviour).

See [Models and Fields](fields.md) for field type reference, or
[Making Queries](query.md) for query filtering and ordering options.
