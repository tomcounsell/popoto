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

same_duck = KeyValueModel.query.get("Sally")
same_duck == duck
>>> True
```

## Filter query results

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

## Values

Returns dictionaries, rather than model instances. Each of those dictionaries represents an object, with the keys corresponding to the attribute names of model objects.
Specify the fields with a tuple of field names. Each dictionary will contain only the field keys/values for the fields you specify.

``` python
Animal.query.filter(values=("name", "color"))
>>> [{"name": "salamander", "color" "green"}, ...]
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
ordering works for field types: `str`, `int`, `float`, `decimal`, `time`, `date`, `datetime`


## Limit Number of Results

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


## Values

Returns dictionaries, rather than model instances.
Each of those dictionaries represents an object, with the keys corresponding to the attribute names of model objects.
values requires a tuple of field names
example:

``` python
Movies.query.filter(name="Life Of Pi", values=("name",))
>>> [{"name": "Life Of Pi"}, ]
```


## KeyField query filters

`{field_name}=`: exact match

`{field_name}__contains=`: partial string match

`{field_name}__startswith=`: partial string match

`{field_name}__endswith=`: partial string match

`{field_name}__in=`: is exact match for any element in provided list


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
```


## GeoField query filters

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
