"""
from ..redis_db import POPOTO_REDIS_DB
from ..models.base import Model
from ..fields.field import Field
from ..fields.shortcuts import KeyField

class OldModel(Model):
    key = KeyField()
    value = Field()

class NewModel(Model):
    key = KeyField()
    key2 = KeyField()
    value = Field()


pipeline = POPOTO_REDIS_DB.pipeline()
redis_keys = OldModel.query.keys()
for i, old_redis_key in enumerate(redis_keys):
    ch = OldModel.query.get(redis_key=old_redis_key)
    ch.delete()
    new_redis_key = NewModel(**{
        field_name: getattr(ch, field_name)
        for field_name in ch._meta.fields.keys()
    }).db_key.redis_key
    pipeline.rename(old_redis_key, new_redis_key)
    if i % 500 == 0:
        pipeline.execute()
        pipeline = POPOTO_REDIS_DB.pipeline()
pipeline.execute()
"""
