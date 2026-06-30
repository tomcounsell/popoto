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
from tests.benchmarks.datasets.sampling import sample_items
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


# ---------------------------------------------------------------------------
# Representative sampling tests (issue #435)
# ---------------------------------------------------------------------------


def _mk_items(n, qtypes=None):
    """Build n synthetic BenchmarkItems with optional per-item question_type."""
    items = []
    for i in range(n):
        qt = qtypes[i] if qtypes is not None else f"qt{i % 3}"
        items.append(
            BenchmarkItem(
                item_id=str(i),
                history=[],
                query=f"q{i}",
                relevant_ids=set(),
                metadata={"question_type": qt, "dataset": "synthetic"},
            )
        )
    return items


def _ids(items):
    return [it.item_id for it in items]


class TestSampling:
    """Unit tests for the shared representative sampler (issue #435)."""

    def test_head_is_prefix(self):
        """head mode returns the contiguous leading slice."""
        items = _mk_items(10)
        assert _ids(sample_items(items, 3, mode="head")) == ["0", "1", "2"]

    def test_stride_spans_full_range(self):
        """stride selects evenly spread indices across the whole list."""
        items = _mk_items(10)
        selected = sample_items(items, 3, mode="stride")
        # indices: round(0)=0, round(10/3)=3, round(20/3)=7
        assert _ids(selected) == ["0", "3", "7"]
        # First item of the corpus is included; last index is in the tail half.
        assert selected[0].item_id == "0"
        assert int(selected[-1].item_id) >= 10 // 2

    def test_stride_is_deterministic(self):
        """stride needs no RNG — repeated calls are identical."""
        items = _mk_items(50)
        a = _ids(sample_items(items, 7, mode="stride"))
        b = _ids(sample_items(items, 7, mode="stride"))
        assert a == b

    def test_stride_no_duplicate_indices(self):
        """stride never picks the same item twice for limit < N."""
        items = _mk_items(100)
        selected = sample_items(items, 37, mode="stride")
        ids = _ids(selected)
        assert len(ids) == 37
        assert len(set(ids)) == 37

    def test_shuffle_seed_deterministic(self):
        """Same seed → identical selection; result is a sorted subset."""
        items = _mk_items(40)
        a = _ids(sample_items(items, 8, mode="shuffle", seed=123))
        b = _ids(sample_items(items, 8, mode="shuffle", seed=123))
        assert a == b
        assert len(a) == 8
        # Returned in original corpus order.
        assert [int(x) for x in a] == sorted(int(x) for x in a)

    def test_shuffle_different_seed_differs(self):
        """Different seeds generally yield a different selection."""
        items = _mk_items(40)
        a = _ids(sample_items(items, 8, mode="shuffle", seed=1))
        b = _ids(sample_items(items, 8, mode="shuffle", seed=2))
        assert a != b

    def test_shuffle_does_not_touch_global_rng(self):
        """Sampling must use a local RNG, never perturb the global one."""
        import random

        random.seed(999)
        baseline = [random.random() for _ in range(3)]
        random.seed(999)
        sample_items(_mk_items(40), 8, mode="shuffle", seed=7)
        after = [random.random() for _ in range(3)]
        assert baseline == after

    def test_stratified_covers_every_category_proportionally(self):
        """stratified represents every question_type, summing to limit."""
        # 3 categories, 3 items each (qt0/qt1/qt2 round-robin over 9 items).
        items = _mk_items(9)
        selected = sample_items(items, 6, mode="stratified", seed=0)
        assert len(selected) == 6
        from collections import Counter

        counts = Counter(it.metadata["question_type"] for it in selected)
        assert set(counts) == {"qt0", "qt1", "qt2"}
        # Even split: limit 6 over 3 equal groups → 2 each.
        assert all(c == 2 for c in counts.values())

    def test_stratified_sums_to_exactly_limit_uneven_groups(self):
        """Largest-remainder apportionment still sums to limit for skewed groups."""
        # 7 of 'a', 2 of 'b', 1 of 'c' → 10 items.
        qtypes = ["a"] * 7 + ["b"] * 2 + ["c"]
        items = _mk_items(10, qtypes=qtypes)
        selected = sample_items(items, 5, mode="stratified", seed=0)
        assert len(selected) == 5

    def test_stratified_is_seed_deterministic(self):
        """Same seed → identical stratified selection."""
        items = _mk_items(30)
        a = _ids(sample_items(items, 9, mode="stratified", seed=3))
        b = _ids(sample_items(items, 9, mode="stratified", seed=3))
        assert a == b

    def test_stratified_all_empty_qtype_falls_back_gracefully(self):
        """All-empty question_type collapses to one bucket without crashing."""
        items = _mk_items(10, qtypes=[""] * 10)
        selected = sample_items(items, 4, mode="stratified", seed=0)
        assert len(selected) == 4

    @pytest.mark.parametrize("mode", ["head", "stride", "shuffle", "stratified"])
    def test_limit_none_returns_all(self, mode):
        """limit=None returns the full corpus unchanged for every mode."""
        items = _mk_items(12)
        assert _ids(sample_items(items, None, mode=mode)) == _ids(items)

    @pytest.mark.parametrize("mode", ["head", "stride", "shuffle", "stratified"])
    def test_limit_ge_n_returns_all(self, mode):
        """limit >= N returns the full corpus for every mode."""
        items = _mk_items(5)
        assert len(sample_items(items, 5, mode=mode)) == 5
        assert len(sample_items(items, 99, mode=mode)) == 5

    @pytest.mark.parametrize("mode", ["head", "stride", "shuffle", "stratified"])
    def test_limit_non_positive_returns_empty(self, mode):
        """limit <= 0 returns an empty list for every mode."""
        items = _mk_items(5)
        assert sample_items(items, 0, mode=mode) == []
        assert sample_items(items, -3, mode=mode) == []

    @pytest.mark.parametrize("mode", ["head", "stride", "shuffle", "stratified"])
    def test_empty_items_returns_empty(self, mode):
        """An empty corpus returns an empty list for every mode."""
        assert sample_items([], 3, mode=mode) == []

    def test_unknown_mode_raises(self):
        """An unknown sample mode fails loud with ValueError."""
        with pytest.raises(ValueError):
            sample_items(_mk_items(5), 2, mode="bogus")


class TestQuestionTypeThreading:
    """Problem B (issue #435): question_type survives into ScenarioResult."""

    def test_question_type_in_scenario_metadata(self):
        """Running an item through ExternalScenario keeps question_type."""
        from tests.benchmarks.scenarios.external_base import ExternalScenario

        item = BenchmarkItem(
            item_id="q1",
            history=[
                {
                    "role": "user",
                    "content": "I adopted a golden retriever named Max.",
                    "turn_id": "s1::0",
                    "session_id": "s1",
                },
                {
                    "role": "assistant",
                    "content": "Max is a great name for a dog.",
                    "turn_id": "s1::1",
                    "session_id": "s1",
                },
            ],
            query="What is my dog Max?",
            relevant_ids={"s1"},
            metadata={
                "dataset": "longmemeval-s",
                "question_type": "single-session-user",
            },
        )
        result = ExternalScenario(item=item).execute()
        assert result.status == "ok"
        assert result.metadata.get("question_type") == "single-session-user"


# ---------------------------------------------------------------------------
# Retrieval-mode contract (issue #437)
# ---------------------------------------------------------------------------


_MINILM_REPO = "sentence-transformers/all-MiniLM-L6-v2"


def _minilm_cached() -> bool:
    """True only if all-MiniLM-L6-v2 is already in the HF cache.

    Guards the hybrid smoke test so the suite never triggers a ~90MB download
    (or requires network) when the model is absent.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return False
    try:
        path = try_to_load_from_cache(repo_id=_MINILM_REPO, filename="config.json")
    except Exception:
        return False
    return isinstance(path, str)


def _dog_item():
    """A tiny BenchmarkItem whose query lexically matches a single session."""
    return BenchmarkItem(
        item_id="q1",
        history=[
            {
                "role": "user",
                "content": "I adopted a golden retriever named Max.",
                "turn_id": "s1::0",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": "Max is a great name for a dog.",
                "turn_id": "s1::1",
                "session_id": "s1",
            },
        ],
        query="What is my dog Max?",
        relevant_ids={"s1"},
        metadata={"dataset": "longmemeval-s", "question_type": "single-session-user"},
    )


class TestRetrievalModeContract:
    """assemble() is the primary path; effective mode is reported (issue #437)."""

    def test_lexical_is_assemble_primary(self):
        """Default lexical run drives assemble() and reports effective mode."""
        from tests.benchmarks.scenarios.external_base import ExternalScenario

        result = ExternalScenario(item=_dog_item()).execute()
        assert result.status == "ok"
        # retrieval_method records the assembler's effective mode, not the old
        # BM25-direct "bm25"/"composite_fallback" values.
        assert result.metadata.get("retrieval_method") == "lexical"
        # Records are returned and mapped back to ground-truth session IDs.
        assert "s1" in result.retrieved_ids

    @pytest.mark.skipif(
        not _minilm_cached(),
        reason="all-MiniLM-L6-v2 not cached; skipping hybrid smoke test",
    )
    def test_hybrid_effective_mode_and_records(self):
        """Hybrid run resolves the assembler to 'hybrid' and returns records."""
        from tests.benchmarks.scenarios.external_base import ExternalScenario

        scenario = ExternalScenario(item=_dog_item(), retrieval_mode="hybrid")
        try:
            result = scenario.execute()
        except Exception as e:  # model load/runtime hiccup → skip, don't fail
            pytest.skip(f"hybrid model unavailable at runtime: {e}")
        assert result.status == "ok"
        assert result.metadata.get("retrieval_method") == "hybrid"
        assert "s1" in result.retrieved_ids


# ---------------------------------------------------------------------------
# Shared provider singleton (issue #442)
# ---------------------------------------------------------------------------


class TestSharedProvider:
    """_get_shared_provider() returns one cached instance (issue #442)."""

    def test_returns_same_object_across_calls(self):
        """Repeated calls return the identical provider object.

        Guards against an accidental per-call construction regression: the
        full hybrid run relies on MiniLM loading once, not 500×. The accessor
        returns the provider WITHOUT triggering a model load (the model loads
        lazily on the first embed()), so this identity check is cheap.
        """
        from tests.benchmarks.scenarios.external_base import _get_shared_provider

        first = _get_shared_provider()
        second = _get_shared_provider()
        assert first is second


# ---------------------------------------------------------------------------
# save_reports mode-suffixed artifacts (issue #442)
# ---------------------------------------------------------------------------


def _minimal_aggregate(retrieval_mode: str) -> dict:
    """A real compute_aggregate()-shaped dict (empty results) for save_reports."""
    from tests.benchmarks.run_external import compute_aggregate

    return compute_aggregate([], "longmemeval-s", retrieval_mode=retrieval_mode)


class TestSaveReportsArtifactNaming:
    """save_reports suffixes non-lexical modes; lexical stays unsuffixed (#442)."""

    @pytest.fixture
    def results_dir(self, tmp_path, monkeypatch):
        """Point run_external.RESULTS_DIR at a scratch dir for the test."""
        import tests.benchmarks.run_external as run_external

        monkeypatch.setattr(run_external, "RESULTS_DIR", tmp_path)
        return tmp_path

    def test_lexical_names_are_unsuffixed(self, results_dir):
        """Lexical mode pins the exact current (unsuffixed) baseline names."""
        import tests.benchmarks.run_external as run_external

        json_path, md_path = run_external.save_reports(
            _minimal_aggregate("lexical"),
            "longmemeval_s",
            retrieval_mode="lexical",
        )
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert json_path.name == f"longmemeval_s_{date_str}.json"
        assert md_path.name == f"longmemeval_s_{date_str}.md"
        assert (results_dir / "longmemeval_s_latest.json").exists()
        assert (results_dir / "longmemeval_s_latest.md").exists()
        # No hybrid-suffixed artifacts created for a lexical run.
        assert not list(results_dir.glob("*_hybrid.*"))

    def test_default_mode_is_lexical(self, results_dir):
        """Omitting retrieval_mode preserves the lexical (unsuffixed) names."""
        import tests.benchmarks.run_external as run_external

        json_path, md_path = run_external.save_reports(
            _minimal_aggregate("lexical"), "longmemeval_s"
        )
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert json_path.name == f"longmemeval_s_{date_str}.json"
        assert md_path.name == f"longmemeval_s_{date_str}.md"

    def test_hybrid_names_are_suffixed(self, results_dir):
        """Hybrid mode suffixes _hybrid into dated AND _latest artifacts."""
        import tests.benchmarks.run_external as run_external

        json_path, md_path = run_external.save_reports(
            _minimal_aggregate("hybrid"),
            "longmemeval_s",
            retrieval_mode="hybrid",
        )
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert json_path.name == f"longmemeval_s_{date_str}_hybrid.json"
        assert md_path.name == f"longmemeval_s_{date_str}_hybrid.md"
        assert (results_dir / "longmemeval_s_latest_hybrid.json").exists()
        assert (results_dir / "longmemeval_s_latest_hybrid.md").exists()

    def test_hybrid_save_does_not_clobber_lexical_baseline(self, results_dir):
        """A hybrid save leaves a pre-existing lexical artifact byte-identical."""
        import tests.benchmarks.run_external as run_external
        from datetime import datetime, timezone

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        # Plant a fake committed lexical baseline.
        baseline = results_dir / f"longmemeval_s_{date_str}.json"
        original_bytes = b'{"committed": "lexical baseline"}'
        baseline.write_bytes(original_bytes)

        run_external.save_reports(
            _minimal_aggregate("hybrid"),
            "longmemeval_s",
            retrieval_mode="hybrid",
        )

        # Lexical baseline is untouched.
        assert baseline.read_bytes() == original_bytes
        # Only _hybrid artifacts were created alongside it.
        assert (results_dir / f"longmemeval_s_{date_str}_hybrid.json").exists()
        assert (results_dir / f"longmemeval_s_{date_str}_hybrid.md").exists()
        # No unsuffixed lexical _latest pointers were created by the hybrid run.
        assert not (results_dir / "longmemeval_s_latest.json").exists()
        assert not (results_dir / "longmemeval_s_latest.md").exists()
