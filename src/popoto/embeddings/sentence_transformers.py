"""
Sentence-Transformers embedding provider.

Wraps a local `sentence-transformers` model (``all-MiniLM-L6-v2`` by
default) behind the AbstractEmbeddingProvider interface. Inference runs
entirely on the local machine, so there is no API key, no per-token cost,
and no network dependency on a paid external provider.

The first use downloads the model weights (~90MB for ``all-MiniLM-L6-v2``)
from Hugging Face and caches them locally; subsequent uses are offline.

``sentence-transformers`` is a heavy optional dependency (it pulls in
PyTorch). It is therefore imported **lazily** inside ``embed()`` so that
``import popoto.embeddings`` stays cheap and does not require the package to
be installed. The dependency ships under the ``[benchmark]`` optional extra::

    pip install popoto[benchmark]

Example:
    from popoto.embeddings.sentence_transformers import (
        SentenceTransformersProvider,
    )
    provider = SentenceTransformersProvider()
    vectors = provider.embed(["hello world"])
"""

from typing import List, Optional

from . import AbstractEmbeddingProvider

# Dimensionality of ``all-MiniLM-L6-v2`` output vectors. Known a priori for
# MiniLM, so ``dimensions`` can be reported without loading the model.
_MINILM_DIMENSIONS = 384


def _load_sentence_transformer_cls():
    """Lazily import and return the ``SentenceTransformer`` class.

    Raises:
        ImportError: If ``sentence-transformers`` is not installed, with an
            actionable hint pointing at the ``[benchmark]`` extra.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "sentence-transformers is required to use "
            "SentenceTransformersProvider. Install it with: "
            "pip install popoto[benchmark]"
        ) from e
    return SentenceTransformer


class SentenceTransformersProvider(AbstractEmbeddingProvider):
    """Local Sentence-Transformers embedding provider.

    Wraps a ``sentence-transformers`` model and produces dense vectors with
    no API key. The default model, ``all-MiniLM-L6-v2``, emits 384-dim
    vectors and is symmetric (the same encoder is used for documents and
    queries), so ``input_type`` is accepted for interface compatibility but
    ignored.

    The underlying model is loaded lazily on the first ``embed()`` call and
    cached on the instance; constructing the provider does nothing heavy and
    triggers no download.

    Args:
        model_name: Name of the sentence-transformers model to load.
            Default ``all-MiniLM-L6-v2`` (384-dim).
        dimensions: Output vector dimensionality. Defaults to 384 for the
            MiniLM model; override only if you pass a different model.

    Raises:
        ImportError: At ``embed()`` time if ``sentence-transformers`` is not
            installed. Install it with ``pip install popoto[benchmark]``.
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        dimensions: int = _MINILM_DIMENSIONS,
    ):
        self._model_name = model_name
        self._dimensions = dimensions
        # _model stays None until the first embed() call loads (and caches) it.
        self._model = None

    def _get_model(self):
        """Load and cache the sentence-transformers model on first use."""
        if self._model is None:
            SentenceTransformer = _load_sentence_transformer_cls()
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings with a local sentence-transformers model.

        Args:
            texts: List of text strings to embed.
            input_type: Accepted for interface compatibility but ignored —
                ``all-MiniLM-L6-v2`` is a symmetric encoder.

        Returns:
            List of embedding vectors, one per input text. An empty input
            list returns ``[]`` without loading the model.

        Raises:
            ImportError: If ``sentence-transformers`` is not installed.
        """
        if not texts:
            return []

        model = self._get_model()
        # model.encode returns a numpy ndarray; .tolist() yields plain floats.
        return model.encode(list(texts)).tolist()

    @property
    def dimensions(self) -> int:
        """Embedding vector dimensionality (384 for ``all-MiniLM-L6-v2``)."""
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        """Conservative batch limit for local CPU inference.

        Embedding many texts in a single forward pass can be memory-heavy on
        modest hardware. Users with more headroom can subclass and override.
        """
        return 64
