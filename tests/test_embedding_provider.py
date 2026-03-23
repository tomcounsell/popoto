"""Tests for AbstractEmbeddingProvider and provider implementations.

Tests cover:
- AbstractEmbeddingProvider cannot be instantiated
- Mock provider implements interface correctly
- VoyageProvider and OpenAIProvider raise ImportError when SDK missing
- Provider embed() returns correct shape
- Provider dimensions and max_batch_size properties
- Empty input handling
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto.embeddings import AbstractEmbeddingProvider


class MockProvider(AbstractEmbeddingProvider):
    """Mock embedding provider for testing."""

    def __init__(self, dim=4):
        self._dim = dim
        self.call_log = []

    def embed(self, texts, input_type=None):
        self.call_log.append({"texts": texts, "input_type": input_type})
        # Return deterministic embeddings based on text length
        return [[float(len(t)) / 100.0] * self._dim for t in texts]

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


class TestAbstractInterface:
    """Test that AbstractEmbeddingProvider enforces the contract."""

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            AbstractEmbeddingProvider()

    def test_mock_provider_is_subclass(self):
        assert issubclass(MockProvider, AbstractEmbeddingProvider)

    def test_incomplete_implementation_raises(self):
        """A provider missing methods cannot be instantiated."""

        class BadProvider(AbstractEmbeddingProvider):
            pass

        with pytest.raises(TypeError):
            BadProvider()


class TestMockProvider:
    """Test the mock provider implements the interface correctly."""

    def test_embed_returns_list_of_vectors(self):
        provider = MockProvider(dim=4)
        results = provider.embed(["hello", "world"])
        assert len(results) == 2
        assert len(results[0]) == 4
        assert len(results[1]) == 4

    def test_embed_empty_list(self):
        provider = MockProvider()
        results = provider.embed([])
        assert results == []

    def test_dimensions_property(self):
        p = MockProvider(dim=8)
        assert p.dimensions == 8

    def test_max_batch_size_property(self):
        p = MockProvider()
        assert p.max_batch_size == 32

    def test_input_type_passed_through(self):
        provider = MockProvider()
        provider.embed(["test"], input_type="document")
        assert provider.call_log[0]["input_type"] == "document"

        provider.embed(["test"], input_type="query")
        assert provider.call_log[1]["input_type"] == "query"

    def test_embed_produces_consistent_output(self):
        provider = MockProvider(dim=3)
        r1 = provider.embed(["same text"])
        r2 = provider.embed(["same text"])
        assert r1 == r2


class TestVoyageProviderImport:
    """Test VoyageProvider import behavior."""

    def test_voyage_provider_importable(self):
        """VoyageProvider class can be imported regardless of SDK availability."""
        from src.popoto.embeddings.voyage import VoyageProvider

        assert VoyageProvider is not None

    def test_voyage_provider_without_sdk_raises(self):
        """If voyageai SDK is not installed, instantiation raises ImportError."""
        from src.popoto.embeddings.voyage import _voyageai_available

        if not _voyageai_available:
            from src.popoto.embeddings.voyage import VoyageProvider

            with pytest.raises(ImportError, match="voyageai"):
                VoyageProvider()


class TestOpenAIProviderImport:
    """Test OpenAIProvider import behavior."""

    def test_openai_provider_importable(self):
        """OpenAIProvider class can be imported regardless of SDK availability."""
        from src.popoto.embeddings.openai import OpenAIProvider

        assert OpenAIProvider is not None

    def test_openai_provider_without_sdk_raises(self):
        """If openai SDK is not installed, instantiation raises ImportError."""
        from src.popoto.embeddings.openai import _openai_available

        if not _openai_available:
            from src.popoto.embeddings.openai import OpenAIProvider

            with pytest.raises(ImportError, match="openai"):
                OpenAIProvider()
