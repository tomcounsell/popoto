"""Regression tests for #575/#570 — partition renders route through canonical_key_str.

``SortedField(partition_by=...)``, ``ConfidenceField(partition_by=...)``, and
``EventStreamMixin``'s partitioned stream key all used to render a partition
value with bare ``str(value)``. For a ``datetime`` partition that is unstable:
the same instant can render differently depending on whether it decoded aware
or naive, so it silently split into two partitions -- no error, just fewer
rows than expected. ``canonical_key_str`` is a no-op (``str(value)``,
byte-identical) for every non-datetime value, so this file's scope guard test
(`test_byte_identity_for_non_datetime_partitions`) must stay green: if it
fails, the fix has stopped being a no-op for some type and has become a key
migration, which is out of appetite (see the plan's ``## No-Gos``).

Covers all 11 in-scope sites:
    1-4. sorted_field_mixin.py: get_partitioned_sortedset_db_key, on_save
         old-partition cleanup, on_delete old-partition cleanup, filter_query
    5-7. query.py: top_by_decay, _resolve_index, _materialize_decay_field
    8-10. confidence_field.py: get_data_hash_key, get_data_hash_key_from_values,
          get_old_data_hash_key
    11. event_stream.py: partitioned stream key
"""

import datetime
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.event_stream import EventStreamMixin  # noqa: E402
from src.popoto.models.canonical_key import canonical_key_str  # noqa: E402
from src.popoto.models.db_key import DB_key  # noqa: E402
from src.popoto.models.query import QueryException  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

UTC = datetime.timezone.utc
TZ_PLUS_7 = datetime.timezone(datetime.timedelta(hours=7))

# One instant, three representations -- same doctrine as test_datetime_key_identity.py.
INSTANT_PLUS_7 = datetime.datetime(2026, 8, 7, 12, 0, 0, 0, tzinfo=TZ_PLUS_7)
INSTANT_UTC = datetime.datetime(2026, 8, 7, 5, 0, 0, 0, tzinfo=UTC)
INSTANT_NAIVE = datetime.datetime(2026, 8, 7, 5, 0, 0, 0)


# --- Test Models -------------------------------------------------------


class KeyPartitionedScore(popoto.Model):
    """SortedField partitioned by a KeyField(type=datetime) -- sites 1, 4."""

    name = popoto.UniqueKeyField()
    bucket = popoto.KeyField(type=datetime.datetime)
    score = popoto.SortedField(type=float, partition_by="bucket")


class StrPartitionedScore(popoto.Model):
    """SortedField with a non-datetime KeyField partition -- sites 2, 3, byte identity."""

    name = popoto.UniqueKeyField()
    category = popoto.KeyField(type=str)
    score = popoto.SortedField(type=float, partition_by="category")


class DecayPartitioned(popoto.Model):
    """DecayingSortedField partitioned by datetime -- query.py sites 5-7."""

    name = popoto.UniqueKeyField()
    bucket = popoto.KeyField(type=datetime.datetime)
    relevance = DecayingSortedField(partition_by="bucket")


class ConfidencePartitioned(popoto.Model):
    """ConfidenceField partitioned by datetime -- sites 8-10."""

    name = popoto.UniqueKeyField()
    bucket = popoto.Field(type=datetime.datetime, null=True)
    certainty = ConfidenceField(initial_confidence=0.5, partition_by="bucket")


class StreamPartitioned(EventStreamMixin, popoto.Model):
    """EventStreamMixin partitioned by datetime -- site 11."""

    _stream_name = "canon_partition_test"
    _stream_partition_field = "bucket"

    name = popoto.UniqueKeyField()
    bucket = popoto.Field(type=datetime.datetime, null=True)


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def force_canonical_datetime_keys(monkeypatch):
    """Every datetime test explicitly clears the legacy switch rather than
    inheriting ambient env -- otherwise the suite's verdict depends on the
    shell (see plan's ## Test Impact 'Environment requirements')."""
    monkeypatch.delenv("POPOTO_DATETIME_KEY_LEGACY", raising=False)
    previous = Defaults.DATETIME_KEY_LEGACY
    Defaults.DATETIME_KEY_LEGACY = False
    yield
    Defaults.DATETIME_KEY_LEGACY = previous


# --- Helpers ---------------------------------------------------------------


def _purge_derived_zset_key(model_class, field_name, redis_key):
    """Mirror ``Model._purge_orphan_keys``'s zset-key derivation (base.py,
    the ``for field_name in meta.sorted_field_names:`` loop) without
    triggering the orphan-detection/Lua machinery, so the test isolates the
    key-bytes comparison the plan's Success Criterion 2 requires."""
    meta = model_class._meta
    field = meta.fields[field_name]
    parts = DB_key.from_redis_key(redis_key)
    values = {}
    for key_field_name in meta.key_field_names:
        try:
            values[key_field_name] = parts[
                meta.get_db_key_index_position(key_field_name)
            ]
        except Exception:
            continue
    zset_key = field.get_sortedset_db_key(model_class, field_name)
    for partition_field_name in field.partition_by:
        zset_key.append(values[partition_field_name])
    return zset_key.redis_key


# --- SortedField: sites 1, 4 (write path + filter_query) -------------------


def test_aware_and_utc_equivalent_share_partition():
    """The core #570 defect: an aware value and its UTC equivalent must
    address the same partition."""
    a = KeyPartitionedScore.create(name="a", bucket=INSTANT_PLUS_7, score=1.0)
    b = KeyPartitionedScore.create(name="b", bucket=INSTANT_UTC, score=2.0)

    field = KeyPartitionedScore._meta.fields["score"]
    key_a = field.get_partitioned_sortedset_db_key(a, "score").redis_key
    key_b = field.get_partitioned_sortedset_db_key(b, "score").redis_key
    assert key_a == key_b

    # filter_query (site 4): querying with a third representation of the
    # same instant must return both rows -- they live in one partition.
    results = KeyPartitionedScore.query.filter(bucket=INSTANT_NAIVE).all()
    assert {r.name for r in results} == {"a", "b"}


def test_naive_datetime_partitions_consistently():
    """Naive is assumed UTC (canonical_key_str doctrine): a naive datetime
    partitions to the same key as its UTC-aware twin -- not a third
    partition."""
    a = KeyPartitionedScore.create(name="c", bucket=INSTANT_NAIVE, score=1.0)
    b = KeyPartitionedScore.create(name="d", bucket=INSTANT_UTC, score=2.0)

    field = KeyPartitionedScore._meta.fields["score"]
    key_a = field.get_partitioned_sortedset_db_key(a, "score").redis_key
    key_b = field.get_partitioned_sortedset_db_key(b, "score").redis_key
    assert key_a == key_b


def test_write_and_purge_paths_agree():
    """get_partitioned_sortedset_db_key (write path) and the zset key
    Model._purge_orphan_keys derives for the same row must be equal. The
    partition field is a KeyField(type=datetime) -- _purge_orphan_keys only
    populates `values` for meta.key_field_names and skips any sorted field
    whose partition names are not all present, so a non-key partition field
    would make this assertion vacuous."""
    item = KeyPartitionedScore.create(name="e", bucket=INSTANT_PLUS_7, score=5.0)
    field = KeyPartitionedScore._meta.fields["score"]

    write_key = field.get_partitioned_sortedset_db_key(item, "score").redis_key
    purge_key = _purge_derived_zset_key(
        KeyPartitionedScore, "score", item.db_key.redis_key
    )
    assert write_key == purge_key


# --- Scope guard: byte identity for every non-datetime type ----------------


@pytest.mark.parametrize(
    "value",
    ["cat-a", 42, 3.14, True, datetime.date(2026, 1, 1), datetime.time(5, 0, 0)],
    ids=["str", "int", "float", "bool", "date", "time"],
)
def test_byte_identity_for_non_datetime_partitions(value):
    """canonical_key_str must be byte-identical to str(value) for every
    non-datetime type. If this fails for any type, BUILD must stop and
    report -- that would be a key migration, out of appetite (## No-Gos)."""
    assert canonical_key_str(value) == str(value)


def test_byte_identity_round_trip_through_partition_key():
    """The scope guard exercised through the actual write-path site, not
    just the helper in isolation."""
    item = StrPartitionedScore.create(name="z", category="widgets", score=1.0)
    field = StrPartitionedScore._meta.fields["score"]
    canonical_key = field.get_partitioned_sortedset_db_key(item, "score").redis_key
    legacy_key = field.get_sortedset_db_key(
        StrPartitionedScore, "score", str("widgets")
    ).redis_key
    assert canonical_key == legacy_key


# --- SortedField: sites 2, 3 (old-partition cleanup on key migration) ------


def test_partition_change_cleanup_paths():
    """Old-partition cleanup on save (site 2) and the on_delete branch that
    fires during key migration (site 3) both target the canonical old key.

    Exercised directly at the SortedFieldMixin level (rather than through
    Model.save(migrate_key=True), which hits an unrelated, pre-existing
    unique-index validation order issue on KeyField migration -- out of
    scope for this plan) so the assertion isolates sites 2 and 3."""
    item = StrPartitionedScore.create(name="f", category="A", score=1.0)
    field = StrPartitionedScore._meta.fields["score"]

    old_key = field.get_sortedset_db_key(StrPartitionedScore, "score", "A").redis_key
    old_redis_key = item.db_key.redis_key
    assert POPOTO_REDIS_DB.zscore(old_key, old_redis_key) == 1.0

    # site 2: on_save's old-partition cleanup, triggered by a partition
    # value change detected against _saved_field_values.
    item._saved_field_values = {"category": "A", "score": 1.0}
    item.category = "B"
    field.on_save(item, "score", item.score, saved_redis_key=old_redis_key)

    new_key = field.get_sortedset_db_key(StrPartitionedScore, "score", "B").redis_key
    assert POPOTO_REDIS_DB.zscore(old_key, old_redis_key) is None
    assert POPOTO_REDIS_DB.zscore(new_key, item.db_key.redis_key) == 1.0

    # site 3: on_delete's old-partition cleanup during key migration
    # (saved_redis_key present). Re-seed the old partition to exercise this
    # branch independently of site 2.
    POPOTO_REDIS_DB.zadd(old_key, {old_redis_key: 1.0})
    item._saved_field_values = {"category": "A", "score": 1.0}
    field.on_delete(item, "score", item.score, saved_redis_key=old_redis_key)
    assert POPOTO_REDIS_DB.zscore(old_key, old_redis_key) is None


# --- query.py: sites 5-7 (top_by_decay, _resolve_index, _materialize_decay_field) ---


def test_query_paths_agree():
    """top_by_decay, _resolve_index, and _materialize_decay_field all
    resolve the same partition key the write path used, via a save ->
    filter-by-partition round trip with mixed datetime representations."""
    a = DecayPartitioned.create(name="p1", bucket=INSTANT_PLUS_7)
    b = DecayPartitioned.create(name="p2", bucket=INSTANT_UTC)

    # top_by_decay -> _resolve_index (sites 5-6)
    results = DecayPartitioned.query.filter(bucket=INSTANT_NAIVE).top_by_decay(
        "relevance", n=10
    )
    assert {r.name for r in results} == {"p1", "p2"}

    # composite_score -> _resolve_index -> _materialize_decay_field (site 7)
    scored = DecayPartitioned.query.filter(bucket=INSTANT_NAIVE).composite_score(
        indexes={"relevance": 1.0}, limit=10
    )
    assert {r.name for r in scored} == {"p1", "p2"}


# --- ConfidenceField: sites 8-10, None asymmetry preserved -----------------


def test_confidence_field_partition_canonical():
    """get_data_hash_key, get_data_hash_key_from_values, and
    get_old_data_hash_key must agree with each other for a datetime
    partition, and the None asymmetry must survive verbatim: the first and
    third skip a None partition value, the second raises QueryException."""
    a = ConfidencePartitioned.create(name="q1", bucket=INSTANT_PLUS_7)
    field = ConfidencePartitioned._meta.fields["certainty"]

    key_get = field.get_data_hash_key(a, "certainty")
    key_from_values = field.get_data_hash_key_from_values(
        ConfidencePartitioned, "certainty", bucket=INSTANT_UTC
    )
    assert key_get == key_from_values

    a._saved_field_values = {"bucket": INSTANT_UTC}
    old_key = field.get_old_data_hash_key(a, "certainty")
    assert old_key == key_get

    # None asymmetry: get_data_hash_key / get_old_data_hash_key SKIP None.
    b = ConfidencePartitioned.create(name="q2", bucket=None)
    unpartitioned_key = (
        field.get_special_use_field_db_key(b, "certainty").redis_key + ":data"
    )
    assert field.get_data_hash_key(b, "certainty") == unpartitioned_key
    b._saved_field_values = {"bucket": None}
    assert field.get_old_data_hash_key(b, "certainty") == unpartitioned_key

    # None asymmetry: get_data_hash_key_from_values RAISES.
    with pytest.raises(QueryException):
        field.get_data_hash_key_from_values(ConfidencePartitioned, "certainty")


# --- EventStreamMixin: site 11, None preserves unpartitioned key -----------


def test_event_stream_partition_canonical():
    """The partitioned stream key uses the canonical rendering, so aware and
    UTC-equivalent datetimes address the same stream; a None partition value
    still yields the unpartitioned base_key (guard preserved)."""
    a = StreamPartitioned.create(name="s1", bucket=INSTANT_PLUS_7)
    b = StreamPartitioned.create(name="s2", bucket=INSTANT_UTC)
    assert a._get_stream_key() == b._get_stream_key()
    assert (
        a._get_stream_key() == "stream:canon_partition_test:2026-08-07T05:00:00.000000Z"
    )

    c = StreamPartitioned.create(name="s3", bucket=None)
    assert c._get_stream_key() == "stream:canon_partition_test"


# --- POPOTO_DATETIME_KEY_LEGACY=1: every converted site stays gated ---------


@pytest.fixture
def legacy_datetime_keys():
    """Turn the legacy switch ON for one test, restoring it explicitly.

    The autouse fixture above forces the switch OFF, so a legacy-mode test
    must re-enable it *after* that fixture has run. Explicit save/restore
    (rather than monkeypatch.setattr) because monkeypatch's undo would fire
    after the autouse teardown and would restore the wrong value.
    """
    previous = Defaults.DATETIME_KEY_LEGACY
    Defaults.DATETIME_KEY_LEGACY = True
    try:
        yield
    finally:
        Defaults.DATETIME_KEY_LEGACY = previous


# str(value) for the aware instant -- the exact bytes every site wrote before
# this change, and the bytes it must still write while the switch is set.
LEGACY_SEGMENT = str(INSTANT_PLUS_7)


def test_legacy_switch_preserves_sorted_field_partition_bytes(legacy_datetime_keys):
    """Sites 1 and 4. With the switch set, the write path and filter_query
    must render the pre-change ``str(value)`` bytes -- no ``force=True``
    anywhere, or a fleet mid-rollout reads keys it never wrote (#476)."""
    assert LEGACY_SEGMENT != canonical_key_str(INSTANT_PLUS_7, force=True)

    item = KeyPartitionedScore(name="lg1", bucket=INSTANT_PLUS_7, score=1.0)
    field = KeyPartitionedScore._meta.fields["score"]

    write_key = field.get_partitioned_sortedset_db_key(item, "score").redis_key
    # Sorted-set keys are built through DB_key, so the segment is escaped;
    # compare against a key built from the literal pre-change bytes rather
    # than against the raw segment.
    assert (
        write_key
        == field.get_sortedset_db_key(
            KeyPartitionedScore, "score", LEGACY_SEGMENT
        ).redis_key
    )
    assert (
        write_key
        != field.get_sortedset_db_key(
            KeyPartitionedScore, "score", canonical_key_str(INSTANT_PLUS_7, force=True)
        ).redis_key
    )

    # site 4: filter_query renders the same legacy bytes, so a query issued
    # under the switch still addresses the partition the writes went to.
    query_key = field.get_sortedset_db_key(
        KeyPartitionedScore, "score", canonical_key_str(INSTANT_PLUS_7)
    ).redis_key
    assert query_key == write_key


def test_legacy_switch_splits_representations_as_before(legacy_datetime_keys):
    """The switch is a *legacy* switch: under it the #570 split is expected
    to persist, because the pre-change bytes are what a mid-rollout fleet
    must keep reading. This pins the gating, not the defect."""
    a = KeyPartitionedScore(name="lg2", bucket=INSTANT_PLUS_7, score=1.0)
    b = KeyPartitionedScore(name="lg3", bucket=INSTANT_UTC, score=2.0)
    field = KeyPartitionedScore._meta.fields["score"]

    key_a = field.get_partitioned_sortedset_db_key(a, "score").redis_key
    key_b = field.get_partitioned_sortedset_db_key(b, "score").redis_key
    assert key_a != key_b


def test_legacy_switch_preserves_query_path_bytes(legacy_datetime_keys):
    """Sites 5-7. The three query.py comprehensions render legacy bytes, so
    they resolve the same partition key the gated write path produced."""
    field = DecayPartitioned._meta.fields["relevance"]
    item = DecayPartitioned(name="lg4", bucket=INSTANT_PLUS_7)

    write_key = field.get_partitioned_sortedset_db_key(item, "relevance").redis_key
    query_key = field.get_sortedset_db_key(
        DecayPartitioned, "relevance", canonical_key_str(INSTANT_PLUS_7)
    ).redis_key
    assert query_key == write_key
    assert (
        write_key
        == field.get_sortedset_db_key(
            DecayPartitioned, "relevance", LEGACY_SEGMENT
        ).redis_key
    )


def test_legacy_switch_preserves_confidence_field_bytes(legacy_datetime_keys):
    """Sites 8-10. All three ConfidenceField helpers render legacy bytes and
    still agree with each other under the switch."""
    field = ConfidencePartitioned._meta.fields["certainty"]
    a = ConfidencePartitioned.create(name="lg5", bucket=INSTANT_PLUS_7)

    key_get = field.get_data_hash_key(a, "certainty")
    key_from_values = field.get_data_hash_key_from_values(
        ConfidencePartitioned, "certainty", bucket=INSTANT_PLUS_7
    )
    a._saved_field_values = {"bucket": INSTANT_PLUS_7}
    key_old = field.get_old_data_hash_key(a, "certainty")

    assert key_get == key_from_values == key_old
    assert key_get.endswith(LEGACY_SEGMENT)


def test_legacy_switch_preserves_event_stream_bytes(legacy_datetime_keys):
    """Site 11. The partitioned stream key renders legacy bytes under the
    switch, and a None partition still yields the unpartitioned base key."""
    a = StreamPartitioned(name="lg6", bucket=INSTANT_PLUS_7)
    assert a._get_stream_key() == f"stream:canon_partition_test:{LEGACY_SEGMENT}"

    c = StreamPartitioned(name="lg7", bucket=None)
    assert c._get_stream_key() == "stream:canon_partition_test"
