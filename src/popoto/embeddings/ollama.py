"""
Ollama embedding provider.

Wraps the Ollama local inference server behind the AbstractEmbeddingProvider
interface. Uses only stdlib (urllib.request) -- no additional dependencies.

Requires a running Ollama instance (https://ollama.com). Start it with:
    ollama serve

Pull a model before use:
    ollama pull nomic-embed-text

The /api/embed endpoint was introduced in Ollama v0.1.26 (2024-01). Versions
older than v0.1.26 only have the legacy /api/embeddings endpoint (single-text).

Example:
    from popoto.embeddings.ollama import OllamaProvider
    provider = OllamaProvider(model="nomic-embed-text")
    vectors = provider.embed(["hello world"], input_type="document")
"""

import json
import urllib.error
import urllib.request
from typing import List, Optional

from . import AbstractEmbeddingProvider


class OllamaProvider(AbstractEmbeddingProvider):
    """Ollama local embedding provider.

    Connects to a locally-running Ollama server and generates embeddings
    using a locally-hosted model. No API key required. No network calls to
    external services.

    Dimensions are auto-detected from the first embed() call if not supplied
    via the ``dim`` constructor argument.

    Args:
        base_url: URL of the Ollama server. Default "http://localhost:11434".
        model: Ollama model name to use. Default "nomic-embed-text" (768-dim).
            Common alternatives: "mxbai-embed-large" (1024-dim),
            "all-minilm" (384-dim).
        dim: Embedding dimensions. If None, auto-detected on first embed()
            call. Provide an explicit value to skip auto-detection and to
            allow calling the ``dimensions`` property before the first embed.

    Raises:
        RuntimeError: If Ollama is not running or the model is not found.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dim: Optional[int] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim

    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings via the local Ollama server.

        Posts a batch request to ``/api/embed`` and returns one vector per
        input text. The ``input_type`` parameter is accepted for interface
        compatibility but is ignored -- Ollama does not distinguish document
        vs. query embeddings.

        Args:
            texts: List of text strings to embed.
            input_type: Accepted but ignored (Ollama has no input-type concept).

        Returns:
            List of embedding vectors, one per input text. Empty list if
            ``texts`` is empty.

        Raises:
            RuntimeError: If Ollama is not running (mentions ``ollama serve``).
            RuntimeError: If the model is not found (mentions ``ollama pull``).
            RuntimeError: If the HTTP response indicates another error.
        """
        if not texts:
            return []

        payload = json.dumps({"model": self._model, "input": texts}).encode()
        request = urllib.request.Request(
            f"{self._base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as e:
            # Read the error body to extract Ollama's error message.
            raw = e.read().decode()
            parsed = json.loads(raw) if raw else {}
            msg = parsed.get("error", "")
            if "not found" in msg.lower() or "model" in msg.lower():
                raise RuntimeError(
                    f"Model '{self._model}' not found. "
                    f"Run: ollama pull {self._model}"
                ) from e
            raise RuntimeError(f"Ollama HTTP {e.code}: {msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is the server running? Start it with: ollama serve"
            ) from e

        data = json.loads(body)
        embeddings: List[List[float]] = data["embeddings"]

        # Auto-detect dimensions from the first response vector.
        if self._dim is None and embeddings:
            self._dim = len(embeddings[0])

        return embeddings

    @property
    def dimensions(self) -> int:
        """Return the embedding vector dimensionality.

        Returns the constructor-supplied ``dim`` value, or the value
        auto-detected from the first ``embed()`` call.

        Raises:
            RuntimeError: If ``embed()`` has not been called yet and ``dim``
                was not supplied to the constructor.
        """
        if self._dim is None:
            raise RuntimeError(
                "OllamaProvider: dimensions unknown — "
                "call embed() first or pass dim=<n> to the constructor"
            )
        return self._dim

    @property
    def max_batch_size(self) -> int:
        """Maximum texts per batch call.

        Conservative default for local inference -- large batches can exhaust
        GPU/CPU memory on modest hardware. Subclass and override to increase.
        """
        return 32
