"""Verbatim guide example tests.

Each test mirrors a code block from a published guide doc. Variable names,
imports, and API calls match the guide text exactly. Where cleanup or
assertions are needed they are added after the guide code, clearly separated.

Guides covered:
- docs/guides/agent-memory-quickstart.md (Levels 1-5)
- docs/guides/policy-cache-recipe.md (Quick Start)
- docs/features/context-assembler.md (Usage + LLM Integration)
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
# Helpers
# ---------------------------------------------------------------------------


def _clean_keys(*patterns):
    """Remove all Redis keys matching the given patterns."""
    for pattern in patterns:
        keys = POPOTO_REDIS_DB.keys(pattern)
        if keys:
            POPOTO_REDIS_DB.delete(*keys)


def _clean_all():
    _clean_keys(
        "*GuideL1Memory*",
        "*GuideL2Memory*",
        "*GuideL3Memory*",
        "*GuideL4Memory*",
        "*GuideL5Memory*",
        "*GuidePCEntry*",
        "*GuideCAMemory*",
        "$EF:*Guide*",
        "$FS:*Guide*",
        "$CoOcF:*Guide*",
        "$ConfidencF:*Guide*",
        "$SortedF:*Guide*",
        "$AT:*Guide*",
        "$WF:*Guide*",
    )


@pytest.fixture(autouse=True)
def clean_redis():
    _clean_all()
    yield
    _clean_all()


# ===========================================================================
# Quickstart Level 1: Recall
# ===========================================================================


class GuideL1Memory(Model):
    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )


class TestQuickstartLevel1:
    def test_save_and_retrieve(self):
        # --- Code from guide (verbatim) ---
        Memory = GuideL1Memory

        Memory(
            agent_id="agent-1",
            content="Deploy uses blue-green strategy",
            importance=2.0,
        ).save()
        Memory(agent_id="agent-1", content="Lunch order: salad", importance=0.5).save()

        results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
        for m in results:
            print(m.content)

        # --- Assertions ---
        assert len(results) == 2
        contents = {r.content for r in results}
        assert "Deploy uses blue-green strategy" in contents
        assert "Lunch order: salad" in contents


# ===========================================================================
# Quickstart Level 2: Attention
# ===========================================================================


class GuideL2Memory(WriteFilterMixin, AccessTrackerMixin, Model):
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


class TestQuickstartLevel2:
    def test_write_filter_discards_noise(self):
        Memory = GuideL2Memory

        # --- Code from guide (verbatim) ---
        result = Memory(agent_id="agent-1", content="noise", importance=0.1).save()
        assert result is False

    def test_high_value_persists(self):
        Memory = GuideL2Memory

        # --- Code from guide (verbatim) ---
        Memory(agent_id="agent-1", content="critical finding", importance=0.9).save()

        results = Memory.query.filter(agent_id="agent-1").top_by_decay(n=5)
        results[0].confirm_access()

        # --- Assertions ---
        assert len(results) == 1
        assert results[0].content == "critical finding"


# ===========================================================================
# Quickstart Level 3: Learning
# ===========================================================================


class GuideL3Memory(WriteFilterMixin, AccessTrackerMixin, Model):
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


class TestQuickstartLevel3:
    def test_confidence_and_observation(self):
        Memory = GuideL3Memory

        # --- Code from guide (verbatim) ---
        m = Memory(
            agent_id="agent-1", content="API key rotates monthly", importance=0.8
        )
        m.save()

        ConfidenceField.update_confidence(m, "confidence", signal=0.9)

        # --- Assertions ---
        conf_after_corroborate = ConfidenceField.get_confidence(m, "confidence")
        assert conf_after_corroborate > 0.5

        # --- More guide code ---
        ConfidenceField.update_confidence(m, "confidence", signal=0.1)

        outcome_map = {m.db_key.redis_key: "acted"}
        ObservationProtocol.on_context_used([m], outcome_map)


# ===========================================================================
# Quickstart Level 4: Association
# ===========================================================================


class GuideL4Memory(WriteFilterMixin, AccessTrackerMixin, Model):
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


class TestQuickstartLevel4:
    def test_link_and_composite_score(self):
        Memory = GuideL4Memory

        # --- Code from guide (verbatim) ---
        m1 = Memory(agent_id="agent-1", content="deploy process", importance=0.8)
        m1.save()
        m2 = Memory(agent_id="agent-1", content="rollback steps", importance=0.8)
        m2.save()

        assoc_field = Memory._meta.fields["associations"]
        assoc_field.link(
            Memory, m1.db_key.redis_key, m2.db_key.redis_key, initial_weight=0.5
        )

        results = Memory.query.filter(agent_id="agent-1").composite_score(
            indexes={"relevance": 1.0},
            limit=10,
        )

        # --- Assertions ---
        assert len(results) >= 1


# ===========================================================================
# Quickstart Level 5: Cognition
# ===========================================================================


class GuideL5Memory(WriteFilterMixin, AccessTrackerMixin, Model):
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


class TestQuickstartLevel5:
    def test_context_assembly(self):
        Memory = GuideL5Memory

        Memory(agent_id="agent-1", content="deploy checklist", importance=0.9).save()

        # --- Code from guide (verbatim) ---
        assembler = ContextAssembler(
            model_class=Memory,
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        result = assembler.assemble(
            query_cues={"topic": "deployment"},
            agent_id="agent-1",
        )

        system_prompt = (
            f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}"
        )

        # --- Assertions ---
        assert result.records is not None
        assert result.formatted is not None
        assert result.metadata is not None
        assert "Relevant context:" in system_prompt

    def test_llm_integration(self):
        """Tests the LLM call example with mocked OpenAI client."""
        Memory = GuideL5Memory

        Memory(
            agent_id="agent-1",
            content="deploy uses blue-green strategy",
            importance=0.9,
        ).save()

        assembler = ContextAssembler(
            model_class=Memory,
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        # --- Code from guide (verbatim, with mocked client) ---
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "We use blue-green deployments."

        with patch("openai.OpenAI") as MockOpenAI:
            client = MockOpenAI.return_value
            client.chat.completions.create.return_value = mock_response

            result = assembler.assemble(
                query_cues={"topic": "deployment"}, agent_id="agent-1"
            )

            messages = [
                {
                    "role": "system",
                    "content": f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}",
                },
                {"role": "user", "content": "What's our deployment strategy?"},
            ]

            response = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=messages,
            )

            answer = response.choices[0].message.content

            outcome_map = {r.db_key.redis_key: "acted" for r in result.records}
            ObservationProtocol.on_context_used(result.records, outcome_map)

        # --- Assertions ---
        assert answer == "We use blue-green deployments."
        client.chat.completions.create.assert_called_once()


# ===========================================================================
# PolicyCache Recipe: Quick Start
# ===========================================================================


class TestPolicyCacheGuide:
    def test_quick_start_example(self):
        from popoto.recipes.policy_cache import (
            PolicyEntry,
            compute_fingerprint,
            initialize_q_value,
            update_q_value,
        )

        # --- Code from guide (verbatim) ---
        fp = compute_fingerprint({"task": "deploy", "env": "staging"})
        policy = PolicyEntry(
            agent_id="agent-1",
            state_fingerprint=fp,
            state_features={"task": "deploy", "env": "staging"},
            action_type="run_playbook",
            action_spec={"playbook": "deploy.yml"},
        )
        policy.save()

        initialize_q_value(policy, initial_q=0.5)

        td_error = update_q_value(policy, reward=1.0)

        # --- Assertions ---
        assert isinstance(td_error, float)

        # --- Cleanup ---
        _clean_keys(
            "*PolicyEntry*",
            "$EF:*PolicyEntry*",
            "$FS:*PolicyEntry*",
            "$CoOcF:*PolicyEntry*",
            "$ConfidencF:*PolicyEntry*",
            "$SortedF:*PolicyEntry*",
            "$AT:*PolicyEntry*",
            "stream:*",
        )


# ===========================================================================
# ContextAssembler Feature Doc: Usage + LLM Integration
# ===========================================================================


class GuideCAMemory(WriteFilterMixin, AccessTrackerMixin, Model):
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


class TestContextAssemblerGuide:
    def test_usage_example(self):
        Memory = GuideCAMemory

        Memory(agent_id="agent-1", content="deployment info", importance=0.9).save()

        # --- Code from guide (verbatim) ---
        from popoto.recipes.context_assembler import ContextAssembler

        assembler = ContextAssembler(
            model_class=Memory,
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        result = assembler.assemble(
            query_cues={"topic": "deployment"},
            agent_id="agent-1",
        )

        # --- Assertions ---
        assert hasattr(result, "records")
        assert hasattr(result, "proactive")
        assert hasattr(result, "formatted")
        assert hasattr(result, "metadata")

    def test_llm_integration(self):
        """Tests the LLM integration snippet from context-assembler.md."""
        Memory = GuideCAMemory

        Memory(agent_id="agent-1", content="deployment strategy", importance=0.9).save()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Blue-green deployment."

        with patch("openai.OpenAI") as MockOpenAI:
            client = MockOpenAI.return_value
            client.chat.completions.create.return_value = mock_response

            # --- Code from guide (verbatim, with mocked client) ---
            from popoto.recipes.context_assembler import ContextAssembler

            assembler = ContextAssembler(
                model_class=Memory,
                score_weights={"relevance": 0.6, "confidence": 0.3},
                max_items=10,
                max_tokens=4000,
            )

            result = assembler.assemble(
                query_cues={"topic": "deployment"},
                agent_id="agent-1",
            )

            messages = [
                {
                    "role": "system",
                    "content": f"You are a helpful assistant.\n\nRelevant context:\n{result.formatted}",
                },
                {"role": "user", "content": "What's our deployment strategy?"},
            ]

            response = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=messages,
            )

            answer = response.choices[0].message.content

            outcome_map = {r.db_key.redis_key: "acted" for r in result.records}
            ObservationProtocol.on_context_used(result.records, outcome_map)

        # --- Assertions ---
        assert answer == "Blue-green deployment."
        client.chat.completions.create.assert_called_once()
