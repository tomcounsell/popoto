# Introduction

Popoto is an ORM for [Redis](https://redis.io) and [Valkey](https://valkey.io) databases.
The familiar syntax makes it easy to use for [Django](https://www.djangoproject.com/) and [Flask](https://flask.palletsprojects.com/) developers.

Redis and Valkey are storage systems that operate in RAM memory.
Since they work at RAM memory level, reading/writing is typically 10-20x faster
compared to PostgreSQL and other traditional relational databases.

!!! tip "Valkey Support"
    Popoto fully supports Valkey, the open-source Redis fork. Simply point your `REDIS_URL` at a Valkey server - no code changes needed.


## Simple Example

Here's a complete example showing how to define a model, create an instance, and retrieve it.

```python
from popoto import Model, KeyField, Field, SortedField, GeoField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()

Restaurant.create(
    name="Burger Palace",
    cuisine="American",
    rating=4.5,
    location=(40.7128, -74.0060)
)

restaurant = Restaurant.query.get(name="Burger Palace")

print(f"{restaurant.name} serves {restaurant.cuisine} food.")
# => 'Burger Palace serves American food.'
```


## Features

Popoto provides a fast, familiar interface for working with Redis and Valkey.

 - **Full Redis and Valkey support** - works with both out of the box
 - very fast stores and queries
 - familiar syntax, similar to Django models
 - [Async operations](async.md) for asyncio-based applications
 - [Multi-tenancy](multi-tenancy.md) support via KeyField namespacing
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

See [Configuration](configuration.md) for full connection options.

### Define a Model

Start by defining a model class. Models inherit from `popoto.Model` and define fields for the data you want to store.

```python
from popoto import Model, KeyField, Field, SortedField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
```

See [Models and Fields](fields.md) for all Model and Field options.

See [Model Meta Options](meta.md) for configuration like default ordering and TTL.

### Create Instances

You can create instances by constructing the model and calling `save()`, or use the `create()` shortcut.

```python
restaurant = Restaurant(name="Burger Palace")
restaurant.cuisine = "American"
restaurant.rating = 4.5
restaurant.save()

# single line command
restaurant = Restaurant.create(name="Burger Palace", cuisine="American", rating=4.5)
```

### Retrieve Instances

Use the query interface to retrieve instances by their key fields.

```python
restaurant = Restaurant.query.get(name="Burger Palace")
print(f"{restaurant.name} serves {restaurant.cuisine} food.")
# => 'Burger Palace serves American food.'
```

See [Making Queries](query.md) for all Query and Filter options.

### Delete Instances

Delete an instance by calling its `delete()` method.

```python
restaurant.delete()
```

To delete all instances of a model, use `delete_all()`:

```python
# Delete all restaurants and clean up all indexes
Restaurant.delete_all()

# Delete multiple models (delete referencing models first)
for model in [Order, MenuItem, Restaurant]:
    model.delete_all()
```

!!! warning
    Always use Popoto's delete methods instead of Redis `DEL` or `FLUSHDB`. Popoto maintains
    secondary indexes (sorted sets, geo sets, unique constraints) that must be cleaned up
    properly. See [Bulk Operations](api-reference.md#modeldelete_all) for details.

### Async Operations

All operations have async counterparts for use in asyncio applications.

```python
import asyncio

async def main():
    restaurant = await Restaurant.async_create(
        name="Burger Palace", cuisine="American", rating=4.5
    )
    loaded = await Restaurant.query.async_get(name="Burger Palace")
    await loaded.async_delete()

asyncio.run(main())
```

See [Async Operations](async.md) for complete async API documentation and examples.

![](/static/popoto.png)

Popoto gets its name from the [Maui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspecies - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
