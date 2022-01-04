import logging
logger = logging.getLogger('POPOTO.KeyFieldMixin')


class UniqueFieldMixin:
    """
    UniqueKeyField() is equivalent to KeyField(unique=True)
    """
    unique: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        uniquekeyfield_defaults = {
            'unique': True,
        }
        self.field_defaults.update(uniquekeyfield_defaults)
        # set field options, let kwargs override
        for k, v in uniquekeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

        if not kwargs.get('unique', True):
            from ..models.base import ModelException
            raise ModelException("UniqueKey field MUST be unique")

