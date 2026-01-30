# Queries and Query Filters

Key-Value Storage and then some...

## Get a single object

Query on a `KeyField` to retrieve a single object instance.

``` python
import popoto

class Animal(popoto.Model):
    name = popoto.KeyField()
    sound = popoto.Field(null=True, default=None)

duck = Animal.create(name="Sally", sound="quack")

same_duck = Animal.query.get(name="Sally")
same_duck == duck
>>> True
```

You can also retrieve an object by its Redis key directly:

``` python
duck = Animal.query.get(redis_key="Animal:Sally")
```

## Get all objects

Retrieve all instances of a model:

``` python
all_animals = Animal.query.all()
```

`all()` supports the same `order_by`, `limit`, and `values` parameters as `filter()`:

``` python
# All animals ordered by name
animals = Animal.query.all(order_by="name")

# First 10 animals
animals = Animal.query.all(limit=10)

# All animals as dicts with only the name field
animals = Animal.query.all(values=("name",))
```

## Filter query results

All filter parameters are `&&` AND'ed together.

``` python
import popoto

class Animal(popoto.Model):
    name = popoto.KeyField()
    sound = popoto.Field(null=True, default=None)

sally = Animal.create(name="Sally", sound="quack")
```

See [KeyField query filters](#keyfield-query-filters) below for supported filters.

Example:

``` python
Animal.query.filter(name__startswith="S")[0].name
>>> "Sally"
```

## Count results

Use `count()` to get the number of matching instances without loading them:

``` python
total = Animal.query.count()
>>> 5

# Count with filters
quackers = Animal.query.count(sound="quack")
>>> 2
```

## Get Redis keys

Use `keys()` to get the raw Redis keys for a model's instances:

``` python
all_keys = Animal.query.keys()
>>> ["Animal:Sally", "Animal:Bob", ...]
```

## Values

Returns dictionaries rather than model instances. Each dictionary represents an object, with keys corresponding to field names. Specify the fields with a tuple of field names.

``` python
Animal.query.filter(values=("name", "sound"))
>>> [{"name": "Sally", "sound": "quack"}, ...]

Animal.query.filter(name="Sally", values=("name",))
>>> [{"name": "Sally"}]
```

Pro Tip: If _all_ the fields specified are _Key_ fields, then query performance will be at least 2x faster compared to a query without any specified values.


## Order By field_name

Results are ordered by the value of a given field.

``` python
Movies.query.filter(order_by="-release_date")
Movies.query.filter(order_by="name")
```

The negative sign in front of "-release_date" indicates descending order. Ascending order is implied.
The second query will return movies ordered by name alphabetically.
Ordering works for field types: `str`, `int`, `float`, `Decimal`, `time`, `date`, `datetime`

You can also set a default ordering via [Model Meta Options](meta.md):

``` python
class Movie(Model):
    name = KeyField()
    release_date = SortedField(type=datetime)

    class Meta:
        order_by = "-release_date"  # Newest first by default

# Uses default ordering from Meta
movies = Movie.query.all()

# Override at query time
movies = Movie.query.all(order_by="name")
```


## Limit Number of Results

Returns first 100 objects:

``` python
movies = Movies.query.filter(name__startswith="The", limit=100)
len(movies)
>>> 100
```

The above may be slightly faster than the equivalent below:

``` python
movies = Movies.query.filter(name__startswith="The")[:100]
len(movies)
>>> 100
```

Both are valid and will return the same list of objects.
If `order_by` is used, ordering is applied before limiting.


## KeyField query filters

`{field_name}=`: exact match

`{field_name}__isnull=`: `True` or `False` to filter for null/non-null values

`{field_name}__contains=`: partial string match

`{field_name}__startswith=`: partial string match

`{field_name}__endswith=`: partial string match

`{field_name}__in=`: exact match for any element in provided list

Example Queries:

```python
Animal.query.filter(name="Sally")
Animal.query.filter(name__startswith="S")
Animal.query.filter(name__contains="all")
Animal.query.filter(name__in=["Sally", "Bob"])
Animal.query.filter(name__isnull=False)
```


## SortedField query filters

`{field_name}=`: exact match

`{field_name}__gt=`: _greater than_ filter

`{field_name}__gte=`: _greater than or equal to_ filter

`{field_name}__lt=`: _less than_ filter

`{field_name}__lte=`: _less than or equal to_ filter


Example Queries:

```python
SortedFloatModel.query.filter(height__gte=john.height)

Racer.query.filter(fastest_lap__lt=55.0)

# Range query
AssetPrice.query.filter(
    timestamp__gte=datetime(2021, 1, 1),
    timestamp__lt=datetime(2021, 1, 2)
)
```


## GeoField query filters

`{field_name}=`: `tuple` or `popoto.GeoField.Coordinates` (float, float) with Coordinates

`{field_name}__isnull=`: filter for null (`None`) values (if `null=True` is set on model field declaration)

`{field_name}_latitude=`: `float`

`{field_name}_longitude=`: `float`

`{field_name}_radius=`: `int` or `float`. Default is `10`

`{field_name}_radius_unit=`: One of `"m"`(meters), `"km"`(kilometers), `"ft"`(feet), `"mi"`(miles). Default is `"km"`(kilometers)

`{field_name}_member=`: Use another model instance's coordinates as the center point

`{field_name}_with_distances=`: `True` to include distance info on each result (adds `_geo_distance` and `_geo_distance_unit` attributes, results sorted by distance)


Example Queries:

```python
# Filter by coordinates and radius
GeoModel.query.filter(
    coordinates=rome.coordinates,
    coordinates_radius=5,
    coordinates_radius_unit='km'
)

# Filter by latitude/longitude directly
GeoModel.query.filter(
    coordinates_latitude=41.902782,
    coordinates_longitude=12.496366
)

# Use another instance as the center point
GeoModel.query.filter(
    coordinates_member=rome,
    coordinates_radius=10,
    coordinates_radius_unit='km'
)

# Include distances in results
results = GeoModel.query.filter(
    coordinates=(41.902782, 12.496366),
    coordinates_radius=10,
    coordinates_radius_unit='km',
    coordinates_with_distances=True
)
for location in results:
    print(f"{location.name}: {location._geo_distance} {location._geo_distance_unit}")
```
