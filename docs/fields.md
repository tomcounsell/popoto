# Models and Fields

Models are the foundation of Popoto. They define the structure of your Redis-stored
data using Python classes with field declarations. If you are familiar with Django or
SQLAlchemy, the pattern will feel natural: inherit from `Model`, declare fields as
class attributes, and Popoto handles persistence, indexing, and querying.

Here is a simple restaurant model to illustrate the basics:

```python
from popoto import Model, KeyField, Field, SortedField, GeoField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)
```

Each field type controls how data is validated, stored, and indexed in Redis. This
guide covers every field type and its configuration options, working through a food
delivery system as a running example.

If you need a field type Popoto does not provide, see
[Writing Custom Fields](field-authoring.md) for the `Field` subclassing contract,
including the round-trip protocol a field with independent Redis state must
implement so it works correctly with [Export & Import](guides/export-import.md).

## KeyField

A `KeyField` determines how Popoto stores and retrieves your objects in Redis. The
values of all KeyFields on a model are concatenated to form the Redis key, making
lookups on KeyFields extremely fast -- a direct Redis GET rather than a scan.

For the `Restaurant` model above, a restaurant with `name="Siam Garden"` is stored at
the Redis key `Restaurant:Siam Garden`. Create a restaurant and retrieve it by name:

```python
restaurant = Restaurant.create(
    name="Siam Garden",
    cuisine="Thai",
    rating=4.5,
    location=GeoField.Coordinates(latitude=40.7128, longitude=-74.0060),
)

loaded = Restaurant.load(name="Siam Garden")
print(loaded.cuisine)
# => "Thai"
```

Because `name` is a `KeyField`, the lookup is a single Redis GET -- the fastest
possible read operation. See [Making Queries](query.md) for additional ways to
retrieve instances.

### Changing a KeyField Value

When you change a `KeyField` value and call `save()`, the Redis key itself changes.
Popoto handles the transition automatically: it deletes the old hash, removes the old
key from the class set, migrates all field indexes (sorted sets, geo sets, unique
constraints) from the old key to the new one, and adds the new key to the class set.
Both full saves and partial saves (`save(update_fields=[...])`) handle this correctly.

```python
restaurant = Restaurant.create(name="Taco Shack", cuisine="Mexican", rating=4.0)

# Change the KeyField value
restaurant.name = "Taco Palace"
restaurant.save()

# Old key is cleaned up, new key is active
loaded = Restaurant.load(name="Taco Palace")
print(loaded.cuisine)
# => "Mexican"

# The old key no longer exists
print(Restaurant.load(name="Taco Shack"))
# => None
```

### Datetime KeyFields

A `KeyField` can hold a `datetime`, and its identity is the **instant**, not the
representation you happened to pass. Popoto normalizes the value to UTC to build the
key, so an aware value, its UTC equivalent, and the equivalent naive value all address
one row:

```python
import datetime

BANGKOK = datetime.timezone(datetime.timedelta(hours=7))

class Reading(Model):
    at = KeyField(type=datetime.datetime)
    celsius = Field(type=float)

Reading.create(at=datetime.datetime(2026, 8, 7, 12, 0, tzinfo=BANGKOK), celsius=31.4)

# All three denote the same instant, so all three find the same row.
Reading.query.get(at=datetime.datetime(2026, 8, 7, 12, 0, tzinfo=BANGKOK))
Reading.query.get(at=datetime.datetime(2026, 8, 7, 5, 0, tzinfo=datetime.timezone.utc))
Reading.query.get(at=datetime.datetime(2026, 8, 7, 5, 0))  # naive, read as UTC
```

The **stored value** still keeps the offset you supplied -- only the key is normalized:

```python
loaded = Reading.query.get(at=datetime.datetime(2026, 8, 7, 5, 0))
print(loaded.at.utcoffset())   # => 7:00:00
print(loaded.at.hour)          # => 12
```

Key and value are deliberately different projections: the key answers "which row",
the value answers "what the caller wrote". A naive value is read as UTC, matching how
`SortedField` has scored naive datetimes all along.

Two consequences worth knowing before you rely on this:

- If your data model needs a naive datetime and an aware UTC datetime to be **two
  distinct rows**, a datetime `KeyField` cannot express that. Use a separate
  discriminator field.
- Rows written before Popoto 1.9.0 sit on the old, representation-derived keys. Run
  `Model.audit_datetime_keys()` to see them and `Model.migrate_datetime_keys()` to move
  them; see recipes 19 and 20 in `popoto.models.migrations`.

## Uniqueness

When two restaurants share the same `name`, the second save overwrites the first. If
you need to guarantee that a field value is globally unique across all instances, use
`UniqueKeyField` or `AutoKeyField`.

`UniqueKeyField` enforces a per-value uniqueness constraint. `AutoKeyField` generates
a unique value automatically, ensuring every instance has a distinct key.

```python
from popoto import Model, KeyField, AutoKeyField, UniqueKeyField
from popoto import Field, SortedField, GeoField

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
```

The `Customer` model uses `username` as its primary KeyField and enforces that every
`email` is unique across all customers. The `Driver` model uses `AutoKeyField` so each
driver gets a unique ID without you supplying one.

```python
customer = Customer.create(
    username="foodie42",
    email="foodie42@example.com",
    name="Jane Doe",
)

# Attempting a duplicate email raises an exception
try:
    Customer.create(
        username="another_user",
        email="foodie42@example.com",
        name="Someone Else",
    )
except Exception as e:
    print(e)
    # => UniqueKeyField 'email' value 'foodie42@example.com' already exists
```

Drivers get an auto-generated key, so you never need to supply `driver_id`:

```python
driver = Driver.create(
    name="Carlos",
    phone="+1-555-0101",
    rating=4.8,
    location=GeoField.Coordinates(latitude=40.7580, longitude=-73.9855),
)

print(driver.driver_id)
# => "a1b2c3d4e5f6..."  (auto-generated UUID4 hex)
```

### AutoKeyField ID Strategies

`AutoKeyField` supports multiple ID generation strategies via the `strategy` parameter.
The default strategy is `uuid4` for backward compatibility.

| Strategy | Length | Time-Sortable | Installation |
|----------|--------|---------------|--------------|
| `uuid4`  | 32 chars | No | Built-in (default) |
| `ulid`   | 26 chars | Yes | `pip install popoto[ulid]` |
| `ksuid`  | 27 chars | Yes | `pip install popoto[ksuid]` |

#### UUID4 (Default)

The default strategy generates a 32-character random hexadecimal string using Python's
`uuid.uuid4()`. UUIDs are excellent for general-purpose unique identifiers but are not
time-sortable, meaning queries cannot rely on ID order to determine creation order.

```python
class Article(Model):
    id = AutoKeyField()  # Default: strategy="uuid4"
    title = Field(type=str)

article = Article.create(title="Hello World")
print(article.id)
# => "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  (32 chars)
```

Use UUID4 when:

- You do not need chronological ordering by ID
- You want zero external dependencies
- Backward compatibility with existing Popoto models is important

#### ULID (Time-Sortable)

ULID (Universally Unique Lexicographically Sortable Identifier) generates 26-character
IDs that are time-sortable. The first 10 characters encode a millisecond-precision
timestamp, and the remaining 16 characters are random. ULIDs use Crockford's Base32
encoding for URL-safety and readability.

```bash
pip install popoto[ulid]
```

```python
class Order(Model):
    id = AutoKeyField(strategy="ulid")
    product = Field(type=str)
    quantity = Field(type=int)

order1 = Order.create(product="Widget", quantity=5)
order2 = Order.create(product="Gadget", quantity=3)

print(order1.id)
# => "01ARZ3NDEKTSV4RRFFQ69G5FAV"  (26 chars)

# IDs are lexicographically sortable by creation time
print(order1.id < order2.id)
# => True
```

Use ULID when:

- You need IDs that sort chronologically (e.g., orders, events, logs)
- You want shorter IDs than UUID4 (26 vs 32 characters)
- You need URL-safe identifiers without special characters

#### KSUID (Time-Sortable)

KSUID (K-Sortable Unique Identifier) generates 27-character IDs that are also
time-sortable. KSUIDs encode a timestamp with second-precision and 16 bytes of random
data using Base62 encoding. They have a longer time range than ULIDs (until year 2150).

```bash
pip install popoto[ksuid]
```

```python
class Event(Model):
    id = AutoKeyField(strategy="ksuid")
    name = Field(type=str)
    timestamp = Field(type=str)

event1 = Event.create(name="user_signup", timestamp="2025-01-15T10:30:00Z")
event2 = Event.create(name="purchase", timestamp="2025-01-15T10:31:00Z")

print(event1.id)
# => "0ujsswThIGTUYm2K8FjOOfXtY1K"  (27 chars)

# IDs are lexicographically sortable by creation time
print(event1.id < event2.id)
# => True
```

Use KSUID when:

- You need IDs that sort chronologically
- You prefer Base62 encoding (alphanumeric only, case-sensitive)
- You need the extended time range (valid until year 2150)

#### Choosing a Strategy

| Use Case | Recommended Strategy |
|----------|---------------------|
| General-purpose unique IDs | `uuid4` (default) |
| Event logs, audit trails | `ulid` or `ksuid` |
| Orders, transactions | `ulid` or `ksuid` |
| Time-series data | `ulid` or `ksuid` |
| Existing Popoto models | `uuid4` (backward compatible) |
| Minimal dependencies | `uuid4` (built-in) |
| Shortest IDs | `ulid` (26 chars) |

!!! tip
    Time-sortable IDs like ULID and KSUID are particularly useful when you want to
    query "most recent" records efficiently, since lexicographic sorting on the ID
    field naturally orders by creation time.

## Composite Keys

When no single field is unique, you can use multiple KeyFields to form a composite
key. The combination of all KeyField values must be unique together. This is useful
for junction or reservation models.

```python
from popoto import Model, KeyField, Field
from popoto import Relationship

class Reservation(Model):
    restaurant = KeyField()
    customer = KeyField()
    party_size = Field(type=int)
    notes = Field(type=str, null=True)
```

Two distinct reservations exist as long as the restaurant-customer pair differs:

```python
Reservation.create(
    restaurant="Siam Garden",
    customer="foodie42",
    party_size=4,
    notes="Window seat please",
)

Reservation.create(
    restaurant="Bella Napoli",
    customer="foodie42",
    party_size=2,
)

# Retrieve by the composite key
reservation = Reservation.load(
    restaurant="Siam Garden", customer="foodie42"
)
print(reservation.party_size)
# => 4
```

The Redis key for this instance is `Reservation:Siam Garden:foodie42`.

!!! warning
    If two reservations share the same restaurant *and* customer values, the second
    save silently overwrites the first. Add a `UniqueKeyField` or `AutoKeyField` if
    you need to allow duplicates on the composite fields.

## Instance Equality and Hashing

Models compare by **key identity**, not by field values. Two instances are equal when
they are the same class with the same Redis key, so a saved instance equals a freshly
loaded copy of itself even if other fields differ:

```python
alice = User.create(name="alice", email="alice@example.com")
same = User.query.get(name="alice")

alice == same
# => True   (same key)

User(name="alice", email="a@x.com") == User(name="alice", email="b@y.com")
# => True   (equality ignores non-key fields)

User(name="alice") == User(name="bob")
# => False  (different keys)
```

Instances are hashable by that same key, so they work in sets and as dict keys:

```python
len({alice, same})
# => 1   (equal instances collapse)
```

A `null=True` KeyField stores `None` as a real part of the key — usually alongside an
`AutoKeyField` that keeps the key unique — so those instances compare and hash normally.

The one exception is an instance that has **never been saved and whose KeyFields are all
`None`**. It has no key yet, so it is equal only to itself and cannot be hashed:

```python
User() == User()
# => False  (two distinct instances, neither has a key)

hash(User())
# => TypeError: User instance is unhashable until it has a key
```

Hashing raises rather than falling back to object identity because the instance's hash
would change the moment you saved it, corrupting any set or dict already holding it.
Set a KeyField value or save the instance first.

## Models Without KeyFields

You can declare a model without any explicit KeyField. Popoto automatically adds a
hidden `AutoKeyField` named `_auto_key`, giving every instance a unique UUID4-based
Redis key. This is convenient when you always query by other fields like `SortedField`
or `GeoField`.

!!! note
    The automatically added `_auto_key` uses the default `uuid4` strategy. If you need
    time-sortable IDs, declare an explicit `AutoKeyField(strategy="ulid")` or
    `AutoKeyField(strategy="ksuid")` instead.

The `MenuItem` model uses an explicit `AutoKeyField`, but the effect is the same as
omitting all key fields entirely:

```python
from popoto import Model, AutoKeyField, Field, SortedField
from popoto import Relationship

class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float)
    restaurant = Relationship(Restaurant)
    available = Field(type=bool, default=True)
```

Create items without worrying about key collisions:

```python
pad_thai = MenuItem.create(
    name="Pad Thai",
    price=14.99,
    restaurant=restaurant,
    available=True,
)

green_curry = MenuItem.create(
    name="Green Curry",
    price=16.50,
    restaurant=restaurant,
)

print(pad_thai.item_id)
# => "e5f6a7b8-..."  (auto-generated)
```

!!! tip
    Use `AutoKeyField` when your model represents items that do not have a natural
    unique identifier, such as orders, menu items, or log entries. Consider using
    `strategy="ulid"` or `strategy="ksuid"` for models where chronological ordering
    by ID is useful.

## Field

The base `Field` class stores a typed value with optional validation. If you do not
specify a `type`, it defaults to `str`.

Popoto supports the following types: `int`, `float`, `Decimal`, `str`, `bool`, `list`,
`set`, `tuple`, `dict`, `bytes`, `datetime.date`, `datetime.datetime`, `datetime.time`.

Here are the types used across our food delivery models: `Restaurant.cuisine` is
`str`, `Restaurant.rating` is `float`, `Restaurant.active` is `bool`, `Order.total`
is `float`, and `Order.status` is `str`. Popoto validates field types when you save:

```python
restaurant = Restaurant(name="Bella Napoli", cuisine="Italian", rating=4.2)
restaurant.active = "yes"  # wrong type, should be bool

print(restaurant.is_valid())
# => False
```

## Named Field Shortcuts

Popoto provides shortcut classes so you can avoid the `type=` parameter. These are
functionally identical to `Field(type=...)`.

| Shortcut        | Equivalent               |
|-----------------|--------------------------|
| `IntField`      | `Field(type=int)`        |
| `FloatField`    | `Field(type=float)`      |
| `DecimalField`  | `Field(type=Decimal)`    |
| `StringField`   | `Field(type=str)`        |
| `BooleanField`  | `Field(type=bool)`       |
| `ListField`     | `Field(type=list)`       |
| `SetField`      | `Field(type=set)`        |
| `TupleField`    | `Field(type=tuple)`      |
| `DictField`     | `Field(type=dict)`       |
| `BytesField`    | `Field(type=bytes)`      |
| `DateField`     | `Field(type=date)`       |
| `DatetimeField` | `Field(type=datetime)`   |
| `TimeField`     | `Field(type=time)`       |
| `IndexedField`  | `Field(indexed=True)`    |
| `UniqueField`   | `Field(indexed=True, unique=True)` |

Import shortcuts from `popoto.fields.shortcuts`:

```python
from popoto import Model, KeyField
from popoto.fields.shortcuts import IntField, FloatField, BooleanField, StringField

class Restaurant(Model):
    name = KeyField()
    cuisine = StringField()
    seat_count = IntField()
    avg_price = FloatField()
    active = BooleanField()
```

Use whichever style you prefer -- the behavior is identical.

## Capped ListField (max_length)

When you pass `max_length=N` to a `ListField`, the list is stored in a separate Redis
list key instead of the model hash. This enables efficient `push()` operations using
Redis `LPUSH` + `LTRIM` without reading the full list.

```python
from popoto import Model, KeyField, ListField

class EventLog(Model):
    session_id = KeyField()
    events = ListField(max_length=100)  # Capped at 100 items

# Save the model first
log = EventLog(session_id="abc", events=[])
log.save()

# Push items directly to Redis (newest first)
log.events.push({"action": "click", "target": "button"})
log.events.push({"action": "scroll", "offset": 500})

# Reload to see the data
loaded = EventLog.query.get(session_id="abc")
print(loaded.events)  # [{"action": "scroll", ...}, {"action": "click", ...}]
```

Key behaviors:

- **push()** prepends items (newest first) and automatically trims to `max_length`
- **save()** replaces the entire Redis list with the current field value
- **delete()** cleans up the separate Redis list key
- **Complex types** (tuples, dicts, Decimals) round-trip correctly through push/read
- **Without max_length**, ListField works exactly as before (stored in model hash)
- The model must be saved before calling `push()` (needs a Redis key)

## Null Values

`KeyField` and `SortedField` are required (`null=False`) by default. All other fields
are optional (`null=True`) by default. You can override this with the `null` keyword.

The `Driver` model illustrates this: `driver_id` (AutoKeyField) and `phone`
(UniqueKeyField) are always required, `rating` (SortedField) is required by default,
while `name` (Field) and `location` (GeoField) are optional by default.

```python
driver = Driver(name=None, phone="+1-555-0199", rating=4.0)
print(driver.is_valid())
# => True  (name is optional, None is acceptable)
```

!!! note
    `UniqueKeyField` and `AutoKeyField` cannot be set to `null=True`. Attempting to
    do so raises a `ModelException` at class definition time.

## Default Values

Fields accept a `default` value used when creating instances without specifying that
field. The `Order` model demonstrates this with its `status` field:

```python
order = Order.create(
    customer=customer,
    restaurant=restaurant,
    total=42.50,
)

print(order.status)
# => "pending"
```

Defaults can also be callables. The callable is invoked each time a new instance is
created, ensuring each instance gets a fresh value. This is critical for mutable types
like lists and dicts.

```python
import uuid
from popoto import Model, KeyField, Field

class Restaurant(Model):
    name = KeyField()
    tags = Field(type=list, default=list)        # fresh list per instance
    metadata = Field(type=dict, default=dict)    # fresh dict per instance
    internal_id = Field(default=uuid.uuid4)      # unique UUID per instance
    display_order = Field(type=int, default=lambda: 0)  # lambda also works
```

!!! warning
    Never use a mutable literal as a default (e.g., `default=[]` or `default={}`).
    This shares a single object across all instances. Always use `default=list` or
    `default=dict` instead.

## String Max Length

You can set a maximum character length for string fields. Redis itself has no
practical string length limit, so `max_length` is purely a validation guard.

```python
class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str, max_length=100)
    description = Field(type=str, max_length=500)

item = MenuItem(name="A" * 150)
print(item.is_valid())
# => False
```

!!! tip
    The default `max_length` for string fields is 1024 characters. Set it explicitly
    only when you need a stricter or looser limit.

## SortedField

`SortedField` enables fast range queries using Redis sorted sets. This is one of
Redis's most powerful features, allowing queries like "menu items under $15" or
"restaurants rated above 4.0" without scanning every instance.

A `SortedField` is required for the range filters `__lt`, `__lte`, `__gt`, `__gte`.
See [Making Queries](query.md) for complete filter documentation.

Using the `MenuItem` model (which has `price = SortedField(type=float)`), create some
items and query by price range:

```python
MenuItem.create(name="Pad Thai", price=14.99, restaurant=restaurant)
MenuItem.create(name="Green Curry", price=16.50, restaurant=restaurant)
MenuItem.create(name="Spring Rolls", price=8.99, restaurant=restaurant)
MenuItem.create(name="Mango Sticky Rice", price=9.50, restaurant=restaurant)

# Find affordable items under $10
budget_items = MenuItem.query.filter(price__lt=10.0)
print(len(budget_items))
# => 2

# Find premium items $15 and above
premium = MenuItem.query.filter(price__gte=15.0)
print([item.name for item in premium])
# => ["Pad Thai", "Green Curry"]
```

You can also query restaurant ratings the same way:

```python
# Find highly rated restaurants
top_restaurants = Restaurant.query.filter(rating__gte=4.0)
```

Range queries work with `int`, `float`, `Decimal`, `datetime`, `date`, and `time`.

!!! note "How `datetime` and `time` are scored"
    A `SortedField(type=datetime.datetime)` scores by the instant. Aware and naive
    values sort consistently, because a naive value is treated as UTC when the score
    is derived.

    A `SortedField(type=datetime.time)` scores by seconds since midnight and
    deliberately ignores `tzinfo` — a bare `time` carries no date, so normalizing by
    the offset would wrap past midnight and sort `01:00+07:00` ahead of
    `23:00+00:00` on the same clock face. Use `datetime.datetime` when the UTC
    offset must participate in ordering. (`SortedField(type=datetime.time)` raised
    `AttributeError` on every save before Popoto 1.8.2.)

## SortedKeyField

`SortedKeyField` combines the direct-lookup speed of `KeyField` with the range query
capabilities of `SortedField`. Use it when a field serves as both a primary identifier
and a range-query target.

```python
from popoto import Model, SortedKeyField, Field

class DailySpecial(Model):
    day_number = SortedKeyField(type=int)
    dish = Field(type=str)
    price = Field(type=float)
```

Query by exact key or by range:

```python
DailySpecial.create(day_number=1, dish="Tacos", price=9.99)
DailySpecial.create(day_number=2, dish="Pasta", price=12.99)
DailySpecial.create(day_number=3, dish="Sushi", price=15.99)

# Direct key lookup
monday = DailySpecial.load(day_number=1)
print(monday.dish)
# => "Tacos"

# Range query
early_week = DailySpecial.query.filter(day_number__lte=2)
print(len(early_week))
# => 2
```

## IndexedField

`IndexedField` provides Set-based secondary indexing on non-key fields. Unlike
`KeyField`, an `IndexedField` does not become part of the Redis key -- it only
enables efficient exact-match queries via `filter()`.

This decouples querying from identity: you can filter on `status`, `category`, or
`region` without those fields affecting the Redis storage key.

```python
from popoto import Model, AutoKeyField, IndexedField, Field

class Order(Model):
    order_id = AutoKeyField()
    status = IndexedField(type=str)
    region = IndexedField(type=str, null=True)
    notes = Field(type=str)
```

Query indexed fields with exact match, `__in`, `__isnull`, `__startswith`, and
`__endswith` lookups:

```python
Order.query.filter(status="shipped")
Order.query.filter(status__in=["pending", "processing"])
Order.query.filter(region__startswith="US-")
Order.query.filter(region__isnull=False)
```

You can also enable indexing on a plain `Field` with `indexed=True`:

```python
category = Field(type=str, indexed=True)  # equivalent to IndexedField(type=str)
```

See [Indexed Fields](indexed_fields.md) for full details on index key patterns,
the atomic save guarantee, operator notes, and the comparison table.

## UniqueField

`UniqueField` combines secondary indexing with a per-value uniqueness constraint. It
guarantees that no two model instances share the same value for this field, without
making the field part of the Redis key.

```python
from popoto import Model, AutoKeyField, UniqueField, Field

class User(Model):
    user_id = AutoKeyField()
    email = UniqueField(type=str)
    name = Field(type=str)

user = User.create(email="alice@example.com", name="Alice")

# Duplicate email raises ModelException
try:
    User.create(email="alice@example.com", name="Not Alice")
except Exception as e:
    print(e)
    # => Uniqueness violation on User.email: value 'alice@example.com' is already taken
```

`UniqueField` cannot be null and cannot have `unique=False`. It supports the same
query lookups as `IndexedField`.

See [Indexed Fields](indexed_fields.md) for the concurrency guarantee, operator
notes on the internal bookkeeping field, and the full comparison table.

## DecayingSortedField

`DecayingSortedField` is a `SortedField` subclass where records lose relevance over time
following a power-law decay curve. The sorted set score is always a timestamp, and a
Lua script computes decay-ranked results server-side:

```
decayed_score = base_score × elapsed_days ^ (-decay_rate)
```

With the default `decay_rate=0.1`, a record scores 1.0 after 1 day, 0.87 after 4 days, and 0.63 after 100 days.

```python
from popoto import Model, KeyField, Field, FloatField
from popoto.fields.decaying_sorted_field import DecayingSortedField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
```

Query for the most relevant recent records with `top_by_decay()`. When a model has
exactly one `DecayingSortedField`, the field name is auto-detected:

```python
# Auto-detect field_name (works because Memory has exactly one DecayingSortedField)
top = Memory.query.filter(agent_id="agent-1").top_by_decay(n=10)

# Explicit field_name also works
top = Memory.query.filter(agent_id="agent-1").top_by_decay("relevance", n=10)

# Override decay rate for this query (aggressive — only very recent)
hot = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5, decay_rate=1.0)
```

Refresh a record's timestamp without a full save using `touch()`:

```python
memory = Memory.query.get(agent_id="agent-1", content="deployment procedure")
memory.touch("relevance")  # Resets the decay clock
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.1` | Controls how fast scores drop. Higher = faster decay. Must be > 0. Empirically tuned; see [Tuning Magic Numbers](guides/tuning-magic-numbers.md). |
| `base_score_field` | `str` | `None` | Name of a companion field whose value multiplies the decay curve. When `None`, base score is 1.0. |
| `confidence_modulation_field` | `str`, `False`, or `None` | `None` | Which `ConfidenceField` modulates each record's effective decay rate. `None` auto-detects a single `ConfidenceField` on the model; a `str` names one; `False` disables. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values (inherited from `SortedField`). |

Use `InteractionWeight` constants with `base_score_field` for source/role-based importance
weighting in multi-agent teams. See [DecayingSortedField — Source weighting](features/decaying-sorted-field.md#source-weighting-with-interactionweight)
for the full pattern.

**Confidence-modulated decay.** `base_score_field` scales the curve's magnitude, which
never changes relative order. Confidence modulation changes the *rate*, so accumulated
outcome evidence alters how fast a record actually leaves the corpus:

```
eff   = decay_rate × 2 ^ (s × 2 × (c0 − c))
score = base_score × t ^ (−decay_rate) × max(t, 1.0) ^ (−(eff − decay_rate))
```

where `c` is the record's `ConfidenceField` value, `c0` is that field's own
`initial_confidence`, `s` is `Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH` (0.5), and
`t` is `elapsed_days`.

```python
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    agent_id = KeyField()
    content = Field(type=str)
    certainty = ConfidenceField()      # exactly one -> modulation is ON, no config
    relevance = DecayingSortedField()

# Or name it explicitly / opt out:
#   DecayingSortedField(confidence_modulation_field="certainty")
#   DecayingSortedField(confidence_modulation_field=False)
```

Modulation is **default-on**: a model with exactly one `ConfidenceField` gets it with no
configuration; zero `ConfidenceField`s means off; two or more with no explicit kwarg means
off plus a warning naming the candidates. The deploy-level kill switch disables it
everywhere without editing model definitions:

```python
from popoto.fields.constants import Defaults

Defaults.DECAY_CONFIDENCE_MODULATION_ENABLED = False
```

Scores are **byte-identical** to the unmodulated formula when a record has no confidence
evidence, when the model has no `ConfidenceField`, when the kwarg is `False`, when the kill
switch is off, or when `s = 0` — the correction term is exactly `1.0` at `c == c0`. Disabled
paths skip the per-member `HGET` entirely.

The `max(t, 1.0)` clamp is load-bearing, not cosmetic. `elapsed_days` is floored at `0.01`,
and for `t < 1` the term `t^(−rate)` is a multiplier greater than 1 that a *larger* rate
amplifies *more*. Without the clamp, modulation would run backwards for the first 24 hours
and boost exactly the low-confidence records it exists to bury.

!!! warning "Rank inversion"
    Rate modulation makes two records' log-log score lines cross exactly once, so a record's
    **rank can improve over time even as its score falls** — behavior magnitude weighting
    never produces. Cached top-N snapshots drift accordingly; re-run `top_by_decay()` rather
    than caching its output if ordering stability matters.

If the `ConfidenceField` declares `partition_by`, the query must filter on those fields or
`top_by_decay()` raises `QueryException` naming the missing filters — modulation never
silently degrades to partial coverage.

All standard `SortedField` range filters (`__gt`, `__gte`, `__lt`, `__lte`, `__between`)
work against the timestamp score. See [Agent Memory](features/agent-memory.md) for the
full agent memory primitives overview.

**Ordering** is deterministic: `top_by_decay()` returns results by decayed score
descending, with equal scores tie-broken by `redis_key` ascending (byte-wise)
inside the Lua script -- before the top-N truncation, so identical queries always
return the identical ordering, including which members survive the `n` cutoff.

## CyclicDecayField

`CyclicDecayField` extends `DecayingSortedField` with two additional temporal forces computed
atomically in a single Lua script:

1. **Cyclical resonance**: Periodic boosts following cosine curves.
2. **Homeostatic pressure**: Urgency that builds linearly while an item goes unresolved.

The effective score is: `decay + cyclic_resonance + pressure`. When `cycles=[]` and
`pressure_rate=0.0`, behavior is identical to `DecayingSortedField`.

```python
from popoto import Model, KeyField, Field, CyclicDecayField
from popoto.fields.constants import TemporalPeriod

class Directive(Model):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = CyclicDecayField(
        decay_rate=0.5,  # override default (0.1) for faster forgetting
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
        pressure_rate=0.1,
    )
```

Query with the same `top_by_decay()` interface, and discharge pressure with `resolve_pressure()`:

```python
# Top 10 by combined decay + cyclic + pressure score (field_name auto-detected)
top = Directive.query.filter(agent_id="agent-1").top_by_decay(n=10)

# Discharge accumulated urgency
directive.resolve_pressure("relevance")

# Refresh the decay clock (same as DecayingSortedField)
directive.touch("relevance")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `decay_rate` | `float` | `0.1` | Power-law decay exponent (inherited). |
| `base_score_field` | `str` | `None` | Companion field whose value multiplies the decay curve (inherited). |
| `confidence_modulation_field` | `str`, `False`, or `None` | `None` | Which `ConfidenceField` modulates the per-record decay rate (inherited). |
| `cycles` | `list` | `[]` | List of `(period, amplitude, phase)` tuples. Use `TemporalPeriod` constants for period. |
| `pressure_rate` | `float` | `0.0` | Rate of urgency buildup per unresolved day. |
| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values (inherited). |

**Ordering** is deterministic, same as `DecayingSortedField`: equal effective
scores (`decay + cyclic + pressure`) are tie-broken by `redis_key` ascending
(byte-wise) inside the Lua script, before the top-N truncation.

**Confidence-modulated decay** is inherited unchanged and applies to the `decay` term only;
`cyclic` and `pressure` are untouched. Auto-detection, the `confidence_modulation_field`
kwarg, the `DECAY_CONFIDENCE_MODULATION_ENABLED` kill switch, bit-exact neutrality, and the
rank-inversion caveat all behave exactly as documented for `DecayingSortedField` above.
One internal difference matters if you read the Lua: `CyclicDecayField` forks the decay
script and already binds `KEYS[2]` = cycles hash and `KEYS[3]` = pressure hash, so the
confidence `:data` hash is `KEYS[4]` there rather than `KEYS[2]` (`ARGV` indices are the same
in both). The two scripts must not be "unified" on `KEYS[2]` — that would unpack the cycles
array as a confidence payload and corrupt scores silently.

See [CyclicDecayField feature docs](features/cyclic-decay-field.md) for the full reference including
the scoring formula, Redis data model, `TemporalPeriod` constants, and error handling.
See [Agent Memory](features/agent-memory.md) for the broader agent memory primitives overview.

## CoOccurrenceField

`CoOccurrenceField` maintains weighted association edges between model instances using
per-PK Redis sorted sets. Weights strengthen via co-retrieval and decay when not
reinforced. A server-side Lua BFS script enables multi-hop associative retrieval.

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.co_occurrence_field import CoOccurrenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    associations = CoOccurrenceField(symmetric=True, max_edges=100)

# Create and link
mem_a = Memory.create(key="ml", content="Machine learning")
mem_b = Memory.create(key="nn", content="Neural networks")
field = Memory._meta.fields["associations"]
field.link(Memory, mem_a.db_key.redis_key, mem_b.db_key.redis_key)

# Propagate associations
scores = field.propagate(Memory, [mem_a.db_key.redis_key], depth=2)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symmetric` | `bool` | `True` | If True, edges are bidirectional. |
| `max_edges` | `int` | `500` | Maximum edges per PK; lowest-weight pruned when exceeded. |
| `decay_factor` | `float` | `0.95` | Default multiplicative decay for `weaken_all()`. |

See [CoOccurrenceField docs](features/co-occurrence-field.md) for the full reference including
methods, Redis key patterns, and synergy with other memory fields.
See [Agent Memory](features/agent-memory.md) for the broader agent memory primitives overview.

## ExistenceFilter

`ExistenceFilter` is a Bloom filter for O(1) probabilistic membership checks. It answers
"have I ever stored anything about X?" without touching any sorted set or hash. False
positives are possible; false negatives are impossible.

Implemented entirely with Redis strings (`SETBIT`/`GETBIT`) and Lua scripts. No Redis
modules required -- works on both Redis and Valkey.

`ExistenceFilter` is a "side-effect field" -- it does not store a value on the model
instance. It maintains a Bloom filter index via `on_save()` hooks.

```python
from popoto import Model, KeyField, Field
from popoto.fields.existence_filter import ExistenceFilter

class Memory(Model):
    agent_id = KeyField()
    topic = Field(type=str)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.topic,
    )
```

Check membership before running expensive queries:

```python
# Fast pre-check before expensive retrieval
if not Memory.bloom.definitely_missing(Memory, "kubernetes deployments"):
    results = Memory.query.filter(agent_id="agent-1").top_by_decay(5)
else:
    results = []  # skip retrieval entirely
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `error_rate` | `float` | `0.01` | Target false positive rate. Lower = more bits required. |
| `capacity` | `int` | `100_000` | Expected number of distinct items. Exceeding this degrades the error rate. |
| `fingerprint_fn` | `Callable` | `None` | Takes a model instance, returns a string fingerprint. Falls back to `redis_key` if not set. |

| Method | Returns | Description |
|--------|---------|-------------|
| `might_exist(model_class, fingerprint)` | `bool` | True if fingerprint is possibly present (may be false positive). |
| `definitely_missing(model_class, fingerprint)` | `bool` | True if fingerprint is guaranteed absent. |
| `fill_ratio(model_class)` | `float` | Proportion of set bits (0.0-1.0). Monitor for capacity warnings. |

**Tokenization:** Fingerprints are automatically tokenized on save. A fingerprint like
`"kubernetes deployment guide"` is split into individual tokens (`"kubernetes"`,
`"deployment"`, `"guide"`), each added to the bloom filter separately. This enables
word-level queries: `might_exist("kubernetes")` returns True after saving that fingerprint.
Queries are normalized with the same rules (lowercase, stop word removal, min-length 3).
See [ExistenceFilter feature docs](features/existence-filter.md#tokenization) for details.

`on_delete()` is a no-op -- Bloom filters do not support removal. Stale positives are
harmless for a pre-filter use case.

See [ExistenceFilter](features/existence-filter.md) for the
full agent memory context.

## BM25Field

`BM25Field` provides ranked keyword search using BM25 scoring, backed entirely by Redis
sorted sets and Lua scripts. No Redis modules required -- works on both Redis and Valkey.

Like `ExistenceFilter`, `BM25Field` is a "side-effect field" -- it does not store a value
on the model instance. It maintains an inverted index and corpus statistics via
`on_save()`/`on_delete()` hooks, and computes BM25 scores at query time server-side.

```python
from popoto import Model, AutoKeyField
from popoto.fields.bm25_field import BM25Field
from popoto.fields.content_field import ContentField

class Memory(Model):
    key = AutoKeyField()
    raw_content = ContentField()
    content_bm25 = BM25Field(source="raw_content")
```

Search via `keyword_search()` on the query builder:

```python
results = Memory.query.keyword_search("redis deployment timeout", limit=10)
for memory in results:
    print(f"{memory.key}: {memory._bm25_score:.3f}")
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | `str` | Yes | Name of the field to read content from for indexing. |

| Class Constant | Default | Description |
|----------------|---------|-------------|
| `BM25_K1` | `1.2` | Term frequency saturation parameter. |
| `BM25_B` | `0.75` | Document length normalization parameter (0 = none, 1 = full). |

| Method | Returns | Description |
|--------|---------|-------------|
| `search(model_class, field_name, query_text, limit)` | `list[tuple[str, float]]` | Raw BM25 search returning `(redis_key, score)` tuples. |
| `recompute_stats(model_class, field_name)` | `None` | Recompute avgdl/n from scratch to correct floating-point drift. |

**Ordering** is deterministic: results are sorted by BM25 score descending, with equal
scores tie-broken by `redis_key` ascending (byte-wise) inside the Lua script -- so
identical searches always return identical orderings, including at the `limit` cutoff,
on both Redis and Valkey.

**Tokenization** uses the same shared tokenizer as ExistenceFilter (`fields/_tokenizer.py`):
lowercase, split on non-word characters, filter tokens shorter than 3 characters, remove
stop words. BM25Field preserves duplicate tokens (`unique=False`) for accurate term
frequency counts.

See [Hybrid Retrieval](features/hybrid-retrieval.md) for the full feature documentation
including RRF fusion and the hybrid retrieval recipe.

## FrequencySketch

`FrequencySketch` is a Count-Min Sketch for approximate frequency counting. Tracks how
many times a fingerprint has been saved, with possible overcounting but never undercounting.

Implemented entirely with Redis hashes (`HINCRBY`/`HGET`) and Lua scripts. No Redis
modules required -- works on both Redis and Valkey.

```python
from popoto import Model, KeyField, Field
from popoto.fields.existence_filter import FrequencySketch

class Memory(Model):
    agent_id = KeyField()
    topic = Field(type=str)
    freq = FrequencySketch(
        fingerprint_fn=lambda inst: inst.topic,
    )
```

Query approximate frequency:

```python
count = Memory.freq.get_frequency(Memory, "kubernetes")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `width` | `int` | `2000` | Number of counters per row. Higher = less overcounting. |
| `depth` | `int` | `7` | Number of hash functions (rows). Higher = more accurate. |
| `fingerprint_fn` | `Callable` | `None` | Takes a model instance, returns a string fingerprint. Falls back to `redis_key` if not set. |

| Method | Returns | Description |
|--------|---------|-------------|
| `get_frequency(model_class, fingerprint)` | `int` | Approximate count. May overcount, never undercounts. |

`on_delete()` is a no-op -- CMS counters are monotonically increasing.

Both `ExistenceFilter` and `FrequencySketch` can be used together on the same model.
See [Agent Memory](features/agent-memory.md) for
the full agent memory context.

## PredictionLedgerMixin

`PredictionLedgerMixin` adds prediction recording, resolution, and error tracking to any
Popoto model. Agents record predictions before acting, then resolve them against actual
outcomes. The mixin computes prediction error and feeds it back into `ConfidenceField`
when error is high.

```python
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.prediction_ledger import PredictionLedgerMixin
from popoto.fields.confidence_field import ConfidenceField

class Memory(PredictionLedgerMixin, Model):
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField()

    _pl_partition = "default"
```

Record a prediction and resolve it:

```python
memory = Memory.create(key="fact1", content="sky is blue")
PredictionLedgerMixin.record_prediction(memory, predicted={"relevance": 0.9})
error = PredictionLedgerMixin.resolve_prediction(memory, actual={"relevance": 0.3})
# error ≈ 0.6
```

Auto-resolution from `ObservationProtocol` outcomes (`acted`, `dismissed`, `contradicted`)
is handled automatically when the model uses both mixins. Resolution is idempotent --
resolving an already-resolved prediction is a no-op.

| Method | Returns | Description |
|--------|---------|-------------|
| `record_prediction(instance, predicted, pipeline=None)` | `None` | Store a prediction for a saved instance |
| `resolve_prediction(instance, actual, pipeline=None)` | `float` or `None` | Resolve with actual values, returns error |
| `auto_resolve(instance, outcome, pipeline=None)` | `float` or `None` | Resolve using outcome-to-error mapping |
| `get_prediction_data(instance)` | `dict` or `None` | Read current prediction metadata |
| `get_highest_errors(model_class, partition, limit)` | `list` | Query instances with highest errors |
| `compute_prediction_error(predicted, actual)` | `float` | Overridable error computation |

Redis keys: `$PL:{ClassName}:meta:{pk}` (hash), `$PL:{ClassName}:errors:{partition}` (sorted set).

Implemented entirely with Redis hashes, sorted sets, and Lua scripts. No Redis modules
required -- works on both Redis and Valkey.

See [PredictionLedger](features/prediction-ledger.md) for
the full agent memory context.

## partition_by

When you always query a `SortedField` together with a specific `KeyField`, you can
dramatically improve performance by defining `partition_by`. This creates a composite index
scoped to the values of the fields listed in the tuple.

The tradeoff is that queries on this `SortedField` *must* include the `partition_by`
fields. This is ideal for the `MenuItem.price` field, where you typically filter items
for a specific restaurant.

```python
class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float, partition_by=('restaurant',))
    restaurant = Relationship(Restaurant)
    available = Field(type=bool, default=True)
```

Now price queries must include the restaurant:

```python
# Fast query: uses the composite index
affordable = MenuItem.query.filter(
    restaurant=restaurant,
    price__lt=12.00,
)

# This would fail because 'restaurant' is required by partition_by
try:
    MenuItem.query.filter(price__lt=12.00)
except Exception as e:
    print(e)
```

!!! tip
    Use `partition_by` when you have a natural parent-child relationship. Menu items
    always belong to a restaurant, so scoping the price index to the restaurant
    reduces the sorted set size and speeds up range queries. See
    [Multi-Tenancy](multi-tenancy.md) for using `partition_by` with a KeyField
    to isolate data by tenant or project.

!!! note "Partition key changes are handled automatically"
    If you change a partition field's value (e.g., moving an item from one restaurant
    to another), Popoto automatically removes the entry from the old partition's sorted
    set and adds it to the new one. No manual cleanup is needed.

!!! note "Deprecation Notice"
    The `sort_by` parameter is deprecated and will be removed in a future major version.
    Use `partition_by` instead. `sort_by` still works but emits a `DeprecationWarning`.

## DatetimeField

`DatetimeField` extends the base `Field` with automatic timestamp management.
It supports two special parameters: `auto_now_add` and `auto_now`.

- `auto_now_add=True` -- Sets the field to the current datetime on first save only.
- `auto_now=True` -- Updates the field to the current datetime on every save.

The `Order` model uses both:

```python
from popoto.fields.datetime_field import DatetimeField

class Order(Model):
    order_id = AutoKeyField()
    # ... other fields ...
    created_at = DatetimeField(auto_now_add=True)  # set on first save
    updated_at = DatetimeField(auto_now=True)       # refreshed on every save
```

Timestamps are managed automatically:

```python
order = Order.create(
    customer=customer,
    restaurant=restaurant,
    total=28.99,
)

print(order.created_at)
# => 2025-06-15 14:30:00.123456+00:00

print(order.updated_at)
# => 2025-06-15 14:30:00.123456+00:00

# Update the order status
order.status = "confirmed"
order.save()

print(order.created_at)
# => 2025-06-15 14:30:00.123456+00:00  (unchanged)

print(order.updated_at)
# => 2025-06-15 14:32:10.654321+00:00  (refreshed)
```

!!! note "Timezone awareness"
    `auto_now` / `auto_now_add` stamp `datetime.now(timezone.utc)`, and the value
    round-trips **aware** — `order.created_at` is still an aware `datetime` after a
    reload rather than silently becoming naive. Compare it against
    `datetime.now(timezone.utc)`, not `datetime.now()`; comparing an aware value to
    a naive one raises `TypeError`.

    This holds for any `datetime.datetime` or `datetime.time` field, not just
    `auto_now`: save an aware value and it comes back aware, save a naive one and it
    comes back naive. `datetime.date` has no time-of-day component and is
    unaffected. A `ZoneInfo` tzinfo comes back as the fixed UTC offset in effect at
    that instant rather than as the zone itself.

    Rows written before Popoto 1.8.2 still decode naive, because their offset was
    never stored — so a dataset spanning that upgrade can hold both shapes. If you
    rely on `SortedField` ordering over `datetime` values, run
    `Model.rebuild_indexes()` after upgrading.

See [Model Meta Options](meta.md) for configuring `order_by` and `ttl` on the `Meta`
class.

## Timestampable Mixin

If you find yourself adding `created_at` and `updated_at` to many models, use the
`Timestampable` mixin to include both fields automatically. It provides
`DatetimeField(auto_now_add=True)` for `created_at` and
`DatetimeField(auto_now=True)` for `updated_at`.

```python
from popoto import Model, KeyField, Field
from popoto.utils.mixins.timestampable import Timestampable

class Restaurant(Timestampable, Model):
    name = KeyField()
    cuisine = Field(type=str)
    # created_at and updated_at are included automatically
```

## WriteFilterMixin

`WriteFilterMixin` gates `save()` calls based on a scoring function you define. Records
scoring below a minimum threshold are silently discarded (never persisted). Records
scoring above a priority threshold are persisted *and* tagged in a Redis sorted set for
preferential retrieval.

```python
from popoto import Model, KeyField, Field
from popoto.fields.write_filter import WriteFilterMixin

class Memory(WriteFilterMixin, Model):
    agent_id = KeyField()
    content = Field(type=str)
    importance = Field(type=float, default=0.5)

    def compute_filter_score(self):
        return self.importance or 0.0
```

The mixin adds three behaviors to your model:

1. **Gate on save**: Before persisting, `compute_filter_score()` is called. If the
   score is below `_wf_min_threshold` (default `0.1`), a `SkipSaveException` is raised and caught by `Model.save()`,
   silently aborting the write.

2. **Priority tagging**: If the score meets or exceeds `_wf_priority_threshold`
   (default 0.7), the record's Redis key is added to a sorted set at
   `$WF:{ClassName}:priority` with the score as its rank.

3. **Cleanup on delete**: When `delete()` is called, the record is removed from the
   priority sorted set automatically.

```python
# Silently discarded — score 0.05 < min_threshold 0.1
low = Memory(agent_id="a1", content="noise", importance=0.05)
low.save()  # No error, but record is NOT in Redis

# Persisted normally — score 0.5 between thresholds
mid = Memory(agent_id="a1", content="useful", importance=0.5)
mid.save()  # Stored in Redis

# Persisted AND priority-tagged — score 0.9 >= priority_threshold 0.7
high = Memory(agent_id="a1", content="critical", importance=0.9)
high.save()  # Stored in Redis AND added to $WF:Memory:priority
```

Override the thresholds by setting class attributes:

```python
class StrictMemory(WriteFilterMixin, Model):
    _wf_min_threshold = 0.4       # Higher bar to persist
    _wf_priority_threshold = 0.8  # Higher bar for priority
    # ...
```

| Attribute | Default | Description |
|-----------|---------|-------------|
| `_wf_min_threshold` | `0.1` | Minimum score to persist. Empirically tuned; see [Tuning Magic Numbers](guides/tuning-magic-numbers.md). |
| `_wf_priority_threshold` | `0.7` | Minimum score for priority tagging |

!!! tip
    `WriteFilterMixin` must appear **before** `Model` in the inheritance list so that
    its `on_save()` hook is called. The scoring function is application logic — Popoto
    provides the gating mechanism, not the scoring logic.

See [Agent Memory](features/agent-memory.md) for the broader
agent memory context and how WriteFilter fits with DecayingSortedField and AccessTracker.

## ContentField

`ContentField` routes large content values (documents, text, binary data) to filesystem
storage, keeping Redis memory usage minimal. Redis stores only a compact reference string
(`$CF:{hash}:{path}`), and the content is lazy-loaded from the filesystem when the
attribute is accessed.

This is ideal for storing long-form text, HTML, markdown, or any content too large to
keep in Redis comfortably.

```bash
pip install popoto
```

No additional dependencies are needed -- ContentField uses the filesystem by default.

```python
import popoto
from popoto.fields.content_field import ContentField

class Document(popoto.Model):
    name = popoto.KeyField()
    body = ContentField()

doc = Document.create(name="readme", body="# Hello World\n\nThis is a large document...")
```

On save, the content is written to the filesystem first, then a reference string is stored
in Redis. On attribute access, the reference is detected and the content is transparently
loaded from the filesystem:

```python
loaded = Document.query.get(name="readme")
print(loaded.body)
# => "# Hello World\n\nThis is a large document..."
```

### Content-Addressable Storage

ContentField uses SHA-256 hashing for content-addressable storage. Identical content
produces the same hash and file path, so duplicate writes are deduplicated automatically.
Writes are atomic (temp file + rename) to prevent partial reads.

### Custom Content Store

By default, ContentField uses `FilesystemStore` which writes to `~/.popoto/content/`
(or the path set via `POPOTO_CONTENT_PATH`). You can pass a custom store per-field
or set a global default via `popoto.configure()`:

```python
from popoto.stores.filesystem import FilesystemStore

class Document(popoto.Model):
    name = popoto.KeyField()
    body = ContentField(store=FilesystemStore(base_path="/data/documents"))
```

Or configure globally:

```python
popoto.configure(content_path="/data/popoto-content")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `store` | `AbstractContentStore` or `"filesystem"` | `"filesystem"` | The content store backend. |

ContentField deletion is a no-op -- content files are append-only. Use
`ContentField.garbage_collect(ModelClass)` to remove orphaned files not referenced
by any live model instance.

## EmbeddingField

`EmbeddingField` generates vector embeddings from a source field on save, stores them
as `.npy` files on the filesystem, and maintains an in-memory cache of pre-normalized
numpy matrices for fast cosine similarity computation at query time.

```bash
pip install popoto[embeddings]          # numpy
pip install popoto[voyage]              # numpy + voyageai
pip install popoto[openai]              # numpy + openai
```

```python
import popoto
from popoto.fields.content_field import ContentField
from popoto.fields.embedding_field import EmbeddingField
from popoto.embeddings.voyage import VoyageProvider

popoto.configure(
    embedding_provider=VoyageProvider(api_key="your-key"),
)

class Memory(popoto.Model):
    topic = popoto.KeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")

m = Memory.create(topic="revenue", content="Q4 revenue exceeded projections...")
# Embedding is generated automatically on save
```

Redis stores only the embedding dimension count (an integer). The actual vector
is stored as a `.npy` file under `~/.popoto/content/.embeddings/`. On save,
EmbeddingField reads the source field value, calls the configured provider to
generate an embedding, and writes the vector atomically to disk.

### Embedding Providers

Popoto ships with four built-in providers. You can also implement your own by
subclassing `AbstractEmbeddingProvider`.

**VoyageProvider** (recommended for retrieval):

```python
from popoto.embeddings.voyage import VoyageProvider

provider = VoyageProvider(
    api_key="your-voyage-api-key",
    model="voyage-3-lite",           # default
    dimensions=512,                   # default
)
```

**OpenAIProvider**:

```python
from popoto.embeddings.openai import OpenAIProvider

provider = OpenAIProvider(
    api_key="your-openai-api-key",
    model="text-embedding-3-small",  # default
    dimensions=1536,                  # default
)
```

**OllamaProvider** (local, no API key):

```python
from popoto.embeddings.ollama import OllamaProvider

provider = OllamaProvider(
    base_url="http://localhost:11434",  # default
    model="nomic-embed-text",           # default (768-dim)
    dim=None,                           # auto-detect on first embed()
)
```

Requires a running Ollama server (`ollama serve`) with the model pulled
(`ollama pull nomic-embed-text`). Uses stdlib only -- no extras to install.
See [Content and Embedding Fields](features/content-and-embedding-fields.md#ollamaprovider)
for setup details.

**SentenceTransformersProvider** (local, no API key):

```python
from popoto.embeddings.sentence_transformers import SentenceTransformersProvider

provider = SentenceTransformersProvider(
    model_name="all-MiniLM-L6-v2",   # default (384-dim)
)
```

Runs `all-MiniLM-L6-v2` locally via `sentence-transformers` (installed by the
`[benchmark]` extra: `pip install popoto[benchmark]`). No API key or daemon; the
model (~90 MB) is downloaded from Hugging Face on first use and cached. Used by the
external benchmark harness for hybrid retrieval — see
[Benchmarking](benchmarks.md#retrieval-modes).

### Querying with semantic_search()

Once models have embeddings, use `semantic_search()` to find semantically similar
instances. See [Making Queries -- semantic_search()](query.md#semantic-search) for the
full query interface.

```python
results = Memory.query.semantic_search("revenue trends", limit=5)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source` | `str` | `None` | Name of the field to read content from for embedding generation. |
| `provider` | `AbstractEmbeddingProvider` | `None` | Provider instance, or None to use the global default set via `popoto.configure()`. |
| `auto_embed` | `bool` | `True` | Generate embeddings automatically on save. |
| `cache` | `bool` | `True` | Cache embeddings in memory for fast similarity search. |

See [Content and Embedding Fields](features/content-and-embedding-fields.md) for the
full feature reference including storage layout, cache management, and provider API.

### Multi-Worker Deployments

`EmbeddingField` caches a pre-normalized embedding matrix in a **process-local**
dict for fast similarity search. In a multi-worker deployment (gunicorn with
several workers, multiple containers, multiple pods) a write on one worker must
invalidate the caches held by its peers — otherwise the peers keep serving
semantic-search results that omit newly written records or include deleted ones,
with no error signal.

The mechanism is selected via the `POPOTO_EMBEDDING_INVALIDATION` environment
variable:

```bash
POPOTO_EMBEDDING_INVALIDATION=pubsub   # default
POPOTO_EMBEDDING_INVALIDATION=mtime    # batch/offline, no live Valkey needed
POPOTO_EMBEDDING_INVALIDATION=none     # single-process, zero overhead
```

**`pubsub` (default)** — On every save/delete, the writing worker publishes to a
per-class Valkey channel `popoto:embedding:invalidate:{ModelName}`. Each worker
runs a daemon subscriber thread (lazily started on its first `semantic_search()`)
that drops the local cache when a peer publishes. Uses only core Valkey/Redis
`PUBLISH`/`SUBSCRIBE` (no Redis modules), so it works on both Redis and Valkey.

**`mtime`** — No Valkey connection required. Before returning a cached matrix,
the worker checks a monotonic integer `_version` counter stored in the
`_index.json` sidecar (an `os.stat()` mtime check is used only as a cheap "did
anything change at all" pre-check). The integer counter is **granularity-proof**:
it detects a peer write even when both writes fall within the same filesystem
mtime tick (a hazard on 1-second-resolution filesystems such as older HFS+ or
some NFS mounts — not just NFS).

**`none`** — Restores the original single-process behavior: no subscriber thread,
no extra Valkey connection, no `PUBLISH`, no `os.stat()`. Cross-process writes are
never observed.

#### Staleness window

| Mode | Staleness window | Notes |
|------|------------------|-------|
| `pubsub` (default) | Valkey RTT + worker poll interval (`sleep_time=0.1`), i.e. ≤ ~100 ms | Requires a live Valkey connection. If the subscriber thread cannot start (Valkey down, ACL restriction, pool exhaustion), it **degrades to the on-disk `_version` check** — never back to the stale-cache bug. After a connection drop, the window extends to the next `semantic_search()` on that worker (the listener self-heals via lazy respawn). |
| `mtime` | Next `semantic_search()` after the write lands on disk | One `os.stat()` per search (~0.4 µs). The integer `_version` counter eliminates the same-mtime-tick hazard on every filesystem. |
| `none` | Never invalidated across processes | Pre-fix single-process behavior. Zero pub/sub overhead, zero extra threads. |

#### Cost of the default for single-process apps

The default `pubsub` mode is **not** byte-for-byte free for a single-process
deployment: it starts one daemon thread per model class, holds one extra Valkey
connection per such class, and issues one `PUBLISH` per save/delete that loops
back to itself (the handler recognizes its own per-process worker id and skips the
message, so no redundant reload occurs). The functional *results* are identical to
the pre-fix behavior in all three modes for a single process; only the resource
profile differs. Single-process apps that want zero overhead should set
`POPOTO_EMBEDDING_INVALIDATION=none`.

Because each listener holds one checked-out connection from the client
connection pool for its lifetime, a process that creates **many** model classes
with `EmbeddingField` (test harnesses, benchmarks, dynamic class factories) can
exhaust the pool and starve unrelated Redis operations. Call
`popoto.fields.embedding_field.stop_invalidation_listeners()` when done with a
batch of model classes to stop every listener and return its connection to the
pool; the next `semantic_search()` lazily respawns a fresh listener if needed.

## GeoField

`GeoField` uses Redis geospatial indexes for location-based queries. This is perfect
for finding nearby restaurants, tracking driver positions, or searching by delivery
address.

Values are `GeoField.Coordinates(latitude, longitude)` namedtuples, though plain
`(latitude, longitude)` tuples also work.

Using the `Restaurant` model (which has `location = GeoField()`), create restaurants
with locations and search by radius:

```python
siam = Restaurant.create(
    name="Siam Garden",
    cuisine="Thai",
    rating=4.5,
    location=GeoField.Coordinates(latitude=40.7484, longitude=-73.9857),
)

bella = Restaurant.create(
    name="Bella Napoli",
    cuisine="Italian",
    rating=4.2,
    location=GeoField.Coordinates(latitude=40.7527, longitude=-73.9772),
)

taco = Restaurant.create(
    name="Taco Loco",
    cuisine="Mexican",
    rating=4.0,
    location=GeoField.Coordinates(latitude=40.7589, longitude=-73.9851),
)

# Find restaurants within 2km of Midtown Manhattan
nearby = Restaurant.query.filter(
    location=(40.7505, -73.9834),
    location_radius=2,
    location_radius_unit='km',
)

print(len(nearby))
# => 3
```

Supported radius units are `"m"` (meters), `"km"` (kilometers), `"ft"` (feet), and
`"mi"` (miles).

## Query with Distances

Add `{field_name}_with_distances=True` to include distance information in results.
Each returned object gets `_geo_distance` and `_geo_distance_unit` attributes, and
results are sorted closest first.

```python
results = Restaurant.query.filter(
    location=(40.7505, -73.9834),
    location_radius=5,
    location_radius_unit='km',
    location_with_distances=True,
)

for r in results:
    print(f"{r.name}: {r._geo_distance} {r._geo_distance_unit}")
# => Siam Garden: 0.3 km
# => Bella Napoli: 0.7 km
# => Taco Loco: 0.9 km
```

You can also use a model instance as the center point with `_member`:

```python
results = Restaurant.query.filter(
    location_member=siam,
    location_radius=3,
    location_radius_unit='km',
    location_with_distances=True,
)

for r in results:
    print(f"{r.name}: {r._geo_distance} km")
# => Siam Garden: 0.0 km
# => Bella Napoli: 0.8 km
# => Taco Loco: 1.2 km
```

!!! tip
    Geo queries are backed by Redis GEORADIUS commands, which run in O(N+log(M))
    time. This is very fast even with millions of locations.

## DataFrameField

`DataFrameField` stores [Pandas DataFrame](https://pandas.pydata.org/docs/reference/frame.html)
objects directly in Redis. This is useful for analytics or caching computed datasets.
Because it depends on Pandas, it uses a separate model outside the canonical set.

```python
import pandas as pd
from popoto import Model, KeyField
from popoto.fields.dataframe_field import DataFrameField

class SalesReport(Model):
    name = KeyField()
    data = DataFrameField()
```

Store and retrieve a sales report:

```python
sales_data = pd.DataFrame({
    'restaurant': ['Siam Garden', 'Bella Napoli', 'Taco Loco'],
    'orders': [142, 98, 215],
    'revenue': [3408.58, 2156.02, 3225.85],
})

report = SalesReport.create(name="weekly_summary", data=sales_data)

loaded = SalesReport.load(name="weekly_summary")
print(loaded.data['revenue'].sum())
# => 8790.45

report.delete()
```

!!! note
    `DataFrameField` requires the `pandas` package. Install it separately if it is
    not already in your environment.

## AccessTrackerMixin

Tracks read access patterns on any model using a two-stage pipeline: reads are first staged (cheap), then atomically promoted to a confirmed access log. This prevents naive "every read strengthens" behavior — only meaningful reads count.

### Basic usage

```python
from popoto import Model, KeyField, Field, AccessTrackerMixin, DecayingSortedField

class Memory(Model, AccessTrackerMixin):
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()

# Reading triggers on_read() automatically via query hooks
memories = Memory.query.filter(agent_id="agent-1").top_by_decay(5)

# After the agent acts on a memory, confirm the read
memories[0].confirm_access()       # promotes staged → confirmed
memories[1].discard_staged_access() # clears staging without promoting

# Inspect access patterns
print(memories[0].access_count)    # 42
print(memories[0].last_accessed)   # 1741872000.0
```

### How staging works

`on_read()` fires automatically when instances are hydrated via `Query.get()`, `Query.filter()`, `top_by_decay()`, and their async variants. Each call appends a timestamp to a per-instance staging list (`RPUSH` — single Redis command, batched via pipeline).

Staged reads are not yet "real" — they represent candidate accesses. Your application decides which reads were meaningful:

- `confirm_access()` — atomically promotes all staged timestamps to the confirmed access log using a Lua script. Updates `access_count` and `last_accessed`.
- `discard_staged_access()` — clears staging without affecting confirmed data.

### Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_max_access_log` | `int` | `100` | Maximum timestamps kept in the confirmed access log. Older entries trimmed on confirm. |
| `_track_reads` | `bool` | `True` | Set to `False` to disable automatic `on_read()` from queries. |
| `_staged_ttl_seconds` | `int` | `86400` | TTL applied to the staged list on every `on_read()` call (24h default). Magic-number tuning knob — increase if your stage→confirm cadence exceeds 24h. |

### Staged-read TTL contract

Staged reads self-expire after `_staged_ttl_seconds` (default 24h). This bounds
Redis memory for records that are read but **never confirmed** without truncating
the count for records that confirm within the window.

**Contract:** Call `confirm_access()` within `_staged_ttl_seconds` of staging reads
via `on_read()`. Reads left unconfirmed past the TTL window are **permanently dropped**
from `access_count` / `last_accessed` by design — the `CONFIRM_ACCESS_LUA` script
finds an empty staged key and returns 0. Set `_staged_ttl_seconds` higher if your
agent's stage→confirm cadence exceeds 24h.

### Suppressing tracking for bulk operations

Use `no_track()` on the query builder to prevent `on_read()` from firing during
internal operations like reindexing, migration, or lifecycle ticks:

```python
# These reads won't be tracked
Memory.query.filter(agent_id="agent-1").no_track().all()
```

`MemoryLifecycle.tick()` uses `.no_track()` automatically — a periodic tick
produces **zero** new staged entries and does not inflate access-frequency signals.

Note: `Model.query.all()` (without `.filter()`) is non-tracking by design and
does not call `on_read()`. The tracking path is the `QueryBuilder`
(`.filter().all()`); chain `.no_track()` there for any internal sweep that
should not count as a user-originated read.

### Delete cleanup

When a tracked model instance is deleted, all three AccessTracker Redis keys (staged, access_log, meta) are automatically removed.

### Redis key patterns

| Key | Type | Purpose |
|-----|------|---------|
| `$AT:{ClassName}:staged:{pk}` | List | Pending read timestamps |
| `$AT:{ClassName}:access_log:{pk}` | List | Confirmed access timestamps (capped) |
| `$AT:{ClassName}:meta:{pk}` | Hash | `access_count` (int) and `last_accessed` (float) |

## EventStreamMixin

Automatically appends to a Redis Stream on every `save()`, `update()`, or `delete()`. This is infrastructure for background processing — the mixin writes events, your application consumes them via Redis Streams' consumer group API.

### Quick Start

```python
from popoto import Model, EventStreamMixin, UniqueKeyField, StringField

class Memory(EventStreamMixin, Model):
    _stream_name = "memory_mutations"       # Stream key: stream:memory_mutations
    _stream_max_length = 10000              # Approximate MAXLEN trimming
    _stream_metadata_fields = ("source",)   # Extra fields in each entry

    key = UniqueKeyField()
    content = StringField()
    source = StringField(default="")

memory = Memory(key="fact1", content="hello", source="user")
memory.save()    # XADD with op="create"
memory.content = "updated"
memory.save()    # XADD with op="update"
memory.delete()  # XADD with op="delete"
```

### Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `_stream_name` | `str` | `"mutations"` | Name for the Redis Stream (key: `stream:{name}`) |
| `_stream_partition_field` | `str` | `None` | Field name to partition streams by (key becomes `stream:{name}:{value}`) |
| `_stream_max_length` | `int` | `10000` | Approximate max entries via `MAXLEN ~` trimming |
| `_stream_metadata_fields` | `tuple` | `()` | Field names whose values are included in stream entries |

### Stream Entry Fields

Every stream entry contains these string fields:

| Field | Description |
|-------|-------------|
| `model` | Model class name |
| `pk` | Redis key of the instance |
| `op` | Operation: `"create"`, `"update"`, `"delete"`, or custom |
| `ts` | Unix timestamp |
| `changed_fields` | Comma-separated list of updated fields (from `update_fields`) |

Plus any fields listed in `_stream_metadata_fields`.

### Partitioned Streams

Route events to different streams based on a field value:

```python
class TenantMemory(EventStreamMixin, Model):
    _stream_name = "mutations"
    _stream_partition_field = "tenant"

    key = UniqueKeyField()
    tenant = StringField()

# Writes to stream:mutations:acme
TenantMemory(key="x", tenant="acme").save()

# Writes to stream:mutations:beta
TenantMemory(key="y", tenant="beta").save()
```

### Custom Events (Non-Save Operations)

Operations that bypass `Model.save()` (like `ConfidenceField.update_confidence()` and `CoOccurrenceField.strengthen()`) can log events via the public `_xadd_event()` method:

```python
# Called automatically by ConfidenceField.update_confidence()
instance._xadd_event(
    op="confidence_update",
    extra_fields={"field": "trust", "signal": "0.8"},
)
```

### Error Handling

- **Non-pipeline mode**: XADD failures are caught and logged — `save()` always succeeds if the data write succeeded.
- **Pipeline mode**: XADD is queued atomically with the save. If the pipeline fails, both data and stream entry fail together.

### WriteFilter Interaction

When `WriteFilterMixin` discards a record (score below threshold), the `save()` returns before reaching the EventStreamMixin hook. No stream entry is produced for filtered records.

### Redis Key Patterns

- `stream:{stream_name}` — default stream key
- `stream:{stream_name}:{partition_value}` — partitioned stream key

## TagField

`TagField` adds **optional, indexed, multi-value scoping** to a model. It is the
scoping primitive for a **centrally hosted Redis/Valkey serving many agents**: a
memory can be scoped by the agent it belongs to, a relevant project, or arbitrary
tags — and **every scoping dimension is optional**. A record with zero tags
transparently lives in the shared pool and is returned by unscoped queries.

Unlike KeyField partitioning (where the partition value becomes part of the
record's Redis key and is *required* at query time), tags are **metadata, not
identity**: a record can carry several tags at once, or none, and scoping is
opt-in per query.

### Basic usage

```python
from popoto import Model, AutoKeyField, Field, TagField

class Memory(Model):
    key = AutoKeyField()
    content = Field(type=str)
    tags = TagField()          # optional; zero tags == shared pool

Memory.create(content="deploy runbook", tags=["agent:valor", "project:popoto"])
Memory.create(content="general note")          # untagged — shared pool

# Membership / any-of (OR) / all-of (AND)
Memory.query.filter(tags__contains="agent:valor")
Memory.query.filter(tags__any=["agent:a", "agent:b"])       # SUNION
Memory.query.filter(tags__all=["agent:valor", "project:popoto"])  # SINTER

# Tag filters compose with any other filter through the same pipeline.
Memory.query.filter(project="popoto", tags__contains="agent:valor")
```

Accepts a `list`, `set`, or `tuple` of scalar tags; values are normalized to a
sorted, de-duplicated list. A bare exact-match `filter(tags=[...])` raises a
`QueryException` (use `__contains`/`__any`/`__all`) — a multi-value field has no
meaningful exact-match semantics.

### Convention over schema

popoto stays agnostic about which scoping dimensions exist. Agents cooperate on
prefixes — `agent:`, `project:` — or use bare tags; nothing is declared in the
schema. Colons in tag values are escaped in the index key, so `agent:valor` never
collides with popoto's key structure.

### Not a security boundary

Tags are a **cooperative** scoping mechanism between trusted agents, **not** access
control. A query with the right tag filter can read any tagged record; there is no
enforcement or isolation. Do not use tags to gate access to sensitive data.

### How it is indexed (Valkey-safe)

Per tag value, a plain Redis Set of instance keys:

```
$TagF:ModelName:field_name:tag_value -> Set of redis_keys
```

Index maintenance rides a single atomic Lua script (`TAG_SWAP_LUA`) that **diffs**
the record's previous tag membership against the new one and issues only the
necessary `SREM`/`SADD` calls — so re-saving with changed tags never leaves
orphaned Set members. The previous membership is read from a server-authoritative
pointer side key (a standalone Redis Set), never a client snapshot. All commands
are core Redis Set operations (`SADD`/`SREM`/`SMEMBERS`/`SUNION`/`SINTER`/`DEL`) —
**no Redis modules**, so it runs identically on Redis and Valkey.

### ContextAssembler integration

`ContextAssembler.assemble()` auto-detects a `TagField` on the model and accepts
optional tag constraints applied across **all** retrieval modes (composite,
hybrid, lexical, push):

```python
assembler = ContextAssembler(Memory, score_weights={"relevance": 1.0})

# Scope retrieval to one agent's memories
assembler.assemble(query_cues={"topic": "deploy"}, tags=["agent:valor"])

# Multiple tags: any-of (OR) by default — surfaces memories in either scope
assembler.assemble(
    query_cues={"topic": "deploy"},
    tags=["agent:valor", "project:popoto"],
)

# all-of semantics (intersection)
assembler.assemble(
    query_cues={"topic": "deploy"},
    tags=["agent:valor", "project:popoto"],
    tag_match="all",
)

# Omitting tags is byte-identical to a model with no TagField.
assembler.assemble(query_cues={"topic": "deploy"})
```

`tag_match` defaults to `"any"` (OR / `SUNION`) — in a shared central pool you'd rather over-surface scoped memories and let ranking sort them than silently miss a memory tagged on only one dimension; pass `"all"` for AND (`SINTER`) when you need the precise intersection. (The `Query.filter` arms — `tags__any` / `tags__all` / `tags__contains` — are always explicit.)

### Deploy-level kill switch

Tag scoping in the assembler is default-on via auto-detection. Because a PyPI
adopter cannot always edit model definitions, `Defaults.TAG_SCOPING_ENABLED`
(default `True`) is a deploy-level escape hatch: set it `False` to make the
assembler ignore tag constraints entirely (retrieval becomes identical to a model
without a TagField). Index maintenance and explicit
`Model.query.filter(tags__all=...)` queries are unaffected — the switch governs
only the subconscious assembler path.

## Reserved Field Names

The following names are reserved and cannot be used as field names:

- `limit` -- Used in `query.filter()` to limit the number of returned objects
- `values` -- Used in `query.filter()` to restrict which fields are returned
- `order_by` -- Used in `query.filter()` to sort results

## Model Methods

This section summarizes the core methods available on every Popoto model.

### Creating and Saving

Create and save in one step with `create()`, or instantiate and call `save()` later:

```python
# One-step creation
restaurant = Restaurant.create(
    name="Siam Garden",
    cuisine="Thai",
    rating=4.5,
    location=GeoField.Coordinates(latitude=40.7484, longitude=-73.9857),
)

# Two-step creation
restaurant = Restaurant(name="Bella Napoli")
restaurant.cuisine = "Italian"
restaurant.rating = 4.2
restaurant.save()
```

### Loading

Load an instance by its KeyField values. For `AutoKeyField` models, load by the
generated key:

```python
restaurant = Restaurant.load(name="Siam Garden")
print(restaurant.cuisine)
# => "Thai"

order = Order.load(order_id=order.order_id)
print(order.status)
# => "pending"
```

### Updating

Modify fields and call `save()` to persist changes. For models with
`DatetimeField(auto_now=True)`, the timestamp is refreshed automatically:

```python
restaurant = Restaurant.load(name="Siam Garden")
restaurant.rating = 4.7
restaurant.save()

order.status = "delivered"
order.save()
# order.updated_at is automatically refreshed
```

### Deleting

Call `delete()` to remove an instance and clean up all associated indexes (sorted
set entries, geo set entries, unique field indexes, and relationship indexes):

```python
restaurant = Restaurant.load(name="Siam Garden")
restaurant.delete()
```

### Validation

Use `is_valid()` to check whether a model instance passes all field validations
before saving:

```python
restaurant = Restaurant(name=None, cuisine="Thai", rating=4.5)
print(restaurant.is_valid())
# => False  (name is a required KeyField)

restaurant.name = "Siam Garden"
print(restaurant.is_valid())
# => True
```

### The db_key Property

Every saved instance has a `db_key` property that exposes its Redis key:

```python
restaurant = Restaurant.create(name="Siam Garden", cuisine="Thai", rating=4.5)
print(restaurant.db_key.redis_key)
# => "Restaurant:Siam Garden"

reservation = Reservation.create(
    restaurant="Siam Garden", customer="foodie42", party_size=4
)
print(reservation.db_key.redis_key)
# => "Reservation:Siam Garden:foodie42"
```

The `db_key` is useful for debugging, logging, or performing custom Redis operations
outside of Popoto's query API.
