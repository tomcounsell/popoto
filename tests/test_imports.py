import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB, print_redis_info
print_redis_info()
from src.popoto import models, fields
from src.popoto.pubsub import Publisher, Subscriber


class KeyValueModel(models.Model):
    """
    should look and quack like a simple key value store
    """
    key = fields.KeyField()
    value = fields.Field(is_null=True, default=None)

duck = KeyValueModel()
duck.key = "Sally"
duck.value = "your most sassy LINE friend"
duck.save()

class AutoKeyModel(models.Model):
    says = fields.Field(default="onomatopoeia")

chicken = AutoKeyModel()
chicken.value = "cluck cluck"

class FarmAnimal(models.Model):
    id = fields.KeyField()
    name = fields.Field(type=str, max_length=100)
    age = fields.Field(type=int, default=0)

goat = FarmAnimal()
goat.id = "AB12"
goat.name = "Pickles"
goat.age = 3
goat.save()

# class FarmMammal(FarmAnimal):
#     id = fields.KeyField(key_prefix = "mammal")

class MyPublishableModel(models.Model):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher = Publisher()

    def save(self, pipeline=None, *args, **kwargs):
        super().save(pipeline=pipeline, *args, **kwargs)
        self.publisher.publish({
            "key": self._db_key,
            "value": self.value
        }, pipeline=pipeline)


pub_thing = MyPublishableModel()
