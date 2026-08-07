"""A datetime KeyField's Redis identity is the instant, not the representation.

``DB_key`` builds both the primary hash key and the ``$KeyF:`` index key from a
KeyField value's rendered form. Rendering with ``str()`` made a ``datetime``
row's identity depend on whether the value happened to decode aware or naive,
which is the shared root of two defects:

- **#538**: before 1.8.2 an aware value reloaded naive, so a re-save derived a
  *different* key, wrote a second hash and orphaned the first.
- **#537**: 1.8.2 wanted to assume UTC for legacy offset-free rows, and could
  not, because stamping an offset on read shifts ``str()`` and therefore shifts
  the key -- reproducing #538 on every legacy row.

``canonical_key_str`` renders a datetime as its UTC instant, fixed width, with
a trailing ``Z``. Three representations of one instant now address one row, and
a decode change can no longer move a key byte.

The tests pin a non-UTC process timezone for the reason #519's and #521's do:
on a UTC box a dropped offset is indistinguishable from a preserved one.
"""

import datetime
import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.fields.constants import Defaults
from src.popoto.models.canonical_key import canonical_key_str
from src.popoto.models.db_key import DB_key
from src.popoto.redis_db import POPOTO_REDIS_DB

OFFSET_ZONE = "Asia/Bangkok"  # +07:00, no DST
TZ_PLUS_7 = datetime.timezone(datetime.timedelta(hours=7))
TZ_MINUS_5 = datetime.timezone(datetime.timedelta(hours=-5))
UTC = datetime.timezone.utc

# One instant, three representations.
INSTANT_PLUS_7 = datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_PLUS_7)
INSTANT_UTC = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456, tzinfo=UTC)
INSTANT_NAIVE = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456)
CANONICAL = "2026-08-07T05:00:00.123456Z"


class Meeting(popoto.Model):
    at = popoto.KeyField(type=datetime.datetime)
    note = popoto.Field(type=str, null=True)


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
    for key in POPOTO_REDIS_DB.keys("*Meeting*"):
        POPOTO_REDIS_DB.delete(key)


# --- canonical_key_str in isolation ----------------------------------------


@pytest.mark.parametrize(
    "value",
    [INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE],
    ids=["aware+07", "aware-utc", "naive"],
)
def test_one_instant_renders_one_canonical_string(value):
    """The headline property. Awareness must not reach the key bytes."""
    assert canonical_key_str(value) == CANONICAL


def test_canonical_form_is_fixed_width_even_at_zero_microseconds():
    """``isoformat()`` drops ``.%f`` at zero microseconds; ``strftime`` does not.

    Fixed width is what keeps lexicographic key order equal to chronological
    order, which ``scan_keys`` patterns and human ``redis-cli --scan`` reading
    both rely on.
    """
    at_zero = datetime.datetime(2026, 8, 7, 5, 0, 0, 0, tzinfo=UTC)
    assert canonical_key_str(at_zero) == "2026-08-07T05:00:00.000000Z"
    assert len(canonical_key_str(at_zero)) == len(CANONICAL)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "a string",
        42,
        3.5,
        True,
        False,
        __import__("decimal").Decimal("1.250"),
        datetime.date(2026, 8, 7),
        datetime.time(12, 30),
        datetime.time(12, 30, tzinfo=TZ_PLUS_7),
    ],
    ids=[
        "none",
        "str",
        "int",
        "float",
        "true",
        "false",
        "decimal",
        "date",
        "time-naive",
        "time-aware",
    ],
)
def test_every_non_datetime_value_renders_byte_identically_to_str(value):
    """The anti-criterion: this change must move no other type's key bytes.

    ``date`` has no offset to lose. ``time`` is excluded deliberately -- a
    naive ``time.isoformat()`` with microseconds is byte-identical to the
    legacy ``%H:%M:%S.%f`` form, so a legacy time cannot be distinguished from
    a deliberately naive one, and an aware time's ``str()`` already carries its
    offset stably (PR #532's reasoning). Neither has the bug.
    """
    assert canonical_key_str(value) == str(value)


def test_none_renders_the_placeholder_db_key_depends_on():
    """``Model.db_key`` and ``_has_unstable_db_key`` both key off "None"."""
    assert canonical_key_str(None) == "None"


def test_fold_does_not_change_the_rendering():
    """``fold`` disambiguates a repeated local wall clock, not a UTC instant."""
    a = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456, tzinfo=UTC, fold=0)
    b = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456, tzinfo=UTC, fold=1)
    assert canonical_key_str(a) == canonical_key_str(b) == CANONICAL


# --- the kill switch --------------------------------------------------------


@pytest.fixture
def legacy_key_bytes():
    """Force 1.8.2 key bytes for the duration of a test."""
    previous = Defaults.DATETIME_KEY_LEGACY
    Defaults.DATETIME_KEY_LEGACY = True
    yield
    Defaults.DATETIME_KEY_LEGACY = previous


@pytest.mark.parametrize(
    "value",
    [INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE],
    ids=["aware+07", "aware-utc", "naive"],
)
def test_kill_switch_reproduces_1_8_2_key_bytes(legacy_key_bytes, value):
    """``POPOTO_DATETIME_KEY_LEGACY`` must be an exact rollback, not an approximation.

    It is what lets an adopter roll readers forward to >= 1.9.0 before any key
    byte moves.
    """
    assert canonical_key_str(value) == str(value)


def test_kill_switch_restores_the_pre_1_9_0_duplication_semantics(legacy_key_bytes):
    """Sanity: with the switch on, the three representations are three keys again.

    This is the state the switch exists to reproduce, and asserting it keeps
    the switch honest -- a no-op switch would pass the byte test above by
    accident if canonicalization were removed.
    """
    keys = {
        DB_key("Meeting", v).redis_key
        for v in (INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE)
    }
    assert len(keys) == 3


# --- identity through DB_key and through Redis ------------------------------


def test_three_representations_build_one_redis_key():
    keys = {
        DB_key("Meeting", v).redis_key
        for v in (INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE)
    }
    assert len(keys) == 1


@pytest.mark.parametrize(
    "lookup",
    [INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE],
    ids=["aware+07", "aware-utc", "naive"],
)
def test_a_row_written_with_one_representation_is_found_by_all_three(lookup):
    Meeting.create(at=INSTANT_PLUS_7, note="written as +07")

    found = Meeting.query.get(at=lookup)

    assert found is not None
    assert found.note == "written as +07"


def test_writing_all_three_representations_yields_one_row():
    """Deliberate identity merge (Risk 2), asserted so it can never regress silently.

    #519 has scored these three identically since 1.7.x, so the system has
    treated them as one instant for longer than it has keyed them as one.
    """
    Meeting.create(at=INSTANT_PLUS_7, note="first")
    Meeting.create(at=INSTANT_UTC, note="second")
    Meeting.create(at=INSTANT_NAIVE, note="third")

    assert len(Meeting.query.all()) == 1
    assert Meeting.query.get(at=INSTANT_UTC).note == "third"


@pytest.mark.parametrize(
    "value",
    [INSTANT_PLUS_7, INSTANT_UTC, INSTANT_NAIVE],
    ids=["aware+07", "aware-utc", "naive"],
)
def test_reload_and_resave_does_not_duplicate(value):
    """#538's mechanism, from the other side of the fix."""
    Meeting.create(at=value, note="first")

    loaded = Meeting.query.get(at=value)
    loaded.note = "second"
    loaded.save()

    assert len(Meeting.query.all()) == 1
    assert Meeting.query.get(at=value).note == "second"


def test_the_stored_value_still_keeps_the_callers_offset():
    """Key and value are deliberately different projections.

    The key is the instant; the hash keeps the exact offset the caller supplied
    (#521). Canonicalizing identity must not touch value fidelity -- if it did,
    this change would be a regression of the fix it builds on.
    """
    Meeting.create(at=INSTANT_PLUS_7, note="offset check")

    loaded = Meeting.query.get(at=INSTANT_UTC)  # look up by a different representation

    assert loaded.at.utcoffset() == datetime.timedelta(hours=7)
    assert loaded.at.hour == 12
    assert loaded.at == INSTANT_UTC  # same instant


def test_the_stored_key_is_the_canonical_form():
    """Pin the actual bytes. A format drift is a silent migration.

    ``DB_key.clean()`` escapes the colons (#525), so the on-disk key carries
    the escape sequence rather than literal colons.
    """
    Meeting.create(at=INSTANT_PLUS_7, note="byte check")

    stored = [k.decode("utf-8") for k in POPOTO_REDIS_DB.keys("Meeting:*")]

    assert len(stored) == 1
    assert stored[0] == "Meeting:" + DB_key.clean(CANONICAL)


def test_the_keyfield_index_agrees_with_the_hash():
    """spike-3's failure mode: canonicalizing one insertion point and not the other.

    ``Model.db_key`` used to pre-``str()`` its partials, so canonicalizing
    inside ``DB_key`` alone moved the ``$KeyF:`` index key while leaving the
    primary hash key raw. Every ``$KeyF:`` member must name a hash that exists.
    """
    Meeting.create(at=INSTANT_PLUS_7, note="index check")

    index_keys = POPOTO_REDIS_DB.keys("$KeyF:Meeting:at:*")
    assert len(index_keys) == 1

    members = POPOTO_REDIS_DB.smembers(index_keys[0])
    assert len(members) == 1
    for member in members:
        assert POPOTO_REDIS_DB.exists(
            member
        ), f"index points at missing hash {member!r}"


# --- compound indexes carry the same fragility ------------------------------


class Slot(popoto.Model):
    slot_id = popoto.KeyField(type=str)
    at = popoto.DatetimeField()

    class Meta:
        indexes = [(("at",), False)]


def test_compound_index_hash_is_awareness_invariant():
    """``Meta.indexes`` hashed ``str(value)`` with the identical fragility.

    Cleanup uses ``compute_index_hash_from_values`` and lookup uses
    ``compute_index_hash``; if the two rendered a datetime differently the old
    index entry would never be deleted.
    """
    from_instance = Slot._meta.compute_index_hash(
        Slot(slot_id="s1", at=INSTANT_PLUS_7), ("at",)
    )
    from_values = Slot._meta.compute_index_hash_from_values(
        ("at",), {"at": INSTANT_UTC}
    )

    assert from_instance == from_values
