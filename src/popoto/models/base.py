import logging

import redis

from .encoding import encode_popoto_model_obj
from .query import Query
from ..fields.field import Field
from ..fields.key_field_mixin import KeyFieldMixin
from ..fields.sorted_field_mixin import SortedFieldMixin
from ..fields.geo_field import GeoField
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger('POPOTO.model_base')


class ModelException(Exception):
    pass


class ModelOptions:
    def __init__(self, model_name):
        self.model_name = model_name
        self.hidden_fields = dict()
        self.explicit_fields = dict()
        self.key_field_names = set()
        # self.list_field_names = set()
        # self.set_field_names = set()
        self.sorted_field_names = set()
        self.geo_field_names = set()

        # todo: should this be a dict of related objects or just a list of field names?
        # self.related_fields = {}  # model becomes graph node

        # todo: allow customizing this in model.Meta class
        self.db_class_key = self.model_name
        self.db_class_set_key = f"$Class:{self.db_class_key}"

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

        if isinstance(field, KeyFieldMixin):
            self.key_field_names.add(field_name)
        elif isinstance(field, SortedFieldMixin):
            self.sorted_field_names.add(field_name)
        elif isinstance(field, GeoField):
            self.geo_field_names.add(field_name)
        # elif isinstance(field, ListField):
        #     self.list_field_names.add(field_name)

    @property
    def fields(self) -> dict:
        return {**self.explicit_fields, **self.hidden_fields}

    @property
    def field_names(self) -> list:
        return list(self.fields.keys())

    @property
    def db_key_length(self):
        return 1 + len(self.key_field_names)

    def get_db_key_index_position(self, field_name):
        return 1 + sorted(self.key_field_names).index(field_name)


class ModelBase(type):
    """Metaclass for all Popoto Models."""

    def __new__(cls, name, bases, attrs, **kwargs):

        # Initialization is only performed for a Model and its subclasses
        parents = [b for b in bases if isinstance(b, ModelBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

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
        new_class.objects = new_class.query = Query(new_class)
        return new_class


class Model(metaclass=ModelBase):
    query: Query

    def __init__(self, **kwargs):
        cls = self.__class__
        # self._ttl = kwargs.get('ttl', None)
        # self._expire_at = kwargs.get('expire_at', None)

        # allow init kwargs to set any base parameters
        self.__dict__.update(kwargs)

        # add auto KeyField if needed
        if not len(self._meta.key_field_names):
            from ..fields.shortcuts import AutoKeyField
            self._meta.add_field('_auto_key', AutoKeyField())

        # prep AutoKeys with new default ids
        for field in self._meta.fields.values():
            if hasattr(field, 'auto') and field.auto:
                field.set_auto_key_value()

        # set defaults
        for field_name, field in self._meta.fields.items():
            setattr(self, field_name, field.default)

        # set field values from init kwargs
        for field_name in self._meta.fields.keys() & kwargs.keys():
            setattr(self, field_name, kwargs.get(field_name))

        # additional key management here

        self._ttl = None  # todo: set default in child Meta class
        self._expire_at = None  # todo: datetime? or timestamp?

        # validate initial attributes
        if not self.is_valid(null_check=False):  # exclude null, will validate null values on pre-save
            raise ModelException(f"Could not instantiate class {self}")

        self._db_key = None
        # _db_key used by Redis cannot be known without performance cost
        # _db_key is predicted until synced during save() call
        if None not in [getattr(self, key_field_name) for key_field_name in self._meta.key_field_names]:
            self._db_key = self.db_key
        self.obsolete_key = None  # to be used when db_key changes between loading and saving the object
        self._db_content = dict()  # empty until synced during save() call

        # todo: create set of possible custom field keys



    @property
    def db_key(self):
        """
        the db key must include the class name - equivalent to db table name
        keys append alphabetically.
        if another order is required, propose feature request in GitHub issue
        possible solutions include param on each model's KeyField order=int
        OR model Meta: key_order = [keyname, keyname, ]
        OR both
        """
        return f"{self._meta.db_class_key}:" + ":".join([
            str(getattr(self, key_field_name)).replace(':', '_') or "None"
            for key_field_name in sorted(self._meta.key_field_names)
        ])

    def __repr__(self):
        return f"<{self.__class__.__name__} Popoto object at {self._db_key}>"

    def __str__(self):
        return str(self.db_key)

    def __eq__(self, other):
        """
        equality method
        instances with the same key(s) and class are considered equal
        except when any key(s) are None, they are not equal to anything except themselves.

        for evaluating all instance values against each other, use something like this:
        self_dict = self._meta.fields.update((k, self.__dict__[k]) for k in set(self.__dict__).intersection(self._meta.fields))
        other_dict = other._meta.fields.update((k, other.__dict__[k]) for k in set(other.__dict__).intersection(other._meta.fields))
        return repr(dict(sorted(self_dict))) == repr(dict(sorted(other_dict)))
        """
        try:
            if isinstance(other, self.__class__):
                # always False if if any KeyFields are None
                if (None in [
                    self._meta.fields.get(key_field_name) for key_field_name in self._meta.key_field_names
                ]) or (None in [
                    other._meta.fields.get(key_field_name) for key_field_name in other._meta.key_field_names
                ]):
                    return repr(self) == repr(other)
                return self.db_key == other.db_key
        except:
            return False
        else:
            return False

    # @property
    # def field_names(self):
    #     return [
    #         k for k, v in self.__dict__.items()
    #         if all([not k.startswith("_"), k + "_meta" in self.__dict__])
    #     ]

    def is_valid(self, null_check=True) -> bool:
        """
            todo: validate values
            - field.type ✅
            - field.null ✅
            - field.max_length ✅
            - ttl, expire_at - todo
        """

        for field_name in self._meta.field_names:
            # type check the field values against their class specified type, unless null/None

            if all([
                getattr(self, field_name) is not None,
                not isinstance(getattr(self, field_name), self._meta.fields[field_name].type)
            ]):
                try:
                    if getattr(self, field_name) is not None:
                        setattr(self, field_name, self._meta.fields[field_name].type(getattr(self, field_name)))
                    if not isinstance(getattr(self, field_name), self._meta.fields[field_name].type):
                        raise TypeError(f"{field_name} is not type {self._meta.fields[field_name].type}. ")
                except TypeError as e:
                    logger.error(
                        f"{str(e)} \n Change the value or modify type on {self.__class__.__name__}.{field_name}"
                    )
                    return False

            # check non-nullable fields
            if null_check and \
                    self._meta.fields[field_name].null is False and \
                    getattr(self, field_name) is None:
                error = f"{field_name} is None/null. " \
                        f"Set a value or set null=True on {self.__class__.__name__}.{field_name}"
                logger.error(error)
                return False

            # validate str max_length
            if self._meta.fields[field_name].type == str and \
                    getattr(self, field_name) and \
                    len(getattr(self, field_name)) > self._meta.fields[field_name].max_length:
                error = f"{field_name} is greater than max_length={self._meta.fields[field_name].max_length}"
                logger.error(error)
                return False

            if self._ttl and self._expire_at:
                raise ModelException("Can set either ttl and expire_at. Not both.")

        for field_name, field_value in self.__dict__.items():
            if field_name in self._meta.fields.keys():
                field_class = self._meta.fields[field_name].__class__
                if not field_class.is_valid(self._meta.fields[field_name], field_value, null_check=null_check):
                    error = f"Validation on [{field_name}] Field failed"
                    logger.error(error)
                    return False

        return True

    def pre_save(self, pipeline: redis.client.Pipeline = None,
                 ignore_errors: bool = False, **kwargs):
        """
        Model instance preparation for saving.
        """
        if not self.is_valid():
            error_message = "Model instance parameters invalid. Failed to save."
            if ignore_errors:
                logger.error(error_message)
            else:
                raise ModelException(error_message)
            return False

        # run any necessary formatting on field data before saving
        for field_name, field in self._meta.fields.items():
            setattr(
                self, field_name,
                field.format_value_pre_save(getattr(self, field_name))
            )
        return pipeline if pipeline else True

    def save(self, pipeline: redis.client.Pipeline = None,
             ttl=None, expire_at=None, ignore_errors: bool = False,
             **kwargs):
        """
            Model instance save method. Uses Redis HSET command with key, dict of values, ttl.
            Also triggers all field on_save methods.
        """

        pipeline_or_success = self.pre_save(pipeline=pipeline, ignore_errors=ignore_errors, **kwargs)
        if not pipeline_or_success:
            return pipeline or False
        elif pipeline:
            pipeline = pipeline_or_success

        new_db_key = self.db_key
        if self._db_key != new_db_key:
            self.obsolete_key = self._db_key

        # todo: implement and test tll, expire_at
        ttl, expire_at = (ttl or self._ttl), (expire_at or self._expire_at)

        """
        1. save object as hashmap
        2. optionally set ttl, expire_at
        3. add to class set
        4. if obsolete key, delete and run field on_delete methods
        5. run field on_save methods
        6. save private version of compiled db key
        """

        hset_mapping = encode_popoto_model_obj(self)  # 1
        self._db_content = hset_mapping  # 1

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.hset(new_db_key, mapping=hset_mapping)  # 1
            # if ttl is not None:
            #     pipeline = pipeline.expire(new_db_key, ttl)  # 2
            # if expire_at is not None:
            #     pipeline = pipeline.expire_at(new_db_key, expire_at)  # 2
            pipeline = pipeline.sadd(self._meta.db_class_set_key, new_db_key)  # 3
            if self.obsolete_key and self.obsolete_key != new_db_key:  # 4
                for field_name, field in self._meta.fields.items():
                    pipeline = field.on_delete(  # 4
                        model_instance=self, field_name=field_name,
                        field_value=getattr(self, field_name),
                        pipeline=pipeline, **kwargs
                    )
                pipeline.delete(self.obsolete_key)  # 4
                self.obsolete_key = None
            for field_name, field in self._meta.fields.items():  # 5
                pipeline = field.on_save(  # 5
                    self, field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=pipeline, **kwargs
                )
            self._db_key = new_db_key  # 6
            return pipeline

        else:
            db_response = POPOTO_REDIS_DB.hset(new_db_key, mapping=hset_mapping)  # 1
            # if ttl is not None:
            #     POPOTO_REDIS_DB.expire(new_db_key, ttl)  # 2
            # if expire_at is not None:
            #     POPOTO_REDIS_DB.expireat(new_db_key, ttl)  # 2
            POPOTO_REDIS_DB.sadd(self._meta.db_class_set_key, new_db_key)  # 2

            if self.obsolete_key and self.obsolete_key != new_db_key:  # 4
                for field_name, field in self._meta.fields.items():
                    field.on_delete(  # 4
                        model_instance=self, field_name=field_name,
                        field_value=getattr(self, field_name),
                        pipeline=None, **kwargs
                    )
                POPOTO_REDIS_DB.delete(self.obsolete_key)  # 4
                self.obsolete_key = None

            for field_name, field in self._meta.fields.items():  # 5
                field.on_save(  # 5
                    self, field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=None, **kwargs
                )

            self._db_key = new_db_key  # 6
            return db_response

    @classmethod
    def create(cls, **kwargs):
        instance = cls(**kwargs)
        instance.save()
        return instance

    @classmethod
    def get(cls, db_key: str = None, **kwargs):
        return cls.query.get(db_key=db_key, **kwargs)

    def delete(self, pipeline: redis.client.Pipeline = None, *args, **kwargs):
        """
            Model instance delete method. Uses Redis DELETE command with key.
            Also triggers all field on_delete methods.
        """

        delete_key = self._db_key or self.db_key

        """
        1. delete object as hashmap
        2. delete from class set
        3. run field on_delete methods
        4. reset private vars
        """

        if pipeline is not None:
            pipeline = pipeline.delete(delete_key)  # 1
            pipeline = pipeline.srem(self._meta.db_class_set_key, delete_key)  # 2

            for field_name, field in self._meta.fields.items():  # 3
                pipeline = field.on_delete(  # 3
                    model_instance=self, field_name=field_name,
                    field_value=getattr(self, field_name),
                    pipeline=pipeline, **kwargs
                )

            self._db_content = dict()  # 4
            return pipeline

        else:
            db_response = POPOTO_REDIS_DB.delete(delete_key)  # 1
            POPOTO_REDIS_DB.srem(self._meta.db_class_set_key, delete_key)  # 2

            for field_name, field in self._meta.fields.items():  # 3
                field.on_delete(  # 3
                    model_instance=self, field_name=field_name,
                    field_value=getattr(self, field_name),
                    pipeline=None, **kwargs
                )

            self._db_content = dict()  # 4
            return bool(db_response >= 0)

    @classmethod
    def get_info(cls):
        from itertools import chain
        query_filters = list(chain(*[
            field.get_filter_query_params(field_name)
            for field_name, field in cls._meta.fields.items()
        ]))
        return {
            'name': cls.__name__,
            'fields': cls._meta.field_names,
            'query_filters': query_filters,
        }
