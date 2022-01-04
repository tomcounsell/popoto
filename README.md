### Status
[![pypi package](https://badge.fury.io/py/popoto.svg)](https://pypi.org/project/popoto)
[![total downloads](https://pepy.tech/badge/popoto)](https://pepy.tech/project/popoto)
[![documentation status](https://readthedocs.org/projects/popoto/badge/?version=latest)](https://popoto.readthedocs.io/en/latest/?badge=latest)

### Documentation: [**popoto.readthedocs.io**](https://popoto.readthedocs.io/en/latest/)


# Popoto - A Redis ORM (Object-Relational Mapper)

## Install

```
pip install popoto
```

## Basic Usage

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

### **Popoto** Features

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Geometric distance search
 - Timeseries for streaming data and finance tickers
 - compatible with Pandas, Xarray for N-dimensional matrix search 🚧
 - PubSub for message queues, streaming data processing
 
**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
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
    invited_by = popoto.Relationship(model=Person)
```


## Save Instances

``` python
lisa = Person(username="@LalisaManobal")
lisa.title = "Queen"
lisa.level = 99
lisa.location = (48.856373, 2.353016)  # Hôtel de Ville, Fashion Week 2021
lisa.last_active = datetime.now()
lisa.save()
```


## Queries

``` python

paris_lat_long = (48.864716, 2.349014)
yesterday = datetime.now() - timedelta(days=1)

query_results = Person.query.filter(
    title__startswith="Queen",
    level__lt=100,
    last_active__gt=yesterday,
    location=paris_lat_long,
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

lisa = query_results[0]
lisa.delete()
>>> True
```


# Documentation

Documentation is available at [**popoto.readthedocs.io**](https://popoto.readthedocs.io/en/latest/)

Please create new feature and documentation related issues [github.com/tomcounsell/popoto/issues](https://github.com/tomcounsell/popoto/issues) or make a pull request with your improvements.


# License

Popoto ORM is released under the MIT Open Source license.


# Popoto Community

Please post your questions on [Stack Overflow](http://stackoverflow.com/questions/tagged/popoto).

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.

For help building applications with Python/Redis, contact [Tom Counsell](https://tomcounsell.com) on [LinkedIn.com/in/tomcounsell](https://linkedin.com/in/tomcounsell)
