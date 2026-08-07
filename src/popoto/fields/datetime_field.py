"""
Datetime field for storing Python datetime objects in Redis.

This module provides DatetimeField, a specialized field type that extends the base
Field class to handle datetime values with automatic timestamp management. It follows
Django's familiar auto_now and auto_now_add patterns, making it intuitive for developers
transitioning from Django ORM to Popoto's Redis-based persistence.

The DatetimeField is central to the Timestampable mixin pattern, enabling models to
automatically track creation and modification times without manual intervention.

Example usage:
    class Article(popoto.Model):
        title = popoto.KeyField()
        published_at = popoto.DatetimeField(null=True)
        created_at = popoto.DatetimeField(auto_now_add=True)
        updated_at = popoto.DatetimeField(auto_now=True)

Design note: Unlike shortcuts.py fields (DateField, TimeField) which are simple type
wrappers, DatetimeField lives in its own module because it introduces behavioral logic
(auto_now/auto_now_add) beyond just type enforcement.
"""

from .field import Field
from datetime import datetime, timezone


class DatetimeField(Field):
    """
    A field for storing datetime values with optional automatic timestamp management.

    DatetimeField provides Django-compatible auto_now and auto_now_add functionality
    for Redis-persisted models. This design decision prioritizes developer familiarity
    over Redis-native timestamp patterns, reducing cognitive overhead when migrating
    from or working alongside Django projects.

    The field automatically handles serialization through msgpack (via the parent
    Field class), storing datetime objects as ISO format strings in Redis.

    Args:
        auto_now_add: Set to current datetime on first save only.
        auto_now: Update to current datetime on every save.

    Attributes:
        auto_now_add (bool): When True, automatically sets the field to the current
            datetime on first save only. Useful for created_at timestamps.
        auto_now (bool): When True, automatically updates the field to the current
            datetime on every save. Useful for updated_at timestamps.

    Example:
        # Track when a user was created and last modified
        class User(popoto.Model):
            username = popoto.KeyField()
            created_at = popoto.DatetimeField(auto_now_add=True)
            last_seen = popoto.DatetimeField(auto_now=True)

        # Or use the built-in Timestampable mixin for the common pattern
        from popoto.utils.mixins import Timestampable

        class User(Timestampable):
            username = popoto.KeyField()
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize a DatetimeField with optional automatic timestamp behavior.

        The auto_now_add and auto_now parameters are extracted before calling the
        parent constructor because they are DatetimeField-specific behaviors not
        recognized by the base Field class.

        Args:
            auto_now_add (bool, optional): If True, set to current datetime on
                creation only. Defaults to False.
            auto_now (bool, optional): If True, set to current datetime on every
                save. Defaults to False.
            **kwargs: Additional Field parameters (null, default, etc.).

        Note:
            Setting both auto_now_add=True and auto_now=True is redundant since
            auto_now will overwrite the value on every save including the first.
        """
        kwargs["type"] = datetime
        # Extract auto_now_add and auto_now before calling super
        self.auto_now_add = kwargs.pop("auto_now_add", False)
        self.auto_now = kwargs.pop("auto_now", False)
        super().__init__(*args, **kwargs)

    def format_value_pre_save(self, field_value, skip_auto_now=False, **kwargs):
        """
        Apply automatic timestamp logic before persisting to Redis.

        This method is called by the model's save pipeline, allowing DatetimeField
        to intercept and modify the value based on auto_now and auto_now_add settings.
        The hook pattern (format_value_pre_save) enables field-specific transformation
        logic without coupling to the model's save implementation.

        Args:
            field_value: The current datetime value, or None if not set.
            skip_auto_now: If True, suppress auto_now timestamp updates.
                Useful for data migrations where existing timestamps should
                be preserved. Does not affect auto_now_add behavior.
            **kwargs: Additional keyword arguments for forward compatibility.

        Returns:
            datetime: The processed datetime value. Returns the current UTC time
                for auto_now fields (unless skip_auto_now is True), the current
                UTC time for auto_now_add when field_value is falsy, or the
                original field_value otherwise.

        Implementation note:
            Timestamps are stamped in UTC via ``datetime.now(timezone.utc)`` so
            that auto_now/auto_now_add fields record correct instants regardless
            of host timezone. Since #521 the encoder preserves the offset, so
            these values read back as aware UTC and consumers no longer need to
            re-attach it. (Rows written before #521 still read back naive; their
            offset was never stored.) ``timezone.utc`` is used rather than
            ``datetime.UTC`` to stay valid on Python 3.10 (the ``requires-python``
            floor); ``datetime.UTC`` is 3.11+.

            auto_now takes precedence if both flags are set, as it unconditionally
            returns the current UTC time regardless of existing value (unless
            skipped).
        """
        if self.auto_now_add and not field_value:
            return datetime.now(timezone.utc)
        if self.auto_now and not skip_auto_now:
            return datetime.now(timezone.utc)
        return field_value
