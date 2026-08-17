"""Deterministic privacy primitives for the agent-memory write path.

This package holds the never-record firewall (issue #561, module M2 of the
memory second wave): a deterministic pre-storage gate whose guarantee must
never rest on a model.

Public surface::

    from popoto.privacy import (
        NeverRecordMixin,
        NeverRecordVerdict,
        scan_never_record,
    )

``scan_never_record`` is a pure function with no ORM or Redis dependency, so
downstream modules (M3's auditable extraction) can run candidates through the
gate before an LLM ever sees them.
"""

from .never_record import (
    NEVER_RECORD_REASONS,
    NeverRecordMixin,
    NeverRecordVerdict,
    scan_never_record,
)

__all__ = [
    "NEVER_RECORD_REASONS",
    "NeverRecordMixin",
    "NeverRecordVerdict",
    "scan_never_record",
]
