# Introduction

##### Popoto - A Redis ORM (Object-Relational Mapper)

**Popoto** is an ORM (Object-Relational Mapper) designed specifically for the [Redis](https://redis.io) cache database. It provides a familiar syntax that is easy to use for developers familiar with [Django](https://www.djangoproject.com/) and [Flask](https://flask.palletsprojects.com/).

Redis operates in RAM memory, which makes reading and writing operations typically 10-20x faster compared to PostgreSQL and other traditional relational databases.

## Quick Start

Here's a simple example of how to use Popoto:

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    fav_color = Field()
    
Person.create(name="Lalisa Manobal", fav_color = "yellow")

lisa = Person.query.get(name="Lalisa Manobal")

print(f"{lisa.name} likes {lisa.fav_color}.")
```

This will output: 'Lalisa Manobal likes yellow.' 


## Features

 - Fast data storage and queries
 - Familiar syntax, similar to Django models
 - Geometric distance search
 - Timeseries for streaming data and finance tickers
 - Compatibility with Pandas, Xarray for N-dimensional matrix search
 - [PubSub](pubsub.md) for message queues, streaming data processing
 - [Django](django.md) utilities for Redis-based user authentication, session, and user admin

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Geometric distance search
 - Timeseries for streaming data and finance tickers
 - compatible with Pandas, Xarray for N-dimensional matrix search
 -  for message queues, streaming data processing
 - [Django](django.md) utilities for Redis-based user authentication, session, and user admin

**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
It is currently being used in production for:

 - Triggering buy/sell actions from streaming price data
 - Robots sending each other messages for teamwork
 - Compressing sensor data and training neural networks

## Installation

To install Popoto, use pip:

``` bash
pip install popoto
```

You can find [Popoto on PyPi](https://pypi.org/project/popoto/).

Set `REDIS_URL` in your deployed environment. This is optional on local:

``` python
REDIS_URL = "redis://HOST[:PORT]/DATABASE[?password=PASSWORD]"
```

## Defining a Model

``` python
import popoto

class Person (popoto.Model)
    name = popoto.KeyField(max_length=100)
    favorite_color = popoto.Field(null=True)

```

See [Models and Fields](fields.md) for more options.

## Creating Instances

``` python
lisa = Person(name="Lalisa Manobal")
lisa.favorite_color = "yellow"
lisa.save()

# single line command
lisa = Person.create(name="Lalisa Manobal", favorite_color = "yellow")
```

## Retrieving Instances

``` python
lisa = Person.query.get("Lalisa Manobal")
print(f"{lisa.name} likes {lisa.favorite_color}.")
```

This will output: 'Lalisa Manobal likes yellow.'

See [Making Queries](query.md) for more options.

### Deleting Instances

``` python
lisa.delete()
```

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin), the world's smallest dolphin subspecies.
Dolphins are fast, agile, and work together in social groups. Similarly, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
