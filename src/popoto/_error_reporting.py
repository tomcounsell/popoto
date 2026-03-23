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
_scope: Optional[object] = None  # sentry_sdk.Scope when active
_original_inits: dict = {}  # class -> original __init__

# Default DSN for the yudame/popoto Sentry project.
# This is a *public/client-side* key -- safe to embed in source code.
_DEFAULT_DSN = (
    "https://examplePublicKey@o0.ingest.sentry.io/0"
)


def _get_popoto_version() -> str:
    """Return the installed Popoto version string, or 'unknown'."""
    try:
        from importlib.metadata import version

        return version("popoto")
    except Exception:
        return "unknown"


def _before_send(event, hint):
    """Only send events whose traceback includes popoto frames."""
    try:
        if "exc_info" in hint:
            import traceback as _tb

            _, _, tb = hint["exc_info"]
            frames = _tb.extract_tb(tb)
            for frame in frames:
                if "popoto" in (frame.filename or ""):
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
        dsn: Optional override for the Sentry DSN. If not provided, the
            ``POPOTO_SENTRY_DSN`` environment variable is checked, then
            the built-in default DSN is used.

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
    global _enabled, _scope

    if _enabled:
        return

    try:
        import sentry_sdk  # noqa: F811
        from sentry_sdk import Client, Scope
    except ImportError:
        return

    resolved_dsn = dsn or os.environ.get("POPOTO_SENTRY_DSN") or _DEFAULT_DSN

    client = Client(
        dsn=resolved_dsn,
        default_integrations=False,
        auto_enabling_integrations=False,
        traces_sample_rate=0,
        before_send=_before_send,
        release=f"popoto@{_get_popoto_version()}",
    )

    scope = Scope()
    scope.set_client(client)
    scope.set_tag("popoto.version", _get_popoto_version())

    _scope = scope
    _enabled = True

    _patch_exceptions()


def capture_exception(exc: Optional[BaseException] = None) -> None:
    """Capture an exception and send it to the Popoto Sentry project.

    This is a no-op if error reporting has not been enabled or if any
    internal error occurs.
    """
    try:
        if not _enabled or _scope is None:
            return
        _scope.capture_exception(exc)
    except Exception:
        pass


def _patch_exceptions() -> None:
    """Monkey-patch Popoto exception ``__init__`` methods to auto-report."""
    try:
        from . import exceptions as _exc

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

            def _make_patched(orig, klass):
                def patched_init(self, *args, **kwargs):
                    orig(self, *args, **kwargs)
                    try:
                        capture_exception(self)
                    except Exception:
                        pass

                return patched_init

            _original_inits[cls] = original_init
            cls.__init__ = _make_patched(original_init, cls)
    except Exception:
        pass
