"""
Custom exceptions for the Popoto Redis ORM library.
"""


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


class SkipSaveException(ModelException):
    """Raised by WriteFilterMixin to silently abort a save operation.

    When a model's compute_filter_score() returns a score below the
    minimum threshold, this exception is raised during pre_save to
    short-circuit persistence. The save() method catches it and returns
    without error.
    """

    pass
