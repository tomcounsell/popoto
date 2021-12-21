import sys
import os
from decimal import Decimal
from datetime import date, datetime, time, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class EveryTypeModel(popoto.Model):
    int_val = popoto.IntField(null=False)
    float_val = popoto.FloatField(null=False)
    decimal_val = popoto.DecimalField(null=False)
    string_val = popoto.StringField(null=False)
    boolean_val = popoto.BooleanField(null=False)
    list_val = popoto.ListField(null=False)
    dict_val = popoto.DictField(null=False)
    bytes_val = popoto.BytesField(null=False)
    date_val = popoto.DateField(null=False)
    datetime_val = popoto.DatetimeField(null=False)
    time_val = popoto.TimeField(null=False)


one = EveryTypeModel(
    int_val=1,
    float_val=1.0,
    decimal_val=Decimal(1.00),
    string_val="one",
    boolean_val=True,
    list_val=[1, ],
    dict_val={"one": 1},
    bytes_val=b'1',
    date_val=date(2020, 1, 1),
    datetime_val=datetime(2020, 1, 1, 13, 11),
    time_val=time(1, 11, 1, 111)
)
one.save()

for item in EveryTypeModel.query.all():
    item.delete()


class EveryNullableTypeModel(popoto.Model):
    int_val = popoto.Field(type=int)
    float_val = popoto.Field(type=float)
    decimal_val = popoto.Field(type=Decimal)
    string_val = popoto.Field(type=str)
    boolean_val = popoto.Field(type=bool)
    list_val = popoto.Field(type=list)
    dict_val = popoto.Field(type=dict)
    bytes_val = popoto.Field(type=bytes)
    date_val = popoto.Field(type=date)
    datetime_val = popoto.Field(type=datetime)
    time_val = popoto.Field(type=time)


two = EveryNullableTypeModel.create()

for item in EveryNullableTypeModel.query.all():
    item.delete()
