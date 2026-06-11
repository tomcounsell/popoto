"""Tests for ConfidenceField — capped-evidence confidence tracking with entrainment.

Tests cover:
- Field initialization (initial_confidence, evidence_cap, validation)
- on_save() initializes companion hash with initial_confidence
- on_delete() cleans up companion hash entries
- update_confidence() with the capped-evidence update rule:
  order-invariant running mean over {prior, signals...} while effective
  evidence <= cap; fixed-gain exponential forgetting (window cap+1) beyond it
- Gain shrinkage: 10th update moves score less than 1st (within the window)
- Corroboration increases confidence, contradiction decreases
- get_confidence() and get_confidence_data() read current values
- Error cases: unsaved model, invalid signal, wrong field type
- Entrainment: ObservationProtocol outcome handlers update confidence
- Auto-discharge: confidence < threshold - epsilon resolves pressure on
  CyclicDecayFields (values within float epsilon of 0.1 do NOT discharge)

FLOAT-TOLERANCE CONVENTION: the incremental Lua update is algebraically
order-invariant but NOT IEEE-754 bit-identical across orderings. Every
closed-form assertion compares against the oracle with
abs(actual - oracle) < 1e-12 — never ==.
"""

import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class ConfidenceItem(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField()


class ConfidenceCustom(popoto.Model):
    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(initial_confidence=0.8)


class ConfidenceWithCyclic(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    certainty = ConfidenceField(initial_confidence=0.5)
    relevance = CyclicDecayField(
        decay_rate=0.5,
        pressure_rate=0.1,
    )


class ConfidenceCapped(popoto.Model):
    """Model with a custom (small) evidence_cap for forgetting-rate tests."""

    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(evidence_cap=5)


class ConfidenceLowPrior(popoto.Model):
    """Model with a low initial_confidence for prior-weight tests."""

    name = popoto.UniqueKeyField()
    certainty = ConfidenceField(initial_confidence=0.01)


# --- Helpers ---


def _seed_confidence_data(
    item, field_name, confidence, evidence_count, corroborations=0, contradictions=0
):
    """Write companion-hash state directly (bypasses the update rule)."""
    import msgpack

    field = item._meta.fields[field_name]
    data_hash_key = field.get_data_hash_key(item, field_name)
    member_key = item.db_key.redis_key
    POPOTO_REDIS_DB.hset(
        data_hash_key,
        member_key,
        msgpack.packb(
            {
                "confidence": confidence,
                "evidence_count": evidence_count,
                "corroborations": corroborations,
                "contradictions": contradictions,
            }
        ),
    )


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [
        ConfidenceItem,
        ConfidenceCustom,
        ConfidenceWithCyclic,
        ConfidenceCapped,
        ConfidenceLowPrior,
    ]:
        for key in POPOTO_REDIS_DB.keys(f"{model_class.__name__}:*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$Confidenc*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$CyclicDecay*"):
            POPOTO_REDIS_DB.delete(key)
        for key in POPOTO_REDIS_DB.keys(f"$RP:*"):
            POPOTO_REDIS_DB.delete(key)


# --- Initialization Tests ---


class TestConfidenceFieldInit:
    def test_default_initial_confidence(self):
        """Default initial_confidence is 0.5."""
        field = ConfidenceField()
        assert field.initial_confidence == 0.5

    def test_custom_initial_confidence(self):
        """Custom initial_confidence is stored."""
        field = ConfidenceField(initial_confidence=0.8)
        assert field.initial_confidence == 0.8

    def test_invalid_initial_confidence_below_zero(self):
        """initial_confidence < 0 raises ModelException."""
        with pytest.raises(popoto.ModelException):
            ConfidenceField(initial_confidence=-0.1)

    def test_invalid_initial_confidence_above_one(self):
        """initial_confidence > 1 raises ModelException."""
        with pytest.raises(popoto.ModelException):
            ConfidenceField(initial_confidence=1.5)

    def test_boundary_initial_confidence_zero(self):
        """initial_confidence=0 is valid."""
        field = ConfidenceField(initial_confidence=0)
        assert field.initial_confidence == 0

    def test_boundary_initial_confidence_one(self):
        """initial_confidence=1 is valid."""
        field = ConfidenceField(initial_confidence=1)
        assert field.initial_confidence == 1

    def test_field_type_is_float(self):
        """ConfidenceField defaults to float type."""
        field = ConfidenceField()
        assert field.type == float

    def test_field_is_nullable(self):
        """ConfidenceField is nullable by default."""
        field = ConfidenceField()
        assert field.null is True


# --- on_save / on_delete Lifecycle ---


class TestConfidenceFieldLifecycle:
    def test_on_save_initializes_companion_hash(self):
        """Saving a model with ConfidenceField creates companion hash entry."""
        item = ConfidenceItem.create(name="test1")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5
        assert data["evidence_count"] == 0
        assert data["corroborations"] == 0
        assert data["contradictions"] == 0

    def test_on_save_custom_initial(self):
        """Custom initial_confidence is used in companion hash."""
        item = ConfidenceCustom.create(name="test1")
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.8

    def test_on_save_does_not_overwrite(self):
        """Re-saving does not overwrite existing confidence data."""
        item = ConfidenceItem.create(name="test1")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)

        # Re-save should NOT reset confidence
        item.save()
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 1  # still has the update

    def test_on_delete_removes_companion_hash(self):
        """Deleting model removes companion hash entry."""
        item = ConfidenceItem.create(name="test_del")
        # Verify data exists
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["confidence"] == 0.5

        item.delete()
        # After delete, get_confidence_data should return defaults
        # (but we can't call it on deleted instance easily, so check Redis directly)
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        member_key = f"ConfidenceItem:{item.name}"
        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        assert raw is None


# --- update_confidence Tests ---


class TestUpdateConfidence:
    def test_corroboration_increases_confidence(self):
        """Signal > 0.5 increases confidence from 0.5 initial."""
        item = ConfidenceItem.create(name="corr1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert new_conf > 0.5

    def test_contradiction_decreases_confidence(self):
        """Signal < 0.5 decreases confidence from 0.5 initial."""
        item = ConfidenceItem.create(name="contr1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.1)
        assert new_conf < 0.5

    def test_capped_formula_first_update(self):
        """First update is the running mean of {prior, signal}.

        Closed form: n=0, prior_weight=1, n_eff = min(0+1, 20) = 1,
        new = 0.5 + (0.9 - 0.5) / (1 + 1) = (0.5 + 0.9) / 2 = 0.7
        """
        item = ConfidenceItem.create(name="formula1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - 0.7) < 1e-12

    def test_capped_formula_second_update(self):
        """Second update extends the running mean over {prior, s1, s2}.

        Closed form: (c0 + s1 + s2) / 3 = (0.5 + 0.9 + 0.9) / 3 = 2.3 / 3
        """
        item = ConfidenceItem.create(name="formula2")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        # After first: confidence = 0.7, evidence_count = 1
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - (0.5 + 0.9 + 0.9) / 3) < 1e-12

    def test_gain_shrinks_within_window(self):
        """10th update moves score less than 1st update (gain 1/(n_eff+1))."""
        item = ConfidenceItem.create(name="precision1")

        # First update: delta = (0.8 - 0.5) / 2 = 0.15
        first_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.8)
        first_delta = abs(first_conf - 0.5)

        # Apply 8 more updates to build evidence
        for _ in range(8):
            ConfidenceField.update_confidence(item, "certainty", signal=0.8)

        # Record confidence before 10th update
        data_before = ConfidenceField.get_confidence_data(item, "certainty")
        conf_before = data_before["confidence"]

        # 10th update
        tenth_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.8)
        tenth_delta = abs(tenth_conf - conf_before)

        assert tenth_delta < first_delta

    def test_counters_increment(self):
        """Corroborations and contradictions counters track correctly."""
        item = ConfidenceItem.create(name="counters1")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        ConfidenceField.update_confidence(item, "certainty", signal=0.1)

        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 3
        assert data["corroborations"] == 2
        assert data["contradictions"] == 1

    def test_confidence_clamped_to_zero_one(self):
        """Confidence stays within [0, 1] bounds."""
        item = ConfidenceItem.create(name="clamp1")
        # Many corroborations
        for _ in range(20):
            conf = ConfidenceField.update_confidence(item, "certainty", signal=1.0)
        assert 0 <= conf <= 1

        item2 = ConfidenceItem.create(name="clamp2")
        # Many contradictions
        for _ in range(20):
            conf = ConfidenceField.update_confidence(item2, "certainty", signal=0.0)
        assert 0 <= conf <= 1


# --- Capped-Evidence Update Rule (issue #407) ---


class TestCappedBayesianUpdate:
    """Tests for the capped-evidence update rule.

    Within the window (effective evidence <= cap) the rule is an exact
    running mean over {prior, signals...}:
        c_n = (c0 + sum(signals)) / (n + 1)
    At the cap the gain freezes at 1/(cap+1) — exponential forgetting.
    """

    def test_order_invariance(self):
        """A fixed multiset of signals lands on the same value in any order.

        Closed-form oracle for n <= cap signals from c0 = 0.5:
            oracle = (0.5 + sum(signals)) / (len(signals) + 1)
                   = (0.5 + 4.05) / 9
        Incremental float evaluation is order-invariant algebraically but
        not bit-identical, hence 1e-12 tolerance (never ==).
        """
        signals = [0.9, 0.1, 0.8, 0.2, 0.7, 0.6, 0.45, 0.3]
        oracle = (0.5 + sum(signals)) / (len(signals) + 1)

        permutations = [
            signals,
            list(reversed(signals)),
            sorted(signals),
            sorted(signals, reverse=True),
            signals[4:] + signals[:4],
        ]
        for i, perm in enumerate(permutations):
            item = ConfidenceItem.create(name=f"perm{i}")
            for s in perm:
                ConfidenceField.update_confidence(item, "certainty", signal=s)
            final = ConfidenceField.get_confidence(item, "certainty")
            assert abs(final - oracle) < 1e-12, f"permutation {i}: {perm}"

    def test_prior_weight_first_update_from_default(self):
        """One update with signal 0.9 from initial 0.5 lands on 0.7.

        Closed form: (c0 + s) / 2 = (0.5 + 0.9) / 2 = 0.7 — the prior
        counts as exactly one pseudo-observation.
        """
        item = ConfidenceItem.create(name="prior1")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - 0.7) < 1e-12

    def test_prior_weight_first_update_from_low_initial(self):
        """One update with signal 0.9 from initial 0.01 lands on 0.455.

        Closed form: (c0 + s) / 2 = (0.01 + 0.9) / 2 = 0.455
        """
        item = ConfidenceLowPrior.create(name="prior2")
        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - 0.455) < 1e-12

    def test_running_mean_equivalence(self):
        """n <= cap updates equal the running mean (c0 + sum(signals)) / (n+1)."""
        signals = [0.85, 0.15, 0.6, 0.4, 0.95, 0.05, 0.7, 0.25, 0.5, 0.8]
        item = ConfidenceItem.create(name="runmean1")
        for s in signals:
            conf = ConfidenceField.update_confidence(item, "certainty", signal=s)
        # Closed form: (0.5 + sum) / 11
        oracle = (0.5 + sum(signals)) / (len(signals) + 1)
        assert abs(conf - oracle) < 1e-12

    def test_cap_forgetting_crosses_half_on_15th_contradiction(self):
        """At the cap, contradictions decay confidence geometrically.

        Seed the companion hash DIRECTLY with confidence=0.9,
        evidence_count=20 (driving 20 updates would land near 0.8810, not
        0.9 — the oracle below assumes the seeded state).

        At the cap: n_eff = min(20 + 1, 20) = 20, gain = 1/21, so each
        0.1-signal contradiction gives c' = c + (0.1 - c)/21, a geometric
        approach to the 0.1 fixed point:
            c_k = 0.1 + (0.9 - 0.1) * (20/21)^k = 0.1 + 0.8 * (20/21)^k
        First k with c_k < 0.5: 0.8*(20/21)^k < 0.4 -> k > ln 2 / ln(21/20)
        = 14.2 -> k = 15. So it stays >= 0.5 through k=14 and crosses on
        the 15th contradiction.
        """
        item = ConfidenceItem.create(name="capforget1")
        _seed_confidence_data(
            item, "certainty", confidence=0.9, evidence_count=20, corroborations=20
        )

        for k in range(1, 15):
            conf = ConfidenceField.update_confidence(item, "certainty", signal=0.1)
            expected = 0.1 + 0.8 * (20 / 21) ** k
            assert abs(conf - expected) < 1e-12, f"k={k}"
            assert conf >= 0.5, f"crossed below 0.5 too early at k={k}"

        # 15th contradiction: c_15 = 0.1 + 0.8*(20/21)^15 = 0.4848... < 0.5
        conf = ConfidenceField.update_confidence(item, "certainty", signal=0.1)
        expected = 0.1 + 0.8 * (20 / 21) ** 15
        assert abs(conf - expected) < 1e-12
        assert conf < 0.5

    def test_custom_evidence_cap_changes_forgetting_rate(self):
        """A smaller evidence_cap forgets faster once at the cap.

        Both seeded with confidence=0.9, evidence_count=20, then one
        0.1-signal contradiction:
          default cap=20: n_eff = min(21, 20) = 20, gain 1/21:
              c' = 0.9 + (0.1 - 0.9)/21 = 0.9 - 0.8/21
          cap=5:          n_eff = min(21, 5)  = 5,  gain 1/6:
              c' = 0.9 + (0.1 - 0.9)/6  = 0.9 - 0.8/6
        """
        default_item = ConfidenceItem.create(name="capdefault1")
        capped_item = ConfidenceCapped.create(name="capcustom1")
        for item in (default_item, capped_item):
            _seed_confidence_data(
                item, "certainty", confidence=0.9, evidence_count=20, corroborations=20
            )

        default_conf = ConfidenceField.update_confidence(
            default_item, "certainty", signal=0.1
        )
        capped_conf = ConfidenceField.update_confidence(
            capped_item, "certainty", signal=0.1
        )

        assert abs(default_conf - (0.9 - 0.8 / 21)) < 1e-12
        assert abs(capped_conf - (0.9 - 0.8 / 6)) < 1e-12
        assert capped_conf < default_conf  # smaller cap forgets faster

    def test_concurrent_updates_within_window_match_oracle(self):
        """Concurrent updates within the window land on the running mean.

        The Lua script is atomic, so concurrent updates serialize in SOME
        order; order-invariance within the window means the final value
        matches the closed-form oracle (0.5 + sum(signals)) / (n+1)
        regardless of interleaving. NOT exact equality — 1e-12 tolerance.
        """
        import threading

        item = ConfidenceItem.create(name="concurrent1")
        signals = [0.9, 0.1, 0.8, 0.2, 0.7, 0.3, 0.6, 0.4, 0.95, 0.05]

        errors = []

        def _update(sig):
            try:
                ConfidenceField.update_confidence(item, "certainty", signal=sig)
            except Exception as e:  # pragma: no cover - failure diagnostics
                errors.append(e)

        threads = [threading.Thread(target=_update, args=(s,)) for s in signals]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == len(signals)
        # Closed form: (0.5 + sum(signals)) / 11
        oracle = (0.5 + sum(signals)) / (len(signals) + 1)
        assert abs(data["confidence"] - oracle) < 1e-12

    def test_corrupt_companion_data_recovers(self):
        """Corrupt companion-hash bytes re-init from defaults on update.

        0xc1 is an invalid msgpack type byte, so cmsgpack.unpack fails and
        the Lua script falls back to {initial_confidence, 0, 0, 0}.
        Closed form after recovery + one 0.9 signal: (0.5 + 0.9)/2 = 0.7.
        """
        item = ConfidenceItem.create(name="corrupt1")
        field = item._meta.fields["certainty"]
        data_hash_key = field.get_data_hash_key(item, "certainty")
        POPOTO_REDIS_DB.hset(data_hash_key, item.db_key.redis_key, b"\xc1garbage")

        new_conf = ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(new_conf - 0.7) < 1e-12

        data = ConfidenceField.get_confidence_data(item, "certainty")
        assert data["evidence_count"] == 1
        assert data["corroborations"] == 1
        assert data["contradictions"] == 0


# --- evidence_cap Configuration (issue #407) ---


class TestEvidenceCapConfig:
    def test_default_evidence_cap_is_20(self):
        """Default evidence_cap comes from Defaults.CONFIDENCE_EVIDENCE_CAP."""
        from src.popoto.fields.constants import Defaults

        assert Defaults.CONFIDENCE_EVIDENCE_CAP == 20
        field = ConfidenceField()
        assert field.evidence_cap == 20

    def test_none_falls_back_to_default(self):
        """evidence_cap=None resolves to the Defaults value."""
        field = ConfidenceField(evidence_cap=None)
        assert field.evidence_cap == 20

    def test_custom_evidence_cap_stored(self):
        """A custom integer evidence_cap is honored."""
        field = ConfidenceField(evidence_cap=5)
        assert field.evidence_cap == 5
        assert ConfidenceCapped._meta.fields["certainty"].evidence_cap == 5

    @pytest.mark.parametrize(
        "bad_cap",
        ["20", 20.0, True, False, 0, -1],
        ids=["str", "float", "bool_true", "bool_false", "zero", "negative"],
    )
    def test_invalid_evidence_cap_raises(self, bad_cap):
        """Non-int (incl. bool) or < 1 evidence_cap raises ModelException."""
        with pytest.raises(popoto.ModelException):
            ConfidenceField(evidence_cap=bad_cap)

    def test_invalid_evidence_cap_raises_at_class_definition(self):
        """Validation fires at model-class definition time, not at use time."""
        with pytest.raises(popoto.ModelException):

            class BadCapModel(popoto.Model):
                name = popoto.UniqueKeyField()
                certainty = ConfidenceField(evidence_cap=0)


# --- get_confidence / get_confidence_data Tests ---


class TestGetConfidence:
    def test_get_confidence_returns_initial(self):
        """get_confidence returns initial_confidence for new items."""
        item = ConfidenceItem.create(name="get1")
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf == 0.5

    def test_get_confidence_after_update(self):
        """get_confidence reflects updates.

        Closed form: first update = (c0 + s) / 2 = (0.5 + 0.9) / 2 = 0.7
        """
        item = ConfidenceItem.create(name="get2")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert abs(conf - 0.7) < 1e-12

    def test_get_confidence_data_structure(self):
        """get_confidence_data returns complete metadata dict."""
        item = ConfidenceItem.create(name="get3")
        ConfidenceField.update_confidence(item, "certainty", signal=0.7)
        data = ConfidenceField.get_confidence_data(item, "certainty")

        assert "confidence" in data
        assert "evidence_count" in data
        assert "corroborations" in data
        assert "contradictions" in data
        assert data["evidence_count"] == 1
        assert data["corroborations"] == 1
        assert data["contradictions"] == 0


# --- Attribute Access Tests (issue #281) ---


class TestAttributeAccess:
    def test_attribute_returns_initial_confidence_after_create(self):
        """instance.certainty returns initial_confidence, not None (issue #281)."""
        item = ConfidenceItem.create(name="attr1")
        assert item.certainty == 0.5

    def test_attribute_returns_custom_initial_confidence(self):
        """instance.certainty returns custom initial_confidence after create."""
        item = ConfidenceCustom.create(name="attr2")
        assert item.certainty == 0.8

    def test_attribute_returns_initial_before_save(self):
        """instance.certainty returns initial_confidence even before save."""
        item = ConfidenceItem(name="attr3")
        assert item.certainty == 0.5

    def test_attribute_returns_updated_value_after_update(self):
        """instance.certainty reflects updated confidence after update_confidence.

        Closed form: first update = (c0 + s) / 2 = (0.5 + 0.9) / 2 = 0.7
        """
        item = ConfidenceItem.create(name="attr4")
        ConfidenceField.update_confidence(item, "certainty", signal=0.9)
        assert abs(item.certainty - 0.7) < 1e-12

    def test_attribute_not_none(self):
        """instance.certainty is never None with default config (issue #281)."""
        item = ConfidenceItem.create(name="attr5")
        assert item.certainty is not None

    def test_confidence_reload_from_redis(self):
        """Reloading from Redis preserves initial_confidence (issue #289)."""
        ConfidenceItem.create(name="reload1")
        loaded = ConfidenceItem.query.get(name="reload1")
        assert loaded.certainty is not None
        assert loaded.certainty == 0.5


# --- Error Cases ---


class TestConfidenceFieldErrors:
    def test_update_unsaved_model_raises_type_error(self):
        """update_confidence on unsaved model raises TypeError."""
        item = ConfidenceItem(name="unsaved1")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "certainty", signal=0.9)

    def test_update_invalid_signal_above_one(self):
        """Signal > 1 raises ValueError."""
        item = ConfidenceItem.create(name="invalid1")
        with pytest.raises(ValueError):
            ConfidenceField.update_confidence(item, "certainty", signal=1.5)

    def test_update_invalid_signal_below_zero(self):
        """Signal < 0 raises ValueError."""
        item = ConfidenceItem.create(name="invalid2")
        with pytest.raises(ValueError):
            ConfidenceField.update_confidence(item, "certainty", signal=-0.1)

    def test_update_signal_none_raises_type_error(self):
        """Signal=None raises TypeError."""
        item = ConfidenceItem.create(name="invalid3")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "certainty", signal=None)

    def test_update_wrong_field_type_raises_type_error(self):
        """update_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceItem.create(name="wrongtype1")
        with pytest.raises(TypeError):
            ConfidenceField.update_confidence(item, "name", signal=0.9)

    def test_get_confidence_wrong_field_raises_type_error(self):
        """get_confidence on non-ConfidenceField raises TypeError."""
        item = ConfidenceItem.create(name="wrongtype2")
        with pytest.raises(TypeError):
            ConfidenceField.get_confidence(item, "name")


# --- Entrainment Tests ---


class TestEntrainment:
    def test_acted_corroborates_confidence(self):
        """ObservationProtocol 'acted' outcome increases confidence."""
        item = ConfidenceWithCyclic.create(name="ent1", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "acted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf > initial_conf

    def test_contradicted_decreases_confidence(self):
        """ObservationProtocol 'contradicted' outcome decreases confidence."""
        item = ConfidenceWithCyclic.create(name="ent2", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "contradicted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf < initial_conf

    def test_dismissed_does_not_change_confidence(self):
        """ObservationProtocol 'dismissed' outcome does NOT change confidence."""
        item = ConfidenceWithCyclic.create(name="ent3", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "dismissed"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf == initial_conf

    def test_deferred_does_not_change_confidence(self):
        """ObservationProtocol 'deferred' outcome does NOT change confidence."""
        item = ConfidenceWithCyclic.create(name="ent4", content="test")
        initial_conf = ConfidenceField.get_confidence(item, "certainty")

        outcome_map = {item.db_key.redis_key: "deferred"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        new_conf = ConfidenceField.get_confidence(item, "certainty")
        assert new_conf == initial_conf

    def _seed_pressure(self, item, last_resolved):
        """Seed pressure state on the relevance CyclicDecayField."""
        import msgpack

        rel_field = item._meta.fields["relevance"]
        pressure_hash_key = rel_field.get_pressure_hash_key(item, "relevance")
        member_key = item.db_key.redis_key
        pressure_data = {"rate": 0.1, "last_resolved": last_resolved}
        POPOTO_REDIS_DB.hset(
            pressure_hash_key, member_key, msgpack.packb(pressure_data)
        )
        return pressure_hash_key, member_key

    def test_auto_discharge_on_low_confidence(self):
        """Confidence clearly below threshold - epsilon auto-discharges pressure."""
        import msgpack
        import time

        from src.popoto.fields.constants import Defaults

        item = ConfidenceWithCyclic.create(name="ent5", content="test")

        # Build up some pressure by setting last_resolved to far in the past
        pressure_hash_key, member_key = self._seed_pressure(
            item, time.time() - 86400 * 30  # 30 days ago
        )

        # Manually seed confidence at 0.05 — clearly below threshold - epsilon
        _seed_confidence_data(
            item, "certainty", confidence=0.05, evidence_count=20, contradictions=20
        )

        # Now trigger contradicted outcome — auto-discharge should fire.
        # The contradicted update itself moves confidence toward 0.1:
        # at the cap, c' = 0.05 + (0.1 - 0.05)/21 = 0.0524, still clearly
        # below 0.1 - 1e-9.
        outcome_map = {item.db_key.redis_key: "contradicted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        # Epsilon boundary, explicitly: post-update confidence must sit
        # below threshold - epsilon for the discharge to have been correct.
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf < (
            Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - Defaults.CONFIDENCE_EPSILON
        )

        # Verify pressure was resolved (last_resolved should be recent)
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        assert raw is not None
        pdata = msgpack.unpackb(raw, raw=False)
        # last_resolved should be within the last minute (was 30 days ago)
        assert time.time() - pdata["last_resolved"] < 60

    def test_no_auto_discharge_within_epsilon_of_threshold(self):
        """The float predecessor of 0.1 does NOT trigger auto-discharge.

        0.09999999999999998 < 0.1 in IEEE-754, but it is within
        CONFIDENCE_EPSILON (1e-9) of the threshold — the strict
        `conf < threshold` comparison wrongly auto-discharged on the
        first contradiction for such float artifacts. With the epsilon
        guard, values within epsilon of the threshold are NOT below it.

        The contradicted update moves the seeded value toward the 0.1
        fixed point (at the cap: c' = c + (0.1 - c)/21), so it stays
        within epsilon of 0.1 — no discharge.
        """
        import msgpack
        import time

        item = ConfidenceWithCyclic.create(name="ent5eps", content="test")

        thirty_days_ago = time.time() - 86400 * 30
        pressure_hash_key, member_key = self._seed_pressure(item, thirty_days_ago)

        # Float predecessor of 0.1 — the artifact value from issue #407
        _seed_confidence_data(
            item,
            "certainty",
            confidence=0.09999999999999998,
            evidence_count=20,
            contradictions=20,
        )

        outcome_map = {item.db_key.redis_key: "contradicted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)

        # Pressure must NOT have been resolved — last_resolved unchanged
        raw = POPOTO_REDIS_DB.hget(pressure_hash_key, member_key)
        assert raw is not None
        pdata = msgpack.unpackb(raw, raw=False)
        assert abs(pdata["last_resolved"] - thirty_days_ago) < 1.0

    def test_entrainment_without_cyclic_field_is_safe(self):
        """Entrainment on model without CyclicDecayField is a no-op for cycles."""
        item = ConfidenceItem.create(name="ent6")
        # This should not raise
        outcome_map = {item.db_key.redis_key: "acted"}
        popoto.ObservationProtocol.on_context_used([item], outcome_map)
        # Confidence should still increase
        conf = ConfidenceField.get_confidence(item, "certainty")
        assert conf > 0.5


# --- Export Test ---


class TestExport:
    def test_confidence_field_exported_from_popoto(self):
        """ConfidenceField is importable from popoto."""
        assert hasattr(popoto, "ConfidenceField")
        assert popoto.ConfidenceField is ConfidenceField
