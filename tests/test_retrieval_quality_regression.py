"""P@10 regression test for BM25 first-class lexical retrieval mode (issue #409).

Validates that the 'lexical' retrieval mode (BM25-only, no embeddings) achieves
a mean Precision@10 >= 0.5 over the 200-record / 20-query static fixture, and
that different queries return distinct result sets (no query-blindness).

Also covers:
- Cold-index backfill-recovery: BM25 degrades to composite when index is empty,
  then restores query-dependent results after re-save.
- end-to-end: a BM25-only model with no explicit retrieval_mode constructor
  arg resolves to _effective_mode == 'lexical' via 'auto' detection.

Fixture: tests/fixtures/retr1_corpus.json
  - 200 records, 10 topics, 20 records per topic
  - 20 queries (2 per topic), each with 20 relevant record IDs
  - relevant_ids are record['id'] values (integers 0-199), NOT array indices
"""

import json
import logging
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.fields.bm25_field import BM25Field  # noqa: E402
from src.popoto.models.base import Model  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.recipes.context_assembler import ContextAssembler  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture path and loader
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retr1_corpus.json"


def load_fixture():
    """Load and validate the static retrieval-quality corpus fixture."""
    data = json.loads(FIXTURE_PATH.read_text())
    assert (
        len(data["records"]) == 200
    ), f"expected 200 records, got {len(data['records'])}"
    assert (
        len(data["queries"]) == 20
    ), f"expected 20 queries, got {len(data['queries'])}"
    assert all(
        len(q["relevant_ids"]) == 20 for q in data["queries"]
    ), "every query must have exactly 20 relevant_ids"
    return data


# ---------------------------------------------------------------------------
# BM25-only model for regression (no EmbeddingField -> auto resolves to lexical)
# ---------------------------------------------------------------------------


class RegressionMemory(Model):
    """BM25-only model — no EmbeddingField so auto mode resolves to lexical."""

    content = Field()
    content_bm25 = BM25Field(source="content")
    topic = Field()
    record_id = Field()  # Stores the original fixture record id as string


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flush():
    POPOTO_REDIS_DB.flushdb()


def _save_fixture_records(data):
    """Save all 200 fixture records into RegressionMemory. Returns {redis_key: record_id}."""
    key_to_id = {}
    for rec in data["records"]:
        m = RegressionMemory(
            content=rec["content"],
            topic=rec["topic"],
            record_id=str(rec["id"]),
        )
        m.save()
        key_to_id[m._redis_key] = rec["id"]
    return key_to_id


def _compute_p_at_10(assembler, data):
    """Return (mean_P@10, list_of_retrieved_frozensets) over all 20 queries."""
    precisions = []
    retrieved_sets = []
    for q in data["queries"]:
        result = assembler.assemble(query_cues={"topic": q["query_text"]})
        relevant_set = set(q["relevant_ids"])
        retrieved_ids = set()
        for r in result.records:
            if r.record_id:
                try:
                    retrieved_ids.add(int(r.record_id))
                except (ValueError, TypeError):
                    pass
        hits = len(retrieved_ids & relevant_set)
        p10 = hits / 10  # denominator fixed at 10 regardless of retrieved count
        precisions.append(p10)
        retrieved_sets.append(frozenset(retrieved_ids))
    mean_p10 = sum(precisions) / len(precisions)
    return mean_p10, retrieved_sets


# ---------------------------------------------------------------------------
# 1. Fixture shape assertions (pre-condition)
# ---------------------------------------------------------------------------


class TestFixtureShape:
    def test_fixture_has_200_records(self):
        data = load_fixture()
        assert len(data["records"]) == 200

    def test_fixture_has_20_queries(self):
        data = load_fixture()
        assert len(data["queries"]) == 20

    def test_every_query_has_20_relevant_ids(self):
        data = load_fixture()
        for q in data["queries"]:
            assert (
                len(q["relevant_ids"]) == 20
            ), f"query {q['query_text']!r} has {len(q['relevant_ids'])} relevant_ids"

    def test_record_ids_are_integers_0_to_199(self):
        data = load_fixture()
        ids = {r["id"] for r in data["records"]}
        assert ids == set(range(200))


# ---------------------------------------------------------------------------
# 2. P@10 regression test (main assertion)
# ---------------------------------------------------------------------------


class TestP10Regression:
    """Mean P@10 >= 0.5 proves lexical BM25 retrieval is query-sensitive."""

    def test_mean_p_at_10_meets_threshold(self):
        """BM25 lexical retrieval must achieve mean P@10 >= 0.5 over 20 queries.

        Lower bound of 0.5 was chosen conservatively below the empirically
        measured value of ~0.58 to allow for minor corpus ordering variation
        while still catching a full retrieval regression (random or query-blind
        baselines would score ~0.1 on this corpus).
        """
        _flush()
        data = load_fixture()
        _save_fixture_records(data)

        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            # No max_tokens — avoid budget truncation masking retrieval failure
            retrieval_mode="lexical",
        )
        assert assembler._effective_mode == "lexical"

        mean_p10, _ = _compute_p_at_10(assembler, data)
        assert mean_p10 >= 0.5, (
            f"mean P@10 = {mean_p10:.3f} is below the 0.5 threshold. "
            "BM25 lexical retrieval may be broken or query-blind."
        )

    def test_distinct_sets_across_queries(self):
        """Different queries must return different result sets (not query-blind).

        At least 19 of 20 queries must return distinct retrieved sets.
        A query-blind retrieval (returning the same records regardless of query)
        would produce only 1 distinct set. Threshold: distinct_fraction >= 0.95.
        """
        _flush()
        data = load_fixture()
        _save_fixture_records(data)

        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            retrieval_mode="lexical",
        )

        _, retrieved_sets = _compute_p_at_10(assembler, data)
        distinct_count = len(set(retrieved_sets))
        distinct_fraction = distinct_count / len(retrieved_sets)
        assert distinct_fraction >= 0.95, (
            f"Only {distinct_count}/20 distinct result sets (fraction={distinct_fraction:.2f}). "
            "BM25 retrieval appears query-blind — different queries return the same results."
        )

    def test_auto_mode_resolves_to_lexical_on_bm25_only_model(self):
        """'auto' mode must resolve to 'lexical' for a BM25-only model.

        This proves the auto-detection plumbing works for the recipe's
        default no-retrieval_mode constructor path.
        """
        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            # no retrieval_mode kwarg — defaults to 'auto'
        )
        assert assembler._effective_mode == "lexical", (
            f"Expected _effective_mode='lexical' for BM25-only model under auto, "
            f"got {assembler._effective_mode!r}"
        )


# ---------------------------------------------------------------------------
# 3. Cold-index backfill-recovery test
# ---------------------------------------------------------------------------


class TestColdIndexRecovery:
    """When the BM25 index is cleared after save(), lexical degrades to composite.
    After re-saving, query-dependent BM25 results are restored.
    """

    def test_cold_index_falls_back_to_composite(self, caplog):
        """Clearing BM25 index keys triggers the lexical -> composite fallback path.

        The cold-index degradation MUST be observable: it is logged at WARNING
        level, naming the model and pointing at the reindex/backfill remedy. This
        is the only runtime signal a user gets that their copied recipe is
        silently query-blind (CONCERN C1). We verify the fallback happens (result
        is a list) and that the WARNING with the model name + reindex pointer
        appears.
        """
        _flush()
        data = load_fixture()
        _save_fixture_records(data)

        # Clear the BM25 index keys to simulate a cold-index scenario.
        # BM25 keys pattern: $BM25:{ClassName}:{field_name}:*
        bm25_keys = POPOTO_REDIS_DB.keys("$BM25:RegressionMemory:content_bm25:*")
        if bm25_keys:
            POPOTO_REDIS_DB.delete(*bm25_keys)

        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            retrieval_mode="lexical",
        )

        # Run with caplog capturing WARNING messages from the assembler logger
        with caplog.at_level(logging.WARNING, logger="POPOTO.ContextAssembler"):
            result = assembler.assemble(query_cues={"topic": "CI pipeline"})

        # Result must still be a list (graceful degradation, no crash)
        assert isinstance(
            result.records, list
        ), "Cold-index fallback must return a list, not raise"

        # The cold-index WARNING must appear, name the model, and point at the
        # reindex/backfill remedy (CONCERN C1 — observability is mandatory).
        cold_index_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "BM25 returned 0 hits" in r.getMessage()
            and "RegressionMemory" in r.getMessage()
            and "re-save" in r.getMessage()
        ]
        assert len(cold_index_warnings) > 0, (
            "Expected a WARNING naming the model and pointing at the reindex "
            "backfill remedy when BM25 returns no results, but none was logged. "
            f"All log messages: {[r.message for r in caplog.records]}"
        )

    def test_cold_index_recovery_after_resave(self):
        """After re-saving records, BM25 retrieval restores query-dependent results."""
        _flush()
        data = load_fixture()

        # Save, then clear the BM25 index
        _save_fixture_records(data)
        bm25_keys = POPOTO_REDIS_DB.keys("$BM25:RegressionMemory:content_bm25:*")
        if bm25_keys:
            POPOTO_REDIS_DB.delete(*bm25_keys)

        # Verify cold state: BM25 search returns empty
        cold_results = BM25Field.search(
            RegressionMemory, "content_bm25", "database index", limit=10
        )
        assert (
            cold_results == []
        ), f"Expected empty BM25 results after clearing index, got {cold_results}"

        # Re-save records (backfill)
        _save_fixture_records(data)

        # Verify warm state: BM25 returns relevant results
        warm_results = BM25Field.search(
            RegressionMemory, "content_bm25", "database index", limit=10
        )
        assert (
            len(warm_results) > 0
        ), "BM25 returned no results after backfill re-save. Index recovery failed."
        # Verify results are relevant (database-topic records have IDs 20-39)
        retrieved_ids = set()
        for redis_key, _ in warm_results:
            record = RegressionMemory.query.get(redis_key=redis_key)
            if record and record.record_id:
                try:
                    retrieved_ids.add(int(record.record_id))
                except (ValueError, TypeError):
                    pass
        database_ids = set(range(20, 40))
        overlap = retrieved_ids & database_ids
        assert len(overlap) > 0, (
            f"After backfill, BM25 search for 'database index' returned no database-topic "
            f"records. Retrieved IDs: {sorted(retrieved_ids)}"
        )


# ---------------------------------------------------------------------------
# 4. End-to-end SubconsciousMemory-style test
# ---------------------------------------------------------------------------


class TestEndToEndLexicalRetrieval:
    """Verify the end-to-end path: save -> ContextAssembler(auto) -> assemble()."""

    def test_end_to_end_query_dependent_retrieval(self):
        """Different query texts return different result sets via auto-lexical mode.

        This mirrors the SubconsciousMemory.inject_context() call chain:
        ContextAssembler(model, score_weights, auto) -> assemble(query_cues).
        """
        _flush()
        data = load_fixture()
        _save_fixture_records(data)

        # Use 'auto' mode (no retrieval_mode kwarg) — resolves to 'lexical'
        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
        )
        assert assembler._effective_mode == "lexical"

        # Run two topically distinct queries and verify they return different sets
        result_database = assembler.assemble(query_cues={"topic": "index postgres"})
        result_testing = assembler.assemble(query_cues={"topic": "unit pytest"})

        ids_database = frozenset(
            int(r.record_id) for r in result_database.records if r.record_id
        )
        ids_testing = frozenset(
            int(r.record_id) for r in result_testing.records if r.record_id
        )

        # Both must return results
        assert len(ids_database) > 0, "database query returned no results"
        assert len(ids_testing) > 0, "testing query returned no results"

        # The sets must not be identical (proves query-sensitivity)
        assert ids_database != ids_testing, (
            "database and testing queries returned identical result sets — "
            "retrieval is query-blind"
        )

        # Results should be topically relevant
        database_ids = set(range(20, 40))
        testing_ids = set(range(80, 100))
        assert (
            len(ids_database & database_ids) > 0
        ), f"database query missed all database-topic records. Got: {sorted(ids_database)}"
        assert (
            len(ids_testing & testing_ids) > 0
        ), f"testing query missed all testing-topic records. Got: {sorted(ids_testing)}"

    def test_empty_db_returns_empty_list(self):
        """With empty DB, lexical mode assemble() returns empty list (no crash)."""
        _flush()

        assembler = ContextAssembler(
            model_class=RegressionMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            retrieval_mode="lexical",
        )
        result = assembler.assemble(query_cues={"topic": "deployment CI pipeline"})
        assert isinstance(result.records, list)
        # May be empty or non-empty; must not raise
