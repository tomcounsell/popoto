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


random_fact_1 = MultiKeyModel.create(key1="calories", key2="stamp", key3="lick", value=0.1)

# filter query
assert random_fact_1 in MultiKeyModel.query.filter(key1="calories")
assert random_fact_1 in MultiKeyModel.query.filter(key2="stamp")
assert random_fact_1 in MultiKeyModel.query.filter(key3="lick")
assert MultiKeyModel.query.count() == 1
assert MultiKeyModel.query.filter(key1="calories", key2="pickle", key3="lick") == list()

random_fact_2 = MultiKeyModel.create(key1="count", key2="pleats", key3="chef hat", value=100)
random_fact_3 = MultiKeyModel.create(key1="watts used", key2="thinking", key3="brain", value=10)
# get query
assert random_fact_1 == MultiKeyModel.query.get(key1="calories", key2="stamp", key3="lick")
assert MultiKeyModel.query.get(key1="calories", key2="pickle", key3="lick") is None
assert MultiKeyModel.query.count(key1="calories", key2="pickle", key3="lick") == 0
assert MultiKeyModel.query.count(key2="stamp") == 1
assert MultiKeyModel.query.count() == 3

assert len(MultiKeyModel.query.filter(key1__in=[m.key1 for m in MultiKeyModel.query.all()], limit=2)) == 2


for item in MultiKeyModel.query.all():
    item.delete()

assert MultiKeyModel.query.count() == 0
