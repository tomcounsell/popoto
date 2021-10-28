import re
from decimal import Decimal
from datetime import date, datetime
import redis

from .field import Field
from ..redis_db import POPOTO_REDIS_DB

class SortedField(Field):
    """
        The SortedField enables fast queries by value range.
        Examples:
            Toys.filter(price_lte=4.99)
            DairyProduct.filter(best_before_date__gte=datetime.now())
        Requirements:
            Must be numeric type (int, float, decimal, date, datetime)
            Null values not allowed. Can set a default.
    """
    is_sort_key: bool = True
    null = False

    def __init__(self, **kwargs):
        super().__init__()
        new_kwargs = {  # default
            'is_sort_key': True,
        }
        if kwargs.get('null', None) == True:
            from ..models.base import ModelException
            raise ModelException("sort field cannot be null")
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

    @classmethod
    def convert_to_numeric(cls, field_type, value):
        if field_type in [int, float]:
            return value
        if field_type is Decimal:
            return float(value)
        if field_type is date:
            return value.toordinal(date)
        if field_type is datetime:
            return value.timestamp()

    @classmethod
    def post_save(cls, model, field_name, numeric_value, pipeline=None):
        z_add_data = {
            "key": model.db_key,
            "name": f'{field_name}:{numeric_value}',
            "score": numeric_value
        }

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.zadd(z_add_data["key"], z_add_data["name"])
            return pipeline
        else:
            return POPOTO_REDIS_DB.zadd(z_add_data["key"], z_add_data["name"])

    def get_filter_query_params(self, field_name):
        return [
            f'{field_name}__gt',
            f'{field_name}__gte',
            f'{field_name}__lt',
            f'{field_name}__lte',
        ]

    @classmethod
    def filter_query(cls, model: 'Model', **query_params) -> list:
        """
        :param model: the popoto.Model to query from
        :param query_params: dict of filter args and values
        :return: list[obj,]
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
        return return_list
