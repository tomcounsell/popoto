from .field import Field
from .key_field_mixin import KeyFieldMixin
from .auto_field_mixin import AutoFieldMixin
from .sorted_field_mixin import SortedFieldMixin
from ..exceptions import ModelException


class IntField(Field):
    """A Field that stores ``int`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = int
        super().__init__(**kwargs)


class FloatField(Field):
    """A Field that stores ``float`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = float
        super().__init__(**kwargs)


class DecimalField(Field):
    """A Field that stores ``Decimal`` values."""

    def __init__(self, *args, **kwargs):
        from decimal import Decimal

        kwargs["type"] = Decimal
        super().__init__(**kwargs)


class StringField(Field):
    """A Field that stores ``str`` values (same as the base ``Field`` default)."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = str
        super().__init__(**kwargs)


class BooleanField(Field):
    """A Field that stores ``bool`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = bool
        super().__init__(**kwargs)


class BytesField(Field):
    """A Field that stores ``bytes`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = bytes
        super().__init__(**kwargs)


class ListField(Field):
    """A Field that stores ``list`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = list
        super().__init__(**kwargs)


class DictField(Field):
    """A Field that stores ``dict`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = dict
        super().__init__(**kwargs)


class SetField(Field):
    """A Field that stores ``set`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = set
        super().__init__(**kwargs)


class TupleField(Field):
    """A Field that stores ``tuple`` values."""

    def __init__(self, *args, **kwargs):
        kwargs["type"] = tuple
        super().__init__(**kwargs)


class DateField(Field):
    """A Field that stores ``datetime.date`` values."""

    def __init__(self, *args, **kwargs):
        from datetime import date

        kwargs["type"] = date
        super().__init__(**kwargs)


class TimeField(Field):
    """A Field that stores ``datetime.time`` values."""

    def __init__(self, *args, **kwargs):
        from datetime import time

        kwargs["type"] = time
        super().__init__(**kwargs)


class KeyField(KeyFieldMixin, Field):
    """A field that forms part of the model's Redis key.

    All KeyField values together enforce a unique-together constraint.
    Supports filter lookups: exact, ``__isnull``, ``__contains``,
    ``__startswith``, ``__endswith``, ``__in``.
    """

    def __init__(self, *args, **kwargs):
        kwargs["key"] = True
        super().__init__(**kwargs)


class UniqueKeyField(KeyField):
    """A KeyField with a per-value uniqueness constraint. Cannot be null."""

    def __init__(self, *args, **kwargs):
        if kwargs.get("unique") is False:
            raise ModelException(f"you may not set unique=False on this field type")
        kwargs["unique"] = True
        if kwargs.get("null") is True:
            raise ModelException(f"you may not set null=True on this field type")
        kwargs["null"] = False
        super().__init__(**kwargs)


class AutoKeyField(AutoFieldMixin, UniqueKeyField):
    """A UniqueKeyField whose value is auto-generated (UUID-based).

    Automatically added to models that define no KeyField of their own.
    """

    def __init__(self, *args, **kwargs):
        if kwargs.get("unique") is False:
            raise ModelException(f"you may not set unique=False on this field type")
        if kwargs.get("null") is True:
            raise ModelException(f"you may not set null=True on this field type")
        kwargs["auto"] = True
        super().__init__(**kwargs)


class SortedField(SortedFieldMixin, Field):
    """A field backed by a Redis sorted set for fast range queries.

    Supports filter lookups: ``__gt``, ``__gte``, ``__lt``, ``__lte``.
    Must be a numeric type (int, float, Decimal, date, or datetime).
    """

    def __init__(self, *args, **kwargs):
        kwargs["sorted"] = True
        super().__init__(**kwargs)


class SortedKeyField(SortedFieldMixin, KeyFieldMixin, Field):
    """A field that is both a KeyField and a SortedField."""

    def __init__(self, *args, **kwargs):
        super().__init__(**kwargs)
