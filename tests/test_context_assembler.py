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
import math
import os
import sys
import time
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src.popoto.exceptions import QueryException  # noqa: E402
from src.popoto.fields.co_occurrence_field import CoOccurrenceField  # noqa: E402
from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.existence_filter import ExistenceFilter  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.fields.shortcuts import (  # noqa: E402
    AutoKeyField,
    FloatField,
    KeyField,
    SortedField,
)
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
    _compute_score_spread,
    _score_proxy_for_records,
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


class FixtureMemory(Model):
    """Deterministic fixture for numeric-regression tests (item 2a-pre, C3).

    No CyclicDecayField (keeps scoring strictly on the DecayingSortedField).
    Topics fingerprinted into ExistenceFilter for cue_familiarity.
    """

    memory_id = AutoKeyField()
    agent_id = KeyField()
    topic = Field(type=str)
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.topic,
    )


class GateMemory(Model):
    """Model with ConfidenceField + CyclicDecayField for confidence-gate
    tests (issue #463) — lets a single test exercise both the pull path
    (gated) and the push path (never gated, must stay intact)."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    topic = Field(type=str)
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    urgency = CyclicDecayField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)
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
        "*FixtureMemory*",
        "*AltFixtureMemory*",
        "*GateMemory*",
        "$EF:*Memory*",
        "$CoOcF:*Memory*",
        "$ConfidencF:*Memory*",
        "$SortedF:*Memory*",
        "$DecayingSortF:*Memory*",
        "$CyclicDF:*Memory*",
        "$KeyF:*Memory*",
        "$Class:*Memory*",
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

    def test_token_counter_receives_serialized_string(self):
        """New contract: the token_counter is handed the serialized record
        STRING (the exact per-record slice the formatter emits), never the
        record object."""
        m = SimpleMemory(agent_id="a1", content="counter contract content")
        m.save()

        seen = []

        def spy_counter(arg):
            seen.append(arg)
            return 10

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_tokens=1000,
            token_counter=spy_counter,
        )
        seen.clear()  # drop the construction-time contract probe call
        result = assembler.assemble(
            query_cues={"topic": "test"},
            partition_filters={"agent_id": "a1"},
        )
        assert len(result.records) == 1
        assert len(seen) == 1
        assert isinstance(seen[0], str)  # serialized text, not the record
        assert "counter contract content" in seen[0]

    def test_default_token_counter(self):
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        # Default is the module-private stdlib estimator over serialized text
        from src.popoto.recipes.context_assembler import _estimate_tokens

        assert assembler._token_counter is _estimate_tokens
        # The estimator measures content scale: 2,000 chars of prose-like
        # content must count hundreds of tokens, not the ~12 the old
        # key-length heuristic produced.
        text = "the quick brown fox jumps over the lazy dog " * 45  # ~2,000 chars
        assert assembler._token_counter(text) > 100
        # Empty input counts zero
        assert _estimate_tokens("") == 0


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
        """Records beyond max_tokens budget are dropped — token_count stays
        within budget AND at least one record is admitted (conjunction, not
        the old weak disjunction)."""
        for i in range(5):
            m = SimpleMemory(agent_id="a1", content=f"long content item {i}" * 10)
            m.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            max_tokens=35,
            # Deterministic counter: every record costs 10 tokens, so the
            # budget admits exactly 3 of the 5 and never overshoots.
            token_counter=lambda text: 10,
        )
        result = assembler.assemble(
            query_cues={"topic": "test"},
            partition_filters={"agent_id": "a1"},
        )
        assert len(result.records) >= 1
        assert len(result.records) < 5  # the budget actually dropped records
        assert result.metadata["token_count"] == 10 * len(result.records)
        assert result.metadata["token_count"] <= 35

    def test_max_tokens_skip_not_break_mixed_sizes(self):
        """An oversized record ranked between small ones is skipped — not a
        packing terminator: the small record after it is still admitted, and
        token_count reflects only admitted records."""
        small1 = SimpleMemory(agent_id="a1", content="s" * 100)
        small1.save()
        big = SimpleMemory(agent_id="a1", content="B" * 8000)
        big.save()
        small2 = SimpleMemory(agent_id="a1", content="t" * 100)
        small2.save()

        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            max_tokens=50,
            # Size-keyed counter: small records cost 10, the oversized
            # record costs 10,000 (never fits in the budget).
            token_counter=lambda text: 10 if len(text) < 1000 else 10_000,
        )
        # Pin candidate rank order deterministically: [small1, big, small2].
        order = [small1, big, small2]
        assembler._pull_path = lambda cues, filters: (order, order)

        result = assembler.assemble(query_cues={"topic": "test"})

        admitted = [str(r) for r in result.records]
        assert admitted == [str(small1), str(small2)]
        assert str(big) not in admitted
        assert result.metadata["token_count"] == 20  # admitted records only


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
# TestRetrievalQualityAssembler (numeric regression guard for item 2a refactor)
# ---------------------------------------------------------------------------


class TestRetrievalQualityAssembler:
    """Numeric-fixture regression guard protecting the Item 2a helper
    extraction (C2/C3). Hardcoded expected values were captured against the
    pre-refactor code — any drift indicates the wrapper methods stopped
    routing state through to the module-level helpers correctly.
    """

    @staticmethod
    def _isclose(a, b):
        import math

        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)

    def test_assess_numeric_fixture(self):
        """Three records with known confidences + two query cues produce
        hardcoded scalar values for the RetrievalQuality metrics. Any
        numeric drift after the helper refactor fails this test.

        Fixture:
            * 3 FixtureMemory instances (topics: alpha, beta, gamma),
              one update each from initial 0.5 at signals 0.9, 0.5, 0.3;
              capped Bayesian oracle (c0 + s) / 2 gives post-update
              confidences 0.7, 0.5, 0.4
            * score_weights={"relevance": 1.0}, max_items=5,
              surfacing_threshold=0.5
            * query_cues={"topic": "alpha", "other": "beta"}
              (both topics are in the ExistenceFilter fingerprint)
            * partition_filters={"agent_id": "a"}
        """
        m1 = FixtureMemory(agent_id="a", topic="alpha", content="c1")
        m1.save()
        m2 = FixtureMemory(agent_id="a", topic="beta", content="c2")
        m2.save()
        m3 = FixtureMemory(agent_id="a", topic="gamma", content="c3")
        m3.save()

        ConfidenceField.update_confidence(m1, "confidence", signal=0.9)
        ConfidenceField.update_confidence(m2, "confidence", signal=0.5)
        ConfidenceField.update_confidence(m3, "confidence", signal=0.3)

        assembler = ContextAssembler(
            model_class=FixtureMemory,
            score_weights={"relevance": 1.0},
            max_items=5,
            surfacing_threshold=0.5,
        )

        quality = assembler.assess(
            query_cues={"topic": "alpha", "other": "beta"},
            partition_filters={"agent_id": "a"},
        )

        # Hardcoded expected scalars captured against the pre-refactor
        # implementation. DO NOT tweak these to paper over a refactor —
        # any divergence means the refactor changed numeric behavior.
        # avg_confidence recomputed for the capped Bayesian rule (#407):
        # ((0.5+0.9)/2 + (0.5+0.5)/2 + (0.5+0.3)/2) / 3
        #   = (0.7 + 0.5 + 0.4) / 3 = 1.6 / 3 = 0.5333...
        assert self._isclose(quality.avg_confidence, 1.6 / 3)
        # score_spread stays 0.0 -- but now for the RIGHT reason (#474). The
        # three records are equally fresh, so their decayed relevance scores
        # are identical (see score_distribution below) -> zero dispersion.
        # Pre-#474 this was 0.0 because the proxy read the empty non-partitioned
        # key and every record scored 0.0.
        assert self._isclose(quality.score_spread, 0.0)
        assert self._isclose(quality.fok_score, 0.64)
        # staleness_ratio: 1.0 -> 0.0 (#474). Pre-fix, the staleness loop read
        # the non-partitioned base key, got None for every partitioned record
        # and counted them all "stale". The partition-aware helper now reads the
        # decayed relevance (~3.98, see below), which is well above the 0.5
        # surfacing_threshold, so these freshly-saved records are NOT stale.
        assert self._isclose(quality.staleness_ratio, 0.0)

        # Per-cue components also hardcoded.
        assert set(quality.per_cue_fok.keys()) == {"alpha", "beta"}
        for cue in ("alpha", "beta"):
            comp = quality.per_cue_fok[cue]
            assert self._isclose(comp["cue_familiarity"], 1.0)
            assert self._isclose(comp["partial_retrieval_count"], 0.6)
            assert self._isclose(comp["subthreshold_activation"], 0.0)
            assert self._isclose(comp["component_score"], 0.64)

        # score_distribution contents (#474): three equal, non-zero decayed
        # relevance scores. DecayingSortedField stores the last_updated
        # timestamp as the ZSET score; the partition-aware proxy decays it via
        # DECAY_SCORE_LUA (base_score 1.0, default decay_rate 0.3). All three
        # records are freshly saved, so elapsed time clamps to the script's
        # 0.01-day floor and each decays to 0.01**(-0.3) ≈ 3.981. Pre-#474 the
        # proxy read the empty non-partitioned key and reported 0.0 for all.
        expected_decayed = 0.01 ** (-Defaults.DECAY_RATE)
        assert len(quality.score_distribution) == 3
        for s in quality.score_distribution:
            assert self._isclose(s, expected_decayed)


# ---------------------------------------------------------------------------
# TestRetrievalQualityFromRecords (issue #370 item 2b: C1 parity + C4 guard)
# ---------------------------------------------------------------------------


class AltFixtureMemory(Model):
    """A second model class for the mixed-record TypeError test (C4)."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    topic = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")


class TestRetrievalQualityFromRecords:
    """Public classmethod: RetrievalQuality.from_records(records, ...).

    Parity contract (C1): same records, same assembler config, the
    classmethod and ``ContextAssembler.assemble(..., assess_quality=True)``
    produce numerically equal scalar fields within 1e-9.
    """

    @staticmethod
    def _isclose(a, b):
        import math

        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)

    def test_from_records_matches_assemble_quality(self):
        """C1: ``from_records`` numerically matches ``assemble(..., assess_quality=True)``
        when given identical records and identical assembler config
        (score_weights, max_items, surfacing_threshold, query_cues).
        """
        import math

        # Save records, then compute quality two ways and compare.
        saved = []
        for i, topic in enumerate(("one", "two", "three")):
            m = FixtureMemory(agent_id="a", topic=topic, content=f"c{i}")
            m.save()
            ConfidenceField.update_confidence(m, "confidence", signal=0.5 + 0.1 * i)
            saved.append(m)

        score_weights = {"relevance": 1.0}
        max_items = 5
        surfacing_threshold = 0.5
        query_cues = {"topic": "one"}

        # Assembler path — use assemble(..., assess_quality=True) so that
        # its metadata["quality"] reflects the SELECTED records.
        assembler = ContextAssembler(
            model_class=FixtureMemory,
            score_weights=score_weights,
            max_items=max_items,
            surfacing_threshold=surfacing_threshold,
        )
        result = assembler.assemble(
            query_cues=query_cues,
            partition_filters={"agent_id": "a"},
            assess_quality=True,
        )
        assembler_quality = result.metadata["quality"]

        # Custom-pipeline path — hand the exact selected records to
        # from_records. `result.records` is what assemble() produced
        # and what its quality was computed against.
        custom_quality = RetrievalQuality.from_records(
            result.records,
            query_cues=query_cues,
            score_weights=score_weights,
            max_items=max_items,
            surfacing_threshold=surfacing_threshold,
        )

        assert self._isclose(
            custom_quality.avg_confidence, assembler_quality.avg_confidence
        )
        assert self._isclose(
            custom_quality.score_spread, assembler_quality.score_spread
        )
        assert self._isclose(custom_quality.fok_score, assembler_quality.fok_score)
        assert self._isclose(
            custom_quality.staleness_ratio, assembler_quality.staleness_ratio
        )

        # score_distribution element-wise tolerant comparison
        assert len(custom_quality.score_distribution) == len(
            assembler_quality.score_distribution
        )
        for a, b in zip(
            custom_quality.score_distribution, assembler_quality.score_distribution
        ):
            assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)

        # per_cue_fok keys match; each component within tolerance.
        assert set(custom_quality.per_cue_fok.keys()) == set(
            assembler_quality.per_cue_fok.keys()
        )
        for cue, comp in custom_quality.per_cue_fok.items():
            expected = assembler_quality.per_cue_fok[cue]
            for k in (
                "cue_familiarity",
                "partial_retrieval_count",
                "subthreshold_activation",
                "component_score",
            ):
                assert math.isclose(comp[k], expected[k], rel_tol=1e-9, abs_tol=1e-9)

    def test_from_records_empty_returns_zero(self):
        """Empty records list -> zero-valued RetrievalQuality; does NOT raise."""
        q = RetrievalQuality.from_records([])
        assert isinstance(q, RetrievalQuality)
        assert q.avg_confidence == 0.0
        assert q.score_spread == 0.0
        assert q.fok_score == 0.0
        assert q.staleness_ratio == 0.0
        assert q.score_distribution == []
        assert q.per_cue_fok == {}

    def test_from_records_no_cues_no_weights(self):
        """query_cues=None + score_weights=None -> zero FOK / spread /
        staleness, avg_confidence still computed from ConfidenceField.
        """
        m = FixtureMemory(agent_id="a", topic="solo", content="body")
        m.save()
        ConfidenceField.update_confidence(m, "confidence", signal=0.7)

        q = RetrievalQuality.from_records([m])
        assert isinstance(q, RetrievalQuality)
        assert q.fok_score == 0.0
        assert q.per_cue_fok == {}
        assert q.score_spread == 0.0
        assert q.score_distribution == []
        assert q.staleness_ratio == 0.0
        # avg_confidence is still computed (ConfidenceField is present).
        assert 0.0 <= q.avg_confidence <= 1.0

    def test_from_records_mixed_models_raises(self):
        """C4: heterogeneous record lists raise TypeError whose message
        names BOTH classes.
        """
        m1 = FixtureMemory(agent_id="a", topic="x", content="c")
        m1.save()
        m2 = AltFixtureMemory(agent_id="a", topic="y")
        m2.save()

        with pytest.raises(TypeError) as excinfo:
            RetrievalQuality.from_records([m1, m2])

        msg = str(excinfo.value)
        assert "FixtureMemory" in msg
        assert "AltFixtureMemory" in msg


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


# ---------------------------------------------------------------------------
# TestPartitionAwareScoreProxy (issue #474)
#
# The shared `_score_proxy_for_records` / `RetrievalQuality` helper historically
# read each record's score from the NON-partitioned sorted-set index key. Agent
# memory models declare `partition_by` on their sorted fields, so the real
# scores live in a partition-specific ZSET; the base key has zero members and
# every ZSCORE returned None -> 0.0 for every record -> score_spread == 0.0.
# ---------------------------------------------------------------------------


class PlainSortedPartitionedMemory(Model):
    """Plain (non-decaying) SortedField partitioned by agent_id.

    A plain SortedField stores the field *value itself* as the ZSET score, so
    distinct saved values must surface as distinct proxy scores once the helper
    reads the partition-specific ZSET.
    """

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = SortedField(type=float, partition_by="agent_id")


class TestPartitionAwareScoreProxy:
    def _mk(self, agent_id="a", scores=(0.2, 0.6, 0.9)):
        records = []
        for i, s in enumerate(scores):
            r = PlainSortedPartitionedMemory(
                agent_id=agent_id, content=f"c{i}", relevance=s
            )
            r.save()
            records.append(r)
        return records

    def test_proxy_reads_partition_specific_scores(self):
        """`_score_proxy_for_records` returns the true per-record partition
        scores for a partitioned SortedField (was all 0.0 pre-#474)."""
        records = self._mk(scores=(0.2, 0.6, 0.9))
        proxy = _score_proxy_for_records(
            records,
            model_class=PlainSortedPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        vals = sorted(proxy.values())
        assert len(vals) == 3
        assert math.isclose(vals[0], 0.2, abs_tol=1e-9)
        assert math.isclose(vals[1], 0.6, abs_tol=1e-9)
        assert math.isclose(vals[2], 0.9, abs_tol=1e-9)

    def test_score_spread_nonzero_for_partitioned_model(self):
        """score_spread reflects real dispersion for a partitioned model."""
        records = self._mk(scores=(0.2, 0.6, 0.9))
        spread, dist = _compute_score_spread(
            records,
            model_class=PlainSortedPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        assert spread > 0.0
        assert len(dist) == 3

    def test_single_partition_record_value_is_the_score(self):
        """Single record -> proxy equals its stored value (sanity)."""
        records = self._mk(scores=(0.42,))
        proxy = _score_proxy_for_records(
            records,
            model_class=PlainSortedPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        assert math.isclose(list(proxy.values())[0], 0.42, abs_tol=1e-9)

    def test_missing_partition_value_does_not_corrupt_others(self):
        """A record missing its partition field scores 0.0 without crashing or
        corrupting the other records' scores."""
        records = self._mk(scores=(0.6, 0.9))
        orphan = PlainSortedPartitionedMemory(content="orphan", relevance=0.5)
        # deliberately do NOT set agent_id / do not save -> partition missing
        proxy = _score_proxy_for_records(
            records + [orphan],
            model_class=PlainSortedPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        good = sorted(v for v in proxy.values() if v > 0)
        assert len(good) == 2
        assert math.isclose(good[0], 0.6, abs_tol=1e-9)
        assert math.isclose(good[1], 0.9, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# TestPartitionAwareDecayingScoreProxy (issue #474 — the decay branch)
#
# The subtle half of #474: a DecayingSortedField stores each member's
# last_updated TIMESTAMP as the ZSET score, not its relevance. A naive
# partition key-swap would surface ~1.7e9 timestamps as "relevance" (inverting
# staleness and flattening spread). The fix decays the timestamp into relevance
# via DECAY_SCORE_LUA on the partition ZSET. These tests exercise that branch
# directly (the plain-SortedField tests above would mask the decay defect).
# ---------------------------------------------------------------------------


class DecayPartitionedMemory(Model):
    """Partitioned DecayingSortedField with a base_score companion.

    Distinct ``strength`` values give freshly-saved records distinct decayed
    relevance scores (base * elapsed^-decay_rate, elapsed clamped to the
    script's 0.01-day floor), so score_spread is non-zero without any aging.
    """

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    strength = FloatField(default=1.0)
    # Explicit decay_rate=0.5 (not the 0.1 default) so aged-record tests reach
    # sub-threshold decayed scores within a reasonable number of days:
    # 40 days -> 40**-0.5 ≈ 0.158 < the 0.5 surfacing threshold.
    relevance = DecayingSortedField(
        partition_by="agent_id", base_score_field="strength", decay_rate=0.5
    )
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=10_000,
        fingerprint_fn=lambda inst: inst.content,
    )


class TestPartitionAwareDecayingScoreProxy:
    @staticmethod
    def _partition_key(agent_id):
        return DecayingSortedField.get_sortedset_db_key(
            DecayPartitionedMemory, "relevance", agent_id
        ).redis_key

    def _backdate(self, agent_id, members_days):
        """Set each member's partition-ZSET score to now - days*86400."""
        now = time.time()
        POPOTO_REDIS_DB.zadd(
            self._partition_key(agent_id),
            {m.db_key.redis_key: now - 86400 * d for m, d in members_days},
        )

    def test_distinct_base_scores_give_nonzero_spread(self):
        """Fresh partitioned decaying records with distinct base scores decay to
        distinct relevances -> real per-record proxy scores and score_spread>0.
        This is the primary #474 decay-branch repro (all 0.0 pre-fix)."""
        recs = []
        for i, w in enumerate((1.0, 3.0, 9.0)):
            r = DecayPartitionedMemory(agent_id="a", content=f"c{i}", strength=w)
            r.save()
            recs.append(r)

        proxy = _score_proxy_for_records(
            recs,
            model_class=DecayPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        vals = sorted(proxy.values())
        # Three distinct, strictly increasing, non-zero decayed scores.
        assert len(vals) == 3
        assert vals[0] > 0.0
        assert vals[0] < vals[1] < vals[2]
        # Decayed relevance scales linearly in base_score at equal freshness.
        decay_rate = DecayPartitionedMemory._meta.fields["relevance"].decay_rate
        factor = 0.01 ** (-decay_rate)
        for got, w in zip(vals, (1.0, 3.0, 9.0)):
            assert math.isclose(got, w * factor, rel_tol=1e-6)

        spread, dist = _compute_score_spread(
            recs,
            model_class=DecayPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        assert spread > 0.0
        assert len(dist) == 3

    def test_proxy_matches_top_by_decay(self):
        """The proxy's decayed score agrees with Query.top_by_decay (both reuse
        DECAY_SCORE_LUA on the same partition ZSET) — no drift."""
        recs = []
        for i, w in enumerate((1.0, 4.0)):
            r = DecayPartitionedMemory(agent_id="a", content=f"c{i}", strength=w)
            r.save()
            recs.append(r)
        # Age them so ordering is unambiguous.
        self._backdate("a", [(recs[0], 1), (recs[1], 4)])

        ranked = DecayPartitionedMemory.query.filter(agent_id="a").top_by_decay(
            "relevance", n=10
        )
        proxy = _score_proxy_for_records(
            recs,
            model_class=DecayPartitionedMemory,
            score_weights={"relevance": 1.0},
        )
        # top_by_decay returns instances in decayed-score order; the proxy's
        # highest score must correspond to top_by_decay's first result.
        best_key = max(proxy, key=proxy.get)
        assert ranked[0].db_key.redis_key == best_key

    def test_aged_records_are_stale_fresh_are_not(self):
        """staleness_ratio regression (#474): records aged past the surfacing
        threshold count as stale; freshly-saved records do not."""
        aged = DecayPartitionedMemory(agent_id="a", content="old", strength=1.0)
        aged.save()
        fresh = DecayPartitionedMemory(agent_id="a", content="new", strength=1.0)
        fresh.save()
        # Age one record ~40 days: decayed = 1.0 * 40**-0.5 ≈ 0.158 < 0.5.
        self._backdate("a", [(aged, 40)])

        assembler = ContextAssembler(
            model_class=DecayPartitionedMemory,
            score_weights={"relevance": 1.0},
            surfacing_threshold=0.5,
        )
        # All fresh -> staleness 0.0.
        fresh_only = _staleness_ratio_wrapper(assembler, [fresh])
        assert fresh_only == 0.0
        # One of two aged past threshold -> staleness 0.5.
        mixed = _staleness_ratio_wrapper(assembler, [aged, fresh])
        assert mixed == 0.5

    def test_fok_subthreshold_activation_for_aged_records(self):
        """FOK subthreshold regression (#474): records whose decayed relevance
        falls in (0, surfacing_threshold) drive subthreshold_activation > 0."""
        recs = []
        for i in range(3):
            r = DecayPartitionedMemory(
                agent_id="a", content=f"deployment{i}", strength=1.0
            )
            r.save()
            recs.append(r)
        # Age all three ~40 days -> decayed = 40**-0.5 ≈ 0.158, inside (0, 0.5).
        self._backdate("a", [(r, 40) for r in recs])

        assembler = ContextAssembler(
            model_class=DecayPartitionedMemory,
            score_weights={"relevance": 1.0},
            surfacing_threshold=0.5,
        )
        quality = assembler.assess(
            query_cues={"content": "deployment0"},
            partition_filters={"agent_id": "a"},
        )
        subthreshold = [
            c["subthreshold_activation"] for c in quality.per_cue_fok.values()
        ]
        assert subthreshold
        assert all(s > 0.0 for s in subthreshold)

    def test_cyclic_field_uses_cyclic_decay_not_raw_timestamp(self):
        """A CyclicDecayField is scored via CYCLIC_DECAY_LUA on the partition
        ZSET, so the proxy returns a bounded decayed relevance -- NOT the raw
        ~1.7e9 last_updated timestamp a naive ZSCORE would surface (#474)."""
        recs = []
        for i in range(2):
            r = CyclicPartitionedMemory(agent_id="a", content=f"c{i}")
            r.save()
            recs.append(r)
        proxy = _score_proxy_for_records(
            recs,
            model_class=CyclicPartitionedMemory,
            score_weights={"urgency": 1.0},
        )
        assert len(proxy) == 2
        # Decayed relevance is O(1), nowhere near an epoch timestamp.
        for v in proxy.values():
            assert 0.0 <= v < 1e6


class CyclicPartitionedMemory(Model):
    """Partitioned CyclicDecayField — exercises the CYCLIC_DECAY_LUA branch."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    urgency = CyclicDecayField(partition_by="agent_id")


def _staleness_ratio_wrapper(assembler, records):
    """Call the module-level _staleness_ratio the way ContextAssembler does."""
    from src.popoto.recipes.context_assembler import _staleness_ratio

    return _staleness_ratio(
        records,
        model_class=assembler.model_class,
        score_weights=assembler.score_weights,
        surfacing_threshold=assembler.surfacing_threshold,
        decaying_sorted_field_name="relevance",
    )


# ---------------------------------------------------------------------------
# TestConfidenceGate — confidence-gated retrieval (issue #463)
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    """Unit tests for the opt-in confidence gate on ContextAssembler.

    The gate reads the rank-0 pull-path candidate's ConfidenceField value
    and, when below ``confidence_gate_threshold``, either drops all
    pull-path records ("refuse") or keeps them and only flags the decision
    in ``metadata["gate"]`` ("flag"). Disabled by default (threshold=None).
    """

    # -- Construction-time validation -------------------------------------

    def test_default_threshold_none_no_gate_key(self):
        """confidence_gate_threshold=None (default) -> metadata has no
        "gate" key at all, bit-for-bit identical to pre-change behavior."""
        assembler = ContextAssembler(
            model_class=SimpleMemory,
            score_weights={"relevance": 1.0},
        )
        assert assembler.confidence_gate_threshold is None
        assert assembler.confidence_gate_mode == "refuse"
        result = assembler.assemble()
        assert "gate" not in result.metadata

    def test_threshold_without_confidence_field_raises(self):
        """A model with no ConfidenceField cannot use the gate."""
        with pytest.raises(QueryException, match="ConfidenceField"):
            ContextAssembler(
                model_class=SimpleMemory,
                score_weights={"relevance": 1.0},
                confidence_gate_threshold=0.5,
            )

    def test_invalid_gate_mode_raises(self):
        with pytest.raises(QueryException, match="not a recognised mode"):
            ContextAssembler(
                model_class=GateMemory,
                score_weights={"relevance": 1.0},
                confidence_gate_threshold=0.5,
                confidence_gate_mode="bogus",
            )

    # -- Empty / skipped pull-path -----------------------------------------

    def test_empty_pull_records_with_threshold(self):
        """No candidates matched: applied=False, gate_score=None,
        gated=False, but the dict is still attached."""
        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.5,
        )
        result = assembler.assemble(
            query_cues={"topic": "nonexistent_xyz"},
            partition_filters={"agent_id": "a1"},
        )
        assert result.metadata["pull_count"] == 0
        assert result.metadata["gate"] == {
            "applied": False,
            "gate_score": None,
            "threshold": 0.5,
            "mode": "refuse",
            "gated": False,
        }

    def test_query_cues_none_skips_pull_path_gate_still_empty(self):
        """query_cues=None skips the pull path entirely: same empty-path
        gate behavior as an unmatched query."""
        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.5,
        )
        result = assembler.assemble()
        assert result.metadata["gate"] == {
            "applied": False,
            "gate_score": None,
            "threshold": 0.5,
            "mode": "refuse",
            "gated": False,
        }

    # -- "refuse" mode -------------------------------------------------------

    def test_refuse_mode_gated_drops_pull_records_keeps_push(self):
        """refuse + gate_score < threshold: pull records dropped, push
        path intact, and no competitive suppression runs (suppression
        candidates were zeroed, not punished, for a refusal already made)."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()  # confidence starts at initial_confidence == 0.5

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 0.5, "urgency": 0.5},
            surfacing_threshold=0.0,  # accept all push candidates
            confidence_gate_threshold=0.9,  # 0.5 < 0.9 -> gated
            confidence_gate_mode="refuse",
        )
        # Force the pull path to return our low-confidence record as rank 0,
        # deterministically, regardless of real scoring.
        assembler._pull_path = lambda cues, filters: ([record], [record])

        with patch.object(ConfidenceField, "update_confidence") as mock_update:
            result = assembler.assemble(
                query_cues={"topic": "pull-topic"},
                partition_filters={"agent_id": "a1"},
            )
            mock_update.assert_not_called()

        assert result.metadata["pull_count"] == 0
        assert result.metadata["gate"] == {
            "applied": True,
            "gate_score": 0.5,
            "threshold": 0.9,
            "mode": "refuse",
            "gated": True,
        }
        # The push path is never gated: the same underlying record is still
        # discoverable via the real (unmocked) push scan and gets injected.
        assert result.metadata["push_count"] >= 1

    def test_refuse_not_gated_retains_records(self):
        """refuse mode but gate_score >= threshold: nothing is dropped."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.1,  # 0.5 >= 0.1 -> not gated
            confidence_gate_mode="refuse",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "pull-topic"},
            partition_filters={"agent_id": "a1"},
        )
        assert result.metadata["pull_count"] == 1
        assert result.metadata["gate"]["gated"] is False
        assert result.metadata["gate"]["applied"] is True

    def test_refuse_assess_quality_fok_not_corrupted(self):
        """BLOCKER fix: a refusal must not corrupt _compute_quality's FoK
        score. all_pull_candidates is left untouched by the refuse clear
        (only the suppression copy fed to _post_effects is zeroed), so
        assess_quality=True still reports a non-degenerate FoK reflecting
        that candidates WERE found, even though they were refused."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.9,
            confidence_gate_mode="refuse",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "pull-topic"},
            partition_filters={"agent_id": "a1"},
            assess_quality=True,
        )
        assert result.metadata["gate"]["gated"] is True
        assert result.metadata["pull_count"] == 0
        assert result.metadata["quality"].fok_score > 0.0

    # -- "flag" mode -----------------------------------------------------

    def test_flag_mode_gated_retains_records(self):
        """flag + gate_score < threshold: nothing is dropped, only the
        metadata annotation reports the decision."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.9,
            confidence_gate_mode="flag",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "pull-topic"},
            partition_filters={"agent_id": "a1"},
        )
        assert result.metadata["pull_count"] == 1
        assert result.metadata["gate"] == {
            "applied": True,
            "gate_score": 0.5,
            "threshold": 0.9,
            "mode": "flag",
            "gated": True,
        }

    def test_flag_not_gated_retains_records(self):
        """flag mode, gate_score >= threshold: not gated, retained."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.1,
            confidence_gate_mode="flag",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        result = assembler.assemble(
            query_cues={"topic": "pull-topic"},
            partition_filters={"agent_id": "a1"},
        )
        assert result.metadata["pull_count"] == 1
        assert result.metadata["gate"]["gated"] is False

    # -- Fault tolerance ----------------------------------------------------

    def test_get_confidence_raises_gate_not_applied(self, caplog):
        """A get_confidence() failure degrades gracefully: a warning is
        logged, the gate is treated as not-applied, and records are
        returned unchanged (no crash)."""
        record = GateMemory(agent_id="a1", topic="pull-topic", content="c")
        record.save()

        assembler = ContextAssembler(
            model_class=GateMemory,
            score_weights={"relevance": 1.0},
            confidence_gate_threshold=0.9,
            confidence_gate_mode="refuse",
        )
        assembler._pull_path = lambda cues, filters: ([record], [record])

        with patch.object(
            ConfidenceField, "get_confidence", side_effect=RuntimeError("boom")
        ):
            with caplog.at_level("WARNING", logger="POPOTO.ContextAssembler"):
                result = assembler.assemble(
                    query_cues={"topic": "pull-topic"},
                    partition_filters={"agent_id": "a1"},
                )

        assert "confidence gate get_confidence failed" in caplog.text
        assert result.metadata["gate"] == {
            "applied": False,
            "gate_score": None,
            "threshold": 0.9,
            "mode": "refuse",
            "gated": False,
        }
        # Not applied -> records untouched, even in refuse mode.
        assert result.metadata["pull_count"] == 1
