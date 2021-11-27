import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class KeyValueModel(popoto.Model):
    key = popoto.KeyField()
    value = popoto.Field()

duck = KeyValueModel()
duck.key = "Sally"
duck.value = "your most sassy LINE friend"
duck.save()
assert duck == KeyValueModel.query.get("Sally")

random_fact_1 = KeyValueModel.create(key="oldest vegetable", value="peas")
assert random_fact_1 == KeyValueModel.query.get(key=random_fact_1.key)

for item in KeyValueModel.query.all():
    item.delete()
assert len(KeyValueModel.query.all()) == 0
assert len(POPOTO_REDIS_DB.keys("KeyValueModel*")) == 0


class AutoKeyModel(popoto.Model):
    key = popoto.AutoKeyField()
    value = popoto.Field()


random_fact_2 = AutoKeyModel.create(value="original CocaCola was green color")
assert random_fact_2 == AutoKeyModel.query.get(key=random_fact_2.key)


class HiddenAutoKeyModel(popoto.Model):
    value = popoto.Field()


random_fact_3 = HiddenAutoKeyModel.create(value="regulation golf balls have 366 dimples")
assert random_fact_3 in HiddenAutoKeyModel.query.all()
first_raw_key = POPOTO_REDIS_DB.keys("HiddenAutoKeyModel:*")[
    0].decode()  # eg. 'HiddenAutoKeyModel:d69146047a054bad99e8e5ba27a95835'
first_item_auto_key = HiddenAutoKeyModel.query.all()[0]._auto_key
assert first_raw_key.split(":")[1] == first_item_auto_key

for item in HiddenAutoKeyModel.query.all():
    item.delete()


class ManyKeyModel(popoto.Model):
    key1 = popoto.KeyField()
    key2 = popoto.KeyField()
    key3 = popoto.KeyField()
    value = popoto.Field(type=float)


random_fact_4 = ManyKeyModel.create(key1="calories", key2="stamp", key3="lick", value=0.1)
assert random_fact_4 == ManyKeyModel.query.get(key1="calories", key2="stamp", key3="lick")

for item in ManyKeyModel.query.all():
    item.delete()
