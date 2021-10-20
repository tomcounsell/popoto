import logging
from src.popoto.models import Model
from src.popoto.redis_db import POPOTO_REDIS_DB
from src.popoto.exceptions import ModelException

logger = logging.getLogger(__name__)


class KeyValueException(ModelException):
    pass


class KeyValueModel(Model):
    """
    stores things in redis database given a key and value
    by default uses the instance class name as the key
    recommend to uniquely identify the instance with a key prefix or suffix
    prefixes are for more specific categories of objects (eg. mammal:human:woman:Lisa )
    suffixes are for specific attributes (eg. Lisa:eye_color, Lisa:age, etc)
    """

    _key_prefix: str = ""
    _key: str = ""
    _key_suffix: str = ""

    def __init__(self, *args, **kwargs):
        # build key
        self._key_prefix = kwargs.get('key_prefix', "")
        self._key = kwargs.get('key', self.__class__.__name__)
        self._key_suffix = kwargs.get('key_suffix', "")
        self._db_key = self.get_db_key(refresh=True)
        kwargs.pop('db_key', '')
        super().__init__(db_key=self._db_key, **kwargs)

    def __str__(self):
        return str(self.get_db_key())

    def get_db_key(self, refresh=False):
        if refresh or not self._db_key:
            self._db_key = self.compile_db_key(
                key_prefix=self._key_prefix,
                key=self._key,
                key_suffix=self._key_suffix
            )
            # todo: add this line for using env in key
            # + "" if SIMULATED_ENV == "PRODUCTION" else str(SIMULATED_ENV)
        return self._db_key

    @classmethod
    def compile_db_key(cls, key: str, key_prefix: str, key_suffix: str) -> str:
        key = key or cls.__name__
        # logging.debug(f"{key}, {key_prefix}, {key_suffix}")
        return str(
            f'{key_prefix.strip(":")}:' +
            f'{key.strip(":")}' +
            f':{key_suffix.strip(":")}'
        ).replace("::", ":").strip(":")

    def save(self, *args, **kwargs):
        self._db_key = self.get_db_key()
        super().save(*args, **kwargs)

    @classmethod
    def get(cls, db_key: str, instance_key: str = None):
        key_prefix, key_suffix = db_key.split(instance_key or cls.__name__)
        key_prefix = key_prefix.strip(":")
        key_suffix = key_suffix.strip(":")
        return cls(key=instance_key or cls.__name__, key_prefix=key_prefix, key_suffix=key_suffix)

    @property
    def value(self):
        self._db_key = self.get_db_key()
        return super().value

    def delete(self, *args, **kwargs):
        self._db_key = self.get_db_key()
        return super().delete(*args, **kwargs)

    def revert(self):
        self._db_key = self.get_db_key(refresh=True)
        return super().revert()
