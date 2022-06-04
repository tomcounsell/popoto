import random
from datetime import date, datetime
from decimal import Decimal
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB


class SortedDateModel(popoto.Model):
    name = popoto.KeyField()
    birthday = popoto.SortedField(type=date)


lisa = SortedDateModel.create(name="Lisa", birthday=date(1997, 3, 27))
rose = SortedDateModel.create(name="Rose", birthday=date(1997, 2, 11))
jisoo = SortedDateModel.create(name="Jisoo", birthday=date(1995, 1, 3))
jennie = SortedDateModel.create(name="Jennie", birthday=date(1996, 1, 16))

assert lisa in SortedDateModel.query.all()
oldest = SortedDateModel.query.filter(birthday__lt=date(1996, 1, 1))[0]
assert jisoo == oldest
younger_than_rose = SortedDateModel.query.filter(birthday__gt=rose.birthday)
assert len(younger_than_rose) == 1
assert lisa in younger_than_rose

for item in SortedDateModel.query.all():
    item.delete()


class SortedIntModel(popoto.Model):
    product = popoto.KeyField()
    count = popoto.SortedField(type=int)


beans = SortedIntModel.create(product="beans", count=15)
cans = SortedIntModel.create(product="cans", count=2)

assert beans.count > cans.count
more_than_cans = SortedIntModel.query.filter(count__gt=cans.count)
assert beans in more_than_cans

for item in SortedIntModel.query.all():
    item.delete()


class SortedFloatModel(popoto.Model):
    wrestler = popoto.KeyField()
    height = popoto.SortedField(type=float)


john = SortedFloatModel.create(wrestler="John Cena", height=1.85)
rock = SortedFloatModel.create(wrestler="Dwayne Johnson", height=1.96)

assert john in SortedFloatModel.query.filter(height__gte=john.height)
assert john not in SortedFloatModel.query.filter(height__gt=john.height)

for item in SortedFloatModel.query.all():
    item.delete()


class Racer(popoto.Model):
    name = popoto.KeyField()
    fastest_lap = popoto.SortedField(type=float)


tim = Racer.create(name="Tim", fastest_lap=54.92)
bob = Racer.create(name="Bob", fastest_lap=57.11)
joe = Racer.create(name="Joe", fastest_lap=51.90)
assert len(Racer.query.filter(fastest_lap__lt=55)) == 2
assert len(Racer.query.filter(fastest_lap__gte=joe.fastest_lap)) == 3

for item in Racer.query.all():
    item.delete()


class SortedAssetsModel(popoto.Model):
    uuid = popoto.AutoKeyField(auto_uuid_length=6)
    market = popoto.KeyField(unique=True)
    asset_id = popoto.KeyField(null=False)
    timestamp = popoto.SortedKeyField(type=datetime, sort_by=('asset_id', 'market'))
    market_cap = popoto.SortedField(type=Decimal, sort_by='market')
    price = popoto.DecimalField()

timestamps = [datetime(2022, 1, 1, hour) for hour in range(23)]
for timestamp in timestamps:
    for market in ["beefi", 'damnance']:
        for asset_id in ["SPOON", "BENT"]:
            SortedAssetsModel.create(
                market=market, asset_id=asset_id, timestamp=timestamp,
                market_cap=Decimal(str(random.randint(10e3, 10e5))),
                price=Decimal(str(random.randint(1, 100))+"."+str(random.randint(1, 100)))
            )

assert len(SortedAssetsModel.query.filter(market='beefi', order_by='market_cap')) == len(timestamps)*2
assert len(SortedAssetsModel.query.filter(market='damnance', asset_id='BENT')) == len(timestamps)
assert len(SortedAssetsModel.query.filter(
    timestamp__gte=datetime(2022, 1, 1, 12),
    timestamp__lt=datetime(2022, 1, 1, 20),
    asset_id='SPOON', market='damnance'
)) == 8*2

for sam in SortedAssetsModel.objects.all():
    sam.delete()
