"""
Opt-in error reporting for Popoto via an isolated Sentry client.

This module provides a way for Popoto users to automatically report
library-specific errors back to the Popoto maintainers. It is entirely
opt-in: nothing runs unless ``enable_error_reporting()`` is called, and
the feature degrades silently if ``sentry-sdk`` is not installed or any
internal error occurs.

The Sentry client is fully isolated from the application's own Sentry
configuration -- it never calls ``sentry_sdk.init()`` and never touches
the global scope.
"""

from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Module-level state (no sentry imports at module level)
# ---------------------------------------------------------------------------
_enabled: bool = False
_client: Optional[object] = None  # sentry_sdk.Client when active
_original_inits: dict = {}  # class -> original __init__

_DEFAULT_DSN: Optional[str] = (
    "https://d5b829d05d3693b45c9fe55f82d230c9"
    "@o4508986235682816.ingest.us.sentry.io/4511091961495552"
)


def _get_popoto_version() -> str:
    """Return the installed Popoto version string, or 'unknown'."""
    try:
        from importlib.metadata import version

        return version("popoto")
    except Exception:
        return "unknown"


def _before_send(event, hint):
    """Only send events that are Popoto exception types.

    Checks whether the exception class is defined in the ``popoto``
    package, ensuring we only report Popoto-specific errors.
    """
    try:
        if "exc_info" in hint:
            exc_type = hint["exc_info"][0]
            if exc_type is not None:
                module = getattr(exc_type, "__module__", "") or ""
                if module.startswith("popoto"):
                    return event
        return None
    except Exception:
        return None


def enable_error_reporting(dsn: Optional[str] = None) -> None:
    """Enable opt-in error reporting to the Popoto maintainers.

    When enabled, exceptions originating in Popoto code are reported to
    a Sentry project maintained by the Popoto authors. This helps
    identify and fix bugs that users encounter in the wild.

    **This is entirely opt-in.** You must call this function explicitly
    for any reporting to occur. If ``sentry-sdk`` is not installed, this
    function silently does nothing.

    The reporter uses an isolated Sentry client that does **not**
    interfere with your application's own ``sentry_sdk.init()`` or
    global Sentry configuration.

    Args:
        dsn: Optional Sentry DSN override. By default, errors are
            reported to the Popoto maintainers' Sentry project. Set
            ``POPOTO_SENTRY_DSN`` env var or pass a DSN here to
            redirect reports to your own project instead.

    Example::

        import popoto

        popoto.enable_error_reporting()
    """
    try:
        _do_enable(dsn)
    except Exception:
        # Never let reporter setup interfere with the library.
        pass


def _do_enable(dsn: Optional[str]) -> None:
    global _enabled, _client

    if _enabled:
        return

    try:
        from sentry_sdk import Client
    except ImportError:
        return

    resolved_dsn = dsn or os.environ.get("POPOTO_SENTRY_DSN") or _DEFAULT_DSN

    if not resolved_dsn:
        return  # No DSN available — silently skip error reporting

    _client = Client(
        dsn=resolved_dsn,
        default_integrations=False,
        auto_enabling_integrations=False,
        traces_sample_rate=0,
        before_send=_before_send,
        release=f"popoto@{_get_popoto_version()}",
        send_default_pii=False,
    )

    _enabled = True

    _patch_exceptions()


def capture_exception(exc: Optional[BaseException] = None) -> None:
    """Capture an exception and send it to the Popoto Sentry project.

    This is a no-op if error reporting has not been enabled or if any
    internal error occurs.
    """
    try:
        if not _enabled or _client is None:
            return
        from sentry_sdk.utils import event_from_exception

        if exc is not None:
            exc_info = (type(exc), exc, exc.__traceback__)
        else:
            import sys

            exc_info = sys.exc_info()

        event, hint = event_from_exception(exc_info, client_options=_client.options)
        _client.capture_event(event, hint=hint)
    except Exception:
        pass


def _patch_exceptions() -> None:
    """Monkey-patch Popoto exception ``__init__`` methods to auto-report."""
    try:
        from . import exceptions as _exc
        from .exceptions import SkipSaveException

        exception_classes = [
            _exc.ModelException,
            _exc.QueryException,
            _exc.PublisherException,
            _exc.SubscriberException,
        ]

        for cls in exception_classes:
            if cls in _original_inits:
                continue  # already patched
            original_init = cls.__init__

            def _make_patched(orig):
                def patched_init(self, *args, **kwargs):
                    orig(self, *args, **kwargs)
                    try:
                        # SkipSaveException is control-flow, not a real error
                        if not isinstance(self, SkipSaveException):
                            capture_exception(self)
                    except Exception:
                        pass

                return patched_init

            _original_inits[cls] = original_init
            cls.__init__ = _make_patched(original_init)
    except Exception:
        pass
