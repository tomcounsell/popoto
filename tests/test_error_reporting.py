"""Tests for the opt-in error reporting module."""

from __future__ import annotations

import sys
from unittest import mock

import pytest


class TestEnableWithoutSentrySdk:
    """Verify enable_error_reporting is a silent no-op when sentry-sdk is absent."""

    def test_enable_no_sentry_sdk(self):
        """enable_error_reporting() must not raise when sentry-sdk is missing."""
        # Temporarily hide sentry_sdk from the import system
        with mock.patch.dict(sys.modules, {"sentry_sdk": None}):
            from popoto._error_reporting import _do_enable

            # Should silently return without error
            _do_enable(dsn=None)

    def test_enable_returns_none(self):
        """The public function always returns None."""
        from popoto._error_reporting import enable_error_reporting

        result = enable_error_reporting()
        assert result is None


class TestEnableWithMockSentry:
    """Verify enable_error_reporting sets up the isolated client correctly."""

    def setup_method(self):
        """Reset module state before each test."""
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._scope = None
        mod._original_inits.clear()

    def test_enable_creates_isolated_client(self):
        """Enabling creates a Client + Scope without calling sentry_sdk.init."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        from popoto._error_reporting import enable_error_reporting

        with mock.patch.object(
            sentry_sdk, "init", side_effect=AssertionError("init must not be called")
        ):
            enable_error_reporting(dsn="https://key@sentry.test/1")

        import popoto._error_reporting as mod

        assert mod._enabled is True
        assert mod._scope is not None

    def test_enable_is_idempotent(self):
        """Calling enable_error_reporting() twice does not create a second client."""
        pytest.importorskip("sentry_sdk")

        from popoto._error_reporting import enable_error_reporting

        enable_error_reporting(dsn="https://key@sentry.test/1")
        import popoto._error_reporting as mod

        scope1 = mod._scope

        enable_error_reporting(dsn="https://key@sentry.test/1")
        assert mod._scope is scope1  # same object

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
        """Restore exception classes after patching."""
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._scope = None


class TestCaptureException:
    """Verify capture_exception behaves correctly."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._scope = None
        mod._original_inits.clear()

    def test_capture_noop_when_disabled(self):
        """capture_exception does nothing when reporting is not enabled."""
        from popoto._error_reporting import capture_exception

        # Should not raise
        capture_exception(RuntimeError("test"))

    def test_capture_none_does_not_raise(self):
        """capture_exception(None) must not raise."""
        from popoto._error_reporting import capture_exception

        capture_exception(None)

    def test_capture_sends_to_scope(self):
        """When enabled, capture_exception forwards to the isolated scope."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mock_scope = mock.MagicMock()
        mod._enabled = True
        mod._scope = mock_scope

        exc = RuntimeError("test error")
        mod.capture_exception(exc)

        mock_scope.capture_exception.assert_called_once_with(exc)

        # cleanup
        mod._enabled = False
        mod._scope = None

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._scope = None


class TestExceptionAutoCapture:
    """Verify that raising patched exceptions triggers capture."""

    def setup_method(self):
        import popoto._error_reporting as mod

        mod._enabled = False
        mod._scope = None
        mod._original_inits.clear()

    def test_raising_model_exception_triggers_capture(self):
        """After enabling, constructing a ModelException calls capture_exception."""
        pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod.enable_error_reporting(dsn="https://key@sentry.test/1")

        mock_scope = mock.MagicMock()
        mod._scope = mock_scope

        from popoto.exceptions import ModelException

        try:
            raise ModelException("test error")
        except ModelException:
            pass

        # The patched __init__ should have called capture_exception
        assert mock_scope.capture_exception.called

    def teardown_method(self):
        import popoto._error_reporting as mod

        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._scope = None


class TestInternalFailureIsolation:
    """Verify that internal failures in the reporter never propagate."""

    def test_enable_survives_internal_error(self):
        """If _do_enable raises internally, enable_error_reporting swallows it."""
        from popoto._error_reporting import enable_error_reporting

        with mock.patch(
            "popoto._error_reporting._do_enable",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise
            enable_error_reporting()

    def test_capture_survives_scope_error(self):
        """If scope.capture_exception raises, capture_exception swallows it."""
        import popoto._error_reporting as mod

        broken_scope = mock.MagicMock()
        broken_scope.capture_exception.side_effect = RuntimeError("broken")
        mod._enabled = True
        mod._scope = broken_scope

        # Must not raise
        mod.capture_exception(RuntimeError("test"))

        mod._enabled = False
        mod._scope = None


class TestAppSentryUnaffected:
    """Verify that enabling Popoto reporting does not affect the app's Sentry."""

    def test_app_sentry_init_unaffected(self):
        """App can call sentry_sdk.init() before/after enable_error_reporting."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        import popoto._error_reporting as mod

        mod._enabled = False
        mod._scope = None
        mod._original_inits.clear()

        # Simulate app's own sentry_sdk.init
        # We just verify enable_error_reporting doesn't call sentry_sdk.init
        with mock.patch.object(sentry_sdk, "init") as mock_init:
            mod.enable_error_reporting(dsn="https://key@sentry.test/1")
            mock_init.assert_not_called()

        # cleanup
        for cls, orig in mod._original_inits.items():
            cls.__init__ = orig
        mod._original_inits.clear()
        mod._enabled = False
        mod._scope = None


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
        mod._scope = None
        mod._original_inits.clear()

    def test_env_var_overrides_default(self):
        """POPOTO_SENTRY_DSN env var takes precedence over the default."""
        pytest.importorskip("sentry_sdk")

        env_dsn = "https://envkey@sentry.test/99"

        with mock.patch.dict("os.environ", {"POPOTO_SENTRY_DSN": env_dsn}):
            with mock.patch("sentry_sdk.Client") as mock_client:
                mock_client.return_value = mock.MagicMock()
                with mock.patch("sentry_sdk.Scope") as mock_scope_cls:
                    mock_scope_cls.return_value = mock.MagicMock()
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
                with mock.patch("sentry_sdk.Scope") as mock_scope_cls:
                    mock_scope_cls.return_value = mock.MagicMock()
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
        mod._scope = None
