# Relationship Field

The `Relationship` field creates references between model instances, similar
to foreign keys in SQL databases. Unlike SQL, Redis has no JOIN operation.
Popoto stores a reference (the related instance's Redis key) and lazy-loads
the full object on access. This keeps writes fast and avoids loading data you
never use, but multi-model queries work differently than in Django or
SQLAlchemy.

## Basic Usage

A `Relationship` field points from one model to another. In a food delivery
system, a `MenuItem` belongs to a `Restaurant` and an `Order` references
a `Customer`, a `Restaurant`, and optionally a `Driver`.

```python
from popoto import (
    Model, KeyField, AutoKeyField, UniqueKeyField,
    Field, SortedField, GeoField, Relationship, DatetimeField,
)

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()

class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float)
    restaurant = Relationship(model=Restaurant)  # links to Restaurant
    available = Field(type=bool, default=True)

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

class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship(model=Customer)
    restaurant = Relationship(model=Restaurant)
    driver = Relationship(model=Driver, null=True)  # nullable
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days
```

`MenuItem.restaurant` is a required relationship. `Order` has three: `customer`
and `restaurant` are required, while `driver` is nullable because a driver is
assigned only after the order is accepted.

## Creating Related Instances

Create the referenced model first, then pass it to the relationship field.

```python
burger_palace = Restaurant.create(
    name="Burger Palace", cuisine="American",
    rating=4.5, location=(40.7128, -74.0060),
)

classic_burger = MenuItem.create(
    name="Classic Burger", price=12.99, restaurant=burger_palace,
)

print(classic_burger.restaurant.name)
# => "Burger Palace"
```

For an `Order`, pass multiple related instances at once.

```python
alice = Customer.create(username="alice", email="alice@example.com",
                        name="Alice Chen", address=(40.7128, -74.0060))

order = Order.create(customer=alice, restaurant=burger_palace,
                     total=25.98, status="pending")
print(order.customer.name)
# => "Alice Chen"
print(order.restaurant.name)
# => "Burger Palace"
```

You can reassign a relationship and call `save()` to update it.

```python
sushi_house = Restaurant.create(name="Sushi House", cuisine="Japanese",
                                rating=4.8, location=(40.7580, -73.9855))
classic_burger.restaurant = sushi_house
classic_burger.save()
print(classic_burger.restaurant.cuisine)
# => "Japanese"
```

## Querying by Relationship

Pass a model instance to filter for all objects that reference it.

```python
items = MenuItem.query.filter(restaurant=burger_palace)
for item in items:
    print(f"{item.name} - ${item.price}")
# => "Classic Burger - $12.99"

alice_orders = Order.query.filter(customer=alice)
print(len(alice_orders))
# => 1
```

!!! note
    The filter value must be a model instance, not a string or key value.
    `Order.query.filter(customer=alice)` works, but
    `Order.query.filter(customer="alice")` raises a `QueryException`.

## Nested Field Access

Use double-underscore notation to query by fields on the related model.

```python
orders = Order.query.filter(restaurant__name="Burger Palace")
for order in orders:
    print(f"Order {order.order_id}: ${order.total}")
# => "Order ...: $25.98"

# Combine nested filters with other parameters
big_orders = Order.query.filter(
    restaurant__name="Burger Palace", total__gte=20.0,
)
print(len(big_orders))
# => 1
```

See [Making Queries](query.md) for the full list of filter operators.

!!! warning
    Nested filters do not recurse through multiple relationship levels.
    `Order.query.filter(restaurant__name="Burger Palace")` works because
    `name` is a direct field on `Restaurant`. You cannot chain through
    a second relationship.

## Traversing Relationships

Because Redis has no JOINs, you traverse relationships with Python loops.

```python
alice = Customer.query.get(username="alice")
alice_orders = Order.query.filter(customer=alice)
restaurants = [order.restaurant for order in alice_orders]
for r in restaurants:
    print(r.name)
# => "Burger Palace"
```

!!! warning "N+1 Query Problem"
    Each `order.restaurant` access triggers a separate Redis call. With 100
    orders, that is 100 additional round trips on top of the initial query.

    ```python
    # Anti-pattern: 1 query + N lazy loads
    for order in Order.query.all():
        print(order.customer.name)   # Redis GET per iteration
        print(order.restaurant.name) # Another Redis GET per iteration
    ```

    For large result sets, consider denormalizing frequently accessed fields
    (e.g., storing `restaurant_name` directly on `Order`).

## How Relationships Work Internally

### Storage as Redis Key

Popoto stores the related instance's Redis key as a string, not the full
serialized object. A `MenuItem` with `restaurant=burger_palace` stores
`"Restaurant:Burger Palace"` in the Redis hash.

### Lazy Loading

When you load a model, relationship fields initially hold the raw Redis key
string. Popoto resolves it to a full instance only when you access the field.

```python
item = MenuItem.query.get(name="Fries")
# item.restaurant is internally "Restaurant:Burger Palace"
restaurant = item.restaurant  # triggers Redis HGETALL
print(restaurant.cuisine)     # => "American"
# Subsequent access uses the resolved instance -- no extra Redis call
```

### Relationship Index

Popoto maintains a Redis set for each relationship value so that
`filter(restaurant=instance)` is an O(1) set lookup rather than a scan.

```
$RelationshipF:MenuItem:restaurant:Restaurant:Burger Palace
  -> { MenuItem:<key1>, MenuItem:<key2>, ... }
```

Indexes are created on `save()`, updated when the relationship changes,
and cleaned up on `delete()`.

### Circular Reference Protection

Popoto tracks which instances are currently being deserialized. If it
encounters a cycle, it leaves the relationship as a Redis key string
instead of recursing infinitely.

## Null Relationships

An `Order` starts without a driver -- `None` until one is assigned.

```python
order = Order.create(customer=alice, restaurant=burger_palace,
                     total=15.50, status="pending")
print(order.driver)
# => None

dave = Driver.create(name="Dave", phone="555-0199",
                     rating=4.9, location=(40.7300, -73.9950))
order.driver = dave
order.status = "assigned"
order.save()
print(order.driver.name)
# => "Dave"
```

!!! tip
    Always check for `None` before accessing attributes on a nullable
    relationship to avoid `AttributeError`.

    ```python
    if order.driver is not None:
        print(order.driver.name)
    else:
        print("No driver assigned yet")
    ```

## Self-Referential Relationships

A model can reference itself -- useful for combo meals that include other
menu items.

```python
class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float)
    restaurant = Relationship(model=Restaurant)
    combo_includes = Relationship(model='MenuItem', null=True)

burger = MenuItem.create(name="Classic Burger", price=12.99,
                         restaurant=burger_palace, combo_includes=None)
combo = MenuItem.create(name="Burger Combo", price=14.99,
                        restaurant=burger_palace, combo_includes=burger)
print(combo.combo_includes.name)
# => "Classic Burger"
```

Use a string (`'MenuItem'`) when the class is not yet fully defined. Set
`null=True` so non-combo items can leave the field empty.

## Best Practices

### Query from the Model That Holds the Relationship

You can only filter on a relationship from the model that declares it.

```python
orders = Order.query.filter(customer=alice)       # Order has customer field
# Customer.query.filter(orders=...) is NOT possible
```

If you need reverse lookups, add a relationship field in both directions.

### Denormalize for Frequent Access Patterns

When you repeatedly need a related field's value, store it directly on the
model. For example, adding `restaurant_name` to `Order` avoids lazy-loading
the `Restaurant` just to display its name.

!!! tip
    Denormalization is a common Redis pattern. Duplicating a field is often
    cheaper than chaining lookups, but you must keep it in sync manually.

### No Cascade Delete

Deleting a model does **not** cascade to related instances. If you delete a
`Restaurant`, any `MenuItem` or `Order` referencing it will hold a stale key.

!!! warning
    Always delete or reassign dependent instances before removing the
    referenced model.

    ```python
    for item in MenuItem.query.filter(restaurant=burger_palace):
        item.delete()
    burger_palace.delete()
    ```

### Keep Relationship Depth Shallow

Each relationship traversal costs a Redis round trip. Design your models so
the most common access patterns require at most one or two hops.

See [Making Queries](query.md) for filter operators and
[Model Meta Options](meta.md) for `order_by` and `ttl` configuration.
