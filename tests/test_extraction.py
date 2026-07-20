"""Tests for popoto.extraction: ExtractedFact, providers, and the pluggable
extraction seam used by SubconsciousMemory.

Covers:
- HeuristicExtractionProvider is a byte-identical port of the pre-change
  ``SubconsciousMemory._split_sentences`` + min-length-filter behavior.
- ``import popoto`` / ``import popoto.extraction`` never pull in ``anthropic``
  (opt-in-only contract).
- ``ClaudeExtractionProvider()`` raises an actionable ``ImportError`` when
  ``anthropic`` is unavailable.
- ``ClaudeExtractionProvider.extract()`` parsing/clamping/failure paths via an
  injected FAKE client (no network, no API key) -- same seam style as
  ``tests/benchmarks/test_judged.py``'s ``FakeClient``.
- Model/prompt pins.

Runs with **no real anthropic API calls** -- the client is always a fake
injected via the constructor's ``self._client`` seam.
"""

import logging
import sys

import pytest

from popoto.extraction import (
    AbstractExtractionProvider,
    DEFAULT_MIN_LENGTH,
    ExtractedFact,
    HeuristicExtractionProvider,
)
from popoto.fields.constants import Defaults
from popoto.recipes.subconscious_memory import SubconsciousMemory


# ---------------------------------------------------------------------------
# HeuristicExtractionProvider: before/after equivalence pin
# ---------------------------------------------------------------------------


class TestHeuristicEquivalence:
    """Pin HeuristicExtractionProvider against the pre-change
    SubconsciousMemory._split_sentences() + min-length-filter logic."""

    REPRESENTATIVE_INPUTS = [
        "The API uses REST endpoints. Authentication is via Bearer tokens.",
        "Is this working? Yes it is! Great news.",
        "- item one\n- item two\n- item three",
        "Version 3.14 was released. It includes fixes.",
        "Visit https://example.com. Next sentence here.",
        "",
        "   ",
        "Short. Ok.",
    ]

    def _expected(self, text, min_length=DEFAULT_MIN_LENGTH):
        """Reproduce the pre-change logic directly against the pinned
        SubconsciousMemory._split_sentences staticmethod."""
        if not text or not text.strip():
            return []
        sentences = SubconsciousMemory._split_sentences(text)
        return [s.strip() for s in sentences if len(s.strip()) >= min_length]

    @pytest.mark.parametrize("text", REPRESENTATIVE_INPUTS)
    def test_matches_pre_change_behavior(self, text):
        provider = HeuristicExtractionProvider()
        facts = provider.extract(text)
        got = [f.text for f in facts]
        assert got == self._expected(text)

    def test_matches_pre_change_behavior_custom_min_length(self):
        text = "Hi. This is a longer sentence that survives filtering."
        provider = HeuristicExtractionProvider(min_length=5)
        facts = provider.extract(text)
        got = [f.text for f in facts]
        assert got == self._expected(text, min_length=5)

    def test_facts_have_no_entities_or_opinions(self):
        """The heuristic provider never has an opinion -- entities empty,
        importance/confidence None -- so downstream falls back to the
        caller-supplied importance and leaves confidence untouched."""
        provider = HeuristicExtractionProvider()
        facts = provider.extract("A sufficiently long first sentence here. And a second one too.")
        assert len(facts) == 2
        for fact in facts:
            assert fact.entities == []
            assert fact.importance is None
            assert fact.confidence is None

    def test_empty_and_whitespace_input(self):
        provider = HeuristicExtractionProvider()
        assert provider.extract("") == []
        assert provider.extract("   ") == []
        assert provider.extract(None) == []

    def test_below_min_length_filtered(self):
        provider = HeuristicExtractionProvider(min_length=10)
        facts = provider.extract("Hi. Ok.")
        assert facts == []


# ---------------------------------------------------------------------------
# Opt-in-only import contract
# ---------------------------------------------------------------------------


class TestOptInImportContract:
    def test_import_popoto_does_not_import_anthropic(self):
        # popoto is already imported by the test collection process; this
        # asserts the *transitive* import graph never pulled anthropic in.
        import popoto  # noqa: F401

        assert "anthropic" not in sys.modules

    def test_import_popoto_extraction_does_not_import_anthropic(self):
        import popoto.extraction  # noqa: F401

        assert "anthropic" not in sys.modules

    def test_claude_module_importable_without_anthropic_installed(self):
        """popoto.extraction.claude itself is always importable (the
        ``import anthropic`` inside it is wrapped in try/except), regardless
        of whether the anthropic SDK happens to be installed in this env."""
        import popoto.extraction.claude as claude_mod

        assert claude_mod.ClaudeExtractionProvider is not None


# ---------------------------------------------------------------------------
# ClaudeExtractionProvider: ImportError when anthropic unavailable
# ---------------------------------------------------------------------------


class TestClaudeProviderImportError:
    def test_raises_actionable_import_error_when_anthropic_unavailable(
        self, monkeypatch
    ):
        import popoto.extraction.claude as claude_mod

        monkeypatch.setattr(claude_mod, "_anthropic_available", False)

        with pytest.raises(ImportError, match=r"pip install popoto\[anthropic\]"):
            claude_mod.ClaudeExtractionProvider()

    def test_error_message_names_anthropic(self, monkeypatch):
        import popoto.extraction.claude as claude_mod

        monkeypatch.setattr(claude_mod, "_anthropic_available", False)

        with pytest.raises(ImportError, match="anthropic"):
            claude_mod.ClaudeExtractionProvider()


# ---------------------------------------------------------------------------
# ClaudeExtractionProvider: parsing/clamping/failure via injected fake client
# ---------------------------------------------------------------------------


class FakeAnthropicTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeAnthropicResponse:
    def __init__(self, text):
        self.content = [FakeAnthropicTextBlock(text)]


class FakeAnthropicMessages:
    """Fake ``client.messages`` -- records calls, returns a canned
    ``FakeAnthropicResponse`` (or raises) from ``create()``."""

    def __init__(self, response_text=None, raise_exc=None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeAnthropicResponse(self.response_text)


class FakeAnthropicClient:
    def __init__(self, response_text=None, raise_exc=None):
        self.messages = FakeAnthropicMessages(
            response_text=response_text, raise_exc=raise_exc
        )


def _make_provider(monkeypatch, response_text=None, raise_exc=None):
    """Build a ClaudeExtractionProvider with a fake client injected via the
    constructor's ``self._client`` seam -- no network, no API key.

    Mirrors tests/benchmarks/test_judged.py's FakeClient-injection style:
    bypass __init__'s real Anthropic() construction by forcing
    _anthropic_available True (so the ImportError guard is skipped) and
    monkeypatching the module's Anthropic class to a lightweight factory.
    """
    import popoto.extraction.claude as claude_mod

    fake_client = FakeAnthropicClient(response_text=response_text, raise_exc=raise_exc)

    class _FakeAnthropicModule:
        @staticmethod
        def Anthropic(api_key=None):
            return fake_client

    monkeypatch.setattr(claude_mod, "_anthropic_available", True)
    monkeypatch.setattr(claude_mod, "anthropic_module", _FakeAnthropicModule())

    provider = claude_mod.ClaudeExtractionProvider(api_key="fake-key")
    return provider, fake_client


class TestClaudeProviderExtraction:
    def test_well_formed_response_produces_correct_facts(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    {
                        "text": "Alice met Bob in Paris.",
                        "entities": ["Alice", "Bob", "Paris"],
                        "importance": 0.8,
                        "confidence": 0.9,
                    },
                    {
                        "text": "The meeting was productive.",
                        "entities": [],
                        "importance": 0.3,
                        "confidence": 0.6,
                    },
                ]
            }
        )
        provider, fake_client = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("Alice met Bob in Paris. The meeting was productive.")

        assert len(facts) == 2
        assert facts[0] == ExtractedFact(
            text="Alice met Bob in Paris.",
            entities=["Alice", "Bob", "Paris"],
            importance=0.8,
            confidence=0.9,
        )
        assert facts[1] == ExtractedFact(
            text="The meeting was productive.",
            entities=[],
            importance=0.3,
            confidence=0.6,
        )
        # One API call, using the pinned model/prompt/schema.
        assert len(fake_client.messages.calls) == 1
        call = fake_client.messages.calls[0]
        assert call["model"] == claude_module_constant("EXTRACTION_MODEL")
        assert call["system"] == claude_module_constant("EXTRACTION_PROMPT")

    def test_missing_importance_and_confidence_default(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    {"text": "A fact with no opinions.", "entities": []},
                ]
            }
        )
        provider, _ = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("A fact with no opinions.")

        assert len(facts) == 1
        assert facts[0].importance == Defaults.EXTRACTION_DEFAULT_IMPORTANCE
        assert facts[0].confidence == Defaults.EXTRACTION_DEFAULT_CONFIDENCE

    def test_out_of_range_importance_and_confidence_clamped(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    {
                        "text": "Too high and too low.",
                        "entities": [],
                        "importance": 5.0,
                        "confidence": -3.0,
                    },
                ]
            }
        )
        provider, _ = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("Too high and too low.")

        assert len(facts) == 1
        assert facts[0].importance == 1.0
        assert facts[0].confidence == 0.0

    def test_non_numeric_importance_defaults(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    {
                        "text": "Non-numeric opinions.",
                        "entities": [],
                        "importance": "not-a-number",
                        "confidence": None,
                    },
                ]
            }
        )
        provider, _ = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("Non-numeric opinions.")

        assert len(facts) == 1
        assert facts[0].importance == Defaults.EXTRACTION_DEFAULT_IMPORTANCE
        assert facts[0].confidence == Defaults.EXTRACTION_DEFAULT_CONFIDENCE

    def test_malformed_json_returns_empty_and_logs_warning(self, monkeypatch, caplog):
        provider, _ = _make_provider(monkeypatch, response_text="not json at all {{{")

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            facts = provider.extract("Some text.")

        assert facts == []
        assert any(
            "extraction failed" in r.message.lower() for r in caplog.records
        ), f"Expected a warning log, got: {[r.message for r in caplog.records]}"

    def test_missing_facts_key_returns_empty_and_logs_warning(self, monkeypatch, caplog):
        import json

        provider, _ = _make_provider(
            monkeypatch, response_text=json.dumps({"not_facts": []})
        )

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            facts = provider.extract("Some text.")

        assert facts == []
        assert any(
            "facts" in r.message.lower() for r in caplog.records
        ), f"Expected a warning log, got: {[r.message for r in caplog.records]}"

    def test_client_raises_returns_empty_and_logs_warning(self, monkeypatch, caplog):
        provider, _ = _make_provider(
            monkeypatch, raise_exc=RuntimeError("simulated API failure")
        )

        with caplog.at_level(logging.WARNING, logger="POPOTO.extraction"):
            facts = provider.extract("Some text.")

        assert facts == []
        assert any(
            "extraction failed" in r.message.lower() for r in caplog.records
        ), f"Expected a warning log, got: {[r.message for r in caplog.records]}"

    def test_empty_and_whitespace_input_short_circuits(self, monkeypatch):
        provider, fake_client = _make_provider(monkeypatch, response_text="{}")

        assert provider.extract("") == []
        assert provider.extract("   ") == []
        # No API call should have been made for empty/blank input.
        assert fake_client.messages.calls == []

    def test_facts_with_non_dict_entries_are_skipped(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    "not a dict",
                    {"text": "A valid fact.", "entities": [], "importance": 0.5, "confidence": 0.5},
                ]
            }
        )
        provider, _ = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("Some text.")

        assert len(facts) == 1
        assert facts[0].text == "A valid fact."

    def test_facts_with_missing_or_blank_text_are_skipped(self, monkeypatch):
        import json

        response = json.dumps(
            {
                "facts": [
                    {"entities": []},
                    {"text": "   ", "entities": []},
                    {"text": "Real fact.", "entities": []},
                ]
            }
        )
        provider, _ = _make_provider(monkeypatch, response_text=response)

        facts = provider.extract("Some text.")

        assert len(facts) == 1
        assert facts[0].text == "Real fact."


def claude_module_constant(name):
    import popoto.extraction.claude as claude_mod

    return getattr(claude_mod, name)


# ---------------------------------------------------------------------------
# Model + prompt pins
# ---------------------------------------------------------------------------


class TestModelAndPromptPins:
    def test_model_is_pinned(self):
        import popoto.extraction.claude as claude_mod

        assert claude_mod.EXTRACTION_MODEL == "claude-opus-4-8"

    def test_prompt_constant_exists_and_is_nonempty(self):
        import popoto.extraction.claude as claude_mod

        assert isinstance(claude_mod.EXTRACTION_PROMPT, str)
        assert len(claude_mod.EXTRACTION_PROMPT) > 0

    def test_facts_schema_shape(self):
        import popoto.extraction.claude as claude_mod

        schema = claude_mod.FACTS_SCHEMA
        assert schema["type"] == "object"
        assert "facts" in schema["properties"]
        fact_schema = schema["properties"]["facts"]["items"]
        assert set(fact_schema["required"]) == {
            "text",
            "entities",
            "importance",
            "confidence",
        }


# ---------------------------------------------------------------------------
# ExtractedFact / AbstractExtractionProvider sanity
# ---------------------------------------------------------------------------


class TestExtractedFactAndAbstractProvider:
    def test_cannot_instantiate_abstract_provider(self):
        with pytest.raises(TypeError):
            AbstractExtractionProvider()

    def test_extracted_fact_with_fewer_than_two_entities_does_not_crash(self):
        # < 2 entities is a normal, valid case -- construction must not raise.
        f0 = ExtractedFact(text="No entities.", entities=[])
        f1 = ExtractedFact(text="One entity.", entities=["Solo"])
        assert f0.entities == []
        assert f1.entities == ["Solo"]

    def test_extracted_fact_defaults(self):
        f = ExtractedFact(text="Just text.")
        assert f.entities == []
        assert f.importance is None
        assert f.confidence is None
