# Introduction

##### Popoto - A Redis ORM (Object-Relational Mapper)

**Popoto** is an ORM for your [Redis](https://redis.io) cache database. 
The familiar syntax makes it easy to use for [Django](https://www.djangoproject.com/) and [Flask](https://flask.palletsprojects.com/) developers.

Redis is a storage system that operates in RAM memory. 
Since it works at RAM memory level, reading/writing is typically 10-20x faster
compared to PostgreSQL and other traditional relational databases.


## Simple Example

``` python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    fav_color = Field()

Person.create(name="Lalisa Manobal", fav_color = "yellow")

lisa = Person.query.get(name="Lalisa Manobal")

print(f"{lisa.name} likes {lisa.fav_color}.")
> 'Lalisa Manobal likes yellow.'
```


## Features

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Geometric distance search
 - Timeseries for streaming data and finance tickers
 - compatible with Pandas, Xarray for N-dimensional matrix search
 - PubSub for message queues, streaming data processing

**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks

## Getting Started

### Install

``` bash
pip install popoto
```

[see Popoto on PyPi](https://pypi.org/project/popoto/)

Set `REDIS_URL` in your deployed env. Optional on local
``` python
REDIS_URL = "redis://HOST[:PORT]/DATABASE[?password=PASSWORD]"
```

### Define a Model

``` python
import popoto

class Person (popoto.Model)
    name = popoto.KeyField(max_length=100)
    favorite_color = popoto.Field(null=True)

```

See [Models and Fields](fields.md) for all Model and Field options.

### Create Instances

``` python
lisa = Person(name="Lalisa Manobal")
lisa.favorite_color = "yellow"
lisa.save()

# single line command
lisa = Person.create(name="Lalisa Manobal", favorite_color = "yellow")
```

### Retreive Instances

``` python
lisa = Person.query.get("Lalisa Manobal")
print(f"{lisa.name} likes {lisa.favorite_color}.")
'Lalisa Manobal likes yellow.'
```

See [Making Queries](query.md) for all Query and Filter options.

### Delete Instances

``` python
lisa.delete()
```

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
