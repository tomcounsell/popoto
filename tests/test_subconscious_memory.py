"""Tests for SubconsciousMemory recipe -- automatic memory injection and extraction.

Tests cover:
- inject_context with and without existing memories
- inject_context with empty messages
- extract_memories from sample response text
- extract_memories with empty response
- report_outcomes dispatching
- report_outcomes with empty assembly result
- Full round-trip with mocked LLM
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto import (
    AccessTrackerMixin,
    AutoKeyField,
    ConfidenceField,
    DecayingSortedField,
    FloatField,
    KeyField,
    Model,
    StringField,
    WriteFilterMixin,
)
from popoto.recipes.context_assembler import AssemblyResult
from popoto.recipes.subconscious_memory import SubconsciousMemory
from popoto.redis_db import POPOTO_REDIS_DB


# ---------------------------------------------------------------------------
# Test Model
# ---------------------------------------------------------------------------


class SCMemory(WriteFilterMixin, AccessTrackerMixin, Model):
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
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    _clean_keys(
        "*SCMemory*",
        "$EF:*SCMemory*",
        "$FS:*SCMemory*",
        "$CoOcF:*SCMemory*",
        "$ConfidencF:*SCMemory*",
        "$SortedF:*SCMemory*",
        "$AT:*SCMemory*",
        "$WF:*SCMemory*",
    )


@pytest.fixture(autouse=True)
def clean_redis():
    _clean_all()
    yield
    _clean_all()


@pytest.fixture
def sm():
    return SubconsciousMemory(
        model_class=SCMemory,
        agent_id="agent-1",
        score_weights={"relevance": 0.6, "confidence": 0.3},
        max_items=10,
        max_tokens=4000,
    )


# ===========================================================================
# inject_context
# ===========================================================================


class TestInjectContext:
    def test_inject_with_no_memories(self, sm):
        """With no saved memories, messages are returned unchanged."""
        messages = [
            {"role": "user", "content": "Hello"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        # No memories exist, so no context injected
        assert len(result_msgs) == 1
        assert result_msgs[0]["role"] == "user"

    def test_inject_with_existing_memories(self, sm):
        """Saved memories are injected into the system message."""
        SCMemory(
            agent_id="agent-1", content="We use blue-green deployments", importance=0.9
        ).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's our deployment strategy?"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        # System message should now contain memory context
        system_content = result_msgs[0]["content"]
        assert "Relevant context:" in system_content
        assert len(assembly.records) >= 1

    def test_inject_creates_system_message_if_absent(self, sm):
        """If no system message exists, one is created with context."""
        SCMemory(agent_id="agent-1", content="Important fact", importance=0.9).save()

        messages = [
            {"role": "user", "content": "Tell me something"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        # A system message should be prepended
        assert result_msgs[0]["role"] == "system"
        assert "Relevant context:" in result_msgs[0]["content"]
        assert result_msgs[1]["role"] == "user"

    def test_inject_with_empty_messages(self, sm):
        """Empty messages list returns unchanged."""
        result_msgs, assembly = sm.inject_context([])
        assert result_msgs == []
        assert assembly.records == []

    def test_inject_does_not_modify_original_system_msg(self, sm):
        """Original system message dict is not mutated in place."""
        SCMemory(agent_id="agent-1", content="A fact", importance=0.9).save()

        original_system = {"role": "system", "content": "Original."}
        messages = [original_system, {"role": "user", "content": "Hi"}]
        result_msgs, assembly = sm.inject_context(messages)

        # The original dict should be untouched
        assert original_system["content"] == "Original."


# ===========================================================================
# extract_memories
# ===========================================================================


class TestExtractMemories:
    def test_extract_from_response(self, sm):
        """Sentences from response text are saved as Memory records."""
        response = "We deploy using blue-green strategy. Rollbacks take under 5 minutes. The CI pipeline runs automatically."
        saved = sm.extract_memories(response, importance=0.6)

        assert len(saved) == 3
        contents = {m.content for m in saved}
        assert "We deploy using blue-green strategy." in contents

    def test_extract_empty_response(self, sm):
        """Empty response returns empty list."""
        assert sm.extract_memories("") == []
        assert sm.extract_memories("   ") == []
        assert sm.extract_memories(None) == []

    def test_extract_filters_short_sentences(self, sm):
        """Sentences shorter than extraction_min_length are skipped."""
        sm.extraction_min_length = 20
        response = "Yes. This is a longer sentence worth saving."
        saved = sm.extract_memories(response, importance=0.5)

        # "Yes." is too short (4 chars < 20), only the longer sentence saved
        assert len(saved) == 1
        assert "longer sentence" in saved[0].content

    def test_extract_respects_write_filter(self, sm):
        """Low importance memories are filtered by WriteFilterMixin."""
        response = "This is a test sentence that should be filtered."
        saved = sm.extract_memories(response, importance=0.1)  # below _wf_min_threshold

        # WriteFilter discards importance < 0.2
        assert len(saved) == 0


# ===========================================================================
# report_outcomes
# ===========================================================================


class TestReportOutcomes:
    def test_report_with_records(self, sm):
        """Outcomes are reported for assembly result records."""
        m = SCMemory(agent_id="agent-1", content="deploy info", importance=0.9)
        m.save()

        # Create a real assembly result
        assembly = AssemblyResult(records=[m])
        # Should not raise
        sm.report_outcomes(assembly, outcome="acted")

    def test_report_empty_assembly(self, sm):
        """Empty assembly result is a no-op."""
        sm.report_outcomes(AssemblyResult(), outcome="acted")

    def test_report_none_assembly(self, sm):
        """None assembly result is a no-op."""
        sm.report_outcomes(None, outcome="acted")

    def test_report_different_outcomes(self, sm):
        """Different outcome types are accepted."""
        m = SCMemory(agent_id="agent-1", content="some fact", importance=0.9)
        m.save()
        assembly = AssemblyResult(records=[m])

        for outcome in ("acted", "dismissed", "contradicted", "deferred"):
            sm.report_outcomes(assembly, outcome=outcome)


# ===========================================================================
# Full round-trip
# ===========================================================================


class TestFullRoundTrip:
    def test_inject_call_extract_report(self, sm):
        """Full cycle: inject -> mock LLM -> extract -> report."""
        # Seed a memory
        SCMemory(
            agent_id="agent-1",
            content="Deploy uses blue-green strategy",
            importance=0.9,
        ).save()

        # 1. Pre-turn: inject context
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "How do we deploy?"},
        ]
        messages, assembly = sm.inject_context(messages)

        # Verify injection happened
        assert "Relevant context:" in messages[0]["content"]

        # 2. Mock LLM call
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "We use blue-green deployments. "
            "The process takes about 10 minutes. "
            "Rollback is immediate."
        )

        with patch("openai.OpenAI") as MockOpenAI:
            client = MockOpenAI.return_value
            client.chat.completions.create.return_value = mock_response

            response = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=messages,
            )
            answer = response.choices[0].message.content

        # 3. Post-turn: extract memories
        new_memories = sm.extract_memories(answer, importance=0.6)
        assert len(new_memories) == 3

        # 4. Report outcomes
        sm.report_outcomes(assembly, outcome="acted")

    def test_round_trip_no_prior_memories(self, sm):
        """Round trip works when no prior memories exist."""
        messages = [
            {"role": "user", "content": "Hello, who are you?"},
        ]
        messages, assembly = sm.inject_context(messages)

        # No memories to inject, messages unchanged
        assert len(messages) == 1

        # Extract from response anyway
        new_memories = sm.extract_memories(
            "I am an AI assistant. I can help with many tasks.",
            importance=0.5,
        )
        assert len(new_memories) == 2

        # Report outcomes (no-op since no records)
        sm.report_outcomes(assembly, outcome="acted")
