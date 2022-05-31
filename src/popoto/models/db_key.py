from collections.abc import Iterable

from ..redis_db import POPOTO_REDIS_DB, ENCODING


class DB_key(list):
    def __init__(self, *key_partials):
        def flatten(yet_flat):
            if isinstance(yet_flat, Iterable) and not isinstance(yet_flat, (str, bytes)):
                for item in yet_flat:
                    yield from flatten(item)
            else:
                yield yet_flat

        super().__init__(flatten(key_partials))

    @classmethod
    def from_redis_key(cls, redis_key):
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode(ENCODING)
        return cls([DB_key.unclean(partial) for partial in redis_key.split(":")])

    @classmethod
    def clean(cls, value: str) -> str:
        value = value.replace('/', '//')
        for char in "'?*^[]-":
            value = value.replace(char, f"/{char}")
        value = value.replace(':', '{&#58;}')
        return value

    @classmethod
    def unclean(cls, value: str) -> str:
        value = value.replace('{&#58;}', ':')
        for char in "'?*^[]-":
            value = value.replace(f"/{char}", char)
        value = value.replace('//', '/',)
        return value

    def __str__(self):
        return ":".join([
            str(partial) if isinstance(partial, DB_key) else self.clean(str(partial))
            for partial in self
        ])

    @property
    def redis_key(self):
        return str(self)

    def exists(self):
        return True if POPOTO_REDIS_DB.exists(self.redis_key) > 0 else False

    def get_instance(self, model_class):
        redis_hash = POPOTO_REDIS_DB.hgetall(self.redis_key)
        from .encoding import decode_popoto_model_hashmap
        return decode_popoto_model_hashmap(model_class, redis_hash)
