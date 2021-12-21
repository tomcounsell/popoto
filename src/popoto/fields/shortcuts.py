from .field import Field
from .key_field_mixin import KeyFieldMixin
from .auto_field_mixin import AutoFieldMixin
from .sorted_field_mixin import SortedFieldMixin


class IntField(Field):
    def __init__(self, *args, **kwargs):
        kwargs['type'] = int
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


