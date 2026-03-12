### Status
[![pypi package](https://badge.fury.io/py/popoto.svg)](https://pypi.org/project/popoto)
[![total downloads](https://pepy.tech/badge/popoto)](https://pepy.tech/project/popoto)
[![documentation status](https://readthedocs.org/projects/popoto/badge/?version=latest)](https://popoto.readthedocs.io/en/latest/?badge=latest)

### Documentation: [**popoto.readthedocs.io**](https://popoto.readthedocs.io/en/latest/)


# Popoto - A Redis/Valkey ORM (Object-Relational Mapper)

## Install

```
pip install popoto
```

## Basic Usage

``` python
from popoto import Model, KeyField, Field, SortedField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field()
    rating = SortedField(type=float)

Restaurant.create(name="Burger Palace", cuisine="American", rating=4.5)

restaurant = Restaurant.query.get(name="Burger Palace")

print(f"{restaurant.name} serves {restaurant.cuisine} food.")
# => "Burger Palace serves American food."
```

### **Popoto** Features

 - very fast stores and queries
 - familiar syntax, similar to Django models
 - Async operations for asyncio-based applications
 - Geometric distance search
 - Timeseries for streaming data
 - compatible with Pandas, Xarray for N-dimensional matrix search
 - PubSub for message queues, streaming data processing
 - **Full Redis and Valkey support** - works with both out of the box

**Popoto** is ideal for streaming data. The pub/sub module allows you to trigger state updates in real time.
Currently being used in production for:

 - trigger buy/sell actions from streaming price data
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks


## Advanced Usage

``` python
import popoto
from popoto import Relationship, DatetimeField

class Restaurant(popoto.Model):
    name = popoto.KeyField()
    cuisine = popoto.Field()
    rating = popoto.SortedField(type=float)
    location = popoto.GeoField()

class Order(popoto.Model):
    order_id = popoto.AutoKeyField()
    restaurant = Relationship(Restaurant)
    total = popoto.SortedField(type=float)
    status = popoto.Field(default="pending")
    created_at = DatetimeField(auto_now_add=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days
```


## Save Instances

``` python
restaurant = Restaurant(name="Burger Palace")
restaurant.cuisine = "American"
restaurant.rating = 4.5
restaurant.location = (40.7128, -74.0060)
restaurant.save()

order = Order.create(restaurant=restaurant, total=24.99)
```


## Queries

``` python
from datetime import datetime, timedelta

midtown = (40.7549, -73.9840)
yesterday = datetime.now() - timedelta(days=1)

nearby_restaurants = Restaurant.query.filter(
    location=midtown,
    location_radius=5, location_radius_unit='km',
    rating__gte=4.0
)

print(len(nearby_restaurants))
# => 1

recent_orders = Order.query.filter(
    created_at__gte=yesterday,
    total__gte=10.00
)
```


# Documentation

Documentation is available at [**popoto.readthedocs.io**](https://popoto.readthedocs.io/en/latest/)

Please create new feature and documentation related issues [github.com/tomcounsell/popoto/issues](https://github.com/tomcounsell/popoto/issues) or make a pull request with your improvements.


# License

Popoto ORM is released under the MIT Open Source license.


# Popoto Community

Questions, bug reports, and feature requests are welcome on [GitHub Issues](https://github.com/tomcounsell/popoto/issues) and [GitHub Discussions](https://github.com/tomcounsell/popoto/discussions). Contributions via pull request are encouraged.

![](/static/popoto.png)

Popoto gets its name from the [Maui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspecies - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and Valkey to make it easy to manage streaming timeseries data and object persistence.
