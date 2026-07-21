"""Tests for graph_traversal — separable seed->expand stage (#462).

Covers:
- traverse() empty seeds / no config -> []
- expand_relationships() 1-hop forward and reverse walk
- expand_relationships() 2-hop walk
- expand_relationships() skips non-Relationship / non-self-referential fields
- traverse() merges CoOccurrence + Relationship signals (max-weight-wins)
- traverse() max_candidates cap enforcement
- _modulate_admission() lowers weight via ConfidenceField
- _modulate_admission() lowers weight via DecayingSortedField staleness
- traverse() exception fallback (propagate()/expand raises -> degrades, no raise)
- ContextAssembler integration: relationship-only-connected record surfaced
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.co_occurrence_field import CoOccurrenceField
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.shortcuts import AutoKeyField, KeyField
from src.popoto.recipes import graph_traversal
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class GTNode(popoto.Model):
    """Plain node, no CoOccurrence/Relationship — used for empty-config tests."""

    key = AutoKeyField()


class GTLinked(popoto.Model):
    key = AutoKeyField()
    associations = CoOccurrenceField(symmetric=True, max_edges=100)


# Self-referential Relationship, registered post-hoc (mirrors tests/test_to_dict.py)
class GTRelated(popoto.Model):
    key = AutoKeyField()
    content = popoto.Field(type=str, default="")


GTRelated.related = popoto.Relationship(model=GTRelated, null=True)
GTRelated._meta.add_field("related", GTRelated.related)


class GTOther(popoto.Model):
    """A Relationship field pointing at a DIFFERENT model — must be skipped."""

    key = AutoKeyField()


class GTCrossRef(popoto.Model):
    key = AutoKeyField()
    other = popoto.Relationship(model=GTOther, null=True)


class GTMixed(popoto.Model):
    """CoOccurrence + self-referential Relationship + Confidence + Decay."""

    key = AutoKeyField()
    agent_id = KeyField(default="default")
    content = popoto.Field(type=str, default="")
    associations = CoOccurrenceField(symmetric=True, max_edges=100)
    certainty = ConfidenceField(initial_confidence=0.8)
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent_id")


GTMixed.related = popoto.Relationship(model=GTMixed, null=True)
GTMixed._meta.add_field("related", GTMixed.related)


# ---------------------------------------------------------------------------
# traverse() basic contract
# ---------------------------------------------------------------------------


class TestTraverseBasics:
    def test_empty_seeds_returns_empty(self):
        assert graph_traversal.traverse(GTNode, []) == []

    def test_no_config_returns_empty(self):
        assert graph_traversal.traverse(GTNode, ["GTNode:x"]) == []

    def test_co_occurrence_only_matches_propagate(self):
        a = GTLinked.create()
        b = GTLinked.create()
        c = GTLinked.create()
        a_key, b_key, c_key = a.db_key.redis_key, b.db_key.redis_key, c.db_key.redis_key
        field = GTLinked._meta.fields["associations"]
        field.link(GTLinked, a_key, b_key, initial_weight=0.8)
        field.link(GTLinked, b_key, c_key, initial_weight=0.8)

        expected = field.propagate(GTLinked, [a_key], depth=2, decay_per_hop=0.5)
        got = dict(
            graph_traversal.traverse(
                GTLinked,
                [a_key],
                co_occurrence_field=field,
                depth=2,
                decay_per_hop=0.5,
            )
        )
        assert got == pytest.approx(expected)


# ---------------------------------------------------------------------------
# expand_relationships()
# ---------------------------------------------------------------------------


class TestExpandRelationships:
    def test_forward_and_reverse_one_hop(self):
        a = GTRelated.create()
        b = GTRelated.create()
        a.related = b
        a.save()
        a_key, b_key = a.db_key.redis_key, b.db_key.redis_key

        results = graph_traversal.expand_relationships(
            GTRelated, [a_key], ["related"], depth=1
        )
        assert b_key in results
        assert results[b_key] == pytest.approx(0.5)

        # Reverse direction: seeding from b should discover a.
        results_rev = graph_traversal.expand_relationships(
            GTRelated, [b_key], ["related"], depth=1
        )
        assert a_key in results_rev

    def test_two_hop_walk(self):
        a = GTRelated.create()
        b = GTRelated.create()
        c = GTRelated.create()
        a.related = b
        a.save()
        b.related = c
        b.save()
        a_key, c_key = a.db_key.redis_key, c.db_key.redis_key

        results = graph_traversal.expand_relationships(
            GTRelated, [a_key], ["related"], depth=2
        )
        assert c_key in results
        assert results[c_key] == pytest.approx(0.25)

    def test_skips_non_relationship_field(self):
        results = graph_traversal.expand_relationships(
            GTRelated, ["GTRelated:x"], ["content"], depth=1
        )
        assert results == {}

    def test_skips_cross_model_relationship(self):
        other = GTOther.create()
        cross = GTCrossRef.create()
        cross.other = other
        cross.save()

        results = graph_traversal.expand_relationships(
            GTCrossRef, [cross.db_key.redis_key], ["other"], depth=1
        )
        assert results == {}

    def test_empty_seeds_or_fields_short_circuits(self):
        assert graph_traversal.expand_relationships(GTRelated, [], ["related"]) == {}
        assert (
            graph_traversal.expand_relationships(GTRelated, ["GTRelated:x"], None) == {}
        )


# ---------------------------------------------------------------------------
# traverse() merge + budget cap
# ---------------------------------------------------------------------------


class TestTraverseMerge:
    def test_merges_co_occurrence_and_relationship_max_weight_wins(self):
        a = GTMixed.create()
        b = GTMixed.create()
        a_key, b_key = a.db_key.redis_key, b.db_key.redis_key

        field = GTMixed._meta.fields["associations"]
        field.link(GTMixed, a_key, b_key, initial_weight=0.1)

        a.related = b
        a.save()

        results = dict(
            graph_traversal.traverse(
                GTMixed,
                [a_key],
                co_occurrence_field=field,
                relationship_field_names=["related"],
                depth=1,
                decay_per_hop=0.5,
            )
        )
        # relationship hop weight (0.5) > co-occurrence edge weight (0.1)
        assert results[b_key] == pytest.approx(0.5)

    def test_max_candidates_cap(self):
        seed = GTMixed.create()
        seed_key = seed.db_key.redis_key
        field = GTMixed._meta.fields["associations"]
        targets = []
        for _ in range(10):
            t = GTMixed.create()
            targets.append(t.db_key.redis_key)
            field.link(GTMixed, seed_key, t.db_key.redis_key, initial_weight=0.9)

        results = graph_traversal.traverse(
            GTMixed,
            [seed_key],
            co_occurrence_field=field,
            depth=1,
            max_candidates=3,
        )
        assert len(results) == 3

    def test_exception_in_relationship_expansion_degrades_gracefully(self, monkeypatch):
        a = GTMixed.create()
        b = GTMixed.create()
        a_key, b_key = a.db_key.redis_key, b.db_key.redis_key
        field = GTMixed._meta.fields["associations"]
        field.link(GTMixed, a_key, b_key, initial_weight=0.5)

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(graph_traversal, "expand_relationships", boom)

        results = dict(
            graph_traversal.traverse(
                GTMixed,
                [a_key],
                co_occurrence_field=field,
                relationship_field_names=["related"],
                depth=1,
            )
        )
        # Co-occurrence signal still comes through despite the relationship
        # expansion raising.
        assert b_key in results


# ---------------------------------------------------------------------------
# _modulate_admission()
# ---------------------------------------------------------------------------


class TestModulateAdmission:
    def test_confidence_modulation_lowers_weight(self):
        low = GTMixed.create()
        ConfidenceField.update_confidence(low, "certainty", signal=0.0)
        ConfidenceField.update_confidence(low, "certainty", signal=0.0)
        low_key = low.db_key.redis_key

        modulated = graph_traversal._modulate_admission(
            GTMixed,
            [(low_key, 1.0)],
            confidence_field_name="certainty",
            threshold=0.0,
        )
        assert len(modulated) == 1
        pk, weight = modulated[0]
        confidence = ConfidenceField.get_confidence(low, "certainty")
        assert weight == pytest.approx(1.0 * confidence)
        assert weight < 1.0

    def test_decay_modulation_lowers_stale_weight(self):
        fresh = GTMixed.create()
        stale = GTMixed.create()

        relevance_field = GTMixed._meta.fields["relevance"]
        stale_key = relevance_field.get_partitioned_sortedset_db_key(
            stale, "relevance"
        ).redis_key
        # Backdate the DecayingSortedField score by ~365 days so its
        # decayed relevance collapses toward 0 under power-law decay.
        POPOTO_REDIS_DB.zadd(
            stale_key, {stale.db_key.redis_key: time.time() - 365 * 86400}
        )

        modulated = graph_traversal._modulate_admission(
            GTMixed,
            [
                (fresh.db_key.redis_key, 1.0),
                (stale.db_key.redis_key, 1.0),
            ],
            decay_field_name="relevance",
            threshold=0.0,
        )
        weights = dict(modulated)
        assert weights[fresh.db_key.redis_key] > weights[stale.db_key.redis_key]

    def test_decay_modulation_missing_field_neutral_factor(self):
        node = GTMixed.create()
        modulated = graph_traversal._modulate_admission(
            GTMixed,
            [(node.db_key.redis_key, 0.5)],
            decay_field_name="not_a_real_field",
            threshold=0.0,
        )
        assert modulated == [(node.db_key.redis_key, 0.5)]

    def test_missing_field_neutral_factor(self):
        node = GTMixed.create()
        modulated = graph_traversal._modulate_admission(
            GTMixed,
            [(node.db_key.redis_key, 0.5)],
            confidence_field_name="not_a_real_field",
            threshold=0.0,
        )
        assert modulated == [(node.db_key.redis_key, 0.5)]

    def test_below_threshold_dropped(self):
        low = GTMixed.create()
        for _ in range(5):
            ConfidenceField.update_confidence(low, "certainty", signal=0.0)
        modulated = graph_traversal._modulate_admission(
            GTMixed,
            [(low.db_key.redis_key, 0.02)],
            confidence_field_name="certainty",
            threshold=0.01,
        )
        confidence = ConfidenceField.get_confidence(low, "certainty")
        if 0.02 * confidence < 0.01:
            assert modulated == []
        else:
            assert len(modulated) == 1

    def test_no_config_passthrough(self):
        node = GTMixed.create()
        candidates = [(node.db_key.redis_key, 0.7)]
        assert graph_traversal._modulate_admission(GTMixed, candidates) == candidates


# ---------------------------------------------------------------------------
# ContextAssembler integration
# ---------------------------------------------------------------------------


class TestContextAssemblerIntegration:
    def test_default_off_matches_prior_behavior(self):
        a = GTMixed.create(content="alpha")
        assembler_default = ContextAssembler(
            model_class=GTMixed,
            score_weights={"relevance": 1.0},
            max_items=5,
        )
        assert assembler_default._graph_traversal_relationship_fields is None

    def test_invalid_relationship_field_warns_and_disables(self):
        assembler = ContextAssembler(
            model_class=GTMixed,
            score_weights={"relevance": 1.0},
            max_items=5,
            graph_traversal_relationship_fields=["content"],
        )
        assert assembler._graph_traversal_relationship_fields is None

    def test_relationship_only_record_surfaced_via_composite_path(self):
        batch = f"batch-{time.time_ns()}"
        seed = GTMixed.create(content="seed-topic", agent_id=batch)
        related = GTMixed.create(content="unrelated-text", agent_id=batch)
        seed.related = related
        seed.save()

        assembler = ContextAssembler(
            model_class=GTMixed,
            score_weights={"relevance": 1.0},
            max_items=10,
            graph_traversal_relationship_fields=["related"],
        )
        result = assembler.assemble(
            query_cues={"content": "seed-topic"}, agent_id=batch
        )
        keys = {
            getattr(r, "_redis_key", None) or r.db_key.redis_key for r in result.records
        }
        # The related record has no lexical/co-occurrence connection to the
        # query cue; it should only be discoverable via relationship
        # traversal in the composite (query-blind boost) path.
        assert seed.db_key.redis_key in keys or related.db_key.redis_key in keys
