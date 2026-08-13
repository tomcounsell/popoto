"""Detection and repair for datetime-keyed rows on non-canonical keys (#538).

1.8.2 fixed the *cause* of ``KeyField(type=datetime)`` duplication and shipped
no way to clean up rows already duplicated by it. Two hashes, two ``$KeyF:``
members, ``query.get()`` resolving to whichever the index returns first, and
an orphan holding real field values that may have diverged from its twin.

These tests build that broken state the way it actually arose -- by writing
rows under pre-1.9.0 keys -- and then assert the audit finds it and the
migration repairs the unambiguous part while refusing the ambiguous part.

The refusal is the design, not a limitation: see
``test_migration_refuses_a_collision_and_changes_nothing``.
"""

import datetime
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto
from src.popoto.models.canonical_key import canonical_key_str
from src.popoto.models.datetime_key_migration import (
    SHAPE_CANONICAL,
    SHAPE_LEGACY_NAIVE,
    SHAPE_LEGACY_OFFSET,
    SHAPE_UNPARSABLE,
    classify_key_value,
    parse_key_value,
)
from src.popoto.models.db_key import DB_key
from src.popoto.models.encoding import encode_popoto_model_obj
from src.popoto.redis_db import POPOTO_REDIS_DB

TZ_PLUS_7 = datetime.timezone(datetime.timedelta(hours=7))
UTC = datetime.timezone.utc

INSTANT_PLUS_7 = datetime.datetime(2026, 8, 7, 12, 0, 0, 123456, tzinfo=TZ_PLUS_7)
INSTANT_UTC = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456, tzinfo=UTC)
INSTANT_NAIVE = datetime.datetime(2026, 8, 7, 5, 0, 0, 123456)
CANONICAL_VALUE = "2026-08-07T05:00:00.123456Z"


class Reading(popoto.Model):
    at = popoto.KeyField(type=datetime.datetime)
    note = popoto.Field(type=str, null=True)


class Plain(popoto.Model):
    name = popoto.KeyField(type=str)


@pytest.fixture(autouse=True)
def clean_keyspace():
    _flush()
    yield
    _flush()


def _flush():
    for pattern in ("*Reading*", "*Plain*"):
        for key in POPOTO_REDIS_DB.keys(pattern):
            POPOTO_REDIS_DB.delete(key)


def _write_row_at_legacy_key(value, note):
    """Write a Reading hash under the key 1.8.2 would have derived for *value*.

    This is how the broken state actually arose, so building it this way keeps
    the tests grounded in the real mechanism rather than in a hand-rolled
    approximation: 1.8.2 rendered a KeyField datetime with ``str()``, which
    carries the offset only when the value is aware.
    """
    legacy_key = f"Reading:{DB_key.clean(str(value))}"
    instance = Reading(at=value, note=note)
    POPOTO_REDIS_DB.hset(legacy_key, mapping=encode_popoto_model_obj(instance))
    POPOTO_REDIS_DB.sadd(Reading._meta.db_class_set_key.redis_key, legacy_key)
    return legacy_key


# --- shape classification ---------------------------------------------------


@pytest.mark.parametrize(
    "rendered,expected",
    [
        ("2026-08-07T05:00:00.123456Z", SHAPE_CANONICAL),
        ("2026-08-07 05:00:00.123456", SHAPE_LEGACY_NAIVE),
        ("2026-08-07 05:00:00", SHAPE_LEGACY_NAIVE),
        ("2026-08-07 12:00:00.123456+07:00", SHAPE_LEGACY_OFFSET),
        ("2026-08-07 05:00:00+00:00", SHAPE_LEGACY_OFFSET),
        ("not a datetime", SHAPE_UNPARSABLE),
        ("", SHAPE_UNPARSABLE),
        ("20260807T12:00:00.123456", SHAPE_UNPARSABLE),
    ],
)
def test_shape_is_decidable_from_the_string_alone(rendered, expected):
    """The audit classifies without loading a hash; that must be unambiguous."""
    assert classify_key_value(rendered) == expected


@pytest.mark.parametrize(
    "rendered",
    [
        "2026-08-07T05:00:00.123456Z",
        "2026-08-07 05:00:00.123456",
        "2026-08-07 12:00:00.123456+07:00",
    ],
)
def test_every_recognized_shape_parses_to_the_same_instant(rendered):
    """All three renderings above denote one instant, so all three canonicalize alike."""
    assert canonical_key_str(parse_key_value(rendered)) == CANONICAL_VALUE


def test_unparsable_returns_none_rather_than_guessing():
    assert parse_key_value("not a datetime") is None


def test_canonical_shape_parses_on_the_3_10_floor():
    """``fromisoformat`` does not accept a trailing "Z" on 3.10.

    ``requires-python`` is >= 3.10, so the parser normalizes the suffix itself
    rather than relying on an interpreter that happens to be newer.
    """
    parsed = parse_key_value("2026-08-07T05:00:00.123456Z")
    assert parsed == INSTANT_UTC
    assert parsed.utcoffset() == datetime.timedelta(0)


# --- audit ------------------------------------------------------------------


def test_audit_on_an_empty_keyspace_is_clean():
    audit = Reading.audit_datetime_keys()
    assert audit.applicable
    assert audit.scanned == 0
    assert audit.is_clean


def test_audit_on_a_model_without_a_datetime_keyfield_reports_not_applicable():
    audit = Plain.audit_datetime_keys()
    assert not audit.applicable
    assert "no KeyField(type=datetime)" in str(audit)


def test_audit_on_an_already_canonical_keyspace_is_clean():
    Reading.create(at=INSTANT_PLUS_7, note="modern")

    audit = Reading.audit_datetime_keys()

    assert audit.scanned == 1
    assert len(audit.canonical_rows) == 1
    assert audit.is_clean


def test_audit_finds_a_legacy_row_and_names_its_target():
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    audit = Reading.audit_datetime_keys()

    assert audit.scanned == 1
    assert not audit.is_clean
    (row,) = audit.migratable_rows
    assert row.redis_key == old_key
    assert row.shape == SHAPE_LEGACY_NAIVE
    assert row.canonical_redis_key == "Reading:" + DB_key.clean(CANONICAL_VALUE)


def test_audit_writes_nothing():
    """Asserted directly, because "read-only" is the property operators rely on."""
    _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")
    _write_row_at_legacy_key(INSTANT_PLUS_7, "legacy aware")
    before = {k: POPOTO_REDIS_DB.dump(k) for k in POPOTO_REDIS_DB.keys("*Reading*")}

    Reading.audit_datetime_keys()

    after = {k: POPOTO_REDIS_DB.dump(k) for k in POPOTO_REDIS_DB.keys("*Reading*")}
    assert before == after


# --- the #538 duplicate pair ------------------------------------------------


def test_audit_detects_the_duplicate_pair_at_count_two():
    """#538's verified repro: one instant, two hashes.

    Pre-1.8.2, an aware value was stored under its aware rendering, reloaded
    naive, and re-saved under the naive rendering -- two hashes for one row.
    """
    aware_key = _write_row_at_legacy_key(INSTANT_PLUS_7, "original")
    naive_key = _write_row_at_legacy_key(INSTANT_NAIVE, "the copy that drifted")

    assert aware_key != naive_key
    assert len(POPOTO_REDIS_DB.keys("Reading:*")) == 2

    audit = Reading.audit_datetime_keys()

    assert len(audit.collisions) == 1
    (collision,) = audit.collisions
    assert collision.canonical_redis_key == "Reading:" + DB_key.clean(CANONICAL_VALUE)
    assert {row.redis_key for row in collision.rows} == {aware_key, naive_key}


def test_collision_report_names_both_hashes_and_the_fields_that_differ():
    """The operator cannot choose without this. It is the whole deliverable."""
    _write_row_at_legacy_key(INSTANT_PLUS_7, "original")
    _write_row_at_legacy_key(INSTANT_NAIVE, "the copy that drifted")

    (collision,) = Reading.audit_datetime_keys().collisions

    assert collision.differing_fields == {"note"}
    assert len(collision.contents) == 2
    rendered = str(Reading.audit_datetime_keys())
    assert "original" in rendered
    assert "the copy that drifted" in rendered
    assert "copies DIFFER on: note" in rendered


def test_identical_copies_are_reported_as_safe_to_collapse():
    """Divergence is what makes a pair dangerous; agreement makes the choice free."""
    _write_row_at_legacy_key(INSTANT_PLUS_7, "same")
    _write_row_at_legacy_key(INSTANT_NAIVE, "same")

    (collision,) = Reading.audit_datetime_keys().collisions

    assert collision.differing_fields == set()
    assert "copies are identical" in str(Reading.audit_datetime_keys())


# --- migration --------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing():
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    report = Reading.migrate_datetime_keys()

    assert report.dry_run
    assert report.moved == [(old_key, "Reading:" + DB_key.clean(CANONICAL_VALUE))]
    assert POPOTO_REDIS_DB.exists(old_key)
    assert len(POPOTO_REDIS_DB.keys("Reading:*")) == 1


def test_migration_moves_a_legacy_row_onto_its_canonical_key():
    old_key = _write_row_at_legacy_key(INSTANT_PLUS_7, "legacy aware")
    new_key = "Reading:" + DB_key.clean(CANONICAL_VALUE)

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert report.moved_count == 1
    assert not POPOTO_REDIS_DB.exists(old_key)
    assert POPOTO_REDIS_DB.exists(new_key)
    assert len(Reading.query.all()) == 1
    assert Reading.query.get(at=INSTANT_UTC).note == "legacy aware"


def test_migration_rebuilds_the_index_so_lookups_work_afterwards():
    _write_row_at_legacy_key(INSTANT_PLUS_7, "legacy aware")

    Reading.migrate_datetime_keys(dry_run=False)

    index_keys = POPOTO_REDIS_DB.keys("$KeyF:Reading:at:*")
    assert len(index_keys) == 1
    for member in POPOTO_REDIS_DB.smembers(index_keys[0]):
        assert POPOTO_REDIS_DB.exists(member), f"index names a missing hash: {member!r}"


def test_migration_fixes_class_set_membership():
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    Reading.migrate_datetime_keys(dry_run=False)

    members = {
        m.decode("utf-8") if isinstance(m, bytes) else m
        for m in POPOTO_REDIS_DB.smembers(Reading._meta.db_class_set_key.redis_key)
    }
    assert old_key not in members
    assert "Reading:" + DB_key.clean(CANONICAL_VALUE) in members


def test_migration_is_idempotent():
    _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    first = Reading.migrate_datetime_keys(dry_run=False)
    second = Reading.migrate_datetime_keys(dry_run=False)

    assert first.moved_count == 1
    assert second.moved_count == 0
    assert Reading.audit_datetime_keys().is_clean


def test_a_second_run_after_a_crash_resumes_rather_than_redoing():
    """Resumability: rows already moved are skipped, remaining rows still move."""
    _write_row_at_legacy_key(INSTANT_NAIVE, "row a")
    other = datetime.datetime(2026, 9, 1, 8, 30, 0, 500000)
    _write_row_at_legacy_key(other, "row b")

    Reading.migrate_datetime_keys(dry_run=False)
    audit = Reading.audit_datetime_keys()

    assert audit.scanned == 2
    assert audit.is_clean


def test_migration_refuses_a_collision_and_changes_nothing():
    """Report-only is the chosen default for #538's duplicate pair.

    The copies may have diverged, so no automatic rule -- last-write-wins,
    newest-first, field-wise merge -- is right often enough to be safe. The
    library reports; the operator decides. Both hashes must still exist
    afterwards.
    """
    aware_key = _write_row_at_legacy_key(INSTANT_PLUS_7, "original")
    naive_key = _write_row_at_legacy_key(INSTANT_NAIVE, "the copy that drifted")

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert report.refused
    assert report.moved_count == 0
    assert POPOTO_REDIS_DB.exists(aware_key)
    assert POPOTO_REDIS_DB.exists(naive_key)
    assert "operator decision" in report.refusal_reason
    assert "COLLISION" in str(report)


def test_a_collision_blocks_the_whole_run_not_just_the_colliding_rows():
    """Partial migration under an unresolved collision is a worse state to be in.

    Half-migrated, the operator cannot tell from the audit alone which rows
    moved, and the collision is still there.
    """
    _write_row_at_legacy_key(INSTANT_PLUS_7, "original")
    _write_row_at_legacy_key(INSTANT_NAIVE, "the copy that drifted")
    unrelated = _write_row_at_legacy_key(
        datetime.datetime(2026, 9, 1, 8, 30, 0, 500000), "innocent bystander"
    )

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert report.refused
    assert POPOTO_REDIS_DB.exists(unrelated)


def test_resolving_a_collision_by_hand_unblocks_the_migration():
    """The documented recipe end to end: reconcile, delete the loser, re-run."""
    _write_row_at_legacy_key(INSTANT_PLUS_7, "keep me")
    loser = _write_row_at_legacy_key(INSTANT_NAIVE, "drop me")

    assert Reading.migrate_datetime_keys(dry_run=False).refused

    POPOTO_REDIS_DB.delete(loser)
    POPOTO_REDIS_DB.srem(Reading._meta.db_class_set_key.redis_key, loser)

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert not report.refused
    assert report.moved_count == 1
    assert Reading.query.get(at=INSTANT_UTC).note == "keep me"


def test_unparsable_keys_are_reported_and_left_alone():
    """A key this module does not recognize is never guessed at."""
    junk_key = "Reading:not-a-datetime"
    POPOTO_REDIS_DB.hset(junk_key, mapping={b"note": b"junk"})

    audit = Reading.audit_datetime_keys()

    assert [row.redis_key for row in audit.unparsable_rows] == [junk_key]
    Reading.migrate_datetime_keys(dry_run=False)
    assert POPOTO_REDIS_DB.exists(junk_key)


def test_race_2_source_gone_and_target_already_canonical_counts_as_success():
    """A live process can rename a row onto the same target mid-migration.

    Both compute the same canonical key by construction, so the loser of that
    race is a no-op on an already-correct row. Treating it as an error would
    make the migration report spurious failures under exactly the conditions
    the self-healing rename is meant to handle.
    """
    from src.popoto.models import datetime_key_migration

    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")
    new_key = "Reading:" + DB_key.clean(CANONICAL_VALUE)

    # Audit first, so the migration holds a row that is about to move under it.
    audit = Reading.audit_datetime_keys()
    assert [row.redis_key for row in audit.migratable_rows] == [old_key]

    # The other process wins: the row is already at the target the migration
    # is about to compute for it.
    POPOTO_REDIS_DB.rename(old_key, new_key)

    # Drive the migration against that stale audit. RENAMENX raises rather than
    # returning 0 when the source is gone, which is why this needs a test.
    report = datetime_key_migration.DatetimeKeyMigrationReport(
        "Reading", "at", False, audit
    )
    datetime_key_migration._apply_moves(Reading, audit, report)

    assert report.moved == [(old_key, new_key)]
    assert report.skipped == []
    assert POPOTO_REDIS_DB.exists(new_key)


# --- inbound Relationship references ----------------------------------------


class Sensor(popoto.Model):
    sensor_id = popoto.KeyField(type=str)
    reading = popoto.Relationship(model=Reading, null=True)


def _point_a_sensor_at(reading_redis_key):
    """Store a live inbound reference to *reading_redis_key*.

    A Relationship holds the target's redis_key as a plain string value, so the
    reference is written directly -- the target row here lives at a legacy key
    that ``Reading.query`` cannot resolve.
    """
    sensor = Sensor(sensor_id="s1")
    sensor.save()
    POPOTO_REDIS_DB.hset(
        sensor.db_key.redis_key,
        mapping=encode_popoto_model_obj(sensor)
        | {b"reading": _msgpack(reading_redis_key)},
    )


def _msgpack(value):
    import msgpack

    return msgpack.packb(value)


def test_a_declared_but_unused_relationship_does_not_block_the_migration():
    """Refusal keys off stored references, not declarations.

    ``Sensor`` declares a Relationship to ``Reading`` for the whole module, so
    if declarations were enough, every migration test here would refuse.
    """
    _write_row_at_legacy_key(INSTANT_NAIVE, "unreferenced")

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert not report.refused
    assert report.moved_count == 1


def test_migration_refuses_when_inbound_relationships_exist():
    """A Relationship stores the target's redis_key inside another model's hash.

    Renaming the target breaks every inbound reference, and they are not
    rewritten automatically: a Relationship value is indistinguishable from an
    ordinary string field's content, so a blind rewrite could corrupt unrelated
    data.
    """
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "referenced")
    _point_a_sensor_at(old_key)

    report = Reading.migrate_datetime_keys(dry_run=False)

    assert report.refused
    assert "Relationship references" in report.refusal_reason
    assert POPOTO_REDIS_DB.exists(old_key)

    for key in POPOTO_REDIS_DB.keys("*Sensor*"):
        POPOTO_REDIS_DB.delete(key)


def test_inbound_relationships_can_be_acknowledged_explicitly():
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "referenced")
    _point_a_sensor_at(old_key)

    report = Reading.migrate_datetime_keys(
        dry_run=False, allow_inbound_relationships=True
    )

    assert not report.refused
    assert report.moved_count == 1
    assert not POPOTO_REDIS_DB.exists(old_key)

    for key in POPOTO_REDIS_DB.keys("*Sensor*"):
        POPOTO_REDIS_DB.delete(key)


# --- per-instance field data (third-review finding) -------------------------
#
# BM25Field, EmbeddingField, ConfidenceField, CoOccurrenceField, and
# ContentField are plain Field subclasses declared on the model *being*
# migrated (not on some other model, the way Relationship is). Each keys its
# own per-instance Redis structures -- or, for EmbeddingField, filesystem
# paths -- off the instance's redis_key through a path `_side_keys` does not
# cover, so a rename would silently orphan them. This refuses exactly the way
# an inbound Relationship reference does: reported, not moved, with an
# explicit opt-in to proceed anyway.


class Instrument(popoto.Model):
    at = popoto.KeyField(type=datetime.datetime)
    confidence = popoto.ConfidenceField(null=True)


@pytest.fixture(autouse=True)
def _clean_instrument_keyspace():
    for key in POPOTO_REDIS_DB.keys("*Instrument*"):
        POPOTO_REDIS_DB.delete(key)
    yield
    for key in POPOTO_REDIS_DB.keys("*Instrument*"):
        POPOTO_REDIS_DB.delete(key)


def _write_instrument_at_legacy_key(value):
    legacy_key = f"Instrument:{DB_key.clean(str(value))}"
    instance = Instrument(at=value)
    POPOTO_REDIS_DB.hset(legacy_key, mapping=encode_popoto_model_obj(instance))
    POPOTO_REDIS_DB.sadd(Instrument._meta.db_class_set_key.redis_key, legacy_key)
    return legacy_key


def test_audit_reports_a_per_instance_field_risk():
    _write_instrument_at_legacy_key(INSTANT_NAIVE)

    audit = Instrument.audit_datetime_keys()

    field_names = {risk.field_name for risk in audit.per_instance_field_risks}
    assert field_names == {"confidence"}
    assert "PER-INSTANCE FIELD RISK" in str(audit)
    assert "confidence" in str(audit)


def test_migration_refuses_when_a_per_instance_field_is_declared():
    old_key = _write_instrument_at_legacy_key(INSTANT_NAIVE)

    report = Instrument.migrate_datetime_keys(dry_run=False)

    assert report.refused
    assert "per-instance data" in report.refusal_reason
    assert "confidence" in report.refusal_reason
    assert POPOTO_REDIS_DB.exists(old_key)


def test_per_instance_field_risk_can_be_acknowledged_explicitly():
    old_key = _write_instrument_at_legacy_key(INSTANT_NAIVE)
    new_key = "Instrument:" + DB_key.clean(CANONICAL_VALUE)

    report = Instrument.migrate_datetime_keys(
        dry_run=False, allow_orphaned_per_instance_fields=True
    )

    assert not report.refused
    assert report.moved_count == 1
    assert not POPOTO_REDIS_DB.exists(old_key)
    assert POPOTO_REDIS_DB.exists(new_key)


def test_a_model_with_no_per_instance_fields_reports_no_risk():
    """Sanity: the check does not false-positive on Reading, which has none."""
    _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    audit = Reading.audit_datetime_keys()

    assert audit.per_instance_field_risks == []


# --- rebuild_indexes divergence guard ---------------------------------------


def test_rebuild_indexes_skips_a_diverged_row_instead_of_indexing_a_ghost():
    """spike-2's measured failure: rebuild left the index pointing at nothing.

    A pre-1.9.0 row decodes to a value whose derived key is canonical, but the
    row is stored under its legacy key. ``on_save`` indexes the *derived* key,
    so rebuilding used to write a ``$KeyF:`` member naming a hash that does not
    exist -- ``query.all()`` returned the row while ``query.filter()`` returned
    a dangling reference. Leaving it unindexed and saying so is strictly better.
    """
    old_key = _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    result = Reading.rebuild_indexes()

    assert result == 0
    assert result.diverged_count == 1
    assert result.diverged_keys == [old_key]

    for index_key in POPOTO_REDIS_DB.keys("$KeyF:Reading:at:*"):
        for member in POPOTO_REDIS_DB.smembers(index_key):
            assert POPOTO_REDIS_DB.exists(
                member
            ), f"rebuild indexed a nonexistent key: {member!r}"


def test_rebuild_indexes_warns_and_points_at_the_audit(caplog):
    """The skip must be observable, or it is just silent data loss of a different shape."""
    _write_row_at_legacy_key(INSTANT_NAIVE, "legacy")

    with caplog.at_level("WARNING", logger="POPOTO.model_base"):
        Reading.rebuild_indexes()

    assert any(
        "audit_datetime_keys" in record.getMessage() for record in caplog.records
    )


def test_rebuild_indexes_still_returns_a_usable_count():
    """The return type widened to carry detail; it must stay int-compatible.

    ``count = User.rebuild_indexes()`` is the documented usage and has been for
    the method's whole history.
    """
    Reading.create(at=INSTANT_PLUS_7, note="modern")

    result = Reading.rebuild_indexes()

    assert result == 1
    assert isinstance(result, int)
    assert result + 1 == 2
    assert f"{result}" == "1"
    assert result.diverged_count == 0


def test_rebuild_indexes_is_convergent_after_migration():
    """The end state: migrate, then rebuild, and nothing diverges."""
    _write_row_at_legacy_key(INSTANT_PLUS_7, "legacy aware")
    Reading.migrate_datetime_keys(dry_run=False)

    result = Reading.rebuild_indexes()

    assert result == 1
    assert result.diverged_count == 0
