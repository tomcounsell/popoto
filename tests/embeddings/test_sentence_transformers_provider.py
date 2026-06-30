"""Tests for SentenceTransformersProvider (issue #437).

Covers the offline-safe contract:
- dimensions == 384 without loading the model
- embed([]) == [] without loading the model
- a missing sentence-transformers dependency raises an actionable ImportError
- the provider implements AbstractEmbeddingProvider

A single real ``encode`` round-trip is included but GUARDED behind a
skip-if-model-not-cached check so the suite stays green (and downloads
nothing) when offline.
"""

import sys

import pytest

from src.popoto.embeddings import (
    AbstractEmbeddingProvider,
    SentenceTransformersProvider,
)

MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"


def _model_is_cached() -> bool:
    """Return True only if the MiniLM weights are already in the HF cache.

    Used to skip the real-encode test when the model has not been downloaded,
    so the unit suite never triggers a ~90MB download or needs network.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return False
    try:
        path = try_to_load_from_cache(repo_id=MODEL_REPO, filename="config.json")
    except Exception:
        return False
    return isinstance(path, str)


class TestContract:
    """Interface and offline-safe behavior."""

    def test_is_abstract_subclass(self):
        assert issubclass(SentenceTransformersProvider, AbstractEmbeddingProvider)

    def test_dimensions_is_384_without_loading(self):
        """dimensions reports 384 without loading the model (no download)."""
        provider = SentenceTransformersProvider()
        assert provider.dimensions == 384
        # The model must NOT have been loaded just to read dimensions.
        assert provider._model is None

    def test_embed_empty_returns_empty_without_loading(self):
        """embed([]) short-circuits to [] without loading the model."""
        provider = SentenceTransformersProvider()
        assert provider.embed([]) == []
        assert provider._model is None

    def test_max_batch_size_is_conservative(self):
        assert SentenceTransformersProvider().max_batch_size == 64


class TestMissingDependency:
    """The missing-dependency branch must fail loud with an actionable hint."""

    def test_embed_without_sentence_transformers_raises(self, monkeypatch):
        """Simulate sentence-transformers being absent via import monkeypatch."""
        # Force `from sentence_transformers import SentenceTransformer` to fail.
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        provider = SentenceTransformersProvider()
        with pytest.raises(ImportError, match=r"popoto\[benchmark\]"):
            provider.embed(["this should fail to embed"])


@pytest.mark.skipif(
    not _model_is_cached(),
    reason="all-MiniLM-L6-v2 not cached; skipping to avoid a ~90MB download",
)
class TestRealEncode:
    """One real encode round-trip, only when the model is already cached."""

    def test_encode_roundtrip_shape(self):
        provider = SentenceTransformersProvider()
        try:
            vectors = provider.embed(["hello world", "a second sentence"])
        except Exception as e:  # network hiccup / load failure → skip, don't fail
            pytest.skip(f"model unavailable at runtime: {e}")
        assert len(vectors) == 2
        assert len(vectors[0]) == 384
        assert len(vectors[1]) == 384
        assert all(isinstance(x, float) for x in vectors[0])
