import redis
from .field import Field
import logging

from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger('POPOTO.Relationship')


class Relationship(Field):
    """
    A field that stores references to one or more other model instances.
    """
    type: 'Model' = None
    model: 'Model' = None
    many: bool = False
    null: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        from ..models.base import Model
        relationship_field_defaults = {
            'type': Model,
            'model': None,
            'many': False,
            'null': True,
        }
        self.field_defaults.update(relationship_field_defaults)
        # set field options, let kwargs override
        for k, v in relationship_field_defaults.items():
            setattr(self, k, kwargs.get(k, v))


    def get_filter_query_params(self, field_name):
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',
        ] + [
            f'{field_name}__{field.get_filter_query_params(related_field_name)}'
            for related_field_name, field in self.model._meta.fields.items()
            if not isinstance(field, Relationship)  # not ready for recursive compilation
        ]

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value: str, pipeline=None, **kwargs):
        from ..models.base import Model
        # on a one-to-many, save the set of many with the related instance
        # add this instance's id to a relationship set based on the related model
        # example: "$RelationshipF:Membership:person:person_db_key"
        relationship_set_key = f'{cls.get_special_use_field_db_key(model_instance, field_name)}:{field_value.db_key}'

        if pipeline and field_value is None:
            return pipeline.srem(relationship_set_key, model_instance.db_key)
        elif pipeline and isinstance(field_value, Model):
            return pipeline.sadd(relationship_set_key, model_instance.db_key)
        elif field_value is None:
            return POPOTO_REDIS_DB.srem(relationship_set_key, model_instance.db_key)
        elif isinstance(field_value, Model):
            return POPOTO_REDIS_DB.sadd(relationship_set_key, model_instance.db_key)
        else:
            return pipeline if pipeline else None

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None, **kwargs):
        relationship_set_key = f'{cls.get_special_use_field_db_key(model_instance, field_name)}:{field_value.db_key}'
        if pipeline:
            return pipeline.srem(relationship_set_key, model_instance.db_key)
        else:
            return POPOTO_REDIS_DB.srem(relationship_set_key, model_instance.db_key)

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        from ..models.base import Model

        keys_lists_to_intersect = list()
        pipeline = POPOTO_REDIS_DB.pipeline()

        def clean_query_value(value: str) -> str:
            for char in "'?*^[]-/":
                value = value.replace(char, f"/{char}")
            return value

        for query_param, query_value in query_params.items():

            if query_param == f'{field_name}':
                if not isinstance(query_value, Model):
                    raise QueryException(f"Query filter on Relationship expects model instance. Instead, got {query_value}")

                relationship_set_key = f'{cls.get_special_use_field_db_key(model, field_name)}:{clean_query_value(query_value.db_key)}'
                keys_lists_to_intersect.append(
                    POPOTO_REDIS_DB.smembers(relationship_set_key)
                )

            elif query_param.startswith(f'{field_name}__'):
                field = model._meta.fields[field_name]
                relationship_field_name = query_param.strip(f'{field_name}__').split("__")[0]

                relationship_field_values = field.model._meta.fields[relationship_field_name].filter_query(
                    model=field.model, field_name=relationship_field_name,
                    **{query_param.strip(f'{field_name}__'): query_value}
                )
                # note: this will be recursive if references another relationship and so forth

                for relationship_field_value in relationship_field_values:
                    relationship_set_key = f'{cls.get_special_use_field_db_key(model, field_name)}:{clean_query_value(relationship_field_value.db_key)}'
                    pipeline.smembers(relationship_set_key)

        keys_lists_to_intersect += pipeline.execute()
        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(*[set(key_list) for key_list in keys_lists_to_intersect])
        return set()

