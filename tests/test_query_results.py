import sys
import os

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto

# MODELS WITH MORE THAN ONE KEYFIELD
# Backend conformance (#759 M1b, plan §5 M1 gate (b)): every test in this
# module runs once per configured backend, and the `backend` fixture binds
# that leg's backend for the test, so the module-level models below run on
# Redis and on Postgres from the same test code.
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class ThingModel(popoto.Model):
    int_key = popoto.KeyField(type=int)
    str_key = popoto.KeyField(type=str)
    float_value = popoto.Field(type=float)
    str_value = popoto.Field(type=str)


@pytest.fixture
def things():
    thing_1 = ThingModel.create(
        int_key=1, str_key="1", float_value=1.1, str_value="1.1"
    )
    thing_2 = ThingModel.create(
        int_key=2, str_key="2", float_value=2.2, str_value="2.2"
    )
    no_thing = ThingModel.create(int_key=0, str_key="0")
    r_thing = ThingModel.create(
        int_key=5, str_key="1", float_value=1.123, str_value="1.123"
    )
    return thing_1, thing_2, no_thing, r_thing


def test_all_returns_every_record(things):
    assert len(ThingModel.query.all()) == 4


def test_order_by(things):
    thing_1, thing_2, no_thing, r_thing = things
    # test order_by
    assert ThingModel.query.all(order_by="float_value")[0] == no_thing
    assert ThingModel.query.all(order_by="str_key")[-1] == thing_2
    assert ThingModel.query.all(order_by="float_value", limit=3)[-1] == r_thing
    assert (
        ThingModel.query.filter(str_key__startswith="1", order_by="int_key")[0]
        == thing_1
    )


def test_limit(things):
    # test limit
    assert len(ThingModel.query.all(limit=2)) == 2
    assert len(ThingModel.query.filter(str_key=1, limit=1)) == 1


def test_order_by_with_limit(things):
    thing_1, thing_2, no_thing, r_thing = things
    # test order_by with limit
    assert (
        ThingModel.query.filter(str_key__in=["1", "2"], order_by="int_key", limit=1)[0]
        == thing_1
    )
    assert (
        ThingModel.query.filter(str_key__in=["1", "2"], order_by="-int_key", limit=1)[0]
        == r_thing
    )
    assert (
        ThingModel.query.filter(
            str_key__in=["1", "2"], order_by="float_value", limit=1
        )[0]
        == thing_1
    )
    assert (
        ThingModel.query.filter(
            str_key__in=["1", "2"], order_by="-float_value", limit=1
        )[0]
        == thing_2
    )


def test_values(things):
    # test values
    only_ints = ThingModel.query.all(values=("int_key",))
    assert all(
        [
            len(only_ints) == 4,
            {"int_key": 1} in only_ints,
            {"int_key": 2} in only_ints,
            {"int_key": 0} in only_ints,
            {"int_key": 5} in only_ints,
        ]
    )
    assert (
        ThingModel.query.all(values=("int_key", "float_value"), order_by="int_key")[0][
            "float_value"
        ]
        == None
    )
    assert (
        ThingModel.query.filter(str_key__startswith="2", values=("str_value",))[0][
            "str_value"
        ]
        == "2.2"
    )


def test_delete_all_empties_the_model(things):
    for item in ThingModel.query.all():
        item.delete()

    assert ThingModel.query.count() == 0
