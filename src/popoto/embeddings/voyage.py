"""
Voyage AI embedding provider.

Wraps the Voyage AI embedding API behind the AbstractEmbeddingProvider
interface. Requires the voyageai package (pip install popoto[voyage]).

Voyage AI provides high-quality embeddings optimized for retrieval,
with separate document/query embedding modes via the input_type parameter.

Example:
    from popoto.embeddings.voyage import VoyageProvider
    provider = VoyageProvider(api_key="your-key", model="voyage-3")
    vectors = provider.embed(["hello world"], input_type="document")
"""

from typing import List, Optional

from . import AbstractEmbeddingProvider

try:
    import voyageai

    _voyageai_available = True
except ImportError:
    _voyageai_available = False


class VoyageProvider(AbstractEmbeddingProvider):
    """Voyage AI embedding provider.

    Args:
        api_key: Voyage AI API key. If None, reads from VOYAGE_API_KEY env var.
        model: Model name to use. Default "voyage-3".

    Raises:
        ImportError: If the voyageai package is not installed.
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = "voyage-3",
    ):
        if not _voyageai_available:
            raise ImportError(
                "voyageai is required to use VoyageProvider. "
                "Install it with: pip install popoto[voyage]"
            )
        self._model = model
        self._client = voyageai.Client(api_key=api_key)

    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings via Voyage AI API.

        Args:
            texts: List of text strings to embed.
            input_type: "document" or "query" -- Voyage AI uses this
                to optimize embeddings for storage vs retrieval.

        Returns:
            List of embedding vectors.

        Raises:
            RuntimeError: If the API call fails.
        """
        if not texts:
            return []

        result = self._client.embed(
            texts,
            model=self._model,
            input_type=input_type,
        )
        return result.embeddings

    @property
    def dimensions(self) -> int:
        """Voyage-3 produces 1024-dimensional vectors."""
        return 1024

    @property
    def max_batch_size(self) -> int:
        """Voyage AI supports up to 128 texts per batch."""
        return 128
