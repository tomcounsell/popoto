"""
A SortedField(type=datetime) score must be a pure function of the stored value.

The hash encoding carries no offset (#521), so the same instant is aware when
it arrives fresh and naive when it comes back from a reload. ``.timestamp()``
reads a naive datetime as *local* time, so before #519 a re-save re-scored the
row by the machine's UTC offset and the index disagreed with the data it
indexed.

Every test here pins the process timezone to a non-UTC zone. Without that these
assertions pass trivially on a UTC CI box while the bug is live for anyone in a
shifted zone, which is exactly how this survived unnoticed.
"""

import datetime
import os
import sys
import time

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB

OFFSET_ZONE = "Asia/Bangkok"  # +07:00, no DST
OFFSET_SECONDS = 7 * 3600


class ScoreDoc(popoto.Model):
    room_id = popoto.KeyField(type=str)
    doc_id = popoto.KeyField(type=str)
    ts = popoto.SortedField(type=datetime.datetime, partition_by="room_id")


@pytest.fixture(autouse=True)
def shifted_local_timezone():
    """Pin local time to a non-UTC zone for the duration of each test."""
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
    for key in POPOTO_REDIS_DB.keys("*ScoreDoc*"):
        POPOTO_REDIS_DB.delete(key)


def _score(doc_id="a"):
    """Read the raw sorted-set score for one member."""
    for key in POPOTO_REDIS_DB.keys("*ScoreDoc*"):
        if POPOTO_REDIS_DB.type(key) != b"zset":
            continue
        for member, score in POPOTO_REDIS_DB.zrange(key, 0, -1, withscores=True):
            if doc_id.encode() in member:
                return score
    return None


def test_local_timezone_is_actually_shifted():
    """Guard the guard: these tests are meaningless if TZ did not take."""
    assert time.timezone != 0 or time.altzone != 0, (
        "process timezone is still UTC; the fixture failed and every "
        "assertion below would pass vacuously"
    )


def test_score_survives_a_reload_and_resave():
    """The invariant. Same row, same instant, same score.

    Nothing here mentions a timezone, which is the point: this is a statement
    about the score being a function of the value, not about UTC.
    """
    aware = datetime.datetime.now(datetime.timezone.utc)
    ScoreDoc(room_id="r", doc_id="a", ts=aware).save()
    first = _score()

    reloaded = ScoreDoc.query.get(room_id="r", doc_id="a")
    reloaded.save()
    second = _score()

    assert first == second, (
        f"score moved by {second - first}s across a reload and re-save; "
        f"the index no longer agrees with the value it indexes"
    )


def test_score_matches_the_true_epoch():
    aware = datetime.datetime.now(datetime.timezone.utc)
    ScoreDoc(room_id="r", doc_id="a", ts=aware).save()

    assert _score() == pytest.approx(aware.timestamp(), abs=1e-6)


def test_resave_does_not_drift_by_the_local_offset():
    """The specific failure shape, asserted against the offset directly."""
    aware = datetime.datetime.now(datetime.timezone.utc)
    ScoreDoc(room_id="r", doc_id="a", ts=aware).save()
    before = _score()

    ScoreDoc.query.get(room_id="r", doc_id="a").save()
    drift = _score() - before

    assert (
        abs(drift) < 1.0
    ), f"score drifted {drift}s on re-save, local offset is {OFFSET_SECONDS}s"


def test_naive_and_aware_forms_of_one_instant_score_identically():
    """Two spellings of the same wall clock must not produce two scores."""
    aware = datetime.datetime(2026, 8, 7, 6, 25, 57, tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2026, 8, 7, 6, 25, 57)

    ScoreDoc(room_id="r", doc_id="aware", ts=aware).save()
    ScoreDoc(room_id="r", doc_id="naive", ts=naive).save()

    assert _score("aware") == _score("naive")


def test_ordering_survives_a_resave_of_the_middle_row():
    """The consequence a bounded read would hit.

    Re-saving one row must not reorder it against untouched neighbours. This
    is the shape a lifecycle method produces when it mutates some other field
    and saves a reloaded instance.
    """
    base = datetime.datetime.now(datetime.timezone.utc)
    for i, name in enumerate(["oldest", "middle", "newest"]):
        ScoreDoc(
            room_id="r",
            doc_id=name,
            ts=base + datetime.timedelta(minutes=i),
        ).save()

    ScoreDoc.query.get(room_id="r", doc_id="middle").save()

    ordered = [
        m.decode()
        for key in POPOTO_REDIS_DB.keys("*ScoreDoc*")
        if POPOTO_REDIS_DB.type(key) == b"zset"
        for m in POPOTO_REDIS_DB.zrange(key, 0, -1)
    ]
    positions = {
        name: next(i for i, m in enumerate(ordered) if name in m)
        for name in ["oldest", "middle", "newest"]
    }
    assert (
        positions["oldest"] < positions["middle"] < positions["newest"]
    ), f"re-saving one row reordered the partition: {ordered}"


def test_rebuild_indexes_repairs_scores_written_before_the_fix():
    """The documented remediation for an already-skewed keyspace.

    rebuild_indexes() drops the sorted sets and re-derives every score from
    the hashes, so a keyspace carrying pre-fix scores is repaired without
    touching the stored values.
    """
    aware = datetime.datetime.now(datetime.timezone.utc)
    ScoreDoc(room_id="r", doc_id="a", ts=aware).save()
    correct = _score()

    # Reproduce a pre-fix score: the same member, shifted by the local offset.
    for key in POPOTO_REDIS_DB.keys("*ScoreDoc*"):
        if POPOTO_REDIS_DB.type(key) != b"zset":
            continue
        for member, _ in POPOTO_REDIS_DB.zrange(key, 0, -1, withscores=True):
            POPOTO_REDIS_DB.zadd(key, {member: correct - OFFSET_SECONDS})
    assert _score() == pytest.approx(correct - OFFSET_SECONDS)

    ScoreDoc.rebuild_indexes()

    assert _score() == pytest.approx(
        correct, abs=1e-6
    ), "rebuild_indexes() must re-derive the score from the stored value"
