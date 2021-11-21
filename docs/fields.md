# Models and Fields

Define your object models

## KeyField

``` python
import popoto

class User(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField(max_length=20)
    name = popoto.KeyField(max_length=100, unique=False)
```
