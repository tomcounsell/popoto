"""Adoption ladder integration tests for the Agent Memory quickstart guide.

Each test class validates one adoption level, proving the DX works
from zero to full-stack agent memory. Models at each level are
independently useful and backward-compatible with previous levels.

Levels:
    1. Recall — DecayingSortedField: time-weighted retrieval
    2. Attention — + AccessTrackerMixin + WriteFilterMixin: noise filtering, read tracking
    3. Learning — + ConfidenceField + ObservationProtocol: outcome-driven effects
    4. Association — + CompositeScoreQuery + CoOccurrenceField: multi-factor ranking
    5. Cognition — + ContextAssembler: full LLM-ready context assembly
"""

import time

import pytest

from popoto import (
    AccessTrackerMixin,
    AutoKeyField,
    ConfidenceField,
    ContextAssembler,
    CoOccurrenceField,
    DecayingSortedField,
    FloatField,
    KeyField,
    Model,
    ObservationProtocol,
    StringField,
    WriteFilterMixin,
)
from popoto.redis_db import POPOTO_REDIS_DB


# ---------------------------------------------------------------------------
# Level 1: Recall — DecayingSortedField only
# ---------------------------------------------------------------------------


class RecallMemory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )


# ---------------------------------------------------------------------------
# Level 2: Attention — + AccessTrackerMixin + WriteFilterMixin
# ---------------------------------------------------------------------------


class AttentionMemory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0


# ---------------------------------------------------------------------------
# Level 3: Learning — + ConfidenceField + ObservationProtocol
# ---------------------------------------------------------------------------


class LearningMemory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0


# ---------------------------------------------------------------------------
# Level 4: Association — + CoOccurrenceField + CompositeScoreQuery
# ---------------------------------------------------------------------------


class AssociationMemory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0


# ---------------------------------------------------------------------------
# Level 5: Cognition — + ContextAssembler
# ---------------------------------------------------------------------------


class CognitionMemory(WriteFilterMixin, AccessTrackerMixin, Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.importance or 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    """Remove all Redis keys matching the given patterns."""
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    """Clean all adoption ladder model keys from Redis."""
    _clean_keys(
        "*RecallMemory*",
        "*AttentionMemory*",
        "*LearningMemory*",
        "*AssociationMemory*",
        "*CognitionMemory*",
        "$EF:*Memory*",
        "$FS:*Memory*",
        "$CoOcF:*Memory*",
        "$ConfidencF:*Memory*",
        "$SortedF:*Memory*",
        "$AT:*Memory*",
        "$WF:*Memory*",
    )


@pytest.fixture(autouse=True)
def clean_redis():
    """Clean Redis before and after each test."""
    _clean_all()
    yield
    _clean_all()


# ===========================================================================
# Level 1: Recall
# ===========================================================================


class TestLevel1Recall:
    """DecayingSortedField gives time-weighted retrieval out of the box."""

    def test_decay_returns_saved_records(self):
        """top_by_decay retrieves all saved records."""
        RecallMemory(agent_id="agent-1", content="fact one", importance=1.0).save()
        RecallMemory(agent_id="agent-1", content="fact two", importance=1.0).save()

        results = RecallMemory.query.filter(agent_id="agent-1").top_by_decay(n=10)
        assert len(results) == 2
        contents = {r.content for r in results}
        assert "fact one" in contents
        assert "fact two" in contents

    def test_importance_affects_ranking(self):
        """Higher importance boosts a memory's decayed score."""
        RecallMemory(
            agent_id="agent-1", content="routine", importance=1.0
        ).save()
        time.sleep(0.05)
        RecallMemory(
            agent_id="agent-1", content="critical", importance=10.0
        ).save()

        results = RecallMemory.query.filter(agent_id="agent-1").top_by_decay(n=2)
        assert results[0].content == "critical"

    def test_partition_isolates_agents(self):
        """Each agent_id has its own sorted index."""
        RecallMemory(agent_id="agent-1", content="a1").save()
        RecallMemory(agent_id="agent-2", content="a2").save()

        r1 = RecallMemory.query.filter(agent_id="agent-1").top_by_decay(n=10)
        r2 = RecallMemory.query.filter(agent_id="agent-2").top_by_decay(n=10)

        assert len(r1) == 1
        assert r1[0].content == "a1"
        assert len(r2) == 1
        assert r2[0].content == "a2"


# ===========================================================================
# Level 2: Attention
# ===========================================================================


class TestLevel2Attention:
    """WriteFilterMixin discards noise; AccessTrackerMixin tracks reads."""

    def test_low_importance_discarded(self):
        """Records below min_threshold are silently dropped (save returns False)."""
        m = AttentionMemory(agent_id="agent-1", content="noise", importance=0.1)
        result = m.save()
        assert result is False  # WriteFilter catches SkipSaveException internally

        results = AttentionMemory.query.filter(agent_id="agent-1").top_by_decay(n=10)
        assert len(results) == 0

    def test_high_importance_persisted(self):
        """Records above min_threshold are saved normally."""
        m = AttentionMemory(agent_id="agent-1", content="useful", importance=0.5)
        m.save()

        results = AttentionMemory.query.filter(agent_id="agent-1").top_by_decay(n=10)
        assert len(results) == 1
        assert results[0].content == "useful"

    def test_priority_tagging(self):
        """Records above priority_threshold get tagged in the priority set."""
        m = AttentionMemory(agent_id="agent-1", content="critical", importance=0.9)
        m.save()

        priority_key = "$WF:AttentionMemory:priority"
        members = POPOTO_REDIS_DB.zrange(priority_key, 0, -1)
        assert len(members) > 0

    def test_access_tracking(self):
        """Reading a record via query stages an access event."""
        AttentionMemory(agent_id="agent-1", content="tracked", importance=0.5).save()
        results = AttentionMemory.query.filter(agent_id="agent-1").top_by_decay(n=1)
        assert len(results) == 1

        # Confirm the staged access
        results[0].confirm_access()


# ===========================================================================
# Level 3: Learning
# ===========================================================================


class TestLevel3Learning:
    """ConfidenceField + ObservationProtocol enable outcome-driven effects."""

    def test_confidence_updates(self):
        """Corroborating evidence strengthens confidence."""
        m = LearningMemory(agent_id="agent-1", content="hypothesis", importance=0.8)
        m.save()

        initial = ConfidenceField.get_confidence(m, "confidence")
        assert initial is not None

        # Corroborate: signal > 0.5 strengthens
        ConfidenceField.update_confidence(m, "confidence", signal=0.9)
        updated = ConfidenceField.get_confidence(m, "confidence")
        assert updated > initial

    def test_contradiction_weakens(self):
        """Contradicting evidence weakens confidence."""
        m = LearningMemory(agent_id="agent-1", content="belief", importance=0.8)
        m.save()

        initial = ConfidenceField.get_confidence(m, "confidence")

        # Contradict: signal < 0.5 weakens
        ConfidenceField.update_confidence(m, "confidence", signal=0.1)
        updated = ConfidenceField.get_confidence(m, "confidence")
        assert updated < initial

    def test_observation_outcomes(self):
        """ObservationProtocol applies effects based on how the agent used a memory."""
        m = LearningMemory(agent_id="agent-1", content="actionable", importance=0.8)
        m.save()

        # Simulate the agent acting on this memory
        outcome_map = {m.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([m], outcome_map)

        # After acted: confidence should be strengthened
        conf = ConfidenceField.get_confidence(m, "confidence")
        assert conf > 0.5  # initial was 0.5, acted pushes it up


# ===========================================================================
# Level 4: Association
# ===========================================================================


class TestLevel4Association:
    """CoOccurrenceField + CompositeScoreQuery enable multi-factor retrieval."""

    def test_link_and_retrieve_associations(self):
        """Linked memories can be retrieved via co-occurrence graph."""
        m1 = AssociationMemory(
            agent_id="agent-1", content="deploy process", importance=0.8
        )
        m1.save()
        m2 = AssociationMemory(
            agent_id="agent-1", content="rollback steps", importance=0.8
        )
        m2.save()

        # Link the two memories via the field instance
        pk1 = m1.db_key.redis_key
        pk2 = m2.db_key.redis_key
        assoc_field = AssociationMemory._meta.fields["associations"]
        assoc_field.link(AssociationMemory, pk1, pk2, initial_weight=0.5)

        # Retrieve associations
        linked = assoc_field.get_linked(AssociationMemory, pk1)
        assert len(linked) > 0
        assert any(pk2 in pair[0] for pair in linked)

    def test_composite_score_query(self):
        """CompositeScoreQuery combines multiple sorted indexes."""
        m1 = AssociationMemory(
            agent_id="agent-1", content="high relevance", importance=0.9
        )
        m1.save()
        m2 = AssociationMemory(
            agent_id="agent-1", content="low relevance", importance=0.3
        )
        m2.save()

        # Composite query weighting relevance
        results = (
            AssociationMemory.query.filter(agent_id="agent-1").composite_score(
                indexes={"relevance": 1.0},
                limit=10,
            )
        )
        assert len(results) >= 1


# ===========================================================================
# Level 5: Cognition
# ===========================================================================


class TestLevel5Cognition:
    """ContextAssembler orchestrates all primitives into LLM-ready context."""

    def test_assemble_returns_result(self):
        """ContextAssembler.assemble() produces an AssemblyResult."""
        CognitionMemory(
            agent_id="agent-1", content="deploy checklist", importance=0.9
        ).save()
        CognitionMemory(
            agent_id="agent-1", content="rollback steps", importance=0.8
        ).save()

        assembler = ContextAssembler(
            model_class=CognitionMemory,
            score_weights={"relevance": 1.0},
            max_items=5,
        )
        result = assembler.assemble(
            query_cues={"topic": "deploy"},
            agent_id="agent-1",
        )

        assert hasattr(result, "records")
        assert hasattr(result, "formatted")
        assert hasattr(result, "metadata")

    def test_assembled_context_contains_records(self):
        """Assembled context includes saved records."""
        CognitionMemory(
            agent_id="agent-1", content="important fact", importance=0.9
        ).save()

        assembler = ContextAssembler(
            model_class=CognitionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
        )
        result = assembler.assemble(
            query_cues={"topic": "important"},
            agent_id="agent-1",
        )

        # Should have at least one record
        assert len(result.records) >= 1
        # Formatted output should be non-empty
        assert len(result.formatted) > 0

    def test_token_budget_limits_output(self):
        """Setting max_tokens limits the assembled output size."""
        for i in range(5):
            CognitionMemory(
                agent_id="agent-1",
                content=f"memory number {i} with some content to fill space",
                importance=0.8,
            ).save()

        assembler = ContextAssembler(
            model_class=CognitionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            max_tokens=50,  # very small budget
        )
        result = assembler.assemble(
            query_cues={"topic": "memory"},
            agent_id="agent-1",
        )

        # With a tiny budget, some records should be dropped
        assert len(result.records) < 5
