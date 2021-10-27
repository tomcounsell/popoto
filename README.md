# Popoto - A Redis ORM (object relational mapper)

Popoto is a simple ORM for your cache database on Redis. 

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - scale up matrix data to N-dimensions, compatible with Pandas, Xarray
 - Geo for geometric map search
 - Timeseries for streamting data and finance tickers
 - Graph for relationship mapping (like Neo4j)
 - PubSub for message queues, streaming data processing, notification microservices

Popoto is ideal for streaming data. The pub/sub utilities allow you to trigger data state updates in real time.
Currently being used in production for:

- trigger buy/sell actions from streaming price data
 - trigger user notifications from real time analytics
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

```
import popoto

class Person (popoto.Model)
    name = popoto.KeyField(max_length=100)
    favorite_color = popoto.Field(null=True)
    
```

## Storing Objects

```
lisa = Person(name="Lalisa Manobal")
lisa.favorite_color = "yellow"
lisa.save()
```

## Retreive Objects

```
lisa = Person.get(name="Lalisa Manobal")
print(f"{lisa.name} likes {lisa.favorite_color}.")
'Lalisa Manobal likes yellow.'
```

## Delete Objects

```
lisa.delete()
```

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.
