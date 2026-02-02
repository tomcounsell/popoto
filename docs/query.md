# Making Queries

Every Popoto model has a `query` attribute that provides a Django-like interface for retrieving
instances from Redis. You can look up a single object by its key, fetch all instances, filter by
field values, count results, and return lightweight dictionaries instead of full model objects.

Queries combine multiple filters with AND logic. If you pass several filter parameters, only
instances matching all criteria are returned.

```python
from popoto import (
    Model, KeyField, AutoKeyField, UniqueKeyField,
    Field, SortedField, GeoField, Relationship,
)
from popoto.fields.datetime_field import DatetimeField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float)
    restaurant = Relationship(Restaurant)
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
```

These models are used throughout the examples on this page. See
[Models and Fields](fields.md) for full details on each field type.

## Get a Single Object

When you know the exact key values, use `query.get()` to retrieve one instance. This performs a
direct Redis key lookup, making it the fastest way to load a model.

```python
burger_palace = Restaurant.create(
    name="Burger Palace",
    cuisine="American",
    rating=4.5,
    location=(40.7128, -74.0060),
)

restaurant = Restaurant.query.get(name="Burger Palace")
print(restaurant.cuisine)
# => "American"
```

You can also retrieve an object by its raw Redis key.

```python
restaurant = Restaurant.query.get(redis_key="Restaurant:Burger Palace")
print(restaurant.rating)
# => 4.5
```

If no matching instance exists, `get()` returns `None`. If more than one instance matches
(possible when filtering on non-key fields), it raises a `QueryException`.

!!! note
    For models with composite keys, pass all key field values to `get()`. For models with
    `AutoKeyField`, use the auto-generated key value or `redis_key`.

## Get All Objects

Use `query.all()` to retrieve every instance of a model. This fetches all Redis keys registered
to that model and loads each one.

```python
all_restaurants = Restaurant.query.all()
print(len(all_restaurants))
# => 1
```

The `all()` method accepts the same `order_by`, `limit`, and `values` parameters as `filter()`.

```python
# All restaurants ordered by name
restaurants = Restaurant.query.all(order_by="name")

# First 5 restaurants
restaurants = Restaurant.query.all(limit=5)

# All restaurants as dicts with only the name field
restaurants = Restaurant.query.all(values=("name",))
# => [{"name": "Burger Palace"}]
```

!!! tip
    If a model defines `order_by` in its `Meta` class, `all()` uses that ordering by default.
    The `Order` model above defaults to `"-created_at"` (newest first). You can override this
    at query time by passing a different `order_by` value.

## Filter Query Results

Use `query.filter()` to retrieve instances matching specific criteria. Filters are expressed as
keyword arguments using Django-style lookup expressions.

```python
sushi_zen = Restaurant.create(
    name="Sushi Zen",
    cuisine="Japanese",
    rating=4.8,
    location=(40.7580, -73.9855),
)

japanese = Restaurant.query.filter(name__contains="Sushi")
print(len(japanese))
# => 1
print(japanese[0].name)
# => "Sushi Zen"
```

You can combine multiple filters. Only instances matching all criteria are returned.

```python
top_restaurants = Restaurant.query.filter(rating__gte=4.5)
print(len(top_restaurants))
# => 2
```

!!! warning
    All filter parameters are AND-ed together. There is no built-in OR support. If you need
    OR logic, run separate queries and merge the results in Python.

## Count Results

Use `query.count()` to get the number of matching instances without loading them into memory.
This is more efficient than calling `len()` on a full result set.

```python
total_restaurants = Restaurant.query.count()
print(total_restaurants)
# => 2

# Count with filters
top_count = MenuItem.query.count(price__lte=15.0)
```

When called without arguments, `count()` uses Redis `SCARD` for a fast cardinality check. With
filter arguments, it computes the intersection of matching key sets.

## Get Redis Keys

Use `query.keys()` to retrieve the raw Redis keys for all instances of a model. This is useful
for debugging or performing custom Redis operations.

```python
keys = Restaurant.query.keys()
# => [b"Restaurant:Burger Palace", b"Restaurant:Sushi Zen"]
```

!!! warning
    The `keys()` method returns bytes objects. For debugging stale data, you can pass
    `clean=True` to remove orphaned keys, but this should never be used in production.

## Values

Return results as dictionaries instead of model instances by passing a `values` tuple. Specify
which fields to include. This is faster and uses less memory when you only need a few fields.

```python
names_and_cuisines = Restaurant.query.all(values=("name", "cuisine"))
# => [{"name": "Burger Palace", "cuisine": "American"}, ...]
```

You can combine `values` with `filter()` for targeted queries.

```python
cheap_items = MenuItem.query.filter(
    price__lte=10.0,
    values=("name", "price"),
)
# => [{"name": "Fries", "price": 4.99}, ...]
```

!!! tip
    When all fields in `values` are key fields, Popoto extracts values directly from the Redis
    key string without deserializing stored data. This can be 2x faster or more for large
    result sets.

## Order By

Use `order_by` to sort results by any field. Prefix the field name with `-` for descending
order. Ascending order is the default.

```python
# Cheapest menu items first
items = MenuItem.query.all(order_by="price")

# Highest-rated restaurants first
restaurants = Restaurant.query.all(order_by="-rating")
```

Ordering works with `filter()` as well.

```python
affordable = MenuItem.query.filter(
    price__lte=20.0,
    order_by="price",
)
```

You can set a default ordering in the model's `Meta` class. The `Order` model above uses
`order_by = "-created_at"` so that queries return the most recent orders first. You can
override this at query time.

```python
# Uses Meta default: newest orders first
recent_orders = Order.query.all()

# Override: sort by total ascending
orders_by_total = Order.query.all(order_by="total")
```

See [Model Meta Options](meta.md) for more on configuring default ordering.

!!! note
    The `order_by` field must be included in `values` if you are using both parameters
    together. Otherwise Popoto raises a `QueryException`.

## Limit Results

Use `limit` to cap the number of returned instances. The limit is applied after filtering and
ordering.

```python
top_5 = Restaurant.query.all(order_by="-rating", limit=5)
print(len(top_5))
# => 2
```

You can also use Python slice notation on the result list.

```python
top_5 = Restaurant.query.all(order_by="-rating")[:5]
```

Both approaches return the same results. The explicit `limit` parameter may be slightly more
efficient because it can short-circuit internal sorting. Combine `limit` with `order_by` to
get "top N" style queries.

```python
# The 3 cheapest items on the menu
cheapest = MenuItem.query.filter(order_by="price", limit=3)
```

## KeyField Filters

KeyField filters support string matching operations on the values that make up the Redis key.
All lookups are case-sensitive.

| Lookup | Description |
|--------|-------------|
| `name=` | Exact match |
| `name__isnull=` | Filter for null (`True`) or non-null (`False`) values |
| `name__contains=` | Substring match anywhere in the value |
| `name__startswith=` | Prefix match |
| `name__endswith=` | Suffix match |
| `name__in=` | Match any value in the provided list |

Here are examples using the `Restaurant` model's `name` KeyField.

```python
# Exact match
Restaurant.query.filter(name="Burger Palace")

# Restaurants whose name contains "Sushi"
Restaurant.query.filter(name__contains="Sushi")

# Restaurants whose name starts with "B"
Restaurant.query.filter(name__startswith="B")

# Restaurants whose name ends with "Zen"
Restaurant.query.filter(name__endswith="Zen")

# Match any of several names
Restaurant.query.filter(name__in=["Burger Palace", "Sushi Zen", "Taco Town"])

# All restaurants that have a name set (non-null)
Restaurant.query.filter(name__isnull=False)
```

!!! note
    KeyField lookups like `__contains`, `__startswith`, and `__endswith` scan all keys for
    the model and filter in Python. For large datasets, prefer exact match or `__in` lookups
    which use Redis set operations directly.

## SortedField Filters

SortedField filters use Redis sorted sets for efficient numeric and date/time range queries.
These are the fastest way to filter on continuous values.

| Lookup | Description |
|--------|-------------|
| `price=` | Exact match |
| `price__gt=` | Greater than |
| `price__gte=` | Greater than or equal to |
| `price__lt=` | Less than |
| `price__lte=` | Less than or equal to |

Filter menu items by price.

```python
# Items under $10
budget_items = MenuItem.query.filter(price__lt=10.0)

# Items $10 or more
premium_items = MenuItem.query.filter(price__gte=10.0)

# Items between $5 and $15
mid_range = MenuItem.query.filter(price__gte=5.0, price__lte=15.0)
```

Filter restaurants by rating.

```python
# Highly rated restaurants (4.0 and above)
top_rated = Restaurant.query.filter(rating__gte=4.0)

# Restaurants rated above 4.5
excellent = Restaurant.query.filter(rating__gt=4.5)
```

Combine range filters with other parameters for precise queries.

```python
# Top-rated restaurants, highest first, limit to 10
best = Restaurant.query.filter(
    rating__gte=4.0,
    order_by="-rating",
    limit=10,
)
```

Range queries work with any sortable type including `int`, `float`, `Decimal`, `datetime`,
`date`, and `time`. The `Order` model's `total` field is a good example.

```python
# Orders over $50
big_orders = Order.query.filter(total__gt=50.0)

# Orders between $20 and $100
typical_orders = Order.query.filter(total__gte=20.0, total__lte=100.0)
```

!!! tip
    SortedField range queries are backed by Redis `ZRANGEBYSCORE`, which runs in
    O(log(N) + M) time where N is the total set size and M is the number of results. This
    makes them efficient even on large datasets.

## GeoField Filters

GeoField filters perform geographic proximity searches using Redis geospatial indexes. You can
find all instances within a given radius of a point, specified either as coordinates or by
referencing another model instance.

| Parameter | Description |
|-----------|-------------|
| `location=` | Tuple `(latitude, longitude)` or `GeoField.Coordinates` |
| `location_latitude=` | Float latitude value |
| `location_longitude=` | Float longitude value |
| `location_radius=` | Search radius distance (default: `10`) |
| `location_radius_unit=` | `"m"`, `"km"`, `"ft"`, or `"mi"` (default: `"km"`) |
| `location_member=` | Use another instance's coordinates as center point |
| `location_with_distances=` | `True` to attach distance info to results |

Find restaurants near a delivery address.

```python
# Restaurants within 5km of Times Square
nearby = Restaurant.query.filter(
    location=(40.7580, -73.9855),
    location_radius=5,
    location_radius_unit='km',
)
for r in nearby:
    print(r.name)
```

You can specify latitude and longitude as separate parameters.

```python
nearby = Restaurant.query.filter(
    location_latitude=40.7580,
    location_longitude=-73.9855,
    location_radius=3,
    location_radius_unit='mi',
)
```

Use another model instance as the center point for the search. This is handy when you already
have a loaded customer or driver and want to find nearby restaurants.

```python
customer = Customer.create(
    username="janedoe",
    email="jane@example.com",
    name="Jane Doe",
    address=(40.7484, -73.9857),
)

# Restaurants within 2km of Jane's address
nearby = Restaurant.query.filter(
    location_member=customer,
    location_radius=2,
    location_radius_unit='km',
)
```

!!! note
    When using `location_member`, Popoto looks up the member's coordinates in the geo index.
    The member must have been saved with a valid GeoField value. The GeoField name on the
    member model must match the GeoField name used in the filter (e.g., both named `location`,
    or use `address` for the customer model's field name accordingly).

### Distances in Results

Pass `location_with_distances=True` to attach distance information to each returned instance.
Results are automatically sorted by distance, closest first.

```python
results = Restaurant.query.filter(
    location=(40.7128, -74.0060),
    location_radius=10,
    location_radius_unit='km',
    location_with_distances=True,
)

for restaurant in results:
    print(f"{restaurant.name}: {restaurant._geo_distance} "
          f"{restaurant._geo_distance_unit}")
# => Burger Palace: 0.0 km
# => Sushi Zen: 5.2 km
```

Distance information is stored on each instance as `_geo_distance` (a float) and
`_geo_distance_unit` (a string matching the `radius_unit` you specified). This is useful for
displaying "2.3 km away" in a delivery app interface.

### Finding Nearby Drivers

A common delivery-app pattern is finding the closest available driver to a restaurant.

```python
driver = Driver.create(
    name="Alex",
    phone="555-0101",
    rating=4.9,
    location=(40.7120, -74.0050),
)

nearby_drivers = Driver.query.filter(
    location=(40.7128, -74.0060),
    location_radius=5,
    location_radius_unit='km',
    location_with_distances=True,
)

if nearby_drivers:
    closest = nearby_drivers[0]
    print(f"Assigning {closest.name} "
          f"({closest._geo_distance} {closest._geo_distance_unit} away)")
    # => Assigning Alex (0.1 km away)
```

See [Models and Fields](fields.md#geofield) for more on defining GeoFields, and
[Relationship Field](relationship.md) for linking drivers to orders.
