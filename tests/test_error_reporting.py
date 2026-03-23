"""Tests for the opt-in error reporting module."""

from __future__ import annotations

import sys
from unittest import mock

import pytest


class TestEnableWithoutSentrySdk:
    """Verify enable_error_reporting is a silent no-op when sentry-sdk is absent."""

    def test_enable_no_sentry_sdk(self):
        """enable_error_reporting() must not raise when sentry-sdk is missing."""
        with mock.patch.dict(sys.modules, {"sentry_sdk": None}):
            from popoto._error_reporting import _do_enable

            _do_enable(dsn=None)

    def test_enable_returns_none(self):
        """The public function always returns None."""
        from popoto._error_reporting import enable_error_reporting

        result = enable_error_reporting()
        assert result is None

    def test_do_enable_silent_noop_without_dsn(self):
        """_do_enable silently returns when no DSN is available."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        old_default = mod._DEFAULT_DSN
        mod._enabled = False
        mod._client = None
        mod._DEFAULT_DSN = None

        try:
            mod._do_enable(dsn=None)

            assert mod._enabled is False
            assert mod._client is None
        finally:
            mod._DEFAULT_DSN = old_default
            mod._enabled = False
            mod._client = None


class TestEnableWithMockSentry:
    """Verify enable_error_reporting sets up the isolated client correctly."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

    def test_enable_creates_isolated_client(self):
        """Enabling creates a Client without calling sentry_sdk.init."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        from popoto._error_reporting import enable_error_reporting

        with mock.patch.object(
            sentry_sdk, "init", side_effect=AssertionError("init must not be called")
        ):
            enable_error_reporting(dsn="https://key@sentry.test/1")

        import popoto._error_reporting as mod

        assert mod._enabled is True
        assert mod._client is not None

    def test_enable_is_idempotent(self):
        """Calling enable_error_reporting() twice does not create a second client."""
        pytest.importorskip("sentry_sdk")

        from popoto._error_reporting import enable_error_reporting

        enable_error_reporting(dsn="https://key@sentry.test/1")
        import popoto._error_reporting as mod

        client1 = mod._client

        enable_error_reporting(dsn="https://key@sentry.test/1")
        assert mod._client is client1

    def test_patches_exception_classes(self):
        """After enabling, Popoto exception __init__ methods are patched."""
        pytest.importorskip("sentry_sdk")

        from popoto._error_reporting import enable_error_reporting

        enable_error_reporting(dsn="https://key@sentry.test/1")

        import popoto._error_reporting as mod
        from popoto.exceptions import (
            ModelException,
            PublisherException,
            QueryException,
            SubscriberException,
        )

        for cls in [
            ModelException,
            QueryException,
            PublisherException,
            SubscriberException,
        ]:
            assert cls in mod._original_inits

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None


class TestCaptureException:
    """Verify capture_exception behaves correctly."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

    def test_capture_noop_when_disabled(self):
        """capture_exception does nothing when reporting is not enabled."""
        from popoto._error_reporting import capture_exception

        capture_exception(RuntimeError("test"))

    def test_capture_none_does_not_raise(self):
        """capture_exception(None) must not raise."""
        from popoto._error_reporting import capture_exception

        capture_exception(None)

    def test_capture_sends_to_client(self):
        """When enabled, capture_exception forwards to the isolated client."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        # Use a real Client so event_from_exception has valid options,
        # but mock capture_event to verify it's called.
        real_client = sentry_sdk.Client(
            dsn="https://key@sentry.test/1",
            default_integrations=False,
            auto_enabling_integrations=False,
        )
        mod._enabled = True
        mod._client = real_client

        with mock.patch.object(real_client, "capture_event") as mock_capture:
            exc = RuntimeError("test error")
            try:
                raise exc
            except RuntimeError:
                mod.capture_exception(exc)

            mock_capture.assert_called_once()

        mod._enabled = False
        mod._client = None

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None


class TestExceptionAutoCapture:
    """Verify that raising patched exceptions triggers capture."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

    def test_raising_model_exception_triggers_capture(self):
        """After enabling, constructing a ModelException calls capture_exception."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod.enable_error_reporting(dsn="https://key@sentry.test/1")

        mock_client = mock.MagicMock()
        mock_client.options = mod._client.options if mod._client else {}
        mod._client = mock_client

        from popoto.exceptions import ModelException

        try:
            raise ModelException("test error")
        except ModelException:
            pass

        assert mock_client.capture_event.called

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None


class TestSkipSaveExceptionExcluded:
    """Verify that SkipSaveException does NOT trigger error reporting."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

    def test_skip_save_exception_not_reported(self):
        """SkipSaveException is control-flow and must not be captured."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod.enable_error_reporting(dsn="https://key@sentry.test/1")

        mock_client = mock.MagicMock()
        mock_client.options = mod._client.options if mod._client else {}
        mod._client = mock_client

        from popoto.exceptions import SkipSaveException

        try:
            raise SkipSaveException("below threshold")
        except SkipSaveException:
            pass

        mock_client.capture_event.assert_not_called()

    def test_model_exception_still_reported(self):
        """Regular ModelException is still captured after SkipSave exclusion."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod.enable_error_reporting(dsn="https://key@sentry.test/1")

        mock_client = mock.MagicMock()
        mock_client.options = mod._client.options if mod._client else {}
        mod._client = mock_client

        from popoto.exceptions import ModelException

        try:
            raise ModelException("real error")
        except ModelException:
            pass

        assert mock_client.capture_event.called

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None


class TestInternalFailureIsolation:
    """Verify that internal failures in the reporter never propagate."""

    def test_enable_survives_internal_error(self):
        """If _do_enable raises internally, enable_error_reporting swallows it."""
        from popoto._error_reporting import enable_error_reporting

        with mock.patch(
            "popoto._error_reporting._do_enable",
            side_effect=RuntimeError("boom"),
        ):
            enable_error_reporting()

    def test_capture_survives_client_error(self):
        """If client.capture_event raises, capture_exception swallows it."""
        import popoto._error_reporting as mod

        broken_client = mock.MagicMock()
        broken_client.options = {"max_value_length": 1024}
        broken_client.capture_event.side_effect = RuntimeError("broken")
        mod._enabled = True
        mod._client = broken_client

        # Must not raise
        mod.capture_exception(RuntimeError("test"))

        mod._enabled = False
        mod._client = None


class TestAppSentryUnaffected:
    """Verify that enabling Popoto reporting does not affect the app's Sentry."""

    def test_app_sentry_init_unaffected(self):
        """App can call sentry_sdk.init() before/after enable_error_reporting."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

        with mock.patch.object(sentry_sdk, "init") as mock_init:
            mod.enable_error_reporting(dsn="https://key@sentry.test/1")
            mock_init.assert_not_called()

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None


class TestBeforeSend:
    """Verify the before_send filter."""

    def test_filters_non_popoto_exceptions(self):
        """Events from non-popoto exception types are filtered out."""
        from popoto._error_reporting import _before_send

        result = _before_send(
            {"event": "data"}, {"exc_info": (ValueError, ValueError("x"), None)}
        )
        assert result is None

    def test_passes_popoto_exceptions(self):
        """Events from popoto exception types are passed through."""
        from popoto._error_reporting import _before_send
        from popoto.exceptions import ModelException

        result = _before_send(
            {"event": "data"},
            {"exc_info": (ModelException, ModelException("test"), None)},
        )
        assert result is not None

    def test_before_send_survives_bad_hint(self):
        """before_send returns None on malformed hints."""
        from popoto._error_reporting import _before_send

        result = _before_send({}, {})
        assert result is None

        result = _before_send({}, {"exc_info": (None, None, None)})
        assert result is None


class TestDsnResolution:
    """Verify DSN is resolved correctly from arguments, env, and default."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._client = None
        mod._original_inits.clear()

    def test_env_var_overrides_default(self):
        """POPOTO_SENTRY_DSN env var takes precedence over the default."""
        pytest.importorskip("sentry_sdk")

        env_dsn = "https://envkey@sentry.test/99"

        with mock.patch.dict("os.environ", {"POPOTO_SENTRY_DSN": env_dsn}):
            with mock.patch("sentry_sdk.Client") as mock_client:
                mock_client.return_value = mock.MagicMock()
                from popoto._error_reporting import _do_enable

                _do_enable(dsn=None)

                mock_client.assert_called_once()
                call_kwargs = mock_client.call_args
                assert call_kwargs[1]["dsn"] == env_dsn

    def test_explicit_dsn_overrides_env(self):
        """Explicit dsn argument takes precedence over env var."""
        pytest.importorskip("sentry_sdk")

        explicit_dsn = "https://explicit@sentry.test/42"

        with mock.patch.dict(
            "os.environ", {"POPOTO_SENTRY_DSN": "https://env@sentry.test/1"}
        ):
            with mock.patch("sentry_sdk.Client") as mock_client:
                mock_client.return_value = mock.MagicMock()
                from popoto._error_reporting import _do_enable

                _do_enable(dsn=explicit_dsn)

                call_kwargs = mock_client.call_args
                assert call_kwargs[1]["dsn"] == explicit_dsn

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._client = None
