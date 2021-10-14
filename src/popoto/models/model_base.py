import redis

from ...popoto import ModelField, ModelKey
import logging
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger(__name__)

class ModelException(Exception):
    pass


class RedisModelBase(type):
    """Metaclass for all Redis models."""

    def __new__(mcs, name, bases, attrs, **kwargs):
        print(attrs)

        module = attrs.pop('__module__')
        new_attrs = {'__module__': module}

        for obj_name, obj in attrs.items():
            if obj_name.startswith("__"):
                new_attrs[obj_name] = obj

            elif isinstance(obj, ModelField):
                if obj.value is not None:
                    try:
                        new_attrs[obj_name] = obj.type(obj.value)
                    except ValueError as e:
                        raise ModelException(f"field.value must be of type field.type\n{str(e)}")
                else:
                    new_attrs[obj_name] = obj.type()

                field_meta = ModelField().__dict__
                field_meta.update(obj.__dict__)
                new_attrs[f'{obj_name}_meta'] = field_meta

            elif not obj_name.startswith("_"):
                raise ModelException(
                    f"public model attributes must inherit from class ModelField. "
                    f"Try using a private var (eg. _{obj_name})_"
                )

        new_class = super().__new__(mcs, name, bases, new_attrs)

        return new_class

        # for field in base._meta.private_fields:
        #     new_class.add_to_class(field.name, field)


class RedisModel(metaclass=RedisModelBase):
    def __init__(self, **kwargs):
        self.ttl = None
        self.expire_at = None
        if self.ttl and self.expire_at:
            raise ModelException("Can set either ttl and expire_at. Not both.")
        self.__dict__.update(kwargs)

    def validate(self) -> bool:
        """
            todo: validate values
            - field.type ✅
            - field.is_null ✅
            - field.max_length ✅
            - field.is_sort_key
        """
        field_names = [k for k, v in self.__dict__.items() if all([not k.startswith("_"), k+"_meta" in self.__dict__])]
        for field_name in field_names:
            if not isinstance(self.__dict__[field_name], self.__dict__[field_name+'_meta'].type):
                error = f"{field_name} is not type {self.__dict__[field_name+'_meta'].type}. " \
                        f"Change the value or modify type on {self.__class__.__name__}.{field_name}"
                logger.error(error)
                return False
            if self.__dict__[field_name] is None and \
                    self.__dict__[field_name+'_meta'].is_null is False:
                error = f"{field_name} is None/null. Set a value or set is_null=True on {self.__class__.__name__}.{field_name}"
                logger.error(error)
                return False
            if self.__dict__[field_name+'_meta'].type == str and \
                    self.__dict__[field_name] and \
                    len(self.__dict__[field_name]) > self.__dict__[field_name+'_meta'].max_length:
                error = f"{field_name} is greater than max_length={self.__dict__[field_name+'_meta'].max_length}"
                logger.error(error)
                return False
            # if self.__dict__[field_name+'_meta'].is_sort_key:

        return True

    def save(self, publish: bool = False, pipeline: redis.client.Pipeline = None, ignore_errors: bool = False, *args, **kwargs):
        """
            Default save method. Uses Redis SET command with key, value, ttl.
            For other Redis structures, see SortedSetModel or GraphNodeModel
        """
        if not self.validate():
            error_message = "Model instance parameters invalid. Failed to save."
            if ignore_errors:
                logger.error(error_message)
            else:
                raise ModelException(error_message)
            return pipeline or 0

        self.value = self.value if self.value is not None else ""
        # logger.debug(f'saving key: {self.db_key}, value: {self.value}')

        if isinstance(pipeline, redis.client.Pipeline):
            return pipeline.set(self._db_key, self.value, ex=self.ttl, xx=self.expire_at)
        else:
            return POPOTO_REDIS_DB.set(self._db_key, self.value, ex=self.ttl, xx=self.expire_at)

    def publish(self, pipeline=None):
        return super().publish(data=self.get_z_add_data(), pipeline=pipeline)
