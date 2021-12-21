import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal

from src.popoto import Model, Field, GeoField, Relationship
from src.popoto.redis_db import POPOTO_REDIS_DB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))


class Person(Model):
    uuid = Field(auto=True, unique=True)
    username = Field(type=str, query=True, unique=True)
    title = Field(type=str, query=True)

    level = Field(type=int, sorted=True, null=True)
    last_active = Field(type=datetime, sorted=True)
    location = GeoField(type=GeoField.Coordinates, units='km')
    friends = Relationship('Person', many=True)

    class Meta:
        unique_together = ('title', 'level')


class AssetPrice(Model):
    _uid = Field(type=str, unique=True, query=True, null=False)
    asset_id = Field(type=str, query=True, null=False)
    price = Field(type=Decimal)
    timestamp = Field(type=datetime, sorted=True, sort_by='asset_id', null=False)

    def pre_save(self, **kwargs):
        self._uid = f"{self.asset_id}{self.timestamp.timestamp()}"
        return super().pre_save(**kwargs)




