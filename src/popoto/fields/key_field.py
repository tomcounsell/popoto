from .field import Field, logger
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
    auto_uuid_length: int = 32
    auto_id: str = ""
    null: bool = False
    max_length: int = 128

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        new_kwargs = {  # defaults
            'unique': True,
            'indexed': False,
            'auto': False,
            'auto_uuid_length': 32,
            'auto_id': "",
            'null': False,
            'max_length': 128,  # Redis limit is 512MB
        }
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

        if self.auto:
            self.default = uuid.uuid4().hex[:self.auto_uuid_length]

    @classmethod
    def is_valid(cls, field, value) -> bool:
        if not super().is_valid(field, value):
            return False
        if field.auto and len(value) != field.auto_uuid_length:
            logger.error(f"auto key value is length {len(value)}. It should be {field.auto_uuid_length}")
            return False
        return True

    def get_filter_query_params(self, field_name: str) -> list:
        return super().get_filter_query_params(field_name) + [
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
        db_key_length, field_key_position = model._meta.db_key_length, model._meta.get_db_key_position(field_name)

        pipeline = POPOTO_REDIS_DB.pipeline()

        def get_key_pattern(query_value_pattern):
            key_pattern = f"{model._meta.db_class_key}:"
            key_pattern += ("*:" * num_keys_before)
            key_pattern += query_value_pattern
            key_pattern += ":*" * num_keys_after
            return key_pattern

        for query_param, query_value in query_params.items():
            num_keys_before = field_key_position - 1
            num_keys_after = db_key_length-(field_key_position+1)

            if query_param.endswith('__in'):
                pipeline_2 = POPOTO_REDIS_DB.pipeline()
                for query_value_elem in query_value:
                    key_pattern = get_key_pattern(f"{query_value}")
                    pipeline_2 = pipeline_2.keys(key_pattern)
                keys_lists_to_union = pipeline_2.execute()
                keys_lists_to_intersect.append(set.union(*[set(key_list) for key_list in keys_lists_to_union]))

            else:
                if query_param == f'{field_name}':
                    key_pattern = get_key_pattern(f"{query_value}")
                elif query_param.endswith('__startswith'):
                    key_pattern = get_key_pattern(f"{query_value}*")
                elif query_param.endswith('__endswith'):
                    key_pattern = get_key_pattern(f"*{query_value}")

                pipeline = pipeline.keys(key_pattern)
            # todo: refactor to use HSCAN or sets, https://redis.io/commands/keys
            # https://redis-py.readthedocs.io/en/stable/index.html#redis.Redis.hscan_iter

        keys_lists_to_intersect += pipeline.execute()
        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(*[set(key_list) for key_list in keys_lists_to_intersect])
        return set()


class UniqueKeyField(KeyField):
    def __init__(self, **kwargs):
        if not kwargs.get('unique', True):
            from ..models.base import ModelException
            raise ModelException("UniqueKey field MUST be unique")
        kwargs['unique'] = True
        super().__init__(**kwargs)


class AutoKeyField(UniqueKeyField):
    def __init__(self, **kwargs):
        kwargs['auto'] = True
        super().__init__(**kwargs)
