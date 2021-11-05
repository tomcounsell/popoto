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

random_fact_4 = MultiKeyModel.create(key1="calories", key2="stamp", key3="lick", value=0.1)
assert random_fact_4 in MultiKeyModel.query.filter(key1="calories")
assert random_fact_4 in MultiKeyModel.query.filter(key2="stamp")
assert random_fact_4 in MultiKeyModel.query.filter(key3="lick")
