from src.popoto.models.base import ModelOptions


class Query:
    """
    an interface for db query operations using Popoto Models
    """
    options: ModelOptions

    def __init__(self, model: 'Model'):

        self.options = model._meta


    def get(self, db_key, **kwargs):
        self.options.indexed_fields