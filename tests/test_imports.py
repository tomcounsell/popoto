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
    value = fields.Field(default="default string")

class MyModel(models.Model):
    my_id = fields.KeyField(auto=True)
    # my_score = fields.SortedSetField(sort_key=True)
    my_value = fields.Field(is_null=True, max_length=2e10)


mm = MyModel()
mm.my_id = "thing123"
mm.my_value = "it is what it is"
mm.save()


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
