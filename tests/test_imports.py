import sys
import os

from src.popoto.pubsub.publisher import Publisher

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto.src.popoto.redis_db import print_redis_info
print_redis_info()
from popoto.src.popoto import models


class MyModel(models.RedisModel):
    my_id = models.ModelKey(auto=True)
    my_score = models.ModelField(sort_key=True)
    my_value = models.ModelField(is_null=True, max_length=2e10)


mm = MyModel()
mm.my_id = "thing123"
mm.my_value = "it is what it is"
mm.save()


class TestKeyValueModel(models.KeyValueModel):
    key = models.ModelKey()
    value = models.ModelField(is_null=True, default="")

new_thing = TestKeyValueModel()
new_thing.key = "thing123"
new_thing.value = "it is what it is"
new_thing.save()

class MyPublishableModel(models.RedisModel):
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
