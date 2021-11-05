import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto

# MODELS WITH A SINGLE KEYFIELD

class KeyValueModel(popoto.Model):
    key = popoto.KeyField()
    value = popoto.Field()


random_fact_1 = KeyValueModel.create(key="oldest vegetable", value="peas")
assert random_fact_1 == KeyValueModel.query.get(key=random_fact_1.key)


class AutoKeyModel(popoto.Model):
    key = popoto.AutoKeyField()
    value = popoto.Field()


random_fact_2 = AutoKeyModel.create(value="original CocaCola was green color")
assert random_fact_2 == AutoKeyModel.query.get(key=random_fact_2.key)


class HiddenAutoKeyModel(popoto.Model):
    value = popoto.Field()


random_fact_3 = HiddenAutoKeyModel.create(value="regulation golf balls have 366 dimples")
assert random_fact_3 in HiddenAutoKeyModel.query.all()


# MODELS WITH MORE THAN ONE KEYFIELD
class MultiKeyModel(popoto.Model):
    key1 = popoto.KeyField()
    key2 = popoto.KeyField()
    key3 = popoto.KeyField()
    value = popoto.Field(type=float)

random_fact_4 = MultiKeyModel.create(key1="calories", key2="stamp", key3="lick", value=0.1)
assert random_fact_4 == MultiKeyModel.get(key1="calories", key2="stamp", key3="lick")
