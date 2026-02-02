# Model Meta Options

Every Popoto model can include a `Meta` inner class to configure model-level
behavior like default ordering, automatic expiration, and composite indexes.
The `Meta` class is processed at class definition time, and its options
become available via `ModelClass._meta`.

## When to Use Meta Options

You should define a `Meta` class when you want to:

- **Set default ordering** for query results without specifying `order_by`
  every time
- **Automatically expire data** using Redis TTL (great for temporary orders,
  sessions, or cached data)
- **Enforce uniqueness** across multiple fields (composite unique constraints)

Without a `Meta` class, your models work fine -- you just configure behavior
at query time instead.

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

The `ttl` (time-to-live) option tells Redis to automatically delete model
instances after a specified number of seconds. When you save a model with a
TTL, Popoto calls Redis's `EXPIRE` command on the key. After that many
seconds, Redis removes it automatically -- ideal for completed orders,
delivery tracking records, or temporary promotions.

Adding `ttl` to the Order model from the previous section:

```python
    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days (30 * 24 * 60 * 60)
```

Every order instance now expires 30 days after its most recent save:

```python
order = Order.create(customer=alice, restaurant=sakura, total=29.50)
# => Redis will automatically delete this order after 30 days
```

The TTL resets every time you call `save()`. Updating an order 15 days in
restarts the 30-day clock:

```python
order.status = "delivered"
order.save()
# => TTL resets to 30 days from now
```

When a key expires, Redis removes it silently. Any subsequent `load()` or
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
`_expire_at`. This accepts a `datetime` and tells Redis to delete the key
at that exact moment.

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

!!! tip
    Because validation happens at import time, you will catch configuration
    errors during development rather than at runtime in production. Define
    your Meta options early and run your test suite to verify them.

See [Models and Fields](fields.md) for field type reference, or
[Making Queries](query.md) for query filtering and ordering options.
