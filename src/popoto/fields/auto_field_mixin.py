import logging
import uuid

import redis

logger = logging.getLogger('POPOTO.field')

class AutoFieldMixin:
    """
    AutoKeyField() is equivalent to KeyField(unique-True, auto=True)
    The AutoKeyField is an auto-generated, universally unique key
    It will be automatically added to models with no specified KeyFields
    Include this field in your model if you cannot otherwise enforce a unique-together constraint with other KeyFields.
    They auto-generated key is random and newly generated for a model instance.
    Model instances with otherwise identical properties are saved as separate instances with different auto-keys.
    """
    # todo: add support for https://github.com/ai/nanoid

    auto: bool = False
    auto_uuid_length: int = 32
    auto_id: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        autokeyfield_defaults = {
            'auto': True,
            'auto_uuid_length': 32,
            'auto_id': "",
        }
        self.field_defaults.update(autokeyfield_defaults)
        # set keyfield_options, let kwargs override
        for k, v in autokeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if len(value) != field.auto_uuid_length:
            logger.error(f"auto key value is length {len(value)}. It should be {field.auto_uuid_length}")
            return False
        return super().is_valid(field, value, null_check=True, **kwargs)

    def get_new_auto_key_value(self):
        return uuid.uuid4().hex[:self.auto_uuid_length]

    def set_auto_key_value(self, force: bool = False):
        if self.auto or force:
            self.default = self.get_new_auto_key_value()

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None, **kwargs):
        return pipeline if pipeline else None


