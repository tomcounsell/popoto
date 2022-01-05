from .field import Field
from .key_field_mixin import KeyFieldMixin
from .auto_field_mixin import AutoFieldMixin
from .sorted_field_mixin import SortedFieldMixin


class IntField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = int
        super().__init__(**kwargs)


class FloatField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = float
        super().__init__(**kwargs)


class DecimalField(Field):
    def __init__(self, *args, **kwargs):
        from decimal import Decimal
        kwargs['type'] = Decimal
        super().__init__(**kwargs)


class StringField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = str
        super().__init__(**kwargs)


class BooleanField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = bool
        super().__init__(**kwargs)


class BytesField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = bytes
        super().__init__(**kwargs)


class ListField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = list
        super().__init__(**kwargs)


class DictField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = dict
        super().__init__(**kwargs)


class SetField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = set
        super().__init__(**kwargs)


class TupleField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = tuple
        super().__init__(**kwargs)


class DateField(Field):
    def __init__(self, *args, **kwargs):
        from datetime import date
        kwargs['type'] = date
        super().__init__(**kwargs)


class DatetimeField(Field):
    def __init__(self, *args, **kwargs):
        from datetime import datetime
        kwargs['type'] = datetime
        super().__init__(**kwargs)


class TimeField(Field):
    def __init__(self, *args, **kwargs):
        from datetime import time
        kwargs['type'] = time
        super().__init__(**kwargs)


class KeyField(KeyFieldMixin, Field):
    def __init__(self, *args, **kwargs):
        kwargs['key'] = True
        super().__init__(**kwargs)


class UniqueKeyField(KeyField):
    def __init__(self, *args, **kwargs):
        kwargs['unique'] = True
        kwargs['null'] = False
        super().__init__(**kwargs)


class AutoKeyField(AutoFieldMixin, UniqueKeyField):
    def __init__(self, *args, **kwargs):
        kwargs['auto'] = True
        super().__init__(**kwargs)


class SortedField(SortedFieldMixin, Field):
    def __init__(self, *args, **kwargs):
        kwargs['sorted'] = True
        super().__init__(**kwargs)


class SortedKeyField(SortedFieldMixin, KeyFieldMixin, Field):
    def __init__(self, *args, **kwargs):
        super().__init__(**kwargs)
