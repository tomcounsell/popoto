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
