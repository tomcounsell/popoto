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


class FinanceException(Exception):
    """Base exception for all finance module errors."""

    pass
