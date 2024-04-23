from .field import Field
from datetime import datetime


class DatetimeField(Field):
    def __init__(self, *args, **kwargs):
        kwargs["type"] = datetime
        # Extract auto_now_add and auto_now before calling super
        self.auto_now_add = kwargs.pop("auto_now_add", False)
        self.auto_now = kwargs.pop("auto_now", False)
        super().__init__(*args, **kwargs)

    def format_value_pre_save(self, field_value):
        """
        If auto_now_add is True, set the field value to the current datetime when creating the instance.
        If auto_now is True, update the field value to the current datetime every time the instance is saved.
        """
        if self.auto_now_add and not field_value:
            return datetime.now()
        if self.auto_now:
            return datetime.now()
        return field_value
