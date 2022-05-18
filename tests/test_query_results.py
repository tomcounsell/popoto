import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


# MODELS WITH MORE THAN ONE KEYFIELD
class ThingModel(popoto.Model):
    int_key = popoto.KeyField(type=int)
    str_key = popoto.KeyField(type=str)
    float_value = popoto.Field(type=float)
    str_value = popoto.Field(type=str)


thing_1 = ThingModel.create(int_key=1, str_key="1", float_value=1.1, str_value="1.1")
thing_2 = ThingModel.create(int_key=2, str_key="2", float_value=2.2, str_value="2.2")
no_thing = ThingModel.create(int_key=0, str_key="0")
r_thing = ThingModel.create(int_key=5, str_key="1", float_value=1.123, str_value="1.123")

assert len(ThingModel.query.all()) == 4
# test order_by
assert ThingModel.query.all(order_by="float_value")[0] == no_thing
assert ThingModel.query.all(order_by="str_key")[-1] == thing_2
assert ThingModel.query.all(order_by="float_value", limit=3)[-1] == r_thing
assert ThingModel.query.filter(str_key__startswith="1", order_by="int_key")[0] == thing_1
# test limit
assert len(ThingModel.query.all(limit=2)) == 2
assert len(ThingModel.query.filter(str_key=1, limit=1)) == 1
# test order_by with limit
assert ThingModel.query.filter(str_key=1, order_by="int_key", limit=1)[0] == thing_1
assert ThingModel.query.filter(str_key=1, order_by="-float_value", limit=1)[0] == r_thing

for item in ThingModel.query.all():
    item.delete()

assert ThingModel.query.count() == 0
