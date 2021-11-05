from ..models.query import QueryException
import logging
logger = logging.getLogger('POPOTO.field')

class Field:
    type: type = str
    unique: bool = False
    indexed: bool = False
    value: str = None
    null: bool = False
    max_length: int = 1024
    default: str = ""

    def __init__(self, **kwargs):
        full_kwargs = {  # default
            'type': str,
            'unique': True,
            'indexed': False,
            'value': None,
            'null': False,
            'max_length': 1024,  # Redis limit is 512MB
            'default': None,
        }
        full_kwargs.update(kwargs)
        # if 'default' not in kwargs:
        #     full_kwargs['default'] = full_kwargs['type']()
        self.__dict__.update(full_kwargs)

    @classmethod
    def is_valid(cls, field, value) -> bool:
        if value is None and not field.null:
            logger.error(f"field {field} is null")
            return False
        if not field.null and not isinstance(value, field.type):
            logger.error(f"field {field} is type {field.type}. But value is {type(value)}")
            return False
        if field.type == str and len(str(value)) > field.max_length:
            return False
        return True

    @classmethod
    def pre_save(cls, model, field, value):
        pass

    @classmethod
    def on_save(cls, model: 'Model', field_name: str, field_value, pipeline=None):
        pass

    @classmethod
    def post_save(cls, model, field, value):
        pass

    def get_filter_query_params(self, field_name: str) -> list:
        # todo: auto manage sets of db_keys to allow filter by any indexed field
        if self.indexed or self.__class__.__name__ in ['GeoField', 'SortedField']:
            params = [f'{field_name}',]
            if self.null:
                params += [f'{field_name}__isnull', ]
            return params
        return list()

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        if model._meta.fields[field_name].indexed:
            raise QueryException("Query filter by any indexed field is not yet implemented.")
        else:
            raise QueryException("Query filter not possible on non-indexed fields")
