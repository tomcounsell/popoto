from datetime import date, datetime, time
from decimal import Decimal
import redis

from ..exceptions import ModelException

import logging

from ..models.db_key import DB_key

logger = logging.getLogger('POPOTO.field')

VALID_FIELD_TYPES = {
    int, float, Decimal, str, bool, bytes, list, dict, set, tuple, date, datetime, time,
}


class FieldBase(type):
    """Metaclass for all Popoto Fields."""

    def __new__(cls, name, bases, attrs, **kwargs):
        # if not a Field, skip setup
        parents = [b for b in bases if isinstance(b, FieldBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

        new_class = super().__new__(cls, name, bases, attrs, **kwargs)
        new_class.field_class_key = DB_key(f"${name.strip('Field')}F")
        return new_class


class Field(metaclass=FieldBase):
    type: type = str
    key: bool = False
    unique: bool = False
    auto: bool = False
    null: bool = True
    value: str = None
    max_length: int = 1024
    default = ""
    sorted: bool = False

    def __init__(self, **kwargs):
        self.field_defaults = {  # default
            'type': str,
            'key': False,
            'unique': False,
            'auto': False,
            'null': True,
            'value': None,
            'max_length': 1024,  # Redis limit is 512MB
            'default': None,
            'sorted': False,
        }
        # set field_options, let kwargs override
        field_options = self.field_defaults.copy()
        for k, v in field_options.items():
            setattr(self, k, kwargs.get(k, v))
        if self.__class__ == Field and self.type not in VALID_FIELD_TYPES:
            raise ModelException(f"{self.type} is not a valid Field type")

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if not null_check and value is None:
            return True
        if field.null and value is None:
            return True
        elif value is None:
            logger.error(f"field {field} is null")
            return False
        elif not isinstance(value, field.type):
            logger.error(f"field {field} is type {field.type}. But value is {type(value)}")
            return False
        if field.type == str and len(str(value)) > field.max_length:
            logger.error(f"{field} value is greater than max_length={field.max_length}")
            return False
        return True

    def format_value_pre_save(self, field_value):
        """
        format field_value before saving to db
        return corrected field_value
        assumes validation is already passed
        """
        return field_value

    @classmethod
    def get_special_use_field_db_key(cls, model: 'Model', *field_names) -> DB_key:
        """
        For use by child class when implementing additional Redis data structures
        Children implementing more than one new structure will need to augment this.
        """
        return DB_key(cls.field_class_key, model._meta.db_class_key, *field_names)


    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None,
                **kwargs):
        """
        for parent classes to override.
        will run for every field of the model instance, including null attributes
        runs async with model instance save event, so order of processing is not guaranteed
        """
        # todo: create choice Sets of instance keys for fields using choices option
        # if model_instance._meta.fields[field_name].choices:
        #     # this will not work! how to edit, delete, prevent overwrite and duplicates?
        #     field_value_b = cls.encode(field_value)
        #     if pipeline:
        #         return pipeline.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)
        #     else:
        #         from ..redis_db import POPOTO_REDIS_DB
        #         return POPOTO_REDIS_DB.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)

        return pipeline if pipeline else None

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline=None, **kwargs):
        """
        for parent classes to override.
        will run for every field of the model instance, including null attributes
        runs async with model instance delete event, so order of processing is not guaranteed
        """
        return pipeline if pipeline else None

    def get_filter_query_params(self, field_name: str) -> set:
        """
        for specialty fields to extend or override
        returns a set of strings, representing valid parameters for Query.filter()
        """
        return set()

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        from ..models.query import QueryException
        raise QueryException("Query filter not allowed on base Field. Consider using a KeyField")
