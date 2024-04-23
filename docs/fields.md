# Models and Fields

Declare a Model by inheriting from `Model`.
Then simply define your choice of fields.
Models are flexible to allow any number of fields and types of fields.
A primary key field (or at least the Redis equivalent) will be automatically added if necessary.

```python
from popoto import Model

class MyObject(Model):
    # add fields
```

## KeyField
A KeyField makes objects fast and easy to query for.
In the background, Popoto uses all KeyFields to compile the primary key on Redis.
There almost no performance downside to using many KeyFields.

``` python
from popoto import Model, AutoKeyField, UniqueKeyField, KeyField

class User(Model):
    uuid = AutoKeyField()
    username = UniqueKeyField(max_length=20)
    name = KeyField(max_length=100, unique=False)
```


It's recommended that at least one KeyField has `unique=True` enforced. 

These KeyFields will each enforce uniqueness across all saved instances:
`AutoKeyfield`, `UniqueKeyfield`, and `KeyField(unique=True)` 

```python
from popoto import Model, AutoKeyField, UniqueKeyField, KeyField

class User(Model):
    uuid = AutoKeyField()
    username = UniqueKeyField()
    email = KeyField(unique=True)
```

However, it is enough for all KeyFields to be considered "unique together"
In this example, a unique Box is the combination of dimensions. The combined dimensions *together* will be *unique* to every instance. And, 2 boxes with the same dimensions will be considered _identical_ and save to the same object.

```python
class Box(Model):
    length = KeyField(type=float)
    width = KeyField(type=float)
    height = KeyField(type=float)
```

Finally, it is also possible to declare a Model without a KeyField and Popoto will create and maintain a hidden unique key.

```python
from popoto import Model, DecimalField, SortedField
from datetime import datetime

class BitcoinPrice(Model):
    usd_value = DecimalField()
    timestamp = SortedField(type=datetime)
```

The above example may be useful in situations where all queries are made via special purpose fields, such as `SortedField`, `GeoField`, or `GraphField`

## Field

All fields inherit from base `Field`. A basic `Field` on any model will provide type validation on create and update events. 

The following types are supported for use:
`int`, `float`, `Decimal`, `str`, `bool`, `list`, `dict`, `bytes`, `datetime.date`, `datetime.datetime`, `datetime.time`

The default type for `value = Field()` is type `str` - string

```python
from popoto import *
from decimal import Decimal
from datetime import datetime, date, time

class EveryTypeModel(Model):
    string_val = Field(type=str)
    int_val = Field(type=int)
    float_val = Field(type=float)
    decimal_val = Field(type=Decimal)
    boolean_val = Field(type=bool)
    list_val = Field(type=list)
    set_val = Field(type=set)
    tuple_val = Field(type=tuple)
    dict_val = Field(type=dict)
    bytes_val = Field(type=bytes)
    date_val = Field(type=date)
    datetime_val = Field(type=datetime)
    time_val = Field(type=time)
```
Named fields shown below are equivalent to the fields above. You may declare fields to your preference.
```python 
class EveryTypeModel(Model):
    int_val = IntField()
    float_val = FloatField()
    decimal_val = DecimalField()
    string_val = StringField()
    boolean_val = BooleanField()
    list_val = ListField()
    set_val = SetField()
    tuple_val = TupleField()
    dict_val = DictField()
    bytes_val = BytesField()
    date_val = DateField()
    datetime_val = DatetimeField()
    time_val = TimeField()
```

### Null Values

KeyField and SortedField values are considered required `null=False` by default. All other fields are optional `null=True` by default. 
You may explicitly declare whether to allow null values using the `null` keywword

```python
class MyModel(Model):
    optional_value = Field(null=True)
    required_value = Field(null=False)
```

### Default Values

All fields will accept a `default` value for new objects.

```python
class MyModel(Model):
    status = Field(type=str, default="unknown")
    is_true = Field(type=bool, default=False)
    access_count = Field(type=int, default=0)
```

### String Max Length

Set a limit to string length. On SQL-like databases, this is often required. 
However, on Redis (and Popoto), there is no performance 
requirement or advantage to setting the `max_length`. 
Use it if you want Popoto to raise exceptions on model validation.


```python
class Tweet(Model):
    text = Field(type=str, max_length=280)
```

## SortedField

Use a `SortedField` for numerical attributes. 
A `SortedField` provides fast and efficient access to ordered instances (via Redis ZADD, ZRANGE). 
Querying for instances by order of a timestamp or attribute counter 
is one of the most powerful and common reasons for employing a Redis database.

A SortedField is necessary in order to use these query filters: `__lt=`, `__gt=`, etc.
See details on filters at [Query Filters](query.md)

```python
import datetime

class SortedDateModel(Model):
    name = KeyField()
    birthday = SortedField(type=datetime.date)

lisa = SortedDateModel.create(name="Lisa", birthday=datetime.date(1997, 3, 27))
rose = SortedDateModel.create(name="Rose", birthday=datetime.date(1997, 2, 11))
jisoo = SortedDateModel.create(name="Jisoo", birthday=datetime.date(1995, 1, 3))
jennie = SortedDateModel.create(name="Jennie", birthday=datetime.date(1996, 1, 16))

oldest = SortedDateModel.query.filter(birthday__lt=datetime.date(1996, 1, 1))[0]
assert jisoo == oldest
younger_than_rose = SortedDateModel.query.filter(birthday__gt=rose.birthday)
assert lisa in younger_than_rose
```

To use a SortedField also as a KeyField, use `SortedKeyField`

```python
class BitcoinPrice(Model):
    timestamp = SortedKeyField(type=datetime)
    usd_value = DecimalField()
```

In some cases, you may always sort against a required `KeyField`.
You will see significant performance improvements if you define `sort_by` (must be a tuple). 
Going forward, whenever a query filter is called on the SortedField, then all fields in 'sort_by' will also need to be defined in the filter.

In the example below, we've generalized the above BitcoinPrice model to be a generic asset.
Because we will query by timestamp ranges for only one asset at a time, we can declare `sort_by="asset"`.

```python
class AssetPrice(Model):
    asset = KeyField()
    timestamp = SortedKeyField(type=datetime, sort_by=('asset',))
    usd_value = DecimalField()

AssetPrice.query.filter(
    asset="Bitcoin", 
    timestamp__gte=datetime(2021,1,1), 
    timestamp__lt=datetime(2021,1,2)
)  ## return Bitcoin prices over 1 day period
```

Note: because `asset` was specified as a sort_by in the timestamp field, the query requires the asset to be defined.
This limitation, if you choose to use it, enables maximum performance with Redis.

## GeoField

The `GeoField` employs another popular Redis feature - geospatial search.
A common use case is searching for objects within a radius of another object.
Use the `GeoField` to set coordinates on a model to enable these powerful query filters.

Popoto provides a namedtuple `Coordinates`. Although, any tuple of `(float, float)` for `(latitude, longitude)` is allowed.

```python
from GeoField import Coordinates

class GeoModel(Model):
    name = KeyField()
    coordinates = GeoField()


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


## DataFrameField

The `DataFrameField` allows for storage of [Pandas DataFrame](https://pandas.pydata.org/docs/reference/frame.html) objects for generic blocks of tabular data.
A common use case is storing machine learning training data and results. No more pesky csv files. Use a database!

```python
import pandas as pd

class DataModel(Model):
    name = KeyField()
    dataframe = DataFrameField()

chicago_home_prices = DataModel.create(
    name="Chicago Home Price",
    df=pandas.read_csv('home_price.csv')
)

chicago_home_prices.df.describe()
>>>
              Price         Year
count      5.000000     5.000000
mean   27600.000000  2016.000000
std     4878.524367     1.581139
min    22000.000000  2014.000000
25%    25000.000000  2015.000000
50%    27000.000000  2016.000000
75%    29000.000000  2017.000000
max    35000.000000  2018.000000
```


##  Reserved Field Names

The following names are reserved and cannot be used as field names:

- _limit_: is used in query.filter() to limit the size of the returned objects list
- _values_: is used in query.filter() to restrict which values are returned for objects
- _order_by_: is used in query.filter() to order the results

