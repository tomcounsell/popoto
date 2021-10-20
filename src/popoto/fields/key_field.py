from time import time
from .model_field import Field


class KeyField(Field):
    unique: bool = True
    auto: bool = False
    key_prefix: str = ""
    key: str = ""
    key_suffix: str = ""

    def __init__(self, **kwargs):
        super().__init__()
        new_kwargs = {  # default
            'unique': True,
            'auto': False,
            'key_prefix': "",
            'key': self.__class__.__name__,
            'key_suffix': "",
        }
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

        if self.auto:
            self.key_suffix += f":{int(time()*10e6)}"  # unix time in microseconds
