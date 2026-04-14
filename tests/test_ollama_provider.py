"""Tests for OllamaProvider.

Tests cover:
- OllamaProvider is a subclass of AbstractEmbeddingProvider
- embed() with mocked HTTP response returns correct vectors
- embed([]) returns []
- dimensions property returns auto-detected value after first embed() call
- dimensions property returns constructor-supplied dim if given
- dimensions property raises RuntimeError before first embed() when no dim set
- max_batch_size returns 32
- URLError raises RuntimeError mentioning "ollama serve"
- HTTP error raises RuntimeError mentioning model name and "ollama pull"
- input_type parameter accepted but ignored
"""

import json
import os
import sys
import unittest.mock
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto.embeddings import AbstractEmbeddingProvider
from src.popoto.embeddings.ollama import OllamaProvider


def _make_response(embeddings):
    """Build a mock urllib response returning the given embeddings list."""
    body = json.dumps({"embeddings": embeddings}).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)
    return mock_resp


class TestOllamaProviderInterface:
    """OllamaProvider must conform to AbstractEmbeddingProvider."""

    def test_is_subclass_of_abstract(self):
        assert issubclass(OllamaProvider, AbstractEmbeddingProvider)

    def test_is_instantiable(self):
        provider = OllamaProvider()
        assert provider is not None

    def test_default_model(self):
        provider = OllamaProvider()
        assert provider._model == "nomic-embed-text"

    def test_default_base_url(self):
        provider = OllamaProvider()
        assert provider._base_url == "http://localhost:11434"

    def test_custom_model(self):
        provider = OllamaProvider(model="mxbai-embed-large")
        assert provider._model == "mxbai-embed-large"

    def test_custom_base_url(self):
        provider = OllamaProvider(base_url="http://myserver:11434")
        assert provider._base_url == "http://myserver:11434"

    def test_trailing_slash_stripped_from_base_url(self):
        provider = OllamaProvider(base_url="http://localhost:11434/")
        assert provider._base_url == "http://localhost:11434"


class TestOllamaProviderEmbed:
    """Test embed() with mocked HTTP."""

    def test_embed_returns_vectors(self):
        vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)):
            provider = OllamaProvider()
            result = provider.embed(["hello", "world"])
        assert result == vectors

    def test_embed_empty_list_returns_empty(self):
        provider = OllamaProvider()
        # No HTTP call should be made for empty input
        with unittest.mock.patch("urllib.request.urlopen") as mock_open:
            result = provider.embed([])
        assert result == []
        mock_open.assert_not_called()

    def test_embed_single_text(self):
        vectors = [[0.1, 0.2, 0.3, 0.4]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)):
            provider = OllamaProvider()
            result = provider.embed(["hello"])
        assert result == vectors

    def test_input_type_accepted_and_ignored(self):
        vectors = [[0.1, 0.2]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)) as mock_open:
            provider = OllamaProvider()
            result = provider.embed(["text"], input_type="document")
        assert result == vectors
        # Verify the HTTP body does NOT contain input_type
        call_args = mock_open.call_args
        request_obj = call_args[0][0]
        body = json.loads(request_obj.data.decode())
        assert "input_type" not in body

    def test_embed_posts_to_correct_endpoint(self):
        vectors = [[0.1]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)) as mock_open:
            provider = OllamaProvider(base_url="http://localhost:11434")
            provider.embed(["test"])
        request_obj = mock_open.call_args[0][0]
        assert request_obj.full_url == "http://localhost:11434/api/embed"

    def test_embed_sends_correct_payload(self):
        vectors = [[0.1, 0.2]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)) as mock_open:
            provider = OllamaProvider(model="nomic-embed-text")
            provider.embed(["hello", "world"])
        request_obj = mock_open.call_args[0][0]
        body = json.loads(request_obj.data.decode())
        assert body == {"model": "nomic-embed-text", "input": ["hello", "world"]}


class TestOllamaProviderDimensions:
    """Test the dimensions property."""

    def test_dimensions_raises_before_first_embed_when_no_dim(self):
        provider = OllamaProvider()
        with pytest.raises(RuntimeError, match="dimensions unknown"):
            _ = provider.dimensions

    def test_dimensions_auto_detected_after_embed(self):
        vectors = [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]]
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)):
            provider = OllamaProvider()
            provider.embed(["text"])
        assert provider.dimensions == 8

    def test_dimensions_from_constructor(self):
        provider = OllamaProvider(dim=768)
        assert provider.dimensions == 768

    def test_constructor_dim_skips_auto_detection(self):
        vectors = [[0.1, 0.2, 0.3]]  # 3-dim
        with unittest.mock.patch("urllib.request.urlopen", return_value=_make_response(vectors)):
            provider = OllamaProvider(dim=768)
            provider.embed(["text"])
        # Constructor dim takes precedence
        assert provider.dimensions == 768

    def test_dimensions_error_message_mentions_constructor(self):
        provider = OllamaProvider()
        with pytest.raises(RuntimeError, match="dim="):
            _ = provider.dimensions


class TestOllamaProviderMaxBatchSize:
    """Test max_batch_size property."""

    def test_max_batch_size_is_32(self):
        provider = OllamaProvider()
        assert provider.max_batch_size == 32


class TestOllamaProviderErrors:
    """Test error handling."""

    def test_url_error_raises_runtime_error_with_ollama_serve(self):
        import socket
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(reason="Connection refused"),
        ):
            provider = OllamaProvider()
            with pytest.raises(RuntimeError, match="ollama serve"):
                provider.embed(["test"])

    def test_http_error_model_not_found(self):
        error_body = json.dumps({"error": "model 'bad-model' not found"}).encode()
        http_error = urllib.error.HTTPError(
            url="http://localhost:11434/api/embed",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=unittest.mock.MagicMock(read=lambda: error_body),
        )
        # HTTPError.read() must return the error body
        http_error.read = unittest.mock.MagicMock(return_value=error_body)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=http_error):
            provider = OllamaProvider(model="bad-model")
            with pytest.raises(RuntimeError, match="bad-model"):
                provider.embed(["test"])

    def test_http_error_model_not_found_mentions_ollama_pull(self):
        error_body = json.dumps({"error": "model 'nomic-embed-text' not found"}).encode()
        http_error = urllib.error.HTTPError(
            url="http://localhost:11434/api/embed",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=unittest.mock.MagicMock(),
        )
        http_error.read = unittest.mock.MagicMock(return_value=error_body)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=http_error):
            provider = OllamaProvider()
            with pytest.raises(RuntimeError, match="ollama pull"):
                provider.embed(["test"])

    def test_http_error_generic(self):
        error_body = json.dumps({"error": "internal server error"}).encode()
        http_error = urllib.error.HTTPError(
            url="http://localhost:11434/api/embed",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=unittest.mock.MagicMock(),
        )
        http_error.read = unittest.mock.MagicMock(return_value=error_body)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=http_error):
            provider = OllamaProvider()
            with pytest.raises(RuntimeError, match="Ollama HTTP 500"):
                provider.embed(["test"])

    def test_url_error_mentions_base_url(self):
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(reason="Connection refused"),
        ):
            provider = OllamaProvider(base_url="http://myhost:11434")
            with pytest.raises(RuntimeError, match="myhost"):
                provider.embed(["test"])
