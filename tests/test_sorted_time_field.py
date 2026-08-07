"""
`SortedField(type=datetime.time)` must work at all.

`datetime.time` is on the allowed-type list in `SortedFieldMixin`
(`sorted_field_mixin.py`), but `convert_to_numeric` scored it with
`field_value.timestamp()` -- a method `datetime.time` does not have. Every
save raised `AttributeError`, so the advertised type never worked and had no
coverage.

The score is now seconds since midnight, which preserves time-of-day ordering.
"""

import datetime
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.fields.sorted_field_mixin import SortedFieldMixin
from src.popoto.redis_db import POPOTO_REDIS_DB

TZ_PLUS_7 = datetime.timezone(datetime.timedelta(hours=7))


class Shift(popoto.Model):
    shift_id = popoto.KeyField(type=str)
    starts_at = popoto.SortedField(type=datetime.time)


class _TimeField:
    type = datetime.time


@pytest.fixture(autouse=True)
def clean():
    _flush()
    yield
    _flush()


def _flush():
    for key in POPOTO_REDIS_DB.keys("*Shift*"):
        POPOTO_REDIS_DB.delete(key)


def _score(value):
    return SortedFieldMixin.convert_to_numeric(_TimeField, value)


def test_scoring_a_time_does_not_raise():
    """The regression: this raised AttributeError on every value."""
    assert _score(datetime.time(0, 0)) == 0


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime.time(0, 0), 0),
        (datetime.time(0, 0, 1), 1),
        (datetime.time(0, 1), 60),
        (datetime.time(1, 0), 3600),
        (datetime.time(12, 30, 15), 45015),
        (datetime.time(23, 59, 59), 86399),
        (datetime.time(23, 59, 59, 999999), 86399.999999),
    ],
)
def test_score_is_seconds_since_midnight(value, expected):
    assert _score(value) == pytest.approx(expected)


def test_score_preserves_time_of_day_ordering():
    times = [
        datetime.time(23, 59),
        datetime.time(0, 1),
        datetime.time(12, 0),
        datetime.time(6, 30),
    ]
    assert sorted(times, key=_score) == sorted(times)


def test_tzinfo_does_not_affect_the_score():
    """Documented limitation, pinned so a change has to be deliberate.

    Folding the offset in would wrap past midnight and put 01:00+07:00 before
    23:00+00:00 on the same clock face. Use `datetime.datetime` when the offset
    must participate in ordering.
    """
    assert _score(datetime.time(12, 0, tzinfo=TZ_PLUS_7)) == _score(
        datetime.time(12, 0)
    )


def test_saving_and_querying_a_time_sorted_field():
    """End to end: the save path is what used to raise."""
    Shift.create(shift_id="evening", starts_at=datetime.time(18, 0))
    Shift.create(shift_id="morning", starts_at=datetime.time(6, 0))
    Shift.create(shift_id="midday", starts_at=datetime.time(12, 0))

    results = Shift.query.filter(
        starts_at__gte=datetime.time(0, 0),
        starts_at__lte=datetime.time(23, 59, 59),
        order_by="starts_at",
    )

    assert [s.shift_id for s in results] == ["morning", "midday", "evening"]


def test_time_value_round_trips_through_the_sorted_field():
    Shift.create(shift_id="s1", starts_at=datetime.time(9, 15, 30))

    loaded = Shift.query.get(shift_id="s1")

    assert loaded.starts_at == datetime.time(9, 15, 30)
