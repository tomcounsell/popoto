import redis
from ..models.base import Model, ModelException
from .field import Field
import logging
logger = logging.getLogger('POPOTO.Relationship')


class Relationship(Field):
    """
    A field that stores references to one or more other model instances.
    """
    type: Model = Model
    model: Model = None
    many: bool = False
    null: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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
            for related_field_name, field in self.model.options.fields.items()
            if not isinstance(field, Relationship)  # not ready for recursive compilation
        ]

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value: str, pipeline=None, **kwargs):
        return pipeline if pipeline else None

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None, **kwargs):
        return pipeline if pipeline else None

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        field = model._meta.fields[field_name]

        for query_param, query_value in query_params.items():

            if query_param == f'{field_name}':
                # return list of keys where instances have .field_name = query_value
                return [query_value.db_key,]


        redis_db_keys_list = []
        return set(redis_db_keys_list)
