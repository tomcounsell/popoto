"""Tests for external benchmark harness components.

All tests are fixture-based and require no network access.
Tests cover:
- recall_at_k metric function (edge cases)
- LongMemEval-S adapter schema validation
- LoCoMo adapter schema validation
"""

import pytest
from pathlib import Path

from tests.benchmarks.datasets import BenchmarkItem
from tests.benchmarks.datasets.longmemeval_s import iter_items as iter_longmemeval
from tests.benchmarks.datasets.locomo import iter_items as iter_locomo
from tests.benchmarks.metrics.retrieval import (
    fractional_recall_at_k,
    mean_reciprocal_rank,
    recall_at_k,
)

FIXTURES_DIR = Path(__file__).parent / "datasets" / "fixtures"
LME_FIXTURE = FIXTURES_DIR / "longmemeval_s_sample.json"
LOCOMO_FIXTURE = FIXTURES_DIR / "locomo_sample.json"


# ---------------------------------------------------------------------------
# recall_at_k tests
# ---------------------------------------------------------------------------


class TestRecallAtK:
    """Unit tests for recall_at_k metric."""

    def test_hit_in_top1(self):
        """Item is first result — Recall@1 = 1.0."""
        assert recall_at_k(["a", "b", "c"], {"a"}, k=1) == 1.0

    def test_miss_in_top1(self):
        """Relevant item not in top-1 — Recall@1 = 0.0."""
        assert recall_at_k(["b", "c", "a"], {"a"}, k=1) == 0.0

    def test_hit_in_top5(self):
        """Relevant item within top-5 — Recall@5 = 1.0."""
        retrieved = ["x", "y", "z", "w", "a", "b"]
        assert recall_at_k(retrieved, {"a"}, k=5) == 1.0

    def test_miss_all_k(self):
        """Relevant item not in any top-k — 0.0."""
        assert recall_at_k(["a", "b", "c"], {"z"}, k=10) == 0.0

    def test_empty_retrieved(self):
        """Empty retrieved list — 0.0."""
        assert recall_at_k([], {"a"}, k=5) == 0.0

    def test_empty_relevant(self):
        """Empty relevant set — 0.0."""
        assert recall_at_k(["a", "b"], set(), k=5) == 0.0

    def test_k_zero(self):
        """k=0 — 0.0."""
        assert recall_at_k(["a", "b"], {"a"}, k=0) == 0.0

    def test_k_negative(self):
        """Negative k — 0.0."""
        assert recall_at_k(["a", "b"], {"a"}, k=-1) == 0.0

    def test_multiple_relevant_all_found(self):
        """Multiple relevant items, all in top-k — any-hit 1.0."""
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=5) == 1.0

    def test_multiple_relevant_partial_is_any_hit(self):
        """Multiple relevant items, only one in top-k — any-hit is 1.0.

        Regression guard for issue #433: under the corrected any-hit
        definition, finding ANY relevant item scores 1.0 (not the fractional
        0.5). Fractional behavior now lives in fractional_recall_at_k.
        """
        assert recall_at_k(["a", "x", "y"], {"a", "b"}, k=3) == 1.0

    def test_single_item_boundary(self):
        """Exactly k items in retrieved, relevant is the last one."""
        assert recall_at_k(["x", "y", "z", "a"], {"a"}, k=4) == 1.0
        assert recall_at_k(["x", "y", "z", "a"], {"a"}, k=3) == 0.0

    def test_returns_only_zero_or_one(self):
        """Any-hit recall is binary: never a fraction (issue #433)."""
        assert recall_at_k(["a", "b", "c"], {"a"}, k=10) == 1.0
        assert recall_at_k(["x", "y", "z"], {"a"}, k=10) == 0.0
        assert recall_at_k(["a", "x"], {"a", "b", "c"}, k=5) == 1.0

    def test_multi_evidence_rank1_hit_is_one(self):
        """Multi-evidence ground truth, rank-1 hit — recall@1 == 1.0.

        The core of issue #433: a top-1 hit on a question with multiple
        evidence sessions must score 1.0, not 1/|relevant|.
        """
        assert recall_at_k(["a", "x", "y"], {"a", "b", "c"}, k=1) == 1.0

    def test_mrr_le_recall_invariant(self):
        """MRR <= any-hit recall over the full list must always hold (#433).

        MRR is bounded above by any-hit recall: a first-relevant item at rank
        r contributes 1/r <= 1 to MRR but a full 1.0 to any-hit recall once k
        reaches r. The universal per-query bound is therefore against recall
        evaluated over the entire ranked list. (For a fixed small k whose
        window ends before the first hit, R@k can legitimately be 0 while
        MRR > 0 — that is not a violation.) This invariant is the guard whose
        aggregate failure (MRR 0.899 > R@5 0.888) exposed the original bug.
        """
        cases = [
            (["a", "b", "c"], {"a"}),
            (["x", "a", "y"], {"a", "b"}),
            (["x", "y", "z", "a"], {"a", "c"}),
            (["x", "y", "z"], {"a"}),  # miss: both 0.0
            (["a", "b"], {"a", "b"}),
        ]
        for retrieved, relevant in cases:
            mrr = mean_reciprocal_rank(retrieved, relevant)
            any_hit = recall_at_k(retrieved, relevant, len(retrieved))
            assert mrr <= any_hit + 1e-9, (
                f"MRR {mrr} > any-hit recall {any_hit} " f"for {retrieved} / {relevant}"
            )
            # When the first hit is within the window, R@k == 1.0 >= MRR.
            for k in (1, 5, 10):
                if recall_at_k(retrieved, relevant, k) == 1.0:
                    assert mrr <= 1.0


class TestFractionalRecallAtK:
    """Unit tests for the retained fractional_recall_at_k helper (issue #433)."""

    def test_partial_is_fraction(self):
        """One of two relevant items in top-k — fractional 0.5."""
        assert fractional_recall_at_k(
            ["a", "x", "y"], {"a", "b"}, k=3
        ) == pytest.approx(0.5)

    def test_all_found_is_one(self):
        """All relevant items found — 1.0."""
        assert fractional_recall_at_k(["a", "b", "c"], {"a", "b"}, k=5) == 1.0

    def test_rank1_hit_multi_evidence_is_fraction(self):
        """Rank-1 hit on 3-evidence question — fractional 1/3 (contrast any-hit)."""
        assert fractional_recall_at_k(
            ["a", "x", "y"], {"a", "b", "c"}, k=1
        ) == pytest.approx(1 / 3)

    def test_edge_cases_zero(self):
        """Empty / non-positive-k edge cases — 0.0."""
        assert fractional_recall_at_k([], {"a"}, k=5) == 0.0
        assert fractional_recall_at_k(["a"], set(), k=5) == 0.0
        assert fractional_recall_at_k(["a"], {"a"}, k=0) == 0.0


# ---------------------------------------------------------------------------
# LongMemEval-S adapter tests
# ---------------------------------------------------------------------------


class TestLongMemEvalAdapter:
    """Schema validation tests for the LongMemEval-S adapter."""

    def test_yields_benchmark_items(self):
        """iter_items should yield BenchmarkItem namedtuples."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        assert len(items) > 0
        for item in items:
            assert isinstance(item, BenchmarkItem)

    def test_item_id_present(self):
        """Each item should have a non-empty item_id."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        for item in items:
            assert item.item_id, f"item_id should be non-empty, got {item.item_id!r}"

    def test_query_is_string(self):
        """Query field should be a non-empty string."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        for item in items:
            assert isinstance(item.query, str)
            assert item.query.strip(), f"query should be non-empty for {item.item_id}"

    def test_history_is_list_of_dicts(self):
        """History should be a list of dicts with role, content, turn_id."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        for item in items:
            assert isinstance(item.history, list)
            assert len(item.history) > 0
            for turn in item.history:
                assert isinstance(turn, dict)
                assert "role" in turn, f"turn missing 'role': {turn}"
                assert "content" in turn, f"turn missing 'content': {turn}"
                assert "turn_id" in turn, f"turn missing 'turn_id': {turn}"

    def test_relevant_ids_is_set(self):
        """relevant_ids should be a set of strings."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        for item in items:
            assert isinstance(item.relevant_ids, set)
            for rid in item.relevant_ids:
                assert isinstance(rid, str), f"relevant_id should be str: {rid!r}"

    def test_metadata_has_dataset_key(self):
        """Metadata should include 'dataset' key."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        for item in items:
            assert "dataset" in item.metadata
            assert item.metadata["dataset"] == "longmemeval-s"

    def test_limit_respected(self):
        """The limit argument should cap the number of items yielded."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE, limit=2))
        assert len(items) <= 2

    def test_all_fixture_items_loaded(self):
        """All 3 fixture items should be loaded without errors."""
        items = list(iter_longmemeval(fixture_path=LME_FIXTURE))
        assert len(items) == 3


# ---------------------------------------------------------------------------
# LoCoMo adapter tests
# ---------------------------------------------------------------------------


class TestLoCoMoAdapter:
    """Schema validation tests for the LoCoMo adapter."""

    def test_yields_benchmark_items(self):
        """iter_items should yield BenchmarkItem namedtuples."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        assert len(items) > 0
        for item in items:
            assert isinstance(item, BenchmarkItem)

    def test_item_id_present(self):
        """Each item should have a non-empty item_id."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            assert item.item_id, f"item_id should be non-empty"

    def test_query_is_string(self):
        """Query field should be a non-empty string."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            assert isinstance(item.query, str)
            assert item.query.strip()

    def test_history_is_list_of_dicts(self):
        """History should be a list of dicts with role, content, turn_id."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            assert isinstance(item.history, list)
            assert len(item.history) > 0
            for turn in item.history:
                assert isinstance(turn, dict)
                assert "role" in turn
                assert "content" in turn
                assert "turn_id" in turn

    def test_relevant_ids_is_set(self):
        """relevant_ids should be a set of strings."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            assert isinstance(item.relevant_ids, set)
            for rid in item.relevant_ids:
                assert isinstance(rid, str)

    def test_metadata_has_dataset_key(self):
        """Metadata should include 'dataset' key."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            assert "dataset" in item.metadata
            assert item.metadata["dataset"] == "locomo"

    def test_limit_respected(self):
        """The limit argument should cap the number of items yielded."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE, limit=3))
        assert len(items) <= 3

    def test_multiple_qa_per_dialogue(self):
        """Each dialogue in the fixture has multiple QA pairs."""
        # Fixture has 2 dialogues × 3 QA = 6 items
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        assert len(items) == 6

    def test_history_shared_across_qa(self):
        """All QA items from the same dialogue share the same history."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        # Items 0-2 should be from locomo_001, items 3-5 from locomo_002
        history_0 = items[0].history
        history_1 = items[1].history
        history_2 = items[2].history
        assert history_0 == history_1 == history_2

    def test_text_only_turns(self):
        """All history turns should have non-empty content (image turns skipped)."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            for turn in item.history:
                assert turn[
                    "content"
                ].strip(), f"Turn content should not be empty: {turn}"
