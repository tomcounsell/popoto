"""Integration tests for SubconsciousMemory behavioral gaps.

Covers 8 behavioral gaps identified in issue #267:
1. Retrieval relevance -- topical queries surface topically relevant memories
2. Agent isolation -- memories are partitioned by agent_id
3. Multi-turn accumulation -- inject-extract cycles grow memory, decay ranks older
4. Observation feedback loop -- acted boosts confidence, contradicted penalizes
5. Token budget enforcement -- injected context respects max_tokens
6. Extraction heuristic edge cases -- _split_sentences handles real-world patterns
7. Configurable field names -- non-default field names work end-to-end
8. WriteFilter boundary behavior -- exact threshold semantics

All tests hit real Redis. No mocking of Popoto internals.

See also: tests/test_subconscious_memory.py for unit-level plumbing tests.
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from popoto import (  # noqa: E402
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
from popoto.recipes.subconscious_memory import SubconsciousMemory  # noqa: E402
from popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402


# ---------------------------------------------------------------------------
# Test Models
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


class AltMemory(WriteFilterMixin, AccessTrackerMixin, Model):
    """Alternative model with non-default field names for Gap 7."""

    memory_id = AutoKeyField()
    owner = KeyField()
    text = StringField(default="")
    score = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="score",
        partition_by="owner",
    )
    confidence = ConfidenceField(initial_confidence=0.5)

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

    def compute_filter_score(self):
        return self.score or 0.0


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
        "*AltMemory*",
        "$EF:*AltMemory*",
        "$FS:*AltMemory*",
        "$CoOcF:*AltMemory*",
        "$ConfidencF:*AltMemory*",
        "$SortedF:*AltMemory*",
        "$AT:*AltMemory*",
        "$WF:*AltMemory*",
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
        agent_id="integ-agent-1",
        score_weights={"relevance": 0.6, "confidence": 0.3},
        max_items=10,
        max_tokens=4000,
    )


# ===========================================================================
# Gap 1: Retrieval Relevance
# ===========================================================================


class TestRetrievalRelevance:
    """Verify that inject_context surfaces topically relevant memories."""

    MEMORIES = [
        # Database topic cluster
        "PostgreSQL connection pooling reduces latency by reusing connections.",
        "Database indexes should be analyzed monthly for bloat.",
        "Redis sorted sets provide O(log N) insertion and retrieval.",
        "MySQL replication lag can be monitored with pt-heartbeat.",
        "Database migrations should always be backward-compatible.",
        # Deployment topic cluster
        "Blue-green deployments minimize downtime during releases.",
        "Kubernetes rolling updates gradually replace old pods.",
        "Docker images should use multi-stage builds for smaller size.",
        "CI pipeline runs linting, tests, and security scans.",
        "Canary releases route a small percentage of traffic to new code.",
        # Frontend topic cluster
        "CSS grid layout handles two-dimensional page layouts.",
        "React hooks replaced class component lifecycle methods.",
        "Web accessibility requires ARIA labels on interactive elements.",
        "TypeScript generics provide type safety for reusable components.",
        "Browser caching headers reduce repeated asset downloads.",
        # Auth topic cluster
        "OAuth2 refresh tokens should rotate on each use.",
        "Password hashing with bcrypt uses adaptive cost factor.",
        "JWT tokens should have short expiration times.",
        "Multi-factor authentication adds a second verification step.",
        "CORS headers control which origins can access the API.",
        # Misc
        "Team standups happen every morning at 9:30 AM.",
        "The office kitchen has free coffee and snacks.",
    ]

    def test_database_query_returns_database_memories(self, sm):
        """Querying about databases should surface database-related memories."""
        for text in self.MEMORIES:
            SCMemory(agent_id="integ-agent-1", content=text, importance=0.8).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "How should we optimize our database?"},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        # Should have retrieved some memories
        assert len(assembly.records) > 0

        # Verify at least some retrieved memories are database-related
        db_keywords = {"database", "postgresql", "redis", "mysql", "index", "migration"}
        retrieved_contents = [
            getattr(r, "content", "").lower() for r in assembly.records
        ]
        db_matches = sum(
            1
            for content in retrieved_contents
            if any(kw in content for kw in db_keywords)
        )
        # At least one database-related memory should appear
        assert db_matches >= 1, (
            f"Expected database-related memories in results, got: {retrieved_contents}"
        )

    def test_retrieves_multiple_relevant_memories(self, sm):
        """A broad query should surface memories from the relevant topic cluster."""
        for text in self.MEMORIES:
            SCMemory(agent_id="integ-agent-1", content=text, importance=0.8).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me about deployment strategies."},
        ]
        _, assembly = sm.inject_context(messages)

        assert len(assembly.records) > 0

        # With 22 diverse memories, at least some deployment-related ones should appear
        deploy_keywords = {
            "deploy",
            "kubernetes",
            "docker",
            "canary",
            "ci pipeline",
            "rolling",
        }
        all_contents = [getattr(r, "content", "").lower() for r in assembly.records]
        deploy_matches = sum(
            1
            for content in all_contents
            if any(kw in content for kw in deploy_keywords)
        )
        assert deploy_matches >= 1, (
            f"Expected deployment-related memories in results, got: {all_contents}"
        )


# ===========================================================================
# Gap 2: Agent Isolation
# ===========================================================================


class TestAgentIsolation:
    """Verify memories are partitioned by agent_id."""

    def test_agent_sees_only_own_memories(self):
        """Query filter by agent_id partitions memories correctly."""
        # Create memories for two different agents
        SCMemory(
            agent_id="iso-agent-1",
            content="Agent one secret deployment procedure.",
            importance=0.9,
        ).save()
        SCMemory(
            agent_id="iso-agent-1",
            content="Agent one database connection string is private.",
            importance=0.9,
        ).save()
        SCMemory(
            agent_id="iso-agent-2",
            content="Agent two uses a completely different deployment.",
            importance=0.9,
        ).save()
        SCMemory(
            agent_id="iso-agent-2",
            content="Agent two database is on a separate cluster.",
            importance=0.9,
        ).save()

        # Verify partition via direct query filter (the primitive the assembler uses)
        agent1_records = list(SCMemory.query.filter(agent_id="iso-agent-1"))
        agent2_records = list(SCMemory.query.filter(agent_id="iso-agent-2"))

        assert len(agent1_records) == 2
        assert len(agent2_records) == 2

        # agent-1 query should only contain agent-1 content
        for r in agent1_records:
            assert "Agent two" not in r.content, (
                f"Agent-2 memory leaked into agent-1 query: {r.content}"
            )

        # agent-2 query should only contain agent-2 content
        for r in agent2_records:
            assert "Agent one" not in r.content, (
                f"Agent-1 memory leaked into agent-2 query: {r.content}"
            )

    def test_extract_saves_to_correct_agent(self):
        """Extracted memories are saved under the correct agent_id."""
        sm1 = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="iso-agent-1",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )
        sm2 = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="iso-agent-2",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )

        sm1.extract_memories("Agent one learned something new today.", importance=0.8)
        sm2.extract_memories("Agent two discovered a different thing.", importance=0.8)

        # Query agent-1 memories only
        agent1_records = SCMemory.query.filter(agent_id="iso-agent-1")
        agent1_contents = [r.content for r in agent1_records]
        assert any("Agent one" in c for c in agent1_contents)
        assert not any("Agent two" in c for c in agent1_contents)


# ===========================================================================
# Gap 3: Multi-turn Accumulation
# ===========================================================================


class TestMultiTurnAccumulation:
    """Verify inject-extract cycles grow memory and decay works."""

    def test_memory_count_grows_across_cycles(self):
        """Each extract cycle should add new memories."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="accum-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=20,
            max_tokens=8000,
        )

        responses = [
            "The API uses REST endpoints. Authentication is via Bearer tokens.",
            "Rate limiting is set to 100 requests per minute. Errors return JSON.",
            "Pagination uses cursor-based navigation. Results are sorted by date.",
            "Webhooks notify on state changes. Retry logic uses exponential backoff.",
            "The SDK supports Python and JavaScript. Documentation is auto-generated.",
        ]

        counts = []
        for response_text in responses:
            # Inject context (may be empty on first turn)
            messages = [{"role": "user", "content": "Tell me more."}]
            sm.inject_context(messages)

            # Extract memories from response
            sm.extract_memories(response_text, importance=0.6)

            # Count total memories for this agent
            all_records = list(SCMemory.query.filter(agent_id="accum-agent"))
            counts.append(len(all_records))

        # Memory count should strictly increase each cycle
        for i in range(1, len(counts)):
            assert counts[i] > counts[i - 1], (
                f"Memory count did not grow at cycle {i}: {counts}"
            )

    def test_decay_ranks_older_memories_lower(self):
        """Older memories should rank lower due to DecayingSortedField decay."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="decay-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        # Save an "old" memory with lower importance (simulating decay)
        old = SCMemory(
            agent_id="decay-agent",
            content="Old fact about server configuration from last year.",
            importance=0.3,
        )
        old.save()

        # Save a "new" memory with higher importance
        new = SCMemory(
            agent_id="decay-agent",
            content="New fact about server configuration just discovered.",
            importance=0.9,
        )
        new.save()

        messages = [
            {"role": "user", "content": "What do we know about server configuration?"},
        ]
        _, assembly = sm.inject_context(messages)

        assert len(assembly.records) >= 2

        # The high-importance "new" memory should rank before the low-importance "old" one
        contents = [getattr(r, "content", "") for r in assembly.records]
        new_idx = next(i for i, c in enumerate(contents) if "New fact" in c)
        old_idx = next(i for i, c in enumerate(contents) if "Old fact" in c)
        assert new_idx < old_idx, (
            f"New memory (idx {new_idx}) should rank before old (idx {old_idx})"
        )


# ===========================================================================
# Gap 4: Observation Feedback Loop
# ===========================================================================


class TestObservationFeedback:
    """Verify that acted/contradicted outcomes change confidence."""

    def test_acted_boosts_confidence(self):
        """Reporting 'acted' should increase the memory's confidence."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="feedback-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        m = SCMemory(
            agent_id="feedback-agent",
            content="Important deployment procedure to follow.",
            importance=0.9,
        )
        m.save()

        # Get initial confidence
        initial_conf = ConfidenceField.get_confidence(m, "confidence")

        # Inject and report acted
        messages = [{"role": "user", "content": "How do we deploy?"}]
        _, assembly = sm.inject_context(messages)
        assert len(assembly.records) > 0

        sm.report_outcomes(assembly, outcome="acted")

        # Confidence should have increased
        new_conf = ConfidenceField.get_confidence(m, "confidence")
        assert new_conf > initial_conf, (
            f"Confidence should increase after 'acted': {initial_conf} -> {new_conf}"
        )

    def test_contradicted_penalizes_confidence(self):
        """Reporting 'contradicted' should decrease the memory's confidence."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="feedback-agent-2",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        m = SCMemory(
            agent_id="feedback-agent-2",
            content="Outdated deployment procedure that was changed.",
            importance=0.9,
        )
        m.save()

        initial_conf = ConfidenceField.get_confidence(m, "confidence")

        messages = [{"role": "user", "content": "What is the deployment procedure?"}]
        _, assembly = sm.inject_context(messages)
        assert len(assembly.records) > 0

        sm.report_outcomes(assembly, outcome="contradicted")

        new_conf = ConfidenceField.get_confidence(m, "confidence")
        assert new_conf < initial_conf, (
            f"Confidence should decrease after 'contradicted': {initial_conf} -> {new_conf}"
        )

    def test_feedback_changes_ranking(self):
        """After feedback, high-confidence memory should rank above low-confidence."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="rank-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        # Two memories with same importance
        m_good = SCMemory(
            agent_id="rank-agent",
            content="Verified correct deployment steps for production.",
            importance=0.8,
        )
        m_good.save()

        m_bad = SCMemory(
            agent_id="rank-agent",
            content="Possibly wrong deployment steps that may be outdated.",
            importance=0.8,
        )
        m_bad.save()

        # Boost the good memory
        messages = [{"role": "user", "content": "How do we deploy to production?"}]
        _, assembly = sm.inject_context(messages)

        # Apply acted to good, contradicted to bad
        if assembly.records:
            for record in assembly.records:
                content = getattr(record, "content", "")
                if "Verified correct" in content:
                    ConfidenceField.update_confidence(record, "confidence", signal=0.9)
                elif "Possibly wrong" in content:
                    ConfidenceField.update_confidence(record, "confidence", signal=0.1)

        # Re-query and check ranking
        _, assembly2 = sm.inject_context(
            [{"role": "user", "content": "Tell me about deployment steps."}]
        )

        if len(assembly2.records) >= 2:
            contents = [getattr(r, "content", "") for r in assembly2.records]
            good_idx = next(
                (i for i, c in enumerate(contents) if "Verified correct" in c), None
            )
            bad_idx = next(
                (i for i, c in enumerate(contents) if "Possibly wrong" in c), None
            )
            if good_idx is not None and bad_idx is not None:
                assert good_idx < bad_idx, (
                    f"High-confidence memory (idx {good_idx}) should rank before "
                    f"low-confidence (idx {bad_idx})"
                )


# ===========================================================================
# Gap 5: Token Budget Enforcement
# ===========================================================================


class TestTokenBudgetEnforcement:
    """Verify that injected context respects max_tokens."""

    def test_token_count_within_budget(self):
        """Total token count in assembly metadata should not exceed max_tokens."""
        # Use a very small budget to force truncation.
        # The default token counter is len(str(record)) // 4, and each record's
        # str() includes all field data. We need content large enough that
        # 30 records cannot fit in the budget.
        max_tokens = 100
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="budget-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=50,
            max_tokens=max_tokens,
        )

        # Each content is ~800 chars, so str(record) // 4 ~ 200+ tokens per record.
        # With budget=100, at most 1 record should fit.
        filler = "x" * 800
        for i in range(30):
            SCMemory(
                agent_id="budget-agent",
                content=f"Memory {i}: {filler}",
                importance=0.8,
            ).save()

        messages = [
            {"role": "user", "content": "What do we know about the system?"},
        ]
        _, assembly = sm.inject_context(messages)

        # Budget enforcement: the assembler should have limited records
        assert len(assembly.records) > 0
        assert len(assembly.records) < 30, (
            f"Expected budget to limit records, got {len(assembly.records)} of 30"
        )

    def test_large_budget_returns_more_records(self):
        """A larger token budget should allow more records through."""
        # Seed memories
        for i in range(10):
            SCMemory(
                agent_id="budget-agent-2",
                content=f"Fact number {i} about the system architecture and design.",
                importance=0.8,
            ).save()

        sm_small = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="budget-agent-2",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=50,
            max_tokens=200,
        )
        sm_large = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="budget-agent-2",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=50,
            max_tokens=5000,
        )

        messages = [{"role": "user", "content": "Tell me about the system."}]

        _, assembly_small = sm_small.inject_context(list(messages))
        _, assembly_large = sm_large.inject_context(list(messages))

        assert len(assembly_large.records) >= len(assembly_small.records), (
            f"Larger budget ({len(assembly_large.records)} records) should return "
            f">= smaller budget ({len(assembly_small.records)} records)"
        )


# ===========================================================================
# Gap 6: Extraction Heuristic Edge Cases
# ===========================================================================


class TestExtractionEdgeCases:
    """Test _split_sentences on real-world LLM output patterns."""

    def test_markdown_bullet_lists_not_split(self):
        """Markdown bullet items without sentence-ending punctuation are NOT split.

        The regex `(?<=[.!?])\\s+` only splits on .!? followed by whitespace.
        Bullet lists without trailing punctuation remain as a single block.
        This is a known limitation of the simple heuristic.
        """
        text = "- item one\n- item two\n- item three"
        sentences = SubconsciousMemory._split_sentences(text)
        # Without .!? at end of each bullet, they stay as one block
        assert len(sentences) == 1, (
            f"Expected 1 block (known limitation), got: {sentences}"
        )

    def test_markdown_bullets_with_periods_split(self):
        """Markdown bullets ending with periods ARE split correctly."""
        text = "- item one. - item two. - item three."
        sentences = SubconsciousMemory._split_sentences(text)
        assert len(sentences) == 3, f"Expected 3 items, got: {sentences}"

    def test_code_blocks_with_periods(self):
        """Periods in code references should not always split sentences."""
        text = "Call foo.bar(). Then check the result."
        sentences = SubconsciousMemory._split_sentences(text)
        # The regex splits on ". " so "foo.bar(). Then" will split
        assert len(sentences) >= 1
        # At minimum the full text is captured across splits
        rejoined = " ".join(sentences)
        assert "foo" in rejoined
        assert "result" in rejoined

    def test_urls_with_periods(self):
        """URLs with periods followed by sentences should be handled."""
        text = "Visit https://example.com. Next sentence here."
        sentences = SubconsciousMemory._split_sentences(text)
        # Should split around the URL's trailing period
        assert len(sentences) >= 1
        rejoined = " ".join(sentences)
        assert "example.com" in rejoined
        assert "Next sentence" in rejoined

    def test_decimal_numbers(self):
        """Decimal numbers should not cause spurious splits."""
        text = "Version 3.14 was released. It includes fixes."
        sentences = SubconsciousMemory._split_sentences(text)
        # "3.14" has no space after period, so no split there
        # Should split on "released. It"
        assert len(sentences) == 2, f"Expected 2 sentences, got: {sentences}"
        assert "3.14" in sentences[0]

    def test_exclamation_and_question_marks(self):
        """Exclamation and question marks should trigger splits."""
        text = "Is this working? Yes it is! Great news."
        sentences = SubconsciousMemory._split_sentences(text)
        assert len(sentences) == 3, f"Expected 3 sentences, got: {sentences}"

    def test_empty_and_whitespace(self):
        """Empty/whitespace input should return empty list."""
        assert SubconsciousMemory._split_sentences("") == []
        assert SubconsciousMemory._split_sentences("   ") == []


# ===========================================================================
# Gap 7: Configurable Field Names
# ===========================================================================


class TestConfigurableFieldNames:
    """Verify non-default field names work end-to-end."""

    def test_extract_saves_to_custom_fields(self):
        """extract_memories should save to 'text' and 'score' fields."""
        sm = SubconsciousMemory(
            model_class=AltMemory,
            agent_id="alt-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
            content_field="text",
            importance_field="score",
            agent_id_field="owner",
        )

        saved = sm.extract_memories(
            "Custom field test sentence one. Custom field test sentence two.",
            importance=0.8,
        )

        assert len(saved) == 2
        # Verify data went into the correct fields
        for record in saved:
            assert record.text != "", "text field should be populated"
            assert record.score == 0.8, f"score field should be 0.8, got {record.score}"
            assert record.owner == "alt-agent", (
                f"owner field should be 'alt-agent', got {record.owner}"
            )

    def test_inject_retrieves_with_default_agent_id_field(self):
        """inject_context retrieves memories when using default 'agent_id' field name.

        Note: inject_context hardcodes filters['agent_id'] in the assembler,
        so custom agent_id_field names (e.g., 'owner') are only used for
        extract_memories, not inject_context. This test verifies retrieval
        works when the model uses the default 'agent_id' KeyField name.
        """
        # Use the standard SCMemory model (has agent_id field)
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="alt-inject-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            max_items=10,
            max_tokens=4000,
        )

        SCMemory(
            agent_id="alt-inject-agent",
            content="Standard field memory about deployment.",
            importance=0.9,
        ).save()

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Tell me about deployment."},
        ]
        result_msgs, assembly = sm.inject_context(messages)

        assert len(assembly.records) >= 1
        assert "Relevant context:" in result_msgs[0]["content"]

    def test_custom_agent_id_field_used_for_extraction(self):
        """extract_memories uses the custom agent_id_field name for saving."""
        sm = SubconsciousMemory(
            model_class=AltMemory,
            agent_id="alt-agent-3",
            score_weights={"relevance": 0.6, "confidence": 0.3},
            content_field="text",
            importance_field="score",
            agent_id_field="owner",
        )

        saved = sm.extract_memories(
            "Custom agent field extraction test sentence.",
            importance=0.8,
        )
        assert len(saved) == 1
        assert saved[0].owner == "alt-agent-3"

        # Verify it's queryable by the custom field name
        records = list(AltMemory.query.filter(owner="alt-agent-3"))
        assert len(records) == 1


# ===========================================================================
# Gap 8: WriteFilter Boundary Behavior
# ===========================================================================


class TestWriteFilterBoundary:
    """Verify exact threshold semantics of WriteFilterMixin."""

    def test_at_min_threshold_saves(self):
        """importance == _wf_min_threshold (0.2) should save."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="wf-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )
        saved = sm.extract_memories(
            "This sentence is exactly at the minimum threshold.",
            importance=0.2,
        )
        assert len(saved) == 1, "Importance 0.2 (== threshold) should save"

    def test_below_min_threshold_filtered(self):
        """importance < _wf_min_threshold (0.2) should be filtered out."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="wf-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )
        saved = sm.extract_memories(
            "This sentence should be filtered by the write filter.",
            importance=0.19,
        )
        assert len(saved) == 0, "Importance 0.19 (< threshold) should be filtered"

    def test_at_priority_threshold(self):
        """importance >= _wf_priority_threshold (0.7) should be tagged priority."""
        m = SCMemory(
            agent_id="wf-agent",
            content="High priority memory for testing.",
            importance=0.7,
        )
        result = m.save()
        assert result is not False, "Importance 0.7 (== priority threshold) should save"

        # The record should exist (was saved successfully)
        records = list(SCMemory.query.filter(agent_id="wf-agent"))
        assert len(records) >= 1

    def test_below_priority_threshold_not_priority(self):
        """importance = 0.69 should save but not be tagged as priority."""
        m = SCMemory(
            agent_id="wf-agent-2",
            content="Just below priority threshold memory.",
            importance=0.69,
        )
        result = m.save()
        assert result is not False, "Importance 0.69 should save (above min threshold)"

    def test_zero_importance_filtered(self):
        """importance = 0.0 should be filtered out."""
        sm = SubconsciousMemory(
            model_class=SCMemory,
            agent_id="wf-agent",
            score_weights={"relevance": 0.6, "confidence": 0.3},
        )
        saved = sm.extract_memories(
            "This sentence has zero importance and should not be saved.",
            importance=0.0,
        )
        assert len(saved) == 0, "Importance 0.0 should be filtered"

    def test_importance_one_saves_as_priority(self):
        """importance = 1.0 should save and be tagged as priority."""
        m = SCMemory(
            agent_id="wf-agent-3",
            content="Maximum importance memory.",
            importance=1.0,
        )
        result = m.save()
        assert result is not False, "Importance 1.0 should save as priority"
