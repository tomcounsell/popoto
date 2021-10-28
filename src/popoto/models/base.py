from datetime import datetime

import msgpack
import redis

import logging

from .query import Query
from ..fields.key_field import KeyField
from ..fields.field import Field
from ..fields.sorted_field import SortedField
from ..redis_db import POPOTO_REDIS_DB, ENCODING

logger = logging.getLogger('POPOTO.model_base')


class ModelException(Exception):
    pass


class ModelOptions:
    def __init__(self, model_name):
        self.model_name = model_name
        self.db_key_field_name = ""
        self.db_key_field = None
        self.hidden_fields = {}
        self.explicit_fields = {}
        # self.list_field_names = []
        # self.set_field_names = []
        self.sorted_field_names = []
        # self.geo_field_names = []
        self.indexed_field_names = []

        # todo: should this be a dict of related objects or just a list of field names?
        # self.related_fields = {}  # model becomes graph node

        self.abstract = False
        self.unique_together = []
        self.index_together = []
        self.parents = []
        self.auto_created = False
        self.base_meta = None

    def add_field(self, field_name: str, field: Field):
        if field_name.startswith("_") and field_name not in self.hidden_fields:
            self.hidden_fields[field_name] = field
        elif field_name not in self.explicit_fields:
            self.explicit_fields[field_name] = field
        else:
            raise ModelException(f"{field_name} is already a Field on the model")

        if field.indexed:
            self.indexed_field_names.append(field_name)

        if isinstance(field, KeyField):
            if not self.db_key_field:
                self.db_key_field_name = field_name
                self.db_key_field = field
            else:
                raise ModelException(
                    "Only one ModelKey field allowed. Consider using a prefix or suffix in your key.")

        elif isinstance(field, SortedField):
            self.sorted_field_names.append(field_name)

    @property
    def fields(self) -> dict:
        return {**self.explicit_fields, **self.hidden_fields}

    @property
    def field_names(self) -> list:
        return list(self.fields.keys())

class ModelBase(type):
    """Metaclass for all Redis models."""

    def __new__(cls, name, bases, attrs, **kwargs):

        # Initialization is only performed for a Model and its subclasses
        parents = [b for b in bases if isinstance(b, ModelBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs)

        # logger.debug({k: v for k, v in attrs.items() if not k.startswith('__')})
        module = attrs.pop('__module__')
        new_attrs = {'__module__': module}
        attr_meta = attrs.pop('Meta', None)
        options = ModelOptions(name)
        options.parents = parents

        for obj_name, obj in attrs.items():
            if obj_name.startswith("__"):
                # builtin or inherited private vars and methods
                new_attrs[obj_name] = obj

            elif isinstance(obj, Field):
                # save field instance
                # attr will be overwritten as a field.type
                # model will handle this and set default values
                options.add_field(obj_name, obj)

            elif callable(obj) or hasattr(obj, '__func__') or hasattr(obj, '__set__'):
                # a callable method or property
                new_attrs[obj_name] = obj

            elif obj_name.startswith("_"):
                # a private static attr not to be saved in the db
                new_attrs[obj_name] = obj

            else:
                raise ModelException(
                    f"public model attributes must inherit from class Field. "
                    f"Try using a private var (eg. _{obj_name})_"
                )

        # todo: handle multiple inheritance
        # for base in parents:
        #     for field_name, field in base.auto_fields.items():
        #         options.add_field(field_name, field)

        new_class = super().__new__(cls, name, bases, new_attrs)

        options.abstract = getattr(attr_meta, 'abstract', False)
        options.meta = attr_meta or getattr(new_class, 'Meta', None)
        options.base_meta = getattr(new_class, '_meta', None)
        new_class._meta = options
        setattr(new_class, 'query', Query(new_class))
        setattr(new_class, 'objects', new_class.query)
        return new_class


class Model(metaclass=ModelBase):

    def __init__(self, **kwargs):
        cls = self.__class__
        # self._ttl = kwargs.get('ttl', None)
        # self._expire_at = kwargs.get('expire_at', None)

        # allow init kwargs to set any base parameters
        self.__dict__.update(kwargs)

        # add auto KeyField if needed
        if not self._meta.db_key_field:
            self._meta.add_field('_auto_key', KeyField(auto=True, key=cls.__name__))

        # set defaults
        for field_name, field in self._meta.fields.items():
            setattr(self, field_name, field.default)

        # set field values from init kwargs
        for field_name in self._meta.fields.keys() & kwargs.keys():
            setattr(self, field_name, kwargs.get(field_name))

        # todo: handle some key management?
        # perhaps need to prepare to handle events such as key change, partial key search, ...

        self._ttl = None  # todo: set default in child Meta class
        self._expire_at = None  # todo: datetime? or timestamp?

        # validate initial attributes
        self.validate(null_check=False)  # exclude null, will validate null values on pre-save
        self._db_content = dict()  # empty until self.load_from_db() or self.save() called


    @property
    def db_key(self):
        return getattr(self, self._meta.db_key_field_name)

    @db_key.setter
    def db_key(self, value):
        setattr(self, self._meta.db_key_field_name, value)

    def __str__(self):
        return str(self.db_key)

    def __eq__(self, other):
        return repr(self) == repr(other)

    def validate(self, null_check=True) -> bool:
        """
            todo: validate values
            - field.type ✅
            - field.null ✅
            - field.max_length ✅
            - field.is_sort_key - add in SortedField validator
            - ttl, expire_at
        """
        field_names = [k for k, v in self.__dict__.items() if
                       all([not k.startswith("_"), k + "_meta" in self.__dict__])]

        for field_name in field_names:
            # type check the field values against their class specified type, unless null/None
            if self.__dict__[field_name] is not None and \
                    not isinstance(self.__dict__[field_name], self.__dict__[field_name + '_meta'].type):
                error = f"{field_name} is not type {self.__dict__[field_name + '_meta'].type}. " \
                        f"Change the value or modify type on {self.__class__.__name__}.{field_name}"
                logger.error(error)
                return False

            # check non-nullable fields
            if null_check and \
                    self.__dict__[field_name + '_meta'].null is False and \
                    self.__dict__[field_name] is None:
                error = f"{field_name} is None/null. Set a value or set null=True on {self.__class__.__name__}.{field_name}"
                logger.error(error)
                return False

            # validate str max_length
            if self.__dict__[field_name + '_meta'].type == str and \
                    self.__dict__[field_name] and \
                    len(self.__dict__[field_name]) > self.__dict__[field_name + '_meta'].max_length:
                error = f"{field_name} is greater than max_length={self.__dict__[field_name + '_meta'].max_length}"
                logger.error(error)
                return False

            if self._ttl and self._expire_at:
                raise ModelException("Can set either ttl and expire_at. Not both.")
        return True

    def save(self, pipeline: redis.client.Pipeline = None, ttl=None, expire_at=None, ignore_errors: bool = False, *args,
             **kwargs):
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

        ttl, expire_at = (ttl or self._ttl), (expire_at or self._expire_at)
        hset_mapping = {
            str(k).encode(ENCODING): msgpack.packb(v)
            for k, v in self.__dict__.items() if k in self._meta.fields
        }
        self._db_content = hset_mapping
        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.hset(self.db_key, mapping=hset_mapping)
            pipeline = pipeline if ttl is None else pipeline.expire(self.db_key, ttl)
            pipeline = pipeline if expire_at is None else pipeline.expire_at(self.db_key, expire_at)
            return pipeline
        else:
            db_response = POPOTO_REDIS_DB.hset(self.db_key, mapping=hset_mapping)
            if ttl is not None: POPOTO_REDIS_DB.expire(self.db_key, ttl)
            if expire_at is not None: POPOTO_REDIS_DB.expireat(self.db_key, ttl)
            return db_response

    @classmethod
    def create(cls, *args, **kwargs):
        instance = cls(*args, **kwargs)
        instance.save()
        return instance

    @classmethod
    def get(cls, db_key: str = None, **kwargs):
        return cls.query.get(db_key=db_key, **kwargs)

    def __repr__(self):
        return str({k: v for k, v in self.__dict__.items() if k in self._meta.fields})

    def load_from_db(self):
        self._db_content = POPOTO_REDIS_DB.hgetall(self.db_key)
        for key_b, db_value_b in self._db_content.items():
            setattr(self, key_b.decode("utf-8"), msgpack.unpackb(db_value_b))

    def revert(self):
        self.load_from_db()

    def delete(self, pipeline=None, *args, **kwargs):
        self._db_content = dict()
        if pipeline is not None:
            pipeline = pipeline.delete(self.db_key)
            return pipeline
        else:
            db_response = POPOTO_REDIS_DB.delete(self.db_key)
            if db_response >= 0:
                return True
