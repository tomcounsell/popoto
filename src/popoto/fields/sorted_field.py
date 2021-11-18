from decimal import Decimal
from datetime import date, datetime
import typing
import redis

from .field import Field
from ..redis_db import POPOTO_REDIS_DB

class SortedField(Field):
    """
        The SortedField enables fast queries for ordering or filter by value range.
        Examples:
            Toys.query.filter(price__lte=4.99)
            DairyProduct.query.filter(best_before_date__gte=datetime.now())
        Requirements:
            Must be numeric type (int, float, Decimal, Date, Datetime)
            Null values not allowed. Can set a default.
    """
    type: type = float
    null: bool = False
    default: float = 0

    def __init__(self, **kwargs):
        super().__init__()
        sortedfield_options = {  # default
            'type': float,
            'null': False,
            'default': 0,
        }
        # set geofield_options, let kwargs override
        for k, v in sortedfield_options.items():
            setattr(self, k, kwargs.get(k, v))
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
            # f'{field_name}__range',  # todo: like https://docs.djangoproject.com/en/3.2/ref/models/querysets/#range
            # f'{field_name}__isnull',  # todo: see todo in __init__
        ]

    @classmethod
    def is_valid(cls, field, value) -> bool:
        if not super().is_valid(field, value):
            return False
        if not isinstance(value, field.type):
            return False
        return True

    @classmethod
    def format_value_pre_save(cls, field_value):
        if cls.type in [int, float]:
            return field_value
        elif cls.type is Decimal:
            return float(field_value)
        elif cls.type is date:
            return field_value.toordinal(date)
        elif cls.type is datetime:
            return field_value.timestamp()
        else:
            raise ValueError("SortedField received non-numeric value.")

    @classmethod
    def get_sortedset_db_key(cls, model, field_name):
        return cls.get_special_use_field_db_key(model, field_name)

    @classmethod
    def on_save(cls, model: 'Model', field_name: str, field_value: typing.Union[int, float], pipeline=None):
        sortedset_db_key, sortedset_member, sortedset_score = (
            cls.get_sortedset_db_key(model, field_name), model.db_key, field_value
        )
        if isinstance(pipeline, redis.client.Pipeline):
            return pipeline.zadd(sortedset_db_key, {sortedset_member: sortedset_score})
        else:
            return POPOTO_REDIS_DB.zadd(sortedset_db_key, {sortedset_member: sortedset_score})

    @classmethod
    def on_delete(cls, model: 'Model', field_name: str, pipeline=None):
        sortedset_db_key, sortedset_member = (
            cls.get_sortedset_db_key(model, field_name), model.db_key
        )
        if pipeline:
            return pipeline.zrem(sortedset_db_key, sortedset_member)
        else:
            return POPOTO_REDIS_DB.zrem(sortedset_db_key, sortedset_member)


    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """

        for query_param, query_value in query_params.items():
            field_ranges = {}
            if '__' in query_param:
                field_name = query_param.split('__')[0]
                field_ranges[field_name] = {'min': '-inf', 'max': '+inf'}
            if '__gt' in query_param:
                field_name, inclusive = query_param.split('__gt')
                field_ranges[field_name]['min'] = f"{'' if inclusive=='e' else '('}{query_value}"
            if '__lt' in query_param:
                field_name, inclusive = query_param.split('__lt')
                field_ranges[field_name]['max'] = f"{'' if inclusive=='e' else '('}{query_value}"

        query_response = POPOTO_REDIS_DB.zrangebyscore(
            model.db_key, field_ranges[field_name]['min'], field_ranges[field_name]['max']
        )

        # if 'get last':
        #     query_response = POPOTO_REDIS_DB.zrange(model.db_key, -1, -1)
        #     try:
        #         [value, score] = query_response[0].decode("utf-8").split(":")
        #     except:
        #         value, score = "unknown", 0
        #
        #     min_score = max_score = score

        # NEW example query_response = [b'100:1']
        # which came from f'{self.value}:{str(score)}' where score = self.score

        return_list = []
        for key_score in query_response:
            key, score = (key_score.decode("utf-8").split(":")[0],
                            float(key_score.decode("utf-8").split(":")[1]))
            return_list.append((key, score))
        return set(return_list)
