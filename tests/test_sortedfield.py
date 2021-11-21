import datetime
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


class SortedDateModel(popoto.Model):
    name = popoto.KeyField()
    birthday = popoto.SortedField(type=datetime.date)


lisa = SortedDateModel.create(name="Lisa", birthday=datetime.date(1997, 3, 27))
rose = SortedDateModel.create(name="Rose", birthday=datetime.date(1997, 2, 11))
jisoo = SortedDateModel.create(name="Jisoo", birthday=datetime.date(1995, 1, 3))
jennie = SortedDateModel.create(name="Jennie", birthday=datetime.date(1996, 1, 16))

assert lisa in SortedDateModel.query.all()
oldest = SortedDateModel.query.filter(birthday__lt=datetime.date(1996, 1, 1))[0]
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
