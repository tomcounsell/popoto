from decimal import Decimal
from datetime import date, datetime, time
import redis.client
import logging
from ..models.db_key import DB_key

logger = logging.getLogger('POPOTO.KeyFieldMixin')

from ..exceptions import ModelException
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

VALID_KEYFIELD_TYPES = [
    int, float, Decimal, str, bool, date, datetime, time,
]


class KeyFieldMixin:
    """
    A KeyField is for unique identifiers and category searches
    All KeyFields are used for indexing on the DB.
    All keys together have unique_together enforced.

    UniqueKeyField and AutoKeyField provide unique constraint and auto-id generation respectively.
    All models must have one or more KeyFields.
    However an AutoKeyField will be automatically added to models without any specified KeyFields.
    """
    key: bool = True
    max_length: int = 128

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        keyfield_defaults = {
            'key': True,
            'max_length': 128,  # Redis limit is 512MB
        }
        self.field_defaults.update(keyfield_defaults)
        # set field options, let kwargs override
        for k, v in keyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
        if self.key and self.type not in VALID_KEYFIELD_TYPES:
            raise ModelException(f"{self.type} is not a valid KeyField type")

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if not super().is_valid(field, value, null_check):
            return False
        return True

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None,
                **kwargs):
        if model_instance._meta.fields[field_name].auto:
            return pipeline if pipeline else None

        unique_set_key = DB_key(cls.get_special_use_field_db_key(model_instance, field_name), field_value)
        if pipeline:
            return pipeline.sadd(unique_set_key.redis_key, model_instance.db_key.redis_key)
        else:
            return POPOTO_REDIS_DB.sadd(unique_set_key.redis_key, model_instance.db_key.redis_key)

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None,
                  **kwargs):
        if model_instance._meta.fields[field_name].auto:
            return pipeline if pipeline else None

        unique_set_key = DB_key(cls.get_special_use_field_db_key(model_instance, field_name), field_value)
        if pipeline:
            return pipeline.srem(unique_set_key.redis_key, model_instance.db_key.redis_key)
        else:
            return POPOTO_REDIS_DB.srem(unique_set_key.redis_key, model_instance.db_key.redis_key)

    def get_filter_query_params(self, field_name: str) -> list:
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',  # takes a str, exact match :x:
            f'{field_name}__contains',  # takes a str, matches :*x*:
            f'{field_name}__startswith',  # takes a str, matches :x*:
            f'{field_name}__endswith',  # takes a str, matches :*x:
            f'{field_name}__in',  # takes a list, returns any matches
        ]

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """

        keys_lists_to_intersect = list()
        db_key_length, field_key_position = model._meta.db_key_length, model._meta.get_db_key_index_position(field_name)
        num_keys_before = field_key_position
        num_keys_after = db_key_length - field_key_position - 1

        pipeline = POPOTO_REDIS_DB.pipeline()

        def get_key_pattern(query_value_pattern):
            key_pattern = model._meta.db_class_key.redis_key + ":"
            key_pattern += "*:" * (num_keys_before - 1)
            key_pattern += query_value_pattern
            key_pattern += ":*" * num_keys_after
            return key_pattern

        redis_set_key_prefix = model._meta.fields[field_name].get_special_use_field_db_key(model, field_name)

        for query_param, query_value in query_params.items():

            if query_param.endswith('__in'):
                pipeline_2 = POPOTO_REDIS_DB.pipeline()
                for query_value_elem in query_value:
                    pipeline_2 = pipeline_2.smembers(DB_key(redis_set_key_prefix, query_value_elem).redis_key)
                keys_lists_to_union = pipeline_2.execute()
                keys_lists_to_intersect.append(set.union(*[set(key_list) for key_list in keys_lists_to_union]))

            else:
                if query_param == f'{field_name}':
                    if model._meta.fields[field_name].auto:
                        # todo: refactor or show warning or depricate ability
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.keys(get_key_pattern(query_value))
                        )
                    else:
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.smembers(DB_key(redis_set_key_prefix, query_value).redis_key)
                        )

                elif query_param.endswith('__isnull'):
                    if query_value is True:
                        keys_lists_to_intersect.append(
                            POPOTO_REDIS_DB.smembers(DB_key(redis_set_key_prefix, None).redis_key)
                        )
                    elif query_value is False:
                        pipeline = pipeline.keys(get_key_pattern(f"[^None]"))  # todo: refactor
                    else:
                        raise QueryException(f"{query_param} filter must be True or False")

                elif query_param.endswith('__startswith'):
                    pipeline = pipeline.keys(get_key_pattern(f"{DB_key.clean(query_value)}*"))  # todo: refactor
                elif query_param.endswith('__endswith'):
                    pipeline = pipeline.keys(get_key_pattern(f"*{DB_key.clean(query_value)}"))  # todo: refactor

            # todo: refactor to use HSCAN or sets, https://redis.io/commands/keys
            # https://redis-py.readthedocs.io/en/stable/index.html#redis.Redis.hscan_iter

        keys_lists_to_intersect += pipeline.execute()
        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(*[set(key_list) for key_list in keys_lists_to_intersect])
        return set()
