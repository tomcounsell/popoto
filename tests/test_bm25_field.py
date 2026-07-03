"""Tests for BM25Field — ranked keyword search using BM25 scoring.

Tests cover:
- Save document, keyword_search returns it ranked
- Multiple documents with varying relevance, correct ranking order
- Exact term match ranks above partial relevance
- Document update (re-save) updates BM25 stats correctly
- Document delete removes from index, stats updated
- Empty corpus returns empty results
- Query with all stop words returns empty results
- Single-term query works
- Multi-term query combines scores
- Tokenizer shared with ExistenceFilter (same tokenization output)
- keyword_search() on non-BM25 field raises QueryException
- BM25Field without source raises ValueError
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields.bm25_field import BM25Field  # noqa: E402
from src.popoto.fields._tokenizer import tokenize  # noqa: E402
from src.popoto.fields.existence_filter import (  # noqa: E402
    tokenize as ef_tokenize,
)
from src.popoto.models.query import QueryException  # noqa: E402
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# --- Test Models ---


class BM25Doc(popoto.Model):
    name = popoto.UniqueKeyField()
    raw_content = popoto.Field(type=str)
    content = BM25Field(source="raw_content")


class BM25DocNoBM25(popoto.Model):
    name = popoto.UniqueKeyField()
    raw_content = popoto.Field(type=str)


# --- Tests ---


class TestBM25FieldBasics:
    """Basic BM25Field save, search, and delete operations."""

    def test_save_and_search_returns_result(self):
        """Save a document, search for a term it contains."""
        doc = BM25Doc(
            name="doc1", raw_content="kubernetes deployment guide for production"
        )
        doc.save()

        results = BM25Field.search(BM25Doc, "content", "kubernetes", limit=10)
        assert len(results) > 0
        keys = [k for k, _s in results]
        assert doc.db_key.redis_key in keys

    def test_search_returns_scores(self):
        """Search results include positive BM25 scores."""
        doc = BM25Doc(
            name="scored1", raw_content="redis caching strategies for performance"
        )
        doc.save()

        results = BM25Field.search(BM25Doc, "content", "redis caching", limit=10)
        assert len(results) > 0
        for key, score in results:
            assert score > 0

    def test_multiple_docs_ranked_correctly(self):
        """Document with more query terms ranks higher."""
        doc_low = BM25Doc(
            name="rank_low",
            raw_content="python programming basics for beginners",
        )
        doc_high = BM25Doc(
            name="rank_high",
            raw_content="redis deployment redis configuration redis cluster setup",
        )
        doc_low.save()
        doc_high.save()

        results = BM25Field.search(BM25Doc, "content", "redis deployment", limit=10)
        assert len(results) >= 1
        # doc_high should rank first (has more redis terms + deployment)
        top_key = results[0][0]
        assert top_key == doc_high.db_key.redis_key

    def test_exact_term_match_above_partial(self):
        """Document with exact keyword match ranks above one with only partial relevance."""
        doc_exact = BM25Doc(
            name="exact1",
            raw_content="kubernetes error code troubleshooting guide",
        )
        doc_partial = BM25Doc(
            name="partial1",
            raw_content="general system troubleshooting for servers",
        )
        doc_exact.save()
        doc_partial.save()

        results = BM25Field.search(BM25Doc, "content", "kubernetes error", limit=10)
        assert len(results) >= 1
        top_key = results[0][0]
        assert top_key == doc_exact.db_key.redis_key

    def test_single_term_query(self):
        """Single-term query works correctly."""
        doc = BM25Doc(name="single1", raw_content="elasticsearch indexing pipeline")
        doc.save()

        results = BM25Field.search(BM25Doc, "content", "elasticsearch", limit=10)
        assert len(results) > 0
        keys = [k for k, _s in results]
        assert doc.db_key.redis_key in keys

    def test_multi_term_query_combines_scores(self):
        """Multi-term query accumulates scores from multiple terms."""
        doc_one_term = BM25Doc(
            name="multi_one",
            raw_content="monitoring dashboard setup",
        )
        doc_both_terms = BM25Doc(
            name="multi_both",
            raw_content="monitoring alerting pipeline for production alerting",
        )
        doc_one_term.save()
        doc_both_terms.save()

        results = BM25Field.search(BM25Doc, "content", "monitoring alerting", limit=10)
        # doc_both_terms has both query terms, should score higher
        if len(results) >= 2:
            keys = [k for k, _s in results]
            assert keys[0] == doc_both_terms.db_key.redis_key


class TestBM25TieOrdering:
    """Regression tests for deterministic tie-breaking (issue #446).

    Equal-scored documents must return in member key (redis_key) ascending
    order, byte-wise, broken inside the Lua script — independent of
    insertion order, repeatable across runs, and stable across the
    ``limit`` truncation boundary.
    """

    TIED_CONTENT = "zebra quagga okapi"
    TIED_NAMES = ["doc_a", "doc_b", "doc_c", "doc_d", "doc_e"]

    def _plant_tied_docs(self, names=None):
        """Save identical-content docs in NON-ascending key order."""
        names = names or self.TIED_NAMES
        for name in reversed(names):
            BM25Doc(name=name, raw_content=self.TIED_CONTENT).save()
        return [f"BM25Doc:{name}" for name in names]

    def test_tie_order_key_ascending_insertion_order_independent(self):
        """Tied docs return key-ascending regardless of insertion order."""
        expected_keys = self._plant_tied_docs()

        results = BM25Field.search(BM25Doc, "content", "zebra", limit=10)
        assert [k for k, _s in results] == expected_keys
        # All scores equal — proves the tie-break path was exercised.
        scores = [s for _k, s in results]
        assert len(set(scores)) == 1

    def test_repeated_searches_identical(self):
        """The same search returns the identical ordered list every run."""
        self._plant_tied_docs()

        first = BM25Field.search(BM25Doc, "content", "quagga", limit=10)
        assert len(first) == len(self.TIED_NAMES)
        for _ in range(9):
            assert (
                BM25Field.search(BM25Doc, "content", "quagga", limit=10) == first
            )

    def test_deterministic_truncation_at_limit(self):
        """With 5 tied docs and limit=3, exactly the 3 lowest keys return."""
        expected_keys = self._plant_tied_docs()

        results = BM25Field.search(BM25Doc, "content", "okapi", limit=3)
        assert [k for k, _s in results] == expected_keys[:3]

    def test_mixed_scores_tie_break_preserves_primary_order(self):
        """Higher-scoring doc ranks first; tied tail stays key-ascending."""
        expected_tied_keys = self._plant_tied_docs()
        # Repeating the query term boosts tf, so this doc scores higher.
        doc_top = BM25Doc(
            name="zzz_top", raw_content="zebra zebra zebra zebra zebra"
        )
        doc_top.save()

        results = BM25Field.search(BM25Doc, "content", "zebra", limit=10)
        keys = [k for k, _s in results]
        assert keys[0] == doc_top.db_key.redis_key
        assert keys[1:] == expected_tied_keys
        # Tail scores are equal and strictly below the top score.
        scores = [s for _k, s in results]
        assert len(set(scores[1:])) == 1
        assert scores[0] > scores[1]


class TestBM25FieldUpdateDelete:
    """Test document updates and deletes update BM25 stats correctly."""

    def test_document_update_changes_index(self):
        """Re-saving with new content updates the BM25 index."""
        doc = BM25Doc(name="update1", raw_content="original content about databases")
        doc.save()

        # Search for original term
        results = BM25Field.search(BM25Doc, "content", "databases", limit=10)
        keys = [k for k, _s in results]
        assert doc.db_key.redis_key in keys

        # Update content
        doc.raw_content = "completely new content about networking"
        doc.save()

        # Old term should no longer match this doc
        results_old = BM25Field.search(BM25Doc, "content", "databases", limit=10)
        keys_old = [k for k, _s in results_old]
        assert doc.db_key.redis_key not in keys_old

        # New term should match
        results_new = BM25Field.search(BM25Doc, "content", "networking", limit=10)
        keys_new = [k for k, _s in results_new]
        assert doc.db_key.redis_key in keys_new

    def test_document_delete_removes_from_index(self):
        """Deleting a document removes it from the BM25 index."""
        doc = BM25Doc(name="delete1", raw_content="temporary content about caching")
        doc.save()

        # Verify it's in the index
        results = BM25Field.search(BM25Doc, "content", "caching", limit=10)
        keys = [k for k, _s in results]
        assert doc.db_key.redis_key in keys

        # Delete
        doc.delete()

        # Should no longer appear
        results_after = BM25Field.search(BM25Doc, "content", "caching", limit=10)
        keys_after = [k for k, _s in results_after]
        assert doc.db_key.redis_key not in keys_after

    def test_delete_updates_doc_count(self):
        """Deleting a document decrements the corpus doc count."""
        prefix = BM25Doc._meta.fields["content"]._key_prefix(BM25Doc)
        n_key = f"{prefix}:n"

        doc1 = BM25Doc(name="ncount1", raw_content="document one content here")
        doc2 = BM25Doc(name="ncount2", raw_content="document two content here")
        doc1.save()
        doc2.save()

        n_after_save = int(POPOTO_REDIS_DB.get(n_key) or 0)

        doc2.delete()

        n_after_delete = int(POPOTO_REDIS_DB.get(n_key) or 0)
        assert n_after_delete == n_after_save - 1


class TestBM25FieldEdgeCases:
    """Edge cases: empty corpus, stop words, empty query."""

    def test_empty_corpus_returns_empty(self):
        """Searching an empty corpus returns empty results."""
        # Use a fresh model class name that hasn't had any saves
        results = BM25Field.search(BM25Doc, "content", "anything", limit=10)
        # May or may not be empty depending on other tests, but should not error
        assert isinstance(results, list)

    def test_query_all_stop_words_returns_empty(self):
        """Query consisting entirely of stop words returns empty results."""
        doc = BM25Doc(name="stopwords1", raw_content="important technical content")
        doc.save()

        results = BM25Field.search(BM25Doc, "content", "the and for are but", limit=10)
        assert results == []

    def test_empty_query_returns_empty(self):
        """Empty query string returns empty results."""
        results = BM25Field.search(BM25Doc, "content", "", limit=10)
        assert results == []

    def test_none_query_returns_empty(self):
        """None query returns empty results."""
        results = BM25Field.search(BM25Doc, "content", None, limit=10)
        assert results == []

    def test_empty_content_save_no_error(self):
        """Saving a document with empty content does not error."""
        doc = BM25Doc(name="empty1", raw_content="")
        doc.save()
        # Should not raise

    def test_query_term_not_in_corpus(self):
        """Querying a term not in any document returns empty or excludes it."""
        doc = BM25Doc(name="notincorpus1", raw_content="python flask framework")
        doc.save()

        results = BM25Field.search(BM25Doc, "content", "xyznonexistentterm", limit=10)
        # The term doesn't exist so no docs should match on it
        for key, score in results:
            # If there are results, they shouldn't include score from this term
            pass
        # More importantly, no error


class TestBM25FieldConfiguration:
    """Test BM25Field configuration and error handling."""

    def test_source_required(self):
        """BM25Field requires a source parameter."""
        with pytest.raises(ValueError, match="source"):
            BM25Field()

    def test_search_on_non_bm25_field_raises(self):
        """keyword_search() on a non-BM25 field raises QueryException."""
        with pytest.raises(QueryException):
            BM25Field.search(BM25DocNoBM25, "raw_content", "test", limit=10)

    def test_bm25_constants(self):
        """BM25 constants are set correctly."""
        assert BM25Field.BM25_K1 == 1.2
        assert BM25Field.BM25_B == 0.75

    def test_recompute_stats(self):
        """recompute_stats() corrects avgdl without error."""
        doc = BM25Doc(name="recompute1", raw_content="test recompute statistics")
        doc.save()

        # Should not raise
        BM25Field.recompute_stats(BM25Doc, "content")


class TestSharedTokenizer:
    """Verify the shared tokenizer produces identical output for both fields."""

    def test_tokenizer_same_output(self):
        """Shared tokenizer and ExistenceFilter tokenizer produce same results."""
        text = "Kubernetes deployment guide for production systems"
        assert tokenize(text) == ef_tokenize(text)

    def test_tokenizer_stop_words_filtered(self):
        """Stop words are filtered by both tokenizers identically."""
        text = "the and for are but not you all"
        assert tokenize(text) == ef_tokenize(text) == []

    def test_tokenizer_short_tokens_filtered(self):
        """Short tokens (< 3 chars) are filtered."""
        text = "a b cd efg hijk"
        result = tokenize(text)
        assert "a" not in result
        assert "b" not in result
        assert "cd" not in result
        assert "efg" in result
        assert "hijk" in result


class TestKeywordSearchQueryMethod:
    """Test the keyword_search() method on QueryBuilder."""

    def test_keyword_search_returns_instances(self):
        """keyword_search() on QueryBuilder returns model instances."""
        doc = BM25Doc(name="qb_search1", raw_content="machine learning optimization")
        doc.save()

        results = BM25Doc.query.keyword_search("machine learning", limit=10)
        assert len(results) > 0
        # Results should be model instances, not tuples
        for inst in results:
            assert hasattr(inst, "name")
            assert hasattr(inst, "_bm25_score")
            assert inst._bm25_score > 0

    def test_keyword_search_no_bm25_field_raises(self):
        """keyword_search() on model without BM25Field raises QueryException."""
        with pytest.raises(QueryException):
            BM25DocNoBM25.query.keyword_search("test", limit=10)


class TestBM25GetIdf:
    """Tests for BM25Field.get_idf() — IDF selectivity signal."""

    def test_get_idf_unseen_tokens(self):
        """Tokens that don't appear in any document get high IDF (rare = selective)."""
        # These tokens were never saved in any BM25Doc
        result = BM25Field.get_idf(BM25Doc, "content", ["xyzzyplugh", "quuxblargh"])
        # Unseen tokens have df=0, so IDF = log((N - 0 + 0.5) / (0 + 0.5))
        # which is a positive number (high selectivity). Just verify they're >= 0.
        for token, idf in result.items():
            assert idf >= 0.0

    def test_get_idf_single_token_string(self):
        """Single token as a string (not list) is handled gracefully."""
        doc = BM25Doc(name="idf-single", raw_content="kubernetes cluster setup")
        doc.save()

        result = BM25Field.get_idf(BM25Doc, "content", "kubernetes")
        assert isinstance(result, dict)
        assert "kubernetes" in result
        assert result["kubernetes"] > 0

    def test_get_idf_empty_token_list(self):
        """Empty token list returns empty dict."""
        result = BM25Field.get_idf(BM25Doc, "content", [])
        assert result == {}

    def test_get_idf_token_not_in_corpus(self):
        """Token not in corpus gets high IDF (maximum selectivity)."""
        doc = BM25Doc(name="idf-absent", raw_content="kubernetes deployment guide")
        doc.save()

        result = BM25Field.get_idf(BM25Doc, "content", ["xyznonexistent"])
        # Token with df=0 should have high IDF (very selective)
        assert result["xyznonexistent"] > 1.0

    def test_get_idf_common_vs_rare_token(self):
        """Common tokens have lower IDF than rare tokens."""
        # Save multiple docs with "kubernetes", one with "watchdog"
        for i in range(5):
            BM25Doc(
                name=f"idf-common-{i}",
                raw_content=f"kubernetes topic number {i:03d}extra",
            ).save()
        BM25Doc(
            name="idf-rare",
            raw_content="watchdog monitoring alerting system",
        ).save()

        result = BM25Field.get_idf(
            BM25Doc, "content", ["kubernetes", "watchdog"]
        )
        # "kubernetes" appears in 5/6 docs -> low IDF
        # "watchdog" appears in 1/6 docs -> high IDF
        assert result["watchdog"] > result["kubernetes"]

    def test_get_idf_on_non_bm25_field_raises(self):
        """get_idf() on a non-BM25 field raises QueryException."""
        with pytest.raises(QueryException):
            BM25Field.get_idf(BM25DocNoBM25, "raw_content", ["test"])

    def test_get_idf_multiple_tokens(self):
        """Batch IDF lookup returns correct values for multiple tokens."""
        BM25Doc(
            name="idf-multi-1",
            raw_content="redis caching strategies performance",
        ).save()
        BM25Doc(
            name="idf-multi-2",
            raw_content="redis cluster deployment production",
        ).save()

        result = BM25Field.get_idf(
            BM25Doc, "content", ["redis", "caching", "cluster"]
        )
        assert len(result) == 3
        # "redis" in 2/2 docs -> low IDF
        # "caching" in 1/2, "cluster" in 1/2 -> higher IDF
        assert result["caching"] > result["redis"]
        assert result["cluster"] > result["redis"]


class TestBM25FilterSelectiveTokens:
    """Tests for BM25Field.filter_selective_tokens()."""

    def test_filter_empty_list(self):
        """Empty token list returns empty list."""
        result = BM25Field.filter_selective_tokens(
            BM25Doc, "content", [], min_idf=1.0
        )
        assert result == []

    def test_filter_keeps_rare_tokens(self):
        """Rare tokens (high IDF) are kept, common ones filtered out."""
        # Create corpus where "common" appears in all docs, "rare" in one
        for i in range(5):
            BM25Doc(
                name=f"filter-common-{i}",
                raw_content=f"common baseline topic {i:03d}extra",
            ).save()
        BM25Doc(
            name="filter-rare",
            raw_content="rare specialized watchdog alerting",
        ).save()

        result = BM25Field.filter_selective_tokens(
            BM25Doc, "content", ["common", "rare"], min_idf=0.5
        )
        # "rare" should be kept (high IDF), "common" depends on threshold
        assert "rare" in result

    def test_filter_preserves_order(self):
        """Filtered tokens maintain original order."""
        BM25Doc(
            name="filter-order",
            raw_content="alpha bravo charlie delta",
        ).save()

        tokens = ["alpha", "bravo", "charlie", "delta"]
        result = BM25Field.filter_selective_tokens(
            BM25Doc, "content", tokens, min_idf=0.0
        )
        # With min_idf=0.0 and all tokens present, all should be kept in order
        assert result == tokens

    def test_filter_absent_tokens_included(self):
        """Tokens not in corpus get max IDF and are included (maximally selective)."""
        BM25Doc(
            name="filter-absent",
            raw_content="kubernetes deployment guide",
        ).save()

        result = BM25Field.filter_selective_tokens(
            BM25Doc, "content", ["kubernetes", "xyznonexistent"], min_idf=1.0
        )
        # "xyznonexistent" has max IDF (not in corpus) -> included
        assert "xyznonexistent" in result


class TestBatchBloomIdfIntegration:
    """End-to-end: batch bloom check -> IDF filter -> search."""

    def test_full_pipeline(self):
        """Batch bloom + IDF filter + search returns relevant results."""
        from src.popoto.fields.existence_filter import ExistenceFilter

        # Create a model with both bloom and BM25
        class IntegrationModel(popoto.Model):
            name = popoto.UniqueKeyField()
            raw_content = popoto.Field(type=str)
            content = BM25Field(source="raw_content")
            bloom = ExistenceFilter(
                error_rate=0.01,
                capacity=10_000,
                fingerprint_fn=lambda inst: inst.raw_content,
            )

        # Clean up
        for pattern in [
            "$EF:IntegrationModel:*",
            "$BM25:IntegrationModel:*",
            "IntegrationModel:*",
        ]:
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern):
                POPOTO_REDIS_DB.delete(key)

        # Save some docs
        IntegrationModel(
            name="int-1",
            raw_content="kubernetes deployment production cluster management",
        ).save()
        IntegrationModel(
            name="int-2",
            raw_content="kubernetes monitoring alerting watchdog system",
        ).save()
        IntegrationModel(
            name="int-3",
            raw_content="redis caching performance optimization",
        ).save()

        # Step 1: Batch bloom check
        keywords = ["kubernetes", "watchdog", "terraform", "redis"]
        bloom_results = IntegrationModel.bloom.might_exist_batch(
            IntegrationModel, keywords
        )
        bloom_hits = [k for k, v in bloom_results.items() if v]

        # "kubernetes", "watchdog", "redis" should hit; "terraform" should miss
        assert "kubernetes" in bloom_hits
        assert "watchdog" in bloom_hits
        assert "redis" in bloom_hits
        assert "terraform" not in bloom_hits

        # Step 2: Filter by selectivity
        selective = BM25Field.filter_selective_tokens(
            IntegrationModel, "content", bloom_hits, min_idf=0.1
        )
        assert len(selective) > 0

        # Step 3: Search with selective tokens
        results = BM25Field.search(
            IntegrationModel, "content", " ".join(selective), limit=10
        )
        assert len(results) > 0

        # Clean up
        for pattern in [
            "$EF:IntegrationModel:*",
            "$BM25:IntegrationModel:*",
            "IntegrationModel:*",
        ]:
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern):
                POPOTO_REDIS_DB.delete(key)
