import logging

from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger('POPOTO.Query')


class QueryException(Exception):
    pass


class Query:
    """
    an interface for db query operations using Popoto Models
    """
    model_class: 'Model'
    options: 'ModelOptions'

    def __init__(self, model_class: 'Model'):
        self.model_class = model_class
        self.options = model_class._meta

    def get(self, db_key=None, **kwargs) -> 'Model':

        if db_key and '_auto_key' in self.options.key_field_names:
            raise QueryException(
                f"{self.model_class.__name__} does not define an explicit KeyField. Cannot perform query.get(db_key)"
            )

        elif db_key and len(self.options.key_field_names) == 1:
            kwargs[list(self.options.key_field_names)[0]] = db_key

        instances = self.filter(**kwargs)
        if len(instances) > 1:
            raise QueryException(
                f"{self.model_class.__name__} found more than one unique instance. Use `.filter()`"
            )
        instance = instances[0] if len(instances) == 1 else None

        if not instance or not hasattr(instance, 'db_key'):
            return None
        return instance

    def keys(self, catchall=False, **kwargs) -> list:
        if catchall:
            logger.warning("{catchall} is for debugging purposes only. Not for use in production environment")
            return list(POPOTO_REDIS_DB.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(POPOTO_REDIS_DB.smembers(self.model_class._meta.db_class_set_key))

    def all(self) -> list:
        redis_db_keys_list = self.keys()
        return Query.get_many_objects(self.model_class, set(redis_db_keys_list))

    def filter_for_keys_set(self, **kwargs) -> list:
        db_keys_sets = []
        employed_kwargs_set = set()

        # todo: use redis.SINTER for keyfield exact match filters

        for field_name, field in self.options.fields.items():
            # intersection of field params and filter kwargs
            params_for_field = set(kwargs.keys()) & set(field.get_filter_query_params(field_name))
            if not params_for_field:
                continue
            logger.debug(f"query on {field_name} with {params_for_field}")

            logger.debug({k: kwargs[k] for k in params_for_field})
            key_set = field.__class__.filter_query(
                self.model_class, field_name, **{k: kwargs[k] for k in params_for_field}
            )
            db_keys_sets.append(key_set)
            employed_kwargs_set = employed_kwargs_set | params_for_field

        # raise error on additional unknown query parameters
        if len(set(kwargs) - employed_kwargs_set):
            raise QueryException(f"Invalid filter parameters: {set(kwargs) - employed_kwargs_set}")

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            return []
        return list(set.intersection(*db_keys_sets))

    def filter(self, **kwargs) -> list:
        """
           Access any and all filters for the fields on the model_class
           Run query using the given paramters
           return a list of model_class objects
        """
        db_keys_set = self.filter_for_keys_set(**kwargs)
        if not len(db_keys_set):
            return []
        return Query.get_many_objects(self.model_class, db_keys_set)

    def count(self, **kwargs) -> int:
        if not len(kwargs):
            return int(POPOTO_REDIS_DB.scard(self.model_class._meta.db_class_set_key) or 0)
        return len(self.filter_for_keys_set(**kwargs))  # maybe possible to refactor to use redis.SINTERCARD

    @classmethod
    def get_many_objects(cls, model: 'Model', db_keys: set) -> list:
        from .encoding import decode_popoto_model_hashmap
        pipeline = POPOTO_REDIS_DB.pipeline()
        for db_key in db_keys:
            pipeline.hgetall(db_key)
        hashes_list = pipeline.execute()
        if {} in hashes_list:
            logger.error("one or more redis keys points to missing objects")
        return [decode_popoto_model_hashmap(model, redis_hash) for redis_hash in hashes_list if redis_hash]
