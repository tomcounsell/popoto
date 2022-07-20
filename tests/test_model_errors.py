import sys
import os

from src.popoto.exceptions import ModelException

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto

try:

    class KeyValueModel(popoto.Model):
        key = popoto.AutoKeyField(null=True)
        value = popoto.Field(null=True)

    KeyValueModel()
except ModelException as e:
    assert "null" in str(e)
else:
    raise Exception("expected null error on AutoKeyField")
