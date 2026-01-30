# Models and Fields

Models are the foundation of Popoto. They define the structure of your Redis-stored data using Python classes with field declarations. If you're familiar with Django or SQLAlchemy, the pattern will feel familiar: inherit from `Model`, declare fields as class attributes, and Popoto handles the rest.

Popoto models are flexible. You can define any number of fields with varying types and behaviors. If you don't specify a primary key field, Popoto automatically creates one for you.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    email = Field()
```

This guide covers all available field types and their configuration options.

## KeyField

KeyFields determine how Popoto stores and retrieves your objects in Redis. They form the Redis key used to look up instances, making queries on KeyFields extremely fast.

In the background, Popoto concatenates all KeyField values to build the primary key. For example, a `Person` with `name="Sally"` is stored at the Redis key `Person:Sally`. You can use multiple KeyFields with minimal performance overhead.

```python
from popoto import Model, KeyField

class Person(Model):
    name = KeyField()
    email = Field()
```

Create and retrieve a person.

```python
person = Person.create(name="Sally", email="sally@example.com")

# Fast retrieval using the KeyField
loaded_person = Person.load(name="Sally")
print(loaded_person.email)
# => "sally@example.com"
```

### Uniqueness

It's recommended that at least one KeyField enforces uniqueness across all saved instances. This prevents accidental overwrites and ensures each instance has a distinct identity.

These KeyField variants enforce uniqueness: `AutoKeyField`, `UniqueKeyField`, and `KeyField(unique=True)`.

```python
from popoto import Model, AutoKeyField, UniqueKeyField, KeyField

class Person(Model):
    uuid = AutoKeyField()
    name = UniqueKeyField()
    email = KeyField(unique=True)
```

Attempting to create a duplicate will raise an exception.

```python
Person.create(uuid="1", name="Sally", email="sally@example.com")

# This will fail because name="Sally" already exists
try:
    Person.create(uuid="2", name="Sally", email="different@example.com")
except Exception as e:
    print(f"Error: {e}")
    # => Error: UniqueKeyField 'name' value 'Sally' already exists
```

### Composite Keys

If no single KeyField is unique, all KeyFields together must be "unique together." In this example, a unique person is identified by the combination of first and last name. Two people with identical first and last names will be treated as the same instance and save to the same Redis key.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    first_name = KeyField()
    last_name = KeyField()
    email = Field()
```

Create instances with composite keys.

```python
Person.create(first_name="Sally", last_name="Smith", email="sally@example.com")
Person.create(first_name="Sally", last_name="Jones", email="sally.jones@example.com")

# These are two distinct people because the composite key differs
sally_smith = Person.load(first_name="Sally", last_name="Smith")
print(sally_smith.email)
# => "sally@example.com"
```

### Models Without KeyFields

You can declare a Model without any KeyField, and Popoto will create and maintain a hidden unique key automatically. This is useful when all queries use specialized fields like `SortedField` or `GeoField` rather than direct key lookups.

```python
from popoto import Model, Field, SortedField
from datetime import datetime
from decimal import Decimal

class Note(Model):
    content = Field(type=str)
    created_at = SortedField(type=datetime)
```

Create and query without KeyFields.

```python
note1 = Note.create(content="First note", created_at=datetime(2025, 1, 1))
note2 = Note.create(content="Second note", created_at=datetime(2025, 1, 2))

# Query using the SortedField instead of KeyFields
recent_notes = Note.query.filter(created_at__gte=datetime(2025, 1, 1))
print(len(recent_notes))
# => 2
```

## Field

All fields inherit from the base `Field` class. A basic `Field` on any model provides type validation on create and update operations.

The following types are supported: `int`, `float`, `Decimal`, `str`, `bool`, `list`, `set`, `tuple`, `dict`, `bytes`, `datetime.date`, `datetime.datetime`, `datetime.time`.

The default type for a field is `str` if not specified.

```python
from popoto import Model, KeyField, Field
from decimal import Decimal
from datetime import datetime, date, time

class Person(Model):
    name = KeyField()
    email = Field(type=str)
    age = Field(type=int)
    balance = Field(type=Decimal)
    is_active = Field(type=bool)
    tags = Field(type=list)
    metadata = Field(type=dict)
    birth_date = Field(type=date)
    last_login = Field(type=datetime)
```

Popoto validates field types when you save.

```python
person = Person.create(
    name="Sally",
    email="sally@example.com",
    age=30,
    balance=Decimal("100.50"),
    is_active=True,
    tags=["user", "active"],
    metadata={"signup_date": "2025-01-01"},
    birth_date=date(1995, 5, 15),
    last_login=datetime(2025, 1, 30, 10, 0, 0)
)

# Type validation occurs on save
try:
    person.age = "thirty"  # Wrong type
    person.save()
except Exception as e:
    print(f"Validation error: {e}")
```

### Named Field Shortcuts

Popoto provides named field classes that are equivalent to specifying the type parameter. Use whichever style you prefer.

```python
from popoto import Model, KeyField
from popoto.fields.shortcuts import (
    IntField, FloatField, DecimalField, StringField,
    BooleanField, ListField, SetField, TupleField,
    DictField, BytesField, DateField, DatetimeField, TimeField
)

class Person(Model):
    name = KeyField()
    age = IntField()
    height = FloatField()
    balance = DecimalField()
    email = StringField()
    is_active = BooleanField()
    tags = ListField()
    categories = SetField()
    coordinates = TupleField()
    metadata = DictField()
    profile_image = BytesField()
    birth_date = DateField()
    last_login = DatetimeField()
    preferred_time = TimeField()
```

These shortcuts are functionally identical to using `Field(type=int)`, `Field(type=str)`, etc.

### Null Values

KeyField and SortedField values are required (`null=False`) by default. All other fields are optional (`null=True`) by default. You can explicitly control this behavior using the `null` keyword argument.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()  # Required by default
    email = Field(null=True)  # Optional, can be None
    age = Field(type=int, null=False)  # Required
```

Setting a required field to `None` will fail validation.

```python
person = Person(name="Sally", age=None)
print(person.is_valid())
# => False

person.age = 30
print(person.is_valid())
# => True
```

### Default Values

All fields accept a `default` value that is used when creating new instances without specifying that field.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    status = Field(type=str, default="active")
    is_verified = Field(type=bool, default=False)
    login_count = Field(type=int, default=0)
```

Defaults are applied when fields are not provided.

```python
person = Person.create(name="Sally")
print(person.status)
# => "active"

print(person.is_verified)
# => False

print(person.login_count)
# => 0
```

### Callable Defaults

Defaults can also be callables. The callable is invoked each time a new instance is created, ensuring each instance gets its own fresh value. This is particularly important for mutable defaults like lists and dictionaries.

```python
import uuid
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    id = Field(default=uuid.uuid4)  # Fresh UUID per instance
    tags = Field(type=list, default=list)  # Fresh empty list per instance
    metadata = Field(type=dict, default=dict)  # Fresh empty dict per instance
```

Each instance gets its own unique values.

```python
person1 = Person.create(name="Sally")
person2 = Person.create(name="Bob")

print(person1.id == person2.id)
# => False

person1.tags.append("admin")
print(person1.tags)
# => ["admin"]

print(person2.tags)
# => []
```

Lambda functions work as well for simple defaults.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    score = Field(type=int, default=lambda: 0)
```

!!! warning
    Never use mutable default values directly (e.g., `default=[]` or `default={}`). This creates a single shared object across all instances. Always use callables like `default=list` or `default=dict`.

### String Max Length

You can set a maximum length limit for string fields. Unlike SQL databases, Redis doesn't require max_length for performance. Use it only if you want Popoto to validate string length and raise exceptions.

```python
from popoto import Model, KeyField, Field

class Note(Model):
    title = KeyField()
    summary = Field(type=str, max_length=280)
```

Validation occurs on save.

```python
note = Note(title="My Note", summary="A" * 300)
try:
    note.save()
except Exception as e:
    print(f"Validation error: {e}")
    # => Validation error: Field 'summary' exceeds max_length of 280
```

## SortedField

SortedField enables fast range queries on numerical attributes using Redis sorted sets. This is one of Redis's most powerful features, allowing efficient queries like "all people older than 25" or "notes created in the last hour."

A SortedField is required to use these query filters: `__lt`, `__lte`, `__gt`, `__gte`. See [Making Queries](query.md) for complete filter documentation.

```python
from popoto import Model, KeyField, SortedField
from datetime import date

class Person(Model):
    name = KeyField()
    email = Field()
    birth_date = SortedField(type=date)
```

Create instances and query by sorted fields.

```python
Person.create(name="Sally", birth_date=date(1995, 5, 15))
Person.create(name="Bob", birth_date=date(1990, 3, 20))
Person.create(name="Alice", birth_date=date(2000, 7, 10))

# Find people born before 1995
older_people = Person.query.filter(birth_date__lt=date(1995, 1, 1))
print(len(older_people))
# => 1

print(older_people[0].name)
# => "Bob"

# Find people born after 1995
younger_people = Person.query.filter(birth_date__gt=date(1995, 12, 31))
print([p.name for p in younger_people])
# => ["Alice"]
```

### SortedKeyField

To use a SortedField also as a KeyField, use `SortedKeyField`. This combines the fast key-based lookup of KeyField with the range query capabilities of SortedField.

```python
from popoto import Model, SortedKeyField, Field
from datetime import datetime
from decimal import Decimal

class Note(Model):
    created_at = SortedKeyField(type=datetime)
    content = Field(type=str)
```

Query using the sorted key field.

```python
note1 = Note.create(created_at=datetime(2025, 1, 1, 10, 0), content="First note")
note2 = Note.create(created_at=datetime(2025, 1, 2, 10, 0), content="Second note")

# Range query on the key field
recent = Note.query.filter(created_at__gte=datetime(2025, 1, 1))
print(len(recent))
# => 2
```

### Performance Optimization with sort_by

When you always query a SortedField in combination with a required KeyField, you can dramatically improve performance by defining `sort_by`. This parameter (which must be a tuple) tells Popoto to create a composite index.

The tradeoff is that all queries on this SortedField must include the fields specified in `sort_by`.

```python
from popoto import Model, KeyField, SortedKeyField, Field
from datetime import datetime
from decimal import Decimal

class Note(Model):
    title = KeyField()
    created_at = SortedKeyField(type=datetime, sort_by=('title',))
    content = Field(type=str)
```

Now queries on `created_at` must include `title`.

```python
Note.create(title="Work", created_at=datetime(2025, 1, 1), content="Meeting notes")
Note.create(title="Personal", created_at=datetime(2025, 1, 2), content="Shopping list")

# This query is extremely fast because it uses the composite index
work_notes = Note.query.filter(
    title="Work",
    created_at__gte=datetime(2025, 1, 1),
    created_at__lt=datetime(2025, 2, 1)
)
print(len(work_notes))
# => 1

# This query will fail because 'title' is required
try:
    all_notes = Note.query.filter(created_at__gte=datetime(2025, 1, 1))
except Exception as e:
    print(f"Error: {e}")
```

!!! tip
    Use `sort_by` when you always filter by the same KeyField along with the SortedField. This provides maximum performance with Redis sorted sets.

## DatetimeField

DatetimeField extends the base Field with automatic timestamp management. It supports two special parameters for common patterns: `auto_now_add` and `auto_now`.

```python
from popoto import Model, KeyField
from popoto.fields.shortcuts import DatetimeField

class Note(Model):
    title = KeyField()
    content = Field(type=str)
    created_at = DatetimeField(auto_now_add=True)  # Set on first save only
    updated_at = DatetimeField(auto_now=True)  # Updated on every save
```

The timestamps are managed automatically.

```python
note = Note.create(title="My Note", content="Initial content")

print(note.created_at)
# => 2025-01-30 10:00:00.123456

print(note.updated_at)
# => 2025-01-30 10:00:00.123456

# Update and save
note.content = "Updated content"
note.save()

print(note.created_at)
# => 2025-01-30 10:00:00.123456 (unchanged)

print(note.updated_at)
# => 2025-01-30 10:05:30.654321 (updated)
```

- `auto_now_add=True`: Sets the field to the current datetime when the instance is first created. The value is not changed on subsequent saves.
- `auto_now=True`: Sets the field to the current datetime every time `save()` is called.

### Timestampable Mixin

You can also use the `Timestampable` mixin which provides both `created_at` and `updated_at` fields automatically.

```python
from popoto import Model, KeyField, Field
from popoto.utils.mixins.timestampable import Timestampable

class Note(Timestampable, Model):
    title = KeyField()
    content = Field(type=str)
    # created_at and updated_at are automatically included
```

The mixin adds both timestamp fields for you.

```python
note = Note.create(title="My Note", content="Some content")

print(hasattr(note, 'created_at'))
# => True

print(hasattr(note, 'updated_at'))
# => True
```

## GeoField

GeoField employs Redis geospatial search capabilities, enabling powerful location-based queries. A common use case is finding all objects within a certain radius of a point.

Popoto provides a `Coordinates` namedtuple, though any tuple of `(latitude, longitude)` as floats is accepted.

```python
from popoto import Model, KeyField, GeoField

class Place(Model):
    name = KeyField()
    coordinates = GeoField()
```

Create places with geographic coordinates.

```python
rome = Place.create(
    name="Rome",
    coordinates=GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
)

vatican = Place.create(
    name="Vatican",
    coordinates=GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
)

colosseum = Place.create(
    name="Colosseum",
    coordinates=GeoField.Coordinates(latitude=41.890251, longitude=12.492373)
)
```

Query for places within a radius.

```python
# Find all places within 5km of Rome
nearby = Place.query.filter(
    coordinates=rome.coordinates,
    coordinates_radius=5,
    coordinates_radius_unit='km'
)

print(vatican in nearby)
# => True

print(colosseum in nearby)
# => True
```

### Query with Distances

Use `{field_name}_with_distances=True` to include distance information in query results. When enabled, each returned object will have `_geo_distance` and `_geo_distance_unit` attributes, and results are automatically sorted by distance (closest first).

```python
# Find all places within 10km of Rome, with distances
results = Place.query.filter(
    coordinates=(41.902782, 12.496366),
    coordinates_radius=10,
    coordinates_radius_unit='km',
    coordinates_with_distances=True
)

for place in results:
    print(f"{place.name}: {place._geo_distance} {place._geo_distance_unit}")
# => Rome: 0.0 km
# => Vatican: 3.5 km
# => Colosseum: 1.4 km
```

You can also use a model instance as the center point.

```python
results = Place.query.filter(
    coordinates_member=rome,
    coordinates_radius=10,
    coordinates_radius_unit='km',
    coordinates_with_distances=True
)

for place in results:
    print(f"{place.name}: {place._geo_distance} km")
# => Rome: 0.0 km
# => Vatican: 3.5 km
# => Colosseum: 1.4 km
```

Delete the places when done.

```python
rome.delete()
vatican.delete()
colosseum.delete()
```

## DataFrameField

DataFrameField allows storage of [Pandas DataFrame](https://pandas.pydata.org/docs/reference/frame.html) objects for tabular data. Common use cases include storing machine learning training data, analysis results, or time-series datasets directly in Redis.

```python
import pandas as pd
from popoto import Model, KeyField
from popoto.fields.shortcuts import DataFrameField

class Dataset(Model):
    name = KeyField()
    dataframe = DataFrameField()
```

Store a DataFrame loaded from CSV.

```python
# Assume we have a CSV with home price data
data = pd.DataFrame({
    'Price': [22000, 25000, 27000, 29000, 35000],
    'Year': [2014, 2015, 2016, 2017, 2018]
})

dataset = Dataset.create(name="Chicago Home Prices", dataframe=data)

# Retrieve and analyze
loaded = Dataset.load(name="Chicago Home Prices")
print(loaded.dataframe.describe())
# =>               Price         Year
# => count      5.000000     5.000000
# => mean   27600.000000  2016.000000
# => std     4878.524367     1.581139
# => min    22000.000000  2014.000000
# => 25%    25000.000000  2015.000000
# => 50%    27000.000000  2016.000000
# => 75%    29000.000000  2017.000000
# => max    35000.000000  2018.000000
```

Clean up the dataset.

```python
dataset.delete()
```

## Reserved Field Names

The following names are reserved and cannot be used as field names:

- `limit`: Used in `query.filter()` to limit the size of the returned objects list
- `values`: Used in `query.filter()` to restrict which values are returned for objects
- `order_by`: Used in `query.filter()` to order the results

## Model Methods

### Creating and Saving

Create and save a model instance in one step using `create()`, or create an instance and save it later.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    email = Field()
    age = Field(type=int)
```

Create and save in one step.

```python
person = Person.create(name="Sally", email="sally@example.com", age=25)
```

Create, modify, then save.

```python
person = Person(name="Bob")
person.email = "bob@example.com"
person.age = 30
person.save()
```

### Loading

Load an existing instance by its KeyField values.

```python
person = Person.load(name="Sally")
print(person.email)
# => "sally@example.com"
```

### Updating

Modify field values and call `save()` to persist changes.

```python
person = Person.load(name="Sally")
person.age = 26
person.save()
```

### Deleting

Delete an instance to remove its Redis key and clean up all associated indexes.

```python
person = Person.load(name="Sally")
person.delete()
```

Deleting removes the Redis key and cleans up all associated indexes, including key field indexes, sorted set entries, geo set entries, relationship indexes, and unique composite indexes.

### Validation

Use `is_valid()` to check if a model instance has valid field values before saving.

```python
person = Person(name=None, email="test@example.com")
print(person.is_valid())
# => False (name is required)

person.name = "Charlie"
print(person.is_valid())
# => True
```

### The db_key Property

Every saved instance has a `db_key` property that returns the Redis key components.

```python
person = Person.create(name="Sally", email="sally@example.com")
print(person.db_key)
# => DB_key object

print(person.db_key.redis_key)
# => "Person:Sally"
```

Clean up the test data.

```python
person.delete()
Person.load(name="Bob").delete()
```
