import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto



class MyPublishableModel(popoto.Model):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.publisher = popoto.Publisher()

    def save(self, pipeline=None, *args, **kwargs):
        super().save(pipeline=pipeline, *args, **kwargs)
        self.publisher.publish({
            "key": self.db_key,
            "value": self.value
        }, pipeline=pipeline)


pub_thing = MyPublishableModel()
