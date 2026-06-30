"""
Embedding providers for Popoto EmbeddingField.

This module provides the AbstractEmbeddingProvider interface and built-in
provider implementations for generating text embeddings via external APIs.

Providers are pluggable -- configure a default via popoto.configure() or
pass a provider instance directly to EmbeddingField.

Available providers:
    - VoyageProvider: Voyage AI embeddings (requires voyageai package)
    - OpenAIProvider: OpenAI embeddings (requires openai package)
    - OllamaProvider: Local Ollama embeddings (requires a running Ollama
      server; stdlib-only, no extra package needed)
    - SentenceTransformersProvider: Local sentence-transformers embeddings
      (all-MiniLM-L6-v2, 384-dim, no API key; requires the [benchmark] extra,
      imported lazily)
"""

from abc import ABC, abstractmethod
from typing import List, Optional


class AbstractEmbeddingProvider(ABC):
    """Abstract interface for embedding generation providers.

    Implementations wrap external embedding APIs (Voyage AI, OpenAI, etc.)
    behind a consistent interface. The provider handles authentication,
    batching, and dimension configuration internally.

    Subclasses must implement:
        - embed(): Generate embeddings for a list of texts
        - dimensions (property): Vector dimensionality
        - max_batch_size (property): Maximum texts per API call
    """

    @abstractmethod
    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings for a list of text strings.

        Args:
            texts: List of text strings to embed.
            input_type: Optional hint for the provider. Common values:
                - "document": Text being stored/indexed
                - "query": Text being searched for
                Some providers (e.g., Voyage AI) use this to optimize
                embeddings for retrieval. Others ignore it.

        Returns:
            List of embedding vectors, one per input text.
            Each vector is a list of floats with length == self.dimensions.

        Raises:
            RuntimeError: If the provider API call fails.
            ValueError: If texts is empty or contains invalid entries.
        """
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """The dimensionality of embedding vectors produced by this provider."""
        ...

    @property
    @abstractmethod
    def max_batch_size(self) -> int:
        """Maximum number of texts that can be embedded in a single API call."""
        ...


from .ollama import OllamaProvider  # noqa: E402  (stdlib-only, safe to eagerly import)

# Safe to eagerly import: the heavy sentence-transformers dependency is
# imported lazily inside SentenceTransformersProvider.embed(), not here.
from .sentence_transformers import (  # noqa: E402
    SentenceTransformersProvider,
)

__all__ = [
    "AbstractEmbeddingProvider",
    "OllamaProvider",
    "SentenceTransformersProvider",
]
