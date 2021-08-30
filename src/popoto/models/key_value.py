import logging
from abc import ABC

from ..redis_db import POPOTO_REDIS_DB
from ..exceptions import ModelException

logger = logging.getLogger(__name__)


class KeyValueException(ModelException):
    pass


class KeyValueModel(ABC):
    """
    stores things in redis database given a key and value
    by default uses the instance class name as the key
    recommend to uniquely identify the instance with a key prefix or suffix
    prefixes are for more specific categories of objects (eg. mammal:human:woman:Lisa )
    suffixes are for specific attributes (eg. Lisa:eye_color, Lisa:age, etc)
    """

    def __init__(self, *args, **kwargs):
        # key for redis storage
        self._db_key_main = kwargs.get('key', self.__class__.__name__)
        self._db_key_prefix = kwargs.get('key_prefix', "")
        self._db_key_suffix = kwargs.get('key_suffix', "")
        self._db_key = self.build_db_key()
        self.value = kwargs.get('value', "")
        self.force_save = kwargs.get('force_save', False)

    def __str__(self):
        return str(self._db_key)

    @classmethod
    def format_db_key(cls, key_main: str, key_prefix: str, key_suffix: str) -> str:
        key_main = key_main or cls.__name__
        return str(
            f'{key_prefix.strip(":")}:' +
            f'{key_main.strip(":")}' +
            f':{key_suffix.strip(":")}'
        ).replace("::", ":").strip(":")

    @classmethod
    def get(cls, db_key: str, instance_key: str = None):
        key_prefix, key_suffix = db_key.split(instance_key or cls.__name__)
        key_prefix = key_prefix.strip(":")
        key_suffix = key_suffix.strip(":")
        return cls.__new__(key=instance_key or cls.__name__, key_prefix=key_prefix, key_suffix=key_suffix)

    def build_db_key(self):
        self._db_key = str(
            self.format_db_key(
                key_main=self._db_key_main,
                key_prefix=self._db_key_prefix,
                key_suffix=self._db_key_suffix
            )
            # todo: add this line for using env in key
            # + f':{SIMULATED_ENV if SIMULATED_ENV != "PRODUCTION" else ""}'
        )
        return self._db_key

    def save(self, pipeline=None, *args, **kwargs):
        if not self.value:
            raise KeyValueException("no value set, nothing to save!")
        if not self.force_save:
            # validate some rules here?
            pass
        # logger.debug(f'savingkey, value: {self.db_key}, {self.value}')
        return POPOTO_REDIS_DB.set(self._db_key, self.value)

    def get_value(self, db_key: str = "", *args, **kwargs):
        return POPOTO_REDIS_DB.get(db_key or self._db_key)



# class ModelBase(type):
#     """Metaclass for all models."""
#     def __new__(cls, name, bases, attrs, **kwargs):
#         contributable_attrs = {}
#         for obj_name, obj in attrs.items():
#             if _has_contribute_to_class(obj):
#                 contributable_attrs[obj_name] = obj
#
#
