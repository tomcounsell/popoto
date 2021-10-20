from .model_field import Field

class GraphNodeField(Field):
    graph_alias: str = ""
    graph_label: str = ""

    def __init__(self, **kwargs):
        super().__init__()
        new_kwargs = {  # default
            'graph_alias': "",  # required field attr?
            'graph_label': "",  # model.db_key:field_name
        }
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])
