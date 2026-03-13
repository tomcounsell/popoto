from .exceptions import (
    ModelException,
    QueryException,
    PublisherException,
    SubscriberException,
)
from .fields.field import Field
from .fields.shortcuts import (
    IntField,
    FloatField,
    DecimalField,
    StringField,
    BooleanField,
    ListField,
    DictField,
    SetField,
    TupleField,
    BytesField,
    TimeField,
    DateField,
    KeyField,
    AutoKeyField,
    UniqueKeyField,
    SortedField,
    SortedKeyField,
)
from .fields.decaying_sorted_field import DecayingSortedField
from .fields.access_tracker import AccessTrackerMixin
from .fields.cyclic_decay_field import CyclicDecayField
from .fields.constants import TemporalPeriod
from .fields.geo_field import GeoField

try:
    from .fields.dataframe_field import DataFrameField
except ImportError:
    pass
from .fields.datetime_field import DatetimeField
from .fields.relationship import Relationship
from .models.base import Model, ModelBase
from .models.q import Q
from .models.expressions import Expression, CombinedExpression
from .pubsub.publisher import Publisher
from .pubsub.subscriber import Subscriber
from .redis_db import POPOTO_REDIS_DB, get_async_redis_db


def get_redis():
    """Get the Redis connection for direct Redis operations.

    Use this when you need Redis primitives not exposed by Popoto models:
    - Sets: SADD, SISMEMBER, SMEMBERS
    - Lists: RPUSH, LPOP, LRANGE
    - Pub/Sub: PUBLISH, SUBSCRIBE

    Returns:
        redis.Redis: The shared Redis connection

    Example:
        import popoto
        redis = popoto.get_redis()
        redis.sadd("my_set", "value")
        redis.rpush("my_queue", "item")
    """
    return POPOTO_REDIS_DB


__all__ = [
    "Field",
    "IntField",
    "FloatField",
    "DecimalField",
    "StringField",
    "BooleanField",
    "BytesField",
    "ListField",
    "DictField",
    "SetField",
    "TupleField",
    "TimeField",
    "DateField",
    "DatetimeField",
    "KeyField",
    "AutoKeyField",
    "UniqueKeyField",
    "SortedField",
    "SortedKeyField",
    "DecayingSortedField",
    "AccessTrackerMixin",
    "CyclicDecayField",
    "TemporalPeriod",
    "GeoField",
    "DataFrameField",
    "Relationship",
    "Model",
    "ModelBase",
    "Q",
    "Expression",
    "CombinedExpression",
    "Publisher",
    "Subscriber",
    "ModelException",
    "QueryException",
    "PublisherException",
    "SubscriberException",
    "get_redis",
    "get_async_redis_db",
]
