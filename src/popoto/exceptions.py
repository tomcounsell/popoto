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


class SkipSaveException(ModelException):
    """Raised by WriteFilterMixin to silently abort a save operation.

    When a model's compute_filter_score() returns a score below the
    minimum threshold, this exception is raised during pre_save to
    short-circuit persistence. The save() method catches it and returns
    without error.
    """

    pass
