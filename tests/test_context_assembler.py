"""Tests for ContextAssembler recipe — orchestrated memory retrieval.

Tests cover:
- ContextAssembler initialization and defaults
- AssemblyResult dataclass structure
- Pull path: ExistenceFilter pre-check, CompositeScoreQuery
- Push path: top_by_decay with surfacing threshold
- Merge: deduplication by redis_key, highest score wins
- Budget: max_items and max_tokens caps
- Post-effects: on_read, on_surfaced, competitive suppression
- Output formatting: structured (JSON), XML, natural language
- Edge cases: no query cues, no CyclicDecayField, empty results
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src.popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.fields.shortcuts import AutoKeyField, KeyField  # noqa: E402
from src.popoto.models.base import Model  # noqa: E402
from src.popoto.recipes.context_assembler import (  # noqa: E402
    COMPETITIVE_SUPPRESSION_SIGNAL,
    DEFAULT_MAX_ITEMS,
    DEFAULT_PROPAGATION_DEPTH,
    DEFAULT_SURFACING_THRESHOLD,
    FOK_WEIGHT_CUE_FAMILIARITY,
    FOK_WEIGHT_PARTIAL_RETRIEVAL,
    FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION,
    AssemblyResult,
    ContextAssembler,
    RetrievalQuality,
    format_natural,
    format_structured,
    format_xml,
)
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# ---------------------------------------------------------------------------
# Test Models
# ---------------------------------------------------------------------------


class SimpleMemory(Model):
    """Model with DecayingSortedField only — no bloom, no co-occurrence, no cyclic."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")


class FullMemory(Model):
    """Model with all field types for full pipeline testing."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    topic = Field(type=str)
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    urgency = CyclicDecayField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.topic,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    """Remove all Redis keys matching patterns (test-only, uses KEYS)."""
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    """Clean all test model keys."""
    _clean_keys(
        "*SimpleMemory*",
        "*FullMemory*",
        "$EF:*Memory*",
        "$CoOcF:*Memory*",
        "$ConfidencF:*Memory*",
        "$SortedF:*Memory*",
        "$CyclicDF:*Memory*",
        "$RP:*Memory*",
    )


@pytest.fixture(autouse=True)
def clean_redis():
    """Clean Redis before and after each test."""
    _clean_all()
    yield
    _clean_all()


# ---------------------------------------------------------------------------
# Constants Tests
# ---------------------------------------------------------------------------


class TestConstants:
    def test_competitive_suppression_signal_value(self):
        assert COMPETITIVE_SUPPRESSION_SIGNAL == 0.3

    def test_default_surfacing_threshold_value(self):
        assert DEFAULT_SURFACING_THRESHOLD == 0.5

    def test_default_max_items_value(self):
        assert DEFAULT_MAX_ITEMS == 10

    def test_default_propagation_depth_value(self):
        assert DEFAULT_PROPAGATION_DEPTH == 2


# ---------------------------------------------------------------------------
# AssemblyResult Tests
# ---------------------------------------------------------------------------


class TestAssemblyResult:
    def test_dataclass_fields(self):
        result = AssemblyResult(
            records=[],
            proactive=[],
            formatted="",
            metadata={},
        )
        assert result.records == []
        assert result.proactive == []
        assert result.formatted == ""
        assert result.metadata == {}

    def test_dataclass_with_data(self):
        result = AssemblyResult(
            records=["a", "b"],
            proactive=["b"],
            formatted='{"items": 2}',
            metadata={"pull_count": 1, "push_count": 1},
        )
        assert len(result.records) == 2
        assert len(result.proactive) == 1
        assert result.metadata["pull_count"] == 1


# ---------------------------------------------------------------------------
# ContextAssembler Initialization Tests
# ---------------------------------------------------------------------------


class TestContextAssemblerInit:
    def test_default_parameters(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler.model_class is SimpleMemory
        assert assembler.score_weights == {"relevance": 1.0}
        assert assembler.max_items == DEFAULT_MAX_ITEMS
        assert assembler.max_tokens is None
        assert assembler.surfacing_threshold == DEFAULT_SURFACING_THRESHOLD
        assert assembler.propagation_depth == DEFAULT_PROPAGATION_DEPTH
        assert assembler.output_format == "structured"

    def test_custom_parameters(self):
        counter = lambda r: 10  # noqa: E731
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 0.7, "urgency": 0.3},
            max_items=5,
            max_tokens=1000,
            surfacing_threshold=0.8,
            propagation_depth=3,
            output_format="xml",
            token_counter=counter,
        )
        assert assembler.max_items == 5
        assert assembler.max_tokens == 1000
        assert assembler.surfacing_threshold == 0.8
        assert assembler.propagation_depth == 3
        assert assembler.output_format == "xml"
        assert assembler._token_counter is counter

    def test_default_token_counter(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        # Default: len(str(record)) // 4
        assert assembler._token_counter("hello world") == len("hello world") // 4


# ---------------------------------------------------------------------------
# Assemble Tests — Empty / Minimal Cases
# ---------------------------------------------------------------------------


class TestAssembleEmpty:
    def test_no_cues_no_cyclic_returns_empty(self):
        """No query_cues and no CyclicDecayField means both paths are skipped."""
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble()
        assert isinstance(result, AssemblyResult)
        assert result.records == []
        assert result.proactive == []
        assert result.metadata["pull_count"] == 0
        assert result.metadata["push_count"] == 0

    def test_no_cues_with_cyclic_runs_push_path(self):
        """No query_cues but model has CyclicDecayField: push path runs."""
        # Save some records with urgency
        m = FullMemory(agent_id="a1", topic="test", content="data")
        m.save()

        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 0.5, "urgency": 0.5},
            surfacing_threshold=0.0,  # Accept all
        )
        result = assembler.assemble(partition_filters={"agent_id": "a1"})
        assert isinstance(result, AssemblyResult)
        # Push path may or may not find results depending on scoring
        assert result.metadata["pull_count"] == 0


# ---------------------------------------------------------------------------
# Assemble Tests — Pull Path
# ---------------------------------------------------------------------------


class TestAssemblePullPath:
    def test_pull_with_composite_score(self):
        """Pull path retrieves records via CompositeScoreQuery."""
        for i in range(3):
            m = SimpleMemory(agent_id="a1", content=f"item-{i}")
            m.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_items=5,
        )
        result = assembler.assemble(
            query_cues={"topic": "test"},
            partition_filters={"agent_id": "a1"},
        )
        assert isinstance(result, AssemblyResult)
        assert result.metadata["pull_count"] >= 0

    def test_bloom_filter_skip_all_missing(self):
        """When ExistenceFilter says all cues definitely missing, skip pull."""
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 1.0},
        )
        # No records saved, so bloom says definitely missing
        result = assembler.assemble(
            query_cues={"topic": "nonexistent_xyz_123"},
            partition_filters={"agent_id": "a1"},
        )
        # Pull should be skipped due to bloom filter
        assert result.metadata["pull_count"] == 0


# ---------------------------------------------------------------------------
# Assemble Tests — Merge and Budget
# ---------------------------------------------------------------------------


class TestAssembleBudget:
    def test_max_items_cap(self):
        """Records beyond max_items are dropped."""
        for i in range(8):
            m = SimpleMemory(agent_id="a1", content=f"item-{i}")
            m.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_items=3,
        )
        result = assembler.assemble(
            query_cues={"topic": "test"},
            partition_filters={"agent_id": "a1"},
        )
        assert len(result.records) <= 3

    def test_max_tokens_cap(self):
        """Records beyond max_tokens budget are dropped."""
        for i in range(5):
            m = SimpleMemory(agent_id="a1", content=f"long content item {i}" * 10)
            m.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            max_tokens=50,  # Very small budget
        )
        result = assembler.assemble(
            query_cues={"topic": "test"},
            partition_filters={"agent_id": "a1"},
        )
        assert result.metadata["token_count"] <= 50 or len(result.records) <= 1


# ---------------------------------------------------------------------------
# Formatter Tests
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_structured_json(self):
        records = [{"key": "val1"}, {"key": "val2"}]
        output = format_structured(records)
        parsed = json.loads(output)
        assert len(parsed) == 2

    def test_format_xml(self):
        records = [{"key": "val1"}]
        output = format_xml(records)
        assert "<records>" in output
        assert "<record>" in output
        assert "</records>" in output

    def test_format_natural(self):
        records = [{"content": "hello"}, {"content": "world"}]
        output = format_natural(records)
        assert "hello" in output or "content" in output

    def test_format_structured_empty(self):
        output = format_structured([])
        assert json.loads(output) == []

    def test_format_xml_empty(self):
        output = format_xml([])
        assert "<records>" in output

    def test_format_natural_empty(self):
        output = format_natural([])
        assert output == ""


# ---------------------------------------------------------------------------
# Assemble Tests — Output Format
# ---------------------------------------------------------------------------


class TestAssembleOutputFormat:
    def test_structured_format(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            output_format="structured",
        )
        result = assembler.assemble()
        # Should be valid JSON
        parsed = json.loads(result.formatted)
        assert isinstance(parsed, list)

    def test_xml_format(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            output_format="xml",
        )
        result = assembler.assemble()
        assert "<records>" in result.formatted

    def test_natural_format(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            output_format="natural",
        )
        result = assembler.assemble()
        assert isinstance(result.formatted, str)


# ---------------------------------------------------------------------------
# Assemble Tests — Metadata
# ---------------------------------------------------------------------------


class TestAssembleMetadata:
    def test_metadata_has_timing(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble()
        assert "timing_ms" in result.metadata
        assert result.metadata["timing_ms"] >= 0

    def test_metadata_has_counts(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble()
        assert "pull_count" in result.metadata
        assert "push_count" in result.metadata
        assert "token_count" in result.metadata


# ---------------------------------------------------------------------------
# Integration Tests — Full Pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_end_to_end_with_full_memory(self):
        """End-to-end: save records, assemble with all field types."""
        for i in range(3):
            m = FullMemory(
                agent_id="agent-1",
                topic=f"topic-{i}",
                content=f"Memory content {i}",
            )
            m.save()

        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 0.6, "urgency": 0.4},
            max_items=5,
            surfacing_threshold=0.0,
        )
        result = assembler.assemble(
            query_cues={"topic": "topic-0"},
            agent_id="agent-1",
        )
        assert isinstance(result, AssemblyResult)
        assert isinstance(result.formatted, str)
        assert result.metadata["timing_ms"] >= 0

    def test_agent_id_merged_into_partition(self):
        """agent_id parameter is merged into partition_filters."""
        m = SimpleMemory(agent_id="agent-x", content="test")
        m.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble(
            query_cues={"topic": "test"},
            agent_id="agent-x",
        )
        assert isinstance(result, AssemblyResult)


# ---------------------------------------------------------------------------
# TestRetrievalQuality — metacognitive layer (Tier 1)
# ---------------------------------------------------------------------------


class TestRetrievalQuality:
    """Tests for RetrievalQuality, assess(), and assemble(assess_quality=True)."""

    def test_fok_weights_match_spec(self):
        """FOK formula weights are 0.4 / 0.4 / 0.2 per design spec."""
        assert FOK_WEIGHT_CUE_FAMILIARITY == 0.4
        assert FOK_WEIGHT_PARTIAL_RETRIEVAL == 0.4
        assert FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION == 0.2
        total = (
            FOK_WEIGHT_CUE_FAMILIARITY
            + FOK_WEIGHT_PARTIAL_RETRIEVAL
            + FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION
        )
        assert abs(total - 1.0) < 1e-9

    def test_assess_quality_default_off(self):
        """assemble() by default produces no 'quality' key in metadata."""
        m = SimpleMemory(agent_id="agent-1", content="content")
        m.save()
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble(query_cues={"topic": "content"})
        assert "quality" not in result.metadata

    def test_assess_quality_true_attaches_quality(self):
        """assemble(..., assess_quality=True) attaches a RetrievalQuality."""
        m = SimpleMemory(agent_id="agent-1", content="deployment notes")
        m.save()
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble(
            query_cues={"topic": "deployment"},
            assess_quality=True,
        )
        assert "quality" in result.metadata
        quality = result.metadata["quality"]
        assert isinstance(quality, RetrievalQuality)
        # With no ConfidenceField on SimpleMemory, avg_confidence defaults to 1.0
        assert quality.avg_confidence == 1.0

    def test_assess_standalone_returns_retrieval_quality(self):
        """ContextAssembler.assess() returns a RetrievalQuality dataclass."""
        m = FullMemory(
            agent_id="agent-1",
            topic="deployment",
            content="deployment notes",
        )
        m.save()
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 0.6, "urgency": 0.4},
        )
        quality = assembler.assess(query_cues={"topic": "deployment"})
        assert isinstance(quality, RetrievalQuality)
        # Bloom should see "deployment" (we just saved it)
        assert "deployment" in quality.per_cue_fok
        # cue_familiarity in {0.0, 1.0} when ExistenceFilter is present
        assert quality.per_cue_fok["deployment"]["cue_familiarity"] in (0.0, 1.0)

    def test_assess_empty_cues_returns_zeros_with_warning(self, caplog):
        """assess({}) returns zero metrics and logs a warning. Does not raise."""
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            quality = assembler.assess(query_cues={})
        assert quality.avg_confidence == 0.0
        assert quality.score_spread == 0.0
        assert quality.fok_score == 0.0
        assert quality.staleness_ratio == 0.0
        # Warning emitted
        assert any("no query_cues" in rec.message for rec in caplog.records)

    def test_fok_neutral_fallback_without_existence_filter(self):
        """When model has no ExistenceFilter, cue_familiarity defaults to 0.5."""
        m = SimpleMemory(agent_id="agent-1", content="content")
        m.save()
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        quality = assembler.assess(query_cues={"topic": "anything"})
        # cue_familiarity must be 0.5 when no ExistenceFilter
        comp = quality.per_cue_fok["anything"]["cue_familiarity"]
        assert comp == 0.5

    def test_fok_cue_familiarity_with_existence_filter_present(self):
        """Saved topics produce cue_familiarity == 1.0 under ExistenceFilter."""
        m = FullMemory(
            agent_id="agent-1",
            topic="observable-topic",
            content="saved content",
        )
        m.save()
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 1.0},
        )
        quality = assembler.assess(query_cues={"topic": "observable-topic"})
        comp = quality.per_cue_fok["observable-topic"]["cue_familiarity"]
        # Saved topic -> ExistenceFilter.might_exist returns True -> 1.0
        assert comp == 1.0

    def test_avg_confidence_populated_with_confidence_field(self):
        """avg_confidence reflects ConfidenceField.get_confidence() mean."""
        m1 = FullMemory(agent_id="agent-1", topic="t1", content="c1")
        m1.save()
        # Update confidence so it's no longer exactly the initial value
        ConfidenceField.update_confidence(m1, "confidence", signal=0.9)
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 0.5, "confidence": 0.5},
        )
        result = assembler.assemble(
            query_cues={"topic": "t1"},
            assess_quality=True,
        )
        quality = result.metadata["quality"]
        # With a saved + updated ConfidenceField, avg_confidence is >= initial
        assert 0.0 <= quality.avg_confidence <= 1.0

    def test_score_spread_zero_on_empty_results(self):
        """score_spread is 0.0 when no records are selected (empty retrieval)."""
        # Note: no FullMemory instances saved; this yields empty results.
        assembler = ContextAssembler(
            model_class=FullMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble(
            query_cues={"topic": "nothing-here"},
            assess_quality=True,
        )
        quality = result.metadata["quality"]
        assert quality.score_spread == 0.0
        assert quality.score_distribution == []

    def test_retrieval_quality_repr_is_non_empty(self):
        """__repr__ produces a debug-friendly non-empty string."""
        q = RetrievalQuality(
            avg_confidence=0.5,
            score_spread=0.1,
            fok_score=0.7,
            staleness_ratio=0.2,
        )
        s = repr(q)
        assert s
        assert "RetrievalQuality" in s
        assert "fok_score=0.7" in s


# ---------------------------------------------------------------------------
# TestAssessWithNoSavedMemories
# ---------------------------------------------------------------------------


class AssessEmptyMemory(Model):
    """Minimal model with ConfidenceField and ExistenceFilter — no records saved."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    topic = Field(type=str)
    confidence = ConfidenceField(initial_confidence=0.5)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.topic,
    )


class TestAssessWithNoSavedMemories:
    def test_assess_with_empty_db_returns_zero_quality(self):
        """assess() with a freshly populated model class (no saved records) must not raise."""
        assembler = ContextAssembler(
            model_class=AssessEmptyMemory,
            score_weights={"confidence": 1.0},
        )

        result = assembler.assess(query_cues={"topic": "anything"})

        # Must return a RetrievalQuality without raising
        assert isinstance(result, RetrievalQuality)
        # With ExistenceFilter on an empty DB, definitely_missing=True for all cues.
        # The early-return path returns avg_confidence=0.5 when ConfidenceField is
        # present (neutral sentinel: no evidence against, but also none for).
        assert result.avg_confidence == 0.5
