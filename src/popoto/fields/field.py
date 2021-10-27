
class Field:
    type: type = str
    unique: bool = False
    value: str = None
    null: bool = False
    max_length: int = 256
    default: str = ""

    def __init__(self, **kwargs):
        full_kwargs = {  # default
            'type': str,
            'unique': True,
            'value': None,
            'null': False,
            'max_length': 265,  # Redis limit is 512MB
            'is_sort_key': False,  # for sorted sets
            'default': "",
        }
        full_kwargs.update(kwargs)
        self.__dict__.update(full_kwargs)
