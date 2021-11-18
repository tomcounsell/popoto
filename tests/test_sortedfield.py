import sys
import os
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
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
assert jisoo is oldest
younger_than_rose = SortedDateModel.query.filter(birthday__gt=rose.birthday)
assert len(younger_than_rose) == 1
assert lisa in younger_than_rose


class SortedIntModel(popoto.Model):
    product = popoto.KeyField()
    count = popoto.SortedField()


class SortedFloatModel(popoto.Model):
    wrestler = popoto.KeyField()
    weight = popoto.SortedField()

