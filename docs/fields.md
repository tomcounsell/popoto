# Models and Fields

Define your object models

## KeyField

Every KeyField is made to be fast and easy to query for.
One or more KeyFields represent the primary index key stored in Redis. 

``` python
import popoto

class User(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField(max_length=20)
    name = popoto.KeyField(max_length=100, unique=False)
```


It's recommended that at least one KeyField has `unique=True` enforced. 

All of following key fields enforce uniqueness: 
`AutoKeyfield`, `UniqueKeyfield`, and `KeyField(unique=True)` 

```python
class User(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField()
    email = popoto.KeyField(unique=True)
```

However, it is enough for all KeyFields to be considered "unique together"

```python
class Box(popoto.Model):
    length = popoto.KeyField(type=float)
    width = popoto.KeyField(type=float)
    height = popoto.KeyField(type=float)
```
In the above example, a Box the combination of dimensions *together* must be *unique* to every instance.

Finally, it is also possible to declare a Model without a KeyField and popoto will create and maintain a hidden unique key.

```python
class BitcoinPrice(popoto.Model):
    usd_value = popoto.Field(type=Decimal)
    timestamp = popoto.SortedField(type=datetime.datetime)
```

This pattern may be used in situations where all queries are made via special purpose fields, such as `SortedField`, `GeoField`, or `GraphField`

## Field

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

### Null Values

By default, all fields are considered required unless `null=True` is specified in the model field declaration.
All Fields can allow null values.

```python
class MyModel(popoto.Model):
    optional_value = popoto.Field(null=True)
```

### Default Values

All fields will accept a `default` value for new objects.

```python
class MyModel(popoto.Model):
    status = popoto.Field(type=str, default="unknown")
    is_true = popoto.Field(type=bool, default=False)
    access_count = popoto.Field(type=int, default=0)
```

### String Max Length

```python
class Tweet(popoto.Model):
    text = popoto.Field(type=str, max_length=280)
```

