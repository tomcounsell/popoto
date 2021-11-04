from time import time
from .field import Field
import uuid

from ..redis_db import POPOTO_REDIS_DB


class KeyField(Field):
    """
    A field the model will use to build the unique db key
    All keys together have unique_together enforced.
    If only one KeyField is on the model, it must be unique
    todo: add support for https://github.com/ai/nanoid
    """
    unique: bool = False
    indexed: bool = False
    auto: bool = False
    auto_uuid_length: int = 0
    auto_id: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        new_kwargs = {  # default
            'unique': True,
            'indexed': True,
            'auto': False,
            'auto_uuid_length': 32,
            'auto_id': "",
        }
        if kwargs.get('unique', None) == False:
            from ..models.base import ModelException
            raise ModelException("key field must be unique")
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

        if self.auto:
            self.default = uuid.uuid4().hex[:self.auto_uuid_length]

    def get_filter_query_params(self, field_name: str) -> list:
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',
            f'{field_name}__contains',  # takes a str, matches :*x*:
            f'{field_name}__startswith',  # takes a str, matches :x*:
            f'{field_name}__endswith',  # takes a str, matches :*x:
            f'{field_name}__in',  # takes a list, returns any matches
            # f'{field_name}__isnull',  # KeyFields can't be null anyway
        ]

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """

        redis_db_keys_lists = list()
        db_key_length, field_key_position = model._meta.db_key_length, model._meta.get_db_key_position(field_name)

        pipeline = POPOTO_REDIS_DB.pipeline()
        for query_param, query_value in query_params.items():
            num_keys_before = field_key_position - 1
            num_keys_after = db_key_length-(field_key_position+1)
            key_pattern = f"{model._meta.db_class_key}:" + ("*:"*num_keys_before) + query_value + ":*"*num_keys_after

            pipeline.keys(key_pattern)
            # todo: refactor to use HSCAN or sets, https://redis.io/commands/keys
            # https://redis-py.readthedocs.io/en/stable/index.html#redis.Redis.hscan_iter

        redis_db_keys_lists += pipeline.execute()
        from itertools import chain
        return set(chain(*redis_db_keys_lists))


class UniqueKeyField(KeyField):
    def __init__(self, **kwargs):
        kwargs['unique'] = True
        super().__init__(**kwargs)


class AutoKeyField(UniqueKeyField):
    def __init__(self, **kwargs):
        kwargs['auto'] = True
        super().__init__(**kwargs)
