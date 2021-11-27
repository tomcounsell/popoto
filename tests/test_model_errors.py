import sys
import os

from src.popoto.exceptions import ModelException

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto

try:
    class KeyValueModel(popoto.Model):
        """
        should look and quack like a simple key value store
        """
        key = popoto.KeyField(null=True)
        value = popoto.Field(null=True)

    KeyValueModel()
except ModelException as e:
    print(e)
    assert "null" in str(e)
else:
    raise Exception("expected null error on KeyField")
