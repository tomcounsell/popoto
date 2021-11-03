import msgpack
import logging
from src.popoto.redis_db import POPOTO_REDIS_DB

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

        if not db_key:
            for field_name, value in kwargs.items():
                if field_name not in self.options.indexed_field_names:
                    raise QueryException(
                        f"{field_name} is not an indexed field. Try using .filter({field_name}={value})"
                    )
            get_params = [
                (field_name, self.options.fields.get(field_name), value)
                for field_name, value in kwargs.items()
            ]

            db_key = ''
            # db_key = PopotoIndex.find(**get_params)

        instance = self.model_class(**{'db_key': db_key})
        if not instance.db_key:
            return None
        instance.load_from_db() or dict()
        if not len(instance._db_content):
            return None
        return instance

    def all(self):
        redis_db_keys_list = POPOTO_REDIS_DB.keys(f"{self.model_class.__name__}:*")
        return Query.get_many_objects(self.model_class, set(redis_db_keys_list))

    @classmethod
    def get_many_objects(cls, model: 'Model', db_keys: set):
        objects_list = []
        pipeline = POPOTO_REDIS_DB.pipeline()
        for db_key in db_keys:
            pipeline.hgetall(db_key)
        hashes_list = pipeline.execute()
        for redis_hash in hashes_list:
            objects_list.append(
                model(**{
                    key_b.decode("utf-8"): msgpack.unpackb(db_value_b)
                    for key_b, db_value_b in redis_hash.items()
                })
            )
        return objects_list

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
            from src.popoto import KeyField, GeoField, SortedField
            if isinstance(field, KeyField):

                logger.debug(f"query on {field_name}")

                # intersection of field params and filter kwargs
                params_for_keyfield = set(kwargs.keys()) & set(field.get_filter_query_params(field_name))

                logger.debug({k: kwargs[k] for k in params_for_keyfield})

                key_set = KeyField.filter_query(
                    self.model_class, field_name, **{k: kwargs[k] for k in params_for_keyfield}
                )
                if len(key_set):
                    db_keys_sets.append(key_set)
        logger.debug(db_keys_sets)
        if not db_keys_sets:
            return []
        return Query.get_many_objects(self.model_class, set.intersection(*db_keys_sets))
