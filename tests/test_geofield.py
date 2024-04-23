import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class GeoModel(popoto.Model):
    key = popoto.KeyField()
    coordinates = popoto.GeoField()


rome = GeoModel(key="Rome")
rome.coordinates = popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
rome.save()

assert rome in GeoModel.query.filter(
    coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
)
assert rome in GeoModel.query.filter(
    coordinates_latitude=41.902782, coordinates_longitude=12.496366
)

vatican = GeoModel(key="Vatican")
vatican.coordinates = popoto.GeoField.Coordinates(
    latitude=41.904755, longitude=12.454628
)
vatican.save()

assert vatican in GeoModel.query.filter(
    coordinates=rome.coordinates, coordinates_radius=5, coordinates_radius_unit="km"
)
assert rome in GeoModel.query.filter(
    coordinates=vatican.coordinates, coordinates_radius=5, coordinates_radius_unit="km"
)

area51 = GeoModel.create(key="Area 51")
area51.coordinates = (None, None)
area51.save()

for item in GeoModel.query.all():
    item.delete()
