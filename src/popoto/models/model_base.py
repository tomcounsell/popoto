from datetime import datetime

import msgpack
import redis


import logging

from ..fields import KeyField, Field
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger('POPOTO.model_base')


class ModelException(Exception):
    pass


class ModelBase(type):
    """Metaclass for all Redis models."""

    def __new__(cls, name, bases, attrs, **kwargs):
        # logger.debug({k: v for k, v in attrs.items() if not k.startswith('__')})
        module = attrs.pop('__module__')
        new_attrs = {'__module__': module}
        db_key_field_name = ""

        for obj_name, obj in attrs.items():
            if obj_name.startswith("__"):
                # builtin or inherited private vars and methods
                new_attrs[obj_name] = obj

            elif callable(obj) or hasattr(obj, '__func__') or hasattr(obj, '__set__'):
                # a callable method or property
                new_attrs[obj_name] = obj

            elif isinstance(obj, Field):
                if isinstance(obj, KeyField):
                    if db_key_field_name != "":
                        raise ModelException(
                            "Only one ModelKey field allowed. Consider using a prefix or suffix in your key.")
                    db_key_field_name = obj_name
                    # todo: handle some key management?
                    # perhaps need to prepare to handle events such as key change, partial key search, ...

                if obj.value is not None:
                    try:
                        new_attrs[obj_name] = obj.type(obj.value)
                    except ValueError as e:
                        raise ModelException(f"field.value must be of type field.type\n{str(e)}")
                else:
                    new_attrs[obj_name] = obj.type()

                field_meta = dict(obj.__class__.__dict__)
                field_meta.update(obj.__dict__)
                new_attrs[f'{obj_name}_meta'] = field_meta

            elif not obj_name.startswith("_"):
                raise ModelException(
                    f"public model attributes must inherit from class Field. "
                    f"Try using a private var (eg. _{obj_name})_"
                )

        new_class = super().__new__(cls, name, bases, new_attrs)
        return new_class

        # for field in base._meta.private_fields:
        #     new_class.add_to_class(field.name, field)


class Model(metaclass=ModelBase):
    _db_key: str = ""  # default will be self.__class__.__name__
    _db_hashmap: dict = None
    _hashmap_delta: dict = {}
    _ttl: int = None  # todo: set default in child Meta class
    _expire_at: datetime = None

    def __init__(self, **kwargs):
        # defaults
        self._hashmap_delta = {}
        self._db_hashmap = {}
        self._db_key = kwargs.get('db_key', self.__class__.__name__)

        self._ttl = kwargs.get('ttl', None)
        self._expire_at = kwargs.get('expire_at', None)

        # allow init kwargs to set any base parameters
        self.__dict__.update(kwargs)

        # validate attributes
        if self._ttl and self._expire_at:
            raise ModelException("Can set either ttl and expire_at. Not both.")
        self.load_from_db()


    def __str__(self):
        return str(self._db_key)

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

    def save(self, pipeline: redis.client.Pipeline = None, ttl=None, expire_at=None, ignore_errors: bool = False, *args, **kwargs):
        """
            Default save method. Uses Redis HSET command with key, dict of values, ttl.
        """
        if not self.validate():
            error_message = "Model instance parameters invalid. Failed to save."
            if ignore_errors:
                logger.error(error_message)
            else:
                raise ModelException(error_message)
            return pipeline or 0

        ttl, expire_at = ttl or self._ttl, expire_at or self._expire_at
        field_names = [k for k, v in self.__dict__.items() if all([not k.startswith("_"), k+"_meta" in self.__dict__])]
        hset_mapping = {str(k): msgpack.packb(v) for k, v in repr(self) if k in field_names}

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.hset(self._db_key, mapping=hset_mapping)
            pipeline = pipeline if ttl is None else pipeline.expire(self._db_key, ttl)
            pipeline = pipeline if expire_at is None else pipeline.expire_at(self._db_key, expire_at)
            return pipeline
        else:
            db_response = POPOTO_REDIS_DB.hset(self._db_key, mapping=hset_mapping)
            if ttl is not None: POPOTO_REDIS_DB.expire(self._db_key, ttl)
            if expire_at is not None: POPOTO_REDIS_DB.expireat(self._db_key, ttl)
            return db_response

    @classmethod
    def get(cls, db_key: str):
        return cls(db_key=db_key)

    def __repr__(self):
        return str({**{k.decode("utf-8"): msgpack.unpackb(v) for k, v in self._db_hashmap.items()}, **self._hashmap_delta})

    def __setattr__(self, key, value):
        super(Model, self).__setattr__(key, value)
        if not key.startswith('_'):
            if self.__dict__.get(f'{key}_meta') and not isinstance(value, self.__dict__[f'{key}_meta']['type']):
                raise TypeError(f"{key} expecting type {self.__dict__[f'_key_meta']['type']}")
            # self._hashmap_delta[key] = msgpack.packb(value)

    # def __getattr__(self, key):
    #     try:
    #         value = super(object, self).__getattr__(key)
    #     except KeyError:
    #         pass
    #     if key not in self._db_hashmap:
    #         value = POPOTO_REDIS_DB.hget(self._db_key, key)
    #     else:
    #         value = self._db_hashmap[key]
    #     return msgpack.unpackb(value) if value is not None else None

    def load_from_db(self):
        self._db_hashmap = POPOTO_REDIS_DB.hgetall(self._db_key)
        print(self._db_hashmap)
        for key_b, db_value_b in self._db_hashmap.items():
            print(key_b, db_value_b)
            print(self.__dict__)
            setattr(self, key_b.decode("utf-8"), msgpack.unpackb(db_value_b))

    def revert(self):
        self._hashmap_delta = {}
        self.load_from_db()

    # @property
    # def value(self):
    #     if self._value is None:
    #         if self._db_value is None:
    #             self._db_value = self._get_db_value(self._db_key)
    #             self._value = self._db_value  # may stay None
    #     if self._value is not None:
    #         return msgpack.unpackb(self._value)
    #     return None

    # @value.setter
    # def value(self, new_value):
    #     self._value = msgpack.packb(new_value)

    def delete(self, pipeline=None, *args, **kwargs):
        if pipeline is not None:
            pipeline = pipeline.delete(self._db_key)
            return pipeline
        else:
            db_response = POPOTO_REDIS_DB.delete(self._db_key)
            if db_response >= 0:
                return True
