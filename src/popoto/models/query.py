
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
        if db_key and self.options.db_key_field_name == '_auto_key':
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

        instance = self.model_class(**{self.options.db_key_field_name: db_key})
        if not instance.db_key:
            print(repr(instance))
            return None
        instance.load_from_db() or dict()
        if not len(instance._db_content):
            return None
        return instance


    def filter(self, **kwargs):
        """

        """


        field_filters = {field_name: field.query_filters for field_name, field in self.options.fields.items()}
        


