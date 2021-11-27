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


class TwoKeyModel(popoto.Model):
    band = popoto.KeyField()
    role = popoto.KeyField()
    name = popoto.Field()


lisa = TwoKeyModel.create(band="BLACKPINK", role="rapper", name="Lalisa Manobal")
jennie = TwoKeyModel.create(band="BLACKPINK", role="vocals", name="Jennie Kim")
jisoo = TwoKeyModel.create(band="BLACKPINK", role="singer", name="Kim Ji-soo")
solar = TwoKeyModel.create(band="Mamamoo", role="singer", name="Kim Yong-sun")
moonbyul = TwoKeyModel.create(band="Mamamoo", role="rapper", name="Moon Byul-yi")

assert len(TwoKeyModel.query.filter(band__startswith="BLACK")) == 3
assert len(TwoKeyModel.query.filter(band__endswith="PINK")) == 3
assert len(TwoKeyModel.query.filter(band__in=["BLACKPINK", "Mamamoo"])) == 5
assert len(TwoKeyModel.query.filter(band="BLACKPINK")) == 3
assert len(TwoKeyModel.query.filter(band="blackpink")) == 0
assert len(TwoKeyModel.query.filter(role="singer")) == 2
assert moonbyul == TwoKeyModel.query.get(band="Mamamoo", role="rapper")


for item in TwoKeyModel.query.all():
    item.delete()


class AutoKeyModel(popoto.Model):
    value = popoto.Field(default="empty")

twice_member_names = AutoKeyModel.create(value="Nayeon, Jeongyeon, Momo, Sana, Jihyo, Mina, Dahyun, Chaeyoung, Tzuyu")
assert twice_member_names in AutoKeyModel.query.all()
assert hasattr(twice_member_names, '_auto_key')
assert '_auto_key' in twice_member_names._meta.fields
assert twice_member_names._auto_key in twice_member_names.db_key

for item in AutoKeyModel.query.all():
    item.delete()
assert len(AutoKeyModel.query.all()) == 0
