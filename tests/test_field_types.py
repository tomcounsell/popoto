import sys
import os
from decimal import Decimal
from datetime import date, datetime, time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


import pytest

# Backend conformance (#759 M1b, plan §5 M1 gate (b)): every test in this
# module runs once per configured backend, and the `backend` fixture binds
# that leg's backend for the test, so the module-level models below run on
# Redis and on Postgres from the same test code.
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class EveryTypeModel(popoto.Model):
    int_val = popoto.IntField(null=False)
    float_val = popoto.FloatField(null=False)
    decimal_val = popoto.DecimalField(null=False)
    string_val = popoto.StringField(null=False)
    boolean_val = popoto.BooleanField(null=False)
    bytes_val = popoto.BytesField(null=False)
    list_val = popoto.ListField(null=False)
    dict_val = popoto.DictField(null=False)
    set_val = popoto.SetField(null=False)
    tuple_val = popoto.TupleField(null=False)
    date_val = popoto.DateField(null=False)
    datetime_val = popoto.DatetimeField(null=False)
    time_val = popoto.TimeField(null=False)


class EveryNullableTypeModel(popoto.Model):
    int_val = popoto.Field(type=int)
    float_val = popoto.Field(type=float)
    decimal_val = popoto.Field(type=Decimal)
    string_val = popoto.Field(type=str)
    boolean_val = popoto.Field(type=bool)
    bytes_val = popoto.Field(type=bytes)
    list_val = popoto.Field(type=list)
    dict_val = popoto.Field(type=dict)
    set_val = popoto.Field(type=set)
    tuple_val = popoto.Field(type=tuple)
    date_val = popoto.Field(type=date)
    datetime_val = popoto.Field(type=datetime)
    time_val = popoto.Field(type=time)


@pytest.mark.redis_only(
    reason="EveryTypeModel includes Bytes/List/Dict/Set/Tuple/Date/Time fields, which arrive on Postgres with M1.1's plain-field breadth"
)
def test_every_non_null_field_type_round_trips_through_save_and_load():
    one = EveryTypeModel(
        int_val=1,
        float_val=1.0,
        decimal_val=Decimal(1.00),
        string_val="one",
        boolean_val=True,
        bytes_val=b"1",
        list_val=[
            1,
        ],
        dict_val={"one": 1},
        set_val={
            1,
        },
        tuple_val=(1,),
        date_val=date(2020, 1, 1),
        datetime_val=datetime(2020, 1, 1, 13, 11),
        time_val=time(1, 11, 1, 111),
    )
    one.save()

    same_one = EveryTypeModel.query.all()[0]
    for field_name in one._meta.fields.keys():
        assert getattr(one, field_name) == getattr(same_one, field_name)

    for item in EveryTypeModel.query.all():
        item.delete()


@pytest.mark.redis_only(
    reason="EveryNullableTypeModel includes bytes/list/dict/set/tuple/date/time fields, which arrive on Postgres with M1.1's plain-field breadth"
)
def test_every_nullable_field_type_creates_with_all_values_unset():
    # No assert in the original: the check is that create() with every
    # field left null does not raise, and that the rows delete cleanly.
    two = EveryNullableTypeModel.create()

    for item in EveryNullableTypeModel.query.all():
        item.delete()
