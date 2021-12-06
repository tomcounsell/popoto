import sys
import os

from src.popoto.exceptions import ModelException

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

try:
    for data_type in [list, dict, ]:
        class IllegalKeyModel(popoto.Model):
            band = popoto.KeyField(type=data_type)
except Exception as e:
    assert isinstance(e, ModelException)
else:
    raise Exception("expected ModelException on KeyField(type=list)")


# Test KeyFields create, update, and destroy Redis Sets

class TestKeySetModel(popoto.Model):
    uuid = popoto.AutoKeyField()
    band = popoto.KeyField(unique=False, null=True)
    role = popoto.KeyField(unique=False, null=True)
    name = popoto.Field()

lisa = TestKeySetModel.create(band="BLACKPINK", role="rapper", name="Lalisa Manobal")
jisoo = TestKeySetModel.create(band="BLACKPINK", role="singer", name="Kim Ji-soo")
solar = TestKeySetModel.create(band="Mamamoo", role="singer", name="Kim Yong-sun")
moonbyul = TestKeySetModel.create(band="Mamamoo", role="rapper", name="Moon Byul-yi")

bp_band_redis_set_key = f"{TestKeySetModel._meta.fields['band'].get_special_use_field_db_key(TestKeySetModel, 'band')}:BLACKPINK"
assert len(POPOTO_REDIS_DB.smembers(bp_band_redis_set_key)) == 2
assert POPOTO_REDIS_DB.smembers(bp_band_redis_set_key) == {lisa.db_key.encode(), jisoo.db_key.encode()}

singer_role_redis_set_key = f"{TestKeySetModel._meta.fields['role'].get_special_use_field_db_key(TestKeySetModel, 'role')}:singer"
assert len(POPOTO_REDIS_DB.smembers(singer_role_redis_set_key)) == 2

assert len(TestKeySetModel.query.filter(band="Mamamoo")) == 2
assert TestKeySetModel.query.filter(band="Mamamoo", role="singer")[0] == solar
assert TestKeySetModel.query.filter(band="BLACKPINK", role="rapper")[0] == lisa

lisa.delete()
assert len(POPOTO_REDIS_DB.smembers(bp_band_redis_set_key)) == 1
jisoo.delete()
assert len(POPOTO_REDIS_DB.smembers(bp_band_redis_set_key)) == 0
assert len(TestKeySetModel.query.filter(band="BLACKPINK")) == 0

for item in TestKeySetModel.query.all():
    item.delete()
assert len(TestKeySetModel.query.all()) == 0
