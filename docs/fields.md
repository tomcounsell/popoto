# Models and Fields

Declare a Model by inheriting from `popoto.Model`.
Then simply define your choice of fields.
Models are flexible to allow any number of fields and types of fields.
A primary key field (or at least the Redis equivalent) will be automatically added if necessary.

```python
import popoto

class MyObject(popoto.Model):
    # add fields
```

# KeyField
A KeyField makes objects fast and easy to query for.
In the background, Popoto uses all KeyFields to compile the primary key on Redis.
There almost no performance downside to using many KeyFields.

``` python
import popoto

class User(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField(max_length=20)
    name = popoto.KeyField(max_length=100, unique=False)
```


It's recommended that at least one KeyField has `unique=True` enforced. 

These KeyFields will each enforce uniqueness across all saved instances:
`AutoKeyfield`, `UniqueKeyfield`, and `KeyField(unique=True)` 

```python
class User(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField()
    email = popoto.KeyField(unique=True)
```

However, it is enough for all KeyFields to be considered "unique together"
In this example, a unique Box is the combination of dimensions. The combined dimensions *together* will be *unique* to every instance.

```python
class Box(popoto.Model):
    length = popoto.KeyField(type=float)
    width = popoto.KeyField(type=float)
    height = popoto.KeyField(type=float)
```

Finally, it is also possible to declare a Model without a KeyField and Popoto will create and maintain a hidden unique key.

```python
class BitcoinPrice(popoto.Model):
    usd_value = popoto.Field(type=Decimal)
    timestamp = popoto.SortedField(type=datetime.datetime)
```

The above example may be useful in situations where all queries are made via special purpose fields, such as `SortedField`, `GeoField`, or `GraphField`

# Field

All fields inherit from base `Field`. A basic `Field` on any model will provide type validation on create and update events. 

The following types are supported for use:
`int`, `float`, `Decimal`, `str`, `bool`, `list`, `dict`, `bytes`, `datetime.date`, `datetime.datetime`, `datetime.time`

The default type for `value = popoto.Field()` is type `str` - string

```python
from decimal import Decimal
import datetime

class EveryTypeModel(popoto.Model):
    string_val = popoto.Field(type=str)
    int_val = popoto.Field(type=int)
    float_val = popoto.Field(type=float)
    decimal_val = popoto.Field(type=Decimal)
    boolean_val = popoto.Field(type=bool)
    list_val = popoto.Field(type=list)
    dict_val = popoto.Field(type=dict)
    bytes_val = popoto.Field(type=bytes)
    date_val = popoto.Field(type=datetime.date)
    datetime_val = popoto.Field(type=datetime.datetime)
    time_val = popoto.Field(type=datetime.time)
```

## Null Values

By default, all fields are considered required unless `null=True` is specified in the model field declaration.
All Fields can allow null values.

```python
class MyModel(popoto.Model):
    optional_value = popoto.Field(null=True)
```

## Default Values

All fields will accept a `default` value for new objects.

```python
class MyModel(popoto.Model):
    status = popoto.Field(type=str, default="unknown")
    is_true = popoto.Field(type=bool, default=False)
    access_count = popoto.Field(type=int, default=0)
```

## String Max Length

```python
class Tweet(popoto.Model):
    text = popoto.Field(type=str, max_length=280)
```

# SortedField

Use a `SortedField` for numerical attributes. 
A `SortedField` provides fast and efficient (Redis ZADD, ZRANGE) access to ordered instances. Querying for instances by order of a timestamp or attribute counter is one of the most powerful and common reasons for employing a Redis database.

```python
import datetime

class SortedDateModel(popoto.Model):
    name = popoto.KeyField()
    birthday = popoto.SortedField(type=datetime.date)

lisa = SortedDateModel.create(name="Lisa", birthday=datetime.date(1997, 3, 27))
rose = SortedDateModel.create(name="Rose", birthday=datetime.date(1997, 2, 11))
jisoo = SortedDateModel.create(name="Jisoo", birthday=datetime.date(1995, 1, 3))
jennie = SortedDateModel.create(name="Jennie", birthday=datetime.date(1996, 1, 16))

oldest = SortedDateModel.query.filter(birthday__lt=datetime.date(1996, 1, 1))[0]
assert jisoo == oldest
younger_than_rose = SortedDateModel.query.filter(birthday__gt=rose.birthday)
assert lisa in younger_than_rose
```

# GeoField

Popoto provides a useful namedtuple `Coordinates`. Although, any tuple of `(float, float)` for `(latitude, longitude)` is allowed.

```python
from popoto.GeoField import Coordinates

class GeoModel(popoto.Model):
    name = popoto.KeyField()
    coordinates = popoto.GeoField()


rome = GeoModel.create(
    name="Rome", 
    coordinates=Coordinates(latitude=41.902782, longitude=12.496366)
)

vatican = GeoModel.create(
    name="Vatican",
    coordinates=Coordinates(latitude=41.904755, longitude=12.454628)
)

assert vatican in GeoModel.query.filter(coordinates=rome.coordinates, coordinates_radius=5, coordinates_radius_unit='km')
```
