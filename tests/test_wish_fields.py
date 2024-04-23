import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal

from src.popoto import Model, Field, GeoField, AutoKeyField, KeyField
from src.popoto.redis_db import POPOTO_REDIS_DB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))


class UniqueTogetherModel(Model):
    uuid = AutoKeyField()
    _alt_uid = KeyField()
    username = Field(type=str, query=True, unique=True)
    title = Field(type=str, query=True)

    level = Field(type=int, sorted=True, null=True)
    last_active = Field(type=datetime, sorted=True)
    location = GeoField(type=GeoField.Coordinates, units="km")
    # friends = Relationship('Person', many=True)

    class Meta:
        unique_together = ("title", "level")

    def pre_save(self, **kwargs):
        self._alt_uid = f"{self.title}{self.level}"
        return super().pre_save(**kwargs)
