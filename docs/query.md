# Queries and Query Filters

Popoto provides a Django-like query API for filtering, ordering, and retrieving model instances from Redis. You can
search by key fields, filter by sorted numeric ranges, perform geographic searches, and retrieve results as model
instances or lightweight dictionaries.

## Get a single object

When you know the exact key field values, use `get()` to retrieve a single instance. This is the fastest query
operation because it performs a direct Redis key lookup.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    email = Field()

person = Person.create(name="Sally", email="sally@example.com")

same_person = Person.query.get(name="Sally")
print(same_person == person)
# => True
```

You can also retrieve an object by its Redis key directly.

```python
person = Person.query.get(redis_key="Person:Sally")
```

## Get all objects

Use `all()` to retrieve all instances of a model. This scans all keys matching the model's pattern in Redis.

```python
all_people = Person.query.all()
```

The `all()` method supports the same `order_by`, `limit`, and `values` parameters as `filter()` (described below).

```python
# All people ordered by name
people = Person.query.all(order_by="name")

# First 10 people
people = Person.query.all(limit=10)

# All people as dicts with only the name field
people = Person.query.all(values=("name",))
```

## Filter query results

Use `filter()` to retrieve instances matching specific criteria. You can filter on any field using lookup expressions
like `__startswith`, `__gte`, or exact matches.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    email = Field()

sally = Person.create(name="Sally", email="sally@example.com")
sam = Person.create(name="Sam", email="sam@example.com")

# Filter by partial match
s_names = Person.query.filter(name__startswith="S")
print(s_names[0].name)
# => "Sally"
```

See the field-specific filter sections below for all supported lookup expressions.

!!! warning
    All filter parameters are AND'ed together. There is no built-in OR support. If you pass multiple filters, only
    instances matching all criteria will be returned.

## Count results

Use `count()` to get the number of matching instances without loading them into memory. This is more efficient than
loading all results and checking the length.

```python
total = Person.query.count()
# => 2

# Count with filters
s_count = Person.query.count(name__startswith="S")
# => 2
```

## Get Redis keys

Use `keys()` to retrieve the raw Redis key strings for a model's instances. This is useful for debugging or when you
need to perform custom Redis operations.

```python
all_keys = Person.query.keys()
# => ["Person:Sally", "Person:Sam", ...]
```

## Values

Return results as dictionaries instead of model instances. Specify which fields to include as a tuple of field names.
This is faster and more memory-efficient when you only need a subset of fields.

```python
Person.query.filter(values=("name", "email"))
# => [{"name": "Sally", "email": "sally@example.com"}, {"name": "Sam", "email": "sam@example.com"}]

Person.query.filter(name="Sally", values=("name",))
# => [{"name": "Sally"}]
```

!!! tip
    If all the fields specified in `values` are key fields, query performance will be at least 2x faster compared to a
    query that loads full model instances. This is because Popoto can extract key field values directly from the Redis
    key pattern without deserializing the stored data.

## Order by field name

Use `order_by` to sort results by a field value. Prefix the field name with `-` for descending order. Ascending order
is the default.

```python
from popoto import Model, KeyField, SortedField

class Person(Model):
    name = KeyField()
    age = SortedField(type=int)

Person.create(name="Alice", age=30)
Person.create(name="Bob", age=25)

# Ascending order (youngest first)
people = Person.query.filter(order_by="age")

# Descending order (oldest first)
people = Person.query.filter(order_by="-age")
```

Ordering works for field types `str`, `int`, `float`, `Decimal`, `time`, `date`, and `datetime`. The field must be a
`SortedField` for numeric ordering, or a `KeyField` for string ordering.

You can also set a default ordering via Model Meta Options.

```python
from datetime import datetime
from popoto import Model, KeyField, SortedField

class Note(Model):
    title = KeyField()
    created_at = SortedField(type=datetime)

    class Meta:
        order_by = "-created_at"  # Newest first by default

# Uses default ordering from Meta
notes = Note.query.all()

# Override at query time
notes = Note.query.all(order_by="title")
```

See [Model Meta Options](meta.md) for more details on configuring default ordering.

## Limit number of results

Use `limit` to restrict the number of results returned. This is applied after filtering and ordering.

```python
people = Person.query.filter(name__startswith="S", limit=10)
print(len(people))
# => 10
```

You can also use Python slice notation to limit results.

```python
people = Person.query.filter(name__startswith="S")[:10]
print(len(people))
# => 10
```

Both approaches return the same results. The explicit `limit` parameter may be slightly faster. If `order_by` is used,
ordering is applied before limiting.

## KeyField query filters

KeyField filters support string matching operations. All lookups are case-sensitive.

- `{field_name}=` — exact match
- `{field_name}__isnull=` — `True` or `False` to filter for null/non-null values
- `{field_name}__contains=` — partial string match (substring anywhere)
- `{field_name}__startswith=` — partial string match (prefix)
- `{field_name}__endswith=` — partial string match (suffix)
- `{field_name}__in=` — exact match for any element in provided list

Example queries using the canonical Person model:

```python
Person.query.filter(name="Sally")
Person.query.filter(name__startswith="S")
Person.query.filter(name__contains="all")
Person.query.filter(name__in=["Sally", "Sam"])
Person.query.filter(name__isnull=False)
```

## SortedField query filters

SortedField filters support numeric and date/time range queries. These are backed by Redis sorted sets for efficient
range lookups.

- `{field_name}=` — exact match
- `{field_name}__gt=` — greater than
- `{field_name}__gte=` — greater than or equal to
- `{field_name}__lt=` — less than
- `{field_name}__lte=` — less than or equal to

Example queries using the canonical Person model:

```python
from popoto import Model, KeyField, SortedField

class Person(Model):
    name = KeyField()
    age = SortedField(type=int)

# Find people 18 or older
adults = Person.query.filter(age__gte=18)

# Find people under 65
working_age = Person.query.filter(age__lt=65)

# Range query: people between 18 and 65
working_adults = Person.query.filter(age__gte=18, age__lt=65)
```

Range queries work with any sortable type including `int`, `float`, `Decimal`, `datetime`, `date`, and `time`.

```python
from datetime import datetime
from popoto import Model, KeyField, SortedField

class Note(Model):
    title = KeyField()
    created_at = SortedField(type=datetime)

# All notes from January 2021
jan_notes = Note.query.filter(
    created_at__gte=datetime(2021, 1, 1),
    created_at__lt=datetime(2021, 2, 1)
)
```

## GeoField query filters

GeoField filters support geographic proximity searches backed by Redis geospatial indexes. You can search by
coordinates, radius, and optionally include distances in the results.

- `{field_name}=` — `tuple` (latitude, longitude) or `popoto.GeoField.Coordinates`
- `{field_name}__isnull=` — filter for null (`None`) values (if `null=True` is set on field)
- `{field_name}_latitude=` — `float` latitude value
- `{field_name}_longitude=` — `float` longitude value
- `{field_name}_radius=` — `int` or `float` radius distance (default: `10`)
- `{field_name}_radius_unit=` — `"m"` (meters), `"km"` (kilometers), `"ft"` (feet), or `"mi"` (miles). Default: `"km"`
- `{field_name}_member=` — Use another model instance's coordinates as the center point
- `{field_name}_with_distances=` — `True` to include distance info (adds `_geo_distance` and `_geo_distance_unit`
  attributes, results sorted by distance)

Example queries using the canonical Place model:

```python
from popoto import Model, KeyField, GeoField

class Place(Model):
    name = KeyField()
    coordinates = GeoField()

rome = Place.create(name="Rome", coordinates=(41.902782, 12.496366))
florence = Place.create(name="Florence", coordinates=(43.769562, 11.255814))

# Filter by coordinates and radius
nearby = Place.query.filter(
    coordinates=(41.902782, 12.496366),
    coordinates_radius=5,
    coordinates_radius_unit='km'
)
```

You can also filter by latitude and longitude separately.

```python
# Filter by latitude/longitude directly
nearby = Place.query.filter(
    coordinates_latitude=41.902782,
    coordinates_longitude=12.496366,
    coordinates_radius=10
)
```

Use another instance as the center point for proximity searches.

```python
# Use another instance as the center point
near_rome = Place.query.filter(
    coordinates_member=rome,
    coordinates_radius=100,
    coordinates_radius_unit='km'
)
```

Include distance information in the results for display or further filtering.

```python
# Include distances in results
results = Place.query.filter(
    coordinates=(41.902782, 12.496366),
    coordinates_radius=200,
    coordinates_radius_unit='km',
    coordinates_with_distances=True
)
for place in results:
    print(f"{place.name}: {place._geo_distance} {place._geo_distance_unit}")
# => Rome: 0.0 km
# => Florence: 231.5 km
```
