
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
            'default': "",
        }
        full_kwargs.update(kwargs)
        self.__dict__.update(full_kwargs)

    @classmethod
    def is_valid(cls, field, value):
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
