from ...popoto.src.popoto.models import RedisModel
from ...popoto.src.popoto.redis_db import POPOTO_REDIS_DB, print_redis_info
print_redis_info()
from ...popoto.src.popoto import KeyValueModel, GraphNodeModel, TimeseriesModel
from ...popoto.src.popoto import PublisherModel, SubscriberModel, ModelKey, ModelField


class MyModel(RedisModel):
    my_id = ModelKey()
    my_score = ModelField(sort_key=True)
    my_value = ModelField(is_null=True, max_length=2e10)


mm = MyModel()
mm.my_id = "thing123"
mm.my_value = "it is what it is"
mm.save()


class TestKeyValueModel(KeyValueModel):
    key = ModelKey()
    value = ModelField(is_null=True, default="")

new_thing = TestKeyValueModel()
new_thing.key = "thing123"
new_thing.value = "it is what it is"
new_thing.save()
