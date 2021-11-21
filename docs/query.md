# Queries and Query Filters

Key-Value Storage and then some...

## Get Object by KeyField

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

## Query Filter by Key Partial Match

``` python
import popoto

class Animal(popoto.Model):
    name = popoto.KeyField()
    sound = popoto.Field(null=True, default=None)

sally = Animal.create(name="Sally", sound="quack")

Animal.query.filter(name__startswith="S")
>>> [{'name': 'Sally', 'sound': 'quack'}]
```
