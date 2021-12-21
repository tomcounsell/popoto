import logging
from decimal import Decimal
import datetime
import typing
import redis

from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB
logger = logging.getLogger('POPOTO.SortedFieldMixin')


class SortedFieldMixin:
    """
        The SortedField enables fast queries for ordering or filter by value range.
        Examples:
            Toys.query.filter(price__lte=4.99)
            DairyProduct.query.filter(best_before_date__gte=datetime.now())
        Requirements:
            Must be numeric type (int, float, Decimal, date, datetime)
            Null values not allowed. Can set a default.
    """
    type: type = float
    null: bool = False
    default = ""
    partition_on = tuple()

    def __init__(self, **kwargs):
        super().__init__()
        sortedfield_defaults = {
            'type': float,
            'null': False,
            'default': None,  # cannot set a default for datetime, so no type gets a default
            'partition_on': tuple(),
        }
        self.field_defaults.update(sortedfield_defaults)

        # set field_options, let kwargs override
        for k, v in sortedfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

        if isinstance(self.partition_on, str):
            self.partition_on = tuple((self.partition_on,))

        elif self.partition_on and not isinstance(self.partition_on, tuple):
            from ..models.base import ModelException
            raise ModelException("partition_on must be str or tuple of str field names")

        # todo: move this to field init validation
        if self.null is not False:
            from ..models.base import ModelException
            raise ModelException("SortedField cannot be null")
            # todo: allow null in SortedField. null removes instance from SortedSet
            # todo: if allow null, how to filter by null value? use extra index set just for nulls?

    def get_filter_query_params(self, field_name):
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',
            f'{field_name}__gt',
            f'{field_name}__gte',
            f'{field_name}__lt',
            f'{field_name}__lte',
            # f'{field_name}__min',
            # f'{field_name}__max',
            # f'{field_name}__range',  # todo: like https://docs.djangoproject.com/en/3.2/ref/models/querysets/#range
            # f'{field_name}__isnull',  # todo: see todo in __init__
        ] + [
            f'{keyfield_name}' for keyfield_name in self.partition_on
        ]

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if not super().is_valid(field, value, null_check):
            return
        if value and not isinstance(value, field.type):
            return False
        return True

    @classmethod
    def format_value_pre_save(cls, field_value):
        if cls.type in [int, float, datetime.datetime, datetime.date, datetime.time]:
            return field_value
        else:
            return float(field_value)

    @classmethod
    def convert_to_numeric(cls, field, field_value):
        if field.type in [int, float]:
            return field_value
        elif field.type is Decimal:
            return float(field_value)
        elif field.type is datetime.date:
            return field_value.toordinal()
        elif field.type is datetime.datetime:
            return field_value.timestamp()
        elif field.type is datetime.time:
            return field_value.timestamp()
        else:
            raise ValueError("SortedField received non-numeric value.")

    @classmethod
    def get_sortedset_db_key(cls, model, field_name):
        return cls.get_special_use_field_db_key(model, field_name)

    @classmethod
    def get_partitioned_sortedset_db_key(cls, model_instance, field_name):
        sortedset_db_key = cls.get_sortedset_db_key(model_instance, field_name)
        # use field names and query values partition_on fields to extend sortedset_db_key
        for partition_field_name in model_instance._meta.fields[field_name].partition_on:
            try:
                sortedset_db_key += f":{str(getattr(model_instance, partition_field_name))}"
            except KeyError:
                raise QueryException(f"{field_name} filter requires partition_on field values")
        return sortedset_db_key

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value: typing.Union[int, float],
                pipeline: redis.client.Pipeline = None, **kwargs):
        sortedset_db_key = cls.get_partitioned_sortedset_db_key(model_instance, field_name)

        sortedset_member = model_instance.db_key
        sortedset_score = cls.convert_to_numeric(model_instance._meta.fields[field_name], field_value)

        if isinstance(pipeline, redis.client.Pipeline):
            return pipeline.zadd(sortedset_db_key, {sortedset_member: sortedset_score})
        else:
            return POPOTO_REDIS_DB.zadd(sortedset_db_key, {sortedset_member: sortedset_score})

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None,
                  **kwargs):
        sortedset_db_key = cls.get_partitioned_sortedset_db_key(model_instance, field_name)
        sortedset_member = model_instance.db_key
        if pipeline:
            return pipeline.zrem(sortedset_db_key, sortedset_member)
        else:
            return POPOTO_REDIS_DB.zrem(sortedset_db_key, sortedset_member)

    @classmethod
    def filter_query(cls, model_class: 'Model', field_name: str, **query_params) -> set:
        """
        :param model_class: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        value_range = {'min': '-inf', 'max': '+inf'}

        for query_param, query_value in query_params.items():
            if field_name not in query_param:
                continue

            numeric_value = cls.convert_to_numeric(model_class._meta.fields[field_name], query_value)
            if '__gt' in query_param:
                inclusive = query_param.split('__gt')[1]
                value_range['min'] = f"{'' if inclusive == 'e' else '('}{numeric_value}"
            elif '__lt' in query_param:
                inclusive = query_param.split('__lt')[1]
                value_range['max'] = f"{'' if inclusive == 'e' else '('}{numeric_value}"
            else:
                raise QueryException(f"Query filters provided are not compatible with this field {field_name}")

        sortedset_db_key = cls.get_sortedset_db_key(model_class, field_name)

        # use field names and query values partition_on fields to extend sortedset_db_key
        for field_name in model_class._meta.fields[field_name].partition_on:
            try:
                sortedset_db_key += f":{str(query_params[field_name])}"
            except KeyError:
                raise QueryException(f"{field_name} filter requires partition_on field values")

        redis_db_keys_list = POPOTO_REDIS_DB.zrangebyscore(
            sortedset_db_key, value_range['min'], value_range['max']
        )
        # redis_db_keys_list = POPOTO_REDIS_DB.zrange(
        #     sortedset_db_key, value_range['min'], value_range['max'],
        #     desc=False, withscores=False,
        #     byscore=True, offset=None, num=None
        # )
        return set(redis_db_keys_list)
