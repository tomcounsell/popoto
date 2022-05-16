import logging

from .db_key import DB_key
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

    def get(self, db_key: DB_key = None, redis_key: str = None, **kwargs) -> 'Model':
        if not db_key and not redis_key and all([key in kwargs for key in self.options.key_field_names]):
            db_key = self.model_class(**kwargs).db_key

        if db_key and not redis_key:
            redis_key = db_key.redis_key

        if redis_key:
            from ..models.encoding import decode_popoto_model_hashmap
            hashmap = POPOTO_REDIS_DB.hgetall(redis_key)
            if not hashmap:
                return None
            instance = decode_popoto_model_hashmap(self.model_class, hashmap)

        else:
            instances = self.filter(**kwargs)
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        # or not hasattr(instance, 'db_key')
        return instance or None

    def keys(self, catchall=False, clean=False, **kwargs) -> list:
        if clean:
            logger.warning("{clean} is for debugging purposes only. Not for use in production environment")
            for db_key in list(POPOTO_REDIS_DB.smembers(self.model_class._meta.db_class_set_key.redis_key)):
                hash = POPOTO_REDIS_DB.hgetall(db_key)
                if not len(hash):
                    POPOTO_REDIS_DB.srem(self.model_class._meta.db_class_set_key.redis_key, db_key)  # 2

        if catchall:
            logger.warning("{catchall} is for debugging purposes only. Not for use in production environment")
            return list(POPOTO_REDIS_DB.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(POPOTO_REDIS_DB.smembers(self.model_class._meta.db_class_set_key.redis_key))

    def all(self, **kwargs) -> list:
        redis_db_keys_list = self.keys()
        return self.prepare_results(
            Query.get_many_objects(self.model_class, set(redis_db_keys_list)), **kwargs
        )

    def filter_for_keys_set(self, **kwargs) -> set:
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
        for unknown_query_param in set(kwargs) - employed_kwargs_set:
            if unknown_query_param in ['limit', 'order_by', 'values']:
                continue
            if any([
                unknown_query_param.startswith(field_name) for field_name in self.options.relationship_field_names
            ]):
                continue
            raise QueryException(f"Invalid filter parameters: {set(kwargs) - employed_kwargs_set}")

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            return set()
        return set.intersection(*db_keys_sets)

    def filter(self, **kwargs) -> list:
        """
           Access any and all filters for the fields on the model_class
           Run query using the given paramters
           return a list of model_class objects
        """
        limit: int = int(kwargs.pop('limit')) if 'limit' in kwargs else None
        order_by_attr_name: str = str(kwargs.pop('order_by')) if 'order_by' in kwargs else None
        # values_attr_names: tuple = kwargs.pop('values') if 'values' in kwargs else None

        db_keys_set = self.filter_for_keys_set(**kwargs)
        if not len(db_keys_set):
            return []
        return self.prepare_results(
            Query.get_many_objects(self.model_class, db_keys_set, limit=limit), **kwargs
        )

    def prepare_results(self, objects, order_by: str = "", values: tuple = (), limit: int = None, **kwargs):
        if order_by:
            order_by_attr_name = order_by
            if (not isinstance(order_by_attr_name, str)) or order_by_attr_name not in self.model_class._meta.fields:
                raise QueryException(f"order_by={order_by_attr_name} must be a field name (str)")
            attr_type = self.model_class._meta.fields[order_by_attr_name].type
            objects.sort(key=lambda item: getattr(item, order_by_attr_name) or attr_type())

        if limit and len(objects) > limit:
            objects = objects[:limit]

        return objects

    def count(self, **kwargs) -> int:
        if not len(kwargs):
            return int(POPOTO_REDIS_DB.scard(self.model_class._meta.db_class_set_key.redis_key) or 0)
        return len(self.filter_for_keys_set(**kwargs))  # maybe possible to refactor to use redis.SINTERCARD

    @classmethod
    def get_many_objects(cls, model: 'Model', db_keys: set, order_by_attr_name: str = "", limit: int = None) -> list:
        from .encoding import decode_popoto_model_hashmap
        pipeline = POPOTO_REDIS_DB.pipeline()
        for db_key in db_keys:
            pipeline.hgetall(db_key)
        hashes_list = pipeline.execute()
        if {} in hashes_list:
            logger.error("one or more redis keys points to missing objects")

        # todo: order the hashes list or objects before applying limit
        return [
            decode_popoto_model_hashmap(model, redis_hash)
            for redis_hash in hashes_list[:limit] if redis_hash
        ]
