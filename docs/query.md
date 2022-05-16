# Queries and Query Filters

Key-Value Storage and then some...

# Get a single object

Query on a `KeyField` to retrieve a single object instance. 

``` python
import popoto

class Animal(popoto.Model):
    name = popoto.KeyField()
    sound = popoto.Field(null=True, default=None)

duck = Animal.create(name="Sally", sound="quack")

same_duck = KeyValueModel.query.get("Sally")
same_duck == duck
>>> True
```

# Filter query results

All filter paramters are `&&` AND'ed together.

``` python
import popoto

class Animal(popoto.Model):
    name = popoto.KeyField()
    sound = popoto.Field(null=True, default=None)

sally = Animal.create(name="Sally", sound="quack")
```

See (#keyfield-query-filters)[KeyField query filters] below for supported filters
example:

``` python
Animal.query.filter(name__startswith="S")[0].name
>>> "salamander"
```


# Order By {field_name}

Results are ordered by the value of a given field. Ascending order is implied.

``` python
Movies.query.filter(order_by="name")
```

the above will return movies ordered by name alphabetically
ordering works for field types: `str`, `int`, `float`, `decimal`, `time`, `date`, `datetime`

# Limit Number of Results

returns first 100 objects

``` python
movies = Movies.query.filter(name__startswith="The", limit=100)
len(movies)
>>> 100
```

the above may be slightly faster than equivalent below

``` python
movies = Movies.query.filter(name__startswith="The")[:100]
len(movies)
>>> 100
```

both are valid and will return the same list of objects.

if order_by is used, it will order before 


# KeyField query filters

`{field_name}=`: exact match

`{field_name}__contains=`: partial string match

`{field_name}__startswith=`: partial string match

`{field_name}__endswith=`: partial string match

`{field_name}__in=`: is exact match for any element in provided list


# SortedField query filters

`{field_name}=`: exact match

`{field_name}__gt=`: _greater than_ filter 

`{field_name}__gte=`: _greater than or equal to_ filter

`{field_name}__lt=`: _less than_ filter

`{field_name}__lte=`: _less than or equal to_ filter


Example Queries:

```python
SortedFloatModel.query.filter(height__gte=john.height)

Racer.query.filter(fastest_lap__lt=55.0)
```


# GeoField query filters

`{field_name}=`: `tuple` or `popoto.GeoField.Coordinates` (float, float) with Coordinates

`{field_name}__isnull=`: filter for null (`None`) values (if `null=True` is set on model field declaration)

`{field_name}_latitude=`: `float`

`{field_name}_longitude=`: `float`

`{field_name}_radius=`: `int` or `float`. Default is `10`

`{field_name}_radius_unit=`: One of `"m"`(meters), `"km"`(kilometers), `"ft"`(feet), `"mi"`(miles). Default is `"km"`(kilometers)


Example Queries:

```python
GeoModel.query.filter(coordinates=rome.coordinates, coordinates_radius=5, coordinates_radius_unit='km')

GeoModel.query.filter(coordinates_latitude=41.902782, coordinates_longitude=12.496366)
```
