import logging

from .db_key import DB_key
from ..redis_db import POPOTO_REDIS_DB, ENCODING

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
            pipeline = POPOTO_REDIS_DB.pipeline()
            from ..fields.key_field_mixin import KeyFieldMixin
            from ..fields.relationship import Relationship

            for db_key in list(POPOTO_REDIS_DB.smembers(self.model_class._meta.db_class_set_key.redis_key)):
                hash = POPOTO_REDIS_DB.hgetall(db_key)
                if not len(hash):
                    pipeline = pipeline.srem(self.model_class._meta.db_class_set_key.redis_key, db_key)

            # find
            for field_name, field in self.model_class._meta.fields.items():  # 3
                if not isinstance(field, (KeyFieldMixin, Relationship)):
                    continue
                field_key_prefix = field.get_special_use_field_db_key(self.model_class, field_name)
                for field_key in POPOTO_REDIS_DB.keys(f"{field_key_prefix}:*"):
                    for object_key in POPOTO_REDIS_DB.smembers(field_key):
                        hash = POPOTO_REDIS_DB.hgetall(object_key)
                        if not len(hash):
                            pipeline = pipeline.srem(field_key, object_key)

            pipeline.execute()

        if catchall:
            logger.warning("{catchall} is for debugging purposes only. Not for use in production environment")
            return list(POPOTO_REDIS_DB.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(POPOTO_REDIS_DB.smembers(self.model_class._meta.db_class_set_key.redis_key))

    def all(self, **kwargs) -> list:
        redis_db_keys_list = self.keys()
        return self.prepare_results(
            Query.get_many_objects(self.model_class, set(redis_db_keys_list), values=kwargs.get('values', None)),
            **kwargs
        )

    def filter_for_keys_set(self, **kwargs) -> set:
        db_keys_sets = []
        yet_employed_kwargs_set = set(kwargs.keys()).difference({'limit', 'order_by', 'values'})
        if not len(yet_employed_kwargs_set):
            return set()

        # todo: use redis.SINTER for keyfield exact match filters

        # do sorted_fields first - because they can obviate some keyfield filters
        for field_name in self.options.sorted_field_names:
            field = self.options.fields[field_name]
            if not len(yet_employed_kwargs_set & self.options.filter_query_params_by_field[field_name]):
                continue  # this field cannot use any of the available filter params
            logger.debug(f"query on {field_name} with {self.options.filter_query_params_by_field[field_name]}")
            logger.debug({k: kwargs[k] for k in self.options.filter_query_params_by_field[field_name] if k in kwargs})
            db_keys_sets.append(field.__class__.filter_query(
                self.model_class, field_name, **kwargs
            ))
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                self.options.filter_query_params_by_field[field_name]
            ).difference(set(field.sort_by))  # also remove the required sort_by field names

        for field_name in self.options.filter_query_params_by_field:
            if field_name in self.options.sorted_field_names:
                continue  # already handled
            params_for_field = yet_employed_kwargs_set & set(self.options.filter_query_params_by_field[field_name])
            if not params_for_field:
                continue  # this field cannot use any of the available filter params

            field = self.options.fields[field_name]
            logger.debug(f"query on {field_name} with {params_for_field}")
            logger.debug({k: kwargs[k] for k in params_for_field})
            key_set = field.__class__.filter_query(
                self.model_class, field_name, **{k: kwargs[k] for k in params_for_field}
            )
            db_keys_sets.append(key_set)
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(params_for_field)

        # raise error on additional unknown query parameters
        if yet_employed_kwargs_set:
            raise QueryException(f"Invalid filter parameters: {','.join(yet_employed_kwargs_set)}")

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            return set()
        # return intersection of all the db keys sets, effectively &&-ing all filters
        return set.intersection(*db_keys_sets)

    def filter(self, **kwargs) -> list:
        """
           Access any and all filters for the fields on the model_class
           Run query using the given paramters
           return a list of model_class objects
        """
        db_keys_set = self.filter_for_keys_set(**kwargs)
        if not len(db_keys_set):
            return []

        return self.prepare_results(
            Query.get_many_objects(
                self.model_class, db_keys_set,
                order_by_attr_name=kwargs.get('order_by', None),
                limit=kwargs.get('limit', None),
                values=kwargs.get('values', None),
            ),
            **kwargs
        )

    def prepare_results(self, objects, order_by: str = "", values: tuple = (), limit: int = None, **kwargs):
        reverse_order = False
        if order_by and order_by.startswith("-"):
            reverse_order = True
            order_by = order_by[1:]
        if order_by:
            order_by_attr_name = order_by
            if (not isinstance(order_by_attr_name, str)) or order_by_attr_name not in self.model_class._meta.fields:
                raise QueryException(f"order_by={order_by_attr_name} must be a field name (str)")
            attr_type = self.model_class._meta.fields[order_by_attr_name].type
            if values and order_by_attr_name not in values:
                raise QueryException("field must be included in values=(fieldnames) in order to use order_by")
            elif values:
                objects.sort(key=lambda item: item.get(order_by_attr_name))
            else:
                objects.sort(key=lambda item: getattr(item, order_by_attr_name) or attr_type())
            objects = list(reversed(objects))[:limit] if reverse_order else objects[:limit]

        if limit and len(objects) > limit:
            objects = objects[:limit]

        return objects

    def count(self, **kwargs) -> int:
        if not len(kwargs):
            return int(POPOTO_REDIS_DB.scard(self.model_class._meta.db_class_set_key.redis_key) or 0)
        return len(self.filter_for_keys_set(**kwargs))  # maybe possible to refactor to use redis.SINTERCARD

    @classmethod
    def get_many_objects(cls, model: 'Model', db_keys: set,
                         order_by_attr_name: str = None, limit: int = None, values: tuple = None) -> list:
        from .encoding import decode_popoto_model_hashmap
        pipeline = POPOTO_REDIS_DB.pipeline()
        reverse_order = False
        # order the hashes list or objects before applying limit
        if order_by_attr_name and order_by_attr_name.startswith("-"):
            order_by_attr_name = order_by_attr_name[1:]
            reverse_order = True

        if order_by_attr_name and order_by_attr_name in model._meta.key_field_names:
            field_position = model._meta.get_db_key_index_position(order_by_attr_name)
            db_keys = list(db_keys)
            db_keys.sort(key=lambda key: key.split(b":")[field_position])
            db_keys = list(reversed(db_keys))[:limit] if reverse_order else db_keys[:limit]

        if values:
            if not isinstance(values, tuple):
                raise QueryException("values takes a tuple. eg. query.filter(values=('name',))")
            elif set(values).issubset(model._meta.key_field_names):
                db_keys = [DB_key.from_redis_key(db_key) for db_key in db_keys]
                return [
                    {
                        field_name: model._meta.fields[field_name].type(
                            db_key[model._meta.get_db_key_index_position(field_name)]
                        ) if db_key[model._meta.get_db_key_index_position(field_name)] else None
                        for field_name in values
                    }
                    for db_key in db_keys
                ]
            else:
                [pipeline.hmget(db_key, values) for db_key in db_keys]
                value_lists = pipeline.execute()
                hashes_list = [
                    {
                        field_name: result[i]
                        for i, field_name in enumerate(values)
                    }
                    for result in value_lists
                ]

        else:
            [pipeline.hgetall(db_key) for db_key in db_keys]
            hashes_list = pipeline.execute()

        if {} in hashes_list:
            logger.error("one or more redis keys points to missing objects. Debug with Model.query.keys(clean=True)")

        return [
            decode_popoto_model_hashmap(model, redis_hash, fields_only=bool(values))
            for redis_hash in hashes_list if redis_hash
        ]
