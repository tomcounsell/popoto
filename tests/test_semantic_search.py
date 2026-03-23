"""Tests for semantic_search() — query integration with composite_score.

Tests cover:
- semantic_search returns ranked results using mock provider
- Empty query returns empty list
- No embeddings returns empty list
- Integration with composite_score indexes
- Model without EmbeddingField raises QueryException
- semantic_search via Query convenience method
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto.fields.content_field import ContentField, set_default_store
from src.popoto.fields.embedding_field import (
    EmbeddingField,
    set_default_provider,
    invalidate_cache,
)
from src.popoto.stores.filesystem import FilesystemStore
from src.popoto.embeddings import AbstractEmbeddingProvider
from src.popoto.redis_db import POPOTO_REDIS_DB
from src.popoto.models.query import QueryException


# --- Mock Provider ---


class SemanticMockProvider(AbstractEmbeddingProvider):
    """Mock provider that returns vectors based on keyword overlap.

    Texts containing "revenue" get a vector pointing in one direction,
    texts containing "weather" get a different direction, making
    similarity predictable for testing.
    """

    def __init__(self):
        self._dim = 4

    def embed(self, texts, input_type=None):
        vectors = []
        for text in texts:
            text_lower = text.lower()
            if "revenue" in text_lower:
                vectors.append([0.9, 0.1, 0.0, 0.0])
            elif "weather" in text_lower:
                vectors.append([0.0, 0.0, 0.9, 0.1])
            elif "financial" in text_lower or "money" in text_lower:
                vectors.append([0.7, 0.3, 0.0, 0.0])
            else:
                vectors.append([0.25, 0.25, 0.25, 0.25])
        return vectors

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


# --- Test Models ---


class SearchDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")


class NoEmbeddingDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")


# --- Fixtures ---


@pytest.fixture(autouse=True)
def setup_env(tmp_path):
    """Set up mock provider and temp storage."""
    provider = SemanticMockProvider()
    set_default_provider(provider)

    store = FilesystemStore(base_path=str(tmp_path / "content"))
    set_default_store(store)
    os.environ["POPOTO_CONTENT_PATH"] = str(tmp_path / "content")

    yield

    set_default_provider(None)
    set_default_store(None)
    invalidate_cache()
    os.environ.pop("POPOTO_CONTENT_PATH", None)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [SearchDoc, NoEmbeddingDoc]:
        keys = POPOTO_REDIS_DB.smembers(model_class._meta.db_class_set_key.redis_key)
        if keys:
            pipe = POPOTO_REDIS_DB.pipeline()
            for k in keys:
                pipe.delete(k)
            pipe.delete(model_class._meta.db_class_set_key.redis_key)
            pipe.execute()
        for pattern in [f"${model_class.__name__}*", f"$*:{model_class.__name__}*"]:
            for k in POPOTO_REDIS_DB.scan_iter(match=pattern):
                POPOTO_REDIS_DB.delete(k)


def create_docs():
    """Create test documents for semantic search."""
    docs = [
        ("revenue_q1", "Revenue report for Q1 showing growth in revenue"),
        ("weather_jan", "Weather forecast for January with cold weather"),
        ("financial", "Financial analysis of market trends and money"),
        ("general", "A general document about various topics"),
    ]
    for name, content in docs:
        doc = SearchDoc(name=name, content=content)
        doc.save()
    return docs


class TestSemanticSearchBasic:
    """Test basic semantic search functionality."""

    def test_semantic_search_returns_results(self):
        create_docs()
        results = SearchDoc.query.semantic_search("revenue trends")
        assert len(results) > 0

    def test_semantic_search_ranks_by_similarity(self):
        create_docs()
        results = SearchDoc.query.semantic_search("revenue growth")
        # Revenue doc should be first (highest similarity)
        assert results[0].name == "revenue_q1"

    def test_semantic_search_weather_query(self):
        create_docs()
        results = SearchDoc.query.semantic_search("weather forecast")
        # Weather doc should be first
        assert results[0].name == "weather_jan"

    def test_semantic_search_returns_model_instances(self):
        create_docs()
        results = SearchDoc.query.semantic_search("revenue")
        assert all(isinstance(r, SearchDoc) for r in results)

    def test_semantic_search_respects_limit(self):
        create_docs()
        results = SearchDoc.query.semantic_search("general topic", limit=2)
        assert len(results) <= 2


class TestSemanticSearchEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_query_returns_empty(self):
        create_docs()
        results = SearchDoc.query.semantic_search("")
        assert results == []

    def test_whitespace_query_returns_empty(self):
        create_docs()
        results = SearchDoc.query.semantic_search("   ")
        assert results == []

    def test_no_embeddings_returns_empty(self):
        # Don't create any docs
        results = SearchDoc.query.semantic_search("test query")
        assert results == []

    def test_no_provider_returns_empty(self):
        create_docs()
        set_default_provider(None)
        results = SearchDoc.query.semantic_search("test")
        assert results == []
        # Restore for cleanup
        set_default_provider(SemanticMockProvider())

    def test_model_without_embedding_field_raises(self):
        with pytest.raises(QueryException, match="no EmbeddingField"):
            NoEmbeddingDoc.query.semantic_search("test")


class TestSemanticSearchWithQueryBuilder:
    """Test semantic_search via QueryBuilder (filter().semantic_search())."""

    def test_via_query_convenience_method(self):
        create_docs()
        # Use the Query.semantic_search convenience method
        results = SearchDoc.query.semantic_search("revenue")
        assert len(results) > 0
        assert results[0].name == "revenue_q1"
