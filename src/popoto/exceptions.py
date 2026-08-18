"""
Custom exceptions for the Popoto Redis ORM library.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .privacy.never_record import NeverRecordVerdict


class ModelException(Exception):
    """Base exception for all Popoto ORM model-related errors."""

    pass


class QueryException(Exception):
    """Raised when a query is malformed or produces an unexpected result."""

    pass


class PublisherException(Exception):
    """Raised when a publish operation fails."""

    pass


class SubscriberException(Exception):
    """Raised when a subscriber's message handler fails."""

    pass


class KeyMutationError(ModelException):
    """Raised when a KeyField value is changed after initial save.

    KeyField values form the Redis storage key (identity) of a model instance.
    Changing them silently would delete the old key and create a new one,
    potentially orphaning references. This exception prevents accidental
    identity changes.

    To intentionally migrate a key, use ``save(migrate_key=True)``.

    Example::

        instance = MyModel.query.get(name="old_name")
        instance.name = "new_name"
        instance.save()  # Raises KeyMutationError

        # Intentional migration:
        instance.save(migrate_key=True)  # Succeeds
    """

    pass


class AppendOnlyViolation(ModelException):
    """Raised by ``AppendOnlyMixin`` when a write would destroy a record (#560).

    Append-only models accept exactly one write per Redis key and no deletes.
    Every shape that would overwrite or remove an existing record raises this:
    re-saving a persisted instance, saving a fresh object whose key collides
    with a stored one, ``delete()``, ``delete_all()``, and
    ``save(migrate_key=True)`` (a key migration ``DELETE``s the old key, so it
    is a destroy dressed as a save).

    The message names the offending Redis key and never the record's content.
    That discipline is load-bearing and mirrors ``NeverRecordException``:
    exception text reaches plaintext log files, so quoting a value would leak
    it through a side channel.

    The documented escape hatch is ``AppendOnlyMixin.hard_delete(instance)``,
    which is retention/admin-only and greppable by design.
    """

    pass


class JournalBlockedError(ModelException):
    """Raised when a provenance-journal write is refused by the firewall (#560).

    ``ProvenanceJournal`` scans candidate content with
    :func:`popoto.privacy.never_record.scan_never_record` *before* it issues or
    queues a single Redis command, so a blocked capture or annotation raises
    rather than returning a sentinel. That matters most for annotations: a
    ``Model.save()`` blocked by the firewall returns the *pipeline* in pipeline
    mode, which is indistinguishable from success, and a caller that queued a
    supersede on top of it would close a target's validity interval against an
    annotation that was never written.

    Deliberately **not** a :class:`NeverRecordException` subclass. That class
    inherits :class:`SkipSaveException` so existing handlers silently swallow a
    filtered save; a journal caller must never silently lose a capture, so this
    error is outside that hierarchy and propagates.

    Carries a content-free ``verdict`` (reason code plus detector name) under
    the same no-quoting rule as :class:`NeverRecordException`.
    """

    def __init__(
        self, message: str, verdict: Optional["NeverRecordVerdict"] = None
    ) -> None:
        super().__init__(message)
        #: The blocking ``NeverRecordVerdict``, or ``None``. Content-free.
        self.verdict = verdict


class SkipSaveException(ModelException):
    """Raised by WriteFilterMixin to silently abort a save operation.

    When a model's compute_filter_score() returns a score below the
    minimum threshold, this exception is raised during pre_save to
    short-circuit persistence. The save() method catches it and returns
    without error.
    """

    pass


class NeverRecordException(SkipSaveException):
    """Raised by NeverRecordMixin when content must never be stored (#561).

    Subclasses SkipSaveException on purpose: every existing handler that
    already swallows a filtered save keeps working unchanged -- including
    ``Model.save()``'s own catch and the broad ``except Exception`` in
    ``SubconsciousMemory.extract_memories()`` -- while callers that need to
    tell a privacy drop apart from a salience skip can catch this subclass.

    The message carries only a reason code and detector name. It must never
    quote the matched text, an offset, or a length: exception messages reach
    plaintext log files (``MemoryService._record_failure`` writes
    ``f"{type(exc).__name__}: {exc}"`` to disk), so a quoted match would
    leak through a side channel the tombstone design closed.
    """

    pass
