"""
An aware datetime must survive the storage round trip with its offset intact.

Before #521 the encoder used ``%Y%m%dT%H:%M:%S.%f``, which has no offset
directive. An aware value went in and a naive one came out, and
``12:00+07:00`` and ``12:00+00:00`` produced byte-identical storage. #519
made the sorted-set *score* a pure function of the stored value; this module
covers the *value* itself.

Two invariants, and the second matters as much as the first:

1. awareness and offset round-trip for values written from #521 onward;
2. values already on disk in the offset-free form still decode, and decode as
   *UTC* (#537), matching what #519 has assumed about them for scoring and
   #421 for stamping.

Invariant 2 was the opposite of this until #537. Assuming UTC shifted
``str(value)``, and identity was derived from ``str(value)``, so the
assumption duplicated every legacy row on its next save. Identity now derives
from the instant instead (``canonical_key_str``), which is what unblocked it.

The tests pin a non-UTC process timezone for the same reason #519's do: on a
UTC box a dropped offset is indistinguishable from a preserved one.
"""

import datetime
import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.db_key import DB_key
from src.popoto.models.encoding import TYPE_ENCODER_DECODERS
from src.popoto.redis_db import POPOTO_REDIS_DB

OFFSET_ZONE = "Asia/Bangkok"  # +07:00, no DST
TZ_PLUS_7 = datetime.timezone(datetime.timedelta(hours=7))
TZ_MINUS_5 = datetime.timezone(datetime.timedelta(hours=-5))

DATETIME_CODEC = TYPE_ENCODER_DECODERS[datetime.datetime]
TIME_CODEC = TYPE_ENCODER_DECODERS[datetime.time]


class Appointment(popoto.Model):
    appointment_id = popoto.KeyField(type=str)
    starts_at = popoto.DatetimeField()


@pytest.fixture(autouse=True)
def shifted_local_timezone():
    """Pin local time to a non-UTC zone so a dropped offset is observable."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = OFFSET_ZONE
    time.tzset()
    _flush()
    yield
    _flush()
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _flush():
    for key in POPOTO_REDIS_DB.keys("*Appointment*"):
        POPOTO_REDIS_DB.delete(key)


# --- codec-level: the round trip itself ------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_PLUS_7),
        datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_MINUS_5),
        datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 8, 7, 12, 0, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 8, 7, 12, 0, 0, 123456),
        datetime.datetime(2026, 8, 7, 12, 0, 0),
    ],
    ids=["+07", "-05", "utc", "utc-no-usec", "naive", "naive-no-usec"],
)
def test_datetime_round_trips_exactly(value):
    """Equality here is not enough on its own -- see the utcoffset test below."""
    decoded = DATETIME_CODEC.decoder(DATETIME_CODEC.encoder(value))
    assert decoded == value
    assert decoded.utcoffset() == value.utcoffset()


def test_awareness_survives_rather_than_merely_comparing_equal():
    """An aware value must come back aware, not naive-but-equal-looking.

    `==` between two aware datetimes compares instants, so a decoder that
    normalized everything to UTC would pass the equality test above while
    still destroying the caller's offset. Assert the offset directly.
    """
    value = datetime.datetime(2026, 8, 7, 12, 0, tzinfo=TZ_PLUS_7)
    decoded = DATETIME_CODEC.decoder(DATETIME_CODEC.encoder(value))
    assert decoded.tzinfo is not None
    assert decoded.utcoffset() == datetime.timedelta(hours=7)
    assert decoded.hour == 12


def test_two_offsets_of_the_same_instant_no_longer_collide():
    """The headline defect: these stored byte-identically before #521.

    This is about *encoded values*, which must stay distinguishable so a
    reloaded value reports the offset its caller supplied. Their derived
    Redis *keys* now deliberately do collide, because identity is the instant
    (#537/#538) -- the two statements are about different projections and are
    both true. See `test_datetime_key_identity.py`.
    """
    bangkok = datetime.datetime(2026, 8, 7, 12, 0, tzinfo=TZ_PLUS_7)
    utc = datetime.datetime(2026, 8, 7, 12, 0, tzinfo=datetime.timezone.utc)

    encoded_bangkok = DATETIME_CODEC.encoder(bangkok)["as_encodable"]
    encoded_utc = DATETIME_CODEC.encoder(utc)["as_encodable"]

    assert encoded_bangkok != encoded_utc
    assert DATETIME_CODEC.decoder({"as_encodable": encoded_bangkok}) == bangkok
    assert DATETIME_CODEC.decoder({"as_encodable": encoded_utc}) == utc


def test_naive_stays_naive():
    """Naive in, naive out. The encoder must not assume a zone at write time."""
    value = datetime.datetime(2026, 8, 7, 12, 0)
    decoded = DATETIME_CODEC.decoder(DATETIME_CODEC.encoder(value))
    assert decoded.tzinfo is None
    assert decoded == value


# --- backward compatibility: values already on disk ------------------------


def test_legacy_offset_free_datetime_still_decodes():
    """Pre-#521 rows must keep loading; the decoder reads both shapes."""
    decoded = DATETIME_CODEC.decoder({"as_encodable": "20260807T12:00:00.123456"})
    assert decoded == datetime.datetime(
        2026, 8, 7, 12, 0, 0, 123456, tzinfo=datetime.timezone.utc
    )


def test_legacy_datetime_is_assumed_utc():
    """A legacy string has no offset stored, and is now read as UTC (#537).

    This test previously asserted the opposite, and the history is the point.
    #521 proposed assuming UTC; the assumption was written (commit `0342550`)
    and reverted, because `datetime` is a valid `KeyField` type and `DB_key`
    derived identity from `str(value)`. Stamping an offset shifted `str()`,
    which shifted the derived key away from the key the row was stored under,
    so loading and re-saving a legacy row wrote a second hash and orphaned the
    original. This test pinned the naive behaviour precisely so that nobody
    could reintroduce the assumption without first fixing that.

    It has been fixed: keys now derive from `canonical_key_str`, which renders
    the instant rather than the representation, so a legacy value and the same
    value stamped UTC produce identical key bytes. The companion assertion is
    `test_legacy_keyfield_row_keeps_its_stored_key` below.
    """
    decoded = DATETIME_CODEC.decoder({"as_encodable": "20260807T12:00:00.123456"})
    assert decoded.tzinfo is datetime.timezone.utc
    assert decoded.utcoffset() == datetime.timedelta(0)


def test_the_legacy_branch_is_selected_by_shape_not_by_a_parser_raising():
    """Branch selection must not depend on the interpreter minor version.

    `fromisoformat` parses `20260807T12:00:00.123456` on 3.11+ and raises on
    the 3.10 `requires-python` floor. While both branches produced the same
    naive value that was harmless, but now that only one branch stamps UTC, a
    try-order fallback would decode the same stored bytes aware on 3.10 and
    naive on 3.12. Only one Python runs per CI job, so this asserts on the
    branch predicate itself rather than on a version-dependent outcome.
    """
    from src.popoto.models.encoding import _LEGACY_DATETIME_RE

    assert _LEGACY_DATETIME_RE.match("20260807T12:00:00.123456")
    # A post-#521 deliberately naive value: date separators, never the legacy shape.
    assert not _LEGACY_DATETIME_RE.match("2026-08-07T12:00:00.123456")
    assert not _LEGACY_DATETIME_RE.match("2026-08-07T12:00:00.123456+07:00")


def test_a_post_521_naive_value_is_not_swept_into_the_legacy_assumption():
    """Deliberate naivety written after #521 must survive as naive."""
    value = datetime.datetime(2026, 8, 7, 12, 0, 0, 123456)
    decoded = DATETIME_CODEC.decoder(DATETIME_CODEC.encoder(value))
    assert decoded.tzinfo is None
    assert decoded == value


def test_a_malformed_stored_datetime_raises_rather_than_being_coerced():
    """Guessing at junk would turn a corrupt row into a plausible wrong answer."""
    with pytest.raises(ValueError):
        DATETIME_CODEC.decoder({"as_encodable": "not a datetime at all"})


def test_legacy_offset_free_time_still_decodes():
    decoded = TIME_CODEC.decoder({"as_encodable": "12:30:00.000000"})
    assert decoded == datetime.time(12, 30)
    assert decoded.tzinfo is None


# --- time ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        datetime.time(12, 30, 0, 5, tzinfo=TZ_PLUS_7),
        datetime.time(12, 30, tzinfo=datetime.timezone.utc),
        datetime.time(12, 30, 0, 5),
        datetime.time(12, 30),
    ],
    ids=["aware-usec", "aware-utc", "naive-usec", "naive"],
)
def test_time_round_trips_exactly(value):
    decoded = TIME_CODEC.decoder(TIME_CODEC.encoder(value))
    assert decoded == value
    assert decoded.utcoffset() == value.utcoffset()


# --- date: unchanged, asserted so a later edit has to be deliberate --------


def test_date_round_trips_and_keeps_its_legacy_format():
    """`date` carries no offset, so #521 leaves its encoding alone."""
    codec = TYPE_ENCODER_DECODERS[datetime.date]
    value = datetime.date(2026, 8, 7)
    assert codec.encoder(value)["as_encodable"] == "20260807"
    assert codec.decoder(codec.encoder(value)) == value


# --- KeyField: awareness must not leak into derived key bytes --------------
#
# `datetime` is a valid KeyField type and `DB_key` builds both the hash key and
# the `$KeyF:` index key from `str(value)`. So a decoder that changes a value's
# awareness changes its identity: the row's derived key stops matching the key
# it is stored under, and a re-save writes a second hash instead of updating
# the first. That is why legacy rows stay naive.


class Event(popoto.Model):
    at = popoto.KeyField(type=datetime.datetime)
    note = popoto.Field(type=str, null=True)


def _flush_events():
    for key in POPOTO_REDIS_DB.keys("*Event*"):
        POPOTO_REDIS_DB.delete(key)


@pytest.fixture(autouse=True)
def clean_events():
    _flush_events()
    yield
    _flush_events()


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime(2026, 8, 7, 12, 0, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 8, 7, 12, 0, tzinfo=TZ_PLUS_7),
        datetime.datetime(2026, 8, 7, 12, 0),
    ],
    ids=["aware-utc", "aware+07", "naive"],
)
def test_datetime_keyfield_does_not_duplicate_on_resave(value):
    """A reload must not shift the row's identity.

    Before #521 an aware key value reloaded naive, so re-saving wrote a second
    hash under a different key and orphaned the first.
    """
    Event.create(at=value, note="first")

    loaded = Event.query.get(at=value)
    loaded.note = "second"
    loaded.save()

    assert len(Event.query.all()) == 1
    assert Event.query.get(at=value).note == "second"


def test_legacy_keyfield_row_keeps_its_stored_key():
    """The regression that used to block assuming UTC for legacy rows.

    History, because it is what makes the current assertion meaningful. This
    test once pinned `str(legacy) == "2026-08-07 12:00:00.123456"`: a legacy
    value decoded naive, so `str()` was unchanged and the derived key still
    matched the key on disk. If it decoded aware, `str()` gained a "+00:00",
    the derived key moved, and the row duplicated on the next save (#537).

    Identity no longer comes from `str()`. `canonical_key_str` renders the
    instant, so the aware-UTC legacy value and the naive one it used to decode
    to produce the *same* key bytes -- which is precisely what made the
    assumption adoptable. The assertion is now on the key rather than on the
    value's repr, because the key is what was ever at risk.
    """
    legacy = DATETIME_CODEC.decoder({"as_encodable": "20260807T12:00:00.123456"})
    assert legacy.tzinfo is datetime.timezone.utc

    naive_equivalent = legacy.replace(tzinfo=None)
    assert (
        DB_key("Event", legacy).redis_key == DB_key("Event", naive_equivalent).redis_key
    )

    Event.create(at=legacy, note="first")
    loaded = Event.query.get(at=legacy)
    loaded.note = "second"
    loaded.save()

    assert len(Event.query.all()) == 1
    assert Event.query.get(at=legacy).note == "second"


# --- end to end through Redis ----------------------------------------------


def test_aware_value_survives_a_real_save_and_load():
    value = datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_PLUS_7)
    Appointment.create(appointment_id="a1", starts_at=value)

    loaded = Appointment.query.get(appointment_id="a1")

    assert loaded.starts_at == value
    assert loaded.starts_at.utcoffset() == datetime.timedelta(hours=7)


def test_resaving_a_reloaded_aware_value_does_not_drift():
    """The #519 failure mode, now at the value layer instead of the score.

    A reload used to return a naive value, so writing it straight back stored
    a different instant than the one that was read.
    """
    value = datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_PLUS_7)
    Appointment.create(appointment_id="a2", starts_at=value)

    first = Appointment.query.get(appointment_id="a2")
    first.save()
    second = Appointment.query.get(appointment_id="a2")

    assert second.starts_at == first.starts_at == value
    assert second.starts_at.utcoffset() == datetime.timedelta(hours=7)
