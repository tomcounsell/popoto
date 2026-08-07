"""Adoption-hardening tests for SubconsciousMemory (issue #513).

Four behaviors are pinned here, each traceable to an empirical gap found
when a new user followed the quickstart verbatim:

1. ``DefaultMemory`` — an importable, batteries-included memory model that
   declares a ``BM25Field``, so ``retrieval_mode='auto'`` resolves to a
   query-sensitive mode without the user authoring a schema.
   ``SubconsciousMemory`` defaults ``model_class`` to it and ``score_weights``
   to the benchmarked ``{"relevance": 1.0}``.
2. Query-blind warning — ``retrieval_mode='auto'`` resolving to ``composite``
   emits a POPOTO-logger warning naming the missing ``BM25Field``. Before
   this change nothing was emitted at any log level.
3. Content-first injection — the injected payload defaults to the memory
   text, carrying no ``memory_id`` UUID, no ``agent_id``, and no raw epoch
   float. Measured baseline for the JSON default was ~2.8x character
   overhead vs the content itself.
4. No dangling doc references in ``subconscious_memory.py``.

Backward compatibility is asserted alongside each: existing callers that
pass ``model_class`` / ``score_weights`` / ``output_format="structured"``
keep the pre-#513 behavior.
"""

import logging
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto import (
    AutoKeyField,
    BM25Field,
    ConfidenceField,
    CoOccurrenceField,
    DecayingSortedField,
    FloatField,
    KeyField,
    Model,
    StringField,
)
from popoto.recipes import DefaultMemory, SubconsciousMemory
from popoto.recipes.context_assembler import ContextAssembler
from popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class QueryBlindMemory(Model):
    """No BM25Field, no EmbeddingField -> 'auto' resolves to composite."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )


class QuerySensitiveMemory(Model):
    """BM25Field present -> 'auto' resolves to lexical (no warning)."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    content_bm25 = BM25Field(source="content")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_model(name):
    for pattern in (
        f"*{name}*",
        f"$EF:*{name}*",
        f"$FS:*{name}*",
        f"$CoOcF:*{name}*",
        f"$ConfidencF:*{name}*",
        f"$SortedF:*{name}*",
        f"$AT:*{name}*",
        f"$WF:*{name}*",
        f"$BM25:*{name}*",
    ):
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


@pytest.fixture(autouse=True)
def clean_redis():
    for name in ("DefaultMemory", "QueryBlindMemory", "QuerySensitiveMemory"):
        _clean_model(name)
    yield
    for name in ("DefaultMemory", "QueryBlindMemory", "QuerySensitiveMemory"):
        _clean_model(name)


# ===========================================================================
# 1. DefaultMemory + zero-argument-schema SubconsciousMemory
# ===========================================================================


class TestDefaultMemoryModel:
    def test_importable_from_recipes(self):
        """The default model is importable without authoring a schema."""
        from popoto.recipes import DefaultMemory as Imported

        assert Imported is DefaultMemory
        assert issubclass(DefaultMemory, Model)

    def test_declares_bm25_field(self):
        """The whole point: retrieval is query-sensitive out of the box."""
        bm25 = [
            (n, f)
            for n, f in DefaultMemory._meta.fields.items()
            if isinstance(f, BM25Field)
        ]
        assert bm25, "DefaultMemory must declare a BM25Field (issue #445/#513)"
        name, field = bm25[0]
        assert field.source == "content"

    def test_declares_expected_field_set(self):
        """Field names the recipe wires by name must exist on the model."""
        fields = DefaultMemory._meta.fields
        for name in ("agent_id", "content", "importance", "relevance"):
            assert name in fields, f"DefaultMemory is missing {name!r}"
        assert isinstance(fields["relevance"], DecayingSortedField)
        assert isinstance(fields["confidence"], ConfidenceField)
        assert isinstance(fields["associations"], CoOccurrenceField)

    def test_auto_resolves_to_query_sensitive_mode(self):
        """ContextAssembler('auto') over DefaultMemory is never composite."""
        assembler = ContextAssembler(model_class=DefaultMemory, score_weights=None)
        assert assembler._effective_mode in ("lexical", "hybrid")

    def test_save_and_query_roundtrip(self):
        DefaultMemory(
            agent_id="agent-1", content="Deploy uses blue-green", importance=0.9
        ).save()
        results = DefaultMemory.query.filter(agent_id="agent-1").top_by_decay(n=5)
        assert any("blue-green" in (m.content or "") for m in results)


class TestSubconsciousMemoryDefaults:
    def test_constructs_with_agent_id_only(self):
        sm = SubconsciousMemory(agent_id="agent-1")
        assert sm.model_class is DefaultMemory
        assert sm.score_weights == {"relevance": 1.0}

    def test_benchmarked_score_weights_default(self):
        """The benchmarked configuration, not the documented 0.6/0.3 pair."""
        from popoto.recipes.subconscious_memory import DEFAULT_SCORE_WEIGHTS

        assert DEFAULT_SCORE_WEIGHTS == {"relevance": 1.0}
        assert SubconsciousMemory(agent_id="a").score_weights == {"relevance": 1.0}

    def test_score_weights_default_is_not_shared_mutable_state(self):
        a = SubconsciousMemory(agent_id="a")
        b = SubconsciousMemory(agent_id="b")
        a.score_weights["relevance"] = 99.0
        assert b.score_weights == {"relevance": 1.0}

    def test_default_model_wires_confidence_and_association_fields(self):
        """Batteries-included: the default model's optional fields are used."""
        sm = SubconsciousMemory(agent_id="agent-1")
        assert sm.confidence_field == "confidence"
        assert sm.co_occurrence_field == "associations"

    def test_explicit_model_class_does_not_get_implicit_field_wiring(self):
        """Backward compat: a caller-supplied model keeps the old None defaults."""
        sm = SubconsciousMemory(
            model_class=QuerySensitiveMemory,
            agent_id="agent-1",
            score_weights={"relevance": 0.6},
        )
        assert sm.confidence_field is None
        assert sm.co_occurrence_field is None

    def test_explicit_score_weights_still_honored(self):
        sm = SubconsciousMemory(
            model_class=QueryBlindMemory,
            agent_id="agent-1",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )
        assert sm.score_weights == {"relevance": 0.6, "confidence": 0.3}

    def test_positional_signature_preserved(self):
        """Pre-#513 positional call ``(model_class, agent_id, score_weights)``."""
        sm = SubconsciousMemory(QueryBlindMemory, "agent-1", {"relevance": 1.0})
        assert sm.model_class is QueryBlindMemory
        assert sm.agent_id == "agent-1"

    def test_agent_id_is_required(self):
        with pytest.raises(ValueError, match="agent_id"):
            SubconsciousMemory()

    def test_end_to_end_with_zero_schema(self):
        sm = SubconsciousMemory(agent_id="agent-1")
        sm.extract_memories(
            "The deploy pipeline uses a blue-green strategy with rollback.",
            importance=0.8,
        )
        messages = [{"role": "user", "content": "what deploy strategy do we use?"}]
        messages, result = sm.inject_context(messages)
        assert result.records
        assert "blue-green" in messages[0]["content"]


# ===========================================================================
# 2. Query-blind warning
# ===========================================================================


class TestQueryBlindWarning:
    def test_auto_composite_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            ContextAssembler(
                model_class=QueryBlindMemory, score_weights={"relevance": 1.0}
            )
        messages = [r.getMessage() for r in caplog.records]
        joined = "\n".join(messages)
        assert "composite" in joined
        assert "BM25Field" in joined
        assert "QueryBlindMemory" in joined
        assert "query-blind" in joined

    def test_warning_names_the_silencing_escape_hatch(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            ContextAssembler(model_class=QueryBlindMemory, score_weights=None)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "retrieval_mode='composite'" in joined

    def test_explicit_composite_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            ContextAssembler(
                model_class=QueryBlindMemory,
                score_weights={"relevance": 1.0},
                retrieval_mode="composite",
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "query-blind" not in joined

    def test_query_sensitive_model_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            ContextAssembler(model_class=QuerySensitiveMemory, score_weights=None)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "query-blind" not in joined

    def test_subconscious_memory_over_query_blind_model_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            SubconsciousMemory(
                model_class=QueryBlindMemory,
                agent_id="agent-1",
                score_weights={"relevance": 1.0},
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "BM25Field" in joined

    def test_default_model_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            SubconsciousMemory(agent_id="agent-1")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "query-blind" not in joined


# ===========================================================================
# 3. Content-first injection format
# ===========================================================================


class TestContentFirstInjection:
    def _seed(self):
        DefaultMemory(
            agent_id="agent-1",
            content="The deploy pipeline uses a blue-green strategy.",
            importance=0.9,
        ).save()

    def test_content_format_is_the_subconscious_default(self):
        sm = SubconsciousMemory(agent_id="agent-1")
        assert sm.output_format == "content"
        assert sm._assembler.output_format == "content"

    def test_assembler_default_format_unchanged(self):
        """ContextAssembler itself still defaults to structured JSON."""
        assembler = ContextAssembler(model_class=DefaultMemory, score_weights=None)
        assert assembler.output_format == "structured"

    def test_injected_payload_has_no_ids_or_epochs(self):
        self._seed()
        sm = SubconsciousMemory(agent_id="agent-1")
        messages, result = sm.inject_context(
            [{"role": "user", "content": "deploy strategy"}]
        )
        payload = result.formatted
        assert "blue-green" in payload
        assert "memory_id" not in payload
        assert "agent_id" not in payload
        assert "agent-1" not in payload
        assert "relevance" not in payload
        # No raw epoch float (17xxxxxxxx.xx) in the payload
        import re

        assert not re.search(r"\b1[6-9]\d{8}\b", payload)

    def test_content_format_overhead_under_1_5x(self):
        """Measured: JSON default was ~2.8x content chars. Content-first is thin."""
        self._seed()
        sm = SubconsciousMemory(agent_id="agent-1")
        _, result = sm.inject_context([{"role": "user", "content": "deploy strategy"}])
        content_chars = sum(len(r.content or "") for r in result.records)
        assert content_chars > 0
        assert len(result.formatted) / content_chars < 1.5

    def test_structured_opt_in_restores_legacy_payload(self):
        self._seed()
        sm = SubconsciousMemory(agent_id="agent-1", output_format="structured")
        _, result = sm.inject_context([{"role": "user", "content": "deploy strategy"}])
        assert "memory_id" in result.formatted
        assert result.formatted.lstrip().startswith("[")

    def test_structured_overhead_is_the_measured_baseline(self):
        """Pins the 'before' number the content format is measured against."""
        self._seed()
        sm = SubconsciousMemory(agent_id="agent-1", output_format="structured")
        _, result = sm.inject_context([{"role": "user", "content": "deploy strategy"}])
        content_chars = sum(len(r.content or "") for r in result.records)
        assert len(result.formatted) / content_chars > 2.0

    def test_format_content_composition_identity(self):
        """format_content(records) == framing + per-record slices (token-budget
        accounting relies on this)."""
        from popoto.recipes.context_assembler import (
            _serialize_record,
            format_content,
        )

        self._seed()
        records = list(DefaultMemory.query.filter(agent_id="agent-1"))
        serialized = [_serialize_record(r, "content", "content") for r in records]
        assert format_content(records, "content") == "\n".join(
            f"- {s}" for s in serialized
        )
        assert format_content([], "content") == ""

    def test_content_field_autodetected_from_bm25_source(self):
        """No explicit content_field -> resolved from the BM25Field source."""
        assembler = ContextAssembler(
            model_class=DefaultMemory,
            score_weights=None,
            output_format="content",
        )
        assert assembler._content_field_name == "content"

    def test_content_format_falls_back_when_no_content_field(self):
        """A model with no resolvable text field degrades, never raises."""

        class NoTextMemory(Model):
            memory_id = AutoKeyField()
            agent_id = KeyField()
            importance = FloatField(default=1.0)
            relevance = DecayingSortedField(
                base_score_field="importance",
                partition_by="agent_id",
            )

        try:
            NoTextMemory(agent_id="agent-1", importance=0.9).save()
            assembler = ContextAssembler(
                model_class=NoTextMemory,
                score_weights={"relevance": 1.0},
                output_format="content",
                retrieval_mode="composite",
            )
            result = assembler.assemble(query_cues=None, agent_id="agent-1")
            assert isinstance(result.formatted, str)
        finally:
            _clean_model("NoTextMemory")


# ===========================================================================
# 4. No dangling doc references
# ===========================================================================


class TestNoFalseGuideClaim:
    def test_module_docstring_has_no_pydanticai_claim(self):
        import popoto.recipes.subconscious_memory as mod

        text = (mod.__doc__ or "").lower()
        assert "pydanticai" not in text
        assert "pydantic ai" not in text

    def test_source_has_no_pydanticai_reference(self):
        import inspect

        import popoto.recipes.subconscious_memory as mod

        source = inspect.getsource(mod).lower()
        assert "pydanticai" not in source
