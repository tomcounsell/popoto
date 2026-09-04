"""Tests for SubconsciousMemory recipe -- automatic memory injection and extraction.

Tests cover:
- inject_context with and without existing memories
- inject_context with empty messages
- extract_memories from sample response text
- extract_memories with empty response
- report_outcomes dispatching
- report_outcomes with empty assembly result
- Full round-trip with mocked LLM

See also: tests/test_subconscious_memory_integration.py for live-Redis
integration tests covering multi-memory ranking, token budgets, TTL expiry,
concurrent agent isolation, and feedback-driven confidence changes.
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
        """Saved memories land at the TAIL, leaving the cached prefix intact.

        A provider prompt cache is keyed on an exact token prefix, so appending
        after all sealed history costs only the injected tokens, while writing
        into the system message would invalidate the whole context on every
        turn recall changes.
        """
        SCMemory(
            agent_id="agent-1", content="We use blue-green deployments", importance=0.9
        ).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's our deployment strategy?"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        assert len(assembly.records) >= 1
        # The system message is untouched...
        assert result_msgs[0]["content"] == "You are a helpful assistant."
        # ...and the block rides the last (user) message.
        assert "Relevant context:" in result_msgs[-1]["content"]
        assert result_msgs[-1]["role"] == "user"
        assert result_msgs[-1]["content"].startswith("What's our deployment strategy?")

    def test_inject_position_system_is_opt_in(self, sm):
        """The pre-1.9 cache-hostile placement is still reachable explicitly."""
        SCMemory(
            agent_id="agent-1", content="We use blue-green deployments", importance=0.9
        ).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's our deployment strategy?"},
        ]
        result_msgs, assembly = sm.inject_context(messages, position="system")

        assert len(assembly.records) >= 1
        assert "Relevant context:" in result_msgs[0]["content"]

    def test_inject_rejects_unknown_position(self, sm):
        with pytest.raises(ValueError, match="position must be"):
            sm.inject_context([{"role": "user", "content": "hi"}], position="middle")

    def test_inject_appends_new_message_when_tail_is_not_user(self, sm):
        """Never edit an earlier message: that is a mutation of sealed history.

        When the array ends on an assistant turn, appending to the last *user*
        message would put the write behind cached tokens. A new trailing
        message keeps it at the true end.
        """
        SCMemory(agent_id="agent-1", content="A durable fact", importance=0.9).save()

        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        assert len(assembly.records) >= 1
        assert len(result_msgs) == 3
        assert result_msgs[0]["content"] == "First question"
        assert result_msgs[1]["content"] == "First answer"
        assert result_msgs[-1]["role"] == "user"
        assert "Relevant context:" in result_msgs[-1]["content"]

    def test_inject_does_not_mutate_caller_messages(self, sm):
        """Tail injection copies rather than editing the caller's dicts."""
        SCMemory(agent_id="agent-1", content="A durable fact", importance=0.9).save()

        original_user = {"role": "user", "content": "Tell me something"}
        messages = [original_user]
        result_msgs, _ = sm.inject_context(messages)

        assert original_user["content"] == "Tell me something"
        assert "Relevant context:" in result_msgs[-1]["content"]

    def test_inject_exclude_keys_suppresses_repeat(self, sm):
        """Suppression is what keeps resident tokens from growing every turn."""
        SCMemory(agent_id="agent-1", content="A repeatable fact", importance=0.9).save()

        messages = [{"role": "user", "content": "Tell me something"}]
        _, first = sm.inject_context(messages)
        assert first.records

        seen = {r.db_key.redis_key for r in first.records}
        result_msgs, second = sm.inject_context(
            [{"role": "user", "content": "Tell me something"}], exclude_keys=seen
        )
        assert not seen & {r.db_key.redis_key for r in second.records}

    def test_inject_creates_system_message_if_absent(self, sm):
        """position="system" still synthesizes a system message when absent."""
        SCMemory(agent_id="agent-1", content="Important fact", importance=0.9).save()

        messages = [
            {"role": "user", "content": "Tell me something"},
        ]
        result_msgs, assembly = sm.inject_context(messages, position="system")

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
        result_msgs, assembly = sm.inject_context(messages, position="system")

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

    def test_extract_accepts_context_kwarg_on_the_default_path(self, sm):
        """M4 (#563) threads a new ``context=`` kwarg through extract_memories.

        On the default (non-auditable) path it is accepted and ignored --
        it only matters on the auditable path (see
        tests/test_reference_resolution.py). Existing callers that omit it
        (every test above this one) must keep working unchanged.
        """
        from popoto.extraction.resolution import TurnContext

        response = "We deploy using blue-green strategy."
        without_context = sm.extract_memories(response, importance=0.6)
        with_context = sm.extract_memories(
            response, importance=0.6, context=TurnContext.now()
        )

        assert len(without_context) == 1
        assert len(with_context) == 1
        assert without_context[0].content == with_context[0].content


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
        pytest.importorskip("openai")
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
