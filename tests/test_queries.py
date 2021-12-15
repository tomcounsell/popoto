import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


# MODELS WITH MORE THAN ONE KEYFIELD
class MultiKeyModel(popoto.Model):
    key1 = popoto.KeyField()
    key2 = popoto.KeyField()
    key3 = popoto.KeyField()
    value = popoto.Field(type=float)

random_fact = MultiKeyModel.create(key1="calories", key2="stamp", key3="lick", value=0.1)
# filter query
assert random_fact in MultiKeyModel.query.filter(key1="calories")
assert random_fact in MultiKeyModel.query.filter(key2="stamp")
assert random_fact in MultiKeyModel.query.filter(key3="lick")
assert MultiKeyModel.query.count() == 1
assert MultiKeyModel.query.filter(key1="calories", key2="pickle", key3="lick") == list()
# get query
assert random_fact == MultiKeyModel.query.get(key1="calories", key2="stamp", key3="lick")
assert MultiKeyModel.query.get(key1="calories", key2="pickle", key3="lick") is None
assert MultiKeyModel.query.count(key1="calories", key2="pickle", key3="lick") == 0
assert MultiKeyModel.query.count(key2="stamp") == 1
assert MultiKeyModel.query.count() == 1

for item in MultiKeyModel.query.all():
    item.delete()

assert MultiKeyModel.query.count() == 0
