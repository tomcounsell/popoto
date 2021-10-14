class ModelField:
    type: type = str
    unique: bool = False
    value: str = None
    is_null: bool = False
    max_length: int = 256
    is_sort_key: bool = False
    default: str = ""

    def __init__(self, **kwargs):
        full_kwargs = {  # default
            'type': str,
            'unique': True,
            'value': None,
            'is_null': False,
            'max_length': 265,
            'is_sort_key': False,
            'default': "",
        }
        full_kwargs.update(kwargs)
        self.__dict__.update(full_kwargs)


class ModelKey(ModelField):
    unique: bool = True
    key_prefix: str = ""
    key: str = ""
    key_suffix: str = ""

    def __init__(self, **kwargs):
        super().__init__()
        full_kwargs = {  # default
            'unique': True,
            'key_prefix': "",
            'key': self.__class__.__name__,
            'key_suffix': "",
        }
        full_kwargs.update(kwargs)
        self.__dict__.update(full_kwargs)
