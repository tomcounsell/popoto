# Popoto - A Redis ORM (Object-Relational Mapper)

# Install

```
pip install popoto
```

# Basic Usage

``` python
import popoto

class Person (popoto.Model)
    name = popoto.KeyField()
    fav_color = popoto.Field()

lisa = Person.create(name="Lalisa Manobal", fav_color = "yellow")
lisa = Person.query.get("Lalisa Manobal")

print(f"{lisa.name} likes {lisa.fav_color}.")
> 'Lalisa Manobal likes yellow.'
```


**Popoto** is a simple ORM for your cache database on Redis.

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - scale up matrix data to N-dimensions, compatible with Pandas, Xarray
 - Geo for geometric map search
 - Timeseries for streaming data and finance tickers
 - Graph for relationship mapping (like Neo4j)
 - PubSub for message queues, streaming data processing, notification microservices

**Popoto** is ideal for streaming data. The pub/sub utilities allow you to trigger data state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks

# Install

```
pip install popoto
```

for deployment, set
```
REDIS_URL = "redis://HOST[:PORT]/DATABASE[?password=PASSWORD]"
```

# Quickstart

``` python
import popoto

class Person (popoto.Model)
    name = popoto.KeyField(max_length=100)
    favorite_color = popoto.Field(null=True)

```

See [Models and Fields](fields.md) for all Model and Field options.

## Storing Objects

``` python
lisa = Person(name="Lalisa Manobal")
lisa.favorite_color = "yellow"
lisa.save()

# single line command
lisa = Person.create(name="Lalisa Manobal", favorite_color = "yellow")
```

## Retreive Objects

``` python
lisa = Person.query.get("Lalisa Manobal")
print(f"{lisa.name} likes {lisa.favorite_color}.")
'Lalisa Manobal likes yellow.'
```

See [Making Queries](query.md) for all Query and Filter options.

## Delete Objects

``` python
lisa.delete()
```

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
