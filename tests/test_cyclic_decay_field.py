"""Tests for CyclicDecayField — Temporal Rhythms + Homeostatic Pressure.

Tests cover:
- Field initialization (cycles, pressure_rate, validation)
- TemporalPeriod constants
- on_save() stores companion hash data
- on_delete() cleans up companion hashes
- top_by_decay() with cyclic resonance
- top_by_decay() with homeostatic pressure
- Three-force superposition against hand-computed values
- CyclicDecayField with no cycles matches DecayingSortedField
- partition_by works with CyclicDecayField
- resolve_pressure() method
- Error cases: unsaved model, wrong field type, invalid parameters
- Benchmarks for 1K and 10K member sets
"""

import math
import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
from src import popoto
from src.popoto.fields.cyclic_decay_field import CyclicDecayField, CYCLIC_DECAY_LUA
from src.popoto.fields.decaying_sorted_field import DecayingSortedField, DECAY_SCORE_LUA
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.models.query import QueryException

# --- Test Models ---


class CyclicItem(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField()


class CyclicWithCycles(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
    )


class CyclicWithPressure(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        pressure_rate=0.1,
    )


class CyclicFull(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
        pressure_rate=0.1,
    )
    weight = popoto.FloatField(default=1.0)


class CyclicWithBase(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        base_score_field="weight",
        cycles=[(TemporalPeriod.QUARTERLY, 3.0, 0)],
        pressure_rate=0.2,
    )
    weight = popoto.FloatField(default=1.0)


class PartitionedCyclic(popoto.Model):
    name = popoto.UniqueKeyField()
    category = popoto.KeyField(null=False)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.MONTHLY, 2.0, 0)],
        pressure_rate=0.05,
        partition_by="category",
    )


class PlainDecayItem(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)


ALL_MODELS = [
    CyclicItem,
    CyclicWithCycles,
    CyclicWithPressure,
    CyclicFull,
    CyclicWithBase,
    PartitionedCyclic,
    PlainDecayItem,
]


# --- Setup / Teardown ---


def setup_module():
    """Clean up test data before running tests."""
    for model in ALL_MODELS:
        model.delete_all()


def teardown_module():
    """Clean up test data after running tests."""
    for model in ALL_MODELS:
        model.delete_all()


# --- TemporalPeriod constants tests ---


class TestTemporalPeriod:
    """Test TemporalPeriod constants."""

    def test_daily(self):
        assert TemporalPeriod.DAILY == 86_400

    def test_weekly(self):
        assert TemporalPeriod.WEEKLY == 604_800

    def test_monthly(self):
        assert TemporalPeriod.MONTHLY == 2_592_000

    def test_quarterly(self):
        assert TemporalPeriod.QUARTERLY == 7_776_000

    def test_yearly(self):
        assert TemporalPeriod.YEARLY == 31_536_000

    def test_importable_from_popoto(self):
        from src.popoto import TemporalPeriod as TP

        assert TP.DAILY == 86_400


# --- Field initialization tests ---


class TestCyclicDecayFieldInit:
    """Test field construction and validation."""

    def test_default_cycles_empty(self):
        field = CyclicItem._meta.fields["relevance"]
        assert field.cycles == []

    def test_default_pressure_rate_zero(self):
        field = CyclicItem._meta.fields["relevance"]
        assert field.pressure_rate == 0.0

    def test_custom_cycles(self):
        field = CyclicWithCycles._meta.fields["relevance"]
        assert len(field.cycles) == 1
        assert field.cycles[0][0] == TemporalPeriod.YEARLY
        assert field.cycles[0][1] == 5.0

    def test_custom_pressure_rate(self):
        field = CyclicWithPressure._meta.fields["relevance"]
        assert field.pressure_rate == 0.1

    def test_inherits_decay_rate(self):
        field = CyclicFull._meta.fields["relevance"]
        assert field.decay_rate == 0.5

    def test_is_decaying_sorted_field(self):
        field = CyclicFull._meta.fields["relevance"]
        assert isinstance(field, DecayingSortedField)

    def test_is_cyclic_decay_field(self):
        field = CyclicFull._meta.fields["relevance"]
        assert isinstance(field, CyclicDecayField)

    def test_invalid_zero_period(self):
        with pytest.raises(popoto.ModelException):

            class BadCyclic(popoto.Model):
                name = popoto.UniqueKeyField()
                score = CyclicDecayField(cycles=[(0, 1.0, 0)])

    def test_invalid_negative_period(self):
        with pytest.raises(popoto.ModelException):

            class BadCyclic2(popoto.Model):
                name = popoto.UniqueKeyField()
                score = CyclicDecayField(cycles=[(-100, 1.0, 0)])

    def test_invalid_negative_amplitude(self):
        with pytest.raises(popoto.ModelException):

            class BadCyclic3(popoto.Model):
                name = popoto.UniqueKeyField()
                score = CyclicDecayField(cycles=[(86400, -1.0, 0)])

    def test_invalid_negative_pressure_rate(self):
        with pytest.raises(popoto.ModelException):

            class BadCyclic4(popoto.Model):
                name = popoto.UniqueKeyField()
                score = CyclicDecayField(pressure_rate=-1)

    def test_exportable_from_popoto(self):
        from src.popoto import CyclicDecayField as CDF

        assert CDF is CyclicDecayField


# --- Save behavior tests ---


class TestCyclicDecayFieldSave:
    """Test that on_save() stores companion hash data."""

    def setup_method(self):
        CyclicWithCycles.delete_all()
        CyclicWithPressure.delete_all()
        CyclicFull.delete_all()

    def teardown_method(self):
        CyclicWithCycles.delete_all()
        CyclicWithPressure.delete_all()
        CyclicFull.delete_all()

    def test_save_stores_timestamp(self):
        before = time.time()
        item = CyclicFull.create(name="ts_test")
        after = time.time()
        assert before <= item.relevance <= after

    def test_save_stores_cycles_hash(self):
        item = CyclicWithCycles.create(name="cycles_test")
        field = CyclicWithCycles._meta.fields["relevance"]
        cycles_key = field.get_cycles_hash_key(item, "relevance")
        raw = popoto.POPOTO_REDIS_DB.hget(cycles_key, item.db_key.redis_key)
        assert raw is not None
        decoded = msgpack.unpackb(raw, raw=False)
        assert len(decoded) == 1
        assert decoded[0][0] == TemporalPeriod.YEARLY
        assert decoded[0][1] == 5.0

    def test_save_stores_pressure_hash(self):
        item = CyclicWithPressure.create(name="pressure_test")
        field = CyclicWithPressure._meta.fields["relevance"]
        pressure_key = field.get_pressure_hash_key(item, "relevance")
        raw = popoto.POPOTO_REDIS_DB.hget(pressure_key, item.db_key.redis_key)
        assert raw is not None
        decoded = msgpack.unpackb(raw, raw=False)
        assert decoded["rate"] == 0.1
        assert "last_resolved" in decoded

    def test_save_preserves_last_resolved(self):
        """Re-saving does not overwrite last_resolved."""
        item = CyclicWithPressure.create(name="preserve_lr")
        field = CyclicWithPressure._meta.fields["relevance"]
        pressure_key = field.get_pressure_hash_key(item, "relevance")

        raw1 = popoto.POPOTO_REDIS_DB.hget(pressure_key, item.db_key.redis_key)
        lr1 = msgpack.unpackb(raw1, raw=False)["last_resolved"]

        time.sleep(0.05)
        item.save()

        raw2 = popoto.POPOTO_REDIS_DB.hget(pressure_key, item.db_key.redis_key)
        lr2 = msgpack.unpackb(raw2, raw=False)["last_resolved"]
        assert lr1 == lr2

    def test_save_no_cycles_no_hash(self):
        """CyclicDecayField with empty cycles removes stale cycle data."""
        item = CyclicItem.create(name="no_cycles")
        field = CyclicItem._meta.fields["relevance"]
        cycles_key = field.get_cycles_hash_key(item, "relevance")
        raw = popoto.POPOTO_REDIS_DB.hget(cycles_key, item.db_key.redis_key)
        assert raw is None


# --- Delete behavior tests ---


class TestCyclicDecayFieldDelete:
    """Test that on_delete() cleans up companion hashes."""

    def setup_method(self):
        CyclicFull.delete_all()

    def teardown_method(self):
        CyclicFull.delete_all()

    def test_delete_cleans_cycles_hash(self):
        item = CyclicFull.create(name="del_cycles")
        field = CyclicFull._meta.fields["relevance"]
        cycles_key = field.get_cycles_hash_key(item, "relevance")
        member_key = item.db_key.redis_key

        # Verify data exists
        assert popoto.POPOTO_REDIS_DB.hget(cycles_key, member_key) is not None

        item.delete()
        assert popoto.POPOTO_REDIS_DB.hget(cycles_key, member_key) is None

    def test_delete_cleans_pressure_hash(self):
        item = CyclicFull.create(name="del_pressure")
        field = CyclicFull._meta.fields["relevance"]
        pressure_key = field.get_pressure_hash_key(item, "relevance")
        member_key = item.db_key.redis_key

        assert popoto.POPOTO_REDIS_DB.hget(pressure_key, member_key) is not None

        item.delete()
        assert popoto.POPOTO_REDIS_DB.hget(pressure_key, member_key) is None


# --- top_by_decay() with cyclic resonance ---


class TestTopByDecayCyclic:
    """Test cyclic resonance ranking."""

    def setup_method(self):
        CyclicWithCycles.delete_all()

    def teardown_method(self):
        CyclicWithCycles.delete_all()

    def test_yearly_cycle_peak(self):
        """A yearly cycle should peak at the phase time and trough 6 months later."""
        now = time.time()
        field = CyclicWithCycles._meta.fields["relevance"]

        # Create two items at the same decay age
        peak_item = CyclicWithCycles.create(name="peak")
        trough_item = CyclicWithCycles.create(name="trough")

        # Set both to same timestamp (1 day ago)
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicWithCycles, "relevance")
        one_day_ago = now - 86400
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                peak_item.db_key.redis_key: one_day_ago,
                trough_item.db_key.redis_key: one_day_ago,
            },
        )

        # Set peak_item cycles with phase = now (peak now)
        # Set trough_item cycles with phase = now - half_year (trough now)
        cycles_key = field.get_cycles_hash_key(peak_item, "relevance")
        half_year = TemporalPeriod.YEARLY / 2

        # Peak cycle: phase aligns so cos(2*pi*(now-phase)/period) = 1
        peak_cycles = [[TemporalPeriod.YEARLY, 5.0, now]]
        # Trough cycle: phase shifted so cos = -1
        trough_cycles = [[TemporalPeriod.YEARLY, 5.0, now - half_year]]

        popoto.POPOTO_REDIS_DB.hset(
            cycles_key, peak_item.db_key.redis_key, msgpack.packb(peak_cycles)
        )
        popoto.POPOTO_REDIS_DB.hset(
            cycles_key, trough_item.db_key.redis_key, msgpack.packb(trough_cycles)
        )

        results = CyclicWithCycles.query.top_by_decay("relevance", n=10)
        assert len(results) == 2
        assert results[0].name == "peak"

    def test_empty_set_returns_empty(self):
        results = CyclicWithCycles.query.top_by_decay("relevance", n=10)
        assert results == []


# --- top_by_decay() with pressure ---


class TestTopByDecayPressure:
    """Test homeostatic pressure ranking."""

    def setup_method(self):
        CyclicWithPressure.delete_all()

    def teardown_method(self):
        CyclicWithPressure.delete_all()

    def test_pressure_increases_over_time(self):
        """Item with longer unresolved time ranks higher due to pressure."""
        now = time.time()
        field = CyclicWithPressure._meta.fields["relevance"]

        old_item = CyclicWithPressure.create(name="old_pressure")
        new_item = CyclicWithPressure.create(name="new_pressure")

        # Set both to same decay timestamp (1 day ago)
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicWithPressure, "relevance")
        one_day_ago = now - 86400
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {
                old_item.db_key.redis_key: one_day_ago,
                new_item.db_key.redis_key: one_day_ago,
            },
        )

        # Set old_item pressure to have been unresolved for 30 days
        pressure_key = field.get_pressure_hash_key(old_item, "relevance")
        old_pressure = {"rate": 0.1, "last_resolved": now - 86400 * 30}
        new_pressure = {"rate": 0.1, "last_resolved": now}

        popoto.POPOTO_REDIS_DB.hset(
            pressure_key,
            old_item.db_key.redis_key,
            msgpack.packb(old_pressure),
        )
        popoto.POPOTO_REDIS_DB.hset(
            pressure_key,
            new_item.db_key.redis_key,
            msgpack.packb(new_pressure),
        )

        results = CyclicWithPressure.query.top_by_decay("relevance", n=10)
        assert len(results) == 2
        # old_item has 30 days * 0.1 = 3.0 pressure boost
        assert results[0].name == "old_pressure"

    def test_pressure_resets_after_resolve(self):
        """resolve_pressure() discharges accumulated urgency."""
        now = time.time()
        field = CyclicWithPressure._meta.fields["relevance"]

        item = CyclicWithPressure.create(name="resolve_test")
        pressure_key = field.get_pressure_hash_key(item, "relevance")

        # Set old last_resolved
        old_pressure = {"rate": 0.1, "last_resolved": now - 86400 * 30}
        popoto.POPOTO_REDIS_DB.hset(
            pressure_key,
            item.db_key.redis_key,
            msgpack.packb(old_pressure),
        )

        # Resolve pressure
        item.resolve_pressure("relevance")

        # Verify last_resolved was updated
        raw = popoto.POPOTO_REDIS_DB.hget(pressure_key, item.db_key.redis_key)
        decoded = msgpack.unpackb(raw, raw=False)
        assert abs(decoded["last_resolved"] - time.time()) < 2.0


# --- Three-force superposition ---


class TestThreeForcesSuperposition:
    """Verify three-force computation against hand-computed values."""

    def setup_method(self):
        CyclicFull.delete_all()

    def teardown_method(self):
        CyclicFull.delete_all()

    def test_known_values(self):
        """Verify effective_score = decay + cyclic + pressure.

        Setup:
        - decay_rate=0.5, 1 day old -> decay = 1.0 * 1^(-0.5) = 1.0
        - yearly cycle, amplitude=5.0, phase=now -> cos(0) = 1 -> cyclic = 5.0
        - pressure_rate=0.1, 10 days unresolved -> pressure = 0.1 * 10 = 1.0
        - Expected effective_score = 1.0 + 5.0 + 1.0 = 7.0
        """
        now = time.time()
        field = CyclicFull._meta.fields["relevance"]

        item = CyclicFull.create(name="three_force")

        # Set decay timestamp to 1 day ago
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicFull, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {item.db_key.redis_key: now - 86400},
        )

        # Set cycles: yearly, amplitude 5.0, phase=now (cos(0)=1)
        cycles_key = field.get_cycles_hash_key(item, "relevance")
        popoto.POPOTO_REDIS_DB.hset(
            cycles_key,
            item.db_key.redis_key,
            msgpack.packb([[TemporalPeriod.YEARLY, 5.0, now]]),
        )

        # Set pressure: rate=0.1, 10 days unresolved
        pressure_key = field.get_pressure_hash_key(item, "relevance")
        popoto.POPOTO_REDIS_DB.hset(
            pressure_key,
            item.db_key.redis_key,
            msgpack.packb({"rate": 0.1, "last_resolved": now - 86400 * 10}),
        )

        # Run the Lua script directly to get the raw score
        cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicFull, "relevance"
        )
        pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
            CyclicFull, "relevance"
        )

        result = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            3,
            ss_key.redis_key,
            cycles_hash_key,
            pressure_hash_key,
            str(now),
            "0.5",
            "10",
            "",
        )

        assert len(result) == 2
        score = float(result[1])
        # Expected: 1.0 (decay) + 5.0 (cyclic) + 1.0 (pressure) = 7.0
        assert abs(score - 7.0) < 0.1, f"Expected ~7.0, got {score}"


# --- Equivalence with DecayingSortedField ---


class TestEquivalenceWithDecaySortedField:
    """CyclicDecayField with no cycles and pressure=0 matches DecayingSortedField."""

    def setup_method(self):
        CyclicItem.delete_all()
        PlainDecayItem.delete_all()

    def teardown_method(self):
        CyclicItem.delete_all()
        PlainDecayItem.delete_all()

    def test_same_ranking(self):
        """Both fields produce the same ranking for identical data."""
        now = time.time()

        # Create items in both models
        cyclic_a = CyclicItem.create(name="a")
        cyclic_b = CyclicItem.create(name="b")
        plain_a = PlainDecayItem.create(name="a")
        plain_b = PlainDecayItem.create(name="b")

        # Backdate identically — use actual field class for correct key prefix
        cyclic_ss = CyclicDecayField.get_sortedset_db_key(CyclicItem, "relevance")
        plain_ss = DecayingSortedField.get_sortedset_db_key(PlainDecayItem, "relevance")

        popoto.POPOTO_REDIS_DB.zadd(
            cyclic_ss.redis_key,
            {
                cyclic_a.db_key.redis_key: now - 86400 * 10,
                cyclic_b.db_key.redis_key: now - 86400 * 1,
            },
        )
        popoto.POPOTO_REDIS_DB.zadd(
            plain_ss.redis_key,
            {
                plain_a.db_key.redis_key: now - 86400 * 10,
                plain_b.db_key.redis_key: now - 86400 * 1,
            },
        )

        cyclic_results = CyclicItem.query.top_by_decay("relevance", n=10)
        plain_results = PlainDecayItem.query.top_by_decay("relevance", n=10)

        assert len(cyclic_results) == 2
        assert len(plain_results) == 2
        assert cyclic_results[0].name == plain_results[0].name
        assert cyclic_results[1].name == plain_results[1].name


# --- Partitioned CyclicDecayField ---


class TestPartitionedCyclic:
    """Test partition_by with CyclicDecayField."""

    def setup_method(self):
        PartitionedCyclic.delete_all()

    def teardown_method(self):
        PartitionedCyclic.delete_all()

    def test_partitioned_query(self):
        PartitionedCyclic.create(name="a1", category="A")
        PartitionedCyclic.create(name="b1", category="B")

        results = PartitionedCyclic.query.filter(category="A").top_by_decay(
            "relevance", n=10
        )
        assert len(results) == 1
        assert results[0].name == "a1"

    def test_partitioned_missing_filter_raises(self):
        PartitionedCyclic.create(name="x1", category="X")
        with pytest.raises(QueryException):
            PartitionedCyclic.query.top_by_decay("relevance", n=10)


# --- resolve_pressure() tests ---


class TestResolvePressure:
    """Test Model.resolve_pressure() method."""

    def setup_method(self):
        CyclicWithPressure.delete_all()
        CyclicItem.delete_all()

    def teardown_method(self):
        CyclicWithPressure.delete_all()
        CyclicItem.delete_all()

    def test_resolve_updates_last_resolved(self):
        item = CyclicWithPressure.create(name="resolve_1")
        result = item.resolve_pressure("relevance")
        assert abs(result - time.time()) < 2.0

    def test_resolve_wrong_field_type_raises(self):
        item = PlainDecayItem.create(name="bad_resolve")
        with pytest.raises(TypeError):
            item.resolve_pressure("relevance")
        item.delete()

    def test_resolve_no_pressure_raises(self):
        """resolve_pressure on CyclicDecayField with pressure_rate=0 raises TypeError."""
        item = CyclicItem.create(name="no_pressure")
        with pytest.raises(TypeError):
            item.resolve_pressure("relevance")

    def test_resolve_unsaved_raises(self):
        item = CyclicWithPressure(name="unsaved_resolve")
        with pytest.raises(TypeError):
            item.resolve_pressure("relevance")

    def test_resolve_nonexistent_field_raises(self):
        item = CyclicWithPressure.create(name="no_field_resolve")
        with pytest.raises(AttributeError):
            item.resolve_pressure("nonexistent")

    def test_resolve_with_pipeline(self):
        item = CyclicWithPressure.create(name="pipeline_resolve")
        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        result = item.resolve_pressure("relevance", pipeline=pipe)
        assert result is pipe
        pipe.execute()

        field = CyclicWithPressure._meta.fields["relevance"]
        pressure_key = field.get_pressure_hash_key(item, "relevance")
        raw = popoto.POPOTO_REDIS_DB.hget(pressure_key, item.db_key.redis_key)
        decoded = msgpack.unpackb(raw, raw=False)
        assert abs(decoded["last_resolved"] - time.time()) < 2.0


# --- Nil companion hash handling ---


class TestNilCompanionHash:
    """Test that Lua handles missing companion hashes gracefully."""

    def setup_method(self):
        CyclicWithCycles.delete_all()

    def teardown_method(self):
        CyclicWithCycles.delete_all()

    def test_members_without_companion_data(self):
        """Members saved before CyclicDecayField migration rank by pure decay."""
        now = time.time()
        field = CyclicWithCycles._meta.fields["relevance"]

        # Create item normally (which stores companion data)
        item = CyclicWithCycles.create(name="has_data")

        # Manually add a member to sorted set without companion hashes
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicWithCycles, "relevance")
        fake_key = "CyclicWithCycles:orphan_member"
        popoto.POPOTO_REDIS_DB.zadd(ss_key.redis_key, {fake_key: now - 86400})

        # Should not crash — orphan member gets pure decay score
        cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicWithCycles, "relevance"
        )
        pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
            CyclicWithCycles, "relevance"
        )

        result = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            3,
            ss_key.redis_key,
            cycles_hash_key,
            pressure_hash_key,
            str(now),
            "0.5",
            "10",
            "",
        )
        # Should return both members without error
        assert len(result) == 4  # 2 members * 2 (key + score)

        # Cleanup fake key
        popoto.POPOTO_REDIS_DB.zrem(ss_key.redis_key, fake_key)


# --- Performance benchmarks ---


class TestCyclicBenchmarks:
    """Benchmark top_by_decay on larger sorted sets with cyclic+pressure."""

    def setup_method(self):
        CyclicFull.delete_all()

    def teardown_method(self):
        CyclicFull.delete_all()

    def test_1k_members(self):
        """Extended Lua handles 1K members with cycle+pressure data."""
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicFull, "relevance")
        cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicFull, "relevance"
        )
        pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
            CyclicFull, "relevance"
        )
        now = time.time()

        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        members = {}
        for i in range(1000):
            redis_key = f"CyclicFull:bench1k_{i}"
            members[redis_key] = now - (i * 3600)
            pipe.hset(
                cycles_hash_key,
                redis_key,
                msgpack.packb([[TemporalPeriod.YEARLY, 5.0, 0]]),
            )
            pipe.hset(
                pressure_hash_key,
                redis_key,
                msgpack.packb({"rate": 0.1, "last_resolved": now - 86400 * i}),
            )
        pipe.zadd(ss_key.redis_key, members)
        pipe.execute()

        start = time.time()
        result = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            3,
            ss_key.redis_key,
            cycles_hash_key,
            pressure_hash_key,
            str(now),
            "0.5",
            "10",
            "",
        )
        elapsed = time.time() - start

        assert len(result) == 20  # 10 items * 2
        assert elapsed < 2.0, f"1K members took {elapsed:.3f}s (expected < 2s)"

        # Cleanup
        popoto.POPOTO_REDIS_DB.delete(ss_key.redis_key)
        popoto.POPOTO_REDIS_DB.delete(cycles_hash_key)
        popoto.POPOTO_REDIS_DB.delete(pressure_hash_key)

    def test_10k_members(self):
        """Extended Lua handles 10K members with cycle+pressure data."""
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicFull, "relevance")
        cycles_hash_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicFull, "relevance"
        )
        pressure_hash_key = CyclicDecayField.get_pressure_hash_key_from_parts(
            CyclicFull, "relevance"
        )
        now = time.time()

        pipe = popoto.POPOTO_REDIS_DB.pipeline()
        members = {}
        for i in range(10000):
            redis_key = f"CyclicFull:bench10k_{i}"
            members[redis_key] = now - (i * 360)
            pipe.hset(
                cycles_hash_key,
                redis_key,
                msgpack.packb([[TemporalPeriod.YEARLY, 5.0, 0]]),
            )
            pipe.hset(
                pressure_hash_key,
                redis_key,
                msgpack.packb({"rate": 0.1, "last_resolved": now - 86400}),
            )
        pipe.zadd(ss_key.redis_key, members)
        pipe.execute()

        start = time.time()
        result = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            3,
            ss_key.redis_key,
            cycles_hash_key,
            pressure_hash_key,
            str(now),
            "0.5",
            "10",
            "",
        )
        elapsed = time.time() - start

        assert len(result) == 20
        assert elapsed < 10.0, f"10K members took {elapsed:.3f}s (expected < 10s)"

        # Cleanup
        popoto.POPOTO_REDIS_DB.delete(ss_key.redis_key)
        popoto.POPOTO_REDIS_DB.delete(cycles_hash_key)
        popoto.POPOTO_REDIS_DB.delete(pressure_hash_key)


# --- Export tests ---


class TestExports:
    """Test package exports."""

    def test_cyclic_decay_field_in_all(self):
        assert "CyclicDecayField" in popoto.__all__

    def test_temporal_period_in_all(self):
        assert "TemporalPeriod" in popoto.__all__
