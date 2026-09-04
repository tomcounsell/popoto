"""
OpenAI embedding provider.

Wraps the OpenAI embedding API behind the AbstractEmbeddingProvider
interface. Requires the openai package (pip install popoto[openai]).

Example:
    from popoto.embeddings.openai import OpenAIProvider
    provider = OpenAIProvider(api_key="your-key")
    vectors = provider.embed(["hello world"], input_type="document")
"""

from typing import List, Optional

from . import AbstractEmbeddingProvider

try:
    import openai as openai_module

    _openai_available = True
except ImportError:
    _openai_available = False


class OpenAIProvider(AbstractEmbeddingProvider):
    """OpenAI embedding provider.

    Args:
        api_key: OpenAI API key. If None, reads from OPENAI_API_KEY env var.
        model: Model name to use. Default "text-embedding-3-small".
        dim: Embedding dimensions. Default 1536 for text-embedding-3-small.

    Raises:
        ImportError: If the openai package is not installed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
    ):
        if not _openai_available:
            raise ImportError(
                "openai is required to use OpenAIProvider. "
                "Install it with: pip install popoto[openai]"
            )
        self._model = model
        self._dim = dim
        self._client = openai_module.OpenAI(api_key=api_key)

    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings via OpenAI API.

        Args:
            texts: List of text strings to embed.
            input_type: Ignored by OpenAI (included for interface compatibility).

        Returns:
            List of embedding vectors.

        Raises:
            RuntimeError: If the API call fails.
        """
        if not texts:
            return []

        response = self._client.embeddings.create(
            input=texts,
            model=self._model,
        )
        return [item.embedding for item in response.data]

    @property
    def dimensions(self) -> int:
        """Return the configured embedding dimensions."""
        return self._dim

    @property
    def max_batch_size(self) -> int:
        """OpenAI supports up to 2048 texts per batch."""
        return 2048
