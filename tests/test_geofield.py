import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

from src import popoto

# Backend conformance (#759 M5, plan §5 M5 gate (b)). This module used to run
# its assertions at import time, so they ran once, on Redis, during
# collection; they are a test now, so each backend leg runs them.
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]


class GeoModel(popoto.Model):
    key = popoto.KeyField()
    coordinates = popoto.GeoField()


def test_geofield_radius_search():
    rome = GeoModel(key="Rome")
    rome.coordinates = popoto.GeoField.Coordinates(
        latitude=41.902782, longitude=12.496366
    )
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
        coordinates=vatican.coordinates,
        coordinates_radius=5,
        coordinates_radius_unit="km",
    )

    area51 = GeoModel.create(key="Area 51")
    area51.coordinates = (None, None)
    area51.save()

    for item in GeoModel.query.all():
        item.delete()
    assert GeoModel.query.count() == 0
