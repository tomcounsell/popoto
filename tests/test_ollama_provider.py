"""Tests for OllamaProvider.

Tests cover:
- OllamaProvider is a subclass of AbstractEmbeddingProvider
- embed() POSTs the correct JSON payload and parses the response
- embed([]) returns []
- dimensions are auto-detected on first embed() call
- dimensions honour the constructor dim= argument
- dimensions raises RuntimeError when unknown (no silent None)
- max_batch_size returns 32 (conservative for local inference)
- URLError (Ollama not running) raises RuntimeError mentioning "ollama serve"
- HTTPError with "model not found" body raises RuntimeError mentioning
  "ollama pull <model>"
- HTTPError with other body raises a generic RuntimeError with status code
- input_type is accepted but ignored

All HTTP calls are mocked via unittest.mock.patch on
``urllib.request.urlopen`` -- no real Ollama server required.
"""

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import urllib.error

import pytest

from src.popoto.embeddings import AbstractEmbeddingProvider
from src.popoto.embeddings.ollama import OllamaProvider


def _mock_urlopen_response(payload: dict) -> MagicMock:
    """Build a MagicMock that behaves like urlopen()'s context manager."""
    body = json.dumps(payload).encode("utf-8")
    fake_resp = MagicMock()
    fake_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = fake_resp
    ctx.__exit__.return_value = False
    return ctx


class TestSubclass:
    def test_is_subclass_of_abstract_provider(self):
        assert issubclass(OllamaProvider, AbstractEmbeddingProvider)

    def test_can_instantiate(self):
        # No network call happens in __init__, so this must succeed even
        # without a running Ollama server.
        provider = OllamaProvider()
        assert provider is not None


class TestEmbedHappyPath:
    def test_embed_returns_vectors_from_response(self):
        provider = OllamaProvider()
        fake_vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response({"embeddings": fake_vectors})
            out = provider.embed(["hello", "world"])
        assert out == fake_vectors

    def test_embed_posts_correct_payload(self):
        provider = OllamaProvider(model="my-model")
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response(
                {"embeddings": [[1.0, 2.0]]}
            )
            provider.embed(["hi"])
        # The first positional arg to urlopen is a Request object.
        req = m.call_args[0][0]
        assert req.full_url == "http://localhost:11434/api/embed"
        assert req.get_method() == "POST"
        sent = json.loads(req.data.decode("utf-8"))
        assert sent == {"model": "my-model", "input": ["hi"]}

    def test_embed_honours_custom_base_url(self):
        provider = OllamaProvider(base_url="http://remote:9999/")
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response(
                {"embeddings": [[0.0]]}
            )
            provider.embed(["x"])
        req = m.call_args[0][0]
        # Trailing slash must be normalised.
        assert req.full_url == "http://remote:9999/api/embed"

    def test_embed_empty_list_returns_empty(self):
        provider = OllamaProvider()
        # No HTTP call should be made -- shortcut before networking.
        with patch("urllib.request.urlopen") as m:
            out = provider.embed([])
        assert out == []
        m.assert_not_called()

    def test_input_type_accepted_but_ignored(self):
        provider = OllamaProvider()
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response(
                {"embeddings": [[0.1, 0.2]]}
            )
            provider.embed(["hi"], input_type="query")
            provider.embed(["hi"], input_type="document")
        # Both calls should have gone through -- input_type was ignored,
        # not passed to Ollama.
        assert m.call_count == 2
        for call in m.call_args_list:
            sent = json.loads(call[0][0].data.decode("utf-8"))
            assert "input_type" not in sent


class TestDimensions:
    def test_dimensions_auto_detected_after_first_call(self):
        provider = OllamaProvider()
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response(
                {"embeddings": [[0.1] * 768]}
            )
            provider.embed(["hi"])
        assert provider.dimensions == 768

    def test_dimensions_from_constructor_dim(self):
        provider = OllamaProvider(dim=1024)
        # Available immediately without any embed() call.
        assert provider.dimensions == 1024

    def test_dimensions_raises_when_unknown(self):
        provider = OllamaProvider()
        with pytest.raises(RuntimeError, match="dimensions unknown"):
            _ = provider.dimensions

    def test_dimensions_cached_across_calls(self):
        provider = OllamaProvider()
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response(
                {"embeddings": [[0.1] * 384]}
            )
            provider.embed(["a"])
            provider.embed(["b"])
        assert provider.dimensions == 384


class TestMaxBatchSize:
    def test_max_batch_size_is_32(self):
        provider = OllamaProvider()
        # Conservative default for local inference (see plan C3).
        assert provider.max_batch_size == 32


class TestConnectionErrors:
    def test_url_error_raises_with_ollama_serve_hint(self):
        provider = OllamaProvider()
        with patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("Connection refused")
            with pytest.raises(RuntimeError, match="ollama serve"):
                provider.embed(["hi"])

    def test_url_error_mentions_base_url(self):
        provider = OllamaProvider(base_url="http://custom:1234")
        with patch("urllib.request.urlopen") as m:
            m.side_effect = urllib.error.URLError("refused")
            with pytest.raises(RuntimeError, match="http://custom:1234"):
                provider.embed(["hi"])


class TestHttpErrors:
    def _http_error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="http://localhost:11434/api/embed",
            code=code,
            msg="error",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

    def test_model_not_found_raises_with_pull_hint(self):
        provider = OllamaProvider(model="mystery-model")
        err = self._http_error(
            404, json.dumps({"error": "model 'mystery-model' not found"}).encode()
        )
        with patch("urllib.request.urlopen") as m:
            m.side_effect = err
            with pytest.raises(RuntimeError) as exc_info:
                provider.embed(["hi"])
        msg = str(exc_info.value)
        assert "mystery-model" in msg
        assert "ollama pull mystery-model" in msg

    def test_generic_http_error_reports_status_code(self):
        provider = OllamaProvider()
        err = self._http_error(500, json.dumps({"error": "oops"}).encode())
        with patch("urllib.request.urlopen") as m:
            m.side_effect = err
            with pytest.raises(RuntimeError, match="Ollama HTTP 500"):
                provider.embed(["hi"])

    def test_http_error_with_empty_body(self):
        provider = OllamaProvider()
        err = self._http_error(503, b"")
        with patch("urllib.request.urlopen") as m:
            m.side_effect = err
            with pytest.raises(RuntimeError, match="Ollama HTTP 503"):
                provider.embed(["hi"])


class TestMalformedResponse:
    def test_malformed_json_raises_runtime_error(self):
        provider = OllamaProvider()
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"not json at all"
        ctx = MagicMock()
        ctx.__enter__.return_value = fake_resp
        ctx.__exit__.return_value = False
        with patch("urllib.request.urlopen") as m:
            m.return_value = ctx
            with pytest.raises(RuntimeError, match="malformed JSON"):
                provider.embed(["hi"])

    def test_missing_embeddings_field_raises(self):
        provider = OllamaProvider()
        with patch("urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen_response({"foo": "bar"})
            with pytest.raises(RuntimeError, match="missing 'embeddings'"):
                provider.embed(["hi"])
