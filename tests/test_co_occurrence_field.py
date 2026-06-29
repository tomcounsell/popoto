"""Tests for CoOccurrenceField — weighted association edges with graph propagation.

Tests cover:
- Field initialization (symmetric, max_edges, decay_factor)
- link() creates weighted edges in Redis sorted sets
- link() symmetric creates bidirectional edges
- link() asymmetric creates unidirectional edges
- strengthen() increments edge weight via ZINCRBY
- strengthen() symmetric mode strengthens both directions
- unlink() removes edges (both directions if symmetric)
- weaken_all() multiplies all scores by factor
- weaken_all() prunes edges below threshold
- max_edges cap trims lowest-weight edges
- get_linked() returns sorted by weight descending
- get_linked() with min_weight filters correctly
- get_linked() with limit caps results
- Self-loop prevention (source_pk == target_pk -> ValueError)
- on_delete() cleans up edges in both directions
- Pipeline support
- propagate() linear chain with depth=2
- propagate() star graph with depth=1
- propagate() multi-path uses max weight
- propagate() depth=0 returns seeds only
- propagate() respects threshold cutoff
- propagate() empty seed list returns empty dict
- Synergy: CoOccurrenceField + DecayingSortedField
- Synergy: CoOccurrenceField + AccessTracker
- Synergy: CoOccurrenceField + ConfidenceField
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.constants import Defaults
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.access_tracker import AccessTrackerMixin
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.redis_db import POPOTO_REDIS_DB

# --- Test Models ---


class CoOcItem(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    associations = CoOccurrenceField(symmetric=True, max_edges=100)


class CoOcAsymmetric(popoto.Model):
    name = popoto.UniqueKeyField()
    associations = CoOccurrenceField(symmetric=False, max_edges=50)


class CoOcSmallCap(popoto.Model):
    name = popoto.UniqueKeyField()
    associations = CoOccurrenceField(symmetric=False, max_edges=3)


class CoOcWithDecay(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    strength = popoto.FloatField(default=1.0)
    last_accessed = DecayingSortedField(
        decay_rate=0.5,
        base_score_field="strength",
    )
    associations = CoOccurrenceField(symmetric=True, max_edges=100)


class CoOcWithTracker(AccessTrackerMixin, popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    associations = CoOccurrenceField(symmetric=True, max_edges=100)


class CoOcWithConfidence(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    certainty = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=100)


# --- Fixtures ---


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [
        CoOcItem,
        CoOcAsymmetric,
        CoOcSmallCap,
        CoOcWithDecay,
        CoOcWithTracker,
        CoOcWithConfidence,
    ]:
        # Clean up model instances
        for key in POPOTO_REDIS_DB.scan_iter(f"{model_class.__name__}:*"):
            POPOTO_REDIS_DB.delete(key)
        # Clean up CoOccurrenceField sorted sets
        for key in POPOTO_REDIS_DB.scan_iter(f"$CoOc*"):
            POPOTO_REDIS_DB.delete(key)
        # Clean up AccessTracker keys
        for key in POPOTO_REDIS_DB.scan_iter(f"$AT:*"):
            POPOTO_REDIS_DB.delete(key)
        # Clean up ConfidenceField keys
        for key in POPOTO_REDIS_DB.scan_iter(f"$Confidenc*"):
            POPOTO_REDIS_DB.delete(key)
        # Clean up DecayingSortedField keys
        for key in POPOTO_REDIS_DB.scan_iter(f"$Decaying*"):
            POPOTO_REDIS_DB.delete(key)


# --- Initialization Tests ---


class TestFieldInit:
    def test_default_params(self):
        field = CoOccurrenceField()
        assert field.symmetric is True
        assert field.max_edges == 500
        assert field.decay_factor == 0.95

    def test_custom_params(self):
        field = CoOccurrenceField(symmetric=False, max_edges=100, decay_factor=0.8)
        assert field.symmetric is False
        assert field.max_edges == 100
        assert field.decay_factor == 0.8

    def test_invalid_max_edges(self):
        with pytest.raises(popoto.ModelException):
            CoOccurrenceField(max_edges=0)

    def test_invalid_decay_factor_too_high(self):
        with pytest.raises(popoto.ModelException):
            CoOccurrenceField(decay_factor=1.5)

    def test_invalid_decay_factor_negative(self):
        with pytest.raises(popoto.ModelException):
            CoOccurrenceField(decay_factor=-0.1)


# --- Link Tests ---


class TestLink:
    def test_link_creates_edge(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.2)

        linked = field.get_linked(CoOcItem, "pk_a")
        assert len(linked) == 1
        assert linked[0][0] == "pk_b"
        assert abs(linked[0][1] - 0.2) < 1e-9

    def test_link_symmetric_creates_bidirectional(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.3)

        # Check forward edge
        forward = field.get_linked(CoOcItem, "pk_a")
        assert len(forward) == 1
        assert forward[0][0] == "pk_b"

        # Check reverse edge
        reverse = field.get_linked(CoOcItem, "pk_b")
        assert len(reverse) == 1
        assert reverse[0][0] == "pk_a"

    def test_link_asymmetric_creates_unidirectional(self):
        field = CoOcAsymmetric._meta.fields["associations"]
        field.link(CoOcAsymmetric, "pk_a", "pk_b", initial_weight=0.3)

        # Forward edge exists
        forward = field.get_linked(CoOcAsymmetric, "pk_a")
        assert len(forward) == 1

        # Reverse edge does NOT exist
        reverse = field.get_linked(CoOcAsymmetric, "pk_b")
        assert len(reverse) == 0

    def test_link_self_loop_raises(self):
        field = CoOcItem._meta.fields["associations"]
        with pytest.raises(ValueError, match="self-loop"):
            field.link(CoOcItem, "pk_a", "pk_a")

    def test_link_idempotent(self):
        """Linking twice keeps original weight."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.2)
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.9)

        linked = field.get_linked(CoOcItem, "pk_a")
        assert len(linked) == 1
        # Should keep original weight (0.2), not overwrite with 0.9
        assert abs(linked[0][1] - 0.2) < 1e-9

    def test_link_rejects_overcap_initial_weight(self):
        """link() with initial_weight > CO_OCCURRENCE_WEIGHT_CAP raises ValueError."""
        field = CoOcItem._meta.fields["associations"]
        with pytest.raises(ValueError, match="CO_OCCURRENCE_WEIGHT_CAP"):
            field.link(
                CoOcItem,
                "pk_a",
                "pk_b",
                initial_weight=Defaults.CO_OCCURRENCE_WEIGHT_CAP + 0.5,
            )


# --- Strengthen Tests ---


class TestStrengthen:
    def test_strengthen_increments_weight(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)
        new_weight = field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05)

        assert abs(new_weight - 0.15) < 1e-9

    def test_strengthen_symmetric(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)
        field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05)

        # Check forward
        forward = field.get_linked(CoOcItem, "pk_a")
        assert abs(forward[0][1] - 0.15) < 1e-9

        # Check reverse
        reverse = field.get_linked(CoOcItem, "pk_b")
        assert abs(reverse[0][1] - 0.15) < 1e-9

    def test_strengthen_negative_delta_raises(self):
        field = CoOcItem._meta.fields["associations"]
        with pytest.raises(ValueError, match="delta must be > 0"):
            field.strengthen(CoOcItem, "pk_a", "pk_b", delta=-0.05)

    def test_strengthen_zero_delta_raises(self):
        field = CoOcItem._meta.fields["associations"]
        with pytest.raises(ValueError, match="delta must be > 0"):
            field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0)

    def test_strengthen_clamps_at_cap(self):
        """Repeated strengthen() never lets the stored weight exceed the cap."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)

        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
        # 0.1 + 50 * 0.05 = 2.6 >> cap=1.0
        last = 0.1
        for _ in range(50):
            last = field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05)
            assert last <= cap + 1e-9, f"weight {last} exceeded cap {cap}"

        # At saturation the returned weight equals the cap.
        assert abs(last - cap) < 1e-9

        # Read back from the ZSET to confirm the stored score matches.
        linked = field.get_linked(CoOcItem, "pk_a", min_weight=0.0)
        assert abs(linked[0][1] - cap) < 1e-9

    def test_strengthen_clamp_symmetric(self):
        """Symmetric strengthen() clamps both directions at the cap."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)

        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
        for _ in range(50):
            field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05)

        forward = field.get_linked(CoOcItem, "pk_a", min_weight=0.0)
        reverse = field.get_linked(CoOcItem, "pk_b", min_weight=0.0)
        assert abs(forward[0][1] - cap) < 1e-9
        assert abs(reverse[0][1] - cap) < 1e-9

    def test_strengthen_nonexistent_edge_clamps(self):
        """strengthen() on a missing edge creates it at min(delta, cap)."""
        field = CoOcItem._meta.fields["associations"]
        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP

        # delta <= cap: edge created at delta.
        w_small = field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05)
        assert abs(w_small - 0.05) < 1e-9
        linked = field.get_linked(CoOcItem, "pk_a", min_weight=0.0)
        assert abs(linked[0][1] - 0.05) < 1e-9

        # delta > cap: edge created at cap.
        w_big = field.strengthen(CoOcItem, "pk_c", "pk_d", delta=cap + 0.5)
        assert abs(w_big - cap) < 1e-9
        linked_big = field.get_linked(CoOcItem, "pk_c", min_weight=0.0)
        assert abs(linked_big[0][1] - cap) < 1e-9


# --- Unlink Tests ---


class TestUnlink:
    def test_unlink_removes_edge(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b")
        field.unlink(CoOcItem, "pk_a", "pk_b")

        assert len(field.get_linked(CoOcItem, "pk_a")) == 0

    def test_unlink_symmetric_removes_both(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b")
        field.unlink(CoOcItem, "pk_a", "pk_b")

        assert len(field.get_linked(CoOcItem, "pk_a")) == 0
        assert len(field.get_linked(CoOcItem, "pk_b")) == 0

    def test_unlink_nonexistent_is_noop(self):
        field = CoOcItem._meta.fields["associations"]
        # Should not raise
        field.unlink(CoOcItem, "pk_a", "pk_b")


# --- Weaken All Tests ---


class TestWeakenAll:
    def test_weaken_all_decays_scores(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=1.0)
        field.link(CoOcItem, "pk_a", "pk_c", initial_weight=0.5)

        field.weaken_all(CoOcItem, "pk_a", factor=0.5)

        linked = field.get_linked(CoOcItem, "pk_a")
        weights = {pk: w for pk, w in linked}
        assert abs(weights["pk_b"] - 0.5) < 1e-9
        assert abs(weights["pk_c"] - 0.25) < 1e-9

    def test_weaken_all_prunes_below_threshold(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.01)
        field.link(CoOcItem, "pk_a", "pk_c", initial_weight=1.0)

        # Weaken so pk_b drops below threshold (0.001)
        removed = field.weaken_all(CoOcItem, "pk_a", factor=0.05)

        linked = field.get_linked(CoOcItem, "pk_a", min_weight=0.0)
        pks = [pk for pk, _ in linked]
        assert "pk_c" in pks
        # pk_b at 0.01 * 0.05 = 0.0005 should be pruned
        assert "pk_b" not in pks

    def test_weaken_all_factor_zero_removes_all(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=1.0)
        field.link(CoOcItem, "pk_a", "pk_c", initial_weight=0.5)

        removed = field.weaken_all(CoOcItem, "pk_a", factor=0)
        assert removed == 2
        assert len(field.get_linked(CoOcItem, "pk_a", min_weight=0)) == 0

    def test_weaken_all_factor_gt_1_raises(self):
        field = CoOcItem._meta.fields["associations"]
        with pytest.raises(ValueError, match="between 0 and 1"):
            field.weaken_all(CoOcItem, "pk_a", factor=1.5)

    def test_weaken_all_no_edges_is_noop(self):
        field = CoOcItem._meta.fields["associations"]
        removed = field.weaken_all(CoOcItem, "pk_nonexistent")
        assert removed == 0


# --- Max Edges Tests ---


class TestMaxEdges:
    def test_max_edges_prunes_lowest(self):
        field = CoOcSmallCap._meta.fields["associations"]  # max_edges=3

        field.link(CoOcSmallCap, "pk_a", "pk_1", initial_weight=0.1)
        field.link(CoOcSmallCap, "pk_a", "pk_2", initial_weight=0.2)
        field.link(CoOcSmallCap, "pk_a", "pk_3", initial_weight=0.3)
        # This 4th link should prune the lowest (pk_1 at 0.1)
        field.link(CoOcSmallCap, "pk_a", "pk_4", initial_weight=0.4)

        linked = field.get_linked(CoOcSmallCap, "pk_a", min_weight=0)
        pks = {pk for pk, _ in linked}
        assert len(pks) == 3
        assert "pk_1" not in pks  # Lowest weight, should be pruned
        assert "pk_4" in pks


# --- Get Linked Tests ---


class TestGetLinked:
    def test_get_linked_sorted_by_weight_desc(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_low", initial_weight=0.1)
        field.link(CoOcItem, "pk_a", "pk_high", initial_weight=0.9)
        field.link(CoOcItem, "pk_a", "pk_mid", initial_weight=0.5)

        linked = field.get_linked(CoOcItem, "pk_a")
        pks = [pk for pk, _ in linked]
        assert pks == ["pk_high", "pk_mid", "pk_low"]

    def test_get_linked_min_weight_filter(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_low", initial_weight=0.01)
        field.link(CoOcItem, "pk_a", "pk_high", initial_weight=0.5)

        linked = field.get_linked(CoOcItem, "pk_a", min_weight=0.1)
        assert len(linked) == 1
        assert linked[0][0] == "pk_high"

    def test_get_linked_limit(self):
        field = CoOcItem._meta.fields["associations"]
        for i in range(10):
            field.link(CoOcItem, "pk_a", f"pk_{i}", initial_weight=0.1 * (i + 1))

        linked = field.get_linked(CoOcItem, "pk_a", limit=3)
        assert len(linked) == 3

    def test_get_linked_empty(self):
        field = CoOcItem._meta.fields["associations"]
        linked = field.get_linked(CoOcItem, "pk_nonexistent")
        assert linked == []

    def test_get_linked_min_weight_filters_all(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.01)
        linked = field.get_linked(CoOcItem, "pk_a", min_weight=1.0)
        assert linked == []


# --- Propagate Tests ---


class TestPropagate:
    def test_propagate_linear_chain(self):
        """A -> B -> C with depth=2 should find C."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "A", "B", initial_weight=1.0)
        field.link(CoOcItem, "B", "C", initial_weight=1.0)

        scores = field.propagate(CoOcItem, ["A"], depth=2, decay_per_hop=0.5)
        assert "B" in scores
        assert "C" in scores
        # B weight: 1.0 * 0.5 * 1.0 = 0.5
        assert abs(scores["B"] - 0.5) < 1e-6
        # C weight: 0.5 * 0.5 * 1.0 = 0.25
        assert abs(scores["C"] - 0.25) < 1e-6

    def test_propagate_star_graph(self):
        """A -> B, A -> C, A -> D with depth=1."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "A", "B", initial_weight=0.8)
        field.link(CoOcItem, "A", "C", initial_weight=0.6)
        field.link(CoOcItem, "A", "D", initial_weight=0.4)

        scores = field.propagate(CoOcItem, ["A"], depth=1, decay_per_hop=0.5)
        assert len(scores) == 3
        assert abs(scores["B"] - 0.4) < 1e-6  # 1.0 * 0.5 * 0.8
        assert abs(scores["C"] - 0.3) < 1e-6  # 1.0 * 0.5 * 0.6
        assert abs(scores["D"] - 0.2) < 1e-6  # 1.0 * 0.5 * 0.4

    def test_propagate_multi_path_uses_max(self):
        """A -> B -> D and A -> C -> D: D's weight should be max of both paths."""
        field = CoOcAsymmetric._meta.fields["associations"]
        field.link(CoOcAsymmetric, "A", "B", initial_weight=1.0)
        field.link(CoOcAsymmetric, "A", "C", initial_weight=0.5)
        field.link(CoOcAsymmetric, "B", "D", initial_weight=1.0)
        field.link(CoOcAsymmetric, "C", "D", initial_weight=1.0)

        scores = field.propagate(CoOcAsymmetric, ["A"], depth=2, decay_per_hop=0.5)
        # Path A->B->D: 1.0 * 0.5 * 1.0 * 0.5 * 1.0 = 0.25
        # Path A->C->D: 1.0 * 0.5 * 0.5 * 0.5 * 1.0 = 0.125
        # max(0.25, 0.125) = 0.25
        assert "D" in scores
        assert abs(scores["D"] - 0.25) < 1e-6

    def test_propagate_depth_zero(self):
        """depth=0 returns only seeds with weight 1.0."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "A", "B", initial_weight=1.0)

        scores = field.propagate(CoOcItem, ["A"], depth=0)
        assert scores == {"A": 1.0}

    def test_propagate_respects_threshold(self):
        """Edges with propagated weight below threshold are excluded."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "A", "B", initial_weight=0.01)

        scores = field.propagate(
            CoOcItem, ["A"], depth=1, decay_per_hop=0.5, threshold=0.1
        )
        # 1.0 * 0.5 * 0.01 = 0.005 < 0.1 threshold
        assert "B" not in scores

    def test_propagate_empty_seeds(self):
        scores = CoOcItem._meta.fields["associations"].propagate(CoOcItem, [], depth=2)
        assert scores == {}

    def test_propagate_seed_with_no_edges(self):
        scores = CoOcItem._meta.fields["associations"].propagate(
            CoOcItem, ["orphan"], depth=2
        )
        assert scores == {}

    def test_propagate_monotonic_with_strengthened_edges(self):
        """5-node chain a-b-c-d-e with all edges at cap: activation is
        monotonically non-increasing in hop count and never exceeds 1.0.

        This is the issue's reproduction — currently fails with
        b:1.95, c:2.44, d:1.95, e:2.44 (non-monotonic, > 1.0).
        """
        field = CoOcItem._meta.fields["associations"]
        chain = ["a", "b", "c", "d", "e"]
        for i in range(len(chain) - 1):
            field.link(CoOcItem, chain[i], chain[i + 1], initial_weight=0.1)
            # Strengthen each edge to cap (1.0).
            for _ in range(50):
                field.strengthen(CoOcItem, chain[i], chain[i + 1], delta=0.05)

        scores = field.propagate(
            CoOcItem, ["a"], depth=4, decay_per_hop=0.5, threshold=0.01
        )

        b, c, d, e = scores["b"], scores["c"], scores["d"], scores["e"]
        # All activations <= 1.0 (the seed weight).
        assert b <= 1.0 + 1e-9
        assert c <= 1.0 + 1e-9
        assert d <= 1.0 + 1e-9
        assert e <= 1.0 + 1e-9
        # Monotonically non-increasing in hop count.
        assert b >= c - 1e-9
        assert c >= d - 1e-9
        assert d >= e - 1e-9

    def test_propagate_threshold_terminates_strengthened_graph(self):
        """On a strengthened chain, threshold pruning fires before the
        far end is reached, so the result set is smaller than the graph."""
        field = CoOcItem._meta.fields["associations"]
        chain = ["a", "b", "c", "d", "e", "f", "g"]
        for i in range(len(chain) - 1):
            field.link(CoOcItem, chain[i], chain[i + 1], initial_weight=0.1)
            for _ in range(50):
                field.strengthen(CoOcItem, chain[i], chain[i + 1], delta=0.05)

        # decay 0.5 per hop, threshold 0.05:
        #   b=0.5, c=0.25, d=0.125, e=0.0625 (>= 0.05, kept)
        #   f=0.03125 (< 0.05, pruned), g never reached.
        scores = field.propagate(
            CoOcItem, ["a"], depth=6, decay_per_hop=0.5, threshold=0.05
        )

        total_non_seed = len(chain) - 1
        assert len(scores) < total_non_seed
        assert "f" not in scores
        assert "g" not in scores
        # The near end is still reached.
        assert "b" in scores

    def test_propagate_read_time_min_for_overcap_weights(self):
        """A manually-ZADDed over-cap stored weight is clamped at read time,
        so propagated activation <= decay_per_hop * cap (not decay_per_hop * raw)."""
        field = CoOcItem._meta.fields["associations"]
        # Bypass link()/strengthen() to inject a raw over-cap score.
        source_key = field.get_edge_key(CoOcItem, "a")
        POPOTO_REDIS_DB.zadd(source_key, {"b": 2.5})

        scores = field.propagate(
            CoOcItem, ["a"], depth=1, decay_per_hop=0.5, threshold=0.01
        )

        cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
        # Effective weight = min(2.5, 1.0) = 1.0; propagated = 1.0 * 0.5 * 1.0 = 0.5.
        # Without the read-time clamp it would be 1.0 * 0.5 * 2.5 = 1.25.
        assert "b" in scores
        assert scores["b"] <= 0.5 * cap + 1e-6
        assert abs(scores["b"] - 0.5 * cap) < 1e-6

    def test_propagate_guard_raises_on_invariant_violation(self):
        """propagate() raises ValueError when cap * decay_per_hop >= 1.0."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "a", "b", initial_weight=0.5)
        with pytest.raises(ValueError, match="Contraction invariant"):
            field.propagate(CoOcItem, ["a"], depth=2, decay_per_hop=1.5)

    def test_propagate_guard_passes_at_defaults(self):
        """propagate() at default cap=1.0 and decay_per_hop=0.5 does not raise."""
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "a", "b", initial_weight=0.5)
        # Should complete normally without ValueError.
        scores = field.propagate(CoOcItem, ["a"], depth=2)
        assert "b" in scores


# --- On Delete Tests ---


class TestOnDelete:
    def test_on_delete_cleans_up_edges(self):
        """Deleting an instance removes its edges and reverse edges."""
        field = CoOcItem._meta.fields["associations"]

        item_a = CoOcItem.create(name="a", content="A")
        item_b = CoOcItem.create(name="b", content="B")
        item_c = CoOcItem.create(name="c", content="C")

        pk_a = item_a.db_key.redis_key
        pk_b = item_b.db_key.redis_key
        pk_c = item_c.db_key.redis_key

        field.link(CoOcItem, pk_a, pk_b, initial_weight=0.5)
        field.link(CoOcItem, pk_a, pk_c, initial_weight=0.3)

        # Delete item_a
        item_a.delete()

        # A's edge set should be gone
        assert len(field.get_linked(CoOcItem, pk_a)) == 0
        # B and C should no longer have reverse edges to A
        linked_b = field.get_linked(CoOcItem, pk_b)
        pks_b = [pk for pk, _ in linked_b]
        assert pk_a not in pks_b

        linked_c = field.get_linked(CoOcItem, pk_c)
        pks_c = [pk for pk, _ in linked_c]
        assert pk_a not in pks_c


# --- Pipeline Tests ---


class TestPipeline:
    def test_strengthen_with_pipeline(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)

        pipe = POPOTO_REDIS_DB.pipeline()
        field.strengthen(CoOcItem, "pk_a", "pk_b", delta=0.05, pipeline=pipe)
        pipe.execute()

        linked = field.get_linked(CoOcItem, "pk_a")
        assert abs(linked[0][1] - 0.15) < 1e-9

    def test_unlink_with_pipeline(self):
        field = CoOcItem._meta.fields["associations"]
        field.link(CoOcItem, "pk_a", "pk_b", initial_weight=0.1)

        pipe = POPOTO_REDIS_DB.pipeline()
        field.unlink(CoOcItem, "pk_a", "pk_b", pipeline=pipe)
        pipe.execute()

        assert len(field.get_linked(CoOcItem, "pk_a")) == 0


# --- Synergy Tests ---


class TestSynergyWithDecayingSortedField:
    def test_propagated_weights_boost_retrieval(self):
        """CoOccurrenceField propagation can identify related records
        that could be used to boost DecayingSortedField retrieval."""
        field = CoOcWithDecay._meta.fields["associations"]

        item_a = CoOcWithDecay.create(name="a", content="A", strength=1.0)
        item_b = CoOcWithDecay.create(name="b", content="B", strength=0.5)
        item_c = CoOcWithDecay.create(name="c", content="C", strength=0.3)

        pk_a = item_a.db_key.redis_key
        pk_b = item_b.db_key.redis_key
        pk_c = item_c.db_key.redis_key

        # Create associations
        field.link(CoOcWithDecay, pk_a, pk_b, initial_weight=0.8)
        field.link(CoOcWithDecay, pk_b, pk_c, initial_weight=0.6)

        # Propagate from A should find B and C
        scores = field.propagate(CoOcWithDecay, [pk_a], depth=2, decay_per_hop=0.5)
        assert pk_b in scores
        assert pk_c in scores

        # B should have higher propagated weight than C
        assert scores[pk_b] > scores[pk_c]


class TestSynergyWithAccessTracker:
    def test_co_accessed_records_can_be_linked(self):
        """AccessTracker confirms access, then strengthen() links records."""
        field = CoOcWithTracker._meta.fields["associations"]

        item_a = CoOcWithTracker.create(name="a", content="A")
        item_b = CoOcWithTracker.create(name="b", content="B")

        pk_a = item_a.db_key.redis_key
        pk_b = item_b.db_key.redis_key

        # Simulate co-access
        item_a.on_read()
        item_a.confirm_access()
        item_b.on_read()
        item_b.confirm_access()

        # Link them based on co-access
        field.link(CoOcWithTracker, pk_a, pk_b, initial_weight=0.1)
        field.strengthen(CoOcWithTracker, pk_a, pk_b, delta=0.1)

        linked = field.get_linked(CoOcWithTracker, pk_a)
        assert len(linked) == 1
        assert abs(linked[0][1] - 0.2) < 1e-9

        # Verify access counts are maintained independently
        assert item_a.access_count == 1
        assert item_b.access_count == 1


class TestSynergyWithConfidenceField:
    def test_confidence_modulates_effective_weight(self):
        """ConfidenceField value can modulate effective edge weight."""
        co_field = CoOcWithConfidence._meta.fields["associations"]

        item_a = CoOcWithConfidence.create(name="a", content="A")
        item_b = CoOcWithConfidence.create(name="b", content="B")

        pk_a = item_a.db_key.redis_key
        pk_b = item_b.db_key.redis_key

        # Create association
        co_field.link(CoOcWithConfidence, pk_a, pk_b, initial_weight=1.0)

        # Update confidence for item_a
        ConfidenceField.update_confidence(item_a, "certainty", signal=0.9)
        conf = ConfidenceField.get_confidence(item_a, "certainty")

        # Get edge weight
        linked = co_field.get_linked(CoOcWithConfidence, pk_a)
        edge_weight = linked[0][1]

        # Application layer would multiply: effective_weight = edge_weight * conf
        effective_weight = edge_weight * conf
        assert effective_weight > 0
        assert effective_weight <= edge_weight  # confidence <= 1.0
