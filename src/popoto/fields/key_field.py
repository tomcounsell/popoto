from decimal import Decimal
from datetime import date, datetime, time
from .field import Field, logger
import uuid

from ..exceptions import ModelException
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

VALID_KEYFIELD_TYPES = [
    int, float, Decimal, str, bool, date, datetime, time,
]

class KeyField(Field):
    """
    A KeyField is for unique identifiers and category searches
    All KeyFields are used for indexing on the DB.
    All keys together have unique_together enforced.

    UniqueKeyField and AutoKeyField provide unique constraint and auto-id generation respectively.
    All models must have one or more KeyFields.
    However an AutoKeyField will be automatically added to models without any specified KeyFields.

    todo: add support for https://github.com/ai/nanoid
    """
    unique: bool = False
    indexed: bool = False
    auto: bool = False
    auto_uuid_length: int = 32
    auto_id: str = ""
    null: bool = False
    max_length: int = 128

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        keyfield_defaults = {
            'unique': False,
            'indexed': False,
            'auto': False,
            'auto_uuid_length': 32,
            'auto_id': "",
            'null': False,
            'max_length': 128,  # Redis limit is 512MB
        }
        self.field_defaults.update(keyfield_defaults)
        # set keyfield_options, let kwargs override
        for k, v in keyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
        if self.__class__ == KeyField and self.type not in VALID_KEYFIELD_TYPES:
            raise ModelException(f"{self.type} is not a valid KeyField type")

    def get_new_auto_key_value(self):
        return uuid.uuid4().hex[:self.auto_uuid_length]

    def set_auto_key_value(self, force: bool = False):
        if self.auto or force:
            self.default = self.get_new_auto_key_value()

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if not super().is_valid(field, value, null_check):
            return False
        if field.auto and len(value) != field.auto_uuid_length:
            logger.error(f"auto key value is length {len(value)}. It should be {field.auto_uuid_length}")
            return False
        return True

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value, pipeline=None, **kwargs):
        if not model_instance._meta.fields[field_name].auto:
            unique_set_key = f"{cls.get_special_use_field_db_key(model_instance, field_name)}:{field_value}"
            if pipeline:
                return pipeline.sadd(unique_set_key, model_instance.db_key)
            else:
                return POPOTO_REDIS_DB.sadd(unique_set_key, model_instance.db_key)

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline=None, **kwargs):
        if not model_instance._meta.fields[field_name].auto:
            unique_set_key = f"{cls.get_special_use_field_db_key(model_instance, field_name)}:{field_value}"
            if pipeline:
                return pipeline.srem(unique_set_key, model_instance.db_key)
            else:
                return POPOTO_REDIS_DB.srem(unique_set_key, model_instance.db_key)


    def get_filter_query_params(self, field_name: str) -> list:
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',    # takes a str, exact match :x:
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
            key_pattern = f"{model._meta.db_class_key}:"
            key_pattern += "*:" * (num_keys_before-1)
            key_pattern += query_value_pattern
            key_pattern += ":*" * num_keys_after
            return key_pattern

        redis_set_key_prefix = f"{model._meta.fields[field_name].get_special_use_field_db_key(model, field_name)}:"

        for query_param, query_value in query_params.items():

            if query_param.endswith('__in'):
                pipeline_2 = POPOTO_REDIS_DB.pipeline()
                for query_value_elem in query_value:
                    pipeline_2 = pipeline_2.smembers(redis_set_key_prefix + query_value_elem)
                    
                keys_lists_to_union = pipeline_2.execute()
                keys_lists_to_intersect.append(set.union(*[set(key_list) for key_list in keys_lists_to_union]))

            else:
                for char in "'?*^[]-/":
                    query_value = query_value.replace(char, f"/{char}")
                if query_param == f'{field_name}':
                    if model._meta.fields[field_name].auto:
                        keys_lists_to_intersect.append(POPOTO_REDIS_DB.keys(get_key_pattern(query_value)))
                    else:
                        keys_lists_to_intersect.append(POPOTO_REDIS_DB.smembers(redis_set_key_prefix + query_value))

                elif query_param.endswith('__isnull'):
                    if query_value is True:
                        keys_lists_to_intersect.append(POPOTO_REDIS_DB.smembers(redis_set_key_prefix + str(None)))
                    elif query_value is False:
                        key_pattern = get_key_pattern(f"[^None]")
                        pipeline = pipeline.keys(key_pattern)  # todo: refactor
                    else:
                        raise QueryException(f"{query_param} filter must be True or False")

                elif query_param.endswith('__startswith'):
                    key_pattern = get_key_pattern(f"{query_value}*")
                    pipeline = pipeline.keys(key_pattern)  # todo: refactor
                elif query_param.endswith('__endswith'):
                    key_pattern = get_key_pattern(f"*{query_value}")
                    pipeline = pipeline.keys(key_pattern)  # todo: refactor


            # todo: refactor to use HSCAN or sets, https://redis.io/commands/keys
            # https://redis-py.readthedocs.io/en/stable/index.html#redis.Redis.hscan_iter

        keys_lists_to_intersect += pipeline.execute()

        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(*[set(key_list) for key_list in keys_lists_to_intersect])
        return set()


class UniqueKeyField(KeyField):
    """
    UniqueKeyField() is equivalent to KeyField(unique=True)
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        uniquekeyfield_defaults = {
            'unique': True,
        }
        self.field_defaults.update(uniquekeyfield_defaults)
        # set keyfield_options, let kwargs override
        for k, v in uniquekeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

        if not kwargs.get('unique', True):
            from ..models.base import ModelException
            raise ModelException("UniqueKey field MUST be unique")


class AutoKeyField(UniqueKeyField):
    """
    AutoKeyField() is equivalent to KeyField(unique-True, auto=True)
    The AutoKeyField is an auto-generated, universally unique key
    It will be automatically added to models with no specified KeyFields
    Include this field in your model if you cannot otherwise enforce a unique-together constraint with other KeyFields.
    They auto-generated key is random and newly generated for a model instance.
    Model instances with otherwise identical properties are saved as separate instances with different auto-keys.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        autokeyfield_defaults = {
            'auto': True,
        }
        self.field_defaults.update(autokeyfield_defaults)
        # set keyfield_options, let kwargs override
        for k, v in autokeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
