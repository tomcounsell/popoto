"""Tests for external benchmark harness components.

All tests are fixture-based and require no network access.
Tests cover:
- recall_at_k metric function (edge cases)
- LongMemEval-S adapter schema validation
- LoCoMo adapter schema validation
"""

import json
import logging

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
        """Each sample in the fixture yields one item per QA pair."""
        # Fixture: conv-26 has 4 QA (3 normal + 1 adversarial), conv-31 has 3 QA
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        assert len(items) == 7

    def test_history_shared_across_qa(self):
        """All QA items from the same sample share the same history."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        # Items 0-3 are from conv-26 (4 QA), items 4-6 from conv-31 (3 QA).
        conv26 = [it for it in items if it.metadata["sample_id"] == "conv-26"]
        conv31 = [it for it in items if it.metadata["sample_id"] == "conv-31"]
        assert len(conv26) == 4
        assert len(conv31) == 3
        # Histories are shared (identical) within a sample.
        assert all(it.history == conv26[0].history for it in conv26)
        assert all(it.history == conv31[0].history for it in conv31)
        # Distinct samples have distinct histories.
        assert conv26[0].history != conv31[0].history

    def test_blip_caption_turns_ingested(self):
        """Image turns are ingested via blip_caption, not skipped.

        A turn is only dropped when it has neither ``text`` nor
        ``blip_caption``; every ingested turn has non-empty content.
        """
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        for item in items:
            for turn in item.history:
                assert turn[
                    "content"
                ].strip(), f"Turn content should not be empty: {turn}"
        # The image turn (D1:3 in conv-26) is present via its caption.
        conv26 = next(it for it in items if it.metadata["sample_id"] == "conv-26")
        image_turns = [t for t in conv26.history if t["turn_id"] == "D1:3"]
        assert len(image_turns) == 1
        assert "snow-capped mountain peak" in image_turns[0]["content"]

    def test_dia_id_intersection(self):
        """Non-adversarial evidence dia_ids must be reachable in history.

        This is the core proof of the fix: relevant_ids (built from
        ``qa[].evidence`` dia_ids) intersect the history turn_ids (also
        dia_ids), so the scorer can actually score a hit.
        """
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        non_adversarial = [it for it in items if not it.metadata["adversarial"]]
        assert non_adversarial, "fixture must contain non-adversarial items"
        for item in non_adversarial:
            history_ids = {row["turn_id"] for row in item.history}
            assert item.relevant_ids, f"{item.item_id} should have evidence"
            assert item.relevant_ids <= history_ids, (
                f"{item.item_id} evidence {item.relevant_ids} not reachable "
                f"in history {history_ids}"
            )

    def test_adversarial_item_empty_evidence(self):
        """Adversarial QA yields an item with empty evidence and adv answer."""
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        adversarial = [it for it in items if it.metadata["adversarial"]]
        assert len(adversarial) == 1
        adv = adversarial[0]
        assert adv.relevant_ids == set()
        assert adv.metadata["answer"] == "Not mentioned in the conversation."

    def test_malformed_sample_skipped_with_warning(self, tmp_path, caplog):
        """A sample missing a required field is skipped (with a warning);
        valid samples are NOT skipped."""
        malformed = [
            {
                "sample_id": "bad",
                # missing 'conversation'
                "qa": [{"question": "?", "answer": "x", "evidence": ["D1:1"]}],
            },
            {
                "sample_id": "good",
                "conversation": {
                    "speaker_a": "A",
                    "speaker_b": "B",
                    "session_1_date_time": "now",
                    "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "hello"}],
                },
                "qa": [{"question": "?", "answer": "hello", "evidence": ["D1:1"]}],
            },
        ]
        fixture = tmp_path / "malformed_locomo.json"
        fixture.write_text(json.dumps(malformed))

        with caplog.at_level(logging.WARNING):
            items = list(iter_locomo(fixture_path=fixture))

        # Only the valid sample's QA is yielded; the malformed one is skipped.
        assert len(items) == 1
        assert items[0].metadata["sample_id"] == "good"
        assert any("Skipping malformed" in rec.message for rec in caplog.records)

    def test_mrr_le_recall_over_fixture_items(self):
        """MRR <= any-hit Recall@k for retrievals that hit fixture evidence.

        Constructs a retrieved list (the item's own history turn_ids) that
        hits the evidence and asserts the shared-metric invariant holds.
        """
        items = list(iter_locomo(fixture_path=LOCOMO_FIXTURE))
        non_adversarial = [it for it in items if not it.metadata["adversarial"]]
        assert non_adversarial
        for item in non_adversarial:
            retrieved = [row["turn_id"] for row in item.history]
            mrr = mean_reciprocal_rank(retrieved, item.relevant_ids)
            any_hit = recall_at_k(retrieved, item.relevant_ids, len(retrieved))
            assert mrr <= any_hit + 1e-9
            # Evidence is reachable, so a full-list retrieval always hits.
            assert any_hit == 1.0
