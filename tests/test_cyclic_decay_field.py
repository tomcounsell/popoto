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
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.cyclic_decay_field import CyclicDecayField, CYCLIC_DECAY_LUA
from src.popoto.fields.decaying_sorted_field import (
    DecayingSortedField,
    DECAY_SCORE_LUA,
    confidence_modulation_args,
)
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


# --- Confidence-modulated decay models (#491) ---


class CyclicConfItem(popoto.Model):
    """No cycles / no pressure, but auto-detects ``certainty`` for modulation."""

    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(decay_rate=0.5)
    certainty = ConfidenceField()


class CyclicConfWithCycles(popoto.Model):
    """Cycles data AND modulation: the KEYS[2]/KEYS[4] anti-corruption model."""

    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
    )
    certainty = ConfidenceField()


class CyclicConfFull(popoto.Model):
    """Cycles + pressure + modulation: all four KEYS bound at once."""

    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.YEARLY, 5.0, 0)],
        pressure_rate=0.1,
    )
    certainty = ConfidenceField()


class PlainConfItem(popoto.Model):
    """DecayingSortedField twin of ``CyclicConfItem`` for the equivalence test."""

    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()


ALL_MODELS = [
    CyclicItem,
    CyclicWithCycles,
    CyclicWithPressure,
    CyclicFull,
    CyclicWithBase,
    PartitionedCyclic,
    PlainDecayItem,
    CyclicConfItem,
    CyclicConfWithCycles,
    CyclicConfFull,
    PlainConfItem,
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


# --- Confidence-modulation helpers (#491) ---


def _decode_scores(raw):
    """Raw Lua output -> {member: raw score string} (Lua's own tostring)."""
    decoded = [x.decode() if isinstance(x, bytes) else x for x in (raw or [])]
    return {decoded[i]: decoded[i + 1] for i in range(0, len(decoded), 2)}


def _plant_confidence(record, confidence, evidence_count=10):
    field = type(record)._meta.fields["certainty"]
    popoto.POPOTO_REDIS_DB.hset(
        field.get_data_hash_key(record, "certainty"),
        record.db_key.redis_key,
        msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": evidence_count,
                "corroborations": evidence_count,
                "contradictions": 0,
            },
            use_bin_type=True,
        ),
    )


def _modulated_eval(model_class, now, n=50, disable=False, cycles_key=None):
    """Run the right decay script for ``model_class`` with modulation wired.

    Mirrors ``query.py``: the cyclic script gets numkeys 4 with the confidence
    hash appended AFTER cycles (KEYS[2]) and pressure (KEYS[3]); the plain
    script gets numkeys 2 with the confidence hash at KEYS[2]. ``disable``
    reproduces the pre-#491 call shape as a byte-exact oracle.
    """
    field = model_class._meta.fields["relevance"]
    ss_key = field.__class__.get_sortedset_db_key(model_class, "relevance").redis_key
    conf_key, s, c0 = confidence_modulation_args(
        model_class, field, "relevance", filters={}
    )
    if disable:
        conf_key, s = "", "0"
    args = [
        str(now),
        str(field.decay_rate),
        str(n),
        field.base_score_field or "",
        s,
        c0,
    ]
    if isinstance(field, CyclicDecayField):
        if cycles_key is None:
            cycles_key = CyclicDecayField.get_cycles_hash_key_from_parts(
                model_class, "relevance"
            )
        raw = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            4,
            ss_key,
            cycles_key,
            CyclicDecayField.get_pressure_hash_key_from_parts(model_class, "relevance"),
            conf_key,
            *args,
        )
    else:
        raw = popoto.POPOTO_REDIS_DB.eval(DECAY_SCORE_LUA, 2, ss_key, conf_key, *args)
    return _decode_scores(raw)


def _modulated_scores_by_name(model_class, now, **kwargs):
    """``{record.name: raw score string}`` for the modulated EVAL."""
    key_to_name = {r.db_key.redis_key: r.name for r in model_class.query.all()}
    return {
        key_to_name[k]: v
        for k, v in _modulated_eval(model_class, now, **kwargs).items()
    }


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

    def test_same_ranking_with_confidence_modulation(self):
        """Equivalence must survive #491: the forked CYCLIC_DECAY_LUA carries a
        copy of the decay math, so a half-edit would show up as cyclic and
        plain disagreeing under identical confidence evidence.

        Scores are compared byte-for-byte, not just by rank order — the two
        scripts must compute the *same number*, not merely the same ordering.
        """
        CyclicConfItem.delete_all()
        PlainConfItem.delete_all()
        for model in (CyclicConfItem, PlainConfItem):
            model._meta.fields["relevance"]._confidence_modulation_cache.clear()

        now = time.time()
        confidences = {"a": 0.95, "b": 0.05}
        records = {}
        for model in (CyclicConfItem, PlainConfItem):
            zscores = {}
            for name, confidence in confidences.items():
                rec = model.create(name=name)
                records[(model, name)] = rec
                zscores[rec.db_key.redis_key] = now - 86400 * 10
                conf_field = model._meta.fields["certainty"]
                popoto.POPOTO_REDIS_DB.hset(
                    conf_field.get_data_hash_key(rec, "certainty"),
                    rec.db_key.redis_key,
                    msgpack.packb(
                        {
                            "confidence": confidence,
                            "evidence_count": 10,
                            "corroborations": 10,
                            "contradictions": 0,
                        },
                        use_bin_type=True,
                    ),
                )
            ss = model._meta.fields["relevance"].__class__.get_sortedset_db_key(
                model, "relevance"
            )
            popoto.POPOTO_REDIS_DB.zadd(ss.redis_key, zscores)

        cyclic_results = CyclicConfItem.query.top_by_decay("relevance", n=10)
        plain_results = PlainConfItem.query.top_by_decay("relevance", n=10)

        # Modulation is live and non-trivial: high confidence wins in both.
        assert [r.name for r in cyclic_results] == ["a", "b"]
        assert [r.name for r in plain_results] == ["a", "b"]

        # Byte-identical scores, evaluated at the same `now` in both scripts.
        assert _modulated_scores_by_name(
            CyclicConfItem, now
        ) == _modulated_scores_by_name(PlainConfItem, now)


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


# --- Deterministic tie-ordering (issue #448) ---


class TestCyclicTieOrdering:
    """Regression tests for deterministic tie-breaking (issue #448).

    Equal effective-scored members must return in member key (redis_key)
    ascending order, byte-wise, broken inside the Lua script -- independent
    of insertion order, repeatable across runs, and stable across the ``n``
    truncation boundary. ``CyclicItem`` uses the default ``cycles=[]`` /
    ``pressure_rate=0.0``, so no companion data is written and
    ``effective_score == decayed`` (the tie collapses to identical scores).
    Mirrors ``TestBM25TieOrdering`` (#446) and ``TestDecayTieOrdering``.
    """

    NAMES = ["tie_a", "tie_b", "tie_c", "tie_d", "tie_e"]

    def setup_method(self):
        CyclicItem.delete_all()

    def _plant_tied(self, names=None):
        """Create identical-base members, then plant one shared timestamp.

        Created in reversed (non-ascending) key order so a lucky insertion
        order cannot masquerade as a correct tie-break.
        """
        names = names or self.NAMES
        keys = {}
        for name in reversed(names):
            keys[name] = CyclicItem.create(name=name).db_key.redis_key
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicItem, "relevance")
        shared_ts = time.time() - 86400  # 1 day ago, identical for every member
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key, {keys[name]: shared_ts for name in names}
        )
        return sorted(keys.values())  # byte-wise ascending == expected order

    def _scores_via_lua(self, n=10):
        """Return the raw effective score strings the Lua script emits."""
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicItem, "relevance")
        cycles_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicItem, "relevance"
        )
        pressure_key = CyclicDecayField.get_pressure_hash_key_from_parts(
            CyclicItem, "relevance"
        )
        raw = popoto.POPOTO_REDIS_DB.eval(
            CYCLIC_DECAY_LUA,
            3,
            ss_key.redis_key,
            cycles_key,
            pressure_key,
            str(time.time()),
            "0.5",
            str(n),
            "",
        )
        decoded = [x.decode() if isinstance(x, bytes) else x for x in raw]
        return [decoded[i + 1] for i in range(0, len(decoded), 2)]

    def test_scores_are_actually_tied(self):
        """All planted members share exactly one score (tie path exercised)."""
        self._plant_tied()
        scores = self._scores_via_lua()
        assert len(scores) == len(self.NAMES)
        assert len(set(scores)) == 1

    def test_tie_order_key_ascending_insertion_independent(self):
        """Tied members return key-ascending regardless of insertion order."""
        expected = self._plant_tied()
        results = CyclicItem.query.top_by_decay("relevance", n=10)
        assert [r.db_key.redis_key for r in results] == expected

    def test_repeated_calls_identical(self):
        """The same query returns the identical ordered list every run."""
        self._plant_tied()
        first = [
            r.db_key.redis_key for r in CyclicItem.query.top_by_decay("relevance", n=10)
        ]
        assert len(first) == len(self.NAMES)
        for _ in range(9):
            again = [
                r.db_key.redis_key
                for r in CyclicItem.query.top_by_decay("relevance", n=10)
            ]
            assert again == first

    def test_deterministic_truncation_at_n(self):
        """With 5 tied members and n=3, exactly the 3 lowest keys return."""
        expected = self._plant_tied()
        results = CyclicItem.query.top_by_decay("relevance", n=3)
        assert [r.db_key.redis_key for r in results] == expected[:3]


# --- Confidence modulation on the forked cyclic script (#491) ---


class TestCyclicConfidenceKeysRegression:
    """The KEYS[2] vs KEYS[4] anti-corruption suite.

    ``CYCLIC_DECAY_LUA`` already binds KEYS[2] = cycles and KEYS[3] = pressure,
    so its confidence hash MUST be KEYS[4]. A mix-up does not raise: reading
    the cycles hash as confidence unpacks an array-of-arrays, whose first
    element is a table rather than a number, so ``c`` silently stays at ``c0``
    and modulation goes quietly inert. Nothing errors and no unmodulated test
    notices.

    The only thing that catches it is asserting modulation has a REAL effect on
    a corpus that also carries cycles data -- which is what these tests do.
    """

    AGED_DAYS = 30

    def setup_method(self):
        for model in (CyclicConfWithCycles, CyclicConfFull):
            model.delete_all()
            model._meta.fields["relevance"]._confidence_modulation_cache.clear()

    def teardown_method(self):
        for model in (CyclicConfWithCycles, CyclicConfFull):
            model.delete_all()

    def _corpus(self, model_class):
        """Two identically-aged records with cycles data and opposed evidence."""
        now = time.time()
        records = {}
        zscores = {}
        for name, confidence in (("high", 0.95), ("low", 0.05)):
            rec = model_class.create(name=name)
            records[name] = rec
            _plant_confidence(rec, confidence)
            zscores[rec.db_key.redis_key] = now - 86400 * self.AGED_DAYS
        ss_key = CyclicDecayField.get_sortedset_db_key(model_class, "relevance")
        popoto.POPOTO_REDIS_DB.zadd(ss_key.redis_key, zscores)
        return now, records

    def test_cycles_data_is_actually_present(self):
        """Guard the guard: without cycles on disk this suite proves nothing."""
        now, records = self._corpus(CyclicConfWithCycles)
        cycles_key = CyclicDecayField.get_cycles_hash_key_from_parts(
            CyclicConfWithCycles, "relevance"
        )
        for rec in records.values():
            assert popoto.POPOTO_REDIS_DB.hget(cycles_key, rec.db_key.redis_key)

        # And the cycle term genuinely moves the score. Compared against the
        # same EVAL with an empty cycles hash rather than a hand-computed
        # constant: the yearly resonance depends on the absolute calendar date,
        # so any fixed expected value would be date-flaky.
        with_cycles = _modulated_scores_by_name(CyclicConfWithCycles, now)
        without = _modulated_scores_by_name(
            CyclicConfWithCycles, now, cycles_key="_test:no_such_cycles_hash"
        )
        assert with_cycles != without

    def test_modulation_is_live_when_cycles_data_is_present(self):
        """The headline anti-corruption assertion.

        If the confidence hash were read from KEYS[2], both members would
        decode the cycles array, fall back to c0, and tie exactly.
        """
        now, _ = self._corpus(CyclicConfWithCycles)
        scores = _modulated_scores_by_name(CyclicConfWithCycles, now)
        assert scores["high"] != scores["low"], (
            "cycles-bearing corpus is not modulated -- confidence is probably "
            "being read from the cycles hash (KEYS[2]) instead of KEYS[4]"
        )
        assert float(scores["high"]) > float(scores["low"])

    def test_ranking_through_top_by_decay_with_cycles(self):
        """Same claim through the real query path (numkeys 4 at the EVAL site)."""
        self._corpus(CyclicConfWithCycles)
        ranked = CyclicConfWithCycles.query.top_by_decay("relevance", n=10)
        assert [r.name for r in ranked] == ["high", "low"]

    def test_all_four_keys_bound_at_once(self):
        """Cycles + pressure + confidence together still rank correctly."""
        now, _ = self._corpus(CyclicConfFull)
        scores = _modulated_scores_by_name(CyclicConfFull, now)
        assert float(scores["high"]) > float(scores["low"])
        ranked = CyclicConfFull.query.top_by_decay("relevance", n=10)
        assert [r.name for r in ranked] == ["high", "low"]

    def test_cycles_and_pressure_survive_modulation_unchanged(self):
        """Turning modulation off must leave the cyclic/pressure terms intact.

        Equal-confidence members are bit-exactly identical with modulation on
        and off, which is only true if KEYS[2]/KEYS[3] still resolve to the
        cycles and pressure hashes after the confidence key was appended.
        """
        now, records = self._corpus(CyclicConfFull)
        for rec in records.values():
            _plant_confidence(rec, 0.5)  # == initial_confidence => neutral

        assert _modulated_scores_by_name(CyclicConfFull, now) == (
            _modulated_scores_by_name(CyclicConfFull, now, disable=True)
        )


class TestCyclicTieOrderingWithConfidence:
    """Mirrors ``TestDecayTieOrderingWithConfidence`` on the cyclic script.

    The unmodulated ``TestCyclicTieOrdering`` above is the regression oracle
    and is left untouched.
    """

    NAMES = ["tie_a", "tie_b", "tie_c", "tie_d", "tie_e"]
    AGED_DAYS = 30

    def setup_method(self):
        CyclicConfItem.delete_all()
        CyclicConfItem._meta.fields["relevance"]._confidence_modulation_cache.clear()

    def teardown_method(self):
        CyclicConfItem.delete_all()

    def _plant_tied(self):
        """Create in reversed key order, then plant one shared age."""
        records = {}
        for name in reversed(self.NAMES):
            records[name] = CyclicConfItem.create(name=name)
        ss_key = CyclicDecayField.get_sortedset_db_key(CyclicConfItem, "relevance")
        shared_ts = time.time() - 86400 * self.AGED_DAYS
        popoto.POPOTO_REDIS_DB.zadd(
            ss_key.redis_key,
            {records[n].db_key.redis_key: shared_ts for n in self.NAMES},
        )
        return records

    def test_no_confidence_members_remain_bit_exactly_tied(self):
        self._plant_tied()
        scores = _modulated_scores_by_name(CyclicConfItem, time.time())
        assert len(scores) == len(self.NAMES)
        assert len(set(scores.values())) == 1

    def test_untouched_tie_order_is_still_key_ascending(self):
        records = self._plant_tied()
        expected = sorted(r.db_key.redis_key for r in records.values())
        results = CyclicConfItem.query.top_by_decay("relevance", n=10)
        assert [r.db_key.redis_key for r in results] == expected

    def test_differing_confidence_members_stop_tying(self):
        records = self._plant_tied()
        for name, confidence in zip(self.NAMES, [0.05, 0.25, 0.5, 0.75, 0.95]):
            _plant_confidence(records[name], confidence)

        scores = _modulated_scores_by_name(CyclicConfItem, time.time())
        assert len(set(scores.values())) == len(self.NAMES)
        results = CyclicConfItem.query.top_by_decay("relevance", n=10)
        assert [r.name for r in results] == list(reversed(self.NAMES))

    def test_equal_confidence_members_still_tie_and_break_by_key(self):
        records = self._plant_tied()
        for name in self.NAMES:
            _plant_confidence(records[name], 0.05)

        scores = _modulated_scores_by_name(CyclicConfItem, time.time())
        assert len(set(scores.values())) == 1
        expected = sorted(r.db_key.redis_key for r in records.values())
        results = CyclicConfItem.query.top_by_decay("relevance", n=3)
        assert [r.db_key.redis_key for r in results] == expected[:3]


# --- Learned amplitude preservation (#679) ---


class CyclicLearned(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.DAILY, 2.0, 0)],
    )


class CyclicLearnedMulti(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[
            (TemporalPeriod.DAILY, 2.0, 0),
            (TemporalPeriod.WEEKLY, 3.0, 0),
        ],
    )


class CyclicLearnedDup(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[
            (TemporalPeriod.DAILY, 1.0, 0),
            (TemporalPeriod.DAILY, 4.0, 100),
        ],
    )


def _read_cycles(model_class, item):
    """Decode the stored cycles entry for one member, or None."""
    field = model_class._meta.fields["relevance"]
    key = field.get_cycles_hash_key(item, "relevance")
    raw = popoto.get_redis().hget(key, item.db_key.redis_key)
    return msgpack.unpackb(raw, raw=False) if raw else None


def _write_cycles_raw(model_class, item, payload):
    """Write raw bytes into the member's cycles entry."""
    field = model_class._meta.fields["relevance"]
    key = field.get_cycles_hash_key(item, "relevance")
    popoto.get_redis().hset(key, item.db_key.redis_key, payload)


class TestLearnedAmplitudePreservedOnSave:
    """on_save preserves learned amplitudes; period/phase stay declarative.

    Regression cover for #679: ``on_save`` used to rewrite the member's cycles
    entry from the class-level declaration on every save, silently erasing
    whatever ``strengthen_cycle`` / ``weaken_cycle`` had accumulated.
    """

    def setup_method(self):
        CyclicLearned.delete_all()
        CyclicLearnedMulti.delete_all()
        CyclicLearnedDup.delete_all()
        CyclicItem.delete_all()

    def teardown_method(self):
        CyclicLearned.delete_all()
        CyclicLearnedMulti.delete_all()
        CyclicLearnedDup.delete_all()
        CyclicItem.delete_all()

    # TC1 — the defect itself.
    def test_strengthened_amplitude_survives_resave(self):
        item = CyclicLearned.create(name="tc1")
        assert _read_cycles(CyclicLearned, item)[0][1] == 2.0

        item.strengthen_cycle("relevance", factor=1.5)
        assert _read_cycles(CyclicLearned, item)[0][1] == pytest.approx(3.0)

        item.save()

        stored = _read_cycles(CyclicLearned, item)
        assert stored[0][1] == pytest.approx(
            3.0
        ), "save() reset the learned amplitude to the class default — #679"

    def test_weakened_amplitude_survives_resave(self):
        item = CyclicLearned.create(name="tc1b")
        item.weaken_cycle("relevance", factor=0.5)
        assert _read_cycles(CyclicLearned, item)[0][1] == pytest.approx(1.0)

        item.save()
        assert _read_cycles(CyclicLearned, item)[0][1] == pytest.approx(1.0)

    def test_repeated_saves_do_not_drift(self):
        item = CyclicLearned.create(name="tc1c")
        item.strengthen_cycle("relevance", factor=2.0)
        for _ in range(5):
            item.save()
        assert _read_cycles(CyclicLearned, item)[0][1] == pytest.approx(4.0)

    # TC2/TC3 — nothing learned yet: declared defaults still win.
    def test_first_save_writes_declared_amplitudes(self):
        item = CyclicLearnedMulti.create(name="tc3")
        stored = _read_cycles(CyclicLearnedMulti, item)
        assert [c[1] for c in stored] == [2.0, 3.0]

    def test_second_save_without_learning_keeps_declared(self):
        item = CyclicLearnedMulti.create(name="tc3b")
        item.save()
        stored = _read_cycles(CyclicLearnedMulti, item)
        assert [c[1] for c in stored] == [2.0, 3.0]

    # TC4 — declaration is authoritative about WHICH cycles exist.
    def test_cycle_added_to_declaration_uses_declared_amplitude(self):
        item = CyclicLearned.create(name="tc4a")
        item.strengthen_cycle("relevance", factor=3.0)  # DAILY -> 6.0

        field = CyclicLearned._meta.fields["relevance"]
        original = field.cycles
        try:
            field.cycles = [
                (TemporalPeriod.DAILY, 2.0, 0),
                (TemporalPeriod.WEEKLY, 9.0, 0),
            ]
            item.save()
        finally:
            field.cycles = original

        stored = _read_cycles(CyclicLearned, item)
        by_period = {c[0]: c[1] for c in stored}
        assert by_period[TemporalPeriod.DAILY] == pytest.approx(6.0)
        assert by_period[TemporalPeriod.WEEKLY] == pytest.approx(9.0)

    def test_cycle_removed_from_declaration_is_dropped(self):
        item = CyclicLearnedMulti.create(name="tc4b")
        item.strengthen_cycle("relevance", factor=2.0)

        field = CyclicLearnedMulti._meta.fields["relevance"]
        original = field.cycles
        try:
            field.cycles = [(TemporalPeriod.DAILY, 2.0, 0)]
            item.save()
        finally:
            field.cycles = original

        stored = _read_cycles(CyclicLearnedMulti, item)
        assert [c[0] for c in stored] == [TemporalPeriod.DAILY]
        assert stored[0][1] == pytest.approx(4.0)

    # TC5 — declared parameters refresh; learned amplitude does not.
    def test_phase_refreshes_from_declaration_while_amplitude_persists(self):
        item = CyclicLearned.create(name="tc5")
        item.strengthen_cycle("relevance", factor=2.0)

        field = CyclicLearned._meta.fields["relevance"]
        original = field.cycles
        try:
            field.cycles = [(TemporalPeriod.DAILY, 2.0, 777)]
            item.save()
        finally:
            field.cycles = original

        stored = _read_cycles(CyclicLearned, item)[0]
        assert stored[2] == 777, "phase is declarative and must refresh"
        assert stored[1] == pytest.approx(4.0), "amplitude is learned"

    def test_duplicate_periods_pair_fifo_and_keep_order(self):
        item = CyclicLearnedDup.create(name="tc5b")
        stored = _read_cycles(CyclicLearnedDup, item)
        assert [c[1] for c in stored] == [1.0, 4.0]

        item.strengthen_cycle("relevance", factor=2.0)
        item.save()

        stored = _read_cycles(CyclicLearnedDup, item)
        assert [c[0] for c in stored] == [TemporalPeriod.DAILY] * 2
        assert [c[1] for c in stored] == [pytest.approx(2.0), pytest.approx(8.0)]
        assert [c[2] for c in stored] == [0, 100]

    # TC6 — the empty-cycles branch is unchanged.
    def test_empty_cycles_still_deletes_stale_entry(self):
        item = CyclicItem.create(name="tc6")
        field = CyclicItem._meta.fields["relevance"]
        key = field.get_cycles_hash_key(item, "relevance")
        popoto.get_redis().hset(
            key, item.db_key.redis_key, msgpack.packb([[86400, 5.0, 0]])
        )

        item.save()

        assert popoto.get_redis().hget(key, item.db_key.redis_key) is None

    # TC7 — corrupt stored state degrades to declared defaults, loudly.
    def test_corrupt_stored_entry_falls_back_to_declared(self, caplog):
        item = CyclicLearned.create(name="tc7")
        _write_cycles_raw(CyclicLearned, item, b"\xff\xfe not msgpack \x00")

        with caplog.at_level("WARNING", logger="POPOTO.CyclicDecayField"):
            item.save()  # must not raise

        stored = _read_cycles(CyclicLearned, item)
        assert stored[0][1] == 2.0
        assert any("Could not decode cycles" in r.message for r in caplog.records)

    def test_stored_entry_of_wrong_shape_falls_back_to_declared(self):
        item = CyclicLearned.create(name="tc7b")
        # Valid msgpack, wrong shape — a dict where a list of cycles belongs.
        _write_cycles_raw(CyclicLearned, item, msgpack.packb({"nope": 1}))

        item.save()

        assert _read_cycles(CyclicLearned, item)[0][1] == 2.0

    def test_unhashable_period_falls_back_instead_of_raising(self, caplog):
        """A payload can decode cleanly and still be unusable.

        The period slot here is itself a list, so keying the merge dict by it
        raises ``TypeError: unhashable type: 'list'``. That is a decode-time
        failure in every sense that matters, so it must take the same
        warn-and-fall-back path — not escape ``save()``. Distinguishes a guard
        around the decode alone from one around decode + normalization.
        """
        item = CyclicLearned.create(name="tc7c")
        _write_cycles_raw(
            CyclicLearned,
            item,
            msgpack.packb([[[TemporalPeriod.DAILY], 9.0, 0]], use_bin_type=True),
        )

        with caplog.at_level("WARNING", logger="POPOTO.CyclicDecayField"):
            item.save()  # must not raise

        assert _read_cycles(CyclicLearned, item)[0][1] == 2.0
        assert any("Could not decode cycles" in r.message for r in caplog.records)

    def test_partial_merge_discarded_when_a_later_entry_is_malformed(self):
        """A half-read payload contributes nothing, not something.

        The first entry is well-formed and the second is not. Carrying the
        first while the second raises would apply learned state to some
        cycles and declared defaults to others — a silently mixed record.
        Both declared cycles must fall back together.
        """
        item = CyclicLearnedMulti.create(name="tc7d")
        _write_cycles_raw(
            CyclicLearnedMulti,
            item,
            msgpack.packb(
                [
                    [TemporalPeriod.DAILY, 7.0, 0],
                    [[TemporalPeriod.WEEKLY], 8.0, 0],
                ],
                use_bin_type=True,
            ),
        )

        item.save()  # must not raise

        stored = {c[0]: c[1] for c in _read_cycles(CyclicLearnedMulti, item)}
        assert stored[TemporalPeriod.DAILY] == 2.0
        assert stored[TemporalPeriod.WEEKLY] == 3.0

    # TC8 — a pipelined save preserves too.
    def test_pipelined_save_preserves_learned_amplitude(self):
        item = CyclicLearned.create(name="tc8")
        item.strengthen_cycle("relevance", factor=2.5)

        pipe = popoto.get_redis().pipeline()
        item.save(pipeline=pipe)
        pipe.execute()

        assert _read_cycles(CyclicLearned, item)[0][1] == pytest.approx(5.0)

    # TC9 — the learned value is observable through ranking, not just bytes.
    def test_learned_amplitude_changes_ranking(self):
        field = CyclicLearned._meta.fields["relevance"]
        original = field.cycles
        # Pin phase to now so cos(2*pi*(now - phase)/period) ~= 1 and a larger
        # amplitude deterministically means a larger effective score.
        phase = int(time.time())
        # Names are chosen so the equal-score tie-break (ascending key) puts the
        # strengthened item LAST. If the learned amplitude were lost, the two
        # scores would tie and "tc9_aaa_weak" would win — so this assertion
        # cannot be satisfied by the tie-break alone.
        try:
            field.cycles = [(TemporalPeriod.DAILY, 1.0, phase)]
            weak = CyclicLearned.create(name="tc9_aaa_weak")
            strong = CyclicLearned.create(name="tc9_zzz_strong")

            strong.strengthen_cycle("relevance", factor=50.0)
            strong.save()

            results = CyclicLearned.query.top_by_decay("relevance", n=2)
        finally:
            field.cycles = original

        assert [r.name for r in results] == [
            "tc9_zzz_strong",
            "tc9_aaa_weak",
        ], "learned amplitude did not reach the Lua scoring path"
        assert _read_cycles(CyclicLearned, weak)[0][1] == 1.0

    def test_reordered_declaration_pairs_by_period_not_position(self):
        """Stored amplitudes follow their period, not their slot index.

        Distinguishes period-keyed matching from naive positional matching:
        the two survive every other test in this class identically.
        """
        item = CyclicLearnedMulti.create(name="tc5c")
        item.strengthen_cycle("relevance", factor=2.0)
        # stored is now DAILY=4.0, WEEKLY=6.0, in that order
        assert [c[1] for c in _read_cycles(CyclicLearnedMulti, item)] == [4.0, 6.0]

        field = CyclicLearnedMulti._meta.fields["relevance"]
        original = field.cycles
        try:
            # Same two cycles, declared in the opposite order.
            field.cycles = [
                (TemporalPeriod.WEEKLY, 3.0, 0),
                (TemporalPeriod.DAILY, 2.0, 0),
            ]
            item.save()
        finally:
            field.cycles = original

        stored = _read_cycles(CyclicLearnedMulti, item)
        assert [c[0] for c in stored] == [TemporalPeriod.WEEKLY, TemporalPeriod.DAILY]
        # Positional matching would give [4.0, 6.0] here; period matching gives
        # each period back its own learned amplitude.
        assert [c[1] for c in stored] == [pytest.approx(6.0), pytest.approx(4.0)]

    # TC10 — the cost claim in the plan's Risks table, enforced.
    def test_no_cycles_read_when_field_declares_no_cycles(self, monkeypatch):
        """Implementation-pinning: update deliberately on refactor.

        Asserts the ``if field.cycles:`` gate in ``on_save`` — a pressure-only
        CyclicDecayField must not pay a cycles HGET. Any refactor that keeps
        the cost contract but moves the read may legitimately rewrite this.
        """
        from src.popoto.fields import cyclic_decay_field as cdf

        real = cdf.get_REDIS_DB()
        calls = []

        class CountingClient:
            def __getattr__(self, attr):
                return getattr(real, attr)

            def hget(self, key, member):
                calls.append(key)
                return real.hget(key, member)

        monkeypatch.setattr(cdf, "get_REDIS_DB", lambda: CountingClient())

        CyclicItem.create(name="tc10").save()
        assert calls == [], f"unexpected cycles read for a cycles-less field: {calls}"

        monkeypatch.undo()
        item = CyclicLearned.create(name="tc10b")
        monkeypatch.setattr(cdf, "get_REDIS_DB", lambda: CountingClient())
        item.save()
        assert len(calls) == 1, "a declared-cycles field must read exactly once"

    # TC11 — Decision 2: a learned 0.0 is preserved, and is recoverable.
    def test_zero_amplitude_is_preserved_not_reset(self):
        item = CyclicLearned.create(name="tc11")
        item.weaken_cycle("relevance", factor=0.0001)  # snaps below 0.01 -> 0.0
        assert _read_cycles(CyclicLearned, item)[0][1] == 0.0

        item.save()

        assert (
            _read_cycles(CyclicLearned, item)[0][1] == 0.0
        ), "a deliberately silenced cycle must not be resurrected by a save"

    def test_deleting_hash_member_restores_declared_defaults(self):
        item = CyclicLearned.create(name="tc11b")
        item.weaken_cycle("relevance", factor=0.0001)
        assert _read_cycles(CyclicLearned, item)[0][1] == 0.0

        # The documented recovery path: drop the member, then re-save.
        field = CyclicLearned._meta.fields["relevance"]
        key = field.get_cycles_hash_key(item, "relevance")
        popoto.get_redis().hdel(key, item.db_key.redis_key)

        item.save()

        assert _read_cycles(CyclicLearned, item)[0][1] == 2.0


# --- Export tests ---


class TestExports:
    """Test package exports."""

    def test_cyclic_decay_field_in_all(self):
        assert "CyclicDecayField" in popoto.__all__

    def test_temporal_period_in_all(self):
        assert "TemporalPeriod" in popoto.__all__
