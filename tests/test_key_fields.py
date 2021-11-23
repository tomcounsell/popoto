import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class UniqueKeyModel(popoto.Model):
    name = popoto.UniqueKeyField()
    description = popoto.Field(null=True)


lisa = UniqueKeyModel(name="Lalisa Manobal")
lisa.description = "Famous K-Pop Rapper for BlackPink"
lisa.save()

same_lisa = UniqueKeyModel.query.get(name="Lalisa Manobal")
assert lisa == same_lisa

for item in UniqueKeyModel.query.all():
    item.delete()


class MultiKeyModel(popoto.Model):
    band = popoto.KeyField()
    role = popoto.KeyField()
    name = popoto.Field()


lisa = MultiKeyModel.create(band="BLACKPINK", role="rapper", name="Lalisa Manobal")
jennie = MultiKeyModel.create(band="BLACKPINK", role="vocals", name="Jennie Kim")
jisoo = MultiKeyModel.create(band="BLACKPINK", role="singer", name="Kim Ji-soo")
solar = MultiKeyModel.create(band="Mamamoo", role="singer", name="Kim Yong-sun")
moonbyul = MultiKeyModel.create(band="Mamamoo", role="rapper", name="Moon Byul-yi")

assert len(MultiKeyModel.query.filter(band="BLACKPINK")) == 3
assert len(MultiKeyModel.query.filter(role="singer")) == 2
assert moonbyul == MultiKeyModel.query.get(band="Mamamoo", role="rapper")


class AutoKeyModel(popoto.Model):
    value = popoto.Field(default="empty")


twice_member_names = AutoKeyModel.create(value="Nayeon, Jeongyeon, Momo, Sana, Jihyo, Mina, Dahyun, Chaeyoung, Tzuyu")
assert twice_member_names in AutoKeyModel.query.all()
assert twice_member_names._meta.fields.get('_auto_key', False)

for item in AutoKeyModel.query.all():
    item.delete()
