import logging
from datetime import date, datetime, time
from decimal import Decimal

from src.popoto.exceptions import ModelException

logger = logging.getLogger('POPOTO.field')

VALID_FIELD_TYPES = [
    int, float, Decimal, str, bool, list, dict, bytes, date, datetime, time,
]


class FieldBase(type):
    """Metaclass for all Popoto Fields."""

    def __new__(cls, name, bases, attrs, **kwargs):
        # if not a Field, skip setup
        parents = [b for b in bases if isinstance(b, FieldBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

        new_class = super().__new__(cls, name, bases, attrs, **kwargs)
        new_class.field_class_key = f"${name.strip('Field')}F"
        return new_class


class Field(metaclass=FieldBase):
    type: type = str
    unique: bool = False
    indexed: bool = False
    value: str = None
    null: bool = False
    max_length: int = 1024
    default: str = ""

    def __init__(self, **kwargs):
        self.field_defaults = {  # default
            'type': str,
            'unique': True,
            'indexed': False,
            'value': None,
            'null': False,
            'max_length': 1024,  # Redis limit is 512MB
            'default': None,
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
            return False
        return True

    @classmethod
    def format_value_pre_save(cls, field_value):
        """
        format field_value before saving to db
        return corrected field_value
        assumes validation is already passed
        """
        return field_value

    @classmethod
    def get_special_use_field_db_key(cls, model: 'Model', field_name: str):
        """
        For use by child class when implementing additional Redis data structures
        Children implementing more than one new structure will need to augment this.
        """
        return f"{cls.field_class_key}:{model._meta.db_class_key}:{field_name}"

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value, pipeline=None, **kwargs):
        """
        for parent classes to override.
        will run for every field of the model instance, including null attributes
        runs async with model instance save event, so order of processing is not guaranteed
        """
        # todo: create choice Sets of instance keys
        # if model_instance._meta.fields[field_name].choices:
        #     # this will not work! how to edit, delete, prevent overwrite and duplicates?
        #     field_value_b = cls.encode(field_value)
        #     if pipeline:
        #         return pipeline.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)
        #     else:
        #         from src.popoto.redis_db import POPOTO_REDIS_DB
        #         return POPOTO_REDIS_DB.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline=None, **kwargs):
        """
        for parent classes to override.
        will run for every field of the model instance, including null attributes
        runs async with model instance delete event, so order of processing is not guaranteed
        """
        pass

    def get_filter_query_params(self, field_name: str) -> list:
        # todo: auto manage sets of db_keys to allow filter by any indexed field
        if self.indexed:
            params = [f'{field_name}', ]
            if self.null:
                params += [f'{field_name}__isnull', ]
            return params
        return list()

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        from ..models.query import QueryException
        if model._meta.fields[field_name].indexed:
            raise QueryException("Query filter by any indexed field is not yet implemented.")
        else:
            raise QueryException("Query filter not possible on non-indexed fields")
