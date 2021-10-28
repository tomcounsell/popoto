from time import time
from .field import Field
import uuid



class KeyField(Field):
    """
    todo: add support for https://github.com/ai/nanoid
    """
    unique: bool = True
    indexed: bool = True
    auto: bool = False
    auto_uuid_length: int = 0
    key_prefix: str = ""
    key: str = ""
    key_suffix: str = ""

    def __init__(self, **kwargs):
        super().__init__()
        new_kwargs = {  # default
            'unique': True,
            'indexed': True,
            'auto': False,
            'auto_uuid_length': 32,
            'key_prefix': "",
            'key': "",
            'key_suffix': "",
        }
        if kwargs.get('unique', None) == False:
            from ..models.base import ModelException
            raise ModelException("key field must be unique")
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

        if self.auto:
            self.key_suffix += f":{uuid.uuid4().hex[:self.auto_uuid_length]}"

        self.default = self.get_key()

    def get_key(self):
        return str(
            f'{self.key_prefix.strip(":")}:' +
            f'{self.key.strip(":")}' +
            f':{self.key_suffix.strip(":")}'
        ).replace("::", ":").strip(":")
