from collections.abc import Iterable

from ..redis_db import POPOTO_REDIS_DB


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
    def clean(cls, value: str, ignore_colons: bool = False) -> str:
        value = value.replace('/', '//')
        for char in "'?*^[]-":
            value = value.replace(char, f"/{char}")
        if not ignore_colons:
            value = value.replace(':', '_')
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
