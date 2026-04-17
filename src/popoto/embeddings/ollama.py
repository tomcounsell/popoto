"""
Ollama embedding provider.

Wraps a locally-running Ollama server behind the AbstractEmbeddingProvider
interface. Uses only the Python standard library (urllib.request) -- no
additional dependencies required.

Ollama runs locally and requires:
    - An Ollama server running (``ollama serve``)
    - The embedding model pulled (``ollama pull nomic-embed-text``)

Because inference happens on the local machine there is no API key, no
per-token cost, and no network dependency on a paid external provider.

Note:
    The ``/api/embed`` endpoint (batch-capable) was introduced in Ollama
    v0.2.0 (July 2024). This provider targets that endpoint rather than
    the legacy ``/api/embeddings`` (single-text) endpoint.

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

    Speaks to a locally-running Ollama server via its ``/api/embed``
    endpoint. No API key, no network dependency on a paid service.

    Args:
        base_url: Base URL of the Ollama server. Default
            ``http://localhost:11434``.
        model: Name of an Ollama embedding model that has been pulled
            locally. Default ``nomic-embed-text`` (768-dim).
        dim: Optional explicit vector dimensionality. If provided,
            ``dimensions`` returns this value immediately without
            requiring an ``embed()`` call. If ``None`` (default),
            dimensions are auto-detected from the first ``embed()``
            response and cached.

    Raises:
        RuntimeError: At ``embed()`` time if the Ollama server is
            unreachable (``ollama serve`` not running) or the requested
            model is not available (run ``ollama pull <model>``).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dim: Optional[int] = None,
    ):
        # Normalise trailing slash so we can safely concat the endpoint.
        self._base_url = base_url.rstrip("/")
        self._model = model
        # _dim is None until either the user supplies it explicitly or
        # the first successful embed() call auto-detects it.
        self._dim: Optional[int] = dim

    def embed(
        self,
        texts: List[str],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate embeddings via the local Ollama server.

        Args:
            texts: List of text strings to embed.
            input_type: Accepted for interface compatibility but ignored.
                Ollama's embedding endpoint does not distinguish between
                document and query inputs.

        Returns:
            List of embedding vectors, one per input text.

        Raises:
            RuntimeError: If the Ollama server is unreachable, the model
                is not available, or the server returns an HTTP error.
        """
        if not texts:
            return []

        url = f"{self._base_url}/api/embed"
        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            # No retry/backoff: local inference should fail fast so the
            # caller sees the underlying problem (ollama serve down,
            # model missing) immediately.
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Ollama returns JSON like {"error": "model 'foo' not found"}.
            # Discriminate between "model not available" (actionable via
            # `ollama pull`) and generic HTTP failures.
            raw = b""
            try:
                raw = e.read()
            except Exception:
                # Some HTTPError instances may already have a consumed body.
                pass
            body_text = raw.decode("utf-8", errors="replace") if raw else ""
            try:
                parsed = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                parsed = {}
            msg = parsed.get("error", "") if isinstance(parsed, dict) else ""
            msg_lower = msg.lower()
            if "not found" in msg_lower or "model" in msg_lower:
                raise RuntimeError(
                    f"Model '{self._model}' not found on Ollama server at "
                    f"{self._base_url}. Run: ollama pull {self._model}"
                ) from e
            raise RuntimeError(
                f"Ollama HTTP {e.code}: {msg or body_text or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            # Connection refused, DNS failure, etc. -- server not running.
            raise RuntimeError(
                f"Cannot reach Ollama server at {self._base_url}: "
                f"{e.reason}. Is it running? Start it with: ollama serve"
            ) from e

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Ollama returned malformed JSON from {url}: {e.msg}"
            ) from e

        embeddings = parsed.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError(
                f"Ollama response missing 'embeddings' field or returned "
                f"empty list. Response keys: {list(parsed.keys())}"
            )

        # Auto-detect dimensions from the first vector the first time we
        # successfully embed something. Subsequent calls keep the cached
        # value. If the user passed ``dim`` to the constructor we leave
        # it alone.
        if self._dim is None:
            self._dim = len(embeddings[0])

        return embeddings

    @property
    def dimensions(self) -> int:
        """Return the embedding vector dimensionality.

        Raises:
            RuntimeError: If dimensions have not been established yet --
                i.e., ``dim`` was not supplied to the constructor and
                ``embed()`` has not yet been called successfully.
                The ``AbstractEmbeddingProvider`` contract types this
                as ``int``, so we refuse to return ``None`` silently.
        """
        if self._dim is None:
            raise RuntimeError(
                "OllamaProvider: dimensions unknown -- call embed() "
                "first or pass dim=<n> to the constructor"
            )
        return self._dim

    @property
    def max_batch_size(self) -> int:
        """Conservative batch limit for local inference.

        Local forward-pass with hundreds of texts can OOM on modest
        hardware. Users with more GPU/RAM headroom can subclass and
        override this property.
        """
        return 32
