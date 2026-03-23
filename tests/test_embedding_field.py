"""Tests for EmbeddingField — automatic embedding generation and storage.

Tests cover:
- EmbeddingField generates embeddings on save via mock provider
- Embedding stored as .npy file on filesystem
- Dimension count stored in Redis
- on_delete removes .npy file
- Cache loading and invalidation
- None/empty source field skips embedding
- Provider failure raises RuntimeError
- auto_embed=False skips automatic generation
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

np = pytest.importorskip("numpy")
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


# --- Mock Provider ---


class MockProvider(AbstractEmbeddingProvider):
    """Mock embedding provider that returns deterministic vectors."""

    def __init__(self, dim=4):
        self._dim = dim
        self.call_count = 0

    def embed(self, texts, input_type=None):
        self.call_count += 1
        vectors = []
        for text in texts:
            # Deterministic: hash-based vector
            seed = sum(ord(c) for c in text) % 1000
            rng = np.random.RandomState(seed)
            vectors.append(rng.randn(self._dim).tolist())
        return vectors

    @property
    def dimensions(self):
        return self._dim

    @property
    def max_batch_size(self):
        return 32


class FailingProvider(AbstractEmbeddingProvider):
    """Provider that always fails."""

    def embed(self, texts, input_type=None):
        raise RuntimeError("Provider API error")

    @property
    def dimensions(self):
        return 4

    @property
    def max_batch_size(self):
        return 32


# --- Test Models ---


class EmbDoc(popoto.Model):
    name = popoto.UniqueKeyField()
    content = ContentField()
    embedding = EmbeddingField(source="content")


class EmbDocNoAuto(popoto.Model):
    name = popoto.UniqueKeyField()
    content = popoto.StringField(default="")
    embedding = EmbeddingField(source="content", auto_embed=False)


class EmbDocPlainSource(popoto.Model):
    """Model with EmbeddingField sourced from a plain StringField."""

    name = popoto.UniqueKeyField()
    text = popoto.StringField(default="")
    embedding = EmbeddingField(source="text")


# --- Fixtures ---


@pytest.fixture(autouse=True)
def setup_providers(tmp_path):
    """Set up mock provider and temp content store."""
    provider = MockProvider(dim=4)
    set_default_provider(provider)

    store = FilesystemStore(base_path=str(tmp_path / "content"), extension=".txt")
    set_default_store(store)

    # Override embeddings dir to use temp
    os.environ["POPOTO_CONTENT_PATH"] = str(tmp_path / "content")

    yield provider

    set_default_provider(None)
    set_default_store(None)
    invalidate_cache()
    os.environ.pop("POPOTO_CONTENT_PATH", None)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up Redis keys after each test."""
    yield
    for model_class in [EmbDoc, EmbDocNoAuto, EmbDocPlainSource]:
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


class TestEmbeddingFieldSave:
    """Test embedding generation on save."""

    def test_save_generates_embedding(self, setup_providers):
        doc = EmbDoc(name="emb1", content="Hello world")
        doc.save()
        assert setup_providers.call_count == 1

    def test_save_stores_npy_file(self):
        doc = EmbDoc(name="emb2", content="Test content")
        doc.save()

        redis_key = doc._redis_key or doc.db_key.redis_key
        npy_path = EmbeddingField._embedding_path("EmbDoc", redis_key)
        assert os.path.exists(npy_path)

        vec = np.load(npy_path)
        assert vec.shape == (4,)
        assert vec.dtype == np.float32

    def test_save_stores_dimension_in_redis(self):
        doc = EmbDoc(name="emb3", content="Test content")
        doc.save()

        # The embedding field value should be the dimension count
        loaded = EmbDoc.query.get(name="emb3")
        assert loaded.__dict__.get("embedding") == 4

    def test_save_with_none_content_skips_embedding(self, setup_providers):
        doc = EmbDoc(name="emb4", content=None)
        doc.save()
        assert setup_providers.call_count == 0

    def test_save_with_empty_content_skips_embedding(self, setup_providers):
        doc = EmbDoc(name="emb5", content="")
        doc.save()
        assert setup_providers.call_count == 0

    def test_save_with_plain_string_source(self, setup_providers):
        doc = EmbDocPlainSource(name="emb6", text="Plain string content")
        doc.save()
        assert setup_providers.call_count == 1


class TestEmbeddingFieldAutoEmbed:
    """Test auto_embed flag."""

    def test_auto_embed_false_skips_generation(self, setup_providers):
        doc = EmbDocNoAuto(name="noauto1", content="Has content")
        doc.save()
        assert setup_providers.call_count == 0


class TestEmbeddingFieldDelete:
    """Test embedding cleanup on delete."""

    def test_delete_removes_npy_file(self):
        doc = EmbDoc(name="del1", content="Delete me")
        doc.save()

        redis_key = doc._redis_key or doc.db_key.redis_key
        npy_path = EmbeddingField._embedding_path("EmbDoc", redis_key)
        assert os.path.exists(npy_path)

        doc.delete()
        assert not os.path.exists(npy_path)


class TestEmbeddingCache:
    """Test embedding cache loading and invalidation."""

    def test_load_embeddings_returns_matrix(self):
        doc1 = EmbDoc(name="cache1", content="First document")
        doc1.save()
        doc2 = EmbDoc(name="cache2", content="Second document")
        doc2.save()

        matrix, keys = EmbeddingField.load_embeddings(EmbDoc)
        assert matrix is not None
        assert matrix.shape == (2, 4)
        assert len(keys) == 2

    def test_load_embeddings_matrix_is_normalized(self):
        doc = EmbDoc(name="norm1", content="Normalize this")
        doc.save()

        matrix, keys = EmbeddingField.load_embeddings(EmbDoc)
        norms = np.linalg.norm(matrix, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_cache_invalidated_on_save(self):
        doc = EmbDoc(name="inv1", content="Initial content")
        doc.save()

        # Load cache
        matrix1, keys1 = EmbeddingField.load_embeddings(EmbDoc)
        assert matrix1 is not None

        # Save another document - should invalidate cache
        doc2 = EmbDoc(name="inv2", content="New content")
        doc2.save()

        # Cache should be cleared; reload gets fresh data
        matrix2, keys2 = EmbeddingField.load_embeddings(EmbDoc)
        assert len(keys2) == 2

    def test_load_empty_model_returns_none(self):
        matrix, keys = EmbeddingField.load_embeddings(EmbDocNoAuto)
        assert matrix is None
        assert keys == []


class TestEmbeddingFieldErrors:
    """Test error handling."""

    def test_provider_failure_raises(self):
        # Set failing provider
        failing = FailingProvider()
        for fname, field in EmbDoc._meta.fields.items():
            if isinstance(field, EmbeddingField):
                field._provider = failing

        doc = EmbDoc(name="fail1", content="This will fail")
        with pytest.raises(RuntimeError, match="Embedding provider failed"):
            doc.save()

        # Reset provider
        for fname, field in EmbDoc._meta.fields.items():
            if isinstance(field, EmbeddingField):
                field._provider = None

    def test_no_provider_configured_skips(self):
        set_default_provider(None)
        doc = EmbDoc(name="noprov1", content="No provider")
        # Should not raise -- just skip embedding
        doc.save()
        set_default_provider(MockProvider(dim=4))
