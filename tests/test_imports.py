import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto
from src.popoto import Model, ModelBase
from src.popoto import Field
from src.popoto import KeyField, AutoKeyField, UniqueKeyField
from src.popoto import IndexField, GeoField, SortedField, SetField, ListField


class KeyValueModel(popoto.Model):
    """
    should look and quack like a simple key value store
    """
    key = popoto.KeyField()
    value = popoto.Field(null=True, default=None)

duck = KeyValueModel()
duck.key = "Sally"
duck.value = "your most sassy LINE friend"
duck.save()

same_duck = KeyValueModel.query.get("Sally")
same_duck == duck

class AutoKeyModel(popoto.Model):
    says = popoto.Field(default="onomatopoeia")

chicken = AutoKeyModel()
chicken.value = "cluck cluck"
chicken._meta.fields['_auto_key'].default
chicken.db_key

class FarmAnimal(popoto.Model):
    id = popoto.KeyField()
    name = popoto.Field(type=str, max_length=100)
    age = popoto.Field(type=int, default=0)

goat = FarmAnimal()
goat.id = "AB12"
goat.name = "Pickles"
goat.age = 3
goat.save()

same_goat = FarmAnimal.get(id="AB12")

class Racer(popoto.Model):
    name = popoto.KeyField()
    fastest_lap = popoto.Field(type=float, null=True)

tim = Racer.create(name="Tim", fastest_lap=54.92)
bob = Racer.create(name="Bob", fastest_lap=57.11)
joe = Racer.create(name="Joe", fastest_lap=51.90)
racers_under_55 = Racer.query.filter(fastest_lap__lt=55)
for racer in racers_under_55:
    print(racer.name)

class Restaurants(popoto.Model):
    name = popoto.KeyField()
    location = popoto.GeoField()

from itertools import chain
list(chain(*[field.get_filter_query_params() for field in Restaurants._meta.fields.values()]))

class MyPublishableModel(popoto.Model):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher = popoto.Publisher()

    def save(self, pipeline=None, *args, **kwargs):
        super().save(pipeline=pipeline, *args, **kwargs)
        self.publisher.publish({
            "key": self._db_key,
            "value": self.value
        }, pipeline=pipeline)


pub_thing = MyPublishableModel()
