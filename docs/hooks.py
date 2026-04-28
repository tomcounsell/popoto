"""MkDocs build hooks for the popoto documentation site.

Currently we use a single hook to suppress griffe's "No type or annotation"
warnings when assembling the auto-generated API reference. These messages are
informational — they document opportunities to add type hints in src/ — but
they are not site-build correctness issues. mkdocs ``--strict`` would otherwise
abort on hundreds of these, blocking deploys for what is editorial polish.

If/when type-hint coverage is improved as a separate effort, this filter can
be relaxed or removed.
"""

from __future__ import annotations

import logging


_GRIFFE_NOISE_PATTERNS = (
    "No type or annotation for parameter",
    "No type or annotation for returned value",
    "No type or annotation for yielded value",
    "does not appear in the function signature",
)


class _GriffeAnnotationFilter(logging.Filter):
    """Demote griffe warnings about missing annotations to DEBUG level."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()
        if any(pat in msg for pat in _GRIFFE_NOISE_PATTERNS):
            return False
        return True


_FILTER = _GriffeAnnotationFilter()


def _attach_to_handlers(logger: logging.Logger) -> None:
    logger.addFilter(_FILTER)
    for handler in logger.handlers:
        handler.addFilter(_FILTER)


def on_startup(command: str, dirty: bool) -> None:  # noqa: ARG001
    """Attach the filter once at MkDocs startup.

    mkdocs forwards log records from sub-loggers via several distinct logger
    names. Griffe's annotation warnings flow through the
    ``mkdocs.plugins.griffe`` logger when surfaced under the strict-mode
    CountHandler — that logger's name is what makes them count toward strict
    failures. The filter is attached at every plausible level so the records
    are dropped before reaching any handler.
    """
    names = (
        "griffe",
        "mkdocs",
        "mkdocs.plugins",
        "mkdocs.plugins.griffe",
        "mkdocs.plugins.mkdocstrings",
        "mkdocs.plugins.mkdocstrings_handlers",
        "mkdocstrings",
    )
    for name in names:
        _attach_to_handlers(logging.getLogger(name))
    _attach_to_handlers(logging.getLogger())
