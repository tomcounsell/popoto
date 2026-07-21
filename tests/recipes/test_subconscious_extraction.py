"""Tests for SubconsciousMemory's pluggable extraction-provider wiring
(issue #461): confidence seeding, co-occurrence seeding, per-fact importance,
and per-seam failure isolation.

All tests hit real Redis (DB 15, auto-isolated via popoto.pytest_plugin).
Extraction providers are FAKE (``AbstractExtractionProvider`` subclasses
constructed in-test) -- no network, no API key, mirroring the FakeClient
injection style used in tests/benchmarks/test_judged.py.

See also:
- tests/test_extraction.py for provider-level unit tests
- tests/test_subconscious_memory_integration.py for the pre-existing
  behavioral-gap integration suite (unaffected by this change)
"""

import logging

import pytest

from popoto import (
    AutoKeyField,
    ConfidenceField,
    CoOccurrenceField,
    FloatField,
    KeyField,
    Model,
    StringField,
)
from popoto.extraction import AbstractExtractionProvider, ExtractedFact
from popoto.fields.constants import Defaults
from popoto.recipes.subconscious_memory import SubconsciousMemory
from popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Test Models
# ---------------------------------------------------------------------------


class ExtMemory(Model):
    """Minimal model -- content + importance only, no ConfidenceField/
    CoOccurrenceField. Used for the default-behavior equivalence tests."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)


class ExtMemoryFull(Model):
    """Full model -- has both a ConfidenceField and a CoOccurrenceField, for
    the seam-specific seeding tests."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    confidence = ConfidenceField(initial_confidence=0.5)
    associations = CoOccurrenceField(symmetric=True, max_edges=50)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    _clean_keys(
        "*ExtMemoryFull*",
        "$EF:*ExtMemoryFull*",
        "$FS:*ExtMemoryFull*",
        "$CoOcF:*ExtMemoryFull*",
        "$ConfidencF:*ExtMemoryFull*",
        "$SortedF:*ExtMemoryFull*",
        "*ExtMemory:*",
        "$EF:*ExtMemory:*",
        "$FS:*ExtMemory:*",
    )


@pytest.fixture(autouse=True)
def clean_redis():
    _clean_all()
    yield
    _clean_all()


class FakeProvider(AbstractExtractionProvider):
    """A fake AbstractExtractionProvider that returns a canned list of
    ExtractedFact records, recording every extract() call."""

    def __init__(self, facts):
        self._facts = facts
        self.calls = []

    def extract(self, text):
        self.calls.append(text)
        return list(self._facts)


# ===========================================================================
# Default-behavior equivalence (no new kwargs passed)
# ===========================================================================


class TestDefaultBehaviorEquivalence:
    """extract_memories() with no extraction_provider/confidence_field/
    co_occurrence_field kwargs must write IDENTICAL records to pre-change
    behavior: same content, flat importance from the caller arg, no
    CoOccurrenceField links, no ConfidenceField update triggered."""

    def test_content_and_flat_importance(self):
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="default-agent",
            score_weights={"relevance": 0.6},
        )

        saved = sm.extract_memories(
            "First sentence goes here for real. Second sentence follows too.",
            importance=0.42,
        )

        assert len(saved) == 2
        contents = sorted(r.content for r in saved)
        assert contents == [
            "First sentence goes here for real.",
            "Second sentence follows too.",
        ]
        for r in saved:
            assert r.importance == 0.42
            assert r.agent_id == "default-agent"

    def test_no_confidence_field_present_no_crash(self):
        """Model has no ConfidenceField at all -- extract_memories must not
        error even though the recipe supports confidence seeding."""
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="default-agent-2",
            score_weights={"relevance": 0.6},
        )
        saved = sm.extract_memories(
            "A single sentence with no opinions.", importance=0.5
        )
        assert len(saved) == 1

    def test_no_co_occurrence_field_present_no_crash(self):
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="default-agent-3",
            score_weights={"relevance": 0.6},
        )
        saved = sm.extract_memories(
            "Another plain sentence right here.", importance=0.5
        )
        assert len(saved) == 1

    def test_empty_response_returns_empty(self):
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="default-agent-4",
            score_weights={"relevance": 0.6},
        )
        assert sm.extract_memories("", importance=0.5) == []
        assert sm.extract_memories("   ", importance=0.5) == []


# ===========================================================================
# Co-occurrence seeding
# ===========================================================================


class TestCoOccurrenceSeeding:
    def test_edge_created_with_two_plus_entities_and_field_configured(self):
        provider = FakeProvider(
            [
                ExtractedFact(
                    text="Alice and Bob met.",
                    entities=["Alice", "Bob"],
                )
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="coocc-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            co_occurrence_field="associations",
        )

        saved = sm.extract_memories("Alice and Bob met.", importance=0.5)
        assert len(saved) == 1

        field = ExtMemoryFull.associations
        linked_from_alice = field.get_linked(ExtMemoryFull, "Alice")
        linked_from_bob = field.get_linked(ExtMemoryFull, "Bob")
        assert any(pk == "Bob" for pk, _weight in linked_from_alice)
        assert any(pk == "Alice" for pk, _weight in linked_from_bob)

    def test_no_edge_when_co_occurrence_field_not_configured(self):
        provider = FakeProvider(
            [
                ExtractedFact(
                    text="Carol and Dave met.",
                    entities=["Carol", "Dave"],
                )
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="coocc-agent-2",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            # co_occurrence_field intentionally omitted
        )

        saved = sm.extract_memories("Carol and Dave met.", importance=0.5)
        assert len(saved) == 1

        field = ExtMemoryFull.associations
        assert field.get_linked(ExtMemoryFull, "Carol") == []
        assert field.get_linked(ExtMemoryFull, "Dave") == []

    def test_no_edge_when_fewer_than_two_entities(self):
        provider = FakeProvider(
            [ExtractedFact(text="Solo mention of Eve.", entities=["Eve"])]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="coocc-agent-3",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            co_occurrence_field="associations",
        )

        saved = sm.extract_memories("Solo mention of Eve.", importance=0.5)
        assert len(saved) == 1

        field = ExtMemoryFull.associations
        assert field.get_linked(ExtMemoryFull, "Eve") == []

    def test_duplicate_entities_deduped_no_self_loop_error(self):
        """A fact whose entities list repeats a name (e.g. ["Alice", "Bob",
        "Alice"]) must not raise -- CoOccurrenceField.link() rejects
        self-pairs, so _seed_associations dedupes entities (order-stable)
        before pairing. This pins the B2 fix: exactly one edge pair
        (Alice, Bob) is created, with no self-pair attempted."""
        provider = FakeProvider(
            [
                ExtractedFact(
                    text="Alice mentioned Bob, then Alice again.",
                    entities=["Alice", "Bob", "Alice"],
                )
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="coocc-agent-4",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            co_occurrence_field="associations",
        )

        # Must not raise (e.g. a ValueError from a self-pair reaching link()).
        saved = sm.extract_memories(
            "Alice mentioned Bob, then Alice again.", importance=0.5
        )
        assert len(saved) == 1

        field = ExtMemoryFull.associations
        linked_from_alice = field.get_linked(ExtMemoryFull, "Alice")
        linked_from_bob = field.get_linked(ExtMemoryFull, "Bob")

        # Exactly one edge pair: Alice<->Bob, no self-loop on Alice.
        assert [pk for pk, _weight in linked_from_alice] == ["Bob"]
        assert [pk for pk, _weight in linked_from_bob] == ["Alice"]

    def test_entity_pairing_capped_at_max_entities_per_fact(self):
        """A fact with more entities than
        Defaults.EXTRACTION_MAX_ENTITIES_PER_FACT must only pair the first
        N (deduped, order-stable) entities, not the full combinatorial set.
        This pins the O(n^2) cap: without it, a malformed/adversarial
        extraction result with many entities could generate an unbounded
        burst of co-occurrence writes for a single fact."""
        cap = Defaults.EXTRACTION_MAX_ENTITIES_PER_FACT
        entities = [f"Entity{i}" for i in range(cap + 5)]
        provider = FakeProvider(
            [ExtractedFact(text="Many entities mentioned.", entities=entities)]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="coocc-agent-cap",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            co_occurrence_field="associations",
        )

        saved = sm.extract_memories("Many entities mentioned.", importance=0.5)
        assert len(saved) == 1

        field = ExtMemoryFull.associations

        # Entities within the cap got paired (and thus linked).
        for name in entities[:cap]:
            assert field.get_linked(ExtMemoryFull, name) != []

        # Entities beyond the cap were excluded from pairing entirely.
        for name in entities[cap:]:
            assert field.get_linked(ExtMemoryFull, name) == []


# ===========================================================================
# Per-fact importance override
# ===========================================================================


class TestPerFactImportance:
    def test_fact_importance_overrides_flat_default(self):
        provider = FakeProvider(
            [
                ExtractedFact(text="High importance fact.", importance=0.95),
                ExtractedFact(text="No opinion fact.", importance=None),
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="importance-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
        )

        saved = sm.extract_memories("irrelevant raw text", importance=0.2)
        assert len(saved) == 2

        by_content = {r.content: r for r in saved}
        assert by_content["High importance fact."].importance == 0.95
        # No opinion -> falls back to the caller-supplied flat importance.
        assert by_content["No opinion fact."].importance == 0.2


# ===========================================================================
# Confidence seeding
# ===========================================================================


class TestConfidenceSeeding:
    def test_confidence_field_updated_when_configured_and_fact_has_opinion(self):
        provider = FakeProvider(
            [ExtractedFact(text="A confident fact.", confidence=0.95)]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="conf-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            confidence_field="confidence",
        )

        saved = sm.extract_memories("irrelevant raw text", importance=0.5)
        assert len(saved) == 1
        instance = saved[0]

        stored = ConfidenceField.get_confidence(instance, "confidence")
        # update_confidence() blends the signal with initial_confidence
        # (0.5) rather than storing the signal verbatim -- so the stored
        # value should differ from the model's untouched initial_confidence.
        assert stored != 0.5

    def test_confidence_field_untouched_when_not_configured(self):
        provider = FakeProvider(
            [ExtractedFact(text="An unseeded fact.", confidence=0.95)]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="conf-agent-2",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            # confidence_field intentionally omitted
        )

        saved = sm.extract_memories("irrelevant raw text", importance=0.5)
        assert len(saved) == 1
        instance = saved[0]

        stored = ConfidenceField.get_confidence(instance, "confidence")
        assert stored == 0.5

    def test_confidence_field_untouched_when_fact_has_no_opinion(self):
        provider = FakeProvider(
            [ExtractedFact(text="No confidence opinion.", confidence=None)]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="conf-agent-3",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            confidence_field="confidence",
        )

        saved = sm.extract_memories("irrelevant raw text", importance=0.5)
        assert len(saved) == 1
        instance = saved[0]

        stored = ConfidenceField.get_confidence(instance, "confidence")
        assert stored == 0.5


# ===========================================================================
# Per-record save-failure isolation
# ===========================================================================


class TestSaveFailureIsolation:
    def test_one_failing_save_does_not_abort_the_rest(self, monkeypatch, caplog):
        provider = FakeProvider(
            [
                ExtractedFact(text="Good fact one."),
                ExtractedFact(text="Poison fact."),
                ExtractedFact(text="Good fact two."),
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemory,
            agent_id="fail-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
        )

        real_save = ExtMemory.save

        def flaky_save(self, *args, **kwargs):
            if self.content == "Poison fact.":
                raise RuntimeError("simulated save failure")
            return real_save(self, *args, **kwargs)

        monkeypatch.setattr(ExtMemory, "save", flaky_save)

        with caplog.at_level(logging.WARNING, logger="POPOTO.SubconsciousMemory"):
            saved = sm.extract_memories("irrelevant raw text", importance=0.5)

        contents = sorted(r.content for r in saved)
        assert contents == ["Good fact one.", "Good fact two."]
        assert any(
            "failed to save" in r.message.lower() for r in caplog.records
        ), f"Expected a warning log, got: {[r.message for r in caplog.records]}"


# ===========================================================================
# link()/update_confidence() failure does not lose the saved memory
# ===========================================================================


class TestSeamFailureDoesNotLoseSavedMemory:
    def test_co_occurrence_link_failure_keeps_saved_memory(self, monkeypatch, caplog):
        provider = FakeProvider(
            [
                ExtractedFact(
                    text="Frank and Grace collaborated.",
                    entities=["Frank", "Grace"],
                )
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="link-fail-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            co_occurrence_field="associations",
        )

        def flaky_link(self, *args, **kwargs):
            raise RuntimeError("simulated link failure")

        monkeypatch.setattr(type(ExtMemoryFull.associations), "link", flaky_link)

        with caplog.at_level(logging.WARNING, logger="POPOTO.SubconsciousMemory"):
            saved = sm.extract_memories("irrelevant raw text", importance=0.5)

        assert len(saved) == 1
        assert saved[0].content == "Frank and Grace collaborated."
        assert any(
            "link" in r.message.lower() and "failed" in r.message.lower()
            for r in caplog.records
        ), f"Expected a link-failure warning log, got: {[r.message for r in caplog.records]}"

    def test_update_confidence_failure_keeps_saved_memory(self, monkeypatch, caplog):
        provider = FakeProvider(
            [
                ExtractedFact(
                    text="A fact whose confidence seed will fail.", confidence=0.8
                )
            ]
        )
        sm = SubconsciousMemory(
            model_class=ExtMemoryFull,
            agent_id="confseed-fail-agent",
            score_weights={"relevance": 0.6},
            extraction_provider=provider,
            confidence_field="confidence",
        )

        def flaky_update_confidence(
            cls, model_instance, field_name, signal, pipeline=None
        ):
            raise RuntimeError("simulated confidence update failure")

        monkeypatch.setattr(
            ConfidenceField, "update_confidence", classmethod(flaky_update_confidence)
        )

        with caplog.at_level(logging.WARNING, logger="POPOTO.SubconsciousMemory"):
            saved = sm.extract_memories("irrelevant raw text", importance=0.5)

        assert len(saved) == 1
        assert saved[0].content == "A fact whose confidence seed will fail."
        assert any(
            "confidence" in r.message.lower() and "failed" in r.message.lower()
            for r in caplog.records
        ), f"Expected a confidence-seed-failure warning log, got: {[r.message for r in caplog.records]}"
