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

# For AccessTrackerMixin models, pass _no_track=True to load without staging a read
```

For `AccessTrackerMixin` models, `get(..., _no_track=True)` (and `async_get`) loads by
key without staging a read; see [Suppressing tracking for bulk
operations](fields.md#suppressing-tracking-for-bulk-operations).

If no matching instance exists, `get()` returns `None`. If more than one instance matches
(possible when filtering on non-key fields), it raises a `QueryException`.

!!! note
    For models with composite keys, pass all key field values to `get()`. For models with
    `AutoKeyField`, use the auto-generated key value or `redis_key`.

## Check Existence Without Loading

When you only need to know *whether* a record is stored, `Model.exists()` answers in one
`EXISTS` command without decoding the hash.

```python
Restaurant.exists(name="Burger Palace")
# => True

Restaurant.exists(name="Nowhere Diner")
# => False
```

It takes the same three spellings as `get()` — key field values, a `DB_key`, or a raw
Redis key string (positionally or as `redis_key=`):

```python
Restaurant.exists("Restaurant:Burger Palace")
Restaurant.exists(redis_key="Restaurant:Burger Palace")
Restaurant.exists(db_key=restaurant.db_key)
```

!!! warning
    Pass a full Redis key as a **string**, not as `DB_key("Restaurant:Burger Palace")`.
    `DB_key` treats a string as a single key partial and escapes the `:` inside it, so a
    multi-segment key wrapped that way names a key that never exists. `exists()`
    short-circuits a `str` straight through, exactly as `get()` does.

`exists()` reports storage, not validity: a record whose `ValidityField` interval has been
closed still exists. Use the [ValidityField filters](#validityfield-filters) for
as-of membership.

## Batching Commands into One Transaction

`popoto.batch()` opens a batch of queued commands against Popoto's shared connection, so
you do not need to import the Redis client to start a transaction.

```python
import popoto

pipe = popoto.batch()
Restaurant(name="Sushi Zen", cuisine="Japanese").save(pipeline=pipe)
Restaurant(name="Taco Stand", cuisine="Mexican").save(pipeline=pipe)
pipe.execute()   # both saves, and all their index writes, in one MULTI/EXEC
```

Every `save()`/`delete()` and every field hook accepts `pipeline=`, so a batch groups a
whole unit of work — model hashes and index maintenance alike — into a single atomic
round-trip. `batch(transaction=False)` queues without a `MULTI`/`EXEC` wrapper, which
pipelines for throughput but gives up atomicity; some callers (the
[provenance journal](features/provenance-journal.md)) require a transactional pipeline and
raise if given a non-transactional one.

The returned object is a `redis.client.Pipeline` — the same type
`popoto.get_redis().pipeline()` returns — so existing code that builds one off the client
keeps working. `batch()` is the preferred spelling.

## Get Multiple Objects by Key

When you already have a list of Redis keys (for example, from a set intersection, a cache of
"recently viewed" items, or a secondary index), use `query.get_many()` to hydrate them all in
a single round-trip. Internally this uses a Redis pipeline to batch `HGETALL` calls, turning
N sequential lookups into one network exchange.

```python
keys = ["Restaurant:Burger Palace", "Restaurant:Sushi Zen", "Restaurant:Gone Place"]
restaurants = Restaurant.query.get_many(redis_keys=keys)
print(restaurants)
# => [<Restaurant>, <Restaurant>, None]  -- third key did not exist
```

The returned list preserves the same order as the input keys. Missing keys appear as `None`
so you can correlate each position with the original key list.

If you do not need the `None` placeholders, pass `skip_none=True` to drop them.

```python
restaurants = Restaurant.query.get_many(redis_keys=keys, skip_none=True)
print(restaurants)
# => [<Restaurant>, <Restaurant>]  -- only existing objects
```

An empty input list returns an empty list immediately, without touching Redis.

!!! tip
    `get_many()` is ideal for the "fan-out then hydrate" pattern: use `query.keys()` or a
    Redis set operation to collect keys, then hydrate them in bulk. This avoids the overhead
    of loading and filtering all instances when you already know which keys you want.

!!! note
    `get_many()` differs from the internal `get_many_objects()` method. The public method
    takes a list of string keys, preserves input order, and returns `None` for missing entries.
    The internal method takes a set of bytes keys and silently drops missing entries.

!!! example "Live demo"
    The [Popoto Kitchen example app](https://github.com/tomcounsell/popoto/tree/main/examples)
    demonstrates `get_many()` in its operations script. Run `python -m popoto_kitchen --ops`
    to see bulk Order loading with both default and `skip_none=True` modes. Source:
    [`examples/popoto_kitchen/operations.py`](https://github.com/tomcounsell/popoto/blob/main/examples/popoto_kitchen/operations.py).

### Async get_many()

The async counterpart `async_get_many()` uses a native async Redis pipeline, so it does
not block the event loop even when hydrating hundreds of keys. It accepts the same
parameters and preserves the same ordering guarantees as the synchronous version.

```python
async def bulk_lookup():
    keys = ["Restaurant:Burger Palace", "Restaurant:Sushi Zen", "Restaurant:Gone Place"]
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys)
    # => [<Restaurant>, <Restaurant>, None]

    # Drop missing entries
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys, skip_none=True)
    # => [<Restaurant>, <Restaurant>]
```

This pairs naturally with `async_keys()` for the "fan-out then hydrate" pattern:

```python
async def hydrate_all():
    keys = await Restaurant.query.async_keys()
    restaurants = await Restaurant.query.async_get_many(redis_keys=keys, skip_none=True)
    print(f"Loaded {len(restaurants)} restaurants")
```

!!! tip
    `async_get_many()` is especially useful inside `asyncio.gather()` when you need to
    hydrate keys from multiple models concurrently. See [Async Operations](async.md#async_get_many)
    for more examples.

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

!!! note
    All filter parameters are AND-ed together by default. For OR logic, use Q objects
    described in the [Q Objects section](#q-objects-or-and-not) below.

## Chainable Query Builder

Popoto supports two query syntaxes: the traditional kwargs-based API shown above, and a fluent
chainable API that allows method chaining. Both syntaxes are fully supported and can be used
interchangeably based on your preference.

### Chainable vs Kwargs Syntax

The original kwargs API passes all parameters to a single `filter()` call.

```python
# Kwargs API - all parameters in one call
results = Restaurant.query.filter(
    rating__gte=4.0,
    order_by="-rating",
    limit=10
)
```

The chainable API builds queries incrementally with method chaining.

```python
# Chainable API - fluent method chaining
results = Restaurant.query.filter(rating__gte=4.0).order_by("-rating").limit(10).all()
```

Both approaches produce identical results. The chainable API can be more readable when building
complex queries dynamically or when you prefer a fluent interface style.

### Chainable Methods

The `QueryBuilder` returned by `filter()` supports these chainable methods:

| Method | Description |
|--------|-------------|
| `filter(**kwargs)` | Add filter criteria, returns new QueryBuilder |
| `order_by(field)` | Set sort field (prefix with "-" for descending) |
| `limit(n)` | Set maximum number of results |
| `values(*fields)` | Return dicts with specified fields instead of model instances |
| `computed_sort(fn, reverse)` | Sort by a Python key function (applied after fetch, before limit) |
| `no_track()` | Suppress `on_read()` staging for `AccessTrackerMixin` models — use for internal operations (reindex, migration, lifecycle ticks) that should not count as reads |
| `top_by_decay(field_name, n)` | Return top-N by time-decayed score ([API ref](reference/popoto/models/query.md)) |
| `composite_score(indexes, limit, temperature)` | Return top-K by weighted composite of multiple sorted indexes ([API ref](reference/popoto/models/query.md)) |
| `semantic_search(query_text, indexes, limit)` | Return top-K by semantic similarity, optionally combined with sorted indexes ([API ref](reference/popoto/models/query.md)) |
| `keyword_search(query_text, field, limit)` | Return instances ranked by BM25 keyword relevance. See [Hybrid Retrieval](features/hybrid-retrieval.md). |
| `fuse(k, limit, post_filter, weights, **ranked_lists)` | Reciprocal Rank Fusion across heterogeneous ranked lists. Applies the builder's own keyword filters to the fused set (before the top-K slice); raises `QueryException` on Q-object filters. See [Hybrid Retrieval](features/hybrid-retrieval.md). |
| `all()` | Execute query and return all results as a list |
| `first()` | Execute query and return first result or None |
| `count()` | Count matching results without loading objects |

### Chaining Multiple Filters

You can chain multiple `filter()` calls to incrementally add criteria. Each call returns a new
QueryBuilder with the combined filters.

```python
# Chain multiple filters - equivalent to AND-ing criteria
results = (
    Restaurant.query
    .filter(rating__gte=4.0)
    .filter(cuisine="Japanese")
    .order_by("-rating")
    .limit(5)
    .all()
)
```

This is equivalent to the kwargs version.

```python
# Same query using kwargs API
results = Restaurant.query.filter(
    rating__gte=4.0,
    cuisine="Japanese",
    order_by="-rating",
    limit=5
)
```

### Getting the First Result

Use `first()` to retrieve only the first matching result, or `None` if no matches exist.
This is more efficient than calling `all()[0]` when you only need one object.

```python
# Get the highest-rated Japanese restaurant
top_japanese = (
    Restaurant.query
    .filter(cuisine="Japanese")
    .order_by("-rating")
    .first()
)

if top_japanese:
    print(f"Best Japanese: {top_japanese.name} ({top_japanese.rating})")
```

### Counting Without Loading

Use `count()` on a QueryBuilder to count matching results without loading objects into memory.

```python
# Count premium menu items without loading them
expensive_count = MenuItem.query.filter(price__gte=20.0).count()
print(f"Premium items: {expensive_count}")
```

A `limit()` chained onto the builder still bounds what `all()` returns, but `count()` always
reports the full matching population regardless of any limit set on the builder.

### Using Values with Chainable API

The `values()` method specifies which fields to return as dictionaries.

```python
# Get only names and ratings as dicts
restaurant_data = (
    Restaurant.query
    .filter(rating__gte=4.0)
    .values("name", "rating")
    .order_by("-rating")
    .all()
)
# => [{"name": "Sushi Zen", "rating": 4.8}, {"name": "Burger Palace", "rating": 4.5}]
```

### Sorting by Computed Values

Use `computed_sort()` to sort results by a value computed in Python rather than a stored
field. The sort is applied after fetching results from Redis but before `limit()`, so you
get the correct top-N results.

```python
# Sort restaurants by a weighted score combining rating and review count
results = (
    Restaurant.query
    .filter(active=True)
    .computed_sort(lambda r: r.rating * 0.7 + r.review_count * 0.3, reverse=True)
    .limit(10)
    .all()
)
```

The `fn` argument is any callable that takes a model instance (or dict when using
`values()`) and returns a sort key. Set `reverse=True` for descending order.

```python
# Works with values() projection -- fn receives dicts
data = (
    MenuItem.query
    .filter(available=True)
    .computed_sort(lambda d: d["price"] / d["rating"], reverse=False)
    .values("name", "price", "rating")
    .limit(5)
    .all()
)
```

When both `computed_sort()` and `order_by()` are set, `computed_sort()` takes precedence
and `order_by()` is ignored (with a warning logged).

!!! note "Performance"
    `computed_sort()` applies a Python `sorted()` call (O(N log N)) on the full result
    set before slicing with `limit()`. For large result sets (>10K records), prefer
    `SortedField` indexes which leverage Redis sorted sets for O(log N + M) performance.

### Backward Compatibility

The QueryBuilder is fully backward compatible with code that treats `filter()` results as a list.
You can iterate over a QueryBuilder directly, use `len()`, or access items by index.

```python
# These all work - QueryBuilder acts like a list
results = Restaurant.query.filter(rating__gte=4.0)

# Iteration
for restaurant in results:
    print(restaurant.name)

# Length
print(len(results))

# Indexing
first = results[0]

# Boolean check
if results:
    print("Found matches!")

# Comparison with list
assert results == Restaurant.query.filter(rating__gte=4.0).all()
```

Each of these operations executes the query when first accessed. For repeated access, call
`all()` once and store the result.

```python
# More efficient for multiple operations
results = Restaurant.query.filter(rating__gte=4.0).all()
print(len(results))
for r in results:
    print(r.name)
```

!!! tip
    The chainable API is especially useful when building queries conditionally. You can
    start with a base query and add filters based on runtime conditions, then call `all()`
    at the end.

```python
# Build query conditionally
query = Restaurant.query.filter(active=True)

if min_rating:
    query = query.filter(rating__gte=min_rating)

if cuisine_type:
    query = query.filter(cuisine=cuisine_type)

results = query.order_by("-rating").limit(20).all()
```

## Q Objects (OR, AND, NOT)

Q objects enable complex query logic with OR, AND, and NOT operators. They provide a Django-style
interface for building expressive filter expressions that go beyond simple AND-ed kwargs.

### Importing Q

Import the `Q` class from popoto alongside your other imports.

```python
from popoto import Q, Model, KeyField, Field, SortedField
```

### Basic Q Object Usage

A Q object wraps filter kwargs, which can then be combined with other Q objects using operators.

```python
# Simple Q object - equivalent to filter(status="active")
active = Restaurant.query.filter(Q(active=True))

# Q objects support all the same lookups as regular filter kwargs
high_rated = Restaurant.query.filter(Q(rating__gte=4.5))
```

### OR Queries with `|`

Use the `|` (pipe) operator to combine Q objects with OR logic. This returns instances matching
either condition.

```python
# Find restaurants that are Japanese OR have a high rating
results = Restaurant.query.filter(
    Q(cuisine="Japanese") | Q(rating__gte=4.5)
)
```

You can chain multiple OR conditions.

```python
# Find restaurants matching any of three cuisines
results = Restaurant.query.filter(
    Q(cuisine="Japanese") | Q(cuisine="Italian") | Q(cuisine="Mexican")
)
```

!!! tip
    For simple "match any of these values" queries, the `__in` lookup is more efficient than
    multiple OR conditions: `filter(cuisine__in=["Japanese", "Italian", "Mexican"])`.

### AND Queries with `&`

Use the `&` operator for explicit AND logic. This returns instances matching both conditions.

```python
# Find active restaurants with high ratings
results = Restaurant.query.filter(
    Q(active=True) & Q(rating__gte=4.0)
)
```

!!! note
    Explicit AND with `&` is equivalent to passing multiple kwargs to `filter()`. Both of
    these queries produce identical results:
    ```python
    # Using Q with &
    Restaurant.query.filter(Q(active=True) & Q(rating__gte=4.0))

    # Using kwargs (simpler)
    Restaurant.query.filter(active=True, rating__gte=4.0)
    ```
    Use `&` when you need to combine it with OR logic in the same expression.

### Negation with `~`

Use the `~` (tilde) operator to negate a Q object. This returns instances that do NOT match the
condition.

```python
# Find all restaurants that are NOT inactive
results = Restaurant.query.filter(~Q(active=False))

# Find restaurants not in the "Fast Food" cuisine
results = Restaurant.query.filter(~Q(cuisine="Fast Food"))
```

!!! warning
    Negation (`~Q`) requires scanning all keys for the model to compute the set difference.
    On large datasets with many instances, this can be slow and memory-intensive. Use with
    caution in production, and prefer positive filters when possible.

### Complex Nested Expressions

Q objects can be nested to build arbitrarily complex expressions. Use parentheses to control
operator precedence.

```python
# Active OR premium restaurants, with high ratings
results = Restaurant.query.filter(
    (Q(active=True) | Q(cuisine="Premium")) & Q(rating__gte=4.0)
)

# Complex: (active AND high-rated) OR (premium AND any rating)
results = Restaurant.query.filter(
    (Q(active=True) & Q(rating__gte=4.5)) | Q(cuisine="Premium")
)

# Exclude specific combinations
results = Restaurant.query.filter(
    Q(rating__gte=3.0) & ~Q(cuisine="Fast Food")
)
```

### Mixing Q Objects with Kwargs

You can pass Q objects alongside regular kwargs to `filter()`. The Q objects are evaluated first,
then AND-ed with the kwargs filters.

```python
# Q object OR combined with kwarg filter
# Equivalent to: (cuisine="Japanese" OR cuisine="Italian") AND active=True
results = Restaurant.query.filter(
    Q(cuisine="Japanese") | Q(cuisine="Italian"),
    active=True
)
```

This is useful when you have a dynamic OR condition but always want to apply a static filter.

```python
# Dynamic cuisine filter with static active=True
cuisines = ["Japanese", "Italian", "Mexican"]
cuisine_q = Q(cuisine=cuisines[0])
for c in cuisines[1:]:
    cuisine_q = cuisine_q | Q(cuisine=c)

results = Restaurant.query.filter(cuisine_q, active=True)
```

### Q Objects with Chainable API

Q objects work seamlessly with the chainable query builder.

```python
results = (
    Restaurant.query
    .filter(Q(cuisine="Japanese") | Q(cuisine="Italian"))
    .filter(rating__gte=4.0)
    .order_by("-rating")
    .limit(10)
    .all()
)
```

### Supported Lookups in Q Objects

Q objects support all the same lookups as regular filter kwargs:

| Field Type | Supported Lookups |
|------------|-------------------|
| KeyField | `=`, `__in`, `__contains`, `__startswith`, `__endswith`, `__isnull` |
| IndexedField / UniqueField | `=`, `__in`, `__startswith`, `__endswith`, `__isnull` |
| SortedField | `=`, `__gt`, `__gte`, `__lt`, `__lte` |
| GeoField | `location=`, `location_radius=`, etc. |
| Field | `=` (exact match) |

```python
# Q with SortedField range
Restaurant.query.filter(Q(rating__gte=4.0) | Q(rating__lte=2.0))

# Q with KeyField pattern matching
Restaurant.query.filter(Q(name__startswith="A") | Q(name__startswith="B"))
```

### Performance Considerations

Q objects provide expressive query syntax, but understanding their performance characteristics
helps you write efficient code.

**OR queries execute both sides**: When you use `|`, Popoto evaluates both Q objects and computes
the union of their result sets. This means two separate filter operations are performed.

```python
# This executes TWO filter operations, then unions the results
Restaurant.query.filter(Q(cuisine="Japanese") | Q(cuisine="Italian"))

# This is more efficient for simple value matching
Restaurant.query.filter(cuisine__in=["Japanese", "Italian"])
```

**AND queries use set intersection**: When you use `&`, Popoto evaluates both Q objects and
computes the intersection. This is similar to passing multiple kwargs.

**Negation scans all keys**: The `~` operator requires fetching all keys for the model to
compute the set difference. Avoid `~Q` on models with many instances.

| Expression | Redis Operations |
|------------|------------------|
| `Q(a=1)` | Single filter |
| `Q(a=1) \| Q(b=2)` | Two filters + set union |
| `Q(a=1) & Q(b=2)` | Two filters + set intersection |
| `~Q(a=1)` | One filter + all keys scan + set difference |

!!! tip
    For best performance, prefer `__in` over multiple OR conditions when matching against a
    list of values, and avoid `~Q` on large datasets.

## Expression-Based Queries

Expression-based queries let you use Python comparison operators directly on Field classes to build
filter conditions. This provides a more Pythonic, type-safe way to write queries with excellent IDE
support for autocomplete and error detection.

### Expression vs Kwargs Syntax

The traditional kwargs syntax uses field names as strings with lookup suffixes.

```python
# Kwargs syntax
Restaurant.query.filter(rating__gte=4.0, status="active")
```

The expression syntax uses Python operators on Field classes accessed through the Model.

```python
# Expression syntax
Restaurant.query.filter(Restaurant.rating >= 4.0, Restaurant.status == "active")
```

Both approaches produce identical results. The expression syntax offers IDE autocomplete on field
names, compile-time detection of typos, and a more natural Python feel.

### Supported Operators

Expression-based queries support all standard Python comparison operators.

| Operator | Description | Equivalent Lookup |
|----------|-------------|-------------------|
| `==` | Equal to | `field=value` |
| `!=` | Not equal to | `~Q(field=value)` |
| `>` | Greater than | `field__gt=value` |
| `>=` | Greater than or equal | `field__gte=value` |
| `<` | Less than | `field__lt=value` |
| `<=` | Less than or equal | `field__lte=value` |

Here are examples using different operators.

```python
# Equal to
Restaurant.query.filter(Restaurant.name == "Burger Palace")

# Not equal to
Restaurant.query.filter(Restaurant.status != "closed")

# Greater than
MenuItem.query.filter(MenuItem.price > 20.0)

# Greater than or equal
Restaurant.query.filter(Restaurant.rating >= 4.5)

# Less than
MenuItem.query.filter(MenuItem.price < 10.0)

# Less than or equal
Order.query.filter(Order.total <= 50.0)
```

### Combining Expressions with & and |

Use the `&` operator for AND logic and `|` for OR logic, just like with Q objects. Parentheses
control operator precedence.

```python
# AND: restaurants with high rating AND active status
Restaurant.query.filter(
    (Restaurant.rating >= 4.0) & (Restaurant.active == True)
)

# OR: restaurants that are Japanese OR Italian
Restaurant.query.filter(
    (Restaurant.cuisine == "Japanese") | (Restaurant.cuisine == "Italian")
)

# Complex: (high-rated AND active) OR premium cuisine
Restaurant.query.filter(
    ((Restaurant.rating >= 4.5) & (Restaurant.active == True))
    | (Restaurant.cuisine == "Premium")
)
```

!!! note
    Always use parentheses around individual expressions when combining with `&` or `|`.
    Without parentheses, Python's operator precedence may produce unexpected results.

### Mixing Expressions with Kwargs

You can mix expression-based filters with traditional kwargs in the same `filter()` call. This is
useful when some conditions are more naturally expressed one way or the other.

```python
# Expression for comparison, kwarg for exact match
Restaurant.query.filter(
    Restaurant.rating > 4.0,
    active=True
)

# Multiple expressions with kwargs
MenuItem.query.filter(
    MenuItem.price >= 5.0,
    MenuItem.price <= 15.0,
    available=True
)
```

The expressions and kwargs are combined with AND logic, just like multiple kwargs would be.

### Expressions with Chainable API

Expression-based queries work seamlessly with the chainable query builder.

```python
# Chain expressions with other query methods
results = (
    Restaurant.query
    .filter(Restaurant.rating >= 4.0)
    .filter(Restaurant.active == True)
    .order_by("-rating")
    .limit(10)
    .all()
)

# Mix expressions and kwargs in chained calls
results = (
    MenuItem.query
    .filter(MenuItem.price <= 20.0)
    .filter(available=True)
    .order_by("price")
    .first()
)
```

### IDE Autocomplete Benefits

One of the main advantages of expression-based queries is improved developer experience with IDE
support.

**Autocomplete**: When you type `Restaurant.`, your IDE suggests all available fields (name,
cuisine, rating, location, active). No need to remember field names or check the model definition.

**Type checking**: If you mistype a field name, your IDE and type checker can catch it immediately.
With kwargs, a typo like `ratng__gte=4.0` would silently fail or raise an error at runtime.

**Refactoring**: When you rename a field, IDE refactoring tools can update all expression-based
queries automatically. String-based kwargs require manual find-and-replace.

```python
# IDE catches this typo immediately
Restaurant.query.filter(Restaurant.ratng >= 4.0)  # AttributeError highlighted

# This typo is only caught at runtime
Restaurant.query.filter(ratng__gte=4.0)  # No IDE warning
```

### When to Use Expressions vs Kwargs

Both syntaxes are fully supported. Choose based on your preference and use case.

**Use expressions when:**

- You want IDE autocomplete and type checking
- Building queries with comparison operators (`>`, `<`, `>=`, `<=`)
- Combining conditions with `&` and `|` operators
- Working in a codebase that values static analysis

**Use kwargs when:**

- Writing simple exact-match filters
- Using special lookups like `__contains`, `__startswith`, `__in`
- Dynamically building queries from dictionaries
- Maintaining consistency with existing Django-style code

```python
# Expressions are natural for comparisons
Restaurant.query.filter(Restaurant.rating >= 4.0)

# Kwargs are concise for pattern matching
Restaurant.query.filter(name__startswith="B")

# Mix both as needed
Restaurant.query.filter(
    Restaurant.rating >= 4.0,
    name__contains="Sushi"
)
```

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
filter arguments, it computes the intersection of matching key sets. `count()` reports the full
matching population and is never bounded by `limit`, on both the chainable and kwargs forms,
including queries that carry `Q` objects — call `count()` and `all()`/`limit()` separately if you
need the page size instead.

## Get Redis Keys

Use `query.keys()` to retrieve the raw Redis keys for all instances of a model. This is useful
for debugging or performing custom Redis operations.

```python
keys = Restaurant.query.keys()
# => [b"Restaurant:Burger Palace", b"Restaurant:Sushi Zen"]
```

!!! warning
    The `keys()` method returns bytes objects. The `clean=True` parameter is deprecated.
    Use [`Model.clean_indexes()`](recipes.md#index-maintenance) for production-safe
    orphan removal that covers all five index types.

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

## IndexedField Filters

IndexedField and UniqueField filters use Redis Set operations for efficient exact-match queries
on non-key fields. They support the same lookups as KeyField (except `__contains`).

| Lookup | Description |
|--------|-------------|
| `status=` | Exact match |
| `status__isnull=` | Filter for null (`True`) or non-null (`False`) values |
| `status__startswith=` | Prefix match |
| `status__endswith=` | Suffix match |
| `status__in=` | Match any value in the provided list |

Here are examples using an `Order` model with `status = IndexedField(type=str)`.

```python
from popoto import Model, AutoKeyField, IndexedField, SortedField

class Order(Model):
    order_id = AutoKeyField()
    status = IndexedField(type=str)
    total = SortedField(type=float)

# Exact match
Order.query.filter(status="shipped")

# Match any of several statuses (server-side SUNION)
Order.query.filter(status__in=["pending", "processing"])

# Orders with a non-null status
Order.query.filter(status__isnull=False)

# Prefix match
Order.query.filter(status__startswith="ship")

# Combine with SortedField range queries
Order.query.filter(status="pending", total__gte=100.0)
```

!!! note
    IndexedField `__startswith` and `__endswith` lookups use Redis SCAN, similar to KeyField
    pattern queries. For large datasets, prefer exact match or `__in` lookups which use
    direct Set operations.

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

## ValidityField Filters

`ValidityField` filters restrict results to records whose bitemporal validity interval
covers a given moment, rather than filtering on a stored value.

| Lookup | Description |
|--------|-------------|
| `{field}__current=True` | Records whose interval covers *now* |
| `{field}__current=False` | Records that are closed, or not yet started (complement of `current=True` over managed records only). Building the complement reads both interval sets in full, so this lookup is O(N) where `current=True` is O(log N + M) |
| `{field}__as_of=t` | Records whose interval covered epoch `t` |

```python
Fact.query.filter(validity__current=True)
Fact.query.filter(validity__as_of=two_weeks_ago)
```

Using either lookup consumes a filter slot, so it disables sorted-range `limit`
pushdown on that query — the default retrieval path (`composite_score()`,
`top_by_decay()`, `ContextAssembler.assemble()`) instead accepts a keyword-only
`as_of` parameter and gates server-side, keeping pushdown intact. See
[ValidityField and SupersessionProtocol](features/validity-and-supersession.md) for
the full semantics, including why a record with no interval entry satisfies
neither `current=True` nor `current=False`.

## Semantic Search

`semantic_search()` finds instances by meaning rather than exact field values. It embeds the
query text via the configured provider, computes cosine similarity against all stored embeddings,
and returns the top-K most similar instances.

This requires a model with an `EmbeddingField` and a configured embedding provider (via
`popoto.configure()` or per-field). See [EmbeddingField](fields.md#embeddingfield) for setup.

### Basic Similarity Search

```python
from popoto.fields.content_field import ContentField
from popoto.fields.embedding_field import EmbeddingField

class Memory(Model):
    topic = KeyField()
    content = ContentField()
    relevance = SortedField(type=float)
    embedding = EmbeddingField(source="content")

# Find memories semantically similar to a query
results = Memory.query.semantic_search("revenue trends", limit=5)
```

When called without `indexes`, results are ranked purely by cosine similarity.

### Combined with Sorted Indexes

Pass `indexes` to blend semantic similarity with other sorted field scores using
`composite_score()` under the hood:

```python
results = Memory.query.semantic_search(
    "revenue trends",
    indexes={"relevance": 0.4},
    limit=10,
    temperature=1.0,
)
```

The similarity scores are injected as an additional weighted signal alongside the
sorted field indexes, producing a unified ranking.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_text` | `str` | *(required)* | The text to search for semantically. |
| `indexes` | `dict` | `None` | Mapping of sorted field names to weights for composite scoring. If None, ranks by similarity alone. |
| `limit` | `int` | `10` | Maximum results to return. |
| `aggregate` | `str` | `"SUM"` | Aggregation mode for ZUNIONSTORE when using indexes. |
| `min_score` | `float` | `None` | Optional minimum composite score threshold. |
| `post_filter` | `Callable` | `None` | Optional `(redis_key, score) -> bool` filter. |
| `co_occurrence_boost` | `dict` | `None` | Optional `{redis_key: weight}` dict for association boosting. |
| `temperature` | `float` | `1.0` | Score scaling factor. |

Returns an empty list if the query text is empty, no provider is configured, or no
embeddings exist for the model.

## Performance Best Practices

Popoto is optimized for common query patterns, but understanding the underlying Redis operations
helps you write efficient code. Here are the key patterns to follow.

### Use `count()` Instead of `len(all())`

The `count()` method uses Redis `SCARD` for O(1) cardinality checks when called without filters.
This is approximately 186x faster than loading all objects just to count them.

```python
# Good: O(1) using Redis SCARD
total = Restaurant.query.count()

# Bad: Loads all objects into memory first
total = len(Restaurant.query.all())
```

### Use `values()` for Partial Field Access

When you only need a few fields, use the `values` parameter. This is 2-4x faster than loading
full model instances because it uses `HMGET` instead of `HGETALL` and skips model instantiation.

```python
# Good: Only fetches name and rating fields
data = Restaurant.query.all(values=("name", "rating"))
# => [{"name": "Burger Palace", "rating": 4.5}, ...]

# Better: If all fields are KeyFields, no Redis commands needed at all
names = Restaurant.query.all(values=("name",))
```

When all requested fields are KeyFields, Popoto extracts values directly from the Redis key
strings without any additional Redis commands.

### Prefer Exact Match and `__in` Over Pattern Filters

Exact match and `__in` lookups use Redis set operations (O(1) per value). Pattern filters
like `__startswith`, `__endswith`, and `__contains` use Redis `SCAN` which iterates through
keys.

```python
# Good: Uses Redis set lookup - O(1)
Restaurant.query.filter(name="Burger Palace")

# Good: Uses SUNION for efficient OR queries
Restaurant.query.filter(name__in=["Burger Palace", "Sushi Zen"])

# Slower: Scans all keys matching pattern
Restaurant.query.filter(name__startswith="B")
```

### Use SortedField for Range Queries

SortedField filters use Redis sorted sets with O(log N + M) time complexity where N is the
index size and M is the result count. This is much faster than filtering in Python.

```python
# Good: Uses Redis ZRANGEBYSCORE - O(log N + M)
MenuItem.query.filter(price__gte=5.0, price__lte=15.0)

# Avoid: Would require loading all items and filtering in Python
# (Popoto doesn't support this pattern, but the principle applies)
```

### Limit Results When Possible

Apply `limit` to reduce the number of objects loaded from Redis. When combined with
`order_by` on a KeyField, the limit is applied before loading objects.

```python
# Good: Only loads 10 objects
top_10 = Restaurant.query.all(order_by="-rating", limit=10)
```

### Lazy Loading for Bulk Queries

When iterating over query results, Popoto uses lazy deserialization by default. Field values
are only decoded from msgpack when you access them. This significantly reduces overhead when
you only use a subset of fields.

```python
# Only decodes 'name' and 'rating' fields, not 'location' or 'cuisine'
for restaurant in Restaurant.query.all():
    print(f"{restaurant.name}: {restaurant.rating}")
```

Fields absent from the stored Redis hash (e.g., a row written before the field was added
to the model, or a hash partially written by a crashed process) resolve to the field's
declared `default` -- or `None` if no default is set -- matching `Model.__init__` semantics.

### Query Performance Summary

| Operation | Redis Command | Time Complexity |
|-----------|--------------|-----------------|
| `count()` (no filters) | SCARD | O(1) |
| `get(key=...)` | HGETALL | O(1) |
| `filter(field=...)` | SMEMBERS | O(1) |
| `filter(field__in=[...])` | SUNION | O(N) where N = total members |
| `filter(indexed_field=...)` | SMEMBERS | O(1) |
| `filter(indexed_field__in=[...])` | SUNION | O(N) where N = total members |
| `filter(field__startswith=...)` | SCAN | O(N) where N = total keys |
| `filter(sorted__gte=...)` | ZRANGEBYSCORE | O(log N + M) |
| `filter(geo=..., radius=...)` | GEORADIUS | O(N + log M) |
| `all()` | SMEMBERS + pipeline HGETALL | O(N) |
| `get_many(redis_keys=...)` | pipeline HGETALL | O(N) where N = number of keys |
| `composite_score(...)` | ZUNIONSTORE + ZREVRANGE + pipeline HGETALL | O(K log K + M) |
| `filter(validity__current=True)` / `filter(validity__as_of=t)` | ZRANGEBYSCORE (x2), intersected | O(log N + M) |
| `filter(validity__current=False)` | the above, plus ZRANGE (x2) over both interval sets to build the complement | O(N) |
| `keyword_search(...)` | Lua BM25 scoring over inverted index | O(T * D) where T = query terms, D = docs per term |
| `fuse(...)` | Rank-merge + pipeline HGETALL | O(sum of list lengths + K log K) |
| `values(...)` on KeyFields only | No Redis call | O(1) |
