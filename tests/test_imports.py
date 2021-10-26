import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB, print_redis_info
print_redis_info()
from src import popoto


class KeyValueModel(popoto.Model):
    """
    should look and quack like a simple key value store
    """
    key = popoto.KeyField()
    value = popoto.Field(is_null=True, default=None)

duck = KeyValueModel()
duck.key = "Sally"
duck.value = "your most sassy LINE friend"
duck.save()

class AutoKeyModel(popoto.Model):
    says = popoto.Field(default="onomatopoeia")

chicken = AutoKeyModel()
chicken.value = "cluck cluck"

class FarmAnimal(popoto.Model):
    id = popoto.KeyField()
    name = popoto.Field(type=str, max_length=100)
    age = popoto.Field(type=int, default=0)

goat = FarmAnimal()
goat.id = "AB12"
goat.name = "Pickles"
goat.age = 3
goat.save()

# class FarmMammal(FarmAnimal):
#     id = popoto.KeyField(key_prefix = "mammal")

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
