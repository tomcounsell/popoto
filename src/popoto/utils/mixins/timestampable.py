from ...models.base import Model
from ...fields.datetime_field import DatetimeField


class Timestampable(Model):
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)
