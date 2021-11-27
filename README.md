## Status
[![pypi package](https://badge.fury.io/py/popoto.svg)](https://pypi.org/project/popoto)
[![documentation status](https://readthedocs.org/projects/popoto/badge/?version=latest)](https://popoto.readthedocs.io/en/latest/?badge=latest)

[![weekly downloads](https://pepy.tech/badge/popoto/week)](https://pepy.tech/project/popoto)
[![total downloads](https://pepy.tech/badge/popoto)](https://pepy.tech/project/popoto) 


# Popoto - A Redis ORM (Object-Relational Mapper)

## Install

```
pip install popoto
```

## Basic Usage

``` python
import popoto

class Person (popoto.Model)
    name = popoto.KeyField()
    fav_color = popoto.Field()

Person.create(name="Lalisa Manobal", fav_color = "yellow")
lisa = Person.query.get("Lalisa Manobal")

print(f"{lisa.name} likes {lisa.fav_color}.")
> 'Lalisa Manobal likes yellow.'
```

### **Popoto** is a simple ORM for your cache database on Redis.

 - very fast stores and queries ✅
 - familiar syntax, similar to Django models ✅
 - scale up matrix data to N-dimensions, compatible with Pandas, Xarray 🚧
 - Geo for geometric map search ✅
 - Timeseries for streaming data and finance tickers 🚧
 - Graph for relationship mapping (like Neo4j) 🚧
 - PubSub for message queues, streaming data processing, notification microservices 🚧

**Popoto** is ideal for streaming data. The pub/sub utilities allow you to trigger data state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks


## Advanced Usage

``` python
import popoto

class Person(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField()
    title = popoto.KeyField()
    level = popoto.SortedField(type=int)
    last_active = popoto.SortedField(type=datetime)
    location = popoto.GeoField()
```


## Save Instances

``` python
lisa = Person(username="@LalisaManobal", title="Queen", last_active=datetime.now())
lisa.level = 99
lisa.location = (48.856373, 2.353016)  # Hôtel de Ville, Fashion Week 2021
lisa.save()
```


## Queries

``` python

paris_latitude, paris_longitude = (48.864716, 2.349014)
query_results = Person.query.filter(
    title__startswith="Queen",
    level__lt=100,
    last_active__gt=(datetime.now()-timedelta(days=1)),
    location_latitude=paris_latitude,
    location_longitude=paris_longitude,
    location_radius=5, location_radius_unit='km'
)

len(query_results)
>>> 1

print(query_results)
>>> [{
    'uuid': 'f1063355b14943ed91fa1e1697806c4f', 
    'username': '@LalisaManobal', 
    'title': 'Queen', 
    'level': 99, 
    'last_active': datetime.datetime(2021, 11, 21, 14, 47, 19, 911023), 
    'location': (48.856373, 2.353016)
}, ]

lisa.delete()
>>> True
```


# Documentation

Documenation is available at [**popoto.readthedocs.io**](https://popoto.readthedocs.io/en/latest/)

Please create new feature and documentation related issues [github.com/tomcounsell/popoto/issues](https://github.com/tomcounsell/popoto/issues) or make a pull request with your improvements.


# License

Popoto ORM is released under the MIT Open Source license.


# Popoto Community

Please post your questions on [Stack Overflow](http://stackoverflow.com/questions/tagged/popoto).

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
