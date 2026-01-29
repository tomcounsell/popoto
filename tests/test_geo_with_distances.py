"""
Tests for GeoField with_distances feature (Issue #24)
"""
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto


class Location(popoto.Model):
    name = popoto.KeyField()
    coordinates = popoto.GeoField()


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data before and after each test"""
    for item in Location.query.all():
        item.delete()
    yield
    for item in Location.query.all():
        item.delete()


class TestGeoWithDistances:
    def test_basic_with_distances(self):
        """Test that with_distances returns distance values on instances"""
        # Create two locations
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )
        vatican = Location.create(
            name="Vatican",
            coordinates=popoto.GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
        )

        # Query with distances from Rome's coordinates
        results = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10,
            coordinates_radius_unit='km',
            coordinates_with_distances=True
        )

        assert len(results) == 2

        # All results should have _geo_distance attribute
        for result in results:
            assert hasattr(result, '_geo_distance')
            assert hasattr(result, '_geo_distance_unit')
            assert result._geo_distance_unit == 'km'
            assert isinstance(result._geo_distance, float)

    def test_distances_are_accurate(self):
        """Test that returned distances are approximately correct"""
        # Rome coordinates
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )
        # Vatican is about 3.5km from Rome center
        vatican = Location.create(
            name="Vatican",
            coordinates=popoto.GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
        )

        results = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10,
            coordinates_radius_unit='km',
            coordinates_with_distances=True
        )

        # Find Rome and Vatican in results
        rome_result = next(r for r in results if r.name == "Rome")
        vatican_result = next(r for r in results if r.name == "Vatican")

        # Rome should be at distance 0 (querying from its own location)
        assert rome_result._geo_distance == 0.0

        # Vatican should be approximately 3.5km away
        assert 3.0 <= vatican_result._geo_distance <= 4.0

    def test_distances_sorted_ascending(self):
        """Test that results with distances are sorted by distance (closest first)"""
        # Create locations at different distances from Rome
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )
        vatican = Location.create(
            name="Vatican",
            coordinates=popoto.GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
        )
        # Florence is about 230km from Rome
        florence = Location.create(
            name="Florence",
            coordinates=popoto.GeoField.Coordinates(latitude=43.769560, longitude=11.255814)
        )

        results = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=500,
            coordinates_radius_unit='km',
            coordinates_with_distances=True
        )

        assert len(results) == 3

        # Should be sorted by distance: Rome (0), Vatican (~3.5km), Florence (~230km)
        assert results[0].name == "Rome"
        assert results[0]._geo_distance == 0.0

        assert results[1].name == "Vatican"
        assert results[1]._geo_distance < 10  # Should be ~3.5km

        assert results[2].name == "Florence"
        assert results[2]._geo_distance > 200  # Should be ~230km

    def test_with_distances_different_units(self):
        """Test that different distance units work correctly"""
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )
        vatican = Location.create(
            name="Vatican",
            coordinates=popoto.GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
        )

        # Query in meters
        results_m = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10000,
            coordinates_radius_unit='m',
            coordinates_with_distances=True
        )
        vatican_m = next(r for r in results_m if r.name == "Vatican")
        assert vatican_m._geo_distance_unit == 'm'
        assert vatican_m._geo_distance > 3000  # Should be ~3500m

        # Query in miles
        results_mi = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10,
            coordinates_radius_unit='mi',
            coordinates_with_distances=True
        )
        vatican_mi = next(r for r in results_mi if r.name == "Vatican")
        assert vatican_mi._geo_distance_unit == 'mi'
        assert vatican_mi._geo_distance < 3  # Should be ~2.2mi

    def test_with_distances_by_member(self):
        """Test with_distances using a member instance instead of coordinates"""
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )
        vatican = Location.create(
            name="Vatican",
            coordinates=popoto.GeoField.Coordinates(latitude=41.904755, longitude=12.454628)
        )

        # Query by member (from Rome)
        results = Location.query.filter(
            coordinates_member=rome,
            coordinates_radius=10,
            coordinates_radius_unit='km',
            coordinates_with_distances=True
        )

        assert len(results) == 2

        rome_result = next(r for r in results if r.name == "Rome")
        vatican_result = next(r for r in results if r.name == "Vatican")

        assert rome_result._geo_distance == 0.0
        assert vatican_result._geo_distance > 0

    def test_without_distances_no_attribute(self):
        """Test that without with_distances, objects don't have distance attributes"""
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )

        # Query without with_distances
        results = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10,
            coordinates_radius_unit='km'
        )

        assert len(results) == 1
        assert not hasattr(results[0], '_geo_distance')

    def test_with_distances_empty_result(self):
        """Test that empty results work correctly with with_distances"""
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )

        # Query from a location with no nearby results
        results = Location.query.filter(
            coordinates=(0.0, 0.0),  # Middle of Atlantic
            coordinates_radius=1,
            coordinates_radius_unit='km',
            coordinates_with_distances=True
        )

        assert len(results) == 0

    def test_with_distances_false_explicit(self):
        """Test that explicitly setting with_distances=False works"""
        rome = Location.create(
            name="Rome",
            coordinates=popoto.GeoField.Coordinates(latitude=41.902782, longitude=12.496366)
        )

        results = Location.query.filter(
            coordinates=(41.902782, 12.496366),
            coordinates_radius=10,
            coordinates_radius_unit='km',
            coordinates_with_distances=False
        )

        assert len(results) == 1
        assert not hasattr(results[0], '_geo_distance')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
