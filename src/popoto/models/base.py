import logging
import asyncio
import sys
import functools

import redis

from .encoding import encode_popoto_model_obj
from .db_key import DB_key
from .query import Query
from ..fields.auto_field_mixin import AutoFieldMixin
from ..fields.field import Field, VALID_FIELD_TYPES
from ..fields.key_field_mixin import KeyFieldMixin
from ..fields.sorted_field_mixin import SortedFieldMixin
from ..fields.geo_field import GeoField
from ..fields.relationship import Relationship
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.model_base")

# Python 3.8 compatibility for asyncio.to_thread()
if sys.version_info >= (3, 9):
    to_thread = asyncio.to_thread
else:
    # Backport for Python 3.8
    async def to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

global RELATED_MODEL_LOAD_SEQUENCE
RELATED_MODEL_LOAD_SEQUENCE = set()


class ModelException(Exception):
    pass


class ModelOptions:
    def __init__(self, model_name):
        self.model_name = model_name
        self.db_class_key = DB_key(self.model_name)
        self.db_class_set_key = DB_key("$Class", self.db_class_key)

        self.hidden_fields = dict()
        self.explicit_fields = dict()
        self.key_field_names = set()
        self.auto_field_names = set()
        # self.list_field_names = set()
        # self.set_field_names = set()
        self.relationship_field_names = set()
        self.sorted_field_names = set()
        self.geo_field_names = set()
        # todo: should this be a dict of related objects or just a list of field names?
        # self.related_fields = {}  # model becomes graph node

        self.filter_query_params_by_field = dict()  # field_name: set(query_params,..)

        self.abstract = False
        self.unique_together = []
        self.index_together = []
        self.parents = []
        self.auto_created = False
        self.base_meta = None
        self.order_by = None  # Default ordering for queries
        self.ttl = None  # Default TTL in seconds for all instances
        self.indexes = ()  # Tuple of ((field_names,), is_unique) tuples

    def add_field(self, field_name: str, field: Field):
        if not field_name[0] == "_" and not field_name[0].islower():
            raise ModelException(
                f"{field_name} field name must start with a lowercase letter."
            )
        elif field_name in ["limit", "order_by", "values"]:
            raise ModelException(
                f"{field_name} is a reserved field name. "
                f"See https://popoto.readthedocs.io/en/latest/fields/#reserved-field-names"
            )
        elif field_name.startswith("_") and field_name not in self.hidden_fields:
            self.hidden_fields[field_name] = field
        elif field_name not in self.explicit_fields:
            self.explicit_fields[field_name] = field
        else:
            raise ModelException(f"{field_name} is already a Field on the model")

        if isinstance(field, KeyFieldMixin):
            self.key_field_names.add(field_name)
        if isinstance(field, AutoFieldMixin):
            self.auto_field_names.add(field_name)
        if isinstance(field, SortedFieldMixin):
            self.sorted_field_names.add(field_name)
        if isinstance(field, GeoField):
            self.geo_field_names.add(field_name)
        # if isinstance(field, ListField):
        #     self.list_field_names.add(field_name)
        if isinstance(field, Relationship):
            self.relationship_field_names.add(field_name)

        self.filter_query_params_by_field[field_name] = field.get_filter_query_params(
            field_name
        )

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

    def get_index_key(self, field_names: tuple) -> str:
        """Generate Redis key for an index."""
        field_key = ":".join(field_names)
        return f"$Index:{self.model_name}:{field_key}"

    def compute_index_hash(self, model_instance, field_names: tuple) -> str:
        """Compute hash of field values for index uniqueness check.

        Returns None if any field value is None (NULL handling: multiple NULLs allowed).
        """
        import hashlib

        values = []
        for field_name in field_names:
            value = getattr(model_instance, field_name, None)
            if value is None:
                return None  # Don't index NULL values (allows multiple NULLs)
            values.append(str(value))
        combined = ":".join(values)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    def compute_index_hash_from_values(self, field_names: tuple, field_values: dict) -> str:
        """Compute hash from a dict of field values (for cleanup of old values).

        Returns None if any field value is None.
        """
        import hashlib

        values = []
        for field_name in field_names:
            value = field_values.get(field_name)
            if value is None:
                return None
            values.append(str(value))
        combined = ":".join(values)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]


class ModelBase(type):
    """Metaclass for all Popoto Models."""

    def __new__(cls, name, bases, attrs, **kwargs):

        # Initialization is only performed for a Model and its subclasses
        parents = [b for b in bases if isinstance(b, ModelBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

        # logger.debug({k: v for k, v in attrs.items() if not k.startswith('__')})
        module = attrs.pop("__module__")
        new_attrs = {"__module__": module}
        attr_meta = attrs.pop("Meta", None)
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

            elif callable(obj) or hasattr(obj, "__func__") or hasattr(obj, "__set__"):
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

        options.abstract = getattr(attr_meta, "abstract", False)
        options.order_by = getattr(attr_meta, "order_by", None)
        options.ttl = getattr(attr_meta, "ttl", None)
        options.indexes = getattr(attr_meta, "indexes", ())

        # Validate order_by field exists
        if options.order_by:
            field_name = options.order_by.lstrip("-")
            if field_name not in options.fields:
                raise ModelException(
                    f"Meta.order_by references '{field_name}' but this field does not exist on {name}"
                )

        # Validate ttl is a positive integer if provided
        if options.ttl is not None and (
            not isinstance(options.ttl, int) or options.ttl <= 0
        ):
            raise ModelException(
                f"Meta.ttl must be a positive integer (seconds), got {options.ttl}"
            )

        # Validate indexes structure
        if options.indexes:
            if not isinstance(options.indexes, (tuple, list)):
                raise ModelException(
                    f"Meta.indexes must be a tuple or list, got {type(options.indexes)}"
                )
            for index in options.indexes:
                if not isinstance(index, (tuple, list)) or len(index) != 2:
                    raise ModelException(
                        f"Each index must be a 2-tuple (field_names, is_unique), got {index}"
                    )
                field_names, is_unique = index
                if not isinstance(field_names, (tuple, list)):
                    raise ModelException(
                        f"Index field names must be a tuple/list, got {type(field_names)}"
                    )
                if not isinstance(is_unique, bool):
                    raise ModelException(
                        f"Index uniqueness flag must be boolean, got {type(is_unique)}"
                    )
                # Validate all field names exist
                for field_name in field_names:
                    if field_name not in options.fields:
                        raise ModelException(
                            f"Unknown field '{field_name}' in Meta.indexes for {name}"
                        )

        options.meta = attr_meta or getattr(new_class, "Meta", None)
        options.base_meta = getattr(new_class, "_meta", None)
        new_class._meta = options
        new_class.objects = new_class.query = Query(new_class)
        return new_class


class Model(metaclass=ModelBase):
    query: Query

    def __init__(self, **kwargs):
        cls = self.__class__

        # allow init kwargs to set any base parameters
        self.__dict__.update(kwargs)

        # add auto KeyField if needed
        if not len(self._meta.key_field_names):
            from ..fields.shortcuts import AutoKeyField

            self._meta.add_field("_auto_key", AutoKeyField())

        # prep AutoKeys with new default ids
        for field in self._meta.fields.values():
            if hasattr(field, "auto") and field.auto:
                field.set_auto_key_value()

        # set defaults (support callable defaults like uuid.uuid4 or dict)
        for field_name, field in self._meta.fields.items():
            default_value = (
                field.default() if callable(field.default) else field.default
            )
            setattr(self, field_name, default_value)

        # set field values from init kwargs
        for field_name in self._meta.fields.keys() & kwargs.keys():
            setattr(self, field_name, kwargs.get(field_name))

        # load relationships
        if len(self._meta.relationship_field_names):
            global RELATED_MODEL_LOAD_SEQUENCE
            is_parent_model = len(RELATED_MODEL_LOAD_SEQUENCE) == 0
            for field_name in self._meta.relationship_field_names:
                if (
                    f"{self.__class__.__name__}.{field_name}"
                    in RELATED_MODEL_LOAD_SEQUENCE
                ):
                    continue
                RELATED_MODEL_LOAD_SEQUENCE.add(
                    f"{self.__class__.__name__}.{field_name}"
                )

                field_value = getattr(self, field_name)
                if isinstance(field_value, Model):
                    setattr(self, field_name, field_value)
                elif isinstance(field_value, str):
                    setattr(
                        self,
                        field_name,
                        self._meta.fields[field_name].model.query.get(
                            redis_key=field_value
                        ),
                    )

                # todo: lazy load the instance from the db
                elif not field_value:
                    setattr(self, field_name, None)
                else:
                    raise ModelException(
                        f"{field_name} expects model instance or redis_key"
                    )
            if is_parent_model:
                RELATED_MODEL_LOAD_SEQUENCE = set()

        # Set TTL from Meta.ttl as default if not already set via kwargs
        if not hasattr(self, "_ttl") or self._ttl is None:
            self._ttl = self._meta.ttl
        if not hasattr(self, "_expire_at"):
            self._expire_at = None  # Can be set per-instance as datetime

        # validate initial attributes
        if not self.is_valid(
            null_check=False
        ):  # exclude null, will validate null values on pre-save
            raise ModelException(f"Could not instantiate class {self}")

        self._redis_key = None
        # _db_key used by Redis cannot be known without performance cost
        # _db_key is predicted until synced during save() call
        if None not in [
            getattr(self, key_field_name)
            for key_field_name in self._meta.key_field_names
        ]:
            self._redis_key = self.db_key.redis_key
        self.obsolete_redis_key = (
            None  # to be used when db_key changes between loading and saving the object
        )
        self._db_content = dict()  # empty until synced during save() call
        self._saved_field_values = (
            dict()
        )  # stores field values at last save for proper on_delete cleanup

        # todo: create set of possible custom field keys

    @property
    def db_key(self) -> DB_key:
        """
        the db key must include the class name - equivalent to db table name
        keys append alphabetically.
        if another order is required, propose feature request in GitHub issue
        possible solutions include param on each model's KeyField order=int
        OR model Meta: key_order = [keyname, keyname, ]
        OR both
        """
        return DB_key(
            self._meta.db_class_key,
            [
                str(getattr(self, key_field_name, "None"))
                for key_field_name in sorted(self._meta.key_field_names)
            ],
        )

    def __repr__(self):
        return f"<{self.__class__.__name__} Popoto object at {self.db_key.redis_key}>"

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
                if (
                    None
                    in [
                        self._meta.fields.get(key_field_name)
                        for key_field_name in self._meta.key_field_names
                    ]
                ) or (
                    None
                    in [
                        other._meta.fields.get(key_field_name)
                        for key_field_name in other._meta.key_field_names
                    ]
                ):
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

            if all(
                [
                    getattr(self, field_name) is not None,
                    not isinstance(
                        getattr(self, field_name), self._meta.fields[field_name].type
                    ),
                ]
            ):
                try:
                    if getattr(self, field_name) is not None:
                        if self._meta.fields[field_name].type in VALID_FIELD_TYPES:
                            setattr(
                                self,
                                field_name,
                                self._meta.fields[field_name].type(
                                    getattr(self, field_name)
                                ),
                            )
                        else:
                            pass  # do not force typing if custom type is defined
                    if not isinstance(
                        getattr(self, field_name), self._meta.fields[field_name].type
                    ):
                        raise TypeError(
                            f"Expected {field_name} to be type {self._meta.fields[field_name].type}. "
                            f"It is type {type(getattr(self, field_name))}"
                        )
                except TypeError as e:
                    logger.error(
                        f"{str(e)} \n Change the value or modify type on {self.__class__.__name__}.{field_name}"
                    )
                    return False

            # check non-nullable fields
            if (
                null_check
                and self._meta.fields[field_name].null is False
                and getattr(self, field_name) is None
            ):
                error = (
                    f"{field_name} is None/null. "
                    f"Set a value or set null=True on {self.__class__.__name__}.{field_name}"
                )
                logger.error(error)
                return False

            # validate str max_length
            if (
                self._meta.fields[field_name].type == str
                and getattr(self, field_name)
                and len(getattr(self, field_name))
                > self._meta.fields[field_name].max_length
            ):
                error = f"{field_name} is greater than max_length={self._meta.fields[field_name].max_length}"
                logger.error(error)
                return False

            if self._ttl and self._expire_at:
                raise ModelException("Can set either ttl and expire_at. Not both.")

        for field_name, field_value in self.__dict__.items():
            if field_name in self._meta.fields.keys():
                field_class = self._meta.fields[field_name].__class__
                if not field_class.is_valid(
                    self._meta.fields[field_name], field_value, null_check=null_check
                ):
                    error = f"Validation on [{field_name}] Field failed"
                    logger.error(error)
                    return False

        return True

    def pre_save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
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

        # Check unique indexes
        for field_names, is_unique in self._meta.indexes:
            if not is_unique:
                continue  # Only check unique indexes

            index_key = self._meta.get_index_key(tuple(field_names))
            index_hash = self._meta.compute_index_hash(self, tuple(field_names))

            # Skip NULL values (multiple NULLs allowed per SQL standard)
            if index_hash is None:
                continue

            # Check if hash exists in Redis HASH
            existing_key = POPOTO_REDIS_DB.hget(index_key, index_hash)
            if existing_key:
                existing_key_str = existing_key.decode() if isinstance(existing_key, bytes) else existing_key
                # Skip self if updating (same db_key)
                if self._redis_key and existing_key_str == self._redis_key:
                    continue
                if existing_key_str == self.db_key.redis_key:
                    continue

                field_values = [str(getattr(self, f)) for f in field_names]
                error_message = (
                    f"Unique index violation on {field_names}: "
                    f"({', '.join(field_values)}) already exists"
                )
                if ignore_errors:
                    logger.error(error_message)
                    return False
                else:
                    raise ModelException(error_message)

        # run any necessary formatting on field data before saving
        for field_name, field in self._meta.fields.items():
            setattr(
                self, field_name, field.format_value_pre_save(getattr(self, field_name))
            )
        return pipeline if pipeline else True

    def save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
        """
        Model instance save method. Uses Redis HSET command with key, dict of values, ttl.
        Also triggers all field on_save methods.
        """

        pipeline_or_success = self.pre_save(
            pipeline=pipeline, ignore_errors=ignore_errors, **kwargs
        )
        if not pipeline_or_success:
            return pipeline or False
        elif pipeline:
            pipeline = pipeline_or_success

        new_db_key = DB_key(self.db_key)  # todo: why have a new key??
        if self._redis_key != new_db_key.redis_key:
            self.obsolete_redis_key = self._redis_key

        # todo: implement and test tll, expire_at
        # ttl, expire_at = (ttl or self._ttl), (expire_at or self._expire_at)

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
            pipeline = pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)  # 1
            if self._ttl is not None:
                pipeline = pipeline.expire(new_db_key.redis_key, self._ttl)  # 2
            elif self._expire_at is not None:
                pipeline = pipeline.expireat(
                    new_db_key.redis_key, int(self._expire_at.timestamp())
                )  # 2
            pipeline = pipeline.sadd(
                self._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3
            if (
                self.obsolete_redis_key
                and self.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in self._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = self._saved_field_values.get(
                        field_name, getattr(self, field_name)
                    )
                    pipeline = field.on_delete(  # 4
                        model_instance=self,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=pipeline,
                        saved_redis_key=self.obsolete_redis_key,
                        **kwargs,
                    )
                pipeline.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None
            for field_name, field in self._meta.fields.items():  # 5
                pipeline = field.on_save(  # 5
                    self,
                    field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=pipeline,
                    **kwargs,
                )
            # Manage indexes  # 6
            for field_names, is_unique in self._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = self._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if self._saved_field_values:
                    old_hash = self._meta.compute_index_hash_from_values(
                        field_names_tuple, self._saved_field_values
                    )
                    if old_hash:
                        pipeline = pipeline.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = self._meta.compute_index_hash(self, field_names_tuple)
                if new_hash:
                    pipeline = pipeline.hset(index_key, new_hash, new_db_key.redis_key)
            self._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            self._saved_field_values = {
                field_name: getattr(self, field_name)
                for field_name in self._meta.fields.keys()
            }
            return pipeline

        else:
            db_response = POPOTO_REDIS_DB.hset(
                new_db_key.redis_key, mapping=hset_mapping
            )  # 1
            if self._ttl is not None:
                POPOTO_REDIS_DB.expire(new_db_key.redis_key, self._ttl)  # 2
            elif self._expire_at is not None:
                POPOTO_REDIS_DB.expireat(
                    new_db_key.redis_key, int(self._expire_at.timestamp())
                )  # 2
            POPOTO_REDIS_DB.sadd(
                self._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3

            if (
                self.obsolete_redis_key
                and self.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in self._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = self._saved_field_values.get(
                        field_name, getattr(self, field_name)
                    )
                    field.on_delete(  # 4
                        model_instance=self,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=None,
                        saved_redis_key=self.obsolete_redis_key,
                        **kwargs,
                    )
                POPOTO_REDIS_DB.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None

            for field_name, field in self._meta.fields.items():  # 5
                field.on_save(  # 5
                    self,
                    field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=None,
                    **kwargs,
                )

            # Manage indexes  # 6
            for field_names, is_unique in self._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = self._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if self._saved_field_values:
                    old_hash = self._meta.compute_index_hash_from_values(
                        field_names_tuple, self._saved_field_values
                    )
                    if old_hash:
                        POPOTO_REDIS_DB.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = self._meta.compute_index_hash(self, field_names_tuple)
                if new_hash:
                    POPOTO_REDIS_DB.hset(index_key, new_hash, new_db_key.redis_key)

            self._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            self._saved_field_values = {
                field_name: getattr(self, field_name)
                for field_name in self._meta.fields.keys()
            }
            return db_response

    @classmethod
    def create(cls, pipeline: redis.client.Pipeline = None, **kwargs):
        instance = cls(**kwargs)
        pipeline_or_db_response = instance.save(pipeline=pipeline)
        return pipeline_or_db_response if pipeline else instance

    @classmethod
    def load(cls, db_key: str = None, **kwargs):
        return cls.query.get(db_key=db_key or cls(**kwargs).db_key)

    def delete(self, pipeline: redis.client.Pipeline = None, *args, **kwargs):
        """
        Model instance delete method. Uses Redis DELETE command with key.
        Also triggers all field on_delete methods.
        1. delete object as hashmap
        2. delete from class set
        3. run field on_delete methods
        4. reset private vars
        returns pipeline or boolean(object existed AND was deleted)
        """
        delete_redis_key = self._redis_key or self.db_key.redis_key
        db_response = False

        if pipeline:
            pipeline = pipeline.delete(delete_redis_key)  # 1
        else:
            db_response = POPOTO_REDIS_DB.delete(delete_redis_key)  # 1
            pipeline = POPOTO_REDIS_DB.pipeline()

        pipeline = pipeline.srem(
            self._meta.db_class_set_key.redis_key, delete_redis_key
        )  # 2

        for field_name, field in self._meta.fields.items():  # 3
            # Use saved field values if available, otherwise fall back to current values
            # This ensures we clean up the correct Redis keys even if field values changed
            field_value = self._saved_field_values.get(
                field_name, getattr(self, field_name)
            )
            pipeline = field.on_delete(
                model_instance=self,
                field_name=field_name,
                field_value=field_value,
                pipeline=pipeline,
                saved_redis_key=delete_redis_key,
                **kwargs,
            )

        # Clean up indexes  # 4
        cleanup_values = self._saved_field_values or {
            field_name: getattr(self, field_name)
            for field_name in self._meta.fields.keys()
        }
        for field_names, is_unique in self._meta.indexes:
            field_names_tuple = tuple(field_names)
            index_key = self._meta.get_index_key(field_names_tuple)
            index_hash = self._meta.compute_index_hash_from_values(
                field_names_tuple, cleanup_values
            )
            if index_hash:
                pipeline = pipeline.hdel(index_key, index_hash)

        self._db_content = dict()  # 5
        self._saved_field_values = dict()  # 5

        if db_response is not False:
            pipeline.execute()
            return bool(db_response > 0)
        else:
            return pipeline

    @classmethod
    def get_info(cls):
        from itertools import chain

        query_filters = list(
            chain(
                *[
                    field.get_filter_query_params(field_name)
                    for field_name, field in cls._meta.fields.items()
                ]
            )
        )
        return {
            "name": cls.__name__,
            "fields": cls._meta.field_names,
            "query_filters": query_filters,
        }

    # Async methods

    async def async_save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
        """Async version of save().

        Runs the synchronous save() method in a thread pool to avoid blocking
        the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            ignore_errors: If True, log errors instead of raising exceptions
            **kwargs: Additional arguments passed to save()

        Returns:
            Pipeline or db_response depending on whether pipeline was provided
        """
        return await to_thread(
            self.save, pipeline=pipeline, ignore_errors=ignore_errors, **kwargs
        )

    async def async_delete(
        self, pipeline: redis.client.Pipeline = None, *args, **kwargs
    ):
        """Async version of delete().

        Runs the synchronous delete() method in a thread pool to avoid blocking
        the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            *args: Additional positional arguments passed to delete()
            **kwargs: Additional keyword arguments passed to delete()

        Returns:
            Pipeline or boolean(object existed AND was deleted)
        """
        return await to_thread(self.delete, pipeline=pipeline, *args, **kwargs)

    @classmethod
    async def async_create(cls, pipeline: redis.client.Pipeline = None, **kwargs):
        """Async version of create().

        Creates a new model instance and saves it to Redis in a thread pool
        to avoid blocking the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            **kwargs: Field values for the new instance

        Returns:
            Pipeline or Model instance depending on whether pipeline was provided
        """
        return await to_thread(cls.create, pipeline=pipeline, **kwargs)

    @classmethod
    async def async_load(cls, db_key: str = None, **kwargs):
        """Async version of load().

        Loads a model instance from Redis by db_key or field values in a
        thread pool to avoid blocking the event loop.

        Args:
            db_key: Optional db_key string to load
            **kwargs: Field values to construct db_key if not provided

        Returns:
            Model instance or None if not found
        """
        return await to_thread(cls.load, db_key=db_key, **kwargs)
