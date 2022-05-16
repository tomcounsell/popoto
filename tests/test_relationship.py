import sys
import os
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src.popoto import Model, KeyField, Field, Relationship


class StarSign(Model):
    name = KeyField()


virgo = StarSign.create(name="Virgo")
aquarius = StarSign.create(name="Aquarius")
leo = StarSign.create(name="Leo")


class Person(Model):
    name = KeyField()
    star_sign = Relationship(model=StarSign)


bk = Person.create(name="Beyoncé Knowles", star_sign=virgo)
kr = Person.create(name="Kelly Rowland", star_sign=aquarius)
mw = Person.create(name="Michelle Williams", star_sign=leo)

assert bk.star_sign.name == "Virgo"
queen_bey = Person.query.get(name="Beyoncé Knowles")

class Group(Model):
    name = KeyField()


dc = Group.create(name="Destiny's Child")


class Membership(Model):
    person = Relationship(model=Person)
    group = Relationship(model=Group)
    joined_at = Field(type=date, null=True)


for person in [bk, kr, mw]:
    Membership.create(person=person, group=dc)


assert len(Membership.query.filter(group=dc)) == 3
# assert bk in Membership.query.filter(group__name="Destiny's Child")


pipeline = POPOTO_REDIS_DB.pipeline()
for model_class in [Membership, Group, Person, StarSign]:
    for item in model_class.query.all():
        pipeline = item.delete(pipeline)
    pipeline.execute()
