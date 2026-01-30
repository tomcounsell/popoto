# Introduction

Popoto is an ORM for your [Redis](https://redis.io) cache database.
The familiar syntax makes it easy to use for [Django](https://www.djangoproject.com/) and [Flask](https://flask.palletsprojects.com/) developers.

Redis is a storage system that operates in RAM memory.
Since it works at RAM memory level, reading/writing is typically 10-20x faster
compared to PostgreSQL and other traditional relational databases.


## Simple Example

Here's a complete example showing how to define a model, create an instance, and retrieve it.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    fav_color = Field()

Person.create(name="Lalisa Manobal", fav_color="yellow")

lisa = Person.query.get(name="Lalisa Manobal")

print(f"{lisa.name} likes {lisa.fav_color}.")
# => 'Lalisa Manobal likes yellow.'
```


## Features

Popoto provides a fast, familiar interface for working with Redis.

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Geometric distance search
 - Timeseries for streaming data
 - compatible with Pandas, Xarray for N-dimensional matrix search
 - [PubSub](pubsub.md) for message queues, streaming data processing

**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks

## Getting Started

### Install

Install Popoto using pip.

```bash
pip install popoto
```

[see Popoto on PyPi](https://pypi.org/project/popoto/)

Set `REDIS_URL` in your deployed environment. This is optional on local development.

```python
REDIS_URL = "redis://HOST[:PORT]/DATABASE[?password=PASSWORD]"
```

### Define a Model

Start by defining a model class. Models inherit from `popoto.Model` and define fields for the data you want to store.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField(max_length=100)
    favorite_color = Field(null=True)
```

See [Models and Fields](fields.md) for all Model and Field options.

See [Model Meta Options](meta.md) for configuration like default ordering and TTL.

### Create Instances

You can create instances by constructing the model and calling `save()`, or use the `create()` shortcut.

```python
lisa = Person(name="Lalisa Manobal")
lisa.favorite_color = "yellow"
lisa.save()

# single line command
lisa = Person.create(name="Lalisa Manobal", favorite_color="yellow")
```

### Retrieve Instances

Use the query interface to retrieve instances by their key fields.

```python
lisa = Person.query.get(name="Lalisa Manobal")
print(f"{lisa.name} likes {lisa.favorite_color}.")
# => 'Lalisa Manobal likes yellow.'
```

See [Making Queries](query.md) for all Query and Filter options.

### Delete Instances

Delete an instance by calling its `delete()` method.

```python
lisa.delete()
```

![](/static/popoto.png)

Popoto gets its name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspecies - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
