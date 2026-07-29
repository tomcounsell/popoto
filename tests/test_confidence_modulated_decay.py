"""Wiring tests for confidence-modulated decay (issue #491).

Covers the resolution layer and the three EVAL call sites that must agree:

- auto-detection of a single ConfidenceField (on / off / ambiguous)
- the explicit ``confidence_modulation_field`` kwarg (str / False / bad name)
- the deploy-level kill switch ``DECAY_CONFIDENCE_MODULATION_ENABLED``
- the partition-subset guard (QueryException with an actionable message)
- bit-exact neutrality on every disabled path

The comprehensive behavioural sweep (ranking effects, latency, cyclic parity)
lives in the decay suites; this file is about the wiring being *connected*.
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import msgpack
import pytest
from src import popoto
from src.popoto.exceptions import ModelException
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.constants import Defaults
from src.popoto.fields.decaying_sorted_field import (
    DECAY_SCORE_LUA,
    MODULATION_DISABLED,
    DecayingSortedField,
    confidence_modulation_args,
    resolve_confidence_modulation_field,
)
from src.popoto.models.query import QueryException
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class ModDecayNoConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)


class ModDecayOneConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()


class ModDecayTwoConfidences(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField()
    trust = ConfidenceField()


class ModDecayExplicitPick(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5, confidence_modulation_field="trust")
    certainty = ConfidenceField()
    trust = ConfidenceField()


class ModDecayOptedOut(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5, confidence_modulation_field=False)
    certainty = ConfidenceField()


class ModDecayBadName(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5, confidence_modulation_field="nope")
    certainty = ConfidenceField()


class ModDecayNotConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(
        decay_rate=0.5, confidence_modulation_field="importance"
    )
    importance = popoto.FloatField(default=1.0)


class ModDecayLowPrior(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField(initial_confidence=0.3)


class ModDecayPartitionedConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    project = popoto.KeyField(null=False)
    relevance = DecayingSortedField(decay_rate=0.5)
    certainty = ConfidenceField(partition_by="project")


ALL_MODELS = [
    ModDecayNoConfidence,
    ModDecayOneConfidence,
    ModDecayTwoConfidences,
    ModDecayExplicitPick,
    ModDecayOptedOut,
    ModDecayBadName,
    ModDecayNotConfidence,
    ModDecayLowPrior,
    ModDecayPartitionedConfidence,
]


@pytest.fixture(autouse=True)
def _clean():
    for model in ALL_MODELS:
        model.delete_all()
        # Resolution is cached per model class; clear so kill-switch and
        # ambiguity tests observe a fresh resolve.
        model._meta.fields["relevance"]._confidence_modulation_cache.clear()
    yield


def _backdate(model_instance, field_name, days):
    """Age a record by rewriting its ZSET score to now - days*86400."""
    field = model_instance._meta.fields[field_name]
    zkey = field.get_partitioned_sortedset_db_key(model_instance, field_name).redis_key
    POPOTO_REDIS_DB.zadd(
        zkey, {model_instance.db_key.redis_key: time.time() - 86400 * days}
    )


def _zkey(model_class, field_name="relevance"):
    field = model_class._meta.fields[field_name]
    return field.__class__.get_sortedset_db_key(model_class, field_name).redis_key


def _plant_confidence(
    record, confidence, field_name="certainty", evidence_count=10, raw=None
):
    """Write a confidence payload straight into the ConfidenceField :data hash.

    Bypasses the Bayesian update so a test can pin an exact confidence rather
    than depend on how many signals it takes to reach one. ``raw`` plants
    arbitrary bytes, for the corrupt-payload path.
    """
    field = type(record)._meta.fields[field_name]
    data_key = field.get_data_hash_key(record, field_name)
    payload = (
        raw
        if raw is not None
        else msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": evidence_count,
                "corroborations": evidence_count,
                "contradictions": 0,
            },
            use_bin_type=True,
        )
    )
    POPOTO_REDIS_DB.hset(data_key, record.db_key.redis_key, payload)


def _baseline_scores(model_class, now, field_name="relevance", n=50):
    """Raw Lua output using the PRE-#491 EVAL shape (numkeys 1, no ARGV[5:]).

    ARGV[5] is nil there, so ``s`` is 0 and the modulation branch never runs.
    This is the byte-exact regression oracle for every neutrality claim.
    """
    field = model_class._meta.fields[field_name]
    return POPOTO_REDIS_DB.eval(
        DECAY_SCORE_LUA,
        1,
        _zkey(model_class, field_name),
        str(now),
        str(field.decay_rate),
        str(n),
        field.base_score_field or "",
    )


def _wired_scores(model_class, now, field_name="relevance", n=50, filters=None):
    """Raw Lua output through the production wiring (mirrors query.py:410)."""
    field = model_class._meta.fields[field_name]
    conf_key, s, c0 = confidence_modulation_args(
        model_class, field, field_name, filters=filters or {}
    )
    return POPOTO_REDIS_DB.eval(
        DECAY_SCORE_LUA,
        2,
        _zkey(model_class, field_name),
        conf_key,
        str(now),
        str(field.decay_rate),
        str(n),
        field.base_score_field or "",
        s,
        c0,
    )


# --- Resolution: auto-detect ---


class TestAutoDetection:
    def test_single_confidence_field_is_auto_detected(self):
        name, field = resolve_confidence_modulation_field(
            ModDecayOneConfidence,
            ModDecayOneConfidence._meta.fields["relevance"],
            "relevance",
        )
        assert name == "certainty"
        assert isinstance(field, ConfidenceField)

    def test_no_confidence_field_means_off(self):
        assert resolve_confidence_modulation_field(
            ModDecayNoConfidence,
            ModDecayNoConfidence._meta.fields["relevance"],
            "relevance",
        ) == (None, None)

    def test_two_confidence_fields_disable_and_warn(self, caplog):
        with caplog.at_level("WARNING", logger="POPOTO.DecayingSortedField"):
            resolved = resolve_confidence_modulation_field(
                ModDecayTwoConfidences,
                ModDecayTwoConfidences._meta.fields["relevance"],
                "relevance",
            )
        assert resolved == (None, None), "must never guess between candidates"
        # The warning has to name the candidates or it is unactionable.
        assert "certainty" in caplog.text and "trust" in caplog.text

    def test_explicit_name_overrides_ambiguity(self):
        name, _ = resolve_confidence_modulation_field(
            ModDecayExplicitPick,
            ModDecayExplicitPick._meta.fields["relevance"],
            "relevance",
        )
        assert name == "trust"

    def test_false_disables_even_with_a_candidate(self):
        assert resolve_confidence_modulation_field(
            ModDecayOptedOut,
            ModDecayOptedOut._meta.fields["relevance"],
            "relevance",
        ) == (None, None)

    def test_unknown_field_name_raises(self):
        with pytest.raises(ModelException, match="does not exist"):
            resolve_confidence_modulation_field(
                ModDecayBadName,
                ModDecayBadName._meta.fields["relevance"],
                "relevance",
            )

    def test_non_confidence_field_name_raises(self):
        with pytest.raises(ModelException, match="must name a ConfidenceField"):
            resolve_confidence_modulation_field(
                ModDecayNotConfidence,
                ModDecayNotConfidence._meta.fields["relevance"],
                "relevance",
            )

    def test_bad_kwarg_type_rejected_at_construction(self):
        with pytest.raises(ModelException, match="confidence_modulation_field"):
            DecayingSortedField(confidence_modulation_field=42)


# --- Kill switch ---


class TestKillSwitch:
    def test_disabled_flag_forces_off_without_touching_model_code(self, monkeypatch):
        monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_ENABLED", False)
        assert resolve_confidence_modulation_field(
            ModDecayOneConfidence,
            ModDecayOneConfidence._meta.fields["relevance"],
            "relevance",
        ) == (None, None)
        assert (
            confidence_modulation_args(
                ModDecayOneConfidence,
                ModDecayOneConfidence._meta.fields["relevance"],
                "relevance",
            )
            == MODULATION_DISABLED
        )

    def test_zero_strength_forces_off(self, monkeypatch):
        monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", 0.0)
        assert (
            confidence_modulation_args(
                ModDecayOneConfidence,
                ModDecayOneConfidence._meta.fields["relevance"],
                "relevance",
            )
            == MODULATION_DISABLED
        )


# --- EVAL args ---


class TestModulationArgs:
    def test_args_carry_the_fields_own_initial_confidence(self):
        key, s, c0 = confidence_modulation_args(
            ModDecayLowPrior,
            ModDecayLowPrior._meta.fields["relevance"],
            "relevance",
        )
        assert key.endswith(":data")
        assert float(s) == Defaults.DECAY_CONFIDENCE_MODULATION_STRENGTH
        # c0 must be the field's configured prior, never a hard-coded 0.5.
        assert float(c0) == 0.3

    def test_disabled_path_passes_empty_key_so_lua_skips_the_hget(self):
        key, s, _ = confidence_modulation_args(
            ModDecayNoConfidence,
            ModDecayNoConfidence._meta.fields["relevance"],
            "relevance",
        )
        assert key == ""
        assert float(s) == 0

    def test_partition_guard_names_the_missing_filter(self):
        field = ModDecayPartitionedConfidence._meta.fields["relevance"]
        with pytest.raises(QueryException) as exc:
            confidence_modulation_args(
                ModDecayPartitionedConfidence, field, "relevance", filters={}
            )
        msg = str(exc.value)
        assert "certainty" in msg, "must name the ConfidenceField"
        assert "project" in msg, "must name the missing filter"
        assert "confidence_modulation_field=False" in msg, "must offer an escape hatch"

    def test_partition_satisfied_by_filters(self):
        field = ModDecayPartitionedConfidence._meta.fields["relevance"]
        key, _, _ = confidence_modulation_args(
            ModDecayPartitionedConfidence,
            field,
            "relevance",
            filters={"project": "apollo"},
        )
        assert key.endswith(":data:apollo")


# --- End-to-end through top_by_decay ---


class TestQueryIntegration:
    def test_partitioned_confidence_query_without_filter_raises(self):
        rec = ModDecayPartitionedConfidence(name="a", project="apollo")
        rec.save()
        with pytest.raises(QueryException, match="project"):
            ModDecayPartitionedConfidence.query.top_by_decay("relevance", n=5)

    def test_no_confidence_data_is_bit_exactly_neutral(self):
        """A model WITH a ConfidenceField but no recorded evidence must score
        identically to the same model with modulation switched off."""
        rec = ModDecayOneConfidence(name="a")
        rec.save()
        _backdate(rec, "relevance", 10)

        field = ModDecayOneConfidence._meta.fields["relevance"]
        zkey = field.get_partitioned_sortedset_db_key(rec, "relevance").redis_key

        from src.popoto.fields.decaying_sorted_field import DECAY_SCORE_LUA

        now = time.time()
        modulated_key, s, c0 = confidence_modulation_args(
            ModDecayOneConfidence, field, "relevance", filters={}
        )
        assert modulated_key != "", "auto-detection should be on for this model"

        def run(conf_key, strength):
            return POPOTO_REDIS_DB.eval(
                DECAY_SCORE_LUA,
                2,
                zkey,
                conf_key,
                str(now),
                str(field.decay_rate),
                "10",
                "",
                strength,
                c0,
            )

        # Byte-identical Lua tostring() output, not just approximately equal.
        assert run(modulated_key, s) == run("", "0")

    def test_low_confidence_record_ranks_below_a_corroborated_one(self):
        """The wiring is live: identical records aged identically diverge once
        one accumulates dismissals and the other corroborations."""
        good = ModDecayOneConfidence(name="good")
        good.save()
        bad = ModDecayOneConfidence(name="bad")
        bad.save()
        for _ in range(8):
            ConfidenceField.update_confidence(good, "certainty", 1.0)
            ConfidenceField.update_confidence(bad, "certainty", 0.0)

        field = ModDecayOneConfidence._meta.fields["relevance"]
        zkey = field.get_partitioned_sortedset_db_key(good, "relevance").redis_key
        aged = time.time() - 86400 * 30
        POPOTO_REDIS_DB.zadd(
            zkey,
            {good.db_key.redis_key: aged, bad.db_key.redis_key: aged},
        )

        ranked = ModDecayOneConfidence.query.top_by_decay("relevance", n=10)
        assert [r.name for r in ranked] == ["good", "bad"]


# --- Byte-exact neutrality through the production wiring -------------------


def _as_scores(raw):
    """Raw Lua output -> {member: raw score string}, preserving Lua's tostring."""
    decoded = [x.decode() if isinstance(x, bytes) else x for x in (raw or [])]
    return {decoded[i]: decoded[i + 1] for i in range(0, len(decoded), 2)}


def _make_aged(model_class, names, days=30, field_name="relevance"):
    """Create records and plant one shared, well-aged timestamp for each.

    ``days`` is comfortably past the ``max(t, 1.0)`` guard, so modulation is
    live and any neutrality result is a real claim rather than an artifact of
    the sub-one-day region.
    """
    records = {}
    now = time.time()
    scores = {}
    for name in names:
        rec = model_class(name=name)
        rec.save()
        records[name] = rec
        scores[rec.db_key.redis_key] = now - 86400 * days
    POPOTO_REDIS_DB.zadd(_zkey(model_class, field_name), scores)
    return records


class TestByteExactNeutrality:
    """Every 'off' path must reproduce pre-#491 scores byte-for-byte.

    Compared on Lua's raw ``tostring()`` output, never on floats within a
    tolerance: the #448 tie-break contract and the assembler's ``rel_tol=1e-6``
    linearity assertion both depend on exact equality, not near-equality.
    """

    def test_model_with_no_confidence_field_is_byte_identical(self):
        _make_aged(ModDecayNoConfidence, ["a", "b", "c"])
        now = time.time()
        assert _wired_scores(ModDecayNoConfidence, now) == _baseline_scores(
            ModDecayNoConfidence, now
        )

    def test_kwarg_false_is_byte_identical_despite_real_evidence(self):
        recs = _make_aged(ModDecayOptedOut, ["a", "b"])
        _plant_confidence(recs["a"], 0.01)
        _plant_confidence(recs["b"], 0.99)
        now = time.time()
        assert _wired_scores(ModDecayOptedOut, now) == _baseline_scores(
            ModDecayOptedOut, now
        )

    def test_kill_switch_is_byte_identical_without_editing_model_code(
        self, monkeypatch
    ):
        """ModDecayOneConfidence auto-detects; only the deploy-level flag is off."""
        recs = _make_aged(ModDecayOneConfidence, ["a", "b"])
        _plant_confidence(recs["a"], 0.01)
        _plant_confidence(recs["b"], 0.99)
        now = time.time()
        # Control: with the switch ON these scores are NOT neutral.
        assert _wired_scores(ModDecayOneConfidence, now) != _baseline_scores(
            ModDecayOneConfidence, now
        )

        monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_ENABLED", False)
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )

    def test_zero_strength_is_byte_identical(self, monkeypatch):
        recs = _make_aged(ModDecayOneConfidence, ["a", "b"])
        _plant_confidence(recs["a"], 0.01)
        _plant_confidence(recs["b"], 0.99)
        now = time.time()

        monkeypatch.setattr(Defaults, "DECAY_CONFIDENCE_MODULATION_STRENGTH", 0.0)
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )

    def test_no_evidence_at_default_prior_is_byte_identical(self):
        """on_save HSETNXs initial_confidence, so 'no data' really means c == c0."""
        recs = _make_aged(ModDecayOneConfidence, ["a", "b"])
        field = ModDecayOneConfidence._meta.fields["certainty"]
        data_key = field.get_data_hash_key(recs["a"], "certainty")
        assert (
            POPOTO_REDIS_DB.hget(data_key, recs["a"].db_key.redis_key) is not None
        ), "on_save should have seeded the prior; otherwise this test is vacuous"

        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )

    def test_non_default_initial_confidence_is_byte_identical(self):
        """initial_confidence=0.3 with zero evidence must ALSO be neutral.

        This is the critique blocker: centering the exponent on a hard-coded
        0.5 would hand every zero-evidence record on this field a permanent
        2^(0.4s) penalty it never earned.
        """
        recs = _make_aged(ModDecayLowPrior, ["a", "b", "c"])
        field = ModDecayLowPrior._meta.fields["certainty"]
        assert field.initial_confidence == 0.3
        data_key = field.get_data_hash_key(recs["a"], "certainty")
        seeded = POPOTO_REDIS_DB.hget(data_key, recs["a"].db_key.redis_key)
        assert seeded is not None, "prior must be on disk or the test proves nothing"
        assert msgpack.unpackb(seeded, raw=False)["confidence"] == 0.3

        now = time.time()
        modulated_key, s, c0 = confidence_modulation_args(
            ModDecayLowPrior, field, "relevance", filters={}
        )
        assert modulated_key != "" and float(s) > 0, "modulation must be live"
        assert float(c0) == 0.3

        assert _wired_scores(ModDecayLowPrior, now) == _baseline_scores(
            ModDecayLowPrior, now
        )

    def test_evidence_moves_scores_so_the_neutrality_tests_are_not_vacuous(self):
        recs = _make_aged(ModDecayOneConfidence, ["a"])
        _plant_confidence(recs["a"], 0.01)
        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) != _baseline_scores(
            ModDecayOneConfidence, now
        )


# --- Risk 4: the sub-one-day sign flip -------------------------------------


class TestSignFlipRegression:
    """A freshly-touched low-confidence record must not outrank a fresh
    high-confidence one.

    Without ``math.max(elapsed_days, 1.0)`` the ``t^(-rate)`` term is a
    multiplier > 1 for t < 1 that a LARGER effective rate amplifies MORE, so
    modulation would run backwards and boost exactly the junk it is meant to
    bury -- across most of an agent's working set, since memory is touched
    constantly.
    """

    def _plant_fresh(self, days):
        recs = _make_aged(ModDecayOneConfidence, ["high_conf", "low_conf"], days=days)
        _plant_confidence(recs["low_conf"], 0.01)
        _plant_confidence(recs["high_conf"], 0.99)
        return recs

    @pytest.mark.parametrize("days", [0.001, 0.25, 0.5, 0.999])
    def test_fresh_low_confidence_never_outranks_fresh_high(self, days):
        recs = self._plant_fresh(days)
        now = time.time()
        scores = _as_scores(_wired_scores(ModDecayOneConfidence, now))
        low = float(scores[recs["low_conf"].db_key.redis_key])
        high = float(scores[recs["high_conf"].db_key.redis_key])
        assert low <= high

    def test_fresh_records_are_byte_identical_to_unmodulated(self):
        """Inside the guard region the correction term is exactly 1.0."""
        self._plant_fresh(0.5)
        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )

    def test_past_one_day_the_intended_direction_takes_over(self):
        """Same setup aged past the hinge: high confidence now wins outright."""
        recs = self._plant_fresh(30)
        now = time.time()
        scores = _as_scores(_wired_scores(ModDecayOneConfidence, now))
        assert float(scores[recs["low_conf"].db_key.redis_key]) < float(
            scores[recs["high_conf"].db_key.redis_key]
        )
        ranked = ModDecayOneConfidence.query.top_by_decay("relevance", n=10)
        assert [r.name for r in ranked] == ["high_conf", "low_conf"]


# --- Directional effect ----------------------------------------------------


class TestDirectionalEffect:
    """Low confidence decays measurably faster, high confidence slower."""

    def _corpus(self, days=30):
        recs = _make_aged(ModDecayOneConfidence, ["low", "mid", "high"], days=days)
        _plant_confidence(recs["low"], 0.05)
        _plant_confidence(recs["mid"], 0.5)  # == the field's initial_confidence
        _plant_confidence(recs["high"], 0.95)
        return recs

    def test_ordering_is_low_mid_high(self):
        recs = self._corpus()
        now = time.time()
        scores = _as_scores(_wired_scores(ModDecayOneConfidence, now))
        low, mid, high = (
            float(scores[recs[n].db_key.redis_key]) for n in ("low", "mid", "high")
        )
        assert low < mid < high

    def test_neutral_member_is_untouched_while_the_others_move(self):
        recs = self._corpus()
        now = time.time()
        modulated = _as_scores(_wired_scores(ModDecayOneConfidence, now))
        neutral = _as_scores(_baseline_scores(ModDecayOneConfidence, now))

        mid_key = recs["mid"].db_key.redis_key
        # c == c0: bit-exact, not merely close.
        assert modulated[mid_key] == neutral[mid_key]
        for name, cmp in (("low", float.__lt__), ("high", float.__gt__)):
            key = recs[name].db_key.redis_key
            assert cmp(float(modulated[key]), float(neutral[key]))

    def test_top_by_decay_reflects_the_ordering(self):
        self._corpus()
        ranked = ModDecayOneConfidence.query.top_by_decay("relevance", n=10)
        assert [r.name for r in ranked] == ["high", "mid", "low"]


# --- Corrupt / out-of-range payloads through the real query path -----------


class TestCorruptPayloadThroughQuery:
    def test_non_msgpack_payload_falls_back_to_neutral(self):
        recs = _make_aged(ModDecayOneConfidence, ["a", "b"])
        # 0xc1 is msgpack's "never used" byte: cmsgpack.unpack raises, so the
        # pcall fails and c must fall back to c0.
        _plant_confidence(recs["a"], 0.0, raw=b"\xc1")
        _plant_confidence(recs["b"], 0.0, raw=b"")
        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )
        # And the query path still returns both records rather than erroring.
        assert len(ModDecayOneConfidence.query.top_by_decay("relevance", n=10)) == 2

    def test_scalar_payload_falls_back_to_neutral(self):
        """Decodable but not a table (`type(data) ~= 'table'`) -> neutral."""
        recs = _make_aged(ModDecayOneConfidence, ["a"])
        _plant_confidence(recs["a"], 0.0, raw=msgpack.packb(42))
        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )

    def test_positional_payload_is_read_like_the_confidence_writer_reads_it(self):
        """A msgpack ARRAY payload is read positionally: data[1] is confidence.

        This is deliberate parity with ``CAPPED_BAYESIAN_UPDATE_LUA``
        (confidence_field.py:78), whose own reader accepts the same positional
        encoding -- the decay reader copies that block verbatim rather than
        inventing a second, divergent notion of a valid payload. Pinned here so
        the coupling is a decision on the record instead of an accident: a
        payload of ``[0.0, ...]`` must score like ``{'confidence': 0.0}``.
        """
        recs = _make_aged(ModDecayOneConfidence, ["a"])
        _plant_confidence(recs["a"], 0.0, raw=msgpack.packb([0.0, 10, 0, 10]))
        now = time.time()
        positional = _as_scores(_wired_scores(ModDecayOneConfidence, now))

        _plant_confidence(recs["a"], 0.0)
        mapping = _as_scores(_wired_scores(ModDecayOneConfidence, now))
        assert positional == mapping
        # And it is genuinely modulated, not silently neutral.
        assert positional != _as_scores(_baseline_scores(ModDecayOneConfidence, now))

    def test_out_of_range_positional_payload_is_still_clamped(self):
        """Even the positional path cannot push the exponent out of bounds."""
        recs = _make_aged(ModDecayOneConfidence, ["a"])
        _plant_confidence(recs["a"], 0.0, raw=msgpack.packb([32, 10, 0, 10]))
        now = time.time()
        clamped = _as_scores(_wired_scores(ModDecayOneConfidence, now))

        _plant_confidence(recs["a"], 1.0)
        assert clamped == _as_scores(_wired_scores(ModDecayOneConfidence, now))

    def test_out_of_range_confidence_is_clamped(self):
        recs = _make_aged(ModDecayOneConfidence, ["under", "over"])
        _plant_confidence(recs["under"], -4.0)
        _plant_confidence(recs["over"], 9.0)
        now = time.time()
        clamped = _as_scores(_wired_scores(ModDecayOneConfidence, now))

        _plant_confidence(recs["under"], 0.0)
        _plant_confidence(recs["over"], 1.0)
        exact = _as_scores(_wired_scores(ModDecayOneConfidence, now))

        assert clamped == exact

    def test_empty_zset_with_modulation_enabled(self):
        assert ModDecayOneConfidence.query.top_by_decay("relevance", n=10) == []

    def test_missing_data_hash_entirely_is_neutral(self):
        recs = _make_aged(ModDecayOneConfidence, ["a", "b"])
        field = ModDecayOneConfidence._meta.fields["certainty"]
        POPOTO_REDIS_DB.delete(field.get_data_hash_key(recs["a"], "certainty"))
        now = time.time()
        assert _wired_scores(ModDecayOneConfidence, now) == _baseline_scores(
            ModDecayOneConfidence, now
        )
