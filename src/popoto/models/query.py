import msgpack
import logging

from .encoding import decode_popoto_model_hashmap
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

    def get(self, db_key=None, **kwargs):

        if db_key and '_auto_key' in self.options.key_field_names:
            raise QueryException(
                f"{self.model_class.__name__} does not define an explicit KeyField. Cannot perform query.get(key)"
            )

        elif db_key and len(self.options.key_field_names) == 1:
            kwargs[self.options.key_field_names[0]] = db_key

        instances = self.filter(**kwargs)
        if len(instances) > 1:
            raise QueryException(
                f"{self.model_class.__name__} found more than one unique instance. Use `.filter()`"
            )
        instance = instances[0] if len(instances) == 1 else None

        if not instance or not hasattr(instance, 'db_key'):
            return None

        return instance

    def all(self):
        # todo: refactor to use SCAN or sets, https://redis.io/commands/keys
        redis_db_keys_list = POPOTO_REDIS_DB.keys(f"{self.model_class._meta.db_class_key}:*")
        return Query.get_many_objects(self.model_class, set(redis_db_keys_list))

    @classmethod
    def get_many_objects(cls, model: 'Model', db_keys: set):
        pipeline = POPOTO_REDIS_DB.pipeline()
        for db_key in db_keys:
            pipeline.hgetall(db_key)
        hashes_list = pipeline.execute()
        return [decode_popoto_model_hashmap(model, redis_hash) for redis_hash in hashes_list]

    def filter(self, **kwargs):
        """
           Access any and all filters for the fields on the model_class
           Run query using the given paramters
           return a list of model_class objects
        """
        from itertools import chain
        all_valid_filter_parameters = list(chain(*[
            field.get_filter_query_params(field_name)
            for field_name, field in self.options.fields.items()
        ]))

        db_keys_sets = []

        for field_name, field in self.options.fields.items():

            from ..fields.key_field import KeyField
            from ..fields.geo_field import GeoField
            queryable_field_classes = [KeyField, GeoField]

            params_for_field = set(kwargs.keys()) & set(field.get_filter_query_params(field_name))
            if not params_for_field:
                continue

            logger.debug(f"query on {field_name}")
            # intersection of field params and filter kwargs

            logger.debug({k: kwargs[k] for k in params_for_field})
            key_set = field.__class__.filter_query(
                self.model_class, field_name, **{k: kwargs[k] for k in params_for_field}
            )
            db_keys_sets.append(key_set)

        logger.debug(db_keys_sets)
        if not db_keys_sets:
            return []
        return Query.get_many_objects(self.model_class, set.intersection(*db_keys_sets))
