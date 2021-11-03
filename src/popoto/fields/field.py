from src.popoto.models.query import QueryException


class Field:
    type: type = str
    unique: bool = False
    indexed: bool = False
    value: str = None
    null: bool = False
    max_length: int = 256
    default: str = ""

    def __init__(self, **kwargs):
        full_kwargs = {  # default
            'type': str,
            'unique': True,
            'indexed': False,
            'value': None,
            'null': False,
            'max_length': 265,  # Redis limit is 512MB
            'default': None,
        }
        full_kwargs.update(kwargs)
        # if 'default' not in kwargs:
        #     full_kwargs['default'] = full_kwargs['type']()
        self.__dict__.update(full_kwargs)

    @classmethod
    def is_valid(cls, field, value) -> bool:
        if any([
            (value is None and not field.null),
            not isinstance(value, field.type),
            len(str(value)) > field.max_length,
        ]):
            return False
        return True

    @classmethod
    def pre_save(cls, model, field, value):
        pass

    @classmethod
    def post_save(cls, model, field, value):
        pass

    def get_filter_query_params(self, field_name: str) -> list:
        # todo: auto manage sets of db_keys to allow filter by any indexed field
        if self.indexed:
            return [
                f'{field_name}',
                f'{field_name}__isnull',
            ]
        return list()

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        raise QueryException("Sorry, query filter by any indexed field is not yet implemented.")

